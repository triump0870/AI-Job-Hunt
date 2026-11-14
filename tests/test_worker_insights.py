import uuid

import pytest
from sqlalchemy import delete

from app.db import SessionLocal
from app.models import AIModel, LLMRequestLog, User
from app.services import model_health, model_market, worker_insights
from app.settings import get_settings
from tests.litellm_fake import deployment

PREFIX = "insights-test-"


def snapshot(**ranker):
    chat = {"enabled": True, "model_type": "CUSTOM", "context_window": 131072, "supports_structured_output": True}
    return {"models": {"Groq": dict(chat), "roomy": dict(chat, context_window=262144), "steady": dict(chat),
                       "spare": dict(chat), "extra": dict(chat),
                       "tiny": dict(chat, context_window=2048),
                       "prose": dict(chat, supports_structured_output=False),
                       "router": dict(chat, context_window=None), "maybe": dict(chat, supports_structured_output=None),
                       "vectors": dict(chat, model_type="EMBEDDING"), "off": dict(chat, enabled=False)},
            "tiers": {}, "routing": {},
            "workers": {"RANKER": {"mode": "FIXED", "primary_model": "Groq", "enabled": True, **ranker},
                        "REVIEWER": {"mode": "FIXED", "primary_model": "roomy", "enabled": True},
                        "EMBEDDER": {"mode": "FIXED", "primary_model": "vectors", "enabled": True}}}


@pytest.fixture
def owner():
    """An account of its own: nothing another test logged for the shared test user can change a figure here."""
    with SessionLocal() as db:
        user = User(email=f"{PREFIX}{uuid.uuid4().hex}@example.com", password_hash="!")
        db.add(user)
        db.commit()
        user_id = user.id
    yield user_id
    with SessionLocal() as db:
        db.execute(delete(LLMRequestLog).where(LLMRequestLog.user_id == user_id))
        db.execute(delete(User).where(User.id == user_id))
        db.commit()


def log(user, model, n=1, *, worker="RANKER", status="SUCCEEDED", error=None, tokens_in=1000, latency=100,
        fallback_used=None, valid=None, estimate=None, tokens_out=None, message=None):
    with SessionLocal() as db:
        for _ in range(n):
            db.add(LLMRequestLog(request_id=f"{PREFIX}{uuid.uuid4()}", user_id=user, provider="litellm",
                                 stage="OTHER", requested_model=model, model=model, worker_type=worker,
                                 status=status, error_type=error, fallback_used=fallback_used,
                                 structured_output_valid=valid, input_tokens=tokens_in, output_tokens=tokens_out,
                                 error_message=message,
                                 estimated_context_tokens=estimate, latency_ms=latency,
                                 created_at=model_health.utcnow()))
        db.commit()


def report(owner, **ranker):
    with SessionLocal() as db:
        return worker_insights.report(db, owner, snapshot(**ranker))


def test_context_is_the_average_the_workers_answered_requests_sent(owner):
    log(owner, "Groq", 3, tokens_in=10000)
    log(owner, "Groq", 1, tokens_in=30000)
    log(owner, "Groq", 1, tokens_in=None, estimate=5000)  # no provider count: our estimate stands in
    log(owner, "Groq", 1, status="FAILED", error="TIMEOUT", tokens_in=999999)  # never answered: not counted
    context = report(owner)["workers"]["RANKER"]["context"]
    assert context["avg_input_tokens"] == 13000 and context["samples"] == 5
    assert context["window"] == 131072 and context["utilization_pct"] == 9.9 and context["model"] == "Groq"
    assert context["near_limit"] is False
    assert report(owner, context_window=32000)["workers"]["RANKER"]["context"]["near_limit"] is True


def test_context_stays_unknown_without_requests_or_a_window(owner):
    context = report(owner)["workers"]["REVIEWER"]["context"]
    assert context["avg_input_tokens"] is None and context["utilization_pct"] is None


def test_best_ranks_the_workers_own_models_and_needs_enough_requests(owner):
    log(owner, "steady", 30)
    log(owner, "Groq", 20)
    log(owner, "Groq", 10, status="FAILED", error="TIMEOUT")
    log(owner, "spare", 25)
    log(owner, "spare", 5, fallback_used=True)  # a fallback's answer counts against the requested model
    log(owner, "roomy", 5)  # too few to rank
    log(owner, "extra", 30, status="FAILED", error="CONFIGURATION")  # a setup error says nothing about a model
    worker = report(owner)["workers"]["RANKER"]
    assert [b["model"] for b in worker["best"]] == ["steady", "spare", "Groq"]
    steady, spare, groq = worker["best"]
    assert steady["success_pct"] == 100 and steady["samples"] == 30 and steady["score"] > spare["score"] > groq["score"]
    assert groq["is_current"] and groq["apply"] is None and worker["current_stats"]["success_pct"] == 66.7
    # spare answered only 83% of its requests: it is ranked, but never added as a backup or offered as a switch.
    assert steady["apply"] == {"model_mode": "FIXED", "primary_model": "steady", "fallback_model": "Groq",
                               "secondary_models": []}
    assert spare["apply"] is None


def test_a_slower_model_ranks_below_an_equally_reliable_fast_one(owner):
    log(owner, "steady", 30, latency=4000)
    log(owner, "spare", 30, latency=200)
    assert [b["model"] for b in report(owner)["workers"]["RANKER"]["best"]] == ["spare", "steady"]


def test_another_accounts_requests_never_appear(owner):
    with SessionLocal() as db:
        other = User(email=f"{PREFIX}{uuid.uuid4().hex}@example.com", password_hash="!")
        db.add(other)
        db.commit()
        other_id = other.id
    try:
        log(other_id, "steady", 40)
        log(other_id, "roomy", 40, worker="REVIEWER")
        worker = report(owner)["workers"]["RANKER"]
        assert worker["requests"] == 0 and worker["best"] == [] and worker["suggestions"] == []
    finally:
        with SessionLocal() as db:
            db.execute(delete(LLMRequestLog).where(LLMRequestLog.user_id == other_id))
            db.execute(delete(User).where(User.id == other_id))
            db.commit()


def test_a_model_that_did_well_in_another_worker_is_suggested_with_the_switch(owner):
    log(owner, "Groq", 20)
    log(owner, "Groq", 10, status="FAILED", error="TIMEOUT")
    log(owner, "roomy", 40, worker="REVIEWER", valid=True)
    log(owner, "steady", 10, worker="REVIEWER")  # too little evidence anywhere
    worker = report(owner, fallback_model="spare", secondary_models=["roomy", "extra"])["workers"]["RANKER"]
    assert [(s["model"], s["basis"]) for s in worker["suggestions"]] == [("roomy", "OTHER_WORKERS")]
    suggestion = worker["suggestions"][0]
    assert suggestion["workers"] == ["Reviewer"] and suggestion["stats"]["samples"] == 40
    # The model it used becomes the first backup; the rest keep their order; the new model is not its own backup.
    assert suggestion["apply"] == {"model_mode": "FIXED", "primary_model": "roomy", "fallback_model": "Groq",
                                   "secondary_models": ["spare", "extra"]}


def test_nothing_is_suggested_over_a_model_that_is_already_doing_as_well(owner):
    log(owner, "Groq", 30)
    log(owner, "roomy", 40, worker="REVIEWER")
    assert report(owner)["workers"]["RANKER"]["suggestions"] == []


def test_a_model_in_use_with_too_few_requests_here_is_judged_on_all_its_requests(owner):
    log(owner, "Groq", 5)  # too few for this worker to rank it
    log(owner, "Groq", 30, worker="REVIEWER")  # but it does fine elsewhere
    log(owner, "roomy", 40, worker="REVIEWER")
    assert report(owner)["workers"]["RANKER"]["suggestions"] == []  # roomy is no better than what it uses
    log(owner, "Groq", 30, worker="REVIEWER", status="FAILED", error="TIMEOUT")
    assert [s["model"] for s in report(owner)["workers"]["RANKER"]["suggestions"]] == ["roomy"]


TOO_LARGE = ("Request too large for model `x` in organization `org_x` service tier `on_demand` on tokens per minute "
             "(TPM): Limit 8000, Requested {n}, please reduce your message size and try again.")


def suggestions(owner, **ranker):
    worker = report(owner, **ranker)["workers"]["RANKER"]
    return [s["model"] for s in worker["suggestions"]], worker["screened"]["rejected"]


def test_an_unknown_context_window_is_offered_only_once_the_model_answered_a_request_that_large(owner):
    log(owner, "Groq", 20, tokens_in=25000, tokens_out=1000)
    log(owner, "Groq", 10, status="FAILED", error="TIMEOUT", estimate=25000)
    log(owner, "router", 40, worker="REVIEWER", tokens_in=20000, tokens_out=500)  # big, but smaller than a typical one
    names, rejected = suggestions(owner)
    assert names == [] and rejected["CONTEXT_UNKNOWN"] == 1
    log(owner, "router", 1, worker="REVIEWER", tokens_in=26000, tokens_out=1000, fallback_used=True)
    assert suggestions(owner)[0] == []  # a fallback answered that one: it proves nothing about the router
    log(owner, "router", 1, worker="REVIEWER", tokens_in=26000, tokens_out=1000)
    worker = report(owner)["workers"]["RANKER"]
    assert [(s["model"], s["flags"]) for s in worker["suggestions"]] == [("router", ["CAPACITY_PROVEN"])]
    assert worker["needs"] == {"context_tokens": 26000, "typical_request_tokens": 26000, "structured_output": True}


def test_a_model_that_refused_a_request_this_size_is_never_offered(owner):
    log(owner, "Groq", 20, tokens_in=9000, tokens_out=500)
    log(owner, "Groq", 10, status="FAILED", error="TIMEOUT", estimate=9000)
    log(owner, "roomy", 40, worker="REVIEWER", tokens_in=9000)
    assert suggestions(owner)[0] == ["roomy"]
    log(owner, "roomy", 1, worker="REVIEWER", status="FAILED", error="RATE_LIMIT", message=TOO_LARGE.format(n=9200))
    names, rejected = suggestions(owner)
    assert names == [] and rejected["REFUSED_AS_TOO_LARGE"] == 1


def test_unknown_json_support_is_offered_only_once_the_models_own_answers_proved_it(owner):
    log(owner, "Groq", 20)
    log(owner, "Groq", 10, status="FAILED", error="TIMEOUT")
    log(owner, "maybe", 40, worker="REVIEWER")  # answered, but nobody judged its JSON
    names, rejected = suggestions(owner)
    assert names == [] and rejected["STRUCTURED_OUTPUT_UNKNOWN"] == 1
    log(owner, "maybe", 19, worker="REVIEWER", valid=True)
    log(owner, "maybe", 3, worker="REVIEWER", valid=False)  # 86% valid: not proof
    assert suggestions(owner)[0] == []
    log(owner, "maybe", 20, worker="REVIEWER", valid=True)
    worker = report(owner)["workers"]["RANKER"]
    assert [(s["model"], s["flags"]) for s in worker["suggestions"]] == [("maybe", ["JSON_PROVEN"])]


def test_a_model_that_fails_often_is_never_suggested_even_with_nothing_to_compare_it_to(owner):
    # The model in use has no requests anywhere: "better than nothing measured" is not better.
    log(owner, "roomy", 21, worker="REVIEWER")
    log(owner, "roomy", 19, worker="REVIEWER", status="FAILED", error="TIMEOUT")  # 52.5% ok
    log(owner, "steady", 36, worker="REVIEWER")
    log(owner, "steady", 4, worker="REVIEWER", status="FAILED", error="TIMEOUT")  # 90% ok: the floor
    names, rejected = suggestions(owner)
    assert names == ["steady"] and rejected["LOW_SUCCESS"] == 1


def test_another_workers_small_requests_say_nothing_about_a_worker_with_large_ones(owner):
    log(owner, "Groq", 20, tokens_in=25000)
    log(owner, "Groq", 10, status="FAILED", error="TIMEOUT", estimate=25000)
    log(owner, "roomy", 200, worker="REVIEWER", tokens_in=1300)  # a classifier's prompts
    names, rejected = suggestions(owner)
    assert names == [] and "LOW_SUCCESS" not in rejected and "NOT_BETTER" not in rejected  # simply not evidence
    log(owner, "roomy", 19, worker="REVIEWER", tokens_in=14000)
    assert suggestions(owner)[0] == []  # 19 comparable requests: one short of enough
    log(owner, "roomy", 1, worker="REVIEWER", status="FAILED", error="TIMEOUT", tokens_in=None, estimate=14000)
    worker = report(owner)["workers"]["RANKER"]
    assert [s["model"] for s in worker["suggestions"]] == ["roomy"]
    assert worker["suggestions"][0]["stats"]["samples"] == 20  # judged on the comparable requests only
    assert worker["suggestions"][0]["stats"]["success_pct"] == 95.0


def test_going_back_to_a_model_is_offered_only_when_it_did_better_than_the_one_in_use(owner):
    log(owner, "Groq", 30)
    log(owner, "steady", 28)
    log(owner, "steady", 2, status="FAILED", error="TIMEOUT")
    best = {b["model"]: b["apply"] for b in report(owner)["workers"]["RANKER"]["best"]}
    assert best == {"Groq": None, "steady": None}


def test_the_catalog_never_speaks_for_a_model_whose_fit_rests_on_a_stand_in(owner, monkeypatch):
    log(owner, "Groq", 30, tokens_in=1000, tokens_out=100)
    log(owner, "router", 5, worker="REVIEWER", tokens_in=2000, tokens_out=100)  # proves capacity, not quality
    monkeypatch.setattr(worker_insights, "_catalog_scores", lambda *a: {"Groq": 40.0, "router": 90.0, "roomy": 41.0})
    assert suggestions(owner)[0] == []  # router's window is not a known fact; roomy is not clearly better
    monkeypatch.setattr(worker_insights, "_catalog_scores", lambda *a: {"router": 90.0, "roomy": 90.0})
    assert suggestions(owner)[0] == []  # the model in use is not on the catalog's scale: nothing to beat


def test_models_that_cannot_do_the_workers_job_are_never_suggested(owner):
    log(owner, "Groq", 10, status="FAILED", error="TIMEOUT")
    for name in ("tiny", "prose", "vectors", "off"):  # too small, no JSON, an embedding model, disabled
        log(owner, name, 40, worker="REVIEWER")
    worker = report(owner)["workers"]["RANKER"]
    assert worker["suggestions"] == []
    # Every enabled chat model is accounted for: screened out at a gate, or left without evidence.
    assert worker["screened"] == {"considered": 8, "rejected": {
        "CONTEXT_TOO_SMALL": 1, "CONTEXT_UNKNOWN": 1, "NO_EVIDENCE": 4, "NO_STRUCTURED_OUTPUT": 1,
        "STRUCTURED_OUTPUT_UNKNOWN": 1}}


def test_a_switch_never_repeats_a_model_and_keeps_at_most_four_backups(owner):
    log(owner, "Groq", 10, status="FAILED", error="TIMEOUT")
    log(owner, "roomy", 40, worker="REVIEWER")
    chain = report(owner, fallback_model="steady", secondary_models=["steady", "spare", "extra", "roomy", "off"])
    change = chain["workers"]["RANKER"]["suggestions"][0]["apply"]
    assert [change["fallback_model"], *change["secondary_models"]] == ["Groq", "steady", "spare", "extra"]


def test_a_tier_workers_model_is_kept_as_the_first_backup(owner):
    log(owner, "roomy", 40, worker="REVIEWER")
    snap = snapshot()
    snap["tiers"] = {"FAST": {"model": "Groq", "enabled": True}}
    snap["workers"]["RANKER"] = {"mode": "TIER", "tier": "FAST", "enabled": True}
    with SessionLocal() as db:
        worker = worker_insights.report(db, owner, snap)["workers"]["RANKER"]
    assert worker["current"] == "Groq"
    assert worker["suggestions"][0]["apply"] == {"model_mode": "FIXED", "primary_model": "roomy",
                                                 "fallback_model": "Groq", "secondary_models": []}


def test_the_embedder_gets_context_only(owner):
    log(owner, "vectors", 40, worker="EMBEDDER")
    worker = report(owner)["workers"]["EMBEDDER"]
    assert worker["requests"] == 40 and worker["best"] == [] and worker["suggestions"] == []


def test_the_catalog_ranks_models_nobody_here_has_measured(owner, monkeypatch):
    log(owner, "Groq", 30)
    monkeypatch.setattr(worker_insights, "_catalog_scores", lambda *a: {"Groq": 40.0, "roomy": 80.0, "spare": 30.0,
                                                                       "tiny": 99.0})
    suggestions = report(owner)["workers"]["RANKER"]["suggestions"]
    assert [(s["model"], s["basis"], s["catalog_score"]) for s in suggestions] == [("roomy", "CATALOG", 80.0)]
    assert suggestions[0]["apply"]["primary_model"] == "roomy"


def test_loading_the_tab_never_fetches_a_catalog(owner, monkeypatch):
    def refuse(user_id):
        def fetch():
            raise AssertionError("fetched a catalog while a screen loaded")
        return {name: (True, fetch, len) for name in model_market.LABELS}

    monkeypatch.setattr(model_market, "_fetchers", refuse)
    monkeypatch.setattr(model_market, "_states", {})
    log(owner, "Groq", 30)
    assert report(owner)["workers"]["RANKER"]["suggestions"] == []
    assert all(state.data is None for state in model_market.load(owner, cached_only=True).values())


def test_a_failure_never_breaks_the_screen(owner, monkeypatch):
    monkeypatch.setattr(worker_insights, "_report", lambda *a, **k: 1 / 0)
    assert report(owner) == {"window_days": 30, "min_samples": 20, "workers": {}, "error": True}


# ───────────── every account: rates only ─────────────

@pytest.fixture
def accounts():
    made, underlying = [], f"openai/{PREFIX}{uuid.uuid4().hex}"
    with SessionLocal() as db:
        for i in range(3):
            user = User(email=f"{PREFIX}{uuid.uuid4().hex}@example.com", password_hash="!")
            db.add(user)
            db.flush()
            db.add(AIModel(user_id=user.id, litellm_model_name=f"alias {i}", provider="Groq",
                           underlying_model=underlying.upper() if i else underlying))
            made.append(user.id)
        db.commit()
    yield made, f"groq/{underlying}"
    with SessionLocal() as db:
        db.execute(delete(LLMRequestLog).where(LLMRequestLog.user_id.in_(made)))
        db.execute(delete(AIModel).where(AIModel.user_id.in_(made)))
        db.execute(delete(User).where(User.id.in_(made)))
        db.commit()


def test_platform_figures_need_enough_accounts_and_carry_only_rates(accounts, monkeypatch):
    users, key = accounts
    for i, user in enumerate(users[:2]):
        log(user, f"alias {i}", 30, valid=True)
    log(users[0], "alias 0", 10, status="FAILED", error="TIMEOUT")
    assert key not in worker_insights.platform_model_stats(refresh=True)  # two accounts: one could be singled out
    log(users[2], "alias 2", 30, valid=False)
    figures = worker_insights.platform_model_stats(refresh=True)[key]
    assert figures == {"success_pct": 90.0, "schema_valid_pct": 66.7, "avg_ms": 100, "score": 85.7}
    assert worker_insights.platform_model_stats()[key] == figures  # cached
    monkeypatch.setattr(get_settings(), "platform_model_stats_min_accounts", 4)
    assert key not in worker_insights.platform_model_stats(refresh=True)
    monkeypatch.setattr(get_settings(), "platform_model_stats_enabled", False)
    assert worker_insights.platform_model_stats(refresh=True) == {}


def test_a_model_that_did_well_across_accounts_is_suggested(owner, monkeypatch):
    log(owner, "Groq", 10, status="FAILED", error="TIMEOUT")
    log(owner, "Groq", 20)
    snap = snapshot()
    snap["models"]["roomy"].update(provider="groq", underlying_model="OpenAI/Shared")
    monkeypatch.setattr(worker_insights, "platform_model_stats", lambda: {
        "groq/openai/shared": {"success_pct": 99.0, "schema_valid_pct": None, "avg_ms": 400, "score": 99.4}})
    with SessionLocal() as db:
        suggestions = worker_insights.report(db, owner, snap)["workers"]["RANKER"]["suggestions"]
    assert [(s["model"], s["basis"]) for s in suggestions] == [("roomy", "PLATFORM")]
    assert suggestions[0]["stats"] == {"success_pct": 99.0, "schema_valid_pct": None, "avg_ms": 400}


# ───────────── the API ─────────────

def test_the_api_reports_insights_and_a_switch_applies_as_one_version(account, litellm):
    chat = {"max_input_tokens": 128000, "supports_response_schema": True}
    litellm.deployments += [deployment(name, f"groq/{name}", info=chat) for name in ("first", "second", "third")]
    account.call("POST", "/models/sync")
    account.worker("RANKER", model_mode="FIXED", primary_model="first", fallback_model="third")
    version = account.apply()["version"]
    log(account.user_id, "first", 10, status="FAILED", error="TIMEOUT")
    log(account.user_id, "first", 20)
    log(account.user_id, "second", 40, worker="REVIEWER")
    try:
        worker = account.get("/insights")["workers"]["RANKER"]
        assert worker["context"]["avg_input_tokens"] == 1000 and worker["best"][0]["model"] == "first"
        change = worker["suggestions"][0]["apply"]
        assert change == {"model_mode": "FIXED", "primary_model": "second", "fallback_model": "first",
                          "secondary_models": ["third"]}
        account.worker("RANKER", **change)
        assert account.apply()["version"] == version + 1
        config = account.get("")["active"]["workers"]["RANKER"]
        assert (config["primary_model"], config["fallback_model"], config["secondary_models"]) == \
            ("second", "first", ["third"])
    finally:
        with SessionLocal() as db:
            db.execute(delete(LLMRequestLog).where(LLMRequestLog.user_id == account.user_id))
            db.commit()
