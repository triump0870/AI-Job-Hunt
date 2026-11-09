"""Follow-ups over MCP and in the Tracker: what is due, Claude's draft, and what the person sent or skipped.

Job Hunt OS never sends a message and never calls a model for these: every tool and endpoint records what a person did.
"""
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app as web_app
from app.mcp import tools
from app.mcp.server import TOOL_NAMES, build_server, run_tool
from app.models import (
    Application,
    ApplicationCommunicationEvent,
    ApplicationEvent,
    ConversationAction,
    FollowUp,
    Job,
)
from app.services import followups, llm, workflow
from app.services.user_settings import settings_for
from tests.conftest import ensure_test_user, make_user

LETTER = "Hi Dana, I applied for the Platform Engineer role last week and remain very interested. Best, Test"
SECRET_TEXT = "Hi Priya, just checking in on the Staff Engineer role. Thanks!"


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
        raise AssertionError("Follow-up tools must not call the LLM layer")

    monkeypatch.setattr(llm.LLMClient, "generate_prompt", refuse)
    monkeypatch.setattr(llm.LLMClient, "generate_prompt_json", refuse)


def _application(db, company, *, applied_days_ago=8, due_in_days=-1, applied=True, recruiter=None):
    job = Job(source="manual", source_ref=uuid.uuid4().hex, title="Platform Engineer", company=company,
              location="Remote", url=f"https://boards.greenhouse.io/{company.lower()}/jobs/1", description="Go",
              status="applied" if applied else "approved", triaged_at=now())
    db.add(job)
    db.flush()
    application = Application(job_id=job.id, status="applied" if applied else "approved", approved_at=now(),
                              applied_at=now() - timedelta(days=applied_days_ago) if applied else None,
                              followup_due_at=now() + timedelta(days=due_in_days) if applied else None,
                              stage="applied" if applied else None,
                              contacts=[recruiter] if recruiter else None)
    db.add(application)
    db.flush()
    return application


@pytest.fixture
def account():
    user_id = make_user(SessionLocal)
    with session(user_id) as db:
        settings_for(db).preferences = {"followup_days": 5}
        overdue = _application(db, "Acme", due_in_days=-1,
                               recruiter={"name": "Dana", "email": "dana@acme.test", "phone": ""})
        drafted = _application(db, "Globex", due_in_days=2)
        db.add(FollowUp(application_id=drafted.id, due_at=drafted.followup_due_at, channel="email", draft=LETTER,
                        status="draft"))
        later = _application(db, "Initech", due_in_days=10)
        replied = _application(db, "Umbrella", due_in_days=-2)
        db.add(ApplicationCommunicationEvent(application_id=replied.id, gmail_message_id=uuid.uuid4().hex,
                                             event_type="RECRUITER_RESPONSE", received_at=now()))
        never = _application(db, "Hooli", applied=False)
        db.commit()
        yield {"user_id": user_id, "overdue": overdue.id, "drafted": drafted.id, "later": later.id,
               "replied": replied.id, "never": never.id}


def _rows(user_id, application_id):
    with session(user_id) as db:
        fus = db.scalars(select(FollowUp).where(FollowUp.application_id == application_id)
                         .order_by(FollowUp.id)).all()
        events = db.scalars(select(ApplicationEvent).where(ApplicationEvent.application_id == application_id)
                            .order_by(ApplicationEvent.id)).all()
        app = db.get(Application, application_id)
        return fus, events, app


# ───────────── list_followups_due ─────────────

def test_lists_drafts_and_dates_with_no_draft_and_flags_a_reply(account):
    uid = account["user_id"]
    out = call(uid, "list_followups_due", within_days=3)
    assert out["ok"] and out["rules"] and out["prompt_version"] == "v1"
    by_app = {f["application_id"]: f for f in out["followups"]}
    assert set(by_app) == {account["overdue"], account["drafted"], account["replied"]}  # Initech is 10 days out
    assert out["followups"][0]["application_id"] == account["replied"]  # soonest first
    overdue = by_app[account["overdue"]]
    assert overdue["needs_draft"] is True and overdue["followup_id"] is None and overdue["draft"] == ""
    assert overdue["recruiters"] == [{"name": "Dana", "email": "dana@acme.test", "phone": ""}]
    assert overdue["company"] == "Acme" and overdue["title"] == "Platform Engineer"
    assert overdue["employer_replied"] is False and overdue["previous_followups"] == []
    drafted = by_app[account["drafted"]]
    assert drafted["needs_draft"] is False and drafted["draft"] == LETTER and drafted["channel"] == "email"
    assert by_app[account["replied"]]["employer_replied"] is True

    due_now = call(uid, "list_followups_due", within_days=0)
    assert {f["application_id"] for f in due_now["followups"]} == {account["overdue"], account["replied"]}
    assert len(call(uid, "list_followups_due", within_days=30, limit=1)["followups"]) == 1
    assert call(uid, "list_followups_due", within_days=-1)["ok"] is False
    assert call(uid, "list_followups_due", limit=500)["ok"] is False


def test_another_account_sees_none_of_these_follow_ups(account):
    other = make_user(SessionLocal)
    assert call(other, "list_followups_due", within_days=30)["followups"] == []
    assert call(other, "save_followup_draft", application_id=account["overdue"], text="Hi")["ok"] is False
    with session(account["user_id"]) as db:
        fu_id = db.scalar(select(FollowUp.id).where(FollowUp.application_id == account["drafted"]))
    assert call(other, "record_followup_sent", followup_id=fu_id)["ok"] is False
    assert call(other, "skip_followup", followup_id=fu_id)["ok"] is False
    assert _rows(account["user_id"], account["drafted"])[0][0].status == "draft"


# ───────────── save_followup_draft ─────────────

def test_a_draft_is_created_once_and_replaced(account):
    uid = account["user_id"]
    first = call(uid, "save_followup_draft", application_id=account["overdue"], text="First version")
    assert first["ok"] and first["status"] == "SAVED" and first["employer_replied"] is False
    second = call(uid, "save_followup_draft", application_id=account["overdue"], text=SECRET_TEXT, channel="linkedin")
    assert second["followup_id"] == first["followup_id"]
    fus, events, _app = _rows(uid, account["overdue"])
    assert len(fus) == 1 and fus[0].draft == SECRET_TEXT and fus[0].channel == "linkedin"
    assert fus[0].status == "draft" and fus[0].generation == {"source": "mcp"} and events == []
    listed = {f["application_id"]: f for f in call(uid, "list_followups_due")["followups"]}
    assert listed[account["overdue"]]["needs_draft"] is False


def test_drafts_are_validated_and_only_for_submitted_applications(account):
    uid = account["user_id"]
    assert "never submitted" in call(uid, "save_followup_draft", application_id=account["never"], text="Hi")["error"]
    assert call(uid, "save_followup_draft", application_id=account["overdue"], text="  ")["ok"] is False
    assert call(uid, "save_followup_draft", application_id=account["overdue"], text="x" * 5001)["ok"] is False
    assert call(uid, "save_followup_draft", application_id=account["overdue"], text="Hi", channel="sms")["ok"] is False
    assert call(uid, "save_followup_draft", application_id=10**9, text="Hi")["ok"] is False
    assert _rows(uid, account["overdue"])[0] == []


def test_a_draft_says_when_the_employer_already_replied(account):
    out = call(account["user_id"], "save_followup_draft", application_id=account["replied"], text="Hi")
    assert out["ok"] and out["employer_replied"] is True


# ───────────── record_followup_sent ─────────────

def test_recording_a_send_is_idempotent_and_the_event_holds_no_text(account):
    uid = account["user_id"]
    fu_id = call(uid, "save_followup_draft", application_id=account["overdue"], text=SECRET_TEXT)["followup_id"]
    sent = call(uid, "record_followup_sent", followup_id=fu_id, recipient="dana@acme.test")
    assert sent["ok"] and sent["status"] == "SENT" and sent["channel"] == "email"
    again = call(uid, "record_followup_sent", followup_id=fu_id, sent_text="Something else")
    assert again["ok"] and again["status"] == "ALREADY_SENT"
    fus, events, app = _rows(uid, account["overdue"])
    assert len(fus) == 1 and fus[0].status == "sent" and fus[0].draft == SECRET_TEXT and fus[0].sent_at
    [event] = events
    assert event.title == "Follow-up sent" and event.kind == "followup"
    assert event.meta["channel"] == "email" and event.meta["recipient"] == "dana@acme.test"
    assert SECRET_TEXT not in f"{event.title} {event.detail} {event.meta}"
    # The next follow-up is scheduled after the account's followup_days (5), from when it was sent.
    assert abs((app.followup_due_at - fus[0].sent_at) - timedelta(days=5)) < timedelta(seconds=1)
    assert sent["next_followup_due_at"] == app.followup_due_at.isoformat()


def test_the_sent_text_and_time_are_what_the_person_says(account):
    uid = account["user_id"]
    fu_id = call(uid, "save_followup_draft", application_id=account["overdue"], text="Draft")["followup_id"]
    out = call(uid, "record_followup_sent", followup_id=fu_id, sent_text="Final words", channel="linkedin",
               sent_at="2026-11-01T09:30:00Z")
    assert out["ok"] and out["sent_at"] == "2026-11-01T09:30:00"
    fus, _events, app = _rows(uid, account["overdue"])
    assert fus[0].draft == "Final words" and fus[0].channel == "linkedin"
    assert app.followup_due_at == datetime(2026, 11, 6, 9, 30)
    fu2 = call(uid, "save_followup_draft", application_id=account["overdue"], text="Two")["followup_id"]
    assert "ISO" in call(uid, "record_followup_sent", followup_id=fu2, sent_at="yesterday")["error"]


def test_never_more_than_two_follow_ups_per_application(account):
    uid = account["user_id"]
    for expected_next in (True, False):
        listed = {f["application_id"]: f for f in call(uid, "list_followups_due", within_days=60)["followups"]}
        assert listed[account["overdue"]]["needs_draft"] is True
        fu_id = call(uid, "save_followup_draft", application_id=account["overdue"], text="Checking in")["followup_id"]
        out = call(uid, "record_followup_sent", followup_id=fu_id)
        assert (out["next_followup_due_at"] is not None) is expected_next
    listed = call(uid, "list_followups_due", within_days=60)["followups"]
    assert account["overdue"] not in {f["application_id"] for f in listed}
    fus, events, app = _rows(uid, account["overdue"])
    assert [f.status for f in fus] == ["sent", "sent"] and app.followup_due_at is None and len(events) == 2


def test_previous_follow_ups_are_listed_for_the_next_draft(account):
    uid = account["user_id"]
    fu_id = call(uid, "save_followup_draft", application_id=account["overdue"], text="The first nudge")["followup_id"]
    call(uid, "record_followup_sent", followup_id=fu_id)
    item = next(f for f in call(uid, "list_followups_due", within_days=30)["followups"]
                if f["application_id"] == account["overdue"])
    assert item["needs_draft"] is True and item["previous_followups"] == ["The first nudge"]


def test_no_next_follow_up_once_the_employer_replied(account):
    uid = account["user_id"]
    fu_id = call(uid, "save_followup_draft", application_id=account["replied"], text="Hello")["followup_id"]
    out = call(uid, "record_followup_sent", followup_id=fu_id)
    assert out["status"] == "SENT" and out["next_followup_due_at"] is None


def test_a_cancelled_follow_up_cannot_be_recorded_as_sent(account):
    uid = account["user_id"]
    fu_id = call(uid, "save_followup_draft", application_id=account["overdue"], text="Hello")["followup_id"]
    assert call(uid, "skip_followup", followup_id=fu_id)["status"] == "SKIPPED"
    assert "cancelled" in call(uid, "record_followup_sent", followup_id=fu_id)["error"]


# ───────────── skip_followup ─────────────

def test_skipping_cancels_the_draft_and_clears_the_date(account):
    uid = account["user_id"]
    with session(uid) as db:
        fu_id = db.scalar(select(FollowUp.id).where(FollowUp.application_id == account["drafted"]))
    out = call(uid, "skip_followup", followup_id=fu_id, reason="Heard back by phone")
    assert out["ok"] and out["status"] == "SKIPPED" and out["application_id"] == account["drafted"]
    fus, events, app = _rows(uid, account["drafted"])
    assert fus[0].status == "cancelled" and app.followup_due_at is None
    assert [e.title for e in events] == ["Follow-up skipped"] and events[0].detail == "Heard back by phone"
    assert call(uid, "skip_followup", followup_id=fu_id)["status"] == "ALREADY_SKIPPED"
    assert len(_rows(uid, account["drafted"])[1]) == 1


def test_skipping_a_date_with_no_draft_records_the_skip(account):
    uid = account["user_id"]
    out = call(uid, "skip_followup", application_id=account["overdue"])
    assert out["status"] == "SKIPPED" and out["followup_id"] is None
    fus, events, app = _rows(uid, account["overdue"])
    assert fus == [] and app.followup_due_at is None and events[0].title == "Follow-up skipped"
    assert account["overdue"] not in {f["application_id"] for f in call(uid, "list_followups_due")["followups"]}
    assert call(uid, "skip_followup")["ok"] is False
    assert call(uid, "skip_followup", application_id=account["never"])["ok"] is False


def test_a_sent_follow_up_cannot_be_skipped(account):
    uid = account["user_id"]
    fu_id = call(uid, "save_followup_draft", application_id=account["overdue"], text="Hello")["followup_id"]
    call(uid, "record_followup_sent", followup_id=fu_id)
    assert "already sent" in call(uid, "skip_followup", followup_id=fu_id)["error"]


# ───────────── the scheduler ─────────────

def test_the_scheduler_drafts_only_what_needs_a_draft(account, monkeypatch):
    drafted = []
    monkeypatch.setattr(workflow, "draft_followup", lambda db, app, commit=True: drafted.append(app.id))
    uid = account["user_id"]
    with session(uid) as db:
        workflow.create_due_followups(db)
    # Due with no draft: Acme. Globex already has a draft and is not due; Umbrella replied, so it is skipped.
    assert drafted == [account["overdue"]]
    _fus, events, app = _rows(uid, account["replied"])
    assert app.followup_due_at is None and events[0].title == "Follow-up not needed"


# ───────────── the Tracker's buttons: the same rows as the MCP tools ─────────────

def _comparable(user_id, application_id):
    fus, events, app = _rows(user_id, application_id)
    return ([(f.status, f.channel, f.draft, f.sent_at is not None) for f in fus],
            [(e.kind, e.title, e.detail, {k: v for k, v in (e.meta or {}).items() if k not in ("source", "followup_id",
                                                                                                  "followup_ids")})
             for e in events],
            app.followup_due_at is not None)


def test_the_web_endpoints_and_the_mcp_tools_write_the_same_rows():
    uid = ensure_test_user().id  # the account the API client acts as
    with session(uid) as db:
        settings_for(db).preferences = {**(settings_for(db).preferences or {}), "followup_days": 5}
        apps = [_application(db, f"Same{i}{uuid.uuid4().hex[:4]}") for i in range(4)]
        fus = [FollowUp(application_id=a.id, due_at=now(), channel="email", draft=SECRET_TEXT, status="draft")
               for a in apps[:3]]
        db.add_all(fus)
        db.commit()
        app_ids, fu_ids = [a.id for a in apps], [f.id for f in fus]
    client = TestClient(web_app)
    sent = client.post(f"/api/followups/{fu_ids[0]}/sent", json={"recipient": "dana@acme.test"})
    assert sent.status_code == 200 and sent.json()["followup_status"] == "SENT"
    assert sent.json()["draft_status"] == "sent"
    assert client.post(f"/api/followups/{fu_ids[0]}/sent", json={}).json()["followup_status"] == "ALREADY_SENT"
    call(uid, "record_followup_sent", followup_id=fu_ids[1], recipient="dana@acme.test")
    assert _comparable(uid, app_ids[0]) == _comparable(uid, app_ids[1])

    skipped = client.post(f"/api/followups/{fu_ids[2]}/skip", json={"reason": "Not needed"})
    assert skipped.status_code == 200 and skipped.json()["followup_status"] == "SKIPPED"
    assert skipped.json()["followup_pending"] is False
    call(uid, "skip_followup", application_id=app_ids[3], reason="Not needed")
    web, mcp = _comparable(uid, app_ids[2]), _comparable(uid, app_ids[3])
    assert web[1:] == mcp[1:] and web[0] == [("cancelled", "email", SECRET_TEXT, False)] and mcp[0] == []
    # Skipping a date with no draft from the Tracker, and a follow-up of another account.
    with session(uid) as db:
        extra = _application(db, f"Extra{uuid.uuid4().hex[:4]}")
        db.commit()
        extra_id = extra.id
    assert client.post(f"/api/applications/{extra_id}/followup-skip", json={}).json()["followup_status"] == "SKIPPED"
    assert client.post("/api/followups/999999999/sent", json={}).status_code == 404
    assert client.post(f"/api/followups/{fu_ids[2]}/sent", json={}).status_code == 400  # cancelled


# ───────────── server and audit ─────────────

def test_follow_up_tools_are_served_and_audited_without_message_text(account):
    uid = account["user_id"]
    server = build_server(uid)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {"list_followups_due", "save_followup_draft", "record_followup_sent", "skip_followup"} <= names
    assert names == set(TOOL_NAMES)
    prompt = asyncio.run(server.get_prompt("follow_up", {"within_days": "2", "limit": "3"})).messages[0].content.text
    assert "list_followups_due" in prompt and "explicitly says yes" in prompt and "within 2 days" in prompt

    call(uid, "list_followups_due")
    fu_id = call(uid, "save_followup_draft", application_id=account["overdue"], text=SECRET_TEXT)["followup_id"]
    call(uid, "record_followup_sent", followup_id=fu_id, sent_text=SECRET_TEXT, recipient="dana@acme.test")
    call(uid, "skip_followup", application_id=account["drafted"], reason="Heard back on a call with the hiring lead")
    with session(uid) as db:
        rows = db.scalars(select(ConversationAction).order_by(ConversationAction.id)).all()
    assert [r.tool_name for r in rows] == ["list_followups_due", "save_followup_draft", "record_followup_sent",
                                           "skip_followup"]
    assert [r.result_status for r in rows] == ["OK"] * 4
    stored = " ".join(f"{r.arguments_summary} {r.result_summary}" for r in rows)
    for text in (SECRET_TEXT, LETTER, "dana@acme.test", "hiring lead"):
        assert text not in stored
    assert rows[0].result_summary == {"count": 3}
    assert rows[1].arguments_summary["text"] == {"length": len(SECRET_TEXT)}
    assert rows[1].entity_type == "application" and rows[1].entity_id == account["overdue"]
    assert rows[2].entity_type == "followup" and rows[2].entity_id == fu_id
    assert rows[2].result_summary == {"outcome": "SENT"} and rows[2].arguments_summary["followup_id"] == fu_id


def test_the_service_refuses_a_follow_up_of_another_application(account):
    uid = account["user_id"]
    with session(uid) as db:
        fu = db.scalar(select(FollowUp).where(FollowUp.application_id == account["drafted"]))
        other = db.get(Application, account["overdue"])
        with pytest.raises(followups.FollowUpRefused):
            followups.skip(db, other, followup=fu)
