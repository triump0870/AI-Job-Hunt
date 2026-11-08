"""The browser-use runner: one browser session for one application execution, driven command by command.

Job Hunt OS starts this file with the browser-use Web UI's own Python (BROWSER_USE_PYTHON, browser-use 0.1.48 and
Playwright installed there) as a child process, and talks to it in JSON lines over stdin/stdout:

    → {"id": 1, "cmd": "observe", "args": {}}
    ← {"id": 1, "ok": true, "result": {...}}            or  {"id": 1, "ok": false, "error": {"code", "message"}}

It reuses browser-use's BrowserContext (its DOM indexing, element location, typing and clicking) and nothing that
decides: there is no browser-use Agent, no LLM and no controller registry here. Every command is one bounded action
Job Hunt OS already validated (app.services.browser_actions); this file enforces its own limits on top, because a
bug upstream must still not be able to submit an application:

  * Chromium is launched plainly by Playwright: no remote-debugging port, no anti-detection flags, no user-agent
    spoofing (browser-use's own launcher adds the first two, so it is not used). Telemetry is off.
  * While Job Hunt OS drives the page, every non-GET request is aborted, so a stray key press or a page script
    cannot send a half-filled form. Three exceptions: a valid, unexpired HMAC token (SUBMIT, signed only after the
    pre-submit validator and the shared claim, lets the page send for its click; LOGIN, only to the login domain),
    the upload a file selection starts (an XHR/fetch PUT or multipart POST, for a few seconds after `upload`), and a
    window handed to a person (takeover with writes), which behaves like a normal browser. A top-level navigation
    outside the allowed hosts is aborted while Job Hunt OS drives, and private addresses are always refused unless
    the caller allows them (tests).
  * Playwright's routing sees only the first request of an HTTP redirect (it lets the following hops through itself),
    so a browser-level CDP Fetch session (`_guard_redirects`) holds every request of the browser and applies the same
    rules to each hop before it is sent, in every page, popup, frame and worker. Server redirects may also stay on the
    route's `redirect_domains` (one ATS moving a form between its own hosts). A hop it refuses fails with
    ERR_BLOCKED_BY_CLIENT; `goto` reports that as URL_NOT_ALLOWED.
  * A click on a submit-like element (type=submit, a form button without a type, or a Submit/Apply/Send label inside
    a form) is refused unless it is `submit` / `login_submit` with that token. Nothing evaluates script a caller
    sent: the only scripts are the constants below, which read the page.
  * Uploads only from the run's own folder. Password fields only through `login_fill`, and only on the login domain.
  * Nothing is logged but command names and error classes (stderr); values never are.

Limits: this is Chromium's CDP, and it rests on `redirectedRequestId` marking a hop: the real-Chromium tests fail if a
browser or Playwright upgrade changes that. A host name is checked by what it resolves to at that moment, for a hop as
for a first request; the browser resolves it again itself, so DNS rebinding is not covered by either.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import sys
import time
from typing import Any
from urllib.parse import urlparse

os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ.setdefault("BROWSER_USE_LOGGING_LEVEL", "result")
LOAD_WAIT_MS = 8000  # how long observe waits for a page's `load` event before reading it as it is

PROTOCOL = 1
MAX_ELEMENTS = 200
MAX_TEXT = 6000
MAX_HTML = 400_000
SUBMIT_WORDS = ("submit", "apply", "send", "finish", "complete", "confirm")

# Read-only page scripts (constants; nothing a caller sends is ever evaluated).
DESCRIBE_JS = """(el) => {
  const text = (n) => (n ? (n.innerText || n.textContent || '') : '').replace(/\\s+/g, ' ').trim();
  const tag = el.tagName.toLowerCase();
  // The form a control belongs to (its `form` attribute included). A <button> with no type submits that form.
  const owner = el.form || el.closest('form');
  const type = (el.getAttribute('type') || (tag === 'button' ? (owner ? 'submit' : 'button')
                                            : (tag === 'input' ? 'text' : ''))).toLowerCase();
  let label = '';
  if (el.labels && el.labels.length) label = text(el.labels[0]);
  if (!label && el.getAttribute('aria-labelledby')) {
    label = el.getAttribute('aria-labelledby').split(/\\s+/).map(id => text(document.getElementById(id))).join(' ');
  }
  if (!label) label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
  if (!label && (tag === 'button' || tag === 'a')) label = text(el) || el.getAttribute('value') || '';
  if (!label && tag === 'input' && (type === 'submit' || type === 'button')) label = el.getAttribute('value') || '';
  if (!label) { const wrap = el.closest('label'); if (wrap) label = text(wrap); }
  if (!label) {
    const box = el.closest('.field, .form-group, .question, [class*="field"], [class*="question"], li, div');
    if (box) { const l = box.querySelector('label, legend, .label, [class*="label"]'); if (l) label = text(l); }
  }
  const fieldset = el.closest('fieldset');
  let legend = fieldset ? text(fieldset.querySelector('legend')) : '';
  if (!legend) {
    // A group heading outside the control's own label (new Greenhouse boards: "Resume/CV *" over an "Attach" button).
    const group = el.closest('[role="group"]');
    if (group) {
      const by = group.getAttribute('aria-labelledby');
      legend = by ? by.split(/\\s+/).map(id => text(document.getElementById(id))).join(' ').trim()
                  : (group.getAttribute('aria-label') || '');
    }
  }
  const form = owner;
  const required = el.required || el.getAttribute('aria-required') === 'true' || /\\*\\s*$/.test(label);
  const options = tag === 'select' ? Array.from(el.options).slice(0, 200).map(o => ({label: text(o), value: o.value})) : [];
  const valuePresent = tag === 'select' ? !!el.value : (type === 'file' ? !!(el.files && el.files.length) :
                       (type === 'checkbox' || type === 'radio') ? el.checked : !!(el.value || '').length);
  let href = '';
  if (tag === 'a') { try { href = new URL(el.href, location.href).hostname; } catch (e) { href = ''; } }
  return {tag, type, name: el.getAttribute('name') || '', id: el.id || '', label: label.slice(0, 500),
          group_label: legend.slice(0, 500), required, options, value_present: valuePresent,
          checked: !!el.checked, value: (type === 'radio' || type === 'checkbox') ? (el.value || '') : '',
          in_form: !!form, form_index: form ? Array.prototype.indexOf.call(document.forms, form) : -1, role: el.getAttribute('role') || '', href_host: href,
          aria_hidden: el.getAttribute('aria-hidden') === 'true', tabindex: el.getAttribute('tabindex') || '',
          autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase()};
}"""
# What a dropdown widget (role=combobox) shows as chosen: its single-value text (React Select and similar), else the
# input's own value.
COMBO_VALUE_JS = """(el) => {
  let box = el;
  for (let i = 0; i < 5 && box; i++) {
    const shown = box.querySelector && box.querySelector('[class*="single-value"], [class*="singleValue"]');
    if (shown) return (shown.innerText || shown.textContent || '').trim();
    box = box.parentElement;
  }
  return (el.value || '').trim();
}"""
OPTION_TEXTS_JS = """() => Array.from(document.querySelectorAll('[role="option"]'))
  .filter(o => o.offsetParent !== null).map(o => (o.innerText || o.textContent || '').replace(/\\s+/g, ' ').trim())"""
# Pointing a person at the fields that need them (takeover): an outline and a tag per field, one banner on the page.
MARK_JS = """(el, note) => {
  el.setAttribute('data-jobhunt-needs-you', '1');
  el.style.setProperty('outline', '3px solid #e5484d', 'important');
  el.style.setProperty('outline-offset', '2px', 'important');
  const tag = document.createElement('div');
  tag.setAttribute('data-jobhunt-mark', '1');
  tag.textContent = note;
  tag.style.cssText = 'all:initial;display:block;margin:4px 0;padding:2px 8px;font:600 12px system-ui;'
    + 'color:#fff;background:#e5484d;border-radius:4px;width:max-content';
  el.insertAdjacentElement('afterend', tag);
}"""
BANNER_JS = """(message) => {
  document.querySelectorAll('[data-jobhunt-banner]').forEach(b => b.remove());
  const banner = document.createElement('div');
  banner.setAttribute('data-jobhunt-banner', '1');
  banner.setAttribute('role', 'status');
  banner.textContent = message;
  banner.style.cssText = 'all:initial;position:fixed;top:0;left:0;right:0;z-index:2147483647;padding:12px 16px;'
    + 'font:600 14px system-ui;color:#fff;background:#1f2937;box-shadow:0 2px 8px rgba(0,0,0,.3)';
  document.body.appendChild(banner);
}"""
UNMARK_JS = """() => {
  document.querySelectorAll('[data-jobhunt-mark], [data-jobhunt-banner]').forEach(n => n.remove());
  document.querySelectorAll('[data-jobhunt-needs-you]').forEach(el => {
    el.removeAttribute('data-jobhunt-needs-you'); el.style.removeProperty('outline');
    el.style.removeProperty('outline-offset');
  });
}"""
# Whether a control's own form holds a password field: a signed LOGIN click is only ever the login form's control.
LOGIN_FORM_JS = """(el) => {
  const form = el.form || el.closest('form');
  return !!(form && form.querySelector('input[type="password"]'));
}"""
# Restore a saved session's local storage for the page's origin (the items come from the person's own saved session).
RESTORE_STORAGE_JS = """(items) => {
  for (const item of items) { window.localStorage.setItem(String(item.name), String(item.value)); }
  return items.length;
}"""
FILE_NAMES_JS = "(el) => Array.from(el.files || []).map(f => f.name)"
FILE_SIZES_JS = "(el) => Array.from(el.files || []).map(f => f.size)"
# A challenge widget a person must answer, per frame: a visible checkbox iframe of reCAPTCHA (v2 / Enterprise), hCaptcha
# or Turnstile, and whether its response field is filled. Invisible widgets (reCAPTCHA v3, `size=invisible`) are left
# out: the site runs them itself when the form is sent, and no person can answer them. Returns kinds and booleans only;
# the response token never leaves the page.
CAPTCHA_JS = r"""() => {
  const shown = el => { const r = el.getBoundingClientRect(); return r.width > 20 && r.height > 20; };
  const kinds = [
    ["recaptcha", /\/recaptcha\/(api2|enterprise)\/anchor/, 'textarea[name="g-recaptcha-response"], textarea[id^="g-recaptcha-response"]'],
    ["hcaptcha", /hcaptcha\.com\/.*frame=checkbox/, 'textarea[name="h-captcha-response"], textarea[name="g-recaptcha-response"]'],
    ["turnstile", /challenges\.cloudflare\.com\//, 'input[name="cf-turnstile-response"]'],
  ];
  // Each widget is judged on its own response field: the one in its nearest container. A container holding several
  // widgets' fields is ambiguous and counts as unanswered; with one widget and one field on the page, that field.
  const fieldOf = (widget, response, widgets, all) => {
    for (let node = widget.parentElement, depth = 0; node && depth < 6; node = node.parentElement, depth++) {
      const inside = node.querySelectorAll(response);
      if (inside.length === 1) return inside[0];
      if (inside.length > 1) return null;
    }
    return widgets.length === 1 && all.length === 1 ? all[0] : null;
  };
  const found = [];
  for (const [kind, source, response] of kinds) {
    const widgets = Array.from(document.querySelectorAll('iframe'))
      .filter(f => source.test(f.src || '') && !/size=invisible/.test(f.src || '') && shown(f));
    const all = Array.from(document.querySelectorAll(response));
    for (const widget of widgets) {
      const field = fieldOf(widget, response, widgets, all);
      found.push({kind, solved: !!field && (field.value || '').length > 0});
    }
  }
  return found;
}"""
FORM_INPUTS_JS = """() => Array.from(document.querySelectorAll('form input, form select, form textarea'))
  .filter(e => !['hidden', 'search', 'submit', 'button'].includes((e.getAttribute('type') || '').toLowerCase()))
  .length"""


class Refused(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def log(message: str) -> None:
    print(f"[browser_use_runner] {message}", file=sys.stderr, flush=True)


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower().strip(".")


INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


def _private(host: str) -> bool:
    """A host that is internal by its name or its literal address (no DNS): checked on every request."""
    host = host.strip("[]")
    if host == "localhost" or host.endswith(INTERNAL_SUFFIXES):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


async def _resolves_private(host: str) -> bool:
    """Whether a host name resolves to any non-public address (app.services.url_safety.check_public_url, which this
    process cannot import). A name that does not resolve is treated as unsafe."""
    if _private(host):
        return True
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError:
        return True
    return any(not ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos)


def _key(info: dict[str, Any]) -> str:
    raw = "|".join(str(info.get(k) or "") for k in ("tag", "type", "name", "id", "label"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _group_key(info: dict[str, Any]) -> str:
    return hashlib.sha1(f"radio|{info.get('name')}|{info.get('group_label')}".encode()).hexdigest()[:16]


def _submit_like(info: dict[str, Any]) -> bool:
    tag, kind, label = info.get("tag"), info.get("type"), (info.get("label") or "").strip().lower()
    if tag == "input" and kind in ("submit", "image"):
        return True
    if tag == "button" and (kind == "submit" or (info.get("in_form") and kind in ("", "submit"))):
        return True
    worded = any(label == w or label.startswith(w + " ") for w in SUBMIT_WORDS)
    return bool(worded and info.get("in_form") and (tag in ("button", "input") or info.get("role") == "button"))


class Runner:
    def __init__(self) -> None:
        self.pw = None
        self.chromium = None
        self.bu_browser = None
        self.ctx = None
        self.sid = ""
        self.key = b""
        self.allowed: set[str] = set()
        self.allow_private = False
        self.redirect_domains: tuple[str, ...] = ()  # where a server redirect may land besides the allowed hosts
        self.redirect_guard = None  # the browser-level CDP session that sees redirect hops (_guard_redirects)
        self.upload_dir = ""
        self.armed: tuple[str, str, float] | None = None  # (kind, domain, until)
        self.uploading_until = 0.0  # a file was just attached: the page may send it (XHR/fetch upload) until then
        # Files this session attached and saw taken: element key -> file name. A board may replace its file input
        # with the file's name once the upload is done (the newer Greenhouse board), so the input is not there to read.
        self.attached: dict[str, str] = {}
        self.takeover = False
        self.takeover_writes = False  # a person has the window: it sends what they send, like a normal browser
        self.origin_storage: dict[str, list[dict[str, Any]]] = {}  # a saved session's local storage, per origin
        self.blocked = 0
        self.navigation_blocked = 0
        self.responses: list[dict[str, Any]] = []
        self.response_seq = 0
        self.uploads_started = 0  # upload requests let out (see _upload)
        self.elements: dict[str, dict[str, Any]] = {}  # key -> description (+ node)
        self.deadline = 0.0

    # ───────────── session ─────────────

    async def open(self, args: dict[str, Any]) -> dict[str, Any]:
        from browser_use.browser.browser import Browser, BrowserConfig
        from browser_use.browser.context import BrowserContext, BrowserContextConfig
        from playwright.async_api import async_playwright

        if self.ctx is not None:
            raise Refused("ALREADY_OPEN", "A session is already open")
        self.sid = str(args["session"])
        self.key = bytes.fromhex(args["hmac_key"])
        self.allowed = {h.lower().strip(".") for h in args.get("allowed_hosts") or [] if h}
        self.allow_private = bool(args.get("allow_private"))
        self.redirect_domains = tuple(h.lower().strip(".") for h in args.get("redirect_domains") or [] if h)
        self.upload_dir = os.path.realpath(args["upload_dir"])
        self.deadline = time.monotonic() + float(args.get("session_seconds") or 300)
        headless = bool(args.get("headless", True))
        self.pw = await async_playwright().start()
        # Plain Chromium: no --remote-debugging-port, no anti-detection flags (unlike browser-use's own launcher).
        self.chromium = await self.pw.chromium.launch(headless=headless)
        await self._guard_redirects()  # before any context exists: no request leaves without it
        self.bu_browser = Browser(BrowserConfig(headless=headless))
        self.bu_browser.playwright, self.bu_browser.playwright_browser = self.pw, self.chromium
        config = BrowserContextConfig(allowed_domains=sorted(self.allowed) or None, highlight_elements=False,
                                      viewport_expansion=-1,  # every element of the page, not only the viewport
                                      wait_between_actions=0.3, minimum_wait_page_load_time=0.2,
                                      maximum_wait_page_load_time=8, permissions=[], no_viewport=not headless)
        self.ctx = BrowserContext(browser=self.bu_browser, config=config)
        session = await self.ctx.get_session()
        await session.context.route("**/*", self._guard)
        # Every non-GET response the site sends (method, status, host, path: never a body), the last 50: the evidence a
        # submission went through, whether the signed click or the person in the window sent it.
        session.context.on("response", self._on_response)
        state = args.get("storage_state") or {}
        cookies = state.get("cookies") or []
        if cookies:
            await session.context.add_cookies(cookies)
        for origin in state.get("origins") or []:  # restored on the first visit to that origin (goto)
            items = [i for i in origin.get("localStorage") or [] if isinstance(i, dict) and "name" in i]
            if items and origin.get("origin"):
                self.origin_storage[str(origin["origin"]).rstrip("/").lower()] = items
        return {"opened": True, "headless": headless}

    async def _guard(self, route: Any) -> None:
        request = route.request
        try:
            top = request.is_navigation_request() and request.frame.parent_frame is None and \
                request.url.startswith(("http:", "https:"))
            if await self._may_send(request.url, request.method, top=top, navigation=request.is_navigation_request(),
                                    upload=lambda: self._upload(request)):
                await route.continue_()
            else:
                await route.abort("blockedbyclient")
        except Exception:  # noqa: BLE001  a page closing mid-request is normal
            pass

    async def _may_send(self, url: str, method: str, *, top: bool, navigation: bool, upload: Any,
                        redirect: bool = False) -> bool:
        """The rules every request meets, its first hop (_guard) and each redirect hop (_on_paused) alike."""
        host = _host(url)
        person = self.takeover and self.takeover_writes
        if top and self.allowed and host not in self.allowed and not person and not (
                redirect and any(host == d or host.endswith("." + d) for d in self.redirect_domains)):
            self.navigation_blocked += 1
            return False
        # The page may not make the browser reach internal services: any request by name or literal address,
        # and every navigation (a frame's too) by the addresses its host resolves to.
        if not self.allow_private and (_private(host) or (navigation and await _resolves_private(host))):
            self.navigation_blocked += 1
            return False
        if method.upper() != "GET":
            armed = self.armed is not None and time.monotonic() < self.armed[2] and (
                self.armed[0] == "SUBMIT" or self.armed[1] == host)  # a submit lets the page send (captcha too)
            uploading = not (armed or person) and upload()
            if not (armed or person or uploading):
                self.blocked += 1
                return False
            if not redirect:  # a hop of an upload already counted is the same upload
                self.uploads_started += uploading
        return True

    async def _guard_redirects(self) -> None:
        """Hold every request of this browser at a browser-level CDP Fetch session, to judge redirect hops before they
        are sent: Playwright's routing is not called for them. At the browser level (not per page) it covers popups and
        service workers too, with no moment between a target's creation and its coverage. Everything else is released
        at once and still meets `_guard`. A failure here fails `open`: no session runs without it."""
        session = await self.chromium.new_browser_cdp_session()
        self.redirect_guard = session
        session.on("Fetch.requestPaused", lambda event: self._on_paused(session, event))
        await session.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})

    async def _on_paused(self, session: Any, event: dict[str, Any]) -> None:
        allow = True
        if event.get("redirectedRequestId"):
            try:
                allow = await self._hop_allowed(session, event)
            except Exception as exc:  # noqa: BLE001  a hop that cannot be judged is not sent
                allow = False
                log(f"redirect check failed: {type(exc).__name__}")
        try:
            if allow:
                await session.send("Fetch.continueRequest", {"requestId": event["requestId"]})
            else:
                await session.send("Fetch.failRequest", {"requestId": event["requestId"],
                                                         "errorReason": "BlockedByClient"})
        except Exception:  # noqa: BLE001  a page closing mid-request is normal
            pass

    async def _hop_allowed(self, session: Any, event: dict[str, Any]) -> bool:
        request = event["request"]
        url, method, kind = str(request["url"]), str(request["method"]), str(event.get("resourceType") or "")
        navigation = kind == "Document"
        top = False
        if navigation and url.startswith(("http:", "https:")):  # a page's target id is its top frame's id
            targets = (await session.send("Target.getTargets")).get("targetInfos") or []
            top = any(t.get("type") == "page" and t.get("targetId") == event.get("frameId") for t in targets)
        headers = {str(k).lower(): str(v) for k, v in (request.get("headers") or {}).items()}
        return await self._may_send(url, method, top=top, navigation=navigation, redirect=True,
                                    upload=lambda: self._upload_like(method, kind.lower(), headers.get("content-type")))

    def _upload(self, request: Any) -> bool:
        return self._upload_like(request.method, request.resource_type, request.headers.get("content-type"))

    def _upload_like(self, method: str, resource_type: str, content_type: str | None) -> bool:
        """The upload a file selection starts: an XHR/fetch PUT, or a multipart POST, shortly after `upload`."""
        if time.monotonic() > self.uploading_until or resource_type not in ("xhr", "fetch"):
            return False
        return method.upper() == "PUT" or (content_type or "").lower().startswith("multipart/form-data")

    def _on_response(self, response: Any) -> None:
        try:
            request = response.request
            if request.method.upper() != "GET":
                parsed = urlparse(response.url)
                self.response_seq += 1
                self.responses.append({"seq": self.response_seq, "method": request.method.upper(),
                                       "status": response.status,
                                       "host": (parsed.hostname or "").lower(), "path": parsed.path[:200],
                                       "upload": self._upload(request)})
                del self.responses[:-50]
        except Exception:  # noqa: BLE001
            pass

    async def _page(self) -> Any:
        if self.ctx is None:
            raise Refused("NOT_OPEN", "No session is open")
        if time.monotonic() > self.deadline:
            raise Refused("SESSION_EXPIRED", "The session's time ran out")
        return await self.ctx.get_agent_current_page()

    async def goto(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args["url"])
        host = _host(url)
        if not url.startswith(("http://", "https://")) or (self.allowed and host not in self.allowed):
            raise Refused("URL_NOT_ALLOWED", "That address is not on this application's allowed hosts")
        if not self.allow_private and await _resolves_private(host):
            raise Refused("URL_NOT_ALLOWED", "Private addresses are not allowed")
        page = await self._page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            if "ERR_BLOCKED_BY_CLIENT" in str(exc):  # a redirect the guard stopped, before its request was sent
                try:  # the browser's error page is still landing: the next command must not read the page mid-way
                    await page.wait_for_url(lambda now: now.startswith("chrome-error:"), timeout=3000)
                except Exception:  # noqa: BLE001
                    pass
                raise Refused("URL_NOT_ALLOWED", "That address redirects off this application's allowed hosts") from exc
            raise
        try:
            await page.wait_for_load_state("load", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        parsed = urlparse(page.url)
        items = self.origin_storage.pop(f"{parsed.scheme}://{parsed.netloc}".lower(), None)
        if items:  # a saved session's local storage, then the page again so it starts signed in
            await page.evaluate(RESTORE_STORAGE_JS, items)
            response = await page.reload(wait_until="domcontentloaded")
        return {"final_url": page.url, "status": response.status if response is not None else None,
                "title": await page.title()}

    # ───────────── reading ─────────────

    async def _elements(self, page: Any) -> dict[int, Any]:
        """The page's interactive elements (browser-use's DOM indexing), read without `BrowserContext.get_state`: that
        also takes a screenshot, which first waits for the page's `load` event with Playwright's 30 s default. A
        headful window often never fires it (a long-lived request of the page), so the state read failed although the
        form was there. Here the wait for `load` is bounded and a page still loading is read as it is."""
        from browser_use.dom.service import DomService

        try:
            await page.wait_for_load_state("load", timeout=LOAD_WAIT_MS)
        except Exception:  # noqa: BLE001  still loading: the form is usually there long before `load`
            pass
        content = await DomService(page).get_clickable_elements(focus_element=-1, viewport_expansion=-1,
                                                                 highlight_elements=False)
        return content.selector_map

    async def observe(self, args: dict[str, Any]) -> dict[str, Any]:
        page = await self._page()
        selector_map = await self._elements(page)
        elements, mapped = [], {}
        for index, node in list(selector_map.items())[:MAX_ELEMENTS]:
            handle = await self.ctx.get_locate_element(node)
            if handle is None:
                continue
            try:
                info = await handle.evaluate(DESCRIBE_JS)
            except Exception:  # noqa: BLE001  detached meanwhile
                continue
            key = _key(info)
            if key in mapped:
                continue
            info.update(key=key, index=index, submit_like=_submit_like(info))
            if info["tag"] == "input" and info["type"] == "radio":
                info["group_key"] = _group_key(info)
            mapped[key] = {**info, "node": node}
            public = {k: v for k, v in info.items() if k != "value" or info["type"] in ("radio", "checkbox")}
            elements.append(public)
        self.elements = mapped
        text = ""
        try:
            text = (await page.inner_text("body"))[:MAX_TEXT]
        except Exception:  # noqa: BLE001
            pass
        html = (await page.content())[:MAX_HTML] if args.get("html") else ""
        return {"url": page.url, "title": await page.title(), "elements": elements, "text": text, "html": html,
                "captcha": await self._captcha(page),
                "attached": {k: n for k, n in self.attached.items() if k not in mapped},
                "blocked_requests": self.blocked, "blocked_navigations": self.navigation_blocked}

    async def _captcha(self, page: Any) -> dict[str, Any]:
        """{present, solved, kind}: whether a challenge a person must answer is on the page (in any frame: an embedded
        board puts it in its own iframe), and whether every one of them has been answered, each on its own field."""
        found: list[dict[str, Any]] = []
        for frame in page.frames:
            try:
                found += await frame.evaluate(CAPTCHA_JS)
            except Exception:  # noqa: BLE001, S112  a frame navigating or gone
                continue
        if not found:
            return {"present": False, "solved": False, "kind": None}
        return {"present": True, "solved": all(bool(f.get("solved")) for f in found), "kind": str(found[0]["kind"])}

    def _element(self, key: str) -> dict[str, Any]:
        element = self.elements.get(str(key))
        if element is None:
            raise Refused("ELEMENT_GONE", "That element is not on the page any more; observe again")
        return element

    async def _handle(self, element: dict[str, Any]) -> Any:
        handle = await self.ctx.get_locate_element(element["node"])
        if handle is None:
            raise Refused("ELEMENT_GONE", "That element is not on the page any more; observe again")
        return handle

    async def read(self, args: dict[str, Any]) -> dict[str, Any]:
        if str(args["key"]) not in self.elements and str(args["key"]) in self.attached:
            return {"files": [self.attached[str(args["key"])]], "filled": True}  # the input became its file's name
        out = await self._read(args)
        out["filled"] = bool(out.get("present") or out.get("checked") or out.get("files")
                             or str(out.get("value") or "").strip())
        return out

    async def _read(self, args: dict[str, Any]) -> dict[str, Any]:
        element = self._element(args["key"])
        page = await self._page()
        kind = element["type"]
        if kind == "radio":
            name = element["name"].replace("\\", "\\\\").replace('"', '\\"')
            checked = page.locator(f'input[type="radio"][name="{name}"]:checked')
            value = await checked.first.get_attribute("value") if await checked.count() else ""
            return {"value": value or ""}
        handle = await self._handle(element)
        if kind == "checkbox":
            return {"checked": await handle.is_checked()}
        if kind == "file":
            return {"files": await handle.evaluate(FILE_NAMES_JS), "sizes": await handle.evaluate(FILE_SIZES_JS)}
        if kind == "password":
            return {"present": bool(await handle.input_value())}  # never the value
        if element.get("role") == "combobox":
            return {"value": await handle.evaluate(COMBO_VALUE_JS)}
        return {"value": await handle.input_value()}

    # ───────────── acting ─────────────

    async def fill(self, args: dict[str, Any]) -> dict[str, Any]:
        element = self._element(args["key"])
        if element["type"] == "password":
            raise Refused("PASSWORD_FIELD", "Password fields are filled only by login_fill")
        if element["tag"] not in ("input", "textarea") or element["type"] in ("file", "checkbox", "radio", "submit"):
            raise Refused("WRONG_KIND", "fill is for text inputs")
        await self.ctx._input_text_element_node(element["node"], str(args["text"]))
        return {"filled": True}

    async def select(self, args: dict[str, Any]) -> dict[str, Any]:
        element = self._element(args["key"])
        if element["tag"] != "select":
            raise Refused("WRONG_KIND", "select is for select elements")
        values = [o["value"] for o in element.get("options") or []]
        if str(args["value"]) not in values:
            raise Refused("NO_SUCH_OPTION", "That option is not one of the select's own options")
        await (await self._handle(element)).select_option(value=str(args["value"]))
        return {"selected": True}

    async def check(self, args: dict[str, Any]) -> dict[str, Any]:
        element = self._element(args["key"])
        page = await self._page()
        if element["type"] == "radio":
            name = element["name"].replace("\\", "\\\\").replace('"', '\\"')
            value = str(args.get("value") or "").replace("\\", "\\\\").replace('"', '\\"')
            option = page.locator(f'input[type="radio"][name="{name}"][value="{value}"]')
            if not await option.count():
                raise Refused("NO_SUCH_OPTION", "That option is not one of the group's own options")
            await option.first.check()
            return {"checked": True}
        if element["type"] != "checkbox":
            raise Refused("WRONG_KIND", "check is for checkboxes and radio options")
        await (await self._handle(element)).check()
        return {"checked": True}

    async def upload(self, args: dict[str, Any]) -> dict[str, Any]:
        element = self._element(args["key"])
        path = os.path.realpath(str(args["path"]))
        if element["type"] != "file":
            raise Refused("WRONG_KIND", "upload is for file inputs")
        if os.path.commonpath([path, self.upload_dir]) != self.upload_dir or not os.path.isfile(path):
            raise Refused("FILE_NOT_ALLOWED", "Only this run's own packet files can be uploaded")
        name = os.path.basename(path)
        handle = await self._handle(element)
        page = await self._page()
        before, started = self.response_seq, self.uploads_started
        begun = time.monotonic()
        self.uploading_until = begun + 30
        try:
            await handle.set_input_files(path)
            # The page sends the file as soon as it is chosen (Greenhouse: to its storage bucket). Wait for that upload
            # to start (a moment) and to be answered (up to 20 s); a page that uploads nothing goes on at once.
            while time.monotonic() - begun < 20:
                answered = sum(1 for r in self.responses if r.get("upload") and r["seq"] > before)
                if self.uploads_started > started and answered >= self.uploads_started - started:
                    break
                if self.uploads_started == started and time.monotonic() - begun > 1.5:
                    break
                await asyncio.sleep(0.2)
            await asyncio.sleep(0.3)  # the page shows what it did with the answer
        finally:
            self.uploading_until = 0.0
        sent = [r for r in self.responses if r.get("upload") and r["seq"] > before]
        failed = any(int(r.get("status") or 0) >= 400 for r in sent) or len(sent) < self.uploads_started - started
        try:
            names = await handle.evaluate(FILE_NAMES_JS)
        except Exception:  # noqa: BLE001  the input was replaced by the file's name
            names = []
        try:
            shown = name in await page.inner_text("body")
        except Exception:  # noqa: BLE001
            shown = False
        if failed or not (name in names or shown):
            raise Refused("UPLOAD_FAILED", "The site did not take the file")
        self.attached[element["key"]] = name
        return {"uploaded": True, "upload_requests": len(sent)}

    async def click(self, args: dict[str, Any]) -> dict[str, Any]:
        element = self._element(args["key"])
        if element.get("submit_like"):
            raise Refused("SUBMIT_REFUSED", "This control may submit the form; only a signed submit can click it")
        if element["type"] in ("password", "file"):
            raise Refused("WRONG_KIND", "click is for buttons and links")
        await self.ctx._click_element_node(element["node"])
        page = await self._page()
        return {"url": page.url}

    async def choose(self, args: dict[str, Any]) -> dict[str, Any]:
        """Set a dropdown widget (role=combobox) to the option whose text is exactly `text` (ignoring case and
        surrounding spaces): open it, type the text to filter, click that option. Anything else (no option, only
        partial matches, several matches) closes the list and refuses: a person's answer is never mapped to a nearby
        option."""
        element = self._element(args["key"])
        if element.get("role") != "combobox" or element.get("submit_like"):
            raise Refused("WRONG_KIND", "choose is for dropdown widgets (role=combobox)")
        wanted = " ".join(str(args.get("text") or "").split()).lower()
        if not wanted:
            raise Refused("NO_SUCH_OPTION", "No answer to choose")
        page = await self._page()
        handle = await self._handle(element)
        await handle.click(timeout=5000)  # the combobox itself: opens its list, never submits
        await handle.fill(str(args["text"]), timeout=5000)
        try:
            await page.wait_for_selector('[role="option"]', state="visible", timeout=4000)
        except Exception:  # noqa: BLE001  no list opened
            pass
        texts = [" ".join(t.split()).lower() for t in await page.evaluate(OPTION_TEXTS_JS)]
        exact = [i for i, t in enumerate(texts) if t == wanted]
        # Exactly one option with exactly this text, never the only option that merely contains it ("Yes" must not
        # become "Yes, I will need sponsorship").
        index = exact[0] if len(exact) == 1 else None
        if index is None:
            await page.keyboard.press("Escape")
            raise Refused("NO_SUCH_OPTION", f"{len(texts)} options, none exactly matching the answer")
        await page.locator('[role="option"]:visible').nth(index).click(timeout=5000)
        return {"chosen": True, "value": await handle.evaluate(COMBO_VALUE_JS)}

    async def point(self, args: dict[str, Any]) -> dict[str, Any]:
        """Show the person what needs them: outline each keyed element, one banner with `message`, the first element
        scrolled into view and focused, the window in front. Reads and marks only; never clicks or types."""
        page = await self._page()
        await page.evaluate(UNMARK_JS)
        marked, first = 0, None
        for key in args.get("keys") or []:
            element = self.elements.get(str(key))
            if element is None:
                continue
            handle = await self.ctx.get_locate_element(element["node"])
            if handle is None:
                continue
            await handle.evaluate(MARK_JS, str(args.get("note") or "Job Hunt OS needs you here"))
            first = first or handle
            marked += 1
        await page.evaluate(BANNER_JS, str(args.get("message") or ""))
        if first is not None:
            try:
                await first.scroll_into_view_if_needed(timeout=3000)
                await first.focus()
            except Exception:  # noqa: BLE001
                pass
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        return {"marked": marked}

    async def unpoint(self, args: dict[str, Any]) -> dict[str, Any]:
        page = await self._page()
        await page.evaluate(UNMARK_JS)
        return {"cleared": True}

    async def scroll(self, args: dict[str, Any]) -> dict[str, Any]:
        page = await self._page()
        await page.mouse.wheel(0, 700 if args.get("direction", "down") == "down" else -700)
        return {"scrolled": True}

    # ───────────── signed actions ─────────────

    def _verify(self, token: str, kind: str, key: str, domain: str) -> None:
        try:
            body, signature = str(token).rsplit(".", 1)
            expected = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()
            claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except Exception as exc:
            raise Refused("BAD_TOKEN", "The authorization token is not valid") from exc
        if not hmac.compare_digest(expected, signature):
            raise Refused("BAD_TOKEN", "The authorization token is not valid")
        if claims.get("kind") != kind or claims.get("sid") != self.sid or claims.get("key") != key \
                or claims.get("domain") != domain or float(claims.get("exp") or 0) < time.time():
            raise Refused("BAD_TOKEN", "The authorization token does not authorize this action")

    async def login_fill(self, args: dict[str, Any]) -> dict[str, Any]:
        element = self._element(args["key"])
        page = await self._page()
        domain = str(args["domain"]).lower()
        if _host(page.url) != domain or not page.url.startswith("https://") and not self.allow_private:
            raise Refused("WRONG_DOMAIN", "The login is filled only on its own domain, over HTTPS")
        usable = element["type"] == "password" or (element["type"] in ("email", "text") and (
            element.get("autocomplete") in ("username", "email") or
            any(w in (element.get("label") or "").lower() for w in ("email", "user"))))
        if not usable or element["tag"] != "input":
            raise Refused("WRONG_KIND", "login_fill is for the username and password fields")
        await (await self._handle(element)).fill(str(args["secret"]))
        return {"filled": True}

    async def _signed_click(self, args: dict[str, Any], kind: str) -> dict[str, Any]:
        element = self._element(args["key"])
        page = await self._page()
        domain = _host(page.url)
        self._verify(args.get("token") or "", kind, element["key"], domain)
        if kind == "SUBMIT" and not element.get("submit_like"):
            raise Refused("NOT_A_SUBMIT", "The signed element is not the form's submit control")
        if kind == "LOGIN" and not await (await self._handle(element)).evaluate(LOGIN_FORM_JS):
            raise Refused("NOT_A_LOGIN", "The signed element is not in the form that holds the password field")
        self.responses = []
        self.armed = (kind, domain, time.monotonic() + 30)
        started = time.monotonic()
        try:
            await self.ctx._click_element_node(element["node"])
            for state in ("load", "networkidle"):
                try:
                    await page.wait_for_load_state(state, timeout=15000)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self.armed = None
        return {"clicked": True, "seconds": round(time.monotonic() - started, 2)}

    async def submit(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self._signed_click(args, "SUBMIT")

    async def login_submit(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self._signed_click(args, "LOGIN")

    async def challenge(self, args: dict[str, Any]) -> dict[str, Any]:
        """Only whether a CAPTCHA is on the page and answered (booleans): cheap enough to ask every few seconds while
        a person has the window, and it leaves the element map alone."""
        return await self._captcha(await self._page())

    async def evidence(self, args: dict[str, Any]) -> dict[str, Any]:
        page = await self._page()
        inputs = 0
        try:
            inputs = await page.evaluate(FORM_INPUTS_JS)
        except Exception:  # noqa: BLE001
            pass
        text = ""
        try:
            text = (await page.inner_text("body"))[:3000]
        except Exception:  # noqa: BLE001
            pass
        return {"url": page.url, "title": await page.title(), "responses": list(self.responses),
                "form_inputs": inputs, "text": text, "captcha": await self._captcha(page)}

    async def set_takeover(self, args: dict[str, Any]) -> dict[str, Any]:
        self.takeover = bool(args.get("on"))
        # A person has the window: with `writes` it sends what they send (any public host), like a normal browser.
        self.takeover_writes = self.takeover and bool(args.get("writes"))
        if self.takeover:
            self.deadline = time.monotonic() + float(args.get("seconds") or 900)
        elif args.get("seconds"):  # kept for the same run's next step (no takeover): only its lifetime grows
            self.deadline = max(self.deadline, time.monotonic() + float(args["seconds"]))
        return {"takeover": self.takeover, "writes": self.takeover_writes}

    async def storage_state(self, args: dict[str, Any]) -> dict[str, Any]:
        session = await self.ctx.get_session()
        return {"state": await session.context.storage_state()}

    async def health(self, args: dict[str, Any]) -> dict[str, Any]:
        import browser_use  # noqa: F401
        import playwright

        version = "unknown"
        try:
            from importlib.metadata import version as dist_version

            version = dist_version("browser-use")
        except Exception:  # noqa: BLE001
            pass
        return {"protocol": PROTOCOL, "browser_use": version,
                "playwright": getattr(playwright, "__version__", "unknown"), "python": sys.version.split()[0]}

    async def close(self, args: dict[str, Any]) -> dict[str, Any]:
        for closer in (lambda: self.ctx.close() if self.ctx else None,
                       lambda: self.chromium.close() if self.chromium else None,
                       lambda: self.pw.stop() if self.pw else None):
            try:
                result = closer()
                if result is not None:
                    await result
            except Exception:  # noqa: BLE001
                pass
        self.ctx = self.chromium = self.pw = self.redirect_guard = None
        return {"closed": True}


COMMANDS = ("health", "open", "goto", "observe", "read", "fill", "select", "check", "upload", "click", "choose",
            "point", "unpoint", "scroll",
            "login_fill", "login_submit", "submit", "evidence", "challenge", "set_takeover", "storage_state", "close")


def _reply_channel() -> Any:
    """stdout carries replies and nothing else. browser-use logs to sys.stdout and some of its helpers print(); one
    stray line there broke the session in Job Hunt OS's client. Replies get a private copy of the stdout pipe, and
    everything else written to stdout (Python or native) goes to stderr."""
    replies = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    return replies


async def main() -> None:
    replies = _reply_channel()
    runner = Runner()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=4 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while True:
        line = await reader.readline()
        if not line:
            break
        try:
            message = json.loads(line)
            ident, cmd, args = message.get("id"), message.get("cmd"), message.get("args") or {}
        except Exception:  # noqa: BLE001
            continue
        try:
            if cmd not in COMMANDS:
                raise Refused("UNKNOWN_COMMAND", "Unknown command")
            result = await getattr(runner, cmd)(args)
            reply = {"id": ident, "ok": True, "result": result}
        except Refused as exc:
            reply = {"id": ident, "ok": False, "error": {"code": exc.code, "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001
            log(f"{cmd} failed: {type(exc).__name__}")
            reply = {"id": ident, "ok": False, "error": {"code": "BROWSER_ERROR", "message": type(exc).__name__}}
        replies.write(json.dumps(reply, default=str) + "\n")
        replies.flush()
        if cmd == "close":
            break
    await runner.close({})


if __name__ == "__main__":
    asyncio.run(main())
