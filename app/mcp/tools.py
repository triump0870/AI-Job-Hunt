"""The Job Hunt OS operations Claude Desktop may call. Each handler takes a session that belongs to one account.

Handlers only read and write through the application services. They take IDs, filters and structured results,
never SQL, file paths, shell input or settings, and they never return API keys or environment values. The only
paths they return are the packet files get_job_packet and get_job_documents write under MCP_PACKET_DIR.
No handler calls a model, and none sends a message: follow-ups are drafted by Claude and sent by the person.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import desc, func, nulls_last, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Application,
    ConversationAction,
    Evidence,
    FollowUp,
    Job,
    JobRanking,
    ResumeVariant,
)
from app.prompts.common import prompt_profile
from app.prompts.mcp_application import APPLICATION_RULES, MCP_APPLICATION_PROMPT_VERSION
from app.prompts.mcp_followup import FOLLOWUP_RULES, MCP_FOLLOWUP_PROMPT_VERSION
from app.prompts.mcp_review import MCP_REVIEW_PROMPT_VERSION, REVIEW_RULES
from app.prompts.mcp_tailoring import HELD_BACK_NOTE, MCP_TAILORING_PROMPT_VERSION, TAILORING_RULES
from app.prompts.tailoring import JOB_FIELDS, fit_analysis_input
from app.services import apply_queue, approval, ats_map, conversation_actions, followups, knowledge
from app.services import resume_verification as rv
from app.services.context_builder import ContextError
from app.services.ranker import policy_for, relevant_evidence
from app.services.resume_format import parse_resume
from app.services.review_results import ReviewIn, apply_review, latest_variant
from app.services.tailor_tasks import packet_outcome
from app.services.tailoring_results import (
    TailoredResumeIn,
    enforce_length,
    schema_errors,
    to_variant_content,
    validate_tailored_resume,
)
from app.services.user_settings import settings_for
from app.services.workflow import (
    AWAITING_TAILORING_STATUSES,
    NEEDS_RETAILOR,
    get_profile,
    persist_tailored_resume,
)
from app.settings import get_settings

DESCRIPTION_LIMIT = 12000


class ToolError(Exception):
    """A request the tool refuses as a whole (bad arguments, batch too large, no resume)."""


# ───────────── shared helpers ─────────────

def tailor_limit() -> int:
    return get_settings().mcp_bulk_tailor_limit


def review_limit() -> int:
    return get_settings().mcp_bulk_review_limit


def _check_limit(limit: int, maximum: int, setting: str) -> int:
    if not isinstance(limit, int) or limit < 1:
        raise ToolError("limit must be a positive integer")
    if limit > maximum:
        raise ToolError(f"limit {limit} is above the maximum of {maximum} ({setting})")
    return limit


def _check_batch(items: Any, maximum: int, setting: str, name: str) -> list:
    if not isinstance(items, list) or not items:
        raise ToolError(f"{name} must be a non-empty list")
    if len(items) > maximum:
        raise ToolError(f"{len(items)} {name} is above the batch maximum of {maximum} ({setting}); "
                        "split the request into smaller batches")
    return items


def _job_ids(job_ids: Any, maximum: int, setting: str) -> list[int]:
    ids = _check_batch(job_ids, maximum, setting, "job_ids")
    if not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise ToolError("job_ids must be a list of integers")
    return list(dict.fromkeys(ids))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _in_tailor_queue(job: Job) -> bool:
    """Never tailored (shortlisted), or tailored and sent back by review (needs_retailor)."""
    return job.status in AWAITING_TAILORING_STATUSES and job.triaged_at is not None


def _latest_ranking(db: Session, job_id: int) -> JobRanking | None:
    return db.scalar(select(JobRanking).where(JobRanking.job_id == job_id).order_by(desc(JobRanking.version)).limit(1))


def _fit_analysis(db: Session, job: Job) -> dict[str, Any]:
    """The Ranker's completed analysis as the tailoring component reads it: present fields only, no scores, never
    an empty template. Raises ContextError(RANKING_REQUIRED) when the job has no completed analysis."""
    from app.services.context_builder import completed_fit_analysis

    return fit_analysis_input(completed_fit_analysis(db, job))


def _evidence_dict(item: Evidence) -> dict[str, Any]:
    return {"id": item.id, "company": item.company, "role": item.role, "claim": item.claim, "context": item.context,
            "skills": item.skills or [], "tags": item.tags or []}


def _evidence_for(db: Session, job: Job, cited: list[int]) -> list[dict[str, Any]]:
    """Only the verified evidence relevant to this job (ranking citations first), never the whole Evidence Bank."""
    relevant = relevant_evidence(db, job.user_id, job, get_settings().ranking_max_evidence)
    known = {item["id"] for item in relevant}
    extra = [i for i in dict.fromkeys(cited) if i not in known]
    rows = db.scalars(select(Evidence).where(Evidence.id.in_(extra), Evidence.verified.is_(True))).all() if extra else []
    first = [_evidence_dict(r) for r in sorted(rows, key=lambda r: extra.index(r.id))]
    return first + relevant


def _posting_targets(db: Session, profile: Any, job: Job, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """What the posting asks for, in the shape the backend Tailor reads it: the parsed requirements (when the
    posting has been parsed), the Ranker's matched requirements with the evidence that proves them, and the ATS
    keywords split into those the candidate's facts support and those that stay gaps."""
    from app.services import job_context as jc
    from app.services.context_builder import keyword_targets, requirement_map

    parsed = jc.build_tailoring_job_context(db, job)
    sources = [{"id": e["id"], "text": e.get("claim") or ""} for e in evidence]
    out = {"job_requirements": {k: v for k, v in parsed.to_dict().items() if k in JOB_FIELDS and v}
           if parsed.structured else {},
           # Only evidence this response carries can be named as proof, and only it can support a keyword.
           "requirement_map": requirement_map(job.fit_analysis, {e["id"] for e in evidence}),
           "keyword_targets": keyword_targets(job, [rv.for_job(db, profile, job).master_resume or "",
                                                   *(" ".join([e.get("claim") or "", *(e.get("skills") or [])])
                                                     for e in evidence)], sources)}
    return {k: v for k, v in out.items() if v}


def _profile_payload(db: Session) -> tuple[Any, dict[str, Any]]:
    profile = get_profile(db)
    return profile, prompt_profile(profile, include_name=True)  # facts about the person, not search settings


def _require_resume(profile: Any) -> None:
    if not (profile.master_resume or "").strip():
        raise ToolError("No active resume: upload or paste a master resume in Job Hunt OS Settings → Resumes first")


def _operation_status(saved: int, total: int) -> str:
    return "OK" if saved == total else "REJECTED" if saved == 0 else "PARTIAL"


# ───────────── tailoring ─────────────

def list_jobs_for_tailoring(db: Session, minimum_fit_score: float | None = None, posted_after: str | None = None,
                            location: str | None = None, company: str | None = None, limit: int = 10) -> dict[str, Any]:
    limit = _check_limit(limit, tailor_limit(), "MCP_BULK_TAILOR_LIMIT")
    query = select(Job).where(Job.status.in_(AWAITING_TAILORING_STATUSES), Job.triaged_at.is_not(None))
    if minimum_fit_score is not None:
        query = query.where(Job.fit_score >= float(minimum_fit_score))
    if posted_after:
        try:
            query = query.where(Job.posted_at >= datetime.fromisoformat(posted_after))
        except ValueError as exc:
            raise ToolError("posted_after must be an ISO date such as 2026-09-01") from exc
    if location:
        query = query.where(Job.location.ilike(f"%{location.strip()}%"))
    if company:
        query = query.where(Job.company.ilike(f"%{company.strip()}%"))
    jobs = db.scalars(query.order_by(nulls_last(desc(Job.fit_score)), Job.id).limit(limit)).all()
    out = []
    for job in jobs:
        ranking = _latest_ranking(db, job.id)
        out.append({"job_id": job.id, "title": job.title, "company": job.company, "location": job.location,
                    "fit_score": job.fit_score, "resume_fit_score": ranking.resume_fit_score if ranking else None,
                    "posted_at": _iso(job.posted_at), "status": job.status,
                    "needs_retailor": job.status == NEEDS_RETAILOR})
    return {"ok": True, "count": len(out), "jobs": out}


def get_tailoring_context(db: Session, job_ids: list[int]) -> dict[str, Any]:
    ids = _job_ids(job_ids, tailor_limit(), "MCP_BULK_TAILOR_LIMIT")
    profile, facts = _profile_payload(db)
    active = rv.selected(db, profile)  # the active resume, in the form the candidate policy's resume source names
    _require_resume(active)
    parsed, plan = parse_resume(active.master_resume), _length_plan(db, active)
    jobs = []
    for job_id in ids:
        job = db.get(Job, job_id)
        if job is None:
            jobs.append({"job_id": job_id, "error": "Job not found"})
            continue
        if not _in_tailor_queue(job):
            jobs.append({"job_id": job_id, "error": f"Job is {job.status}, not waiting for tailoring"})
            continue
        ranking = _latest_ranking(db, job.id)
        analysis = job.fit_analysis or {}
        try:
            fit = _fit_analysis(db, job)
        except ContextError as exc:  # the Tailor never works without the Ranker's analysis
            jobs.append({"job_id": job_id, "error": str(exc)})
            continue
        evidence = _evidence_for(db, job, ranking.relevant_evidence_ids if ranking else [])
        jobs.append({
            "job_id": job.id, "company": job.company, "role": job.title, "location": job.location,
            "job_description": (job.description or "")[:DESCRIPTION_LIMIT],
            "fit_analysis": fit,
            "relevant_evidence": evidence,
            **_posting_targets(db, profile, job, evidence),
            "recommended_resume_focus": (ranking.recommended_resume_focus if ranking
                                         else analysis.get("recommended_resume_focus") or []),
            "resume_gaps": ranking.resume_gaps if ranking else analysis.get("resume_gaps") or [],
            **_retailor_feedback(db, job),
            **_job_resume(db, profile, job),
        })
    # The profile and the active resume are the same for most jobs, so they are sent once; a job that uses another
    # resume (chosen for its search, or by the person) carries its own `resume`, which replaces active_resume for it.
    return {"ok": True, "prompt_version": MCP_TAILORING_PROMPT_VERSION, "rules": list(TAILORING_RULES),
            "candidate_profile": facts,
            "active_resume": {"text": active.master_resume,
                              "resume_id": getattr(getattr(active, "chosen_resume", None), "id", None),
                              "source": getattr(active, "resume_source", "master_text"),
                              "length_plan": plan, "roles": _roles(parsed, plan),
                              **_held_back(db, active)},
            "jobs": jobs}


def _held_back(db: Session, view: Any) -> dict[str, Any]:
    """The resume's unverified facts tailoring may not use because they were read with too little confidence
    (context_builder.hold_back_unconfident), as the in-app Tailor leaves them out. Empty, and absent, otherwise."""
    from app.services.context_builder import VERIFIED_ONLY, WITH_UNVERIFIED, hold_back_unconfident
    from app.services.user_settings import candidate_policy

    resume = getattr(view, "chosen_resume", None)
    if resume is None:
        return {}
    allowed = WITH_UNVERIFIED if candidate_policy(db, view.user_id)["allow_unverified_resume_facts"] else VERIFIED_ONLY
    _data, held = hold_back_unconfident(db, resume, resume.structured_data, allowed, "tailoring")
    return {"held_back_facts": held, "held_back_note": HELD_BACK_NOTE} if held else {}


def _length_plan(db: Session, view: Any) -> dict[str, Any]:
    """The account's default length plan for this resume's roles (app.services.resume_length), as the Tailor reads
    it: the page target, the summary's sentence budget and each role's max_bullets by role_index."""
    from app.services import resume_length

    plan = resume_length.plan_for(db, getattr(view, "user_id", None))
    caps = resume_length.role_caps(plan, parse_resume(view.master_resume or "").roles)
    return {**plan.as_dict(), "target": resume_length.PAGE_LABELS[plan.pages],
            "summary_sentences": resume_length.SUMMARY_SENTENCES[plan.pages],
            "role_caps": {str(index): cap for index, cap in caps.items()}}


def _roles(parsed: Any, plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"role_index": i, "title": r.title, "company": r.company, "dates": r.dates, "bullets": r.bullets,
             "max_bullets": plan["role_caps"].get(str(i), 0)} for i, r in enumerate(parsed.roles)]


def _job_resume(db: Session, profile: Any, job: Job) -> dict[str, Any]:
    view = rv.for_job(db, profile, job)
    chosen = getattr(view, "chosen_resume", None)
    if chosen is None or chosen.is_active:
        return {}  # the job uses the active resume, sent once as active_resume
    parsed, plan = parse_resume(view.master_resume), _length_plan(db, view)
    return {"resume": {"resume_id": chosen.id, "name": chosen.name or f"Version {chosen.version}",
                       "note": "Tailor this job from this resume, not active_resume",
                       "text": view.master_resume, "length_plan": plan, "roles": _roles(parsed, plan),
                       **_held_back(db, view)}}


def _retailor_feedback(db: Session, job: Job) -> dict[str, Any]:
    """For a packet review sent back: what to fix this time. Empty for a job that has never been tailored."""
    from app.services.review_results import retailor_feedback

    return retailor_feedback(latest_variant(db, job.id) if job.status == NEEDS_RETAILOR else None)


def _save_one_tailored(db: Session, profile: Any, item: Any, seen: set[int]) -> dict[str, Any]:
    job_id = item.get("job_id") if isinstance(item, dict) else None
    try:
        data = TailoredResumeIn.model_validate(item)
    except ValidationError as exc:
        return {"job_id": job_id, "status": "REJECTED", "errors": schema_errors(exc)}
    if data.job_id in seen:
        return {"job_id": data.job_id, "status": "REJECTED",
                "errors": [{"code": "DUPLICATE", "location": "job_id", "message": "This job appears twice in the batch"}]}
    seen.add(data.job_id)
    job = db.get(Job, data.job_id)
    if job is None:
        return {"job_id": data.job_id, "status": "REJECTED",
                "errors": [{"code": "NOT_FOUND", "location": "job_id", "message": "Job not found"}]}
    if not _in_tailor_queue(job):
        return {"job_id": job.id, "status": "REJECTED", "errors": [
            {"code": "NOT_IN_QUEUE", "location": "job_id", "message": f"Job is {job.status}, not waiting for tailoring"}]}
    view = rv.for_job(db, profile, job)
    issues = validate_tailored_resume(db, view, job, data)
    if issues:
        return {"job_id": job.id, "status": "REJECTED", "errors": issues}
    # The length plan is enforced, not rejected: bullets beyond a role's budget are dropped and reported.
    plan = _length_plan(db, view)
    sections, trimmed = enforce_length(list(data.experience_sections), plan)
    data = data.model_copy(update={"experience_sections": sections})
    content = {**to_variant_content(data, source="claude_desktop"), "prompt_version": MCP_TAILORING_PROMPT_VERSION,
               "length_plan": plan}
    variant = persist_tailored_resume(db, job, content)
    return {"job_id": job.id, "status": "SAVED", "resume_id": variant.id, "version": variant.version,
            "job_status": job.status, "next": "waiting for review", **({"trimmed": trimmed} if trimmed else {})}


def save_bulk_tailored_resumes(db: Session, results: list[dict[str, Any]]) -> dict[str, Any]:
    items = _check_batch(results, tailor_limit(), "MCP_BULK_TAILOR_LIMIT", "results")
    profile = get_profile(db)
    _require_resume(profile)
    seen: set[int] = set()
    out = []
    for item in items:
        try:
            out.append(_save_one_tailored(db, profile, item, seen))
        except Exception:  # noqa: BLE001 - one broken item never stops the batch
            db.rollback()
            out.append({"job_id": item.get("job_id") if isinstance(item, dict) else None, "status": "ERROR",
                        "errors": [{"code": "INTERNAL", "location": "result", "message": "Could not be saved"}]})
    saved = sum(1 for r in out if r["status"] == "SAVED")
    return {"ok": True, "operation_id": str(uuid.uuid4()), "status": _operation_status(saved, len(out)),
            "saved": saved, "rejected": len(out) - saved, "results": out}


def save_tailored_resume(db: Session, result: dict[str, Any]) -> dict[str, Any]:
    return save_bulk_tailored_resumes(db, [result])


# ───────────── review ─────────────

def _waiting_for_review(db: Session, job: Job) -> ResumeVariant | None:
    variant = latest_variant(db, job.id)
    return variant if job.status == "tailored" and variant is not None and variant.reviewer_score is None else None


def list_jobs_for_review(db: Session, minimum_fit_score: float | None = None, limit: int = 20) -> dict[str, Any]:
    limit = _check_limit(limit, review_limit(), "MCP_BULK_REVIEW_LIMIT")
    query = select(Job).where(Job.status == "tailored")
    if minimum_fit_score is not None:
        query = query.where(Job.fit_score >= float(minimum_fit_score))
    out = []
    for job in db.scalars(query.order_by(nulls_last(desc(Job.fit_score)), Job.id)).all():
        variant = _waiting_for_review(db, job)
        if variant is None:
            continue
        out.append({"job_id": job.id, "title": job.title, "company": job.company, "fit_score": job.fit_score,
                    "tailored_resume_version": variant.version, "status": job.status,
                    "tailored_by": (variant.content or {}).get("source") or "backend",
                    "tailored_at": _iso(variant.created_at)})
        if len(out) >= limit:
            break
    return {"ok": True, "count": len(out), "jobs": out}


def get_review_context(db: Session, job_ids: list[int]) -> dict[str, Any]:
    ids = _job_ids(job_ids, review_limit(), "MCP_BULK_REVIEW_LIMIT")
    profile, facts = _profile_payload(db)
    policy = policy_for(db, profile.user_id)
    jobs = []
    for job_id in ids:
        job = db.get(Job, job_id)
        variant = _waiting_for_review(db, job) if job else None
        if variant is None:
            jobs.append({"job_id": job_id, "error": "Job not found or not waiting for review"})
            continue
        content = variant.content or {}
        jobs.append({
            "job_id": job.id, "company": job.company, "role": job.title, "location": job.location,
            "job_description": (job.description or "")[:DESCRIPTION_LIMIT],
            "verified_evidence": _evidence_for(db, job, content.get("selected_evidence_ids") or []),
            "tailored_resume": {"version": variant.version, "markdown": variant.markdown,
                                "summary": content.get("summary", ""), "skills": content.get("skills") or [],
                                "bullet_evidence": content.get("bullet_evidence") or [],
                                "cover_letter": variant.cover_letter,
                                "screening_answer_drafts": content.get("screening_answer_drafts") or {}},
            **_held_back(db, rv.for_job(db, profile, job)),
        })
    return {"ok": True, "prompt_version": MCP_REVIEW_PROMPT_VERSION, "rules": list(REVIEW_RULES),
            "candidate_profile": facts,
            "candidate_policy": {**policy.public(), "min_review_score": settings_for(db).min_review_score,
                                 "work_authorization": profile.work_authorization or ""},
            "jobs": jobs}


def _save_one_review(db: Session, item: Any, seen: set[int]) -> dict[str, Any]:
    job_id = item.get("job_id") if isinstance(item, dict) else None
    try:
        review = ReviewIn.model_validate(item)
    except ValidationError as exc:
        return {"job_id": job_id, "status": "REJECTED", "errors": schema_errors(exc)}
    if review.job_id in seen:
        return {"job_id": review.job_id, "status": "REJECTED",
                "errors": [{"code": "DUPLICATE", "location": "job_id", "message": "This job appears twice in the batch"}]}
    seen.add(review.job_id)
    job = db.get(Job, review.job_id)
    variant = _waiting_for_review(db, job) if job else None
    if variant is None:
        return {"job_id": review.job_id, "status": "REJECTED", "errors": [
            {"code": "NOT_WAITING", "location": "job_id", "message": "Job not found or not waiting for review"}]}
    if review.tailored_resume_version is not None and review.tailored_resume_version != variant.version:
        return {"job_id": job.id, "status": "REJECTED", "errors": [
            {"code": "STALE_VERSION", "location": "tailored_resume_version",
             "message": f"Version {variant.version} is the one waiting for review"}]}
    decision = apply_review(db, job, variant, review, source="claude_desktop")
    auto_approved = packet_outcome(db, job, variant)["auto_approved"] if decision.decision == "PASS" else False
    return {"job_id": job.id, "status": "SAVED", "version": variant.version, "decision": decision.decision,
            "reasons": decision.reasons, "job_status": job.status, "auto_approved": auto_approved}


def save_bulk_reviews(db: Session, reviews: list[dict[str, Any]]) -> dict[str, Any]:
    items = _check_batch(reviews, review_limit(), "MCP_BULK_REVIEW_LIMIT", "reviews")
    seen: set[int] = set()
    out = []
    for item in items:
        try:
            out.append(_save_one_review(db, item, seen))
        except Exception:  # noqa: BLE001 - one broken item never stops the batch
            db.rollback()
            out.append({"job_id": item.get("job_id") if isinstance(item, dict) else None, "status": "ERROR",
                        "errors": [{"code": "INTERNAL", "location": "review", "message": "Could not be saved"}]})
    saved = sum(1 for r in out if r["status"] == "SAVED")
    return {"ok": True, "operation_id": str(uuid.uuid4()), "status": _operation_status(saved, len(out)),
            "saved": saved, "rejected": len(out) - saved, "results": out}


def save_review(db: Session, review: dict[str, Any]) -> dict[str, Any]:
    return save_bulk_reviews(db, [review])


# ───────────── status ─────────────

def get_bulk_operation_status(db: Session, operation_id: str | None = None) -> dict[str, Any]:
    row: ConversationAction | None = conversation_actions.latest_operation(db, operation_id)
    if row is None:
        raise ToolError("No bulk operation found" + (f" with id {operation_id}" if operation_id else ""))
    summary = row.result_summary or {}
    current = []
    for item in summary.get("items") or []:
        job = db.get(Job, item["job_id"]) if isinstance(item.get("job_id"), int) else None
        variant = latest_variant(db, job.id) if job else None
        current.append({**item, "job_status_now": job.status if job else None,
                        "latest_version": variant.version if variant else None,
                        "reviewed": variant is not None and variant.reviewer_score is not None,
                        "review_decision": ((variant.reviewer_notes or {}).get("decision") if variant else None)})
    return {"ok": True, "operation_id": row.operation_id, "tool_name": row.tool_name,
            "created_at": _iso(row.created_at), "result_status": row.result_status,
            "saved": summary.get("saved"), "rejected": summary.get("rejected"), "items": current}


# ───────────── applying ─────────────

def application_page_limit() -> int:
    return get_settings().mcp_application_page_limit


def _threshold(reviewer_score_above: float | None) -> float:
    try:
        return apply_queue.check_threshold(reviewer_score_above)
    except (TypeError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


def _job(db: Session, job_id: Any) -> Job:
    if not isinstance(job_id, int) or isinstance(job_id, bool):
        raise ToolError("job_id must be an integer")
    job = db.get(Job, job_id)
    if job is None:
        raise ToolError(f"Job {job_id} not found")
    return job


def _blocker_out(blocker: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if not blocker else {k: blocker.get(k) for k in ("reason", "detail", "question_ids", "recorded_at")}


WHO_APPLIES = ("claude", "person")


def route(job: Job) -> dict[str, Any]:
    """Which ATS the job's application lives on and who may submit it (`ats_map`, deterministic). Claude may drive a
    form only where Job Hunt OS can complete it (`AtsMatch.auto_submit`); LinkedIn Easy Apply, Indeed Apply and Ashby
    are always the person's."""
    match = ats_map.for_job(job)
    if match is None:
        return {"ats": None, "label": None, "apply_domain": None, "who_applies": "person",
                "reason": "The job has no application web address"}
    claude = match.auto_submit
    reason = (f"{match.label} form on {match.apply_domain}: Claude may fill it and submit it after "
              "begin_application_submission" if claude
              else f"{match.note or match.label + ' applications cannot be automated'}: the candidate applies")
    return {"ats": match.ats, "label": match.label, "apply_domain": match.apply_domain,
            "who_applies": "claude" if claude else "person", "reason": reason}


def list_jobs_for_application(db: Session, reviewer_score_above: float | None = None, company: str | None = None,
                              include_blocked: bool = False, offset: int = 0, limit: int = 20,
                              who_applies: str | None = None) -> dict[str, Any]:
    limit = _check_limit(limit, application_page_limit(), "MCP_APPLICATION_PAGE_LIMIT")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ToolError("offset must be a non-negative integer")
    if who_applies is not None and who_applies not in WHO_APPLIES:
        raise ToolError("who_applies must be claude or person")
    threshold = _threshold(reviewer_score_above)
    ready = apply_queue.ready_to_apply(db, reviewer_score_above=threshold, include_blocked=bool(include_blocked),
                                       company=company)
    ready = [{**r, "route": route(r["job"])} for r in ready]
    if who_applies is not None:
        ready = [r for r in ready if r["route"]["who_applies"] == who_applies]
    page = ready[offset:offset + limit]
    jobs = [{"job_id": r["job"].id, "title": r["job"].title, "company": r["job"].company,
             "location": r["job"].location, "apply_url": apply_queue.apply_url(r["job"]),
             "posting_url": r["job"].url or r["job"].source_url or None, "route": r["route"],
             "reviewer_score": r["resume"].reviewer_score, "fit_score": r["job"].fit_score,
             "resume_version": r["resume"].version, "approved_at": _iso(r["application"].approved_at),
             "blocker": _blocker_out(r["blocker"]), "claim": _claim_out(r.get("claim"))} for r in page]
    next_offset = offset + len(page) if offset + len(page) < len(ready) else None
    return {"ok": True, "reviewer_score_above": threshold, "total": len(ready), "offset": offset,
            "count": len(jobs), "next_offset": next_offset, "jobs": jobs}


def _formats(formats: list[str] | None) -> list[str]:
    if formats is not None and not isinstance(formats, list):
        raise ToolError(f"formats must be a list chosen from {', '.join(apply_queue.FILE_FORMATS)}")
    formats = list(dict.fromkeys(formats or apply_queue.FILE_FORMATS))
    if any(f not in apply_queue.FILE_FORMATS for f in formats):
        raise ToolError(f"formats must be chosen from {', '.join(apply_queue.FILE_FORMATS)}")
    return formats


def get_job_packet(db: Session, job_id: int, formats: list[str] | None = None,
                   include_content: bool = False, cover_letter_files: bool = True) -> dict[str, Any]:
    job = _job(db, job_id)
    formats = _formats(formats)
    reason = apply_queue.not_ready_reason(db, job)
    if reason:
        raise ToolError(f"Job {job.id} is not ready to apply to: {reason}")
    if apply_queue.guarded_hold(db, job) is not None:
        raise ToolError(f"Job {job.id}: {apply_queue.ROUTE_HELD_REASON} ({apply_queue.ROUTE_HELD})")
    application = job.application
    resume = apply_queue.approved_resume(db, application)
    content = resume.content or {}
    # The letter exactly as it was approved (approval.packet_content): rendered, never edited or re-hashed.
    letter = approval.packet_content(resume)["cover_letter"]
    return {"ok": True, "prompt_version": MCP_APPLICATION_PROMPT_VERSION, "rules": list(APPLICATION_RULES),
            "job_id": job.id, "title": job.title, "company": job.company, "location": job.location,
            "apply_url": apply_queue.apply_url(job), "route": route(job),
            "resume": {"resume_id": resume.id, "version": resume.version, "reviewer_score": resume.reviewer_score,
                       "approved_at": _iso(application.approved_at), "markdown": resume.markdown,
                       "summary": content.get("summary", "")},
            "cover_letter": letter,
            "screening_answer_drafts": content.get("screening_answer_drafts") or {},
            "files": apply_queue.packet_files(db, job, resume, formats, include_content=bool(include_content),
                                              cover_letter=letter if cover_letter_files else None),
            "blocker": _blocker_out(apply_queue.open_blocker(db, application))}


# ───────────── finding jobs and their documents ─────────────

QUERY_LIMIT = 200


def _approved_packet(db: Session, job: Job) -> ResumeVariant | None:
    """The approved resume version when the job's packet is approved (or applied) and still the one approved."""
    application = job.application
    if application is None or application.status not in ("approved", "applied") or not application.approved_at:
        return None
    resume = apply_queue.approved_resume(db, application)
    return resume if resume is not None and approval.verify(db, application).ok else None


def find_jobs(db: Session, job_id: int | None = None, query: str | None = None, stage: str | None = None,
              limit: int = 10) -> dict[str, Any]:
    limit = _check_limit(limit, application_page_limit(), "MCP_APPLICATION_PAGE_LIMIT")
    if job_id is None and not (isinstance(query, str) and query.strip()):
        raise ToolError("Pass a job_id, or a query (part of the company name or job title)")
    if query is not None and (not isinstance(query, str) or len(query) > QUERY_LIMIT):
        raise ToolError(f"query must be text of at most {QUERY_LIMIT} characters")
    if stage is not None and not isinstance(stage, str):
        raise ToolError("stage must be a job status (e.g. tailored, approved, applied) or workflow state")
    statement = select(Job)
    if job_id is not None:
        if not isinstance(job_id, int) or isinstance(job_id, bool):
            raise ToolError("job_id must be an integer")
        statement = statement.where(Job.id == job_id)
    if query and query.strip():
        pattern = f"%{query.strip()}%"
        statement = statement.where(or_(Job.company.ilike(pattern), Job.title.ilike(pattern)))
    if stage and stage.strip():
        statement = statement.where(or_(func.lower(Job.status) == stage.strip().lower(),
                                        Job.state == stage.strip().upper()))
    jobs = db.scalars(statement.order_by(desc(Job.id)).limit(limit)).all()
    out = []
    for job in jobs:
        versions = db.scalar(select(func.count(ResumeVariant.id)).where(ResumeVariant.job_id == job.id)) or 0
        latest = latest_variant(db, job.id)
        application = job.application
        out.append({"job_id": job.id, "title": job.title, "company": job.company, "location": job.location,
                    "status": job.status, "state": job.state, "fit_score": job.fit_score,
                    "tailored_versions": versions, "latest_version": latest.version if latest else None,
                    "reviewer_score": latest.reviewer_score if latest else None,
                    "approved": _approved_packet(db, job) is not None,
                    "applied": bool(application and application.applied_at),
                    "application_id": application.id if application else None,
                    "apply_url": apply_queue.apply_url(job)})
    return {"ok": True, "count": len(out), "jobs": out}


DRAFT_NOTE = ("These documents are not an approved packet: read or share them, but never upload them to an "
              "application. The candidate approves the packet in Job Hunt OS first.")


def get_job_documents(db: Session, job_id: int, formats: list[str] | None = None,
                      include_content: bool = False) -> dict[str, Any]:
    job = _job(db, job_id)
    formats = _formats(formats)
    approved = _approved_packet(db, job)
    resume = approved or latest_variant(db, job.id)
    if resume is None:
        raise ToolError(f"Job {job.id} has no tailored resume yet")
    if approved is not None:
        status, letter = "APPROVED", approval.packet_content(resume)["cover_letter"]
    else:
        # A version the reviewer sent back (RETAILOR) is served as TAILORED: it was reviewed, but not passed.
        decision = (resume.reviewer_notes or {}).get("decision")
        status = "REVIEWED" if resume.reviewer_score is not None and decision != "RETAILOR" else "TAILORED"
        letter = resume.cover_letter or ""
    reason = apply_queue.not_ready_reason(db, job)
    if reason is None and apply_queue.guarded_hold(db, job) is not None:
        reason = apply_queue.ROUTE_HELD_REASON
    out = {"ok": True, "status": status, "job_id": job.id, "title": job.title, "company": job.company,
           "approved": approved is not None, "may_submit": reason is None, "version": resume.version,
           "reviewer_score": resume.reviewer_score, "markdown": resume.markdown, "cover_letter": letter,
           "files": apply_queue.packet_files(db, job, resume, formats, include_content=bool(include_content),
                                             cover_letter=letter, draft=approved is None)}
    if reason is not None:
        out["not_ready_reason"] = reason
    if approved is None:
        out["note"] = DRAFT_NOTE
    return out


def search_application_knowledge(db: Session, question: str, limit: int = 5) -> dict[str, Any]:
    if not isinstance(question, str) or len(question.strip()) < 2:
        raise ToolError("question must be the form question's text")
    limit = _check_limit(limit, 20, "at most 20 sources")
    return {"ok": True, "question": question.strip()[:2000], **knowledge.search(db, question[:2000], limit)}


def request_human_answer(db: Session, job_id: int, question: str, answer_options: list[str] | None = None,
                         answer_type: str | None = None, category: str = "general", sensitive: bool = False,
                         required: bool | None = None, field_label: str | None = None,
                         note: str | None = None) -> dict[str, Any]:
    job = _job(db, job_id)
    try:
        saved = knowledge.request_answer(db, job, question, answer_options=answer_options, answer_type=answer_type,
                                         category=category, sensitive=sensitive, required=required,
                                         field_label=field_label, note=note)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return {"ok": True, "job_id": job.id, **saved}


def _claim_out(claim: Any) -> dict[str, Any] | None:
    if claim is None:
        return None
    return {"claim_id": claim.id, "status": claim.status, "client": claim.client, "job_id": claim.job_id,
            "claimed_at": _iso(claim.claimed_at), "lease_expires_at": _iso(claim.lease_expires_at)}


def begin_application_submission(db: Session, job_id: int) -> dict[str, Any]:
    job = _job(db, job_id)
    result = apply_queue.begin_submission(db, job, client="mcp")
    if not result.ok:
        return {"ok": False, "status": result.status, "job_id": job.id, "error": result.reason,
                "conflict_job_id": result.conflict_job_id}
    claim = result.claim
    return {"ok": True, "status": "CLAIMED", "job_id": job.id, "claim_token": claim.token,
            "identity": claim.identity, "lease_expires_at": _iso(claim.lease_expires_at)}


def release_application_claim(db: Session, job_id: int, claim_token: str, reason: str) -> dict[str, Any]:
    job = _job(db, job_id)
    if not isinstance(claim_token, str) or not claim_token.strip():
        raise ToolError("claim_token is required")
    try:
        claim = apply_queue.release_claim(db, job, claim_token, reason or "")
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return {"ok": True, "status": "RELEASED", "job_id": job.id, "claim": _claim_out(claim)}


def record_application_result(db: Session, job_id: int, outcome: str, confirmation_id: str | None = None,
                              blocker_reason: str | None = None, detail: str | None = None,
                              question_ids: list[int] | None = None, claim_token: str | None = None) -> dict[str, Any]:
    job = _job(db, job_id)
    if outcome not in ("submitted", "blocked"):
        raise ToolError("outcome must be submitted or blocked")
    if question_ids is not None and not (isinstance(question_ids, list)
                                         and all(isinstance(i, int) and not isinstance(i, bool) for i in question_ids)):
        raise ToolError("question_ids must be a list of integers")
    if claim_token is not None and not isinstance(claim_token, str):
        raise ToolError("claim_token must be a string")
    if outcome == "blocked":
        application = apply_queue.lock_application(db, job)
        if application is not None and application.status == "applied":
            return {"ok": True, "status": "ALREADY_APPLIED", "job_id": job.id, "job_status": job.status,
                    "applied_at": _iso(application.applied_at), "confirmation_id": application.confirmation_id,
                    "submitted_via": application.submitted_via}
        if application is None or application.status != "approved" or job.status != "approved":
            raise ToolError(f"Job {job.id} is {job.status}, not approved for application")
        try:
            blocker = apply_queue.record_blocker(db, job, blocker_reason or "", detail=detail or "",
                                                 question_ids=question_ids, claim_token=claim_token)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return {"ok": True, "status": "BLOCKED", "job_id": job.id, "job_status": job.status,
                "blocker": _blocker_out(blocker)}
    result = apply_queue.record_submission(db, job, claim_token=claim_token, client="mcp",
                                           confirmation_id=confirmation_id)
    application = result.application
    if result.status == "ALREADY_APPLIED":
        # Idempotent: a second report of the same submission changes nothing.
        return {"ok": True, "status": "ALREADY_APPLIED", "job_id": job.id, "job_status": job.status,
                "applied_at": _iso(application.applied_at), "confirmation_id": application.confirmation_id,
                "submitted_via": application.submitted_via}
    if result.status == "NOT_APPROVED":
        raise ToolError(result.reason or f"Job {job.id} is not approved for application")
    if result.status == "DUPLICATE_APPLICATION":
        return {"ok": False, "status": "DUPLICATE_APPLICATION", "job_id": job.id,
                "duplicate_of_job_id": result.duplicate_job_id, "error": f"{result.reason}; not recorded"}
    if not result.ok:
        return {"ok": False, "status": result.status, "job_id": job.id, "error": f"{result.reason}; not recorded"}
    return {"ok": True, "status": "SUBMITTED", "job_id": job.id, "job_status": job.status,
            "applied_at": _iso(application.applied_at), "confirmation_id": application.confirmation_id,
            "followup_due_at": _iso(application.followup_due_at), "claimed": result.claimed}


# ───────────── follow-ups ─────────────

FOLLOWUP_LIMIT = 50
MAX_WITHIN_DAYS = 60


def _int_id(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolError(f"{name} must be an integer")
    return value


def _application(db: Session, application_id: Any) -> Application:
    app = db.get(Application, _int_id(application_id, "application_id"))
    if app is None:
        raise ToolError(f"Application {application_id} not found")
    return app


def _followup(db: Session, followup_id: Any) -> FollowUp:
    followup = db.get(FollowUp, _int_id(followup_id, "followup_id"))
    if followup is None:
        raise ToolError(f"Follow-up {followup_id} not found")
    return followup


def _when(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ToolError("sent_at must be an ISO date and time, e.g. 2026-11-06T09:30:00Z") from exc


def list_followups_due(db: Session, within_days: int = 3, limit: int = 20) -> dict[str, Any]:
    if not isinstance(within_days, int) or isinstance(within_days, bool) or not 0 <= within_days <= MAX_WITHIN_DAYS:
        raise ToolError(f"within_days must be a whole number of days from 0 to {MAX_WITHIN_DAYS}")
    limit = _check_limit(limit, FOLLOWUP_LIMIT, f"at most {FOLLOWUP_LIMIT}")
    items = followups.due(db, within_days=within_days, limit=limit)
    return {"ok": True, "prompt_version": MCP_FOLLOWUP_PROMPT_VERSION, "rules": list(FOLLOWUP_RULES),
            "within_days": within_days, "count": len(items), "followups": items}


def save_followup_draft(db: Session, application_id: int, text: str, channel: str = "email") -> dict[str, Any]:
    app = _application(db, application_id)
    try:
        followup = followups.save_draft(db, app, text, channel, source="mcp")
    except followups.FollowUpRefused as exc:
        raise ToolError(str(exc)) from exc
    return {"ok": True, "status": "SAVED", "followup_id": followup.id, "application_id": app.id,
            "job_id": app.job_id, "channel": followup.channel, "due_at": _iso(followup.due_at),
            "employer_replied": followups.employer_replied(db, app),
            "note": "Saved as a draft; nothing was sent. Show it to the candidate before anything is sent."}


def record_followup_sent(db: Session, followup_id: int, sent_text: str | None = None, channel: str | None = None,
                         sent_at: str | None = None, recipient: str | None = None) -> dict[str, Any]:
    followup = _followup(db, followup_id)
    try:
        outcome = followups.mark_sent(db, followup, sent_text=sent_text, channel=channel, sent_at=_when(sent_at),
                                      recipient=recipient, source="mcp")
    except followups.FollowUpRefused as exc:
        raise ToolError(str(exc)) from exc
    return {"ok": True, "status": outcome.status, "followup_id": followup.id,
            "application_id": outcome.application.id, "job_id": outcome.application.job_id,
            "channel": followup.channel, "sent_at": _iso(followup.sent_at),
            "next_followup_due_at": _iso(outcome.next_due_at)}


def skip_followup(db: Session, followup_id: int | None = None, application_id: int | None = None,
                  reason: str | None = None) -> dict[str, Any]:
    if followup_id is None and application_id is None:
        raise ToolError("Pass a followup_id or an application_id")
    followup = _followup(db, followup_id) if followup_id is not None else None
    app = _application(db, application_id if application_id is not None else followup.application_id)
    try:
        outcome = followups.skip(db, app, followup=followup, reason=reason, source="mcp")
    except followups.FollowUpRefused as exc:
        raise ToolError(str(exc)) from exc
    return {"ok": True, "status": outcome.status, "application_id": app.id, "job_id": app.job_id,
            "followup_id": outcome.followup.id if outcome.followup else None}
