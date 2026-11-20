"""AI structuring of uploaded resumes: the Resume Parser model reads a version, code holds it to the resume's text.

    upload → resume_verification.record_version → ensure_structured (the regex parser: instant, always there)
           → enqueue(resume)                                   ← a task row only; no model in the request
    scheduler job `resume_parse` → claim → resume_parsing_graph.run
      LOAD_RESUME → EXTRACT_STRUCTURE → GROUND_IN_TEXT → CHECK_QUALITY → APPLY_RESULT
                                                                         ├─ filled   nobody reviewed or edited it
                                                                         └─ compare  a person did: a comparison

Each step here is one function the LangGraph graph (app.agents.resume_parsing) calls as a node, and `process` calls
in sequence when the graph cannot run (RESUME_PARSE_GRAPH_MODE=off, AGENT_GRAPHS_ENABLED off, or SQLite).

Rules a change has to keep:
  * The model extracts; code decides. `ground` is deterministic: a value the model returned is kept only when the
    resume's own text contains it (a bullet word for word, give or take PDF line breaks and broken characters). An
    ungrounded value is dropped, never rewritten, and the counts are kept on the task.
  * A person's work wins. The result replaces the structured data only while no unit is reviewed, no change was made
    by the person and the revision is the one the task was queued against (`untouched`); otherwise it becomes the
    usual comparison (`resume_verification.compare`), resolved value by value. Everything it writes is UNVERIFIED.
  * The model output lives on the task row (`staged`) between steps, never in a graph checkpoint.
  * Every write after the model call is guarded by the task's lease (`held`), and by the graph thread's lease when a
    graph runs it (`within`, app.agents.effects), so Kill is always safe and a replayed step writes once.
  * The text never leaves through the request: a missing AI configuration ends the task (`not_configured`) and the
    regex parse stays.
"""
from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

from sqlalchemy import desc, func, or_, select, update
from sqlalchemy.orm import Session

from app.models import ACTIVE_RESUME_PARSE_STATUSES, Resume, ResumeDataChange, ResumeParseTask
from app.services import structured_resume as sr
from app.services import worker_settings
from app.services.lifecycle import STOPPED_BY_USER
from app.settings import get_settings

log = logging.getLogger("jobhunt.resume_parsing")

KIND = "resume_parsing"
PENDING, PROCESSING, COMPLETED, FAILED = "PENDING", "PROCESSING", "COMPLETED", "FAILED"
MAX_ERROR_LENGTH = 2000
# How a task ends (ResumeParseTask.outcome).
FILLED, COMPARE, KEPT, NOT_CONFIGURED, GONE = "filled", "compare", "kept", "not_configured", "gone"
RESULTS = (FILLED, COMPARE, KEPT, NOT_CONFIGURED, GONE)
# What one run reports to the scheduler.
OUTCOMES = (*RESULTS, "retrying", "failed", "lost")
UPLOAD, USER_REQUEST = "upload", "user_request"

# Step ids (stable: the graph's node ids, the task's `step` and Running workers use them).
LOAD, EXTRACT, GROUND, CHECK, APPLY = "LOAD_RESUME", "EXTRACT_STRUCTURE", "GROUND_IN_TEXT", "CHECK_QUALITY", "APPLY_RESULT"
STEPS = (LOAD, EXTRACT, GROUND, CHECK, APPLY)

# A bullet counts as the resume's own when this share of its word pairs is in the text (PDF line breaks and
# characters the extraction broke, which the prompt lets the model repair, cost a pair or two).
BULLET_MATCH = 0.8
# The AI result is refused when it carries fewer roles than the regex parse and less than this share of its bullets.
BULLET_FLOOR = 0.8
# The renamings the prompt allows (K8s -> Kubernetes): the canonical name is grounded by any of its spellings.
SKILL_ALIASES = {"kubernetes": ("k8s",), "postgresql": ("postgres", "psql"), "google cloud platform": ("gcp",),
                 "javascript": ("js",), "typescript": ("ts",), "amazon web services": ("aws",),
                 "microsoft azure": ("azure",), "golang": ("go",), "go": ("golang",), "node.js": ("node", "nodejs"),
                 "react": ("reactjs", "react.js"), "ci/cd": ("cicd", "ci cd")}

# Called with (session, step) before a step writes; False means the step's write was already recorded (a replayed
# graph node). None outside a graph.
Within = Callable[[Session, str], bool] | None


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _prompt_version(marked: bool = False) -> str:
    """The prompt version a version is structured with: v7 for a resume read with V2 (its parse view), else v6."""
    from app.prompts.resume_parser import (
        RESUME_PARSER_PROMPT_VERSION,
        RESUME_PARSER_V2_PROMPT_VERSION,
    )

    return RESUME_PARSER_V2_PROMPT_VERSION if marked else RESUME_PARSER_PROMPT_VERSION


def _view(db: Session, resume: Resume | None) -> str | None:
    """The parse view of a version read with resume extraction V2 (None otherwise)."""
    if resume is None:
        return None
    from app.services.resume_extraction import store

    row = store.record(db, resume.id)
    return row.parse_view if row is not None and row.parse_view else None


# ───────────── the queue ─────────────

def active_task(db: Session, resume_id: int) -> ResumeParseTask | None:
    queued = next((t for t in db.new if isinstance(t, ResumeParseTask) and t.resume_id == resume_id), None)
    return queued or db.scalar(select(ResumeParseTask).where(
        ResumeParseTask.resume_id == resume_id, ResumeParseTask.status.in_(ACTIVE_RESUME_PARSE_STATUSES)))


def latest_task(db: Session, resume_id: int) -> ResumeParseTask | None:
    return db.scalar(select(ResumeParseTask).where(ResumeParseTask.resume_id == resume_id)
                     .order_by(desc(ResumeParseTask.id)).limit(1))


def enqueue(db: Session, resume: Resume, *, reason: str = UPLOAD, run_now: bool = False,
            now: datetime | None = None) -> ResumeParseTask | None:
    """Queue one AI structuring of this version. Does not commit.

    A version never has two active tasks. An upload is not queued again for text the current prompt already
    structured (or a person stopped); a person's request always is."""
    if not get_settings().resume_parse_enabled or not (resume.raw_text or "").strip():
        return None
    now = now or utcnow()
    active = active_task(db, resume.id)
    if active is not None:
        if run_now and active.status == PENDING:
            active.run_now_at, active.next_attempt_at = now, None
        return active
    if reason != USER_REQUEST:
        if resume.parser == "llm" and resume.parser_version == _prompt_version(_view(db, resume) is not None):
            return None
        last = latest_task(db, resume.id)
        if last is not None and last.raw_text_hash == resume.raw_text_hash and (
                last.last_error == STOPPED_BY_USER or last.outcome in RESULTS):
            return None
    task = ResumeParseTask(user_id=resume.user_id, resume_id=resume.id, raw_text_hash=resume.raw_text_hash,
                           base_revision=resume.revision or 0, status=PENDING, reason=reason, attempt_count=0,
                           run_now_at=now if run_now else None, created_at=now)
    db.add(task)
    db.flush()
    return task


def ai_only(db: Session, user_id: int) -> bool:
    """The account structures uploads with AI only: an upload runs now, and the regex parse is only a fallback."""
    return bool(worker_settings.get(db, user_id, KIND, "ai_only"))


def extraction_v2(db: Session, user_id: int) -> bool:
    """Whether this account's PDF uploads are read by resume extraction V2 (worker setting, else the server's)."""
    return bool(worker_settings.get(db, user_id, KIND, "extraction_v2"))


def queue_after_upload(db: Session, resume: Resume) -> None:
    """The upload hook: queue the new version (run now when the account structures uploads with AI only), and wake
    the worker. Never fails the upload."""
    try:
        with db.begin_nested():
            queued = enqueue(db, resume, run_now=ai_only(db, resume.user_id))
    except Exception:  # a queue problem never loses an upload
        log.warning("could not queue AI structuring for resume %s", resume.id, exc_info=True)
        return
    if queued is not None:
        nudge()


def nudge() -> None:
    try:
        from app import scheduler_hooks

        scheduler_hooks.nudge("resume_parse")
    except Exception:  # a hint only: the job also runs on its interval
        log.debug("could not nudge resume_parse", exc_info=True)


@dataclass(frozen=True)
class ParseClaim:
    task_id: int
    lock_token: str
    worker_id: str
    user_id: int
    resume_id: int
    attempt: int = 1
    run_now: bool = False  # asked to run now: skips the provider cooldown for this one run


def claim(db: Session, *, worker_id: str, limit: int, now: datetime | None = None,
          lease_seconds: int | None = None) -> list[ParseClaim]:
    """Lease up to `limit` due tasks (FOR UPDATE SKIP LOCKED on PostgreSQL), run-now tasks first. Commits."""
    now = now or utcnow()
    lease = timedelta(seconds=lease_seconds or get_settings().resume_parse_lease_seconds)
    m = ResumeParseTask
    query = (select(m).where(m.status == PENDING, or_(m.next_attempt_at.is_(None), m.next_attempt_at <= now))
             .order_by(m.run_now_at.is_(None), m.next_attempt_at.asc().nulls_first(), m.id).limit(max(1, limit)))
    stopped = worker_settings.paused(db, KIND)
    if stopped:  # a paused account keeps its queue; Run now still takes a task
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
        claimed.append(ParseClaim(task.id, task.lock_token, worker_id[:200], task.user_id, task.resume_id,
                                  task.attempt_count, run_now))
    db.commit()
    return claimed


def held(db: Session, c: ParseClaim) -> ResumeParseTask | None:
    """The task, only while this run still holds its lease. On PostgreSQL the row stays locked until the caller
    commits or rolls back, so a Kill or a lease recovery (which lock the same row) cannot land between this check and
    the write it guards."""
    query = select(ResumeParseTask).where(
        ResumeParseTask.id == c.task_id, ResumeParseTask.status == PROCESSING,
        ResumeParseTask.lock_token == c.lock_token).execution_options(populate_existing=True)
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    return db.scalar(query)


def recover_expired(db: Session, now: datetime | None = None) -> int:
    """Tasks whose worker died go back in the queue, or fail once their attempts are used. Commits."""
    now = now or utcnow()
    rows = db.scalars(select(ResumeParseTask).where(ResumeParseTask.status == PROCESSING,
                                                    ResumeParseTask.lock_expires_at < now)).all()
    message = "Interrupted: the worker stopped while structuring this resume"
    for task in rows:
        if task.attempt_count >= worker_settings.get(db, task.user_id, KIND, "max_attempts"):
            _finish(task, FAILED, now, error=message, category="LEASE_EXPIRED")
        else:
            _release(task)
            task.last_error, task.error_category = message, "LEASE_EXPIRED"
    db.commit()
    return len(rows)


def _release(task: ResumeParseTask) -> None:
    task.status, task.locked_by, task.lock_token, task.lock_expires_at = PENDING, None, None, None


def _finish(task: ResumeParseTask, status: str, now: datetime, *, outcome: str | None = None,
            error: str | None = None, category: str | None = None) -> None:
    task.status, task.completed_at, task.outcome = status, now, outcome
    task.locked_by = task.lock_token = task.lock_expires_at = task.next_attempt_at = None
    task.last_error = error[:MAX_ERROR_LENGTH] if error else None
    task.error_category = category
    task.staged = None  # the model output is only kept while the run needs it


def fail(sessions: Callable[[], Session], c: ParseClaim, error: str, now: datetime | None = None) -> str:
    """A step raised: retry later with a growing wait, or fail once the attempts are used. Never raises."""
    now = now or utcnow()
    try:
        with sessions() as db:
            db.info["user_id"] = c.user_id
            task = held(db, c)
            if task is None:
                return "lost"
            own = worker_settings.for_user(db, task.user_id, KIND)
            if task.attempt_count >= own["max_attempts"]:
                _finish(task, FAILED, now, error=error, category="EXTERNAL_SERVICE")
                outcome = "failed"
            else:
                _release(task)
                task.last_error, task.error_category = error[:MAX_ERROR_LENGTH], "EXTERNAL_SERVICE"
                task.next_attempt_at = now + timedelta(seconds=own["retry_seconds"] * task.attempt_count)
                outcome = "retrying"
            db.commit()
            return outcome
    except Exception:
        log.warning("could not settle resume parse task %s", c.task_id, exc_info=True)
        return "lost"


def requeue(db: Session, task: ResumeParseTask, user_id: int) -> ResumeParseTask:
    """Retry / Run now from Running workers: a finished task's resume gets a new task (a person asked)."""
    from app.services.workers import WorkerError

    resume = db.scalar(select(Resume).where(Resume.id == task.resume_id, Resume.user_id == user_id))
    if resume is None:
        raise WorkerError("The resume no longer exists", 404)
    fresh = enqueue(db, resume, reason=USER_REQUEST)
    if fresh is None:
        raise WorkerError("This resume has no text to structure")
    return fresh


def on_stopped(db: Session, task: ResumeParseTask, now: datetime) -> None:
    """Kill: the task's graph thread stops too, so no node writes again; the model output it staged is dropped."""
    task.staged = None
    if task.agent_thread_id:
        from app.services import qualification

        qualification.stop_threads(db, [task.agent_thread_id], now)


# ───────────── grounding: the model's values held to the resume's own text ─────────────

def _norm(text: Any) -> str:
    """Lower case words and numbers only, single spaces: what two spellings of the same text share."""
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _link(text: Any) -> str:
    return _norm(re.sub(r"^\s*(?:https?://)?(?:www\.)?", "", str(text or ""), flags=re.IGNORECASE))


class Source:
    """The resume text, prepared once for the checks."""

    def __init__(self, raw_text: str):
        self.text = f" {_norm(raw_text)} "
        words = self.text.split()
        self.words = set(words)
        self.pairs = set(pairwise(words))
        self.digits = re.sub(r"\D", "", raw_text or "")

    def has(self, value: Any) -> bool:
        needle = _norm(value)
        return bool(needle) and f" {needle} " in self.text

    def has_link(self, value: Any) -> bool:
        needle = _link(value)
        return bool(needle) and f" {needle} " in self.text

    def has_phone(self, value: Any) -> bool:
        digits = re.sub(r"\D", "", str(value or ""))
        return len(digits) >= 6 and digits in self.digits

    def has_passage(self, value: Any) -> bool:
        """A bullet or a sentence: the text has it, word for word give or take a few broken words. A number is a fact,
        never a broken word: every one in the passage must be in the text exactly (45% never passes as 95%)."""
        words = _norm(value).split()
        if not words:
            return False
        if f" {' '.join(words)} " in self.text:
            return True
        if len(words) < 4 or any(re.search(r"\d", word) and word not in self.words for word in words):
            return False
        pairs = list(pairwise(words))
        return sum(pair in self.pairs for pair in pairs) / len(pairs) >= BULLET_MATCH

    def has_skill(self, value: Any) -> bool:
        if self.has(value):
            return True
        return any(self.has(alias) for alias in SKILL_ALIASES.get(str(value or "").strip().lower(), ()))


class _Counter:
    def __init__(self) -> None:
        self.dropped: dict[str, int] = {}

    def drop(self, what: str, n: int = 1) -> None:
        if n:
            self.dropped[what] = self.dropped.get(what, 0) + n


def _text_of(value: Any) -> str:
    return value.get("text", "") if isinstance(value, dict) else str(value or "")


def _passages(values: list[Any], src: Source, count: _Counter, what: str) -> list[Any]:
    kept = [v for v in values or [] if _text_of(v).strip() and src.has_passage(_text_of(v))]
    count.drop(what, len([v for v in values or [] if _text_of(v).strip()]) - len(kept))
    return kept


def _names(values: list[Any], src: Source, count: _Counter, what: str) -> list[str]:
    kept = [v for v in values or [] if isinstance(v, str) and src.has_skill(v)]
    count.drop(what, len(values or []) - len(kept))
    return kept


def _fields(item: dict[str, Any], names: tuple[str, ...], src: Source, count: _Counter, what: str) -> None:
    """Clear each named field the text does not contain (a value the model inferred or reworded)."""
    for name in names:
        if item.get(name) and not src.has(item[name]):
            item[name] = ""
            count.drop(what)


def _dates(item: dict[str, Any], src: Source, count: _Counter, start: str = "start_date",
           end: str = "end_date", current_flag: bool = False) -> None:
    """A date is the text's own: its raw form must be in the text, and the normalized month is computed from it
    (sr.normalize_date: a year alone never gets a month). With `current_flag` (a role), is_current comes from the
    grounded end date only, in both directions: "Present" sets it, anything else, or no end date, clears it."""
    if current_flag:
        item["is_current"] = False
    for field, raw_field in ((start, f"raw_{start}"), (end, f"raw_{end}")):
        raw = item.get(raw_field)
        if raw and not src.has(raw):
            item[raw_field] = raw = None
            count.drop("dates")
        if not raw:
            if item.get(field):
                item[field] = None
                count.drop("dates")
            continue
        normalized, current = sr.normalize_date(raw)
        if field == end and current and current_flag:
            item["is_current"] = True
        item[field] = normalized


def ground(data: dict[str, Any], raw_text: str) -> tuple[dict[str, Any], dict[str, int]]:
    """The model's structured resume (sr.from_parser_output's shape), keeping only what the resume text states.
    Deterministic; never adds or rewrites a value. Returns the kept data and what was dropped, by kind."""
    src, count = Source(raw_text), _Counter()
    out = dict(data)
    personal = dict(out.get("personal_information") or {})
    _fields(personal, ("full_name", "headline", "location"), src, count, "personal")
    if personal.get("email") and personal["email"].lower() not in (raw_text or "").lower():
        personal["email"] = ""
        count.drop("personal")
    if personal.get("phone") and not src.has_phone(personal["phone"]):
        personal["phone"] = ""
        count.drop("personal")
    for name in ("linkedin", "github", "portfolio"):
        if personal.get(name) and not src.has_link(personal[name]):
            personal[name] = ""
            count.drop("personal")
    links = [link for link in personal.get("other_links") or [] if src.has_link(link)]
    count.drop("personal", len(personal.get("other_links") or []) - len(links))
    personal["other_links"] = links
    out["personal_information"] = personal

    summary = out.get("summary") or ""
    if summary and not src.has_passage(summary):
        out["summary"] = ""
        count.drop("summary")

    skills = []
    for skill in out.get("skills") or []:
        if not isinstance(skill, dict):
            continue
        skill = dict(skill)
        raw = skill.pop("raw", None)
        if src.has_skill(skill.get("name")):
            if raw and not src.has(skill.get("name")) and src.has(raw) and normalizes(raw, skill.get("name")):
                skill["raw"] = raw  # kept through an alias ("K8s" for "Kubernetes"): say how the resume writes it
            skills.append(skill)
        elif raw and src.has(raw) and normalizes(raw, skill.get("name")):
            skills.append({**skill, "raw": raw})  # a normalized name ("Apache Kafka") of a term the text has ("Kafka")
    count.drop("skills", len(out.get("skills") or []) - len(skills))
    for skill in skills:
        if skill.get("category") and not src.has(skill["category"]):
            skill["category"] = ""
    out["skills"] = skills

    roles = []
    for role in out.get("experience") or []:
        role = dict(role)
        _fields(role, ("company", "job_title", "location", "employment_type"), src, count, "role_fields")
        _dates(role, src, count, current_flag=True)
        role["achievements"] = _passages(role.get("achievements"), src, count, "bullets")
        role["responsibilities"] = _passages(role.get("responsibilities"), src, count, "bullets")
        role["technologies"] = _names(role.get("technologies"), src, count, "skills")
        role["skills"] = _names(role.get("skills"), src, count, "skills")
        if not (role.get("company") or role.get("job_title")):
            count.drop("roles")
            continue
        roles.append(role)
    out["experience"] = roles

    projects = []
    for project in out.get("projects") or []:
        project = dict(project)
        project.pop("image_url", None)  # the person's own additions: a parse never supplies them
        project.pop("result", None)
        if project.get("description") and not src.has_passage(project["description"]):
            project["description"] = ""
            count.drop("project_fields")
        if project.get("url") and not src.has_link(project["url"]):
            project["url"] = ""
            count.drop("project_fields")
        project["achievements"] = _passages(project.get("achievements"), src, count, "bullets")
        project["technologies"] = _names(project.get("technologies"), src, count, "skills")
        project["skills"] = _names(project.get("skills"), src, count, "skills")
        if not (project.get("name") and src.has(project["name"])):
            count.drop("projects")
            continue
        projects.append(project)
    out["projects"] = projects

    education = []
    for item in out.get("education") or []:
        item = dict(item)
        _fields(item, ("degree", "field_of_study", "institution", "university", "location", "grade"), src, count,
                "education_fields")
        _dates(item, src, count)
        if not (item.get("institution") or item.get("degree")):
            count.drop("education")
            continue
        education.append(item)
    out["education"] = education

    certifications = []
    for item in out.get("certifications") or []:
        item = dict(item)
        if not src.has(item.get("name")):
            count.drop("certifications")
            continue
        _fields(item, ("issuer", "credential_id"), src, count, "certification_fields")
        for name in ("issue_date", "expiration_date"):
            if item.get(name) and not src.has(item[name]):
                item[name] = None
                count.drop("dates")
        if item.get("url") and not src.has_link(item["url"]):
            item["url"] = ""
        certifications.append(item)
    out["certifications"] = certifications

    awards = []
    for item in out.get("awards") or []:
        item = dict(item)
        if not src.has_passage(item.get("title")):
            count.drop("awards")
            continue
        _fields(item, ("issuer",), src, count, "award_fields")
        if item.get("date") and not src.has(item["date"]):
            item["date"] = None
        if item.get("description") and not src.has_passage(item["description"]):
            item["description"] = ""
        awards.append(item)
    out["awards"] = awards

    languages = [dict(lang) for lang in out.get("languages") or [] if src.has(lang.get("name"))]
    count.drop("languages", len(out.get("languages") or []) - len(languages))
    for lang in languages:
        if lang.get("proficiency") and not src.has(lang["proficiency"]):
            lang["proficiency"] = ""
    out["languages"] = languages
    out["additional_information"] = [
        {**v, "url": v["url"] if src.has_link(v["url"]) else ""} if isinstance(v, dict) and v.get("url") else v
        for v in _passages(out.get("additional_information"), src, count, "additional")]
    return sr.fit_parser_output(out), count.dropped


def normalizes(raw: Any, name: Any) -> bool:
    """`name` is a normalized form of `raw`, by a rule code can check: the same words with more around them ("Kafka" →
    "Apache Kafka"), a known alias ("K8s" → "Kubernetes") or its initials ("GCP" → "Google Cloud Platform"). Never a
    different skill ("Python" → "Rust")."""
    raw_n, name_n = _norm(raw), _norm(name)
    if not raw_n or not name_n:
        return False
    if f" {raw_n} " in f" {name_n} " or f" {name_n} " in f" {raw_n} ":
        return True
    if raw_n in SKILL_ALIASES.get(str(name or "").strip().lower(), ()):
        return True
    initials = "".join(word[0] for word in name_n.split())
    return len(raw_n) >= 2 and " " not in raw_n and raw_n == initials and len(initials) >= 2


_LIMIT = 60  # held-back values kept per parse


def held_back(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """What grounding left out of the model's answer, for the person to add or discard: skills, bullets, roles,
    projects, certifications, awards, languages, education, the summary and additional lines. Values only (and where
    they sat), never a field the text half-supports; at most _LIMIT."""
    out: list[dict[str, Any]] = []

    def hold(kind: str, value: Any, where: str = "") -> None:
        text = _text_of(value).strip() if not isinstance(value, str) else value.strip()
        if text and len(out) < _LIMIT:
            item = {"kind": kind, "value": text[:600], "where": where[:200]}
            item["id"] = sr.short_hash(kind, item["value"], item["where"])
            if all(i["id"] != item["id"] for i in out):
                out.append(item)

    kept_skills = {_norm(s.get("name")) for s in after.get("skills") or [] if isinstance(s, dict)}
    for skill in before.get("skills") or []:
        if isinstance(skill, dict) and _norm(skill.get("name")) not in kept_skills:
            hold("skill", skill.get("name") or "", skill.get("category") or "")
    if (before.get("summary") or "") and not (after.get("summary") or ""):
        hold("summary", before["summary"])
    kept_roles = {(_norm(r.get("job_title")), _norm(r.get("company"))) for r in after.get("experience") or []}
    kept_bullets = {_norm(_text_of(b)) for r in after.get("experience") or []
                    for b in [*(r.get("achievements") or []), *(r.get("responsibilities") or [])]}
    for role in before.get("experience") or []:
        label = " · ".join(p for p in (role.get("job_title"), role.get("company")) if p)
        key = (_norm(role.get("job_title")), _norm(role.get("company")))
        if key not in kept_roles and not any(k[0] == key[0] or k[1] == key[1] for k in kept_roles if any(k)):
            hold("role", label)
        for bullet in [*(role.get("achievements") or []), *(role.get("responsibilities") or [])]:
            if _norm(_text_of(bullet)) not in kept_bullets:
                hold("bullet", bullet, label)
    for kind, key in (("project", "name"), ("certification", "name"), ("award", "title"), ("language", "name")):
        section = {"project": "projects", "certification": "certifications", "award": "awards",
                   "language": "languages"}[kind]
        kept = {_norm(i.get(key)) for i in after.get(section) or [] if isinstance(i, dict)}
        for item in before.get(section) or []:
            if isinstance(item, dict) and item.get(key) and _norm(item.get(key)) not in kept:
                hold(kind, item[key])
    kept_education = {(_norm(e.get("degree")), _norm(e.get("institution"))) for e in after.get("education") or []}
    for item in before.get("education") or []:
        if isinstance(item, dict) and (_norm(item.get("degree")), _norm(item.get("institution"))) not in kept_education \
                and not any(k[0] == _norm(item.get("degree")) or k[1] == _norm(item.get("institution"))
                            for k in kept_education if any(k)):
            hold("education", " · ".join(p for p in (item.get("degree"), item.get("institution")) if p))
    kept_extra = {_norm(_text_of(v)) for v in after.get("additional_information") or []}
    for value in before.get("additional_information") or []:
        if _norm(_text_of(value)) not in kept_extra:
            hold("additional", value)
    return out


def shape(data: dict[str, Any]) -> dict[str, int]:
    """Counts only: what a structured resume (input shape) holds."""
    roles = data.get("experience") or []
    projects = data.get("projects") or []
    return {"roles": len(roles),
            "bullets": sum(len(r.get("achievements") or []) + len(r.get("responsibilities") or []) for r in roles)
            + sum(len(p.get("achievements") or []) for p in projects),
            "skills": len(data.get("skills") or []), "projects": len(projects),
            "education": len(data.get("education") or [])}


def acceptable(ai: dict[str, int], baseline: dict[str, int]) -> str | None:
    """Why the grounded AI result is refused in favour of the regex parse, or None. Counts only, deterministic."""
    if not ai["roles"] and not ai["bullets"] and not ai["education"] and not ai["skills"]:
        return "The AI result held nothing the resume text confirms"
    if ai["roles"] < baseline["roles"] and ai["bullets"] < baseline["bullets"] * BULLET_FLOOR:
        return (f"The AI result found fewer roles ({ai['roles']} vs {baseline['roles']}) and fewer bullets "
                f"({ai['bullets']} vs {baseline['bullets']}) than the current data")
    return None


# ───────────── the steps (graph nodes, or `process` in sequence) ─────────────

def _session(sessions: Callable[[], Session], c: ParseClaim) -> Session:
    db = sessions()
    db.info["user_id"] = c.user_id
    return db


def _record(db: Session, within: Within, step: str) -> bool:
    return within(db, step) if within is not None else True


def _end(db: Session, task: ResumeParseTask, outcome: str, step: str, note: str | None = None) -> dict[str, Any]:
    task.step = step
    _finish(task, COMPLETED, utcnow(), outcome=outcome, error=note)
    db.commit()
    return {"step": step, "outcome": outcome}


def load(sessions: Callable[[], Session], c: ParseClaim, within: Within = None) -> dict[str, Any]:
    """The version still exists with the text the task was queued for, and the account can call its model."""
    from app.services.user_settings import ai_ready

    with _session(sessions, c) as db:
        task = held(db, c)
        if task is None:
            return {"step": LOAD, "outcome": "lost"}
        resume = db.get(Resume, task.resume_id)
        if resume is None or not (resume.raw_text or "").strip():
            return _end(db, task, GONE, LOAD, "The resume or its text no longer exists")
        if resume.raw_text_hash != task.raw_text_hash:
            return _end(db, task, GONE, LOAD, "The resume text changed after the task was queued")
        ready, reason = ai_ready(db, task.user_id)
        if not ready:
            return _end(db, task, NOT_CONFIGURED, LOAD, reason)
        task.step = LOAD
        db.commit()
        return {"step": LOAD, "resume_id": resume.id}


def checked(raw: Any) -> dict[str, Any]:
    """The model call's validator: the parser output in the structured resume's input shape, or the gateway's schema
    error naming each field, so a malformed answer gets the correction retry."""
    from pydantic import ValidationError

    from app.services.llm import LLMSchemaError

    structured = raw.get("structured_resume") if isinstance(raw, dict) else None
    if not isinstance(structured, dict):
        raise LLMSchemaError("ResumeParserOutput", "WRONG_OUTPUT_SCHEMA", ["structured_resume: an object is required"])
    try:
        data = sr.from_parser_output(structured)
        sr.StructuredResumeIn.model_validate(data)
    except ValidationError as exc:
        problems = [f"{'.'.join(str(p) for p in e['loc']) or 'output'}: {e['msg']}" for e in exc.errors()[:6]]
        raise LLMSchemaError("ResumeParserOutput", "WRONG_OUTPUT_SCHEMA", problems) from exc
    return data


# generate(prompt, call) → the parser's validated data. The graph passes the LangChain door (GatewayChatModel); the
# plain path calls LLMClient. Either way it is the same gateway call, with the same telemetry rows.
Generate = Callable[[Any, Any], dict[str, Any]]


def gateway_generate(sessions: Callable[[], Session], user_id: int) -> Generate:
    def generate(prompt: Any, call: Any) -> dict[str, Any]:
        from app.services.llm import LLMClient

        with sessions() as db:
            db.info["user_id"] = user_id
            client = LLMClient.for_user(db, user_id)
        result = client.generate_prompt(prompt, call)
        return result.validated if isinstance(result.validated, dict) else checked(result.data)
    return generate


def extract(sessions: Callable[[], Session], c: ParseClaim, generate: Generate | None = None,
            within: Within = None) -> dict[str, Any]:
    """The Resume Parser model reads the text (no lock held while it does); its validated output is staged on the
    task. A missing or broken AI configuration ends the task and keeps the regex parse."""
    from app.prompts.resume_parser import build_resume_parser_prompt
    from app.services import backoff
    from app.services.llm import LLMCall, LLMConfigError

    with _session(sessions, c) as db:
        task = held(db, c)
        if task is None:
            return {"step": EXTRACT, "outcome": "lost"}
        if task.staged is not None and task.step == EXTRACT:
            return {"step": EXTRACT, **shape(task.staged)}  # a replayed node: the model already answered
        resume = db.get(Resume, task.resume_id)
        view = _view(db, resume)  # read with V2: the model reads the structure the reader marked (prompt v7)
        text = view or (resume.raw_text if resume is not None else "")
        db.rollback()
    prompt = build_resume_parser_prompt(text or "", marked=view is not None)
    call = LLMCall(workflow_type="profile", entity_id=c.resume_id, attempt=c.attempt, validate=checked)
    try:
        with backoff.bypass(c.run_now):
            data = (generate or gateway_generate(sessions, c.user_id))(prompt, call)
    except LLMConfigError as exc:
        with _session(sessions, c) as db:
            task = held(db, c)
            if task is None:
                return {"step": EXTRACT, "outcome": "lost"}
            return _end(db, task, NOT_CONFIGURED, EXTRACT, f"{type(exc).__name__}: {exc}")
    with _session(sessions, c) as db:
        task = held(db, c)
        if task is None or not _record(db, within, EXTRACT):
            db.rollback()
            return {"step": EXTRACT, "outcome": "lost"} if task is None else {"step": EXTRACT}
        task.staged, task.step = data, EXTRACT
        db.commit()
        return {"step": EXTRACT, **shape(data)}


def ground_step(sessions: Callable[[], Session], c: ParseClaim, within: Within = None) -> dict[str, Any]:
    """Hold the staged output to the resume's text (`ground`); keep the grounded data and the dropped counts."""
    with _session(sessions, c) as db:
        task = held(db, c)
        if task is None:
            return {"step": GROUND, "outcome": "lost"}
        if task.step in (GROUND, CHECK):  # a replayed node: already grounded
            return {"step": GROUND, **shape(task.staged or {}), "dropped": sum((task.counts or {}).get(
                "dropped", {}).values())}
        resume = db.get(Resume, task.resume_id)
        from app.services.resume_extraction import store

        text = store.grounding_text(db, resume) if resume is not None else ""  # raw, plus the clean text (V2)
        grounded, dropped = ground(task.staged or {}, text)
        if not _record(db, within, GROUND):
            db.rollback()
            return {"step": GROUND}
        if resume is not None and (row := store.record(db, resume.id)) is not None:
            row.review = held_back(task.staged or {}, grounded)  # V2: shown to the person, never used unasked
        task.staged, task.step = grounded, GROUND
        task.counts = {"dropped": dropped, "kept": shape(grounded)}
        db.commit()
        return {"step": GROUND, **shape(grounded), "dropped": sum(dropped.values())}


def check(sessions: Callable[[], Session], c: ParseClaim, within: Within = None) -> dict[str, Any]:
    """Compare the grounded result with the regex parse of the same text; a clearly poorer result is not used."""
    with _session(sessions, c) as db:
        task = held(db, c)
        if task is None:
            return {"step": CHECK, "outcome": "lost"}
        resume = db.get(Resume, task.resume_id)
        from app.services.resume_verification import parse_structured

        baseline = shape(parse_structured(db, resume) if resume is not None else sr.parse_deterministic(""))
        ai = shape(task.staged or {})
        problem = acceptable(ai, baseline)
        if not _record(db, within, CHECK):
            db.rollback()
            return {"step": CHECK}
        task.counts = {**(task.counts or {}), "baseline": baseline}
        if problem:
            return {**_end(db, task, KEPT, CHECK, problem), "baseline_roles": baseline["roles"]}
        task.step = CHECK
        db.commit()
        return {"step": CHECK, "baseline_roles": baseline["roles"], "baseline_bullets": baseline["bullets"]}


def untouched(db: Session, resume: Resume, base_revision: int) -> bool:
    """Nobody has reviewed or edited the structured data since the task was queued: the AI result may replace it."""
    if (resume.revision or 0) != base_revision:
        return False
    if any(unit.status in sr.REVIEWED for unit in sr.iter_units(resume.structured_data)):
        return False
    edits = db.scalar(select(func.count(ResumeDataChange.id)).where(ResumeDataChange.resume_id == resume.id,
                                                                     ResumeDataChange.source == "USER"))
    return not edits


def _lock_resume(db: Session, resume_id: int) -> Resume | None:
    if db.get_bind().dialect.name == "sqlite":
        db.execute(update(Resume).where(Resume.id == resume_id).values(id=Resume.id)
                   .execution_options(synchronize_session=False))
    query = select(Resume).where(Resume.id == resume_id).execution_options(populate_existing=True)
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    return db.scalar(query)


def apply(sessions: Callable[[], Session], c: ParseClaim, within: Within = None) -> dict[str, Any]:
    """Fill the structured data (untouched) or offer it as a comparison (a person reviewed or edited it). One
    transaction, under the task's lease; everything written is UNVERIFIED."""
    from app.services import resume_verification as rv

    with _session(sessions, c) as db:
        task = held(db, c)
        if task is None:
            return {"step": APPLY, "outcome": "lost"}
        resume = _lock_resume(db, task.resume_id)
        if resume is None or resume.raw_text_hash != task.raw_text_hash:
            return _end(db, task, GONE, APPLY, "The resume changed while it was being structured")
        if not _record(db, within, APPLY):
            db.rollback()
            return {"step": APPLY}
        rv.ensure_structured(db, resume)
        version = _prompt_version(_view(db, resume) is not None)
        if untouched(db, resume, task.base_revision):
            proposed = sr.materialize(task.staged or {})
            rv.record(db, resume, "*", "PARSE", "PARSER",
                      new={"parser": "llm", "version": version, "units": sum(1 for _ in sr.iter_units(proposed)),
                           "dropped": sum(((task.counts or {}).get("dropped") or {}).values())})
            resume.parser, resume.parser_version, resume.pending_reparse = "llm", version, None
            rv._touch(resume, proposed)
            rv.refresh_status(resume)
            db.flush()
            rv.sync_evidence(db, resume)
            outcome = FILLED
        else:
            rv.compare(db, resume, task.staged or {}, parser="llm", parser_version=version)
            outcome = COMPARE
        return _end(db, task, outcome, APPLY)


def process(sessions: Callable[[], Session], c: ParseClaim, generate: Generate | None = None) -> str:
    """Every step in sequence, without a graph (and the graph's fallback). Returns an outcome; never raises."""
    try:
        for step in (lambda: load(sessions, c), lambda: extract(sessions, c, generate),
                     lambda: ground_step(sessions, c), lambda: check(sessions, c), lambda: apply(sessions, c)):
            result = step()
            if result.get("outcome"):
                return result["outcome"]
        return "lost"
    except Exception as exc:  # one resume never stops the worker
        log.warning("resume parse task %s failed", c.task_id, exc_info=True)
        return fail(sessions, c, f"{type(exc).__name__}: {exc}")


# ───────────── what the resume screen shows ─────────────

NOTES = {FILLED: "Filled from the AI parse", COMPARE: "AI parse ready to compare below",
         KEPT: "The AI parse kept the current data", NOT_CONFIGURED: "AI parsing needs an AI model",
         GONE: "The resume changed; the AI parse was not used"}


def status_of(db: Session, resume_id: int) -> dict[str, Any] | None:
    """The latest AI structuring of a version, metadata only (never the staged data)."""
    from app.services import redaction

    task = latest_task(db, resume_id)
    if task is None:
        return None
    active = task.status in ACTIVE_RESUME_PARSE_STATUSES
    dropped = ((task.counts or {}).get("dropped") or {}) if not active else {}
    stopped = task.last_error == STOPPED_BY_USER
    note = ("Stopped" if stopped else "Structuring with AI…" if active
            else "The AI parse failed" if task.status == FAILED else NOTES.get(task.outcome or "", ""))
    return {"task_id": task.id, "status": task.status, "active": active, "step": task.step, "outcome": task.outcome,
            "note": note, "dropped": sum(dropped.values()), "dropped_by_kind": dropped,
            "detail": redaction.redact_text(task.last_error, 300) if task.last_error and not active else None,
            "finished_at": task.completed_at.isoformat() if task.completed_at else None}
