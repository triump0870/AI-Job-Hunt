"""Ephemeral application sessions: a fresh anonymous context per step, on the shared Chromium process.

The context comes from `BrowserService.new_context()` exactly as JD recovery's do (no cookies, storage, credentials,
downloads; the same SSRF routing, redirect hops included), and it is never handed to JD recovery or reused: it is closed when the step ends.
On top, every request that is not a GET is aborted, at the page and at its context, so nothing a page does — a script,
a stray Enter, a form, a service worker it registers — can post an application. (`page.route` alone does not see
requests a service worker makes or answers; the shared context allows service workers for JD recovery, and in
Chromium `context.route` sees them.) Page loads are recorded in Monitoring → External APIs (provider WEB, service
application_browser) like every other third-party call.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from app.infrastructure.application_browser.service import (
    ApplicationPage,
    ApplicationSessionManager,
    PageLoad,
)
from app.infrastructure.browser.service import (
    BLOCKED_URL,
    TIMEOUT,
    BrowserService,
    NavigationFailed,
)

log = logging.getLogger("jobhunt.application_browser")
FIELD_TIMEOUT_MS = 5000


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class _Page(ApplicationPage):
    def __init__(self, page: Any, *, allow_private: bool, user_id: int, job_id: int | None, ats: str,
                 record_telemetry: bool, context: Any = None):
        self._page = page
        self._context = context
        self._allow_private = allow_private
        self._user_id, self._job_id, self._ats = user_id, job_id, ats
        self._record_telemetry = record_telemetry
        self._blocked = 0

    async def _guard(self, route: Any) -> None:
        """Abort anything that is not a GET (a form post, an XHR submit, a beacon); let the rest through to the
        context's own routing (the SSRF checks every browser page gets). Installed on the context too, before the
        page loads, so a service worker's requests (its own, or a page's it answers) are aborted the same way."""
        try:
            if route.request.method.upper() != "GET":
                self._blocked += 1
                await route.abort("blockedbyclient")
                return
            await route.fallback()
        except Exception:  # noqa: BLE001  the page closing mid-request is normal
            log.debug("application page routing ended early")

    async def install(self) -> None:
        if self._context is not None:
            await self._context.route("**/*", self._guard)  # last registered runs first: before the SSRF route
        await self._page.route("**/*", self._guard)

    @property
    def blocked_requests(self) -> int:
        return self._blocked

    async def goto(self, url: str) -> PageLoad:
        from app.services.url_safety import UnsafeURL, check_public_url

        try:
            await asyncio.to_thread(check_public_url, url, allow_private=self._allow_private)
        except UnsafeURL as exc:
            raise NavigationFailed(BLOCKED_URL, str(exc)) from exc
        started, clock, error, response = _utcnow(), time.monotonic(), None, None
        try:
            response = await self._page.goto(url, wait_until="domcontentloaded")
            with contextlib.suppress(Exception):
                await self._page.wait_for_load_state("load", timeout=5000)
        except Exception as exc:  # noqa: BLE001
            error = exc
        load = PageLoad(url=url, final_url=self._page.url, status=response.status if response is not None else None,
                        title=await self._page.title() if error is None else "", blocked_requests=self._blocked)
        await self._record(url, started, clock, load, error)
        if error is not None:
            from app.infrastructure.browser.playwright_service import classify_navigation_error

            category = classify_navigation_error(error)
            why = "did not load in time" if category == TIMEOUT else f"could not be loaded ({category.lower()})"
            raise NavigationFailed(category, f"The application page {why} ({type(error).__name__})")
        return load

    async def _record(self, url: str, started: datetime, clock: float, load: PageLoad, error: Exception | None) -> None:
        if not self._record_telemetry:
            return
        from app.services import external_api

        call = external_api.ExternalCall(provider="WEB", service="application_browser", operation="load_form",
                                         user_id=self._user_id, job_id=self._job_id,
                                         metadata={"ats": self._ats, "final_host": urlparse(load.final_url).hostname})
        kwargs: dict[str, Any] = {"started_at": started, "completed_at": _utcnow(), "method": "GET", "url": url,
                                  "status_code": load.status}
        if error is not None:
            kwargs.update(error=error)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(external_api.record, call, **kwargs)

    async def html(self) -> str:
        return await self._page.content()

    # Each field action waits at most FIELD_TIMEOUT_MS: a field that cannot be acted on (hidden, detached, not
    # editable) is reported quickly and validation decides, instead of one field using up the whole step.
    async def fill(self, locator: str, text: str) -> None:
        await self._page.locator(locator).first.fill(text, timeout=FIELD_TIMEOUT_MS)

    async def select(self, locator: str, value: str) -> None:
        await self._page.locator(locator).first.select_option(value=value, timeout=FIELD_TIMEOUT_MS)

    async def check(self, locator: str) -> None:
        await self._page.locator(locator).first.check(timeout=FIELD_TIMEOUT_MS)

    async def upload(self, locator: str, path: str) -> None:
        await self._page.locator(locator).first.set_input_files(path, timeout=FIELD_TIMEOUT_MS)

    async def value_of(self, locator: str) -> str:
        return await self._page.locator(locator).first.input_value(timeout=FIELD_TIMEOUT_MS)

    async def is_checked(self, locator: str) -> bool:
        return await self._page.locator(locator).first.is_checked(timeout=FIELD_TIMEOUT_MS)


class EphemeralApplicationSessions(ApplicationSessionManager):
    """A fresh anonymous context per step. Nothing persists; a login wall pauses for a person."""

    persistent = False

    def __init__(self, browser: BrowserService, *, record_telemetry: bool = True):
        self._browser = browser
        self._record_telemetry = record_telemetry

    def is_available(self) -> bool:
        return self._browser.is_available()

    def diagnostics(self) -> dict[str, Any]:
        return {"persistent": False, "kind": "ephemeral", "browser": self._browser.diagnostics()}

    @asynccontextmanager
    async def session(self, *, user_id: int, ats: str, job_id: int | None = None) -> AsyncIterator[ApplicationPage]:
        context = await self._browser.new_context()
        page = None
        try:
            page = await self._browser.new_page(context)
            wrapped = _Page(page, allow_private=bool(getattr(self._browser, "allow_private_hosts", False)),
                            user_id=user_id, job_id=job_id, ats=ats, record_telemetry=self._record_telemetry,
                            context=context)
            await wrapped.install()
            yield wrapped
        finally:
            if page is not None:
                await self._browser.close_page(page)
            await self._browser.close_context(context)
