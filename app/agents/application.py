"""The application execution graph: one approved job from its packet to a filled, validated form (Phase 5) and, in
GUARDED_AUTO, through one claim-guarded final click and its confirmation (Phase 6).

    VERIFY_PACKET → ROUTE (backend) → ANALYZE_FORM ─┬→ wait (NEEDS_HUMAN: a person takes over the window)
                                                    └→ RESOLVE_ANSWERS ─┬→ wait (NEEDS_INPUT: a person answers)
                                                                        ├→ FILL (ASSIST) → VALIDATE → READY_TO_SUBMIT
                                                                        ├→ SUBMIT (GUARDED_AUTO) → CONFIRMED or
                                                                        │   SUBMISSION_UNCERTAIN
                                                                        └→ VALIDATE (OBSERVE ends there)

The graph conducts the shared services: `apply_queue.not_ready_reason` and `approval.verify` decide whether the
packet may be used (checked again against the digest captured at enqueue before every browser step), `ats_forms` reads
the form, `answer_resolver` decides each answer's source (never a model), `application_backends` picks the backend
(the Greenhouse adapter on Playwright, or the browser-use sidecar for a form no adapter knows, where
`browser_actions` checks every step the BROWSER_NAVIGATOR proposes), and `submission` owns the final click: the
pre-submit validator, the shared claim, the SubmissionIntent and the confirmation verifier.

Rules a node keeps:
  * Writes happen only while the run holds its thread: `effects.within` locks the thread row and records the step in
    the same transaction as the task's stage. A stopped run raises LeaseLost and writes nothing.
  * Stages move only through `application_execution.set_stage`; the submit stages need the run's intent.
  * A replayed SUBMIT never clicks again: an intent that reached CLICKING is reconciled, never retried.
  * State is ids, enums, counts and hashes only; never page HTML, a form value or an answer.
"""
from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.agents import effects

GRAPH = "application"
GRAPH_VERSION = "v3"
EXPORT_FIELDS = ("job_id", "task_id", "mode", "step", "outcome", "code", "waiting_for", "route", "backend",
                 "field_count", "required_count", "resolved_count", "needs_input_count", "needs_confirmation_count",
                 "filled_count", "missing_required_count", "blocked_requests", "form_fingerprint", "actions",
                 "model_steps")

VERIFY_PACKET = "VERIFY_PACKET"
ROUTE = "ROUTE"
ANALYZE_FORM = "ANALYZE_FORM"
RESOLVE_ANSWERS = "RESOLVE_ANSWERS"
FILL = "FILL"
VALIDATE = "PRE_SUBMIT_VALIDATION"
SUBMIT = "SUBMIT"
NODES = (VERIFY_PACKET, ROUTE, ANALYZE_FORM, RESOLVE_ANSWERS, FILL, VALIDATE, SUBMIT)

# How a run ends (ApplicationExecutionTask.outcome).
READY, OBSERVED, NEEDS_HUMAN, BLOCKED, UNSUPPORTED = "READY_TO_SUBMIT", "OBSERVED", "NEEDS_HUMAN", "BLOCKED", \
    "UNSUPPORTED"
CONFIRMED, SUBMISSION_UNCERTAIN = "CONFIRMED", "SUBMISSION_UNCERTAIN"
INPUT, HUMAN = "INPUT", "HUMAN"


class ApplicationInput(BaseModel):
    """What the worker starts a run with. Built on the server from the task row; never taken from a caller."""

    model_config = ConfigDict(extra="forbid")
    job_id: int
    task_id: int
    mode: Literal["OBSERVE", "ASSIST", "GUARDED_AUTO"] = "OBSERVE"


class ApplicationResume(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["continue"]


class ApplicationState(TypedDict, total=False):
    job_id: int
    task_id: int
    mode: str
    step: str
    outcome: str
    code: str
    waiting_for: str | None
    resume_at: str | None
    route: str | None
    backend: str | None
    actions: int
    model_steps: int
    field_count: int
    required_count: int
    resolved_count: int
    needs_input_count: int
    needs_confirmation_count: int
    filled_count: int
    missing_required_count: int
    blocked_requests: int
    form_fingerprint: str | None


@dataclass(frozen=True)
class ApplicationContext:
    """Per-run context, never checkpointed: the lease, a session factory, the application browser and the bridge
    that runs its coroutines on the event loop the browser lives on."""

    run: effects.RunHandle
    sessions: Callable[[], Session]
    hold: Callable[[str], None] | None = None
    task_id: int | None = None
    browser: Any = None  # ApplicationSessionManager
    run_async: Callable[[Coroutine[Any, Any, Any]], Any] | None = None


def _hold(ctx: ApplicationContext, point: str) -> None:
    if ctx.hold is not None:
        ctx.hold(point)


def _end(step: str, outcome: str, code: str | None = None, **values: Any) -> dict[str, Any]:
    return {"step": step, "outcome": outcome, "code": code or outcome, "waiting_for": None, "resume_at": None,
            **values}


def _session(ctx: ApplicationContext) -> Session:
    db = ctx.sessions()
    db.info["user_id"] = ctx.run.user_id
    return db


def _load(db: Session, state: ApplicationState) -> tuple[Any, Any]:
    from app.models import ApplicationExecutionTask, Job

    task = db.get(ApplicationExecutionTask, state["task_id"], populate_existing=True)
    job = db.get(Job, state["job_id"])
    return task, job


def _record(db: Session, ctx: ApplicationContext, key: str, task: Any, stage: str, **fields: Any) -> bool:
    """Record this step once (lease checked) and write the task's stage and fields with it. Commits."""
    from app.services import application_execution as service

    _, created = effects.within(db, ctx.run, key)
    if created or task.stage != stage:
        service.set_stage(task, stage)
    for name, value in fields.items():
        setattr(task, name, value)
    db.commit()
    return created


def _key(state: ApplicationState, step: str, extra: str = "") -> str:
    return f"{step.lower()}:{state['task_id']}{extra}"


# ───────────── VERIFY_PACKET ─────────────

def _packet_problem(db: Session, task: Any, job: Any) -> tuple[str, str] | None:
    """(code, reason) when this packet may not be used now: not ready (the shared rules), or not the approved packet
    the execution was queued with."""
    from app.services import apply_queue, approval

    if job is None or task is None:
        return "REMOVED", "The job no longer exists"
    reason = apply_queue.not_ready_reason(db, job)
    checked = approval.verify(db, job.application)
    if not checked.ok:
        return "PACKET_CHANGED", checked.reason or "The approved packet changed"
    if (checked.packet.digest if checked.packet else None) != task.packet_digest:
        return "PACKET_CHANGED", "The packet was approved again after this execution was queued; queue it again"
    if reason is not None:
        return "NOT_READY", reason
    return None


def verify_packet(state: ApplicationState, runtime: Runtime[ApplicationContext]) -> dict[str, Any]:
    """Check the approved packet's digest before anything external starts (approval.verify); a changed packet blocks
    the run and needs a new approval."""
    ctx = runtime.context
    with _session(ctx) as db:
        task, job = _load(db, state)
        problem = _packet_problem(db, task, job)
        if problem:
            if task is not None:
                _record(db, ctx, _key(state, VERIFY_PACKET), task, "BLOCKED", result_code=problem[0],
                        last_error=problem[1][:300])
            return _end(VERIFY_PACKET, BLOCKED, problem[0])
        _record(db, ctx, _key(state, VERIFY_PACKET), task, "DISCOVERING")
    return {"step": VERIFY_PACKET}


# ───────────── ROUTE ─────────────

def route(state: ApplicationState, runtime: Runtime[ApplicationContext]) -> dict[str, Any]:
    """Choose the backend (application_backends.choose) and note the route in the account's ATS map."""
    from app.services import application_backends, ats_map

    ctx = runtime.context
    with _session(ctx) as db:
        task, job = _load(db, state)
        if task.backend:
            return {"step": ROUTE, "backend": task.backend}
        choice = application_backends.choose(job)
        if choice.backend is None:
            _record(db, ctx, _key(state, ROUTE), task, "BLOCKED", result_code="UNSUPPORTED_ROUTE",
                    last_error=choice.reason[:300])
            return _end(ROUTE, UNSUPPORTED, "UNSUPPORTED_ROUTE")
        ats_map.observe(db, job, backend=choice.backend)
        _record(db, ctx, _key(state, ROUTE), task, "ROUTED", backend=choice.backend)
    return {"step": ROUTE, "backend": choice.backend}


# ───────────── ANALYZE_FORM ─────────────

async def _read_form(browser: Any, user_id: int, job_id: int, url: str) -> dict[str, Any]:
    from app.services import ats_forms

    async with browser.session(user_id=user_id, ats="unknown", job_id=job_id) as page:
        load = await page.goto(url)
        html = await page.html()
    problem = ats_forms.page_problem(html, status=load.status, title=load.title)
    if problem:
        return {"problem": problem, "final_url": load.final_url}
    adapter = ats_forms.adapter_for(load.final_url, html)
    if adapter is None:
        return {"problem": ats_forms.UNSUPPORTED, "final_url": load.final_url}
    form = adapter.analyze(load.final_url, html)
    if form is None or not form.fields:
        return {"problem": ats_forms.NO_FORM, "final_url": load.final_url, "route": adapter.NAME}
    return {"form": form.model_dump(), "route": adapter.NAME, "final_url": load.final_url}


PAGE_UNAVAILABLE = "PAGE_UNAVAILABLE"  # the application page could not be loaded (NavigationFailed)


def _load_form(ctx: ApplicationContext, user_id: int, job_id: int, url: str) -> dict[str, Any]:
    """Read the form in a private page. A page that could not be loaded is a result, not a crash: a slow or unreachable
    site is said in words (retryable or not), and analyze_form decides."""
    from app.infrastructure.browser.service import NavigationFailed

    try:
        return ctx.run_async(_read_form(ctx.browser, user_id, job_id, url))
    except NavigationFailed as exc:
        return {"problem": PAGE_UNAVAILABLE, "category": exc.category, "retryable": exc.retryable,
                "detail": str(exc)[:300]}


BLOCKER_FOR = {"LOGIN_REQUIRED": "login_required", "OTP_REQUIRED": "login_required", "LOGIN_FAILED": "login_required",
               "ACCOUNT_REQUIRED": "account_required", "CAPTCHA_OR_ANTI_BOT": "captcha",
               "POSTING_CLOSED": "posting_closed"}
TAKEOVER_PROBLEMS = ("LOGIN_REQUIRED", "OTP_REQUIRED", "LOGIN_FAILED", "ACCOUNT_REQUIRED", "CAPTCHA_OR_ANTI_BOT")
LOGIN_NAVIGATOR_PROBLEMS = ("NAVIGATOR_LOGIN", "NAVIGATOR_SIGNUP", "NAVIGATOR_OTP", "NAVIGATOR_CAPTCHA")
FIELDS_NEED_YOU = "FIELDS_NEED_YOU"  # required fields nothing verified could fill: the person fills them in the window


def _takeover_problem(problem: str | None) -> bool:
    return bool(problem) and (problem in TAKEOVER_PROBLEMS or problem == FIELDS_NEED_YOU
                              or problem.startswith("NAVIGATOR_"))


def _headful() -> bool:
    from app.settings import get_settings

    return not get_settings().browser_use_headless


def _facts(job: Any) -> Any:
    """The job's fields a browser step needs, detached from the session (nothing lazy, nothing expired)."""
    from types import SimpleNamespace

    return SimpleNamespace(**{name: getattr(job, name) for name in (
        "id", "user_id", "title", "company", "direct_apply_url", "url", "canonical_apply_url", "source_url",
        "source_board")})


def _packet_dir(job: Any) -> str:
    from pathlib import Path

    from app.settings import get_settings

    folder = Path(get_settings().mcp_packet_dir) / str(job.user_id) / str(job.id)
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder.resolve())


def _navigator(ctx: ApplicationContext, job: Any, attempt: int) -> tuple[Any, Any]:
    from functools import partial

    from app.prompts import build_browser_navigation_prompt
    from app.services import application_backends

    def lease() -> None:
        with _session(ctx) as db:
            effects.held(db, ctx.run)
            db.rollback()

    propose = application_backends.proposer(ctx.sessions, job.user_id, job.id, attempt, lease)
    builder = partial(build_browser_navigation_prompt, job={"title": job.title, "company": job.company})
    return propose, builder


def _bu_session(ctx: ApplicationContext, user_id: int, task_id: int, job_id: int, match: Any,
                upload_dir: str) -> Any:
    """The execution's parked session (a person took it over), or a new one with the saved login."""
    from app.infrastructure.application_browser.browser_use_sidecar import get_sidecars
    from app.services import application_backends

    manager = get_sidecars()
    session = manager.get(user_id, task_id)
    if session is not None:
        ctx.run_async(manager.unpark(session))
        return session
    storage = application_backends.saved_session(ctx.sessions, user_id, match)
    return ctx.run_async(application_backends.start_session(user_id=user_id, task_id=task_id, job_id=job_id,
                                                            match=match, upload_dir=upload_dir,
                                                            storage_state=storage))


def _bu_load(ctx: ApplicationContext, session: Any, user_id: int, match: Any, url: str) -> tuple[str | None, dict]:
    """Load the application page and, at a login wall, try the vault's login once."""
    from app.services import application_backends, ats_forms

    ctx.run_async(session.call("goto", url=url))
    observation = ctx.run_async(session.call("observe", html=True))
    problem = ats_forms.page_problem(observation.get("html") or "", title=observation.get("title"),
                                     captcha=observation.get("captcha"))
    if problem == ats_forms.LOGIN_REQUIRED:
        problem, observation = application_backends.try_login(ctx.sessions, session, ctx.run_async, user_id, match,
                                                              observation)
    return problem, observation


def _planned_on_page(planned: Any, current: Any, observation: dict[str, Any]) -> bool:
    """Every planned field is on this page: in the form as read now (it may have grown a conditional question), or a
    file input the board replaced with the uploaded file's name once it took it (the runner's `attached`)."""
    from app.services.ats_forms import generic

    if current is None:
        return False
    here, attached = {f.id for f in current.fields}, observation.get("attached") or {}
    return all(f.id in here or (f.kind == "file" and generic.key_of(f.locator) in attached) for f in planned.fields)


def _bu_resume(ctx: ApplicationContext, session: Any) -> tuple[str | None, dict]:
    """The window as the person left it: the marks removed and the page observed where it is, never reloaded."""
    from app.services import ats_forms

    ctx.run_async(session.call("unpoint"))
    observation = ctx.run_async(session.call("observe", html=True))
    return ats_forms.page_problem(observation.get("html") or "", title=observation.get("title"),
                                  captcha=observation.get("captcha")), observation


def _park_or_close(ctx: ApplicationContext, session: Any, problem: str | None) -> bool:
    """A problem a person can fix in the window: hand a headful session to them (takeover; the window then works like
    a normal browser). Anything else, or a headless session: close it.

    A window handed over is picked up where the person left it (`resume_in_place`): what they did there — a solved
    CAPTCHA, a sign-in, fields they typed — is never thrown away by reloading the apply URL. The run still observes the
    page afresh and checks everything again; it falls back to the apply URL only when the form is no longer there."""
    from app.infrastructure.application_browser.browser_use_sidecar import get_sidecars
    from app.settings import get_settings

    manager = get_sidecars()
    if _takeover_problem(problem):
        kept = bool(ctx.run_async(manager.park(session, get_settings().application_takeover_ttl_minutes)))
        session.resume_in_place = kept
        return kept
    ctx.run_async(manager.close(session.user_id, session.task_id))
    return False


def _analyze_browser_use(ctx: ApplicationContext, task_id: int, job: Any, url: str, attempt: int) -> dict[str, Any]:
    from app.infrastructure.application_browser.browser_use_sidecar import (
        SidecarRefused,
        SidecarUnavailable,
    )
    from app.services import application_backends, ats_forms, ats_map, browser_actions
    from app.services.ats_forms import generic

    match = ats_map.for_job(job)
    try:
        session = _bu_session(ctx, job.user_id, task_id, job.id, match, _packet_dir(job))
    except SidecarUnavailable as exc:
        return {"problem": "BACKEND_UNAVAILABLE", "detail": str(exc)[:200]}
    problem, keep, steps = None, False, 0
    in_place = bool(getattr(session, "resume_in_place", False))
    session.resume_in_place = False
    try:
        problem, observation = _bu_resume(ctx, session) if in_place else (None, None)
        if not in_place or (not problem and generic.analyze(observation.get("url") or url,
                                                            observation.get("elements") or []) is None):
            problem, observation = _bu_load(ctx, session, job.user_id, match, url)  # nothing to pick up: load it
        if not problem:
            form = generic.analyze(observation.get("url") or url, observation.get("elements") or [])
            if form is None:  # the form may need revealing (an Apply button, a first section)
                propose, builder = _navigator(ctx, job, attempt)
                result = browser_actions.FillResult()
                observation = browser_actions.navigate(
                    session, ctx.run_async, propose, builder, None, {}, {}, result,
                    hosts=set(application_backends.allowed_hosts(match)), max_actions=browser_actions.REVEAL_STEPS,
                    reveal_only=True)
                steps = result.model_steps
                problem = result.code if result.outcome != "FILLED" else None
                form = generic.analyze(observation.get("url") or url, observation.get("elements") or []) \
                    if not problem else None
            if not problem and form is None:
                problem = ats_forms.NO_FORM
        if problem:
            keep = _park_or_close(ctx, session, problem)
            return {"problem": problem, "takeover": keep, "final_url": observation.get("url"), "model_steps": steps}
        # Kept (signed in, the form revealed) for this execution's FILL; closed by the reaper if it never comes.
        from app.infrastructure.application_browser.browser_use_sidecar import get_sidecars
        from app.settings import get_settings

        ctx.run_async(get_sidecars().hold(session, get_settings().application_takeover_ttl_minutes))
        session.resume_in_place = True  # FILL picks this page up as it is (a solved CAPTCHA, the revealed form)
        return {"form": form.model_dump(), "route": generic.NAME, "final_url": observation.get("url"),
                "model_steps": steps}
    except application_backends.NavigatorUnavailable:
        _park_or_close(ctx, session, None)
        return {"problem": "NAVIGATOR_UNAVAILABLE"}
    except (SidecarRefused, SidecarUnavailable) as exc:
        _park_or_close(ctx, session, None)
        return {"problem": "BACKEND_UNAVAILABLE" if isinstance(exc, SidecarUnavailable) else exc.code,
                "detail": str(exc)[:200] or None}


def _browser_use_on() -> bool:
    from app.infrastructure.application_browser.browser_use_sidecar import configured

    return configured()


def _takeover(db: Session, ctx: ApplicationContext, state: ApplicationState, task: Any, job: Any, problem: str,
              resume_at: str, **extra: Any) -> dict[str, Any]:
    """Wait for the person in the kept window; the run resumes at `resume_at` in that same page (see `_park_or_close`)
    and checks everything again before any action."""
    from datetime import UTC, datetime, timedelta

    from app.services import ats_map
    from app.settings import get_settings

    match = ats_map.for_job(job)
    expires = datetime.now(UTC).replace(tzinfo=None) + timedelta(
        minutes=get_settings().application_takeover_ttl_minutes)
    fields = extra.get("fields") or []
    _record(db, ctx, _key(state, resume_at, f":takeover:a{task.attempt_count}:{problem}:{len(fields)}"), task,
            "NEEDS_HUMAN", result_code=problem,
            interrupt={"kind": "NEEDS_HUMAN", "reason": problem, "takeover": True, "resume_at": resume_at,
                       "expires_at": expires.isoformat(), "login_domain": match.login_domain if match else None,
                       **({"fields": fields} if fields else {}),
                       **({"question_ids": extra["question_ids"]} if extra.get("question_ids") else {})})
    login = problem in TAKEOVER_PROBLEMS and problem != "CAPTCHA_OR_ANTI_BOT"
    ats_map.observe(db, job, needs_login=login or None, last_outcome=problem, checked=True)
    user_id, task_id = task.user_id, task.id
    db.commit()
    from app.services import application_execution as service

    if problem in service.CHALLENGE_REASONS:  # once the person answers it, the run continues by itself
        service.watch_challenge(ctx.sessions, user_id, task_id)
    elif problem in service.FIELD_REASONS and fields:  # and once they have filled the fields pointed out
        service.watch_fields(ctx.sessions, user_id, task_id)
    return {"step": resume_at.upper(), "waiting_for": HUMAN, "resume_at": resume_at}


def analyze_form(state: ApplicationState, runtime: Runtime[ApplicationContext]) -> dict[str, Any]:
    """Read the application form in a fresh browser context (ATS adapter or the browser-use runner) into its fields
    and fingerprint; a login wall or challenge waits for the person."""
    from app.services import apply_queue, ats_forms, ats_map

    ctx = runtime.context
    with _session(ctx) as db:
        task, job = _load(db, state)
        if task.form and task.form_fingerprint:
            return {"step": ANALYZE_FORM, "route": task.route, "backend": task.backend,
                    "form_fingerprint": task.form_fingerprint,
                    "field_count": len(task.form.get("fields") or []), "waiting_for": None}
        url = apply_queue.apply_url(job)
        user_id, job_id, backend, task_id, attempt = job.user_id, job.id, task.backend, task.id, task.attempt_count
        job = _facts(job)
    _hold(ctx, "before_browser")
    if backend == "browser_use_webui":
        read = _analyze_browser_use(ctx, task_id, job, url, attempt)
    else:
        read = _load_form(ctx, user_id, job_id, url)
        if read.get("problem") == PAGE_UNAVAILABLE and _browser_use_on():
            # The private page timed out or lost its connection: browser-use renders it in its own browser.
            read = _analyze_browser_use(ctx, task_id, job, url, attempt)
            backend = "browser_use_webui"
        if read.get("problem") == PAGE_UNAVAILABLE:
            if read.get("retryable"):
                raise effects.StepUnavailable(read["category"], read["detail"])  # the worker retries it later
            read = {"problem": f"PAGE_{read['category']}", "detail": read["detail"]}
        dropdowns = bool(read.get("form")) and ats_forms.ApplicationForm.model_validate(read["form"]) \
            .required_dropdowns()
        if (read.get("problem") in (ats_forms.NO_FORM, ats_forms.UNSUPPORTED) or dropdowns) and _browser_use_on():
            # The adapter does not know this markup, or the form has dropdown widgets this page cannot open: the
            # browser-use backend can.
            read = _analyze_browser_use(ctx, task_id, job, url, attempt)
            backend = "browser_use_webui"
        elif dropdowns:
            read = {"problem": ats_forms.DROPDOWN_FIELDS, "route": read.get("route"),
                    "detail": f"{len(dropdowns)} required dropdown question(s) need the browser-use backend or you"}
    _hold(ctx, "after_browser")
    with _session(ctx) as db:
        task, job = _load(db, state)
        problem = read.get("problem")
        if problem and read.get("takeover"):
            return _takeover(db, ctx, state, task, job, problem, "analyze_form")
        if problem == ats_forms.DROPDOWN_FIELDS:
            created = _record(db, ctx, _key(state, ANALYZE_FORM), task, "NEEDS_HUMAN", result_code=problem,
                              route=read.get("route"), last_error=read.get("detail"),
                              interrupt={"kind": "NEEDS_HUMAN", "reason": problem})
            if created and job is not None and job.application is not None and job.application.status == "approved":
                apply_queue.record_blocker(db, job, "form_error", recorded_by="application_worker",
                                           detail=read.get("detail") or problem)
            ats_map.observe(db, job, last_outcome=problem, checked=True)
            db.commit()
            return _end(ANALYZE_FORM, NEEDS_HUMAN, problem)
        if problem in (*ats_forms.HUMAN_BLOCKERS, "LOGIN_FAILED", ats_forms.POSTING_CLOSED):
            stage = "BLOCKED" if problem == ats_forms.POSTING_CLOSED else "NEEDS_HUMAN"
            created = _record(db, ctx, _key(state, ANALYZE_FORM), task, stage, result_code=problem,
                              route=read.get("route"), interrupt={"kind": stage, "reason": problem})
            if created and job is not None and job.application is not None and job.application.status == "approved":
                apply_queue.record_blocker(db, job, BLOCKER_FOR[problem], recorded_by="application_worker",
                                           detail=f"The application worker stopped at the form: {problem}")
            ats_map.observe(db, job, last_outcome=problem, checked=True,
                            needs_login=problem in ("LOGIN_REQUIRED", "LOGIN_FAILED", "ACCOUNT_REQUIRED") or None)
            db.commit()
            return _end(ANALYZE_FORM, NEEDS_HUMAN if stage == "NEEDS_HUMAN" else BLOCKED, problem)
        if problem:
            _record(db, ctx, _key(state, ANALYZE_FORM), task, "BLOCKED", result_code=problem,
                    route=read.get("route"), last_error=(read.get("detail") or None),
                    **({"backend": backend} if backend else {}))
            ats_map.observe(db, job, last_outcome=problem, checked=True)
            db.commit()
            return _end(ANALYZE_FORM, UNSUPPORTED, problem, route=read.get("route"))
        form = ats_forms.ApplicationForm.model_validate(read["form"])
        _record(db, ctx, _key(state, ANALYZE_FORM), task, "FORM_ANALYZING", route=read["route"],
                form=form.model_dump(), form_fingerprint=form.fingerprint, interrupt=None, backend=backend)
        ats_map.observe(db, job, form_fingerprint=form.fingerprint, last_outcome="FORM_READ", checked=True,
                        needs_login=False)
        db.commit()
    return {"step": ANALYZE_FORM, "route": read["route"], "backend": backend, "form_fingerprint": form.fingerprint,
            "field_count": len(form.fields), "required_count": len(form.required_fields()), "waiting_for": None,
            "model_steps": read.get("model_steps") or 0}


# ───────────── RESOLVE_ANSWERS ─────────────

def resolve_answers(state: ApplicationState, runtime: Runtime[ApplicationContext]) -> dict[str, Any]:
    """Find a verified source for every field (answer_resolver.plan): profile, packet files, the same saved
    question; a sensitive answer needs the person's confirmation, a missing required one waits."""
    from app.services import answer_resolver, ats_forms

    ctx = runtime.context
    with _session(ctx) as db:
        task, job = _load(db, state)
        effects.held(db, ctx.run)
        form = ats_forms.ApplicationForm.model_validate(task.form)
        # Each saved question commits, which ends the thread lock taken above: check the lease again before each.
        plan = answer_resolver.plan(db, job, form, confirmed=list(task.confirmed_question_ids or []),
                                    before_write=lambda: effects.held(db, ctx.run))
        task, job = _load(db, state)
        fields = plan["fields"]
        counts = {"resolved_count": sum(1 for f in fields.values() if f["status"] == answer_resolver.RESOLVED),
                  "needs_input_count": len(plan["needs_input"]),
                  "needs_confirmation_count": len(plan["needs_confirmation"])}
        waiting = plan["needs_input"] or plan["needs_confirmation"]
        attempt = f":a{task.attempt_count}:{len(plan['needs_input'])}:{len(plan['needs_confirmation'])}"
        if waiting and _in_window(state, task):
            # The browser fills what is verified, then shows the person the rest in the open window (FILL).
            _record(db, ctx, _key(state, RESOLVE_ANSWERS, attempt), task, "ANSWERING", plan=plan, interrupt=None)
            return {"step": RESOLVE_ANSWERS, "waiting_for": None, "resume_at": None, **counts}
        if waiting:
            _record(db, ctx, _key(state, RESOLVE_ANSWERS, attempt), task, "NEEDS_INPUT", plan=plan,
                    interrupt={"kind": "NEEDS_INPUT", "question_ids": plan["needs_input"],
                               "confirm_question_ids": plan["needs_confirmation"]})
            if state.get("mode") in ("ASSIST", "GUARDED_AUTO") and task.backend == "browser_use_webui":
                _keep_for_input(ctx, task.user_id, task.id)
            return {"step": RESOLVE_ANSWERS, "waiting_for": INPUT, "resume_at": "resolve_answers", **counts}
        _record(db, ctx, _key(state, RESOLVE_ANSWERS, attempt), task, "ANSWERING", plan=plan, interrupt=None)
    return {"step": RESOLVE_ANSWERS, "waiting_for": None, "resume_at": None, **counts}


def _keep_for_input(ctx: ApplicationContext, user_id: int, task_id: int) -> None:
    """While the run waits for an answer in the Knowledge bank, keep the browser-use page it read (signed in, the form
    revealed) for APPLICATION_INPUT_HOLD_MINUTES, so FILL picks it up in place. Nothing can act in it meanwhile; when
    it is gone by then (the hold ran out, a restart), FILL loads the apply URL as before. Never fails the step."""
    from app.infrastructure.application_browser.browser_use_sidecar import get_sidecars
    from app.settings import get_settings

    try:
        manager = get_sidecars()
        session = manager.get(user_id, task_id)
        if session is None:
            return
        ctx.run_async(manager.hold(session, max(1, get_settings().application_input_hold_minutes), extend=True))
        session.resume_in_place = True
    except Exception:  # noqa: BLE001  the page is read again after the answer
        return


def _in_window(state: ApplicationState, task: Any) -> bool:
    """A filling run (ASSIST or auto submit) on the browser-use backend with a visible window: what nothing verified
    answers, the person fills in that window instead of the run stopping first."""
    return state.get("mode") in ("ASSIST", "GUARDED_AUTO") \
        and (task.backend or state.get("backend")) == "browser_use_webui" and _headful()


def _after_resolve(state: ApplicationState) -> str:
    if state.get("outcome"):
        return END
    if state.get("waiting_for"):
        return "wait"
    return {"ASSIST": "fill", "GUARDED_AUTO": "submit"}.get(state.get("mode") or "", "validate")


# ───────────── FILL (ASSIST only) ─────────────

async def _fill_form(browser: Any, user_id: int, job_id: int, url: str, form: Any, values: dict[str, Any],
                     expected: str) -> dict[str, Any]:
    from app.services import ats_forms

    adapter = ats_forms.adapter_named(form.adapter)
    async with browser.session(user_id=user_id, ats=form.adapter, job_id=job_id) as page:
        load = await page.goto(url)
        html = await page.html()
        problem = ats_forms.page_problem(html, status=load.status, title=load.title)
        if problem:
            return {"problem": problem}
        current = adapter.analyze(load.final_url, html)
        if current is None or current.fingerprint != expected:
            return {"problem": "FORM_CHANGED"}
        report = await adapter.fill(page, current, values)
        missing = []
        for field in current.required_fields():
            if field.kind in ("checkbox",):
                ok = await page.is_checked(field.locator)
            elif field.kind == "radio":
                value = values.get(field.id)
                ok = value is not None and await page.is_checked(f'{field.locator}[value="{value}"]')
            else:
                ok = bool((await page.value_of(field.locator)).strip())
            if not ok:
                missing.append(field.id)
        return {"report": report, "missing": missing, "blocked_requests": page.blocked_requests}


def fill(state: ApplicationState, runtime: Runtime[ApplicationContext]) -> dict[str, Any]:
    """Re-check the packet digest, then fill the form from the resolved sources in a page that cannot submit
    (assist)."""
    from app.services import answer_resolver, apply_queue, approval, ats_forms

    ctx = runtime.context
    with _session(ctx) as db:
        task, job = _load(db, state)
        problem = _packet_problem(db, task, job)  # the approved digest, checked again right before the form
        if problem:
            _record(db, ctx, _key(state, FILL, f":a{task.attempt_count}"), task, "BLOCKED", result_code=problem[0],
                    last_error=problem[1][:300])
            return _end(FILL, BLOCKED, problem[0])
        form = ats_forms.ApplicationForm.model_validate(task.form)
        variant = apply_queue.approved_resume(db, job.application)
        files = {f["format"]: f["path"] for f in apply_queue.packet_files(db, job, variant, ["pdf"])}
        digests = {f["format"]: f["sha256"] for f in apply_queue.packet_files(db, job, variant, ["pdf"])}
        letter = approval.packet_content(variant)["cover_letter"]
        if letter:
            from pathlib import Path

            files["cover_letter"] = answer_resolver.write_cover_letter(Path(files["pdf"]).parent, letter)
        values = answer_resolver.values(db, job, form, task.plan or {}, files=files)
        url, user_id, job_id, expected = apply_queue.apply_url(job), job.user_id, job.id, task.form_fingerprint
        from app.services import application_execution as service

        service.attempt_note(db, task, file_digests=digests)
        _record(db, ctx, _key(state, FILL, f":a{task.attempt_count}"), task, "FILLING")
        task, job = _load(db, state)
        backend, task_id, attempt, plan = task.backend, task.id, task.attempt_count, dict(task.plan or {})
        job = _facts(job)
    _hold(ctx, "before_fill")
    if backend == "browser_use_webui":
        result = _fill_browser_use(ctx, task_id, job, url, form, plan, values, expected, attempt, files=files)
    else:
        result = ctx.run_async(_fill_form(ctx.browser, user_id, job_id, url, form, values, expected))
    values.clear()  # the values never leave this step
    with _session(ctx) as db:
        task, job = _load(db, state)
        fields_problem = (result.get("problem") or "") == FIELDS_NEED_YOU or \
            (result.get("problem") or "").startswith("NAVIGATOR_")
        if result.get("problem") and result.get("takeover"):
            # Fields the person fills in the window: on Continue, answers saved in the Apply kit are resolved again
            # first (RESOLVE_ANSWERS), then FILL picks up the page where it is, without reloading it.
            return _takeover(db, ctx, state, task, job, result["problem"],
                             "resolve_answers" if fields_problem else "fill", fields=result.get("fields"),
                             question_ids=result.get("question_ids"))
        if result.get("problem"):
            code = result["problem"]
            stage = "NEEDS_HUMAN" if code in (*ats_forms.HUMAN_BLOCKERS, "LOGIN_FAILED") or fields_problem \
                else "BLOCKED"
            _record(db, ctx, _key(state, FILL, f":a{task.attempt_count}:done"), task, stage, result_code=code,
                    form=None if code == "FORM_CHANGED" else task.form,
                    form_fingerprint=None if code == "FORM_CHANGED" else task.form_fingerprint,
                    **({"interrupt": {"kind": "NEEDS_HUMAN", "reason": code, "fields": result["fields"]}}
                       if result.get("fields") else {}))
            return _end(FILL, NEEDS_HUMAN if stage == "NEEDS_HUMAN" else BLOCKED, code)
        filled = sum(1 for v in result["report"].values() if v == "filled")
        plan = dict(task.plan or {})
        if result.get("person_filled"):
            # Filled by the person in the window: accepted for ASSIST (never for guarded submission); what they typed
            # is not read or stored here.
            entries = dict(plan.get("fields") or {})
            for fid in result["person_filled"]:
                entries[fid] = {**(entries.get(fid) or {}), "status": "PERSON", "source": "person_in_window"}
            plan["fields"] = entries
        _record(db, ctx, _key(state, FILL, f":a{task.attempt_count}:done"), task, "PRE_SUBMIT_VALIDATION",
                plan=plan, interrupt=None)
    return {"step": FILL, "filled_count": filled, "missing_required_count": len(result["missing"]),
            "blocked_requests": result["blocked_requests"], "actions": result.get("actions") or 0,
            "model_steps": result.get("model_steps") or 0}


def _plan_grown(ctx: ApplicationContext, task_id: int, job_id: int, planned: Any, current: Any, plan: dict[str, Any],
                values: dict[str, Any], files: dict[str, str] | None, attached: dict[str, str] | None = None) -> None:
    """Questions the page shows now that the form read at ANALYZE_FORM did not have (the page grew in the person's
    window, or the next section appeared): resolved exactly as RESOLVE_ANSWERS resolves the planned ones — a verified
    saved answer is used, an unanswered required question is saved to the Knowledge bank for the person. Their entries
    join `plan` (and the task's stored plan, with the fields as `extra_fields`) and their values join `values`.
    `task.form` and its fingerprint stay as planned, so a fresh load still matches; this runs on every in-place pass."""
    from app.models import ApplicationExecutionTask, Job
    from app.services import answer_resolver
    from app.services.ats_forms import ApplicationForm, FormField, generic

    known = {f.id for f in planned.fields}
    extra = [f for f in current.fields if f.id not in known]
    # A grown file input the board replaced with the uploaded file's name is no longer in the form as read now: keep
    # its stored entry while the runner still reports the upload, so the pre-submit checks see it attached.
    here = {f.id for f in extra}
    extra += [f for f in (FormField.model_validate(d) for d in plan.get("extra_fields") or [])
              if f.kind == "file" and f.id not in here and f.id not in known
              and generic.key_of(f.locator) in (attached or {})]
    if not extra:
        if plan.pop("extra_fields", None) is not None:  # an earlier pass's questions are no longer on the page
            with _session(ctx) as db:
                effects.held(db, ctx.run)
                task = db.get(ApplicationExecutionTask, task_id, populate_existing=True)
                task.plan = {k: v for k, v in (task.plan or {}).items() if k != "extra_fields"}
                db.commit()
        return
    grown = ApplicationForm(adapter=planned.adapter, host=planned.host, fields=extra, submit=planned.submit)
    with _session(ctx) as db:
        effects.held(db, ctx.run)
        task = db.get(ApplicationExecutionTask, task_id, populate_existing=True)
        job = db.get(Job, job_id)
        found = answer_resolver.plan(db, job, grown, confirmed=list(task.confirmed_question_ids or []),
                                     before_write=lambda: effects.held(db, ctx.run))
        values.update(answer_resolver.values(db, job, grown, found, files=files))
        plan["fields"] = {**(plan.get("fields") or {}), **found["fields"]}
        plan["extra_fields"] = [f.model_dump() for f in extra]
        for name in ("needs_input", "needs_confirmation"):
            plan[name] = sorted({*(plan.get(name) or []), *found[name]})
        effects.held(db, ctx.run)
        task = db.get(ApplicationExecutionTask, task_id, populate_existing=True)
        stored = dict(task.plan or {})
        task.plan = {**stored, "fields": {**(stored.get("fields") or {}), **found["fields"]},
                     "extra_fields": plan["extra_fields"], "needs_input": plan["needs_input"],
                     "needs_confirmation": plan["needs_confirmation"]}
        db.commit()


def _fill_browser_use(ctx: ApplicationContext, task_id: int, job: Any, url: str, form: Any, plan: dict[str, Any],
                      values: dict[str, Any], expected: str, attempt: int, *, for_submit: bool = False,
                      files: dict[str, str] | None = None) -> dict[str, Any]:
    """Fill a form no adapter knows through the sidecar: every resolved field into its own element without a model,
    the navigator only for resolved fields that could not be reached, then a read-back.

    Required fields nothing verified fills (and anything the navigator asks a person about) are pointed out in the
    page: outlined, a banner, the window in front. With a visible window it is kept for the person (FIELDS_NEED_YOU),
    who fills them there; the next pass picks the page up where it is, without reloading it, and accepts a required
    field the person filled. In ASSIST a finished form stays open for the person to submit; `for_submit` (auto submit)
    keeps the session for the SUBMIT node instead and returns it with the form as read now."""
    from app.infrastructure.application_browser.browser_use_sidecar import (
        SidecarRefused,
        SidecarUnavailable,
        get_sidecars,
    )
    from app.services import application_backends, ats_map, browser_actions
    from app.services.ats_forms import generic
    from app.settings import get_settings

    match = ats_map.for_job(job)
    try:
        session = _bu_session(ctx, job.user_id, task_id, job.id, match, _packet_dir(job))
    except SidecarUnavailable:
        return {"problem": "BACKEND_UNAVAILABLE"}
    in_place = bool(getattr(session, "resume_in_place", False))
    session.resume_in_place = False
    keep = False
    try:
        if in_place:  # the person worked in this very page (fields, a CAPTCHA, a sign-in): pick it up where it is
            problem, observation = _bu_resume(ctx, session)
            here = None if problem else generic.analyze(observation.get("url") or url, observation.get("elements") or [])
            if not problem and not _planned_on_page(form, here, observation):
                in_place = False  # the form is no longer on this page (a sign-in moved it): the apply URL again
        if not in_place:
            problem, observation = _bu_load(ctx, session, job.user_id, match, url)
        if problem:
            keep = _park_or_close(ctx, session, problem)
            return {"problem": problem, "takeover": keep}
        current = generic.analyze(observation.get("url") or url, observation.get("elements") or [])
        result = browser_actions.FillResult()
        limit = get_settings().application_max_actions
        if in_place and current is not None and current.fingerprint != expected and \
                _planned_on_page(form, current, observation):
            pass  # the page grew (a conditional question appeared): the planned fields are all still there
        elif current is None or current.fingerprint != expected:  # a fresh page: reveal the form again first
            propose, builder = _navigator(ctx, job, attempt)
            observation = browser_actions.navigate(session, ctx.run_async, propose, builder, None, {}, {}, result,
                                                   hosts=set(application_backends.allowed_hosts(match)),
                                                   max_actions=browser_actions.REVEAL_STEPS, reveal_only=True)
            current = generic.analyze(observation.get("url") or url, observation.get("elements") or []) \
                if result.outcome == "FILLED" else None
            if current is None or current.fingerprint != expected:
                keep = _park_or_close(ctx, session, result.code if result.outcome == "NEEDS_HUMAN" else None)
                return {"problem": result.code if result.outcome != "FILLED" else "FORM_CHANGED", "takeover": keep}
        if current is not None:
            _plan_grown(ctx, task_id, job.id, form, current, plan, values, files, observation.get("attached"))
        browser_actions.fill_deterministic(session, ctx.run_async, current, plan, values, observation, result, limit)
        observation = ctx.run_async(session.call("observe", html=True))
        missing, person = browser_actions.read_required(session, ctx.run_async, current, values, observation)
        if any(m in values for m in missing):  # resolved but not reached: the navigator may find the way
            propose, builder = _navigator(ctx, job, attempt)
            observation = browser_actions.navigate(session, ctx.run_async, propose, builder, current, plan, values,
                                                   result, hosts=set(application_backends.allowed_hosts(match)),
                                                   max_actions=limit)
            if result.outcome == "BLOCKED":
                _park_or_close(ctx, session, None)
                return {"problem": result.code or "NAVIGATION_FAILED", "takeover": False}
            missing, person = browser_actions.read_required(session, ctx.run_async, current, values, observation)
        if missing or result.outcome == "NEEDS_HUMAN":
            labels = {f.id: f.label for f in current.fields}
            fields = [{"id": fid, "label": labels.get(fid, fid)[:200]} for fid in missing]
            code = result.code if result.outcome == "NEEDS_HUMAN" else FIELDS_NEED_YOU
            if _headful():
                keys = [e["key"] for f in current.fields if f.id in missing
                        if (e := browser_actions.element_for(f, observation)) is not None]
                keys += [result.ask_element] if result.ask_element and result.ask_element not in keys else []
                count = len(fields)
                then = "press Continue in the Apply kit and Job Hunt OS submits it" if for_submit else \
                    "press Continue in the Apply kit, or submit here yourself"
                message = (f"Job Hunt OS needs you: fill the {count} highlighted field{'s' if count != 1 else ''}. "
                           f"It carries on by itself a few seconds after you finish (or {then})."
                           if count and code == FIELDS_NEED_YOU else
                           f"Job Hunt OS needs you: fill the {count} highlighted field{'s' if count != 1 else ''}, "
                           f"then {then}." if count else
                           f"Job Hunt OS needs you on this page. When you are done, {then}.")
                ctx.run_async(session.call("point", keys=keys, message=message, note="Job Hunt OS needs you here"))
                keep = _park_or_close(ctx, session, code)
            return {"problem": code, "takeover": keep, "fields": fields, "person_filled": person,
                    "question_ids": list(plan.get("needs_input") or [])}
        if for_submit:  # auto submit: the SUBMIT node validates this very page and clicks
            keep = True
            ctx.run_async(get_sidecars().hold(session, get_settings().application_takeover_ttl_minutes))
            return {"session": session, "current": current, "observation": observation, "missing": [],
                    "person_filled": person, "actions": result.actions, "model_steps": result.model_steps}
        if _headful():
            # ASSIST: the filled form stays open for the person to review and submit, like a normal browser.
            keep = bool(ctx.run_async(get_sidecars().park(session, get_settings().application_takeover_ttl_minutes)))
        report = {fid: "filled" for fid in result.filled}
        return {"report": report, "missing": [], "person_filled": person,
                "blocked_requests": observation.get("blocked_requests") or 0, "actions": result.actions,
                "model_steps": result.model_steps, "handed_over": keep}
    except application_backends.NavigatorUnavailable:
        return {"problem": "NAVIGATOR_UNAVAILABLE"}
    except (SidecarRefused, SidecarUnavailable) as exc:
        return {"problem": "BACKEND_UNAVAILABLE" if isinstance(exc, SidecarUnavailable) else exc.code}
    finally:
        if not keep:
            ctx.run_async(get_sidecars().close(job.user_id, task_id))


# ───────────── PRE_SUBMIT_VALIDATION ─────────────

def validate(state: ApplicationState, runtime: Runtime[ApplicationContext]) -> dict[str, Any]:
    """OBSERVE: every required field has a source. ASSIST: and every required field holds a value on the page. The
    run ends here: READY_TO_SUBMIT is a person's cue to submit (through the Apply kit, which takes the claim)."""
    ctx = runtime.context
    with _session(ctx) as db:
        task, job = _load(db, state)
        problem = _packet_problem(db, task, job)
        if problem:
            _record(db, ctx, _key(state, VALIDATE, f":a{task.attempt_count}"), task, "BLOCKED",
                    result_code=problem[0], last_error=problem[1][:300])
            return _end(VALIDATE, BLOCKED, problem[0])
        unresolved = [fid for fid, f in ((task.plan or {}).get("fields") or {}).items()
                      if f.get("required") and f.get("status") not in ("RESOLVED", "PERSON")]
        if unresolved or state.get("missing_required_count"):
            _record(db, ctx, _key(state, VALIDATE, f":a{task.attempt_count}"), task, "BLOCKED",
                    result_code="REQUIRED_FIELDS_MISSING")
            return _end(VALIDATE, BLOCKED, "REQUIRED_FIELDS_MISSING")
        if state.get("mode") != "ASSIST":
            _record(db, ctx, _key(state, VALIDATE, f":a{task.attempt_count}"), task, "PRE_SUBMIT_VALIDATION",
                    result_code=OBSERVED)
            return _end(VALIDATE, OBSERVED)
        _record(db, ctx, _key(state, VALIDATE, f":a{task.attempt_count}"), task, "READY_TO_SUBMIT",
                result_code=READY)
    return _end(VALIDATE, READY)


# ───────────── SUBMIT (GUARDED_AUTO only) ─────────────

async def _guarded_read(page: Any, form: Any, values: dict[str, Any]) -> dict[str, Any]:
    """What the guarded page shows now: its host, any problem, the form's fingerprint, which required fields do not
    hold exactly their resolved value, and the file attached to each file input."""
    from app.services import ats_forms

    html = await page.html()
    facts: dict[str, Any] = {"host": page.host, "problem": ats_forms.page_problem(html)}
    adapter = ats_forms.adapter_named(form.adapter)
    current = adapter.analyze(page.url, html) if adapter is not None and not facts["problem"] else None
    facts["fingerprint"] = current.fingerprint if current is not None else None
    missing, uploaded = [], {}
    for field in form.fields:
        value = values.get(field.id)
        if field.kind == "file":
            uploaded[field.id] = await page.value_of(field.locator) if field.id in values else ""
            ok = not field.required or (isinstance(value, dict) and uploaded[field.id].endswith(
                value["path"].replace("\\", "/").rsplit("/", 1)[-1]))
        elif field.kind == "checkbox":
            ok = not field.required or await page.is_checked(field.locator) == (value is True)
        elif field.kind == "radio":
            ok = not field.required or (value is not None and await page.is_checked(
                f'{field.locator}[value="{value}"]'))
        else:
            ok = not field.required or (value is not None and (await page.value_of(field.locator)).strip()
                                        == str(value).strip())
        if not ok:
            missing.append(field.id)
    facts.update(missing=missing, uploaded=uploaded)
    return facts


async def _guarded_fill(page: Any, url: str, form: Any, values: dict[str, Any], expected: str) -> dict[str, Any]:
    from app.services import ats_forms

    load = await page.goto(url)
    html = await page.html()
    problem = ats_forms.page_problem(html, status=load.status, title=load.title)
    if problem:
        return {"host": page.host, "problem": problem}
    adapter = ats_forms.adapter_named(form.adapter)
    current = adapter.analyze(load.final_url, html) if adapter is not None else None
    if current is None or current.fingerprint != expected:
        return {"host": page.host, "problem": "FORM_CHANGED"}
    await adapter.fill(page, current, values)
    return await _guarded_read(page, current, values)


def _end_guarded(db: Session, ctx: ApplicationContext, state: ApplicationState, task: Any, stage: str, code: str,
                 outcome: str, extra: str = "") -> dict[str, Any]:
    _record(db, ctx, _key(state, SUBMIT, f":a{task.attempt_count}:{code}{extra}"), task, stage, result_code=code)
    return _end(SUBMIT, outcome, code)


def submit(state: ApplicationState, runtime: Runtime[ApplicationContext]) -> dict[str, Any]:
    """Fill, validate, claim, click once, confirm — in one guarded page, with no transaction held over the browser.

    A replay (the worker died after this node began) never clicks again: an intent that reached CLICKING makes the run
    SUBMISSION_UNCERTAIN (or CONFIRMED when it was); an ARMED one is aborted and its claim released first."""
    from pathlib import Path

    from app.infrastructure.application_browser.guarded_submit import (
        SubmitRefused,
        SubmitTicket,
        get_guarded_sessions,
    )
    from app.services import answer_resolver, apply_queue, approval, ats_forms, ats_map, submission
    from app.services import application_execution as service
    from app.services.submission import ConfirmationVerifier, PreSubmitValidator

    ctx = runtime.context
    with _session(ctx) as db:
        task, job = _load(db, state)
        effects.held(db, ctx.run)
        prior = submission.clicked(db, task.id)
        if prior is not None:
            if prior.status == submission.CLICKING:
                submission.uncertain(db, task, prior, "The run restarted after the final click started")
            outcome = CONFIRMED if prior.status == submission.CONFIRMED else SUBMISSION_UNCERTAIN
            return _end(SUBMIT, outcome, outcome)
        for intent in submission.intents(db, task.id):
            if intent.status == submission.ARMED:  # nothing was clicked: give the claim back and start again
                submission.abort(db, intent, job, "The run restarted before the click")
        task, job = _load(db, state)
        problem = _packet_problem(db, task, job)
        if problem:
            return _end_guarded(db, ctx, state, task, "BLOCKED", problem[0], BLOCKED)
        form = ats_forms.ApplicationForm.model_validate(task.form)
        variant = apply_queue.approved_resume(db, job.application)
        packet = apply_queue.packet_files(db, job, variant, ["pdf"])
        files = {f["format"]: f["path"] for f in packet}
        digests = {f["format"]: f["sha256"] for f in packet}
        letter = approval.packet_content(variant)["cover_letter"]
        if letter:
            files["cover_letter"] = answer_resolver.write_cover_letter(Path(files["pdf"]).parent, letter)
            digests["cover_letter"] = submission.file_digest(files["cover_letter"])
        values = answer_resolver.values(db, job, form, task.plan or {}, files=files)
        match = ats_map.for_job(job)
        url, user_id, job_id, task_id = apply_queue.apply_url(job), job.user_id, job.id, task.id
        expected = task.form_fingerprint
        service.attempt_note(db, task, file_digests=digests)
        _record(db, ctx, _key(state, SUBMIT, f":a{task.attempt_count}:fill"), task, "FILLING")
        backend, attempt, plan, facts_of_job = task.backend, task.attempt_count, dict(task.plan or {}), _facts(job)
    _hold(ctx, "before_fill")
    if backend == "browser_use_webui":
        return _submit_browser_use(ctx, state, facts_of_job, url, form, plan, values, files, digests, expected,
                                   task_id, attempt, match)
    page = ctx.run_async(get_guarded_sessions().open(user_id=user_id, job_id=job_id, ats=form.adapter))
    try:
        facts = ctx.run_async(_guarded_fill(page, url, form, values, expected))
        with _session(ctx) as db:
            task, job = _load(db, state)
            checked = PreSubmitValidator.check(db, task, job, form=form, page=facts, files=files,
                                               expected_digests=digests)
            if not checked.ok:
                return _end_guarded(db, ctx, state, task, "NEEDS_HUMAN" if checked.human else "BLOCKED",
                                    checked.code or "VALIDATION_FAILED", NEEDS_HUMAN if checked.human else BLOCKED)
            _record(db, ctx, _key(state, SUBMIT, f":a{task.attempt_count}:validated"), task, "PRE_SUBMIT_VALIDATION")
            effects.held(db, ctx.run)
            armed = submission.arm(db, task, job, route=match.route, domain=match.apply_domain, backend="playwright",
                                   file_digests=digests)
            if armed.intent is None:
                task, _ = _load(db, state)
                return _end_guarded(db, ctx, state, task, "BLOCKED", armed.status, BLOCKED)
            intent_id, claim_id = armed.intent.id, armed.claim.id
        _hold(ctx, "armed")
        again = ctx.run_async(_guarded_read(page, form, values))  # the page, again, immediately before the click
        with _session(ctx) as db:
            task, job = _load(db, state)
            intent = db.get(submission.SubmissionIntent, intent_id)
            claim = db.get(submission.ApplicationClaim, claim_id, populate_existing=True)
            checked = PreSubmitValidator.check(db, task, job, form=form, page=again, files=files,
                                               expected_digests=digests, claim=claim)
            if not checked.ok:
                submission.abort(db, intent, job, f"Revalidation failed: {checked.code}")
                task, _ = _load(db, state)
                return _end_guarded(db, ctx, state, task, "NEEDS_HUMAN" if checked.human else "BLOCKED",
                                    checked.code or "VALIDATION_FAILED", NEEDS_HUMAN if checked.human else BLOCKED,
                                    ":recheck")
            effects.within(db, ctx.run, _key(state, SUBMIT, f":clicking:{intent_id}"))
            clicking = submission.commit_clicking(db, task, intent)
            if not clicking.ok:
                db.rollback()
                intent = db.get(submission.SubmissionIntent, intent_id)
                submission.abort(db, intent, job, f"Not authorized at the click: {clicking.code}")
                task, _ = _load(db, state)
                return _end_guarded(db, ctx, state, task, "NEEDS_HUMAN" if clicking.human else "BLOCKED",
                                    clicking.code or "NOT_AUTHORIZED", NEEDS_HUMAN if clicking.human else BLOCKED,
                                    ":clicking")
        _hold(ctx, "before_click")
        ticket = SubmitTicket(intent_id=intent_id, domain=match.apply_domain, adapter=form.adapter,
                              recheck=lambda: submission.still_clicking(ctx.sessions, user_id, task_id, intent_id))
        try:
            ctx.run_async(page.submit(ticket))
        except SubmitRefused as exc:  # refused before the click: nothing was sent
            with _session(ctx) as db:
                task, job = _load(db, state)
                effects.held(db, ctx.run)  # a stopped run writes nothing (Kill already settled its intent)
                submission.abort(db, db.get(submission.SubmissionIntent, intent_id), job,
                                 f"Refused before the click: {exc.code}", refused_at_click=True)
                task, _ = _load(db, state)
                service.set_stage(task, "BLOCKED")
                task.result_code = exc.code
                db.commit()
            return _end(SUBMIT, BLOCKED, exc.code)
        except Exception as exc:  # noqa: BLE001  the click may have happened: the outcome is unknown
            with _session(ctx) as db:
                task, _ = _load(db, state)
                effects.held(db, ctx.run)
                submission.uncertain(db, task, db.get(submission.SubmissionIntent, intent_id),
                                     f"The browser failed during the final click ({type(exc).__name__})")
            return _end(SUBMIT, SUBMISSION_UNCERTAIN, SUBMISSION_UNCERTAIN)
        _hold(ctx, "after_click")
        evidence = ctx.run_async(page.evidence())
        with _session(ctx) as db:
            task, job = _load(db, state)
            intent = db.get(submission.SubmissionIntent, intent_id)
            effects.within(db, ctx.run, _key(state, SUBMIT, f":clicked:{intent_id}"))
            submission.after_click(db, task, intent)
            db.commit()
            confirmation = ConfirmationVerifier.verify(form.adapter, match.apply_domain, evidence)
            evidence.clear()
            if confirmation.confirmed:
                recorded = submission.confirm(db, task, intent, confirmation)
                outcome = CONFIRMED if recorded == "SUBMITTED" else SUBMISSION_UNCERTAIN
            else:
                submission.uncertain(db, task, intent, confirmation.reason, confirmation.facts)
                outcome = SUBMISSION_UNCERTAIN
            ats_map.observe(db, job, last_outcome=outcome, checked=True)
            db.commit()
        return _end(SUBMIT, outcome, outcome)
    finally:
        values.clear()
        ctx.run_async(page.close())


def _bu_facts(ctx: ApplicationContext, session: Any, planned: Any, values: dict[str, Any],
              extra: list[Any] | None = None) -> dict[str, Any]:
    """What the browser-use page shows now, for the PreSubmitValidator: its host and address, any problem, the form's
    fingerprint (the planned one while every planned field is still there: a conditional question may have grown it),
    the required fields that do not hold their answer (or a person's), the file attached to each file field, and the
    submit control. Never a value."""
    from urllib.parse import urlparse

    from app.services import ats_forms, browser_actions
    from app.services.ats_forms import generic

    observation = ctx.run_async(session.call("observe", html=True))
    url = observation.get("url") or ""
    facts: dict[str, Any] = {"host": (urlparse(url).hostname or "").lower(), "url": url,
                             "problem": ats_forms.page_problem(observation.get("html") or "",
                                                               title=observation.get("title"),
                                                               captcha=observation.get("captcha"))}
    current = generic.analyze(url, observation.get("elements") or []) if not facts["problem"] else None
    if current is None:
        return {**facts, "fingerprint": None, "missing": [], "uploaded": {}, "submit_key": None}
    grown = _planned_on_page(planned, current, observation)
    facts["fingerprint"] = planned.fingerprint if grown else current.fingerprint
    facts["missing"], facts["person"] = browser_actions.read_required(session, ctx.run_async, current, values,
                                                                      observation)
    uploaded = {}
    # A grown page's file inputs too: those on the page now, and the stored ones (`extra`, application._plan_grown)
    # whose input the board replaced with the uploaded file's name (element_for finds them through `attached`).
    for field_ in {f.id: f for f in [*planned.fields, *(extra or []), *current.fields]}.values():
        element = browser_actions.element_for(field_, observation) if field_.kind == "file" else None
        if element is not None:
            read = ctx.run_async(session.call("read", key=element["key"]))
            uploaded[field_.id] = ", ".join(read.get("files") or [])
    facts["uploaded"] = uploaded
    facts["submit_key"] = _submit_control(observation, current)
    return facts


def _submit_control(observation: dict[str, Any], form: Any) -> str | None:
    """The key of the form's own submit control: a submit-like element in the form that holds the most fields,
    preferring the label the form was read with. None when there is none, or no single best one."""
    from collections import Counter

    from app.services.ats_forms import generic

    elements = observation.get("elements") or []
    keys = {generic.key_of(f.locator) for f in form.fields}
    forms = Counter(e.get("form_index") for e in elements if e.get("key") in keys or e.get("group_key") in keys)
    home = forms.most_common(1)[0][0] if forms else None
    candidates = [e for e in elements if e.get("submit_like") and e.get("in_form")
                  and (home is None or e.get("form_index") == home)]
    wanted = " ".join((form.submit.label or "").lower().split())
    exact = [e for e in candidates if " ".join((e.get("label") or "").lower().split()) == wanted] if wanted else []
    chosen = exact or candidates
    return chosen[0]["key"] if len(chosen) == 1 else None


def _submit_browser_use(ctx: ApplicationContext, state: ApplicationState, job: Any, url: str, form: Any,
                        plan: dict[str, Any], values: dict[str, Any], files: dict[str, str], digests: dict[str, str],
                        expected: str, task_id: int, attempt: int, match: Any) -> dict[str, Any]:
    """Auto submit on the browser-use backend: fill the page (fields nothing verified fills go to the person in the
    window, as in ASSIST), then the same order as the Playwright path — validate, claim and intent, the page again,
    validate again, CLICKING, one signed click on the form's own submit control, the site's confirmation. An unknown
    outcome keeps the window open for the person."""
    from app.infrastructure.application_browser.browser_use_sidecar import (
        REFUSED_BEFORE_CLICK,
        SidecarRefused,
        get_sidecars,
    )
    from app.services import application_execution as service
    from app.services import ats_forms, ats_map, submission
    from app.services.submission import ConfirmationVerifier, PreSubmitValidator
    from app.settings import get_settings

    manager = get_sidecars()
    filled = _fill_browser_use(ctx, task_id, job, url, form, plan, values, expected, attempt, for_submit=True,
                              files=files)
    if filled.get("problem"):
        values.clear()
        with _session(ctx) as db:
            task, job_row = _load(db, state)
            code = filled["problem"]
            fields_problem = code == FIELDS_NEED_YOU or code.startswith("NAVIGATOR_")
            if filled.get("takeover"):
                return _takeover(db, ctx, state, task, job_row, code, "resolve_answers" if fields_problem else "submit",
                                 fields=filled.get("fields"), question_ids=filled.get("question_ids"))
            human = code in (*ats_forms.HUMAN_BLOCKERS, "LOGIN_FAILED") or fields_problem
            return _end_guarded(db, ctx, state, task, "NEEDS_HUMAN" if human else "BLOCKED", code,
                                NEEDS_HUMAN if human else BLOCKED)
    session, keep = filled["session"], False
    try:
        if filled.get("person_filled"):
            with _session(ctx) as db:
                task, _ = _load(db, state)
                entries = dict((task.plan or {}).get("fields") or {})
                for fid in filled["person_filled"]:
                    entries[fid] = {**(entries.get(fid) or {}), "status": "PERSON", "source": "person_in_window"}
                _record(db, ctx, _key(state, SUBMIT, f":a{attempt}:person"), task, "FILLING",
                        plan={**(task.plan or {}), "fields": entries})
        extra = [ats_forms.FormField.model_validate(f) for f in plan.get("extra_fields") or []]
        facts = _bu_facts(ctx, session, form, values, extra)
        with _session(ctx) as db:
            task, job_row = _load(db, state)
            checked = PreSubmitValidator.check(db, task, job_row, form=form, page=facts, files=files,
                                               expected_digests=digests)
            if checked.ok and not facts.get("submit_key"):
                checked = submission.Validation(False, "SUBMIT_NOT_FOUND", "No single submit control on the form",
                                                human=True)
            if not checked.ok and checked.human and _headful():
                # Something only the person can settle (an attestation, a sensitive answer, no clear Submit): the
                # window is theirs, with the reason on the page; Continue checks everything again and submits.
                ctx.run_async(session.call("point", keys=[], note="", message=(
                    f"Job Hunt OS needs you: {checked.reason}. Then press Continue in the Apply kit, "
                    "or submit here yourself.")[:300]))
                keep = _park_or_close(ctx, session, FIELDS_NEED_YOU)
                if keep:
                    return _takeover(db, ctx, state, task, job_row, checked.code or FIELDS_NEED_YOU,
                                     "resolve_answers")
            if not checked.ok:
                return _end_guarded(db, ctx, state, task, "NEEDS_HUMAN" if checked.human else "BLOCKED",
                                    checked.code or "VALIDATION_FAILED", NEEDS_HUMAN if checked.human else BLOCKED)
            _record(db, ctx, _key(state, SUBMIT, f":a{attempt}:validated"), task, "PRE_SUBMIT_VALIDATION")
            effects.held(db, ctx.run)
            armed = submission.arm(db, task, job_row, route=match.route, domain=facts["host"],
                                   backend="browser_use_webui", file_digests=digests)
            if armed.intent is None:
                task, _ = _load(db, state)
                return _end_guarded(db, ctx, state, task, "BLOCKED", armed.status, BLOCKED)
            intent_id, claim_id = armed.intent.id, armed.claim.id
        _hold(ctx, "armed")
        again = _bu_facts(ctx, session, form, values, extra)  # the page, again, immediately before the click
        with _session(ctx) as db:
            task, job_row = _load(db, state)
            intent = db.get(submission.SubmissionIntent, intent_id)
            claim = db.get(submission.ApplicationClaim, claim_id, populate_existing=True)
            checked = PreSubmitValidator.check(db, task, job_row, form=form, page=again, files=files,
                                               expected_digests=digests, claim=claim)
            if checked.ok and (again.get("submit_key") != facts.get("submit_key") or again["host"] != facts["host"]):
                checked = submission.Validation(False, "FORM_CHANGED", "The submit control moved since the check")
            if not checked.ok:
                submission.abort(db, intent, job_row, f"Revalidation failed: {checked.code}")
                task, _ = _load(db, state)
                return _end_guarded(db, ctx, state, task, "NEEDS_HUMAN" if checked.human else "BLOCKED",
                                    checked.code or "VALIDATION_FAILED", NEEDS_HUMAN if checked.human else BLOCKED,
                                    ":recheck")
            effects.within(db, ctx.run, _key(state, SUBMIT, f":clicking:{intent_id}"))
            clicking = submission.commit_clicking(db, task, intent)
            if not clicking.ok:
                db.rollback()
                intent = db.get(submission.SubmissionIntent, intent_id)
                submission.abort(db, intent, job_row, f"Not authorized at the click: {clicking.code}")
                task, _ = _load(db, state)
                return _end_guarded(db, ctx, state, task, "NEEDS_HUMAN" if clicking.human else "BLOCKED",
                                    clicking.code or "NOT_AUTHORIZED", NEEDS_HUMAN if clicking.human else BLOCKED,
                                    ":clicking")
        _hold(ctx, "before_click")
        user_id = job.user_id
        if not submission.still_clicking(ctx.sessions, user_id, task_id, intent_id):
            with _session(ctx) as db:
                task, job_row = _load(db, state)
                effects.held(db, ctx.run)
                submission.abort(db, db.get(submission.SubmissionIntent, intent_id), job_row,
                                 "Refused before the click: NOT_AUTHORIZED", refused_at_click=True)
                task, _ = _load(db, state)
                service.set_stage(task, "BLOCKED")
                task.result_code = "NOT_AUTHORIZED"
                db.commit()
            return _end(SUBMIT, BLOCKED, "NOT_AUTHORIZED")
        token = submission.signed_submit(session, again["submit_key"], again["host"])
        try:
            ctx.run_async(session.call("submit", key=again["submit_key"], token=token))
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, SidecarRefused) and exc.code in REFUSED_BEFORE_CLICK:  # nothing was sent
                with _session(ctx) as db:
                    task, job_row = _load(db, state)
                    effects.held(db, ctx.run)
                    submission.abort(db, db.get(submission.SubmissionIntent, intent_id), job_row,
                                     f"Refused before the click: {exc.code}", refused_at_click=True)
                    task, _ = _load(db, state)
                    service.set_stage(task, "BLOCKED")
                    task.result_code = exc.code
                    db.commit()
                return _end(SUBMIT, BLOCKED, exc.code)
            # Anything else, a refusal with another code too: the click may have happened, the outcome is unknown.
            keep = _park_or_close(ctx, session, FIELDS_NEED_YOU)
            with _session(ctx) as db:
                task, _ = _load(db, state)
                effects.held(db, ctx.run)
                submission.uncertain(db, task, db.get(submission.SubmissionIntent, intent_id),
                                     f"The browser failed during the final click ({type(exc).__name__})")
            return _end(SUBMIT, SUBMISSION_UNCERTAIN, SUBMISSION_UNCERTAIN)
        _hold(ctx, "after_click")
        evidence = ctx.run_async(session.call("evidence"))
        with _session(ctx) as db:
            task, job_row = _load(db, state)
            intent = db.get(submission.SubmissionIntent, intent_id)
            effects.within(db, ctx.run, _key(state, SUBMIT, f":clicked:{intent_id}"))
            submission.after_click(db, task, intent)
            db.commit()
            confirmation = ConfirmationVerifier.verify(form.adapter, again["host"], evidence)
            email_code = ats_forms.asks_email_code(evidence.get("text"))
            evidence.clear()
            if confirmation.confirmed:
                recorded = submission.confirm(db, task, intent, confirmation)
                outcome = CONFIRMED if recorded == "SUBMITTED" else SUBMISSION_UNCERTAIN
            elif email_code:
                # The site emailed a code to finish: the window stays with the person, who enters it (the Apply kit
                # can look it up in Gmail); the window watcher records the submission once the site confirms it.
                submission.uncertain(db, task, intent, "The site emailed you a code to finish the application",
                                     {**(confirmation.facts or {}), "email_code": True})
                outcome = SUBMISSION_UNCERTAIN
            else:
                submission.uncertain(db, task, intent, confirmation.reason, confirmation.facts)
                outcome = SUBMISSION_UNCERTAIN
            ats_map.observe(db, job_row, last_outcome=outcome, checked=True)
            db.commit()
        if outcome != CONFIRMED:  # the person looks at the page to say what happened
            keep = bool(ctx.run_async(manager.park(session, get_settings().application_takeover_ttl_minutes)))
        return _end(SUBMIT, outcome, outcome)
    finally:
        values.clear()
        if not keep:
            ctx.run_async(manager.close(job.user_id, task_id))


# ───────────── waiting ─────────────

def wait(state: ApplicationState) -> dict[str, Any]:
    """Settle the run until a person answers, confirms, or finishes what the window needed; the worker resumes it."""
    answer = interrupt({"wait": state.get("waiting_for"), "step": state.get("step"), "job_id": state.get("job_id")})
    ApplicationResume.model_validate(answer if isinstance(answer, dict) else {"action": answer})
    return {"waiting_for": None}


def _next(after: str) -> Callable[[ApplicationState], str]:
    def route_next(state: ApplicationState) -> str:
        if state.get("outcome"):
            return END
        return "wait" if state.get("waiting_for") else after
    return route_next


def _after_wait(state: ApplicationState) -> str:
    return state.get("resume_at") or "resolve_answers"


def build(checkpointer: Any) -> Any:
    graph = StateGraph(ApplicationState, context_schema=ApplicationContext)
    graph.add_node("verify_packet", verify_packet)
    graph.add_node("route", route)
    graph.add_node("analyze_form", analyze_form)
    graph.add_node("resolve_answers", resolve_answers)
    graph.add_node("fill", fill)
    graph.add_node("validate", validate)
    graph.add_node("submit", submit)
    graph.add_node("wait", wait)
    graph.add_edge(START, "verify_packet")
    graph.add_conditional_edges("verify_packet", _next("route"), ["route", END])
    graph.add_conditional_edges("route", _next("analyze_form"), ["analyze_form", END])
    graph.add_conditional_edges("analyze_form", _next("resolve_answers"), ["resolve_answers", "wait", END])
    graph.add_conditional_edges("resolve_answers", _after_resolve, ["wait", "fill", "validate", "submit", END])
    graph.add_conditional_edges("wait", _after_wait, ["resolve_answers", "analyze_form", "fill", "submit", "validate"])
    graph.add_conditional_edges("fill", _next("validate"), ["validate", "wait", END])
    graph.add_edge("validate", END)
    # SUBMIT can hand the window to the person (a CAPTCHA, a field nothing verified fills): it waits like any other
    # step, on the same thread, and resumes where `resume_at` says, never from the start.
    graph.add_conditional_edges("submit", _next(END), ["wait", END])
    return graph.compile(checkpointer=checkpointer, name=GRAPH)


def summary(state: dict[str, Any]) -> dict[str, Any]:
    return {k: state[k] for k in EXPORT_FIELDS if k in state and state[k] is not None}
