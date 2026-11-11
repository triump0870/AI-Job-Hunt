"""The model market: catalog parsers, exact joins, caching, your own telemetry, and deterministic ranking."""
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.db import SessionLocal
from app.models import AIModel, LLMRequestLog
from app.services import model_market as mm
from app.services import model_resolution
from app.services.model_market import Constraints, MarketModel, Requirements
from app.settings import get_settings
from tests.conftest import make_user

OPENROUTER = {"data": [
    {"id": "openai/gpt-oss-120b", "name": "gpt-oss-120b", "context_length": 131072,
     "pricing": {"prompt": "0.0000001", "completion": "0.0000005", "input_cache_read": "0.00000005"},
     "top_provider": {"max_completion_tokens": 32768}, "supported_parameters": ["tools", "structured_outputs"],
     "architecture": {"input_modalities": ["text"]}, "description": "x" * 1000},
    {"id": "meta/tiny:free", "context_length": 8000, "pricing": {"prompt": "0", "completion": "0"},
     "supported_parameters": ["tools"]},
    {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}},
    {"name": "no id"}, "junk",
]}
LITELLM = {
    "sample_spec": {"input_cost_per_token": 1},
    "groq/openai/gpt-oss-120b": {"litellm_provider": "groq", "mode": "chat", "input_cost_per_token": 1.5e-7,
                                 "output_cost_per_token": 6e-7, "max_input_tokens": 131072,
                                 "supports_response_schema": True},
    "groq/llama-x": {"litellm_provider": "groq", "mode": "chat", "input_cost_per_token": 5e-8,
                     "output_cost_per_token": 8e-8, "max_input_tokens": 8192},
    "text-embedding-3-small": {"litellm_provider": "openai", "mode": "embedding", "input_cost_per_token": 2e-8},
    "openrouter/openai/gpt-oss-120b": {"litellm_provider": "openrouter", "mode": "chat",
                                       "input_cost_per_token": 9e-7, "output_cost_per_token": 9e-7},
}
GROQ = {"data": [{"id": "openai/gpt-oss-120b", "context_window": 131072, "max_completion_tokens": 65536,
                  "active": True}, {"id": "brand-new", "active": True}, {"object": "model"}]}
OLLAMA_CLOUD = {"models": [{"name": "gpt-oss:120b", "details": {"parameter_size": "120B"},
                            "capabilities": ["tools", "thinking"]}, {"model": "beam:cloud"}, {}]}
ENDPOINTS = {"data": {"id": "openai/gpt-oss-120b", "endpoints": [
    {"provider_name": "A", "pricing": {"prompt": "0.0000001", "completion": "0.0000005"}, "context_length": 131072,
     "uptime_last_30m": 90.0, "uptime_last_1d": 97.5, "status": 0},
    {"provider_name": "B", "uptime_last_30m": 100, "latency_last_30m": {"p50": 420}},
]}}
AA = {"data": [
    {"slug": "gpt-oss-120b", "name": "gpt-oss-120B", "model_creator": {"name": "OpenAI"},
     "evaluations": {"artificial_analysis_intelligence_index": 61.2}, "median_output_tokens_per_second": 250,
     "median_time_to_first_token_seconds": 0.5},
    {"slug": "dup", "evaluations": {"artificial_analysis_intelligence_index": 1}},
    {"slug": "DUP", "evaluations": {"artificial_analysis_intelligence_index": 2}},
]}


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    mm.reset_cache()
    settings = get_settings()
    for name, value in (("model_market_enabled", True), ("model_market_cache_hours", 6),
                        ("artificial_analysis_api_key", None), ("groq_api_key", None), ("ollama_api_key", None),
                        ("ollama_base_url", "")):
        monkeypatch.setattr(settings, name, value)
    yield
    mm.reset_cache()
    monkeypatch.setattr(mm, "_transport", None)


class Web:
    """The catalogs as a MockTransport: counts calls, and can refuse or fail a host."""

    def __init__(self):
        self.calls: list[str] = []
        self.status: dict[str, int] = {}
        self.headers: dict[str, dict[str, str]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        self.headers[request.url.host] = dict(request.headers)
        for host, code in self.status.items():
            if host in url:
                return httpx.Response(code, json={"error": "no"})
        if url.endswith("/endpoints"):
            return httpx.Response(200, json=ENDPOINTS)
        if "openrouter.ai" in url:
            return httpx.Response(200, json=OPENROUTER)
        if "raw.githubusercontent.com" in url:
            return httpx.Response(200, json=LITELLM)
        if "api.groq.com" in url:
            return httpx.Response(200, json=GROQ)
        if "ollama.com" in url:
            return httpx.Response(200, json=OLLAMA_CLOUD)
        if "artificialanalysis.ai" in url:
            return httpx.Response(200, json=AA)
        return httpx.Response(404)


@pytest.fixture
def web(monkeypatch):
    fake = Web()
    monkeypatch.setattr(mm, "_transport", httpx.MockTransport(fake))
    return fake


def row(model_id="m", platform="openrouter", **fields):
    return MarketModel(platform=platform, model_id=model_id, **fields)


# ───────────── parsers ─────────────

def test_openrouter_prices_capabilities_and_unknowns():
    rows = {r.model_id: r for r in mm.parse_openrouter(OPENROUTER)}
    assert set(rows) == {"openai/gpt-oss-120b", "meta/tiny:free", "openrouter/auto"}
    big = rows["openai/gpt-oss-120b"]
    assert (big.input_per_million, big.output_per_million, big.cached_input_per_million) == (0.1, 0.5, 0.05)
    assert big.structured_output is True and big.tools is True and big.reasoning is False and big.free is False
    assert big.max_output_tokens == 32768 and len(big.description) <= mm.DESCRIPTION_LIMIT
    assert rows["meta/tiny:free"].free is True and rows["meta/tiny:free"].structured_output is False
    auto = rows["openrouter/auto"]  # a router's -1 price is unknown, not negative, and its capabilities unknown
    assert auto.input_per_million is None and auto.free is None and auto.structured_output is None
    assert mm.parse_openrouter(None) == [] and mm.parse_openrouter({"data": "x"}) == []


def test_litellm_price_list_rows_are_on_their_platform_without_the_prefix():
    rows = {(r.platform, r.model_id): r for r in mm.parse_litellm_prices(LITELLM)}
    assert ("groq", "openai/gpt-oss-120b") in rows and ("openrouter", "openai/gpt-oss-120b") in rows
    assert not any(m == "text-embedding-3-small" for _, m in rows)  # embeddings are not chat models
    groq = rows[("groq", "openai/gpt-oss-120b")]
    assert (groq.input_per_million, groq.output_per_million, groq.structured_output) == (0.15, 0.6, True)


def test_groq_ollama_cloud_and_local_rows():
    groq = {r.model_id: r for r in mm.parse_groq(GROQ)}
    assert set(groq) == {"openai/gpt-oss-120b", "brand-new"}
    assert groq["openai/gpt-oss-120b"].input_per_million is None  # Groq's API has no prices
    cloud = {r.model_id: r for r in mm.parse_ollama_cloud(OLLAMA_CLOUD)}
    assert set(cloud) == {"gpt-oss:120b", "beam"}
    assert cloud["gpt-oss:120b"].input_per_million is None and "subscription" in cloud["gpt-oss:120b"].pricing_note
    assert cloud["gpt-oss:120b"].tools is True and cloud["gpt-oss:120b"].reasoning is True
    local = {(r.platform, r.model_id): r for r in mm.parse_ollama_local(
        {"qwen2.5:7b": {"context_length": 32768, "capabilities": ["completion"]}, "gpt-oss:20b-cloud": {}})}
    assert local[("ollama_local", "qwen2.5:7b")].input_per_million == 0.0
    assert local[("ollama_local", "qwen2.5:7b")].context_window == 32768
    assert ("ollama_cloud", "gpt-oss:20b") in local  # a cloud model your Ollama lists is still a cloud model


def test_endpoints_prefer_the_day_uptime_and_keep_unreported_latency_unknown():
    a, b = mm.parse_openrouter_endpoints(ENDPOINTS)
    assert a["uptime_pct"] == 97.5 and a["uptime_last_30m_pct"] == 90.0 and a["latency_ms"] is None
    assert b["uptime_pct"] == 100 and b["latency_ms"] == 420
    assert mm.best_uptime([a, b]) == 100 and mm.best_uptime([]) is None


# ───────────── joins ─────────────

def test_merge_keeps_the_platform_api_figures_and_fills_only_unknowns_from_the_price_list():
    rows = mm.merge(mm.parse_groq(GROQ), mm.parse_litellm_prices(LITELLM))
    groq = rows[("groq", "openai/gpt-oss-120b")]
    assert groq.input_per_million == 0.15 and groq.max_output_tokens == 65536  # price list fills, Groq keeps its own
    assert groq.sources == ["groq", "litellm_prices"]
    assert rows[("groq", "brand-new")].input_per_million is None  # no exact price row: unknown, not borrowed
    assert rows[("groq", "llama-x")].sources == ["litellm_prices"]


def test_benchmarks_join_by_exact_base_name_and_drop_ambiguous_slugs():
    benchmarks = mm.parse_artificial_analysis(AA)
    assert set(benchmarks) == {"gpt-oss-120b"}  # "dup" and "DUP" collide: no join rather than a wrong one
    rows = [row("openai/gpt-oss-120b"), row("gpt-oss:120b", "ollama_cloud"), row("gpt-oss-12b")]
    mm.attach_benchmarks(rows, benchmarks)
    assert [r.quality_index for r in rows] == [61.2, 61.2, None]
    assert "artificial_analysis:gpt-oss-120b" in rows[1].sources


# ───────────── fetching and the cache ─────────────

def test_sources_fetch_once_per_ttl_and_unconfigured_sources_are_said(web):
    states = mm.load(1)
    assert states["openrouter"].count == 3 and states["ollama_cloud"].count == 2
    assert states["groq"].configured is False and states["groq"].error == "not configured"
    assert states["artificial_analysis"].configured is False
    first = len(web.calls)
    mm.load(1)
    assert len(web.calls) == first  # cached
    mm.load(1, refresh=True)
    assert len(web.calls) == 2 * first


def test_a_failed_refresh_keeps_the_last_good_copy_marked_stale(web):
    mm.load(1, ["openrouter"])
    web.status["openrouter.ai"] = 503
    state = mm.load(1, ["openrouter"], refresh=True)["openrouter"]
    assert state.stale is True and state.count == 3 and "503" in state.error
    assert len(mm.parse_openrouter(state.data)) == 3


def test_keys_are_sent_only_to_their_own_host_and_a_refusal_names_the_key(web, monkeypatch):
    monkeypatch.setattr(get_settings(), "groq_api_key", "gsk-test")
    web.status["ollama.com"] = 401
    states = mm.load(1)
    assert states["groq"].count == 2
    assert web.headers["api.groq.com"]["authorization"] == "Bearer gsk-test"
    assert "authorization" not in web.headers["openrouter.ai"]
    assert states["ollama_cloud"].error == "refused (401): needs OLLAMA_API_KEY in .env"


def test_switching_the_market_off_makes_no_web_request(web, monkeypatch):
    monkeypatch.setattr(get_settings(), "model_market_enabled", False)
    states = mm.load(1)
    assert web.calls == [] and all(not s.enabled for n, s in states.items() if n != "ollama_local")


def test_endpoint_lookups_are_cached(web):
    mm.openrouter_endpoints("openai/gpt-oss-120b", 1)
    mm.openrouter_endpoints("openai/gpt-oss-120b", 1)
    assert sum(url.endswith("/endpoints") for url in web.calls) == 1
    assert mm.openrouter_endpoints("no-slash", 1)["error"]


# ───────────── filters and ranking ─────────────

def test_only_a_known_shortfall_excludes_and_unknown_is_said():
    need = Requirements(structured_output=True, min_context=10_000)
    assert mm.matches(row(context_window=8000), Constraints(), need)[0] is False
    assert mm.matches(row(structured_output=False, context_window=20_000), Constraints(), need)[0] is False
    ok, notes = mm.matches(row(), Constraints(), need)
    assert ok and notes == ["CONTEXT_UNKNOWN", "STRUCTURED_OUTPUT_UNKNOWN"]
    assert mm.matches(row(free=None), Constraints(free_only=True))[0] is False
    assert mm.matches(row(platform="ollama_cloud"), Constraints(providers=("ollama",)))[0] is True
    assert mm.matches(row(platform="groq"), Constraints(exclude_providers=("groq",)))[0] is False
    assert mm.matches(row(input_per_million=None), Constraints(max_input_cost_per_million=1))[0] is False


def test_cost_per_request_uses_the_workers_typical_tokens_and_your_own_measured_cost():
    need = Requirements(input_tokens=10_000, output_tokens=2_000)
    priced = row(input_per_million=1.0, output_per_million=2.0)
    assert mm.cost_per_request(priced, need) == pytest.approx(0.014)
    assert mm.cost_per_request(row(), need) is None
    assert mm.cost_per_request(row(free=True), need) == 0.0
    measured = row(input_per_million=1.0, output_per_million=2.0,
                   observed={"samples": mm.OWN_SAMPLES, "cost_per_request": 0.5})
    assert mm.cost_per_request(measured, need) == 0.5


def test_your_latency_wins_over_benchmarks_once_measured_enough():
    bench = {"ttft_seconds": 1.0, "tokens_per_second": 100.0}
    need = Requirements(output_tokens=500)
    assert mm.latency_seconds(row(**bench), need) == (6.0, "artificial_analysis")
    few = row(**bench, observed={"samples": mm.OWN_SAMPLES - 1, "p50_ms": 2000})
    assert mm.latency_seconds(few, need) == (6.0, "artificial_analysis")
    many = row(**bench, observed={"samples": mm.OWN_SAMPLES, "p50_ms": 2000})
    assert mm.latency_seconds(many, need) == (2.0, "yours")
    assert mm.latency_seconds(row(), need) == (None, None)


def market():
    return [
        row("cheap-slow", input_per_million=0.1, output_per_million=0.1, quality_index=40, uptime_pct=99,
            ttft_seconds=5, tokens_per_second=20),
        row("dear-fast", input_per_million=5, output_per_million=15, quality_index=70, uptime_pct=99.9,
            ttft_seconds=0.2, tokens_per_second=400),
        row("middle", input_per_million=1, output_per_million=2, quality_index=55, uptime_pct=98,
            ttft_seconds=1, tokens_per_second=150),
        row("price-only", input_per_million=0.01, output_per_million=0.01),
    ]


def test_objectives_change_the_order_deterministically():
    order = {o: [s["model_id"] for s in mm.rank(market(), o, Constraints(), limit=3)["suggestions"]]
             for o in mm.OBJECTIVES}
    assert order["cheapest"][0] == "cheap-slow"
    assert order["fastest"][0] == "dear-fast" and order["quality"][0] == "dear-fast"
    assert order == {o: [s["model_id"] for s in mm.rank(market(), o, Constraints(), limit=3)["suggestions"]]
                     for o in mm.OBJECTIVES}


def test_a_missing_metric_lowers_coverage_and_never_counts_as_zero():
    ranked = mm.rank(market(), "balanced", Constraints(), limit=5)
    assert "price-only" not in [s["model_id"] for s in ranked["suggestions"]]  # one metric: too little to rank
    assert ranked["insufficient_data"][0]["id"] == "openrouter/price-only"
    top = ranked["suggestions"][0]
    assert top["coverage"] == "4 of 5 metrics known" and "schema unknown" in top["reasons"]


def test_without_any_quality_score_the_ranking_says_so():
    rows = [row("a", input_per_million=1, output_per_million=1, uptime_pct=99),
            row("b", input_per_million=2, output_per_million=2, uptime_pct=98)]
    ranked = mm.rank(rows, "balanced", Constraints())
    assert ranked["notes"] and "ARTIFICIAL_ANALYSIS_API_KEY" in ranked["notes"][0]
    assert [s["quality_index"] for s in ranked["suggestions"]] == [None, None]
    with pytest.raises(ValueError):
        mm.rank(rows, "best", Constraints())


def test_free_openrouter_models_carry_the_shared_daily_quota():
    rows = [row("x:free", free=True, uptime_pct=99), row("y", input_per_million=1, output_per_million=1,
                                                          uptime_pct=99)]
    ranked = mm.rank(rows, "cheapest", Constraints(), free_quota_429s=12)
    free = next(s for s in ranked["suggestions"] if s["model_id"] == "x:free")
    assert "SHARES_OPENROUTER_FREE_DAILY_QUOTA" in free["flags"]
    assert any("12 times" in reason for reason in free["reasons"])


def test_the_uptime_shortlist_spans_the_price_range():
    rows = [row(f"m{i:02d}", input_per_million=float(i), output_per_million=float(i)) for i in range(30)]
    rows.append(row("unknown-price"))
    picked = [r.model_id for r in mm.shortlist(rows, Constraints(), None, n=4)]
    assert picked[0] == "m00" and picked[-1] == "m29" and len(picked) == 4 and "unknown-price" not in picked


def test_suggestions_are_compared_with_the_current_model():
    suggestions = [{"cost_per_request_usd": 0.5, "latency_seconds": 1.0, "quality_index": 60, "gateway_names": []},
                   {"cost_per_request_usd": 0.0, "latency_seconds": None, "quality_index": None,
                    "gateway_names": ["mine"]}]
    mm.compare(suggestions, {"known": True, "gateway_name": "mine", "cost_per_request_usd": 1.0,
                             "latency_seconds": 4.0, "quality_index": 50})
    assert suggestions[0]["vs_current"] == {"cost": "-50% vs current", "latency": "-75% vs current",
                                            "quality_index": 10}
    assert suggestions[1]["is_current"] is True and suggestions[0]["is_current"] is False


# ───────────── your account ─────────────

def log(user_id, model, **fields):
    values = {"request_id": f"market-{uuid.uuid4()}", "user_id": user_id, "stage": "RANKER", "provider": "litellm",
                  "requested_model": model, "model": model, "worker_type": "RANKER", "input_tokens": 1000, "output_tokens": 200,
                  "latency_ms": 2000, "estimated_cost": Decimal("0.002"), "cost_source": "pricing_table", "status": "SUCCEEDED",
                  "structured_output_valid": True, "created_at": utcnow() - timedelta(hours=1)}
    values.update(fields)
    return LLMRequestLog(**values)


def test_telemetry_is_this_accounts_own_and_skips_requests_never_sent():
    mine, other = make_user(SessionLocal), make_user(SessionLocal)
    with SessionLocal() as db:
        db.add_all([log(mine, "beam:cloud") for _ in range(3)]
                   + [log(mine, "beam:cloud", status="FAILED", error_type="TIMEOUT", latency_ms=None),
                      log(mine, "beam:cloud", status="FAILED", error_type="RATE_LIMIT", cost_source="not_sent"),
                      log(mine, "beam:cloud", status="FAILED", error_type="CONFIGURATION"),
                      log(mine, "or", status="FAILED", error_type="RATE_LIMIT",
                          error_message="Rate limit exceeded: free-models-per-day"),
                      log(mine, "beam:cloud", created_at=utcnow() - timedelta(days=30)),
                      log(other, "beam:cloud", latency_ms=99_999)])
        db.commit()
        measured = mm.telemetry(db, mine)
    beam = measured["models"]["beam:cloud"]
    assert beam["samples"] == 4 and beam["failure_pct"] == 25.0 and beam["p95_ms"] == 2000
    assert beam["cost_per_request"] == 0.002 and beam["schema_valid_pct"] == 100.0
    assert measured["workers"]["RANKER"]["p90_input_tokens"] == 1000
    assert measured["openrouter_free_quota_429s"] == 1


def test_gateway_models_land_on_their_platform_with_your_measurements(web, monkeypatch):
    user_id = make_user(SessionLocal)
    snapshot = {"workers": {"RANKER": {"mode": "FIXED", "primary_model": "groq/openai/gpt-oss-120b",
                                       "fallback_model": "beam:cloud"}},
                "models": {"groq/openai/gpt-oss-120b": {}, "beam:cloud": {}}}
    monkeypatch.setattr(model_resolution, "active_snapshot", lambda uid: (1, snapshot))
    with SessionLocal() as db:
        db.add_all([AIModel(user_id=user_id, litellm_model_name="groq/openai/gpt-oss-120b", provider="groq",
                            underlying_model="openai/gpt-oss-120b", display_name="Groq 120b"),
                    AIModel(user_id=user_id, litellm_model_name="beam:cloud", provider="ollama",
                            underlying_model="beam:cloud", display_name="beam"),
                    AIModel(user_id=user_id, litellm_model_name="Router", provider="litellm",
                            underlying_model="complexity_router", model_type="AUTO_ROUTER")])
        db.add_all([log(user_id, "groq/openai/gpt-oss-120b") for _ in range(mm.OWN_SAMPLES)])
        db.commit()
        found = mm.catalog(db, user_id)
    rows = {(r.platform, r.model_id): r for r in found.rows}
    groq = rows[("groq", "openai/gpt-oss-120b")]
    assert groq.in_gateway and groq.gateway_names == ["groq/openai/gpt-oss-120b"] and groq.used_by == ["RANKER"]
    assert groq.input_per_million == 0.15 and groq.observed["samples"] == mm.OWN_SAMPLES
    assert "gateway" in groq.sources and "litellm_prices" in groq.sources
    beam = rows[("ollama_cloud", "beam")]
    assert beam.in_gateway and "subscription" in beam.pricing_note and beam.used_by == ["RANKER"]
    assert rows[("gateway", "Router")].pricing_note.startswith("an auto router")
    need = Requirements(input_tokens=1000, output_tokens=200)
    assert mm.current_model(found.rows, user_id, "RANKER", need)["id"] == "groq/openai/gpt-oss-120b"
