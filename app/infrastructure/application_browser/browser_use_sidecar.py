"""The browser-use backend: one sidecar process (sidecars/browser_use_runner) per application execution.

    SidecarManager.start(user_id, task_id, …)  → SidecarSession: a child process running the runner with the
                                                  browser-use Web UI's own Python (BROWSER_USE_PYTHON)
    session.call("observe") / "fill" / …        → one bounded command, JSON lines over stdin/stdout, with a timeout
    manager.park(session, minutes)              → a headful session kept for a person to take over (OTP, login)
    manager.get(user_id, task_id)               → that same session, only for its own account and execution

Why a separate process (ADR-031): browser-use 0.1.48 needs langchain-core 0.3 and Job Hunt OS pins 1.6, and its own
Agent would call models outside the LLM gateway. The runner uses browser-use's DOM indexing and element actions only;
Job Hunt OS decides every action. Isolation comes from the process: its own Chromium, its own temporary profile, no
port, an environment with nothing but PATH and HOME, and a session that is never shared between executions or
accounts. Every command is recorded in Monitoring → External APIs (provider BROWSER_USE, service sidecar), metadata
only. A signed action (`sign_action`) is the only way the runner clicks a submit or login control; only
app.services.submission and app.services.application_backends sign one (tests/test_no_automated_submit.py).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.settings import get_settings

log = logging.getLogger("jobhunt.browser_use_sidecar")
RUNNER = Path(__file__).resolve().parents[3] / "sidecars" / "browser_use_runner" / "runner.py"
PROTOCOL = 1
EXPECTED_BROWSER_USE = "0.1.48"
SIGNED_SECONDS = 60



def _reply(line: bytes) -> dict[str, Any] | None:
    """One protocol line, or None for anything else on the pipe (a library's log line from an older runner): skipped,
    never mistaken for the session failing. Only the line's length is logged, never its text (it may hold page text)."""
    try:
        reply = json.loads(line)
    except ValueError:
        reply = None
    if isinstance(reply, dict):
        return reply
    if line.strip():
        log.warning("browser-use runner wrote a line that is not a reply (%d bytes); skipped", len(line))
    return None

class SidecarUnavailable(RuntimeError):
    """The browser-use backend is off, not installed, or its process died."""


class SidecarRefused(RuntimeError):
    """The runner refused a command (its own limits) or the page could not do it. `code` says which."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def configured() -> bool:
    settings = get_settings()
    python = (settings.browser_use_python or "").strip()
    return bool(settings.browser_use_webui_enabled and python and Path(python).exists() and RUNNER.exists())


def _environment() -> dict[str, str]:
    """Nothing of Job Hunt OS's own environment (keys, database URL) reaches the runner."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
           "ANONYMIZED_TELEMETRY": "false", "PYTHONUNBUFFERED": "1", "PYTHONNOUSERSITE": "1"}
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        env["PLAYWRIGHT_BROWSERS_PATH"] = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    return env


@dataclass
class SidecarSession:
    user_id: int
    task_id: int
    job_id: int | None
    process: Any
    session_id: str = field(default_factory=lambda: secrets.token_hex(16))
    hmac_key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    headless: bool = True
    parked_until: float | None = None
    domain_for_login: str | None = None
    # Kept with the person's work in it (a takeover) or the form read for the next step (waiting for an answer): the
    # run's next step observes this page where it is instead of reloading the apply URL.
    resume_in_place: bool = False
    _next: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def call(self, cmd: str, *, timeout: float | None = None, record: bool = True, **args: Any) -> dict[str, Any]:
        """Send one command and wait for its answer. Raises SidecarRefused (the runner said no) or
        SidecarUnavailable (the process is gone, or it did not answer in time: it is killed)."""
        if not self.alive:
            raise SidecarUnavailable("The browser-use session has ended")
        limit = timeout or get_settings().browser_use_command_timeout_seconds
        started, clock = _utcnow(), time.monotonic()
        error: BaseException | None = None
        try:
            async with self._lock:
                self._next += 1
                ident = self._next
                self.process.stdin.write((json.dumps({"id": ident, "cmd": cmd, "args": args}) + "\n").encode())
                await self.process.stdin.drain()
                while True:
                    line = await asyncio.wait_for(self.process.stdout.readline(), timeout=limit)
                    if not line:
                        raise SidecarUnavailable("The browser-use session ended unexpectedly")
                    reply = _reply(line)
                    if reply is not None and reply.get("id") == ident:
                        break
            if not reply.get("ok"):
                problem = reply.get("error") or {}
                raise SidecarRefused(str(problem.get("code") or "BROWSER_ERROR"), str(problem.get("message") or ""))
            return reply.get("result") or {}
        except TimeoutError as exc:
            error = exc
            await self.kill()
            raise SidecarUnavailable(f"The browser-use session did not answer {cmd} in {limit:.0f}s") from exc
        except (SidecarRefused, SidecarUnavailable) as exc:
            error = exc
            raise
        except (OSError, ValueError) as exc:
            error = exc
            await self.kill()
            raise SidecarUnavailable(f"The browser-use session failed ({type(exc).__name__})") from exc
        finally:
            if record:
                await _record(self, cmd, started, clock, error)

    async def kill(self) -> None:
        if self.alive:
            with contextlib.suppress(Exception):
                self.process.kill()
                await asyncio.wait_for(self.process.wait(), timeout=5)

    async def close(self) -> None:
        if self.alive:
            with contextlib.suppress(Exception):
                await self.call("close", timeout=15, record=False)
            await self.kill()


def sign_action(session: SidecarSession, kind: str, key: str, domain: str, seconds: int = SIGNED_SECONDS) -> str:
    """A token the runner accepts for one SUBMIT or LOGIN click: this session, this element, this domain, briefly.
    Only app.services.submission (SUBMIT, after the pre-submit validator and the claim) and
    app.services.application_backends (LOGIN, on an allowlisted login domain) call it."""
    if kind not in ("SUBMIT", "LOGIN"):
        raise ValueError("kind must be SUBMIT or LOGIN")
    body = base64.urlsafe_b64encode(json.dumps({"kind": kind, "sid": session.session_id, "key": key,
                                                "domain": domain, "exp": time.time() + seconds}).encode())
    body_text = body.decode().rstrip("=")
    return f"{body_text}.{hmac.new(session.hmac_key, body_text.encode(), hashlib.sha256).hexdigest()}"


async def _record(session: SidecarSession, cmd: str, started: datetime, clock: float,
                  error: BaseException | None) -> None:
    from app.services import external_api

    call = external_api.ExternalCall(provider="BROWSER_USE", service="sidecar", operation=cmd,
                                     user_id=session.user_id, job_id=session.job_id,
                                     metadata={"task_id": session.task_id, "headless": session.headless})
    kwargs: dict[str, Any] = {"started_at": started, "completed_at": _utcnow(), "method": "PIPE",
                              "url": f"sidecar://browser-use/{cmd}"}
    if error is not None:
        kwargs["error"] = error
        if isinstance(error, SidecarRefused):
            call.note(refusal=error.code)
    with contextlib.suppress(Exception):  # telemetry never changes an outcome
        await asyncio.to_thread(external_api.record, call, **kwargs)


class SidecarManager:
    """The process's browser-use sessions, keyed by (account, execution). Lives on the scheduler's event loop."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[int, int], SidecarSession] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    def run_threadsafe(self, coro: Any, timeout: float = 60) -> Any:
        """Run a coroutine on the loop the sessions live on, from another thread (an API request)."""
        if self.loop is None or self.loop.is_closed():
            coro.close()
            raise SidecarUnavailable("No browser-use session is running in this process")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:  # already on that loop (never block it): run it in the background
            return self.loop.create_task(coro)
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def available(self) -> bool:
        return configured()

    async def start(self, *, user_id: int, task_id: int, job_id: int | None, allowed_hosts: list[str],
                    upload_dir: str, storage_state: dict[str, Any] | None = None, headless: bool | None = None,
                    allow_private: bool = False, redirect_domains: list[str] | None = None) -> SidecarSession:
        if not configured():
            raise SidecarUnavailable("The browser-use backend is not configured (BROWSER_USE_WEBUI_ENABLED, "
                                     "BROWSER_USE_PYTHON)")
        await self.close(user_id, task_id)
        self.loop = asyncio.get_running_loop()
        settings = get_settings()
        process = await asyncio.create_subprocess_exec(
            settings.browser_use_python, "-I", str(RUNNER), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=_environment(),
            cwd=upload_dir, limit=8 * 1024 * 1024)
        headless = settings.browser_use_headless if headless is None else headless
        session = SidecarSession(user_id=user_id, task_id=task_id, job_id=job_id, process=process, headless=headless)
        try:
            health = await session.call("health", timeout=60)
            if health.get("protocol") != PROTOCOL:
                raise SidecarUnavailable("The browser-use runner speaks another protocol version")
            await session.call("open", timeout=90, session=session.session_id, hmac_key=session.hmac_key.hex(),
                               allowed_hosts=allowed_hosts, redirect_domains=redirect_domains or [],
                               allow_private=allow_private, upload_dir=upload_dir,
                               headless=headless, storage_state=storage_state or {},
                               session_seconds=settings.browser_use_timeout_seconds)
        except BaseException:
            await session.kill()
            raise
        self._sessions[(user_id, task_id)] = session
        return session

    def get(self, user_id: int, task_id: int) -> SidecarSession | None:
        """The execution's own live session, or None. Another account's key never matches: the account is part of
        the key, and callers pass the account of the task they hold."""
        session = self._sessions.get((user_id, task_id))
        if session is None or not session.alive:
            self._sessions.pop((user_id, task_id), None)
            return None
        if session.parked_until is not None and session.parked_until < time.monotonic():
            return None
        return session

    async def park(self, session: SidecarSession, minutes: int) -> bool:
        """Hand a headful session to the person for `minutes`: it then behaves like a normal browser (it sends what they
        send, uploads and their own submission included). Headless sessions cannot be taken over and are closed
        instead."""
        if session.headless or not session.alive:
            await self.close(session.user_id, session.task_id)
            return False
        session.parked_until = time.monotonic() + minutes * 60
        await session.call("set_takeover", on=True, writes=True, seconds=minutes * 60)
        return True

    async def hold(self, session: SidecarSession, minutes: int, *, extend: bool = False) -> None:
        """Keep a session (signed in, the form revealed) for the same execution's next step, for at most `minutes`;
        nobody can act in it meanwhile (no takeover), and the reaper closes it after. `extend` also lengthens the
        runner's own session time to match (a run waiting for an answer in the Knowledge bank)."""
        if session.alive:
            session.parked_until = time.monotonic() + minutes * 60
            if extend:
                await session.call("set_takeover", on=False, seconds=minutes * 60)

    async def unpark(self, session: SidecarSession) -> None:
        session.parked_until = None
        await session.call("set_takeover", on=False)  # the person's turn is over: non-GET blocked again

    async def close(self, user_id: int, task_id: int) -> None:
        session = self._sessions.pop((user_id, task_id), None)
        if session is not None:
            await session.close()

    async def reap(self) -> int:
        """Close parked sessions whose takeover time ran out, and dead ones."""
        now, closed = time.monotonic(), 0
        for key, session in list(self._sessions.items()):
            if not session.alive or (session.parked_until is not None and session.parked_until < now):
                await self.close(*key)
                closed += 1
        return closed

    def parked(self, user_id: int) -> list[int]:
        now = time.monotonic()
        return [task for (user, task), s in self._sessions.items()
                if user == user_id and s.parked_until is not None and s.parked_until >= now and s.alive]

    def handed_over(self) -> list[tuple[int, int]]:
        """Every (account, execution) whose window a person has now (parked, alive, in its time)."""
        now = time.monotonic()
        return [key for key, s in list(self._sessions.items())
                if s.parked_until is not None and s.parked_until >= now and s.alive and not s.headless]


_manager: SidecarManager | None = None


def get_sidecars() -> SidecarManager:
    global _manager
    if _manager is None:
        _manager = SidecarManager()
    return _manager


def set_sidecars(manager: SidecarManager | None) -> None:
    """Replace the process's manager (tests); None goes back to the real one."""
    global _manager
    _manager = manager


@dataclass
class RunnerPage:
    url: str
    final_url: str
    status: int | None
    title: str
    html: str


async def read_page(url: str, *, user_id: int, job_id: int | None, allow_private: bool = False,
                    headless: bool = False) -> RunnerPage:
    """Read one public page through a runner of its own, for JD recovery: no element is touched, nothing but GET leaves
    the page (the runner's guard), and navigation stays on the URL's own host.

    Deliberately not a SidecarManager session: those are keyed by application execution, and starting one closes the
    session already under that key, which may be a person's window. Nothing is shared with an application run: its own
    process, Chromium and temporary profile, an empty folder, no saved login. Closed before this returns.
    Raises SidecarUnavailable (off, not installed, died, too slow) or SidecarRefused (the runner or the page said no)."""
    if not configured():
        raise SidecarUnavailable("The browser-use backend is not configured (BROWSER_USE_WEBUI_ENABLED, "
                                 "BROWSER_USE_PYTHON)")
    settings = get_settings()
    host = (urlparse(url).hostname or "").lower().strip(".")
    with tempfile.TemporaryDirectory(prefix="jobhunt-page-") as folder:
        process = await asyncio.create_subprocess_exec(
            settings.browser_use_python, "-I", str(RUNNER), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=_environment(), cwd=folder,
            limit=8 * 1024 * 1024)
        session = SidecarSession(user_id=user_id, task_id=0, job_id=job_id, process=process, headless=headless)
        try:
            health_ = await session.call("health", timeout=60)
            if health_.get("protocol") != PROTOCOL:
                raise SidecarUnavailable("The browser-use runner speaks another protocol version")
            await session.call("open", timeout=90, session=session.session_id, hmac_key=session.hmac_key.hex(),
                               allowed_hosts=[host], allow_private=allow_private, upload_dir=folder, headless=headless,
                               storage_state={}, session_seconds=settings.browser_use_timeout_seconds)
            loaded = await session.call("goto", url=url)
            page = await session.call("page_html")
            final_url = str(page.get("url") or loaded.get("final_url") or url)
            # The runner's guard does not see an HTTP redirect (Playwright does not route one), so a page that ended on
            # another host is caught here: its content is not used. The redirect's own request has already been made.
            if (urlparse(final_url).hostname or "").lower().strip(".") != host:
                raise SidecarRefused("URL_NOT_ALLOWED", "The page redirected to another host")
            return RunnerPage(url=url, final_url=final_url, status=loaded.get("status"),
                              title=str(page.get("title") or ""), html=str(page.get("html") or ""))
        finally:
            await session.close()


async def health() -> dict[str, Any]:
    """Start the runner once and ask what it is (browser-use and Python versions); never opens a browser."""
    if not configured():
        return {"configured": False}
    settings = get_settings()
    process = await asyncio.create_subprocess_exec(
        settings.browser_use_python, "-I", str(RUNNER), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=_environment())
    session = SidecarSession(user_id=0, task_id=0, job_id=None, process=process)
    try:
        info = await session.call("health", timeout=60, record=False)
        return {"configured": True, **info, "expected_browser_use": EXPECTED_BROWSER_USE,
                "compatible": info.get("protocol") == PROTOCOL and info.get("browser_use") == EXPECTED_BROWSER_USE}
    except (SidecarUnavailable, SidecarRefused) as exc:
        return {"configured": True, "compatible": False, "error": str(exc)[:200]}
    finally:
        await session.kill()
