"""Human takeover, saved logins and signup on the browser-use backend (Phase 6), on PostgreSQL, with an in-process fake
sidecar (no window opens) that behaves like the runner: a login wall, a signup page, then the form.

  * a login wall with no saved login keeps the window for the person (PENDING, NEEDS_HUMAN, takeover); when they say
    they are done the run re-observes the page and fills it — never replaying anything;
  * a saved login on an allowlisted domain is filled once by Python (signed LOGIN click), and the password never
    reaches a task, an attempt, a checkpoint, a log line or another domain;
  * a signup page gets a generated password shown once, and "Save this login" stores it with the window's session;
  * a takeover nobody comes back to ends NEEDS_HUMAN with the shared blocker;
  * another account cannot continue, sign up or save a login on someone else's execution.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.agents import checkpoint
from app.infrastructure.application_browser import browser_use_sidecar as bus
from app.models import ApplicationExecutionTask, AtsCredential, AtsSession, Job
from app.services import application_execution, ats_auth
from tests import application_support as aps
from tests import qualification_support as qs

URL = "https://acme.wd5.myworkdayjobs.com/External/job/Singapore/Platform-Engineer_R1"
DOMAIN = "acme.wd5.myworkdayjobs.com"
PASSWORD = "Saved-Vault-Pa55!"
LOGIN = [{"key": "u", "tag": "input", "type": "email", "label": "Email address", "in_form": True, "form_index": 0},
         {"key": "p", "tag": "input", "type": "password", "label": "Password", "in_form": True, "form_index": 0},
         {"key": "s", "tag": "button", "type": "submit", "label": "Sign In", "submit_like": True, "in_form": True,
          "form_index": 0}]
SIGNUP = [*LOGIN[:2], {"key": "p2", "tag": "input", "type": "password", "label": "Verify new password"},
          {"key": "s2", "tag": "button", "type": "submit", "label": "Create Account", "submit_like": True}]
FORM = [{"key": "n", "tag": "input", "type": "text", "label": "Full name *", "required": True, "in_form": True},
        {"key": "e", "tag": "input", "type": "email", "label": "Email *", "required": True, "in_form": True},
        {"key": "r", "tag": "input", "type": "file", "label": "Resume *", "required": True, "in_form": True},
        {"key": "q", "tag": "input", "type": "text", "label": aps.PYTHON_QUESTION + " *", "required": True,
         "in_form": True},
        {"key": "go", "tag": "button", "type": "submit", "label": "Submit", "submit_like": True, "in_form": True}]
HTML = {"landing": "<p>Welcome back, Asha</p>", "done": "<p>Thank you for applying</p>",
        "login": '<form><input type="email"><input type="password"><button>Sign In</button></form><p>Sign in</p>',
        "signup": '<form><input type="password"><input type="password"></form><p>Create an account</p>',
        "form": "<form><input name=n><input type=file></form><p>Apply</p>"}


class FakeSession:
    def __init__(self, manager: FakeManager, user_id: int, task_id: int, storage: dict | None):
        self.manager, self.user_id, self.task_id = manager, user_id, task_id
        self.session_id, self.hmac_key, self.headless = "fake", b"k" * 32, False
        self.parked_until: float | None = None
        self.alive = True
        self.values: dict[str, Any] = {}
        self.commands: list[tuple[str, dict]] = []
        self.replaced: dict[str, str] = {}
        self.storage = storage

    @property
    def page(self) -> str:
        return self.manager.page

    async def call(self, cmd: str, **args: Any) -> dict[str, Any]:
        from app.services import redaction

        self.commands.append((cmd, {k: v for k, v in args.items() if k != "secret"}))
        if cmd == "goto":
            if self.manager.page == "landing":  # signed in, then the site's home: the apply URL shows the form
                self.manager.page = "form"
            return {"final_url": URL, "status": 200, "title": "Acme careers"}
        if cmd == "observe":
            elements = {"landing": [], "done": [], "login": LOGIN, "signup": SIGNUP, "form": FORM}[self.page]
            gone = self.replaced if self.page == "form" else {}  # file inputs the board replaced with their file's name
            return {"url": URL, "title": "Acme", "elements": [dict(e) for e in elements if e["key"] not in gone],
                    "html": HTML[self.page], "text": "Acme careers", "blocked_requests": 0,
                    "captcha": dict(self.manager.captcha), "attached": dict(gone)}
        if cmd == "challenge":
            return dict(self.manager.captcha)
        if cmd in ("fill", "select"):
            self.values[args["key"]] = args.get("text", args.get("value"))
            if self.manager.captcha_after_fill:  # the site shows its CAPTCHA once the form is filled
                self.manager.captcha = {"present": True, "solved": False, "kind": "recaptcha"}
                self.manager.captcha_after_fill = False
            return {"filled": True}
        if cmd == "submit":
            self.manager.submitted.append(args["key"])
            self.manager.page = "done"
            return {"clicked": True}
        if cmd == "evidence":  # the site's confirmation only after a submission; before it, the form is there
            if self.page != "done":
                return {"url": URL, "title": "Acme", "form_inputs": 4, "responses": [], "text": "Apply",
                        "captcha": dict(self.manager.captcha)}
            return {"url": URL + "/confirmation", "title": "Thanks", "form_inputs": 0,
                    "responses": [{"method": "POST", "status": 200, "host": DOMAIN}],
                    "text": "Thank you for applying", "captcha": {"present": False, "solved": False, "kind": None}}
        if cmd == "upload":
            self.values[args["key"]] = [args["path"].rsplit("/", 1)[-1]]
            if self.manager.replace_uploads:
                self.replaced[args["key"]] = self.values[args["key"]][0]
            return {"uploaded": True}
        if cmd == "read":
            value = self.values.get(args["key"])
            out = {"files": value} if isinstance(value, list) else {"value": value or ""}
            return {**out, "filled": bool(value)}
        if cmd == "point":
            self.manager.pointed.append((list(args.get("keys") or []), args.get("message")))
            return {"marked": len(args.get("keys") or [])}
        if cmd == "unpoint":
            return {"cleared": True}
        if cmd == "login_fill":
            self.manager.secrets.append((args["key"], args["secret"], args["domain"],
                                         redaction.redact_text(args["secret"])))
            return {"filled": True}
        if cmd == "login_submit":
            body, signature = args["token"].rsplit(".", 1)
            import base64
            import hashlib
            import hmac
            assert hmac.compare_digest(hmac.new(self.hmac_key, body.encode(), hashlib.sha256).hexdigest(), signature)
            claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            assert claims["kind"] == "LOGIN" and claims["domain"] == DOMAIN and claims["key"] == "s"
            if [s for _, s, _, _ in self.manager.secrets][-1] == PASSWORD:
                self.manager.page = "form"
            return {"clicked": True}
        if cmd == "set_takeover":
            return {"takeover": args.get("on")}
        if cmd == "storage_state":
            return {"state": {"cookies": [{"name": "wd-session", "value": "abc", "domain": DOMAIN},
                                          {"name": "tracker", "value": "x", "domain": ".ads.example"}],
                              "origins": []}}
        if cmd in ("close", "health"):
            return {}
        raise AssertionError(f"unexpected command {cmd}")

    async def close(self) -> None:
        self.alive = False


class FakeManager:
    def __init__(self, page: str = "login"):
        self.page = page
        self.sessions: dict[tuple[int, int], FakeSession] = {}
        self.started: list[dict[str, Any]] = []
        self.secrets: list[tuple] = []
        self.pointed: list[tuple] = []
        self.parked: list[bool] = []  # whether each kept window may send forms (writes)
        self.held: list[tuple[int, bool]] = []  # (minutes, runner time extended)
        self.captcha = {"present": False, "solved": False, "kind": None}
        self.captcha_after_fill = False
        self.replace_uploads = False
        self.submitted: list[str] = []
        self.windows: list[FakeSession] = []  # every window started, closed ones included
        self.loop = None

    def available(self) -> bool:
        return True

    async def start(self, *, user_id, task_id, job_id, allowed_hosts, upload_dir, storage_state=None,
                    headless=None, allow_private=False, redirect_domains=None):
        self.loop = asyncio.get_running_loop()
        self.started.append({"user": user_id, "task": task_id, "hosts": allowed_hosts, "storage": storage_state})
        session = FakeSession(self, user_id, task_id, storage_state)
        self.sessions[(user_id, task_id)] = session
        self.windows.append(session)
        return session

    def get(self, user_id, task_id):
        session = self.sessions.get((user_id, task_id))
        if session is None or not session.alive:
            return None
        if session.parked_until is not None and session.parked_until < time.monotonic():
            return None  # kept past its time, as the real manager: the reaper closes it
        return session

    async def park(self, session, minutes):
        session.parked_until = time.monotonic() + minutes * 60
        self.parked.append(True)  # handed to the person: the window works like a normal browser
        return True

    async def hold(self, session, minutes, *, extend=False):
        session.parked_until = time.monotonic() + minutes * 60
        self.held.append((minutes, extend))

    def handed_over(self):
        now = time.monotonic()
        return [key for key, s in self.sessions.items() if s.alive and s.parked_until and s.parked_until >= now]

    async def unpark(self, session):
        session.parked_until = None

    async def close(self, user_id, task_id):
        session = self.sessions.pop((user_id, task_id), None)
        if session is not None:
            await session.close()

    async def reap(self):
        return 0

    def run_threadsafe(self, coro, timeout=60):
        return asyncio.run(coro)


@pytest.fixture
def sessions(pg_sessions, pg_engine, monkeypatch, tmp_path):
    aps.configure(monkeypatch, "assist")
    settings = aps.get_settings()
    for name, value in (("mcp_packet_dir", str(tmp_path / "packets")), ("browser_use_webui_enabled", True),
                        ("browser_use_headless", False), ("secret_key", "test-secret-for-the-vault"),
                        ("application_login_domains", DOMAIN), ("application_auth_sessions_enabled", True)):
        monkeypatch.setattr(settings, name, value)
    checkpoint.forget_setup()
    qs.clear_checkpoints(pg_engine)
    yield qs.app_sessions(pg_engine)
    bus.set_sidecars(None)


def account(sessions) -> tuple[int, int]:
    user = aps.owner(sessions)
    aps.answer(sessions, user, aps.PYTHON_QUESTION, "8")
    job_id = aps.approved_job(sessions, user, URL)
    aps.enqueue(sessions, user, job_id)
    return user, job_id


def drive(sessions) -> None:
    asyncio.run(aps.drive_async(sessions, passes=6))


def client_as(user_id: int, monkeypatch, sessions) -> TestClient:
    """The API as `user_id`, on the test database's sessions (the account filter applies as in production)."""
    from app import auth
    from app.auth import CurrentUser, get_user_db
    from app.main import app

    def user_db():
        db = sessions()
        db.info["user_id"] = user_id
        try:
            yield db
        finally:
            db.close()
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, "x@jobhunt.test", False))
    monkeypatch.setitem(app.dependency_overrides, get_user_db, user_db)
    return TestClient(app)


def test_a_login_wall_waits_in_the_window_and_the_run_resumes_in_the_same_page(sessions, monkeypatch):
    fake = FakeManager("login")
    bus.set_sidecars(fake)
    user, job_id = account(sessions)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage) == ("PENDING", "NEEDS_HUMAN")
    assert task.interrupt["takeover"] and task.interrupt["reason"] == "LOGIN_REQUIRED"
    assert task.interrupt["login_domain"] == DOMAIN
    assert fake.get(user, task.id).parked_until is not None  # the window is kept for the person
    assert fake.parked == [True]  # handed to the person, who signs in there
    drive(sessions)
    assert aps.task_of(sessions, job_id).stage == "NEEDS_HUMAN"  # nothing happens until they say so

    fake.page = "form"  # the person signs in, in the window
    before = len(fake.sessions[(user, task.id)].commands)
    response = client_as(user, monkeypatch, sessions).post(f"/api/application-executions/{task.id}/continue")
    assert response.status_code == 200, response.text
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage, task.outcome) == ("COMPLETED", "READY_TO_SUBMIT", "READY_TO_SUBMIT")
    resumed = [c for c, _ in fake.sessions[(user, task.id)].commands[before:] if c not in WATCHER_READS]
    # Picked up where the person left it (signed in, the form showing): observed afresh, never reloaded or replayed.
    assert resumed[:2] == ["unpoint", "observe"] and "goto" not in resumed
    assert "login_fill" not in resumed and "login_submit" not in resumed
    assert len(fake.started) == 1  # the same window, not a new browser

    saved = client_as(user, monkeypatch, sessions).post(f"/api/application-executions/{task.id}/save-login",
                                              json={"password": "Typed-By-Person-1"})
    assert saved.status_code == 200 and saved.json()["session_saved"], saved.text
    assert "Typed-By-Person-1" not in saved.text
    with sessions() as db:
        kept = ats_auth.session_state(db, user, DOMAIN)
        assert [c["name"] for c in kept["cookies"]] == ["wd-session"]  # nothing of another site
        assert ats_auth.login_secret(db, user, DOMAIN).password == "Typed-By-Person-1"


def test_a_saved_login_is_filled_once_on_its_own_domain_and_never_leaks(sessions, monkeypatch):
    fake = FakeManager("login")
    bus.set_sidecars(fake)
    user = aps.owner(sessions)
    with sessions() as db:
        db.info["user_id"] = user
        ats_auth.save_credential(db, user, DOMAIN, "asha@example.test", PASSWORD)
        db.commit()
    aps.answer(sessions, user, aps.PYTHON_QUESTION, "8")
    job_id = aps.approved_job(sessions, user, URL)
    aps.enqueue(sessions, user, job_id)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert task.outcome == "READY_TO_SUBMIT"
    fills = [(key, domain, shown) for key, _, domain, shown in fake.secrets]
    assert fills == [("u", DOMAIN, "[redacted]"), ("p", DOMAIN, "[redacted]")]  # redacted from logs while in use
    with sessions() as db:
        for table in ("application_execution_tasks", "application_execution_attempts", "agent_threads",
                      "agent_effects", "system_logs", "external_api_request_logs", "checkpoint_blobs",
                      "checkpoint_writes"):
            dump = " ".join(str(row) for row in db.execute(text(f"SELECT * FROM {table}")).all())
            assert PASSWORD not in dump, table


def test_a_failed_saved_login_waits_for_the_person_and_tries_only_once(sessions, monkeypatch):
    fake = FakeManager("login")
    bus.set_sidecars(fake)
    user = aps.owner(sessions)
    with sessions() as db:
        db.info["user_id"] = user
        ats_auth.save_credential(db, user, DOMAIN, "asha@example.test", "an-outdated-password")
        db.commit()
    job_id = aps.approved_job(sessions, user, URL)
    aps.enqueue(sessions, user, job_id)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.stage, task.interrupt["reason"]) == ("NEEDS_HUMAN", "LOGIN_FAILED")
    assert [cmd for cmd, _ in fake.sessions[(user, task.id)].commands].count("login_submit") == 1


def test_a_signup_gets_a_generated_password_once_and_save_login_keeps_it(sessions, monkeypatch):
    fake = FakeManager("signup")
    bus.set_sidecars(fake)
    user, job_id = account(sessions)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert task.interrupt["reason"] == "ACCOUNT_REQUIRED" and task.interrupt["takeover"]
    client = client_as(user, monkeypatch, sessions)
    issued = client.post(f"/api/application-executions/{task.id}/signup").json()
    assert issued["username"] == "asha@example.test" and len(issued["password"]) == 24
    with sessions() as db:
        row = db.scalar(select(AtsCredential))
        assert row.status == "PENDING" and issued["password"] not in row.password_encrypted
    assert client.post(f"/api/application-executions/{task.id}/save-login", json={}).status_code == 200
    listing = client.get("/api/settings/ats-credentials")
    assert issued["password"] not in listing.text and listing.json()["credentials"][0]["status"] == "ACTIVE"
    with sessions() as db:
        assert ats_auth.login_secret(db, user, DOMAIN).password == issued["password"]
        assert db.scalar(select(AtsSession)).domain == DOMAIN


def test_a_takeover_nobody_comes_back_to_ends_with_the_blocker(sessions):
    from datetime import timedelta

    bus.set_sidecars(FakeManager("login"))
    _user, job_id = account(sessions)
    drive(sessions)
    with sessions() as db:
        assert application_execution.end_expired_takeovers(db, aps.utcnow() + timedelta(minutes=16)) == 1
        task = db.scalar(select(ApplicationExecutionTask))
        assert (task.status, task.outcome, task.result_code) == ("COMPLETED", "NEEDS_HUMAN", "TAKEOVER_EXPIRED")
        job = db.get(Job, job_id)
        assert job.application.apply_blocker["reason"] == "login_required"


def test_another_account_cannot_touch_someone_elses_window(sessions, monkeypatch):
    fake = FakeManager("login")
    bus.set_sidecars(fake)
    _user, job_id = account(sessions)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    stranger = aps.owner(sessions)
    client = client_as(stranger, monkeypatch, sessions)
    for path in ("continue", "signup", "save-login"):
        response = client.post(f"/api/application-executions/{task.id}/{path}", json={"password": "x" * 10})
        assert response.status_code in (404, 409), path
    assert fake.get(stranger, task.id) is None
    assert aps.task_of(sessions, job_id).stage == "NEEDS_HUMAN"
    with sessions() as db:
        assert db.scalar(select(AtsCredential)) is None



def test_a_field_nothing_verified_fills_is_pointed_out_in_the_open_window_and_kept(sessions, monkeypatch):
    """The Python question has no verified answer: the run fills the rest, points to that field in the window and
    waits (FIELDS_NEED_YOU, its label listed). The person types in the window and continues: the page is not reloaded,
    the field is accepted as theirs (PERSON), the run ends READY_TO_SUBMIT, and what they typed can be saved."""
    fake = FakeManager("form")
    bus.set_sidecars(fake)
    user = aps.owner(sessions)  # no saved answer to the Python question
    job_id = aps.approved_job(sessions, user, URL)
    aps.enqueue(sessions, user, job_id)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage, task.interrupt["reason"]) == ("PENDING", "NEEDS_HUMAN", "FIELDS_NEED_YOU")
    assert [f["label"] for f in task.interrupt["fields"]] == [aps.PYTHON_QUESTION]
    assert task.interrupt["question_ids"] and fake.pointed[-1][0] == ["q"]
    assert "fill the 1 highlighted field" in fake.pointed[-1][1]
    assert fake.parked == [True]  # handed to the person to fill the fields (and submit, if they choose)
    client = client_as(user, monkeypatch, sessions)
    kit = client.get(f"/api/jobs/{job_id}/apply-kit").json()
    assert kit["execution_takeover"]["fields"][0]["label"] == aps.PYTHON_QUESTION
    assert [q["question"] for q in kit["execution_questions"]] == [aps.PYTHON_QUESTION]  # answer here or in the page
    session = fake.sessions[(user, task.id)]
    before = [c for c, _ in session.commands].count("goto")
    session.values["q"] = "11"  # the person types it in the window
    assert client.post(f"/api/application-executions/{task.id}/continue").status_code == 200
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage, task.outcome) == ("COMPLETED", "READY_TO_SUBMIT", "READY_TO_SUBMIT")
    assert [c for c, _ in session.commands].count("goto") == before  # picked up in place, never reloaded
    # The filled form stays open for the person to submit, like a normal browser: nothing to start first.
    assert fake.parked == [True, True] and fake.get(user, task.id).parked_until is not None
    field_id = next(f["id"] for f in task.form["fields"] if f["label"] == aps.PYTHON_QUESTION)
    assert task.plan["fields"][field_id]["status"] == "PERSON" and "11" not in str(task.plan)
    stranger = aps.owner(sessions)
    assert client_as(stranger, monkeypatch, sessions).post(
        f"/api/application-executions/{task.id}/save-window-answers").status_code == 404
    saved = client_as(user, monkeypatch, sessions).post(f"/api/application-executions/{task.id}/save-window-answers")
    assert saved.json() == {"saved": 1} and "11" not in saved.text
    with sessions() as db:
        from app.models import KnowledgeAnswer
        row = db.scalar(select(KnowledgeAnswer).where(KnowledgeAnswer.question == aps.PYTHON_QUESTION))
        assert (row.answer, row.verified) == ("11", True)


def test_without_a_visible_window_the_questions_go_to_the_apply_kit(sessions, monkeypatch):
    """Headless (or remote) installs cannot hand a page to a person: an unanswered question stops the run before the
    browser fills (NEEDS_INPUT, answered in the Apply kit), and nothing is pointed at."""
    monkeypatch.setattr(aps.get_settings(), "browser_use_headless", True)
    fake = FakeManager("form")
    bus.set_sidecars(fake)
    user = aps.owner(sessions)
    job_id = aps.approved_job(sessions, user, URL)
    aps.enqueue(sessions, user, job_id)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage) == ("PENDING", "NEEDS_INPUT") and fake.pointed == []


# ───────────── picking the page up where the person left it ─────────────

WATCHER_READS = ("evidence", "challenge")  # what the scheduler pass reads from a kept window (never an action)


def _commands_since(session: FakeSession, before: int) -> list[str]:
    """The window's commands after `before`, without the scheduler pass's reads. The session object itself: a run
    that ends closes its window."""
    return [c for c, _ in session.commands[before:] if c not in WATCHER_READS]


def _answer_saved(sessions, user: int, question: str, text: str) -> None:
    """The person answers, in the Knowledge bank, the question the run saved for them."""
    from app.models import KnowledgeAnswer
    from app.services import application_execution as ae
    from app.services.knowledge import question_key

    with sessions() as db:
        db.info["user_id"] = user
        row = db.scalar(select(KnowledgeAnswer).where(KnowledgeAnswer.question_key == question_key(question)))
        row.answer, row.verified = text, True
        ae.wake_waiting(db, user)
        db.commit()


def _solve(fake: FakeManager) -> None:
    fake.captcha = {"present": True, "solved": True, "kind": "recaptcha"}


def _watch(sessions) -> int:
    from app.services import application_execution as ae

    with sessions() as db:
        return ae.watch_challenges(db)


def test_a_captcha_solved_in_the_window_continues_by_itself_in_the_same_page(sessions):
    fake = FakeManager("form")
    fake.captcha = {"present": True, "solved": False, "kind": "recaptcha"}  # a reCAPTCHA iframe, however long the page
    bus.set_sidecars(fake)
    user, job_id = account(sessions)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage, task.interrupt["reason"]) == ("PENDING", "NEEDS_HUMAN", "CAPTCHA_OR_ANTI_BOT")
    assert _watch(sessions) == 0  # not solved yet: nothing continues
    drive(sessions)
    assert aps.task_of(sessions, job_id).stage == "NEEDS_HUMAN"

    window = fake.sessions[(user, task.id)]
    before = len(window.commands)
    _solve(fake)  # the person ticks "I'm not a robot" in the window
    assert _watch(sessions) == 1  # no Continue needed
    assert aps.task_of(sessions, job_id).interrupt["continued_by"] == "window"
    drive(sessions)
    done = aps.task_of(sessions, job_id)
    assert (done.status, done.outcome) == ("COMPLETED", "READY_TO_SUBMIT"), (done.result_code, done.last_error)
    resumed = _commands_since(window, before)
    assert "goto" not in resumed and resumed[:2] == ["unpoint", "observe"]  # the solved page, never reloaded
    assert len(fake.started) == 1 and done.agent_thread_id == task.agent_thread_id


@pytest.mark.parametrize("replaces_uploads", [False, True], ids=["file-input-kept", "file-input-replaced"])
def test_auto_submit_waits_at_a_captcha_and_submits_once_in_the_same_page_when_it_is_solved(sessions, monkeypatch,
                                                                                          replaces_uploads):
    from app.models import SubmissionIntent
    from tests import guarded_support as gs

    monkeypatch.setattr(aps.get_settings(), "application_execution_mode", "guarded_auto")
    fake = FakeManager("form")
    fake.captcha_after_fill = True  # the site shows its CAPTCHA once the form is filled, above Submit
    fake.replace_uploads = replaces_uploads  # some boards swap the file input for the uploaded file's name
    bus.set_sidecars(fake)
    user = aps.owner(sessions)
    aps.answer(sessions, user, aps.PYTHON_QUESTION, "8")
    gs.consent(sessions, user)
    job_id = aps.approved_job(sessions, user, URL)
    aps.enqueue(sessions, user, job_id, mode="GUARDED_AUTO")
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    # The SUBMIT step waits for the person like any other (it used to end the run instead).
    assert (task.status, task.stage, task.interrupt["reason"]) == ("PENDING", "NEEDS_HUMAN", "CAPTCHA_OR_ANTI_BOT")
    assert fake.submitted == []

    window = fake.sessions[(user, task.id)]
    before = len(window.commands)
    _solve(fake)
    assert _watch(sessions) == 1
    drive(sessions)
    done = aps.task_of(sessions, job_id)
    assert (done.status, done.outcome) == ("COMPLETED", "CONFIRMED"), (done.result_code, done.last_error)
    assert fake.submitted == ["go"]  # one signed click, after every check ran again
    assert "goto" not in _commands_since(window, before)
    assert done.agent_thread_id == task.agent_thread_id  # the same run, resumed: never started over
    with sessions() as db:
        db.info["user_id"] = user
        intents = db.scalars(select(SubmissionIntent).where(SubmissionIntent.execution_task_id == task.id)).all()
        assert [i.status for i in intents] == ["CONFIRMED"]


def test_the_graph_can_pause_at_submit_and_resume_there():
    from app.agents import application

    edges = {(e.source, e.target) for e in application.build(None).get_graph().edges}
    assert ("submit", "wait") in edges and ("wait", "submit") in edges


def test_a_sign_in_that_leaves_the_form_behind_loads_the_apply_url_again(sessions, monkeypatch):
    fake = FakeManager("login")
    bus.set_sidecars(fake)
    user, job_id = account(sessions)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert task.interrupt["reason"] == "LOGIN_REQUIRED"
    fake.page = "landing"  # signed in; the site shows its home page, not the form
    window = fake.sessions[(user, task.id)]
    before = len(window.commands)
    assert client_as(user, monkeypatch, sessions).post(
        f"/api/application-executions/{task.id}/continue").status_code == 200
    drive(sessions)
    done = aps.task_of(sessions, job_id)
    assert (done.status, done.outcome) == ("COMPLETED", "READY_TO_SUBMIT")
    resumed = _commands_since(window, before)
    assert resumed[:3] == ["unpoint", "observe", "goto"]  # looked where the person left it first, then the apply URL


def test_a_captcha_window_of_another_account_is_never_continued(sessions):
    from app.services import application_execution as ae

    fake = FakeManager("form")
    fake.captcha = {"present": True, "solved": False, "kind": "recaptcha"}
    bus.set_sidecars(fake)
    _user, job_id = account(sessions)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    _solve(fake)
    other = aps.owner(sessions)
    with sessions() as db:
        db.info["user_id"] = other
        assert ae.watch_challenges(db) == 0  # an account's session only looks at its own windows
        assert ae.challenge_solved(db, other, task.id) is False
    assert aps.task_of(sessions, job_id).stage == "NEEDS_HUMAN"


def test_waiting_for_an_answer_keeps_the_read_form_and_fills_it_in_place(sessions, monkeypatch):

    monkeypatch.setattr(aps.get_settings(), "browser_use_headless", True)  # no window: the Apply kit asks instead
    fake = FakeManager("form")
    bus.set_sidecars(fake)
    user = aps.owner(sessions)
    job_id = aps.approved_job(sessions, user, URL)
    aps.enqueue(sessions, user, job_id)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert task.stage == "NEEDS_INPUT"
    kept = fake.sessions[(user, task.id)]
    assert kept.resume_in_place and fake.held[-1] == (aps.get_settings().application_input_hold_minutes, True)

    before = len(kept.commands)
    _answer_saved(sessions, user, aps.PYTHON_QUESTION, "8")
    assert aps.task_of(sessions, job_id).stage == "ANSWERING"  # woken by the answer
    drive(sessions)
    done = aps.task_of(sessions, job_id)
    assert (done.status, done.outcome) == ("COMPLETED", "READY_TO_SUBMIT"), (done.result_code, done.last_error)
    assert "goto" not in _commands_since(kept, before) and len(fake.started) == 1


def test_an_answer_after_the_hold_ran_out_reads_the_form_again(sessions, monkeypatch):

    monkeypatch.setattr(aps.get_settings(), "browser_use_headless", True)
    fake = FakeManager("form")
    bus.set_sidecars(fake)
    user = aps.owner(sessions)
    job_id = aps.approved_job(sessions, user, URL)
    aps.enqueue(sessions, user, job_id)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    held = fake.sessions[(user, task.id)]
    held.parked_until = time.monotonic() - 1  # the hold ran out
    before = len(held.commands)
    _answer_saved(sessions, user, aps.PYTHON_QUESTION, "8")
    drive(sessions)
    done = aps.task_of(sessions, job_id)
    assert done.outcome == "READY_TO_SUBMIT" and len(fake.started) == 2  # a new window, the apply URL loaded again
    assert _commands_since(held, before) == []  # the expired window was not reused
    assert "goto" in [c for c, _ in fake.windows[-1].commands]


def test_a_solved_challenge_that_removes_its_widget_still_continues(sessions):
    fake = FakeManager("form")
    fake.captcha = {"present": True, "solved": False, "kind": "recaptcha"}
    bus.set_sidecars(fake)
    _user, job_id = account(sessions)
    drive(sessions)
    assert _watch(sessions) == 0  # the widget is there, unanswered
    fake.captcha = {"present": False, "solved": False, "kind": None}  # solving it took the widget away
    assert _watch(sessions) == 1
    drive(sessions)
    assert aps.task_of(sessions, job_id).outcome == "READY_TO_SUBMIT"


def test_a_captcha_only_the_page_text_named_waits_for_continue(sessions):
    fake = FakeManager("form")
    bus.set_sidecars(fake)
    HTML_WITH_TEXT = "<form><input name=n></form><p>Please complete the captcha to continue</p>"
    _user, job_id = account(sessions)
    original, HTML["form"] = HTML["form"], HTML_WITH_TEXT
    try:
        drive(sessions)
        task = aps.task_of(sessions, job_id)
        assert task.interrupt["reason"] == "CAPTCHA_OR_ANTI_BOT"
        assert _watch(sessions) == 0 and _watch(sessions) == 0  # no widget was ever seen: nothing to read as solved
        assert aps.task_of(sessions, job_id).stage == "NEEDS_HUMAN"
    finally:
        HTML["form"] = original
