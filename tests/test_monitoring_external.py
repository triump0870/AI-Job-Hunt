"""The External APIs, Apify and Logs sections: what they aggregate, what they hide, and what they keep.

Rows are written through the real instrumentation layer wherever that is the thing under test, so these are
end-to-end checks of "make a call → see it in Monitoring", not tests of a serializer.
"""
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.main import app
from app.models import ExternalAPIRequestLog, ExternalAPIUsageDaily, ProviderCooldown, SearchRun, SystemLogEntry
from app.services import ai_advisor, backoff, external_api, external_monitoring, sources, system_log, tracing
from app.settings import get_settings
from tests.conftest import ensure_test_user, make_user

client = TestClient(app)


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def owner():
    return ensure_test_user().id


@pytest.fixture(autouse=True)
def clean(owner):
    with SessionLocal() as db:
        db.execute(delete(ExternalAPIRequestLog))
        db.execute(delete(ExternalAPIUsageDaily))
        db.execute(delete(SystemLogEntry))
        db.execute(delete(ProviderCooldown))
        db.execute(delete(SearchRun).where(SearchRun.user_id == owner))
        db.commit()
    backoff.reset_cache()
    yield


def call_row(owner, *, provider="APIFY", service="apify_api", operation="dataset_fetch", status="SUCCEEDED",
             latency_ms=500, error_type=None, http_status_code=200, retry_count=0, cost=None, when=None,
             workflow="hunter", trace_id=None, job_id=None):
    """A telemetry row written directly, for the aggregation cases that need exact numbers."""
    at = when or utcnow()
    with SessionLocal() as db:
        db.add(ExternalAPIRequestLog(
            request_id=str(uuid.uuid4()), trace_id=trace_id, user_id=owner, provider=provider, service=service,
            operation=operation, workflow=workflow, status=status, http_status_code=http_status_code,
            latency_ms=latency_ms, error_type=error_type, retry_count=retry_count, attempt=retry_count + 1,
            estimated_cost=None if cost is None else Decimal(str(cost)),
            cost_source="provider_reported" if cost is not None else "unknown",
            job_id=job_id, started_at=at, completed_at=at, created_at=at))
        db.commit()


def search_run(owner, **fields):
    with SessionLocal() as db:
        run = SearchRun(**{"user_id": owner, "provider": "apify", "external_run_id": uuid.uuid4().hex,
                           "actor_id": "openclawai/job-board-scraper", "status": "SUCCEEDED", **fields})
        db.add(run)
        db.commit()
        return run.id


# ───────────── the dashboard ─────────────

def test_the_dashboard_counts_calls_latency_retries_and_failures(owner):
    for _ in range(8):
        call_row(owner, latency_ms=200)
    call_row(owner, latency_ms=4000, retry_count=2)
    call_row(owner, provider="OPENROUTER", service="llm", operation="chat_completion", status="FAILED",
             error_type="RATE_LIMIT", http_status_code=429, workflow="ranking", cost=0.002)

    out = client.get("/api/monitoring/external/summary?range=7d").json()
    assert out["requests"] == 10 and out["failed"] == 1
    assert out["success_rate"] == 0.9 and out["rate_limited"] == 1
    assert out["retries"] == 2 and out["retried_requests"] == 1
    # eight calls at 200ms, one slow call at 4000ms and one failure at the helper default of 500ms
    assert out["p95_latency_ms"] == 4000 and out["avg_latency_ms"] == pytest.approx(610, abs=1)
    assert out["estimated_cost"] == pytest.approx(0.002)

    providers = client.get("/api/monitoring/external/providers").json()["providers"]
    assert [p["provider"] for p in providers] == ["APIFY", "OPENROUTER"]
    assert providers[1]["failure_rate"] == 1.0 and providers[1]["rate_limit_rate"] == 1.0


def test_percentiles_come_from_the_database_rows_not_an_estimate(owner):
    for ms in range(1, 101):
        call_row(owner, latency_ms=ms)
    latency = client.get("/api/monitoring/external/latency").json()["overall"]
    assert latency["p50_latency_ms"] == 50 and latency["p95_latency_ms"] == 95 and latency["p99_latency_ms"] == 99


def test_calls_can_be_filtered_by_provider_operation_workflow_status_and_errors(owner):
    call_row(owner, provider="APIFY", operation="dataset_fetch", workflow="hunter")
    call_row(owner, provider="OPENROUTER", service="llm", operation="chat_completion", workflow="ranking")
    call_row(owner, provider="OPENROUTER", service="llm", operation="chat_completion", workflow="ranking",
             status="FAILED", error_type="TIMEOUT", http_status_code=None)

    def total(query):
        return client.get(f"/api/monitoring/external/requests?{query}").json()["total"]

    assert total("provider=OPENROUTER") == 2
    assert total("provider=openrouter") == 2, "provider matching is case-insensitive"
    assert total("operation=dataset_fetch") == 1
    assert total("workflow=hunter") == 1
    assert total("status=FAILED") == 1
    assert total("error_only=true") == 1
    assert total("range=today") == 3


def test_the_filter_lists_hold_every_choice_in_the_range(owner):
    """Options come from the range, not from the filtered rows.

    Building them from the filtered rows removed the chosen value's siblings from the list — so after filtering
    to failures, a provider with only successful calls (Apify, typically) vanished and could not be selected.
    """
    call_row(owner, provider="APIFY", operation="actor_run_status", workflow="apify_sync")
    call_row(owner, provider="APIFY", operation="dataset_fetch", workflow="apify_sync")
    call_row(owner, provider="OPENROUTER", service="llm", operation="chat_completion", workflow="ranking",
             status="FAILED", error_type="RATE_LIMIT", http_status_code=429)

    for query in ("", "status=FAILED", "provider=OPENROUTER", "error_type=RATE_LIMIT", "http_status_code=429"):
        facets = client.get(f"/api/monitoring/external/dashboard?{query}").json()["facets"]
        assert facets["providers"] == ["APIFY", "OPENROUTER"], f"a provider disappeared with {query!r}"
        assert facets["operations"] == ["actor_run_status", "chat_completion", "dataset_fetch"]
        assert facets["workflows"] == ["apify_sync", "ranking"]

    # The options are still only this account's, and only this range's.
    other = make_user(SessionLocal, email=f"other-{uuid.uuid4().hex[:8]}@example.com")
    call_row(other, provider="GEMINI")
    call_row(owner, provider="EMAIL", when=utcnow() - timedelta(days=40))
    facets = client.get("/api/monitoring/external/filters?range=7d").json()
    assert "GEMINI" not in facets["providers"] and "EMAIL" not in facets["providers"]
    assert facets["error_types"] == ["RATE_LIMIT"] and facets["status_codes"] == [200, 429]
    assert set(facets["all_error_types"]) >= {"TIMEOUT", "SERVER_ERROR"}, "every type is offered for a wider range"


def test_successful_apify_calls_are_recorded_and_findable(owner):
    """The successful half of Apify's traffic is telemetry like any other, not only its failures."""
    for _ in range(4):
        call_row(owner, provider="APIFY", operation="actor_run_status", workflow="apify_sync")
    call_row(owner, provider="APIFY", operation="actor_run_status", workflow="apify_sync", status="FAILED",
             error_type="CLIENT_ERROR", http_status_code=403)

    assert client.get("/api/monitoring/external/requests?provider=APIFY").json()["total"] == 5
    assert client.get("/api/monitoring/external/requests?provider=APIFY&status=SUCCEEDED").json()["total"] == 4
    assert client.get("/api/monitoring/external/requests?provider=apify&status=SUCCEEDED").json()["total"] == 4
    assert client.get("/api/monitoring/external/requests?provider=APIFY&error_type=CLIENT_ERROR").json()["total"] == 1
    assert client.get("/api/monitoring/external/requests?http_status_code=403").json()["total"] == 1
    assert client.get("/api/monitoring/external/requests?error_type=NONSENSE").status_code == 422
    assert client.get("/api/monitoring/external/requests?http_status_code=99").status_code == 422


def test_the_total_matches_what_the_filtered_list_returns(owner):
    """The count and the rows come from one definition of the filter, so they cannot drift apart."""
    for _ in range(6):
        call_row(owner, provider="APIFY", operation="dataset_fetch")
    for _ in range(2):
        call_row(owner, provider="APIFY", operation="dataset_fetch", status="FAILED", error_type="TIMEOUT",
                 http_status_code=None)
    listed = client.get("/api/monitoring/external/requests?provider=APIFY&error_type=TIMEOUT&per_page=100").json()
    assert listed["total"] == len(listed["items"]) == 2
    assert all(item["error_type"] == "TIMEOUT" for item in listed["items"])


def test_the_request_list_is_paginated(owner):
    for _ in range(12):
        call_row(owner)
    first = client.get("/api/monitoring/external/requests?page=1&per_page=5").json()
    last = client.get("/api/monitoring/external/requests?page=3&per_page=5").json()
    assert first["total"] == 12 and first["pages"] == 3 and len(first["items"]) == 5
    assert len(last["items"]) == 2
    assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in last["items"]})


def test_a_call_detail_shows_safe_fields_only(owner):
    with external_api.external_client("APIFY", service="apify_api", operation="actor_run_start", user_id=owner,
                                      transport=httpx.MockTransport(lambda r: httpx.Response(201, json={"ok": 1})),
                                      headers={"Authorization": "Bearer apify_api_secrettoken1234"}) as http:
        http.post("https://api.apify.com/v2/actors/x~y/runs", json={"query": "engineer"},
                  extensions={"metadata": {"actor_id": "x/y"}})
    row_id = client.get("/api/monitoring/external/requests").json()["items"][0]["id"]
    detail = client.get(f"/api/monitoring/external/requests/{row_id}").json()
    assert detail["provider"] == "APIFY" and detail["http_status_code"] == 201
    assert detail["metadata"] == {"actor_id": "x/y"} and detail["llm"] is None
    assert "secrettoken1234" not in str(detail) and "Authorization" not in str(detail)
    assert client.get("/api/monitoring/external/requests/999999").status_code == 404


def test_one_account_cannot_read_anothers_telemetry(owner):
    other = make_user(SessionLocal, email=f"other-{uuid.uuid4().hex[:8]}@example.com")
    call_row(other, provider="APIFY")
    call_row(owner, provider="OPENROUTER")
    listed = client.get("/api/monitoring/external/requests").json()
    assert listed["total"] == 1 and listed["items"][0]["provider"] == "OPENROUTER"
    with SessionLocal() as db:
        theirs = db.scalar(select(ExternalAPIRequestLog.id).where(ExternalAPIRequestLog.user_id == other))
    assert client.get(f"/api/monitoring/external/requests/{theirs}").status_code == 404


# ───────────── Apify ─────────────

def test_apify_usage_comes_from_what_apify_reported(owner):
    search_run(owner, item_count=120, created_jobs=22, compute_units=0.042, usage_total_usd=Decimal("0.0137"),
               memory_mb=2048, completed_at=utcnow(),
               stats={"import": {"seen": 120, "normalized": 118, "created": 22, "duplicates": 96}})
    search_run(owner, item_count=0, created_jobs=0, status="FAILED", completed_at=utcnow())
    call_row(owner, operation="actor_run_start")

    out = client.get("/api/monitoring/apify/summary").json()
    assert out["runs"] == 2 and out["succeeded"] == 1 and out["failed"] == 1 and out["success_rate"] == 0.5
    assert out["runs_today"] == 2
    assert out["compute_units"] == 0.042 and out["runs_with_reported_usage"] == 1
    assert out["reported_cost"] == pytest.approx(0.0137) and out["runs_with_reported_cost"] == 1
    assert out["jobs_discovered"] == 120 and out["jobs_deduplicated"] == 96 and out["jobs_after_deduplication"] == 22
    assert out["api"]["requests"] == 1
    assert [a["actor_id"] for a in out["by_actor"]] == ["openclawai/job-board-scraper"]


def test_runs_of_multi_location_actors_group_by_their_locations(owner):
    """Regression: an actor whose input takes a list of locations crashed the summary (and with it the AI
    Optimization Advisor) with "unhashable type: 'list'"."""
    search_run(owner, input_json={"location": ["Bengaluru", "Singapore"]}, completed_at=utcnow())
    search_run(owner, input_json={"location": "Bengaluru"}, completed_at=utcnow())
    search_run(owner, input_json={"location": {"city": "Pune", "country": "India"}}, completed_at=utcnow())
    search_run(owner, input_json={}, location="Remote", completed_at=utcnow())

    out = client.get("/api/monitoring/apify/summary").json()
    assert {row["location"]: row["runs"] for row in out["by_location"]} == {
        "Bengaluru, Singapore": 1, "Bengaluru": 1, "Pune, India": 1, "Remote": 1}
    assert {card["location"] for card in out["recent_runs"]} >= {"Bengaluru, Singapore", "Pune, India"}
    with SessionLocal() as db:
        now = utcnow()
        aggregates = external_monitoring.advisor_aggregates(db, owner, now - timedelta(days=1), now + timedelta(days=1))
    assert aggregates is not None


def test_apify_usage_that_was_not_reported_stays_unknown(owner):
    search_run(owner, item_count=10, created_jobs=1, completed_at=utcnow())
    out = client.get("/api/monitoring/apify/summary").json()
    assert out["compute_units"] is None and out["reported_cost"] is None, "unknown usage is never guessed at"
    assert out["runs_with_reported_cost"] == 0
    assert out["by_actor"][0]["compute_units"] is None and out["by_actor"][0]["reported_cost"] is None


def test_apify_reported_usage_is_read_off_the_run(owner):
    """apply_apify_usage copies Apify's own figures; anything missing from the payload is left alone."""
    run_id = search_run(owner, item_count=0, created_jobs=0)
    payload = {"usage": {"ACTOR_COMPUTE_UNITS": 0.25, "DATASET_WRITES": 300}, "usageTotalUsd": 0.08,
               "options": {"memoryMbytes": 4096}, "stats": {"runTimeSecs": 61.2}}
    with SessionLocal() as db:
        run = db.get(SearchRun, run_id)
        sources.apply_apify_usage(run, payload)
        db.commit()
        db.refresh(run)
        assert run.compute_units == 0.25 and float(run.usage_total_usd) == 0.08 and run.memory_mb == 4096
        assert run.stats["apify_usage"]["DATASET_WRITES"] == 300
        sources.apply_apify_usage(run, {})
        assert run.compute_units == 0.25, "an empty payload never erases what Apify already reported"


def test_the_apify_run_list_is_paginated_and_names_the_setup(owner):
    for _ in range(4):
        search_run(owner, item_count=5, created_jobs=1, completed_at=utcnow())
    page = client.get("/api/monitoring/apify/runs?per_page=3&page=1").json()
    assert len(page["items"]) == 3 and page["has_more"] is True
    assert set(page["items"][0]) >= {"actor_id", "jobs_discovered", "jobs_created", "compute_units", "reported_cost"}


# ───────────── logs ─────────────

def test_logs_are_filtered_searched_and_paginated(owner):
    system_log.log_event(category="SCHEDULER", level="INFO", message="hunter succeeded", operation="hunter",
                         user_id=owner, status="succeeded", duration_ms=4200)
    system_log.log_event(category="EXTERNAL_API", level="ERROR", message="APIFY dataset_fetch failed: TIMEOUT",
                         provider="APIFY", operation="dataset_fetch", user_id=owner, status="TIMEOUT")
    system_log.log_event(category="APIFY", level="WARNING", message="Apify run aborted", provider="APIFY",
                         user_id=owner)

    assert client.get("/api/monitoring/logs").json()["total"] == 3
    assert client.get("/api/monitoring/logs?level=ERROR").json()["total"] == 1
    assert client.get("/api/monitoring/logs?category=SCHEDULER").json()["total"] == 1
    assert client.get("/api/monitoring/logs?provider=APIFY").json()["total"] == 2
    assert client.get("/api/monitoring/logs?search=dataset").json()["total"] == 1
    paged = client.get("/api/monitoring/logs?per_page=2&page=2").json()
    assert len(paged["items"]) == 1 and paged["pages"] == 2
    assert client.get("/api/monitoring/logs?category=NOPE").status_code == 422


def test_a_log_entry_links_to_the_call_it_describes(owner):
    with external_api.external_client("APIFY", service="apify_api", operation="dataset_fetch", user_id=owner,
                                      transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))) as http:
        http.get("https://api.apify.com/v2/datasets/x/items")
    entry = client.get("/api/monitoring/logs").json()["items"][0]
    detail = client.get(f"/api/monitoring/logs/{entry['id']}").json()
    assert detail["level"] == "ERROR" and detail["category"] == "APIFY"
    assert detail["external_request"]["http_status_code"] == 500
    assert detail["details"]["host"] == "api.apify.com"


def test_a_trace_gathers_everything_one_piece_of_work_did(owner):
    with tracing.trace("hunter", user_id=owner) as ctx:
        call_row(owner, trace_id=ctx.trace_id, operation="actor_run_start")
        call_row(owner, trace_id=ctx.trace_id, provider="OPENROUTER", service="llm", operation="chat_completion")
        system_log.log_event(category="SCHEDULER", message="hunter succeeded", user_id=owner)
    out = client.get(f"/api/monitoring/external/traces/{ctx.trace_id}").json()
    assert len(out["calls"]) == 2 and out["providers"] == ["APIFY", "OPENROUTER"]
    assert [e["message"] for e in out["events"]] == ["hunter succeeded"]


# ───────────── retention ─────────────

def test_old_calls_roll_up_into_daily_totals_and_are_deleted(owner):
    old = utcnow() - timedelta(days=120)
    for _ in range(3):
        call_row(owner, when=old, latency_ms=100, cost=0.001)
    call_row(owner, when=old, latency_ms=900, status="FAILED", error_type="TIMEOUT", http_status_code=None)
    call_row(owner, latency_ms=50)  # recent: must survive

    with SessionLocal() as db:
        result = external_monitoring.prune(db, utcnow(), retention_days=90)
        assert result == {"rolled_up": 1, "deleted": 4}
        remaining = db.scalars(select(ExternalAPIRequestLog)).all()
        daily = db.scalar(select(ExternalAPIUsageDaily))
        assert len(remaining) == 1 and remaining[0].latency_ms == 50
        assert daily.requests == 4 and daily.failures == 1 and daily.timeouts == 1
        assert daily.latency_ms_max == 900 and daily.latency_ms_sum == 1200
        assert float(daily.estimated_cost) == pytest.approx(0.003) and daily.unpriced_requests == 1


def test_old_log_lines_are_deleted(owner):
    with SessionLocal() as db:
        db.add(SystemLogEntry(created_at=utcnow() - timedelta(days=45), level="INFO", category="SYSTEM",
                              message="ancient", user_id=owner))
        db.add(SystemLogEntry(created_at=utcnow(), level="INFO", category="SYSTEM", message="recent", user_id=owner))
        db.commit()
        assert system_log.prune(db, utcnow(), retention_days=30) == {"deleted": 1}
        assert [e.message for e in db.scalars(select(SystemLogEntry)).all()] == ["recent"]


def test_retention_can_be_turned_off(owner):
    call_row(owner, when=utcnow() - timedelta(days=400))
    with SessionLocal() as db:
        assert external_monitoring.prune(db, utcnow(), retention_days=0) == {"rolled_up": 0, "deleted": 0}
        assert system_log.prune(db, utcnow(), retention_days=0) == {"deleted": 0}


def test_the_cleanup_job_applies_every_retention(owner):
    from app.services import scheduled_jobs as sj

    call_row(owner, when=utcnow() - timedelta(days=400))
    with SessionLocal() as db:
        db.add(SystemLogEntry(created_at=utcnow() - timedelta(days=400), level="INFO", category="SYSTEM",
                              message="ancient", user_id=owner))
        db.commit()
    ctx = sj.JobContext(sessions=SessionLocal, trigger="manual", now=utcnow(), worker_id="test", interval_minutes=15)
    out = sj.cleanup(ctx)
    assert out["external_api"]["deleted"] == 1 and out["system_logs"]["deleted"] == 1
    assert "telemetry" in out


# ───────────── the advisor ─────────────

def test_the_advisor_is_given_aggregated_external_metrics(owner):
    for _ in range(12):
        call_row(owner, provider="APIFY", operation="dataset_fetch", latency_ms=300)
    for _ in range(3):
        call_row(owner, provider="APIFY", operation="dataset_fetch", status="FAILED", error_type="RATE_LIMIT",
                 http_status_code=429)
    search_run(owner, item_count=200, created_jobs=1, completed_at=utcnow(), compute_units=0.9,
               usage_total_usd=Decimal("0.3"), stats={"import": {"seen": 200, "created": 1, "duplicates": 199}})

    with SessionLocal() as db:
        aggregates = external_monitoring.advisor_aggregates(db, owner, utcnow() - timedelta(days=7), utcnow())
    assert aggregates["totals"]["requests"] == 15 and aggregates["totals"]["rate_limited"] == 3
    assert aggregates["providers"][0]["provider"] == "APIFY"
    assert aggregates["apify"]["runs"] == 1 and aggregates["apify"]["reported_cost"] == pytest.approx(0.3)
    assert "endpoint_path" not in str(aggregates) and "request_id" not in str(aggregates)

    findings = ai_advisor.detect_external_findings(aggregates)
    assert any("rate limiting" in f["recommendation"]["finding"] for f in findings)
    for finding in findings:
        assert set(finding["recommendation"]) >= {"finding", "evidence", "recommended_change", "expected_benefit",
                                                  "risk"}


def test_the_advisor_only_recommends(owner):
    """No rule in the advisor is allowed to touch configuration; they all produce text."""
    findings = ai_advisor.detect_external_findings({
        "providers": [{"provider": "APIFY", "requests": 50, "failed": 20, "failure_rate": 0.4, "timeouts": 5,
                       "server_errors": 10, "rate_limited": 0, "rate_limit_rate": 0.0, "retries": 0,
                       "retry_rate": 0.0}],
        "operations": [], "apify": {}, "apify_by_actor": []})
    assert findings and all(f["recommendation"]["category"] in
                            ("RELIABILITY", "COST", "LATENCY", "ARCHITECTURE", "EXTERNAL_API") for f in findings)
    settings = get_settings()
    before = {k: getattr(settings, k) for k in vars(settings) if not k.startswith("_")}
    ai_advisor.detect_external_findings({})
    assert {k: getattr(settings, k) for k in before} == before, "detecting findings changes no configuration"


# ───────────── LLM telemetry meets external telemetry ─────────────

def test_an_llm_call_is_one_provider_call_with_two_views(owner, monkeypatch):
    from app.services.llm import LLMClient

    with SessionLocal() as db:
        db.info["user_id"] = owner
        with tracing.trace("ranking", workflow_type="job", workflow_entity_id=5, user_id=owner) as ctx:
            LLMClient(overrides={"llm_provider": "mock"}, user_id=owner).generate_json("system", "FIT_ANALYSIS\njob")

    from app.models import LLMRequestLog

    with SessionLocal() as db:
        external = db.scalar(select(ExternalAPIRequestLog).where(ExternalAPIRequestLog.provider == "MOCK"))
        llm = db.scalar(select(LLMRequestLog).order_by(LLMRequestLog.id.desc()))
    assert external is not None and external.service == "llm" and external.operation == "chat_completion"
    assert external.workflow == "ranking" and external.trace_id == ctx.trace_id
    assert llm.external_request_id == external.request_id, "the LLM row points at the generic row, not a copy"
    assert llm.trace_id == ctx.trace_id

    detail = client.get(f"/api/monitoring/external/requests/{external.id}").json()
    assert detail["llm"]["stage"] == "OTHER" or detail["llm"]["stage"]
    assert detail["llm"]["model"] == "mock"


def test_rate_limits_count_only_outbound_calls_and_keep_last_reported_windows(owner, monkeypatch):
    from app.services import provider_usage

    now = utcnow()
    monkeypatch.setattr(provider_usage, "utcnow", lambda: now)
    other = make_user(SessionLocal)

    def record(seconds, provider="LITELLM", metadata=None, status=200, headers=None, user_id=owner):
        at = now - timedelta(seconds=seconds)
        call = external_api.ExternalCall(provider=provider, user_id=user_id, metadata=metadata or {})
        external_api.persist(external_api.build_entry(call, started_at=at, completed_at=at, status_code=status,
                                                       headers=httpx.Headers(headers or {})))

    groq = {"x-litellm-model-api-base": "https://api.groq.com/openai/v1",
            "x-litellm-model-name": "groq/openai/gpt-oss-20b"}
    quota = {"x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "0",
             "x-ratelimit-reset-requests": "2m59.56s", "llm_provider-retry-after": "2",
             "x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "20",
             "x-ratelimit-reset-tokens": "7.66s"}
    record(70, headers={**groq, **quota, "x-ratelimit-remaining-requests": "1"})
    record(59, headers={**groq, **quota}, status=429, metadata={"model": "router", "payload": "private text"})
    record(1, headers=groq, metadata={"model": "router"})
    record(60, metadata={"model": "old-gateway-route", "rate_limits": {"requests_window": "day"}})
    record(60, provider="APIFY", status=500)
    record(86399, provider="OPENROUTER")
    record(86400, provider="TOO_OLD")
    record(-1, provider="FUTURE")
    record(0, provider="MOCK")
    record(1, provider="OTHER_ACCOUNT", user_id=other, headers=quota)
    for flags in ({"skipped_by": "cooldown"}, {"skipped_by": "token_pacing"}, {"blocked_by": "pre_send_policy"},
                  {"not_sent": True}):
        record(1, provider="HELD", metadata=flags, status=None)
    record(1, metadata={"model": "legacy-alias"})

    result = client.get("/api/monitoring/rate-limits?all_accounts=true")
    assert result.status_code == 200, result.text
    body = result.json()
    providers = {p["provider"]: p for p in body["providers"]}
    assert set(providers) == {"APIFY", "GROQ", "LITELLM", "OPENROUTER"}
    assert providers["GROQ"] == {"provider": "GROQ", "calls_minute": 2, "calls_day": 4, "http_429_day": 1,
                                 "models": [{"model": "groq/openai/gpt-oss-20b", "calls_minute": 2, "calls_day": 3},
                                            {"model": "old-gateway-route", "calls_minute": 0, "calls_day": 1}]}
    assert providers["APIFY"]["calls_day"] == 1 and providers["APIFY"]["calls_minute"] == 0
    assert providers["OPENROUTER"]["calls_day"] == 1 and providers["OPENROUTER"]["calls_minute"] == 0
    snapshot, = body["snapshots"]
    assert snapshot["provider"] == "GROQ" and snapshot["requests_window"] == "day"
    assert snapshot["requests_limit"] == 1000 and snapshot["requests_remaining"] == 0
    assert snapshot["tokens_per_minute_limit"] == 8000 and snapshot["tokens_per_minute_remaining"] == 20
    assert snapshot["retry_after_seconds"] == 2 and snapshot["tokens_reset_seconds"] == 7.66
    assert snapshot["requests_reset_seconds"] == 179.56 and "private text" not in result.text


def test_openrouter_key_quota_only_returns_reported_counters(owner, monkeypatch):
    from app.services import provider_usage

    key = "a-private-test-key"
    monkeypatch.setattr(provider_usage, "user_keys", lambda db, user_id: {"openrouter": key if user_id == owner else None})
    client_factory = external_api.external_client

    def handle(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/key"
        assert request.headers["Authorization"] == f"Bearer {key}" and request.extensions[external_api.NO_CAPTURE]
        return httpx.Response(200, json={"data": {"label": "private account", "usage": 1234, "is_free_tier": False,
                                                 "free_model_daily_requests": {"limit": 50, "used": 50, "remaining": 0}}})
    monkeypatch.setattr(external_api, "external_client", lambda *args, **kwargs: client_factory(
        *args, **kwargs, transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(get_settings(), "external_api_debug_payloads", True)
    response = client.get("/api/monitoring/rate-limits/openrouter")
    assert response.status_code == 200
    body = response.json()
    assert body.pop("observed_at") and body == {"status": "reported", "used": 50, "limit": 50, "remaining": 0}
    with SessionLocal() as db:
        call = db.scalar(select(ExternalAPIRequestLog).where(ExternalAPIRequestLog.provider == "OPENROUTER_ACCOUNT"))
        assert call.user_id == owner and call.payload is None
        assert key not in str(call.request_metadata) and "private account" not in str(call.request_metadata)


@pytest.mark.parametrize("code, body, status", [
    (200, {"data": {"usage_daily": 10, "is_free_tier": True}}, "unreported"),
    (200, {"data": {"free_model_daily_requests": {"used": False, "remaining": -1}}}, "unreported"),
    (429, {}, "unavailable"),
])
def test_openrouter_quota_unknown_and_unavailable(owner, monkeypatch, code, body, status):
    from app.services import provider_usage

    factory = external_api.external_client
    monkeypatch.setattr(provider_usage, "user_keys", lambda *_: {"openrouter": "test"})
    monkeypatch.setattr(external_api, "external_client", lambda *args, **kwargs: factory(
        *args, **kwargs, transport=httpx.MockTransport(lambda _: httpx.Response(code, json=body))))
    assert client.get("/api/monitoring/rate-limits/openrouter").json() == {"status": status}


def test_openrouter_quota_without_account_key_or_with_custom_endpoint_stays_local(owner, monkeypatch):
    from app.services import provider_usage

    monkeypatch.setattr(provider_usage, "user_keys", lambda *_: {})
    assert client.get("/api/monitoring/rate-limits/openrouter").json() == {"status": "no_key"}
    monkeypatch.setattr(get_settings(), "openrouter_base_url", "https://private.example/v1")
    assert client.get("/api/monitoring/rate-limits/openrouter").json() == {"status": "custom_endpoint"}


def test_wrapped_sdk_429_counts_once_and_preserves_the_provider_wait(owner):
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(429, request=request, headers={"retry-after": "3"})
    with pytest.raises(RuntimeError), external_api.external_operation("OPENROUTER", user_id=owner,
                                                                      metadata={"model": "example:free"}):
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError("typed adapter error") from exc
    data = client.get("/api/monitoring/rate-limits").json()
    assert data["providers"][0]["http_429_day"] == data["providers"][0]["calls_day"] == 1
    assert data["snapshots"][0]["retry_after_seconds"] == 3


def test_unspecified_quota_keeps_the_reported_limit_and_zero_remaining(owner):
    with external_api.external_client("EXAMPLE", user_id=owner, transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"llm_provider-x-ratelimit-limit": "40",
                                                   "llm_provider-x-ratelimit-remaining": "0",
                                                   "x-ratelimit-reset": "90"}))) as http:
        http.get("https://example.test/")
    snapshot, = client.get("/api/monitoring/rate-limits").json()["snapshots"]
    assert snapshot["limit"] == 40 and snapshot["remaining"] == 0 and snapshot["reset_at"]
    assert snapshot["requests_window"] is None and snapshot["tokens_per_minute_limit"] is None
