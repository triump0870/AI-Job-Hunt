"""Running workers API: the background task queues of the signed-in account, and the controls to unstick them.

Every endpoint acts on the signed-in account only (app.tenancy plus an explicit user filter). Kill and run-now never
change configuration: they move the account's own task rows and, on request, clear its provider cooldowns.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import CurrentUser, current_user, get_user_db
from app.scheduler_hooks import run_soon
from app.services import backoff, monitoring, server_settings, worker_handoff, worker_settings, workers

router = APIRouter(prefix="/api/workers")


def _scheduler(request: Request) -> Any:
    return getattr(request.app.state, "scheduler", None)


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except workers.WorkerError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.get("")
def summary(request: Request, user: CurrentUser = Depends(current_user),
            db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Every worker with its running, queued and retrying counts, its schedule, and the account's cooldowns."""
    return workers.summary(db, user.id, _scheduler(request))


@router.get("/settings")
def worker_settings_view(request: Request, user: CurrentUser = Depends(current_user),
                         db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """This account's worker settings, and the server's (editable by an admin only)."""
    return {"account": worker_settings.describe(db, user.id), "server": server_settings.describe(),
            "server_editable": bool(user.is_admin),
            "schedules": {s.kind: workers.scheduler_facts(_scheduler(request), s.scheduler_job)
                          for s in workers.KINDS.values()}}


class AccountSettingsIn(BaseModel):
    workers: dict[str, dict[str, Any]] = Field(default_factory=dict)


@router.put("/settings")
def update_worker_settings(payload: AccountSettingsIn, user: CurrentUser = Depends(current_user),
                           db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Change this account's worker settings. Configuration only: no task is moved."""
    try:
        return {"account": worker_settings.update(db, user.id, payload.workers)}
    except worker_settings.WorkerSettingError as exc:
        raise HTTPException(422, str(exc)) from exc


class ServerSettingsIn(BaseModel):
    values: dict[str, int] = Field(default_factory=dict)


@router.put("/server-settings")
def update_server_settings(payload: ServerSettingsIn, user: CurrentUser = Depends(current_user),
                           db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Change server-wide worker settings (admin only). They pace every account's scheduler jobs."""
    if not user.is_admin:
        raise HTTPException(403, "Only an admin can change the server's worker settings")
    try:
        return {"server": server_settings.update_values(db, payload.values, actor=f"user:{user.id}")}
    except server_settings.ServerSettingError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{kind}")
def worker(kind: str, request: Request, range: str = Query("7d", pattern="^(today|7d|30d|90d)$"),  # noqa: A002
           q: str = Query("", max_length=200), location: str = Query("all", max_length=200),
           status: str = Query("", max_length=20), user: CurrentUser = Depends(current_user),
           db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """One worker's tasks (active first, then the last day's finished ones) and the metrics it already has.
    `q` (job title, company or #id) and `status` (active, running, queued, retrying, failed, done) narrow the tasks;
    they never change the metrics."""
    spec = _guard(workers.kind_of, kind)
    begin, finish = monitoring.time_range(range)
    listed = _guard(workers.rows, db, user.id, spec.kind, q=q, status="" if status == "all" else status)
    out: dict[str, Any] = {"kind": spec.kind, "label": spec.label, "description": spec.description,
                           "scheduler": workers.scheduler_facts(_scheduler(request), spec.scheduler_job),
                           "cooldowns": backoff.active(db, user.id), **listed}
    if spec.kind == "parsing":
        from app.services import job_description

        out["metrics"] = job_description.metrics(db, user.id, begin, finish)
    elif spec.kind == "classification":
        from app.services import domain_classifier

        out["metrics"] = domain_classifier.metrics(db, user.id, begin, finish)
        out["mode"] = domain_classifier.mode()
        out["comparison"] = domain_classifier.compare(db, user.id)
    elif spec.kind == "recovery":
        from app.services import jd_recovery

        out["metrics"] = jd_recovery.metrics(db, user.id, begin, finish)
        out["diagnostics"] = jd_recovery.diagnostics(db, user.id)
    elif spec.kind == "ranking":
        from app.services import ranker

        tracker = ranker.tracker(db, user.id, q=q, location=location)
        progress = {r["id"]: r for r in tracker["active"]}
        for r in out["rows"]:
            extra = progress.get(r["task_id"])
            if extra:
                r.update({k: extra.get(k) for k in ("queue_position", "stage_label", "percent", "eta_seconds",
                                                    "elapsed_seconds", "location")})
        if q or location != "all":  # the tracker's own filter decides which rows match
            wanted = {r["id"] for r in tracker["active"] + tracker["recent"]}
            out["rows"] = [r for r in out["rows"] if r["task_id"] in wanted]
        out["tracker"] = {k: tracker.get(k) for k in ("counts", "progress", "concurrency", "interval_minutes",
                                                      "next_run_at", "typical_seconds", "auto_rank", "blocked",
                                                      "locations", "finished_cleared_at")}
    return out


@router.get("/{kind}/performance")
def worker_performance(kind: str, range: str = Query("7d", pattern="^(today|7d|30d|90d)$"),  # noqa: A002
                       user: CurrentUser = Depends(current_user),
                       db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """One worker's results and model calls in the selected range, read from its task rows: Clear finished, which
    only hides the list, never changes them."""
    begin, finish = monitoring.time_range(range)
    return _guard(workers.performance, db, user.id, kind, begin, finish)


@router.get("/{kind}/activity")
def worker_activity(kind: str, user: CurrentUser = Depends(current_user),
                    db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """One worker's last 24 hours, hour by hour: queued, done and failed tasks, the median run time and wait."""
    return _guard(workers.activity, db, user.id, kind)


class KillIn(BaseModel):
    task_ids: list[int] | None = Field(default=None, max_length=5000)


@router.post("/kill-all")
def kill_everything(user: CurrentUser = Depends(current_user), db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Stop and requeue fresh every running or retrying task of every worker, and clear the account's cooldowns."""
    return workers.kill_all(db, user.id)


@router.post("/cooldowns/reset")
def reset_cooldowns(provider: str | None = Query(None, max_length=30), user: CurrentUser = Depends(current_user),
                    db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Clear this account's provider rate-limit cooldowns (one provider, or all)."""
    return {"cleared": backoff.reset(db, user.id, provider)}


@router.post("/{kind}/clear")
def clear_finished(kind: str, user: CurrentUser = Depends(current_user),
                   db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Hide this tab's finished tasks from its list. Nothing is deleted; metrics and history keep them."""
    return _guard(workers.clear_finished, db, user.id, kind)


@router.delete("/{kind}/clear")
def show_cleared(kind: str, user: CurrentUser = Depends(current_user),
                 db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Show the finished tasks of the last day again."""
    return _guard(workers.clear_finished, db, user.id, kind, clear=False)


@router.post("/{kind}/kill-all")
def kill_worker(kind: str, user: CurrentUser = Depends(current_user),
                db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Stop and requeue fresh every running or retrying task of one worker, and clear the account's cooldowns."""
    return _guard(workers.kill, db, user.id, kind, None)


@router.post("/{kind}/tasks/{task_id}/kill")
def kill_task(kind: str, task_id: int, user: CurrentUser = Depends(current_user),
              db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Stop one task's current attempt and requeue it fresh (attempts and retry wait reset)."""
    return _guard(workers.kill, db, user.id, kind, [task_id])


class RetryIn(BaseModel):
    task_ids: list[int] = Field(default_factory=list, max_length=workers.RETRY_LIMIT)


@router.post("/{kind}/retry")
def retry_tasks(kind: str, payload: RetryIn, request: Request, background: BackgroundTasks,
                user: CurrentUser = Depends(current_user), db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Queue the chosen failed or blocked tasks again, in normal order. Each is reported retried or skipped."""
    out = _guard(workers.retry, db, user.id, kind, payload.task_ids)
    job = workers.KINDS[kind].scheduler_job
    if job and out["retried"]:
        run_soon(background, request.app, job)
    return out


@router.post("/{kind}/run-queued")
def run_queued(kind: str, request: Request, background: BackgroundTasks, user: CurrentUser = Depends(current_user),
               db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Run every queued or waiting task of this worker now, in bulk (see workers.run_queued_now)."""
    out = _guard(workers.run_queued_now, db, user.id, kind)
    job = workers.KINDS[kind].scheduler_job
    if job and out["released"]:
        run_soon(background, request.app, job)
    return out


class NudgeIn(BaseModel):
    job_ids: list[int] | None = Field(default=None, max_length=500)


@router.post("/{kind}/nudge")
def nudge_forward(kind: str, payload: NudgeIn | None = None, user: CurrentUser = Depends(current_user),
                  db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Move this worker's finished jobs that never reached the next stage on to it (each through its normal path).
    `job_ids` limits it to those jobs: one row's button, or the rows shown."""
    _guard(workers.kind_of, kind)
    return worker_handoff.nudge_forward(db, user.id, kind, payload.job_ids if payload else None)


@router.get("/{kind}/tasks/{task_id}/history")
def task_history(kind: str, task_id: int, user: CurrentUser = Depends(current_user),
                 db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """What happened to one task, newest first: its attempts, graph steps, the job's moves and events, page loads
    and model calls. Metadata only."""
    return _guard(workers.history, db, user.id, kind, task_id)


@router.post("/{kind}/tasks/{task_id}/run-now")
def run_task_now(kind: str, task_id: int, request: Request, background: BackgroundTasks,
                 user: CurrentUser = Depends(current_user), db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Run one task on the next pass: first in line, past its retry wait, the ranking cadence and provider cooldowns."""
    out = _guard(workers.run_now, db, user.id, kind, task_id)
    job = workers.KINDS[kind].scheduler_job
    if job:
        run_soon(background, request.app, job)
    return out
