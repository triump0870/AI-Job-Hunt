"""Sync from OpenRouter: choose the top models by a ranking and must-haves, add only those to the catalog, and let the
direct OpenRouter route follow a worker's assignment to one of them."""
import sys
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import AIModel
from app.prompts.common import Prompt
from app.services import model_market as mm
from app.services import model_resolution
from app.services.llm import LLMCall, LLMClient, LLMError
from app.settings import get_settings

MILLION = 1_000_000


def _model(model_id, *, prompt=None, completion=None, context=None, created=None, params=()):
    item = {"id": model_id, "name": model_id.split("/")[-1].title(), "created": created, "context_length": context,
            "supported_parameters": list(params)}
    if prompt is not None:
        item["pricing"] = {"prompt": str(prompt / MILLION), "completion": str(completion / MILLION)}
    return item


LISTED = [
    _model("vendor/cheap:free", prompt=0, completion=0, context=32000, created=100, params=("response_format", "tools")),
    _model("vendor/mid", prompt=1, completion=2, context=128000, created=300, params=("structured_outputs", "reasoning")),
    _model("vendor/big", prompt=5, completion=15, context=1_000_000, created=200),
    _model("vendor/noprice", context=8000, created=400, params=("tools",)),
]


class OpenRouterList:
    def __init__(self):
        self.models = [dict(m) for m in LISTED]
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "openrouter.ai"
        self.calls += 1
        return httpx.Response(200, json={"data": self.models})


@pytest.fixture
def web(monkeypatch):
    mm.reset_cache()
    settings = get_settings()
    monkeypatch.setattr(settings, "model_market_enabled", True)
    monkeypatch.setattr(settings, "artificial_analysis_api_key", None)
    fake = OpenRouterList()
    monkeypatch.setattr(mm, "_transport", httpx.MockTransport(fake))
    yield fake
    mm.reset_cache()


def ids(result):
    return [m["model_id"] for m in result["models"]]


# ───────────── choosing the top models ─────────────

def test_each_ranking_orders_best_first_and_unknown_figures_last(account, web):
    assert ids(account.get("/openrouter/models?rank=cheapest")) == [
        "vendor/cheap:free", "vendor/mid", "vendor/big", "vendor/noprice"]
    assert ids(account.get("/openrouter/models?rank=context")) == [
        "vendor/big", "vendor/mid", "vendor/cheap:free", "vendor/noprice"]
    assert ids(account.get("/openrouter/models?rank=newest")) == [
        "vendor/noprice", "vendor/mid", "vendor/big", "vendor/cheap:free"]
    top = account.get("/openrouter/models?rank=cheapest&limit=2")
    assert ids(top) == ["vendor/cheap:free", "vendor/mid"] and top["matching"] == 4 and top["listed"] == 4
    mid = top["models"][1]
    assert (mid["input_per_million"], mid["output_per_million"], mid["average_per_million"]) == (1.0, 2.0, 1.5)


def test_must_haves_count_only_what_openrouter_states(account, web):
    def pick(query):
        return ids(account.get(f"/openrouter/models?rank=cheapest&{query}"))

    assert pick("free=true") == ["vendor/cheap:free"]
    assert pick("structured=true") == ["vendor/cheap:free", "vendor/mid"]
    assert pick("tools=true") == ["vendor/cheap:free", "vendor/noprice"]
    assert pick("reasoning=true") == ["vendor/mid"]
    assert pick("vision=true") == []  # nobody states vision: unknown never passes
    assert pick("min_context=100000") == ["vendor/mid", "vendor/big"]
    assert pick("max_price=2") == ["vendor/cheap:free", "vendor/mid"]  # no stated price: left out


def test_quality_ranking_needs_benchmarks_and_is_never_guessed(account, web):
    assert "quality" not in account.get("/openrouter/models")["ranks"]
    refused = account.call("GET", "/openrouter/models?rank=quality", expect=422)
    assert "ARTIFICIAL_ANALYSIS_API_KEY" in refused["detail"]["message"]
    account.call("GET", "/openrouter/models?rank=popular", expect=422)


def test_the_list_is_cached_until_refreshed_and_switched_off_says_so(account, web, monkeypatch):
    account.get("/openrouter/models")
    account.get("/openrouter/models?rank=context")
    assert web.calls == 1
    account.get("/openrouter/models?refresh=true")
    assert web.calls == 2
    monkeypatch.setattr(get_settings(), "model_market_enabled", False)
    refused = account.call("GET", "/openrouter/models", expect=422)
    assert "MODEL_MARKET_ENABLED" in refused["detail"]["message"]


# ───────────── adding the chosen ones ─────────────

def catalog(user_id):
    with SessionLocal() as db:
        return {m.litellm_model_name: m for m in db.scalars(select(AIModel).where(
            AIModel.user_id == user_id, AIModel.source == "OPENROUTER")).all()}


def test_only_the_chosen_models_are_added_with_what_openrouter_states(account, web):
    before = account.revision()
    out = account.call("POST", "/models/sync-openrouter", json={"ids": ["vendor/mid", "vendor/cheap:free"]})
    assert sorted(out["added"]) == ["openrouter/vendor/cheap:free", "openrouter/vendor/mid"]
    assert out["draft_revision"] == before + 1
    rows = catalog(account.user_id)
    assert set(rows) == {"openrouter/vendor/mid", "openrouter/vendor/cheap:free"}
    mid = rows["openrouter/vendor/mid"]
    assert (mid.provider, mid.underlying_model, mid.context_window) == ("openrouter", "vendor/mid", 128000)
    assert (mid.supports_structured_output, mid.supports_reasoning, mid.supports_tools) == (True, True, False)
    assert mid.supports_vision is None and mid.present_in_litellm is False
    listed = account.get("/openrouter/models")["models"]
    assert {m["model_id"]: m["in_catalog"] for m in listed}["vendor/mid"] is True
    assert "vendor/mid" not in ids(account.get("/openrouter/models?hide_in_catalog=true"))
    refused = account.call("POST", "/models/sync-openrouter", expect=422, json={"ids": ["vendor/made-up"]})
    assert "does not list vendor/made-up" in refused["detail"]["message"]


def test_a_resync_refreshes_facts_keeps_removals_and_never_deletes_a_delisted_model(account, web):
    account.call("POST", "/models/sync-openrouter", json={"ids": ["vendor/mid", "vendor/big"]})
    web.models = [m for m in web.models if m["id"] != "vendor/big"]
    web.models[1]["context_length"] = 200000  # vendor/mid grew
    account.call("POST", "/models/openrouter/vendor/mid/remove", json={"revision": account.revision()})
    out = account.call("POST", "/models/sync-openrouter", json={"ids": []})
    assert out["missing"] == ["openrouter/vendor/big"] and out["added"] == []
    rows = catalog(account.user_id)
    assert rows["openrouter/vendor/big"].removed_at is None, "a delisted model may be in use: it stays"
    assert rows["openrouter/vendor/mid"].removed_at is not None, "a person's removal survives a refresh"
    assert rows["openrouter/vendor/mid"].context_window == 128000
    account.call("POST", "/models/sync-openrouter", json={"ids": ["vendor/mid"]})
    mid = catalog(account.user_id)["openrouter/vendor/mid"]
    assert mid.removed_at is None and mid.context_window == 200000, "choosing it again brings it back, refreshed"
    assert mid.enabled is True, "and usable: removing it had disabled it"


def test_an_accounts_openrouter_models_are_its_own(account, web):
    account.call("POST", "/models/sync-openrouter", json={"ids": ["vendor/mid"]})
    with SessionLocal() as db:
        others = db.scalars(select(AIModel.user_id).where(
            AIModel.litellm_model_name == "openrouter/vendor/mid", AIModel.user_id != account.user_id)).all()
    assert account.user_id not in others
    assert catalog(account.user_id)["openrouter/vendor/mid"].user_id == account.user_id


# ───────────── the direct OpenRouter route follows the assignment ─────────────

class FakeOpenRouter:
    def __init__(self):
        self.requests = []
        outer = self

        class Completions:
            def create(self, **kwargs):
                outer.requests.append(kwargs)
                return SimpleNamespace(model=kwargs["model"], usage=None,
                                       choices=[SimpleNamespace(message=SimpleNamespace(content='{"score": 80}'))])

        class OpenAI:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=Completions())

        self.module = SimpleNamespace(OpenAI=OpenAI)


@pytest.fixture
def direct(account, web, monkeypatch):
    """An account on the direct OpenRouter route, with its own key, and two OpenRouter models in its catalog."""
    fake = FakeOpenRouter()
    monkeypatch.setitem(sys.modules, "openai", fake.module)
    monkeypatch.setattr(get_settings(), "openrouter_require_free", False)
    monkeypatch.setattr(get_settings(), "openrouter_fallback_models", "")
    account.gateway(False)
    monkeypatch.setattr(get_settings(), "llm_provider", "openrouter")  # the direct route's provider
    account.call("POST", "/models/sync-openrouter", json={"ids": ["vendor/cheap:free", "vendor/mid"]})
    from app.services import user_settings
    real_keys = user_settings.user_keys
    monkeypatch.setattr(user_settings, "user_keys", lambda db, user_id=None: {**real_keys(db, user_id),
                                                                             "openrouter": "sk-or-test"})
    account.fake = fake
    return account


def ranking(job_id=1):
    return Prompt("ranking", "v4", "FIT_ANALYSIS", "Score the job.", f"FIT_ANALYSIS\nJOB ID: {job_id}")


def client_for(account):
    with SessionLocal() as db:
        db.info["user_id"] = account.user_id
        client = LLMClient.for_user(db)
    client.keys = {**client.keys, "openrouter": "sk-or-test"}
    return client


def test_a_worker_assigned_an_openrouter_model_uses_it_with_its_backups(direct):
    direct.worker("RANKER", model_mode="FIXED", primary_model="openrouter/vendor/mid",
                  fallback_model="openrouter/vendor/cheap:free")
    direct.apply()  # LiteLLM does not serve these, and on the direct route it does not need to
    model_resolution.invalidate()
    client = client_for(direct)
    route = client.route_for("RANKER")
    assert (route["route"], route["model"], route["backups"]) == ("OPENROUTER", "vendor/mid", ["vendor/cheap:free"])
    assert client.generate_prompt(ranking(), LLMCall()).data["score"] == 80
    sent = direct.fake.requests[-1]
    assert sent["model"] == "vendor/mid" and sent["extra_body"]["models"] == ["vendor/cheap:free"]


def test_an_unassigned_worker_keeps_the_settings_model_exactly_as_before(direct):
    model_resolution.invalidate()
    client = client_for(direct)
    settings_model = client._config()["stage_models"].get("rank") or client._config()["model"]
    assert client.route_for("RANKER")["model"] == settings_model and client.route_for("RANKER")["backups"] == []
    client.generate_prompt(ranking(), LLMCall())
    assert direct.fake.requests[-1]["model"] == settings_model
    assert "models" not in direct.fake.requests[-1]["extra_body"]


def test_the_free_only_guard_covers_a_paid_backup(direct, monkeypatch):
    direct.worker("RANKER", model_mode="FIXED", primary_model="openrouter/vendor/cheap:free",
                  fallback_model="openrouter/vendor/mid")
    direct.apply()
    model_resolution.invalidate()
    monkeypatch.setattr(get_settings(), "openrouter_require_free", True)
    with pytest.raises(LLMError, match="vendor/mid"):
        client_for(direct).generate_prompt(ranking(), LLMCall())
    assert direct.fake.requests == []


def test_on_the_gateway_an_openrouter_model_must_still_be_served_by_litellm(direct):
    direct.gateway(True)
    direct.worker("RANKER", model_mode="FIXED", primary_model="openrouter/vendor/mid")
    refused = direct.apply(expect=422)
    assert "not served by LiteLLM" in str(refused)


# ───────────── testing a model or a worker on the direct route ─────────────

def test_a_synced_model_is_tested_through_openrouter_on_the_direct_route(direct):
    out = direct.call("POST", "/models/openrouter/vendor/mid/test")
    assert out["success"] is True and out["route"] == "OPENROUTER" and out["error"] is None
    assert direct.fake.requests[-1]["model"] == "vendor/mid", "the OpenRouter id, never the catalog name"
    assert catalog(direct.user_id)["openrouter/vendor/mid"].health_status == "HEALTHY"


def test_a_configuration_problem_is_reported_without_calling_the_model_unhealthy(direct, monkeypatch):
    monkeypatch.setattr(get_settings(), "openrouter_require_free", True)
    out = direct.call("POST", "/models/openrouter/vendor/mid/test")
    assert out["success"] is False and "OPENROUTER_REQUIRE_FREE" in out["error"]
    assert direct.fake.requests == []
    assert catalog(direct.user_id)["openrouter/vendor/mid"].health_status != "UNHEALTHY"


def test_a_worker_on_a_synced_model_is_tested_through_openrouter(direct):
    direct.worker("RANKER", model_mode="FIXED", primary_model="openrouter/vendor/mid",
                  fallback_model="openrouter/vendor/cheap:free")
    direct.call("POST", "/workers/RANKER/test", json={})
    sent = direct.fake.requests[-1]
    assert sent["model"] == "vendor/mid" and sent["extra_body"]["models"] == ["vendor/cheap:free"]
