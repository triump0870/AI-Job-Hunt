"""Where each answer of an application form comes from (Phase 5). Deterministic; no model writes an answer.

    plan(db, job, form, confirmed)    → field id → {status, source, …}: what fills each field, never the value
    values(db, job, form, plan, …)    → the values themselves, re-read from their sources at fill time (never stored)

Order, per field:
  1. the candidate profile's own contact fields (name, email, phone, LinkedIn, GitHub, location), by the form's markup;
  2. the approved packet: the resume file (rendered from the approved version, after `approval.verify`) and the
     approved cover letter (`approval.packet_content`, the text the digest covers);
  3. `knowledge.search` on the field's label: a verified saved answer to the same question (ANSWERED). For a select or
     radio the answer must be one of the form's own options, or the field is not answered;
  4. a sensitive saved answer (NEEDS_CONFIRMATION) is used only once the person confirmed it for this execution;
     a job-specific answer ("Why this role?") written for another job is never used: the field waits on it
     (NEEDS_INPUT) until the person saves one for this job (`knowledge.fits_job`);
  5. anything else a *required* field needs is saved for the person (`knowledge.request_answer`) and the execution
     waits (NEEDS_INPUT). Optional fields that cannot be answered, and voluntary self-identification (EEO), stay empty.

Work-authorization, visa and sponsorship questions are never answered from a profile fact or a suggestion (the
existing `knowledge.search` rule): only the person's own verified saved answer, confirmed if sensitive.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models import Job, KnowledgeAnswer
from app.services import knowledge
from app.services.ats_forms import ApplicationForm, FormField

RESOLVED, CONFIRM, NEEDS_INPUT, SKIPPED = "RESOLVED", "NEEDS_CONFIRMATION", "NEEDS_INPUT", "SKIPPED"
PROFILE_ROLES = {"email": "email", "phone": "phone", "linkedin": "linkedin_url", "github": "github_url",
                 "location": "location"}
YES = ("yes", "true", "y", "1")


def _entry(field: FormField, status: str, source: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"status": status, "source": source, "required": field.required, "kind": field.kind, **extra}


def _profile(db: Session) -> Any:
    from app.services.workflow import get_profile

    return get_profile(db)


def _name_parts(name: str) -> tuple[str, str]:
    parts = (name or "").split()
    return (parts[0], " ".join(parts[1:])) if len(parts) > 1 else ((parts[0], "") if parts else ("", ""))


def _option_for(field: FormField, answer: str) -> str | None:
    """The form option an answer names (its label or value, ignoring case), or None — never a guess."""
    wanted = (answer or "").strip().lower()
    for option in field.options:
        if wanted and wanted in (option.label.strip().lower(), option.value.strip().lower()):
            return option.value
    return None


def _sensitive_question(label: str) -> bool:
    return bool(knowledge.terms(label) & knowledge.PROFILE_ANSWERS["work_authorization"][0])


def plan(db: Session, job: Job, form: ApplicationForm, *, confirmed: list[int] | None = None,
         requested_by: str = "application_worker", before_write: Callable[[], None] | None = None) -> dict[str, Any]:
    """Each field's source and status. Saves the questions a person must answer (request_answer commits).
    `before_write` runs before each of those saves (the graph passes its lease check, so a run that was stopped
    meanwhile raises there and leaves no further questions behind).
    Returns {"fields": {...}, "needs_input": [question ids], "needs_confirmation": [question ids]}."""
    confirmed_ids = set(confirmed or [])
    profile = _profile(db)
    fields: dict[str, Any] = {}
    needs_input: list[int] = []
    needs_confirmation: list[int] = []
    for field in form.fields:
        entry = _resolve(db, job, profile, field, confirmed_ids)
        if entry["status"] == NEEDS_INPUT and field.required and not entry.get("other_job"):
            if before_write is not None:
                before_write()
            saved = knowledge.request_answer(
                db, job, field.label, answer_options=[o.label for o in field.options] or None,
                answer_type=("choice" if field.options else "boolean" if field.kind == "checkbox" else "text"),
                category="demographic" if field.eeo else "general",
                sensitive=field.eeo or _sensitive_question(field.label), required=True, field_label=field.label,
                requested_by=requested_by)
            if saved["status"] == "ALREADY_ANSWERED":
                entry = _resolve(db, job, profile, field, confirmed_ids)  # answered meanwhile: read it again
            elif saved["status"] == "NEEDS_CONFIRMATION":
                entry = _entry(field, CONFIRM, f"knowledge_answer:{saved['question_id']}",
                               question_id=saved["question_id"], sensitive=True)
            else:
                entry["question_id"] = saved["question_id"]
        if entry["status"] == NEEDS_INPUT and not field.required:
            entry = _entry(field, SKIPPED, None, why="optional and not answered by a verified source")
        if entry["status"] == NEEDS_INPUT and entry.get("question_id") is not None:
            needs_input.append(entry["question_id"])
        # A verified saved answer that fits no option of this form (re-resolved above) has no question to wait on:
        # the field stays unresolved, so the pre-submit checks hand it to the person instead of filling a guess.
        if entry["status"] == CONFIRM:
            needs_confirmation.append(entry["question_id"])
        fields[field.id] = entry
    return {"fields": fields, "needs_input": sorted(set(needs_input)),
            "needs_confirmation": sorted(set(needs_confirmation))}


def _resolve(db: Session, job: Job, profile: Any, field: FormField, confirmed: set[int]) -> dict[str, Any]:
    if field.eeo:
        return _entry(field, NEEDS_INPUT if field.required else SKIPPED, None, why="voluntary self-identification")
    if field.role in ("first_name", "last_name", "full_name"):
        first, last = _name_parts(getattr(profile, "name", ""))
        value = {"first_name": first, "last_name": last, "full_name": getattr(profile, "name", "")}[field.role]
        return _entry(field, RESOLVED, f"candidate_profile.name:{field.role}") if value else _entry(field, NEEDS_INPUT)
    if field.role in PROFILE_ROLES:
        column = PROFILE_ROLES[field.role]
        value = str(getattr(profile, column, "") or "").strip()
        return _entry(field, RESOLVED, f"candidate_profile.{column}") if value else _entry(field, NEEDS_INPUT)
    if field.role == "resume":
        return _entry(field, RESOLVED, "packet.resume_pdf" if field.kind == "file" else "packet.resume_text")
    if field.role == "cover_letter":
        from app.services import approval

        variant = _approved_variant(db, job)
        letter = approval.packet_content(variant)["cover_letter"] if variant is not None else ""
        if not letter:
            return _entry(field, NEEDS_INPUT)
        return _entry(field, RESOLVED, "packet.cover_letter_file" if field.kind == "file" else "packet.cover_letter")
    found = knowledge.search(db, field.label, 50)  # enough sensitive rows to find the same question among similar ones
    status = found["status"]
    if status == "ANSWERED" and (found.get("answer_source") or "").startswith("knowledge_answer:"):
        answer = found["answer"]
        row = db.get(KnowledgeAnswer, int(found["answer_source"].split(":", 1)[1]))
        if row is not None and not knowledge.fits_job(row, job.id):
            # "Why this role?" answered for another employer: never sent here. The run waits on that question; the
            # Apply kit offers this job's own draft (job insights), and saving it makes the answer this job's.
            return _entry(field, NEEDS_INPUT, why="written for another job", question_id=row.id, other_job=True)
        if _chooses(field) and _option_for(field, answer) is None:
            return _entry(field, NEEDS_INPUT, why="the saved answer is not one of the form's options")
        return _entry(field, RESOLVED, found["answer_source"])
    if status == "ANSWERED" and (found.get("answer_source") or "").startswith("candidate_profile."):
        return _entry(field, RESOLVED, found["answer_source"]) if not _chooses(field) \
            else _entry(field, NEEDS_INPUT)
    if status == "NEEDS_CONFIRMATION":
        # knowledge.search withholds sensitive answers to *similar* questions too (for a person reading them). A form
        # value may only come from the same question: "Are you authorized to work in the US?" and "Will you require
        # sponsorship to work in the US?" are similar, and the same saved "Yes" means the opposite in each.
        same = next((s for s in found.get("sensitive_answers") or []
                     if knowledge.same_question(field.label, s["question"])), None)
        if same is None:
            return _entry(field, NEEDS_INPUT, why="only a similar question has a saved sensitive answer")
        question_id = same["id"]
        if question_id in confirmed:
            return _entry(field, RESOLVED, same["source"], sensitive=True, question_id=question_id,
                          confirmed=True)
        return _entry(field, CONFIRM, same["source"], sensitive=True, question_id=question_id)
    return _entry(field, NEEDS_INPUT, why=status)


def _approved_variant(db: Session, job: Job) -> Any:
    from app.services.apply_queue import approved_resume

    return approved_resume(db, job.application)


def values(db: Session, job: Job, form: ApplicationForm, field_plan: dict[str, Any], *,
           files: dict[str, str] | None = None) -> dict[str, Any]:
    """The values to fill, read again from each source now. Only RESOLVED fields; a source that no longer gives a
    value (an answer un-verified meanwhile) leaves its field out. Never persisted, never logged."""
    from app.services import approval

    profile = _profile(db)
    out: dict[str, Any] = {}
    for field in form.fields:
        entry = (field_plan.get("fields") or {}).get(field.id) or {}
        if entry.get("status") != RESOLVED:
            continue
        source = entry.get("source") or ""
        value: Any = None
        if source.startswith("candidate_profile.name:"):
            first, last = _name_parts(profile.name)
            value = {"first_name": first, "last_name": last, "full_name": profile.name}[source.rsplit(":", 1)[1]]
        elif source.startswith("candidate_profile."):
            value = str(getattr(profile, source.split(".", 1)[1], "") or "").strip()
        elif source == "packet.resume_pdf" and files and files.get("pdf"):
            value = {"path": files["pdf"]}
        elif source == "packet.cover_letter_file" and files and files.get("cover_letter"):
            value = {"path": files["cover_letter"]}
        elif source in ("packet.resume_text", "packet.cover_letter"):
            variant = _approved_variant(db, job)
            content = approval.packet_content(variant) if variant is not None else {}
            value = content.get("resume_markdown" if source == "packet.resume_text" else "cover_letter")
        elif source.startswith("knowledge_answer:"):
            row = db.get(KnowledgeAnswer, int(source.split(":")[1]))
            if row is not None and row.verified and (row.answer or "").strip():
                value = _form_value(field, row.answer)
        if value not in (None, ""):
            out[field.id] = value
    return out


def _chooses(field: FormField) -> bool:
    """A field whose value must be one of its options. A dropdown widget whose options are not in the page yet takes
    the verified answer's own text, and the runner's `choose` requires an exact option match when it opens."""
    return field.kind in ("select", "radio") or (field.kind == "combobox" and bool(field.options))


def _form_value(field: FormField, answer: str) -> Any:
    if _chooses(field):
        return _option_for(field, answer)
    if field.kind == "combobox":
        return answer.strip()
    if field.kind == "checkbox":
        return answer.strip().lower() in YES
    return answer.strip()


def write_cover_letter(folder: Path, letter: str) -> str:
    """The approved letter as a text file beside the packet's resume files, for a form that wants a file."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "cover_letter.txt"
    path.write_text(letter, encoding="utf-8")
    return str(path)
