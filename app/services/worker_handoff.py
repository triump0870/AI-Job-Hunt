"""Nudge forward (Running workers): move jobs that finished one stage but never reached the next.

Each worker normally hands a job on by itself: a recovered description queues a parse, a finished parse releases the
ranking, a qualified ranking queues tailoring, a reviewed packet may be approved by policy. A hand-off that was missed
(a crash between the two steps, a worker that was paused or switched off, a setting turned on later) leaves the job
parked between two stages. This module finds those jobs and gives them the push, through the same function the
worker itself uses, so there is no second workflow:

    Recovery  → Parsing    job_description.queue_parse
    Parsing   → Ranking    ranker.request_ranking (the hard filters and the domain filter still apply)
    Ranking   → Tailoring  auto_tailor.queue(manual=True)
    Tailoring → Reviewing  tailor_tasks.enqueue(mode="review"): a saved packet whose review never ran or failed
    Reviewing → Approval   workflow.approve_job, only where the account's own policy (automatic mode and the minimum
                           reviewer score) would have approved the packet already

Insights has no next stage. Rules a change has to keep (AGENTS.md → Running workers): only the account's own jobs;
a job a person stopped (`STOPPED_BY_USER`), removed from ranking, or already approved or applied is left alone; nothing
sets `run_now_at`, so no provider cooldown is bypassed; a job that cannot move is skipped with its reason and never
fails the batch; bounded. No model is called here, and no state is written except through the shared functions above.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, exists, func, select
from sqlalchemy.orm import Session

from app import scheduler_hooks
from app.models import (
    ACTIVE_JD_PARSE_STATUSES,
    Application,
    Job,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobRankingTask,
    ResumeVariant,
    TailorTask,
)
from app.services import auto_tailor, domain_match, job_description, ranker
from app.services.lifecycle import STOPPED_BY_USER
from app.services.lifecycle import WorkflowState as S
from app.services.user_settings import settings_for
from app.settings import get_settings

LIMIT = 200  # jobs looked at (and moved) per press


@dataclass(frozen=True)
class Stage:
    to: str  # what the job moves into, as the button says it
    find: Callable[[Session, int], list[int]]
    move: Callable[[Session, int, list[int]], tuple[list[int], list[dict[str, Any]]]]
    wake: tuple[str, ...]  # scheduler jobs / workers to nudge afterwards
    why_none: str  # shown when nothing is waiting


def _owned(user_id: int):
    return Job.user_id == user_id


# ───────────── Recovery → Parsing ─────────────

def _recovered_unparsed(db: Session, user_id: int) -> list[int]:
    """Jobs whose recovery finished with a description that has no parse task for its current text."""
    if not get_settings().jd_parse_enabled:
        return []
    recovered = select(JobDescriptionFetchTask.job_id).where(JobDescriptionFetchTask.user_id == user_id,
                                                             JobDescriptionFetchTask.status == "COMPLETED",
                                                             JobDescriptionFetchTask.result_code == "RECOVERED")
    stopped = exists().where(JobDescriptionParseTask.job_id == Job.id, JobDescriptionParseTask.last_error == STOPPED_BY_USER)
    jobs = db.scalars(select(Job).where(_owned(user_id), Job.id.in_(recovered), Job.description.is_not(None),
                                        Job.description != "", ~stopped,
                                        domain_match.not_held())
                      .order_by(Job.id).limit(LIMIT * 3)).all()
    active = set(db.scalars(select(JobDescriptionParseTask.job_id).where(
        JobDescriptionParseTask.user_id == user_id, JobDescriptionParseTask.status.in_(ACTIVE_JD_PARSE_STATUSES))).all())
    return [j.id for j in jobs if j.id not in active and job_description.needs_parse(j)][:LIMIT]


def _queue_parses(db: Session, user_id: int, ids: list[int]):
    moved, skipped = [], []
    for job in db.scalars(select(Job).where(_owned(user_id), Job.id.in_(ids))).all():
        if job_description.queue_parse(db, job, reason="user_request") is None:
            skipped.append({"job_id": job.id, "reason": "No description to parse"})
        else:
            moved.append(job.id)
    db.commit()
    return moved, skipped


# ───────────── Parsing → Ranking ─────────────

def _parsed_unranked(db: Session, user_id: int) -> list[int]:
    """Jobs whose parse is over (parsed, or given up on: ranking falls back to the raw text) but that are not in
    line for ranking, or whose ranking is parked behind a wait the parse already ended."""
    done = select(func.max(JobDescriptionParseTask.id)).where(JobDescriptionParseTask.user_id == user_id).group_by(
        JobDescriptionParseTask.job_id)
    finished = select(JobDescriptionParseTask.job_id).where(JobDescriptionParseTask.id.in_(done),
                                                            JobDescriptionParseTask.status.in_(("COMPLETED", "FAILED")),
                                                            JobDescriptionParseTask.last_error.is_distinct_from(STOPPED_BY_USER))
    stopped = exists().where(JobRankingTask.job_id == Job.id, JobRankingTask.last_error == STOPPED_BY_USER)
    waiting_states = (*ranker.PRE_RANK_STATES, S.RANK_PENDING)
    jobs = db.scalars(select(Job).where(_owned(user_id), Job.id.in_(finished), Job.state.in_(waiting_states),
                                        Job.fit_score.is_(None), ~stopped,
                                        domain_match.not_held())
                      .order_by(Job.id).limit(LIMIT * 3)).all()
    now = ranker.utcnow()
    parked = set(db.scalars(select(JobRankingTask.job_id).where(
        JobRankingTask.user_id == user_id, JobRankingTask.status == "PENDING",
        JobRankingTask.next_attempt_at.is_not(None), JobRankingTask.next_attempt_at > now,
        JobRankingTask.last_error.like(f"{ranker.PARKED_FOR_DESCRIPTION}%"))).all())
    active = set(db.scalars(select(JobRankingTask.job_id).where(
        JobRankingTask.user_id == user_id, JobRankingTask.status.in_(("PENDING", "PROCESSING")))).all())
    return [j.id for j in jobs if j.id in parked or j.id not in active][:LIMIT]


def _queue_rankings(db: Session, user_id: int, ids: list[int]):
    for job_id in ids:
        ranker.release_parked(db, job_id)
    out = ranker.request_ranking(db, ids, force=False, only_unranked=True, actor=f"user:{user_id}")
    skipped = [{"job_id": s["id"], "reason": s["reason"]} for s in out["skipped"]]
    skipped_ids = {s["job_id"] for s in skipped}
    # Jobs that already had a waiting task were only released from their wait: that is a move too.
    return [i for i in ids if i not in skipped_ids], skipped


# ───────────── Ranking → Tailoring ─────────────

def _qualified_untailored(db: Session, user_id: int) -> list[int]:
    return auto_tailor.waiting_job_ids(db, LIMIT)


def _queue_tailoring(db: Session, user_id: int, ids: list[int]):
    out = auto_tailor.queue(db, ids, manual=True)
    queued = [t["job_id"] for t in out["queued"]]
    skipped = [{"job_id": s["id"], "reason": s["reason"]} for s in out["skipped"]]
    skipped += [{"job_id": i, "reason": "Not queued: it changed while this ran"} for i in ids
                if i not in queued and i not in {s["job_id"] for s in skipped}]
    return queued, skipped


# ───────────── Tailoring → Reviewing ─────────────

def _unreviewed_packets(db: Session, user_id: int) -> list[int]:
    """Jobs still in the Tailor queue whose latest packet was saved but never reviewed (the review failed, or the run
    was interrupted between saving and reviewing), with no tailoring pass queued or running."""
    from app.services.workflow import SKIP_TAILORING_STATUSES

    latest = select(func.max(ResumeVariant.version)).where(ResumeVariant.job_id == Job.id).correlate(Job).scalar_subquery()
    decided = exists().where(Application.job_id == Job.id, Application.status.in_(("approved", "applied")))
    active = exists().where(TailorTask.job_id == Job.id, TailorTask.status.in_(("queued", "running")))
    stopped = exists().where(TailorTask.job_id == Job.id, TailorTask.error == STOPPED_BY_USER)
    return list(db.scalars(
        select(Job.id).join(ResumeVariant, (ResumeVariant.job_id == Job.id) & (ResumeVariant.version == latest))
        .where(_owned(user_id), Job.status.in_(SKIP_TAILORING_STATUSES), ResumeVariant.reviewer_score.is_(None),
               ~decided, ~active, ~stopped)
        .order_by(Job.id).limit(LIMIT)).all())


def _queue_reviews(db: Session, user_id: int, ids: list[int]):
    from app.services import tailor_tasks

    out = tailor_tasks.enqueue(db, ids, mode="review")
    return [t["job_id"] for t in out["queued"]], [{"job_id": s["id"], "reason": s["reason"]} for s in out["skipped"]]


# ───────────── Reviewing → Approval ─────────────

def _reviewed_unapproved(db: Session, user_id: int) -> list[int]:
    """Reviewed packets the account's own policy would approve (automatic mode, a stored PASS, minimum score met) that were not."""
    setting = settings_for(db, user_id)
    if setting.application_mode != "automatic":
        return []
    latest = select(func.max(ResumeVariant.version)).where(ResumeVariant.job_id == Job.id).correlate(Job).scalar_subquery()
    approved = exists().where(Application.job_id == Job.id, Application.status.in_(("approved", "applied")))
    stopped = exists().where(TailorTask.job_id == Job.id, TailorTask.error == STOPPED_BY_USER)
    rows = db.execute(
        select(Job.id, ResumeVariant.reviewer_score)
        .join(ResumeVariant, (ResumeVariant.job_id == Job.id) & (ResumeVariant.version == latest))
        # Only a stored PASS ("tailored"): a packet held for a person ("pending_approval": the reviewer asked for a human,
        # confidence was low or a candidate-policy filter failed) is never approved by a nudge, and neither is one whose
        # tailoring the person stopped.
        .where(_owned(user_id), Job.status == "tailored", ~approved, ~stopped,
               ResumeVariant.reviewer_score.is_not(None), ResumeVariant.reviewer_score >= setting.min_review_score)
        .order_by(desc(ResumeVariant.reviewer_score), Job.id).limit(LIMIT)).all()
    return [row[0] for row in rows]


def _approve(db: Session, user_id: int, ids: list[int]):
    from app.services.workflow import approve_job

    moved, skipped = [], []
    for job in db.scalars(select(Job).where(_owned(user_id), Job.id.in_(ids))).all():
        try:
            approve_job(db, job)
            moved.append(job.id)
        except ValueError as exc:
            db.rollback()
            skipped.append({"job_id": job.id, "reason": str(exc)})
    return moved, skipped


STAGES: dict[str, Stage] = {
    "recovery": Stage("Parsing", _recovered_unparsed, _queue_parses, ("jd_parsing",),
                      "Every recovered description is already parsed or waiting to be."),
    "parsing": Stage("Ranking", _parsed_unranked, _queue_rankings, ("ranking",),
                     "Every parsed job is already ranked or waiting to be."),
    "ranking": Stage("Tailoring", _qualified_untailored, _queue_tailoring, ("tailor",),
                     "Every qualified job is already tailored or queued, or automatic tailoring is off."),
    "tailoring": Stage("Reviewing", _unreviewed_packets, _queue_reviews, ("tailor",),
                       "Every saved packet has been reviewed or is being tailored."),
    "reviewing": Stage("Approval", _reviewed_unapproved, _approve, (),
                       "No reviewed packet is waiting for an approval your settings would give."),
}


def stage_of(kind: str) -> Stage | None:
    return STAGES.get(kind)


def waiting(db: Session, user_id: int, kind: str) -> int:
    """How many jobs Nudge forward would move now (0 for a worker with no next stage)."""
    stage = STAGES.get(kind)
    return len(stage.find(db, user_id)) if stage else 0


def next_stage(db: Session, user_id: int, kind: str) -> dict[str, Any] | None:
    """What the screen shows on the Nudge forward button: where jobs go next and how many are waiting for it."""
    stage = STAGES.get(kind)
    if stage is None:
        return None
    try:
        ids = stage.find(db, user_id)
        return {"to": stage.to, "waiting": len(ids), "job_ids": ids}  # the jobs a row's own Nudge button may move
    except Exception:  # noqa: BLE001  a button's count must never break the screen
        db.rollback()
        return {"to": stage.to, "waiting": 0, "job_ids": []}


def nudge_forward(db: Session, user_id: int, kind: str, job_ids: list[int] | None = None) -> dict[str, Any]:
    """Move this worker's finished-but-stuck jobs into the next stage. Commits. Never raises for one job.

    `job_ids` narrows it to those jobs (a row's own button, or the rows a filter shows); one that is not waiting for
    the next stage is skipped with that reason instead of being forced on."""
    stage = STAGES.get(kind)
    if stage is None:
        return {"kind": kind, "to": None, "moved": [], "skipped": [], "message": "This worker has no next stage"}
    ids = stage.find(db, user_id)
    declined: list[dict[str, Any]] = []
    if job_ids is not None:
        wanted = list(dict.fromkeys(job_ids))[:LIMIT]
        declined = [{"job_id": i, "reason": f"Not waiting for {stage.to}: already there, or not eligible"}
                    for i in wanted if i not in ids]
        ids = [i for i in ids if i in set(wanted)]
    if not ids:
        return {"kind": kind, "to": stage.to, "moved": [], "skipped": declined,
                "message": None if declined else stage.why_none}
    moved, skipped = stage.move(db, user_id, ids)
    skipped = [*skipped, *declined]
    db.commit()
    if moved:
        for name in stage.wake:
            scheduler_hooks.nudge(name)
    return {"kind": kind, "to": stage.to, "moved": moved, "skipped": skipped, "message": None}
