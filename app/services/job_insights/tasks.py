"""The Job Insights research queue: one active task per job, claimed with a lease, retried with backoff.

    approval (workflow.approve_job) ─┐
    Find insights / Refresh (API)   ─┼→ JobInsightTask PENDING   ← nothing here waits for research
    backlog sweep (approved jobs)   ─┘        ↓ scheduler job `job_insights` (scheduled_jobs.run_live)
                                         claim (SKIP LOCKED + guarded UPDATE) → service.process

The same claim, lease and run-now rules as JD recovery (app.services.jd_recovery): two app instances never hold the
same task, a task whose worker died is retried or failed once its attempts are used, and every write after a network
or model call checks the lease first (`held`).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ACTIVE_JOB_INSIGHT_TASK_STATUSES,
    Application,
    Job,
    JobInsight,
    JobInsightTask,
)
from app.services import worker_settings
from app.services.lifecycle import ErrorCategory
from app.settings import get_settings

log = logging.getLogger("jobhunt.job_insights")

PENDING, PROCESSING, COMPLETED, PARTIAL, FAILED, SKIPPED = (
    "PENDING", "PROCESSING", "COMPLETED", "PARTIAL", "FAILED", "SKIPPED")
AUTO_APPROVED, USER_REQUEST, REFRESH, BACKLOG = "AUTO_APPROVED", "USER_REQUEST", "REFRESH", "BACKLOG"
BEFORE_APPROVAL = "BEFORE_APPROVAL"
FULL, APPLICATION_FORM = "FULL", "APPLICATION_FORM"  # JobInsightTask.scope
KIND = "insights"  # the Running workers kind (app.services.workers, app.services.worker_settings)
WORKER = "JOB_INSIGHTS"
MAX_ERROR_LENGTH = 2000
# Applications whose job is worth researching: approved, or already submitted.
RESEARCHED_STATUSES = ("approved", "applied")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _pending_in_session(db: Session, job_id: int) -> JobInsightTask | None:
    # Sessions do not autoflush: a task added earlier in this transaction is not visible to a query yet.
    return next((t for t in db.new if isinstance(t, JobInsightTask) and t.job_id == job_id
                 and t.status in ACTIVE_JOB_INSIGHT_TASK_STATUSES), None)


def active_task(db: Session, job_id: int) -> JobInsightTask | None:
    return db.scalar(select(JobInsightTask).where(JobInsightTask.job_id == job_id,
                                                  JobInsightTask.status.in_(ACTIVE_JOB_INSIGHT_TASK_STATUSES)))


def latest_task(db: Session, job_id: int) -> JobInsightTask | None:
    return db.scalar(select(JobInsightTask).where(JobInsightTask.job_id == job_id)
                     .order_by(JobInsightTask.id.desc()).limit(1))


def enqueue(db: Session, job: Job, *, reason: str = AUTO_APPROVED, force: bool = False,
            now: datetime | None = None, scope: str = FULL) -> JobInsightTask | None:
    """Queue research for this job. Flushes, does not commit.

    Returns the job's active task when there is one: a second request (two Refresh clicks, approval racing a
    sweep) never creates a second task; a person's Refresh on a queued automatic task marks it to bypass the cache.
    Without `force`, a job that already has research is left alone. `scope` APPLICATION_FORM queues only the drafts
    for pasted form questions; a queued full run covers them, and a full request widens a queued form-only task."""
    now = now or utcnow()
    existing = _pending_in_session(db, job.id) or active_task(db, job.id)
    if existing is not None:
        if force and existing.status == PENDING and scope == FULL:
            existing.force_refresh, existing.reason, existing.scope = True, reason, FULL
        return existing
    if not force and scope == FULL:
        insight = db.scalar(select(JobInsight).where(JobInsight.job_id == job.id))
        if insight is not None and insight.refreshed_at is not None:
            return None
    application_id = db.scalar(select(Application.id).where(Application.job_id == job.id))
    task = JobInsightTask(user_id=job.user_id, job_id=job.id, application_id=application_id, status=PENDING,
                          reason=reason, force_refresh=force and scope == FULL, scope=scope, attempt_count=0,
                          created_at=now, updated_at=now)
    db.add(task)
    db.flush()
    return task


def on_approved(db: Session, job: Job) -> None:
    """A job was just approved: queue its research. Never fails or delays the approval itself."""
    if not get_settings().job_insights_enabled:
        return
    try:
        with db.begin_nested():
            enqueue(db, job, reason=AUTO_APPROVED)
    except IntegrityError:
        pass  # another writer queued it at the same moment
    except Exception:
        log.exception("Could not queue job insights for job %s", job.id)


def on_packet_ready(db: Session, job: Job) -> bool:
    """A reviewed packet just reached Approvals: research the job now when the account turned "Research before
    approval" on (a minimum fit above 0) and the job's fit reaches it. Off by default: research then waits for the
    approval. Never fails the packet; does not commit. True when a task was queued."""
    if not get_settings().job_insights_enabled or job.fit_score is None:
        return False
    try:
        threshold = int(worker_settings.get(db, job.user_id, KIND, "before_approval_min_fit") or 0)
        if threshold <= 0 or job.fit_score < threshold:
            return False
        with db.begin_nested():
            return enqueue(db, job, reason=BEFORE_APPROVAL) is not None
    except IntegrityError:
        return False  # another writer queued it at the same moment
    except Exception:
        log.exception("Could not queue research before approval for job %s", job.id)
        return False


def sweep(db: Session, limit: int | None = None, *, now: datetime | None = None) -> dict[str, int]:
    """Queue approved jobs that were never researched (approved before this feature, or a queueing that failed).
    A job with any task at all is left alone: a person who stopped it decides when it runs again. Commits."""
    if not get_settings().job_insights_enabled:
        return {"queued": 0}
    limit = max(1, limit or get_settings().job_insights_enqueue_batch_size)
    jobs = db.scalars(select(Job).join(Application, Application.job_id == Job.id).where(
        Application.status.in_(RESEARCHED_STATUSES), Job.user_id.is_not(None),
        ~exists(select(JobInsightTask.id).where(JobInsightTask.job_id == Job.id)),
        ~exists(select(JobInsight.id).where(JobInsight.job_id == Job.id, JobInsight.refreshed_at.is_not(None))))
        .order_by(Job.id.desc()).limit(limit)).all()
    paused = worker_settings.paused(db, KIND, {job.user_id for job in jobs})
    queued = 0
    for job in jobs:
        if job.user_id in paused:
            continue
        try:
            with db.begin_nested():
                queued += int(enqueue(db, job, reason=BACKLOG, now=now) is not None)
        except IntegrityError:
            continue
    db.commit()
    return {"queued": queued}


# ───────────── claiming ─────────────

@dataclass(frozen=True)
class InsightClaim:
    task_id: int
    lock_token: str
    worker_id: str
    user_id: int
    job_id: int
    run_now: bool = False  # asked to run now from Running workers: skips the provider cooldown


def claim(db: Session, *, worker_id: str, limit: int, now: datetime | None = None,
          lease_seconds: int | None = None) -> list[InsightClaim]:
    """Lease up to `limit` due tasks. Commits. Safe across app instances (see jd_recovery.claim)."""
    now = now or utcnow()
    lease = timedelta(seconds=lease_seconds or get_settings().job_insights_lease_seconds)
    t = JobInsightTask
    query = (select(t.id).where(t.status == PENDING, or_(t.next_attempt_at.is_(None), t.next_attempt_at <= now))
             .order_by(t.run_now_at.is_(None), t.next_attempt_at.asc().nulls_first(), t.id).limit(max(1, limit)))
    stopped = worker_settings.paused(db, KIND)
    if stopped:
        query = query.where(or_(t.user_id.not_in(stopped), t.run_now_at.is_not(None)))
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True, of=t)
    claimed: list[InsightClaim] = []
    for task_id in list(db.scalars(query).all()):
        token = uuid.uuid4().hex
        won = db.execute(update(t).where(t.id == task_id, t.status == PENDING)
                         .values(status=PROCESSING, locked_by=worker_id[:200], lock_token=token,
                                 lock_expires_at=now + lease, attempt_count=t.attempt_count + 1, started_at=now,
                                 next_attempt_at=None, updated_at=now)
                         .execution_options(synchronize_session=False)).rowcount
        if won != 1:
            continue
        owner, job_id, run_now = db.execute(select(t.user_id, t.job_id, t.run_now_at).where(t.id == task_id)).one()
        if run_now is not None:
            db.execute(update(t).where(t.id == task_id).values(run_now_at=None)
                       .execution_options(synchronize_session=False))
        claimed.append(InsightClaim(task_id, token, worker_id[:200], owner, job_id, run_now is not None))
    db.commit()
    return claimed


def recover_expired(db: Session, now: datetime | None = None) -> int:
    """Tasks whose worker died go back in the queue, or fail once their attempts are used up. Commits."""
    now = now or utcnow()
    rows = db.scalars(select(JobInsightTask).where(JobInsightTask.status == PROCESSING,
                                                   JobInsightTask.lock_expires_at < now)).all()
    for task in rows:
        if task.attempt_count >= worker_settings.get(db, task.user_id, KIND, "max_attempts"):
            finish(task, FAILED, now, "Interrupted: the worker stopped while researching", ErrorCategory.LEASE_EXPIRED)
        else:
            release(task, now, "Interrupted: the worker stopped while researching", retry_in=0,
                    category=ErrorCategory.LEASE_EXPIRED)
    db.commit()
    return len(rows)


def _clear_lease(task: JobInsightTask) -> None:
    task.locked_by = task.lock_token = task.lock_expires_at = None


def finish(task: JobInsightTask, status: str, now: datetime, error: str | None = None,
           category: ErrorCategory | None = None) -> None:
    _clear_lease(task)
    task.status, task.completed_at, task.updated_at, task.next_attempt_at = status, now, now, None
    task.last_error = (error or "")[:MAX_ERROR_LENGTH] or None
    task.error_category = category if status in (FAILED, SKIPPED) else None


def release(task: JobInsightTask, now: datetime, error: str, *, retry_in: float | None = None,
            retry_seconds: int | None = None, category: ErrorCategory = ErrorCategory.TRANSIENT) -> None:
    """Back in the queue for a later attempt; the wait doubles with each attempt."""
    _clear_lease(task)
    base = retry_seconds if retry_seconds is not None else get_settings().job_insights_retry_seconds
    delay = base * (2 ** max(0, task.attempt_count - 1)) if retry_in is None else retry_in
    task.status, task.updated_at = PENDING, now
    task.next_attempt_at = now + timedelta(seconds=delay)
    task.last_error, task.error_category = error[:MAX_ERROR_LENGTH], category


def held(db: Session, claim_: InsightClaim) -> JobInsightTask | None:
    """The task, only while this worker still holds its lease. On PostgreSQL the row stays locked until the caller's
    transaction ends, so Kill (which locks it too) cannot take the lease between this check and the commit."""
    query = select(JobInsightTask).where(
        JobInsightTask.id == claim_.task_id, JobInsightTask.status == PROCESSING,
        JobInsightTask.lock_token == claim_.lock_token).execution_options(populate_existing=True)
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    return db.scalar(query)
