"""What a provider's 429 says about when the model can be called again.

A 429 is not one thing. A per-minute throttle clears in seconds, a daily quota at a time the provider names, a usage
cap (Ollama's 5-hour or weekly limits) at a time it does not name, and "request too large" never clears for that
request however long the model rests. Route health (app.services.model_health) rests a model accordingly.

Only what the provider said is used: a reset header, a reset timestamp in the error body, or "try again in 8.4s".
When it names no time, `until` stays None and the account's setting decides. Never raises.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

THROTTLE = "THROTTLE"  # per-minute limits: "try again in 8.385s"
QUOTA = "QUOTA"  # a daily (or other) quota with a reset the provider names
USAGE_CAP = "USAGE_CAP"  # a usage allowance used up, reset time not given (Ollama's 5-hour / weekly caps)
TOO_LARGE = "TOO_LARGE"  # this one request exceeds a limit; the model is fine for smaller ones
UNKNOWN = "UNKNOWN"

LABELS = {THROTTLE: "rate limited", QUOTA: "quota used up", USAGE_CAP: "usage limit reached",
          TOO_LARGE: "request larger than the model's rate limit", UNKNOWN: "rate limited"}

_TOO_LARGE = re.compile(r"request too large|please reduce your message size", re.I)
_USAGE_CAP = re.compile(r"usage limit|5-hour|weekly (?:cap|limit)|monthly (?:cap|limit)|insufficient[_ ]quota|"
                        r"exceeded your current quota|out of credits", re.I)
_QUOTA = re.compile(r"per[- ]day|daily|requests per day|\bRPD\b|free-models-per", re.I)
_TRY_AGAIN = re.compile(r"(?:try again|retry) (?:in|after)\s+(?:(\d+)h)?\s*(?:(\d+)m(?!s))?\s*(?:(\d+(?:\.\d+)?)s)?"
                        r"\s*(?:(\d+(?:\.\d+)?)ms)?", re.I)
_RESET_IN_BODY = re.compile(r"""["']?x-ratelimit-reset(?:-requests|-tokens)?["']?\s*[:=]\s*["']?(\d{1,16})""", re.I)
_RESET_HEADERS = ("retry-after", "x-ratelimit-reset", "x-ratelimit-reset-requests", "ratelimit-reset",
                  "x-rate-limit-reset", "llm_provider-retry-after", "llm_provider-x-ratelimit-reset")
MAX_REST = timedelta(days=8)  # a weekly cap, with a day to spare; anything later is not believed


def too_large(text: str | None) -> bool:
    """A 429 that refused one oversized request, not the model."""
    return bool(text and _TOO_LARGE.search(text))


# Groq's wording: "for model `openai/gpt-oss-20b` … on tokens per minute (TPM): Limit 8000, Requested 9197".
_LIMIT = re.compile(r"for model `([^`]{1,200})`.{0,300}?\((TPM|ITPM|OTPM|RPM|TPD|RPD)\):\s*Limit (\d+)"
                    r"(?:,\s*Used \d+)?,\s*Requested (\d+)", re.I | re.S)
# Which tokens a limit counts: input and output (TPM, TPD), input only (ITPM) or output only (OTPM).
COUNTS = {"TPM": "total", "TPD": "total", "ITPM": "input", "OTPM": "output"}


@dataclass(frozen=True)
class SizeLimit:
    """The limit a provider refused a request against, as it stated it."""
    deployment: str  # the model behind the gateway's name ("qwen/qwen3.8-27b" behind "Groq")
    kind: str  # TPM, ITPM, OTPM …
    limit: int
    requested: int

    @property
    def counts(self) -> str:
        return COUNTS.get(self.kind, "total")


def size_limit(text: str | None) -> SizeLimit | None:
    """The limit and request size in a "request too large" 429, or None when the provider did not state them."""
    if not too_large(text):
        return None
    match = _LIMIT.search(text or "")
    if not match:
        return None
    return SizeLimit(match.group(1), match.group(2).upper(), int(match.group(3)), int(match.group(4)))


@dataclass(frozen=True)
class RateLimit:
    kind: str
    until: datetime | None = None  # naive UTC, from the provider; None when it named no time

    @property
    def label(self) -> str:
        return LABELS.get(self.kind, "rate limited")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _moment(value: str, now: datetime) -> datetime | None:
    """A reset value: seconds from now, or an epoch timestamp in seconds or milliseconds."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 10_000_000_000:
        at = datetime.fromtimestamp(number / 1000, UTC).replace(tzinfo=None)
    elif number > 1_000_000_000:
        at = datetime.fromtimestamp(number, UTC).replace(tzinfo=None)
    else:
        at = now + timedelta(seconds=max(0.0, number))
    return at if now < at <= now + MAX_REST else None


def _try_again(text: str, now: datetime) -> datetime | None:
    match = _TRY_AGAIN.search(text)
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds, millis = (float(g) if g else 0.0 for g in match.groups())
    at = now + timedelta(hours=hours, minutes=minutes, seconds=seconds, milliseconds=millis)
    return at if now < at <= now + MAX_REST else None


def classify(text: str | None, headers: Any = None, *, now: datetime | None = None) -> RateLimit:
    """What kind of 429 this is and, when the provider said, when it clears."""
    try:
        now = now or utcnow()
        text = text or ""
        if too_large(text):
            return RateLimit(TOO_LARGE)
        until = None
        for name in _RESET_HEADERS:
            value = headers.get(name) if headers is not None else None
            if value and (until := _moment(str(value), now)):
                break
        if until is None and (match := _RESET_IN_BODY.search(text)):
            until = _moment(match.group(1), now)
        until = until or _try_again(text, now)
        if _USAGE_CAP.search(text):
            return RateLimit(USAGE_CAP, until)
        if _QUOTA.search(text):
            return RateLimit(QUOTA, until)
        return RateLimit(THROTTLE if until else UNKNOWN, until)
    except Exception:  # noqa: BLE001  a reading we cannot make is an ordinary rate limit
        return RateLimit(UNKNOWN)


# ───────────── which provider refused ─────────────

# LiteLLM names the provider that answered 429: "litellm.RateLimitError: RateLimitError: OpenrouterException - {…}".
_PROVIDER = re.compile(r"\b([A-Za-z][A-Za-z0-9_]{1,40})Exception\s*-", re.IGNORECASE)
_NOT_PROVIDERS = {"litellm", "ratelimit", "llm", "api", "http", "timeout", "internalserver", "badrequest",
                  "serviceunavailable", "apiconnection", "contextwindowexceeded"}
_FREE_POOL = re.compile(r"free-models-per", re.IGNORECASE)


def provider_named(text: str | None) -> str | None:
    """The provider LiteLLM says refused the request, normalized ("Ollama_chat" → "ollama_cloud": only Ollama's hosted
    models have usage limits), or None when the text names none. Only what the error said is used."""
    match = _PROVIDER.search(text or "")
    if not match:
        return None
    name = match.group(1).lower()
    if name in _NOT_PROVIDERS:
        return None
    name = name.removesuffix("_chat")
    return "ollama_cloud" if name == "ollama" else name


def free_pool(text: str | None) -> bool:
    """A 429 for the provider's shared free-model quota (OpenRouter's free-models-per-day): paid models are unaffected."""
    return bool(text and _FREE_POOL.search(text))
