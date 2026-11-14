"""Claude Desktop MCP: tools, validation, review decisions, audit trail, tokens and the stdio server."""
import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import CurrentUser
from app.db import SessionLocal, engine
from app.main import app
from app.mcp import tools
from app.mcp.__main__ import configured_token
from app.mcp.__main__ import main as mcp_main
from app.mcp.server import TOOL_NAMES, build_server, run_tool
from app.models import ConversationAction, Evidence, Job, McpToken, ResumeVariant
from app.services import llm, mcp_tokens, tailor_tasks
from app.services.user_settings import profile_for, settings_for
from app.settings import get_settings
from tests.conftest import make_user
from tests.resume_support import use_resume_text

ROOT = Path(__file__).resolve().parents[1]
MODEL_CALL = llm.LLMClient.generate_prompt_json
MASTER = """# Test Candidate

## Summary
Backend engineer building Python services.

## Skills
Python, FastAPI, PostgreSQL, AWS

## Experience
### Senior Engineer | Acme Corp | 2021 - Present
- Built Python FastAPI services handling 10k requests per second
- Led a team of 4 engineers delivering payments APIs

### Engineer | Beta Labs | 2018 - 2021
- Migrated reporting jobs to PostgreSQL and cut runtime by 40%
"""


def now():
    return datetime.now(UTC).replace(tzinfo=None)


def session(user_id):
    db = SessionLocal()
    db.info["user_id"] = user_id
    return db


@pytest.fixture
def account():
    """A fresh account with a structured resume, verified evidence and ranked jobs in the Tailor queue."""
    user_id = make_user(SessionLocal)
    with session(user_id) as db:
        profile = profile_for(db)
        profile.name, profile.master_resume = "Test Candidate", MASTER
        profile.work_authorization = "Singapore: EP (Employment Pass)"
        settings_for(db).min_review_score = 75
        acme = Evidence(company="Acme Corp", role="Senior Engineer", skills=["Python", "Kafka"],
                        claim="Scaled the payments API to 25k requests per second with Python and Kafka")
        beta = Evidence(company="Beta Labs", role="Engineer", claim="Cut reporting runtime by 40% with PostgreSQL",
                        skills=["PostgreSQL"])
        unverified = Evidence(company="Acme Corp", claim="Owned a 50 person org", verified=False)
        db.add_all([acme, beta, unverified])
        jobs = []
        for index, (score, company, location) in enumerate([(92, "Grab", "Singapore"), (88, "Stripe", "Remote"),
                                                            (70, "Shopee", "Singapore")]):
            job = Job(source="manual", source_ref=uuid.uuid4().hex, title="Senior Backend Engineer", company=company,
                      location=location, description="Python FastAPI payments at scale. Kafka a plus.",
                      fit_score=score, status="shortlisted", triaged_at=now(),
                      posted_at=now() - timedelta(days=index * 10),
                      fit_analysis={"summary": "Strong backend fit", "resume_gaps": ["Kafka not on resume"],
                                    "recommended_resume_focus": ["Payments scale"], "internal_note": "not sent"})
            db.add(job)
            jobs.append(job)
        db.commit()
        yield {"user_id": user_id, "jobs": [j.id for j in jobs], "acme": acme.id, "beta": beta.id,
               "unverified": unverified.id}


def tailored(job_id, evidence_id, **overrides):
    result = {
        "job_id": job_id, "summary": "Backend engineer building Python payments services at scale.",
        "skills_order": ["Python", "Kafka", "FastAPI"],
        "experience_sections": [
            {"role_index": 0, "bullets": [
                {"text": "Scaled the payments API to 25k requests per second with Python and Kafka",
                 "evidence_ids": [str(evidence_id)]},
                {"text": "Led a team of 4 engineers delivering payments APIs", "evidence_ids": []}]}],
        "resume_bullets": [], "cover_letter": "I build Python payment systems.", "screening_answer_drafts": {},
    }
    return {**result, **overrides}


def review(job_id, **overrides):
    return {"job_id": job_id, "review_score": 92, "confidence": 0.94, "unsupported_claims": [],
            "missing_requirements": [], "overstated_claims": [], "risks": [], "recommendation": "PASS", **overrides}


def call(user_id, name, **arguments):
    return run_tool(SessionLocal, user_id, name, getattr(tools, name), arguments)


def _refuse(*_args, **_kwargs):
    raise AssertionError("Claude Desktop mode must not call the LLM layer")


@pytest.fixture(autouse=True)
def no_model_calls(monkeypatch):
    """Claude Desktop mode never calls a model API from Job Hunt OS."""
    monkeypatch.setattr(llm.LLMClient, "generate_prompt_json", _refuse)


# ───────────── server ─────────────

def test_server_exposes_only_the_approved_tools_and_prompts(account):
    server = build_server(account["user_id"])
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == set(TOOL_NAMES)
    assert not any(word in name for name in names for word in ("sql", "file", "shell", "env", "key", "setting"))
    assert {p.name for p in asyncio.run(server.list_prompts())} == {"tailor_jobs", "review_applications", "apply_to_jobs",
                                                                  "follow_up"}
    content = asyncio.run(server.call_tool("list_jobs_for_tailoring", {"minimum_fit_score": 85}))
    assert "Grab" in str(content)


def test_new_tools_are_registered_with_their_arguments(account):
    server = build_server(account["user_id"])
    tools_by_name = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    expected = {"find_jobs": {"job_id", "query", "stage", "limit"},
                "get_job_documents": {"job_id", "formats", "include_content"},
                "list_followups_due": {"within_days", "limit"},
                "save_followup_draft": {"application_id", "text", "channel"},
                "record_followup_sent": {"followup_id", "sent_text", "channel", "sent_at", "recipient"},
                "skip_followup": {"followup_id", "application_id", "reason"}}
    for name, arguments in expected.items():
        assert name in TOOL_NAMES and hasattr(tools, name)
        assert set(tools_by_name[name].input_schema["properties"]) == arguments, name
    assert "who_applies" in tools_by_name["list_jobs_for_application"].input_schema["properties"]
    assert "cover_letter_files" in tools_by_name["get_job_packet"].input_schema["properties"]


def test_server_refuses_to_start_without_a_valid_token(account, monkeypatch, capsys):
    monkeypatch.delenv("JOBHUNT_MCP_TOKEN", raising=False)
    assert mcp_main(["serve"]) == 2
    assert "authentication failed" in capsys.readouterr().err
    with session(account["user_id"]) as db:
        row, token = mcp_tokens.create_token(db)
        assert row.token_hash != token and token.startswith("jhos_mcp_")
        mcp_tokens.revoke_token(db, row.id)
    monkeypatch.setenv("JOBHUNT_MCP_TOKEN", token)
    assert mcp_main(["serve"]) == 2
    monkeypatch.setenv("JOBHUNT_MCP_TOKEN", "jhos_mcp_unknown")
    assert mcp_main(["serve"]) == 2


def test_token_is_read_from_dotenv_when_the_client_passes_no_environment(monkeypatch, tmp_path):
    """MCP clients such as the Inspector start servers with a minimal environment; .env must still work."""
    env_file = tmp_path / ".env"
    env_file.write_text("JOBHUNT_MCP_TOKEN=jhos_mcp_from_dotenv\n")
    monkeypatch.delenv("JOBHUNT_MCP_TOKEN", raising=False)
    assert configured_token(env_file) == "jhos_mcp_from_dotenv"
    monkeypatch.setenv("JOBHUNT_MCP_TOKEN", "jhos_mcp_from_client")
    assert configured_token(env_file) == "jhos_mcp_from_client"


def test_stdio_server_starts_and_lists_tools_for_the_token_account(account):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with session(account["user_id"]) as db:
        _, token = mcp_tokens.create_token(db)
    database = engine.url.render_as_string(hide_password=False)  # the database this suite uses
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp"], cwd="/",  # the server resolves .env and relative paths itself
                                   env={"PYTHONPATH": str(ROOT), "JOBHUNT_MCP_TOKEN": token, "DATABASE_URL": database,
                                        "SCHEDULER_ENABLED": "false", "PATH": os.environ.get("PATH", "")})

    async def run():
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            listed = await client.list_tools()
            result = await client.call_tool("list_jobs_for_tailoring", {"limit": 2})
            return {t.name for t in listed.tools}, result

    names, result = asyncio.run(asyncio.wait_for(run(), timeout=60))
    assert names == set(TOOL_NAMES)
    assert "Grab" in str(result.content) and "Stripe" in str(result.content)
    with session(account["user_id"]) as db:
        assert db.scalar(select(McpToken)).last_used_at is not None


def test_token_api_creates_lists_and_revokes_without_exposing_hashes():
    client = TestClient(app)
    created = client.post("/api/settings/mcp-tokens", json={"name": "Laptop"}).json()
    assert created["token"].startswith("jhos_mcp_") and created["name"] == "Laptop"
    listed = client.get("/api/settings/mcp-tokens").json()["tokens"]
    assert any(t["id"] == created["id"] for t in listed) and all("token" not in t and "token_hash" not in t for t in listed)
    assert client.delete(f"/api/settings/mcp-tokens/{created['id']}").json() == {"revoked": True}
    assert next(t for t in client.get("/api/settings/mcp-tokens").json()["tokens"] if t["id"] == created["id"])["revoked"]


# ───────────── tailoring ─────────────

def test_list_jobs_for_tailoring_filters_and_orders(account):
    uid = account["user_id"]
    out = call(uid, "list_jobs_for_tailoring")
    assert [j["company"] for j in out["jobs"]] == ["Grab", "Stripe", "Shopee"]
    assert set(out["jobs"][0]) == {"job_id", "title", "company", "location", "fit_score", "resume_fit_score",
                                   "posted_at", "status", "needs_retailor"}
    assert [j["company"] for j in call(uid, "list_jobs_for_tailoring", minimum_fit_score=85)["jobs"]] == ["Grab", "Stripe"]
    assert [j["company"] for j in call(uid, "list_jobs_for_tailoring", location="singapore")["jobs"]] == ["Grab", "Shopee"]
    assert [j["company"] for j in call(uid, "list_jobs_for_tailoring", company="str")["jobs"]] == ["Stripe"]
    posted_after = (now() - timedelta(days=5)).date().isoformat()
    assert [j["company"] for j in call(uid, "list_jobs_for_tailoring", posted_after=posted_after)["jobs"]] == ["Grab"]
    assert len(call(uid, "list_jobs_for_tailoring", limit=1)["jobs"]) == 1
    assert call(uid, "list_jobs_for_tailoring", posted_after="yesterday")["ok"] is False


def test_other_accounts_never_see_these_jobs(account):
    other = make_user(SessionLocal)
    assert call(other, "list_jobs_for_tailoring")["jobs"] == []
    assert call(other, "get_tailoring_context", job_ids=account["jobs"][:1])["ok"] is False  # no resume on that account


def test_batch_limits_reject_oversized_requests(account, monkeypatch):
    uid = account["user_id"]
    monkeypatch.setattr(get_settings(), "mcp_bulk_tailor_limit", 2)
    monkeypatch.setattr(get_settings(), "mcp_bulk_review_limit", 1)
    assert "MCP_BULK_TAILOR_LIMIT" in call(uid, "list_jobs_for_tailoring", limit=3)["error"]
    assert "MCP_BULK_TAILOR_LIMIT" in call(uid, "get_tailoring_context", job_ids=account["jobs"])["error"]
    oversized = call(uid, "save_bulk_tailored_resumes", results=[tailored(j, account["acme"]) for j in account["jobs"]])
    assert oversized["ok"] is False and "batch maximum of 2" in oversized["error"]
    assert "MCP_BULK_REVIEW_LIMIT" in call(uid, "save_bulk_reviews", reviews=[review(1), review(2)])["error"]
    assert "MCP_BULK_REVIEW_LIMIT" in call(uid, "get_review_context", job_ids=[1, 2])["error"]
    with session(uid) as db:
        assert db.scalars(select(ResumeVariant)).all() == []


def test_tailoring_context_is_compact_and_keeps_evidence_ids(account):
    uid, grab = account["user_id"], account["jobs"][0]
    out = call(uid, "get_tailoring_context", job_ids=[grab, 999999])
    assert out["ok"] and out["rules"] and out["candidate_profile"]["work_authorization"].startswith("Singapore")
    assert [r["company"] for r in out["active_resume"]["roles"]] == ["Acme Corp", "Beta Labs"]
    job = out["jobs"][0]
    assert set(job) == {"job_id", "company", "role", "location", "job_description", "fit_analysis",
                        "relevant_evidence", "recommended_resume_focus", "resume_gaps"}
    assert "internal_note" not in job["fit_analysis"] and job["resume_gaps"] == ["Kafka not on resume"]
    ids = {e["id"] for e in job["relevant_evidence"]}
    assert account["acme"] in ids and account["unverified"] not in ids
    assert out["jobs"][1] == {"job_id": 999999, "error": "Job not found"}


def test_bulk_tailoring_saves_valid_items_and_rejects_invalid_ones(account):
    uid, (grab, stripe, shopee) = account["user_id"], account["jobs"]
    invented = tailored(stripe, account["acme"], experience_sections=[{"role_index": 0, "bullets": [
        {"text": "Scaled the payments API to 90k requests per second", "evidence_ids": [account["acme"]]}]}])
    out = call(uid, "save_bulk_tailored_resumes", results=[tailored(grab, account["acme"]), invented,
                                                           {"job_id": shopee, "summary": 5}])
    assert out["ok"] and out["status"] == "PARTIAL" and (out["saved"], out["rejected"]) == (1, 2)
    first, second, third = out["results"]
    assert first["status"] == "SAVED" and first["version"] == 1 and first["job_status"] == "tailored"
    assert second["status"] == "REJECTED" and {e["code"] for e in second["errors"]} == {"INVENTED_METRIC"}
    assert third["errors"][0]["code"] == "SCHEMA"
    with session(uid) as db:
        variant = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == grab))
        assert variant.reviewer_score is None and variant.content["source"] == "claude_desktop"
        assert variant.content["selected_evidence_ids"] == [account["acme"]]
        assert "25k requests per second" in variant.markdown and "Beta Labs" in variant.markdown
        assert profile_for(db).master_resume == MASTER  # the original resume is never modified
        assert db.get(Job, stripe).status == "shortlisted"


def bullets(role_index, text, *evidence_ids):
    return {"experience_sections": [{"role_index": role_index, "bullets": [{"text": text, "evidence_ids": list(evidence_ids)}]}]}


@pytest.mark.parametrize(("change", "code"), [
    (lambda a: bullets(0, "Owned a 50 person org", "E999999"), "UNKNOWN_EVIDENCE"),
    (lambda a: bullets(1, "Scaled the payments API to 25k requests per second", a["acme"]), "EVIDENCE_ROLE_MISMATCH"),
    (lambda a: bullets(0, "Architected a global ML platform"), "UNCITED_CLAIM"),
    (lambda a: {"skills_order": ["Python", "Rust"]}, "INVENTED_SKILL"),
    (lambda a: bullets(7, "Led a team of 4 engineers"), "UNKNOWN_ROLE"),
    (lambda a: {"cover_letter": "I am a US citizen and need no sponsorship."}, "WORK_AUTHORIZATION"),
    (lambda a: {"resume_bullets": [{"text": "Led a team of 4 engineers delivering payments APIs", "evidence_ids": []}]},
     "LAYOUT"),
])
def test_deterministic_tailoring_checks(account, change, code):
    result = tailored(account["jobs"][0], account["acme"], **change(account))
    out = call(account["user_id"], "save_tailored_resume", result=result)
    assert out["results"][0]["status"] == "REJECTED"
    assert code in {e["code"] for e in out["results"][0]["errors"]}


@pytest.mark.parametrize(("field", "value", "code"), [
    ("summary", "Backend engineer working across AWS, GCP and Azure.", "UNSUPPORTED_TERM"),
    ("cover_letter", "I built the payments platform on Kubernetes at Netflix.", "UNSUPPORTED_TERM"),
    ("summary", "Backend engineer running Python services at 900k requests per second.", "INVENTED_METRIC"),
    ("screening_answer_drafts", {"stack": "I ship on Bedrock and Vertex AI daily."}, "UNSUPPORTED_TERM"),
    ("screening_answer_drafts", {"team": "I have led 63 engineers."}, "INVENTED_METRIC"),
])
def test_the_summary_cover_letter_and_screening_answers_are_grounded_too(account, field, value, code):
    """Prose is read as fact, so its numbers and the names it drops are checked like a bullet's."""
    out = call(account["user_id"], "save_tailored_resume",
               result=tailored(account["jobs"][0], account["acme"], **{field: value}))
    assert out["results"][0]["status"] == "REJECTED"
    assert code in {e["code"] for e in out["results"][0]["errors"]}
    assert out["results"][0]["errors"][0]["location"].startswith(field)


def test_grounded_prose_is_accepted(account):
    """Naming this job, yourself and your own evidence is fine; nothing here needs a retry."""
    grab = account["jobs"][0]
    result = tailored(grab, account["acme"],
                      summary="Senior Engineer at Acme Corp scaling payments with Python and Kafka.",
                      cover_letter="Dear Hiring Manager,\n\nI would love to build at Grab. My work on the "
                                   "payments API at Acme Corp reached 25k requests per second.\n\nSincerely,\n"
                                   "Test Candidate",
                      screening_answer_drafts={"authorization": "Singapore: EP (Employment Pass).",
                                               "stack": "Python, FastAPI and PostgreSQL, with Kafka for streaming."})
    assert call(account["user_id"], "save_tailored_resume", result=result)["results"][0]["status"] == "SAVED"


@pytest.mark.parametrize(("skill", "saved"), [("Go", False), ("Python", True), ("Kafka", True), ("R", False),
                                              ("PostgreSQL", True), ("Vertex AI", False)])
def test_skills_are_matched_as_words_not_as_fragments(account, skill, saved):
    """'Go' must not pass because the corpus says 'Django', and 'R' must not pass on every word with an r."""
    out = call(account["user_id"], "save_tailored_resume",
               result=tailored(account["jobs"][0], account["acme"], skills_order=[skill]))
    result = out["results"][0]
    assert (result["status"] == "SAVED") is saved
    if not saved:
        assert "INVENTED_SKILL" in {e["code"] for e in result["errors"]}


def test_unverified_and_other_account_evidence_ids_are_rejected(account):
    other = make_user(SessionLocal)
    with session(other) as db:
        foreign = Evidence(company="Acme Corp", claim="Scaled the payments API to 25k requests per second")
        db.add(foreign)
        db.commit()
        foreign_id = foreign.id
    for evidence_id in (account["unverified"], foreign_id):
        out = call(account["user_id"], "save_tailored_resume", result=tailored(account["jobs"][0], evidence_id))
        assert "UNKNOWN_EVIDENCE" in {e["code"] for e in out["results"][0]["errors"]}


def test_saving_requires_the_job_to_be_waiting_for_tailoring(account):
    uid, grab = account["user_id"], account["jobs"][0]
    assert call(uid, "save_tailored_resume", result=tailored(grab, account["acme"]))["saved"] == 1
    again = call(uid, "save_bulk_tailored_resumes", results=[tailored(grab, account["acme"])])
    assert again["results"][0]["errors"][0]["code"] == "NOT_IN_QUEUE"
    duplicate = call(uid, "save_bulk_tailored_resumes",
                     results=[tailored(account["jobs"][1], account["acme"])] * 2)
    assert [r["status"] for r in duplicate["results"]] == ["SAVED", "REJECTED"]


# ───────────── review ─────────────

def tailor_all(account, count=3):
    out = call(account["user_id"], "save_bulk_tailored_resumes",
               results=[tailored(j, account["acme"]) for j in account["jobs"][:count]])
    assert out["saved"] == count


def test_list_jobs_for_review_and_review_context(account):
    uid = account["user_id"]
    tailor_all(account)
    listed = call(uid, "list_jobs_for_review")["jobs"]
    assert [j["company"] for j in listed] == ["Grab", "Stripe", "Shopee"]
    assert {"job_id", "title", "company", "fit_score", "tailored_resume_version", "status"} <= set(listed[0])
    assert [j["company"] for j in call(uid, "list_jobs_for_review", minimum_fit_score=85)["jobs"]] == ["Grab", "Stripe"]
    context = call(uid, "get_review_context", job_ids=[listed[0]["job_id"]])
    assert context["candidate_policy"]["min_review_score"] == 75
    assert context["candidate_policy"]["work_authorization"].startswith("Singapore")
    job = context["jobs"][0]
    assert job["tailored_resume"]["version"] == 1 and job["tailored_resume"]["bullet_evidence"]
    assert account["acme"] in {e["id"] for e in job["verified_evidence"]}


def test_bulk_review_decisions_are_made_by_the_backend(account):
    uid, (grab, stripe, shopee) = account["user_id"], account["jobs"]
    tailor_all(account)
    out = call(uid, "save_bulk_reviews", reviews=[
        review(grab, tailored_resume_version=1),
        review(stripe, unsupported_claims=["Kafka at 25k rps is not in the master resume"]),  # PASS overridden
        review(shopee, recommendation="skip"),
    ])
    decisions = {r["job_id"]: r for r in out["results"]}
    assert decisions[grab]["decision"] == "PASS" and decisions[grab]["job_status"] == "tailored"
    assert decisions[grab]["auto_approved"] is False
    assert decisions[stripe]["decision"] == "RETAILOR" and decisions[stripe]["job_status"] == "needs_retailor"
    assert decisions[shopee]["decision"] == "NEEDS_HUMAN" and decisions[shopee]["job_status"] == "pending_approval"
    with session(uid) as db:
        variant = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == grab))
        assert variant.reviewer_score == 92 and variant.reviewer_notes["source"] == "claude_desktop"
    assert call(uid, "list_jobs_for_review")["jobs"] == []
    assert call(uid, "save_review", review=review(grab))["results"][0]["errors"][0]["code"] == "NOT_WAITING"


def test_a_sent_back_packet_is_distinguishable_from_a_job_never_tailored(account):
    """RETAILOR must not look like a fresh shortlist: the job carries needs_retailor and the reason why."""
    uid, (grab, stripe, _shopee) = account["user_id"], account["jobs"]
    tailor_all(account, 2)
    call(uid, "save_review", review=review(grab, unsupported_claims=["GCP is not in the evidence store"]))

    with session(uid) as db:
        assert db.get(Job, grab).status == "needs_retailor"
        assert db.get(Job, stripe).status == "tailored"

    listed = {j["job_id"]: j for j in call(uid, "list_jobs_for_tailoring")["jobs"]}
    assert listed[grab]["status"] == "needs_retailor" and listed[grab]["needs_retailor"] is True
    assert listed[account["jobs"][2]]["needs_retailor"] is False

    context = {j["job_id"]: j for j in call(uid, "get_tailoring_context", job_ids=[grab, account["jobs"][2]])["jobs"]}
    previous = context[grab]["previous_review"]
    assert previous["version"] == 1 and previous["unsupported_claims"] == ["GCP is not in the evidence store"]
    assert previous["decision_reasons"] and "previous_review" not in context[account["jobs"][2]]

    # Re-tailoring it saves version 2 and puts it back in front of the reviewer.
    saved = call(uid, "save_tailored_resume", result=tailored(grab, account["acme"]))["results"][0]
    assert saved["status"] == "SAVED" and saved["version"] == 2 and saved["job_status"] == "tailored"
    assert grab in {j["job_id"] for j in call(uid, "list_jobs_for_review")["jobs"]}


def test_the_workspace_shows_a_sent_back_packet_as_work_to_redo(account, monkeypatch):
    """The Tailor screen and the sidebar count a re-tailor job, and the queue row says why."""
    uid, grab = account["user_id"], account["jobs"][0]
    tailor_all(account, 1)
    call(uid, "save_review", review=review(grab, overstated_claims=["Led the whole platform"]))

    signed_in = CurrentUser(uid, "mcp@jobhunt.test", False)
    monkeypatch.setattr("app.auth.authenticate", lambda _request: signed_in)
    client = TestClient(app)
    row = next(r for r in client.get("/api/tailor/queue").json() if r["id"] == grab)
    assert row["status"] == "needs_retailor" and row["review_decision"] == "RETAILOR" and row["review_reasons"]
    assert [r["id"] for r in client.get("/api/tailor/queue?status=needs_retailor").json()] == [grab]
    assert grab not in [r["id"] for r in client.get("/api/tailor/queue?status=has_packet").json()]
    assert client.get("/api/workspace/summary").json()["counts"]["tailor"] == 3  # two untailored plus this one
    todo = next(a for a in client.get("/api/workspace/today").json()["actions"]
                if a["kind"] == "tailor" and a["job_id"] == grab)
    assert todo["reason"].startswith("Review sent this packet back")
    # The packet itself still opens, with the review's reasons on it.
    packet = client.get(f"/api/tailor/{grab}").json()
    assert packet["job"]["status"] == "needs_retailor" and packet["resume"]["version"] == 1
    assert any(item["tone"] == "bad" for item in packet["resume"]["review"])
    # Discovery can filter for it too, and triage leaves a job that already has a packet alone.
    found = client.get("/api/jobs/search?stage=needs_retailor").json()
    assert [item["id"] for item in found["items"]] == [grab] and found["items"][0]["status"] == "needs_retailor"
    assert client.post(f"/api/jobs/{grab}/triage", json={"action": "maybe"}).status_code == 409
    # Queueing it needs no "regenerate": the packet under review is the one being replaced.
    assert [t["job_id"] for t in client.post("/api/tailor-tasks", json={"ids": [grab]}).json()["queued"]] == [grab]


def test_review_enforces_scores_candidate_policy_and_schema(account):
    uid, (grab, stripe, shopee) = account["user_id"], account["jobs"]
    tailor_all(account)
    with session(uid) as db:
        settings_for(db).application_mode = "automatic"
        profile = profile_for(db)
        profile.search_preferences = {"exclude_companies": ["Stripe"]}
        db.commit()
    out = call(uid, "save_bulk_reviews", reviews=[
        review(grab, review_score=60),
        review(stripe),
        review(shopee, recommendation="APPROVE"),
    ])
    low, blocked, invalid = out["results"]
    assert low["decision"] == "NEEDS_HUMAN" and "below your minimum of 75" in low["reasons"][0]
    assert blocked["decision"] == "NEEDS_HUMAN" and "Candidate policy" in blocked["reasons"][0]
    assert invalid["status"] == "REJECTED" and invalid["errors"][0]["location"] == "recommendation"
    stale = call(uid, "save_review", review=review(shopee, tailored_resume_version=4))
    assert stale["results"][0]["errors"][0]["code"] == "STALE_VERSION"
    approved = call(uid, "save_review", review=review(shopee))
    assert approved["results"][0]["decision"] == "PASS" and approved["results"][0]["auto_approved"] is True


def test_review_reruns_deterministic_evidence_checks(account):
    uid, grab = account["user_id"], account["jobs"][0]
    tailor_all(account, 1)
    with session(uid) as db:  # the evidence behind a saved bullet is withdrawn before review
        db.get(Evidence, account["acme"]).verified = False
        db.commit()
    result = call(uid, "save_review", review=review(grab))["results"][0]
    assert result["decision"] == "RETAILOR" and result["reasons"][0].startswith("UNKNOWN_EVIDENCE")


# ───────────── audit trail ─────────────

def test_every_tool_call_is_audited_without_content(account):
    uid, grab = account["user_id"], account["jobs"][0]
    call(uid, "list_jobs_for_tailoring", minimum_fit_score=85)
    saved = call(uid, "save_bulk_tailored_resumes", results=[tailored(grab, account["acme"])])
    call(uid, "get_tailoring_context", job_ids=list(range(50)))  # rejected: too many
    with session(uid) as db:
        rows = db.scalars(select(ConversationAction).order_by(ConversationAction.id)).all()
    assert [r.tool_name for r in rows] == ["list_jobs_for_tailoring", "save_bulk_tailored_resumes", "get_tailoring_context"]
    assert all(r.source == "CLAUDE_DESKTOP" for r in rows)
    assert rows[0].arguments_summary == {"minimum_fit_score": 85} and rows[0].result_status == "OK"
    assert rows[1].operation_id == saved["operation_id"] and rows[1].result_summary["items"][0]["status"] == "SAVED"
    assert rows[1].arguments_summary == {"results": {"count": 1, "job_ids": [grab]}}
    assert "25k" not in str(rows[1].arguments_summary) and rows[2].result_status == "ERROR"

    status = call(uid, "get_bulk_operation_status")
    assert status["operation_id"] == saved["operation_id"] and status["items"][0]["job_status_now"] == "tailored"
    assert call(uid, "get_bulk_operation_status", operation_id="nope")["ok"] is False


# ───────────── model market ─────────────

def test_model_market_tools_are_registered_read_only_and_audited_without_an_entity(account, monkeypatch):
    import httpx

    from app.services import model_market
    from tests.test_model_market import OLLAMA_CLOUD, OPENROUTER

    def catalogs(request):
        if "openrouter.ai" in str(request.url):
            return httpx.Response(200, json=OPENROUTER)
        if "ollama.com" in str(request.url):
            return httpx.Response(200, json=OLLAMA_CLOUD)
        return httpx.Response(503)

    model_market.reset_cache()
    monkeypatch.setattr(model_market, "_transport", httpx.MockTransport(catalogs))
    monkeypatch.setattr(get_settings(), "ollama_base_url", "")
    uid = account["user_id"]
    server = build_server(uid)
    tools_by_name = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert {"list_model_sources", "search_models", "suggest_models", "get_model_details"} <= set(tools_by_name)
    assert {"worker_type", "objective", "max_cost_per_request", "in_gateway_only"} <= set(
        tools_by_name["suggest_models"].input_schema["properties"])

    sources = call(uid, "list_model_sources")
    by_name = {s["source"]: s for s in sources["sources"]}
    assert by_name["openrouter"]["models"] == 3 and by_name["litellm_prices"]["error"] == "answered HTTP 503"
    found = call(uid, "search_models", query="gpt-oss", limit=5)
    assert found["ok"] and {m["id"] for m in found["models"]} == {"openrouter/openai/gpt-oss-120b",
                                                                 "ollama_cloud/gpt-oss:120b"}
    suggested = call(uid, "suggest_models", worker_type="ranker", objective="cheapest", limit=3)
    assert suggested["ok"] and suggested["worker_type"] == "RANKER" and suggested["requirements"]["structured_output"]
    assert "Suggestions only" in suggested["note"]
    assert call(uid, "suggest_models", objective="best")["ok"] is False
    assert call(uid, "suggest_models", worker_type="NOPE")["ok"] is False
    assert call(uid, "search_models", limit=500)["ok"] is False
    assert call(uid, "get_model_details", model_id="openrouter/openai/gpt-oss-120b")["count"] == 1
    assert call(uid, "get_model_details", model_id="nothing-like-it")["ok"] is False
    with session(uid) as db:
        rows = db.scalars(select(ConversationAction).where(ConversationAction.tool_name.in_(
            ("list_model_sources", "search_models", "suggest_models", "get_model_details")))).all()
    assert len(rows) == 8 and all(r.entity_type is None and r.entity_id is None for r in rows)
    model_market.reset_cache()


# ───────────── execution modes ─────────────

def test_backend_mode_still_tailors_and_reviews_with_the_model(monkeypatch):
    monkeypatch.setattr(llm.LLMClient, "generate_prompt_json", MODEL_CALL)  # the backend path calls the (mock) model
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    client = TestClient(app)
    use_resume_text(client, "Built Python services", name="Test Candidate")
    assert client.patch("/api/settings", json={"execution_mode": "backend"}).json()["execution_mode"] == "backend"
    job = client.post("/api/jobs/from-url", json={"url": f"https://example.com/jobs/{uuid.uuid4().hex}",
                                                   "title": "Engineer", "company": "Acme", "description": "Python"}).json()["job"]
    client.post(f"/api/jobs/{job['id']}/triage", json={"action": "shortlist"})
    assert len(client.post("/api/tailor-tasks", json={"ids": [job["id"]]}).json()["queued"]) == 1
    tailor_tasks.run_pending(SessionLocal)
    packet = client.get(f"/api/tailor/{job['id']}").json()["resume"]
    assert packet["version"] == 1 and packet["reviewer_score"] == 86

    # Claude Desktop mode: the background workers take no tailoring work.
    assert client.patch("/api/settings", json={"execution_mode": "claude_desktop"}).json()["execution_mode"] == "claude_desktop"
    other = client.post("/api/jobs/from-url", json={"url": f"https://example.com/jobs/{uuid.uuid4().hex}",
                                                     "title": "Engineer", "company": "Beta", "description": "Python"}).json()["job"]
    client.post(f"/api/jobs/{other['id']}/triage", json={"action": "shortlist"})
    skipped = client.post("/api/tailor-tasks", json={"ids": [other["id"]]}).json()["skipped"]
    assert skipped == [{"id": other["id"], "reason": tailor_tasks.DESKTOP_MODE_REASON}]
    # A task queued while the backend was in charge is cancelled rather than run with the model API.
    queued = client.post("/api/jobs/from-url", json={"url": f"https://example.com/jobs/{uuid.uuid4().hex}",
                                                     "title": "Engineer", "company": "Gamma", "description": "Python"}).json()["job"]
    client.post(f"/api/jobs/{queued['id']}/triage", json={"action": "shortlist"})
    client.patch("/api/settings", json={"execution_mode": "backend"})
    assert len(client.post("/api/tailor-tasks", json={"ids": [queued["id"]]}).json()["queued"]) == 1
    client.patch("/api/settings", json={"execution_mode": "claude_desktop"})
    monkeypatch.setattr(llm.LLMClient, "generate_prompt_json", _refuse)
    tailor_tasks.run_pending(SessionLocal)
    task = next(t for t in client.get("/api/tailor-tasks").json() if t["job_id"] == queued["id"])
    assert task["status"] == "cancelled" and task["error"] == tailor_tasks.DESKTOP_MODE_REASON
    assert client.get(f"/api/tailor/{queued['id']}").json()["resume"] is None

    assert client.patch("/api/settings", json={"execution_mode": "cloud"}).status_code == 422
    assert client.patch("/api/settings", json={"min_review_score": 70}).json()["execution_mode"] == "claude_desktop"
    client.patch("/api/settings", json={"execution_mode": "backend"})
