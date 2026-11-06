"""Approved packets waiting to be submitted, what happens before a submission starts, and what happened.

A job is ready to apply to when its packet is approved, nothing was submitted for it yet, the approved resume's
reviewer score is above the floor, it has an application URL, no other posting of the same job was already applied to, and its last attempt
did not stop at a blocker that is still open. Recording a submission goes through `workflow.mark_applied`, the
same path the Apply screen uses, so a job is only ever applied to once.

Every client that submits — Claude Desktop over MCP, the Apply kit, and (from Phase 6) the execution worker — shares
this module (Phase 5):

    begin_submission(job)          → under the account lock: readiness (approved digest included), duplicates, then a
                                     claim on every alias of the posting (`posting_identity`) with a token
    the client submits outside     → nothing here holds a transaction meanwhile
    record_submission(job, token)  → the one recorder: lock, digest, duplicates, the claim; mark_applied; RECORDED
    record_blocker(job, token)     → nothing was submitted: the claim is released
    expire_claims()                → a claim past its lease is UNCERTAIN, never free; a person resolves it

`lock_application` serialises the *recording* of results; the claim is what keeps two clients from starting the same
submission. A submission made entirely outside Job Hunt OS cannot be serialised: it is recorded (or refused) when a
person reports it.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import desc, nulls_last, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.models import (
    HELD_APPLICATION_CLAIM_STATUSES,
    Application,
    ApplicationClaim,
    ApplicationClaimKey,
    ApplicationEvent,
    Job,
    ResumeVariant,
    User,
)
from app.services import approval, knowledge
from app.services.exports import (
    DOCX_MEDIA_TYPE,
    markdown_to_docx,
    markdown_to_simple_pdf,
    packet_filename,
    packet_slug,
)
from app.services.workflow import get_profile, mark_applied
from app.settings import get_settings

# The approved resume must have been scored above this by the reviewer. A caller may only raise it.
MIN_REVIEWER_SCORE = 60.0
BLOCKER_REASONS = ("captcha", "login_required", "account_required", "needs_human_answer", "missing_document",
                   "posting_closed", "form_error", "other")
FILE_FORMATS = ("pdf", "docx")
MEDIA_TYPES = {"pdf": "application/pdf", "docx": DOCX_MEDIA_TYPE}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def apply_url(job: Job) -> str | None:
    """Where to apply: the employer's own form when the board gave one, else the posting."""
    return job.direct_apply_url or job.url or job.canonical_apply_url or job.source_url or None


def approved_resume(db: Session, application: Application | None) -> ResumeVariant | None:
    return db.get(ResumeVariant, application.approved_resume_id) if application and application.approved_resume_id else None


def _url_key(url: str | None) -> str | None:
    """`url:<canonical>` for a posting or application URL; None for a site's root page, which many postings share."""
    from app.services.apify_adapters import canonical_url

    canon = canonical_url(url)
    if not canon or urlparse(canon).path in ("", "/"):
        return None
    return f"url:{canon}"


def posting_identity(job: Job) -> list[str]:
    """Every alias key of the posting, strongest first; the first is its canonical identity.

    `board:<board>:<id>` (the board's own id), then `url:` for each URL the posting is known by — posting, source,
    direct apply and the stored canonical ones — normalised the same way (https, no www, no fragment, no trailing
    slash, no tracking parameters), so an alias of one job collides with another's; then the stored
    company/title/city `dedupe:` key (never computed here: two different openings may share a title and city).
    """
    keys: list[str] = []
    if job.source_board and job.source_job_id:
        keys.append(f"board:{job.source_board.strip().lower()}:{job.source_job_id.strip()}"[:800])
    for url in (job.canonical_apply_url, job.direct_apply_url, job.canonical_url, job.url, job.source_url):
        key = _url_key(url)
        if key:
            keys.append(key[:800])
    if job.dedupe_key:
        keys.append(f"dedupe:{job.dedupe_key}"[:800])
    if not keys:
        keys.append(f"job:{job.id}")  # nothing to match on: the job itself is the posting
    return list(dict.fromkeys(keys))


def applied_duplicate(db: Session, job: Job) -> Job | None:
    """Another saved posting of the same job already applied to: the same stored apply URL, posting URL or
    company/title/city key (as before), or any alias key `posting_identity` derives (another URL of the same posting,
    the board's own id). Jobs whose canonical columns were never filled (manual, Lever, Greenhouse) match too."""
    same = [column == value for column, value in ((Job.canonical_apply_url, job.canonical_apply_url),
                                                  (Job.canonical_url, job.canonical_url),
                                                  (Job.dedupe_key, job.dedupe_key)) if value]
    if same:
        found = db.scalar(select(Job).join(Application, Application.job_id == Job.id)
                          .where(Job.id != job.id, Job.user_id == job.user_id, Application.status == "applied",
                                 or_(*same)).order_by(Job.id).limit(1))
        if found is not None:
            return found
    keys = set(posting_identity(job))
    applied = db.scalars(select(Job).join(Application, Application.job_id == Job.id)
                         .where(Job.id != job.id, Job.user_id == job.user_id, Application.status == "applied")
                         .order_by(Job.id)).all()  # every one: a truncated scan would miss a duplicate
    return next((other for other in applied if keys & set(posting_identity(other))), None)


def open_blocker(db: Session, application: Application) -> dict[str, Any] | None:
    """The blocker the last attempt stopped at, unless it has since been cleared.

    A `needs_human_answer` blocker clears itself once a person has answered every question it names.
    """
    blocker = application.apply_blocker
    if not blocker:
        return None
    if blocker.get("reason") == "needs_human_answer" and blocker.get("question_ids") \
            and knowledge.answered(db, blocker["question_ids"]):
        return None
    return blocker


def check_threshold(reviewer_score_above: float | None) -> float:
    threshold = MIN_REVIEWER_SCORE if reviewer_score_above is None else float(reviewer_score_above)
    if threshold < MIN_REVIEWER_SCORE:
        raise ValueError(f"reviewer_score_above cannot be lower than {MIN_REVIEWER_SCORE:g}")
    return threshold


def ready_to_apply(db: Session, *, reviewer_score_above: float | None = None, include_blocked: bool = False,
                   company: str | None = None) -> list[dict[str, Any]]:
    """Every approved, unsubmitted job above the reviewer floor, best reviewed first."""
    threshold = check_threshold(reviewer_score_above)
    query = (select(Job, Application, ResumeVariant)
             .join(Application, Application.job_id == Job.id)
             .join(ResumeVariant, ResumeVariant.id == Application.approved_resume_id)
             .where(Job.status == "approved", Application.status == "approved", Application.applied_at.is_(None),
                    ResumeVariant.reviewer_score > threshold))
    if company:
        query = query.where(Job.company.ilike(f"%{company.strip()}%"))
    query = query.order_by(desc(ResumeVariant.reviewer_score), nulls_last(desc(Job.fit_score)), Job.id)
    out = []
    for job, application, resume in db.execute(query).all():
        if not apply_url(job) or applied_duplicate(db, job) is not None:
            continue
        if not approval.verify(db, application).ok:
            continue  # changed after approval: it needs a new approval before anyone applies with it
        blocker = open_blocker(db, application)
        if blocker and not include_blocked:
            continue
        claim = open_claim(db, job)
        if claim is not None and not include_blocked:
            continue  # someone is submitting it now, or must say whether they did
        out.append({"job": job, "application": application, "resume": resume, "blocker": blocker, "claim": claim})
    return out


def not_ready_reason(db: Session, job: Job, *, reviewer_score_above: float | None = None) -> str | None:
    """Why this job's packet may not be used for an application now, or None when it may."""
    application = job.application
    if application is not None and application.status == "applied":
        return f"Already applied on {application.applied_at:%Y-%m-%d}" if application.applied_at else "Already applied"
    if application is None or application.status != "approved" or job.status != "approved":
        return f"Job is {job.status}, not approved for application"
    resume = approved_resume(db, application)
    if resume is None or resume.reviewer_score is None:
        return "The approved packet has no reviewed resume"
    checked = approval.verify(db, application)
    if not checked.ok:
        return checked.reason
    threshold = check_threshold(reviewer_score_above)
    if resume.reviewer_score <= threshold:
        return f"Reviewer score {resume.reviewer_score:g} is not above {threshold:g}"
    duplicate = applied_duplicate(db, job)
    if duplicate is not None:
        return f"Already applied to the same posting as job {duplicate.id} ({duplicate.company})"
    if not apply_url(job):
        return "The job has no application URL; add the posting URL in Job Hunt OS first"
    return None


def packet_folder(job: Job, *, draft: bool = False) -> Path:
    """MCP_PACKET_DIR/<account>/<job>/ for an approved packet; its drafts/ subfolder for a version nobody approved."""
    folder = Path(get_settings().mcp_packet_dir).expanduser().resolve() / str(job.user_id) / str(job.id)
    return folder / "drafts" if draft else folder


def packet_files(db: Session, job: Job, resume: ResumeVariant, formats: list[str], *,
                 include_content: bool = False, cover_letter: str | None = None,
                 draft: bool = False) -> list[dict[str, Any]]:
    """Render the resume (and, given its text, the cover letter) and write them where a browser upload can pick
    them up.

    Files go to MCP_PACKET_DIR/<account>/<job>/ under the same names the Apply screen downloads, each tagged with its
    `role` (resume or cover_letter). A `draft` (a version that is not the approved packet) goes to the drafts/
    subfolder with "-draft" in its name, so it is never mistaken for, or overwrites, an approved file.
    """
    renderers = {"pdf": markdown_to_simple_pdf, "docx": markdown_to_docx}
    slug = packet_slug(get_profile(db).name, job.company)
    folder = packet_folder(job, draft=draft)
    folder.mkdir(parents=True, exist_ok=True)
    parts = [("resume", kind, resume.markdown, packet_filename(slug, kind, resume.version, draft=draft))
             for kind in formats]
    if (cover_letter or "").strip():
        parts += [("cover_letter", kind, cover_letter, packet_filename(slug, f"cover_letter_{kind}", draft=draft))
                  for kind in formats]
    out = []
    for role, kind, text, name in parts:
        data = renderers[kind](text)
        path = folder / name
        path.write_bytes(data)
        item = {"role": role, "format": kind, "filename": path.name, "path": str(path),
                "media_type": MEDIA_TYPES[kind], "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        if include_content:
            item["content_base64"] = base64.b64encode(data).decode()
        out.append(item)
    return out


def record_blocker(db: Session, job: Job, reason: str, *, detail: str = "", question_ids: list[int] | None = None,
                   recorded_by: str = "claude_desktop", claim_token: str | None = None) -> dict[str, Any]:
    """The attempt stopped before anything was submitted. A claim it held (`claim_token`) is released."""
    if reason not in BLOCKER_REASONS:
        raise ValueError(f"blocker_reason must be one of {', '.join(BLOCKER_REASONS)}")
    question_ids = list(dict.fromkeys(question_ids or []))
    if reason == "needs_human_answer" and not question_ids:
        raise ValueError("needs_human_answer needs the question_ids request_human_answer returned")
    claim = None
    if claim_token:
        claim = _claim_by_token(db, job, claim_token)
        if claim is None or claim.status != CLAIMED:
            raise ValueError("claim_token is not an open claim on this job")
        if _lease_ran_out(claim):
            raise ValueError(LEASE_RAN_OUT)
    blocker = {"reason": reason, "detail": (detail or "").strip()[:2000] or None, "question_ids": question_ids,
               "recorded_by": recorded_by, "recorded_at": _now().isoformat()}
    application = job.application
    application.apply_blocker = blocker
    if claim is not None:
        _finish(claim, RELEASED, f"blocked: {reason}")
    db.add(ApplicationEvent(application=application, kind="blocker", title=f"Application blocked: {reason.replace('_', ' ')}",
                            detail=blocker["detail"] or "", meta={k: blocker[k] for k in ("reason", "question_ids")}))
    db.commit()
    return blocker


# ───────────── pre-submit claims ─────────────

CLAIMED, UNCERTAIN, RECORDED, RELEASED = "CLAIMED", "UNCERTAIN", "RECORDED", "RELEASED"
CLAIM_CLIENTS = ("mcp", "apply_kit", "worker")
SUBMITTED_BY = {"mcp": "Claude Desktop", "apply_kit": "you", "worker": "the application worker"}
SUBMITTED_VIA = {"mcp": "claude_desktop", "apply_kit": "apply_kit", "worker": "application_worker"}


@dataclass
class ClaimResult:
    """What begin_submission decided. `status`: CLAIMED (token issued), NOT_READY, DUPLICATE (already applied, here
    or on another posting of the same job), CLAIM_CONFLICT (another client is submitting it now), UNCERTAIN (an
    earlier claim's outcome is unknown: a person must say whether it was submitted) or ROUTE_HELD (a guarded
    execution owns this posting's submission: no other client may start one beside it)."""

    status: str
    claim: ApplicationClaim | None = None
    reason: str | None = None
    conflict_job_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == CLAIMED


@dataclass
class RecordResult:
    """What record_submission decided. `status`: SUBMITTED, ALREADY_APPLIED, NOT_APPROVED, PACKET_CHANGED,
    DUPLICATE_APPLICATION, CLAIM_CONFLICT or INVALID_CLAIM."""

    status: str
    application: Application | None = None
    reason: str | None = None
    duplicate_job_id: int | None = None
    claimed: bool = False

    @property
    def ok(self) -> bool:
        return self.status in ("SUBMITTED", "ALREADY_APPLIED")


LEASE_RAN_OUT = ("This submission's reservation ran out, so whether it was submitted is unknown; the candidate says "
                 "what happened (Apply kit)")


def _lease_ran_out(claim: ApplicationClaim, now: datetime | None = None) -> bool:
    return claim.lease_expires_at is not None and claim.lease_expires_at < (now or _now())


def claim_lease() -> timedelta:
    return timedelta(minutes=max(1, get_settings().application_claim_lease_minutes))


def _finish(claim: ApplicationClaim, status: str, resolution: str, now: datetime | None = None) -> None:
    claim.status, claim.resolved_at, claim.resolution = status, now or _now(), resolution[:200]
    for key in claim.keys:
        key.held = status in HELD_APPLICATION_CLAIM_STATUSES


def _claim_by_token(db: Session, job: Job, token: str) -> ApplicationClaim | None:
    if not isinstance(token, str) or not token.strip():
        return None
    return db.scalar(select(ApplicationClaim).where(ApplicationClaim.token == token.strip(),
                                                    ApplicationClaim.job_id == job.id,
                                                    ApplicationClaim.user_id == job.user_id)
                     .execution_options(populate_existing=True).limit(1))


def held_claims(db: Session, job: Job) -> list[ApplicationClaim]:
    """The claims holding any alias of this posting (any job of the account), oldest first."""
    keys = posting_identity(job)
    ids = db.scalars(select(ApplicationClaimKey.claim_id).where(ApplicationClaimKey.user_id == job.user_id,
                                                                ApplicationClaimKey.held.is_(True),
                                                                ApplicationClaimKey.key.in_(keys))).all()
    if not ids:
        return []
    return list(db.scalars(select(ApplicationClaim).where(ApplicationClaim.id.in_(set(ids)))
                           .execution_options(populate_existing=True).order_by(ApplicationClaim.id)).all())


def open_claim(db: Session, job: Job) -> ApplicationClaim | None:
    """The CLAIMED or UNCERTAIN claim holding this posting, if any (for screens and the ready queue)."""
    return next((c for c in held_claims(db, job) if c.status in (CLAIMED, UNCERTAIN)), None)


def expire_claims(db: Session, *, now: datetime | None = None, user_id: int | None = None, limit: int = 500) -> int:
    """CLAIMED claims past their lease become UNCERTAIN: the client may have submitted, so the posting stays held and
    a person says what happened (resolve_uncertain). Never released automatically. Commits."""
    now = now or _now()
    query = select(ApplicationClaim).where(ApplicationClaim.status == CLAIMED, ApplicationClaim.lease_expires_at < now)
    if user_id is not None:
        query = query.where(ApplicationClaim.user_id == user_id)
    rows = db.scalars(query.order_by(ApplicationClaim.id).limit(limit)).all()
    expired = 0
    for claim in rows:
        # Only while it is still CLAIMED: a release or a record committed since the select wins (its keys are
        # already settled), and this sweep never turns a finished claim back into an unknown one.
        moved = db.execute(update(ApplicationClaim)
                           .where(ApplicationClaim.id == claim.id, ApplicationClaim.status == CLAIMED,
                                  ApplicationClaim.lease_expires_at < now)
                           .values(status=UNCERTAIN)
                           .execution_options(synchronize_session=False)).rowcount
        if not moved:
            continue
        expired += 1
        set_committed_value(claim, "status", UNCERTAIN)  # what the row now holds; no second UPDATE
        application = db.get(Application, claim.application_id) if claim.application_id else None
        if application is not None:
            db.add(ApplicationEvent(application=application, kind="note", title="Submission outcome unknown",
                                    detail="A submission was started and never reported. Say whether it was submitted "
                                           "before this posting can be applied to again.",
                                    meta={"claim_id": claim.id, "client": claim.client}))
    if rows:
        db.commit()
    return expired


def _conflict(job: Job, claims: list[ApplicationClaim], *, token: str | None = None) -> ClaimResult | None:
    for claim in claims:
        if token is not None and claim.token == token:
            continue
        if claim.status == RECORDED:
            return ClaimResult("DUPLICATE", reason=f"Already applied to this posting (job {claim.job_id})",
                               conflict_job_id=claim.job_id)
        if claim.status == UNCERTAIN:
            return ClaimResult(UNCERTAIN, claim=claim, conflict_job_id=claim.job_id,
                               reason="An earlier submission of this posting was started and never reported; say "
                                      "whether it was submitted first")
        if claim.status == CLAIMED:
            where = "this job" if claim.job_id == job.id else f"job {claim.job_id}"
            return ClaimResult("CLAIM_CONFLICT", claim=claim, conflict_job_id=claim.job_id,
                               reason=f"A submission of this posting is already in progress ({where}, started by "
                                      f"{SUBMITTED_BY.get(claim.client, claim.client)})")
    return None


def _new_claim(db: Session, job: Job, application: Application, *, client: str, status: str,
               now: datetime) -> ApplicationClaim:
    packet = approval.live(db, application)
    keys = posting_identity(job)
    claim = ApplicationClaim(user_id=job.user_id, job_id=job.id, application_id=application.id,
                             approved_packet_id=packet.id if packet else None,
                             packet_digest=packet.digest if packet else None, identity=keys[0],
                             token=secrets.token_hex(24), client=client, status=status, claimed_at=now,
                             lease_expires_at=now + claim_lease())
    claim.keys = [ApplicationClaimKey(user_id=job.user_id, key=key, held=True) for key in keys]
    if status == RECORDED:
        claim.resolved_at, claim.resolution = now, "recorded without a claim"
    db.add(claim)
    return claim


def guarded_hold(db: Session, job: Job) -> Any | None:
    """The active GUARDED_AUTO execution that owns this posting (this job or another posting of it), if any. While
    one does, Claude Desktop and the Apply kit may not start a submission or be issued the packet (route exclusivity,
    ADR-033): two submitters must never work on the same posting at once, and MCP clicks outside Job Hunt OS."""
    from app.models import ACTIVE_APPLICATION_EXECUTION_TASK_STATUSES, ApplicationExecutionTask

    q = ApplicationExecutionTask
    rows = db.scalars(select(q).where(q.user_id == job.user_id, q.mode == "GUARDED_AUTO",
                                      q.status.in_(ACTIVE_APPLICATION_EXECUTION_TASK_STATUSES)).limit(100)).all()
    if not rows:
        return None
    keys = set(posting_identity(job))
    for task in rows:
        other = job if task.job_id == job.id else db.get(Job, task.job_id)
        if other is not None and (other.id == job.id or keys & set(posting_identity(other))):
            return task
    return None


ROUTE_HELD = "ROUTE_HELD"
ROUTE_HELD_REASON = ("The application worker is submitting this posting in guarded mode; wait for it to finish, or "
                     "stop it in Running workers first")


def begin_submission(db: Session, job: Job, *, client: str, now: datetime | None = None,
                     execution_id: int | None = None) -> ClaimResult:
    """Reserve this posting for one submission before anything external starts. Commits.

    Under the account lock: readiness exactly as packet issuance checks it (approved, the approved digest unchanged,
    reviewer floor, apply URL, no applied duplicate), then every alias of the posting must be free. The claim holds
    them all (unique per account while held), so a second client — on this job, a duplicate job row or another URL of
    the same posting — gets CLAIM_CONFLICT, never a second token. Nothing here waits for a browser."""
    if client not in CLAIM_CLIENTS:
        raise ValueError(f"client must be one of {', '.join(CLAIM_CLIENTS)}")
    now = now or _now()
    expire_claims(db, now=now, user_id=job.user_id)
    application = lock_application(db, job)
    if client != "worker" and guarded_hold(db, job) is not None:
        db.rollback()
        return ClaimResult(ROUTE_HELD, reason=ROUTE_HELD_REASON)
    reason = not_ready_reason(db, job)
    if reason is not None:
        db.rollback()
        duplicate = applied_duplicate(db, job) if application is not None else None
        status = "DUPLICATE" if (duplicate is not None or (application and application.status == "applied")) \
            else "NOT_READY"
        return ClaimResult(status, reason=reason, conflict_job_id=duplicate.id if duplicate else None)
    conflict = _conflict(job, held_claims(db, job))
    if conflict is not None:
        db.rollback()
        return conflict
    try:
        with db.begin_nested():
            claim = _new_claim(db, job, application, client=client, status=CLAIMED, now=now)
            claim.execution_id = execution_id
            db.flush()
    except IntegrityError:  # another session claimed an alias at the same moment (SQLite has no row locks)
        db.rollback()
        return _conflict(job, held_claims(db, job)) or ClaimResult(
            "CLAIM_CONFLICT", reason="A submission of this posting is already in progress")
    db.add(ApplicationEvent(application=application, kind="note", title="Submission started",
                            detail=f"Started by {SUBMITTED_BY[client]}",
                            meta={"claim_id": claim.id, "client": client, "identity": claim.identity}))
    db.commit()
    return ClaimResult(CLAIMED, claim=claim)


def _intent(db: Session, claim: ApplicationClaim) -> Any | None:
    from app.models import SubmissionIntent

    return db.scalar(select(SubmissionIntent).where(SubmissionIntent.claim_id == claim.id)
                     .execution_options(populate_existing=True).limit(1))


CLICKED = ("A final Submit may have been clicked for this claim, so whether it was submitted is a fact to establish, "
           "not something to release; say what happened (Apply kit)")


def release_claim(db: Session, job: Job, token: str, reason: str) -> ApplicationClaim:
    """The client says nothing was submitted: free the posting. Only an open (CLAIMED) claim; an UNCERTAIN one is a
    person's to resolve, and so is one whose SubmissionIntent got as far as the click. Commits."""
    from app.models import CLICKED_INTENT_STATUSES

    lock_application(db, job)
    claim = _claim_by_token(db, job, token)
    if claim is None or claim.status != CLAIMED:
        db.rollback()
        raise ValueError("claim_token is not an open claim on this job")
    if _lease_ran_out(claim):
        # The sweep may not have run yet: past its lease the outcome is unknown whatever the status column says.
        db.rollback()
        raise ValueError(LEASE_RAN_OUT)
    from app.services import submission

    intent = _intent(db, claim)
    if intent is not None and intent.status in CLICKED_INTENT_STATUSES:
        db.rollback()
        raise ValueError(CLICKED)
    # ARMED → ABORTED in one conditional UPDATE: a worker committing CLICKING at this moment wins or loses cleanly.
    if intent is not None and intent.status != "ABORTED" and not submission.move(
            db, intent.id, "ABORTED", only_from=("ARMED",), resolved_at=_now(), reason=f"released: {reason}"[:300]):
        db.rollback()
        raise ValueError(CLICKED)
    _finish(claim, RELEASED, f"not submitted: {(reason or 'no reason given').strip()}")
    db.commit()
    return claim


def mark_uncertain(db: Session, claim_id: int, reason: str, *, now: datetime | None = None) -> bool:
    """A client that may have submitted cannot tell whether it did (the confirmation was ambiguous, the browser
    died after the click): the claim becomes UNCERTAIN at once instead of waiting for its lease, so nobody starts
    another submission of the posting until a person says what happened. Only from CLAIMED. Does not commit."""
    now = now or _now()
    moved = db.execute(update(ApplicationClaim).where(ApplicationClaim.id == claim_id,
                                                      ApplicationClaim.status == CLAIMED)
                       .values(status=UNCERTAIN, resolution=reason[:200])
                       .execution_options(synchronize_session=False)).rowcount
    if moved:
        claim = db.get(ApplicationClaim, claim_id, populate_existing=True)
        application = db.get(Application, claim.application_id) if claim.application_id else None
        if application is not None:
            db.add(ApplicationEvent(application=application, kind="note", title="Submission outcome unknown",
                                    detail=f"{reason} Say whether it was submitted before this posting can be "
                                           "applied to again."[:1000],
                                    meta={"claim_id": claim_id, "client": claim.client}))
    return bool(moved)


def resolve_uncertain(db: Session, job: Job, claim_id: int, *, submitted: bool,
                      confirmation_id: str | None = None) -> RecordResult | ApplicationClaim:
    """A person says what happened to a submission whose outcome is unknown (UNCERTAIN, or still CLAIMED): submitted →
    recorded through record_submission; not submitted → released. Commits."""
    claim = db.scalar(select(ApplicationClaim).where(ApplicationClaim.id == claim_id,
                                                     ApplicationClaim.job_id == job.id,
                                                     ApplicationClaim.user_id == job.user_id))
    if claim is None or claim.status not in (CLAIMED, UNCERTAIN):
        raise ValueError("There is no open submission to resolve for this job")
    if submitted:
        result = record_submission(db, job, claim_token=claim.token, client=claim.client,
                                   confirmation_id=confirmation_id, resolving=True)
        if result.status == "SUBMITTED":
            _settle_intent(db, claim_id, "RESOLVED_SUBMITTED", "a person said it was submitted")
        return result
    from app.services.lifecycle import PERSON, parse_state
    from app.services.lifecycle import WorkflowState as S
    from app.services.work_queue import transition

    lock_application(db, job)
    claim = db.get(ApplicationClaim, claim_id, populate_existing=True)
    if claim is None or claim.status not in (CLAIMED, UNCERTAIN):
        # Recorded (or released) by its client since it was read: a RECORDED claim keeps its keys for good.
        db.rollback()
        raise ValueError("There is no open submission to resolve for this job")
    _finish(claim, RELEASED, "a person said it was not submitted")
    _settle_intent(db, claim_id, "RESOLVED_NOT_SUBMITTED", "a person said it was not submitted", commit=False)
    if job.state and parse_state(job.state) is S.APPLYING:
        # The one move only a person makes (lifecycle.PERSON_ONLY): back to the approved packet.
        transition(db, job, S.READY_TO_APPLY, actor=PERSON, reason="The candidate said the submission was not sent")
    db.commit()
    return claim


def _settle_intent(db: Session, claim_id: int, status: str, reason: str, *, commit: bool = True) -> None:
    from app.models import SubmissionIntent

    intent = db.scalar(select(SubmissionIntent).where(SubmissionIntent.claim_id == claim_id).limit(1))
    if intent is not None and intent.status not in ("CONFIRMED", "RESOLVED_SUBMITTED", "RESOLVED_NOT_SUBMITTED"):
        intent.status, intent.resolved_at, intent.reason = status, _now(), reason[:300]
        if commit:
            db.commit()


def record_submission(db: Session, job: Job, *, claim_token: str | None = None, client: str = "mcp",
                      confirmation_id: str | None = None, submitted_by: str | None = None,
                      submitted_via: str | None = None, resolving: bool = False,
                      evidence: dict[str, Any] | None = None) -> RecordResult:
    """The one place a submission is recorded (MCP, the Apply kit, `POST /api/jobs/{id}/applied`, a person resolving).

    Under `lock_application`: an already applied job is ALREADY_APPLIED (idempotent); otherwise the application must
    be approved and its packet still the approved one, no other posting of the job applied to, and the posting not
    held by another client's claim. With `claim_token` the claim must be this job's open claim for the same approved
    digest. Without one (older MCP clients, a person's own report) the result is recorded as unclaimed. Either way the
    posting's alias keys stay held (RECORDED) for good. `mark_applied` commits everything at once, including the
    canonical READY_TO_APPLY → APPLYING → APPLIED transitions.

    `evidence` is only ever passed by the guarded worker after its ConfirmationVerifier found the site's proof
    ({"kind": "verified_browser", "facts": …}), and only with its own claim token; every other report is `reported`."""
    if evidence is not None and (client != "worker" or not claim_token):
        raise ValueError("Only the application worker, holding its claim, records a verified submission")
    confirmation_id = (confirmation_id or "").strip()[:200] or None
    now = _now()
    application = lock_application(db, job)
    if application is not None and application.status == "applied":
        db.rollback()
        return RecordResult("ALREADY_APPLIED", application=application)
    if application is None or application.status != "approved" or job.status != "approved":
        db.rollback()
        return RecordResult("NOT_APPROVED", reason=f"Job {job.id} is {job.status}, not approved for application")
    checked = approval.verify(db, application)
    if not checked.ok:
        db.rollback()
        return RecordResult("PACKET_CHANGED", reason=checked.reason)
    claim = None
    if claim_token:
        claim = _claim_by_token(db, job, claim_token)
        allowed = (CLAIMED, UNCERTAIN) if resolving else (CLAIMED,)
        if claim is None or claim.status not in allowed:
            db.rollback()
            return RecordResult("INVALID_CLAIM", reason="claim_token is not an open claim on this job")
        if claim.packet_digest != (checked.packet.digest if checked.packet else None):
            db.rollback()
            return RecordResult("PACKET_CHANGED", reason="The approved packet changed after the submission started")
        client = claim.client
    duplicate = applied_duplicate(db, job)
    if duplicate is not None:
        db.rollback()
        return RecordResult("DUPLICATE_APPLICATION", duplicate_job_id=duplicate.id,
                            reason=f"Already applied to the same posting as job {duplicate.id} ({duplicate.company})")
    conflict = _conflict(job, held_claims(db, job), token=claim.token if claim else None)
    if conflict is not None:
        db.rollback()
        status = "DUPLICATE_APPLICATION" if conflict.status == "DUPLICATE" else "CLAIM_CONFLICT"
        return RecordResult(status, reason=conflict.reason, duplicate_job_id=conflict.conflict_job_id)
    try:
        with db.begin_nested():
            if claim is not None:
                _finish(claim, RECORDED, "submitted" + (" (resolved by a person)" if resolving else ""), now)
            else:
                claim = _new_claim(db, job, application, client=client, status=RECORDED, now=now)
            db.flush()
    except IntegrityError:
        db.rollback()
        return RecordResult("DUPLICATE_APPLICATION", reason="Another posting of this job was recorded at the same time")
    tokenless = not claim_token
    application = mark_applied(db, job, submitted_by=submitted_by or SUBMITTED_BY.get(client, "you"),
                               submitted_via=submitted_via or SUBMITTED_VIA.get(client, client),
                               confirmation_id=confirmation_id,
                               claim={"claim_id": claim.id, "claim": "unclaimed" if tokenless else "claimed",
                                      "client": client},
                               evidence=evidence if not resolving else None)
    return RecordResult("SUBMITTED", application=application, claimed=not tokenless)


def lock_application(db: Session, job: Job) -> Application | None:
    """Re-read the application under row locks, so two agents cannot both record a submission.

    The account row is locked first: duplicate postings are different jobs, so a lock on this job's application
    alone would let two sessions each pass the duplicate check before either commits. Submissions for one account
    are therefore recorded one at a time (SQLite, which has no row locks, serialises writes anyway).
    """
    db.execute(select(User.id).where(User.id == job.user_id).with_for_update())
    return db.scalar(select(Application).where(Application.job_id == job.id).with_for_update()
                     .execution_options(populate_existing=True).limit(1))
