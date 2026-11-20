"""AI structuring of uploaded resumes (app.services.resume_parsing) and its LangGraph graph (app.agents.resume_parsing).

The model is replaced by a `generate` function where a test needs a specific answer; otherwise the mock provider
answers (it returns the regex parse of the text). The graph runs on SQLite with an in-memory checkpoint.
"""
from __future__ import annotations

import copy
import io

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import AgentEffect, AgentThread, Resume, ResumeParseTask
from app.services import resume_parsing as rp
from app.services import resume_parsing_graph, workers
from app.services import resume_verification as rv
from app.services import structured_resume as sr
from app.services.lifecycle import STOPPED_BY_USER
from app.settings import get_settings
from tests.conftest import make_user

client = TestClient(app)

# The header names the employer first: the regex parser reads it as "Title | Company" and gets both wrong.
RESUME = """# Jane Doe
Lead Engineer
Bengaluru, India | jane@example.com | +91 98765 43210 | linkedin.com/in/jane

## Skills
- Languages: Python, Go
- Cloud: AWS, K8s

## Experience
### Zendrive | Senior Engineer | Bengaluru | Jun 2024 - Oct 2025
- Reduced production incidents by 45% across Kafka pipelines
- Owned the Python ingestion services on AWS

### Adori AI | Engineer | Remote | Jan 2021 - May 2024
- Built LLM features for podcast search

## Education
B.Tech Computer Science, IIT Delhi, 2012 - 2016
"""


def ai_answer() -> dict:
    """What a good model returns for RESUME (input shape), plus one invented bullet and one invented skill."""
    return sr.from_parser_output({
        "personal_information": {"full_name": "Jane Doe", "headline": "Lead Engineer", "location": "Bengaluru, India",
                                 "email": "jane@example.com", "phone": "+91 98765 43210",
                                 "linkedin": "https://www.linkedin.com/in/jane"},
        "skills": [{"name": "Python", "category": "Languages"}, {"name": "Go", "category": "Languages"},
                   {"name": "AWS", "category": "Cloud"}, {"name": "Kubernetes", "category": "Cloud"},
                   {"name": "Rust", "category": "Languages"}],
        "experience": [
            {"company": "Zendrive", "job_title": "Senior Engineer", "location": "Bengaluru",
             "start_date": "2024-06", "end_date": "2025-10", "raw_start_date": "Jun 2024", "raw_end_date": "Oct 2025",
             "achievements": ["Reduced production incidents by 45% across Kafka pipelines",
                              "Saved $2M a year by rewriting the billing system"],
             "responsibilities": ["Owned the Python ingestion services on AWS"],
             "technologies": ["Kafka", "Python", "AWS"]},
            {"company": "Adori AI", "job_title": "Engineer", "location": "Remote", "start_date": "2021-01",
             "end_date": "2024-05", "raw_start_date": "Jan 2021", "raw_end_date": "May 2024",
             "achievements": ["Built LLM features for podcast search"]}],
        "education": [{"degree": "B.Tech", "field_of_study": "Computer Science", "institution": "IIT Delhi",
                       "raw_start_date": "2012", "raw_end_date": "2016"}],
    })


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    monkeypatch.setattr(get_settings(), "resume_parse_enabled", True)


@pytest.fixture
def user(monkeypatch):
    user_id = make_user(SessionLocal)
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, f"{user_id}@jobhunt.test", False))
    return user_id


def sessions_for(user_id):
    def factory():
        db = SessionLocal()
        db.info["user_id"] = user_id
        return db
    return factory


def upload(text=RESUME, name="cv.txt") -> dict:
    out = client.post("/api/profile/resume", files={"file": (name, io.BytesIO(text.encode()), "text/plain")})
    assert out.status_code == 200, out.text
    return client.get("/api/settings/resume").json()["active"]


def claim_one(user_id) -> rp.ParseClaim:
    with sessions_for(user_id)() as db:
        claims = [c for c in rp.claim(db, worker_id="test", limit=50) if c.user_id == user_id]
    assert len(claims) == 1
    return claims[0]


def resume_row(user_id, resume_id) -> Resume:
    with sessions_for(user_id)() as db:
        resume = db.get(Resume, resume_id)
        db.expunge(resume)
        return resume


def task_row(user_id, resume_id) -> ResumeParseTask:
    with sessions_for(user_id)() as db:
        task = rp.latest_task(db, resume_id)
        db.expunge(task)
        return task


def roles(resume: Resume) -> list[tuple[str, str]]:
    return [(r["job_title"], r["company"]) for r in resume.structured_data["experience"]]


# ───────────── grounding ─────────────

def test_grounding_keeps_the_texts_own_values_and_drops_invented_ones():
    data, dropped = rp.ground(ai_answer(), RESUME)
    zendrive = data["experience"][0]
    assert [b if isinstance(b, str) else b["text"] for b in zendrive["achievements"]] == [
        "Reduced production incidents by 45% across Kafka pipelines"]
    assert [s["name"] for s in data["skills"]] == ["Python", "Go", "AWS", "Kubernetes"]  # K8s grounds Kubernetes
    assert zendrive["start_date"] == "2024-06" and zendrive["end_date"] == "2025-10"
    assert data["personal_information"]["linkedin"] == "https://www.linkedin.com/in/jane"
    assert data["education"][0]["start_date"] is None  # a year alone never gets a month
    assert dropped == {"bullets": 1, "skills": 1}


def test_grounding_clears_a_reworded_title_and_drops_an_invented_role():
    answer = ai_answer()
    answer["experience"][1]["job_title"] = "Software Engineer II"
    answer["experience"].append({"company": "Initech", "job_title": "Architect", "achievements": ["Designed it all"]})
    data, dropped = rp.ground(answer, RESUME)
    assert [(r["job_title"], r["company"]) for r in data["experience"]] == [
        ("Senior Engineer", "Zendrive"), ("", "Adori AI")]
    assert dropped["roles"] == 1 and dropped["role_fields"] == 3


def test_a_bullet_split_by_pdf_extraction_still_grounds():
    text = RESUME.replace("across Kafka pipelines", "across\nKafka pi-\npelines")
    assert rp.Source(text).has_passage("Reduced production incidents by 45% across Kafka pipelines")
    assert not rp.Source(text).has_passage("Reduced costs by 45% across every team")


# ───────────── the queue and the steps ─────────────

def test_an_upload_queues_the_ai_parse_and_an_untouched_resume_is_filled(user):
    detail = upload()
    assert roles(resume_row(user, detail["id"]))[0] == ("Zendrive", "Senior Engineer")  # the regex parse, wrong
    assert task_row(user, detail["id"]).status == "PENDING"
    outcome = rp.process(sessions_for(user), claim_one(user), lambda prompt, call: ai_answer())
    assert outcome == rp.FILLED
    resume = resume_row(user, detail["id"])
    assert roles(resume) == [("Senior Engineer", "Zendrive"), ("Engineer", "Adori AI")]
    assert resume.parser == "llm" and resume.pending_reparse is None
    assert all(u.status == "UNVERIFIED" for u in sr.iter_units(resume.structured_data))
    task = task_row(user, detail["id"])
    assert (task.status, task.outcome, task.staged) == ("COMPLETED", "filled", None)
    assert task.counts["dropped"] == {"bullets": 1, "skills": 1}
    shown = client.get("/api/settings/resume").json()["active"]["ai_parse"]
    assert shown["outcome"] == "filled" and shown["dropped"] == 2 and not shown["active"]


def test_the_mock_provider_runs_end_to_end(user):
    detail = upload()
    assert rp.process(sessions_for(user), claim_one(user)) == rp.FILLED
    assert resume_row(user, detail["id"]).parser == "llm"


def test_a_reviewed_resume_gets_a_comparison_and_keeps_its_data(user):
    detail = upload()
    unit = next(u for u in sr.iter_units(detail["structured"]) if u.section == "skills")
    assert client.post(f"/api/resumes/{detail['id']}/verify-item",
                       json={"item_id": unit.item_id, "status": "VERIFIED"}).status_code == 200
    before = resume_row(user, detail["id"]).structured_data
    assert rp.process(sessions_for(user), claim_one(user), lambda prompt, call: ai_answer()) == rp.COMPARE
    resume = resume_row(user, detail["id"])
    assert resume.structured_data == before
    assert resume.pending_reparse["parser"] == "llm" and resume.pending_reparse["changes"]


def test_an_edit_while_the_model_reads_turns_the_fill_into_a_comparison(user):
    detail = upload()

    def generate(prompt, call):
        data = copy.deepcopy(detail["structured"])
        data["summary"] = {"text": "Edited by the person"}
        with sessions_for(user)() as db:
            rv.apply_edit(db, db.get(Resume, detail["id"]), sr.as_input(data), detail["revision"])
            db.commit()
        return ai_answer()

    assert rp.process(sessions_for(user), claim_one(user), generate) == rp.COMPARE
    assert resume_row(user, detail["id"]).structured_data["summary"]["text"] == "Edited by the person"


def test_a_poorer_answer_keeps_the_current_data(user):
    detail = upload()
    before = resume_row(user, detail["id"]).structured_data
    assert rp.process(sessions_for(user), claim_one(user), lambda prompt, call: sr.from_parser_output({})) == rp.KEPT
    assert resume_row(user, detail["id"]).structured_data == before
    assert "nothing the resume text confirms" in task_row(user, detail["id"]).last_error


def test_no_ai_configuration_ends_the_task_and_keeps_the_regex_parse(user, monkeypatch):
    from app.services import user_settings

    detail = upload()
    monkeypatch.setattr(user_settings, "ai_ready", lambda db, user_id: (False, "No OpenAI API key saved"))
    assert rp.process(sessions_for(user), claim_one(user), lambda prompt, call: pytest.fail("no model call")) \
        == rp.NOT_CONFIGURED
    assert resume_row(user, detail["id"]).parser == "deterministic"


def test_a_failing_model_retries_then_fails(user, monkeypatch):
    detail = upload()

    def broken(prompt, call):
        raise RuntimeError("provider down")

    assert rp.process(sessions_for(user), claim_one(user), broken) == "retrying"
    with sessions_for(user)() as db:
        db.execute(ResumeParseTask.__table__.update().where(ResumeParseTask.resume_id == detail["id"])
                   .values(next_attempt_at=None, attempt_count=get_settings().resume_parse_max_attempts - 1))
        db.commit()
    assert rp.process(sessions_for(user), claim_one(user), broken) == "failed"
    assert task_row(user, detail["id"]).status == "FAILED"


def test_kill_stops_the_run_and_nothing_is_applied(user):
    detail = upload()
    claim = claim_one(user)
    with sessions_for(user)() as db:
        out = workers.kill(db, user, "resume_parsing", [claim.task_id])
    assert out["stopped"] == 1
    assert rp.process(sessions_for(user), claim, lambda prompt, call: ai_answer()) == "lost"
    assert resume_row(user, detail["id"]).parser == "deterministic"
    task = task_row(user, detail["id"])
    assert task.last_error == STOPPED_BY_USER
    with sessions_for(user)() as db:  # an upload of the same text never queues it again behind the person's back
        assert rp.enqueue(db, db.get(Resume, detail["id"])) is None


def test_reparse_with_ai_queues_instead_of_calling_the_model(user):
    detail = upload()
    rp.process(sessions_for(user), claim_one(user), lambda prompt, call: ai_answer())
    out = client.post(f"/api/resumes/{detail['id']}/reparse", json={"use_ai": True})
    assert out.status_code == 200
    assert out.json()["ai_parse"]["active"] is True
    task = task_row(user, detail["id"])
    assert task.reason == rp.USER_REQUEST and task.run_now_at is not None


def test_an_upload_already_structured_by_the_current_prompt_is_not_queued_again(user):
    detail = upload()
    rp.process(sessions_for(user), claim_one(user), lambda prompt, call: ai_answer())
    with sessions_for(user)() as db:
        assert rp.enqueue(db, db.get(Resume, detail["id"])) is None


def test_running_workers_lists_the_task_with_the_resume_name(user):
    upload()
    rows = client.get("/api/workers/resume_parsing").json()["rows"]
    assert rows[0]["title"] == "cv.txt" and rows[0]["company"] == "v1" and rows[0]["job_id"] is None


def test_running_workers_shows_the_model_a_resume_parse_used(user):
    import uuid

    from app.models import LLMRequestLog

    detail = upload()
    queued = client.get("/api/workers/resume_parsing").json()["rows"][0]
    assert queued["model"]["source"] == "next"  # the model its request goes to
    with sessions_for(user)() as db:  # the gateway's row for the parse (profile, keyed by the resume)
        db.add(LLMRequestLog(request_id=f"rp-{uuid.uuid4()}", user_id=user, workflow_type="profile",
                             workflow_entity_id=detail["id"], worker_type="RESUME_PARSER", stage="OTHER",
                             provider="litellm", requested_model="parser-model", model="parser-model",
                             status="SUCCEEDED", created_at=rp.utcnow()))
        db.commit()
    assert rp.process(sessions_for(user), claim_one(user), lambda prompt, call: ai_answer()) == rp.FILLED
    done = client.get("/api/workers/resume_parsing").json()["rows"][0]
    assert done["status"] == "COMPLETED"
    assert done["model"]["source"] == "used" and done["model"]["model"] == "parser-model"


def test_ai_only_runs_an_upload_now_even_while_paused_and_keeps_the_regex_parse_as_fallback(user):
    from app.services import worker_settings

    with sessions_for(user)() as db:
        worker_settings.update(db, user, {"resume_parsing": {"ai_only": True, "auto": False}})
    assert client.get("/api/settings/resume").json()["ai_only"] is True
    detail = upload()
    assert task_row(user, detail["id"]).run_now_at is not None, "ahead of the queue"
    assert resume_row(user, detail["id"]).parser == "deterministic", "the fallback is there at once"
    claim = claim_one(user)  # claimed although the account paused the worker
    assert claim.run_now
    assert rp.process(sessions_for(user), claim, lambda prompt, call: ai_answer()) == rp.FILLED
    assert resume_row(user, detail["id"]).parser == "llm"


def test_without_ai_only_an_upload_waits_its_turn(user):
    detail = upload()
    assert task_row(user, detail["id"]).run_now_at is None
    assert client.get("/api/settings/resume").json()["ai_only"] is False


def test_new_uploads_use_ai_and_the_new_reader_by_default(user, monkeypatch):
    """Both reading options are defaults, off the Resumes screen: on for every account that never chose."""
    from app.settings import Settings, get_settings

    assert Settings.model_fields["resume_upload_ai_only"].default is True
    assert Settings.model_fields["resume_upload_reader_v2"].default is True
    monkeypatch.setattr(get_settings(), "resume_upload_ai_only", True)
    monkeypatch.setattr(get_settings(), "resume_upload_reader_v2", True)
    shown = client.get("/api/settings/resume").json()
    assert shown["ai_only"] is True and shown["extraction_v2"] is True
    detail = upload()
    assert task_row(user, detail["id"]).run_now_at is not None, "the Resume Parser model reads it straight away"


def test_the_pdf_reader_follows_the_server_until_the_account_chooses(user, monkeypatch):
    from app.services import document_text, worker_settings
    from app.settings import get_settings

    seen = []
    real = document_text.extract

    def spy(kind, data, **kwargs):
        seen.append(kwargs.get("v2"))
        return real(kind, data, **kwargs)

    monkeypatch.setattr(document_text, "extract", spy)
    monkeypatch.setattr(get_settings(), "resume_upload_reader_v2", False)
    assert client.get("/api/settings/resume").json()["extraction_v2"] is False
    upload()
    monkeypatch.setattr(get_settings(), "resume_upload_reader_v2", True)  # the server default moves: so does the account
    assert client.get("/api/settings/resume").json()["extraction_v2"] is True
    with sessions_for(user)() as db:
        worker_settings.update(db, user, {"resume_parsing": {"extraction_v2": False}})  # the account's own choice wins
    assert client.get("/api/settings/resume").json()["extraction_v2"] is False
    upload("Another resume\nSkills\nPython")
    assert seen == [False, False]
    with sessions_for(user)() as db:
        field = next(f for f in worker_settings.describe(db, user)["resume_parsing"]["fields"]
                     if f["name"] == "extraction_v2")
    assert field["type"] == "bool" and field["default"] is True, "Running workers → Settings shows the server's value"


# ───────────── the graph ─────────────

def test_the_graph_fills_the_resume_and_records_each_step_once(user):
    detail = upload()
    claim = claim_one(user)
    outcome = resume_parsing_graph.run(sessions_for(user), claim, saver=InMemorySaver(), graph=True)
    assert outcome == rp.FILLED  # the mock provider answered through GatewayChatModel
    assert resume_row(user, detail["id"]).parser == "llm"
    with sessions_for(user)() as db:
        task = rp.latest_task(db, detail["id"])
        thread = db.get(AgentThread, task.agent_thread_id)
        assert thread.graph == "resume_parsing" and thread.status == "COMPLETED"
        assert thread.result["outcome"] == rp.FILLED and "structured_resume" not in str(thread.result)
        keys = sorted(e.effect_key.split(":")[0] for e in db.scalars(select(AgentEffect).where(
            AgentEffect.agent_thread_id == thread.id)).all())
    assert keys == ["resume_parse.APPLY_RESULT", "resume_parse.CHECK_QUALITY", "resume_parse.EXTRACT_STRUCTURE",
                    "resume_parse.GROUND_IN_TEXT"]


def test_a_recorded_step_is_not_written_twice(user):
    detail = upload()
    claim = claim_one(user)
    for step in (rp.load, lambda s, c: rp.extract(s, c, lambda prompt, call: ai_answer()), rp.ground_step, rp.check):
        assert not step(sessions_for(user), claim).get("outcome")
    replayed = rp.apply(sessions_for(user), claim, within=lambda db, step: False)
    assert replayed == {"step": rp.APPLY}
    assert resume_row(user, detail["id"]).parser == "deterministic"


# ───────────── review fixes ─────────────

def test_a_changed_number_never_passes_as_a_broken_word():
    text = "Reduced production incidents by 45% across Kafka pipelines through improved monitoring and automated recovery"
    src = rp.Source(text)
    assert not src.has_passage(text.replace("45%", "95%"))
    assert not src.has_passage(text.replace("45%", "45% and $2M"))
    # PDF extraction broke an ordinary word: the clean bullet still grounds, with its number exact.
    assert rp.Source(text.replace("pipelines", "pipe-\nlines")).has_passage(text)


def test_the_current_role_flag_follows_the_grounded_end_date():
    text = "Acme | Engineer | Jan 2020 - Dec 2021\nInitech | Lead | Jan 2022 - Present"
    data, _ = rp.ground({"experience": [
        {"company": "Acme", "job_title": "Engineer", "raw_end_date": "Dec 2021", "end_date": "2021-12", "is_current": True},
        {"company": "Acme", "job_title": "Engineer", "raw_end_date": "Mar 2023", "is_current": True},  # not in the text
        {"company": "Initech", "job_title": "Lead", "raw_end_date": "Present", "is_current": False}]}, text)
    assert [r["is_current"] for r in data["experience"]] == [False, False, True]
    assert data["experience"][1].get("end_date") is None and data["experience"][2].get("end_date") is None


def test_performance_reads_a_failed_resume_parse(user):
    from datetime import UTC, datetime, timedelta

    upload()
    claim = claim_one(user)
    with sessions_for(user)() as db:
        db.execute(ResumeParseTask.__table__.update().where(ResumeParseTask.id == claim.task_id)
                   .values(attempt_count=get_settings().resume_parse_max_attempts))
        db.commit()
    assert rp.fail(sessions_for(user), claim, "RuntimeError: provider down") == "failed"
    now = datetime.now(UTC).replace(tzinfo=None)
    with sessions_for(user)() as db:
        out = workers.performance(db, user, "resume_parsing", now - timedelta(days=1), now + timedelta(minutes=1))
    assert out["failed"] == 1 and out["failures_by_category"] == {"EXTERNAL_SERVICE": 1}


def test_held_locks_the_task_row_on_postgresql():
    from sqlalchemy.dialects import postgresql

    class Bind:
        dialect = postgresql.dialect()

    seen = []

    class Db:
        def get_bind(self):
            return Bind()

        def scalar(self, query):
            seen.append(str(query.compile(dialect=postgresql.dialect())))

    rp.held(Db(), rp.ParseClaim(1, "token", "w", 1, 1))
    assert seen[0].rstrip().endswith("FOR UPDATE")
