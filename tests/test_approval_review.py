"""What the Approvals screen tells the person deciding (app.services.approval_review, /api/approvals).

* The queue labels each status for what it is; a rejected job is out of the default view.
* The reviewer's disputed claims are listed one by one, in plain words, never as counts or codes.
* The checklist states what it measured: evidence the reviewer disputes, the length target, mechanical defects.
* "ATS answers" are this job's questions only; another job's motivation answer is flagged, a sensitive one withheld.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.main import app
from app.models import Job, JobInsight, KnowledgeAnswer, ResumeVariant
from app.services import approval_review
from app.settings import get_settings
from tests.conftest import ensure_test_user

client = TestClient(app)

NOTES = {
    "unsupported_claims": ["The AI Marketing Platform project is not in any verified fact."],
    "overstated_claims": ["'Engineered a targeting system' overstates the verified project."],
    "deterministic_issues": [{"code": "UNSUPPORTED_TERM", "location": "cover_letter",
                              "message": "['s3-backed'] appear nowhere in the master resume, your profile or verified "
                                         "evidence; cite them there first or leave them out"}],
    "reject_reasons": [("UNSUPPORTED_TERM: ['s3-backed'] appear nowhere in the master resume, your profile or "
                        "verified evidence; cite them there first or leave them out"),
                       "1 unsupported claim(s) found by the reviewer", "1 overstated claim(s) found by the reviewer"],
    "improvements": ["Familiarity with OAuth."],
}
MARKDOWN = """# Jane Doe
Engineer

## Projects
Knowledge Assistant: Designed RAG pipelines, resulting in 30% faster document retrieval for teams.
- resulting in 30% faster document retrieval for teams.
"""


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


@pytest.fixture
def owner():
    return ensure_test_user().id


@pytest.fixture(autouse=True)
def clean(owner):
    with SessionLocal() as db:
        # SQLite reuses job ids and does not cascade: remove what hangs off the jobs first.
        ids = select(Job.id).where(Job.user_id == owner, Job.source == "approval-review-test")
        db.execute(delete(ResumeVariant).where(ResumeVariant.job_id.in_(ids)))
        db.execute(delete(JobInsight).where(JobInsight.job_id.in_(ids)))
        db.execute(delete(Job).where(Job.user_id == owner, Job.source == "approval-review-test"))
        db.execute(delete(KnowledgeAnswer).where(KnowledgeAnswer.user_id == owner,
                                                 KnowledgeAnswer.category == "approval-review-test"))
        db.commit()
    yield


def make_job(owner, *, status="tailored", notes=None, markdown=MARKDOWN, content=None, company="Logitech"):
    with SessionLocal() as db:
        job = Job(user_id=owner, source="approval-review-test", source_ref=uuid.uuid4().hex,
                  title="Lead AI Engineer", company=company, status=status, description="Python and leadership")
        db.add(job)
        db.flush()
        db.add(ResumeVariant(job_id=job.id, user_id=owner, version=1, markdown=markdown, reviewer_score=74,
                             reviewer_notes=notes if notes is not None else NOTES, content=content or {}))
        db.commit()
        return job.id


def answer(owner, question, text, *, job_id=None, verified=True, sensitive=False, asked_by=None):
    with SessionLocal() as db:
        row = KnowledgeAnswer(user_id=owner, question=question, question_key=uuid.uuid4().hex, answer=text,
                              category="approval-review-test", verified=verified, sensitive=sensitive,
                              source_job_id=job_id,
                              request_context={"job_ids": asked_by} if asked_by else None)
        db.add(row)
        db.commit()
        return row.id


def rows(**params):
    return client.get("/api/approvals", params={"q": "Logitech", **params}).json()


def test_each_status_is_labelled_for_what_it_is(owner):
    co = "Statuslabel Co"  # a company no other test uses, so counts are this test's alone
    new, back, gone = (make_job(owner, company=co), make_job(owner, status="pending_approval", company=co),
                       make_job(owner, status="rejected", company=co))
    listed = {r["id"]: r for r in rows(q=co)}
    assert listed[new]["age"].startswith("tailored") and listed[new]["to_decide"]
    assert listed[back]["age"] == "sent back" and listed[back]["to_decide"] and not listed[back]["waiting"]
    assert gone not in listed, "a rejected job is out of the default view"
    assert [r["id"] for r in rows(q=co, status="sent_back")] == [back]
    rejected = rows(q=co, status="rejected")
    assert [r["id"] for r in rejected] == [gone] and rejected[0]["age"] == "rejected"
    facets = rows(q=co, with_facets=True)
    assert facets["status_counts"]["rejected"] == 1 and facets["status_counts"]["all"] == 2
    assert facets["min_review_score"] == client.get(f"/api/approvals/{new}").json()["min_review_score"]


def test_disputed_claims_are_listed_one_by_one_in_plain_words(owner):
    review = client.get(f"/api/approvals/{make_job(owner)}").json()["resume"]["review"]
    packet = [n["text"] for n in review if n["group"] == "packet"]
    assert packet == [
        ("Cover letter: “s3-backed” isn't in the master resume, your profile or verified evidence — add it there "
         "first, or leave it out"),
        "Unsupported: The AI Marketing Platform project is not in any verified fact.",
        "Overstated: 'Engineered a targeting system' overstates the verified project."]
    assert [n["text"] for n in review if n["group"] == "gap"] == ["Familiarity with OAuth."]
    assert not any("found by the reviewer" in n["text"] or "UNSUPPORTED_TERM" in n["text"] for n in review)


def test_a_review_stored_before_issue_lists_still_reads_as_sentences():
    notes = {"reject_reasons": ["INVENTED_METRIC: Numbers ['45'] are not in the cited evidence",
                                "Review score 60 is below your minimum of 70"]}
    assert [n["text"] for n in approval_review.review_notes(notes)] == [
        "Resume: Numbers “45” are not in the cited evidence", "Review score 60 is below your minimum of 70"]


def test_the_checklist_states_what_it_measured(owner):
    checks = {c["label"]: c for c in client.get(f"/api/approvals/{make_job(owner)}").json()["checklist"]}
    evidence = checks["Claims backed by evidence"]
    assert evidence["state"] == "warn" and "reviewer disputes 2 claims" in evidence["detail"]
    invented = checks["No invented experience"]
    assert invented["state"] == "warn" and invented["detail"].startswith("Cover letter: “s3-backed”")
    clean = checks["Resume reads cleanly"]
    assert clean["state"] == "warn" and "repeats text" in clean["detail"]
    assert clean["issues"][0]["code"] == "REPEATED_LINE"
    assert checks["Length"]["state"] == "ok" and "target 2" in checks["Length"]["detail"]


def test_the_length_check_uses_the_packets_own_page_target(owner):
    long = "# Jane\n\n## Experience\n" + "\n".join(f"- Built system number {i} with many words here" * 3
                                                 for i in range(60))
    job = make_job(owner, notes={}, markdown=long, content={"length_plan": {"pages": 1}})
    length = next(c for c in client.get(f"/api/approvals/{job}").json()["checklist"] if c["label"] == "Length")
    assert length["state"] == "warn" and "target 1" in length["detail"]
    clean = make_job(owner, notes={}, markdown="# Jane\n\n## Summary\nEngineer with ten years of work.\n")
    checks = {c["label"]: c["state"] for c in client.get(f"/api/approvals/{clean}").json()["checklist"]}
    assert checks["No invented experience"] == checks["Resume reads cleanly"] == checks["Length"] == "ok"


def test_only_this_jobs_questions_are_shown(owner):
    job, other = make_job(owner), make_job(owner)
    answer(owner, "Python Experience", "11 years")  # in the library, but this job never asked it
    answer(owner, "Do you need sponsorship?", "No", sensitive=True, asked_by=[job], job_id=job)
    answer(owner, "Why are you interested in this position? *\nHint : In terms of the company", "I like it",
           job_id=other, asked_by=[other, job])
    answer(owner, "Notice period?", "", verified=False, asked_by=[job], job_id=job)
    with SessionLocal() as db:
        db.add(JobInsight(user_id=owner, job_id=job, application_form_input=[],
                          application_questions=[{"id": "p1", "question": "Describe a system you scaled",
                                                  "origin": "PREDICTED", "status": "DRAFTED", "answer": "Draft"}]))
        db.commit()
    out = client.get(f"/api/approvals/{job}").json()["answers"]
    by_question = {a["question"]: a for a in out["items"]}
    assert out["known"] and "Python Experience" not in by_question
    assert by_question["Describe a system you scaled"]["state"] == "draft"
    assert by_question["Do you need sponsorship?"] == {**by_question["Do you need sponsorship?"],
                                                      "state": "confirm", "answer": None}
    motivation = by_question["Why are you interested in this position?"]
    assert motivation["state"] == "other_job" and motivation["answer"] == "I like it"
    assert by_question["Notice period?"]["state"] == "needs_input"


def test_a_motivation_answer_written_for_this_job_is_verified(owner):
    job = make_job(owner)
    answer(owner, "Why do you want to work at Logitech?", "Because", job_id=job, asked_by=[job])
    answer(owner, "Why are you looking for a new job?", "Growth", asked_by=[job])
    states = {a["question"]: a["state"] for a in client.get(f"/api/approvals/{job}").json()["answers"]["items"]}
    assert states == {"Why do you want to work at Logitech?": "verified",
                      "Why are you looking for a new job?": "verified"}, "not about one employer: reusable"


@pytest.mark.parametrize(("question", "specific"), [
    ("Why are you interested in this position?", True), ("Why do you want to work at Anthropic?", True),
    ("What excites you about this role?", True), ("Why should we hire you for the team?", True),
    ("Why are you looking for a new job?", False), ("Python experience", False),
])
def test_job_specific_questions(question, specific):
    assert approval_review.job_specific(question) is specific


def test_the_detail_states_the_roles_facts_and_what_approving_does(owner):
    job_id = make_job(owner)
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        job.location, job.remote_type, job.seniority = "Dublin, Ireland", "hybrid", "Senior"
        job.salary_min, job.salary_max, job.salary_currency, job.salary_interval = 110000, 135000, "eur", "year"
        job.url = "https://boards.greenhouse.io/acme/jobs/123"
        db.commit()
    out = client.get(f"/api/approvals/{job_id}").json()
    facts = out["facts"]
    assert facts["work_mode"] == "hybrid" and facts["salary"]["text"] == "EUR 110K–135K/yr"
    assert facts["seniority"] == "Senior" and facts["route"]["label"]
    # Auto submit is off on this test server: approving opens the apply kit, and the label says so.
    assert out["approve_effect"]["kind"] == "kit" and out["approve_effect"]["label"] == "Approve → apply kit"
    row = next(r for r in rows() if r["id"] == job_id)
    assert row["work_mode"] == "hybrid" and row["salary"] == "EUR 110K–135K/yr"


def test_an_unstated_salary_stays_unknown(owner):
    job_id = make_job(owner)
    facts = client.get(f"/api/approvals/{job_id}").json()["facts"]
    assert facts["salary"] is None and facts["work_mode"] is None


def test_checks_say_which_must_be_fixed_and_how(owner):
    checks = {c["label"]: c for c in client.get(f"/api/approvals/{make_job(owner)}").json()["checklist"]}
    invented = checks["No invented experience"]
    assert invented["level"] == "blocker" and invented["fix"] == {"label": "Fix in Tailor", "screen": "tailor"}
    assert checks["Length"]["level"] == "nudge" and "fix" not in checks["Length"], "a passed check needs no fix"


def test_the_pay_check_follows_this_jobs_form(owner):
    job = make_job(owner)
    pay = lambda: next(c for c in client.get(f"/api/approvals/{job}").json()["checklist"]
                       if c["label"] == "Compensation answer")
    assert pay()["detail"].startswith("No verified pay answer yet; needed only if the form asks")
    with SessionLocal() as db:
        db.add(JobInsight(user_id=owner, job_id=job, application_form_input=[{"id": "q1"}],
                          application_questions=[{"id": "p1", "question": "Expected salary?", "origin": "PREDICTED",
                                                  "status": "DRAFTED", "answer": "x"},
                                                 {"id": "q1", "question": "Years of Python?", "origin": "FORM",
                                                  "status": "DRAFTED", "answer": "11"}]))
        db.commit()
    assert pay()["state"] == "warn", "a predicted pay question counts as asked until the form says otherwise"
    with SessionLocal() as db:
        insight = db.scalar(select(JobInsight).where(JobInsight.job_id == job))
        insight.application_questions = [q for q in insight.application_questions if q["origin"] == "FORM"]
        db.commit()
    assert pay() == {**pay(), "state": "ok", "detail": "This form doesn't ask about pay"}
    answer(owner, "What is your expected salary?", "", verified=False, asked_by=[job], job_id=job)
    assert pay()["state"] == "warn" and "asks “What is your expected salary?”" in pay()["detail"]


def test_role_gaps_lead_with_required_requirements_that_are_not_met(owner):
    job_id = make_job(owner)
    with SessionLocal() as db:
        db.get(Job, job_id).fit_analysis = {"requirement_assessments": [
            {"requirement": "Kubernetes", "level": "PREFERRED", "status": "MISSING"},
            {"requirement": "Python", "level": "REQUIRED", "status": "MATCH", "supported": "11 years"},
            {"requirement": "OAuth", "level": "REQUIRED", "status": "PARTIAL_MATCH", "unsupported": "no OIDC"}]}
        db.commit()
    gaps = client.get(f"/api/approvals/{job_id}").json()["role_gaps"]
    assert [(g["requirement"], g["status"]) for g in gaps] == [("OAuth", "partial"), ("Python", "met"),
                                                               ("Kubernetes", "missing")]


def test_postings_of_one_role_share_a_duplicate_key_and_other_roles_do_not(owner):
    same = [make_job(owner, company="Affirm") for _ in range(2)]
    with SessionLocal() as db:
        for job_id, title in zip(same, ["Senior Staff SWE, Backend (Cards)", "senior staff swe — backend (cards)"],
                                 strict=True):
            db.get(Job, job_id).title = title
        other = db.get(Job, make_job(owner, company="Affirm"))
        other.title = "Senior Staff SWE, Fullstack (Marketplace)"
        db.commit()
        other_id = other.id
    keys = {r["id"]: r["duplicate_key"] for r in rows(q="Affirm")}
    assert keys[same[0]] == keys[same[1]] != keys[other_id]


def test_the_queue_filters_by_work_mode(owner):
    remote, office = make_job(owner), make_job(owner)
    with SessionLocal() as db:
        db.get(Job, remote).remote_type = "remote"
        db.commit()
    assert [r["id"] for r in rows(work_mode="remote")] == [remote]
    assert office in [r["id"] for r in rows(work_mode="not_stated")]
    assert rows(with_facets=True)["work_modes"]["remote"] == 1
    assert client.get("/api/approvals", params={"work_mode": "moon"}).status_code == 422


def _form(label):
    from app.services.ats_forms import ApplicationForm, FormField, SubmitControl
    return ApplicationForm(adapter="test", host="example.com", submit=SubmitControl(present=True, label="Submit"),
                           fields=[FormField(id="why", label=label, kind="textarea", required=True, locator="#why")])


def test_auto_apply_never_sends_another_jobs_motivation_answer(owner):
    from app.services import answer_resolver

    first, second = make_job(owner), make_job(owner)
    row = answer(owner, "Why are you interested in this position?", "Logitech's mice", job_id=first, asked_by=[first])
    reusable = answer(owner, "Why are you looking for a new job?", "Growth", job_id=first)
    with SessionLocal() as db:
        db.info["user_id"] = owner
        job = db.get(Job, second)
        plan = answer_resolver.plan(db, job, _form("Why are you interested in this position?"))
        assert plan["fields"]["why"]["status"] == "NEEDS_INPUT" and plan["needs_input"] == [row]
        assert answer_resolver.plan(db, db.get(Job, first), _form("Why are you interested in this position?")
                                    )["fields"]["why"]["status"] == "RESOLVED", "the job it was written for uses it"
        assert answer_resolver.plan(db, job, _form("Why are you looking for a new job?")
                                    )["fields"]["why"]["status"] == "RESOLVED", "not about one employer: reused"
    assert reusable
    # Answering it for this job (the Apply kit) makes the saved answer this job's.
    out = client.put(f"/api/knowledge/{row}", json={"question": "Why are you interested in this position?",
                                                    "answer": "Logitech builds what I use", "verified": True,
                                                    "category": "approval-review-test", "for_job_id": second})
    assert out.status_code == 200
    with SessionLocal() as db:
        db.info["user_id"] = owner
        assert answer_resolver.plan(db, db.get(Job, second), _form("Why are you interested in this position?")
                                    )["fields"]["why"]["status"] == "RESOLVED"


def test_job_insights_draft_a_motivation_answer_written_for_another_job(owner):
    from app.services.job_insights import application_form

    first, second = make_job(owner), make_job(owner)
    answer(owner, "Why are you interested in this position?", "Logitech's mice", job_id=first)
    with SessionLocal() as db:
        db.info["user_id"] = owner
        waiting: set[str] = set()
        assert application_form.before_model(db, "q", "Why are you interested in this position?", "FORM", waiting,
                                             second) is None and waiting == {"q"}, "drafted for this job instead"
        settled = application_form.before_model(db, "q", "Why are you interested in this position?", "FORM", set(),
                                                first)
        assert settled["status"] == "LIBRARY"
        assert all(not approval_review.job_specific(a["question"])
                   for a in application_form.known_answers(db, owner)), "never shown to the drafting model"


def test_salary_filter_posted_sort_and_stale_postings(owner):
    from datetime import datetime, timedelta

    paid, old, fresh = make_job(owner), make_job(owner), make_job(owner)
    with SessionLocal() as db:
        db.get(Job, paid).salary_min = 100000
        db.get(Job, old).posted_at = datetime.now() - timedelta(days=45)
        db.get(Job, fresh).posted_at = datetime.now() - timedelta(days=2)
        db.commit()
    assert [r["id"] for r in rows(salary="only")] == [paid]
    assert [r["id"] for r in rows(sort="posted")][:2] == [fresh, old], "undated postings last"
    assert client.get(f"/api/approvals/{old}").json()["stale"]["kind"] == "old"
    assert client.get(f"/api/approvals/{fresh}").json()["stale"] is None
    why = client.get(f"/api/approvals/{paid}").json()["score_why"]
    assert why["score"] == 74 and why["disputed"] == 2 and why["checks_failed"] == 1


def test_why_the_score_reads_as_sentences(owner):
    job = make_job(owner)
    with SessionLocal() as db:
        variant = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job))
        variant.reviewer_notes = {**NOTES, "decision_reasons": NOTES["reject_reasons"]}
        db.commit()
    reasons = client.get(f"/api/approvals/{job}").json()["score_why"]["decision_reasons"]
    assert reasons == [("Resume: “s3-backed” isn't in the master resume, your profile or verified evidence — add it "
                        "there first, or leave it out")]
