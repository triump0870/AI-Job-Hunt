"""Structured resume: the canonical machine-readable form of one resume version.

    uploaded file → extracted text → clean_raw_text → parse (deterministic, or the LLM parser prompt)
    → materialize: stable item ids + per-item verification → Resume.structured_data

The raw text is the read-only reference and is never rewritten by edits. Every verifiable unit (personal
information, the summary, each skill, each role, each bullet, each project, degree, certification, award,
language and additional line) carries a stable `id` and `verification: {status, updated_at}`. Units start
UNVERIFIED; only app.services.resume_verification changes their status.

Parsing adds no facts of its own: dates are normalized to YYYY-MM only when month and year are both present (the
original text is kept in raw_*), skills come only from the resume's own skills section, and a role's
technologies are only those skills its own lines mention.
"""
from __future__ import annotations

import hashlib
import re
import types
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from app.services.resume_format import BULLET_RE, parse_resume, to_plain

PARSER_VERSION = "d2"  # d2: a bracket that belongs to an employer ("Zendrive (India)") is kept
ITEM_STATUSES = ("UNVERIFIED", "VERIFIED", "REJECTED", "EDITED")
REVIEWED = ("VERIFIED", "REJECTED")
SECTIONS = ("personal_information", "summary", "skills", "experience", "projects", "education", "certifications",
            "awards", "languages", "additional_information")
# Sections that must be fully reviewed (every unit VERIFIED or REJECTED) before the whole resume can be verified.
REQUIRED_SECTIONS = ("personal_information", "summary", "skills", "experience", "education")
LIST_SECTIONS = ("skills", "experience", "projects", "education", "certifications", "awards", "languages",
                 "additional_information")
SECTION_LABELS = {"personal_information": "Personal information", "summary": "Professional summary",
                  "skills": "Skills", "experience": "Work experience", "projects": "Projects",
                  "education": "Education", "certifications": "Certifications", "awards": "Awards",
                  "languages": "Languages", "additional_information": "Additional information"}


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ───────────── input models: what an editor or a parser may supply ─────────────
# Verification fields are never read from input: they are ignored here and kept server-side.

Short = Field(default="", max_length=300)


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def _null_is_default(cls, value: Any, info: ValidationInfo) -> Any:
        """The parser prompt asks for null when a value is missing; a field that cannot hold None takes its
        default ("" / [] / False) instead of failing the whole parse."""
        if value is not None or info.field_name is None:
            return value
        field = cls.model_fields[info.field_name]
        optional = get_origin(field.annotation) in (Union, types.UnionType) and type(None) in get_args(field.annotation)
        return value if optional else field.get_default(call_default_factory=True)


class MetricIn(_In):
    value: str | None = Field(default=None, max_length=60)
    unit: str | None = Field(default=None, max_length=30)
    description: str | None = Field(default=None, max_length=500)
    source_text: str | None = Field(default=None, max_length=2000)


class BulletIn(_In):
    id: str | None = Field(default=None, max_length=120)
    text: str = Field(max_length=2000)


class PersonalInformationIn(_In):
    full_name: str = Short
    headline: str = Field(default="", max_length=500)
    location: str = Short
    email: str = Short
    phone: str = Short
    linkedin: str = Short
    github: str = Short
    portfolio: str = Short
    other_links: list[str] = Field(default_factory=list, max_length=20)


class SummaryIn(_In):
    text: str = Field(default="", max_length=5000)


class SkillIn(_In):
    id: str | None = Field(default=None, max_length=120)
    name: str = Field(max_length=200)
    category: str = Field(default="", max_length=120)
    # How the resume writes it when `name` is a normalized form ("Kafka" for "Apache Kafka"); kept only when grounding
    # found it in the text (resume extraction V2). Absent otherwise.
    raw: str | None = Field(default=None, max_length=200)


class ExperienceIn(_In):
    id: str | None = Field(default=None, max_length=120)
    company: str = Short
    job_title: str = Short
    location: str = Short
    employment_type: str = Field(default="", max_length=60)
    start_date: str | None = Field(default=None, max_length=10)
    end_date: str | None = Field(default=None, max_length=10)
    raw_start_date: str | None = Field(default=None, max_length=60)
    raw_end_date: str | None = Field(default=None, max_length=60)
    is_current: bool = False
    responsibilities: list[BulletIn | str] = Field(default_factory=list, max_length=60)
    achievements: list[BulletIn | str] = Field(default_factory=list, max_length=60)
    technologies: list[str] = Field(default_factory=list, max_length=80)
    skills: list[str] = Field(default_factory=list, max_length=80)
    raw_bullets: list[str] = Field(default_factory=list, max_length=120)
    # Which role of the parsed raw text this entry came from; lets tailoring map it back to the rendered resume.
    source_role_index: int | None = Field(default=None, ge=0)


class ProjectIn(_In):
    id: str | None = Field(default=None, max_length=120)
    name: str = Short
    description: str = Field(default="", max_length=3000)
    url: str = Short
    technologies: list[str] = Field(default_factory=list, max_length=80)
    skills: list[str] = Field(default_factory=list, max_length=80)
    achievements: list[BulletIn | str] = Field(default_factory=list, max_length=40)


class EducationIn(_In):
    id: str | None = Field(default=None, max_length=120)
    degree: str = Short
    field_of_study: str = Short
    institution: str = Short
    # The degree-awarding university, when it differs from the institution (an affiliated college, say).
    university: str = Short
    location: str = Short
    start_date: str | None = Field(default=None, max_length=10)
    end_date: str | None = Field(default=None, max_length=10)
    raw_start_date: str | None = Field(default=None, max_length=60)
    raw_end_date: str | None = Field(default=None, max_length=60)
    # As written on the resume: "8.2/10 CGPA", "First Class", "3.7 GPA".
    grade: str = Field(default="", max_length=60)


class CertificationIn(_In):
    id: str | None = Field(default=None, max_length=120)
    name: str = Short
    issuer: str = Short
    issue_date: str | None = Field(default=None, max_length=60)
    expiration_date: str | None = Field(default=None, max_length=60)
    credential_id: str = Short
    url: str = Short


class AwardIn(_In):
    id: str | None = Field(default=None, max_length=120)
    title: str = Short
    issuer: str = Short
    date: str | None = Field(default=None, max_length=60)
    description: str = Field(default="", max_length=2000)


class LanguageIn(_In):
    id: str | None = Field(default=None, max_length=120)
    name: str = Field(default="", max_length=100)
    proficiency: str = Field(default="", max_length=100)


class AdditionalIn(_In):
    id: str | None = Field(default=None, max_length=120)
    text: str = Field(default="", max_length=3000)


class StructuredResumeIn(_In):
    personal_information: PersonalInformationIn = Field(default_factory=PersonalInformationIn)
    summary: SummaryIn | str = Field(default_factory=SummaryIn)
    skills: list[SkillIn] = Field(default_factory=list, max_length=300)
    experience: list[ExperienceIn] = Field(default_factory=list, max_length=60)
    projects: list[ProjectIn] = Field(default_factory=list, max_length=60)
    education: list[EducationIn] = Field(default_factory=list, max_length=30)
    certifications: list[CertificationIn] = Field(default_factory=list, max_length=60)
    awards: list[AwardIn] = Field(default_factory=list, max_length=60)
    languages: list[LanguageIn] = Field(default_factory=list, max_length=30)
    additional_information: list[AdditionalIn | str] = Field(default_factory=list, max_length=60)


# ───────────── raw text ─────────────

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"}


def clean_raw_text(text: str) -> str:
    """Remove extraction noise (control characters, ligatures, runs of spaces, blank-line runs). Wording,
    numbers, names, dates and URLs are untouched."""
    out = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    for ligature, plain in _LIGATURES.items():
        out = out.replace(ligature, plain)
    out = _CONTROL.sub("", out)
    lines = [re.sub(r" {2,}", " ", line).rstrip() for line in out.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ───────────── dates, metrics, ids ─────────────

MONTHS = {m: i for i, names in enumerate(
    [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
     ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
     ("dec", "december")], start=1) for m in names}
CURRENT_WORDS = ("present", "current", "now", "today", "till date", "to date")
_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_DATE = rf"(?:{_MONTH}\s+\d{{4}}|\d{{1,2}}/\d{{4}}|\d{{4}}-\d{{2}}|\d{{4}})"
DATE_RANGE_RE = re.compile(rf"(?P<start>{_DATE})\s*(?:-|–|—|to|until)\s*(?P<end>{_DATE}|present|current|now|today)",
                           re.IGNORECASE)
SINGLE_DATE_RE = re.compile(_DATE, re.IGNORECASE)
METRIC_RE = re.compile(
    r"(?<![\w.])(?P<value>[$€£₹]\s?\d[\d,]*(?:\.\d+)?\s?(?:[kKmMbB]|million|billion)?"
    r"|\d[\d,]*(?:\.\d+)?\s?(?:%|x\b|X\b|\+|k\b|K\b|M\b|B\b|ms\b|million\b|billion\b|hours?\b|days?\b|weeks?\b|"
    r"users\b|customers\b|engineers\b|people\b|requests\b|services\b|countries\b|teams?\b|members\b))")
ACHIEVEMENT_VERBS = ("reduced", "increased", "improved", "cut ", "grew", "saved", "accelerated", "achieved", "boosted",
                     "doubled", "tripled", "delivered", "launched", "led ", "scaled", "won ", "eliminated", "decreased",
                     "raised", "lowered", "shipped", "drove", "generated", "hired", "grown")


def normalize_date(raw: str | None) -> tuple[str | None, bool]:
    """(YYYY-MM or None, is_current). A year without a month stays None: months are never guessed."""
    text = (raw or "").strip().lower().rstrip(".")
    if not text:
        return None, False
    if any(text == word or text.startswith(word) for word in CURRENT_WORDS):
        return None, True
    if match := re.fullmatch(r"(\d{4})-(\d{2})", text):
        return (f"{match.group(1)}-{match.group(2)}" if 1 <= int(match.group(2)) <= 12 else None), False
    if match := re.fullmatch(r"(\d{1,2})/(\d{4})", text):
        month = int(match.group(1))
        return (f"{match.group(2)}-{month:02d}" if 1 <= month <= 12 else None), False
    if match := re.fullmatch(r"([a-z]+)\.?\s+(\d{4})", text):
        month = MONTHS.get(match.group(1))
        return (f"{match.group(2)}-{month:02d}" if month else None), False
    return None, False


def extract_metrics(text: str) -> list[dict[str, Any]]:
    out = []
    for match in METRIC_RE.finditer(text or ""):
        value = match.group("value").strip()
        unit = ("%" if value.endswith("%") else "x" if value[-1:] in "xX" else "currency" if value[:1] in "$€£₹"
                else re.sub(r"[\d,.\s$€£₹]", "", value) or None)
        number = re.search(r"\d[\d,]*(?:\.\d+)?", value)
        out.append({"value": number.group(0) if number else value, "unit": unit, "description": None,
                    "source_text": text})
    return out


def slug(value: str, limit: int = 32) -> str:
    return re.sub(r"[^a-z0-9]+", "_", to_plain(value or "").lower()).strip("_")[:limit].strip("_")


def short_hash(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(p or "" for p in parts).encode("utf-8")).hexdigest()[:8]


def _year(value: str | None) -> str:
    match = re.search(r"\d{4}", value or "")
    return match.group(0) if match else "na"


# ───────────── deterministic parser ─────────────

HEADING_RE = re.compile(
    r"^(?:#+\s*)?(professional summary|summary|profile|about me|about|objective|technical skills|core competencies|"
    r"skills|professional experience|work experience|employment history|experience|employment|work history|"
    r"projects|personal projects|education|certifications?|licenses? (?:and|&) certifications?|awards?(?: (?:and|&) honou?rs)?|"
    r"honou?rs|languages|additional information|publications|interests|volunteering|volunteer experience)\s*:?\s*$",
    re.IGNORECASE)
EXTRA_KINDS = (("certifications", ("certif", "licen")), ("awards", ("award", "honor", "honour")),
               ("languages", ("language",)), ("additional_information", ("additional", "publication", "interest",
                                                                           "volunteer")))
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
URL_RE = re.compile(r"(?:https?://)?(?:www\.)?[\w-]+\.[a-z]{2,}(?:/[^\s|,]*)?", re.IGNORECASE)


def to_dialect(text: str) -> str:
    """Make plain-text resumes readable by resume_format.parse_resume: known headings become `## `, and lines
    with a date range inside an experience section become `### ` role headers. Only used for parsing."""
    out, kind = [], ""
    for raw in to_plain(text).splitlines():
        line = raw.strip()
        heading = HEADING_RE.match(line)
        if heading and not line.startswith("###"):
            title = heading.group(1).strip()
            kind = "experience" if re.search(r"experience|employment|work history", title, re.IGNORECASE) \
                and not re.search(r"volunteer", title, re.IGNORECASE) else title.lower()
            out.append(f"## {title[:1].upper()}{title[1:]}")
            continue
        if line.startswith("## "):
            kind = "experience" if re.search(r"experience|employment|work history|career", line, re.IGNORECASE) else line.lower()
        if (kind == "experience" and line and not line.startswith("#") and not BULLET_RE.match(line)
                and DATE_RANGE_RE.search(line) and len(line) < 200):
            out.append(f"### {line}")
            continue
        out.append(raw)
    return "\n".join(out)


def _section_kind(title: str, kind: str) -> str:
    if kind != "other":
        return kind
    lowered = title.lower()
    return next((name for name, words in EXTRA_KINDS if any(w in lowered for w in words)), "other")


def _trim_part(part: str) -> str:
    """A heading part without separators and the brackets a removed date left behind ("(Jan 2020 - 2022)"). A
    bracket that belongs to the value ("Zendrive (India)") stays."""
    part = re.sub(r"\(\s*\)", "", part).strip(" ,-–—")
    while part.startswith("(") and part.count("(") > part.count(")"):
        part = part[1:].strip(" ,-–—")
    while part.endswith(")") and part.count(")") > part.count("("):
        part = part[:-1].strip(" ,-–—")
    return part


def _role_fields(header: str) -> dict[str, Any]:
    dates = DATE_RANGE_RE.search(header)
    raw_start = dates.group("start") if dates else None
    raw_end = dates.group("end") if dates else None
    if not dates and (single := SINGLE_DATE_RE.search(header)):
        raw_start = single.group(0)
    rest = header
    if dates:
        rest = header[:dates.start()] + header[dates.end():]
    elif raw_start:
        rest = header.replace(raw_start, "")
    parts = [_trim_part(p) for p in rest.split("|")]
    parts = [p for p in parts if p]
    if len(parts) == 1:
        single_part = parts[0]
        if " at " in single_part:
            parts = [s.strip() for s in single_part.split(" at ", 1)]
        else:
            parts = [s.strip() for s in re.split(r"\s+[-–—]\s+|,\s+", single_part) if s.strip()]
    start, _ = normalize_date(raw_start)
    end, current = normalize_date(raw_end)
    return {"job_title": parts[0] if parts else "", "company": parts[1] if len(parts) > 1 else "",
            "location": ", ".join(parts[2:]) if len(parts) > 2 else "", "start_date": start, "end_date": end,
            "raw_start_date": raw_start, "raw_end_date": raw_end, "is_current": current}


def _is_achievement(text: str) -> bool:
    lowered = f"{text.lower()} "
    return bool(METRIC_RE.search(text)) or lowered.startswith(ACHIEVEMENT_VERBS)


def _mentions(text: str, names: list[str]) -> list[str]:
    lowered = f" {to_plain(text).lower()} "
    found = []
    for name in names:
        clean = name.lower().strip()
        if clean and re.search(rf"(?<![\w+#]){re.escape(clean)}(?![\w+#])", lowered) and name not in found:
            found.append(name)
    return found


def _split_list(value: str) -> list[str]:
    return [item.strip(" .;") for item in re.split(r",|;|•|·|\|", value) if item.strip(" .;")]


def _personal(parsed_name: str, header_lines: list[str]) -> dict[str, Any]:
    info = PersonalInformationIn().model_dump()
    text = " | ".join(header_lines)
    lines = list(header_lines)
    name = parsed_name
    if not name and lines and not re.search(r"[@\d|]", lines[0]) and len(lines[0].split()) <= 5:
        name = lines.pop(0)
    info["full_name"] = name
    if match := EMAIL_RE.search(text):
        info["email"] = match.group(0)
    if match := PHONE_RE.search(EMAIL_RE.sub("", text)):
        info["phone"] = match.group(0).strip()
    links = [u for u in URL_RE.findall(EMAIL_RE.sub("", text)) if "." in u and not re.fullmatch(r"[\d.]+", u)]
    for link in links:
        lowered = link.lower()
        if "linkedin." in lowered and not info["linkedin"]:
            info["linkedin"] = link
        elif "github." in lowered and not info["github"]:
            info["github"] = link
        elif link not in info["other_links"]:
            info["other_links"].append(link)
    for line in lines:
        if not re.search(r"[@|]|\d{3}|https?://|www\.|linkedin|github", line, re.IGNORECASE) and not info["headline"]:
            info["headline"] = line
            continue
        for segment in line.split("|"):
            segment = segment.strip()
            if (segment and not info["location"] and not EMAIL_RE.search(segment) and not PHONE_RE.search(segment)
                    and not URL_RE.fullmatch(segment) and not re.search(r"linkedin|github|https?://", segment, re.IGNORECASE)
                    and len(segment) <= 60):
                info["location"] = segment
    return info


def parse_deterministic(raw_text: str, *, marked: bool = False) -> dict[str, Any]:
    """Parse resume text into StructuredResumeIn-shaped data without an LLM. Nothing is inferred.

    `marked`: the text is a parse view (app.services.resume_extraction.structure) whose sections and roles are already
    marked, so headings are not guessed again — a line a person said is not a heading stays an ordinary line."""
    parsed = parse_resume(raw_text if marked else to_dialect(raw_text))
    data: dict[str, Any] = {"personal_information": _personal(parsed.name, parsed.header_lines), "summary": "",
                            "skills": [], "experience": [], "projects": [], "education": [], "certifications": [],
                            "awards": [], "languages": [], "additional_information": []}
    skill_names: list[str] = []
    for section in parsed.sections:
        if _section_kind(section.title, section.kind) != "skills":
            continue
        for line in section.lines:
            body = line.lstrip("-# ").strip()
            category, _, values = body.partition(":") if ":" in body else ("", "", body)
            for name in _split_list(values):
                if len(name) <= 60 and name.lower() not in {n.lower() for n in skill_names}:
                    skill_names.append(name)
                    data["skills"].append({"name": name, "category": category.strip()})
    for section in parsed.sections:
        kind = _section_kind(section.title, section.kind)
        items = [line.lstrip("- ").strip() for line in section.lines if line.strip()]
        if kind == "summary":
            data["summary"] = " ".join(items)
        elif kind == "experience":
            for index, role in enumerate(section.roles):
                fields = _role_fields(role.header)
                bullets = [b.strip() for b in role.bullets if b.strip()]
                achievements = [b for b in bullets if _is_achievement(b)]
                data["experience"].append({
                    **fields, "source_role_index": index, "raw_bullets": bullets,
                    "responsibilities": [b for b in bullets if b not in achievements], "achievements": achievements,
                    "technologies": _mentions(" ".join([role.header, *bullets]), skill_names), "skills": []})
        elif kind == "projects":
            current: dict[str, Any] | None = None
            for line in section.lines:
                is_bullet = line.startswith("- ")
                body = line.lstrip("#- ").strip()
                if not body:
                    continue
                if not is_bullet or current is None:
                    name, _, description = body.partition(":") if not is_bullet else (body, "", "")
                    current = {"name": name.strip(), "description": description.strip(), "achievements": [],
                               "technologies": _mentions(body, skill_names)}
                    data["projects"].append(current)
                else:
                    current["achievements"].append(body)
                    current["technologies"] = list(dict.fromkeys(current["technologies"] + _mentions(body, skill_names)))
        elif kind == "education":
            for body in items:
                dates = DATE_RANGE_RE.search(body)
                single = None if dates else SINGLE_DATE_RE.search(body)
                raw_start = dates.group("start") if dates else None
                raw_end = dates.group("end") if dates else (single.group(0) if single else None)
                rest = body.replace(dates.group(0), "") if dates else body.replace(raw_end, "") if raw_end else body
                parts = [p.strip(" ,-–—()") for p in re.split(r"\||,|\s+[-–—]\s+", rest) if p.strip(" ,-–—()")]
                data["education"].append({
                    "degree": parts[0] if parts else "", "institution": parts[1] if len(parts) > 1 else "",
                    "location": ", ".join(parts[2:]), "raw_start_date": raw_start, "raw_end_date": raw_end,
                    "start_date": normalize_date(raw_start)[0], "end_date": normalize_date(raw_end)[0]})
        elif kind == "certifications":
            for body in items:
                parts = [p.strip() for p in re.split(r"\||\s+[-–—]\s+|,\s+", body) if p.strip()]
                issued = SINGLE_DATE_RE.search(body)
                parts = [p for p in parts if not (issued and p == issued.group(0))]
                data["certifications"].append({"name": parts[0] if parts else body,
                                               "issuer": parts[1] if len(parts) > 1 else "",
                                               "issue_date": issued.group(0) if issued else None})
        elif kind == "awards":
            data["awards"] += [{"title": body} for body in items]
        elif kind == "languages":
            for body in items:
                for part in _split_list(body):
                    name, _, level = re.sub(r"\s*[(:–—-]\s*", "\x00", part, count=1).partition("\x00")
                    data["languages"].append({"name": name.strip(), "proficiency": level.strip(" )")})
        elif kind in ("additional_information", "other"):
            data["additional_information"] += items
    return fit_parser_output(data)


# ───────────── materialize: ids + verification ─────────────

def _unverified() -> dict[str, Any]:
    return {"status": "UNVERIFIED", "updated_at": None}


class _Ids:
    def __init__(self, previous: dict[str, Any] | None):
        self.known = {unit.item_id for unit in iter_units(previous)} if previous else set()
        self.used: set[str] = set()

    def take(self, supplied: str | None, generated: str) -> str:
        """Keep an id the resume already had; otherwise use a generated one, made unique."""
        if supplied and supplied in self.known and supplied not in self.used:
            self.used.add(supplied)
            return supplied
        candidate, n = generated, 2
        while candidate in self.used or (candidate in self.known and candidate != supplied):
            candidate, n = f"{generated}_{n}", n + 1
        self.used.add(candidate)
        return candidate


def _bullets(values: list[Any], ids: _Ids, prefix: str, parent: str) -> list[dict[str, Any]]:
    out = []
    for value in values:
        bullet = value if isinstance(value, BulletIn) else BulletIn(text=str(value))
        if not bullet.text:
            continue
        out.append({"id": ids.take(bullet.id, f"{prefix}_{short_hash(parent, bullet.text)}"), "text": bullet.text,
                    "metrics": extract_metrics(bullet.text), "verification": _unverified()})
    return out


def within_limits(model: type[BaseModel], value: Any) -> Any:
    """Parser output trimmed to the input model's list limits, so an unusually long resume still parses."""
    if not isinstance(value, dict):
        return value
    out = dict(value)
    for name, field in model.model_fields.items():
        item = out.get(name)
        limit = next((m.max_length for m in field.metadata if getattr(m, "max_length", None)), None)
        if isinstance(item, list):
            item = item[:limit] if limit else item
            inner = next((a for a in getattr(field.annotation, "__args__", ()) if isinstance(a, type)
                          and issubclass(a, BaseModel)), None)
            if inner is None:
                args = getattr(field.annotation, "__args__", ())
                inner = next((b for a in args for b in getattr(a, "__args__", ()) if isinstance(b, type)
                              and issubclass(b, BaseModel)), None)
            out[name] = [within_limits(inner, i) if inner else i for i in item]
        elif isinstance(item, str) and limit:
            out[name] = item[:limit]
    return out


def fit_parser_output(data: dict[str, Any]) -> dict[str, Any]:
    return within_limits(StructuredResumeIn, data)


def materialize(source: StructuredResumeIn | dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validated structured data with ids. Items keep ids (and their stored verification) from `previous`;
    new items get generated ids and start UNVERIFIED. Callers decide what a content change does to status."""
    data = source if isinstance(source, StructuredResumeIn) else StructuredResumeIn.model_validate(source)
    ids = _Ids(previous)
    old = {unit.item_id: unit.node.get("verification") for unit in iter_units(previous)} if previous else {}

    def status(item_id: str) -> dict[str, Any]:
        return dict(old.get(item_id) or _unverified())

    summary = data.summary if isinstance(data.summary, SummaryIn) else SummaryIn(text=str(data.summary))
    out: dict[str, Any] = {
        "personal_information": {"id": ids.take("personal", "personal"), **data.personal_information.model_dump()},
        "summary": {"id": ids.take("summary", "summary"), "text": summary.text},
    }
    out["skills"] = [{"id": ids.take(s.id, f"skill_{slug(s.name) or short_hash(s.name)}"), "name": s.name,
                      "category": s.category, **({"raw": s.raw} if s.raw else {})} for s in data.skills if s.name]
    experience = []
    for role in data.experience:
        role_id = ids.take(role.id, f"exp_{slug(role.company) or slug(role.job_title) or 'role'}_{_year(role.start_date or role.raw_start_date)}")
        fields = role.model_dump(exclude={"id", "responsibilities", "achievements"})
        entry = {"id": role_id, **fields,
                 "responsibilities": _bullets(role.responsibilities, ids, "resp", role_id),
                 "achievements": _bullets(role.achievements, ids, "ach", role_id)}
        entry["metrics"] = [m for b in entry["achievements"] + entry["responsibilities"] for m in b["metrics"]]
        if not entry["raw_bullets"]:
            entry["raw_bullets"] = [b["text"] for b in entry["responsibilities"] + entry["achievements"]]
        experience.append(entry)
    out["experience"] = experience
    projects = []
    for project in data.projects:
        project_id = ids.take(project.id, f"proj_{slug(project.name) or short_hash(project.description)}")
        projects.append({"id": project_id, **project.model_dump(exclude={"id", "achievements"}),
                         "achievements": _bullets(project.achievements, ids, "pach", project_id)})
    out["projects"] = projects
    out["education"] = [{"id": ids.take(e.id, f"edu_{slug(e.institution) or slug(e.degree) or 'school'}_{_year(e.end_date or e.raw_end_date)}"),
                         **e.model_dump(exclude={"id"})} for e in data.education if e.degree or e.institution]
    out["certifications"] = [{"id": ids.take(c.id, f"cert_{slug(c.name) or short_hash(c.issuer)}"),
                              **c.model_dump(exclude={"id"})} for c in data.certifications if c.name]
    out["awards"] = [{"id": ids.take(a.id, f"award_{slug(a.title) or short_hash(a.description)}"),
                      **a.model_dump(exclude={"id"})} for a in data.awards if a.title or a.description]
    out["languages"] = [{"id": ids.take(lang.id, f"lang_{slug(lang.name)}"), **lang.model_dump(exclude={"id"})}
                        for lang in data.languages if lang.name]
    additional = [a if isinstance(a, AdditionalIn) else AdditionalIn(text=str(a)) for a in data.additional_information]
    out["additional_information"] = [{"id": ids.take(a.id, f"info_{short_hash(a.text)}"), "text": a.text}
                                     for a in additional if a.text]
    for unit in iter_units(out):
        unit.node["verification"] = status(unit.item_id)
    out["derived_information"] = derive(out)
    return out


# ───────────── traversal ─────────────

@dataclass
class Unit:
    """One verifiable unit of structured data."""

    section: str
    item_id: str
    path: str  # e.g. experience[exp_zendrive_2024].achievements[ach_1a2b3c4d]
    node: dict[str, Any]
    parent: dict[str, Any] | None = None
    kind: str = "item"  # personal | summary | skill | role | bullet | project | education | ...

    @property
    def status(self) -> str:
        return (self.node.get("verification") or {}).get("status") or "UNVERIFIED"

    def content(self) -> Any:
        """The facts the verification covers: everything but ids, verification, child bullets and derived fields."""
        skip = {"id", "verification", "responsibilities", "achievements", "metrics", "raw_bullets", "source_role_index"}
        return {k: v for k, v in self.node.items() if k not in skip}


def iter_units(data: dict[str, Any] | None) -> Iterator[Unit]:
    if not data:
        return
    if isinstance(data.get("personal_information"), dict):
        yield Unit("personal_information", data["personal_information"].get("id", "personal"), "personal_information",
                   data["personal_information"], kind="personal")
    if isinstance(data.get("summary"), dict) and data["summary"].get("text"):
        yield Unit("summary", data["summary"].get("id", "summary"), "summary", data["summary"], kind="summary")
    for section in LIST_SECTIONS:
        for item in data.get(section) or []:
            if not isinstance(item, dict) or "id" not in item:
                continue
            path = f"{section}[{item['id']}]"
            yield Unit(section, item["id"], path, item, kind=section)
            for child in ("responsibilities", "achievements") if section in ("experience", "projects") else ():
                for bullet in item.get(child) or []:
                    if isinstance(bullet, dict) and "id" in bullet:
                        yield Unit(section, bullet["id"], f"{path}.{child}[{bullet['id']}]", bullet, parent=item,
                                   kind="bullet")


def find_unit(data: dict[str, Any] | None, item_id: str) -> Unit | None:
    return next((unit for unit in iter_units(data) if unit.item_id == item_id), None)


def as_input(data: dict[str, Any]) -> StructuredResumeIn:
    return StructuredResumeIn.model_validate(data)


# ───────────── derived information ─────────────

LEADERSHIP_WORDS = ("led ", "lead ", "managed", "manager", "mentor", "hired", "head of", "director", "principal",
                    "team of", "roadmap")
AI_WORDS = (" ai ", " ai/", " ml ", "machine learning", "llm", "genai", "generative", "nlp", "deep learning", "rag ",
            "langchain", "langgraph", "openai", "model serving")
CLOUD_WORDS = ("aws", "gcp", "google cloud", "azure", "kubernetes", "k8s", "cloud", "terraform", "serverless")
DATA_WORDS = ("data platform", "kafka", "spark", "airflow", "etl", "data pipeline", "warehouse", "snowflake",
              "bigquery", "flink", "streaming", "lakehouse", "dbt")


def _months(value: str | None) -> int | None:
    match = re.fullmatch(r"(\d{4})-(\d{2})", value or "")
    return int(match.group(1)) * 12 + int(match.group(2)) - 1 if match else None


def experience_years(roles: list[dict[str, Any]], today: date | None = None) -> float | None:
    today = today or utcnow().date()
    spans = []
    for role in roles:
        start = _months(role.get("start_date"))
        end = _months(role.get("end_date")) if role.get("end_date") else (
            today.year * 12 + today.month - 1 if role.get("is_current") else None)
        if start is None or end is None or end < start:
            continue
        spans.append((start, end + 1))
    if not spans:
        return None
    spans.sort()
    total, (cur_start, cur_end) = 0, spans[0]
    for start, end in spans[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            total, cur_start, cur_end = total + cur_end - cur_start, start, end
    return round((total + cur_end - cur_start) / 12, 1)


def derive(data: dict[str, Any]) -> dict[str, Any]:
    roles = data.get("experience") or []
    text = " " + " ".join(
        [r.get("job_title", "") + " " + " ".join(b["text"] for b in r.get("achievements", []) + r.get("responsibilities", []))
         + " " + " ".join(r.get("technologies") or []) for r in roles]
        + [(data.get("summary") or {}).get("text", "")]).lower() + " "
    current = next((r for r in roles if r.get("is_current")), None) or {}
    return {
        "total_experience_years": experience_years(roles),
        "current_role": current.get("job_title") or None,
        "current_company": current.get("company") or None,
        "primary_roles": list(dict.fromkeys(r.get("job_title") for r in roles if r.get("job_title")))[:3],
        "primary_skills": [s["name"] for s in (data.get("skills") or [])][:10],
        "leadership_experience": any(w in text for w in LEADERSHIP_WORDS),
        "ai_experience": any(w in text for w in AI_WORDS),
        "cloud_experience": any(w in text for w in CLOUD_WORDS),
        "data_platform_experience": any(w in text for w in DATA_WORDS),
    }


def from_parser_output(structured: dict[str, Any]) -> dict[str, Any]:
    """Map the LLM parser's `structured_resume` (app.prompts.resume_parser template) onto the input shape."""
    data = dict(structured or {})
    personal = data.get("personal_information") or data.get("candidate") or {}
    skills = data.get("skills") or []
    if isinstance(skills, dict):  # {category: [names]}
        skills = [{"name": name, "category": category.replace("_", " ")} for category, names in skills.items()
                  for name in names or [] if isinstance(name, str)]
    else:
        skills = [s if isinstance(s, dict) else {"name": str(s)} for s in skills]
    experience = []
    for role in data.get("experience") or []:
        if not isinstance(role, dict):
            continue
        role = dict(role)
        role.setdefault("raw_start_date", role.pop("start_date_raw", None))
        role.setdefault("raw_end_date", role.pop("end_date_raw", None))
        experience.append(role)
    education = []
    for item in data.get("education") or []:
        if isinstance(item, dict):
            item = dict(item)
            item.setdefault("raw_start_date", item.pop("start_date_raw", None))
            item.setdefault("raw_end_date", item.pop("end_date_raw", None))
            education.append(item)
    awards = [a if isinstance(a, dict) else {"title": str(a)} for a in data.get("awards") or []]
    languages = [lang if isinstance(lang, dict) else {"name": str(lang)} for lang in data.get("languages") or []]
    summary = data.get("summary")
    return fit_parser_output({
        "personal_information": personal,
        "summary": summary.get("text", "") if isinstance(summary, dict) else summary or "",
        "skills": skills, "experience": experience, "projects": data.get("projects") or [], "education": education,
        "certifications": data.get("certifications") or [], "awards": awards, "languages": languages,
        "additional_information": [a if isinstance(a, (str, dict)) else str(a)
                                   for a in data.get("additional_information") or []]})


MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _display_date(value: str | None, raw: str | None) -> str:
    """A date as the resume shows it: the text as written while it still says the same thing, else the corrected
    YYYY-MM as "Mon YYYY"."""
    raw = (raw or "").strip()
    if not value:
        return raw
    if raw and normalize_date(raw)[0] == value:
        return raw
    match = re.fullmatch(r"(\d{4})-(\d{2})", value)
    return f"{MONTH_NAMES[int(match.group(2)) - 1]} {match.group(1)}" if match and 1 <= int(match.group(2)) <= 12 \
        else value


def display_dates(item: dict[str, Any]) -> str:
    start = _display_date(item.get("start_date"), item.get("raw_start_date"))
    end = "Present" if item.get("is_current") else _display_date(item.get("end_date"), item.get("raw_end_date"))
    return " - ".join(part for part in (start, end) if part)


def _ordered_bullets(role: dict[str, Any], keep) -> list[str]:
    """A role's bullets in the order the resume wrote them (raw_bullets); bullets added later come last."""
    bullets = [b for b in (role.get("achievements") or []) + (role.get("responsibilities") or [])
               if isinstance(b, dict) and (b.get("text") or "").strip() and keep(b)]
    written = {text: index for index, text in enumerate(role.get("raw_bullets") or [])}
    return [b["text"].strip() for b in sorted(bullets, key=lambda b: written.get(b["text"], len(written)))]


def _comparable(text: str) -> str:
    """Lower-cased words only, so "…for internal teams." and "for internal teams" compare equal."""
    return " ".join(re.findall(r"[a-z0-9%]+", (text or "").lower()))


def render_document(data: dict[str, Any] | None) -> str:
    """The structured data as a resume document in the dialect resume_format.parse_resume reads.

    Used when the candidate policy's resume source is "structured": tailoring, review and every rendered resume
    then show the data as the person corrected it (titles, employers, dates, education), never the raw text. Only
    REJECTED units are left out; what the Tailor may *claim* is governed separately by the candidate policy.
    Roles keep their stored order, so a role's index here is its index in data["experience"] minus rejected roles.
    """
    if not data:
        return ""

    def keep(node: dict[str, Any]) -> bool:
        return ((node.get("verification") or {}).get("status") or "UNVERIFIED") != "REJECTED"

    out: list[str] = []
    personal = data.get("personal_information") or {}
    if keep(personal):
        if personal.get("full_name"):
            out.append(f"# {personal['full_name']}")
        if personal.get("headline"):
            out.append(personal["headline"])
        contact = [personal.get(k) for k in ("location", "email", "phone", "linkedin", "github", "portfolio")]
        contact += list(personal.get("other_links") or [])
        if any(contact):
            out.append(" | ".join(str(c) for c in contact if c))
    summary = data.get("summary") or {}
    if summary.get("text") and keep(summary):
        out += ["", "## Summary", summary["text"].strip()]
    skills = [s for s in data.get("skills") or [] if keep(s) and s.get("name")]
    if skills:
        groups: dict[str, list[str]] = {}
        for skill in skills:
            groups.setdefault((skill.get("category") or "").strip(), []).append(skill["name"])
        out += ["", "## Skills", *[f"- {label}: {', '.join(names)}" if label else f"- {', '.join(names)}"
                                   for label, names in groups.items()]]
    roles = [r for r in data.get("experience") or [] if keep(r)]
    if roles:
        out += ["", "## Experience"]
        for role in roles:
            title = (role.get("job_title") or "").strip() or (role.get("company") or "").strip() or "Role"
            company = (role.get("company") or "").strip() if role.get("job_title") else ""
            location, dates = (role.get("location") or "").strip(), display_dates(role)
            if company and dates:  # "Title | Company | [Location |] Dates": the layout parse_resume reads by position
                out += ["", f"### {' | '.join(p for p in (title, company, location, dates) if p)}"]
            else:  # without an employer or dates, the rest goes under the heading so no part lands in the wrong place
                out += ["", f"### {' | '.join(p for p in (title, company) if p)}"]
                # With an employer, "Location | Dates" under the heading; without one a location would read as the
                # employer, so it stays out of the text (it is still in the structured data).
                meta = [location, dates] if company else [dates]
                if any(meta):
                    out.append(" | ".join(p for p in meta if p))
            out += [f"- {text}" for text in _ordered_bullets(role, keep)]
    projects = [p for p in data.get("projects") or [] if keep(p) and (p.get("name") or p.get("description"))]
    if projects:
        out += ["", "## Projects"]
        for project in projects:
            head = ": ".join(part for part in ((project.get("name") or "").strip(),
                                                (project.get("description") or "").strip()) if part)
            out.append(head)
            # An achievement the description already says (a parser often splits one sentence into both) is not
            # printed again: the resume would repeat itself.
            said = _comparable(project.get("description") or "")
            out += [f"- {b['text'].strip()}" for b in project.get("achievements") or []
                    if isinstance(b, dict) and (b.get("text") or "").strip() and keep(b)
                    and not (said and _comparable(b["text"]) in said)]
    education = [e for e in data.get("education") or [] if keep(e)]
    if education:
        out += ["", "## Education"]
        for entry in education:
            degree = ", ".join(p for p in (entry.get("degree"), entry.get("field_of_study")) if p)
            out.append(" | ".join(p for p in (degree, entry.get("institution"), entry.get("university"),
                                              entry.get("location"), display_dates(entry), entry.get("grade")) if p))
    for label, section, fields in (("Certifications", "certifications", ("name", "issuer", "issue_date")),
                                   ("Awards", "awards", ("title", "issuer", "date")),
                                   ("Languages", "languages", ("name", "proficiency"))):
        items = [i for i in data.get(section) or [] if keep(i)]
        if items:
            out += ["", f"## {label}", *[" | ".join(str(i[f]) for f in fields if i.get(f)) for i in items]]
    extra = [i.get("text") for i in data.get("additional_information") or [] if keep(i) and i.get("text")]
    if extra:
        out += ["", "## Additional Information", *extra]
    return "\n".join(out).strip() + "\n" if out else ""


def render_text(data: dict[str, Any], *, statuses: tuple[str, ...] = REVIEWED[:1]) -> str:
    """A compact plain-text rendering of the units whose status is in `statuses` (VERIFIED by default)."""
    if not data:
        return ""
    lines: list[str] = []
    keep = set(statuses)

    def ok(node: dict[str, Any]) -> bool:
        return ((node.get("verification") or {}).get("status") or "UNVERIFIED") in keep

    personal = data.get("personal_information") or {}
    if ok(personal):
        lines += [v for v in (personal.get("full_name"), personal.get("headline"), personal.get("location")) if v]
    summary = data.get("summary") or {}
    if summary.get("text") and ok(summary):
        lines += ["", "Summary", summary["text"]]
    skills = [s["name"] for s in data.get("skills") or [] if ok(s)]
    if skills:
        lines += ["", "Skills", ", ".join(skills)]
    for role in data.get("experience") or []:
        if not ok(role):
            continue
        dates = f"{role.get('start_date') or role.get('raw_start_date') or '?'} - " \
                f"{'present' if role.get('is_current') else role.get('end_date') or role.get('raw_end_date') or '?'}"
        lines += ["", " | ".join(filter(None, [role.get("job_title"), role.get("company"), role.get("location"), dates]))]
        lines += [f"- {b['text']}" for b in role.get("achievements", []) + role.get("responsibilities", []) if ok(b)]
    for label, section, fields in (("Projects", "projects", ("name", "description")),
                                   ("Education", "education", ("degree", "field_of_study", "institution", "university",
                                                                  "grade", "end_date")),
                                   ("Certifications", "certifications", ("name", "issuer", "issue_date")),
                                   ("Awards", "awards", ("title", "issuer", "date")),
                                   ("Languages", "languages", ("name", "proficiency"))):
        items = [i for i in data.get(section) or [] if ok(i)]
        if items:
            lines += ["", label, *[" | ".join(str(i[f]) for f in fields if i.get(f)) for i in items]]
    return "\n".join(lines).strip()
