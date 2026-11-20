"""The application execution worker's queue (Phase 5): enqueue, claim, run, settle, wait for a person, kill, retry.

    POST /api/jobs/{id}/application-execution → enqueue (refused unless the packet is ready to apply to: the shared
                                                apply_queue rules, the approved digest captured on the task)
    the `application_execution` scheduler job → claim (SKIP LOCKED + guarded UPDATE) → process: start or resume the
                                                task's graph thread (app.agents.application) and settle the task
    a person answers or confirms              → wake: the waiting task becomes claimable again

APPLICATION_EXECUTION_MODE (off by default) chooses what a run may do: `observe` reads the form and resolves where
each answer comes from; `assist` also fills it in an ephemeral browser page and validates it, ending READY_TO_SUBMIT,
and a person submits (in the kept browser window, or on the site). `guarded_auto` (Phase 6, auto submit) lets a run
of an account that turned Auto submit on submit the application itself, on any route a browser can complete — only
through app.services.submission, holding the shared claim and a SubmissionIntent — and queues that run when the job is
approved (`on_approved`). The submit stages (SUBMITTING,
CONFIRMING, CONFIRMED) are refused here (`set_stage`) unless that intent is passed, and by the column otherwise.

PostgreSQL is the only queue. Waiting for a person uses no attempt and no polling: a task waiting at NEEDS_INPUT is
not claimable until a person answers or confirms (or asks to Run now). Kill stops the task and its thread; nothing
revives it until a person asks (Retry and Run now queue a new task).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import event, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ACTIVE_APPLICATION_EXECUTION_TASK_STATUSES,
    APPLICATION_EXECUTION_STAGES,
    AgentThread,
    ApplicationExecutionAttempt,
    ApplicationExecutionTask,
    Job,
    KnowledgeAnswer,
)
from app.services import backoff, worker_settings
from app.services.lifecycle import STOPPED_BY_USER
from app.services.redaction import redact_text
from app.settings import get_settings

log = logging.getLogger("jobhunt.application_execution")
SessionFactory = Callable[[], Session]
KIND = "application"
JOB = "application_execution"
PENDING, PROCESSING, COMPLETED, FAILED, SKIPPED = "PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED"
OUTCOMES = ("completed", "waiting", "retrying", "failed", "stopped", "busy", "lost")
MODE_OFF = "MODE_OFF"
ERROR_LIMIT = 300
# Reached only by a guarded run holding its SubmissionIntent (app.services.submission); refused otherwise.
SUBMIT_STAGES = frozenset({"SUBMITTING", "CONFIRMING", "CONFIRMED"})
# The intent status each submit stage needs.
STAGE_INTENT = {"SUBMITTING": ("CLICKING",), "CONFIRMING": ("CLICKING",), "CONFIRMED": ("CONFIRMED",)}
SERVER_MODES = {"OBSERVE": ("observe", "assist", "guarded_auto"), "ASSIST": ("assist", "guarded_auto"),
                "GUARDED_AUTO": ("guarded_auto",)}
WAITING = "NEEDS_INPUT"
# Stages a PENDING task waits in for a person: answers (NEEDS_INPUT) or the takeover window (NEEDS_HUMAN, Phase 6).
WAITING_STAGES = ("NEEDS_INPUT", "NEEDS_HUMAN")


class ExecutionRefused(ValueError):
    def __init__(self, message: str, code: str):
        self.code = code
        super().__init__(message)


class SubmitForbidden(RuntimeError):
    """Something tried to move an execution into a submit stage without the SubmissionIntent that authorizes it."""


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ───────────── mode ─────────────

def mode() -> str:
    return get_settings().application_execution_mode_value


def _postgres(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def routes(db: Session) -> bool:
    return mode() != "off" and get_settings().agent_graphs_enabled and _postgres(db)


def available_mode(db: Session) -> str:
    """The mode screens show: the server's mode only where a run can actually be queued (`routes`), else off, so a
    deployment that cannot run the worker (SQLite) never offers an action that is always refused."""
    return mode() if routes(db) else "off"


def set_stage(task: ApplicationExecutionTask, stage: str, *, intent: Any = None) -> None:
    """The only way an execution's stage changes. Refuses unknown names, and the submit stages (SubmitForbidden)
    unless `intent` is this execution's SubmissionIntent in the status that stage needs."""
    if stage not in APPLICATION_EXECUTION_STAGES:
        raise ValueError(f"unknown execution stage {stage}")
    if stage in SUBMIT_STAGES:
        if intent is None or intent.execution_task_id != task.id or intent.status not in STAGE_INTENT[stage] \
                or task.mode != "GUARDED_AUTO":
            raise SubmitForbidden(f"{stage} needs this guarded execution's SubmissionIntent "
                                  "(app.services.submission); nothing else may reach it")
        task._submit_stage_allowed = stage  # read once by the column guard below
    task.stage = stage


@event.listens_for(ApplicationExecutionTask.stage, "set", retval=True)
def _no_submit_stage(target: Any, value: str, _old: Any, _initiator: Any) -> str:
    """Belt and braces: whatever code sets the column, a submit stage reaches the database only through set_stage
    with the execution's intent."""
    if value in SUBMIT_STAGES:
        allowed = target.__dict__.pop("_submit_stage_allowed", None)
        if allowed != value:
            raise SubmitForbidden(f"{value} needs this guarded execution's SubmissionIntent "
                                  "(app.services.submission); nothing else may reach it")
    return value


# ───────────── enqueue ─────────────

def active_task(db: Session, job_id: int) -> ApplicationExecutionTask | None:
    q = ApplicationExecutionTask
    return db.scalar(select(q).where(q.job_id == job_id, q.status.in_(ACTIVE_APPLICATION_EXECUTION_TASK_STATUSES))
                     .limit(1))


def latest(db: Session, job_id: int) -> ApplicationExecutionTask | None:
    q = ApplicationExecutionTask
    return db.scalar(select(q).where(q.job_id == job_id).order_by(q.id.desc()).limit(1))


BROWSER_USE = "browser_use_webui"


def enqueue(db: Session, job: Job, *, execution_mode: str | None = None, reason: str = "user_request",
            now: datetime | None = None, backend: str | None = None) -> ApplicationExecutionTask:
    """Queue the execution worker for an approved job, or return its active task. Refused (ExecutionRefused) while
    the mode is off, when the packet may not be used (the shared apply_queue rules), or while a submission of the
    posting is in progress. Captures the approved digest. Flushes in a savepoint; the caller commits."""
    from app.services import apply_queue, approval

    server = mode()
    # A run sent to the browser-use window by a person fills it for them to submit (ASSIST) unless they say otherwise.
    fallback = "ASSIST" if backend is not None and server in ("assist", "guarded_auto") else None
    wanted = (execution_mode or fallback or default_mode(db, job.user_id)).upper()
    if not routes(db):
        raise ExecutionRefused("The application worker is off (APPLICATION_EXECUTION_MODE, AGENT_GRAPHS_ENABLED and "
                               "PostgreSQL are needed)", "OFF")
    if server not in SERVER_MODES.get(wanted, ()):
        raise ExecutionRefused(f"This server runs the application worker in {server} mode", "MODE")
    if backend is not None:
        backend_refusal(backend, wanted)
    existing = active_task(db, job.id)
    if existing is not None:
        if backend is not None and existing.backend != backend:
            raise ExecutionRefused("This job already has a run in progress; stop it in Running workers first",
                                   "ACTIVE")
        return existing
    reason_not_ready = apply_queue.not_ready_reason(db, job)
    if reason_not_ready:
        raise ExecutionRefused(reason_not_ready, "NOT_READY")
    claim = apply_queue.open_claim(db, job)
    if claim is not None:
        raise ExecutionRefused("A submission of this posting is in progress or its outcome is unknown", "CLAIMED")
    if wanted == "GUARDED_AUTO":
        guarded_refusal(db, job)
    packet = approval.live(db, job.application)
    now = now or utcnow()
    q = ApplicationExecutionTask
    task = q(user_id=job.user_id, job_id=job.id, application_id=job.application.id,
             approved_packet_id=packet.id if packet else None, packet_digest=packet.digest if packet else None,
             mode=wanted, status=PENDING, stage="QUEUED", reason=reason[:40], attempt_count=0, created_at=now,
             updated_at=now, backend=backend)
    try:
        with db.begin_nested():
            db.add(task)
            db.flush()
    except IntegrityError:  # queued at the same moment by another request: one task
        found = active_task(db, job.id)
        if found is None:
            raise
        return found
    _nudge_after_commit(db)
    return task


def default_mode(db: Session, user_id: int) -> str:
    """The mode a run gets when nobody names one: auto submit (GUARDED_AUTO) for an account that turned it on, on a
    server that allows it; otherwise fill only (ASSIST), or the server's own mode when it is lower."""
    from app.services import submission

    server = mode()
    if server == "guarded_auto":
        return "GUARDED_AUTO" if submission.consent(db, user_id)["enabled"] else "ASSIST"
    return server.upper()


def backend_refusal(backend: str, mode_: str) -> None:
    """A person may send a run to the browser-use backend (a visible window when BROWSER_USE_HEADLESS is false), in
    any mode."""
    from app.infrastructure.application_browser.browser_use_sidecar import configured

    if backend != BROWSER_USE:
        raise ExecutionRefused("The only backend a run can be sent to is browser_use_webui", "BACKEND")
    if not configured():
        raise ExecutionRefused("The browser-use backend is not set up on this server (BROWSER_USE_WEBUI_ENABLED, "
                               "BROWSER_USE_PYTHON)", "BACKEND_UNAVAILABLE")


def guarded_refusal(db: Session, job: Job) -> None:
    """Why this job may not run in GUARDED_AUTO (ExecutionRefused), checked when it is queued; the PreSubmitValidator
    checks all of it again before the click."""
    from app.services import ats_map, submission

    if not submission.consent(db, job.user_id)["enabled"]:
        raise ExecutionRefused("Auto submit is off for this account (Settings → Auto submit)", "NO_CONSENT")
    match = ats_map.for_job(job)
    if match is None or not match.auto_submit:
        raise ExecutionRefused((match.note if match else None) or "Applications on this site are not automated",
                               "ROUTE_NOT_SUPPORTED")


def on_approved(db: Session, job: Job) -> None:
    """Auto submit: a job the person approved is queued for submission at once (GUARDED_AUTO), when the account
    turned Auto submit on and the server allows it. Anything that refuses it (not ready, an unsupported site, a claim
    already open) leaves the job for the person, as before. Runs in a savepoint: approval never fails because of it."""
    try:
        if mode() != "guarded_auto" or not routes(db) or default_mode(db, job.user_id) != "GUARDED_AUTO":
            return
        with db.begin_nested():
            enqueue(db, job, execution_mode="GUARDED_AUTO", reason="auto_submit")
    except ExecutionRefused as exc:
        log.info("auto submit not queued for job %s: %s", job.id, exc.code)
    except Exception:
        log.warning("auto submit could not be queued for job %s", job.id, exc_info=True)


def _nudge_after_commit(db: Session) -> None:
    if db.info.get("application_execution_nudge"):
        return
    db.info["application_execution_nudge"] = True

    def wake(session: Session) -> None:
        session.info.pop("application_execution_nudge", None)
        from app import scheduler_hooks

        scheduler_hooks.nudge(JOB)

    event.listen(db, "after_commit", wake, once=True)


# ───────────── a person's answers ─────────────

def _ready_to_continue(db: Session, task: ApplicationExecutionTask) -> bool:
    waiting = task.interrupt or {}
    if waiting.get("kind") == "NEEDS_HUMAN":
        return bool(waiting.get("continued"))  # the person said they finished in the window
    needed = list(waiting.get("question_ids") or [])
    if needed:
        from app.services.knowledge import fits_job

        rows = db.scalars(select(KnowledgeAnswer).where(KnowledgeAnswer.id.in_(needed))).all()
        if len(rows) != len(set(needed)) or not all(r.verified and (r.answer or "").strip()
                                                    and fits_job(r, task.job_id) for r in rows):
            return False
    confirm = set(waiting.get("confirm_question_ids") or [])
    return confirm <= set(task.confirmed_question_ids or [])


def wake(db: Session, task: ApplicationExecutionTask) -> bool:
    """A waiting task whose questions are now answered and confirmed becomes claimable (stage ANSWERING). Does not
    commit."""
    if task.status != PENDING or task.stage not in WAITING_STAGES or not _ready_to_continue(db, task):
        return False
    set_stage(task, "ANSWERING" if task.stage == WAITING else "ROUTED")
    task.next_attempt_at = None
    _nudge_after_commit(db)
    return True


def wake_waiting(db: Session, user_id: int) -> int:
    """After a person answers a question in the Knowledge bank: wake the account's tasks that waited for it. Never
    raises (a savepoint); does not commit."""
    woken = 0
    try:
        with db.begin_nested():
            q = ApplicationExecutionTask
            for task in db.scalars(select(q).where(q.user_id == user_id, q.status == PENDING, q.stage == WAITING)
                                   .limit(200)).all():
                woken += wake(db, task)
            db.flush()
    except Exception:
        log.warning("could not wake waiting application executions", exc_info=True)
    return woken


def continue_after_takeover(db: Session, task_id: int) -> ApplicationExecutionTask:
    """The person finished in the kept window (logged in, entered the code, solved a CAPTCHA, filled fields): the run
    resumes in that same page while it is open, revalidating everything before it acts. Commits."""
    task = db.get(ApplicationExecutionTask, task_id)
    if task is None:
        raise ExecutionRefused("Execution not found", "NOT_FOUND")
    waiting = dict(task.interrupt or {})
    if task.status != PENDING or task.stage != "NEEDS_HUMAN" or not waiting.get("takeover"):
        raise ExecutionRefused("This execution is not waiting for you in its browser window", "NOT_WAITING")
    waiting["continued"] = True
    task.interrupt = waiting
    wake(db, task)
    db.commit()
    return task


# Takeovers a person settles in the window by answering a CAPTCHA: the run continues by itself once it is answered.
CHALLENGE_REASONS = ("CAPTCHA_OR_ANTI_BOT", "NAVIGATOR_CAPTCHA")
# Takeovers a person settles by filling the pointed-out fields: the run continues by itself once every one of them
# holds something that has not changed for APPLICATION_FIELDS_SETTLE_SECONDS (they stopped typing).
FIELD_REASONS = ("FIELDS_NEED_YOU",)
_fields_seen: dict[tuple[int, int], tuple[tuple[str, ...], float]] = {}  # window → (value digests, unchanged since)
_watching: dict[tuple[int, int], Any] = {}
_widget_seen: set[tuple[int, int]] = set()  # windows where a challenge widget was seen while it waited


def _answered(key: tuple[int, int], reading: Any) -> bool:
    """Whether the window's challenge is behind the person: its widget reads solved, or a widget seen earlier is gone
    (solving it removed it, or moved on to the form). The run re-observes and re-validates the page either way; a
    CAPTCHA only the page text named (no widget ever seen) still waits for Continue."""
    if not isinstance(reading, dict):
        return False
    if reading.get("present"):
        _widget_seen.add(key)
        return bool(reading.get("solved"))
    return key in _widget_seen


def _watched(user_id: int, task_id: int) -> bool:
    """A watcher is running for this window (one whose loop closed before it ran does not count)."""
    running = _watching.get((user_id, task_id))
    return running is not None and not running.done()


def _continue_from_window(db: Session, user_id: int, task_id: int, reasons: tuple[str, ...]) -> bool | None:
    """Continue the run as the Continue button would (marked `continued_by: window`) when it waits in its window for
    one of `reasons`. True: woken. None: not settled yet (the run is still handing the window over), ask again.
    False: nothing to continue (another reason, already continued, ended). Commits when it wakes."""
    task = db.get(ApplicationExecutionTask, task_id)
    if task is None or task.user_id != user_id:
        return False
    if task.status == PROCESSING:
        return None
    waiting = dict(task.interrupt or {})
    if task.status != PENDING or task.stage != "NEEDS_HUMAN" or not waiting.get("takeover") \
            or waiting.get("reason") not in reasons or waiting.get("continued"):
        return False
    waiting.update(continued=True, continued_by="window")
    task.interrupt = waiting
    woken = wake(db, task)
    db.commit()
    return woken


def challenge_solved(db: Session, user_id: int, task_id: int) -> bool | None:
    """The window of this task reports its CAPTCHA answered: continue the run (see `_continue_from_window`)."""
    return _continue_from_window(db, user_id, task_id, CHALLENGE_REASONS)


def fields_filled(db: Session, user_id: int, task_id: int) -> bool | None:
    """Every field the window pointed out holds something and stopped changing: continue the run (see
    `_continue_from_window`). The run reads the page again and checks every field before it acts."""
    return _continue_from_window(db, user_id, task_id, FIELD_REASONS)


def watch_challenge(sessions: Callable[[], Session], user_id: int, task_id: int) -> None:
    """Ask the window handed over for a CAPTCHA, every APPLICATION_CHALLENGE_POLL_SECONDS, whether the person answered
    it (the runner's booleans; never the answer), and continue the run as soon as they have: a solved reCAPTCHA lasts
    about two minutes, longer than a scheduler pass. Runs on the loop the session lives on, until the window is no
    longer handed over. Never raises; one watcher per window."""
    try:
        from app.infrastructure.application_browser.browser_use_sidecar import get_sidecars
        from app.services import threads

        manager = get_sidecars()
        loop = getattr(manager, "loop", None)
        if loop is None or loop.is_closed() or _watched(user_id, task_id):
            return

        def settle() -> bool | None:
            with sessions() as db:
                db.info["user_id"] = user_id
                return challenge_solved(db, user_id, task_id)

        async def poll() -> None:
            try:
                while True:
                    await asyncio.sleep(max(1, get_settings().application_challenge_poll_seconds))
                    session = manager.get(user_id, task_id)
                    if session is None or session.parked_until is None:
                        return
                    try:
                        reading = await session.call("challenge", timeout=15, record=False)
                    except Exception:  # a busy or navigating page: ask again
                        log.debug("CAPTCHA window of execution %s did not answer", task_id, exc_info=True)
                        continue
                    if not _answered((user_id, task_id), reading):
                        continue
                    done = await threads.run_io(settle)
                    if done is not None:
                        return
            finally:
                _watching.pop((user_id, task_id), None)
                _widget_seen.discard((user_id, task_id))

        _watching[(user_id, task_id)] = asyncio.run_coroutine_threadsafe(poll(), loop)
    except Exception:  # the Continue button and the scheduler pass still work
        log.warning("could not watch the CAPTCHA window of execution %s", task_id, exc_info=True)


async def fields_reading(session: Any, field_ids: list[str]) -> tuple[str, ...] | None:
    """A digest of what each pointed-out field holds now, or None while any of them is empty or not on the page. The
    page is observed afresh (the person may have changed it) and each field read by its element key; a radio group is
    read through one of its options. The digests stay in this process's memory, never stored, logged or returned."""
    import hashlib

    observation = await session.call("observe", timeout=60, record=False)
    elements = observation.get("elements") or []
    by_key = {e["key"]: e for e in elements if e.get("key")}
    by_group = {e["group_key"]: e for e in elements if e.get("group_key")}
    attached = observation.get("attached") or {}
    out: list[str] = []
    for field_id in field_ids:
        key = field_id[3:] if field_id.startswith("bu_") else None
        if key is not None and key in attached:  # a file input the board replaced with the uploaded file's name
            out.append(hashlib.sha256(f"file|{attached[key]}".encode()).hexdigest())
            continue
        element = by_key.get(key or "") or by_group.get(key or "")
        if element is None:
            return None
        read = await session.call("read", key=element["key"], timeout=60, record=False)
        if not read.get("filled"):
            return None
        out.append(hashlib.sha256(repr(sorted(read.items())).encode()).hexdigest())
    return tuple(out)


def settled(key: tuple[int, int], reading: tuple[str, ...] | None, now: float | None = None) -> bool:
    """Every field is filled and nothing changed for APPLICATION_FIELDS_SETTLE_SECONDS: the person stopped typing."""
    import time

    from app.services import server_settings

    now = time.monotonic() if now is None else now
    if not reading:
        _fields_seen.pop(key, None)
        return False
    seen = _fields_seen.get(key)
    if seen is None or seen[0] != reading:
        _fields_seen[key] = (reading, now)
        return False
    return now - seen[1] >= max(0, server_settings.value("application_fields_settle_seconds"))


def _field_ids(task: ApplicationExecutionTask | None) -> list[str]:
    waiting = (task.interrupt or {}) if task is not None else {}
    if task is None or task.status != PENDING or waiting.get("reason") not in FIELD_REASONS \
            or not waiting.get("takeover") or waiting.get("continued"):
        return []
    return [f["id"] for f in waiting.get("fields") or [] if f.get("id")]


def watch_fields(sessions: Callable[[], Session], user_id: int, task_id: int) -> None:
    """Watch a window handed over for fields only the person can fill (FIELDS_NEED_YOU): every
    APPLICATION_CHALLENGE_POLL_SECONDS, read whether each pointed-out field holds something, and continue the run once
    all of them do and have not changed for a moment. Same loop and rules as `watch_challenge`; never raises."""
    try:
        from app.infrastructure.application_browser.browser_use_sidecar import get_sidecars
        from app.services import server_settings, threads

        manager = get_sidecars()
        loop = getattr(manager, "loop", None)
        if loop is None or loop.is_closed() or _watched(user_id, task_id):
            return
        key = (user_id, task_id)

        def fields() -> list[str] | None:
            with sessions() as db:
                db.info["user_id"] = user_id
                task = db.get(ApplicationExecutionTask, task_id)
                if task is not None and task.status == PROCESSING:
                    return None  # still handing the window over
                return _field_ids(task)

        def settle() -> bool | None:
            with sessions() as db:
                db.info["user_id"] = user_id
                return fields_filled(db, user_id, task_id)

        async def poll() -> None:
            try:
                while True:
                    await asyncio.sleep(max(1, server_settings.value("application_challenge_poll_seconds")))
                    session = manager.get(user_id, task_id)
                    if session is None or session.parked_until is None:
                        return
                    ids = await threads.run_io(fields)
                    if ids is None:
                        continue
                    if not ids:
                        return
                    try:
                        reading = await fields_reading(session, ids)
                    except Exception:  # a busy or navigating page: ask again
                        log.debug("window of execution %s did not answer", task_id, exc_info=True)
                        continue
                    if not settled(key, reading):
                        continue
                    done = await threads.run_io(settle)
                    if done is not None:
                        return
            finally:
                _watching.pop(key, None)
                _fields_seen.pop(key, None)

        _watching[key] = asyncio.run_coroutine_threadsafe(poll(), loop)
    except Exception:  # the Continue button and the scheduler pass still work
        log.warning("could not watch the browser window of execution %s", task_id, exc_info=True)


def watch_filled_windows(db: Session, limit: int = 20) -> int:
    """Each scheduler pass, for windows waiting for pointed-out fields with no watcher running (a restart): continue
    the ones whose fields are all filled and unchanged since the last pass. Never raises."""
    try:
        from app.infrastructure.application_browser.browser_use_sidecar import get_sidecars

        manager = get_sidecars()
        windows = manager.handed_over()[:limit]
    except Exception:  # noqa: BLE001
        return 0
    woken, account = 0, db.info.get("user_id")
    for user_id, task_id in windows:
        if account not in (None, user_id) or _watched(user_id, task_id):
            continue
        db.info["user_id"] = user_id
        try:
            task = db.get(ApplicationExecutionTask, task_id)
            ids = _field_ids(task) if task is not None and task.user_id == user_id else []
            session = manager.get(user_id, task_id) if ids else None
            if session is None:
                continue
            reading = manager.run_threadsafe(fields_reading(session, ids), timeout=120)
            if settled((user_id, task_id), reading) and fields_filled(db, user_id, task_id):
                woken += 1
                _fields_seen.pop((user_id, task_id), None)
        except Exception:  # one window never stops the others, nor the pass
            db.rollback()
            log.warning("could not check the browser window of execution %s", task_id, exc_info=True)
        finally:
            db.info["user_id"] = account
    return woken


def watch_challenges(db: Session, limit: int = 20) -> int:
    """Each scheduler pass, for windows still waiting at a CAPTCHA: continue the ones the person answered (the same
    as `watch_challenge`, when no watcher is running). Never raises."""
    try:
        from app.infrastructure.application_browser.browser_use_sidecar import get_sidecars

        manager = get_sidecars()
        windows = manager.handed_over()[:limit]
    except Exception:  # noqa: BLE001
        return 0
    woken, account = 0, db.info.get("user_id")
    for user_id, task_id in windows:
        if account not in (None, user_id) or _watched(user_id, task_id):
            continue
        db.info["user_id"] = user_id
        try:
            task = db.get(ApplicationExecutionTask, task_id)
            if task is None or task.user_id != user_id or task.status != PENDING \
                    or (task.interrupt or {}).get("reason") not in CHALLENGE_REASONS:
                continue
            session = manager.get(user_id, task_id)
            reading = manager.run_threadsafe(session.call("challenge", timeout=15, record=False), timeout=20) \
                if session is not None else None
            if _answered((user_id, task_id), reading) and challenge_solved(db, user_id, task_id):
                woken += 1
                _widget_seen.discard((user_id, task_id))
        except Exception:  # one window never stops the others, nor the pass
            db.rollback()
            log.warning("could not check the CAPTCHA window of execution %s", task_id, exc_info=True)
        finally:
            db.info["user_id"] = account
    return woken


def end_expired_takeovers(db: Session, now: datetime | None = None, limit: int = 200) -> int:
    """Takeovers nobody came back to: the window is closed (SidecarManager.reap) and the run ends NEEDS_HUMAN
    (TAKEOVER_EXPIRED) with the shared blocker, as a Phase 5 run would; a person retries when ready. Commits."""
    from app.services import apply_queue, qualification

    now = now or utcnow()
    q = ApplicationExecutionTask
    ended = 0
    for task in db.scalars(select(q).where(q.status == PENDING, q.stage == "NEEDS_HUMAN").limit(limit)).all():
        waiting = task.interrupt or {}
        try:
            expires = datetime.fromisoformat(waiting.get("expires_at") or "")
        except ValueError:
            continue
        if waiting.get("continued") or expires > now:
            continue
        task.status, task.completed_at, task.outcome, task.result_code = COMPLETED, now, "NEEDS_HUMAN", \
            "TAKEOVER_EXPIRED"
        qualification.stop_threads(db, [task.agent_thread_id], now, note="Nobody took over the browser window")
        job = db.get(Job, task.job_id)
        reason = {"ACCOUNT_REQUIRED": "account_required", "CAPTCHA_OR_ANTI_BOT": "captcha"}.get(
            waiting.get("reason") or "", "login_required")
        if job is not None and job.application is not None and job.application.status == "approved":
            db.commit()
            apply_queue.record_blocker(db, job, reason, recorded_by="application_worker",
                                       detail=f"The browser window waited for you and closed: {waiting.get('reason')}")
        ended += 1
    db.commit()
    return ended


SUBMITTED_IN_WINDOW = "SUBMITTED_IN_WINDOW"


def watch_windows(db: Session, limit: int = 20) -> int:
    """A window handed to the person works like a normal browser, so they may submit there themselves. Each pass reads
    what those windows saw (non-GET responses, the page; never a value) and records a submission the site confirmed
    (the same ConfirmationVerifier evidence as auto submit): an open claim of the run's own is resolved as submitted,
    anything else is recorded as the person's report. The run then ends SUBMITTED_IN_WINDOW. Never raises. Commits."""
    from urllib.parse import urlparse

    from app.services import apply_queue, qualification, submission

    try:
        from app.infrastructure.application_browser.browser_use_sidecar import get_sidecars

        manager = get_sidecars()
        windows = manager.handed_over()[:limit]
    except Exception:  # noqa: BLE001
        return 0
    recorded = 0
    account = db.info.get("user_id")
    for user_id, task_id in windows:
        if account not in (None, user_id):
            continue  # a session that belongs to one account only looks at that account's windows
        db.info["user_id"] = user_id  # the recorder works as the window's own account
        try:
            task = db.get(ApplicationExecutionTask, task_id)
            job = db.get(Job, task.job_id) if task is not None and task.user_id == user_id else None
            if job is None or job.status != "approved":
                continue
            session = manager.get(user_id, task_id)
            evidence = manager.run_threadsafe(session.call("evidence"), timeout=20) if session is not None else None
            if not isinstance(evidence, dict):
                continue
            host = (urlparse(evidence.get("url") or "").hostname or "").lower()
            confirmation = submission.ConfirmationVerifier.verify("browser_use", host, evidence)
            evidence.clear()
            if not confirmation.confirmed:
                continue
            claim = apply_queue.open_claim(db, job)
            if claim is not None and claim.execution_id == task.id:
                result = apply_queue.resolve_uncertain(db, job, claim.id, submitted=True,
                                                       confirmation_id=confirmation.confirmation_id)
            else:
                result = apply_queue.record_submission(db, job, client="apply_kit",
                                                       confirmation_id=confirmation.confirmation_id,
                                                       submitted_via="browser_window")
            if getattr(result, "status", "SUBMITTED") not in ("SUBMITTED", "ALREADY_APPLIED"):
                db.rollback()
                continue
            task = db.get(ApplicationExecutionTask, task_id, populate_existing=True)
            if task.status == PENDING:
                task.status, task.completed_at = COMPLETED, utcnow()
                qualification.stop_threads(db, [task.agent_thread_id], note="Submitted in the browser window")
            task.outcome = task.outcome or "READY_TO_SUBMIT"
            task.result_code, task.interrupt = SUBMITTED_IN_WINDOW, None
            db.commit()
            recorded += 1
        except Exception:  # one window never stops the others, nor the pass
            db.rollback()
            log.warning("could not check the browser window of execution %s", task_id, exc_info=True)
        finally:
            db.info["user_id"] = account
    return recorded


def questions(db: Session, task: ApplicationExecutionTask) -> list[dict[str, Any]]:
    """The questions this execution waits for the person to answer (saved in the Knowledge bank by the resolver), with
    what the Apply kit needs to ask them in place: the wording, the form's own options, whether it is sensitive and
    what is saved so far. For the account's own Apply kit only; nothing here is stored on the task."""
    ids = list((task.interrupt or {}).get("question_ids") or []) if task.stage in WAITING_STAGES else []
    if not ids:
        return []
    from app.services.knowledge import fits_job

    rows = {r.id: r for r in db.scalars(select(KnowledgeAnswer).where(KnowledgeAnswer.id.in_(ids))).all()}
    out = []
    for i in ids:
        if i not in rows:
            continue
        row = rows[i]
        other = not fits_job(row, task.job_id)
        # A job-specific answer written for another job: offer this job's own draft (job insights) in its place.
        answer = (_insight_draft(db, task.job_id, row.question) or "") if other else (row.answer or "")
        out.append({"id": i, "question": row.question, "answer": answer, "verified": row.verified and not other,
                    "answered": bool(row.verified and (row.answer or "").strip()) and not other,
                    "answer_type": row.answer_type, "answer_options": row.answer_options or [],
                    "category": row.category, "sensitive": row.sensitive, "other_job": other,
                    "note": "Your saved answer was written for another job. This is a draft for this one: check it "
                            "and save it." if other else None})
    return out


def _insight_draft(db: Session, job_id: int, question: str) -> str | None:
    """This job's drafted answer to the same question (job insights' application questions), if one was drafted."""
    from app.models import JobInsight
    from app.services import knowledge

    insight = db.scalar(select(JobInsight).where(JobInsight.job_id == job_id))
    for item in (insight.application_questions if insight else None) or []:
        if item.get("status") == "DRAFTED" and item.get("answer") \
                and knowledge.same_question(question, item.get("question") or ""):
            return str(item["answer"])
    return None


def confirmations(db: Session, task: ApplicationExecutionTask) -> list[dict[str, Any]]:
    """The saved sensitive answers this execution waits for the person to confirm: each question and the answer that
    would be filled, so a person sees exactly what they authorize. For the account's own Apply kit only (it shows
    saved answers already); never stored on the task, the graph or a log."""
    ids = list((task.interrupt or {}).get("confirm_question_ids") or []) if task.stage == WAITING else []
    if not ids:
        return []
    rows = {r.id: r for r in db.scalars(select(KnowledgeAnswer).where(KnowledgeAnswer.id.in_(ids))).all()}
    return [{"id": i, "question": rows[i].question, "answer": rows[i].answer} for i in ids if i in rows]


def confirm_answers(db: Session, task_id: int, question_ids: list[int]) -> ApplicationExecutionTask:
    """The person confirms sensitive saved answers for this execution only. Commits."""
    task = db.get(ApplicationExecutionTask, task_id)
    if task is None:
        raise ExecutionRefused("Execution not found", "NOT_FOUND")
    asked = set((task.interrupt or {}).get("confirm_question_ids") or [])
    if task.stage != WAITING or not set(question_ids) <= asked:
        raise ExecutionRefused("These answers are not waiting for confirmation in this execution", "NOT_WAITING")
    task.confirmed_question_ids = sorted(set(task.confirmed_question_ids or []) | set(question_ids))
    wake(db, task)
    db.commit()
    return task


# ───────────── claiming ─────────────

@dataclass(frozen=True)
class ExecutionClaim:
    task_id: int
    token: str
    worker_id: str
    user_id: int
    job_id: int
    run_now: bool = False


def claim(db: Session, *, worker_id: str, limit: int, now: datetime | None = None,
          lease_seconds: int | None = None) -> list[ExecutionClaim]:
    """Lease up to `limit` due PENDING tasks. Run-now first; a task waiting for a person is skipped unless asked to
    run now; paused accounts are held unless asked. Commits."""
    now = now or utcnow()
    lease = lease_seconds or get_settings().application_execution_lease_seconds
    q = ApplicationExecutionTask
    query = (select(q.id, q.user_id, q.job_id, q.run_now_at)
             .where(q.status == PENDING, or_(q.next_attempt_at.is_(None), q.next_attempt_at <= now),
                    or_(q.stage.not_in(WAITING_STAGES), q.run_now_at.is_not(None)))
             .order_by(q.run_now_at.is_(None), q.id).limit(max(1, limit)))
    paused = worker_settings.paused(db, KIND)
    if paused:
        query = query.where(or_(q.run_now_at.is_not(None), q.user_id.not_in(paused)))
    if _postgres(db):
        query = query.with_for_update(skip_locked=True, of=q)
    claims: list[ExecutionClaim] = []
    for row in db.execute(query).all():
        token = str(uuid.uuid4())
        won = db.execute(update(q).where(q.id == row.id, q.status == PENDING)
                         .values(status=PROCESSING, lock_token=token, locked_by=worker_id[:200], locked_at=now,
                                 lock_expires_at=now + timedelta(seconds=lease), run_now_at=None,
                                 attempt_count=q.attempt_count + 1, started_at=now, updated_at=now)
                         .execution_options(synchronize_session=False)).rowcount
        if won:
            claims.append(ExecutionClaim(row.id, token, worker_id, row.user_id, row.job_id,
                                         run_now=row.run_now_at is not None))
    db.commit()
    return claims


def renew(sessions: SessionFactory, lease_seconds: int | None = None) -> Callable[[list[Any]], set[int]]:
    from app.services import scheduled_jobs

    return scheduled_jobs.renew(sessions, ApplicationExecutionTask,
                                lease_seconds or get_settings().application_execution_lease_seconds, token="token")


def _clear_lease(task: ApplicationExecutionTask) -> None:
    task.lock_token = task.locked_by = task.locked_at = task.lock_expires_at = None


def recover_expired(db: Session, now: datetime | None = None) -> int:
    """PROCESSING tasks whose worker died: RETRYING (pending again) or FAILED at the attempt limit. A dead worker
    never moves a task towards a submit: there is no such stage to move to. Commits."""
    from app.services import submission

    now = now or utcnow()
    q = ApplicationExecutionTask
    rows = db.scalars(select(q).where(q.status == PROCESSING, q.lock_expires_at < now).limit(200)).all()
    for task in rows:
        limit = worker_settings.get(db, task.user_id, KIND, "max_attempts")
        _clear_lease(task)
        if task.mode == "GUARDED_AUTO" and submission.reconcile(db, task, "The worker stopped answering") in (
                submission.UNCERTAIN, submission.CONFIRMED):
            # A click may have happened: the outcome is established by a person, never by another attempt.
            task = db.get(ApplicationExecutionTask, task.id, populate_existing=True)
            _clear_lease(task)
            task.status, task.completed_at, task.outcome = COMPLETED, now, "SUBMISSION_UNCERTAIN"
            task.result_code = "SUBMISSION_UNCERTAIN"
            task.last_error = "The worker stopped after the final click started; say whether it was submitted"
            from app.services import qualification

            qualification.stop_threads(db, [task.agent_thread_id], now, note="Submission outcome unknown")
            continue
        if task.attempt_count >= limit:
            task.status, task.completed_at, task.outcome = FAILED, now, "FAILED"
            set_stage(task, "FAILED")
            task.last_error = "The worker stopped answering (lease expired) on its last attempt"
        else:
            task.status, task.next_attempt_at = PENDING, now
            set_stage(task, "RETRYING")
    db.commit()
    return len(rows)


def _held(db: Session, c: ExecutionClaim) -> ApplicationExecutionTask | None:
    q = ApplicationExecutionTask
    query = select(q).where(q.id == c.task_id, q.user_id == c.user_id)
    if _postgres(db):
        query = query.with_for_update()
    task = db.scalar(query.execution_options(populate_existing=True))
    if task is None or task.status != PROCESSING or task.lock_token != c.token:
        return None
    return task


# ───────────── the event loop the browser lives on ─────────────

def loop_runner(loop: asyncio.AbstractEventLoop, timeout: float | None = None) -> Callable[[Coroutine], Any]:
    """Run a browser coroutine from a worker thread on `loop` (the scheduler's), waiting at most `timeout`."""
    limit = timeout or get_settings().application_execution_step_timeout_seconds

    def run(coro: Coroutine) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=limit)
    return run


@contextmanager
def background_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """An event loop on its own thread, for running the worker outside the scheduler (tests, the dry run)."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="application-browser-loop", daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


# ───────────── running one task ─────────────

def attempt_note(db: Session, task: ApplicationExecutionTask, **fields: Any) -> None:
    """Write facts onto the task's current attempt row (created on first use). Does not commit."""
    row = db.scalar(select(ApplicationExecutionAttempt).where(ApplicationExecutionAttempt.task_id == task.id,
                                                              ApplicationExecutionAttempt.attempt == task.attempt_count))
    if row is None:
        row = ApplicationExecutionAttempt(user_id=task.user_id, task_id=task.id, attempt=task.attempt_count,
                                          started_at=utcnow())
        db.add(row)
    for name, value in fields.items():
        setattr(row, name, value)


def process(sessions: SessionFactory, c: ExecutionClaim, *, browser: Any = None,
            run_async: Callable[[Coroutine], Any] | None = None, saver: Any | None = None,
            hold: Callable[[str], None] | None = None) -> str:
    """Start or resume the task's graph thread, run it until it ends or waits, and settle the task. Never raises."""
    from app.services import tracing

    try:
        # Its own trace, not the scheduler pass's: Running workers links a task to exactly this run's log lines.
        with tracing.trace("application_execution", workflow_type="job", workflow_entity_id=c.job_id,
                           job_id=c.job_id, user_id=c.user_id, inherit=False), backoff.bypass(c.run_now):
            return _process(sessions, c, browser=browser, run_async=run_async, saver=saver, hold=hold)
    except Exception as exc:
        log.warning("application execution task %s failed", c.task_id, exc_info=True)
        return _settle_error(sessions, c, exc)


def _process(sessions: SessionFactory, c: ExecutionClaim, *, browser: Any, run_async: Any, saver: Any,
             hold: Any) -> str:
    from app.agents import application as graph
    from app.agents import runtime
    from app.infrastructure.application_browser import get_application_sessions

    with sessions() as db:
        db.info["user_id"] = c.user_id
        task = _held(db, c)
        if task is None:
            return "lost"
        attempt_note(db, task)
        thread = db.get(AgentThread, task.agent_thread_id) if task.agent_thread_id else None
        if thread is None:
            key = f"application:{task.job_id}:{task.id}:{task.created_at.isoformat()}:a{task.attempt_count}"
            db.commit()
            thread = runtime.start(db, graph.GRAPH, {"job_id": task.job_id, "task_id": task.id, "mode": task.mode},
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
    runtime.execute(sessions, run, command, saver=saver, hold=hold,
                    extra={"task_id": c.task_id, "browser": browser or get_application_sessions(),
                           "run_async": run_async or _no_loop})
    return _settle(sessions, c, thread_pk)


def _no_loop(coro: Coroutine) -> Any:
    return asyncio.run(coro)


def _release_busy(db: Session, c: ExecutionClaim, thread_pk: int) -> str:
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


def _settle(sessions: SessionFactory, c: ExecutionClaim, thread_pk: int) -> str:
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
            if task.stage not in WAITING_STAGES:
                set_stage(task, WAITING)
            attempt_note(db, task, stage_reached=task.stage, outcome="WAITING", ended_at=now)
            task.status, task.attempt_count = PENDING, max(0, task.attempt_count - 1)  # waiting uses no attempt
            task.next_attempt_at, task.last_error = None, None
            wake(db, task)  # everything it needs may already have been answered meanwhile
            db.commit()
            return "waiting"
        if status == "COMPLETED":
            result = thread.result or {}
            task.status, task.completed_at = COMPLETED, now
            task.outcome = str(result.get("outcome") or "")[:30] or None
            if task.outcome not in ("BLOCKED", "UNSUPPORTED"):  # a blocked run keeps why (the step recorded it)
                task.last_error = None
            task.result_code = str(result.get("code") or "")[:60] or None
            attempt_note(db, task, stage_reached=task.stage, outcome=task.outcome,
                         form_fingerprint=task.form_fingerprint, ended_at=now)
            db.commit()
            _log_end(task, "INFO", f"ended: {(task.outcome or 'done').lower()}"
                     + (f" ({task.result_code.replace('_', ' ').lower()})" if task.result_code else ""))
            return "completed"
        if status == "STOPPED":
            task.status, task.completed_at = SKIPPED, now
            task.last_error = (thread.last_error if thread is not None else None) or STOPPED_BY_USER
            task.outcome = "STOPPED"
            set_stage(task, "CANCELLED")
            attempt_note(db, task, stage_reached=task.stage, outcome="STOPPED", ended_at=now)
            db.commit()
            return "stopped"
        message = redact_text((thread.last_error if thread is not None else "") or "graph run failed", ERROR_LIMIT)
        return _retry_or_fail(db, task, message, now)


def _retry_or_fail(db: Session, task: ApplicationExecutionTask, message: str, now: datetime) -> str:
    limit = worker_settings.get(db, task.user_id, KIND, "max_attempts")
    task.last_error, task.agent_thread_id = message, None
    attempt_note(db, task, stage_reached=task.stage, outcome="FAILED", error_category="OTHER", ended_at=now)
    if task.attempt_count >= limit:
        task.status, task.completed_at, task.outcome = FAILED, now, "FAILED"
        set_stage(task, "FAILED")
        db.commit()
        _log_end(task, "ERROR", f"failed after {task.attempt_count} attempt(s): {message}")
        return "failed"
    wait = worker_settings.get(db, task.user_id, KIND, "retry_seconds") * max(1, task.attempt_count)
    task.status, task.next_attempt_at = PENDING, now + timedelta(seconds=wait)
    set_stage(task, "RETRYING")
    db.commit()
    _log_end(task, "WARNING", f"attempt {task.attempt_count} of {limit} failed, retrying in {wait}s: {message}")
    return "retrying"


def _log_end(task: ApplicationExecutionTask, level: str, what: str) -> None:
    """One Monitoring → Logs line for how a run ended, in the run's own trace (written after the commit). Never
    raises: system_log catches its own errors."""
    from app.services import system_log

    system_log.log_event(category="APPLICATION", level=level, operation="application_execution", workflow=KIND,
                         user_id=task.user_id, entity_type="job", entity_id=task.job_id,
                         status=(task.outcome or task.stage or "")[:30] or None,
                         message=f"Application run #{task.id} {what}")


def _settle_error(sessions: SessionFactory, c: ExecutionClaim, exc: Exception) -> str:
    try:
        with sessions() as db:
            db.info["user_id"] = c.user_id
            task = _held(db, c)
            if task is None:
                return "lost"
            _clear_lease(task)
            return _retry_or_fail(db, task, redact_text(f"{type(exc).__name__}: {exc}", ERROR_LIMIT), utcnow())
    except Exception:
        log.warning("could not settle application execution task %s", c.task_id, exc_info=True)
        return "lost"


# ───────────── stopping, retrying, mode off ─────────────

def on_stopped(db: Session, task: ApplicationExecutionTask, now: datetime) -> None:
    """Kill from Running workers: the task is already SKIPPED with STOPPED_BY_USER; stop its thread too. A guarded run
    stopped after its click started stays SUBMISSION_UNCERTAIN (a kill is not a "not submitted"); one stopped before
    gives its claim back."""
    from app.services import qualification, submission

    task.outcome = "STOPPED"
    _close_window(task)
    if task.mode == "GUARDED_AUTO" and submission.reconcile(db, task, "Stopped by you") == submission.UNCERTAIN:
        task = db.get(ApplicationExecutionTask, task.id, populate_existing=True)
        task.outcome = "SUBMISSION_UNCERTAIN"
    else:
        set_stage(task, "CANCELLED")
    if task.agent_thread_id:
        qualification.stop_threads(db, [task.agent_thread_id], now)


def _close_window(task: ApplicationExecutionTask) -> None:
    """A kept browser-use window (takeover) closes with its execution. Never raises."""
    try:
        from app.infrastructure.application_browser.browser_use_sidecar import get_sidecars

        manager = get_sidecars()
        if manager.get(task.user_id, task.id) is not None:
            manager.run_threadsafe(manager.close(task.user_id, task.id), timeout=20)
    except Exception:  # noqa: BLE001  the reaper closes it at the end of its takeover time anyway
        log.debug("could not close the browser window of execution %s", task.id)



def on_job_deleted(db: Session, job_id: int) -> None:
    from app.services import qualification

    q = ApplicationExecutionTask
    rows = db.scalars(select(q).where(q.job_id == job_id)).all()
    qualification.stop_threads(db, [r.agent_thread_id for r in rows], note="The job was deleted")
    for row in rows:
        db.delete(row)


def requeue(db: Session, task: ApplicationExecutionTask, user_id: int) -> ApplicationExecutionTask:
    """Retry and Run now: a new task (and so a new thread) for the job, in the same mode."""
    from app.services.workers import WorkerError

    job = db.scalar(select(Job).where(Job.id == task.job_id, Job.user_id == user_id))
    if job is None:
        raise WorkerError("The job no longer exists", 404)
    try:
        # The same backend only when a person chose it; otherwise ROUTE decides again (the site may have changed).
        chosen = task.backend if task.backend == BROWSER_USE and (task.reason or "").startswith("browser_use") else None
        fresh = enqueue(db, job, execution_mode=task.mode, reason=task.reason if chosen else "user_request",
                        backend=chosen)
    except ExecutionRefused as exc:
        raise WorkerError(str(exc)) from exc
    db.flush()
    return fresh


def release(db: Session, now: datetime | None = None, limit: int = 200) -> int:
    """Mode off with tasks left: end them (MODE_OFF) and stop their threads. Nothing else changes: the Apply kit and
    Claude Desktop keep working as before. Commits."""
    from app.services import qualification

    now = now or utcnow()
    q = ApplicationExecutionTask
    rows = db.scalars(select(q).where(q.status == PENDING).order_by(q.id).limit(limit)).all()
    qualification.stop_threads(db, [r.agent_thread_id for r in rows], now, note="The application worker is off")
    for task in rows:
        task.status, task.completed_at, task.result_code = SKIPPED, now, MODE_OFF
        set_stage(task, "CANCELLED")
    db.commit()
    return len(rows)


# ───────────── screens ─────────────

def view(task: ApplicationExecutionTask) -> dict[str, Any]:
    """The execution for the Apply kit: stage, why it waits, and each field's source and status — never a value."""
    form = task.form or {}
    plan = (task.plan or {}).get("fields") or {}
    fields = [{"id": f["id"], "label": f["label"], "required": f["required"], "kind": f["kind"], "eeo": f.get("eeo"),
               "status": (plan.get(f["id"]) or {}).get("status"),
               "source": (plan.get(f["id"]) or {}).get("source"),
               "question_id": (plan.get(f["id"]) or {}).get("question_id")} for f in form.get("fields") or []]
    return {"id": task.id, "job_id": task.job_id, "mode": task.mode, "status": task.status, "stage": task.stage,
            "outcome": task.outcome, "result_code": task.result_code, "route": task.route, "backend": task.backend,
            "interrupt": task.interrupt, "attempts": task.attempt_count, "last_error": task.last_error,
            "submit_control": (form.get("submit") or {}).get("label") if form else None, "fields": fields,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None}
