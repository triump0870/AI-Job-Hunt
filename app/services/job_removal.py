"""Postings that no longer exist are deleted, and stay deleted.

    recovery or the listing check reads the posting URL → 404/410 or "no longer accepting applications"
      → remove_posting(): tombstone (removed_postings) + a Monitoring → Logs line + delete the job
      → the next search that returns the same posting skips it (is_removed)

The listing check re-reads every job up to approval about once a day: HTTP first, then one ordinary rendering in the
shared browser when HTTP found nothing readable (never after a login wall, a challenge or a 429). A page that still
shows the posting but says it no longer takes applications counts as closed.

Jobs the person approved or applied to are never deleted: the log says the posting closed, the job stays and is
labelled (posting_closed_at). Only a page that says the posting is gone counts — a login wall, a challenge, a timeout
or a server error never deletes anything. With AUTO_DELETE_REMOVED_JOBS=false removed postings are only marked (the
recovery task says so).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Application,
    InsightObservation,
    Job,
    JobClassificationTask,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobInsight,
    JobInsightTask,
    JobRankingTask,
    RemovedPosting,
    TailorTask,
)
from app.services import jd_quality, system_log, threads
from app.settings import get_settings

# Jobs the person has committed to: never deleted automatically.
PROTECTED_STATUSES = ("approved", "applied")
REMOVED_REASONS = ("JOB_REMOVED", "JOB_CLOSED")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def protected(db: Session, job: Job) -> bool:
    if job.status in PROTECTED_STATUSES:
        return True
    return db.scalar(select(Application.id).where(Application.job_id == job.id,
                                                  Application.applied_at.is_not(None))) is not None


def is_removed(db: Session, owner: int, *, source: str | None = None, source_ref: str | None = None,
               canonical_url: str | None = None, dedupe_key: str | None = None) -> bool:
    """Whether this posting was deleted earlier because it no longer existed."""
    matches = []
    if source and source_ref:
        matches.append((RemovedPosting.source == source) & (RemovedPosting.source_ref == source_ref))
    if canonical_url:
        matches.append(RemovedPosting.canonical_url == canonical_url)
    if dedupe_key:
        matches.append(RemovedPosting.dedupe_key == dedupe_key)
    if not matches:
        return False
    return db.scalar(select(RemovedPosting.id).where(RemovedPosting.user_id == owner, or_(*matches)).limit(1)) \
        is not None


def remove_posting(db: Session, job: Job, reason: str, *, actor: str, now: datetime | None = None) -> str:
    """Delete a job whose posting is gone, leaving a tombstone. Returns deleted, kept (protected) or disabled.
    Does not commit."""
    now = now or utcnow()
    details = {"job_id": job.id, "title": job.title, "company": job.company, "url": job.url, "reason": reason,
               "actor": actor}
    if not get_settings().auto_delete_removed_jobs:
        return "disabled"
    if protected(db, job):
        if job.posting_closed_at is None:
            job.posting_closed_at, job.posting_closed_reason = now, reason[:40]
        system_log.log_event(category="WORKFLOW", level="WARNING", operation="posting_removed", workflow="DISCOVERY",
                             user_id=job.user_id, entity_type="job", entity_id=job.id, status="kept",
                             message=f"Posting for {job.title} at {job.company} is gone; kept because you approved "
                                     "or applied to it", details=details)
        return "kept"
    db.add(RemovedPosting(user_id=job.user_id, source=job.source or "", source_ref=job.source_ref or "",
                          canonical_url=job.canonical_url, dedupe_key=job.dedupe_key, title=(job.title or "")[:300],
                          company=(job.company or "")[:300], reason=reason[:40], removed_at=now))
    system_log.log_event(category="WORKFLOW", level="INFO", operation="posting_removed", workflow="DISCOVERY",
                         user_id=job.user_id, entity_type="job", entity_id=job.id, status="deleted",
                         message=f"Deleted {job.title} at {job.company}: the posting no longer exists",
                         details=details)
    delete_job(db, job)
    return "deleted"


def delete_job(db: Session, job: Job) -> None:
    """Delete a job and its task rows. PostgreSQL cascades these; deleting them here too keeps SQLite installs free of
    orphans (a reused job id would otherwise inherit another job's tasks). Does not commit."""
    from app.services import application_execution, preparation, qualification

    qualification.on_job_deleted(db, job.id)  # its graph runs stop and lose their lease
    preparation.on_job_deleted(db, job.id)
    application_execution.on_job_deleted(db, job.id)
    from app.models import ApplicationClaim, ApprovedPacket

    for model in (JobClassificationTask, JobDescriptionFetchTask, JobDescriptionParseTask, JobRankingTask, JobInsightTask,
                  ApprovedPacket, ApplicationClaim):
        for row in db.scalars(select(model).where(model.job_id == job.id)).all():
            db.delete(row)
    for insight in db.scalars(select(JobInsight).where(JobInsight.job_id == job.id)).all():
        for row in db.scalars(select(InsightObservation).where(InsightObservation.job_insight_id == insight.id)).all():
            db.delete(row)
        db.delete(insight)
    db.delete(job)


# ───────────── the listing check ─────────────

# Every state up to approval. In-flight states (RANKING, TAILORING, APPLYING) are left to their worker, and states
# after applying are not checked: a posting closing once you applied is expected and changes nothing.
CHECKED_STATES = ("DISCOVERED", "NORMALIZED", "FILTERED", "RANK_PENDING", "RANKED", "QUALIFIED", "TAILORED",
                  "REVIEWED", "READY_TO_APPLY", "SKIPPED", "ARCHIVED")


def due_jobs(db: Session, now: datetime, limit: int | None, *, owner: int | None = None,
             checked_before: datetime | None = None) -> list[tuple[int, int]]:
    """(job id, owner) of postings worth re-checking: a URL, up to approval (approved ones only get labelled), not
    held by a worker, not checked recently. `owner` and `checked_before` are for a check a person asked for."""
    cutoff = checked_before or now - timedelta(days=max(0, get_settings().listing_check_max_age_days))
    tailoring = select(TailorTask.id).where(TailorTask.job_id == Job.id, TailorTask.status.in_(("queued", "running")))
    query = select(Job.id, Job.user_id).where(Job.user_id.is_not(None), Job.url != "", Job.state.in_(CHECKED_STATES),
                                              Job.status != "applied", Job.lock_token.is_(None), ~tailoring.exists(),
                                              or_(Job.listing_checked_at.is_(None), Job.listing_checked_at < cutoff))
    if owner is not None:
        query = query.where(Job.user_id == owner)
    query = query.order_by(Job.listing_checked_at.asc().nulls_first(), Job.id)
    return list(db.execute(query if limit is None else query.limit(max(1, limit))).all())


def closed_banner(result: Any) -> bool:
    """A page that still shows the posting but says it no longer takes applications (LinkedIn, some ATSs)."""
    extraction = getattr(result, "extraction", None)
    text = jd_quality.lower_text(getattr(extraction, "page_text", None))[:40000]
    return any(marker in text for marker in jd_quality.CLOSED_BANNER_MARKERS)


async def read_posting(snapshot: Any, browser: Any = None) -> tuple[Any, bool]:
    """HTTP, then one ordinary browser rendering when HTTP found nothing readable and the account allows it.
    Returns (result, whether the browser was used). Never a model. Raises only RateLimitedCooldown: nothing was sent,
    so the posting has not been read."""
    from app.services import jd_recovery
    from app.services.external_api import RateLimitedCooldown

    try:
        result = await threads.run_work(jd_recovery._http_stage, snapshot)
    except Exception:  # noqa: BLE001  one posting failing never stops the check
        return None, False
    # _http_stage reports the cooldown as a network error (it is an httpx.HTTPError), which recovery simply retries.
    if result.detail == RateLimitedCooldown.__name__:
        raise RateLimitedCooldown("WEB", 0)
    if (browser is None or not snapshot.use_browser or not jd_recovery.should_try_browser_after_http(result)
            or result.code == "RATE_LIMITED"):
        return result, False
    try:
        if not browser.is_available():
            return result, False
        rendered = await jd_recovery._browser_stage(snapshot, browser, result.final_url or snapshot.url)
    except Exception:  # noqa: BLE001
        return result, True
    return (result if rendered.kind == "unavailable" else rendered), True


def verdict(result: Any) -> tuple[str, str | None]:
    """("removed", reason), ("live", None) or ("unknown", None). Only a page that says so is removed."""
    if result is None:
        return "unknown", None
    if result.kind == "removed":
        return "removed", result.code or "JOB_REMOVED"
    if result.kind in ("found", "ambiguous", "empty") and closed_banner(result):
        return "removed", jd_quality.JOB_CLOSED
    if result.kind in ("found", "ambiguous"):
        return "live", None
    return "unknown", None  # blocked, transient, permanent, not_html, empty: never deleted on a guess


async def check_one(sessions: Callable[[], Session], job_id: int, owner: int, now: datetime, *,
                    browser: Any = None) -> str:
    """Read one posting and act on what it says: deleted, kept (approved: labelled instead), live, unknown or
    missing. Never a model, and never a deletion on a guess."""
    from app.services import jd_recovery, worker_settings

    with sessions() as db:
        db.info["user_id"] = owner
        job = db.get(Job, job_id)
        if job is None:
            return "missing"
        use_browser = bool(worker_settings.for_user(db, owner, "recovery").get("use_browser", True))
        snapshot = jd_recovery.Snapshot(job.id, owner, job.url, job.title or "", job.company or "", 1,
                                        use_browser=use_browser, use_model=False)
    result, used_browser = await read_posting(snapshot, browser)
    kind, reason = verdict(result)
    with sessions() as db:
        db.info["user_id"] = owner
        job = db.get(Job, job_id)
        if job is None:
            return "missing"
        job.listing_checked_at = now
        outcome = kind
        if kind == "removed":
            outcome = remove_posting(db, job, reason or "JOB_REMOVED", actor="listing_check", now=now)
        elif kind == "live":
            job.posting_closed_at = job.posting_closed_reason = None  # a posting that reopened
        db.commit()
    return f"{outcome}+browser" if used_browser else outcome


async def check_listings(sessions: Callable[[], Session], now: datetime, *, limit: int | None = None,
                         concurrency: int | None = None, browser: Any = None) -> dict[str, Any]:
    with sessions() as db:
        due = due_jobs(db, now, limit or get_settings().listing_check_batch_size)
    return await _check_due(sessions, due, now, concurrency=concurrency, browser=browser)


async def _check_due(sessions: Callable[[], Session], due: list[tuple[int, int]], now: datetime, *,
                     concurrency: int | None = None, browser: Any = None) -> dict[str, Any]:
    from app.services.external_api import RateLimitedCooldown

    semaphore = asyncio.Semaphore(max(1, concurrency or get_settings().jd_fetch_max_concurrency))
    held = False  # one site's 429 rests every posting fetch (provider WEB): the rest wait, unread and not marked checked

    async def run(job_id: int, owner: int) -> str:
        nonlocal held
        async with semaphore:
            if held:
                return "held"
            try:
                return await check_one(sessions, job_id, owner, now, browser=browser)
            except RateLimitedCooldown:
                held = True
                return "held"
            except Exception:  # noqa: BLE001  one posting failing never stops the check
                return "unknown"

    outcomes = await asyncio.gather(*(run(job_id, owner) for job_id, owner in due))
    summary: dict[str, Any] = {"checked": len(due), "browser_used": 0}
    for outcome in outcomes:
        outcome, _, used = outcome.partition("+")
        summary["browser_used"] += bool(used)
        summary[outcome] = summary.get(outcome, 0) + 1
    return summary


# ───────────── a check a person asked for ─────────────

# Pressing the button again soon after moves on to postings not read yet instead of reading the same pages again.
MANUAL_RECHECK_AFTER = timedelta(hours=1)
# Accounts with a check running in this process. Only stops a double press: a second server process could overlap it,
# which reads a page twice at worst (check_one finds a deleted job "missing").
_manual_running: set[int] = set()


def manual_due(db: Session, owner: int, now: datetime) -> list[tuple[int, int]]:
    """Every posting of this account due for a check it asked for, least recently checked first."""
    return due_jobs(db, now, None, owner=owner, checked_before=now - MANUAL_RECHECK_AFTER)


def manual_running(owner: int) -> bool:
    return owner in _manual_running


def manual_progress(db: Session, owner: int, since: datetime) -> dict[str, int]:
    """What has been read since a check started, from the rows the check writes. A scheduled pass running at the same
    time counts too: these are the account's postings read and removed since then."""
    still_listed = db.scalar(select(func.count(Job.id)).where(Job.user_id == owner,
                                                              Job.listing_checked_at >= since)) or 0
    removed = db.scalar(select(func.count(RemovedPosting.id)).where(RemovedPosting.user_id == owner,
                                                                    RemovedPosting.removed_at >= since)) or 0
    return {"checked": still_listed + removed, "removed": removed}


async def check_account(sessions: Callable[[], Session], owner: int, due: list[tuple[int, int]],
                        now: datetime) -> None:
    """Run after the response of the request that asked for it: the request itself never reads a posting."""
    from app.infrastructure.browser import get_browser_service

    if owner in _manual_running:
        return
    _manual_running.add(owner)
    browser = get_browser_service()
    try:
        summary = await _check_due(sessions, due, now, browser=browser)
        system_log.log_event(category="WORKFLOW", level="INFO", operation="listing_check_manual", workflow="DISCOVERY",
                             user_id=owner, status="succeeded", details=summary,
                             message=f"Listing check you asked for: {summary['checked'] - summary.get('held', 0)} of "
                                     f"{summary['checked']} postings read, {summary.get('deleted', 0)} deleted")
    finally:
        _manual_running.discard(owner)
        await browser.release_idle()
