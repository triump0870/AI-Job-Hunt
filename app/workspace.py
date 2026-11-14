"""API for the redesigned workspace: overview, today, triage, tailor, approvals, tracker.

Every number the UI shows is derived from the database here; nothing is hard-coded.
Job Hunt OS never submits applications — the apply kit hands the final click to the user.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Literal

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import and_, desc, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_user_db
from app.models import (
    Application,
    ApplicationCommunicationEvent,
    ApplicationEvent,
    AppSetting,
    Evidence,
    FollowUp,
    GmailConnection,
    Job,
    KnowledgeAnswer,
    Resume,
    ResumeDocument,
    ResumeVariant,
    SavedSearch,
    SearchRun,
)
from app.prompts import build_evidence_extraction_prompt
from app.scheduler_hooks import run_soon
from app.services import (
    approval,
    ats_keywords,
    domain_match,
    evidence_tags,
    feedback_learning,
    ranker,
    resume_length,
    resume_matching,
    resume_verification,
)
from app.services import followups as followup_service
from app.services.approval import ApprovalRefused
from app.services.actors import (
    ACTOR_GROUPS,
    BUILTIN,
    DEFAULT_ACTORS,
    FAN_OUTS,
    HN_MONTH_RE,
    MAX_RUNS,
    PLACEHOLDERS,
    REMOTE_PLATFORMS,
    SENIORITY_LEVELS,
    YC_BATCH_RE,
    describe_input,
    plan_runs,
)
from app.services.actors import catalog as actor_catalog
from app.services.ai_config import STAGES as AI_STAGES
from app.services.ai_config import is_free_model
from app.services.ai_config import resolve as resolve_ai_config
from app.services.application_inbox import service as inbox_service
from app.services.application_inbox.classify import HUMAN_REPLY_TYPES
from app.services.contacts import (
    MAX_RECRUITERS,
    ContactIn,
    clean_email,
    clean_phone,
    event_meta,
    format_in_zone,
    recruiters_of,
    set_primary_recruiter,
    set_recruiters,
    to_utc,
    zone,
)
from app.services.exports import DOCX_MEDIA_TYPE, markdown_to_docx, packet_filename, packet_slug
from app.services.external_api import external_client
from app.services.external_monitoring import run_location
from app.services.job_removal import PROTECTED_STATUSES, delete_job
from app.services.knowledge import question_key
from app.services.lifecycle import WorkflowState as S
from app.services.lifecycle import can_transition
from app.services.llm import LLMCall, LLMClient
from app.services.locations import location_facets, location_key
from app.services.resume_format import strip_citations
from app.services.saved_searches import SCHEDULE_KINDS, next_due, schedule_label, schedule_problems
from app.services.sources import (
    apify_client,
    import_remote_run,
    list_remote_runs,
    refresh_apify_run,
    start_apify_search,
    upsert_job,
)
from app.services.tracker import (
    ALL_STAGE_IDS,
    BASE_STAGES,
    CLOSED_STAGE,
    EXTRA_STAGES,
    INACTIVE_STAGES,
    INTERVIEW_STAGES,
    REPLY_STAGES,
    archive_older_than,
    older_than,
    set_stage,
    unarchive,
)
from app.services.url_safety import UnsafeURL, check_public_url
from app.services.user_settings import apify_token, lock_settings_row, settings_for
from app.services.work_queue import transition
from app.services.workflow import (
    AWAITING_TAILORING_STATUSES,
    NEEDS_RETAILOR,
    approve_job,
    draft_followup,
    followup_days,
    get_profile,
    resume_edits,
    update_resume_edit,
)
from app.settings import get_settings

router = APIRouter()

# "archive" puts a job away without judging it: it leaves the queue and is not ranked, but unlike a dismissal it
# teaches the domain filter nothing (app.services.feedback_learning).
TRIAGE_ACTIONS = {"shortlist": "shortlisted", "maybe": "maybe", "skip": "dismissed", "dismiss": "dismissed",
                  "archive": "archived", "restore": "discovered"}
QUEUE_STATUSES = ("discovered", "shortlisted", "archived")
# Words too common in gap names to tie a gap to a claim ("Security engineering" is not partly met by any
# "...engineering..." claim).
GENERIC_GAP_WORDS = frozenset({"engineering", "engineer", "software", "experience", "systems", "system", "development",
                               "strong", "deep", "knowledge", "skills", "proficiency", "and", "the", "with"})
AGENCY_HINTS = ("staffing", "recruit", "consultan", "talent", "placement", "manpower", "hr services", "headhunt")
BOARDS = ["linkedin", "indeed", "glassdoor", "naukri", "bayt", "zip_recruiter"]
DEFAULT_PREFS = {"weekly_target": 10, "followup_days": None, "email_run_sheet": False, "warn_closing": True,
                 "extra_stages": []}


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def setting_row(db: Session) -> AppSetting:
    return settings_for(db)


def prefs(db: Session) -> dict:
    merged = {**DEFAULT_PREFS, **(setting_row(db).preferences or {})}
    merged["followup_days"] = followup_days(db)
    return merged


def board_label(source: str) -> str:
    key = (source or "").split(":", 1)[-1]
    return {"linkedin": "LinkedIn", "indeed": "Indeed", "glassdoor": "Glassdoor", "naukri": "Naukri",
            "bayt": "Bayt", "zip_recruiter": "ZipRecruiter", "manual": "Manual", "lever": "Lever",
            "greenhouse": "Greenhouse", "unknown": "Other"}.get(key, key.replace("_", " ").title() or "Other")


# Easy Apply (apply on the board itself) is read from explicit signals only, never guessed: a flag the board put on
# the item, or a search that asked the board for Easy Apply jobs only. A job with neither is simply not marked.
EASY_APPLY_FLAGS = ("easyApply", "easy_apply", "isEasyApply", "is_easy_apply", "directApply", "directapply",
                    "direct_apply")
EASY_APPLY_TYPES = ("applyType", "apply_type", "applicationType", "application_type", "applyMethod", "apply_method")
# Boards the Multi Job Board actor (JobSpy) restricts to Easy Apply when its easyApply input is on. Google Jobs and
# BDJobs ignore the input, so their jobs are not marked by the run alone.
EASY_APPLY_BOARDS = {"linkedin", "indeed", "glassdoor", "zip_recruiter", "naukri", "bayt"}


def easy_apply(job: Job, run_input: dict | None = None) -> bool:
    raw = job.raw if isinstance(job.raw, dict) else {}
    for key in EASY_APPLY_FLAGS:
        value = raw.get(key)
        if value is True or (isinstance(value, str) and value.strip().lower() in ("true", "yes", "1")):
            return True
    for key in EASY_APPLY_TYPES:
        if "easyapply" in re.sub(r"[^a-z]", "", str(raw.get(key) or "").lower()):
            return True
    run_input = run_input or {}
    if run_input.get("directApply") is True:  # Advanced LinkedIn: every item it returns is Easy Apply
        return True
    return run_input.get("easyApply") is True and (job.source or "").split(":", 1)[-1] in EASY_APPLY_BOARDS


def run_inputs(db: Session, jobs: list[Job]) -> dict[int, dict]:
    """The actor input of each job's search run, in one query."""
    ids = {j.search_run_id for j in jobs if j.search_run_id}
    if not ids:
        return {}
    return {run.id: run.input_json or {} for run in db.scalars(select(SearchRun).where(SearchRun.id.in_(ids)))}


def is_agency(company: str) -> bool:
    lowered = (company or "").lower()
    return any(hint in lowered for hint in AGENCY_HINTS)


def ago(value: datetime | None) -> str:
    if not value:
        return "—"
    seconds = max(0, (utcnow() - value).total_seconds())
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def day_label(value: datetime | None) -> str:
    return value.strftime("%-d %b") if value else ""


def search_prefs(db: Session) -> dict:
    return dict(get_profile(db).search_preferences or {})


def excluded_companies(db: Session) -> set[str]:
    return {c.strip().lower() for c in search_prefs(db).get("exclude_companies") or [] if c.strip()}


def latest_resume(db: Session, job_id: int) -> ResumeVariant | None:
    return db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job_id)
                     .order_by(desc(ResumeVariant.version)).limit(1))


def requirement_names(analysis: dict) -> dict[str, str]:
    """Requirement id -> text, from a ranking's assessments. Older rankings may list "req_003" instead of the text."""
    return {str(a["requirement_id"]): str(a["requirement"]) for a in analysis.get("requirement_assessments") or []
            if isinstance(a, dict) and a.get("requirement_id") and a.get("requirement")}


def ranked_list(analysis: dict, key: str, names: dict[str, str] | None = None) -> list:
    names = requirement_names(analysis) if names is None else names
    return [names.get(item, item) if isinstance(item, str) else item for item in analysis.get(key) or []]


def job_card(job: Job) -> dict:
    analysis = job.fit_analysis or {}
    names = requirement_names(analysis)
    app = job.application
    return {
        "id": job.id, "title": job.title, "company": job.company, "location": job.location, "url": job.url,
        "source": job.source, "board": board_label(job.source),
        "fit": None if job.fit_score is None else round(job.fit_score),
        # "posted" is when *we* found the job (kept for older screens); the employer's own date is posted_at, and
        # stays null when the board did not give one (posted_date_raw keeps whatever text it did give).
        "status": job.status, "discovered_at": job.discovered_at.isoformat(), "posted": ago(job.discovered_at),
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
        "posted_ago": ago(job.posted_at) if job.posted_at else None, "posted_raw": job.posted_date_raw or None,
        "matches": ranked_list(analysis, "strong_matches", names), "gaps": ranked_list(analysis, "gaps", names),
        "missing": ranked_list(analysis, "must_have_missing", names), "keywords": analysis.get("keywords") or [],
        "summary": analysis.get("summary") or "", "strategy": analysis.get("application_strategy") or "",
        "snippet": (job.description or "")[:360], "agency": is_agency(job.company),
        # Ranking waits for a real posting (app.services.ranker.missing_description); the UI asks for one.
        "needs_description": ranker.missing_description(job),
        # Domain filter (app.services.domain_match): NOT_MATCHING jobs are hidden from Discovery unless asked for.
        "domain_match": job.domain_match, "domain_reason": job.domain_reason,
        # The resume ranking and tailoring use for this job; None = the active resume. (resume_id in the job detail
        # is its tailored packet.)
        "resume_choice_id": job.resume_id, "resume_choice": job.resume_choice,
        "search_run_id": job.search_run_id, "application_id": app.id if app else None,
        "application_status": app.status if app else None, "stage": app.stage if app else None,
        "rejection": rejection_card(job), "starred": bool(job.starred),
        # The listing check found the posting closed but kept the job (approved): app.services.job_removal.
        "posting_closed": ({"at": job.posting_closed_at.isoformat(), "ago": ago(job.posting_closed_at),
                            "reason": job.posting_closed_reason} if job.posting_closed_at else None),
    }


# How a job's resume was chosen (Job.resume_choice), in the words the job and Tailor screens show.
RESUME_CHOICE_LABELS = {
    "AUTO": "Matched by resume tags",
    "SETUP": "Pinned by the search setup",
    "USER": "Chosen by you",
    "ACTIVE": "Pinned to the active resume",
}


def resume_ref(resume: Resume | None) -> dict | None:
    """A resume version as the screens name it: never its text."""
    if resume is None:
        return None
    return {"id": resume.id, "name": resume.name or f"Version {resume.version}", "version": resume.version,
            "is_active": bool(resume.is_active), "domains": resume_matching.groups(resume)}


def resume_in_use(db: Session, job: Job) -> dict | None:
    """The resume ranking and tailoring read for this job now, resolved the way resume_verification.for_job resolves
    it (the job's own resume, else the active one), and why. Read-only: never activates or re-matches anything."""
    profile = get_profile(db)
    chosen = resume_verification.chosen_resume(db, profile, job)
    resume = chosen or resume_verification.active_resume(db, profile, create=False)
    if resume is None:
        return None
    choice = job.resume_choice if chosen is not None or job.resume_choice == "ACTIVE" else None
    rematch = stale_match(db, job, resume.id)
    if choice in RESUME_CHOICE_LABELS:
        how = RESUME_CHOICE_LABELS[choice]
        if choice == "AUTO" and (reason := resume_matching.score(resume, job).reason):
            how = f"{how} ({reason})"
    elif job.resume_id is not None:
        how = "The chosen resume is gone: the active resume is used"
    elif rematch is not None:
        how = "No resume matched by tags when this job was found: the active resume is used"
    else:
        how = "No resume matched by tags clearly: the active resume is used"
    return {**resume_ref(resume), "choice": choice, "how": how, "rematch": rematch}


def stale_match(db: Session, job: Job, in_use_id: int | None = None) -> dict | None:
    """What matching by today's tags would change for a job: the resume they pick (None: the active one) and why.

    A job matched automatically (or never) may have been matched before its tags changed: any difference is offered.
    A pinned job (by the person or its search) is offered the tag match only when the tags clearly pick a resume other
    than the one it uses (`pinned`: taking it replaces that choice). None when nothing would change. Read-only:
    re-matching stays the person's action, since a ranked job is ranked again."""
    match = resume_matching.pick(resume_matching.tagged_resumes(db, job.user_id), job)
    target = match.resume_id if match else None
    pinned = job.resume_choice not in resume_matching.REMATCHABLE
    if (target is None or target == in_use_id) if pinned else target == job.resume_id:
        return None
    resume = db.get(Resume, target) if target is not None else None
    return {"resume": resume_ref(resume), "reason": match.reason if match else "no resume matches clearly now",
            "pinned": pinned}


def packet_source_resume(db: Session, job: Job, variant: ResumeVariant, now: dict | None = None) -> dict:
    """The resume version a saved packet was tailored from, as its own content records it (the tailoring context's
    resume id, or an untailored packet's source), and whether that is still the resume the job would use now."""
    content = variant.content or {}
    source_id = (content.get("context") or {}).get("id") or content.get("source_resume_id")
    resume = db.scalar(select(Resume).where(Resume.id == source_id)) if source_id else None
    now = now if now is not None else resume_in_use(db, job)
    return {"recorded": source_id is not None, "resume": resume_ref(resume),
            "deleted": source_id is not None and resume is None,
            "differs": bool(source_id and now and now["id"] != source_id)}


def get_job(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


# ───────────────────────────── summary / overview / today ─────────────────────────────

def triage_query(db: Session):
    query = select(Job).where(Job.triaged_at.is_(None), Job.status.in_(QUEUE_STATUSES), domain_match.not_held())
    excluded = excluded_companies(db)
    if excluded:
        query = query.where(func.lower(Job.company).not_in(excluded))
    return query


def reviewed_or_approved():
    """A packet belongs in Approvals once its latest version has been reviewed. Tailoring saves the packet (status
    "tailored") before the Reviewer runs; until the review is stored, or when it failed, the job stays on Tailor.
    Approved jobs always qualify: approval needed a reviewed packet, and the packet is locked after it."""
    latest = select(func.max(ResumeVariant.version)).where(ResumeVariant.job_id == Job.id) \
        .correlate(Job).scalar_subquery()
    reviewed = exists().where(ResumeVariant.job_id == Job.id, ResumeVariant.version == latest,
                              ResumeVariant.reviewer_score.is_not(None))
    return or_(Job.status == "approved", reviewed)


def approval_jobs(db: Session) -> list[Job]:
    return db.scalars(select(Job).where(Job.status.in_(("tailored", "pending_approval")), reviewed_or_approved())
                      .order_by(desc(Job.fit_score))).all()


def live_apps(db: Session) -> list[Application]:
    return db.scalars(select(Application).where(Application.applied_at.is_not(None))).all()


def scheduled_searches(db: Session) -> dict:
    """What the sidebar's auto-search card says: how many setups run on their own, and how wide they search."""
    setups = db.scalars(select(SavedSearch).where(SavedSearch.enabled.is_(True),
                                                  SavedSearch.schedule_kind != "manual")).all()
    return {"schedule": "daily" if setups else "manual", "setups": len(setups),
            "next_run": next(iter(sorted(s.next_run_at.isoformat() for s in setups if s.next_run_at)), None),
            "roles": len({role for s in setups for role in (s.settings or {}).get("roles") or []}),
            "locations": len({place for s in setups for place in (s.settings or {}).get("locations") or []})}


@router.get("/api/workspace/summary")
def summary(db: Session = Depends(get_user_db)):
    row = setting_row(db)
    profile = get_profile(db)
    apps = live_apps(db)
    live = [a for a in apps if (a.stage or "applied") not in INACTIVE_STAGES]
    tailor_count = db.scalar(select(func.count(Job.id)).where(Job.status.in_(AWAITING_TAILORING_STATUSES),
                                                              Job.triaged_at.is_not(None),
                                                              or_(Job.status == NEEDS_RETAILOR, ~Job.resumes.any()))) or 0
    last_run = db.scalar(select(SearchRun).order_by(desc(SearchRun.id)).limit(1))
    approved_at = db.scalar(select(func.max(Application.approved_at)))
    return {
        # Approved packets the person has not applied with yet; `latest_at` is when the newest approval happened (the
        # browser compares it with the one it last announced to raise a desktop notification).
        "approved": {"count": db.scalar(select(func.count(Application.id)).where(Application.status == "approved")) or 0,
                     "latest_at": approved_at.isoformat() if approved_at else None},
        "counts": {"today": len(today_actions(db)), "discovery": db.scalar(select(func.count()).select_from(triage_query(db).subquery())) or 0,
                   "tailor": tailor_count, "approvals": len(approval_jobs(db)), "tracker": len(live)},
        "profile": {"name": profile.name, "headline": profile.headline},
        "llm_provider": row.llm_provider, "model": row.model, "application_mode": row.application_mode,
        "auto_search": {**scheduled_searches(db),
                        "last_run": last_run.created_at.isoformat() if last_run else None,
                        "last_run_status": last_run.status if last_run else None},
        "total_jobs": db.scalar(select(func.count(Job.id))) or 0,
    }


def _pct(n: int, total: int) -> str:
    return f"{(100 * n / total):.0f}%" if total else "0%"


OUTCOME_DAYS = 90  # replies lag discovery, so outcome metrics look further back than the 30-day funnel
GHOSTED_DAYS = 21  # no answer after three weeks counts as no response
OFFER_STAGES = {"offer", "negotiating"}
WAITING_STAGES = {"applied", "acknowledged"}
FIT_BANDS = [(90, "90+"), (75, "75–89"), (60, "60–74"), (0, "Under 60")]
# Application Inbox event types (app.models.EMAIL_EVENT_TYPES) grouped the way the Overview counts company emails.
INBOX_GROUPS = [
    ("interviews", "Interview invites", {"INTERVIEW_INVITATION", "INTERVIEW_RESCHEDULE"}),
    ("assessments", "Assessments", {"ASSESSMENT_INVITATION", "ASSESSMENT_REMINDER"}),
    ("recruiter", "Recruiter replies", {"RECRUITER_RESPONSE", "FOLLOWUP_RESPONSE", "ADDITIONAL_INFORMATION_REQUEST",
                                        "DOCUMENT_REQUEST"}),
    ("rejections", "Rejections", {"APPLICATION_REJECTION"}),
    ("offers", "Offers", {"OFFER", "OFFER_UPDATE"}),
]


# Emails a person marked as an answer to an application without moving its stage. Rejections and on-hold notices
# answer, but are not a reply that moves the application on.
NOT_A_REPLY = {"APPLICATION_REJECTION", "APPLICATION_ON_HOLD"}


def _reached(app: Application, stages: set[str]) -> bool:
    """Whether an application is in, or ever was in, one of these stages: a rejection after a screening call still
    counts as a reply. A stage move an Inbox email made and a correction later undid (application_inbox.service.
    _withdraw keeps it as history) never counts: the email was about another application."""
    if (app.stage or "applied") in stages:
        return True
    undone = {(e.meta or {}).get("email_event_id") for e in app.events
              if (e.meta or {}).get("source") == "application_inbox_correction"}
    return any(e.kind == "stage" and e.detail in stages
               and not ((e.meta or {}).get("email_event_id") in undone
                        and (e.meta or {}).get("source") != "application_inbox_correction")
               for e in app.events)


def _reply_emails(db: Session, apps: list[Application]) -> dict[int, set[str]]:
    """application id -> the employer's reply types the Application Inbox matched to it since it was sent (the same
    rule as application_inbox.service.reply_received). Stored events only; never Gmail."""
    sent = {a.id: a.applied_at for a in apps}
    if not sent:
        return {}
    rows = db.execute(select(ApplicationCommunicationEvent.application_id, ApplicationCommunicationEvent.event_type,
                             ApplicationCommunicationEvent.received_at)
                      .where(ApplicationCommunicationEvent.application_id.in_(sent),
                             ApplicationCommunicationEvent.event_type.in_(HUMAN_REPLY_TYPES),
                             ApplicationCommunicationEvent.review_status != "DISMISSED")).all()
    emails: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        if row.received_at is None or row.received_at >= sent[row.application_id]:
            emails[row.application_id].add(row.event_type)
    return emails


def _replied(app: Application, emails: dict[int, set[str]]) -> bool:
    """The employer replied: the application reached a reply stage, or the Inbox saw a reply that moves it on."""
    return _reached(app, REPLY_STAGES) or bool(emails.get(app.id, set()) - NOT_A_REPLY)


def _answered(app: Application, emails: dict[int, set[str]]) -> bool:
    """The employer answered at all, rejections included (a rejection later closed still counts)."""
    return _replied(app, emails) or _reached(app, {"rejected"}) or bool(emails.get(app.id))


# Level words a role is grouped without: "Staff Software Engineer" and "Software Engineer II" are both Software Engineer.
LEVEL_WORDS = frozenset({"senior", "sr", "staff", "lead", "principal", "junior", "jr", "associate", "intern", "head",
                         "chief", "i", "ii", "iii", "iv", "1", "2", "3", "mid", "level", "entry"})
# Role types for titles that match none of the person's target roles, checked in order (the first match wins, so
# "AI Forward Deployed Engineer" is Forward Deployed / Solutions and "ML Engineering Manager" is Engineering Manager).
ROLE_TYPES = [
    ("Engineering Manager", (r"\b(engineering|software|development) manager\b|\bhead of (engineering|ai|ml|data)\b"
                             r"|\bdirector of engineering\b|\bvp,? engineering\b")),
    ("Product Manager", r"\bproduct (manager|owner|lead|management)\b|\bapm\b"),
    ("Program Manager", r"\b(program|project|delivery) manager\b|\btpm\b"),
    ("Forward Deployed / Solutions", (r"\bforward[- ]deployed\b|\bsolutions? (engineer|architect)\b"
                                      r"|\b(sales|customer|field) engineer\b")),
    ("AI/ML Engineer", (r"\b(ai|ml|machine learning|llm|genai|gen ai|generative ai|deep learning|nlp|computer vision"
                        r"|mlops)\b.*\b(engineer|developer|architect|specialist|lead|engineering)\b"
                        r"|\b(engineer|developer)\b.*\b(ai|ml|machine learning|llm|genai|generative ai)\b")),
    ("Data Scientist", r"\b(data|research|applied|ai|ml) scientist\b|\bdata science\b|\bscientist\b"),
    ("Data Engineer", r"\b(data|analytics|etl|big data) (engineer|platform|architect)\b"),
    ("Platform / DevOps / SRE", r"\b(devops|sre|site reliability|platform|infrastructure|cloud|reliability)\b"),
    ("Software Engineer", (r"\b(software|backend|back end|full ?stack|frontend|front end|python|java|golang|web|mobile"
                           r"|ios|android|application) (engineer|developer|development|engineering)\b"
                           r"|\b(developer|programmer|sde|swe)\b")),
    ("Architect", r"\barchitect\b"),
    ("Analyst", r"\banalyst\b"),
]


def _title_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9+#]+", (text or "").lower())


def _role_bucket(title: str, roles: list[str]) -> str:
    """The role a job title belongs to: one of the person's target roles when its words (levels aside) are all in the
    title, else a role type (ROLE_TYPES), else the title itself without its level. Shared by role mix and replies by
    role, so both group jobs the same way. Deterministic: no model decides a group."""
    words = set(_title_words(title))
    ordered = sorted(roles, key=lambda r: -len(_title_words(r)))  # the most specific target role wins
    # A target role named in full ("Associate Product Manager"), then one whose words without levels are there.
    for strip in (frozenset(), LEVEL_WORDS):
        for role in ordered:
            core = set(_title_words(role)) - strip
            if core and core <= words:
                return role
    lowered = " ".join(_title_words(title))
    for label, pattern in ROLE_TYPES:
        if re.search(pattern, lowered):
            return label
    # Nothing recognised: the title before any " - ", "(", "|" or "," qualifier, without level words.
    head = re.split(r"\s[-–—|]\s|[(|,/]", title or "")[0]
    kept = [w for w in head.split() if w.lower().strip(".") not in LEVEL_WORDS]
    return " ".join(kept[:4]).title() if kept else "Other roles"


def _fit_band(score: float | None) -> str:
    if score is None:
        return "Unscored"
    return next(label for floor, label in FIT_BANDS if score >= floor)


def _outcome_rows(apps: list[Application], key, emails: dict[int, set[str]], limit: int = 6,
                  order: list[str] | None = None) -> list[dict]:
    """Applications grouped by key(app): how many were sent, answered, interviewed and rejected in each group.

    Sorted by applications sent (or by `order`); groups past `limit` are folded into "Other"."""
    groups: dict[str, list[Application]] = defaultdict(list)
    for app in apps:
        groups[key(app)].append(app)
    labels = ([label for label in order if label in groups] if order
              else sorted(groups, key=lambda label: (-len(groups[label]), label.lower())))
    if not order and len(labels) > limit:
        for label in labels[limit - 1:]:
            if label != "Other":
                groups["Other"].extend(groups.pop(label))
        labels = [*(label for label in labels[:limit - 1] if label != "Other"), "Other"]
    rows = []
    for label in labels:
        members = groups[label]
        replies = sum(1 for a in members if _replied(a, emails))
        rows.append({"label": label, "applied": len(members), "replies": replies,
                     "interviews": sum(1 for a in members if _reached(a, INTERVIEW_STAGES)),
                     "rejected": sum(1 for a in members if _reached(a, {"rejected"})),
                     "rate": _pct(replies, len(members)) if members else "—"})
    return rows


def _outcomes(apps: list[Application], now: datetime, emails: dict[int, set[str]]) -> dict:
    """Where each application sent in the outcome window stands now. Every application lands in exactly one bucket."""
    counts = Counter()
    for app in apps:
        stage = app.stage or "applied"
        if stage == "rejected":
            counts["rejected"] += 1
        elif _reached(app, OFFER_STAGES):
            counts["offer"] += 1
        elif stage in INTERVIEW_STAGES:
            counts["interviewing"] += 1
        elif stage in ("closed", "onhold"):
            counts["closed"] += 1
        elif _answered(app, emails):
            counts["in_process"] += 1
        elif app.applied_at < now - timedelta(days=GHOSTED_DAYS):
            counts["no_response"] += 1
        else:
            counts["awaiting"] += 1
    answered = sum(1 for a in apps if _answered(a, emails))
    buckets = [("awaiting", "Awaiting reply", "#c6cad8"), ("no_response", f"No reply after {GHOSTED_DAYS} days", "#9aa1b2"),
               ("in_process", "Replied", "#a79bf0"), ("interviewing", "Interviewing", "#0f9c86"),
               ("offer", "Offer", "#0e7a68"), ("rejected", "Rejected", "#c22f4e"), ("closed", "Closed or on hold", "#e0a136")]
    return {"total": len(apps), "response_rate": _pct(answered, len(apps)) if apps else "—", "answered": answered,
            "buckets": [{"id": key, "label": label, "n": counts[key], "pct": _pct(counts[key], len(apps)), "color": color}
                        for key, label, color in buckets if counts[key]]}


def _inbox_counts(db: Session, since: datetime) -> dict | None:
    """Company emails the Application Inbox matched to applications since `since`; None when Gmail is not connected.
    Reads stored events only — never Gmail."""
    if db.scalar(select(GmailConnection.id).where(GmailConnection.status == "CONNECTED").limit(1)) is None:
        return None
    rows = db.execute(select(ApplicationCommunicationEvent.event_type, ApplicationCommunicationEvent.requires_action,
                             ApplicationCommunicationEvent.review_status)
                      .where(ApplicationCommunicationEvent.application_id.is_not(None),
                             ApplicationCommunicationEvent.review_status != "DISMISSED",
                             func.coalesce(ApplicationCommunicationEvent.received_at,
                                           ApplicationCommunicationEvent.created_at) >= since)).all()
    types = Counter(row.event_type for row in rows)
    return {"groups": [{"id": key, "label": label, "n": sum(types[t] for t in members)}
                       for key, label, members in INBOX_GROUPS],
            "total": len(rows),
            "needs_action": sum(1 for row in rows if row.requires_action and row.review_status == "OPEN")}


@router.get("/api/workspace/overview")
def overview(db: Session = Depends(get_user_db)):
    since = utcnow() - timedelta(days=30)
    jobs = db.scalars(select(Job).where(Job.discovered_at >= since)).all()
    total = len(jobs)
    emails = _reply_emails(db, live_apps(db))
    shortlisted = [j for j in jobs if j.status not in ("discovered", "archived", "dismissed", "maybe")]
    tailored = [j for j in jobs if j.resumes]
    approved = [j for j in jobs if j.application and j.application.approved_at]
    applied = [j for j in jobs if j.application and j.application.applied_at]
    interviewing = [j for j in applied if (j.application.stage or "") in INTERVIEW_STAGES]
    funnel = [
        ("Discovered", len(jobs), "", "#c6cad8", "discovery"),
        ("Shortlisted", len(shortlisted), f"{_pct(len(shortlisted), total)} kept", "#a79bf0", "discovery"),
        ("Tailored", len(tailored), f"{_pct(len(tailored), len(shortlisted))} of shortlist", "#7c69ee", "tailor"),
        ("Approved", len(approved), f"{_pct(len(approved), len(tailored))} pass review", "#5b4ae8", "approvals"),
        ("Applied", len(applied), f"{_pct(len(applied), len(approved))} submitted", "#0f9c86", "tracker"),
        ("Interviewing", len(interviewing), f"{_pct(len(interviewing), len(applied))} of applied", "#0e7a68", "tracker"),
    ]

    by_board: dict[str, dict] = defaultdict(lambda: {"found": 0, "applied": 0, "replies": 0})
    for job in jobs:
        row = by_board[board_label(job.source)]
        row["found"] += 1
        if job.application and job.application.applied_at:
            row["applied"] += 1
            if _replied(job.application, emails):
                row["replies"] += 1
    source_perf = sorted(({"board": k, **v, "rate": _pct(v["replies"], v["applied"]) if v["applied"] else "—"}
                          for k, v in by_board.items()), key=lambda r: -r["found"])

    now = utcnow()
    upcoming = []
    for app in live_apps(db):
        if (app.stage or "applied") in INACTIVE_STAGES:
            continue
        if app.followup_due_at and app.followup_due_at <= now + timedelta(days=10) and app.stage == "applied":
            upcoming.append({"date": app.followup_due_at.isoformat(), "label": f"Follow-up · {app.job.company}",
                             "kind": "nudge", "job_id": app.job_id})
        for event in app.events:
            if event.kind == "interview" and now <= event.occurred_at <= now + timedelta(days=10):
                upcoming.append({"date": event.occurred_at.isoformat(), "label": f"{event.title} · {app.job.company}",
                                 "kind": "interview", "job_id": app.job_id})
    upcoming.sort(key=lambda u: u["date"])

    target = prefs(db)["weekly_target"]
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    weeks = []
    all_applied = [a.applied_at for a in live_apps(db)]
    for offset in range(5, -1, -1):
        start = week_start - timedelta(weeks=offset)
        end = start + timedelta(weeks=1)
        weeks.append({"label": f"W{start.isocalendar()[1]}", "n": sum(1 for d in all_applied if start <= d < end)})
    peak = max([target, *[w["n"] for w in weeks]]) or 1
    for w in weeks:
        w["pct"] = f"{100 * w['n'] / peak:.0f}%"

    roles = [r for r in get_profile(db).target_roles or [] if r.strip()]
    mix = Counter(_role_bucket(job.title, roles) for job in jobs)
    role_mix = [{"label": label, "n": n, "pct": _pct(n, total)} for label, n in mix.most_common(4)]
    fits = [j.fit_score for j in jobs if j.fit_score is not None]

    def avg_days(pairs):
        values = [(b - a).total_seconds() / 86400 for a, b in pairs if a and b and b >= a]
        return round(sum(values) / len(values), 1) if values else None

    triage_pairs = [(j.discovered_at, j.triaged_at) for j in jobs]
    apply_pairs = [(j.triaged_at or j.discovered_at, j.application.applied_at) for j in applied]
    reply_pairs = []
    for j in applied:
        reply = next((e.occurred_at for e in j.application.events
                      if e.kind == "stage" and e.detail in REPLY_STAGES), None)
        reply_pairs.append((j.application.applied_at, reply))
    stale = [j for j in approved if not j.application.applied_at and j.application.approved_at < now - timedelta(days=2)]
    sent = [a for a in live_apps(db) if a.applied_at >= now - timedelta(days=OUTCOME_DAYS)]
    passed = Counter(db.scalars(select(func.coalesce(Job.rejection_reason, "")).where(Job.rejected_at >= since)).all())
    pass_reasons = [{"id": key or None, "label": REJECTION_REASONS.get(key, "No reason given"), "n": n,
                     "pct": _pct(n, sum(passed.values()))} for key, n in passed.most_common(4)]
    return {
        "total": total, "funnel": [{"label": l, "n": n, "conv": c, "color": col, "screen": s,
                                    "pct": _pct(n, total) if total else "0%"} for l, n, c, col, s in funnel],
        "source_perf": source_perf, "upcoming": upcoming[:6], "weeks": weeks, "weekly_target": target,
        "role_mix": role_mix, "median_fit": round(median(fits)) if fits else None,
        "time_in_stage": {"triage": avg_days(triage_pairs), "apply": avg_days(apply_pairs), "reply": avg_days(reply_pairs)},
        "stale_approved": len(stale), "approvals": len(approval_jobs(db)),
        "outcome_days": OUTCOME_DAYS,
        "by_location": _outcome_rows(sent, lambda a: location_key(a.job.location), emails),
        "by_role": _outcome_rows(sent, lambda a: _role_bucket(a.job.title, roles), emails),
        "fit_vs_outcome": _outcome_rows(sent, lambda a: _fit_band(a.job.fit_score), emails,
                                        order=[label for _, label in FIT_BANDS] + ["Unscored"]),
        "outcomes": _outcomes(sent, now, emails), "inbox": _inbox_counts(db, since), "pass_reasons": pass_reasons,
    }


def today_actions(db: Session) -> list[dict]:
    now = utcnow()
    actions = []
    for job in approval_jobs(db):
        resume = latest_resume(db, job.id)
        if resume and resume.reviewer_score is not None:
            gaps = len((job.fit_analysis or {}).get("gaps") or [])
            actions.append({"kind": "approve", "job_id": job.id, "company": job.company, "title": job.title,
                            "fit": job.fit_score, "priority": 0, "minutes": 6,
                            "reason": f"Tailored packet scored {resume.reviewer_score:.0f} with the reviewer."
                                      + (f" {gaps} gap flag{'s' if gaps != 1 else ''} to check." if gaps else "")})
    for job in db.scalars(select(Job).where(Job.status.in_(AWAITING_TAILORING_STATUSES), Job.triaged_at.is_not(None),
                                            or_(Job.status == NEEDS_RETAILOR, ~Job.resumes.any()))
                          .order_by(desc(Job.fit_score)).limit(5)).all():
        matches = len((job.fit_analysis or {}).get("strong_matches") or [])
        if job.status == NEEDS_RETAILOR:
            resume = latest_resume(db, job.id)
            reasons = (resume.reviewer_notes or {}).get("decision_reasons") if resume else None
            reason = "Review sent this packet back to be tailored again." + (f" {reasons[0]}" if reasons else "")
        else:
            reason = (f"Shortlisted {ago(job.triaged_at or job.discovered_at)}."
                      + (f" {matches} strong matches in your evidence." if matches else ""))
        actions.append({"kind": "tailor", "job_id": job.id, "company": job.company, "title": job.title,
                        "fit": job.fit_score, "priority": 1, "minutes": 12, "reason": reason})
    for app in live_apps(db):
        if app.stage == "applied" and app.followup_due_at and app.followup_due_at <= now:
            days = (now - app.applied_at).days
            actions.append({"kind": "follow_up", "job_id": app.job_id, "application_id": app.id,
                            "company": app.job.company, "title": app.job.title, "fit": app.job.fit_score,
                            "priority": 2, "minutes": 5,
                            "reason": f"Applied {days} day{'s' if days != 1 else ''} ago with no reply logged."})
    actions.sort(key=lambda a: (a["priority"], -(a["fit"] or 0)))
    return actions


@router.get("/api/workspace/today")
def today(db: Session = Depends(get_user_db)):
    now = utcnow()
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    apps = live_apps(db)
    this_week = [a for a in apps if a.applied_at >= week_start]
    stage_events = db.scalars(select(ApplicationEvent).where(ApplicationEvent.kind == "stage",
                                                             ApplicationEvent.occurred_at >= week_start)).all()
    replies_week = len({e.application_id for e in stage_events if e.detail in REPLY_STAGES})
    interviews_week = len({e.application_id for e in stage_events if e.detail in INTERVIEW_STAGES})
    replied = [a for a in apps if (a.stage or "") in REPLY_STAGES]
    followups = []
    for app in sorted(apps, key=lambda a: a.followup_due_at or now):
        if app.stage != "applied" or not app.followup_due_at or app.followup_due_at > now + timedelta(days=3):
            continue
        delta = (app.followup_due_at.date() - now.date()).days
        due = f"Overdue {-delta}d" if delta < 0 else "Due today" if delta == 0 else f"In {delta} days"
        followups.append({"application_id": app.id, "job_id": app.job_id, "company": app.job.company,
                          "role": app.job.title, "due": due, "tone": "overdue" if delta < 0 else "today" if delta == 0 else "soon",
                          "detail": f"Applied {day_label(app.applied_at)} · "
                                    + (f"recruiter {app.recruiter_name}" if app.recruiter_name else "no contact yet")})
    days = []
    for i in range(7):
        start = week_start + timedelta(days=i)
        days.append({"label": "MTWTFSS"[i], "n": sum(1 for a in this_week if start <= a.applied_at < start + timedelta(days=1)),
                     "future": start.date() > now.date()})
    pending_triage = db.scalar(select(func.count()).select_from(triage_query(db).subquery())) or 0
    triaged_today = db.scalar(select(func.count(Job.id)).where(Job.triaged_at >= now.replace(hour=0, minute=0))) or 0
    unverified = db.scalar(select(func.count(KnowledgeAnswer.id)).where(KnowledgeAnswer.verified.is_(False))) or 0
    target = prefs(db)["weekly_target"]
    overview_data = overview(db)
    return {
        "date": now.isoformat(), "actions": today_actions(db)[:5], "followups": followups[:5],
        "week": {"applied": len(this_week), "target": target, "replies": replies_week, "interviews": interviews_week,
                 "response_rate": _pct(len(replied), len(apps)) if apps else "—", "days": days},
        "funnel": overview_data["funnel"], "pending_triage": pending_triage, "triaged_today": triaged_today,
        "approvals": len(approval_jobs(db)), "unverified_answers": unverified,
    }


# ───────────────────────────── discovery ─────────────────────────────

def _dedupe_key(job: Job) -> str:
    return re.sub(r"[^a-z0-9]+", " ", f"{job.company}|{job.title}".lower()).strip()


# Discovery's date filter: how far back "found" (discovered_at) or "posted" (the employer's date) may be.
DATE_WINDOWS = {"24h": timedelta(hours=24), "3d": timedelta(days=3), "7d": timedelta(days=7),
                "15d": timedelta(days=15), "30d": timedelta(days=30), "60d": timedelta(days=60),
                "90d": timedelta(days=90)}
# Work mode as the boards state it (app.services.apify_adapters._remote): remote, hybrid, or not stated.
WORK_MODES = ("remote", "hybrid", "onsite_unknown")


def _day(value: str, name: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, f"{name} must be a date (YYYY-MM-DD)") from None


def _work_mode(job: Job) -> str:
    mode = (job.remote_type or "").lower()
    return mode if mode in ("remote", "hybrid") else "onsite_unknown"


def _has_salary(job: Job) -> bool:
    """Only a salary the posting states (or one already extracted for it); never an estimate. The stored envelope
    (compensation.enrich_compensation) exists for checked jobs without a salary too, so only its `posting` counts."""
    from app.services.compensation import extract_posting_salary

    stored = job.compensation if isinstance(job.compensation, dict) else {}
    return bool(stored.get("posting")) or extract_posting_salary(job.raw) is not None


@router.get("/api/jobs/search")
def jobs_search(q: str = "", min_fit: int = 0, max_fit: int = 0, board: str = "all", location: str = "all",
                stage: str = "all", description: str = "all", sort: str = "fit", within: str = "",
                older_than: str = "", date_from: str = "", date_to: str = "", posted_unknown: bool = False,
                date_field: str = "found", include_applied: bool = False, work_mode: str = "all",
                job_type: str = "all", has_salary: bool = False, setup_id: int | None = None,
                page: int = Query(1, ge=1), per_page: int = Query(25, ge=1, le=200), queue: bool = False,
                hide_agencies: bool = False, hide_reposts: bool = False, keywords: str = "",
                domain: str = "matching", ids_only: bool = False, db: Session = Depends(get_user_db)):
    from app.services import job_description  # local import, as app.services.sources does

    if description not in ("all", "") and description not in job_description.DESCRIPTION_STATES:
        raise HTTPException(422, f"Unknown description filter: {description}")
    for window in (within, older_than):
        if window and window not in DATE_WINDOWS:
            raise HTTPException(422, f"Unknown date window: {window}")
    if date_field not in ("found", "posted"):
        raise HTTPException(422, "date_field must be found or posted")
    if domain not in ("matching", "not_matching", "all"):
        raise HTTPException(422, "domain must be matching, not_matching or all")
    if work_mode not in ("all", "", *WORK_MODES):
        raise HTTPException(422, "work_mode must be remote, hybrid or onsite_unknown")
    now = utcnow()
    newest = now - DATE_WINDOWS[older_than] if older_than else None  # older than: before this moment
    cutoff = now - DATE_WINDOWS[within] if within else None
    day_from, day_to = _day(date_from, "date_from"), _day(date_to, "date_to")
    if day_to is not None:
        day_to += timedelta(days=1)  # inclusive: the whole of the last day
    if day_from and day_to and day_from >= day_to:
        raise HTTPException(422, "date_from must not be after date_to")
    # Without a posting date a job is unknown for every window; "posted_unknown" lists exactly those.
    unknown_only = posted_unknown and date_field == "posted"
    dated = any(x is not None for x in (cutoff, newest, day_from, day_to))
    setup_runs = None
    if setup_id is not None:
        setup_runs = set(db.scalars(select(SearchRun.id).where(SearchRun.saved_search_id == setup_id)).all())
    job_type_key = (job_type or "all").strip().lower()
    # Jobs you already applied to stay out of Discovery unless you ask for them (the Applied stage, or the box).
    show_applied = include_applied or stage in SUBMITTED_STATUSES
    applied_ids = set() if show_applied else set(db.scalars(
        select(Application.job_id).where(Application.applied_at.is_not(None))).all())
    query = triage_query(db) if queue else select(Job)
    jobs = db.scalars(query.order_by(Job.id)).all()
    parse_status = job_description.latest_parse_statuses(db)
    described = {job.id: job_description.description_state(job, parse_status.get(job.id)) for job in jobs}
    excluded = excluded_companies(db)
    total_all = len(jobs)
    needle = q.lower().strip()
    words = [w.strip().lower() for w in keywords.split(",") if w.strip()]
    seen: set[str] = set()
    rows = []
    applied_hidden = 0
    domain_counts = Counter()
    for job in jobs:
        key = _dedupe_key(job)
        repost = key in seen
        seen.add(key)
        if job.company.lower() in excluded:
            continue
        hidden = job.domain_match == "NOT_MATCHING"
        domain_counts["not_matching" if hidden else "matching"] += 1
        if (domain == "matching" and hidden) or (domain == "not_matching" and not hidden):
            continue
        if min_fit and (job.fit_score or 0) < min_fit:
            continue
        if max_fit and (job.fit_score is None or job.fit_score > max_fit):
            continue
        if board != "all" and board_label(job.source) != board:
            continue
        if location not in ("all", "") and location_key(job.location) != location:
            continue
        if description not in ("all", "") and described[job.id] != description:
            continue
        if not show_applied and (job.status in SUBMITTED_STATUSES or job.id in applied_ids):
            applied_hidden += 1
            continue
        when = job.discovered_at if date_field == "found" else job.posted_at
        if unknown_only and when is not None:
            continue
        if dated:
            # A posting date the board never gave is unknown, so it matches no date window or range.
            if when is None or (cutoff is not None and when < cutoff) or (newest is not None and when >= newest):
                continue
            if (day_from is not None and when < day_from) or (day_to is not None and when >= day_to):
                continue
        if work_mode not in ("all", "") and _work_mode(job) != work_mode:
            continue
        if job_type_key != "all" and (job.job_type or "").strip().lower() != job_type_key:
            continue
        if setup_runs is not None and job.search_run_id not in setup_runs:
            continue
        if has_salary and not _has_salary(job):
            continue
        if stage == "unranked":  # not a status: any job without a fit score yet
            if job.fit_score is not None:
                continue
        elif stage != "all" and job.status != stage:
            continue
        haystack = f"{job.title} {job.company} {job.location}".lower()
        if needle and needle not in haystack:
            continue
        if words and not all(any(part in f"{haystack} {job.description.lower()[:4000]}" for part in w.split("/")) for w in words):
            continue
        if hide_agencies and is_agency(job.company):
            continue
        if hide_reposts and repost:
            continue
        rows.append(job)
    if sort == "new":
        rows.sort(key=lambda j: j.discovered_at, reverse=True)
    elif sort == "posted":  # newest posting first; jobs without a posting date last
        rows.sort(key=lambda j: (j.posted_at is None, -(j.posted_at.timestamp() if j.posted_at else 0), -j.id))
    elif sort == "company":
        rows.sort(key=lambda j: j.company.lower())
    else:
        rows.sort(key=lambda j: (j.fit_score is None, -(j.fit_score or 0), -j.id))
    if queue:  # the triage queue never lists hidden jobs, so their count comes from the table itself
        domain_counts["not_matching"] = db.scalar(
            select(func.count(Job.id)).where(Job.triaged_at.is_(None), Job.status.in_(QUEUE_STATUSES),
                                             Job.domain_match == "NOT_MATCHING")) or 0
    total = len(rows)
    if ids_only:  # "select all matching" across every page
        chosen = rows[:5000]
        return {"ids": [j.id for j in chosen], "total": total,
                "ranked_ids": [j.id for j in chosen if j.fit_score is not None],
                "parsed_ids": [j.id for j in chosen if described[j.id] == "PARSED"]}
    from app.services import job_removal

    pages = max(1, -(-total // per_page))
    page = min(page, pages)
    boards = sorted({board_label(j.source) for j in jobs})
    counts = Counter(described.values())
    return {"items": [{**job_card(j), "description_state": described[j.id]}
                      for j in rows[(page - 1) * per_page: page * per_page]], "total": total,
            "description_states": [[key, name, counts.get(key, 0)]
                                   for key, name in job_description.DESCRIPTION_STATES.items()],
            "total_all": total_all, "applied_hidden": applied_hidden,
            "listing_checked_today": job_removal.scheduled_check_ran_today(db, db.info["user_id"], job_removal.utcnow()),
            "domain_counts": {"matching": domain_counts["matching"], "not_matching": domain_counts["not_matching"]},
            "parse_enabled": get_settings().jd_parse_enabled,
            "page": page, "pages": pages, "per_page": per_page, "boards": boards,
            "locations": location_facets([j.location for j in jobs]),
            "job_types": sorted({(j.job_type or "").strip().lower() for j in jobs} - {""}),
            "work_modes": dict(Counter(_work_mode(j) for j in jobs)),
            "setups": [[s.id, s.name] for s in db.scalars(select(SavedSearch).order_by(SavedSearch.name)).all()]}


class TriageIn(BaseModel):
    action: str


class BulkTriageIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    action: str


def apply_triage(job: Job, action: str) -> None:
    if action not in TRIAGE_ACTIONS:
        raise HTTPException(422, "Unknown triage action")
    if job.status in ("tailored", NEEDS_RETAILOR, "approved", "applied", "pending_approval") and action != "shortlist":
        raise HTTPException(409, "This job already has a tailored packet or application")
    if job.status not in ("tailored", NEEDS_RETAILOR, "approved", "applied", "pending_approval"):
        job.status = TRIAGE_ACTIONS[action]
    job.triaged_at = None if action == "restore" else utcnow()


TRIAGE_FEEDBACK = {"shortlist": "SHORTLISTED", "skip": "DISMISSED", "dismiss": "DISMISSED"}


def _triage_feedback(db: Session, jobs: list[Job], action: str) -> None:
    """What the person decided in triage teaches the domain filter (app.services.feedback_learning)."""
    kind = TRIAGE_FEEDBACK.get(action)
    if not kind or not jobs:
        return
    for job in jobs:
        feedback_learning.record(db, job, kind)
    feedback_learning.after_feedback(db, jobs[0].user_id)


@router.post("/api/jobs/{job_id}/triage")
def triage(job_id: int, payload: TriageIn, db: Session = Depends(get_user_db)):
    job = get_job(db, job_id)
    apply_triage(job, payload.action)
    _triage_feedback(db, [job], payload.action)
    db.commit()
    return job_card(job)


@router.post("/api/jobs/bulk-triage")
def bulk_triage(payload: BulkTriageIn, db: Session = Depends(get_user_db)):
    changed = []
    for job in db.scalars(select(Job).where(Job.id.in_(payload.ids))).all():
        try:
            apply_triage(job, payload.action)
            changed.append(job)
        except HTTPException:
            continue
    _triage_feedback(db, changed, payload.action)
    db.commit()
    return {"changed": len(changed)}


class BulkIdsIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)
    include_submitted: bool = False


@router.post("/api/jobs/bulk-delete")
def bulk_delete(payload: BulkIdsIn, db: Session = Depends(get_user_db)):
    """Permanently delete jobs with their tailored packets. Approved or applied jobs need include_submitted.

    Each deletion is remembered as feedback (the job itself is gone), so similar jobs can be hidden later — except
    for jobs the domain filter had already hidden: clearing those out says nothing new."""
    deleted, protected = 0, []
    owner = None
    # Clearing out many jobs at once is tidying, not a judgement on each of them (AGENTS.md: Discovery quality).
    from app.services import domain_classifier

    cleanup = domain_classifier.mode() == domain_classifier.ON and len(payload.ids) > 3
    for job in db.scalars(select(Job).where(Job.id.in_(payload.ids))).all():
        if job.status in PROTECTED_STATUSES and not payload.include_submitted:
            protected.append(job.id)
            continue
        if job.domain_match != "NOT_MATCHING":
            feedback_learning.record(db, job, cleanup and "BULK_DELETED" or "DELETED")
            owner = job.user_id
        delete_job(db, job)
        deleted += 1
    if owner is not None:
        feedback_learning.after_feedback(db, owner)
    db.commit()
    return {"deleted": deleted, "protected": protected,
            "missing": max(0, len(set(payload.ids)) - deleted - len(protected))}


@router.post("/api/jobs/bulk-rank", status_code=202)
def bulk_rank(payload: BulkIdsIn, request: Request, background: BackgroundTasks, rerank: bool = False,
              db: Session = Depends(get_user_db)):
    """Queue ranking for selected jobs. `rerank` also re-ranks ranked jobs as a new version.

    While parsing is on, only jobs whose current description is parsed are ranked: Parse them first."""
    from app.services import job_description  # local import, as app.services.sources does

    ids, unparsed = list(dict.fromkeys(payload.ids)), []
    if get_settings().jd_parse_enabled:
        jobs = db.scalars(select(Job).where(Job.id.in_(ids))).all()
        statuses = job_description.latest_parse_statuses(db, [job.id for job in jobs])
        parsed = {job.id for job in jobs if job_description.description_state(job, statuses.get(job.id)) == "PARSED"}
        unparsed = [job.id for job in jobs if job.id not in parsed]
        ids = [job_id for job_id in ids if job_id not in unparsed]
    out = ranker.request_ranking(db, ids, force=rerank, only_unranked=not rerank, actor=f"user:{db.info['user_id']}")
    out["skipped"] += [{"id": job_id, "reason": "not parsed yet"} for job_id in unparsed]
    run_soon(background, request.app, "ranking")
    return out


@router.post("/api/jobs/bulk-parse", status_code=202)
def bulk_parse(payload: BulkIdsIn, request: Request, background: BackgroundTasks, db: Session = Depends(get_user_db)):
    """Queue a parse of each selected job's description. The parse worker does the model calls; ranking follows."""
    from app.services import job_description  # local import, as app.services.sources does

    ids = list(dict.fromkeys(payload.ids))
    jobs = {job.id: job for job in db.scalars(select(Job).where(Job.id.in_(ids))).all()}
    statuses = job_description.latest_parse_statuses(db, list(jobs))
    queued, skipped = [], []
    for job_id in ids:
        job = jobs.get(job_id)
        state = job_description.description_state(job, statuses.get(job_id)) if job else None
        why = ("not found" if job is None
               else "hidden: not matching your domain" if job.domain_match == "NOT_MATCHING"
               else "waiting for the AI check" if job.domain_match == "PENDING"
               else "waiting for your review" if job.domain_match == "REVIEW"
               else "no description" if state == "NO_DESCRIPTION"
               else "already parsed" if state == "PARSED"
               else "already parsing" if state == "PARSING"
               else None)
        if not why:
            try:  # the parse sweep may queue the same job at the same moment
                with db.begin_nested():
                    job_description.queue_parse(db, job, reason="manual")
            except IntegrityError:
                why = "already parsing"
        (skipped.append({"id": job_id, "reason": why}) if why else queued.append(job_id))
    db.commit()
    if queued:
        run_soon(background, request.app, "jd_parsing")
    return {"queued": queued, "skipped": skipped}


@router.post("/api/jobs/listing-check", status_code=202)
def listing_check_start(background: BackgroundTasks, db: Session = Depends(get_user_db)):
    """Queue a check of every posting of this account that is due. Returns at once; poll the status route below."""
    from app.db import SessionLocal
    from app.services import job_removal

    if not get_settings().auto_delete_removed_jobs:
        raise HTTPException(409, "Removed postings are kept on this server (AUTO_DELETE_REMOVED_JOBS=false)")
    owner = db.info["user_id"]
    if job_removal.manual_running(owner):
        raise HTTPException(409, "A listing check is already running")
    now = job_removal.utcnow()
    due = job_removal.manual_due(db, owner, now)
    if due:
        background.add_task(job_removal.check_account, SessionLocal, owner, due, now)
    return {"due": len(due), "since": now.isoformat()}


@router.get("/api/jobs/listing-check/status")
def listing_check_status(since: datetime, db: Session = Depends(get_user_db)):
    from app.services import job_removal

    owner = db.info["user_id"]
    return {**job_removal.manual_progress(db, owner, since), "running": job_removal.manual_running(owner)}


@router.get("/api/jobs/{job_id}")
def job_detail(job_id: int, db: Session = Depends(get_user_db)):
    job = get_job(db, job_id)
    resume = latest_resume(db, job.id)
    app = job.application
    fits = sorted((j for j in db.scalars(select(Job.fit_score).where(Job.fit_score.is_not(None))).all()), reverse=True)
    rank = None
    if job.fit_score is not None and fits:
        rank = round(100 * (fits.index(job.fit_score) + 1) / len(fits))
    steps = [
        ("Discovered", True, ago(job.discovered_at)),
        ("Ranked", job.fit_score is not None, f"fit {job.fit_score:.0f}" if job.fit_score is not None else "not yet"),
        ("Tailored", resume is not None, f"v{resume.version} · reviewer {resume.reviewer_score:.0f}" if resume and resume.reviewer_score is not None else "not yet"),
        ("Approved", bool(app and app.approved_at), day_label(app.approved_at) if app and app.approved_at else "not yet"),
        ("Applied", bool(app and app.applied_at), day_label(app.applied_at) if app and app.applied_at else "your action"),
    ]
    evidence = db.scalars(select(Evidence).where(Evidence.verified.is_(True))).all()
    gap_notes = []
    analysis = job.fit_analysis or {}
    # Fatal gaps are usually also listed as missing requirements: show each gap once.
    gaps: dict[str, str] = {}
    for g in [*ranked_list(analysis, "must_have_missing"), *ranked_list(analysis, "gaps")]:
        if (text := str(g).strip()):
            gaps.setdefault(text.lower(), text)
    for gap in list(gaps.values())[:4]:
        words = {w for w in re.findall(r"[a-z0-9]{3,}", gap.lower())} - GENERIC_GAP_WORDS
        partial = next((e for e in evidence if words & set(re.findall(r"[a-z0-9]{3,}", f"{e.claim} {' '.join(e.skills or [])}".lower()))), None)
        gap_notes.append({"gap": str(gap), "partial": f"{partial.company or 'Evidence'}: {partial.claim}" if partial else ""})
    from app.services import job_description  # local import, as app.services.sources does

    parse_status = job_description.latest_parse_statuses(db, [job.id]).get(job.id)
    from app.services import jd_recovery

    return {**job_card(job), "description": job.description, "fit_analysis": job.fit_analysis or {},
            "description_state": job_description.description_state(job, parse_status),
            "parse_enabled": get_settings().jd_parse_enabled,
            "description_recovery": jd_recovery.status_of(db, job),
            "rank_pct": rank, "wizard": [{"label": l, "done": d, "meta": m} for l, d, m in steps],
            "gap_notes": gap_notes, "notes": app.notes if app else "",
            "resume_id": resume.id if resume else None, "pipeline": ranker.pipeline_status(db, job),
            # Which resume ranking and tailoring read for this job now, and the one its latest packet was made from.
            "resume_in_use": (in_use := resume_in_use(db, job)),
            "packet_source": packet_source_resume(db, job, resume, in_use) if resume else None}


class JobUrlIn(BaseModel):
    url: str = Field(min_length=8, max_length=1200)
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""


def _safe_public_url(url: str) -> None:
    try:
        check_public_url(url)
    except UnsafeURL as exc:
        raise HTTPException(422, str(exc)) from exc


def extract_job_posting(html: str) -> dict:
    """Title, company, location and description from a posting page. `description_source` says where the text came
    from: "posting" is the page's own JobPosting data, "page_text" is every visible word on it (menus included)."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("@type") in ("JobPosting", ["JobPosting"]):
                org = item.get("hiringOrganization") or {}
                place = item.get("jobLocation") or {}
                place = place[0] if isinstance(place, list) and place else place
                address = (place or {}).get("address") or {} if isinstance(place, dict) else {}
                found = {"title": item.get("title", ""), "company": org.get("name", "") if isinstance(org, dict) else str(org),
                         "location": ", ".join(filter(None, [address.get("addressLocality"), address.get("addressCountry") if isinstance(address.get("addressCountry"), str) else ""])) if isinstance(address, dict) else "",
                         "description": BeautifulSoup(item.get("description", ""), "html.parser").get_text("\n", strip=True)}
                break
    meta = lambda prop: (soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop}) or {}).get("content", "")
    found.setdefault("title", "")
    found["title"] = found["title"] or meta("og:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
    found["company"] = found.get("company") or meta("og:site_name")
    found["description_source"] = "posting" if found.get("description") else "page_text"
    if not found.get("description"):
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        found["description"] = soup.get_text("\n", strip=True)[:MAX_DESCRIPTION_CHARS]
    return found


MAX_DESCRIPTION_CHARS = 30000


def fetch_job_posting(url: str, *, operation: str = "page_fetch") -> dict:
    """Fetch a public posting page and extract it. Raises HTTPException (422) for an unusable URL."""
    _safe_public_url(url)
    with external_client("WEB", service="job_posting", operation=operation, timeout=20,
                         follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
    return extract_job_posting(resp.text)


@router.post("/api/jobs/from-url")
def job_from_url(payload: JobUrlIn, db: Session = Depends(get_user_db)):
    data = {"title": payload.title, "company": payload.company, "location": payload.location,
            "description": payload.description}
    fetch_error = ""
    if not (data["title"] and data["company"] and data["description"]):
        try:
            extracted = fetch_job_posting(payload.url)
            data = {k: data[k] or extracted.get(k, "") for k in data}
        except httpx.HTTPError as exc:
            fetch_error = f"Could not fetch the posting ({exc.__class__.__name__})"
    missing = [k for k in ("title", "company") if not data[k].strip()]
    if missing:
        raise HTTPException(422, {"message": fetch_error or "Could not read the posting — fill in the missing fields",
                                  "missing": missing, "extracted": data})
    ref = hashlib.sha256(payload.url.encode()).hexdigest()[:40]
    job, created = upsert_job(db, source="manual", source_ref=ref, title=data["title"], company=data["company"],
                              location=data["location"], url=payload.url, description=data["description"])
    return {"created": created, "job": job_card(job)}


class JobTextIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    company: str = Field(min_length=1, max_length=300)
    location: str = Field(default="", max_length=300)
    url: str = Field(default="", max_length=1200)
    description: str = Field(max_length=MAX_DESCRIPTION_CHARS)


@router.post("/api/jobs/from-text")
def job_from_text(payload: JobTextIn, db: Session = Depends(get_user_db)):
    """Add a job from a description the person pasted or uploaded. The text is theirs (description_source USER), so
    recovery never replaces it. With a URL it shares the link path's identity, so adding both ways is one job."""
    from app.services import jd_recovery

    title, company, url = payload.title.strip(), payload.company.strip(), payload.url.strip()
    text = payload.description.strip()
    if not (title and company):
        raise HTTPException(422, "Enter the job title and company")
    floor = get_settings().ranking_min_description_chars
    if len(text) < floor:
        raise HTTPException(422, f"That is too short to rank against — paste the full description (at least {floor} "
                                 "characters)")
    if url and not re.match(r"^https?://\S+$", url, re.I):
        raise HTTPException(422, "The posting link must start with http:// or https://")
    key = url or f"text:{company.casefold()}|{title.casefold()}|{payload.location.strip().casefold()}"
    ref = hashlib.sha256(key.encode()).hexdigest()[:40]
    job, created = upsert_job(db, source="manual", source_ref=ref, title=title, company=company,
                              location=payload.location.strip(), url=url, description=text)
    if created or job.description == text:
        jd_recovery.on_description_changed(db, job, by_user=True)
        db.commit()
    return {"created": created, "job": job_card(job)}


MAX_JD_FILE_BYTES = 10 * 1024 * 1024
JD_FILE_TYPES = {".pdf": "application/pdf", ".txt": "text/plain",
                 ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
_JD_LABELS = {
    "title": re.compile(r"^\s*(?:job\s+title|position|role|title)\s*[:\-–]\s*(.{2,150})$", re.I),
    "company": re.compile(r"^\s*(?:company(?:\s+name)?|employer|organi[sz]ation)\s*[:\-–]\s*(.{2,150})$", re.I),
    "location": re.compile(r"^\s*(?:job\s+)?location\s*[:\-–]\s*(.{2,150})$", re.I),
}


def read_job_file(filename: str, content_type: str, data: bytes) -> str:
    """The text of an uploaded job description (PDF, DOCX or TXT). Raises HTTPException for an unusable file."""
    from app.services import document_text
    from app.services.structured_resume import clean_raw_text

    suffix = Path(filename or "").suffix.lower()
    kind = JD_FILE_TYPES.get(suffix) or (content_type if content_type in JD_FILE_TYPES.values() else "")
    if not kind:
        raise HTTPException(415, "Upload the job description as a PDF, DOCX or TXT file")
    if not data:
        raise HTTPException(400, "The file is empty")
    try:
        text = document_text.extract(kind, data, errors="replace")
    except Exception as exc:
        raise HTTPException(422, f"Could not read the file ({exc.__class__.__name__})") from exc
    text = clean_raw_text(text)
    if not text:
        raise HTTPException(422, "No readable text was found in the file — a scanned PDF has none, so paste the "
                                 "description instead")
    return text[:MAX_DESCRIPTION_CHARS]


def guess_job_fields(text: str) -> dict:
    """Title, company and location only where the text labels them ("Job title: …"); never inferred otherwise. The
    result is a draft the person checks before anything is saved."""
    found = {"title": "", "company": "", "location": ""}
    for line in text.splitlines()[:60]:
        for key, pattern in _JD_LABELS.items():
            match = pattern.match(line)
            if match and not found[key]:
                found[key] = match.group(1).strip()
    return found


@router.post("/api/jobs/read-file")
async def job_read_file(file: UploadFile = File(...)):
    """Read an uploaded job description into a draft. Nothing is saved: the person checks the title, company and
    text, then adds it with /api/jobs/from-text. The file itself is not kept."""
    data = await file.read(MAX_JD_FILE_BYTES + 1)
    if len(data) > MAX_JD_FILE_BYTES:
        raise HTTPException(413, f"The file is larger than {MAX_JD_FILE_BYTES // (1024 * 1024)} MB")
    text = read_job_file(file.filename or "", file.content_type or "", data)
    return {**guess_job_fields(text), "description": text}


@router.post("/api/jobs/{job_id}/description/fetch")
def fetch_job_description(job_id: int, db: Session = Depends(get_user_db)):
    """Read the description from the job's posting URL. Nothing is saved: the text comes back as a draft the user
    checks (a page without JobPosting data yields every word on it, menus and cookie notices included)."""
    job = get_job(db, job_id)
    if not (job.url or "").strip():
        raise HTTPException(422, "This job has no posting URL to read from — paste the description instead")
    try:
        found = fetch_job_posting(job.url, operation="description_fetch")
    except httpx.HTTPStatusError as exc:
        raise HTTPException(422, f"The posting page answered {exc.response.status_code} — many boards block "
                                 "automated reads, so paste the description instead") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(422, f"Could not fetch the posting ({exc.__class__.__name__}) — paste the description "
                                 "instead") from exc
    text = (found.get("description") or "").strip()
    if len(text) < get_settings().ranking_min_description_chars:
        raise HTTPException(422, "The posting page has no readable description (it is probably built in the "
                                 "browser) — open the posting and paste the description instead")
    return {"description": text, "source": found["description_source"]}


class JobDescriptionIn(BaseModel):
    description: str = Field(max_length=MAX_DESCRIPTION_CHARS)


@router.put("/api/jobs/{job_id}/description")
def save_job_description(job_id: int, payload: JobDescriptionIn, request: Request, background: BackgroundTasks,
                         db: Session = Depends(get_user_db)):
    """Set the posting text by hand (typed, pasted or fetched), then parse and rank it."""
    from app.services import (  # local import, as app.services.sources does
        jd_recovery,
        job_description,
    )

    job = job_description.lock_job(db, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    text = payload.description.strip()
    floor = get_settings().ranking_min_description_chars
    if len(text) < floor:
        raise HTTPException(422, f"That is too short to rank against — paste the full description (at least {floor} "
                                 "characters)")
    job.description = text
    job_description.normalize_job(db, job)
    # A person's own text: never replaced by recovery, and recovery that has not started is cancelled.
    jd_recovery.on_description_changed(db, job, by_user=True)
    job_description.queue_parse(db, job, reason="description_changed")
    ranker.reopen_for_description(db, job, actor=f"user:{db.info['user_id']}")
    # A job ranked without a description (or with a different one) is ranked again against the new text.
    why = ranker.queue_job(db, job, policy=ranker.policy_for(db, job.user_id), actor=f"user:{db.info['user_id']}",
                           reason="description_changed", force=True)
    db.commit()
    run_soon(background, request.app, "ranking")
    return {"job": job_card(job), "ranking_queued": why is None, "ranking_skipped": why}


class JobTitleIn(BaseModel):
    title: str = Field(max_length=300)


@router.put("/api/jobs/{job_id}/title")
def save_job_title(job_id: int, payload: JobTitleIn, db: Session = Depends(get_user_db)):
    """Correct a job's title by hand (boards can send a URL or a placeholder). It queues no ranking, so it costs no
    model call, and it leaves dedupe_key alone: the posting's identity for duplicates and claims does not change."""
    from app.services import job_description  # local import, as save_job_description does

    job = job_description.lock_job(db, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    title = " ".join(payload.title.split())
    if not title or title.lower() in ranker.PLACEHOLDER_TITLES:
        raise HTTPException(422, "Enter the job's title")
    job.title = title
    db.commit()
    return {"job": job_card(job)}


class CustomActorIn(BaseModel):
    id: str = Field(pattern=r"^[\w.-]+[/~][\w.-]+$", max_length=200)
    name: str = Field(default="", max_length=120)
    fan_out: str = "role_location"
    input_template: dict = Field(default_factory=dict)


class SearchProfileIn(BaseModel):
    roles: list[str] = Field(default_factory=list, max_length=5)
    locations: list[str] = Field(default_factory=list, max_length=5)
    # Python, Java…: the first is added to each role's search, and imported jobs must mention one of them.
    primary_skills: list[str] = Field(default_factory=list, max_length=3)
    actors: list[str] = Field(default_factory=lambda: list(DEFAULT_ACTORS), max_length=10)
    # Shared by every setup of the account (share_custom_actors), which holds additions to MAX_CUSTOM_ACTORS; this bound
    # only keeps a payload sane, so a library from before sharing that is a little over the cap can still be saved.
    custom_actors: list[CustomActorIn] = Field(default_factory=list, max_length=100)  # PAYLOAD_ACTOR_LIMIT
    sites: list[str] = Field(default_factory=lambda: ["linkedin", "indeed", "glassdoor"])
    remote_platforms: list[str] = Field(default_factory=lambda: list(REMOTE_PLATFORMS))
    remoteok_url: str = "https://remoteok.com/remote-jobs"
    hours_old: int = Field(default=168, ge=0, le=8760)
    max_results: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=1000)
    job_type: str = ""
    seniority: list[str] = Field(default_factory=list)
    country_indeed: str = Field(default="usa", max_length=40)
    distance: int = Field(default=50, ge=0, le=500)
    is_remote: bool = False
    easy_apply: bool = False
    fetch_description: bool = True
    enforce_annual_salary: bool = False
    residential_proxy: bool = True
    salary_only: bool = False
    visa_sponsorship: bool = False
    exclude_keywords: list[str] = Field(default_factory=list, max_length=20)
    linkedin_company_ids: list[str] = Field(default_factory=list, max_length=20)
    skip_agencies: bool = True
    # Startup boards: Work at a Startup, Y Combinator Jobs, HN Who's Hiring and Wellfound.
    yc_batches: list[str] = Field(default_factory=list, max_length=10)
    hn_month: str = Field(default="current", max_length=10)
    min_salary: int = Field(default=0, ge=0, le=10_000_000)
    startup_details: bool = True
    auto_rank: bool = True
    # The resume jobs from this search are ranked and tailored with: auto (matched by resume tags; the active resume
    # when nothing matches clearly), active (always the active resume) or fixed (resume_id).
    resume_mode: Literal["auto", "active", "fixed"] = "auto"
    resume_id: int | None = None
    schedule: str = "manual"


JOB_TYPES = {"", "fulltime", "parttime", "contract", "internship", "temporary"}


def get_search_profile(db: Session) -> dict:
    profile = get_profile(db)
    saved = dict((profile.search_preferences or {}).get("search_profile") or {})
    if not isinstance(saved.get("seniority"), list):  # older profiles stored "any"
        saved.pop("seniority", None)
    base = SearchProfileIn(roles=(profile.target_roles or [])[:5], locations=(profile.preferred_locations or [])[:5]).model_dump()
    return {**base, **saved}


def validate_search_profile(payload: SearchProfileIn) -> None:
    problems = []
    if any(len(skill.strip()) > 40 for skill in payload.primary_skills):
        problems.append("Primary skills are at most 40 characters each")
    if payload.schedule not in ("manual", "daily"):
        problems.append("Schedule must be manual or daily")
    if payload.job_type not in JOB_TYPES:
        problems.append("Unknown job type")
    if set(payload.sites) - set(BOARDS):
        problems.append(f"Unknown boards: {', '.join(sorted(set(payload.sites) - set(BOARDS)))}")
    if set(payload.remote_platforms) - set(REMOTE_PLATFORMS):
        problems.append("Unknown remote job boards")
    if set(payload.seniority) - set(SENIORITY_LEVELS):
        problems.append("Unknown seniority level")
    bad_batches = [b for b in payload.yc_batches if not YC_BATCH_RE.match(b.strip())]
    if bad_batches:
        problems.append(f"YC batches look like W24 or S21, not {', '.join(bad_batches)}")
    if not HN_MONTH_RE.match(payload.hn_month.strip().lower()):
        problems.append("HN thread month must be 'current' or YYYY-MM")
    for entry in payload.custom_actors:
        if entry.fan_out not in FAN_OUTS:
            problems.append(f"{entry.id}: unknown fan-out '{entry.fan_out}'")
        if entry.id in BUILTIN:
            problems.append(f"{entry.id} is already built in")
    known = set(BUILTIN) | {c.id for c in payload.custom_actors}
    if not payload.actors:
        problems.append("Choose at least one Apify actor")
    if set(payload.actors) - known:
        problems.append(f"Unknown actors: {', '.join(sorted(set(payload.actors) - known))}")
    if "openclawai/job-board-scraper" in payload.actors and not payload.sites:
        problems.append("Choose at least one board for Multi Job Board")
    if problems:
        raise HTTPException(422, "; ".join(problems))


@router.get("/api/search/actors")
def search_actors(db: Session = Depends(get_user_db)):
    sp = get_search_profile(db)
    return {"actors": [spec.public() for spec in actor_catalog(sp).values()], "placeholders": list(PLACEHOLDERS),
            "fan_outs": list(FAN_OUTS), "groups": list(ACTOR_GROUPS), "boards": BOARDS, "remote_platforms": REMOTE_PLATFORMS,
            "seniority": SENIORITY_LEVELS, "max_runs": MAX_RUNS}


class ActorLookupIn(BaseModel):
    id: str = Field(pattern=r"^[\w.-]+[/~][\w.-]+$", max_length=200)


GUESSES = [(re.compile(r"^(search_?term|keywords?|query|search|search_?keywords?|title|position)$", re.I), "{{role}}"),
           (re.compile(r"^(search_?terms|titles|queries|title_?search)$", re.I), "{{roles}}"),
           (re.compile(r"^(location|city|place|where)$", re.I), "{{location}}"),
           (re.compile(r"^(locations|location_?search)$", re.I), "{{locations}}"),
           (re.compile(r"^(max_?(items|results|jobs)|limit|results_?wanted|rows|count)$", re.I), "{{max_results}}"),
           (re.compile(r"^(hours_?old|posted_?within_?hours)$", re.I), "{{hours_old}}"),
           (re.compile(r"^(days_?old|posted_?within_?days|date_?posted)$", re.I), "{{days_old}}"),
           (re.compile(r"^(is_?remote|remote(_?only)?)$", re.I), "{{is_remote}}"),
           (re.compile(r"^(job_?types?|employment_?types?)$", re.I), "{{job_type}}"),
           (re.compile(r"^(sites|boards|platforms)$", re.I), "{{sites}}"),
           (re.compile(r"^(country|country_?indeed)$", re.I), "{{country}}"),
           (re.compile(r"^(seniority|seniority_?(filter|levels?)|experience_?levels?)$", re.I), "{{seniority}}"),
           (re.compile(r"^(distance|radius)$", re.I), "{{distance}}"),
           (re.compile(r"^(easy_?apply|direct_?apply)$", re.I), "{{easy_apply}}"),
           (re.compile(r"^(exclude_?keywords|title_?exclusion_?search|excluded?_?terms)$", re.I), "{{exclude_keywords}}"),
           (re.compile(r"^(min_?salary|salary_?min)$", re.I), "{{min_salary}}"),
           (re.compile(r"^(visa_?sponsorship|sponsorship)$", re.I), "{{visa_sponsorship}}"),
           (re.compile(r"^(remove_?agency|skip_?agencies)$", re.I), "{{skip_agencies}}"),
           (re.compile(r"^(has_?salary|salary_?only)$", re.I), "{{salary_only}}"),
           (re.compile(r"^(skill|primary_?skill|main_?skill)$", re.I), "{{skill}}"),
           (re.compile(r"^(skills|primary_?skills|skills?_?terms|tech_?stack|technologies)$", re.I), "{{skills}}")]
# Placeholders that fill in a list, and the one-value form of each that has one.
LIST_PLACEHOLDERS = {"roles", "locations", "skills", "sites", "seniority", "exclude_keywords", "linkedin_company_ids",
                     "yc_batches", "remote_platforms"}
ONE_OF = {"roles": "role", "locations": "location", "skills": "skill"}


def guess_placeholder(name: str, prop: dict | None = None) -> str | list[str]:
    """The placeholder for an actor input field, guessed from its name and fitted to its declared type: a value that
    is only a placeholder keeps its own type, so a string field gets the one-value form and an array field a list."""
    guess = next((value for pattern, value in GUESSES if pattern.match(name)), "")
    kind, placeholder = (prop or {}).get("type"), guess[2:-2]
    if not guess:
        return ""
    if kind == "string" and placeholder in LIST_PLACEHOLDERS:
        return f"{{{{{ONE_OF[placeholder]}}}}}" if placeholder in ONE_OF else ""
    if kind == "array" and placeholder not in LIST_PLACEHOLDERS:
        return [guess]
    return guess


@router.post("/api/search/actors/lookup")
def lookup_actor(payload: ActorLookupIn, db: Session = Depends(get_user_db)):
    """Confirm a Store actor exists and suggest an input template from its input schema."""
    token = apify_token(db)
    if not token:
        raise HTTPException(400, "Add your Apify API token in Settings to look actors up")
    api_id = payload.id.replace("/", "~")
    try:
        with apify_client(token, operation="actor_lookup", timeout=20,
                          metadata={"actor_id": payload.id}) as client:
            actor = client.get(f"https://api.apify.com/v2/acts/{api_id}")
            if actor.status_code == 404:
                raise HTTPException(404, f"No Apify actor named '{payload.id}'")
            actor.raise_for_status()
            info = actor.json().get("data") or {}
            build = client.get(f"https://api.apify.com/v2/acts/{api_id}/builds/default")
            schema = {}
            if build.is_success:
                raw = (build.json().get("data") or {}).get("inputSchema") or "{}"
                schema = json.loads(raw) if isinstance(raw, str) else raw
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"Could not reach Apify: {exc.__class__.__name__}") from exc
    properties = (schema or {}).get("properties") or {}
    template = {}
    for name, prop in properties.items():
        guess = guess_placeholder(name, prop)
        if guess:
            template[name] = guess
        elif name in (schema.get("required") or []) and "prefill" in prop:
            template[name] = prop["prefill"]

    return {"id": f"{info.get('username', payload.id.split('/')[0])}/{info.get('name', payload.id.split('/')[-1])}",
            "name": info.get("title") or info.get("name") or payload.id,
            "summary": (info.get("description") or "")[:300],
            "fields": [{"name": n, "type": p.get("type", ""), "title": p.get("title", ""), "required": n in (schema.get("required") or []), "suggested": guess_placeholder(n, p),
                        **{k: p[k] for k in ("default", "prefill") if k in p}}
                       for n, p in properties.items()],
            "input_template": template}


def board_yield(db: Session) -> dict[str, int | None]:
    """Jobs kept per 100 found, per board, over the last 30 days — the number under each board checkbox."""
    since = utcnow() - timedelta(days=30)
    jobs = db.scalars(select(Job).where(Job.discovered_at >= since, Job.source.like("apify:%"))).all()
    stats: dict[str, int | None] = {}
    for site in BOARDS:
        mine = [j for j in jobs if j.source == f"apify:{site}"]
        kept = [j for j in mine if j.status not in ("discovered", "archived", "dismissed")]
        stats[site] = round(100 * len(kept) / len(mine)) if mine else None
    return stats


@router.get("/api/search/profile")
def search_profile_get(db: Session = Depends(get_user_db)):
    return {"profile": get_search_profile(db), "board_yield": board_yield(db),
            "apify_configured": bool(apify_token(db)), "actor_id": get_settings().apify_actor_id}


@router.put("/api/search/profile")
def search_profile_put(payload: SearchProfileIn, db: Session = Depends(get_user_db)):
    validate_search_profile(payload)
    profile = get_profile(db)
    prefs_value = dict(profile.search_preferences or {})
    prefs_value["search_profile"] = payload.model_dump()
    profile.search_preferences = prefs_value
    db.commit()
    return {"profile": get_search_profile(db)}


@router.post("/api/search/profile/preview")
def search_profile_preview(payload: SearchProfileIn):
    """Show exactly which actor runs a profile would start, with the inputs and any filter conflicts."""
    validate_search_profile(payload)
    try:
        runs, warnings = plan_runs(payload.model_dump())
    except ValueError as exc:
        return {"runs": [], "warnings": [], "error": str(exc)}
    return {"runs": [{"actor_id": r["actor_id"], "actor_name": r["actor_name"], "location": r["location"],
                      "roles": r["roles"], "input": r["input"]} for r in runs],
            "warnings": warnings, "error": None}


def run_search_profile(db: Session, sp: dict) -> list[SearchRun]:
    try:
        plan, _ = plan_runs(sp)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return [start_apify_search(db, {"actor_id": item["actor_id"], "actor_input": item["input"],
                                    "location": item["location"], "roles": item["roles"],
                                    "search_params": item["search_params"]}) for item in plan]


@router.post("/api/search/profile/run")
def search_profile_run(db: Session = Depends(get_user_db)):
    sp = get_search_profile(db)
    try:
        runs = run_search_profile(db, sp)
        _, warnings = plan_runs(sp)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"runs": [{"id": r.id, "status": r.status, "actor_id": getattr(r, "actor_id", ""),
                      "location": run_location(r)} for r in runs], "warnings": warnings}


# ───────────── saved search setups ─────────────

DEFAULT_SETUP_NAME = "My search"
MIGRATED_KEY = "search_profile_migrated"


class SavedSearchIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    settings: SearchProfileIn
    # What the person did to their own actors in this form (added or edited; removed). Only these change the account's
    # shared actors: one merely missing from, older in or still left in a stale form is never taken for a change.
    changed_actors: list[str] = Field(default_factory=list, max_length=100)
    removed_actors: list[str] = Field(default_factory=list, max_length=100)
    schedule_kind: str = "manual"
    every_hours: int = Field(default=24, ge=1, le=168)
    time_of_day: str = Field(default="07:00", max_length=5)
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    timezone: str = Field(default="UTC", max_length=60)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _trim_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("Give the setup a name")
        return name


def validate_saved_search(payload: SavedSearchIn) -> None:
    validate_search_profile(payload.settings)
    problems = schedule_problems(payload)
    if problems:
        raise HTTPException(422, "; ".join(problems))


def saved_search_card(setup: SavedSearch) -> dict:
    return {"id": setup.id, "name": setup.name, "settings": setup.settings,
            "schedule_kind": setup.schedule_kind, "every_hours": setup.every_hours,
            "time_of_day": setup.time_of_day, "weekdays": setup.weekdays or [], "timezone": setup.timezone,
            "enabled": setup.enabled, "schedule_label": schedule_label(setup),
            "last_run_at": setup.last_run_at.isoformat() if setup.last_run_at else None,
            "next_run_at": setup.next_run_at.isoformat() if setup.next_run_at else None}


def apply_saved_search(setup: SavedSearch, payload: SavedSearchIn) -> SavedSearch:
    """Copy a validated payload onto a setup and work out when it next comes due."""
    setup.name = payload.name
    setup.settings = payload.settings.model_dump()
    setup.schedule_kind = payload.schedule_kind
    setup.every_hours = payload.every_hours
    setup.time_of_day = payload.time_of_day
    setup.weekdays = sorted({d for d in payload.weekdays if 0 <= d <= 6})
    setup.timezone = payload.timezone
    setup.enabled = payload.enabled
    setup.next_run_at = next_due(setup, utcnow())
    return setup


MAX_CUSTOM_ACTORS = 25
PAYLOAD_ACTOR_LIMIT = 100  # SearchProfileIn.custom_actors


def share_custom_actors(db: Session, saved: SavedSearch | None = None, changed: list[str] | tuple = (),
                        removed: list[str] | tuple = (), before: list[dict] | None = None) -> list[str]:
    """Actors a person adds belong to the account, not to one setup: every setup lists the same ones.

    `saved` is the setup just written. Its actors join (or replace) the shared copy only when named in `changed`, the
    ones the person added or edited in that form; ids in `removed` leave every setup and its selection. Anything else
    in the saved form is ignored, so a stale form never undoes, brings back or deletes another setup's change. Ticking stays per setup: a shared actor
    arrives unticked. Returns the names of scheduled setups whose last ticked actor was removed; their schedule is
    paused, since a setup with nothing ticked has nothing to run.
    """
    setups = list(db.scalars(select(SavedSearch).order_by(SavedSearch.id)).all())
    removed = set(removed)
    library: dict[str, dict] = {}
    for entry in [*(before or []), *(e for s in setups if s is not saved for e in (s.settings or {}).get("custom_actors") or [])]:
        if entry.get("id") and entry["id"] not in removed:
            library.setdefault(entry["id"], entry)
    known = set(library)
    for entry in ((saved.settings or {}).get("custom_actors") or []) if saved else []:
        if entry["id"] in changed and entry["id"] not in removed:
            library[entry["id"]] = entry
    added = set(library) - known
    if added and len(library) > MAX_CUSTOM_ACTORS:
        raise HTTPException(422, f"You can add up to {MAX_CUSTOM_ACTORS} of your own actors across your setups; "
                                 "remove one before adding another")
    if len(library) > PAYLOAD_ACTOR_LIMIT:
        return []  # older setups with this many between them could not be saved at all; leave each its own
    shared, paused = list(library.values()), []
    for setup in setups:
        settings = dict(setup.settings or {})
        if (settings.get("custom_actors") or []) != shared:
            settings["custom_actors"] = shared
        selection = settings.get("actors")  # absent means the defaults; leave it so
        if selection is not None:
            # A tick on a user-added actor that is no longer shared (removed here, or elsewhere after a form loaded).
            kept = [a for a in selection if a in BUILTIN or a in library]
            if kept != selection:
                settings["actors"] = kept
                if not kept and setup.enabled and setup.schedule_kind != "manual":
                    setup.enabled = False  # nothing left to run; plan_runs refuses an empty selection too
                    paused.append(setup.name)
        if settings != (setup.settings or {}):
            setup.settings = settings
    return paused


def ensure_saved_searches(db: Session) -> list[SavedSearch]:
    """This account's setups, moving its one older search profile into a setup the first time."""
    setups = list(db.scalars(select(SavedSearch).order_by(SavedSearch.id)).all())
    profile = get_profile(db)
    prefs = dict(profile.search_preferences or {})
    if setups or prefs.get(MIGRATED_KEY):
        share_custom_actors(db)  # setups saved before actors were shared each kept their own
        if db.dirty:
            db.commit()
        return setups
    legacy = get_search_profile(db)
    daily = legacy.get("schedule") == "daily"
    setup = SavedSearch(name=DEFAULT_SETUP_NAME, settings=legacy, schedule_kind="daily" if daily else "manual",
                        every_hours=24, time_of_day="07:00", weekdays=[], timezone="UTC", enabled=True)
    setup.next_run_at = next_due(setup, utcnow())
    db.add(setup)
    prefs[MIGRATED_KEY] = True
    profile.search_preferences = prefs
    db.commit()
    return [setup]


def get_setup(db: Session, setup_id: int) -> SavedSearch:
    setup = db.get(SavedSearch, setup_id)
    if not setup:
        raise HTTPException(404, "Saved search not found")
    return setup


def reject_duplicate_name(db: Session, name: str, keep: int | None = None) -> None:
    clash = db.scalar(select(SavedSearch.id).where(func.lower(SavedSearch.name) == name.lower(),
                                                   SavedSearch.id != (keep or -1)))
    if clash:
        raise HTTPException(409, f"You already have a setup named '{name}'")


@router.get("/api/search/setups")
def saved_searches_list(db: Session = Depends(get_user_db)):
    setups = ensure_saved_searches(db)
    return {"setups": [saved_search_card(s) for s in setups], "apify_configured": bool(apify_token(db)),
            "board_yield": board_yield(db), "schedule_kinds": list(SCHEDULE_KINDS)}


@router.post("/api/search/setups")
def saved_search_create(payload: SavedSearchIn, db: Session = Depends(get_user_db)):
    validate_saved_search(payload)
    reject_duplicate_name(db, payload.name)
    setup = apply_saved_search(SavedSearch(), payload)
    db.add(setup)
    db.flush()
    paused = share_custom_actors(db, setup, payload.changed_actors, payload.removed_actors)
    db.commit()
    return {"setup": saved_search_card(setup), "paused_setups": paused}


@router.put("/api/search/setups/{setup_id}")
def saved_search_update(setup_id: int, payload: SavedSearchIn, request: Request, background: BackgroundTasks,
                        db: Session = Depends(get_user_db)):
    """Save a setup. When its resume changed, the jobs it already found follow it (discovery.follow_setup), except
    the ones the person pinned to a resume themselves and the approved or applied ones."""
    from app.services import discovery

    setup = get_setup(db, setup_id)
    validate_saved_search(payload)
    reject_duplicate_name(db, payload.name, keep=setup.id)
    before = discovery.setup_choice(setup)
    actors_before = list((setup.settings or {}).get("custom_actors") or [])
    apply_saved_search(setup, payload)
    paused = share_custom_actors(db, setup, payload.changed_actors, payload.removed_actors, actors_before)
    moved = None
    if discovery.setup_choice(setup) != before:
        db.flush()
        moved = discovery.follow_setup(db, setup, actor=f"user:{db.info.get('user_id')}")
    db.commit()
    if moved and moved["ranking_queued"]:
        run_soon(background, request.app, "ranking")
    return {"setup": saved_search_card(setup), "paused_setups": paused,
            **({"resume_jobs": moved} if moved is not None else {})}


@router.delete("/api/search/setups/{setup_id}")
def saved_search_delete(setup_id: int, db: Session = Depends(get_user_db)):
    setup = get_setup(db, setup_id)
    db.delete(setup)
    db.commit()
    return {"deleted": setup_id}


@router.post("/api/search/setups/{setup_id}/duplicate")
def saved_search_duplicate(setup_id: int, db: Session = Depends(get_user_db)):
    setup = get_setup(db, setup_id)
    taken = {name.lower() for name in db.scalars(select(SavedSearch.name)).all()}
    name = next(candidate for candidate in (f"{setup.name[:110]} copy", *(f"{setup.name[:108]} copy {n}" for n in range(2, 50)))
                if candidate.lower() not in taken)
    copy = SavedSearch(name=name, settings=dict(setup.settings or {}), schedule_kind=setup.schedule_kind,
                       every_hours=setup.every_hours, time_of_day=setup.time_of_day,
                       weekdays=list(setup.weekdays or []), timezone=setup.timezone, enabled=setup.enabled)
    copy.next_run_at = next_due(copy, utcnow())
    db.add(copy)
    db.commit()
    return {"setup": saved_search_card(copy)}


def start_saved_search(db: Session, setup: SavedSearch, now: datetime) -> list[SearchRun]:
    """Start one setup's actor runs and tie them back to it, so the runs list can say where they came from."""
    runs = run_search_profile(db, setup.settings or {})
    for run in runs:
        run.saved_search_id = setup.id
    setup.last_run_at = now
    db.commit()
    return runs


@router.post("/api/search/setups/{setup_id}/run")
def saved_search_run(setup_id: int, db: Session = Depends(get_user_db)):
    setup = get_setup(db, setup_id)
    sp = setup.settings or {}
    try:
        runs = start_saved_search(db, setup, utcnow())
        _, warnings = plan_runs(sp)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"runs": [{"id": r.id, "status": r.status, "actor_id": getattr(r, "actor_id", ""),
                      "location": run_location(r)} for r in runs],
            "warnings": warnings, "setup": saved_search_card(setup)}


def run_due_saved_searches(db: Session, owner: int, now: datetime, force: bool = False) -> dict[str, list]:
    """Start the account's enabled setups that are due. The guarded UPDATE makes each start happen once."""
    ensure_saved_searches(db)
    due = or_(SavedSearch.next_run_at.is_(None), SavedSearch.next_run_at <= now)
    scheduled = SavedSearch.schedule_kind != "manual"
    conditions = [SavedSearch.user_id == owner, SavedSearch.enabled.is_(True), scheduled]
    setups = db.scalars(select(SavedSearch).where(*conditions, *([] if force else [due])).order_by(SavedSearch.id)).all()
    run_ids, errors = [], []
    for setup in setups:
        following = next_due(setup, now) or now + timedelta(hours=24)
        claimed = db.execute(update(SavedSearch).where(SavedSearch.id == setup.id, *conditions,
                                                       *([] if force else [due]))
                             .values(last_run_at=now, next_run_at=following)
                             .execution_options(synchronize_session=False)).rowcount
        db.commit()
        if not claimed:
            continue  # another instance started it
        try:
            run_ids += [run.id for run in start_saved_search(db, setup, now)]
        except Exception as exc:  # one setup failing must not stop the others
            db.rollback()
            errors.append({"saved_search_id": setup.id, "error": f"{type(exc).__name__}: {exc}"[:300]})
    return {"run_ids": run_ids, "errors": errors}


@router.get("/api/search/apify/runs/{run_id}/detail")
def run_detail(run_id: int, db: Session = Depends(get_user_db)):
    run = db.get(SearchRun, run_id)
    if not run:
        raise HTTPException(404, "Search run not found")
    pulled = db.scalars(select(Job).where(Job.search_run_id == run.id).order_by(desc(Job.fit_score))).all()
    i = run.input_json or {}
    duration = ""
    if run.completed_at:
        seconds = int((run.completed_at - run.created_at).total_seconds())
        duration = f"{seconds // 60}m {seconds % 60:02d}s"
    filtered = sum(1 for j in pulled if j.status in ("archived", "dismissed") or is_agency(j.company))
    context = [["Actor", f"{run.actor_id} · run {run.external_run_id[:6]}…" + (f" · {duration}" if duration else "")],
               *describe_input(i)]
    if run.error:
        context.append(["Error", run.error])
    return {
        "id": run.id, "status": run.status, "created_at": run.created_at.isoformat(), "error": run.error,
        "completed": run.completed_at is not None,
        "counts": {"pulled": run.item_count, "new": run.created_jobs,
                   "duplicates": max(0, run.item_count - run.created_jobs), "filtered": filtered},
        "context": context,
        "results": [{**job_card(j), "verdict": "agency" if is_agency(j.company) else
                     "below fit floor" if j.status == "archived" else "dismissed" if j.status == "dismissed" else "new"}
                    for j in pulled[:50]],
    }


# ───────────────────────────── tailor / approvals / apply kit ─────────────────────────────

def keyword_coverage(job: Job, resume: ResumeVariant | None) -> list[dict]:
    """The posting's ATS keywords and whether the tailored resume covers each (app.services.ats_keywords)."""
    analysis = job.fit_analysis or {}
    names = requirement_names(analysis)  # a ranking may list requirements by id ("1", "req_002"): show their text
    gaps = " ".join(str(g) for g in ranked_list(analysis, "gaps", names) + ranked_list(analysis, "must_have_missing", names)
                    + ranked_list(analysis, "partial_matches", names))
    return ats_keywords.coverage(job, resume.markdown if resume else "", analysis, names, gaps)


def missing_supported(coverage: list[dict], resume: ResumeVariant) -> list[str]:
    """Keywords the candidate's facts support (recorded when the resume was tailored) that the resume, as it reads
    now, does not cover: wording a person can add without claiming anything new."""
    supported = {ats_keywords.normalize(w) for w in
                 ((resume.content or {}).get("keyword_coverage") or {}).get("supported") or []}
    return [k["word"] for k in coverage if k["status"] != "covered" and ats_keywords.normalize(k["word"]) in supported]


# Status filters for the Tailor and Approvals screens: filter name -> test on a row.
TAILOR_STATUS_FILTERS = {
    "not_tailored": lambda r: not r["has_resume"],
    "has_packet": lambda r: r["has_resume"] and r["status"] not in ("pending_approval", NEEDS_RETAILOR),
    "needs_retailor": lambda r: r["status"] == NEEDS_RETAILOR,
    "sent_back": lambda r: r["status"] == "pending_approval",
    "untailored": lambda r: r["untailored"],  # packet uses the master resume unchanged
}
APPROVAL_STATUS_FILTERS = {
    "waiting": lambda r: r["status"] == "tailored",
    "sent_back": lambda r: r["status"] in ("pending_approval", "rejected"),
    "approved": lambda r: r["status"] == "approved",
    "starred": lambda r: r["starred"],
}
PACKET_SORTS = {
    "fit": lambda r: (r["fit"] is None, -(r["fit"] or 0), -r["id"]),
    "reviewer": lambda r: (r["reviewer_score"] is None, -(r["reviewer_score"] or 0), -r["id"]),
    "company": lambda r: (r["company"].lower(), -r["id"]),
    "new": lambda r: (r["discovered_at"],),  # reversed below
}


def filter_packet_rows(rows: list[dict], *, q: str, min_fit: int, min_review: int, board: str, status: str,
                       sort: str, status_filters: dict, with_facets: bool, location: str = "all",
                       easy_apply_only: bool = False):
    """Filter and sort Tailor or Approvals rows. The unfiltered order is kept for sort="" (the screen's default).

    Returns the plain list unless `with_facets`, which adds totals and the boards and locations present before
    filtering.
    """
    if status not in ("all", "") and status not in status_filters:
        raise HTTPException(422, f"Unknown status filter: {status}")
    if sort not in ("", "default") and sort not in PACKET_SORTS:
        raise HTTPException(422, f"Unknown sort: {sort}")
    needle = q.lower().strip()
    out = [r for r in rows
           if (not needle or needle in f"{r['title']} {r['company']} {r['location']}".lower())
           and (not min_fit or (r["fit"] or 0) >= min_fit)
           and (not min_review or (r["reviewer_score"] or 0) >= min_review)
           and (board in ("all", "") or r["board"] == board)
           and (location in ("all", "") or location_key(r["location"]) == location)
           and (not easy_apply_only or r.get("easy_apply"))
           and (status in ("all", "") or status_filters[status](r))]
    if sort in PACKET_SORTS:
        out.sort(key=PACKET_SORTS[sort], reverse=sort == "new")
    if not with_facets:
        return out
    return {"items": out, "total": len(out), "total_all": len(rows), "boards": sorted({r["board"] for r in rows}),
            "locations": location_facets([r["location"] for r in rows]),
            "easy_apply": sum(1 for r in rows if r.get("easy_apply"))}


@router.get("/api/tailor/queue")
def tailor_queue(q: str = "", min_fit: int = 0, min_review: int = 0, board: str = "all", location: str = "all",
                 status: str = "all", sort: str = "fit", with_facets: bool = False, db: Session = Depends(get_user_db)):
    # AI-ranked jobs only reach Tailor once you have confirmed the shortlist in triage.
    jobs = db.scalars(select(Job).where(or_(and_(Job.status.in_(AWAITING_TAILORING_STATUSES),
                                                 Job.triaged_at.is_not(None)),
                                            Job.status.in_(("tailored", "pending_approval"))))
                      .order_by(desc(Job.fit_score))).all()
    # The reviewer score and ATS keyword coverage of each job's latest packet, so the queue shows
    # everything the single-job screen shows.
    latest: dict[int, ResumeVariant] = {}
    if jobs:
        for variant in db.scalars(select(ResumeVariant).where(ResumeVariant.job_id.in_([j.id for j in jobs]))
                                  .order_by(ResumeVariant.version)).all():
            latest[variant.job_id] = variant
    rows = []
    for job in jobs:
        resume = latest.get(job.id)
        coverage = keyword_coverage(job, resume) if resume else []
        rows.append({**job_card(job), "has_resume": resume is not None,
                     "reviewer_score": resume.reviewer_score if resume else None,
                     "ats_pct": ats_keywords.score(coverage),
                     "untailored": bool((resume.content or {}).get("untailored")) if resume else False,
                     "resume_version": resume.version if resume else None,
                     "review_decision": (resume.reviewer_notes or {}).get("decision") if resume else None,
                     "review_reasons": ((resume.reviewer_notes or {}).get("decision_reasons") or [])[:3] if resume else []})
    return filter_packet_rows(rows, q=q, min_fit=min_fit, min_review=min_review, board=board, status=status, sort=sort,
                              status_filters=TAILOR_STATUS_FILTERS, with_facets=with_facets,
                              location=location)


def packet(db: Session, job: Job) -> dict:
    resume = latest_resume(db, job.id)
    row = setting_row(db)
    base = {"job": job_card(job), "min_review_score": row.min_review_score,
            "application_mode": row.application_mode, "resume": None}
    if not resume:
        return base
    edits = resume_edits(db, resume)
    coverage = keyword_coverage(job, resume)
    notes = resume.reviewer_notes or {}
    review = ([{"tone": "good", "text": s} for s in notes.get("strengths") or []]
              + [{"tone": "warn", "text": s} for s in notes.get("improvements") or []]
              + [{"tone": "bad", "text": s} for s in notes.get("reject_reasons") or []])
    return {**base, "resume": {
        "id": resume.id, "version": resume.version, "created_at": resume.created_at.isoformat(),
        "generated": ago(resume.created_at), "reviewer_score": resume.reviewer_score, "markdown": resume.markdown,
        "untailored": bool((resume.content or {}).get("untailored")),
        # The resume version this packet was tailored from, and the one the job resolves to now.
        "source": packet_source_resume(db, job, resume, in_use := resume_in_use(db, job)), "resume_in_use": in_use,
        "cover_letter": strip_citations(resume.cover_letter), "cover_words": len(resume.cover_letter.split()),
        "edits": edits, "review": review, "keywords": coverage,
        "ats_pct": ats_keywords.score(coverage),
        "missing_supported": missing_supported(coverage, resume),
        # The page target it was tailored for (app.services.resume_length) and a rough estimate of its length.
        "length": {"target_pages": ((resume.content or {}).get("length_plan") or {}).get("pages"),
                   "estimated_pages": resume_length.estimate_pages(resume.markdown or "")},
    }}


@router.get("/api/tailor/{job_id}")
def tailor_detail(job_id: int, db: Session = Depends(get_user_db)):
    return packet(db, get_job(db, job_id))


class EditIn(BaseModel):
    status: str | None = None
    text: str | None = Field(default=None, max_length=4000)


@router.patch("/api/resumes/{resume_id}/edits/{index}")
def edit_resume(resume_id: int, index: int, payload: EditIn, db: Session = Depends(get_user_db)):
    resume = db.get(ResumeVariant, resume_id)
    if not resume:
        raise HTTPException(404, "Resume not found")
    if payload.status and payload.status not in ("accepted", "review", "reverted"):
        raise HTTPException(422, "Status must be accepted, review or reverted")
    if index < 0 or index >= len(resume_edits(db, resume)):
        raise HTTPException(404, "Edit not found")
    try:
        update_resume_edit(db, resume, index, payload.status, payload.text)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return packet(db, resume.job)


@router.put("/api/resumes/{resume_id}/cover-letter")
def edit_cover_letter(resume_id: int, payload: EditIn, db: Session = Depends(get_user_db)):
    resume = db.get(ResumeVariant, resume_id)
    if not resume:
        raise HTTPException(404, "Resume not found")
    try:
        approval.guard_edit(db, resume, "The cover letter")  # an approved packet loses its approval with this edit
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    resume.cover_letter = (payload.text or "").strip()
    db.commit()
    return {"ok": True}


def answers_for(db: Session) -> list[KnowledgeAnswer]:
    return db.scalars(select(KnowledgeAnswer).order_by(desc(KnowledgeAnswer.verified), desc(KnowledgeAnswer.use_count))).all()


APPROVAL_QUEUE_STATUSES = ("tailored", "pending_approval", "approved", "rejected")


@router.get("/api/approvals")
def approvals(q: str = "", min_fit: int = 0, min_review: int = 0, board: str = "all", location: str = "all",
              status: str = "all", sort: str = "default", easy_apply_filter: str = Query("all", alias="easy_apply"),
              with_facets: bool = False, db: Session = Depends(get_user_db)):
    if easy_apply_filter not in ("all", "", "only"):
        raise HTTPException(422, f"Unknown easy_apply filter: {easy_apply_filter}")
    jobs = db.scalars(select(Job).where(Job.status.in_(APPROVAL_QUEUE_STATUSES), reviewed_or_approved())
                      .order_by(desc(Job.id))).all()
    inputs = run_inputs(db, jobs)
    rows = []
    for job in jobs:
        resume = latest_resume(db, job.id)
        app = job.application
        age = (f"approved {day_label(app.approved_at)}" if job.status == "approved" and app and app.approved_at
               else "sent back" if job.status in ("pending_approval", "rejected") else f"tailored {ago(resume.created_at) if resume else ''}")
        rows.append({**job_card(job), "reviewer_score": resume.reviewer_score if resume else None, "age": age,
                     "waiting": job.status == "tailored",
                     "easy_apply": easy_apply(job, inputs.get(job.search_run_id)),
                     "approval_override": bool(app.approval_override) if app else False})
    # Starred jobs lead the queue, then the ones waiting for a decision.
    rows.sort(key=lambda r: (not r["starred"], not r["waiting"], r["status"] == "approved"))
    return filter_packet_rows(rows, q=q, min_fit=min_fit, min_review=min_review, board=board, status=status, sort=sort,
                              status_filters=APPROVAL_STATUS_FILTERS, with_facets=with_facets,
                              location=location, easy_apply_only=easy_apply_filter == "only")


@router.get("/api/approvals/{job_id}")
def approval_detail(job_id: int, db: Session = Depends(get_user_db)):
    job = get_job(db, job_id)
    data = packet(db, job)
    resume = data["resume"]
    profile = get_profile(db)
    answers = answers_for(db)
    checklist = []
    if resume:
        edits = [e for e in resume["edits"] if e["status"] != "reverted"]
        cited = sum(1 for e in edits if e["evidence"])
        rejects = (latest_resume(db, job.id).reviewer_notes or {}).get("reject_reasons") or []
        body = resume["markdown"].split("## Source Resume")[0]
        comp = [a for a in answers if a.category == "compensation"]
        score = resume["reviewer_score"] or 0
        checklist = [
            {"label": "Every claim cites evidence", "detail": f"{cited} of {len(edits)} edits traced", "state": "ok" if cited == len(edits) else "warn"},
            {"label": "No invented experience", "detail": rejects[0] if rejects else "Reviewer raised no unsupported claims", "state": "warn" if rejects else "ok"},
            {"label": "Contact details present", "detail": "Email and phone on profile" if profile.email and profile.phone else "Add email and phone in Settings", "state": "ok" if profile.email and profile.phone else "warn"},
            {"label": "One page, ATS-safe", "detail": f"{len(body.split())} words · plain text sections", "state": "ok" if len(body.split()) <= 650 else "warn"},
            {"label": "Compensation answer", "detail": "Verified answer on file" if any(a.verified for a in comp) else "No verified compensation answer yet", "state": "ok" if any(a.verified for a in comp) else "warn"},
            {"label": "Reviewer threshold", "detail": f"{score:.0f} vs minimum {data['min_review_score']}", "state": "ok" if score >= data["min_review_score"] else "warn"},
        ]
    app = job.application
    return {**data, "checklist": checklist, "pipeline": ranker.pipeline_status(db, job),
            "notes": app.notes if app else "", "application_id": app.id if app else None,
            "approval": {"override": bool(app.approval_override), "note": app.approval_note or ""} if app else None,
            "rejection_reasons": [{"id": key, "label": label} for key, label in REJECTION_REASONS.items()],
            "profile": {"name": profile.name, "headline": profile.headline, "location": profile.location},
            "answers": [{"id": a.id, "question": a.question, "answer": a.answer, "verified": a.verified,
                         "sensitive": a.sensitive, "category": a.category} for a in answers[:6]]}


class NotesIn(BaseModel):
    notes: str = ""


class StarIn(BaseModel):
    starred: bool


# Why a candidate turns a job down. The reason is optional: rejecting with none is one click, and the label is
# only for the screen — nothing here changes how a job is ranked.
REJECTION_REASONS = {
    "compensation": "Pay too low",
    "location": "Location or remote policy",
    "seniority": "Wrong seniority",
    "role_mismatch": "Role is not what I want",
    "company": "Not interested in the company",
    "tech_stack": "Technology or domain",
    "no_longer_open": "Posting closed or filled",
    "applied_elsewhere": "Already applied or in process",
    "other": "Other",
}
# Rejecting is for jobs that have not been submitted. Once an application is out, the tracker records the
# outcome instead (app.workspace tracker stages), so nothing here can rewrite that history.
SUBMITTED_STATUSES = ("applied",)


class RejectionIn(BaseModel):
    reason: str = ""  # one of REJECTION_REASONS, or empty to reject without giving a reason
    notes: str = Field(default="", max_length=2000)


def rejection_card(job: Job) -> dict | None:
    """What the queue and the tracker show about a rejection, or None when the job was not rejected."""
    if job.rejected_at is None:
        return None
    return {"reason": job.rejection_reason,
            "label": REJECTION_REASONS.get(job.rejection_reason or "", "No reason given"),
            "note": job.rejection_note or "", "at": job.rejected_at.isoformat(),
            "on": day_label(job.rejected_at)}


def check_rejection_reason(reason: str) -> str:
    reason = reason.strip().lower()
    if reason and reason not in REJECTION_REASONS:
        raise HTTPException(422, f"Unknown rejection reason '{reason}'")
    return reason


def reject_one(db: Session, job: Job, reason: str, notes: str) -> None:
    """Reject one job without committing. `reason` is already checked; learning runs once, in the caller."""
    if job.status in SUBMITTED_STATUSES:
        raise HTTPException(409, "This application has already been submitted; record the outcome in the tracker")
    job.rejection_reason, job.rejection_note = reason or None, notes.strip() or None
    job.rejected_at = job.triaged_at = utcnow()
    job.status = "dismissed"
    feedback_learning.record(db, job, "REJECTED")
    # The workflow state follows where the lifecycle allows it (app.services.lifecycle). A job whose state has
    # not caught up with the legacy status is still rejected — the queue is what the user acted on.
    if can_transition(job.state, S.SKIPPED, previous_state=job.previous_state):
        transition(db, job, S.SKIPPED, actor="user",
                   reason=f"Rejected: {REJECTION_REASONS.get(reason, 'no reason given')}")
    application = job.application
    if application is not None and application.applied_at is None:
        if application.status == "approved":  # the approval no longer stands: its snapshot is withdrawn, not kept live
            approval.withdraw(db, application, "Rejected after approval")
        application.status = "rejected"
    from app.services import preparation

    preparation.on_decision(db, job, "rejected")


@router.post("/api/jobs/{job_id}/reject")
def reject_job(job_id: int, payload: RejectionIn, db: Session = Depends(get_user_db)):
    """Turn a job down from the approval queue: it leaves the queue and the pipeline, with an optional reason.

    This rejects the job, not the packet — use /revision to send a packet back for another tailoring pass. The
    tailored resume is kept, so a restored job still has its work.
    """
    job = get_job(db, job_id)
    reject_one(db, job, check_rejection_reason(payload.reason), payload.notes)
    feedback_learning.after_feedback(db, job.user_id)
    db.commit()
    db.refresh(job)
    return {"ok": True, "job": job_card(job), "rejection": rejection_card(job)}


@router.get("/api/rejection-reasons")
def rejection_reasons():
    """The reasons the approval screen offers, so the list lives in one place."""
    return {"reasons": [{"id": key, "label": label} for key, label in REJECTION_REASONS.items()]}


def send_back_one(db: Session, job: Job, notes: str | None) -> None:
    """Send one packet back for revision without committing. `notes=None` keeps the notes already on file."""
    if not job.resumes:
        raise HTTPException(400, "There is no tailored packet to send back")
    if job.application is not None and job.application.status == "approved":
        # Sending back an approved packet withdraws the approval, as an edit does: snapshot revoked and the canonical
        # state back to REVIEWED, so the next version is recorded through its real stages before it is approved again.
        approval.revoke(db, job, "Sent back for revision")
    app = job.application or Application(job_id=job.id)
    db.add(app)
    app.status = "pending_approval"
    if notes is not None:
        app.notes = notes
    job.status = "pending_approval"
    from app.services import preparation

    preparation.on_decision(db, job, "revise")  # a waiting preparation graph run tailors again (bounded)


@router.post("/api/jobs/{job_id}/revision")
def send_back(job_id: int, payload: NotesIn, db: Session = Depends(get_user_db)):
    job = get_job(db, job_id)
    send_back_one(db, job, payload.notes)
    db.commit()
    return {"ok": True, "status": job.status}


# What the approval queue can do to several jobs at once. Approving in bulk only approves packets that cleared
# your reviewer minimum: waiving it stays a deliberate, one-packet decision (POST /api/jobs/{id}/approve).
BULK_APPROVAL_LIMIT = 200


class BulkApprovalIn(BaseModel):
    action: Literal["reject", "revise", "approve"]
    job_ids: list[int] = Field(min_length=1, max_length=BULK_APPROVAL_LIMIT)
    reason: str = ""  # reject only: one of REJECTION_REASONS, or empty
    notes: str = Field(default="", max_length=2000)  # reject only


@router.post("/api/approvals/bulk")
def bulk_approval(payload: BulkApprovalIn, db: Session = Depends(get_user_db)):
    """Reject, send back or approve several queued jobs. Each job is decided on its own: one that cannot take the
    action is skipped with the reason, and never fails the rest."""
    reason = check_rejection_reason(payload.reason) if payload.action == "reject" else ""
    min_score = settings_for(db).min_review_score
    done, skipped = [], []
    for job_id in dict.fromkeys(payload.job_ids):
        job = db.get(Job, job_id)  # the session is the account's: another account's job is simply not found
        try:
            if job is None:
                raise HTTPException(404, "Job not found")
            if job.status not in APPROVAL_QUEUE_STATUSES:
                raise HTTPException(409, "Not in the approval queue")
            if payload.action == "reject":
                reject_one(db, job, reason, payload.notes)
            elif payload.action == "revise":
                if job.status == "pending_approval":
                    raise HTTPException(409, "Already sent back for revision")
                send_back_one(db, job, None)
            else:
                if job.status == "approved":
                    raise HTTPException(409, "Already approved")
                latest = max(job.resumes, key=lambda item: item.version) if job.resumes else None
                if latest is None or latest.reviewer_score is None:
                    raise HTTPException(400, "Not reviewed yet")
                if latest.reviewer_score < min_score:
                    raise HTTPException(400, f"Reviewer scored {latest.reviewer_score:.0f}, below your minimum of "
                                             f"{min_score}; approve it on its own to waive the minimum")
                try:
                    approve_job(db, job)
                except ApprovalRefused as exc:
                    raise HTTPException(409, str(exc)) from exc
                feedback_learning.record(db, job, "APPROVED")
            db.commit()
            done.append(job_id)
        except HTTPException as exc:
            db.rollback()
            skipped.append({"id": job_id, "title": job.title if job else "", "reason": str(exc.detail)})
    if done and payload.action in ("reject", "approve"):
        feedback_learning.after_feedback(db, db.info["user_id"])
        db.commit()
    return {"ok": True, "action": payload.action, "done": done, "skipped": skipped}


@router.put("/api/jobs/{job_id}/notes")
def save_notes(job_id: int, payload: NotesIn, db: Session = Depends(get_user_db)):
    job = get_job(db, job_id)
    app = job.application
    if not app:
        app = Application(job_id=job.id, status="pending_approval")
        db.add(app)
    app.notes = payload.notes
    db.commit()
    return {"ok": True}


@router.put("/api/jobs/{job_id}/star")
def star_job(job_id: int, payload: StarIn, db: Session = Depends(get_user_db)):
    """Star or unstar a job. A bookmark for the person: no workflow state, ranking or task changes."""
    job = get_job(db, job_id)
    job.starred = payload.starred
    db.commit()
    return {"ok": True, "starred": job.starred}


def packet_names(db: Session, job: Job, resume: ResumeVariant | None) -> dict[str, str]:
    """Every file name for this job's packet, from one definition."""
    slug = packet_slug(get_profile(db).name, job.company)
    version = resume.version if resume else 1
    return {kind: packet_filename(slug, kind, version) for kind in ("pdf", "docx", "cover_letter")}


def check_export(db: Session, job_id: int) -> None:
    """An approved packet downloads only as it was approved (app.services.approval)."""
    try:
        approval.check_export(db, job_id)
    except approval.PacketChanged as exc:
        raise HTTPException(409, str(exc)) from exc


def download_headers(filename: str) -> dict[str, str]:
    return {"Content-Disposition": f'attachment; filename="{filename}"'}


def packet_download_name(db: Session, job_id: int, kind: str) -> str:
    """The name a download should carry, resolved the same way the apply kit resolves it."""
    job = get_job(db, job_id)
    application = job.application
    resume = (db.get(ResumeVariant, application.approved_resume_id)
              if application and application.approved_resume_id else None) or latest_resume(db, job.id)
    return packet_names(db, job, resume)[kind]


@router.get("/api/jobs/{job_id}/apply-kit")
def apply_kit(job_id: int, db: Session = Depends(get_user_db)):
    job = get_job(db, job_id)
    app = job.application
    if not app or app.status not in ("approved", "applied"):
        raise HTTPException(400, "Approve the packet before opening the apply kit")
    resume = db.get(ResumeVariant, app.approved_resume_id) if app.approved_resume_id else latest_resume(db, job.id)
    names = packet_names(db, job, resume)
    answers = [a for a in answers_for(db) if a.answer and a.verified][:8]
    return {
        "job": job_card(job), "status": app.status, "reviewer_score": resume.reviewer_score if resume else None,
        "followup_date": (utcnow() + timedelta(days=followup_days(db))).isoformat(),
        "files": [
            {"kind": "PDF", "name": names["pdf"], "meta": "ATS-safe export", "href": f"/api/jobs/{job.id}/resume.pdf"},
            {"kind": "DOCX", "name": names["docx"], "meta": "for portals that reject PDF", "href": f"/api/jobs/{job.id}/resume.docx"},
            {"kind": "TXT", "name": names["cover_letter"], "meta": f"{len(resume.cover_letter.split())} words · paste-ready", "href": f"/api/jobs/{job.id}/cover-letter.txt"},
        ] if resume else [],
        "answers": [{"id": a.id, "question": a.question, "answer": a.answer, "sensitive": a.sensitive} for a in answers],
        "claim": _open_claim_view(db, job), **_execution_view(db, job),
    }


def _execution_view(db: Session, job: Job) -> dict:
    """The job's latest application worker run (Phase 5) and whether the worker is on, for the Apply kit."""
    from app.application_api import _takeover_view
    from app.services import application_execution, submission

    task = application_execution.latest(db, job.id)
    intents = submission.intents(db, task.id) if task is not None else []
    return {"execution": application_execution.view(task) if task is not None else None,
            "execution_confirmations": application_execution.confirmations(db, task) if task is not None else [],
            "execution_questions": application_execution.questions(db, task) if task is not None else [],
            "execution_mode": application_execution.available_mode(db),
            "execution_intent": submission.view(intents[-1]) if intents else None,
            "execution_takeover": _takeover_view(task),
            "guarded_consent": submission.consent(db, job.user_id)["enabled"],
            "browser_use_ready": _browser_use_ready()}


def _browser_use_ready() -> bool:
    from app.infrastructure.application_browser.browser_use_sidecar import configured

    return configured()


def _open_claim_view(db: Session, job: Job) -> dict | None:
    """The posting's open pre-submit claim for the Apply kit (Phase 5): started where, by whom, and whether its
    outcome is unknown."""
    from app.application_api import claim_view
    from app.services import apply_queue

    return claim_view(apply_queue.open_claim(db, job), with_token=True, job_id=job.id)


@router.post("/api/knowledge/{item_id}/used")
def knowledge_used(item_id: int, db: Session = Depends(get_user_db)):
    item = db.get(KnowledgeAnswer, item_id)
    if not item:
        raise HTTPException(404, "Knowledge answer not found")
    item.use_count = (item.use_count or 0) + 1
    db.commit()
    return {"use_count": item.use_count}


@router.get("/api/jobs/{job_id}/resume.docx")
def resume_docx(job_id: int, db: Session = Depends(get_user_db)):
    resume = approval.export_variant(db, job_id)  # the approved version once approved, never a newer one
    if not resume:
        raise HTTPException(404, "No tailored resume for this job")
    check_export(db, job_id)
    return Response(markdown_to_docx(resume.markdown), media_type=DOCX_MEDIA_TYPE,
                    headers=download_headers(packet_download_name(db, job_id, "docx")))


@router.get("/api/jobs/{job_id}/cover-letter.txt")
def cover_letter_txt(job_id: int, db: Session = Depends(get_user_db)):
    resume = approval.export_variant(db, job_id)  # the approved version once approved, never a newer one
    if not resume:
        raise HTTPException(404, "No tailored resume for this job")
    check_export(db, job_id)
    # Letters saved before citations were stripped on write still download clean.
    return Response(strip_citations(resume.cover_letter), media_type="text/plain; charset=utf-8",
                    headers=download_headers(packet_download_name(db, job_id, "cover_letter")))


# ───────────────────────────── tracker ─────────────────────────────

def stages(db: Session) -> list[dict]:
    extras = prefs(db).get("extra_stages") or []
    return [*BASE_STAGES, *[s for s in EXTRA_STAGES if s["id"] in extras], CLOSED_STAGE]


def app_payload(db: Session, app: Application) -> dict:
    job = app.job
    now = utcnow()
    # A draft the Application Inbox cancelled (the employer replied) is no longer offered.
    draft = db.scalar(select(FollowUp).where(FollowUp.application_id == app.id, FollowUp.status != "cancelled")
                      .order_by(desc(FollowUp.id)).limit(1))
    resume = db.get(ResumeVariant, app.approved_resume_id) if app.approved_resume_id else None
    stage = app.stage or "applied"
    tag, tone = "", "muted"
    if stage == "applied" and app.followup_due_at:
        delta = (app.followup_due_at.date() - now.date()).days
        tag, tone = ("follow-up due" if delta <= 0 else f"follow-up {day_label(app.followup_due_at)}"), ("warn" if delta <= 0 else "muted")
    upcoming = next((e for e in app.events if e.kind == "interview" and e.occurred_at >= now), None)
    if upcoming:
        tag, tone = f"{upcoming.title.lower()} {day_label(upcoming.occurred_at)}", "good"
    if stage == "applied" and app.followup_due_at:
        next_step = ("Send the follow-up", f"due {day_label(app.followup_due_at)}")
    elif upcoming:
        next_step = (f"Prepare for {upcoming.title.lower()}",
                     format_in_zone(upcoming.occurred_at, event_meta(upcoming.meta)["timezone"]))
    elif stage in INACTIVE_STAGES:
        next_step = ("Archive or reopen", "")
    else:
        next_step = ("Log the latest contact", "keeps the timeline honest")
    return {
        "id": app.id, "job_id": job.id, "title": job.title, "company": job.company, "location": job.location,
        "url": job.url, "fit": None if job.fit_score is None else round(job.fit_score), "stage": stage,
        "archived": app.archived_at is not None,
        "age": ago(app.applied_at), "tag": tag, "tone": tone,
        "meta": f"applied {day_label(app.applied_at)}" + (f" · follow-up {day_label(app.followup_due_at)}" if app.followup_due_at and stage == "applied" else ""),
        "recruiter_name": app.recruiter_name, "recruiter_email": app.recruiter_email, "notes": app.notes,
        "recruiters": recruiters_of(app),
        "docs": " · ".join(filter(None, [f"resume v{resume.version}" if resume else "", "cover letter" if resume and resume.cover_letter else ""])) or "not recorded",
        # The packet that was submitted, so the tracker can open it (read-only: an applied packet is locked).
        "resume_id": resume.id if resume else None,
        "draft": draft.draft if draft else "", "draft_id": draft.id if draft else None,
        # "draft" offers Mark sent and Skip; "sent" is what went out. A follow-up date with no draft can be skipped too.
        "draft_status": draft.status if draft else None,
        "followup_pending": stage == "applied" and app.followup_due_at is not None,
        "next": next_step[0], "next_date": next_step[1],
        # The Application Inbox: the email about this application, and whether the employer has already replied
        # (a follow-up would then be unnecessary).
        "email_activity": inbox_service.events_for_application(db, app.id)[:10],
        "inbox_reply": inbox_service.reply_received(db, app) is not None,
        "timeline": [{"id": e.id, "kind": e.kind, "title": e.title, "detail": e.detail,
                      "at": e.occurred_at.isoformat(), **event_meta(e.meta),
                      "at_local": format_in_zone(e.occurred_at, event_meta(e.meta)["timezone"], "%-d %b %Y, %H:%M")
                      if event_meta(e.meta)["timezone"] else ""} for e in app.events],
    }


@router.get("/api/tracker")
def tracker(db: Session = Depends(get_user_db)):
    apps = db.scalars(select(Application).where(Application.applied_at.is_not(None)).order_by(desc(Application.applied_at))).all()
    payload = [app_payload(db, a) for a in apps]
    live = [a for a in payload if a["stage"] not in INACTIVE_STAGES]
    replies = [a for a in payload if a["stage"] in REPLY_STAGES]
    due = [a for a in apps if (a.stage or "applied") == "applied" and a.followup_due_at and a.followup_due_at <= utcnow()]
    extras = prefs(db).get("extra_stages") or []
    return {"stages": stages(db), "available_stages": [s for s in EXTRA_STAGES if s["id"] not in extras],
            "extra_stages": extras, "applications": payload,
            # What an email can be corrected to, wherever its Email activity is shown.
            "email_event_types": inbox_service.event_types(),
            "stats": {"live": len(live), "replies": len(replies), "due": len(due),
                      "response_rate": _pct(len(replies), len(payload)) if payload else "—"}}


class StagesIn(BaseModel):
    extra_stages: list[str] = Field(default_factory=list)


@router.put("/api/tracker/stages")
def tracker_stages(payload: StagesIn, db: Session = Depends(get_user_db)):
    valid = {s["id"] for s in EXTRA_STAGES}
    if set(payload.extra_stages) - valid:
        raise HTTPException(422, "Unknown stage")
    row = setting_row(db)
    current = dict(row.preferences or {})
    current["extra_stages"] = list(dict.fromkeys(payload.extra_stages))
    row.preferences = current
    db.commit()
    return tracker(db)


class TrackerArchiveIn(BaseModel):
    days: int = Field(ge=0, le=3650)  # 0: any age
    scope: Literal["no_reply", "live", "finished", "all"] = "no_reply"
    dry_run: bool = False


@router.post("/api/tracker/archive")
def tracker_archive(payload: TrackerArchiveIn, db: Session = Depends(get_user_db)):
    """Archive the applications submitted more than `days` ago that are in the scope's stages (app.services.tracker).
    `dry_run` only counts them, for the preview the screen shows before anything moves."""
    if payload.dry_run:
        return older_than(db, payload.days, payload.scope).summary()
    summary = archive_older_than(db, payload.days, payload.scope)
    db.commit()
    return {**summary, "tracker": tracker(db)}


@router.post("/api/applications/{application_id}/unarchive")
def unarchive_application(application_id: int, db: Session = Depends(get_user_db)):
    app = get_app(db, application_id)
    if unarchive(db, app):
        db.commit()
    return app_payload(db, app)


class StageIn(BaseModel):
    stage: str


def get_app(db: Session, application_id: int) -> Application:
    app = db.get(Application, application_id)
    if not app:
        raise HTTPException(404, "Application not found")
    return app


@router.post("/api/applications/{application_id}/stage")
def move_stage(application_id: int, payload: StageIn, db: Session = Depends(get_user_db)):
    app = get_app(db, application_id)
    if payload.stage not in ALL_STAGE_IDS:
        raise HTTPException(422, "Unknown stage")
    if not app.applied_at:
        raise HTTPException(400, "Only submitted applications can move through the tracker")
    if set_stage(db, app, payload.stage) is not None:
        db.commit()
    return app_payload(db, app)


class EventIn(BaseModel):
    kind: str = "note"
    title: str = Field(default="", max_length=300)
    detail: str = Field(default="", max_length=4000)
    occurred_at: datetime | None = None
    # Interviews: a wall-clock time in a named IANA time zone (e.g. "2026-09-16T16:00" in "Asia/Kolkata"),
    # used instead of occurred_at, plus an optional contact.
    local_time: datetime | None = None
    timezone: str = Field(default="", max_length=64)
    contact_name: str = Field(default="", max_length=200)
    contact_email: str = Field(default="", max_length=300)
    contact_phone: str = Field(default="", max_length=40)

    @field_validator("contact_email")
    @classmethod
    def _email(cls, value: str) -> str:
        return clean_email(value)

    @field_validator("contact_phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        return clean_phone(value)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        value = value.strip()
        if value:
            zone(value)
        return value

    @model_validator(mode="after")
    def _local_time_needs_zone(self) -> EventIn:
        if self.local_time is not None and self.local_time.tzinfo is None and not self.timezone:
            raise ValueError("A local time needs a time zone")
        return self


@router.post("/api/applications/{application_id}/events")
def add_event(application_id: int, payload: EventIn, db: Session = Depends(get_user_db)):
    app = get_app(db, application_id)
    if payload.kind not in ("note", "call", "email", "referral", "interview"):
        raise HTTPException(422, "Unknown event kind")
    if not (payload.title.strip() or payload.detail.strip()):
        raise HTTPException(422, "Write something to log")
    if payload.local_time is not None:
        occurred = to_utc(payload.local_time, payload.timezone)
    elif payload.occurred_at is not None:
        occurred = (payload.occurred_at.astimezone(UTC).replace(tzinfo=None) if payload.occurred_at.tzinfo
                    else payload.occurred_at)
    else:
        occurred = utcnow()
    meta = {key: value for key, value in (("timezone", payload.timezone), ("contact_name", payload.contact_name.strip()),
                                          ("contact_email", payload.contact_email),
                                          ("contact_phone", payload.contact_phone)) if value}
    db.add(ApplicationEvent(application=app, kind=payload.kind, title=payload.title.strip() or payload.kind.title(),
                            detail=payload.detail.strip(), occurred_at=occurred, meta=meta or None))
    if payload.kind == "interview" and (app.stage or "applied") in ("applied", "acknowledged", "screening"):
        db.add(ApplicationEvent(application=app, kind="stage", title="Moved to Interviewing", detail="interviewing"))
        app.stage = "interviewing"
    db.commit()
    db.refresh(app)
    return app_payload(db, app)


class RecruiterPatchIn(BaseModel):
    recruiter_name: str = ""
    recruiter_email: str = ""


@router.put("/api/applications/{application_id}/recruiter")
def set_recruiter(application_id: int, payload: RecruiterPatchIn, db: Session = Depends(get_user_db)):
    """Set the first recruiter's name and email. Other recruiters are kept."""
    app = get_app(db, application_id)
    set_primary_recruiter(app, payload.recruiter_name, payload.recruiter_email)
    db.commit()
    return app_payload(db, app)


class RecruitersIn(BaseModel):
    recruiters: list[ContactIn] = Field(default_factory=list, max_length=MAX_RECRUITERS)


@router.put("/api/applications/{application_id}/recruiters")
def replace_recruiters(application_id: int, payload: RecruitersIn, db: Session = Depends(get_user_db)):
    """Replace every recruiter on an application, in order. The first is the primary contact."""
    app = get_app(db, application_id)
    set_recruiters(app, [r.model_dump() for r in payload.recruiters])
    db.commit()
    return app_payload(db, app)


@router.post("/api/applications/{application_id}/followup-draft")
def followup_draft(application_id: int, db: Session = Depends(get_user_db)):
    app = get_app(db, application_id)
    if not app.applied_at:
        raise HTTPException(400, "Follow-ups are only drafted for submitted applications")
    try:
        draft_followup(db, app)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return app_payload(db, app)


class FollowUpSentIn(BaseModel):
    """The person sent the follow-up themselves. Every field defaults to the draft as saved."""
    sent_text: str | None = Field(default=None, max_length=followup_service.TEXT_LIMIT)
    channel: Literal["email", "linkedin", "other"] | None = None
    sent_at: datetime | None = None
    recipient: str | None = Field(default=None, max_length=followup_service.RECIPIENT_LIMIT)


class FollowUpSkipIn(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


def get_followup(db: Session, followup_id: int) -> FollowUp:
    followup = db.get(FollowUp, followup_id)
    if not followup:
        raise HTTPException(404, "Follow-up not found")
    return followup


@router.post("/api/followups/{followup_id}/sent")
def followup_sent(followup_id: int, payload: FollowUpSentIn | None = None, db: Session = Depends(get_user_db)):
    """Mark sent: the person sent this follow-up themselves (Job Hunt OS never sends). Recording it twice changes
    nothing. The same service as the MCP tool record_followup_sent, so both write the same rows."""
    payload = payload or FollowUpSentIn()
    followup = get_followup(db, followup_id)
    try:
        outcome = followup_service.mark_sent(db, followup, sent_text=payload.sent_text, channel=payload.channel,
                                      sent_at=payload.sent_at, recipient=payload.recipient, source="tracker")
    except followup_service.FollowUpRefused as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**app_payload(db, outcome.application), "followup_status": outcome.status}


@router.post("/api/followups/{followup_id}/skip")
def followup_skip(followup_id: int, payload: FollowUpSkipIn | None = None, db: Session = Depends(get_user_db)):
    """Skip: no follow-up for this application. The draft is cancelled and the follow-up date cleared."""
    followup = get_followup(db, followup_id)
    app = get_app(db, followup.application_id)
    try:
        outcome = followup_service.skip(db, app, followup=followup, reason=(payload or FollowUpSkipIn()).reason,
                                 source="tracker")
    except followup_service.FollowUpRefused as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**app_payload(db, app), "followup_status": outcome.status}


@router.post("/api/applications/{application_id}/followup-skip")
def application_followup_skip(application_id: int, payload: FollowUpSkipIn | None = None,
                              db: Session = Depends(get_user_db)):
    """Skip a follow-up that has no draft yet: the follow-up date is cleared."""
    app = get_app(db, application_id)
    try:
        outcome = followup_service.skip(db, app, reason=(payload or FollowUpSkipIn()).reason, source="tracker")
    except followup_service.FollowUpRefused as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**app_payload(db, app), "followup_status": outcome.status}


# ───────────────────────────── settings helpers ─────────────────────────────

class PreferencesIn(BaseModel):
    weekly_target: int = Field(default=10, ge=1, le=50)
    followup_days: int = Field(default=7, ge=1, le=60)
    email_run_sheet: bool = False
    warn_closing: bool = True


@router.get("/api/preferences")
def preferences_get(db: Session = Depends(get_user_db)):
    config = resolve_ai_config(get_settings(), (setting_row(db).preferences or {}).get("ai") or {})
    provider = config["llm_provider"]
    stage_models = []
    for stage, label in AI_STAGES:
        model = "mock (no API calls)" if provider == "mock" else config["stage_models"][stage]
        stage_models.append({"id": stage, "stage": label, "model": model, "source": config["stage_sources"][stage],
                             "badge": "free" if is_free_model(provider, model) else "paid"})
    return {**prefs(db), "stage_models": stage_models, "require_free": config["require_free"]}


@router.put("/api/preferences")
def preferences_put(payload: PreferencesIn, db: Session = Depends(get_user_db)):
    row = setting_row(db)
    row = lock_settings_row(db, row)
    row.preferences = {**(row.preferences or {}), **payload.model_dump(exclude_unset=True)}
    db.commit()
    return preferences_get(db)


@router.get("/api/profile/resumes")
def resume_list(db: Session = Depends(get_user_db)):
    """The resume library. A file is `active` exactly when its version is the active resume (the one selection,
    app.services.resume_verification.set_active); `in_use` says the same for the job search's default."""
    docs = db.scalars(select(ResumeDocument).order_by(desc(ResumeDocument.active), desc(ResumeDocument.id))).all()
    # A file's tags are its resume version's (the newest one it became): the library and Versions always agree.
    versions: dict[int, Resume] = {}
    for version in db.scalars(select(Resume).where(Resume.resume_document_id.is_not(None))
                              .order_by(Resume.version)).all():
        versions[version.resume_document_id] = version
    active = db.scalar(select(Resume).where(Resume.is_active.is_(True)))
    return [{"id": d.id, "filename": d.original_filename, "size": d.size, "media_type": d.media_type,
             "active": bool(active is not None and active.resume_document_id == d.id),
             "created_at": d.created_at.isoformat(), "words": len(d.extracted_text.split()),
             "in_use": bool(active is not None and active.resume_document_id == d.id),
             "version_id": versions[d.id].id if d.id in versions else None,
             "domain": versions[d.id].domain if d.id in versions else None,
             "roles": (versions[d.id].roles or []) if d.id in versions else [],
             "domains": resume_matching.groups(versions[d.id]) if d.id in versions else []} for d in docs]


@router.post("/api/profile/resumes/{document_id}/primary")
def resume_primary(document_id: int, db: Session = Depends(get_user_db)):
    """Make this file's resume version the active resume (the person's explicit choice)."""
    document = db.get(ResumeDocument, document_id)
    if not document:
        raise HTTPException(404, "Resume not found")
    profile = get_profile(db)
    version = resume_verification.version_of_document(db, profile, document)
    resume_verification.set_active(db, version)
    db.commit()
    return resume_list(db)


@router.delete("/api/profile/resumes/{document_id}")
def resume_document_delete(document_id: int, db: Session = Depends(get_user_db)):
    """Delete an uploaded file from the library. Its resume version, with its text and structured data, stays, so
    the active resume never changes because a file was removed."""
    document = db.get(ResumeDocument, document_id)
    if not document:
        raise HTTPException(404, "Resume not found")
    path = Path(document.storage_path)
    db.delete(document)
    db.commit()
    # Uploads of identical files share one stored file, so only remove it when no row still points at it.
    # A resume version keeps its original file: the uploaded original is never lost while a version uses it.
    if (path.is_file() and not db.scalar(select(ResumeDocument.id).where(ResumeDocument.storage_path == str(path)))
            and not resume_verification.storage_in_use(db, str(path))):
        path.unlink(missing_ok=True)
    return {"deleted": document_id, "promoted": None, "resumes": resume_list(db)}


@router.post("/api/profile/resume/parse-evidence")
def parse_evidence(db: Session = Depends(get_user_db)):
    profile = get_profile(db)
    if not profile.master_resume.strip():
        raise HTTPException(400, "Upload or paste a master resume first")
    try:
        result = LLMClient.for_user(db).generate_prompt_json(build_evidence_extraction_prompt(profile.master_resume),
                                                            LLMCall(workflow_type="profile", entity_id=profile.id))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    existing = {e.claim.strip().lower() for e in db.scalars(select(Evidence)).all()}
    added = 0
    extracted: list[Evidence] = []
    for item in result.get("claims") or []:
        claim = str(item.get("claim", "")).strip() if isinstance(item, dict) else ""
        if not claim or claim.lower() in existing:
            continue
        # Parsed claims start unverified: only a human can make them eligible for tailoring. They come from the
        # active resume's text, so they are that resume's evidence (an AUTO tag the person can change).
        row = Evidence(claim=claim[:2000], company=str(item.get("company", ""))[:200], role=str(item.get("role", ""))[:200],
                       skills=[str(s) for s in item.get("skills") or []][:12], verified=False,
                       source="llm_extraction")
        db.add(row)
        extracted.append(row)
        existing.add(claim.lower())
        added += 1
    evidence_tags.link_extracted(db, resume_verification.active_resume(db, profile, create=False), extracted)
    db.commit()
    return {"added": added}


class EvidencePatchIn(BaseModel):
    verified: bool


@router.get("/api/evidence/diagnostics")
def evidence_diagnostics(db: Session = Depends(get_user_db)):
    """Evidence whose employer or role disagrees with the resume item it came from. Read-only: nothing is corrected
    here, and ERROR-level items are left out of tailoring until the candidate fixes them."""
    from app.services.evidence_provenance import diagnose

    issues = [issue.as_dict() for issue in diagnose(db, get_profile(db).user_id)]
    return {"issues": issues, "held_back_from_generation": sorted({i["evidence_id"] for i in issues
                                                                    if i["severity"] == "ERROR"})}


@router.patch("/api/evidence/{evidence_id}")
def evidence_patch(evidence_id: int, payload: EvidencePatchIn, db: Session = Depends(get_user_db)):
    item = db.get(Evidence, evidence_id)
    if not item:
        raise HTTPException(404, "Evidence not found")
    item.verified = payload.verified
    db.commit()
    return {"id": item.id, "verified": item.verified}


@router.delete("/api/evidence/{evidence_id}")
def evidence_delete(evidence_id: int, db: Session = Depends(get_user_db)):
    item = db.get(Evidence, evidence_id)
    if not item:
        raise HTTPException(404, "Evidence not found")
    db.delete(item)
    db.commit()
    return {"ok": True}


@router.post("/api/search/apify/runs/{run_id}/rank", status_code=202)
def rank_run_jobs(run_id: int, request: Request, background: BackgroundTasks, db: Session = Depends(get_user_db)):
    """Queue the unranked jobs a run pulled in. Imports already queue new jobs; this only catches older ones."""
    run = db.get(SearchRun, run_id)
    if not run:
        raise HTTPException(404, "Search run not found")
    ids = db.scalars(select(Job.id).where(Job.search_run_id == run.id, Job.fit_score.is_(None),
                                          Job.state.in_(ranker.RANK_WAITING_STATES))).all()
    out = ranker.request_ranking(db, ids, force=False, only_unranked=True, actor=f"user:{db.info['user_id']}")
    run_soon(background, request.app, "ranking")
    return out


class KnowledgeUpdateIn(BaseModel):
    question: str = Field(min_length=2)
    answer: str = ""
    category: str = "general"
    answer_type: str = "text"
    verified: bool = False
    sensitive: bool = False


@router.put("/api/knowledge/{item_id}")
def knowledge_put(item_id: int, payload: KnowledgeUpdateIn, db: Session = Depends(get_user_db)):
    item = db.get(KnowledgeAnswer, item_id)
    if not item:
        raise HTTPException(404, "Knowledge answer not found")
    key = question_key(payload.question)
    clash = db.scalar(select(KnowledgeAnswer).where(KnowledgeAnswer.question_key == key, KnowledgeAnswer.id != item.id))
    if clash:
        raise HTTPException(409, "Another saved answer already covers this question")
    for field, value in payload.model_dump().items():
        setattr(item, field, value)
    item.question_key = key
    if item.verified and (item.answer or "").strip():
        from app.services import application_execution

        application_execution.wake_waiting(db, item.user_id)  # an execution waiting for this answer can go on
    db.commit()
    return {"id": item.id, "verified": item.verified}


# ───────────────────────────── Apify run sync ─────────────────────────────

def _apify_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return "Apify rejected the token" if code in (401, 403) else "Apify run not found" if code == 404 else f"Apify returned HTTP {code}"
    if isinstance(exc, httpx.HTTPError):
        return f"Could not reach Apify ({exc.__class__.__name__})"
    return str(exc)


@router.get("/api/search/apify/remote-runs")
def remote_runs(limit: int = Query(20, ge=1, le=50), db: Session = Depends(get_user_db)):
    """Runs in the connected Apify account, marked with whether Job Hunt OS has imported them."""
    try:
        return {"runs": list_remote_runs(db, limit)}
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, _apify_error(exc)) from exc


class ImportRunsIn(BaseModel):
    external_run_ids: list[str] = Field(min_length=1, max_length=25)
    reimport: bool = False


def _sync_results(db: Session, work) -> dict:
    results = []
    for label, action in work:
        try:
            results.append({**action(), "ok": True})
        except Exception as exc:  # one bad run must not stop the others
            db.rollback()
            results.append({"external_run_id": label, "ok": False, "error": _apify_error(exc)})
    ok = [r for r in results if r["ok"]]
    return {"results": results, "created": sum((r.get("import") or {}).get("created", 0) for r in ok),
            "duplicates": sum((r.get("import") or {}).get("duplicates", 0) for r in ok)}


@router.post("/api/search/apify/import")
def import_runs(payload: ImportRunsIn, db: Session = Depends(get_user_db)):
    """Import runs by Apify run ID: track them, normalize their items and skip duplicates."""
    if not apify_token(db):
        raise HTTPException(400, "Add your Apify API token in Settings")
    bad = [rid for rid in payload.external_run_ids if not re.fullmatch(r"[A-Za-z0-9]{6,40}", rid)]
    if bad:
        raise HTTPException(422, f"Invalid Apify run IDs: {', '.join(bad[:3])}")
    work = [(rid, lambda rid=rid: import_remote_run(db, rid, payload.reimport)) for rid in dict.fromkeys(payload.external_run_ids)]
    return _sync_results(db, work)


@router.post("/api/search/apify/runs/refresh-pending")
def refresh_pending_runs(db: Session = Depends(get_user_db)):
    """Check every run that has not finished yet and import the ones that have."""
    if not apify_token(db):
        raise HTTPException(400, "Add your Apify API token in Settings")
    pending = db.scalars(select(SearchRun).where(SearchRun.provider == "apify", SearchRun.completed_at.is_(None))
                         .order_by(desc(SearchRun.id)).limit(25)).all()
    return _sync_results(db, [(run.external_run_id, lambda run=run: refresh_apify_run(db, run)) for run in pending])
