"""Model failover: route health, thresholds, ordering by algorithm, the API and an end-to-end switch."""
import uuid
from datetime import UTC, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.main import app
from app.models import AppSetting, FailoverEvent, LLMRequestLog, ModelRouteState
from app.services import failover, failover_settings, model_health, model_resolution, rate_limits
from app.services.llm import LLMClient, LLMRateLimitError
from app.services.llm_telemetry import LLMCall
from app.services.model_resolution import resolve
from tests.conftest import ensure_test_user
from tests.test_ai_models import snapshot
from tests.test_litellm_gateway import gateway, ranking_prompt, run  # noqa: F401

client = TestClient(app)
CFG = failover_settings.resolve({})


@pytest.fixture
def owner():
    return ensure_test_user().id


@pytest.fixture(autouse=True)
def clean():
    def wipe():
        with SessionLocal() as db:
            db.execute(delete(ModelRouteState))
            db.execute(delete(FailoverEvent))
            db.execute(delete(LLMRequestLog).where(LLMRequestLog.request_id.like("failover-test-%")))
            for row in db.scalars(select(AppSetting)).all():  # thresholds a test saved never leak into the next one
                if "failover" in (row.preferences or {}):
                    row.preferences = {k: v for k, v in row.preferences.items() if k != "failover"}
            db.commit()
        model_health.reset_cache()
        failover.reset_cache()
    wipe()
    yield
    wipe()


def outcome(owner, model="mid", status="SUCCEEDED", error_type=None, worker="RANKER", fallback_used=False,
            rate_limit=None):
    model_health.record_outcome({"user_id": owner, "provider": "litellm", "requested_model": model,
                                 "status": status, "error_type": error_type, "worker_type": worker,
                                 "fallback_used": fallback_used}, rate_limit=rate_limit)


def route(owner, model="mid", worker=""):
    with SessionLocal() as db:
        return db.scalar(select(ModelRouteState).where(ModelRouteState.user_id == owner,
                                                       ModelRouteState.model == model,
                                                       ModelRouteState.worker_type == worker))


def events(owner):
    with SessionLocal() as db:
        return db.scalars(select(FailoverEvent).where(FailoverEvent.user_id == owner).order_by(FailoverEvent.id)).all()


# ───────────── judging a window: pure ─────────────

def rows(n_ok=0, n_fail=0, kind="PROVIDER_ERROR", latency=100):
    return ([{"status": "SUCCEEDED", "latency_ms": latency}] * n_ok
            + [{"status": "FAILED", "error_type": kind}] * n_fail)


def test_too_few_requests_are_never_judged():
    assert model_health.judge(model_health.summarize(rows(0, 4)), CFG) is None  # 4 < min_samples (5)


def test_failure_rate_rate_limits_schema_and_latency_breach_thresholds():
    assert model_health.judge(model_health.summarize(rows(2, 6)), CFG)[0] == "ERROR_RATE"
    assert model_health.judge(model_health.summarize(rows(10, 3, "RATE_LIMIT")), CFG)[0] == "RATE_LIMIT"
    assert model_health.judge(model_health.summarize(rows(2, 6, "SCHEMA_VALIDATION")), CFG)[0] == "SCHEMA"
    slow = model_health.summarize(rows(10, 0, latency=9000))
    assert model_health.judge(slow, CFG) is None  # no latency limit by default
    assert model_health.judge(slow, {**CFG, "max_p95_ms": 5000})[0] == "LATENCY"
    assert model_health.judge(slow, {**CFG, "degradation_pct": 50}, baseline_p95=2000)[0] == "LATENCY"
    assert model_health.judge(slow, {**CFG, "degradation_pct": 50}, baseline_p95=8000) is None


def test_configuration_errors_and_failovers_are_counted_correctly():
    stats = model_health.summarize(rows(5, 3, "CONFIGURATION") + [{"status": "SUCCEEDED", "fallback_used": True}])
    assert stats.samples == 6 and stats.failures == 1  # config errors say nothing about a model; a failover counts


# ───────────── per-request outcomes rest and recover a route ─────────────

def test_a_rate_limit_rests_the_model_once_and_notifies(owner):
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")  # still cooling: the same outage, not a new event
    state = route(owner)
    assert state.state == "COOLING" and state.cooling_until > model_health.utcnow()
    assert model_health.resting(owner, "mid", "RANKER") and not model_health.resting(owner, "other", "RANKER")
    found = events(owner)
    assert len(found) == 1 and found[0].cause == "RATE_LIMIT" and "mid" in found[0].message


def test_failures_in_a_row_rest_the_route_for_that_worker_only(owner):
    for _ in range(CFG["consecutive_failures"] - 1):
        outcome(owner, status="FAILED", error_type="PROVIDER_ERROR")
    assert not model_health.resting(owner, "mid", "RANKER")
    outcome(owner, status="FAILED", error_type="PROVIDER_ERROR")
    assert model_health.resting(owner, "mid", "RANKER") and not model_health.resting(owner, "mid", "TAILOR")


def test_a_success_between_failures_resets_the_run(owner):
    for _ in range(CFG["consecutive_failures"] - 1):
        outcome(owner, status="FAILED", error_type="PROVIDER_ERROR")
    outcome(owner)  # success
    outcome(owner, status="FAILED", error_type="PROVIDER_ERROR")
    assert not model_health.resting(owner, "mid", "RANKER")


def test_disabled_failover_and_configuration_errors_never_rest_a_route(owner):
    outcome(owner, status="FAILED", error_type="CONFIGURATION")
    assert route(owner) is None
    with SessionLocal() as db:
        failover_settings.update(db, owner, {"enabled": False})
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    assert route(owner) is None
    with SessionLocal() as db:
        failover_settings.update(db, owner, {"enabled": True})


def test_a_rested_route_recovers_after_its_cooldown_and_enough_probe_successes(owner):
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    with SessionLocal() as db:
        db.query(ModelRouteState).update({"cooling_until": model_health.utcnow() - timedelta(seconds=1)})
        db.commit()
    model_health.reset_cache()
    assert not model_health.resting(owner, "mid", "RANKER")  # half-open: traffic probes it
    outcome(owner)
    assert route(owner).state == "COOLING"  # one success is not enough (recovery_successes = 2)
    outcome(owner)
    assert route(owner).state == "HEALTHY"
    assert [e.cause for e in events(owner)] == ["RATE_LIMIT", "RECOVERED"]


def test_a_failed_probe_rests_the_route_again_for_longer(owner):
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    first = route(owner).cooling_until - model_health.utcnow()
    with SessionLocal() as db:
        db.query(ModelRouteState).update({"cooling_until": model_health.utcnow() - timedelta(seconds=1)})
        db.commit()
    model_health.reset_cache()
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    state = route(owner)
    assert state.stats["trips"] == 2 and state.cooling_until - model_health.utcnow() > first


def test_the_sweep_rests_a_route_whose_window_breaches_a_threshold(owner):
    with SessionLocal() as db:
        for i in range(8):
            ok = i < 2
            db.add(LLMRequestLog(request_id=f"failover-test-{uuid.uuid4()}", user_id=owner, provider="litellm", stage="OTHER",
                                 requested_model="mid", model="mid", worker_type="RANKER",
                                 status="SUCCEEDED" if ok else "FAILED", error_type=None if ok else "PROVIDER_ERROR",
                                 latency_ms=100, created_at=model_health.utcnow()))
        db.commit()
        result = model_health.evaluate(db, owner)
    assert result["tripped"] == 1 and route(owner, worker="RANKER").state == "DEGRADED"
    assert events(owner)[0].cause == "ERROR_RATE"
    with SessionLocal() as db:
        assert model_health.evaluate(db, owner)["tripped"] == 0  # already resting: no second event
    assert len(events(owner)) == 1


# ───────────── choosing a model ─────────────

def worker():
    return resolve(snapshot(RANKER={"mode": "FIXED", "primary_model": "mid", "fallback_model": "fast",
                                    "secondary_models": ["deep", "mid", "fast"]}), "RANKER")


def test_secondary_models_are_ordered_and_deduplicated():
    w = worker()
    assert w.model == "mid" and w.chain() == ["fast", "deep"]


def test_a_healthy_primary_is_left_alone(owner):
    w = worker()
    assert failover.apply(w, owner) is w


def test_a_resting_primary_moves_to_the_next_healthy_model_and_keeps_the_rest_as_fallbacks(owner):
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    moved = failover.apply(worker(), owner)
    assert moved.model == "fast" and moved.failed_over_from == "mid"
    assert moved.chain() == ["deep"]  # a rate-limited primary is not called at all while another model can answer
    assert "failover" in moved.note


def test_priority_prefers_configured_order_and_other_algorithms_prefer_the_better_route(owner):
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    with SessionLocal() as db:
        db.add_all([ModelRouteState(user_id=owner, worker_type="RANKER", model="fast", score=60.0,
                                    stats={"p95_ms": 900, "failure_pct": 20}, updated_at=model_health.utcnow()),
                    ModelRouteState(user_id=owner, worker_type="RANKER", model="deep", score=95.0,
                                    stats={"p95_ms": 300, "failure_pct": 1}, updated_at=model_health.utcnow())])
        db.commit()
    model_health.reset_cache()
    for algorithm, expected in (("PRIORITY", "fast"), ("LATENCY", "deep"), ("LEAST_ERRORS", "deep"),
                                ("WEIGHTED", "deep")):
        with SessionLocal() as db:
            failover_settings.update(db, owner, {"algorithm": algorithm})
        failover.reset_cache()
        assert failover.apply(worker(), owner).model == expected, algorithm


def test_a_worker_without_secondary_models_is_never_changed(owner):
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    alone = resolve(snapshot(RANKER={"mode": "FIXED", "primary_model": "mid"}), "RANKER")
    assert failover.apply(alone, owner) is alone


def test_failover_fails_open(owner, monkeypatch):
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    monkeypatch.setattr(failover, "order", lambda *a, **k: 1 / 0)
    w = worker()
    assert failover.apply(w, owner) is w


# ───────────── thresholds ─────────────

def test_settings_are_validated_and_defaults_are_not_stored(owner):
    with SessionLocal() as db:
        out = failover_settings.update(db, owner, {"max_failure_pct": 30, "algorithm": "LATENCY"})
        assert out["values"]["max_failure_pct"] == 30 and out["values"]["algorithm"] == "LATENCY"
        for bad in ({"max_failure_pct": 0}, {"algorithm": "RANDOM"}, {"nonsense": 1}, {"enabled": "yes"},
                    {"cooldown_seconds": 3000, "max_cooldown_seconds": 100}):
            with pytest.raises(failover_settings.FailoverSettingError):
                failover_settings.update(db, owner, bad)
        failover_settings.update(db, owner, {"max_failure_pct": 50, "algorithm": "PRIORITY"})
        assert failover_settings.for_user(db, owner) == failover_settings.resolve({})


def test_api_settings_routes_events_and_reset(owner):
    assert client.get("/api/failover/settings").json()["values"]["algorithm"] == "PRIORITY"
    assert client.put("/api/failover/settings", json={"values": {"max_rate_limits": 2}}).json()["values"][
        "max_rate_limits"] == 2
    assert client.put("/api/failover/settings", json={"values": {"max_rate_limits": 0}}).status_code == 400
    client.put("/api/failover/settings", json={"values": {"max_rate_limits": 3}})
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    listed = client.get("/api/failover/routes").json()
    assert listed["resting"] == 1 and listed["routes"][0]["model"] == "mid" and listed["routes"][0]["resting"]
    feed = client.get("/api/failover/events?since_id=0").json()
    assert len(feed["events"]) == 1 and feed["latest_id"] == feed["events"][0]["id"] and feed["notify"]
    assert client.get(f"/api/failover/events?since_id={feed['latest_id']}").json()["events"] == []
    client.post("/api/failover/events/ack", json={"up_to_id": feed["latest_id"]})
    assert client.get("/api/failover/events?since_id=0").json()["events"][0]["acknowledged"]
    assert client.post("/api/failover/routes/reset", json={"model": "mid"}).status_code == 200
    assert client.get("/api/failover/routes").json()["resting"] == 0
    assert client.post("/api/failover/routes/reset", json={"model": "never-seen"}).status_code == 404


# ───────────── end to end through the gateway ─────────────

def test_a_rate_limited_primary_fails_over_to_a_secondary_model_and_back(gateway, litellm):  # noqa: F811
    gateway.worker("RANKER", fallback_model="tier-1-fast", secondary_models=["tier-3-review"])
    gateway.apply()
    uid = gateway.user_id
    primary = model_resolution.get(uid, "RANKER").model
    assert model_resolution.get(uid, "RANKER").chain() == ["tier-1-fast", "tier-3-review"]

    litellm.rate_limited.add(primary)
    with pytest.raises(LLMRateLimitError):
        run(gateway, ranking_prompt())  # the 429 itself still fails this one request ...
    assert model_health.resting(uid, primary, "RANKER")

    result = run(gateway, ranking_prompt())  # ... and the next one goes to the secondary model
    assert result.routing["requested_model"] == "tier-1-fast"
    with SessionLocal() as db:
        last = db.scalars(select(LLMRequestLog).where(LLMRequestLog.user_id == uid)
                          .order_by(LLMRequestLog.id.desc())).first()
    assert last.requested_model == "tier-1-fast" and last.routing_cause == "FAILOVER"
    assert last.failed_over_from == primary  # the request's own fact, read by Running workers for finished tasks
    assert [e.cause for e in events(uid)] == ["RATE_LIMIT"]
    with SessionLocal() as db:  # Running workers reads the same plan: where the next request goes and why
        plan = LLMClient.for_user(db, uid).route_for("RANKER")
    assert plan["route"] == "LITELLM" and plan["model"] == "tier-1-fast" and plan["failed_over_from"] == primary
    assert plan["primary"] == primary and plan["mode"] == "TIER" and plan["configured"] == "PRIMARY_REASONING"
    assert LLMClient and LLMCall  # imported for the fixtures' sake


# ───────────── a model's own fallbacks ─────────────

def test_a_models_own_fallbacks_follow_the_workers_backups_and_take_over_when_it_rests(owner):
    snap = snapshot(RANKER={"mode": "FIXED", "primary_model": "mid", "fallback_model": "fast"})
    snap["models"]["mid"]["fallback_models"] = ["deep", "fast", "mid", "off"]  # repeats, itself, a disabled one
    w = resolve(snap, "RANKER")
    assert w.model_fallbacks == ("deep", "fast") and w.chain() == ["fast", "deep"]
    alone = resolve(snapshot(RANKER={"mode": "FIXED", "primary_model": "mid"}) | {
        "models": {**snapshot()["models"], "mid": {"enabled": True, "fallback_models": ["deep"]}}}, "RANKER")
    assert alone.chain() == ["deep"]  # no backups on the worker: the model's fallback still applies
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    moved = failover.apply(alone, owner)
    assert moved.model == "deep" and moved.failed_over_from == "mid" and moved.chain() == []


def test_a_model_rests_for_its_own_time_before_it_is_tried_again(owner, monkeypatch):
    monkeypatch.setattr(model_resolution, "active_snapshot",
                        lambda uid: (3, {"models": {"mid": {"enabled": True, "rest_seconds": 3600}}}))
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    rest = route(owner).cooling_until - model_health.utcnow()
    assert timedelta(minutes=59) < rest <= timedelta(hours=1)
    assert model_health._cooldown(CFG, 1) == timedelta(seconds=CFG["cooldown_seconds"])  # no rest time: the setting
    assert model_health._cooldown(CFG, 3, 3600) == timedelta(hours=1)  # a model's own rest time never grows
    assert model_health._cooldown(CFG, 2) == timedelta(seconds=2 * CFG["cooldown_seconds"])  # the setting doubles


def test_a_rate_limit_rests_the_model_for_the_accounts_rate_limit_rest(owner):
    with SessionLocal() as db:
        failover_settings.update(db, owner, {"rate_limit_rest_minutes": 30})
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    rest = route(owner).cooling_until - model_health.utcnow()
    assert timedelta(minutes=29) < rest <= timedelta(minutes=30)
    cfg = {**CFG, "rate_limit_rest_minutes": 30}
    assert model_health._cooldown(cfg, 4, None, "RATE_LIMIT") == timedelta(minutes=30)  # never grows
    assert model_health._cooldown(cfg, 1, None, "ERROR_RATE") == timedelta(seconds=CFG["cooldown_seconds"])  # 429s only
    assert model_health._cooldown(cfg, 1, 3600, "RATE_LIMIT") == timedelta(hours=1)  # the model's own time wins


OLLAMA_CAP = ('Ollama_chatException - {"error":"You reached the Free usage limit. Pro is $20/month for $60 of usage, '
              'with no 5-hour or weekly caps: https://ollama.com/upgrade"}')


def test_a_usage_cap_rests_the_model_for_the_accounts_usage_rest(owner):
    outcome(owner, status="FAILED", error_type="RATE_LIMIT", rate_limit=rate_limits.classify(OLLAMA_CAP))
    rest = route(owner).cooling_until - model_health.utcnow()
    assert timedelta(hours=4, minutes=59) < rest <= timedelta(hours=5)  # the default: 300 minutes, not 60 seconds
    assert "usage limit reached" in route(owner).reason


def test_a_named_reset_holds_the_model_until_then_even_when_it_is_already_resting(owner):
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")  # a plain 429: the 60-second cooldown
    assert route(owner).cooling_until - model_health.utcnow() <= timedelta(seconds=60)
    reset = model_health.utcnow() + timedelta(hours=3)
    reset_ms = int(reset.replace(tzinfo=UTC).timestamp() * 1000)
    quota = rate_limits.classify(f'"free-models-per-day" "X-RateLimit-Reset":"{reset_ms}"')
    assert quota.kind == rate_limits.QUOTA
    outcome(owner, status="FAILED", error_type="RATE_LIMIT", rate_limit=quota)
    assert abs((route(owner).cooling_until - reset).total_seconds()) < 1  # held until the provider's reset
    assert [e.cause for e in events(owner)] == ["RATE_LIMIT"]  # one outage, one event


def test_a_request_too_large_for_the_rate_limit_does_not_rest_the_model(owner):
    too_large = rate_limits.classify("Request too large for model `x` on tokens per minute (TPM): Limit 8000, "
                                     "Requested 9197, please reduce your message size and try again")
    outcome(owner, status="FAILED", error_type="RATE_LIMIT", rate_limit=too_large)
    assert route(owner) is None and not model_health.resting(owner, "mid")
    stats = model_health.summarize([{"status": "FAILED", "error_type": "RATE_LIMIT",
                                     "error_message": "Request too large for model x"}] * 5)
    assert stats.rate_limits == 0 and stats.samples == 0


def test_a_usage_capped_model_through_the_gateway_rests_until_the_usage_rest(gateway, litellm):  # noqa: F811
    gateway.worker("RANKER", fallback_model="tier-1-fast")
    gateway.apply()
    uid = gateway.user_id
    primary = model_resolution.get(uid, "RANKER").model
    litellm.rate_limited.add(primary)
    litellm.rate_limit_messages[primary] = OLLAMA_CAP
    with pytest.raises(LLMRateLimitError) as raised:
        run(gateway, ranking_prompt())
    assert raised.value.rate_limit.kind == rate_limits.USAGE_CAP
    with SessionLocal() as db:
        row = db.scalar(select(ModelRouteState).where(ModelRouteState.user_id == uid, ModelRouteState.model == primary))
    assert row.cooling_until - model_health.utcnow() > timedelta(hours=4)


def test_a_rate_limited_model_test_rests_that_model_not_the_gateways_admin_calls():
    from app.services.external_api import cooldown_provider

    # a model test (service model_test) is a model request like a worker's: a 429 rests only that model, so Apply
    # and Sync (admin calls, no model) still reach LiteLLM
    assert cooldown_provider("LITELLM", "model_test", {"model": "groq/llama-guard-3-8b"}) == "LITELLM:groq/llama-guard-3-8b"
    assert cooldown_provider("LITELLM", "llm", {"model": "mid"}) == "LITELLM:mid"
    assert cooldown_provider("LITELLM", "admin", None) == "LITELLM"
    assert cooldown_provider("APIFY", "actor", {"model": "x"}) == "APIFY"


def test_apply_during_a_litellm_cooldown_is_a_429_not_a_server_error(monkeypatch):
    from app.services import ai_model_config
    from app.services.external_api import RateLimitedCooldown

    def cooling(*_a, **_k):
        raise RateLimitedCooldown("LITELLM", 12)

    monkeypatch.setattr(ai_model_config, "apply", cooling)
    response = client.post("/api/settings/ai/apply", json={})
    assert response.status_code == 429 and "LITELLM" in response.json()["detail"]


def test_a_resting_backup_moves_behind_the_healthy_ones_while_the_primary_keeps_its_place(owner):
    w = resolve(snapshot(RANKER={"mode": "FIXED", "primary_model": "mid", "fallback_model": "fast",
                                 "secondary_models": ["deep"]}), "RANKER")
    assert w.chain() == ["fast", "deep"]
    outcome(owner, model="fast", status="FAILED", error_type="RATE_LIMIT")
    moved = failover.apply(w, owner)
    assert moved.model == "mid" and moved.failed_over_from is None and moved.chain() == ["deep"]  # fast: rate limited


# ───────────── provider-wide rate limits ─────────────

OPENROUTER_RPM = ('LLMRateLimitError: LiteLLM request failed for model=X: litellm.RateLimitError: RateLimitError: '
                  'OpenrouterException - {"error":{"message":"Rate limit exceeded: requests per minute"}}')
OPENROUTER_FREE = ('LLMRateLimitError: litellm.RateLimitError: RateLimitError: OpenrouterException - {"error":'
                   '{"message":"Rate limit exceeded: free-models-per-day. Add 10 credits"}}')
PROVIDERS = {"models": {
    "or-paid": {"provider": "openrouter", "underlying_model": "deepseek/deepseek-chat"},
    "or-free": {"provider": "openrouter", "underlying_model": "nvidia/nemotron:free"},
    "groq-a": {"provider": "groq", "underlying_model": "openai/gpt-oss-120b"},
    "Router B4": {"provider": "litellm", "model_type": "AUTO_ROUTER"},
    "Router X": {"provider": "litellm", "model_type": "AUTO_ROUTER"},
    "local": {"provider": "ollama", "underlying_model": "qwen2.5:7b"},
    "cloud": {"provider": "ollama", "underlying_model": "beam:cloud"},
}, "routing": {"auto_router_model_name": "jobhunt-auto"}}


@pytest.fixture
def providers(monkeypatch):
    monkeypatch.setattr(model_resolution, "active_snapshot", lambda uid: (1, PROVIDERS))


def limit(owner, model, message, *, worker="RANKER", fallbacks_sent=False):
    model_health.record_outcome({"user_id": owner, "provider": "litellm", "requested_model": model,
                                 "status": "FAILED", "error_type": "RATE_LIMIT", "worker_type": worker,
                                 "error_message": message}, rate_limit=rate_limits.classify(message),
                                fallbacks_sent=fallbacks_sent)


def test_the_provider_named_in_a_429_is_read_from_litellms_error():
    assert rate_limits.provider_named(OPENROUTER_RPM) == "openrouter"
    assert rate_limits.provider_named("RateLimitError: GroqException - {") == "groq"
    assert rate_limits.provider_named(OLLAMA_CAP) == "ollama_cloud"  # only Ollama's hosted models have limits
    assert rate_limits.provider_named("litellm.RateLimitError: RateLimitError: plain") is None
    assert rate_limits.free_pool(OPENROUTER_FREE) and not rate_limits.free_pool(OPENROUTER_RPM)


def test_a_429_naming_its_provider_rests_every_model_of_it_for_every_worker(owner, providers):
    limit(owner, "or-paid", OPENROUTER_RPM, worker="RANKER")
    assert model_health.resting(owner, "or-free", "REVIEWER") and model_health.limited_until(owner, "or-free", "TAILOR")
    assert not model_health.resting(owner, "groq-a", "REVIEWER")
    row = route(owner, "provider:openrouter")
    assert row.state == "COOLING" and route(owner, "or-paid") is None  # one rest for the provider, not per model
    assert [e.message.split(":")[0] for e in events(owner)] == ["Every openrouter model is resting"]
    limit(owner, "or-free", OPENROUTER_RPM)  # the same outage again: still one event
    assert len(events(owner)) == 1


def test_the_free_model_quota_rests_only_free_models(owner, providers):
    limit(owner, "or-free", OPENROUTER_FREE)
    assert model_health.resting(owner, "or-free") and not model_health.resting(owner, "or-paid")
    assert route(owner, "provider:openrouter:free").stats["free_only"] is True
    assert route(owner, "provider:openrouter") is None
    limit(owner, "or-free", OPENROUTER_RPM)  # a limit that is not the free quota rests paid models too
    assert model_health.resting(owner, "or-paid")


def test_a_short_paid_rest_never_inherits_the_free_quota_deadline(owner, providers):
    with SessionLocal() as db:
        cfg = failover_settings.for_user(db, owner)
        now = model_health.utcnow()
        midnight = now + timedelta(hours=10)
        model_health._rest_provider(db, owner, "or-free", "openrouter", "free quota", midnight, cfg, now, True, False)
        model_health._rest_provider(db, owner, "or-paid", "openrouter", "rpm", now + timedelta(seconds=30), cfg, now,
                                    False, False)
        db.commit()
    model_health.reset_cache()
    later = model_health.utcnow() + timedelta(hours=1)
    assert model_health.limited_until(owner, "or-free", now=later) is not None  # the free quota still rests
    assert model_health.limited_until(owner, "or-paid", now=later) is None  # the per-minute limit has cleared
    paid = route(owner, "provider:openrouter")
    assert paid.cooling_until < midnight and paid.stats["free_only"] is False


def test_a_router_alias_joins_its_provider_only_from_a_429_without_fallbacks(owner, providers):
    limit(owner, "Router X", OPENROUTER_RPM, fallbacks_sent=True)  # LiteLLM reports the last provider it tried
    assert route(owner, "provider:openrouter").stats["members"] == []
    assert route(owner, "Router X").state == "COOLING"  # the alias itself still rests, as before
    limit(owner, "Router B4", OPENROUTER_RPM)
    assert route(owner, "provider:openrouter").stats["members"] == ["Router B4"]
    assert route(owner, "Router B4") is None  # covered by the provider's rest
    assert model_health.provider_keys(owner, "Router B4") == ["provider:openrouter"]
    assert model_health.resting(owner, "Router B4", "REVIEWER")
    limit(owner, "jobhunt-auto", OPENROUTER_RPM)  # the app's own auto router spans providers: never a member
    assert "jobhunt-auto" not in route(owner, "provider:openrouter").stats["members"]


def test_ollama_cloud_limits_never_rest_local_models(owner, providers):
    limit(owner, "cloud", OLLAMA_CAP)
    assert model_health.resting(owner, "cloud") and not model_health.resting(owner, "local")


def test_a_429_naming_no_provider_rests_only_that_model(owner, providers):
    limit(owner, "or-paid", "LLMRateLimitError: Rate limit reached")
    assert route(owner, "or-paid").state == "COOLING" and route(owner, "provider:openrouter") is None
    assert not model_health.resting(owner, "or-free")


def test_a_provider_rest_ends_and_recovers_like_any_route(owner, providers):
    limit(owner, "or-paid", OPENROUTER_RPM)
    with SessionLocal() as db:
        db.execute(ModelRouteState.__table__.update().where(ModelRouteState.model == "provider:openrouter")
                   .values(cooling_until=model_health.utcnow() - timedelta(seconds=1)))
        db.commit()
    model_health.reset_cache()
    assert not model_health.resting(owner, "or-free")  # half-open: traffic probes it
    for _ in range(CFG["recovery_successes"]):
        outcome(owner, model="or-free")
    assert route(owner, "provider:openrouter").state == "HEALTHY"


def test_a_person_puts_a_provider_back_in_service(owner, providers):
    limit(owner, "or-paid", OPENROUTER_RPM)
    response = client.post("/api/failover/routes/reset", json={"model": "provider:openrouter", "worker_type": ""})
    assert response.status_code == 200, response.text
    assert not model_health.resting(owner, "or-free")


def test_the_routes_api_shows_a_provider_rest(owner, providers):
    limit(owner, "Router B4", OPENROUTER_FREE)
    item = next(r for r in client.get("/api/failover/routes").json()["routes"]
                if r["model"] == "provider:openrouter:free")
    assert item["provider"] == "openrouter" and item["members"] == ["Router B4"] and item["free_only"] is True


# ───────────── skipping and holding rate-limited models ─────────────

def chained(**escalation):
    snap = snapshot(RANKER={"mode": "FIXED", "primary_model": "mid", "fallback_model": "fast",
                            "secondary_models": ["deep"], **({"escalation": escalation} if escalation else {})})
    return resolve(snap, "RANKER")


def test_an_error_rested_backup_is_still_a_last_resort(owner):
    for _ in range(CFG["consecutive_failures"]):
        outcome(owner, model="fast", status="FAILED", error_type="PROVIDER_ERROR")
    assert failover.apply(chained(), owner).chain() == ["deep", "fast"]  # errors, not a rate limit: kept last


def test_when_every_model_is_rate_limited_the_request_is_held_until_the_first_clears(owner):
    w = chained()
    assert failover.held_seconds(w, owner) is None
    for model in ("mid", "fast"):
        outcome(owner, model=model, status="FAILED", error_type="RATE_LIMIT")
    assert failover.held_seconds(w, owner) is None  # deep can still answer
    outcome(owner, model="deep", status="FAILED", error_type="RATE_LIMIT")
    held = failover.held_seconds(w, owner)
    assert held and held <= CFG["cooldown_seconds"]
    with backoff_bypass():
        assert failover.held_seconds(w, owner) is None  # a task a person asked to run now still sends
    assert client.put("/api/failover/settings", json={"values": {"enabled": False}}).status_code == 200
    failover.reset_cache()
    assert failover.held_seconds(w, owner) is None


def test_the_apps_auto_router_moves_behind_a_backup_while_every_tier_model_is_rate_limited(owner):
    from dataclasses import replace

    w = replace(chained(), model="jobhunt-auto", router_model="jobhunt-auto",
                router_tiers={"SIMPLE": "tier-a", "COMPLEX": "tier-b"})
    cfg = failover_settings.resolve({})
    assert failover.order(w, owner, cfg)[0] == "jobhunt-auto"
    for model in ("tier-a", "tier-b"):
        outcome(owner, model=model, status="FAILED", error_type="RATE_LIMIT")
    ordered = failover.order(w, owner, cfg)
    assert ordered[0] != "jobhunt-auto" and "jobhunt-auto" not in ordered  # a backup that can answer goes first


def backoff_bypass():
    from app.services import backoff

    return backoff.bypass()


def test_the_escalation_model_falls_back_only_to_its_own_fallbacks(owner):
    snap = snapshot(RANKER={"mode": "FIXED", "primary_model": "mid", "fallback_model": "fast",
                            "escalation": {"model": "deep", "conditions": ["LOW_CONFIDENCE"],
                                           "confidence_threshold": 0.9}})
    snap["models"]["deep"]["fallback_models"] = ["big"]
    w = resolve(snap, "RANKER")
    assert w.escalation.fallbacks == ("big",) and "big" in w.capabilities
    assert failover.escalation_target(w, owner) == "deep"
    outcome(owner, model="deep", status="FAILED", error_type="RATE_LIMIT")
    assert failover.escalation_target(w, owner) == "big"
    outcome(owner, model="big", status="FAILED", error_type="RATE_LIMIT")
    assert failover.escalation_target(w, owner) is None  # never "fast", the worker's own backup


# ───────────── end to end: holds and escalation through the gateway ─────────────

def rest(uid, model, minutes=60):
    with SessionLocal() as db:
        model_health.trip(db, uid, model, "", cause="RATE_LIMIT", reason="test", cfg=CFG,
                          not_before=model_health.utcnow() + timedelta(minutes=minutes))
        db.commit()
    model_health.reset_cache()


def escalating(account, **extra):
    account.worker("RANKER", escalation={"model": "tier-3-review", "conditions": ["LOW_CONFIDENCE", "MANUAL"],
                                         "confidence_threshold": 0.85, **extra})
    account.apply()


def test_a_rate_limited_escalation_model_keeps_the_first_answer_without_calling_it(gateway, litellm):  # noqa: F811
    from app.models import SystemLogEntry

    escalating(gateway)
    rest(gateway.user_id, "tier-3-review")
    litellm.answer("tier-2-reasoning", {"score": 60, "confidence": 0.5})
    result = run(gateway, ranking_prompt(), workflow_type="job", entity_id=7)
    assert result.data == {"score": 60, "confidence": 0.5}
    assert result.routing["escalation_skipped"] == {"reason": "LOW_CONFIDENCE", "cause": "RATE_LIMITED",
                                                    "model": "tier-3-review"}
    assert [b["model"] for b in litellm.chat_bodies] == ["tier-2-reasoning"]  # nothing sent to the resting model
    with SessionLocal() as db:
        line = db.scalars(select(SystemLogEntry).where(SystemLogEntry.user_id == gateway.user_id,
                                                       SystemLogEntry.operation == "escalation_skipped")).one()
    assert line.status == "RATE_LIMITED" and line.entity_id == 7 and "first answer stands" in line.message


def test_an_escalation_that_hits_a_429_keeps_the_first_answer(gateway, litellm):  # noqa: F811
    escalating(gateway)
    litellm.answer("tier-2-reasoning", {"score": 60, "confidence": 0.5})
    litellm.rate_limited.add("tier-3-review")
    result = run(gateway, ranking_prompt())
    assert result.data["score"] == 60 and result.routing["escalation_skipped"]["cause"] == "RATE_LIMIT"
    assert [b["model"] for b in litellm.chat_bodies] == ["tier-2-reasoning", "tier-3-review"]


def test_a_manual_escalation_still_reports_its_failure(gateway, litellm):  # noqa: F811
    escalating(gateway)
    litellm.answer("tier-2-reasoning", {"score": 60, "confidence": 0.99})
    litellm.rate_limited.add("tier-3-review")
    with pytest.raises(LLMRateLimitError):
        run(gateway, ranking_prompt(), escalate=True)


def test_a_schema_failure_with_nowhere_to_escalate_keeps_its_own_error(gateway, litellm):  # noqa: F811
    from app.services.llm import LLMResponseError

    gateway.worker("RANKER", escalation={"model": "tier-3-review", "conditions": ["SCHEMA_FAILURE"]})
    gateway.apply()
    rest(gateway.user_id, "tier-3-review")
    litellm.answer("tier-2-reasoning", "this is not json")
    with pytest.raises(LLMResponseError) as raised:
        run(gateway, ranking_prompt())
    assert not getattr(raised.value, "_jobhunt_escalated", False)  # its correction retry is still allowed
    assert [b["model"] for b in litellm.chat_bodies] == ["tier-2-reasoning"]


def test_every_model_rate_limited_sends_nothing_and_gives_the_wait(gateway, litellm):  # noqa: F811
    gateway.worker("RANKER", fallback_model="tier-1-fast")
    gateway.apply()
    uid = gateway.user_id
    for model in (model_resolution.get(uid, "RANKER").model, "tier-1-fast"):
        rest(uid, model, minutes=10)
    before = len(logs_of(uid))
    with pytest.raises(LLMRateLimitError) as raised:
        run(gateway, ranking_prompt())
    assert 0 < raised.value.held_seconds <= 600 and litellm.chat_bodies == []
    assert len(logs_of(uid)) == before  # a held request is not a provider call


def test_rate_limited_backups_are_not_sent_as_litellm_fallbacks(gateway, litellm):  # noqa: F811
    gateway.worker("RANKER", fallback_model="tier-1-fast", secondary_models=["tier-3-review"])
    gateway.apply()
    rest(gateway.user_id, "tier-1-fast")
    run(gateway, ranking_prompt())
    assert litellm.chat_bodies[-1]["fallbacks"] == ["tier-3-review"]


def logs_of(uid):
    with SessionLocal() as db:
        return db.scalars(select(LLMRequestLog).where(LLMRequestLog.user_id == uid)).all()
