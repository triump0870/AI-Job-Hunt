"""AI Orchestration → Models: the HTTP API over the AI configuration service (app.services.ai_model_config).

Edits change the draft (desired configuration) only; POST /apply makes the draft the active configuration, and
workers pick it up on their next request. Every write that changes the draft accepts the `revision` the client
last saw and answers 409 when someone else changed the draft since, so concurrent edits never overwrite each other
silently. No response ever contains the LiteLLM API key or any other credential.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import CurrentUser, current_user, get_user_db
from app.security import SecretsUnavailable
from app.services import ai_model_config as config
from app.services import ai_registry
from app.services.external_api import RateLimitedCooldown
from app.services.litellm_client import ollama_tags
from app.services.user_settings import lock_settings_row, settings_for
from app.settings import get_settings
from app.tenancy import require_user_id

router = APIRouter(prefix="/api/settings/ai", tags=["ai models"])


class Revisioned(BaseModel):
    revision: int | None = Field(default=None, description="The draft revision this edit is based on")


class ConnectionIn(BaseModel):
    base_url: str | None = None
    api_key: str | None = Field(default=None, description="Write-only. Omitted = unchanged, empty = removed")
    sync_mode: str | None = None


class GatewayIn(BaseModel):
    enabled: bool


class WorkerIn(Revisioned):
    model_mode: str | None = None
    primary_model: str | None = None
    tier_name: str | None = None
    fallback_model: str | None = None
    secondary_models: list[str] | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    context_window: int | None = None
    prompt_token_budget: int | None = None
    reasoning_effort: str | None = None
    timeout_seconds: int | None = None
    retry_count: int | None = None
    max_concurrency: int | None = None
    use_structured_output: bool | None = None
    enabled: bool | None = None
    escalation: dict[str, Any] | None = None


class RoutingIn(Revisioned):
    auto_router_enabled: bool | None = None
    auto_router_model_name: str | None = None
    manage_auto_router: bool | None = None
    classifier: str | None = None
    classifier_model: str | None = None
    classifier_plugin: str | None = None
    tier_models: dict[str, str | None] | None = None
    default_model: str | None = None
    send_metadata: bool | None = None


class TierIn(Revisioned):
    litellm_model_name: str | None = None
    description: str | None = None
    enabled: bool | None = None


class ManagedModelIn(Revisioned):
    litellm_model_name: str
    display_name: str | None = None
    model_type: str | None = None
    ollama_model: str | None = None
    api_base: str | None = None
    num_ctx: int | None = None
    keep_alive: str | None = None
    deployment_params: dict[str, Any] | None = None


class ModelIn(Revisioned):
    display_name: str | None = None
    enabled: bool | None = None
    model_type: str | None = None
    overrides: dict[str, Any] | None = None
    num_ctx: int | None = None
    keep_alive: str | None = None
    deployment_params: dict[str, Any] | None = None
    fallback_models: list[str] | None = None
    rest_seconds: int | None = None


class ApplyIn(BaseModel):
    revision: int | None = Field(default=None, description="The draft revision that was previewed")
    base_version: int | None = Field(default=None, description="The active version the preview was based on")


class RestoreIn(BaseModel):
    base_version: int | None = None


class WorkerTestIn(BaseModel):
    configuration: str = Field(default="draft", pattern="^(draft|active)$")


def _call(fn, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except config.AIConfigError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except SecretsUnavailable as exc:
        raise HTTPException(400, f"{exc}. Ask the server admin to set SECRET_KEY.") from exc
    except RateLimitedCooldown as exc:
        raise HTTPException(429, f"{exc}. Try again in a moment.") from exc


def _set(payload: BaseModel) -> dict[str, Any]:
    """Only the fields the client sent (explicit nulls clear a value)."""
    return {k: v for k, v in payload.model_dump(exclude_unset=True).items() if k != "revision"}


@router.get("")
def ai_overview(db: Session = Depends(get_user_db)):
    return _call(config.overview, db, require_user_id(db))


@router.get("/insights")
def ai_insights(db: Session = Depends(get_user_db)):
    """Per worker: context used, the models that did best, and models worth trying, each with the change that
    switches to it. Separate from the overview so the Workers tab paints first; read-only."""
    from app.services import worker_insights

    user_id = require_user_id(db)
    return worker_insights.report(db, user_id, config.draft_snapshot(db, user_id))


# ───────────── LiteLLM connection and gateway mode ─────────────

@router.put("/connection")
def connection_put(payload: ConnectionIn, db: Session = Depends(get_user_db)):
    user_id = require_user_id(db)
    conn = _call(config.update_connection, db, user_id, base=payload.base_url, key=payload.api_key,
                 mode=payload.sync_mode)
    return config.connection_public(db, conn)


@router.post("/test-connection")
def connection_test(db: Session = Depends(get_user_db)):
    return _call(config.test_connection, db, require_user_id(db))


@router.get("/gateway")
def gateway_get(db: Session = Depends(get_user_db)):
    """The route every worker's requests take now (LiteLLM or the direct provider), and whether each is ready."""
    return config.gateway_status(db, require_user_id(db))


@router.put("/gateway")
def gateway_put(payload: GatewayIn, db: Session = Depends(get_user_db)):
    """Turn LiteLLM gateway mode on or off: which path every worker's requests take."""
    user_id = require_user_id(db)
    if payload.enabled and not config.gateway_settings(db, user_id)[0]:
        raise HTTPException(422, "Save the LiteLLM base URL before turning gateway mode on")
    row = lock_settings_row(db, settings_for(db))
    preferences = dict(row.preferences or {})
    ai = dict(preferences.get("ai") or {})
    current = ai.get("llm_provider") or ""
    if payload.enabled:
        if current != "litellm":
            ai["previous_provider"] = current
        ai["llm_provider"] = "litellm"
    else:
        previous = ai.pop("previous_provider", "")
        ai["llm_provider"] = previous if previous != "litellm" else ""
        if not ai["llm_provider"] and (get_settings().llm_provider or "").lower() == "litellm":
            ai["llm_provider"] = "openrouter"  # the server default is the gateway; off must mean off
    row.preferences = {**preferences, "ai": ai}
    config.audit(db, user_id, "GATEWAY", actor=config._actor(db, user_id),
                 changes=[{"path": "gateway", "before": current == "litellm", "after": payload.enabled}])
    db.commit()
    return {"gateway_enabled": payload.enabled, "llm_provider": ai["llm_provider"] or get_settings().llm_provider,
            **config.gateway_status(db, user_id)}


# ───────────── models ─────────────

@router.get("/models")
def models_list(db: Session = Depends(get_user_db)):
    return {"models": _call(config.overview, db, require_user_id(db))["models"]}


@router.post("/models/sync")
def models_sync(db: Session = Depends(get_user_db)):
    return _call(ai_registry.discover, db, require_user_id(db))


@router.get("/ollama")
def ollama_models(user: CurrentUser = Depends(current_user)):
    """Models on the server's own Ollama (OLLAMA_BASE_URL), for the add-a-local-model form."""
    base = get_settings().ollama_base_url
    tags = ollama_tags(base, user_id=user.id)
    return {"api_base": base, "models": [{"name": name, **facts} for name, facts in sorted(tags.items())],
            "reachable": bool(tags)}


@router.post("/models", status_code=201)
def models_add(payload: ManagedModelIn, db: Session = Depends(get_user_db)):
    data = payload.model_dump(exclude_unset=True)
    data.pop("revision", None)
    return _call(config.add_managed_model, db, require_user_id(db), data, payload.revision)


@router.put("/models/{name:path}")
def models_update(name: str, payload: ModelIn, db: Session = Depends(get_user_db)):
    return _call(config.update_model, db, require_user_id(db), name, _set(payload), payload.revision)


@router.delete("/models/{name:path}")
def models_unmanage(name: str, revision: int | None = None, db: Session = Depends(get_user_db)):
    return _call(config.remove_managed_model, db, require_user_id(db), name, revision)


@router.post("/models/{name:path}/remove")
def models_remove(name: str, payload: Revisioned | None = None, db: Session = Depends(get_user_db)):
    """Remove a model from Job Hunt OS (409 with `references` while anything uses it)."""
    return _call(config.remove_model, db, require_user_id(db), name, (payload or Revisioned()).revision)


@router.post("/models/{name:path}/restore")
def models_restore(name: str, payload: Revisioned | None = None, db: Session = Depends(get_user_db)):
    return _call(config.restore_model, db, require_user_id(db), name, (payload or Revisioned()).revision)


@router.post("/models/{name:path}/test")
def models_test(name: str, db: Session = Depends(get_user_db)):
    return _call(ai_registry.test_model, db, require_user_id(db), name)


# ───────────── workers, tiers, routing ─────────────

@router.get("/workers")
def workers_list(db: Session = Depends(get_user_db)):
    return {"workers": _call(config.overview, db, require_user_id(db))["workers"]}


@router.put("/workers/{worker_type}")
def workers_update(worker_type: str, payload: WorkerIn, db: Session = Depends(get_user_db)):
    return _call(config.update_worker, db, require_user_id(db), worker_type.upper(), _set(payload), payload.revision)


@router.post("/workers/{worker_type}/test")
def workers_test(worker_type: str, payload: WorkerTestIn | None = None, db: Session = Depends(get_user_db)):
    use_draft = (payload or WorkerTestIn()).configuration == "draft"
    return _call(ai_registry.test_worker, db, require_user_id(db), worker_type.upper(),
                                   use_draft=use_draft)


@router.get("/tiers")
def tiers_list(db: Session = Depends(get_user_db)):
    return {"tiers": _call(config.overview, db, require_user_id(db))["tiers"]}


@router.put("/tiers/{name}")
def tiers_upsert(name: str, payload: TierIn, db: Session = Depends(get_user_db)):
    return _call(config.upsert_tier, db, require_user_id(db), name, _set(payload), payload.revision)


@router.delete("/tiers/{name}")
def tiers_delete(name: str, revision: int | None = None, db: Session = Depends(get_user_db)):
    return _call(config.delete_tier, db, require_user_id(db), name.upper(), revision)


@router.get("/routing")
def routing_get(db: Session = Depends(get_user_db)):
    return {"routing": _call(config.overview, db, require_user_id(db))["routing"]}


@router.put("/routing")
def routing_put(payload: RoutingIn, db: Session = Depends(get_user_db)):
    return _call(config.update_routing, db, require_user_id(db), _set(payload), payload.revision)


# ───────────── preview, apply, verify, history ─────────────

@router.post("/preview")
def preview(db: Session = Depends(get_user_db)):
    return _call(config.preview, db, require_user_id(db))


@router.post("/apply")
def apply(payload: ApplyIn, db: Session = Depends(get_user_db)):
    return _call(config.apply, db, require_user_id(db),
                                   expected_revision=payload.revision, expected_version=payload.base_version)


@router.post("/discard")
def discard(db: Session = Depends(get_user_db)):
    """Put the draft back to the active version (the screen calls it when an immediate save could not be applied)."""
    return _call(config.discard_draft, db, require_user_id(db))


@router.post("/verify")
def verify(db: Session = Depends(get_user_db)):
    return _call(config.verify, db, require_user_id(db))


@router.get("/runtime")
def runtime(db: Session = Depends(get_user_db)):
    """Desired against active against what LiteLLM serves right now (one read-only LiteLLM call)."""
    user_id = require_user_id(db)
    live = config.load_live(db, user_id)
    return {**_call(config.runtime_view, db, user_id, live), "live_error": live.error}


@router.get("/versions")
def versions(db: Session = Depends(get_user_db)):
    return {"versions": _call(config.versions, db, require_user_id(db))}


@router.get("/versions/{version_id}")
def version_detail(version_id: int, db: Session = Depends(get_user_db)):
    from sqlalchemy import select

    from app.models import AIConfigVersion

    row = db.scalar(select(AIConfigVersion).where(AIConfigVersion.id == version_id,
                                                  AIConfigVersion.user_id == require_user_id(db)))
    if row is None:
        raise HTTPException(404, "That configuration version does not exist")
    return config.version_dict(row, include_snapshot=True)


@router.post("/versions/{version_id}/restore")
def version_restore(version_id: int, payload: RestoreIn | None = None, db: Session = Depends(get_user_db)):
    return _call(config.restore, db, require_user_id(db), version_id,
                                   expected_version=(payload or RestoreIn()).base_version)


@router.get("/audit")
def audit_list(db: Session = Depends(get_user_db)):
    return {"entries": config.audit_entries(db, require_user_id(db))}
