"""The shared headless browser: one interface every browser-using feature goes through.

    feature (JD recovery today; ATS apply and confirmation capture later)
      → get_browser_service()                       ← one per process, started lazily
      → one Chromium process
      → an isolated context + page per task         ← no cookies, storage or downloads survive it
      → closed again, even when the task fails

Features never import Playwright or launch Chromium themselves. The service is optional: when Playwright or Chromium
is missing, `is_available()` is False, `diagnostics()` says why in safe terms, and calls raise BrowserUnavailable —
so a caller can carry on without a browser (HTTP job-description recovery does).

Every page load is recorded like any other third-party call (provider WEB, service playwright), so browser work shows
up in Monitoring → External APIs beside the HTTP fetches.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

# Why the browser cannot be used at all (BrowserUnavailable.code).
PLAYWRIGHT_DISABLED = "PLAYWRIGHT_DISABLED"
PLAYWRIGHT_NOT_INSTALLED = "PLAYWRIGHT_NOT_INSTALLED"
CHROMIUM_MISSING = "CHROMIUM_MISSING"
BROWSER_START_FAILED = "BROWSER_START_FAILED"
UNSUPPORTED_BROWSER = "UNSUPPORTED_BROWSER"

# Why one page load failed (NavigationFailed.category).
TIMEOUT, DNS, CONNECTION, SSL = "TIMEOUT", "DNS", "CONNECTION", "SSL"
REDIRECT_LOOP, PAGE_CRASHED, BROWSER_CRASHED = "REDIRECT_LOOP", "PAGE_CRASHED", "BROWSER_CRASHED"
BLOCKED_URL, OTHER = "BLOCKED_URL", "OTHER"
RETRYABLE = frozenset({TIMEOUT, DNS, CONNECTION, PAGE_CRASHED, BROWSER_CRASHED, OTHER})


class BrowserUnavailable(RuntimeError):
    """The browser cannot be used. `code` is one of the constants above; the message is safe to display."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class NavigationFailed(RuntimeError):
    """One page could not be loaded. `category` says why; `retryable` whether trying later may help."""

    def __init__(self, category: str, message: str):
        self.category = category
        self.retryable = category in RETRYABLE
        super().__init__(message)


@dataclass
class RenderedPage:
    url: str
    final_url: str
    status: int | None
    html: str
    title: str = ""
    redirect_count: int = 0
    elapsed_ms: int = 0
    details: dict[str, Any] = field(default_factory=dict)


class BrowserService(ABC):
    """What a feature may ask of the shared browser."""

    @abstractmethod
    async def start(self) -> None:
        """Start the browser now (it also starts on first use). Raises BrowserUnavailable."""

    @abstractmethod
    async def stop(self) -> None:
        """Close every context and the browser process. Safe to call when it never started."""

    @abstractmethod
    def is_available(self) -> bool:
        """Whether a page load can be attempted: enabled, installed and not known to be broken."""

    @abstractmethod
    def diagnostics(self) -> dict[str, Any]:
        """Safe facts for health screens: status, browser, headless, why unavailable. Never secrets or paths."""

    @abstractmethod
    async def new_context(self) -> Any:
        """A fresh isolated browser context: no cookies, storage, service workers or downloads."""

    @abstractmethod
    async def new_page(self, context: Any) -> Any:
        """A page in `context` with the service's timeouts set."""

    @abstractmethod
    async def close_page(self, page: Any) -> None:
        """Close a page; never raises."""

    @abstractmethod
    async def close_context(self, context: Any) -> None:
        """Close a context and everything in it; never raises."""

    @abstractmethod
    async def fetch_rendered_page(self, url: str, *, wait_for: Sequence[str] = (),
                                  metadata: dict[str, Any] | None = None, visible: bool = False) -> RenderedPage:
        """Load `url` in an isolated page and return its rendered HTML.

        Navigation waits for DOMContentLoaded, then for any `wait_for` selector and for the page text to settle —
        bounded waits, never `networkidle`. Raises BrowserUnavailable or NavigationFailed.

        `visible` renders in a window of its own, closed with the page: one at a time, spaced apart. Only JD recovery
        asks for it, for the hosts in PLAYWRIGHT_VISIBLE_HOSTS.
        """

    async def release_idle(self) -> None:
        """Close the browser if configuration says not to keep it running between batches."""

    @asynccontextmanager
    async def page_session(self) -> AsyncIterator[Any]:
        """An isolated page for one task, closed with its context whatever happens."""
        context = await self.new_context()
        page = None
        try:
            page = await self.new_page(context)
            yield page
        finally:
            if page is not None:
                await self.close_page(page)
            await self.close_context(context)


_service: BrowserService | None = None


def get_browser_service() -> BrowserService:
    """The process's browser service, configured from settings. Creating it starts nothing."""
    global _service
    if _service is None:
        from app.infrastructure.browser.playwright_service import PlaywrightBrowserService

        _service = PlaywrightBrowserService.from_settings()
    return _service


def set_browser_service(service: BrowserService | None) -> None:
    """Replace the process's browser service (tests); None goes back to the configured one."""
    global _service
    _service = service


async def shutdown_browser_service() -> None:
    """Close the browser at application shutdown. Never raises."""
    if _service is not None:
        try:
            await _service.stop()
        except Exception:
            logging.getLogger("jobhunt.browser").warning("browser shutdown failed", exc_info=True)
