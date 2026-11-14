"""Monitoring & AI Optimization API.

Every endpoint shows only the signed-in account's LLM telemetry. The admin may pass all_accounts=true to see every
account. Nothing here returns API keys, prompt text (unless LLM_PROMPT_DEBUG stored it) or resume and job content.
Analyses are queued for the scheduler and produce recommendations only. A recommendation's one-click change is made
only when the person clicks, and only through the AI configuration's draft → preview → apply
(app.services.advisor_actions).
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import CurrentUser, current_user, get_user_db
from app.models import (
    EXTERNAL_ERROR_TYPES,
    SYSTEM_LOG_CATEGORIES,
    SYSTEM_LOG_LEVELS,
    AIOptimizationReport,
    AIOptimizationTask,
)
from app.scheduler_hooks import run_soon
from app.services import (
    advisor_actions,
    ai_advisor,
    ai_model_config,
    external_monitoring,
    model_performance,
    monitoring,
)
from app.services.user_settings import lock_settings_row, settings_for

router = APIRouter(prefix="/api/monitoring")
log = logging.getLogger("jobhunt.monitoring_api")


def monitoring_filter(
    user: CurrentUser = Depends(current_user),
    range: str = Query("7d", pattern="^(today|7d|30d|90d)$"),  # noqa: A002  query parameter name
    start: datetime | None = None,
    end: datetime | None = None,
    provider: str | None = Query(None, max_length=30),
    model: str | None = Query(None, max_length=300),
    stage: str | None = Query(None, max_length=30),
    prompt_version: str | None = Query(None, max_length=20),
    status: str | None = Query(None, pattern="^(SUCCEEDED|FAILED)$"),
    workflow_type: str | None = Query(None, max_length=30),
    job_id: int | None = None,
    entity_id: int | None = None,
    worker_type: str | None = Query(None, max_length=40),
    config_version: int | None = Query(None, ge=0),
    routing_tier: str | None = Query(None, max_length=30),
    all_accounts: bool = False,
) -> monitoring.MonitoringFilter:
    if all_accounts and not user.is_admin:
        raise HTTPException(403, "Only the admin can view every account's usage")
    begin, finish = monitoring.time_range(range, start, end)
    if finish < begin:
        raise HTTPException(422, "end must be after start")
    if job_id is not None:
        workflow_type, entity_id = "job", job_id
    return monitoring.MonitoringFilter(start=begin, end=finish, user_id=None if all_accounts else user.id,
                                       provider=provider, model=model, stage=stage, prompt_version=prompt_version,
                                       status=status, workflow_type=workflow_type, entity_id=entity_id,
                                       worker_type=worker_type, config_version=config_version,
                                       routing_tier=routing_tier)


Filter = Depends(monitoring_filter)


@router.get("/dashboard")
def dashboard(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)):
    """Everything the Monitoring tab shows, in one call."""
    return monitoring.dashboard(db, f)


@router.get("/summary")
def summary(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)):
    return {"filter": f.public(), **monitoring.summary(monitoring.rows(db, f))}


@router.get("/stages")
def stages(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)):
    return {"filter": f.public(), "stages": monitoring.by_stage(monitoring.rows(db, f))}


@router.get("/models")
def models(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)):
    return {"filter": f.public(), "models": monitoring.by_model(monitoring.rows(db, f))}


@router.get("/performance")
def performance(f: monitoring.MonitoringFilter = Filter,
                group_by: str = Query("model", pattern="^(model|provider|worker|stage|prompt_version|config_version|"
                                                       "routing_tier|service_tier|hour|day)$"),
                db: Session = Depends(get_user_db)):
    """Model comparison: reliability, latency, throughput, cost, quality and context for every value of `group_by`,
    computed from the raw request rows. Outcome signals (reviewer, corrections) are the account's own."""
    items = monitoring.rows(db, f, model_performance.COLUMNS)
    out = model_performance.compare(items, group_by, f.start, f.end)
    out["filter"] = f.public()
    out["truncated"] = len(items) >= monitoring.MAX_ROWS
    out["outcomes"] = (model_performance.quality_signals(db, f.user_id, f.start, f.end)
                       if f.user_id is not None else None)  # per-account tables: not aggregated across accounts
    out["options"] = monitoring.options(db, f)
    return out


@router.get("/feedback")
def feedback(user: CurrentUser = Depends(current_user), db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Human correction rates of the signed-in account's own automated results, and its recruiter outcomes beside
    them (never attributed to a model). Always the account's own: per-account tables are not pooled."""
    from app.services.agent_eval import feedback as agent_feedback

    return agent_feedback.report(db, user.id)


@router.get("/costs")
def costs(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)):
    items = monitoring.rows(db, f)
    return {"filter": f.public(), "total": monitoring.summary(items)["estimated_cost"],
            "timeseries": monitoring.timeseries(items, f),
            "by_model": [{k: m[k] for k in ("provider", "model", "requests", "estimated_cost", "unpriced_requests", "free")}
                         for m in monitoring.by_model(items)],
            "by_stage": [{k: s[k] for k in ("stage", "requests", "estimated_cost", "cost_share", "unpriced_requests")}
                         for s in monitoring.by_stage(items)]}


@router.get("/latency")
def latency(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)):
    return {"filter": f.public(), **monitoring.latency(monitoring.rows(db, f))}


@router.get("/context")
def context(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)):
    return {"filter": f.public(), **monitoring.context(monitoring.rows(db, f))}


@router.get("/errors")
def errors(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)):
    return {"filter": f.public(), **monitoring.errors(monitoring.rows(db, f))}


@router.get("/requests")
def requests(f: monitoring.MonitoringFilter = Filter, page: int = Query(1, ge=1), per_page: int = Query(25, ge=1, le=100),
             db: Session = Depends(get_user_db)):
    return {"filter": f.public(), **monitoring.list_requests(db, f, page, per_page)}


@router.get("/requests/{request_id}")
def request_detail(request_id: int, all_accounts: bool = False, user: CurrentUser = Depends(current_user),
                   db: Session = Depends(get_user_db)):
    if all_accounts and not user.is_admin:
        raise HTTPException(403, "Only the admin can view every account's usage")
    detail = monitoring.request_detail(db, request_id, None if all_accounts else user.id)
    if detail is None:
        raise HTTPException(404, "LLM request not found")
    return detail


# ───────────── external APIs ─────────────

def external_filter(
    user: CurrentUser = Depends(current_user),
    range: str = Query("7d", pattern="^(today|7d|30d|90d)$"),  # noqa: A002  query parameter name
    start: datetime | None = None,
    end: datetime | None = None,
    provider: str | None = Query(None, max_length=30),
    service: str | None = Query(None, max_length=60),
    operation: str | None = Query(None, max_length=60),
    workflow: str | None = Query(None, max_length=60),
    status: str | None = Query(None, pattern="^(SUCCEEDED|FAILED)$"),
    error_only: bool = False,
    error_type: str | None = Query(None, max_length=30),
    http_status_code: int | None = Query(None, ge=100, le=599),
    job_id: int | None = None,
    application_id: int | None = None,
    trace_id: str | None = Query(None, max_length=32),
    all_accounts: bool = False,
) -> external_monitoring.ExternalFilter:
    if all_accounts and not user.is_admin:
        raise HTTPException(403, "Only the admin can view every account's usage")
    begin, finish = monitoring.time_range(range, start, end)
    if finish < begin:
        raise HTTPException(422, "end must be after start")
    if error_type and error_type.upper() not in EXTERNAL_ERROR_TYPES:
        raise HTTPException(422, f"Unknown failure type '{error_type}'")
    return external_monitoring.ExternalFilter(
        start=begin, end=finish, user_id=None if all_accounts else user.id,
        provider=(provider or "").upper() or None, service=service, operation=operation, workflow=workflow,
        status=status, error_only=error_only, error_type=(error_type or "").upper() or None,
        http_status_code=http_status_code, job_id=job_id, application_id=application_id, trace_id=trace_id)


ExtFilter = Depends(external_filter)


@router.get("/external/dashboard")
def external_dashboard(f: external_monitoring.ExternalFilter = ExtFilter, db: Session = Depends(get_user_db)):
    """Everything the External APIs tab shows, in one call."""
    return external_monitoring.dashboard(db, f)


@router.get("/external/summary")
def external_summary(f: external_monitoring.ExternalFilter = ExtFilter, db: Session = Depends(get_user_db)):
    return {"filter": f.public(), **external_monitoring.summary(external_monitoring.rows(db, f))}


@router.get("/external/providers")
def external_providers(f: external_monitoring.ExternalFilter = ExtFilter, db: Session = Depends(get_user_db)):
    """Per-provider health: success, failure, 429, 5xx, timeout and retry rates, with latency."""
    items = external_monitoring.rows(db, f)
    return {"filter": f.public(), "providers": external_monitoring.by_provider(items),
            "operations": external_monitoring.by_operation(items)}


@router.get("/external/filters")
def external_filters(f: external_monitoring.ExternalFilter = ExtFilter, db: Session = Depends(get_user_db)):
    """Everything that can be filtered for in this range — the choices, not the current selection."""
    return {"filter": f.public(), **external_monitoring.facets(db, f),
            "all_error_types": list(EXTERNAL_ERROR_TYPES)}


@router.get("/external/latency")
def external_latency(f: external_monitoring.ExternalFilter = ExtFilter, db: Session = Depends(get_user_db)):
    return {"filter": f.public(), **external_monitoring.latency(external_monitoring.rows(db, f))}


@router.get("/external/errors")
def external_errors(f: external_monitoring.ExternalFilter = ExtFilter, db: Session = Depends(get_user_db)):
    return {"filter": f.public(), **external_monitoring.errors(external_monitoring.rows(db, f)),
            "error_types": list(EXTERNAL_ERROR_TYPES)}


@router.get("/external/requests")
def external_requests(f: external_monitoring.ExternalFilter = ExtFilter, page: int = Query(1, ge=1),
                      per_page: int = Query(25, ge=1, le=100), db: Session = Depends(get_user_db)):
    return {"filter": f.public(), **external_monitoring.list_requests(db, f, page, per_page)}


@router.get("/external/requests/{request_id}")
def external_request_detail(request_id: int, all_accounts: bool = False, user: CurrentUser = Depends(current_user),
                            db: Session = Depends(get_user_db)):
    if all_accounts and not user.is_admin:
        raise HTTPException(403, "Only the admin can view every account's usage")
    detail = external_monitoring.request_detail(db, request_id, None if all_accounts else user.id)
    if detail is None:
        raise HTTPException(404, "External API request not found")
    return detail


@router.get("/external/traces/{trace_id}")
def external_trace(trace_id: str, all_accounts: bool = False, user: CurrentUser = Depends(current_user),
                   db: Session = Depends(get_user_db)):
    """Every call and event that shares one correlation ID, in order."""
    if all_accounts and not user.is_admin:
        raise HTTPException(403, "Only the admin can view every account's usage")
    return external_monitoring.trace_detail(db, trace_id[:32], None if all_accounts else user.id)


@router.get("/apify/summary")
def apify_summary(f: external_monitoring.ExternalFilter = ExtFilter, db: Session = Depends(get_user_db)):
    """Apify runs, what they found, and the usage and cost Apify itself reported."""
    return {"filter": f.public(), **external_monitoring.apify_summary(db, f)}


@router.get("/apify/runs")
def apify_runs(f: external_monitoring.ExternalFilter = ExtFilter, page: int = Query(1, ge=1),
               per_page: int = Query(25, ge=1, le=100), db: Session = Depends(get_user_db)):
    from app.models import SavedSearch

    runs = external_monitoring.apify_runs(db, f, limit=per_page * page + 1)
    setups = {s.id: s.name for s in db.scalars(select(SavedSearch)).all()}
    page_items = runs[(page - 1) * per_page: page * per_page]
    return {"filter": f.public(), "items": [external_monitoring.run_card(r, setups) for r in page_items],
            "page": page, "per_page": per_page, "has_more": len(runs) > page * per_page}


@contextmanager
def owned_scope(f: monitoring.MonitoringFilter, db: Session) -> Iterator[Session]:
    """The session for metrics over owned tables. The request's session only ever sees the signed-in account
    (app.tenancy), so the admin's every-account view (user_id None) reads through an unscoped one; a person's
    own view keeps the scoped session, with the explicit user filter as a second guard."""
    if f.user_id is not None:
        yield db
        return
    from app.db import SessionLocal

    with SessionLocal() as everyone:
        yield everyone


@router.get("/jd-recovery")
def jd_recovery_metrics(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Job description recovery: success and fallback rates, blocked and failed pages, and what recovered jobs did
    next (parsed, ranked), plus what recovery can use right now."""
    from app.services import jd_recovery

    with owned_scope(f, db) as scope:
        found = jd_recovery.metrics(scope, f.user_id, f.start, f.end)
    return {"filters": f.public(), "metrics": found,
            "diagnostics": jd_recovery.diagnostics(db, db.info.get("user_id"))}


@router.get("/jd-parsing")
def jd_parsing_metrics(f: monitoring.MonitoringFilter = Filter, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Job description parsing: parse tasks and their outcomes, the queue, the parser's model calls (LLM stage
    JD_PARSER) and the scheduler job."""
    from app.services import job_description

    with owned_scope(f, db) as scope:
        return {"filters": f.public(), "metrics": job_description.metrics(scope, f.user_id, f.start, f.end)}


@router.get("/application-inbox")
def application_inbox_metrics(f: monitoring.MonitoringFilter = Filter,
                              db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Application Inbox: sync runs, Gmail requests and failures, matching (deterministic, model-assisted, by you),
    classifications, stage changes applied or suggested, reviews and corrections. Model calls themselves are in the
    LLM requests view under the Inbox stage."""
    from app.services.application_inbox import service as inbox

    with owned_scope(f, db) as scope:
        found = inbox.metrics(scope, f.user_id, f.start, f.end)
    return {"filters": f.public(), "metrics": found, "status": inbox.status(db)}


@router.get("/logs")
def logs(user: CurrentUser = Depends(current_user),
         range: str = Query("7d", pattern="^(today|7d|30d|90d)$"),  # noqa: A002  query parameter name
         start: datetime | None = None, end: datetime | None = None,
         category: str | None = Query(None, max_length=20), level: str | None = Query(None, max_length=10),
         provider: str | None = Query(None, max_length=30), workflow: str | None = Query(None, max_length=60),
         trace_id: str | None = Query(None, max_length=32), search: str | None = Query(None, max_length=120),
         page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=200), all_accounts: bool = False,
         db: Session = Depends(get_user_db)):
    """Operational events, newest first. Paginated: log history is never returned in full."""
    if all_accounts and not user.is_admin:
        raise HTTPException(403, "Only the admin can view every account's logs")
    if category and category not in SYSTEM_LOG_CATEGORIES:
        raise HTTPException(422, f"Unknown log category '{category}'")
    if level and level not in SYSTEM_LOG_LEVELS:
        raise HTTPException(422, f"Unknown log level '{level}'")
    begin, finish = monitoring.time_range(range, start, end)
    return {"filter": {"range": range, "category": category, "level": level, "provider": provider,
                       "workflow": workflow, "trace_id": trace_id, "search": search},
            "levels": list(SYSTEM_LOG_LEVELS), "all_categories": list(SYSTEM_LOG_CATEGORIES),
            **external_monitoring.logs(db, start=begin, end=finish, user_id=None if all_accounts else user.id,
                                       category=category, level=level, provider=(provider or "").upper() or None,
                                       workflow=workflow, trace_id=trace_id, search=search, page=page,
                                       per_page=per_page)}


class CaptureIn(BaseModel):
    enabled: bool


def _capture_state(db: Session, user_id: int) -> dict[str, Any]:
    from app.services.llm_telemetry import CAPTURE_PREFERENCE
    from app.settings import get_settings

    saved = bool(((settings_for(db, user_id).preferences or {}).get("monitoring") or {}).get(CAPTURE_PREFERENCE))
    return {"enabled": saved, "server_wide": get_settings().llm_prompt_debug,
            "retention_days": get_settings().llm_prompt_debug_retention_days}


@router.get("/capture")
def capture_get(user: CurrentUser = Depends(current_user), db: Session = Depends(get_user_db)):
    """Whether this account's LLM requests keep their full prompt and response (off unless turned on)."""
    return _capture_state(db, user.id)


@router.put("/capture")
def capture_put(payload: CaptureIn, user: CurrentUser = Depends(current_user), db: Session = Depends(get_user_db)):
    """Turn full prompt and response capture on or off for this account. Captured text is redacted and cleared
    after llm_prompt_debug_retention_days; turning capture off does not erase what was already captured."""
    from app.services.llm_telemetry import CAPTURE_PREFERENCE, forget_capture_setting

    row = lock_settings_row(db, settings_for(db, user.id))
    preferences = dict(row.preferences or {})
    row.preferences = {**preferences, "monitoring": {**(preferences.get("monitoring") or {}),
                                                     CAPTURE_PREFERENCE: payload.enabled}}
    db.commit()
    forget_capture_setting(user.id)
    return _capture_state(db, user.id)


@router.get("/logs/{log_id}")
def log_detail(log_id: int, all_accounts: bool = False, user: CurrentUser = Depends(current_user),
               db: Session = Depends(get_user_db)):
    if all_accounts and not user.is_admin:
        raise HTTPException(403, "Only the admin can view every account's logs")
    detail = external_monitoring.log_detail(db, log_id, None if all_accounts else user.id)
    if detail is None:
        raise HTTPException(404, "Log entry not found")
    return detail


# ───────────── AI Optimization Advisor ─────────────

class AnalyzeIn(BaseModel):
    range: str = Field("7d", pattern="^(today|7d|30d|90d)$")
    start: datetime | None = None
    end: datetime | None = None


@router.post("/analyze", status_code=202)
def analyze(payload: AnalyzeIn, request: Request, background: BackgroundTasks,
            user: CurrentUser = Depends(current_user), db: Session = Depends(get_user_db)):
    """Queue an analysis of this account's usage. It runs in the background and only produces recommendations."""
    begin, finish = monitoring.time_range(payload.range, payload.start, payload.end)
    try:
        task = ai_advisor.queue_analysis(db, user.id, begin, finish)
    except ai_advisor.ActiveAnalysis as exc:
        raise HTTPException(409, str(exc)) from exc
    run_soon(background, request.app, "ai_optimization")
    return {"task": ai_advisor.task_dict(task)}


@router.get("/recommendations")
def recommendations(db: Session = Depends(get_user_db), user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """The latest report, recent analysis tasks, and this account's analysis schedule."""
    report = db.scalar(select(AIOptimizationReport).order_by(AIOptimizationReport.created_at.desc(),
                                                             AIOptimizationReport.id.desc()).limit(1))
    tasks = db.scalars(select(AIOptimizationTask).order_by(AIOptimizationTask.id.desc()).limit(10)).all()
    return {"report": ai_advisor.report_dict(report) if report else None,
            "tasks": [ai_advisor.task_dict(t) for t in tasks],
            "active": any(t.status in ai_advisor.ACTIVE for t in tasks),
            "schedule": ai_advisor.schedule_of(db, user.id),
            "actions": _overview(db, user.id, report),
            "notice": "Nothing changes on its own. Review a suggested change, then apply it with one click; every "
                      "applied change is a configuration version you can undo."}


def _overview(db: Session, user_id: int, report: AIOptimizationReport | None) -> dict[str, Any]:
    """The one-click layer. If it cannot be built, the report still shows, just without its actions."""
    try:
        return advisor_actions.overview(db, user_id, report)
    except Exception as exc:  # noqa: BLE001 - the recommendations themselves must stay readable
        db.rollback()
        log.warning("AI optimization actions unavailable for account %s: %s", user_id, type(exc).__name__)
        return {"items": [], "blocked": "One-click changes are unavailable right now; the recommendations still apply.",
                "staged": None, "decisions": []}


def _actions_call(fn, *args: Any) -> Any:
    try:
        return fn(*args)
    except advisor_actions.ActionError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except ai_model_config.AIConfigError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.post("/recommendations/{report_id}/actions/{action_id}/stage")
def stage_action(report_id: int, action_id: str = Path(max_length=160), db: Session = Depends(get_user_db),
                 user: CurrentUser = Depends(current_user)):
    """Put one suggested change into the AI configuration draft and preview it. Nothing is live until apply."""
    return _actions_call(advisor_actions.stage, db, user.id, report_id, action_id)


@router.post("/recommendations/decisions/{decision_id}/apply")
def apply_action(decision_id: int, db: Session = Depends(get_user_db), user: CurrentUser = Depends(current_user)):
    return _actions_call(advisor_actions.apply, db, user.id, decision_id)


@router.post("/recommendations/decisions/{decision_id}/cancel")
def cancel_action(decision_id: int, db: Session = Depends(get_user_db), user: CurrentUser = Depends(current_user)):
    return _actions_call(advisor_actions.cancel, db, user.id, decision_id)


@router.post("/recommendations/decisions/{decision_id}/undo")
def undo_action(decision_id: int, db: Session = Depends(get_user_db), user: CurrentUser = Depends(current_user)):
    return _actions_call(advisor_actions.undo, db, user.id, decision_id)


class DismissIn(BaseModel):
    key: str = Field(min_length=1, max_length=40)


@router.post("/recommendations/{report_id}/dismiss")
def dismiss_recommendation(report_id: int, payload: DismissIn, db: Session = Depends(get_user_db),
                           user: CurrentUser = Depends(current_user)):
    """Hide a recommendation (and the same advice in later reports) for 30 days."""
    return _actions_call(advisor_actions.dismiss, db, user.id, report_id, payload.key)


@router.post("/recommendations/decisions/{decision_id}/restore")
def restore_recommendation(decision_id: int, db: Session = Depends(get_user_db),
                           user: CurrentUser = Depends(current_user)):
    return _actions_call(advisor_actions.restore_dismissed, db, user.id, decision_id)


@router.get("/reports/{report_id}")
def report(report_id: int, db: Session = Depends(get_user_db)):
    item = db.get(AIOptimizationReport, report_id)
    if item is None:
        raise HTTPException(404, "Report not found")
    return ai_advisor.report_dict(item, include_aggregates=True)


class ScheduleIn(BaseModel):
    schedule: str = Field(pattern="^(off|daily|weekly)$")


@router.put("/schedule")
def schedule(payload: ScheduleIn, user: CurrentUser = Depends(current_user), db: Session = Depends(get_user_db)):
    """The account's own choice of scheduled analysis. This is a user preference, not production configuration."""
    row = lock_settings_row(db, settings_for(db, user.id))
    preferences = dict(row.preferences or {})
    preferences["ai_advisor"] = {**(preferences.get("ai_advisor") or {}), "schedule": payload.schedule}
    row.preferences = preferences
    db.commit()
    return {"schedule": payload.schedule}
