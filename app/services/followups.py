"""Follow-ups after a submission: what is due, a person's own draft, and recording what the person sent or skipped.

    due(db, within_days, limit)          → drafts due in the window, and submitted applications whose follow-up date is
                                           in the window with no draft yet (`needs_draft`)
    save_draft(db, app, text, channel)   → create or replace the application's one open draft
    mark_sent(db, followup, ...)         → the person sent it: status "sent", the final text, a timeline event, and the
                                           next follow-up scheduled (never more than MAX_SENT per application)
    skip(db, app, followup, reason)      → the person decided not to follow up: the draft is cancelled, the date cleared

Job Hunt OS never sends a message: every function here records what a person did or will do. No model is called here
(the scheduler's AI draft is `workflow.draft_followup`). This module, the scheduler's draft and the Application Inbox's
cancellation (`application_inbox.analysis.suppress_followup`) are the only writers of `FollowUp.status`; the MCP tools
and the Tracker's endpoints both go through it, so they write identical rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models import Application, ApplicationEvent, FollowUp, Job
from app.services.contacts import recruiters_of

CHANNELS = ("email", "linkedin", "other")
TEXT_LIMIT = 5000
REASON_LIMIT = 300
RECIPIENT_LIMIT = 300
# A second nudge is scheduled after the first one is sent; never a third.
MAX_SENT = 2
DRAFT, SENT, CANCELLED = "draft", "sent", "cancelled"


class FollowUpRefused(ValueError):
    """A request the follow-up service refuses (nothing was written)."""


@dataclass
class Outcome:
    status: str  # SENT | ALREADY_SENT | SKIPPED | ALREADY_SKIPPED | SAVED
    followup: FollowUp | None
    application: Application
    next_due_at: datetime | None = None


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _clean_text(text: Any, name: str = "text") -> str:
    if not isinstance(text, str) or not text.strip():
        raise FollowUpRefused(f"{name} must be the message text (1-{TEXT_LIMIT} characters)")
    text = text.strip()
    if len(text) > TEXT_LIMIT:
        raise FollowUpRefused(f"{name} is longer than {TEXT_LIMIT} characters")
    return text


def _channel(channel: Any, default: str = "email") -> str:
    value = default if channel is None else channel
    if value not in CHANNELS:
        raise FollowUpRefused(f"channel must be one of {', '.join(CHANNELS)}")
    return value


def _submitted(app: Application) -> None:
    if app.applied_at is None:
        raise FollowUpRefused("Follow-ups are only for submitted applications; this one was never submitted")


def open_draft(db: Session, app: Application) -> FollowUp | None:
    """The application's one open (unsent, not cancelled) follow-up, newest first."""
    return db.scalar(select(FollowUp).where(FollowUp.application_id == app.id, FollowUp.status == DRAFT)
                     .order_by(desc(FollowUp.id)).limit(1))


def sent_count(db: Session, app: Application) -> int:
    return db.scalar(select(func.count(FollowUp.id)).where(FollowUp.application_id == app.id,
                                                           FollowUp.status == SENT)) or 0


def needs_draft(db: Session, app: Application) -> bool:
    """A follow-up date is set on a submitted application, nothing is drafted for it yet, and fewer than MAX_SENT
    follow-ups have gone out. The scheduler drafts these (`workflow.create_due_followups`) once they are due."""
    return (app.status == "applied" and app.followup_due_at is not None and open_draft(db, app) is None
            and sent_count(db, app) < MAX_SENT)


def employer_replied(db: Session, app: Application) -> bool:
    from app.services.application_inbox.service import blocks_followup

    return blocks_followup(db, app)


def previous_texts(db: Session, app: Application) -> list[str]:
    return [t for t in db.scalars(select(FollowUp.draft).where(FollowUp.application_id == app.id,
                                                               FollowUp.status == SENT).order_by(FollowUp.id)).all() if t]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _item(db: Session, app: Application, followup: FollowUp | None) -> dict[str, Any]:
    job = db.get(Job, app.job_id)
    return {"followup_id": followup.id if followup else None, "application_id": app.id, "job_id": app.job_id,
            "company": job.company if job else None, "title": job.title if job else None,
            "applied_at": _iso(app.applied_at),
            "due_at": _iso(followup.due_at if followup else app.followup_due_at),
            "channel": followup.channel if followup else None, "draft": followup.draft if followup else "",
            "needs_draft": followup is None, "recruiters": recruiters_of(app),
            "previous_followups": previous_texts(db, app), "employer_replied": employer_replied(db, app)}


def due(db: Session, *, within_days: int = 3, limit: int = 20, now: datetime | None = None) -> list[dict[str, Any]]:
    """Follow-ups due within `within_days` (0: due now or overdue), soonest first. Reads only; never calls a model."""
    horizon = (now or utcnow()) + timedelta(days=within_days)
    drafts = db.execute(select(FollowUp, Application).join(Application, Application.id == FollowUp.application_id)
                        .where(FollowUp.status == DRAFT, FollowUp.due_at <= horizon,
                               Application.applied_at.is_not(None), Application.archived_at.is_(None))
                        .order_by(FollowUp.due_at, FollowUp.id)).all()
    items = [(fu.due_at, app, fu) for fu, app in drafts]
    drafted = {app.id for _due, app, _fu in items}
    apps = db.scalars(select(Application).where(Application.status == "applied",
                                                Application.applied_at.is_not(None),
                                                Application.archived_at.is_(None),
                                                Application.followup_due_at.is_not(None),
                                                Application.followup_due_at <= horizon)
                      .order_by(Application.followup_due_at, Application.id)).all()
    items += [(app.followup_due_at, app, None) for app in apps if app.id not in drafted and needs_draft(db, app)]
    items.sort(key=lambda item: (item[0], item[1].id))
    return [_item(db, app, fu) for _due, app, fu in items[:limit]]


def save_draft(db: Session, app: Application, text: str, channel: str | None = "email", *,
               source: str = "mcp") -> FollowUp:
    """Create or replace the application's one open draft with the person's (or Claude's) text. Never sends."""
    _submitted(app)
    text, channel = _clean_text(text), _channel(channel)
    followup = open_draft(db, app)
    if followup is None:
        followup = FollowUp(application_id=app.id, due_at=app.followup_due_at or utcnow(), status=DRAFT)
        db.add(followup)
    followup.draft, followup.channel, followup.generation = text, channel, {"source": source}
    db.commit()
    db.refresh(followup)
    return followup


def _followup_days(db: Session) -> int:
    from app.services.workflow import followup_days

    return followup_days(db)


def mark_sent(db: Session, followup: FollowUp, *, sent_text: str | None = None, channel: str | None = None,
              sent_at: datetime | None = None, recipient: str | None = None, source: str = "mcp") -> Outcome:
    """Record that the person sent this follow-up themselves. Idempotent: a follow-up already sent is left as it is
    (ALREADY_SENT). Schedules the next one after the account's `followup_days`, never more than MAX_SENT in all, and
    none once the employer replied or the application moved on."""
    app = db.get(Application, followup.application_id)
    if followup.status == SENT:
        return Outcome("ALREADY_SENT", followup, app, app.followup_due_at)
    if followup.status != DRAFT:
        raise FollowUpRefused("This follow-up was cancelled; save a new draft before recording one as sent")
    _submitted(app)
    text = _clean_text(followup.draft if sent_text is None else sent_text, "sent_text")
    channel = _channel(channel, followup.channel or "email")
    if recipient is not None and not isinstance(recipient, str):
        raise FollowUpRefused("recipient must be text")
    recipient = (recipient or "").strip()[:RECIPIENT_LIMIT] or None
    when = sent_at or utcnow()
    if when.tzinfo is not None:
        when = when.astimezone(UTC).replace(tzinfo=None)
    followup.status, followup.sent_at, followup.draft, followup.channel = SENT, when, text, channel
    db.flush()
    more = sent_count(db, app) < MAX_SENT and not employer_replied(db, app)
    app.followup_due_at = when + timedelta(days=_followup_days(db)) if more else None
    # The timeline says that a follow-up went out, how and to whom; the message itself stays on the follow-up row.
    db.add(ApplicationEvent(application=app, kind="followup", title="Follow-up sent", occurred_at=when,
                            detail=f"By {channel}" + (f" to {recipient}" if recipient else ""),
                            meta={"channel": channel, "followup_id": followup.id, "source": source,
                                  **({"recipient": recipient} if recipient else {})}))
    db.commit()
    db.refresh(followup)
    return Outcome("SENT", followup, app, app.followup_due_at)


def skip(db: Session, app: Application, *, followup: FollowUp | None = None, reason: str | None = None,
         source: str = "mcp") -> Outcome:
    """The person decided not to follow up: the open draft (or `followup`) is cancelled and the follow-up date is
    cleared, so neither the scheduler nor the Tracker offers it again. With no draft, the skip is recorded on the
    application alone. Nothing pending → ALREADY_SKIPPED, nothing written."""
    _submitted(app)
    if followup is not None and followup.application_id != app.id:
        raise FollowUpRefused("That follow-up belongs to another application")
    if followup is not None and followup.status == SENT:
        raise FollowUpRefused("This follow-up was already sent; there is nothing to skip")
    if reason is not None and not isinstance(reason, str):
        raise FollowUpRefused("reason must be text")
    drafts = [followup] if followup is not None and followup.status == DRAFT else \
        [] if followup is not None else list(db.scalars(select(FollowUp).where(FollowUp.application_id == app.id,
                                                                               FollowUp.status == DRAFT)).all())
    if not drafts and app.followup_due_at is None:
        return Outcome("ALREADY_SKIPPED", followup, app)
    for draft in drafts:
        draft.status = CANCELLED
    app.followup_due_at = None
    reason = (reason or "").strip()[:REASON_LIMIT]
    db.add(ApplicationEvent(application=app, kind="note", title="Follow-up skipped",
                            detail=reason or "No follow-up will be sent for this application.",
                            meta={"source": source, "followup_ids": [d.id for d in drafts]}))
    db.commit()
    return Outcome("SKIPPED", drafts[0] if drafts else followup, app)
