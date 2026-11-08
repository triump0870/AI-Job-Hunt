"""Is this job in the candidate's domain? Deterministic, no model: quality over quantity at discovery.

    new job → assess(job, rules)
      MATCH         → description recovery / parsing / ranking as usual
      NOT_MATCHING  → hidden (SKIPPED "Outside your domain: sales"), never recovered, parsed or ranked; listed in
                      Discovery → Not matching, where the person can keep it (KEPT, never re-assessed)

Rules come from the account: its target roles, headline and resume (roles and skills), the roles of its saved
searches, its own exclude and include lists (seeded with common off-domain areas), and terms learned from the jobs
it dismissed (app.services.feedback_learning). A term the candidate's own profile uses is never excluded, and an
include term always wins. When unsure, the job is kept: only a clear term match hides a job.

With DOMAIN_CLASSIFIER_MODE=on this module keeps only the unambiguous decisions (`triage`). Everything else goes to the
domain classifier (app.services.domain_classifier), and the job waits as PENDING, held from every worker. `assess` stays
as it is: it is what the mode `off` runs, and the baseline every comparison measures against.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    Job,
    JobClassificationTask,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobRankingTask,
)
from app.services.lifecycle import WorkflowState as S
from app.services.lifecycle import can_transition
from app.settings import get_settings

MATCH, NOT_MATCHING, KEPT, PENDING, REVIEW = "MATCH", "NOT_MATCHING", "KEPT", "PENDING", "REVIEW"
HELD = (NOT_MATCHING, PENDING, REVIEW)  # no recovery, parse or ranking may start for these
WAITING = (PENDING, REVIEW)  # held, but not decided against: for the classifier, or for the person (Discovery → Review)
HARD, POSITIVE, ASK = "HARD", "POSITIVE", "ASK"  # what triage decides
# Off-domain areas: excluded unless the candidate's own target roles name them.
DEFAULT_DOMAIN_EXCLUDES = ("sales", "account executive", "account manager", "business development", "bdr", "sdr",
                           "digital marketing", "marketing", "seo", "sem", "social media", "content writer",
                           "copywriter", "recruiter", "talent acquisition", "customer support", "customer service",
                           "telecaller", "telesales")
# Skill sets: excluded in a title that names none of the candidate's own skills.
DEFAULT_SKILL_EXCLUDES = ("c++", "java", ".net", "php", "frontend", "front-end", "front end", "ui developer",
                          "react developer", "angular", "ios", "android", "mobile developer", "qa", "test engineer",
                          "manual testing")
# Words in a role that say nothing about its domain.
GENERIC_ROLE_WORDS = frozenset({
    "engineer", "engineering", "developer", "development", "senior", "sr", "junior", "jr", "lead", "staff",
    "principal", "head", "manager", "director", "vp", "chief", "officer", "specialist", "associate", "intern",
    "architect", "consultant", "analyst", "scientist", "i", "ii", "iii", "iv", "of", "and", "the", "for", "with",
    "to", "in", "at", "a", "an", "remote", "hybrid", "team", "member", "expert", "professional", "executive"})
DESCRIPTION_WINDOW = 3000
# A title with one of these describes technical work, so a word beside it ("Marketing", "Customer Service") does not by
# itself say the job is off-domain: triage asks the classifier instead of hiding it. German and French postings too.
TECHNICAL_ROLE_WORDS = ("engineer", "engineers", "engineering", "developer", "developers", "programmer", "architect",
                        "scientist", "researcher", "devops", "sre", "swe", "sde", "technical lead", "tech lead",
                        "ingenieur", "ingenieurin", "entwickler", "entwicklerin", "ingénieur", "développeur")
# Titles that name the same role. Applied to both sides before comparing, in this order: "SDE 3" and "Senior Software
# Engineer", "FDE" and "Forward Deployed Engineer", "Artificial Intelligence Engineer" and "AI Engineer". Only ever
# protects a job or forwards it, never hides one.
ROLE_EQUIVALENTS = tuple((re.compile(pattern), name) for pattern, name in (
    (r"\bsr\b\.?", "senior"),
    (r"\b(?:engg|engr)\b\.?", "engineer"),
    (r"\bartificial intelligence\b", "ai"),
    (r"\bmachine learning\b", "ml"),
    (r"\bforward[ -]deploy(?:ed|ment)(?: software)?[ -]engineers?\b", "forward deployed engineer"),
    (r"\bfdes?\b", "forward deployed engineer"),
    (r"\b(?:sde|swe|software development engineer|software developer|software engineer)[ -]*(?:3|iii)\b",
     "senior software engineer"),
    (r"\b(?:sde|swe|software development engineer|software developer|software engineer)[ -]*(?:4|iv)\b",
     "staff software engineer"),
    (r"\b(?:sde|swe|software development engineer|software developer)(?:[ -]*(?:1|2|i|ii))?\b", "software engineer"),
))
# Skills that are also everyday words or people skills: a posting naming them says nothing about the kind of work.
UNINFORMATIVE_SKILLS = frozenset({"go", "r", "c", "hiring", "mentorship", "mentoring", "okrs", "leadership", "cursor",
                                  "engineering leadership", "engineering management", "architecture reviews",
                                  "client integration", "enterprise deployments"})
# Other names of a skill that a whole-word match cannot see. Only protects a job or forwards it, never hides one.
SKILL_SYNONYMS = {"genai": ("generative ai", "gen ai"), "generative ai": ("genai", "gen ai"),
                  "llm": ("large language model",), "ai": ("artificial intelligence", "ki"),
                  "ml": ("machine learning",), "machine learning": ("ml",)}


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _norm(term: str) -> str:
    return re.sub(r"\s+", " ", (term or "").strip().lower())


def term_pattern(term: str) -> re.Pattern[str]:
    """A whole-word match that works for C++, .NET and "front-end": "java" never matches "javascript"."""
    return re.compile(rf"(?<![\w+#.]){re.escape(_norm(term))}(?![\w+#])")


def contains(text: str, term: str) -> bool:
    return bool(term) and term_pattern(term).search(_norm(text)) is not None


@lru_cache(maxsize=512)
def _skill_pattern(skill: str) -> re.Pattern[str]:
    from app.services.resume_parsing import SKILL_ALIASES

    name = _norm(skill)
    forms = {name, *SKILL_SYNONYMS.get(name, ()), *SKILL_ALIASES.get(name, ())}
    body = "|".join(sorted((re.escape(_norm(form)) for form in forms if form), key=len, reverse=True))
    return re.compile(rf"(?<![\w+#.])(?:{body})(?:s|es)?(?![\w+#])")


def names_skill(text: str, skill: str) -> bool:
    """Like `contains`, but "LLMs" names `llm`, "Generative AI" names `genai` and "k8s" names `kubernetes`."""
    return bool(skill and skill.strip()) and _skill_pattern(skill).search(_norm(text)) is not None


def canonical_title(title: str) -> str:
    text = _norm(title)
    for pattern, name in ROLE_EQUIVALENTS:
        text = pattern.sub(name, text)
    return text


def names_role(title: str, role: str) -> bool:
    """The title names this role, under any of its equivalent names."""
    return contains(canonical_title(title), canonical_title(role))


def skills_found(text: str, skills: list[str]) -> list[str]:
    """Which of the candidate's skills this text names, leaving out those that are also everyday words."""
    return [s for s in skills if _norm(s) not in UNINFORMATIVE_SKILLS and names_skill(text, s)]


def role_tokens(roles: list[str]) -> set[str]:
    """The words of the candidate's roles that name a domain: "AI" and "backend" from "Senior Backend AI Engineer"."""
    return {w for role in roles for w in re.findall(r"[a-z0-9+#.]+", role.lower())
            if w not in GENERIC_ROLE_WORDS and len(w) > 1}


@dataclass
class Rules:
    enabled: bool = True
    domain_excludes: list[str] = field(default_factory=list)
    skill_excludes: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    learned_titles: list[dict[str, Any]] = field(default_factory=list)
    learned_companies: list[dict[str, Any]] = field(default_factory=list)

    @property
    def role_words(self) -> set[str]:
        return role_tokens(self.roles)


@dataclass(frozen=True)
class Verdict:
    match: bool
    code: str  # MATCH | DOMAIN | SKILL | DESCRIPTION | LEARNED
    reason: str = ""


@dataclass(frozen=True)
class Triage:
    """What the rules can settle alone. HARD hides, POSITIVE forwards, ASK goes to the classifier. `signals` are the
    doubts behind an ASK: they never hide a job, and each one rules the positive shortcut out."""

    route: str
    code: str = ""
    reason: str = ""
    signals: tuple[str, ...] = ()


# ───────────── rules ─────────────

def preferences(db: Session, user_id: int) -> dict[str, Any]:
    """The account's saved domain settings with defaults: {enabled, exclude_terms, skill_excludes, include_terms,
    learn_from_feedback, learned}."""
    from app.models import AppSetting

    row = db.scalar(select(AppSetting).where(AppSetting.user_id == user_id))
    saved = ((row.preferences if row else None) or {}).get("domain") or {}
    return {"enabled": saved.get("enabled", True),
            "exclude_terms": list(saved.get("exclude_terms", DEFAULT_DOMAIN_EXCLUDES)),
            "skill_excludes": list(saved.get("skill_excludes", DEFAULT_SKILL_EXCLUDES)),
            "include_terms": list(saved.get("include_terms", [])),
            "learn_from_feedback": saved.get("learn_from_feedback", True),
            "learned": list(saved.get("learned", []))}


def derived(db: Session, user_id: int) -> dict[str, list[str]]:
    """Roles and skills the account's own data names: profile, active resume, saved searches (roles, primary skills)."""
    from app.models import CandidateProfile, Resume, SavedSearch

    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user_id))
    roles: list[str] = list((profile.target_roles if profile else None) or [])
    skills: list[str] = list(((profile.search_preferences or {}) if profile else {}).get("primary_skills") or [])
    if profile is not None and profile.headline:
        roles.append(profile.headline)
    resume = db.scalar(select(Resume).where(Resume.user_id == user_id, Resume.is_active.is_(True)))
    if resume is not None and resume.structured_data:
        from app.services.structured_resume import derive

        try:
            facts = derive(resume.structured_data)
        except Exception:  # noqa: BLE001  a half-parsed resume still leaves the profile's own roles
            facts = {}
        roles += facts.get("primary_roles") or []
        skills += facts.get("primary_skills") or []
    for setup in db.scalars(select(SavedSearch).where(SavedSearch.user_id == user_id)).all():
        roles += [r for r in ((setup.settings or {}).get("roles") or []) if isinstance(r, str)]
        skills += [s for s in ((setup.settings or {}).get("primary_skills") or []) if isinstance(s, str)]
    clean = lambda values: list(dict.fromkeys(_norm(v) for v in values if isinstance(v, str) and v.strip()))
    return {"roles": clean(roles), "skills": clean(skills)}


def resume_skills(db: Session, user_id: int) -> list[str]:
    """Every skill in the structured data of all the account's resumes, not just the active one's first ten: a job may
    well be matched to another resume. Skills a person rejected are left out."""
    from app.models import Resume

    names: list[str] = []
    for data in db.scalars(select(Resume.structured_data).where(Resume.user_id == user_id)).all():
        for skill in (data or {}).get("skills") or []:
            if isinstance(skill, dict) and skill.get("name") and str(skill.get("status") or "").upper() != "REJECTED":
                names.append(_norm(str(skill["name"])))
    return list(dict.fromkeys(names))


def rules_for(db: Session, user_id: int, wide: bool | None = None) -> Rules:
    """`wide` None follows DOMAIN_CLASSIFIER_MODE; False is always the old, narrower rule set (the evaluation baseline)."""
    prefs, own = preferences(db, user_id), derived(db, user_id)
    words = role_tokens(own["roles"])
    own_skills = set(own["skills"])
    # A term the candidate's own roles or skills use is never an exclusion ("sales engineer", "Java" on the resume).
    domain = [t for t in map(_norm, prefs["exclude_terms"])
              if t and not any(contains(role, t) for role in own["roles"])]
    # "java" is the candidate's own when a skill names it, as in "Java 17" or "Java Spring".
    skill = [t for t in map(_norm, prefs["skill_excludes"])
             if t and t not in words and not any(contains(own, t) for own in own_skills)]
    learned = [e for e in prefs["learned"] if e.get("status") == "active"] if prefs["learn_from_feedback"] else []
    from app.services import domain_classifier

    # The hybrid reads every resume's skills. Off mode keeps the old, narrower list byte for byte.
    if domain_classifier.mode() == domain_classifier.ON if wide is None else wide:
        own = {**own, "skills": list(dict.fromkeys([*own["skills"], *resume_skills(db, user_id)]))}
        skill = [t for t in skill if not any(contains(mine, t) for mine in own["skills"])]
    return Rules(enabled=bool(prefs["enabled"]) and get_settings().domain_filter_enabled, domain_excludes=domain,
                 skill_excludes=skill, includes=[t for t in map(_norm, prefs["include_terms"]) if t],
                 roles=own["roles"], skills=own["skills"],
                 learned_titles=[e for e in learned if e.get("kind") == "title"],
                 learned_companies=[e for e in learned if e.get("kind") == "company"])


# ───────────── the verdict ─────────────

def assess(job: Any, rules: Rules) -> Verdict:
    """MATCH unless a clear rule says otherwise. Include terms always win."""
    title = _norm(getattr(job, "title", "") or "")
    if not rules.enabled or not title:
        return Verdict(True, MATCH)
    if any(contains(title, term) for term in rules.includes):
        return Verdict(True, MATCH, "Matches one of your include terms")
    hit = next((term for term in rules.domain_excludes if contains(title, term)), None)
    if hit:
        return Verdict(False, "DOMAIN", f"Outside your domain: {hit}")
    has_own_skill = any(contains(title, skill) for skill in rules.skills)
    has_role_word = any(contains(title, word) for word in rules.role_words)
    hit = next((term for term in rules.skill_excludes if contains(title, term)), None)
    if hit and not has_own_skill:
        return Verdict(False, "SKILL", f"Different skill set: {hit}")
    company = _norm(getattr(job, "company", "") or "")
    learned_company = next((e for e in rules.learned_companies if company and _norm(e.get("term", "")) == company), None)
    if learned_company:
        return Verdict(False, "LEARNED", f"You dismissed {learned_company.get('evidence', 0)} jobs at this company")
    learned = next((e for e in rules.learned_titles if contains(title, e.get("term", ""))), None)
    if learned and not has_own_skill:
        return Verdict(False, "LEARNED", f"Similar to jobs you dismissed: '{learned['term']}' "
                                         f"({learned.get('evidence', 0)}×)")
    if not has_own_skill and not has_role_word and rules.skills:
        body = _norm((getattr(job, "description", "") or "")[:DESCRIPTION_WINDOW])
        foreign = [term for term in rules.skill_excludes if contains(body, term)]
        if len(foreign) >= 2 and not any(contains(body, skill) for skill in rules.skills):
            return Verdict(False, "DESCRIPTION", f"Different skill set: {', '.join(foreign[:3])}")
    return Verdict(True, MATCH)


def _technical(title: str) -> bool:
    return any(contains(title, word) for word in TECHNICAL_ROLE_WORDS)


def own_title(title: str, rules: Rules) -> bool:
    """The title names one of the candidate's target roles or skills: it is never hidden on a model's word alone."""
    return any(names_role(title, role) for role in rules.roles) or bool(skills_found(title, rules.skills))


def triage(job: Any, rules: Rules, scope_signals: tuple[str, ...] = ()) -> Triage:
    """Only the unambiguous decides here (AGENTS.md: Discovery quality):

    HARD      a term the person chose (their own exclude list, "Always exclude"); a default area in a title that names
              no technical work ("Founding Account Executive"); a skill in a title with none of the candidate's role
              words or skills ("Senior Java Developer"); a company with enough explicit dismissals and no keeps.
    POSITIVE  an include term; or a target role with a domain word, one of the candidate's skills in the title or text,
              and no doubt. Spares the model call.
    ASK       everything else. Learned title words, foreign skills beside the candidate's own role words and search
              scope misses are signals: they send a job to the model, and never hide it.
    """
    title = _norm(getattr(job, "title", "") or "")
    if not rules.enabled or not title:
        return Triage(POSITIVE, MATCH)
    if any(contains(title, term) for term in rules.includes):
        return Triage(POSITIVE, MATCH, "Matches one of your include terms")
    signals = list(scope_signals)
    technical = _technical(title)
    for term in rules.domain_excludes:
        if contains(title, term):
            if term not in DEFAULT_DOMAIN_EXCLUDES or not technical:  # the person's own term, or plainly not technical
                return Triage(HARD, "DOMAIN", f"Outside your domain: {term}")
            signals.append(f"DOMAIN:{term}")
    has_own_skill = bool(skills_found(title, rules.skills))
    has_role_word = any(contains(canonical_title(title), word) for word in rules.role_words)
    for term in rules.skill_excludes:
        if contains(title, term) and not has_own_skill:
            if not has_role_word:
                return Triage(HARD, "SKILL", f"Different skill set: {term}")
            signals.append(f"SKILL:{term}")
    company = _norm(getattr(job, "company", "") or "")
    for entry in rules.learned_companies:
        if company and _norm(entry.get("term", "")) == company:
            # Only what cleaned learning counted (feedback_learning.counted_events): never a bulk cleanup.
            if entry.get("clean") and not entry.get("positive"):
                return Triage(HARD, "LEARNED", f"You dismissed {entry.get('evidence', 0)} jobs at this company")
            signals.append(f"LEARNED_COMPANY:{company}")
    signals += [f"LEARNED:{e['term']}" for e in rules.learned_titles if contains(title, e.get("term", ""))]
    body = _norm((getattr(job, "description", "") or "")[:DESCRIPTION_WINDOW])
    if not has_own_skill and not has_role_word and rules.skills:
        foreign = [term for term in rules.skill_excludes if contains(body, term)]
        if len(foreign) >= 2 and not skills_found(body, rules.skills):
            signals.append(f"DESCRIPTION:{','.join(foreign[:3])}")
    if not signals:
        role = next((r for r in rules.roles if names_role(title, r) and role_tokens([r])), None)
        if role and (has_own_skill or skills_found(body, rules.skills)):
            return Triage(POSITIVE, MATCH, f"The title names your role: {role}")
    return Triage(ASK, signals=tuple(dict.fromkeys(signals)))


# ───────────── acting on it ─────────────

def _cancel_pending_work(db: Session, job: Job, now: datetime) -> None:
    """A hidden job spends nothing: waiting recovery, parse and ranking tasks are closed."""
    reason = "Hidden: outside your domain"
    for task in db.scalars(select(JobDescriptionFetchTask).where(JobDescriptionFetchTask.job_id == job.id,
                                                                JobDescriptionFetchTask.status == "PENDING")).all():
        task.status, task.result_code, task.completed_at, task.last_error = "SKIPPED", NOT_MATCHING, now, reason
    for task in db.scalars(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job.id,
                                                                JobDescriptionParseTask.status == "PENDING")).all():
        task.status, task.completed_at, task.last_error = "FAILED", now, reason
    for task in db.scalars(select(JobRankingTask).where(JobRankingTask.job_id == job.id,
                                                       JobRankingTask.status == "PENDING")).all():
        task.status, task.completed_at, task.last_error = "SKIPPED", now, reason
        task.locked_by = task.lock_token = task.lock_expires_at = None
    for task in db.scalars(select(JobClassificationTask).where(JobClassificationTask.job_id == job.id,
                                                              JobClassificationTask.mode == "ON",
                                                              JobClassificationTask.status == "PENDING")).all():
        task.status, task.completed_at, task.last_error = "SKIPPED", now, reason


def mark(job: Job, verdict: Verdict, now: datetime | None = None) -> None:
    job.domain_match = MATCH if verdict.match else NOT_MATCHING
    job.domain_reason = verdict.reason[:300] or None
    job.domain_checked_at = now or utcnow()


def hide(db: Session, job: Job, verdict: Verdict, *, actor: str, now: datetime | None = None) -> None:
    """Mark a job not matching and take it out of the pipeline. Only jobs not ranked yet are moved; a ranked job
    keeps its state (it is only marked). Reversible with keep()."""
    from app.services.work_queue import transition

    now = now or utcnow()
    mark(job, verdict, now)
    _cancel_pending_work(db, job, now)
    if job.state is S.DISCOVERED:
        transition(db, job, S.NORMALIZED, actor=actor, reason="Title and company present", now=now)
    if job.state in (S.NORMALIZED, S.FILTERED, S.RANK_PENDING):
        transition(db, job, S.SKIPPED, actor=actor, reason=verdict.reason, now=now)


def not_held() -> Any:
    """SQL: the job may be worked on (not hidden, and not waiting for the domain classifier)."""
    return or_(Job.domain_match.is_(None), Job.domain_match.not_in(HELD))


def screen(db: Session, job: Job, rules_cache: dict[int, Rules] | None = None, *, actor: str = "discovery",
           now: datetime | None = None, search_run_id: int | None = None) -> bool:
    """Assess a job not assessed yet, hiding it when it does not match. True when it may carry on. A job sent to the
    domain classifier (mode on) may not carry on yet: `job.domain_match` is then PENDING."""
    if job.domain_match in (MATCH, KEPT):
        return True
    if job.domain_match in HELD:
        return False
    cache = rules_cache if rules_cache is not None else {}
    if job.user_id not in cache:
        cache[job.user_id] = rules_for(db, job.user_id)
    from app.services import domain_classifier

    if domain_classifier.mode() == domain_classifier.ON:
        return domain_classifier.screen(db, job, cache[job.user_id], actor=actor, now=now, search_run_id=search_run_id)
    verdict = assess(job, cache[job.user_id])
    if verdict.match:
        mark(job, verdict, now)
        return True
    hide(db, job, verdict, actor=actor, now=now)
    return False


def keep(db: Session, job: Job, *, actor: str, now: datetime | None = None) -> str | None:
    """The person keeps a hidden job: it is never assessed again and goes through recovery, parsing and ranking
    like any new job. Returns why ranking could not queue it, or None."""
    now = now or utcnow()
    job.domain_match, job.domain_reason, job.domain_checked_at = KEPT, "Kept by you", now
    return forward(db, job, actor=actor, now=now, reason="Kept although it looked outside your domain")


def forward(db: Session, job: Job, *, actor: str, now: datetime, reason: str) -> str | None:
    """A job the person kept or the classifier let through goes on like any new job: back out of SKIPPED, a resume, then
    recovery, parsing and ranking. Returns why ranking could not queue it, or None."""
    from app.services import jd_recovery, job_description, ranker
    from app.services.work_queue import transition

    if job.state is S.SKIPPED and can_transition(job.state, S.NORMALIZED, previous_state=job.previous_state):
        transition(db, job, S.NORMALIZED, actor=actor, reason=reason, now=now)
    from app.services import discovery

    discovery.choose_resume(db, job)  # hidden jobs were never matched to a resume
    from app.services import qualification

    if qualification.routes(db) and job.state in ranker.PRE_RANK_STATES:
        qualification.dispatch(db, job, reason="user_request", now=now)
        return None
    if not jd_recovery.on_new_job(db, job):
        job_description.queue_parse(db, job, reason="user_request", now=now)
    if job.state in ranker.PRE_RANK_STATES:
        return ranker.queue_job(db, job, policy=ranker.policy_for(db, job.user_id), actor=actor, now=now)
    return None


def reapply(db: Session, user_id: int, *, actor: str) -> dict[str, int]:
    """Assess the account's unranked jobs again after its lists changed. Kept jobs are never touched; a job that now
    matches is put back in the queue. Does not commit."""
    from app.services import domain_classifier, search_scope

    if domain_classifier.mode() == domain_classifier.ON:
        return domain_classifier.reapply(db, user_id, actor=actor)
    rules = rules_for(db, user_id)
    hidden = restored = 0
    jobs = db.scalars(select(Job).where(Job.user_id == user_id, Job.domain_match.is_distinct_from(KEPT),
                                        Job.state.in_((S.DISCOVERED, S.NORMALIZED, S.FILTERED, S.RANK_PENDING,
                                                       S.SKIPPED)))).all()
    for job in jobs:
        was_hidden = job.domain_match == NOT_MATCHING
        if job.state is S.SKIPPED and not was_hidden:
            continue  # skipped for another reason
        verdict = assess(job, rules)
        if verdict.match and was_hidden and search_scope.hidden_by_scope(job):
            scoped = search_scope.verdict_for(db, job)
            if scoped is not None and not scoped.match:
                mark(job, scoped)  # hidden by its own search's scope, which these lists do not decide
                continue
        if verdict.match and was_hidden:
            keep(db, job, actor=actor)
            job.domain_match, job.domain_reason = MATCH, None
            restored += 1
        elif verdict.match:
            mark(job, verdict)
        elif not was_hidden:
            hide(db, job, verdict, actor=actor)
            hidden += 1
        else:
            mark(job, verdict)
    return {"hidden": hidden, "restored": restored, "checked": len(jobs)}


def sweep(db: Session, limit: int = 200) -> dict[str, int]:
    """Assess not-yet-assessed jobs that have not been ranked (the backlog from before this feature). Commits."""
    jobs = db.scalars(select(Job).where(Job.domain_match.is_(None), Job.user_id.is_not(None),
                                        Job.state.in_((S.DISCOVERED, S.NORMALIZED, S.FILTERED, S.RANK_PENDING)))
                      .order_by(Job.id.desc()).limit(max(1, limit))).all()
    cache: dict[int, Rules] = {}
    hidden = sum(0 if screen(db, job, cache, actor="domain_filter") else 1 for job in jobs)
    db.commit()
    return {"assessed": len(jobs), "hidden": hidden}
