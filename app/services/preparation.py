"""The resume preparation graph's queue: dispatch, claim, run, settle, a person's decision, shadow and hand-back
(Phase 4).

    PREPARATION_GRAPH_MODE=on      automatic tailoring (auto_tailor.queue) calls `dispatch` instead of queueing the
                                   tailor worker itself; the `preparation` scheduler job claims a task, runs (or
                                   resumes) its graph thread and settles the task from how the thread ended
    PREPARATION_GRAPH_MODE=shadow  the current path is untouched; `shadow_sweep` picks, per account, a bounded number of
                                   jobs it recently tailored and reviewed and runs the graph read-only over each, then
                                   stores the old-vs-new decision on the task (`parity`). Never calls a model.
    PREPARATION_GRAPH_MODE=off     (the default) nothing new runs; tasks left from `on` are handed back (`release`)

PostgreSQL is the only queue: a task row per run, claimed with SKIP LOCKED and a guarded UPDATE, leased and renewed by
`scheduled_jobs.run_live`. A task is not claimable while its job has an active tailor task (the run waits for it), nor
while it waits for a person who has not decided yet. Waiting costs no attempt. Kill stops the task, its thread and the
tailoring pass it queued; no sweep queues the job again until a person asks (Retry, Run now).
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, event, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.models import (
    ACTIVE_PREPARATION_TASK_STATUSES,
    AgentThread,
    Job,
    JobPreparationTask,
    ResumeVariant,
    TailorTask,
)
from app.services import backoff, system_log, worker_settings
from app.services.lifecycle import STOPPED_BY_USER
from app.services.redaction import redact_text
from app.settings import get_settings

log = logging.getLogger("jobhunt.preparation")
SessionFactory = Callable[[], Session]
ON, SHADOW = "ON", "SHADOW"
PENDING, PROCESSING, COMPLETED, FAILED, SKIPPED = "PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED"
OUTCOMES = ("completed", "waiting", "retrying", "failed", "stopped", "busy", "lost")
ROUTED_TO_LEGACY = "ROUTED_TO_LEGACY"
KIND = "preparation"
APPROVAL = "APPROVAL"
# Job statuses where a person has decided the packet: a run waiting for approval ends at them (agents.preparation).
DECIDED_STATUSES = ("approved", "dismissed", "rejected", "applied", "archived")
ACTIVE_TAILOR = ("queued", "running")
ERROR_LIMIT = 300


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ───────────── mode and routing ─────────────

def mode() -> str:
    return get_settings().preparation_mode


def _postgres(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def routes(db: Session) -> bool:
    """True when qualified jobs are prepared by the graph: mode on, agent graphs on, PostgreSQL."""
    return mode() == "on" and get_settings().agent_graphs_enabled and _postgres(db)


def _held_until_asked() -> Any:
    """The job's latest ON run ended where only a person moves it on: stopped by them, or failed its attempts."""
    q, newer = JobPreparationTask, aliased(JobPreparationTask)
    latest = ~exists().where(newer.job_id == q.job_id, newer.mode == ON, newer.id > q.id)
    return and_(latest, or_(q.status == FAILED, and_(q.status == SKIPPED, q.last_error == STOPPED_BY_USER)))


def untouched(job_id: Any = Job.id) -> Any:
    """SQL: the graph never ran this job, or handed it back (automatic tailoring leaves a job the graph holds to it)."""
    q = JobPreparationTask
    return ~exists().where(q.job_id == job_id, q.mode == ON,
                           or_(q.result_code.is_(None), q.result_code != ROUTED_TO_LEGACY))


def active_task(db: Session, job_id: int, task_mode: str = ON) -> JobPreparationTask | None:
    q = JobPreparationTask
    pending = next((t for t in db.new if isinstance(t, q) and t.job_id == job_id and t.mode == task_mode
                    and t.status in ACTIVE_PREPARATION_TASK_STATUSES), None)
    return pending or db.scalar(select(q).where(q.job_id == job_id, q.mode == task_mode,
                                                q.status.in_(ACTIVE_PREPARATION_TASK_STATUSES)).limit(1))


def dispatch(db: Session, job: Job, *, reason: str = "new", force: bool = False,
             now: datetime | None = None) -> JobPreparationTask | None:
    """Queue the job for the graph, or return its active task. Never revives a run a person stopped, or one that
    failed its attempts, unless they ask again (`force`). Flushes in a savepoint, does not commit."""
    if not routes(db) and not force:
        return None
    existing = active_task(db, job.id)
    if existing is not None:
        return existing
    q = JobPreparationTask
    if not force:
        held = db.scalar(select(q.id).where(q.job_id == job.id, q.mode == ON, _held_until_asked()).limit(1))
        if held is not None:
            return None
    now = now or utcnow()
    task = q(user_id=job.user_id, job_id=job.id, mode=ON, status=PENDING, reason=reason[:40], attempt_count=0,
             revision=0, created_at=now, updated_at=now)
    try:
        with db.begin_nested():
            db.add(task)
            db.flush()
    except IntegrityError:  # another writer queued it at the same moment: one task
        return active_task(db, job.id)
    _nudge_after_commit(db)
    return task


def _nudge_after_commit(db: Session) -> None:
    if db.info.get("preparation_nudge"):
        return
    db.info["preparation_nudge"] = True

    def wake(session: Session) -> None:
        session.info.pop("preparation_nudge", None)
        from app import scheduler_hooks

        scheduler_hooks.nudge(KIND)

    event.listen(db, "after_commit", wake, once=True)


def on_decision(db: Session, job: Job, action: str) -> None:
    """A person approved, rejected or sent back the job's packet in the existing screens. The graph's waiting run
    records it on its next turn. Never raises and never changes the person's action (savepoint); does not commit."""
    if action not in ("approved", "rejected", "revise"):
        return
    try:
        with db.begin_nested():
            task = active_task(db, job.id)
            if task is None:
                return
            task.person_decision, task.next_attempt_at = action, None
            db.flush()
        _nudge_after_commit(db)
    except Exception:
        log.warning("could not hand a decision to the preparation graph for job %s", job.id, exc_info=True)


# ───────────── claiming ─────────────

@dataclass(frozen=True)
class PreparationClaim:
    task_id: int
    token: str
    worker_id: str
    user_id: int
    job_id: int
    mode: str
    run_now: bool = False


def _busy() -> Any:
    """The job has a tailoring pass queued or running: its run waits for it."""
    q = JobPreparationTask
    return exists().where(TailorTask.job_id == q.job_id, TailorTask.status.in_(ACTIVE_TAILOR))


def step_workers(db: Session, user_id: int, rows: list[dict[str, Any]]) -> dict[int, str]:
    """Which model worker each active run is at ({task id: worker type}), for Running workers' Model & route. Read only.

    The job's active tailor task says first: one task tailors and then reviews, so a run waiting on it follows its
    stage. Otherwise the run's own step does. A run left out has no model next: it waits for a person's approval, or
    Claude Desktop does this account's tailoring and review (neither goes through the gateway)."""
    from app.agents import preparation as graph

    if graph._desktop_mode(db, user_id):
        return {}
    ids = {r["job_id"] for r in rows}
    tasks = db.execute(select(TailorTask.job_id, TailorTask.stage, TailorTask.mode).where(
        TailorTask.user_id == user_id, TailorTask.job_id.in_(ids), TailorTask.status.in_(ACTIVE_TAILOR))).all() \
        if ids else []
    reviewing = {t.job_id: t.stage == "reviewing" or t.mode in ("skip", "review") for t in tasks}
    out = {}
    for r in rows:
        if r.get("waiting_for") == APPROVAL or r.get("step") == graph.AWAIT_APPROVAL:
            continue
        at_review = reviewing.get(r["job_id"])
        if at_review is None:
            at_review = r.get("waiting_for") == "REVIEWING" or r.get("step") in (graph.VALIDATE_FACTS, graph.REVIEW,
                                                                                graph.DECIDE)
        out[r["task_id"]] = "REVIEWER" if at_review else "TAILOR"
    return out


def _waiting_for_person() -> Any:
    """Waiting at AWAIT_APPROVAL with nothing decided: not claimable (a person's action, or Run now, wakes it)."""
    q = JobPreparationTask
    decided = exists().where(Job.id == q.job_id, Job.status.in_(DECIDED_STATUSES))
    # coalesce: a NULL column must make this false, not NULL (NOT NULL would hide every task from the claim)
    return and_(func.coalesce(q.waiting_for, "") == APPROVAL, q.person_decision.is_(None), q.run_now_at.is_(None),
                ~decided)


def claim(db: Session, *, worker_id: str, limit: int, task_mode: str = ON, now: datetime | None = None,
          lease_seconds: int | None = None) -> list[PreparationClaim]:
    """Lease up to `limit` due PENDING tasks of `task_mode`. Run-now first; paused accounts are held unless a task was
    asked to run now. Commits. PostgreSQL (SKIP LOCKED) and a guarded UPDATE: two workers never get the same task."""
    now = now or utcnow()
    lease = lease_seconds or get_settings().preparation_lease_seconds
    q = JobPreparationTask
    paused = worker_settings.paused(db, KIND)
    query = (select(q.id, q.user_id, q.job_id, q.run_now_at)
             .where(q.status == PENDING, q.mode == task_mode,
                    or_(q.next_attempt_at.is_(None), q.next_attempt_at <= now))
             .order_by(q.run_now_at.is_(None), q.id).limit(max(1, limit)))
    if task_mode == ON:
        query = query.where(~_busy(), ~_waiting_for_person())
    if paused:
        query = query.where(or_(q.run_now_at.is_not(None), q.user_id.not_in(paused)))
    if _postgres(db):
        query = query.with_for_update(skip_locked=True, of=q)
    rows = db.execute(query).all()
    claims: list[PreparationClaim] = []
    for row in rows:
        token = str(uuid.uuid4())
        won = db.execute(update(q).where(q.id == row.id, q.status == PENDING)
                         .values(status=PROCESSING, lock_token=token, locked_by=worker_id[:200], locked_at=now,
                                 lock_expires_at=now + timedelta(seconds=lease), run_now_at=None,
                                 attempt_count=q.attempt_count + 1, started_at=now, updated_at=now)
                         .execution_options(synchronize_session=False)).rowcount
        if won:
            claims.append(PreparationClaim(row.id, token, worker_id, row.user_id, row.job_id, task_mode,
                                           run_now=row.run_now_at is not None))
    db.commit()
    return claims


def renew(sessions: SessionFactory, lease_seconds: int | None = None) -> Callable[[list[Any]], set[int]]:
    from app.services import scheduled_jobs

    return scheduled_jobs.renew(sessions, JobPreparationTask,
                                lease_seconds or get_settings().preparation_lease_seconds, token="token")


def recover_expired(db: Session, now: datetime | None = None) -> int:
    """PROCESSING tasks whose worker died: back to PENDING, or FAILED when their attempts are used up. Commits."""
    now = now or utcnow()
    q = JobPreparationTask
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


def _clear_lease(task: JobPreparationTask) -> None:
    task.lock_token = task.locked_by = task.locked_at = task.lock_expires_at = None


# ───────────── running one task ─────────────

def _held(db: Session, c: PreparationClaim) -> JobPreparationTask | None:
    q = JobPreparationTask
    query = select(q).where(q.id == c.task_id, q.user_id == c.user_id)
    if _postgres(db):
        query = query.with_for_update()
    task = db.scalar(query.execution_options(populate_existing=True))
    if task is None or task.status != PROCESSING or task.lock_token != c.token:
        return None
    return task


def process(sessions: SessionFactory, c: PreparationClaim, *, saver: Any | None = None,
            hold: Callable[[str], None] | None = None) -> str:
    """Start or resume the task's graph thread, run it until it ends or waits, and settle the task. Never raises."""
    from app.services import tracing

    try:
        with tracing.trace("preparation", workflow_type="job", workflow_entity_id=c.job_id, job_id=c.job_id,
                           user_id=c.user_id), backoff.bypass(c.run_now):
            return _process(sessions, c, saver=saver, hold=hold)
    except Exception as exc:
        log.warning("preparation task %s failed", c.task_id, exc_info=True)
        return _settle_error(sessions, c, exc)


def _process(sessions: SessionFactory, c: PreparationClaim, *, saver: Any | None,
             hold: Callable[[str], None] | None) -> str:
    from app.agents import preparation as graph
    from app.agents import runtime

    with sessions() as db:
        db.info["user_id"] = c.user_id
        task = _held(db, c)
        if task is None:
            return "lost"
        thread = db.get(AgentThread, task.agent_thread_id) if task.agent_thread_id else None
        if thread is None:
            key = f"preparation:{task.job_id}:{task.id}:{task.created_at.isoformat()}:a{task.attempt_count}"
            limit = worker_settings.get(db, task.user_id, KIND, "max_revisions")
            thread = runtime.start(db, graph.GRAPH, {"job_id": task.job_id, "task_id": task.id, "mode": task.mode,
                                                     "reason": task.reason, "max_revisions": limit},
                                   idempotency_key=key)
            task = _held(db, c)
            if task is None:
                return "lost"
            task.agent_thread_id = thread.id
            db.commit()
        status, thread_pk = thread.status, thread.id
        if status in ("COMPLETED", "FAILED", "STOPPED"):
            return _settle(sessions, c, thread_pk)
        command = None
        if status == "INTERRUPTED":
            try:
                run, command = runtime.resume(db, thread_pk, {"action": "continue"}, c.worker_id, by_worker=True)
            except runtime.ThreadBusy:
                run = None
        else:
            run = runtime.claim(db, thread_pk, c.user_id, ("QUEUED", "RUNNING"), c.worker_id)
        if run is None:
            return _release_busy(db, c, thread_pk)
    runtime.execute(sessions, run, command, saver=saver, hold=hold, extra={"task_id": c.task_id})
    return _settle(sessions, c, thread_pk)


def _release_busy(db: Session, c: PreparationClaim, thread_pk: int) -> str:
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


def _settle(sessions: SessionFactory, c: PreparationClaim, thread_pk: int) -> str:
    """Write how the thread ended onto the task, only while this claim still holds it."""
    now = utcnow()
    with sessions() as db:
        db.info["user_id"] = c.user_id
        task = _held(db, c)
        if task is None:
            return "lost"
        thread = db.get(AgentThread, thread_pk)
        status = thread.status if thread is not None else "STOPPED"
        _clear_lease(task)
        if status == "INTERRUPTED":
            interrupt = thread.interrupt or {}
            task.status, task.attempt_count = PENDING, max(0, task.attempt_count - 1)  # waiting uses no attempt
            task.waiting_for = str(interrupt.get("wait") or "")[:40] or None
            task.step = str(interrupt.get("step") or "")[:60] or task.step
            task.next_attempt_at, task.last_error = None, None
            _copy_progress(db, task, thread_pk)
            db.commit()
            return "waiting"
        if status == "COMPLETED":
            result = thread.result or {}
            task.status, task.completed_at, task.waiting_for, task.last_error = COMPLETED, now, None, None
            task.step = str(result.get("step") or "")[:60] or None
            task.outcome = str(result.get("outcome") or "")[:30] or None
            task.result_code = str(result.get("code") or "")[:60] or None
            _apply_result(task, result)
            if task.mode == SHADOW:
                task.parity = parity(result)
            db.commit()
            if task.mode == SHADOW and task.parity and task.parity.get("match") is False:
                _log_mismatch(task)
            return "completed"
        if status == "STOPPED":
            task.status, task.completed_at = SKIPPED, now
            task.last_error = (thread.last_error if thread is not None else None) or STOPPED_BY_USER
            task.outcome = "STOPPED"
            db.commit()
            return "stopped"
        message = redact_text((thread.last_error if thread is not None else "") or "graph run failed", ERROR_LIMIT)
        return _retry_or_fail(db, task, message, now)


def _apply_result(task: JobPreparationTask, result: dict[str, Any]) -> None:
    if result.get("revision") is not None:
        task.revision = int(result["revision"])
    if result.get("decision"):
        task.decision = str(result["decision"])[:20]
    if result.get("variant_id"):
        task.variant_id = int(result["variant_id"])


def _copy_progress(db: Session, task: JobPreparationTask, thread_pk: int) -> None:
    """While a run waits, Running workers show its revision, decision and version: read from its last checkpoint
    summary would need the checkpointer, so the run's own effect records (ids only) are used instead."""
    from app.models import AgentEffect

    keys = db.scalars(select(AgentEffect.effect_key).where(AgentEffect.agent_thread_id == thread_pk)
                      .order_by(AgentEffect.id)).all()
    revisions = [int(k.rsplit(":r", 1)[1]) for k in keys if k.startswith("tailor.enqueue:") and ":r" in k]
    if revisions:
        task.revision = max(revisions)
    variant = db.scalar(select(ResumeVariant.id).where(ResumeVariant.job_id == task.job_id)
                        .order_by(ResumeVariant.version.desc()).limit(1))
    if variant:
        task.variant_id = variant


def _retry_or_fail(db: Session, task: JobPreparationTask, message: str, now: datetime) -> str:
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


def _settle_error(sessions: SessionFactory, c: PreparationClaim, exc: Exception) -> str:
    try:
        with sessions() as db:
            db.info["user_id"] = c.user_id
            task = _held(db, c)
            if task is None:
                return "lost"
            _clear_lease(task)
            return _retry_or_fail(db, task, redact_text(f"{type(exc).__name__}: {exc}", ERROR_LIMIT), utcnow())
    except Exception:
        log.warning("could not settle preparation task %s", c.task_id, exc_info=True)
        return "lost"


# ───────────── stopping ─────────────

def on_stopped(db: Session, task: JobPreparationTask, now: datetime) -> None:
    """Kill from Running workers: the task is already SKIPPED with STOPPED_BY_USER; stop its thread too, and the
    tailoring pass it queued (as Kill on Tailoring would), so no model is asked after the person stopped it."""
    from app.services import qualification, workers

    task.outcome, task.waiting_for = "STOPPED", None
    if task.agent_thread_id:
        qualification.stop_threads(db, [task.agent_thread_id], now)
    query = select(TailorTask).where(TailorTask.job_id == task.job_id, TailorTask.user_id == task.user_id,
                                     TailorTask.status.in_(ACTIVE_TAILOR))
    if _postgres(db):
        query = query.with_for_update()
    for row in db.scalars(query).all():
        workers._stop(db, workers.KINDS["tailoring"], row, task.user_id, now)


def on_job_deleted(db: Session, job_id: int) -> None:
    """A job is being deleted (job_removal.delete_job): stop its runs and delete its task rows. Does not commit."""
    from app.services import qualification

    q = JobPreparationTask
    rows = db.scalars(select(q).where(q.job_id == job_id)).all()
    qualification.stop_threads(db, [r.agent_thread_id for r in rows], note="The job was deleted")
    for row in rows:
        db.delete(row)


def requeue(db: Session, task: JobPreparationTask, user_id: int) -> JobPreparationTask:
    """Retry and Run now: a new task (and so a new thread) for the job, past a stop the person made."""
    from app.services.workers import WorkerError

    job = db.scalar(select(Job).where(Job.id == task.job_id, Job.user_id == user_id))
    if job is None:
        raise WorkerError("The job no longer exists", 404)
    fresh = dispatch(db, job, reason="user_request", force=True)
    if fresh is None:
        raise WorkerError("This job could not be queued for preparation")
    db.flush()
    return fresh


def release(db: Session, now: datetime | None = None, limit: int = 200) -> int:
    """Mode off with ON tasks left: end them (ROUTED_TO_LEGACY) and stop their threads. Their jobs carry on through
    the current path: a tailoring pass the graph queued is an ordinary task the tailor worker finishes, and the Tailor
    and Approve screens act on the job as they always did. A job the graph held before it queued any tailoring pass
    gets its automatic-tailoring claim back, so automatic tailoring picks it up again. Commits."""
    from app.services import qualification, tailor_tasks

    now = now or utcnow()
    q = JobPreparationTask
    rows = db.scalars(select(q).where(q.mode == ON, q.status == PENDING).order_by(q.id).limit(limit)).all()
    qualification.stop_threads(db, [r.agent_thread_id for r in rows], now, note="Handed back to the current path")
    for task in rows:
        task.status, task.completed_at, task.result_code = SKIPPED, now, ROUTED_TO_LEGACY
        task.waiting_for = None
    unstarted = [r.job_id for r in rows if r.reason == "auto"]
    if unstarted:
        # The claim auto_tailor.queue took for the graph; never a job with a tailoring pass or a person's decision.
        db.execute(update(Job).where(Job.id.in_(unstarted), Job.auto_tailor_status == tailor_tasks.AUTO_QUEUED,
                                     ~exists().where(TailorTask.job_id == Job.id))
                   .values(auto_tailor_status=None).execution_options(synchronize_session=False))
    db.commit()
    return len(rows)


# ───────────── shadow ─────────────

def shadow_sweep(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Queue SHADOW runs for jobs whose packet the current path reviewed recently, up to each account's daily limit.
    Read-only runs that never call a model. Commits."""
    from app.services import server_settings

    now = now or utcnow()
    daily = server_settings.value("preparation_shadow_daily_limit")
    if daily <= 0:
        return {"queued": 0}
    since = now - timedelta(hours=max(1, get_settings().preparation_shadow_lookback_hours))
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    q = JobPreparationTask
    no_shadow = ~exists().where(q.job_id == Job.id, q.mode == SHADOW)
    reviewed = exists().where(ResumeVariant.job_id == Job.id, ResumeVariant.reviewer_score.is_not(None),
                              ResumeVariant.created_at >= since)
    used = dict(db.execute(select(q.user_id, func.count()).where(q.mode == SHADOW, q.created_at >= day)
                           .group_by(q.user_id)).all())
    queued = 0
    candidates = db.execute(select(Job.id, Job.user_id).where(Job.user_id.is_not(None), no_shadow, reviewed)
                            .order_by(Job.id.desc()).limit(2000)).all()
    for job_id, user_id in candidates:
        if used.get(user_id, 0) >= daily:
            continue
        try:
            with db.begin_nested():
                db.add(q(user_id=user_id, job_id=job_id, mode=SHADOW, status=PENDING, reason="shadow",
                         attempt_count=0, revision=0, created_at=now, updated_at=now))
                db.flush()
        except IntegrityError:
            continue
        used[user_id] = used.get(user_id, 0) + 1
        queued += 1
    db.commit()
    return {"queued": queued}


def parity(result: dict[str, Any]) -> dict[str, Any]:
    """Old vs new for one job, from the shadow run's result: the review decision the graph predicts against the one
    the current path's packet carries. UNKNOWN on either side is left out, and a run with nothing to compare is
    neither a match nor a mismatch (`match` None)."""
    predicted, actual = result.get("predicted_decision"), result.get("actual_decision")
    fields: dict[str, Any] = {}
    if predicted not in (None, "UNKNOWN") and actual not in (None, "UNKNOWN"):
        fields["decision"] = {"predicted": predicted, "actual": actual, "match": predicted == actual}
    return {"match": all(f["match"] for f in fields.values()) if fields else None, "fields": fields,
            "disagreement": bool(result.get("disagreement")), "outcome": result.get("outcome")}


def _log_mismatch(task: JobPreparationTask) -> None:
    differs = sorted(k for k, v in (task.parity or {}).get("fields", {}).items() if not v["match"])
    system_log.log_event(category="WORKFLOW", level="WARNING", operation="preparation_shadow", workflow=KIND,
                         entity_type="job", entity_id=task.job_id, user_id=task.user_id, status="differs",
                         message="Preparation graph (shadow) differs from the current path: " + ", ".join(differs),
                         details={"task_id": task.id, "fields": differs})


def summary(db: Session, user_id: int | None = None) -> dict[str, Any]:
    """Counts for the parity report: shadow runs by match, and ON runs by outcome."""
    q = JobPreparationTask
    base = select(q.mode, q.outcome, func.count()).where(q.status == COMPLETED)
    if user_id is not None:
        base = base.where(q.user_id == user_id)
    by_outcome = {f"{m}:{o}": n for m, o, n in db.execute(base.group_by(q.mode, q.outcome)).all()}
    shadow = db.scalars(select(q.parity).where(q.mode == SHADOW, q.status == COMPLETED, q.parity.is_not(None),
                                               *([q.user_id == user_id] if user_id is not None else []))
                        .limit(5000)).all()
    compared = [p for p in shadow if (p or {}).get("match") is not None]
    matched = sum(1 for p in compared if p["match"] is True)
    return {"outcomes": by_outcome, "shadow_runs": len(shadow), "shadow_compared": len(compared),
            "shadow_matched": matched}
