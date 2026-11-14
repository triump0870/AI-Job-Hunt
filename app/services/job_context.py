"""Task-specific job context: the parts of a posting a task needs, from the structured description.

    Job (raw description, immutable) + structured_description + JobRequirementEvidence
    → build_ranking_job_context / build_tailoring_job_context / build_review_job_context
      / build_interview_insights_job_context

Selection is application logic, not a model call. The raw description is sent only when a posting has not been
parsed (or parsing failed), so a task sees requirements rather than the whole advert.

Two separations are kept everywhere:
* REQUIRED against PREFERRED: a preferred skill is never presented as a hard requirement.
* explicit against derived: what the employer wrote against what the system inferred. Derived signals travel with
  `derived: true` and a label saying they are likely, inferred preparation topics, never employer statements.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, JobRequirementEvidence
from app.services.llm_telemetry import estimate_tokens

REQUIRED, PREFERRED, INFORMATIONAL, UNKNOWN = "REQUIRED", "PREFERRED", "INFORMATIONAL", "UNKNOWN"
CRITICAL_LEVELS = (REQUIRED,)
RANKING_RESPONSIBILITIES = 8
RANKING_REQUIREMENTS = 25
TAILOR_REQUIREMENTS = 12
# Tailoring addresses the responsibilities in the employer's own order: the first ones are what the role is for.
TAILOR_RESPONSIBILITIES = 10
DERIVED_LABEL = "likely / inferred preparation topics, not stated by the employer"


@dataclass
class JobContextStats:
    raw_job_tokens: int = 0
    selected_job_context_tokens: int = 0
    job_context_reduction_percent: float = 0.0

    @classmethod
    def measure(cls, raw: int, selected: int) -> JobContextStats:
        reduction = round(100 * (raw - selected) / raw, 1) if raw else 0.0
        return cls(raw, selected, max(0.0, reduction))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JobContext:
    """What one task receives about a posting."""

    task: str  # ranking | tailoring | review | interview_insights
    job_id: int
    identity: dict[str, Any] = field(default_factory=dict)
    role_summary: str | None = None
    required: list[dict[str, Any]] = field(default_factory=list)
    preferred: list[dict[str, Any]] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    leadership_expectations: list[str] = field(default_factory=list)
    technical_domains: list[str] = field(default_factory=list)
    education_requirements: list[str] = field(default_factory=list)
    experience_requirements: dict[str, Any] = field(default_factory=dict)
    location_requirements: dict[str, Any] = field(default_factory=dict)
    compensation: dict[str, Any] = field(default_factory=dict)
    hard_requirements: list[str] = field(default_factory=list)
    derived_signals: dict[str, Any] = field(default_factory=dict)
    candidate_matches: list[dict[str, Any]] = field(default_factory=list)
    candidate_gaps: list[dict[str, Any]] = field(default_factory=list)
    # The employer's own words for the requirements above, when a task needs to check them.
    source_evidence: list[dict[str, Any]] = field(default_factory=list)
    # Only when the posting has no structured description yet: the posting as written.
    raw_description: str = ""
    structured: bool = False
    status: str = "RAW"
    stats: JobContextStats = field(default_factory=JobContextStats)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out.pop("stats")
        return out


def _tokens(text: str) -> set[str]:
    from app.services.ranker import _tokens as tokenize

    return tokenize(text)


def requirements_of(db: Session, job: Job, *, levels: tuple[str, ...] | None = None,
                    derived: bool | None = False) -> list[JobRequirementEvidence]:
    """This posting's requirement evidence. `derived=False` keeps only what the employer actually wrote."""
    query = select(JobRequirementEvidence).where(JobRequirementEvidence.job_id == job.id)
    if levels:
        query = query.where(JobRequirementEvidence.requirement_level.in_(levels))
    if derived is not None:
        query = query.where(JobRequirementEvidence.is_derived.is_(derived))
    return list(db.scalars(query.order_by(JobRequirementEvidence.id)).all())


def requirement_dict(row: JobRequirementEvidence, *, with_source: bool = False) -> dict[str, Any]:
    out = {"id": row.item_id, "type": row.requirement_type, "value": row.normalized_value,
           "level": row.requirement_level, "confidence": round(row.confidence, 2), "derived": row.is_derived,
           "origin": row.origin}
    if with_source:
        out |= {"source_text": row.source_text, "source_section": row.source_section}
    return out


# A sentence that explicitly offers a choice. Only these group rows parsed before the parser marked alternatives
# itself: a bare "or" is too loose to merge requirements on.
CHOICE_CUE = re.compile(r"\b(?:one of|any of|either\b.+\bor)\b", re.IGNORECASE)


def alternative_groups(rows: list[JobRequirementEvidence]) -> dict[int, str]:
    """Which rows are alternatives of one requirement, as {row id: group id}.

    The parser's alternative_group decides. Rows parsed before it existed are grouped only when two or more of the
    same type and level quote the same sentence and that sentence says "one of" / "any of" / "either ... or"."""
    groups: dict[int, str] = {}
    by_sentence: dict[tuple[str, str, str], list[JobRequirementEvidence]] = {}
    marked: dict[tuple[str, str], list[JobRequirementEvidence]] = {}
    for row in rows:
        if row.alternative_group:
            marked.setdefault((row.alternative_group, row.requirement_level), []).append(row)
        elif row.source_text and CHOICE_CUE.search(row.source_text):
            key = (row.source_text.strip(), row.requirement_type, row.requirement_level)
            by_sentence.setdefault(key, []).append(row)
    for (group, _level), members in marked.items():
        if len(members) > 1:
            groups |= {row.id: group for row in members}
    for members in by_sentence.values():
        if len(members) > 1:
            groups |= {row.id: f"alt_{members[0].item_id}" for row in members}
    return groups


def grouped_requirements(rows: list[JobRequirementEvidence], level: str, *,
                         with_source: bool = False) -> list[dict[str, Any]]:
    """The requirements at one level as a task should weigh them: alternatives the posting offers ("one of Python,
    Java or Go") become one requirement met by any of them, in the position of its first member."""
    groups = alternative_groups(rows)
    out: list[dict[str, Any]] = []
    collected: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.requirement_level != level:
            continue
        group = groups.get(row.id)
        if group is None:
            out.append(requirement_dict(row, with_source=with_source))
            continue
        if group not in collected:
            entry = requirement_dict(row, with_source=with_source) | {"id": group, "alternatives": [], "member_ids": []}
            collected[group] = entry
            out.append(entry)
        entry = collected[group]
        entry["alternatives"].append(row.normalized_value)
        entry["member_ids"].append(row.item_id)
        entry["confidence"] = min(entry["confidence"], round(row.confidence, 2))
    for entry in collected.values():
        entry["value"] = "Any one of: " + ", ".join(entry["alternatives"])
    return out


def _structured(job: Job) -> dict[str, Any]:
    """The structured description, only when it was parsed from the description the job has now.

    A posting whose text changed after parsing (and whose re-parse is pending or failed) is stale: every task
    falls back to the current raw text rather than reading requirements from the old advert.
    """
    from app.services.job_description import is_current

    return (job.structured_description or {}) if is_current(job) else {}


# Words a requirement sentence uses to ask for something, rather than the thing itself.
FILLER = {"strong", "experience", "experienced", "required", "require", "requires", "requirement", "requirements",
          "years", "year", "knowledge", "familiarity", "familiar", "exposure", "plus", "ability", "able", "proven",
          "demonstrated", "understanding", "skills", "skill", "working", "work", "hands", "solid", "excellent",
          "good", "great", "deep", "must", "have", "should", "you", "candidate", "ideally", "preferred", "bonus",
          "building", "build", "using", "use", "and", "with", "the", "for"}


def _distinctive(text: str) -> set[str]:
    """The words that name what is being asked for, without the words used to ask for it."""
    words = _tokens(text)
    return (words - FILLER) or words


def candidate_match(requirements: list[dict[str, Any]], candidate: Any) -> tuple[list[dict], list[dict]]:
    """Which requirements the candidate's verified facts already answer, and which they do not.

    Matching is deterministic word overlap against the candidate's verified skills, bullets and evidence, so the
    model spends its attention on the ambiguous ones rather than re-deriving the obvious.
    """
    from app.services.context_builder import TaskContext

    if isinstance(candidate, TaskContext):
        pool = " ".join(
            [" ".join(candidate.relevant_skills)]
            + [f"{role.get('job_title', '')} {role.get('company', '')} " + " ".join(
                b["text"] for b in role.get("bullets", [])) for role in candidate.relevant_experience]
            + [f"{item.get('claim', '')} {' '.join(item.get('skills') or [])}" for item in candidate.relevant_evidence])
    else:
        pool = str(candidate or "")
    words = _tokens(pool)
    matches, gaps = [], []
    def coverage(value: str) -> tuple[bool, set[str]]:
        needed = _distinctive(value)
        overlap = needed & words
        # Half of what the requirement actually names has to appear in the candidate's verified facts.
        return bool(overlap) and len(overlap) >= max(1, round(0.5 * len(needed))), overlap

    for item in requirements:
        # Alternatives ("one of Python, Java or Go") are met by any one of them.
        options = [coverage(value) for value in item.get("alternatives") or [item["value"]]]
        covered, overlap = next(((True, terms) for hit, terms in options if hit),
                                (False, set().union(*(terms for _hit, terms in options))))
        entry = {**item, "matched_terms": sorted(overlap)[:6]}
        (matches if covered else gaps).append(entry)
    return matches, gaps


# Paragraphs that ask nothing of the candidate and appear in most postings: equal-opportunity statements,
# accommodation notices and "learn more about us" links.
BOILERPLATE = re.compile(
    r"equal[- ]opportunity employer|equal employment opportunity|reasonable accommodation|regardless of (?:age|race|"
    r"color|gender|religion)|protected (?:veteran|by applicable law)|to learn more,? about|check out more about|"
    r"e-verify", re.IGNORECASE)


def compact_posting(text: str) -> str:
    """The posting as a model should read it: exact repeated paragraphs and legal boilerplate removed. Everything
    the employer asks for, and what they say about the role and company, stays word for word."""
    kept, seen = [], set()
    for paragraph in re.split(r"\n\s*\n", text or ""):
        key = re.sub(r"[^a-z0-9]+", " ", paragraph.lower()).strip()
        if not key or key in seen or BOILERPLATE.search(paragraph):
            continue
        seen.add(key)
        kept.append(paragraph.strip())
    return "\n\n".join(kept)


def _relevant(values: list[str], terms: set[str], limit: int) -> list[str]:
    scored = sorted(values, key=lambda value: -len(_tokens(value) & terms))
    return scored[:limit] if limit else scored


def _stats(job: Job, context: JobContext) -> None:
    raw = estimate_tokens(job.description or "")
    selected = estimate_tokens(json.dumps(context.to_dict(), default=str))
    context.stats = JobContextStats.measure(max(raw, selected), selected)


def _base(db: Session, job: Job, task: str) -> JobContext:
    data = _structured(job)
    context = JobContext(task=task, job_id=job.id, status=job.structured_description_status,
                         structured=bool(data))
    if not data:
        # Not parsed, parsing failed, or the posting changed since: the task gets the posting as it is now.
        from app.services.job_description import normalize_description

        context.raw_description = compact_posting(normalize_description(job.description or ""))
        context.identity = {"title": job.title, "company": job.company, "location": job.location}
        return context
    context.identity = {k: v for k, v in (data.get("job_identity") or {}).items() if v}
    context.role_summary = data.get("role_summary")
    context.experience_requirements = {k: v for k, v in (data.get("experience_requirements") or {}).items()
                                       if v is not None}
    context.location_requirements = {k: v for k, v in (data.get("location_requirements") or {}).items()
                                     if v is not None}
    context.compensation = {k: v for k, v in (data.get("compensation") or {}).items() if v is not None}
    context.technical_domains = data.get("technical_domains") or []
    context.hard_requirements = data.get("hard_requirements") or []
    return context


def build_ranking_job_context(db: Session, job: Job, candidate: Any = None) -> JobContext:
    """What the ranker needs: identity, required and preferred requirements, experience, responsibilities,
    leadership, domains, location and work authorization, with the candidate's matches and gaps when known."""
    context = _base(db, job, "ranking")
    data = _structured(job)
    if not context.structured:
        _stats(job, context)
        return context
    rows = requirements_of(db, job)
    terms = _tokens(f"{job.title or ''} {job.description or ''}")
    context.required = grouped_requirements(rows, REQUIRED)[:RANKING_REQUIREMENTS]
    context.preferred = grouped_requirements(rows, PREFERRED)[:RANKING_REQUIREMENTS]
    context.responsibilities = _relevant(data.get("responsibilities") or [], terms, RANKING_RESPONSIBILITIES)
    context.leadership_expectations = data.get("leadership_expectations") or []
    context.education_requirements = data.get("education_requirements") or []
    if candidate is not None:
        context.candidate_matches, context.candidate_gaps = candidate_match(context.required + context.preferred,
                                                                           candidate)
    # The employer's own sentences for the hard requirements, so a fatal gap can always be justified.
    context.source_evidence = [requirement_dict(r, with_source=True) for r in rows
                               if r.requirement_level == REQUIRED and r.requirement_type in
                               ("EXPERIENCE", "EDUCATION", "WORK_AUTHORIZATION", "VISA", "LOCATION", "SENIORITY")][:10]
    _stats(job, context)
    return context


def build_tailoring_job_context(db: Session, job: Job, candidate: Any = None) -> JobContext:
    """What tailoring needs: the critical and preferred requirements, the responsibilities to address, and where
    the candidate already matches or falls short. Not the whole posting."""
    context = _base(db, job, "tailoring")
    data = _structured(job)
    if not context.structured:
        _stats(job, context)
        return context
    rows = requirements_of(db, job)
    context.required = grouped_requirements(rows, REQUIRED)[:TAILOR_REQUIREMENTS]
    context.preferred = grouped_requirements(rows, PREFERRED)[:TAILOR_REQUIREMENTS]
    context.responsibilities = (data.get("responsibilities") or [])[:TAILOR_RESPONSIBILITIES]
    context.leadership_expectations = data.get("leadership_expectations") or []
    if candidate is not None:
        context.candidate_matches, context.candidate_gaps = candidate_match(context.required + context.preferred,
                                                                           candidate)
    _stats(job, context)
    return context


def build_review_job_context(db: Session, job: Job) -> JobContext:
    """What the reviewer needs: the requirements with the employer's own sentences, so it can check both that the
    resume stays factual and that it answers what the posting actually asks."""
    context = _base(db, job, "review")
    data = _structured(job)
    if not context.structured:
        _stats(job, context)
        return context
    rows = requirements_of(db, job)
    context.required = grouped_requirements(rows, REQUIRED)
    context.preferred = grouped_requirements(rows, PREFERRED)
    context.responsibilities = data.get("responsibilities") or []
    context.leadership_expectations = data.get("leadership_expectations") or []
    context.source_evidence = [requirement_dict(r, with_source=True) for r in rows
                               if r.requirement_level in (REQUIRED, PREFERRED)]
    _stats(job, context)
    return context


def build_interview_insights_job_context(db: Session, job: Job) -> JobContext:
    """What interview preparation needs: the domains, responsibilities and skills the posting states, plus the
    derived signals, clearly labelled as inferred."""
    context = _base(db, job, "interview_insights")
    data = _structured(job)
    if not context.structured:
        _stats(job, context)
        return context
    rows = requirements_of(db, job)
    context.required = grouped_requirements(rows, REQUIRED)
    context.preferred = grouped_requirements(rows, PREFERRED)
    context.responsibilities = data.get("responsibilities") or []
    context.leadership_expectations = data.get("leadership_expectations") or []
    context.derived_signals = {"label": DERIVED_LABEL, **(data.get("derived_signals") or {}),
                               "derived_requirements": [requirement_dict(r) for r in
                                                        requirements_of(db, job, derived=True)]}
    _stats(job, context)
    return context


def _hash_of(document: dict[str, Any] | None) -> str:
    from app.services.job_description import structured_hash

    return structured_hash(document)


def summary(db: Session, job: Job) -> dict[str, Any]:
    """The structured description as the API and the UI show it, with its requirement evidence."""
    rows = requirements_of(db, job, derived=None)
    groups = alternative_groups(rows)
    data = job.structured_description or {}
    stale = bool(data) and not _structured(job)
    return {
        "job_id": job.id, "status": job.structured_description_status,
        # The posting changed after it was parsed: workflows use the raw text until the new parse lands.
        "stale": stale,
        "version": job.structured_description_version,
        "parsed_at": job.structured_description_parsed_at.isoformat() if job.structured_description_parsed_at else None,
        "structured_description": data or None,
        # Sent back with a correction, so a correction never writes over a parse that landed while editing.
        "structured_hash": _hash_of(job.structured_description),
        "has_candidate": bool(job.structured_description_candidate),
        "candidate": job.structured_description_candidate,
        # Each row as the employer wrote it; alternative_group ties the options of one "any one of" requirement.
        "requirements": [requirement_dict(r, with_source=True) | {"alternative_group": groups.get(r.id)}
                         for r in rows],
        # A group of alternatives counts once: it is one requirement.
        "counts": {level: len({groups.get(r.id, r.id) for r in rows if r.requirement_level == level})
                   for level in (REQUIRED, PREFERRED, INFORMATIONAL, UNKNOWN)},
        "derived_label": DERIVED_LABEL,
    }
