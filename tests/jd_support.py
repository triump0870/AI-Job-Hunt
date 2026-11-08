"""Shared helpers for the job-description recovery tests: a local posting server, a fake browser service and a fake
Playwright runtime. Nothing here reaches the internet: every page is served from tests/fixtures/jd_recovery."""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Self

from app.infrastructure.browser.service import (
    BrowserService,
    BrowserUnavailable,
    NavigationFailed,
    RenderedPage,
)

PAGES = Path(__file__).resolve().parent / "fixtures" / "jd_recovery"


def page(name: str) -> str:
    return (PAGES / f"{name}.html").read_text()


class PostingServer:
    """Serves the fixture pages, plus routes for redirects, removed postings, errors and non-HTML content.

        /<name>.html            the fixture page
        /redirect/<name>        302 → /<name>.html (an aggregator link that lands on the ATS page)
        /loop-a, /loop-b        redirect to each other forever
        /gone                   404, /expired 410, /busy 503, /limited 429, /forbidden 403
        /file.pdf               a PDF, not a web page
        /custom/<key>           whatever `routes[key]` says: (status, body, headers)
    """

    def __init__(self) -> None:
        self.hits: list[str] = []
        self.routes: dict[str, tuple[int, str, dict[str, str]]] = {}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep test output quiet
                pass

            def do_GET(self) -> None:
                server.hits.append(self.path)
                status, body, headers = server.respond(self.path)
                data = body.encode("utf-8")
                self.send_response(status)
                headers = {"Content-Type": "text/html; charset=utf-8", **headers}
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def respond(self, path: str) -> tuple[int, str, dict[str, str]]:
        path = path.split("?", 1)[0]
        if path.startswith("/custom/"):
            return self.routes[path.removeprefix("/custom/")]
        if path.startswith("/redirect/"):
            return 302, "", {"Location": f"/{path.removeprefix('/redirect/')}.html"}
        fixed = {"/loop-a": (302, "", {"Location": "/loop-b"}), "/loop-b": (302, "", {"Location": "/loop-a"}),
                 "/gone": (404, "<h1>Not found</h1>", {}), "/expired": (410, "<h1>Gone</h1>", {}),
                 "/busy": (503, "<h1>Service unavailable</h1>", {}),
                 "/limited": (429, "<h1>Too many requests</h1>", {"Retry-After": "1"}),
                 "/forbidden": (403, "<h1>Forbidden</h1>", {}),
                 "/file.pdf": (200, "%PDF-1.4 not a page", {"Content-Type": "application/pdf"})}
        if path in fixed:
            return fixed[path]
        target = PAGES / path.lstrip("/")
        if path.endswith(".html") and target.is_file():
            return 200, target.read_text(), {}
        return 404, "<h1>Not found</h1>", {}

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeBrowser(BrowserService):
    """A browser service that renders fixture pages without Chromium, and records what it was asked."""

    def __init__(self, pages: dict[str, tuple[int, str]] | None = None, *, available: bool = True,
                 error: Exception | None = None, final_url: str | None = None):
        self.pages = pages or {}
        self.available = available
        self.error = error
        self.final_url = final_url
        self.calls: list[str] = []
        self.released = 0

    async def start(self) -> None:
        if not self.available:
            raise BrowserUnavailable("CHROMIUM_MISSING", "Chromium executable missing")

    async def stop(self) -> None:
        pass

    def is_available(self) -> bool:
        return self.available

    def diagnostics(self) -> dict[str, Any]:
        return {"status": "AVAILABLE" if self.available else "UNAVAILABLE",
                "reason": None if self.available else "Chromium executable missing"}

    async def new_context(self) -> Any:
        return object()

    async def new_page(self, context: Any) -> Any:
        return object()

    async def close_page(self, page: Any) -> None:
        pass

    async def close_context(self, context: Any) -> None:
        pass

    async def release_idle(self) -> None:
        self.released += 1

    async def fetch_rendered_page(self, url: str, *, wait_for=(), metadata=None) -> RenderedPage:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        key = next((k for k in self.pages if url.endswith(k)), None)
        status, html = self.pages[key] if key else (404, "<h1>Not found</h1>")
        return RenderedPage(url=url, final_url=self.final_url or url, status=status, html=html, title="")


def navigation_failed(category: str) -> NavigationFailed:
    return NavigationFailed(category, f"{category.lower()}: simulated")


# ───────────── a fake Playwright runtime, for BrowserService lifecycle tests ─────────────

class FakeRequest:
    def __init__(self, redirected_from: FakeRequest | None = None):
        self.redirected_from = redirected_from


class FakeResponse:
    def __init__(self, status: int = 200, redirects: int = 0):
        self.status = status
        request = None
        for _ in range(redirects):
            request = FakeRequest(request)
        self.request = FakeRequest(request)


class FakePage:
    def __init__(self, runtime: FakeRuntime, context: FakeContext):
        self.runtime, self.context, self.closed = runtime, context, False
        self.url = ""

    def on(self, *_: Any) -> None:
        pass

    async def goto(self, url: str, wait_until: str = "", timeout: float = 0) -> FakeResponse:
        self.runtime.wait_until.append(wait_until)
        self.runtime.active += 1
        self.runtime.peak = max(self.runtime.peak, self.runtime.active)
        try:
            await asyncio.sleep(self.runtime.delay)
            if self.runtime.goto_error is not None:
                raise self.runtime.goto_error
        finally:
            self.runtime.active -= 1
        self.url = self.runtime.final_url or url
        return FakeResponse(200, self.runtime.redirects)

    async def wait_for_selector(self, *_: Any, **__: Any) -> None:
        pass

    async def evaluate(self, *_: Any) -> int:
        return 100

    async def content(self) -> str:
        return self.runtime.html

    async def title(self) -> str:
        return "Fake page"

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, runtime: FakeRuntime, options: dict[str, Any]):
        self.runtime, self.options, self.closed, self.pages = runtime, options, False, []

    def set_default_navigation_timeout(self, value: float) -> None:
        self.runtime.timeouts.append(value)

    def set_default_timeout(self, value: float) -> None:
        pass

    async def route(self, *_: Any) -> None:
        pass

    async def new_page(self) -> FakePage:
        page = FakePage(self.runtime, self)
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True


class FakeCDPSession:
    def on(self, event: str, handler: Any) -> None:
        pass

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {}


class FakeBrowserProcess:
    def __init__(self, runtime: FakeRuntime):
        self.runtime, self.connected, self.handlers = runtime, True, {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    def is_connected(self) -> bool:
        return self.connected

    async def new_browser_cdp_session(self) -> FakeCDPSession:
        return FakeCDPSession()

    async def new_context(self, **options: Any) -> FakeContext:
        context = FakeContext(self.runtime, options)
        self.runtime.contexts.append(context)
        return context

    async def close(self) -> None:
        self.connected = False
        self.runtime.closed_browsers += 1

    def crash(self) -> None:
        self.connected = False
        self.handlers.get("disconnected", lambda *_: None)(self)


class FakeRuntime:
    """Stands in for `async_playwright()`: records launches, contexts and pages."""

    def __init__(self, html: str = "<html><body><p>ok</p></body></html>", *, launch_error: Exception | None = None):
        self.html = html
        self.launch_error = launch_error
        self.goto_error: Exception | None = None
        self.delay = 0.0
        self.redirects = 0
        self.final_url: str | None = None
        self.launches: list[dict[str, Any]] = []
        self.browsers: list[FakeBrowserProcess] = []
        self.contexts: list[FakeContext] = []
        self.timeouts: list[float] = []
        self.wait_until: list[str] = []
        self.active = self.peak = self.closed_browsers = self.stopped = 0
        self.chromium = self

    # async_playwright() → object with .start()
    def __call__(self) -> FakeRuntime:
        return self

    async def start(self) -> FakeRuntime:
        return self

    async def stop(self) -> None:
        self.stopped += 1

    async def launch(self, **options: Any) -> FakeBrowserProcess:
        if self.launch_error is not None:
            raise self.launch_error
        self.launches.append(options)
        browser = FakeBrowserProcess(self)
        self.browsers.append(browser)
        return browser
