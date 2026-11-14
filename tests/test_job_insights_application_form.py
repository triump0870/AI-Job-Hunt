"""Application-form questions on Job Insights: pasted questions drafted in the background, predicted ones drafted in a
research run, the person's library answers winning, personal questions never drafted, grounding checks, the last good
drafts kept, tenancy and the API. The model is the no-cost mock provider or a stub returning a crafted answer."""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import Evidence, JobInsight, KnowledgeAnswer
from app.prompts.contracts import ApplicationFormPrepResult
from app.services.job_insights import application_form, provenance, service, synthesis
from app.services.knowledge import question_key
from app.services.llm import LLMClient, LLMResult
from tests.conftest import make_user
from tests.test_job_insights import (  # noqa: F401  fixtures are used by name
    FakeSource,
    insight_of,
    insights,
    new_job,
    owner,
    refresh,
    run_insights,
    signed_in,
    tasks_of,
)

MARK = "test:application-form"
NOTICE = "What is your notice period for Northwind?"


def _seed(owner: int) -> int:
    """A verified fact the posting shares words with, and a verified saved answer to one form question."""
    with SessionLocal() as db:
        db.execute(delete(Evidence).where(Evidence.context == MARK))
        db.execute(delete(KnowledgeAnswer).where(KnowledgeAnswer.category == "test_form"))
        fact = Evidence(user_id=owner, company="Northwind Labs", role="Backend Engineer", context=MARK,
                        claim="Built reliable Python services for payments", verified=True)
        db.add(fact)
        db.add(KnowledgeAnswer(user_id=owner, question=NOTICE, question_key=question_key(NOTICE),
                               answer="One month", category="test_form", verified=True))
        db.commit()
        return fact.id


def _unseed() -> None:
    with SessionLocal() as db:
        db.execute(delete(Evidence).where(Evidence.context == MARK))
        db.execute(delete(KnowledgeAnswer).where(KnowledgeAnswer.category == "test_form"))
        db.commit()


def _crafted(monkeypatch, items):
    """The model answers the application-form prompt with `items` (a function of the prompt's user text)."""
    real = LLMClient.generate_prompt

    def generate(self, prompt, call=None):
        if prompt.name != "application_form":
            return real(self, prompt, call)
        data = {"form_questions": items(prompt.user)}
        return LLMResult(data, "mock", "mock", validated=call.validate(data))
    monkeypatch.setattr(LLMClient, "generate_prompt", generate)


def by_question(job_id):
    return {q["question"]: q for q in insight_of(job_id).application_questions or []}


# ───────────── pasted questions ─────────────

def test_pasted_questions_are_drafted_in_the_background_without_research(owner, signed_in, monkeypatch):
    fact = _seed(owner)
    try:
        job_id = new_job(owner)
        run_insights()
        counted = FakeSource("glassdoor_interview")
        monkeypatch.setattr(service, "SOURCES", [counted])
        client = signed_in(owner)
        out = client.post(f"/api/jobs/{job_id}/insights/application-questions", json={"questions": [
            "1. Why do you want to join Northwind Labs?", "Do you require visa sponsorship to work here?", NOTICE,
            "short"]}).json()
        assert out["added"] == 3, "list markers are stripped and fragments dropped"
        form = out["application_form"]
        assert form["pending"] and {q["status"] for q in form["from_form"]} == {"PENDING"}
        assert [t.scope for t in tasks_of(job_id)][-1] == "APPLICATION_FORM"
        assert out["task"]["scope"] == "APPLICATION_FORM"

        assert run_insights() == ["completed"]
        assert counted.calls == 0, "a form-only task never calls a research source"
        got = by_question(job_id)
        why = got["Why do you want to join Northwind Labs?"]
        assert why["status"] == "DRAFTED" and why["kind"] == "GENERATED" and why["evidence_ids"]
        with SessionLocal() as db:  # the mock drafts from the first evidence it is shown: verified, this account's
            cited = db.scalars(select(Evidence).where(Evidence.id.in_(why["evidence_ids"]))).all()
        assert all(e.verified and e.user_id == owner for e in cited) and fact
        assert why["answer"] and "source_ids" not in why
        visa = got["Do you require visa sponsorship to work here?"]
        assert visa["status"] == "NEEDS_INPUT" and visa["answer"] is None
        notice = got[NOTICE]
        assert notice["status"] == "LIBRARY" and notice["answer"] == "One month" and notice["kind"] == "FACT"
        with SessionLocal() as db:
            assert db.scalar(select(KnowledgeAnswer).where(KnowledgeAnswer.question.like("Why do you want%"))) \
                is None, "a draft is never saved as an answer"

        view = client.get(f"/api/jobs/{job_id}/insights").json()["application_form"]
        assert not view["pending"] and len(view["from_form"]) == 3
        assert client.post(f"/api/jobs/{job_id}/insights/application-questions",
                           json={"questions": [NOTICE]}).json()["added"] == 0, "the same question is not added twice"
    finally:
        _unseed()


def test_removing_a_pasted_question_removes_its_draft(owner, signed_in):
    job_id = new_job(owner)
    run_insights()
    client = signed_in(owner)
    client.post(f"/api/jobs/{job_id}/insights/application-questions",
                json={"questions": ["Tell us about a system you scaled."]})
    run_insights()
    qid = insight_of(job_id).application_form_input[0]["id"]
    assert client.delete(f"/api/jobs/{job_id}/insights/application-questions/{qid}").status_code == 200
    insight = insight_of(job_id)
    assert insight.application_form_input == [] and not any(q["id"] == qid for q in insight.application_questions)
    assert client.delete(f"/api/jobs/{job_id}/insights/application-questions/{qid}").status_code == 404


def test_another_account_cannot_add_questions_to_a_job(owner, signed_in):
    job_id = new_job(owner)
    other = make_user(SessionLocal)
    assert signed_in(other).post(f"/api/jobs/{job_id}/insights/application-questions",
                                 json={"questions": ["Why do you want to join?"]}).status_code == 404


def test_questions_pasted_during_a_full_run_are_drafted_afterwards(owner, monkeypatch):
    job_id = new_job(owner)
    real = synthesis.run

    def run_then_paste(*args, **kwargs):
        out = real(*args, **kwargs)
        with SessionLocal() as db:  # the person pastes while the run is past reading the form questions
            insight = db.scalar(select(JobInsight).where(JobInsight.job_id == job_id))
            application_form.add_questions(insight, ["Describe a project you are proud of."], service.utcnow())
            db.commit()
        return out
    monkeypatch.setattr(synthesis, "run", run_then_paste)
    run_insights()
    assert tasks_of(job_id)[-1].scope == "APPLICATION_FORM" and tasks_of(job_id)[-1].status == "PENDING"
    monkeypatch.setattr(synthesis, "run", real)
    run_insights()
    assert "Describe a project you are proud of." in by_question(job_id)


# ───────────── predicted questions and the rules after the model ─────────────

def test_predicted_questions_follow_the_rules_whatever_the_model_says(owner, monkeypatch):
    fact = _seed(owner)
    try:
        def items(_user):
            return [
                {"form_question": "What are your salary expectations for this role?", "form_category": "OTHER",
                 "form_decision": "DRAFT", "form_answer": "Around 200k.", "evidence_ids": [fact]},
                {"form_question": "Describe your experience building reliable services.",
                 "form_category": "EXPERIENCE", "form_decision": "DRAFT",
                 "form_answer": "I built reliable Python services for payments.", "evidence_ids": [fact],
                 "likely_because": "The posting centres on reliable services."},
                {"form_question": "How large was the team you led?", "form_category": "LEADERSHIP",
                 "form_decision": "DRAFT", "form_answer": "I led 4173 engineers.", "evidence_ids": [fact]},
                {"form_question": "Tell us about your Kafka work.", "form_category": "TECHNICAL",
                 "form_decision": "DRAFT", "form_answer": "Kafka pipelines.", "evidence_ids": [987654321]},
                {"form_question": "What is your greatest strength?", "form_category": "BEHAVIORAL",
                 "form_decision": "DRAFT", "form_answer": "Calm under pressure."},
            ]
        _crafted(monkeypatch, items)
        job_id = new_job(owner)
        run_insights()
        got = by_question(job_id)
        salary = got["What are your salary expectations for this role?"]
        assert salary["status"] == "NEEDS_INPUT" and salary["answer"] is None, "pay is never drafted"
        good = got["Describe your experience building reliable services."]
        assert good["status"] == "DRAFTED" and good["origin"] == "PREDICTED" and good["rationale"]
        metric = got["How large was the team you led?"]
        assert metric["status"] == "NEEDS_INPUT" and "4173" in metric["reason"], "an invented number withholds it"
        assert "Tell us about your Kafka work." not in got, "an item citing an unknown id is dropped alone"
        uncited = got["What is your greatest strength?"]
        assert uncited["status"] == "NEEDS_INPUT", "a factual draft needs evidence"
        assert insight_of(job_id).prompt_versions["application_form"] == "v4"
    finally:
        _unseed()


def test_a_run_without_the_model_keeps_the_last_drafts(owner, monkeypatch):
    _seed(owner)
    try:
        job_id = new_job(owner)
        run_insights()
        before = insight_of(job_id).application_questions
        assert before, "the mock predicts at least one question"
        monkeypatch.setattr(synthesis, "run", lambda *a, **k: synthesis.Synthesis(errors=["model down"]))
        refresh(job_id)
        run_insights()
        assert insight_of(job_id).application_questions == before
    finally:
        _unseed()


# ───────────── the candidate's resume, profile and per-question retrieval ─────────────

LEADERSHIP = "Tell us about a time you led a team of engineers."
RESUMES = __import__("tests.rag_support", fromlist=["DATASET"]).DATASET["resumes"]


def _candidate(user_id: int) -> dict[str, int]:
    """A fresh account with the backend resume (active, experience verified), the data resume, a headline and a
    work authorization a draft must never see."""
    from app.services import resume_verification
    from app.services.user_settings import profile_for

    with SessionLocal() as db:
        db.info["user_id"] = user_id
        profile = profile_for(db)
        profile.headline, profile.work_authorization = "Backend engineer", "Needs sponsorship (secret-ish)"
        ids = {}
        for key in ("backend", "data"):
            resume = resume_verification.record_version(db, profile, RESUMES[key]["text"], name=key,
                                                        activate=key == "backend")
            resume_verification.verify_section(db, resume, "experience")
            resume_verification.verify_section(db, resume, "education")
            ids[key] = resume.id
        db.commit()
        return ids


def _evidence_id(user_id: int, claim_start: str) -> int:
    with SessionLocal() as db:
        return db.scalar(select(Evidence.id).where(Evidence.user_id == user_id, Evidence.claim.like(f"{claim_start}%")))


def test_the_form_context_carries_the_jobs_resume_and_retrieves_for_each_question():
    from app.models import Job
    from app.services.context_builder import build_application_form_context
    from app.services.user_settings import profile_for

    user = make_user(SessionLocal)
    _candidate(user)
    job_id = new_job(user, approve=False)
    led = _evidence_id(user, "Led an on-call rotation")
    with SessionLocal() as db:
        db.info["user_id"] = user
        form = build_application_form_context(db, profile_for(db), db.get(Job, job_id),
                                              [{"ref": "F1", "question": LEADERSHIP}])
    ctx = form.context
    assert ctx.candidate.get("headline") == "Backend engineer"
    assert "work_authorization" not in ctx.candidate, "only the person answers work authorization"
    assert {r["company"] for r in ctx.relevant_experience} == {"Acme", "Globex"}, "the job's (active) resume"
    assert led in form.questions["F1"]["evidence_ids"], "the question finds its own evidence"
    assert led in ctx.evidence_ids and form.evidence_labels[led].startswith("Led an on-call")
    assert ctx.stats.selected_context_tokens > 0


def test_a_pasted_question_is_drafted_from_the_resume_and_its_own_retrieval(monkeypatch):
    user = make_user(SessionLocal)
    _candidate(user)
    job_id = new_job(user)
    run_insights()
    led = _evidence_id(user, "Led an on-call rotation")
    seen = {}

    def items(user_text):
        seen["prompt"] = user_text
        return [{"form_ref": "F1", "form_question": LEADERSHIP, "form_category": "BEHAVIORAL",
                 "form_decision": "DRAFT", "evidence_ids": [led],
                 "form_answer": "At Globex I led an on-call rotation of 6 engineers and wrote the incident runbooks."}]
    _crafted(monkeypatch, items)
    with SessionLocal() as db:
        db.info["user_id"] = user
        insight = db.scalar(select(JobInsight).where(JobInsight.job_id == job_id))
        application_form.add_questions(insight, [LEADERSHIP], insight.updated_at or insight.created_at)
        from app.services.job_insights import tasks as insight_tasks
        insight_tasks.enqueue(db, insight.job, reason=insight_tasks.USER_REQUEST, force=True,
                              scope=insight_tasks.APPLICATION_FORM)
        db.commit()
    run_insights()
    prompt = seen["prompt"]
    assert "CANDIDATE:" in prompt and "RESUME:" in prompt and "Globex" in prompt
    assert "RETRIEVED FOR EACH QUESTION:" in prompt and str(led) in prompt
    assert "Needs sponsorship" not in prompt, "work authorization never reaches the model"
    got = by_question(job_id)[LEADERSHIP]
    assert got["status"] == "DRAFTED" and got["evidence_ids"] == [led]
    assert got["evidence"] == ["Led an on-call rotation of 6 engineers and wrote the incident runbooks"]


@pytest.mark.parametrize("mode", ["off", "shadow", "on"])
def test_another_resumes_facts_are_never_retrieved_in_any_mode(monkeypatch, mode):
    """Scope before scoring for this new path in every mode, not only with retrieval on (Codex review, PR #89)."""
    from app.models import Job
    from app.services.context_builder import build_application_form_context
    from app.services.user_settings import profile_for
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "rag_retrieval_mode", mode)
    monkeypatch.setattr(get_settings(), "rag_vector_enabled", False)
    user = make_user(SessionLocal)
    _candidate(user)
    job_id = new_job(user, approve=False)
    churn = _evidence_id(user, "Trained churn prediction")  # the data resume's fact
    assert churn, "the data resume has its own verified evidence"
    with SessionLocal() as db:
        db.info["user_id"] = user
        form = build_application_form_context(db, profile_for(db), db.get(Job, job_id), [
            {"ref": "F1", "question": "Describe a machine learning model you trained, such as churn prediction."}])
    assert churn not in form.context.evidence_ids
    assert churn not in form.questions["F1"]["evidence_ids"]


def _paste_and_run(user, job_id, questions, items, monkeypatch):
    """Paste `questions`, answer the form prompt with `items(prompt_user)`, run the task. Returns what the model was
    sent: {"prompt": user text, "stats": the call's context_stats}."""
    from app.services.job_insights import tasks as insight_tasks

    seen = {}
    real = LLMClient.generate_prompt

    def generate(self, prompt, call=None):
        if prompt.name != "application_form":
            return real(self, prompt, call)
        seen.update(prompt=prompt.user, stats=call.context_stats)
        data = {"form_questions": items(prompt.user)}
        return LLMResult(data, "mock", "mock", validated=call.validate(data))
    monkeypatch.setattr(LLMClient, "generate_prompt", generate)
    with SessionLocal() as db:
        db.info["user_id"] = user
        insight = db.scalar(select(JobInsight).where(JobInsight.job_id == job_id))
        application_form.add_questions(insight, questions, insight.updated_at or insight.created_at)
        insight_tasks.enqueue(db, insight.job, reason=insight_tasks.USER_REQUEST, force=True,
                              scope=insight_tasks.APPLICATION_FORM)
        db.commit()
    run_insights()
    return seen


def test_a_degree_or_skills_question_can_rest_on_a_cited_resume_fact(monkeypatch):
    """Verified resume facts that are not bullets carry a RESUME ref a draft may cite; an unknown ref drops the item."""
    user = make_user(SessionLocal)
    _candidate(user)
    job_id = new_job(user)
    run_insights()
    study, made_up = "What did you study at university?", "Which certifications do you hold today?"
    seen = _paste_and_run(user, job_id, [study, made_up], lambda _u: [
        {"form_ref": "F1", "form_question": study, "form_category": "EXPERIENCE", "form_decision": "DRAFT",
         "form_answer": "I studied Computer Science at IIT Delhi.", "form_resume_refs": ["EDU1"]},
        {"form_ref": "F2", "form_question": made_up, "form_category": "EXPERIENCE", "form_decision": "DRAFT",
         "form_answer": "I hold an AWS certification.", "form_resume_refs": ["CERT9"]}], monkeypatch)
    assert '"ref": "EDU1"' in seen["prompt"] and '"ref": "ROLE1"' in seen["prompt"]
    got = by_question(job_id)
    assert got[study]["status"] == "DRAFTED" and got[study]["resume_refs"] == ["EDU1"]
    assert "IIT Delhi" in got[study]["resume_facts"][0]
    assert got[made_up]["status"] == "NEEDS_INPUT", "a ref the prompt never showed is not support"


def test_per_question_hits_stay_inside_the_capped_prompt(monkeypatch):
    """With more hits than the cap, every question keeps its best hit, the per-question map only points at evidence
    the prompt carries, and citing evidence the cap left out is refused."""
    from app.services.context_builder import FormContext, TaskContext

    user = make_user(SessionLocal)
    job_id = new_job(user)
    run_insights()
    with SessionLocal() as db:
        rows = [Evidence(user_id=user, company="Northwind Labs", role="Engineer", context=MARK, verified=True,
                         claim=f"Verified fact number {n} about payments") for n in range(6)]
        db.add_all(rows)
        db.commit()
        ids = [r.id for r in rows]
    q1, q2 = "Describe your payments experience in detail.", "Describe your reliability experience please."
    fake = FormContext(TaskContext(task="application_form", candidate={}, resume={},
                                   relevant_evidence=[{"id": i, "claim": f"fact {i}"} for i in ids]),
                       {"F1": {"evidence_ids": ids[:3], "knowledge_ids": []},
                        "F2": {"evidence_ids": ids[3:], "knowledge_ids": []}},
                       {i: f"fact {i}" for i in ids}, scope=set(ids))
    monkeypatch.setattr(application_form, "candidate_context", lambda *a, **k: fake)
    monkeypatch.setattr(application_form, "MAX_EVIDENCE", 2)
    try:
        seen = _paste_and_run(user, job_id, [q1, q2], lambda _u: [
            {"form_ref": "F1", "form_question": q1, "form_category": "EXPERIENCE", "form_decision": "DRAFT",
             "form_answer": "Verified fact number 0 about payments.", "evidence_ids": [ids[0]]},
            {"form_ref": "F2", "form_question": q2, "form_category": "EXPERIENCE", "form_decision": "DRAFT",
             "form_answer": "Verified fact number 4 about payments.", "evidence_ids": [ids[4]]}], monkeypatch)
        retrieved = __import__("json").loads(
            seen["prompt"].split("RETRIEVED FOR EACH QUESTION:\n", 1)[1].split("\n", 1)[0])
        assert retrieved == {"F1": {"evidence_ids": [ids[0]], "knowledge_ids": []},
                             "F2": {"evidence_ids": [ids[3]], "knowledge_ids": []}}, "each question's best hit"
        got = by_question(job_id)
        assert got[q1]["status"] == "DRAFTED"
        assert got[q2]["status"] == "NEEDS_INPUT", "evidence the cap left out cannot be cited"
    finally:
        _unseed()


def test_an_unverified_summary_never_becomes_a_verified_background_flag():
    from app.models import Job
    from app.services import resume_verification
    from app.services.context_builder import build_application_form_context
    from app.services.user_settings import profile_for

    user = make_user(SessionLocal)
    text = ("# Sam Doe\n\n## Summary\nMachine learning leader who managed teams.\n\n## Experience\n"
            "### Engineer | Acme | Pune | Jan 2020 - Dec 2022\n- Designed PostgreSQL schemas for the billing ledger\n")
    with SessionLocal() as db:
        db.info["user_id"] = user
        resume = resume_verification.record_version(db, profile_for(db), text, name="r", activate=True)
        resume_verification.verify_section(db, resume, "experience")  # the summary stays unverified
        db.commit()
    job_id = new_job(user, approve=False)
    with SessionLocal() as db:
        db.info["user_id"] = user
        form = build_application_form_context(db, profile_for(db), db.get(Job, job_id), [])
    background = form.context.candidate.get("verified_background") or {}
    assert not background.get("leadership_experience") and not background.get("ai_experience")
    assert "summary" not in form.context.candidate and "SUMMARY" not in form.resume_facts


def test_the_request_row_measures_the_prompt_actually_sent(monkeypatch):
    from app.services.llm_telemetry import estimate_tokens

    user = make_user(SessionLocal)
    _candidate(user)
    job_id = new_job(user)
    run_insights()
    seen = _paste_and_run(user, job_id, [LEADERSHIP], lambda _u: [], monkeypatch)
    stats = seen["stats"]
    assert stats["selected_context_tokens"] == estimate_tokens(seen["prompt"])
    assert stats["full_available_context_tokens"] >= stats["selected_context_tokens"]
    assert stats["context_builder_version"]


# ───────────── units ─────────────

def test_the_contract_never_keeps_an_answer_outside_a_draft():
    out = ApplicationFormPrepResult.model_validate({"form_questions": [
        {"form_question": "Why do you want this job?", "form_decision": "DRAFT"},
        {"form_question": "Are you willing to relocate?", "form_decision": "NEEDS_INPUT", "form_answer": "Yes"}]})
    assert [(q.form_decision, q.form_answer) for q in out.form_questions] == [("NEEDS_INPUT", None),
                                                                                ("NEEDS_INPUT", None)]


def test_personal_questions_are_recognised_without_catching_ordinary_ones():
    assert application_form.personal("Do you now or in the future require sponsorship?")
    assert application_form.personal("What is your expected salary?")
    assert application_form.personal("When can you start?")
    assert application_form.personal("Anything", "DEMOGRAPHIC")
    assert not application_form.personal("How did you address a conflict in your team?")
    assert not application_form.personal("Describe a high availability system you built.")


def test_provenance_drops_a_draft_that_claims_a_research_source():
    sections = {"application_questions": [
        {"id": "f1", "status": "DRAFTED", "kind": "GENERATED", "answer": "x", "source_ids": [1]},
        {"id": "f2", "status": "DRAFTED", "kind": "GENERATED", "answer": "y"},
        {"id": "f3", "status": "NEEDS_INPUT", "kind": "UNKNOWN", "answer": "z"}]}
    out, report = provenance.enforce(sections, [])
    assert [q["id"] for q in out["application_questions"]] == ["f2"]


def test_a_reviewed_answer_is_saved_to_the_library_from_the_insights_tab(owner, signed_in, monkeypatch):
    from app.services import application_execution

    woken = []
    monkeypatch.setattr(application_execution, "wake_waiting", lambda db, uid: woken.append(uid) or 0)
    job_id = new_job(owner)
    run_insights()
    client = signed_in(owner)
    why, visa = "Tell us why you want to join the payments team here.", "Do you require visa sponsorship to work here?"
    client.post(f"/api/jobs/{job_id}/insights/application-questions", json={"questions": [why, visa]})
    run_insights()
    try:
        got = by_question(job_id)
        qid = got[why]["id"]
        url = f"/api/jobs/{job_id}/insights/application-questions/{qid}/save"
        assert client.post(url, json={"answer": "My own words."}).status_code == 422, "reviewed must be confirmed"
        assert client.post(url, json={"answer": "   ", "reviewed": True}).status_code == 422

        out = client.post(url, json={"answer": "My own words.", "reviewed": True}).json()
        item = next(q for q in out["application_form"]["from_form"] if q["id"] == qid)
        assert item["status"] == "LIBRARY" and item["kind"] == "FACT" and item["answer"] == "My own words."
        assert item["answer_source"] == f"knowledge_answer:{out['knowledge_answer_id']}"
        assert woken == [owner], "an application run waiting for this answer is woken, as from the library"
        with SessionLocal() as db:
            row = db.get(KnowledgeAnswer, out["knowledge_answer_id"])
            assert (row.user_id, row.answer, row.verified, row.sensitive, row.source_job_id) == \
                (owner, "My own words.", True, False, job_id)
        assert client.post(url, json={"answer": "Again", "reviewed": True}).status_code == 404, \
            "a library answer is edited in the library"

        vid = by_question(job_id)[visa]["id"]
        out = client.post(f"/api/jobs/{job_id}/insights/application-questions/{vid}/save",
                          json={"answer": "No", "reviewed": True}).json()
        item = next(q for q in out["application_form"]["from_form"] if q["id"] == vid)
        assert out["sensitive"] and item["status"] == "NEEDS_INPUT" and item["answer"] is None, \
            "a personal answer is saved sensitive and never shown on the item"
        other = make_user(SessionLocal)
        assert signed_in(other).post(f"/api/jobs/{job_id}/insights/application-questions/{qid}/save",
                                     json={"answer": "x", "reviewed": True}).status_code == 404
    finally:
        with SessionLocal() as db:
            db.execute(delete(KnowledgeAnswer).where(KnowledgeAnswer.question.in_([why, visa])))
            db.commit()


# ───────────── drafts from the closest experience ─────────────

FULLSTACK = "Tell us about a recent project where you worked across both the frontend and backend."


def test_a_question_waiting_in_the_library_still_gets_a_draft_and_saving_answers_it(owner, signed_in, monkeypatch):
    fact = _seed(owner)
    with SessionLocal() as db:  # asked once before and never answered: waiting in the Answer library
        db.add(KnowledgeAnswer(user_id=owner, question=FULLSTACK, question_key=question_key(FULLSTACK),
                               category="test_form", verified=False))
        db.commit()
    try:
        _crafted(monkeypatch, lambda _user: [
            {"form_ref": "F1", "form_question": FULLSTACK, "form_category": "EXPERIENCE", "form_decision": "DRAFT",
             "form_basis": "CLOSEST", "form_answer": "I built reliable Python services for payments.",
             "evidence_ids": [fact], "form_note": "From the payments services work; check it covers a frontend."}])
        job_id = new_job(owner)
        run_insights()
        client = signed_in(owner)
        client.post(f"/api/jobs/{job_id}/insights/application-questions", json={"questions": [FULLSTACK]})
        run_insights()
        item = by_question(job_id)[FULLSTACK]
        assert item["status"] == "DRAFTED" and item["answer"], "a waiting question is drafted, not left waiting"
        assert item["label"] == application_form.CLOSEST_LABEL and item["basis"] == "CLOSEST"
        assert "check it covers a frontend" in item["reason"] and application_form.WAITING in item["reason"]

        out = client.post(f"/api/jobs/{job_id}/insights/application-questions/{item['id']}/save",
                          json={"answer": "My reviewed words.", "reviewed": True}).json()
        with SessionLocal() as db:
            rows = db.scalars(select(KnowledgeAnswer).where(KnowledgeAnswer.question == FULLSTACK)).all()
        assert [(r.id, r.answer, r.verified) for r in rows] == [(out["knowledge_answer_id"], "My reviewed words.",
                                                                  True)], "the waiting row is answered, not copied"
    finally:
        _unseed()


def test_a_closest_draft_without_a_citation_is_still_withheld(owner, signed_in, monkeypatch):
    _crafted(monkeypatch, lambda _user: [
        {"form_ref": "F1", "form_question": FULLSTACK, "form_category": "EXPERIENCE", "form_decision": "DRAFT",
         "form_basis": "CLOSEST", "form_answer": "I built a full-stack dashboard.", "form_note": "Adapted."}])
    job_id = new_job(owner)
    run_insights()
    signed_in(owner).post(f"/api/jobs/{job_id}/insights/application-questions", json={"questions": [FULLSTACK]})
    run_insights()
    item = by_question(job_id)[FULLSTACK]
    assert item["status"] == "NEEDS_INPUT" and item["answer"] is None


def test_draft_with_ai_queues_one_question_and_refuses_a_personal_one(owner, signed_in):
    job_id = new_job(owner)
    run_insights()
    client = signed_in(owner)
    visa = "Do you require visa sponsorship to work here?"
    client.post(f"/api/jobs/{job_id}/insights/application-questions", json={"questions": [FULLSTACK, visa]})
    run_insights()
    got = by_question(job_id)
    url = f"/api/jobs/{job_id}/insights/application-questions/{{}}/draft"
    out = client.post(url.format(got[FULLSTACK]["id"])).json()
    drafting = next(q for q in out["application_form"]["from_form"] if q["id"] == got[FULLSTACK]["id"])
    assert drafting["status"] == "PENDING" and out["application_form"]["pending"]
    assert tasks_of(job_id)[-1].scope == "APPLICATION_FORM" and tasks_of(job_id)[-1].status == "PENDING"
    assert client.post(url.format(got[visa]["id"])).status_code == 422, "a personal question is never drafted"
    assert client.post(url.format("fnope")).status_code == 404
    assert signed_in(make_user(SessionLocal)).post(url.format(got[FULLSTACK]["id"])).status_code == 404
    assert run_insights() == ["completed"] and FULLSTACK in by_question(job_id)


def test_the_contract_reads_form_basis_leniently():
    out = ApplicationFormPrepResult.model_validate({"form_questions": [
        {"form_question": "Describe a full-stack project.", "form_decision": "DRAFT", "form_answer": "x",
         "form_basis": "closest"},
        {"form_question": "Describe a backend project.", "form_decision": "DRAFT", "form_answer": "y",
         "form_basis": "nonsense"}]})
    assert [q.form_basis for q in out.form_questions] == ["CLOSEST", "DIRECT"]
