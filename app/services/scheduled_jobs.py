"""Scheduled jobs: the work behind every scheduler entry, manual admin trigger and cron run.

Each function receives a JobContext, opens its own database sessions, handles a bounded amount of
work and returns a JSON-serialisable summary. A summary whose `skipped` value is a string means the
job had nothing it was allowed to do; that is not an error.

Work that belongs to a person runs per account, in a session bound to that account (app.tenancy),
with that account's own API keys and preferences: its search schedule, auto-ranking switch and AI
provider. One account failing never stops the others.

Every job is safe to run twice, including at the same moment from two app instances:
- row-level work is claimed with PostgreSQL locks (ranking task and work_queue leases, FOR UPDATE SKIP LOCKED);
- finished work is detected from the database before acting (ranking tasks and inputs, compensation_checked_at,
  SearchRun.completed_at, existing follow-up drafts, a search run started within the interval).
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import scheduler_hooks
from app.models import (
    Application,
    Job,
    JobClassificationTask,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobInsightTask,
    JobRankingTask,
    ResumeParseTask,
    SearchRun,
    User,
)
from app.services import (
    ai_advisor,
    auto_tailor,
    domain_classifier,
    external_monitoring,
    jd_recovery,
    job_description,
    monitoring,
    ranker,
    resume_parsing,
    server_settings,
    system_log,
    threads,
)
from app.services import work_queue as wq
from app.services.compensation import enrich_compensation
from app.services.hunter import run_due_profiles
from app.services.lifecycle import WorkflowState as S
from app.services.sources import give_up_on_run, refresh_apify_run
from app.services.user_settings import ai_ready, apify_token
from app.services.workflow import create_due_followups
from app.settings import get_settings
from app.workspace import run_due_saved_searches

SessionFactory = Callable[[], Session]
# Jobs not ranked yet: before the ranker's hard filters, waiting for it, or being ranked.
RANK_PENDING_STATES = ranker.RANK_WAITING_STATES


@dataclass(frozen=True)
class JobContext:
    sessions: SessionFactory
    trigger: str  # schedule | manual | cron
    now: datetime
    worker_id: str
    interval_minutes: int


@dataclass(frozen=True)
class JobSpec:
    name: str
    title: str
    interval_setting: str
    run: Callable[[JobContext], dict[str, Any] | Awaitable[dict[str, Any]]]
    description: str


def skipped(reason: str, **details: Any) -> dict[str, Any]:
    return {"skipped": reason, **details}


def _is_postgres(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def _as_user(db: Session, user_id: int) -> Session:
    """Bind a session to one account so it can only see and change that account's data."""
    db.info["user_id"] = user_id
    return db


def _account_ids(ctx: JobContext) -> list[int]:
    with ctx.sessions() as db:
        return list(db.scalars(select(User.id).order_by(User.id)).all())


def _error(user_id: int, exc: Exception) -> dict[str, Any]:
    return {"user_id": user_id, "error": f"{type(exc).__name__}: {exc}"[:300]}


# ───────────── hunter / Apify ─────────────

def _hunt_for_account(ctx: JobContext, db: Session) -> tuple[list[int] | str, list[dict[str, Any]]]:
    """Start this account's due saved Discovery searches and its due Hunter search profiles.

    A manual trigger runs every scheduled setup at once, whatever their next run time says.
    """
    if not apify_token(db):
        return "no Apify key", []
    setups = run_due_saved_searches(db, db.info["user_id"], ctx.now, force=ctx.trigger == "manual")
    profiles = run_due_profiles(db, db.info["user_id"], ctx.now)
    started = setups["run_ids"] + profiles["run_ids"]
    errors = setups["errors"] + profiles["errors"]
    if not started and not errors:
        return "no search was due", []
    return started, errors


def hunter(ctx: JobContext) -> dict[str, Any]:
    """Start each account's due searches, with its own Apify token, at most once per interval."""
    accounts = _account_ids(ctx)
    if not accounts:
        return skipped("No accounts yet")
    started, not_started, errors = 0, Counter(), []
    for user_id in accounts:
        try:
            with _as_user(ctx.sessions(), user_id) as db:
                outcome, profile_errors = _hunt_for_account(ctx, db)
        except Exception as exc:  # one account's failure must not stop the others
            errors.append(_error(user_id, exc))
            continue
        errors += [{"user_id": user_id, **problem} for problem in profile_errors]
        if isinstance(outcome, str):
            not_started[outcome] += 1
        else:
            started += len(outcome)
    summary = {"accounts": len(accounts), "started": started, "not_started": dict(not_started), "errors": errors}
    if not started and not errors:
        return skipped("No account had a search due", **summary)
    return summary


def apify_sync(ctx: JobContext) -> dict[str, Any]:
    """Check unfinished Apify runs of every account and import the datasets of the ones that finished."""
    summary: dict[str, Any] = {"checked": 0, "completed": 0, "created": 0, "no_token": 0, "gave_up": 0, "errors": []}
    max_age = timedelta(hours=max(1, get_settings().apify_run_max_age_hours))
    with ctx.sessions() as db:
        pending = db.execute(select(SearchRun.id, SearchRun.user_id)
                             .where(SearchRun.provider == "apify", SearchRun.completed_at.is_(None))
                             .order_by(SearchRun.id).limit(max(1, get_settings().apify_sync_batch_size))).all()
        tokens: dict[int, bool] = {}
        for run_id, owner in pending:
            if owner is not None and owner not in tokens:
                tokens[owner] = bool(apify_token(db, owner))
            if owner is None or not tokens[owner]:
                summary["no_token"] += 1
                continue
            run = db.scalars(select(SearchRun).where(SearchRun.id == run_id, SearchRun.completed_at.is_(None))
                             .with_for_update(skip_locked=True)).first()
            if run is None:
                continue  # finished meanwhile, or another instance is importing it right now
            if run.created_at and ctx.now - run.created_at > max_age:
                # Checking forever is what kept stale runs hammering Apify: no run lasts this long.
                give_up_on_run(db, run, f"Still unfinished {max_age.total_seconds() / 3600:.0f} hours after it "
                                        "started, so it is no longer checked")
                summary["gave_up"] += 1
                continue
            try:
                out = refresh_apify_run(db, run)
            except Exception as exc:  # one bad run must not stop the others
                db.rollback()
                summary["errors"].append({"run_id": run_id, "error": str(exc)[:300]})
                continue
            summary["checked"] += 1
            summary["gave_up"] += int(bool(out.get("gave_up")))
            summary["completed"] += int(bool(out.get("completed")))
            summary["created"] += (out.get("import") or {}).get("created", 0)
    return summary


# ───────────── the queued workers: claim as slots free, with limits read live ─────────────

def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


LIVE_CHECK_SECONDS = 5.0  # how often a run re-reads "side by side" and "tasks per run" while it waits


# Tasks a pass stopped waiting for (stopped from Running workers): their threads finish on their own; a reference keeps
# the asyncio task alive until then, and their results are discarded by the workers' own lease guards.
_ABANDONED: set[asyncio.Task] = set()


async def run_live(*, concurrency: str, batch: str | None, claim: Callable[[int], list[Any]],
                   process: Callable[[Any], Awaitable[str]],
                   heartbeat: Callable[[list[Any]], set[int]] | None = None) -> tuple[int, Counter]:
    """Run one scheduler pass of a queued worker. Returns (claimed, outcomes).

    Tasks are claimed as slots free up, not all at once, so a task is only PROCESSING while it really runs, and the
    two limits an admin can change in Running workers → Settings (app.services.server_settings) apply to the run in
    progress within seconds: more side by side starts more tasks straight away, fewer lets running ones finish and
    starts none until the run is under the new limit; tasks per run caps what this pass claims. Nothing needs a
    restart, and the environment value is only the default.

    Every few seconds `heartbeat` renews the lease of each task still running and returns the ids still held. A
    model call can outlast a task's lease, which must not make it look abandoned (and be claimed twice); and a task
    stopped from Running workers lost its lease, so the pass stops waiting for it at once: its slot is free for other
    work and the pass is not held open by a call nobody wants any more.

    `batch` None leaves the size of the pass to `claim` (the retrieval indexer, whose budget is per account).

    `process` must not raise: each worker's own processing turns a failure into an outcome."""
    outcomes: Counter = Counter()
    running: dict[asyncio.Task, Any] = {}
    missing: dict[asyncio.Task, float] = {}  # tasks no longer held, since when
    claimed, exhausted = 0, False
    while True:
        room = max(1, server_settings.value(concurrency)) - len(running)
        if batch is not None:
            room = min(room, max(1, server_settings.value(batch)) - claimed)
        if room > 0 and not exhausted:
            fresh = await threads.run_io(claim, room)
            claimed += len(fresh)
            exhausted = len(fresh) < room  # nothing more is due now: the next pass picks up what arrives later
            running.update({asyncio.create_task(process(c)): c for c in fresh})
        if not running:
            return claimed, outcomes
        done, _ = await asyncio.wait(running, timeout=LIVE_CHECK_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            running.pop(task)
            missing.pop(task, None)
            outcomes[task.result()] += 1
        if heartbeat and running:
            held = await threads.run_io(heartbeat, list(running.values()))
            now = time.monotonic()
            for task, c in list(running.items()):
                if c.task_id in held:
                    missing.pop(task, None)
                    continue
                # Not held: either it just finished (its row is final a moment before its thread returns) or it was
                # stopped. Only a task still not held a whole check later is given up on.
                first = missing.setdefault(task, now)
                if now - first >= LIVE_CHECK_SECONDS and not task.done():
                    running.pop(task)
                    missing.pop(task, None)
                    _ABANDONED.add(task)
                    task.add_done_callback(_ABANDONED.discard)
                    outcomes["stopped"] += 1


def renew(sessions: Callable[[], Session], model: Any, lease_seconds: int, token: str = "lock_token") -> Callable:
    """A heartbeat for `run_live`: extend each held task's lease, return the ids still held."""
    def beat(claims: list[Any]) -> set[int]:
        held: set[int] = set()
        until = utcnow() + timedelta(seconds=lease_seconds)
        with sessions() as db:
            for c in claims:
                won = db.execute(update(model).where(model.id == c.task_id, model.status == "PROCESSING",
                                                     model.lock_token == getattr(c, token))
                                 .values(lock_expires_at=until).execution_options(synchronize_session=False))
                if won.rowcount:
                    held.add(c.task_id)
            db.commit()
        return held
    return beat


# ───────────── ranking ─────────────

def _prepare_ranking(ctx: JobContext) -> dict[str, Any] | str:
    """Deterministic sweep (filter, queue, recover, re-rank) before any ranking task is claimed."""
    with ctx.sessions() as db:
        if not _is_postgres(db):
            return "Ranking workers need PostgreSQL (FOR UPDATE SKIP LOCKED)"
        queue = ranker.sweep(db)
        _, _, waiting = ranker.accounts(db, ctx.now)
    return {"queue": queue, "accounts_waiting": waiting}


def _claim_ranking(ctx: JobContext, limit: int) -> list[ranker.TaskClaim]:
    # Accounts are looked at again on every claim: a cadence that just started, a pause or a new limit apply at once.
    with ctx.sessions() as db:
        ready, auto, _ = ranker.accounts(db)
        return ranker.claim(db, worker_id=ctx.worker_id, limit=limit, ready_accounts=ready, auto_rank_accounts=auto)


async def ranking(ctx: JobContext) -> dict[str, Any]:
    """Rank up to `ranking_batch_size` tasks, at most `ranking_concurrency` model calls at a time (both live)."""
    details = await threads.run_io(_prepare_ranking, ctx)
    if isinstance(details, str):
        return skipped(details)
    async def rank(c: ranker.TaskClaim) -> str:
        outcome = await threads.run_work(ranker.process, ctx.sessions, c)
        scheduler_hooks.nudge("qualification")  # a graph run waiting for this ranking may go on (a hint only)
        return outcome

    claimed, outcomes = await run_live(
        concurrency="ranking_concurrency", batch="ranking_batch_size",
        claim=lambda n: _claim_ranking(ctx, n), process=rank,
        heartbeat=renew(ctx.sessions, JobRankingTask, get_settings().ranking_lease_seconds, token="token"))
    summary = {"claimed": claimed, **{name: outcomes.get(name, 0) for name in ranker.OUTCOMES}, **details}
    if not claimed and not any(details["queue"].values()):
        return skipped("Nothing to rank", **summary)
    return summary


def _prepare_jd_parsing(ctx: JobContext) -> dict[str, Any] | str:
    """Queue postings that were never parsed and recover crashed workers before anything is claimed."""
    with ctx.sessions() as db:
        if not _is_postgres(db):
            return "Job description parsing needs PostgreSQL (FOR UPDATE SKIP LOCKED)"
        recovered = job_description.recover_expired(db)
        queue = job_description.sweep(db)
    return {"queue": queue, "recovered": recovered}


def _claim_jd_parsing(ctx: JobContext, limit: int) -> list[job_description.ParseClaim]:
    with ctx.sessions() as db:
        return job_description.claim(db, worker_id=ctx.worker_id, limit=limit)


def _prepare_resume_parsing(ctx: JobContext) -> dict[str, Any] | str:
    with ctx.sessions() as db:
        if not _is_postgres(db):
            return "Resume parsing needs PostgreSQL (FOR UPDATE SKIP LOCKED)"
        return {"recovered": resume_parsing.recover_expired(db)}


def _claim_resume_parsing(ctx: JobContext, limit: int) -> list[resume_parsing.ParseClaim]:
    with ctx.sessions() as db:
        return resume_parsing.claim(db, worker_id=ctx.worker_id, limit=limit)


async def resume_parse(ctx: JobContext) -> dict[str, Any]:
    """Structure uploaded resumes with the Resume Parser model (the LangGraph graph when it can run),
    `resume_parse_concurrency` at a time."""
    from app.services import resume_parsing_graph

    if not get_settings().resume_parse_enabled:
        return skipped("Resume parsing is off (RESUME_PARSE_ENABLED)")
    details = await threads.run_io(_prepare_resume_parsing, ctx)
    if isinstance(details, str):
        return skipped(details)
    claimed, outcomes = await run_live(
        concurrency="resume_parse_concurrency", batch="resume_parse_batch_size",
        claim=lambda n: _claim_resume_parsing(ctx, n),
        process=lambda c: threads.run_work(resume_parsing_graph.run, ctx.sessions, c),
        heartbeat=renew(ctx.sessions, ResumeParseTask, get_settings().resume_parse_lease_seconds))
    summary = {"claimed": claimed, **{name: outcomes.get(name, 0) for name in resume_parsing.OUTCOMES}, **details}
    if not claimed:
        return skipped("No resumes waiting", **summary)
    return summary


def _prepare_domain_classifier(ctx: JobContext) -> dict[str, Any] | str:
    with ctx.sessions() as db:
        if not _is_postgres(db):
            return "The domain classifier needs PostgreSQL (FOR UPDATE SKIP LOCKED)"
        return {"recovered": domain_classifier.recover_expired(db), "swept": domain_classifier.sweep(db)}


def _claim_domain_classifier(ctx: JobContext, limit: int) -> list[domain_classifier.ClassifyClaim]:
    with ctx.sessions() as db:
        return domain_classifier.claim(db, worker_id=ctx.worker_id, limit=limit)


async def domain_classifier_job(ctx: JobContext) -> dict[str, Any]:
    """Read the jobs the rules could not settle, `domain_classifier_concurrency` model calls at a time, one request per
    job. Runs in mode on, in shadow, and once more after being turned off, to release jobs still waiting."""
    details = await threads.run_io(_prepare_domain_classifier, ctx)
    if isinstance(details, str):
        return skipped(details)
    claimed, outcomes = await run_live(
        concurrency="domain_classifier_concurrency", batch="domain_classifier_batch_size",
        claim=lambda n: _claim_domain_classifier(ctx, n),
        process=lambda c: threads.run_work(domain_classifier.process, ctx.sessions, c),
        heartbeat=renew(ctx.sessions, JobClassificationTask, get_settings().domain_classifier_lease_seconds))
    summary = {"claimed": claimed, **{name: outcomes.get(name, 0) for name in domain_classifier.OUTCOMES}, **details}
    if not claimed and not details["swept"]:
        return skipped("No jobs waiting for the domain check", **summary)
    from app import scheduler_hooks

    for job in ("jd_recovery", "jd_parsing", "ranking", "qualification"):
        scheduler_hooks.nudge(job)  # what it let through may go on now (a hint only)
    return summary


async def jd_parsing(ctx: JobContext) -> dict[str, Any]:
    """Parse up to `jd_parse_batch_size` descriptions, `jd_parse_concurrency` model calls at a time (both live)."""
    if not get_settings().jd_parse_enabled:
        return skipped("Job description parsing is off (JD_PARSE_ENABLED)")
    details = await threads.run_io(_prepare_jd_parsing, ctx)
    if isinstance(details, str):
        return skipped(details)
    claimed, outcomes = await run_live(
        concurrency="jd_parse_concurrency", batch="jd_parse_batch_size",
        claim=lambda n: _claim_jd_parsing(ctx, n),
        process=lambda c: threads.run_work(job_description.process, ctx.sessions, c),
        heartbeat=renew(ctx.sessions, JobDescriptionParseTask, get_settings().jd_parse_lease_seconds))
    summary = {"claimed": claimed, **{name: outcomes.get(name, 0) for name in job_description.OUTCOMES}, **details}
    if not claimed and not details["queue"]["queued"]:
        return skipped("No job descriptions waiting", **summary)
    return summary


def _prepare_jd_recovery(ctx: JobContext) -> dict[str, Any]:
    """Recover crashed workers and assess never-assessed descriptions (queueing recovery) before claiming.

    Claiming is a guarded UPDATE (plus SKIP LOCKED on PostgreSQL), so it is safe on SQLite local installs too."""
    with ctx.sessions() as db:
        expired = jd_recovery.recover_expired(db, ctx.now)
        queue = jd_recovery.sweep(db, now=ctx.now)
    # "recovered" is an outcome (a description recovered); leases taken back from crashed workers are counted apart.
    return {"queue": queue, "expired_leases": expired}


def _claim_jd_recovery(ctx: JobContext, limit: int) -> list[jd_recovery.FetchClaim]:
    with ctx.sessions() as db:
        # Its own time: a live pass claims for longer than a task's lease, and a lease must start when its claim does.
        return jd_recovery.claim(db, worker_id=ctx.worker_id, limit=limit)


async def jd_recovery_job(ctx: JobContext) -> dict[str, Any]:
    """Recover up to `jd_fetch_batch_size` missing descriptions, `jd_fetch_max_concurrency` at a time (both live).

    HTTP first; the shared browser only for pages HTTP could not read, and never more pages at once than
    PLAYWRIGHT_MAX_CONCURRENCY. One task failing never affects another task, or any other scheduled job."""
    from app.infrastructure.browser import get_browser_service

    details = await threads.run_io(_prepare_jd_recovery, ctx)
    browser = get_browser_service()

    async def run(claim: jd_recovery.FetchClaim) -> str:
        try:
            outcome = await jd_recovery.process(ctx.sessions, claim, browser=browser)
        except Exception:  # noqa: BLE001  the lease expires and the task is retried (recover_expired)
            return "lost"
        if outcome != "lost":
            # Ranking skips a job while its recovery is active, so any end of recovery (recovered, failed, blocked,
            # skipped, unchanged…) may have made a queued ranking ready. Only new text needs a parse.
            if outcome == "recovered":
                scheduler_hooks.nudge("jd_parsing")
            scheduler_hooks.nudge("ranking")
            scheduler_hooks.nudge("qualification")
        return outcome

    try:
        claimed, outcomes = await run_live(
            concurrency="jd_fetch_max_concurrency", batch="jd_fetch_batch_size",
            claim=lambda n: _claim_jd_recovery(ctx, n), process=run,
            heartbeat=renew(ctx.sessions, JobDescriptionFetchTask, get_settings().jd_fetch_lease_seconds))
    finally:
        await browser.release_idle()
    summary = {"claimed": claimed, **{name: outcomes.get(name, 0) for name in jd_recovery.OUTCOMES}, **details}
    if not claimed and not details["queue"]["queued"] and not details["expired_leases"]:
        return skipped("No job descriptions waiting for recovery", **summary)
    return summary


async def listing_check(ctx: JobContext) -> dict[str, Any]:
    """Re-read a bounded batch of postings (every job up to approval, about once a day) and delete the ones that no
    longer exist or no longer take applications; approved jobs are labelled instead.

    HTTP first, the shared browser only when HTTP found nothing readable — never after a login wall, a challenge or
    a 429 — and never a model. Only a removed or closed page deletes: blocked pages, timeouts and server errors change
    nothing (app.services.job_removal)."""
    from app.infrastructure.browser import get_browser_service
    from app.services import job_removal

    if not get_settings().auto_delete_removed_jobs:
        return skipped("Removed postings are kept (AUTO_DELETE_REMOVED_JOBS=false)")
    browser = get_browser_service()
    try:
        summary = await job_removal.check_listings(ctx.sessions, ctx.now, browser=browser)
    finally:
        await browser.release_idle()
    if not summary["checked"]:
        return skipped("No postings due for a check", **summary)
    return summary


def _prepare_job_insights(ctx: JobContext) -> dict[str, Any]:
    from app.services.job_insights import tasks as insight_tasks

    with ctx.sessions() as db:
        expired = insight_tasks.recover_expired(db, ctx.now)
        queue = insight_tasks.sweep(db, now=ctx.now)
    return {"queue": queue, "expired_leases": expired}


def _claim_job_insights(ctx: JobContext, limit: int) -> list[Any]:
    from app.services.job_insights import tasks as insight_tasks

    with ctx.sessions() as db:
        return insight_tasks.claim(db, worker_id=ctx.worker_id, limit=limit)


async def job_insights(ctx: JobContext) -> dict[str, Any]:
    """Research up to `job_insights_batch_size` approved jobs, `job_insights_max_concurrency` at a time (both live).

    Each run reads the posting, the recruiter's notes, the company's site and the configured Apify actors side by
    side, then the model organises what was found. A source failing never fails the run or another job."""
    from app.services.job_insights import graph as insight_graph
    from app.services.job_insights import service as insight_service

    if not get_settings().job_insights_enabled:
        return skipped("Job insights are off (JOB_INSIGHTS_ENABLED=false)")
    details = await threads.run_io(_prepare_job_insights, ctx)
    claimed, outcomes = await run_live(
        concurrency="job_insights_max_concurrency", batch="job_insights_batch_size",
        claim=lambda n: _claim_job_insights(ctx, n),
        # JOB_INSIGHTS_GRAPH_MODE picks the path (off: service.process exactly as before)
        process=lambda c: threads.run_work(insight_graph.run, ctx.sessions, c),
        heartbeat=renew(ctx.sessions, JobInsightTask, get_settings().job_insights_lease_seconds))
    summary = {"claimed": claimed, **{name: outcomes.get(name, 0) for name in insight_service.OUTCOMES}, **details}
    if not claimed and not details["queue"]["queued"] and not details["expired_leases"]:
        return skipped("No approved jobs waiting for insights", **summary)
    return summary


# ───────────── enrichment, applications, follow-ups, inbox ─────────────

def compensation(ctx: JobContext) -> dict[str, Any]:
    """Record the salary each posting states. Glassdoor and PayScale are marked not configured.

    Rows are read without locks and written with a guarded UPDATE, so enrichment never holds row locks
    while it works (which would make the ranking claim skip those jobs), and a job checked by another
    instance in the meantime is not written twice.
    """
    with ctx.sessions() as db:
        rows = db.execute(select(Job.id, Job.raw).where(Job.compensation_checked_at.is_(None)).order_by(Job.id)
                          .limit(max(1, get_settings().compensation_batch_size))).all()
        results = [(job_id, enrich_compensation(raw, ctx.now)) for job_id, raw in rows]
        checked = found = 0
        for job_id, data in results:
            written = db.execute(update(Job).where(Job.id == job_id, Job.compensation_checked_at.is_(None))
                                 .values(compensation=data, compensation_checked_at=ctx.now)
                                 .execution_options(synchronize_session=False)).rowcount
            checked += written
            found += written and data["status"] == "found"
        db.commit()
        remaining = db.scalar(select(func.count(Job.id)).where(Job.compensation_checked_at.is_(None))) or 0
    return {"checked": checked, "salary_found": found, "remaining": remaining}


def applications(ctx: JobContext) -> dict[str, Any]:
    """Report the application queue, and mark pre-submit claims whose lease ran out as UNCERTAIN (a person says what
    happened). Submission stays a human action, so nothing is submitted here.

    While the application worker is off — its mode, or AGENT_GRAPHS_ENABLED, which also unschedules its own job —
    this always-on pass settles the runs it left: expired leases are taken back and pending runs end MODE_OFF, so no
    row stays active in Running workers or blocks a later run of the same job."""
    from app.services import application_execution, apply_queue

    worker_off: dict[str, Any] = {}
    with ctx.sessions() as db:
        uncertain = apply_queue.expire_claims(db, now=ctx.now)
        if application_execution.mode() == "off":
            worker_off = {"execution_leases_taken_back": application_execution.recover_expired(db, ctx.now),
                          "executions_ended": application_execution.release(db, ctx.now)}
        counts = dict(db.execute(select(Job.state, func.count()).where(Job.state.in_((S.READY_TO_APPLY, S.APPLYING)))
                                 .group_by(Job.state)).all())
    return skipped("Automatic application submission is not enabled; approved packets stay in the apply kit",
                   ready_to_apply=counts.get(S.READY_TO_APPLY, 0), applying=counts.get(S.APPLYING, 0),
                   claims_now_uncertain=uncertain, **worker_off)


def followups(ctx: JobContext) -> dict[str, Any]:
    """Draft each account's due follow-ups with its own AI key. Drafts only; nothing is sent."""
    accounts = _account_ids(ctx)
    drafted, no_ai_key, errors = 0, 0, []
    for user_id in accounts:
        try:
            with _as_user(ctx.sessions(), user_id) as db:
                if not ai_ready(db, user_id)[0]:
                    no_ai_key += 1
                    continue
                drafted += create_due_followups(db)
        except Exception as exc:
            errors.append(_error(user_id, exc))
    summary = {"accounts": len(accounts), "drafted": drafted, "no_ai_key": no_ai_key, "errors": errors}
    if not accounts:
        return skipped("No accounts yet", **summary)
    return summary


def auto_tailor_job(ctx: JobContext) -> dict[str, Any]:
    """Queue qualified jobs for tailoring in accounts that turned automatic tailoring on. Ranking queues them
    straight away; this catches any the ranking hook missed. The tailor worker runs them."""
    accounts = _account_ids(ctx)
    enabled, queued, errors = 0, 0, []
    for user_id in accounts:
        try:
            with _as_user(ctx.sessions(), user_id) as db:
                result = auto_tailor.sweep(db)
                if result is not None:
                    enabled += 1
                    queued += result["queued"]
        except Exception as exc:
            errors.append(_error(user_id, exc))
    summary = {"accounts": enabled, "queued": queued, "errors": errors}
    if not enabled and not errors:
        return skipped("No account has automatic tailoring on", **summary)
    return summary


async def inbox(ctx: JobContext) -> dict[str, Any]:
    """Read each connected account's Gmail (read-only) for updates on its submitted applications.

    One account at a time, each in its own session: a Gmail outage, an expired grant or a rate limit stops that
    account's sync and nothing else. Model analyses that failed earlier are retried even without Gmail."""
    from app.models import ApplicationEmailMessage, GmailConnection
    from app.services.application_inbox.sync import sync_account

    settings = get_settings()
    if not settings.application_inbox_enabled and ctx.trigger == "schedule":
        return skipped("The application inbox is off (APPLICATION_INBOX_ENABLED)")
    if not settings.gmail_oauth_configured:
        return skipped("Gmail is not set up on this server (GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET)")

    def accounts() -> list[int]:
        with ctx.sessions() as db:
            connected = set(db.scalars(select(GmailConnection.user_id)
                                       .where(GmailConnection.status == "CONNECTED")).all())
            waiting = set(db.scalars(select(ApplicationEmailMessage.user_id).distinct()
                                     .where(ApplicationEmailMessage.analysis_status == "PENDING")).all())
        return sorted(connected | waiting)

    users = await asyncio.to_thread(accounts)
    if not users:
        return skipped("No account has connected Gmail")
    statuses: Counter = Counter()
    totals: Counter = Counter()
    errors = []
    for user_id in users:
        try:
            result = await asyncio.to_thread(sync_account, ctx.sessions, user_id, trigger=ctx.trigger,
                                             worker_id=ctx.worker_id, now=ctx.now)
        except Exception as exc:  # noqa: BLE001  one account's failure must not stop the others
            errors.append(_error(user_id, exc))
            continue
        statuses[result.get("status", "unknown")] += 1
        totals.update({k: v for k, v in result.items() if isinstance(v, int) and k not in ("user_id", "limit",
                                                                                          "duration_ms")})
        if result.get("status") not in ("succeeded", "partial", "skipped"):
            errors.append({"user_id": user_id, "status": result.get("status"), "error": result.get("error")})
    return {"accounts": len(users), "statuses": dict(statuses), **dict(totals), "errors": errors}


# ───────────── analytics, cleanup ─────────────

def analytics(ctx: JobContext) -> dict[str, Any]:
    """Snapshot of the whole system, kept as this job's last result (visible to the admin only)."""
    with ctx.sessions() as db:
        count = lambda *where: db.scalar(select(func.count()).select_from(Job).where(*where)) or 0
        by_state = {str(state): n for state, n in db.execute(select(Job.state, func.count()).group_by(Job.state)).all()}
        by_status = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
        by_stage = {stage or "applied": n for stage, n in db.execute(
            select(Application.stage, func.count()).where(Application.applied_at.is_not(None))
            .group_by(Application.stage)).all()}
        pending = {"ranking": count(Job.state.in_(RANK_PENDING_STATES)),
                   "compensation": count(Job.compensation_checked_at.is_(None)),
                   "search_runs": db.scalar(select(func.count(SearchRun.id)).where(SearchRun.completed_at.is_(None))) or 0}
        failed = count(Job.state == S.FAILED)
        accounts = db.scalar(select(func.count(User.id))) or 0
    return {"accounts": accounts, "jobs_by_state": by_state, "jobs_by_status": by_status,
            "applications_by_stage": by_stage, "pending": pending, "failed_jobs": failed,
            "generated_at": ctx.now.isoformat()}


def cleanup(ctx: JobContext) -> dict[str, Any]:
    """Recover work whose worker crashed, and apply telemetry retention (roll up, then delete old rows).

    Three retentions, each with its own setting: LLM requests and external API calls are rolled into daily
    totals that are kept for good, while operational log lines are simply deleted.
    """
    with ctx.sessions() as db:
        telemetry = monitoring.prune(db, ctx.now)
        external = external_monitoring.prune(db, ctx.now)
        external["captured_cleared"] = external_monitoring.clear_captured(db, ctx.now)
        logs = system_log.prune(db, ctx.now)
        retention = {"telemetry": telemetry, "external_api": external, "system_logs": logs,
                     "domain_classifier_cache": domain_classifier.purge_cache(db, ctx.now)}
    if get_settings().agent_graphs_enabled:  # graph checkpoints (app.agents); LangGraph is not loaded otherwise
        from app.agents import runtime as agent_runtime

        try:
            retention["agent_checkpoints"] = agent_runtime.prune(ctx.sessions, ctx.now)
        except Exception as exc:  # noqa: BLE001  checkpoint retention must not stop the rest of cleanup
            retention["agent_checkpoints"] = {"error": f"{type(exc).__name__}"}
    from app.services import rag_index

    try:
        with ctx.sessions() as db:
            retention["retired_rag_indexes"] = rag_index.purge_retired(db, ctx.now)
    except Exception as exc:  # noqa: BLE001  index retention must not stop the rest of cleanup
        retention["retired_rag_indexes"] = {"error": f"{type(exc).__name__}"}
    with ctx.sessions() as db:
        if not _is_postgres(db):
            return skipped("Lease recovery needs PostgreSQL", **retention)
        return {"recovered_leases": wq.recover_expired_leases(db), **retention}


AGENT_RECOVERY_BATCH = 5


async def agent_graphs(ctx: JobContext) -> dict[str, Any]:
    """Continue agent graph runs whose worker died (lease expired) or that never started, from their last checkpoint.

    Off unless AGENT_GRAPHS_ENABLED. A run's effects are recorded once (app.agents.effects), so continuing a run that
    had already made one never repeats it."""
    if not get_settings().agent_graphs_enabled:
        return skipped("Agent graphs are off (AGENT_GRAPHS_ENABLED)")
    with ctx.sessions() as db:
        if not _is_postgres(db):
            return skipped("Agent graphs need PostgreSQL")
    from app.agents import runtime as agent_runtime

    runs = await threads.run_io(agent_runtime.claim_stale, ctx.sessions, ctx.worker_id, AGENT_RECOVERY_BATCH)
    if not runs:
        return skipped("No agent graph run to recover")
    outcomes: Counter = Counter()
    for run in runs:
        outcomes[await threads.run_work(agent_runtime.execute, ctx.sessions, run)] += 1
    return {"recovered": len(runs), **outcomes}


def _prepare_qualification(ctx: JobContext, mode: str) -> dict[str, Any]:
    """Take back leases of crashed runs, then: off or shadow → hand ON tasks back to the current path; shadow → queue
    the day's shadow runs. Never calls a model."""
    from app.services import qualification

    with ctx.sessions() as db:
        details: dict[str, Any] = {"mode": mode, "expired_leases": qualification.recover_expired(db, ctx.now)}
        if mode != "on":  # the current path qualifies again: runs left from mode on go back to it
            details["released"] = qualification.release(db, ctx.now)
        if mode == "shadow":
            details["shadow"] = qualification.shadow_sweep(db, ctx.now)
    return details


def _claim_qualification(ctx: JobContext, limit: int, task_mode: str) -> list[Any]:
    from app.services import qualification

    with ctx.sessions() as db:
        return qualification.claim(db, worker_id=ctx.worker_id, limit=limit, task_mode=task_mode)


async def qualification_job(ctx: JobContext) -> dict[str, Any]:
    """Advance qualification graph runs (app.agents.qualification): each claimed task's thread runs until its job is
    decided or it waits for recovery, parsing or ranking, which the existing workers do. Shadow runs only read.

    Off unless AGENT_GRAPHS_ENABLED; in mode off it only hands tasks left from mode on back to the current path."""
    if not get_settings().agent_graphs_enabled:
        return skipped("Agent graphs are off (AGENT_GRAPHS_ENABLED)")
    with ctx.sessions() as db:
        if not _is_postgres(db):
            return skipped("The qualification graph needs PostgreSQL")
    from app.services import qualification

    mode = qualification.mode()
    details = await threads.run_io(_prepare_qualification, ctx, mode)
    if mode == "off":
        if not details.get("released") and not details.get("expired_leases"):
            return skipped("The qualification graph is off (QUALIFICATION_GRAPH_MODE)", **details)
        return details
    task_mode = qualification.ON if mode == "on" else qualification.SHADOW
    claimed, outcomes = await run_live(
        concurrency="qualification_concurrency", batch="qualification_batch_size",
        claim=lambda n: _claim_qualification(ctx, n, task_mode),
        process=lambda c: threads.run_work(qualification.process, ctx.sessions, c),
        heartbeat=qualification.renew(ctx.sessions))
    summary = {"claimed": claimed, **{name: outcomes.get(name, 0) for name in qualification.OUTCOMES}, **details}
    if not claimed and not (details.get("shadow") or {}).get("queued"):
        return skipped("No qualification runs waiting", **summary)
    return summary


def _prepare_preparation(ctx: JobContext, mode: str) -> dict[str, Any]:
    """Take back leases of crashed runs, then: off or shadow → hand ON tasks back to the current path; shadow → queue
    the day's shadow runs. Never calls a model."""
    from app.services import preparation

    with ctx.sessions() as db:
        details: dict[str, Any] = {"mode": mode, "expired_leases": preparation.recover_expired(db, ctx.now)}
        if mode != "on":
            details["released"] = preparation.release(db, ctx.now)
        if mode == "shadow":
            details["shadow"] = preparation.shadow_sweep(db, ctx.now)
    return details


def _claim_preparation(ctx: JobContext, limit: int, task_mode: str) -> list[Any]:
    from app.services import preparation

    with ctx.sessions() as db:
        return preparation.claim(db, worker_id=ctx.worker_id, limit=limit, task_mode=task_mode)


async def preparation_job(ctx: JobContext) -> dict[str, Any]:
    """Advance resume preparation graph runs (app.agents.preparation): each claimed task's thread runs until its packet
    is decided or it waits for the tailor worker or for a person. Shadow runs only read.

    Off unless AGENT_GRAPHS_ENABLED; in mode off it only hands tasks left from mode on back to the current path."""
    if not get_settings().agent_graphs_enabled:
        return skipped("Agent graphs are off (AGENT_GRAPHS_ENABLED)")
    with ctx.sessions() as db:
        if not _is_postgres(db):
            return skipped("The preparation graph needs PostgreSQL")
    from app.services import preparation

    mode = preparation.mode()
    details = await threads.run_io(_prepare_preparation, ctx, mode)
    if mode == "off":
        if not details.get("released") and not details.get("expired_leases"):
            return skipped("The preparation graph is off (PREPARATION_GRAPH_MODE)", **details)
        return details
    task_mode = preparation.ON if mode == "on" else preparation.SHADOW
    claimed, outcomes = await run_live(
        concurrency="preparation_concurrency", batch="preparation_batch_size",
        claim=lambda n: _claim_preparation(ctx, n, task_mode),
        process=lambda c: threads.run_work(preparation.process, ctx.sessions, c),
        heartbeat=preparation.renew(ctx.sessions))
    summary = {"claimed": claimed, **{name: outcomes.get(name, 0) for name in preparation.OUTCOMES}, **details}
    if not claimed and not (details.get("shadow") or {}).get("queued"):
        return skipped("No preparation runs waiting", **summary)
    return summary


def _prepare_application_execution(ctx: JobContext, mode: str) -> dict[str, Any]:
    """Take back leases of crashed runs (RETRYING, never towards a submit) and mark expired pre-submit claims
    UNCERTAIN; mode off → end the tasks left over."""
    from app.services import application_execution, apply_queue

    with ctx.sessions() as db:
        details: dict[str, Any] = {"mode": mode,
                                   "expired_leases": application_execution.recover_expired(db, ctx.now),
                                   "claims_now_uncertain": apply_queue.expire_claims(db, now=ctx.now),
                                   "takeovers_ended": application_execution.end_expired_takeovers(db, ctx.now),
                                   "submitted_in_window": application_execution.watch_windows(db),
                                   "captchas_answered": application_execution.watch_challenges(db),
                                   "fields_filled_in_window": application_execution.watch_filled_windows(db)}
        if mode == "off":
            details["released"] = application_execution.release(db, ctx.now)
    return details


def _claim_application_execution(ctx: JobContext, limit: int) -> list[Any]:
    from app.services import application_execution

    with ctx.sessions() as db:
        return application_execution.claim(db, worker_id=ctx.worker_id, limit=limit)


async def application_execution_job(ctx: JobContext) -> dict[str, Any]:
    """Advance application execution runs (app.agents.application): read an approved job's form, resolve where each
    answer comes from, and in assist mode fill it in an ephemeral browser page and validate it — ending at
    READY_TO_SUBMIT. Nothing here submits an application.

    Off unless AGENT_GRAPHS_ENABLED and APPLICATION_EXECUTION_MODE; in mode off it only ends tasks left over."""
    if not get_settings().agent_graphs_enabled:
        return skipped("Agent graphs are off (AGENT_GRAPHS_ENABLED)")
    with ctx.sessions() as db:
        if not _is_postgres(db):
            return skipped("The application worker needs PostgreSQL")
    from app.infrastructure.application_browser import get_application_sessions
    from app.infrastructure.browser import get_browser_service
    from app.services import application_execution

    mode = application_execution.mode()
    details = await threads.run_io(_prepare_application_execution, ctx, mode)
    if mode == "off":
        if not details.get("released") and not details.get("expired_leases"):
            return skipped("The application worker is off (APPLICATION_EXECUTION_MODE)", **details)
        return details
    from app.infrastructure.application_browser.browser_use_sidecar import get_sidecars

    browser = get_application_sessions()
    run_async = application_execution.loop_runner(asyncio.get_running_loop())
    await get_sidecars().reap()  # takeover windows nobody came back to
    try:
        claimed, outcomes = await run_live(
            concurrency="application_execution_concurrency", batch="application_execution_batch_size",
            claim=lambda n: _claim_application_execution(ctx, n),
            process=lambda c: threads.run_work(application_execution.process, ctx.sessions, c, browser=browser,
                                               run_async=run_async),
            heartbeat=application_execution.renew(ctx.sessions))
    finally:
        await get_browser_service().release_idle()
    summary = {"claimed": claimed, **{name: outcomes.get(name, 0) for name in application_execution.OUTCOMES},
               **details}
    if not claimed:
        return skipped("No application runs waiting", **summary)
    return summary


async def rag_index_job(ctx: JobContext) -> dict[str, Any]:
    """Embed each account's verified evidence and saved answers into its retrieval index (app.services.rag_index).

    Off unless RAG_VECTOR_ENABLED. Every account is swept first (its queue is kept while it is paused); then batches
    are claimed round-robin across accounts and embedded `rag_index_concurrency` at a time (live, run_live), each
    account up to `rag_index_max_entries_per_run` entries. An account without a usable embedding model (none assigned
    to the Embedder, or not in LiteLLM gateway mode) is skipped with the reason; a provider failure stops that
    account's pass and its entries are retried later. A new model is built beside the serving index and replaces it
    only when complete."""
    if not get_settings().rag_vector_enabled:
        return skipped("Vector retrieval is off (RAG_VECTOR_ENABLED)")
    from app.services import rag_index

    accounts = _account_ids(ctx)
    summary: dict[str, Any] = {"accounts": len(accounts), "claimed": 0, "embedded": 0, "failed": 0, "skipped": {},
                               "errors": []}
    passes: list[rag_index.AccountPass] = []
    for user_id in accounts:
        try:
            prepared = await threads.run_work(rag_index.prepare, ctx.sessions, user_id)
        except Exception as exc:  # noqa: BLE001  one account's failure must not stop the others
            summary["errors"].append(_error(user_id, exc))
            continue
        if isinstance(prepared, dict):
            summary["skipped"][prepared["skipped"]] = summary["skipped"].get(prepared["skipped"], 0) + 1
        else:
            passes.append(prepared)
    owner: dict[str, rag_index.AccountPass] = {}
    open_passes = list(passes)

    def claim(limit: int) -> list[rag_index.Claimed]:
        out: list[rag_index.Claimed] = []
        while len(out) < limit and open_passes:  # one batch per account per round: no account starves the others
            for run in list(open_passes):
                if len(out) >= limit:
                    break
                try:
                    claimed = rag_index.take(ctx.sessions, run)
                except Exception as exc:  # noqa: BLE001
                    summary["errors"].append(_error(run.user_id, exc))
                    claimed = None
                if claimed is None:
                    open_passes.remove(run)
                    continue
                owner[claimed.token] = run
                out.append(claimed)
        return out

    async def process(claimed: rag_index.Claimed) -> str:
        run = owner[claimed.token]
        try:
            return await threads.run_work(rag_index.work, ctx.sessions, run, claimed)
        except Exception as exc:  # noqa: BLE001  the lease expires and the entries are claimed again
            summary["errors"].append(_error(run.user_id, exc))
            return "error"

    if passes:
        claimed, _ = await run_live(concurrency="rag_index_concurrency", batch=None, claim=claim, process=process,
                                    heartbeat=lambda claims: rag_index.renew(ctx.sessions, claims))
        summary["claimed"] = claimed
    for run in passes:
        try:
            result = await threads.run_io(rag_index.finish, ctx.sessions, run)
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(_error(run.user_id, exc))
            continue
        summary["embedded"] += result.get("embedded", 0)
        summary["failed"] += result.get("failed", 0)
        if result.get("error"):
            summary["errors"].append({"user_id": run.user_id, "error": result["error"]})
    if not passes and not summary["errors"] and summary["skipped"]:
        return skipped("No account has an embedding index to build", **summary)
    return summary


def litellm_telemetry(ctx: JobContext) -> dict[str, Any]:
    """Fill in the auto router's tier and cause from LiteLLM's spend logs (best effort, bounded)."""
    from app.services.litellm_routing_import import import_routing

    result = import_routing(ctx.sessions, ctx.now)
    if not result["accounts"]:
        return skipped("No auto-router requests waiting for LiteLLM routing details", **result)
    return result


def model_health_job(ctx: JobContext) -> dict[str, Any]:
    """Judge each account's recently used model routes against its failover thresholds. Recommends nothing and
    edits no configuration: a breaching route is rested (an overlay) and a notification is queued."""
    from app.services import model_health

    accounts = _account_ids(ctx)
    summary: dict[str, Any] = {"accounts": len(accounts), "routes": 0, "tripped": 0, "errors": []}
    for user_id in accounts:
        try:
            with _as_user(ctx.sessions(), user_id) as db:
                result = model_health.evaluate(db, user_id, now=ctx.now)
            summary["routes"] += result["routes"]
            summary["tripped"] += result["tripped"]
        except Exception as exc:  # one account's failure must not stop the others
            summary["errors"].append(_error(user_id, exc))
    if not summary["routes"] and not summary["errors"]:
        return skipped("No model requests in any failover window", **summary)
    return summary


ADVISOR_BATCH = 5


def ai_optimization(ctx: JobContext) -> dict[str, Any]:
    """Queue scheduled analyses, then process a few waiting AI optimization tasks. Recommendations only."""
    with ctx.sessions() as db:
        queued = ai_advisor.enqueue_scheduled(db, ctx.now)
        recovered = ai_advisor.recover_expired(db, ctx.now)
    outcomes: Counter = Counter()
    for _ in range(ADVISOR_BATCH):
        claim = ai_advisor.claim_next(ctx.sessions, ctx.worker_id)
        if claim is None:
            break
        outcomes[ai_advisor.process(ctx.sessions, claim)] += 1
    summary = {"scheduled": queued, "recovered": recovered, **{k: outcomes.get(k, 0) for k in
                                                               ("completed", "retrying", "failed", "lost")}}
    if not queued and not recovered and not outcomes:
        return skipped("No analyses waiting", **summary)
    return summary


JOBS: list[JobSpec] = [
    JobSpec("hunter", "Hunter / Apify search", "hunter_interval_minutes", hunter,
            "Starts each account's saved search when its schedule is not manual, with its own Apify key."),
    JobSpec("apify_sync", "Apify result sync", "apify_sync_interval_minutes", apify_sync,
            "Imports datasets of Apify runs that finished."),
    JobSpec("ranking", "Job ranking", "ranking_interval_minutes", ranking,
            "Filters and queues new jobs, then ranks a bounded batch of ranking tasks, a few at a time."),
    JobSpec("domain_classifier", "Domain check", "domain_classifier_interval_minutes", domain_classifier_job,
            "Reads jobs the rules could not settle with a fast model (DOMAIN_CLASSIFIER_MODE), before anything expensive "
            "is spent on them. Only a confirmed NO_MATCH hides a job."),
    JobSpec("jd_parsing", "Job description parsing", "jd_parse_interval_minutes", jd_parsing,
            "Parses job descriptions into structured requirements, each tied to the sentence it came from."),
    JobSpec("resume_parse", "Resume parsing", "resume_parse_interval_minutes", resume_parse,
            "Structures uploaded resumes with the Resume Parser model (LangGraph when AGENT_GRAPHS_ENABLED), keeps only "
            "what the resume's text states, and fills the structured data while nobody has reviewed it."),
    JobSpec("jd_recovery", "Job description recovery", "jd_recovery_interval_minutes", jd_recovery_job,
            "Reads missing job descriptions from their posting URLs: HTTP first, the browser only when needed."),
    JobSpec("listing_check", "Removed posting check", "listing_check_interval_minutes", listing_check,
            "Re-reads postings daily (HTTP, then the browser when needed) and deletes closed ones (approved jobs are kept)."),
    JobSpec("compensation", "Salary enrichment", "compensation_interval_minutes", compensation,
            "Records the salary stated in each posting."),
    JobSpec("job_insights", "Job insights", "job_insights_interval_minutes", job_insights,
            "Researches approved jobs: salary, company, values and interviews, each fact with its source."),
    JobSpec("auto_tailor", "Automatic tailoring", "auto_tailor_interval_minutes", auto_tailor_job,
            "Queues newly qualified jobs for tailoring in accounts that turned it on; the tailor worker runs them "
            "within each account's tailoring limit."),
    JobSpec("applications", "Application processing", "application_interval_minutes", applications,
            "Reports the application queue; submission is not automated."),
    JobSpec("followups", "Follow-ups", "followup_interval_minutes", followups,
            "Drafts due follow-ups without sending them."),
    JobSpec("inbox", "Application inbox", "application_inbox_interval_minutes", inbox,
            "Reads connected Gmail accounts (read-only) for updates on submitted applications."),
    JobSpec("analytics", "Analytics", "analytics_interval_minutes", analytics,
            "Stores a pipeline snapshot."),
    JobSpec("ai_optimization", "AI optimization advisor", "ai_optimization_interval_minutes", ai_optimization,
            "Analyses LLM usage and writes recommendations; never changes configuration."),
    JobSpec("litellm_telemetry", "LiteLLM routing import", "litellm_telemetry_interval_minutes", litellm_telemetry,
            "Adds the auto router's tier and cause from LiteLLM's spend logs to Monitoring."),
    JobSpec("model_health", "Model health & failover", "model_health_interval_minutes", model_health_job,
            "Judges each model's recent performance and rests the ones past the failover thresholds; never edits configuration."),
    JobSpec("agent_graphs", "Agent graph recovery", "agent_recovery_interval_minutes", agent_graphs,
            "Continues agent graph runs (LangGraph pilot) left by a crashed worker from their last checkpoint; "
            "off unless AGENT_GRAPHS_ENABLED."),
    JobSpec("qualification", "Qualification graph", "qualification_interval_minutes", qualification_job,
            "Runs the LangGraph qualification graph (admission, description, parse, hard filters, evidence, ranking) "
            "over the existing workers, or compares it read-only with the current path in shadow mode; off unless "
            "AGENT_GRAPHS_ENABLED and QUALIFICATION_GRAPH_MODE."),
    JobSpec("preparation", "Preparation graph", "preparation_interval_minutes", preparation_job,
            "Runs the LangGraph resume preparation graph (resume, analysis, evidence, tailoring, evidence checks, "
            "review decision, bounded revisions, your approval) over the existing tailor worker, or compares it "
            "read-only with the current path in shadow mode; off unless AGENT_GRAPHS_ENABLED and "
            "PREPARATION_GRAPH_MODE."),
    JobSpec("application_execution", "Application worker", "application_execution_interval_minutes",
            application_execution_job,
            "Runs the LangGraph application worker: reads approved jobs' application forms, finds a verified source "
            "for each answer and, in assist mode, fills the form in a private browser page and validates it, stopping "
            "at ready to submit. Never submits; off unless AGENT_GRAPHS_ENABLED and APPLICATION_EXECUTION_MODE."),
    JobSpec("rag_index", "Retrieval index", "rag_index_interval_minutes", rag_index_job,
            "Embeds verified evidence and saved answers for retrieval, builds a new index beside the old one when the "
            "embedding model changes, and retires the old one once it is complete; off unless RAG_VECTOR_ENABLED."),
    JobSpec("cleanup", "Cleanup", "cleanup_interval_minutes", cleanup,
            "Recovers jobs left locked by crashed workers and applies LLM telemetry retention."),
]
