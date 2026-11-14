"""LangSmith export (app.agents.tracing_boundary): off, on and unavailable; no credential ever leaves the process.

Nothing here reaches LangSmith: `requests.Session.send`, the transport under the instrumented session, is replaced by
a recorder, so the assertions are on the exact bytes that would have been sent.
"""
from __future__ import annotations

import json
from typing import Any, TypedDict

import pytest
import requests
from langgraph.graph import END, START, StateGraph

from app.agents import export_policy, runtime, tracing_boundary
from app.db import SessionLocal
from app.main import app  # noqa: F401  (migrates the test database)
from app.models import ExternalAPIRequestLog
from app.settings import get_settings
from tests import agents_support as support
from tests.conftest import make_user

S = support.SECRETS


class Wire:
    """Stands in for the network under the instrumented session."""

    def __init__(self, mode: str = "ok"):
        self.mode, self.bodies, self.headers = mode, [], []

    def send(self, session: Any, request: Any, **kwargs: Any) -> requests.Response:
        body = request.body.encode() if isinstance(request.body, str) else (request.body or b"")
        self.bodies.append(bytes(body))
        self.headers.append(dict(request.headers))
        if self.mode == "down":
            raise requests.ConnectionError("connection refused")
        if self.mode == "timeout":
            raise requests.Timeout("read timed out")
        response = requests.Response()
        response.status_code = {"503": 503, "quota": 429, "429": 429}.get(self.mode, 200)
        response._content, response.request, response.url = b"{}", request, request.url
        if self.mode == "quota":
            response._content = b'{"error":"Monthly unique traces usage limit exceeded"}'
        return response

    def text(self) -> str:
        return b"\n".join(self.bodies).decode("utf-8", "replace")


@pytest.fixture
def wire(monkeypatch):
    recorder = Wire()
    monkeypatch.setattr(requests.Session, "send", lambda self, request, **kw: recorder.send(self, request, **kw))
    tracing_boundary.reset()
    yield recorder
    tracing_boundary.flush()
    tracing_boundary.reset()


@pytest.fixture
def export_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_graphs_enabled", True)
    monkeypatch.setattr(settings, "agent_langsmith_export_enabled", True)
    monkeypatch.setattr(settings, "agent_langsmith_api_key", "lsv2_pt_SYNTHETIC_langsmith_key")
    # Every debug and capture switch on: none of them may widen what is exported.
    monkeypatch.setattr(settings, "llm_prompt_debug", True)
    monkeypatch.setattr(settings, "external_api_debug_payloads", True)


def run_pilot(user_id: int, kind: str = "review") -> int:
    saver = support.memory_saver()
    pk = support.start(SessionLocal, user_id, {"kind": kind, "items": 3})
    runtime.execute(SessionLocal, support.claim(SessionLocal, user_id, pk), saver=saver)
    if kind == "review":
        run, command = support.resume(SessionLocal, user_id, pk, "approve")
        runtime.execute(SessionLocal, run, command, saver=saver)
    tracing_boundary.flush()
    return pk


def export_rows(since_id: int) -> list[ExternalAPIRequestLog]:
    with SessionLocal() as db:
        return list(db.query(ExternalAPIRequestLog).filter(ExternalAPIRequestLog.provider == "LANGSMITH",
                                                           ExternalAPIRequestLog.id > since_id))


def last_log_id() -> int:
    with SessionLocal() as db:
        return db.query(ExternalAPIRequestLog.id).order_by(ExternalAPIRequestLog.id.desc()).limit(1).scalar() or 0


def assert_no_secret(text: str) -> None:
    for name, value in S.items():
        if name == "otp":
            assert '"otp"' not in text and "482915" not in text
        else:
            assert value not in text and value.split("=")[-1] not in text, name
    for field in ('"authorization"', '"cookie"', '"password"', '"api_key"', '"storage_state"', '"access_token"'):
        assert field not in text.lower(), field


# ───────────── off ─────────────

def test_disabled_export_sends_nothing_even_when_the_environment_asks_for_tracing(wire, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_SYNTHETIC_env_key")
    monkeypatch.setattr(get_settings(), "agent_graphs_enabled", True)
    made = []
    import langsmith

    original = langsmith.Client.__init__
    monkeypatch.setattr(langsmith.Client, "__init__", lambda self, *a, **k: made.append(1) or original(self, *a, **k))
    pk = run_pilot(make_user(SessionLocal))
    assert support.thread(SessionLocal, pk).status == "COMPLETED"
    assert wire.bodies == [] and made == []
    assert tracing_boundary.configured() is False


def test_export_needs_both_the_switch_and_a_key(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_langsmith_export_enabled", True)
    assert tracing_boundary.configured() is False  # no key
    monkeypatch.setattr(settings, "agent_langsmith_export_enabled", False)
    monkeypatch.setattr(settings, "agent_langsmith_api_key", "lsv2_pt_SYNTHETIC")
    assert tracing_boundary.configured() is False  # no switch


# ───────────── on ─────────────

def test_enabled_export_sends_safe_metadata_only(wire, export_on):
    before = last_log_id()
    pk = run_pilot(make_user(SessionLocal))
    assert support.thread(SessionLocal, pk).status == "COMPLETED"
    text = wire.text()
    assert wire.bodies and "/runs" in str(export_rows(before)[0].endpoint_path)
    assert '"langgraph_node":"approval"' in text and '"graph":"pilot"' in text  # the useful part arrives
    thread_id = support.thread(SessionLocal, pk).thread_id
    assert thread_id not in text and export_policy.pseudonym(thread_id) in text  # the owner is not named
    assert '"user_id"' not in text and '"runtime"' not in text
    assert all(not body.startswith(b"\x28\xb5\x2f\xfd") for body in wire.bodies)  # never compressed
    rows = export_rows(before)
    assert rows and all(r.status == "SUCCEEDED" for r in rows)
    assert all(r.request_metadata is None or "payload" not in json.dumps(r.request_metadata) for r in rows)


class LeakyState(TypedDict, total=False):
    kind: str
    items: int
    note: str
    password: str
    authorization: str
    cookie: str
    otp: str
    storage_state: dict


def leaky_graph(fail: bool):
    """A graph whose state, metadata and errors are full of credentials: what a buggy future graph might do."""
    def node(state: LeakyState) -> dict:
        if fail:
            raise RuntimeError(f"upstream said 401 for {S['bearer']} cookie={S['cookie']} key={S['openai']}")
        return {"note": f"token {S['google_access']} and {S['anthropic']}", "items": 2}
    graph = StateGraph(LeakyState)
    graph.add_node("leak", node)
    graph.add_edge(START, "leak")
    graph.add_edge("leak", END)
    return graph.compile()


@pytest.mark.parametrize("fail", [False, True], ids=["success", "error"])
def test_no_credential_reaches_langsmith_even_from_a_leaky_graph(wire, export_on, fail):
    state = {"kind": "auto", "items": 1, "note": f"refresh {S['google_refresh']} jwt {S['jwt']}",
             "password": S["password"], "authorization": S["bearer"], "cookie": S["cookie"], "otp": S["otp"],
             "storage_state": {"cookies": [{"name": "sid", "value": S["cookie"]}], "origins": []}}
    config = {"configurable": {"thread_id": "pilot:u1:" + "a" * 32 + ":v1", "api_key": S["openai"],
                               "client_secret": S["google_client_secret"]},
              "metadata": {"authorization": S["bearer"], "graph": "leaky", "note": S["anthropic"],
                           "trace_id": f"trace {S['jwt']}"},
              "tags": [f"key:{S['openai']}", "safe-tag"], "run_name": f"run {S['openai']}"}
    with tracing_boundary.export(("kind", "items", "note")) as callbacks:
        config["callbacks"] = callbacks
        try:
            leaky_graph(fail).invoke(state, config=config)
        except RuntimeError:
            assert fail
    tracing_boundary.flush()
    assert wire.bodies, "the run should still be exported (safely)"
    text = wire.text()
    assert_no_secret(text)
    assert '"safe-tag"' in text and '"graph":"leaky"' in text
    if fail:
        assert '"RuntimeError"' in text and "401" not in text  # the class name only


def test_the_wire_check_refuses_what_the_projection_missed():
    assert export_policy.wire_check(b'{"inputs": {"kind": "auto"}}') is None
    assert export_policy.wire_check(None) is None
    for body in (json.dumps({"x": S["openai"]}).encode(), b'{"password": "x"}', b'{"Authorization": "x"}',
                 b'{"storage_state": {}}', b"\x28\xb5\x2f\xfdcompressed", b"\xff\xfe\x00binary"):
        assert export_policy.wire_check(body) is not None, body


def test_a_refused_body_is_never_sent_and_is_recorded(wire, monkeypatch):
    from app.services.external_api import requests_session

    before = last_log_id()
    session = requests_session("LANGSMITH", service="runs", operation="trace_export",
                               pre_send=export_policy.wire_check)
    response = session.post("https://api.smith.langchain.com/runs", data=json.dumps({"note": S["anthropic"]}))
    assert response.status_code == 400 and wire.bodies == []
    rows = export_rows(before)
    assert len(rows) == 1 and rows[0].error_type == "CLIENT_ERROR" and rows[0].http_status_code is None
    assert rows[0].request_metadata == {"blocked_by": "pre_send_policy"}
    assert S["anthropic"] not in (rows[0].error_message or "")


def test_a_body_the_check_cannot_read_is_never_sent(wire):
    """A streamed body (LangSmith streams multipart batches over its inline limit) fails closed."""
    from app.services.external_api import requests_session

    before = last_log_id()
    session = requests_session("LANGSMITH", service="runs", operation="trace_export",
                               pre_send=export_policy.wire_check)
    response = session.post("https://api.smith.langchain.com/runs/multipart",
                            data=iter([json.dumps({"note": S["anthropic"]}).encode()]))
    assert response.status_code == 400 and wire.bodies == []
    rows = export_rows(before)
    assert len(rows) == 1 and rows[0].request_metadata == {"blocked_by": "pre_send_policy"}


# ───────────── unavailable ─────────────

@pytest.mark.parametrize("mode", ["down", "timeout", "503"])
def test_a_langsmith_outage_changes_nothing(wire, export_on, mode):
    wire.mode = mode
    before = last_log_id()
    user = make_user(SessionLocal)
    pk = run_pilot(user)
    row = support.thread(SessionLocal, pk)
    assert row.status == "COMPLETED" and row.result["outcome"] == "completed"
    assert len(support.effects(SessionLocal, pk)) == 1
    rows = export_rows(before)
    assert rows and all(r.status == "FAILED" for r in rows)
    assert {r.error_type for r in rows} <= {"CONNECTION", "TIMEOUT", "SERVER_ERROR", "OTHER", "SDK_ERROR"}


# ───────────── rate limited: one 429, then a pause ─────────────

def test_a_spent_quota_pauses_export_instead_of_failing_on_every_run(wire, export_on, monkeypatch):
    wire.mode = "quota"
    before = last_log_id()
    user = make_user(SessionLocal)
    pk = run_pilot(user)
    tracing_boundary.flush()
    assert support.thread(SessionLocal, pk).status == "COMPLETED"  # the run itself is untouched
    assert tracing_boundary.paused() and "usage limit" in tracing_boundary.paused()
    sent, rows = len(wire.bodies), export_rows(before)
    assert sent == 1 and [r.error_type for r in rows] == ["RATE_LIMIT"]  # the SDK's retries were not sent

    made = []
    monkeypatch.setattr(tracing_boundary, "client", lambda fields: made.append(fields))
    assert support.thread(SessionLocal, run_pilot(user)).status == "COMPLETED"
    tracing_boundary.flush()
    assert not made and len(wire.bodies) == sent and len(export_rows(before)) == 1  # no call, no new row


def test_a_pause_ends_and_export_starts_again(wire, export_on, monkeypatch):
    tracing_boundary.pause(120, "rate limited")
    user = make_user(SessionLocal)
    run_pilot(user)
    tracing_boundary.flush()
    assert not wire.bodies
    clock = tracing_boundary.time.monotonic()
    monkeypatch.setattr(tracing_boundary.time, "monotonic", lambda: clock + 121)
    assert tracing_boundary.paused() is None
    run_pilot(user)
    tracing_boundary.flush()
    assert wire.bodies


@pytest.mark.parametrize("mode, retry_after, seconds", [
    ("429", None, tracing_boundary.RATE_LIMIT_PAUSE_SECONDS),  # an ordinary rate limit: a short pause
    ("429", "900", 900),                                        # the provider's own figure wins when longer
    ("quota", None, 3600), ("quota", "30", 3600),               # a spent plan does not come back in 30 seconds
    ("quota", "999999", tracing_boundary.MAX_PAUSE_SECONDS),    # and nothing pauses export for ever
])
def test_how_long_a_429_pauses_export(wire, monkeypatch, mode, retry_after, seconds):
    from app.services.external_api import requests_session

    paused_for = []
    monkeypatch.setattr(tracing_boundary, "pause", lambda secs, reason: paused_for.append(secs))
    wire.mode = mode
    if retry_after:
        original = wire.send

        def send(session, request, **kw):
            response = original(session, request, **kw)
            response.headers["Retry-After"] = retry_after
            return response
        wire.send = send
    session = requests_session("LANGSMITH", operation="trace_export", on_response=tracing_boundary._on_response)
    assert session.post("https://api.smith.langchain.com/runs/batch", data=b"{}").status_code == 429
    assert [min(s, tracing_boundary.MAX_PAUSE_SECONDS) for s in paused_for] == [seconds]


def test_a_paused_session_sends_and_stores_nothing(wire):
    from app.services.external_api import requests_session

    before = last_log_id()
    session = requests_session("LANGSMITH", operation="trace_export", paused=lambda: "paused for a test")
    assert session.post("https://api.smith.langchain.com/runs/batch", data=b"{}").status_code == 400
    assert not wire.bodies and not export_rows(before)


# ───────────── Phase 7: shapes a projection alone used to let through ─────────────

# Credentials with no tell-tale name: a one-time code, an opaque session token, a LangSmith key, a cookie line.
UNNAMED = {"otp": "482915", "opaque": "Qx7Lm2Np9Rs4Tv6Wy8Za1Bc3De5", "langsmith": "lsv2_sk_SYNTHETICabcdefghijklmnop0123",
           "cookie_line": "Set-Cookie: csrftoken=SYNTHETICcsrf0123456789; Path=/"}


def unnamed_graph():
    """Plain-looking values in fields the graph allowlisted: only the value's shape gives them away."""
    def node(state: LeakyState) -> dict:
        return {"kind": UNNAMED["opaque"], "note": UNNAMED["otp"], "items": 1}
    graph = StateGraph(LeakyState)
    graph.add_node("leak", node)
    graph.add_edge(START, "leak")
    graph.add_edge("leak", END)
    return graph.compile()


def test_unnamed_credentials_in_allowlisted_fields_never_reach_langsmith(wire, export_on, monkeypatch):
    # Every debug and capture switch is on (export_on), plus an account's own capture: none widens the export.
    monkeypatch.setattr(get_settings(), "external_api_debug_payloads", True)
    config = {"configurable": {"thread_id": "pilot:u1:" + "b" * 32 + ":v1"},
              "metadata": {"graph": "unnamed", "worker": UNNAMED["langsmith"], "prompt_name": UNNAMED["otp"],
                           "trace_id": UNNAMED["cookie_line"]}, "tags": ["safe-tag"]}
    with tracing_boundary.export(("kind", "note", "items")) as callbacks:
        config["callbacks"] = callbacks
        unnamed_graph().invoke({"kind": "auto", "note": UNNAMED["otp"], "items": 0}, config=config)
    tracing_boundary.flush()
    text = wire.text()
    assert wire.bodies and '"graph":"unnamed"' in text
    for name, value in UNNAMED.items():
        assert value not in text, name
    assert "SYNTHETICcsrf" not in text and "482915" not in text
    assert all(set(h) & {"Cookie", "cookie", "Authorization"} == set() for h in wire.headers)


def test_the_request_check_refuses_credentials_in_urls_and_headers():
    ok = "https://api.smith.langchain.com/runs/multipart"
    assert export_policy.request_check(ok, {"x-api-key": "lsv2_pt_x", "Content-Type": "application/json"}) is None
    assert export_policy.request_check(ok, {"Cookie": "sid=1"}) == "credential header"
    assert export_policy.request_check(ok, {"Authorization": "Bearer x"}) == "credential header"
    assert export_policy.request_check(ok + "?api_key=abc", {}) == "credential field in the URL"
    assert export_policy.request_check(ok + "?code=482915", {}) == "secret-shaped value in the URL"
    assert export_policy.request_check("https://user:pw@api.example.test/runs", {}) == "credential in the URL"
    assert export_policy.request_check(ok + "?session=" + UNNAMED["opaque"], {}) is not None
    assert export_policy.wire_check(UNNAMED["cookie_line"].encode()) is not None
    assert export_policy.wire_check(b'{"note": "sessionid=SYNTHETICcookie12345; Path=/"}') == \
        "cookie or session material"
    assert export_policy.wire_check(json.dumps({"note": UNNAMED["langsmith"]}).encode()) == "secret-shaped text"


def test_a_request_refused_for_its_url_is_never_sent(wire, monkeypatch):
    from app.services.external_api import requests_session

    session = requests_session("LANGSMITH", operation="trace_export", pre_send=export_policy.wire_check,
                               pre_send_request=export_policy.request_check)
    response = session.get("https://api.example.test/runs?token=" + UNNAMED["opaque"])
    assert response.status_code == 400 and wire.bodies == []


def test_projection_keeps_ids_and_models_but_not_codes_or_tokens():
    keep = export_policy._plain
    assert keep("ranker-main") == "ranker-main" and keep("anthropic/claude-sonnet-4-5") is not None
    assert keep("3f2c" * 8) is not None  # a lower-case hex id (trace ids, digests) is an id, not a token
    assert keep("v3") == "v3" and keep(482915) == 482915  # a count is a number
    assert keep("482915") is None and keep(UNNAMED["opaque"]) is None and keep(UNNAMED["langsmith"]) is None


CONTENT_WORDS = ("resume", "description", "answer", "body", "text", "prompt", "response", "email", "subject",
                 "sender", "html", "page", "value", "letter", "summary", "cookie", "password", "token", "url", "name")
ID_SUFFIXES = ("id", "ids", "count", "version", "hash", "status", "score", "choice", "fingerprint")


@pytest.mark.parametrize("graph", sorted(runtime.GRAPHS))
def test_every_graph_exports_only_ids_enums_and_counts(graph):
    """A graph's EXPORT_FIELDS are the only state keys a trace may show: never a credential or a content field."""
    from app.agents import safety

    fields = runtime.GRAPHS[graph].export_fields
    assert fields and len(fields) == len(set(fields))
    for name in fields:
        assert not safety.denied_key(name), name
        parts = name.lower().split("_")
        # "resume_id", "resume_version", "resume_hash" name a row, not its text.
        assert not set(parts) & set(CONTENT_WORDS) or parts[-1] in ID_SUFFIXES, f"{graph}.{name} names content"
