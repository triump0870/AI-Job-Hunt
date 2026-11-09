"""Structured job description API: what the posting requires, where each requirement came from, and corrections.

The raw description is never written here. Clients send typed structured data, never free-form JSON, and
verification of what the employer said stays with the parser's source text.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_user_db
from app.models import (
    Job,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobStructuredDataChange,
)
from app.scheduler_hooks import run_soon
from app.services import jd_quality, jd_recovery, job_context, job_description
from app.services.job_description import StructuredJobDescriptionIn

router = APIRouter()


class CorrectionIn(BaseModel):
    structured_description: StructuredJobDescriptionIn
    # structured_hash of the version the edit started from (GET .../structured-description).
    base_hash: str = Field(min_length=64, max_length=64)


class ResolveIn(BaseModel):
    decisions: dict[str, Literal["keep", "use_new"]] = Field(default_factory=dict)


def _job(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


def _task_state(db: Session, job: Job) -> dict | None:
    task = db.scalar(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job.id)
                     .order_by(desc(JobDescriptionParseTask.id)).limit(1))
    if task is None:
        return None
    return {"id": task.id, "status": task.status, "reason": task.reason, "attempt_count": task.attempt_count,
            "last_error": task.last_error,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None}


def _detail(db: Session, job: Job) -> dict:
    return {**job_context.summary(db, job), "parse_task": _task_state(db, job),
            "normalized_description": job.normalized_description}


@router.get("/api/jobs/{job_id}/structured-description")
def structured_description(job_id: int, db: Session = Depends(get_user_db)):
    """The structured posting, its requirement evidence and the state of its parse."""
    return _detail(db, _job(db, job_id))


@router.put("/api/jobs/{job_id}/structured-description")
def correct_structured_description(job_id: int, payload: CorrectionIn, db: Session = Depends(get_user_db)):
    """Correct parsed values. Corrected fields are never overwritten by a later parse."""
    job = _job(db, job_id)
    try:
        reconciled = job_description.apply_correction(db, job, payload.structured_description, payload.base_hash)
    except job_description.StaleCorrection as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    db.refresh(job)
    # Corrections that could not be tied to exactly one requirement: saved in the document, but ranking and
    # tailoring still read the requirement rows, so the user is told rather than left to assume.
    return {**_detail(db, job), "unmatched_corrections": reconciled.unmatched}


@router.post("/api/jobs/{job_id}/structured-description/reparse")
def reparse_structured_description(job_id: int, db: Session = Depends(get_user_db)):
    """Parse the posting again. A corrected job gets a candidate version to compare instead of being overwritten."""
    job = _job(db, job_id)
    try:
        out = job_description.reparse(db, job)
    except Exception as exc:
        db.rollback()
        raise HTTPException(400, f"Could not parse this job description: {exc}") from exc
    db.commit()
    db.refresh(job)
    return {**_detail(db, job), "applied": out["applied"], "changes": out["changes"]}


@router.post("/api/jobs/{job_id}/structured-description/reparse/resolve")
def resolve_reparse(job_id: int, payload: ResolveIn, db: Session = Depends(get_user_db)):
    job = _job(db, job_id)
    try:
        out = job_description.resolve_reparse(db, job, payload.decisions)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    db.commit()
    db.refresh(job)
    return {**_detail(db, job), "accepted": out["accepted"], "unmatched_corrections": out["unmatched"]}


@router.get("/api/jobs/{job_id}/requirements")
def requirements(job_id: int, level: str | None = Query(None, max_length=15),
                 include_derived: bool = False, db: Session = Depends(get_user_db)):
    """Every requirement with the employer's own sentence. Derived signals are excluded unless asked for."""
    job = _job(db, job_id)
    rows = job_context.requirements_of(db, job, levels=(level.upper(),) if level else None,
                                       derived=None if include_derived else False)
    return {"job_id": job.id, "status": job.structured_description_status,
            "items": [job_context.requirement_dict(row, with_source=True) for row in rows],
            "derived_label": job_context.DERIVED_LABEL}


@router.get("/api/jobs/{job_id}/structured-description/history")
def history(job_id: int, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
            db: Session = Depends(get_user_db)):
    job = _job(db, job_id)
    query = select(JobStructuredDataChange).where(JobStructuredDataChange.job_id == job.id)
    rows = db.scalars(query.order_by(desc(JobStructuredDataChange.id)).limit(limit).offset(offset)).all()
    return {"job_id": job.id, "items": [
        {"id": row.id, "field_path": row.field_path, "old_value": row.old_value, "new_value": row.new_value,
         "change_source": row.change_source, "created_at": row.created_at.isoformat()} for row in rows]}


@router.get("/api/jobs/{job_id}/interview-insights")
def interview_insights(job_id: int, db: Session = Depends(get_user_db)):
    """Preparation topics for this posting: what it states, and separately what the system infers from it."""
    job = _job(db, job_id)
    context = job_context.build_interview_insights_job_context(db, job)
    return {**context.to_dict(), "stats": context.stats.as_dict()}


# ───────────── description recovery (app.services.jd_recovery) ─────────────

def _recovery(db: Session, job: Job) -> dict:
    history = db.scalars(select(JobDescriptionFetchTask).where(JobDescriptionFetchTask.job_id == job.id)
                         .order_by(desc(JobDescriptionFetchTask.id)).limit(5)).all()
    return {"job_id": job.id, **jd_recovery.status_of(db, job),
            "history": [jd_recovery.task_dict(task) for task in history]}


@router.post("/api/jobs/{job_id}/recover-description", status_code=202)
def recover_description(job_id: int, request: Request, background: BackgroundTasks, force: bool = False,
                        db: Session = Depends(get_user_db)):
    """Queue recovery of this job's description from its posting URL. Nothing is fetched in the request: the
    scheduler's recovery worker does it (started right after this response). A usable description is left alone
    unless `force` is set, and a recovery already queued or running is returned rather than duplicated."""
    job = job_description.lock_job(db, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if not (job.url or job.source_url or "").strip():
        raise HTTPException(422, "This job has no posting URL to recover the description from — paste it instead")
    if jd_quality.is_job_description_usable(job) and not force:
        db.rollback()
        return {**_recovery(db, job), "queued": False, "message": "This job already has a usable description"}
    try:
        task = jd_recovery.enqueue(db, job, reason="user_request", force=True)
        db.commit()
    except IntegrityError:  # queued by another request at the same moment
        db.rollback()
        task = jd_recovery.active_task(db, job.id)
    run_soon(background, request.app, "jd_recovery")
    return {**_recovery(db, job), "queued": task is not None, "message": "Recovery queued"}


@router.post("/api/jobs/recover-descriptions", status_code=202)
def recover_descriptions(request: Request, background: BackgroundTasks, db: Session = Depends(get_user_db)):
    """Queue recovery for every job with no description. Nothing is fetched in the request. Says what was queued and
    what was left out, and why."""
    from app.workspace import excluded_companies

    out = jd_recovery.enqueue_missing(db, excluded_companies=excluded_companies(db))
    if out["queued"]:
        run_soon(background, request.app, "jd_recovery")
    return out


@router.get("/api/jobs/{job_id}/description-recovery")
def description_recovery(job_id: int, db: Session = Depends(get_user_db)):
    """Where recovery of this job's description stands, with its recent attempts."""
    return _recovery(db, _job(db, job_id))


@router.get("/api/system/jd-recovery")
def jd_recovery_diagnostics(db: Session = Depends(get_user_db)):
    """What description recovery can use right now: HTTP, the browser, the model. No secrets or file paths."""
    return jd_recovery.diagnostics(db, db.info.get("user_id"))
