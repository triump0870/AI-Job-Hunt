"""The qualification graph's queue: dispatch, claim, run, settle, shadow comparison and hand-back (Phase 3).

    QUALIFICATION_GRAPH_MODE=on      discovery.admit, hunter._save, ranker.sweep and domain_match.keep call
                                     `dispatch` instead of starting recovery, parsing and ranking themselves; the
                                     `qualification` scheduler job claims a task, runs (or resumes) its graph thread
                                     and settles the task from how the thread ended
    QUALIFICATION_GRAPH_MODE=shadow  the current path is untouched; `shadow_sweep` picks, per account, a bounded
                                     number of jobs it recently decided and runs the graph read-only over each, then
                                     stores the old-vs-new comparison on the task (`parity`)
    QUALIFICATION_GRAPH_MODE=off     (the default) nothing new runs; tasks left from `on` are handed back to the
                                     current path (`release`)

PostgreSQL is the only queue: a task row per run, claimed with SKIP LOCKED and a guarded UPDATE, leased and
renewed by `scheduled_jobs.run_live`. A task whose job has an active recovery, parse or ranking task is not
claimable, because its run waits for that work. Waiting costs no attempt. Kill stops the task and its thread, and no
sweep queues the job again until a person asks (Retry, Run now). A run's writes are lease-guarded twice: by the
thread (app.agents.effects) inside nodes, and by the task's token when it is settled.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, event, exists, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.models import (
    ACTIVE_JD_FETCH_STATUSES,
    ACTIVE_JD_PARSE_STATUSES,
    ACTIVE_JOB_CLASSIFICATION_STATUSES,
    ACTIVE_QUALIFICATION_TASK_STATUSES,
    ACTIVE_RANKING_TASK_STATUSES,
    AgentThread,
    Job,
    JobClassificationTask,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobQualificationTask,
    JobRankingTask,
    WorkflowTransition,
)
from app.services import backoff, system_log, worker_settings
from app.services.lifecycle import STOPPED_BY_USER
from app.services.redaction import redact_text
from app.settings import get_settings

log = logging.getLogger("jobhunt.qualification")
SessionFactory = Callable[[], Session]
ON, SHADOW = "ON", "SHADOW"
PENDING, PROCESSING, COMPLETED, FAILED, SKIPPED = "PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED"
OUTCOMES = ("completed", "waiting", "retrying", "failed", "stopped", "busy", "lost")
ROUTED_TO_LEGACY = "ROUTED_TO_LEGACY"
KIND = "qualification"
ERROR_LIMIT = 300
# Compared on every shadow run (predicted_* against actual_*).
PARITY_FIELDS = ("admission", "jd", "filter", "decision")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ───────────── mode and routing ─────────────

def mode() -> str:
    return get_settings().qualification_mode


def _postgres(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def routes(db: Session) -> bool:
    """True when new and waiting jobs are qualified by the graph: mode on, agent graphs on, PostgreSQL."""
    return mode() == "on" and _postgres(db)


def _held_until_asked() -> Any:
    """The job's latest ON run ended where only a person moves it on: stopped by them, or failed its attempts. A
    sweep never starts another run behind it (that would make the attempt limit unbounded); Retry and Run now do."""
    q, newer = JobQualificationTask, aliased(JobQualificationTask)
    latest = ~exists().where(newer.job_id == q.job_id, newer.mode == ON, newer.id > q.id)
    return and_(latest, or_(q.status == FAILED, and_(q.status == SKIPPED, q.last_error == STOPPED_BY_USER)))


def _owned_clause(job_id: Any) -> Any:
    """An ON task that holds the job: one still running, or the latest one a person stopped or that failed."""
    q = JobQualificationTask
    return exists().where(q.job_id == job_id, q.mode == ON,
                          or_(q.status.in_(ACTIVE_QUALIFICATION_TASK_STATUSES), _held_until_asked()))


def unowned(job_id: Any = Job.id) -> Any:
    """SQL: no ON task holds this job (sweeps use it so they never queue a job the graph is driving)."""
    return ~_owned_clause(job_id)


def untouched(job_id: Any = Job.id) -> Any:
    """SQL: the graph never ran this job. Backlog sweeps leave a job it ran alone: it already decided whether the
    job needed recovery or a parse (a stub whose recovery failed is not parsed behind its back)."""
    q = JobQualificationTask
    return ~exists().where(q.job_id == job_id, q.mode == ON)


def owns(db: Session, job: Job) -> bool:
    """The graph drives this job: recovery and parsing hand it back to the graph, not to the ranker."""
    if not routes(db):
        return False
    pending = any(isinstance(t, JobQualificationTask) and t.job_id == job.id and t.mode == ON
                  and t.status in ACTIVE_QUALIFICATION_TASK_STATUSES for t in db.new)
    return pending or bool(db.scalar(select(_owned_clause(job.id))))


def active_task(db: Session, job_id: int, task_mode: str = ON) -> JobQualificationTask | None:
    q = JobQualificationTask
    pending = next((t for t in db.new if isinstance(t, q) and t.job_id == job_id and t.mode == task_mode
                    and t.status in ACTIVE_QUALIFICATION_TASK_STATUSES), None)
    return pending or db.scalar(select(q).where(q.job_id == job_id, q.mode == task_mode,
                                                q.status.in_(ACTIVE_QUALIFICATION_TASK_STATUSES)).limit(1))


def dispatch(db: Session, job: Job, *, reason: str = "new", force: bool = False,
             now: datetime | None = None) -> JobQualificationTask | None:
    """Queue the job for the graph, or return its active task. Never revives a run a person stopped, or one that
    failed its attempts, unless they ask again (`force`). Flushes in a savepoint, does not commit."""
    if not routes(db) and not force:
        return None
    existing = active_task(db, job.id)
    if existing is not None:
        return existing
    q = JobQualificationTask
    if not force:
        held = db.scalar(select(q.id).where(q.job_id == job.id, q.mode == ON, _held_until_asked()).limit(1))
        if held is not None:
            return None
    now = now or utcnow()
    task = q(user_id=job.user_id, job_id=job.id, mode=ON, status=PENDING, reason=reason[:40], attempt_count=0,
             paid_calls=0, created_at=now, updated_at=now)
    try:
        with db.begin_nested():
            db.add(task)
            db.flush()
    except IntegrityError:  # another writer queued it at the same moment: one task
        return active_task(db, job.id)
    _nudge_after_commit(db)
    return task


def _nudge_after_commit(db: Session) -> None:
    """Wake the qualification job once the task it should claim is committed (a nudge before then could find nothing
    and use up the wake). Only a hint: a rolled-back transaction leaves at most one spare nudge on the next commit."""
    if db.info.get("qualification_nudge"):
        return
    db.info["qualification_nudge"] = True

    def wake(session: Session) -> None:
        session.info.pop("qualification_nudge", None)
        from app import scheduler_hooks

        scheduler_hooks.nudge(KIND)

    event.listen(db, "after_commit", wake, once=True)


# ───────────── claiming ─────────────

@dataclass(frozen=True)
class QualificationClaim:
    task_id: int
    token: str
    worker_id: str
    user_id: int
    job_id: int
    mode: str
    run_now: bool = False
    allow_paid: bool = False


def _busy() -> Any:
    """The job has a domain check, recovery, parsing or ranking in progress: its run waits for it."""
    q = JobQualificationTask
    return or_(
        exists().where(JobClassificationTask.job_id == q.job_id, JobClassificationTask.mode == "ON",
                       JobClassificationTask.status.in_(ACTIVE_JOB_CLASSIFICATION_STATUSES)),
        exists().where(JobDescriptionFetchTask.job_id == q.job_id,
                       JobDescriptionFetchTask.status.in_(ACTIVE_JD_FETCH_STATUSES)),
        exists().where(JobDescriptionParseTask.job_id == q.job_id,
                       JobDescriptionParseTask.status.in_(ACTIVE_JD_PARSE_STATUSES)),
        exists().where(JobRankingTask.job_id == q.job_id, JobRankingTask.status.in_(ACTIVE_RANKING_TASK_STATUSES)))


def step_workers(db: Session, user_id: int, rows: list[dict[str, Any]]) -> dict[int, str]:
    """Which model worker each active run is at ({task id: worker type}), for Running workers' Model & route. Read only:
    the worker it waits for, else the worker its next step hands to. The graph itself never calls a model.

    A run before the parse step goes to the parser when its description still needs parsing, as `parse_jd` decides.
    Recovery shows only while the run waits for it: its model is a fallback on ambiguous pages."""
    from app.agents import qualification as graph
    from app.services import job_description

    waits = {"JD_RECOVERY": "JD_RECOVERY", "JD_PARSE": "JD_PARSER", "RANKING": "RANKER"}
    before_parse = (None, graph.VALIDATE_JOB, graph.ADMISSION, graph.CHECK_JD, graph.RECOVER_JD)
    open_ = [r for r in rows if waits.get(r.get("waiting_for")) is None and r.get("step") in before_parse]
    jobs = {j.id: j for j in db.execute(select(
        Job.id, Job.description, Job.structured_description_status, Job.structured_description_hash).where(
        Job.user_id == user_id, Job.id.in_({r["job_id"] for r in open_}))).all()} if open_ else {}
    parsing = get_settings().jd_parse_enabled
    out = {}
    for r in rows:
        job = jobs.get(r["job_id"])
        out[r["task_id"]] = waits.get(r.get("waiting_for")) or (
            "JD_PARSER" if parsing and job is not None and job_description.needs_parse(job) else "RANKER")
    return out


def claim(db: Session, *, worker_id: str, limit: int, task_mode: str = ON, now: datetime | None = None,
          lease_seconds: int | None = None) -> list[QualificationClaim]:
    """Lease up to `limit` due PENDING tasks of `task_mode`. Run-now first; paused accounts are held unless a task was
    asked to run now. Commits. PostgreSQL (SKIP LOCKED) and a guarded UPDATE: two workers never get the same task."""
    now = now or utcnow()
    lease = lease_seconds or get_settings().qualification_lease_seconds
    q = JobQualificationTask
    paused = worker_settings.paused(db, KIND)
    query = (select(q.id, q.user_id, q.job_id, q.run_now_at)
             .where(q.status == PENDING, q.mode == task_mode, or_(q.next_attempt_at.is_(None), q.next_attempt_at <= now))
             .order_by(q.run_now_at.is_(None), q.id).limit(max(1, limit)))
    if task_mode == ON:
        query = query.where(~_busy())
    if paused:
        query = query.where(or_(q.run_now_at.is_not(None), q.user_id.not_in(paused)))
    if _postgres(db):
        query = query.with_for_update(skip_locked=True, of=q)
    rows = db.execute(query).all()
    claims: list[QualificationClaim] = []
    for row in rows:
        token = str(uuid.uuid4())
        won = db.execute(update(q).where(q.id == row.id, q.status == PENDING)
                         .values(status=PROCESSING, lock_token=token, locked_by=worker_id[:200], locked_at=now,
                                 lock_expires_at=now + timedelta(seconds=lease), run_now_at=None,
                                 attempt_count=q.attempt_count + 1, started_at=now, updated_at=now)
                         .execution_options(synchronize_session=False)).rowcount
        if won:
            claims.append(QualificationClaim(row.id, token, worker_id, row.user_id, row.job_id, task_mode,
                                             run_now=row.run_now_at is not None))
    if task_mode == SHADOW and claims:
        claims = [_reserve_paid(db, c, now) for c in claims]
    db.commit()
    return claims


def _reserve_paid(db: Session, c: QualificationClaim, now: datetime) -> QualificationClaim:
    """A shadow run may rank a job the current path never ranked only within the account's paid budget for the day.
    The reservation is the task's own `paid_calls` (given back on settle when unused), counted under a per-account
    transaction lock, so two runs never both take the last one."""
    from app.services import server_settings

    limit = server_settings.value("qualification_shadow_paid_daily_limit")
    if limit <= 0:
        return c
    from app.models import JobRanking

    if db.scalar(select(func.count()).select_from(JobRanking).where(JobRanking.job_id == c.job_id)):
        return c  # a stored ranking answers it: no call needed
    if _postgres(db):
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key(f"qualification-paid:{c.user_id}")})
    q = JobQualificationTask
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    used = db.scalar(select(func.coalesce(func.sum(q.paid_calls), 0)).where(
        q.user_id == c.user_id, q.mode == SHADOW, q.created_at >= day)) or 0
    if used >= limit:
        return c
    db.execute(update(q).where(q.id == c.task_id).values(paid_calls=1).execution_options(synchronize_session=False))
    return QualificationClaim(c.task_id, c.token, c.worker_id, c.user_id, c.job_id, c.mode, c.run_now, True)


def _lock_key(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


def renew(sessions: SessionFactory, lease_seconds: int | None = None) -> Callable[[list[Any]], set[int]]:
    from app.services import scheduled_jobs

    return scheduled_jobs.renew(sessions, JobQualificationTask,
                                lease_seconds or get_settings().qualification_lease_seconds, token="token")


def recover_expired(db: Session, now: datetime | None = None) -> int:
    """PROCESSING tasks whose worker died: back to PENDING, or FAILED when their attempts are used up. Commits."""
    now = now or utcnow()
    q = JobQualificationTask
    rows = db.scalars(select(q).where(q.status == PROCESSING, q.lock_expires_at < now).limit(200)).all()
    for task in rows:
        limit = worker_settings.get(db, task.user_id, KIND, "max_attempts")
        _clear_lease(task)
        if task.attempt_count >= limit:
            task.status, task.completed_at, task.outcome = FAILED, now, "FAILED"
            task.last_error = "The worker stopped answering (lease expired) on its last attempt"
        else:
            task.status, task.next_attempt_at = PENDING, now
    db.commit()
    return len(rows)


def _clear_lease(task: JobQualificationTask) -> None:
    task.lock_token = task.locked_by = task.locked_at = task.lock_expires_at = None


# ───────────── running one task ─────────────

def _held(db: Session, c: QualificationClaim) -> JobQualificationTask | None:
    q = JobQualificationTask
    query = select(q).where(q.id == c.task_id, q.user_id == c.user_id)
    if _postgres(db):
        query = query.with_for_update()
    task = db.scalar(query.execution_options(populate_existing=True))
    if task is None or task.status != PROCESSING or task.lock_token != c.token:
        return None
    return task


def process(sessions: SessionFactory, c: QualificationClaim, *, saver: Any | None = None,
            hold: Callable[[str], None] | None = None) -> str:
    """Start or resume the task's graph thread, run it until it ends or waits, and settle the task. Never raises."""
    from app.services import tracing

    try:
        with tracing.trace("qualification", workflow_type="job", workflow_entity_id=c.job_id, job_id=c.job_id,
                           user_id=c.user_id), backoff.bypass(c.run_now):
            return _process(sessions, c, saver=saver, hold=hold)
    except Exception as exc:
        log.warning("qualification task %s failed", c.task_id, exc_info=True)
        return _settle_error(sessions, c, exc)


def _process(sessions: SessionFactory, c: QualificationClaim, *, saver: Any | None,
             hold: Callable[[str], None] | None) -> str:
    from app.agents import qualification as graph
    from app.agents import runtime

    with sessions() as db:
        db.info["user_id"] = c.user_id
        task = _held(db, c)
        if task is None:
            return "lost"
        thread = db.get(AgentThread, task.agent_thread_id) if task.agent_thread_id else None
        if thread is None:
            # Unique per task and attempt. The creation time keeps a reused id (a restored database) from ever
            # reaching another run's checkpoints.
            key = f"qualification:{task.job_id}:{task.id}:{task.created_at.isoformat()}:a{task.attempt_count}"
            thread = runtime.start(db, graph.GRAPH, {"job_id": task.job_id, "task_id": task.id, "mode": task.mode,
                                                     "reason": task.reason}, idempotency_key=key)
            task = _held(db, c)
            if task is None:
                return "lost"
            task.agent_thread_id = thread.id
            db.commit()
        status, thread_pk = thread.status, thread.id
        if status in ("COMPLETED", "FAILED", "STOPPED"):
            return _settle(sessions, c, thread_pk)  # it ended last time; only the settle was lost
        command = None
        if status == "INTERRUPTED":
            try:
                run, command = runtime.resume(db, thread_pk, {"action": "continue"}, c.worker_id, by_worker=True)
            except runtime.ThreadBusy:
                run = None
        else:  # QUEUED, or RUNNING left by a crashed worker (its lease must have expired)
            run = runtime.claim(db, thread_pk, c.user_id, ("QUEUED", "RUNNING"), c.worker_id)
        if run is None:
            return _release_busy(db, c, thread_pk)
    runtime.execute(sessions, run, command, saver=saver, hold=hold,
                    extra={"task_id": c.task_id, "allow_paid": c.allow_paid})
    return _settle(sessions, c, thread_pk)


def _release_busy(db: Session, c: QualificationClaim, thread_pk: int) -> str:
    """Another run still holds the thread (a crashed worker's lease has not expired): try again after it does."""
    thread = db.get(AgentThread, thread_pk)
    task = _held(db, c)
    if task is None:
        return "lost"
    _clear_lease(task)
    task.status, task.attempt_count = PENDING, max(0, task.attempt_count - 1)
    task.next_attempt_at = (thread.lock_expires_at if thread is not None and thread.lock_expires_at else utcnow()) \
        + timedelta(seconds=1)
    db.commit()
    return "busy"


def _settle(sessions: SessionFactory, c: QualificationClaim, thread_pk: int) -> str:
    """Write how the thread ended onto the task, only while this claim still holds it."""
    now = utcnow()
    with sessions() as db:
        db.info["user_id"] = c.user_id
        task = _held(db, c)
        if task is None:
            return "lost"  # stopped from Running workers, or taken over: nothing more is written
        thread = db.get(AgentThread, thread_pk)
        status = thread.status if thread is not None else "STOPPED"
        _clear_lease(task)
        if status == "INTERRUPTED":
            interrupt = thread.interrupt or {}
            task.status, task.attempt_count = PENDING, max(0, task.attempt_count - 1)  # waiting uses no attempt
            task.waiting_for = str(interrupt.get("wait") or "")[:40] or None
            task.step = str(interrupt.get("step") or "")[:60] or task.step
            task.next_attempt_at, task.last_error = None, None
            db.commit()
            return "waiting"
        if status == "COMPLETED":
            result = thread.result or {}
            task.status, task.completed_at, task.waiting_for, task.last_error = COMPLETED, now, None, None
            task.step = str(result.get("step") or "")[:60] or None
            task.outcome = str(result.get("outcome") or "")[:30] or None
            task.result_code = str(result.get("code") or "")[:60] or None
            if task.mode == SHADOW:
                task.paid_calls = int(result.get("paid_calls") or 0)
                task.parity = parity(result)
            db.commit()
            if task.mode == SHADOW and task.parity and not task.parity.get("match"):
                _log_mismatch(task)
            elif task.outcome in ("QUALIFIED",):
                _after_qualified(sessions, c)
            return "completed"
        if status == "STOPPED":
            task.status, task.completed_at = SKIPPED, now
            task.last_error = (thread.last_error if thread is not None else None) or STOPPED_BY_USER
            task.outcome = "STOPPED"
            db.commit()
            return "stopped"
        # FAILED: a node raised (database error, a bug). Retried on a new thread, never more than the attempts.
        message = redact_text((thread.last_error if thread is not None else "") or "graph run failed", ERROR_LIMIT)
        return _retry_or_fail(db, task, message, now)


def _retry_or_fail(db: Session, task: JobQualificationTask, message: str, now: datetime) -> str:
    limit = worker_settings.get(db, task.user_id, KIND, "max_attempts")
    task.last_error, task.agent_thread_id, task.waiting_for = message, None, None
    if task.attempt_count >= limit:
        task.status, task.completed_at, task.outcome = FAILED, now, "FAILED"
        db.commit()
        return "failed"
    wait = worker_settings.get(db, task.user_id, KIND, "retry_seconds") * max(1, task.attempt_count)
    task.status, task.next_attempt_at = PENDING, now + timedelta(seconds=wait)
    db.commit()
    return "retrying"


def _settle_error(sessions: SessionFactory, c: QualificationClaim, exc: Exception) -> str:
    try:
        with sessions() as db:
            db.info["user_id"] = c.user_id
            task = _held(db, c)
            if task is None:
                return "lost"
            _clear_lease(task)
            return _retry_or_fail(db, task, redact_text(f"{type(exc).__name__}: {exc}", ERROR_LIMIT), utcnow())
    except Exception:
        log.warning("could not settle qualification task %s", c.task_id, exc_info=True)
        return "lost"


def _after_qualified(sessions: SessionFactory, c: QualificationClaim) -> None:
    """Nothing to do: the ranking worker already handed a qualified job to automatic tailoring
    (ranker.process → auto_tailor.after_qualified). Kept so a future graph step has one place to hook in."""


# ───────────── stopping ─────────────

def stop_threads(db: Session, thread_ids: list[int], now: datetime | None = None, note: str = STOPPED_BY_USER) -> int:
    """Stop graph threads without opening the checkpointer: a run in progress loses its lease and writes nothing
    more. Does not commit."""
    ids = [i for i in thread_ids if i]
    if not ids:
        return 0
    now = now or utcnow()
    return db.execute(update(AgentThread).where(AgentThread.id.in_(ids),
                                                AgentThread.status.in_(("QUEUED", "RUNNING", "INTERRUPTED")))
                      .values(status="STOPPED", last_error=note, completed_at=now, updated_at=now, lock_token=None,
                              lock_expires_at=None, interrupt=None)
                      .execution_options(synchronize_session=False)).rowcount


def on_stopped(db: Session, task: JobQualificationTask, now: datetime) -> None:
    """Kill from Running workers: the task is already SKIPPED with STOPPED_BY_USER; stop its thread too, and the
    ranking it handed the job to, so no model is asked and nothing decides the job after the person stopped it.

    The ranking is stopped exactly as Kill on Ranking stops it (the job goes SKIPPED with STOPPED_BY_USER until a
    person asks again; Retry here puts it back). Recovery and parsing tasks are left to finish: they only read the
    posting and never move the job."""
    from app.services import workers

    task.outcome, task.waiting_for = "STOPPED", None
    if task.agent_thread_id:
        stop_threads(db, [task.agent_thread_id], now)
    r = JobRankingTask
    query = select(r).where(r.job_id == task.job_id, r.user_id == task.user_id,
                            r.status.in_(ACTIVE_RANKING_TASK_STATUSES))
    if _postgres(db):
        query = query.with_for_update()
    for row in db.scalars(query).all():
        workers._stop(db, workers.KINDS["ranking"], row, task.user_id, now)


def on_job_deleted(db: Session, job_id: int) -> None:
    """A job is being deleted (job_removal.delete_job): stop its runs and delete its task rows. Does not commit."""
    q = JobQualificationTask
    rows = db.scalars(select(q).where(q.job_id == job_id)).all()
    stop_threads(db, [r.agent_thread_id for r in rows], note="The job was deleted")
    for row in rows:
        db.delete(row)


def requeue(db: Session, task: JobQualificationTask, user_id: int) -> JobQualificationTask:
    """Retry and Run now: a new task (and so a new thread) for the job, past a stop the person made."""
    from app.services.workers import WorkerError

    job = db.scalar(select(Job).where(Job.id == task.job_id, Job.user_id == user_id))
    if job is None:
        raise WorkerError("The job no longer exists", 404)
    fresh = dispatch(db, job, reason="user_request", force=True)
    if fresh is None:
        raise WorkerError("This job could not be queued for qualification")
    db.flush()
    return fresh


# ───────────── off: hand tasks back to the current path ─────────────

def release(db: Session, now: datetime | None = None, limit: int = 200) -> int:
    """Mode off with ON tasks left: end them (ROUTED_TO_LEGACY) and stop their threads. Their jobs carry on through
    the current path: ranker.sweep takes jobs before ranking, and recovery, parse and ranking tasks the graph queued
    are ordinary rows their workers finish. Commits."""
    now = now or utcnow()
    q = JobQualificationTask
    rows = db.scalars(select(q).where(q.mode == ON, q.status == PENDING).order_by(q.id).limit(limit)).all()
    stop_threads(db, [r.agent_thread_id for r in rows], now, note="Handed back to the current path")
    for task in rows:
        task.status, task.completed_at, task.result_code = SKIPPED, now, ROUTED_TO_LEGACY
        task.waiting_for = None
    db.commit()
    return len(rows)


# ───────────── shadow ─────────────

def shadow_sweep(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Queue SHADOW runs for jobs the current path decided recently (qualified, archived, skipped or hidden), up to
    each account's daily limit. Read-only runs, so this never changes a job. Commits."""
    from app.services import server_settings

    now = now or utcnow()
    daily = server_settings.value("qualification_shadow_daily_limit")
    if daily <= 0:
        return {"queued": 0}
    since = now - timedelta(hours=max(1, get_settings().qualification_shadow_lookback_hours))
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    q = JobQualificationTask
    no_shadow = ~exists().where(q.job_id == Job.id, q.mode == SHADOW)
    decided = exists().where(WorkflowTransition.job_id == Job.id, WorkflowTransition.created_at >= since,
                             WorkflowTransition.to_state.in_(("QUALIFIED", "ARCHIVED", "SKIPPED")))
    hidden = and_(Job.domain_match == "NOT_MATCHING", Job.domain_checked_at >= since)
    used = dict(db.execute(select(q.user_id, func.count()).where(q.mode == SHADOW, q.created_at >= day)
                           .group_by(q.user_id)).all())
    queued = 0
    candidates = db.execute(select(Job.id, Job.user_id).where(Job.user_id.is_not(None), no_shadow,
                                                              or_(decided, hidden))
                            .order_by(Job.id.desc()).limit(2000)).all()
    for job_id, user_id in candidates:
        if used.get(user_id, 0) >= daily:
            continue
        try:
            with db.begin_nested():
                db.add(q(user_id=user_id, job_id=job_id, mode=SHADOW, status=PENDING, reason="shadow",
                         attempt_count=0, paid_calls=0, created_at=now, updated_at=now))
                db.flush()
        except IntegrityError:
            continue
        used[user_id] = used.get(user_id, 0) + 1
        queued += 1
    db.commit()
    return {"queued": queued}


def parity(result: dict[str, Any]) -> dict[str, Any]:
    """Old vs new for one job, from the shadow run's result (ids and enums only).

    A field is compared only when both sides are known; a prediction that needed a paid call nobody allowed is
    UNAVAILABLE and left out. A job the current path hid must also have had no recovery, parse or ranking task."""
    fields: dict[str, Any] = {}
    for name in PARITY_FIELDS:
        predicted, actual = result.get(f"predicted_{name}"), result.get(f"actual_{name}")
        if name == "admission" and predicted is not None and actual is not None:
            predicted, actual = predicted == "NOT_MATCHING", actual == "NOT_MATCHING"  # hidden or not
        if name == "filter" and actual == "NONE":
            actual = None  # the current path never reached the hard filter (hidden, or still waiting)
        if predicted in (None, "UNAVAILABLE") or actual is None:
            continue
        fields[name] = {"predicted": predicted, "actual": actual, "match": predicted == actual}
    hidden = result.get("actual_admission") == "NOT_MATCHING"
    zero_work = (result.get("actual_tasks") or 0) == 0 if hidden else None
    match = all(f["match"] for f in fields.values()) and zero_work is not False
    return {"match": match, "fields": fields, "hidden_zero_work": zero_work,
            "decision_source": result.get("decision_source"), "outcome": result.get("outcome")}


def _log_mismatch(task: JobQualificationTask) -> None:
    differs = sorted(k for k, v in (task.parity or {}).get("fields", {}).items() if not v["match"])
    if (task.parity or {}).get("hidden_zero_work") is False:
        differs.append("hidden_zero_work")
    system_log.log_event(category="WORKFLOW", level="WARNING", operation="qualification_shadow", workflow=KIND,
                         entity_type="job", entity_id=task.job_id, user_id=task.user_id, status="differs",
                         message="Qualification graph (shadow) differs from the current path: " + ", ".join(differs),
                         details={"task_id": task.id, "fields": differs})


def summary(db: Session, user_id: int | None = None) -> dict[str, Any]:
    """Counts for the parity report: shadow runs by match, and ON runs by outcome."""
    q = JobQualificationTask
    base = select(q.mode, q.outcome, func.count()).where(q.status == COMPLETED)
    if user_id is not None:
        base = base.where(q.user_id == user_id)
    by_outcome = {f"{m}:{o}": n for m, o, n in db.execute(base.group_by(q.mode, q.outcome)).all()}
    shadow = db.scalars(select(q.parity).where(q.mode == SHADOW, q.status == COMPLETED, q.parity.is_not(None),
                                               *([q.user_id == user_id] if user_id is not None else []))
                        .limit(5000)).all()
    matched = sum(1 for p in shadow if (p or {}).get("match"))
    return {"outcomes": by_outcome, "shadow_runs": len(shadow), "shadow_matched": matched}
