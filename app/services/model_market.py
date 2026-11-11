"""The model market: which models are on offer, on which platforms, what they cost and how they perform.

    web catalogs (cached)  OpenRouter, LiteLLM's price list, Artificial Analysis, Groq, Ollama Cloud, local Ollama
    your account (live)    the gateway's models and health, and what your own requests measured (7 days)
    → catalog()            one row per platform and model, every figure with the source it came from
    → rank()               deterministic suggestions for a worker or a use case, compared with what it uses now

Read-only, and it only suggests: changing a worker's model still goes through AI Orchestration's draft → preview →
apply. Nothing here calls a model; catalog text from the web is data and is truncated.

Unknown stays unknown: a price, latency or quality score that no source reported is None, never estimated from a
model's name. Rows from different sources are joined only on an exact normalized key (`model_key`, `base_name`), and
each row lists the sources that contributed. Fetches go through the instrumented client (Monitoring → External APIs);
a failed refresh keeps the last good copy and says it is stale. Never raises to the caller: a source that fails is
reported in `sources()`.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from app.services.external_api import external_client
from app.settings import get_settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger("jobhunt.model_market")

OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
OPENROUTER_ENDPOINTS = "https://openrouter.ai/api/v1/models/{author}/{slug}/endpoints"
LITELLM_PRICES = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
ARTIFICIAL_ANALYSIS = "https://artificialanalysis.ai/api/v2/data/llms/models"
GROQ_MODELS = "https://api.groq.com/openai/v1/models"
OLLAMA_CLOUD_TAGS = "https://ollama.com/api/tags"

MILLION = 1_000_000
TELEMETRY_DAYS = 7
TELEMETRY_MAX_ROWS = 20_000
OWN_SAMPLES = 20  # your own measurements replace web figures from this many requests
DESCRIPTION_LIMIT = 300
ENDPOINTS_LIMIT = 10
# Errors that say nothing about a model (app.services.model_health.NOT_HEALTH).
NOT_HEALTH = ("CONFIGURATION", "CONTEXT_OVERFLOW")

# LiteLLM provider prefixes that name a platform, not part of the model's own id ("groq/openai/gpt-oss-120b").
PLATFORM_PREFIXES = {"openrouter", "groq", "ollama", "ollama_chat", "openai", "anthropic", "gemini", "vertex_ai",
                     "bedrock", "azure", "mistral", "deepseek", "together_ai", "fireworks_ai", "xai", "cerebras",
                     "perplexity", "cohere", "sambanova", "nvidia_nim", "deepinfra", "hosted_vllm"}

OBJECTIVES: dict[str, dict[str, float]] = {
    "balanced": {"cost": 0.25, "latency": 0.2, "quality": 0.25, "reliability": 0.2, "schema": 0.1},
    "cheapest": {"cost": 0.6, "quality": 0.15, "reliability": 0.15, "latency": 0.1},
    "fastest": {"latency": 0.6, "reliability": 0.2, "cost": 0.1, "quality": 0.1},
    "quality": {"quality": 0.6, "schema": 0.15, "reliability": 0.15, "latency": 0.1},
    "reliable": {"reliability": 0.5, "schema": 0.3, "latency": 0.1, "cost": 0.1},
}
METRICS = ("cost", "latency", "quality", "reliability", "schema")
# A model is ranked only when the metrics known for it carry at least this share of the objective's weight.
MIN_KNOWN_WEIGHT = 0.4


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ───────────── rows ─────────────

@dataclass
class MarketModel:
    platform: str  # where it is served: openrouter, groq, ollama_cloud, ollama_local, openai, … or gateway
    model_id: str  # the platform's own id, without the platform prefix
    name: str | None = None
    input_per_million: float | None = None
    output_per_million: float | None = None
    cached_input_per_million: float | None = None
    free: bool | None = None
    pricing_note: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    structured_output: bool | None = None
    tools: bool | None = None
    reasoning: bool | None = None
    vision: bool | None = None
    ttft_seconds: float | None = None  # web: Artificial Analysis median time to first token
    tokens_per_second: float | None = None  # web: Artificial Analysis median output speed
    quality_index: float | None = None  # web: Artificial Analysis intelligence index
    uptime_pct: float | None = None  # web: OpenRouter endpoints, last 30 minutes
    active: bool | None = None  # the platform lists it as served right now
    description: str | None = None
    parameter_size: str | None = None
    # Your gateway and your own requests
    in_gateway: bool = False
    gateway_names: list[str] = field(default_factory=list)
    gateway_health: str | None = None
    resting: bool = False
    used_by: list[str] = field(default_factory=list)
    observed: dict[str, Any] | None = None
    observed_tpm_limit: int | None = None
    sources: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return self.platform, model_key(self.model_id)

    def public(self) -> dict[str, Any]:
        # Unknown (None) and empty fields are left out; a zero price is a fact and stays.
        out = {k: v for k, v in asdict(self).items()
               if not (v is None or v == [] or (v is False and k in ("resting",)))}
        out["id"] = f"{self.platform}/{self.model_id}"
        return out


def model_key(model_id: str | None) -> str:
    """The id a platform serves a model under, normalized only in case: no fuzzy matching."""
    return (model_id or "").strip().lower()


def base_name(model_id: str | None) -> str:
    """The model itself, across platforms ("openai/gpt-oss-120b", "gpt-oss:120b-cloud" → "gpt-oss-120b"). Used only to
    attach model-level facts (benchmark quality and speed) and always reported, so a join can be checked."""
    name = model_key(model_id).rsplit("/", 1)[-1]
    for suffix in (":free", ":cloud", "-cloud"):
        name = name.removesuffix(suffix)
    return name.replace(":", "-")


def split_platform(name: str) -> tuple[str | None, str]:
    """("groq", "openai/gpt-oss-120b") for "groq/openai/gpt-oss-120b"; (None, name) without a platform prefix."""
    head, _, rest = name.partition("/")
    if rest and head.lower() in PLATFORM_PREFIXES:
        return head.lower(), rest
    return None, name


def ollama_platform(name: str) -> str:
    """Ollama serves hosted models (":cloud", "-cloud") and models on your own machine under one API."""
    lowered = name.lower()
    return "ollama_cloud" if lowered.endswith((":cloud", "-cloud")) else "ollama_local"


def ollama_cloud_id(name: str) -> str:
    lowered = name
    for suffix in (":cloud", "-cloud"):
        if lowered.lower().endswith(suffix):
            return lowered[: -len(suffix)]
    return lowered


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None and number > 0 else None


def _per_million(per_token: Any) -> float | None:
    """A per-token price as USD per million tokens. A negative price (OpenRouter's routers) is unknown."""
    number = _num(per_token)
    return round(number * MILLION, 6) if number is not None and number >= 0 else None


def _text(value: Any, limit: int = DESCRIPTION_LIMIT) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


# ───────────── parsers: pure ─────────────

def parse_openrouter(body: Any) -> list[MarketModel]:
    out = []
    for item in (body or {}).get("data") or [] if isinstance(body, dict) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
        top = item.get("top_provider") if isinstance(item.get("top_provider"), dict) else {}
        params = {p for p in item.get("supported_parameters") or [] if isinstance(p, str)}
        modalities = (item.get("architecture") or {}).get("input_modalities") if isinstance(
            item.get("architecture"), dict) else None
        input_price, output_price = _per_million(pricing.get("prompt")), _per_million(pricing.get("completion"))
        free = (True if item["id"].endswith(":free") or (input_price == 0 and output_price == 0)
                else False if input_price is not None and output_price is not None else None)
        out.append(MarketModel(
            platform="openrouter", model_id=item["id"], name=_text(item.get("name"), 120),
            input_per_million=input_price, output_per_million=output_price,
            cached_input_per_million=_per_million(pricing.get("input_cache_read")), free=free,
            context_window=_int(item.get("context_length")) or _int(top.get("context_length")),
            max_output_tokens=_int(top.get("max_completion_tokens")),
            structured_output=True if params & {"structured_outputs", "response_format"} else None if not params
            else False,
            tools=("tools" in params) if params else None, reasoning=("reasoning" in params) if params else None,
            vision=("image" in modalities) if isinstance(modalities, list) else None,
            active=True, description=_text(item.get("description")), sources=["openrouter"]))
    return out


def parse_litellm_prices(body: Any) -> list[MarketModel]:
    out = []
    for name, item in (body.items() if isinstance(body, dict) else []):
        if name == "sample_spec" or not isinstance(item, dict) or item.get("mode") not in (None, "chat", "responses"):
            continue
        provider = item.get("litellm_provider")
        if not isinstance(provider, str) or not provider:
            continue
        prefix, model_id = split_platform(name)
        platform = provider.lower()
        if platform in ("ollama", "ollama_chat"):
            platform = ollama_platform(model_id)
            model_id = ollama_cloud_id(model_id) if platform == "ollama_cloud" else model_id
        elif prefix and prefix != platform and prefix not in ("openai",):
            model_id = name  # an id that carries another platform's prefix is kept whole
        input_price = _per_million(item.get("input_cost_per_token"))
        output_price = _per_million(item.get("output_cost_per_token"))
        out.append(MarketModel(
            platform=platform, model_id=model_id,
            input_per_million=input_price, output_per_million=output_price,
            cached_input_per_million=_per_million(item.get("cache_read_input_token_cost")),
            free=True if input_price == 0 and output_price == 0 else
            (False if input_price is not None and output_price is not None and (input_price or output_price) else None),
            context_window=_int(item.get("max_input_tokens")),
            max_output_tokens=_int(item.get("max_output_tokens")) or _int(item.get("max_tokens")),
            structured_output=item.get("supports_response_schema") if isinstance(
                item.get("supports_response_schema"), bool) else None,
            tools=item.get("supports_function_calling") if isinstance(
                item.get("supports_function_calling"), bool) else None,
            reasoning=item.get("supports_reasoning") if isinstance(item.get("supports_reasoning"), bool) else None,
            vision=item.get("supports_vision") if isinstance(item.get("supports_vision"), bool) else None,
            sources=["litellm_prices"]))
    return out


@dataclass(frozen=True)
class Benchmark:
    name: str
    creator: str | None
    quality_index: float | None
    tokens_per_second: float | None
    ttft_seconds: float | None
    input_per_million: float | None
    output_per_million: float | None


def parse_artificial_analysis(body: Any) -> dict[str, Benchmark]:
    """Model-level benchmarks keyed by `base_name` of the model's slug. A slug two models share is dropped: an
    ambiguous join is no join."""
    seen: dict[str, Benchmark | None] = {}
    for item in (body or {}).get("data") or [] if isinstance(body, dict) else []:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug") or item.get("name")
        if not isinstance(slug, str) or not slug:
            continue
        evaluations = item.get("evaluations") if isinstance(item.get("evaluations"), dict) else {}
        pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
        creator = item.get("model_creator") if isinstance(item.get("model_creator"), dict) else {}
        entry = Benchmark(
            name=_text(item.get("name"), 120) or slug, creator=_text(creator.get("name"), 60),
            quality_index=_num(evaluations.get("artificial_analysis_intelligence_index")),
            tokens_per_second=_num(item.get("median_output_tokens_per_second")) or None,
            ttft_seconds=_num(item.get("median_time_to_first_token_seconds")) or None,
            input_per_million=_num(pricing.get("price_1m_input_tokens")),
            output_per_million=_num(pricing.get("price_1m_output_tokens")))
        key = base_name(slug)
        seen[key] = None if key in seen else entry
    return {k: v for k, v in seen.items() if v is not None}


def parse_groq(body: Any) -> list[MarketModel]:
    out = []
    for item in (body or {}).get("data") or [] if isinstance(body, dict) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        out.append(MarketModel(
            platform="groq", model_id=item["id"], name=item["id"],
            context_window=_int(item.get("context_window")), max_output_tokens=_int(item.get("max_completion_tokens")),
            active=item.get("active") if isinstance(item.get("active"), bool) else None,
            pricing_note=None, sources=["groq"]))
    return out


def parse_ollama_cloud(body: Any) -> list[MarketModel]:
    out = []
    for item in (body or {}).get("models") or [] if isinstance(body, dict) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("model")
        if not isinstance(name, str) or not name:
            continue
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        out.append(_ollama_row("ollama_cloud", ollama_cloud_id(name), details, item.get("capabilities"),
                               "ollama_cloud"))
    return out


def parse_ollama_local(tags: dict[str, dict[str, Any]]) -> list[MarketModel]:
    """Rows from app.services.litellm_client.ollama_tags. A cloud model your Ollama lists is still a cloud model."""
    out = []
    for name, facts in (tags or {}).items():
        platform = ollama_platform(name)
        row = _ollama_row(platform, ollama_cloud_id(name) if platform == "ollama_cloud" else name,
                          {"parameter_size": facts.get("parameter_size")}, facts.get("capabilities"), "ollama_local")
        row.context_window = _int(facts.get("context_length"))
        out.append(row)
    return out


def _ollama_row(platform: str, model_id: str, details: dict[str, Any], capabilities: Any, source: str) -> MarketModel:
    caps = {c for c in capabilities or [] if isinstance(c, str)} if isinstance(capabilities, list) else None
    local = platform == "ollama_local"
    return MarketModel(
        platform=platform, model_id=model_id, name=model_id,
        input_per_million=0.0 if local else None, output_per_million=0.0 if local else None, free=True if local else None,
        pricing_note=("runs on your own hardware" if local else
                      "Ollama Cloud is a subscription with usage caps, not priced per token"),
        tools=("tools" in caps) if caps else None, reasoning=("thinking" in caps) if caps else None,
        vision=("vision" in caps) if caps else None, parameter_size=_text(details.get("parameter_size"), 20),
        active=True, sources=[source])


def parse_openrouter_endpoints(body: Any) -> list[dict[str, Any]]:
    """Per-provider facts for one OpenRouter model, as reported; a figure it does not report is None."""
    data = body.get("data") if isinstance(body, dict) else None
    out = []
    for item in (data or {}).get("endpoints") or [] if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}

        def figure(*names: str, item: dict[str, Any] = item) -> float | None:
            for name in names:
                value = item.get(name)
                if isinstance(value, dict):
                    value = value.get("p50", value.get("median"))
                if (number := _num(value)) is not None:
                    return number
            return None

        out.append({"provider": _text(item.get("provider_name") or item.get("name"), 80),
                    "input_per_million": _per_million(pricing.get("prompt")),
                    "output_per_million": _per_million(pricing.get("completion")),
                    "context_window": _int(item.get("context_length")),
                    "max_output_tokens": _int(item.get("max_completion_tokens")),
                    "uptime_pct": figure("uptime_last_1d", "uptime_last_30m"),
                    "uptime_last_30m_pct": figure("uptime_last_30m"),
                    "quantization": _text(item.get("quantization"), 20),
                    "latency_ms": figure("latency_last_30m", "latency"),
                    "throughput_tps": figure("throughput_last_30m", "throughput"),
                    "status": item.get("status") if isinstance(item.get("status"), (int, str)) else None})
    return out


# ───────────── merging: pure ─────────────

FILL = ("name", "input_per_million", "output_per_million", "cached_input_per_million", "free", "pricing_note",
        "context_window", "max_output_tokens", "structured_output", "tools", "reasoning", "vision", "active",
        "description", "parameter_size")


def merge(*groups: Iterable[MarketModel]) -> dict[tuple[str, str], MarketModel]:
    """One row per (platform, model id). Earlier groups win a figure; later ones only fill what is still unknown."""
    rows: dict[tuple[str, str], MarketModel] = {}
    for group in groups:
        for row in group:
            known = rows.get(row.key)
            if known is None:
                rows[row.key] = replace(row, sources=list(row.sources), gateway_names=list(row.gateway_names),
                                        used_by=list(row.used_by))
                continue
            for name in FILL:
                if getattr(known, name) is None and getattr(row, name) is not None:
                    setattr(known, name, getattr(row, name))
            known.sources += [s for s in row.sources if s not in known.sources]
    return rows


def attach_benchmarks(rows: Iterable[MarketModel], benchmarks: dict[str, Benchmark]) -> None:
    for row in rows:
        bench = benchmarks.get(base_name(row.model_id))
        if bench is None:
            continue
        row.quality_index, row.tokens_per_second, row.ttft_seconds = (
            bench.quality_index, bench.tokens_per_second, bench.ttft_seconds)
        row.sources.append(f"artificial_analysis:{base_name(row.model_id)}")


# ───────────── fetching (cached) ─────────────

@dataclass
class SourceState:
    name: str
    label: str
    configured: bool = True
    enabled: bool = True
    fetched_at: datetime | None = None
    expires: float = 0.0
    count: int = 0
    stale: bool = False
    error: str | None = None
    data: Any = None

    def public(self) -> dict[str, Any]:
        return {"source": self.name, "label": self.label, "configured": self.configured, "enabled": self.enabled,
                "models": self.count, "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
                "stale": self.stale, "error": self.error}


_lock = threading.Lock()
_states: dict[str, SourceState] = {}
_transport: httpx.BaseTransport | None = None  # tests inject an httpx.MockTransport


def reset_cache() -> None:
    with _lock:
        _states.clear()
        _endpoints.clear()


LABELS = {"openrouter": "OpenRouter models API", "litellm_prices": "LiteLLM price list",
          "artificial_analysis": "Artificial Analysis benchmarks", "groq": "Groq models API",
          "ollama_cloud": "Ollama Cloud catalog", "ollama_local": "Your local Ollama"}


def _get_json(provider: str, operation: str, url: str, *, user_id: int | None, headers: dict[str, str] | None = None,
              timeout: float = 20) -> Any:
    extra = {"transport": _transport} if _transport is not None else {}
    with external_client(provider, service="model_market", operation=operation, user_id=user_id, timeout=timeout,
                         headers=headers, follow_redirects=False, **extra) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()


def _fetchers(user_id: int | None) -> dict[str, tuple[bool, Callable[[], Any], Callable[[Any], int]]]:
    """source → (configured, fetch, count)."""
    settings = get_settings()

    def bearer(key: str | None) -> dict[str, str] | None:
        return {"Authorization": f"Bearer {key}"} if key else None

    def local() -> Any:
        from app.services.litellm_client import ollama_tags

        tags = ollama_tags(settings.ollama_base_url, user_id=user_id)
        if not tags:
            raise RuntimeError(f"no models listed by the Ollama server at {settings.ollama_base_url} (is it running?)")
        return tags

    return {
        "openrouter": (True, lambda: _get_json("OPENROUTER_CATALOG", "list_models", OPENROUTER_MODELS,
                                               user_id=user_id), lambda d: len(parse_openrouter(d))),
        "litellm_prices": (True, lambda: _get_json("LITELLM_PRICES", "price_list", LITELLM_PRICES, user_id=user_id,
                                                   timeout=30), lambda d: len(d) if isinstance(d, dict) else 0),
        "artificial_analysis": (bool(settings.artificial_analysis_api_key),
                                lambda: _get_json("ARTIFICIAL_ANALYSIS", "list_models", ARTIFICIAL_ANALYSIS,
                                                  user_id=user_id,
                                                  headers={"x-api-key": settings.artificial_analysis_api_key or ""}),
                                lambda d: len(parse_artificial_analysis(d))),
        "groq": (bool(settings.groq_api_key),
                 lambda: _get_json("GROQ_CATALOG", "list_models", GROQ_MODELS, user_id=user_id,
                                   headers=bearer(settings.groq_api_key)), lambda d: len(parse_groq(d))),
        "ollama_cloud": (True, lambda: _get_json("OLLAMA_CLOUD_CATALOG", "list_models", OLLAMA_CLOUD_TAGS,
                                                 user_id=user_id, headers=bearer(settings.ollama_api_key)),
                         lambda d: len(parse_ollama_cloud(d))),
        "ollama_local": (bool(settings.ollama_base_url), local, lambda d: len(d) if isinstance(d, dict) else 0),
    }


def _describe(name: str, exc: BaseException) -> str:
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    if status in (401, 403):
        key = {"ollama_cloud": "OLLAMA_API_KEY", "groq": "GROQ_API_KEY",
               "artificial_analysis": "ARTIFICIAL_ANALYSIS_API_KEY"}.get(name)
        return f"refused ({status})" + (f": needs {key} in .env" if key else "")
    if status:
        return f"answered HTTP {status}"
    return (str(exc) or type(exc).__name__)[:200]


def load(user_id: int | None, names: Iterable[str] | None = None, *, refresh: bool = False) -> dict[str, SourceState]:
    """Every source's state, fetching the ones whose cache expired. A failure keeps the last good data (stale)."""
    settings = get_settings()
    ttl = max(0.0, float(settings.model_market_cache_hours)) * 3600
    fetchers = _fetchers(user_id)
    wanted = list(names) if names is not None else list(fetchers)
    out: dict[str, SourceState] = {}
    for name in wanted:
        configured, fetch, count = fetchers[name]
        with _lock:
            state = _states.setdefault(name, SourceState(name, LABELS[name]))
        state.configured, state.enabled = configured, settings.model_market_enabled or name == "ollama_local"
        if not (state.configured and state.enabled):
            state.error = None if state.configured else "not configured"
            out[name] = state
            continue
        now = time.monotonic()
        if refresh or state.data is None or state.expires <= now:
            try:
                data = fetch()
                state.data, state.count, state.fetched_at = data, count(data), utcnow()
                state.stale, state.error = False, None
            except Exception as exc:  # noqa: BLE001  one source failing never takes the others down
                log.info("model market source %s failed: %s", name, exc)
                state.error, state.stale = _describe(name, exc), state.data is not None
            state.expires = now + ttl
        out[name] = state
    return out


def sources(user_id: int | None) -> list[dict[str, Any]]:
    return [state.public() for state in load(user_id).values()]


_endpoints: dict[str, tuple[float, dict[str, Any]]] = {}
ENDPOINTS_CACHE_SECONDS = 1800.0  # uptime moves faster than a catalog


def openrouter_endpoints(model_id: str, user_id: int | None) -> dict[str, Any]:
    """Per-provider figures for one OpenRouter model (price, context, uptime; OpenRouter reports no latency here),
    cached for half an hour. Fetched for a handful of models at a time, never the whole catalog."""
    author, _, slug = model_id.partition("/")
    if not author or not slug:
        return {"endpoints": [], "error": "not an OpenRouter author/slug id"}
    now = time.monotonic()
    with _lock:
        hit = _endpoints.get(model_id)
    if hit and hit[0] > now:
        return hit[1]
    url = OPENROUTER_ENDPOINTS.format(author=quote(author, safe=""), slug=quote(slug, safe=":."))
    try:
        result: dict[str, Any] = {"endpoints": parse_openrouter_endpoints(
            _get_json("OPENROUTER_CATALOG", "model_endpoints", url, user_id=user_id))}
    except Exception as exc:  # noqa: BLE001
        return {"endpoints": [], "error": _describe("openrouter", exc)}
    with _lock:
        _endpoints[model_id] = (now + ENDPOINTS_CACHE_SECONDS, result)
    return result


def best_uptime(endpoints: list[dict[str, Any]]) -> float | None:
    """OpenRouter sends a request to a provider that is up, so a model is as available as its best endpoint."""
    known = [e["uptime_pct"] for e in endpoints if e.get("uptime_pct") is not None]
    return round(max(known), 1) if known else None


def shortlist(rows: list[MarketModel], c: Constraints, r: Requirements | None, n: int = ENDPOINTS_LIMIT) -> list[
        MarketModel]:
    """The OpenRouter candidates worth an uptime lookup before ranking: those passing the filters with a known cost,
    spread evenly over the price range. Bounded, so a suggestion makes at most `n` extra requests."""
    picked = [row for row in rows if row.platform == "openrouter" and row.uptime_pct is None and matches(row, c, r)[0]]
    priced = sorted((row for row in picked if cost_per_request(row, r) is not None),
                    key=lambda row: (cost_per_request(row, r), row.model_id))
    if len(priced) <= n:
        return priced
    # Evenly across the price range, cheapest and dearest included: cheapest-first alone would only ever give free
    # models a reliability figure, and they would win every ranking on price.
    step = (len(priced) - 1) / (n - 1) if n > 1 else 0
    return [priced[round(i * step)] for i in range(n)]


def enrich_uptime(rows: list[MarketModel], c: Constraints, r: Requirements | None, user_id: int | None,
                  n: int = ENDPOINTS_LIMIT) -> int:
    """Fill uptime for a shortlist of OpenRouter candidates. Returns how many got a figure."""
    filled = 0
    for row in shortlist(rows, c, r, n):
        uptime = best_uptime(openrouter_endpoints(row.model_id, user_id).get("endpoints") or [])
        if uptime is not None:
            row.uptime_pct, filled = uptime, filled + 1
            if "openrouter_endpoints" not in row.sources:
                row.sources.append("openrouter_endpoints")
    return filled


# ───────────── your account: gateway and telemetry ─────────────

def _percentile(values: list[float], pct: float) -> float | None:
    from app.services.model_health import percentile

    return percentile([int(v) for v in values], pct) if values else None


def telemetry(db: Session, user_id: int, *, now: datetime | None = None) -> dict[str, Any]:
    """What this account's own gateway requests measured over the last week: per model and per worker. Bounded, and
    filtered by user_id explicitly (telemetry is not tenancy-filtered automatically)."""
    from sqlalchemy import or_, select

    from app.models import LLMRequestLog

    since = (now or utcnow()) - timedelta(days=TELEMETRY_DAYS)
    rows = db.execute(select(
        LLMRequestLog.requested_model, LLMRequestLog.worker_type, LLMRequestLog.status, LLMRequestLog.error_type,
        LLMRequestLog.latency_ms, LLMRequestLog.input_tokens, LLMRequestLog.output_tokens,
        LLMRequestLog.estimated_cost, LLMRequestLog.cost_source, LLMRequestLog.structured_output_valid,
    ).where(LLMRequestLog.user_id == user_id, LLMRequestLog.provider == "litellm", LLMRequestLog.created_at >= since,
            LLMRequestLog.requested_model.is_not(None),
            or_(LLMRequestLog.cost_source.is_(None), LLMRequestLog.cost_source != "not_sent"))
        .order_by(LLMRequestLog.created_at.desc()).limit(TELEMETRY_MAX_ROWS)).all()
    by_model: dict[str, list[Any]] = defaultdict(list)
    by_worker: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        if row.status == "FAILED" and row.error_type in NOT_HEALTH:
            continue
        by_model[row.requested_model].append(row)
        if row.worker_type:
            by_worker[row.worker_type].append(row)
    from sqlalchemy import func

    free_quota = db.scalar(select(func.count(LLMRequestLog.id)).where(
        LLMRequestLog.user_id == user_id, LLMRequestLog.created_at >= since, LLMRequestLog.error_type == "RATE_LIMIT",
        LLMRequestLog.error_message.ilike("%free-models-per-day%"))) or 0
    return {"models": {m: _summary(items) for m, items in by_model.items()},
            "workers": {w: _tokens(items) for w, items in by_worker.items()}, "days": TELEMETRY_DAYS,
            "openrouter_free_quota_429s": int(free_quota)}


def _summary(items: list[Any]) -> dict[str, Any]:
    ok = [r for r in items if r.status == "SUCCEEDED"]
    latencies = [r.latency_ms for r in ok if r.latency_ms is not None]
    judged = [r.structured_output_valid for r in items if r.structured_output_valid is not None]
    costs = [float(r.estimated_cost) for r in ok if r.estimated_cost is not None and r.cost_source != "unknown"]
    failures = len(items) - len(ok)
    return {"samples": len(items), "failure_pct": round(100 * failures / len(items), 1) if items else None,
            "rate_limits": sum(1 for r in items if r.error_type == "RATE_LIMIT"),
            "p50_ms": _percentile(latencies, 50), "p95_ms": _percentile(latencies, 95),
            "schema_valid_pct": round(100 * sum(judged) / len(judged), 1) if judged else None,
            "cost_per_request": round(sum(costs) / len(costs), 6) if costs else None}


def _tokens(items: list[Any]) -> dict[str, Any]:
    ok = [r for r in items if r.status == "SUCCEEDED"]
    ins = [r.input_tokens for r in ok if r.input_tokens]
    outs = [r.output_tokens for r in ok if r.output_tokens]
    return {"samples": len(ok), "p90_input_tokens": _percentile(ins, 90), "p90_output_tokens": _percentile(outs, 90),
            "p50_input_tokens": _percentile(ins, 50), "p50_output_tokens": _percentile(outs, 50)}


def gateway_rows(db: Session, user_id: int, measured: dict[str, Any]) -> list[MarketModel]:
    """This account's gateway models as rows on the platform that serves them, with what your requests measured."""
    from sqlalchemy import select

    from app.models import AIModel, ProviderRateWindow
    from app.services import model_health, model_resolution

    models = db.scalars(select(AIModel).where(AIModel.user_id == user_id, AIModel.removed_at.is_(None),
                                              AIModel.enabled.is_(True)).limit(500)).all()
    snapshot = model_resolution.active_snapshot(user_id)[1]
    users: dict[str, list[str]] = defaultdict(list)
    for worker in (snapshot.get("workers") or {}):
        resolved = model_resolution.resolve(snapshot, worker)
        for name in {resolved.model, *resolved.chain()} - {None}:
            users[name].append(worker)
    windows = {row.provider: row.tokens_limit for row in db.scalars(
        select(ProviderRateWindow).where(ProviderRateWindow.user_id == user_id).limit(500)).all()}
    out = []
    for m in models:
        if m.model_type == "EMBEDDING":
            continue
        provider = (m.provider or "").lower()
        underlying = m.underlying_model or m.litellm_model_name
        if provider in ("ollama", "ollama_chat"):
            platform = ollama_platform(underlying)
            model_id = ollama_cloud_id(underlying) if platform == "ollama_cloud" else underlying
        elif provider and provider not in ("litellm",) and m.model_type != "AUTO_ROUTER":
            platform, model_id = provider, underlying
        else:  # an auto router or a model whose provider nobody reported: only the gateway knows it
            platform, model_id = "gateway", m.litellm_model_name
        out.append(MarketModel(
            platform=platform, model_id=model_id, name=m.display_name or m.litellm_model_name,
            context_window=m.context_window, max_output_tokens=m.max_output_tokens,
            structured_output=m.supports_structured_output, tools=m.supports_tools, reasoning=m.supports_reasoning,
            vision=m.supports_vision, in_gateway=True, gateway_names=[m.litellm_model_name],
            gateway_health=m.health_status, resting=model_health.resting(user_id, m.litellm_model_name),
            used_by=sorted(users.get(m.litellm_model_name, [])), observed=measured.get(m.litellm_model_name),
            observed_tpm_limit=windows.get(f"LITELLM:{m.litellm_model_name}"[:230]),
            pricing_note=("an auto router: what it costs depends on the model it picks" if m.model_type == "AUTO_ROUTER"
                          else "Ollama Cloud is a subscription with usage caps, not priced per token"
                          if platform == "ollama_cloud" else None), sources=["gateway"]))
    return out


def _attach_gateway(rows: dict[tuple[str, str], MarketModel], gateway: list[MarketModel]) -> None:
    for g in gateway:
        known = rows.get(g.key)
        if known is None:
            rows[g.key] = g
            continue
        for name in FILL:  # what the web says wins for prices; the gateway fills capabilities it reported
            if getattr(known, name) is None and getattr(g, name) is not None:
                setattr(known, name, getattr(g, name))
        known.in_gateway = True
        known.gateway_names += [n for n in g.gateway_names if n not in known.gateway_names]
        known.gateway_health = known.gateway_health or g.gateway_health
        known.resting = known.resting or g.resting
        known.used_by = sorted({*known.used_by, *g.used_by})
        known.observed = known.observed or g.observed  # several gateway names for one model: the first measured
        known.observed_tpm_limit = known.observed_tpm_limit or g.observed_tpm_limit
        known.sources.append("gateway")


# ───────────── the catalog ─────────────

@dataclass
class Catalog:
    rows: list[MarketModel]
    sources: list[dict[str, Any]]
    telemetry: dict[str, Any]
    benchmarks: bool


def catalog(db: Session, user_id: int, *, refresh: bool = False) -> Catalog:
    states = load(user_id, refresh=refresh)

    def data(name: str) -> Any:
        state = states.get(name)
        return state.data if state is not None and state.enabled and state.configured else None

    # Order is precedence: a platform's own API beats the price list for what it reports about itself.
    rows = merge(parse_openrouter(data("openrouter")), parse_groq(data("groq")),
                 parse_ollama_cloud(data("ollama_cloud")), parse_ollama_local(data("ollama_local") or {}),
                 parse_litellm_prices(data("litellm_prices")))
    benchmarks = parse_artificial_analysis(data("artificial_analysis"))
    measured: dict[str, Any] = {"models": {}, "workers": {}, "days": TELEMETRY_DAYS}
    try:
        measured = telemetry(db, user_id)
        _attach_gateway(rows, gateway_rows(db, user_id, measured["models"]))
    except Exception:
        log.warning("model market could not read this account's gateway or telemetry", exc_info=True)
    attach_benchmarks(rows.values(), benchmarks)
    return Catalog(list(rows.values()), [s.public() for s in states.values()], measured, bool(benchmarks))


# ───────────── filtering and ranking: pure ─────────────

@dataclass(frozen=True)
class Constraints:
    query: str | None = None
    providers: tuple[str, ...] = ()
    exclude_providers: tuple[str, ...] = ()
    free_only: bool = False
    min_context: int | None = None
    needs_structured_output: bool = False
    needs_tools: bool = False
    needs_reasoning: bool = False
    max_input_cost_per_million: float | None = None
    max_cost_per_request: float | None = None
    in_gateway_only: bool = False


@dataclass(frozen=True)
class Requirements:
    worker_type: str | None = None
    label: str | None = None
    structured_output: bool = False
    min_context: int | None = None
    input_tokens: int | None = None  # typical request (p90) used for cost per request
    output_tokens: int | None = None
    basis: str = "none"  # "telemetry" (your requests) | "registry" | "none"


def _platform_matches(platform: str, wanted: Iterable[str]) -> bool:
    platform = platform.lower()
    return any(platform == w or platform.startswith(w + "_") or (w == "ollama" and platform.startswith("ollama"))
               for w in (x.lower().strip() for x in wanted if x))


def matches(row: MarketModel, c: Constraints, r: Requirements | None = None) -> tuple[bool, list[str]]:
    """Whether a row passes the hard filters, and the notes on requirements it might not meet (unknown support).
    Only a *known* shortfall excludes: unknown is kept and said."""
    notes: list[str] = []
    if c.query and c.query.lower() not in f"{row.platform}/{row.model_id} {row.name or ''}".lower():
        return False, notes
    if c.providers and not _platform_matches(row.platform, c.providers):
        return False, notes
    if c.exclude_providers and _platform_matches(row.platform, c.exclude_providers):
        return False, notes
    if c.free_only and row.free is not True:
        return False, notes
    if c.in_gateway_only and not row.in_gateway:
        return False, notes
    need_context = max(c.min_context or 0, (r.min_context if r else None) or 0)
    if need_context:
        if row.context_window is not None and row.context_window < need_context:
            return False, notes
        if row.context_window is None:
            notes.append("CONTEXT_UNKNOWN")
    for wanted, value, code in ((c.needs_structured_output or (r and r.structured_output), row.structured_output,
                                 "STRUCTURED_OUTPUT_UNKNOWN"), (c.needs_tools, row.tools, "TOOLS_UNKNOWN"),
                                (c.needs_reasoning, row.reasoning, "REASONING_UNKNOWN")):
        if wanted and value is False:
            return False, notes
        if wanted and value is None:
            notes.append(code)
    if c.max_input_cost_per_million is not None and (
            row.input_per_million is None or row.input_per_million > c.max_input_cost_per_million):
        return False, notes
    if c.max_cost_per_request is not None:
        cost = cost_per_request(row, r)
        if cost is None or cost > c.max_cost_per_request:
            return False, notes
    return True, notes


def cost_per_request(row: MarketModel, r: Requirements | None) -> float | None:
    """The request's estimated cost from list prices and the worker's typical tokens; your measured cost per request
    wins when there are enough samples. None when either side is unknown."""
    if row.observed and (row.observed.get("samples") or 0) >= OWN_SAMPLES and \
            row.observed.get("cost_per_request") is not None:
        return float(row.observed["cost_per_request"])
    if row.free is True:
        return 0.0
    if row.input_per_million is None or row.output_per_million is None:
        return None
    inp = (r.input_tokens if r and r.input_tokens else None) or 2000
    out = (r.output_tokens if r and r.output_tokens else None) or 500
    return round((inp * row.input_per_million + out * row.output_per_million) / MILLION, 8)


def latency_seconds(row: MarketModel, r: Requirements | None) -> tuple[float | None, str | None]:
    """Typical time to a full answer: your own p50 when measured enough, else benchmark TTFT + output at the
    benchmark's speed. None when neither is known."""
    observed = row.observed or {}
    if (observed.get("samples") or 0) >= OWN_SAMPLES and observed.get("p50_ms") is not None:
        return round(observed["p50_ms"] / 1000, 2), "yours"
    if row.ttft_seconds is not None and row.tokens_per_second:
        out = (r.output_tokens if r and r.output_tokens else None) or 500
        return round(row.ttft_seconds + out / row.tokens_per_second, 2), "artificial_analysis"
    return None, None


def metric_values(row: MarketModel, r: Requirements | None) -> dict[str, float | None]:
    """Each metric as a number where *higher is better*, or None when unknown."""
    observed = row.observed or {}
    enough = (observed.get("samples") or 0) >= OWN_SAMPLES
    cost = cost_per_request(row, r)
    latency, _ = latency_seconds(row, r)
    reliability = (100 - observed["failure_pct"] if enough and observed.get("failure_pct") is not None
                   else row.uptime_pct)
    schema = observed.get("schema_valid_pct") if enough else None
    return {"cost": -cost if cost is not None else None, "latency": -latency if latency is not None else None,
            "quality": row.quality_index, "reliability": reliability, "schema": schema}


def _percentiles(values: dict[int, float]) -> dict[int, float]:
    """Each candidate's standing among those with the metric known: 0 worst … 1 best; ties share a standing."""
    if not values:
        return {}
    ordered = sorted(set(values.values()))
    if len(ordered) == 1:
        return dict.fromkeys(values, 0.5)
    rank = {v: i / (len(ordered) - 1) for i, v in enumerate(ordered)}
    return {k: rank[v] for k, v in values.items()}


def rank(rows: list[MarketModel], objective: str, c: Constraints, r: Requirements | None = None,
         limit: int = 5, *, free_quota_429s: int = 0) -> dict[str, Any]:
    weights = OBJECTIVES.get(objective)
    if weights is None:
        raise ValueError(f"objective must be one of {', '.join(OBJECTIVES)}")
    candidates: list[tuple[MarketModel, list[str]]] = []
    for row in rows:
        ok, notes = matches(row, c, r)
        if ok:
            candidates.append((row, notes))
    values = [metric_values(row, r) for row, _ in candidates]
    standing = {m: _percentiles({i: v[m] for i, v in enumerate(values) if v[m] is not None}) for m in METRICS}
    total = sum(weights.values())
    scored, insufficient = [], []
    for i, (row, notes) in enumerate(candidates):
        known = {m: standing[m][i] for m in weights if i in standing[m]}
        known_weight = sum(weights[m] for m in known) / total
        entry = (row, notes, values[i], known, known_weight)
        if known_weight < MIN_KNOWN_WEIGHT or len(known) < 2:
            insufficient.append(entry)
            continue
        raw = sum(weights[m] * known[m] for m in known) / sum(weights[m] for m in known)
        scored.append((round(100 * raw * (0.5 + 0.5 * known_weight), 1), entry))
    # Ties: more metrics known, then your gateway's own, then more context, then the id (stable).
    scored.sort(key=lambda s: (-s[0], -len(s[1][3]), not s[1][0].in_gateway, -(s[1][0].context_window or 0),
                               s[1][0].platform, s[1][0].model_id))
    insufficient.sort(key=lambda e: (-e[4], not e[0].in_gateway, e[0].platform, e[0].model_id))
    top = [_suggestion(row, notes, vals, known, kw, score, r, weights, free_quota_429s)
           for score, (row, notes, vals, known, kw) in scored[:limit]]
    notes = []
    if "quality" in weights and not standing["quality"]:
        notes.append("No candidate has a known quality score, so this ranking cannot tell a small model from a strong "
                     "one: it favours cheap, available models. Add ARTIFICIAL_ANALYSIS_API_KEY for benchmark scores, "
                     "or narrow with min_context / needs_* / provider. Quality was left out, not counted as zero.")
    return {"objective": objective, "weights": weights, "candidates": len(candidates), "suggestions": top,
            "insufficient_data": [_brief(e[0], e[4]) for e in insufficient[: max(0, limit - len(top))]] if
            len(top) < limit else [], "notes": notes}


def _brief(row: MarketModel, known_weight: float) -> dict[str, Any]:
    return {"id": f"{row.platform}/{row.model_id}", "in_gateway": row.in_gateway,
            "coverage": round(known_weight, 2), "why": "too few of this objective's metrics are known to rank it"}


def _suggestion(row: MarketModel, notes: list[str], values: dict[str, float | None], known: dict[str, float],
                known_weight: float, score: float, r: Requirements | None, weights: dict[str, float],
                free_quota_429s: int = 0) -> dict[str, Any]:
    cost = cost_per_request(row, r)
    latency, latency_source = latency_seconds(row, r)
    reasons = [f"{m}: better than {round(100 * v)}% of candidates"
               for m, v in sorted(known.items(), key=lambda kv: -weights[kv[0]])]
    reasons += [f"{m} unknown" for m in weights if m not in known]
    if row.resting:
        notes = [*notes, "RESTING_IN_YOUR_GATEWAY"]
    if row.platform == "openrouter" and row.model_id.endswith(":free"):
        # OpenRouter's free models share one daily request quota per account ("free-models-per-day").
        notes = [*notes, "SHARES_OPENROUTER_FREE_DAILY_QUOTA"]
        if free_quota_429s:
            reasons.append(f"your account hit OpenRouter's free daily quota {free_quota_429s} times in "
                           f"{TELEMETRY_DAYS} days; this model draws on the same quota")
    return {
        "id": f"{row.platform}/{row.model_id}", "platform": row.platform, "model_id": row.model_id, "name": row.name,
        "score": score, "coverage": f"{len(known)} of {len(weights)} metrics known",
        "cost_per_request_usd": cost, "cost_per_1k_requests_usd": round(cost * 1000, 4) if cost is not None else None,
        "input_per_million": row.input_per_million, "output_per_million": row.output_per_million,
        "free": row.free, "pricing_note": row.pricing_note,
        "latency_seconds": latency, "latency_source": latency_source, "ttft_seconds": row.ttft_seconds,
        "tokens_per_second": row.tokens_per_second, "quality_index": row.quality_index, "uptime_pct": row.uptime_pct,
        "context_window": row.context_window, "structured_output": row.structured_output, "tools": row.tools,
        "reasoning": row.reasoning, "observed": row.observed, "observed_tpm_limit": row.observed_tpm_limit,
        "in_gateway": row.in_gateway, "gateway_names": row.gateway_names, "used_by": row.used_by,
        "reasons": reasons, "flags": notes, "sources": row.sources,
    }


# ───────────── requirements and comparison ─────────────

def requirements_for(worker_type: str | None, measured: dict[str, Any], snapshot: dict[str, Any],
                     label: str | None = None) -> Requirements:
    """What a worker needs, from the worker registry, its configuration and its own requests — never a guess."""
    from app.services.ai_workers import WORKERS

    if not worker_type:
        return Requirements(label=label)
    worker = WORKERS.get(worker_type)
    if worker is None:
        raise ValueError(f"unknown worker_type {worker_type}; one of {', '.join(sorted(WORKERS))}")
    tokens = (measured.get("workers") or {}).get(worker_type) or {}
    configured = ((snapshot.get("workers") or {}).get(worker_type) or {}).get("max_output_tokens")
    inp, out = tokens.get("p90_input_tokens"), tokens.get("p90_output_tokens") or configured
    need = max(worker.min_context_tokens or 0, (inp or 0) + (out or 0)) or None
    return Requirements(worker_type=worker_type, label=label or worker.label,
                        structured_output=worker.structured_output_required, min_context=need, input_tokens=inp,
                        output_tokens=out, basis="telemetry" if inp else "registry")


def current_model(rows: list[MarketModel], user_id: int, worker_type: str, r: Requirements) -> dict[str, Any] | None:
    from app.services import model_resolution

    resolved = model_resolution.get(user_id, worker_type)
    if not resolved.model:
        return None
    row = next((x for x in rows if resolved.model in x.gateway_names), None)
    if row is None:
        return {"gateway_name": resolved.model, "known": False}
    cost = cost_per_request(row, r)
    latency, source = latency_seconds(row, r)
    return {"gateway_name": resolved.model, "id": f"{row.platform}/{row.model_id}", "known": True,
            "cost_per_request_usd": cost, "latency_seconds": latency, "latency_source": source,
            "quality_index": row.quality_index, "observed": row.observed, "backups": resolved.chain(),
            "escalation": resolved.escalation.model if resolved.escalation else None}


def compare(suggestions: list[dict[str, Any]], current: dict[str, Any] | None) -> None:
    """Adds each suggestion's difference from the current model, where both figures are known."""
    if not current or not current.get("known"):
        return
    for s in suggestions:
        delta: dict[str, Any] = {}
        a, b = s.get("cost_per_request_usd"), current.get("cost_per_request_usd")
        if a is not None and b is not None:
            delta["cost"] = ("same" if a == b else f"{round(100 * (a - b) / b)}% vs current" if b
                             else f"+${a:.6f} per request vs free current")
        a, b = s.get("latency_seconds"), current.get("latency_seconds")
        if a is not None and b:
            delta["latency"] = f"{round(100 * (a - b) / b)}% vs current"
        a, b = s.get("quality_index"), current.get("quality_index")
        if a is not None and b is not None:
            delta["quality_index"] = round(a - b, 1)
        s["vs_current"] = delta or None
        s["is_current"] = current.get("gateway_name") in (s.get("gateway_names") or [])
