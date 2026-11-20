"""Background tailoring: single-job and bulk tailoring run as database-backed tasks that survive page reloads.

Queueing a job writes a `tailor_tasks` row and returns at once. An in-process worker, started with the app,
claims queued tasks with a conditional UPDATE (so two app instances never run the same task), runs them in
threads, and records the stage each one is in (ranking, tailoring, reviewing). The Tailor screen polls the
task list to show that progress.

A task whose worker died mid-run (a restart or deploy) is left holding an expired lease. The next claim puts
it back in the queue, and fails it once it has been interrupted MAX_ATTEMPTS times.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, event, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Job, ResumeVariant, TailorTask
from app.services import (
    backoff,
    context_builder,
    server_settings,
    threads,
    user_settings,
    worker_settings,
)
from app.services.user_settings import execution_mode_for, settings_for
from app.services.workflow import (
    NEEDS_RETAILOR,
    SKIP_TAILORING_STATUSES,
    approve_job,
    review_packet,
    skip_tailoring,
    tailor_job,
)
from app.settings import get_settings

log = logging.getLogger("jobhunt.tailor")

SessionFactory = Callable[[], Session]
ACTIVE = ("queued", "running")
FINISHED = ("succeeded", "failed", "cancelled")
MAX_ATTEMPTS = 2
# Rough share of a run each stage starts at, for the progress bars.
STAGE_PROGRESS = {
    "tailor": {"queued": 0, "ranking": 5, "tailoring": 25, "reviewing": 70, "done": 100},
    "skip": {"queued": 0, "ranking": 0, "tailoring": 0, "reviewing": 30, "done": 100},
    "review": {"queued": 0, "ranking": 0, "tailoring": 0, "reviewing": 10, "done": 100},
}
# jobs.auto_tailor_status (app.services.auto_tailor): automatic tailoring queued the job, or the person cancelled it.
# RESUME_CHANGED: the person moved a tailored job to another resume, so its packet is re-tailored once it is ranked again.
AUTO_QUEUED, DECLINED, RESUME_CHANGED = "QUEUED", "DECLINED", "RESUME"
DESKTOP_MODE_REASON ="Claude Desktop mode: tailor and review this job from Claude Desktop"
# Finished tasks stay on the Tailor screen this long unless cleared.
RECENT = timedelta(hours=24)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# What the person asked a revision to change when they sent the packet back (Approvals): at most this many items.
MAX_PERSON_REQUESTS = 12
PERSON_REQUEST_CHARS = 400


def person_requests(values: list[Any] | None) -> list[str]:
    """The person's revision requests, trimmed and bounded. Their words, so still never a source of new facts."""
    out = [" ".join(str(v).split())[:PERSON_REQUEST_CHARS] for v in values or [] if str(v or "").strip()]
    return list(dict.fromkeys(out))[:MAX_PERSON_REQUESTS]


def _revision_feedback(db: Session, job: Job, variant_id: int | None,
                       requests: list[str] | None = None) -> dict[str, Any] | None:
    """The review a revision pass fixes: the named version of this job, else none — plus what the person asked to
    change when they sent it back."""
    from app.services.review_results import retailor_feedback

    variant = db.get(ResumeVariant, variant_id) if variant_id else None
    feedback = retailor_feedback(variant) if variant is not None and variant.job_id == job.id else {}
    if feedback and requests:
        feedback["previous_review"]["person_requests"] = person_requests(requests)
    return feedback or None


def packet_outcome(db: Session, job: Job, resume: ResumeVariant, *, auto_approve: bool = True) -> dict[str, Any]:
    """Auto-approve a finished packet when the account allows it, and summarise the packet."""
    from app.services.approval import ApprovalRefused

    auto_approved = False
    app_setting = settings_for(db)
    if auto_approve and app_setting.application_mode == "automatic" \
            and (resume.reviewer_score or 0) >= app_setting.min_review_score:
        try:
            approve_job(db, job)
            auto_approved = True
        except ApprovalRefused:  # the job's state cannot reach ready-to-apply: a person decides
            db.rollback()
    if not auto_approved and resume.reviewer_score is not None:
        from app.services.job_insights import tasks as insight_tasks

        insight_tasks.on_packet_ready(db, job)  # research before approval, when the account turned it on
    return {"resume_id": resume.id, "version": resume.version, "reviewer_score": resume.reviewer_score,
            "markdown": resume.markdown, "cover_letter": resume.cover_letter,
            "reviewer_notes": resume.reviewer_notes, "auto_approved": auto_approved,
            "untailored": bool((resume.content or {}).get("untailored"))}


def task_dict(task: TailorTask) -> dict[str, Any]:
    stage = "done" if task.status in FINISHED else task.stage
    job = task.job
    return {
        "id": task.id, "job_id": task.job_id, "title": job.title if job else "", "company": job.company if job else "",
        "mode": task.mode, "regenerate": task.regenerate, "pages": (task.options or {}).get("pages"), "status": task.status, "stage": stage,
        "progress": STAGE_PROGRESS[task.mode].get(stage, 0), "error": task.error, "result": task.result,
        "created_at": _iso(task.created_at), "started_at": _iso(task.started_at), "finished_at": _iso(task.finished_at),
    }


# ───────────── per-user operations (the session belongs to the signed-in user) ─────────────

def enqueue(db: Session, ids: list[int], mode: str = "tailor", regenerate: bool = False,
            pages: int | None = None, options: dict[str, Any] | None = None, commit: bool = True,
            revise: bool = False) -> dict[str, Any]:
    """Queue tailoring for jobs in the Tailor queue. Jobs already queued or running, or with a packet
    (unless `regenerate`), are skipped with a reason. `pages` is their page target (None = the default in Settings,
    read when the task runs).

    Mode `review` only reviews each job's latest saved packet: it needs one, and skips a packet already reviewed
    unless `regenerate`. `revise` (mode `tailor`) makes each run a revision pass that fixes what the job's latest
    version's review found."""
    wanted = list(dict.fromkeys(ids))
    jobs = {j.id: j for j in db.scalars(select(Job).where(Job.id.in_(wanted))).all()}
    latest = dict(db.execute(select(ResumeVariant.job_id, func.max(ResumeVariant.id))
                             .where(ResumeVariant.job_id.in_(wanted)).group_by(ResumeVariant.job_id)).all())
    has_packet = set(latest)
    reviewed = _latest_reviewed(db, wanted) if mode == "review" else set()
    active = set(db.scalars(select(TailorTask.job_id).where(TailorTask.job_id.in_(wanted),
                                                            TailorTask.status.in_(ACTIVE))).all())
    tasks, skipped = [], []
    desktop = execution_mode_for(db) == "claude_desktop"
    for job_id in wanted:
        job = jobs.get(job_id)
        reason = ("not found" if job is None
                  # Claude Desktop mode: the reasoning happens in Claude Desktop, so the backend makes no model calls.
                  else DESKTOP_MODE_REASON if desktop
                  else "already queued" if job_id in active
                  else "not in the Tailor queue" if job.status not in SKIP_TAILORING_STATUSES
                  else ("no packet to review" if job_id not in has_packet
                        else "already reviewed" if job_id in reviewed and not regenerate
                        else None) if mode == "review"
                  else "already has a packet" if job_id in has_packet and not regenerate
                  and job.status != NEEDS_RETAILOR
                  # The Tailor works from the Ranker's analysis; skipping tailoring needs none.
                  else context_builder.RANKING_REQUIRED_REASON if mode == "tailor" and not context_builder.is_ranked(job)
                  else None)
        if reason:
            skipped.append({"id": job_id, "reason": reason})
            continue
        extra = {**({"pages": pages} if pages and mode == "tailor" else {}),
                 **({"revise_from": latest[job_id]} if revise and mode == "tailor" and job_id in latest else {}),
                 **(options or {})}
        task = TailorTask(job_id=job.id, mode=mode, regenerate=regenerate, status="queued", stage="queued", attempts=0,
                          options=extra or None)
        try:
            # uq_tailor_tasks_active_job: another request may have queued this job since the check above.
            with db.begin_nested():
                db.add(task)
                db.flush()
        except IntegrityError:
            skipped.append({"id": job_id, "reason": "already queued"})
            continue
        tasks.append(task)
    if not commit:  # the caller commits it with its own write (the preparation graph's effect record)
        if tasks:
            _nudge_after_commit(db)
        return {"queued": [task_dict(t) for t in tasks], "skipped": skipped}
    db.commit()
    if tasks:
        from app import scheduler_hooks

        scheduler_hooks.nudge("tailor")  # start now instead of at the next poll; the task table is the queue
    return {"queued": [task_dict(t) for t in tasks], "skipped": skipped}


def _latest_reviewed(db: Session, job_ids: list[int]) -> set[int]:
    """Of these jobs, the ones whose latest packet version has a review."""
    latest = (select(ResumeVariant.job_id, func.max(ResumeVariant.version).label("version"))
              .where(ResumeVariant.job_id.in_(job_ids)).group_by(ResumeVariant.job_id).subquery())
    return set(db.scalars(select(ResumeVariant.job_id)
                          .join(latest, (ResumeVariant.job_id == latest.c.job_id)
                                & (ResumeVariant.version == latest.c.version))
                          .where(ResumeVariant.reviewer_score.is_not(None))).all())


def _nudge_after_commit(db: Session) -> None:
    def wake(_session: Session) -> None:
        from app import scheduler_hooks

        scheduler_hooks.nudge("tailor")

    event.listen(db, "after_commit", wake, once=True)


def list_tasks(db: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    """Running tasks first, then the queue in order, then tasks finished in the last day, newest first."""
    since = (now or utcnow()) - RECENT
    tasks = db.scalars(select(TailorTask).where((TailorTask.status.in_(ACTIVE)) | (TailorTask.finished_at >= since))
                       .order_by(TailorTask.id)).all()
    rank = {"running": 0, "queued": 1}
    ordered = sorted(tasks, key=lambda t: (rank.get(t.status, 2),
                                           t.id if t.status in ACTIVE else -(t.finished_at or t.created_at).timestamp()))
    return [task_dict(t) for t in ordered]


def cancel_queued(db: Session, task_ids: list[int] | None = None, now: datetime | None = None) -> int:
    """Cancel queued tasks (all of them when no ids are given). A running task always finishes."""
    query = update(TailorTask).where(TailorTask.status == "queued")
    if task_ids is not None:
        query = query.where(TailorTask.id.in_(task_ids))
    result = db.execute(query.values(status="cancelled", stage="done", finished_at=now or utcnow())
                        .returning(TailorTask.job_id).execution_options(synchronize_session=False))
    job_ids = list(result.scalars().all())
    if job_ids:
        # The person's decision outlives the task row ("Clear finished"): automatic tailoring never queues these again.
        db.execute(update(Job).where(Job.id.in_(job_ids)).values(auto_tailor_status=DECLINED)
                   .execution_options(synchronize_session=False))
    db.commit()
    return len(job_ids)


def clear_finished(db: Session) -> int:
    result = db.execute(delete(TailorTask).where(TailorTask.status.in_(FINISHED))
                        .execution_options(synchronize_session=False))
    db.commit()
    return result.rowcount or 0


# ───────────── worker side (system sessions that see every account) ─────────────

def claim_next(sessions: SessionFactory, worker_id: str, lease_seconds: int, now: datetime | None = None) -> int | None:
    now = now or utcnow()
    with sessions() as db:
        expired = (TailorTask.status == "running") & (TailorTask.lock_expires_at < now)
        # Each account decides how often an interrupted run is restarted (Running workers → Settings). Every expired
        # task is checked against its own account's limit, in bounded batches, before any of it runs again.
        limits: dict[int, int] = {}
        last = 0
        while batch := db.execute(select(TailorTask.id, TailorTask.user_id, TailorTask.attempts)
                                  .where(expired, TailorTask.id > last).order_by(TailorTask.id).limit(200)).all():
            for task_id, user_id, attempts in batch:
                if user_id not in limits:
                    limits[user_id] = worker_settings.get(db, user_id, "tailoring", "max_attempts")
                over = (attempts or 0) >= limits[user_id]
                db.execute(update(TailorTask).where(TailorTask.id == task_id, expired)
                           .values(**({"status": "failed", "finished_at": now,  # keeps its stage, like run_task
                                       "error": "Interrupted: the server restarted while this job was being "
                                                "tailored. Try again."} if over
                                      else {"status": "queued", "stage": "queued"}),
                                   locked_by=None, lock_expires_at=None)
                           .execution_options(synchronize_session=False))
            last = batch[-1][0]
        db.commit()
        # A task asked to run now (Running workers) goes first.
        # Accounts that paused automatic tailoring are left out before the limit, so their queue can never hide
        # other accounts' work; Run now still starts one of theirs.
        candidates = select(TailorTask.id, TailorTask.user_id).where(TailorTask.status == "queued")
        paused = worker_settings.paused(db, "tailoring")
        if paused:
            candidates = candidates.where(or_(TailorTask.user_id.not_in(paused), TailorTask.run_now_at.is_not(None)))
        queued = db.execute(candidates.order_by(TailorTask.run_now_at.is_(None), TailorTask.id).limit(20)).all()
        quota = _account_quota(db, {user_id for _, user_id in queued})
        for task_id, user_id in queued:
            if quota.get(user_id, 1) <= 0:
                continue  # this account is already tailoring as many packets at once as it allows
            # Each claim holds the task under its own name, so a run that was stopped from Running workers (and its
            # task claimed again, maybe by this same worker) can never write over the new run.
            holder = f"{worker_id[:180]}#{uuid.uuid4().hex[:8]}"
            claimed = db.execute(update(TailorTask).where(TailorTask.id == task_id, TailorTask.status == "queued")
                                 .values(status="running", locked_by=holder, started_at=now,
                                         lock_expires_at=now + timedelta(seconds=lease_seconds),
                                         attempts=TailorTask.attempts + 1)
                                 .execution_options(synchronize_session=False))
            db.commit()
            if claimed.rowcount:
                return task_id
    return None


def _account_quota(db: Session, user_ids: set[int]) -> dict[int, int]:
    """How many more packets each account may tailor at once: its own limit less what it already has running.

    The count comes from the database, not from one worker's bookkeeping, so the limit holds across restarts
    and across several workers.
    """
    if not user_ids:
        return {}
    limits = user_settings.limits_for(db, user_ids, "tailoring_concurrency")
    running = dict(db.execute(select(TailorTask.user_id, func.count(TailorTask.id))
                              .where(TailorTask.user_id.in_(user_ids), TailorTask.status == "running")
                              .group_by(TailorTask.user_id)).all())
    return {user_id: max(0, limits[user_id] - running.get(user_id, 0)) for user_id in user_ids}


def _write(sessions: SessionFactory, task_id: int, worker_id: str, **values: Any) -> bool:
    """Update a task this worker still holds. False when the lease was lost (expired and taken over)."""
    with sessions() as db:
        result = db.execute(update(TailorTask).where(TailorTask.id == task_id, TailorTask.status == "running",
                                                      TailorTask.locked_by == worker_id)
                            .values(**values).execution_options(synchronize_session=False))
        db.commit()
        return bool(result.rowcount)


class TaskAbandoned(RuntimeError):
    """This run no longer holds its task: it was stopped from Running workers (or its lease was taken over)."""


def run_task(sessions: SessionFactory, task_id: int, worker_id: str, lease_seconds: int) -> str:
    """Run one claimed task in a session bound to its owner. Returns the final status."""
    with sessions() as db:
        task = db.get(TailorTask, task_id)
        if task is None or task.status != "running" or not (task.locked_by or "").startswith(worker_id[:180]):
            return "missing"
        user_id, job_id, mode = task.user_id, task.job_id, task.mode
        pages = (task.options or {}).get("pages")
        revise_from = (task.options or {}).get("revise_from")  # a revision pass: the version whose review to fix
        requests = (task.options or {}).get("person_requests")  # what the person asked that pass to change
        conducted = bool((task.options or {}).get("conducted"))  # queued by the preparation graph
        holder, run_now = task.locked_by, task.run_now_at is not None
    worker_id = holder  # every write below is guarded by this claim's own name
    if run_now and not _write(sessions, task_id, worker_id, run_now_at=None):  # used once, by this run only
        return "abandoned"

    def progress(stage: str) -> None:
        """Report the stage and renew the lease. Also the stop point: a run that lost its task goes no further."""
        if not _write(sessions, task_id, worker_id, stage=stage,
                      lock_expires_at=utcnow() + timedelta(seconds=lease_seconds)):
            raise TaskAbandoned(f"Tailoring task {task_id} was stopped")

    with sessions() as db:
        db.info["user_id"] = user_id

        def still_held(session: Session) -> None:
            """Every commit of this run (the packet, the review, the job's status) checks, inside the same
            transaction, that this run still holds its task: a run stopped from Running workers can never save.

            SQLAlchemy fires before_commit on a savepoint release too. Locking the task row there would keep it
            locked by this open transaction, and the next progress() (another connection writing that row) would
            wait for it forever — the run hangs and Kill hangs behind it. A savepoint saves nothing; only the real
            commit is checked."""
            if session.in_nested_transaction():
                return
            held = session.execute(select(TailorTask.id).where(TailorTask.id == task_id, TailorTask.status == "running",
                                                               TailorTask.locked_by == worker_id)
                                   .with_for_update()).first()
            if held is None:
                raise TaskAbandoned(f"Tailoring task {task_id} was stopped")

        event.listen(db, "before_commit", still_held)
        # The account may have moved to Claude Desktop mode after this task was queued: a task that was
        # waiting then must not spend the configured model API now.
        if execution_mode_for(db, user_id) == "claude_desktop":
            _write(sessions, task_id, worker_id, status="cancelled", stage="done", error=DESKTOP_MODE_REASON,
                   locked_by=None, lock_expires_at=None, finished_at=utcnow())
            return "cancelled"
        try:
            job = db.get(Job, job_id)
            if job is None:
                raise ValueError("This job was deleted")
            if job.status not in SKIP_TAILORING_STATUSES:
                raise ValueError(f"This job is {job.status.replace('_', ' ')} and can no longer be tailored")
            with backoff.bypass(run_now):
                feedback = _revision_feedback(db, job, revise_from, requests) if mode == "tailor" else None
                made = skip_tailoring(db, job, progress=progress) if mode == "skip" \
                    else review_packet(db, job, progress=progress) if mode == "review" \
                    else tailor_job(db, job, progress=progress, pages=pages, **({"feedback": feedback} if feedback else {}))
                # The preparation graph decides on a conducted run (review decision, then approval); it is never
                # approved here.
                out = packet_outcome(db, job, made, auto_approve=not conducted)
            result = {key: out[key] for key in ("resume_id", "version", "reviewer_score", "auto_approved", "untailored")}
            status, error = "succeeded", None
        except TaskAbandoned:
            db.rollback()
            log.info("tailor task %s was stopped; nothing it made was saved", task_id)
            return "abandoned"
        except Exception as exc:  # one failing job never stops the worker
            db.rollback()
            log.info("tailor task %s failed: %s", task_id, exc)
            status, error, result = "failed", str(exc)[:500] or exc.__class__.__name__, None
    # The stage is kept: a finished run records the step it ended in (Running workers → Reviewing lists the runs
    # that reached review). task_dict shows every finished task as "done".
    _write(sessions, task_id, worker_id, status=status, error=error, result=result,
           locked_by=None, lock_expires_at=None, finished_at=utcnow())
    if conducted:  # the preparation graph run waiting for this pass may go on (a hint only)
        from app import scheduler_hooks

        scheduler_hooks.nudge("preparation")
    return status


def run_pending(sessions: SessionFactory, worker_id: str = "inline", lease_seconds: int = 900) -> int:
    """Run every queued task in this thread, one at a time. For tests and one-off scripts."""
    count = 0
    while (task_id := claim_next(sessions, worker_id, lease_seconds)) is not None:
        run_task(sessions, task_id, worker_id, lease_seconds)
        count += 1
    return count


class TailorWorker:
    """Polls for queued tailoring tasks and runs up to `concurrency` of them at once, each in a thread.

    Without a fixed `concurrency` the limit is the server setting (TAILOR_CONCURRENCY, or an admin's override in
    Running workers → Settings), re-read on every poll, so a change applies without a restart. Lowering it never stops
    a running task: the worker just claims no more until enough have finished.
    """

    def __init__(self, sessions: SessionFactory, *, enabled: bool = True, concurrency: int | None = None,
                 poll_seconds: float = 2.0, lease_seconds: int = 900):
        self.sessions = sessions
        self.enabled = enabled
        self._concurrency = None if concurrency is None else max(1, concurrency)
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._loop_task: asyncio.Task | None = None
        self._inflight: set[asyncio.Task] = set()
        self._wake: asyncio.Event | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None

    @property
    def concurrency(self) -> int:
        if self._concurrency is not None:
            return self._concurrency
        return max(1, server_settings.value("tailor_concurrency"))  # cached briefly, never raises

    async def start(self) -> None:
        if self._loop_task is None:
            from app import scheduler_hooks

            self._event_loop = asyncio.get_running_loop()
            self._wake = asyncio.Event()
            scheduler_hooks.register("tailor", self.nudge)
            self._loop_task = asyncio.create_task(self._loop(), name="jobhunt-tailor-worker")

    def nudge(self) -> None:
        """Look for queued tasks now instead of at the next poll. Safe from any thread; the queue is the task table."""
        loop, wake = self._event_loop, self._wake
        if loop is not None and wake is not None and not loop.is_closed():
            loop.call_soon_threadsafe(wake.set)

    async def stop(self, timeout: float = 10.0) -> None:
        from app import scheduler_hooks

        scheduler_hooks.unregister("tailor", self.nudge)
        if self._loop_task is not None:
            self._loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        if self._inflight:
            # Threads cannot be interrupted; an unfinished task's lease expires and it is picked up again.
            await asyncio.wait(self._inflight, timeout=timeout)

    async def _loop(self) -> None:
        while True:
            try:
                # Read off the event loop: a cache miss reads the server_settings table.
                limit = await threads.run_io(lambda: self.concurrency)
                while len(self._inflight) < limit:
                    task_id = await threads.run_io(claim_next, self.sessions, self.worker_id, self.lease_seconds)
                    if task_id is None:
                        break
                    job = asyncio.create_task(threads.run_work(run_task, self.sessions, task_id, self.worker_id,
                                                                self.lease_seconds))
                    self._inflight.add(job)
                    job.add_done_callback(self._inflight.discard)
            except Exception:  # never let one bad poll end the loop
                log.exception("tailor worker poll failed")
            if self._wake is None:
                await asyncio.sleep(self.poll_seconds)
                continue
            # Sleep until the next poll, or until something queued a task and nudged.
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.poll_seconds)
            self._wake.clear()


def build_worker(sessions: SessionFactory | None = None) -> TailorWorker:
    from app.db import SessionLocal

    settings = get_settings()
    return TailorWorker(sessions or SessionLocal, enabled=settings.tailor_worker_enabled,
                        lease_seconds=settings.tailor_lease_seconds)
