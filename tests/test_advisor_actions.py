"""One-click actions for AI optimization recommendations: derived rules, briefs, coding prompts, and the
stage → apply → undo flow through the AI configuration's own draft/preview/apply."""
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    AIConfigVersion,
    AIOptimizationDecision,
    AIOptimizationReport,
    LLMRequestLog,
    WorkerModelConfig,
)
from app.services import advisor_actions as aa


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


def stat(n, failed=0, rate_limited=0):
    return aa.ModelStat(requests=n, failed=failed, rate_limited=rate_limited, answered_by_backup=0)


def worker(**cfg):
    return {"mode": "FIXED", "primary_model": "p", "fallback_model": None, "secondary_models": [], "escalation": None,
            "prompt_token_budget": None, "enabled": True, **cfg}


def by_type(actions):
    return {a["type"]: a for a in actions}


# ───────────── deriving actions: deterministic rules over the request log ─────────────

def test_failing_backups_are_dropped_and_the_next_one_becomes_the_fallback():
    workers = {"RANKER": worker(fallback_model="bad", secondary_models=["good", "also-bad"])}
    stats = {("RANKER", "p"): stat(200, 10), ("RANKER", "bad"): stat(100, 97), ("RANKER", "also-bad"): stat(40, 40),
             ("RANKER", "good"): stat(50, 2)}
    action = by_type(aa.candidate_actions(workers, stats))["drop-backups"]
    assert action["payload"] == {"fallback_model": "good", "secondary_models": None}
    assert action["kind"] == "CONFIG" and action["worker_type"] == "RANKER"
    assert {r["model"]: (r["role_before"], r["role_after"]) for r in action["models"]}["bad"] == ("fallback", "—")
    assert action["confidence"]["level"] == "MEDIUM"  # the decisive models have 40 and 100 requests


def test_a_model_without_enough_requests_is_never_judged():
    workers = {"RANKER": worker(fallback_model="bad")}
    assert aa.candidate_actions(workers, {("RANKER", "bad"): stat(aa.MIN_SAMPLE - 1, aa.MIN_SAMPLE - 1)}) == []


def test_a_failing_primary_is_replaced_only_by_a_configured_model_that_works():
    workers = {"JD_PARSER": worker(primary_model="router", fallback_model="ok", secondary_models=["dead"])}
    stats = {("JD_PARSER", "router"): stat(500, 440), ("JD_PARSER", "ok"): stat(60, 3),
             ("JD_PARSER", "dead"): stat(30, 30)}
    actions = by_type(aa.candidate_actions(workers, stats))
    assert actions["replace-primary"]["payload"] == {"primary_model": "ok", "fallback_model": None,
                                                     "secondary_models": None}
    assert "drop-backups" not in actions  # the replacement already leaves the dead backup out
    # No healthy configured model: nothing is invented; only the failing backup is dropped.
    stats[("JD_PARSER", "ok")] = stat(60, 30)
    assert "replace-primary" not in by_type(aa.candidate_actions(workers, stats))


def test_a_failing_escalation_offers_off_or_a_healthy_retarget_as_alternatives():
    esc = {"model": "flaky", "tier": None, "conditions": ["LOW_CONFIDENCE"], "confidence_threshold": 0.9}
    workers = {"REVIEWER": worker(fallback_model="steady", escalation=esc)}
    stats = {("REVIEWER", "flaky"): stat(168, 140), ("REVIEWER", "steady"): stat(80, 4), ("REVIEWER", "p"): stat(190, 17)}
    actions = by_type(aa.candidate_actions(workers, stats))
    assert actions["escalation-off"]["payload"] == {"escalation": None}
    assert actions["escalation-retarget"]["payload"]["escalation"] == {**esc, "model": "steady"}
    assert actions["escalation-off"]["group"] == actions["escalation-retarget"]["group"] == "escalation.REVIEWER"


def test_a_healthy_escalation_that_answers_most_requests_is_promoted():
    esc = {"model": "big", "tier": None, "conditions": ["LOW_CONFIDENCE"], "confidence_threshold": 0.9}
    workers = {"RANKER": worker(escalation=esc)}
    stats = {("RANKER", "big"): stat(300, 6), ("RANKER", "p"): stat(500, 20)}
    routing = {"workers": [{"worker_type": "RANKER", "escalation_rate": 0.7}]}
    action = by_type(aa.candidate_actions(workers, stats, routing))["promote-escalation"]
    assert action["payload"]["primary_model"] == "big" and action["payload"]["escalation"] is None


def test_a_prompt_budget_is_offered_only_to_workers_that_trim():
    stages = [{"stage": "RANKER", "requests": 400, "avg_context_tokens": 5968.0, "p95_context_tokens": 7527},
              {"stage": "REVIEWER", "requests": 400, "avg_context_tokens": 9235.0, "p95_context_tokens": 12377}]
    actions = aa.candidate_actions({"RANKER": worker(), "REVIEWER": worker()}, {}, None, stages)
    budgets = [a for a in actions if a["type"] == "prompt-budget"]
    assert [(a["worker_type"], a["payload"]) for a in budgets] == [("RANKER", {"prompt_token_budget": 6000})]


def test_disabled_and_embedding_workers_get_no_actions():
    workers = {"RANKER": worker(fallback_model="bad", enabled=False), "EMBEDDER": worker(fallback_model="bad")}
    stats = {("RANKER", "bad"): stat(100, 100), ("EMBEDDER", "bad"): stat(100, 100)}
    assert aa.candidate_actions(workers, stats) == []


def test_the_action_id_changes_when_the_configuration_does():
    stats = {("RANKER", "bad"): stat(100, 100)}
    one = aa.candidate_actions({"RANKER": worker(fallback_model="bad", secondary_models=["x"])}, stats)[0]["id"]
    two = aa.candidate_actions({"RANKER": worker(fallback_model="bad", secondary_models=["y"])}, stats)[0]["id"]
    assert one != two


# ───────────── matching actions to recommendations ─────────────

ACTIONS = [
    {"id": "a", "type": "drop-backups", "worker_type": "RANKER",
     "models": [{"model": "Openrouter", "role_before": "fallback", "role_after": "—"}]},
    {"id": "b", "type": "escalation-off", "worker_type": "REVIEWER",
     "models": [{"model": "nemotron-free", "role_before": "escalation", "role_after": "—"}]},
    {"id": "c", "type": "prompt-budget", "worker_type": "RANKER", "models": []},
]


def rec(**fields):
    return {"title": "t", "category": "RELIABILITY", "priority": "HIGH", "finding": "", "evidence": "",
            "recommended_change": "", "expected_benefit": "", "estimated_cost_saving": "unknown", "risk": "",
            "affected_stage": "RANKER", **fields}


def test_a_stage_recommendation_gets_that_stages_model_actions():
    assert [a["id"] for a in aa.actions_for(rec(), ACTIONS)] == ["a"]
    assert [a["id"] for a in aa.actions_for(rec(category="CONTEXT", recommended_change="Trim context"), ACTIONS)] == ["c"]


def test_an_all_stage_recommendation_only_gets_actions_on_models_it_names():
    named = rec(affected_stage="ALL", recommended_change="Stop using nemotron-free for structured output")
    assert [a["id"] for a in aa.actions_for(named, ACTIONS)] == ["b"]
    assert aa.actions_for(rec(affected_stage="ALL", recommended_change="Improve reliability"), ACTIONS) == []


def test_code_changes_get_a_coding_prompt_and_pure_config_changes_do_not():
    assert aa.needs_code(rec(category="ARCHITECTURE"), ACTIONS)
    assert aa.needs_code(rec(recommended_change="add result caching"), ACTIONS[:1])
    assert aa.needs_code(rec(), [])  # nothing one-click: the assistant can still carry it out
    assert not aa.needs_code(rec(recommended_change="Remove Openrouter from the Ranker"), ACTIONS[:1])


def test_the_coding_prompt_carries_the_finding_owners_and_invariants_but_nothing_private():
    report = AIOptimizationReport(time_range_start=datetime(2026, 11, 7, tzinfo=UTC),
                                  time_range_end=datetime(2026, 11, 14, tzinfo=UTC))
    r = rec(title="Deduplicate repeated calls", category="ARCHITECTURE", evidence="313 extra calls",
            recommended_change="Cache successful results by entity-stage-prompt-version")
    prompt = aa.coding_prompt(r, report, {"stage": "RANKER", "requests": 1490, "failure_rate": 0.5})
    assert prompt.startswith("# Task: Deduplicate repeated calls")
    assert "AGENTS.md" in prompt and "313 extra calls" in prompt and "app/services/ranker.py" in prompt
    assert "app/services/llm.py" in prompt and "input hash" in prompt
    assert "LLMClient.generate_prompt" in prompt and "1,490 requests" in prompt
    assert "frontend tests only if the frontend changed" in " ".join(prompt.split())


def test_the_recommendation_key_is_stable_across_reports():
    assert aa.recommendation_key(rec(title="Fix X ")) == aa.recommendation_key(rec(title="fix x"))
    assert aa.recommendation_key(rec(affected_stage="TAILOR")) != aa.recommendation_key(rec())


# ───────────── telemetry is counted by the model the worker asked for ─────────────

def log(user_id, worker, requested, answered, status, *, fallback=False, error=None, when=None):
    return LLMRequestLog(request_id=str(uuid.uuid4()), user_id=user_id, stage="REVIEWER", provider="litellm",
                         requested_model=requested, model=answered, prompt_name="review", prompt_version="v1",
                         input_tokens=10, output_tokens=5, total_tokens=15, usage_source="provider_reported",
                         latency_ms=100, cost_source="free", status=status, retry_count=0, error_type=error,
                         structured_output_valid=status == "SUCCEEDED", structured_output_retry_count=0, cached=False,
                         worker_type=worker, fallback_used=fallback, created_at=when or utcnow() - timedelta(hours=1),
                         workflow_type="job")


def test_a_routers_successes_logged_under_member_models_still_count_for_the_router(account):
    with SessionLocal() as db:
        db.add_all([log(account.user_id, "RANKER", "router", "router", "FAILED", error="RATE_LIMIT") for _ in range(6)]
                   + [log(account.user_id, "RANKER", "router", "member-a", "SUCCEEDED") for _ in range(14)]
                   + [log(account.user_id, "RANKER", "router", "member-b", "SUCCEEDED", when=utcnow() - timedelta(days=30))])
        db.commit()
        stats = aa.model_stats(db, account.user_id, utcnow() - timedelta(days=7), utcnow())
    assert stats[("RANKER", "router")].public() == {"requests": 20, "failed": 6, "failure_rate": 0.3,
                                                    "rate_limited": 6, "answered_by_backup": 0}
    assert ("RANKER", "member-a") not in stats


# ───────────── stage → apply → undo, end to end through the AI configuration ─────────────

def setup_reviewer(acct):
    acct.call("POST", "/models/sync")
    acct.apply()
    acct.worker("REVIEWER", model_mode="FIXED", primary_model="tier-3-review", fallback_model="tier-1-fast",
                secondary_models=["tier-2-reasoning"])
    acct.apply()
    with SessionLocal() as db:
        rows = [log(acct.user_id, "REVIEWER", "tier-1-fast", "tier-1-fast", "FAILED") for _ in range(30)]
        rows += [log(acct.user_id, "REVIEWER", "tier-2-reasoning", "tier-2-reasoning", "SUCCEEDED") for _ in range(30)]
        rows += [log(acct.user_id, "REVIEWER", "tier-3-review", "tier-3-review", "SUCCEEDED") for _ in range(40)]
        report = AIOptimizationReport(
            user_id=acct.user_id, time_range_start=utcnow() - timedelta(days=7), time_range_end=utcnow() + timedelta(
                minutes=5), summary="s", recommendations=[rec(
                    title="Drop the failing Reviewer fallback", affected_stage="REVIEWER",
                    recommended_change="Remove tier-1-fast from the Reviewer")],
            aggregates={"stages": [{"stage": "REVIEWER", "requests": 100, "failure_rate": 0.3}], "totals": {}},
            generated_by="rules", created_at=utcnow())
        db.add_all([*rows, report])
        db.commit()
        return report.id


def recommendations(acct):
    response = acct.client.get("/api/monitoring/recommendations")
    assert response.status_code == 200, response.text
    return response.json()


def post(acct, path, expect=200, **kw):
    response = acct.client.post(f"/api/monitoring/recommendations{path}", **kw)
    assert response.status_code == expect, response.text
    return response.json()


def reviewer(user_id):
    with SessionLocal() as db:
        row = db.scalar(select(WorkerModelConfig).where(WorkerModelConfig.user_id == user_id,
                                                        WorkerModelConfig.worker_type == "REVIEWER"))
        return row.fallback_model, row.secondary_models


def test_one_click_stage_apply_and_undo(account):
    report_id = setup_reviewer(account)
    page = recommendations(account)
    item = page["actions"]["items"][0]
    assert page["actions"]["blocked"] is None and item["coding_prompt"] is None
    action = next(a for a in item["actions"] if a["type"] == "drop-backups")
    assert action["payload"] == {"fallback_model": "tier-2-reasoning", "secondary_models": None}
    assert item["brief"]["confidence"]["level"] == "HIGH"  # 100 Reviewer requests in the period
    assert action["confidence"]["level"] == "MEDIUM"  # the decision rests on the failing model's 30 requests
    versions_before = account.get("/versions")["versions"][0]["version"]

    staged = post(account, f"/{report_id}/actions/{action['id']}/stage")
    assert staged["preview"]["can_apply"] and staged["decision"]["status"] == "STAGED"
    assert any(c["label"] == "Reviewer · fallback model" and c["after"] == "tier-2-reasoning"
               for c in staged["preview"]["changes"])
    assert reviewer(account.user_id)[0] == "tier-2-reasoning"  # in the draft …
    assert account.get("/versions")["versions"][0]["version"] == versions_before  # … but not live yet
    assert recommendations(account)["actions"]["staged"]["id"] == staged["decision"]["id"]
    post(account, f"/{report_id}/actions/{action['id']}/stage", expect=409)  # one staged change at a time

    applied = post(account, f"/decisions/{staged['decision']['id']}/apply")
    assert applied["decision"]["status"] == "APPLIED"
    assert applied["decision"]["config_version"] == versions_before + 1
    latest = account.get("/versions")["versions"][0]
    assert latest["summary"].startswith("AI optimization: Remove failing backup model from Reviewer")
    after = recommendations(account)
    assert after["actions"]["staged"] is None
    assert not [a for a in after["actions"]["items"][0]["actions"] if a["type"] == "drop-backups"]  # done already
    assert after["actions"]["items"][0]["applied"][0]["status"] == "APPLIED"

    undone = post(account, f"/decisions/{staged['decision']['id']}/undo")
    assert undone["decision"]["status"] == "UNDONE" and undone["decision"]["undo_version"] == versions_before + 2
    assert reviewer(account.user_id) == ("tier-1-fast", ["tier-2-reasoning"])
    post(account, f"/decisions/{staged['decision']['id']}/undo", expect=409)  # only once


def test_cancel_puts_the_draft_back(account):
    report_id = setup_reviewer(account)
    action = recommendations(account)["actions"]["items"][0]["actions"][0]
    staged = post(account, f"/{report_id}/actions/{action['id']}/stage")
    post(account, f"/decisions/{staged['decision']['id']}/cancel")
    assert reviewer(account.user_id) == ("tier-1-fast", ["tier-2-reasoning"])
    assert recommendations(account)["actions"]["blocked"] is None
    post(account, f"/decisions/{staged['decision']['id']}/apply", expect=409)


def test_unapplied_edits_block_one_click_changes(account):
    report_id = setup_reviewer(account)
    action = recommendations(account)["actions"]["items"][0]["actions"][0]
    account.worker("TAILOR", temperature=0.3)  # someone's own edit, not applied yet
    page = recommendations(account)
    assert "unapplied edits" in page["actions"]["blocked"]
    post(account, f"/{report_id}/actions/{action['id']}/stage", expect=409)
    with SessionLocal() as db:
        assert db.scalar(select(WorkerModelConfig.temperature).where(
            WorkerModelConfig.user_id == account.user_id, WorkerModelConfig.worker_type == "TAILOR")) == 0.3


def test_a_stale_action_is_refused(account):
    report_id = setup_reviewer(account)
    action = recommendations(account)["actions"]["items"][0]["actions"][0]
    account.worker("REVIEWER", fallback_model="tier-2-reasoning", secondary_models=[])
    account.apply()  # the person fixed it by hand
    detail = post(account, f"/{report_id}/actions/{action['id']}/stage", expect=409)["detail"]
    assert "no longer applies" in detail


def test_undo_refuses_once_a_newer_version_exists(account):
    report_id = setup_reviewer(account)
    action = recommendations(account)["actions"]["items"][0]["actions"][0]
    staged = post(account, f"/{report_id}/actions/{action['id']}/stage")
    post(account, f"/decisions/{staged['decision']['id']}/apply")
    account.worker("TAILOR", temperature=0.4)
    account.apply()
    assert "has changed since" in post(account, f"/decisions/{staged['decision']['id']}/undo", expect=409)["detail"]


def test_dismiss_hides_a_recommendation_and_show_again_brings_it_back(account):
    report_id = setup_reviewer(account)
    key = recommendations(account)["actions"]["items"][0]["key"]
    decision = post(account, f"/{report_id}/dismiss", json={"key": key})["decision"]
    assert recommendations(account)["actions"]["items"][0]["dismissed"]["id"] == decision["id"]
    post(account, f"/decisions/{decision['id']}/restore")
    assert recommendations(account)["actions"]["items"][0]["dismissed"] is None
    post(account, f"/{report_id}/dismiss", expect=404, json={"key": "nope"})


def test_another_account_cannot_touch_these_decisions(account, anonymous):
    report_id = setup_reviewer(account)
    action = recommendations(account)["actions"]["items"][0]["actions"][0]
    staged = post(account, f"/{report_id}/actions/{action['id']}/stage")
    from fastapi.testclient import TestClient

    from app.main import app

    other = TestClient(app)
    email = f"other-{uuid.uuid4().hex}@example.com"
    assert other.post("/api/auth/signup", json={"email": email, "password": "a password for tests",
                                                "confirm_password": "a password for tests"}).status_code == 201
    for path in (f"/decisions/{staged['decision']['id']}/apply", f"/decisions/{staged['decision']['id']}/cancel"):
        assert other.post(f"/api/monitoring/recommendations{path}").status_code == 404
    assert other.post(f"/api/monitoring/recommendations/{report_id}/actions/{action['id']}/stage").status_code == 404
    with SessionLocal() as db:
        assert db.get(AIOptimizationDecision, staged["decision"]["id"]).status == "STAGED"
        assert db.scalar(select(AIConfigVersion.id).where(AIConfigVersion.user_id == account.user_id)) is not None


def test_changes_only_go_through_the_ai_configuration_service():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parents[1] / "app/services/advisor_actions.py").read_text())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    # No configuration tables or settings rows in reach: every change is ai_model_config.update_worker/apply/restore.
    assert not imported & {"WorkerModelConfig", "AIRoutingConfig", "ModelTier", "AIModel", "AppSetting"}
    assert "setattr" not in calls and not [c for c in calls if c.startswith(("update(", "delete("))]
    assert {"ai_model_config.update_worker", "ai_model_config.apply", "ai_model_config.restore"} <= calls
