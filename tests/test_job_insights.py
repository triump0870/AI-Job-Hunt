"""Job Insights end to end: queueing on approval, claiming, sources (faked), matching, compensation, synthesis
labels, provenance, caching, failure handling, tenancy, telemetry and the API.

Nothing here reaches Glassdoor, PayScale, Apify or a company website: sources are fakes, Apify answers through an
httpx MockTransport, and the model is the no-cost mock provider (or a stub returning a crafted answer)."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete, select

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app as web_app
from app.models import (
    Application,
    ExternalAPIRequestLog,
    InsightObservation,
    Job,
    JobInsight,
    JobInsightTask,
    LLMRequestLog,
    ResumeVariant,
)
from app.prompts.contracts import CompanyInsightSynthesis, InterviewPrepResult
from app.services import compensation, workers, workflow
from app.services.job_insights import apify, matching, service, synthesis, tasks
from app.services.job_insights.base import (
    FAILED,
    NO_DATA,
    NOT_CONFIGURED,
    OK,
    ObservationIn,
    SourceResult,
)
from app.services.job_insights.sources import PostingSource, RecruiterSource, extract_company_page
from app.settings import get_settings
from tests.conftest import ensure_test_user, make_user

SOURCE = "test:job-insights"


# ───────────── fixtures and helpers ─────────────

class FakeSource:
    """A source adapter that answers what the test says, and counts how often it was asked."""

    def __init__(self, name, observations=(), *, status=OK, source_type="GLASSDOOR", company_level=False,
                 cacheable=False, ttl="compensation", raises=None, not_configured=None, detail=None):
        self.name, self.label, self.source_type = name, name.replace("_", " ").title(), source_type
        self.company_level, self.cacheable, self.ttl = company_level, cacheable, ttl
        self.observations, self.status, self.raises = list(observations), status, raises
        self.not_configured, self.detail, self.calls = not_configured, detail, 0

    def configured(self):
        return self.not_configured

    def fetch(self, snapshot):
        self.calls += 1
        if self.raises:
            raise self.raises
        key = matching.cache_key(self.name, snapshot.target, company_level=self.company_level)
        return SourceResult(self.name, self.status, [ObservationIn(**{**o.__dict__, "cache_key": key})
                                                     for o in self.observations], detail=self.detail)


def obs(category, source_type, data, confidence=0.8, **extra):
    return ObservationIn(category, source_type, data, confidence, recency=extra.pop("recency", "HIGH"),
                         match_scope=extra.pop("match_scope", "COMPANY_ROLE_CITY"), **extra)


def salary(source_type, low, high, currency="SGD", period="year", basis="BASE", confidence=0.8, sample_size=None,
           **extra):
    return obs("COMPENSATION", source_type, {"low": low, "high": high, "currency": currency, "period": period,
                                             "basis": basis, "sample_size": sample_size}, confidence, **extra)


@pytest.fixture
def owner():
    return ensure_test_user().id


def _clean(owner: int) -> None:
    with SessionLocal() as db:
        jobs = select(Job.id).where(Job.source == SOURCE)
        insights = select(JobInsight.id).where(JobInsight.job_id.in_(jobs))
        db.execute(delete(InsightObservation).where(InsightObservation.job_insight_id.in_(insights)))
        db.execute(delete(JobInsight).where(JobInsight.job_id.in_(jobs)))
        db.execute(delete(JobInsightTask).where(JobInsightTask.job_id.in_(jobs)))
        db.execute(delete(Application).where(Application.job_id.in_(jobs)))
        db.execute(delete(ResumeVariant).where(ResumeVariant.job_id.in_(jobs)))
        db.execute(delete(Job).where(Job.source == SOURCE))
        # Tasks other tests left queued would be claimed by these tests' workers.
        db.execute(delete(JobInsightTask).where(JobInsightTask.status.in_(("PENDING", "PROCESSING"))))
        db.commit()


@pytest.fixture(autouse=True)
def insights(owner, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "job_insights_enabled", True)
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), RecruiterSource()])
    _clean(owner)
    yield
    _clean(owner)


@pytest.fixture
def signed_in(monkeypatch):
    def sign_in(uid):
        monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(uid, f"{uid}@jobhunt.test", False))
        return TestClient(web_app)
    return sign_in


def new_job(owner, *, company="Northwind Labs", title="Senior Backend Engineer", location="Singapore",
            approve=True, **fields) -> int:
    with SessionLocal() as db:
        db.info["user_id"] = owner
        job = Job(user_id=owner, source=SOURCE, source_ref=uuid.uuid4().hex, title=title, company=company,
                  location=location, description="Build reliable Python services. " * 30,
                  url=f"https://jobs.example.com/{uuid.uuid4().hex}", **fields)
        db.add(job)
        db.flush()
        db.add(ResumeVariant(user_id=owner, job_id=job.id, version=1, reviewer_score=90))
        db.flush()
        if approve:
            workflow.approve_job(db, job)
        db.commit()
        return job.id


def run_insights(now=None) -> list[str]:
    with SessionLocal() as db:
        claims = tasks.claim(db, worker_id="test-worker", limit=50, now=now)
    return [service.process(SessionLocal, claim) for claim in claims]


def insight_of(job_id) -> JobInsight | None:
    with SessionLocal() as db:
        return db.scalar(select(JobInsight).where(JobInsight.job_id == job_id))


def tasks_of(job_id) -> list[JobInsightTask]:
    with SessionLocal() as db:
        return list(db.scalars(select(JobInsightTask).where(JobInsightTask.job_id == job_id)
                               .order_by(JobInsightTask.id)).all())


def refresh(job_id):
    with SessionLocal() as db:
        tasks.enqueue(db, db.get(Job, job_id), reason=tasks.REFRESH, force=True)
        db.commit()


# ───────────── trigger and queue ─────────────

def test_approval_queues_research_without_waiting_for_it(owner):
    job_id = new_job(owner)
    queued = tasks_of(job_id)
    assert len(queued) == 1 and queued[0].status == "PENDING" and queued[0].reason == "AUTO_APPROVED"
    assert queued[0].application_id is not None
    assert insight_of(job_id) is None, "approval only writes a task row; nothing is researched yet"


def test_approval_stands_when_queueing_research_fails(owner, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("queue unavailable")
    monkeypatch.setattr(tasks, "enqueue", broken)
    job_id = new_job(owner)
    with SessionLocal() as db:
        assert db.scalar(select(Application.status).where(Application.job_id == job_id)) == "approved"
    assert tasks_of(job_id) == []


def test_a_job_never_has_two_active_research_tasks(owner):
    job_id = new_job(owner)
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        first = tasks.enqueue(db, job, reason=tasks.USER_REQUEST, force=True)
        second = tasks.enqueue(db, job, reason=tasks.REFRESH, force=True)
        db.commit()
        assert first.id == second.id and first.force_refresh
    assert len(tasks_of(job_id)) == 1


def test_automatic_queueing_skips_a_job_that_already_has_research(owner):
    job_id = new_job(owner)
    assert run_insights() == ["completed"]
    with SessionLocal() as db:
        assert tasks.enqueue(db, db.get(Job, job_id)) is None
    refresh(job_id)  # a person's Refresh always queues
    assert [t.status for t in tasks_of(job_id)] == ["COMPLETED", "PENDING"]


def test_the_sweep_queues_approved_jobs_research_never_reached(owner):
    job_id = new_job(owner, approve=False)
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        db.add(Application(user_id=owner, job_id=job_id, status="approved"))
        job.status = "approved"
        db.commit()
        assert tasks.sweep(db)["queued"] >= 1
    assert [t.reason for t in tasks_of(job_id)] == ["BACKLOG"]


def test_claims_never_hand_one_task_to_two_workers(owner):
    new_job(owner)
    with SessionLocal() as db:
        first = tasks.claim(db, worker_id="a", limit=5)
    with SessionLocal() as db:
        second = tasks.claim(db, worker_id="b", limit=5)
    assert len(first) == 1 and second == []


def test_parallel_claims_on_postgresql_never_overlap(owner):
    import threading

    with SessionLocal() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("parallel claiming is exercised on PostgreSQL (TEST_DATABASE_URL)")
    for _ in range(8):
        new_job(owner)
    claimed, lock = [], threading.Lock()

    def worker(name):
        with SessionLocal() as db:
            got = tasks.claim(db, worker_id=name, limit=4)
        with lock:
            claimed.extend(c.task_id for c in got)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(claimed) == len(set(claimed)) == 8


def test_a_lost_lease_cannot_write_its_result(owner):
    job_id = new_job(owner)
    with SessionLocal() as db:
        claim = tasks.claim(db, worker_id="slow", limit=1)[0]
    with SessionLocal() as db:
        db.get(JobInsightTask, claim.task_id).lock_token = uuid.uuid4().hex
        db.commit()
    assert service.process(SessionLocal, claim) == "lost"
    assert insight_of(job_id) is None or insight_of(job_id).refreshed_at is None


def test_kill_stops_research_and_the_sweep_leaves_the_job_alone(owner):
    job_id = new_job(owner)
    with SessionLocal() as db:
        tasks.claim(db, worker_id="w", limit=1)
    with SessionLocal() as db:
        assert workers.kill(db, owner, "insights")["stopped"] == 1
    with SessionLocal() as db:
        tasks.sweep(db)
    assert [t.status for t in tasks_of(job_id)] == ["SKIPPED"]


# ───────────── sources, statuses and failure ─────────────

def test_one_failed_source_makes_the_run_partial_not_failed(owner, monkeypatch):
    good = FakeSource("glassdoor_compensation", [salary("GLASSDOOR", 170_000, 210_000)])
    broken = FakeSource("payscale", raises=RuntimeError("actor schema changed"), source_type="PAYSCALE")
    off = FakeSource("public_web", not_configured="No search provider", source_type="PUBLIC_WEB")
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), good, broken, off])
    job_id = new_job(owner)
    assert run_insights() == ["partial"]
    insight = insight_of(job_id)
    assert insight.status == "PARTIAL" and insight.compensation["market_range"]["low"] == 170_000
    status = tasks_of(job_id)[-1].source_status
    assert status["glassdoor_compensation"]["status"] == OK and status["payscale"]["status"] == FAILED
    assert status["public_web"]["status"] == NOT_CONFIGURED


def test_every_source_and_the_model_failing_is_retried_then_failed(owner, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_insights_max_attempts", 2)
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), FakeSource("glassdoor_compensation", status=FAILED)])
    monkeypatch.setattr(synthesis, "run", lambda *a, **k: synthesis.Synthesis(errors=["model down"]))
    job_id = new_job(owner)
    assert run_insights() == ["retrying"]
    assert run_insights(now=tasks.utcnow() + timedelta(days=1)) == ["failed"]
    assert tasks_of(job_id)[-1].status == "FAILED" and insight_of(job_id).status == "FAILED"


def test_a_failed_refresh_keeps_the_last_good_insights(owner, monkeypatch):
    values = FakeSource("official_company", [obs("VALUES", "OFFICIAL_COMPANY", {"value": "Ownership"},
                                                 match_scope="COMPANY")], source_type="OFFICIAL_COMPANY")
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), values])
    job_id = new_job(owner)
    assert run_insights() == ["completed"]
    before = insight_of(job_id)
    assert [v["value"] for v in before.company["official_values"]] == ["Ownership"]

    monkeypatch.setattr(get_settings(), "job_insights_max_attempts", 1)
    values.status, values.observations = FAILED, []
    monkeypatch.setattr(synthesis, "run", lambda *a, **k: synthesis.Synthesis(errors=["model down"]))
    refresh(job_id)
    assert run_insights() == ["failed"]
    after = insight_of(job_id)
    assert [v["value"] for v in after.company["official_values"]] == ["Ownership"]
    assert after.refreshed_at == before.refreshed_at and after.last_error
    with SessionLocal() as db:
        kept = db.scalars(select(InsightObservation).where(InsightObservation.job_insight_id == after.id,
                                                           InsightObservation.source_name == "official_company")).all()
    assert len(kept) == 1, "a failed source keeps what it found last time"


def test_a_partial_refresh_without_the_model_keeps_the_previous_synthesis(owner, monkeypatch):
    reviews = FakeSource("glassdoor_reviews", [obs("CULTURE", "GLASSDOOR", {"headline": "High ownership"},
                                                   match_scope="COMPANY")], company_level=True)
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), reviews])
    job_id = new_job(owner)
    run_insights()
    themes = insight_of(job_id).company["culture_themes"]
    assert themes and themes[0]["label"] == "Employee-reported"
    monkeypatch.setattr(synthesis, "run", lambda *a, **k: synthesis.Synthesis(errors=["model down"]))
    refresh(job_id)
    assert run_insights() == ["partial"]
    assert insight_of(job_id).company["culture_themes"] == themes


# ───────────── compensation ─────────────

def _rows(*observations):
    return [{"id": i + 1, "source_type": o.source_type, "source_name": o.source_type.lower(),
             "match_scope": o.match_scope, "recency": o.recency, "confidence": o.confidence,
             "observed_at": None, "data": o.data} for i, o in enumerate(observations)]


def test_the_employer_salary_is_never_replaced_by_an_estimate():
    out = compensation.compensation_insight(_rows(salary("EMPLOYER", 180_000, 220_000, confidence=0.98),
                                                  salary("GLASSDOOR", 175_000, 215_000)))
    assert out["status"] == "EMPLOYER_POSTED"
    assert out["recommended_discussion_range"]["low"] == 180_000
    assert out["recommended_discussion_range"]["label"] == "The employer's posted range"
    assert out["market_range"]["label"].startswith("Market estimate")


def test_glassdoor_and_payscale_that_agree_form_a_market_consensus():
    out = compensation.compensation_insight(_rows(salary("GLASSDOOR", 185_000, 225_000, sample_size=12),
                                                  salary("PAYSCALE", 175_000, 215_000, match_scope="MARKET_ROLE_CITY",
                                                         confidence=0.6)))
    assert out["status"] == "MULTI_SOURCE_CONSENSUS"
    market = out["market_range"]
    assert 175_000 <= market["low"] <= 185_000 and 215_000 <= market["high"] <= 225_000
    assert out["recommended_discussion_range"]["label"].startswith("Market estimate")
    assert out["employer_posted_range"] is None and out["recruiter_budget"] is None


def test_company_specific_data_outranks_market_data():
    rows = _rows(salary("PAYSCALE", 100_000, 120_000, match_scope="MARKET_GLOBAL"),
                 salary("GLASSDOOR", 110_000, 130_000, match_scope="COMPANY_ROLE_CITY"))
    out = compensation.compensation_insight(rows)
    assert [r["source_type"] for r in out["observations"]] == ["GLASSDOOR", "PAYSCALE"]
    assert compensation.precedence(out["observations"][0]) < compensation.precedence(out["observations"][1])


def test_conflicting_estimates_are_not_averaged():
    out = compensation.compensation_insight(_rows(salary("GLASSDOOR", 210_000, 260_000),
                                                  salary("PAYSCALE", 120_000, 140_000)))
    assert out["status"] == "CONFLICTING_ESTIMATES" and out["market_range"] is None
    assert len(out["conflict"]["alternatives"]) == 2 and out["conflict"]["spread"] > 0.25


def test_an_employer_figure_far_from_the_market_is_flagged_but_kept():
    out = compensation.compensation_insight(_rows(salary("EMPLOYER", 150_000, 150_000, confidence=0.98),
                                                  salary("GLASSDOOR", 210_000, 260_000)))
    assert out["status"] == "CONFLICTING_ESTIMATES" and out["conflict"]["between"] == "employer and market"
    assert out["recommended_discussion_range"]["low"] == 150_000


def test_original_currency_and_period_are_kept_and_never_converted():
    out = compensation.compensation_insight(_rows(salary("GLASSDOOR", 90, 110, period="hour"),
                                                  salary("PAYSCALE", 140_000, 160_000, currency="USD")))
    hourly, other = out["observations"]
    assert hourly["original"] == {**hourly["original"], "low": 90, "high": 110, "period": "hour", "currency": "SGD"}
    assert hourly["annual_low"] == 90 * 2080 and hourly["included"]
    assert not other["included"] and "not converted" in other["excluded_reason"]
    assert out["currency"] == "SGD"


def test_base_and_total_compensation_are_never_compared():
    out = compensation.compensation_insight(_rows(salary("GLASSDOOR", 180_000, 200_000, basis="BASE"),
                                                  salary("GLASSDOOR", 260_000, 320_000, basis="TOTAL_COMPENSATION")))
    assert out["status"] == "SINGLE_SOURCE_ESTIMATE" and out["basis"] == "BASE"
    assert out["base_salary"]["low"] == 180_000 and out["total_compensation"] is None
    assert [r["included"] for r in out["observations"]] == [True, False]


def test_unknown_salary_stays_unknown():
    out = compensation.compensation_insight([])
    assert out["status"] == "UNKNOWN" and out["recommended_discussion_range"] is None and out["confidence"] == "NONE"


def test_a_recruiter_budget_outranks_market_data():
    out = compensation.compensation_insight(_rows(salary("GLASSDOOR", 170_000, 200_000),
                                                  salary("RECRUITER", 190_000, 210_000, confidence=0.95)))
    assert out["status"] == "RECRUITER_PROVIDED"
    assert out["recommended_discussion_range"]["label"] == "The recruiter's stated budget"


def test_the_posting_salary_is_the_employer_source(owner):
    job_id = new_job(owner, raw={"salary_min": 180000, "salary_max": 220000, "salary_currency": "SGD",
                                 "salary_interval": "yearly"})
    run_insights()
    comp = insight_of(job_id).compensation
    assert comp["employer_posted_range"]["low"] == 180_000 and comp["status"] == "EMPLOYER_POSTED"


def test_a_board_estimate_is_market_data_not_the_employer(owner):
    job_id = new_job(owner, salary_min=150000, salary_max=180000, salary_currency="SGD", salary_interval="year",
                     salary_source="glassdoor_estimate")
    run_insights()
    comp = insight_of(job_id).compensation
    assert comp["employer_posted_range"] is None and comp["market_range"]["low"] == 150_000


# ───────────── matching ─────────────

def target(**kw):
    return matching.target_for(kw.get("company", "Grab"), kw.get("title", "Lead Software Engineer"),
                               kw.get("location", "Singapore"), kw.get("website"))


def test_an_ambiguous_company_name_needs_confirmation():
    now = tasks.utcnow()
    unconfirmed = matching.assess(target(), base_confidence=0.8, company="Grab", title="Lead Software Engineer",
                                  now=now)
    assert unconfirmed.status == matching.NEEDS_REVIEW
    confirmed = matching.assess(target(), base_confidence=0.8, company="Grab", title="Lead Software Engineer",
                                location="Singapore", now=now)
    assert confirmed.status == matching.ACCEPTED and confirmed.scope == "COMPANY_ROLE_CITY"


def test_a_different_company_or_domain_is_rejected():
    now = tasks.utcnow()
    assert matching.assess(target(company="Northwind Labs"), base_confidence=0.8, company="Northwind Traders",
                           title="Lead Software Engineer", now=now).status == matching.REJECTED
    assert matching.assess(target(website="https://grab.com"), base_confidence=0.8, company="Grab",
                           domain="grab.co.uk", now=now, company_level=True).status == matching.REJECTED
    assert matching.assess(target(website="https://www.grab.com"), base_confidence=0.8, company="Grab Holdings Inc.",
                           domain="careers.grab.com", now=now, company_level=True).status == matching.ACCEPTED


def test_location_and_role_shape_the_match():
    now = tasks.utcnow()
    t = target(company="Northwind Labs", title="Senior Backend Engineer")
    city = matching.assess(t, base_confidence=0.8, company="Northwind Labs", title="Sr. Backend Engineer",
                           location="Singapore", now=now)
    market = matching.assess(t, base_confidence=0.8, title="Senior Backend Engineer", location="Singapore", now=now)
    similar = matching.assess(t, base_confidence=0.8, company="Northwind Labs", title="Senior Backend Developer",
                              location="Singapore", now=now)
    other_country = matching.assess(t, base_confidence=0.8, company="Northwind Labs", title="Senior Backend Engineer",
                                    location="Austin, TX", now=now, compensation=True)
    unrelated = matching.assess(t, base_confidence=0.8, company="Northwind Labs", title="Account Executive", now=now)
    assert city.scope == "COMPANY_ROLE_CITY" and market.scope == "MARKET_ROLE_CITY"
    assert similar.scope == "COMPANY_SIMILAR_ROLE" and similar.confidence < city.confidence
    assert other_country.status == matching.REJECTED and unrelated.status == matching.REJECTED
    assert city.confidence > market.confidence


def test_recent_observations_weigh_more():
    now = tasks.utcnow()
    t = target(company="Northwind Labs")
    fresh = matching.assess(t, base_confidence=0.8, company="Northwind Labs", title="Lead Software Engineer",
                            observed_at=now - timedelta(days=60), now=now)
    old = matching.assess(t, base_confidence=0.8, company="Northwind Labs", title="Lead Software Engineer",
                          observed_at=now - timedelta(days=900), now=now)
    assert (fresh.recency, old.recency) == ("HIGH", "LOW") and fresh.confidence > old.confidence
    assert old.status == matching.ACCEPTED, "old data is kept unless an age limit is configured"
    limited = matching.assess(t, base_confidence=0.8, company="Northwind Labs", title="Lead Software Engineer",
                              observed_at=now - timedelta(days=900), now=now, max_age_months=24)
    assert limited.status == matching.REJECTED


def test_title_normalization_records_seniority_and_discipline():
    role = matching.normalize_title("Sr. Backend Engineer (Payments)")
    assert (role.seniority, role.discipline, role.normalized) == ("SENIOR", "BACKEND", "senior backend engineer")


# ───────────── Apify adapters (no live calls) ─────────────

def _apify_transport(items, *, status="SUCCEEDED", seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path.endswith("/runs"):
            return httpx.Response(201, json={"data": {"id": "run123", "status": status, "defaultDatasetId": "ds9"}})
        if "/datasets/" in request.url.path:
            return httpx.Response(200, json=items)
        return httpx.Response(200, json={"data": {"id": "run123", "status": status, "defaultDatasetId": "ds9"}})
    return httpx.MockTransport(handler)


@pytest.fixture
def apify_fake(monkeypatch):
    from app.services import sources as sources_module
    from app.services.external_api import external_client

    holder = {"items": [], "status": "SUCCEEDED", "seen": []}

    def client(token, *, operation, user_id=None, metadata=None, timeout=30, json_body=False, service="apify_api",
               job_id=None, workflow_type=None):
        # The transport is given at construction: a proxy set in the environment can then never route around it.
        return external_client("APIFY", service=service, operation=operation, timeout=timeout, user_id=user_id,
                               metadata=metadata or {}, job_id=job_id, workflow_type=workflow_type,
                               headers={"Authorization": f"Bearer {token}"},
                               transport=_apify_transport(holder["items"], status=holder["status"],
                                                          seen=holder["seen"]))
    monkeypatch.setattr(sources_module, "apify_client", client)
    monkeypatch.setattr(get_settings(), "apify_glassdoor_compensation_actor", "someone/salary-actor")
    monkeypatch.setattr(get_settings(), "apify_glassdoor_compensation_input",
                        '{"companyName": "{company}", "jobTitle": "{role}", "maxItems": "{limit}"}')
    monkeypatch.setattr("app.services.user_settings.apify_token", lambda db, user_id=None: "apify_api_" + "x" * 30)
    return holder


def test_an_unconfigured_actor_is_reported_and_never_invented(owner, monkeypatch):
    monkeypatch.setattr(get_settings(), "apify_payscale_actor", "")
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), apify.PayScaleSource()])
    job_id = new_job(owner)
    run_insights()
    state = tasks_of(job_id)[-1].source_status["payscale"]
    assert state["status"] == NOT_CONFIGURED and "APIFY_PAYSCALE_ACTOR" in state["detail"]


def test_glassdoor_salaries_are_matched_and_wrong_companies_dropped(owner, monkeypatch, apify_fake):
    apify_fake["items"] = [
        {"employerName": "Northwind Labs", "jobTitle": "Senior Backend Engineer", "location": "Singapore",
         "basePayLow": 170000, "basePayHigh": 210000, "currency": "SGD", "payPeriod": "ANNUAL", "salaryCount": 14},
        {"employerName": "Northwind Traders", "jobTitle": "Senior Backend Engineer", "location": "Singapore",
         "basePayLow": 90000, "basePayHigh": 100000, "currency": "SGD", "payPeriod": "ANNUAL"},
        {"employerName": "Northwind Labs", "jobTitle": "Senior Backend Engineer", "location": "Seattle, WA",
         "basePayLow": 200000, "basePayHigh": 250000, "currency": "USD", "payPeriod": "ANNUAL"}]
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), apify.GlassdoorCompensationSource()])
    job_id = new_job(owner)
    run_insights()
    state = tasks_of(job_id)[-1].source_status["glassdoor_compensation"]
    assert state["status"] == OK and state["observations"] == 1 and state["rejected"] == 2
    with SessionLocal() as db:
        row = db.scalar(select(InsightObservation).where(InsightObservation.source_name == "glassdoor_compensation",
                                                         InsightObservation.job_insight_id == insight_of(job_id).id))
    assert row.actor_id == "someone/salary-actor" and row.source_reference == "run:run123 dataset:ds9"
    assert row.match_scope == "COMPANY_ROLE_CITY" and row.data["sample_size"] == 14
    assert insight_of(job_id).compensation["market_range"]["sample_size"] == 14


def test_apify_calls_are_recorded_without_the_token(owner, monkeypatch, apify_fake):
    apify_fake["items"] = [{"employerName": "Northwind Labs", "jobTitle": "Senior Backend Engineer",
                            "basePayLow": 1, "basePayHigh": 2, "currency": "SGD", "payPeriod": "ANNUAL"}]
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), apify.GlassdoorCompensationSource()])
    job_id = new_job(owner)
    run_insights()
    with SessionLocal() as db:
        rows = db.scalars(select(ExternalAPIRequestLog).where(ExternalAPIRequestLog.job_id == job_id,
                                                              ExternalAPIRequestLog.provider == "APIFY")).all()
    assert {(r.provider, r.service) for r in rows} == {("APIFY", "job_insights")}
    assert {r.operation for r in rows} >= {"glassdoor_compensation_run", "glassdoor_compensation_dataset"}
    assert "x" * 30 not in json.dumps([r.request_metadata for r in rows], default=str)
    assert all(r.workflow_type == "job" for r in rows)


def test_an_actor_whose_schema_changed_fails_only_that_source(owner, monkeypatch, apify_fake):
    apify_fake["items"] = [{"unexpected": "shape"}]
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), apify.GlassdoorCompensationSource()])
    job_id = new_job(owner)
    assert run_insights() == ["partial"]
    state = tasks_of(job_id)[-1].source_status["glassdoor_compensation"]
    assert state["status"] == FAILED and "Schema not recognized" in state["detail"]


def test_an_empty_dataset_and_a_failed_run_are_reported(owner, monkeypatch, apify_fake):
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), apify.GlassdoorCompensationSource()])
    job_id = new_job(owner)
    run_insights()
    assert tasks_of(job_id)[-1].source_status["glassdoor_compensation"]["status"] == NO_DATA
    apify_fake["status"] = "FAILED"
    refresh(job_id)
    run_insights()
    assert tasks_of(job_id)[-1].source_status["glassdoor_compensation"]["status"] == FAILED


def test_the_actor_input_template_is_filled_from_the_job(monkeypatch):
    rendered = apify.render_input('{"company": "{company}", "max": "{limit}", "q": ["{role} in {location}"]}',
                                  {"company": "Grab", "role": "Lead Engineer", "location": "Singapore", "limit": 25})
    assert rendered == {"company": "Grab", "max": 25, "q": ["Lead Engineer in Singapore"]}


# ───────────── the official site, values and culture ─────────────

def test_official_values_are_read_from_the_page_itself():
    html = """<html><head><title>About us</title><meta name="description" content="We build payments."></head>
      <body><h2>Our values</h2><ul><li>Customer obsession</li><li>Move with urgency</li></ul>
      <p>Our mission is to make payments simple for every business.</p></body></html>"""
    page = extract_company_page(html, "https://example.com/about")
    assert [v["value"] for v in page["values"]] == ["Customer obsession", "Move with urgency"]
    assert page["missions"] and page["description"] == "We build payments."


def test_employee_culture_is_kept_apart_from_official_values(owner, monkeypatch):
    official = FakeSource("official_company", [obs("VALUES", "OFFICIAL_COMPANY", {"value": "Heart"},
                                                   match_scope="COMPANY")], source_type="OFFICIAL_COMPANY")
    reviews = FakeSource("glassdoor_reviews", [obs("CULTURE", "GLASSDOOR", {"headline": "Fast paced"},
                                                   match_scope="COMPANY"),
                                               obs("SENTIMENT", "GLASSDOOR", {"overall_rating": 4.1, "culture": 3.9,
                                                                              "sample_size": 812, "aggregate": True},
                                                   match_scope="COMPANY")], company_level=True)
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), official, reviews])
    job_id = new_job(owner)
    run_insights()
    company = insight_of(job_id).company
    assert [v["value"] for v in company["official_values"]] == ["Heart"]
    assert all(v["kind"] == "FACT" and v["source_ids"] for v in company["official_values"])
    assert [t["theme"] for t in company["culture_themes"]] == ["Fast paced"]
    assert company["culture_themes"][0]["label"] == "Employee-reported"
    assert company["sentiment"]["overall_rating"] == 4.1 and company["sentiment"]["sample_size"] == 812
    assert "not objective" in company["sentiment"]["label"]


def test_a_review_theme_offered_as_an_official_value_is_dropped():
    research = [{"id": 1, "category": "CULTURE", "source_type": "GLASSDOOR"},
                {"id": 2, "category": "VALUES", "source_type": "OFFICIAL_COMPANY"}]
    answer = CompanyInsightSynthesis.model_validate({
        "official_values": [{"value": "High ownership", "supporting_source_ids": [1]},
                            {"value": "Heart", "supporting_source_ids": [2]}],
        "culture_themes": [{"theme": "Heart", "sentiment": "POSITIVE", "supporting_source_ids": [2]}],
        "research_confidence": 0.5})
    dropped: list[str] = []
    out = synthesis.validate_company(answer, research, dropped)
    assert [v.value for v in out.official_values] == ["Heart"] and out.culture_themes == []
    assert len(dropped) == 2


def test_unknown_source_ids_reject_the_answer():
    answer = CompanyInsightSynthesis.model_validate({
        "official_values": [{"value": "Invented", "supporting_source_ids": [999]}], "research_confidence": 0.5})
    with pytest.raises(synthesis.SynthesisRejected):
        synthesis.validate_company(answer, [{"id": 1, "category": "VALUES", "source_type": "OFFICIAL_COMPANY"}], [])


def test_the_contracts_are_strict():
    with pytest.raises(ValidationError):
        CompanyInsightSynthesis.model_validate({"research_confidence": 0.5, "salary": "SGD 200K"})
    with pytest.raises(ValidationError):
        InterviewPrepResult.model_validate({"generated_questions": [{"question": "Tell me about X?",
                                                                     "rationale": "r", "source_ids": [1]}]})


def test_a_stage_nothing_reports_is_never_shown_as_reported():
    answer = CompanyInsightSynthesis.model_validate({
        "interview_stages": [{"name": "System design", "order": 2, "origin": "REPORTED",
                              "rationale": "Staff role"}], "research_confidence": 0.5})
    out = synthesis.validate_company(answer, [], [])
    assert out.interview_stages[0].origin == "INFERRED"


# ───────────── interview intelligence ─────────────

def test_reported_questions_carry_their_sources_and_generated_ones_never_do(owner, monkeypatch):
    questions = FakeSource("glassdoor_interview", [
        obs("INTERVIEW_QUESTION", "GLASSDOOR", {"question": "Design a high-volume ride matching service"},
            title="Northwind Labs · Software Engineer · Singapore"),
        obs("INTERVIEW_PROCESS", "GLASSDOOR", {"text": "Four rounds", "duration": "3 weeks", "difficulty": "Hard"})],
        ttl="interview")
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), questions])

    def crafted(self, prompt, call=None):
        from app.services.llm import LLMResult

        if prompt.name == "interview_insights":
            data = {"generated_questions": [
                {"question": "How would you design an idempotent payment workflow?", "origin": "ROLE_DERIVED",
                 "category": "SYSTEM_DESIGN", "rationale": "The posting asks for reliable services."},
                {"question": "You have not worked in banking. How would you ramp up?",
                 "origin": "CANDIDATE_SPECIFIC", "category": "DOMAIN", "rationale": "A gap the fit analysis notes.",
                 "preparation": "Connect adjacent payments work; do not claim banking experience."}],
                "questions_to_ask": [{"stage": "HIRING_MANAGER", "question": "How is platform reliability split "
                                      "between product and platform teams today?", "context": "Reliability"}],
                "compensation_questions": []}
        else:
            return real(self, prompt, call)
        return LLMResult(data, "mock", "mock", validated=call.validate(data))
    from app.services.llm import LLMClient

    real = LLMClient.generate_prompt
    monkeypatch.setattr(LLMClient, "generate_prompt", crafted)
    job_id = new_job(owner)
    run_insights()
    insight = insight_of(job_id)
    reported = insight.reported_questions
    assert reported[0]["origin"] == "REPORTED" and reported[0]["kind"] == "FACT" and reported[0]["source_ids"]
    assert reported[0]["source_type"] == "GLASSDOOR"
    generated = insight.generated_questions
    # The profile question cited neither evidence nor a gap, so it is dropped rather than shown as grounded.
    assert {q["origin"] for q in generated} == {"ROLE_DERIVED"}
    assert all(q["kind"] == "GENERATED" and "source_ids" not in q and q["why_likely"] for q in generated)
    assert generated[0]["id"].startswith("q") and len(generated[0]["id"]) == 13, "a stable content id"
    assert insight.questions_to_ask[0]["kind"] == "GENERATED"
    assert insight.interview_process["durations"][0]["text"] == "3 weeks"


def test_generated_questions_cannot_cite_requirements_or_evidence_they_were_not_shown():
    answer = InterviewPrepResult.model_validate({"generated_questions": [
        {"question": "Tell me about Kafka at scale?", "rationale": "Required", "requirement_ids": ["req_404"]}]})
    with pytest.raises(synthesis.SynthesisRejected):
        synthesis.validate_interview(answer, requirement_ids={"req_001"}, evidence_ids=set(), value_ids=set())
    answer = InterviewPrepResult.model_validate({"generated_questions": [
        {"question": "Tell me about cost cutting?", "rationale": "Strong match", "evidence_ids": [12]}]})
    with pytest.raises(synthesis.SynthesisRejected):
        synthesis.validate_interview(answer, requirement_ids=set(), evidence_ids={7}, value_ids=set())


def test_model_calls_are_recorded_with_their_worker_and_prompt_version(owner, monkeypatch):
    official = FakeSource("official_company", [obs("VALUES", "OFFICIAL_COMPANY", {"value": "Heart"},
                                                   match_scope="COMPANY")], source_type="OFFICIAL_COMPANY")
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), official])
    job_id = new_job(owner)
    run_insights()
    with SessionLocal() as db:
        rows = db.scalars(select(LLMRequestLog).where(LLMRequestLog.workflow_entity_id == job_id,
                                                      LLMRequestLog.prompt_name.in_(("company_insights",
                                                                                     "interview_insights")))).all()
    assert {r.prompt_name for r in rows} == {"company_insights", "interview_insights"}
    assert {(r.prompt_name, r.stage) for r in rows} == {("company_insights", "COMPANY_INSIGHTS"),
                                                        ("interview_insights", "INTERVIEW_INSIGHTS")}
    assert {(r.prompt_name, r.prompt_version) for r in rows} == {("company_insights", "v1"),
                                                                ("interview_insights", "v2")}
    assert insight_of(job_id).prompt_versions == {"company_insights": "v1", "interview_insights": "v2",
                                                  "application_form": "v4"}


# ───────────── cache ─────────────

def test_research_is_reused_for_another_job_at_the_same_company(owner, monkeypatch):
    shared = FakeSource("glassdoor_compensation", [salary("GLASSDOOR", 170_000, 210_000)], cacheable=True)
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), shared])
    first = new_job(owner)
    run_insights()
    second = new_job(owner)
    run_insights()
    assert shared.calls == 1
    assert tasks_of(second)[-1].source_status["glassdoor_compensation"]["cached"] is True
    assert insight_of(second).compensation["market_range"]["low"] == 170_000
    assert first != second


def test_stale_cache_and_a_refresh_both_research_again(owner, monkeypatch):
    shared = FakeSource("glassdoor_compensation", [salary("GLASSDOOR", 170_000, 210_000)], cacheable=True)
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), shared])
    first = new_job(owner)
    run_insights()
    refresh(first)
    run_insights()
    assert shared.calls == 2, "a person's Refresh bypasses the cache"
    with SessionLocal() as db:
        for row in db.scalars(select(InsightObservation).where(InsightObservation.source_name ==
                                                               "glassdoor_compensation")).all():
            row.fetched_at = tasks.utcnow() - timedelta(days=60)
        db.commit()
    new_job(owner)
    run_insights()
    assert shared.calls == 3


def test_the_cache_never_crosses_accounts(owner, monkeypatch):
    shared = FakeSource("glassdoor_compensation", [salary("GLASSDOOR", 170_000, 210_000)], cacheable=True)
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), shared])
    new_job(owner)
    run_insights()
    other = make_user(SessionLocal)
    new_job(other)
    run_insights()
    assert shared.calls == 2


# ───────────── API, tenancy, tracker ─────────────

def test_find_insights_and_refresh_through_the_api(owner, signed_in):
    job_id = new_job(owner, approve=False)
    client = signed_in(owner)
    first = client.post(f"/api/jobs/{job_id}/insights/refresh").json()
    again = client.post(f"/api/jobs/{job_id}/insights/refresh").json()
    assert first["task"]["id"] == again["task"]["id"] and first["task"]["reason"] == "USER_REQUEST"
    run_insights()
    view = client.get(f"/api/jobs/{job_id}/insights").json()
    assert view["status"] == "COMPLETED" and view["last_successful_at"] and not view["researching"]
    assert {s["name"] for s in view["sources"]} == {"job_posting", "recruiter"}
    assert client.post(f"/api/jobs/{job_id}/insights/refresh").json()["task"]["reason"] == "REFRESH"
    for part in ("compensation", "company", "interview", "sources"):
        assert client.get(f"/api/jobs/{job_id}/insights/{part}").status_code == 200


def test_another_account_cannot_see_or_refresh_my_insights(owner, signed_in):
    job_id = new_job(owner)
    run_insights()
    stranger = signed_in(make_user(SessionLocal))
    assert stranger.get(f"/api/jobs/{job_id}/insights").status_code == 404
    assert stranger.post(f"/api/jobs/{job_id}/insights/refresh").status_code == 404
    assert stranger.get(f"/api/jobs/{job_id}/insights/sources").status_code == 404
    with SessionLocal() as db:
        app_id = db.scalar(select(Application.id).where(Application.job_id == job_id))
    assert stranger.get(f"/api/applications/{app_id}/insights").status_code == 404


def test_recruiter_notes_outrank_estimates_and_survive_refresh(owner, signed_in, monkeypatch):
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), RecruiterSource(),
                                             FakeSource("glassdoor_compensation",
                                                        [salary("GLASSDOOR", 150_000, 170_000)])])
    job_id = new_job(owner)
    client = signed_in(owner)
    saved = client.put(f"/api/jobs/{job_id}/insights/inputs", json={"recruiter": {
        "recruiter_name": "Mei", "budget_low": 190000, "budget_high": 210000, "currency": "sgd", "period": "year",
        "interview_process": "Recruiter screen\nCoding\nSystem design", "recruiter_questions": ["Why us?"]}})
    assert saved.status_code == 200
    run_insights()
    view = client.get(f"/api/jobs/{job_id}/insights").json()
    assert view["compensation"]["recruiter_budget"]["low"] == 190000
    assert [s["name"] for s in view["interview"]["process"]["stages"]] == ["Recruiter screen", "Coding",
                                                                           "System design"]
    assert view["interview"]["process"]["stages"][0]["source_type"] == "RECRUITER"
    client.post(f"/api/jobs/{job_id}/insights/refresh")
    run_insights()
    assert insight_of(job_id).recruiter_input["recruiter_name"] == "Mei"


def test_a_private_or_invalid_company_website_is_refused(owner, signed_in):
    job_id = new_job(owner)
    out = signed_in(owner).put(f"/api/jobs/{job_id}/insights/inputs", json={"company_website": "http://127.0.0.1"})
    assert out.status_code == 422


def test_the_application_view_follows_the_tracker_stage(owner, signed_in, monkeypatch):
    job_id = new_job(owner)
    run_insights()
    with SessionLocal() as db:
        application = db.scalar(select(Application).where(Application.job_id == job_id))
        application.status, application.stage = "applied", "screening"
        db.commit()
        app_id = application.id
    client = signed_in(owner)
    screening = client.get(f"/api/applications/{app_id}/insights").json()
    assert [f["id"] for f in screening["focus"]] == ["RECRUITER_SCREEN"]
    assert screening["compensation_prep"] is not None
    assert all(q["stage"] == "RECRUITER" for q in screening["questions_to_ask"])
    with SessionLocal() as db:
        db.get(Application, app_id).stage = "offer"
        db.commit()
    offer = client.get(f"/api/applications/{app_id}/insights").json()
    assert [f["id"] for f in offer["focus"]] == ["HR_OFFER"] and offer["insights"]["job"]["id"] == job_id


def test_prepare_answer_outlines_from_evidence_and_saves_nothing(owner, signed_in):
    job_id = new_job(owner)
    run_insights()
    with SessionLocal() as db:
        insight = db.scalar(select(JobInsight).where(JobInsight.job_id == job_id))
        insight.generated_questions = [{"id": "q1", "question": "How do you keep Python services reliable?",
                                        "origin": "ROLE_DERIVED", "evidence_ids": [], "gaps": ["Banking"]}]
        db.commit()
    out = signed_in(owner).post(f"/api/jobs/{job_id}/insights/questions/q1/prepare").json()
    assert out["kind"] == "GENERATED" and out["gaps_not_to_claim"] == ["Banking"] and len(out["outline"]) == 4
    assert signed_in(owner).post(f"/api/jobs/{job_id}/insights/questions/q9/prepare").status_code == 404


def test_compensation_prep_never_invents_an_employer_budget(owner):
    job_id = new_job(owner)
    run_insights()
    prep = insight_of(job_id).compensation_prep
    assert prep["posted"] is None and prep["recruiter_budget"] is None and prep["discussion_range"] is None
    assert prep["questions"] and all(q["kind"] == "GENERATED" for q in prep["questions"])


# ───────────── review fixes ─────────────

def test_a_refresh_without_the_model_keeps_the_observations_its_kept_sections_cite(owner, monkeypatch):
    values = FakeSource("official_company", [obs("VALUES", "OFFICIAL_COMPANY", {"value": "Ownership"},
                                                 match_scope="COMPANY")], source_type="OFFICIAL_COMPANY")
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), values])
    job_id = new_job(owner)
    run_insights()
    cited = insight_of(job_id).company["official_values"][0]["source_ids"]
    monkeypatch.setattr(synthesis, "run", lambda *a, **k: synthesis.Synthesis(errors=["model down"]))
    refresh(job_id)
    run_insights()
    insight = insight_of(job_id)
    assert insight.company["official_values"][0]["source_ids"] == cited
    with SessionLocal() as db:
        kept = db.get(InsightObservation, cited[0])
        current = db.scalars(select(InsightObservation).where(InsightObservation.job_insight_id == insight.id,
                                                              InsightObservation.superseded_at.is_(None))).all()
    assert kept is not None and kept.superseded_at is not None, "still cited, so kept (marked superseded)"
    assert [o.data["value"] for o in current] == ["Ownership"] and current[0].id != cited[0]
    monkeypatch.undo()
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), values])
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    refresh(job_id)
    run_insights()  # the model is back: the section is rebuilt and the old row is no longer cited
    with SessionLocal() as db:
        assert db.get(InsightObservation, cited[0]) is None


def test_posting_facts_are_saved_even_when_every_outside_source_and_the_model_fail(owner, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_insights_max_attempts", 1)
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), FakeSource("glassdoor_compensation", status=FAILED)])
    monkeypatch.setattr(synthesis, "run", lambda *a, **k: synthesis.Synthesis(errors=["model down"]))
    job_id = new_job(owner, raw={"salary_min": 150000, "salary_max": 170000, "salary_currency": "SGD",
                                 "salary_interval": "year"})
    assert run_insights() == ["failed"]
    insight = insight_of(job_id)
    assert insight.compensation["employer_posted_range"]["low"] == 150_000 and insight.status == "PARTIAL"


def test_an_actor_without_an_input_template_is_never_run(monkeypatch):
    monkeypatch.setattr(get_settings(), "apify_payscale_actor", "someone/payscale")
    monkeypatch.setattr(get_settings(), "apify_payscale_input", "")
    assert "APIFY_PAYSCALE_INPUT" in apify.PayScaleSource().configured()
    assert not hasattr(apify, "default_input")


def test_a_reported_stage_must_cite_an_interview_report():
    research = [{"id": 1, "category": "VALUES", "source_type": "OFFICIAL_COMPANY"},
                {"id": 2, "category": "INTERVIEW_PROCESS", "source_type": "GLASSDOOR"}]
    answer = CompanyInsightSynthesis.model_validate({"interview_stages": [
        {"name": "Values round", "order": 1, "origin": "REPORTED", "supporting_source_ids": [1]},
        {"name": "System design", "order": 2, "origin": "REPORTED", "supporting_source_ids": [2]}],
        "research_confidence": 0.5})
    out = synthesis.validate_company(answer, research, [])
    assert [(s.name, s.origin) for s in out.interview_stages] == [("System design", "REPORTED")]


def test_ungrounded_generated_questions_are_dropped():
    answer = InterviewPrepResult.model_validate({"generated_questions": [
        {"question": "Tell me about Kafka at scale?", "rationale": "Required"},
        {"question": "Tell me about your pipeline work?", "origin": "CANDIDATE_SPECIFIC", "rationale": "Strong"},
        {"question": "Tell me about Python services?", "rationale": "Required", "requirement_ids": ["req_001"]}]})
    dropped: list[str] = []
    out = synthesis.ground_questions(answer, has_requirements=True, dropped=dropped)
    assert [q.question for q in out.generated_questions] == ["Tell me about Python services?"] and len(dropped) == 2


def test_citations_are_checked_against_only_what_the_prompt_showed():
    from app.prompts.company_insights import MAX_OBSERVATIONS, shown_observations

    many = [{"id": i, "category": "VALUES", "source_type": "OFFICIAL_COMPANY", "data": {"value": f"V{i}"}}
            for i in range(MAX_OBSERVATIONS + 30)]
    shown = shown_observations(many)
    assert len(shown) <= MAX_OBSERVATIONS
    unseen = CompanyInsightSynthesis.model_validate({"official_values": [
        {"value": "Guess", "supporting_source_ids": [MAX_OBSERVATIONS + 5]}], "research_confidence": 0.5})
    with pytest.raises(synthesis.SynthesisRejected):
        synthesis.validate_company(unseen, shown, [])


def test_pay_questions_with_figures_are_discarded():
    prep = InterviewPrepResult.model_validate({"compensation_questions": [
        "Would SGD 200,000 be approved?", "Is 180k within the band?", "What is the approved band for this level?"]})
    out = service.compensation_prep({}, prep, None)
    assert [q["question"] for q in out["questions"]] == ["What is the approved band for this level?"]


def test_an_unknown_employer_basis_is_not_compared_with_market_base_pay():
    out = compensation.compensation_insight(_rows(salary("EMPLOYER", 300_000, 320_000, basis="UNKNOWN",
                                                         confidence=0.98),
                                                  salary("GLASSDOOR", 180_000, 200_000, basis="BASE")))
    assert out["status"] == "EMPLOYER_POSTED" and out["conflict"] is None
    assert out["base_salary"]["low"] == 180_000, "only the market's base salary is called base salary"


def test_only_http_links_are_stored():
    assert service.safe_url("javascript:alert(1)") is None
    assert service.safe_url("data:text/html,x") is None
    assert service.safe_url("https://grab.com/values") == "https://grab.com/values"


def test_provenance_is_paged(owner, signed_in, monkeypatch):
    many = FakeSource("glassdoor_interview", [obs("INTERVIEW_QUESTION", "GLASSDOOR", {"question": f"Question {i}?"})
                                              for i in range(5)], ttl="interview")
    monkeypatch.setattr(service, "SOURCES", [PostingSource(), many])
    job_id = new_job(owner)
    run_insights()
    client = signed_in(owner)
    first = client.get(f"/api/jobs/{job_id}/insights/sources?limit=2").json()
    assert first["total"] == 5 and first["truncated"] and first["next_offset"] == 2 and len(first["observations"]) == 2
    last = client.get(f"/api/jobs/{job_id}/insights/sources?offset=4&limit=2").json()
    assert not last["truncated"] and last["next_offset"] is None and len(last["observations"]) == 1


def test_the_research_node_counts_the_calls_research_actually_makes():
    from types import SimpleNamespace

    from app.services import orchestration
    from app.services import orchestration_topology as topo

    node = topo.NODES_BY_ID["company_insights"]
    for provider, operation in (("APIFY", "glassdoor_compensation_run"), ("APIFY", "payscale_compensation_dataset"),
                                ("WEB", "official_company_page")):
        assert orchestration._matches(node, SimpleNamespace(provider=provider, operation=operation)), operation
    assert not orchestration._matches(node, SimpleNamespace(provider="WEB", operation="jd_recovery_http"))
    prep = topo.NODES_BY_ID["interview_insights"]
    assert node.llm_stages == ("COMPANY_INSIGHTS",) and prep.llm_stages == ("INTERVIEW_INSIGHTS",)
