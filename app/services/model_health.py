"""How each model is really performing, and when a route should be rested.

A *route* is a model overall (worker_type "") or a model for one worker. Health comes from what actually happened:
`llm_request_logs` rows, judged deterministically against the account's thresholds (app.services.failover_settings).
No model is ever called, and unknown stays healthy: a route with fewer than `min_samples` requests is never judged.

    request outcome → record_outcome   a 429 or a run of failures rests the route at once (database row, any process)
    scheduled sweep → evaluate         windows judged: failure rate, rate limits, schema failures, p95 latency, and
                                       latency against the route's own 7-day history
    a rested route  → COOLING until `cooling_until`, then half-open: traffic probes it, `recovery_successes`
                      successes restore it, one failure rests it again for twice as long

A 429 that names its provider ("OpenrouterException - …") rests the whole provider, as a route `provider:<name>` for
every worker: each model the configuration says that provider serves, and each gateway alias (an auto router) seen
refusing with that provider's 429 on a request sent without fallbacks. A shared free-model quota (OpenRouter's
free-models-per-day) rests only the free models. A 429 that names no provider rests that one model, as before.

The state is an overlay on the active AI configuration (app.services.failover reads it); nothing here edits a
configuration version. State lives in `model_route_states`, so every process agrees; readers cache it for a moment.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select

from app.services import failover_settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger("jobhunt.model_health")

CACHE_SECONDS = 2.0
MAX_ROWS = 3000  # rows read per route and window: bounded
BASELINE_DAYS = 7
BASELINE_MIN_SAMPLES = 20
# Errors that say nothing about how a model performs: our own configuration and an oversized request.
NOT_HEALTH = ("CONFIGURATION", "CONTEXT_OVERFLOW")
SCHEMA_ERRORS = ("SCHEMA_VALIDATION", "INVALID_JSON")
# A provider-wide rest is the route (worker_type "", "provider:<name>").
PROVIDER_PREFIX = "provider:"
MAX_MEMBERS = 100  # gateway aliases remembered per provider: bounded


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


_sessions: Callable[[], Session] | None = None
_lock = threading.Lock()
_cache: dict[int, tuple[float, dict[tuple[str, str], dict[str, Any]]]] = {}


def use_sessions(factory: Callable[[], Session] | None) -> None:
    """Point health tracking at another database (tests); None restores the application database."""
    global _sessions
    _sessions = factory
    reset_cache()


def _session_factory() -> Callable[[], Session]:
    if _sessions is not None:
        return _sessions
    from app.db import SessionLocal

    return SessionLocal


def reset_cache(user_id: int | None = None) -> None:
    with _lock:
        if user_id is None:
            _cache.clear()
        else:
            _cache.pop(user_id, None)


# ───────────── windows: pure ─────────────

@dataclass(frozen=True)
class Stats:
    samples: int = 0
    failures: int = 0
    rate_limits: int = 0
    schema_failures: int = 0
    p50_ms: int | None = None
    p95_ms: int | None = None

    @property
    def failure_pct(self) -> float:
        return round(100 * self.failures / self.samples, 1) if self.samples else 0.0

    @property
    def schema_pct(self) -> float:
        return round(100 * self.schema_failures / self.samples, 1) if self.samples else 0.0

    def public(self) -> dict[str, Any]:
        return {"samples": self.samples, "failures": self.failures, "failure_pct": self.failure_pct,
                "rate_limits": self.rate_limits, "schema_failures": self.schema_failures,
                "schema_pct": self.schema_pct, "p50_ms": self.p50_ms, "p95_ms": self.p95_ms}


def percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(pct / 100 * len(ordered)) - 1))]


def summarize(rows: list[dict[str, Any]]) -> Stats:
    """Stats over request rows ({status, error_type, latency_ms, fallback_used, error_message}). A request the gateway
    had to fail over (fallback_used) did not get its answer from this model, so it counts against it. A 429 for one
    oversized request is not counted: it says nothing about the model."""
    from app.services.rate_limits import too_large

    failures = rate_limits = schema = samples = 0
    latencies: list[int] = []
    for row in rows:
        kind = row.get("error_type")
        failed = row.get("status") == "FAILED"
        if failed and (kind in NOT_HEALTH or (kind == "RATE_LIMIT" and too_large(row.get("error_message")))):
            continue
        samples += 1
        if failed and kind in SCHEMA_ERRORS:
            schema += 1
        elif failed or row.get("fallback_used"):
            failures += 1
            if kind == "RATE_LIMIT":
                rate_limits += 1
        elif row.get("latency_ms") is not None:
            latencies.append(int(row["latency_ms"]))
    return Stats(samples, failures, rate_limits, schema, percentile(latencies, 50), percentile(latencies, 95))


def judge(stats: Stats, cfg: dict[str, Any], baseline_p95: int | None = None) -> tuple[str, str] | None:
    """(cause, reason) when the window breaches a threshold, else None. Too few samples is never a breach."""
    if stats.samples < cfg["min_samples"]:
        return None
    if stats.rate_limits >= cfg["max_rate_limits"]:
        return "RATE_LIMIT", f"{stats.rate_limits} rate limits in the last {cfg['window_minutes']} minutes"
    if stats.failure_pct > cfg["max_failure_pct"]:
        return "ERROR_RATE", f"{stats.failure_pct:g}% of {stats.samples} requests failed (limit {cfg['max_failure_pct']}%)"
    if stats.schema_pct > cfg["max_schema_failure_pct"]:
        return "SCHEMA", f"{stats.schema_pct:g}% of answers broke their output contract"
    if cfg["max_p95_ms"] and stats.p95_ms and stats.p95_ms > cfg["max_p95_ms"]:
        return "LATENCY", f"p95 latency {stats.p95_ms} ms (limit {cfg['max_p95_ms']} ms)"
    if (cfg["degradation_pct"] and baseline_p95 and stats.p95_ms
            and stats.p95_ms > baseline_p95 * (1 + cfg["degradation_pct"] / 100)):
        return "LATENCY", f"p95 latency {stats.p95_ms} ms is well above its usual {baseline_p95} ms"
    return None


def score(stats: Stats, baseline_p95: int | None = None) -> float | None:
    """0-100, higher is better; None when there is nothing to judge. Reliability counts most, then output quality
    and speed against the route's own history. Used only to order healthy candidates."""
    if not stats.samples:
        return None
    value = 100.0 - 0.6 * stats.failure_pct - 0.25 * stats.schema_pct
    if baseline_p95 and stats.p95_ms:
        value -= min(15.0, max(0.0, 15.0 * (stats.p95_ms / baseline_p95 - 1)))
    return round(max(0.0, value), 1)


# ───────────── reading the logs ─────────────

def window_stats(db: Session, user_id: int, model: str, worker_type: str = "", *, since: datetime,
                 until: datetime | None = None) -> Stats:
    from app.models import LLMRequestLog

    query = select(LLMRequestLog.status, LLMRequestLog.error_type, LLMRequestLog.latency_ms,
                   LLMRequestLog.fallback_used, LLMRequestLog.error_message).where(
        LLMRequestLog.user_id == user_id, LLMRequestLog.provider == "litellm",
        LLMRequestLog.requested_model == model, LLMRequestLog.created_at >= since,
        # a request never sent (held by a cooldown or token pacing) says nothing about the model
        or_(LLMRequestLog.cost_source.is_(None), LLMRequestLog.cost_source != "not_sent"))
    if until is not None:
        query = query.where(LLMRequestLog.created_at < until)
    if worker_type:
        query = query.where(LLMRequestLog.worker_type == worker_type)
    rows = db.execute(query.order_by(LLMRequestLog.created_at.desc()).limit(MAX_ROWS)).all()
    return summarize([{"status": r[0], "error_type": r[1], "latency_ms": r[2], "fallback_used": r[3],
                       "error_message": r[4]} for r in rows])


def baseline_p95(db: Session, user_id: int, model: str, worker_type: str, *, now: datetime,
                 window_minutes: int) -> int | None:
    """The route's own typical p95: the previous days, before the window being judged."""
    stats = window_stats(db, user_id, model, worker_type, since=now - timedelta(days=BASELINE_DAYS),
                         until=now - timedelta(minutes=window_minutes))
    return stats.p95_ms if stats.samples >= BASELINE_MIN_SAMPLES else None


# ───────────── route state ─────────────

def _row_view(row: Any) -> dict[str, Any]:
    return {"state": row.state, "cooling_until": row.cooling_until, "score": row.score, "stats": row.stats or {},
            "consecutive_failures": row.consecutive_failures, "probe_successes": row.probe_successes,
            "reason": row.reason}


def states(user_id: int | None) -> dict[tuple[str, str], dict[str, Any]]:
    """Every non-default route of the account, keyed (worker_type, model). Cached for a moment; never raises."""
    if user_id is None:
        return {}
    now = time.monotonic()
    with _lock:
        hit = _cache.get(user_id)
        if hit and hit[0] > now:
            return hit[1]
    try:
        from app.models import ModelRouteState

        with _session_factory()() as db:
            rows = db.scalars(select(ModelRouteState).where(ModelRouteState.user_id == user_id).limit(2000)).all()
            view = {(r.worker_type, r.model): _row_view(r) for r in rows}
    except Exception:
        log.warning("could not read model route states", exc_info=True)
        return {}
    with _lock:
        _cache[user_id] = (now + CACHE_SECONDS, view)
    return view


def resting(user_id: int | None, model: str | None, worker_type: str = "", *, now: datetime | None = None) -> bool:
    """True while the model should not be used for this worker: rested model-wide, for this worker, or because its
    provider is rate limited."""
    if not model:
        return False
    moment = now or utcnow()
    view = states(user_id)
    for key in (("", model), (worker_type, model)):
        entry = view.get(key)
        if entry and entry["state"] != "HEALTHY" and entry["cooling_until"] and entry["cooling_until"] > moment:
            return True
    return _provider_until(user_id, model, view, moment) is not None


def limited_until(user_id: int | None, model: str | None, worker_type: str = "", *,
                  now: datetime | None = None) -> datetime | None:
    """Until when the model is rate limited (a COOLING rest: its own, this worker's, or its provider's), else None.
    A rest for errors (DEGRADED) is not a rate limit: such a model stays a last resort."""
    if not model or user_id is None:
        return None
    moment = now or utcnow()
    view = states(user_id)
    untils = [entry["cooling_until"] for key in (("", model), (worker_type, model))
              if (entry := view.get(key)) and entry["state"] == "COOLING" and entry["cooling_until"]
              and entry["cooling_until"] > moment]
    provider = _provider_until(user_id, model, view, moment)
    if provider is not None:
        untils.append(provider)
    return max(untils) if untils else None


# ───────────── providers ─────────────

def provider_route(name: str) -> str:
    return f"{PROVIDER_PREFIX}{name}"[:200]


def _model_entry(user_id: int | None, model: str) -> dict[str, Any]:
    """The model's entry in the active configuration (cached by model_resolution). Never raises."""
    try:
        from app.services import model_resolution

        snapshot = model_resolution.active_snapshot(user_id)[1]
        return ((snapshot.get("models") or {}).get(model) or {}) | {
            "_router": (snapshot.get("routing") or {}).get("auto_router_model_name") == model}
    except Exception:  # noqa: BLE001  unknown means no provider is known
        return {}


def configured_provider(user_id: int | None, model: str) -> str | None:
    """The provider the configuration says serves this model, or None for an auto router or an unreported provider.
    Ollama's own models are not rate limited; its hosted (cloud) models are, under "ollama_cloud"."""
    entry = _model_entry(user_id, model)
    provider = str(entry.get("provider") or "").lower()
    if not provider or provider == "litellm" or entry.get("model_type") == "AUTO_ROUTER" or entry.get("_router"):
        return None
    provider = provider.removesuffix("_chat")
    if provider == "ollama":
        underlying = str(entry.get("underlying_model") or model).lower()
        return "ollama_cloud" if underlying.endswith((":cloud", "-cloud")) or model.lower().endswith(
            (":cloud", "-cloud")) else None
    return provider


def _free(user_id: int | None, model: str) -> bool | None:
    """Whether the model is one of a provider's free models: True/False when its name says, None for an alias."""
    entry = _model_entry(user_id, model)
    names = [n.lower() for n in (model, entry.get("underlying_model")) if isinstance(n, str) and n]
    if any(n.endswith((":free", "/free")) or n == "free" for n in names):
        return True
    return False if configured_provider(user_id, model) else None


def provider_keys(user_id: int | None, model: str, view: dict[tuple[str, str], dict[str, Any]] | None = None) -> list[
        str]:
    """The provider routes that cover this model: its configured provider, else every provider it was seen refusing
    for."""
    configured = configured_provider(user_id, model)
    if configured:
        return [provider_route(configured)]
    return [key for (worker, key), entry in (view if view is not None else states(user_id)).items()
            if worker == "" and key.startswith(PROVIDER_PREFIX) and model in ((entry.get("stats") or {}).get("members")
                                                                             or ())]


def _provider_until(user_id: int | None, model: str, view: dict[tuple[str, str], dict[str, Any]],
                    moment: datetime) -> datetime | None:
    if not any(worker == "" and key.startswith(PROVIDER_PREFIX) for worker, key in view):
        return None  # the common case: no provider is resting, and no configuration lookup is needed
    untils = []
    for key in provider_keys(user_id, model, view):
        entry = view.get(("", key))
        if not entry or entry["state"] != "COOLING" or not entry["cooling_until"] or entry["cooling_until"] <= moment:
            continue
        if (entry.get("stats") or {}).get("free_only") and _free(user_id, model) is False:
            continue  # the free-model quota is used up; a paid model on the same provider is not
        untils.append(entry["cooling_until"])
    return max(untils) if untils else None


def route_score(user_id: int | None, model: str, worker_type: str = "") -> tuple[float | None, dict[str, Any]]:
    entry = states(user_id).get((worker_type, model)) or {}
    return entry.get("score"), entry.get("stats") or {}


def _row(db: Session, user_id: int, worker_type: str, model: str, create: bool = True) -> Any:
    from app.models import ModelRouteState

    row = db.scalar(select(ModelRouteState).where(ModelRouteState.user_id == user_id,
                                                  ModelRouteState.worker_type == worker_type,
                                                  ModelRouteState.model == model))
    if row is None and create:
        row = ModelRouteState(user_id=user_id, worker_type=worker_type, model=model, state="HEALTHY",
                              consecutive_failures=0, probe_successes=0, updated_at=utcnow())
        db.add(row)
        db.flush()
    return row


def _cooldown(cfg: dict[str, Any], trips: int, rest_seconds: int | None = None, cause: str | None = None) -> timedelta:
    """How long a route rests. A model's own rest time is exactly that, every time: it is the person's answer to "when
    is it tried again". Then, for a 429, the account's rest after a rate limit, also the same every time. Otherwise the
    account's cooldown doubles on each trip in a row, up to the longest."""
    from app.services.backoff import RateLimitPolicy

    if rest_seconds:
        return timedelta(seconds=rest_seconds)
    if cause == "RATE_LIMIT" and cfg.get("rate_limit_rest_minutes"):
        return timedelta(minutes=cfg["rate_limit_rest_minutes"])
    return RateLimitPolicy(True, cfg["cooldown_seconds"], 2.0, cfg["max_cooldown_seconds"]).delay(max(1, trips))


def _rest_seconds(user_id: int, model: str) -> int | None:
    """The model's own rest time from the active configuration (cached by model_resolution). Never raises."""
    try:
        from app.services import model_resolution

        return model_resolution.rest_seconds_of(model_resolution.active_snapshot(user_id)[1], model)
    except Exception:  # noqa: BLE001  unknown means the account's setting
        return None


def _emit(db: Session, user_id: int, *, worker_type: str, from_model: str, to_model: str | None, cause: str,
          message: str, metrics: dict[str, Any] | None, cfg: dict[str, Any]) -> None:
    from app.models import FailoverEvent

    db.add(FailoverEvent(user_id=user_id, worker_type=worker_type, from_model=from_model, to_model=to_model,
                         cause=cause, algorithm=cfg.get("algorithm"), message=message[:400], metrics=metrics,
                         created_at=utcnow()))
    # The Monitoring log line goes out after the commit: system_log writes through its own connection, which would
    # wait on this transaction's lock (SQLite) until the route state it describes is saved.
    db.info.setdefault("failover_logs", []).append({
        "message": message, "category": "LLM", "level": "WARNING" if cause != "RECOVERED" else "INFO",
        "user_id": user_id, "operation": "failover", "status": cause,
        "details": {"worker_type": worker_type or None, "from_model": from_model, "to_model": to_model,
                    **(metrics or {})}})


def _flush_logs(db: Session) -> None:
    """Write the Monitoring lines queued by `_emit`. Call after the commit; never raises."""
    from app.services import system_log

    for entry in db.info.pop("failover_logs", []):
        system_log.log_event(**entry)


def trip(db: Session, user_id: int, model: str, worker_type: str, *, cause: str, reason: str,
         cfg: dict[str, Any], stats: Stats | None = None, now: datetime | None = None,
         not_before: datetime | None = None, rest_model: str | None = None) -> bool:
    """Rest a route. Returns False when it was already resting (so one outage is one event, not hundreds).

    `not_before` is when the provider said the limit clears: the route rests at least until then, since a call
    before it can only be refused again. A route already resting is held that long too, without a new event."""
    moment = now or utcnow()
    row = _row(db, user_id, worker_type, model)
    if row.state != "HEALTHY" and row.cooling_until and row.cooling_until > moment:
        if not_before and not_before > row.cooling_until:
            row.cooling_until, row.reason, row.updated_at = not_before, reason[:300], moment
            reset_cache(user_id)
        return False
    trips = int((row.stats or {}).get("trips", 0)) + 1 if row.state != "HEALTHY" or row.probe_successes else 1
    row.state = "COOLING" if cause == "RATE_LIMIT" else "DEGRADED"
    rest = _rest_seconds(user_id, rest_model or model)  # a provider rests for the refusing model's own rest time
    row.cooling_until = max(moment + _cooldown(cfg, trips, rest, cause), not_before or moment)
    row.reason, row.last_changed_at, row.probe_successes, row.updated_at = reason[:300], moment, 0, moment
    row.consecutive_failures = 0
    row.stats = {**(stats.public() if stats else row.stats or {}), "trips": trips}
    where = f" for {worker_type.replace('_', ' ').title()}" if worker_type else ""
    subject = (f"Every {model[len(PROVIDER_PREFIX):]} model" if model.startswith(PROVIDER_PREFIX) else model)
    _emit(db, user_id, worker_type=worker_type, from_model=model, to_model=None, cause=cause,
          message=f"{subject} is resting{where}: {reason}", metrics=row.stats, cfg=cfg)
    reset_cache(user_id)
    return True


def recover(db: Session, user_id: int, model: str, worker_type: str, *, cfg: dict[str, Any],
            now: datetime | None = None) -> None:
    moment = now or utcnow()
    row = _row(db, user_id, worker_type, model, create=False)
    if row is None or row.state == "HEALTHY":
        return
    row.state, row.cooling_until, row.reason = "HEALTHY", None, None
    row.consecutive_failures, row.probe_successes, row.last_changed_at, row.updated_at = 0, 0, moment, moment
    row.stats = {**(row.stats or {}), "trips": 0}
    where = f" for {worker_type.replace('_', ' ').title()}" if worker_type else ""
    _emit(db, user_id, worker_type=worker_type, from_model=model, to_model=model, cause="RECOVERED",
          message=f"{model} is healthy again{where} and back in use", metrics=None, cfg=cfg)
    reset_cache(user_id)


def reset_route(db: Session, user_id: int, model: str, worker_type: str = "") -> bool:
    """A person's manual reset: the route is healthy now. Commits."""
    row = _row(db, user_id, worker_type, model, create=False)
    if row is None:
        return False
    cfg = failover_settings.for_user(db, user_id)
    recover(db, user_id, model, worker_type, cfg=cfg)
    row.consecutive_failures = 0
    db.commit()
    _flush_logs(db)
    reset_cache(user_id)
    return True


# ───────────── per-request outcomes ─────────────

def record_outcome(entry: dict[str, Any], rate_limit: Any = None, *, fallbacks_sent: bool = False) -> None:
    """Called with every LLM telemetry row (app.services.llm_telemetry.record). Never raises.

    Success on a healthy route writes nothing. A 429 rests the model overall, at least until the provider said it
    clears (`rate_limit`, app.services.rate_limits); a run of failures rests it for the worker; a success on a resting
    route that has finished cooling counts toward recovery."""
    try:
        user_id, model = entry.get("user_id"), entry.get("requested_model")
        if not user_id or not model or entry.get("provider") != "litellm":
            return
        failed = entry.get("status") == "FAILED"
        kind = entry.get("error_type")
        if failed and (kind in NOT_HEALTH or entry.get("cost_source") == "not_sent"):
            return  # a request never sent (held by a cooldown or token pacing) says nothing about the model
        worker_type = entry.get("worker_type") or ""
        known = states(user_id)
        keys = [("", model), (worker_type, model), *(("", key) for key in provider_keys(user_id, model, known))]
        if not failed and not entry.get("fallback_used") and not any(
                known.get(k) and (known[k]["state"] != "HEALTHY" or known[k]["consecutive_failures"]) for k in keys):
            return
        with _session_factory()() as db:
            cfg = failover_settings.for_user(db, user_id)
            if not cfg["enabled"]:
                return
            _apply_outcome(db, user_id, model, worker_type, failed or bool(entry.get("fallback_used")), kind, cfg,
                           rate_limit if failed else None, message=entry.get("error_message"),
                           fallbacks_sent=fallbacks_sent)
            db.commit()
            _flush_logs(db)
        reset_cache(user_id)
    except Exception:
        log.warning("could not record a model outcome", exc_info=True)


def _rate_limit_rest(limit: Any, cfg: dict[str, Any], now: datetime) -> tuple[datetime | None, str]:
    """How long a 429 keeps the model out, and why: the provider's own reset when it named one; for a usage cap it
    did not time, the account's rest after a usage limit."""
    from app.services import rate_limits

    kind = getattr(limit, "kind", rate_limits.UNKNOWN)
    until = getattr(limit, "until", None)
    label = rate_limits.LABELS.get(kind, "rate limited")
    if until:
        return until, f"the provider answered 429 ({label}); it said the limit clears at {until:%Y-%m-%d %H:%M} UTC"
    if kind == rate_limits.USAGE_CAP:
        minutes = cfg.get("usage_limit_rest_minutes") or 0
        return (now + timedelta(minutes=minutes) if minutes else None,
                f"the provider answered 429 ({label}) without saying when it resets")
    return None, f"the provider answered 429 ({label})"


def _apply_outcome(db: Session, user_id: int, model: str, worker_type: str, failed: bool, kind: str | None,
                   cfg: dict[str, Any], rate_limit: Any = None, *, message: str | None = None,
                   fallbacks_sent: bool = False) -> None:
    from app.services import rate_limits

    now = utcnow()
    if failed:
        if kind == "RATE_LIMIT":
            if getattr(rate_limit, "kind", None) == rate_limits.TOO_LARGE:
                return  # one request over the model's limit says nothing about the model's next, smaller one
            until, reason = _rate_limit_rest(rate_limit, cfg, now)
            named = rate_limits.provider_named(message)
            if named is None:  # the error does not say who refused: rest this model only
                trip(db, user_id, model, "", cause="RATE_LIMIT", reason=reason, cfg=cfg, now=now, not_before=until)
                return
            if not _rest_provider(db, user_id, model, named, reason, until, cfg, now, rate_limits.free_pool(message),
                                  fallbacks_sent):
                # The provider that refused is not one this model is known to use (an alias not yet seen refusing
                # alone): the model rests as before, and is not taught to be that provider's.
                trip(db, user_id, model, "", cause="RATE_LIMIT", reason=reason, cfg=cfg, now=now, not_before=until)
            return
        row = _row(db, user_id, worker_type, model)
        halfopen = row.state != "HEALTHY" and (row.cooling_until is None or row.cooling_until <= now)
        row.consecutive_failures += 1
        row.updated_at = now
        if halfopen or row.consecutive_failures >= cfg["consecutive_failures"]:
            trip(db, user_id, model, worker_type, cause="ERROR_RATE",
                 reason=(f"{row.consecutive_failures} failures in a row" if not halfopen else "it failed its recovery probe"),
                 cfg=cfg, now=now)
        return
    routes = [(key, model) for key in ("", worker_type)] + [("", p) for p in provider_keys(user_id, model)]
    for key, name in routes:
        row = _row(db, user_id, key, name, create=False)
        if row is None:
            continue
        row.consecutive_failures = 0
        row.updated_at = now
        if row.state != "HEALTHY" and row.cooling_until and row.cooling_until <= now:
            row.probe_successes += 1
            if row.probe_successes >= cfg["recovery_successes"]:
                recover(db, user_id, name, key, cfg=cfg, now=now)


def _rest_provider(db: Session, user_id: int, model: str, named: str, reason: str, until: datetime | None,
                   cfg: dict[str, Any], now: datetime, free_only: bool, fallbacks_sent: bool) -> bool:
    """Rest every model of the provider that refused. Returns whether `model` is covered by that rest: the provider
    is the one configured for it, or it is an alias learned (now or before) to be served by it.

    An alias is learned only from a request sent without fallbacks: with fallbacks, LiteLLM reports the last
    provider it tried, which may not be the alias's own. The app's own auto router spans providers and is never
    learned."""
    key = provider_route(named)
    configured = configured_provider(user_id, model)
    new = trip(db, user_id, key, "", cause="RATE_LIMIT", reason=f"{named} answered 429 for {model}: {reason}",
               cfg=cfg, now=now, not_before=until, rest_model=model)
    row = _row(db, user_id, "", key)
    stats = dict(row.stats or {})
    # A rest for the free quota alone widens to every model when a paid one is refused too; never narrows.
    stats["free_only"] = free_only if new else bool(stats.get("free_only", free_only)) and free_only
    members = [m for m in stats.get("members") or [] if isinstance(m, str)]
    learnable = (configured is None and not fallbacks_sent and not _model_entry(user_id, model).get("_router"))
    if learnable and model not in members:
        members = [*members, model][-MAX_MEMBERS:]
    stats["members"] = members
    row.stats = stats
    reset_cache(user_id)
    return configured == named or (configured is None and model in members)


# ───────────── the scheduled sweep ─────────────

def candidate_routes(db: Session, user_id: int, since: datetime) -> list[tuple[str, str]]:
    from app.models import LLMRequestLog

    rows = db.execute(select(LLMRequestLog.worker_type, LLMRequestLog.requested_model).where(
        LLMRequestLog.user_id == user_id, LLMRequestLog.provider == "litellm", LLMRequestLog.created_at >= since,
        LLMRequestLog.requested_model.is_not(None)).distinct().limit(500)).all()
    return [(w or "", m) for w, m in rows]


def evaluate(db: Session, user_id: int, *, now: datetime | None = None) -> dict[str, int]:
    """Judge every recently used route of one account; refresh scores, rest the breaching ones. Commits."""
    moment = now or utcnow()
    cfg = failover_settings.for_user(db, user_id)
    out = {"routes": 0, "tripped": 0}
    if not cfg["enabled"]:
        return out
    since = moment - timedelta(minutes=cfg["window_minutes"])
    for worker_type, model in candidate_routes(db, user_id, since):
        out["routes"] += 1
        stats = window_stats(db, user_id, model, worker_type, since=since)
        base = baseline_p95(db, user_id, model, worker_type, now=moment, window_minutes=cfg["window_minutes"])
        row = _row(db, user_id, worker_type, model)
        row.score, row.updated_at = score(stats, base), moment
        if row.state == "HEALTHY" or (row.cooling_until and row.cooling_until <= moment):
            row.stats = {**(row.stats or {}), **stats.public(), "baseline_p95_ms": base}
        verdict = judge(stats, cfg, base) if row.state == "HEALTHY" else None
        if verdict and trip(db, user_id, model, worker_type, cause=verdict[0], reason=verdict[1], cfg=cfg, stats=stats,
                            now=moment):
            out["tripped"] += 1
    db.commit()
    _flush_logs(db)
    reset_cache(user_id)
    return out
