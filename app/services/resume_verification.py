"""Resume versions, user verification, change history and the Evidence Bank link.

    Structured field → user verifies → VerifiedEvidence (Evidence, source="resume") created or updated

Rules this module enforces:
* Resume versions are never overwritten. An upload or new master text is a new version; re-uploading the same
  file reuses its version.
* One selection: the active version (Resume.is_active). Only set_active changes it, and only because the person
  asked; uploads and edits never do (a profile's very first resume is the one exception). The profile's master
  text and the library's active file are mirrors of it that set_active keeps in step.
* Every workflow resolves its resume through selected / for_job: the job's own resume, else the active one, in
  the form the candidate policy's resume source names (structured data, or the raw text as written).
* Only the user verifies. Every unit starts UNVERIFIED; editing a VERIFIED or REJECTED unit makes it EDITED and
  a VERIFIED resume NEEDS_REVIEW until it is verified again.
* A parser never overwrites data a person reviewed or edited: re-parsing produces a comparison that the user
  resolves value by value. The AI structuring of a new upload (app.services.resume_parsing) fills the data only while
  nobody has reviewed or edited it.
* Evidence is created only from VERIFIED bullets under a VERIFIED role or project. Anything else that had
  evidence has it marked unverified (never deleted, so rankings that cited it stay explainable).
* Every change is written to ResumeDataChange with its source (PARSER, USER, SYSTEM).
"""
from __future__ import annotations

import copy
import hashlib
import re
from datetime import datetime
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import CandidateProfile, Evidence, Resume, ResumeDataChange, ResumeDocument
from app.services import evidence_tags
from app.services import structured_resume as sr
from app.services.structured_resume import (
    REVIEWED,
    StructuredResumeIn,
    Unit,
    clean_raw_text,
    iter_units,
    materialize,
)

RESUME_STATUSES = ("UNVERIFIED", "PARTIALLY_VERIFIED", "VERIFIED", "NEEDS_REVIEW")
CHANGE_FOR_STATUS = {"VERIFIED": "VERIFY", "REJECTED": "REJECT", "UNVERIFIED": "UNVERIFY"}


class ResumeConflict(RuntimeError):
    """The structured data changed since the editor loaded it."""


class UnknownItem(LookupError):
    pass


class VerificationBlocked(ValueError):
    def __init__(self, blocking: list[dict[str, Any]]):
        super().__init__(f"{len(blocking)} required item(s) still need review")
        self.blocking = blocking


def utcnow() -> datetime:
    return sr.utcnow()


def _iso() -> str:
    return utcnow().replace(microsecond=0).isoformat()


def record(db: Session, resume: Resume, path: str, change_type: str, source: str, *, item_id: str | None = None,
           old: Any = None, new: Any = None) -> None:
    db.add(ResumeDataChange(user_id=resume.user_id, resume_id=resume.id, field_path=path[:300], item_id=item_id,
                            old_value=old, new_value=new, change_type=change_type, source=source,
                            changed_at=utcnow()))


def _touch(resume: Resume, data: dict[str, Any]) -> None:
    resume.structured_data = data  # a new object, so the JSON column is saved
    resume.revision = (resume.revision or 0) + 1
    resume.updated_at = utcnow()


# ───────────── versions ─────────────

def versions(db: Session, profile: CandidateProfile) -> list[Resume]:
    return list(db.scalars(select(Resume).where(Resume.candidate_profile_id == profile.id)
                           .order_by(desc(Resume.version))).all())


def parse_text(db: Session, resume: Resume) -> str:
    """What the regex parser reads: the parse view of a version read with resume extraction V2, else its raw text."""
    from app.services.resume_extraction import store

    return store.parse_text(db, resume)


def parse_structured(db: Session, resume: Resume) -> dict[str, Any]:
    """The regex parse of a version: from its parse view when it was read with V2, else from its raw text."""
    from app.services.resume_extraction import store

    return store.parse(db, resume)


def ensure_structured(db: Session, resume: Resume) -> dict[str, Any]:
    """Structured data for a version, parsing it deterministically the first time (e.g. migrated versions)."""
    if resume.structured_data is None:
        resume.structured_data = materialize(parse_structured(db, resume))
        resume.parser, resume.parser_version = "deterministic", sr.PARSER_VERSION
        db.flush()
        record(db, resume, "*", "PARSE", "PARSER", new={"parser": resume.parser, "version": resume.parser_version,
                                                        "units": sum(1 for _ in iter_units(resume.structured_data))})
    return resume.structured_data


def set_active(db: Session, resume: Resume) -> Resume:
    """Make this version the active resume: the one selection every AI workflow uses unless a job has its own.

    This is the only function that changes the selection. Its mirrors follow it here and nowhere else: the
    profile's master text is the active version's raw text, and the library marks the version's file as active.
    Uploads, text edits and structured edits never call it on their own."""
    for other in db.scalars(select(Resume).where(Resume.candidate_profile_id == resume.candidate_profile_id,
                                                 Resume.is_active.is_(True), Resume.id != resume.id)).all():
        other.is_active = False
    db.flush()
    resume.is_active = True
    profile = db.get(CandidateProfile, resume.candidate_profile_id)
    if profile is not None and (profile.master_resume or "") != (resume.raw_text or ""):
        profile.master_resume = resume.raw_text or ""
    for document in db.scalars(select(ResumeDocument).where(ResumeDocument.user_id == resume.user_id)).all():
        document.active = document.id == resume.resume_document_id
    db.flush()
    return resume


def has_active(db: Session, profile: CandidateProfile) -> bool:
    return db.scalar(select(func.count(Resume.id)).where(Resume.candidate_profile_id == profile.id,
                                                         Resume.is_active.is_(True))) > 0


def record_version(db: Session, profile: CandidateProfile, raw_text: str, *, document: ResumeDocument | None = None,
                   source: str = "upload", name: str | None = None, activate: bool = False,
                   extraction=None) -> Resume:
    """The version for this text (and file). An identical file or text reuses its version; nothing is overwritten.

    A new version never becomes active by itself (`activate` is for an explicit choice): the person's selection
    stays as it is. The one exception is a profile with no active resume yet, whose first resume is used.

    `extraction` (resume extraction V2) is recorded with a new version before it is parsed, so the parser reads its
    parse view; a reused version gets it only when it has none yet, and its data is not parsed again."""
    from app.services.resume_extraction import store
    digest = sr.text_hash(raw_text)
    query = select(Resume).where(Resume.candidate_profile_id == profile.id)
    existing = None
    if document is not None:
        existing = db.scalar(query.where(Resume.source_file_hash == document.checksum, Resume.raw_text_hash == digest))
    if existing is None:  # the same text: reused, unless it already has another file (a distinct upload stays listed)
        same_text = query.where(Resume.raw_text_hash == digest)
        if document is not None:
            same_text = same_text.where(Resume.resume_document_id.is_(None))
        existing = db.scalar(same_text.order_by(desc(Resume.version)))
    if existing is not None:
        if document is not None and existing.resume_document_id is None:
            existing.resume_document_id = document.id
            existing.source_storage_path = existing.source_storage_path or document.storage_path
            existing.source_filename = existing.source_filename or document.original_filename
            existing.source_media_type = existing.source_media_type or document.media_type
            existing.source_file_hash = existing.source_file_hash or document.checksum
        if extraction is not None and store.record(db, existing.id) is None:
            store.save(db, existing, extraction)
        ensure_structured(db, existing)
        resume = existing
    else:
        version = (db.scalar(select(func.max(Resume.version)).where(Resume.candidate_profile_id == profile.id)) or 0) + 1
        resume = Resume(user_id=profile.user_id, candidate_profile_id=profile.id, version=version, source=source,
                        name=(name or (document.original_filename if document else "Master resume text"))[:300],
                        resume_document_id=document.id if document else None,
                        source_filename=document.original_filename if document else None,
                        source_media_type=document.media_type if document else None,
                        source_file_hash=document.checksum if document else None,
                        source_storage_path=document.storage_path if document else None,
                        raw_text=raw_text, raw_text_hash=digest, revision=0, is_active=False, is_default=False,
                        verification_status="UNVERIFIED", created_at=utcnow(), updated_at=utcnow())
        db.add(resume)
        db.flush()
        if extraction is not None:
            store.save(db, resume, extraction)
        ensure_structured(db, resume)
        # The regex parse is there at once; the AI structuring of the new version is queued (never run here).
        from app.services import resume_parsing

        resume_parsing.queue_after_upload(db, resume)
    if activate or not has_active(db, profile):
        return set_active(db, resume)
    if resume.is_active:
        return resume
    # An inactive version's file is not the active one in the library either, unless the active version uses the same
    # file (a re-read, or an upload of the same file whose text now reads differently): its download must keep working.
    if document is not None and not db.scalar(select(Resume.id).where(
            Resume.candidate_profile_id == profile.id, Resume.resume_document_id == document.id,
            Resume.is_active.is_(True)).limit(1)):
        document.active = False
        db.flush()
    return resume


def active_resume(db: Session, profile: CandidateProfile, create: bool = True) -> Resume | None:
    """The active version, parsed.

    Only a legacy profile that has master text but no version at all gets one here. A profile with versions keeps
    the one the person made active, whatever its master text says: the text is a mirror, never a selection."""
    resume = db.scalar(select(Resume).where(Resume.candidate_profile_id == profile.id, Resume.is_active.is_(True)))
    if resume is None and create:
        newest = db.scalar(select(Resume).where(Resume.candidate_profile_id == profile.id)
                           .order_by(desc(Resume.version)).limit(1))
        if newest is not None:
            resume = set_active(db, newest)  # versions but none active (an old delete): the newest is used
        elif (profile.master_resume or "").strip():
            # The uploaded file this text came from, when there is one: the version keeps it as its original.
            text = profile.master_resume.strip()
            documents = db.scalars(select(ResumeDocument).where(ResumeDocument.user_id == profile.user_id)
                                   .order_by(desc(ResumeDocument.active), desc(ResumeDocument.id))).all()
            document = next((d for d in documents if (d.extracted_text or "").strip() == text), None)
            resume = _record_text_version(db, profile, profile.master_resume, document=document)
    if resume is not None:
        ensure_structured(db, resume)
    return resume


class JobProfile:
    """The candidate profile as one workflow sees it.

    `chosen_resume` is the resume version the workflow uses: the job's own (Job.resume_id, pinned by its search
    setup or by the person, or matched by tags), else the active one. `master_resume` is that version's document as
    the candidate policy's resume source says: its structured data rendered as a resume ("structured", the
    default) or its raw text as written ("master_text"). Everything else is the real profile.

    Workflows resolve the view once (selected / for_job) and pass it on, so every place that reads the master text
    or the version reads the same resume in the same form, without a second code path. The view never changes which
    resume is active, and it cannot rewrite the master text."""

    def __init__(self, profile: CandidateProfile, resume: Resume, source: str = "structured"):
        object.__setattr__(self, "_profile", profile)
        object.__setattr__(self, "chosen_resume", resume)
        object.__setattr__(self, "resume_source", source)
        object.__setattr__(self, "_document", None)

    @property
    def raw_text(self) -> str:
        """The chosen version's text as written: what identifies the resume (ranking hashes it)."""
        return self.chosen_resume.raw_text or ""

    @property
    def master_resume(self) -> str:
        if self.resume_source == "master_text":
            return self.raw_text
        if self._document is None:
            object.__setattr__(self, "_document", sr.render_document(self.chosen_resume.structured_data))
        return self._document

    def __getattr__(self, name: str):
        return getattr(self._profile, name)

    def __setattr__(self, name: str, value) -> None:
        if name == "master_resume":
            raise AttributeError("A workflow's view of the profile cannot change the master resume")
        setattr(self._profile, name, value)


ResumeView = JobProfile


def chosen_resume(db: Session, profile: CandidateProfile, job) -> Resume | None:
    """The resume chosen for this job, when it still exists and belongs to this profile. None = the active one."""
    resume_id = getattr(job, "resume_id", None)
    if not resume_id:
        return None
    resume = db.get(Resume, resume_id)
    return resume if resume is not None and resume.candidate_profile_id == getattr(profile, "id", None) else None


def selected(db: Session, profile: CandidateProfile | JobProfile | None, job=None) -> CandidateProfile | JobProfile | None:
    """The one resolver for which resume a workflow uses, and in what form.

        the job's own resume (Job.resume_id)  →  else the active version
        resume source "structured"            →  its structured data, rendered    (JobProfile.master_resume)
        resume source "master_text"           →  its raw text as written

    Returns the profile unchanged only when it has no resume at all."""
    from app.services.user_settings import candidate_policy

    if profile is None or isinstance(profile, JobProfile):
        return profile
    resume = chosen_resume(db, profile, job) if job is not None else None
    resume = resume or active_resume(db, profile)
    if resume is None:
        return profile
    ensure_structured(db, resume)
    return JobProfile(profile, resume, candidate_policy(db, profile.user_id)["resume_source"])


def for_job(db: Session, profile: CandidateProfile | JobProfile | None, job) -> CandidateProfile | JobProfile | None:
    """The profile to use for this job's ranking, tailoring or review (see selected)."""
    if job is None:
        return selected(db, profile)
    return selected(db, profile, job)


def raw_text(profile: CandidateProfile | JobProfile) -> str:
    """The selected resume's text as written, whatever the resume source."""
    return profile.raw_text if isinstance(profile, JobProfile) else profile.master_resume or ""


def resume_for(db: Session, profile: CandidateProfile | JobProfile, create: bool = True) -> Resume | None:
    """The resume version a workflow reads: the view's chosen one, otherwise the active one."""
    if isinstance(profile, JobProfile):
        return profile.chosen_resume
    return active_resume(db, profile, create=create)


class ActiveResumeError(ValueError):
    """The active version cannot be deleted: another version has to become active first."""


def delete_version(db: Session, resume: Resume) -> dict[str, int]:
    """Delete one non-active version with its structured data and history. Does not commit.

    Nothing else is lost silently:
    * verified evidence it produced stays verified when another version verifies the same fact (it is re-linked), and
      is un-verified otherwise, so tailoring never cites a fact no remaining resume supports;
    * jobs and saved searches that used it go back to matching by tags (else the active resume);
    * the uploaded file stays in the resume library.
    """
    from app.models import Job, SavedSearch

    if resume.is_active:
        raise ActiveResumeError("This is the active resume. Make another version active before deleting it.")
    evidence = db.scalars(select(Evidence).where(Evidence.source == "resume", Evidence.resume_id == resume.id)).all()
    for row in evidence:
        row.resume_id = None
    jobs = db.scalars(select(Job).where(Job.resume_id == resume.id)).all()
    for job in jobs:
        job.resume_id, job.resume_choice = None, None
    setups = 0
    for setup in db.scalars(select(SavedSearch).where(SavedSearch.user_id == resume.user_id)).all():
        if (setup.settings or {}).get("resume_id") == resume.id:
            setup.settings = {**setup.settings, "resume_id": None, "resume_mode": "auto"}
            setups += 1
    for change in db.scalars(select(ResumeDataChange).where(ResumeDataChange.resume_id == resume.id)).all():
        db.delete(change)  # PostgreSQL cascades these; SQLite does not
    from app.models import ResumeParseTask
    from app.services.resume_extraction import store

    if (extraction := store.record(db, resume.id)) is not None:
        db.delete(extraction)  # cascaded on PostgreSQL too
    for task in db.scalars(select(ResumeParseTask).where(ResumeParseTask.resume_id == resume.id)).all():
        db.delete(task)  # likewise: on SQLite a pending task would outlive its resume and block a reused id
    evidence_tags.forget_resume(db, resume.id)  # its evidence tags (cascaded on PostgreSQL)
    profile_id = resume.candidate_profile_id
    db.delete(resume)
    db.flush()
    # Only the evidence this version produced is touched: a fact the newest remaining version still verifies is
    # re-linked to it; any other is un-verified. Evidence of other versions is left exactly as it is.
    verified_elsewhere: dict[str, tuple[Resume, str]] = {}
    for other in db.scalars(select(Resume).where(Resume.candidate_profile_id == profile_id)
                            .order_by(desc(Resume.version))).all():
        for item_id, fact in verified_facts(other.structured_data).items():
            verified_elsewhere.setdefault(fact_key(fact["company"], fact["claim"]), (other, item_id))
    retired = 0
    for row in evidence:
        source = verified_elsewhere.get(row.fact_key or "")
        if source is not None:
            row.resume_id, row.structured_item_id = source[0].id, source[1]
            row.context = f"Verified from resume v{source[0].version}"
            evidence_tags.sync_auto_links(db, source[0], {row.id} | _auto_linked(db, source[0]))
        elif row.verified:
            row.verified, row.context = False, "Its resume version was deleted"
            retired += 1
    return {"jobs": len(jobs), "saved_searches": setups, "evidence_relinked": len(evidence) - retired,
            "evidence_retired": retired}


def version_of_document(db: Session, profile: CandidateProfile, document: ResumeDocument) -> Resume:
    """The resume version an uploaded file became (the newest, when there are several). A file uploaded before
    versions existed gets its version now, without becoming active."""
    resume = db.scalar(select(Resume).where(Resume.resume_document_id == document.id)
                       .order_by(desc(Resume.version)).limit(1))
    if resume is not None:
        return resume
    return record_version(db, profile, document.extracted_text or "", document=document, activate=False)


class RereadUnavailable(ValueError):
    """The version's file cannot be read again with resume extraction V2."""


def reread_file(db: Session, profile: CandidateProfile, resume: Resume, *, user_id: int) -> Resume:
    """Read a version's own uploaded file again with resume extraction V2, as a new upload of that file would.

    For a version read before V2 (it has no extraction record): its text came from the older reader, which keeps a
    ligature glyph a font maps wrongly ("hCps" for "https") when it occurs only once. The result is a new version
    (`record_version`), queued for AI structuring like any upload; the old version, its verified facts and the
    active selection stay as they are. Explicit and per version: nothing re-reads files in bulk. Does not commit."""
    from pathlib import Path

    from app.services import document_text, resume_parsing
    from app.services.resume_extraction import pipeline, store
    from app.settings import get_settings

    if store.record(db, resume.id) is not None:
        raise RereadUnavailable("This resume was already read with the new reader")
    if not resume_parsing.extraction_v2(db, user_id):
        raise RereadUnavailable("Turn on “Read PDF uploads with the new reader” first")
    if resume.source_media_type not in (document_text.PDF, document_text.DOCX):
        raise RereadUnavailable("Only a PDF or DOCX file can be read again")
    # The library entry may be gone (deleting it keeps the original a version uses): the version's own source path and
    # checksum identify the file either way.
    document = db.get(ResumeDocument, resume.resume_document_id) if resume.resume_document_id else None
    root = (Path(get_settings().upload_dir) / str(user_id)).resolve()
    path = Path(resume.source_storage_path).resolve() if resume.source_storage_path else None
    if path is None or not path.is_file() or not path.is_relative_to(root):
        raise RereadUnavailable("The original file is no longer available: upload it again")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != resume.source_file_hash:
        raise RereadUnavailable("The stored file does not match this resume: upload it again")
    extraction = pipeline.extract(resume.source_media_type, data, learned=store.learned(db, user_id))
    text = clean_raw_text(extraction.text)
    if not text.strip():
        raise RereadUnavailable("No readable text was found in the file")
    if document is not None:
        document.extracted_text = text
    reused = db.scalar(select(func.count(Resume.id)).where(Resume.candidate_profile_id == profile.id,
                                                           Resume.raw_text_hash == sr.text_hash(text))) > 0
    created = record_version(db, profile, text, document=document, source="upload", name=resume.name,
                             extraction=extraction)
    if document is None and created.id != resume.id and not created.source_storage_path:
        created.source_storage_path, created.source_filename = resume.source_storage_path, resume.source_filename
        created.source_media_type, created.source_file_hash = resume.source_media_type, resume.source_file_hash
        db.flush()
    if reused:
        # The same text: record_version kept the existing version and its data. The new reader's structure (its parse
        # view, now recorded) is offered as the comparison a re-parse makes, never applied on its own.
        pending = reparse(db, created)
        if not pending.get("changes"):
            created.pending_reparse = None
            db.flush()
    return created


def save_text_version(db: Session, profile: CandidateProfile, text: str) -> Resume | None:
    """Resume text typed or pasted in Settings: its own version (or the existing one with the same text). It does
    not become active and does not change the master text; the person makes it active when they want it used."""
    text = sr.clean_raw_text(text or "")
    if not text.strip():
        return None
    return _record_text_version(db, profile, text)


def _record_text_version(db: Session, profile: CandidateProfile, text: str,
                         document: ResumeDocument | None = None) -> Resume:
    """record_version that tolerates another worker creating the same version at the same moment."""
    try:
        with db.begin_nested():
            return record_version(db, profile, text, document=document,
                                  source="upload" if document is not None else "manual_text")
    except IntegrityError:
        found = db.scalar(select(Resume).where(Resume.candidate_profile_id == profile.id,
                                               Resume.raw_text_hash == sr.text_hash(text))
                          .order_by(desc(Resume.version)))
        if found is None:
            raise
        return found


def storage_in_use(db: Session, path: str) -> bool:
    """Whether a resume version still references this stored original file."""
    return bool(db.scalar(select(func.count(Resume.id)).where(Resume.source_storage_path == path)))


# ───────────── progress and status ─────────────

def _label(unit: Unit) -> str:
    node = unit.node
    if unit.kind == "bullet":
        return node.get("text", "")[:120]
    for key in ("job_title", "name", "title", "degree", "text", "full_name"):
        if node.get(key):
            extra = node.get("company") or node.get("institution") or ""
            return f"{node[key]}{' · ' + extra if extra else ''}"[:120]
    return sr.SECTION_LABELS.get(unit.section, unit.section)


def counts(units: list[Unit]) -> dict[str, int]:
    out = dict.fromkeys(("VERIFIED", "UNVERIFIED", "EDITED", "REJECTED"), 0)
    for unit in units:
        out[unit.status] = out.get(unit.status, 0) + 1
    return out


def _state(c: dict[str, int]) -> str:
    total = sum(c.values())
    if not total:
        return "EMPTY"
    if c["EDITED"]:
        return "NEEDS_REVIEW"
    if c["VERIFIED"] + c["REJECTED"] == total:
        return "VERIFIED"
    return "PARTIAL" if c["VERIFIED"] + c["REJECTED"] else "UNVERIFIED"


def blocking_items(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [{"item_id": u.item_id, "path": u.path, "section": u.section, "status": u.status, "label": _label(u)}
            for u in iter_units(data) if u.section in sr.REQUIRED_SECTIONS and u.status not in REVIEWED]


def progress(data: dict[str, Any] | None) -> dict[str, Any]:
    units = list(iter_units(data))
    total = counts(units)
    reviewed = total["VERIFIED"] + total["REJECTED"]
    sections = []
    for section in sr.SECTIONS:
        c = counts([u for u in units if u.section == section])
        sections.append({"section": section, "label": sr.SECTION_LABELS[section], "counts": c, "state": _state(c),
                         "required": section in sr.REQUIRED_SECTIONS})
    entries = []
    for group in ("experience", "projects"):
        for item in (data or {}).get(group) or []:
            c = counts([u for u in units if u.item_id == item["id"] or (u.parent is item)])
            entries.append({"section": group, "item_id": item["id"], "label": _label(Unit(group, item["id"], "", item)),
                            "counts": c, "state": _state(c)})
    return {"total": len(units), "counts": total, "reviewed": reviewed,
            "percent": round(100 * reviewed / len(units)) if units else 0,
            "needs_review": total["UNVERIFIED"] + total["EDITED"], "sections": sections, "entries": entries,
            "blocking": blocking_items(data)}


def refresh_status(resume: Resume, invalidated: bool = False) -> None:
    """Resume-level status. VERIFIED only comes from verify_resume; a verified resume whose facts changed, or whose
    required sections are no longer fully reviewed, NEEDS_REVIEW."""
    data = resume.structured_data
    if resume.verification_status in ("VERIFIED", "NEEDS_REVIEW"):
        if invalidated or blocking_items(data):
            resume.verification_status = "NEEDS_REVIEW"
        return
    reviewed = any(u.status in REVIEWED for u in iter_units(data))
    resume.verification_status = "PARTIALLY_VERIFIED" if reviewed else "UNVERIFIED"


# ───────────── verification ─────────────

def _set_status(unit: Unit, status: str) -> None:
    unit.node["verification"] = {"status": status, "updated_at": _iso()}


def set_item_status(db: Session, resume: Resume, item_id: str, status: str, source: str = "USER") -> dict[str, Any]:
    if status not in ("VERIFIED", "REJECTED", "UNVERIFIED"):
        raise ValueError("status must be VERIFIED, REJECTED or UNVERIFIED")
    data = copy.deepcopy(ensure_structured(db, resume))
    unit = sr.find_unit(data, item_id)
    if unit is None:
        raise UnknownItem(item_id)
    old = unit.status
    if old != status:
        _set_status(unit, status)
        record(db, resume, unit.path, CHANGE_FOR_STATUS[status], source, item_id=item_id, old=old, new=status)
        _touch(resume, data)
        refresh_status(resume, invalidated=old in REVIEWED and status == "UNVERIFIED")
        sync_evidence(db, resume)
    return data


def verify_section(db: Session, resume: Resume, section: str, source: str = "USER") -> dict[str, Any]:
    if section not in sr.SECTIONS:
        raise UnknownItem(section)
    data = copy.deepcopy(ensure_structured(db, resume))
    changed = False
    for unit in iter_units(data):
        if unit.section == section and unit.status in ("UNVERIFIED", "EDITED"):
            record(db, resume, unit.path, "VERIFY", source, item_id=unit.item_id, old=unit.status, new="VERIFIED")
            _set_status(unit, "VERIFIED")
            changed = True
    if changed:
        _touch(resume, data)
        refresh_status(resume)
        sync_evidence(db, resume)
    return data


def confidence(db: Session, resume: Resume) -> dict[str, dict[str, Any]]:
    """Each fact's confidence (app.services.resume_extraction.confidence): {} for a version read without V2."""
    from app.services.resume_extraction import store
    from app.services.resume_extraction.confidence import assess

    return assess(resume.structured_data, resume.raw_text or "", store.record(db, resume.id))


def verify_confident(db: Session, resume: Resume, source: str = "USER") -> int:
    """The person accepts every fact still unverified whose confidence is high, in one action. A fact a person edited
    or decided is left alone, and nothing is ever verified without this request. Returns how many were verified."""
    levels = confidence(db, resume)
    data = copy.deepcopy(ensure_structured(db, resume))
    count = 0
    for unit in iter_units(data):
        if unit.status == "UNVERIFIED" and levels.get(unit.item_id, {}).get("level") == "high":
            record(db, resume, unit.path, "VERIFY", source, item_id=unit.item_id, old=unit.status, new="VERIFIED")
            _set_status(unit, "VERIFIED")
            count += 1
    if count:
        _touch(resume, data)
        refresh_status(resume)
        sync_evidence(db, resume)
    return count


def verify_resume(db: Session, resume: Resume, user_id: int | None) -> Resume:
    """Mark the whole resume VERIFIED. Every unit in the required sections must be VERIFIED or REJECTED."""
    from app.services.resume_extraction import store

    data = ensure_structured(db, resume)
    # A heading no table could place must be assigned first: its lines may belong to any section.
    blocking = [{"item_id": f"section:{s['line']}", "label": f"Heading \u201c{s['text']}\u201d: choose its section"}
                for s in store.unplaced(db, resume.id)] + blocking_items(data)
    if blocking:
        raise VerificationBlocked(blocking)
    old = resume.verification_status
    resume.verification_status, resume.verified_at, resume.verified_by = "VERIFIED", utcnow(), user_id
    record(db, resume, "*", "VERIFY_RESUME", "USER", old=old, new="VERIFIED")
    sync_evidence(db, resume)
    return resume


# ───────────── editing ─────────────

def _flat(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {"value": value}


def _changed(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """The keys whose value differs. A key one side lacks equals an empty value, so a field added to the schema later
    (a project's result, say) never reads as an edit of data saved before it existed."""
    def value(content: dict[str, Any], key: str) -> Any:
        v = content.get(key)
        return None if v in ("", [], {}) else v
    return [key for key in sorted(set(old) | set(new)) if value(old, key) != value(new, key)]


def apply_edit(db: Session, resume: Resume, payload: StructuredResumeIn, expected_revision: int,
               source: str = "USER") -> dict[str, Any]:
    """Save edited structured data. Changed VERIFIED/REJECTED units become EDITED; new units start UNVERIFIED."""
    if expected_revision != (resume.revision or 0):
        raise ResumeConflict("This resume changed since you opened it. Reload to see the latest version.")
    before = ensure_structured(db, resume)
    after = materialize(payload, previous=before)
    old_units = {u.item_id: u for u in iter_units(before)}
    new_units = {u.item_id: u for u in iter_units(after)}
    invalidated = False
    for item_id, unit in new_units.items():
        previous = old_units.get(item_id)
        if previous is None:
            record(db, resume, unit.path, "ADD", source, item_id=item_id, new=unit.content())
            continue
        old_content, new_content = _flat(previous.content()), _flat(unit.content())
        diff = _changed(old_content, new_content)
        for key in diff:
            record(db, resume, f"{unit.path}.{key}", "UPDATE", source, item_id=item_id, old=old_content.get(key),
                   new=new_content.get(key))
        if diff and previous.status in ("VERIFIED", "REJECTED", "EDITED"):
            invalidated = invalidated or previous.status in REVIEWED
            _set_status(unit, "EDITED")
        if previous.path != unit.path and not diff:
            record(db, resume, unit.path, "REORDER", source, item_id=item_id, old=previous.path, new=unit.path)
    for item_id, unit in old_units.items():
        if item_id not in new_units:
            record(db, resume, unit.path, "REMOVE", source, item_id=item_id, old=unit.content())
            invalidated = invalidated or unit.status in REVIEWED
    for section in sr.LIST_SECTIONS:
        old_order = [i["id"] for i in before.get(section) or [] if i["id"] in new_units]
        new_order = [i["id"] for i in after.get(section) or [] if i["id"] in old_units]
        if old_order != new_order:
            record(db, resume, section, "REORDER", source, old=old_order, new=new_order)
    if after == before:
        return before
    if resume.pending_reparse:
        resume.pending_reparse = None  # compared against data that no longer exists
        record(db, resume, "*", "REPARSE_KEEP", "SYSTEM", old="pending comparison discarded after an edit")
    _touch(resume, after)
    refresh_status(resume, invalidated=invalidated)
    sync_evidence(db, resume)
    return after


# ───────────── Evidence Bank ─────────────

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", sr.to_plain(text or "").lower()).strip(" .")


def fact_key(company: str, claim: str) -> str:
    return hashlib.sha256(f"{_norm(company)}|{_norm(claim)}".encode()).hexdigest()


def verified_facts(data: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Evidence-ready facts: VERIFIED bullets whose role or project is VERIFIED too."""
    facts = {}
    for unit in iter_units(data):
        if unit.kind != "bullet" or unit.status != "VERIFIED" or unit.parent is None:
            continue
        parent = unit.parent
        if (parent.get("verification") or {}).get("status") != "VERIFIED":
            continue
        pool = list(dict.fromkeys((parent.get("technologies") or []) + (parent.get("skills") or [])))
        if unit.section == "experience":
            company, role = parent.get("company") or "", parent.get("job_title") or ""
        else:
            company, role = "", f"Project: {parent.get('name') or ''}".strip()
        text = unit.node["text"]
        facts[unit.item_id] = {"company": company[:200], "role": role[:200], "claim": text[:2000],
                               "skills": (sr._mentions(text, pool) or pool)[:12],
                               "tags": ["achievement" if ".achievements[" in unit.path else "responsibility"]}
    return facts


def sync_evidence(db: Session, resume: Resume) -> dict[str, int]:
    """Create or update VerifiedEvidence for this version's verified facts; un-verify evidence for the rest."""
    wanted = verified_facts(resume.structured_data)
    rows = db.scalars(select(Evidence).where(Evidence.user_id == resume.user_id, Evidence.source == "resume",
                                             Evidence.resume_id == resume.id)).all()
    by_item = {row.structured_item_id: row for row in rows}
    created = updated = retired = 0
    linked: list[Evidence] = []
    for item_id, fact in wanted.items():
        key = fact_key(fact["company"], fact["claim"])
        row = by_item.get(item_id) or db.scalar(select(Evidence).where(
            Evidence.user_id == resume.user_id, Evidence.source == "resume", Evidence.fact_key == key)
            .order_by(Evidence.id).limit(1))
        if row is None:
            row = Evidence(user_id=resume.user_id, source="resume", verified=True, **fact)
            db.add(row)
            created += 1
        else:
            updated += 1
        for field, value in fact.items():
            setattr(row, field, value)
        row.verified, row.resume_id, row.structured_item_id = True, resume.id, item_id
        row.source_file_hash, row.fact_key = resume.source_file_hash, key
        row.context = f"Verified from resume v{resume.version}"
        linked.append(row)
    for row in rows:
        if row.structured_item_id not in wanted and row.verified:
            row.verified = False
            retired += 1
    db.flush()
    # The evidence this version verifies is tagged to it (AUTO); a person's own tags and removals stay as they are.
    evidence_tags.sync_auto_links(db, resume, {row.id for row in linked})
    db.flush()
    return {"created": created, "updated": updated, "retired": retired}


def _auto_linked(db: Session, resume: Resume) -> set[int]:
    from app.models import EvidenceResumeLink

    return set(db.scalars(select(EvidenceResumeLink.evidence_id).where(
        EvidenceResumeLink.resume_id == resume.id, EvidenceResumeLink.origin == "AUTO",
        EvidenceResumeLink.excluded.is_(False))).all())


# ───────────── re-parse: a comparison, never an overwrite ─────────────

def _parent_path(path: str) -> str:
    return path.rsplit(".", 1)[0] if "." in path else ""


def reparse(db: Session, resume: Resume, use_ai: bool = False) -> dict[str, Any]:
    """Parse the raw text again and store the differences for the user to resolve. Current data is untouched.

    The AI parse is never run here, in the request: `use_ai` queues it (app.services.resume_parsing), and its result
    fills the data while nobody has reviewed it, or arrives as this same comparison."""
    if use_ai:
        from app.services import resume_parsing

        task = resume_parsing.enqueue(db, resume, reason=resume_parsing.USER_REQUEST, run_now=True)
        if task is None:
            raise UnknownItem("AI structuring is off, or this resume has no text")
        db.flush()
        resume_parsing.nudge()
        return {"queued": True, "task_id": task.id}
    ensure_structured(db, resume)
    return compare(db, resume, parse_structured(db, resume), parser="deterministic",
                   parser_version=sr.PARSER_VERSION)


def compare(db: Session, resume: Resume, source: dict[str, Any], *, parser: str, parser_version: str) -> dict[str, Any]:
    """Store the differences between the current structured data and `source` (a parser's output, input shape) as the
    pending comparison the user resolves value by value. Current data is untouched."""
    current = ensure_structured(db, resume)
    proposed = materialize(source)
    old_units = {u.item_id: u for u in iter_units(current)}
    new_units = {u.item_id: u for u in iter_units(proposed)}
    changes: list[dict[str, Any]] = []
    for item_id, unit in new_units.items():
        previous = old_units.get(item_id)
        label = f"{sr.SECTION_LABELS[unit.section]} · {_label(unit)}"
        if previous is None:
            parent_id = unit.parent["id"] if unit.parent is not None else None
            if parent_id is not None and parent_id not in old_units:
                continue  # comes with its new parent
            changes.append({"type": "add", "path": unit.path, "item_id": item_id, "section": unit.section,
                            "parent_id": parent_id, "list": unit.path.rsplit(".", 1)[-1].split("[")[0]
                            if parent_id else unit.section, "label": label, "current": None,
                            "proposed": copy.deepcopy(unit.node), "current_status": None})
            continue
        old_content, new_content = _flat(previous.content()), _flat(unit.content())
        for key in _changed(old_content, new_content):
            changes.append({"type": "update", "path": f"{previous.path}.{key}", "item_id": item_id, "field": key,
                            "section": unit.section, "label": f"{label} · {key.replace('_', ' ')}",
                            "current": old_content.get(key), "proposed": new_content.get(key),
                            "current_status": previous.status})
    for item_id, unit in old_units.items():
        if item_id not in new_units and (unit.parent is None or unit.parent["id"] in new_units):
            changes.append({"type": "remove", "path": unit.path, "item_id": item_id, "section": unit.section,
                            "label": f"{sr.SECTION_LABELS[unit.section]} · {_label(unit)}",
                            "current": unit.content(), "proposed": None, "current_status": unit.status})
    for index, change in enumerate(changes):
        change["id"] = f"chg_{index + 1}"
    resume.pending_reparse = {"parser": parser, "parser_version": parser_version, "created_at": _iso(),
                              "base_revision": resume.revision or 0, "changes": changes}
    resume.updated_at = utcnow()
    db.flush()
    return resume.pending_reparse


def resolve_reparse(db: Session, resume: Resume, decisions: dict[str, str]) -> dict[str, Any]:
    """Apply the user's choice for each difference: "use_new" takes the parser value, anything else keeps the
    current one. Accepted values are UNVERIFIED: a parser result is never trusted on its own."""
    pending = resume.pending_reparse
    if not pending:
        raise UnknownItem("No re-parse comparison is waiting")
    if pending.get("base_revision") != (resume.revision or 0):
        resume.pending_reparse = None
        raise ResumeConflict("The resume changed after the re-parse. Re-parse again to compare.")
    data = copy.deepcopy(resume.structured_data)
    invalidated = accepted = 0
    for change in pending["changes"]:
        if decisions.get(change["id"]) != "use_new":
            record(db, resume, change["path"], "REPARSE_KEEP", "USER", item_id=change["item_id"],
                   old=change["current"], new=change["proposed"])
            continue
        unit = sr.find_unit(data, change["item_id"])
        if change["type"] == "update" and unit is not None:
            unit.node[change["field"]] = change["proposed"]
        elif change["type"] == "add":
            node = change["proposed"]
            parent = sr.find_unit(data, change["parent_id"]) if change["parent_id"] else None
            if change["parent_id"] and parent is None:
                continue
            target = parent.node.setdefault(change["list"], []) if parent else data.setdefault(change["section"], [])
            if not isinstance(target, list):
                continue
            target.append(node)
            unit = sr.find_unit(data, change["item_id"])
        elif change["type"] == "remove" and unit is not None:
            container = unit.parent.get(change["path"].rsplit(".", 1)[-1].split("[")[0]) if unit.parent else \
                data.get(unit.section)
            if isinstance(container, list):
                container[:] = [n for n in container if n.get("id") != change["item_id"]]
            elif unit.section == "summary":
                data["summary"]["text"] = ""
            invalidated += unit.status in REVIEWED
            unit = None
        if unit is not None:
            invalidated += unit.status in REVIEWED
            for child in [unit] + [u for u in iter_units(data) if u.parent is unit.node] if change["type"] == "add" else [unit]:
                _set_status(child, "UNVERIFIED")
        record(db, resume, change["path"], "REPARSE_ACCEPT", "PARSER", item_id=change["item_id"],
               old=change["current"], new=change["proposed"])
        accepted += 1
    resume.pending_reparse = None
    if accepted:
        normalized = materialize(StructuredResumeIn.model_validate(data), previous=data)
        resume.parser, resume.parser_version = pending.get("parser"), pending.get("parser_version")
        _touch(resume, normalized)
        refresh_status(resume, invalidated=bool(invalidated))
        sync_evidence(db, resume)
    db.flush()
    return resume.structured_data


def changes(db: Session, resume: Resume, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    query = select(ResumeDataChange).where(ResumeDataChange.resume_id == resume.id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(desc(ResumeDataChange.id)).limit(limit).offset(offset)).all()
    return {"total": total, "items": [
        {"id": r.id, "field_path": r.field_path, "item_id": r.item_id, "old_value": r.old_value,
         "new_value": r.new_value, "change_type": r.change_type, "source": r.source,
         "changed_at": r.changed_at.isoformat() if r.changed_at else None} for r in rows]}
