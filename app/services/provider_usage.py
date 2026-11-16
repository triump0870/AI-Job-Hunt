"""Account-scoped rate-limit monitoring over recorded outbound calls and provider-reported quotas."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models import ExternalAPIRequestLog as Call
from app.services import external_api
from app.services.external_monitoring import utcnow
from app.services.redaction import redact_text
from app.services.user_settings import user_keys
from app.settings import get_settings

OPENROUTER_API = "https://openrouter.ai/api/v1"
QUOTA_FIELDS = ("limit", "requests_limit", "requests_remaining", "requests_reset_seconds", "tokens_per_minute_limit",
                "tokens_per_minute_remaining", "tokens_reset_seconds", "retry_after_seconds")


def _number(value: Any) -> int | float | None:
    return value if type(value) in (int, float) and 0 <= value <= 2**53 and math.isfinite(value) else None


def usage(db: Session, user_id: int, *, now: datetime | None = None) -> dict[str, Any]:
    """Count instrumented calls once across workers, including failures. SDK-internal retries are opaque."""
    now = now or utcnow()
    meta = Call.request_metadata
    # Older telemetry predates upstream_provider. Its day-labelled request window was specific to Groq.
    legacy_groq = case((meta["rate_limits"]["requests_window"].as_string() == "day", "GROQ"))
    upstream = func.coalesce(meta["upstream_provider"].as_string(), legacy_groq, Call.provider)
    provider = case((Call.provider == "LITELLM", upstream), else_=Call.provider).label("provider")
    model = func.coalesce(meta["answered_model"].as_string(), meta["model_group"].as_string(),
                          meta["model"].as_string()).label("model")
    scope = (Call.user_id == user_id, Call.created_at > now - timedelta(days=1), Call.created_at <= now,
             Call.provider != "MOCK", meta["skipped_by"].as_string().is_(None),
             meta["blocked_by"].as_string().is_(None), meta["not_sent"].as_boolean().is_not(True))
    minute = func.count().filter(Call.created_at > now - timedelta(minutes=1))
    limited = func.count().filter(Call.http_status_code == 429)
    counts = select(provider, model, minute, func.count(), limited).where(*scope).group_by(provider, model)
    providers: dict[str, dict[str, Any]] = {}
    for name, route, recent, daily, hits in db.execute(counts.order_by(provider, model)):
        group = providers.setdefault(name, {"provider": name, "calls_minute": 0, "calls_day": 0,
                                           "http_429_day": 0, "models": []})
        group["calls_minute"] += recent
        group["calls_day"] += daily
        group["http_429_day"] += hits
        if route:
            group["models"].append({"model": redact_text(route, 300), "calls_minute": recent, "calls_day": daily})

    limits = meta["rate_limits"]
    observed = func.coalesce(Call.completed_at, Call.created_at)
    rank = func.row_number().over(partition_by=(provider, model), order_by=(observed.desc(), Call.id.desc()))
    readings = select(provider, model, limits.label("limits"), observed.label("observed_at"),
                      Call.rate_limit_remaining, Call.rate_limit_reset_at, rank.label("rank")).where(
        *scope, or_(Call.rate_limit_remaining.is_not(None), Call.rate_limit_reset_at.is_not(None),
                    *(limits[key].as_string().is_not(None) for key in QUOTA_FIELDS))).subquery()
    snapshots = []
    for row in db.execute(select(readings).where(readings.c.rank == 1).order_by(readings.c.provider, readings.c.model)):
        fields = row.limits if isinstance(row.limits, dict) else {}
        snapshots.append({"provider": row.provider, "model": redact_text(row.model, 300), "observed_at": row.observed_at,
                          "requests_window": fields.get("requests_window") if fields.get("requests_window")
                          in ("day", "minute") else None,
                          **{key: _number(fields.get(key)) for key in QUOTA_FIELDS},
                          "remaining": row.rate_limit_remaining, "reset_at": row.rate_limit_reset_at})
    return {"as_of": now, "recording_enabled": get_settings().external_api_telemetry_enabled,
            "providers": list(providers.values()), "snapshots": snapshots}


def openrouter_quota(db: Session, user_id: int) -> dict[str, Any]:
    """Read only the signed-in account's free-model daily counter. No key, billing data or bodies leave this boundary."""
    if get_settings().openrouter_base_url.rstrip("/") != OPENROUTER_API:
        return {"status": "custom_endpoint"}
    key = user_keys(db, user_id).get("openrouter")
    if not key:
        return {"status": "no_key"}
    try:
        with external_api.external_client("OPENROUTER_ACCOUNT", service="account", operation="daily_quota",
                                          user_id=user_id, timeout=5) as client:
            response = client.get(f"{OPENROUTER_API}/key", headers={"Authorization": f"Bearer {key}"},
                                  extensions={external_api.NO_CAPTURE: True})
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError):
        return {"status": "unavailable"}
    data = body.get("data") if isinstance(body, dict) else None
    daily = data.get("free_model_daily_requests") if isinstance(data, dict) else None
    if not isinstance(daily, dict):
        return {"status": "unreported"}
    counts = {name: _number(daily.get(name)) for name in ("used", "limit", "remaining")}
    if all(value is None for value in counts.values()):
        return {"status": "unreported"}
    return {"status": "reported", "observed_at": utcnow(), **counts}
