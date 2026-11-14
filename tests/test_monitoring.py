"""LLM request telemetry, cost estimation and the Monitoring API.

Telemetry is captured once in the shared LLM layer, so these tests drive LLMClient with fake provider SDKs and read
back the llm_request_logs rows it wrote. They run on the default test database (SQLite or TEST_DATABASE_URL).
"""
from dataclasses import replace
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import google
from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import Job, LLMRequestLog, LLMUsageDaily
from app.prompts import build_ranking_prompt, build_review_prompt
from app.prompts.ranking import RANKING_PROMPT_VERSION
from app.services import llm_telemetry, model_catalog, monitoring
from app.services.llm import LLMCall, LLMClient, LLMConfigError, LLMContextOverflowError, LLMRateLimitError, \
    LLMResponseError, LLMTimeoutError
from app.settings import get_settings
from job_hunt_agent import telemetry as adk_telemetry
from tests.conftest import make_user

RESUME = "Led platform teams at Acme, cut cloud cost by 40% and shipped Kubernetes migrations on AWS."
profile = SimpleNamespace(name="Asha", headline="Engineering Manager", target_roles=["EM"], preferred_locations=["SG"],
                          work_authorization="PR", search_preferences={}, location="Singapore")
evidence = [{"id": 1, "claim": "Cut cloud cost by 40% at Acme", "company": "Acme", "skills": ["AWS"]}]


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


def job(job_id=7, description="Lead Python platform teams on Kubernetes and AWS. " * 20):
    return SimpleNamespace(id=job_id, title="Engineering Manager", company="Globex", location="Singapore",
                           description=description)


def ranking_prompt(job_id=7):
    return build_ranking_prompt(candidate_profile=profile, resume=RESUME, evidence=evidence, job=job(job_id))


def logs_for(user_id):
    with SessionLocal() as db:
        return db.scalars(select(LLMRequestLog).where(LLMRequestLog.user_id == user_id)
                          .order_by(LLMRequestLog.id)).all()


@pytest.fixture
def user_id():
    return make_user(SessionLocal)


def client_for(user_id, provider, keys=None, **overrides):
    return LLMClient(overrides={"llm_provider": provider, **overrides}, keys=keys or {}, user_id=user_id)


class FakeOpenAI:
    """Stands in for the OpenAI SDK (also used for OpenRouter)."""
    reply = {}
    captured = {}

    def __init__(self, **kwargs):
        FakeOpenAI.captured["client"] = kwargs
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        FakeOpenAI.captured["request"] = kwargs
        if isinstance(FakeOpenAI.reply.get("raise"), Exception):
            raise FakeOpenAI.reply["raise"]
        usage = FakeOpenAI.reply.get("usage")
        return SimpleNamespace(model=FakeOpenAI.reply.get("model"), usage=usage,
                               choices=[SimpleNamespace(message=SimpleNamespace(content=FakeOpenAI.reply.get("content", '{"ok": true}')))])


@pytest.fixture
def fake_openai(monkeypatch):
    FakeOpenAI.reply, FakeOpenAI.captured = {}, {}
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    return FakeOpenAI


def openai_usage(prompt, completion, cached=None, cost=None):
    extra = {"cost": cost} if cost is not None else {}
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, model_extra=extra,
                           prompt_tokens_details=SimpleNamespace(cached_tokens=cached))


# ───────────── tokens, latency, provider and model ─────────────

def test_openai_usage_latency_model_and_cost_are_recorded(user_id, fake_openai):
    fake_openai.reply = {"model": "gpt-5.6-luna-2026-09-01", "usage": openai_usage(1200, 300, cached=200)}
    # Usage recording is the subject here, not the ranking contract: the fake answers {"ok": true}.
    result = client_for(user_id, "openai", {"openai": "sk-test"}, model="gpt-5.6-luna").generate_prompt(
        replace(ranking_prompt(), output_schema=None), LLMCall(workflow_type="job", entity_id=7))
    assert result.data == {"ok": True}
    log, = logs_for(user_id)
    assert (log.provider, log.requested_model, log.model) == ("openai", "gpt-5.6-luna", "gpt-5.6-luna-2026-09-01")
    assert (log.input_tokens, log.output_tokens, log.total_tokens, log.cache_read_tokens) == (1200, 300, 1500, 200)
    assert log.usage_source == "provider_reported" and log.cached is True
    assert log.latency_ms is not None and log.latency_ms >= 0 and log.completed_at >= log.created_at
    assert log.status == "SUCCEEDED" and log.stage == "RANKER" and log.prompt_version == RANKING_PROMPT_VERSION
    # The dated snapshot has no catalog entry, so it is priced as the requested model.
    assert (log.estimated_cost, log.cost_source) == (Decimal("0.00056400"), "pricing_table")
    assert (log.workflow_type, log.workflow_entity_id) == ("job", 7)


def test_cost_uses_the_catalog_with_cached_and_long_context_rates(user_id, fake_openai):
    fake_openai.reply = {"model": "gpt-5.6-luna", "usage": openai_usage(1200, 300, cached=200)}
    client_for(user_id, "openai", {"openai": "sk-test"}, model="gpt-5.6-luna").generate_json("s", "FIT_ANALYSIS\n")
    fake_openai.reply = {"model": "gpt-5.6-luna", "usage": openai_usage(300_000, 1000)}
    client_for(user_id, "openai", {"openai": "sk-test"}, model="gpt-5.6-luna").generate_json("s", "FIT_ANALYSIS\n")
    short, long = logs_for(user_id)
    # 1,000 uncached × $0.20 + 200 cached × $0.02 + 300 output × $1.20, per million tokens.
    assert short.estimated_cost == Decimal("0.00056400") and short.cost_source == "pricing_table"
    assert short.pricing_effective_from.isoformat() == "2026-09-17"
    # Over 272K input tokens the whole request uses the long-context rates: 300,000 × $0.40 + 1,000 × $1.80.
    assert long.estimated_cost == Decimal("0.12180000")
    assert long.context_window_size == 1_050_000 and long.context_utilization_percent == pytest.approx(28.57, abs=0.01)


def test_free_models_cost_zero_and_unknown_models_cost_null(user_id, fake_openai, monkeypatch):
    fake_openai.reply = {"model": "openrouter/free", "usage": openai_usage(500, 50, cost=0.02)}
    client_for(user_id, "openrouter", {"openrouter": "k"}, model="openrouter/free").generate_json("s", "FOLLOW_UP\n")
    fake_openai.reply = {"model": "gpt-9-unreleased", "usage": openai_usage(500, 50)}
    client_for(user_id, "openai", {"openai": "k"}, model="gpt-9-unreleased").generate_json("s", "FOLLOW_UP\n")
    free, unknown = logs_for(user_id)
    assert (free.estimated_cost, free.cost_source) == (Decimal(0), "free")
    assert (unknown.estimated_cost, unknown.cost_source) == (None, "unknown")
    assert unknown.context_window_size is None and unknown.context_utilization_percent is None


def test_openrouter_reports_its_own_cost_and_usage_accounting_is_requested(user_id, fake_openai):
    fake_openai.reply = {"model": "anthropic/claude-sonnet-5", "usage": openai_usage(2000, 100, cost=0.0071)}
    client_for(user_id, "openrouter", {"openrouter": "k"}, model="anthropic/claude-sonnet-5",
               require_free=False).generate_json("s", "REVIEW_RESUME\n")
    log, = logs_for(user_id)
    assert (log.estimated_cost, log.cost_source) == (Decimal("0.00710000"), "provider_reported")
    assert fake_openai.captured["request"]["extra_body"]["usage"] == {"include": True}
    assert log.context_window_size == 1_000_000


def test_anthropic_cache_tokens_and_cost(user_id, monkeypatch):
    class FakeAnthropic:
        def __init__(self, **kwargs):
            self.messages = SimpleNamespace(create=lambda **_: SimpleNamespace(
                model="claude-sonnet-5", content=[SimpleNamespace(text='{"score": 90}')],
                usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=5000,
                                      cache_creation_input_tokens=0)))

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=FakeAnthropic))
    client_for(user_id, "anthropic", {"anthropic": "k"}, model="claude-sonnet-5").generate_json("s", "REVIEW_RESUME\n")
    log, = logs_for(user_id)
    assert (log.input_tokens, log.output_tokens, log.cache_read_tokens) == (6000, 200, 5000)
    # 1,000 × $2 + 200 × $10 + 5,000 cached × $0.20, per million tokens.
    assert log.estimated_cost == Decimal("0.00500000") and log.stage == "OTHER"


def test_gemini_usage_metadata_is_recorded(user_id, monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs):
            self.models = SimpleNamespace(generate_content=lambda **_: SimpleNamespace(
                text='{"draft": "hi"}', model_version="gemini-2.5-flash",
                usage_metadata=SimpleNamespace(prompt_token_count=800, candidates_token_count=100,
                                               cached_content_token_count=None)))

    monkeypatch.setattr(google, "genai", SimpleNamespace(Client=FakeClient), raising=False)
    result = client_for(user_id, "gemini", {"gemini": "k"}, model="gemini-2.5-flash").generate_json("s", "FOLLOW_UP\n")
    assert result == {"draft": "hi"}
    log, = logs_for(user_id)
    assert (log.provider, log.model, log.input_tokens, log.output_tokens) == ("gemini", "gemini-2.5-flash", 800, 100)
    assert log.estimated_cost == Decimal("0.00049000") and log.context_window_size == 1_048_576


def test_the_mock_provider_is_estimated_and_free(user_id):
    client_for(user_id, "mock").generate_prompt(ranking_prompt())
    log, = logs_for(user_id)
    assert log.provider == "mock" and log.usage_source == "estimated"
    assert log.input_tokens == llm_telemetry.estimate_tokens(ranking_prompt().system) + \
        llm_telemetry.estimate_tokens(ranking_prompt().user)
    assert log.output_tokens > 0 and (log.estimated_cost, log.cost_source) == (Decimal(0), "free")


def test_the_catalog_uses_the_entry_in_effect(tmp_path, monkeypatch):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"models": [
        {"provider": "openai", "model": "demo", "input_cost_per_million": 1, "output_cost_per_million": 2,
         "effective_from": "2026-01-01"},
        {"provider": "openai", "model": "demo", "input_cost_per_million": 10, "output_cost_per_million": 20,
         "effective_from": "2026-06-01"}]}))
    monkeypatch.setattr(get_settings(), "model_catalog_file", str(path))
    model_catalog.reload()
    try:
        old = model_catalog.estimate_cost("openai", "demo", input_tokens=1_000_000, output_tokens=0, at=datetime(2026, 3, 1))
        new = model_catalog.estimate_cost("openai", "demo", input_tokens=1_000_000, output_tokens=0, at=datetime(2026, 7, 1))
        before = model_catalog.estimate_cost("openai", "demo", input_tokens=1, output_tokens=1, at=datetime(2025, 1, 1))
        assert (old.cost, new.cost, before.cost) == (Decimal(1), Decimal(10), None)
        missing = model_catalog.estimate_cost("openai", "demo", input_tokens=None, output_tokens=5)
        assert missing.cost is None and missing.source == "unknown"  # no tokens, no guess
    finally:
        model_catalog.reload()


def test_catalog_file_is_the_only_source_of_prices():
    data = json.loads(model_catalog.DEFAULT_CATALOG.read_text())
    assert all(entry.get("source_url") and entry.get("effective_from") for entry in data["models"])
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "app"
    # The model market (and the MCP tools that expose it) reports catalogs' list prices for suggestions; it never
    # computes a recorded cost, which still comes only from model_catalog.
    reporting = {root / "services" / "model_market.py", root / "mcp" / "tools.py", root / "mcp" / "server.py"}
    offenders = [p.name for p in root.rglob("*.py") if "per_million" in p.read_text() and p.name != "model_catalog.py"
                 and p not in reporting]
    assert offenders == []
    from app.services import llm_telemetry
    assert "model_market" not in Path(llm_telemetry.__file__).read_text()  # recorded cost never reads the market


# ───────────── context size and attribution ─────────────

def test_context_size_and_contributors_are_estimated_from_prompt_sections(user_id):
    prompt = ranking_prompt()
    client_for(user_id, "mock").generate_prompt(prompt)
    log, = logs_for(user_id)
    breakdown = log.context_breakdown
    assert set(breakdown) == set(llm_telemetry.CONTRIBUTORS)
    assert breakdown["system_prompt"] == llm_telemetry.estimate_tokens(prompt.system)
    assert breakdown["resume"] > 0 and breakdown["evidence"] > 0 and breakdown["candidate_profile"] > 0
    assert breakdown["job_description"] > breakdown["resume"]  # the long posting dominates this prompt
    assert log.estimated_context_tokens == log.input_tokens
    assert log.context_window_size is None and log.context_utilization_percent is None  # mock has no window


@pytest.mark.parametrize("prompt_name,stage", [("ranking", "RANKER"), ("tailoring", "TAILOR"), ("review", "REVIEWER"),
                                               ("resume_parser", "RESUME_PARSER"), ("evidence_extraction", "RESUME_PARSER"),
                                               ("application_question", "APPLICATION"), ("followup", "FOLLOW_UP"),
                                               ("inbox", "INBOX"), ("ai_optimization", "ADVISOR"),
                                               ("job_description_parser", "JD_PARSER"),
                                               ("job_description_recovery", "JD_RECOVERY"), ("anything", "OTHER")])
def test_every_prompt_is_attributed_to_its_stage(prompt_name, stage):
    assert llm_telemetry.STAGE_BY_PROMPT.get(prompt_name, "OTHER") == stage


def test_workflow_entity_and_retries_are_attributed(user_id):
    review = build_review_prompt(candidate_profile=profile, evidence=evidence, job=job(), tailored_resume="# Asha")
    client_for(user_id, "mock").generate_prompt(review, LLMCall(workflow_type="job", entity_id=42, attempt=3))
    log, = logs_for(user_id)
    assert (log.stage, log.workflow_type, log.workflow_entity_id, log.retry_count) == ("REVIEWER", "job", 42, 2)


# ───────────── failures and structured output ─────────────

def test_structured_output_failures_are_recorded_and_the_error_reaches_the_caller(user_id):
    class BadRanking(ValueError):
        pass

    def reject(data):
        raise BadRanking("missing overall_fit_score")

    call = LLMCall(workflow_type="job", entity_id=9, attempt=1, validate=reject)
    for attempt in (1, 2):
        call.attempt = attempt
        with pytest.raises(BadRanking):
            client_for(user_id, "mock").generate_prompt(ranking_prompt(9), call)
    accepted = client_for(user_id, "mock").generate_prompt(ranking_prompt(9), LLMCall(
        workflow_type="job", entity_id=9, attempt=3, validate=lambda data: data["overall_fit_score"]))
    assert accepted.validated == 80
    first, second, third = logs_for(user_id)
    assert (first.status, first.error_type, first.structured_output_valid) == ("FAILED", "SCHEMA_VALIDATION", False)
    assert (first.structured_output_retry_count, second.structured_output_retry_count) == (0, 1)
    assert (third.status, third.structured_output_valid, third.structured_output_retry_count) == ("SUCCEEDED", True, 2)
    assert "missing overall_fit_score" in first.error_message


@pytest.mark.parametrize("raised,expected_error,error_type", [
    (type("APITimeoutError", (Exception,), {})("timed out"), LLMTimeoutError, "TIMEOUT"),
    (type("RateLimitError", (Exception,), {"status_code": 429})("slow down"), LLMRateLimitError, "RATE_LIMIT"),
    (Exception("This model's maximum context length is 128000 tokens"), LLMContextOverflowError, "CONTEXT_OVERFLOW"),
])
def test_provider_failures_are_classified(user_id, fake_openai, raised, expected_error, error_type):
    fake_openai.reply = {"raise": raised}
    with pytest.raises(expected_error):
        client_for(user_id, "openai", {"openai": "k"}, model="gpt-5.6-luna").generate_json("s", "TAILOR_RESUME\n")
    log, = logs_for(user_id)
    assert (log.status, log.error_type, log.input_tokens) == ("FAILED", error_type, None)
    assert log.usage_source == "none" and log.estimated_context_tokens > 0  # context is known even without usage


def test_invalid_json_and_configuration_errors(user_id, fake_openai):
    fake_openai.reply = {"content": "I cannot answer in JSON", "usage": openai_usage(100, 20)}
    with pytest.raises(LLMResponseError):
        client_for(user_id, "openai", {"openai": "k"}, model="gpt-5.6-luna").generate_json("s", "FIT_ANALYSIS\n")
    with pytest.raises(LLMConfigError):
        client_for(user_id, "openrouter", {}, model="openrouter/free").generate_json("s", "FIT_ANALYSIS\n")
    invalid, config = logs_for(user_id)
    assert (invalid.error_type, invalid.structured_output_valid, invalid.input_tokens) == ("INVALID_JSON", False, 100)
    assert (config.error_type, config.cost_source, config.estimated_cost) == ("CONFIGURATION", "not_sent", Decimal(0))


def test_secrets_are_redacted_and_prompts_are_not_stored_unless_debugging(user_id, fake_openai, monkeypatch):
    fake_openai.reply = {"raise": Exception("401 invalid key sk-or-v1-abcdef1234567890abcdef Authorization: Bearer abc.def.ghi123")}
    with pytest.raises(Exception):
        client_for(user_id, "openai", {"openai": "k"}, model="gpt-5.6-luna").generate_prompt(ranking_prompt())
    failed, = logs_for(user_id)
    assert "sk-or-v1-abcdef" not in failed.error_message and "abc.def.ghi123" not in failed.error_message
    assert "[redacted]" in failed.error_message
    assert failed.debug_prompt is None and failed.debug_response is None

    monkeypatch.setattr(get_settings(), "llm_prompt_debug", True)
    client_for(user_id, "mock").generate_prompt(ranking_prompt())
    debugged = logs_for(user_id)[-1]
    assert RESUME in debugged.debug_prompt and "overall_fit_score" in debugged.debug_response


def test_telemetry_failure_never_breaks_the_llm_request(user_id, fake_openai, monkeypatch):
    def broken(entry):
        raise RuntimeError("database is down")

    monkeypatch.setattr(llm_telemetry, "persist", broken)
    assert client_for(user_id, "mock").generate_prompt(ranking_prompt()).data["overall_fit_score"] == 80
    fake_openai.reply = {"raise": type("APITimeoutError", (Exception,), {})("slow")}
    with pytest.raises(LLMTimeoutError):  # the request's own error, not the telemetry error
        client_for(user_id, "openai", {"openai": "k"}, model="gpt-5.6-luna").generate_json("s", "FIT_ANALYSIS\n")

    def failing_sessions():
        raise RuntimeError("no connection")

    monkeypatch.undo()
    llm_telemetry.use_sessions(failing_sessions)
    try:
        assert client_for(user_id, "mock").generate_json("s", "FOLLOW_UP\n")["draft"]
    finally:
        llm_telemetry.use_sessions(None)
    assert logs_for(user_id) == []


def test_telemetry_can_be_turned_off(user_id, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_telemetry_enabled", False)
    client_for(user_id, "mock").generate_prompt(ranking_prompt())
    assert logs_for(user_id) == []


def test_adk_agent_calls_are_recorded_through_the_same_pipeline(monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "false")
    ctx = SimpleNamespace(invocation_id=uuid.uuid4().hex, agent_name="job_fit_ranker")
    request = SimpleNamespace(model="gemini-2.5-flash", contents=[SimpleNamespace(parts=[SimpleNamespace(text="Rank job 5")])],
                              config=SimpleNamespace(system_instruction="You are a ranker."))
    response = SimpleNamespace(model_version="gemini-2.5-flash", content=SimpleNamespace(parts=[SimpleNamespace(text="done")]),
                               usage_metadata=SimpleNamespace(prompt_token_count=1000, candidates_token_count=40,
                                                              cached_content_token_count=None), error_code=None)
    adk_telemetry.before_model(ctx, request)
    adk_telemetry.after_model(ctx, response)
    with SessionLocal() as db:
        log = db.scalars(select(LLMRequestLog).where(LLMRequestLog.workflow_type == "agent")
                         .order_by(LLMRequestLog.id.desc())).first()
    assert (log.stage, log.provider, log.input_tokens, log.output_tokens) == ("RANKER", "gemini", 1000, 40)
    assert log.estimated_cost == Decimal("0.00040000") and log.prompt_version

    ctx2 = SimpleNamespace(invocation_id=uuid.uuid4().hex, agent_name="resume_reviewer")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    adk_telemetry.before_model(ctx2, request)
    adk_telemetry.on_model_error(ctx2, request, TimeoutError("deadline exceeded"))
    with SessionLocal() as db:
        failed = db.scalars(select(LLMRequestLog).where(LLMRequestLog.workflow_type == "agent")
                            .order_by(LLMRequestLog.id.desc())).first()
    assert (failed.stage, failed.provider, failed.status, failed.error_type) == ("REVIEWER", "vertex_ai", "FAILED", "TIMEOUT")
    assert failed.estimated_cost is None  # Vertex AI prices are not in the catalog: unknown, not guessed


# ───────────── retention ─────────────

def test_old_telemetry_is_rolled_up_into_daily_totals_then_deleted(user_id):
    old = utcnow() - timedelta(days=120)
    with SessionLocal() as db:
        for cost, status in ((Decimal("0.01"), "SUCCEEDED"), (None, "FAILED")):
            db.add(add_log_row(user_id, created_at=old, estimated_cost=cost, status=status,
                               error_type=None if status == "SUCCEEDED" else "TIMEOUT"))
        db.add(add_log_row(user_id, created_at=utcnow()))
        db.commit()
        out = monitoring.prune(db, retention_days=90)
        assert out["deleted"] >= 2
        daily = db.scalars(select(LLMUsageDaily).where(LLMUsageDaily.user_id == user_id)).all()
    assert len(logs_for(user_id)) == 1
    row, = daily
    assert (row.requests, row.failures, row.unpriced_requests, row.input_tokens) == (2, 1, 1, 2000)
    assert row.estimated_cost == Decimal("0.01")


# ───────────── API ─────────────

def add_log_row(user_id, **fields):
    values = dict(request_id=str(uuid.uuid4()), user_id=user_id, stage="RANKER", provider="openrouter",
                  requested_model="openrouter/free", model="openrouter/free", prompt_name="ranking", prompt_version="v2",
                  input_tokens=1000, output_tokens=200, total_tokens=1200, usage_source="provider_reported",
                  estimated_context_tokens=1000, context_window_size=None, context_utilization_percent=None,
                  context_breakdown={"system_prompt": 200, "evidence": 800}, latency_ms=1500,
                  estimated_cost=Decimal(0), cost_source="free", status="SUCCEEDED", retry_count=0,
                  structured_output_valid=True, structured_output_retry_count=0, cached=False,
                  created_at=utcnow() - timedelta(hours=1), completed_at=utcnow())
    values.update(fields)
    return LLMRequestLog(**values)


@pytest.fixture
def signed_in(monkeypatch):
    def sign_in(user_id, is_admin=False):
        monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, f"{user_id}@jobhunt.test", is_admin))
        return TestClient(app)
    return sign_in


def test_monitoring_api_summarises_filters_and_isolates_accounts(user_id, signed_in):
    other = make_user(SessionLocal)
    with SessionLocal() as db:
        job_row = Job(user_id=user_id, source="test", source_ref=uuid.uuid4().hex, title="Staff Engineer", company="Initech")
        db.add(job_row)
        db.flush()
        rows = [add_log_row(user_id, workflow_type="job", workflow_entity_id=job_row.id, latency_ms=1000),
                add_log_row(user_id, stage="TAILOR", provider="anthropic", model="claude-sonnet-5",
                            requested_model="claude-sonnet-5", estimated_cost=Decimal("0.05"), cost_source="pricing_table",
                            input_tokens=30000, output_tokens=2000, total_tokens=32000, estimated_context_tokens=30000,
                            context_window_size=1_000_000, context_utilization_percent=3.0, latency_ms=4000,
                            retry_count=1, debug_prompt="SECRET RESUME TEXT"),
                add_log_row(user_id, status="FAILED", error_type="SCHEMA_VALIDATION", structured_output_valid=False,
                            estimated_cost=None, cost_source="unknown", latency_ms=9000,
                            context_window_size=10_000, context_utilization_percent=95.0),
                add_log_row(user_id, created_at=utcnow() - timedelta(days=20)),  # outside 7 days
                add_log_row(other, stage="REVIEWER")]
        db.add_all(rows)
        db.commit()
        failed_id, tailor_id, first_id, other_id = rows[2].id, rows[1].id, rows[0].id, rows[4].id
        job_id = job_row.id
    client = signed_in(user_id)

    s = client.get("/api/monitoring/summary").json()
    assert (s["requests"], s["failed"], s["input_tokens"], s["output_tokens"]) == (3, 1, 32000, 2400)
    assert s["estimated_cost"] == 0.05 and s["unpriced_requests"] == 1
    assert s["retry_rate"] == pytest.approx(1 / 3, abs=1e-3) and s["p95_latency_ms"] == 9000
    assert s["structured_output_failure_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert client.get("/api/monitoring/summary", params={"range": "30d"}).json()["requests"] == 4

    assert client.get("/api/monitoring/summary", params={"stage": "TAILOR"}).json()["requests"] == 1
    assert client.get("/api/monitoring/summary", params={"status": "FAILED"}).json()["requests"] == 1
    assert client.get("/api/monitoring/summary", params={"provider": "anthropic"}).json()["estimated_cost"] == 0.05
    assert client.get("/api/monitoring/summary", params={"job_id": job_id}).json()["requests"] == 1

    dash = client.get("/api/monitoring/dashboard").json()
    stages = {x["stage"]: x for x in dash["stages"]}
    assert set(stages) >= {"RANKER", "TAILOR", "REVIEWER", "RESUME_PARSER", "APPLICATION", "FOLLOW_UP", "INBOX",
                           "COMPANY_INSIGHTS", "OTHER"}
    assert stages["RANKER"]["requests"] == 2 and stages["TAILOR"]["cost_share"] == 1.0
    assert {b["label"]: b["requests"] for b in dash["context"]["buckets"]}[">90%"] == 1
    assert dash["context"]["near_limit_requests"] == 1 and dash["context"]["unknown_window"] == 1
    assert {e["error_type"]: e["requests"] for e in dash["errors"]["by_type"]}["SCHEMA_VALIDATION"] == 1
    assert dash["latency"]["overall"]["p99_latency_ms"] == 9000 and len(dash["timeseries"]) >= 7
    assert "anthropic" in dash["options"]["providers"]

    models = client.get("/api/monitoring/models").json()["models"]
    assert models[0]["model"] == "claude-sonnet-5" and models[0]["estimated_cost"] == 0.05
    costs = client.get("/api/monitoring/costs").json()
    assert costs["total"] == 0.05 and sum(d["estimated_cost"] for d in costs["timeseries"]) == pytest.approx(0.05)
    assert client.get("/api/monitoring/context").json()["high_context_requests"] == 1
    assert client.get("/api/monitoring/errors").json()["failed"] == 1
    assert client.get("/api/monitoring/latency").json()["overall"]["avg_latency_ms"] == pytest.approx(4666.67, abs=0.01)

    page = client.get("/api/monitoring/requests", params={"per_page": 2}).json()
    assert page["total"] == 3 and len(page["items"]) == 2
    assert all("debug" not in item and "error_message" not in item for item in page["items"])

    detail = client.get(f"/api/monitoring/requests/{first_id}").json()
    assert detail["entity"] == {"type": "job", "id": job_id, "job_id": job_id, "title": "Staff Engineer", "company": "Initech"}
    assert "description" not in json.dumps(detail).lower()
    tailor = client.get(f"/api/monitoring/requests/{tailor_id}").json()
    assert tailor["debug"]["prompt"] == "SECRET RESUME TEXT"  # only because it was stored with debugging on
    assert client.get(f"/api/monitoring/requests/{failed_id}").json()["error_type"] == "SCHEMA_VALIDATION"
    assert client.get(f"/api/monitoring/requests/{other_id}").status_code == 404  # another account's request

    assert client.get("/api/monitoring/summary", params={"all_accounts": True}).status_code == 403
    admin = signed_in(user_id, is_admin=True)
    everyone = admin.get("/api/monitoring/summary", params={"all_accounts": True}).json()
    assert everyone["requests"] >= 4
    assert admin.get(f"/api/monitoring/requests/{other_id}", params={"all_accounts": True}).status_code == 200
    assert client.get("/api/monitoring/summary", params={"range": "year"}).status_code == 422


# ───────────── full prompt and response capture, and links to the logs ─────────────

def set_capture(user_id, enabled):
    from app.models import AppSetting

    with SessionLocal() as db:
        row = db.scalar(select(AppSetting).where(AppSetting.user_id == user_id))
        if row is None:
            row = AppSetting(user_id=user_id)
            db.add(row)
        prefs = dict(row.preferences or {})
        row.preferences = {**prefs, "monitoring": {llm_telemetry.CAPTURE_PREFERENCE: enabled}}
        db.commit()
    llm_telemetry.forget_capture_setting(user_id)


def test_an_account_can_capture_its_own_prompts_and_responses(user_id):
    other = make_user(SessionLocal)
    client_for(user_id, "mock").generate_prompt(ranking_prompt())
    assert logs_for(user_id)[-1].debug_prompt is None, "off by default"

    set_capture(user_id, True)
    client_for(user_id, "mock").generate_prompt(ranking_prompt())
    client_for(other, "mock").generate_prompt(ranking_prompt())
    captured = logs_for(user_id)[-1]
    assert RESUME in captured.debug_prompt and "overall_fit_score" in captured.debug_response
    assert logs_for(other)[-1].debug_prompt is None, "one account's switch never captures another's"

    set_capture(user_id, False)
    client_for(user_id, "mock").generate_prompt(ranking_prompt())
    assert logs_for(user_id)[-1].debug_prompt is None


def test_the_capture_switch_is_saved_per_account_through_the_api(user_id, monkeypatch):
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, f"{user_id}@jobhunt.test", False))
    api = TestClient(app)
    assert api.get("/api/monitoring/capture").json()["enabled"] is False
    out = api.put("/api/monitoring/capture", json={"enabled": True}).json()
    assert out["enabled"] is True and out["retention_days"] == get_settings().llm_prompt_debug_retention_days
    assert llm_telemetry.capture_payloads(user_id) is True, "the next call sees it at once"
    assert api.put("/api/monitoring/capture", json={"enabled": False}).json()["enabled"] is False
    assert llm_telemetry.capture_payloads(user_id) is False


def test_captured_text_is_cleared_after_its_own_retention_but_the_row_stays(user_id):
    with SessionLocal() as db:
        db.add(add_log_row(user_id, created_at=utcnow() - timedelta(days=10), debug_prompt="PROMPT", debug_response="OUT"))
        db.add(add_log_row(user_id, created_at=utcnow() - timedelta(days=1), debug_prompt="PROMPT", debug_response="OUT"))
        db.commit()
        out = monitoring.prune(db, retention_days=90)
    assert out["debug_text_cleared"] >= 1
    old, recent = logs_for(user_id)
    assert (old.debug_prompt, old.debug_response) == (None, None) and recent.debug_prompt == "PROMPT"


def test_a_request_links_to_its_logs_and_a_log_line_back_to_its_request(user_id):
    from app.models import SystemLogEntry
    from app.services import external_monitoring

    trace = uuid.uuid4().hex
    with SessionLocal() as db:
        request = add_log_row(user_id, trace_id=trace, debug_prompt="PROMPT")
        db.add(request)
        db.add_all([SystemLogEntry(user_id=user_id, trace_id=trace, message="Ranking failed", level="ERROR",
                                   category="WORKFLOW"),
                    SystemLogEntry(user_id=user_id, trace_id=trace, message="Retrying", category="WORKFLOW")])
        db.commit()
        detail = monitoring.request_detail(db, request.id, user_id)
        line = db.scalar(select(SystemLogEntry).where(SystemLogEntry.trace_id == trace).order_by(SystemLogEntry.id))
        back = external_monitoring.log_detail(db, line.id, user_id)
    assert detail["trace_log_count"] == 2
    assert [r["id"] for r in back["llm_requests"]] == [request.id] and back["llm_requests"][0]["captured"] is True


def log_lines(user_id, category=None):
    from app.models import SystemLogEntry

    with SessionLocal() as db:
        query = select(SystemLogEntry).where(SystemLogEntry.user_id == user_id)
        if category:
            query = query.where(SystemLogEntry.category == category)
        return db.scalars(query.order_by(SystemLogEntry.id)).all()


def test_every_llm_call_writes_a_log_line_that_opens_its_prompt_and_response(user_id):
    from app.services import external_monitoring

    set_capture(user_id, True)
    client_for(user_id, "mock").generate_prompt(ranking_prompt())
    line, = log_lines(user_id, "LLM")
    assert line.level == "INFO" and line.status == "SUCCEEDED" and "call succeeded" in line.message
    request = logs_for(user_id)[-1]
    assert line.details["llm_request_id"] == request.request_id and line.details["captured"] is True
    with SessionLocal() as db:
        detail = external_monitoring.log_detail(db, line.id, user_id)
    linked, = detail["llm_requests"]
    assert linked["id"] == request.id
    assert RESUME in linked["payload"]["prompt"] and "overall_fit_score" in linked["payload"]["response"]


def test_a_failed_llm_call_is_one_error_line_not_two(user_id, fake_openai):
    fake_openai.reply = {"raise": Exception("upstream exploded")}
    with pytest.raises(Exception):
        client_for(user_id, "openai", {"openai": "k"}, model="gpt-5.6-luna").generate_prompt(ranking_prompt())
    lines = log_lines(user_id)
    assert [(l.category, l.level) for l in lines] == [("LLM", "ERROR")], "the generic API failure line is not repeated"
    assert lines[0].details["captured"] is False


def test_success_lines_can_be_turned_off(user_id, monkeypatch):
    monkeypatch.setattr(get_settings(), "system_log_llm_calls", False)
    client_for(user_id, "mock").generate_prompt(ranking_prompt())
    assert log_lines(user_id, "LLM") == []


def test_jd_parsing_has_its_own_stage_and_monitoring_view(user_id, monkeypatch):
    """Regression: JD parser calls were filed under OTHER, and nothing in Monitoring showed parsing at all."""
    from app.models import JobDescriptionParseTask
    from app.prompts import build_job_description_parser_prompt

    posting = "Responsibilities:\n- Build Python services on AWS.\nRequirements:\n- 5+ years of Python."
    client_for(user_id, "mock").generate_prompt(build_job_description_parser_prompt(job(description=posting), posting),
                                                LLMCall(workflow_type="job", entity_id=7))
    log, = logs_for(user_id)
    assert log.stage == "JD_PARSER" and log.prompt_name == "job_description_parser"
    now = utcnow()
    with SessionLocal() as db:
        db.info["user_id"] = user_id
        parsed = Job(user_id=user_id, source="manual", source_ref=uuid.uuid4().hex, title="Engineer", company="Acme",
                     description=posting, structured_description_parsed_at=now)
        db.add(parsed)
        db.flush()
        db.add_all([
            JobDescriptionParseTask(user_id=user_id, job_id=parsed.id, description_hash="a", status="COMPLETED",
                                    started_at=now - timedelta(seconds=3), completed_at=now),
            JobDescriptionParseTask(user_id=user_id, job_id=parsed.id, description_hash="b", status="FAILED",
                                    last_error="LLMRateLimitError: rate limit exceeded", completed_at=now),
            JobDescriptionParseTask(user_id=user_id, job_id=parsed.id, description_hash="c", status="PENDING",
                                    attempt_count=1)])
        db.commit()
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, "x@jobhunt.test", False))
    x = TestClient(app).get("/api/monitoring/jd-parsing?range=7d").json()["metrics"]
    assert (x["tasks_created"], x["completed"], x["failed"], x["retrying"]) == (3, 1, 1, 1)
    assert x["success_rate"] == 0.5 and x["average_duration_ms"] == 3000 and x["jobs_parsed"] == 1
    assert x["queue"]["pending"] == 1 and x["top_errors"][0]["error"].startswith("LLMRateLimitError")
    assert x["llm"]["requests"] == 1 and x["llm"]["models"] == {"mock": 1}
    assert x["llm"]["estimated_cost"] is None or x["llm"]["estimated_cost"] == 0
    stages = {s["stage"] for s in TestClient(app).get("/api/monitoring/dashboard?range=7d").json()["stages"]}
    assert "JD_PARSER" in stages
