"""Structured job descriptions: parsing, requirement evidence, corrections, async tasks and job context."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import Job, JobDescriptionParseTask, JobRequirementEvidence
from app.services import job_context as jc
from app.services import job_description as jd
from app.services.lifecycle import STOPPED_BY_USER
from app.settings import get_settings
from tests.conftest import make_user

client = TestClient(app)

JD_HTML = """<p>About Globex</p>
<p>We are a fast-growing data company. Our mission is to make fleets safer.</p>
<h3>Responsibilities</h3>
<ul><li>You will own the Kafka pipelines behind our analytics platform.</li>
<li>Partner with product to deliver real-time reporting.</li></ul>
<h3>Requirements</h3>
<ul><li>Strong experience with Python is required.</li>
<li>Minimum of 8 years building distributed systems.</li>
<li>Experience leading and mentoring engineers.</li>
<li>Bachelor&#39;s degree in Computer Science.</li>
<li>You must be based in Singapore and able to work onsite twice a week.</li></ul>
<h3>Nice to have</h3>
<ul><li>Familiarity with Kubernetes is a plus.</li>
<li>Exposure to Apache Flink would be great.</li></ul>
<p>Salary: $180,000 - $220,000 per year.</p>"""


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


@pytest.fixture
def user(monkeypatch):
    user_id = make_user(SessionLocal)
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, f"{user_id}@jobhunt.test", False))
    return user_id


def session(user_id):
    db = SessionLocal()
    db.info["user_id"] = user_id
    return db


def add_job(user_id, description=JD_HTML, **fields):
    with session(user_id) as db:
        job = Job(source="manual", source_ref=f"jd-{uuid.uuid4().hex[:8]}", title="Data Platform Lead",
                  company="Globex", location="Singapore", description=description, status="discovered", **fields)
        db.add(job)
        db.commit()
        return job.id


def parse(user_id, job_id):
    with session(user_id) as db:
        job = db.get(Job, job_id)
        jd.parse_now(db, job)
        db.commit()
        return job.structured_description


def structured(job_id):
    return client.get(f"/api/jobs/{job_id}/structured-description").json()


def requirements(job_id, **params):
    return client.get(f"/api/jobs/{job_id}/requirements", params=params).json()["items"]


# ───────────── normalization and raw preservation ─────────────

def test_the_raw_description_is_never_rewritten(user):
    job_id = add_job(user)
    parse(user, job_id)
    with session(user) as db:
        job = db.get(Job, job_id)
    assert job.description == JD_HTML, "the employer's text is kept exactly as it arrived"
    assert "<li>" not in job.normalized_description and "Bachelor's degree" in job.normalized_description
    assert job.normalized_description.startswith("About Globex")


def test_normalization_keeps_wording_and_finds_sections():
    text = jd.normalize_description(JD_HTML)
    assert "- Strong experience with Python is required." in text
    assert [heading for heading, _ in jd.sections_of(text)] == ["About Globex", "Responsibilities", "Requirements",
                                                                "Nice to have"]


# ───────────── parsing ─────────────

def test_required_and_preferred_requirements_are_kept_apart(user):
    job_id = add_job(user)
    parse(user, job_id)
    data = structured(job_id)
    assert data["status"] == "PARSED" and data["version"] == jd.PARSER_VERSION
    levels = {item["value"]: item["level"] for item in data["requirements"]}
    assert levels["Strong experience with Python is required."] == "REQUIRED"
    assert levels["Familiarity with Kubernetes is a plus."] == "PREFERRED"
    assert levels["Exposure to Apache Flink would be great."] == "PREFERRED"
    # Marketing copy asks nothing of the candidate.
    assert levels["We are a fast-growing data company."] == "INFORMATIONAL"
    assert data["counts"]["REQUIRED"] >= 4 and data["counts"]["PREFERRED"] == 2
    structured_description = data["structured_description"]
    assert any("Kubernetes" in skill for skill in structured_description["preferred_skills"])
    assert all("Kubernetes" not in skill for skill in structured_description["required_skills"])


def test_responsibilities_experience_leadership_and_education_are_extracted(user):
    job_id = add_job(user)
    data = parse(user, job_id)
    assert any("Kafka pipelines" in item for item in data["responsibilities"])
    assert data["experience_requirements"]["minimum_years"] == 8
    assert data["experience_requirements"]["maximum_years"] is None
    assert data["experience_requirements"]["leadership_required"] is True
    assert any("mentoring" in item for item in data["leadership_expectations"])
    assert any("Computer Science" in item for item in data["education_requirements"])
    assert any("Singapore" in item for item in data["hard_requirements"])


def test_visa_and_sponsorship_stay_unknown_when_the_posting_says_nothing(user):
    data = parse(user, add_job(user))
    assert data["location_requirements"]["visa_sponsorship"] is None
    assert data["location_requirements"]["work_authorization_required"] is None


def test_compensation_comes_from_the_posting_and_the_board_never_overwrites_it(user):
    data = parse(user, add_job(user))
    assert data["compensation"] == {"min": 180000.0, "max": 220000.0, "currency": "USD", "period": "year",
                                    "source_text": "Salary: $180,000 - $220,000 per year.",
                                    "source": "job_description"}
    # A posting without a salary falls back to what the board reported, marked as such.
    board = add_job(user, description="Requirements\nStrong Python experience is required.",
                    salary_min=90000, salary_max=120000, salary_currency="SGD", salary_interval="year")
    data = parse(user, board)
    assert data["compensation"]["min"] == 90000 and data["compensation"]["source"] == "job_board"


def test_every_requirement_keeps_its_source_text_and_confidence(user):
    job_id = add_job(user)
    parse(user, job_id)
    for item in requirements(job_id):
        assert item["source_text"] and 0 <= item["confidence"] <= 1
    python = next(item for item in requirements(job_id) if "Python" in item["value"])
    assert python["source_text"] == "Strong experience with Python is required."
    assert python["source_section"] == "Requirements" and python["level"] == "REQUIRED"


def test_explicit_and_derived_are_stored_apart(user):
    job_id = add_job(user)
    parse(user, job_id)
    with session(user) as db:
        job = db.get(Job, job_id)
        db.add(JobRequirementEvidence(user_id=user, job_id=job.id, item_id="der_001", requirement_type="DOMAIN",
                                      normalized_value="Distributed systems interview", requirement_level="UNKNOWN",
                                      source_text="", confidence=0.4, is_explicit=False, is_derived=True))
        db.commit()
    assert all(not item["derived"] for item in requirements(job_id)), "derived signals are excluded by default"
    with_derived = requirements(job_id, include_derived=True)
    assert any(item["derived"] for item in with_derived)
    insights = client.get(f"/api/jobs/{job_id}/interview-insights").json()
    assert "not stated by the employer" in insights["derived_signals"]["label"]
    assert any(item["value"] == "Distributed systems interview"
               for item in insights["derived_signals"]["derived_requirements"])


def test_parser_output_is_validated_against_the_schema(user, monkeypatch):
    from pydantic import ValidationError

    from app.services.llm import LLMClient

    monkeypatch.setattr(LLMClient, "generate_prompt_json",
                        lambda self, prompt, call=None: {"structured_description": {"experience_requirements":
                                                                                    {"minimum_years": "many"}},
                                                         "requirements": "not a list"})
    job_id = add_job(user)
    with session(user) as db, pytest.raises(ValidationError):
        jd.parse_now(db, db.get(Job, job_id))
    # A long posting is trimmed to the schema's limits instead of failing validation.
    monkeypatch.setattr(LLMClient, "generate_prompt_json",
                        lambda self, prompt, call=None: {"structured_description":
                                                         {"required_skills": [f"Skill {i}" for i in range(200)]},
                                                         "requirements": []})
    with session(user) as db:
        parsed = jd.parse_now(db, db.get(Job, job_id))
        assert len(parsed.structured_description.required_skills) == 60


# ───────────── the async task ─────────────

def claim_for(job_id):
    """Claim this job's task. The test database holds other accounts' tasks, so the batch is filtered to ours."""
    with SessionLocal() as db:
        mine = db.execute(JobDescriptionParseTask.__table__.update()
                          .where(JobDescriptionParseTask.job_id == job_id).values(next_attempt_at=None))
        db.commit()
        assert mine.rowcount, "nothing queued for this job"
        task_id = db.scalar(select(JobDescriptionParseTask.id).where(JobDescriptionParseTask.job_id == job_id))
        return next((claim for claim in jd.claim(db, worker_id="test-worker", limit=200)
                     if claim.task_id == task_id), None)


def test_a_new_job_is_queued_once_and_parsed_by_the_worker(user):
    from app.services.sources import upsert_job

    source_ref = uuid.uuid4().hex
    with session(user) as db:
        job, created = upsert_job(db, source="test", source_ref=source_ref, title="Data Platform Lead",
                                  company="Globex", description=JD_HTML)
        job_id = job.id
        assert created
        # Upserting the same posting again never queues a second task.
        upsert_job(db, source="test", source_ref=source_ref, title="Data Platform Lead", company="Globex",
                   description=JD_HTML)
        tasks = db.scalars(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job_id)).all()
        assert len(tasks) == 1 and tasks[0].status == "PENDING"

    claim = claim_for(job_id)
    assert claim is not None
    assert jd.process(SessionLocal, claim) == "parsed"
    with session(user) as db:
        job = db.get(Job, job_id)
        task = db.scalar(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job_id))
    assert job.structured_description_status == "PARSED" and job.structured_description
    assert task.status == "COMPLETED" and task.completed_at and task.last_error is None
    # Nothing is queued again while the description is unchanged.
    with session(user) as db:
        assert jd.queue_parse(db, job) is None or db.scalar(
            select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job_id,
                                                  JobDescriptionParseTask.status == "PENDING")) is None


def test_a_failing_parse_retries_and_then_gives_up_without_blocking_the_job(user, monkeypatch):
    from app.services.llm import LLMClient, LLMTimeoutError

    monkeypatch.setattr(LLMClient, "generate_prompt_json",
                        lambda self, prompt, call=None: (_ for _ in ()).throw(LLMTimeoutError("provider timed out")))
    job_id = add_job(user)
    with session(user) as db:
        jd.queue_parse(db, db.get(Job, job_id))
        db.commit()
    outcomes = []
    for _ in range(get_settings().jd_parse_max_attempts):
        outcomes.append(jd.process(SessionLocal, claim_for(job_id)))
    assert outcomes == ["retrying"] * (len(outcomes) - 1) + ["failed"]
    with session(user) as db:
        job, task = db.get(Job, job_id), db.scalar(select(JobDescriptionParseTask)
                                                   .where(JobDescriptionParseTask.job_id == job_id))
        assert task.status == "FAILED" and "timed out" in task.last_error
        assert job.structured_description_status == "NEEDS_REVIEW"
        # The pipeline is never held up by a posting that cannot be parsed.
        assert jd.waiting_reason(db, job) is None


def test_ranking_waits_only_while_a_parse_is_still_possible(user):
    job_id = add_job(user)
    with session(user) as db:
        job = db.get(Job, job_id)
        # Nothing queued yet: ranking never reads an unparsed posting, so it waits and queues the parse itself.
        assert jd.waiting_reason(db, job) == "The job description is waiting to be parsed"
        assert jd.hold_for_parse(db, job) == "The job description is still being parsed"
        assert jd.hold_for_parse(db, job) == "The job description is still being parsed"
        db.commit()
        tasks = db.scalars(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job_id)).all()
        assert [(t.status, t.reason) for t in tasks] == [("PENDING", "ranking")], "queued once"
        assert jd.waiting_reason(db, job) == "The job description is still being parsed"
        jd.parse_now(db, job)
        db.commit()
        assert jd.waiting_reason(db, job) is None
        assert jd.hold_for_parse(db, job) is None


def test_ranking_waits_for_a_parse_of_the_current_wording(user):
    """A parse that finished for an older wording does not count: the new text is parsed before ranking."""
    job_id = add_job(user)
    with session(user) as db:
        job = db.get(Job, job_id)
        jd.queue_parse(db, job)
        db.commit()
        db.execute(JobDescriptionParseTask.__table__.update().where(JobDescriptionParseTask.job_id == job_id)
                   .values(status="COMPLETED"))
        job.description = JD_HTML + "<p>Rust is required.</p>"
        db.commit()
        assert jd.hold_for_parse(db, job) == "The job description is still being parsed"
        db.commit()
        assert db.scalar(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job_id,
                                                               JobDescriptionParseTask.status == "PENDING"))


@pytest.mark.parametrize("error", ["TimeoutError: down", STOPPED_BY_USER])
def test_a_failed_or_stopped_parse_of_this_wording_never_holds_ranking_back(user, error):
    job_id = add_job(user)
    with session(user) as db:
        job = db.get(Job, job_id)
        jd.queue_parse(db, job)
        db.commit()
        db.execute(JobDescriptionParseTask.__table__.update().where(JobDescriptionParseTask.job_id == job_id)
                   .values(status="FAILED", last_error=error))
        db.commit()
        assert jd.hold_for_parse(db, job) is None
        db.commit()
        assert len(db.scalars(select(JobDescriptionParseTask)
                              .where(JobDescriptionParseTask.job_id == job_id)).all()) == 1, "never queued again"


def test_a_parse_stopped_by_a_person_is_never_queued_again_even_for_newer_wording(user):
    job_id = add_job(user)
    with session(user) as db:
        job = db.get(Job, job_id)
        jd.queue_parse(db, job)
        db.commit()
        db.execute(JobDescriptionParseTask.__table__.update().where(JobDescriptionParseTask.job_id == job_id)
                   .values(status="FAILED", last_error=STOPPED_BY_USER, description_hash="an older wording"))
        db.commit()
        assert jd.hold_for_parse(db, job) is None
        db.commit()
        assert db.scalar(select(func.count()).select_from(JobDescriptionParseTask)
                         .where(JobDescriptionParseTask.job_id == job_id)) == 1


def test_a_failure_counts_for_the_wording_the_attempt_read_not_the_one_queued(user, monkeypatch):
    """The text changed while its parse was queued: the task parsed the new text, so its failure covers it."""
    from app.services.llm import LLMClient, LLMTimeoutError

    job_id = add_job(user)
    with session(user) as db:
        job = db.get(Job, job_id)
        jd.queue_parse(db, job)
        db.commit()
        job.description = JD_HTML + "<p>Rust is required.</p>"  # the active task is reused for the new text
        assert jd.queue_parse(db, job, reason="description_changed") is not None
        db.commit()
    monkeypatch.setattr(LLMClient, "generate_prompt_json",
                        lambda self, prompt, call=None: (_ for _ in ()).throw(LLMTimeoutError("down")))
    for _ in range(get_settings().jd_parse_max_attempts):
        jd.process(SessionLocal, claim_for(job_id))
    with session(user) as db:
        job = db.get(Job, job_id)
        task, = db.scalars(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job_id)).all()
        assert task.status == "FAILED" and task.description_hash == jd.description_hash(job.description)
        assert jd.hold_for_parse(db, job) is None, "its attempts are used up: no fresh round behind its back"


def test_ranking_leaves_a_stub_being_recovered_to_recovery(user):
    """Discovery chose recovery over parsing an unusable stub: ranking waits without queueing a parse of it."""
    from app.models import JobDescriptionFetchTask

    job_id = add_job(user)
    with session(user) as db:
        job = db.get(Job, job_id)
        db.add(JobDescriptionFetchTask(user_id=user, job_id=job_id, status="PENDING", reason="auto",
                                       attempt_count=0, created_at=jd.utcnow()))
        assert jd.hold_for_parse(db, job) == jd.PARSE_AFTER_RECOVERY
        db.commit()
        assert jd.hold_for_parse(db, job) == jd.PARSE_AFTER_RECOVERY
        db.commit()
        assert db.scalar(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job_id)) is None


def test_queueing_a_parse_for_ranking_wakes_the_parser_once_it_is_committed(user):
    from app import scheduler_hooks

    job_ids = [add_job(user), add_job(user)]
    woken = []
    scheduler_hooks.register("jd_parsing", lambda: woken.append("jd_parsing"))
    try:
        with session(user) as db:
            for job_id in job_ids:
                jd.hold_for_parse(db, db.get(Job, job_id))
            assert woken == [], "not before the task is committed"
            db.commit()
            assert woken == ["jd_parsing"], "once per transaction"
            jd.hold_for_parse(db, db.get(Job, job_ids[0]))  # already queued: nothing new, no nudge
            db.commit()
            assert woken == ["jd_parsing"]
    finally:
        scheduler_hooks.unregister("jd_parsing")


def test_existing_jobs_are_picked_up_in_batches(user):
    """The backlog is queued a batch at a time, once per job."""
    ids = [add_job(user) for _ in range(3)]
    with SessionLocal() as db:
        first = jd.sweep(db, limit=2)["queued"]
        # The sweep reads every account's backlog (an unscoped session), so earlier tests' jobs share it: sweep it all.
        second = jd.sweep(db, limit=10_000)["queued"]
        assert first == 2 and second >= 1
        counts = {job_id: db.scalar(select(JobDescriptionParseTask.id).where(
            JobDescriptionParseTask.job_id == job_id)) for job_id in ids}
        assert all(task is not None for task in counts.values()), "every backlog job is queued"
        tasks = db.scalars(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id.in_(ids))).all()
        assert len(tasks) == len(ids), "and only once each"
        assert jd.sweep(db, limit=10_000)["queued"] == 0


# ───────────── manual correction, re-parse and history ─────────────

def correct(job_id, mutate):
    """Edit the current version, as the UI does: the save names the version it started from."""
    current = structured(job_id)
    data = current["structured_description"]
    mutate(data)
    return client.put(f"/api/jobs/{job_id}/structured-description",
                      json={"structured_description": data, "base_hash": current["structured_hash"]})


def test_manual_corrections_are_recorded_and_survive_a_reparse(user):
    job_id = add_job(user)
    parse(user, job_id)

    out = correct(job_id, lambda data: data["job_identity"].update(seniority="Staff")).json()
    assert out["status"] == "MANUALLY_CORRECTED"
    assert out["structured_description"]["job_identity"]["seniority"] == "Staff"
    history = client.get(f"/api/jobs/{job_id}/structured-description/history").json()["items"]
    assert history[0]["field_path"] == "job_identity.seniority" and history[0]["change_source"] == "USER"
    assert history[0]["new_value"] == "Staff"

    # A re-parse of a corrected job proposes changes instead of overwriting them.
    reparsed = client.post(f"/api/jobs/{job_id}/structured-description/reparse").json()
    assert reparsed["applied"] is False
    assert reparsed["structured_description"]["job_identity"]["seniority"] == "Staff"
    change = next(c for c in reparsed["changes"] if c["path"] == "job_identity.seniority")
    assert change["current"] == "Staff" and change["corrected"] is True

    kept = client.post(f"/api/jobs/{job_id}/structured-description/reparse/resolve", json={"decisions": {}}).json()
    assert kept["structured_description"]["job_identity"]["seniority"] == "Staff" and kept["accepted"] == 0
    assert kept["candidate"] is None

    client.post(f"/api/jobs/{job_id}/structured-description/reparse")
    change = next(c for c in structured(job_id)["candidate"]["changes"] if c["path"] == "job_identity.seniority")
    used = client.post(f"/api/jobs/{job_id}/structured-description/reparse/resolve",
                       json={"decisions": {change["id"]: "use_new"}}).json()
    assert used["structured_description"]["job_identity"]["seniority"] != "Staff" and used["accepted"] == 1
    sources = {item["change_source"] for item in
               client.get(f"/api/jobs/{job_id}/structured-description/history").json()["items"]}
    assert {"PARSER", "USER"} <= sources


def test_a_reparse_of_an_uncorrected_job_simply_replaces_it(user):
    job_id = add_job(user)
    parse(user, job_id)
    out = client.post(f"/api/jobs/{job_id}/structured-description/reparse").json()
    assert out["applied"] is True and out["status"] == "PARSED" and out["candidate"] is None


def test_structured_job_data_belongs_to_its_owner(user, monkeypatch):
    job_id = add_job(user)
    parse(user, job_id)
    other = make_user(SessionLocal)
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(other, "other@jobhunt.test", False))
    assert client.get(f"/api/jobs/{job_id}/structured-description").status_code == 404
    assert client.post(f"/api/jobs/{job_id}/structured-description/reparse").status_code == 404


# ───────────── job context builders ─────────────

def contexts(user_id, job_id):
    with session(user_id) as db:
        job = db.get(Job, job_id)
        return {"ranking": jc.build_ranking_job_context(db, job),
                "tailoring": jc.build_tailoring_job_context(db, job),
                "review": jc.build_review_job_context(db, job),
                "insights": jc.build_interview_insights_job_context(db, job)}


def test_context_builders_select_only_what_each_task_needs(user):
    job_id = add_job(user)
    parse(user, job_id)
    built = contexts(user, job_id)

    ranking = built["ranking"]
    assert ranking.structured and ranking.identity["title"] == "Data Platform Lead"
    assert [item["level"] for item in ranking.required] == ["REQUIRED"] * len(ranking.required)
    assert [item["level"] for item in ranking.preferred] == ["PREFERRED"] * len(ranking.preferred)
    assert ranking.raw_description == "", "a parsed posting is never sent as raw text"
    assert ranking.source_evidence and all(item["source_text"] for item in ranking.source_evidence)

    tailoring = built["tailoring"]
    assert len(tailoring.required) <= jc.TAILOR_REQUIREMENTS and not tailoring.source_evidence

    review = built["review"]
    assert all(item.get("source_text") is not None for item in review.source_evidence)

    insights = built["insights"]
    assert insights.derived_signals["label"] == jc.DERIVED_LABEL


def test_an_unparsed_posting_still_reaches_the_task_as_raw_text(user):
    job_id = add_job(user)
    ranking = contexts(user, job_id)["ranking"]
    assert not ranking.structured and "Kafka pipelines" in ranking.raw_description


def test_candidate_matches_and_gaps_are_computed_from_verified_facts(user):
    job_id = add_job(user)
    parse(user, job_id)
    requirement_list = [{"id": "req_1", "value": "Strong experience with Python is required.", "level": "REQUIRED"},
                        {"id": "req_2", "value": "Familiarity with Rust is a plus.", "level": "PREFERRED"}]
    matches, gaps = jc.candidate_match(requirement_list, "Built Python services and Kafka pipelines at scale")
    assert [item["id"] for item in matches] == ["req_1"] and [item["id"] for item in gaps] == ["req_2"]


def test_job_context_reduction_is_measured(user):
    job_id = add_job(user, description=JD_HTML + "\n" + "Extra paragraph about our benefits. " * 200)
    parse(user, job_id)
    stats = contexts(user, job_id)["tailoring"].stats
    assert stats.raw_job_tokens > stats.selected_job_context_tokens > 0
    assert stats.job_context_reduction_percent > 50


def test_the_parser_prompt_lives_in_the_prompts_package():
    from pathlib import Path

    from app.prompts import PROMPT_VERSIONS
    from app.prompts.job_description_parser import (
        JOB_DESCRIPTION_PARSER_PROMPT_VERSION,
        build_job_description_parser_prompt,
    )

    root = Path(__file__).resolve().parents[1]
    assert (root / "app" / "prompts" / "job_description_parser.py").is_file()
    assert PROMPT_VERSIONS["job_description_parser"] == JOB_DESCRIPTION_PARSER_PROMPT_VERSION
    source = (root / "app" / "services" / "job_description.py").read_text()
    assert "You convert one job posting" not in source, "the prompt text lives in app/prompts, not in the service"

    class FakeJob:
        id, title, company, location = 1, "Data Platform Lead", "Globex", "Singapore"
        remote_type = job_type = seniority = experience = None
        salary_min = salary_max = salary_currency = salary_interval = None

    prompt = build_job_description_parser_prompt(FakeJob(), "Strong Python experience is required.", ["Requirements"])
    for rule in ("REQUIRED", "PREFERRED", "source_text", "is_derived", "Never guess salary", "derived_signals"):
        assert rule in prompt.system, rule
    assert "<untrusted_job_description>" in prompt.user and "Data Platform Lead" in prompt.user


# ───────────── what the prompts receive ─────────────

def test_prompts_get_requirements_instead_of_the_whole_advert(user):
    import io

    from app.prompts import build_ranking_prompt, build_review_prompt, build_tailoring_prompt
    from app.services import context_builder as cb
    from app.services.user_settings import profile_for

    client.post("/api/profile/resume", files={"file": ("r.txt", io.BytesIO(
        b"# Jane Doe\n\n## Skills\n- Languages: Python\n\n## Experience\n"
        b"### Lead Engineer | Zendrive | Remote | Jun 2024 - Oct 2025\n- Ran Kafka pipelines in production\n"),
        "text/plain")})
    job_id = add_job(user)
    parse(user, job_id)
    with session(user) as db:
        profile, job = profile_for(db), db.get(Job, job_id)
        ranking = cb.build_ranking_context(db, profile, job)
        tailoring_context = cb.build_tailoring_context(db, profile, job)
        review = cb.build_review_context(db, profile, job)
        db.commit()
        prompts = {"ranking": build_ranking_prompt(candidate_profile=profile, job=job, context=ranking),
                   "tailoring": build_tailoring_prompt(candidate_profile=profile, job=job, context=tailoring_context),
                   "review": build_review_prompt(candidate_profile=profile, job=job, context=review,
                                                 tailored_resume="# Jane Doe")}
    for name, prompt in prompts.items():
        assert "JOB REQUIREMENTS:" in prompt.user, name
        assert "<untrusted_job_description>" not in prompt.user, f"{name} no longer repeats the whole advert"
        assert "Strong experience with Python is required." in prompt.user, name
        assert "REQUIRED" in prompt.system and "PREFERRED" in prompt.system, name
    # The reviewer can quote the employer; ranking sees the hard requirements it may call fatal.
    assert "source_text" in prompts["review"].user
    assert ranking.job_context["candidate_matches"] or ranking.job_context["candidate_gaps"]
    # Both sides of context selection travel with the call.
    assert set(ranking.call_stats()) == {"full_available_context_tokens", "selected_context_tokens",
                                         "context_reduction_percent", "raw_job_tokens",
                                         "selected_job_context_tokens", "job_context_reduction_percent",
                                         "context_builder_version"}
    # Match analysis is the Ranker's: only its context computes candidate matches, the Tailor's does not.
    assert not tailoring_context.job_context.get("candidate_matches")
    assert "term_overlap" in prompts["ranking"].user and "term_overlap" not in prompts["tailoring"].user


def test_an_unparsed_posting_still_sends_the_description(user):
    from app.prompts import build_ranking_prompt
    from app.services import context_builder as cb
    from app.services.user_settings import profile_for

    job_id = add_job(user)
    with session(user) as db:
        profile, job = profile_for(db), db.get(Job, job_id)
        context = cb.build_ranking_context(db, profile, job)
        db.commit()
        prompt = build_ranking_prompt(candidate_profile=profile, job=job, context=context)
    assert "<untrusted_job_description>" in prompt.user and "Kafka pipelines" in prompt.user


# ───────────── review fixes (PR #44) ─────────────

def _merge_fuller_description(user_id, job_id, text):
    """Run Hunter's merge path, which updates descriptions without going through upsert_job."""
    from types import SimpleNamespace

    from app.services import hunter

    run = SimpleNamespace(actor_id="actor", external_run_id="run", id=None, search_profile_id=None)
    ctx = SimpleNamespace(location="Singapore")
    found = {"matched_search_role": "Lead", "source_board": "linkedin", "posted_at": None, "posted_date_raw": None,
             "source_job_id": None, "canonical_url": None, "description": text,
             **{field: None for field in hunter.FILLABLE}}
    with session(user_id) as db:
        hunter._merge(db.get(Job, job_id), run, ctx, found)
        db.commit()


def test_a_hunter_merge_with_a_fuller_posting_is_reparsed_and_never_read_stale(user):
    job_id = add_job(user)
    parse(user, job_id)
    fuller = JD_HTML + "<p>Requirements</p><ul><li>Hands-on Rust experience is required.</li></ul>"
    _merge_fuller_description(user, job_id, fuller)
    with session(user) as db:
        job = db.get(Job, job_id)
        task = db.scalar(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job_id,
                                                               JobDescriptionParseTask.status == "PENDING"))
        assert task is not None and task.reason == "description_changed"
        assert not jd.is_current(job)
        # Until the new parse lands, every task reads the posting as it is now, not the old requirements.
        context = jc.build_ranking_job_context(db, job)
        assert not context.structured and "Rust experience" in context.raw_description
        assert jd.waiting_reason(db, job) == "The job description is still being parsed"
    assert structured(job_id)["stale"] is True


def test_a_failed_reparse_of_changed_text_falls_back_to_the_raw_posting(user, monkeypatch):
    from app.services.llm import LLMClient, LLMTimeoutError

    job_id = add_job(user)
    parse(user, job_id)
    _merge_fuller_description(user, job_id, JD_HTML + "<p>Rust is required.</p>")
    monkeypatch.setattr(LLMClient, "generate_prompt_json",
                        lambda self, prompt, call=None: (_ for _ in ()).throw(LLMTimeoutError("down")))
    for _ in range(get_settings().jd_parse_max_attempts):
        jd.process(SessionLocal, claim_for(job_id))
    with session(user) as db:
        job = db.get(Job, job_id)
        assert job.structured_description_status == "NEEDS_REVIEW"
        assert jd.waiting_reason(db, job) is None
        assert "Rust is required" in jc.build_tailoring_job_context(db, job).raw_description


def test_ranking_keeps_waiting_through_the_final_attempt(user):
    job_id = add_job(user)
    with session(user) as db:
        jd.queue_parse(db, db.get(Job, job_id))
        db.commit()
        db.execute(JobDescriptionParseTask.__table__.update().where(JobDescriptionParseTask.job_id == job_id)
                   .values(status="PROCESSING", attempt_count=get_settings().jd_parse_max_attempts))
        db.commit()
        assert jd.waiting_reason(db, db.get(Job, job_id)) == "The job description is still being parsed"
    # A dead worker's task on its final attempt fails instead of looping, which ends the wait.
    with SessionLocal() as db:
        db.execute(JobDescriptionParseTask.__table__.update().where(JobDescriptionParseTask.job_id == job_id)
                   .values(lock_expires_at=jd.utcnow().replace(year=2000)))
        db.commit()
        jd.recover_expired(db)
    with session(user) as db:
        task = db.scalar(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job_id))
        assert task.status == "FAILED" and jd.waiting_reason(db, db.get(Job, job_id)) is None


def test_a_parse_given_up_on_by_lease_recovery_hands_the_job_to_ranking(user, monkeypatch):
    from app.services import ranker

    job_id = add_job(user)
    with session(user) as db:
        jd.queue_parse(db, db.get(Job, job_id))
        db.commit()
        db.execute(JobDescriptionParseTask.__table__.update().where(JobDescriptionParseTask.job_id == job_id)
                   .values(status="PROCESSING", attempt_count=get_settings().jd_parse_max_attempts,
                           lock_expires_at=jd.utcnow().replace(year=2000)))
        db.commit()
    released = []
    monkeypatch.setattr(ranker, "release_parked", lambda db, job_id: released.append(job_id) or 1)
    with SessionLocal() as db:
        jd.recover_expired(db)
    assert job_id in released


def test_a_worker_that_lost_its_lease_cannot_write(user):
    job_id = add_job(user)
    with session(user) as db:
        jd.queue_parse(db, db.get(Job, job_id))
        db.commit()
    first = claim_for(job_id)
    with SessionLocal() as db:  # the lease expires and another worker takes the task over
        db.execute(JobDescriptionParseTask.__table__.update().where(JobDescriptionParseTask.id == first.task_id)
                   .values(status="PENDING", lock_token=None))
        db.commit()
    second = claim_for(job_id)
    assert first.lock_token != second.lock_token
    assert jd.process(SessionLocal, first) == "lost"
    with session(user) as db:
        assert db.get(Job, job_id).structured_description is None, "the stale worker wrote nothing"
    assert jd.process(SessionLocal, second) == "parsed"


def test_a_missing_ai_key_waits_without_using_attempts(user, monkeypatch):
    from app.services import user_settings

    monkeypatch.setattr(user_settings, "ai_ready", lambda db, user_id: (False, "No OpenRouter API key saved"))
    job_id = add_job(user)
    with session(user) as db:
        jd.queue_parse(db, db.get(Job, job_id))
        db.commit()
    for _ in range(get_settings().jd_parse_max_attempts + 1):
        assert jd.process(SessionLocal, claim_for(job_id)) == "waiting"
    with session(user) as db:
        task = db.scalar(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job_id))
        assert task.status == "PENDING" and task.attempt_count == 0 and "API key" in task.last_error
        assert task.error_category.value == "CONFIGURATION"
    monkeypatch.setattr(user_settings, "ai_ready", lambda db, user_id: (True, ""))
    assert jd.process(SessionLocal, claim_for(job_id)) == "parsed", "the backlog resumes once the key is added"


def test_a_correction_saved_during_the_model_call_is_not_overwritten(user, monkeypatch):
    from app.services.llm import LLMClient

    job_id = add_job(user)
    parse(user, job_id)
    with session(user) as db:
        jd.queue_parse(db, db.get(Job, job_id), reason="user_request")
        db.commit()
    real = LLMClient.generate_prompt_json

    def slow_model(self, prompt, call=None):
        out = real(self, prompt, call)
        correct(job_id, lambda data: data["job_identity"].update(seniority="Staff"))  # the user saves meanwhile
        return out
    monkeypatch.setattr(LLMClient, "generate_prompt_json", slow_model)
    assert jd.process(SessionLocal, claim_for(job_id)) == "parsed"
    data = structured(job_id)
    assert data["structured_description"]["job_identity"]["seniority"] == "Staff"
    assert data["status"] == "MANUALLY_CORRECTED"


def test_explicit_requirements_must_quote_the_posting(user, monkeypatch):
    from app.services.llm import LLMClient

    monkeypatch.setattr(LLMClient, "generate_prompt_json", lambda self, prompt, call=None: {
        "structured_description": {"compensation": {"min": 250000, "max": 300000, "currency": "USD",
                                                    "source_text": "Pay is $250k-$300k."}},
        "requirements": [
            {"type": "SKILL", "normalized_value": "Python", "requirement_level": "REQUIRED",
             "source_text": "Strong experience with Python is required.", "confidence": 0.95, "is_explicit": True},
            {"type": "SKILL", "normalized_value": "Go", "requirement_level": "REQUIRED",
             "source_text": "Go is mandatory for this role.", "confidence": 0.95, "is_explicit": True}]})
    job_id = add_job(user, description=JD_HTML.replace("<p>Salary: $180,000 - $220,000 per year.</p>", ""),
                     salary_min=90000, salary_max=120000, salary_currency="SGD", salary_interval="year")
    data = parse(user, job_id)
    rows = {item["value"]: item for item in requirements(job_id, include_derived=True)}
    assert rows["Python"]["derived"] is False and rows["Python"]["source_text"]
    # A quote that is not in the posting is not the employer's word: it is kept only as a marked inference.
    assert rows["Go"]["derived"] is True and rows["Go"]["source_text"] == "" and rows["Go"]["confidence"] <= 0.5
    assert "Go" not in {item["value"] for item in requirements(job_id)}
    # Salary without a sentence from the posting is dropped; the board's figure is used and labelled as such.
    assert data["compensation"]["min"] == 90000 and data["compensation"]["source"] == "job_board"


def test_the_parser_never_sees_board_salary_or_experience():
    from app.prompts.job_description_parser import build_job_description_parser_prompt

    class BoardJob:
        id, title, company, location = 1, "Data Platform Lead", "Globex", "Singapore"
        remote_type, job_type, seniority, experience = "remote", "full-time", "lead", "10+ years"
        salary_min, salary_max, salary_currency, salary_interval = 90000, 120000, "SGD", "year"

    prompt = build_job_description_parser_prompt(BoardJob(), "Strong Python experience is required.")
    posting = prompt.user.split("JOB POSTING:\n", 1)[1].split("\n", 1)[0]
    assert "Globex" in posting
    for leaked in ("90000", "SGD", "10+ years", "salary", "experience"):
        assert leaked not in posting, leaked


def test_corrections_reach_the_requirement_rows_workflows_read(user):
    job_id = add_job(user)
    parse(user, job_id)
    kubernetes = "Familiarity with Kubernetes is a plus."
    python = "Strong experience with Python is required."

    def change(data):
        data["preferred_skills"].remove(kubernetes)
        data["required_skills"].append(kubernetes)       # moved: the user says it is required
        data["required_skills"].remove(python)           # removed: not a requirement after all
        data["required_skills"].append("Terraform")      # added: not in the posting
    correct(job_id, change)
    rows = {item["value"]: item for item in requirements(job_id)}
    assert rows[kubernetes]["level"] == "REQUIRED" and rows[kubernetes]["origin"] == "USER"
    assert rows[python]["level"] == "INFORMATIONAL" and rows[python]["source_text"] == python  # quote kept
    assert rows["Terraform"]["level"] == "REQUIRED" and rows["Terraform"]["origin"] == "USER"
    assert rows["Terraform"]["source_text"] == "" and rows["Terraform"]["derived"] is False
    with session(user) as db:
        ranking = jc.build_ranking_job_context(db, db.get(Job, job_id))
    required = {item["value"] for item in ranking.required}
    assert kubernetes in required and "Terraform" in required and python not in required


def test_resolving_a_reparse_only_applies_the_accepted_values(user, monkeypatch):
    job_id = add_job(user)
    parse(user, job_id)
    correct(job_id, lambda data: data["job_identity"].update(seniority="Staff"))
    before = {(item["value"], item["level"]) for item in requirements(job_id)}

    # The next parse differs in the title and adds a requirement that never shows up as a comparison choice.
    from app.services.llm import LLMClient
    real = LLMClient.generate_prompt_json

    def changed(self, prompt, call=None):
        out = real(self, prompt, call)
        out["structured_description"]["job_identity"]["title"] = "Principal Data Engineer"
        out["requirements"].append({"type": "SKILL", "normalized_value": "Strong experience with Python is required.",
                                    "requirement_level": "PREFERRED", "source_text": "Strong experience with Python "
                                    "is required.", "confidence": 0.9, "is_explicit": True})
        return out
    monkeypatch.setattr(LLMClient, "generate_prompt_json", changed)
    client.post(f"/api/jobs/{job_id}/structured-description/reparse")
    title = next(c for c in structured(job_id)["candidate"]["changes"] if c["path"] == "job_identity.title")
    out = client.post(f"/api/jobs/{job_id}/structured-description/reparse/resolve",
                      json={"decisions": {title["id"]: "use_new"}}).json()
    assert out["structured_description"]["job_identity"]["title"] == "Principal Data Engineer"
    assert {(item["value"], item["level"]) for item in requirements(job_id)} == before, \
        "accepting a title must not replace the requirement rows"


def test_selected_tokens_do_not_count_an_unsent_posting(user):
    import io

    from app.services import context_builder as cb
    from app.services.user_settings import profile_for

    client.post("/api/profile/resume", files={"file": ("r.txt", io.BytesIO(
        b"# Jane\n\n## Skills\n- Languages: Python\n\n## Experience\n### Lead | Zendrive | Remote | Jun 2024 - Oct 2025\n"
        b"- Ran Kafka pipelines\n"), "text/plain")})
    job_id = add_job(user, description=JD_HTML + "<p>" + "Benefits and perks paragraph. " * 400 + "</p>")
    with session(user) as db:
        raw = cb.build_ranking_context(db, profile_for(db), db.get(Job, job_id)).stats.selected_context_tokens
        db.commit()
    parse(user, job_id)
    with session(user) as db:
        context = cb.build_ranking_context(db, profile_for(db), db.get(Job, job_id))
        db.commit()
    assert context.stats.selected_context_tokens < raw - 1000, "the unsent advert is no longer counted as selected"
    assert context.job_stats.raw_job_tokens > context.job_stats.selected_job_context_tokens


def test_long_requirement_sections_stay_valid_json():
    import json

    from app.prompts.common import job_sections

    context = {"job_context": {"structured": True, "required": [
        {"id": f"req_{i}", "value": f"Requirement {i} " + "x" * 300, "level": "REQUIRED",
         "source_text": "y" * 600} for i in range(200)]}}
    section, = job_sections(context, with_source=True)
    body = section.split("JOB REQUIREMENTS:\n", 1)[1]
    parsed = json.loads(body)  # would raise on a string cut at an arbitrary character
    assert len(body) <= 24000 and 0 < len(parsed["required"]) < 200


def _model_parse(monkeypatch, required, preferred, rows):
    """Make the parser answer like a real model: short skill names in the lists, sentences in the requirements."""
    from app.services.llm import LLMClient

    monkeypatch.setattr(LLMClient, "generate_prompt_json", lambda self, prompt, call=None: {
        "structured_description": {"required_skills": required, "preferred_skills": preferred},
        "requirements": [{"type": "SKILL", "normalized_value": value, "requirement_level": level,
                          "source_text": quote, "confidence": 0.9, "is_explicit": True}
                         for value, level, quote in rows]})


def test_corrections_reach_rows_whose_text_differs_from_the_list(user, monkeypatch):
    _model_parse(monkeypatch, ["Python", "Kafka"], ["Kubernetes"], [
        ("Strong experience with Python", "REQUIRED", "Strong experience with Python is required."),
        ("Kafka pipelines", "REQUIRED", "You will own the Kafka pipelines behind our analytics platform."),
        ("Familiarity with Kubernetes", "PREFERRED", "Familiarity with Kubernetes is a plus.")])
    job_id = add_job(user)
    parse(user, job_id)

    out = correct(job_id, lambda d: d["required_skills"].remove("Python")).json()
    assert out["unmatched_corrections"] == []
    rows = {item["value"]: item for item in requirements(job_id)}
    assert rows["Strong experience with Python"]["level"] == "INFORMATIONAL", "the removal reached the row"
    assert rows["Strong experience with Python"]["origin"] == "USER"

    correct(job_id, lambda d: (d["preferred_skills"].remove("Kubernetes"), d["required_skills"].append("Kubernetes")))
    rows = requirements(job_id)
    kubernetes = [item for item in rows if "Kubernetes" in item["value"]]
    assert [(item["value"], item["level"]) for item in kubernetes] == [("Familiarity with Kubernetes", "REQUIRED")], \
        "the move changed the existing row instead of adding a contradictory second one"
    with session(user) as db:
        required = {item["value"] for item in jc.build_ranking_job_context(db, db.get(Job, job_id)).required}
    assert "Strong experience with Python" not in required and "Familiarity with Kubernetes" in required


def test_an_ambiguous_correction_is_reported_and_changes_no_row(user, monkeypatch):
    _model_parse(monkeypatch, ["Python", "Go"], [], [
        ("Python services at scale", "REQUIRED", "Strong experience with Python is required."),
        ("Python and Go", "REQUIRED", "Minimum of 8 years building distributed systems.")])
    job_id = add_job(user)
    parse(user, job_id)
    before = {(item["value"], item["level"]) for item in requirements(job_id)}
    out = correct(job_id, lambda d: d["required_skills"].remove("Python")).json()
    # Demoting both rows would also demote Go, which the user kept: nothing is guessed, and the user is told.
    assert out["unmatched_corrections"] == ["Python"]
    assert {(item["value"], item["level"]) for item in requirements(job_id)} == before
    assert out["structured_description"]["required_skills"] == ["Go"], "the correction itself is still saved"


def test_the_parser_must_name_list_skills_like_their_requirements():
    from app.prompts.job_description_parser import (
        JOB_DESCRIPTION_PARSER_PROMPT_VERSION,
        JOB_DESCRIPTION_PARSER_SYSTEM,
    )

    assert JOB_DESCRIPTION_PARSER_PROMPT_VERSION == "v3"
    assert "copied exactly from the normalized_value of a SKILL" in JOB_DESCRIPTION_PARSER_SYSTEM



def test_a_correction_started_before_a_parse_landed_is_refused(user):
    """The API side of the race: an edit of an older version must not revert a newer parse."""
    job_id = add_job(user)
    parse(user, job_id)
    opened = structured(job_id)          # the user opens the edit form here
    edited = opened["structured_description"]
    edited["job_identity"]["seniority"] = "Staff"

    with session(user) as db:            # ...and a scheduled parse lands before they save
        job = db.get(Job, job_id)
        job.structured_description = {**job.structured_description,
                                      "role_summary": "Lead the data platform team (re-parsed)."}
        db.commit()
    newer = structured(job_id)

    out = client.put(f"/api/jobs/{job_id}/structured-description",
                     json={"structured_description": edited, "base_hash": opened["structured_hash"]})
    assert out.status_code == 409 and "parsed again" in out.json()["detail"]
    after = structured(job_id)
    assert after["structured_description"] == newer["structured_description"], "nothing was written"
    assert after["status"] == "PARSED"
    history = client.get(f"/api/jobs/{job_id}/structured-description/history").json()["items"]
    assert not [item for item in history if item["change_source"] == "USER"], \
        "the parser's values were not recorded as user corrections"

    # Editing the version that is actually stored works.
    assert correct(job_id, lambda d: d["job_identity"].update(seniority="Staff")).status_code == 200


def test_a_correction_must_name_the_version_it_edits(user):
    job_id = add_job(user)
    parse(user, job_id)
    data = structured(job_id)["structured_description"]
    assert client.put(f"/api/jobs/{job_id}/structured-description",
                      json={"structured_description": data}).status_code == 422


def test_the_parse_lock_holds_the_sqlite_writer_lock_until_commit(user):
    """On SQLite a plain SELECT holds no lock, so the worker's re-read and its write could interleave with a save."""
    import sqlite3

    from app.db import engine

    job_id = add_job(user)
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite-specific: PostgreSQL uses SELECT ... FOR UPDATE")
    path = engine.url.database
    with session(user) as db:
        assert jd.lock_job(db, job_id) is not None
        other = sqlite3.connect(path, timeout=0.1)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("UPDATE jobs SET title = title WHERE id = ?", (job_id,))
            other.commit()
        other.close()
        db.rollback()


# ───────────── a requirement in the wrong shape ─────────────

SKILL_SHAPED = {"structured_description": {}, "requirements": [
    {"skill": "Strong hands-on Google Cloud Platform (GCP) experience"},
    {"skill": "Deep production experience with Kubernetes (GKE preferred)"}]}


def test_requirements_without_the_template_fields_break_the_contract():
    """Regression: {"skill": ...} items used to pass the contract (typed as plain dicts) and only failed in the
    service, on every attempt, so the gateway never sent its one schema correction."""
    from pydantic import ValidationError

    from app.prompts.contracts import JobDescriptionParserOutput

    with pytest.raises(ValidationError) as caught:
        JobDescriptionParserOutput.model_validate(SKILL_SHAPED)
    fields = {".".join(str(p) for p in e["loc"]) for e in caught.value.errors()}
    assert {"requirements.0.normalized_value", "requirements.0.skill"} <= fields
    ok = JobDescriptionParserOutput.model_validate({"structured_description": {}, "requirements": [
        {"type": "skill", "normalized_value": "GCP", "requirement_level": "required",
         "source_text": "Strong hands-on GCP experience"}] * 250})
    assert ok.requirements[0].requirement_level == "REQUIRED", "levels are normalised, not failed"
    assert len(ok.requirements) == 250, "a long list is not a contract failure: the service keeps the first 200"


def test_a_skill_shaped_answer_is_corrected_once_and_then_parses(user, monkeypatch):
    from app.services.llm import LLMClient

    job_id = add_job(user)
    answers = []
    real = LLMClient._mock

    def mock(self, task, user_text):
        if "PARSE_JOB_DESCRIPTION" in user_text.split("\n", 1)[0]:
            answers.append(user_text)
            if len(answers) == 1:
                return SKILL_SHAPED
        return real(self, task, user_text)
    monkeypatch.setattr(LLMClient, "_mock", mock)
    parse(user, job_id)
    assert len(answers) == 2 and "SCHEMA CORRECTION" in answers[1]
    assert "normalized_value" in answers[1].split("SCHEMA CORRECTION", 1)[1], "it says what was missing"
    with session(user) as db:
        rows = db.scalars(select(JobRequirementEvidence).where(JobRequirementEvidence.job_id == job_id)).all()
    assert rows and all(row.normalized_value for row in rows)


# ───────────── the parse state lists show and filter by ─────────────

def test_the_description_state_says_where_the_parse_stands(user):
    job_id = add_job(user)
    with session(user) as db:
        job = db.get(Job, job_id)
        assert jd.description_state(Job(description=""), None) == "NO_DESCRIPTION"
        assert jd.description_state(job, "PENDING") == "PARSING"
        assert jd.description_state(job, None) == "NOT_PARSED"
    parse(user, job_id)
    with session(user) as db:
        job = db.get(Job, job_id)
        status = jd.latest_parse_statuses(db, [job_id]).get(job_id)
        assert jd.description_state(job, status) == "PARSED"
        job.description = job.description + "\nAlso: Rust."  # new wording: the old parse no longer counts
        assert jd.description_state(job, status) == "NOT_PARSED"
        job.structured_description_status = jd.NEEDS_REVIEW
        assert jd.description_state(job, "FAILED") == "FAILED"


def test_the_jobs_table_filters_by_description_state(user):
    parsed, waiting = add_job(user), add_job(user)
    parse(user, parsed)
    out = client.get("/api/jobs/search", params={"q": "Globex", "description": "PARSED", "per_page": 200}).json()
    ids = {item["id"] for item in out["items"]}
    assert parsed in ids and waiting not in ids
    assert all(item["description_state"] == "PARSED" for item in out["items"])
    assert {key for key, _name, _n in out["description_states"]} == set(jd.DESCRIPTION_STATES)
    assert client.get("/api/jobs/search", params={"description": "nope"}).status_code == 422
    assert client.get(f"/api/jobs/{parsed}").json()["description_state"] == "PARSED"


def test_a_wrong_shape_in_the_structured_description_is_a_schema_failure_inside_the_call(user, monkeypatch):
    """A small model answering education_requirements as an object: the gateway sees it as the schema failure it is,
    logs it so, and asks once more with the exact problem, instead of logging a success the task cannot use."""
    from app.models import LLMRequestLog
    from app.services.llm import LLMClient, LLMSchemaError

    answers = [
        {"structured_description": {"education_requirements": {"Minimum education": "Bachelor's degree"}},
         "requirements": []},
        {"structured_description": {"education_requirements": ["Bachelor's degree"]}, "requirements": []},
    ]
    sent = []

    def model(self, provider, system, user_message, task, model_name, config, attempt):
        from app.services.llm import LLMResult
        sent.append(user_message)
        attempt.sent, attempt.model = True, "small-model"
        return LLMResult(answers[len(sent) - 1], "mock", "small-model")
    monkeypatch.setattr(LLMClient, "_dispatch", model)
    job_id = add_job(user)
    with session(user) as db:
        parsed = jd.parse_now(db, db.get(Job, job_id))
    assert parsed.structured_description.education_requirements == ["Bachelor's degree"]
    assert len(sent) == 2 and "structured_description.education_requirements" in sent[1]
    with SessionLocal() as db:
        rows = db.scalars(select(LLMRequestLog).where(LLMRequestLog.workflow_entity_id == job_id)
                          .order_by(LLMRequestLog.id)).all()
    assert [(r.status, r.error_type) for r in rows][-2:] == [("FAILED", "SCHEMA_VALIDATION"), ("SUCCEEDED", None)]
    with pytest.raises(LLMSchemaError):
        jd.checked({"structured_description": {"education_requirements": {"a": "b"}}, "requirements": []})
