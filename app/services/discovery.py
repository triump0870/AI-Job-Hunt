"""What happens to a job the moment it is discovered, whichever import found it.

    new job (Apify/Hunter sync, Lever/Greenhouse import)
      → domain filter (app.services.domain_match): not matching → hidden, nothing else happens
        (DOMAIN_CLASSIFIER_MODE=on: only unambiguous rules hide; the rest waits for app.services.domain_classifier, which
        continues below through domain_match.forward. shadow: the same is only recorded, for comparison.)
      → search scope (app.services.search_scope): not the run's roles, skills or place → hidden the same way
      → resume: the saved search's pinned resume, else the best-tagged resume (app.services.resume_matching), else
        none (the account's active resume, at use time)
      → description usable? parse it : recover it first (app.services.jd_recovery)
      (with QUALIFICATION_GRAPH_MODE=on the last step is the qualification graph's: app.services.qualification)

A job the person adds themselves (source "manual") is always kept: adding it is the decision. Postings deleted
earlier because they no longer existed are not imported again (app.services.job_removal.is_removed); the import
paths check that before creating anything.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Job, Resume, SavedSearch, SearchRun
from app.services import (
    domain_classifier,
    domain_match,
    jd_recovery,
    job_description,
    resume_matching,
    search_scope,
)

# Jobs whose resume never moves again: what was sent stays what was sent.
SETTLED_STATUSES = ("approved", "applied")


def setup_resume_choice(db: Session, search_run_id: int | None, owner: int) -> tuple[str, int | None]:
    """What the saved search behind this run says about the resume: ("auto", None) — match by tags, the default —
    ("active", None) — always the active resume — or ("fixed", id) for a resume it pinned (that still exists)."""
    run = db.get(SearchRun, search_run_id) if search_run_id else None
    setup = db.get(SavedSearch, run.saved_search_id) if run is not None and run.saved_search_id else None
    settings = (setup.settings or {}) if setup is not None else {}
    chosen = settings.get("resume_id")
    mode = settings.get("resume_mode") or ("fixed" if isinstance(chosen, int) else "auto")
    if mode == "fixed" and isinstance(chosen, int):
        resume = db.get(Resume, chosen)
        return ("fixed", resume.id) if resume is not None and resume.user_id == owner else ("auto", None)
    return ("active", None) if mode == "active" else ("auto", None)


def choose_resume(db: Session, job: Job, search_run_id: int | None = None) -> None:
    """The saved search's pinned resume, else the best-tagged resume for the job (app.services.resume_matching),
    else none (the active resume). Does not commit."""
    if job.resume_choice is not None or job.resume_id is not None:
        return
    mode, resume_id = setup_resume_choice(db, search_run_id or job.search_run_id, job.user_id)
    if mode == "fixed":
        job.resume_id, job.resume_choice = resume_id, resume_matching.SETUP
    elif mode == "active":
        job.resume_choice = resume_matching.SETUP  # pinned to the active resume: never re-matched
    else:
        resume_matching.auto_assign(db, job)


def admit(db: Session, job: Job, *, search_run_id: int | None = None,
          rules_cache: dict[int, domain_match.Rules] | None = None) -> str:
    """Decide what a just-created (flushed) job needs. Returns created or not_matching. Does not commit."""
    if job.source != "manual":
        domain_classifier.observe(db, job, search_run_id, rules_cache)  # shadow mode: recorded, the job is not touched
    if job.source == "manual":
        job.domain_match, job.domain_reason = domain_match.KEPT, "Added by you"
    elif not domain_match.screen(db, job, rules_cache, actor="discovery", search_run_id=search_run_id):
        # Waiting for the classifier is not hidden: it was created, and its verdict decides what happens next.
        return "created" if job.domain_match == domain_match.PENDING else "not_matching"
    elif not search_scope.screen(db, job, search_run_id, actor="discovery"):
        return "not_matching"  # a role, skill or place its own search did not ask for
    choose_resume(db, job, search_run_id)
    from app.services import qualification

    if qualification.routes(db):
        # The qualification graph takes it from here (QUALIFICATION_GRAPH_MODE=on): it recovers, parses and ranks the
        # job through the same services, in order, after admitting it again (idempotent) as its first step.
        qualification.dispatch(db, job, reason="new")
        return "created"
    if not jd_recovery.on_new_job(db, job):
        job_description.queue_parse(db, job)
    return "created"


def setup_choice(setup: SavedSearch) -> tuple[str, int | None]:
    """A setup's resume as stored: ("auto" | "active" | "fixed", resume id)."""
    settings = setup.settings or {}
    chosen = settings.get("resume_id")
    mode = settings.get("resume_mode") or ("fixed" if isinstance(chosen, int) else "auto")
    return mode, chosen if mode == "fixed" and isinstance(chosen, int) else None


def setup_jobs(db: Session, setup: SavedSearch) -> list[Job]:
    """The jobs this setup found whose resume follows the setup: not pinned by the person (USER, ACTIVE), not
    approved or applied, not hidden."""
    runs = select(SearchRun.id).where(SearchRun.saved_search_id == setup.id)
    return list(db.scalars(select(Job).where(
        Job.search_run_id.in_(runs),
        or_(Job.resume_choice.is_(None), Job.resume_choice.in_((resume_matching.AUTO, resume_matching.SETUP))),
        Job.status.not_in(SETTLED_STATUSES),
        Job.domain_match.is_distinct_from(domain_match.NOT_MATCHING))).all())


def repoint_jobs(db: Session, jobs: list[Job], assign: Callable[[Job], None], actor: str) -> dict[str, Any]:
    """Apply a resume choice to jobs (`assign` sets resume_id / resume_choice). A job whose resume changed is ranked
    again with it, and a tailored packet needs re-tailoring: automatic tailoring re-tailors it once the new ranking
    qualifies it (`tailor_tasks.RESUME_CHANGED`), when the account has it on. Does not commit."""
    from app.services import ranker
    from app.services.tailor_tasks import RESUME_CHANGED
    from app.services.workflow import NEEDS_RETAILOR

    changed, requeued, skipped = [], [], []
    policy = None
    for job in jobs:
        before = job.resume_id
        assign(job)
        if job.resume_id == before:
            continue
        changed.append(job.id)
        had_packet = job.status in ("tailored", "pending_approval")
        if had_packet:
            job.status = NEEDS_RETAILOR
        if job.fit_score is not None or job.state in (ranker.S.RANKED, ranker.S.QUALIFIED, ranker.S.RANK_PENDING):
            policy = policy or ranker.policy_for(db, job.user_id)
            why = ranker.queue_job(db, job, policy=policy, actor=actor, reason="resume_changed", force=True)
            (skipped.append({"id": job.id, "reason": why}) if why else requeued.append(job.id))
            if had_packet and not why:
                # The person changed the resume: a new request, even if they once cancelled this job's tailoring. Only
                # with a ranking queued, so the packet is never re-tailored from the old resume's analysis.
                job.auto_tailor_status = RESUME_CHANGED
    return {"changed": changed, "ranking_queued": requeued, "ranking_skipped": skipped}


def follow_setup(db: Session, setup: SavedSearch, actor: str) -> dict[str, Any]:
    """After a setup's resume changed: its jobs (setup_jobs) take the setup's choice again — the pinned resume, the
    active one, or the best-tagged. A job the person pinned themselves keeps its resume. Does not commit."""
    def assign(job: Job) -> None:
        job.resume_id, job.resume_choice = None, None
        choose_resume(db, job, job.search_run_id)

    jobs = setup_jobs(db, setup)
    return {"jobs": len(jobs), **repoint_jobs(db, jobs, assign, actor)}
