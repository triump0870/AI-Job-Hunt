"""The shared browser (app.infrastructure.browser): lazy start, one Chromium reused across isolated contexts, crash
recovery, bounded navigation, failure classification, concurrency limits and clean shutdown.

Lifecycle tests drive a fake Playwright runtime (tests/jd_support.py). The last tests start real headless Chromium
against a local page and are skipped when Chromium is not installed (`python -m playwright install chromium`)."""
import asyncio

import pytest

from app.infrastructure.browser import playwright_service as pw
from app.infrastructure.browser.service import (
    BLOCKED_URL,
    BROWSER_START_FAILED,
    CHROMIUM_MISSING,
    DNS,
    PLAYWRIGHT_DISABLED,
    PLAYWRIGHT_NOT_INSTALLED,
    REDIRECT_LOOP,
    SSL,
    TIMEOUT,
    BrowserUnavailable,
    NavigationFailed,
    get_browser_service,
    set_browser_service,
    shutdown_browser_service,
)
from app.settings import get_settings
from tests.jd_support import FakeRuntime, PostingServer

URL = "https://jobs.example.com/posting/1"


@pytest.fixture
def runtime(monkeypatch):
    fake = FakeRuntime("<html><body><main><p>Hello posting</p></main></body></html>")
    monkeypatch.setattr("playwright.async_api.async_playwright", fake)
    # Public-URL checks would resolve DNS; the fake runtime never touches the network anyway.
    monkeypatch.setattr("app.services.url_safety.check_public_url", lambda url, allow_private=False: None)
    return fake


def service(**options) -> pw.PlaywrightBrowserService:
    return pw.PlaywrightBrowserService(record_telemetry=False, **options)


def run(coro):
    return asyncio.run(coro)


# ───────────── lifecycle ─────────────

def test_nothing_starts_until_a_page_is_needed(runtime):
    browser = service()
    assert runtime.launches == [] and browser.diagnostics()["running"] is False

    async def go():
        page = await browser.fetch_rendered_page(URL)
        await browser.stop()
        return page

    page = run(go())
    assert len(runtime.launches) == 1 and "Hello posting" in page.html and page.status == 200


def test_one_browser_is_reused_for_many_pages(runtime):
    browser = service()

    async def go():
        for _ in range(4):
            await browser.fetch_rendered_page(URL)
        await browser.stop()

    run(go())
    assert len(runtime.launches) == 1 and browser.launches == 1
    assert len(runtime.contexts) == 4, "each page gets its own context"


def test_every_page_gets_an_isolated_context_that_is_closed_afterwards(runtime):
    browser = service()

    async def go():
        await browser.fetch_rendered_page(URL)
        await browser.fetch_rendered_page(URL)
        await browser.stop()

    run(go())
    first, second = runtime.contexts
    assert first is not second
    for context in runtime.contexts:
        assert context.closed and all(page.closed for page in context.pages)
        assert context.options["accept_downloads"] is False and context.options["service_workers"] == "allow"
        assert "user_agent" not in context.options, "Chromium's native User-Agent must be preserved"
        assert "storage_state" not in context.options and "extra_http_headers" not in context.options


def test_pages_and_contexts_are_closed_even_when_navigation_fails(runtime):
    runtime.goto_error = RuntimeError("net::ERR_CONNECTION_REFUSED at https://jobs.example.com")
    browser = service()

    async def go():
        with pytest.raises(NavigationFailed):
            await browser.fetch_rendered_page(URL)
        await browser.stop()

    run(go())
    assert runtime.contexts and all(c.closed and all(p.closed for p in c.pages) for c in runtime.contexts)


def test_a_crashed_browser_is_started_again(runtime):
    browser = service()

    async def go():
        await browser.fetch_rendered_page(URL)
        runtime.browsers[0].crash()
        assert browser.diagnostics()["running"] is False
        await browser.fetch_rendered_page(URL)
        await browser.stop()

    run(go())
    assert len(runtime.launches) == 2 and browser.crashes == 1


def test_stop_closes_the_browser_and_playwright(runtime):
    browser = service()

    async def go():
        await browser.fetch_rendered_page(URL)
        await browser.stop()

    run(go())
    assert runtime.closed_browsers == 1 and runtime.stopped == 1


def test_the_browser_closes_after_a_batch_unless_kept_running(runtime):
    kept, closed = service(keep_running=True), service(keep_running=False)

    async def go(browser):
        await browser.fetch_rendered_page(URL)
        await browser.release_idle()
        running = browser.diagnostics()["running"]
        await browser.stop()
        return running

    assert run(go(kept)) is True
    assert run(go(closed)) is False


def test_the_application_shutdown_hook_closes_the_shared_browser(runtime):
    browser = service()
    set_browser_service(browser)
    try:
        async def go():
            await browser.fetch_rendered_page(URL)
            await shutdown_browser_service()

        run(go())
        assert runtime.closed_browsers == 1
    finally:
        set_browser_service(None)


def test_the_configured_service_is_one_per_process_and_starts_nothing():
    set_browser_service(None)
    first = get_browser_service()
    assert get_browser_service() is first and first.diagnostics()["running"] is False
    set_browser_service(None)


# ───────────── configuration ─────────────

def test_headless_is_the_default_and_can_be_turned_off_for_debugging(runtime, monkeypatch):
    run(service().fetch_rendered_page(URL))
    assert runtime.launches[-1]["headless"] is True
    monkeypatch.setattr(get_settings(), "playwright_headless", False)
    headed = pw.PlaywrightBrowserService.from_settings()
    headed.record_telemetry = False
    run(headed.fetch_rendered_page(URL))
    assert runtime.launches[-1]["headless"] is False and headed.diagnostics()["headless"] is False


def test_compatibility_defaults_and_diagnostics(runtime):
    browser = service()
    run(browser.fetch_rendered_page(URL))
    assert runtime.launches[-1] == {"headless": True}
    diagnostics = browser.diagnostics()
    assert diagnostics["enabled"] is True
    assert diagnostics["service_workers"] == "allow"
    assert diagnostics["block_nonessential_resources"] is False
    assert diagnostics["debug_artifacts"] is False


def test_navigation_waits_for_dom_content_not_network_idle(runtime):
    run(service().fetch_rendered_page(URL))
    assert runtime.wait_until == ["domcontentloaded"]


def test_timeouts_come_from_configuration(runtime):
    run(service(navigation_timeout_seconds=12).fetch_rendered_page(URL))
    assert runtime.timeouts == [12000]


def test_redirects_are_counted_and_the_final_url_kept(runtime):
    runtime.redirects, runtime.final_url = 2, "https://ats.example.com/jobs/9"
    page = run(service().fetch_rendered_page(URL))
    assert page.redirect_count == 2 and page.final_url == "https://ats.example.com/jobs/9" and page.url == URL


def test_no_more_pages_than_the_concurrency_limit(runtime):
    runtime.delay = 0.05
    browser = service(max_concurrency=2)

    async def go():
        await asyncio.gather(*(browser.fetch_rendered_page(URL) for _ in range(6)))
        await browser.stop()

    run(go())
    assert runtime.peak == 2


# ───────────── failures ─────────────

def test_a_navigation_timeout_is_a_retryable_timeout(runtime):
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    runtime.goto_error = PlaywrightTimeout("Timeout 45000ms exceeded.")
    with pytest.raises(NavigationFailed) as failed:
        run(service().fetch_rendered_page(URL))
    assert failed.value.category == TIMEOUT and failed.value.retryable


def test_a_page_that_never_settles_hits_the_page_timeout(runtime):
    runtime.delay = 2
    with pytest.raises(NavigationFailed) as failed:
        run(service(navigation_timeout_seconds=1, page_timeout_seconds=1).fetch_rendered_page(URL))
    assert failed.value.category == TIMEOUT


@pytest.mark.parametrize("message,category,retryable", [
    ("net::ERR_NAME_NOT_RESOLVED at https://x", DNS, True),
    ("net::ERR_CERT_AUTHORITY_INVALID at https://x", SSL, False),
    ("net::ERR_TOO_MANY_REDIRECTS at https://x", REDIRECT_LOOP, False),
])
def test_network_errors_are_classified(runtime, message, category, retryable):
    runtime.goto_error = RuntimeError(message)
    with pytest.raises(NavigationFailed) as failed:
        run(service().fetch_rendered_page(URL))
    assert failed.value.category == category and failed.value.retryable is retryable


def test_a_browser_that_dies_mid_page_is_replaced_next_time(runtime):
    runtime.goto_error = RuntimeError("Target page, context or browser has been closed")
    browser = service()

    async def go():
        with pytest.raises(NavigationFailed) as failed:
            await browser.fetch_rendered_page(URL)
        runtime.goto_error = None
        await browser.fetch_rendered_page(URL)
        await browser.stop()
        return failed.value

    failure = run(go())
    assert failure.category == "BROWSER_CRASHED" and failure.retryable and len(runtime.launches) == 2


def test_a_missing_chromium_is_reported_and_the_app_keeps_running(monkeypatch):
    fake = FakeRuntime(launch_error=RuntimeError("Executable doesn't exist at /ms-playwright/chromium/chrome"))
    monkeypatch.setattr("playwright.async_api.async_playwright", fake)
    monkeypatch.setattr("app.services.url_safety.check_public_url", lambda url, allow_private=False: None)
    monkeypatch.setattr(pw, "chromium_installed", lambda: False)
    browser = service()
    with pytest.raises(BrowserUnavailable) as failed:
        run(browser.fetch_rendered_page(URL))
    assert failed.value.code == CHROMIUM_MISSING and "playwright install chromium" in str(failed.value)
    assert browser.is_available() is False
    diagnostics = browser.diagnostics()
    assert diagnostics["status"] == "UNAVAILABLE" and diagnostics["reason_code"] == CHROMIUM_MISSING
    assert "/ms-playwright" not in str(diagnostics), "no file paths in diagnostics"


def test_any_other_start_failure_is_reported_and_retried_later(monkeypatch):
    fake = FakeRuntime(launch_error=OSError("no display"))
    monkeypatch.setattr("playwright.async_api.async_playwright", fake)
    monkeypatch.setattr("app.services.url_safety.check_public_url", lambda url, allow_private=False: None)
    browser = service()
    with pytest.raises(BrowserUnavailable) as failed:
        run(browser.start())
    assert failed.value.code == BROWSER_START_FAILED and browser.is_available()
    fake.launch_error = None
    run(browser.fetch_rendered_page(URL))
    assert len(fake.launches) == 1


def test_disabled_or_uninstalled_playwright_is_unavailable(monkeypatch):
    disabled = service(enabled=False)
    assert not disabled.is_available() and disabled.diagnostics()["reason_code"] == PLAYWRIGHT_DISABLED
    with pytest.raises(BrowserUnavailable):
        run(disabled.fetch_rendered_page(URL))
    monkeypatch.setattr(pw, "playwright_installed", lambda: False)
    missing = service()
    assert not missing.is_available() and missing.diagnostics()["reason_code"] == PLAYWRIGHT_NOT_INSTALLED


def test_private_addresses_are_never_loaded(runtime, monkeypatch):
    monkeypatch.undo()  # back to the real public-URL check
    monkeypatch.setattr("playwright.async_api.async_playwright", runtime)
    with pytest.raises(NavigationFailed) as failed:
        run(service().fetch_rendered_page("http://127.0.0.1:9/admin"))
    assert failed.value.category == BLOCKED_URL and runtime.launches == []


class FakeRoute:
    def __init__(self, url: str, kind: str = "script", navigation: bool = False):
        from types import SimpleNamespace

        self.request = SimpleNamespace(url=url, resource_type=kind, is_navigation_request=lambda: navigation)
        self.aborted, self.continued = None, False

    async def abort(self, reason: str) -> None:
        self.aborted = reason

    async def continue_(self) -> None:
        self.continued = True


@pytest.mark.parametrize("url,kind,navigation,allowed", [
    ("https://cdn.example.com/app.js", "script", False, True),
    ("https://cdn.example.com/logo.png", "image", False, True),
    ("http://169.254.169.254/latest/meta-data/", "fetch", False, False),  # cloud metadata
    ("http://localhost:5432/", "xhr", False, False),
    ("http://10.0.0.8/admin", "document", True, False),
    ("http://db.internal/", "script", False, False),
])
def test_pages_cannot_make_the_browser_reach_internal_services(url, kind, navigation, allowed):
    route = FakeRoute(url, kind, navigation)
    run(service()._route(route))
    assert route.continued is allowed and (route.aborted is None) is allowed


@pytest.mark.parametrize("kind", ["image", "media", "font"])
def test_nonessential_resource_blocking_is_opt_in(kind):
    allowed = FakeRoute("https://cdn.example.com/asset", kind)
    blocked = FakeRoute("https://cdn.example.com/asset", kind)
    run(service()._route(allowed))
    run(service(block_nonessential_resources=True)._route(blocked))
    assert allowed.continued and blocked.aborted == "blockedbyclient"


def test_the_cheap_private_check():
    from app.services.url_safety import obviously_private

    assert obviously_private("http://127.0.0.1/x") and obviously_private("http://[::1]/") and \
        obviously_private("http://192.168.1.4") and obviously_private("http://printer.local/")
    assert not obviously_private("https://boards.greenhouse.io/x") and not obviously_private("https://8.8.8.8/")


def test_page_loads_are_recorded_as_external_calls(runtime):
    import uuid

    from sqlalchemy import select

    import app.main  # noqa: F401  migrates the test database, as every API test does
    from app.db import SessionLocal
    from app.models import ExternalAPIRequestLog

    tag = uuid.uuid4().hex
    run(pw.PlaywrightBrowserService().fetch_rendered_page(URL, metadata={"test_tag": tag}))
    with SessionLocal() as db:
        rows = [row for row in db.scalars(select(ExternalAPIRequestLog).where(
            ExternalAPIRequestLog.service == "playwright")).all() if (row.request_metadata or {}).get("test_tag") == tag]
    assert len(rows) == 1
    row = rows[0]
    assert row.provider == "WEB" and row.operation == "render_page" and row.endpoint_host == "jobs.example.com"
    assert row.status == "SUCCEEDED" and row.request_metadata["headless"] is True


# ───────────── real Chromium ─────────────

needs_chromium = pytest.mark.skipif(not (pw.playwright_installed() and pw.chromium_installed()),
                                    reason="Chromium is not installed (python -m playwright install chromium)")


@needs_chromium
def test_real_chromium_renders_a_javascript_posting():
    with PostingServer() as server:
        browser = pw.PlaywrightBrowserService(allow_private_hosts=True, record_telemetry=False,
                                              navigation_timeout_seconds=20)

        async def go():
            try:
                return await browser.fetch_rendered_page(server.url("rendered.html"),
                                                         wait_for=(".job-description",))
            finally:
                await browser.stop()

        page = run(go())
    assert page.status == 200 and "Kafka and Flink streaming pipelines" in page.html
    assert "Staff Data Engineer" in page.title


@needs_chromium
def test_real_chromium_contexts_share_no_cookies():
    with PostingServer() as server:
        script = ("document.getElementById('out').textContent = 'cookie=[' + document.cookie + ']';"
                  "document.cookie = 'seen=1; path=/';")
        server.routes["cookie"] = (200, f"<html><body><p id='out'></p><script>{script}</script></body></html>", {})
        browser = pw.PlaywrightBrowserService(allow_private_hosts=True, record_telemetry=False)

        async def go():
            try:
                first = await browser.fetch_rendered_page(server.url("custom/cookie"))
                second = await browser.fetch_rendered_page(server.url("custom/cookie"))
                return first, second, browser.launches
            finally:
                await browser.stop()

        first, second, launches = run(go())
    assert "cookie=[]" in first.html and "cookie=[]" in second.html, "the second page saw the first page's cookie"
    assert launches == 1


# ───────────── HTTP redirects (Playwright's routing does not see their hops) ─────────────

@pytest.fixture
def public_loopback(monkeypatch):
    """127.0.0.1 counts as public, so a page can be loaded with the private checks on; every other address and name
    meets the real checks. `localhost` is the same server, and internal."""
    from app.services import url_safety

    real_check, real_private = url_safety.check_public_url, url_safety.obviously_private

    def check(url, allow_private=False):
        if "//127.0.0.1" not in url:
            real_check(url, allow_private=allow_private)

    monkeypatch.setattr(url_safety, "check_public_url", check)
    monkeypatch.setattr(url_safety, "obviously_private", lambda url: "//127.0.0.1" not in url and real_private(url))


def _redirecting_site(server: PostingServer) -> str:
    """Every way a page can start a request that a server then redirects to `localhost` (internal)."""
    internal = f"http://localhost:{server.httpd.server_port}"
    routes = server.routes
    for name in ("top", "image", "frame", "popup", "worker", "serviceworker"):
        routes[f"go-{name}"] = (302, "", {"Location": f"{internal}/custom/internal-{name}"})
        routes[f"internal-{name}"] = (200, "<html><body>internal</body></html>", {})
    page = "<html><body><main><p>A posting long enough to read</p></main>{}</body></html>"
    routes["image-page"] = (200, page.format("<img src='/custom/go-image'>"), {})
    routes["frame-page"] = (200, page.format("<iframe src='/custom/go-frame'></iframe>"), {})
    routes["popup-page"] = (200, page.format("<script>window.open('/custom/go-popup')</script>"), {})
    routes["worker-page"] = (200, page.format("<script>new Worker('/custom/worker.js')</script>"), {})
    routes["worker.js"] = (200, "fetch('/custom/go-worker', {mode: 'no-cors'})", {"Content-Type": "text/javascript"})
    routes["sw-page"] = (200, page.format("<script>navigator.serviceWorker.register('/custom/sw.js', "
                                          "{scope: '/custom/'})</script>"), {})
    routes["sw.js"] = (200, "fetch('/custom/go-serviceworker', {mode: 'no-cors'})",
                       {"Content-Type": "text/javascript"})
    return internal


def _reached(server: PostingServer) -> list[str]:
    return [hit for hit in server.hits if "/internal-" in hit]


SUBREQUEST_PAGES = ("image-page", "frame-page", "popup-page", "worker-page", "sw-page")


@needs_chromium
def test_real_chromium_sends_no_redirect_hop_to_an_internal_address(public_loopback):
    with PostingServer() as server:
        _redirecting_site(server)
        browser = pw.PlaywrightBrowserService(record_telemetry=False, navigation_timeout_seconds=15)

        async def go():
            try:
                with pytest.raises(NavigationFailed) as failed:
                    await browser.fetch_rendered_page(server.url("custom/go-top"))
                assert failed.value.category == BLOCKED_URL and not failed.value.retryable
                for name in SUBREQUEST_PAGES:  # the page itself still loads; only the hop is stopped
                    page = await browser.fetch_rendered_page(server.url(f"custom/{name}"))
                    assert "A posting long enough" in page.html, name
                    await asyncio.sleep(0.5)  # what the page started has had its time
            finally:
                await browser.stop()

        run(go())
        assert _reached(server) == []
        assert {f"/custom/go-{n}" for n in ("top", "image", "frame", "popup", "worker", "serviceworker")} <= set(
            server.hits), "every first hop was sent, so every redirect was really met"


@needs_chromium
def test_real_chromium_would_reach_those_addresses_without_the_private_checks():
    """The control: the same site with private hosts allowed, so `localhost` is reachable and every hop arrives."""
    with PostingServer() as server:
        _redirecting_site(server)
        browser = pw.PlaywrightBrowserService(allow_private_hosts=True, record_telemetry=False,
                                              navigation_timeout_seconds=15)

        async def go():
            try:
                first = await browser.fetch_rendered_page(server.url("custom/go-top"))
                for name in SUBREQUEST_PAGES:
                    await browser.fetch_rendered_page(server.url(f"custom/{name}"))
                await asyncio.sleep(0.5)
                return first
            finally:
                await browser.stop()

        first = run(go())
        assert first.redirect_count == 1 and "localhost" in first.final_url
        assert {f"/custom/internal-{n}" for n in ("top", "image", "frame", "popup", "worker", "serviceworker")} <= set(
            server.hits)


@needs_chromium
def test_real_chromium_still_follows_a_redirect_to_a_public_address(public_loopback):
    with PostingServer() as server:
        browser = pw.PlaywrightBrowserService(record_telemetry=False, navigation_timeout_seconds=15)

        async def go():
            try:
                return await browser.fetch_rendered_page(server.url("redirect/rendered"),
                                                         wait_for=(".job-description",))
            finally:
                await browser.stop()

        page = run(go())
    assert page.redirect_count == 1 and page.final_url.endswith("/rendered.html") and page.status == 200


@needs_chromium
def test_real_chromium_a_browser_started_again_still_stops_redirects(public_loopback):
    with PostingServer() as server:
        _redirecting_site(server)
        browser = pw.PlaywrightBrowserService(record_telemetry=False, navigation_timeout_seconds=15)

        async def go():
            try:
                await browser.start()
                await browser.stop()
                with pytest.raises(NavigationFailed):
                    await browser.fetch_rendered_page(server.url("custom/go-top"))
            finally:
                await browser.stop()

        run(go())
        assert _reached(server) == [] and "/custom/go-top" in server.hits


@needs_chromium
def test_real_chromium_an_application_page_is_stopped_at_the_same_redirect(public_loopback):
    from app.infrastructure.application_browser.ephemeral import EphemeralApplicationSessions

    with PostingServer() as server:
        _redirecting_site(server)
        browser = pw.PlaywrightBrowserService(record_telemetry=False, navigation_timeout_seconds=15)
        sessions = EphemeralApplicationSessions(browser, record_telemetry=False)

        async def go():
            try:
                async with sessions.session(user_id=1, ats="generic") as page:
                    with pytest.raises(NavigationFailed) as failed:
                        await page.goto(server.url("custom/go-top"))
                    assert failed.value.category == BLOCKED_URL
                async with sessions.session(user_id=1, ats="generic") as page:
                    await page.goto(server.url("custom/image-page"))  # loads; its image's hop does not
                    await asyncio.sleep(0.3)
            finally:
                await browser.stop()

        run(go())
        assert _reached(server) == [] and {"/custom/go-top", "/custom/go-image"} <= set(server.hits)


def test_a_browser_without_the_redirect_guard_does_not_start(monkeypatch):
    fake = FakeRuntime()
    monkeypatch.setattr("playwright.async_api.async_playwright", fake)

    async def refuses(self):
        raise RuntimeError("no CDP here")

    monkeypatch.setattr("tests.jd_support.FakeBrowserProcess.new_browser_cdp_session", refuses)
    browser = service()
    with pytest.raises(BrowserUnavailable) as failed:
        run(browser.start())
    assert failed.value.code == BROWSER_START_FAILED and not browser._running()
