"""The only place a LangSmith client is made (tests/test_external_api_guard.py enforces it).

Off unless AGENT_LANGSMITH_EXPORT_ENABLED and AGENT_LANGSMITH_API_KEY are set; then a graph run gets one
LangChainTracer whose client:

* sends through app.services.external_api.requests_session (provider LANGSMITH), so every export is measured in
  Monitoring, a refused body is never sent, and an outage is a recorded failure instead of an exception;
* applies app.agents.export_policy: allowlist hooks for inputs, outputs and metadata, error class names only, and a
  final allowlist of run fields, then the fail-closed wire check on the bytes;
* never compresses (the wire check must be able to read what it sends) and omits runtime/environment details.

Every graph run happens inside `export(...)`: tracing is disabled outright when export is off, and pinned to the
policed client when it is on, so LANGSMITH_TRACING / LANGCHAIN_TRACING_V2 in the environment can never add an
unpoliced tracer. Monitoring stays the operational record; a trace here is
for inspection only, and a LangSmith failure never changes a run or any business state.

A 429 pauses export for this process (`pause`): graph runs go untraced and nothing is sent until the pause ends, then
the next run tries again. Without it a spent monthly trace quota is retried on every run, each one a failed call and
two log rows. The provider cooldown in app.services.backoff does not cover this: it is per account and an export
belongs to none.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.agents import export_policy
from app.settings import get_settings

log = logging.getLogger("jobhunt.agents.tracing")
_clients: dict[tuple, Any] = {}
_lock = threading.Lock()
RATE_LIMIT_PAUSE_SECONDS = 300  # a 429 with no Retry-After that is not about the plan's quota
MAX_PAUSE_SECONDS = 86_400
QUOTA_WORDS = ("usage limit", "quota")  # "Monthly unique traces usage limit exceeded"
_pause = {"until": 0.0, "reason": "", "skipped": 0}  # monotonic clock; this process only


def configured() -> bool:
    settings = get_settings()
    return bool(settings.agent_langsmith_export_enabled and settings.agent_langsmith_api_key)


def pause(seconds: float, reason: str) -> None:
    """Stop exporting for a while; logged once, when the pause starts or grows."""
    until = time.monotonic() + max(1.0, min(float(seconds), MAX_PAUSE_SECONDS))
    with _lock:
        if until <= _pause["until"]:
            return
        fresh = _pause["until"] <= time.monotonic()
        _pause.update(until=until, reason=reason, skipped=0 if fresh else _pause["skipped"])
    log.warning("LangSmith export paused for %ds: %s. Graph runs continue untraced.", round(seconds), reason)


def paused() -> str | None:
    """Why nothing is exported right now, or None. The end of a pause is logged with what it saved."""
    with _lock:
        if not _pause["until"]:
            return None
        if time.monotonic() < _pause["until"]:
            return f"LangSmith export is paused: {_pause['reason']}"
        skipped = _pause["skipped"]
        _pause.update(until=0.0, reason="", skipped=0)
    log.info("LangSmith export resumed after a pause (%d requests not sent)", skipped)
    return None


def _skip() -> str | None:
    """`paused()` for the session: also counts the queued request it is about to drop."""
    why = paused()
    if why:
        with _lock:
            _pause["skipped"] += 1
    return why


def _on_response(response: Any) -> None:
    """A 429 starts a pause: the provider's Retry-After when it gives one, the quota pause when the plan's limit is
    spent (it will not come back within minutes), a short one otherwise."""
    if response.status_code != 429:
        return
    from app.services.external_api import duration_seconds

    text = (response.text or "")[:2000].lower()
    quota = any(word in text for word in QUOTA_WORDS)
    after = duration_seconds(response.headers.get("retry-after"))
    floor = max(1, get_settings().agent_langsmith_quota_pause_minutes) * 60 if quota else RATE_LIMIT_PAUSE_SECONDS
    pause(max(after or 0.0, floor), "the plan's usage limit is spent" if quota else "rate limited")


def _client_class() -> type:
    from langsmith import Client

    class PolicedClient(Client):
        """A LangSmith client whose every run passes the export policy before serialization."""

        def _run_transform(self, run: Any, update: bool = False, copy: bool = False) -> dict:
            return export_policy.project_run(super()._run_transform(run, update=update, copy=copy))

        def update_run(self, run_id: Any, **fields: Any) -> None:
            return super().update_run(run_id, **export_policy.project_update(fields))

        def _hide_run_error(self, error: Any) -> Any:
            return export_policy.project_error(error)

    return PolicedClient


def client(fields: tuple[str, ...]) -> Any:
    """The policed client for a graph's safe state fields (one per process and field set)."""
    from app.services.external_api import requests_session

    settings = get_settings()
    key = (fields, settings.agent_langsmith_endpoint, settings.agent_langsmith_api_key)
    with _lock:
        if key not in _clients:
            hook = export_policy.project_state(fields)
            _clients[key] = _client_class()(
                api_url=settings.agent_langsmith_endpoint, api_key=settings.agent_langsmith_api_key,
                session=requests_session("LANGSMITH", service="runs", operation="trace_export",
                                         pre_send=export_policy.wire_check,
                                         pre_send_request=export_policy.request_check,
                                         paused=_skip, on_response=_on_response),
                timeout_ms=settings.agent_langsmith_timeout_ms, hide_inputs=hook, hide_outputs=hook,
                hide_metadata=export_policy.project_metadata, omit_traced_runtime_info=True,
                info={"instance_flags": {}})  # known up front: no /info call, and never zstd compression
        return _clients[key]


@contextmanager
def export(fields: tuple[str, ...]) -> Iterator[list[Any]]:
    """The tracing context of one graph run; yields the callbacks to pass to it. Never raises on its own.

    Off: LangSmith tracing is disabled for the run, whatever the environment says. On: tracing is pinned to the
    policed client, so the one tracer LangChain may add from the environment (only when none is passed) is policed too.
    """
    from langsmith.run_helpers import tracing_context

    tracer, policed = None, None
    if configured() and not paused():
        try:
            from langchain_core.tracers import LangChainTracer

            policed = client(fields)
            tracer = LangChainTracer(project_name=get_settings().agent_langsmith_project, client=policed)
        except Exception:
            log.warning("LangSmith export is configured but could not start; running without it", exc_info=True)
            tracer, policed = None, None
    if tracer is None:
        with tracing_context(enabled=False):
            yield []
        return
    with tracing_context(enabled=True, client=policed, project_name=get_settings().agent_langsmith_project):
        yield [tracer]


def flush() -> None:
    """Wait for queued exports (tests, shutdown). Never raises."""
    for c in list(_clients.values()):
        try:
            c.flush()
        except Exception:
            log.warning("LangSmith flush failed", exc_info=True)


def reset() -> None:
    """Forget the cached clients and any pause (tests; a settings change)."""
    with _lock:
        _clients.clear()
        _pause.update(until=0.0, reason="", skipped=0)
