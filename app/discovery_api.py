"""Discovery quality API: the domain filter (lists, learned rules, keeping a hidden job) and the resume per job.

Nothing here calls a model. Hidden jobs are listed by GET /api/jobs/search?domain=not_matching.
"""
from __future__ import annotations

import copy
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import get_user_db
from app.models import Job, Resume, SavedSearch, SearchRun
from app.models import now as utcnow
from app.scheduler_hooks import run_soon
from app.services import (
    discovery,
    domain_classifier,
    domain_match,
    feedback_learning,
    resume_matching,
)
from app.services.user_settings import lock_settings_row, settings_for

router = APIRouter()

Terms = Field(default_factory=list, max_length=200)


class DomainIn(BaseModel):
    enabled: bool = True
    exclude_terms: list[str] = Terms
    skill_excludes: list[str] = Terms
    include_terms: list[str] = Terms
    learn_from_feedback: bool = True


class LearnedIn(BaseModel):
    term: str = Field(min_length=1, max_length=200)
    kind: Literal["title", "company"] = "title"
    action: Literal["disable", "enable", "delete", "promote"]


class IdsIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)


# Re-match filters: the stages a person picks from, each the job statuses it covers (approved and applied never move).
REMATCH_STAGES = {"new": ("discovered",), "ranked": ("shortlisted", "maybe"),
                  "tailored": ("tailored", "needs_retailor", "pending_approval"), "archived": ("archived", "dismissed")}
REMATCH_DEFAULT_STAGES = ("new", "ranked", "tailored")


class RematchIn(BaseModel):
    stages: list[Literal["new", "ranked", "tailored", "archived"]] | None = None  # None: every stage but archived
    found_within_days: int | None = Field(default=None, ge=1, le=3650)
    saved_search_id: int | None = None
    board: str | None = Field(default=None, max_length=50)


class RenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)


def _user(db: Session) -> int:
    return db.info["user_id"]


def _clean(terms: list[str]) -> list[str]:
    return list(dict.fromkeys(t.strip().lower() for t in terms if t and t.strip()))[:200]


def _save_domain(db: Session, **changes) -> None:
    row = lock_settings_row(db, settings_for(db))
    preferences = copy.deepcopy(row.preferences or {})  # a new object: JSON columns save by identity
    preferences.setdefault("domain", {}).update(changes)
    row.preferences = preferences


def _domain_view(db: Session) -> dict:
    user_id = _user(db)
    prefs, own, rules = domain_match.preferences(db, user_id), domain_match.derived(db, user_id), \
        domain_match.rules_for(db, user_id)
    hidden = db.scalar(select(Job.id).where(Job.domain_match == domain_match.NOT_MATCHING).limit(1)) is not None
    return {**prefs, "server_enabled": rules.enabled or not prefs["enabled"],
            "derived": {"roles": own["roles"], "skills": own["skills"], "role_words": sorted(rules.role_words)},
            # Excludes the account's own roles or skills cancel out (never applied).
            "overridden": sorted(set(map(domain_match._norm, prefs["exclude_terms"] + prefs["skill_excludes"]))
                                 - set(rules.domain_excludes) - set(rules.skill_excludes)),
            "defaults": {"exclude_terms": list(domain_match.DEFAULT_DOMAIN_EXCLUDES),
                         "skill_excludes": list(domain_match.DEFAULT_SKILL_EXCLUDES)},
            "has_hidden_jobs": hidden, "classifier_mode": domain_classifier.mode()}


@router.get("/api/settings/domain")
def get_domain(db: Session = Depends(get_user_db)):
    """The domain filter: the account's lists, what its profile and resume imply, and the learned rules."""
    return _domain_view(db)


@router.put("/api/settings/domain")
def put_domain(payload: DomainIn, db: Session = Depends(get_user_db)):
    _save_domain(db, enabled=payload.enabled, exclude_terms=_clean(payload.exclude_terms),
                 skill_excludes=_clean(payload.skill_excludes), include_terms=_clean(payload.include_terms),
                 learn_from_feedback=payload.learn_from_feedback)
    db.commit()
    return _domain_view(db)


@router.put("/api/settings/domain/learned")
def learned_rule(payload: LearnedIn, db: Session = Depends(get_user_db)):
    """Disable, enable, delete or promote (to a permanent exclude term) one learned rule."""
    prefs = domain_match.preferences(db, _user(db))
    learned = [dict(e) for e in prefs["learned"]]
    entry = next((e for e in learned if e.get("term") == payload.term and e.get("kind") == payload.kind), None)
    if entry is None:
        raise HTTPException(404, "No such learned rule")
    changes: dict = {}
    if payload.action in ("disable", "enable"):
        entry["status"] = "disabled" if payload.action == "disable" else "active"
        entry["user_disabled"] = payload.action == "disable"
    else:
        learned.remove(entry)
        if payload.action == "promote" and payload.kind == "title":
            changes["exclude_terms"] = _clean(prefs["exclude_terms"] + [payload.term])
        elif payload.action == "promote":
            from app.models import CandidateProfile

            profile = db.scalar(select(CandidateProfile))
            if profile is not None:
                search = copy.deepcopy(profile.search_preferences or {})
                search["exclude_companies"] = list(dict.fromkeys([*search.get("exclude_companies", []), entry["term"]]))
                profile.search_preferences = search
        elif payload.action == "delete":
            # Forget it for good: a rule deleted by the person is not learned again from the same events.
            entry["status"], entry["user_disabled"], entry["forgotten"] = "disabled", True, True
            learned.append(entry)
    _save_domain(db, learned=learned, **changes)
    db.commit()
    return _domain_view(db)


class ReviewIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)
    matches: bool


@router.get("/api/discovery/review")
def review_list(db: Session = Depends(get_user_db)):
    """Jobs the domain check left ambiguous, waiting for the person. Nothing is spent on them until they choose."""
    return domain_classifier.review_jobs(db, _user(db))


@router.post("/api/discovery/review")
def review_decide(payload: ReviewIn, db: Session = Depends(get_user_db)):
    """The person's choice: matching jobs go on to the detailed match, the others are hidden (Keep brings them back)."""
    out = domain_classifier.resolve_review(db, _user(db), payload.ids, payload.matches, actor=f"user:{_user(db)}")
    db.commit()
    return out


@router.post("/api/jobs/domain/recheck")
def recheck(db: Session = Depends(get_user_db)):
    """Put jobs hidden by a rule through the hybrid pipeline. It queues model calls, so it runs only when a person asks.
    Jobs they kept, jobs in a place their search did not cover and jobs the classifier hid stay as they are."""
    if domain_classifier.mode() != domain_classifier.ON:
        raise HTTPException(409, "The AI check is not on (DOMAIN_CLASSIFIER_MODE)")
    out = domain_classifier.recheck(db, _user(db), actor=f"user:{_user(db)}")
    db.commit()
    return out


@router.post("/api/jobs/domain/reapply")
def reapply(db: Session = Depends(get_user_db)):
    """Assess the account's unranked jobs again with the current lists. Kept jobs are never touched."""
    out = domain_match.reapply(db, _user(db), actor=f"user:{_user(db)}")
    db.commit()
    return out


def _keep(db: Session, ids: list[int]) -> dict:
    kept, skipped = [], []
    for job in db.scalars(select(Job).where(Job.id.in_(ids))).all():
        if job.domain_match != domain_match.NOT_MATCHING:
            skipped.append({"id": job.id, "reason": "This job is not hidden"})
            continue
        domain_match.keep(db, job, actor=f"user:{_user(db)}")
        feedback_learning.record(db, job, "KEPT")
        kept.append(job.id)
    if kept:
        feedback_learning.after_feedback(db, _user(db))
    db.commit()
    return {"kept": kept, "skipped": skipped}


@router.post("/api/jobs/{job_id}/domain-keep")
def keep_job(job_id: int, request: Request, background: BackgroundTasks, db: Session = Depends(get_user_db)):
    """Keep a job the domain filter hid: it goes through recovery, parsing and ranking like any new job."""
    if db.get(Job, job_id) is None:
        raise HTTPException(404, "Job not found")
    out = _keep(db, [job_id])
    run_soon(background, request.app, "ranking")
    return out


@router.post("/api/jobs/bulk-domain-keep")
def bulk_keep(payload: IdsIn, request: Request, background: BackgroundTasks, db: Session = Depends(get_user_db)):
    out = _keep(db, payload.ids)
    run_soon(background, request.app, "ranking")
    return out


# ───────────── the resume per job ─────────────

ResumeMode = Literal["auto", "active", "fixed"]


class ResumeChoiceIn(BaseModel):
    # auto: the best-tagged resume (app.services.resume_matching); active: always the active resume; fixed: resume_id.
    # Without a mode, a resume_id means fixed and none means active (the first version of this API).
    mode: ResumeMode | None = None
    resume_id: int | None = None


class BulkResumeIn(ResumeChoiceIn):
    ids: list[int] = Field(min_length=1, max_length=5000)


class DomainTagsIn(BaseModel):
    domain: str | None = Field(default=None, max_length=80)
    roles: list[str] = Field(default_factory=list, max_length=30)


class TagsIn(DomainTagsIn):
    """`domains` (one entry per domain, each with its roles); a body with only `domain` / `roles` is one domain."""
    domains: list[DomainTagsIn] | None = Field(default=None, max_length=8)


def _resume(db: Session, resume_id: int | None) -> Resume | None:
    if resume_id is None:
        return None
    resume = db.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(404, "Resume not found")
    return resume


def _mode(payload: ResumeChoiceIn) -> ResumeMode:
    return payload.mode or ("fixed" if payload.resume_id is not None else "active")


def _apply_choice(db: Session, job: Job, mode: ResumeMode, resume: Resume | None) -> None:
    if mode == "fixed":
        job.resume_id, job.resume_choice = resume.id, resume_matching.USER
    elif mode == "active":
        job.resume_id, job.resume_choice = None, resume_matching.ACTIVE
    else:
        job.resume_choice = None
        resume_matching.auto_assign(db, job)


def _set_resume(db: Session, jobs: list[Job], mode: ResumeMode, resume: Resume | None) -> dict:
    """Point jobs at a resume. A ranked job is ranked again with it; a tailored packet needs re-tailoring."""
    if mode == "fixed" and resume is None:
        raise HTTPException(422, "Choose a resume")
    out = discovery.repoint_jobs(db, jobs, lambda job: _apply_choice(db, job, mode, resume), actor=f"user:{_user(db)}")
    db.commit()
    return {**out, "mode": mode, "resume_id": resume.id if resume is not None else None}


@router.put("/api/jobs/{job_id}/resume")
def set_job_resume(job_id: int, payload: ResumeChoiceIn, request: Request, background: BackgroundTasks,
                   db: Session = Depends(get_user_db)):
    """Choose the resume this job is ranked and tailored with: auto (best-tagged), active, or a specific one."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    out = _set_resume(db, [job], _mode(payload), _resume(db, payload.resume_id))
    run_soon(background, request.app, "ranking")
    return out


@router.post("/api/jobs/bulk-resume")
def bulk_resume(payload: BulkResumeIn, request: Request, background: BackgroundTasks,
                db: Session = Depends(get_user_db)):
    """Choose the resume for many jobs at once — for example older jobs discovered before setups chose one."""
    resume = _resume(db, payload.resume_id)
    out = _set_resume(db, list(db.scalars(select(Job).where(Job.id.in_(payload.ids))).all()), _mode(payload), resume)
    run_soon(background, request.app, "ranking")
    return out


def _rematch_jobs(db: Session, payload: RematchIn) -> list[Job]:
    """Jobs re-matching may move: matched automatically or never chosen, not approved or applied, not hidden, and
    within the person's filters."""
    stages = payload.stages if payload.stages is not None else REMATCH_DEFAULT_STAGES
    statuses = [status for stage in stages for status in REMATCH_STAGES[stage]]
    query = select(Job).where(Job.resume_choice.is_(None) | (Job.resume_choice == resume_matching.AUTO),
                              Job.status.in_(statuses),
                              Job.domain_match.is_distinct_from(domain_match.NOT_MATCHING))
    if payload.found_within_days is not None:
        query = query.where(Job.discovered_at >= utcnow() - timedelta(days=payload.found_within_days))
    if payload.saved_search_id is not None:
        query = query.where(Job.search_run_id.in_(select(SearchRun.id)
                                                  .where(SearchRun.saved_search_id == payload.saved_search_id)))
    if payload.board:
        query = query.where(Job.source_board == payload.board)
    return list(db.scalars(query).all())


def _rematch_options(db: Session) -> dict:
    """What the filters offer: the account's setups and the boards its jobs came from, with how many jobs each has."""
    setups = db.execute(select(SavedSearch.id, SavedSearch.name).order_by(SavedSearch.name)).all()
    boards = db.execute(select(Job.source_board, func.count()).where(Job.source_board.is_not(None))
                        .group_by(Job.source_board).order_by(func.count().desc())).all()
    return {"stages": [{"id": stage, "default": stage in REMATCH_DEFAULT_STAGES} for stage in REMATCH_STAGES],
            "setups": [{"id": i, "name": name} for i, name in setups],
            "boards": [{"name": name, "jobs": n} for name, n in boards]}


@router.post("/api/jobs/resume/rematch")
def rematch(request: Request, background: BackgroundTasks, dry_run: bool = False, payload: RematchIn | None = None,
            db: Session = Depends(get_user_db)):
    """Match the jobs that have no pinned resume to the best-tagged resume again (after tagging resumes), within the
    optional filters (stage, found within N days, search setup, board). Jobs whose resume changes and were ranked are
    ranked again, so `dry_run` first says how many that is, and which resume they would move to."""
    jobs = _rematch_jobs(db, payload or RematchIn())
    resumes = resume_matching.tagged_resumes(db, _user(db))
    moves = [(job, (match.resume_id if (match := resume_matching.pick(resumes, job)) else None)) for job in jobs]
    moving = [job for job, target in moves if target != job.resume_id]
    reranked = sum(1 for job in moving if job.fit_score is not None)
    if dry_run:
        names = {r.id: f"{r.name or 'Version'} · v{r.version}" for r in resumes}
        into: dict[str, int] = {}
        for job, target in moves:
            if target != job.resume_id:
                key = names.get(target, "the active resume")
                into[key] = into.get(key, 0) + 1
        db.rollback()
        return {"jobs": len(jobs), "changing": len(moving), "reranked": reranked,
                "into": [{"resume": k, "jobs": n} for k, n in sorted(into.items(), key=lambda kv: -kv[1])],
                "options": _rematch_options(db)}
    out = _set_resume(db, moving, "auto", None)
    run_soon(background, request.app, "ranking")
    return {"jobs": len(jobs), "changing": len(moving), "reranked": len(out["ranking_queued"]), **out}


def _choice(r: Resume) -> dict:
    return {"id": r.id, "name": r.name or f"Version {r.version}", "version": r.version, "is_active": r.is_active,
            "verification_status": r.verification_status, "domain": r.domain, "roles": r.roles or [],
            "domains": resume_matching.groups(r),
            "created_at": r.created_at.isoformat() if r.created_at else None}


@router.get("/api/resumes/choices")
def resume_choices(db: Session = Depends(get_user_db)):
    """Every resume version the account can pick for a search or a job, newest first, with its tags."""
    rows = db.scalars(select(Resume).order_by(Resume.version.desc())).all()
    return {"items": [_choice(r) for r in rows], "catalog": resume_matching.catalog()}


@router.put("/api/resumes/{resume_id}")
def rename_resume(resume_id: int, payload: RenameIn, db: Session = Depends(get_user_db)):
    """Rename a resume version. Only its label changes: its file, data, tags, verification and which resume is
    active stay as they are, and no job is re-matched or re-ranked."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, "A resume needs a name")
    resume = _resume(db, resume_id)
    resume.name = name
    db.commit()
    return {"id": resume.id, "name": resume.name}


def _groups(payload: TagsIn) -> list[dict]:
    if payload.domains is not None:
        return [g.model_dump() for g in payload.domains]
    return [{"domain": payload.domain, "roles": payload.roles}]


@router.put("/api/resumes/{resume_id}/tags")
def tag_resume(resume_id: int, payload: TagsIn, db: Session = Depends(get_user_db)):
    """Tag a resume version with its domains, each with its target roles. New jobs are matched to the best-tagged resume;
    existing jobs change only when re-matched (POST /api/jobs/resume/rematch)."""
    resume = _resume(db, resume_id)
    resume_matching.set_tags(resume, _groups(payload))
    db.commit()
    return _choice(resume)


@router.put("/api/profile/resumes/{document_id}/tags")
def tag_resume_file(document_id: int, payload: TagsIn, db: Session = Depends(get_user_db)):
    """Tag an uploaded file: the tags live on its resume version, so the library and Versions always agree."""
    from app.models import ResumeDocument
    from app.services import resume_verification as rv
    from app.services.user_settings import profile_for

    document = db.get(ResumeDocument, document_id)
    if document is None:
        raise HTTPException(404, "Resume not found")
    resume = rv.version_of_document(db, profile_for(db), document)
    resume_matching.set_tags(resume, _groups(payload))
    db.commit()
    return {"document_id": document.id, **_choice(resume)}
