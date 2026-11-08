"""Was this job what its search asked for? Deterministic, no model: job boards match keywords loosely.

    new job from a search run → assess(job, run's roles, locations, primary skills)
      in scope      → carries on (domain filter already passed)
      out of scope  → hidden like the domain filter (Discovery → Not matching), never recovered, parsed or ranked;
                      the person can keep it (app.services.domain_match.keep)
    recovered description (app.services.jd_recovery) → checked again before parsing, since the skill check needs text
    domain lists reapplied (domain_match.reapply)     → a job this check hid stays hidden

Only a clear miss hides a job: a run that recorded no roles, a job without a location, or a description not fetched
yet is never held against it.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from app.models import Job, SearchRun
from app.services import domain_match
from app.services.apify_adapters import Country, resolve_country
from app.services.domain_match import Verdict, contains, role_tokens
from app.services.locations import location_key

ROLE, SKILL, LOCATION = "SCOPE_ROLE", "SCOPE_SKILL", "SCOPE_LOCATION"
# Reason prefixes, so a hidden job still says which check hid it after the verdict object is gone.
ROLE_REASON, SKILL_REASON = "Not one of this search's roles", "Does not mention"
LOCATION_REASON, ONSITE_REASON = "Outside this search's location", "On-site, but this search is remote only"
SCOPE_REASONS = (ROLE_REASON, SKILL_REASON, LOCATION_REASON, ONSITE_REASON)
# Cities that boards spell more than one way.
CITY_ALIASES = {"bangalore": "bengaluru", "gurgaon": "gurugram", "bombay": "mumbai", "nyc": "new york",
                "new york city": "new york", "madras": "chennai", "calcutta": "kolkata", "new delhi": "delhi"}
ONSITE_WORDS = ("on-site", "onsite", "on site", "in-office", "in office")


def location_country(location: str) -> Country | None:
    """The country a location names, read from its last part first: "London, Ontario, Canada" is Canada even though
    London is also a UK city."""
    for part in reversed([p for p in (location or "").split(",") if p.strip()]):
        found = resolve_country("", part)
        if found:
            return found
    return None


def _city(location: str) -> str:
    key = re.sub(r"\s+", " ", location_key(location).lower()).strip()
    return CITY_ALIASES.get(key, key)


def _remote(location: str) -> bool:
    return "remote" in (location or "").lower()


def is_onsite(job: Any) -> bool:
    """Only when the posting says so: an on-site work type or location. Boards that leave it out are unknown."""
    raw = getattr(job, "raw", None) or {}
    values = [getattr(job, "remote_type", None), getattr(job, "location", None)]
    if isinstance(raw, dict):
        values += [raw.get(k) for k in ("work_from_home_type", "workplace_type", "work_type", "remote_type")]
    return any(isinstance(v, str) and any(w in v.lower() for w in ONSITE_WORDS) for v in values)


def _place_accepts(place: str, where: str, distance: int) -> bool:
    if _remote(place):
        return True  # a remote search returns postings labelled with any city; on-site ones are caught separately
    wanted, found = location_country(place), location_country(where)
    if wanted and found and wanted.indeed_name != found.indeed_name:
        return False
    return _remote(where) or distance != 0 or _city(where) == _city(place)


def assess(job: Any, roles: list[str], locations: list[str], skills: list[str], *, distance: int = 50,
           remote_only: bool = False) -> Verdict:
    """MATCH unless the title names none of the roles (or skills), the description none of the skills, the job is
    clearly on-site for a remote-only search, or its location fits none of the searched places (another country, or
    another city with a zero search radius)."""
    title = getattr(job, "title", "") or ""
    words = role_tokens(roles)
    if words and not any(contains(title, w) for w in words) and not any(contains(title, s) for s in skills):
        return Verdict(False, ROLE, f"{ROLE_REASON}: {', '.join(roles)}"[:300])

    description = getattr(job, "description", "") or ""
    if skills and description.strip() and not any(contains(f"{title}\n{description}", s) for s in skills):
        return Verdict(False, SKILL, f"{SKILL_REASON} {', '.join(skills)}"[:300])

    return _place_miss(job, locations, distance, remote_only) or Verdict(True, domain_match.MATCH)


def _place_miss(job: Any, locations: list[str], distance: int, remote_only: bool) -> Verdict | None:
    """A posting that is on-site for a remote-only search, or in a place the search did not cover. A fact, not a guess."""
    if remote_only and is_onsite(job):
        return Verdict(False, LOCATION, ONSITE_REASON)
    where = getattr(job, "location", "") or ""
    places = [p for p in locations if p and p.strip()]
    if places and where.strip() and not any(_place_accepts(p, where, distance) for p in places):
        return Verdict(False, LOCATION, f"{LOCATION_REASON}: {where} (searched {'; '.join(places)})"[:300])
    return None


def verdict_for(db: Session, job: Job, search_run_id: int | None = None) -> Verdict | None:
    """The job against its own search run's scope; None when the run recorded none (older runs, plain imports)."""
    scope = _scope_of(db, job, search_run_id)
    if scope is None:
        return None
    roles, places, skills, distance, remote_only = scope
    return assess(job, roles, places, skills, distance=distance, remote_only=remote_only)


def _scope_of(db: Session, job: Job, search_run_id: int | None) -> tuple[list[str], list[str], list[str], int, bool] | None:
    run_id = search_run_id or job.search_run_id
    run = db.get(SearchRun, run_id) if run_id else None
    if run is None or not run.roles:
        return None
    params = run.search_params or {}
    skills = [s for s in params.get("primary_skills") or [] if isinstance(s, str) and s.strip()]
    # Runs covering several places record each one; a one-place run (and Hunter runs) only has run.location.
    places = [p for p in params.get("locations") or [] if isinstance(p, str)] or [run.location or ""]
    distance = params.get("distance")
    return list(run.roles), places, skills, 50 if distance is None else int(distance), bool(params.get("is_remote"))


def hybrid(db: Session, job: Job, search_run_id: int | None = None,
           rules: domain_match.Rules | None = None) -> tuple[Verdict | None, tuple[str, ...]]:
    """DOMAIN_CLASSIFIER_MODE=on: a place miss still hides (a fact); a role or skill miss is only a doubt for the
    classifier, because boards and searches name the same work in many ways ("Artificial Intelligence Engineer" on a
    search for "AI Engineer", "LLMs" for `llm`). Returns (the hiding verdict or None, the doubts)."""
    scope = _scope_of(db, job, search_run_id)
    if scope is None:
        return None, ()
    roles, places, skills, distance, remote_only = scope
    miss = assess(job, roles, [], skills)  # roles and skills only: no places, not remote-only
    place = _place_miss(job, places, distance, remote_only)
    if miss.match:
        return place, ()
    title, text = getattr(job, "title", "") or "", getattr(job, "description", "") or ""
    # A literal miss is no doubt at all when the job names the same role or skill another way, or another of the person's.
    if miss.code == ROLE:
        named = any(domain_match.names_role(title, role) for role in roles) or \
            (rules is not None and domain_match.own_title(title, rules))
    else:
        named = bool(domain_match.skills_found(f"{title}\n{text}", [*skills, *(rules.skills if rules else [])]))
    return place, (() if named else (miss.code,))


def hidden_by_scope(job: Job) -> bool:
    return job.domain_match == domain_match.NOT_MATCHING and (job.domain_reason or "").startswith(SCOPE_REASONS)


def screen(db: Session, job: Job, search_run_id: int | None = None, *, actor: str = "discovery") -> bool:
    """Hide a job that its own search did not ask for. True when it may carry on."""
    if job.domain_match == domain_match.KEPT:
        return True
    from app.services import domain_classifier

    if domain_classifier.mode() == domain_classifier.ON:
        verdict = hybrid(db, job, search_run_id)[0]
    else:
        verdict = verdict_for(db, job, search_run_id)
    if verdict is None or verdict.match:
        return True
    domain_match.hide(db, job, verdict, actor=actor)
    return False
