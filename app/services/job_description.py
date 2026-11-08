"""Structured job descriptions: the employer's posting turned into machine-readable requirements.

    raw description (never rewritten) → normalize → parse (model, or deterministic cues) → validate schema
    → jobs.structured_description + JobRequirementEvidence rows (each with the sentence it came from)

Parsing runs as its own database-backed task (JobDescriptionParseTask), claimed by the scheduler the way ranking
tasks are. A job that cannot be parsed never blocks the pipeline: after its attempts run out the ranker falls back
to the raw description.

Rules this module enforces:
* `Job.description` is the employer's text and is only ever read here.
* Nothing is invented: every requirement carries source_text, and unknown values stay null or empty.
* REQUIRED and PREFERRED are kept apart, and so are explicit employer statements and derived signals.
* A parser never overwrites a value the user corrected: a re-parse of a corrected job becomes a candidate version
  the user resolves field by field.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import desc, event, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ACTIVE_JD_FETCH_STATUSES,
    ACTIVE_JD_PARSE_STATUSES,
    REQUIREMENT_LEVELS,
    REQUIREMENT_TYPES,
    Job,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobRequirementEvidence,
    JobStructuredDataChange,
)
from app.prompts.job_description_parser import (
    JOB_DESCRIPTION_PARSER_PROMPT_VERSION,
    build_job_description_parser_prompt,
)
from app.services import backoff, worker_settings
from app.services.lifecycle import STOPPED_BY_USER, ErrorCategory
from app.settings import get_settings

log = logging.getLogger(__name__)

PENDING, PROCESSING, COMPLETED, FAILED = "PENDING", "PROCESSING", "COMPLETED", "FAILED"
RAW, NORMALIZED, PARSED, NEEDS_REVIEW, MANUALLY_CORRECTED = ("RAW", "NORMALIZED", "PARSED", "NEEDS_REVIEW",
                                                             "MANUALLY_CORRECTED")
PARSER_VERSION = JOB_DESCRIPTION_PARSER_PROMPT_VERSION
MAX_ERROR_LENGTH = 4000
OUTCOMES = ("parsed", "retrying", "waiting", "failed", "skipped", "superseded", "lost")


class ParseLost(RuntimeError):
    """The task is no longer held by this worker: its lease expired and was taken over, or it was deleted."""


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def description_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ───────────── normalization ─────────────

_TAGS = re.compile(r"<[^>]+>")
_BREAKS = re.compile(r"<\s*(?:br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", re.IGNORECASE)
_ITEMS = re.compile(r"<\s*li[^>]*>", re.IGNORECASE)
_BULLETS = re.compile(r"^\s*(?:[-*•●▪‣⁃◦·]|\d+[.)])\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
HEADING_WORDS = ("requirement", "qualification", "responsibilit", "what you", "who you are", "about", "benefit",
                 "nice to have", "preferred", "must have", "skills", "experience", "role", "job", "we offer",
                 "perks", "compensation", "salary", "location", "how to apply", "you will", "the team")


def normalize_description(text: str) -> str:
    """The posting as clean text: HTML removed, bullets normalized, blank-line runs collapsed. Wording untouched."""
    out = _ITEMS.sub("\n- ", text or "")
    out = _BREAKS.sub("\n", out)
    out = _TAGS.sub(" ", out)
    out = html.unescape(out).replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    out = _CONTROL.sub("", out)
    lines = []
    for raw in out.split("\n"):
        line = re.sub(r"[  ]{2,}", " ", raw).strip()
        lines.append(f"- {_BULLETS.sub('', line)}" if _BULLETS.match(line) else line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def is_heading(line: str) -> bool:
    clean = line.strip().rstrip(":").strip()
    if not clean or len(clean) > 80 or clean.startswith("- "):
        return False
    lowered = clean.lower()
    return (line.rstrip().endswith(":") and len(clean.split()) <= 8) or (
        clean == clean.upper() and len(clean.split()) <= 8 and any(ch.isalpha() for ch in clean)) or (
        len(clean.split()) <= 6 and any(word in lowered for word in HEADING_WORDS) and not clean.endswith("."))


def sections_of(text: str) -> list[tuple[str, list[str]]]:
    """(heading, lines) for each section of the posting; lines before the first heading sit under ""."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if is_heading(line):
            sections.append((line.strip().rstrip(":").strip(), []))
        else:
            sections[-1][1].append(line.strip())
    return [(heading, lines) for heading, lines in sections if lines]


def sentences(line: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", line.lstrip("- ").strip())
    return [p.strip() for p in parts if p.strip()]


# ───────────── schema ─────────────

class _In(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class JobIdentityIn(_In):
    title: str | None = Field(default=None, max_length=300)
    company: str | None = Field(default=None, max_length=300)
    location: str | None = Field(default=None, max_length=300)
    employment_type: str | None = Field(default=None, max_length=60)
    remote_type: str | None = Field(default=None, max_length=40)
    seniority: str | None = Field(default=None, max_length=100)


class ExperienceRequirementsIn(_In):
    minimum_years: float | None = Field(default=None, ge=0, le=60)
    maximum_years: float | None = Field(default=None, ge=0, le=60)
    leadership_required: bool | None = None
    management_required: bool | None = None


class LocationRequirementsIn(_In):
    country: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=120)
    relocation_supported: bool | None = None
    visa_sponsorship: bool | None = None
    work_authorization_required: bool | None = None


class CompensationIn(_In):
    min: float | None = Field(default=None, ge=0)
    max: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=10)
    period: str | None = Field(default=None, max_length=20)
    source_text: str | None = Field(default=None, max_length=1000)
    source: str | None = Field(default=None, max_length=20)  # job_description | job_board


class DerivedSignalsIn(_In):
    likely_interview_domains: list[str] = Field(default_factory=list, max_length=20)
    likely_system_design_topics: list[str] = Field(default_factory=list, max_length=20)
    likely_leadership_topics: list[str] = Field(default_factory=list, max_length=20)
    likely_business_topics: list[str] = Field(default_factory=list, max_length=20)


Phrases = Field(default_factory=list, max_length=60)


class StructuredJobDescriptionIn(_In):
    job_identity: JobIdentityIn = Field(default_factory=JobIdentityIn)
    role_summary: str | None = Field(default=None, max_length=4000)
    responsibilities: list[str] = Phrases
    required_skills: list[str] = Phrases
    preferred_skills: list[str] = Phrases
    experience_requirements: ExperienceRequirementsIn = Field(default_factory=ExperienceRequirementsIn)
    technical_domains: list[str] = Phrases
    leadership_expectations: list[str] = Phrases
    education_requirements: list[str] = Phrases
    location_requirements: LocationRequirementsIn = Field(default_factory=LocationRequirementsIn)
    compensation: CompensationIn = Field(default_factory=CompensationIn)
    hard_requirements: list[str] = Phrases
    nice_to_have: list[str] = Phrases
    keywords: list[str] = Field(default_factory=list, max_length=80)
    company_context: list[str] = Phrases
    derived_signals: DerivedSignalsIn = Field(default_factory=DerivedSignalsIn)


class RequirementIn(_In):
    id: str | None = Field(default=None, max_length=60)
    type: str = Field(default="OTHER", max_length=25)
    normalized_value: str = Field(max_length=300)
    requirement_level: str = Field(default="UNKNOWN", max_length=15)
    source_text: str = Field(default="", max_length=4000)
    source_section: str | None = Field(default=None, max_length=120)
    confidence: float = Field(default=0.5, ge=0, le=1)
    is_explicit: bool = True
    is_derived: bool = False

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, value: Any) -> str:
        clean = str(value or "").strip().upper()
        return clean if clean in REQUIREMENT_TYPES else "OTHER"

    @field_validator("requirement_level", mode="before")
    @classmethod
    def _level(cls, value: Any) -> str:
        clean = str(value or "").strip().upper()
        return clean if clean in REQUIREMENT_LEVELS else "UNKNOWN"


class ParsedJobDescription(_In):
    structured_description: StructuredJobDescriptionIn = Field(default_factory=StructuredJobDescriptionIn)
    requirements: list[RequirementIn] = Field(default_factory=list, max_length=200)


# ───────────── deterministic extraction ─────────────

REQUIRED_CUES = ("required", "must have", "must be", "you have", "you must", "essential", "minimum of", "at least",
                 "we require", "is a requirement", "proven", "demonstrated", "strong experience")
PREFERRED_CUES = ("nice to have", "preferred", "a plus", "bonus", "ideally", "desirable", "would be great",
                  "familiarity with", "exposure to", "good to have", "advantage")
LEADERSHIP_CUES = ("lead", "leading", "mentor", "manage", "management", "hiring", "coach", "line manager",
                   "direct reports", "stakeholder")
EDUCATION_CUES = ("degree", "bachelor", "master", "phd", "b.tech", "b.e.", "m.tech", "computer science")
VISA_CUES = ("visa", "sponsorship", "sponsor", "work permit", "work authorization", "work authorisation",
             "right to work")
LOCATION_CUES = ("onsite", "on-site", "hybrid", "remote", "relocate", "relocation", "based in", "office")
RESPONSIBILITY_CUES = ("you will", "responsible for", "own", "build", "design", "deliver", "drive", "partner with",
                       "collaborate")
COMPANY_CUES = ("we are", "our mission", "our team is", "founded", "our customers", "about us", "we believe")
YEARS_RE = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(\d{1,2})?\s*\+?\s*years?", re.IGNORECASE)
SALARY_RE = re.compile(
    r"(?P<currency>[$€£₹]|USD|EUR|GBP|INR|SGD)\s?(?P<min>\d[\d,]*(?:\.\d+)?)\s?(?P<min_unit>[kKmM]|lakh|lpa)?"
    r"(?:\s*(?:-|–|to)\s*(?P<currency2>[$€£₹])?\s?(?P<max>\d[\d,]*(?:\.\d+)?)\s?(?P<max_unit>[kKmM]|lakh|lpa)?)?",
    re.IGNORECASE)
PERIOD_WORDS = {"year": "year", "annum": "year", "annual": "year", "yearly": "year", "month": "month",
                "hour": "hour", "week": "week", "day": "day"}
CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}
# Words that name work rather than a requirement of the candidate.
SECTION_LEVEL = {"requirement": "REQUIRED", "qualification": "REQUIRED", "must have": "REQUIRED",
                 "what you": "REQUIRED", "who you are": "REQUIRED", "nice to have": "PREFERRED",
                 "preferred": "PREFERRED", "bonus": "PREFERRED", "responsibilit": "INFORMATIONAL",
                 "you will": "INFORMATIONAL", "about": "INFORMATIONAL", "benefit": "INFORMATIONAL",
                 "perks": "INFORMATIONAL", "we offer": "INFORMATIONAL", "the team": "INFORMATIONAL"}


def _level_for(text: str, heading: str) -> str:
    lowered = text.lower()
    if any(cue in lowered for cue in PREFERRED_CUES):
        return "PREFERRED"
    if any(cue in lowered for cue in REQUIRED_CUES):
        return "REQUIRED"
    for word, level in SECTION_LEVEL.items():
        if word in heading.lower():
            return level
    return "UNKNOWN"


def _type_for(text: str) -> str:
    lowered = f" {text.lower()} "
    if any(cue in lowered for cue in VISA_CUES):
        return "VISA"
    if any(cue in lowered for cue in EDUCATION_CUES):
        return "EDUCATION"
    if YEARS_RE.search(text):
        return "EXPERIENCE"
    if any(cue in lowered for cue in LEADERSHIP_CUES):
        return "LEADERSHIP"
    if any(cue in lowered for cue in LOCATION_CUES):
        return "LOCATION"
    if SALARY_RE.search(text) and any(word in lowered for word in ("salary", "compensation", "package", "ctc")):
        return "COMPENSATION"
    if any(cue in lowered for cue in COMPANY_CUES):
        return "OTHER"
    if any(cue in lowered for cue in RESPONSIBILITY_CUES):
        return "RESPONSIBILITY"
    return "SKILL"


def _amount(value: str | None, unit: str | None) -> float | None:
    if not value:
        return None
    number = float(value.replace(",", ""))
    unit = (unit or "").lower()
    if unit in ("k",):
        number *= 1_000
    elif unit in ("m",):
        number *= 1_000_000
    elif unit in ("lakh", "lpa"):
        number *= 100_000
    return number


def extract_compensation(text: str) -> dict[str, Any]:
    """Salary the posting itself states. Never an estimate: no match means no compensation."""
    for line in (text or "").splitlines():
        lowered = line.lower()
        if not any(word in lowered for word in ("salary", "compensation", "pay", "package", "ctc", "base")):
            continue
        match = SALARY_RE.search(line)
        if not match:
            continue
        currency = match.group("currency") or ""
        period = next((PERIOD_WORDS[word] for word in PERIOD_WORDS if word in lowered), None)
        return {"min": _amount(match.group("min"), match.group("min_unit")),
                "max": _amount(match.group("max"), match.group("max_unit")),
                "currency": CURRENCY_SYMBOLS.get(currency, currency.upper() or None), "period": period,
                "source_text": line.strip()[:1000], "source": "job_description"}
    return {}


def deterministic_parse(job: Any, text: str) -> dict[str, Any]:
    """Parse with cue words only, no model. Used by the mock provider and as the floor for what must be extracted."""
    structured = StructuredJobDescriptionIn().model_dump()
    structured["job_identity"] = {"title": getattr(job, "title", None), "company": getattr(job, "company", None),
                                 "location": getattr(job, "location", None),
                                 "employment_type": getattr(job, "job_type", None),
                                 "remote_type": getattr(job, "remote_type", None),
                                 "seniority": getattr(job, "seniority", None)}
    requirements: list[dict[str, Any]] = []
    summary_lines: list[str] = []
    for heading, lines in sections_of(text):
        for line in lines:
            for sentence in sentences(line):
                if len(sentence) < 12:
                    continue
                level, kind = _level_for(sentence, heading), _type_for(sentence)
                if kind in ("COMPENSATION", "OTHER"):
                    level = "INFORMATIONAL"  # pay and company blurb are not asks of the candidate
                if not summary_lines and not heading:
                    summary_lines.append(sentence)
                entry = {"type": kind, "normalized_value": sentence[:300], "requirement_level": level,
                         "source_text": sentence[:4000], "source_section": heading or None,
                         "confidence": 0.9 if level in ("REQUIRED", "PREFERRED") else 0.5,
                         "is_explicit": True, "is_derived": False}
                requirements.append(entry)
                target = ("required_skills" if level == "REQUIRED" and kind == "SKILL" else
                          "preferred_skills" if level == "PREFERRED" and kind == "SKILL" else
                          "responsibilities" if kind == "RESPONSIBILITY" else
                          "leadership_expectations" if kind == "LEADERSHIP" else
                          "education_requirements" if kind == "EDUCATION" else
                          "company_context" if kind == "OTHER" else None)
                if target and sentence not in structured[target]:
                    structured[target].append(sentence[:300])
                if level == "REQUIRED" and kind in ("EXPERIENCE", "EDUCATION", "VISA", "LOCATION"):
                    structured["hard_requirements"].append(sentence[:300])
                elif level == "PREFERRED":
                    structured["nice_to_have"].append(sentence[:300])
                if kind == "EXPERIENCE" and (years := YEARS_RE.search(sentence)):
                    minimum, maximum = years.group(1), years.group(2)
                    structured["experience_requirements"]["minimum_years"] = float(minimum)
                    structured["experience_requirements"]["maximum_years"] = float(maximum) if maximum else None
                if kind == "LEADERSHIP" and level == "REQUIRED":
                    structured["experience_requirements"]["leadership_required"] = True
    for key, value in structured.items():
        if isinstance(value, list):
            structured[key] = list(dict.fromkeys(value))[:60]
    structured["role_summary"] = " ".join(summary_lines)[:4000] or None
    structured["compensation"] = {**structured["compensation"], **extract_compensation(text)}
    return {"structured_description": structured, "requirements": requirements}


# ───────────── persistence ─────────────

def _flat(value: Any, prefix: str = "") -> dict[str, Any]:
    """Structured data as {field_path: value}, so changes can be recorded field by field."""
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            out.update(_flat(item, f"{prefix}.{key}" if prefix else key))
    else:
        out[prefix] = value
    return out


def record_change(db: Session, job: Job, path: str, old: Any, new: Any, source: str) -> None:
    db.add(JobStructuredDataChange(user_id=job.user_id, job_id=job.id, field_path=path[:300], old_value=old,
                                   new_value=new, change_source=source, created_at=utcnow()))


def corrected_paths(db: Session, job: Job) -> set[str]:
    """Field paths the user corrected: a parser never overwrites these."""
    rows = db.scalars(select(JobStructuredDataChange.field_path)
                      .where(JobStructuredDataChange.job_id == job.id,
                             JobStructuredDataChange.change_source == "USER")).all()
    return set(rows)


def _key(value: str) -> str:
    return _plain(value)


def store_requirements(db: Session, job: Job, requirements: list[RequirementIn]) -> list[JobRequirementEvidence]:
    """Replace the parser's requirement rows with this parse. Rows the user settled (origin USER) are kept, and a
    parsed row for the same requirement never replaces them."""
    kept = {}
    for row in db.scalars(select(JobRequirementEvidence).where(JobRequirementEvidence.job_id == job.id)).all():
        if row.origin == "USER":
            kept[_key(row.normalized_value)] = row
        else:
            db.delete(row)
    db.flush()
    stored = []
    for index, item in enumerate(requirements, start=1):
        if not item.normalized_value or _key(item.normalized_value) in kept:
            continue
        row = JobRequirementEvidence(
            user_id=job.user_id, job_id=job.id, item_id=item.id or f"req_{index:03d}", requirement_type=item.type,
            normalized_value=item.normalized_value[:300], requirement_level=item.requirement_level,
            source_text=item.source_text or "", source_section=item.source_section,
            confidence=float(item.confidence), is_explicit=bool(item.is_explicit) and not item.is_derived,
            is_derived=bool(item.is_derived), origin="PARSER", created_at=utcnow(), updated_at=utcnow())
        db.add(row)
        stored.append(row)
    return stored


# The editable lists that define what a posting requires, and the level each one means.
REQUIREMENT_LISTS = {"required_skills": "REQUIRED", "preferred_skills": "PREFERRED"}


def _sentence_with(posting: str, value: str) -> str:
    """The posting sentence that names `value`, if there is one: a quote for a requirement the user added."""
    needle = _plain(value)
    for line in (posting or "").splitlines():
        for sentence in sentences(line):
            if needle and needle in _plain(sentence):
                return sentence
    return ""


@dataclass
class Reconciled:
    touched: int = 0
    # Corrections that could not be tied to exactly one requirement row, so no row was changed for them.
    unmatched: list[str] = field(default_factory=list)


def _names(row: JobRequirementEvidence, value: str) -> bool:
    """Whether the row's requirement names `value` as a whole word or phrase ("Python" in "Strong Python skills")."""
    needle = _plain(value)
    return bool(needle) and re.search(rf"(?<![\w+#]){re.escape(needle)}(?![\w+#])", _plain(row.normalized_value)) is not None


def _row_for(rows: list[JobRequirementEvidence], value: str, level: str | None) -> JobRequirementEvidence | None | str:
    """The one row a list value refers to: an exact match, otherwise the only row at `level` naming it.

    Returns "ambiguous" when several rows name the value: changing all of them could demote an unrelated skill
    written in the same sentence ("Python and Go"), so none is changed.
    """
    key = _key(value)
    exact = [row for row in rows if _key(row.normalized_value) == key]
    if exact:
        return exact[0]
    if level is None:
        return None
    named = [row for row in rows if row.requirement_level == level and _names(row, value)]
    if len(named) > 1:
        return "ambiguous"
    return named[0] if named else None


def reconcile_requirements(db: Session, job: Job, old: dict[str, Any], new: dict[str, Any], *,
                           candidates: list[RequirementIn] | None = None) -> Reconciled:
    """Bring the requirement rows in line with a change to the required/preferred lists.

    Only what changed is touched, value by value: a value moved between the lists changes its row's level, a value
    added gets a row (quoted from the posting when the posting says it), and a value removed from both lists stops
    being a requirement (INFORMATIONAL) while its quote is kept. Every touched row becomes origin USER.

    A list value finds its row by exact text or, when the parser wrote a sentence ("Strong Python experience") for
    a short skill ("Python"), by being the only row at its old level that names it. A change that matches no row, or
    several, is reported in `unmatched` rather than guessed.
    """
    posting = normalize_description(job.description or "")
    rows = list(db.scalars(select(JobRequirementEvidence).where(
        JobRequirementEvidence.job_id == job.id, JobRequirementEvidence.is_derived.is_(False))).all())
    quotes = {_key(item.normalized_value): item for item in candidates or []}
    before, after, shown = {}, {}, {}
    for field_name, level in REQUIREMENT_LISTS.items():
        for value in old.get(field_name) or []:
            before.setdefault(_key(value), level)
            shown.setdefault(_key(value), value)
        for value in new.get(field_name) or []:
            after.setdefault(_key(value), level)
            shown[_key(value)] = value
    result = Reconciled()
    for key, level in after.items():
        if before.get(key) == level:
            continue
        # A moved value looks for its row at the level it had; a new one only takes an exact match.
        row = _row_for(rows, shown[key], before.get(key))
        if row == "ambiguous":
            result.unmatched.append(shown[key])
            continue
        if row is None:
            item = quotes.get(key)
            quote = item.source_text if item is not None and quoted_in(posting, item.source_text) else \
                _sentence_with(posting, shown[key])
            row = JobRequirementEvidence(
                user_id=job.user_id, job_id=job.id, item_id=f"usr_{uuid.uuid4().hex[:8]}", requirement_type="SKILL",
                normalized_value=shown[key][:300], requirement_level=level, source_text=quote,
                source_section=item.source_section if item is not None and quote else None, confidence=1.0,
                # The user's own statement: not the employer's words unless the posting says it, and not an inference.
                is_explicit=bool(quote), is_derived=False, origin="USER", created_at=utcnow(), updated_at=utcnow())
            db.add(row)
            rows.append(row)
        else:
            row.requirement_level, row.origin, row.updated_at = level, "USER", utcnow()
        result.touched += 1
    for key, level in before.items():
        if key in after:
            continue
        row = _row_for(rows, shown[key], level)
        if isinstance(row, JobRequirementEvidence) and row.requirement_level in ("REQUIRED", "PREFERRED"):
            row.requirement_level, row.origin, row.updated_at = "INFORMATIONAL", "USER", utcnow()
            result.touched += 1
        else:
            # Removed from the lists, but no single requirement row says it: nothing downstream changed for it.
            result.unmatched.append(shown[key])
    return result


def store_parse(db: Session, job: Job, parsed: ParsedJobDescription, *, source: str = "PARSER",
                version: str = PARSER_VERSION) -> dict[str, Any]:
    """Save a parse, keeping any field the user corrected and recording every change."""
    structured = parsed.structured_description.model_dump()
    previous = job.structured_description or {}
    protected = corrected_paths(db, job) if job.structured_description_status == MANUALLY_CORRECTED else set()
    old_flat, new_flat = _flat(previous), _flat(structured)
    for path in sorted(set(old_flat) | set(new_flat)):
        if path in protected:
            # A value the user corrected: keep theirs, and say so in the history.
            keys = path.split(".")
            target = structured
            for key in keys[:-1]:
                target = target.setdefault(key, {})
            target[keys[-1]] = old_flat.get(path)
            continue
        if old_flat.get(path) != new_flat.get(path):
            record_change(db, job, path, old_flat.get(path), new_flat.get(path), source)
    job.structured_description = structured
    job.structured_description_version = version
    job.structured_description_parsed_at = utcnow()
    job.structured_description_hash = description_hash(job.description or "")
    job.structured_description_status = MANUALLY_CORRECTED if protected else PARSED
    store_requirements(db, job, parsed.requirements)
    if protected & set(REQUIREMENT_LISTS):
        # The user's required/preferred lists survive the parse; the rows must agree with them.
        db.flush()
        reconcile_requirements(db, job, parsed.structured_description.model_dump(), structured,
                               candidates=parsed.requirements)
    return structured


def normalize_job(db: Session, job: Job) -> str:
    """Keep the cleaned posting text beside the raw one. The raw description is never changed."""
    text = normalize_description(job.description or "")
    if (job.normalized_description or "") != text:
        job.normalized_description = text
        if job.structured_description_status == RAW:
            job.structured_description_status = NORMALIZED
    return text


def is_current(job: Job) -> bool:
    """True when the structured description was parsed from the description the job has now.

    A posting whose text changed after it was parsed is stale: workflows use the raw text until it is parsed again.
    """
    return bool(job.structured_description) and \
        job.structured_description_hash == description_hash(job.description or "")


def parse_job_description(db: Session, job: Job, *, user_id: int | None = None) -> ParsedJobDescription:
    """Parse one posting with the account's model. Nothing on the job is written here: the caller stores the
    result against a fresh copy of the row, so a slow model call never writes over newer data."""
    from app.services.llm import LLMCall, LLMClient

    text = normalize_description(job.description or "")
    if not text.strip():
        raise ValueError("This job has no description to parse")
    prompt = build_job_description_parser_prompt(job, text, [heading for heading, _ in sections_of(text) if heading])
    # The full schema is checked inside the model call, not after it: a wrong shape (education_requirements as an
    # object, say) is then recorded as the schema failure it is and gets the gateway's one correction retry, instead
    # of a call logged as successful whose answer the task cannot use.
    raw = LLMClient.for_user(db, user_id or job.user_id).generate_prompt_json(
        prompt, LLMCall(workflow_type="job", entity_id=job.id, validate=checked))
    return with_board_facts(job, ground(validated(raw if isinstance(raw, dict) else {}), text))


def lock_job(db: Session, job_id: int) -> Job | None:
    """The job as the database has it now, locked until the caller commits, so a read-then-write cannot interleave
    with another writer. PostgreSQL locks the row; SQLite takes its single writer lock with a no-op update first,
    as app.services.user_settings.lock_settings_row does (a plain SELECT would hold no lock at all)."""
    if db.get_bind().dialect.name == "sqlite":
        db.execute(update(Job).where(Job.id == job_id).values(id=Job.id).execution_options(synchronize_session=False))
    query = select(Job).where(Job.id == job_id).execution_options(populate_existing=True)
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    return db.scalar(query)


def structured_hash(document: dict[str, Any] | None) -> str:
    """A version tag for the structured description: an edit names the version it started from."""
    return hashlib.sha256(json.dumps(document or {}, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def validated(raw: dict[str, Any]) -> ParsedJobDescription:
    """Parser output as a ParsedJobDescription, trimmed to the schema's list limits so a long posting still parses."""
    from app.services.structured_resume import within_limits

    body = dict(raw)
    body["structured_description"] = within_limits(StructuredJobDescriptionIn, body.get("structured_description") or {})
    body["requirements"] = (body.get("requirements") or [])[:200]
    return ParsedJobDescription.model_validate(body)


def checked(raw: Any) -> ParsedJobDescription:
    """`validated` as the model call's validator: a shape problem is the gateway's schema error, naming each field."""
    from pydantic import ValidationError

    from app.services.llm import LLMSchemaError

    try:
        return validated(raw if isinstance(raw, dict) else {})
    except ValidationError as exc:
        problems = [f"{'.'.join(str(p) for p in error['loc']) or 'output'}: {error['msg']}"
                    for error in exc.errors()[:6]]
        raise LLMSchemaError("JobDescriptionParserOutput", "WRONG_OUTPUT_SCHEMA", problems) from exc


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^[-*•\s]+", "", (text or "").lower())).strip(" .;:")


def quoted_in(posting: str, quote: str) -> bool:
    """Whether `quote` really appears in the posting (ignoring case, spacing, bullets and end punctuation)."""
    needle = _plain(quote)
    return bool(needle) and needle in _plain(posting)


def ground(parsed: ParsedJobDescription, posting: str) -> ParsedJobDescription:
    """Hold the model to the posting. An "explicit" requirement whose source_text is not actually in the posting
    is not an employer statement: it becomes a derived row with no quote and capped confidence. Salary the model
    reports without a sentence from the posting is dropped (the job board may fill it in, marked as such)."""
    for item in parsed.requirements:
        if item.is_explicit and not item.is_derived and not quoted_in(posting, item.source_text):
            item.is_explicit, item.is_derived = False, True
            item.source_text, item.confidence = "", min(item.confidence, 0.5)
        if item.is_derived:
            item.is_explicit = False
    salary = parsed.structured_description.compensation
    if (salary.min is not None or salary.max is not None) and not quoted_in(posting, salary.source_text or ""):
        parsed.structured_description.compensation = CompensationIn()
    elif salary.min is not None or salary.max is not None:
        salary.source = "job_description"
    return parsed


def with_board_facts(job: Job, parsed: ParsedJobDescription) -> ParsedJobDescription:
    """Fill identity and salary the job board reported when the posting text does not state them. Employer salary
    already in the structured data is never replaced (external estimates never reach this field)."""
    identity = parsed.structured_description.job_identity
    for name, value in (("title", job.title), ("company", job.company), ("location", job.location),
                        ("remote_type", job.remote_type), ("employment_type", job.job_type),
                        ("seniority", job.seniority)):
        if not getattr(identity, name) and value:
            setattr(identity, name, value)
    salary = parsed.structured_description.compensation
    if salary.min is None and salary.max is None and (job.salary_min or job.salary_max):
        salary.min, salary.max = job.salary_min, job.salary_max
        salary.currency = salary.currency or job.salary_currency
        salary.period = salary.period or job.salary_interval
        salary.source = "job_board"
        salary.source_text = salary.source_text or f"Salary reported by {job.source_board or job.source or 'the board'}"
    return parsed


# ───────────── the parse queue ─────────────

def needs_parse(job: Job) -> bool:
    """True when the job has a description that has not been parsed in its current wording."""
    if not (job.description or "").strip():
        return False
    if job.structured_description_status in (PARSED, MANUALLY_CORRECTED):
        return job.structured_description_hash != description_hash(job.description or "")
    return True


def queue_parse(db: Session, job: Job, *, reason: str = "new", now: datetime | None = None) -> JobDescriptionParseTask | None:
    """Queue one parse for this job. A job never has two active tasks, and the same text is never queued twice."""
    if not (job.description or "").strip() or job.domain_match in ("PENDING", "REVIEW"):
        return None  # nothing to read, or the domain check (or the person's review) has not let it through yet
    digest = description_hash(job.description or "")
    # A task this session created but has not flushed yet counts too: sessions do not autoflush, so without this
    # the same job could be queued twice in one transaction and break the caller's commit.
    queued = next((task for task in db.new
                   if isinstance(task, JobDescriptionParseTask) and task.job_id == job.id), None)
    if queued is not None:
        return queued
    active = db.scalar(select(JobDescriptionParseTask).where(
        JobDescriptionParseTask.job_id == job.id, JobDescriptionParseTask.status.in_(ACTIVE_JD_PARSE_STATUSES)))
    if active is not None:
        return active
    task = JobDescriptionParseTask(user_id=job.user_id, job_id=job.id, description_hash=digest, status=PENDING,
                                  reason=reason, attempt_count=0, created_at=now or utcnow())
    db.add(task)
    return task


def sweep(db: Session, limit: int | None = None) -> dict[str, int]:
    """Queue parses for postings that have never been parsed, oldest first. Commits.

    A posting whose text changes is queued by whoever changed it (app.services.sources.upsert_job, the re-parse
    endpoint); this sweep only picks up postings that were never parsed at all, including the existing backlog.
    """
    limit = limit or get_settings().jd_parse_enqueue_batch_size
    active = select(JobDescriptionParseTask.job_id).where(JobDescriptionParseTask.status.in_(ACTIVE_JD_PARSE_STATUSES))
    # A stub being recovered from the posting (app.services.jd_recovery) is parsed once the real text arrives.
    recovering = select(JobDescriptionFetchTask.job_id).where(
        JobDescriptionFetchTask.status.in_(ACTIVE_JD_FETCH_STATUSES))
    # A parse a person stopped from Running workers stays stopped until they ask again (Retry, Run now, re-parse).
    stopped = select(JobDescriptionParseTask.job_id).where(JobDescriptionParseTask.status == FAILED,
                                                          JobDescriptionParseTask.last_error == STOPPED_BY_USER)
    backlog = (select(Job)
               .where(Job.description.is_not(None), Job.description != "",
                      Job.structured_description_status.in_((RAW, NORMALIZED)), Job.id.not_in(active),
                      Job.id.not_in(recovering), Job.id.not_in(stopped),
                      or_(Job.domain_match.is_(None), Job.domain_match.not_in(("NOT_MATCHING", "PENDING", "REVIEW")))))
    from app.services import qualification

    if qualification.routes(db):
        # The qualification graph parses the jobs it qualifies, after admitting them: jobs before ranking are its.
        from app.services.lifecycle import WorkflowState as S

        backlog = backlog.where(qualification.untouched(),
                                Job.state.not_in((S.DISCOVERED, S.NORMALIZED, S.FILTERED)))
    candidates = db.scalars(backlog.order_by(Job.id).limit(max(1, limit))).all()
    queued = 0
    for job in candidates:
        if not needs_parse(job):
            continue
        try:  # another worker may queue the same job at the same moment
            with db.begin_nested():
                if queue_parse(db, job) is not None:
                    queued += 1
        except IntegrityError:
            continue
    db.commit()
    return {"queued": queued, "waiting": len(candidates) - queued}


@dataclass(frozen=True)
class ParseClaim:
    task_id: int
    lock_token: str
    worker_id: str
    run_now: bool = False  # asked to run now from Running workers: skips the provider cooldown


def claim(db: Session, *, worker_id: str, limit: int, now: datetime | None = None,
          lease_seconds: int | None = None) -> list[ParseClaim]:
    """Lease up to `limit` due tasks (FOR UPDATE SKIP LOCKED on PostgreSQL). Commits.

    Each claim carries the lease token: a worker whose lease was taken over can no longer write its result.
    """
    now = now or utcnow()
    lease = timedelta(seconds=lease_seconds or get_settings().jd_parse_lease_seconds)
    query = (select(JobDescriptionParseTask)
             .where(JobDescriptionParseTask.status == PENDING,
                    or_(JobDescriptionParseTask.next_attempt_at.is_(None),
                        JobDescriptionParseTask.next_attempt_at <= now))
             .order_by(JobDescriptionParseTask.run_now_at.is_(None),
                       JobDescriptionParseTask.next_attempt_at.asc().nulls_first(), JobDescriptionParseTask.id)
             .limit(max(1, limit)))
    # An account that switched automatic parsing off (Running workers → Settings) keeps its tasks queued; Run now
    # still takes one.
    stopped = worker_settings.paused(db, "parsing")
    if stopped:
        query = query.where(or_(JobDescriptionParseTask.user_id.not_in(stopped),
                                JobDescriptionParseTask.run_now_at.is_not(None)))
    if db.get_bind().dialect.name == "postgresql":  # two workers never receive the same task
        query = query.with_for_update(skip_locked=True, of=JobDescriptionParseTask)
    tasks = db.scalars(query.execution_options(populate_existing=True)).all()
    claimed = []
    for task in tasks:
        task.status, task.locked_by = PROCESSING, worker_id[:200]
        task.lock_token, task.lock_expires_at = uuid.uuid4().hex, now + lease
        task.attempt_count += 1
        task.started_at, task.next_attempt_at = now, None
        run_now, task.run_now_at = task.run_now_at is not None, None
        claimed.append(ParseClaim(task.id, task.lock_token, worker_id[:200], run_now))
    db.commit()
    return claimed


def recover_expired(db: Session, now: datetime | None = None) -> int:
    """Tasks whose worker died go back in the queue."""
    now = now or utcnow()
    rows = db.scalars(select(JobDescriptionParseTask).where(
        JobDescriptionParseTask.status == PROCESSING, JobDescriptionParseTask.lock_expires_at < now)).all()
    message = "Interrupted: the worker stopped while parsing this description"
    failed = []
    for task in rows:
        if task.attempt_count >= worker_settings.get(db, task.user_id, "parsing", "max_attempts"):
            _finish(task, FAILED, now, message, ErrorCategory.LEASE_EXPIRED)
            _mark_unparsed(db.get(Job, task.job_id))
            failed.append(task.job_id)
            continue
        task.status, task.locked_by, task.lock_token, task.lock_expires_at = PENDING, None, None, None
        task.last_error, task.error_category = message, ErrorCategory.LEASE_EXPIRED
    db.commit()
    for job_id in failed:  # given up on: ranking falls back to the raw text, and starts now
        hand_to_ranking(db, job_id)
    return len(rows)


def _finish(task: JobDescriptionParseTask, status: str, now: datetime, error: str | None = None,
            category: ErrorCategory | None = None) -> None:
    task.status, task.completed_at = status, now
    task.locked_by = task.lock_token = task.lock_expires_at = None
    task.last_error, task.error_category = (error[:MAX_ERROR_LENGTH] if error else None), category
    task.next_attempt_at = None


def _held(db: Session, claim: ParseClaim) -> JobDescriptionParseTask | None:
    """The task, only while this worker still holds its lease."""
    return db.scalar(select(JobDescriptionParseTask).where(
        JobDescriptionParseTask.id == claim.task_id, JobDescriptionParseTask.status == PROCESSING,
        JobDescriptionParseTask.lock_token == claim.lock_token).execution_options(populate_existing=True))


def _mark_unparsed(job: Job | None) -> None:
    """A posting that could not be parsed in its current wording: workflows fall back to the raw text."""
    if job is not None and (job.structured_description_status in (RAW, NORMALIZED) or not is_current(job)):
        job.structured_description_status = NEEDS_REVIEW


def _wait(task: JobDescriptionParseTask, message: str, now: datetime) -> str:
    """Hand the task back without using an attempt (e.g. no AI key yet): it resumes once the account is ready."""
    task.status, task.locked_by, task.lock_token, task.lock_expires_at = PENDING, None, None, None
    task.attempt_count = max(0, task.attempt_count - 1)
    task.last_error, task.error_category = message[:MAX_ERROR_LENGTH], ErrorCategory.CONFIGURATION
    task.next_attempt_at = now + timedelta(seconds=get_settings().ranking_wait_seconds)
    return "waiting"


def process(sessions: Any, claim: ParseClaim) -> str:
    """Parse one claimed task in a session bound to its owner. Returns the outcome.

    The model is called without holding any row lock. The result is written against the job as it is afterwards,
    and only while this worker still holds the task's lease.
    """
    from app.services.llm import LLMConfigError, error_type
    from app.services.user_settings import ai_ready

    with sessions() as db:
        task = _held(db, claim)
        if task is None:
            return "lost"
        db.info["user_id"] = task.user_id
        job, now = db.get(Job, task.job_id), utcnow()
        if job is None:
            _finish(task, FAILED, now, "The job was deleted")
            db.commit()
            return "skipped"
        ready, reason = ai_ready(db, task.user_id)
        if not ready:
            outcome = _wait(task, reason, now)
            db.commit()
            return outcome
        job_id, parsed_hash = job.id, description_hash(job.description or "")
        # The wording this attempt reads, not the one the task was queued for: a failure (or a kill) is then judged
        # against the text it really was about, and ranking never queues the same wording again (hold_for_parse).
        if task.description_hash != parsed_hash:
            task.description_hash = parsed_hash
            db.commit()
            job = db.get(Job, job_id)
        try:
            with backoff.bypass(claim.run_now):
                parsed = parse_job_description(db, job, user_id=task.user_id)
        except Exception as exc:  # noqa: BLE001  one bad posting never stops the worker
            db.rollback()
            return _failed(db, claim, exc, now, config=isinstance(exc, LLMConfigError),
                           category=ErrorCategory.VALIDATION if error_type(exc) == "SCHEMA_VALIDATION"
                           else ErrorCategory.EXTERNAL_SERVICE)
        task, job = _held(db, claim), lock_job(db, job_id)
        if task is None:
            db.rollback()
            return "lost"  # the lease expired and another worker took the task over: its result stands
        if job is None:
            _finish(task, FAILED, now, "The job was deleted")
            db.commit()
            return "skipped"
        if description_hash(job.description or "") != parsed_hash:
            # The posting changed while the model was reading it: this parse describes the old text.
            _finish(task, COMPLETED, now, "Superseded: the description changed during parsing")
            db.flush()
            queue_parse(db, job, reason="description_changed")
            db.commit()
            return "superseded"
        normalize_job(db, job)
        store_parse(db, job, parsed)
        _finish(task, COMPLETED, now)
        db.commit()
        hand_to_ranking(db, job_id)
        return "parsed"


def _failed(db: Session, claim: ParseClaim, exc: Exception, now: datetime, *, config: bool,
            category: ErrorCategory) -> str:
    task = _held(db, claim)
    if task is None:
        return "lost"
    handoff_job_id = task.job_id
    message = f"{type(exc).__name__}: {exc}"
    if config:
        outcome = _wait(task, message, now)
    elif task.attempt_count >= (own := worker_settings.for_user(db, task.user_id, "parsing"))["max_attempts"]:
        _finish(task, FAILED, now, message, category)
        _mark_unparsed(db.get(Job, task.job_id))
        outcome = "failed"
    else:
        task.status, task.locked_by, task.lock_token, task.lock_expires_at = PENDING, None, None, None
        task.last_error, task.error_category = message[:MAX_ERROR_LENGTH], category
        task.next_attempt_at = now + timedelta(seconds=own["retry_seconds"] * task.attempt_count)
        outcome = "retrying"
    db.commit()
    if outcome == "failed":  # a failed parse no longer holds ranking back: it falls back to the raw description
        hand_to_ranking(db, handoff_job_id)
    return outcome


def hand_to_ranking(db: Session, job_id: int) -> None:
    """The description is ready for ranking (parsed, or given up on): release a ranking parked waiting for it and wake
    the ranking worker so it starts now. Only a hint on top of the task table; it never changes the parse's outcome."""
    try:
        from app import scheduler_hooks
        from app.services import ranker

        ranker.release_parked(db, job_id)
        scheduler_hooks.nudge("ranking")
        scheduler_hooks.nudge("qualification")  # a graph run waiting for this parse may go on
    except Exception:  # noqa: BLE001
        db.rollback()
        log.warning("could not hand job %s to ranking", job_id, exc_info=True)


def parse_now(db: Session, job: Job) -> ParsedJobDescription:
    """Parse and store one job in the caller's session (manual re-parse, tests)."""
    parsed = parse_job_description(db, job)
    job = lock_job(db, job.id) or job
    normalize_job(db, job)
    store_parse(db, job, parsed)
    return parsed


# ───────────── waiting, for the ranker ─────────────

PARSE_RUNNING = "The job description is still being parsed"
PARSE_NOT_QUEUED = "The job description is waiting to be parsed"
PARSE_AFTER_RECOVERY = "The job description is being recovered before it is parsed"


def _parse_wait(db: Session, job: Job) -> str | None:
    """PARSE_RUNNING, PARSE_NOT_QUEUED (needs a parse that nobody has queued) or None (ranking may go ahead)."""
    if not get_settings().jd_parse_enabled or not needs_parse(job) or job.domain_match in ("NOT_MATCHING", "PENDING", "REVIEW"):
        return None
    queued = next((task for task in db.new
                   if isinstance(task, JobDescriptionParseTask) and task.job_id == job.id), None)
    task = queued or db.scalar(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job.id)
                               .order_by(desc(JobDescriptionParseTask.id)).limit(1))
    if task is not None and task.status in ACTIVE_JD_PARSE_STATUSES:
        # Queued or running, including its final attempt. Every task ends COMPLETED or FAILED (lease recovery fails a
        # task that used its last attempt), so this wait always ends.
        return PARSE_RUNNING
    if task is not None and task.status == FAILED and task.last_error == STOPPED_BY_USER:
        return None  # a person stopped it: never queued again behind their back (Retry, Run now or re-parse do)
    if task is not None and task.status == FAILED and task.description_hash == description_hash(job.description or ""):
        # Its attempts ran out for this very wording (the hash `process` read): ranking falls back to the raw text.
        return None
    if _recovering(db, job):
        # Discovery chose recovery over parsing an unusable stub; recovery queues the parse once the real text arrives.
        return PARSE_AFTER_RECOVERY
    return PARSE_NOT_QUEUED  # never queued, or only for an older wording of the posting


def _recovering(db: Session, job: Job) -> bool:
    if any(isinstance(task, JobDescriptionFetchTask) and task.job_id == job.id
           and task.status in ACTIVE_JD_FETCH_STATUSES for task in db.new):
        return True
    return db.scalar(select(JobDescriptionFetchTask.id).where(
        JobDescriptionFetchTask.job_id == job.id, JobDescriptionFetchTask.status.in_(ACTIVE_JD_FETCH_STATUSES))
        .limit(1)) is not None


def _wake_parser_after_commit(db: Session) -> None:
    """Start the parse worker once the task it should claim is committed (a nudge before then could find nothing).
    Only a hint: a rolled-back transaction leaves at most one spare nudge on the session's next commit."""
    if db.info.get("jd_parsing_nudge"):
        return
    db.info["jd_parsing_nudge"] = True

    def wake(session: Session) -> None:
        session.info.pop("jd_parsing_nudge", None)
        from app import scheduler_hooks

        scheduler_hooks.nudge("jd_parsing")

    event.listen(db, "after_commit", wake, once=True)


def waiting_reason(db: Session, job: Job) -> str | None:
    """Why ranking should wait for this job's description to be parsed, or None to go ahead. Read only.

    Ranking only ever reads a parsed description: it waits while the parse is queued or running, and for a posting
    that needs a parse nobody has queued yet (`hold_for_parse` queues it). A failed parse of the current wording
    never holds a job back: the ranker falls back to the raw description.
    """
    return _parse_wait(db, job)


def hold_for_parse(db: Session, job: Job, *, now: datetime | None = None) -> str | None:
    """`waiting_reason`, queueing the parse ranking is waiting for when there is none. Does not commit.

    Called wherever a ranking is queued (app.services.ranker.enqueue) and again before one runs, so whichever path
    found the job — discovery, a board import, a person, the backlog — ranking starts only after the parse."""
    reason = _parse_wait(db, job)
    if reason == PARSE_NOT_QUEUED and queue_parse(db, job, reason="ranking", now=now) is not None:
        _wake_parser_after_commit(db)  # a ranking someone asked for now must not wait for the parser's next tick
        return PARSE_RUNNING
    return reason


# ───────────── where a posting's parse stands, for lists and filters ─────────────

# What a user sees and filters by. Not a workflow state: a job is "Discovered" and "Parsed" at the same time.
DESCRIPTION_STATES = {"PARSED": "Parsed", "PARSING": "Parsing", "FAILED": "Parse failed", "NOT_PARSED": "Not parsed",
                      "NO_DESCRIPTION": "No description"}


def latest_parse_statuses(db: Session, job_ids: list[int] | None = None) -> dict[int, str]:
    """Each job's most recent parse task status, in one query (for the whole account when job_ids is None)."""
    latest = select(JobDescriptionParseTask.job_id, func.max(JobDescriptionParseTask.id).label("id"))
    if job_ids is not None:
        if not job_ids:
            return {}
        latest = latest.where(JobDescriptionParseTask.job_id.in_(job_ids))
    latest = latest.group_by(JobDescriptionParseTask.job_id).subquery()
    return dict(db.execute(select(JobDescriptionParseTask.job_id, JobDescriptionParseTask.status)
                           .join(latest, JobDescriptionParseTask.id == latest.c.id)).all())


def description_state(job: Job, latest_task_status: str | None) -> str:
    """One of DESCRIPTION_STATES. A parse of older wording does not count: the text has changed since."""
    if not (job.description or "").strip():
        return "NO_DESCRIPTION"
    if latest_task_status in ACTIVE_JD_PARSE_STATUSES:
        return "PARSING"
    if job.structured_description_status in (PARSED, MANUALLY_CORRECTED) and is_current(job):
        return "PARSED"
    if job.structured_description_status == NEEDS_REVIEW:
        return "FAILED"  # its attempts ran out; ranking and tailoring use the raw text (_mark_unparsed)
    return "NOT_PARSED"


# ───────────── manual correction and re-parse ─────────────

class StaleCorrection(RuntimeError):
    """The structured description changed (a parse landed) after the edit was started from it."""


def apply_correction(db: Session, job: Job, payload: StructuredJobDescriptionIn,
                     base_hash: str | None = None) -> Reconciled:
    """Save the user's corrections to the structured description. Corrected paths are protected from parsers.
    Returns what reached the requirement rows, including corrections that could not be tied to one row.

    The row is locked and re-read first. When `base_hash` names an older version than the one stored (a parse
    landed while the user was editing), nothing is written: the edit is a whole document, so saving it would revert
    the parse and record every reverted value as a correction, protecting stale values from all later parses.
    """
    job = lock_job(db, job.id) or job
    if base_hash is not None and base_hash != structured_hash(job.structured_description):
        raise StaleCorrection("This job description was parsed again while you were editing. Your edits were not "
                              "saved: review the new values and correct them again.")
    old = job.structured_description or StructuredJobDescriptionIn().model_dump()
    new = payload.model_dump()
    old_flat, new_flat = _flat(old), _flat(new)
    changed = [path for path in sorted(set(old_flat) | set(new_flat)) if old_flat.get(path) != new_flat.get(path)]
    for path in changed:
        record_change(db, job, path, old_flat.get(path), new_flat.get(path), "USER")
    reconciled = Reconciled()
    if changed:
        job.structured_description = new
        job.structured_description_status = MANUALLY_CORRECTED
        # Every workflow reads the requirement rows, not this document: a correction has to reach them too.
        reconciled = reconcile_requirements(db, job, old, new)
    return reconciled


def reparse(db: Session, job: Job) -> dict[str, Any]:
    """Parse the posting again.

    A job the user corrected keeps its values: the new parse is stored as a candidate with a field-by-field
    comparison to resolve. Anything else is simply re-parsed.
    """
    parsed = parse_job_description(db, job)
    job = lock_job(db, job.id) or job  # the row as it is after the model call, not as it was before
    normalize_job(db, job)
    if job.structured_description_status != MANUALLY_CORRECTED or not job.structured_description:
        store_parse(db, job, parsed)
        job.structured_description_candidate = None
        return {"applied": True, "structured_description": job.structured_description, "changes": []}
    current, proposed = _flat(job.structured_description), _flat(parsed.structured_description.model_dump())
    protected = corrected_paths(db, job)
    changes = [{"id": f"chg_{index}", "path": path, "current": current.get(path), "proposed": proposed.get(path),
                "corrected": path in protected}
               for index, path in enumerate(sorted(set(current) | set(proposed)), start=1)
               if current.get(path) != proposed.get(path)]
    job.structured_description_candidate = {
        "version": PARSER_VERSION, "created_at": utcnow().isoformat(), "changes": changes,
        "description_hash": description_hash(job.description or ""),
        "structured_description": parsed.structured_description.model_dump(),
        "requirements": [item.model_dump() for item in parsed.requirements]}
    return {"applied": False, "structured_description": job.structured_description, "changes": changes}


def resolve_reparse(db: Session, job: Job, decisions: dict[str, str]) -> dict[str, Any]:
    """Apply the user's choice for each difference a re-parse found. "use_new" takes the parser's value."""
    job = lock_job(db, job.id) or job
    candidate = job.structured_description_candidate
    if not candidate:
        raise ValueError("No re-parse is waiting for this job")
    structured = copy.deepcopy(job.structured_description or {})  # a new object: JSON columns save by identity
    accepted = 0
    for change in candidate["changes"]:
        if decisions.get(change["id"]) != "use_new":
            continue
        keys = change["path"].split(".")
        target = structured
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        target[keys[-1]] = change["proposed"]
        record_change(db, job, change["path"], change["current"], change["proposed"], "PARSER")
        accepted += 1
    previous = job.structured_description or {}
    job.structured_description = structured
    job.structured_description_parsed_at = utcnow()
    # The comparison was parsed from the posting as it was then; if the text has changed since, it stays stale.
    job.structured_description_hash = candidate.get("description_hash") or job.structured_description_hash
    # Requirement rows follow the individual decisions: only values the user accepted into the required or
    # preferred lists change. Kept values, and parser rows that never appeared as a comparison, stay as they were.
    reconciled = reconcile_requirements(db, job, previous, structured,
                                        candidates=[RequirementIn.model_validate(item)
                                                    for item in candidate["requirements"]])
    job.structured_description_candidate = None
    return {"accepted": accepted, "structured_description": structured, "unmatched": reconciled.unmatched}


# ───────────── monitoring ─────────────

def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


def metrics(db: Session, user_id: int | None, start: datetime, end: datetime) -> dict[str, Any]:
    """JD parsing for Monitoring: the parse tasks created in the window, the queue right now, the parser's model calls
    (LLM stage JD_PARSER) and the scheduler job. One account, or every account for the admin."""
    from app.models import LLMRequestLog, SchedulerJob
    from app.services.redaction import redact_text

    def owned(model: Any, query: Any) -> Any:
        return query.where(model.user_id == user_id) if user_id is not None else query

    T = JobDescriptionParseTask
    tasks = db.execute(owned(T, select(T.status, T.attempt_count, T.error_category, T.last_error, T.started_at,
                                       T.completed_at).where(T.created_at >= start, T.created_at <= end))).all()
    done = [t for t in tasks if t.status == COMPLETED]
    failed = [t for t in tasks if t.status == FAILED]
    durations = [(t.completed_at - t.started_at).total_seconds() * 1000 for t in done
                 if t.started_at and t.completed_at and t.completed_at >= t.started_at]
    errors: dict[str, int] = {}
    for t in failed:
        key = (redact_text((t.last_error or "unknown").split("\n")[0], 160) or "unknown").strip()
        errors[key] = errors.get(key, 0) + 1
    queue = dict(db.execute(owned(T, select(T.status, func.count()).where(T.status.in_(ACTIVE_JD_PARSE_STATUSES))
                                  .group_by(T.status))).all())
    oldest = db.scalar(owned(T, select(func.min(T.created_at)).where(T.status == PENDING)))
    parsed = db.scalar(owned(Job, select(func.count(Job.id)).where(Job.structured_description_parsed_at >= start,
                                                                     Job.structured_description_parsed_at <= end))) or 0
    calls = db.execute(owned(LLMRequestLog, select(
        LLMRequestLog.status, LLMRequestLog.error_type, LLMRequestLog.model, LLMRequestLog.input_tokens,
        LLMRequestLog.output_tokens, LLMRequestLog.latency_ms, LLMRequestLog.estimated_cost)
        .where(LLMRequestLog.stage == "JD_PARSER", LLMRequestLog.created_at >= start,
               LLMRequestLog.created_at <= end))).all()
    latencies = [c.latency_ms for c in calls if c.latency_ms is not None]
    costs = [c.estimated_cost for c in calls if c.estimated_cost is not None]
    by_model: dict[str, int] = {}
    llm_errors: dict[str, int] = {}
    for c in calls:
        by_model[c.model or "unknown"] = by_model.get(c.model or "unknown", 0) + 1
        if c.status == "FAILED":
            llm_errors[c.error_type or "OTHER"] = llm_errors.get(c.error_type or "OTHER", 0) + 1
    job_row = db.get(SchedulerJob, "jd_parsing")
    return {
        "tasks_created": len(tasks), "completed": len(done), "failed": len(failed),
        "retrying": sum(1 for t in tasks if t.status == PENDING and (t.attempt_count or 0) > 0),
        "success_rate": round(len(done) / (len(done) + len(failed)), 3) if done or failed else None,
        "average_duration_ms": round(sum(durations) / len(durations)) if durations else None,
        "jobs_parsed": parsed,
        "failures_by_category": dict(sorted(_count(str(t.error_category.value) if t.error_category else "UNKNOWN"
                                                   for t in failed).items(), key=lambda kv: -kv[1])),
        "top_errors": [{"error": e, "tasks": n} for e, n in sorted(errors.items(), key=lambda kv: -kv[1])[:5]],
        "queue": {"pending": queue.get(PENDING, 0), "processing": queue.get(PROCESSING, 0),
                  "oldest_pending_at": oldest.isoformat() if oldest else None},
        "llm": {"requests": len(calls), "failed": sum(1 for c in calls if c.status == "FAILED"),
                "input_tokens": sum(c.input_tokens or 0 for c in calls),
                "output_tokens": sum(c.output_tokens or 0 for c in calls),
                # Unknown cost stays unknown: None unless at least one call reported or priced one.
                "estimated_cost": float(sum(costs)) if costs else None,
                "average_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
                "models": dict(sorted(by_model.items(), key=lambda kv: -kv[1])), "errors": llm_errors},
        "scheduler": None if job_row is None else {
            "enabled": get_settings().jd_parse_enabled, "last_status": job_row.last_status,
            "last_finished_at": job_row.last_finished_at.isoformat() if job_row.last_finished_at else None,
            "next_run_at": job_row.next_run_at.isoformat() if job_row.next_run_at else None,
            "last_error": redact_text((job_row.last_error or "").split("\n")[0], 300) or None,
            "last_result": job_row.last_result},
    }
