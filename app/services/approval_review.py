"""What the Approvals screen shows about one packet, worded for the person deciding. Deterministic; reads only.

    review_notes(notes)                         → the reviewer's findings, one row per claim it disputes
    checklist(db, job, data, profile, answers)  → the pre-submit checks, each stating what it actually measured
    job_answers(db, job)                        → the application questions known for this job and their answers

Nothing here decides anything: approving, sending back and the review decision stay with their owning services.
"""
from __future__ import annotations

import ast
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, JobInsight, KnowledgeAnswer
from app.services import resume_lint
from app.services.resume_format import body_text
from app.services.resume_length import DEFAULT_PAGES, estimate_pages

# Where a deterministic issue sits (tailoring_results issue locations), in the person's words.
LOCATIONS = (
    (re.compile(r"^cover_letter"), "Cover letter"),
    (re.compile(r"^summary"), "Summary"),
    (re.compile(r"^skills_order"), "Skills"),
    (re.compile(r"^experience_sections"), "Experience"),
    (re.compile(r"^resume_bullets"), "Highlights"),
    (re.compile(r"^screening_answer_drafts"), "Screening answer"),
)
LIST_RE = re.compile(r"\[[^\]]*\]")
# The count lines decide_review adds next to the claim lists themselves; the claims are shown instead.
COUNT_RE = re.compile(r"^\d+ (unsupported|overstated) claim\(s\) found by the reviewer$")
CODE_RE = re.compile(r"^([A-Z][A-Z_]+): (.*)$", re.DOTALL)
# Questions whose answer is about one employer or one role: an answer written for another job reads wrong here.
JOB_SPECIFIC_RE = re.compile(
    r"\bwhy\b[^?]*\b(?:interested|want to (?:work|join)|wish to (?:work|join)|(?:this|our|the) "
    r"(?:role|position|job|company|team|opportunity))\b|"
    r"\bwhat\b[^?]*\b(?:excites|attracts|interests|draws) you\b",
    re.IGNORECASE)
MAX_ANSWERS = 20


def _where(location: str) -> str:
    return next((label for pattern, label in LOCATIONS if pattern.match(location or "")), "Resume")


def _quoted(match: re.Match[str]) -> str:
    try:
        items = ast.literal_eval(match.group(0))
    except (ValueError, SyntaxError):
        return match.group(0)
    return ", ".join(f"“{item}”" for item in items) if isinstance(items, list) else match.group(0)


def issue_text(issue: dict[str, Any]) -> str:
    """A deterministic issue as a sentence: where it is and what is wrong, without the code or list syntax."""
    code, message = issue.get("code") or "", (issue.get("message") or "").strip()
    message = LIST_RE.sub(_quoted, message)
    if code == "UNSUPPORTED_TERM":
        message = re.sub(r"\s*appear nowhere in", " isn't in", message)
        message = message.replace("; cite them there first or leave them out", "")
        message += " — add it there first, or leave it out"
    elif code == "INVENTED_SKILL" and issue.get("value"):
        message = f"“{issue['value']}” isn't in your resume, profile skills or verified evidence"
    return f"{_where(issue.get('location') or '')}: {message[:1].upper()}{message[1:]}"


def _reason_text(reason: str) -> str:
    """A stored reject reason ("CODE: message") as a sentence, for reviews stored before deterministic_issues."""
    match = CODE_RE.match(reason.strip())
    if not match:
        return reason
    return issue_text({"code": match.group(1), "message": match.group(2)})


def review_notes(notes: dict[str, Any]) -> list[dict[str, str]]:
    """The reviewer's findings: each disputed claim on its own row (group "packet"), the job's requirements the
    resume does not show (group "gap"), then strengths. Rows keep the {tone, text} shape the screens read."""
    issues = notes.get("deterministic_issues") or []
    unsupported = notes.get("unsupported_claims") or []
    overstated = notes.get("overstated_claims") or []
    packet: list[str] = [issue_text(i) for i in issues if isinstance(i, dict)]
    packet += [f"Unsupported: {claim}" for claim in unsupported]
    packet += [f"Overstated: {claim}" for claim in overstated]
    for reason in notes.get("reject_reasons") or []:
        text = str(reason).strip()
        if not text or COUNT_RE.match(text):
            continue
        if CODE_RE.match(text) and issues:
            continue  # already listed from deterministic_issues
        packet.append(_reason_text(text))
    return ([{"tone": "bad", "group": "packet", "text": t} for t in dict.fromkeys(packet)]
            + [{"tone": "warn", "group": "gap", "text": s} for s in notes.get("improvements") or []]
            + [{"tone": "good", "group": "strength", "text": s} for s in notes.get("strengths") or []])


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def checklist(job: Job, data: dict[str, Any], variant: Any, profile: Any,
              answers: list[KnowledgeAnswer]) -> list[dict[str, str]]:
    resume = data["resume"]
    notes = (variant.reviewer_notes or {}) if variant is not None else {}
    content = (variant.content or {}) if variant is not None else {}
    edits = [e for e in resume["edits"] if e["status"] != "reverted"]
    cited = sum(1 for e in edits if e["evidence"])
    disputed = len(notes.get("unsupported_claims") or []) + len(notes.get("overstated_claims") or [])
    if cited < len(edits):
        evidence = (f"{cited} of {len(edits)} edits cite evidence"
                    + (f" · reviewer disputes {_plural(disputed, 'claim')}" if disputed else ""), "warn")
    elif disputed:
        evidence = (f"Every edit cites evidence, but the reviewer disputes {_plural(disputed, 'claim')}", "warn")
    else:
        evidence = (f"{cited} of {len(edits)} edits cite evidence; the reviewer disputes none", "ok")

    issues = [i for i in notes.get("deterministic_issues") or [] if isinstance(i, dict)]
    legacy = [r for r in notes.get("reject_reasons") or [] if CODE_RE.match(str(r))] if not issues else []
    found = [issue_text(i) for i in issues] or [_reason_text(r) for r in legacy]
    invented = ((found[0] + (f" (+{len(found) - 1} more)" if len(found) > 1 else ""), "warn") if found
                else ("No term or number the evidence doesn't back", "ok"))

    body = body_text(resume["markdown"])
    pages = estimate_pages(body)
    target = int((content.get("length_plan") or {}).get("pages") or DEFAULT_PAGES)
    words = len(body.split())
    length = (f"≈{pages:g} pages · target {target} · {words} words",
              "ok" if pages <= target + 0.1 else "warn")

    lint = resume_lint.lint(resume["markdown"])
    clean = ((f"“{lint[0]['text'][:70].strip()}” {lint[0]['message']}"
              + (f" (+{len(lint) - 1} more)" if len(lint) > 1 else ""), "warn") if lint
             else ("No repeated lines, stray dashes or spacing slips", "ok"))

    comp = [a for a in answers if a.category == "compensation"]
    score = resume["reviewer_score"] or 0
    contact = bool(profile.email and profile.phone)
    return [
        {"label": "Claims backed by evidence", "detail": evidence[0], "state": evidence[1]},
        {"label": "No invented experience", "detail": invented[0], "state": invented[1]},
        {"label": "Resume reads cleanly", "detail": clean[0], "state": clean[1], "issues": lint[:10]},
        {"label": "Contact details present", "detail": "Email and phone on profile" if contact
         else "Add email and phone in Settings", "state": "ok" if contact else "warn"},
        {"label": "Length", "detail": length[0], "state": length[1]},
        {"label": "Compensation answer", "detail": "Verified answer on file" if any(a.verified for a in comp)
         else "No verified compensation answer yet", "state": "ok" if any(a.verified for a in comp) else "warn"},
        {"label": "Reviewer threshold", "detail": f"{score:.0f} vs minimum {data['min_review_score']}",
         "state": "ok" if score >= data["min_review_score"] else "warn"},
    ]


def clean_question(question: str) -> str:
    """The question as the form asked it, without scraped hint text or required-field marks."""
    first = re.split(r"\s*\*?\s*\n|\s+\*\s*Hint\s*:", question or "", maxsplit=1)[0]
    return first.strip().rstrip("*").strip()


def job_specific(question: str) -> bool:
    return bool(JOB_SPECIFIC_RE.search(question or ""))


def _asked_by(row: KnowledgeAnswer, job_id: int) -> bool:
    context = row.request_context or {}
    return row.source_job_id == job_id or job_id in (context.get("job_ids") or [])


def _library_state(row: KnowledgeAnswer | None, job: Job, question: str) -> tuple[str, str]:
    if row is None or not (row.answer or "").strip():
        return "needs_input", "Needs your answer"
    if row.sensitive:
        return "confirm", "Confirm on the form"
    if not row.verified:
        return "unverified", "Not verified"
    if job_specific(question) and row.source_job_id != job.id:
        return "other_job", "Written for another job — check it fits"
    return "verified", "Verified"


def job_answers(db: Session, job: Job) -> dict[str, Any]:
    """The questions this job's application is known to ask — pasted from its form or predicted by its research
    (job_insights.application_questions), or asked by an application run (Knowledge bank requests) — each with the
    answer that would be used and where it comes from. Saved answers to other jobs' questions are never listed."""
    from app.services.job_insights import application_form

    rows = {r.id: r for r in db.scalars(select(KnowledgeAnswer)).all()}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(question: str, answer: str | None, state: str, label: str, origin: str) -> None:
        text = clean_question(question)
        key = " ".join(text.lower().split())
        if not text or key in seen:
            return
        seen.add(key)
        label = LIST_RE.sub(_quoted, label or "").replace("; cite them there first or leave them out", "")
        out.append({"question": text, "answer": answer if state not in ("confirm",) else None, "state": state,
                    "label": label, "origin": origin})

    insight = db.scalar(select(JobInsight).where(JobInsight.job_id == job.id))
    if insight is not None:
        for item in application_form.current(insight.application_questions or [], insight.application_form_input):
            origin = "form" if item.get("origin") == application_form.FORM else "predicted"
            status = item.get("status")
            if status == application_form.LIBRARY:
                row = rows.get(item.get("knowledge_answer_id"))
                state, label = _library_state(row, job, item["question"]) if row is not None \
                    else ("verified", item.get("reason") or "From your profile")
                add(item["question"], item.get("answer"), state, label, origin)
            elif status == application_form.DRAFTED:
                add(item["question"], item.get("answer"), "draft", "Draft — check before you submit", origin)
            else:
                add(item["question"], None, "needs_input", item.get("reason") or "Needs your answer", origin)
    for row in sorted(rows.values(), key=lambda r: r.id):
        if _asked_by(row, job.id):
            state, label = _library_state(row, job, row.question)
            add(row.question, row.answer or None, state, label, "form")
    known = bool(out)
    return {"items": out[:MAX_ANSWERS], "known": known}
