"""Learning from what the person dismisses and keeps. Deterministic counting, no model.

    dismiss / reject / delete a job   → negative event (title terms, company)
    shortlist / approve / keep a job  → positive event
    events → learned exclusions: a title term or company behind at least LEARN_MIN_DISMISSALS dismissed jobs, at
             least 80% of its events negative, and never a term the candidate's own roles, skills or include list use
    learned exclusions → app.services.domain_match hides similar new jobs (reason "Similar to jobs you dismissed")

With DOMAIN_CLASSIFIER_MODE=on, learning counts only what says something about taste (`counted_events`): a mass cleanup
is not a dislike, and a job the person also shortlisted or applied to is not a negative. Learned title words then only
send a job to the classifier (domain_match.triage); only "Always exclude" hides on a word.

A learned rule turns itself off when the person later keeps enough jobs with that term (20% positive), and the
person can disable, delete or promote any rule in Settings. Events are kept after the job is deleted. A posting
deleted because it no longer exists is never an event: that says nothing about the person's taste.
"""
from __future__ import annotations

import copy
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import NEGATIVE_FEEDBACK, AppSetting, Job, JobFeedbackEvent
from app.services import domain_match
from app.settings import get_settings

NEGATIVE_SHARE = 0.8
DISABLE_POSITIVE_SHARE = 0.2
MIN_DISTINCT_JOBS = 2
MAX_NGRAM = 3
# Deletes per account and day from which they are a cleanup, not a judgement on each job (the history before
# BULK_DELETED existed has only DELETED rows).
CLEANUP_DELETES_PER_DAY = 20
# Words that are never a learned term on their own: seniority, generic job words, glue.
STOPWORDS = domain_match.GENERIC_ROLE_WORDS | frozenset({
    "full", "time", "part", "contract", "permanent", "temporary", "job", "role", "position", "opening", "urgent",
    "hiring", "immediate", "joiner", "wfh", "onsite", "on", "site", "work", "from", "home", "new", "or", "&", "-",
    "india", "usa", "uk", "us", "remote", "hybrid", "bangalore", "bengaluru", "mumbai", "pune", "delhi", "hyderabad"})


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def title_terms(title: str) -> list[str]:
    """1–3 word phrases of the title, without seniority or generic words at their edges."""
    words = [w for w in re.findall(r"[a-z0-9+#.]+", (title or "").lower()) if w.strip(".")]
    terms: list[str] = []
    for size in range(1, MAX_NGRAM + 1):
        for start in range(len(words) - size + 1):
            gram = words[start:start + size]
            if gram[0] in STOPWORDS or gram[-1] in STOPWORDS or any(len(w) < 2 for w in gram):
                continue
            terms.append(" ".join(gram))
    return list(dict.fromkeys(terms))


def record(db: Session, job: Job, action: str) -> JobFeedbackEvent | None:
    """Remember what the person did with this job. Flushes nothing and never raises into the caller's request."""
    try:
        event = JobFeedbackEvent(user_id=job.user_id, job_id=job.id, action=action, title=(job.title or "")[:300],
                                 company=(job.company or "")[:300], terms=title_terms(job.title or ""),
                                 created_at=utcnow())
        db.add(event)
        return event
    except Exception:  # noqa: BLE001  learning is best effort; the person's action always goes through
        return None


def counted_events(events: list[JobFeedbackEvent]) -> list[JobFeedbackEvent]:
    """The events that say something about the person's taste. Drops deletions from a day of mass cleanup, and the
    negatives of a job they also shortlisted, kept, approved or applied to."""
    deletes: Counter = Counter(e.created_at.date() for e in events if e.action == "DELETED" and e.created_at)
    liked = {e.job_id for e in events if e.job_id and e.action not in NEGATIVE_FEEDBACK and e.action != "BULK_DELETED"}
    return [e for e in events
            if not (e.action in NEGATIVE_FEEDBACK and e.job_id in liked)
            and not (e.action == "DELETED" and e.created_at
                     and deletes[e.created_at.date()] >= CLEANUP_DELETES_PER_DAY)]


def _protected(db: Session, user_id: int) -> tuple[set[str], set[str]]:
    """Terms that may never be learned: the candidate's own role words, skills and include terms."""
    prefs, own = domain_match.preferences(db, user_id), domain_match.derived(db, user_id)
    words = domain_match.role_tokens(own["roles"]) | set(own["skills"])
    words |= {domain_match._norm(t) for t in prefs["include_terms"]}
    return words, set(own["roles"])


def learn(db: Session, user_id: int) -> list[dict[str, Any]]:
    """Recompute the account's learned exclusions from its events and save them. Rules the person disabled stay
    disabled. Does not commit. Returns the learned list."""
    minimum = max(1, get_settings().learn_min_dismissals)
    events = db.scalars(select(JobFeedbackEvent).where(JobFeedbackEvent.user_id == user_id,
                                                       JobFeedbackEvent.action != "BULK_DELETED")).all()
    from app.services import domain_classifier

    clean = domain_classifier.mode() == domain_classifier.ON
    if clean:
        events = counted_events(list(events))
    stats: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"neg": set(), "pos": set(), "examples": []})
    for event in events:
        negative = event.action in NEGATIVE_FEEDBACK
        key_of_job = event.job_id or f"e{event.id}"
        keys = [("title", term) for term in (event.terms or [])]
        if event.company:
            keys.append(("company", domain_match._norm(event.company)))
        for key in keys:
            entry = stats[key]
            (entry["neg"] if negative else entry["pos"]).add(key_of_job)
            if negative and len(entry["examples"]) < 3 and event.title not in entry["examples"]:
                entry["examples"].append(event.title)
    protected_words, own_roles = _protected(db, user_id)
    row = db.scalar(select(AppSetting).where(AppSetting.user_id == user_id))
    previous = {(e.get("kind"), e.get("term")): e for e in
                ((row.preferences or {}).get("domain") or {}).get("learned", [])} if row else {}
    learned: dict[tuple[str, str], dict[str, Any]] = {}
    for (kind, term), entry in stats.items():
        negative, positive = len(entry["neg"]), len(entry["pos"])
        if negative < minimum or negative < MIN_DISTINCT_JOBS:
            continue
        if kind == "title" and (any(w in protected_words for w in term.split())
                                or any(domain_match.contains(role, term) for role in own_roles)):
            continue
        share = negative / (negative + positive)
        old = previous.get((kind, term)) or {}
        if share < NEGATIVE_SHARE and not old:
            continue
        status = "disabled" if old.get("user_disabled") or positive / (negative + positive) >= DISABLE_POSITIVE_SHARE \
            else "active"
        learned[(kind, term)] = {"term": term, "kind": kind, "evidence": negative, "positive": positive,
                                 "examples": entry["examples"], "status": status,
                                 "user_disabled": bool(old.get("user_disabled")),
                                 "forgotten": bool(old.get("forgotten")),
                                 **({"clean": True} if clean else {}),
                                 "first_seen": old.get("first_seen") or utcnow().isoformat(),
                                 "updated_at": utcnow().isoformat()}
    # Keep the shortest phrase: "sdr" makes "sdr intern" redundant.
    titles = sorted((e for e in learned.values() if e["kind"] == "title"), key=lambda e: len(e["term"].split()))
    kept: list[dict[str, Any]] = []
    for entry in titles:
        if not any(domain_match.contains(entry["term"], shorter["term"]) for shorter in kept):
            kept.append(entry)
    result = kept + [e for e in learned.values() if e["kind"] == "company"]
    save_learned(db, user_id, result)
    return result


def save_learned(db: Session, user_id: int, learned: list[dict[str, Any]]) -> None:
    from app.services.user_settings import lock_settings_row, settings_for

    row = lock_settings_row(db, settings_for(db, user_id))
    preferences = copy.deepcopy(row.preferences or {})  # a new object: JSON columns save by identity
    preferences.setdefault("domain", {})["learned"] = learned
    row.preferences = preferences


def after_feedback(db: Session, user_id: int) -> None:
    """Re-learn after an event, if the account learns from feedback. Never raises into the caller's request."""
    try:
        if domain_match.preferences(db, user_id)["learn_from_feedback"]:
            db.flush()
            if db.scalar(select(AppSetting.id).where(AppSetting.user_id == user_id)) is None:
                from app.services.user_settings import settings_for

                settings_for(db, user_id)  # created once, outside the savepoint: creating it commits
            with db.begin_nested():  # a failure rolls back only the learning, never the person's action
                learn(db, user_id)
    except Exception:
        import logging

        logging.getLogger("jobhunt.feedback").warning("could not update learned domain rules", exc_info=True)
