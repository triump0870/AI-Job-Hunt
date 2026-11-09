"""Tailoring, review, approval and follow-up workflows.

Every model call here follows one path, and no model output reaches the database directly:

    context builder (app.services.context_builder) → prompt (app.prompts) → LLM gateway (app.services.llm)
    → output contract → evidence and policy validation → persistence → a state change made here, in Python

Models recommend (a review's PASS / RETAILOR / SKIP); this module and app.services.review_results decide.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import (
    Application,
    ApplicationEvent,
    CandidateProfile,
    Evidence,
    FollowUp,
    Job,
    ResumeVariant,
)
from app.prompts import (
    build_cover_letter_prompt,
    build_followup_prompt,
    build_review_prompt,
    build_tailoring_prompt,
)
from app.services import prompt_budget
from app.services import resume_verification as rv
from app.services.contacts import recruiters_of
from app.services.context_builder import (
    ContextError,
    build_cover_letter_context,
    build_followup_context,
    build_review_context,
    build_tailoring_context,
    fit_prompt,
)
from app.services.llm import LLMCall, LLMClient, LLMResult
from app.services.resume_format import (
    ParsedResume,
    parse_resume,
    relevant_skills,
    render_legacy,
    render_resume,
    skills_text,
    strip_citations,
    summary_text,
    to_plain,
)
from app.services.user_settings import profile_for, settings_for
from app.settings import get_settings

log = logging.getLogger("jobhunt.workflow")


def followup_days(db: Session) -> int:
    prefs = settings_for(db).preferences or {}
    return int(prefs.get("followup_days") or get_settings().followup_days)


def get_profile(db: Session) -> CandidateProfile:
    """The candidate profile of the user this session belongs to (app.tenancy)."""
    return profile_for(db)


# Called with the stage a tailoring run is entering ("tailoring", "reviewing") so background tasks can report progress.
Progress = Callable[[str], None]


def _no_progress(_stage: str) -> None:
    return None


def generation_provenance(prompt_version: str, result: LLMResult) -> dict:
    """What produced a stored output: prompt version, provider, the model that answered and the AI config version."""
    routing = result.routing or {}
    return {"prompt_version": prompt_version, "provider": result.provider, "model": result.model,
            "config_version": routing.get("config_version"), "escalated": bool(routing.get("escalated"))}


def tailor_job(db: Session, job: Job, progress: Progress = _no_progress, pages: int | None = None,
               feedback: dict | None = None) -> ResumeVariant:
    """Tailor from verified facts and the Ranker's completed fit analysis. Ranking never runs here: it is the
    asynchronous ranker's job (app.services.ranker). Without a completed analysis the context builder raises
    ContextError(RANKING_REQUIRED) and no model is called.

    `pages` (1, 2 or 3) is this job's page target; omitted, the account's default (app.services.resume_length).
    `feedback` is a revision pass's previous review (review_results.retailor_feedback): what to fix, never new facts."""
    from app.services import resume_length
    from app.services.tailoring_results import apply_tailoring_patch

    profile = rv.for_job(db, get_profile(db), job)  # the job's chosen resume, else the active one
    plan = resume_length.plan_for(db, job.user_id, pages)
    # Verified facts relevant to this job only; raises UnverifiedResumeError / ContextError before any model call.
    context = build_tailoring_context(db, profile, job, plan)
    if feedback:
        context.revision_feedback = feedback
    progress("tailoring")
    llm = LLMClient.for_user(db)
    # Within the Tailor's prompt token budget, when it has one; the citable ids below are taken after trimming.
    prompt = fit_prompt(context, job, lambda: build_tailoring_prompt(candidate_profile=profile, job=job,
                                                                     context=context),
                        prompt_budget.budget_for(db, job.user_id, "TAILOR"))
    result = llm.generate_prompt(prompt, LLMCall(workflow_type="job", entity_id=job.id,
                                                 context_stats=context.call_stats()))
    # Citable: the evidence list, and the id of every source bullet and project bullet the context showed.
    content = apply_tailoring_patch(db, profile, job, result.validated,
                                    allowed_evidence=context.evidence_ids | context.citable_ids,
                                    length_plan=context.length_plan)
    content.update(generation_provenance(prompt.version, result))
    # A role the Tailor left alone shows its first bullets in this order (most relevant first) up to its budget.
    content["fallback_bullets"] = {str(role["role_index"]): list(role["bullets"]) for role in context.roles}
    content["context"] = {**context.resume, **context.stats.as_dict(), "evidence_ids": sorted(context.evidence_ids),
                          "context_builder_version": context.call_stats()["context_builder_version"],
                          "excluded_evidence": context.excluded_evidence,
                          "held_back_facts": [{"id": f["id"], "level": f["level"]} for f in context.held_back_facts],
                          "fit_analysis": {"ranking_id": context.fit_analysis.get("ranking_id"),
                                           "ranking_version": context.fit_analysis.get("ranking_version")},
                          "allow_unverified_resume_facts": context.policy.get("allow_unverified_resume_facts", False)}
    content["keyword_coverage"] = supported_keyword_coverage(context.keyword_targets,
                                                             build_resume_markdown(profile, content))
    content["cover_letter"], content["cover_letter_generation"] = write_cover_letter(db, profile, job, llm,
                                                                                     tailored=content)
    # A last check before anything is saved: a run stopped from Running workers ends here, saving nothing.
    progress("tailoring")
    resume = persist_tailored_resume(db, job, content)
    progress("reviewing")
    review_resume(db, resume)
    return resume


def supported_keyword_coverage(targets: dict, markdown: str) -> dict:
    """Which posting keywords the candidate's facts support the tailored resume still leaves out. Recorded with the
    resume and shown beside the ATS score; it never triggers another model call."""
    from app.services import ats_keywords

    text = ats_keywords.normalize((markdown or "").split("## Source Resume")[0])
    use = [item["keyword"] for item in (targets or {}).get("use") or []]
    missing = [word for word in use if not ats_keywords._contains(text, ats_keywords.normalize(word))]
    return {"supported": use, "missing_supported": missing,
            "score": round(100 * (len(use) - len(missing)) / len(use)) if use else None}


def tailored_highlights(content: dict | None) -> dict:
    """The validated tailored resume a letter is written beside, as it renders: its summary (when the Tailor wrote
    one) and the first role's bullets, the Tailor's own or, for a role it left alone, the source bullets the
    resume shows in its place (`fallback_bullets`, as build_resume_markdown renders them)."""
    if not content:
        return {}
    patched = {s.get("role_index"): s.get("bullets") or [] for s in content.get("experience") or []}
    fallback = {int(k): v for k, v in (content.get("fallback_bullets") or {}).items()}
    roles = sorted(set(patched) | set(fallback), key=lambda i: -1 if i is None else i)
    first = roles[0] if roles else None
    bullets = (patched.get(first) or fallback.get(first)) if roles else content.get("experience_bullets")
    bullets = [str(b) for b in bullets or [] if str(b).strip()]
    return {k: v for k, v in {"summary": content.get("summary"), "top_bullets": bullets[:5]}.items() if v}


def write_cover_letter(db: Session, profile: CandidateProfile, job: Job, llm: LLMClient,
                       tailored: dict | None = None) -> tuple[str, dict]:
    """A letter in the candidate's own voice, from its own prompt and context (app.prompts.cover_letter): the
    posting and its company facts, the verified proof points that fit it, and the answers the posting raises.
    `tailored` is the validated tailored content the letter is written beside, so both tell the same story."""
    context = build_cover_letter_context(db, profile, job, tailored=tailored_highlights(tailored))
    prompt = build_cover_letter_prompt(context, recruiters=recruiters_of(job.application) if job.application else [])
    result = llm.generate_prompt(prompt, LLMCall(workflow_type="job", entity_id=job.id,
                                                 context_stats=context.call_stats()))
    from app.services.tailoring_results import (
        TailoredResumeIn,
        validate_tailored_resume,
        verified_evidence,
    )

    letter = strip_citations(to_plain(result.validated.cover_letter)).strip()
    # The same deterministic checks as a letter saved over MCP (invented numbers, names nothing supports, work
    # authorization the profile does not state). A letter is one unit a person approves before it is sent, so its
    # issues are recorded with it for that review rather than silently cutting sentences out of it.
    issues = [issue for issue in validate_tailored_resume(db, profile, job, TailoredResumeIn.model_construct(
        job_id=job.id, summary="", skills_order=[], experience_sections=[], resume_bullets=[], cover_letter=letter,
        screening_answer_drafts={})) if issue["location"] == "cover_letter"]
    known = verified_evidence(db, job)
    return letter, {**generation_provenance(prompt.version, result), "validation_issues": issues,
                    "evidence_ids": [i for i in result.validated.selected_evidence_ids if i in known]}


def persist_tailored_resume(db: Session, job: Job, content: dict, *, markdown: str | None = None) -> ResumeVariant:
    """Store a new tailored version for a job. Every tailoring path (backend worker, Claude Desktop over MCP,
    the ADK agent) saves through here. The master resume is only read, never written."""
    # A pass that was running when a person approved the packet must not save over that decision: the approved
    # version stays the newest one and the job stays approved.
    decided = db.scalar(select(Application.status).where(Application.job_id == job.id))
    if decided in ("approved", "applied"):
        raise ValueError(f"This job was {decided} while it was being tailored; send it back for revision to tailor "
                         "it again")
    # Evidence ids are kept in the structured content; the letter itself is what a recruiter reads.
    content = {**content, "cover_letter": strip_citations(content.get("cover_letter", "") or "").strip()}
    version = (db.scalar(select(ResumeVariant.version).where(ResumeVariant.job_id == job.id)
                         .order_by(desc(ResumeVariant.version)).limit(1)) or 0) + 1
    profile = rv.for_job(db, get_profile(db), job)
    # Which resume version the packet was made from, so the Tailor screen can name it. The backend Tailor records it
    # in its context; every other path (Claude Desktop over MCP, skip tailoring) records the job's resolved resume.
    if not (content.get("context") or {}).get("id") and not content.get("source_resume_id"):
        source = rv.resume_for(db, profile, create=False) if profile is not None else None
        if source is not None:
            content["source_resume_id"] = source.id
    resume = ResumeVariant(job_id=job.id, version=version, content=content,
                           markdown=build_resume_markdown(profile, content) if markdown is None else markdown,
                           cover_letter=content.get("cover_letter", "") or "")
    db.add(resume)
    job.status = "tailored"
    db.commit()
    db.refresh(resume)
    return resume


# A packet that review sent back to be tailored again. It keeps its old version until a new one is saved,
# and is distinct from "shortlisted", which means nothing has been tailored yet.
NEEDS_RETAILOR = "needs_retailor"
# Jobs that are in the Tailor queue and have not been approved or submitted yet.
SKIP_TAILORING_STATUSES = ("shortlisted", NEEDS_RETAILOR, "tailored", "pending_approval")
# Of those, the ones still waiting for a (new) tailored packet.
AWAITING_TAILORING_STATUSES = ("shortlisted", NEEDS_RETAILOR)


def skip_tailoring(db: Session, job: Job, progress: Progress = _no_progress) -> ResumeVariant:
    """Use the master resume unchanged for a job that needs no tailoring. No AI edits; the reviewer still scores it."""
    profile = rv.for_job(db, get_profile(db), job)
    if not (profile.master_resume or "").strip():
        if isinstance(profile, rv.JobProfile) and profile.resume_source == "structured":
            raise ValueError("This resume's structured data is empty: review it in Settings → Resumes, or switch "
                             "the resume source to the resume text")
        raise ValueError("Add a master resume in Settings before skipping tailoring")
    markdown = render_resume(parse_resume(profile.master_resume), name=profile.name, headline=profile.headline)
    resume = persist_tailored_resume(db, job, {"untailored": True, "summary": "", "skills": [], "experience": [],
                                               "selected_evidence_ids": [], "cover_letter": ""}, markdown=markdown)
    progress("reviewing")
    review_resume(db, resume)
    return resume


def review_packet(db: Session, job: Job, progress: Progress = _no_progress) -> ResumeVariant:
    """Review the latest saved packet of a job, without tailoring it again: for a packet whose review never ran or
    failed (tailoring saves the version before the Reviewer runs). No new version is made."""
    resume = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job.id)
                       .order_by(desc(ResumeVariant.version)).limit(1))
    if resume is None:
        raise ValueError("This job has no packet to review")
    progress("reviewing")
    review_resume(db, resume)
    return resume


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9+#.]{3,}", (text or "").lower())}


def _best_match(text: str, candidates: list[str]) -> tuple[str, float]:
    target = _tokens(text)
    best, best_score = "", 0.0
    for candidate in candidates:
        words = _tokens(candidate)
        if not target or not words:
            continue
        score = len(target & words) / len(target)
        if score > best_score:
            best, best_score = candidate, score
    return best, best_score


NEW_BULLET = "New bullet - not a rewrite of an existing line"


def _role_bullets(parsed: ParsedResume, content: dict) -> dict[int, list[str]]:
    """Validated per-role bullets from the tailoring output; unknown roles are ignored."""
    out: dict[int, list[str]] = {}
    for item in content.get("experience") or []:
        if not isinstance(item, dict):
            continue
        index = item.get("role_index")
        bullets = [str(b).strip() for b in item.get("bullets") or [] if str(b).strip()]
        if isinstance(index, int) and 0 <= index < len(parsed.roles) and bullets:
            out.setdefault(index, []).extend(bullets)
    return out


def _resume_view(profile: CandidateProfile | rv.JobProfile) -> CandidateProfile | rv.JobProfile:
    """A profile passed without its view (no job at hand) reads the active resume in the policy's form."""
    from sqlalchemy.orm import object_session

    if isinstance(profile, rv.JobProfile):
        return profile
    db = object_session(profile)
    return rv.selected(db, profile) if db is not None else profile


def edit_slots(profile: CandidateProfile, content: dict) -> tuple[ParsedResume, list[dict]]:
    """The reviewable units of a tailored resume. Edits and the rebuilt Markdown share this order. "Before" is the
    selected resume's document (its structured data, or its text as written: resume_verification.selected)."""
    profile = _resume_view(profile)
    parsed = parse_resume(profile.master_resume or "")
    primary = (profile.search_preferences or {}).get("primary_skills") or []
    slots = [
        {"section": "SUMMARY", "before": summary_text(parsed) or profile.headline or "", "after": content.get("summary", "")},
        {"section": "SKILLS", "before": skills_text(parsed) or ", ".join(primary) or "No skills listed on your profile",
         "after": ", ".join(str(s) for s in content.get("skills") or [])},
    ]
    roles = _role_bullets(parsed, content) if parsed.structured else {}
    if roles:
        for role_index, bullets in sorted(roles.items()):
            role = parsed.roles[role_index]
            for bullet in bullets:
                before, score = _best_match(bullet, role.bullets)
                slots.append({"section": " · ".join(filter(None, ["EXPERIENCE", role.title, role.company])),
                              "before": before if score >= 0.2 else NEW_BULLET, "after": bullet,
                              "role_index": role_index, "restore": before if score >= 0.2 else ""})
    else:
        master_lines = [line.strip(" -\t") for line in to_plain(profile.master_resume or "").splitlines() if len(line.strip()) > 20]
        section = "HIGHLIGHTS" if parsed.structured else "EXPERIENCE"
        for bullet in content.get("experience_bullets") or []:
            before, score = _best_match(bullet, master_lines)
            slots.append({"section": section, "before": before if score >= 0.2 else NEW_BULLET, "after": str(bullet),
                          "role_index": None, "restore": ""})
    for index, slot in enumerate(slots):
        slot["index"] = index
    return parsed, slots


def resume_edits(db: Session, resume: ResumeVariant) -> list[dict]:
    """Present a tailored resume as reviewable edits: before (master resume) → after (tailored)."""
    content = resume.content or {}
    if content.get("untailored"):
        return []  # the master resume is used as-is: nothing was rewritten
    profile = rv.for_job(db, get_profile(db), db.get(Job, resume.job_id))
    state = content.get("edit_state") or {}
    evidence = db.scalars(select(Evidence).where(Evidence.verified.is_(True))).all()
    selected = set(content.get("selected_evidence_ids") or [])
    claims = {e.id: e for e in evidence}

    cited_by_text = {item["text"]: item.get("evidence_ids") or [] for item in content.get("bullet_evidence") or []}

    def cite(text: str) -> str:
        own = [claims[i] for i in cited_by_text.get(text, []) if i in claims]
        if own:  # the bullet recorded its own evidence: show that, not a best guess
            return f"{own[0].company or 'verified'} · {own[0].claim[:80]}"
        pool = [claims[i] for i in selected if i in claims] or list(claims.values())
        match, score = _best_match(text, [f"{e.claim} {' '.join(e.skills or [])}" for e in pool])
        if score < 0.15:
            return ""
        item = next(e for e in pool if f"{e.claim} {' '.join(e.skills or [])}" == match)
        return f"{item.company or 'verified'} · {item.claim[:80]}"

    edits = []
    for slot in edit_slots(profile, content)[1]:
        saved = state.get(str(slot["index"])) or {}
        text = saved.get("text") or slot["after"]
        if slot["section"] == "SKILLS":
            evidence_label = "master resume skills"
        else:
            evidence_label = cite(text) or (f"master resume · {slot['restore'][:70]}" if slot.get("restore") else "")
        edits.append({"index": slot["index"], "section": slot["section"], "before": slot["before"], "after": text,
                      "evidence": evidence_label, "status": saved.get("status") or ("accepted" if evidence_label else "review"),
                      "edited": bool(saved.get("text"))})
    return edits


def build_resume_markdown(profile: CandidateProfile, content: dict) -> str:
    """Rebuild the full resume: master structure, tailored wording, never the raw source text."""
    parsed, slots = edit_slots(profile, content)
    state = content.get("edit_state") or {}

    def resolved(slot: dict) -> str | None:
        saved = state.get(str(slot["index"])) or {}
        if saved.get("status") == "reverted":
            return None
        return saved.get("text") or slot["after"]

    summary, skills_value = resolved(slots[0]), resolved(slots[1])
    skill_list = [s.strip() for s in (skills_value or "").split(",") if s.strip()]
    if not parsed.structured:
        bullets = [value for value in (resolved(s) for s in slots[2:]) if value]
        return render_legacy(profile.name, profile.headline, summary or profile.headline or "", skill_list, bullets)
    role_bullets: dict[int, list[str]] = {}
    highlights: list[str] = []
    for slot in slots[2:]:
        value = resolved(slot)
        if slot["role_index"] is None:
            if value:
                highlights.append(value)
            continue
        value = value if value is not None else slot["restore"]  # a reverted rewrite restores its master bullet
        bucket = role_bullets.setdefault(slot["role_index"], [])
        if value and value not in bucket:
            bucket.append(value)
    caps, fallback = length_caps(content)
    return render_resume(parsed, name=profile.name, headline=profile.headline, summary=summary or None,
                         top_skills=relevant_skills(parsed, skill_list), role_bullets=role_bullets, highlights=highlights,
                         role_caps=caps, fallback_bullets=fallback)


def length_caps(content: dict) -> tuple[dict[int, int] | None, dict[int, list[str]] | None]:
    """A tailored resume's bullet budget per role_index and the bullets a role it left alone falls back to (most
    relevant first), from content["length_plan"] (app.services.resume_length). (None, None) for packets made
    before length plans: they render exactly as they always did."""
    plan = content.get("length_plan") or {}
    if not plan.get("role_caps"):
        return None, None
    caps = {int(k): int(v) for k, v in plan["role_caps"].items()}
    fallback = {int(k): list(v) for k, v in (content.get("fallback_bullets") or {}).items()}
    return caps, fallback


def update_resume_edit(db: Session, resume: ResumeVariant, index: int, status: str | None, text: str | None) -> None:
    from app.services import approval

    approval.guard_edit(db, resume, "The resume")  # an approved packet loses its approval with this edit
    content = dict(resume.content or {})
    state = dict(content.get("edit_state") or {})
    entry = dict(state.get(str(index)) or {})
    if status:
        entry["status"] = status
    if text is not None:
        entry["text"] = text.strip()
        entry.setdefault("status", "accepted")
    state[str(index)] = entry
    content["edit_state"] = state
    resume.content = content
    resume.markdown = build_resume_markdown(rv.for_job(db, get_profile(db), db.get(Job, resume.job_id)), content)
    db.commit()


def tailored_claims(content: dict) -> list[dict]:
    """The tailored units with the evidence each cites, for the Reviewer to check one by one."""
    claims = [{"section": "summary", "text": content["summary"], "evidence_ids": content.get("summary_evidence_ids", [])}
              ] if content.get("summary") else []
    return claims + [{"section": "experience" if item.get("role_index") is not None else "highlights",
                      "text": item["text"], "evidence_ids": item.get("evidence_ids") or []}
                     for item in content.get("bullet_evidence") or []]


def review_resume(db: Session, resume: ResumeVariant) -> dict:
    """The Reviewer checks an already-tailored version. It recommends; it never rewrites the resume or moves the job."""
    from app.services.review_results import normalize_backend_review

    job = db.get(Job, resume.job_id)
    if job is None or not (resume.markdown or "").strip():
        raise ContextError("REVIEW_CONTEXT_INCOMPLETE", "there is no tailored resume text to review")
    llm = LLMClient.for_user(db)
    profile = rv.for_job(db, get_profile(db), job)
    context = build_review_context(db, profile, job)  # verified structured truth to check the text against
    prompt = build_review_prompt(candidate_profile=profile, job=job, tailored_resume=resume.markdown, context=context,
                                 tailored_claims=tailored_claims(resume.content or {}))
    result = llm.generate_prompt(prompt, LLMCall(workflow_type="job", entity_id=resume.job_id,
                                                 context_stats=context.call_stats()))
    notes = {**normalize_backend_review(result.validated.model_dump()), **generation_provenance(prompt.version, result)}
    store_review(db, resume, notes)
    return notes


def store_review(db: Session, resume: ResumeVariant, notes: dict, *, commit: bool = True) -> None:
    """Persist a review on a tailored version. Backend and Claude Desktop reviews are stored the same way."""
    resume.reviewer_score = float(notes.get("review_score", notes.get("score", 0)) or 0)
    resume.reviewer_notes = notes
    if commit:
        db.commit()


def approve_job(db: Session, job: Job, *, override: bool = False, note: str = "", by_person: bool = False) -> Application:
    """Approve the latest reviewed packet. `override` records that the reviewer minimum was waived for it.
    `by_person`: the candidate approved it on a screen, which may overrule the ranking policy's archive
    (approval.path_to_ready); automatic approvals never pass it."""
    from app.services import approval

    latest = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job.id)
                       .order_by(desc(ResumeVariant.version)).limit(1))
    if not latest or latest.reviewer_score is None:
        raise ValueError("The latest resume must be reviewed before approval")
    # The canonical state goes to READY_TO_APPLY through validated transitions, or the approval is refused before
    # anything is written (approval.ApprovalRefused).
    approval.reconcile_to_ready(db, job, latest, by_person=by_person)
    application = job.application
    if not application:
        application = Application(job_id=job.id)
        db.add(application)
    application.status = "approved"
    application.approved_at = datetime.now(UTC).replace(tzinfo=None)
    application.approved_resume_id = latest.id
    application.approval_override = bool(override)
    application.approval_note = (note or "").strip()[:2000] or None
    job.status = "approved"
    application.job = job
    # Exactly what was approved, and its digest: export, packet issuance and application check against it.
    approval.snapshot(db, application, latest, override=override)
    if override:
        # The tracker timeline should say the minimum was waived, and why, long after the decision.
        db.add(ApplicationEvent(application=application, kind="approval", title="Approved below the reviewer minimum",
                                detail=(note or "").strip()[:2000] or f"Reviewer scored {latest.reviewer_score:.0f}"))
    db.flush()
    # Job Insights research starts in the background; approval never waits for it and never fails because of it.
    from app.services.job_insights import tasks as job_insights

    job_insights.on_approved(db, job)
    from app.services import preparation

    preparation.on_decision(db, job, "approved")  # a preparation graph run waiting for this ends (never blocks)
    from app.services import application_execution

    application_execution.on_approved(db, job)  # auto submit, when the account turned it on (never blocks)
    db.commit()
    db.refresh(application)
    return application


# Where a submitted job's canonical state already is, or has moved past: nothing to advance.
_SUBMITTED_STATES = ("APPLIED", "FOLLOWUP_DUE", "FOLLOWED_UP", "INTERVIEW", "REJECTED", "OFFER")


def advance_to_applied(db: Session, job: Job, *, actor: str, reason: str) -> str | None:
    """Move the canonical state READY_TO_APPLY → APPLYING → APPLIED through validated transitions (work_queue),
    reconciling an older approval to READY_TO_APPLY first where the stored facts back it (approval.reconcile_to_ready).
    Never forges a step: when no backed path exists the state stays where it is and the reason is returned for the
    audit trail. Does not commit."""
    from app.services import approval
    from app.services.lifecycle import InvalidTransition, parse_state
    from app.services.lifecycle import WorkflowState as S
    from app.services.work_queue import transition

    state = parse_state(job.state) if job.state else None
    if state is not None and state.value in _SUBMITTED_STATES:
        return None
    try:
        if state not in (S.READY_TO_APPLY, S.APPLYING):
            variant = db.get(ResumeVariant, job.application.approved_resume_id) if job.application and \
                job.application.approved_resume_id else None
            if variant is None:
                return f"the canonical state stayed {state.value if state else 'unset'}: no approved version"
            approval.reconcile_to_ready(db, job, variant)
        if parse_state(job.state) is S.READY_TO_APPLY:
            transition(db, job, S.APPLYING, actor=actor, reason=reason)
        transition(db, job, S.APPLIED, actor=actor, reason=reason)
    except (approval.ApprovalRefused, InvalidTransition) as exc:
        return f"the canonical state stayed {job.state}: {exc}"[:300]
    return None


def mark_applied(db: Session, job: Job, *, submitted_by: str = "you", submitted_via: str | None = None,
                 confirmation_id: str | None = None, claim: dict | None = None,
                 evidence: dict | None = None) -> Application:
    """Record the submission on the application and the job, and move the canonical state to APPLIED (through
    APPLYING). Callers go through apply_queue.record_submission, which takes the account lock and checks the packet,
    duplicates and claims first; `claim` is its provenance and `evidence` says how the submission is known:
    `verified_browser` (the confirmation verifier saw the site's evidence) or `reported` (a person or Claude Desktop
    said so). A report is never treated as verified."""
    application = job.application or approve_job(db, job)
    if application.status != "approved":
        raise ValueError("Application must be approved before marking applied")
    provenance = {"evidence": (evidence or {}).get("kind") or "reported",
                  **({"verification": evidence.get("facts")} if evidence and evidence.get("facts") else {})}
    not_advanced = advance_to_applied(db, job, actor=(submitted_via or "person")[:60],
                                      reason=f"Submitted by {submitted_by} ({provenance['evidence']})")
    applied_at = datetime.now(UTC).replace(tzinfo=None)
    application.status = "applied"
    application.stage = "applied"
    application.applied_at = applied_at
    application.followup_due_at = applied_at + timedelta(days=followup_days(db))
    application.submitted_via = submitted_via
    application.confirmation_id = confirmation_id or None
    application.apply_blocker = None
    job.status = "applied"
    detail = f"Submitted by {submitted_by} · {job.url or 'no posting URL'}"
    db.add(ApplicationEvent(application=application, kind="applied", title="Applied",
                            detail=detail + (f" · confirmation {confirmation_id}" if confirmation_id else ""),
                            occurred_at=applied_at,
                            meta={**({"confirmation_id": confirmation_id} if confirmation_id else {}),
                                  **(claim or {}), **provenance,
                                  **({"state_not_advanced": not_advanced} if not_advanced else {})}))
    db.commit()
    db.refresh(application)
    return application


def create_due_followups(db: Session) -> int:
    now = datetime.now(UTC).replace(tzinfo=None)
    apps = db.scalars(
        select(Application).where(
            Application.status == "applied",
            Application.followup_due_at.is_not(None),
            Application.followup_due_at <= now,
        )
    ).all()
    from app.services import followups
    from app.services.application_inbox.service import blocks_followup

    created = 0
    for app in apps:
        if not followups.needs_draft(db, app):  # a draft is waiting, or MAX_SENT follow-ups already went out
            continue
        if blocks_followup(db, app):
            # The Application Inbox saw a reply (or the application moved on): a nudge would be noise.
            app.followup_due_at = None
            db.add(ApplicationEvent(application=app, kind="note", title="Follow-up not needed",
                                    detail="The employer already replied, or the application moved beyond Acknowledged, "
                                           "so no follow-up was drafted.",
                                    meta={"source": "application_inbox"}))
            continue
        draft_followup(db, app, commit=False)
        created += 1
    db.commit()
    return created


def draft_followup(db: Session, app: Application, commit: bool = True) -> FollowUp:
    """Generate (or regenerate) the follow-up draft for one application. Never sends anything."""
    now = datetime.now(UTC).replace(tzinfo=None)
    llm = LLMClient.for_user(db)
    job = db.get(Job, app.job_id)
    profile = rv.for_job(db, get_profile(db), job)
    from app.services.followups import previous_texts

    sent = previous_texts(db, app)  # what was really sent; a cancelled draft never went out
    context = build_followup_context(db, profile, job)
    prompt = build_followup_prompt(
        candidate_profile=profile, job=job, context=context,
        application={"status": app.status, "applied_at": app.applied_at.isoformat() if app.applied_at else ""},
        recruiters=recruiters_of(app), previous_followups=[text for text in sent if text])
    result = llm.generate_prompt(prompt, LLMCall(workflow_type="application", entity_id=app.id,
                                                 context_stats=context.call_stats()))
    fu = db.scalar(select(FollowUp).where(FollowUp.application_id == app.id, FollowUp.status == "draft")
                   .order_by(desc(FollowUp.id)).limit(1))
    if not fu:
        fu = FollowUp(application_id=app.id, due_at=app.followup_due_at or now, channel="email", status="draft")
        db.add(fu)
    fu.draft = result.validated.draft
    fu.generation = {**generation_provenance(prompt.version, result), "evidence_ids": result.validated.evidence_ids}
    if commit:
        db.commit()
        db.refresh(fu)
    return fu
