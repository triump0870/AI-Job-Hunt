"""Discovery quality: jobs outside the candidate's domain are hidden at discovery (deterministic, no model), the
filter learns from what the person dismisses, dead postings are deleted and never imported again, and each job can
be ranked and tailored with its own resume.

Everything runs as a dedicated account, so the shared test account's profile is never changed."""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

import app.main  # noqa: F401  migrates the test database, as every API test does
from app import auth
from app.db import SessionLocal
from app.main import app as web_app
from app.models import (
    CandidateProfile,
    Job,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobFeedbackEvent,
    JobRankingTask,
    RemovedPosting,
    Resume,
    ResumeVersion,
    SavedSearch,
    SearchRun,
    WorkflowTransition,
)
from app.services import domain_match, feedback_learning, job_removal, ranker, sources
from app.services import resume_verification as rv
from app.services.domain_match import DEFAULT_DOMAIN_EXCLUDES, DEFAULT_SKILL_EXCLUDES, Rules, assess
from app.services.lifecycle import WorkflowState as S
from app.settings import get_settings
from tests.conftest import make_user
from tests.jd_support import PAGES as PAGES_DIR
from tests.jd_support import FakeBrowser, PostingServer

SOURCE = "test:discovery-quality"
POSTING = ("About the role\nWe are looking for a backend engineer to build Python services.\nResponsibilities\n"
           "- Design and build APIs\n- Own services in production\nRequirements\n- 5+ years of experience with "
           "Python and PostgreSQL\n- Experience with AWS\nNice to have: Kubernetes. Benefits: remote, learning "
           "budget, health insurance and a friendly team that collaborates across time zones.")
RESUME_A = "Asha Rao\nSenior Backend Engineer at Acme 2020-2025\n- Built Python services serving 2M requests a day\n"
RESUME_B = "Asha Rao\nMachine Learning Engineer at Globex 2019-2025\n- Trained ranking models in PyTorch\n"


# ───────────── the verdict itself (no database) ─────────────

RULES = Rules(domain_excludes=list(DEFAULT_DOMAIN_EXCLUDES), skill_excludes=list(DEFAULT_SKILL_EXCLUDES),
              roles=["senior backend engineer", "ai engineer"], skills=["python", "fastapi", "machine learning"])


@pytest.mark.parametrize("title,code", [
    ("Sales Manager", "DOMAIN"), ("Digital Marketing Lead", "DOMAIN"), ("Business Development Executive", "DOMAIN"),
    ("Senior Java Developer", "SKILL"), ("Frontend Engineer", "SKILL"), ("C++ Developer", "SKILL"),
    ("iOS Engineer", "SKILL"), ("QA Analyst", "SKILL"), (".NET Developer", "SKILL")])
def test_off_domain_titles_are_hidden(title, code):
    verdict = assess(Job(title=title, company="X", description=""), RULES)
    assert not verdict.match and verdict.code == code and verdict.reason


@pytest.mark.parametrize("title", ["Backend Engineer (Python)", "Senior AI Engineer", "Java / Python Backend Engineer",
                                   "Full Stack Engineer - JavaScript", "Staff Engineer", "Platform Engineer"])
def test_matching_or_unclear_titles_are_kept(title):
    assert assess(Job(title=title, company="X", description=""), RULES).match


def test_include_terms_always_win():
    rules = Rules(**{**RULES.__dict__, "includes": ["sales engineer"]})
    assert assess(Job(title="AI Sales Engineer", company="X", description=""), rules).match


def test_the_description_decides_only_a_title_that_says_nothing():
    java = "We build with Java, Spring and Angular. Experience with PHP is a plus."
    verdict = assess(Job(title="Software Engineer", company="X", description=java), RULES)
    assert not verdict.match and verdict.code == "DESCRIPTION"
    assert assess(Job(title="Software Engineer", company="X", description=java + " Python too."), RULES).match
    assert assess(Job(title="Backend Engineer", company="X", description=java), RULES).match, "a role word keeps it"


def test_a_disabled_filter_keeps_everything():
    rules = Rules(**{**RULES.__dict__, "enabled": False})
    assert assess(Job(title="Sales Manager", company="X", description=""), rules).match


def test_learned_rules_hide_similar_jobs():
    rules = Rules(**{**RULES.__dict__, "learned_titles": [{"term": "underwriter", "evidence": 4}],
                     "learned_companies": [{"term": "spammy staffing", "evidence": 3}]})
    verdict = assess(Job(title="Underwriter Intern", company="X", description=""), rules)
    assert not verdict.match and verdict.code == "LEARNED" and "underwriter" in verdict.reason
    assert assess(Job(title="Backend Engineer", company="Spammy Staffing", description=""), rules).code == "LEARNED"


def test_title_terms_skip_generic_words():
    assert feedback_learning.title_terms("Senior Claims Adjuster Intern - Remote") == [
        "claims", "adjuster", "claims adjuster"]


# ───────────── an account with a profile ─────────────

def _clear_orphans() -> None:
    """SQLite does not enforce cascades: task rows of jobs deleted by earlier tests would attach to a reused job id."""
    with SessionLocal() as db:
        for model in (JobDescriptionFetchTask, JobDescriptionParseTask, JobRankingTask, WorkflowTransition):
            db.execute(delete(model).where(model.job_id.not_in(select(Job.id))))
        for model in (ResumeVersion, Resume):
            db.execute(delete(model).where(model.candidate_profile_id.not_in(select(CandidateProfile.id))))
        db.commit()


@pytest.fixture
def user(monkeypatch):
    _clear_orphans()
    owner = make_user(SessionLocal)
    with SessionLocal() as db:
        db.info["user_id"] = owner
        profile = CandidateProfile(user_id=owner, name="Asha Rao", headline="Senior Backend Engineer",
                                   target_roles=["Backend Engineer", "AI Engineer"], master_resume=RESUME_A,
                                   search_preferences={"primary_skills": ["python"]})
        db.add(profile)
        db.commit()
    monkeypatch.setattr(auth, "authenticate", lambda request: auth.CurrentUser(owner, "dq@jobhunt.test", False))
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    yield owner
    with SessionLocal() as db:
        ids = select(Job.id).where(Job.user_id == owner)
        for model in (JobDescriptionFetchTask, JobDescriptionParseTask, JobRankingTask, WorkflowTransition):
            db.execute(delete(model).where(model.job_id.in_(ids)))
        db.execute(delete(Job).where(Job.user_id == owner))
        for model in (JobFeedbackEvent, RemovedPosting, ResumeVersion, Resume, SearchRun, SavedSearch, CandidateProfile):
            db.execute(delete(model).where(model.user_id == owner))
        db.commit()


def add(owner: int, title: str, description: str = POSTING, *, source: str = SOURCE, url: str = "",
        search_run_id: int | None = None, company: str | None = None) -> int:
    with SessionLocal() as db:
        db.info["user_id"] = owner
        # A company of its own each time: the filter also learns companies the person keeps dismissing.
        job, _ = sources.upsert_job(db, source=source, source_ref=uuid.uuid4().hex, title=title,
                                    company=company or f"Company {uuid.uuid4().hex[:6]}",
                                    url=url, description=description, search_run_id=search_run_id)
        return job.id


def job_of(job_id: int) -> Job:
    with SessionLocal() as db:
        return db.get(Job, job_id)


def rows(model, job_id: int) -> list:
    with SessionLocal() as db:
        return list(db.scalars(select(model).where(model.job_id == job_id)).all())


@pytest.fixture
def client():
    return TestClient(web_app)


# ───────────── hidden at discovery ─────────────

def test_an_off_domain_job_is_hidden_and_costs_nothing(user):
    job_id = add(user, "Sales Manager")
    job = job_of(job_id)
    assert job.domain_match == "NOT_MATCHING" and job.domain_reason == "Outside your domain: sales"
    assert job.state is S.SKIPPED
    assert rows(JobDescriptionParseTask, job_id) == [] and rows(JobDescriptionFetchTask, job_id) == []
    assert rows(JobRankingTask, job_id) == []
    with SessionLocal() as db:
        assert ranker.hard_filter_reason(db.get(Job, job_id), ranker.RankingPolicy(70, 0.6)) == job.domain_reason


def test_a_matching_job_goes_on_as_before(user):
    job_id = add(user, "Backend Engineer")
    assert job_of(job_id).domain_match == "MATCH" and len(rows(JobDescriptionParseTask, job_id)) == 1


def test_a_job_the_person_added_is_never_hidden(user):
    job_id = add(user, "Sales Manager", source="manual")
    assert job_of(job_id).domain_match == "KEPT" and len(rows(JobDescriptionParseTask, job_id)) == 1


def test_the_profile_decides_what_is_foreign(user):
    with SessionLocal() as db:
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user))
        profile.target_roles = ["Sales Engineer"]
        profile.search_preferences = {"primary_skills": ["java"]}
        db.commit()
    assert job_of(add(user, "Solutions Sales Engineer")).domain_match == "MATCH", "their own role is not foreign"
    assert job_of(add(user, "Senior Java Developer")).domain_match == "MATCH", "their own skill is not foreign"


def test_discovery_lists_hidden_jobs_only_when_asked(user, client):
    hidden, shown = add(user, "Digital Marketing Lead"), add(user, "Backend Engineer")
    default = client.get("/api/jobs/search").json()
    assert shown in [i["id"] for i in default["items"]] and hidden not in [i["id"] for i in default["items"]]
    assert default["domain_counts"] == {"matching": 1, "not_matching": 1}
    only = client.get("/api/jobs/search?domain=not_matching").json()
    assert [i["id"] for i in only["items"]] == [hidden]
    assert only["items"][0]["domain_reason"] == "Outside your domain: digital marketing"
    triage = client.get("/api/jobs/search?queue=true").json()
    assert hidden not in [i["id"] for i in triage["items"]] and triage["domain_counts"]["not_matching"] == 1


def test_keeping_a_hidden_job_puts_it_through_the_pipeline(user, client):
    job_id = add(user, "Sales Engineer for AI Platforms")
    assert job_of(job_id).domain_match == "NOT_MATCHING"
    out = client.post(f"/api/jobs/{job_id}/domain-keep").json()
    assert out["kept"] == [job_id]
    job = job_of(job_id)
    assert job.domain_match == "KEPT" and job.state is S.RANK_PENDING
    assert len(rows(JobDescriptionParseTask, job_id)) == 1 and len(rows(JobRankingTask, job_id)) == 1
    with SessionLocal() as db:
        event = db.scalar(select(JobFeedbackEvent).where(JobFeedbackEvent.job_id == job_id))
        assert event.action == "KEPT"
        assert domain_match.reapply(db, user, actor="test")["hidden"] == 0, "kept jobs are never re-assessed"


def test_older_jobs_are_assessed_once_by_the_sweep(user):
    with SessionLocal() as db:
        job = Job(user_id=user, source=SOURCE, source_ref=uuid.uuid4().hex, title="Android Developer",
                  company="Acme", description=POSTING, state=S.DISCOVERED)
        db.add(job)
        db.commit()
        job_id = job.id
    with SessionLocal() as db:
        assert domain_match.sweep(db)["hidden"] >= 1
    assert job_of(job_id).domain_match == "NOT_MATCHING" and job_of(job_id).state is S.SKIPPED
    with SessionLocal() as db:
        assert job_id not in [j.id for j in db.scalars(select(Job).where(Job.domain_match.is_(None))).all()]


def test_changing_the_lists_can_bring_jobs_back(user, client):
    job_id = add(user, "Growth Marketing Engineer")
    assert job_of(job_id).domain_match == "NOT_MATCHING"
    settings = client.get("/api/settings/domain").json()
    assert "marketing" in settings["exclude_terms"] and "backend" in settings["derived"]["role_words"]
    client.put("/api/settings/domain", json={**{k: settings[k] for k in (
        "enabled", "exclude_terms", "skill_excludes", "learn_from_feedback")}, "include_terms": ["growth"]})
    out = client.post("/api/jobs/domain/reapply").json()
    assert out["restored"] == 1 and job_of(job_id).domain_match == "MATCH"


# ───────────── learning from the person ─────────────

def dismiss(client, owner: int, titles: list[str]) -> list[int]:
    ids = [add(owner, title) for title in titles]
    assert client.post("/api/jobs/bulk-triage", json={"ids": ids, "action": "dismiss"}).status_code == 200
    return ids


def learned(owner: int) -> list[dict]:
    with SessionLocal() as db:
        return domain_match.preferences(db, owner)["learned"]


def test_three_dismissals_teach_the_filter(user, client):
    dismiss(client, user, ["Underwriter Associate", "Underwriter Trainee", "Senior Underwriter"])
    rule = next(e for e in learned(user) if e["term"] == "underwriter")
    assert rule["status"] == "active" and rule["evidence"] == 3 and rule["examples"]
    job = job_of(add(user, "Underwriter Team Lead"))
    assert job.domain_match == "NOT_MATCHING" and job.domain_reason.startswith("Similar to jobs you dismissed: 'underwriter'")


def test_two_dismissals_are_not_enough(user, client):
    dismiss(client, user, ["Underwriter Associate", "Underwriter Trainee"])
    assert not [e for e in learned(user) if e["term"] == "underwriter"]
    assert job_of(add(user, "Underwriter Team Lead")).domain_match == "MATCH"


def test_the_candidates_own_roles_and_skills_are_never_learned(user, client):
    dismiss(client, user, ["Python Backend Engineer", "Backend Engineer Python", "Senior Python Backend Engineer"])
    terms = {e["term"] for e in learned(user)}
    assert not any("python" in t or "backend" in t for t in terms)


def test_keeping_similar_jobs_turns_a_rule_off(user, client):
    dismiss(client, user, ["Underwriter Associate", "Underwriter Trainee", "Senior Underwriter"])
    hidden = add(user, "Underwriter Team Lead")
    client.post(f"/api/jobs/{hidden}/domain-keep")
    rule = next(e for e in learned(user) if e["term"] == "underwriter")
    assert rule["status"] == "disabled" and rule["positive"] == 1


def test_the_person_can_forget_or_promote_a_rule(user, client):
    dismiss(client, user, ["Underwriter Associate", "Underwriter Trainee", "Senior Underwriter"])
    out = client.put("/api/settings/domain/learned", json={"term": "underwriter", "kind": "title",
                                                          "action": "promote"}).json()
    assert "underwriter" in out["exclude_terms"] and not [e for e in out["learned"] if e["term"] == "underwriter"]
    dismiss(client, user, ["Claims Adjuster", "Claims Adjuster Trainee", "Senior Claims Adjuster"])
    client.put("/api/settings/domain/learned", json={"term": "claims", "kind": "title", "action": "delete"})
    dismiss(client, user, ["Claims Lead"])  # learning runs again: a forgotten rule stays forgotten
    rule = next(e for e in learned(user) if e["term"] == "claims")
    assert rule["status"] == "disabled" and rule["forgotten"]


def test_deletions_are_remembered_after_the_job_is_gone(user, client):
    ids = [add(user, t) for t in ("Account Director", "Account Director EMEA", "Account Director APAC")]
    client.post("/api/jobs/bulk-delete", json={"ids": ids})
    assert all(job_of(i) is None for i in ids)
    with SessionLocal() as db:
        events = db.scalars(select(JobFeedbackEvent).where(JobFeedbackEvent.user_id == user,
                                                           JobFeedbackEvent.action == "DELETED")).all()
    assert {e.job_id for e in events} == set(ids)
    assert any(e["term"] == "account" for e in learned(user)), "the shortest phrase: director is a generic word"


# ───────────── postings that no longer exist ─────────────

@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(get_settings(), "jd_fetch_allow_private_hosts", True)
    with PostingServer() as posting_server:
        yield posting_server


def check(owner: int, job_id: int, browser=None) -> str:
    from app.services.jd_recovery import utcnow

    return asyncio.run(job_removal.check_one(SessionLocal, job_id, owner, utcnow(), browser=browser))


def test_the_listing_check_deletes_only_postings_that_are_gone(user, server):
    gone, closed = add(user, "Backend Engineer", url=server.url("gone")), add(user, "AI Engineer",
                                                                             url=server.url("closed.html"))
    live, login, busy = (add(user, "Backend Engineer", url=server.url(p)) for p in ("posting.html", "login.html", "busy"))
    assert check(user, gone) == "deleted" and check(user, closed) == "deleted"
    assert job_of(gone) is None and job_of(closed) is None
    assert check(user, live) == "live" and check(user, login) == "unknown" and check(user, busy) == "unknown"
    for job_id in (live, login, busy):
        assert job_of(job_id).listing_checked_at is not None, "checked, never deleted on a guess"
    with SessionLocal() as db:
        assert {r.reason for r in db.scalars(select(RemovedPosting).where(RemovedPosting.user_id == user))} == \
            {"JOB_REMOVED", "JOB_CLOSED"}
        assert db.scalars(select(JobFeedbackEvent).where(JobFeedbackEvent.user_id == user)).all() == [], \
            "a dead posting says nothing about the person's taste"


def test_approved_or_applied_jobs_are_never_deleted(user, server):
    job_id = add(user, "Backend Engineer", url=server.url("gone"))
    with SessionLocal() as db:
        db.get(Job, job_id).status = "applied"
        db.commit()
    assert check(user, job_id) == "kept" and job_of(job_id) is not None


def move(job_id: int, **fields) -> None:
    """Put a job where a test needs it, past the lifecycle's transition guard."""
    with SessionLocal() as db:
        db.execute(update(Job).where(Job.id == job_id).values(**fields))
        db.commit()


BANNER_PAGE = f"<html><head><title>Backend Engineer</title></head><body><main><h1>Backend Engineer</h1>" \
              f"<div class='notice'>No longer accepting applications</div><p>{POSTING}</p></main></body></html>"
SCRIPT_SHELL = "<html><head><title>Careers</title></head><body><div id='root'>Please enable JavaScript</div></body></html>"


def test_tailored_jobs_waiting_for_approval_are_checked_and_deleted(user, server):
    job_id = add(user, "Backend Engineer", url=server.url("gone"))
    move(job_id, state=S.READY_TO_APPLY, status="tailored")
    with SessionLocal() as db:
        assert job_id in {j for j, _ in job_removal.due_jobs(db, job_removal.utcnow(), 500)}
    assert check(user, job_id) == "deleted" and job_of(job_id) is None


def test_an_approved_job_whose_posting_closed_is_kept_and_labelled_until_it_reopens(user, server):
    server.routes["approved"] = (404, "<h1>Not found</h1>", {})
    job_id = add(user, "Backend Engineer", url=server.url("custom/approved"))
    move(job_id, state=S.READY_TO_APPLY, status="approved")
    assert check(user, job_id) == "kept"
    job = job_of(job_id)
    assert job is not None and job.posting_closed_at is not None and job.posting_closed_reason == "JOB_REMOVED"
    server.routes["approved"] = (200, (PAGES_DIR / "posting.html").read_text(), {})
    assert check(user, job_id) == "live" and job_of(job_id).posting_closed_at is None


def test_a_posting_that_still_shows_its_description_but_takes_no_applications_is_deleted(user, server):
    server.routes["banner"] = (200, BANNER_PAGE, {})
    job_id = add(user, "Backend Engineer", url=server.url("custom/banner"))
    assert check(user, job_id) == "deleted" and job_of(job_id) is None


def test_the_browser_reads_a_page_http_could_not(user, server):
    server.routes["shell"] = (200, SCRIPT_SHELL, {})
    closed, live = (add(user, "Backend Engineer", url=server.url(f"custom/shell?{n}")) for n in ("a", "b"))
    browser = FakeBrowser({"shell?a": (200, BANNER_PAGE)})
    assert check(user, closed, browser) == "deleted+browser" and job_of(closed) is None
    assert browser.calls, "the page was rendered"
    live_browser = FakeBrowser({"shell?b": (200, (PAGES_DIR / "posting.html").read_text())})
    assert check(user, live, live_browser) == "live+browser" and job_of(live) is not None


def test_a_login_wall_never_reaches_the_browser(user, server):
    job_id = add(user, "Backend Engineer", url=server.url("login.html"))
    browser = FakeBrowser({"login.html": (200, BANNER_PAGE)})
    assert check(user, job_id, browser) == "unknown" and browser.calls == []
    assert job_of(job_id) is not None


def test_jobs_a_worker_holds_or_that_were_applied_to_are_not_checked(user, server):
    from datetime import timedelta

    from app.models import TailorTask

    held, tailoring, applied, free = (add(user, "Backend Engineer", url=server.url("gone")) for _ in range(4))
    now = job_removal.utcnow()
    move(held, lock_token="t", locked_by="w", locked_at=now, lock_expires_at=now + timedelta(minutes=5))
    move(applied, state=S.APPLIED, status="applied")
    with SessionLocal() as db:
        db.add(TailorTask(job_id=tailoring, user_id=user, status="running"))
        db.commit()
    with SessionLocal() as db:
        due = {j for j, _ in job_removal.due_jobs(db, now, 500)}
    assert free in due and not due & {held, tailoring, applied}
    with SessionLocal() as db:
        db.execute(delete(TailorTask).where(TailorTask.job_id == tailoring))
        db.commit()


def test_a_deleted_posting_is_not_imported_again(user, server):
    job_id = add(user, "Backend Engineer", url=server.url("gone"))
    with SessionLocal() as db:
        ref = db.get(Job, job_id).source_ref
    check(user, job_id)
    item = {"title": "Backend Engineer", "company": "Acme", "url": server.url("gone"), "description": POSTING,
            "site": "linkedin", "id": "dead-1"}
    with SessionLocal() as db:
        db.info["user_id"] = user
        first = sources.import_apify_items(db, [item], user_id=user)
    assert first["created"] == 1
    with SessionLocal() as db:
        db.info["user_id"] = user
        created = db.scalar(select(Job).where(Job.user_id == user, Job.source_ref == "linkedin:dead-1"))
        assert job_removal.remove_posting(db, created, "JOB_REMOVED", actor="test") == "deleted"
        db.commit()
    with SessionLocal() as db:
        db.info["user_id"] = user
        again = sources.import_apify_items(db, [item], user_id=user)
        assert job_removal.is_removed(db, user, source=SOURCE, source_ref=ref)
    assert again["created"] == 0 and again["previously_removed"] == 1


def test_check_listings_button_reads_this_accounts_postings_and_deletes_the_gone(user, server, client):
    gone, live = add(user, "Backend Engineer", url=server.url("gone")), add(user, "AI Engineer",
                                                                            url=server.url("posting.html"))
    other = make_user(SessionLocal)
    theirs = add(other, "Backend Engineer", url=server.url("gone"))
    started = client.post("/api/jobs/listing-check")
    assert started.status_code == 202 and started.json()["due"] == 2
    assert job_of(gone) is None and job_of(live).listing_checked_at is not None
    with SessionLocal() as db:
        db.info["user_id"] = other
        assert db.get(Job, theirs).listing_checked_at is None, "another account's postings are not touched"
    status = client.get("/api/jobs/listing-check/status", params={"since": started.json()["since"]}).json()
    assert status == {"checked": 2, "removed": 1, "running": False}


def test_check_listings_moves_on_from_postings_it_just_read(user, server, client):
    add(user, "AI Engineer", url=server.url("posting.html"))
    assert client.post("/api/jobs/listing-check").json()["due"] == 1
    assert client.post("/api/jobs/listing-check").json()["due"] == 0


def test_check_listings_refuses_a_double_press_and_a_server_that_keeps_removed_postings(user, client, monkeypatch):
    job_removal._manual_running.add(user)
    try:
        assert client.post("/api/jobs/listing-check").status_code == 409
    finally:
        job_removal._manual_running.discard(user)
    monkeypatch.setattr(get_settings(), "auto_delete_removed_jobs", False)
    assert client.post("/api/jobs/listing-check").status_code == 409


def test_the_scheduled_listing_check_is_bounded(user, server, monkeypatch):
    monkeypatch.setattr(get_settings(), "listing_check_batch_size", 2)
    for _ in range(3):
        add(user, "Backend Engineer", url=server.url("posting.html"))
    with SessionLocal() as db:
        due = job_removal.due_jobs(db, job_removal.utcnow(), 2)
    assert len(due) == 2
    summary = asyncio.run(job_removal.check_listings(SessionLocal, job_removal.utcnow(), limit=2))
    assert summary["checked"] == 2


# ───────────── the resume per search and per job ─────────────

@pytest.fixture
def resumes(user):
    with SessionLocal() as db:
        db.info["user_id"] = user
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user))
        first = rv.record_version(db, profile, RESUME_A, source="manual_text", name="Backend resume")
        second = rv.record_version(db, profile, RESUME_B, source="manual_text", name="ML resume", activate=False)
        profile.master_resume = RESUME_A
        db.commit()
        return first.id, second.id


def test_a_saved_search_chooses_the_resume_of_the_jobs_it_finds(user, resumes):
    _, ml = resumes
    with SessionLocal() as db:
        db.info["user_id"] = user
        setup = SavedSearch(user_id=user, name="ML roles", settings={"roles": ["ML Engineer"], "resume_id": ml})
        db.add(setup)
        db.flush()
        run = SearchRun(user_id=user, provider="apify", saved_search_id=setup.id, external_run_id=uuid.uuid4().hex)
        db.add(run)
        db.commit()
        run_id = run.id
    job_id = add(user, "Machine Learning Engineer", search_run_id=run_id)
    assert job_of(job_id).resume_id == ml
    assert job_of(add(user, "Backend Engineer")).resume_id is None, "no setup: the active resume"


def test_workflows_read_the_jobs_resume_and_never_change_the_active_one(user, resumes):
    active, ml = resumes
    job_id = add(user, "Machine Learning Engineer")
    with SessionLocal() as db:
        db.info["user_id"] = user
        job = db.get(Job, job_id)
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user))
        plain = rv.for_job(db, profile, job)
        assert plain.chosen_resume.id == active, "no choice: the active resume"
        job.resume_id = ml
        view = rv.for_job(db, profile, job)
        assert rv.raw_text(view) == RESUME_B and rv.resume_for(db, view).id == ml
        assert "Trained ranking models in PyTorch" in view.master_resume, "the structured source renders its data"
        assert view.resume_source == "structured" and view.master_resume != RESUME_B
        assert view.name == "Asha Rao" and view.id == profile.id
        snapshot = ranker.resume_snapshot(db, view)
        assert snapshot.content == RESUME_B
        from app.services.context_builder import build_ranking_context

        context = build_ranking_context(db, view, job)
        assert context.resume["id"] == ml
        with pytest.raises(AttributeError):
            view.master_resume = "changed"
        db.commit()
    with SessionLocal() as db:
        assert db.get(Resume, active).is_active and not db.get(Resume, ml).is_active
        assert db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user)).master_resume == RESUME_A


def test_choosing_a_resume_for_old_jobs_ranks_them_again(user, resumes, client):
    _, ml = resumes
    job_id = add(user, "Machine Learning Engineer")
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        ranker.queue_job(db, job, policy=ranker.RankingPolicy(70, 0.6), actor="test")
        db.commit()
    choices = client.get("/api/resumes/choices").json()["items"]
    assert {c["name"] for c in choices} >= {"Backend resume", "ML resume"}
    out = client.post("/api/jobs/bulk-resume", json={"ids": [job_id], "resume_id": ml}).json()
    assert out["changed"] == [job_id] and out["ranking_queued"] == [job_id]
    assert job_of(job_id).resume_id == ml and client.get(f"/api/jobs/{job_id}").json()["resume_choice_id"] == ml
    back = client.put(f"/api/jobs/{job_id}/resume", json={"resume_id": None}).json()
    assert back["resume_id"] is None and job_of(job_id).resume_id is None
    assert client.put("/api/resumes/999999/", json={"name": "x"}).status_code in (404, 405)


def test_a_resume_can_be_renamed_for_the_pickers(user, resumes, client):
    active, ml = resumes
    client.put(f"/api/resumes/{ml}/tags", json={"domain": "ML", "roles": ["ML Engineer"]})
    assert client.put(f"/api/resumes/{active}", json={"name": "Platform resume"}).json()["name"] == "Platform resume"
    out = client.put(f"/api/resumes/{ml}", json={"name": "  Applied ML  "}).json()
    assert out == {"id": ml, "name": "Applied ML"}
    with SessionLocal() as db:
        renamed = db.get(Resume, ml)
        # Only the label changed: tags, text and which resume is active stay as they were.
        assert (renamed.domain, renamed.roles, renamed.is_active) == ("ML", ["ML Engineer"], False)
        assert renamed.raw_text == RESUME_B and db.get(Resume, active).is_active
    assert client.put(f"/api/resumes/{ml}", json={"name": "   "}).status_code == 422
    assert client.put(f"/api/resumes/{ml}", json={"name": ""}).status_code == 422


def test_the_mcp_context_carries_the_jobs_own_resume(user, resumes):
    from app.mcp import tools

    _, ml = resumes
    job_id = add(user, "Machine Learning Engineer")
    with SessionLocal() as db:
        db.info["user_id"] = user
        job = db.get(Job, job_id)
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user))
        assert tools._job_resume(db, profile, job) == {}
        job.resume_id = ml
        own = tools._job_resume(db, profile, job)["resume"]
    assert own["resume_id"] == ml and own["name"] == "ML resume"
    assert "Trained ranking models in PyTorch" in own["text"], "the job's own resume, in the policy's form"


def test_archiving_puts_a_job_away_without_teaching_the_filter(user, client):
    job_id = add(user, "Backend Engineer")
    assert client.post(f"/api/jobs/{job_id}/triage", json={"action": "archive"}).json()["status"] == "archived"
    job = job_of(job_id)
    assert job.status == "archived" and job.triaged_at is not None
    assert job_id not in [i["id"] for i in client.get("/api/jobs/search?queue=true").json()["items"]]
    assert job_id in [i["id"] for i in client.get("/api/jobs/search?stage=archived").json()["items"]]
    with SessionLocal() as db:
        assert ranker.hard_filter_reason(db.get(Job, job_id), ranker.RankingPolicy(70, 0.6)) == "Archived by you"
        assert db.scalars(select(JobFeedbackEvent).where(JobFeedbackEvent.job_id == job_id)).all() == []
    client.post(f"/api/jobs/{job_id}/triage", json={"action": "restore"})
    job = job_of(job_id)
    assert job.status == "discovered" and job.triaged_at is None


# ───────────── the search's own scope ─────────────

def _scoped_run(owner: int, **params) -> int:
    with SessionLocal() as db:
        db.info["user_id"] = owner
        run = SearchRun(user_id=owner, provider="apify", external_run_id=uuid.uuid4().hex,
                        actor_id="openclawai/job-board-scraper", roles=["Backend Engineer"],
                        location="Bengaluru, India", search_params={"primary_skills": ["Python"], **params})
        db.add(run)
        db.commit()
        return run.id


def _item(title: str, location: str, description: str = POSTING) -> dict:
    return {"title": title, "company": f"Company {uuid.uuid4().hex[:6]}", "location": location, "site": "linkedin",
            "id": uuid.uuid4().hex, "description": description, "job_url": f"https://example.test/{uuid.uuid4().hex}"}


def _import(owner: int, run_id: int, items: list[dict]) -> dict:
    with SessionLocal() as db:
        db.info["user_id"] = owner
        return sources.import_apify_items(db, items, run_id, "linkedin", user_id=owner)


def _by_title(owner: int, run_id: int) -> dict[str, Job]:
    with SessionLocal() as db:
        return {j.title: j for j in db.scalars(select(Job).where(Job.user_id == owner, Job.search_run_id == run_id))}


def test_jobs_outside_the_searchs_roles_skills_or_country_are_hidden(user, client):
    run_id = _scoped_run(user)
    stats = _import(user, run_id, [
        _item("Senior Backend Engineer", "Bengaluru, Karnataka, India"),
        _item("Data Analyst", "Bengaluru, Karnataka, India"),
        _item("Backend Engineer", "Austin, TX, United States"),
        _item("Backend Engineer II", "Pune, India", description=POSTING.replace("Python", "Go")),
    ])
    jobs = _by_title(user, run_id)
    assert stats["created"] == 4 and stats["not_matching"] == 3
    assert jobs["Senior Backend Engineer"].domain_match == "MATCH"
    assert jobs["Data Analyst"].domain_reason.startswith("Not one of this search's roles")
    assert jobs["Backend Engineer"].domain_reason.startswith("Outside this search's location")
    assert jobs["Backend Engineer II"].domain_reason == "Does not mention Python"
    for title in ("Data Analyst", "Backend Engineer", "Backend Engineer II"):
        hidden = jobs[title]
        assert hidden.state is S.SKIPPED and rows(JobDescriptionParseTask, hidden.id) == []
        assert rows(JobRankingTask, hidden.id) == [] and rows(JobDescriptionFetchTask, hidden.id) == []
    assert client.post(f"/api/jobs/{jobs['Data Analyst'].id}/domain-keep").json()["kept"] == [jobs["Data Analyst"].id]
    assert job_of(jobs["Data Analyst"].id).domain_match == "KEPT"


def test_runs_without_a_recorded_scope_import_as_before(user):
    with SessionLocal() as db:
        db.info["user_id"] = user
        run = SearchRun(user_id=user, provider="apify", external_run_id=uuid.uuid4().hex)
        db.add(run)
        db.commit()
        run_id = run.id
    _import(user, run_id, [_item("Data Analyst", "Austin, TX")])
    assert _by_title(user, run_id)["Data Analyst"].domain_match == "MATCH"


def test_the_import_stops_at_the_setups_limit_even_when_pulled_again(user):
    run_id = _scoped_run(user, max_jobs=2)
    items = [_item(f"Backend Engineer {n}", "Bengaluru, India") for n in range(5)]
    stats = _import(user, run_id, items)
    assert stats["created"] == 2 and stats["over_limit"] == 3 and len(_by_title(user, run_id)) == 2
    again = _import(user, run_id, items)
    assert again["created"] == 0 and len(_by_title(user, run_id)) == 2


def test_reapplying_the_domain_lists_keeps_scope_hidden_jobs_hidden(user, client):
    run_id = _scoped_run(user)
    _import(user, run_id, [_item("Data Analyst", "Bengaluru, India")])
    hidden = _by_title(user, run_id)["Data Analyst"]
    assert client.post("/api/jobs/domain/reapply").status_code == 200
    job = job_of(hidden.id)
    assert job.domain_match == "NOT_MATCHING" and job.domain_reason.startswith("Not one of this search's roles")
    assert rows(JobDescriptionParseTask, hidden.id) == [] and rows(JobRankingTask, hidden.id) == []


def test_a_recovered_description_is_checked_against_the_searchs_skills(user):
    from types import SimpleNamespace

    from app.services import jd_recovery

    run_id = _scoped_run(user)
    _import(user, run_id, [_item("Backend Engineer", "Bengaluru, India", description="")])
    job_id = _by_title(user, run_id)["Backend Engineer"].id
    assert job_of(job_id).domain_match == "MATCH", "no description yet: kept"
    with SessionLocal() as db:
        db.info["user_id"] = user
        job = db.get(Job, job_id)
        task = JobDescriptionFetchTask(user_id=user, job_id=job_id, status="PROCESSING")
        db.add(task)
        db.flush()
        outcome = SimpleNamespace(text=POSTING.replace("Python", "Java"), method="HTTP", final_url=job.url)
        assert jd_recovery.apply_recovered(db, job, task, outcome, jd_recovery.utcnow()) == "recovered"
        db.commit()
    job = job_of(job_id)
    assert job.domain_match == "NOT_MATCHING" and job.domain_reason == "Does not mention Python"
    assert rows(JobDescriptionParseTask, job_id) == [] and rows(JobRankingTask, job_id) == []


def test_a_setups_primary_skill_is_never_a_foreign_skill(user):
    with SessionLocal() as db:
        db.info["user_id"] = user
        db.add(SavedSearch(user_id=user, name="Java roles", settings={"roles": ["Backend Engineer"],
                                                                      "primary_skills": ["Java"]}, weekdays=[]))
        db.commit()
    assert job_of(add(user, "Senior Java Developer")).domain_match == "MATCH"


def test_a_compound_primary_skill_protects_the_skill_it_names(user):
    with SessionLocal() as db:
        db.info["user_id"] = user
        db.add(SavedSearch(user_id=user, name="Spring roles", settings={"roles": ["Backend Engineer"],
                                                                        "primary_skills": ["Java 17"]}, weekdays=[]))
        db.commit()
    assert job_of(add(user, "Java Backend Engineer")).domain_match == "MATCH"
    assert job_of(add(user, "Senior PHP Developer")).domain_match == "NOT_MATCHING", "other exclusions still apply"


def test_the_job_and_its_packet_name_the_resume_they_use(user, resumes, client):
    from app.services import resume_matching
    from app.services.workflow import persist_tailored_resume

    active, ml = resumes
    job_id = add(user, "Backend Engineer")
    # No resume matched by tags: the screens say the active resume is used, and why.
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["resume_in_use"]["id"] == active and detail["resume_in_use"]["choice"] is None
    assert "active resume is used" in detail["resume_in_use"]["how"]
    assert detail["packet_source"] is None

    with SessionLocal() as db:
        db.info["user_id"] = user
        resume_matching.set_tags(db.get(Resume, ml), [{"domain": "ML", "roles": ["Backend Engineer"]}])
        job = db.get(Job, job_id)
        assert resume_matching.auto_assign(db, job) is not None and job.resume_id == ml
        db.commit()
        # A packet saved without a tailoring context (MCP, skip tailoring) records the resume it was made from.
        persist_tailored_resume(db, job, {"summary": "", "skills": [], "experience": []}, markdown="# Asha")
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["resume_in_use"]["id"] == ml and detail["resume_in_use"]["choice"] == "AUTO"
    assert "role 'backend engineer'" in detail["resume_in_use"]["how"].lower()
    packet = client.get(f"/api/tailor/{job_id}").json()["resume"]
    assert packet["source"]["resume"]["name"] == "ML resume" and not packet["source"]["differs"]
    assert packet["resume_in_use"]["id"] == ml

    # The person moves the job to the active resume: the packet still names its own source, and says it differs.
    client.put(f"/api/jobs/{job_id}/resume", json={"mode": "active"})
    packet = client.get(f"/api/tailor/{job_id}").json()["resume"]
    assert packet["source"]["resume"]["id"] == ml and packet["source"]["differs"]
    assert packet["resume_in_use"]["id"] == active and packet["resume_in_use"]["choice"] == "ACTIVE"
    # Pinned to the active resume while the tags clearly pick the ML one: the tag match is offered, flagged as
    # replacing the person's choice. Nothing changes until they take it.
    assert packet["resume_in_use"]["rematch"]["resume"]["id"] == ml and packet["resume_in_use"]["rematch"]["pinned"]
    client.put(f"/api/jobs/{job_id}/resume", json={"mode": "fixed", "resume_id": ml})
    assert client.get(f"/api/jobs/{job_id}").json()["resume_in_use"]["rematch"] is None, "already the tag match"


def test_a_job_matched_before_its_resume_was_tagged_is_offered_a_rematch(user, resumes, client):
    from app.services import resume_matching

    active, ml = resumes
    job_id = add(user, "Staff Software Engineer, Growth")
    detail = client.get(f"/api/jobs/{job_id}").json()["resume_in_use"]
    assert detail["id"] == active and detail["rematch"] is None, "no tag matches: nothing to offer"

    # Tagged after the job was found: the job keeps the active resume until the person re-matches it, but says so.
    with SessionLocal() as db:
        db.info["user_id"] = user
        resume_matching.set_tags(db.get(Resume, ml), [{"domain": "Backend", "roles": ["Staff Software Engineer"]}])
        db.commit()
    detail = client.get(f"/api/jobs/{job_id}").json()["resume_in_use"]
    assert detail["id"] == active and detail["choice"] is None
    assert detail["rematch"]["resume"]["id"] == ml and "staff software engineer" in detail["rematch"]["reason"].lower()
    assert detail["rematch"]["pinned"] is False
    assert "when this job was found" in detail["how"]
    with SessionLocal() as db:
        db.info["user_id"] = user
        assert db.get(Job, job_id).resume_id is None, "showing the offer changes nothing"

    client.put(f"/api/jobs/{job_id}/resume", json={"mode": "auto"})
    detail = client.get(f"/api/jobs/{job_id}").json()["resume_in_use"]
    assert detail["id"] == ml and detail["choice"] == "AUTO" and detail["rematch"] is None

    # Its tags removed again: the AUTO job would now fall back to the active resume, and is offered that.
    with SessionLocal() as db:
        db.info["user_id"] = user
        resume_matching.set_tags(db.get(Resume, ml), [])
        db.commit()
    detail = client.get(f"/api/jobs/{job_id}").json()["resume_in_use"]
    assert detail["id"] == ml and detail["rematch"] == {"resume": None, "reason": "no resume matches clearly now",
                                                            "pinned": False}


def test_rematch_takes_only_the_jobs_the_filters_select(user, resumes, client):
    from datetime import timedelta

    from app.models import now
    from app.services import resume_matching

    _, ml = resumes
    fresh, old, archived, other_board = (add(user, "Staff Software Engineer") for _ in range(4))
    with SessionLocal() as db:
        db.info["user_id"] = user
        db.execute(update(Job).where(Job.id.in_([fresh, old, archived, other_board]))
                   .values(source_board="greenhouse", status="shortlisted"))
        db.execute(update(Job).where(Job.id == old).values(discovered_at=now() - timedelta(days=60)))
        db.execute(update(Job).where(Job.id == archived).values(status="archived"))
        db.execute(update(Job).where(Job.id == other_board).values(source_board="lever"))
        resume_matching.set_tags(db.get(Resume, ml), [{"domain": "Backend", "roles": ["Staff Software Engineer"]}])
        db.commit()

    def plan(**filters):
        return client.post("/api/jobs/resume/rematch?dry_run=true", json=filters).json()

    everything = plan()
    assert everything["changing"] == 3, "archived jobs are left out unless asked for"
    assert everything["into"] == [{"resume": "ML resume · v2", "jobs": 3}]
    assert {"name": "greenhouse", "jobs": 3} in everything["options"]["boards"]
    assert plan(stages=["ranked", "archived"])["changing"] == 4
    assert plan(stages=["tailored"])["changing"] == 0
    assert plan(found_within_days=30)["changing"] == 2
    assert plan(board="lever")["changing"] == 1
    assert client.post("/api/jobs/resume/rematch?dry_run=true").json()["changing"] == 3, "no body: the defaults"

    out = client.post("/api/jobs/resume/rematch", json={"found_within_days": 30, "board": "greenhouse"}).json()
    assert out["changed"] == [fresh]
    assert job_of(fresh).resume_id == ml and job_of(old).resume_id is None and job_of(archived).resume_id is None
