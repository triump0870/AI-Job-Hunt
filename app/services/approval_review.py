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


def _pay_check(answers: list[KnowledgeAnswer], questions: dict[str, Any] | None,
               pay: dict[str, Any] | None) -> tuple[str, str]:
    """Compensation, for this job: whether its form asks about pay and what answer it would get, next to the range
    the posting states. A form that never asks needs no answer."""
    posted = f" · posted {pay['text']}" if pay else ""
    asked = [q for q in (questions or {}).get("items") or [] if PAY_QUESTION_RE.search(q.get("question") or "")]
    if asked:
        ready = [q for q in asked if q.get("state") in ("verified", "confirm")]
        return ((f"The form asks about pay; your answer is on file{posted}", "ok") if len(ready) == len(asked)
                else (f"The form asks “{asked[0]['question'][:60]}” and there is no verified answer{posted}", "warn"))
    # Only the form's own questions (pasted, or read by an application run) can say it never asks; predicted ones
    # cannot.
    if any(q.get("origin") == "form" for q in (questions or {}).get("items") or []):
        return f"This form doesn't ask about pay{posted}", "ok"
    verified = any(a.verified for a in answers if a.category == "compensation")
    return ((f"Verified answer on file, if the form asks{posted}", "ok") if verified
            else (f"No verified pay answer yet; needed only if the form asks{posted}", "warn"))


# Which checks must be fixed before the packet goes out (blocker) and which are worth a look (nudge). Neither gates
# Approve: the person may approve whatever the checks say.
BLOCKERS = {"Claims backed by evidence", "No invented experience", "Resume reads cleanly", "Contact details present",
            "Reviewer threshold"}
FIXES = {
    "Claims backed by evidence": {"label": "Fix in Tailor", "screen": "tailor"},
    "No invented experience": {"label": "Fix in Tailor", "screen": "tailor"},
    "Resume reads cleanly": {"label": "Fix in Tailor", "screen": "tailor"},
    "Length": {"label": "Shorten in Tailor", "screen": "tailor"},
    "Contact details present": {"label": "Add in Settings", "href": "#settings"},
    "Compensation answer": {"label": "Add to Knowledge bank", "screen": "knowledge"},
    "Reviewer threshold": {"label": "Send back for revision", "act": "revise-open"},
}


def checklist(job: Job, data: dict[str, Any], variant: Any, profile: Any,
              answers: list[KnowledgeAnswer], questions: dict[str, Any] | None = None) -> list[dict[str, Any]]:
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

    pay = _pay_check(answers, questions, salary(job))
    score = resume["reviewer_score"] or 0
    contact = bool(profile.email and profile.phone)
    items = [
        {"label": "Claims backed by evidence", "detail": evidence[0], "state": evidence[1]},
        {"label": "No invented experience", "detail": invented[0], "state": invented[1]},
        {"label": "Resume reads cleanly", "detail": clean[0], "state": clean[1], "issues": lint[:10]},
        {"label": "Contact details present", "detail": "Email and phone on profile" if contact
         else "Add email and phone in Settings", "state": "ok" if contact else "warn"},
        {"label": "Length", "detail": length[0], "state": length[1]},
        {"label": "Compensation answer", "detail": pay[0], "state": pay[1]},
        {"label": "Reviewer threshold", "detail": f"{score:.0f} vs minimum {data['min_review_score']}",
         "state": "ok" if score >= data["min_review_score"] else "warn"},
    ]
    for item in items:
        item["level"] = "blocker" if item["label"] in BLOCKERS else "nudge"
        if item["state"] != "ok":
            item["fix"] = FIXES.get(item["label"])
    return items


def clean_question(question: str) -> str:
    """The question as the form asked it, without scraped hint text or required-field marks."""
    first = re.split(r"\s*\*?\s*\n|\s+\*\s*Hint\s*:", question or "", maxsplit=1)[0]
    return first.strip().rstrip("*").strip()


def job_specific(question: str) -> bool:
    from app.services.knowledge import job_specific as specific

    return specific(question)


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
        return "other_job", "Written for another job — auto-apply drafts one for this job and asks you to save it"
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


# ───────────── the role's facts, and what approving does ─────────────

WORK_MODE_LABELS = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}
PERIOD_LABELS = {"year": "/yr", "yearly": "/yr", "annual": "/yr", "month": "/mo", "monthly": "/mo", "hour": "/hr",
                 "hourly": "/hr", "week": "/wk", "day": "/day"}
PAY_QUESTION_RE = re.compile(r"\b(salary|compensation|pay|ctc|remuneration|expected (?:base|package))\b",
                             re.IGNORECASE)


def work_mode(job: Job) -> str | None:
    """remote, hybrid or onsite as the board stated it; None when it did not say."""
    mode = (job.remote_type or "").strip().lower().replace("-", "").replace(" ", "")
    if mode in ("remote", "hybrid"):
        return mode
    if mode in ("onsite", "inoffice", "office"):
        return "onsite"
    text = (job.location or "").lower()
    return "remote" if "remote" in text else "hybrid" if "hybrid" in text else None


def _money(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value / 1000:.0f}K" if value >= 10_000 else f"{value:,.0f}"


def salary(job: Job) -> dict[str, Any] | None:
    """The salary the posting states (never an estimate): {min, max, currency, period, text}. None when unstated."""
    from app.services.compensation import extract_posting_salary

    found: dict[str, Any] | None = None
    if job.salary_min is not None or job.salary_max is not None:
        found = {"min": job.salary_min, "max": job.salary_max, "currency": job.salary_currency,
                 "period": job.salary_interval}
    else:
        stored = job.compensation if isinstance(job.compensation, dict) else {}
        found = stored.get("posting") or extract_posting_salary(job.raw)
    if not found or (found.get("min") is None and found.get("max") is None):
        return None
    low, high = found.get("min"), found.get("max")
    amount = "–".join(_money(v) for v in (low, high) if v is not None) if low != high else _money(low)
    currency = (found.get("currency") or "").upper()
    period = PERIOD_LABELS.get(str(found.get("period") or "").lower(), "")
    return {"min": low, "max": high, "currency": currency or None, "period": found.get("period"),
            "text": f"{currency + ' ' if currency else ''}{amount}{period}"}


def _location_wanted(job: Job, preferred: list[str], mode: str | None) -> bool | None:
    """Whether the job's place is one of the person's preferred locations. None when no preference is set."""
    wanted = [p.strip().lower() for p in preferred or [] if p and p.strip()]
    if not wanted:
        return None
    place = (job.location or "").lower()
    if mode == "remote" and any("remote" in w for w in wanted):
        return True
    return any(w.split(",")[0].strip() in place for w in wanted if w.split(",")[0].strip())


def approve_effect(db: Session, job: Job) -> dict[str, str]:
    """What approving this job will do: queue the guarded automatic submission (auto) or open the apply kit for the
    person (kit). A prediction for the button's label only; application_execution.on_approved decides."""
    from app.services import application_execution, ats_map

    match = ats_map.for_job(job)
    auto = (application_execution.mode() == "guarded_auto" and application_execution.routes(db)
            and application_execution.default_mode(db, job.user_id) == "GUARDED_AUTO")
    if auto and match is not None and match.auto_submit:
        return {"kind": "auto", "label": "Approve & auto-apply",
                "detail": f"Approving queues the guarded submission on {match.label}; it pauses for anything only "
                          "you can answer."}
    why = ("Auto submit is off for your account" if not auto
           else "This job has no application web address" if match is None
           else match.note or f"{match.label} applications are not automated")
    return {"kind": "kit", "label": "Approve → apply kit",
            "detail": f"{why}: approving opens the apply kit and you submit the form yourself."}


def facts(db: Session, job: Job, profile: Any) -> dict[str, Any]:
    """The role's facts the decision needs, from what the posting stated. Unknown stays None; a mismatch with the
    person's preferences is flagged, never decided."""
    from app.services import ats_map

    mode = work_mode(job)
    pay = salary(job)
    match = ats_map.for_job(job)
    wanted = _location_wanted(job, list(getattr(profile, "preferred_locations", None) or []), mode)
    return {
        "location": job.location or None, "work_mode": mode, "work_mode_label": WORK_MODE_LABELS.get(mode or ""),
        "location_wanted": wanted, "salary": pay, "seniority": job.seniority or None,
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
        "route": {"ats": match.ats if match else None, "label": match.label if match else None,
                  "auto_submit": bool(match and match.auto_submit)},
    }


def role_gaps(job: Job) -> list[dict[str, Any]]:
    """The job's requirements against the candidate's evidence, as the Ranker assessed them: requirement → what is
    shown → met / partial / missing / unknown. Required ones first; at most 15 (the Ranker's own limit)."""
    status = {"MATCH": "met", "PARTIAL_MATCH": "partial", "MISSING": "missing", "UNKNOWN": "unknown"}
    rows = [a for a in (job.fit_analysis or {}).get("requirement_assessments") or []
            if isinstance(a, dict) and a.get("requirement")]
    rows.sort(key=lambda a: (str(a.get("level") or "").upper() != "REQUIRED",
                             ["MISSING", "PARTIAL_MATCH", "UNKNOWN", "MATCH"].index(a.get("status"))
                             if a.get("status") in status else 4))
    return [{"requirement": str(a["requirement"]), "level": str(a.get("level") or "UNKNOWN").lower(),
             "status": status.get(str(a.get("status") or "").upper(), "unknown"),
             "supported": a.get("supported") or "", "unsupported": a.get("unsupported") or "",
             "fatal": bool(a.get("fatal"))} for a in rows[:15]]


def duplicate_key(job: Job) -> str:
    """Postings of one role: the same company and the same full title, case and punctuation aside. The title is kept
    whole on purpose — "Engineer, Backend (Cards)" and "Engineer, Fullstack (Marketplace)" are different roles."""
    words = lambda text: " ".join(re.findall(r"[a-z0-9+#]+", (text or "").lower()))
    return f"{words(job.company)}|{words(job.title)}"


STALE_DAYS = 30


def stale(job: Job) -> dict[str, str] | None:
    """Why this posting may no longer be worth applying to: closed by the employer, or posted long ago. Informs only;
    the person may approve anyway."""
    from datetime import UTC, datetime

    if job.posting_closed_at is not None:
        return {"kind": "closed", "text": "The daily posting check found this posting closed."}
    if job.posted_at is not None:
        days = (datetime.now(UTC).replace(tzinfo=None) - job.posted_at).days
        if days > STALE_DAYS:
            return {"kind": "old", "text": f"Posted {days} days ago: it may be filled. Check the posting before you "
                                           "approve."}
    return None


def score_why(variant: Any, minimum: int) -> dict[str, Any] | None:
    """What stands behind the reviewer's score, from its own stored review: its summary, recommendation and
    confidence, the decision and its reasons, and how many claims it disputed. Nothing is recomputed."""
    notes = (variant.reviewer_notes or {}) if variant is not None else {}
    if variant is None or variant.reviewer_score is None or not notes:
        return None
    confidence = notes.get("confidence")
    return {"score": variant.reviewer_score, "minimum": minimum, "summary": str(notes.get("summary") or "")[:600],
            "recommendation": notes.get("recommendation") or None,
            "confidence": round(float(confidence) * 100) if isinstance(confidence, int | float) else None,
            "decision": notes.get("decision") or None,
            # In words, as the review rows show them: no codes, and no "N claim(s) found" count lines (the claims
            # themselves are listed under the score).
            "decision_reasons": [_reason_text(str(r)) for r in notes.get("decision_reasons") or []
                                 if str(r).strip() and not COUNT_RE.match(str(r).strip())][:5],
            "disputed": len(notes.get("unsupported_claims") or []) + len(notes.get("overstated_claims") or []),
            "checks_failed": len(notes.get("deterministic_issues") or [])}
