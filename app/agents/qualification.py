"""The qualification graph (Phase 3): one job from discovery to QUALIFIED or ARCHIVED, through the existing services.

    VALIDATE_JOB → DOMAIN_AND_SEARCH_SCOPE_ADMISSION → CHECK_JD → RECOVER_JD_IF_NEEDED → PARSE_JD
      → QUALIFICATION_HARD_FILTER → RETRIEVE_VERIFIED_JOB_RESUME_EVIDENCE → RANK → VALIDATE → QUALIFY / ARCHIVE

The graph is a conductor, not a second implementation. Each node calls the service that already owns the step:
`domain_match.screen` and `search_scope.screen` for admission, `jd_recovery.enqueue` for recovery,
`job_description.queue_parse` for parsing, `ranker.prepare` for the hard filters, `rag_retrieval.scope` for the job's
evidence scope, and `ranker.queue_job` for ranking. The ranking worker then calls the model, validates the output and
decides with `ranker.qualify`. The recovery, parsing and ranking workers keep doing the HTTP, browser and model work
under their own claims and leases. Their task rows, `WorkflowTransition` history and LLM telemetry are the same rows
the current path writes, so Running workers and AI Orchestration read the same facts.

The graph itself never calls Apify, HTTP, a browser or a model. The one exception is a shadow run whose paid budget
allows a ranking (`allow_paid`, off by default).

Rules a node keeps:
  * Admission runs first, before any recovery, parse, browser or model work, also for old jobs. A job hidden as
    NOT_MATCHING ends the run (HIDDEN) with no task created.
  * A node writes only while its run holds the thread. `effects.within` locks the thread row, records the effect
    and makes the business write in one transaction. A replayed node finds the record and does not queue the work
    a second time; a stopped run raises LeaseLost and writes nothing.
  * A node that queued work waits for it. The run settles INTERRUPTED and its task goes back to PENDING. The task's
    claim skips the job while a recovery, parse or ranking task is active, so nothing polls. When the work ends, the
    worker resumes the run and the node reads the task row again.
  * State is ids, enums and counts only (checked by GuardedSerializer); never a description, resume or prompt.

SHADOW runs walk the same nodes read-only. A node predicts what it would decide from the deterministic functions
(`domain_match.assess`, `search_scope.verdict_for`, `jd_quality`, `ranker.hard_filter_reason`, `ranker.qualify` over
the latest stored ranking) and reads what the current path actually did. The service compares the two
(app.services.qualification.parity). A shadow session is rolled back and refuses to commit.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict
from sqlalchemy import desc, event, func, select
from sqlalchemy.orm import Session

from app.agents import effects

GRAPH = "qualification"
GRAPH_VERSION = "v1"
# The state keys safe to show in a trace (app.agents.tracing_boundary) and to store as the run's result: ids, enums
# and counts only.
EXPORT_FIELDS = (
    "job_id", "task_id", "mode", "step", "outcome", "code", "waiting_for", "jd_branch", "recovery_task_id",
    "recovery_status", "parse_task_id", "parse_status", "ranking_task_id", "ranking_status", "ranking_id", "resume_id",
    "resume_choice", "evidence_count", "evidence_outside_scope", "decision", "paid_calls",
    "predicted_admission", "actual_admission", "predicted_jd", "actual_jd", "predicted_filter", "actual_filter",
    "predicted_decision", "actual_decision", "actual_tasks", "decision_source",
)
ACTIVE = ("PENDING", "PROCESSING")
ACTOR = "qualification"

# Node ids (stable: traces, Running workers and the task's `step` use them).
VALIDATE_JOB = "VALIDATE_JOB"
ADMISSION = "DOMAIN_AND_SEARCH_SCOPE_ADMISSION"
CHECK_JD = "CHECK_JD"
RECOVER_JD = "RECOVER_JD_IF_NEEDED"
PARSE_JD = "PARSE_JD"
HARD_FILTER = "QUALIFICATION_HARD_FILTER"
RETRIEVE = "RETRIEVE_VERIFIED_JOB_RESUME_EVIDENCE"
RANK = "RANK"
VALIDATE = "VALIDATE"
DECIDE = "QUALIFY_ARCHIVE"
NODES = (VALIDATE_JOB, ADMISSION, CHECK_JD, RECOVER_JD, PARSE_JD, HARD_FILTER, RETRIEVE, RANK, VALIDATE, DECIDE)

# How a run ends (JobQualificationTask.outcome).
QUALIFIED, ARCHIVED, HIDDEN, SKIPPED, BLOCKED = "QUALIFIED", "ARCHIVED", "HIDDEN", "SKIPPED", "BLOCKED"
REMOVED, TRIAGED, STOPPED, FAILED, ALREADY_DECIDED = "REMOVED", "TRIAGED", "STOPPED", "FAILED", "ALREADY_DECIDED"
UNAVAILABLE = "UNAVAILABLE"  # a shadow prediction that could not be made without a paid call


class QualificationInput(BaseModel):
    """What the worker starts a run with. Built on the server from the task row; never taken from a caller."""

    model_config = ConfigDict(extra="forbid")
    job_id: int
    task_id: int
    mode: Literal["ON", "SHADOW"] = "ON"
    reason: str = "new"


class QualificationResume(BaseModel):
    """The only value a resume carries: the work the run waited for has ended."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["continue"]


class QualificationState(TypedDict, total=False):
    job_id: int
    task_id: int
    mode: str
    reason: str
    step: str
    outcome: str
    code: str
    waiting_for: str | None
    resume_at: str | None
    jd_branch: str
    recovery_task_id: int | None
    recovery_status: str | None
    parse_task_id: int | None
    parse_status: str | None
    ranking_task_id: int | None
    ranking_status: str | None
    ranking_id: int | None
    resume_id: int | None
    resume_choice: str | None
    evidence_count: int
    evidence_outside_scope: int
    decision: str | None
    decision_source: str | None
    paid_calls: int
    predicted_admission: str
    actual_admission: str
    predicted_jd: str
    actual_jd: str
    predicted_filter: str
    actual_filter: str
    predicted_decision: str | None
    actual_decision: str | None
    actual_tasks: int


@dataclass(frozen=True)
class QualificationContext:
    """Per-run context, never checkpointed: the lease, a session factory and what the worker allows."""

    run: effects.RunHandle
    sessions: Callable[[], Session]
    hold: Callable[[str], None] | None = None  # tests stop a process at named points
    task_id: int | None = None
    allow_paid: bool = False  # SHADOW only: this run may rank a job the current path never ranked (budgeted)


class ShadowWrite(RuntimeError):
    """A shadow run tried to commit. Shadow runs only read."""


def _hold(ctx: QualificationContext, point: str) -> None:
    if ctx.hold is not None:
        ctx.hold(point)


@contextmanager
def _session(ctx: QualificationContext, shadow: bool) -> Iterator[Session]:
    """A session bound to the run's account. A shadow session refuses to commit and is always rolled back."""
    with ctx.sessions() as db:
        db.info["user_id"] = ctx.run.user_id
        if not shadow:
            yield db
            return

        def refuse(session: Session) -> None:
            # SQLAlchemy fires before_commit on a savepoint release too (a read that guards itself with
            # begin_nested, such as retrieval's full-text query). A savepoint saves nothing, and the session is
            # rolled back at the end: only a real commit is a write.
            if session.in_nested_transaction():
                return
            raise ShadowWrite("a shadow qualification run never writes")

        event.listen(db, "before_commit", refuse)
        try:
            yield db
        finally:
            db.rollback()
            event.remove(db, "before_commit", refuse)


def _context(runtime: Runtime[QualificationContext]) -> QualificationContext:
    return runtime.context


def _shadow(state: QualificationState) -> bool:
    return state.get("mode") == "SHADOW"


def _end(step: str, outcome: str, code: str | None = None, **values: Any) -> dict[str, Any]:
    return {"step": step, "outcome": outcome, "code": code or outcome, "waiting_for": None, "resume_at": None,
            **values}


def _wait(step: str, node: str, what: str, **values: Any) -> dict[str, Any]:
    return {"step": step, "waiting_for": what, "resume_at": node, **values}


# ───────────── facts every node re-checks ─────────────

def _job(db: Session, state: QualificationState) -> Any:
    from app.models import Job

    return db.get(Job, state["job_id"])


def _terminal(db: Session, job: Any, step: str) -> dict[str, Any] | None:
    """The run ends here when the job is gone, a person has decided on it, or it is hidden."""
    from app.services import domain_match

    if job is None:
        return _end(step, REMOVED, "JOB_DELETED")
    if job.status in ("dismissed", "approved", "applied") or (job.status == "archived" and job.triaged_at):
        return _end(step, TRIAGED, f"STATUS_{job.status.upper()}")
    if job.domain_match == domain_match.NOT_MATCHING:
        return _end(step, HIDDEN, "NOT_MATCHING")
    return None


def _state_name(job: Any) -> str:
    return getattr(job.state, "value", str(job.state))


# ───────────── VALIDATE_JOB ─────────────

def validate_job(state: QualificationState, runtime: Runtime[QualificationContext]) -> dict[str, Any]:
    """Make sure the job still exists and still needs deciding: a deleted, triaged or already decided job ends the
    run; one a person asked for again is put back first (ranker.restore_removed)."""
    ctx = _context(runtime)
    from app.services import ranker
    from app.services.lifecycle import WorkflowState as S

    with _session(ctx, _shadow(state)) as db:
        job = _job(db, state)
        if _shadow(state):
            if job is None:
                return _end(VALIDATE_JOB, REMOVED, "JOB_DELETED")
            return {"step": VALIDATE_JOB, "paid_calls": 0, **_actuals(db, job)}
        if job is None:
            return _end(VALIDATE_JOB, REMOVED, "JOB_DELETED")
        if job.status in ("dismissed", "approved", "applied") or (job.status == "archived" and job.triaged_at):
            return _end(VALIDATE_JOB, TRIAGED, f"STATUS_{job.status.upper()}")
        if job.state in (S.QUALIFIED, S.ARCHIVED, S.RANKED, S.TAILORING, S.TAILORED):
            return _end(VALIDATE_JOB, ALREADY_DECIDED, _state_name(job), decision=_state_name(job))
        if job.state is S.SKIPPED and state.get("reason") == "user_request" and ranker.removed_by_user(db, job):
            # Taken out of the queue earlier and asked for again (Retry): the ranker puts it back like a new job.
            # Replay-safe: once it is NORMALIZED this branch no longer matches.
            effects.held(db, ctx.run)
            ranker.restore_removed(db, job, actor=ACTOR, reason="Qualification asked for again")
            db.commit()
        elif job.state is S.SKIPPED or job.state is S.FAILED or job.state not in (
                *ranker.PRE_RANK_STATES, S.RANK_PENDING, S.RANKING):
            outcome = STOPPED if job.state is S.SKIPPED and ranker.removed_by_user(db, job) else \
                FAILED if job.state is S.FAILED else SKIPPED
            return _end(VALIDATE_JOB, outcome, f"STATE_{_state_name(job)}")
    return {"step": VALIDATE_JOB, "paid_calls": 0}


def _actuals(db: Session, job: Any) -> dict[str, Any]:
    """What the current path did with this job (shadow runs compare against it)."""
    from app.models import (
        JobDescriptionFetchTask,
        JobDescriptionParseTask,
        JobRankingTask,
        WorkflowTransition,
    )
    from app.services import domain_match

    tasks = sum(db.scalar(select(func.count()).select_from(m).where(m.job_id == job.id)) or 0
                for m in (JobDescriptionFetchTask, JobDescriptionParseTask, JobRankingTask))
    fetched = db.scalar(select(func.count()).select_from(JobDescriptionFetchTask)
                        .where(JobDescriptionFetchTask.job_id == job.id)) or 0
    states = [s.value if hasattr(s, "value") else str(s) for s in db.scalars(
        select(WorkflowTransition.to_state).where(WorkflowTransition.job_id == job.id)
        .order_by(WorkflowTransition.id)).all()]
    decided = db.scalar(select(WorkflowTransition.to_state).where(
        WorkflowTransition.job_id == job.id, WorkflowTransition.from_state == "RANKED",
        WorkflowTransition.to_state.in_(("QUALIFIED", "ARCHIVED"))).order_by(desc(WorkflowTransition.id)).limit(1))
    hidden = job.domain_match == domain_match.NOT_MATCHING
    return {"actual_admission": "NOT_MATCHING" if hidden else (job.domain_match or "MATCH"),
            "actual_jd": "recover" if fetched else "parse",
            "actual_filter": "FILTERED" if "FILTERED" in states else ("SKIPPED" if "SKIPPED" in states else "NONE"),
            "actual_decision": getattr(decided, "value", decided) if decided is not None else None,
            "actual_tasks": int(tasks)}


# ───────────── DOMAIN_AND_SEARCH_SCOPE_ADMISSION ─────────────

def admission(state: QualificationState, runtime: Runtime[QualificationContext]) -> dict[str, Any]:
    """Deterministic admission before anything is spent. A hidden job ends the run with no task created."""
    ctx = _context(runtime)
    from app.services import discovery, domain_classifier, domain_match, search_scope

    with _session(ctx, _shadow(state)) as db:
        job = _job(db, state)
        if _shadow(state):
            if job is None:
                return _end(ADMISSION, REMOVED, "JOB_DELETED")
            if job.source == "manual" or job.domain_match == domain_match.KEPT:
                predicted = domain_match.KEPT
            elif domain_classifier.mode() == domain_classifier.ON and job.domain_match:
                predicted = job.domain_match  # the classifier's verdict is the one on the job: no model in shadow
            else:
                verdict = domain_match.assess(job, domain_match.rules_for(db, job.user_id))
                scoped = search_scope.verdict_for(db, job) if verdict.match else None
                predicted = "NOT_MATCHING" if not verdict.match or (scoped is not None and not scoped.match) \
                    else domain_match.MATCH
            if predicted == "NOT_MATCHING":
                return _end(ADMISSION, HIDDEN, "NOT_MATCHING", predicted_admission=predicted)
            return {"step": ADMISSION, "predicted_admission": predicted}
        done = _terminal(db, job, ADMISSION)
        if done:
            return done
        effects.held(db, ctx.run)
        if job.source == "manual" and job.domain_match is None:
            job.domain_match, job.domain_reason = domain_match.KEPT, "Added by you"
        admitted = domain_match.screen(db, job, None, actor=ACTOR) and \
            search_scope.screen(db, job, job.search_run_id, actor=ACTOR)
        if admitted:
            discovery.choose_resume(db, job)  # never changes a resume the setup or the person pinned
        waiting = job.domain_match == domain_match.PENDING
        review = job.domain_match == domain_match.REVIEW
        checking = waiting and domain_classifier.active_task(db, job.id) is not None
        db.commit()
        if checking:  # the domain classifier reads it (its pass nudges this job); the model call is the worker's
            return _wait(ADMISSION, "admission", "DOMAIN_CLASSIFIER")
        if review:  # the person decides in Discovery → Review; their choice dispatches the job again
            return _end(ADMISSION, HIDDEN, "NEEDS_REVIEW")
        if waiting:  # a person stopped the check: nothing revives it until they ask, and its verdict dispatches again
            return _end(ADMISSION, HIDDEN, "CLASSIFICATION_STOPPED")
        if not admitted:
            return _end(ADMISSION, HIDDEN, "NOT_MATCHING")
    return {"step": ADMISSION}


def _after_admission(state: QualificationState) -> str:
    if state.get("outcome"):
        return END
    return "check_jd"


# ───────────── CHECK_JD ─────────────

def check_jd(state: QualificationState, runtime: Runtime[QualificationContext]) -> dict[str, Any]:
    """Decide whether the posting's description is usable as it is (parse it) or must be recovered from its URL
    first (jd_quality); nothing is fetched here."""
    ctx = _context(runtime)
    from app.services import jd_quality, jd_recovery
    from app.services.lifecycle import WorkflowState as S
    from app.settings import get_settings

    with _session(ctx, _shadow(state)) as db:
        job = _job(db, state)
        if _shadow(state):
            if job is None:
                return _end(CHECK_JD, REMOVED, "JOB_DELETED")
            # The text discovery saw: the board's own when recovery replaced it.
            text = job.original_description if job.description_source == jd_recovery.RECOVERED and \
                job.original_description is not None else job.description
            usable = job.description_source == jd_recovery.USER or jd_quality.quality_of(text) == jd_quality.USABLE
            branch = "parse" if usable or not get_settings().jd_fetch_enabled else "recover"
            return {"step": CHECK_JD, "jd_branch": branch, "predicted_jd": branch}
        done = _terminal(db, job, CHECK_JD)
        if done:
            return done
        if job.state in (S.RANK_PENDING, S.RANKING):
            return {"step": CHECK_JD, "jd_branch": "ranking"}  # queued for ranking by another path: wait for it
        effects.held(db, ctx.run)
        quality = jd_recovery.assess(job)
        db.commit()
    usable = quality == jd_quality.USABLE or not get_settings().jd_fetch_enabled
    return {"step": CHECK_JD, "jd_branch": "parse" if usable else "recover"}


def _after_check(state: QualificationState) -> str:
    if state.get("outcome"):
        return END
    return {"recover": "recover_jd", "ranking": "retrieve"}.get(state.get("jd_branch") or "", "parse_jd")


# ───────────── RECOVER_JD_IF_NEEDED ─────────────

def recover_jd(state: QualificationState, runtime: Runtime[QualificationContext]) -> dict[str, Any]:
    """Queue one description recovery task (jd_recovery.enqueue), once per job whatever replays, and wait for the
    recovery worker."""
    ctx = _context(runtime)
    from app.models import JobDescriptionFetchTask
    from app.services import jd_recovery

    with _session(ctx, _shadow(state)) as db:
        job = _job(db, state)
        if _shadow(state):
            latest = jd_recovery.latest_task(db, job.id) if job is not None else None
            return {"step": RECOVER_JD, "recovery_task_id": latest.id if latest else None,
                    "recovery_status": latest.status if latest else None}
        done = _terminal(db, job, RECOVER_JD)
        if done:
            return done
        record, created = effects.within(db, ctx.run, f"recovery.enqueue:{job.id}")
        if created:
            task = jd_recovery.enqueue(db, job, reason="auto")
            record.summary = {"task": task.id if task is not None else None}
        db.commit()
        task_id = (record.summary or {}).get("task")
        task = db.get(JobDescriptionFetchTask, task_id) if task_id else None
        status, code = (task.status, task.result_code) if task is not None else (None, None)
    _hold(ctx, "recover:after_enqueue")
    if status in ACTIVE:
        return _wait(RECOVER_JD, "recover_jd", "JD_RECOVERY", recovery_task_id=task_id, recovery_status=status)
    # Ended (or never queued: no URL, or this URL was tried before): the job goes on with the text it has, as on the
    # current path; a missing description makes the hard filter skip it.
    return {"step": RECOVER_JD, "recovery_task_id": task_id, "recovery_status": status, "code": code,
            "waiting_for": None, "resume_at": None}


# ───────────── PARSE_JD ─────────────

def parse_jd(state: QualificationState, runtime: Runtime[QualificationContext]) -> dict[str, Any]:
    """Queue the structured parse of the description (job_description.queue_parse), once per description, and wait
    for the parser worker."""
    ctx = _context(runtime)
    from app.models import JobDescriptionParseTask
    from app.services import job_description, ranker

    with _session(ctx, _shadow(state)) as db:
        job = _job(db, state)
        if _shadow(state):
            latest = db.scalar(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job.id)
                               .order_by(desc(JobDescriptionParseTask.id)).limit(1)) if job is not None else None
            return {"step": PARSE_JD, "parse_task_id": latest.id if latest else None,
                    "parse_status": latest.status if latest else None}
        done = _terminal(db, job, PARSE_JD)  # recovery's own scope check may have hidden it
        if done:
            return done
        if ranker.missing_description(job):
            return {"step": PARSE_JD, "parse_status": "NOT_NEEDED", "waiting_for": None, "resume_at": None}
        waiting = job_description._parse_wait(db, job)
        task = None
        if waiting == job_description.PARSE_NOT_QUEUED:
            digest = job_description.description_hash(job.description or "")[:16]
            record, created = effects.within(db, ctx.run, f"parse.enqueue:{job.id}:{digest}")
            if created:
                queued = job_description.queue_parse(db, job, reason="new")
                db.flush()
                record.summary = {"task": queued.id if queued is not None else None}
            db.commit()
            task_id = (record.summary or {}).get("task")
            task = db.get(JobDescriptionParseTask, task_id) if task_id else None
        elif waiting is not None:
            task = db.scalar(select(JobDescriptionParseTask).where(
                JobDescriptionParseTask.job_id == job.id, JobDescriptionParseTask.status.in_(ACTIVE)).limit(1))
        else:
            task = db.scalar(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job.id)
                             .order_by(desc(JobDescriptionParseTask.id)).limit(1))
        task_id, status = (task.id, task.status) if task is not None else (None, None)
    _hold(ctx, "parse:after_enqueue")
    if status in ACTIVE:
        return _wait(PARSE_JD, "parse_jd", "JD_PARSE", parse_task_id=task_id, parse_status=status)
    # A failed parse (attempts used up) continues: the ranker reads the raw text, as on the current path.
    return {"step": PARSE_JD, "parse_task_id": task_id, "parse_status": status, "waiting_for": None,
            "resume_at": None}


# ───────────── QUALIFICATION_HARD_FILTER ─────────────

def hard_filter(state: QualificationState, runtime: Runtime[QualificationContext]) -> dict[str, Any]:
    """Normalize and apply the account's hard filters through the ranker's own rules (ranker.prepare): a job that
    fails one is skipped with its reason, before any model."""
    ctx = _context(runtime)
    from app.services import ranker
    from app.services.lifecycle import WorkflowState as S

    with _session(ctx, _shadow(state)) as db:
        job = _job(db, state)
        if _shadow(state):
            policy = ranker.policy_for(db, job.user_id)
            problem = ranker.normalization_problem(job) or (ranker.NO_DESCRIPTION if ranker.missing_description(job)
                                                            else ranker.hard_filter_reason(job, policy))
            if problem:
                return _end(HARD_FILTER, SKIPPED, "HARD_FILTER", predicted_filter="SKIPPED")
            return {"step": HARD_FILTER, "predicted_filter": "FILTERED"}
        done = _terminal(db, job, HARD_FILTER)
        if done:
            return done
        effects.held(db, ctx.run)
        reason = ranker.prepare(db, job, ranker.policy_for(db, job.user_id), actor=ACTOR) \
            if job.state in ranker.PRE_RANK_STATES else None
        db.commit()
        skipped = job.state is S.SKIPPED
    if not skipped:
        return {"step": HARD_FILTER}
    if reason == ranker.NO_DESCRIPTION:
        blocked = state.get("recovery_status") in ("BLOCKED", "FAILED")
        return _end(HARD_FILTER, BLOCKED if blocked else SKIPPED, "NO_DESCRIPTION")
    return _end(HARD_FILTER, SKIPPED, "NORMALIZATION" if reason and reason.startswith("Missing") else "HARD_FILTER")


# ───────────── RETRIEVE_VERIFIED_JOB_RESUME_EVIDENCE ─────────────

def _scope(db: Session, job: Any) -> tuple[Any, set[int]] | None:
    from app.services import rag_retrieval, ranker

    profile = ranker._profile(db, job.user_id)
    if profile is None:
        return None
    return rag_retrieval.scope(db, profile, job)


def retrieve(state: QualificationState, runtime: Runtime[QualificationContext]) -> dict[str, Any]:
    """The job's resume (its pin, else the active one) and the verified evidence it may draw on. Ids only.

    Read-only: the ranker builds its own context from the same resume, so the graph never sends a second copy."""
    ctx = _context(runtime)
    with _session(ctx, True) as db:  # read-only in both modes: nothing here is ever saved
        job = _job(db, state)
        if job is None:
            return _end(RETRIEVE, REMOVED, "JOB_DELETED")
        try:
            scoped = _scope(db, job)
        except Exception:  # noqa: BLE001  the ranker reports a missing profile or resume itself
            scoped = None
        resume, ids = scoped if scoped is not None else (None, set())
        return {"step": RETRIEVE, "resume_id": getattr(resume, "id", None), "resume_choice": job.resume_choice,
                "evidence_count": len(ids)}


# ───────────── RANK ─────────────

def rank(state: QualificationState, runtime: Runtime[QualificationContext]) -> dict[str, Any]:
    """Queue the job for the Ranker (ranker.queue_job), once, and wait for the ranking worker; the ranker, not the
    graph, decides QUALIFIED or ARCHIVED."""
    ctx = _context(runtime)
    from app.models import JobRanking, JobRankingTask
    from app.services import ranker
    from app.services.lifecycle import WorkflowState as S

    if _shadow(state):
        return _shadow_rank(state, ctx)
    with _session(ctx, False) as db:
        job = _job(db, state)
        done = _terminal(db, job, RANK)
        if done:
            return done
        record, created = effects.within(db, ctx.run, f"rank.enqueue:{job.id}")
        if created:
            why = None
            if job.state in ranker.PRE_RANK_STATES or job.state is S.RANK_PENDING:
                why = ranker.queue_job(db, job, policy=ranker.policy_for(db, job.user_id), actor=ACTOR)
            active = db.scalar(select(JobRankingTask).where(JobRankingTask.job_id == job.id,
                                                            JobRankingTask.status.in_(ACTIVE)).limit(1))
            record.summary = {"task": active.id if active is not None else None, "queued": why is None}
        db.commit()
        task_id = (record.summary or {}).get("task")
        task = db.get(JobRankingTask, task_id) if task_id else None
        if task is None:
            return _end(RANK, SKIPPED, "RANKING_NOT_QUEUED")
        status, ranking_id = task.status, task.ranking_id
        if ranking_id and db.get(JobRanking, ranking_id) is None:
            ranking_id = None
    _hold(ctx, "rank:after_enqueue")
    if status in ACTIVE:
        return _wait(RANK, "rank", "RANKING", ranking_task_id=task_id, ranking_status=status)
    return {"step": RANK, "ranking_task_id": task_id, "ranking_status": status, "ranking_id": ranking_id,
            "waiting_for": None, "resume_at": None}


def _shadow_rank(state: QualificationState, ctx: QualificationContext) -> dict[str, Any]:
    """The decision `ranker.qualify` makes over the job's latest stored ranking, under today's policy. A job the
    current path never ranked is ranked again only when the run's paid budget allows it (never saved)."""
    from app.models import JobRanking
    from app.services import ranker

    with _session(ctx, True) as db:
        job = _job(db, state)
        if job is None:
            return _end(RANK, REMOVED, "JOB_DELETED")
        policy = ranker.policy_for(db, job.user_id)
        ranking = db.scalar(select(JobRanking).where(JobRanking.job_id == job.id)
                            .order_by(desc(JobRanking.version)).limit(1))
        if ranking is not None:
            decision = ranker.qualify(ranking, policy).state.value
            return {"step": RANK, "ranking_id": ranking.id, "predicted_decision": decision,
                    "decision_source": "stored_ranking"}
        if not ctx.allow_paid:
            return {"step": RANK, "predicted_decision": UNAVAILABLE, "decision_source": "no_ranking"}
        try:
            output = _rank_with_model(db, job)
        except Exception:  # noqa: BLE001  a failed shadow call changes nothing; it is reported as unavailable
            return {"step": RANK, "predicted_decision": UNAVAILABLE, "decision_source": "model_failed",
                    "paid_calls": state.get("paid_calls", 0) + 1}
        return {"step": RANK, "predicted_decision": ranker.qualify(output, policy).state.value,
                "decision_source": "shadow_model", "paid_calls": state.get("paid_calls", 0) + 1}


def _rank_with_model(db: Session, job: Any) -> Any:
    """One ranking request through the gateway, exactly as the ranker builds it, with nothing saved."""
    from app.services import ranker
    from app.services import resume_verification as rv
    from app.services.llm_telemetry import LLMCall
    from app.settings import get_settings

    profile = rv.for_job(db, ranker._profile(db, job.user_id), job)
    if profile is None:
        raise LookupError("no candidate profile")
    context = ranker.build_ranking_context(db, profile, job, get_settings().ranking_max_evidence)
    prompt = ranker.fit_prompt(context, job, lambda: ranker.build_ranking_prompt(candidate_profile=profile, job=job,
                                                                                  context=context),
                               ranker.prompt_budget.budget_for(db, job.user_id, "RANKER"))
    verified, job_id = set(context.evidence_ids), job.id
    result = ranker.LLMClient.for_user(db, job.user_id).generate_prompt(prompt, LLMCall(
        workflow_type="agent", entity_id=job_id, validate=lambda data: ranker.validate_output(data, job_id, verified),
        context_stats=context.call_stats()))
    output, _ = result.validated
    return output


# ───────────── VALIDATE ─────────────

def validate(state: QualificationState, runtime: Runtime[QualificationContext]) -> dict[str, Any]:
    """Check what the ranker stored. Its result is never rewritten here: a deviation is recorded, the decision stays
    the ranker's (`ranker.qualify` through `work_queue.transition`)."""
    ctx = _context(runtime)
    from app.models import JobRanking, JobRankingTask, WorkflowTransition
    from app.services import ranker
    from app.services.lifecycle import STOPPED_BY_USER

    if _shadow(state):
        predicted, actual = state.get("predicted_decision"), state.get("actual_decision")
        return {"step": VALIDATE, "code": "PARITY" if predicted == actual else "DIFFERS"}
    with _session(ctx, True) as db:
        job = _job(db, state)
        if job is None:
            return _end(VALIDATE, REMOVED, "JOB_DELETED")
        task = db.get(JobRankingTask, state.get("ranking_task_id")) if state.get("ranking_task_id") else None
        if task is None or task.status == "FAILED":
            return _end(VALIDATE, FAILED, "RANKING_FAILED")
        if task.status == "SKIPPED":
            stopped = task.last_error in (STOPPED_BY_USER, ranker.REMOVED_BY_USER)
            return _end(VALIDATE, STOPPED if stopped else SKIPPED, "RANKING_SKIPPED")
        ranking = db.get(JobRanking, task.ranking_id) if task.ranking_id else None
        if ranking is None:
            return _end(VALIDATE, FAILED, "RANKING_MISSING")
        expected = ranker.qualify(ranking, ranker.policy_for(db, job.user_id)).state.value
        made = db.scalar(select(WorkflowTransition.to_state).where(
            WorkflowTransition.job_id == job.id, WorkflowTransition.from_state == "RANKED",
            WorkflowTransition.to_state.in_(("QUALIFIED", "ARCHIVED"))).order_by(desc(WorkflowTransition.id)).limit(1))
        made = getattr(made, "value", made)
        try:
            scoped = _scope(db, job)
        except Exception:  # noqa: BLE001
            scoped = None
        cited = set(ranking.relevant_evidence_ids or [])
        outside = len(cited - scoped[1]) if scoped is not None else 0
        return {"step": VALIDATE, "ranking_id": ranking.id, "decision": made or expected,
                "code": "VALIDATED" if made == expected else "DECISION_DIFFERS",
                "evidence_outside_scope": outside}


# ───────────── QUALIFY / ARCHIVE ─────────────

def decide(state: QualificationState, runtime: Runtime[QualificationContext]) -> dict[str, Any]:
    """Record the outcome the ranker's transition made. The graph never moves the job here itself."""
    if _shadow(state):
        predicted = state.get("predicted_decision") or UNAVAILABLE
        return _end(DECIDE, predicted if predicted in (QUALIFIED, ARCHIVED) else UNAVAILABLE, state.get("code"))
    decision = state.get("decision")
    return _end(DECIDE, decision if decision in (QUALIFIED, ARCHIVED) else FAILED, state.get("code"))


# ───────────── waiting ─────────────

def wait(state: QualificationState) -> dict[str, Any]:
    """Settle the run until the work it waits for ends. The worker resumes it then; the node it came from reads the
    task row again."""
    answer = interrupt({"wait": state.get("waiting_for"), "step": state.get("step"), "job_id": state.get("job_id")})
    QualificationResume.model_validate(answer if isinstance(answer, dict) else {"action": answer})
    return {"waiting_for": None}


def _next(after: str) -> Callable[[QualificationState], str]:
    def route(state: QualificationState) -> str:
        if state.get("outcome"):
            return END
        if state.get("waiting_for"):
            return "wait"
        return after
    return route


def _after_wait(state: QualificationState) -> str:
    return state.get("resume_at") or "validate_job"


def _after_validate_job(state: QualificationState) -> str:
    return END if state.get("outcome") else "admission"


def build(checkpointer: Any) -> Any:
    graph = StateGraph(QualificationState, context_schema=QualificationContext)
    graph.add_node("validate_job", validate_job)
    graph.add_node("admission", admission)
    graph.add_node("check_jd", check_jd)
    graph.add_node("recover_jd", recover_jd)
    graph.add_node("parse_jd", parse_jd)
    graph.add_node("hard_filter", hard_filter)
    graph.add_node("retrieve", retrieve)
    graph.add_node("rank", rank)
    graph.add_node("validate", validate)
    graph.add_node("decide", decide)
    graph.add_node("wait", wait)
    graph.add_edge(START, "validate_job")
    graph.add_conditional_edges("validate_job", _after_validate_job, ["admission", END])
    graph.add_conditional_edges("admission", _after_admission, ["check_jd", END])
    graph.add_conditional_edges("check_jd", _after_check, ["recover_jd", "parse_jd", "retrieve", END])
    graph.add_conditional_edges("recover_jd", _next("parse_jd"), ["parse_jd", "wait", END])
    graph.add_conditional_edges("parse_jd", _next("hard_filter"), ["hard_filter", "wait", END])
    graph.add_conditional_edges("hard_filter", _next("retrieve"), ["retrieve", END])
    graph.add_conditional_edges("retrieve", _next("rank"), ["rank", END])
    graph.add_conditional_edges("rank", _next("validate"), ["validate", "wait", END])
    graph.add_conditional_edges("validate", _next("decide"), ["decide", END])
    graph.add_edge("decide", END)
    graph.add_conditional_edges("wait", _after_wait, ["recover_jd", "parse_jd", "rank"])
    return graph.compile(checkpointer=checkpointer, name=GRAPH)


def summary(state: dict[str, Any]) -> dict[str, Any]:
    """The finished run's result as stored on the thread: safe fields only."""
    return {k: state[k] for k in EXPORT_FIELDS if k in state and state[k] is not None}
