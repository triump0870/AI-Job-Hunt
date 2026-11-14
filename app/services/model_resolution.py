"""Runtime model resolution: which model a worker uses, how it calls it, and when it escalates.

    worker (ranker, tailor, …) → LLMClient.generate_prompt → worker type from the prompt's name
    → get(user_id, "RANKER")  — the account's *active* AIConfigVersion, cached for a few seconds
    → ResolvedWorker: LiteLLM model name, generation settings, fallback, escalation policy, metadata

Workers never name a model. Resolution reads only the latest applied configuration version (never the draft
being edited), so an edit takes effect when it is applied and not a moment before, and every request records
the version it ran under.

Routing policy lives here and nowhere else:
* FIXED       → the worker's primary model.
* TIER        → the model mapped to the worker's tier.
* AUTO_ROUTER → the LiteLLM auto router; while the router is switched off, the worker's tier.
* Infrastructure fallback (timeouts, 429, outages) is LiteLLM's job: the worker's fallback model is sent with the
  request as a client-side fallback.
* Quality escalation (low confidence, schema failure, ambiguity) is Job Hunt OS's job: `escalation_reason` decides,
  after an answer arrived, whether a stronger model should answer instead. The two never substitute for each other:
  a rate-limited escalation model is replaced only by its own configured fallbacks, never by the worker's backups.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.services.ai_workers import EMBEDDING_WORKERS, WORKERS
from app.settings import get_settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

DEFAULT_WORKER = "DEFAULT"
DEFAULT_TIER = "PRIMARY_REASONING"
# Output fields a machine-facing prompt may use to ask for a second opinion (see app.prompts); absent = no signal.
AMBIGUITY_FIELDS = ("ambiguous", "needs_escalation")
FATAL_UNCERTAIN_FIELDS = ("fatal_requirement_uncertain",)


@dataclass(frozen=True)
class Escalation:
    model: str | None
    conditions: tuple[str, ...]
    confidence_threshold: float | None
    label: str = ""
    # The escalation model's own fallbacks (AIModel.fallback_models): tried, in order, only when it is rate limited.
    fallbacks: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedWorker:
    worker_type: str
    mode: str | None
    configured: str | None  # the logical assignment as configured: a model, a tier name or AUTO_ROUTER
    model: str | None  # the LiteLLM model name the request goes to
    note: str | None = None  # why the model is what it is when it is not the configured one
    fallback_model: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    context_window: int | None = None
    # Tokens the built prompt may use before its context builder trims it (app.services.prompt_budget); None = no trim.
    prompt_token_budget: int | None = None
    reasoning_effort: str | None = None
    timeout_seconds: int | None = None
    retry_count: int | None = None
    max_concurrency: int | None = None
    use_structured_output: bool = True
    structured_output_required: bool = False
    enabled: bool = True
    escalation: Escalation | None = None
    config_version: int | None = None
    send_metadata: bool = True
    # Capabilities of the models this worker may call (primary, fallback, escalation), from the active snapshot.
    capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Auto router tier → model, to read the tier LiteLLM chose from the model group that answered.
    router_tiers: dict[str, str] = field(default_factory=dict)
    router_model: str | None = None
    # Models tried after `fallback_model`, in order, and the model failover moved this request away from (if any).
    secondary_models: tuple[str, ...] = ()
    failed_over_from: str | None = None
    # The fallbacks configured on the model itself (AIModel.fallback_models), tried after the worker's own backups.
    model_fallbacks: tuple[str, ...] = ()

    def chain(self) -> list[str]:
        """The models behind the primary, in the order they are tried (LiteLLM client-side fallbacks): the worker's
        backups first, then the primary model's own fallbacks."""
        seen = {self.model}
        return [m for m in (self.fallback_model, *self.secondary_models, *self.model_fallbacks)
                if m and not (m in seen or seen.add(m))]

    def caps(self, model: str | None) -> dict[str, Any]:
        return self.capabilities.get(model or "", {})

    def context_limit(self, model: str | None) -> int | None:
        """The context this request may use: the worker's own budget, else what the model is known to run with."""
        return self.context_window or self.caps(model).get("context_window")


# ───────────── pure resolution over a snapshot ─────────────

def _enabled_model(snapshot: dict[str, Any], name: str | None) -> tuple[str | None, str | None]:
    if not name:
        return None, None
    entry = (snapshot.get("models") or {}).get(name)
    if entry is not None and not entry.get("enabled", True):
        return None, f"model {name} is disabled"
    return name, None


def tier_model(snapshot: dict[str, Any], tier: str | None) -> tuple[str | None, str | None]:
    entry = (snapshot.get("tiers") or {}).get(tier or "")
    if entry is None:
        return None, f"tier {tier} does not exist" if tier else "no tier set"
    if not entry.get("enabled", True):
        return None, f"tier {tier} is disabled"
    if not entry.get("model"):
        return None, f"tier {tier} has no model"
    return _enabled_model(snapshot, entry["model"])


def router_active(snapshot: dict[str, Any]) -> bool:
    routing = snapshot.get("routing") or {}
    return bool(routing.get("auto_router_enabled") and routing.get("auto_router_model_name")
                and routing.get("classifier") != "DISABLED")


def resolve_model(snapshot: dict[str, Any], worker_type: str) -> tuple[str | None, str | None, str | None]:
    """(model, configured, note) for one worker. `model` is None when nothing usable is configured."""
    config = (snapshot.get("workers") or {}).get(worker_type)
    if worker_type in EMBEDDING_WORKERS and (config is None or not config.get("enabled", True)):
        return None, None, "not configured"  # an embedding worker never borrows a chat tier
    if config is None:
        model, note = tier_model(snapshot, DEFAULT_TIER)
        return model, DEFAULT_TIER, note
    mode = config.get("mode")
    if mode == "FIXED":
        model, note = _enabled_model(snapshot, config.get("primary_model"))
        return model, config.get("primary_model"), note or (None if model else "no primary model set")
    if mode == "AUTO_ROUTER":
        if router_active(snapshot):
            return (snapshot["routing"]["auto_router_model_name"], "AUTO_ROUTER", None)
        # Recorded as AUTO_ROUTER→TIER, so Monitoring never counts these requests as the router's decisions.
        model, note = tier_model(snapshot, config.get("tier"))
        return (model, f"AUTO_ROUTER→{config.get('tier')}",
                f"auto router is off; using tier {config.get('tier')}" + (f" ({note})" if note else ""))
    model, note = tier_model(snapshot, config.get("tier"))
    return model, config.get("tier"), note


def model_fallbacks_of(snapshot: dict[str, Any], model: str | None) -> tuple[str, ...]:
    """A model's own fallbacks that are usable now (in the registry and enabled), in order."""
    entry = (snapshot.get("models") or {}).get(model or "") or {}
    return tuple(name for name in (entry.get("fallback_models") or [])
                 if isinstance(name, str) and name and name != model and _enabled_model(snapshot, name)[0])


def rest_seconds_of(snapshot: dict[str, Any], model: str | None) -> int | None:
    """How long this model rests after a failure, when the model sets it; None follows the account's setting."""
    value = ((snapshot.get("models") or {}).get(model or "") or {}).get("rest_seconds")
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def escalation_model(snapshot: dict[str, Any], policy: dict[str, Any] | None) -> str | None:
    if not policy:
        return None
    if policy.get("model"):
        return _enabled_model(snapshot, policy["model"])[0]
    if policy.get("tier"):
        return tier_model(snapshot, policy["tier"])[0]
    return None


def _capabilities(snapshot: dict[str, Any], name: str | None) -> dict[str, Any]:
    entry = (snapshot.get("models") or {}).get(name or "") or {}
    return {key: entry.get(key) for key in ("supports_structured_output", "supports_reasoning", "supports_tools",
                                            "supports_vision", "context_window", "max_output_tokens", "model_type",
                                            "embedding_dimensions")}


def resolve(snapshot: dict[str, Any], worker_type: str | None, version: int | None = None) -> ResolvedWorker:
    worker_type = worker_type or DEFAULT_WORKER
    config = (snapshot.get("workers") or {}).get(worker_type) or {}
    model, configured, note = resolve_model(snapshot, worker_type)
    if model is None and get_settings().litellm_default_model:
        model = get_settings().litellm_default_model.strip()
        note = f"{note or 'nothing assigned'}; using LITELLM_DEFAULT_MODEL"
    policy = config.get("escalation") or None
    escalation = None
    if policy and policy.get("conditions"):
        target = escalation_model(snapshot, policy)
        escalation = Escalation(model=target, conditions=tuple(policy.get("conditions") or ()),
                                confidence_threshold=policy.get("confidence_threshold"),
                                label=policy.get("model") or policy.get("tier") or "",
                                fallbacks=model_fallbacks_of(snapshot, target) if target else ())
    fallback = config.get("fallback_model") or None
    secondary = tuple(m for m in (config.get("secondary_models") or ()) if isinstance(m, str) and m)
    model_fallbacks = model_fallbacks_of(snapshot, model)
    routing = snapshot.get("routing") or {}
    names = {model, fallback, *secondary, *model_fallbacks, escalation.model if escalation else None,
             *(escalation.fallbacks if escalation else ())} - {None}
    worker = WORKERS.get(worker_type)
    return ResolvedWorker(
        worker_type=worker_type, mode=config.get("mode"), configured=configured, model=model, note=note,
        fallback_model=fallback if fallback != model else None, secondary_models=secondary,
        model_fallbacks=model_fallbacks,
        temperature=config.get("temperature"), max_output_tokens=config.get("max_output_tokens"),
        context_window=config.get("context_window"), prompt_token_budget=config.get("prompt_token_budget"),
        reasoning_effort=config.get("reasoning_effort"),
        timeout_seconds=config.get("timeout_seconds"), retry_count=config.get("retry_count"),
        max_concurrency=config.get("max_concurrency"),
        use_structured_output=config.get("use_structured_output", True),
        structured_output_required=bool(worker and worker.structured_output_required),
        enabled=config.get("enabled", True), escalation=escalation, config_version=version,
        send_metadata=routing.get("send_metadata", True),
        capabilities={name: _capabilities(snapshot, name) for name in names},
        router_tiers={tier: name for tier, name in (routing.get("tier_models") or {}).items() if name},
        router_model=routing.get("auto_router_model_name") if router_active(snapshot) else None)


# ───────────── the active configuration, cached ─────────────

_lock = threading.Lock()
_cache: dict[int, tuple[float, int | None, dict[str, Any]]] = {}
_sessions: Callable[[], Session] | None = None


def use_sessions(factory: Callable[[], Session] | None) -> None:
    """Point resolution at another database (tests); None restores the application database."""
    global _sessions
    _sessions = factory
    invalidate()


def _session_factory() -> Callable[[], Session]:
    if _sessions is not None:
        return _sessions
    from app.db import SessionLocal

    return SessionLocal


def invalidate(user_id: int | None = None) -> None:
    """Forget cached configuration: one account's after it applies a change, everyone's with no argument."""
    with _lock:
        if user_id is None:
            _cache.clear()
        else:
            _cache.pop(user_id, None)


def active_snapshot(user_id: int | None) -> tuple[int | None, dict[str, Any]]:
    """(version number, snapshot) of the account's active configuration; ({}, None) for work with no account."""
    if user_id is None:
        return None, {}
    ttl = max(0, get_settings().ai_config_cache_seconds)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(user_id)
        if hit and hit[0] > now:
            return hit[1], hit[2]
    from sqlalchemy import select

    from app.models import AIConfigVersion
    from app.services import ai_model_config

    with _session_factory()() as db:
        row = db.execute(select(AIConfigVersion.version_number, AIConfigVersion.configuration_snapshot)
                         .where(AIConfigVersion.user_id == user_id)
                         .order_by(AIConfigVersion.version_number.desc()).limit(1)).first()
        if row is None and ai_model_config.ensure_seeded(db, user_id):
            row = db.execute(select(AIConfigVersion.version_number, AIConfigVersion.configuration_snapshot)
                             .where(AIConfigVersion.user_id == user_id)
                             .order_by(AIConfigVersion.version_number.desc()).limit(1)).first()
    version, snapshot = (row[0], row[1] or {}) if row else (None, {})
    with _lock:
        _cache[user_id] = (now + ttl, version, snapshot)
    return version, snapshot


def get(user_id: int | None, worker_type: str | None) -> ResolvedWorker:
    """The worker's resolved configuration, e.g. `get(user_id, "RANKER").model`."""
    version, snapshot = active_snapshot(user_id)
    return resolve(snapshot, worker_type, version)


# ───────────── per-request policy ─────────────

def request_params(worker: ResolvedWorker, model: str, contract: Any = None) -> tuple[dict[str, Any], list[str]]:
    """The generation parameters to send, and the configured ones deliberately withheld.

    A parameter is only sent when the model is not known to lack it: reasoning effort needs a model known to
    reason; JSON mode is withheld only from a model known not to support it. `drop_params` lets LiteLLM drop any
    remaining parameter the provider cannot take, instead of failing the request.

    Structured output: a worker with an output contract (app.prompts.contracts) sends it as a JSON Schema only to a
    model known to support structured output. A model whose support is unknown gets JSON mode, and the contract is
    checked by app.services.llm either way — nothing is sent on a guess, and nothing is accepted on JSON alone.
    """
    caps = worker.caps(model)
    params: dict[str, Any] = {"drop_params": True}
    withheld: list[str] = []
    if worker.temperature is not None:
        params["temperature"] = worker.temperature
    if worker.max_output_tokens:
        params["max_tokens"] = worker.max_output_tokens
    if worker.reasoning_effort:
        if caps.get("supports_reasoning") is True:
            params["reasoning_effort"] = worker.reasoning_effort
        else:
            withheld.append("reasoning_effort")
    if worker.use_structured_output or worker.structured_output_required or contract is not None:
        support = caps.get("supports_structured_output")
        if support is False:
            withheld.append("response_format")
        elif contract is not None and support is True:
            from app.prompts.contracts import response_format

            params["response_format"] = response_format(contract)
        else:
            params["response_format"] = {"type": "json_object"}
    return params, withheld


def schema_enforcement(params: dict[str, Any]) -> str:
    """How a request's output contract is enforced, from the parameters actually sent."""
    kind = (params.get("response_format") or {}).get("type")
    return {"json_schema": "native_schema", "json_object": "json_mode"}.get(kind, "prompt_only")


def _confidence(data: dict[str, Any] | None) -> float | None:
    value = (data or {}).get("confidence")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 100 if value > 1 else float(value)
    return None


def confidence_of(data: dict[str, Any] | None) -> float | None:
    return _confidence(data)


def escalation_reason(worker: ResolvedWorker, *, data: dict[str, Any] | None = None, schema_failed: bool = False,
                      manual: bool = False) -> str | None:
    """Why this answer should be escalated to the worker's escalation model, or None to keep it."""
    policy = worker.escalation
    if policy is None or not policy.model:
        return None
    conditions = set(policy.conditions)
    if manual and "MANUAL" in conditions:
        return "MANUAL"
    if schema_failed:
        return "SCHEMA_FAILURE" if "SCHEMA_FAILURE" in conditions else None
    if "LOW_CONFIDENCE" in conditions and policy.confidence_threshold is not None:
        confidence = _confidence(data)
        if confidence is not None and confidence < policy.confidence_threshold:
            return "LOW_CONFIDENCE"
    if "AMBIGUOUS" in conditions and any((data or {}).get(key) is True for key in AMBIGUITY_FIELDS):
        return "AMBIGUOUS"
    if "FATAL_REQUIREMENT_UNCERTAIN" in conditions and any(
            (data or {}).get(key) is True for key in FATAL_UNCERTAIN_FIELDS):
        return "FATAL_REQUIREMENT_UNCERTAIN"
    return None


def request_metadata(worker: ResolvedWorker, *, workflow_type: str | None, entity_id: int | None, attempt: int,
                     trace_id: str | None, previous_confidence: float | None = None,
                     escalation: str | None = None) -> dict[str, Any]:
    """What LiteLLM (its logs, and a custom auto-router classifier) is told about the request. No content."""
    meta: dict[str, Any] = {
        "jobhunt": True, "worker_type": worker.worker_type, "workflow_type": (workflow_type or "").upper() or None,
        "retry_number": max(0, attempt - 1), "structured_output_required": worker.structured_output_required,
        "jobhunt_config_version": worker.config_version, "jobhunt_trace_id": trace_id,
    }
    if entity_id is not None:
        meta["application_id" if workflow_type == "application" else "job_id"] = entity_id
    if worker.escalation and worker.escalation.confidence_threshold is not None:
        meta["confidence_required"] = worker.escalation.confidence_threshold
    if previous_confidence is not None:
        meta["previous_confidence"] = previous_confidence
    if escalation:
        meta["escalation"] = escalation
    return {k: v for k, v in meta.items() if v is not None}


def routing_tier(worker: ResolvedWorker, requested: str, answered_group: str | None,
                 answered_model: str | None) -> str | None:
    """The auto router tier behind an answer, when the model group that answered maps to exactly one tier."""
    if not worker.router_model or requested != worker.router_model:
        return None
    for candidate in (answered_group, answered_model):
        tiers = [tier for tier, name in worker.router_tiers.items() if candidate and name == candidate]
        if len(tiers) == 1:
            return tiers[0]
    return None


# ───────────── per-worker concurrency ─────────────

_slots: dict[tuple[int | None, str, int], threading.BoundedSemaphore] = {}


@contextmanager
def concurrency_slot(worker: ResolvedWorker, user_id: int | None, timeout: float) -> Iterator[bool]:
    """Hold one of the worker's `max_concurrency` request slots in this process. Yields False if none freed up
    within `timeout`, so the caller can report a rate limit instead of piling more requests on the model."""
    limit = worker.max_concurrency
    if not limit:
        yield True
        return
    key = (user_id, worker.worker_type, int(limit))
    with _lock:
        semaphore = _slots.setdefault(key, threading.BoundedSemaphore(int(limit)))
    acquired = semaphore.acquire(timeout=max(0.0, timeout))
    try:
        yield acquired
    finally:
        if acquired:
            semaphore.release()
