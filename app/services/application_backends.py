"""Which browser backend an application execution uses, and the browser-use backend's session steps (Phase 6).

    choose(job)            ats_api → playwright → browser_use_webui, in that order:
                             * ats_api: an authorized ATS submission API. None exists that a candidate may use, so
                               the registry is empty and this backend is never chosen (it is reported NOT_AVAILABLE)
                             * playwright: the route has a deterministic form adapter (Greenhouse); also every form
                               when the browser-use backend is off (exactly the Phase 5 behaviour)
                             * browser_use_webui: a dynamic form no adapter knows, when BROWSER_USE_WEBUI_ENABLED
                           In-board applications behind a board's own login (LinkedIn, Indeed) are never automated.
    start_session(…)       a sidecar session limited to the route's hosts, started with the account's saved session
                           for the login domain when sessions are on (app.services.ats_auth)
    try_login(…)           one login attempt from the vault, on an allowlisted login domain only; an OTP, a CAPTCHA
                           or a failed login waits for the person
    proposer(…)            the BROWSER_NAVIGATOR worker through the LLM gateway (LLMClient), lease-checked

Nothing here clicks a final Submit: a LOGIN token authorizes the login form's own control and nothing else.
"""
from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.services import ats_forms, ats_map
from app.settings import get_settings

ATS_API, PLAYWRIGHT, BROWSER_USE = ats_map.ATS_API, ats_map.PLAYWRIGHT, ats_map.BROWSER_USE
BACKENDS = (ATS_API, PLAYWRIGHT, BROWSER_USE)
# Authorized candidate-side ATS submission APIs Job Hunt OS may call. Greenhouse's Job Board API accepts applications
# only with the employer's own API key, and Lever's posting API likewise; none can be used on a candidate's behalf.
ATS_API_ADAPTERS: dict[str, Any] = {}
Run = Callable[[Coroutine[Any, Any, Any]], Any]


class NavigatorUnavailable(RuntimeError):
    """No model is configured for the account, or the gateway failed: the person finishes the form."""


@dataclass(frozen=True)
class Choice:
    backend: str | None
    reason: str
    match: ats_map.AtsMatch | None


def choose(job: Any) -> Choice:
    match = ats_map.for_job(job)
    if match is not None and match.auth_required == ats_map.UNSUPPORTED:
        return Choice(None, match.note or f"{match.label} applications are not automated", match)
    if match is not None and match.ats in ATS_API_ADAPTERS:
        return Choice(ATS_API, "an authorized ATS API", match)
    if match is not None and match.adapter:
        return Choice(PLAYWRIGHT, f"the {match.label} form adapter", match)
    if get_settings().browser_use_webui_enabled:
        return Choice(BROWSER_USE, "a dynamic form (browser-use backend)", match)
    return Choice(PLAYWRIGHT, "no adapter for this site; the browser-use backend is off", match)


def status() -> dict[str, Any]:
    """The backends for Settings → Application routes."""
    from app.infrastructure.application_browser import browser_use_sidecar

    settings = get_settings()
    return {"ats_api": {"available": bool(ATS_API_ADAPTERS), "adapters": sorted(ATS_API_ADAPTERS),
                        "note": "No ATS offers a candidate-side submission API Job Hunt OS may use"},
            "playwright": {"available": settings.playwright_enabled,
                           "adapters": [a.NAME for a in ats_forms.adapters()]},
            "browser_use_webui": {"enabled": settings.browser_use_webui_enabled,
                                  "configured": browser_use_sidecar.configured(),
                                  "headless": settings.browser_use_headless}}


def allowed_hosts(match: ats_map.AtsMatch | None, final_url: str | None = None) -> list[str]:
    from urllib.parse import urlparse

    hosts = {h for h in (match.apply_domain if match else None, match.login_domain if match else None) if h}
    final = (urlparse(final_url or "").hostname or "").lower()
    if final and match is not None and ats_map.detect(final_url) and ats_map.detect(final_url).ats == match.ats:
        hosts.add(final)  # a redirect inside the same ATS (an embed to its hosted board)
    return sorted(hosts)


def redirect_domains(match: ats_map.AtsMatch | None) -> list[str]:
    """Where a server redirect may take the page besides `allowed_hosts`: the ATS's own domains (one board moving a form
    between its hosts, which `allowed_hosts` also accepts as a final URL), or an employer's own site. A country's
    second-level domain (acme.co.uk) is not a site: only hosts under the apply domain itself count there."""
    if match is None:
        return []
    hosts = ats_map.ats_named(match.ats).hosts
    if hosts:
        return sorted(hosts)
    labels = (match.apply_domain or "").lower().strip(".").split(".")
    parent = labels[1:] if len(labels) > 2 else labels
    if len(parent) == 2 and len(parent[1]) == 2 and len(parent[0]) <= 3:
        parent = labels
    return [".".join(parent)] if any(parent) else []


async def start_session(*, user_id: int, task_id: int, job_id: int, match: ats_map.AtsMatch | None, upload_dir: str,
                        storage_state: dict[str, Any] | None) -> Any:
    from app.infrastructure.application_browser.browser_use_sidecar import get_sidecars

    allow_private = get_settings().application_allow_private_hosts
    return await get_sidecars().start(user_id=user_id, task_id=task_id, job_id=job_id,
                                      allowed_hosts=allowed_hosts(match), redirect_domains=redirect_domains(match),
                                      upload_dir=upload_dir,
                                      storage_state=storage_state, allow_private=allow_private)


def saved_session(sessions: Callable[[], Session], user_id: int, match: ats_map.AtsMatch | None) -> dict[str, Any] | None:
    from app.services import ats_auth

    if match is None or not match.login_domain:
        return None
    with sessions() as db:
        db.info["user_id"] = user_id
        state = ats_auth.session_state(db, user_id, match.login_domain)
        db.commit()
        return state


def _login_parts(observation: dict[str, Any]) -> tuple[dict | None, dict | None, dict | None]:
    """The login form's username field, password field and button — all three from the form that holds the password
    field, never another form on the page (an application form's Submit must never get a LOGIN signature). A password
    field outside any form gives nothing: a person signs in."""
    elements = observation.get("elements") or []
    password = next((e for e in elements if e.get("type") == "password"), None)
    form = password.get("form_index") if password is not None else None
    if form is None or form < 0:
        return None, password, None
    own = [e for e in elements if e.get("form_index") == form]
    username = next((e for e in own if e.get("tag") == "input" and e.get("type") in ("email", "text") and (
        e.get("autocomplete") in ("username", "email") or
        any(w in (e.get("label") or "").lower() for w in ("email", "user")))), None)
    submit = next((e for e in own if e.get("submit_like")), None)
    return username, password, submit


def try_login(sessions: Callable[[], Session], session: Any, run: Run, user_id: int, match: ats_map.AtsMatch | None,
              observation: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """One login from the vault on the route's login domain. Returns (problem or None, the observation after)."""
    from app.infrastructure.application_browser.browser_use_sidecar import (
        SidecarRefused,
        sign_action,
    )
    from app.services import ats_auth, redaction

    if match is None or not match.login_domain:
        return ats_forms.LOGIN_REQUIRED, observation
    with sessions() as db:
        db.info["user_id"] = user_id
        secret = ats_auth.login_secret(db, user_id, match.login_domain)
        db.commit()
    if secret is None:
        return ats_forms.LOGIN_REQUIRED, observation
    username, password, submit = _login_parts(observation)
    if username is None or password is None or submit is None:
        return ats_forms.LOGIN_REQUIRED, observation
    with redaction.holding(secret.password, secret.username):
        try:
            run(session.call("login_fill", key=username["key"], secret=secret.username, domain=match.login_domain))
            run(session.call("login_fill", key=password["key"], secret=secret.password, domain=match.login_domain))
            token = sign_action(session, "LOGIN", submit["key"], match.login_domain)
            run(session.call("login_submit", key=submit["key"], token=token))
        except SidecarRefused:
            return "LOGIN_FAILED", observation
    after = run(session.call("observe", html=True))
    problem = ats_forms.page_problem(after.get("html") or "", title=after.get("title"))
    if problem == ats_forms.LOGIN_REQUIRED:
        return "LOGIN_FAILED", after
    return problem, after


def proposer(sessions: Callable[[], Session], user_id: int, job_id: int, attempt: int,
             before: Callable[[], None]) -> Callable[[Any], Any]:
    """The navigator's next step for one prompt, through the gateway. `before` checks the run's lease."""
    from app.prompts.contracts import BrowserActionProposal
    from app.services.llm import LLMCall, LLMClient, LLMError
    from app.services.user_settings import ai_ready

    def propose(prompt: Any) -> BrowserActionProposal:
        before()
        with sessions() as db:
            db.info["user_id"] = user_id
            ready, why = ai_ready(db, user_id)
            if not ready:
                raise NavigatorUnavailable(why or "No model is configured")
            try:
                result = LLMClient.for_user(db, user_id).generate_prompt(prompt, LLMCall(
                    workflow_type="job", entity_id=job_id, attempt=attempt,
                    validate=BrowserActionProposal.model_validate))
            except LLMError as exc:
                raise NavigatorUnavailable(type(exc).__name__) from exc
        return result.validated
    return propose
