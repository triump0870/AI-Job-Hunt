"""The AI configuration service: the one way an account's AI model configuration changes.

    AI Orchestration → Models: edit ──► draft rows (the desired configuration, PostgreSQL)
                                     │
    Preview ──► diff(active version, draft) + validation + the LiteLLM sync plan
                                     │
    Apply ────► validate ─► LiteLLMConfigManager (only what lives inside LiteLLM) ─► verify
                                     │ success                         │ failure
                                     ▼                                 ▼
                     AIConfigVersion N+1 (frozen snapshot)     nothing changes: LiteLLM restored,
                     cache invalidated → next request uses it  version N stays active, audit says why
    Roll back ──► an old snapshot becomes the draft and is applied as a *new* version (history is immutable)

Workers never read the draft; app.services.model_resolution reads only the latest version. Every attempt to
change the configuration — including rejected and failed ones — is written to the audit trail. Snapshots,
audit entries and API responses never contain a credential.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AI_MODEL_TYPES,
    AUTO_ROUTER_CLASSIFIERS,
    LITELLM_SYNC_MODES,
    REASONING_EFFORTS,
    WORKER_MODEL_MODES,
    AIConfigAuditEntry,
    AIConfigVersion,
    AIModel,
    AIRoutingConfig,
    LiteLLMConnection,
    ModelTier,
    User,
    WorkerModelConfig,
)
from app.security import decrypt_secret, encrypt_secret
from app.services import model_capacity, model_resolution
from app.services.ai_workers import (
    AUTO_ROUTER_TIERS,
    DEFAULT_TIERS,
    ESCALATION_CONDITIONS,
    WORKERS,
    label,
)
from app.services.litellm_client import LiteLLMAPI, LiteLLMError, normalize_base_url
from app.services.litellm_config_manager import LiteLLMConfigManager, literal_secrets, render
from app.services.prompt_budget import TRIMMED_WORKERS
from app.services.redaction import redact_text, redact_value
from app.settings import get_settings

log = logging.getLogger("jobhunt.ai_model_config")

SNAPSHOT_SCHEMA = 1
APPLY_LEASE = timedelta(minutes=10)
TIER_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,59}$")
MODEL_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:/@+\- ]{0,198}[A-Za-z0-9._:/@+\-])?$")  # LiteLLM aliases may hold spaces
PLUGIN_PATH = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")
LIMITS = {"temperature": (0.0, 2.0), "max_output_tokens": (1, 200_000), "context_window": (512, 2_000_000),
          "prompt_token_budget": (256, 2_000_000), "timeout_seconds": (1, 3600), "retry_count": (0, 10), "max_concurrency": (1, 100),
          "rest_seconds": (10, 604_800), "embedding_dimensions": (1, 65_536)}
MAX_MODEL_FALLBACKS = 4
WORKER_FIELDS = ("model_mode", "primary_model", "tier_name", "fallback_model", "secondary_models", "temperature", "max_output_tokens",
                 "context_window", "prompt_token_budget", "reasoning_effort", "timeout_seconds", "retry_count",
                 "max_concurrency", "use_structured_output", "enabled", "escalation")
ROUTING_FIELDS = ("auto_router_enabled", "auto_router_model_name", "manage_auto_router", "classifier",
                  "classifier_model", "classifier_plugin", "tier_models", "default_model", "send_metadata")
CAPABILITIES = ("supports_structured_output", "supports_tools", "supports_reasoning", "supports_vision")
OVERRIDABLE = (*CAPABILITIES, "context_window", "max_output_tokens", "embedding_dimensions")
FIELD_LABELS = {"mode": "mode", "primary_model": "model", "tier": "tier", "fallback_model": "fallback model", "secondary_models": "secondary models",
                "fallback_models": "model fallbacks", "rest_seconds": "rest time",
                "temperature": "temperature", "max_output_tokens": "max output tokens",
                "context_window": "context window", "prompt_token_budget": "prompt token budget",
                "reasoning_effort": "reasoning effort",
                "timeout_seconds": "timeout", "retry_count": "retries", "max_concurrency": "max concurrency",
                "use_structured_output": "structured output", "enabled": "enabled", "escalation": "escalation",
                "model": "model", "description": "description", "auto_router_enabled": "Auto Router",
                "auto_router_model_name": "router model name", "manage_auto_router": "managed by Job Hunt OS",
                "classifier": "classifier", "classifier_model": "classifier model",
                "classifier_plugin": "classifier plugin", "tier_models": "tier models",
                "default_model": "default model", "send_metadata": "request metadata",
                "deployment_params": "deployment", "managed": "managed", "model_type": "type",
                "display_name": "name", "overrides": "overrides"}


class AIConfigError(Exception):
    status_code = 422

    def __init__(self, message: str, **detail: Any):
        super().__init__(message)
        self.detail = {"message": message, **detail}


class NotFound(AIConfigError):
    status_code = 404


class ConfigConflict(AIConfigError):
    status_code = 409


class ConfigBusy(AIConfigError):
    status_code = 409


class ValidationFailed(AIConfigError):
    status_code = 422


class ApplyFailed(AIConfigError):
    status_code = 502


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _actor(db: Session, user_id: int) -> str:
    return db.scalar(select(User.email).where(User.id == user_id)) or f"user:{user_id}"


def audit(db: Session, user_id: int, action: str, *, result: str = "SUCCEEDED", source: str = "USER",
          actor: str = "", version_number: int | None = None, changes: list[Any] | None = None,
          sync_result: dict[str, Any] | None = None, error: str | None = None) -> None:
    """Add an audit entry to the session (the caller commits). Everything stored is redacted."""
    db.add(AIConfigAuditEntry(user_id=user_id, action=action, result=result, source=source, actor=actor[:320],
                              version_number=version_number, changes=redact_value(changes) if changes else None,
                              sync_result=redact_value(sync_result) if sync_result else None,
                              error=redact_text(error, 1000), created_at=utcnow()))


# ───────────── the LiteLLM connection ─────────────

def connection_for(db: Session, user_id: int) -> LiteLLMConnection:
    row = db.scalar(select(LiteLLMConnection).where(LiteLLMConnection.user_id == user_id))
    if row is not None:
        return row
    mode = get_settings().litellm_sync_mode or "disabled"
    try:
        # The server's LITELLM_SYNC_MODE is the account's starting point; from then on the saved value wins.
        row = LiteLLMConnection(user_id=user_id, sync_mode=mode if mode in LITELLM_SYNC_MODES else "disabled",
                                created_at=utcnow(), updated_at=utcnow())
        db.add(row)
        db.commit()
    except IntegrityError:  # created by a concurrent request
        db.rollback()
        row = db.scalar(select(LiteLLMConnection).where(LiteLLMConnection.user_id == user_id))
        if row is None:
            raise
    return row


def _first_account(db: Session) -> int | None:
    return db.scalar(select(func.min(User.id)))


def base_url(conn: LiteLLMConnection) -> str | None:
    """The account's saved base URL, else LITELLM_BASE_URL."""
    return normalize_base_url(conn.base_url) or normalize_base_url(get_settings().litellm_base_url)


def api_key(db: Session, conn: LiteLLMConnection) -> str | None:
    """The account's saved key; LITELLM_API_KEY only for the first account, like the other .env keys."""
    saved = decrypt_secret(conn.api_key_enc)
    if saved:
        return saved
    env = get_settings().litellm_api_key
    return env if env and conn.user_id == _first_account(db) else None


def sync_mode(conn: LiteLLMConnection) -> str:
    """The account's sync mode. config_file only counts while the operator's managed path is configured."""
    mode = conn.sync_mode if conn.sync_mode in LITELLM_SYNC_MODES else "disabled"
    if mode == "config_file" and not get_settings().litellm_managed_config_path:
        return "disabled"
    return mode


def gateway_settings(db: Session, user_id: int) -> tuple[str | None, str | None]:
    """(base URL, API key) the LLM layer sends requests with. Read-only: it runs inside workers' transactions, so it
    must never create a row (and commit their pending work) the way connection_for does."""
    conn = db.scalar(select(LiteLLMConnection).where(LiteLLMConnection.user_id == user_id))
    if conn is not None:
        return base_url(conn), api_key(db, conn)
    settings = get_settings()
    key = settings.litellm_api_key if settings.litellm_api_key and user_id == _first_account(db) else None
    return normalize_base_url(settings.litellm_base_url), key


def gateway_status(db: Session, user_id: int) -> dict[str, Any]:
    """Which way workers send requests now, and what each way needs, for the Overview switch. Read-only.

    There are two routes: the LiteLLM gateway, or the one direct provider chosen in Settings (with its model).
    `direct_provider` is the one turning the gateway off returns to (app.ai_models_api.gateway_put)."""
    from app.services.ai_config import resolve as resolve_ai_config
    from app.services.user_settings import KEY_LABELS, ai_overrides, user_keys

    overrides = ai_overrides(db, user_id)
    current = resolve_ai_config(get_settings(), overrides)["llm_provider"]
    gateway = current == "litellm"
    if gateway:
        direct = (overrides.get("previous_provider") or "").strip().lower()
        if not direct or direct == "litellm":
            env = (get_settings().llm_provider or "").lower()
            direct = env if env and env != "litellm" else "openrouter"
    else:
        direct = current
    direct_model = resolve_ai_config(get_settings(), {**overrides, "llm_provider": direct})["model"]
    base, _key = gateway_settings(db, user_id)
    conn = db.scalar(select(LiteLLMConnection).where(LiteLLMConnection.user_id == user_id))
    return {"gateway_enabled": gateway,
            "direct_provider": direct, "direct_label": KEY_LABELS.get(direct, direct.title()),
            "direct_model": direct_model, "direct_ready": direct == "mock" or bool(user_keys(db, user_id).get(direct)),
            "litellm_ready": bool(base), "litellm_base_url": base or "",
            "litellm_status": conn.status if conn else None,
            "litellm_version": conn.litellm_version if conn else None}


def connection_public(db: Session, conn: LiteLLMConnection) -> dict[str, Any]:
    key = api_key(db, conn)
    settings = get_settings()
    return {"base_url": base_url(conn), "base_url_saved": conn.base_url or "",
            "base_url_env": normalize_base_url(settings.litellm_base_url),
            "api_key_configured": bool(key), "api_key_hint": f"…{key[-4:]}" if key and len(key) >= 8 else "",
            "api_key_source": "settings" if conn.api_key_enc else ("env" if key else None),
            "sync_mode": sync_mode(conn), "sync_modes": [m for m in LITELLM_SYNC_MODES
                                                         if m != "config_file" or settings.litellm_managed_config_path],
            "managed_config_path": settings.litellm_managed_config_path,
            "restart_command_configured": bool(settings.litellm_restart_command),
            "status": conn.status, "litellm_version": conn.litellm_version,
            "last_checked_at": _iso(conn.last_checked_at), "last_latency_ms": conn.last_latency_ms,
            "last_error": conn.last_error, "last_successful_sync_at": _iso(conn.last_discovery_at),
            "sync_status": conn.sync_status, "last_sync_at": _iso(conn.last_sync_at),
            "last_sync_error": conn.last_sync_error, "draft_revision": conn.draft_revision,
            "secret_storage": bool(settings.secret_key)}


def update_connection(db: Session, user_id: int, *, base: str | None = None, key: str | None = None,
                      mode: str | None = None) -> LiteLLMConnection:
    """Save connection settings. None leaves a value unchanged; "" clears it (and falls back to .env)."""
    conn = _locked_connection(db, user_id)
    if base is not None:
        clean = base.strip()
        if clean and normalize_base_url(clean) is None:
            raise AIConfigError("The LiteLLM base URL must be an http(s) URL")
        conn.base_url = normalize_base_url(clean) if clean else None
        conn.status = "UNKNOWN"
    if key is not None:
        conn.api_key_enc = encrypt_secret(key.strip()) if key.strip() else None
        conn.status = "UNKNOWN"
    if mode is not None:
        if mode not in LITELLM_SYNC_MODES:
            raise AIConfigError(f"Unknown sync mode {mode}")
        if mode == "config_file" and not get_settings().litellm_managed_config_path:
            raise AIConfigError("config_file mode needs LITELLM_MANAGED_CONFIG_PATH on the server")
        conn.sync_mode = mode
    conn.updated_at = utcnow()
    audit(db, user_id, "CONNECTION", actor=_actor(db, user_id),
          changes=[{"path": "connection", "fields": [n for n, v in (("base_url", base), ("api_key", key),
                                                                     ("sync_mode", mode)) if v is not None]}])
    db.commit()
    return conn


def api_for(db: Session, user_id: int, service: str = "admin") -> LiteLLMAPI:
    """A LiteLLM API for the account. Ends the session's transaction first: no transaction is held across the
    network calls that follow (SQLite has a single writer, and their telemetry is written from another session)."""
    conn = connection_for(db, user_id)
    url, key = base_url(conn), api_key(db, conn)
    db.commit()
    if not url:
        raise AIConfigError("Add the LiteLLM base URL first (AI Orchestration → Models → LiteLLM connection)")
    return LiteLLMAPI(url, key, user_id=user_id, service=service)


def test_connection(db: Session, user_id: int) -> dict[str, Any]:
    """Reachability, authentication, model discovery and latency. Makes no model call."""
    conn = connection_for(db, user_id)
    report: dict[str, Any] = {"reachable": False, "authenticated": False, "discovery": False, "latency_ms": None,
                              "models": 0, "litellm_version": None, "error": None}
    started = utcnow()
    try:
        with api_for(db, user_id) as api:
            clock = time.monotonic()
            ready = api.readiness()
            report["latency_ms"] = int((time.monotonic() - clock) * 1000)
            report["reachable"] = True
            report["litellm_version"] = ready.get("litellm_version") if isinstance(ready, dict) else None
            names = api.model_names()
            report["authenticated"] = True
            report["models"] = len(names)
            api.model_info()
            report["discovery"] = True
        status = "CONNECTED"
    except AIConfigError as exc:
        status, report["error"] = "DISCONNECTED", str(exc)
    except LiteLLMError as exc:
        status = {"AUTH_FAILED": "AUTH_FAILED", "UNAVAILABLE": "UNAVAILABLE"}.get(exc.status, "DISCONNECTED")
        if report["authenticated"]:  # discovery alone failed
            status = "CONNECTED"
        report["error"] = str(exc)
    conn = _locked_connection(db, user_id)
    conn.status, conn.last_checked_at, conn.last_latency_ms = status, started, report["latency_ms"]
    conn.litellm_version = report["litellm_version"] or conn.litellm_version
    conn.last_error = redact_text(report["error"], 1000)
    db.commit()
    return {**report, "status": status, "label": {"CONNECTED": "Connected", "AUTH_FAILED": "Authentication failed",
                                                  "UNAVAILABLE": "LiteLLM unavailable"}.get(status, "Disconnected")}


def _locked_connection(db: Session, user_id: int) -> LiteLLMConnection:
    conn = connection_for(db, user_id)
    if db.get_bind().dialect.name == "sqlite":
        db.execute(update(LiteLLMConnection).where(LiteLLMConnection.id == conn.id).values(id=conn.id))
    return db.scalar(select(LiteLLMConnection).where(LiteLLMConnection.id == conn.id).with_for_update()
                     .execution_options(populate_existing=True))


# ───────────── seeding (the migration from .env and saved stage models) ─────────────

def ensure_seeded(db: Session, user_id: int) -> bool:
    """Create the account's AI configuration once, from its existing settings. True when this call created it.

    Nothing an install relies on is discarded: stage models saved in Settings and RANK_MODEL / TAILOR_MODEL /
    REVIEW_MODEL / FOLLOWUP_MODEL keep working in direct-provider mode exactly as before, and in LiteLLM gateway
    mode they become the matching workers' FIXED assignments. The result is recorded as version 1 (MIGRATION).
    """
    if db.scalar(select(AIConfigVersion.id).where(AIConfigVersion.user_id == user_id).limit(1)) is not None:
        return False
    from app.services.ai_config import resolve as resolve_ai_config
    from app.services.user_settings import ai_overrides

    settings = get_settings()
    connection_for(db, user_id)  # commits on creation, so it must not happen halfway through the seed
    try:
        imported: list[dict[str, Any]] = []
        tiers = {}
        for name, description, env_name in DEFAULT_TIERS:
            model = (getattr(settings, env_name, None) or "").strip() or None
            tiers[name] = model
            if db.scalar(select(ModelTier.id).where(ModelTier.user_id == user_id, ModelTier.name == name)) is None:
                db.add(ModelTier(user_id=user_id, name=name, description=description, litellm_model_name=model,
                                 enabled=True, created_at=utcnow(), updated_at=utcnow()))
            if model:
                imported.append({"path": f"tiers.{name}.model", "after": model, "from": env_name.upper()})
        router = (settings.litellm_auto_router_model or "").strip() or None
        if db.scalar(select(AIRoutingConfig.id).where(AIRoutingConfig.user_id == user_id)) is None:
            # A router named in .env already exists in LiteLLM: route to it, but do not manage it.
            db.add(AIRoutingConfig(user_id=user_id, auto_router_enabled=bool(router),
                                   auto_router_model_name=router or "jobhunt-auto", manage_auto_router=not router,
                                   classifier="HEURISTIC", tier_models={t: None for t in AUTO_ROUTER_TIERS},
                                   send_metadata=True, created_at=utcnow(), updated_at=utcnow()))
        overrides = ai_overrides(db, user_id)
        effective = resolve_ai_config(settings, overrides)
        gateway = effective["llm_provider"] == "litellm"
        saved_stages = overrides.get("stage_models") or {}
        env_stages = {"rank": settings.rank_model, "tailor": settings.tailor_model, "review": settings.review_model,
                      "followup": settings.followup_model}
        referenced = {m for m in [*tiers.values(), router] if m}
        existing = set(db.scalars(select(WorkerModelConfig.worker_type)
                                  .where(WorkerModelConfig.user_id == user_id)).all())
        for worker in WORKERS.values():
            if worker.worker_type in existing:
                continue
            legacy = None
            if worker.legacy_stage:
                legacy = (saved_stages.get(worker.legacy_stage) or "").strip() or \
                         (env_stages.get(worker.legacy_stage) or "").strip() or None
            if legacy and gateway:
                mode, primary = "FIXED", legacy
                referenced.add(legacy)
                imported.append({"path": f"workers.{worker.worker_type}.primary_model", "after": legacy,
                                 "from": "saved stage model" if saved_stages.get(worker.legacy_stage)
                                 else f"{worker.legacy_stage.upper()}_MODEL"})
            else:
                mode, primary = worker.default_mode, None
            db.add(WorkerModelConfig(user_id=user_id, worker_type=worker.worker_type, model_mode=mode,
                                     primary_model=primary, tier_name=None if worker.optional else worker.default_tier,
                                     use_structured_output=worker.capability == "CHAT",
                                     enabled=not worker.optional, created_at=utcnow(), updated_at=utcnow()))
        known = set(db.scalars(select(AIModel.litellm_model_name).where(AIModel.user_id == user_id)).all())
        for name in sorted(referenced - known):
            db.add(AIModel(user_id=user_id, display_name=name, litellm_model_name=name, source="SEED",
                           model_type="AUTO_ROUTER" if name == router else "CUSTOM",
                           created_at=utcnow(), updated_at=utcnow()))
        db.flush()
        snapshot = draft_snapshot(db, user_id)
        summary = "Initial AI model configuration" + (
            f" imported from existing settings: {', '.join(i['path'] + '=' + str(i['after']) for i in imported)}"
            if imported else " from defaults")
        db.add(AIConfigVersion(user_id=user_id, version_number=1, configuration_snapshot=snapshot,
                               changed_by="system", source="MIGRATION", change_summary=summary[:2000],
                               changes=imported, sync_result={"status": "SYNCED", "mode": "none",
                                                               "message": "Seeded; nothing applied to LiteLLM"},
                               created_at=utcnow()))
        audit(db, user_id, "SEED", source="MIGRATION", actor="system", version_number=1, changes=imported)
        db.commit()
    except IntegrityError:  # another request seeded this account at the same time
        db.rollback()
        return False
    return True


# ───────────── snapshots ─────────────

def _effective(row: AIModel, name: str) -> Any:
    overrides = row.overrides or {}
    return overrides[name] if overrides.get(name) is not None else getattr(row, name)


def safe_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Deployment params fit to store and show: `os.environ/NAME` references are kept (they are how LiteLLM finds a
    credential, not the credential), anything else in a credential field is redacted."""
    if params is None:
        return None
    out = {}
    for key, value in params.items():
        if isinstance(value, str) and value.startswith("os.environ/"):
            out[key] = value
        elif literal_secrets({key: value}):
            out[key] = "[redacted]"
        else:
            out[key] = redact_value(value)
    return out


# Engines that run on the person's own machine, and OpenAI-compatible routes that may point anywhere.
LOCAL_ENGINES = frozenset({"ollama", "ollama_chat", "lm_studio", "llamafile", "vllm"})
SELF_HOSTABLE_ROUTES = frozenset({"openai_like", "custom_openai", "hosted_vllm"})
# Ollama names its hosted models "<name>:cloud" or "<name>:<size>-cloud", even when the local daemon relays them.
_OLLAMA_CLOUD_NAME = re.compile(r"[:-]cloud(?:$|-)", re.IGNORECASE)


def _api_base_hosting(api_base: str) -> str | None:
    """LOCAL or CLOUD from a deployment's address; None when it cannot be read, since this label must never raise."""
    try:
        host = (urlsplit(api_base if "//" in api_base else f"//{api_base}").hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    if host == "localhost" or host.endswith((".localhost", ".local")) or host == "host.docker.internal":
        return "LOCAL"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "CLOUD"
    return "LOCAL" if address.is_loopback or address.is_private else "CLOUD"


def model_hosting(row: AIModel) -> str | None:
    """Where one model runs: LOCAL, CLOUD, or None when that is not known.

    Derived for display, never stored. A router runs nowhere itself (see catalog_hosting), and a model with no reported
    provider stays unknown.
    """
    provider = (row.provider or "").lower()
    if provider == "litellm" or row.model_type == "AUTO_ROUTER":
        return None
    # The real model name, never the alias: a person can call any model "local-cloud". The suffix is Ollama's naming,
    # so it says nothing about another engine, but it does settle a model whose provider nobody reported.
    if (not provider or provider.startswith("ollama")) and _OLLAMA_CLOUD_NAME.search(row.underlying_model or ""):
        return "CLOUD"
    api_base = str((row.deployment_params or {}).get("api_base") or "") if row.managed else ""
    if api_base:
        return _api_base_hosting(api_base)
    if not provider or provider in SELF_HOSTABLE_ROUTES:
        return None
    return "LOCAL" if provider in LOCAL_ENGINES else "CLOUD"


def _unlisted_hosting(name: str) -> str | None:
    """A router member with no catalog row, such as a classifier: "groq/x" names its provider, a bare name does not."""
    provider, _, underlying = name.partition("/") if "/" in name else ("", "", name)
    return model_hosting(AIModel(litellm_model_name=name, provider=provider or None, underlying_model=underlying,
                                 model_type="CUSTOM", managed=False))


def _router_hosting(members: list[str], known: dict[str, str | None]) -> str | None:
    """LOCAL or CLOUD only when every member is known to be; MIXED as soon as both kinds are known to be in there."""
    kinds = {known[name] if name in known else _unlisted_hosting(name) for name in members}
    if {"LOCAL", "CLOUD"} <= kinds:
        return "MIXED"
    return next(iter(kinds)) if len(kinds) == 1 else None


def catalog_hosting(rows: list[AIModel]) -> dict[str, str | None]:
    """Where every model in a catalog runs, with each router judged by the models it routes to.

    Pass removed models too: LiteLLM can still route to a model that was removed from Job Hunt OS.
    """
    own = {row.litellm_model_name: model_hosting(row) for row in rows}
    return {row.litellm_model_name: _router_hosting(row.router_models, own) if row.router_models
            else own[row.litellm_model_name] for row in rows}


def model_snapshot(row: AIModel) -> dict[str, Any]:
    return {"display_name": row.display_name or row.litellm_model_name, "enabled": row.enabled,
            "model_type": row.model_type, "provider": row.provider, "underlying_model": row.underlying_model,
            **{name: _effective(row, name) for name in OVERRIDABLE},
            "max_context_window": row.max_context_window, "overrides": dict(row.overrides or {}),
            "managed": row.managed,
            "deployment_params": safe_params(row.deployment_params) if row.managed and row.deployment_params
            else None,
            "fallback_models": list(row.fallback_models or []), "rest_seconds": row.rest_seconds}


def worker_snapshot(row: WorkerModelConfig) -> dict[str, Any]:
    return {"mode": row.model_mode, "primary_model": row.primary_model, "tier": row.tier_name,
            "fallback_model": row.fallback_model, "secondary_models": list(row.secondary_models or []),
            "temperature": row.temperature,
            "max_output_tokens": row.max_output_tokens, "context_window": row.context_window,
            "prompt_token_budget": row.prompt_token_budget,
            "reasoning_effort": row.reasoning_effort, "timeout_seconds": row.timeout_seconds,
            "retry_count": row.retry_count, "max_concurrency": row.max_concurrency,
            "use_structured_output": row.use_structured_output, "enabled": row.enabled,
            "escalation": row.escalation or None}


def routing_snapshot(row: AIRoutingConfig | None) -> dict[str, Any]:
    if row is None:
        return {"auto_router_enabled": False, "auto_router_model_name": None, "manage_auto_router": True,
                "classifier": "HEURISTIC", "classifier_model": None, "classifier_plugin": None,
                "tier_models": {}, "default_model": None, "send_metadata": True}
    return {"auto_router_enabled": row.auto_router_enabled, "auto_router_model_name": row.auto_router_model_name,
            "manage_auto_router": row.manage_auto_router, "classifier": row.classifier,
            "classifier_model": row.classifier_model, "classifier_plugin": row.classifier_plugin,
            "tier_models": {t: (row.tier_models or {}).get(t) for t in AUTO_ROUTER_TIERS},
            "default_model": row.default_model, "send_metadata": row.send_metadata}


def draft_snapshot(db: Session, user_id: int) -> dict[str, Any]:
    """The desired configuration, in the same shape a version stores."""
    models = db.scalars(select(AIModel).where(AIModel.user_id == user_id, AIModel.removed_at.is_(None))
                        .order_by(AIModel.litellm_model_name)).all()
    tiers = db.scalars(select(ModelTier).where(ModelTier.user_id == user_id).order_by(ModelTier.name)).all()
    workers = db.scalars(select(WorkerModelConfig).where(WorkerModelConfig.user_id == user_id)
                         .order_by(WorkerModelConfig.worker_type)).all()
    routing = db.scalar(select(AIRoutingConfig).where(AIRoutingConfig.user_id == user_id))
    return {"schema": SNAPSHOT_SCHEMA,
            "models": {m.litellm_model_name: model_snapshot(m) for m in models},
            "tiers": {t.name: {"description": t.description, "model": t.litellm_model_name, "enabled": t.enabled}
                      for t in tiers},
            "workers": {w.worker_type: worker_snapshot(w) for w in workers},
            "routing": routing_snapshot(routing)}


def active_version(db: Session, user_id: int) -> AIConfigVersion | None:
    return db.scalar(select(AIConfigVersion).where(AIConfigVersion.user_id == user_id)
                     .order_by(AIConfigVersion.version_number.desc()).limit(1))


def _flatten(value: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    if isinstance(value, dict) and value and not (len(prefix) >= 3 and prefix[-1] in (
            "escalation", "tier_models", "deployment_params", "overrides")):
        out: dict[tuple[str, ...], Any] = {}
        for key, item in value.items():
            out.update(_flatten(item, (*prefix, str(key))))
        return out
    return {prefix: value}


def _label(path: tuple[str, ...]) -> str:
    field = FIELD_LABELS.get(path[-1], path[-1].replace("_", " "))
    if path[0] == "workers" and len(path) >= 3:
        return f"{label(path[1])} · {field}"
    if path[0] == "tiers" and len(path) >= 3:
        return f"Tier {path[1]} · {field}"
    if path[0] == "models" and len(path) >= 3:
        return f"Model {path[1]} · {field}"
    if path[0] == "routing":
        return f"Auto Router · {field}" if len(path) == 2 else f"Auto Router · {' '.join(path[1:])}"
    return " · ".join(path)


def diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Every configured value that differs, plus each worker whose *effective* model changes as a result."""
    left, right = _flatten({k: v for k, v in (before or {}).items() if k != "schema"}), \
        _flatten({k: v for k, v in (after or {}).items() if k != "schema"})
    changes = []
    for path in sorted(set(left) | set(right)):
        if not path or left.get(path) == right.get(path):
            continue
        changes.append({"path": ".".join(path), "label": _label(path), "before": left.get(path),
                        "after": right.get(path),
                        "kind": "added" if path not in left else "removed" if path not in right else "changed"})
    for worker in sorted(set((before or {}).get("workers") or {}) | set((after or {}).get("workers") or {})):
        was = model_resolution.resolve_model(before or {}, worker)[0] if before else None
        now = model_resolution.resolve_model(after or {}, worker)[0]
        if was != now:
            changes.append({"path": f"effective.{worker}", "label": f"{label(worker)} · effective model",
                            "before": was, "after": now, "kind": "effective"})
    return changes


# ───────────── validation ─────────────

@dataclass
class LiveState:
    names: set[str] | None = None  # model names LiteLLM serves
    deployments: list[dict[str, Any]] | None = None
    router_settings: dict[str, Any] | None = None
    error: str | None = None

    @property
    def known(self) -> bool:
        return self.names is not None


def load_live(db: Session, user_id: int) -> LiveState:
    conn = connection_for(db, user_id)
    url, key = base_url(conn), api_key(db, conn)
    db.commit()
    return load_live_from(url, key, user_id)


def load_live_from(url: str | None, key: str | None, user_id: int) -> LiveState:
    """What LiteLLM serves right now: model names, deployments and router settings. Never raises."""
    if not url:
        return LiveState(error="No LiteLLM base URL is configured")
    try:
        with LiteLLMAPI(url, key, user_id=user_id) as api:
            deployments = api.model_info()
            names = set(api.model_names()) | {d["model_name"] for d in deployments}
            try:
                router = api.router_settings()
            except LiteLLMError:
                router = None
        return LiveState(names=names, deployments=deployments, router_settings=router)
    except LiteLLMError as exc:
        return LiveState(error=str(exc))


# A model's issue paths end in one of these; the name before it may itself contain dots ("gpt-4.1").
MODEL_ISSUE_FIELD = re.compile(r"\.(fallback_models\.\d+|rest_seconds|model_type|deployment_params)$")


def _subject(path: str) -> str | None:
    """What an issue is about, named as the screens name it ("Ranker", "Tier FAST", "Auto Router")."""
    head, _, rest = path.partition(".")
    if head == "workers" and rest:
        return label(rest.split(".", 1)[0])
    if head == "tiers" and rest:
        return f"Tier {rest.split('.', 1)[0]}"
    if head == "models" and rest:
        return f"Model {MODEL_ISSUE_FIELD.sub('', rest)}"
    if head == "routing":
        return "Auto Router"
    if head in ("connection", "litellm"):
        return "LiteLLM"
    return None


def _issue(path: str, message: str) -> dict[str, str]:
    issue = {"path": path, "message": message}
    if subject := _subject(path):
        issue["subject"] = subject
    return issue


def issue_text(issue: dict[str, str]) -> str:
    """An issue as one line, with what it is about in front unless the message already starts with it."""
    subject, message = issue.get("subject"), issue["message"]
    return message if not subject or message.startswith(subject) else f"{subject}: {message}"


def _in_range(value: Any, name: str) -> bool:
    low, high = LIMITS[name]
    return isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high


def _fallback_cycle(start: str, edges: dict[str, set[str]]) -> list[str] | None:
    """A fallback chain from `start` that returns to a model already in it."""
    stack = [(start, [start])]
    while stack:
        node, path = stack.pop()
        for nxt in edges.get(node, ()):
            if nxt in path:
                return [*path, nxt]
            if len(path) < 20:
                stack.append((nxt, [*path, nxt]))
    return None


def _router_fallback_edges(router_settings: dict[str, Any] | None) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {}
    for entry in (router_settings or {}).get("fallbacks") or []:
        if isinstance(entry, dict):
            for source, targets in entry.items():
                edges.setdefault(str(source), set()).update(str(t) for t in (targets or []))
    return edges


def _validate_embedder(base: str, config: dict[str, Any], model: str | None, models: dict[str, Any], check_model,
                       errors: list[dict[str, str]], warnings: list[dict[str, str]]) -> None:
    """An embedding worker: one FIXED embedding model of known vector size. Nothing else applies to it (no tiers,
    fallbacks or generation settings: a fallback of another size would make vectors that cannot be compared)."""
    if config.get("mode") != "FIXED":
        errors.append(_issue(f"{base}.mode", "An embedding worker uses one fixed model (FIXED mode)"))
        return
    if model is None:
        errors.append(_issue(f"{base}.primary_model", "Choose an embedding model, or switch the worker off"))
        return
    for name in ("fallback_model", "secondary_models", "escalation"):
        if config.get(name):
            errors.append(_issue(f"{base}.{name}", "An embedding worker has no fallback or escalation models: "
                                                   "vectors of another model cannot be compared"))
    entry = check_model(f"{base}.primary_model", model) or models.get(model) or {}
    if entry and entry.get("model_type") != "EMBEDDING":
        errors.append(_issue(f"{base}.primary_model", f"{model} is not an embedding model (set its type to "
                                                      "EMBEDDING if it is one)"))
    elif not entry.get("embedding_dimensions"):
        errors.append(_issue(f"{base}.primary_model", f"The vector size of {model} is unknown: run model "
                             "discovery, test the model, or set its embedding dimensions"))


def validate(snapshot: dict[str, Any], *, live: LiveState | None = None, gateway: bool = True,
             connection_ready: bool = True) -> dict[str, Any]:
    """Errors block an Apply; warnings are shown and allowed. Hard blocks only where correctness requires them."""
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if gateway and not connection_ready:
        errors.append(_issue("connection", "LiteLLM gateway mode is on but no LiteLLM base URL is configured"))
    models = snapshot.get("models") or {}
    tiers = snapshot.get("tiers") or {}
    routing = snapshot.get("routing") or {}
    live = live or LiveState()
    managed_names = {d.model_name for d in render(snapshot)}

    def check_model(path: str, name: str | None, *, what: str = "model") -> dict[str, Any] | None:
        if not name:
            return None
        entry = models.get(name)
        if entry is None and name not in managed_names:
            if live.known and name not in live.names:
                errors.append(_issue(path, f"{what.capitalize()} {name} does not exist in LiteLLM or the model "
                                           "registry"))
            elif not live.known:
                warnings.append(_issue(path, f"{what.capitalize()} {name} is not in the model registry; run model "
                                             "discovery"))
            return None
        if entry is not None and not entry.get("enabled", True):
            errors.append(_issue(path, f"{what.capitalize()} {name} is disabled"))
        if live.known and name not in live.names and name not in managed_names:
            errors.append(_issue(path, f"{what.capitalize()} {name} is not served by LiteLLM"))
        return entry

    def not_for_chat(path: str, entry: dict[str, Any] | None, name: str, worker_type: str) -> None:
        """A chat worker's fallback or secondary model must write text: failover sends it the same chat request."""
        if (entry or {}).get("model_type") == "EMBEDDING":
            errors.append(_issue(path, f"{name} is an embedding model; {label(worker_type)} needs a model that writes "
                                       "text"))

    for name, tier in tiers.items():
        if not TIER_NAME.match(name):
            errors.append(_issue(f"tiers.{name}", "Tier names are upper-case letters, digits and underscores"))
        if tier.get("enabled", True) and tier.get("model"):
            check_model(f"tiers.{name}.model", tier["model"])

    router_on = model_resolution.router_active(snapshot)
    router_name = routing.get("auto_router_model_name")
    if routing.get("auto_router_enabled"):
        if not router_name or not MODEL_NAME.match(router_name):
            errors.append(_issue("routing.auto_router_model_name", "The auto router needs a valid model name"))
        if routing.get("classifier") not in AUTO_ROUTER_CLASSIFIERS:
            errors.append(_issue("routing.classifier", "Unknown classifier"))
        if routing.get("manage_auto_router"):
            tier_models = routing.get("tier_models") or {}
            for tier in AUTO_ROUTER_TIERS:
                target = tier_models.get(tier)
                if not target:
                    errors.append(_issue(f"routing.tier_models.{tier}", f"Map the {tier} routing tier to a model"))
                elif target == router_name:
                    errors.append(_issue(f"routing.tier_models.{tier}", "The router cannot route to itself"))
                else:
                    check_model(f"routing.tier_models.{tier}", target)
            if routing.get("default_model"):
                check_model("routing.default_model", routing["default_model"], what="default model")
            if routing.get("classifier") == "LLM":
                if not routing.get("classifier_model"):
                    errors.append(_issue("routing.classifier_model", "The LLM classifier needs a model"))
                else:
                    check_model("routing.classifier_model", routing["classifier_model"], what="classifier model")
            if routing.get("classifier") == "CUSTOM" and not PLUGIN_PATH.match(routing.get("classifier_plugin") or ""):
                errors.append(_issue("routing.classifier_plugin", "The custom classifier needs the dotted path of "
                                     "a ClassifierPlugin instance importable by LiteLLM"))
        elif live.known and router_name not in live.names:
            errors.append(_issue("routing.auto_router_model_name",
                                 f"LiteLLM has no model named {router_name}; turn on 'managed by Job Hunt OS' or "
                                 "define the router in LiteLLM"))

    # A model's own fallbacks: usable models, never the model itself. Two models may name each other: a chain only
    # ever takes the primary model's own fallbacks (never a fallback's fallbacks), so it cannot loop.
    for name, entry in models.items():
        for index, target in enumerate(entry.get("fallback_models") or []):
            path = f"models.{name}.fallback_models.{index}"
            if target == name:
                errors.append(_issue(path, "A model cannot fall back to itself"))
            else:
                found = check_model(path, target, what="fallback model")
                if entry.get("model_type") != "EMBEDDING" and (found or {}).get("model_type") == "EMBEDDING":
                    errors.append(_issue(path, f"{target} is an embedding model; {name} writes text, so it cannot "
                                               "fall back to it"))
        rest = entry.get("rest_seconds")
        if rest is not None and not _in_range(rest, "rest_seconds"):
            low, high = LIMITS["rest_seconds"]
            errors.append(_issue(f"models.{name}.rest_seconds", f"Rest time must be between {low} and {high} seconds"))

    fallback_edges = _router_fallback_edges(live.router_settings)
    resolved: dict[str, str | None] = {}
    for worker_type, config in (snapshot.get("workers") or {}).items():
        base = f"workers.{worker_type}"
        worker = WORKERS.get(worker_type)
        if worker is None:
            warnings.append(_issue(base, f"{worker_type} is not a known worker"))
        mode = config.get("mode")
        if mode not in WORKER_MODEL_MODES:
            errors.append(_issue(f"{base}.mode", f"Unknown model mode {mode}"))
            continue
        if not config.get("enabled", True):
            continue
        if mode == "FIXED" and not config.get("primary_model"):
            errors.append(_issue(f"{base}.primary_model", "FIXED mode needs a primary model"))
        if mode in ("TIER", "AUTO_ROUTER") and config.get("tier") and config["tier"] not in tiers:
            errors.append(_issue(f"{base}.tier", f"Tier {config['tier']} does not exist"))
        if mode == "AUTO_ROUTER" and not router_on:
            if not config.get("tier"):
                errors.append(_issue(f"{base}.mode", "The auto router is off and no tier is set to use instead"))
            else:
                warnings.append(_issue(f"{base}.mode", f"The auto router is off; {label(worker_type)} uses tier "
                                                       f"{config['tier']} until it is on"))
        model, _configured, note = model_resolution.resolve_model(snapshot, worker_type)
        resolved[worker_type] = model
        if worker is not None and worker.capability == "EMBEDDING":
            _validate_embedder(base, config, model, models, check_model, errors, warnings)
            continue
        if model is None:
            (errors if gateway else warnings).append(
                _issue(f"{base}.mode", f"{label(worker_type)} has no usable model ({note or 'nothing assigned'})"))
            continue
        entry = check_model(f"{base}.primary_model" if mode == "FIXED" else f"{base}.tier", model)
        caps = entry or {}
        if caps.get("model_type") == "EMBEDDING":
            errors.append(_issue(f"{base}.primary_model" if mode == "FIXED" else f"{base}.tier",
                                 f"{model} is an embedding model; {label(worker_type)} needs a model that writes text"))
        fallback = config.get("fallback_model")
        if fallback:
            if fallback == model:
                errors.append(_issue(f"{base}.fallback_model", f"The fallback model cannot be the primary model "
                                                                  f"({model})"))
            else:
                not_for_chat(f"{base}.fallback_model", check_model(f"{base}.fallback_model", fallback,
                                                                   what="fallback model"), fallback, worker_type)
                cycle = _fallback_cycle(model, {**fallback_edges, model: {*fallback_edges.get(model, set()), fallback}})
                if cycle:
                    errors.append(_issue(f"{base}.fallback_model", "Circular fallback: " + " → ".join(cycle)))
        seen = {model, fallback}
        for index, extra in enumerate(config.get("secondary_models") or []):
            path = f"{base}.secondary_models.{index}"
            if extra in seen:
                errors.append(_issue(path, "A model can appear only once in the primary, fallback and secondary models"))
                continue
            seen.add(extra)
            not_for_chat(path, check_model(path, extra, what="secondary model"), extra, worker_type)
        for name in ("temperature", "max_output_tokens", "context_window", "prompt_token_budget", "timeout_seconds",
                     "retry_count", "max_concurrency"):
            value = config.get(name)
            if value is not None and not _in_range(value, name):
                low, high = LIMITS[name]
                errors.append(_issue(f"{base}.{name}", f"{FIELD_LABELS[name].capitalize()} must be between {low} "
                                                       f"and {high}"))
        if config.get("context_window") and caps.get("context_window") and \
                config["context_window"] > caps["context_window"]:
            errors.append(_issue(f"{base}.context_window", f"{config['context_window']} tokens exceeds the "
                                 f"{caps['context_window']}-token context {model} runs with"))
        if config.get("prompt_token_budget") and worker_type not in TRIMMED_WORKERS:
            warnings.append(_issue(f"{base}.prompt_token_budget", f"{label(worker_type)} does not trim its prompt "
                                   "yet, so a prompt token budget has no effect"))
        window = config.get("context_window") or caps.get("context_window")
        if config.get("prompt_token_budget") and window and config["prompt_token_budget"] >= window:
            warnings.append(_issue(f"{base}.prompt_token_budget", f"A {config['prompt_token_budget']}-token prompt "
                                   f"budget is no smaller than the {window}-token context, so it never trims"))
        if config.get("max_output_tokens") and caps.get("max_output_tokens") and \
                config["max_output_tokens"] > caps["max_output_tokens"]:
            errors.append(_issue(f"{base}.max_output_tokens", f"{model} produces at most "
                                 f"{caps['max_output_tokens']} output tokens"))
        effort = config.get("reasoning_effort")
        if effort is not None:
            if effort not in REASONING_EFFORTS:
                errors.append(_issue(f"{base}.reasoning_effort", "Reasoning effort is low, medium or high"))
            elif caps.get("supports_reasoning") is False:
                errors.append(_issue(f"{base}.reasoning_effort", f"{model} does not support reasoning effort"))
            elif caps.get("supports_reasoning") is None and model != router_name:
                warnings.append(_issue(f"{base}.reasoning_effort", f"It is not known whether {model} supports "
                                       "reasoning effort, so it will not be sent until discovery confirms it"))
        if worker and worker.structured_output_required:
            if not config.get("use_structured_output", True):
                warnings.append(_issue(f"{base}.use_structured_output", f"{label(worker_type)} parses typed JSON; "
                                       "turning structured output off makes schema failures more likely"))
            if caps.get("supports_structured_output") is False:
                errors.append(_issue(f"{base}.primary_model", f"{model} cannot return structured output, which "
                                     f"{label(worker_type)} requires"))
            elif caps.get("supports_structured_output") is None and model != router_name:
                warnings.append(_issue(f"{base}.primary_model", f"Structured Output Required: it is unknown "
                                       f"whether {model} returns JSON reliably"))
        if worker and caps.get("context_window") and caps["context_window"] < worker.min_context_tokens:
            warnings.append(_issue(f"{base}.primary_model", f"This model may not satisfy {label(worker_type)} "
                                   f"requirements: {caps['context_window']}-token context, "
                                   f"{worker.min_context_tokens} recommended"))
        policy = config.get("escalation") or {}
        if policy:
            unknown = set(policy.get("conditions") or ()) - set(ESCALATION_CONDITIONS)
            if unknown:
                errors.append(_issue(f"{base}.escalation", f"Unknown escalation condition(s): {', '.join(unknown)}"))
            target = policy.get("model") or (model_resolution.tier_model(snapshot, policy.get("tier"))[0]
                                             if policy.get("tier") else None)
            if policy.get("conditions") and not target:
                errors.append(_issue(f"{base}.escalation", "Escalation needs a model or a mapped tier"))
            elif target and target == model:
                errors.append(_issue(f"{base}.escalation", "Escalating to the same model cannot improve an answer"))
            elif target:
                check_model(f"{base}.escalation", target, what="escalation model")
            threshold = policy.get("confidence_threshold")
            if threshold is not None and not (isinstance(threshold, (int, float)) and 0 <= threshold <= 1):
                errors.append(_issue(f"{base}.escalation", "The confidence threshold is between 0 and 1"))
            if "LOW_CONFIDENCE" in (policy.get("conditions") or ()) and threshold is None:
                errors.append(_issue(f"{base}.escalation", "LOW_CONFIDENCE needs a confidence threshold"))

    if resolved.get("REVIEWER") and resolved.get("REVIEWER") == resolved.get("TAILOR"):
        warnings.append(_issue("workers.REVIEWER", "Reviewer and Tailor use the same model; a different model "
                               "family gives a more independent review (optional)"))

    for name, entry in models.items():
        if entry.get("model_type") not in AI_MODEL_TYPES:
            errors.append(_issue(f"models.{name}.model_type", "Unknown model type"))
        if entry.get("managed"):
            params = entry.get("deployment_params") or {}
            if not MODEL_NAME.match(name):
                errors.append(_issue(f"models.{name}", "Model names are letters, digits, spaces and . _ : / @ + -"))
            if not params.get("model"):
                errors.append(_issue(f"models.{name}.deployment_params", "A managed model needs a LiteLLM model "
                                     "(e.g. ollama_chat/llama3.2:3b)"))
            for key in literal_secrets(params):
                errors.append(_issue(f"models.{name}.deployment_params", f"{key} must be an os.environ/NAME "
                                     "reference; secrets are never stored in Job Hunt OS configuration"))
            ctx, ceiling = params.get("num_ctx"), entry.get("max_context_window")
            if ctx is not None and (not isinstance(ctx, int) or ctx < 512):
                errors.append(_issue(f"models.{name}.deployment_params", "num_ctx must be a whole number ≥ 512"))
            elif ctx and ceiling and ctx > ceiling:
                errors.append(_issue(f"models.{name}.deployment_params",
                                     f"num_ctx {ctx} exceeds the model's {ceiling}-token maximum"))
            provider = str(params.get("model") or "").split("/", 1)[0]
            if provider and not provider.startswith("ollama") and not params.get("api_key"):
                warnings.append(_issue(f"models.{name}.deployment_params", f"{provider} usually needs an api_key "
                                       "(an os.environ/NAME reference LiteLLM resolves)"))
    return {"errors": errors, "warnings": warnings, "live_checked": live.known, "live_error": live.error}


# ───────────── editing the draft ─────────────

def _bump(db: Session, user_id: int, expected_revision: int | None) -> LiteLLMConnection:
    conn = _locked_connection(db, user_id)
    if expected_revision is not None and conn.draft_revision != expected_revision:
        db.rollback()
        raise ConfigConflict("Someone else changed the AI configuration since you loaded it. Reload and try again.",
                             draft_revision=conn.draft_revision)
    conn.draft_revision += 1
    conn.updated_at = utcnow()
    return conn


def _clean_model_name(value: Any, field: str) -> str | None:
    if value in (None, ""):
        return None
    clean = str(value).strip()
    if not MODEL_NAME.match(clean):
        raise AIConfigError(f"{field}: {clean!r} is not a valid LiteLLM model name")
    return clean


def _number(value: Any, name: str, kind: type) -> Any:
    if value in (None, ""):
        return None
    try:
        number = kind(value)
    except (TypeError, ValueError) as exc:
        raise AIConfigError(f"{FIELD_LABELS.get(name, name)} must be a number") from exc
    low, high = LIMITS[name]
    if not low <= number <= high:
        raise AIConfigError(f"{FIELD_LABELS.get(name, name).capitalize()} must be between {low} and {high}")
    return number


def clean_escalation(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    if not isinstance(value, dict):
        raise AIConfigError("escalation must be an object")
    conditions = [str(c).upper() for c in value.get("conditions") or []]
    unknown = set(conditions) - set(ESCALATION_CONDITIONS)
    if unknown:
        raise AIConfigError(f"Unknown escalation condition(s): {', '.join(sorted(unknown))}")
    threshold = value.get("confidence_threshold")
    if threshold not in (None, ""):
        try:
            threshold = float(threshold)
        except (TypeError, ValueError) as exc:
            raise AIConfigError("The confidence threshold must be a number between 0 and 1") from exc
        if not 0 <= threshold <= 1:
            raise AIConfigError("The confidence threshold must be between 0 and 1")
    else:
        threshold = None
    tier = (str(value.get("tier") or "").strip().upper() or None)
    policy = {"model": _clean_model_name(value.get("model"), "escalation model"), "tier": tier,
              "conditions": conditions, "confidence_threshold": threshold}
    return policy if conditions else None


def update_worker(db: Session, user_id: int, worker_type: str, changes: dict[str, Any],
                  expected_revision: int | None = None) -> dict[str, Any]:
    ensure_seeded(db, user_id)
    if worker_type not in WORKERS:
        raise NotFound(f"Unknown worker {worker_type}")
    conn = _bump(db, user_id, expected_revision)
    row = db.scalar(select(WorkerModelConfig).where(WorkerModelConfig.user_id == user_id,
                                                    WorkerModelConfig.worker_type == worker_type))
    if row is None:
        worker = WORKERS[worker_type]
        row = WorkerModelConfig(user_id=user_id, worker_type=worker_type, model_mode=worker.default_mode,
                                tier_name=None if worker.optional else worker.default_tier,
                                use_structured_output=worker.capability == "CHAT",
                                created_at=utcnow(), updated_at=utcnow())
        db.add(row)
    for name, value in changes.items():
        if name not in WORKER_FIELDS:
            raise AIConfigError(f"Unknown worker setting {name}")
        if name == "model_mode":
            if value not in WORKER_MODEL_MODES:
                raise AIConfigError(f"Model mode is one of {', '.join(WORKER_MODEL_MODES)}")
        elif name in ("primary_model", "fallback_model"):
            value = _clean_model_name(value, FIELD_LABELS.get(name, name))
        elif name == "secondary_models":
            if value in (None, ""):
                value = None
            elif isinstance(value, (list, tuple)) and len(value) <= 8:
                value = [m for m in (_clean_model_name(v, "Secondary model") for v in value) if m] or None
            else:
                raise AIConfigError("Secondary models: a list of up to 8 model names")
        elif name == "tier_name":
            value = (str(value).strip().upper() or None) if value not in (None, "") else None
        elif name == "temperature":
            value = _number(value, name, float)
        elif name in ("max_output_tokens", "context_window", "prompt_token_budget", "timeout_seconds", "retry_count",
                      "max_concurrency"):
            value = _number(value, name, int)
        elif name == "reasoning_effort":
            value = (str(value).lower() or None) if value not in (None, "") else None
            if value is not None and value not in REASONING_EFFORTS:
                raise AIConfigError("Reasoning effort is low, medium or high")
        elif name in ("use_structured_output", "enabled"):
            value = bool(value)
        elif name == "escalation":
            value = clean_escalation(value)
        setattr(row, name, value)
    row.updated_at = utcnow()
    db.commit()
    return {"worker": worker_snapshot(row), "draft_revision": conn.draft_revision}


def update_routing(db: Session, user_id: int, changes: dict[str, Any],
                   expected_revision: int | None = None) -> dict[str, Any]:
    ensure_seeded(db, user_id)
    conn = _bump(db, user_id, expected_revision)
    row = db.scalar(select(AIRoutingConfig).where(AIRoutingConfig.user_id == user_id))
    if row is None:
        row = AIRoutingConfig(user_id=user_id, created_at=utcnow(), updated_at=utcnow())
        db.add(row)
    for name, value in changes.items():
        if name not in ROUTING_FIELDS:
            raise AIConfigError(f"Unknown routing setting {name}")
        if name in ("auto_router_model_name", "classifier_model", "default_model"):
            value = _clean_model_name(value, name)
        elif name == "classifier":
            value = str(value or "").upper()
            if value not in AUTO_ROUTER_CLASSIFIERS:
                raise AIConfigError(f"Classifier is one of {', '.join(AUTO_ROUTER_CLASSIFIERS)}")
            if value == "DISABLED":
                row.auto_router_enabled = False
        elif name == "classifier_plugin":
            value = (str(value).strip() or None) if value else None
            if value and not PLUGIN_PATH.match(value):
                raise AIConfigError("The classifier plugin is a dotted Python path, e.g. jobhunt_classifier.router")
        elif name == "tier_models":
            if not isinstance(value, dict):
                raise AIConfigError("tier_models maps SIMPLE, MEDIUM, COMPLEX and REASONING to models")
            unknown = set(value) - set(AUTO_ROUTER_TIERS)
            if unknown:
                raise AIConfigError(f"Unknown routing tier(s): {', '.join(sorted(unknown))}")
            value = {tier: _clean_model_name(value.get(tier, (row.tier_models or {}).get(tier)), tier)
                     for tier in AUTO_ROUTER_TIERS}
        else:
            value = bool(value)
        setattr(row, name, value)
    row.updated_at = utcnow()
    db.commit()
    return {"routing": routing_snapshot(row), "draft_revision": conn.draft_revision}


def upsert_tier(db: Session, user_id: int, name: str, changes: dict[str, Any],
                expected_revision: int | None = None) -> dict[str, Any]:
    ensure_seeded(db, user_id)
    name = (name or "").strip().upper()
    if not TIER_NAME.match(name):
        raise AIConfigError("Tier names are upper-case letters, digits and underscores (e.g. FAST)")
    conn = _bump(db, user_id, expected_revision)
    row = db.scalar(select(ModelTier).where(ModelTier.user_id == user_id, ModelTier.name == name))
    if row is None:
        row = ModelTier(user_id=user_id, name=name, description="", enabled=True, created_at=utcnow(),
                        updated_at=utcnow())
        db.add(row)
    if "litellm_model_name" in changes:
        row.litellm_model_name = _clean_model_name(changes["litellm_model_name"], f"tier {name}")
    if "description" in changes:
        row.description = str(changes["description"] or "")[:300]
    if "enabled" in changes:
        row.enabled = bool(changes["enabled"])
    row.updated_at = utcnow()
    db.commit()
    return {"tier": {"name": row.name, "description": row.description, "model": row.litellm_model_name,
                     "enabled": row.enabled}, "draft_revision": conn.draft_revision}


def delete_tier(db: Session, user_id: int, name: str, expected_revision: int | None = None) -> dict[str, Any]:
    ensure_seeded(db, user_id)
    in_use = db.scalars(select(WorkerModelConfig.worker_type).where(
        WorkerModelConfig.user_id == user_id, WorkerModelConfig.tier_name == name)).all()
    if in_use:
        raise AIConfigError(f"Tier {name} is used by {', '.join(label(w) for w in in_use)}")
    conn = _bump(db, user_id, expected_revision)
    deleted = db.execute(delete(ModelTier).where(ModelTier.user_id == user_id, ModelTier.name == name)).rowcount
    if not deleted:
        db.rollback()
        raise NotFound(f"Tier {name} does not exist")
    db.commit()
    return {"deleted": name, "draft_revision": conn.draft_revision}


def _deployment_params(payload: dict[str, Any]) -> dict[str, Any]:
    """litellm_params for a managed model, from either raw params or the local-Ollama form fields."""
    if payload.get("deployment_params") is not None:
        params = payload["deployment_params"]
        if not isinstance(params, dict):
            raise AIConfigError("deployment_params must be an object")
        params = dict(params)
    else:
        ollama = str(payload.get("ollama_model") or "").strip()
        if not ollama:
            raise AIConfigError("Choose the Ollama model to deploy")
        params = {"model": f"ollama_chat/{ollama}"}
        base = normalize_base_url(payload.get("api_base") or "http://localhost:11434")
        if base is None:
            raise AIConfigError("The Ollama base URL must be an http(s) URL")
        params["api_base"] = base
        if payload.get("num_ctx") not in (None, ""):
            params["num_ctx"] = _number(payload["num_ctx"], "context_window", int)
        if payload.get("keep_alive") not in (None, ""):
            keep_alive = str(payload["keep_alive"]).strip()
            if not re.fullmatch(r"-?\d+[smh]?|-1", keep_alive):
                raise AIConfigError("keep_alive is a duration such as 5m, 1h or -1 (keep loaded)")
            params["keep_alive"] = keep_alive
    bad = literal_secrets(params)
    if bad:
        raise AIConfigError(f"{', '.join(bad)} must be an os.environ/NAME reference, never a literal secret")
    return params


def add_managed_model(db: Session, user_id: int, payload: dict[str, Any],
                      expected_revision: int | None = None) -> dict[str, Any]:
    """Register a model Job Hunt OS will deploy into LiteLLM on the next Apply (e.g. a local Ollama model)."""
    ensure_seeded(db, user_id)
    name = _clean_model_name(payload.get("litellm_model_name"), "model name")
    if not name:
        raise AIConfigError("Give the model a LiteLLM model name (alias), e.g. local-fast")
    params = _deployment_params(payload)
    existing = db.scalar(select(AIModel).where(AIModel.user_id == user_id, AIModel.litellm_model_name == name))
    if existing is not None and (existing.present_in_litellm and not existing.managed):
        raise AIConfigError(f"{name} already exists in LiteLLM and is not managed by Job Hunt OS; choose another name")
    conn = _bump(db, user_id, expected_revision)
    row = existing or AIModel(user_id=user_id, litellm_model_name=name, source="USER", created_at=utcnow())
    row.removed_at = None  # adding a removed model again brings it back
    row.display_name = str(payload.get("display_name") or name)[:200]
    row.managed, row.deployment_params, row.enabled = True, params, True
    row.model_type = payload.get("model_type") if payload.get("model_type") in AI_MODEL_TYPES else row.model_type or "CUSTOM"
    model = str(params.get("model") or "")
    row.provider = model.split("/", 1)[0] if "/" in model else row.provider
    row.underlying_model = model.split("/", 1)[1] if "/" in model else model or row.underlying_model
    if params.get("num_ctx"):
        row.context_window = params["num_ctx"]
    if row.provider and row.provider.startswith("ollama"):
        row.supports_structured_output = True  # Ollama constrains decoding to JSON for response_format
    row.updated_at = utcnow()
    db.add(row)
    db.commit()
    return {"model": model_snapshot(row), "draft_revision": conn.draft_revision}


def update_model(db: Session, user_id: int, name: str, changes: dict[str, Any],
                 expected_revision: int | None = None) -> dict[str, Any]:
    ensure_seeded(db, user_id)
    row = db.scalar(select(AIModel).where(AIModel.user_id == user_id, AIModel.litellm_model_name == name))
    if row is None:
        raise NotFound(f"Model {name} is not in the registry")
    conn = _bump(db, user_id, expected_revision)
    dropped: list[str] = []
    for key, value in changes.items():
        if key == "display_name":
            row.display_name = str(value or name)[:200]
        elif key == "enabled":
            if row.enabled and not value:
                dropped = drop_backup_uses(db, user_id, name)
                audit(db, user_id, "DISABLE", actor=_actor(db, user_id),
                      changes=[{"model": name, "dropped_from": dropped}])
            row.enabled = bool(value)
        elif key == "model_type":
            if value not in AI_MODEL_TYPES:
                raise AIConfigError(f"Model type is one of {', '.join(AI_MODEL_TYPES)}")
            row.model_type = value
        elif key == "overrides":
            if not isinstance(value, dict) or set(value) - set(OVERRIDABLE):
                raise AIConfigError(f"Overrides may set {', '.join(OVERRIDABLE)}")
            clean = {}
            for k, v in value.items():
                if v in (None, ""):
                    continue
                clean[k] = bool(v) if k in CAPABILITIES else _number(v, k, int)
            row.overrides = clean or None
        elif key == "fallback_models":
            if value in (None, ""):
                row.fallback_models = None
                continue
            if not isinstance(value, (list, tuple)) or len(value) > MAX_MODEL_FALLBACKS:
                raise AIConfigError(f"Fallback models: a list of up to {MAX_MODEL_FALLBACKS} model names")
            names = [m for m in (_clean_model_name(v, "Fallback model") for v in value) if m]
            if name in names:
                raise AIConfigError("A model cannot fall back to itself")
            row.fallback_models = list(dict.fromkeys(names)) or None
        elif key == "rest_seconds":
            row.rest_seconds = None if value in (None, "") else _number(value, "rest_seconds", int)
        elif key in ("deployment_params", "ollama_model", "num_ctx", "keep_alive", "api_base"):
            continue  # handled below
        else:
            raise AIConfigError(f"Unknown model setting {key}")
    if row.managed and any(k in changes for k in ("deployment_params", "ollama_model", "num_ctx", "keep_alive")):
        params = dict(row.deployment_params or {})
        if "deployment_params" in changes:
            params = _deployment_params({"deployment_params": changes["deployment_params"]})
        else:
            if changes.get("num_ctx") not in (None, ""):
                params["num_ctx"] = _number(changes["num_ctx"], "context_window", int)
            elif "num_ctx" in changes:
                params.pop("num_ctx", None)
            if changes.get("keep_alive") not in (None, ""):
                params["keep_alive"] = _deployment_params({"ollama_model": "x", "keep_alive": changes["keep_alive"]})[
                    "keep_alive"]
            elif "keep_alive" in changes:
                params.pop("keep_alive", None)
        row.deployment_params = params
        if params.get("num_ctx"):
            row.context_window = params["num_ctx"]
    row.updated_at = utcnow()
    db.commit()
    return {"model": model_snapshot(row), "draft_revision": conn.draft_revision, "dropped": dropped}


def drop_backup_uses(db: Session, user_id: int, name: str) -> list[str]:
    """Stop using a model as a backup: it leaves every worker's backups (the next one moving up, order kept) and
    every other model's own fallbacks. Edits the draft only (the caller commits; the apply versions it). A main
    model, a tier, the auto router and escalation are left alone. Returns what changed, worded like
    `model_references`."""
    out: list[str] = []
    workers = db.scalars(select(WorkerModelConfig).where(WorkerModelConfig.user_id == user_id)).all()
    for row in sorted(workers, key=lambda w: w.worker_type):
        backups = [b for b in (row.fallback_model, *(row.secondary_models or [])) if b]
        if name not in backups:
            continue
        out += [f"{label(row.worker_type)} (backup {i})" for i, b in enumerate(backups, 1) if b == name]
        rest = [b for b in backups if b != name]
        row.fallback_model = rest[0] if rest else None
        row.secondary_models = rest[1:] or None
        row.updated_at = utcnow()
    models = db.scalars(select(AIModel).where(AIModel.user_id == user_id, AIModel.litellm_model_name != name)).all()
    for row in sorted(models, key=lambda m: m.litellm_model_name):
        if name in (row.fallback_models or []):
            row.fallback_models = [m for m in row.fallback_models if m != name] or None
            row.updated_at = utcnow()
            out.append(f"{row.litellm_model_name}'s fallbacks")
    return out


def model_references(snapshot: dict[str, Any], name: str) -> list[str]:
    """Every place a configuration still uses a model, in words a person can act on. Empty: nothing uses it."""
    out: list[str] = []
    for worker_type, config in sorted((snapshot.get("workers") or {}).items()):
        who = label(worker_type)
        if config.get("mode") == "FIXED" and config.get("primary_model") == name:
            out.append(f"{who} (its model)")
        backups = [config.get("fallback_model"), *(config.get("secondary_models") or [])]
        out += [f"{who} (backup {i})" for i, b in enumerate(backups, 1) if b == name]
        if (config.get("escalation") or {}).get("model") == name:
            out.append(f"{who} (quality escalation)")
    out += [f"tier {tier}" for tier, entry in sorted((snapshot.get("tiers") or {}).items()) if entry.get("model") == name]
    out += [f"{other}'s fallbacks" for other, entry in sorted((snapshot.get("models") or {}).items())
            if other != name and name in (entry.get("fallback_models") or [])]
    routing = snapshot.get("routing") or {}
    if routing.get("auto_router_enabled") and routing.get("auto_router_model_name") == name:
        out.append("the auto router")
    out += [f"auto router tier {tier}" for tier, model in sorted((routing.get("tier_models") or {}).items())
            if model == name]
    for key, what in (("default_model", "auto router default model"), ("classifier_model", "auto router classifier")):
        if routing.get(key) == name:
            out.append(what)
    return out


def remove_model(db: Session, user_id: int, name: str, expected_revision: int | None = None) -> dict[str, Any]:
    """Remove a model from Job Hunt OS: it leaves the configuration and every picker, and a sync leaves it removed.
    A model Job Hunt OS manages loses its LiteLLM deployment on the apply that follows; a discovered one stays in
    LiteLLM (its configuration is not ours). Refused while anything still uses it. Commits."""
    from app.models import ModelRouteState

    ensure_seeded(db, user_id)
    row = db.scalar(select(AIModel).where(AIModel.user_id == user_id, AIModel.litellm_model_name == name,
                                          AIModel.removed_at.is_(None)))
    if row is None:
        raise NotFound(f"{name} is not in the model registry")
    conn = _bump(db, user_id, expected_revision)
    dropped = drop_backup_uses(db, user_id, name)
    db.flush()
    references = model_references(draft_snapshot(db, user_id), name)
    if references:
        db.rollback()  # a refused removal keeps its backups too
        raise ConfigConflict(f"{name} is still used by: {', '.join(references)}. Change those first.",
                             references=references)
    # `managed` is kept: the snapshot leaves the model out, so the apply deletes an owned deployment anyway, and a
    # Restore redeploys it
    row.removed_at, row.enabled, row.updated_at = utcnow(), False, utcnow()
    db.execute(delete(ModelRouteState).where(ModelRouteState.user_id == user_id, ModelRouteState.model == name))
    audit(db, user_id, "REMOVE", actor=_actor(db, user_id), changes=[{"model": name, "dropped_from": dropped}])
    db.commit()
    return {"removed": name, "draft_revision": conn.draft_revision, "dropped": dropped}


def restore_model(db: Session, user_id: int, name: str, expected_revision: int | None = None) -> dict[str, Any]:
    """Bring a removed model back, enabled; a model Job Hunt OS managed is redeployed by the apply that follows."""
    row = db.scalar(select(AIModel).where(AIModel.user_id == user_id, AIModel.litellm_model_name == name,
                                          AIModel.removed_at.is_not(None)))
    if row is None:
        raise NotFound(f"{name} is not a removed model")
    conn = _bump(db, user_id, expected_revision)
    row.removed_at, row.enabled, row.updated_at = None, True, utcnow()
    audit(db, user_id, "RESTORE", actor=_actor(db, user_id), changes=[{"model": name}])
    db.commit()
    return {"restored": name, "draft_revision": conn.draft_revision}


def remove_managed_model(db: Session, user_id: int, name: str, expected_revision: int | None = None) -> dict[str, Any]:
    """Stop managing a model: the next Apply removes its deployment from LiteLLM."""
    row = db.scalar(select(AIModel).where(AIModel.user_id == user_id, AIModel.litellm_model_name == name))
    if row is None or not row.managed:
        raise NotFound(f"{name} is not a model Job Hunt OS manages")
    conn = _bump(db, user_id, expected_revision)
    row.managed, row.enabled = False, False
    row.updated_at = utcnow()
    db.commit()
    return {"model": model_snapshot(row), "draft_revision": conn.draft_revision}


def write_snapshot_to_draft(db: Session, user_id: int, snapshot: dict[str, Any]) -> None:
    """Make a stored snapshot the draft again (roll back). Discovered facts about models are left as they are."""
    _bump(db, user_id, None)
    workers = {w.worker_type: w for w in db.scalars(select(WorkerModelConfig)
                                                    .where(WorkerModelConfig.user_id == user_id)).all()}
    for worker_type, config in (snapshot.get("workers") or {}).items():
        row = workers.get(worker_type) or WorkerModelConfig(user_id=user_id, worker_type=worker_type,
                                                            created_at=utcnow())
        row.model_mode, row.primary_model, row.tier_name = config.get("mode") or "TIER", config.get("primary_model"), \
            config.get("tier")
        for name in ("fallback_model", "secondary_models", "temperature", "max_output_tokens", "context_window",
                     "prompt_token_budget", "reasoning_effort", "timeout_seconds", "retry_count", "max_concurrency",
                     "escalation"):
            setattr(row, name, config.get(name))
        row.use_structured_output = config.get("use_structured_output", True)
        row.enabled = config.get("enabled", True)
        row.updated_at = utcnow()
        db.add(row)
    tiers = {t.name: t for t in db.scalars(select(ModelTier).where(ModelTier.user_id == user_id)).all()}
    wanted = snapshot.get("tiers") or {}
    for name, row in tiers.items():
        if name not in wanted:
            db.delete(row)
    for name, tier in wanted.items():
        row = tiers.get(name) or ModelTier(user_id=user_id, name=name, created_at=utcnow())
        row.description, row.litellm_model_name, row.enabled = tier.get("description") or "", tier.get("model"), \
            tier.get("enabled", True)
        row.updated_at = utcnow()
        db.add(row)
    routing = db.scalar(select(AIRoutingConfig).where(AIRoutingConfig.user_id == user_id)) or \
        AIRoutingConfig(user_id=user_id, created_at=utcnow())
    for name in ROUTING_FIELDS:
        if name in (snapshot.get("routing") or {}):
            setattr(routing, name, snapshot["routing"][name])
    routing.updated_at = utcnow()
    db.add(routing)
    models = {m.litellm_model_name: m for m in db.scalars(select(AIModel).where(AIModel.user_id == user_id)).all()}
    wanted_models = snapshot.get("models") or {}
    for name, row in models.items():
        if name not in wanted_models and row.managed and row.removed_at is None:
            row.managed = False  # added after that version: its deployment is removed again
    for name, entry in wanted_models.items():
        row = models.get(name) or AIModel(user_id=user_id, litellm_model_name=name, source="USER",
                                          created_at=utcnow())
        row.display_name = entry.get("display_name") or name
        row.enabled = entry.get("enabled", True)
        row.model_type = entry.get("model_type") or "CUSTOM"
        row.overrides = entry.get("overrides") or None
        row.removed_at = None  # rolling back to a version that had it brings it back
        row.fallback_models = list(entry.get("fallback_models") or []) or None
        row.rest_seconds = entry.get("rest_seconds")
        row.managed = bool(entry.get("managed"))
        if entry.get("managed"):
            row.deployment_params = entry.get("deployment_params")
        if row.id is None:
            for key in ("provider", "underlying_model", "max_context_window", *OVERRIDABLE):
                if entry.get(key) is not None:
                    setattr(row, key, entry[key])
        row.updated_at = utcnow()
        db.add(row)
    db.flush()


# ───────────── preview, apply, roll back ─────────────

def _manager(db: Session, user_id: int, conn: LiteLLMConnection) -> LiteLLMConfigManager:
    return LiteLLMConfigManager(base_url=base_url(conn), api_key=api_key(db, conn), user_id=user_id,
                                mode=sync_mode(conn))


def _gateway_on(db: Session, user_id: int) -> bool:
    from app.services.ai_config import resolve as resolve_ai_config
    from app.services.user_settings import ai_overrides

    return resolve_ai_config(get_settings(), ai_overrides(db, user_id))["llm_provider"] == "litellm"


@dataclass
class _Checked:
    """A validated draft: what was checked, against which revision and version, and the plan to apply it."""

    draft: dict[str, Any]
    before: dict[str, Any]
    changes: list[dict[str, Any]]
    report: dict[str, Any]
    plan: Any
    manager: LiteLLMConfigManager
    revision: int
    version: int


def _check(db: Session, user_id: int, live: LiveState | None) -> _Checked:
    """Read the draft, then validate it against LiteLLM with no database transaction open.

    SQLite has one writer, and telemetry for the LiteLLM calls made here is written from its own session, so no
    transaction (and certainly no row lock) may be held across network I/O.
    """
    ensure_seeded(db, user_id)
    conn = connection_for(db, user_id)
    active = active_version(db, user_id)
    draft = draft_snapshot(db, user_id)
    before = dict(active.configuration_snapshot or {}) if active else {}
    revision, version = conn.draft_revision, active.version_number if active else 0
    gateway, url, key, mode = _gateway_on(db, user_id), base_url(conn), api_key(db, conn), sync_mode(conn)
    db.commit()  # end the read transaction before talking to LiteLLM
    live = live or load_live_from(url, key, user_id)
    manager = LiteLLMConfigManager(base_url=url, api_key=key, user_id=user_id, mode=mode)
    plan = manager.plan(draft, live.deployments)
    report = validate(draft, live=live, gateway=gateway, connection_ready=bool(url))
    sync_errors, sync_warnings = manager.validate(plan, draft)
    report["errors"] += [_issue("litellm", e) for e in sync_errors]
    report["warnings"] += [_issue("litellm", w) for w in sync_warnings]
    return _Checked(draft, before, diff(before, draft), report, plan, manager, revision, version)


def preview(db: Session, user_id: int, live: LiveState | None = None) -> dict[str, Any]:
    checked = _check(db, user_id, live)
    return {"base_version": checked.version, "draft_revision": checked.revision, "changes": checked.changes,
            "validation": checked.report, "sync_plan": checked.plan.public(),
            "can_apply": not checked.report["errors"]}


def apply(db: Session, user_id: int, *, actor: str | None = None, source: str = "USER",
          expected_revision: int | None = None, expected_version: int | None = None,
          restored_from: int | None = None, summary: str | None = None, live: LiveState | None = None) -> dict[str, Any]:
    """Validate the draft and make it the active configuration as one logical operation. See the module doc."""
    actor = actor or _actor(db, user_id)
    action = "ROLLBACK" if restored_from else "APPLY"
    checked = _check(db, user_id, live)
    if expected_version is not None and expected_version != checked.version:
        raise ConfigConflict(f"The active configuration is now v{checked.version}; preview again before applying",
                             active_version=checked.version)
    if expected_revision is not None and expected_revision != checked.revision:
        raise ConfigConflict("The configuration was edited after it was previewed; preview again",
                             draft_revision=checked.revision)
    if not checked.changes:
        return {"status": "UNCHANGED", "version": checked.version,
                "message": "Nothing changed since the active version"}
    if checked.report["errors"]:
        audit(db, user_id, action, result="REJECTED", source=source, actor=actor, changes=checked.changes,
              error="; ".join(issue_text(e) for e in checked.report["errors"])[:1000])
        db.commit()
        raise ValidationFailed("The configuration is not valid", validation=checked.report)

    # Claim the account's apply slot, and make sure nothing moved while the draft was being validated.
    conn = _locked_connection(db, user_id)
    if conn.sync_status == "APPLYING" and conn.applying_since and utcnow() - conn.applying_since < APPLY_LEASE:
        db.rollback()
        raise ConfigBusy("Another change to the AI configuration is being applied; try again in a moment")
    current = active_version(db, user_id)
    if conn.draft_revision != checked.revision or (current.version_number if current else 0) != checked.version:
        db.rollback()
        raise ConfigConflict("The configuration changed while it was being validated; preview again",
                             draft_revision=conn.draft_revision)
    conn.sync_status, conn.applying_since = "APPLYING", utcnow()
    db.commit()  # visible to other requests: a second Apply now waits instead of racing this one

    try:
        result = checked.manager.apply(checked.plan, checked.draft, checked.before)
    except Exception as exc:  # an unexpected failure must still release APPLYING
        log.exception("LiteLLM sync failed for account %s", user_id)
        from app.services.litellm_config_manager import SyncResult

        result = SyncResult("FAILED", checked.plan.mode, error=f"{type(exc).__name__}: {exc}")
    conn = _locked_connection(db, user_id)
    conn.applying_since = None
    if result.status == "FAILED":
        conn.sync_status, conn.last_sync_error = "FAILED", redact_text(result.error, 1000)
        audit(db, user_id, action, result="FAILED", source=source, actor=actor, changes=checked.changes,
              sync_result=result.public(), error=result.error)
        db.commit()
        raise ApplyFailed(result.message or "Applying the configuration to LiteLLM failed",
                          sync_result=result.public(), active_version=checked.version)
    number = checked.version + 1
    text = summary or (f"Restored v{restored_from}" if restored_from else _summary(checked.changes))
    try:
        db.add(AIConfigVersion(user_id=user_id, version_number=number, configuration_snapshot=checked.draft,
                               changed_by=actor[:320], source=source, change_summary=text[:2000],
                               changes=redact_value(checked.changes), sync_result=result.public(),
                               restored_from_version=restored_from, created_at=utcnow()))
        conn.sync_status = "SYNCED" if result.status == "SYNCED" else "PENDING"
        conn.last_sync_at, conn.last_sync_error = utcnow(), None
        audit(db, user_id, action, source=source, actor=actor, version_number=number, changes=checked.changes,
              sync_result=result.public())
        db.commit()
    except Exception:
        # The version could not be recorded: put LiteLLM back so it matches the version that is still active.
        db.rollback()
        if checked.plan.needs_sync:
            manager = checked.manager
            manager.apply(manager.plan(checked.before, load_live_from(manager.base_url, manager.api_key,
                                                                      user_id).deployments),
                          checked.before, checked.draft)
        conn = _locked_connection(db, user_id)
        conn.sync_status, conn.applying_since, conn.last_sync_error = "FAILED", None, "Could not record the version"
        db.commit()
        raise
    status = conn.sync_status
    model_resolution.invalidate(user_id)
    return {"status": status, "version": number, "draft_revision": checked.revision, "sync": result.public(),
            "warnings": checked.report["warnings"], "changes": checked.changes}


def _summary(changes: list[dict[str, Any]]) -> str:
    effective = [c for c in changes if c["kind"] == "effective"]
    shown = effective or changes
    text = "; ".join(f"{c['label']}: {c['before']} → {c['after']}" for c in shown[:6])
    return text + (f" (+{len(shown) - 6} more)" if len(shown) > 6 else "")


def restore(db: Session, user_id: int, version_id: int, *, actor: str | None = None,
            expected_version: int | None = None) -> dict[str, Any]:
    """Roll back to an older version by applying its snapshot as a new version. The draft is kept if it fails."""
    target = db.scalar(select(AIConfigVersion).where(AIConfigVersion.user_id == user_id,
                                                     AIConfigVersion.id == version_id))
    if target is None:
        raise NotFound("That configuration version does not exist")
    saved = draft_snapshot(db, user_id)
    write_snapshot_to_draft(db, user_id, target.configuration_snapshot or {})
    db.commit()
    try:
        return apply(db, user_id, actor=actor, expected_version=expected_version,
                     restored_from=target.version_number, summary=f"Restored v{target.version_number}")
    except AIConfigError:
        db.rollback()
        write_snapshot_to_draft(db, user_id, saved)
        db.commit()
        raise


def discard_draft(db: Session, user_id: int) -> dict[str, Any]:
    """Throw away edits that were never applied: the draft becomes the active version again. Creates no version.

    The settings screen saves by applying straight away; when that apply is refused (validation, LiteLLM), this puts
    the draft back so a refused edit is never left waiting. Commits."""
    ensure_seeded(db, user_id)
    active = active_version(db, user_id)
    if active is None:
        raise NotFound("There is no applied configuration to go back to")
    snapshot = active.configuration_snapshot or {}
    write_snapshot_to_draft(db, user_id, snapshot)
    _unrestore(db, user_id, snapshot, active.created_at)
    db.commit()
    return {"status": "DISCARDED", "active_version": active.version_number,
            "draft_revision": connection_for(db, user_id).draft_revision}


def _unrestore(db: Session, user_id: int, snapshot: dict[str, Any], since: datetime | None) -> None:
    """A model restored after the active version and absent from it is removed again: Undo puts a refused Restore back."""
    restored = {(c or {}).get("model") for r in db.scalars(select(AIConfigAuditEntry).where(
        AIConfigAuditEntry.user_id == user_id, AIConfigAuditEntry.action == "RESTORE",
        *([AIConfigAuditEntry.created_at >= since] if since else []))).all() for c in (r.changes or [])}
    wanted = snapshot.get("models") or {}
    for row in db.scalars(select(AIModel).where(AIModel.user_id == user_id, AIModel.removed_at.is_(None))).all():
        if row.litellm_model_name in restored and row.litellm_model_name not in wanted:
            row.removed_at, row.enabled, row.updated_at = utcnow(), False, utcnow()


def verify(db: Session, user_id: int) -> dict[str, Any]:
    """Compare what LiteLLM runs with the active version, and record SYNCED / OUT_OF_SYNC."""
    ensure_seeded(db, user_id)
    conn = connection_for(db, user_id)
    active = active_version(db, user_id)
    snapshot = dict(active.configuration_snapshot or {}) if active else {}
    number = active.version_number if active else None
    manager = _manager(db, user_id, conn)
    db.commit()  # no transaction across network I/O
    verified, differences = manager.verify(snapshot)
    live = load_live_from(manager.base_url, manager.api_key, user_id)
    missing = sorted({m for m in _referenced(snapshot) if live.known and m not in live.names
                      and m not in {d.model_name for d in render(snapshot)}})
    locked = _locked_connection(db, user_id)
    if verified is None:
        status = locked.sync_status
    elif verified and not missing:
        status = "SYNCED"
    else:
        status = "OUT_OF_SYNC"
    if locked.sync_status != "APPLYING" and verified is not None:
        locked.sync_status = status
        locked.last_sync_error = None if status == "SYNCED" else "; ".join(
            [*differences, *(f"{m} is not served by LiteLLM" for m in missing)])[:1000]
    audit(db, user_id, "VERIFY", actor=_actor(db, user_id), result="SUCCEEDED" if status == "SYNCED" else "FAILED",
          version_number=number,
          error=None if status == "SYNCED" else "; ".join([*differences, *missing]) or live.error)
    db.commit()
    return {"status": status, "verified": verified, "differences": differences, "missing_models": missing,
            "error": live.error}


def _referenced(snapshot: dict[str, Any]) -> set[str]:
    names = {model_resolution.resolve_model(snapshot, w)[0] for w in (snapshot.get("workers") or {})}
    for config in (snapshot.get("workers") or {}).values():
        names.add(config.get("fallback_model"))
        names.update(config.get("secondary_models") or ())
    models = snapshot.get("models") or {}
    for name in [n for n in names if n]:  # a primary's own fallbacks join its chain (ResolvedWorker.chain)
        names.update((models.get(name) or {}).get("fallback_models") or ())
    return {n for n in names if n}


# ───────────── reading it all back ─────────────

def version_dict(row: AIConfigVersion, include_snapshot: bool = False) -> dict[str, Any]:
    out = {"id": row.id, "version": row.version_number, "changed_by": row.changed_by, "source": row.source,
           "summary": row.change_summary, "changes": row.changes or [], "sync_result": row.sync_result,
           "restored_from_version": row.restored_from_version, "created_at": _iso(row.created_at)}
    if include_snapshot:
        out["snapshot"] = row.configuration_snapshot
    return out


def versions(db: Session, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    ensure_seeded(db, user_id)
    rows = db.scalars(select(AIConfigVersion).where(AIConfigVersion.user_id == user_id)
                      .order_by(AIConfigVersion.version_number.desc()).limit(limit)).all()
    return [version_dict(r) for r in rows]


def audit_entries(db: Session, user_id: int, limit: int = 100) -> list[dict[str, Any]]:
    rows = db.scalars(select(AIConfigAuditEntry).where(AIConfigAuditEntry.user_id == user_id)
                      .order_by(AIConfigAuditEntry.created_at.desc(), AIConfigAuditEntry.id.desc()).limit(limit)).all()
    return [{"id": r.id, "action": r.action, "source": r.source, "actor": r.actor, "result": r.result,
             "version": r.version_number, "changes": r.changes, "sync_result": r.sync_result, "error": r.error,
             "created_at": _iso(r.created_at)} for r in rows]


def runtime_view(db: Session, user_id: int, live: LiveState | None = None) -> dict[str, Any]:
    """Desired (draft) against active (applied version) against LiteLLM, per worker."""
    ensure_seeded(db, user_id)
    conn = connection_for(db, user_id)
    active = active_version(db, user_id)
    active_snapshot = active.configuration_snapshot if active else {}
    draft = draft_snapshot(db, user_id)
    managed = {d.model_name for d in render(active_snapshot)}
    rows = []
    for worker_type in WORKERS:
        desired, desired_logical, _ = model_resolution.resolve_model(draft, worker_type)
        running, running_logical, note = model_resolution.resolve_model(active_snapshot, worker_type)
        served = None if live is None or not live.known or running is None else (running in live.names
                                                                                  or running in managed)
        if desired != running or desired_logical != running_logical:
            state = "PENDING"
        elif served is False:
            state = "OUT_OF_SYNC"
        else:
            state = "SYNCED"
        rows.append({"worker_type": worker_type, "label": label(worker_type), "desired": desired,
                     "desired_assignment": desired_logical, "active": running, "active_assignment": running_logical,
                     "note": note, "served_by_litellm": served, "state": state})
    pending = diff(active_snapshot, draft)
    overall = conn.sync_status if conn.sync_status in ("APPLYING", "FAILED", "OUT_OF_SYNC") else (
        "PENDING" if pending or conn.sync_status == "PENDING" else "SYNCED")
    return {"sync_status": overall, "active_version": active.version_number if active else 0,
            "pending_changes": len(pending), "workers": rows, "last_sync_at": _iso(conn.last_sync_at),
            "last_sync_error": conn.last_sync_error}


def usage_by_model(db: Session, user_id: int, snapshot: dict[str, Any]) -> dict[str, list[str]]:
    """Which workers use each model under a configuration (primary, fallback, escalation)."""
    out: dict[str, list[str]] = {}
    for worker_type, config in (snapshot.get("workers") or {}).items():
        names = {model_resolution.resolve_model(snapshot, worker_type)[0], config.get("fallback_model"),
                 *(config.get("secondary_models") or ()),
                 model_resolution.escalation_model(snapshot, config.get("escalation"))}
        for name in names - {None}:
            out.setdefault(name, []).append(worker_type)
    routing = snapshot.get("routing") or {}
    if model_resolution.router_active(snapshot):
        for tier, name in (routing.get("tier_models") or {}).items():
            if name:
                out.setdefault(name, []).append(f"AUTO:{tier}")
    return out


def overview(db: Session, user_id: int) -> dict[str, Any]:
    """Everything AI Orchestration → Models shows, in one response. Makes no LiteLLM call."""
    ensure_seeded(db, user_id)
    conn = connection_for(db, user_id)
    active = active_version(db, user_id)
    draft = draft_snapshot(db, user_id)
    every = db.scalars(select(AIModel).where(AIModel.user_id == user_id).order_by(AIModel.litellm_model_name)).all()
    models = [m for m in every if m.removed_at is None]
    hosting = catalog_hosting(every)
    removed = [{"name": m.litellm_model_name, "removed_at": _iso(m.removed_at), "managed_before": m.source == "USER"}
               for m in every if m.removed_at is not None]
    used = usage_by_model(db, user_id, draft)
    from app.services.ai_config import resolve as resolve_ai_config
    from app.services.user_settings import ai_overrides

    ai = resolve_ai_config(get_settings(), ai_overrides(db, user_id))
    return {
        "gateway_enabled": ai["llm_provider"] == "litellm", "llm_provider": ai["llm_provider"],
        "gateway": gateway_status(db, user_id),
        "connection": connection_public(db, conn),
        "active_version": active.version_number if active else 0,
        "draft": draft, "active": active.configuration_snapshot if active else {},
        "pending_changes": diff(active.configuration_snapshot if active else {}, draft),
        "workers": [{"worker_type": w.worker_type, "label": w.label, "description": w.description,
                     "structured_output_required": w.structured_output_required,
                     "min_context_tokens": w.min_context_tokens, "has_prompt": bool(w.prompts),
                     "capability": w.capability, "optional": w.optional,
                     "trims_prompt": w.worker_type in TRIMMED_WORKERS,
                     "config": draft["workers"].get(w.worker_type),
                     "resolved": dict(zip(("model", "assignment", "note"),
                                          model_resolution.resolve_model(draft, w.worker_type), strict=True))}
                    for w in WORKERS.values()],
        "models": [{"name": m.litellm_model_name, **model_snapshot(m), "hosting": hosting[m.litellm_model_name],
                    "present_in_litellm": m.present_in_litellm, "source": m.source, "health_status": m.health_status,
                    "health_checked_at": _iso(m.health_checked_at), "health_latency_ms": m.health_latency_ms,
                    "health_error": m.health_error, "last_synced_at": _iso(m.last_synced_at),
                    "discovered": {name: getattr(m, name) for name in OVERRIDABLE},
                    "used_by": sorted(used.get(m.litellm_model_name, []))} for m in models],
        "removed_models": removed,
        "request_size": model_capacity.report(db, user_id, draft),  # requests too big for their model, and fixes
        "tiers": draft["tiers"], "routing": draft["routing"],
        "options": {"model_modes": list(WORKER_MODEL_MODES), "reasoning_efforts": list(REASONING_EFFORTS),
                    "model_types": list(AI_MODEL_TYPES), "classifiers": list(AUTO_ROUTER_CLASSIFIERS),
                    "routing_tiers": list(AUTO_ROUTER_TIERS), "escalation_conditions": list(ESCALATION_CONDITIONS),
                    "limits": {k: list(v) for k, v in LIMITS.items()}},
    }
