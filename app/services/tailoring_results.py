"""Tailored resumes: schema and deterministic checks for every tailoring path, and the patch renderer.

A model writes the tailored content — Claude Desktop over MCP, or the backend Tailor worker, whose answer is a
TailoringResult patch (app.prompts.contracts). This module decides whether it may be stored. Every check is
deterministic, and the same checks run for both paths:

* UNKNOWN_EVIDENCE        an evidence ID that is not a verified Evidence row of this account
* EVIDENCE_ROLE_MISMATCH  a role's bullet cites evidence recorded for a different employer
* UNCITED_CLAIM           a bullet with no evidence ID that is not a close restatement of a master resume bullet
* INVENTED_METRIC         a number that appears in neither the cited evidence nor the master resume
* UNSUPPORTED_TERM        a technology, employer or other name in a bullet, the summary, cover letter or a screening
                          answer that appears nowhere in the master resume, the profile or verified evidence
                          (a job requirement the candidate lacks stays missing: it cannot enter as a named term)
* EVIDENCE_NOT_IN_CONTEXT (backend Tailor) evidence the tailoring context did not supply, e.g. evidence held back for
                          a provenance conflict
* INVENTED_SKILL          a skill found nowhere in the master resume, the profile skills or verified evidence
* UNKNOWN_ROLE            a role_index the master resume does not have (titles, employers and dates are never taken
                          from the input, so they cannot be changed)
* WORK_AUTHORIZATION      visa / sponsorship / citizenship wording the candidate profile does not state
* LAYOUT / EMPTY          bullets in a shape the master resume cannot hold, or no content at all
"""
from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CandidateProfile, Evidence, Job
from app.services.resume_format import parse_resume, to_plain
from app.services.workflow import _best_match
from app.settings import get_settings

log = logging.getLogger("jobhunt.tailoring_results")

# A bullet without evidence must restate a master resume bullet at least this closely (share of its words).
RESTATEMENT_THRESHOLD = 0.6
WORK_AUTH_RE = re.compile(
    r"\b(visas?|sponsor\w*|citizen\w*|permanent resident\w*|work permits?|work authori[sz]\w*|green card|"
    r"employment pass|right to work|h-?1b|pr status|residency)\b", re.IGNORECASE)
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
# Words as the corpus and the checked text are compared: "go" never matches inside "django", and "c++" survives.
WORD_RE = re.compile(r"[a-z0-9][a-z0-9+#.]*")
NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]*")
# A sentence, line or clause boundary: the word right after one may be capitalized for grammar alone.
SEGMENT_RE = re.compile(r"[.!?:;\n]+")
# Capitalized words that carry no factual claim, so they are never treated as a named technology or employer.
PROSE_WORDS = frozenset(""" 
i a an the and or but as at in on for to of with from by we our my your their this that these those it its
dear hello hi sincerely regards best kind thank thanks thankyou yours respectfully hiring manager team role
position opening application applicant candidate resume cv company please if when while where what which who
january february march april may june july august september october november december monday tuesday
wednesday thursday friday saturday sunday am pm ai ml
""".split())  # noqa: SIM905 - a readable block, not a literal list


class BulletIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=600)
    evidence_ids: list[int] = Field(default_factory=list, max_length=10)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _ids(cls, values: Any) -> Any:
        """Evidence IDs arrive as 12 or "12" (or "E12"); anything else is a schema error."""
        if not isinstance(values, list):
            return values
        out = []
        for value in values:
            if isinstance(value, str) and re.fullmatch(r"[Ee]?\d+", value.strip()):
                value = int(value.strip().lstrip("Ee"))
            out.append(value)
        return out


class ExperienceSectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_index: int = Field(ge=0)
    bullets: list[BulletIn] = Field(min_length=1, max_length=8)


class TailoredResumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: int
    summary: str = Field(default="", max_length=1500)
    skills_order: list[str] = Field(default_factory=list, max_length=40)
    experience_sections: list[ExperienceSectionIn] = Field(default_factory=list, max_length=20)
    resume_bullets: list[BulletIn] = Field(default_factory=list, max_length=30)
    cover_letter: str = Field(default="", max_length=8000)
    screening_answer_drafts: dict[str, str] = Field(default_factory=dict, max_length=20)

    @field_validator("skills_order")
    @classmethod
    def _skills(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(v.strip() for v in values if v.strip()))


def schema_errors(exc: ValidationError) -> list[dict[str, str]]:
    return [{"code": "SCHEMA", "location": ".".join(str(p) for p in error["loc"]) or "result", "message": error["msg"]}
            for error in exc.errors()[:10]]


def _issue(code: str, location: str, message: str) -> dict[str, str]:
    return {"code": code, "location": location, "message": message}


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in NUMBER_RE.findall(text or "")}


def _words(text: str) -> set[str]:
    """Whole words, so a skill is matched as a word and never as a fragment of another one."""
    return {word.rstrip(".") for word in WORD_RE.findall((text or "").lower())}


def _skill_words(skill: str) -> list[str]:
    return [word for word in _words(skill) if word]


def _named_terms(text: str) -> set[str]:
    """Names a reader would read as facts: acronyms (GCP, ETL) and words capitalized mid-sentence (Bedrock).

    A word that only opens a sentence or a line is skipped: its capital says nothing about what it claims.
    """
    terms = set()
    for segment in SEGMENT_RE.split(text or ""):
        for index, token in enumerate(NAME_RE.findall(segment.strip())):
            word = token.strip(".-").lower()
            acronym = token.isupper() and len(token) > 1
            capitalized = token[:1].isupper() and any(c.islower() for c in token[1:])
            if word and word not in PROSE_WORDS and (acronym or (capitalized and index > 0)):
                terms.add(word)
    return terms


# Separators inside one name ("CI/CD", "Node.js", "Python-based"): each part is a word of its own.
NAME_PARTS_RE = re.compile(r"[./-]+")


def _with_parts(words: set[str]) -> set[str]:
    """The words, plus the parts of each compound one, so "Node.js" in the resume supports "Node" in a bullet."""
    return words | {part for word in words for part in NAME_PARTS_RE.split(word) if part}


# Generic words a hyphenated compound may end in ("S3-backed", "Kafka-driven"). They name no technology, employer or
# skill, so they need no support of their own; the name they modify still does.
COMPOUND_MODIFIERS = frozenset({"backed", "based", "driven", "powered", "enabled", "native", "first", "ready",
                                "aware", "friendly", "oriented", "centric", "focused", "heavy", "facing", "agnostic"})


def _term_supported(term: str, supported: set[str]) -> bool:
    """A named term is supported when the corpus has it, or has every part of it: the resume's "CI/CD" is read as
    "ci" and "cd", and "Python-based" rests on "python" and "based". A trailing generic modifier ("S3-backed")
    needs no support, but the name before it always does: "backed" alone never passes."""
    if term in supported:
        return True
    parts = [part for part in NAME_PARTS_RE.split(term) if part]
    names = [part for part in parts if part not in COMPOUND_MODIFIERS] if len(parts) > 1 else parts
    return bool(names) and all(part in supported for part in names)


def verified_evidence(db: Session, job: Job) -> dict[int, Evidence]:
    """This account's verified evidence, by ID. Evidence of other accounts is never visible. With
    RAG_RETRIEVAL_MODE=on, only the evidence the job's own resume may use (app.services.evidence_tags), the same scope
    its context was built from, so a citation of another resume's fact is refused like an unknown one."""
    rows = db.scalars(select(Evidence).where(Evidence.user_id == job.user_id, Evidence.verified.is_(True))).all()
    evidence = {e.id: e for e in rows}
    scope = _resume_scope(db, job)
    return evidence if scope is None else {i: e for i, e in evidence.items() if i in scope}


def _resume_scope(db: Session, job: Job) -> set[int] | None:
    if get_settings().rag_mode != "on":
        return None
    from app.services import rag_retrieval

    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == job.user_id))
    if profile is None:
        return None
    try:
        return rag_retrieval.scope(db, profile, job)[1]
    except Exception:  # like retrieval itself: on error, the account-wide check of the deterministic path
        log.warning("resume scope for job %s could not be read; checking against all verified evidence", job.id,
                    exc_info=True)
        return None


def _same_employer(a: str, b: str) -> bool:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    return not a or not b or a in b or b in a


def _evidence_text(evidence: dict[int, Evidence]) -> str:
    return " ".join(f"{e.company} {e.role} {e.claim} {e.context} {' '.join(e.skills or [])} "
                    f"{' '.join(e.tags or [])}" for e in evidence.values())


def _vocabulary(master_text: str, evidence_text: str, profile: CandidateProfile) -> set[str]:
    """Everything the candidate can be said to have done: the resume, their own profile, and verified evidence."""
    prefs = profile.search_preferences or {}
    return _with_parts(_words(" ".join([master_text, evidence_text, profile.headline or "", profile.work_authorization or "",
                            *(str(s) for s in prefs.get("primary_skills") or []),
                            *(str(s) for s in prefs.get("secondary_skills") or [])])))


def validate_tailored_resume(db: Session, profile: CandidateProfile, job: Job, data: TailoredResumeIn, *,
                             legacy: bool = False) -> list[dict[str, str]]:
    """Every problem that stops this tailored resume from being stored. Empty means it may be saved.

    `legacy` re-checks content the backend worker produced before bullets carried evidence IDs: citation and
    metric checks are skipped for it because that content never recorded which evidence each bullet used.
    """
    from app.services import resume_verification as rv

    profile = rv.for_job(db, profile, job)  # checked against the resume this job uses, in the policy's form
    issues: list[dict[str, str]] = []
    master = profile.master_resume or ""
    parsed = parse_resume(master)
    master_text = to_plain(master)
    evidence = verified_evidence(db, job)

    if not legacy and not data.experience_sections and not data.resume_bullets and not data.summary.strip():
        issues.append(_issue("EMPTY", "result", "Provide a summary and tailored bullets"))
    if not legacy and parsed.structured and data.resume_bullets:
        issues.append(_issue("LAYOUT", "resume_bullets",
                             "The master resume has roles: put bullets under experience_sections with a role_index"))
    if not legacy and not parsed.structured and data.experience_sections:
        issues.append(_issue("LAYOUT", "experience_sections",
                             "The master resume has no parsed roles: use resume_bullets instead"))

    evidence_text = _evidence_text(evidence)
    supported = _vocabulary(master_text, evidence_text, profile)

    def check_bullet(bullet: BulletIn, location: str, candidates: list[str], employer: str) -> None:
        unknown = [i for i in bullet.evidence_ids if i not in evidence]
        if unknown:
            issues.append(_issue("UNKNOWN_EVIDENCE", location,
                                 f"Evidence IDs {unknown} are not verified evidence of this account"
                                 + (" that this job's resume may use" if get_settings().rag_mode == "on" else "")))
        cited = [evidence[i] for i in bullet.evidence_ids if i in evidence]
        for item in cited:
            if not _same_employer(item.company, employer):
                issues.append(_issue("EVIDENCE_ROLE_MISMATCH", location,
                                     f"Evidence {item.id} is from {item.company}, not {employer}"))
        if legacy:
            return
        if not bullet.evidence_ids:
            _, score = _best_match(bullet.text, candidates)
            if score < RESTATEMENT_THRESHOLD:
                issues.append(_issue("UNCITED_CLAIM", location,
                                     "A rewritten factual bullet must cite evidence_ids or restate a master resume bullet"))
        sources = master_text + " " + " ".join(f"{e.claim} {e.context}" for e in cited)
        invented = sorted(_numbers(bullet.text) - _numbers(sources))
        if invented:
            issues.append(_issue("INVENTED_METRIC", location,
                                 f"Numbers {invented} are not in the cited evidence or the master resume"))
        unsupported = sorted(term for term in _named_terms(bullet.text) if not _term_supported(term, supported))
        if unsupported:
            issues.append(_issue("UNSUPPORTED_TERM", location,
                                 f"{unsupported} appear nowhere in the master resume, your profile or verified evidence"))

    for s_index, section in enumerate(data.experience_sections):
        if section.role_index >= len(parsed.roles):
            if legacy:
                continue
            issues.append(_issue("UNKNOWN_ROLE", f"experience_sections.{s_index}.role_index",
                                 f"The master resume has no role {section.role_index}"))
            continue
        role = parsed.roles[section.role_index]
        for b_index, bullet in enumerate(section.bullets):
            check_bullet(bullet, f"experience_sections.{s_index}.bullets.{b_index}", role.bullets, role.company)
    all_master_lines = [line.strip(" -\t") for line in master_text.splitlines() if len(line.strip()) > 20]
    for b_index, bullet in enumerate(data.resume_bullets):
        check_bullet(bullet, f"resume_bullets.{b_index}", all_master_lines, "")

    for skill in data.skills_order:
        words = _skill_words(skill)
        if not words or not set(words) <= supported:
            issues.append({**_issue("INVENTED_SKILL", "skills_order",
                                    f"'{skill}' is not in the master resume, profile skills or verified evidence"),
                           "value": skill})

    # The summary, cover letter and screening answers are read as fact too, so their numbers and the names
    # they drop must come from the same places a bullet's do. Naming this job and yourself is always fine.
    prose = {"summary": data.summary, "cover_letter": data.cover_letter,
             **{f"screening_answer_drafts.{key}": value for key, value in data.screening_answer_drafts.items()}}
    texts = {**prose, **{f"experience_sections.{i}": " ".join(b.text for b in s.bullets)
                         for i, s in enumerate(data.experience_sections)},
             "resume_bullets": " ".join(b.text for b in data.resume_bullets)}
    issues += _prose_issues({} if legacy else prose, texts, supported=supported, job=job, profile=profile,
                            supported_numbers=_numbers(master_text + " " + evidence_text))
    return issues


def _prose_issues(prose: dict[str, str], authorization_texts: dict[str, str], *, supported: set[str], job: Job,
                  profile: CandidateProfile, supported_numbers: set[str]) -> list[dict[str, str]]:
    """Numbers and named terms in prose must come from the resume, the profile or verified evidence; a work
    authorization claim anywhere must match the profile's own."""
    issues: list[dict[str, str]] = []
    allowed = supported | _words(f"{job.company} {job.title} {profile.name}")
    for location, text in prose.items():
        invented = sorted(_numbers(text) - supported_numbers)
        if invented:
            issues.append(_issue("INVENTED_METRIC", location,
                                 f"Numbers {invented} are not in the master resume or your verified evidence"))
        unsupported = sorted(term for term in _named_terms(text) if not _term_supported(term, allowed))
        if unsupported:
            issues.append(_issue("UNSUPPORTED_TERM", location,
                                 f"{unsupported} appear nowhere in the master resume, your profile or verified "
                                 "evidence; cite them there first or leave them out"))
    authorization = (profile.work_authorization or "").lower()
    for location, text in authorization_texts.items():
        for match in {m.group(0).lower() for m in WORK_AUTH_RE.finditer(text or "")}:
            stem = match[:5]
            if not authorization or stem not in authorization:
                issues.append(_issue("WORK_AUTHORIZATION", location,
                                     f"'{match}' is not supported by the work authorization in your profile"))
    return issues


def check_prose(db: Session, profile: CandidateProfile, job: Job, texts: dict[str, str]) -> list[dict[str, str]]:
    """The checks a tailored resume's screening answers get, for drafts written elsewhere (application-form drafts on
    Job Insights): numbers and named terms from the job's resume, the profile or verified evidence only, and no work
    authorization claim the profile does not make."""
    from app.services import resume_verification as rv

    profile = rv.for_job(db, profile, job)
    master_text = to_plain(profile.master_resume or "")
    evidence_text = _evidence_text(verified_evidence(db, job))
    supported = _vocabulary(master_text, evidence_text, profile)
    return _prose_issues(texts, texts, supported=supported, job=job, profile=profile,
                         supported_numbers=_numbers(master_text + " " + evidence_text))


def to_variant_content(data: TailoredResumeIn, *, source: str) -> dict[str, Any]:
    """The existing ResumeVariant.content shape, so the Tailor screen, edits and exports work unchanged."""
    bullet_evidence = [{"role_index": s.role_index, "text": b.text, "evidence_ids": b.evidence_ids}
                       for s in data.experience_sections for b in s.bullets]
    bullet_evidence += [{"role_index": None, "text": b.text, "evidence_ids": b.evidence_ids} for b in data.resume_bullets]
    return {
        "summary": data.summary,
        "skills": data.skills_order,
        "experience": [{"role_index": s.role_index, "bullets": [b.text for b in s.bullets]}
                       for s in data.experience_sections],
        "experience_bullets": [b.text for b in data.resume_bullets],
        "selected_evidence_ids": sorted({i for item in bullet_evidence for i in item["evidence_ids"]}),
        "cover_letter": data.cover_letter,
        "screening_answer_drafts": data.screening_answer_drafts,
        "bullet_evidence": bullet_evidence,
        "source": source,
    }


def from_variant_content(job_id: int, content: dict[str, Any]) -> tuple[TailoredResumeIn, bool]:
    """Rebuild the checked shape from stored content; True when the content predates per-bullet evidence."""
    content = content or {}
    if "bullet_evidence" in content:
        sections: dict[int, list[dict]] = {}
        loose = []
        for item in content["bullet_evidence"]:
            bullet = {"text": item["text"], "evidence_ids": item.get("evidence_ids") or []}
            if item.get("role_index") is None:
                loose.append(bullet)
            else:
                sections.setdefault(item["role_index"], []).append(bullet)
        legacy = False
    else:
        sections = {}
        for item in content.get("experience") or []:
            if isinstance(item, dict) and isinstance(item.get("role_index"), int):
                sections.setdefault(item["role_index"], []).extend(
                    {"text": str(b), "evidence_ids": []} for b in item.get("bullets") or [] if str(b).strip())
        loose = [{"text": str(b), "evidence_ids": []} for b in content.get("experience_bullets") or [] if str(b).strip()]
        legacy = True
    data = TailoredResumeIn.model_construct(
        job_id=job_id, summary=str(content.get("summary") or ""),
        skills_order=[str(s) for s in content.get("skills") or []],
        experience_sections=[ExperienceSectionIn.model_construct(
            role_index=index, bullets=[BulletIn.model_construct(**b) for b in bullets])
            for index, bullets in sorted(sections.items())],
        resume_bullets=[BulletIn.model_construct(**b) for b in loose],
        cover_letter=str(content.get("cover_letter") or ""),
        screening_answer_drafts=dict(content.get("screening_answer_drafts") or {}))
    return data, legacy


# ───────────── the backend Tailor's patch ─────────────

class TailoringRejected(ValueError):
    """Every unit the Tailor generated failed evidence or policy validation: nothing is stored."""

    def __init__(self, issues: list[dict[str, str]]):
        self.issues = issues
        summary = "; ".join(f"{i['code']} at {i['location']}" for i in issues[:4])
        super().__init__(f"TAILOR_OUTPUT_UNSUPPORTED: no generated claim passed evidence validation ({summary})")


def _unit_of(location: str) -> tuple[str, ...]:
    """The patch unit an issue location names: ('summary',), ('role', 2), ('bullet', 2, 0), ('highlight', 1) ..."""
    parts = location.split(".")
    if parts[0] == "experience_sections" and len(parts) >= 4 and parts[2] == "bullets":
        return ("bullet", int(parts[1]), int(parts[3]))
    if parts[0] == "experience_sections" and len(parts) >= 2 and parts[1].isdigit():
        return ("role", int(parts[1]))
    if parts[0] == "resume_bullets" and len(parts) >= 2 and parts[1].isdigit():
        return ("highlight", int(parts[1]))
    return (parts[0],)


def enforce_length(sections: list[Any], length_plan: dict[str, Any] | None) -> tuple[list[Any], list[dict[str, str]]]:
    """Experience sections cut to each role's bullet budget (app.services.resume_length): the most relevant bullets
    (the first ones) are kept, the rest dropped with a LENGTH issue, and a role whose budget is 0 keeps no bullets.
    Nothing is padded. Without a plan the sections are returned unchanged."""
    caps = (length_plan or {}).get("role_caps")
    if not caps:
        return sections, []
    kept, issues = [], []
    for index, section in enumerate(sections):
        cap = caps.get(str(section.role_index))
        if cap is None or len(section.bullets) <= cap:
            kept.append(section)
            continue
        issues.append(_issue("LENGTH", f"experience_sections.{index}",
                             f"Role {section.role_index} allows {cap} bullet{'' if cap == 1 else 's'} "
                             f"({len(section.bullets) - cap} dropped)" if cap else
                             f"Role {section.role_index} shows its heading only in this length plan"))
        if cap:
            kept.append(type(section).model_construct(role_index=section.role_index, bullets=section.bullets[:cap]))
    return kept, issues


def apply_tailoring_patch(db: Session, profile: CandidateProfile, job: Job, patch: Any, *,
                          allowed_evidence: set[int], length_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """Source resume + TailoringResult patch → the stored tailored content, deterministically.

    The patch is checked unit by unit (validate_tailored_resume, plus the evidence the context supplied). A unit
    with an issue is dropped and the source text stays in its place: nothing is repaired by guessing, and the
    dropped units and their reasons are kept with the content. Titles, employers and dates come from the master
    resume at render time, so a patch cannot change them. Raises TailoringRejected when no unit survives."""
    sections = [ExperienceSectionIn.model_construct(role_index=u.role_index, bullets=[
        BulletIn.model_construct(text=b.text, evidence_ids=list(b.evidence_ids)) for b in u.bullets])
        for u in patch.role_updates]
    highlights = [BulletIn.model_construct(text=h.text, evidence_ids=list(h.evidence_ids)) for h in patch.highlights]
    summary = patch.summary.text if patch.summary else ""
    data = TailoredResumeIn.model_construct(job_id=job.id, summary=summary, skills_order=list(patch.skills_order),
                                            experience_sections=sections, resume_bullets=highlights, cover_letter="",
                                            screening_answer_drafts={})
    issues = [i for i in validate_tailored_resume(db, profile, job, data) if i["code"] != "EMPTY"]
    # Evidence the tailoring context did not offer (for example evidence held back for a provenance conflict).
    for s_index, section in enumerate(sections):
        for b_index, bullet in enumerate(section.bullets):
            outside = sorted(set(bullet.evidence_ids) - allowed_evidence)
            if outside:
                issues.append(_issue("EVIDENCE_NOT_IN_CONTEXT", f"experience_sections.{s_index}.bullets.{b_index}",
                                     f"Evidence {outside} was not part of this job's tailoring context"))
    if patch.summary and set(patch.summary.evidence_ids) - allowed_evidence:
        issues.append(_issue("EVIDENCE_NOT_IN_CONTEXT", "summary", "The summary cites evidence outside the context"))

    dropped = {_unit_of(i["location"]) for i in issues}
    bad_skills = {i.get("value") for i in issues if i["code"] == "INVENTED_SKILL"}
    kept_sections = []
    for s_index, section in enumerate(sections):
        if ("role", s_index) in dropped or ("experience_sections",) in dropped:
            continue
        bullets = [b for b_index, b in enumerate(section.bullets) if ("bullet", s_index, b_index) not in dropped]
        if bullets:
            kept_sections.append(ExperienceSectionIn.model_construct(role_index=section.role_index, bullets=bullets))
    kept_sections, length_issues = enforce_length(kept_sections, length_plan)
    issues += length_issues
    kept_highlights = [] if ("resume_bullets",) in dropped else [
        h for index, h in enumerate(highlights) if ("highlight", index) not in dropped]
    kept_summary = "" if ("summary",) in dropped else summary
    kept_skills = [skill for skill in data.skills_order if skill not in bad_skills]
    if not (kept_sections or kept_highlights or kept_summary or kept_skills):
        raise TailoringRejected(issues)
    kept = TailoredResumeIn.model_construct(job_id=job.id, summary=kept_summary, skills_order=kept_skills,
                                            experience_sections=kept_sections, resume_bullets=kept_highlights,
                                            cover_letter="", screening_answer_drafts={})
    content = to_variant_content(kept, source="backend")
    if kept_summary and patch.summary:
        content["summary_evidence_ids"] = list(patch.summary.evidence_ids)
        content["selected_evidence_ids"] = sorted(set(content["selected_evidence_ids"]) | set(patch.summary.evidence_ids))
    content["validation"] = {"issues": issues[:30], "dropped_units": len(dropped)}
    if length_plan:
        content["length_plan"] = length_plan
    return content
