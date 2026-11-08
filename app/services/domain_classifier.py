"""Hybrid domain classification: unambiguous rules first, a fast model for the rest, and code has the last word.

    new job → triage (app.services.domain_match, search_scope)     no model
      HARD      → hidden, reason recorded
      POSITIVE  → goes on, no model call
      ASK       → job PENDING (held from every worker), task queued
    task (scheduler job domain_classifier, `run_live`)              async, bounded, lease-guarded
      → cache hit, or the DOMAIN_CLASSIFIER worker through the LLM gateway (tokens, cost, latency: its telemetry)
      → decide(): a NO_MATCH stands only if its evidence holds; otherwise it becomes UNCERTAIN with a gate code
      MATCH            → the existing path: resume, recovery, parsing, ranking (domain_match.forward)
      UNCERTAIN        → the same path when the title is the person's own and the job names their skills (settle);
                         otherwise it waits as REVIEW
                         for the person (Discovery → Review), and nothing is spent on it until they choose
      NO_MATCH         → hidden with its reason and evidence; Keep still works

Rules a change has to keep (AGENTS.md: Discovery quality):
* A failure never hides a job. No model configured, or the attempts used up: UNCERTAIN, and the job goes on.
* There is no confidence number anywhere. Evidence is what code can check: the quoted phrases are in the posting, no
  skill of the candidate is matched, and the title is not the candidate's own.
* One request per job. One posting's text can never influence another's verdict, and tokens and failures stay
  attributable to one job. The system prompt and candidate come first, so providers can reuse them as a prompt cache.
* Shadow mode never touches a job: rows here only, and model calls only within DOMAIN_CLASSIFIER_SHADOW_DAILY_LIMIT.
* Every row is the record of why a job was kept or hidden: metadata only, never prompt or posting text.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ACTIVE_JOB_CLASSIFICATION_STATUSES,
    Job,
    JobClassificationCache,
    JobClassificationTask,
    LLMRequestLog,
)
from app.prompts.contracts import DomainClassificationResult
from app.services import domain_match, system_log, worker_settings
from app.services.domain_match import (
    ASK,
    HARD,
    MATCH,
    NOT_MATCHING,
    PENDING,
    POSITIVE,
    Rules,
    Triage,
    Verdict,
)
from app.services.lifecycle import STOPPED_BY_USER
from app.services.lifecycle import WorkflowState as S
from app.settings import get_settings

log = logging.getLogger("jobhunt.domain_classifier")

OFF, SHADOW, ON = "off", "shadow", "on"
TASK_ON, TASK_SHADOW = "ON", "SHADOW"
PROCESSING, COMPLETED, FAILED, SKIPPED = "PROCESSING", "COMPLETED", "FAILED", "SKIPPED"
NO_MATCH, UNCERTAIN = "NO_MATCH", "UNCERTAIN"
ACTOR = "domain_classifier"
OUTCOMES = ("match", "uncertain", "review", "no_match", "retry", "lost", "skipped")
AI_REASON = "AI check"  # how a hidden job's reason starts, so a recheck can tell it from a rule's
WAITING_REASON = "Waiting for the AI check"
UNAVAILABLE = "CLASSIFIER_UNAVAILABLE"
FORWARD, REVIEW_JOB, HIDE = "FORWARD", "REVIEW", "HIDE"
PERSON_REASON = "You marked it as not matching"
RECHECK_LIMIT = 500
BULK_CHOICE = 3  # more jobs than this in one "not a match" click is a cleanup, not a judgement on each of them
MIN_QUOTE_CHARS = 8
QUOTE_FILLER = frozenset({"the", "and", "for", "with", "this", "that", "from", "you", "our", "are", "will", "work",
                          "role", "team", "job", "position"})
# Where a posting's substance starts, in the languages the boards send.
SUBSTANCE = re.compile(r"responsibilit|requirement|qualification|what you|you will|you'll|about the role|the role|"
                       r"your impact|aufgaben|anforderung|dein profil|ihr profil|missions|profil recherch", re.IGNORECASE)
HEAD_CHARS = 400
WINDOW_CHARS = 2000


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def mode() -> str:
    value = (get_settings().domain_classifier_mode or OFF).strip().lower()
    return value if value in (OFF, SHADOW, ON) else OFF


# ───────────── what the model reads ─────────────

def candidate(db: Session, user_id: int) -> dict[str, Any]:
    """The candidate as the classifier sees them: roles, skills, headline, years. No name, contact or resume text."""
    from app.models import CandidateProfile, Resume
    from app.services.resume_matching import groups
    from app.services.structured_resume import derive

    own = domain_match.derived(db, user_id)
    roles = list(own["roles"])
    years: Any = None
    for resume in db.scalars(select(Resume).where(Resume.user_id == user_id)).all():
        roles += [r.lower() for g in groups(resume) for r in g.get("roles") or [] if isinstance(r, str)]
        if resume.is_active and resume.structured_data:
            try:
                years = derive(resume.structured_data).get("total_experience_years")
            except Exception:  # noqa: BLE001  a half-parsed resume still leaves the roles and skills
                years = None
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user_id))
    # The same skills the gate protects (every resume's), so the model can report any of them.
    skills = list(dict.fromkeys([*own["skills"], *domain_match.resume_skills(db, user_id)]))
    return {"roles": list(dict.fromkeys(roles)), "skills": skills,
            "headline": (profile.headline if profile else "") or "", "years_experience": years}


def fingerprint(who: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({"roles": sorted(who.get("roles") or []), "skills": sorted(who.get("skills") or []),
                                      "headline": who.get("headline") or "", "years": who.get("years_experience")},
                                     sort_keys=True).encode()).hexdigest()


def posting_window(description: str | None, limit: int = WINDOW_CHARS) -> str:
    """The part of a posting that says what the work is: its opening, then from its first responsibilities or
    requirements heading on. Deterministic, so the same posting always costs the same and hits the same cache entry."""
    text = re.sub(r"[ \t]+", " ", (description or "")).strip()
    text = re.sub(r"\n\s*\n+", "\n", text)
    if len(text) <= limit:
        return text
    found = SUBSTANCE.search(text, HEAD_CHARS)
    if found is None:
        return text[:limit]
    return f"{text[:HEAD_CHARS]}\n…\n{text[found.start():found.start() + limit - HEAD_CHARS - 3]}"


def cache_key(who_fingerprint: str, version: str, title: str, company: str, window: str) -> str:
    norm = domain_match._norm
    return hashlib.sha256("\x1f".join((version, who_fingerprint, norm(title), norm(company), norm(window))).encode()).hexdigest()


# ───────────── the evidence gate ─────────────

@dataclass(frozen=True)
class Decision:
    """What code made of an answer. `gate_code` says why it is not what the model said."""

    classification: str
    reason: str
    matched_skills: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    gate_code: str | None = None
    job_skills: list[str] = field(default_factory=list)  # the candidate's skills the whole job names (code, not model)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


def _own_skill(name: str, skills: list[str]) -> bool:
    from app.services.resume_parsing import normalizes

    return any(domain_match._norm(name) == skill or normalizes(name, skill) or normalizes(skill, name)
               for skill in skills)


def _thin(quote: str) -> bool:
    """A quote that shows nothing: not a phrase (one word), too short, or only filler words ("and", "the team")."""
    squashed = _squash(quote)
    words = re.findall(r"[^\W\d_]{4,}", squashed)
    return (len(squashed) < MIN_QUOTE_CHARS or len(squashed.split()) < 2
            or not [w for w in words if w not in QUOTE_FILLER])


def decide(answer: DomainClassificationResult, *, title: str, posting: str, rules: Rules,
           full_text: str | None = None) -> Decision:
    """Reject only on sufficient evidence, and code decides what is sufficient. A NO_MATCH stands only when every
    phrase it quotes is in the text it was shown, none of the candidate's skills matched, the title is not the
    candidate's own, the text does not name several of their skills, and a bare title was not a technical one."""
    text = f"{title}\n{posting}"
    matched = [s for s in answer.matched_skills if domain_match.names_skill(text, s) and _own_skill(s, rules.skills)]
    found = domain_match.skills_found(full_text if full_text is not None else text, rules.skills)
    if answer.classification != NO_MATCH:
        return Decision(answer.classification, answer.reason, matched, job_skills=found)
    evidence = answer.irrelevance_evidence
    shown = _squash(text)
    code = None
    if not evidence:
        code = "NO_EVIDENCE"
    elif not all(len(_squash(p)) >= 3 and _squash(p) in shown for p in evidence):
        code = "EVIDENCE_NOT_IN_POSTING"
    elif any(_thin(p) for p in evidence):
        code = "EVIDENCE_TOO_THIN"
    elif matched:
        code = "SKILLS_MATCHED"
    elif domain_match.own_title(domain_match._norm(title), rules):
        code = "TITLE_PROTECTED"
    elif len(found) >= 2:
        code = "TEXT_NAMES_OWN_SKILLS"
    elif not posting.strip() and domain_match._technical(domain_match._norm(title)):
        code = "TITLE_ONLY_TECHNICAL"
    elif posting.strip() and not any(_squash(p) in _squash(posting) for p in evidence):
        code = "EVIDENCE_ONLY_IN_TITLE"  # a posting with a body: a title alone does not show the work is different
    if code:
        return Decision(UNCERTAIN, answer.reason, matched, list(evidence), code, found)
    return Decision(NO_MATCH, answer.reason, [], list(evidence), job_skills=found)


def job_skill_text(job: Any) -> str:
    """Where a job names its skills: its whole description (not only what the model was shown), and the requirements a
    parse found, when it has been parsed."""
    parts = [getattr(job, "title", "") or "", getattr(job, "description", "") or ""]
    structured = getattr(job, "structured_description", None) or {}
    for requirement in (structured.get("requirements") or []) if isinstance(structured, dict) else []:
        if isinstance(requirement, dict):
            parts.append(str(requirement.get("normalized_value") or requirement.get("source_text") or ""))
    return "\n".join(parts)


def settle(decision: Decision, *, title: str, skills: list[str], rules: Rules, title_only: bool) -> str:
    """FORWARD, REVIEW or HIDE. Deterministic: the model's answer, then the job's own skills and title against the
    person's. Ambiguity is the person's to settle, and costs nothing until they do. A model NO_MATCH that code doubts is
    always theirs: something points each way."""
    if decision.classification == NO_MATCH:
        return HIDE
    if decision.classification == MATCH or decision.gate_code == UNAVAILABLE:
        return FORWARD
    if decision.gate_code:
        return REVIEW_JOB
    if title_only:
        return FORWARD  # nothing to judge yet: recovery fetches the text, and recheck_recovered reads it again
    return FORWARD if domain_match.own_title(title, rules) and len(skills) >= 2 else REVIEW_JOB


def hidden_reason(decision: Decision) -> str:
    quoted = "; ".join(f"“{p}”" for p in decision.evidence[:2])
    return f"{AI_REASON}: {decision.reason}{f' ({quoted})' if quoted else ''}"[:300]


def kept_reason(decision: Decision) -> str:
    skills = f" Skills: {', '.join(decision.matched_skills[:6])}." if decision.matched_skills else ""
    word = "matches" if decision.classification == MATCH else "unclear, so it goes on"
    return f"{AI_REASON} ({word}): {decision.reason}{skills}"[:300]


# ───────────── routing a job ─────────────

def route(db: Session, job: Job, rules: Rules, search_run_id: int | None = None) -> Triage:
    """The rules' answer for this job, the search's own place check included. A job the ranker's hard filters will end
    anyway (an excluded company, the title keywords, the salary floor) never costs a model call: it goes on, and the
    ranker records it with its own reason."""
    from app.services import ranker, search_scope

    place, doubts = search_scope.hybrid(db, job, search_run_id, rules)
    triaged = domain_match.triage(job, rules, doubts)
    if triaged.route != HARD and place is not None:
        return Triage(HARD, place.code, place.reason, triaged.signals)
    if triaged.route == ASK and ranker.hard_filter_reason(job, ranker.policy_for(db, job.user_id)):
        return Triage(POSITIVE, MATCH, "", triaged.signals)
    return triaged


def _record(db: Session, job: Job, triaged: Triage, task_mode: str, reason: str, now: datetime, *,
            legacy: Verdict | None = None) -> JobClassificationTask:
    """A finished row for a decision the rules made alone: every job has its explanation in one place."""
    row = JobClassificationTask(
        user_id=job.user_id, job_id=job.id, mode=task_mode, status=COMPLETED, reason=reason[:40], route=triaged.route,
        signals=list(triaged.signals), title_only=not (job.description or "").strip(),
        classification=NO_MATCH if triaged.route == HARD else MATCH, explanation=(triaged.reason or None),
        matched_skills=[], irrelevance_evidence=[], attempt_count=0, created_at=now, completed_at=now,
        legacy_match=legacy.match if legacy else None, legacy_reason=(legacy.reason[:300] or None) if legacy else None)
    db.add(row)
    return row


def active_task(db: Session, job_id: int) -> JobClassificationTask | None:
    return db.scalar(select(JobClassificationTask).where(
        JobClassificationTask.job_id == job_id, JobClassificationTask.status.in_(ACTIVE_JOB_CLASSIFICATION_STATUSES)))


def enqueue(db: Session, job: Job, *, signals: tuple[str, ...] | list[str] = (), reason: str = "new",
            task_mode: str = TASK_ON, now: datetime | None = None, legacy: Verdict | None = None
            ) -> JobClassificationTask:
    """Queue one read of this job, or return its active task (never a second). Flushes in a savepoint, no commit."""
    existing = next((t for t in db.new if isinstance(t, JobClassificationTask) and t.job_id == job.id
                     and t.status in ACTIVE_JOB_CLASSIFICATION_STATUSES), None) or active_task(db, job.id)
    if existing is not None:
        return existing
    from app.prompts import DOMAIN_CLASSIFIER_PROMPT_VERSION

    task = JobClassificationTask(
        user_id=job.user_id, job_id=job.id, mode=task_mode, status="PENDING", reason=reason[:40], route=ASK,
        signals=list(signals), title_only=not (job.description or "").strip(),
        prompt_version=DOMAIN_CLASSIFIER_PROMPT_VERSION, matched_skills=[], irrelevance_evidence=[], attempt_count=0,
        created_at=now or utcnow(), legacy_match=legacy.match if legacy else None,
        legacy_reason=(legacy.reason[:300] or None) if legacy else None)
    try:
        with db.begin_nested():
            db.add(task)
            db.flush()
    except IntegrityError:  # another writer queued it at the same moment
        return active_task(db, job.id)  # type: ignore[return-value]
    return task


def budget_left(db: Session, user_id: int, now: datetime) -> int | None:
    """Model calls this account may still queue today in mode on; None when there is no limit."""
    limit = get_settings().domain_classifier_daily_limit
    if limit <= 0:
        return None
    used = db.scalar(select(func.count(JobClassificationTask.id)).where(
        JobClassificationTask.user_id == user_id, JobClassificationTask.mode == TASK_ON,
        JobClassificationTask.route == ASK, JobClassificationTask.status != SKIPPED,
        JobClassificationTask.created_at >= now - timedelta(days=1))) or 0
    return max(0, limit - used)


def _hold(db: Session, job: Job, signals: tuple[str, ...], reason: str, now: datetime) -> None:
    """Held from every worker. Over the day's budget it queues no call: `sweep` queues it as the count falls, so the job
    waits unhidden and unranked rather than being forwarded unread."""
    job.domain_match, job.domain_reason, job.domain_checked_at = PENDING, WAITING_REASON, now
    room = budget_left(db, job.user_id, now)
    if room is None or room > 0:
        enqueue(db, job, signals=signals, reason=reason, now=now)


def screen(db: Session, job: Job, rules: Rules, *, actor: str, now: datetime | None = None,
           search_run_id: int | None = None) -> bool:
    """Mode on: settle a job the rules can, hold the rest for the classifier. True when it may carry on now."""
    now = now or utcnow()
    triaged = route(db, job, rules, search_run_id)
    if triaged.route == HARD:
        _record(db, job, triaged, TASK_ON, "new", now)
        domain_match.hide(db, job, Verdict(False, triaged.code, triaged.reason), actor=actor, now=now)
        return False
    if triaged.route == POSITIVE:
        _record(db, job, triaged, TASK_ON, "new", now)
        domain_match.mark(job, Verdict(True, MATCH, triaged.reason), now)
        return True
    _hold(db, job, triaged.signals, "new", now)
    return False


def _legacy(db: Session, job: Job, rules: Rules, search_run_id: int | None) -> Verdict:
    from app.services import search_scope

    verdict = domain_match.assess(job, rules)
    if verdict.match:
        scoped = search_scope.verdict_for(db, job, search_run_id)
        if scoped is not None and not scoped.match:
            return scoped
    return verdict


def _shadow_room(db: Session, user_id: int, now: datetime) -> bool:
    limit = get_settings().domain_classifier_shadow_daily_limit
    if limit <= 0:
        return False
    used = db.scalar(select(func.count(JobClassificationTask.id)).where(
        JobClassificationTask.user_id == user_id, JobClassificationTask.mode == TASK_SHADOW,
        JobClassificationTask.route == ASK, JobClassificationTask.status != SKIPPED,  # a skipped row reserved no call
        JobClassificationTask.created_at >= now - timedelta(days=1))) or 0
    return used < limit


def observe(db: Session, job: Job, search_run_id: int | None, rules_cache: dict[int, Rules] | None = None) -> None:
    """Mode shadow: record what the hybrid would do beside what the old filter says. The job is never touched, and a
    failure here never changes an import."""
    if mode() != SHADOW:
        return
    try:
        with db.begin_nested():
            now = utcnow()
            cache = rules_cache if rules_cache is not None else {}
            rules = cache.setdefault(job.user_id, domain_match.rules_for(db, job.user_id))
            triaged, legacy = route(db, job, rules, search_run_id), _legacy(db, job, rules, search_run_id)
            if triaged.route != ASK:
                _record(db, job, triaged, TASK_SHADOW, "new", now, legacy=legacy)
            elif _shadow_room(db, job.user_id, now):
                enqueue(db, job, signals=triaged.signals, task_mode=TASK_SHADOW, now=now, legacy=legacy)
            else:
                row = _record(db, job, triaged, TASK_SHADOW, "new", now, legacy=legacy)
                row.status, row.classification, row.gate_code = SKIPPED, None, "NO_SHADOW_BUDGET"
            db.flush()
    except Exception:  # telemetry must not change an outcome
        log.warning("could not record a shadow classification", exc_info=True)


# ───────────── the queue ─────────────

@dataclass(frozen=True)
class ClassifyClaim:
    task_id: int
    lock_token: str
    worker_id: str
    run_now: bool = False


def claim(db: Session, *, worker_id: str, limit: int, now: datetime | None = None,
          lease_seconds: int | None = None) -> list[ClassifyClaim]:
    """Lease up to `limit` due tasks (FOR UPDATE SKIP LOCKED on PostgreSQL). Commits."""
    now = now or utcnow()
    lease = timedelta(seconds=lease_seconds or get_settings().domain_classifier_lease_seconds)
    m = JobClassificationTask
    query = (select(m).where(m.status == "PENDING", or_(m.next_attempt_at.is_(None), m.next_attempt_at <= now))
             .order_by(m.run_now_at.is_(None), m.next_attempt_at.asc().nulls_first(), m.id).limit(max(1, limit)))
    stopped = worker_settings.paused(db, "classification")
    if stopped:
        query = query.where(or_(m.user_id.not_in(stopped), m.run_now_at.is_not(None)))
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True, of=m)
    claimed = []
    for task in db.scalars(query.execution_options(populate_existing=True)).all():
        task.status, task.locked_by = PROCESSING, worker_id[:200]
        task.lock_token, task.lock_expires_at = uuid.uuid4().hex, now + lease
        task.attempt_count += 1
        task.started_at, task.next_attempt_at = now, None
        run_now, task.run_now_at = task.run_now_at is not None, None
        claimed.append(ClassifyClaim(task.id, task.lock_token, worker_id[:200], run_now))
    db.commit()
    return claimed


def _clear_lease(task: JobClassificationTask) -> None:
    task.locked_by = task.lock_token = task.lock_expires_at = None


def _held(db: Session, c: ClassifyClaim) -> JobClassificationTask | None:
    """The task, only while this worker still holds its lease."""
    query = select(JobClassificationTask).where(JobClassificationTask.id == c.task_id,
                                                JobClassificationTask.status == PROCESSING,
                                                JobClassificationTask.lock_token == c.lock_token)
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    return db.scalar(query.execution_options(populate_existing=True))


def _lock_job(db: Session, job_id: int) -> Job | None:
    query = select(Job).where(Job.id == job_id)
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    return db.scalar(query.execution_options(populate_existing=True))


def recover_expired(db: Session, now: datetime | None = None) -> int:
    """Tasks whose worker died go back in the queue; with no attempts left the job goes on unhidden. Commits."""
    now = now or utcnow()
    rows = db.scalars(select(JobClassificationTask).where(JobClassificationTask.status == PROCESSING,
                                                          JobClassificationTask.lock_expires_at < now)).all()
    message = "Interrupted: the worker stopped while reading this job"
    for task in rows:
        db.info["user_id"] = task.user_id
        _clear_lease(task)
        if task.attempt_count >= worker_settings.get(db, task.user_id, "classification", "max_attempts"):
            _open(db, task, db.get(Job, task.job_id), message, now)
        else:
            task.status, task.last_error = "PENDING", message
    db.commit()
    return len(rows)


def settle_by_old_filter(db: Session, job: Job, now: datetime, why: str) -> None:
    """The AI check will not read this job (it was turned off): the old filter decides, exactly as it does with the mode
    off. A job it would hide is hidden; only one it keeps goes on."""
    verdict = _legacy(db, job, domain_match.rules_for(db, job.user_id, wide=False), job.search_run_id)
    if verdict.match:
        domain_match.mark(job, verdict, now)
        domain_match.forward(db, job, actor=ACTOR, now=now, reason=why)
    else:
        domain_match.hide(db, job, verdict, actor=ACTOR, now=now)


def sweep(db: Session, limit: int = 200, now: datetime | None = None) -> int:
    """Housekeeping for held jobs. Commits. In mode on: a job left PENDING with no task is queued (within the day's
    budget, unless a person stopped its check), and a job waiting for review longer than DOMAIN_CLASSIFIER_REVIEW_DAYS goes
    on. In any other mode nothing will read them: the old filter decides, PENDING and REVIEW alike."""
    now = now or utcnow()
    active = select(JobClassificationTask.job_id).where(
        JobClassificationTask.status.in_(ACTIVE_JOB_CLASSIFICATION_STATUSES))
    stopped = select(JobClassificationTask.job_id).where(JobClassificationTask.last_error == STOPPED_BY_USER)
    jobs = db.scalars(select(Job).where(Job.domain_match == PENDING, Job.user_id.is_not(None), Job.id.not_in(active),
                                        Job.id.not_in(stopped)).order_by(Job.id).limit(max(1, limit))).all()
    on, room, count = mode() == ON, {}, len(jobs)
    for job in jobs:
        if not on:
            settle_by_old_filter(db, job, now, "The AI check was turned off")
            continue
        if job.user_id not in room:
            room[job.user_id] = budget_left(db, job.user_id, now)
        if room[job.user_id] is not None and room[job.user_id] <= 0:
            count -= 1  # over today's budget: it waits, held and unhidden
            continue
        if room[job.user_id] is not None:
            room[job.user_id] -= 1
        enqueue(db, job, reason="sweep", now=now)
    days = get_settings().domain_classifier_review_days
    waiting = select(Job).where(Job.domain_match == domain_match.REVIEW, Job.user_id.is_not(None))
    if not on:
        stale = db.scalars(waiting.order_by(Job.id).limit(max(1, limit))).all()
    elif days > 0:
        stale = db.scalars(waiting.where(Job.domain_checked_at < now - timedelta(days=days))
                           .order_by(Job.id).limit(max(1, limit))).all()
    else:
        stale = []
    for job in stale:
        if on:
            _release(db, job, "Review timed out: it goes on like any uncertain job", now)
        else:
            settle_by_old_filter(db, job, now, "The AI check was turned off")
    db.commit()
    return count + len(stale)


# ───────────── settling ─────────────

def _release(db: Session, job: Job, reason: str, now: datetime) -> None:
    """The job goes on. One that has been through ranking already only loses its hidden mark."""
    job.domain_match, job.domain_reason, job.domain_checked_at = MATCH, reason[:300] or None, now
    from app.services import ranker

    if job.state is S.SKIPPED or job.state in ranker.PRE_RANK_STATES:
        domain_match.forward(db, job, actor=ACTOR, now=now, reason="The AI check let it through")


def _write(task: JobClassificationTask, decision: Decision, raw: str | None, *, cache_hit: bool, now: datetime,
           status: str = COMPLETED, error: str | None = None) -> None:
    _clear_lease(task)
    task.status, task.completed_at, task.next_attempt_at = status, now, None
    task.model_classification, task.classification = raw, decision.classification
    task.explanation, task.gate_code = (decision.reason or "")[:300] or None, decision.gate_code
    task.matched_skills = list(dict.fromkeys([*decision.matched_skills, *decision.job_skills]))[:20]
    task.irrelevance_evidence = decision.evidence
    task.cache_hit, task.last_error = cache_hit, error


def _apply(db: Session, task: JobClassificationTask, job: Job | None, decision: Decision, now: datetime,
           rules: Rules | None = None) -> str:
    """Move the job, and return what happened to it. Only an ON task moves one, and only while the job is still waiting
    for it: a person may have kept it."""
    action = FORWARD if rules is None or job is None else settle(
        decision, title=job.title or "", skills=decision.job_skills, rules=rules, title_only=bool(task.title_only))
    if task.mode != TASK_ON or job is None or job.domain_match != PENDING:
        return action
    if action == HIDE:
        domain_match.hide(db, job, Verdict(False, "CLASSIFIER", hidden_reason(decision)), actor=ACTOR, now=now)
    elif action == REVIEW_JOB:
        job.domain_match, job.domain_checked_at = domain_match.REVIEW, now
        job.domain_reason = f"Needs your review: {decision.reason}"[:300]
    else:
        _release(db, job, kept_reason(decision), now)
    return action


def _open(db: Session, task: JobClassificationTask, job: Job | None, why: str, now: datetime) -> None:
    """Fail open: the check could not be done, and that is no evidence against the job."""
    decision = Decision(UNCERTAIN, "The AI check could not run, so the job goes on", gate_code=UNAVAILABLE)
    _write(task, decision, None, cache_hit=False, now=now, error=why[:1000])
    _apply(db, task, job, decision, now)


def _cached(db: Session, user_id: int, key: str, now: datetime) -> dict[str, Any] | None:
    row = db.scalar(select(JobClassificationCache).where(
        JobClassificationCache.user_id == user_id, JobClassificationCache.cache_key == key,
        JobClassificationCache.expires_at > now))
    if row is None:
        return None
    row.hits += 1
    return dict(row.answer or {})


def _remember(db: Session, user_id: int, key: str, answer: DomainClassificationResult, version: str,
              model: str | None, now: datetime) -> None:
    days = max(1, get_settings().domain_classifier_cache_days)
    row = db.scalar(select(JobClassificationCache).where(JobClassificationCache.user_id == user_id,
                                                         JobClassificationCache.cache_key == key))
    if row is not None:
        row.answer, row.model, row.expires_at = answer.model_dump(), (model or "")[:300] or None, now + timedelta(days=days)
        return
    try:
        with db.begin_nested():
            db.add(JobClassificationCache(user_id=user_id, cache_key=key, answer=answer.model_dump(),
                                          prompt_version=version, model=(model or "")[:300] or None, hits=0,
                                          created_at=now, expires_at=now + timedelta(days=days)))
            db.flush()
    except IntegrityError:  # the same posting answered at the same moment: either answer serves
        pass


def _retry(db: Session, c: ClassifyClaim, exc: Exception, now: datetime, *, give_back: float | None = None) -> str:
    """Another attempt later, or fail open. `give_back`: a request held before it was sent keeps its attempt."""
    db.rollback()
    task = _held(db, c)
    if task is None:
        return "lost"
    db.info["user_id"] = task.user_id
    message = f"{type(exc).__name__}: {exc}"[:1000]
    own = worker_settings.for_user(db, task.user_id, "classification")
    if give_back is None and task.attempt_count >= own["max_attempts"]:
        job = _lock_job(db, task.job_id)
        _open(db, task, job, message, now)
        db.commit()
        _log_decision(task, job)
        return "uncertain"
    _clear_lease(task)
    task.status, task.last_error = "PENDING", message
    if give_back is not None:
        task.attempt_count = max(0, task.attempt_count - 1)
    wait = give_back if give_back is not None else own["retry_seconds"] * max(1, task.attempt_count)
    task.next_attempt_at = now + timedelta(seconds=wait)
    db.commit()
    return "retry"


def _log_decision(task: JobClassificationTask, job: Job | None) -> None:
    """A Monitoring line for what a person would want to see: a job hidden, or a check that could not run."""
    if task.mode != TASK_ON or (task.classification != NO_MATCH and task.gate_code != UNAVAILABLE):
        return
    hidden = task.classification == NO_MATCH
    system_log.log_event(
        category="WORKFLOW", level="INFO" if hidden else "WARNING", operation=ACTOR, workflow=ACTOR, entity_type="job",
        entity_id=task.job_id, user_id=task.user_id, status=task.classification,
        message=(job.domain_reason if hidden and job is not None else "The AI check could not run; the job went on") or "",
        details={"signals": list(task.signals or [])[:10], "gate_code": task.gate_code, "cache_hit": task.cache_hit})


def process(sessions: Any, c: ClassifyClaim) -> str:
    """Read one claimed job. Never raises (`run_live`). The model is called with no row locks held, and everything
    written after it is guarded by this claim's lease."""
    try:
        return _process(sessions, c)
    except Exception as exc:  # one bad posting never stops the worker
        log.warning("domain classification failed", exc_info=True)
        with sessions() as db:
            return _retry(db, c, exc, utcnow())


def _process(sessions: Any, c: ClassifyClaim) -> str:
    from app.prompts import DOMAIN_CLASSIFIER_PROMPT_VERSION, build_domain_classifier_prompt
    from app.services import backoff, tracing
    from app.services.llm import LLMCall, LLMClient, LLMConfigError, LLMRateLimitError
    from app.services.rate_limits import TOO_LARGE
    from app.services.user_settings import ai_ready

    with sessions() as db:
        task, now = _held(db, c), utcnow()
        if task is None:
            return "lost"
        user_id, job_id, task_mode = task.user_id, task.job_id, task.mode
        db.info["user_id"] = user_id
        job = db.get(Job, job_id)
        if job is None or (task_mode == TASK_ON and job.domain_match != PENDING):
            _clear_lease(task)
            task.status, task.completed_at = SKIPPED, now
            task.last_error = "The job was deleted" if job is None else "Decided while it waited"
            db.commit()
            return "skipped"
        if mode() != (ON if task_mode == TASK_ON else SHADOW):  # turned off (or switched) while it waited
            _clear_lease(task)
            task.status, task.completed_at, task.last_error = SKIPPED, now, "The AI check was turned off"
            if task_mode == TASK_ON and job.domain_match == PENDING:
                settle_by_old_filter(db, job, now, "The AI check was turned off")
            db.commit()
            return "skipped"
        ready, why = ai_ready(db, user_id)
        if not ready:
            _open(db, task, job, why, now)
            db.commit()
            _log_decision(task, job)
            return "uncertain"
        title, company = job.title or "", job.company or ""
        window, skill_text = posting_window(job.description), job_skill_text(job)
        task.title_only = not window
        rules, who = domain_match.rules_for(db, user_id), candidate(db, user_id)
        key = cache_key(fingerprint(who), DOMAIN_CLASSIFIER_PROMPT_VERSION, title, company, window)
        stored = _cached(db, user_id, key, now)
        attempt = task.attempt_count
        db.commit()  # no transaction stays open across a model call

        answer: DomainClassificationResult | None = None
        if stored:
            try:
                answer = DomainClassificationResult.model_validate(stored)
            except ValueError:
                stored = None  # an entry an older contract wrote: ask again
        cache_hit, model = answer is not None, None
        if answer is None:
            prompt = build_domain_classifier_prompt(who, SimpleNamespace(title=title, company=company), window)
            try:
                with backoff.bypass(c.run_now), tracing.trace(job_id=job_id, user_id=user_id):
                    result = LLMClient.for_user(db, user_id).generate_prompt(prompt, LLMCall(
                        workflow_type="job", entity_id=job_id, attempt=attempt,
                        validate=DomainClassificationResult.model_validate))
            except LLMConfigError as exc:
                db.rollback()
                task, job = _held(db, c), _lock_job(db, job_id)
                if task is None:
                    return "lost"
                _open(db, task, job, f"{type(exc).__name__}: {exc}", utcnow())
                db.commit()
                _log_decision(task, job)
                return "uncertain"
            except LLMRateLimitError as exc:
                limit = getattr(exc, "rate_limit", None)
                if getattr(limit, "kind", None) == TOO_LARGE:
                    return _retry(db, c, exc, utcnow())
                return _retry(db, c, exc, utcnow(), give_back=exc.held_seconds)
            answer, model = result.validated, result.model

        decision = decide(answer, title=title, posting=window, rules=rules, full_text=skill_text)
        now = utcnow()
        task, job = _held(db, c), _lock_job(db, job_id)
        if task is None:
            db.rollback()
            return "lost"  # stopped, or the lease expired and another worker took it over: nothing is written
        if not cache_hit:
            _remember(db, user_id, key, answer, DOMAIN_CLASSIFIER_PROMPT_VERSION, model, now)
        _write(task, decision, answer.classification, cache_hit=cache_hit, now=now)
        action = _apply(db, task, job, decision, now, rules)
        db.commit()
        _log_decision(task, job)
        if action == REVIEW_JOB:
            return "review"
        return {MATCH: "match", UNCERTAIN: "uncertain", NO_MATCH: "no_match"}[decision.classification]


# ───────────── the person settles what stayed ambiguous ─────────────

def review_jobs(db: Session, user_id: int, limit: int = 200) -> dict[str, Any]:
    """Discovery → Review: jobs waiting for the person, with what code found on each side. Newest first, bounded."""
    query = select(Job).where(Job.user_id == user_id, Job.domain_match == domain_match.REVIEW)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    jobs = db.scalars(query.order_by(Job.id.desc()).limit(max(1, limit))).all()
    m = JobClassificationTask
    latest: dict[int, JobClassificationTask] = {}
    if jobs:
        for task in db.scalars(select(m).where(m.job_id.in_([j.id for j in jobs]), m.mode == TASK_ON).order_by(m.id)).all():
            latest[task.job_id] = task
    rows = []
    for job in jobs:
        task = latest.get(job.id)
        rows.append({"id": job.id, "title": job.title, "company": job.company, "location": job.location,
                     "url": job.url, "reason": task.explanation if task else job.domain_reason,
                     "your_skills_found": list(task.matched_skills or []) if task else [],
                     "points_against": list(task.irrelevance_evidence or []) if task else [],
                     "model_said": task.model_classification if task else None,
                     "summary": (job.description or "")[:320]})
    return {"total": total, "jobs": rows}


def resolve_review(db: Session, user_id: int, ids: list[int], matches: bool, *, actor: str) -> dict[str, Any]:
    """The person's choice, which wins: a match goes on like a job they kept and is never assessed again; anything else
    is hidden as their decision, and Keep still brings it back. Both teach the filter. Does not commit."""
    from app.services import feedback_learning

    now, done, skipped = utcnow(), [], []
    for job in db.scalars(select(Job).where(Job.user_id == user_id, Job.id.in_(ids))).all():
        if job.domain_match != domain_match.REVIEW:
            skipped.append({"id": job.id, "reason": "This job is not waiting for your review"})
            continue
        if matches or len(ids) <= BULK_CHOICE:  # a big "not a match" click is a cleanup: it hides, and teaches nothing
            feedback_learning.record(db, job, "KEPT" if matches else "DISMISSED")
        if matches:
            domain_match.keep(db, job, actor=actor, now=now)
        else:
            domain_match.hide(db, job, Verdict(False, "PERSON", PERSON_REASON), actor=actor, now=now)
        done.append(job.id)
    if done and (matches or len(ids) <= BULK_CHOICE):
        feedback_learning.after_feedback(db, user_id)
    return {"done": done, "skipped": skipped, "matches": matches}


# ───────────── asking again ─────────────

def requeue(db: Session, job: Job, *, previous: JobClassificationTask, now: datetime | None = None
            ) -> JobClassificationTask:
    """Retry and Run now: a new read of the job, in the same mode as the one that failed or was stopped."""
    now = now or utcnow()
    if previous.mode == TASK_ON and job.domain_match == PENDING:
        job.domain_reason, job.domain_checked_at = WAITING_REASON, now
    return enqueue(db, job, signals=list(previous.signals or []), reason="user_request", task_mode=previous.mode, now=now)


def recheck_recovered(db: Session, job: Job, now: datetime) -> bool:
    """A description just arrived for a job the classifier could only read by its title and left UNCERTAIN: read it
    once more before anything is spent on parsing and ranking. True when it now waits for that read."""
    if mode() != ON or job.domain_match == domain_match.KEPT:
        return False
    latest = db.scalar(select(JobClassificationTask).where(JobClassificationTask.job_id == job.id,
                                                           JobClassificationTask.mode == TASK_ON)
                       .order_by(JobClassificationTask.id.desc()).limit(1))
    if latest is None or latest.route != ASK or not latest.title_only or latest.classification != UNCERTAIN \
            or latest.gate_code == UNAVAILABLE:
        return False
    job.domain_match, job.domain_reason, job.domain_checked_at = PENDING, WAITING_REASON, now
    enqueue(db, job, signals=list(latest.signals or []), reason="description_changed", now=now)
    return True


def _relearn(db: Session, user_id: int) -> None:
    from app.services import feedback_learning

    try:
        with db.begin_nested():
            feedback_learning.learn(db, user_id)
    except Exception:  # stale learned rules only send more jobs to the model
        log.warning("could not relearn before a recheck", exc_info=True)


def recheck(db: Session, user_id: int, *, actor: str, limit: int = RECHECK_LIMIT) -> dict[str, int]:
    """The person asks: put jobs hidden by a rule through the hybrid pipeline. It queues model calls, so it never runs by
    itself. Jobs they kept, jobs in another place than their search asked for, and jobs the classifier itself hid stay
    as they are. Does not commit."""
    from app.services import search_scope

    if mode() != ON:
        return {"checked": 0, "queued": 0, "restored": 0, "still_hidden": 0}
    _relearn(db, user_id)
    db.flush()
    rules, now = domain_match.rules_for(db, user_id), utcnow()
    untouched = (AI_REASON, PERSON_REASON, search_scope.LOCATION_REASON, search_scope.ONSITE_REASON)
    # Excluded in SQL, before the limit: newer jobs that stay as they are must not crowd out older ones that can change.
    jobs = db.scalars(select(Job).where(Job.user_id == user_id, Job.domain_match == NOT_MATCHING,
                                        *(or_(Job.domain_reason.is_(None), ~Job.domain_reason.startswith(p, autoescape=True))
                                          for p in untouched))
                      .order_by(Job.id.desc()).limit(max(1, limit))).all()
    queued = restored = still = 0
    for job in jobs:
        before = job.domain_reason
        job.domain_match = None  # the ranker's filters below must not read the old verdict
        triaged = route(db, job, rules)
        if triaged.route == HARD:
            job.domain_match, job.domain_reason = NOT_MATCHING, (triaged.reason or before or "")[:300] or None
            still += 1
        elif triaged.route == POSITIVE:
            _record(db, job, triaged, TASK_ON, "recheck", now)
            _release(db, job, triaged.reason, now)
            restored += 1
        else:
            _hold(db, job, triaged.signals, "recheck", now)
            queued += 1
    return {"checked": len(jobs), "queued": queued, "restored": restored, "still_hidden": still}


def reapply(db: Session, user_id: int, *, actor: str) -> dict[str, int]:
    """Mode on: the person changed their lists. Visible jobs not ranked yet that a list now hides are hidden; hidden
    ones go through `recheck`. Does not commit."""
    rules, now = domain_match.rules_for(db, user_id), utcnow()
    jobs = db.scalars(select(Job).where(Job.user_id == user_id, Job.domain_match == MATCH,
                                        Job.state.in_((S.DISCOVERED, S.NORMALIZED, S.FILTERED, S.RANK_PENDING)))
                      .limit(RECHECK_LIMIT)).all()
    hidden = 0
    for job in jobs:
        triaged = route(db, job, rules)
        if triaged.route == HARD:
            domain_match.hide(db, job, Verdict(False, triaged.code, triaged.reason), actor=actor, now=now)
            hidden += 1
    again = recheck(db, user_id, actor=actor)
    return {"hidden": hidden, "restored": again["restored"], "queued": again["queued"],
            "checked": len(jobs) + again["checked"]}


def purge_cache(db: Session, now: datetime | None = None) -> int:
    """Cleanup job: expired answers. Commits."""
    rows = db.scalars(select(JobClassificationCache).where(JobClassificationCache.expires_at <= (now or utcnow()))).all()
    for row in rows:
        db.delete(row)
    db.commit()
    return len(rows)


# ───────────── reading ─────────────

GATE_ROW_LIMIT = 5000
GATE_EXAMPLES = 3


def _where_it_stands(mode: str, state: str | None, reason: str | None) -> str:
    """Where a job the gate held stands today. Only a person's own choices count as kept or removed."""
    if mode == TASK_SHADOW:
        return "other"  # shadow verdicts moved nothing, so today's state says nothing about them
    if state == domain_match.REVIEW:
        return "waiting"
    if state == domain_match.KEPT:
        return "kept"
    if state == domain_match.NOT_MATCHING:
        return "removed" if reason == PERSON_REASON else "other"
    return "moved_on" if state == domain_match.MATCH or state is None else "other"


def gate_review(db: Session, user_id: int, start: datetime, end: datetime) -> dict[str, Any]:
    """The model's NO_MATCH answers in the range: which ones the gate stopped, why, and where those jobs stand now. Each
    job counts once, by its latest finished read, so a re-check never counts it twice. A deleted job's task goes with it.
    Bounded, and read from the task rows only."""
    m = JobClassificationTask
    rows = db.execute(select(m.job_id, m.gate_code, m.model_classification, m.classification, m.explanation,
                             m.irrelevance_evidence, m.mode, Job.title, Job.company, Job.domain_match, Job.domain_reason)
                      .join(Job, Job.id == m.job_id)
                      .where(m.user_id == user_id, m.created_at >= start, m.created_at <= end,
                             m.classification.is_not(None))
                      .order_by(m.id.desc()).limit(GATE_ROW_LIMIT + 1)).all()
    truncated = len(rows) > GATE_ROW_LIMIT
    seen: set[int] = set()
    codes: dict[str, dict[str, Any]] = {}
    stood = unavailable = 0
    for row in rows[:GATE_ROW_LIMIT]:
        if row.job_id in seen:
            continue
        seen.add(row.job_id)
        if row.gate_code == UNAVAILABLE:
            unavailable += 1
        elif row.classification == NO_MATCH:
            stood += 1
        elif row.gate_code and row.model_classification == NO_MATCH:
            stands = _where_it_stands(row.mode, row.domain_match, row.domain_reason)
            entry = codes.setdefault(row.gate_code, {
                "code": row.gate_code, "jobs": 0, "examples": [],
                "now": {"waiting": 0, "kept": 0, "removed": 0, "moved_on": 0, "other": 0}})
            entry["jobs"] += 1
            entry["now"][stands] += 1
            if len(entry["examples"]) < GATE_EXAMPLES:
                entry["examples"].append({"job_id": row.job_id, "title": row.title, "company": row.company, "now": stands,
                                          "reason": row.explanation, "evidence": list(row.irrelevance_evidence or [])[:3]})
    stopped = sum(entry["jobs"] for entry in codes.values())
    return {"no_match": stopped + stood, "stopped": stopped, "stood": stood, "unavailable": unavailable,
            "truncated": truncated, "codes": sorted(codes.values(), key=lambda e: (-e["jobs"], e["code"]))}


def metrics(db: Session, user_id: int, start: datetime, end: datetime) -> dict[str, Any]:
    """Call rate, cache hits, what the gate changed and what it cost. Tokens and cost are the gateway's own rows, read
    here and never counted twice; a cost nobody reported is None, not 0."""
    m = JobClassificationTask
    where = (m.user_id == user_id, m.created_at >= start, m.created_at <= end)
    rows = db.execute(select(m.route, m.classification, m.gate_code, m.cache_hit, m.mode, func.count())
                      .where(*where).group_by(m.route, m.classification, m.gate_code, m.cache_hit, m.mode)).all()
    routes: dict[str, int] = {}
    classes: dict[str, int] = {}
    gates: dict[str, int] = {}
    cached = asked = 0
    for route_name, classification, gate, hit, _mode, n in rows:
        routes[route_name] = routes.get(route_name, 0) + n
        if route_name == ASK:
            asked += n
            cached += n if hit else 0
            if classification:
                classes[classification] = classes.get(classification, 0) + n
        if gate:
            gates[gate] = gates.get(gate, 0) + n
    total = sum(routes.values())
    calls = db.execute(select(LLMRequestLog.input_tokens, LLMRequestLog.output_tokens, LLMRequestLog.estimated_cost,
                              LLMRequestLog.latency_ms, LLMRequestLog.status)
                       .where(LLMRequestLog.user_id == user_id, LLMRequestLog.stage == "DOMAIN_CLASSIFIER",
                              LLMRequestLog.created_at >= start, LLMRequestLog.created_at <= end)
                       .order_by(LLMRequestLog.id.desc()).limit(5000)).all()
    latencies = sorted(c.latency_ms for c in calls if c.latency_ms is not None)
    costs = [float(c.estimated_cost) for c in calls if c.estimated_cost is not None]
    tokens = [(c.input_tokens or 0) + (c.output_tokens or 0) for c in calls
              if c.input_tokens is not None or c.output_tokens is not None]
    held = db.scalar(select(func.count(Job.id)).where(Job.user_id == user_id, Job.domain_match == domain_match.REVIEW)) or 0
    return {"jobs": total, "needs_review": held, "routes": routes, "classifications": classes, "gate_codes": gates,
            "gate_review": gate_review(db, user_id, start, end), "asked": asked, "cache_hits": cached, "model_calls": len(calls),
            "ask_rate": round(asked / total, 4) if total else None,
            "cache_hit_rate": round(cached / asked, 4) if asked else None,
            "tokens": sum(tokens) if tokens else None, "tokens_per_call": round(sum(tokens) / len(tokens)) if tokens else None,
            "cost": round(sum(costs), 6) if costs else None, "calls_with_cost": len(costs),
            "latency_p50_ms": round(statistics.median(latencies)) if latencies else None,
            "latency_p95_ms": latencies[int(len(latencies) * 0.95) - 1 if len(latencies) > 1 else 0] if latencies else None}


def compare(db: Session, user_id: int, limit: int = 1000) -> dict[str, Any]:
    """Shadow mode: where the old filter and the hybrid disagree. Metadata only, bounded."""
    m = JobClassificationTask
    rows = db.execute(select(m.job_id, m.legacy_match, m.legacy_reason, m.classification, m.explanation, m.route, Job.title)
                      .join(Job, Job.id == m.job_id)
                      .where(m.user_id == user_id, m.mode == TASK_SHADOW, m.classification.is_not(None),
                             m.legacy_match.is_not(None))
                      .order_by(m.id.desc()).limit(max(1, limit))).all()
    rescued = [r for r in rows if r.legacy_match is False and r.classification != NO_MATCH]
    newly_hidden = [r for r in rows if r.legacy_match is True and r.classification == NO_MATCH]
    shown = lambda r: {"job_id": r.job_id, "title": r.title, "legacy_reason": r.legacy_reason,
                       "classification": r.classification, "reason": r.explanation, "route": r.route}
    return {"compared": len(rows), "old_hides_new_keeps": len(rescued), "old_keeps_new_hides": len(newly_hidden),
            "rescued": [shown(r) for r in rescued[:50]], "newly_hidden": [shown(r) for r in newly_hidden[:50]]}
