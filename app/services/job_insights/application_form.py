"""Application-form preparation on Job Insights: what to write for the questions an ATS form asks.

    pasted questions (application_form_input) ─┐
                                               ├→ before the model: the person's verified library answer wins,
    a full research run predicts more ─────────┘   a personal question (visa, salary, notice, …) needs the person
          → the candidate's context (context_builder.build_application_form_context): the job's own resume (verified
            roles, bullets with their evidence ids, projects, skills, education), the profile's headline and verified
            summary, and for each pasted question its own retrieval — verified evidence (RAG when it is on) and
            similar verified saved answers — inside the job's resume scope
          → one APPLICATION_ANSWER call (prompt `application_form`, through the LLM gateway) with the structured JD, the
            fit analysis, that context, verified non-sensitive saved answers and a company summary
          → after the model, item by item: ids it was not shown drop that item; a personal question loses its draft;
            a draft must pass the screening-answer grounding checks (tailoring_results.check_prose) or needs input
          → job_insights.application_questions (merged with the last good drafts)

A draft is a suggestion. Nothing here writes a KnowledgeAnswer: the person saves an answer in the Answer library, and
marks it verified there. Runs inside a claimed research task (a full run, or an APPLICATION_FORM task queued when the
person pastes questions), never in a request.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import Job, JobInsight, KnowledgeAnswer

FULL, FORM_ONLY = "FULL", "APPLICATION_FORM"
FORM, PREDICTED = "FORM", "PREDICTED"
LIBRARY, DRAFTED, NEEDS_INPUT = "LIBRARY", "DRAFTED", "NEEDS_INPUT"
MAX_FORM_QUESTIONS = 20
MAX_QUESTION_CHARS = 400
MAX_KNOWN_ANSWERS = 12
# Saved-answer categories only the person may answer (app/static/knowledge.js KNOWLEDGE_CATEGORIES): never shown to
# the model, even verified.
PERSONAL_KNOWLEDGE = ("work_authorization", "compensation", "notice_period", "relocation", "demographic")
# Questions only the person may answer, whatever the model says: work authorization, pay, availability, relocation,
# demographic, legal and contact details. Read on the question text, before and after the model.
PERSONAL_RE = re.compile(
    r"\b(visas?|sponsor\w*|work authori[sz]\w*|authori[sz]ed to work|legally (?:able|eligible|permitted)|"
    r"right to work|work permits?|employment pass|citizen\w*|permanent resident\w*|green card|h-?1b|"
    r"salary|salaries|compensation|pay (?:range|expectations?)|expected pay|remuneration|ctc|hourly rate|"
    r"notice period|start date|earliest start|when can you start|available to start|your availability|"
    r"relocat\w*|commute|willing to travel|"
    r"gender|pronouns?|sexual orientation|race|racial|ethnic\w*|disabilit\w*|veteran|date of birth|your age|"
    r"criminal|convict\w*|background check|"
    r"phone number|mobile number|e-?mail address|home address|mailing address|linkedin|github|portfolio)\b",
    re.IGNORECASE)
PERSONAL_CATEGORIES = ("WORK_AUTHORIZATION", "COMPENSATION", "AVAILABILITY", "RELOCATION", "DEMOGRAPHIC", "PERSONAL")


def question_id(origin: str, question: str) -> str:
    """Stable across runs: the same question keeps its id, so its draft never moves to another question."""
    digest = hashlib.sha1(f"{origin}|{' '.join(question.lower().split())}".encode()).hexdigest()[:12]
    return f"f{digest}"


def clean_questions(lines: list[str]) -> list[str]:
    """Pasted questions as the person meant them: trimmed, list markers removed, short fragments dropped."""
    out = []
    for line in lines:
        text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line or "").strip()
        if len(text) >= 8:
            out.append(" ".join(text.split())[:MAX_QUESTION_CHARS])
    return out


def add_questions(insight: JobInsight, questions: list[str], now: datetime) -> list[dict[str, Any]]:
    """Add pasted questions to the person's own list (deduplicated, bounded). Returns the ones that were new."""
    current = list(insight.application_form_input or [])
    known = {q["id"] for q in current}
    added = []
    for text in clean_questions(questions):
        qid = question_id(FORM, text)
        if qid in known:
            continue
        if len(current) >= MAX_FORM_QUESTIONS:
            raise ValueError(f"A job keeps at most {MAX_FORM_QUESTIONS} form questions; remove one first")
        item = {"id": qid, "question": text, "added_at": now.isoformat()}
        current.append(item)
        added.append(item)
        known.add(qid)
    insight.application_form_input = current
    return added


def remove_question(insight: JobInsight, qid: str) -> bool:
    current = list(insight.application_form_input or [])
    kept = [q for q in current if q.get("id") != qid]
    if len(kept) == len(current):
        return False
    insight.application_form_input = kept
    insight.application_questions = [q for q in insight.application_questions or [] if q.get("id") != qid]
    return True


def redraft(insight: JobInsight, qid: str) -> None:
    """Ask the model again for one pasted question the person is waiting on: its item is cleared, so it shows as
    drafting and the next APPLICATION_FORM run (or `service.follow_up`) drafts it. A personal question, or one already
    answered from the person's library, is refused: there is nothing for a model to write."""
    pasted = next((q for q in insight.application_form_input or [] if q.get("id") == qid), None)
    if pasted is None:
        raise LookupError("Question not found")
    item = next((q for q in insight.application_questions or [] if q.get("id") == qid), None)
    if personal(pasted["question"], (item or {}).get("category")):
        raise ValueError("Only you can answer this question; it is never drafted")
    if item is not None and item.get("status") == LIBRARY:
        raise ValueError("This question already has your saved answer")
    insight.application_questions = [q for q in insight.application_questions or [] if q.get("id") != qid]


MAX_ANSWER_CHARS = 8000


def save_reviewed(db: Session, insight: JobInsight, job: Job, qid: str, answer: str,
                  sensitive: bool = False) -> KnowledgeAnswer:
    """The person read this question's answer (a draft they may have edited, or one they typed) and saves it to their
    Answer library as verified, from the Insights tab. The item then shows as their saved answer.

    The answer is the person's own words: nothing here calls a model or keeps an unreviewed draft. A personal
    question (or one they mark sensitive) is saved sensitive: it is confirmed on each form, so its text is not shown
    on the item. An existing saved row for the same question is updated (their earlier request is answered), never
    duplicated. Application runs waiting for an answer are woken, as when it is answered in the library."""
    from app.services import knowledge

    answer = (answer or "").strip()
    if not answer:
        raise ValueError("Write the answer before saving it")
    if len(answer) > MAX_ANSWER_CHARS:
        raise ValueError(f"An answer is at most {MAX_ANSWER_CHARS} characters")
    items = list(insight.application_questions or [])
    index = next((i for i, q in enumerate(items) if q.get("id") == qid), None)
    if index is None or items[index].get("status") not in (DRAFTED, NEEDS_INPUT):
        raise LookupError("Question not found")
    item = items[index]
    question = item["question"]
    sensitive = bool(sensitive) or item.get("category") == "PERSONAL" or personal(question)
    row = knowledge.find_same(db, question)
    if row is None:
        row = KnowledgeAnswer(user_id=job.user_id, question=question,
                              question_key=knowledge.question_key(question), category="general",
                              answer_type="text", source_job_id=job.id)
        db.add(row)
    row.answer, row.verified, row.sensitive = answer, True, bool(row.sensitive) or sensitive
    if row.source_job_id is None or knowledge.job_specific(question):
        row.source_job_id = job.id  # a "why this company" answer is now this job's (knowledge.fits_job)
    db.flush()
    from app.services import application_execution

    application_execution.wake_waiting(db, row.user_id)  # as in the library: a run waiting for this answer goes on
    if row.sensitive:
        saved = _item(qid, question, item.get("origin") or FORM, NEEDS_INPUT, category="PERSONAL",
                      knowledge_answer_id=row.id,
                      reason="Saved in your Answer library as sensitive; confirm it on the form each time")
    else:
        saved = _item(qid, question, item.get("origin") or FORM, LIBRARY, answer=row.answer,
                      answer_source=f"knowledge_answer:{row.id}", knowledge_answer_id=row.id,
                      category=item.get("category"), requirement_ids=item.get("requirement_ids"),
                      requirements=item.get("requirements"))
    items[index] = saved
    insight.application_questions = items
    return row


def personal(question: str, category: str | None = None) -> bool:
    return bool(PERSONAL_RE.search(question or "")) or (category or "") in PERSONAL_CATEGORIES


def undrafted(insight: JobInsight | None) -> list[str]:
    """Pasted questions no run has handled yet: their ids."""
    if insight is None:
        return []
    handled = {q.get("id") for q in insight.application_questions or []}
    return [q["id"] for q in insight.application_form_input or [] if q.get("id") not in handled]


def current(items: list[dict[str, Any]], form_input: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The section as it is saved: pasted questions the person removed meanwhile are left out."""
    pasted = {q.get("id") for q in form_input or []}
    return [i for i in items if i.get("origin") != FORM or i.get("id") in pasted]


# ───────────── items ─────────────

def _item(qid: str, question: str, origin: str, status: str, **values: Any) -> dict[str, Any]:
    kind = {LIBRARY: "FACT", DRAFTED: "GENERATED"}.get(status, "UNKNOWN")
    label = values.get("label") or {LIBRARY: "Your saved answer",
                                    DRAFTED: "Draft — check and edit before you submit"}.get(status, "Needs your answer")
    return {"id": qid, "question": question, "origin": origin, "status": status, "category": values.get("category"),
            "answer": values.get("answer"), "answer_source": values.get("answer_source"),
            "knowledge_answer_id": values.get("knowledge_answer_id"),
            "evidence_ids": values.get("evidence_ids") or [], "evidence": values.get("evidence") or [],
            "requirement_ids": values.get("requirement_ids") or [], "requirements": values.get("requirements") or [],
            "knowledge_ids": values.get("knowledge_ids") or [], "resume_refs": values.get("resume_refs") or [],
            "resume_facts": values.get("resume_facts") or [], "rationale": values.get("rationale") or None,
            "reason": values.get("reason") or None, "kind": kind, "label": label,
            "basis": values.get("basis") or None}


WAITING = "Also waiting for your answer in the Answer library; saving this answers it there"
CLOSEST_LABEL = "Draft from your closest experience — check it fits before you save"


def before_model(db: Session, qid: str, question: str, origin: str,
                 pending: set[str] | None = None, job_id: int | None = None) -> dict[str, Any] | None:
    """What the person's own data settles without a model: a verified saved answer (or a contact detail from the
    profile), a sensitive saved answer, or a personal question. A question already waiting in the Answer library is
    not settled: it still gets a draft (its id goes into `pending`), and saving that draft answers the waiting row."""
    from app.services import knowledge

    found = knowledge.search(db, question, limit=3)
    status = found["status"]
    if status == "ANSWERED":
        source = found.get("answer_source") or ""
        knowledge_id = int(source.split(":", 1)[1]) if source.startswith("knowledge_answer:") else None
        row = db.get(KnowledgeAnswer, knowledge_id) if knowledge_id else None
        if row is not None and job_id is not None and not knowledge.fits_job(row, job_id):
            if pending is not None:
                pending.add(qid)  # drafted for this job; saving the draft makes the saved answer this job's
            return None
        return _item(qid, question, origin, LIBRARY, answer=found["answer"], answer_source=source,
                     knowledge_answer_id=knowledge_id,
                     reason=None if knowledge_id else "From your profile")
    if status == "NEEDS_CONFIRMATION":
        return _item(qid, question, origin, NEEDS_INPUT, category="PERSONAL",
                     reason="You saved a sensitive answer to this; confirm it on the form each time")
    if personal(question):
        return _item(qid, question, origin, NEEDS_INPUT, category="PERSONAL",
                     reason="Only you can answer this (work authorization, pay, availability, relocation or "
                            "personal details). Save your answer in the Answer library to reuse it.")
    if status == "PENDING_HUMAN" and pending is not None:
        pending.add(qid)
    return None


def known_answers(db: Session, user_id: int) -> list[dict[str, Any]]:
    """Verified, non-sensitive saved answers outside the personal categories, most used first, with their ids."""
    rows = db.scalars(select(KnowledgeAnswer).where(
        KnowledgeAnswer.user_id == user_id, KnowledgeAnswer.verified.is_(True), KnowledgeAnswer.sensitive.is_(False),
        KnowledgeAnswer.category.not_in(PERSONAL_KNOWLEDGE))
        .order_by(desc(KnowledgeAnswer.use_count), KnowledgeAnswer.id).limit(MAX_KNOWN_ANSWERS * 3)).all()
    from app.services.knowledge import job_specific

    # A "why this company" answer is about one employer: the model never sees another job's as material.
    out = [{"id": r.id, "question": r.question[:300], "answer": r.answer[:800]} for r in rows
           if (r.answer or "").strip() and not personal(r.question) and not job_specific(r.question)]
    return out[:MAX_KNOWN_ANSWERS]


def company_summary(insight: JobInsight | None, synthesized: Any = None) -> dict[str, Any]:
    """A few lines about the company for motivation answers: this run's summary, else the stored one."""
    stored = (insight.company if insight is not None else None) or {}
    summary = getattr(synthesized, "company_summary", None) or (stored.get("summary") or {}).get("text")
    values = ([v.value for v in synthesized.official_values] if synthesized is not None else
              [v.get("value") for v in stored.get("official_values") or []])
    out = {"summary": summary, "official_values": [v for v in values if v][:8],
           "mission": [m.get("text") for m in stored.get("mission") or [] if m.get("text")][:2]}
    return {k: v for k, v in out.items() if v}


def screen(answer: Any, *, form_refs: set[str], requirement_ids: set[str], evidence_ids: set[int],
           knowledge_ids: set[int], dropped: list[str], resume_refs: set[str] | frozenset[str] = frozenset()) -> Any:
    """Item by item: an item citing an id it was not shown is dropped (and noted), never the whole answer."""
    keep = []
    for item in answer.form_questions:
        unknown = ([r for r in item.requirement_ids if r not in requirement_ids]
                   + [e for e in item.evidence_ids if e not in evidence_ids]
                   + [k for k in item.knowledge_ids if k not in knowledge_ids]
                   + [r for r in item.form_resume_refs if r not in resume_refs])
        if item.form_ref is not None and item.form_ref not in form_refs:
            dropped.append(f"form answer for an unknown form question {item.form_ref!r}")
        elif unknown:
            dropped.append(f"form answer citing unknown ids {unknown[:5]}: '{item.form_question[:60]}'")
        else:
            keep.append(item)
    answer.form_questions = keep
    return answer


def after_model(db: Session, job: Job, profile: Any, item: Any, *, qid: str, question: str, origin: str,
                requirement_labels: dict[str, str], evidence_labels: dict[int, str],
                resume_facts: dict[str, str] | None = None, waiting: bool = False) -> dict[str, Any]:
    """One model item as it is saved. A personal question never keeps a draft; a draft that states a number, a name
    or a work authorization the person's data does not support needs the person instead. A verified resume fact that
    is not a bullet (a role, a degree, the skills ... cited by its RESUME ref) supports a draft like evidence does.
    A CLOSEST draft (no fact answers the question exactly; it is adapted from the nearest verified experience) passes
    the same checks and is labelled so, with the model's note on what to check. `waiting`: the question is also
    waiting in the Answer library."""
    from app.services.tailoring_results import check_prose

    common = {"category": item.form_category, "requirement_ids": item.requirement_ids,
              "requirements": [requirement_labels.get(r, r) for r in item.requirement_ids],
              "rationale": item.likely_because}
    if personal(question, item.form_category):
        return _item(qid, question, origin, NEEDS_INPUT, **{**common, "category": "PERSONAL"},
                     reason="Only you can answer this (work authorization, pay, availability, relocation or personal "
                            "details). Save your answer in the Answer library to reuse it.")
    if item.form_decision != "DRAFT":
        return _item(qid, question, origin, NEEDS_INPUT, **common,
                     reason=WAITING if waiting else item.form_note or "Your saved data does not answer this")
    if (not item.evidence_ids and not item.knowledge_ids and not item.form_resume_refs
            and item.form_category != "MOTIVATION"):
        return _item(qid, question, origin, NEEDS_INPUT, **common,
                     reason="No verified evidence supports a draft; add evidence or answer it yourself")
    problems = check_prose(db, profile, job, {"draft": item.form_answer or ""}) if profile is not None else []
    if problems:
        return _item(qid, question, origin, NEEDS_INPUT, **common,
                     reason="The draft was withheld: " + "; ".join(p["message"] for p in problems[:2]))
    closest = item.form_basis == "CLOSEST"
    note = (item.form_note or "Nothing you saved answers this exactly; it was adapted from related experience.") \
        if closest else ""
    reason = " ".join(r for r in (note, WAITING if waiting else "") if r)
    return _item(qid, question, origin, DRAFTED, **common, answer=item.form_answer, evidence_ids=item.evidence_ids,
                 label=CLOSEST_LABEL if closest else None, basis="CLOSEST" if closest else None,
                 reason=reason or None,
                 evidence=[evidence_labels.get(e, "") for e in item.evidence_ids], knowledge_ids=item.knowledge_ids,
                 resume_refs=item.form_resume_refs,
                 resume_facts=[(resume_facts or {}).get(r, r) for r in item.form_resume_refs])


# ───────────── the candidate's context ─────────────

MAX_EVIDENCE = 36
MAX_KNOWLEDGE = 20


def candidate_context(db: Session, client: Any, profile: Any, job: Job, questions: list[dict[str, str]]) -> Any:
    """context_builder.build_application_form_context for this pass: the job's resume and profile, and per-question
    retrieval. None without a profile, without a model (nothing would read it) or when it cannot be built: the
    posting-level evidence of the research context still stands."""
    import logging

    from app.services.context_builder import build_application_form_context

    if profile is None or getattr(client, "offline", False):
        return None
    try:
        with db.begin_nested():
            return build_application_form_context(db, profile, job, questions)
    except Exception:  # noqa: BLE001  drafting goes on with the research context
        logging.getLogger("jobhunt.job_insights").warning("application form context for job %s failed", job.id,
                                                          exc_info=True)
        return None


def _round_robin(lists: list[list[int]]) -> list[int]:
    """Each question's best hit, then each one's second, ...: a cap shared by many questions keeps every question's
    best hits rather than the first questions' whole lists."""
    out, seen = [], set()
    for rank in range(max((len(ids) for ids in lists), default=0)):
        for ids in lists:
            if rank < len(ids) and ids[rank] not in seen:
                seen.add(ids[rank])
                out.append(ids[rank])
    return out


def _question_first(items: list[dict[str, Any]], ranked: list[int]) -> list[dict[str, Any]]:
    """`items` with the ids of `ranked` first, in that order."""
    order = {i: n for n, i in enumerate(ranked)}
    return sorted(items, key=lambda e: (order.get(e["id"], len(order)), e["id"]))


def bullet_evidence(resume: dict[str, Any]) -> set[int]:
    """Evidence ids the RESUME section carries on its bullets."""
    return {i for part in ("experience", "projects") for unit in resume.get(part) or []
            for b in unit.get("bullets") or [] for i in b.get("evidence_ids") or []}


def call_stats(form: Any, prompt: Any) -> dict[str, Any]:
    """Context size for the LLM request row, measured on the prompt actually sent (capped evidence and answers,
    resume, fit, company, form questions) against everything the candidate context could have carried. Posting-side
    figures are not measured here and stay unknown."""
    from app.services.context_builder import CONTEXT_BUILDER_VERSION, ContextStats
    from app.services.llm_telemetry import estimate_tokens

    selected = estimate_tokens(prompt.user)
    full = form.context.stats.full_available_context_tokens if form is not None else 0
    return {**ContextStats.measure(max(full, selected), selected).as_dict(),
            "context_builder_version": CONTEXT_BUILDER_VERSION}


def _merged(first: list[dict[str, Any]], second: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    """Both lists, one copy of each id, the first list's items first."""
    seen, out = set(), []
    for item in [*first, *second]:
        if item["id"] not in seen:
            seen.add(item["id"])
            out.append(item)
    return out[:cap]


# ───────────── one drafting pass ─────────────

def draft(db: Session, client: Any, job: Job, *, attempt: int, ctx: dict[str, Any], company: dict[str, Any],
          predict: bool, dropped: list[str], errors: list[str]) -> dict[str, Any]:
    """Draft the job's application form: every pasted question, and (in a full run) predicted ones. Never raises: a
    model failure is recorded in `errors` and the previous drafts stand. Returns {items, model_ran, predicted}, the
    section merged with the last good one."""
    from app.prompts import build_application_form_prompt
    from app.prompts.application_form import MAX_PREDICTED
    from app.prompts.contracts import ApplicationFormPrepResult
    from app.services.llm import LLMCall, LLMError
    from app.services.workflow import get_profile

    insight = db.scalar(select(JobInsight).where(JobInsight.job_id == job.id))
    previous = {i.get("id"): i for i in (insight.application_questions if insight else None) or []}
    pasted = list((insight.application_form_input if insight else None) or [])
    settled: dict[str, dict[str, Any]] = {}
    ask: list[dict[str, str]] = []
    waiting: set[str] = set()
    for n, q in enumerate(pasted, start=1):
        early = before_model(db, q["id"], q["question"], FORM, waiting, job.id)
        if early is not None:
            settled[q["id"]] = early
        else:
            ask.append({"ref": f"F{n}", "id": q["id"], "question": q["question"]})

    model_ran, predicted_items = False, None
    profile = get_profile(db)
    evidence_labels = {e["id"]: (e.get("claim") or "")[:160] for e in ctx["evidence"]}
    if ask or predict:
        form = candidate_context(db, client, profile, job, [{"ref": a["ref"], "question": a["question"]} for a in ask])
        hits = form.questions if form else {}
        research = ctx["evidence"] if form is None or form.scope is None else \
            [e for e in ctx["evidence"] if e["id"] in form.scope]  # the research context is account-wide when off
        evidence = _merged(_question_first(form.evidence, _round_robin([h["evidence_ids"] for h in hits.values()]))
                           if form else [], research, MAX_EVIDENCE)
        knowledge = _merged(_question_first(form.knowledge, _round_robin([h["knowledge_ids"] for h in hits.values()]))
                            if form else [], known_answers(db, job.user_id), MAX_KNOWLEDGE)
        resume = form.resume if form else {}
        # Only what the capped prompt carries may be cited or pointed at.
        shown_evidence = {e["id"] for e in evidence} | bullet_evidence(resume)
        shown_knowledge = {k["id"] for k in knowledge}
        retrieved = {ref: {"evidence_ids": [i for i in h["evidence_ids"] if i in shown_evidence],
                           "knowledge_ids": [i for i in h["knowledge_ids"] if i in shown_knowledge]}
                     for ref, h in hits.items()}
        retrieved = {ref: h for ref, h in retrieved.items() if any(h.values())}
        resume_facts = form.resume_facts if form else {}
        if form is not None:
            evidence_labels = {**form.evidence_labels, **evidence_labels}
        prompt = build_application_form_prompt(
            job={"title": job.title, "company": job.company, "location": job.location}, context=ctx["context"],
            fit_analysis=ctx["fit"], evidence=evidence, knowledge=knowledge, company=company,
            form_questions=[{"ref": a["ref"], "question": a["question"]} for a in ask],
            predict=MAX_PREDICTED if predict else 0,
            candidate=form.context.candidate if form else None, resume=resume or None, retrieved=retrieved or None)
        refs = {a["ref"]: a for a in ask}
        try:
            result = client.generate_prompt(prompt, LLMCall(
                workflow_type="job", entity_id=job.id, attempt=attempt,
                context_stats=call_stats(form, prompt) if form else None,
                validate=lambda data: screen(ApplicationFormPrepResult.model_validate(data), form_refs=set(refs),
                                             requirement_ids=set(ctx["requirements"]), evidence_ids=shown_evidence,
                                             knowledge_ids=shown_knowledge, resume_refs=set(resume_facts),
                                             dropped=dropped)))
            model_ran = True
        except (LLMError, ValueError) as exc:
            errors.append(f"Application form drafts failed: {type(exc).__name__}: {exc}"[:300])
            result = None
        if result is not None:
            predicted_items = []
            seen = {q["id"] for q in pasted}  # a prediction that repeats a pasted question is not shown twice
            for item in result.validated.form_questions:
                if item.form_ref is not None:
                    asked = refs[item.form_ref]
                    if asked["id"] in settled:
                        continue  # one answer per pasted question
                    settled[asked["id"]] = after_model(
                        db, job, profile, item, qid=asked["id"], question=asked["question"], origin=FORM,
                        requirement_labels=ctx["requirements"], evidence_labels=evidence_labels,
                        resume_facts=resume_facts, waiting=asked["id"] in waiting)
                elif predict and len(predicted_items) < MAX_PREDICTED:
                    qid = question_id(PREDICTED, item.form_question)
                    if question_id(FORM, item.form_question) in seen or qid in seen:
                        continue
                    seen.add(qid)
                    predicted_items.append(before_model(db, qid, item.form_question, PREDICTED, waiting, job.id) or after_model(
                        db, job, profile, item, qid=qid, question=item.form_question, origin=PREDICTED,
                        requirement_labels=ctx["requirements"], evidence_labels=evidence_labels,
                        resume_facts=resume_facts, waiting=qid in waiting))

    items = []
    for q in pasted:
        if q["id"] in settled:
            items.append(settled[q["id"]])
        elif q["id"] in previous and previous[q["id"]].get("status") != NEEDS_INPUT:
            items.append(previous[q["id"]])  # the model did not answer this time: the last good draft stands
        else:
            items.append(_item(q["id"], q["question"], FORM, NEEDS_INPUT,
                               reason=" ".join(r for r in ("Not drafted: " + (errors[-1] if errors else
                                                                              "the model gave no answer"),
                                                           WAITING if q["id"] in waiting else "") if r)))
    if predicted_items is not None:
        items += predicted_items
    else:
        items += [i for i in previous.values() if i.get("origin") == PREDICTED]
    return {"items": items, "model_ran": model_ran, "predicted": predicted_items is not None}
