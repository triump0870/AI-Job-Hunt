"""The model registry: what LiteLLM serves, what is known about each model, and whether it works.

    discover()   GET /v1/models + /model/info (+ /health/latest, + Ollama /api/tags for local models)
                 → AIModel rows: provider, underlying model, context window, capabilities, health
    test_model() one tiny request straight to a model (a few dozen tokens) → success, latency, what answered
    test_worker() a worker's fixture task through the real LLM layer, with the draft or the active configuration

Facts come only from what LiteLLM or Ollama report. When neither says, the value stays NULL — unknown — and the UI
shows "unknown" rather than a guess. Two rules are applied because they are properties of the software, not of a
model: an `auto_router/…` deployment is the auto router, and Ollama constrains decoding to JSON when asked for
`response_format`, so an Ollama deployment supports structured output.
"""
from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AIModel
from app.prompts import build_model_health_prompt, build_worker_test_prompt
from app.services import ai_model_config as config
from app.services import model_market, model_resolution
from app.services.ai_workers import WORKERS
from app.services.litellm_client import LiteLLMError, ollama_tags
from app.services.litellm_config_manager import classify_live, owner_tag
from app.services.redaction import redact_text
from app.settings import get_settings

FACTS = ("provider", "underlying_model", "supports_structured_output", "supports_tools", "supports_reasoning",
         "supports_vision", "context_window", "max_context_window", "max_output_tokens", "embedding_dimensions",
         "router_models")
JSON_TYPES = {"string": str, "number": (int, float), "array": list, "object": dict, "boolean": bool}


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def describe(deployments: list[dict[str, Any]], ollama: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """What the deployments behind one model name say about it. Disagreeing deployments leave a fact unknown."""
    described = [_describe_one(d, ollama) for d in deployments]
    if not described:
        return {}
    out = {}
    for key in (*FACTS, "model_type"):
        values = {json.dumps(d.get(key)) for d in described}
        out[key] = described[0].get(key) if len(values) == 1 else None
    return out


def _router_members(params: dict[str, Any]) -> list[str] | None:
    """Every model an auto router deployment can send a request to, sorted so a re-sync compares equal.

    That includes an LLM classifier: it reads each request in order to choose a tier, so it counts for where they go.
    """
    router = params.get("complexity_router_config") or {}
    members = {params.get("complexity_router_default_model"), router.get("default_model"),
               (router.get("classifier_llm_config") or {}).get("model")}
    for target in (router.get("tiers") or {}).values():
        members.update(target if isinstance(target, list) else [target])
    return sorted(str(m) for m in members if m) or None


def _describe_one(deployment: dict[str, Any], ollama: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    params = deployment.get("litellm_params") or {}
    info = deployment.get("model_info") or {}
    model = str(params.get("model") or "")
    provider, _, underlying = model.partition("/") if "/" in model else (info.get("litellm_provider"), "", model)
    # LiteLLM's model map does not know every name, but the deployment still states the provider it was set up with.
    provider = provider or info.get("litellm_provider") or params.get("custom_llm_provider")
    facts: dict[str, Any] = {
        "provider": provider, "underlying_model": underlying or None,
        "supports_structured_output": _bool(info.get("supports_response_schema")),
        "supports_tools": _bool(info.get("supports_function_calling")),
        "supports_reasoning": _bool(info.get("supports_reasoning")),
        "supports_vision": _bool(info.get("supports_vision")),
        "context_window": _int(params.get("num_ctx")) or _int(info.get("max_input_tokens")),
        "max_context_window": _int(info.get("max_input_tokens")),
        "max_output_tokens": _int(info.get("max_output_tokens")),
        # LiteLLM's model map states an embedding model's vector size; nothing else is taken as its size.
        "embedding_dimensions": _int(info.get("output_vector_size")),
        "model_type": None,
    }
    if model.startswith("auto_router/"):
        facts.update(provider="litellm", model_type="AUTO_ROUTER", supports_structured_output=None,
                     router_models=_router_members(params))
    elif info.get("mode") == "embedding":
        facts["model_type"] = "EMBEDDING"
    if str(provider or "").startswith("ollama"):
        facts["supports_structured_output"] = True
        tags = (ollama.get(str(params.get("api_base") or "")) or {}).get(underlying) or {}
        capabilities = set(tags.get("capabilities") or [])
        if tags:
            # Ollama lists every capability a model has, so an absent one is a definite no.
            facts["supports_tools"] = "tools" in capabilities
            facts["supports_reasoning"] = "thinking" in capabilities
            facts["supports_vision"] = "vision" in capabilities
            facts["max_context_window"] = _int(tags.get("context_length")) or facts["max_context_window"]
            if "completion" not in capabilities and "embedding" in capabilities:
                facts["model_type"] = "EMBEDDING"
        if not facts["context_window"]:
            facts["context_window"] = facts["max_context_window"]
    return facts


def _health(latest: dict[str, Any] | None, deployments: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    checks = (latest or {}).get("latest_health_checks") or {}
    ids = {(d.get("model_info") or {}).get("id") for d in deployments}
    found = [c for key, c in checks.items() if isinstance(c, dict) and (key in ids or c.get("model_name") == name)]
    if not found:
        return None
    healthy = all(str(c.get("status") or "").lower() == "healthy" for c in found)
    return {"status": "HEALTHY" if healthy else "UNHEALTHY",
            "latency_ms": _int(found[0].get("response_time_ms")),
            "error": None if healthy else redact_text(next((c.get("error_message") for c in found
                                                            if c.get("error_message")), None), 500)}


def discover(db: Session, user_id: int) -> dict[str, Any]:
    """Refresh the registry from LiteLLM. Changing a known fact bumps the draft, so it shows as a pending change."""
    config.ensure_seeded(db, user_id)
    started = config.utcnow()
    try:
        with config.api_for(db, user_id, service="discovery") as api:
            ready = api.readiness()
            deployments = api.model_info()
            names = set(api.model_names())
            latest = api.health_latest()
    except LiteLLMError as exc:
        conn = config._locked_connection(db, user_id)
        conn.status, conn.last_error, conn.last_checked_at = exc.status if exc.status in (
            "AUTH_FAILED", "UNAVAILABLE") else "DISCONNECTED", redact_text(str(exc), 1000), started
        config.audit(db, user_id, "DISCOVER", result="FAILED", actor=config._actor(db, user_id), error=str(exc))
        db.commit()
        raise config.AIConfigError(f"Model discovery failed: {exc}", status=conn.status) from exc

    groups: dict[str, list[dict[str, Any]]] = {}
    for deployment in deployments:
        groups.setdefault(deployment["model_name"], []).append(deployment)
    names |= set(groups)
    bases = {str((d.get("litellm_params") or {}).get("api_base")) for d in deployments
             if str((d.get("litellm_params") or {}).get("model") or "").startswith("ollama")
             and (d.get("litellm_params") or {}).get("api_base")}
    ollama = {base: ollama_tags(base, user_id=user_id) for base in sorted(bases)}
    owner = owner_tag(user_id)
    rows = {m.litellm_model_name: m for m in db.scalars(select(AIModel).where(AIModel.user_id == user_id)).all()}
    before = {name: config.model_snapshot(row) for name, row in rows.items()}
    added, missing = [], []
    for name in sorted(names):
        facts = describe(groups.get(name, []), ollama)
        row = rows.get(name)
        if row is not None and row.removed_at is not None:
            continue  # a person removed it from Job Hunt OS: a sync leaves it removed (Restore brings it back)
        if row is None:
            row = AIModel(user_id=user_id, litellm_model_name=name, display_name=name, source="DISCOVERED",
                          model_type=facts.get("model_type") or "CUSTOM", created_at=started)
            db.add(row)
            rows[name] = row
            added.append(name)
        for key in FACTS:
            if key == "embedding_dimensions" and facts.get(key) is None and row.embedding_dimensions:
                continue  # measured by an explicit model test; LiteLLM not stating a size does not unlearn it
            setattr(row, key, facts.get(key))
        if facts.get("model_type") and row.source == "DISCOVERED":
            row.model_type = facts["model_type"]
        live = classify_live(groups.get(name, []), owner)
        if any(d.managed_elsewhere for d in live):
            row.managed = False
        row.present_in_litellm = True
        row.last_synced_at = started
        health = _health(latest, groups.get(name, []), name)
        if health:
            row.health_status, row.health_latency_ms, row.health_error = health["status"], health["latency_ms"], \
                health["error"]
            row.health_checked_at = started
        row.updated_at = started
    for name, row in rows.items():
        if name not in names and row.present_in_litellm and row.removed_at is None:
            row.present_in_litellm = False
            missing.append(name)
    db.flush()
    changed = any(config.model_snapshot(row) != before.get(name) for name, row in rows.items() if name in before)
    conn = config._locked_connection(db, user_id)
    if changed or added:
        conn.draft_revision += 1
    conn.status, conn.last_error, conn.last_checked_at = "CONNECTED", None, started
    conn.last_discovery_at = started
    conn.litellm_version = (ready or {}).get("litellm_version") or conn.litellm_version
    config.audit(db, user_id, "DISCOVER", actor=config._actor(db, user_id),
                 changes=[{"added": added, "missing": missing, "models": len(names)}])
    db.commit()
    return {"models": len(names), "added": added, "missing": missing, "facts_changed": changed,
            "litellm_version": conn.litellm_version, "draft_revision": conn.draft_revision,
            "ollama_enriched": sorted(base for base, tags in ollama.items() if tags)}


# ───────────── OpenRouter: choose the top models, then add them to the catalog ─────────────

OPENROUTER_PREFIX = model_resolution.OPENROUTER_PREFIX
OPENROUTER_SOURCE = "OPENROUTER"
OPENROUTER_RANKS = ("cheapest", "context", "newest", "quality")
OPENROUTER_MAX_PICK = 50


def _openrouter_rows(user_id: int, *, refresh: bool = False) -> list[model_market.MarketModel]:
    """OpenRouter's public model list (cached for MODEL_MARKET_CACHE_HOURS); raises when there is none to offer."""
    if not get_settings().model_market_enabled:
        raise config.AIConfigError("Reading public model catalogs is switched off on this server (MODEL_MARKET_ENABLED)")
    state = model_market.load(user_id, ["openrouter"], refresh=refresh)["openrouter"]
    rows = model_market.parse_openrouter(state.data) if state.data is not None else []
    if not rows:
        raise config.AIConfigError(f"Could not read OpenRouter's model list: {state.error or 'it listed no models'}",
                                   status="UNAVAILABLE")
    return rows


def _quality_available() -> bool:
    return bool(get_settings().artificial_analysis_api_key)


def openrouter_candidates(db: Session, user_id: int, criteria: model_market.PickCriteria, *,
                          refresh: bool = False) -> dict[str, Any]:
    """The top `limit` OpenRouter models for the criteria, for the person to choose from. Adds nothing."""
    if criteria.rank not in OPENROUTER_RANKS:
        raise config.AIConfigError(f"Rank by one of: {', '.join(OPENROUTER_RANKS)}")
    if criteria.rank == "quality" and not _quality_available():
        raise config.AIConfigError("Ranking by quality needs ARTIFICIAL_ANALYSIS_API_KEY on the server")
    rows = _openrouter_rows(user_id, refresh=refresh)
    if criteria.rank == "quality":
        state = model_market.load(user_id, ["artificial_analysis"])["artificial_analysis"]
        model_market.attach_benchmarks(rows, model_market.parse_artificial_analysis(state.data)
                                       if state.data is not None else {})
    in_catalog = set(db.scalars(select(AIModel.litellm_model_name).where(
        AIModel.user_id == user_id, AIModel.removed_at.is_(None),
        AIModel.litellm_model_name.startswith(OPENROUTER_PREFIX))).all())
    have = {name[len(OPENROUTER_PREFIX):] for name in in_catalog}
    picked, matching = model_market.pick_top(rows, criteria, have, max_pick=OPENROUTER_MAX_PICK)
    return {"models": picked, "matching": matching, "listed": len(rows),
            "ranks": [r for r in OPENROUTER_RANKS if r != "quality" or _quality_available()]}


def _openrouter_facts(row: model_market.MarketModel) -> dict[str, Any]:
    return {"provider": "openrouter", "underlying_model": row.model_id, "context_window": row.context_window,
            "max_output_tokens": row.max_output_tokens, "supports_structured_output": row.structured_output,
            "supports_tools": row.tools, "supports_reasoning": row.reasoning, "supports_vision": row.vision}


def discover_openrouter(db: Session, user_id: int, ids: list[str]) -> dict[str, Any]:
    """Add the chosen OpenRouter models to the catalog, and refresh what OpenRouter states about the ones already in
    it. Never adds a model nobody chose, and never deletes one OpenRouter stopped listing (an assignment may use it)."""
    config.ensure_seeded(db, user_id)
    chosen = list(dict.fromkeys(str(i).strip() for i in ids or [] if str(i).strip()))
    if len(chosen) > OPENROUTER_MAX_PICK:
        raise config.AIConfigError(f"Add at most {OPENROUTER_MAX_PICK} OpenRouter models at a time")
    started = config.utcnow()
    listed = {row.model_id: row for row in _openrouter_rows(user_id, refresh=True)}
    unknown = [i for i in chosen if i not in listed]
    if unknown:
        raise config.AIConfigError(f"OpenRouter does not list {', '.join(unknown[:5])}", unknown=unknown)
    rows = {m.litellm_model_name: m for m in db.scalars(select(AIModel).where(AIModel.user_id == user_id)).all()}
    before = {name: config.model_snapshot(row) for name, row in rows.items()}
    known = [row.underlying_model for name, row in rows.items()
             if row.source == OPENROUTER_SOURCE and row.removed_at is None and row.underlying_model]
    added, missing = [], []
    for model_id in dict.fromkeys([*chosen, *known]):
        name = OPENROUTER_PREFIX + model_id
        row = rows.get(name)
        if model_id not in listed:
            if row is not None:
                missing.append(name)
            continue
        if row is not None and row.removed_at is not None:
            if model_id not in chosen:
                continue  # a person removed it: a refresh leaves it removed
            row.removed_at, row.enabled = None, True  # choosing it again brings it back usable, as Restore does
        market = listed[model_id]
        if row is None:
            row = AIModel(user_id=user_id, litellm_model_name=name, display_name=market.name or model_id,
                          source=OPENROUTER_SOURCE, model_type="CUSTOM", created_at=started)
            db.add(row)
            rows[name] = row
            added.append(name)
        for key, value in _openrouter_facts(market).items():
            setattr(row, key, value)
        row.last_synced_at = row.updated_at = started
    db.flush()
    changed = any(config.model_snapshot(row) != before.get(name) for name, row in rows.items() if name in before)
    conn = config._locked_connection(db, user_id)
    if changed or added:
        conn.draft_revision += 1
    config.audit(db, user_id, "DISCOVER", actor=config._actor(db, user_id),
                 changes=[{"source": "openrouter", "added": added, "missing": missing, "models": len(chosen)}])
    db.commit()
    return {"models": len(chosen), "added": added, "missing": missing, "facts_changed": changed,
            "draft_revision": conn.draft_revision}


TEST_MAX_TOKENS = 512


# A fixed, fictional input for an embedding model's test: no account data is ever sent.
EMBEDDING_TEST_INPUT = "Job Hunt OS embedding test"


def test_embedding_model(db: Session, user_id: int, name: str, row: AIModel | None) -> dict[str, Any]:
    """Embed one fixed sentence; records health and the vector size the model actually returned."""
    result: dict[str, Any] = {"model": name, "success": False, "latency_ms": None, "model_group": None,
                              "underlying_model": None, "embedding_dimensions": None, "error": None}
    clock = time.monotonic()
    try:
        with config.api_for(db, user_id, service="embedding") as api:
            answer = api.embeddings(name, [EMBEDDING_TEST_INPUT], operation="model_test",
                                    timeout=int(get_settings().rag_embed_timeout_seconds or 60))
        if not answer.vectors or not answer.vectors[0]:
            raise config.AIConfigError("The model returned no vector; it may not be an embedding model")
        size = len(answer.vectors[0])
        result.update(success=True, latency_ms=answer.latency_ms, embedding_dimensions=size,
                      model_group=answer.litellm.get("model_group"),
                      underlying_model=answer.litellm.get("model_name") or answer.model)
        if row is not None:
            row.embedding_dimensions = size  # measured, not guessed
    except (LiteLLMError, config.AIConfigError) as exc:
        result.update(error=redact_text(str(exc), 500), latency_ms=int((time.monotonic() - clock) * 1000))
    if row is not None:
        row.health_status = "HEALTHY" if result["success"] else "UNHEALTHY"
        row.health_checked_at, row.health_latency_ms, row.health_error = config.utcnow(), result["latency_ms"], \
            result["error"]
    config.audit(db, user_id, "MODEL_TEST", actor=config._actor(db, user_id),
                 result="SUCCEEDED" if result["success"] else "FAILED", changes=[{"model": name}],
                 error=result["error"])
    db.commit()
    return result


def test_model(db: Session, user_id: int, name: str) -> dict[str, Any]:
    """A tiny, safe request straight to one model. Records the model's health; no account data is sent."""
    row = db.scalar(select(AIModel).where(AIModel.user_id == user_id, AIModel.litellm_model_name == name))
    if row is not None and row.model_type == "EMBEDDING":
        return test_embedding_model(db, user_id, name, row)
    if row is not None and _direct_openrouter(db, user_id) and model_resolution.is_openrouter_model(
            name, {"provider": row.provider, "underlying_model": row.underlying_model}):
        return _test_model_direct(db, user_id, row)
    prompt = build_model_health_prompt()
    structured = row is None or row.supports_structured_output is not False
    # The same model timeout as real requests (LLM_REQUEST_TIMEOUT_SECONDS): a slow or cold-loading local model
    # should not fail its health test when it would answer a worker in time.
    timeout = int(get_settings().llm_request_timeout_seconds or 120)
    # Room to answer: a reasoning model spends tokens thinking before it writes {"ok": true}, and with a tiny budget
    # its answer comes back empty (Groq then rejects JSON mode: "json_validate_failed"). Still a tiny request.
    body: dict[str, Any] = {"model": name, "messages": prompt.messages(), "max_tokens": TEST_MAX_TOKENS,
                            "temperature": 0, "drop_params": True, "timeout": timeout, "num_retries": 0,
                            "metadata": {"jobhunt": True, "worker_type": "MODEL_TEST"}}
    if row is not None and row.supports_reasoning is True:
        body["reasoning_effort"] = "low"
    if structured:
        body["response_format"] = {"type": "json_object"}
    result: dict[str, Any] = {"model": name, "success": False, "latency_ms": None, "model_group": None,
                              "underlying_model": None, "structured_output": None, "error": None}
    clock = time.monotonic()
    try:
        with config.api_for(db, user_id, service="model_test") as api:
            chat = api.chat(body, operation="model_test", metadata={"model": name}, timeout=timeout + 15)
        content = ((chat.body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        if not content.strip():
            raise config.AIConfigError("The model returned no output. A reasoning model may need more tokens than the "
                                       "test allows, or the model may not answer chat requests")
        result.update(success=True, latency_ms=chat.latency_ms, model_group=chat.litellm.get("model_group"),
                      underlying_model=chat.litellm.get("model_name") or chat.body.get("model"),
                      litellm_call_id=chat.litellm.get("call_id"))
        try:
            parsed = json.loads(content.strip().strip("`").removeprefix("json").strip())
            result["structured_output"] = isinstance(parsed, dict) and parsed.get("ok") is True
        except ValueError:
            result["structured_output"] = False
    except (LiteLLMError, config.AIConfigError) as exc:
        result.update(error=redact_text(str(exc), 500), latency_ms=int((time.monotonic() - clock) * 1000))
    if row is not None:
        row.health_status = "HEALTHY" if result["success"] else "UNHEALTHY"
        row.health_checked_at, row.health_latency_ms, row.health_error = config.utcnow(), result["latency_ms"], \
            result["error"]
    config.audit(db, user_id, "MODEL_TEST", actor=config._actor(db, user_id),
                 result="SUCCEEDED" if result["success"] else "FAILED", changes=[{"model": name}],
                 error=result["error"])
    db.commit()
    return result


def _direct_openrouter(db: Session, user_id: int) -> bool:
    """Workers call OpenRouter directly (the LiteLLM route is off and the direct provider is OpenRouter)."""
    status = config.gateway_status(db, user_id)
    return not status["gateway_enabled"] and status["direct_provider"] == "openrouter"


def _test_model_direct(db: Session, user_id: int, row: AIModel) -> dict[str, Any]:
    """The health test of a model synced from OpenRouter, sent the way a worker would send it on the direct route
    (the LLM layer, with the account's own OpenRouter key and free-only guard). A configuration problem (no key, the
    free-only guard) is reported without calling the model unhealthy."""
    from app.services.llm import LLMCall, LLMClient, LLMConfigError, LLMError

    name = row.litellm_model_name
    worker = model_resolution.ResolvedWorker(worker_type="MODEL_TEST", mode="FIXED", configured=name, model=name)
    route = model_resolution.DirectRoute(worker, str(row.underlying_model), ())
    client = LLMClient.for_user(db, user_id)
    db.commit()  # no transaction across the model call; its telemetry is written from another session
    result: dict[str, Any] = {"model": name, "route": "OPENROUTER", "success": False, "latency_ms": None,
                              "model_group": None, "underlying_model": row.underlying_model,
                              "structured_output": None, "error": None}
    clock = time.monotonic()
    health = True
    try:
        answer = client.generate_prompt(build_model_health_prompt(),
                                        LLMCall(workflow_type="system", worker_type="MODEL_TEST", direct=route))
        result.update(success=True, underlying_model=answer.model or row.underlying_model,
                      structured_output=isinstance(answer.data, dict) and answer.data.get("ok") is True)
    except LLMConfigError as exc:
        health = False
        result["error"] = redact_text(str(exc), 500)
    except (LLMError, ValueError) as exc:
        result["error"] = redact_text(str(exc), 500)
    result["latency_ms"] = int((time.monotonic() - clock) * 1000)
    row = db.get(AIModel, row.id)
    if row is not None and health:
        row.health_status = "HEALTHY" if result["success"] else "UNHEALTHY"
        row.health_checked_at, row.health_latency_ms, row.health_error = config.utcnow(), result["latency_ms"], \
            result["error"]
    config.audit(db, user_id, "MODEL_TEST", actor=config._actor(db, user_id),
                 result="SUCCEEDED" if result["success"] else "FAILED", changes=[{"model": name, "route": "OPENROUTER"}],
                 error=result["error"])
    db.commit()
    return result


def _schema_check(expected: dict[str, str]):
    def check(data: dict[str, Any]) -> dict[str, Any]:
        def wrong(value: Any, kind: str) -> bool:
            # JSON booleans are Python ints; a boolean is only right where the schema asks for one.
            return not isinstance(value, JSON_TYPES[kind]) or (isinstance(value, bool) and kind != "boolean")

        problems = [f"{key} ({kind})" for key, kind in expected.items() if wrong(data.get(key), kind)]
        if problems:
            raise ValueError("missing or mistyped: " + ", ".join(problems))
        return data
    return check


def test_worker(db: Session, user_id: int, worker_type: str, *, use_draft: bool = True) -> dict[str, Any]:
    """Run the worker's fixture task through the real gateway path with the draft (or active) configuration.

    Sends fictional sample data only, never a real application. Recorded in Monitoring like any request.
    """
    from app.services.llm import LLMCall, LLMClient, LLMError

    if worker_type not in WORKERS:
        raise config.NotFound(f"Unknown worker {worker_type}")
    config.ensure_seeded(db, user_id)
    active = config.active_version(db, user_id)
    snapshot = config.draft_snapshot(db, user_id) if use_draft else (active.configuration_snapshot if active else {})
    if WORKERS[worker_type].capability == "EMBEDDING":
        model, _assigned, note = model_resolution.resolve_model(snapshot, worker_type)
        if model is None:
            return {"worker_type": worker_type, "configuration": "draft" if use_draft else "active", "model": None,
                    "success": False, "error": f"No embedding model is assigned ({note})"}
        row = db.scalar(select(AIModel).where(AIModel.user_id == user_id, AIModel.litellm_model_name == model))
        out = test_embedding_model(db, user_id, model, row)
        expected = ((snapshot.get("models") or {}).get(model) or {}).get("embedding_dimensions")
        if out["success"] and expected and out["embedding_dimensions"] != expected:
            out.update(success=False, error=f"{model} returned {out['embedding_dimensions']}-dimension vectors; "
                                            f"the configuration says {expected}")
        return {"worker_type": worker_type, "configuration": "draft" if use_draft else "active", **out}
    resolved = model_resolution.resolve(snapshot, worker_type, None if use_draft else active.version_number)
    prompt, expected = build_worker_test_prompt(worker_type)
    # On the direct OpenRouter route a worker assigned a model synced from OpenRouter is tested the way it runs:
    # straight to OpenRouter. Anything else is tested through the gateway, as before.
    direct = model_resolution.direct_route_for(snapshot, resolved) if _direct_openrouter(db, user_id) else None
    client = (LLMClient.for_user(db, user_id) if direct is not None else
              LLMClient(overrides={"llm_provider": "litellm"}, user_id=user_id,
                        gateway=config.gateway_settings(db, user_id)))
    db.commit()  # no transaction across the model call; its telemetry is written from another session
    out: dict[str, Any] = {"worker_type": worker_type, "configuration": "draft" if use_draft else "active",
                           "model": resolved.model, "assignment": resolved.configured, "note": resolved.note,
                           "success": False, "schema_ok": None, "error": None}
    clock = time.monotonic()
    try:
        result = client.generate_prompt(prompt, LLMCall(workflow_type="system", worker_type=worker_type,
                                                        resolved=resolved, direct=direct,
                                                        validate=_schema_check(expected)))
        out.update(success=True, schema_ok=True, routing=result.routing,
                   answered_by=result.model, latency_ms=(result.routing or {}).get("latency_ms"),
                   input_tokens=result.usage.input_tokens if result.usage else None,
                   output_tokens=result.usage.output_tokens if result.usage else None)
    except ValueError as exc:  # the answer arrived but did not match the fixture's schema
        out.update(schema_ok=False, error=redact_text(str(exc), 500))
    except LLMError as exc:
        out.update(error=redact_text(str(exc), 500), schema_ok=False if "JSON" in str(exc) else None)
    out.setdefault("latency_ms", int((time.monotonic() - clock) * 1000))
    if out.get("latency_ms") is None:
        out["latency_ms"] = int((time.monotonic() - clock) * 1000)
    config.audit(db, user_id, "WORKER_TEST", actor=config._actor(db, user_id),
                 result="SUCCEEDED" if out["success"] else "FAILED",
                 changes=[{"worker_type": worker_type, "configuration": out["configuration"]}], error=out["error"])
    db.commit()
    return out
