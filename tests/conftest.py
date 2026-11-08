"""Isolate the test suite from a developer's .env before any app module is imported.

Settings are read once and cached, and .env may point at a real database, real API keys and
basic-auth credentials. Tests always use TEST_DATABASE_URL (or throwaway SQLite), the mock LLM,
no Apify token and no login.
"""
import os

os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "sqlite:///./test_suite.db")
os.environ["DOMAIN_CLASSIFIER_MODE"] = "off"  # a developer's .env must not change what these tests read
os.environ["DOMAIN_CLASSIFIER_SHADOW_DAILY_LIMIT"] = "0"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["SCHEDULER_ENABLED"] = "false"  # tests start schedulers explicitly
os.environ["TAILOR_WORKER_ENABLED"] = "false"  # tests run tailoring tasks with tailor_tasks.run_pending
# No automatic job-description recovery: nothing in the suite may queue a fetch of a real posting URL. The recovery
# tests turn it on and serve their pages from a local HTTP server.
os.environ["JD_FETCH_ENABLED"] = "false"
os.environ["PLAYWRIGHT_HEADLESS"] = "true"
os.environ["SECRET_KEY"] = "test-secret-key-for-encrypting-api-keys"
for key in ("APIFY_API_TOKEN", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
            "ADMIN_TOKEN", "JOBHUNT_MCP_TOKEN", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
            "GOOGLE_OAUTH_REDIRECT_URI"):
    os.environ[key] = ""
# No real Gmail: the inbox tests turn the worker on and serve Google from tests/gmail_fake.py.
os.environ["APPLICATION_INBOX_ENABLED"] = "false"
# Agent graphs and their LangSmith export are off; the agent tests turn them on and never reach LangSmith.
os.environ["AGENT_GRAPHS_ENABLED"] = "false"
os.environ["AGENT_LANGSMITH_EXPORT_ENABLED"] = "false"
os.environ["AGENT_LANGSMITH_API_KEY"] = ""
for key in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
    os.environ.pop(key, None)

import pytest  # noqa: E402
from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

TEST_USER_EMAIL = "tester@jobhunt.test"


def ensure_test_user():
    """The account that API tests act as unless they sign in or opt out with the `anonymous` fixture.

    Looked up (and recreated if a test dropped the tables) on each use, never cached.
    """
    from app.auth import CurrentUser
    from app.db import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == TEST_USER_EMAIL))
        if user is None:
            user = User(email=TEST_USER_EMAIL, password_hash="!", is_admin=True)  # "!" never matches a password
            db.add(user)
            db.commit()
            db.refresh(user)
        return CurrentUser(user.id, user.email, user.is_admin)


def make_user(sessions, email=None, is_admin=False) -> int:
    """Create an account in any database (e.g. the PostgreSQL workflow schema) and return its id."""
    import uuid

    from app.models import User

    with sessions() as db:
        user = User(email=email or f"{uuid.uuid4().hex[:10]}@jobhunt.test", password_hash="!", is_admin=is_admin)
        db.add(user)
        db.commit()
        return user.id


@pytest.fixture(autouse=True)
def signed_in_as_test_user(monkeypatch):
    """Requests without a session cookie act as the test account; real cookies still sign in normally."""
    from app import auth

    real = auth.session_user
    monkeypatch.setattr(auth, "authenticate", lambda request: real(request) or ensure_test_user())


@pytest.fixture(autouse=True)
def lenient_resume_policy(request, monkeypatch):
    """Tests written before resume verification tailor from unverified resume text, as if the candidate had allowed
    it. Tests marked `strict_resume_policy` use the production default: verified facts only."""
    if request.node.get_closest_marker("strict_resume_policy"):
        return
    from app.services import user_settings

    monkeypatch.setitem(user_settings.CANDIDATE_POLICY_DEFAULTS, "allow_unverified_resume_facts", True)


# A completed fit analysis, as the Ranker stores it (app.services.ranker.legacy_fit_analysis), for tests that tailor
# a job without running the PostgreSQL-only ranker first.
COMPLETED_FIT_ANALYSIS = {
    "score": 80, "overall_fit_score": 80, "candidate_fit_score": 82, "resume_fit_score": 74, "confidence": 0.8,
    "summary": "Good overall fit.", "strong_matches": ["Python"], "partial_matches": [], "missing_requirements": [],
    "fatal_gaps": [], "resume_gaps": ["Production ownership is not quantified"],
    "recommended_resume_focus": ["Lead with production ownership"], "requirement_assessments": [],
}


@pytest.fixture(autouse=True)
def ranked_before_tailoring(request, monkeypatch):
    """Tailoring needs the Ranker's completed FitAnalysis (RANKING_REQUIRED otherwise), and ranking needs PostgreSQL.
    Tests written before that rule tailor jobs the ranker never saw, as if it had ranked them: a job without a
    completed analysis reads as COMPLETED_FIT_ANALYSIS merged with what it has. Tests marked `strict_ranking` get the
    production rule."""
    if request.node.get_closest_marker("strict_ranking"):
        return
    from app.services import context_builder

    def stored(job):
        analysis = job.fit_analysis or {}
        complete = all(analysis.get(key) is not None for key in context_builder.REQUIRED_FIT_FIELDS)
        return analysis if complete else {**COMPLETED_FIT_ANALYSIS, **analysis}
    monkeypatch.setattr(context_builder, "stored_fit_analysis", stored)


@pytest.fixture(autouse=True)
def qualified_before_approval(request, monkeypatch):
    """Approval moves the job's workflow state to READY_TO_APPLY and refuses a job whose state cannot get there
    (app.services.approval). Tests written before that rule approve jobs added by hand that the ranker never saw, as
    if it had qualified them: a job still before ranking (DISCOVERED, NORMALIZED, FILTERED) reads as QUALIFIED when it
    is approved. Tests marked `strict_approval` get the production rule."""
    if request.node.get_closest_marker("strict_approval"):
        return
    from app.services import approval
    from app.services.lifecycle import WorkflowState as S
    from app.services.lifecycle import parse_state

    original = approval.path_to_ready

    def path(db, job, variant, **kwargs):
        if parse_state(job.state) in (S.DISCOVERED, S.NORMALIZED, S.FILTERED):
            from sqlalchemy.orm.attributes import set_committed_value

            set_committed_value(job, "state", S.QUALIFIED)  # as if ranked (the lifecycle guard is for real moves)
        return original(db, job, variant, **kwargs)
    monkeypatch.setattr(approval, "path_to_ready", path)


@pytest.fixture
def anonymous(monkeypatch):
    """Turn off the test account: requests are signed in only by a real session cookie."""
    from app import auth

    monkeypatch.setattr(auth, "authenticate", auth.session_user)
    auth._failures.clear()

# The workflow-state tests wipe and re-migrate their database. They get their own schema so the
# app tests, which share TEST_DATABASE_URL through the public schema, keep their tables.
WORKFLOW_TEST_SCHEMA = "jobhunt_workflow_tests"


@pytest.fixture(scope="session")
def pg_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL database to run the workflow-state tests")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or "test" not in (parsed.database or ""):
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL database whose name contains 'test'")
    return url


def reset_schema(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {WORKFLOW_TEST_SCHEMA} CASCADE"))
        connection.execute(text(f"CREATE SCHEMA {WORKFLOW_TEST_SCHEMA}"))


@pytest.fixture(scope="session")
def pg_engine(pg_url):
    from app.db_migrations import run_migrations

    engine = create_engine(pg_url, pool_size=20, max_overflow=20,
                           connect_args={"options": f"-csearch_path={WORKFLOW_TEST_SCHEMA}"})
    reset_schema(engine)
    run_migrations(engine)
    yield engine
    with engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {WORKFLOW_TEST_SCHEMA} CASCADE"))
    engine.dispose()


@pytest.fixture
def pg_sessions(pg_engine):
    """A session factory on a migrated PostgreSQL schema with empty jobs and scheduler tables."""
    with pg_engine.begin() as connection:
        connection.execute(text("TRUNCATE jobs, scheduler_jobs, users RESTART IDENTITY CASCADE"))
    return sessionmaker(bind=pg_engine, autoflush=False)


# LiteLLM gateway tests (tests/litellm_fake.py): `litellm` serves an in-process LiteLLM proxy through the
# instrumented client, `account` is a freshly signed-up account with its LiteLLM key saved.
from tests.litellm_fake import account, litellm  # noqa: E402,F401
