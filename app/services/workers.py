"""Running workers: every queued background task in one place, with the controls a person needs when a queue sticks.

    JD parsing   job_description_parse_tasks    scheduler job jd_parsing
    JD recovery  job_description_fetch_tasks    scheduler job jd_recovery
    Ranking      job_ranking_tasks              scheduler job ranking
    Job insights job_insight_tasks              scheduler job job_insights
    Tailoring    tailor_tasks (tailor → review)  the in-process tailor worker
    Reviewing    tailor_tasks at the review step  a view of Tailoring: the same rows, settings and controls
    Indexing     rag_index_entries              scheduler job rag_index (no job: an Evidence or saved-answer row)
    Resume parsing resume_parse_tasks           scheduler job resume_parse (no job: a resume version)

Most queues hold one task per job. The retrieval indexer's entries belong to an account and a source row instead, so a
kind may describe tasks without a job (`WorkerKind.subject`): how a row is labelled, what a search matches and how a
finished task is queued again (`requeue`, the same row back to waiting). Its rows carry `job_id` None.

Two controls, both acting on the signed-in account's own tasks only:

* **Kill** stops a task's current attempt and requeues it fresh: attempts back to zero, retry wait cleared. A running
  thread cannot be interrupted, so killing takes its lease away instead: the old run's writes are guarded by that
  lease (ranker._owned, job_description._held, jd_recovery._held, tailor_tasks._write) and become no-ops, and the
  tailoring run stops at its next checkpoint, before saving anything. Killing every task of a kind also clears the
  account's provider rate-limit cooldowns (app.services.backoff.reset).
* **Run now** marks a task (`run_now_at`): the next claim takes it first, ignores its retry wait and the account's
  ranking cadence, and its calls skip the provider cooldown for that one run (app.services.backoff.bypass). A 429 is
  still recorded, so the task simply backs off again if the provider is still refusing.

Nothing here calls a model or a provider. Error text is redacted before it is returned.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    ApplicationExecutionTask,
    AppSetting,
    Job,
    JobClassificationTask,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobInsightTask,
    JobPreparationTask,
    JobQualificationTask,
    JobRankingTask,
    RagIndexEntry,
    RagIndexVersion,
    Resume,
    ResumeParseTask,
    TailorTask,
)
from app.services import backoff, redaction, server_settings, worker_settings
from app.services.lifecycle import STOPPED_BY_USER, WorkflowState
from app.settings import get_settings

RECENT = timedelta(hours=24)
ROW_LIMIT = 200
ERROR_LIMIT = 300
KILLED_NOTE = "Stopped by you and requeued"  # older rows; Kill now stops (lifecycle.STOPPED_BY_USER)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class WorkerError(Exception):
    """A control that cannot be applied, with the reason a person should see."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class WorkerKind:
    """One queue of background tasks, described the same way for every worker so the screen stays generic."""

    kind: str
    label: str
    description: str
    model: Any
    scheduler_job: str | None  # None: the tailor worker polls on its own
    pending: str  # waiting status
    running: str
    finished: tuple[str, ...]
    max_attempts: Callable[[], int]
    node_id: str  # the AI Orchestration stage it feeds
    # A view of another worker's queue (Reviewing is the review step of Tailoring): the rows `scope` selects, that
    # worker's settings, and left out of totals and Kill all so no task is counted or stopped twice.
    scope: Callable[[Any], Any] | None = None
    view_of: str | None = None
    # Tasks that belong to no job: `subject(db, user_id, tasks)` labels them ({task id: {"title", "company", ...}}),
    # `search(model, text)` is what the search box matches, `requeue(db, task, user_id)` puts a failed or stopped
    # task back in the queue (the same row) for Retry and Run now.
    subject: Callable[[Session, int, list[Any]], dict[int, dict[str, Any]]] | None = None
    search: Callable[[Any, str], Any] | None = None
    requeue: Callable[[Session, Any, int], Any] | None = None
    shown: tuple[tuple[str, str], ...] = ()  # a raw finished status → the word every worker uses for it
    stopped_status: str = "SKIPPED"  # what Kill leaves (with lifecycle.STOPPED_BY_USER as the error)
    # Where its model calls are recorded: the llm_request_logs workflow_type, and the row key holding the entity id.
    llm_link: tuple[str, str] = ("job", "job_id")
    # A conductor (a graph that never calls a model itself, so its orchestration node names no workers): the workers it
    # hands its steps to, and `step_worker(db, user_id, rows)`, which says whose step each active row is at ({task id:
    # worker type}). Its rows show that worker's model, and the requests those workers made for the job.
    conducts: tuple[str, ...] = ()
    step_worker: Callable[[Session, int, list[dict[str, Any]]], dict[int, str]] | None = None

    @property
    def active(self) -> tuple[str, str]:
        return (self.pending, self.running)

    @property
    def tailor(self) -> bool:
        return self.model is TailorTask

    @property
    def job_bound(self) -> bool:
        return self.subject is None

    @property
    def settings_kind(self) -> str:
        return self.view_of or self.kind

    @property
    def attempts(self) -> Any:
        return self.model.attempts if self.tailor else self.model.attempt_count

    def where(self, query: Any) -> Any:
        return query.where(self.scope(self.model)) if self.scope is not None else query


def _tailor_max() -> int:
    from app.services.tailor_tasks import MAX_ATTEMPTS

    return MAX_ATTEMPTS


def _reviewing(m: Any) -> Any:
    """The tailor tasks at the review step: reviewing now, a review-only run waiting (skip tailoring, or a review of a
    saved packet), or finished while reviewing (tailor_tasks keeps a finished run's last stage)."""
    return or_(and_(m.status == "running", m.stage == "reviewing"),
               and_(m.status == "queued", m.mode.in_(("skip", "review"))),
               and_(m.status.in_(("succeeded", "failed", "cancelled")), m.stage == "reviewing"))


def _qualification_scope(m: Any) -> Any:
    """Shadow runs only compare (Monitoring); Running workers lists the runs that drive jobs."""
    return m.mode == "ON"


def _qualification_step_workers(db: Session, user_id: int, rows_: list[dict[str, Any]]) -> dict[int, str]:
    from app.services import qualification

    return qualification.step_workers(db, user_id, rows_)


def _preparation_step_workers(db: Session, user_id: int, rows_: list[dict[str, Any]]) -> dict[int, str]:
    from app.services import preparation

    return preparation.step_workers(db, user_id, rows_)


def _index_scope(m: Any) -> Any:
    """Entries of a retired index are left out: they are never worked again and are purged with it."""
    return m.index_version_id.in_(select(RagIndexVersion.id).where(RagIndexVersion.status != "RETIRED"))


def _index_subjects(db: Session, user_id: int, tasks: list[Any]) -> dict[int, dict[str, Any]]:
    """"Evidence #12 · index v3", and the embedding model of that index version. Metadata only: never the fact's or
    the answer's text."""
    ids = {t.index_version_id for t in tasks}
    versions = {v.id: v for v in db.scalars(select(RagIndexVersion).where(
        RagIndexVersion.user_id == user_id, RagIndexVersion.id.in_(ids))).all()} if ids else {}
    out = {}
    for t in tasks:
        v = versions.get(t.index_version_id)
        route = (v.provider or "").upper() if v else ""
        out[t.id] = {
            "title": f"{'Evidence' if t.source_type == 'evidence' else 'Saved answer'} #{t.source_id}",
            "company": f"index v{t.index_version_id}" + (f" ({v.status.lower()})" if v else ""),
            "subject": {"type": t.source_type, "id": t.source_id, "index_version": t.index_version_id,
                        "collection": t.collection, "sensitive": bool(t.sensitive)},
            # The model is the index version's own (an index never mixes models), never a guess.
            "model": {"model": v.model, "route": route, "route_label": _route_label(route) if route else None,
                      "via": None, "source": "used" if t.embedded_at else "next", "backups": []} if v else None,
            "model_change": None}
    return out


def _index_search(m: Any, text: str) -> Any:
    """`#12` is the source row's id; words match its kind (evidence, answer) or collection."""
    if text.lstrip("#").isdigit():
        return m.source_id == int(text.lstrip("#"))
    like = f"%{text.lower()[:100]}%"
    return or_(func.lower(m.source_type).like(like), func.lower(m.collection).like(like))


def _index_requeue(db: Session, task: Any, user_id: int) -> Any:
    """A failed or stopped entry goes back to waiting, fresh. The indexer reads the live row when it runs it."""
    if task.status != "FAILED":
        raise WorkerError("Only a failed or stopped entry can be indexed again")
    task.status, task.attempt_count, task.next_attempt_at = "PENDING", 0, None
    task.last_error = task.completed_at = task.lock_token = task.lock_expires_at = None
    db.flush()
    return task


def _resume_subjects(db: Session, user_id: int, tasks: list[Any]) -> dict[int, dict[str, Any]]:
    """"Resume name · v3": the version's own label. Never its text."""
    ids = {t.resume_id for t in tasks}
    resumes = {r.id: r for r in db.scalars(select(Resume).where(Resume.user_id == user_id, Resume.id.in_(ids))).all()} \
        if ids else {}
    out = {}
    for t in tasks:
        r = resumes.get(t.resume_id)
        out[t.id] = {"title": (r.name or "Resume") if r else "Deleted resume",
                     "company": f"v{r.version}" if r else "", "step": t.step, "outcome": t.outcome,
                     "subject": {"type": "resume", "id": t.resume_id}, "resume_id": t.resume_id}
    return out


def _resume_search(m: Any, text: str) -> Any:
    """`#12` is the resume's id; words match the resume's name."""
    if text.lstrip("#").isdigit():
        return m.resume_id == int(text.lstrip("#"))
    like = f"%{text.lower()[:100]}%"
    return m.resume_id.in_(select(Resume.id).where(func.lower(Resume.name).like(like)))


def _resume_requeue(db: Session, task: Any, user_id: int) -> Any:
    from app.services import resume_parsing

    return resume_parsing.requeue(db, task, user_id)


KINDS: dict[str, WorkerKind] = {k.kind: k for k in (
    WorkerKind("parsing", "JD parsing", "Parses each posting into structured requirements (JD parser model).",
               JobDescriptionParseTask, "jd_parsing", "PENDING", "PROCESSING", ("COMPLETED", "FAILED", "SKIPPED"),
               lambda: get_settings().jd_parse_max_attempts, "jd_parser", stopped_status="FAILED"),
    WorkerKind("classification", "Domain check", "Reads each job the rules could not settle with a fast model, before "
               "anything expensive is spent on it. A job is hidden only when code confirms the model's evidence; a "
               "failed check never hides one.", JobClassificationTask, "domain_classifier", "PENDING", "PROCESSING",
               ("COMPLETED", "FAILED", "SKIPPED"), lambda: get_settings().domain_classifier_max_attempts,
               "domain_classifier"),
    WorkerKind("recovery", "JD recovery", "Reads a description from the posting URL when the board sent none: HTTP, "
               "then the browser, a model only on ambiguous pages.", JobDescriptionFetchTask, "jd_recovery",
               "PENDING", "PROCESSING", ("COMPLETED", "FAILED", "BLOCKED", "SKIPPED"),
               lambda: get_settings().jd_fetch_max_attempts, "jd_recovery"),
    WorkerKind("ranking", "Ranking", "Scores each job's fit; code decides qualified or archived.", JobRankingTask,
               "ranking", "PENDING", "PROCESSING", ("COMPLETED", "FAILED", "SKIPPED"),
               lambda: get_settings().ranking_max_attempts, "ranker"),
    WorkerKind("qualification", "Qualification", "Takes each new job through admission, description recovery, "
               "parsing, hard filters, evidence and ranking, in that order (LangGraph). It never calls a model itself: "
               "it hands each step to its worker and waits.", JobQualificationTask, "qualification", "PENDING",
               "PROCESSING", ("COMPLETED", "FAILED", "SKIPPED"), lambda: get_settings().qualification_max_attempts,
               "qualification", scope=_qualification_scope, conducts=("JD_RECOVERY", "JD_PARSER", "RANKER"),
               step_worker=_qualification_step_workers),
    WorkerKind("preparation", "Preparation", "Takes each qualified job through resume selection, the Ranker's "
               "analysis, evidence, tailoring, the evidence checks, the review decision and a bounded revision loop, "
               "then waits for your approval (LangGraph). It never calls a model itself: tailoring and review run in "
               "the Tailoring worker.", JobPreparationTask, "preparation", "PENDING", "PROCESSING",
               ("COMPLETED", "FAILED", "SKIPPED"), lambda: get_settings().preparation_max_attempts, "preparation",
               scope=_qualification_scope, conducts=("TAILOR", "REVIEWER"), step_worker=_preparation_step_workers),
    WorkerKind("application", "Application", "Reads an approved job's application form, finds a verified source for "
               "every answer and (in assist mode) fills it in a private browser page, then stops at ready to submit "
               "for you (LangGraph). It never submits: you do, from the Apply kit. A login, code or CAPTCHA waits for "
               "you.", ApplicationExecutionTask, "application_execution", "PENDING", "PROCESSING",
               ("COMPLETED", "FAILED", "SKIPPED"), lambda: get_settings().application_execution_max_attempts,
               "application_execution"),
    WorkerKind("insights", "Job insights", "Researches approved jobs (posting, company site, Glassdoor and PayScale "
               "when configured), then a model organises what was found.", JobInsightTask, "job_insights", "PENDING",
               "PROCESSING", ("COMPLETED", "PARTIAL", "FAILED", "SKIPPED"),
               lambda: get_settings().job_insights_max_attempts, "company_insights"),
    WorkerKind("tailoring", "Tailoring", "Tailors the resume and cover letter, then the reviewer checks the packet.",
               TailorTask, None, "queued", "running", ("succeeded", "failed", "cancelled"), _tailor_max, "tailor"),
    WorkerKind("reviewing", "Reviewing", "The reviewer checks each tailored packet and recommends PASS, RETAILOR or "
               "SKIP; code decides. It runs as the last step of a Tailoring task: Kill and Run now act on that task, "
               "and a retry tailors the job again.", TailorTask, None, "queued", "running",
               ("succeeded", "failed", "cancelled"), _tailor_max, "reviewer", scope=_reviewing, view_of="tailoring"),
    WorkerKind("indexing", "Indexing", "Embeds your verified evidence and saved answers for retrieval (the Embedder "
               "model, through LiteLLM). It never generates text, and an entry you stop stays out of the index until "
               "you retry it.", RagIndexEntry, "rag_index", "PENDING", "PROCESSING",
               ("ACTIVE", "FAILED", "DEACTIVATED"), lambda: get_settings().rag_index_max_attempts, "retrieval_index",
               scope=_index_scope, subject=_index_subjects, search=_index_search, requeue=_index_requeue,
               shown=(("ACTIVE", "COMPLETED"), ("DEACTIVATED", "SKIPPED")), stopped_status="FAILED"),
    WorkerKind("resume_parsing", "Resume parsing", "Structures each uploaded resume with the Resume Parser model "
               "(LangGraph), keeps only what the resume's own text states, and fills the structured data while you "
               "have not reviewed it (a comparison otherwise).", ResumeParseTask, "resume_parse", "PENDING",
               "PROCESSING", ("COMPLETED", "FAILED"), lambda: get_settings().resume_parse_max_attempts,
               "resume_parser", subject=_resume_subjects, search=_resume_search, requeue=_resume_requeue,
               stopped_status="FAILED", llm_link=("profile", "resume_id")),
)}


def kind_of(name: str) -> WorkerKind:
    spec = KINDS.get(name)
    if spec is None:
        raise WorkerError(f"Unknown worker '{name}'. Workers: {', '.join(KINDS)}", 404)
    return spec


def _lock(db: Session, query):
    return query.with_for_update() if db.get_bind().dialect.name == "postgresql" else query


# ───────────── reading ─────────────

def _display_status(spec: WorkerKind, task: Any, now: datetime) -> str:
    """RUNNING, QUEUED, RETRYING or the finished status, the same words for every worker."""
    status = task.status
    if status == spec.running:
        return "RUNNING"
    if status not in spec.active and (task.error if spec.tailor else task.last_error) == STOPPED_BY_USER:
        return "STOPPED"
    if status == spec.pending:
        waiting = getattr(task, "next_attempt_at", None)
        if (waiting is not None and waiting > now) or ((_attempts(spec, task) or 0) > 0 and not spec.tailor):
            return "RETRYING"
        return "QUEUED"
    if spec.kind == "application" and status == "COMPLETED" and getattr(task, "outcome", None) in APPLICATION_BLOCKED:
        return "BLOCKED"  # the run ended without reaching the form (a backend down, a route it cannot open): not done
    return dict(spec.shown).get(status, status).upper()


APPLICATION_BLOCKED = ("BLOCKED", "UNSUPPORTED")  # app.agents.application outcomes a run ends at without a result


def _started_graph_run(spec: WorkerKind, task: Any) -> bool:
    return spec.kind in ("qualification", "preparation", "application") \
        and getattr(task, "agent_thread_id", None) is not None


def _attempts(spec: WorkerKind, task: Any) -> int:
    return (task.attempts if spec.tailor else task.attempt_count) or 0


def row(spec: WorkerKind, task: Any, label: dict[str, Any] | None, now: datetime,
        max_attempts: int | None = None) -> dict[str, Any]:
    """One task as Running workers shows it. Metadata only. `label` is its job's title and company (or, for a task
    without a job, what `subject` says); `max_attempts` is the account's own limit."""
    tailoring = spec.tailor
    status = _display_status(spec, task, now)
    error = task.error if tailoring else task.last_error
    code = None if tailoring else getattr(task, "error_category", None)
    category = getattr(code, "value", code) if code else None
    label = label or {}
    finished_at = task.finished_at if tailoring else getattr(task, "completed_at", None)
    waiting = None if tailoring else task.next_attempt_at
    active = task.status in spec.active
    return {"task_id": task.id, "job_id": getattr(task, "job_id", None), "title": label.get("title"),
            "company": label.get("company"), "kind": spec.kind, "status": status, "raw_status": task.status,
            "stage": (task.stage if active else "done") if tailoring else ("running" if status == "RUNNING" else status.lower()),
            "mode": task.mode if tailoring else None, "reason": getattr(task, "reason", None),
            "attempt": _attempts(spec, task), "max_attempts": max_attempts or spec.max_attempts(),
            "created_at": _iso(task.created_at), "started_at": _iso(task.started_at), "finished_at": _iso(finished_at),
            "next_attempt_at": _iso(waiting), "lease_expires_at": _iso(task.lock_expires_at),
            "last_error": redaction.redact_text(error, ERROR_LIMIT) if error and status != "COMPLETED" else None,
            "error_category": category, "run_now": task.run_now_at is not None,
            "result_code": getattr(task, "result_code", None),
            # A graph run between steps has started (it waits for recovery, a parse or a ranking): it can be stopped.
            "can_kill": status in ("RUNNING", "RETRYING") or (active and _started_graph_run(spec, task)),
            "waiting_for": getattr(task, "waiting_for", None), "step": getattr(task, "step", None),
            "can_run_now": status in ("QUEUED", "RETRYING") or status in STATUS_FILTERS["failed"],
            "retried_by": None,  # set by rows() when a newer task of the same job exists
            "can_remove": active and status != "RUNNING" and spec.kind in ("ranking", "tailoring"),
            **({"reviewer_score": (task.result or {}).get("reviewer_score")} if spec.kind == "reviewing" else {}),
            **(_application_facts(task, status) if spec.kind == "application" else {}),
            **{k: v for k, v in label.items() if k not in ("title", "company")}}


def _application_facts(task: Any, status: str) -> dict[str, Any]:
    """An application run's backend, and what it waits for when it waits for the person: answers to save in the
    Knowledge bank, answers to confirm in the Apply kit, or something to finish in its browser window. Such a run is
    not claimable until then (application_execution.WAITING_STAGES), so it is not "waiting its turn"."""
    out: dict[str, Any] = {"backend": task.backend}
    if status == "QUEUED" and task.stage in ("NEEDS_INPUT", "NEEDS_HUMAN"):
        waiting = task.interrupt or {}
        out["waiting_for"] = "YOU"
        out["waiting_detail"] = {"kind": waiting.get("kind") or task.stage, "reason": waiting.get("reason"),
                                 "questions": len(waiting.get("question_ids") or []),
                                 "confirmations": len(waiting.get("confirm_question_ids") or []),
                                 "fields": len(waiting.get("fields") or []), "expires_at": waiting.get("expires_at"),
                                 "takeover": bool(waiting.get("takeover")),
                                 "continued": bool(waiting.get("continued"))}
        out["can_run_now"] = False  # it resumes by itself once the person has done what it needs
    return out


# Status filters, the same for every worker: what a task is doing, in the words the screen uses.
STATUS_FILTERS = {
    "active": ("RUNNING", "QUEUED", "RETRYING"), "running": ("RUNNING",), "queued": ("QUEUED",),
    "retrying": ("RETRYING",), "failed": ("FAILED", "BLOCKED", "STOPPED"),
    "done": ("COMPLETED", "PARTIAL", "SUCCEEDED", "SKIPPED", "CANCELLED"),
}
SCAN_LIMIT = 2000  # rows read before filtering, so a match past the first page is still found


def bucket(status: str) -> str:
    """Which status filter a displayed status falls under (besides "active")."""
    return next(name for name, members in STATUS_FILTERS.items() if name != "active" and status in members)


# ───────────── clearing the finished list ─────────────
# "Clear finished" hides finished tasks from a tab's list; nothing is deleted. Each tab keeps the time it was cleared
# (ranking keeps its own, shared with the ranking tracker); finished tasks from before it stay out of the list, while
# metrics, the Performance card (performance), the activity chart and each job's history still count them.
CLEARED_PREFERENCES = "workers_cleared"  # AppSetting.preferences[...]: {kind: ISO time}


def cleared_at(db: Session, user_id: int, kind: str) -> datetime | None:
    spec = kind_of(kind)
    if spec.kind == "ranking":
        from app.services import ranker

        return ranker.finished_cleared_at(db, user_id)
    prefs = db.scalar(select(AppSetting.preferences).where(AppSetting.user_id == user_id)) or {}
    value = (prefs.get(CLEARED_PREFERENCES) or {}).get(spec.kind)
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def clear_finished(db: Session, user_id: int, kind: str, *, clear: bool = True,
                   now: datetime | None = None) -> dict[str, Any]:
    """Hide (or, with clear=False, show again) a tab's finished tasks. Only the view changes. Commits."""
    spec, now = kind_of(kind), now or utcnow()
    if spec.kind == "ranking":
        from app.services import ranker

        ranker.clear_finished(db, user_id, clear=clear, now=now)
    else:
        from sqlalchemy.orm.attributes import flag_modified

        from app.services import user_settings

        row = user_settings.lock_settings_row(db, user_settings.settings_for(db, user_id))
        prefs = dict(row.preferences or {})
        cleared = {k: v for k, v in (prefs.get(CLEARED_PREFERENCES) or {}).items() if k != spec.kind}
        if clear:
            cleared[spec.kind] = now.isoformat()
        prefs[CLEARED_PREFERENCES] = cleared
        row.preferences = prefs
        flag_modified(row, "preferences")
        db.commit()
    return {"kind": spec.kind, "cleared_at": _iso(cleared_at(db, user_id, spec.kind))}


def _hidden(db: Session, spec: WorkerKind, user_id: int, since: datetime | None, now: datetime) -> int:
    """How many finished tasks of the last day a clear is hiding."""
    if since is None:
        return 0
    m = spec.model
    finished_column = m.finished_at if spec.tailor else m.completed_at
    return db.scalar(spec.where(select(func.count()).select_from(m).where(
        m.user_id == user_id, m.status.not_in(spec.active), finished_column >= now - RECENT,
        finished_column <= since))) or 0


def rows(db: Session, user_id: int, kind: str, now: datetime | None = None, *, q: str = "",
         status: str = "") -> dict[str, Any]:
    """Active tasks (running first, then what runs next) and those finished in the last day, narrowed by a search
    (job title, company or `#id`) and a status filter. Bounded. `counts` are per status filter over the search, so the
    filter can say how many each choice would show."""
    spec, now = kind_of(kind), now or utcnow()
    if status and status not in STATUS_FILTERS:
        raise WorkerError(f"Unknown status filter '{status}'. Filters: {', '.join(STATUS_FILTERS)}", 422)
    m = spec.model
    finished_column = m.finished_at if spec.tailor else m.completed_at
    since = cleared_at(db, user_id, spec.kind)
    shown_from = max(now - RECENT, since) if since else now - RECENT
    query = spec.where(select(m).where(m.user_id == user_id, or_(m.status.in_(spec.active), finished_column > shown_from)
                                       if since else or_(m.status.in_(spec.active), finished_column >= shown_from)))
    text = (q or "").strip()
    if text and spec.search is not None:
        query = query.where(spec.search(m, text))
    elif text.lstrip("#").isdigit():
        query = query.where(m.job_id == int(text.lstrip("#")))
    elif text:
        like = f"%{text.lower()[:100]}%"
        query = query.join(Job, Job.id == m.job_id).where(Job.user_id == user_id, or_(
            func.lower(Job.title).like(like), func.lower(Job.company).like(like)))
    tasks = list(db.scalars(query.order_by((m.status != spec.running), (m.status != spec.pending),
                                           m.run_now_at.is_(None), m.id).limit(SCAN_LIMIT)).all())
    labels = _labels_for(db, spec, user_id, tasks)
    limit = worker_settings.get(db, user_id, spec.settings_kind, "max_attempts")
    found = [row(spec, t, labels.get(t.id), now, limit) for t in tasks]
    _mark_retried(db, spec, user_id, found, now)
    counts = {name: sum(r["status"] in members for r in found) for name, members in STATUS_FILTERS.items()}
    counts["all"] = len(found)
    out = [r for r in found if not status or r["status"] in STATUS_FILTERS[status]]
    order = {"RUNNING": 0, "QUEUED": 1, "RETRYING": 2}
    out.sort(key=lambda r: (order.get(r["status"], 3), not r["run_now"],
                            r["next_attempt_at"] or "", -(r["task_id"]) if r["status"] not in order else r["task_id"]))
    attach_models(db, user_id, spec, out[:ROW_LIMIT])
    if spec.kind == "recovery":
        _attach_read_routes(db, user_id, out[:ROW_LIMIT])
    return {"rows": out[:ROW_LIMIT], "total": len(out), "truncated": len(out) > ROW_LIMIT or len(tasks) >= SCAN_LIMIT,
            "status_counts": counts, "filters": {"q": text, "status": status or "all"},
            "cleared_at": _iso(since), "cleared_hidden": _hidden(db, spec, user_id, since, now)}


ACTIVITY_HOURS = 24
ACTIVITY_LIMIT = 5000


def activity(db: Session, user_id: int, kind: str, now: datetime | None = None,
             hours: int = ACTIVITY_HOURS) -> dict[str, Any]:
    """The last day of one worker, hour by hour: tasks queued, finished done and finished failed, plus the typical run
    time and wait. Read from the task rows themselves; bounded."""
    spec, now = kind_of(kind), now or utcnow()
    m = spec.model
    finished_column = m.finished_at if spec.tailor else m.completed_at
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=hours - 1)
    buckets = [{"hour": _iso(start + timedelta(hours=i)), "queued": 0, "done": 0, "failed": 0} for i in range(hours)]

    def slot(at: datetime | None) -> dict[str, Any] | None:
        if at is None or at < start:
            return None
        i = int((at - start).total_seconds() // 3600)
        return buckets[i] if 0 <= i < hours else None

    for (created,) in db.execute(spec.where(select(m.created_at).where(m.user_id == user_id, m.created_at >= start))
                                 .limit(ACTIVITY_LIMIT)).all():
        if (b := slot(created)) is not None:
            b["queued"] += 1
    runs, waits = [], []
    finished = db.execute(spec.where(select(m.status, m.created_at, m.started_at, finished_column)
                                     .where(m.user_id == user_id, finished_column >= start)).limit(ACTIVITY_LIMIT)).all()
    for status, created, started, ended in finished:
        b = slot(ended)
        if b is None:
            continue
        if status in spec.active:  # requeued since it finished
            continue
        b["failed" if status.upper() in STATUS_FILTERS["failed"] else "done"] += 1
        if started and ended and ended >= started:
            runs.append((ended - started).total_seconds())
        if created and started and started >= created:
            waits.append((started - created).total_seconds())
    done, failed = sum(b["done"] for b in buckets), sum(b["failed"] for b in buckets)
    return {"kind": spec.kind, "hours": buckets, "done": done, "failed": failed,
            "queued": sum(b["queued"] for b in buckets),
            "success_rate": round(done / (done + failed), 4) if done + failed else None,
            "median_run_seconds": _median(runs), "median_wait_seconds": _median(waits)}


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return round(ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2, 1)


# ───────────── performance over a range ─────────────
# The Performance card of the tabs that had none (JD parsing and JD recovery keep their own metrics). Read from the
# task rows and llm_request_logs of the selected range, never from the list: clearing the list changes nothing here.
PERFORMANCE_LIMIT = 5000
PERFORMANCE_STAGES = {"insights": ("COMPANY_INSIGHTS", "INTERVIEW_INSIGHTS")}  # else the orchestration node's stages
_DONE = ("COMPLETED", "PARTIAL", "SUCCEEDED")
_FAILED = ("FAILED", "BLOCKED")


def _llm_stages(spec: WorkerKind) -> tuple[str, ...]:
    from app.services import orchestration_topology

    found = orchestration_topology.node(spec.node_id)
    return PERFORMANCE_STAGES.get(spec.kind) or (found.llm_stages if found else ())


def llm_usage(db: Session, user_id: int, stages: Iterable[str], start: datetime, end: datetime) -> dict[str, Any]:
    """A worker's model calls in a range, the shape JD parsing's metrics use. Unknown cost and latency stay None."""
    from app.models import LLMRequestLog as L

    stages = tuple(stages)
    calls = db.execute(select(L.status, L.error_type, L.model, L.input_tokens, L.output_tokens, L.latency_ms,
                              L.estimated_cost).where(L.user_id == user_id, L.stage.in_(stages), L.created_at >= start,
                                                      L.created_at <= end).limit(PERFORMANCE_LIMIT)).all() if stages else []
    latencies = [c.latency_ms for c in calls if c.latency_ms is not None]
    costs = [c.estimated_cost for c in calls if c.estimated_cost is not None]
    by_model: dict[str, int] = {}
    errors: dict[str, int] = {}
    for c in calls:
        by_model[c.model or "unknown"] = by_model.get(c.model or "unknown", 0) + 1
        if c.status == "FAILED":
            errors[c.error_type or "OTHER"] = errors.get(c.error_type or "OTHER", 0) + 1
    return {"requests": len(calls), "failed": sum(1 for c in calls if c.status == "FAILED"),
            "input_tokens": sum(c.input_tokens or 0 for c in calls),
            "output_tokens": sum(c.output_tokens or 0 for c in calls),
            "estimated_cost": float(sum(costs)) if costs else None,
            "average_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
            "models": dict(sorted(by_model.items(), key=lambda kv: -kv[1])), "errors": errors,
            "stages": list(stages), "truncated": len(calls) >= PERFORMANCE_LIMIT}


def performance(db: Session, user_id: int, kind: str, start: datetime, end: datetime,
                now: datetime | None = None) -> dict[str, Any]:
    """One worker's results in a range: the tasks created in it, how they ended, how long they took, the errors they
    hit and its model calls, plus what that worker decides (ranking decisions, reviewer scores, partial research).
    Bounded and read only. The list's Clear does not touch any of it."""
    spec, now = kind_of(kind), now or utcnow()
    m, ended = spec.model, ("finished_at" if spec.tailor else "completed_at")
    tasks = list(db.scalars(spec.where(select(m).where(m.user_id == user_id, m.created_at >= start,
                                                       m.created_at <= end))
                            .order_by(m.id.desc()).limit(PERFORMANCE_LIMIT)).all())
    statuses = [_display_status(spec, t, now) for t in tasks]
    by_status: dict[str, int] = {}
    for s in statuses:
        by_status[s] = by_status.get(s, 0) + 1
    done = sum(by_status.get(s, 0) for s in _DONE)
    failed = sum(by_status.get(s, 0) for s in _FAILED)
    durations = [(getattr(t, ended) - t.started_at).total_seconds() * 1000
                 for t, s in zip(tasks, statuses, strict=True)
                 if s in _DONE and t.started_at and getattr(t, ended) and getattr(t, ended) >= t.started_at]
    waits = [(t.started_at - t.created_at).total_seconds() for t in tasks
             if t.started_at and t.created_at and t.started_at >= t.created_at]
    errors: dict[str, int] = {}
    categories: dict[str, int] = {}
    for t, s in zip(tasks, statuses, strict=True):
        if s not in _FAILED:
            continue
        text = t.error if spec.tailor else t.last_error
        key = (redaction.redact_text((text or "unknown").split("\n")[0], 160) or "unknown").strip()
        errors[key] = errors.get(key, 0) + 1
        if not spec.tailor:
            code = getattr(t, "error_category", None)
            category = getattr(code, "value", code) if code else "UNKNOWN"  # an enum, or a plain string column
            categories[category] = categories.get(category, 0) + 1
    out: dict[str, Any] = {
        "kind": spec.kind, "start": _iso(start), "end": _iso(end),
        "tasks_created": len(tasks), "completed": done, "failed": failed,
        "stopped": by_status.get("STOPPED", 0),
        "skipped": by_status.get("SKIPPED", 0) + by_status.get("CANCELLED", 0),
        "active": sum(by_status.get(s, 0) for s in ("RUNNING", "QUEUED", "RETRYING")),
        "by_status": dict(sorted(by_status.items(), key=lambda kv: -kv[1])),
        "success_rate": round(done / (done + failed), 3) if done + failed else None,
        "average_duration_ms": round(sum(durations) / len(durations)) if durations else None,
        "median_wait_seconds": _median(waits),
        "retried": sum(1 for t in tasks if _attempts(spec, t) > 1),
        "failures_by_category": dict(sorted(categories.items(), key=lambda kv: -kv[1])),
        "top_errors": [{"error": e, "tasks": n} for e, n in sorted(errors.items(), key=lambda kv: -kv[1])[:5]],
        "llm": llm_usage(db, user_id, _llm_stages(spec), start, end),
        "truncated": len(tasks) >= PERFORMANCE_LIMIT,
    }
    if spec.kind == "ranking":
        from app.models import JobRanking

        ranked = db.execute(select(JobRanking.decision, JobRanking.overall_fit_score).where(
            JobRanking.user_id == user_id, JobRanking.created_at >= start, JobRanking.created_at <= end)
            .limit(PERFORMANCE_LIMIT)).all()
        decisions: dict[str, int] = {}
        for decision, _ in ranked:
            decisions[decision or "UNKNOWN"] = decisions.get(decision or "UNKNOWN", 0) + 1
        fits = [float(f) for _, f in ranked if f is not None]
        out["ranking"] = {"rankings": len(ranked), "decisions": dict(sorted(decisions.items(), key=lambda kv: -kv[1])),
                          "qualified": decisions.get("QUALIFIED", 0), "archived": decisions.get("ARCHIVED", 0),
                          "average_fit": round(sum(fits) / len(fits), 1) if fits else None,
                          "median_fit": _median(fits)}
    elif spec.kind == "insights":
        out["insights"] = {"complete": by_status.get("COMPLETED", 0), "partial": by_status.get("PARTIAL", 0)}
    elif spec.tailor:
        results = [t.result or {} for t, s in zip(tasks, statuses, strict=True) if s == "SUCCEEDED"]
        scores = [float(r["reviewer_score"]) for r in results if isinstance(r.get("reviewer_score"), int | float)]
        out["review"] = {"scored": len(scores), "median_score": _median(scores),
                         "lowest_score": min(scores) if scores else None,
                         "highest_score": max(scores) if scores else None,
                         "auto_approved": sum(1 for r in results if r.get("auto_approved")),
                         "untailored": sum(1 for r in results if r.get("untailored")),
                         "skip_mode": sum(1 for t in tasks if t.mode == "skip")}
    return out


def _newer(db: Session, spec: WorkerKind, user_id: int, job_ids: Iterable[int]) -> dict[int, Any]:
    """The latest task of each job: a failed task that has a newer one was already run again."""
    ids = list(set(job_ids))
    if not ids or not spec.job_bound:  # a task without a job is retried in place: there is never a newer one
        return {}
    m = spec.model
    latest = db.execute(select(func.max(m.id)).where(m.user_id == user_id, m.job_id.in_(ids)).group_by(m.job_id)).scalars()
    return {t.job_id: t for t in db.scalars(select(m).where(m.id.in_(list(latest)))).all()}


def _mark_retried(db: Session, spec: WorkerKind, user_id: int, rows_: list[dict[str, Any]], now: datetime) -> None:
    """A failed or blocked task that was run again (a newer task exists for its job) can no longer be run now: the
    newer task is the one to watch. It says which task that is and what it is doing."""
    finished = [r for r in rows_ if r["status"] in STATUS_FILTERS["failed"]]
    newest = _newer(db, spec, user_id, (r["job_id"] for r in finished))
    for r in finished:
        latest = newest.get(r["job_id"])
        if latest is not None and latest.id != r["task_id"]:
            r["can_run_now"] = False
            r["retried_by"] = {"task_id": latest.id, "status": _display_status(spec, latest, now)}


# ───────────── the model a task uses ─────────────
# Read, never decided: the route and model come from the active configuration and the failover overlay
# (LLMClient.route_for), what a running task actually called comes from its own LLM request rows. No model is called.

def _route_label(route: str) -> str:
    if route == "LITELLM":
        return "LiteLLM"
    from app.services.user_settings import KEY_LABELS

    return KEY_LABELS.get(route.lower(), route.title())


def _via(plan: dict[str, Any]) -> str | None:
    if plan.get("mode") == "AUTO_ROUTER":
        return "auto router"
    if plan.get("mode") == "TIER" and plan.get("configured"):
        return f"tier {plan['configured']}"
    return None


def _change(plan: dict[str, Any], last: Any | None, finished: bool = False) -> dict[str, Any] | None:
    """What moved this task off its assigned model, newest fact first; None when nothing did. For a finished task only
    what happened in its own request counts: today's configuration or resting models say nothing about it."""
    if last is not None:
        if last.escalated:
            return {"kind": "ESCALATED", "from": plan.get("primary"), "to": last.model or last.requested_model,
                    "reason": (last.escalation_reason or "").replace("_", " ").lower() or "quality escalation"}
        if last.routing_cause == "FAILOVER":
            # the request's own fact; a row from before it was recorded falls back to today's plan only while active
            source = last.failed_over_from or (None if finished else plan.get("primary"))
            return {"kind": "FAILOVER", "from": source, "to": last.model or last.requested_model,
                    "reason": "its model was resting"}
        if last.fallback_used:
            return {"kind": "FALLBACK", "from": last.requested_model, "to": last.model,
                    "reason": "LiteLLM fell back after an error"}
        if finished:
            return None
        if plan.get("model") and last.requested_model and last.requested_model != plan["model"] \
                and last.requested_model != plan.get("failed_over_from"):
            return {"kind": "CONFIG", "from": last.requested_model, "to": plan["model"],
                    "reason": "the model was changed since its last request"}
    if plan.get("failed_over_from"):
        return {"kind": "RESTING", "from": plan["failed_over_from"], "to": plan.get("model"),
                "reason": "its model is resting, so a backup answers"}
    return None


def attach_models(db: Session, user_id: int, spec: WorkerKind | str, rows_: list[dict[str, Any]],
                  status_of: Callable[[dict[str, Any]], str | None] | None = None) -> None:
    """Add `model` (route, model, how it was chosen) and `model_change` to every row.

    A finished task shows the model of its own last request (between its creation and its finish), or `source:
    "none"` when it never called a model; only what happened in that request counts as a change (failover, LiteLLM
    fallback, escalation). A running or retrying task shows its own latest request when there is one; otherwise, like
    a waiting task, the model its next request goes to. Bounded (one query), metadata only, and it never raises.
    `status_of` reads a row's RUNNING / QUEUED / RETRYING when the rows are not Running workers rows (the ranking
    tracker, the tailoring task list); anything else is finished.

    A conductor's row reads the requests of the workers it hands steps to: while active, those of the worker at its
    current step, even while it waits for that worker (its own status is then only ever queued); once finished, the
    last request made for the job during the run. The step's worker also decides the plan, and `worker` names it. An
    active run with no worker has no model next (it waits for a person), so it reads like a finished one."""
    spec = kind_of(spec) if isinstance(spec, str) else spec
    status_of = status_of or (lambda r: r.get("status"))
    workflow_type, key = spec.llm_link
    rows_ = [r for r in rows_ if r.get(key)]
    if not rows_:
        return
    try:
        from app.models import LLMRequestLog
        from app.services.llm import LLMClient
        from app.services.orchestration_topology import NODES

        workers = spec.conducts or next((n.workers for n in NODES if n.id == spec.node_id), ())
        if not workers:
            return
        client = LLMClient.for_user(db, user_id)
        plans: dict[str, dict[str, Any]] = {}

        def plan_of(worker: str | None) -> dict[str, Any]:
            # A conductor's plan is its current worker's; anything else keeps its node's first worker.
            name = worker if spec.conducts and worker else workers[0]
            if name not in plans:
                plans[name] = client.route_for(name)
            return plans[name]

        since = min((r["created_at"] for r in rows_ if r.get("created_at")), default=None)
        if since is None:
            return
        logs = db.execute(select(LLMRequestLog).where(
            LLMRequestLog.user_id == user_id, LLMRequestLog.workflow_type == workflow_type,
            LLMRequestLog.workflow_entity_id.in_({r[key] for r in rows_}),
            LLMRequestLog.worker_type.in_(workers), LLMRequestLog.created_at >= datetime.fromisoformat(since))
            .order_by(LLMRequestLog.id.desc()).limit(1000)).scalars().all()
        by_entity: dict[int, list[Any]] = {}
        for log in logs:  # newest first
            by_entity.setdefault(log.workflow_entity_id, []).append(log)
        browser_use = spec.kind == "application" and _browser_use_configured()
        at_step = spec.step_worker(db, user_id, [r for r in rows_ if status_of(r) in STATUS_FILTERS["active"]]) \
            if spec.step_worker else {}
        for r in rows_:
            active = status_of(r) in STATUS_FILTERS["active"]
            step_worker = at_step.get(r.get("task_id"))
            if spec.kind == "application" and active and not (
                    r.get("backend") == "browser_use_webui" or (r.get("backend") is None and browser_use)):
                # Only the browser-use backend asks a model anything (the Browser Navigator); a form adapter never
                # does. A run not routed yet may still go to browser-use (a form the adapters cannot read), so it
                # shows the Navigator's model while browser-use is set up. A finished run shows what it really used.
                r["model"], r["model_change"] = {"model": None, "source": "none"}, None
                continue
            start = datetime.fromisoformat(r["created_at"]) if r.get("created_at") else None
            finished = r.get("finished_at") or r.get("completed_at")
            end = datetime.fromisoformat(finished) + timedelta(seconds=5) if finished and not active else None
            last = next((log for log in by_entity.get(r[key], [])
                         if (start is None or log.created_at >= start) and (end is None or log.created_at <= end)
                         and (step_worker is None or log.worker_type == step_worker)),
                        None)
            if not active or (spec.step_worker is not None and step_worker is None):
                plan = plan_of(last.worker_type if last is not None else None)
                r["model"] = _used(last, plan) if last is not None else {"model": None, "source": "none"}
                r["model_change"] = _change(plan, last, finished=True) if last is not None else None
                if spec.conducts and last is not None:
                    r["model"]["worker"] = _worker_label(last.worker_type)
                continue
            plan = plan_of(step_worker)
            used = last is not None and (bool(spec.conducts) or status_of(r) in ("RUNNING", "RETRYING"))
            r["model"] = _used(last, plan) if used else {
                "model": plan.get("model"), "route": plan["route"], "route_label": _route_label(plan["route"]),
                "via": _via(plan), "source": "next", "backups": plan.get("backups") or []}
            r["model_change"] = _change(plan, last if used else None)
            if step_worker:
                r["model"]["worker"] = _worker_label(step_worker)
    except Exception:
        import logging

        logging.getLogger("jobhunt.workers").warning("could not read task models", exc_info=True)


def _attach_read_routes(db: Session, user_id: int, rows_: list[dict[str, Any]]) -> None:
    """A finished recovery task may use no model at all, so its row also says what read the page (`read_route`), and a
    move from one route to the next is its change, unless a model's own change is already there. Only what the task
    recorded counts. Bounded (one query), labels only, and it never raises."""
    finished = {r["task_id"]: r for r in rows_ if r.get("status") not in STATUS_FILTERS["active"]}
    if not finished:
        return
    try:
        from app.models import JobDescriptionFetchTask
        from app.services.jd_recovery import read_route

        for task_id, diagnostics in db.execute(select(JobDescriptionFetchTask.id, JobDescriptionFetchTask.diagnostics)
                                               .where(JobDescriptionFetchTask.user_id == user_id,
                                                      JobDescriptionFetchTask.id.in_(finished))).all():
            route = read_route(diagnostics)
            if route is None:
                continue
            r = finished[task_id]
            r["read_route"] = route["label"]
            if route["from"] and not r.get("model_change"):
                r["model_change"] = {"kind": "ROUTE", "from": route["from"], "to": route["label"],
                                     "reason": route["reason"]}
    except Exception:
        import logging

        logging.getLogger("jobhunt.workers").warning("could not read recovery routes", exc_info=True)


def _worker_label(worker_type: str | None) -> str | None:
    from app.services.ai_workers import WORKERS

    found = WORKERS.get(worker_type or "")
    return found.label if found else worker_type


def _browser_use_configured() -> bool:
    from app.infrastructure.application_browser.browser_use_sidecar import configured

    return configured()


def _used(last: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """The model and route one of the task's own requests went to."""
    route = (last.provider or "").upper()
    route = "LITELLM" if route in ("LITELLM", "") else route
    return {"model": last.model or last.requested_model, "route": route, "route_label": _route_label(route),
            "via": _via(plan) if route == plan.get("route") else None, "source": "used",
            "backups": plan.get("backups") or []}


def ranking_status(item: dict[str, Any]) -> str | None:
    """A ranking tracker task in Running workers' words."""
    if item.get("status") == "PROCESSING":
        return "RUNNING"
    if item.get("status") == "PENDING":
        return "RETRYING" if item.get("attempt_count") else "QUEUED"
    return None


def attach_tailoring_models(db: Session, user_id: int, items: list[dict[str, Any]]) -> None:
    """Tailoring tasks: the reviewer's model while a task is at its review step, the tailor's otherwise."""
    status = {"running": "RUNNING", "queued": "QUEUED"}
    reviewing = [i for i in items if i.get("stage") == "reviewing"]
    attach_models(db, user_id, "reviewing", reviewing, lambda i: status.get(i.get("status")))
    attach_models(db, user_id, "tailoring", [i for i in items if i.get("stage") != "reviewing"],
                  lambda i: status.get(i.get("status")))


def _labels(db: Session, job_ids: list[int], user_id: int) -> dict[int, tuple[str, str]]:
    if not job_ids:
        return {}
    return {r[0]: (r[1], r[2]) for r in db.execute(select(Job.id, Job.title, Job.company)
                                                  .where(Job.id.in_(set(job_ids)), Job.user_id == user_id)).all()}


def _labels_for(db: Session, spec: WorkerKind, user_id: int, tasks: list[Any]) -> dict[int, dict[str, Any]]:
    """Each task's label by task id: its job's title and company, or what the kind's `subject` says."""
    if not spec.job_bound:
        return spec.subject(db, user_id, tasks) if tasks else {}
    jobs = _labels(db, [t.job_id for t in tasks], user_id)
    return {t.id: {"title": jobs[t.job_id][0], "company": jobs[t.job_id][1]} for t in tasks if t.job_id in jobs}


def counts(db: Session, user_id: int, now: datetime | None = None) -> dict[str, dict[str, int]]:
    """Running, queued and retrying tasks per worker: the tab badges and the sidebar count."""
    now = now or utcnow()
    out = {}
    for spec in KINDS.values():
        m = spec.model
        waiting = m.created_at if spec.tailor else m.next_attempt_at  # tailoring has no retry wait
        tasks = db.execute(spec.where(select(m.status, waiting, spec.attempts)
                                      .where(m.user_id == user_id, m.status.in_(spec.active))).limit(5000)).all()
        c = {"running": 0, "queued": 0, "retrying": 0}
        for status, wait_until, attempts in tasks:
            if status == spec.running:
                c["running"] += 1
            elif not spec.tailor and ((wait_until is not None and wait_until > now) or (attempts or 0) > 0):
                c["retrying"] += 1
            else:
                c["queued"] += 1
        c["active"] = c["running"] + c["queued"] + c["retrying"]
        out[spec.kind] = c
    return out


# ───────────── controls ─────────────

def _task(db: Session, spec: WorkerKind, user_id: int, task_id: int) -> Any:
    task = db.scalar(_lock(db, spec.where(select(spec.model).where(spec.model.id == task_id,
                                                                   spec.model.user_id == user_id))))
    if task is None:
        raise WorkerError("Task not found", 404)
    return task


def _requeue_fresh(db: Session, spec: WorkerKind, task: Any, user_id: int, now: datetime) -> bool:
    """Take the task away from whatever run holds it and put it back at the start of the queue. True if it ran."""
    was_running = task.status == spec.running
    if spec.tailor:
        task.status, task.stage, task.attempts = spec.pending, "queued", 0
        task.locked_by = task.lock_expires_at = task.started_at = None
        task.error = None
        return was_running
    task.status = spec.pending
    task.lock_token = task.lock_expires_at = None
    for column in ("locked_by", "locked_at", "error_category"):
        if hasattr(task, column):
            setattr(task, column, None)
    task.attempt_count, task.next_attempt_at = 0, None
    task.last_error = KILLED_NOTE
    if spec.kind == "ranking" and was_running:
        # The ranker moved the job to RANKING when it claimed the task; it waits for ranking again now.
        from app.services.work_queue import transition

        job = db.scalar(_lock(db, select(Job).where(Job.id == task.job_id, Job.user_id == user_id)))
        if job is not None and job.state is WorkflowState.RANKING:
            transition(db, job, WorkflowState.RANK_PENDING, actor=f"user:{user_id}",
                       reason="Stopped from Running workers; waiting to be ranked again", now=now)
    return was_running


def _stop(db: Session, spec: WorkerKind, task: Any, user_id: int, now: datetime) -> bool:
    """End a task as stopped by the person and keep its job out of the automatic queue. True if it was running.

    The lease goes with it, so a run still talking to a model can no longer write anything (the same guards as
    before); its job is not picked up again by any sweep until someone asks (Retry, Run now, or the job's own
    screen)."""
    stage = getattr(task, "stage", None)
    was_running = _requeue_fresh(db, spec, task, user_id, now)  # takes the lease away; ranking: job back to waiting
    if spec.tailor:
        from app.services.tailor_tasks import DECLINED

        # A run stopped while reviewing keeps that stage, so Reviewing still lists it.
        task.status, task.error, task.finished_at = "cancelled", STOPPED_BY_USER, now
        task.stage = "reviewing" if was_running and stage == "reviewing" else "done"
        job = db.scalar(select(Job).where(Job.id == task.job_id, Job.user_id == user_id))
        if job is not None:
            job.auto_tailor_status = DECLINED  # automatic tailoring never queues it again on its own
        return was_running
    task.status = spec.stopped_status
    task.completed_at, task.next_attempt_at = now, None
    task.last_error = STOPPED_BY_USER
    if spec.kind == "recovery":
        task.result_code = "STOPPED_BY_USER"
    if spec.kind == "qualification":
        from app.services import qualification

        qualification.on_stopped(db, task, now)  # its graph thread stops too, so no node writes again
    if spec.kind == "preparation":
        from app.services import preparation

        preparation.on_stopped(db, task, now)  # its thread and the tailoring pass it queued stop too
    if spec.kind == "application":
        from app.services import application_execution

        application_execution.on_stopped(db, task, now)  # its graph thread stops too, and its stage is CANCELLED
    if spec.kind == "resume_parsing":
        from app.services import resume_parsing

        resume_parsing.on_stopped(db, task, now)  # its graph thread stops too, and its staged output is dropped
    if spec.kind == "insights" and getattr(task, "agent_thread_id", None):
        from app.services import qualification

        qualification.stop_threads(db, [task.agent_thread_id], now)  # a graph run (JOB_INSIGHTS_GRAPH_MODE=on)
    if spec.kind == "ranking":
        from app.services.work_queue import transition

        job = db.scalar(_lock(db, select(Job).where(Job.id == task.job_id, Job.user_id == user_id)))
        if job is not None and job.state is WorkflowState.RANK_PENDING:
            # SKIPPED, like a job taken out of the ranking queue: no sweep queues it again (ranker.remove_from_ranking).
            transition(db, job, WorkflowState.SKIPPED, actor=f"user:{user_id}", reason=STOPPED_BY_USER, now=now)
    return was_running


def kill(db: Session, user_id: int, kind: str, task_ids: list[int] | None = None,
         now: datetime | None = None) -> dict[str, Any]:
    """Stop running and retrying tasks and take them out of the queue. `task_ids` None: every one of this worker's
    tasks, and the account's provider cooldowns are cleared too. Commits."""
    spec, now = kind_of(kind), now or utcnow()
    m = spec.model
    query = spec.where(select(m).where(m.user_id == user_id, m.status.in_(spec.active)))
    if task_ids is not None:
        query = query.where(m.id.in_(task_ids))
    tasks = list(db.scalars(_lock(db, query.order_by(m.id))).all())
    if task_ids is not None and not tasks:
        raise WorkerError("That task has already finished", 409)
    running = stopped = 0
    stopped_jobs = []
    for task in tasks:
        # A graph run between steps (waiting for recovery, a parse or a ranking) has started: Kill stops it too.
        if task.status != spec.running and _display_status(spec, task, now) != "RETRYING" \
                and not _started_graph_run(spec, task):
            continue  # a task still waiting its first turn is not stopped by Kill (Remove takes it out)
        running += _stop(db, spec, task, user_id, now)
        stopped += 1
        stopped_jobs.append(getattr(task, "job_id", None))
    db.commit()
    if spec.kind == "parsing":
        from app.services import job_description

        for job_id in stopped_jobs:  # a stopped parse no longer holds ranking back: it reads the raw text, now
            job_description.hand_to_ranking(db, job_id)
    cleared = backoff.reset(db, user_id) if task_ids is None else []
    return {"kind": spec.kind, "stopped": stopped, "running": running, "cooldowns_cleared": cleared}


def kill_all(db: Session, user_id: int, now: datetime | None = None) -> dict[str, Any]:
    results = {kind: kill(db, user_id, kind, None, now) for kind, spec in KINDS.items() if spec.view_of is None}
    return {"workers": results, "stopped": sum(r["stopped"] for r in results.values()),
            "running": sum(r["running"] for r in results.values()),
            "cooldowns_cleared": sorted({p for r in results.values() for p in r["cooldowns_cleared"]})}


def _requeue_finished(db: Session, spec: WorkerKind, task: Any, user_id: int) -> Any:
    """A failed or blocked task gets a new one through the path that normally queues it (a task without a job: the
    same row, through its kind's `requeue`)."""
    if spec.requeue is not None:
        return spec.requeue(db, task, user_id)
    job = db.scalar(select(Job).where(Job.id == task.job_id, Job.user_id == user_id))
    if job is None:
        raise WorkerError("The job no longer exists", 404)
    if spec.kind == "ranking":
        from app.services import ranker

        out = ranker.request_ranking(db, [job.id], force=True, actor=f"user:{user_id}")
        if out["skipped"]:
            raise WorkerError(out["skipped"][0]["reason"])
    elif spec.kind == "parsing":
        from app.services import job_description

        if job_description.queue_parse(db, job, reason="user_request") is None:
            raise WorkerError("This job has no description to parse")
        db.commit()
    elif spec.kind == "classification":
        from app.services import domain_classifier

        domain_classifier.requeue(db, job, previous=task)
        db.commit()
    elif spec.kind == "recovery":
        from app.services import jd_recovery

        if jd_recovery.enqueue(db, job, reason="user_request", force=True) is None:
            raise WorkerError("This job has no posting URL to recover from")
        db.commit()
    elif spec.kind == "qualification":
        from app.services import qualification

        return qualification.requeue(db, task, user_id)
    elif spec.kind == "preparation":
        from app.services import preparation

        return preparation.requeue(db, task, user_id)
    elif spec.kind == "application":
        from app.services import application_execution

        return application_execution.requeue(db, task, user_id)
    elif spec.kind == "insights":
        from app.services.job_insights import tasks as insight_tasks

        insight_tasks.enqueue(db, job, reason=insight_tasks.USER_REQUEST, force=True)
        db.commit()
    else:
        from app.services import tailor_tasks

        # Regenerate: a run that failed in review may already have saved its packet, which a plain enqueue refuses.
        # A retried revision pass still fixes the same review, at the same page target (never `conducted`: a person
        # asked for this run, not the preparation graph).
        kept = {k: v for k, v in (task.options or {}).items() if k in ("revise_from", "pages")}
        out = tailor_tasks.enqueue(db, [job.id], task.mode, True, options=kept or None)
        if out["skipped"]:
            raise WorkerError(f"Cannot tailor again: {out['skipped'][0]['reason']}")
    fresh = db.scalar(select(spec.model).where(spec.model.job_id == job.id, spec.model.user_id == user_id,
                                               spec.model.status.in_(spec.active)))
    if fresh is None:
        raise WorkerError("The job could not be queued again")
    return fresh


RETRY_LIMIT = 500


def retry(db: Session, user_id: int, kind: str, task_ids: list[int], now: datetime | None = None) -> dict[str, Any]:
    """Queue many failed or blocked tasks again at once, each through its worker's normal path (_requeue_finished).

    Per task, never all-or-nothing: a task that is not the account's, has not failed, or whose job was already run
    again is skipped with its reason. Unlike Run now nothing jumps the queue or skips a provider cooldown: failures
    in bulk usually mean an outage, and the retried tasks wait their turn like any other. Commits."""
    spec, now = kind_of(kind), now or utcnow()
    ids = list(dict.fromkeys(int(i) for i in task_ids))[:RETRY_LIMIT]
    if not ids:
        raise WorkerError("Choose at least one task to retry", 422)
    tasks = {t.id: t for t in db.scalars(spec.where(select(spec.model).where(spec.model.user_id == user_id,
                                                                             spec.model.id.in_(ids)))).all()}
    retried, skipped = [], []
    for task_id in ids:
        task = tasks.get(task_id)
        if task is None:
            skipped.append({"task_id": task_id, "reason": "Not found"})
            continue
        shown = _display_status(spec, task, now)
        if shown not in STATUS_FILTERS["failed"]:
            skipped.append({"task_id": task_id, "reason": f"Not failed ({shown.lower()})"})
            continue
        latest = _newer(db, spec, user_id, [getattr(task, "job_id", None)]).get(getattr(task, "job_id", None))
        if latest is not None and latest.id != task.id:
            skipped.append({"task_id": task_id, "reason": f"Already run again as task #{latest.id}"})
            continue
        try:
            fresh = _requeue_finished(db, spec, task, user_id)
            db.commit()
        except WorkerError as exc:
            db.rollback()
            skipped.append({"task_id": task_id, "reason": str(exc)})
            continue
        retried.append({"task_id": task_id, "new_task_id": fresh.id, "job_id": getattr(task, "job_id", None)})
    return {"kind": spec.kind, "retried": retried, "skipped": skipped}


def run_now(db: Session, user_id: int, kind: str, task_id: int, now: datetime | None = None) -> dict[str, Any]:
    """Put one task at the front of its queue, past its retry wait, the ranking cadence and provider cooldowns.
    A failed task is queued again first. Commits; the caller nudges the scheduler (run_soon)."""
    spec, now = kind_of(kind), now or utcnow()
    task = _task(db, spec, user_id, task_id)
    if task.status == spec.running:
        raise WorkerError("This task is running right now")
    if task.status not in spec.active:
        if _display_status(spec, task, now) not in STATUS_FILTERS["failed"]:
            raise WorkerError("This task already finished; queue the job again from its screen" if spec.job_bound
                              else "This entry already finished; nothing is waiting to run")
        latest = _newer(db, spec, user_id, [getattr(task, "job_id", None)]).get(getattr(task, "job_id", None))
        if latest is not None and latest.id != task.id:
            raise WorkerError(f"Already run again as task #{latest.id} "
                              f"({_display_status(spec, latest, now).lower()}); use Run now on that one")
        task = _requeue_finished(db, spec, task, user_id)
    task.run_now_at = now
    if not spec.tailor:
        task.next_attempt_at = None
    if spec.kind == "ranking":
        task.force = True  # a ranking the person asked for: the cadence never holds it back
    db.commit()
    labels = _labels_for(db, spec, user_id, [task])
    return row(spec, task, labels.get(task.id), now, worker_settings.get(db, user_id, spec.settings_kind, "max_attempts"))


def run_queued_now(db: Session, user_id: int, kind: str, now: datetime | None = None) -> dict[str, Any]:
    """Rank (or parse, recover, research) everything that is waiting, now: every queued or waiting task of the account
    loses its retry wait, and ranking tasks become the person's own request, so the account's cadence, a paused queue
    or auto-ranking being off no longer hold them back.

    Unlike one task's Run now this does not set run_now_at: a provider cooldown still applies, because many tasks at
    once are exactly what makes a provider refuse (the same rule as bulk Retry). Tasks a person stopped stay stopped;
    tasks whose description is still being read wait for it (the ranking claim skips them). Commits; the caller wakes
    the worker (run_soon)."""
    spec, now = kind_of(kind), now or utcnow()
    if spec.tailor:
        raise WorkerError("Tailoring starts as soon as a slot is free: there is no wait to skip")
    tasks = db.scalars(select(spec.model).where(spec.model.user_id == user_id, spec.model.status == spec.pending,
                                                or_(spec.model.last_error.is_(None), spec.model.last_error != STOPPED_BY_USER))
                       .order_by(spec.model.id).limit(RETRY_LIMIT)).all()
    for task in tasks:
        task.next_attempt_at = None
        if spec.kind == "ranking":
            task.force = True
    db.commit()
    return {"kind": spec.kind, "released": len(tasks), "task_ids": [t.id for t in tasks]}


# ───────────── the screen ─────────────

def scheduler_facts(scheduler: Any, job: str | None) -> dict[str, Any] | None:
    """When this worker's scheduler job runs. Schedule facts only: results and errors span every account."""
    if job is None:
        settings = get_settings()
        return {"name": "tailor_worker", "enabled": bool(settings.tailor_worker_enabled), "interval_minutes": None,
                "poll_seconds": 2, "concurrency": server_settings.value("tailor_concurrency"), "running": None,
                "next_run_at": None,
                "last_finished_at": None, "last_status": None}
    if scheduler is None:
        return None
    found = next((j for j in scheduler.status()["jobs"] if j["name"] == job), None)
    if found is None:
        return None
    return {k: found.get(k) for k in ("name", "enabled", "interval_minutes", "running", "next_run_at",
                                      "last_finished_at", "last_status")}


def summary(db: Session, user_id: int, scheduler: Any = None, now: datetime | None = None) -> dict[str, Any]:
    from app.services import worker_handoff

    now = now or utcnow()
    per_kind = counts(db, user_id, now)
    return {"generated_at": _iso(now), "cooldowns": backoff.active(db, user_id, now=now),
            "workers": [{"kind": s.kind, "label": s.label, "description": s.description, "node_id": s.node_id,
                         "counts": per_kind[s.kind], "scheduler": scheduler_facts(scheduler, s.scheduler_job),
                         "paused": not worker_settings.get(db, user_id, s.settings_kind, "auto"), "view_of": s.view_of,
                         "cleared_at": _iso(cleared_at(db, user_id, s.kind)),
                         # Nudge forward: where this worker's finished jobs go next, and how many are stuck before it.
                         "next_stage": worker_handoff.next_stage(db, user_id, s.kind)}
                        for s in KINDS.values()],
            "active": sum(per_kind[k]["active"] for k, s in KINDS.items() if s.view_of is None),
            "running": sum(per_kind[k]["running"] for k, s in KINDS.items() if s.view_of is None)}


# ───────────── a task's history ─────────────
# What happened to one task, in order, from facts that are already stored: its own row and attempts, the graph steps
# it recorded (agent_effects), its job's workflow transitions and application events, its submission intent, and the
# page loads, runner commands and model calls made for its job while it ran. Metadata only: never a form value, an
# answer, page text or a prompt. Bounded: each source is read with a limit.

HISTORY_LIMIT = 200
STEP_LABELS = {
    "verify_packet": "Checked the approved packet", "route": "Chose how to open the form",
    "analyze_form": "Read the application form", "fill": "Filled the form",
    "pre_submit_validation": "Checked every required field", "submit": "Guarded submission step",
}


def _step(key: str) -> tuple[str, str]:
    """A recorded graph step (its effect key) in words, and its tone."""
    parts = key.split(":")
    name = parts[0]
    if "takeover" in parts:  # resolve_answers:<task>:takeover:a<n>:<reason>:<fields> too
        reason = next((p for p in reversed(parts) if p and not p.isdigit()), "")
        return f"Waited for you in the browser window ({reason.replace('_', ' ').lower()})", "warn"
    if name == "resolve_answers" and len(parts) >= 5 and parts[2].startswith("a") \
            and parts[3].isdigit() and parts[4].isdigit():
        asked, confirm = int(parts[3]), int(parts[4])
        if asked or confirm:
            return (f"Saved {asked} question{'s' if asked != 1 else ''} for you to answer"
                    + (f" and {confirm} to confirm" if confirm else ""), "warn")
        return "Found a verified source for every answer", "good"
    if name == "fill" and parts[-1] == "done":
        return "Finished filling the form", "good"
    if name == "fill":
        return "Started filling the form", "muted"
    if name == "submit":
        return {"fill": "Filled the form for a guarded submission", "validated": "Passed the pre-submit checks",
                "clicking": "Authorized the one final click", "clicked": "Clicked Submit"}.get(
            parts[2] if len(parts) > 2 else "", f"Stopped the guarded submission ({parts[-1].lower()})"), "brand"
    return STEP_LABELS.get(name, name.replace("_", " ").capitalize()), "muted"


def history(db: Session, user_id: int, kind: str, task_id: int, now: datetime | None = None) -> dict[str, Any]:
    """The task's timeline for Running workers, newest first. 404 for another account's task."""
    import re

    from app.models import (
        AgentEffect,
        ApplicationEvent,
        ApplicationExecutionAttempt,
        ExternalAPIRequestLog,
        LLMRequestLog,
        SubmissionIntent,
        WorkflowTransition,
    )

    spec, now = kind_of(kind), now or utcnow()
    task = db.scalar(spec.where(select(spec.model).where(spec.model.id == task_id, spec.model.user_id == user_id)))
    if task is None:  # read only: no row lock, unlike the actions
        raise WorkerError("Task not found", 404)
    labels = _labels_for(db, spec, user_id, [task])
    view = row(spec, task, labels.get(task.id), now, worker_settings.get(db, user_id, spec.settings_kind,
                                                                           "max_attempts"))
    start = task.created_at
    end = (task.finished_at if spec.tailor else getattr(task, "completed_at", None)) or now
    job_id = getattr(task, "job_id", None)
    events: list[dict[str, Any]] = []

    def add(at: datetime | None, title: str, *, detail: str = "", tone: str = "muted", source: str) -> None:
        if at is not None:
            events.append({"at": at.isoformat(), "title": title, "detail": detail[:300], "tone": tone,
                           "source": source})

    add(start, "Queued", detail=f"because: {getattr(task, 'reason', '') or 'a person asked'}"
        + (f" · backend {task.backend}" if getattr(task, "backend", None) else ""), source="task")
    if task.started_at:
        add(task.started_at, "Claimed by the worker (latest attempt)", source="task")
    if spec.kind == "application":
        for a in db.scalars(select(ApplicationExecutionAttempt).where(
                ApplicationExecutionAttempt.task_id == task.id).order_by(ApplicationExecutionAttempt.attempt)
                .limit(HISTORY_LIMIT)).all():
            add(a.started_at, f"Attempt {a.attempt} started", source="attempt")
            if a.ended_at:
                outcome = (a.outcome or "ended").replace("_", " ").lower()
                add(a.ended_at, f"Attempt {a.attempt} {outcome}",
                    detail=" · ".join(filter(None, [f"reached {a.stage_reached.replace('_', ' ').lower()}"
                                                    if a.stage_reached else "", a.error_category or ""])),
                    tone="bad" if a.outcome == "FAILED" else "warn" if a.outcome in ("WAITING", "BLOCKED") else "good",
                    source="attempt")
        mine = re.compile(rf"^[a-z_]+:{task.id}(:|$)")
        for effect in db.scalars(select(AgentEffect).where(AgentEffect.user_id == user_id,
                                                           AgentEffect.created_at >= start,
                                                           AgentEffect.effect_key.like(f"%:{task.id}%"))
                                 .order_by(AgentEffect.id).limit(HISTORY_LIMIT)).all():
            if mine.match(effect.effect_key):
                title, tone = _step(effect.effect_key)
                add(effect.created_at, title, tone=tone, source="step")
        for intent in db.scalars(select(SubmissionIntent).where(SubmissionIntent.execution_task_id == task.id)
                                 .limit(20)).all():
            add(intent.created_at, "Reserved the posting for one submission (claim)", tone="brand", source="intent")
            add(intent.clicking_at, "Final click authorized", tone="brand", source="intent")
            add(intent.resolved_at, f"Submission {intent.status.replace('_', ' ').lower()}", detail=intent.reason or "",
                tone="good" if intent.status in ("CONFIRMED", "RESOLVED_SUBMITTED") else "warn", source="intent")
        if job_id is not None:
            for log in db.scalars(select(ExternalAPIRequestLog).where(
                    ExternalAPIRequestLog.user_id == user_id, ExternalAPIRequestLog.job_id == job_id,
                    ExternalAPIRequestLog.created_at >= start, ExternalAPIRequestLog.created_at <= end,
                    ExternalAPIRequestLog.provider.in_(("WEB", "BROWSER_USE")))
                    .order_by(ExternalAPIRequestLog.id).limit(HISTORY_LIMIT)).all():
                if log.provider == "WEB" and log.service != "application_browser":
                    continue
                what = f"Opened {log.endpoint_host or 'the page'}" if log.provider == "WEB" else \
                    f"Browser-use: {log.operation.replace('_', ' ')}"
                add(log.started_at, what, detail=" · ".join(filter(None, [
                    f"HTTP {log.http_status_code}" if log.http_status_code else "",
                    f"{log.latency_ms} ms" if log.latency_ms is not None else "",
                    (log.error_type or "").lower()])), tone="bad" if log.status == "FAILED" else "muted",
                    source="browser")
            for call in db.scalars(select(LLMRequestLog).where(
                    LLMRequestLog.user_id == user_id, LLMRequestLog.workflow_entity_id == job_id,
                    LLMRequestLog.worker_type == "BROWSER_NAVIGATOR", LLMRequestLog.created_at >= start,
                    LLMRequestLog.created_at <= end).order_by(LLMRequestLog.id).limit(HISTORY_LIMIT)).all():
                add(call.created_at, "Browser Navigator proposed a step",
                    detail=" · ".join(filter(None, [call.model or "", call.status.lower(),
                                                    f"{call.latency_ms} ms" if call.latency_ms else ""])),
                    source="model")
    if job_id is not None:
        for t in db.scalars(select(WorkflowTransition).where(WorkflowTransition.job_id == job_id,
                                                             WorkflowTransition.created_at >= start,
                                                             WorkflowTransition.created_at <= end)
                            .order_by(WorkflowTransition.id).limit(HISTORY_LIMIT)).all():
            add(t.created_at, f"Job moved {t.from_state or '—'} → {t.to_state}", detail=f"by {t.actor}",
                tone="brand", source="workflow")
        job = db.get(Job, job_id)
        if job is not None and job.application is not None:
            for e in db.scalars(select(ApplicationEvent).where(ApplicationEvent.application_id == job.application.id,
                                                               ApplicationEvent.created_at >= start,
                                                               ApplicationEvent.created_at <= end)
                                .order_by(ApplicationEvent.id).limit(HISTORY_LIMIT)).all():
                add(e.created_at, e.title or e.kind, detail=e.detail or "",
                    tone="warn" if e.kind == "blocker" else "muted", source="application")
    finished = end if end is not now else None
    out_traces = _trace_ids(db, user_id, spec, task, job_id, start, end)
    if finished is not None:
        add(finished, f"Ended: {view['status'].lower()}", detail=" · ".join(filter(None, [
            (getattr(task, "result_code", None) or "").replace("_", " ").lower(),
            view.get("last_error") or ""])), tone="bad" if view["status"] in STATUS_FILTERS["failed"] else "good",
            source="task")
    events.sort(key=lambda e: e["at"], reverse=True)
    out: dict[str, Any] = {"task": view, "events": events[:HISTORY_LIMIT], "trace_ids": out_traces,
                           "log_count": _log_count(db, user_id, out_traces)}
    if spec.kind == "application":
        from app.infrastructure.application_browser.browser_use_sidecar import configured
        from app.services import application_execution

        out["browser_use_ready"] = configured() and application_execution.available_mode(db) in ("observe", "assist",
                                                                                                  "guarded_auto")
    return out


TRACE_LIMIT = 8


def _trace_ids(db: Session, user_id: int, spec: WorkerKind, task: Any, job_id: int | None, start: datetime,
               end: datetime) -> list[str]:
    """The traces this task's runs wrote under, newest first, so its history can open exactly their Monitoring → Logs
    lines: its graph thread's, and those of the log lines, model calls and page loads made for its job while it ran.
    Bounded; metadata only."""
    from app.models import AgentThread, ExternalAPIRequestLog, LLMRequestLog, SystemLogEntry

    found: dict[str, datetime] = {}

    def take(rows: Any) -> None:
        for trace_id, at in rows:
            if trace_id and (trace_id not in found or (at and at > found[trace_id])):
                found[trace_id] = at or start

    thread_pk = getattr(task, "agent_thread_id", None)
    if thread_pk:
        take(db.execute(select(AgentThread.trace_id, AgentThread.updated_at).where(
            AgentThread.id == thread_pk, AgentThread.user_id == user_id)).all())
    if job_id is not None:
        take(db.execute(select(SystemLogEntry.trace_id, SystemLogEntry.created_at).where(
            SystemLogEntry.user_id == user_id, SystemLogEntry.entity_type == "job", SystemLogEntry.entity_id == job_id,
            SystemLogEntry.workflow == spec.kind, SystemLogEntry.created_at >= start, SystemLogEntry.created_at <= end)
            .order_by(SystemLogEntry.id.desc()).limit(50)).all())
        take(db.execute(select(LLMRequestLog.trace_id, LLMRequestLog.created_at).where(
            LLMRequestLog.user_id == user_id, LLMRequestLog.workflow_entity_id == job_id,
            LLMRequestLog.created_at >= start, LLMRequestLog.created_at <= end)
            .order_by(LLMRequestLog.id.desc()).limit(50)).all())
        if spec.kind == "application":
            take(db.execute(select(ExternalAPIRequestLog.trace_id, ExternalAPIRequestLog.created_at).where(
                ExternalAPIRequestLog.user_id == user_id, ExternalAPIRequestLog.job_id == job_id,
                ExternalAPIRequestLog.created_at >= start, ExternalAPIRequestLog.created_at <= end)
                .order_by(ExternalAPIRequestLog.id.desc()).limit(50)).all())
    return [t for t, _ in sorted(found.items(), key=lambda item: item[1], reverse=True)][:TRACE_LIMIT]


def _log_count(db: Session, user_id: int, trace_ids: list[str]) -> int:
    from app.models import SystemLogEntry

    if not trace_ids:
        return 0
    return db.scalar(select(func.count(SystemLogEntry.id)).where(SystemLogEntry.user_id == user_id,
                                                                   SystemLogEntry.trace_id.in_(trace_ids))) or 0
