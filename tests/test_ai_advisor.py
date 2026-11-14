"""AI Optimization Advisor: asynchronous tasks, deterministic findings, validated recommendations, advice only."""
import ast
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import AIOptimizationReport, AIOptimizationTask, AppSetting, CandidateProfile, LLMRequestLog
from app.prompts import PROMPT_VERSIONS
from app.prompts.ai_optimization import AI_OPTIMIZATION_PROMPT_VERSION, build_ai_optimization_prompt
from app.services import ai_advisor, model_catalog, monitoring
from app.services import scheduled_jobs as sj
from app.services.llm import LLMResult
from app.settings import get_settings
from tests.conftest import make_user

ROOT = Path(__file__).resolve().parents[1]


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


def ctx():
    return sj.JobContext(sessions=SessionLocal, trigger="schedule", now=utcnow(), worker_id=f"test:{uuid.uuid4().hex[:6]}",
                         interval_minutes=5)


def log_row(user_id, **fields):
    values = dict(request_id=str(uuid.uuid4()), user_id=user_id, stage="RANKER", provider="openrouter",
                  requested_model="openrouter/free", model="openrouter/free", prompt_name="ranking", prompt_version="v2",
                  input_tokens=30000, output_tokens=600, total_tokens=30600, usage_source="provider_reported",
                  estimated_context_tokens=30000, context_breakdown={"system_prompt": 2000, "resume": 4600, "evidence": 23400},
                  latency_ms=2000, estimated_cost=Decimal(0), cost_source="free", status="SUCCEEDED", retry_count=0,
                  structured_output_valid=True, structured_output_retry_count=0, cached=False,
                  created_at=utcnow() - timedelta(hours=2), completed_at=utcnow(), workflow_type="job")
    values.update(fields)
    return LLMRequestLog(**values)


def seed_usage(user_id):
    """A week of usage with the patterns the advisor should notice."""
    with SessionLocal() as db:
        rows = [log_row(user_id, workflow_entity_id=i, retry_count=1 if i < 4 else 0,
                        status="FAILED" if i < 2 else "SUCCEEDED", error_type="SCHEMA_VALIDATION" if i < 2 else None,
                        structured_output_valid=False if i < 2 else True) for i in range(20)]
        rows += [log_row(user_id, stage="REVIEWER", prompt_name="review", provider="anthropic", model="claude-sonnet-5",
                         requested_model="claude-sonnet-5", estimated_cost=Decimal("0.08"), cost_source="pricing_table",
                         input_tokens=12000, output_tokens=800, context_breakdown={"resume": 6000, "evidence": 6000},
                         estimated_context_tokens=12000, workflow_entity_id=100 + i, latency_ms=45000) for i in range(8)]
        rows += [log_row(user_id, stage="FOLLOW_UP", prompt_name="followup", workflow_type="application",
                         workflow_entity_id=5, input_tokens=3000, output_tokens=150, estimated_context_tokens=3000,
                         context_breakdown={"system_prompt": 500, "conversation": 2500}) for _ in range(6)]
        db.add_all(rows)
        db.commit()


@pytest.fixture
def user_id():
    user = make_user(SessionLocal)
    with SessionLocal() as db:
        db.add(AppSetting(user_id=user, llm_provider="mock", model="mock", preferences={"ai": {"llm_provider": "mock"}}))
        db.add(CandidateProfile(user_id=user, master_resume="Engineering manager with platform experience."))
        db.commit()
    return user


@pytest.fixture
def signed_in(monkeypatch):
    def sign_in(user):
        monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user, f"{user}@jobhunt.test", False))
        return TestClient(app)
    return sign_in


def run_until_done(task_id, limit=20):
    """Run the scheduler's advisor job until this task has finished (other tests' tasks may be queued too)."""
    for _ in range(limit):
        sj.ai_optimization(ctx())
        with SessionLocal() as db:
            task = db.get(AIOptimizationTask, task_id)
            if task.status in ("COMPLETED", "FAILED"):
                return task
    raise AssertionError("task did not finish")


class FakeAdvisorModel:
    """Answers the advisor prompt; the real LLM layer runs the validator the same way."""

    def __init__(self, data):
        self.data, self.prompts = data, []

    def for_user(self, db, user_id=None):
        return self

    def generate_prompt(self, prompt, call=None):
        self.prompts.append(prompt)
        validated = call.validate(self.data) if call and call.validate else None
        return LLMResult(self.data, "openrouter", "vendor/advisor-model", None, validated)


GOOD_ANSWER = {
    "summary": "Ranker context is dominated by the evidence bank and the Reviewer drives spend.",
    "estimated_savings": "30-40% of monthly spend",
    "recommendations": [{
        "title": "Reduce Ranker evidence context", "category": "context", "priority": "high",
        "finding": "Ranker averages 30K input tokens per request.",
        "evidence": "Evidence is 78% of Ranker context across 20 requests.",
        "recommended_change": "Retrieve only evidence relevant to the JD before ranking.",
        "expected_benefit": "Lower latency and input-token cost.", "estimated_cost_saving": "35-45%",
        "risk": "Low if evidence retrieval recall is monitored.", "affected_stage": "RANKER"}],
}


# ───────────── queueing and processing ─────────────

def test_analyze_queues_one_asynchronous_task_and_the_scheduler_writes_a_report(user_id, signed_in, monkeypatch):
    seed_usage(user_id)
    fake = FakeAdvisorModel(GOOD_ANSWER)
    monkeypatch.setattr(ai_advisor, "LLMClient", fake)
    client = signed_in(user_id)

    queued = client.post("/api/monitoring/analyze", json={"range": "7d"})
    assert queued.status_code == 202 and queued.json()["task"]["status"] == "PENDING"
    assert client.post("/api/monitoring/analyze", json={"range": "7d"}).status_code == 409  # one at a time
    assert client.get("/api/monitoring/recommendations").json()["active"] is True
    assert fake.prompts == []  # nothing ran inside the request

    task = run_until_done(queued.json()["task"]["id"])
    assert task.status == "COMPLETED" and task.report_id is not None and task.attempt_count == 1
    out = client.get("/api/monitoring/recommendations").json()
    report = out["report"]
    assert out["active"] is False and "Nothing changes on its own" in out["notice"]
    assert report["generated_by"] == "llm" and report["prompt_version"] == AI_OPTIMIZATION_PROMPT_VERSION
    assert (report["model_provider"], report["model_name"]) == ("openrouter", "vendor/advisor-model")
    rec, = report["recommendations"]
    assert (rec["category"], rec["priority"], rec["affected_stage"]) == ("CONTEXT", "HIGH", "RANKER")
    assert set(rec) >= {"title", "finding", "evidence", "recommended_change", "expected_benefit",
                        "estimated_cost_saving", "risk"}
    assert report["estimated_savings"] == "30-40% of monthly spend"
    assert report["context_findings"] and report["cost_findings"] and report["reliability_findings"]
    detail = client.get(f"/api/monitoring/reports/{report['id']}").json()
    assert detail["aggregates"]["totals"]["requests"] == 34


def test_the_advisor_sends_aggregates_not_individual_requests(user_id, monkeypatch):
    seed_usage(user_id)
    with SessionLocal() as db:
        db.add_all([log_row(user_id, debug_prompt="PRIVATE RESUME TEXT " * 50, workflow_entity_id=900 + i)
                    for i in range(300)])
        db.commit()
    fake = FakeAdvisorModel(GOOD_ANSWER)
    monkeypatch.setattr(ai_advisor, "LLMClient", fake)
    with SessionLocal() as db:
        ai_advisor.queue_analysis(db, user_id, utcnow() - timedelta(days=7), utcnow())
    with SessionLocal() as db:
        task_id = db.scalar(select(AIOptimizationTask.id).where(AIOptimizationTask.user_id == user_id))
    run_until_done(task_id)
    prompt, = fake.prompts
    assert prompt.name == "ai_optimization" and "PRIVATE RESUME TEXT" not in prompt.user
    assert len(prompt.user) < 20000  # 334 requests summarised in a small, bounded context
    aggregates = json.loads(prompt.user.split("USAGE AGGREGATES:\n", 1)[1]
                            .split("\n\nEXTERNAL API AGGREGATES:", 1)[0])
    assert aggregates["totals"]["requests"] == 334 and len(aggregates["outliers"]["largest_context"]) <= 5
    assert "request_id" not in prompt.user
    # Third-party telemetry travels with it, aggregated the same way (app.services.external_monitoring).
    external = json.loads(prompt.user.split("EXTERNAL API AGGREGATES:\n", 1)[1].split("\n\nRULE FINDINGS:", 1)[0])
    assert set(external) >= {"totals", "providers", "apify"} and "endpoint_path" not in prompt.user


def test_rules_write_the_report_without_an_ai_key_or_when_the_answer_is_invalid(user_id, monkeypatch):
    seed_usage(user_id)
    with SessionLocal() as db:
        db.scalar(select(AppSetting).where(AppSetting.user_id == user_id)).preferences = {"ai": {"llm_provider": "openrouter"}}
        db.commit()
        report = ai_advisor.analyse(db, user_id, utcnow() - timedelta(days=7), utcnow())
    assert report.generated_by == "rules" and "no AI provider" in report.summary
    assert report.recommendations and all(r["recommended_change"] for r in report.recommendations)

    with SessionLocal() as db:
        db.scalar(select(AppSetting).where(AppSetting.user_id == user_id)).preferences = {"ai": {"llm_provider": "mock"}}
        db.commit()
    monkeypatch.setattr(ai_advisor, "LLMClient", FakeAdvisorModel({"summary": "", "recommendations": [{"title": "x"}]}))
    with SessionLocal() as db:
        fallback = ai_advisor.analyse(db, user_id, utcnow() - timedelta(days=7), utcnow())
    assert fallback.generated_by == "rules" and "AdvisorOutputError" in fallback.summary


def test_an_empty_period_still_completes(user_id):
    with SessionLocal() as db:
        report = ai_advisor.analyse(db, user_id, utcnow() - timedelta(days=7), utcnow())
    assert report.recommendations == [] and "No LLM requests" in report.summary


def test_scheduled_analyses_follow_each_accounts_choice(user_id, signed_in):
    client = signed_in(user_id)
    assert client.put("/api/monitoring/schedule", json={"schedule": "hourly"}).status_code == 422
    assert client.put("/api/monitoring/schedule", json={"schedule": "daily"}).json() == {"schedule": "daily"}
    with SessionLocal() as db:
        assert ai_advisor.enqueue_scheduled(db) >= 1
        task = db.scalar(select(AIOptimizationTask).where(AIOptimizationTask.user_id == user_id))
        assert task.trigger == "scheduled" and (task.time_range_end - task.time_range_start) == timedelta(days=1)
        ai_advisor.enqueue_scheduled(db)
        assert len(db.scalars(select(AIOptimizationTask).where(AIOptimizationTask.user_id == user_id)).all()) == 1
    assert client.get("/api/monitoring/recommendations").json()["schedule"] == "daily"


def test_a_crashed_analysis_is_recovered(user_id):
    with SessionLocal() as db:
        task = ai_advisor.queue_analysis(db, user_id, utcnow() - timedelta(days=1), utcnow())
        task_id = task.id
    claim = ai_advisor.claim_next(SessionLocal, "crashed-worker", now=utcnow() - timedelta(hours=1))
    assert claim is not None
    with SessionLocal() as db:
        assert ai_advisor.recover_expired(db) >= 1
        assert db.get(AIOptimizationTask, task_id).status == "PENDING"
    assert ai_advisor.process(SessionLocal, claim) == "lost"  # the old lease can no longer write
    assert run_until_done(task_id).status == "COMPLETED"


# ───────────── deterministic findings ─────────────

def test_findings_detect_context_cost_model_reliability_latency_and_duplicate_calls(user_id):
    seed_usage(user_id)
    with SessionLocal() as db:
        db.add_all([log_row(user_id, stage="REVIEWER", prompt_name="review", workflow_entity_id=200 + i,
                            context_breakdown={"resume": 6000, "evidence": 6000}) for i in range(10)])
        db.commit()
        aggregates = monitoring.advisor_aggregates(db, user_id, utcnow() - timedelta(days=7), utcnow())
    findings = ai_advisor.detect_findings(aggregates)
    by = {(f["category"], f["affected_stage"]) for f in findings}
    assert ("CONTEXT", "RANKER") in by  # evidence is 78% of ranker context
    ranker_context = next(f for f in findings if f["category"] == "CONTEXT" and f["affected_stage"] == "RANKER")
    assert ranker_context["priority"] == "HIGH" and ranker_context["evidence"].startswith("Evidence is 78.0%")
    assert "relevant" in ranker_context["recommendation"]["recommended_change"]
    reviewer_cost = next(f for f in findings if f["category"] == "COST" and f["affected_stage"] == "REVIEWER")
    assert "deterministic validation" in reviewer_cost["recommendation"]["recommended_change"]
    assert ("MODEL", "REVIEWER") in by  # a free model already handles reviews
    assert ("RELIABILITY", "RANKER") in by  # 20% retries and 10% failures
    assert ("LATENCY", "REVIEWER") in by  # p95 over 30s
    assert ("ARCHITECTURE", "FOLLOW_UP") in by  # the same application's follow-up generated 6 times
    assert [f["priority"] for f in findings] == sorted((f["priority"] for f in findings),
                                                       key=["HIGH", "MEDIUM", "LOW"].index)
    for f in findings:
        rec = f["recommendation"]
        assert set(rec) == {"title", "category", "priority", "finding", "evidence", "recommended_change",
                            "expected_benefit", "estimated_cost_saving", "risk", "affected_stage"}


def test_findings_need_enough_data():
    assert ai_advisor.detect_findings({"stages": [], "models": [], "totals": {"requests": 0}}) == []


# ───────────── advice only ─────────────

FORBIDDEN_WRITES = ("set_user_keys", "lock_settings_row", "settings_for", "profile_for", "write_text", "os.environ",
                    "open(", "import model_catalog", "model_catalog.reload", "setattr(", "update(AppSetting",
                    "update(CandidateProfile", "delete(")


def test_the_advisor_module_cannot_write_configuration():
    source = (ROOT / "app/services/ai_advisor.py").read_text()
    assert [token for token in FORBIDDEN_WRITES if token in source] == []
    tree = ast.parse(source)
    targets = [ast.unparse(t) for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AugAssign))
               for t in (node.targets if isinstance(node, ast.Assign) else [node.target])]
    assert not [t for t in targets if "preferences" in t or "llm_provider" in t or "model" == t.split(".")[-1]]


def test_running_an_analysis_changes_no_configuration(user_id, monkeypatch):
    seed_usage(user_id)
    switch_models = {**GOOD_ANSWER, "recommendations": [{
        **GOOD_ANSWER["recommendations"][0], "title": "Switch Reviewer to openrouter/free", "category": "MODEL",
        "recommended_change": "Set the review stage model to openrouter/free and lower min_review_score to 50."}]}
    monkeypatch.setattr(ai_advisor, "LLMClient", FakeAdvisorModel(switch_models))

    def snapshot():
        with SessionLocal() as db:
            setting = db.scalar(select(AppSetting).where(AppSetting.user_id == user_id))
            profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user_id))
            return ({c.name: getattr(setting, c.name) for c in AppSetting.__table__.columns if c.name != "updated_at"},
                    {c.name: getattr(profile, c.name) for c in CandidateProfile.__table__.columns if c.name != "updated_at"})

    before = snapshot()
    settings_before = get_settings().model_dump()
    catalog_before = model_catalog.DEFAULT_CATALOG.read_text()
    prompts_before = {p.name: p.read_text() for p in (ROOT / "app/prompts").glob("*.py")}
    with SessionLocal() as db:
        task = ai_advisor.queue_analysis(db, user_id, utcnow() - timedelta(days=7), utcnow())
        task_id = task.id
    run_until_done(task_id)

    assert snapshot() == before
    assert get_settings().model_dump() == settings_before
    assert model_catalog.DEFAULT_CATALOG.read_text() == catalog_before
    assert {p.name: p.read_text() for p in (ROOT / "app/prompts").glob("*.py")} == prompts_before
    with SessionLocal() as db:
        report = db.scalar(select(AIOptimizationReport).where(AIOptimizationReport.user_id == user_id))
    assert report.recommendations[0]["title"] == "Switch Reviewer to openrouter/free"  # stored as advice only


# ───────────── prompt ─────────────

def test_the_advisor_prompt_lives_in_app_prompts():
    prompt = build_ai_optimization_prompt({"totals": {"requests": 3}}, [{"category": "COST"}])
    assert PROMPT_VERSIONS["ai_optimization"] == AI_OPTIMIZATION_PROMPT_VERSION == "v4"
    assert prompt.user.startswith("AI_OPTIMIZATION") and prompt.name == "ai_optimization"
    assert "cannot and must never claim to change model configuration" in " ".join(prompt.system.split())
    for field in ("title", "category", "priority", "finding", "evidence", "recommended_change", "expected_benefit",
                  "estimated_cost_saving", "risk", "affected_stage", "summary", "estimated_savings"):
        assert field in prompt.system
    assert '"requests": 3' in prompt.user and '"requests": 3' not in prompt.system
    assert "You are an AI cost" not in (ROOT / "app/services/ai_advisor.py").read_text()  # no inline prompt
