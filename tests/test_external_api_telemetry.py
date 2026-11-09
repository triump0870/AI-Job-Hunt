"""Every third-party call is measured by the layer that makes it, and nothing secret is kept.

These tests drive the real InstrumentedClient against httpx.MockTransport, so what they exercise is the same
code path production uses. No test calls a logging function: that is the point of the design.
"""
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import ExternalAPIRequestLog, ProviderCooldown, SystemLogEntry
from app.services import backoff, external_api, system_log, tracing
from app.settings import get_settings
from tests.conftest import ensure_test_user

SECRET_TOKEN = "apify_api_supersecrettoken123"
OPENROUTER_RESET_MS = int((datetime.now(UTC) + timedelta(minutes=30)).timestamp() * 1000)


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def owner():
    return ensure_test_user().id


@pytest.fixture(autouse=True)
def clean_telemetry():
    """Start each test with no telemetry and no cooldown.

    A 429 in one test starts a real backoff for the test account (app.services.backoff), which would hold off
    the next test's calls — the behaviour is right, but each test here is about one call of its own.
    """
    with SessionLocal() as db:
        db.execute(delete(ExternalAPIRequestLog))
        db.execute(delete(SystemLogEntry))
        db.execute(delete(ProviderCooldown))
        db.commit()
    backoff.reset_cache()
    yield


def responder(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/timeout"):
        raise httpx.ReadTimeout("timed out", request=request)
    if path.endswith("/dns"):
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided")
    if path.endswith("/refused"):
        raise httpx.ConnectError("connection refused")
    if path.endswith("/boom"):
        raise RuntimeError("the SDK exploded")
    if path.endswith("/limited"):
        return httpx.Response(429, headers={"x-ratelimit-remaining": "0", "retry-after": "45"}, json={"error": "slow down"})
    if path.endswith("/limited-forwarded"):  # OpenRouter's reset in milliseconds, as the LiteLLM proxy forwards it
        return httpx.Response(429, headers={"llm_provider-x-ratelimit-reset": str(OPENROUTER_RESET_MS)}, json={"error": "slow"})
    if path.endswith("/missing"):
        return httpx.Response(404, json={"error": "no such actor"})
    if path.endswith("/broken"):
        return httpx.Response(503, text="upstream unavailable")
    return httpx.Response(200, json={"data": {"id": "run-1", "status": "SUCCEEDED"}})


def client(provider="APIFY", **kwargs):
    kwargs.setdefault("service", "apify_api")
    kwargs.setdefault("operation", "dataset_fetch")
    return external_api.external_client(provider, transport=httpx.MockTransport(responder), **kwargs)


def logged(**where) -> list[ExternalAPIRequestLog]:
    with SessionLocal() as db:
        query = select(ExternalAPIRequestLog).order_by(ExternalAPIRequestLog.id)
        for column, value in where.items():
            query = query.where(getattr(ExternalAPIRequestLog, column) == value)
        return list(db.scalars(query).all())


def only(**where) -> ExternalAPIRequestLog:
    rows = logged(**where)
    assert len(rows) == 1, f"expected one row, found {len(rows)}"
    return rows[0]


# ───────────── the call is measured because of where it was made, not what the caller remembered ─────────────

def test_a_plain_request_is_logged_without_the_caller_doing_anything(owner):
    with client(user_id=owner) as http:
        response = http.get("https://api.apify.com/v2/datasets/abc/items")
    assert response.status_code == 200

    row = only()
    assert (row.provider, row.service, row.operation) == ("APIFY", "apify_api", "dataset_fetch")
    assert row.status == "SUCCEEDED" and row.http_status_code == 200 and row.error_type is None
    assert row.http_method == "GET" and row.endpoint_host == "api.apify.com"
    assert row.endpoint_path == "/v2/datasets/abc/items"
    assert row.user_id == owner and row.latency_ms is not None and row.latency_ms >= 0
    assert row.response_size_bytes and row.request_size_bytes is not None
    assert row.completed_at >= row.started_at and row.attempt == 1 and row.retry_count == 0


def test_an_operation_and_its_metadata_can_be_named_per_request(owner):
    with client(user_id=owner) as http:
        http.post("https://api.apify.com/v2/actors/x~y/runs", json={"query": "engineer"},
                  extensions={"operation": "actor_run_start", "metadata": {"actor_id": "x/y", "run_id": "run-1"}})
    row = only()
    assert row.operation == "actor_run_start" and row.http_method == "POST"
    assert row.request_metadata == {"actor_id": "x/y", "run_id": "run-1"}
    assert row.request_size_bytes and row.request_size_bytes > 0


def test_a_provider_nobody_has_integrated_yet_is_measured_the_same_way(owner):
    """A future integration gets telemetry from the client, not from a list of known providers."""
    with external_api.external_client("SOME_FUTURE_API", service="v1", operation="search",
                                      transport=httpx.MockTransport(responder), user_id=owner) as http:
        http.get("https://future.example.test/v1/search")
    row = only()
    assert row.provider == "SOME_FUTURE_API" and row.operation == "search" and row.status == "SUCCEEDED"


# ───────────── failures ─────────────

@pytest.mark.parametrize(("path", "error_type", "status_code"), [
    ("/timeout", "TIMEOUT", None),
    ("/dns", "DNS", None),
    ("/refused", "CONNECTION", None),
    ("/limited", "RATE_LIMIT", 429),
    ("/missing", "CLIENT_ERROR", 404),
    ("/broken", "SERVER_ERROR", 503),
])
def test_every_kind_of_failure_is_classified(owner, path, error_type, status_code):
    with client(user_id=owner) as http:
        try:
            http.get(f"https://api.apify.com/v2{path}")
        except httpx.HTTPError:
            pass
    row = only()
    assert row.status == "FAILED" and row.error_type == error_type and row.http_status_code == status_code
    assert row.error_message


def test_an_sdk_exception_is_recorded_and_still_raised(owner):
    """A vendor SDK that owns its transport is wrapped at its boundary; the exception reaches the caller intact."""
    with pytest.raises(RuntimeError, match="the SDK exploded"):
        with external_api.external_operation("OPENAI", service="llm", operation="chat_completion",
                                             user_id=owner) as call:
            call.note(model="gpt-test")
            raise RuntimeError("the SDK exploded")
    row = only()
    assert row.provider == "OPENAI" and row.status == "FAILED" and row.error_type == "SDK_ERROR"
    assert row.request_metadata == {"model": "gpt-test"} and "the SDK exploded" in row.error_message


def test_a_rate_limit_records_what_the_provider_said_about_it(owner):
    with client(user_id=owner) as http:
        http.get("https://api.apify.com/v2/limited")
    row = only()
    assert row.rate_limit_remaining == 0
    assert row.rate_limit_reset_at is not None and row.rate_limit_reset_at > utcnow()


def test_an_epoch_reset_forwarded_by_litellm_is_recorded_as_that_moment(owner):
    """It used to be read as seconds from now, which overflowed and lost the row and the cooldown together."""
    with client(user_id=owner) as http:
        http.get("https://api.apify.com/v2/limited-forwarded")
    row = only()
    assert row.http_status_code == 429
    assert row.rate_limit_reset_at == datetime.fromtimestamp(OPENROUTER_RESET_MS / 1000, UTC).replace(tzinfo=None)
    with SessionLocal() as db:
        assert db.scalar(select(ProviderCooldown)) is not None


@pytest.mark.parametrize("header", ["x-ratelimit-reset", "llm_provider-x-ratelimit-reset"])
@pytest.mark.parametrize("value", ["1e300", "inf", "-inf", "nan", "soon"])
def test_a_reset_that_is_not_a_date_is_unknown_and_never_raises(header, value):
    at = utcnow()
    reset = external_api._reset_at(httpx.Headers({header: value}), at, status=429)
    assert reset is None or reset == at


@pytest.mark.parametrize(("value", "seconds"), [("45", 45), ("2m59.56s", 179.56), ("120ms", 0.12)])
def test_a_forwarded_duration_still_counts_from_now(value, seconds):
    at = utcnow()
    assert external_api._reset_at(httpx.Headers({"llm_provider-x-ratelimit-reset": value}), at, status=429) == \
        at + timedelta(seconds=seconds)


def test_a_failure_also_becomes_a_log_line(owner):
    with client(user_id=owner) as http:
        http.get("https://api.apify.com/v2/broken")
    with SessionLocal() as db:
        entry = db.scalar(select(SystemLogEntry))
    row = only()
    assert entry.level == "ERROR" and entry.category == "APIFY" and entry.status == "SERVER_ERROR"
    assert entry.external_request_id == row.request_id and "SERVER_ERROR" in entry.message


# ───────────── retries ─────────────

def test_retries_are_attempts_of_one_operation_not_separate_work(owner):
    """Three attempts at the same logical call: each is its own row, each knowing which attempt it was."""
    with client(user_id=owner) as http:
        for attempt in (1, 2, 3):
            http.get("https://api.apify.com/v2/datasets/abc/items", extensions={"attempt": attempt})
    rows = logged()
    assert [r.attempt for r in rows] == [1, 2, 3]
    assert [r.retry_count for r in rows] == [0, 1, 2], "the first attempt is not a retry"


# ───────────── secrets ─────────────

def test_credentials_never_reach_the_database(owner):
    with external_api.external_client("APIFY", service="apify_api", operation="dataset_fetch",
                                      transport=httpx.MockTransport(responder), user_id=owner,
                                      headers={"Authorization": f"Bearer {SECRET_TOKEN}",
                                               "X-Api-Key": "sk-ant-secretkey12345",
                                               "Cookie": "session=abc123"}) as http:
        http.get(f"https://api.apify.com/v2/datasets/abc/items?token={SECRET_TOKEN}&clean=true")
    row = only()
    stored = " ".join(str(v) for v in (row.endpoint_path, row.error_message, row.request_metadata))
    for secret in (SECRET_TOKEN, "sk-ant-secretkey12345", "session=abc123", "Authorization", "Bearer"):
        assert secret not in stored
    with SessionLocal() as db:
        dump = str([dict(r._mapping) for r in db.execute(select(ExternalAPIRequestLog.__table__)).all()])
    assert SECRET_TOKEN not in dump and "sk-ant-secretkey12345" not in dump


def test_a_secret_inside_an_error_message_is_redacted(owner):
    def leaky(request):
        raise httpx.ConnectError(f"could not authenticate with {SECRET_TOKEN}")

    with external_api.external_client("APIFY", service="apify_api", operation="actor_run_status",
                                      transport=httpx.MockTransport(leaky), user_id=owner) as http:
        with pytest.raises(httpx.ConnectError):
            http.get("https://api.apify.com/v2/actor-runs/1")
    row = only()
    assert SECRET_TOKEN not in row.error_message and "[redacted]" in row.error_message


def test_bodies_are_not_stored_by_default_and_are_capped_when_they_are(owner, monkeypatch):
    with client(user_id=owner) as http:
        http.post("https://api.apify.com/v2/actors/x~y/runs", json={"resume": "PRIVATE CANDIDATE TEXT " * 40})
    row = only()
    assert row.request_metadata is None, "payloads are off by default"
    assert row.request_size_bytes > 0, "the size is recorded instead of the body"

    settings = get_settings()
    monkeypatch.setattr(settings, "external_api_debug_payloads", True)
    monkeypatch.setattr(settings, "external_api_payload_limit", 120)
    with client(user_id=owner) as http:
        http.post("https://api.apify.com/v2/actors/x~y/runs",
                  json={"resume": "PRIVATE CANDIDATE TEXT " * 40, "token": SECRET_TOKEN})
    debug = logged()[-1]
    assert len(debug.request_metadata["request_payload"]) <= 120, "extracts are capped"
    assert SECRET_TOKEN not in debug.request_metadata["request_payload"], "and still redacted"
    assert debug.request_metadata["request_headers"]["host"]


# ───────────── traces and workflow ─────────────

def test_a_trace_ties_a_workflow_together(owner):
    with tracing.trace("hunter", workflow_type="system", user_id=owner) as outer:
        with client(user_id=owner) as http:
            http.get("https://api.apify.com/v2/datasets/abc/items")
        with tracing.trace("ranking", job_id=77, workflow_type="job", workflow_entity_id=77):
            with external_api.external_operation("OPENROUTER", service="llm", operation="chat_completion",
                                                 user_id=owner):
                pass
    rows = logged()
    assert {r.trace_id for r in rows} == {outer.trace_id}, "one correlation ID across both providers"
    apify, llm = rows
    assert apify.workflow == "hunter" and llm.workflow == "ranking"
    assert llm.job_id == 77 and llm.workflow_type == "job" and llm.workflow_entity_id == 77
    assert apify.job_id is None


def test_calls_made_without_a_trace_are_still_recorded(owner):
    with client(user_id=owner) as http:
        http.get("https://api.apify.com/v2/datasets/abc/items")
    assert only().trace_id is None


# ───────────── cost ─────────────

def test_cost_is_null_unless_the_provider_reported_one(owner):
    with client(user_id=owner) as http:
        http.get("https://api.apify.com/v2/datasets/abc/items")
    row = only()
    assert row.estimated_cost is None and row.cost_source == "unknown" and row.cost_currency is None

    with external_api.external_operation("OPENROUTER", service="llm", operation="chat_completion",
                                         user_id=owner) as call:
        call.record_usage(cost=0.0123, source="provider_reported")
    priced = logged()[-1]
    assert float(priced.estimated_cost) == pytest.approx(0.0123)
    assert priced.cost_source == "provider_reported" and priced.cost_currency == "USD"

    with external_api.external_operation("OPENROUTER", service="llm", operation="chat_completion") as call:
        call.record_usage(cost=None)
    assert logged()[-1].estimated_cost is None, "an unknown cost is never turned into zero"


# ───────────── telemetry is best effort ─────────────

def test_a_broken_telemetry_store_does_not_break_the_api_call(owner, monkeypatch):
    def explode(entry):
        raise RuntimeError("the telemetry database is gone")

    monkeypatch.setattr(external_api, "persist", explode)
    with client(user_id=owner) as http:
        response = http.get("https://api.apify.com/v2/datasets/abc/items")
    assert response.status_code == 200 and response.json()["data"]["status"] == "SUCCEEDED"
    assert logged() == []


def test_a_broken_log_store_does_not_break_the_api_call(owner, monkeypatch):
    monkeypatch.setattr(system_log, "_session_factory", lambda: (_ for _ in ()).throw(RuntimeError("no database")))
    with client(user_id=owner) as http:
        response = http.get("https://api.apify.com/v2/broken")
    assert response.status_code == 503, "the caller still sees exactly what the provider said"
    assert only().status == "FAILED"


def test_telemetry_can_be_turned_off(owner, monkeypatch):
    monkeypatch.setattr(get_settings(), "external_api_telemetry_enabled", False)
    with client(user_id=owner) as http:
        assert http.get("https://api.apify.com/v2/datasets/abc/items").status_code == 200
    assert logged() == []


# ───────────── the decorator, for SDKs with nothing finer to wrap ─────────────

def test_the_decorator_instruments_a_whole_integration_method(owner):
    @external_api.track_external_call("SEARCH", service="serp", operation="web_search")
    def search(query, call=None):
        call.note(query_length=len(query)).record_usage(cost=0.005)
        return ["result"]

    assert search("engineering jobs") == ["result"]
    row = only()
    assert (row.provider, row.service, row.operation) == ("SEARCH", "serp", "web_search")
    assert row.request_metadata == {"query_length": 16} and float(row.estimated_cost) == pytest.approx(0.005)


def test_the_decorator_records_a_failure_and_re_raises():
    @external_api.track_external_call("EMAIL", service="smtp")
    def send():
        raise ValueError("mailbox unavailable")

    with pytest.raises(ValueError):
        send()
    row = only()
    assert row.operation == "send" and row.status == "FAILED" and row.error_type == "INVALID_RESPONSE"


# ───────────── captured request and response bodies ─────────────

@pytest.fixture
def capture_on(owner):
    from app.models import AppSetting
    from app.services import llm_telemetry
    from app.services.user_settings import settings_for

    def switch(enabled: bool):
        with SessionLocal() as db:
            row = settings_for(db, owner)
            prefs = dict(row.preferences or {})
            prefs["monitoring"] = {**(prefs.get("monitoring") or {}), llm_telemetry.CAPTURE_PREFERENCE: enabled}
            db.query(AppSetting).filter(AppSetting.id == row.id).update({"preferences": prefs})
            db.commit()
        llm_telemetry.forget_capture_setting(owner)
    switch(True)
    yield switch
    switch(False)


def test_an_account_that_turns_on_capture_keeps_redacted_request_and_response_bodies(owner, capture_on):
    with client(user_id=owner, headers={"Authorization": f"Bearer {SECRET_TOKEN}"}) as http:
        http.post("https://api.apify.com/v2/actors/x~y/runs",
                  json={"query": "staff engineer", "api_key": "sk-live-" + "x" * 30, "nested": {"token": "abc"}})
    row = only()
    payload = row.payload
    request, response = payload["request"], payload["response"]
    assert request["method"] == "POST" and request["url"].startswith("https://api.apify.com/v2/actors")
    assert '"query": "staff engineer"' in request["body"]
    assert "sk-live" not in request["body"] and '"token": "abc"' not in request["body"]  # credential fields replaced
    assert SECRET_TOKEN not in str(request["headers"])  # the Authorization header keeps only its name
    assert response["status"] == 200 and '"run-1"' in response["body"]
    assert row.request_metadata is None  # the short debug extracts stay off: this is the account's own capture


def test_capture_is_capped_off_by_default_and_cleared_after_its_retention(owner, capture_on, monkeypatch):
    from app.services import external_monitoring

    monkeypatch.setattr(get_settings(), "external_api_capture_limit", 300)
    with client(user_id=owner) as http:
        http.post("https://api.apify.com/v2/actors/x~y/runs", content=b"client_secret=GOCSPX-abc&q=" + b"y" * 2000,
                  headers={"content-type": "application/x-www-form-urlencoded"})
    body = only().payload["request"]["body"]
    assert len(body) <= 300 and "GOCSPX-abc" not in body and "q=" in body
    capture_on(False)
    with client(user_id=owner) as http:
        http.post("https://api.apify.com/v2/actors/x~y/runs", json={"query": "q"})
    assert logged()[-1].payload is None  # off: nothing kept
    with SessionLocal() as db:
        cleared = external_monitoring.clear_captured(db, now=utcnow() + __import__("datetime").timedelta(days=8))
    assert cleared == 1 and all(r.payload is None for r in logged())
    assert len(logged()) == 2  # the rows and their numbers stay


def test_a_log_line_shows_the_captured_request_and_response(owner, capture_on):
    from app.services import external_monitoring

    with client(user_id=owner) as http:
        http.post("https://api.apify.com/v2/actors/missing", json={"query": "q"})
    with SessionLocal() as db:
        line = db.scalars(select(SystemLogEntry).where(SystemLogEntry.user_id == owner)
                          .order_by(SystemLogEntry.id.desc())).first()
        detail = external_monitoring.log_detail(db, line.id, owner)
    shown = detail["external_request"]["payload"]
    assert '"query": "q"' in shown["request"]["body"]
    assert shown["response"]["status"] == 404 and "no such actor" in shown["response"]["body"]


def test_captured_urls_lose_their_credential_query_values(owner, capture_on):
    with client(user_id=owner) as http:
        http.get("https://api.apify.com/v2/datasets/d/items", params={"q": "x", "code": "4/0AbCdEf", "api_key": "short"})
    url = only().payload["request"]["url"]
    assert "4/0AbCdEf" not in url and "short" not in url and "q=x" in url


def test_llm_prompt_debug_alone_keeps_no_external_bodies(owner, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_prompt_debug", True)  # LLM text only: not third-party bodies
    with client(user_id=owner) as http:
        http.post("https://api.apify.com/v2/actors/x~y/runs", json={"query": "q"})
    assert only().payload is None
