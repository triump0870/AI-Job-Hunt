"""The one way Job Hunt OS talks to a third party — and therefore the one place it is measured.

    business service
      → external_client(...) / external_operation(...) / @track_external_call(...)
      → the third-party API
      → ExternalAPIRequestLog + SystemLogEntry written automatically
      → Monitoring

Nothing a service writes turns telemetry on: the HTTP client *is* the instrumentation, so an integration added
next year is measured the day it is written. Use `external_client` for anything HTTP, and `external_operation`
(or the decorator) only where a vendor SDK owns its own transport and cannot be handed our client.

What is recorded: provider, service and operation, host and path, method, timing, HTTP status, byte sizes,
attempt and retry count, rate-limit headers, a failure category, and provider-reported usage or cost when the
provider returns it. What is never recorded: credentials, headers, cookies, or request and response bodies —
unless EXTERNAL_API_DEBUG_PAYLOADS is on, and then only redacted, capped extracts.

Every failure in here is swallowed: telemetry must never turn a working API call into a failed one.
"""
from __future__ import annotations

import inspect
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import TYPE_CHECKING, Any

import httpx

from app.services import backoff, token_pacing, tracing
from app.services.redaction import redact_body, redact_headers, redact_text, redact_url, redact_value
from app.settings import get_settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger("jobhunt.external_api")

UA = "JobHuntOS/0.2 (+personal-job-search-tool)"
MAX_PATH = 500
MAX_METADATA_KEYS = 40
# Response headers providers use for their rate limits, most specific first.
REMAINING_HEADERS = ("x-ratelimit-remaining-requests", "x-ratelimit-remaining", "ratelimit-remaining",
                     "x-rate-limit-remaining", "anthropic-ratelimit-requests-remaining")
RESET_HEADERS = ("x-ratelimit-reset-requests", "x-ratelimit-reset", "ratelimit-reset", "x-rate-limit-reset",
                 "anthropic-ratelimit-requests-reset", "retry-after")
# The LiteLLM proxy forwards every provider header with this prefix (`llm_provider-retry-after`). On a success the
# OpenAI-named `x-ratelimit-*` headers also arrive bare; on a refusal only the remaining/limit counters do, so a 429's
# Retry-After and resets are only ever in the prefixed form (litellm get_response_headers / process_response_headers).
FORWARDED = "llm_provider-"
# Groq and OpenAI split a limit into requests and tokens. Tokens are always per minute; requests are per minute at
# OpenAI but per DAY at Groq, so a Groq response's request counter is never the figure that runs out first.
TOKEN_LIMIT, TOKEN_REMAINING, TOKEN_RESET = ("x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens",
                                             "x-ratelimit-reset-tokens")
REQUEST_LIMIT, REQUEST_REMAINING, REQUEST_RESET = ("x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
                                                   "x-ratelimit-reset-requests")
# "7.66s", "2m59.56s", "1h2m", "120ms" (Groq, OpenAI); a plain number is seconds.
_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ───────────── what a call is ─────────────

@dataclass
class ExternalCall:
    """The identity of one logical call, and the facts collected while it runs.

    A service names the provider, service and operation; everything else is filled in by the layer itself or,
    for SDK calls, reported afterwards through `record_usage` / `note`.
    """

    provider: str
    service: str = ""
    operation: str = ""
    user_id: int | None = None
    attempt: int = 1  # 1-based: retry_count is attempt - 1
    workflow_type: str | None = None
    workflow_entity_id: int | None = None
    job_id: int | None = None
    application_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    estimated_cost: Decimal | None = None
    cost_currency: str | None = None
    cost_source: str = "unknown"
    http_status_code: int | None = None
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # The redacted request and response, only when someone asked for them (capture_limit): Monitoring shows them.
    payload: dict[str, Any] | None = None

    def note(self, **metadata: Any) -> ExternalCall:
        """Add safe, provider-specific facts (actor id, run id, dataset size…) to this call's telemetry."""
        self.metadata.update({k: v for k, v in metadata.items() if v is not None})
        return self

    def record_usage(self, *, cost: float | Decimal | None = None, currency: str | None = "USD",
                     source: str = "provider_reported") -> ExternalCall:
        """Record what the provider said this call cost. Never call it with a guess: unknown cost stays NULL."""
        if cost is None:
            return self
        try:
            self.estimated_cost = Decimal(str(cost))
        except (InvalidOperation, ValueError):
            return self
        self.cost_currency, self.cost_source = (currency or "USD")[:3], source
        return self


class RateLimitedCooldown(httpx.HTTPError):
    """The call was not made: this account is still backing off from the provider's last 429.

    It is an httpx.HTTPError so that every caller which already handles a transport failure handles this too —
    a call that was deliberately not made should look like a call that did not get through.
    """

    def __init__(self, provider: str, retry_after_seconds: int, reason: str | None = None, *, paced: bool = False):
        # paced: held by token pacing (app.services.token_pacing) — nothing was sent, and the wait is the token reset
        self.provider, self.retry_after_seconds, self.paced = provider, retry_after_seconds, paced
        super().__init__(reason or f"{provider} is rate limiting this account; "
                                   f"not calling again for {retry_after_seconds}s")


def _classify_exception(exc: BaseException) -> str:
    name = type(exc).__name__
    text = str(exc).lower()
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)) or "timeout" in name.lower():
        return "TIMEOUT"
    if isinstance(exc, httpx.ConnectError) or "connect" in name.lower():
        return "DNS" if ("getaddrinfo" in text or "name or service not known" in text
                         or "nodename nor servname" in text or "name resolution" in text) else "CONNECTION"
    if isinstance(exc, RateLimitedCooldown):
        return "RATE_LIMIT"
    if isinstance(exc, (httpx.NetworkError, httpx.TransportError)):
        return "CONNECTION"
    if isinstance(exc, (httpx.DecodingError, ValueError)):
        return "INVALID_RESPONSE"
    if isinstance(exc, httpx.HTTPStatusError):
        return _classify_status(exc.response.status_code) or "OTHER"
    return "SDK_ERROR"


def owner_of(call: ExternalCall) -> int | None:
    """The account a call belongs to: what the caller said, else whatever the trace knows."""
    if call.user_id is not None:
        return call.user_id
    ctx = tracing.current()
    return ctx.user_id if ctx else None


def _classify_status(code: int | None) -> str | None:
    if code is None or code < 400:
        return None
    return "RATE_LIMIT" if code == 429 else "CLIENT_ERROR" if code < 500 else "SERVER_ERROR"


def _header_int(headers: Any, names: Iterable[str]) -> int | None:
    for name in names:
        raw = headers.get(name) if headers is not None else None
        if raw is None:
            continue
        try:
            return int(float(str(raw).strip()))
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def _header(headers: Any, name: str) -> Any:
    """A header as the provider sent it, or as the LiteLLM proxy forwarded it (`llm_provider-<name>`)."""
    if headers is None:
        return None
    value = headers.get(name)
    return value if value is not None else headers.get(FORWARDED + name)


def duration_seconds(raw: Any) -> float | None:
    """A reset written as a duration ("7.66s", "2m59.56s", "1h2m", "120ms") or plain seconds; None when unreadable."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    match = _DURATION.fullmatch(text)
    if not text or not match or not any(match.groups()):
        return None
    hours, minutes, seconds, millis = (float(part) if part else 0.0 for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def _count(headers: Any, name: str) -> int | None:
    raw = _header(headers, name)
    try:
        return int(float(str(raw).strip())) if raw is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def is_groq(headers: Any, metadata: dict[str, Any] | None = None) -> bool:
    """Whether the answer came from Groq: its own `x-groq-*` headers (forwarded or not), the deployment LiteLLM
    reports, or a `groq/` model name. Only then is the request counter known to be per day."""
    try:
        if headers is not None and any("x-groq-" in str(name).lower() for name in headers):
            return True
        base = str(headers.get("x-litellm-model-api-base") or "") if headers is not None else ""
        names = [headers.get("x-litellm-model-name") if headers is not None else None, (metadata or {}).get("model")]
        return "groq.com" in base.lower() or any(isinstance(n, str) and n.lower().startswith("groq/") for n in names)
    except Exception:  # noqa: BLE001  telemetry only
        return False


def rate_window(headers: Any, metadata: dict[str, Any] | None = None) -> token_pacing.Window:
    """The token and request limits a response reported, each with its own window. Never raises."""
    try:
        groq = is_groq(headers, metadata)
        return token_pacing.Window(
            tokens_limit=_count(headers, TOKEN_LIMIT), tokens_remaining=_count(headers, TOKEN_REMAINING),
            tokens_reset_seconds=duration_seconds(_header(headers, TOKEN_RESET)),
            requests_limit=_count(headers, REQUEST_LIMIT), requests_remaining=_count(headers, REQUEST_REMAINING),
            requests_reset_seconds=duration_seconds(_header(headers, REQUEST_RESET)),
            requests_window="day" if groq else None)
    except Exception:  # noqa: BLE001
        return token_pacing.Window()


def _window_note(window: token_pacing.Window) -> dict[str, Any] | None:
    """The limits for the request row's metadata, labelled with their windows; None when the provider sent none."""
    note = {"tokens_per_minute_limit": window.tokens_limit, "tokens_per_minute_remaining": window.tokens_remaining,
            "tokens_reset_seconds": window.tokens_reset_seconds, "requests_limit": window.requests_limit,
            "requests_remaining": window.requests_remaining, "requests_reset_seconds": window.requests_reset_seconds,
            "requests_window": window.requests_window}
    note = {k: v for k, v in note.items() if v is not None}
    return note or None


def _duration_reset(headers: Any, status: int | None) -> float | None:
    """Seconds to a reset written as a duration, or forwarded by LiteLLM. A 429's Retry-After comes first, then —
    when the tokens ran out, or for Groq, whose request reset is the daily window — the token reset."""
    if status == 429:
        after = duration_seconds(_header(headers, "retry-after"))
        if after is not None:
            return after
    if (status == 429 and _count(headers, TOKEN_REMAINING) == 0) or is_groq(headers):
        tokens = duration_seconds(_header(headers, TOKEN_RESET))
        if tokens is not None:
            return tokens
    return next((seconds for seconds in (duration_seconds(_header(headers, name)) for name in RESET_HEADERS)
                 if seconds is not None), None)


def _reset_at(headers: Any, at: datetime, *, status: int | None = None) -> datetime | None:
    """A rate-limit reset, whether the provider sends seconds-from-now, an epoch timestamp or a duration ("2m59.56s")."""
    value = _header_int(headers, RESET_HEADERS)
    if value is None:
        # Also where LiteLLM's forwarded headers arrive (`llm_provider-x-ratelimit-reset`), so an epoch comes this way
        # too: OpenRouter sends milliseconds.
        value = _duration_reset(headers, status) if headers is not None else None
        if value is None:
            return None
    try:
        if value > 10_000_000_000:  # milliseconds since the epoch
            return datetime.fromtimestamp(value / 1000, UTC).replace(tzinfo=None)
        if value > 1_000_000_000:  # seconds since the epoch
            return datetime.fromtimestamp(value, UTC).replace(tzinfo=None)
        return at + timedelta(seconds=max(0, value))
    except (OverflowError, OSError, ValueError):  # not a date: a reset is a provider fact, never a guess
        return None


# ───────────── persistence ─────────────

_sessions: Callable[[], Session] | None = None


def use_sessions(factory: Callable[[], Session] | None) -> None:
    """Point telemetry at another database (tests); None restores the application database."""
    global _sessions
    _sessions = factory


def _session_factory() -> Callable[[], Session]:
    if _sessions is not None:
        return _sessions
    from app.db import SessionLocal

    return SessionLocal


def build_entry(call: ExternalCall, *, started_at: datetime, completed_at: datetime, method: str | None = None,
                url: str | None = None, status_code: int | None = None, request_bytes: int | None = None,
                response_bytes: int | None = None, headers: Any = None, error: BaseException | None = None,
                error_type: str | None = None) -> dict[str, Any]:
    """One telemetry row. Pure: everything it needs has already been measured."""
    ctx = tracing.current()
    code = status_code if status_code is not None else call.http_status_code
    failure = error_type or (_classify_exception(error) if error is not None else _classify_status(code))
    parsed = httpx.URL(url) if url else None
    window = rate_window(headers, call.metadata) if headers is not None else token_pacing.Window()
    noted = {**call.metadata, "rate_limits": _window_note(window)} if _window_note(window) else call.metadata
    metadata = redact_value(dict(list(noted.items())[:MAX_METADATA_KEYS])) if noted else None
    # Groq's request counter is per day: its row shows the per-minute tokens left, the limit that runs out first.
    remaining = (window.tokens_remaining if window.requests_window == "day" and window.tokens_remaining is not None
                 else _header_int(headers, REMAINING_HEADERS))
    return {
        "request_id": call.request_id,
        "trace_id": (call.metadata.get("trace_id") if isinstance(call.metadata.get("trace_id"), str) else None)
                    or (ctx.trace_id if ctx else None),
        "user_id": call.user_id if call.user_id is not None else (ctx.user_id if ctx else None),
        "provider": (call.provider or "OTHER")[:30].upper(), "service": (call.service or "")[:60],
        "operation": (call.operation or "")[:60],
        "workflow": (ctx.name if ctx else None),
        "workflow_type": call.workflow_type or (ctx.workflow_type if ctx else None),
        "workflow_entity_id": call.workflow_entity_id if call.workflow_entity_id is not None
                              else (ctx.workflow_entity_id if ctx else None),
        "job_id": call.job_id if call.job_id is not None else (ctx.job_id if ctx else None),
        "application_id": call.application_id if call.application_id is not None
                          else (ctx.application_id if ctx else None),
        "http_method": (method or "")[:10] or None,
        "endpoint_host": (parsed.host if parsed else None) or None,
        "endpoint_path": (str(parsed.path)[:MAX_PATH] if parsed else None) or None,
        "status": "FAILED" if (error is not None or failure is not None) else "SUCCEEDED",
        "http_status_code": code,
        "started_at": started_at, "completed_at": completed_at,
        "latency_ms": max(0, int((completed_at - started_at).total_seconds() * 1000)),
        "request_size_bytes": request_bytes, "response_size_bytes": response_bytes,
        "attempt": max(1, call.attempt), "retry_count": max(0, call.attempt - 1),
        "rate_limit_remaining": remaining,
        "rate_limit_reset_at": _reset_at(headers, completed_at, status=code),
        "error_type": failure,
        "error_message": redact_text(f"{type(error).__name__}: {error}") if error is not None
                         else (f"HTTP {code}" if failure else None),
        "estimated_cost": call.estimated_cost, "cost_currency": call.cost_currency, "cost_source": call.cost_source,
        "request_metadata": metadata, "created_at": started_at, "payload": call.payload,
    }


def persist(entry: dict[str, Any]) -> None:
    from app.models import ExternalAPIRequestLog

    with _session_factory()() as db:
        db.add(ExternalAPIRequestLog(**entry))
        db.commit()


def record(call: ExternalCall, **measurements: Any) -> str | None:
    """Store one call's telemetry, update the provider's cooldown, and log anything notable. Never raises."""
    try:
        entry = build_entry(call, **measurements)
        _apply_backoff(entry)
        _observe_window(entry, measurements.get("headers"))
        if not get_settings().external_api_telemetry_enabled:
            return None
        persist(entry)
        _log_notable(entry)
        return entry["request_id"]
    except Exception:  # noqa: BLE001  telemetry is best effort by design
        log.warning("could not record external API telemetry for %s/%s", call.provider, call.operation, exc_info=True)
        return None


def _note_oversized(call: ExternalCall, response: httpx.Response, streamed: bool) -> None:
    """Mark a 429 that refused one oversized request ("Request too large"), so it starts no cooldown: the provider
    refused that request, not the next, smaller one (app.services.rate_limits). Only the mark is kept, never the
    body. Never raises."""
    if response.status_code != 429 or streamed:
        return
    try:
        from app.services.rate_limits import too_large

        if too_large(response.text):
            call.note(oversized_request=True)
    except Exception:  # noqa: BLE001  an unreadable body is an ordinary 429
        log.debug("could not read a 429 body", exc_info=True)


def _apply_backoff(entry: dict[str, Any]) -> None:
    """A 429 starts or lengthens this provider's cooldown; a normal answer ends it. A 429 for one oversized request
    does neither."""
    user_id = entry["user_id"]
    provider = cooldown_provider(entry["provider"], entry.get("service"), entry.get("request_metadata"))
    metadata = entry["request_metadata"] or {}
    if entry["error_type"] == "RATE_LIMIT" and not metadata.get("skipped_by") and not metadata.get("oversized_request"):
        backoff.record_limit(user_id, provider, retry_after=entry["rate_limit_reset_at"])
    elif entry["status"] == "SUCCEEDED":
        backoff.note_success(user_id, provider)


def _observe_window(entry: dict[str, Any], headers: Any) -> None:
    """Keep the token window of the model that answered for pacing (app.services.token_pacing), and settle the
    request's own reservation: taken into the answer's figure, or given back when the request brought no window of
    its model (it failed, was refused as too large, or LiteLLM's fallback or router answered with another model)."""
    metadata = entry["request_metadata"] or {}
    key = cooldown_provider(entry["provider"], entry.get("service"), metadata)
    if not token_pacing.paced(key):
        return
    reserved = metadata.get("tokens_reserved")
    reserved = reserved if isinstance(reserved, int) and not isinstance(reserved, bool) else 0
    window = (rate_window(headers, metadata) if headers is not None and not metadata.get("oversized_request")
              else token_pacing.Window())
    group = headers.get("x-litellm-model-group") if headers is not None else None
    answered = f"LITELLM:{group}"[:230] if isinstance(group, str) and group else key
    if window.paceable() and answered == key:
        token_pacing.observe(entry["user_id"], key, window, released=reserved, now=entry["completed_at"])
        return
    token_pacing.release(entry["user_id"], key, reserved,
                         spent=entry["status"] == "SUCCEEDED" and answered == key)
    if window.paceable():
        token_pacing.observe(entry["user_id"], answered, window, now=entry["completed_at"])


def _log_notable(entry: dict[str, Any]) -> None:
    """A failed or unusually slow call is also an operational event: Monitoring → Logs shows it without a join.

    Successful, normal-speed calls are not logged here — they are already one row each in the request table.
    """
    from app.services import system_log

    failed = entry["status"] == "FAILED"
    slow = (entry["latency_ms"] or 0) >= max(1, get_settings().external_api_slow_call_ms)
    if not failed and not slow:
        return
    if entry.get("service") == "llm" and get_settings().llm_telemetry_enabled:
        # A model call (app.services.llm): llm_telemetry.log_call writes its line, with the model and request row.
        return
    what = f"{entry['provider']} {entry['operation'] or entry['endpoint_path'] or ''}".strip()
    system_log.log_event(
        level="ERROR" if failed else "WARNING",
        category="APIFY" if entry["provider"] == "APIFY" else "EXTERNAL_API",
        message=f"{what} failed: {entry['error_type']}" if failed else f"{what} took {entry['latency_ms']}ms",
        provider=entry["provider"], operation=entry["operation"], workflow=entry["workflow"],
        trace_id=entry["trace_id"], user_id=entry["user_id"],
        status=entry["error_type"] if failed else "SLOW", duration_ms=entry["latency_ms"],
        external_request_id=entry["request_id"], entity_type=entry["workflow_type"],
        entity_id=entry["workflow_entity_id"],
        details={"http_status_code": entry["http_status_code"], "error": entry["error_message"],
                 "host": entry["endpoint_host"], "path": entry["endpoint_path"], "attempt": entry["attempt"]})


# ───────────── the HTTP client every integration uses ─────────────

def _debug_headers(headers: Any) -> dict[str, str] | None:
    """Header names (never their secret values) — only when EXTERNAL_API_DEBUG_PAYLOADS is on."""
    return redact_headers(headers) if get_settings().external_api_debug_payloads else None


def capture_limit(call: ExternalCall) -> int | None:
    """How much of a body to keep, or None to keep none. Bodies are kept only on request: EXTERNAL_API_DEBUG_PAYLOADS
    for every account (short extracts), or the account's own capture switch (Monitoring → Logs, which also covers LLM
    prompts) up to EXTERNAL_API_CAPTURE_LIMIT. Never raises."""
    settings = get_settings()
    try:
        from app.services.llm_telemetry import account_captures

        if account_captures(owner_of(call)):  # the account's own switch; LLM_PROMPT_DEBUG covers LLM text only
            return settings.external_api_capture_limit
    except Exception:  # noqa: BLE001  telemetry is best effort
        log.debug("could not read the capture setting", exc_info=True)
    return settings.external_api_payload_limit if settings.external_api_debug_payloads else None


# A request whose bodies must never be kept, whatever capture says (`extensions={NO_CAPTURE: True}`): the bodies are
# candidate text or numbers no redaction can recognise, such as embedding inputs and vectors.
NO_CAPTURE = "jobhunt_no_capture"


def capture_payload(call: ExternalCall, request: httpx.Request, response: httpx.Response | None = None,
                    error: BaseException | None = None) -> None:
    """Keep the redacted request and response on the call when capture is on. Never raises."""
    try:
        if request.extensions.get(NO_CAPTURE):
            return
        limit = capture_limit(call)
        if not limit:
            return
        body = None if request.extensions.get("jobhunt_streamed") else request.content
        payload: dict[str, Any] = {"request": {"method": request.method, "url": redact_url(str(request.url)),
                                               "headers": redact_headers(request.headers),
                                               "body": redact_body(body, limit)}}
        if response is not None:
            payload["response"] = {"status": response.status_code, "headers": redact_headers(response.headers),
                                   "body": redact_body(response.content, limit)}
        if error is not None:
            payload["error"] = redact_text(f"{type(error).__name__}: {error}", 2000)
        call.payload = payload
    except Exception:  # noqa: BLE001  capture must never change a call
        log.debug("could not capture a payload", exc_info=True)


def _payload_extract(raw: bytes | None) -> str | None:
    """A short, redacted extract of a body — only when EXTERNAL_API_DEBUG_PAYLOADS is on."""
    settings = get_settings()
    if not settings.external_api_debug_payloads or not raw:
        return None
    limit = max(0, settings.external_api_payload_limit)
    return redact_text(raw[:limit].decode("utf-8", "replace"), limit)


def _report_response(request: httpx.Request, response: httpx.Response, call: ExternalCall) -> None:
    """Let the caller add provider-reported facts (cost header, model that answered) to the call before it is stored:
    `extensions={"on_response": fn}`, where fn(response, call) may call `call.note` / `call.record_usage`. Telemetry
    only: an error in it is logged and never changes the response."""
    hook = (request.extensions or {}).get("on_response")
    if not callable(hook):
        return
    try:
        hook(response, call)
    except Exception:  # telemetry must not change an outcome
        log.warning("on_response telemetry hook failed for %s/%s", call.provider, call.operation, exc_info=True)


class InstrumentedClient(httpx.Client):
    """An httpx.Client that records every request it makes.

    `send` is the single funnel for `get`, `post`, SDK usage and redirects alike, so there is no way to use this
    client without being measured. Per-request detail is passed through httpx extensions:

        client.get("/datasets/x/items", extensions={"operation": "dataset_page_fetch", "metadata": {...}})
    """

    def __init__(self, *, provider: str, service: str = "", operation: str = "", user_id: int | None = None,
                 workflow_type: str | None = None, workflow_entity_id: int | None = None, job_id: int | None = None,
                 application_id: int | None = None, metadata: dict[str, Any] | None = None,
                 attempt: int = 1, **kwargs: Any):
        headers = {"User-Agent": UA, **(kwargs.pop("headers", None) or {})}
        super().__init__(headers=headers, **kwargs)
        self.call_defaults = dict(provider=provider, service=service, operation=operation, user_id=user_id,
                                  workflow_type=workflow_type, workflow_entity_id=workflow_entity_id,
                                  job_id=job_id, application_id=application_id, attempt=attempt)
        self.default_metadata = dict(metadata or {})
        self.last_request_id: str | None = None

    def _call_for(self, request: httpx.Request) -> ExternalCall:
        extra = request.extensions or {}
        defaults = {**self.call_defaults}
        for key in ("provider", "service", "operation", "user_id", "workflow_type", "workflow_entity_id",
                    "job_id", "application_id", "attempt"):
            if extra.get(key) is not None:
                defaults[key] = extra[key]
        return ExternalCall(metadata={**self.default_metadata, **(extra.get("metadata") or {})}, **defaults)

    def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        call = self._call_for(request)
        started, clock = utcnow(), time.monotonic()
        hold_off(call, method=request.method, url=str(request.url))
        request_bytes = _request_size(request)
        try:
            response = super().send(request, **kwargs)
        except Exception as exc:
            capture_payload(call, request, error=exc)
            self.last_request_id = record(call, started_at=started, completed_at=_finished(started, clock),
                                          method=request.method, url=str(request.url), request_bytes=request_bytes,
                                          error=exc)
            raise
        if not kwargs.get("stream") and not request.extensions.get(NO_CAPTURE):
            call.note(request_payload=_payload_extract(request.content),
                      response_payload=_payload_extract(response.content),
                      request_headers=_debug_headers(request.headers))
            capture_payload(call, request, response)
        _note_oversized(call, response, bool(kwargs.get("stream")))
        _report_response(request, response, call)
        self.last_request_id = record(
            call, started_at=started, completed_at=_finished(started, clock), method=request.method,
            url=str(request.url), status_code=response.status_code, request_bytes=request_bytes,
            response_bytes=_response_size(response), headers=response.headers)
        response.extensions["jobhunt_request_id"] = self.last_request_id
        return response


class InstrumentedAsyncClient(httpx.AsyncClient):
    """The asynchronous twin of InstrumentedClient, for integrations that need one."""

    def __init__(self, *, provider: str, service: str = "", operation: str = "", user_id: int | None = None,
                 metadata: dict[str, Any] | None = None, **kwargs: Any):
        headers = {"User-Agent": UA, **(kwargs.pop("headers", None) or {})}
        super().__init__(headers=headers, **kwargs)
        self.call_defaults = dict(provider=provider, service=service, operation=operation, user_id=user_id)
        self.default_metadata = dict(metadata or {})

    def _call_for(self, request: httpx.Request) -> ExternalCall:
        extra = request.extensions or {}
        defaults = {**self.call_defaults}
        for key in ("provider", "service", "operation", "user_id"):
            if extra.get(key) is not None:
                defaults[key] = extra[key]
        return ExternalCall(metadata={**self.default_metadata, **(extra.get("metadata") or {})}, **defaults)

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        call = self._call_for(request)
        started, clock = utcnow(), time.monotonic()
        hold_off(call, method=request.method, url=str(request.url))
        request_bytes = _request_size(request)
        try:
            response = await super().send(request, **kwargs)
        except Exception as exc:
            record(call, started_at=started, completed_at=_finished(started, clock), method=request.method,
                   url=str(request.url), request_bytes=request_bytes, error=exc)
            raise
        _note_oversized(call, response, bool(kwargs.get("stream")))
        record(call, started_at=started, completed_at=_finished(started, clock), method=request.method,
               url=str(request.url), status_code=response.status_code, request_bytes=request_bytes,
               response_bytes=_response_size(response), headers=response.headers)
        return response


def cooldown_provider(provider: str, service: str | None, metadata: dict[str, Any] | None) -> str:
    """The key a 429 cooldown is kept under. A model request through the LiteLLM gateway (a worker's request or a
    model test) rests only that model (`LITELLM:<model>`), so one rate-limited model never blocks the gateway's other
    models, nor its admin calls (Apply, Sync): that is what lets failover (app.services.failover) send the request
    somewhere else. Everything else rests the whole provider."""
    model = (metadata or {}).get("model")
    if provider.upper() == "LITELLM" and isinstance(model, str) and model:
        return f"LITELLM:{model}"[:230]
    return provider


def hold_off(call: ExternalCall, *, method: str | None = None, url: str | None = None) -> None:
    """Refuse to make a call while its provider's cooldown is running, and record the one we saved.

    The saved call is real telemetry — it is why a queue stopped hammering a provider — so it is stored with
    `skipped_by: cooldown` and no HTTP status, since nothing was sent.
    """
    owner = owner_of(call)
    if backoff.bypassing():  # a task a person asked to run now (app.services.workers)
        return
    key = cooldown_provider(call.provider, call.service, call.metadata)
    try:
        backoff.check(owner, key)
    except backoff.RateLimited as limited:
        call.note(skipped_by="cooldown", retry_after_seconds=limited.retry_after_seconds)
        backoff.note_skipped(owner, key)
        moment = utcnow()
        record(call, started_at=moment, completed_at=moment, method=method, url=url,
               error=RateLimitedCooldown(limited.provider, limited.retry_after_seconds), error_type="RATE_LIMIT")
        raise RateLimitedCooldown(limited.provider, limited.retry_after_seconds) from limited
    # A model request the last answer says this minute cannot take: wait for the token reset instead of a 429.
    need = call.metadata.get("tokens_needed")
    wait, reserved = token_pacing.reserve(owner, key, need) if token_pacing.paced(key) else (None, 0)
    if reserved:
        call.note(tokens_reserved=reserved)  # settled when the answer (or the failure) is recorded
    if wait:
        held = RateLimitedCooldown(key.upper(), wait, f"{key} has fewer tokens left this minute than this request needs "
                                                      f"(about {need}); waiting {wait}s for the provider's reset",
                                   paced=True)
        call.note(skipped_by="token_pacing", retry_after_seconds=wait)
        moment = utcnow()
        record(call, started_at=moment, completed_at=moment, method=method, url=url, error=held,
               error_type="RATE_LIMIT")
        raise held


def _finished(started: datetime, clock: float) -> datetime:
    """The end of the call, measured on the monotonic clock so a system clock change cannot skew latency."""
    return started + timedelta(seconds=max(0.0, time.monotonic() - clock))


def _request_size(request: httpx.Request) -> int | None:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            return int(length)
        except ValueError:
            return None
    try:
        return len(request.content)
    except (httpx.StreamConsumed, RuntimeError):
        return None


def _response_size(response: httpx.Response) -> int | None:
    length = response.headers.get("content-length")
    if length is not None:
        try:
            return int(length)
        except ValueError:
            pass
    try:
        return len(response.content)
    except (httpx.ResponseNotRead, RuntimeError):
        return None


def external_client(provider: str, *, service: str = "", operation: str = "", timeout: float | int = 30,
                    headers: dict[str, str] | None = None, **kwargs: Any) -> InstrumentedClient:
    """The HTTP client for third-party calls. Use it instead of httpx.Client everywhere in production code."""
    return InstrumentedClient(provider=provider, service=service, operation=operation, timeout=timeout,
                              headers=headers, **kwargs)


def external_async_client(provider: str, *, service: str = "", operation: str = "", timeout: float | int = 30,
                          headers: dict[str, str] | None = None, **kwargs: Any) -> InstrumentedAsyncClient:
    return InstrumentedAsyncClient(provider=provider, service=service, operation=operation, timeout=timeout,
                                   headers=headers, **kwargs)


# ───────────── SDK boundaries ─────────────

@contextmanager
def external_operation(provider: str, *, service: str = "", operation: str = "", user_id: int | None = None,
                       attempt: int = 1, workflow_type: str | None = None, workflow_entity_id: int | None = None,
                       job_id: int | None = None, application_id: int | None = None,
                       metadata: dict[str, Any] | None = None):
    """Measure one call made by a vendor SDK that owns its own transport.

        with external_operation("OPENAI", service="chat", operation="chat_completion") as call:
            response = client.chat.completions.create(...)
            call.record_usage(cost=..., source="provider_reported")

    The call is timed, its failure classified and its row written whatever happens; the exception, if any,
    reaches the caller unchanged.
    """
    call = ExternalCall(provider=provider, service=service, operation=operation, user_id=user_id, attempt=attempt,
                        workflow_type=workflow_type, workflow_entity_id=workflow_entity_id, job_id=job_id,
                        application_id=application_id, metadata=dict(metadata or {}))
    hold_off(call)
    started, clock = utcnow(), time.monotonic()
    try:
        yield call
    except Exception as exc:
        record(call, started_at=started, completed_at=_finished(started, clock),
               status_code=_status_of(exc) or call.http_status_code, error=exc)
        raise
    record(call, started_at=started, completed_at=_finished(started, clock))


class ExportBlocked(Exception):
    """A request a `pre_send` policy refused: it was never sent."""


def requests_session(provider: str, *, service: str = "", operation: str = "",
                     pre_send: Callable[[bytes | None], str | None] | None = None,
                     pre_send_request: Callable[[str, Any], str | None] | None = None) -> Any:
    """A `requests.Session` for a vendor SDK that only accepts one (LangSmith), measured like `external_client`.

    `Session.send` is the funnel for every request the SDK makes, whatever adapters it mounts, so nothing gets past:
    `pre_send(body)` may refuse a request by returning a reason (the request is not sent; the SDK gets a 400 and
    the refusal is recorded as a CLIENT_ERROR with `blocked_by`); `pre_send_request(url, headers)` may refuse it the same
    way for its URL or headers; a transport failure is recorded and answered with a 503 instead of raising,
    so an outage of the provider never reaches the code that called the SDK. Bodies are never stored.
    """
    import requests

    class InstrumentedSession(requests.Session):
        def send(self, request: Any, **kwargs: Any) -> Any:
            call = ExternalCall(provider=provider, service=service, operation=operation)
            started, clock = utcnow(), time.monotonic()
            body = request.body.encode() if isinstance(request.body, str) else request.body
            size = len(body) if isinstance(body, bytes) else None
            measured = {"method": request.method, "url": redact_url(request.url or ""), "request_bytes": size}
            try:
                reason = None
                if pre_send is not None:
                    # A streamed body (a file, a generator, a multipart encoder) cannot be checked: refuse it.
                    readable = body is None or isinstance(body, (bytes, bytearray))
                    reason = pre_send(bytes(body) if body is not None else None) if readable else "unreadable body"
                if not reason and pre_send_request is not None:
                    reason = pre_send_request(request.url or "", request.headers)
                if reason:
                    call.note(blocked_by="pre_send_policy")
                    record(call, started_at=started, completed_at=_finished(started, clock),
                           error=ExportBlocked(reason), error_type="CLIENT_ERROR", **measured)
                    return _synthetic(request, 400, "blocked by the Job Hunt OS export policy")
                hold_off(call, method=request.method, url=measured["url"])
                response = super().send(request, **kwargs)
            except Exception as exc:  # noqa: BLE001  an outage must not reach the SDK's caller
                record(call, started_at=started, completed_at=_finished(started, clock), error=exc, **measured)
                return _synthetic(request, 503, "provider unavailable")
            record(call, started_at=started, completed_at=_finished(started, clock), status_code=response.status_code,
                   response_bytes=len(response.content or b""), headers=response.headers, **measured)
            return response

    return InstrumentedSession()


def _synthetic(request: Any, status: int, message: str) -> Any:
    import requests

    response = requests.Response()
    response.status_code, response.request, response.url = status, request, request.url
    response.reason = message
    response._content = json.dumps({"detail": message}).encode()
    response.headers["content-type"] = "application/json"
    return response


def _status_of(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    return code if isinstance(code, int) else None


def track_external_call(provider: str, *, service: str = "", operation: str | None = None):
    """Instrument a whole integration method, for SDKs where there is nothing finer to wrap.

        @track_external_call("SEARCH", service="serp", operation="search")
        def search(query: str) -> list[dict]: ...

    The wrapped function may take a keyword-only `call` argument to add metadata or provider-reported cost.
    """
    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        name = operation or func.__name__
        wants_call = "call" in inspect.signature(func).parameters

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with external_operation(provider, service=service, operation=name) as call:
                return func(*args, call=call, **kwargs) if wants_call else func(*args, **kwargs)
        return wrapper
    return decorate
