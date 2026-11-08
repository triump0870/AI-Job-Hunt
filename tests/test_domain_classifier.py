"""The hybrid domain pipeline: rules hide only the unambiguous, a fast model reads the rest, code keeps a NO_MATCH only when
its evidence holds, and with the mode off nothing changes. The model is a stub: these test what code does with an answer.
scripts/domain_classifier_eval.py measures a real one."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app.main  # noqa: F401  migrates the test database, as every API test does
from app import auth
from app.db import SessionLocal
from app.main import app as web_app
from app.models import (
    AppSetting,
    CandidateProfile,
    Job,
    JobClassificationCache,
    JobClassificationTask,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobFeedbackEvent,
    JobRankingTask,
    SearchRun,
    WorkflowTransition,
)
from app.prompts.contracts import DomainClassificationResult
from app.services import domain_classifier as dc
from app.services import domain_match as dm
from app.services import feedback_learning, ranker, search_scope, sources, workers
from app.services.domain_match import DEFAULT_DOMAIN_EXCLUDES, DEFAULT_SKILL_EXCLUDES, Rules
from app.services.lifecycle import STOPPED_BY_USER
from app.services.lifecycle import WorkflowState as S
from app.services.llm import LLMClient, LLMConfigError, LLMRateLimitError, LLMResponseError
from app.settings import get_settings
from tests.conftest import make_user

POSTING = ("About the role\nBuild Python services and LLM features for customers.\nResponsibilities\n- Own services in "
           "production\nRequirements\n- 5+ years of Python\n- Experience with AWS. Benefits: remote, learning budget, "
           "health insurance and a friendly team that collaborates across time zones.")
PAYROLL = ("About the role\nRun monthly payroll for 800 employees.\nResponsibilities\n- Reconcile benefits every month\n"
           "Requirements\n- 5+ years in payroll administration\nBenefits: remote, learning budget, health insurance and "
           "a friendly team that collaborates across several time zones and offices.")
RULES = Rules(domain_excludes=list(DEFAULT_DOMAIN_EXCLUDES), skill_excludes=list(DEFAULT_SKILL_EXCLUDES),
              roles=["staff software engineer", "lead ai engineer", "forward deployed engineer"],
              skills=["python", "llm", "genai"],
              learned_titles=[{"term": t, "evidence": 12, "status": "active"} for t in ("data", "machine", "native", "growth")],
              learned_companies=[{"term": "affirm", "evidence": 13, "positive": 0, "status": "active"}])


def job(title, company="Acme", description=""):
    return SimpleNamespace(title=title, company=company, description=description)


def answer(classification, *evidence, skills=(), reason="Because."):
    return DomainClassificationResult(classification=classification, reason=reason, matched_skills=list(skills),
                                      irrelevance_evidence=list(evidence))


# ───────────── which rules still hide ─────────────

@pytest.mark.parametrize("title,code", [
    ("Founding Account Executive", "DOMAIN"), ("Digital Marketing Manager", "DOMAIN"), ("Technical Recruiter", "DOMAIN"),
    ("Senior Java Developer", "SKILL"), ("Frontend Engineer", "SKILL"), ("iOS Developer", "SKILL")])
def test_only_the_unambiguous_hides_without_a_model(title, code):
    out = dm.triage(job(title), RULES)
    assert (out.route, out.code) == (dm.HARD, code)


@pytest.mark.parametrize("title", [
    "Senior Full Stack Engineer - Customer Service", "GTM Engineer, Marketing", "Senior Sales Engineer",
    "Staff Software Engineer, Frontend", "Staff Software Engineer, iOS Platform"])
def test_a_foreign_word_beside_technical_work_goes_to_the_model(title):
    out = dm.triage(job(title), RULES)
    assert out.route == dm.ASK and out.signals


def test_every_job_the_old_filter_wrongly_hid_now_goes_on_or_to_the_model():
    """What the account's own data showed: these were applied to, shortlisted or scored 70 and up."""
    rules = Rules(**{**RULES.__dict__, "learned_titles": [{"term": t, "evidence": 9} for t in (
        "tech", "program", "technology", "data", "safeguards", "science", "grc", "growth", "scaling", "technical", "native")]})
    for title, company in [
            ("Tech lead - TikTok Search", "TikTok"), ("Lead Software Engineer, Trading Technology", "JPMorganChase"),
            ("Senior Staff Software Engineer (Data Platform)", "Randstad"), ("Staff+ Research Engineer, RL Data Platform", "A"),
            ("Staff+ Software Engineer, Safeguards Human Review Tooling", "A"), ("Staff+ Software Engineer, Claude Science", "A"),
            ("Staff+ Software Engineer, GRC Platform", "A"), ("Staff Software Engineer, Growth ", "A"),
            ("Staff + Sr. Software Engineer, Scaling", "A"), ("Senior Technical Lead", "B"),
            ("Forward Deployed Engineer (m/w/d) KI-native Entwicklung", "Union Investment"),
            ("Lead Data & ML Engineer", "C"), ("Staff Software Engineer, Backend (Identity Decisioning)", "Affirm")]:
        assert dm.assess(job(title, company), rules).match is False, title  # the old filter hid each one
        assert dm.triage(job(title, company), rules).route != dm.HARD, title


def test_a_term_the_person_chose_still_hides_technical_work():
    rules = Rules(**{**RULES.__dict__, "domain_excludes": [*RULES.domain_excludes, "decisioning"]})
    assert dm.triage(job("Staff Software Engineer, Decisioning"), rules).route == dm.HARD


def test_an_include_term_wins_over_every_rule():
    rules = Rules(**{**RULES.__dict__, "includes": ["recruiter"]})
    assert dm.triage(job("Technical Recruiter"), rules).route == dm.POSITIVE


def test_only_cleanly_learned_companies_hide():
    rules = Rules(**{**RULES.__dict__, "learned_companies": [
        {"term": "affirm", "evidence": 13, "positive": 0}, {"term": "hdfc bank", "evidence": 4, "positive": 0, "clean": True},
        {"term": "kept co", "evidence": 4, "positive": 1, "clean": True}]})
    assert dm.triage(job("Staff Software Engineer", "Affirm", POSTING), rules).route != dm.HARD  # from mass deletes
    assert dm.triage(job("Virtual RM", "HDFC Bank"), rules).route == dm.HARD
    assert dm.triage(job("Virtual RM", "Kept Co"), rules).route == dm.ASK


def test_a_clear_match_spares_the_model_call_and_a_doubt_cancels_that():
    assert dm.triage(job("Lead AI Engineer", description=POSTING), RULES).route == dm.POSITIVE
    assert dm.triage(job("Staff Software Engineer", description=POSTING), RULES).route == dm.POSITIVE
    assert dm.triage(job("Staff Software Engineer"), RULES).route == dm.ASK  # no text, no skill: nothing shows a fit
    assert dm.triage(job("Staff Software Engineer", description=POSTING), RULES, ("SCOPE_SKILL",)).route == dm.ASK
    assert dm.triage(job("Staff Software Engineer, Growth", description=POSTING), RULES).route == dm.ASK


@pytest.mark.parametrize("text,skill", [("We build on LLMs", "llm"), ("Generative AI products", "genai"),
                                        ("Large language models at scale", "llm"), ("Runs on k8s", "kubernetes"),
                                        ("KI-native Entwicklung", "ai")])
def test_a_skill_is_recognised_however_a_posting_writes_it(text, skill):
    assert dm.names_skill(text, skill)


def test_a_skill_is_not_found_inside_another_word():
    assert not dm.names_skill("JavaScript and maintenance", "java") and not dm.names_skill("Trained staff", "ai")


# ───────────── search scope ─────────────

def test_scope_role_and_skill_misses_are_doubts_but_a_place_miss_still_hides(user):
    with SessionLocal() as db:
        db.info["user_id"] = user
        run = SearchRun(user_id=user, external_run_id="test-scope", roles=["AI Engineer"], location="Berlin, Germany",
                        search_params={"primary_skills": ["Python"], "distance": 50})
        db.add(run)
        db.flush()
        far = SimpleNamespace(title="Data Analyst", description="Python dashboards.", location="Pune, India",
                              search_run_id=run.id)
        near = SimpleNamespace(**{**far.__dict__, "location": "Berlin, Germany"})
        assert search_scope.verdict_for(db, near).match is False  # the old check hid both
        place, doubts = search_scope.hybrid(db, far)
        assert place is not None and place.code == search_scope.LOCATION and doubts == (search_scope.ROLE,)
        place, doubts = search_scope.hybrid(db, near)
        assert place is None and doubts == (search_scope.ROLE,)
        db.rollback()


# ───────────── the evidence gate ─────────────

PAYROLL_TITLE = "Payroll Specialist"


def decide(result, title=PAYROLL_TITLE, posting=PAYROLL):
    return dc.decide(result, title=title, posting=posting, rules=RULES)


def test_a_no_match_with_real_evidence_stands():
    out = decide(answer("NO_MATCH", "Run monthly payroll"))
    assert (out.classification, out.gate_code, out.evidence) == ("NO_MATCH", None, ["Run monthly payroll"])


@pytest.mark.parametrize("result,kwargs,code", [
    (answer("NO_MATCH"), {}, "NO_EVIDENCE"),
    (answer("NO_MATCH", "quota-carrying sales role"), {}, "EVIDENCE_NOT_IN_POSTING"),
    (answer("NO_MATCH", "this is sales"), {"title": "Lead Backend", "posting": "Python work."}, "EVIDENCE_NOT_IN_POSTING"),
    (answer("NO_MATCH", "Run monthly payroll", skills=["python"]), {"posting": PAYROLL + " Some Python."}, "SKILLS_MATCHED"),
    (answer("NO_MATCH", "Lead AI Engineer"), {"title": "Lead AI Engineer", "posting": "Own the roadmap."}, "TITLE_PROTECTED"),
    (answer("NO_MATCH", "Run monthly payroll"), {"posting": PAYROLL + " Uses Python and LLMs."}, "TEXT_NAMES_OWN_SKILLS"),
    (answer("NO_MATCH", "Platform Engineer"), {"title": "Platform Engineer", "posting": ""}, "TITLE_ONLY_TECHNICAL")])
def test_a_no_match_without_sufficient_evidence_becomes_uncertain(result, kwargs, code):
    out = decide(result, **kwargs)
    assert (out.classification, out.gate_code) == ("UNCERTAIN", code)


def test_a_bare_title_with_no_technical_work_can_be_rejected():
    assert decide(answer("NO_MATCH", "Payroll Specialist"), posting="").classification == "NO_MATCH"


def test_evidence_is_compared_ignoring_case_and_line_breaks():
    assert decide(answer("NO_MATCH", "run monthly   PAYROLL")).classification == "NO_MATCH"


def test_invented_skills_are_dropped_from_a_match():
    out = decide(answer("MATCH", skills=["python", "rust", "llm"]), title="Staff Engineer", posting=POSTING)
    assert out.classification == "MATCH" and out.matched_skills == ["python", "llm"]


def test_the_contract_has_no_confidence_number():
    assert "confidence" not in DomainClassificationResult.model_fields
    with pytest.raises(ValueError):
        DomainClassificationResult(classification="MATCH", reason="x", confidence=0.99)


def test_the_posting_window_keeps_the_substance_and_is_stable():
    long = "Intro. " * 100 + "\nResponsibilities\n- Design firmware\n" + "filler " * 800
    window = dc.posting_window(long)
    assert len(window) <= dc.WINDOW_CHARS and "Responsibilities" in window and window.startswith("Intro.")
    assert dc.posting_window(long) == window and dc.posting_window("short") == "short"


# ───────────── the pipeline, end to end ─────────────

@pytest.fixture
def user(monkeypatch):
    with SessionLocal() as db:  # the queue is claimed across accounts: nothing left by an earlier run may answer here
        db.execute(delete(JobClassificationTask))
        db.commit()
    owner = make_user(SessionLocal)
    with SessionLocal() as db:
        db.info["user_id"] = owner
        db.add(CandidateProfile(user_id=owner, name="Asha Rao", headline="Lead AI Engineer", target_roles=["Lead AI Engineer"],
                                master_resume="Asha Rao\nLead AI Engineer\n",
                                search_preferences={"primary_skills": ["python", "llm"]}))
        db.commit()
    monkeypatch.setattr(auth, "authenticate", lambda request: auth.CurrentUser(owner, "dc@jobhunt.test", False))
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    monkeypatch.setattr(get_settings(), "jd_fetch_enabled", False)
    yield owner
    with SessionLocal() as db:
        ids = select(Job.id).where(Job.user_id == owner)
        for model in (JobClassificationTask, JobDescriptionFetchTask, JobDescriptionParseTask, JobRankingTask, WorkflowTransition):
            db.execute(delete(model).where(model.job_id.in_(ids)))
        db.execute(delete(Job).where(Job.user_id == owner))
        for model in (JobClassificationCache, JobFeedbackEvent, SearchRun, AppSetting, CandidateProfile):
            db.execute(delete(model).where(model.user_id == owner))
        db.commit()


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(get_settings(), "domain_classifier_mode", "on")


@pytest.fixture
def model(monkeypatch):
    """A stub for the gateway: answers from `model.answer`, counts its calls, may raise `model.error`."""
    stub = SimpleNamespace(answer=answer("MATCH", skills=["python"]), calls=0, error=None, prompts=[])

    def generate(self, prompt, call=None):
        stub.calls += 1
        stub.prompts.append(prompt)
        if stub.error is not None:
            raise stub.error
        return SimpleNamespace(validated=stub.answer, model="stub-fast", data={})

    monkeypatch.setattr(LLMClient, "generate_prompt", generate)
    return stub


def add(owner, title, description=POSTING, company="Globex", **extra):
    with SessionLocal() as db:
        db.info["user_id"] = owner
        row, _ = sources.upsert_job(db, source="test:classifier", source_ref=f"{title}-{company}-{description[:20]}-{extra}",
                                    title=title, company=company, url="", description=description, **extra)
        return row.id


def run_pass(owner=None):
    with SessionLocal() as db:
        claims = dc.claim(db, worker_id="test", limit=50)
    return [dc.process(SessionLocal, c) for c in claims]


def row_of(job_id):
    with SessionLocal() as db:
        return db.get(Job, job_id)


def tasks_of(job_id):
    with SessionLocal() as db:
        return list(db.scalars(select(JobClassificationTask).where(JobClassificationTask.job_id == job_id)
                               .order_by(JobClassificationTask.id)).all())


def spent(job_id):
    """Rows that would cost recovery, parsing or a ranking."""
    with SessionLocal() as db:
        return [m.__name__ for m in (JobDescriptionFetchTask, JobDescriptionParseTask, JobRankingTask)
                if db.scalar(select(m.id).where(m.job_id == job_id, m.status.in_(("PENDING", "PROCESSING"))).limit(1))]


def test_off_mode_is_the_old_filter_and_writes_nothing_new(user, model):
    hidden = add(user, "Marketing Manager")
    kept = add(user, "Backend Platform Engineer")
    assert row_of(hidden).domain_match == dm.NOT_MATCHING and row_of(kept).domain_match == dm.MATCH
    assert spent(kept) and not tasks_of(hidden) and not tasks_of(kept) and model.calls == 0


def test_shadow_records_both_verdicts_and_never_touches_a_job(user, model, monkeypatch):
    monkeypatch.setattr(get_settings(), "domain_classifier_mode", "shadow")
    monkeypatch.setattr(get_settings(), "domain_classifier_shadow_daily_limit", 5)
    asked = add(user, "Backend Platform Engineer, Reliability")
    assert row_of(asked).domain_match == dm.MATCH  # the old filter decided, as before
    before = spent(asked)
    model.answer = answer("NO_MATCH", "Run monthly payroll")
    assert run_pass() == ["no_match"] or model.calls == 1
    task = tasks_of(asked)[-1]
    assert task.mode == "SHADOW" and task.legacy_match is True and task.route == dm.ASK
    current = row_of(asked)
    assert current.domain_match == dm.MATCH and spent(asked) == before


def test_shadow_never_pays_without_a_budget(user, model, monkeypatch):
    monkeypatch.setattr(get_settings(), "domain_classifier_mode", "shadow")
    job_id = add(user, "Backend Platform Engineer, Reliability")
    assert run_pass() == [] and model.calls == 0
    task = tasks_of(job_id)[-1]
    assert (task.status, task.gate_code) == ("SKIPPED", "NO_SHADOW_BUDGET")


def test_a_rule_hides_at_once_and_records_why(user, on, model):
    job_id = add(user, "Founding Account Executive")
    current = row_of(job_id)
    assert current.domain_match == dm.NOT_MATCHING and current.state is S.SKIPPED and "account executive" in current.domain_reason
    assert not spent(job_id) and model.calls == 0
    assert tasks_of(job_id)[0].route == dm.HARD


def test_a_clear_match_goes_on_with_no_model_call(user, on, model):
    job_id = add(user, "Lead AI Engineer")
    assert row_of(job_id).domain_match == dm.MATCH and spent(job_id) and model.calls == 0
    assert tasks_of(job_id)[0].route == dm.POSITIVE


def test_a_pending_job_spends_nothing_until_its_verdict(user, on, model):
    job_id = add(user, "Payments Infrastructure Engineer")
    assert row_of(job_id).domain_match == dm.PENDING and not spent(job_id)
    with SessionLocal() as db:
        db.info["user_id"] = user
        current = db.get(Job, job_id)
        assert ranker.queue_job(db, current, policy=ranker.policy_for(db, user), actor="test") == "Waiting for the AI check"
        from app.services import jd_recovery, job_description

        assert job_description.queue_parse(db, current) is None and jd_recovery.enqueue(db, current) is None
        assert dm.screen(db, current) is False
        if db.get_bind().dialect.name == "postgresql":  # the ranking sweep claims with SKIP LOCKED
            ranker.sweep(db)
        job_description.sweep(db)
        jd_recovery.sweep(db)
        db.commit()
    assert not spent(job_id) and row_of(job_id).domain_match == dm.PENDING
    assert row_of(job_id).state is S.DISCOVERED


def test_a_match_goes_to_the_detailed_match(user, on, model):
    model.answer = answer("MATCH", skills=["python"], reason="Backend work.")
    job_id = add(user, "Payments Infrastructure Engineer")
    assert run_pass() == ["match"]
    current, task = row_of(job_id), tasks_of(job_id)[-1]
    assert current.domain_match == dm.MATCH and spent(job_id) and current.domain_reason.startswith(dc.AI_REASON)
    assert (task.status, task.classification, task.model_classification) == ("COMPLETED", "MATCH", "MATCH")


@pytest.mark.parametrize("title,skills,title_only,expected", [
    ("Lead AI Engineer", ["python", "llm"], False, dc.FORWARD),      # their title, and their skills in the job
    ("Lead AI Engineer", ["python"], False, dc.REVIEW_JOB),           # their title, one skill: could go either way
    ("Payments Lead", ["python", "llm", "rag"], False, dc.REVIEW_JOB),  # skills, but not a title of theirs
    ("Payments Lead", [], True, dc.FORWARD)])                         # nothing to judge yet: read again after recovery
def test_what_settles_an_uncertain(title, skills, title_only, expected):
    decision = dc.Decision("UNCERTAIN", "Unclear.", job_skills=skills)
    assert dc.settle(decision, title=title, skills=skills, rules=RULES, title_only=title_only) == expected


def test_an_ambiguous_job_waits_for_the_person_and_spends_nothing(user, on, model):
    model.answer = answer("UNCERTAIN", reason="Could be either.")
    job_id = add(user, "Payments Operations Lead", PAYROLL)
    assert run_pass() == ["review"]
    current = row_of(job_id)
    assert current.domain_match == dm.REVIEW and "Could be either" in current.domain_reason and not spent(job_id)
    with SessionLocal() as db:
        db.info["user_id"] = user
        row = db.get(Job, job_id)
        assert ranker.queue_job(db, row, policy=ranker.policy_for(db, user), actor="test") == "Waiting for your review"
        from app.services import jd_recovery, job_description

        assert job_description.queue_parse(db, row) is None and jd_recovery.enqueue(db, row) is None
        assert dm.screen(db, row) is False
        job_description.sweep(db)
        db.commit()
    assert not spent(job_id) and row_of(job_id).domain_match == dm.REVIEW


def test_the_review_section_lets_the_person_keep_or_remove(user, on, model):
    client = TestClient(web_app)
    model.answer = answer("UNCERTAIN", reason="Could be either.")
    wanted = add(user, "Payments Operations Lead", PAYROLL)
    unwanted = add(user, "Payments Operations Manager", PAYROLL)
    run_pass()
    out = client.get("/api/discovery/review").json()
    assert out["total"] == 2 and {j["id"] for j in out["jobs"]} == {wanted, unwanted}
    assert out["jobs"][0]["reason"] == "Could be either." and "your_skills_found" in out["jobs"][0]
    assert client.post("/api/discovery/review", json={"ids": [wanted], "matches": True}).json()["done"] == [wanted]
    assert client.post("/api/discovery/review", json={"ids": [unwanted], "matches": False}).json()["done"] == [unwanted]
    kept, removed = row_of(wanted), row_of(unwanted)
    assert kept.domain_match == dm.KEPT and spent(wanted)
    assert removed.domain_match == dm.NOT_MATCHING and removed.domain_reason == dc.PERSON_REASON and not spent(unwanted)
    assert client.get("/api/discovery/review").json()["total"] == 0
    again = client.post("/api/discovery/review", json={"ids": [wanted], "matches": True}).json()
    assert again["done"] == [] and again["skipped"]  # already decided
    with SessionLocal() as db:
        actions = {e.job_id: e.action for e in db.scalars(select(JobFeedbackEvent).where(JobFeedbackEvent.user_id == user))}
    assert actions == {wanted: "KEPT", unwanted: "DISMISSED"}


def test_the_review_list_shows_only_this_accounts_jobs(user, on, model):
    model.answer = answer("UNCERTAIN")
    add(user, "Payments Operations Lead", PAYROLL)
    run_pass()
    other = make_user(SessionLocal)
    with SessionLocal() as db:
        db.info["user_id"] = other
        assert dc.review_jobs(db, other)["total"] == 0
        assert dc.resolve_review(db, other, [1], True, actor="test")["done"] == []
        db.rollback()


@pytest.mark.parametrize("title,role", [
    ("Artificial Intelligence Engineer", "ai engineer"), ("AI Engineer", "artificial intelligence engineer"),
    ("Senior Artificial Intelligence Engineer", "ai engineer"), ("FDE - EMEA", "forward deployed engineer"),
    ("Forward Deployed Engineer", "fde"), ("Forward Deployment Engineer", "forward deployed engineer"),
    ("SDE 3", "senior software engineer"), ("SDE-III, Payments", "senior software engineer"),
    ("Senior Software Engineer", "sde 3"), ("SDE 4", "staff software engineer"), ("SDE IV", "staff software engineer"),
    ("Staff Software Engineer", "sde 4"), ("Sr. Software Engineer", "senior software engineer"),
    ("Software Development Engineer", "software engineer")])
def test_equivalent_titles_name_the_same_role(title, role):
    assert dm.names_role(title, role)


@pytest.mark.parametrize("title,role", [
    ("SDE 3", "staff software engineer"), ("SDE 2", "senior software engineer"), ("Sales Engineer", "ai engineer"),
    ("Retail Engineer", "ai engineer"), ("Fidelity Analyst", "forward deployed engineer")])
def test_different_roles_stay_different(title, role):
    assert not dm.names_role(title, role)


def test_an_equivalent_title_is_never_hidden_on_a_models_word():
    rules = Rules(**{**RULES.__dict__, "roles": ["sde 3", "ai engineer"], "skills": ["kafka"]})
    for title in ("Senior Software Engineer (Golang - Platform team)", "Artificial Intelligence Engineer"):
        out = dc.decide(answer("NO_MATCH", title), title=title, posting="Build services.", rules=rules)
        assert (out.classification, out.gate_code) == ("UNCERTAIN", "TITLE_PROTECTED"), title
        assert dc.settle(out, title=title, skills=out.job_skills, rules=rules, title_only=False) == dc.REVIEW_JOB


def test_a_search_for_one_name_accepts_the_other(user):
    with SessionLocal() as db:
        db.info["user_id"] = user
        run = SearchRun(user_id=user, external_run_id="test-alias", roles=["AI Engineer"], location="", search_params={})
        db.add(run)
        db.flush()
        same = SimpleNamespace(title="Artificial Intelligence Engineer", description="", location="", search_run_id=run.id)
        assert search_scope.verdict_for(db, same).match is False  # the old check hid it
        assert search_scope.hybrid(db, same) == (None, ())
        db.rollback()


def test_skills_come_from_every_resume_and_everyday_words_do_not_count(user, on):
    from app.models import Resume

    with SessionLocal() as db:
        db.info["user_id"] = user
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user))
        db.add(Resume(user_id=user, candidate_profile_id=profile.id, name="Other", version=1, raw_text="x", raw_text_hash="x", is_active=False,
                      structured_data={"skills": [{"name": "Kafka"}, {"name": "Go"}, {"name": "COBOL", "status": "REJECTED"}]}))
        db.commit()
        names = dm.resume_skills(db, user)
        assert "kafka" in names and "cobol" not in names
        rules = dm.rules_for(db, user)
        assert "kafka" in rules.skills and "python" in rules.skills
        assert dm.skills_found("Go to market with Kafka", rules.skills) == ["kafka"]
        db.execute(delete(Resume).where(Resume.user_id == user))
        db.commit()


def test_off_mode_keeps_the_narrow_skill_list(user):
    from app.models import Resume

    with SessionLocal() as db:
        db.info["user_id"] = user
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user))
        db.add(Resume(user_id=user, candidate_profile_id=profile.id, name="Other", version=1, raw_text="x", raw_text_hash="x", is_active=False,
                      structured_data={"skills": [{"name": "Kafka"}]}))
        db.commit()
        assert "kafka" not in dm.rules_for(db, user).skills
        db.execute(delete(Resume).where(Resume.user_id == user))
        db.commit()


def test_a_no_match_with_evidence_hides_and_keep_restores_it(user, on, model, monkeypatch):
    model.answer = answer("NO_MATCH", "Run monthly payroll", reason="This is payroll administration.")
    job_id = add(user, "Payments Operations Lead", PAYROLL)
    assert run_pass() == ["no_match"]
    current = row_of(job_id)
    assert current.domain_match == dm.NOT_MATCHING and current.state is S.SKIPPED and not spent(job_id)
    assert "payroll administration" in current.domain_reason and "Run monthly payroll" in current.domain_reason
    with SessionLocal() as db:
        db.info["user_id"] = user
        dm.keep(db, db.get(Job, job_id), actor="test")
        db.commit()
    current = row_of(job_id)
    assert current.domain_match == dm.KEPT and current.state is not S.SKIPPED and spent(job_id)


def test_a_no_match_without_evidence_never_hides(user, on, model):
    model.answer = answer("NO_MATCH", "this text is not there")
    job_id = add(user, "Payments Infrastructure Engineer")
    assert run_pass() == ["review"]  # the model said no and code doubts it: the person's to settle
    task = tasks_of(job_id)[-1]
    assert (task.model_classification, task.classification, task.gate_code) == \
        ("NO_MATCH", "UNCERTAIN", "EVIDENCE_NOT_IN_POSTING")
    assert row_of(job_id).domain_match == dm.REVIEW


def test_the_prompt_carries_no_personal_data_and_marks_the_posting_untrusted(user, on, model):
    add(user, "Payments Infrastructure Engineer")
    run_pass()
    text = model.prompts[0].user
    assert "Asha" not in text and "<untrusted_job_posting>" in text and "lead ai engineer" in text


def test_a_duplicate_posting_is_answered_from_the_cache_through_the_same_gate(user, on, model):
    model.answer = answer("NO_MATCH", "Run monthly payroll")
    first = add(user, "Payments Operations Lead", PAYROLL, company="Northwind")
    run_pass()
    assert model.calls == 1
    second = add(user, "Payments Operations Lead", PAYROLL + " ", company="Northwind")
    third = add(user, "Payments Operations Lead", PAYROLL + "  ", company="Northwind")
    if second != first:
        run_pass()
        assert model.calls == 1
        assert tasks_of(second)[-1].cache_hit is True and row_of(second).domain_match == dm.NOT_MATCHING
    assert third is not None
    with SessionLocal() as db:
        entry = db.scalar(select(JobClassificationCache).where(JobClassificationCache.user_id == user))
        assert entry.prompt_version == "v2" and "payroll" not in entry.cache_key.lower()


def test_the_cache_belongs_to_one_account(user, on, model):
    model.answer = answer("MATCH")
    add(user, "Payments Infrastructure Engineer")
    run_pass()
    other = make_user(SessionLocal)
    with SessionLocal() as db:
        db.info["user_id"] = other
        assert db.scalar(select(JobClassificationCache)) is None
        db.info["user_id"] = user
        key = db.scalar(select(JobClassificationCache)).cache_key
        assert dc._cached(db, other, key, dc.utcnow()) is None
        assert dc._cached(db, user, key, dc.utcnow()) is not None
        db.rollback()


def test_an_expired_answer_is_asked_again_and_purged(user, on, model):
    job_id = add(user, "Payments Infrastructure Engineer")
    run_pass()
    with SessionLocal() as db:
        db.info["user_id"] = user
        entry = db.scalar(select(JobClassificationCache))
        entry.expires_at = dc.utcnow() - timedelta(days=1)
        db.commit()
        assert dc.purge_cache(db) >= 1
    assert job_id


def test_a_failing_provider_retries_then_fails_open(user, on, model):
    model.error = LLMResponseError("the provider broke")
    job_id = add(user, "Payments Infrastructure Engineer")
    assert run_pass() == ["retry"]
    task = tasks_of(job_id)[-1]
    assert task.status == "PENDING" and task.next_attempt_at is not None and "provider broke" in task.last_error
    assert row_of(job_id).domain_match == dm.PENDING
    for _ in range(5):
        with SessionLocal() as db:
            pending = db.get(JobClassificationTask, task.id)
            pending.next_attempt_at = None
            db.commit()
        outcomes = run_pass()
        if outcomes == ["uncertain"]:
            break
    task = tasks_of(job_id)[-1]
    assert (task.status, task.classification, task.gate_code) == ("COMPLETED", "UNCERTAIN", dc.UNAVAILABLE)
    assert row_of(job_id).domain_match == dm.MATCH and spent(job_id)  # a failed check never hides a job


def test_no_model_configured_fails_open_at_once(user, on, model):
    model.error = LLMConfigError("no key")
    job_id = add(user, "Payments Infrastructure Engineer")
    assert run_pass() == ["uncertain"]
    assert tasks_of(job_id)[-1].gate_code == dc.UNAVAILABLE and row_of(job_id).domain_match == dm.MATCH


def test_a_request_held_for_a_rate_limit_keeps_its_attempt(user, on, model):
    model.error = LLMRateLimitError("wait", held_seconds=90)
    job_id = add(user, "Payments Infrastructure Engineer")
    assert run_pass() == ["retry"]
    task = tasks_of(job_id)[-1]
    assert task.attempt_count == 0 and task.status == "PENDING" and task.next_attempt_at > dc.utcnow()


def test_a_task_that_lost_its_lease_writes_nothing(user, on, model, monkeypatch):
    model.answer = answer("NO_MATCH", "Run monthly payroll")
    job_id = add(user, "Payments Operations Lead", PAYROLL)
    with SessionLocal() as db:
        claims = dc.claim(db, worker_id="test", limit=50)
    real = LLMClient.generate_prompt

    def killed_meanwhile(self, prompt, call=None):
        with SessionLocal() as other:
            workers.kill(other, user, "classification", [claims[0].task_id])
        return real(self, prompt, call)

    monkeypatch.setattr(LLMClient, "generate_prompt", killed_meanwhile)
    assert dc.process(SessionLocal, claims[0]) == "lost"
    assert row_of(job_id).domain_match == dm.PENDING and not spent(job_id)
    task = tasks_of(job_id)[-1]
    assert task.last_error == STOPPED_BY_USER and task.classification is None


def test_kill_stops_nothing_revives_it_and_retry_asks_again(user, on, model):
    job_id = add(user, "Payments Infrastructure Engineer")
    with SessionLocal() as db:
        claims = dc.claim(db, worker_id="test", limit=50)
        workers.kill(db, user, "classification", [claims[0].task_id])
    with SessionLocal() as db:
        dc.sweep(db)
    assert run_pass() == [] and row_of(job_id).domain_match == dm.PENDING and model.calls == 0
    with SessionLocal() as db:
        out = workers.retry(db, user, "classification", [claims[0].task_id])
        assert out
    assert run_pass() == ["match"] and row_of(job_id).domain_match == dm.MATCH


def test_a_crashed_worker_goes_back_in_the_queue(user, on, model):
    job_id = add(user, "Payments Infrastructure Engineer")
    with SessionLocal() as db:
        dc.claim(db, worker_id="crashed", limit=50, lease_seconds=1)
        assert dc.recover_expired(db, dc.utcnow() + timedelta(seconds=5)) == 1
    assert tasks_of(job_id)[-1].status == "PENDING"
    assert run_pass() == ["match"]


def test_a_job_kept_while_waiting_is_not_overruled(user, on, model):
    model.answer = answer("NO_MATCH", "Run monthly payroll")
    job_id = add(user, "Payments Operations Lead", PAYROLL)
    with SessionLocal() as db:
        db.info["user_id"] = user
        dm.keep(db, db.get(Job, job_id), actor="test")
        db.commit()
    assert run_pass() == ["skipped"] and row_of(job_id).domain_match == dm.KEPT and model.calls == 0
    assert tasks_of(job_id)[-1].status == "SKIPPED"


def test_a_title_only_uncertain_is_read_again_when_its_text_arrives(user, on, model):
    model.answer = answer("UNCERTAIN", reason="Only a title.")
    job_id = add(user, "Payments Infrastructure Engineer", "")
    run_pass()
    assert tasks_of(job_id)[-1].title_only is True
    with SessionLocal() as db:
        db.info["user_id"] = user
        current = db.get(Job, job_id)
        current.description = PAYROLL
        assert dc.recheck_recovered(db, current, dc.utcnow()) is True
        db.commit()
    assert row_of(job_id).domain_match == dm.PENDING
    model.answer = answer("NO_MATCH", "Run monthly payroll")
    assert run_pass() == ["no_match"] and row_of(job_id).domain_match == dm.NOT_MATCHING


def test_recheck_runs_only_when_asked_and_leaves_decided_jobs_alone(user, model, monkeypatch):
    learned = {"term": "payments", "kind": "title", "status": "active", "evidence": 9, "positive": 0, "examples": []}
    with SessionLocal() as db:
        db.info["user_id"] = user
        db.add(AppSetting(user_id=user, preferences={"domain": {"learned": [learned]}}))
        db.commit()
    wrongly = add(user, "Payments Infrastructure Engineer")
    placed = add(user, "Payments Backend Engineer", company="Elsewhere")
    assert row_of(wrongly).domain_match == dm.NOT_MATCHING  # the old filter, on a one-word learned rule
    with SessionLocal() as db:
        db.info["user_id"] = user
        row = db.get(Job, placed)
        row.domain_reason = f"{search_scope.LOCATION_REASON}: Pune (searched Berlin)"
        db.commit()
    client = TestClient(web_app)
    assert client.post("/api/jobs/domain/recheck").status_code == 409  # off: nothing queued
    monkeypatch.setattr(get_settings(), "domain_classifier_mode", "on")
    assert run_pass() == [] and row_of(wrongly).domain_match == dm.NOT_MATCHING  # turning it on revives nothing
    out = client.post("/api/jobs/domain/recheck").json()
    assert out["queued"] == 1 and row_of(wrongly).domain_match == dm.PENDING
    assert row_of(placed).domain_match == dm.NOT_MATCHING
    assert run_pass() == ["match"]
    current = row_of(wrongly)
    assert current.domain_match == dm.MATCH and current.state is not S.SKIPPED and spent(wrongly)


# ───────────── cleaned learning ─────────────

def event(job_id, action, title, company="Acme", when=None):
    return JobFeedbackEvent(job_id=job_id, action=action, title=title, company=company, terms=feedback_learning.title_terms(title),
                            created_at=when or dc.utcnow())


def test_a_day_of_mass_deletes_and_a_liked_job_teach_nothing():
    day = dc.utcnow()
    cleanup = [event(i, "DELETED", f"Data Role {i}", when=day) for i in range(25)]
    other_day = [event(100, "DELETED", "Solo Role", when=day - timedelta(days=3))]
    changed_mind = [event(200, "SHORTLISTED", "Staff Engineer"), event(200, "DELETED", "Staff Engineer", when=day - timedelta(days=5)),
                    event(201, "REJECTED", "Staff Engineer", when=day - timedelta(days=5)), event(201, "APPLIED", "Staff Engineer")]
    plain = [event(300, "REJECTED", "Tax Counsel")]
    kept = feedback_learning.counted_events(cleanup + other_day + changed_mind + plain)
    assert {e.job_id for e in kept} == {100, 200, 201, 300}
    assert all(e.action not in ("DELETED", "REJECTED") for e in kept if e.job_id in (200, 201))


def test_mass_deletes_learn_no_rule_but_explicit_dismissals_do(user, on):
    now = dc.utcnow()
    with SessionLocal() as db:
        db.info["user_id"] = user
        for i in range(25):
            db.add(JobFeedbackEvent(user_id=user, job_id=1000 + i, action="DELETED", title=f"Data Thing {i}", company="Deleted Co",
                                    terms=["data"], created_at=now))
        for i in range(3):
            db.add(JobFeedbackEvent(user_id=user, job_id=2000 + i, action="REJECTED", title=f"Virtual RM {i}", company="HDFC Bank",
                                    terms=["virtual"], created_at=now))
        db.flush()
        learned = feedback_learning.learn(db, user)
        db.commit()
    terms = {(e["kind"], e["term"]) for e in learned}
    assert ("title", "data") not in terms and ("company", "deleted co") not in terms
    assert ("company", "hdfc bank") in terms and next(e for e in learned if e["term"] == "hdfc bank")["clean"] is True


def test_a_bulk_delete_is_recorded_as_a_cleanup(user, on, model):
    client = TestClient(web_app)
    ids = [add(user, f"Payments Infrastructure Engineer {i}", company=f"C{i}") for i in range(4)]
    assert client.post("/api/jobs/bulk-delete", json={"ids": ids}).status_code == 200
    with SessionLocal() as db:
        actions = set(db.scalars(select(JobFeedbackEvent.action).where(JobFeedbackEvent.user_id == user)).all())
    assert actions == {"BULK_DELETED"}


# ───────────── registries and reports ─────────────

def test_the_worker_is_registered_everywhere_a_worker_must_be():
    from app.services import ai_workers, orchestration_topology, worker_settings

    assert ai_workers.WORKERS["DOMAIN_CLASSIFIER"].default_tier == ai_workers.FAST
    assert ai_workers.worker_for_prompt("domain_classifier") == "DOMAIN_CLASSIFIER"
    assert workers.KINDS["classification"].node_id == "domain_classifier"
    assert "classification" in worker_settings.FIELDS
    assert orchestration_topology.node_for_worker("DOMAIN_CLASSIFIER").id == "domain_classifier"
    assert not orchestration_topology.validate()


def test_metrics_read_the_tasks_and_never_invent_a_cost(user, on, model):
    add(user, "Payments Infrastructure Engineer")
    add(user, "Founding Account Executive")
    run_pass()
    with SessionLocal() as db:
        db.info["user_id"] = user
        out = dc.metrics(db, user, dc.utcnow() - timedelta(hours=1), dc.utcnow() + timedelta(hours=1))
    assert out["jobs"] == 2 and out["routes"] == {dm.ASK: 1, dm.HARD: 1} and out["ask_rate"] == 0.5
    assert out["classifications"] == {"MATCH": 1} and out["cost"] is None


def test_the_comparison_lists_only_shadow_disagreements(user, model, monkeypatch):
    monkeypatch.setattr(get_settings(), "domain_classifier_mode", "shadow")
    monkeypatch.setattr(get_settings(), "domain_classifier_shadow_daily_limit", 5)
    learned = {"term": "payments", "kind": "title", "status": "active", "evidence": 9, "positive": 0, "examples": []}
    with SessionLocal() as db:
        db.info["user_id"] = user
        db.add(AppSetting(user_id=user, preferences={"domain": {"learned": [learned]}}))
        db.commit()
    job_id = add(user, "Payments Infrastructure Engineer")
    assert row_of(job_id).domain_match == dm.NOT_MATCHING
    run_pass()
    with SessionLocal() as db:
        db.info["user_id"] = user
        out = dc.compare(db, user)
    assert out["old_hides_new_keeps"] == 1 and out["rescued"][0]["job_id"] == job_id
    assert row_of(job_id).domain_match == dm.NOT_MATCHING  # still only recorded


def test_the_evaluation_suite_passes_its_gates():
    from app.services.agent_eval.datasets import suite_dataset
    from app.services.agent_eval.gates import evaluate
    from app.services.agent_eval.suites import SUITES

    result = SUITES["domain_classifier"](suite_dataset("domain_classifier"), {})
    assert evaluate(result)["status"] == "PASS", result.failed_cases
    assert result.metrics["hybrid_false_negative_rate"] < result.metrics["legacy_false_negative_rate"]
