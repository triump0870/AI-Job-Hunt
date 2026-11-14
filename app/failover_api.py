"""AI Orchestration → Failover: thresholds, route health, and the switch notifications the browser polls.

Reads and edits only this account's own failover settings and route overlay. It never touches the AI configuration
(models, tiers, versions): failover is a runtime overlay (app.services.failover). Responses carry metadata only:
model names, counts and latencies, never prompts or responses.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth import get_user_db
from app.models import FailoverEvent, ModelRouteState
from app.services import failover_settings, model_health
from app.services.model_health import PROVIDER_PREFIX, provider_name
from app.services.redaction import redact_text
from app.tenancy import require_user_id

router = APIRouter(prefix="/api/failover", tags=["failover"])


class SettingsIn(BaseModel):
    values: dict[str, Any]


class ResetIn(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    worker_type: str = Field(default="", max_length=40)


class AckIn(BaseModel):
    up_to_id: int = Field(ge=0)


def _route(row: ModelRouteState, now: datetime) -> dict[str, Any]:
    cooling = bool(row.cooling_until and row.cooling_until > now and row.state != "HEALTHY")
    stats = row.stats or {}
    return {"worker_type": row.worker_type or None, "model": row.model, "state": row.state, "resting": cooling,
            "half_open": row.state != "HEALTHY" and not cooling, "score": row.score,
            "cooling_until": row.cooling_until.isoformat() + "Z" if row.cooling_until else None,
            "reason": redact_text(row.reason or "", 300) or None, "samples": stats.get("samples"),
            "failure_pct": stats.get("failure_pct"), "rate_limits": stats.get("rate_limits"),
            "p95_ms": stats.get("p95_ms"), "baseline_p95_ms": stats.get("baseline_p95_ms"),
            "probe_successes": row.probe_successes, "trips": stats.get("trips", 0),
            # A provider-wide rest (model_health.PROVIDER_PREFIX): which provider, and the aliases seen using it.
            "provider": provider_name(row.model),
            "members": [m for m in stats.get("members") or [] if isinstance(m, str)][:50],
            "free_only": bool(stats.get("free_only")) if row.model.startswith(PROVIDER_PREFIX) else None,
            "changed_at": row.last_changed_at.isoformat() + "Z" if row.last_changed_at else None}


def _event(row: FailoverEvent) -> dict[str, Any]:
    return {"id": row.id, "worker_type": row.worker_type or None, "from_model": row.from_model,
            "to_model": row.to_model, "cause": row.cause, "algorithm": row.algorithm,
            "message": redact_text(row.message, 400), "created_at": row.created_at.isoformat() + "Z",
            "acknowledged": row.acknowledged_at is not None}


@router.get("/settings")
def get_settings_view(db: Session = Depends(get_user_db)):
    return failover_settings.describe(db, require_user_id(db))


@router.put("/settings")
def put_settings(payload: SettingsIn, db: Session = Depends(get_user_db)):
    try:
        return failover_settings.update(db, require_user_id(db), payload.values)
    except failover_settings.FailoverSettingError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/routes")
def routes(db: Session = Depends(get_user_db)):
    """Every route that has been judged or rested, worst first."""
    user_id = require_user_id(db)
    now = model_health.utcnow()
    rows = db.scalars(select(ModelRouteState).where(ModelRouteState.user_id == user_id)
                      .order_by(ModelRouteState.updated_at.desc()).limit(500)).all()
    items = sorted((_route(r, now) for r in rows), key=lambda r: (not r["resting"], not r["half_open"], r["score"] or 100))
    return {"routes": items, "resting": sum(1 for r in items if r["resting"])}


@router.post("/routes/reset")
def reset_route(payload: ResetIn, db: Session = Depends(get_user_db)):
    """Put a resting route back in service now."""
    if not model_health.reset_route(db, require_user_id(db), payload.model, payload.worker_type.upper()):
        raise HTTPException(404, "That route has no recorded health")
    return {"ok": True}


@router.get("/events")
def events(since_id: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100), db: Session = Depends(get_user_db)):
    """Switches newer than `since_id`, oldest first, for the browser to announce. `latest_id` is the newest id."""
    user_id = require_user_id(db)
    rows = db.scalars(select(FailoverEvent).where(FailoverEvent.user_id == user_id, FailoverEvent.id > since_id)
                      .order_by(FailoverEvent.id.asc()).limit(limit)).all()
    latest = db.scalar(select(FailoverEvent.id).where(FailoverEvent.user_id == user_id)
                       .order_by(FailoverEvent.id.desc()).limit(1)) or 0
    notify = failover_settings.for_user(db, user_id)["notify"]
    return {"events": [_event(r) for r in rows], "latest_id": latest, "notify": notify}


@router.post("/events/ack")
def acknowledge(payload: AckIn, db: Session = Depends(get_user_db)):
    user_id = require_user_id(db)
    db.execute(update(FailoverEvent).where(FailoverEvent.user_id == user_id, FailoverEvent.id <= payload.up_to_id,
                                           FailoverEvent.acknowledged_at.is_(None))
               .values(acknowledged_at=datetime.now(UTC).replace(tzinfo=None)))
    db.commit()
    return {"ok": True}
