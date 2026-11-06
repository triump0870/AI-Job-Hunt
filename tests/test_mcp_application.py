"""Claude Desktop applying over MCP: the ready queue, packets, sourced answers, questions for a person, results."""
import asyncio
import base64
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.mcp import tools
from app.mcp.server import build_server, run_tool
from app.models import (
    Application,
    ApplicationEvent,
    ConversationAction,
    Evidence,
    Job,
    KnowledgeAnswer,
    ResumeVariant,
)
from app.services import llm
from app.services.knowledge import question_key
from app.services.user_settings import profile_for
from app.settings import get_settings
from tests.conftest import ensure_test_user, make_user

RESUME = """# Test Candidate
test@example.com

## Experience
### Senior Engineer | Acme Corp | 2021 - Present
- Scaled the payments API to 25k requests per second with Python and Kafka
"""


def now():
    return datetime.now(UTC).replace(tzinfo=None)


def session(user_id):
    db = SessionLocal()
    db.info["user_id"] = user_id
    return db


def call(user_id, name, **arguments):
    return run_tool(SessionLocal, user_id, name, getattr(tools, name), arguments)


@pytest.fixture(autouse=True)
def no_model_calls(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("Applying over MCP must not call the LLM layer")

    monkeypatch.setattr(llm.LLMClient, "generate_prompt_json", refuse)


@pytest.fixture(autouse=True)
def packet_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "mcp_packet_dir", str(tmp_path / "packets"))
    return tmp_path / "packets"


def _job(db, company, *, reviewer_score, status="approved", applied=False, fit=80, **fields):
    job = Job(source="manual", source_ref=uuid.uuid4().hex, title="Senior Backend Engineer", company=company,
              location="Singapore", url=f"https://jobs.example.test/{company.lower()}", description="Python",
              fit_score=fit, status="applied" if applied else status, triaged_at=now(), **fields)
    db.add(job)
    db.flush()
    variant = ResumeVariant(job_id=job.id, version=2, markdown=RESUME, cover_letter=f"Dear {company},",
                            content={"summary": "Backend engineer", "screening_answer_drafts": {"why": "Payments"}},
                            reviewer_score=reviewer_score)
    db.add(variant)
    db.flush()
    if status == "approved" or applied:
        db.add(Application(job_id=job.id, status="applied" if applied else "approved", approved_at=now(),
                           applied_at=now() if applied else None, approved_resume_id=variant.id))
    return job


@pytest.fixture
def account():
    user_id = make_user(SessionLocal)
    with session(user_id) as db:
        profile = profile_for(db)
        profile.name, profile.master_resume = "Test Candidate", RESUME
        profile.email, profile.phone = "test@example.com", "+65 5555 0100"
        profile.work_authorization = "Singapore: EP (Employment Pass)"
        kafka = Evidence(company="Acme Corp", role="Senior Engineer", skills=["Python", "Kafka"],
                         claim="Scaled the payments API to 25k requests per second with Python and Kafka")
        unverified = Evidence(company="Acme Corp", claim="Ran a Kafka platform for 300 teams", skills=["Kafka"],
                              verified=False)
        db.add_all([kafka, unverified])
        grab = _job(db, "Grab", reviewer_score=92, fit=70, direct_apply_url="https://grab.example.test/apply/1")
        stripe = _job(db, "Stripe", reviewer_score=61, fit=95)
        _job(db, "Shopee", reviewer_score=60)  # not above 60
        _job(db, "Sea", reviewer_score=95, status="tailored")  # not approved
        applied = _job(db, "Gojek", reviewer_score=88, applied=True, canonical_apply_url="https://gojek.example.test/a/9")
        # The same Gojek posting found again on another board: already applied to, so never offered.
        dupe = _job(db, "Gojek", reviewer_score=90, canonical_apply_url="https://gojek.example.test/a/9")
        notice = KnowledgeAnswer(question="What is your notice period?", question_key=question_key("What is your notice period?"),
                                 answer="30 days", category="notice_period", verified=True)
        gender = KnowledgeAnswer(question="What is your gender?", question_key=question_key("What is your gender?"),
                                 answer="Prefer not to say", category="demographic", verified=True, sensitive=True)
        db.add_all([notice, gender])
        db.commit()
        yield {"user_id": user_id, "grab": grab.id, "stripe": stripe.id, "applied": applied.id, "dupe": dupe.id,
               "kafka": kafka.id, "unverified": unverified.id, "notice": notice.id}


# ───────────── list_jobs_for_application ─────────────

def test_lists_only_approved_unapplied_jobs_reviewed_above_60(account):
    out = call(account["user_id"], "list_jobs_for_application")
    assert out["ok"] and out["reviewer_score_above"] == 60 and out["total"] == 2
    assert [j["company"] for j in out["jobs"]] == ["Grab", "Stripe"]  # best reviewed first, not best fit
    grab = out["jobs"][0]
    assert grab["apply_url"] == "https://grab.example.test/apply/1"
    assert grab["posting_url"] == "https://jobs.example.test/grab"
    assert grab["reviewer_score"] == 92 and grab["resume_version"] == 2 and grab["blocker"] is None


def test_pagination_and_raising_the_threshold(account):
    uid = account["user_id"]
    first = call(uid, "list_jobs_for_application", limit=1)
    assert [j["company"] for j in first["jobs"]] == ["Grab"] and first["next_offset"] == 1
    second = call(uid, "list_jobs_for_application", limit=1, offset=first["next_offset"])
    assert [j["company"] for j in second["jobs"]] == ["Stripe"] and second["next_offset"] is None
    assert [j["company"] for j in call(uid, "list_jobs_for_application", reviewer_score_above=90)["jobs"]] == ["Grab"]
    assert call(uid, "list_jobs_for_application", company="stri")["total"] == 1
    lowered = call(uid, "list_jobs_for_application", reviewer_score_above=50)
    assert lowered["ok"] is False and "cannot be lower than 60" in lowered["error"]
    assert call(uid, "list_jobs_for_application", limit=500)["ok"] is False
    assert call(uid, "list_jobs_for_application", offset=-1)["ok"] is False


def test_other_accounts_see_none_of_these_jobs(account):
    other = make_user(SessionLocal)
    assert call(other, "list_jobs_for_application")["total"] == 0
    assert call(other, "get_job_packet", job_id=account["grab"])["ok"] is False
    assert call(other, "record_application_result", job_id=account["grab"], outcome="submitted")["ok"] is False
    with session(account["user_id"]) as db:
        assert db.get(Job, account["grab"]).status == "approved"


# ───────────── get_job_packet ─────────────

def test_packet_is_the_approved_version_as_uploadable_files(account, packet_dir):
    out = call(account["user_id"], "get_job_packet", job_id=account["grab"])
    assert out["ok"] and out["resume"]["version"] == 2 and out["resume"]["reviewer_score"] == 92
    assert out["cover_letter"] == "Dear Grab," and out["screening_answer_drafts"] == {"why": "Payments"}
    assert out["apply_url"] == "https://grab.example.test/apply/1" and out["rules"]
    files = {f["format"]: f for f in out["files"] if f["role"] == "resume"}
    assert set(files) == {"pdf", "docx"}
    assert files["pdf"]["filename"] == "test-candidate-grab-v2.pdf" and files["docx"]["filename"] == "test-candidate-grab-v2.docx"
    pdf = Path(files["pdf"]["path"])
    assert pdf.is_relative_to(packet_dir.resolve()) and pdf.read_bytes().startswith(b"%PDF")
    assert Path(files["docx"]["path"]).read_bytes().startswith(b"PK")  # a zip: Office Open XML
    assert files["pdf"]["size_bytes"] == pdf.stat().st_size and "content_base64" not in files["pdf"]

    inline = call(account["user_id"], "get_job_packet", job_id=account["grab"], formats=["docx"], include_content=True)
    docx, letter = inline["files"]
    assert docx["format"] == "docx" and base64.b64decode(docx["content_base64"]).startswith(b"PK")
    assert docx["role"] == "resume" and letter["role"] == "cover_letter" and letter["format"] == "docx"
    assert call(account["user_id"], "get_job_packet", job_id=account["grab"], formats=["exe"])["ok"] is False


def test_packet_uses_the_approved_version_not_a_newer_draft(account):
    with session(account["user_id"]) as db:
        db.add(ResumeVariant(job_id=account["grab"], version=3, markdown="# Newer draft", reviewer_score=None))
        db.commit()
    out = call(account["user_id"], "get_job_packet", job_id=account["grab"])
    assert out["resume"]["version"] == 2 and "Newer draft" not in out["resume"]["markdown"]


@pytest.mark.parametrize("which, message", [("applied", "Already applied"), ("dupe", "same posting as job")])
def test_no_packet_for_a_job_already_applied_to(account, which, message):
    out = call(account["user_id"], "get_job_packet", job_id=account[which])
    assert out["ok"] is False and message in out["error"]


def test_no_packet_below_the_reviewer_floor_or_before_approval(account):
    uid = account["user_id"]
    with session(uid) as db:
        shopee = db.scalar(select(Job).where(Job.company == "Shopee")).id
        sea = db.scalar(select(Job).where(Job.company == "Sea")).id
    assert "not above 60" in call(uid, "get_job_packet", job_id=shopee)["error"]
    assert "not approved" in call(uid, "get_job_packet", job_id=sea)["error"]
    assert "not found" in call(uid, "get_job_packet", job_id=10**9)["error"]


# ───────────── search_application_knowledge ─────────────

def test_a_verified_saved_answer_answers_the_question(account):
    out = call(account["user_id"], "search_application_knowledge", question="What's your notice period?")
    assert out["status"] == "ANSWERED" and out["answer"] == "30 days"
    assert out["answer_source"] == f"knowledge_answer:{account['notice']}"


def test_a_similar_question_never_borrows_another_questions_answer(account):
    """Python and Java questions overlap 0.6 in content words; the Python answer must never answer Java."""
    uid = account["user_id"]
    python = "How many years of Python experience do you have?"
    with session(uid) as db:
        db.add(KnowledgeAnswer(question=python, question_key=question_key(python), answer="6", verified=True))
        db.commit()
    java = call(uid, "search_application_knowledge", question="How many years of Java experience do you have?")
    assert java["status"] == "UNKNOWN" and java["answer"] is None and java["knowledge_answers"] == []
    similar = java["similar_questions"][0]
    assert similar["question"] == python and similar["answered"] is True and "answer" not in similar
    reworded = call(uid, "search_application_knowledge", question="Years of Python experience?")
    assert reworded["status"] == "ANSWERED" and reworded["answer"] == "6"  # only filler words differ
    asked = call(uid, "request_human_answer", job_id=account["grab"], question="How many years of Java experience do you have?")
    assert asked["status"] == "SAVED"
    again = call(uid, "request_human_answer", job_id=account["grab"], question="What's your notice period?")
    assert again["status"] == "ALREADY_ANSWERED" and again["question_id"] == account["notice"]


def test_sensitive_answers_need_confirmation_and_are_not_returned(account):
    out = call(account["user_id"], "search_application_knowledge", question="What is your gender?")
    assert out["status"] == "NEEDS_CONFIRMATION" and out["answer"] is None
    assert "Prefer not to say" not in str(out)


def test_contact_details_answer_and_other_facts_only_support(account):
    uid = account["user_id"]
    phone = call(uid, "search_application_knowledge", question="Phone number")
    assert phone["status"] == "ANSWERED" and phone["answer"] == "+65 5555 0100"
    assert phone["answer_source"] == "candidate_profile.phone"
    visa = call(uid, "search_application_knowledge", question="Will you now or in the future require visa sponsorship?")
    assert visa["status"] == "SUPPORTED" and visa["answer"] is None
    assert visa["profile_facts"][0]["value"] == "Singapore: EP (Employment Pass)"


def test_verified_evidence_supports_experience_questions(account):
    out = call(account["user_id"], "search_application_knowledge", question="Describe your experience with Kafka")
    assert out["status"] == "SUPPORTED" and out["answer"] is None
    assert [e["id"] for e in out["evidence"]] == [account["kafka"]]  # never the unverified claim
    assert out["evidence"][0]["source"] == f"evidence:{account['kafka']}"


def test_nothing_known_is_unknown(account):
    out = call(account["user_id"], "search_application_knowledge", question="What is your favourite colour?")
    assert out["status"] == "UNKNOWN" and out["answer"] is None
    assert not (out["knowledge_answers"] or out["evidence"] or out["profile_facts"])
    assert call(account["user_id"], "search_application_knowledge", question=" ")["ok"] is False


# ───────────── request_human_answer ─────────────

def test_a_question_is_saved_once_with_the_job_and_the_form_options(account):
    uid = account["user_id"]
    question = "Are you willing to work from the Jakarta office 3 days a week?"
    saved = call(uid, "request_human_answer", job_id=account["grab"], question=question,
                 answer_options=["Yes", "No", "Yes", " "], required=True, field_label="Hybrid")
    assert saved["ok"] and saved["status"] == "SAVED" and saved["answer_options"] == ["Yes", "No"]
    assert saved["answer_type"] == "choice"
    again = call(uid, "request_human_answer", job_id=account["stripe"], question=question.upper())
    assert again["status"] == "ALREADY_REQUESTED" and again["question_id"] == saved["question_id"]
    assert again["job_ids"] == [account["grab"], account["stripe"]]
    with session(uid) as db:
        row = db.get(KnowledgeAnswer, saved["question_id"])
        assert row.answer == "" and not row.verified and row.source_job_id == account["grab"]
        assert row.answer_options == ["Yes", "No"]
    searched = call(uid, "search_application_knowledge", question=question)
    assert searched["status"] == "PENDING_HUMAN" and searched["pending_questions"][0]["id"] == saved["question_id"]


def test_the_knowledge_bank_shows_which_job_asked_and_the_options():
    uid = ensure_test_user().id  # the account the API client acts as
    with session(uid) as db:
        job = _job(db, "Globex", reviewer_score=80)
        db.commit()
        job_id = job.id
    question = f"Do you hold a {uuid.uuid4().hex[:6]} certification?"
    call(uid, "request_human_answer", job_id=job_id, question=question, answer_options=["Yes", "No"], required=True)
    listed = next(q for q in TestClient(app).get("/api/knowledge").json() if q["question"] == question)
    assert listed["answer"] == "" and not listed["verified"] and listed["answer_options"] == ["Yes", "No"]
    assert listed["asked_by"]["job_id"] == job_id and listed["asked_by"]["company"] == "Globex"
    assert listed["asked_by"]["required"] is True


def test_an_answered_question_is_not_reopened(account):
    uid = account["user_id"]
    out = call(uid, "request_human_answer", job_id=account["grab"], question="What is your notice period?")
    assert out["status"] == "ALREADY_ANSWERED" and out["answer"] == "30 days"
    sensitive = call(uid, "request_human_answer", job_id=account["grab"], question="What is your gender?")
    assert sensitive["status"] == "NEEDS_CONFIRMATION" and "answer" not in sensitive
    assert call(uid, "request_human_answer", job_id=account["grab"], question="Q?", answer_type="essay")["ok"] is False
    assert call(uid, "request_human_answer", job_id=account["grab"], question="Pick", answer_options="Yes")["ok"] is False


# ───────────── record_application_result ─────────────

def test_a_submission_is_recorded_once(account):
    uid, grab = account["user_id"], account["grab"]
    out = call(uid, "record_application_result", job_id=grab, outcome="submitted", confirmation_id="GRAB-12345")
    assert out["ok"] and out["status"] == "SUBMITTED" and out["job_status"] == "applied"
    assert out["confirmation_id"] == "GRAB-12345" and out["followup_due_at"]
    again = call(uid, "record_application_result", job_id=grab, outcome="submitted", confirmation_id="OTHER")
    assert again["status"] == "ALREADY_APPLIED" and again["confirmation_id"] == "GRAB-12345"
    with session(uid) as db:
        application = db.scalar(select(Application).where(Application.job_id == grab))
        assert application.status == "applied" and application.submitted_via == "claude_desktop"
        events = db.scalars(select(ApplicationEvent).where(ApplicationEvent.application_id == application.id)).all()
        assert [e.kind for e in events] == ["applied"]
        assert "Claude Desktop" in events[0].detail
        assert events[0].meta["confirmation_id"] == "GRAB-12345"
        assert events[0].meta["claim"] == "unclaimed" and events[0].meta["client"] == "mcp"
    assert [j["company"] for j in call(uid, "list_jobs_for_application")["jobs"]] == ["Stripe"]
    assert call(uid, "get_job_packet", job_id=grab)["ok"] is False


def test_a_duplicate_posting_is_never_recorded_as_a_second_application(account):
    uid = account["user_id"]
    out = call(uid, "record_application_result", job_id=account["dupe"], outcome="submitted")
    assert out["ok"] is False and out["status"] == "DUPLICATE_APPLICATION"
    assert out["duplicate_of_job_id"] == account["applied"]
    with session(uid) as db:
        assert db.get(Job, account["dupe"]).status == "approved"


def test_a_blocker_hides_the_job_until_the_person_answers(account):
    uid, grab = account["user_id"], account["grab"]
    asked = call(uid, "request_human_answer", job_id=grab, question="Expected start date?", required=True)
    blocked = call(uid, "record_application_result", job_id=grab, outcome="blocked", blocker_reason="needs_human_answer",
                   detail="Required start date question", question_ids=[asked["question_id"]])
    assert blocked["status"] == "BLOCKED" and blocked["job_status"] == "approved"
    assert blocked["blocker"]["question_ids"] == [asked["question_id"]]
    assert [j["company"] for j in call(uid, "list_jobs_for_application")["jobs"]] == ["Stripe"]
    shown = call(uid, "list_jobs_for_application", include_blocked=True)["jobs"]
    assert shown[0]["company"] == "Grab" and shown[0]["blocker"]["reason"] == "needs_human_answer"

    with session(uid) as db:  # the person answers it in the Knowledge bank
        row = db.get(KnowledgeAnswer, asked["question_id"])
        row.answer, row.verified = "Two weeks after an offer", True
        db.commit()
    assert [j["company"] for j in call(uid, "list_jobs_for_application")["jobs"]] == ["Grab", "Stripe"]
    assert call(uid, "search_application_knowledge", question="Expected start date?")["answer"] == "Two weeks after an offer"
    done = call(uid, "record_application_result", job_id=grab, outcome="submitted")
    with session(uid) as db:
        assert done["status"] == "SUBMITTED" and db.get(Job, grab).application.apply_blocker is None


def test_a_sensitive_question_keeps_its_blocker_open(account):
    """A saved sensitive answer is confirmed per application, so it never clears a blocker by itself."""
    uid, grab = account["user_id"], account["grab"]
    asked = call(uid, "request_human_answer", job_id=grab, question="What is your gender?")
    assert asked["status"] == "NEEDS_CONFIRMATION"
    call(uid, "record_application_result", job_id=grab, outcome="blocked", blocker_reason="needs_human_answer",
         question_ids=[asked["question_id"]])
    assert [j["company"] for j in call(uid, "list_jobs_for_application")["jobs"]] == ["Stripe"]
    blocked = call(uid, "list_jobs_for_application", include_blocked=True)["jobs"][0]
    assert blocked["company"] == "Grab" and blocked["blocker"]["reason"] == "needs_human_answer"


def test_a_job_without_an_application_url_is_not_ready(account):
    uid = account["user_id"]
    with session(uid) as db:
        stripe = db.get(Job, account["stripe"])
        stripe.url = ""
        db.commit()
    assert [j["company"] for j in call(uid, "list_jobs_for_application")["jobs"]] == ["Grab"]
    refused = call(uid, "get_job_packet", job_id=account["stripe"])
    assert refused["ok"] is False and "no application URL" in refused["error"]


def test_blockers_are_validated(account):
    uid, grab = account["user_id"], account["grab"]
    assert call(uid, "record_application_result", job_id=grab, outcome="maybe")["ok"] is False
    assert call(uid, "record_application_result", job_id=grab, outcome="blocked", blocker_reason="bored")["ok"] is False
    assert call(uid, "record_application_result", job_id=grab, outcome="blocked",
                blocker_reason="needs_human_answer")["ok"] is False  # which questions?
    captcha = call(uid, "record_application_result", job_id=grab, outcome="blocked", blocker_reason="captcha")
    assert captcha["ok"] and call(uid, "list_jobs_for_application")["total"] == 1


# ───────────── server and audit ─────────────

def test_tools_are_served_and_audited_without_content(account):
    server = build_server(account["user_id"])
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {"list_jobs_for_application", "get_job_packet", "search_application_knowledge", "request_human_answer",
            "record_application_result"} <= names
    content = asyncio.run(server.call_tool("list_jobs_for_application", {"limit": 5}))
    assert "Grab" in str(content)
    assert "list_jobs_for_application" in asyncio.run(server.get_prompt("apply_to_jobs", {"limit": "3"})).messages[0].content.text
    call(account["user_id"], "request_human_answer", job_id=account["grab"], question="Secret project codename?")
    call(account["user_id"], "record_application_result", job_id=account["grab"], outcome="submitted",
         confirmation_id="GRAB-1")
    with session(account["user_id"]) as db:
        rows = db.scalars(select(ConversationAction).order_by(ConversationAction.id)).all()
    assert [r.tool_name for r in rows] == ["list_jobs_for_application", "request_human_answer", "record_application_result"]
    assert [r.result_status for r in rows] == ["OK", "OK", "OK"]
    assert rows[1].arguments_summary["question"] == {"length": len("Secret project codename?")}
    assert rows[2].entity_id == account["grab"] and rows[2].result_summary == {"outcome": "SUBMITTED"}
    assert rows[2].arguments_summary["outcome"] == "submitted"


# ───────────── routes: who applies ─────────────

def _set_url(user_id, job_id, url):
    with session(user_id) as db:
        job = db.get(Job, job_id)
        job.direct_apply_url, job.url = url, url
        db.commit()


@pytest.mark.parametrize("url, ats, who", [
    ("https://www.linkedin.com/jobs/view/4465457931", "linkedin", "person"),
    ("https://www.indeed.com/viewjob?jk=abc", "indeed", "person"),
    ("https://jobs.ashbyhq.com/acme/123", "ashby", "person"),
    ("https://boards.greenhouse.io/grab/jobs/123", "greenhouse", "claude"),
    ("https://careers.grab.example.test/apply/1", "generic", "claude"),
])
def test_each_job_says_who_applies(account, url, ats, who):
    uid = account["user_id"]
    _set_url(uid, account["grab"], url)
    listed = next(j for j in call(uid, "list_jobs_for_application")["jobs"] if j["job_id"] == account["grab"])
    route = listed["route"]
    assert route["ats"] == ats and route["who_applies"] == who and route["reason"]
    assert route["apply_domain"] == url.split("/")[2]
    if who == "person":
        assert "candidate applies" in route["reason"]
    assert call(uid, "get_job_packet", job_id=account["grab"])["route"] == route


def test_jobs_can_be_listed_by_who_applies(account):
    uid = account["user_id"]
    _set_url(uid, account["grab"], "https://www.linkedin.com/jobs/view/1")
    person = call(uid, "list_jobs_for_application", who_applies="person")
    assert [j["company"] for j in person["jobs"]] == ["Grab"] and person["total"] == 1
    claude = call(uid, "list_jobs_for_application", who_applies="claude")
    assert [j["company"] for j in claude["jobs"]] == ["Stripe"]
    assert call(uid, "list_jobs_for_application", who_applies="robot")["ok"] is False


def test_the_application_rules_leave_linkedin_indeed_and_ashby_to_the_person():
    from app.prompts.mcp_application import (
        APPLICATION_RULES,
        MCP_APPLICATION_PROMPT_VERSION,
        build_mcp_application_prompt,
    )

    assert MCP_APPLICATION_PROMPT_VERSION == "v3"
    rule = next(r for r in APPLICATION_RULES if "who_applies" in r)
    assert all(name in rule for name in ("LinkedIn", "Indeed", "Ashby"))
    assert "who_applies is person" in build_mcp_application_prompt(3)


# ───────────── cover letter files ─────────────

def _pdf_text(path):
    from pypdf import PdfReader

    return " ".join(" ".join((page.extract_text() or "").split()) for page in PdfReader(path).pages)


def _docx_text(path):
    from docx import Document

    return " ".join(" ".join(p.text.split()) for p in Document(path).paragraphs if p.text.strip())


def test_the_approved_cover_letter_comes_as_pdf_and_docx(account, packet_dir):
    uid = account["user_id"]
    letter = "Dear Grab team,\n\nI scaled a payments API to 25k requests per second.\n\nKind regards,\nTest Candidate"
    with session(uid) as db:
        variant = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == account["grab"]))
        variant.cover_letter = letter
        db.commit()
    out = call(uid, "get_job_packet", job_id=account["grab"])
    assert out["ok"] and out["cover_letter"] == letter
    letters = {f["format"]: f for f in out["files"] if f["role"] == "cover_letter"}
    assert set(letters) == {"pdf", "docx"}
    assert letters["pdf"]["filename"] == "cover-letter-test-candidate-grab.pdf"
    assert letters["docx"]["filename"] == "cover-letter-test-candidate-grab.docx"
    folder = Path(next(f for f in out["files"] if f["role"] == "resume")["path"]).parent
    assert Path(letters["pdf"]["path"]).parent == folder  # beside the resume files
    expected = " ".join(letter.split())
    assert _pdf_text(letters["pdf"]["path"]) == expected
    assert _docx_text(letters["docx"]["path"]) == expected
    # Not asked for: only the resume files.
    plain = call(uid, "get_job_packet", job_id=account["grab"], cover_letter_files=False)
    assert {f["role"] for f in plain["files"]} == {"resume"}


def test_no_cover_letter_files_when_the_letter_is_empty(account):
    uid = account["user_id"]
    with session(uid) as db:
        variant = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == account["grab"]))
        variant.cover_letter = "   "
        db.commit()
    out = call(uid, "get_job_packet", job_id=account["grab"])
    assert out["ok"] and out["cover_letter"] == ""
    assert [f["role"] for f in out["files"]] == ["resume", "resume"]


def test_the_apply_screen_still_downloads_the_letter_as_text():
    from app.services.exports import packet_filename

    assert packet_filename("a-b", "cover_letter") == "cover-letter-a-b.txt"
    assert packet_filename("a-b", "cover_letter_pdf") == "cover-letter-a-b.pdf"
    assert packet_filename("a-b", "pdf", 3, draft=True) == "a-b-v3-draft.pdf"


# ───────────── find_jobs ─────────────

def test_find_jobs_by_company_or_title_at_any_stage(account):
    uid = account["user_id"]
    out = call(uid, "find_jobs", query="gojek")
    assert out["ok"] and out["count"] == 2 and {j["job_id"] for j in out["jobs"]} == {account["applied"], account["dupe"]}
    applied = next(j for j in out["jobs"] if j["job_id"] == account["applied"])
    assert applied["applied"] is True and applied["application_id"] and applied["tailored_versions"] == 1
    assert applied["latest_version"] == 2 and applied["reviewer_score"] == 88
    assert set(applied) == {"job_id", "title", "company", "location", "status", "state", "fit_score",
                            "tailored_versions", "latest_version", "reviewer_score", "approved", "applied",
                            "application_id", "apply_url"}  # no description, no resume text
    sea = call(uid, "find_jobs", query="SEA")["jobs"]
    assert [j["company"] for j in sea] == ["Sea"] and sea[0]["approved"] is False and sea[0]["application_id"] is None
    assert call(uid, "find_jobs", query="backend", stage="tailored")["jobs"][0]["company"] == "Sea"
    assert call(uid, "find_jobs", query="backend", limit=2)["count"] == 2
    assert call(uid, "find_jobs", job_id=account["grab"])["jobs"][0]["approved"] is True
    assert call(uid, "find_jobs")["ok"] is False
    assert call(uid, "find_jobs", query="x", limit=10**6)["ok"] is False


def test_find_jobs_never_returns_another_accounts_job(account):
    other = make_user(SessionLocal)
    assert call(other, "find_jobs", query="Grab")["jobs"] == []
    assert call(other, "find_jobs", job_id=account["grab"])["jobs"] == []
    assert call(other, "get_job_documents", job_id=account["grab"])["ok"] is False


# ───────────── get_job_documents ─────────────

def test_documents_of_an_approved_job_are_the_packet(account, packet_dir):
    out = call(account["user_id"], "get_job_documents", job_id=account["grab"])
    assert out["ok"] and out["status"] == "APPROVED" and out["approved"] and out["may_submit"]
    assert out["version"] == 2 and out["cover_letter"] == "Dear Grab," and "note" not in out
    assert all("drafts" not in f["path"] and "-draft" not in f["filename"] for f in out["files"])


def test_documents_of_an_unapproved_job_are_drafts_and_may_not_be_submitted(account, packet_dir):
    uid = account["user_id"]
    with session(uid) as db:
        sea = db.scalar(select(Job).where(Job.company == "Sea"))
        db.add(ResumeVariant(job_id=sea.id, version=3, markdown=RESUME, cover_letter="Dear Sea,", reviewer_score=None))
        db.commit()
        sea_id = sea.id
    out = call(uid, "get_job_documents", job_id=sea_id)
    assert out["ok"] and out["status"] == "TAILORED" and out["version"] == 3  # the latest version
    assert out["approved"] is False and out["may_submit"] is False and out["not_ready_reason"] and out["note"]
    assert out["cover_letter"] == "Dear Sea," and out["markdown"] == RESUME
    drafts = (packet_dir / str(uid) / str(sea_id) / "drafts").resolve()
    assert {f["role"] for f in out["files"]} == {"resume", "cover_letter"} and len(out["files"]) == 4
    for item in out["files"]:
        assert Path(item["path"]).parent == drafts and "-draft." in item["filename"]
    assert sorted(p.name for p in drafts.parent.iterdir()) == ["drafts"]  # nothing beside the approved files
    claim = call(uid, "begin_application_submission", job_id=sea_id)
    assert claim["ok"] is False and claim["status"] == "NOT_READY"
    assert call(uid, "get_job_packet", job_id=sea_id)["ok"] is False


@pytest.mark.parametrize("score, decision, status", [(70, "PASS", "REVIEWED"), (40, "RETAILOR", "TAILORED")])
def test_a_reviewed_version_is_reviewed_unless_sent_back(account, score, decision, status):
    uid = account["user_id"]
    with session(uid) as db:
        sea = db.scalar(select(Job).where(Job.company == "Sea"))
        variant = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == sea.id))
        variant.reviewer_score, variant.reviewer_notes = score, {"decision": decision}
        db.commit()
        sea_id = sea.id
    out = call(uid, "get_job_documents", job_id=sea_id, formats=["pdf"])
    assert out["status"] == status and out["may_submit"] is False and len(out["files"]) == 2


def test_no_documents_before_tailoring(account):
    uid = account["user_id"]
    with session(uid) as db:
        job = Job(source="manual", source_ref=uuid.uuid4().hex, title="Engineer", company="Bare", status="shortlisted")
        db.add(job)
        db.commit()
        job_id = job.id
    assert "no tailored resume" in call(uid, "get_job_documents", job_id=job_id)["error"]


def test_document_tools_are_audited_without_letter_text(account):
    uid = account["user_id"]
    call(uid, "get_job_packet", job_id=account["grab"])
    call(uid, "get_job_documents", job_id=account["grab"])
    call(uid, "find_jobs", query="Grab")
    with session(uid) as db:
        rows = db.scalars(select(ConversationAction).order_by(ConversationAction.id)).all()
    assert [r.tool_name for r in rows] == ["get_job_packet", "get_job_documents", "find_jobs"]
    stored = " ".join(f"{r.arguments_summary} {r.result_summary}" for r in rows)
    assert "Dear Grab" not in stored and "Scaled the payments" not in stored
    assert rows[1].result_summary == {"outcome": "APPROVED"} and rows[2].result_summary == {"count": 1}
    assert rows[2].arguments_summary["query"] == "Grab"
