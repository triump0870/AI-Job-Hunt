"""Job description recovery: read a posting's real description from its URL when the board sent none.

    job discovered → description usable? (app.services.jd_quality)
      yes → structured JD parse (app.services.job_description) → ranking
      no  → JobDescriptionFetchTask PENDING  ← ingestion only inserts this row and moves on
            → scheduler claims a bounded batch (FOR UPDATE SKIP LOCKED + a guarded update)
            → HTTP: the board's documented JSON endpoint, else the page (app.services.external_api)
            → the shared browser, only when HTTP found no usable description (app.infrastructure.browser)
            → the model, only to choose among blocks already fetched when extraction is ambiguous
            → deterministic validation (jd_quality.validate_recovered)
            → save with provenance, keep the original → parse task → reopen/queue ranking

Rules this module keeps:
* Ingestion, API requests and other workers never wait for recovery; ranking of this one job waits only while its
  recovery task is queued or running (`waiting_reason`), and every task ends.
* HTTP is tried first and the browser is not started when HTTP succeeds. The model never browses, never writes the
  description (it picks block ids), and is used only after deterministic extraction.
* Login walls, CAPTCHAs and anti-bot challenges are recorded as BLOCKED and never bypassed.
* Recovered text is validated before it is saved; a usable description is never replaced by a worse one, and the text
  the board sent is kept in `original_description`. A person's own text (description_source USER) is never touched.
* One job has at most one active task; finished tasks are its history and the source of Monitoring's metrics.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ACTIVE_JD_FETCH_STATUSES, Job, JobDescriptionFetchTask, JobRanking
from app.services import backoff, jd_extractors, jd_quality, server_settings, system_log, tracing, worker_settings
from app.services.lifecycle import ErrorCategory
from app.services.lifecycle import WorkflowState as S
from app.settings import get_settings

PENDING, PROCESSING, COMPLETED, FAILED, BLOCKED, SKIPPED = (
    "PENDING", "PROCESSING", "COMPLETED", "FAILED", "BLOCKED", "SKIPPED")
HTTP, PLAYWRIGHT, LLM_ASSISTED = "HTTP", "PLAYWRIGHT", "LLM_ASSISTED"
SOURCE, RECOVERED, USER = "SOURCE", "RECOVERED", "USER"
OUTCOMES = ("recovered", "unchanged", "kept_existing", "retrying", "failed", "blocked", "skipped", "removed", "lost")
WORKER = "JD_RECOVERY"
MAX_ERROR_LENGTH = 2000
MAX_HTML = 3_000_000
# A deterministic pick this sure is used as is; below it, a page with several plausible blocks goes to the model.
AMBIGUOUS_BELOW = 0.6
# Jobs whose missing description is worth recovering: not yet ranked, or held back for want of a description.
RECOVERABLE_STATES = (S.DISCOVERED, S.NORMALIZED, S.FILTERED, S.RANK_PENDING, S.SKIPPED)

# Result codes (JobDescriptionFetchTask.result_code) and what a person reads for each.
RESULT_MESSAGES = {
    "RECOVERED": "Recovered from the posting",
    "UNCHANGED": "The posting says the same as the description already saved",
    "KEPT_EXISTING": "The saved description was kept: the posting's text was not better",
    "DESCRIPTION_ARRIVED": "A description arrived before recovery ran",
    jd_quality.JOB_REMOVED: "Page no longer exists",
    jd_quality.JOB_CLOSED: "The posting is closed",
    jd_quality.LOGIN_REQUIRED: "Login required",
    jd_quality.CAPTCHA_OR_ANTI_BOT: "Anti-bot challenge",
    jd_quality.ACCESS_DENIED: "The site refused access",
    jd_quality.ERROR_PAGE: "The site showed an error page",
    jd_quality.UNRELATED_PAGE: "The link now leads to a different page",
    "NO_SOURCE_URL": "This job has no posting URL",
    "INVALID_URL": "The posting URL cannot be fetched",
    "EXTRACTION_FAILED": "No job description could be found on the page",
    "NOT_HTML": "The posting URL is not a web page",
    "NETWORK_ERROR": "The site could not be reached",
    "TIMEOUT": "The site took too long to answer",
    "RATE_LIMITED": "The site is rate limiting requests",
    "SERVER_ERROR": "The site had a server error",
    "SSL_ERROR": "The site's security certificate is not valid",
    "REDIRECT_LOOP": "The page redirects in a loop",
    "BROWSER_ERROR": "The browser could not load the page",
    "LEASE_EXPIRED": "Interrupted: the worker stopped while recovering",
    "JOB_DELETED": "The job was deleted",
}
STATE_LABELS = {PENDING: "Queued", PROCESSING: "Fetching", COMPLETED: "Recovered", BLOCKED: "Blocked",
                FAILED: "Failed", SKIPPED: "Failed"}
TRANSIENT_CODES = frozenset({"NETWORK_ERROR", "TIMEOUT", "RATE_LIMITED", "SERVER_ERROR", "BROWSER_ERROR",
                             "EXTRACTION_FAILED", jd_quality.ERROR_PAGE, "LEASE_EXPIRED"})
BLOCKED_CODES = frozenset({jd_quality.LOGIN_REQUIRED, jd_quality.CAPTCHA_OR_ANTI_BOT, jd_quality.ACCESS_DENIED})
SKIPPED_CODES = frozenset({jd_quality.JOB_REMOVED, jd_quality.JOB_CLOSED, "NO_SOURCE_URL", "INVALID_URL", "NOT_HTML",
                           "JOB_DELETED", "DESCRIPTION_ARRIVED"})
CATEGORY_OF = {"NETWORK_ERROR": ErrorCategory.TRANSIENT, "TIMEOUT": ErrorCategory.TIMEOUT,
               "RATE_LIMITED": ErrorCategory.RATE_LIMITED, "SERVER_ERROR": ErrorCategory.EXTERNAL_SERVICE,
               "BROWSER_ERROR": ErrorCategory.EXTERNAL_SERVICE, "EXTRACTION_FAILED": ErrorCategory.VALIDATION,
               "SSL_ERROR": ErrorCategory.EXTERNAL_SERVICE, "REDIRECT_LOOP": ErrorCategory.EXTERNAL_SERVICE,
               jd_quality.ERROR_PAGE: ErrorCategory.EXTERNAL_SERVICE, jd_quality.UNRELATED_PAGE: ErrorCategory.VALIDATION,
               "LEASE_EXPIRED": ErrorCategory.LEASE_EXPIRED}
# A refusal from a host read in a visible window (PLAYWRIGHT_VISIBLE_HOSTS) pauses that host for a fixed hour. These
# visits share the person's address with their own use of the site, so it is left alone rather than asked again.
VISIBLE_REFUSALS = frozenset({jd_quality.ACCESS_DENIED, jd_quality.CAPTCHA_OR_ANTI_BOT, "RATE_LIMITED"})
VISIBLE_REST = backoff.RateLimitPolicy(base_seconds=3600, multiplier=1.0, max_seconds=3600)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def message_for(code: str | None) -> str:
    return RESULT_MESSAGES.get(code or "", (code or "").replace("_", " ").capitalize())


# ───────────── detection ─────────────

def assess(job: Job) -> str:
    """Record how usable the job's description is (Job.description_quality) and its content hash. Returns the
    quality. Text a person saved is always usable."""
    if job.description_source == USER and (job.description or "").strip():
        quality = jd_quality.USABLE
    else:
        quality = jd_quality.quality_of(job.description)
    job.description_quality = quality
    job.description_content_hash = jd_quality.content_hash(job.description)
    return quality


def needs_recovery(job: Job) -> bool:
    return not jd_quality.is_job_description_usable(job)


def _pending_in_session(db: Session, job: Job) -> JobDescriptionFetchTask | None:
    # Sessions do not autoflush: a task added earlier in this transaction is not visible to a query yet.
    return next((task for task in db.new if isinstance(task, JobDescriptionFetchTask) and task.job_id == job.id
                 and task.status in ACTIVE_JD_FETCH_STATUSES), None)


def active_task(db: Session, job_id: int) -> JobDescriptionFetchTask | None:
    return db.scalar(select(JobDescriptionFetchTask).where(
        JobDescriptionFetchTask.job_id == job_id, JobDescriptionFetchTask.status.in_(ACTIVE_JD_FETCH_STATUSES)))


def latest_task(db: Session, job_id: int) -> JobDescriptionFetchTask | None:
    return db.scalar(select(JobDescriptionFetchTask).where(JobDescriptionFetchTask.job_id == job_id)
                     .order_by(JobDescriptionFetchTask.id.desc()).limit(1))


def enqueue(db: Session, job: Job, *, reason: str = "auto", force: bool = False,
            now: datetime | None = None) -> JobDescriptionFetchTask | None:
    """Queue recovery for this job. Flushes, does not commit.

    Returns the job's active task when there already is one (never a second). Without `force` (automatic recovery) a
    URL that was already tried is not tried again; a person asking again (`force`) always queues a new attempt.
    A job without a posting URL gets a SKIPPED task so the reason is visible.
    """
    now = now or utcnow()
    existing = _pending_in_session(db, job) or active_task(db, job.id)
    if existing is not None:
        return existing
    if job.domain_match in ("PENDING", "REVIEW"):
        return None  # nothing is fetched before the domain check (or the person's review) lets the job through
    url = (job.url or job.source_url or "").strip()
    if not force:
        previous = latest_task(db, job.id)
        if previous is not None and (previous.source_url or "") == url:
            return None  # this URL has been tried; a new automatic attempt would get the same answer
    task = JobDescriptionFetchTask(user_id=job.user_id, job_id=job.id, source_url=url[:1200], status=PENDING,
                                   reason=reason[:40], attempt_count=0, created_at=now, updated_at=now)
    if not url:
        task.status, task.result_code, task.completed_at = SKIPPED, "NO_SOURCE_URL", now
        task.last_error = message_for("NO_SOURCE_URL")
    db.add(task)
    db.flush()
    return task


def on_new_job(db: Session, job: Job) -> bool:
    """A job was just created. Assess its description and, when it is unusable and automatic recovery is on, queue
    recovery. Returns True when recovery was queued (the caller then skips parsing the unusable text)."""
    assess(job)
    if not get_settings().jd_fetch_enabled or job.description_quality == jd_quality.USABLE:
        return False
    try:
        with db.begin_nested():
            task = enqueue(db, job, reason="auto")
    except IntegrityError:  # another writer queued it at the same moment
        return True
    return task is not None and task.status == PENDING


def on_description_changed(db: Session, job: Job, *, by_user: bool = False) -> None:
    """The description was replaced (a fuller import, or a person's own text). Re-assess it; a now-usable description
    cancels recovery that has not started, and a still-unusable one may be queued."""
    if by_user:
        job.description_source = USER
    elif job.description_source == RECOVERED:
        job.description_source = SOURCE  # a board re-import replaced the recovered text
    quality = assess(job)
    if quality == jd_quality.USABLE:
        for task in db.scalars(select(JobDescriptionFetchTask).where(
                JobDescriptionFetchTask.job_id == job.id, JobDescriptionFetchTask.status == PENDING)).all():
            _finish(task, SKIPPED, utcnow(), "DESCRIPTION_ARRIVED")
        return
    if get_settings().jd_fetch_enabled and not by_user:
        with contextlib.suppress(IntegrityError), db.begin_nested():
            enqueue(db, job, reason="auto")


def sweep(db: Session, limit: int | None = None, *, now: datetime | None = None) -> dict[str, int]:
    """Assess descriptions never assessed (jobs from before this feature, or written by another path) and queue
    recovery for unusable ones. Bounded and idempotent: an assessed job is not read again. Commits."""
    from app.services import ranker

    if not get_settings().jd_fetch_enabled:
        return {"assessed": 0, "queued": 0}
    limit = max(1, limit or get_settings().jd_fetch_enqueue_batch_size)
    from app.services import domain_match, qualification

    backlog = select(Job).where(Job.description_quality.is_(None), Job.user_id.is_not(None))
    if qualification.routes(db):
        # The graph recovers the jobs it qualifies (after admitting them); jobs before ranking are its to take.
        backlog = backlog.where(qualification.untouched(), Job.state.not_in(ranker.PRE_RANK_STATES))
    jobs = db.scalars(backlog.order_by(Job.id.desc()).limit(limit)).all()
    assessed = queued = 0
    rules: dict = {}
    for job in jobs:
        quality = assess(job)
        assessed += 1
        if quality == jd_quality.USABLE or job.state not in RECOVERABLE_STATES:
            continue
        if job.state is not S.SKIPPED and not domain_match.screen(db, job, rules, now=now):
            continue  # outside the account's domain: nothing is spent on it
        if job.domain_match in domain_match.HELD:
            continue
        if job.state is S.SKIPPED and (job.previous_state is not S.NORMALIZED or ranker.removed_by_user(db, job)):
            continue  # skipped for another reason than a missing description
        try:
            with db.begin_nested():
                task = enqueue(db, job, reason="backlog", now=now)
                queued += int(task is not None and task.status == PENDING)
        except IntegrityError:
            continue
    db.commit()
    return {"assessed": assessed, "queued": queued}


def waiting_reason(db: Session, job: Job) -> str | None:
    """Why ranking should wait for this job's recovery, or None to go ahead. Only while a task is queued or running;
    every task ends (lease recovery fails a task that used its last attempt), so the wait always ends."""
    if active_task(db, job.id) is None:
        return None
    return "The job description is being recovered from the posting"


# ───────────── claiming ─────────────

@dataclass(frozen=True)
class FetchClaim:
    task_id: int
    lock_token: str
    worker_id: str
    user_id: int
    job_id: int
    run_now: bool = False  # asked to run now from Running workers: skips the provider cooldown


def claim(db: Session, *, worker_id: str, limit: int, now: datetime | None = None,
          lease_seconds: int | None = None) -> list[FetchClaim]:
    """Lease up to `limit` due tasks. Commits.

    Safe across app instances: PostgreSQL skips rows another worker has locked, and every claim is a guarded UPDATE
    (still PENDING) whose row count decides who got it — so two workers never hold the same task on any database.
    """
    now = now or utcnow()
    lease = timedelta(seconds=lease_seconds or get_settings().jd_fetch_lease_seconds)
    query = (select(JobDescriptionFetchTask.id)
             .where(JobDescriptionFetchTask.status == PENDING,
                    or_(JobDescriptionFetchTask.next_attempt_at.is_(None), JobDescriptionFetchTask.next_attempt_at <= now))
             .order_by(JobDescriptionFetchTask.run_now_at.is_(None),
                       JobDescriptionFetchTask.next_attempt_at.asc().nulls_first(), JobDescriptionFetchTask.id)
             .limit(max(1, limit)))
    # An account that switched automatic recovery off keeps its tasks queued; Run now still takes one.
    stopped = worker_settings.paused(db, "recovery")
    if stopped:
        query = query.where(or_(JobDescriptionFetchTask.user_id.not_in(stopped),
                                JobDescriptionFetchTask.run_now_at.is_not(None)))
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True, of=JobDescriptionFetchTask)
    ids = list(db.scalars(query).all())
    claimed: list[FetchClaim] = []
    for task_id in ids:
        token = uuid.uuid4().hex
        won = db.execute(update(JobDescriptionFetchTask)
                         .where(JobDescriptionFetchTask.id == task_id, JobDescriptionFetchTask.status == PENDING)
                         .values(status=PROCESSING, locked_by=worker_id[:200], lock_token=token,
                                 lock_expires_at=now + lease, attempt_count=JobDescriptionFetchTask.attempt_count + 1,
                                 started_at=now, next_attempt_at=None, updated_at=now)
                         .execution_options(synchronize_session=False)).rowcount
        if won == 1:
            owner, job_id, run_now = db.execute(select(JobDescriptionFetchTask.user_id, JobDescriptionFetchTask.job_id,
                                                       JobDescriptionFetchTask.run_now_at)
                                                .where(JobDescriptionFetchTask.id == task_id)).one()
            if run_now is not None:
                db.execute(update(JobDescriptionFetchTask).where(JobDescriptionFetchTask.id == task_id)
                           .values(run_now_at=None).execution_options(synchronize_session=False))
            claimed.append(FetchClaim(task_id, token, worker_id[:200], owner, job_id, run_now is not None))
    db.commit()
    return claimed


def recover_expired(db: Session, now: datetime | None = None) -> int:
    """Tasks whose worker died go back in the queue, or fail once their attempts are used up. Commits."""
    now = now or utcnow()
    rows = db.scalars(select(JobDescriptionFetchTask).where(
        JobDescriptionFetchTask.status == PROCESSING, JobDescriptionFetchTask.lock_expires_at < now)).all()
    for task in rows:
        if task.attempt_count >= worker_settings.get(db, task.user_id, "recovery", "max_attempts"):
            _finish(task, FAILED, now, "LEASE_EXPIRED")
        else:
            _release(task, now, "LEASE_EXPIRED", retry_in=0)
    db.commit()
    return len(rows)


def _clear_lease(task: JobDescriptionFetchTask) -> None:
    task.locked_by = task.lock_token = task.lock_expires_at = None


def _finish(task: JobDescriptionFetchTask, status: str, now: datetime, code: str | None,
            detail: str | None = None) -> None:
    _clear_lease(task)
    task.status, task.result_code, task.completed_at, task.updated_at = status, code, now, now
    task.next_attempt_at = None
    if status == COMPLETED:
        task.last_error, task.error_category = None, None
    else:
        task.last_error = (detail or message_for(code))[:MAX_ERROR_LENGTH]
        task.error_category = (ErrorCategory.BLOCKED if status == BLOCKED else CATEGORY_OF.get(code or "")
                               or (ErrorCategory.VALIDATION if status == SKIPPED else ErrorCategory.UNKNOWN))


def _release(task: JobDescriptionFetchTask, now: datetime, code: str, *, retry_in: float | None = None,
             detail: str | None = None, retry_seconds: int | None = None) -> None:
    """Back in the queue for a later attempt. `retry_seconds` is the account's first retry wait."""
    _clear_lease(task)
    base = retry_seconds if retry_seconds is not None else get_settings().jd_fetch_retry_seconds
    delay = base * (2 ** max(0, task.attempt_count - 1)) if retry_in is None else retry_in
    task.status, task.result_code, task.updated_at = PENDING, code, now
    task.next_attempt_at = now + timedelta(seconds=delay)
    task.last_error = (detail or message_for(code))[:MAX_ERROR_LENGTH]
    task.error_category = CATEGORY_OF.get(code, ErrorCategory.TRANSIENT)


def _held(db: Session, claim_: FetchClaim) -> JobDescriptionFetchTask | None:
    """The task, only while this worker still holds its lease."""
    return db.scalar(select(JobDescriptionFetchTask).where(
        JobDescriptionFetchTask.id == claim_.task_id, JobDescriptionFetchTask.status == PROCESSING,
        JobDescriptionFetchTask.lock_token == claim_.lock_token).execution_options(populate_existing=True))


# ───────────── the recovery pipeline ─────────────

@dataclass
class Snapshot:
    """What the pipeline needs from the job, read once so no database session is held while fetching."""

    job_id: int
    user_id: int
    url: str
    title: str
    company: str
    attempt: int
    use_browser: bool = True  # the account's own switches (Running workers → Settings)
    use_model: bool = True


@dataclass
class Outcome:
    status: str  # COMPLETED | FAILED | BLOCKED | SKIPPED | RETRY | HOLD
    code: str
    held_seconds: int = 0  # HOLD: nothing was fetched, so the attempt is given back and the task waits this long
    text: str = ""
    method: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    redirect_count: int = 0
    playwright_used: bool = False
    llm_used: bool = False
    llm_model: str | None = None
    detail: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class PageResult:
    """One stage's reading of the posting."""

    kind: str  # found | ambiguous | empty | blocked | removed | transient | permanent | not_html
    code: str | None = None
    extraction: jd_extractors.Extraction | None = None
    method: str | None = None
    status: int | None = None
    final_url: str | None = None
    redirect_count: int = 0
    detail: str | None = None


def _validation(snapshot: Snapshot, text: str, final_url: str | None) -> jd_quality.Validation:
    return jd_quality.validate_recovered(text, job_title=snapshot.title, source_url=snapshot.url, final_url=final_url)


def _read_page(snapshot: Snapshot, html: str, *, status: int | None, final_url: str, title_hint: str = "",
               method: str, redirects: int, extractor: jd_extractors.JobDescriptionExtractor) -> PageResult:
    """Classify a fetched page and extract from it. Pure: the same for HTTP and browser pages."""
    extraction = extractor.extract(html, final_url)
    title = extraction.title or title_hint
    base = {"status": status, "final_url": final_url, "redirect_count": redirects, "extraction": extraction,
            "method": method}
    blocked = jd_quality.classify_page(status=status, text=extraction.page_text, title=title,
                                       has_password_field=extraction.has_password_field)
    if blocked == jd_quality.JOB_REMOVED:
        return PageResult("removed", blocked, **base)
    if blocked:
        return PageResult("blocked", blocked, **base)
    if status == 429:
        return PageResult("transient", "RATE_LIMITED", **base)
    if status is not None and status >= 500:
        return PageResult("transient", "SERVER_ERROR", **base)
    if extraction.found:
        check = _validation(snapshot, extraction.text, final_url)
        if check.ok:
            confident = extraction.method in ("json_ld", "api", "selector") or extraction.confidence >= AMBIGUOUS_BELOW
            return PageResult("found" if confident else "ambiguous", None, **base)
        if jd_quality.UNRELATED_PAGE in check.reasons:
            return PageResult("permanent", jd_quality.UNRELATED_PAGE, **base)
    closed = jd_quality.closed_or_error(extraction.page_text, title)
    if closed == jd_quality.JOB_CLOSED:
        return PageResult("removed", closed, **base)
    return PageResult("empty", closed or "EXTRACTION_FAILED", **base)


def _http_stage(snapshot: Snapshot) -> PageResult:
    """Plain HTTP through the instrumented client. Redirects are followed one by one, each checked, so a posting URL
    cannot redirect into a private network, and a loop is recognised."""
    from app.services.external_api import external_client
    from app.services.url_safety import UnsafeURL, check_public_url

    settings = get_settings()
    extractor = jd_extractors.extractor_for(snapshot.url)
    allow_private = settings.jd_fetch_allow_private_hosts
    headers = {"Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.9"}
    with tracing.trace(job_id=snapshot.job_id), external_client(
            "WEB", service="job_posting", operation="jd_recovery_http", timeout=settings.jd_fetch_http_timeout_seconds,
            headers=headers, follow_redirects=False, user_id=snapshot.user_id, job_id=snapshot.job_id,
            metadata={"worker": WORKER, "extractor": extractor.name, "attempt": snapshot.attempt}) as client:
        api_url = extractor.structured_url(snapshot.url)
        if api_url:
            try:
                response = client.get(api_url, extensions={"operation": "jd_recovery_api"})
                if response.status_code == 200:
                    text = extractor.from_structured(response.json())
                    if text and _validation(snapshot, text, snapshot.url).ok:
                        extraction = jd_extractors.Extraction(text=text, method="api", extractor=extractor.name,
                                                              confidence=0.97)
                        return PageResult("found", None, extraction, "api", 200, snapshot.url, 0)
            except (httpx.HTTPError, ValueError):
                pass  # the page itself is tried next
        current, seen, redirects = snapshot.url, {snapshot.url}, 0
        while True:
            try:
                check_public_url(current, allow_private=allow_private)
            except UnsafeURL as exc:
                return PageResult("permanent", "INVALID_URL", status=None, final_url=current, detail=str(exc),
                                  redirect_count=redirects)
            try:
                response = client.get(current)
            except httpx.TimeoutException as exc:
                return PageResult("transient", "TIMEOUT", final_url=current, detail=type(exc).__name__,
                                  redirect_count=redirects)
            except httpx.ConnectError as exc:
                text = str(exc).lower()
                ssl = "ssl" in text or "certificate" in text
                return PageResult("permanent" if ssl else "transient", "SSL_ERROR" if ssl else "NETWORK_ERROR",
                                  final_url=current, detail=type(exc).__name__, redirect_count=redirects)
            except httpx.HTTPError as exc:
                return PageResult("transient", "NETWORK_ERROR", final_url=current, detail=type(exc).__name__,
                                  redirect_count=redirects)
            if response.is_redirect and response.headers.get("location"):
                target = urljoin(current, response.headers["location"])
                redirects += 1
                if target in seen or redirects > settings.jd_fetch_max_redirects:
                    return PageResult("permanent", "REDIRECT_LOOP", status=response.status_code, final_url=current,
                                      redirect_count=redirects)
                seen.add(target)
                current = target
                continue
            break
        content_type = response.headers.get("content-type", "").lower()
        if response.status_code < 400 and content_type and not any(kind in content_type for kind in (
                "html", "xml", "text/plain")):
            return PageResult("not_html", "NOT_HTML", status=response.status_code, final_url=current,
                              redirect_count=redirects)
        html = response.text[:MAX_HTML]
    return _read_page(snapshot, html, status=response.status_code, final_url=current, method=HTTP,
                      redirects=redirects, extractor=jd_extractors.extractor_for(current))


def _visible_host(url: str | None) -> str | None:
    """The PLAYWRIGHT_VISIBLE_HOSTS entry this URL belongs to (the host or a subdomain of it), if any."""
    host = (urlparse(url or "").hostname or "").lower()
    for entry in get_settings().playwright_visible_hosts.split(","):
        entry = entry.strip().lower()
        if entry and host and (host == entry or host.endswith(f".{entry}")):
            return entry
    return None


def _visible_provider(host: str) -> str:
    return f"WEB:{host}"  # a cooldown of its own: other sites and external calls are not held up


async def _browser_stage(snapshot: Snapshot, browser: Any, start_url: str, *, visible: bool = False) -> PageResult:
    from app.infrastructure.browser.service import (
        BLOCKED_URL,
        REDIRECT_LOOP,
        SSL,
        TIMEOUT,
        BrowserUnavailable,
        NavigationFailed,
    )

    extractor = jd_extractors.extractor_for(start_url)
    try:
        with tracing.trace(job_id=snapshot.job_id, user_id=snapshot.user_id):
            page = await browser.fetch_rendered_page(start_url, wait_for=extractor.selectors, metadata={
                "worker": WORKER, "extractor": extractor.name, "attempt": snapshot.attempt}, visible=visible)
    except BrowserUnavailable as exc:
        return PageResult("unavailable", "PLAYWRIGHT_UNAVAILABLE", detail=f"{exc.code}: {exc}")
    except NavigationFailed as exc:
        code = {TIMEOUT: "TIMEOUT", SSL: "SSL_ERROR", REDIRECT_LOOP: "REDIRECT_LOOP",
                BLOCKED_URL: "INVALID_URL"}.get(exc.category, "BROWSER_ERROR" if exc.category not in (
                    "DNS", "CONNECTION") else "NETWORK_ERROR")
        return PageResult("transient" if exc.retryable else "permanent", code, detail=str(exc))
    return await asyncio.to_thread(
        _read_page, snapshot, page.html, status=page.status, final_url=page.final_url, title_hint=page.title,
        method=PLAYWRIGHT, redirects=page.redirect_count, extractor=jd_extractors.extractor_for(page.final_url))


def _llm_stage(sessions: Callable[[], Session], snapshot: Snapshot, extraction: jd_extractors.Extraction,
               final_url: str | None) -> tuple[str | None, dict[str, Any]]:
    """Ask the model which fetched blocks are the posting. Returns (assembled text or None, diagnostics)."""
    from app.prompts import build_job_description_recovery_prompt
    from app.prompts.contracts import JobDescriptionRecoveryResult
    from app.services.llm import LLMCall, LLMClient, LLMError
    from app.services.user_settings import ai_ready

    notes: dict[str, Any] = {"blocks": len(extraction.candidates)}
    with sessions() as db:
        db.info["user_id"] = snapshot.user_id
        ready, why = ai_ready(db, snapshot.user_id)
        if not ready:
            return None, {**notes, "skipped": "LLM_UNAVAILABLE", "reason": why}
        prompt = build_job_description_recovery_prompt(
            snapshot, [block.public() for block in extraction.candidates], page_title=extraction.title)
        try:
            with tracing.trace(job_id=snapshot.job_id, user_id=snapshot.user_id):
                result = LLMClient.for_user(db, snapshot.user_id).generate_prompt(prompt, LLMCall(
                    workflow_type="job", entity_id=snapshot.job_id, attempt=snapshot.attempt,
                    validate=JobDescriptionRecoveryResult.model_validate))
        except LLMError as exc:
            return None, {**notes, "error": type(exc).__name__}
    answer: JobDescriptionRecoveryResult = result.validated
    known = {block.id for block in extraction.candidates}
    chosen = [block_id for block_id in answer.selected_block_ids if block_id in known]
    notes.update(model=result.model, selected=chosen, confidence=answer.confidence,
                 is_job_posting=answer.is_job_posting)
    if not answer.is_job_posting or not chosen:
        return None, notes
    text = jd_extractors.assemble(extraction.candidates, chosen)
    check = _validation(snapshot, text, final_url)
    notes["validation"] = check.as_dict()
    return (text if check.ok else None), notes


def _stage_note(result: PageResult) -> dict[str, Any]:
    extraction = result.extraction
    return {"kind": result.kind, "code": result.code, "status": result.status, "redirects": result.redirect_count,
            "extractor": extraction.extractor if extraction else None,
            "method": extraction.method if extraction else None,
            "confidence": extraction.confidence if extraction else None,
            "candidates": len(extraction.candidates) if extraction else 0,
            "detail": (result.detail or "")[:200] or None}


def should_try_browser_after_http(result: PageResult) -> bool:
    """Central policy for a single ordinary browser attempt after HTTP.

    A bare HTTP access denial is not evidence of a challenge: Chromium may render that public page normally.
    Explicit policy controls and terminal results are never retried as a way around them.
    """
    if result.code in {jd_quality.CAPTCHA_OR_ANTI_BOT, jd_quality.LOGIN_REQUIRED, "RATE_LIMITED",
                       jd_quality.JOB_REMOVED, jd_quality.JOB_CLOSED, "INVALID_URL", "NOT_HTML"}:
        return False
    return result.kind == "empty" or (result.kind == "blocked" and result.code == jd_quality.ACCESS_DENIED)


def _done(snapshot: Snapshot, result: PageResult, method: str, outcome: Outcome) -> Outcome:
    outcome.status, outcome.code, outcome.method = COMPLETED, "RECOVERED", method
    outcome.text = result.extraction.text if result.extraction else ""
    outcome.final_url, outcome.http_status = result.final_url, result.status
    outcome.redirect_count = result.redirect_count
    return outcome


def _stop(result: PageResult, outcome: Outcome) -> Outcome:
    """A page that ends recovery without a description: removed, blocked, or a permanent failure."""
    outcome.code = result.code or "EXTRACTION_FAILED"
    outcome.final_url = result.final_url or outcome.final_url
    outcome.http_status = result.status if result.status is not None else outcome.http_status
    outcome.redirect_count = max(outcome.redirect_count, result.redirect_count)
    outcome.detail = result.detail
    outcome.status = BLOCKED if outcome.code in BLOCKED_CODES else SKIPPED if outcome.code in SKIPPED_CODES \
        else "RETRY" if outcome.code in TRANSIENT_CODES else FAILED
    return outcome


async def recover(sessions: Callable[[], Session], snapshot: Snapshot, *, browser: Any = None,
                  llm_enabled: bool | None = None) -> Outcome:
    """HTTP, then the browser when HTTP found nothing usable, then the model on an ambiguous page. Never raises."""
    from app.services.url_safety import UnsafeURL, check_public_url

    outcome = Outcome(status=FAILED, code="EXTRACTION_FAILED")
    llm_enabled = get_settings().jd_fetch_llm_enabled if llm_enabled is None else llm_enabled
    llm_enabled = llm_enabled and snapshot.use_model
    try:
        await asyncio.to_thread(check_public_url, snapshot.url, allow_private=get_settings().jd_fetch_allow_private_hosts)
    except UnsafeURL as exc:
        outcome.status, outcome.code, outcome.detail = SKIPPED, "INVALID_URL", str(exc)
        return outcome

    visible_host = _visible_host(snapshot.url) if snapshot.use_browser else None
    if visible_host and not backoff.bypassing():  # a person's Run now goes ahead; a new refusal pauses the host again
        until = await asyncio.to_thread(backoff.cooling_until, snapshot.user_id, _visible_provider(visible_host))
        if until is not None:
            outcome.status, outcome.code = "HOLD", "RATE_LIMITED"
            outcome.held_seconds = max(1, round((until - utcnow()).total_seconds()))
            outcome.detail = f"{visible_host} refused a recent visit, so visits are paused until {until:%H:%M} UTC"
            outcome.diagnostics["playwright"] = {"attempted": False, "kind": "skipped", "reason": outcome.detail}
            return outcome

    http = await asyncio.to_thread(_http_stage, snapshot)
    outcome.diagnostics["http"] = _stage_note(http)
    outcome.final_url, outcome.http_status, outcome.redirect_count = http.final_url, http.status, http.redirect_count
    if http.kind == "found":
        return _done(snapshot, http, HTTP, outcome)
    try_browser = should_try_browser_after_http(http)
    if ((http.kind in ("removed", "blocked", "permanent", "not_html") and not try_browser)
            or http.code == "RATE_LIMITED"):
        # Explicit login/challenge/rate-limit controls and terminal results are never worked around.
        outcome.diagnostics["playwright"] = {"attempted": False, "kind": "skipped",
                                             "reason": message_for(http.code)}
        return _stop(http, outcome)

    best = http if http.kind == "ambiguous" else None
    candidates_from = http if http.extraction and http.extraction.candidates else None
    last_problem = http
    if best is None and try_browser and not snapshot.use_browser:
        outcome.diagnostics["playwright"] = {"attempted": False, "kind": "skipped",
                                             "reason": "The browser fallback is turned off for your account"}
    elif best is None and try_browser:
        if browser is None or not browser.is_available():
            outcome.diagnostics["playwright"] = {"attempted": False, "kind": "unavailable",
                                                 "code": "PLAYWRIGHT_UNAVAILABLE",
                                                 "detail": (browser.diagnostics().get("reason")
                                                            if browser is not None else "no browser service")}
        else:
            outcome.playwright_used = True
            rendered = await _browser_stage(snapshot, browser, http.final_url or snapshot.url,
                                            visible=visible_host is not None)
            outcome.diagnostics["playwright"] = {"attempted": True, **_stage_note(rendered)}
            if visible_host:
                outcome.diagnostics["playwright"]["visible"] = True
                if rendered.code in VISIBLE_REFUSALS:
                    await asyncio.to_thread(backoff.record_limit, snapshot.user_id, _visible_provider(visible_host),
                                            policy=VISIBLE_REST)
                elif rendered.kind == "found":
                    await asyncio.to_thread(backoff.note_success, snapshot.user_id, _visible_provider(visible_host))
            if rendered.kind == "found":
                return _done(snapshot, rendered, PLAYWRIGHT, outcome)
            if rendered.kind in ("removed", "blocked", "permanent"):
                return _stop(rendered, outcome)
            if rendered.kind == "ambiguous":
                best = rendered
            if rendered.extraction and rendered.extraction.candidates:
                candidates_from = rendered
            if rendered.kind != "unavailable":
                last_problem = rendered

    source = best or candidates_from
    if source is not None and source.extraction and source.extraction.candidates and llm_enabled:
        outcome.llm_used = True
        text, notes = await asyncio.to_thread(_llm_stage, sessions, snapshot, source.extraction, source.final_url)
        outcome.diagnostics["llm"] = notes
        outcome.llm_model = notes.get("model")
        if notes.get("skipped"):
            outcome.llm_used = False
        if text:
            outcome.status, outcome.code, outcome.method, outcome.text = COMPLETED, "RECOVERED", LLM_ASSISTED, text
            outcome.final_url, outcome.http_status = source.final_url, source.status
            outcome.redirect_count = max(outcome.redirect_count, source.redirect_count)
            return outcome
    if best is not None:  # the model could not settle it: the deterministic pick still validated
        return _done(snapshot, best, best.method or HTTP, outcome)
    return _stop(last_problem, outcome)


# ───────────── processing one claimed task ─────────────

def _load(sessions: Callable[[], Session], claim_: FetchClaim) -> Snapshot | str:
    with sessions() as db:
        task = _held(db, claim_)
        if task is None:
            return "lost"
        db.info["user_id"] = task.user_id
        job = db.get(Job, task.job_id)
        now = utcnow()
        if job is None:
            _finish(task, SKIPPED, now, "JOB_DELETED")
            db.commit()
            return "skipped"
        if not jd_quality.is_job_description_usable(job) or task.reason == "user_request":
            own = worker_settings.for_user(db, task.user_id, "recovery")
            return Snapshot(job.id, task.user_id, task.source_url or job.url or "", job.title or "",
                            job.company or "", task.attempt_count, own["use_browser"], own["use_model"])
        # A usable description arrived after this task was queued: nothing to recover.
        _finish(task, SKIPPED, now, "DESCRIPTION_ARRIVED")
        db.commit()
        return "skipped"


async def process(sessions: Callable[[], Session], claim_: FetchClaim, *, browser: Any = None) -> str:
    """Recover one claimed task. Returns the outcome (OUTCOMES). One task's failure never reaches the caller."""
    started = time.monotonic()
    snapshot = await asyncio.to_thread(_load, sessions, claim_)
    if isinstance(snapshot, str):
        return snapshot
    try:
        with backoff.bypass(claim_.run_now):
            outcome = await recover(sessions, snapshot, browser=browser)
    except Exception as exc:  # noqa: BLE001  an unexpected failure is one task's problem, retried later
        outcome = Outcome(status="RETRY", code="BROWSER_ERROR" if "playwright" in repr(exc).lower()
                          else "NETWORK_ERROR", detail=f"{type(exc).__name__}")
    latency = int((time.monotonic() - started) * 1000)
    return await asyncio.to_thread(_settle, sessions, claim_, snapshot, outcome, latency)


def _measure(task: JobDescriptionFetchTask, outcome: Outcome, latency: int) -> None:
    task.fetch_method = outcome.method if outcome.status == COMPLETED else task.fetch_method
    task.final_url = (outcome.final_url or task.final_url or "")[:1200] or None
    task.http_status, task.redirect_count = outcome.http_status, outcome.redirect_count
    task.playwright_used = bool(task.playwright_used or outcome.playwright_used)
    task.llm_used = bool(task.llm_used or outcome.llm_used)
    task.llm_model = (outcome.llm_model or task.llm_model or "")[:200] or None
    task.latency_ms = latency
    task.recovered_chars = len(outcome.text) if outcome.text else task.recovered_chars
    task.diagnostics = {**(task.diagnostics or {}), f"attempt_{task.attempt_count}": outcome.diagnostics}


def _settle(sessions: Callable[[], Session], claim_: FetchClaim, snapshot: Snapshot, outcome: Outcome,
            latency: int) -> str:
    from app.services.job_description import lock_job

    now = utcnow()
    with sessions() as db:
        task = _held(db, claim_)
        if task is None:
            return "lost"  # the lease expired and another worker took over: its result stands
        db.info["user_id"] = task.user_id
        _measure(task, outcome, latency)
        label = "failed"
        if outcome.status == COMPLETED:
            job = lock_job(db, task.job_id)
            if job is None:
                _finish(task, SKIPPED, now, "JOB_DELETED")
                label = "skipped"
            else:
                label = apply_recovered(db, job, task, outcome, now)
        elif outcome.status == "HOLD":  # nothing was fetched: the attempt is given back and the task waits
            task.attempt_count = max(0, task.attempt_count - 1)
            _release(task, now, outcome.code, retry_in=outcome.held_seconds, detail=outcome.detail)
            label = "retrying"
        elif outcome.status == "RETRY" and task.attempt_count < (own := worker_settings.for_user(
                db, task.user_id, "recovery"))["max_attempts"]:
            _release(task, now, outcome.code, detail=message_for(outcome.code), retry_seconds=own["retry_seconds"])
            label = "retrying"
        else:
            status = FAILED if outcome.status == "RETRY" else outcome.status
            _finish(task, status, now, outcome.code)
            label = {BLOCKED: "blocked", SKIPPED: "skipped"}.get(status, "failed")
        final = {"status": task.status, "code": task.result_code}
        if outcome.code in (jd_quality.JOB_REMOVED, jd_quality.JOB_CLOSED):
            # The posting is gone: delete the job (its tasks go with it) and remember it, so the next search does
            # not bring it back (app.services.job_removal). Approved or applied jobs are kept.
            from app.services import job_removal

            job = lock_job(db, task.job_id)
            if job is not None and job_removal.remove_posting(db, job, outcome.code, actor="jd_recovery",
                                                              now=now) == "deleted":
                label = "removed"
        db.commit()
    _log(snapshot, claim_, outcome, final, label, latency)
    return label


def apply_recovered(db: Session, job: Job, task: JobDescriptionFetchTask, outcome: Outcome, now: datetime) -> str:
    """Save recovered text on the (locked) job with its provenance, then hand the job to parsing and ranking.

    Equivalent text changes nothing downstream. A usable description is kept unless the recovered posting is clearly
    fuller, and the text the board sent is always kept in original_description."""
    from app.services import job_description, ranker

    text = outcome.text.strip()
    digest = jd_quality.content_hash(text)
    job.recovered_description = text
    job.description_fetch_method = outcome.method
    job.description_source_url = (task.source_url or job.url or "")[:1200] or None
    job.description_final_url = (outcome.final_url or "")[:1200] or None
    job.description_fetched_at = now
    job.description_recovery_task_id = task.id
    if digest and digest == jd_quality.content_hash(job.description):
        _finish(task, COMPLETED, now, "UNCHANGED")
        return "unchanged"
    if job.description_source == USER or not jd_quality.better_than(text, job.description):
        _finish(task, COMPLETED, now, "KEPT_EXISTING")
        return "kept_existing"
    if job.original_description is None and (job.description or "").strip():
        job.original_description = job.description  # the board's text is never discarded
    was_ranked = job.state in (S.RANKED, S.QUALIFIED, S.ARCHIVED)
    job.description = text
    job.description_source = RECOVERED
    job.description_quality = jd_quality.USABLE
    job.description_content_hash = digest
    job_description.normalize_job(db, job)
    _finish(task, COMPLETED, now, "RECOVERED")
    db.flush()
    from app.services import search_scope

    if not search_scope.screen(db, job, actor="jd_recovery"):
        return "recovered"  # the text shows the search did not ask for it (a primary skill it never mentions)
    from app.services import domain_classifier, qualification

    if domain_classifier.recheck_recovered(db, job, now):
        return "recovered"  # read by its title alone so far: the classifier reads the text before parsing and ranking
    job_description.queue_parse(db, job, reason="description_changed", now=now)
    # Ranking waits for the parse (job_description.waiting_reason); queueing is all this worker does.
    if ranker.removed_by_user(db, job):
        return "recovered"  # the person took this job out of ranking; a description does not put it back
    if qualification.owns(db, job):
        return "recovered"  # the qualification graph waits for this recovery and takes the job on itself
    try:
        with db.begin_nested():
            ranker.reopen_for_description(db, job, actor="jd_recovery", now=now)
            if job.state in ranker.PRE_RANK_STATES or was_ranked:
                ranker.queue_job(db, job, policy=ranker.policy_for(db, job.user_id), actor="jd_recovery",
                                 reason="description_changed", force=was_ranked, now=now)
    except Exception:  # noqa: BLE001  the next ranking sweep queues it instead
        system_log.log_event(category="WORKFLOW", level="WARNING", operation="jd_recovery", workflow=WORKER,
                             entity_type="job", entity_id=job.id, user_id=job.user_id,
                             message="Recovered description saved; ranking will pick the job up on its next sweep")
    return "recovered"


def _log(snapshot: Snapshot, claim_: FetchClaim, outcome: Outcome, final: dict[str, Any], label: str,
         latency: int) -> None:
    level = "INFO" if label in ("recovered", "unchanged", "kept_existing", "removed") else \
        "WARNING" if label in ("blocked", "retrying", "skipped") else "ERROR"
    system_log.log_event(
        category="WORKFLOW", level=level, operation="jd_recovery", workflow=WORKER, user_id=snapshot.user_id,
        entity_type="job", entity_id=snapshot.job_id, status=label, duration_ms=latency,
        message=f"JD recovery {label}: {message_for(final['code'])}",
        details={"worker": WORKER, "job_id": snapshot.job_id, "task_id": claim_.task_id,
                 "fetch_method": outcome.method, "source_host": urlparse(snapshot.url).hostname,
                 "success": outcome.status == COMPLETED, "http_status": outcome.http_status,
                 "attempt": snapshot.attempt, "redirect_count": outcome.redirect_count,
                 "characters_recovered": len(outcome.text) if outcome.text else 0,
                 "final_jd_length": len(outcome.text) if final["code"] == "RECOVERED" else None,
                 "playwright_used": outcome.playwright_used, "llm_used": outcome.llm_used,
                 "llm_model": outcome.llm_model, "result_code": final["code"],
                 "blocked_reason": final["code"] if final["status"] == BLOCKED else None,
                 "error_category": None if outcome.status == COMPLETED else outcome.code})


# ───────────── read models ─────────────

def task_dict(task: JobDescriptionFetchTask | None) -> dict[str, Any] | None:
    if task is None:
        return None
    return {"id": task.id, "status": task.status, "reason": task.reason, "result_code": task.result_code,
            "message": message_for(task.result_code) if task.result_code else None,
            "attempt_count": task.attempt_count, "fetch_method": task.fetch_method,
            "source_url": task.source_url, "final_url": task.final_url, "http_status": task.http_status,
            "playwright_used": task.playwright_used, "llm_used": task.llm_used,
            "recovered_chars": task.recovered_chars, "latency_ms": task.latency_ms,
            "next_attempt_at": task.next_attempt_at.isoformat() if task.next_attempt_at else None,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None}


def status_of(db: Session, job: Job) -> dict[str, Any]:
    """Where the job's description recovery stands, in words a person can act on. Never a stack trace."""
    task = latest_task(db, job.id)
    usable = jd_quality.is_job_description_usable(job)
    if task is None:
        state = "NOT_NEEDED" if usable else "NOT_STARTED"
        label = "" if usable else "Job description unavailable"
    else:
        state = task.status
        label = STATE_LABELS.get(task.status, task.status.title())
        if task.status == COMPLETED and task.result_code != "RECOVERED":
            label = "Checked"
        if task.status in (FAILED, BLOCKED, SKIPPED) and task.result_code:
            label = f"{label} - {message_for(task.result_code).lower()}"
        elif task.status == PENDING and task.attempt_count:
            label = f"Queued - retrying ({message_for(task.result_code).lower()})"
    return {"state": state, "label": label, "usable": usable, "quality": job.description_quality or
            jd_quality.quality_of(job.description), "has_url": bool((job.url or job.source_url or "").strip()),
            "can_recover": bool((job.url or job.source_url or "").strip()) and (
                task is None or task.status not in ACTIVE_JD_FETCH_STATUSES),
            "description_source": job.description_source or SOURCE,
            "fetch_method": job.description_fetch_method, "fetched_at":
            job.description_fetched_at.isoformat() if job.description_fetched_at else None,
            "source_url": job.description_source_url, "final_url": job.description_final_url,
            "has_original": job.original_description is not None, "task": task_dict(task)}


def metrics(db: Session, user_id: int | None, start: datetime, end: datetime) -> dict[str, Any]:
    """Recovery metrics for Monitoring: one account (or every account for the admin), tasks created in the window."""
    def scoped(query):
        query = query.where(JobDescriptionFetchTask.created_at >= start, JobDescriptionFetchTask.created_at <= end)
        return query.where(JobDescriptionFetchTask.user_id == user_id) if user_id is not None else query

    tasks = db.execute(scoped(select(JobDescriptionFetchTask.status, JobDescriptionFetchTask.result_code,
                                     JobDescriptionFetchTask.fetch_method, JobDescriptionFetchTask.playwright_used,
                                     JobDescriptionFetchTask.llm_used, JobDescriptionFetchTask.latency_ms,
                                     JobDescriptionFetchTask.recovered_chars, JobDescriptionFetchTask.completed_at,
                                     JobDescriptionFetchTask.job_id))).all()
    finished = [t for t in tasks if t.status not in ACTIVE_JD_FETCH_STATUSES]
    recovered = [t for t in finished if t.status == COMPLETED and t.result_code == "RECOVERED"]
    attempted = [t for t in finished if t.result_code not in ("NO_SOURCE_URL", "DESCRIPTION_ARRIVED", "JOB_DELETED")]
    browser = [t for t in attempted if t.playwright_used]

    def rate(part: int, whole: int) -> float | None:
        return round(part / whole, 3) if whole else None

    latencies = [t.latency_ms for t in finished if t.latency_ms is not None]
    lengths = [t.recovered_chars for t in recovered if t.recovered_chars]
    missing = select(func.count(Job.id)).where(Job.description_quality.is_not(None),
                                               Job.description_quality != jd_quality.USABLE)
    if user_id is not None:
        missing = missing.where(Job.user_id == user_id)
    by_job = {t.job_id: t.completed_at for t in recovered if t.completed_at}
    parsed = ranked = 0
    if by_job:
        for job_id, parsed_at in db.execute(select(Job.id, Job.structured_description_parsed_at)
                                            .where(Job.id.in_(list(by_job)))).all():
            parsed += int(parsed_at is not None and parsed_at >= by_job[job_id])
        for job_id, first in db.execute(select(JobRanking.job_id, func.max(JobRanking.created_at))
                                        .where(JobRanking.job_id.in_(list(by_job)))
                                        .group_by(JobRanking.job_id)).all():
            ranked += int(first is not None and first >= by_job[job_id])
    by_result: dict[str, int] = {}
    for t in finished:
        by_result[t.result_code or t.status] = by_result.get(t.result_code or t.status, 0) + 1
    return {
        "missing_descriptions": db.scalar(missing) or 0,
        "tasks_created": len(tasks), "active": len(tasks) - len(finished), "finished": len(finished),
        "recovered": len(recovered),
        "success_rate": rate(len(recovered), len(attempted)),
        "http_only_success_rate": rate(sum(1 for t in recovered if t.fetch_method == HTTP), len(attempted)),
        "playwright_fallback_rate": rate(len(browser), len(attempted)),
        "playwright_success_rate": rate(sum(1 for t in recovered if t.fetch_method == PLAYWRIGHT), len(browser)),
        "llm_fallback_rate": rate(sum(1 for t in attempted if t.llm_used), len(attempted)),
        "llm_success_rate": rate(sum(1 for t in recovered if t.fetch_method == LLM_ASSISTED),
                                 sum(1 for t in attempted if t.llm_used)),
        "average_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
        "blocked": sum(1 for t in finished if t.status == BLOCKED),
        "failed": sum(1 for t in finished if t.status == FAILED),
        # Removed jobs are deleted with their tasks; the tombstones they leave are what is counted.
        "jobs_removed": _removed_count(db, user_id, start, end),
        "average_recovered_length": round(sum(lengths) / len(lengths)) if lengths else None,
        "subsequently_parsed": parsed, "subsequently_ranked": ranked,
        "by_result": dict(sorted(by_result.items(), key=lambda kv: -kv[1])),
    }


def _removed_count(db: Session, user_id: int | None, start: datetime, end: datetime) -> int:
    from app.models import RemovedPosting

    query = select(func.count(RemovedPosting.id)).where(RemovedPosting.removed_at >= start,
                                                        RemovedPosting.removed_at <= end)
    if user_id is not None:
        query = query.where(RemovedPosting.user_id == user_id)
    return db.scalar(query) or 0


def diagnostics(db: Session | None = None, user_id: int | None = None) -> dict[str, Any]:
    """What recovery can use right now, without secrets: HTTP, the browser, and (for an account) the model."""
    from app.infrastructure.browser import get_browser_service

    settings = get_settings()
    browser = get_browser_service().diagnostics()
    llm: dict[str, Any] = {"status": "UNAVAILABLE", "reason": "Model fallback is off (JD_FETCH_LLM_ENABLED=false)"}
    if settings.jd_fetch_llm_enabled:
        llm = {"status": "UNKNOWN", "reason": None}
        if db is not None and user_id is not None:
            from app.services.user_settings import ai_ready

            ready, why = ai_ready(db, user_id)
            llm = {"status": "AVAILABLE" if ready else "UNAVAILABLE", "reason": None if ready else why}
    return {"enabled": settings.jd_fetch_enabled,
            "http": {"status": "AVAILABLE"},
            "playwright": {k: browser.get(k) for k in (
                "status", "reason_code", "reason", "enabled", "browser", "headless", "running", "launches",
                "crashes", "max_concurrency", "keep_browser_running", "service_workers",
                "block_nonessential_resources", "debug_artifacts", "package_installed", "chromium_installed",
                "last_error")},
            "llm": llm,
            "settings": {"min_characters": settings.jd_description_min_characters,
                         "batch_size": server_settings.value("jd_fetch_batch_size"),
                         "max_concurrency": server_settings.value("jd_fetch_max_concurrency"),
                         "max_attempts": settings.jd_fetch_max_attempts,
                         "interval_minutes": server_settings.value("jd_recovery_interval_minutes")}}
