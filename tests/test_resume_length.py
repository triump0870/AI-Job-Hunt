"""Tailored resume length: a page target and a top-down bullet budget per role (app.services.resume_length).

The structured resume is the foundation tailoring selects from, not a checklist: the newest role gets the most
bullets, older roles fewer, a budget of 0 keeps a role's heading only, and code enforces the budget."""
import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import Job, ResumeVariant, TailorTask
from app.prompts import build_tailoring_prompt
from app.services import context_builder as cb
from app.services import resume_length as rl
from app.services import tailor_tasks
from app.services.llm import LLMClient
from app.services.resume_format import parse_resume
from app.services.workflow import build_resume_markdown, tailor_job
from tests.test_context_builder import add_job, build, session, verify
from tests.test_context_builder import user as user  # noqa: PLC0414  the fixture, re-exported
from tests.test_prompt_architecture import COMPLETED, set_fit

client = TestClient(app)


def settings(**length):
    response = client.patch("/api/settings", json={"tailoring_length": length})
    assert response.status_code == 200, response.text
    return response.json()["tailoring_length"]


def ready_job(user_id):
    job_id = add_job(user_id)
    set_fit(user_id, job_id, COMPLETED)
    verify("experience", "skills", "summary")
    return job_id


# ───────────── the plan ─────────────

def test_presets_give_the_newest_role_the_most_bullets():
    assert rl.PRESETS == {1: (4, 3, 2, 1, 0), 2: (6, 5, 4, 3, 2, 1), 3: (7, 6, 5, 4, 3, 2)}
    one = rl.LengthPlan(1, rl.PRESETS[1])
    assert [one.cap(rank) for rank in range(7)] == [4, 3, 2, 1, 0, 0, 0], "older roles: heading only"
    assert rl.LengthPlan(2, rl.PRESETS[2]).cap(9) == 1, "the last budget repeats for every older role"


def test_roles_are_ranked_by_recency_not_by_where_the_resume_lists_them():
    plan = rl.LengthPlan(2, (6, 5, 4))
    roles = [{"dates": "Jan 2016 - Dec 2018"}, {"dates": "Jun 2024 - Present"}, {"dates": "2019-03 - 2024-05"}]
    assert rl.role_caps(plan, roles) == {1: 6, 2: 5, 0: 4}, "current first, then the latest end date"
    assert rl.role_caps(plan, [{"dates": ""}, {"dates": ""}]) == {0: 6, 1: 5}, "no dates: document order"


def test_edited_budgets_are_clamped_and_bad_ones_fall_back_to_the_preset():
    assert rl.clean_budgets([9, -1, 3], 2) == (8, 0, 3)
    assert rl.clean_budgets(["x"], 2) == rl.PRESETS[2] and rl.clean_budgets([], 1) == rl.PRESETS[1]
    assert rl.clean_pages(5) == 2 and rl.clean_pages("1") == 1


def test_the_page_estimate_grows_with_the_resume():
    short = "# Jane\n## Experience\n### Lead | Acme | 2020 - 2024\n- Built things\n"
    long = short + "\n".join(f"- Delivered project number {i} with measurable results for many customers" for i in range(90))
    assert rl.estimate_pages(short) < 0.5 and rl.estimate_pages(long) > 1.5


# ───────────── settings ─────────────

def test_the_default_length_and_budgets_are_saved_and_restored(user):
    assert client.get("/api/settings").json()["tailoring_length"]["pages"] == 2, "2 pages by default"
    saved = settings(pages=1, budgets={"1": [5, 2]})
    assert saved["pages"] == 1 and saved["budgets"]["1"] == [5, 2] and saved["presets"]["1"] == [4, 3, 2, 1, 0]
    assert settings(budgets={"1": []})["budgets"]["1"] == [4, 3, 2, 1, 0], "[] restores the preset"
    assert client.patch("/api/settings", json={"tailoring_length": {"pages": 4}}).status_code == 422
    assert client.patch("/api/settings", json={"tailoring_length": {"budgets": {"2": [9]}}}).status_code == 422
    with session(user) as db:
        assert rl.plan_for(db, user) == rl.LengthPlan(1, rl.PRESETS[1])
        assert rl.plan_for(db, user, pages=3) == rl.LengthPlan(3, rl.PRESETS[3]), "a per-job choice wins"


# ───────────── context and prompt ─────────────

def test_the_context_offers_candidates_per_budget_and_heading_only_roles(user):
    job_id = ready_job(user)
    context = build(user, cb.build_tailoring_context, job_id, plan=rl.LengthPlan(1, (2, 0)))
    roles = {role["role_index"]: role for role in context.source_resume["roles"]}
    assert context.length_plan["role_caps"] == {"0": 2, "1": 0} and context.length_plan["target"] == "1 page"
    assert roles[0]["max_bullets"] == 2 and 1 <= len(roles[0]["bullets"]) <= 2 + cb.CANDIDATE_SLACK
    assert roles[1]["max_bullets"] == 0 and roles[1]["bullets"] == [], "a 0 budget sends the heading only"
    prompt = build_tailoring_prompt(candidate_profile=None, job=None, context=context)
    assert "LENGTH PLAN" in prompt.user and '"max_bullets_by_role_index"' in prompt.user
    assert "not a checklist" in prompt.system and "Update at most 3 roles" not in prompt.system
    assert prompt.version == "v11"


# ───────────── enforcement and rendering ─────────────

def _tailor(user, monkeypatch, role_updates, pages=None):
    monkeypatch.setattr(LLMClient, "_mock_tailoring", staticmethod(lambda _user: {
        "summary": None, "skills_order": [], "highlights": [], "selected_evidence_ids": [], "role_updates": role_updates}))
    job_id = ready_job(user)
    with session(user) as db:
        variant = tailor_job(db, db.get(Job, job_id), pages=pages)
        return job_id, variant.id


def test_bullets_beyond_a_budget_are_dropped_and_old_roles_keep_their_heading(user, monkeypatch):
    settings(budgets={"1": [1, 0]})
    _, variant_id = _tailor(user, monkeypatch, [
        {"role_index": 0, "bullets": [{"text": "Reduced production incidents by 45% across Kafka pipelines", "evidence_ids": []},
                                      {"text": "Led a team of 8 engineers on the data platform", "evidence_ids": []}]},
        {"role_index": 1, "bullets": [{"text": "Built React dashboards for retail analytics", "evidence_ids": []}]}],
        pages=1)
    with session(user) as db:
        variant = db.get(ResumeVariant, variant_id)
        content = variant.content
        parsed = parse_resume(variant.markdown)
    assert content["length_plan"]["pages"] == 1
    assert {i["code"] for i in content["validation"]["issues"]} >= {"LENGTH"}
    assert [len(r.bullets) for r in parsed.roles] == [1, 0], "the newest role keeps 1 bullet; the older one none"
    assert [(r.title, r.company) for r in parsed.roles] == [("Lead Engineer", "Zendrive"),
                                                            ("Frontend Developer", "Pixel Co")], "headings intact"


def test_a_role_the_tailor_left_alone_shows_only_its_budget(user, monkeypatch):
    settings(budgets={"1": [2, 1]})
    _, variant_id = _tailor(user, monkeypatch, [
        {"role_index": 1, "bullets": [{"text": "Built React dashboards for retail analytics", "evidence_ids": []}]}],
        pages=1)
    with session(user) as db:
        variant = db.get(ResumeVariant, variant_id)
        parsed = parse_resume(variant.markdown)
        caps = {int(k): v for k, v in variant.content["length_plan"]["role_caps"].items()}
        assert caps == {0: 2, 1: 1}
        assert len(parsed.roles[0].bullets) == 2, "3 source bullets, but only its budget of 2, most relevant first"
        assert "Organised the office book club" not in variant.markdown, "the least relevant bullet is left out"
        # Re-rendering (accepting or reverting an edit) keeps the plan.
        assert build_resume_markdown(variant_profile(db, variant), variant.content) == variant.markdown


def variant_profile(db, variant):
    from app.services import resume_verification as rv
    from app.services.workflow import get_profile

    return rv.for_job(db, get_profile(db), db.get(Job, variant.job_id))


def test_a_packet_made_before_length_plans_renders_as_before(user):
    with session(user) as db:
        from app.services.workflow import get_profile

        content = {"summary": "", "skills": [], "experience": [], "selected_evidence_ids": []}
        full = build_resume_markdown(get_profile(db), content)
    assert [len(r.bullets) for r in parse_resume(full).roles] == [3, 2], "no plan: every source bullet, as before"


# ───────────── per-job choice ─────────────

def test_a_job_can_be_tailored_to_its_own_length(user, monkeypatch):
    job_id = ready_job(user)
    assert client.post("/api/tailor-tasks", json={"ids": [job_id], "pages": 4}).status_code == 422
    out = client.post("/api/tailor-tasks", json={"ids": [job_id], "pages": 1}).json()
    assert out["queued"][0]["pages"] == 1
    with SessionLocal() as db:
        assert db.get(TailorTask, out["queued"][0]["id"]).options == {"pages": 1}
    seen = {}
    real = tailor_tasks.tailor_job

    def spy(db, job, progress, pages=None):
        seen["pages"] = pages
        return real(db, job, progress=progress, pages=pages)

    monkeypatch.setattr(tailor_tasks, "tailor_job", spy)
    tailor_tasks.run_pending(SessionLocal)
    assert seen["pages"] == 1
    packet = client.get(f"/api/tailor/{job_id}").json()["resume"]
    assert packet["length"]["target_pages"] == 1 and packet["length"]["estimated_pages"] > 0


@pytest.mark.parametrize("pages", [None, 3])
def test_the_one_job_endpoint_takes_pages_or_the_default(user, pages):
    job_id = ready_job(user)
    response = client.post(f"/api/jobs/{job_id}/tailor", params={"pages": pages} if pages else {})
    assert response.status_code == 200, response.text
    packet = client.get(f"/api/tailor/{job_id}").json()["resume"]
    assert packet["length"]["target_pages"] == (pages or 2)


# ───────────── Claude Desktop (MCP) ─────────────

def test_claude_desktop_gets_the_budgets_and_its_saves_are_held_to_them(user):
    from datetime import UTC, datetime

    from app.mcp import tools

    settings(pages=1, budgets={"1": [1, 0]})
    job_id = ready_job(user)
    with session(user) as db:
        db.get(Job, job_id).triaged_at = datetime.now(UTC).replace(tzinfo=None)
        db.commit()
        context = tools.get_tailoring_context(db, [job_id])
        resume = context["active_resume"]
        assert resume["length_plan"]["target"] == "1 page" and resume["length_plan"]["summary_sentences"] == 3
        assert [r["max_bullets"] for r in resume["roles"]] == [1, 0]
        saved = tools.save_tailored_resume(db, {
            "job_id": job_id, "summary": "", "skills_order": [], "resume_bullets": [], "cover_letter": "",
            "screening_answer_drafts": {}, "experience_sections": [
                {"role_index": 0, "bullets": [
                    {"text": "Reduced production incidents by 45% across Kafka pipelines", "evidence_ids": []},
                    {"text": "Led a team of 8 engineers on the data platform", "evidence_ids": []}]},
                {"role_index": 1, "bullets": [{"text": "Built React dashboards for retail analytics", "evidence_ids": []}]}]})
        item = saved["results"][0] if "results" in saved else saved["items"][0]
        assert item["status"] == "SAVED", item
        assert {t["code"] for t in item["trimmed"]} == {"LENGTH"}, "trimmed and reported, never rejected"
        variant = db.get(ResumeVariant, item["resume_id"])
        assert [len(r.bullets) for r in parse_resume(variant.markdown).roles] == [1, 0]
