"""AI Orchestration: a read-only projection of what is running, where each job is, and which model handles it.

    topology  (app.services.orchestration_topology)          what the stages are; static
    runtime   (this module)                                   what the stages are doing; computed on request

Nothing here is a new source of truth. Every number is aggregated from tables other parts of the system already own:

    jobs, workflow_transitions, *_tasks, resume_variants,      where work is and how it moved (business state)
    applications, application_events, followups
    llm_request_logs, external_api_request_logs, system_logs    how calls went (Monitoring telemetry)
    ai_config_versions + model_resolution / ai_config            which model a worker is assigned (configuration)
    ai_optimization_reports                                      the advisor's recommendations (advice only)

Rules this module keeps:
* Deterministic. No model is called to draw the graph, decide a node's health or describe a job's history.
* Metadata only. No prompt, response, resume, email body, application answer or credential is ever read into a
  payload; error messages pass through app.services.redaction and are capped.
* Configured, requested and actual model are separate facts. The actual model comes only from telemetry
  (llm_request_logs.model); when nothing recorded it, it is unknown — never inferred from the configuration.
* Tenancy. Owned tables are read through the caller's scoped session plus an explicit user filter; the two
  telemetry tables are filtered by user_id in every query. The admin's every-account view returns aggregates only:
  job titles, companies and packets always belong to the signed-in account.
* Bounded. Every query has a limit; topology is static; graph aggregates are cached for a few seconds, and the
  live poll touches only active task rows.
"""
from __future__ import annotations

import threading
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AIConfigVersion,
    AIModel,
    AIOptimizationReport,
    Application,
    ApplicationClaim,
    ApplicationCommunicationEvent,
    ApplicationEmailMessage,
    ApplicationEvent,
    ApplicationExecutionTask,
    ExternalAPIRequestLog,
    FollowUp,
    Job,
    JobDescriptionFetchTask,
    JobClassificationTask,
    JobDescriptionParseTask,
    JobInsightTask,
    JobPreparationTask,
    JobQualificationTask,
    JobRanking,
    JobRankingTask,
    LLMRequestLog,
    RagIndexEntry,
    RagIndexVersion,
    ResumeVariant,
    SearchRun,
    SubmissionIntent,
    SystemLogEntry,
    TailorTask,
    WorkflowTransition,
)
from app.services import external_monitoring, model_performance, model_resolution, redaction, worker_settings
from app.services import orchestration_topology as topo
from app.services.ai_workers import WORKERS, worker_for_prompt
from app.services.ai_workers import label as worker_label
from app.services.monitoring import _mean, _rate, percentile
from app.settings import get_settings

# ───────────── policy: every threshold that decides a status, in one place ─────────────


@dataclass(frozen=True)
class HealthPolicy:
    """How a node's status is decided. Deterministic, documented, and returned with the graph so the UI can say why.

    Checked in this order; the first that applies wins:
      DISABLED  every AI worker of an AI-first node is switched off in the active configuration
      ERROR     the last `error_consecutive_failures` calls (or finished tasks) inside the window all failed
      DEGRADED  in the window, at least `min_samples` calls or tasks and a failure rate ≥ `degraded_failure_rate`,
                or a schema failure rate ≥ `degraded_schema_failure_rate`, or no model can be resolved for an
                enabled worker, or its model failed its last health check
      BUSY      something is running or queued right now
      HEALTHY   activity in the selected range, and none of the above
      NO_DATA   nothing recorded in the selected range and nothing queued
    One historic failure never makes a node ERROR: the window and the consecutive rule both have to agree.
    """

    window_hours: int = 24
    min_samples: int = 5
    degraded_failure_rate: float = 0.10
    degraded_schema_failure_rate: float = 0.10
    error_consecutive_failures: int = 3


POLICY = HealthPolicy()
# The same rules in words, in order, for the legend ("how is this status decided?").
HEALTH_RULES = (
    ("DISABLED", "Every AI worker of an AI-first stage is switched off in the active configuration"),
    ("ERROR", "The last {error_consecutive_failures} model calls or finished tasks within {window_hours}h all failed"),
    ("DEGRADED", "Within {window_hours}h: at least {min_samples} calls or tasks and ≥{degraded_failure_pct}% failed, "
                 "or ≥{degraded_schema_pct}% of answers failed their schema; or no model can be resolved for an "
                 "enabled worker; or its model failed its last health check"),
    ("BUSY", "Something is running or queued right now"),
    ("HEALTHY", "Activity in the selected range and none of the above"),
    ("NO_DATA", "Nothing recorded in the selected range and nothing queued"),
)


def health_rules() -> list[dict[str, str]]:
    values = {**asdict(POLICY), "degraded_failure_pct": round(POLICY.degraded_failure_rate * 100),
              "degraded_schema_pct": round(POLICY.degraded_schema_failure_rate * 100)}
    return [{"status": status, "rule": rule.format(**values)} for status, rule in HEALTH_RULES]
STATUSES = ("HEALTHY", "BUSY", "DEGRADED", "ERROR", "DISABLED", "NO_DATA")
# A job's position at one stage. CURRENT is where it is now; WAITING is a stage it has not reached yet.
STEP_STATUSES = ("COMPLETED", "CURRENT", "WAITING", "FAILED", "RETRYING", "SKIPPED", "BLOCKED")
# What the job is doing at its current stage.
ACTIVITIES = ("RUNNING", "QUEUED", "RETRYING", "AWAITING_HUMAN", "WAITING_TO_START", "MONITORING", "BLOCKED",
              "FAILED")
LOW_SAMPLE = 20  # fewer requests than this: comparisons are flagged as a small sample

GRAPH_CACHE_SECONDS = 15
HISTORY_CACHE_SECONDS = 60
LLM_ROW_LIMIT = 20_000
EXTERNAL_ROW_LIMIT = 20_000
EDGE_ROW_LIMIT = 5_000
LIVE_LIMIT = 50
JOB_TELEMETRY_LIMIT = 200
TRACE_LIMIT = 400
RECENT_LIMIT = 8
CONFIG_VERSION_LIMIT = 200
ERROR_TEXT_LIMIT = 240

# Tracker stages (app.services.tracker) grouped the way the graph shows them.
INTERVIEW_STAGES = ("screening", "interviewing", "takehome")
OFFER_STAGES = ("offer", "negotiating")
CLOSED_STAGES = ("rejected", "closed")
# In direct-provider mode a worker's model is the Settings stage model of its task (app.services.llm).
WORKER_TASK = {"RANKER": "rank", "TAILOR": "tailor", "REVIEWER": "review", "FOLLOWUP": "followup",
               "AI_OPTIMIZATION": "advisor", "EVIDENCE_EXTRACTION": "tailor"}
# Workers that run in Claude Desktop (over MCP) instead of the backend when the account chose that.
DESKTOP_WORKERS = ("TAILOR", "REVIEWER")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None or end < start:
        return None
    return round((end - start).total_seconds(), 3)


def _safe_error(text: str | None) -> str | None:
    return redaction.redact_text(text, ERROR_TEXT_LIMIT) if text else None


@dataclass(frozen=True)
class Scope:
    """Whose data a view covers and over which period. `user_id` None is the admin's every-account view."""

    viewer_id: int
    user_id: int | None
    start: datetime
    end: datetime
    range_name: str = "7d"

    @property
    def all_accounts(self) -> bool:
        return self.user_id is None

    def public(self) -> dict[str, Any]:
        return {"range": self.range_name, "start": _iso(self.start), "end": _iso(self.end),
                "all_accounts": self.all_accounts}


def _owned(query, column, scope_user: int | None):
    """A second guard on top of the scoped session: filter an owned table by account explicitly."""
    return query if scope_user is None else query.where(column == scope_user)


# ───────────── small caches (aggregates only; never per-request payloads) ─────────────

_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, Any]] = {}


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cached(key: tuple, ttl: int, build):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = build()
    with _cache_lock:
        if len(_cache) > 256:
            _cache.clear()
        _cache[key] = (now + ttl, value)
    return value


# ───────────── model assignment: configured → requested → actual ─────────────

def gateway_state(db: Session, user_id: int) -> dict[str, Any]:
    """How this account's workers reach a model right now: LiteLLM gateway mode or a direct provider."""
    from app.services.ai_config import resolve as resolve_ai_config
    from app.services.user_settings import ai_overrides, execution_mode_for

    ai = resolve_ai_config(get_settings(), ai_overrides(db, user_id))
    gateway = ai["llm_provider"] == "litellm"
    version = model_resolution.active_snapshot(user_id)[0] if gateway else None
    return {"mode": "litellm" if gateway else "direct", "provider": ai["llm_provider"],
            "default_model": None if gateway else (ai.get("model") or None),
            "stage_models": {} if gateway else dict(ai.get("stage_models") or {}),
            "config_version": version, "execution_mode": execution_mode_for(db, user_id)}


def worker_assignment(worker_type: str, gateway: dict[str, Any], user_id: int) -> dict[str, Any]:
    """What one worker is configured to use. Never what answered: that is telemetry (see `observed_models`)."""
    worker = WORKERS.get(worker_type)
    base = {"worker_type": worker_type, "label": worker_label(worker_type),
            "has_prompt": bool(worker and worker.prompts)}
    if gateway["execution_mode"] == "claude_desktop" and worker_type in DESKTOP_WORKERS:
        base["executed_by"] = "claude_desktop"
    if gateway["mode"] != "litellm":
        task = WORKER_TASK.get(worker_type, "default")
        model = gateway["stage_models"].get(task) or gateway["default_model"]
        return {**base, "mode": "DIRECT", "provider": gateway["provider"], "configured": model, "resolved_model": model,
                "fallback_model": None, "escalation": None, "enabled": True, "config_version": None,
                "note": f"Direct provider mode: the {task} model from Settings", "router": None}
    version, snapshot = model_resolution.active_snapshot(user_id)
    resolved = model_resolution.resolve(snapshot, worker_type, version)
    routing = snapshot.get("routing") or {}
    router = None
    if resolved.mode == "AUTO_ROUTER":
        router = {"active": model_resolution.router_active(snapshot),
                  "model": routing.get("auto_router_model_name"), "classifier": routing.get("classifier"),
                  "tiers": {tier: name for tier, name in (routing.get("tier_models") or {}).items()},
                  "default_model": routing.get("default_model")}
    escalation = None
    if resolved.escalation:
        escalation = {"model": resolved.escalation.model, "label": resolved.escalation.label,
                      "conditions": list(resolved.escalation.conditions),
                      "confidence_threshold": resolved.escalation.confidence_threshold}
    return {**base, "mode": resolved.mode or "TIER", "provider": "litellm", "configured": resolved.configured,
            "resolved_model": resolved.model, "fallback_model": resolved.fallback_model, "escalation": escalation,
            "enabled": resolved.enabled, "config_version": version, "note": resolved.note, "router": router,
            "max_concurrency": resolved.max_concurrency, "timeout_seconds": resolved.timeout_seconds}


def assignments(db: Session, user_id: int) -> dict[str, Any]:
    """Every worker's assignment under the account's active configuration, plus how requests reach a model."""
    gateway = gateway_state(db, user_id)
    return {"gateway": gateway,
            "workers": {w: worker_assignment(w, gateway, user_id) for w in WORKERS}}


def _model_health(db: Session, user_id: int) -> dict[str, dict[str, Any]]:
    rows = db.execute(_owned(select(AIModel.litellm_model_name, AIModel.health_status, AIModel.health_checked_at,
                                    AIModel.enabled), AIModel.user_id, user_id)).all()
    return {r[0]: {"health_status": r[1], "health_checked_at": _iso(r[2]), "enabled": r[3]} for r in rows}


# ───────────── telemetry rows (narrow, bounded, metadata only) ─────────────

LLM_COLUMNS = (LLMRequestLog.id, LLMRequestLog.created_at, LLMRequestLog.completed_at, LLMRequestLog.user_id,
               LLMRequestLog.stage, LLMRequestLog.worker_type, LLMRequestLog.status, LLMRequestLog.latency_ms,
               LLMRequestLog.input_tokens, LLMRequestLog.output_tokens, LLMRequestLog.estimated_cost,
               LLMRequestLog.cost_source, LLMRequestLog.retry_count, LLMRequestLog.structured_output_valid,
               LLMRequestLog.structured_output_retry_count, LLMRequestLog.validation_failure_reason,
               LLMRequestLog.schema_enforcement, LLMRequestLog.error_type, LLMRequestLog.provider,
               LLMRequestLog.model, LLMRequestLog.requested_model, LLMRequestLog.configured_model,
               LLMRequestLog.model_mode, LLMRequestLog.model_group, LLMRequestLog.routing_tier,
               LLMRequestLog.routing_cause, LLMRequestLog.fallback_used, LLMRequestLog.escalated,
               LLMRequestLog.escalation_reason, LLMRequestLog.confidence, LLMRequestLog.config_version,
               LLMRequestLog.context_utilization_percent, LLMRequestLog.workflow_type,
               LLMRequestLog.workflow_entity_id, LLMRequestLog.trace_id, LLMRequestLog.prompt_name,
               LLMRequestLog.prompt_version)
# Model health on a node and the detail behind it read the same raw rows, with the columns Monitoring's
# performance view needs (app.services.model_performance), each column once.
LLM_COLUMNS = tuple({c.key: c for c in (*LLM_COLUMNS, *model_performance.COLUMNS)}.values())


def llm_rows(db: Session, scope: Scope, stages: Iterable[str] | None = None, *, start: datetime | None = None,
             end: datetime | None = None, limit: int = LLM_ROW_LIMIT) -> list[Any]:
    query = select(*LLM_COLUMNS).where(LLMRequestLog.created_at >= (start or scope.start),
                                       LLMRequestLog.created_at <= (end or scope.end))
    if scope.user_id is not None:
        query = query.where(LLMRequestLog.user_id == scope.user_id)
    if stages is not None:
        query = query.where(LLMRequestLog.stage.in_(list(stages)))
    return db.execute(query.order_by(LLMRequestLog.created_at.desc(), LLMRequestLog.id.desc()).limit(limit)).all()


def external_rows(db: Session, scope: Scope, matches: Iterable[topo.ExternalMatch] | None = None,
                  limit: int = EXTERNAL_ROW_LIMIT) -> list[Any]:
    row = ExternalAPIRequestLog
    query = select(*external_monitoring.ROW_COLUMNS).where(row.created_at >= scope.start, row.created_at <= scope.end)
    if scope.user_id is not None:
        query = query.where(row.user_id == scope.user_id)
    if matches is not None:
        conditions = [and_(row.provider == m.provider, row.operation.like(f"{m.operation_prefix}%"))
                      if m.operation_prefix else row.provider == m.provider for m in matches]
        if not conditions:
            return []
        query = query.where(or_(*conditions))
    return db.execute(query.order_by(row.created_at.desc()).limit(limit)).all()


def _matches(node: topo.Node, r: Any) -> bool:
    return any(r.provider == m.provider and (not m.operation_prefix or (r.operation or "").startswith(m.operation_prefix))
               for m in node.external)


def llm_metrics(items: list[Any]) -> dict[str, Any]:
    """Transparent dimensions, never a single score: reliability, latency, tokens, cost, validation, routing."""
    total = len(items)
    failed = sum(r.status == "FAILED" for r in items)
    latencies = [r.latency_ms for r in items if r.latency_ms is not None]
    checked = [r for r in items if r.structured_output_valid is not None]
    routed = [r for r in items if r.fallback_used is not None]
    first = [r for r in items if not r.escalated]
    costs = [float(r.estimated_cost) for r in items if r.estimated_cost is not None]
    inputs = [r.input_tokens for r in items if r.input_tokens is not None]
    outputs = [r.output_tokens for r in items if r.output_tokens is not None]
    utilization = [r.context_utilization_percent for r in items if r.context_utilization_percent is not None]
    perf = model_performance.block(items, 3600)
    return {
        "requests": total, "succeeded": total - failed, "failed": failed,
        "success_rate": _rate(total - failed, total), "failure_rate": _rate(failed, total),
        "avg_latency_ms": _mean(latencies), "p50_latency_ms": percentile(latencies, 50),
        "p95_latency_ms": percentile(latencies, 95), "p99_latency_ms": percentile(latencies, 99),
        "avg_input_tokens": _mean(inputs), "avg_output_tokens": _mean(outputs),
        "input_tokens": sum(inputs), "output_tokens": sum(outputs),
        "requests_without_usage": total - len(inputs),
        "avg_context_utilization_percent": _mean(utilization),
        # NULL cost means unknown, not free: the total covers priced requests and says how many were not.
        "estimated_cost": round(sum(costs), 6) if costs else None, "unpriced_requests": total - len(costs),
        "free": bool(items) and all(r.cost_source == "free" for r in items),
        "retried_requests": sum((r.retry_count or 0) > 0 for r in items),
        "retry_rate": _rate(sum((r.retry_count or 0) > 0 for r in items), total),
        "timeouts": sum(r.error_type == "TIMEOUT" for r in items),
        "timeout_rate": _rate(sum(r.error_type == "TIMEOUT" for r in items), total),
        "schema_checked": len(checked),
        "schema_failures": sum(r.structured_output_valid is False for r in checked),
        "schema_failure_rate": _rate(sum(r.structured_output_valid is False for r in checked), len(checked)),
        "schema_pass_rate": _rate(sum(r.structured_output_valid is True for r in checked), len(checked)),
        "fallbacks": sum(r.fallback_used is True for r in routed),
        "fallback_rate": _rate(sum(r.fallback_used is True for r in routed), len(routed)),
        "escalations": sum(bool(r.escalated) for r in items),
        "escalation_rate": _rate(sum(bool(r.escalated) for r in items), len(first)),
        "failovers": perf["reliability"]["failover_requests"], "failover_rate": perf["reliability"]["failover_rate"],
        "model_mismatch_rate": perf["reliability"]["model_mismatch_rate"],
        "model_mismatch_samples": perf["reliability"]["model_mismatch_samples"],
        "retrieval_p50_ms": perf["latency"]["retrieval_p50_ms"], "retrieval_p95_ms": perf["latency"]["retrieval_p95_ms"],
        "retrieval_samples": perf["latency"]["retrieval_samples"],
        # Failed attempts included, per task that got done; None when no task's request was priced.
        "cost_per_successful_task": perf["cost"]["per_successful_task"],
        "low_sample": total < LOW_SAMPLE,
    }


def _consecutive_failures(items: list[Any], status_of=lambda r: r.status == "FAILED") -> int:
    """Failures in a row, counting back from the most recent item (items are newest first)."""
    n = 0
    for r in items:
        if not status_of(r):
            break
        n += 1
    return n


# ───────────── task tables: queues and throughput ─────────────

@dataclass(frozen=True)
class TaskTable:
    node_id: str
    model: Any
    pending: tuple[str, ...]
    running: tuple[str, ...]
    failed: tuple[str, ...]
    blocked: tuple[str, ...]
    completed: tuple[str, ...]
    attempts: Any
    started: Any
    finished: Any


TASK_TABLES = (
    # First: a job the graph waits on is placed at the worker doing its step, which the later tables override with.
    TaskTable("qualification", JobQualificationTask, ("PENDING",), ("PROCESSING",), ("FAILED",), (), ("COMPLETED",),
              JobQualificationTask.attempt_count, JobQualificationTask.started_at,
              JobQualificationTask.completed_at),
    TaskTable("preparation", JobPreparationTask, ("PENDING",), ("PROCESSING",), ("FAILED",), (), ("COMPLETED",),
              JobPreparationTask.attempt_count, JobPreparationTask.started_at, JobPreparationTask.completed_at),
    TaskTable("application_execution", ApplicationExecutionTask, ("PENDING",), ("PROCESSING",), ("FAILED",), (),
              ("COMPLETED",), ApplicationExecutionTask.attempt_count, ApplicationExecutionTask.started_at,
              ApplicationExecutionTask.completed_at),
    TaskTable("domain_classifier", JobClassificationTask, ("PENDING",), ("PROCESSING",), ("FAILED",), (), ("COMPLETED",),
              JobClassificationTask.attempt_count, JobClassificationTask.started_at, JobClassificationTask.completed_at),
    TaskTable("jd_recovery", JobDescriptionFetchTask, ("PENDING",), ("PROCESSING",), ("FAILED",), ("BLOCKED",),
              ("COMPLETED",), JobDescriptionFetchTask.attempt_count, JobDescriptionFetchTask.started_at,
              JobDescriptionFetchTask.completed_at),
    TaskTable("jd_parser", JobDescriptionParseTask, ("PENDING",), ("PROCESSING",), ("FAILED",), (), ("COMPLETED",),
              JobDescriptionParseTask.attempt_count, JobDescriptionParseTask.started_at,
              JobDescriptionParseTask.completed_at),
    TaskTable("company_insights", JobInsightTask, ("PENDING",), ("PROCESSING",), ("FAILED",), (),
              ("COMPLETED", "PARTIAL"), JobInsightTask.attempt_count, JobInsightTask.started_at,
              JobInsightTask.completed_at),
    TaskTable("ranker", JobRankingTask, ("PENDING",), ("PROCESSING",), ("FAILED",), (), ("COMPLETED",),
              JobRankingTask.attempt_count, JobRankingTask.started_at, JobRankingTask.completed_at),
)
TASK_BY_NODE = {t.node_id: t for t in TASK_TABLES}
# The Running workers kind (app.services.worker_settings) behind each task table, for its attempt limit.
WORKER_KIND_BY_NODE = {"qualification": "qualification", "preparation": "preparation",
                       "application_execution": "application", "domain_classifier": "classification", "jd_recovery": "recovery", "jd_parser": "parsing", "company_insights": "insights",
                       "ranker": "ranking"}


def _blank_live() -> dict[str, int]:
    return {"queued": 0, "running": 0, "retrying": 0, "waiting": 0, "watching": 0, "failed_recent": 0,
            "blocked_recent": 0}


def live_counts(db: Session, user_id: int | None, now: datetime | None = None) -> dict[str, dict[str, int]]:
    """How many items sit at each node right now. Grouped counts only: cheap enough to poll."""
    now = now or utcnow()
    recent = now - timedelta(hours=POLICY.window_hours)
    counts: dict[str, dict[str, int]] = {n.id: _blank_live() for n in topo.NODES if n.implemented}
    for t in TASK_TABLES:
        m = t.model
        status = m.status
        retry = and_(status.in_(t.pending), t.attempts > 0)
        row = db.execute(_owned(select(
            func.sum(case((and_(status.in_(t.pending), t.attempts == 0), 1), else_=0)),
            func.sum(case((retry, 1), else_=0)),
            func.sum(case((status.in_(t.running), 1), else_=0)),
            func.sum(case((and_(status.in_(t.failed), t.finished >= recent), 1), else_=0)),
            func.sum(case((and_(status.in_(t.blocked or ("__none__",)), t.finished >= recent), 1), else_=0)),
        ).where(or_(status.in_(t.pending + t.running), t.finished >= recent)), m.user_id, user_id)).first()
        c = counts[t.node_id]
        c["queued"], c["retrying"], c["running"], c["failed_recent"], c["blocked_recent"] = (int(v or 0) for v in row)
    # The retrieval indexer's entries belong to no job: counted at its node, never placed as a job's packet.
    e = RagIndexEntry
    waiting = e.status == "PENDING"
    row = db.execute(_owned(select(
        func.sum(case((and_(waiting, e.attempt_count == 0), 1), else_=0)),
        func.sum(case((and_(waiting, e.attempt_count > 0), 1), else_=0)),
        func.sum(case((e.status == "PROCESSING", 1), else_=0)),
        func.sum(case((and_(e.status == "FAILED", e.completed_at >= recent), 1), else_=0)),
    ).where(or_(e.status.in_(("PENDING", "PROCESSING")), e.completed_at >= recent),
            e.index_version_id.in_(select(RagIndexVersion.id).where(RagIndexVersion.status != "RETIRED"))),
        e.user_id, user_id)).first()
    if "retrieval_index" in counts:
        c = counts["retrieval_index"]
        c["queued"], c["retrying"], c["running"], c["failed_recent"] = (int(v or 0) for v in row)
    # Tailoring runs one task through tailor → review; the task's stage says which worker holds it.
    rows = db.execute(_owned(select(TailorTask.status, TailorTask.stage, TailorTask.attempts, func.count())
                             .where(or_(TailorTask.status.in_(("queued", "running")),
                                        and_(TailorTask.status == "failed", TailorTask.finished_at >= recent)))
                             .group_by(TailorTask.status, TailorTask.stage, TailorTask.attempts),
                             TailorTask.user_id, user_id)).all()
    for status, stage, attempts, n in rows:
        if status == "failed":  # a finished run keeps the stage it ended in
            counts["reviewer" if stage == "reviewing" else "tailor"]["failed_recent"] += n
        elif status == "queued":
            counts["tailor"]["retrying" if (attempts or 0) > 0 else "queued"] += n
        else:
            counts[{"ranking": "ranker", "reviewing": "reviewer"}.get(stage, "tailor")]["running"] += n
    counts["domain_filter"]["waiting"] = db.scalar(_owned(select(func.count(Job.id)).where(
        Job.state == "DISCOVERED", or_(Job.domain_match.is_(None), Job.domain_match != "NOT_MATCHING")),
        Job.user_id, user_id)) or 0
    counts["approval"]["waiting"] = db.scalar(_owned(select(func.count(Job.id)).where(
        Job.status.in_(("tailored", "pending_approval"))), Job.user_id, user_id)) or 0
    app_rows = db.execute(_owned(select(Application.status, Application.stage, func.count()).where(
        Application.status.in_(("approved", "applied"))).group_by(Application.status, Application.stage),
        Application.user_id, user_id)).all()
    for status, stage, n in app_rows:
        stage = stage or "applied"
        if status == "approved":
            counts["application"]["waiting"] += n
        elif stage in INTERVIEW_STAGES:
            counts["interview"]["waiting"] += n
        elif stage not in OFFER_STAGES + CLOSED_STAGES and stage != "onhold":
            counts["application_inbox"]["watching"] += n
    counts["follow_up"]["waiting"] = db.scalar(_owned(select(func.count(FollowUp.id)).where(
        FollowUp.status == "draft", FollowUp.due_at <= now), FollowUp.user_id, user_id)) or 0
    counts["discovery"]["running"] = db.scalar(_owned(select(func.count(SearchRun.id)).where(
        SearchRun.completed_at.is_(None), SearchRun.status.in_(("READY", "RUNNING")),
        SearchRun.created_at >= now - timedelta(days=2)), SearchRun.user_id, user_id)) or 0
    return counts


def task_metrics(db: Session, scope: Scope, table: TaskTable) -> dict[str, Any]:
    """Throughput of one async worker's task table in the range: processed, outcomes and run time."""
    m = table.model
    rows = db.execute(_owned(select(m.status, table.started, table.finished, table.attempts, m.created_at)
                             .where(m.created_at >= scope.start, m.created_at <= scope.end)
                             .order_by(m.created_at.desc()).limit(EDGE_ROW_LIMIT), m.user_id, scope.user_id)).all()
    durations = [s for s in (_seconds(r[1], r[2]) for r in rows) if s is not None]
    finished = [r for r in rows if r[0] in table.completed + table.failed + table.blocked]
    failed = sum(r[0] in table.failed for r in rows)
    return {"tasks": len(rows), "completed": sum(r[0] in table.completed for r in rows), "failed": failed,
            "blocked": sum(r[0] in table.blocked for r in rows), "skipped": sum(r[0] == "SKIPPED" for r in rows),
            "finished": len(finished), "failure_rate": _rate(failed, len(finished)),
            "retried": sum((r[3] or 0) > 1 for r in rows), "retry_rate": _rate(sum((r[3] or 0) > 1 for r in rows),
                                                                                len(rows)),
            "median_run_seconds": percentile(durations, 50), "p95_run_seconds": percentile(durations, 95),
            "truncated": len(rows) >= EDGE_ROW_LIMIT}


def _recent_task_outcomes(db: Session, table: TaskTable, user_id: int | None, since: datetime) -> list[str]:
    m = table.model
    return [r[0] for r in db.execute(_owned(select(m.status).where(
        table.finished >= since, m.status.in_(table.completed + table.failed + table.blocked))
        .order_by(table.finished.desc()).limit(50), m.user_id, user_id)).all()]


def _tailor_metrics(db: Session, scope: Scope) -> dict[str, Any]:
    rows = db.execute(_owned(select(TailorTask.status, TailorTask.started_at, TailorTask.finished_at,
                                    TailorTask.attempts)
                             .where(TailorTask.created_at >= scope.start, TailorTask.created_at <= scope.end)
                             .limit(EDGE_ROW_LIMIT), TailorTask.user_id, scope.user_id)).all()
    durations = [s for s in (_seconds(r[1], r[2]) for r in rows) if s is not None]
    finished = [r for r in rows if r[0] in ("succeeded", "failed")]
    failed = sum(r[0] == "failed" for r in rows)
    return {"tasks": len(rows), "completed": sum(r[0] == "succeeded" for r in rows), "failed": failed,
            "cancelled": sum(r[0] == "cancelled" for r in rows), "finished": len(finished),
            "failure_rate": _rate(failed, len(finished)), "retried": sum((r[3] or 0) > 1 for r in rows),
            "retry_rate": _rate(sum((r[3] or 0) > 1 for r in rows), len(rows)),
            "median_run_seconds": percentile(durations, 50), "p95_run_seconds": percentile(durations, 95),
            "truncated": len(rows) >= EDGE_ROW_LIMIT}


# ───────────── node status ─────────────

def node_status(node: topo.Node, *, live: dict[str, int], recent_llm: list[Any], recent_tasks: list[str],
                failed_task_statuses: tuple[str, ...], range_activity: int, worker_assignments: list[dict[str, Any]],
                model_health: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    """(status, reasons). See HealthPolicy for the rules, which this applies in order."""
    p = POLICY
    ai_first = node.primary_type == "AI"
    if ai_first and worker_assignments and not any(a.get("enabled", True) for a in worker_assignments):
        return "DISABLED", ["Every worker of this stage is switched off in the active AI configuration"]
    llm_failed_run = _consecutive_failures(recent_llm)
    task_failed_run = _consecutive_failures(recent_tasks, lambda s: s in failed_task_statuses)
    if llm_failed_run >= p.error_consecutive_failures:
        return "ERROR", [f"The last {llm_failed_run} model calls in the past {p.window_hours}h failed"]
    if task_failed_run >= p.error_consecutive_failures:
        return "ERROR", [f"The last {task_failed_run} tasks in the past {p.window_hours}h failed"]
    reasons = []
    if len(recent_llm) >= p.min_samples:
        rate = sum(r.status == "FAILED" for r in recent_llm) / len(recent_llm)
        if rate >= p.degraded_failure_rate:
            reasons.append(f"{rate:.0%} of {len(recent_llm)} model calls failed in the past {p.window_hours}h")
        checked = [r for r in recent_llm if r.structured_output_valid is not None]
        if len(checked) >= p.min_samples:
            schema = sum(r.structured_output_valid is False for r in checked) / len(checked)
            if schema >= p.degraded_schema_failure_rate:
                reasons.append(f"{schema:.0%} of answers failed their output schema in the past {p.window_hours}h")
    if len(recent_tasks) >= p.min_samples:
        rate = sum(s in failed_task_statuses for s in recent_tasks) / len(recent_tasks)
        if rate >= p.degraded_failure_rate:
            reasons.append(f"{rate:.0%} of {len(recent_tasks)} finished tasks failed in the past {p.window_hours}h")
    for a in worker_assignments:
        if a.get("enabled", True) and a.get("has_prompt") and a.get("mode") != "DIRECT" \
                and not a.get("executed_by") and not a.get("resolved_model"):
            reasons.append(f"No model can be resolved for {a['label']}" + (f" ({a['note']})" if a.get("note") else ""))
        health = model_health.get(a.get("resolved_model") or "")
        if health and health["health_status"] == "UNHEALTHY":
            reasons.append(f"{a['resolved_model']} failed its last health check")
    if reasons:
        return "DEGRADED", reasons
    if live.get("running") or live.get("queued") or live.get("retrying"):
        return "BUSY", [f"{live.get('running', 0)} running, {live.get('queued', 0) + live.get('retrying', 0)} queued"]
    if range_activity or any(live.get(k) for k in ("waiting", "watching")):
        return "HEALTHY", []
    return "NO_DATA", ["Nothing recorded in this range"]


# ───────────── the graph ─────────────

def _recommendations(db: Session, user_id: int) -> list[dict[str, Any]]:
    report = db.scalar(_owned(select(AIOptimizationReport), AIOptimizationReport.user_id, user_id)
                       .order_by(AIOptimizationReport.created_at.desc(), AIOptimizationReport.id.desc()).limit(1))
    if report is None:
        return []
    out = []
    for item in report.recommendations or []:
        if isinstance(item, dict):
            out.append({k: item.get(k) for k in ("title", "category", "priority", "affected_stage", "finding",
                                                 "recommended_change", "expected_benefit", "risk")}
                       | {"report_id": report.id, "report_created_at": _iso(report.created_at)})
    return out


def _node_recommendations(node: topo.Node, recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in recs if r.get("affected_stage") in node.llm_stages]


def graph(db: Session, scope: Scope) -> dict[str, Any]:
    """Topology plus the runtime summary each node and edge shows. Cached for a few seconds per scope."""
    # A preset ends "now", so it is keyed by its start minute; an explicit window by its exact start and end, so two
    # different windows never share a cached graph.
    window = (_iso(scope.start), _iso(scope.end)) if scope.range_name == "custom" \
        else (scope.start.replace(second=0, microsecond=0),)
    key = ("graph", scope.viewer_id, scope.user_id, scope.range_name, *window)
    return _cached(key, GRAPH_CACHE_SECONDS, lambda: _build_graph(db, scope))


def _build_graph(db: Session, scope: Scope) -> dict[str, Any]:
    now = utcnow()
    recent_since = now - timedelta(hours=POLICY.window_hours)
    config = assignments(db, scope.viewer_id)
    model_health = _model_health(db, scope.viewer_id)
    recs = _recommendations(db, scope.viewer_id)
    live = live_counts(db, scope.user_id, now)
    rows = llm_rows(db, scope)
    recent_scope = Scope(scope.viewer_id, scope.user_id, recent_since, now)
    recent_rows = llm_rows(db, recent_scope, limit=2000)
    ext = external_rows(db, scope, [m for n in topo.NODES for m in n.external])
    by_stage: dict[str, list[Any]] = defaultdict(list)
    for r in rows:
        by_stage[r.stage].append(r)
    recent_by_stage: dict[str, list[Any]] = defaultdict(list)
    for r in recent_rows:
        recent_by_stage[r.stage].append(r)
    # Edges first: a deterministic stage's only trace may be the items it passed on.
    edges = {e.id: _edge_summary(db, scope, e) for e in topo.EDGES if e.measure}
    passed_on: Counter = Counter()
    for e in topo.EDGES:
        if e.id in edges:
            passed_on[e.source] += edges[e.id]["count"]
            passed_on[e.target] += edges[e.id]["count"]
    nodes: dict[str, Any] = {}
    window = model_performance.window_seconds(scope.start, scope.end, now)
    for node in topo.NODES:
        if not node.implemented:
            continue
        node_rows = [r for s in node.llm_stages for r in by_stage.get(s, [])]
        node_recent = sorted((r for s in node.llm_stages for r in recent_by_stage.get(s, [])),
                             key=lambda r: (r.created_at, r.id), reverse=True)
        node_ext = [r for r in ext if _matches(node, r)]
        worker_rows = [config["workers"][w] for w in node.workers if w in config["workers"]]
        table = TASK_BY_NODE.get(node.id)
        tasks = task_metrics(db, scope, table) if table else (_tailor_metrics(db, scope) if node.id == "tailor"
                                                              else None)
        recent_tasks = _recent_task_outcomes(db, table, scope.user_id, recent_since) if table else []
        activity = len(node_rows) + len(node_ext) + (tasks["tasks"] if tasks else 0) + passed_on[node.id]
        status, reasons = node_status(
            node, live=live.get(node.id, {}), recent_llm=node_recent, recent_tasks=recent_tasks,
            failed_task_statuses=(table.failed + table.blocked) if table else (), range_activity=activity,
            worker_assignments=worker_rows, model_health=model_health)
        llm = llm_metrics(node_rows) if node.is_ai else None
        nodes[node.id] = {
            "model_health": model_performance.compact(node_rows, window) if node.is_ai else None,
            "status": status, "status_reasons": reasons, "live": live.get(node.id, _blank_live()),
            "assignments": worker_rows, "llm": llm,
            "observed_models": [m for m, _ in Counter(r.model for r in node_rows if r.model).most_common(3)],
            "external": external_monitoring.health(node_ext) if node.external else None,
            "tasks": tasks, "recommendations": len(_node_recommendations(node, recs)),
            "playwright_used": _playwright_count(db, scope) if node.id == "jd_recovery" else None,
        }
    statuses = Counter(n["status"] for n in nodes.values())
    return {"generated_at": _iso(now), "scope": scope.public(), "topology": topo.public(),
            "gateway": config["gateway"], "nodes": nodes, "edges": edges,
            "system": {"statuses": dict(statuses),
                       "status": "ERROR" if statuses.get("ERROR") else "DEGRADED" if statuses.get("DEGRADED")
                       else "BUSY" if statuses.get("BUSY") else "HEALTHY" if statuses.get("HEALTHY") else "NO_DATA",
                       "llm_rows_truncated": len(rows) >= LLM_ROW_LIMIT},
            "health_policy": {**asdict(POLICY), "rules": health_rules()},
            "telemetry_retention_days": get_settings().llm_telemetry_retention_days}


def _playwright_count(db: Session, scope: Scope) -> int:
    return db.scalar(_owned(select(func.count(JobDescriptionFetchTask.id)).where(
        JobDescriptionFetchTask.playwright_used.is_(True), JobDescriptionFetchTask.created_at >= scope.start,
        JobDescriptionFetchTask.created_at <= scope.end), JobDescriptionFetchTask.user_id, scope.user_id)) or 0


# ───────────── edges: transitions measured from stored facts ─────────────
# Each fact returns rows of (job_id, at, start, end): `at` places the transition in the range; `start`/`end` give a
# per-item duration only when both are stored on the same row (Edge.timed). Nothing is interpolated.

def _fact_rows(db: Session, scope: Scope, edge: topo.Edge) -> list[tuple[int | None, datetime, datetime | None,
                                                                         datetime | None]]:
    s, e, u = scope.start, scope.end, scope.user_id
    lim = EDGE_ROW_LIMIT
    key = edge.id
    if key == "discovery__domain_filter":
        q = select(Job.id, Job.discovered_at, Job.discovered_at, Job.domain_checked_at).where(
            Job.discovered_at >= s, Job.discovered_at <= e)
        return [tuple(r) for r in db.execute(_owned(q, Job.user_id, u).limit(lim)).all()]
    if key in ("domain_filter__domain_classifier", "domain_classifier__resume_match"):
        t = JobClassificationTask
        q = select(t.job_id, t.created_at, t.created_at, t.completed_at).where(t.created_at >= s, t.created_at <= e,
                                                                               t.route == "ASK", t.mode == "ON")
        if key == "domain_classifier__resume_match":
            q = q.where(t.classification.in_(("MATCH", "UNCERTAIN")))
        return [(r[0], r[1], None, None) for r in db.execute(_owned(q, t.user_id, u).limit(lim)).all()]
    if key == "domain_filter__resume_match":
        q = select(Job.id, Job.domain_checked_at, Job.domain_checked_at, Job.domain_checked_at).where(
            Job.domain_checked_at >= s, Job.domain_checked_at <= e, Job.domain_match.in_(("MATCH", "KEPT")))
        return [(r[0], r[1], None, None) for r in db.execute(_owned(q, Job.user_id, u).limit(lim)).all()]
    if key == "resume_match__jd_recovery":
        t = JobDescriptionFetchTask
        q = select(t.job_id, t.created_at, t.created_at, t.completed_at).where(t.created_at >= s, t.created_at <= e)
        return [tuple(r) for r in db.execute(_owned(q, t.user_id, u).limit(lim)).all()]
    if key == "resume_match__jd_parser":
        t, f = JobDescriptionParseTask, JobDescriptionFetchTask
        q = select(t.job_id, t.created_at).where(t.created_at >= s, t.created_at <= e,
                                                  ~exists(select(f.id).where(f.job_id == t.job_id)))
        return [(r[0], r[1], None, None) for r in db.execute(_owned(q, t.user_id, u).limit(lim)).all()]
    if key == "jd_recovery__jd_parser":
        t = JobDescriptionFetchTask
        q = select(t.job_id, t.completed_at).where(t.completed_at >= s, t.completed_at <= e,
                                                    t.result_code == "RECOVERED")
        return [(r[0], r[1], None, None) for r in db.execute(_owned(q, t.user_id, u).limit(lim)).all()]
    if key == "jd_parser__ranker":
        t = JobDescriptionParseTask
        q = select(t.job_id, t.completed_at).where(t.completed_at >= s, t.completed_at <= e, t.status == "COMPLETED")
        return [(r[0], r[1], None, None) for r in db.execute(_owned(q, t.user_id, u).limit(lim)).all()]
    if key in ("ranker__tailor", "ranker__archived"):
        w = WorkflowTransition
        target, sources = ("QUALIFIED", ("RANKED",)) if key == "ranker__tailor" else ("ARCHIVED", ("RANKED", "QUALIFIED"))
        q = select(w.job_id, w.created_at).where(w.created_at >= s, w.created_at <= e, w.to_state == target,
                                                  w.from_state.in_(sources))
        return [(r[0], r[1], None, None) for r in db.execute(_owned(q, w.user_id, u).limit(lim)).all()]
    if key in ("tailor__reviewer", "reviewer__approval", "reviewer__tailor"):
        v = ResumeVariant
        q = select(v.job_id, v.created_at, v.reviewer_score, v.reviewer_notes).where(v.created_at >= s,
                                                                                      v.created_at <= e)
        rows = db.execute(_owned(q, v.user_id, u).limit(lim)).all()
        if key == "tailor__reviewer":
            return [(r[0], r[1], None, None) for r in rows]
        if key == "reviewer__approval":
            return [(r[0], r[1], None, None) for r in rows if r[2] is not None
                    and (r[3] or {}).get("decision") != "RETAILOR"]
        return [(r[0], r[1], None, None) for r in rows if (r[3] or {}).get("decision") == "RETAILOR"]
    if key == "approval__application":
        a = Application
        q = select(a.job_id, a.approved_at, a.approved_at, a.applied_at).where(a.approved_at >= s, a.approved_at <= e)
        return [tuple(r) for r in db.execute(_owned(q, a.user_id, u).limit(lim)).all()]
    if key == "approval__company_insights":
        t = JobInsightTask
        q = select(t.job_id, t.created_at, t.created_at, t.completed_at).where(t.created_at >= s, t.created_at <= e)
        return [tuple(r) for r in db.execute(_owned(q, t.user_id, u).limit(lim)).all()]
    if key == "approval__application_execution":
        t = ApplicationExecutionTask
        q = select(t.job_id, t.created_at, t.created_at, t.completed_at).where(t.created_at >= s, t.created_at <= e)
        return [tuple(r) for r in db.execute(_owned(q, t.user_id, u).limit(lim)).all()]
    if key == "application_execution__application":
        t = ApplicationExecutionTask
        q = select(t.job_id, t.completed_at).where(t.completed_at >= s, t.completed_at <= e,
                                                    t.outcome == "READY_TO_SUBMIT")
        return [(r[0], r[1], None, None) for r in db.execute(_owned(q, t.user_id, u).limit(lim)).all()]
    if key == "application_execution__submission_confirmation":
        t = SubmissionIntent
        q = select(t.job_id, t.clicking_at, t.clicking_at, t.resolved_at).where(t.clicking_at >= s,
                                                                                 t.clicking_at <= e)
        return [tuple(r) for r in db.execute(_owned(q, t.user_id, u).limit(lim)).all()]
    if key == "submission_confirmation__application":
        t = SubmissionIntent
        q = select(t.job_id, t.resolved_at).where(t.resolved_at >= s, t.resolved_at <= e, t.status == "CONFIRMED")
        return [(r[0], r[1], None, None) for r in db.execute(_owned(q, t.user_id, u).limit(lim)).all()]
    if key == "application_inbox__inbox_review":
        m, c = ApplicationEmailMessage, ApplicationCommunicationEvent
        q = select(m.application_id, m.received_at).where(m.match_status == "NEEDS_REVIEW", m.received_at >= s,
                                                           m.received_at <= e)
        rows = [(None, r[1], None, None) for r in db.execute(_owned(q, m.user_id, u).limit(lim)).all()]
        q = select(Application.job_id, c.received_at).join(Application, Application.id == c.application_id).where(
            c.needs_review.is_(True), c.received_at >= s, c.received_at <= e)
        return rows + [(r[0], r[1], None, None) for r in db.execute(_owned(q, c.user_id, u).limit(lim)).all()]
    if key == "company_insights__interview_insights":
        t = JobInsightTask
        q = select(t.job_id, t.completed_at).where(t.completed_at >= s, t.completed_at <= e,
                                                    t.status.in_(("COMPLETED", "PARTIAL")))
        return [(r[0], r[1], None, None) for r in db.execute(_owned(q, t.user_id, u).limit(lim)).all()]
    if key == "application__application_inbox":
        a = Application
        q = select(a.job_id, a.applied_at).where(a.applied_at >= s, a.applied_at <= e)
        return [(r[0], r[1], None, None) for r in db.execute(_owned(q, a.user_id, u).limit(lim)).all()]
    if key == "application_inbox__follow_up":
        q = select(Application.job_id, FollowUp.created_at).join(Application, Application.id == FollowUp.application_id)\
            .where(FollowUp.created_at >= s, FollowUp.created_at <= e)
        return [(r[0], r[1], None, None) for r in db.execute(_owned(q, FollowUp.user_id, u).limit(lim)).all()]
    stage_targets = {"application_inbox__interview": INTERVIEW_STAGES, "application_inbox__outcome": CLOSED_STAGES,
                     "interview__outcome": OFFER_STAGES}
    if key in stage_targets:
        ev = ApplicationEvent
        q = select(Application.job_id, ev.occurred_at).join(Application, Application.id == ev.application_id).where(
            ev.kind == "stage", ev.detail.in_(stage_targets[key]), ev.occurred_at >= s, ev.occurred_at <= e)
        return [(r[0], r[1], None, None) for r in db.execute(_owned(q, ev.user_id, u).limit(lim)).all()]
    return []


def _edge_summary(db: Session, scope: Scope, edge: topo.Edge) -> dict[str, Any]:
    rows = _fact_rows(db, scope, edge)
    durations = [d for d in (_seconds(r[2], r[3]) for r in rows) if d is not None] if edge.timed else []
    return {"count": len(rows), "truncated": len(rows) >= EDGE_ROW_LIMIT,
            "median_seconds": percentile(durations, 50) if durations else None,
            "last_at": _iso(max((r[1] for r in rows if r[1]), default=None))}


def edge_detail(db: Session, scope: Scope, source: str, target: str) -> dict[str, Any] | None:
    edge = topo.edge(source, target)
    if edge is None:
        return None
    out = {**edge.public(), "scope": scope.public()}
    if not edge.measure:
        return {**out, "count": None, "note": "A supporting relationship: nothing is recorded per item."}
    rows = _fact_rows(db, scope, edge)
    durations = [d for d in (_seconds(r[2], r[3]) for r in rows) if d is not None] if edge.timed else []
    out.update({"count": len(rows), "truncated": len(rows) >= EDGE_ROW_LIMIT,
                "jobs": len({r[0] for r in rows if r[0] is not None}),
                "last_at": _iso(max((r[1] for r in rows if r[1]), default=None)),
                "median_seconds": percentile(durations, 50), "p95_seconds": percentile(durations, 95),
                "timed_items": len(durations),
                "timing_note": None if edge.timed else "No per-item time is recorded for this transition"})
    # Branches out of the same node, so a split reads as shares ("71% qualified, 29% archived").
    siblings = [e for e in topo.EDGES if e.source == source and e.measure and e.kind != "support"]
    if len(siblings) > 1:
        counts = {e.target: (len(rows) if e.id == edge.id else len(_fact_rows(db, scope, e))) for e in siblings}
        whole = sum(counts.values())
        out["split"] = [{"target": t, "label": topo.EDGES_BY_ID[f"{source}__{t}"].label, "count": n,
                         "share": _rate(n, whole)} for t, n in counts.items()]
    # Failures and retries at the target, when it is an async worker with a task table.
    table = TASK_BY_NODE.get(target)
    if table:
        metrics = task_metrics(db, scope, table)
        out["target_tasks"] = {k: metrics[k] for k in ("tasks", "completed", "failed", "blocked", "retried")}
    # Recent items only for the signed-in account's own jobs: never another account's titles.
    if scope.user_id is not None:
        recent = sorted((r for r in rows if r[0] is not None and r[1]), key=lambda r: r[1], reverse=True)[:10]
        jobs = _job_labels(db, [r[0] for r in recent], scope.user_id)
        out["recent"] = [{"job_id": r[0], "at": _iso(r[1]), **jobs.get(r[0], {})} for r in recent]
    return out


def _job_labels(db: Session, job_ids: list[int], user_id: int) -> dict[int, dict[str, Any]]:
    if not job_ids:
        return {}
    rows = db.execute(select(Job.id, Job.title, Job.company).where(Job.id.in_(job_ids), Job.user_id == user_id)).all()
    return {r[0]: {"title": r[1], "company": r[2]} for r in rows}


# ───────────── node detail and model history ─────────────

def node_detail(db: Session, scope: Scope, node_id: str) -> dict[str, Any] | None:
    node = topo.node(node_id)
    if node is None or not node.implemented:
        return None
    summary = graph(db, scope)["nodes"][node_id]
    rows = llm_rows(db, scope, node.llm_stages) if node.llm_stages else []
    recs = _node_recommendations(node, _recommendations(db, scope.viewer_id))
    out = {"node": node.public(), "scope": scope.public(), **summary, "recommendations": recs,
           "routing": routing_detail(rows, summary["assignments"]) if node.is_ai else None,
           "performance": _performance(rows, scope) if node.is_ai else None,
           "recent": _recent_activity(db, scope, node, rows)}
    if node.external:
        ext = external_rows(db, scope, node.external)
        out["external_operations"] = [{"provider": p, "operation": o, **external_monitoring.health(g)}
                                      for (p, o), g in _group(ext, lambda r: (r.provider, r.operation)).items()][:12]
    if node.id == "jd_recovery":
        out["fetch_methods"] = dict(Counter(r[0] or "NONE" for r in db.execute(_owned(
            select(JobDescriptionFetchTask.fetch_method).where(JobDescriptionFetchTask.created_at >= scope.start,
                                                               JobDescriptionFetchTask.created_at <= scope.end)
            .limit(EDGE_ROW_LIMIT), JobDescriptionFetchTask.user_id, scope.user_id)).all()))
    if node.id == "reviewer":
        out["quality"] = _review_quality(db, scope)
    if node.id == "application_inbox":
        out["quality"] = _inbox_quality(db, scope)
    return out


def _group(items: Iterable[Any], key) -> dict[Any, list[Any]]:
    groups: dict[Any, list[Any]] = defaultdict(list)
    for item in items:
        groups[key(item)].append(item)
    return groups


def routing_detail(rows: list[Any], worker_assignments: list[dict[str, Any]]) -> dict[str, Any]:
    """How requests were actually routed: requested vs answered models, router tiers, fallback and escalation.

    Router tiers are counted only from requests that went to the auto router and recorded a tier; when none did,
    `tiers_recorded` is false and the UI says so instead of guessing."""
    auto = [r for r in rows if r.model_mode == "AUTO_ROUTER" and r.configured_model == "AUTO_ROUTER"]
    tiers = Counter(r.routing_tier for r in auto if r.routing_tier)
    primary = [r for r in rows if r.fallback_used is False]
    return {
        "modes": dict(Counter(r.model_mode or "UNRECORDED" for r in rows)),
        "configured": dict(Counter(r.configured_model for r in rows if r.configured_model).most_common(8)),
        "requested": dict(Counter(r.requested_model for r in rows if r.requested_model).most_common(8)),
        "actual": dict(Counter(r.model for r in rows if r.model).most_common(8)),
        "actual_unknown": sum(1 for r in rows if not r.model),
        "auto_router_requests": len(auto), "tiers_recorded": bool(tiers),
        "tiers": [{"tier": t, "requests": n, "share": _rate(n, sum(tiers.values()))} for t, n in tiers.most_common()],
        "routing_causes": dict(Counter(r.routing_cause for r in rows if r.routing_cause).most_common(8)),
        "primary_requests": len(primary), "fallback_requests": sum(r.fallback_used is True for r in rows),
        "fallback_unknown": sum(r.fallback_used is None for r in rows),
        "escalations": sum(bool(r.escalated) for r in rows),
        "escalation_reasons": dict(Counter(r.escalation_reason for r in rows if r.escalated and r.escalation_reason)),
        "config_versions": sorted({r.config_version for r in rows if r.config_version is not None}, reverse=True)[:10],
        "assignments": worker_assignments,
    }


def llm_card(r: Any) -> dict[str, Any]:
    """One LLM call as orchestration shows it: metadata, never the prompt or the answer."""
    return {"id": r.id, "at": _iso(r.created_at), "stage": r.stage, "worker_type": r.worker_type,
            "status": r.status, "error_type": r.error_type, "provider": r.provider, "model_mode": r.model_mode,
            "configured_model": r.configured_model, "requested_model": r.requested_model,
            "actual_model": r.model, "model_group": r.model_group, "routing_tier": r.routing_tier,
            "routing_cause": r.routing_cause, "fallback_used": r.fallback_used, "escalated": bool(r.escalated),
            "escalation_reason": r.escalation_reason, "latency_ms": r.latency_ms, "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens, "retry_count": r.retry_count,
            "schema_valid": r.structured_output_valid, "validation_failure_reason": r.validation_failure_reason,
            "config_version": r.config_version, "trace_id": r.trace_id, "prompt_version": r.prompt_version,
            "job_id": r.workflow_entity_id if r.workflow_type == "job" else None,
            "application_id": r.workflow_entity_id if r.workflow_type == "application" else None}


def _recent_activity(db: Session, scope: Scope, node: topo.Node, rows: list[Any]) -> dict[str, Any]:
    if scope.all_accounts:
        return _activity_aggregate(db, scope, node, rows)
    out: dict[str, Any] = {
        "successes": [llm_card(r) for r in rows if r.status == "SUCCEEDED"][:RECENT_LIMIT],
        "failures": [llm_card(r) for r in rows if r.status == "FAILED"][:RECENT_LIMIT],
        "retries": [llm_card(r) for r in rows if (r.retry_count or 0) > 0 or r.structured_output_retry_count
                    or r.escalated][:RECENT_LIMIT]}
    table = TASK_BY_NODE.get(node.id)
    if table:
        m = table.model
        failed = db.execute(_owned(select(m.id, m.job_id, m.status, m.error_category, m.last_error, m.attempt_count,
                                          table.finished)
                                   .where(m.status.in_(table.failed + table.blocked), table.finished >= scope.start)
                                   .order_by(table.finished.desc()).limit(RECENT_LIMIT), m.user_id, scope.user_id)).all()
        out["failed_tasks"] = [{"task_id": r[0], "job_id": None if scope.all_accounts else r[1], "status": r[2],
                                "error_category": r[3], "error": _safe_error(r[4]), "attempts": r[5],
                                "at": _iso(r[6])} for r in failed]
    return out


def _activity_aggregate(db: Session, scope: Scope, node: topo.Node, rows: list[Any]) -> dict[str, Any]:
    """The every-account view of a node's recent activity: counts and distributions only. No call, request, task or
    job of another account is ever listed, not even with its ids blanked."""
    out: dict[str, Any] = {
        "aggregate_only": True,
        "calls": {"succeeded": sum(r.status == "SUCCEEDED" for r in rows),
                  "failed": sum(r.status == "FAILED" for r in rows),
                  "retried": sum(bool((r.retry_count or 0) > 0 or r.structured_output_retry_count or r.escalated)
                                 for r in rows)},
        "failures_by_error": dict(Counter(r.error_type or "UNKNOWN" for r in rows if r.status == "FAILED")),
        "calls_by_model": dict(Counter(r.model or "unknown" for r in rows))}
    table = TASK_BY_NODE.get(node.id)
    if table:
        m = table.model
        counts = db.execute(_owned(select(m.error_category, func.count())
                                   .where(m.status.in_(table.failed + table.blocked), table.finished >= scope.start)
                                   .group_by(m.error_category), m.user_id, scope.user_id)).all()
        out["failed_tasks_by_category"] = {(c.value if c is not None and hasattr(c, "value") else c or "UNKNOWN"): n
                                           for c, n in counts}
    return out


def _review_quality(db: Session, scope: Scope) -> dict[str, Any]:
    """Reviewer outcomes the variants already store: decisions and scores. Not a model quality score."""
    rows = db.execute(_owned(select(ResumeVariant.reviewer_score, ResumeVariant.reviewer_notes).where(
        ResumeVariant.created_at >= scope.start, ResumeVariant.created_at <= scope.end,
        ResumeVariant.reviewer_score.is_not(None)).limit(EDGE_ROW_LIMIT), ResumeVariant.user_id, scope.user_id)).all()
    decisions = Counter((r[1] or {}).get("decision") or "UNRECORDED" for r in rows)
    scores = [float(r[0]) for r in rows if r[0] is not None]
    return {"reviewed": len(rows), "decisions": dict(decisions), "pass_rate": _rate(decisions.get("PASS", 0), len(rows)),
            "retailor_rate": _rate(decisions.get("RETAILOR", 0), len(rows)),
            "median_score": percentile(scores, 50), "low_sample": len(rows) < LOW_SAMPLE}


def _inbox_quality(db: Session, scope: Scope) -> dict[str, Any]:
    """How often a person corrected what the inbox worker concluded (stored per event)."""
    ev = ApplicationCommunicationEvent
    rows = db.execute(_owned(select(ev.classification_method, ev.user_corrected, ev.stage_decision).where(
        ev.created_at >= scope.start, ev.created_at <= scope.end).limit(EDGE_ROW_LIMIT), ev.user_id,
        scope.user_id)).all()
    return {"events": len(rows), "corrected": sum(bool(r[1]) for r in rows),
            "correction_rate": _rate(sum(bool(r[1]) for r in rows), len(rows)),
            "methods": dict(Counter(r[0] for r in rows)), "stage_decisions": dict(Counter(r[2] for r in rows)),
            "low_sample": len(rows) < LOW_SAMPLE}


def _assignment_at(snapshot: dict[str, Any], worker_type: str) -> dict[str, Any]:
    """The orchestration-relevant part of one worker's configuration in a version snapshot. Nothing else is read:
    snapshots also hold generation settings and (redacted) deployment parameters this view never needs."""
    model, configured, note = model_resolution.resolve_model(snapshot, worker_type)
    config = (snapshot.get("workers") or {}).get(worker_type) or {}
    return {"mode": config.get("mode"), "assignment": configured, "model": model,
            "fallback_model": config.get("fallback_model") or None, "enabled": config.get("enabled", True),
            "note": note}


def _performance(rows: list[Any], scope: Scope) -> dict[str, Any]:
    """Per actual model, per configuration version and over time: the same blocks Monitoring compares models with,
    for this stage's requests only. Raw rows in, aggregates out; nothing here calls a model or LiteLLM."""
    series = "hour" if (scope.end - scope.start) <= timedelta(days=2) else "day"
    return {"models": model_performance.compare(rows, "model", scope.start, scope.end),
            "config_versions": model_performance.compare(rows, "config_version", scope.start, scope.end)["groups"],
            "series": model_performance.compare(rows, series, scope.start, scope.end)["groups"],
            "series_by": series, "rows_truncated": len(rows) >= LLM_ROW_LIMIT}


def model_history(db: Session, scope: Scope, node_id: str) -> dict[str, Any] | None:
    """Per worker: when its assignment changed (from applied configuration versions) and how calls went while
    each assignment was active, plus a table by the model that actually answered. Observed, never causal."""
    node = topo.node(node_id)
    if node is None or not node.implemented:
        return None
    key = ("history", scope.viewer_id, scope.user_id, node_id, scope.range_name)
    return _cached(key, HISTORY_CACHE_SECONDS, lambda: _build_history(db, scope, node))


def _build_history(db: Session, scope: Scope, node: topo.Node) -> dict[str, Any]:
    versions = db.execute(select(AIConfigVersion.version_number, AIConfigVersion.created_at,
                                 AIConfigVersion.changed_by, AIConfigVersion.source,
                                 AIConfigVersion.change_summary, AIConfigVersion.configuration_snapshot)
                          .where(AIConfigVersion.user_id == scope.viewer_id)
                          .order_by(AIConfigVersion.version_number.desc()).limit(CONFIG_VERSION_LIMIT)).all()
    versions = list(reversed(versions))
    now = utcnow()
    retention = get_settings().llm_telemetry_retention_days
    since = now - timedelta(days=retention) if retention > 0 else datetime(1970, 1, 1)
    # Always the viewer's own calls: the periods come from the viewer's configuration versions, and another account's
    # calls binned into them would describe assignments that account never had.
    all_rows = llm_rows(db, Scope(scope.viewer_id, scope.viewer_id, since, now), node.llm_stages) \
        if node.llm_stages else []
    workers = []
    for worker_type in node.workers:
        periods: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []
        previous = None
        for v in versions:
            current = _assignment_at(v[5] or {}, worker_type)
            if previous is None or current != previous:
                if periods:
                    periods[-1]["ended_at"] = _iso(v[1])
                    periods[-1]["to_version"] = v[0] - 1
                periods.append({**current, "from_version": v[0], "to_version": None, "started_at": _iso(v[1]),
                                "ended_at": None, "changed_by": v[2], "source": v[3]})
                if previous is not None:
                    changes.append({"version": v[0], "at": _iso(v[1]), "changed_by": v[2], "source": v[3],
                                    "summary": v[4], "from": previous, "to": current})
            previous = current
        # Direct-provider rows carry no worker type; the prompt names the worker that sent them.
        mine = [r for r in all_rows if (r.worker_type or worker_for_prompt(r.prompt_name)) == worker_type] \
            if len(node.workers) > 1 else all_rows
        for p in periods:
            start = datetime.fromisoformat(p["started_at"])
            end = datetime.fromisoformat(p["ended_at"]) if p["ended_at"] else now
            group = [r for r in mine if start <= r.created_at < end]
            p["observed"] = {k: v for k, v in llm_metrics(group).items()
                             if k in ("requests", "success_rate", "failure_rate", "p95_latency_ms", "avg_input_tokens",
                                      "avg_output_tokens", "schema_failure_rate", "fallback_rate", "estimated_cost",
                                      "unpriced_requests", "low_sample")}
            p["observed_models"] = dict(Counter(r.model for r in group if r.model).most_common(4))
            p["current"] = p["ended_at"] is None
        by_model = []
        for model, group in _group(mine, lambda r: r.model or "unknown").items():
            stats = llm_metrics(group)
            by_model.append({"model": model, "first_seen": _iso(min(r.created_at for r in group)),
                             "last_seen": _iso(max(r.created_at for r in group)),
                             **{k: stats[k] for k in ("requests", "success_rate", "p50_latency_ms", "p95_latency_ms",
                                                      "avg_input_tokens", "avg_output_tokens", "schema_failure_rate",
                                                      "fallback_rate", "estimated_cost", "unpriced_requests",
                                                      "low_sample")}})
        by_model.sort(key=lambda m: m["last_seen"] or "", reverse=True)
        workers.append({"worker_type": worker_type, "label": worker_label(worker_type),
                        "periods": list(reversed(periods)), "changes": list(reversed(changes)),
                        "by_actual_model": by_model})
    gateway = gateway_state(db, scope.viewer_id)["mode"] == "litellm"
    return {"node_id": node.id, "scope": scope.public(), "workers": workers, "versions_considered": len(versions),
            "gateway_mode": gateway,
            "mode_note": None if gateway else "Direct provider mode: configuration versions do not choose this "
                                               "worker's model right now (Settings stage models do); the table by "
                                               "answering model is what telemetry recorded.",
            "telemetry_since": _iso(since) if retention > 0 else None,
            "note": "Observed metrics over the period each assignment was active. Sample sizes are shown; "
                    "a difference between periods is an observation, not proof that one model is better."}


# ───────────── jobs: where a packet is and how it got there ─────────────

PATH = ("discovery", "domain_filter", "domain_classifier", "resume_match", "jd_recovery", "jd_parser", "ranker", "archived", "tailor",
        "reviewer", "approval", "application", "application_inbox", "follow_up", "interview", "outcome")
# Shown only when the job went there.
BRANCH_NODES = ("archived", "follow_up", "interview", "outcome")


@dataclass
class JobFacts:
    """Everything stored about one job that orchestration reads. Loaded once per request, bounded."""

    job: Job
    fetch: list[JobDescriptionFetchTask]
    parse: list[JobDescriptionParseTask]
    ranking_tasks: list[JobRankingTask]
    rankings: list[JobRanking]
    tailor_tasks: list[TailorTask]
    variants: list[ResumeVariant]
    application: Application | None
    stage_events: list[ApplicationEvent]
    followups: list[FollowUp]
    inbox_events: int
    transitions: list[WorkflowTransition]
    llm: list[Any]
    limits: dict[str, int] = field(default_factory=dict)  # the account's own attempt limits per worker


def load_facts(db: Session, job: Job, user_id: int) -> JobFacts:
    def latest(model, order, limit=20):
        return list(db.scalars(select(model).where(model.job_id == job.id, model.user_id == user_id)
                               .order_by(order.desc()).limit(limit)).all())

    application = db.scalar(select(Application).where(Application.job_id == job.id, Application.user_id == user_id))
    stage_events, followups, inbox = [], [], 0
    if application is not None:
        stage_events = list(db.scalars(select(ApplicationEvent).where(ApplicationEvent.application_id == application.id,
                                                                      ApplicationEvent.user_id == user_id)
                                       .order_by(ApplicationEvent.occurred_at.desc()).limit(50)).all())
        followups = list(db.scalars(select(FollowUp).where(FollowUp.application_id == application.id,
                                                           FollowUp.user_id == user_id)
                                    .order_by(FollowUp.created_at.desc()).limit(20)).all())
        inbox = db.scalar(select(func.count(ApplicationCommunicationEvent.id)).where(
            ApplicationCommunicationEvent.application_id == application.id,
            ApplicationCommunicationEvent.user_id == user_id)) or 0
    llm_filter = and_(LLMRequestLog.workflow_type == "job", LLMRequestLog.workflow_entity_id == job.id)
    if application is not None:
        llm_filter = or_(llm_filter, and_(LLMRequestLog.workflow_type == "application",
                                          LLMRequestLog.workflow_entity_id == application.id))
    llm = db.execute(select(*LLM_COLUMNS).where(LLMRequestLog.user_id == user_id, llm_filter)
                     .order_by(LLMRequestLog.created_at.desc(), LLMRequestLog.id.desc())
                     .limit(JOB_TELEMETRY_LIMIT)).all()
    transitions = list(db.scalars(select(WorkflowTransition).where(WorkflowTransition.job_id == job.id,
                                                                   WorkflowTransition.user_id == user_id)
                                  .order_by(WorkflowTransition.id.desc()).limit(100)).all())
    return JobFacts(job=job, fetch=latest(JobDescriptionFetchTask, JobDescriptionFetchTask.id),
                    parse=latest(JobDescriptionParseTask, JobDescriptionParseTask.id),
                    ranking_tasks=latest(JobRankingTask, JobRankingTask.id),
                    rankings=latest(JobRanking, JobRanking.version, 5),
                    tailor_tasks=latest(TailorTask, TailorTask.id), variants=latest(ResumeVariant, ResumeVariant.version, 10),
                    application=application, stage_events=stage_events, followups=followups, inbox_events=inbox,
                    transitions=list(reversed(transitions)), llm=llm,
                    limits={kind: worker_settings.get(db, user_id, kind, "max_attempts")
                            for kind in ("parsing", "recovery", "ranking", "tailoring")})


@dataclass(frozen=True)
class Position:
    node_id: str | None
    activity: str | None
    since: datetime | None
    attempt: int | None = None
    max_attempts: int | None = None
    reason: str | None = None
    terminal: bool = False
    terminal_node: str | None = None


def _tailor_attempts(limits: dict[str, int] | None = None) -> int:
    from app.services.tailor_tasks import MAX_ATTEMPTS

    return (limits or {}).get("tailoring") or MAX_ATTEMPTS


def _active(items: list[Any], statuses: tuple[str, ...]) -> Any | None:
    return next((t for t in items if t.status in statuses), None)


def locate(f: JobFacts) -> Position:
    """Where the job is now, from its live state. Deterministic: the most specific active fact wins."""
    job, s = f.job, get_settings()
    state = str(job.state.value if hasattr(job.state, "value") else job.state)
    previous = str(job.previous_state.value if hasattr(job.previous_state, "value") else job.previous_state or "")
    if job.domain_match == "NOT_MATCHING":
        return Position("domain_filter", "BLOCKED", job.domain_checked_at, reason=job.domain_reason or
                        "Hidden by the domain filter", terminal=True)
    if job.domain_match == "PENDING":
        return Position("domain_classifier", "QUEUED", job.domain_checked_at, reason=job.domain_reason)
    if job.domain_match == "REVIEW":
        return Position("domain_classifier", "AWAITING_HUMAN", job.domain_checked_at, reason=job.domain_reason)
    tailor = _active(f.tailor_tasks, ("queued", "running"))
    if tailor is not None:
        if tailor.status == "queued":
            activity = "RETRYING" if (tailor.attempts or 0) > 0 else "QUEUED"
            return Position("tailor", activity, tailor.created_at, tailor.attempts or 0, _tailor_attempts(f.limits),
                            reason="Waiting for the tailoring worker")
        node = {"ranking": "ranker", "reviewing": "reviewer"}.get(tailor.stage, "tailor")
        return Position(node, "RUNNING", tailor.started_at or tailor.created_at, tailor.attempts or 1,
                        _tailor_attempts(f.limits))
    for tasks, node, limit in ((f.ranking_tasks, "ranker", f.limits.get("ranking", s.ranking_max_attempts)),
                               (f.parse, "jd_parser", f.limits.get("parsing", s.jd_parse_max_attempts)),
                               (f.fetch, "jd_recovery", f.limits.get("recovery", s.jd_fetch_max_attempts))):
        task = _active(tasks, ("PENDING", "PROCESSING"))
        if task is None:
            continue
        if task.status == "PROCESSING":
            return Position(node, "RUNNING", task.started_at or task.created_at, (task.attempt_count or 0) or 1, limit)
        retry = (task.attempt_count or 0) > 0
        return Position(node, "RETRYING" if retry else "QUEUED", task.created_at, task.attempt_count or 0, limit,
                        reason=_safe_error(task.last_error) if retry else None)
    last_change = f.transitions[-1].created_at if f.transitions else job.updated_at
    if state == "FAILED":
        node = topo.node_for_state(previous)
        return Position(node.id if node else "ranker", "FAILED", last_change, job.attempt_count,
                        reason=_safe_error(job.last_error) or (job.error_category.value if job.error_category else None))
    if state == "RANKING":
        return Position("ranker", "RUNNING", last_change)
    if state in ("FILTERED", "RANK_PENDING"):
        return Position("ranker", "QUEUED", last_change, reason="Waiting for the ranker")
    if state == "DISCOVERED":
        return Position("domain_filter", "QUEUED", job.discovered_at, reason="Not screened yet")
    app = f.application
    if app is not None:
        stage = app.stage or "applied"
        if app.status == "rejected":
            return Position(None, None, None, terminal=True, terminal_node="approval",
                            reason="You declined this packet at approval")
        if app.status == "applied":
            if stage in OFFER_STAGES + CLOSED_STAGES:
                return Position(None, None, None, terminal=True, terminal_node="outcome",
                                reason=f"Tracker stage: {stage}")
            if stage in INTERVIEW_STAGES:
                return Position("interview", "AWAITING_HUMAN", _stage_since(f, stage), reason=f"Tracker stage: {stage}")
            due = next((x for x in f.followups if x.status == "draft"), None)
            if due is not None and due.due_at and due.due_at <= utcnow():
                return Position("follow_up", "AWAITING_HUMAN", due.due_at, reason="A follow-up is due for you to send")
            return Position("application_inbox", "MONITORING", app.applied_at,
                            reason="Submitted; watching for replies")
        if app.status == "approved":
            if app.apply_blocker:
                return Position("application", "BLOCKED", app.approved_at,
                                reason=_safe_error(str((app.apply_blocker or {}).get("reason") or "Blocked")))
            return Position("application", "AWAITING_HUMAN", app.approved_at, reason="Approved; waiting to be submitted")
    if job.status in ("tailored", "pending_approval"):
        variant = f.variants[0] if f.variants else None
        return Position("approval", "AWAITING_HUMAN", variant.created_at if variant else last_change,
                        reason="Waiting for your approval")
    if job.status == "needs_retailor":
        return Position("tailor", "WAITING_TO_START", f.variants[0].created_at if f.variants else last_change,
                        reason="The review sent the packet back for another tailoring pass")
    if job.status == "dismissed":
        return Position(None, None, None, terminal=True, reason="You dismissed this job")
    if state == "ARCHIVED":
        return Position(None, None, None, terminal=True,
                        terminal_node="archived" if previous in ("RANKED", "QUALIFIED") else None,
                        reason=f.transitions[-1].reason if f.transitions else "Archived")
    if state == "SKIPPED":
        node = topo.node_for_state(previous)
        return Position(None, None, None, terminal=True, terminal_node=node.id if node else None,
                        reason=f.transitions[-1].reason if f.transitions else "Skipped")
    if state == "QUALIFIED":
        return Position("tailor", "WAITING_TO_START", last_change, reason="Qualified; not tailored yet")
    if state == "NORMALIZED":
        return Position("ranker", "WAITING_TO_START", last_change, reason="Waiting to be queued for ranking")
    if state in ("OFFER", "REJECTED"):
        return Position(None, None, None, terminal=True, terminal_node="outcome")
    node = topo.node_for_state(state)
    return Position(node.id if node else None, "WAITING_TO_START" if node else None, last_change)


def _stage_since(f: JobFacts, stage: str) -> datetime | None:
    return next((e.occurred_at for e in f.stage_events if e.kind == "stage" and e.detail == stage), None)


def _llm_summary(rows: list[Any]) -> dict[str, Any] | None:
    """The calls a job made at one stage: the latest one in full, the rest counted."""
    if not rows:
        return None
    latest = rows[0]
    return {"calls": len(rows), "failed": sum(r.status == "FAILED" for r in rows),
            "retries": sum((r.retry_count or 0) > 0 or bool(r.structured_output_retry_count) for r in rows),
            "escalations": sum(bool(r.escalated) for r in rows),
            "fallbacks": sum(r.fallback_used is True for r in rows), "latest": llm_card(latest)}


def _step(node_id: str, status: str, *, at: datetime | None = None, done_at: datetime | None = None,
          detail: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"node_id": node_id, "label": topo.NODES_BY_ID[node_id].label, "status": status, "started_at": _iso(at),
            "completed_at": _iso(done_at), "detail": detail, **extra}


TASK_STEP = {"COMPLETED": "COMPLETED", "FAILED": "FAILED", "BLOCKED": "BLOCKED", "SKIPPED": "SKIPPED",
             "PENDING": "WAITING", "PROCESSING": "CURRENT"}


def _history_steps(f: JobFacts) -> dict[str, dict[str, Any] | None]:
    """What the stored facts say happened at each stage, regardless of where the job is now. None = no evidence."""
    job = f.job
    llm_by_stage = _group(f.llm, lambda r: r.stage)
    steps: dict[str, dict[str, Any] | None] = {}
    steps["discovery"] = _step("discovery", "COMPLETED", at=job.discovered_at, done_at=job.discovered_at,
                               detail=" · ".join(x for x in (job.source_board or job.source, job.matched_search_role)
                                                 if x) or None, search_run_id=job.search_run_id)
    if job.domain_match == "NOT_MATCHING":
        steps["domain_filter"] = _step("domain_filter", "BLOCKED", done_at=job.domain_checked_at,
                                       detail=job.domain_reason or "Hidden: outside your domain")
    elif job.domain_match in ("MATCH", "KEPT"):
        steps["domain_filter"] = _step("domain_filter", "COMPLETED", done_at=job.domain_checked_at,
                                       detail=job.domain_reason)
    elif job.domain_match in ("PENDING", "REVIEW"):
        steps["domain_filter"] = _step("domain_filter", "COMPLETED", done_at=job.domain_checked_at,
                                       detail="Its rules could not settle this job")
    else:
        steps["domain_filter"] = None
    steps["resume_match"] = _step("resume_match", "COMPLETED", detail=f"Resume #{job.resume_id}"
                                  + (f" ({job.resume_choice.lower()})" if job.resume_choice else "")) \
        if job.resume_id else _step("resume_match", "SKIPPED", detail="Your active resume is used")
    for node_id, tasks in (("jd_recovery", f.fetch), ("jd_parser", f.parse)):
        if not tasks:
            steps[node_id] = None
            continue
        t = tasks[0]
        extra = {"attempts": t.attempt_count, "tasks": len(tasks),
                 "failed_attempts": sum(x.status in ("FAILED", "BLOCKED") for x in tasks)}
        if node_id == "jd_recovery":
            extra.update(method=t.fetch_method, result=t.result_code, playwright_used=t.playwright_used,
                         llm_used=t.llm_used)
        steps[node_id] = _step(node_id, TASK_STEP.get(t.status, "WAITING"), at=t.started_at or t.created_at,
                               done_at=t.completed_at, detail=t.result_code if node_id == "jd_recovery" else t.reason,
                               error=_safe_error(t.last_error) if t.status in ("FAILED", "BLOCKED") else None,
                               error_category=t.error_category.value if t.error_category else None, **extra)
    if f.rankings or f.ranking_tasks:
        r = f.rankings[0] if f.rankings else None
        t = f.ranking_tasks[0] if f.ranking_tasks else None
        status = "COMPLETED" if r else TASK_STEP.get(t.status, "WAITING")
        steps["ranker"] = _step("ranker", status, at=(t.started_at or t.created_at) if t else None,
                                done_at=r.created_at if r else (t.completed_at if t else None),
                                detail=f"{r.decision} · fit {r.overall_fit_score:.0f}" if r else None,
                                decision=r.decision if r else None, ranking_version=r.version if r else None,
                                recorded_model=r.model if r else None, config_version=r.config_version if r else None,
                                attempts=t.attempt_count if t else None,
                                error=_safe_error(t.last_error) if t and t.status == "FAILED" else None)
    else:
        steps["ranker"] = None
    state = str(job.state.value if hasattr(job.state, "value") else job.state)
    archived = next((x for x in reversed(f.transitions) if str(getattr(x.to_state, "value", x.to_state)) == "ARCHIVED"),
                    None)
    steps["archived"] = _step("archived", "COMPLETED", done_at=archived.created_at, detail=archived.reason) \
        if state == "ARCHIVED" and archived else None
    if f.variants or f.tailor_tasks:
        v = f.variants[0] if f.variants else None
        t = f.tailor_tasks[0] if f.tailor_tasks else None
        status = "COMPLETED" if v else ("FAILED" if t and t.status == "failed" else
                                        "SKIPPED" if t and t.status == "cancelled" else "WAITING")
        steps["tailor"] = _step("tailor", status, at=(t.started_at or t.created_at) if t else None,
                                done_at=v.created_at if v else (t.finished_at if t else None),
                                detail=f"Version {v.version}" + (" · master resume unchanged" if (v.content or {})
                                                                 .get("untailored") else "") if v else None,
                                versions=len(f.variants), recorded_model=(v.content or {}).get("model") if v else None,
                                error=_safe_error(t.error) if t and t.status == "failed" else None)
    else:
        steps["tailor"] = None
    reviewed = next((v for v in f.variants if v.reviewer_score is not None), None)
    # No review timestamp is stored: the packet's creation time is before the reviewer's call, so it stays unknown.
    steps["reviewer"] = _step("reviewer", "COMPLETED", done_at=None,
                              detail=f"{(reviewed.reviewer_notes or {}).get('decision') or 'Reviewed'} · score "
                                     f"{reviewed.reviewer_score:.0f}",
                              decision=(reviewed.reviewer_notes or {}).get("decision"),
                              recorded_model=(reviewed.reviewer_notes or {}).get("model")) if reviewed else None
    app = f.application
    if app is not None and app.approved_at:
        steps["approval"] = _step("approval", "COMPLETED", done_at=app.approved_at,
                                  detail="Approved below the reviewer minimum" if app.approval_override else "Approved")
    elif app is not None and app.status == "rejected":
        steps["approval"] = _step("approval", "SKIPPED", detail="You declined this packet")
    else:
        steps["approval"] = None
    if app is not None and app.applied_at:
        steps["application"] = _step("application", "COMPLETED", done_at=app.applied_at,
                                     detail=f"Submitted via {app.submitted_via}" if app.submitted_via else "Submitted")
    elif app is not None and app.apply_blocker:
        steps["application"] = _step("application", "BLOCKED", detail=_safe_error(str(app.apply_blocker.get("reason"))))
    else:
        steps["application"] = None
    steps["application_inbox"] = _step("application_inbox", "COMPLETED",
                                       detail=f"{f.inbox_events} email{'s' if f.inbox_events != 1 else ''} matched") \
        if f.inbox_events else None
    sent = next((x for x in f.followups if x.status == "sent"), None)
    if sent:
        steps["follow_up"] = _step("follow_up", "COMPLETED", done_at=sent.sent_at, detail="Follow-up sent")
    elif f.followups and all(x.status == "cancelled" for x in f.followups):
        steps["follow_up"] = _step("follow_up", "SKIPPED", detail="Cancelled: a reply arrived")
    else:
        steps["follow_up"] = None
    moves = [e for e in f.stage_events if e.kind == "stage"]
    interview = next((e for e in moves if e.detail in INTERVIEW_STAGES), None)
    steps["interview"] = _step("interview", "COMPLETED", done_at=interview.occurred_at, detail=interview.title) \
        if interview else None
    ended = next((e for e in moves if e.detail in OFFER_STAGES + CLOSED_STAGES), None)
    steps["outcome"] = _step("outcome", "COMPLETED", done_at=ended.occurred_at, detail=ended.title) if ended else None
    for node_id, step in steps.items():
        node = topo.NODES_BY_ID[node_id]
        if step is not None and node.llm_stages:
            step["llm"] = _llm_summary([r for s in node.llm_stages for r in llm_by_stage.get(s, [])])
    return steps


def job_path(f: JobFacts, position: Position) -> list[dict[str, Any]]:
    """The job's route through the graph: history where it has been, CURRENT where it is, WAITING ahead."""
    history = _history_steps(f)
    focus = position.node_id or position.terminal_node
    focus_index = PATH.index(focus) if focus in PATH else None
    out = []
    for index, node_id in enumerate(PATH):
        step = history.get(node_id)
        if node_id == position.node_id:
            status = {"RUNNING": "CURRENT", "QUEUED": "CURRENT", "AWAITING_HUMAN": "CURRENT", "MONITORING": "CURRENT",
                      "WAITING_TO_START": "CURRENT", "RETRYING": "RETRYING", "BLOCKED": "BLOCKED",
                      "FAILED": "FAILED"}.get(position.activity or "", "CURRENT")
            step = {**(step or _step(node_id, status)), "status": status, "current": True,
                    "activity": position.activity, "since": _iso(position.since), "reason": position.reason}
        elif step is None:
            if node_id in BRANCH_NODES:
                continue
            ahead = focus_index is not None and index > focus_index
            if position.terminal:
                status, detail = "SKIPPED", "Not reached"
            elif ahead:
                status, detail = "WAITING", None
            else:
                status, detail = "SKIPPED", "No record at this stage"
            step = _step(node_id, status, detail=detail)
        elif node_id == position.terminal_node:
            step = {**step, "terminal": True}
        out.append(step)
    return out


def packet(db: Session, job: Job, user_id: int, f: JobFacts | None = None) -> dict[str, Any]:
    """A job as orchestration shows it: who it is (title, company, id — nothing private), where it is, and what is
    handling it. The model is the one telemetry recorded for the current stage; failing that, the assignment, labelled
    as such."""
    f = f or load_facts(db, job, user_id)
    pos = locate(f)
    now = utcnow()
    node = topo.node(pos.node_id) if pos.node_id else None
    model, model_source = None, None
    if node and node.llm_stages:
        recent = [r for r in f.llm if r.stage in node.llm_stages and (pos.since is None or r.created_at >= pos.since)]
        if recent and recent[0].model:
            model, model_source = recent[0].model, "telemetry"
        elif node.workers:
            a = worker_assignment(node.workers[0], gateway_state(db, user_id), user_id)
            if a.get("resolved_model"):
                model, model_source = a["resolved_model"], "assignment"
    state = job.state.value if hasattr(job.state, "value") else job.state
    return {"job_id": job.id, "title": job.title, "company": job.company, "state": state, "status": job.status,
            "node_id": pos.node_id, "node_label": node.label if node else None, "activity": pos.activity,
            "since": _iso(pos.since), "elapsed_seconds": _seconds(pos.since, now), "attempt": pos.attempt,
            "max_attempts": pos.max_attempts, "reason": pos.reason, "terminal": pos.terminal,
            "terminal_node": pos.terminal_node, "model": model, "model_source": model_source,
            "active": pos.activity in ("RUNNING", "QUEUED", "RETRYING"), "generated_at": _iso(now)}


def job_state(db: Session, job_id: int, user_id: int) -> dict[str, Any] | None:
    job = db.scalar(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    if job is None:
        return None
    f = load_facts(db, job, user_id)
    pos = locate(f)
    return {"packet": packet(db, job, user_id, f), "path": job_path(f, pos)}


def search_jobs(db: Session, user_id: int, q: str, limit: int = 20) -> list[dict[str, Any]]:
    """The job selector: the account's own jobs by id, title or company. Labels only."""
    query = select(Job.id, Job.title, Job.company, Job.state, Job.status, Job.updated_at).where(Job.user_id == user_id)
    text = (q or "").strip()
    if text.lstrip("#").isdigit():
        query = query.where(Job.id == int(text.lstrip("#")))
    elif text:
        like = f"%{text.lower()[:100]}%"
        query = query.where(or_(func.lower(Job.title).like(like), func.lower(Job.company).like(like)))
    rows = db.execute(query.order_by(Job.updated_at.desc()).limit(max(1, min(limit, 50)))).all()
    return [{"job_id": r[0], "title": r[1], "company": r[2], "state": getattr(r[3], "value", r[3]), "status": r[4],
             "updated_at": _iso(r[5])} for r in rows]


# ───────────── live packets ─────────────

def live(db: Session, scope: Scope) -> dict[str, Any]:
    """What is in flight right now: active task rows, as packets. Cheap: active rows only, bounded.

    The admin's every-account view gets node counts across accounts but packets only from their own account."""
    now = utcnow()
    counts = live_counts(db, scope.user_id, now)
    owner = scope.viewer_id
    settings = get_settings()
    found: dict[int, dict[str, Any]] = {}

    def add(job_id: int, node_id: str, activity: str, since: datetime | None, attempt: int | None, limit: int | None,
            task: str, task_id: int) -> None:
        if job_id in found and found[job_id]["activity"] == "RUNNING":
            return
        found[job_id] = {"job_id": job_id, "node_id": node_id, "node_label": topo.NODES_BY_ID[node_id].label,
                         "activity": activity, "since": _iso(since), "elapsed_seconds": _seconds(since, now),
                         "attempt": attempt, "max_attempts": limit, "task": task, "task_id": task_id}

    own = {kind: worker_settings.get(db, owner, kind, "max_attempts") for kind in ("classification", "recovery", "parsing", "ranking",
                                                                                  "insights", "tailoring",
                                                                                  "qualification", "preparation",
                                                                                  "application")}
    for t in TASK_TABLES:
        limit = own[WORKER_KIND_BY_NODE[t.node_id]]
        m = t.model
        rows = db.execute(select(m.id, m.job_id, m.status, m.attempt_count, m.created_at, t.started)
                          .where(m.user_id == owner, m.status.in_(t.pending + t.running))
                          .order_by(m.created_at).limit(LIVE_LIMIT)).all()
        for r in rows:
            running = r[2] in t.running
            activity = "RUNNING" if running else ("RETRYING" if (r[3] or 0) > 0 else "QUEUED")
            add(r[1], t.node_id, activity, (r[5] or r[4]) if running else r[4], r[3] or (1 if running else 0), limit,
                m.__tablename__, r[0])
    rows = db.execute(select(TailorTask.id, TailorTask.job_id, TailorTask.status, TailorTask.stage, TailorTask.attempts,
                             TailorTask.created_at, TailorTask.started_at)
                      .where(TailorTask.user_id == owner, TailorTask.status.in_(("queued", "running")))
                      .order_by(TailorTask.created_at).limit(LIVE_LIMIT)).all()
    for r in rows:
        if r[2] == "running":
            node = {"ranking": "ranker", "reviewing": "reviewer"}.get(r[3], "tailor")
            found.pop(r[1], None)  # the tailoring run is the most specific fact about this job
            add(r[1], node, "RUNNING", r[6] or r[5], r[4] or 1, _tailor_attempts(own), "tailor_tasks", r[0])
        else:
            add(r[1], "tailor", "RETRYING" if (r[4] or 0) > 0 else "QUEUED", r[5], r[4] or 0, _tailor_attempts(own),
                "tailor_tasks", r[0])
    items = sorted(found.values(), key=lambda p: (p["activity"] != "RUNNING", p["since"] or ""))[:LIVE_LIMIT]
    labels = _job_labels(db, [p["job_id"] for p in items], owner)
    items = [{**p, **labels.get(p["job_id"], {"title": None, "company": None})} for p in items]
    _attach_models(db, items, owner)
    return {"generated_at": _iso(now), "scope": scope.public(), "counts": counts, "packets": items,
            "active": bool(items), "total_running": sum(c["running"] for c in counts.values()),
            "total_queued": sum(c["queued"] + c["retrying"] for c in counts.values())}


def _attach_models(db: Session, items: list[dict[str, Any]], user_id: int) -> None:
    """The model telemetry recorded for each running packet's stage since it started; nothing when none was."""
    running = [p for p in items if p["activity"] == "RUNNING" and p["since"] and topo.NODES_BY_ID[p["node_id"]].llm_stages]
    if not running:
        return
    since = min(datetime.fromisoformat(p["since"]) for p in running)
    rows = db.execute(select(LLMRequestLog.workflow_entity_id, LLMRequestLog.stage, LLMRequestLog.model,
                             LLMRequestLog.created_at)
                      .where(LLMRequestLog.user_id == user_id, LLMRequestLog.workflow_type == "job",
                             LLMRequestLog.workflow_entity_id.in_([p["job_id"] for p in running]),
                             LLMRequestLog.created_at >= since)
                      .order_by(LLMRequestLog.created_at.desc()).limit(500)).all()
    for p in running:
        stages = topo.NODES_BY_ID[p["node_id"]].llm_stages
        hit = next((r for r in rows if r[0] == p["job_id"] and r[1] in stages and r[2]
                    and r[3] >= datetime.fromisoformat(p["since"])), None)
        p["model"] = hit[2] if hit else None


# ───────────── the trace: one job, every source, in order ─────────────

def job_trace(db: Session, job_id: int, user_id: int) -> dict[str, Any] | None:
    """Workflow transitions, task lifecycle, packet milestones, LLM calls, external calls and log lines for one job,
    merged in timestamp order. Correlated by the ids every source already stores (job, application, trace)."""
    job = db.scalar(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    if job is None:
        return None
    f = load_facts(db, job, user_id)
    events: list[dict[str, Any]] = []

    def add(at: datetime | None, source: str, kind: str, title: str, *, node: str | None = None, status: str | None = None,
            **extra: Any) -> None:
        if at is not None:
            events.append({"at": _iso(at), "source": source, "kind": kind, "title": title, "node_id": node,
                           "status": status, **extra})

    add(job.discovered_at, "workflow", "DISCOVERED", "Discovered", node="discovery",
        detail=job.source_board or job.source)
    for t in f.transitions:
        to = str(getattr(t.to_state, "value", t.to_state))
        node = topo.node_for_state(to)
        add(t.created_at, "workflow", to, f"{getattr(t.from_state, 'value', t.from_state) or '—'} → {to}",
            node=node.id if node else None, actor=t.actor, detail=_safe_error(t.reason), attempt=t.attempt_count,
            error_category=getattr(t.error_category, "value", t.error_category))
    for node_id, tasks in (("jd_recovery", f.fetch), ("jd_parser", f.parse), ("ranker", f.ranking_tasks)):
        for t in tasks:
            add(t.created_at, "task", "QUEUED", f"{topo.NODES_BY_ID[node_id].label} queued", node=node_id,
                task_id=t.id, detail=t.reason)
            add(t.started_at, "task", "STARTED", f"{topo.NODES_BY_ID[node_id].label} started", node=node_id,
                task_id=t.id, attempt=t.attempt_count)
            done = getattr(t, "completed_at", None)
            add(done, "task", t.status, f"{topo.NODES_BY_ID[node_id].label} {t.status.lower()}", node=node_id,
                status=t.status, task_id=t.id, attempt=t.attempt_count,
                error=_safe_error(t.last_error) if t.status in ("FAILED", "BLOCKED") else None,
                detail=getattr(t, "result_code", None))
    for t in f.tailor_tasks:
        add(t.created_at, "task", "QUEUED", f"Tailoring queued ({t.mode})", node="tailor", task_id=t.id)
        add(t.started_at, "task", "STARTED", "Tailoring started", node="tailor", task_id=t.id, attempt=t.attempts)
        add(t.finished_at, "task", t.status.upper(), f"Tailoring {t.status}", node="tailor", status=t.status.upper(),
            task_id=t.id, error=_safe_error(t.error) if t.status == "failed" else None)
    for v in f.variants:
        add(v.created_at, "packet", "TAILORED", f"Tailored version {v.version} saved", node="tailor")
        # A review has no stored time of its own (the variant's created_at predates the reviewer call), so it is not
        # placed on the timeline; the reviewer step shows its outcome.
    app = f.application
    if app is not None:
        add(app.approved_at, "packet", "APPROVED", "Approved", node="approval")
        add(app.applied_at, "packet", "APPLIED", "Submitted", node="application", detail=app.submitted_via)
        for e in f.stage_events:
            node = "interview" if e.detail in INTERVIEW_STAGES else "outcome" if e.detail in OFFER_STAGES + CLOSED_STAGES \
                else "application_inbox"
            # Never the event's own title: inbox events build it from the email's sender. Kind and stage only.
            label = (f"Stage: {str(e.detail).replace('_', ' ').lower()}" if e.kind == "stage" and e.detail
                     else str(e.kind or "event").replace("_", " ").capitalize())
            add(e.occurred_at, "tracker", e.kind.upper(), label, node=node)
        for x in f.followups:
            add(x.created_at, "packet", "FOLLOWUP_DRAFTED", "Follow-up drafted", node="follow_up", status=x.status)
            add(x.sent_at, "packet", "FOLLOWUP_SENT", "Follow-up sent", node="follow_up")
        # Email: the classification only. Never a subject, sender or body.
        for r in db.execute(select(ApplicationCommunicationEvent.received_at, ApplicationCommunicationEvent.event_type,
                                   ApplicationCommunicationEvent.stage_decision)
                            .where(ApplicationCommunicationEvent.application_id == app.id,
                                   ApplicationCommunicationEvent.user_id == user_id)
                            .order_by(ApplicationCommunicationEvent.received_at.desc()).limit(50)).all():
            add(r[0], "inbox", r[1], f"Email classified: {r[1].replace('_', ' ').lower()}", node="application_inbox",
                detail=f"stage decision {r[2]}")
        # Matching: the status and score only. A message the matcher was unsure of stays NEEDS_REVIEW.
        for r in db.execute(select(ApplicationEmailMessage.id, ApplicationEmailMessage.received_at,
                                   ApplicationEmailMessage.match_status, ApplicationEmailMessage.match_score)
                            .where(ApplicationEmailMessage.application_id == app.id,
                                   ApplicationEmailMessage.user_id == user_id)
                            .order_by(ApplicationEmailMessage.received_at.desc()).limit(50)).all():
            add(r[1], "inbox", f"EMAIL_{r[2]}", f"Email {r[2].replace('_', ' ').lower()}",
                node="inbox_review" if r[2] == "NEEDS_REVIEW" else "application_inbox", status=r[2],
                message_id=r[0], match_score=r[3])
        from app.services.journey import review_messages

        for r in review_messages(db, app.id, user_id):
            add(r.received_at, "inbox", "EMAIL_NEEDS_REVIEW", "Email waiting for review (not matched)",
                node="inbox_review", status="NEEDS_REVIEW", message_id=r.id, match_score=r.match_score)
    for t in db.scalars(select(ApplicationExecutionTask).where(ApplicationExecutionTask.job_id == job.id,
                                                               ApplicationExecutionTask.user_id == user_id)
                        .order_by(ApplicationExecutionTask.id.desc()).limit(10)).all():
        add(t.created_at, "task", "QUEUED", f"Application run queued ({t.mode.lower()})", node="application_execution",
            task_id=t.id)
        add(t.started_at, "task", "STARTED", "Application run started", node="application_execution", task_id=t.id,
            attempt=t.attempt_count)
        add(t.completed_at, "task", t.stage, f"Application run {t.stage.replace('_', ' ').lower()}",
            node="application_execution", status=t.status, task_id=t.id, detail=t.result_code)
    for c in db.scalars(select(ApplicationClaim).where(ApplicationClaim.job_id == job.id,
                                                       ApplicationClaim.user_id == user_id)
                        .order_by(ApplicationClaim.id.desc()).limit(10)).all():
        add(c.claimed_at, "packet", "CLAIMED", f"Posting claimed ({c.client})", node="application", claim_id=c.id)
        add(c.resolved_at, "packet", c.status, f"Claim {c.status.lower()}", node="application", status=c.status,
            claim_id=c.id)
    for i in db.scalars(select(SubmissionIntent).where(SubmissionIntent.job_id == job.id,
                                                       SubmissionIntent.user_id == user_id)
                        .order_by(SubmissionIntent.id.desc()).limit(10)).all():
        add(i.clicking_at, "packet", "CLICKING", "Final submit clicked", node="submission_confirmation",
            intent_id=i.id)
        add(i.resolved_at, "packet", i.status, f"Submission {i.status.replace('_', ' ').lower()}",
            node="submission_confirmation", status=i.status, intent_id=i.id,
            form_gone=(i.evidence or {}).get("form_gone"))
    trace_ids = set()
    for r in f.llm:
        node = topo.node_for_llm_stage(r.stage)
        card = llm_card(r)
        trace_ids.add(r.trace_id)
        add(r.created_at, "llm", "LLM_CALL", f"{(r.worker_type or r.stage).replace('_', ' ').title()} model call",
            node=node.id if node else None, status=r.status, llm=card)
    ext_filter = ExternalAPIRequestLog.job_id == job.id
    if app is not None:
        ext_filter = or_(ext_filter, ExternalAPIRequestLog.application_id == app.id)
    ext = db.execute(select(ExternalAPIRequestLog.id, ExternalAPIRequestLog.created_at, ExternalAPIRequestLog.provider,
                            ExternalAPIRequestLog.operation, ExternalAPIRequestLog.status,
                            ExternalAPIRequestLog.http_status_code, ExternalAPIRequestLog.latency_ms,
                            ExternalAPIRequestLog.error_type, ExternalAPIRequestLog.retry_count,
                            ExternalAPIRequestLog.trace_id, ExternalAPIRequestLog.endpoint_host)
                     .where(ExternalAPIRequestLog.user_id == user_id, ext_filter)
                     .order_by(ExternalAPIRequestLog.created_at.desc()).limit(JOB_TELEMETRY_LIMIT)).all()
    for r in ext:
        trace_ids.add(r[9])
        node = next((n for n in topo.NODES if n.implemented and _matches(n, r)), None)
        add(r[1], "external", "EXTERNAL_CALL", f"{r[2]} {r[3]}", node=node.id if node else None, status=r[4],
            external={"id": r[0], "provider": r[2], "operation": r[3], "http_status_code": r[5], "latency_ms": r[6],
                      "error_type": r[7], "retry_count": r[8], "trace_id": r[9], "host": r[10]})
    trace_ids.discard(None)
    log_filter = and_(SystemLogEntry.entity_type == "job", SystemLogEntry.entity_id == job.id)
    if trace_ids:
        log_filter = or_(log_filter, SystemLogEntry.trace_id.in_(sorted(trace_ids)[:100]))
    logs = db.execute(select(SystemLogEntry.id, SystemLogEntry.created_at, SystemLogEntry.level,
                             SystemLogEntry.category, SystemLogEntry.message, SystemLogEntry.trace_id,
                             SystemLogEntry.workflow, SystemLogEntry.status, SystemLogEntry.entity_type,
                             SystemLogEntry.entity_id)
                      .where(SystemLogEntry.user_id == user_id, log_filter)
                      .order_by(SystemLogEntry.created_at.desc()).limit(JOB_TELEMETRY_LIMIT)).all()
    for r in logs:
        # LLM calls already appear as calls; their INFO log line would only repeat them.
        if r[3] == "LLM" and r[2] == "INFO":
            continue
        add(r[1], "log", r[3], redaction.redact_text(r[4], 300) or "", status=r[7], level=r[2],
            log={"id": r[0], "trace_id": r[5], "workflow": r[6]})
    events.sort(key=lambda e: (e["at"], e["source"] != "workflow"))
    truncated = len(events) > TRACE_LIMIT
    return {"job_id": job.id, "title": job.title, "company": job.company, "events": events[-TRACE_LIMIT:],
            "truncated": truncated, "trace_ids": sorted(trace_ids),
            "sources": dict(Counter(e["source"] for e in events)),
            "note": "Metadata only: no prompts, responses, resume text or email content."}
