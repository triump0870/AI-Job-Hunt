"""Provider-independent LLM layer: one call shape for the LiteLLM gateway, OpenRouter, OpenAI, Anthropic, Gemini
and the no-cost mock.

LiteLLM gateway mode (LLM_PROVIDER / Settings provider `litellm`) is the preferred mode: every request goes to the
LiteLLM proxy, and the worker's model and generation settings come from AI Orchestration → Models
(app.services.model_resolution) rather than from any name in code. In that mode nothing here calls Ollama, OpenAI,
Anthropic, OpenRouter or Gemini directly. The direct-provider modes remain for installs without a gateway.

Every request goes through `LLMClient._generate`, which records telemetry (app.services.llm_telemetry): tokens,
context size, latency, estimated cost, errors and structured-output validity. Services never add monitoring code;
they may pass an `LLMCall` saying which job or application the request is for, the attempt number, and a validator.

Failures are typed so workers can decide what to do without reading error text:

* LLMConfigError: missing key, unsupported provider, or a paid model while OPENROUTER_REQUIRE_FREE is on.
  Raised before any network call; retrying cannot help until the settings change.
* LLMTimeoutError / LLMRateLimitError: the provider was slow or throttled; worth retrying later.
* LLMContextOverflowError: the prompt does not fit the model's context window.
* LLMResponseError: the model answered, but not with a JSON object.
* LLMSchemaError: the model answered with JSON that does not match the worker's output contract (app.prompts.contracts),
  including JSON in another worker's shape (POSSIBLE_CROSS_WORKER_OUTPUT).
* LLMError: any other provider failure (outage, 5xx, connection error).

Structured output, for every provider:

    model answer → JSON object → the prompt's output contract (unknown fields rejected) → the service's own validator
    → LLMResult.validated

A contract failure is retried once with a short schema correction appended to the same request (unless the gateway
already escalated it to a stronger model); a second failure reaches the caller. Missing fields are never inferred.
How the contract was enforced is recorded per request (`schema_enforcement`): native_schema (a JSON Schema the provider
enforces), json_mode (JSON only, the contract checked here), prompt_only, or none.
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from app.services import failover, llm_telemetry, model_resolution, rate_limits, tracing
from app.services.ai_config import is_free_model
from app.services.ai_config import resolve as resolve_ai_config
from app.services.ai_workers import label as worker_label
from app.services.ai_workers import worker_for_prompt
from app.services.external_api import RateLimitedCooldown, external_operation
from app.services.llm_telemetry import Attempt, LLMCall, Usage
from app.settings import get_settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.prompts.common import Prompt

__all__ = ["LLMCall", "LLMClient", "LLMConfigError", "LLMContextOverflowError", "LLMError", "LLMRateLimitError",
           "LLMResponseError", "LLMResult", "LLMSchemaError", "LLMTimeoutError"]


log = logging.getLogger("jobhunt.llm")


class LLMError(RuntimeError):
    pass


class LLMConfigError(LLMError):
    """The configured provider cannot be called as set up."""


class LLMTimeoutError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    """A 429. `rate_limit` says what kind and, when the provider named it, when it clears (app.services.rate_limits)."""

    def __init__(self, *args: Any, rate_limit: Any = None, held_seconds: int | None = None):
        super().__init__(*args)
        if rate_limit is None and args:
            from app.services import rate_limits

            rate_limit = rate_limits.classify(str(args[0]))
        self.rate_limit = rate_limit
        # Set when token pacing held the request (app.services.token_pacing): nothing was sent, and the model's
        # token window resets in this many seconds. Queued workers wait for it without spending an attempt.
        self.held_seconds = held_seconds


def _rate_limited(waiting: RateLimitedCooldown) -> LLMRateLimitError:
    return LLMRateLimitError(str(waiting), held_seconds=waiting.retry_after_seconds if waiting.paced else None)


class LLMContextOverflowError(LLMError):
    """The request is larger than the model's context window."""


class LLMResponseError(LLMError):
    """The provider answered, but the answer is not a usable JSON object."""
    reason = "INVALID_JSON"


class LLMSchemaError(LLMResponseError):
    """Valid JSON that does not match the worker's output contract. `reason` is WRONG_OUTPUT_SCHEMA, or
    POSSIBLE_CROSS_WORKER_OUTPUT when its fields belong to another worker (`looks_like` names which)."""

    def __init__(self, contract: str, reason: str, problems: list[str], looks_like: str | None = None):
        self.contract, self.reason, self.problems, self.looks_like = contract, reason, problems, looks_like
        detail = f" (looks like {looks_like} output)" if looks_like else ""
        super().__init__(f"{reason}: the answer does not match {contract}{detail}: " + "; ".join(problems))


# One schema-correction retry per request; a second contract failure is final.
MAX_SCHEMA_CORRECTIONS = 1


def unwrap(contract: Any, data: Any) -> Any:
    """An answer wrapped in one outer key ({"fit_analysis": {...}}, often the prompt's marker echoed back) is its
    inner object — only when the whole answer fails the contract and that inner object passes it on its own.
    Nothing is inferred. Anything else is returned unchanged."""
    from app.prompts.contracts import SIGNAL_FIELDS

    if contract is None or not isinstance(data, dict):
        return data
    keys = [k for k in data if k not in SIGNAL_FIELDS]
    if len(keys) != 1 or keys[0] in contract.model_fields or not isinstance(data[keys[0]], dict):
        return data
    inner = data[keys[0]]
    try:
        contract.model_validate({k: v for k, v in inner.items() if k not in SIGNAL_FIELDS})
    except ValidationError:
        return data
    return inner


def check_contract(contract: Any, data: dict[str, Any]) -> Any:
    """The answer as the contract's model. Escalation signals are routing hints, not part of any result."""
    from app.prompts.contracts import SIGNAL_FIELDS, diagnose

    try:
        return contract.model_validate({k: v for k, v in data.items() if k not in SIGNAL_FIELDS})
    except ValidationError as exc:
        reason, looks_like = diagnose(contract, data)
        problems = [f"{'.'.join(str(p) for p in error['loc']) or 'result'}: {error['msg']}"
                    for error in exc.errors()[:6]]
        raise LLMSchemaError(contract.__name__, reason, problems, looks_like) from exc


def correction_note(contract: Any, error: LLMResponseError) -> str:
    """Appended to the user message for the one retry (wording in app.prompts.common.schema_correction)."""
    from app.prompts.common import schema_correction

    return schema_correction(getattr(contract, "__name__", "the requested"),
                             getattr(error, "problems", None) or [str(error)[:300]])


@dataclass(frozen=True)
class LLMResult:
    data: dict[str, Any]
    provider: str
    model: str  # the model that answered when the provider reports it, otherwise the one requested
    usage: Usage | None = None
    validated: Any = None  # what LLMCall.validate returned, when a validator was given
    # Gateway mode: what was asked for and what answered (worker, model group, tier, fallback, escalation).
    routing: dict[str, Any] | None = None


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise LLMResponseError("Model did not return JSON")
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMResponseError("Model returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise LLMResponseError(f"Model returned a JSON {type(value).__name__}, not an object")
    return value


# Stage (from a prompt marker) → the worker whose configuration a prompt without a known name runs under.
STAGE_WORKERS = {"rank": "RANKER", "tailor": "TAILOR", "review": "REVIEWER", "followup": "FOLLOWUP",
                 "advisor": "AI_OPTIMIZATION"}
# Prompt markers (app.prompts) → the stage whose model runs them.
MARKER_TASKS = {"FIT_ANALYSIS": "rank", "TAILOR_RESUME": "tailor", "COVER_LETTER": "tailor", "REVIEW_RESUME": "review", "FOLLOW_UP": "followup",
                "EXTRACT_EVIDENCE": "tailor", "AI_OPTIMIZATION": "advisor"}
CONTEXT_OVERFLOW = re.compile(r"context[_ ]length|context window|maximum context|prompt is too long|too many tokens", re.IGNORECASE)


def _task_from_prompt(user: str) -> str:
    """The stage of a prompt, read from its first line only, so text inside the data can never reroute it."""
    first_line = (user or "").lstrip().split("\n", 1)[0].strip()
    return MARKER_TASKS.get(first_line, "default")


def _csv_models(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _is_free_openrouter_model(model: str) -> bool:
    # The free router itself and explicit :free variants have zero token price.
    return is_free_model("openrouter", model)


def _provider_error(provider: str, model: str, exc: Exception) -> LLMError:
    """Map an SDK or transport exception to a typed LLMError without importing every SDK."""
    name = type(exc).__name__
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    detail = f"{provider} request failed for model={model}: {exc}"
    if isinstance(exc, TimeoutError) or "Timeout" in name:
        return LLMTimeoutError(detail)
    if status == 429 or "RateLimit" in name:
        return LLMRateLimitError(detail)
    if status == 413 or CONTEXT_OVERFLOW.search(str(exc)):
        return LLMContextOverflowError(detail)
    return LLMError(detail)


def _unavailable(exc: BaseException) -> bool:
    """The model never gave an answer (throttled, held, timed out, unreachable) — as opposed to giving a bad one or
    our own configuration being wrong."""
    if isinstance(exc, RateLimitedCooldown):
        return True
    return isinstance(exc, LLMError) and not isinstance(exc, (LLMResponseError, LLMConfigError,
                                                              LLMContextOverflowError))


def error_type(exc: BaseException, *, validation: bool = False) -> str:
    """The Monitoring tab's failure category for an exception raised while generating."""
    if validation or (isinstance(exc, LLMSchemaError) and exc.reason != "INVALID_JSON"):
        return "SCHEMA_VALIDATION"
    return ("CONFIGURATION" if isinstance(exc, LLMConfigError) else "TIMEOUT" if isinstance(exc, (LLMTimeoutError, TimeoutError))
            else "RATE_LIMIT" if isinstance(exc, (LLMRateLimitError, RateLimitedCooldown))
            else "CONTEXT_OVERFLOW" if isinstance(exc, LLMContextOverflowError)
            else "INVALID_JSON" if isinstance(exc, LLMResponseError)
            else "PROVIDER_ERROR" if isinstance(exc, LLMError) else "OTHER")


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _answered_model(response: Any, requested: str) -> str:
    answered = getattr(response, "model", None)
    return answered if isinstance(answered, str) and answered else requested


def _answered_model_name(body: Any) -> str | None:
    value = body.get("model") if isinstance(body, dict) else None
    return value if isinstance(value, str) and value else None


def _dict_usage(usage: Any, reported_cost: float | None) -> Usage | None:
    """Token usage from an OpenAI-shaped JSON answer; the cost is what the gateway itself reported, if anything."""
    if not isinstance(usage, dict) or _int(usage.get("prompt_tokens")) is None:
        return None
    prompt = _int(usage["prompt_tokens"])
    cached = _int((usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
    return Usage(input_tokens=prompt, output_tokens=_int(usage.get("completion_tokens")), cache_read_tokens=cached,
                 uncached_input_tokens=prompt - (cached or 0), reported_cost=reported_cost,
                 reasoning_tokens=_int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")))


def _gateway_timings(attempt: Attempt, facts: dict[str, Any]) -> None:
    """The model call's own duration is what LiteLLM spent on the request minus its overhead; both come from response
    headers, so when either is absent the duration stays unknown (never inferred from our wall-clock latency)."""
    try:
        overhead, total = facts.get("overhead_duration_ms"), facts.get("response_duration_ms")
        if overhead is not None and overhead >= 0:
            attempt.gateway_overhead_ms = int(overhead)
        if overhead is not None and total is not None and total >= overhead >= 0:
            attempt.generation_ms = int(total - overhead)
    except Exception:  # noqa: BLE001  timing facts are telemetry; they must never fail the request
        log.warning("could not read gateway timings", exc_info=True)


def _gateway_error(model: str, exc: Exception) -> LLMError:
    """A LiteLLM failure as the typed error workers already understand."""
    from app.services.litellm_client import LiteLLMAuthError, LiteLLMRejected, LiteLLMUnsupported

    detail = f"LiteLLM request failed for model={model}: {exc}"
    status = getattr(exc, "http_status", None)
    text = str(exc)
    if isinstance(exc, LiteLLMAuthError):
        return LLMConfigError(f"LiteLLM refused the API key saved in AI Orchestration → Models: {text}")
    if status == 429:
        limit = getattr(exc, "rate_limit", None)
        if getattr(limit, "kind", None) == rate_limits.TOO_LARGE:
            # larger than the model's whole per-minute limit (Groq): no wait makes it fit, so it is never retried
            detail += (" — waiting cannot help: shorten the prompt (a prompt token budget) or use a model with a "
                       "higher limit")
        return LLMRateLimitError(detail, rate_limit=limit)
    if status in (408, 504) or "did not answer in time" in text or "Timeout" in text:
        return LLMTimeoutError(detail)
    if status == 413 or CONTEXT_OVERFLOW.search(text):
        return LLMContextOverflowError(detail)
    if isinstance(exc, LiteLLMUnsupported) or (isinstance(exc, LiteLLMRejected) and re.search(
            r"invalid model|model not found|no deployments|not allowed to access model", text, re.IGNORECASE)):
        return LLMConfigError(detail)
    return LLMError(detail)


def _workflow_trace(call: LLMCall) -> Any:
    """The current trace narrowed to the call's job or application. Telemetry only: never changes the call."""
    entity = call.entity_id
    if call.workflow_type == "job" and entity is not None:
        return tracing.trace(job_id=entity)
    if call.workflow_type == "application" and entity is not None:
        return tracing.trace(application_id=entity)
    return contextlib.nullcontext()


class LLMClient:
    """Calls the AI provider chosen in a user's settings, with that user's own API key."""

    def __init__(self, overrides: dict[str, Any] | None = None, keys: dict[str, str | None] | None = None,
                 user_id: int | None = None, gateway: tuple[str | None, str | None] | None = None):
        self.settings = get_settings()
        self.overrides = overrides or {}
        self.keys = keys or {}
        self.user_id = user_id
        # (LiteLLM base URL, API key) for gateway mode; None outside it.
        self.gateway = gateway

    @classmethod
    def for_user(cls, db: Session, user_id: int | None = None) -> LLMClient:
        """A client for the user the session belongs to (or an explicit user)."""
        from app.services.user_settings import ai_overrides, user_keys

        owner = user_id if user_id is not None else db.info.get("user_id")
        overrides = ai_overrides(db, user_id)
        gateway = None
        if resolve_ai_config(get_settings(), overrides)["llm_provider"] == "litellm" and owner is not None:
            from app.services.ai_model_config import gateway_settings

            gateway = gateway_settings(db, owner)
        return cls(overrides=overrides, keys=user_keys(db, user_id), user_id=owner, gateway=gateway)

    def _config(self) -> dict[str, Any]:
        return resolve_ai_config(self.settings, self.overrides)

    def _timeout(self) -> int:
        return int(getattr(self.settings, "llm_request_timeout_seconds", 120) or 120)

    def _retries(self) -> int:
        return int(getattr(self.settings, "llm_max_retries", 1) or 0)

    def generate_prompt_json(self, prompt: Prompt, call: LLMCall | None = None) -> dict[str, Any]:
        """Run a prompt built in app.prompts and return its JSON result."""
        return self.generate_prompt(prompt, call).data

    def generate_prompt(self, prompt: Prompt, call: LLMCall | None = None) -> LLMResult:
        """Run a prompt built in app.prompts. The stage comes from the prompt's marker, never from its data.

        The answer is checked against the prompt's output contract before the call's own validator sees it. A
        contract failure gets one retry with a schema correction; the retry is never escalated a second time.

        The call runs under the trace already open, narrowed to the job or application it is for (same trace id), so
        the gateway's external-call row carries that job too and the job's journey finds it (orchestration.journey).
        """
        call = call or LLMCall()
        with _workflow_trace(call):
            return self._generate_prompt(prompt, call)

    def _generate_prompt(self, prompt: Prompt, call: LLMCall) -> LLMResult:
        task = MARKER_TASKS.get(prompt.marker, "default")
        call.contract = prompt.output_schema
        try:
            return self._generate(prompt.system, prompt.user, task, prompt_name=prompt.name,
                                  prompt_version=prompt.version, call=call)
        except LLMResponseError as exc:
            if (prompt.output_schema is None or getattr(exc, "_jobhunt_escalated", False)
                    or call.schema_corrections >= MAX_SCHEMA_CORRECTIONS):
                raise
            retry = replace(call, schema_corrections=call.schema_corrections + 1, no_escalation=True,
                            structured_output_retry_count=call.structured_output_retry_count + 1)
            return self._generate(prompt.system, prompt.user + correction_note(prompt.output_schema, exc), task,
                                  prompt_name=prompt.name, prompt_version=prompt.version, call=retry)

    def generate_json(self, system: str, user: str) -> dict[str, Any]:
        return self._generate(system, user, _task_from_prompt(user)).data

    def model_for(self, task: str) -> tuple[str, str]:
        """The provider and model a stage would use."""
        config = self._config()
        if config["llm_provider"] == "litellm":
            return "litellm", model_resolution.get(self.user_id, STAGE_WORKERS.get(task)).model or ""
        return config["llm_provider"], config["stage_models"].get(task) or config["model"]

    def route_for(self, worker_type: str) -> dict[str, Any]:
        """Where this worker's next request goes, without calling anything: the route (LITELLM or a direct provider),
        the model, the configured assignment and, through LiteLLM, the model failover has moved it from (if any)."""
        config = self._config()
        if config["llm_provider"] == "litellm":
            primary = model_resolution.get(self.user_id, worker_type)
            resolved = failover.apply(primary, self.user_id)
            return {"route": "LITELLM", "model": resolved.model, "primary": primary.model, "mode": resolved.mode,
                    "configured": resolved.configured, "failed_over_from": resolved.failed_over_from,
                    "backups": resolved.chain()}
        stage = next((name for name, worker in STAGE_WORKERS.items() if worker == worker_type), None)
        model = config["stage_models"].get(stage) or config["model"]
        return {"route": str(config["llm_provider"]).upper(), "model": model, "primary": model, "mode": None,
                "configured": None, "failed_over_from": None, "backups": []}

    def _generate(self, system: str, user: str, task: str, *, prompt_name: str | None = None,
                  prompt_version: str | None = None, call: LLMCall | None = None) -> LLMResult:
        config = self._config()
        provider = config["llm_provider"]
        if provider == "litellm":
            try:
                return self._gateway(system, user, task, prompt_name, prompt_version, call or LLMCall())
            except RateLimitedCooldown as waiting:
                raise _rate_limited(waiting) from waiting
        model = config["stage_models"].get(task) or config["model"]
        attempt = Attempt(provider=provider, requested_model=model, user_id=self.user_id, call=call or LLMCall(),
                          prompt_name=prompt_name, prompt_version=prompt_version, system=system, user=user)
        started = time.monotonic()
        # A provider that just rate limited this account is left alone (app.services.backoff). The wait is
        # reported as a rate limit, so the ranker and the workers retry it later exactly as they already do.
        try:
            return self._instrumented(system, user, task, model, config, attempt, prompt_name, prompt_version, started)
        except RateLimitedCooldown as waiting:
            raise _rate_limited(waiting) from waiting

    def _instrumented(self, system: str, user: str, task: str, model: str, config: dict[str, Any], attempt: Attempt,
                      prompt_name: str | None, prompt_version: str | None, started: float) -> LLMResult:
        """The provider SDKs own their transport, so the generic external-call row is recorded here, at the one
        boundary every provider passes through. LLMRequestLog then points at it (external_request_id), giving
        the External APIs tab and the LLM tab two views of the same request instead of two records of it."""
        provider = attempt.provider
        with external_operation(provider.upper(), service="llm", operation="chat_completion",
                                user_id=self.user_id, attempt=attempt.call.attempt,
                                workflow_type=attempt.call.workflow_type,
                                workflow_entity_id=attempt.call.entity_id,
                                metadata={"model": model, "stage": llm_telemetry.STAGE_BY_PROMPT.get(prompt_name or "", "OTHER"),
                                          "prompt_name": prompt_name, "prompt_version": prompt_version}) as external:
            attempt.external_request_id = external.request_id
            return self._run(system, user, task, model, config, attempt, external, started)

    @staticmethod
    def _validated(result: LLMResult, attempt: Attempt) -> LLMResult:
        """Contract first, then the service's validator. Records the outcome on the attempt; raises on failure."""
        call = attempt.call
        if call.contract is None and call.validate is None:
            return result
        validated: Any = None
        unwrapped = unwrap(call.contract, result.data)
        if unwrapped is not result.data:
            result = replace(result, data=unwrapped)  # the service and the stored result see the same answer
        try:
            if call.contract is not None:
                validated = check_contract(call.contract, result.data)
            if call.validate is not None:
                validated = call.validate(result.data)
        except LLMSchemaError as exc:
            attempt.structured_output_valid, attempt.validation_failure_reason = False, exc.reason
            raise
        except Exception as exc:
            attempt.structured_output_valid, attempt.validation_failure_reason = False, "SEMANTIC_VALIDATION"
            try:
                exc._jobhunt_validation = True  # lets _gateway tell a schema failure from other errors
            except AttributeError:
                pass
            raise
        attempt.structured_output_valid = True
        return replace(result, validated=validated)

    def _run(self, system: str, user: str, task: str, model: str, config: dict[str, Any], attempt: Attempt,
             external: Any, started: float) -> LLMResult:
        provider = attempt.provider
        failure: BaseException | None = None
        failure_type: str | None = None
        try:
            result = self._dispatch(provider, system, user, task, model, config, attempt)
            return self._validated(result, attempt)
        except Exception as exc:
            failure = exc
            failure_type = error_type(exc, validation=attempt.validation_failure_reason == "SEMANTIC_VALIDATION")
            if isinstance(exc, LLMResponseError):
                attempt.structured_output_valid = False
                attempt.validation_failure_reason = attempt.validation_failure_reason or exc.reason
            raise
        finally:
            # Tokens travel to the generic row too; cost does not. An LLM call's cost lives only on its
            # llm_request_logs row, so a total that adds external rows skips those an LLM row points at
            # (external_request_id) and nothing is counted twice. `not_sent` requests (missing key, refused before
            # the network) are marked as configuration failures rather than counted as provider calls.
            external.note(answered_model=attempt.model, sent=attempt.sent,
                          input_tokens=attempt.usage.input_tokens if attempt.usage else None,
                          output_tokens=attempt.usage.output_tokens if attempt.usage else None)
            if not attempt.sent and failure is not None:
                external.http_status_code = None
                external.metadata["not_sent"] = True
            llm_telemetry.record(attempt, latency_ms=int((time.monotonic() - started) * 1000), error=failure,
                                 error_type=failure_type)

    def _dispatch(self, provider: str, system: str, user: str, task: str, model: str, config: dict[str, Any],
                  attempt: Attempt) -> LLMResult:
        # Direct providers get JSON mode, not a JSON Schema: support varies by model there, and the contract is
        # always checked here. Anthropic has no JSON mode; the contract rides in the prompt only.
        attempt.schema_enforcement = ("none" if attempt.call.contract is None and provider == "mock"
                                      else "prompt_only" if provider in ("anthropic", "mock") else "json_mode")
        if provider == "mock":
            attempt.sent, attempt.model = True, "mock"
            data = self._mock(task, user)
            attempt.response_text = json.dumps(data)
            return LLMResult(data, "mock", "mock")
        if provider == "openai":
            return self._openai(system, user, model, attempt)
        if provider == "anthropic":
            return self._anthropic(system, user, model, attempt)
        if provider == "gemini":
            return self._gemini(system, user, model, attempt)
        if provider == "openrouter":
            return self._openrouter(system, user, task=task, model=model, require_free=config["require_free"],
                                    attempt=attempt)
        raise LLMConfigError(f"Unsupported LLM_PROVIDER={provider}")

    # ───────────── LiteLLM gateway mode ─────────────

    def _gateway(self, system: str, user: str, task: str, prompt_name: str | None, prompt_version: str | None,
                 call: LLMCall) -> LLMResult:
        """One logical request: the worker's model, then — only when its escalation policy says so — one
        escalated request to a stronger model. Each is recorded as its own telemetry row."""
        worker_type = call.worker_type or worker_for_prompt(prompt_name) or STAGE_WORKERS.get(task)
        resolved = call.resolved or model_resolution.get(self.user_id, worker_type)
        # What the request will take off a per-minute token window (app.services.token_pacing): a model whose window
        # cannot take it this minute moves behind its backups, like a resting one.
        need = (llm_telemetry.estimate_tokens(system) + llm_telemetry.estimate_tokens(user)
                + (resolved.max_output_tokens or 0))
        resolved = failover.apply(resolved, self.user_id, tokens_needed=need)
        # Every model the worker could use is rate limited (its own 429 or its provider's): send nothing, and hold the
        # task until the first one clears. Paced, so the caller gives the attempt back (ranker._paced).
        held = failover.held_seconds(resolved, self.user_id)
        if held:
            raise RateLimitedCooldown(f"LITELLM:{resolved.model}", held,
                                      f"every model {worker_label(resolved.worker_type)} can use is rate limited; "
                                      f"nothing was sent, trying again in {held}s", paced=True)
        try:
            first = self._gateway_attempt(system, user, resolved.model, resolved, call, prompt_name, prompt_version)
        except Exception as exc:
            schema_failed = isinstance(exc, LLMResponseError) or getattr(exc, "_jobhunt_validation", False)
            reason = (model_resolution.escalation_reason(resolved, schema_failed=schema_failed)
                      if schema_failed and not call.no_escalation else None)
            if reason is None:
                raise
            target = failover.escalation_target(resolved, self.user_id, tokens_needed=need)
            if target is None:  # nowhere to escalate to: the schema failure goes to its correction retry
                self._escalation_skipped(resolved, call, reason, "RATE_LIMITED")
                raise
            try:
                return self._gateway_attempt(system, user, target, resolved, call, prompt_name,
                                             prompt_version, escalation=reason)
            except Exception as escalated:
                if _unavailable(escalated):  # the stronger model never answered: the original failure stands
                    self._escalation_skipped(resolved, call, reason, error_type(escalated), target)
                    raise exc from None
                try:
                    escalated._jobhunt_escalated = True  # a stronger model already had its try: no correction retry
                except AttributeError:
                    pass
                raise
        reason = (None if call.no_escalation
                  else model_resolution.escalation_reason(resolved, data=first.data, manual=call.escalate))
        if reason is None:
            return first
        target = failover.escalation_target(resolved, self.user_id, tokens_needed=need)
        if target is None and reason != "MANUAL":
            return self._keep_first(first, resolved, call, reason, "RATE_LIMITED")
        try:
            return self._gateway_attempt(system, user, target or resolved.escalation.model, resolved, call,
                                         prompt_name, prompt_version, escalation=reason,
                                         previous_confidence=model_resolution.confidence_of(first.data))
        except Exception as escalated:
            # A valid first answer is kept when the second opinion could not be had (invariant 9: preserve good
            # results). A person's MANUAL escalation, or our own configuration error, is reported instead.
            if reason == "MANUAL" or isinstance(escalated, LLMConfigError):
                raise
            return self._keep_first(first, resolved, call, reason, error_type(escalated), target)

    def _keep_first(self, first: LLMResult, resolved: Any, call: LLMCall, reason: str, cause: str,
                    model: str | None = None) -> LLMResult:
        self._escalation_skipped(resolved, call, reason, cause, model)
        routing = {**(first.routing or {}), "escalation_skipped": {
            "reason": reason, "cause": cause, "model": model or resolved.escalation.model}}
        return replace(first, routing=routing)

    def _escalation_skipped(self, resolved: Any, call: LLMCall, reason: str, cause: str,
                            model: str | None = None) -> None:
        """Monitoring → Logs: the answer stands without the second opinion its policy asked for, and why."""
        from app.services import system_log

        target = model or (resolved.escalation.model if resolved.escalation else None)
        system_log.log_event(
            message=(f"{worker_label(resolved.worker_type)}: escalation ({reason}) to {target} skipped — "
                     + ("it and its fallbacks are rate limited" if cause == "RATE_LIMITED" else f"it failed ({cause})")
                     + "; the first answer stands"),
            category="LLM", level="INFO", user_id=self.user_id, operation="escalation_skipped", status=cause,
            workflow=call.workflow_type, entity_type=call.workflow_type, entity_id=call.entity_id,
            details={"worker_type": resolved.worker_type, "escalation_reason": reason, "model": target,
                     "fallbacks": list(resolved.escalation.fallbacks) if resolved.escalation else []})

    def _gateway_attempt(self, system: str, user: str, model: str | None, resolved: Any, call: LLMCall,
                         prompt_name: str | None, prompt_version: str | None, *, escalation: str | None = None,
                         previous_confidence: float | None = None) -> LLMResult:
        attempt = Attempt(provider="litellm", requested_model=model, user_id=self.user_id, call=call,
                          prompt_name=prompt_name, prompt_version=prompt_version, system=system, user=user,
                          worker_type=resolved.worker_type, model_mode=resolved.mode,
                          configured_model=resolved.configured, config_version=resolved.config_version,
                          escalated=escalation is not None, escalation_reason=escalation,
                          routing_cause="FAILOVER" if resolved.failed_over_from and escalation is None else None,
                          failed_over_from=resolved.failed_over_from if escalation is None else None,
                          context_window=resolved.context_limit(model))
        started = time.monotonic()
        failure: BaseException | None = None
        failure_type: str | None = None
        try:
            result = self._litellm_request(system, user, model, resolved, attempt, call, escalation,
                                           previous_confidence)
            if call.contract is None and call.validate is None:
                if attempt.structured_output_valid is None and resolved.structured_output_required:
                    attempt.structured_output_valid = True  # parsed as a JSON object; no finer schema was given
                return result
            return self._validated(result, attempt)
        except Exception as exc:
            failure = exc
            failure_type = error_type(exc, validation=attempt.validation_failure_reason == "SEMANTIC_VALIDATION")
            if isinstance(exc, LLMResponseError):
                attempt.structured_output_valid = False
                attempt.validation_failure_reason = attempt.validation_failure_reason or exc.reason
            raise
        finally:
            llm_telemetry.record(attempt, latency_ms=int((time.monotonic() - started) * 1000), error=failure,
                                 error_type=failure_type)

    def _litellm_request(self, system: str, user: str, model: str | None, resolved: Any, attempt: Attempt,
                         call: LLMCall, escalation: str | None, previous_confidence: float | None) -> LLMResult:
        from app.services.litellm_client import LiteLLMAPI, LiteLLMError

        base, key = self.gateway or (None, None)
        if not base:
            raise LLMConfigError("LiteLLM gateway mode is on but no LiteLLM base URL is configured: add it in "
                                 "AI Orchestration → Models → LiteLLM connection")
        if not resolved.enabled:
            raise LLMConfigError(f"{worker_label(resolved.worker_type)} is disabled in AI Orchestration → Models")
        if not model:
            raise LLMConfigError(f"{worker_label(resolved.worker_type)} has no model: "
                                 f"{resolved.note or 'nothing is assigned'} (AI Orchestration → Models)")
        budget = resolved.context_window
        estimated = llm_telemetry.estimate_tokens(system) + llm_telemetry.estimate_tokens(user)
        if budget and estimated > budget:
            raise LLMContextOverflowError(f"The {worker_label(resolved.worker_type)} request needs about {estimated} "
                                          f"tokens but its context window is set to {budget}")
        params, withheld = model_resolution.request_params(resolved, model, call.contract)
        attempt.schema_enforcement = model_resolution.schema_enforcement(params)
        timeout = resolved.timeout_seconds or self._timeout()
        retries = resolved.retry_count if resolved.retry_count is not None else self._retries()
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system + "\nReturn only valid JSON. No markdown fences."},
                         {"role": "user", "content": user}],
            **params, "timeout": timeout, "num_retries": retries,
        }
        # Infrastructure fallback is LiteLLM's (a client-side fallback on this request): the worker's fallback and
        # secondary models, already ordered by route health (app.services.failover). An escalated request has
        # already been judged; it gets no silent fallback to a weaker model.
        if resolved.chain() and escalation is None:
            body["fallbacks"] = resolved.chain()
            attempt.fallbacks_sent = True
        if resolved.send_metadata:
            body["metadata"] = model_resolution.request_metadata(
                resolved, workflow_type=call.workflow_type, entity_id=call.entity_id, attempt=call.attempt,
                trace_id=tracing.current_trace_id(), previous_confidence=previous_confidence, escalation=escalation)
        http_timeout = min(3600, timeout * (retries + 1) * (2 if "fallbacks" in body else 1) + 15)
        external = {"model": model, "stage": llm_telemetry.STAGE_BY_PROMPT.get(attempt.prompt_name or "", "OTHER"),
                    "worker_type": resolved.worker_type, "prompt_name": attempt.prompt_name,
                    "prompt_version": attempt.prompt_version, "params_withheld": withheld or None,
                    "escalation": escalation, "tokens_needed": estimated + int(params.get("max_tokens") or 0)}
        waiting = time.monotonic()
        with model_resolution.concurrency_slot(resolved, self.user_id, timeout) as free:
            if resolved.max_concurrency:  # without a limit there is no queue to measure
                attempt.queue_wait_ms = int((time.monotonic() - waiting) * 1000)
            if not free:
                raise LLMRateLimitError(f"{worker_label(resolved.worker_type)} already has {resolved.max_concurrency} "
                                        "requests in flight (max concurrency)")
            with LiteLLMAPI(base, key, user_id=self.user_id, service="llm", timeout=http_timeout) as api:
                attempt.sent = True
                try:
                    chat = api.chat(body, metadata={k: v for k, v in external.items() if v is not None})
                except RateLimitedCooldown:
                    attempt.sent = False
                    raise
                except LiteLLMError as exc:
                    attempt.external_request_id = api.client.last_request_id
                    raise _gateway_error(model, exc) from exc
        attempt.external_request_id = chat.request_id
        facts = chat.litellm
        choice = (chat.body.get("choices") or [{}])[0] if isinstance(chat.body, dict) else {}
        attempt.response_text = ((choice or {}).get("message") or {}).get("content") or "{}"
        attempt.model = facts.get("model_name") or _answered_model_name(chat.body) or model
        attempt.litellm_call_id = facts.get("call_id")
        _gateway_timings(attempt, facts)
        tier = chat.body.get("service_tier") if isinstance(chat.body, dict) else None
        attempt.service_tier = tier if isinstance(tier, str) else None
        attempt.model_group = facts.get("model_group")
        if facts.get("attempted_fallbacks") is not None:
            attempt.fallback_used = facts["attempted_fallbacks"] > 0
        attempt.routing_tier = model_resolution.routing_tier(resolved, model, attempt.model_group, attempt.model)
        attempt.usage = _dict_usage(chat.body.get("usage") if isinstance(chat.body, dict) else None,
                                    facts.get("response_cost"))
        data = _extract_json(attempt.response_text)
        attempt.confidence = model_resolution.confidence_of(data)
        routing = {"worker_type": resolved.worker_type, "mode": resolved.mode, "configured": resolved.configured,
                   "requested_model": model, "model_group": attempt.model_group, "underlying_model": attempt.model,
                   "routing_tier": attempt.routing_tier, "fallback_used": attempt.fallback_used,
                   "escalated": escalation is not None, "escalation_reason": escalation,
                   "litellm_call_id": attempt.litellm_call_id, "latency_ms": chat.latency_ms,
                   "config_version": resolved.config_version, "params_withheld": withheld}
        return LLMResult(data, "litellm", attempt.model, attempt.usage, routing=routing)

    @staticmethod
    def _openai_usage(response: Any) -> Usage | None:
        usage = getattr(response, "usage", None)
        prompt, completion = _int(getattr(usage, "prompt_tokens", None)), _int(getattr(usage, "completion_tokens", None))
        if usage is None or prompt is None:
            return None
        cached = _int(getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None))
        extra = getattr(usage, "model_extra", None) or {}
        cost = getattr(usage, "cost", None) if getattr(usage, "cost", None) is not None else extra.get("cost")
        return Usage(input_tokens=prompt, output_tokens=completion, cache_read_tokens=cached,
                     uncached_input_tokens=prompt - (cached or 0),
                     reported_cost=float(cost) if isinstance(cost, (int, float)) else None,
                     reasoning_tokens=_int(getattr(getattr(usage, "completion_tokens_details", None),
                                                   "reasoning_tokens", None)))

    def _openai(self, system: str, user: str, model: str, attempt: Attempt) -> LLMResult:
        if not self.keys.get("openai"):
            raise LLMConfigError("OPENAI_API_KEY is missing: add your OpenAI API key in Settings")
        from openai import OpenAI

        client = OpenAI(api_key=self.keys["openai"], timeout=self._timeout(), max_retries=self._retries())
        attempt.sent = True
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            raise _provider_error("OpenAI", model, exc) from exc
        attempt.model, attempt.usage = _answered_model(response, model), self._openai_usage(response)
        tier = getattr(response, "service_tier", None)
        attempt.service_tier = tier if isinstance(tier, str) else None
        if attempt.usage:
            attempt.usage.reported_cost = None  # OpenAI does not report cost; the catalog prices it
        attempt.response_text = response.choices[0].message.content or "{}"
        return LLMResult(_extract_json(attempt.response_text), "openai", attempt.model, attempt.usage)

    def _anthropic(self, system: str, user: str, model: str, attempt: Attempt) -> LLMResult:
        if not self.keys.get("anthropic"):
            raise LLMConfigError("ANTHROPIC_API_KEY is missing: add your Anthropic API key in Settings")
        from anthropic import Anthropic

        client = Anthropic(api_key=self.keys["anthropic"], timeout=self._timeout(), max_retries=self._retries())
        attempt.sent = True
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=5000,
                system=system + "\nReturn only valid JSON. No markdown fences.",
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:
            raise _provider_error("Anthropic", model, exc) from exc
        attempt.model = _answered_model(msg, model)
        usage = getattr(msg, "usage", None)
        uncached = _int(getattr(usage, "input_tokens", None))
        if uncached is not None:
            read = _int(getattr(usage, "cache_read_input_tokens", None))
            write = _int(getattr(usage, "cache_creation_input_tokens", None))
            attempt.usage = Usage(input_tokens=uncached + (read or 0) + (write or 0),
                                  output_tokens=_int(getattr(usage, "output_tokens", None)), cache_read_tokens=read,
                                  cache_write_tokens=write, uncached_input_tokens=uncached)
        attempt.response_text = "".join(getattr(block, "text", "") for block in msg.content)
        return LLMResult(_extract_json(attempt.response_text), "anthropic", attempt.model, attempt.usage)

    def _gemini(self, system: str, user: str, model: str, attempt: Attempt) -> LLMResult:
        if not self.keys.get("gemini"):
            raise LLMConfigError("GEMINI_API_KEY is missing: add your Gemini API key in Settings")
        from google import genai

        client = genai.Client(api_key=self.keys["gemini"], http_options={"timeout": self._timeout() * 1000})
        attempt.sent = True
        try:
            response = client.models.generate_content(
                model=model, contents=user,
                config={"system_instruction": system, "response_mime_type": "application/json"})
        except Exception as exc:
            raise _provider_error("Gemini", model, exc) from exc
        version = getattr(response, "model_version", None)
        attempt.model = version if isinstance(version, str) and version else model
        meta = getattr(response, "usage_metadata", None)
        prompt = _int(getattr(meta, "prompt_token_count", None))
        if prompt is not None:
            cached = _int(getattr(meta, "cached_content_token_count", None))
            attempt.usage = Usage(input_tokens=prompt, output_tokens=_int(getattr(meta, "candidates_token_count", None)),
                                  cache_read_tokens=cached, uncached_input_tokens=prompt - (cached or 0))
        attempt.response_text = getattr(response, "text", None) or "{}"
        return LLMResult(_extract_json(attempt.response_text), "gemini", attempt.model, attempt.usage)

    def _openrouter(self, system: str, user: str, task: str, model: str, require_free: bool,
                    attempt: Attempt) -> LLMResult:
        if not self.keys.get("openrouter"):
            raise LLMConfigError("OPENROUTER_API_KEY is missing: add your OpenRouter API key in Settings")

        primary = model
        fallbacks = _csv_models(self.settings.openrouter_fallback_models)
        all_models = [primary, *fallbacks]

        # Avoid accidental spend when the user intends to run on free inference. Checked before any request.
        if require_free:
            paid = [name for name in all_models if not _is_free_openrouter_model(name)]
            if paid:
                raise LLMConfigError(
                    "OPENROUTER_REQUIRE_FREE=true but paid/non-free model(s) were configured: "
                    + ", ".join(paid)
                    + ". Use openrouter/free or a :free model, or turn off the free-only guard in Settings."
                )

        from openai import OpenAI

        headers: dict[str, str] = {"X-Title": self.settings.openrouter_app_name}
        if self.settings.openrouter_site_url:
            headers["HTTP-Referer"] = self.settings.openrouter_site_url

        client = OpenAI(
            api_key=self.keys["openrouter"],
            base_url=self.settings.openrouter_base_url,
            default_headers=headers,
            timeout=self._timeout(),
            max_retries=self._retries(),
        )

        # OpenRouter extensions: model-level fallbacks, and usage accounting so each response reports its cost.
        extra_body: dict[str, Any] = {"usage": {"include": True}}
        if fallbacks:
            extra_body["models"] = fallbacks

        request_kwargs: dict[str, Any] = {
            "model": primary,
            "messages": [
                {"role": "system", "content": system + "\nReturn only valid JSON. No markdown fences."},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self.settings.openrouter_max_tokens,
            "extra_body": extra_body,
        }

        attempt.sent = True
        try:
            response = client.chat.completions.create(**request_kwargs)
        except Exception as exc:
            raise _provider_error(f"OpenRouter (task={task})", primary, exc) from exc

        attempt.model, attempt.usage = _answered_model(response, primary), self._openai_usage(response)
        attempt.response_text = response.choices[0].message.content or "{}"
        return LLMResult(_extract_json(attempt.response_text), "openrouter", attempt.model, attempt.usage)

    def _mock(self, task: str, user: str) -> dict[str, Any]:
        # Useful for local smoke tests without API spend.
        if task == "rank":
            job_id = re.search(r"^JOB ID: (\d+)$", user, re.MULTILINE)
            return {
                "job_id": int(job_id.group(1)) if job_id else 0,
                "overall_fit_score": 80,
                "confidence": 0.8,
                "candidate_fit_score": 82,
                "resume_fit_score": 74,
                "seniority_match_score": 80,
                "skills_match_score": 80,
                "leadership_match_score": 70,
                "domain_match_score": 75,
                "location_match_score": 90,
                "strong_matches": ["Python", "backend systems"],
                "partial_matches": ["Cloud infrastructure"],
                "missing_requirements": [],
                "fatal_gaps": [],
                "resume_gaps": ["Production ownership is not quantified on the resume"],
                "relevant_evidence_ids": [],
                "recommended_resume_focus": ["Lead with production ownership and measurable outcomes"],
                "qualification_recommendation": "QUALIFY",
                "reasoning_summary": "Good overall fit based on the supplied profile.",
            }
        if task == "review":
            return {"review_score": 86, "confidence": 0.9, "recommendation": "PASS", "unsupported_claims": [],
                    "overstated_claims": [], "unaddressed_requirements": [], "risks": ["Keep bullets concise"],
                    "strengths": ["Relevant evidence", "Clear outcomes"], "summary": "Mock review."}
        if task == "followup":
            return {"draft": "Hi, I wanted to follow up on my application and reiterate my interest in the role. Best regards."}
        if task == "advisor":
            return {"summary": "Mock analysis of the supplied usage aggregates.", "recommendations": [],
                    "estimated_savings": "unknown"}
        if "PARSE_JOB_DESCRIPTION" in user.split("\n", 1)[0]:
            from types import SimpleNamespace

            from app.services.job_description import deterministic_parse

            facts = json.loads(re.search(r"^JOB POSTING:\n(.*)$", user, re.MULTILINE).group(1))
            text = user.split("<untrusted_job_description>", 1)[-1].rsplit("</untrusted_job_description>", 1)[0]
            job = SimpleNamespace(**{field: facts.get(field) for field in
                                     ("title", "company", "location", "remote_type", "job_type", "seniority")},
                                  source_board=None, source="mock")
            return deterministic_parse(job, text.strip())
        if "CLASSIFY_JOB_DOMAIN" in user.split("\n", 1)[0]:
            return {"classification": "UNCERTAIN", "reason": "Mock: no model read this posting.", "matched_skills": [],
                    "irrelevance_evidence": []}
        if "RECOVER_JOB_DESCRIPTION" in user.split("\n", 1)[0]:
            from app.services.jd_extractors import likely_blocks

            raw = user.split("<untrusted_page_blocks>", 1)[-1].rsplit("</untrusted_page_blocks>", 1)[0].strip()
            chosen = likely_blocks(json.loads(raw) if raw.startswith("[") else [])
            return {"is_job_posting": bool(chosen), "selected_block_ids": chosen, "confidence": 0.8 if chosen else 0.1,
                    "explanation": "Mock: the block with the most job-posting structure."}
        if "COVER_LETTER" in user.split("\n", 1)[0]:
            company = re.search(r'^JOB:\n.*?"company": "([^"]*)"', user, re.MULTILINE | re.DOTALL)
            return {"cover_letter": f"Dear Hiring Team,\n\nI am applying for this role at "
                                    f"{company.group(1) if company else 'your company'}. I have built and run the "
                                    "kind of systems the posting describes.\n\nIn my last role I led the work "
                                    "behind it and cut production incidents by 45%.\n\nI would bring the same "
                                    "focus here, and I am happy to talk whenever suits you.\n\nSincerely,\n"
                                    "The Candidate",
                    "selected_evidence_ids": []}
        if "PARSE_RESUME" in user.split("\n", 1)[0]:
            from app.services.structured_resume import parse_deterministic

            text = user.split("<untrusted_resume>", 1)[-1].rsplit("</untrusted_resume>", 1)[0].strip()
            return {"structured_resume": parse_deterministic(text)}
        if "EXTRACT_EVIDENCE" in user.split("\n", 1)[0]:
            lines = [line.strip(" -•*\t") for line in user.split("MASTER RESUME:", 1)[-1].splitlines()]
            claims = [line for line in lines if len(line) > 25 and re.search(r"\d", line)][:12]
            return {"claims": [{"claim": c, "company": "", "role": "", "skills": []} for c in claims]}
        if "TAILOR_RESUME" in user.split("\n", 1)[0]:
            return self._mock_tailoring(user)
        if "ANALYZE_APPLICATION_EMAIL" in user.split("\n", 1)[0]:
            # No spend and no guesses: a neutral update that a person reviews (the deterministic rules come first).
            return {"event_type": "GENERAL_RECRUITING_UPDATE", "confidence": 0.5,
                    "summary": "Mock: an update about this application.", "application_stage_detected": "UNKNOWN",
                    "requires_user_action": False, "action_items": [], "interview": None, "assessment": None,
                    "recruiter": None, "important_facts": [], "recommended_tracker_action": "NO_CHANGE",
                    "needs_review": True}
        if "MATCH_APPLICATION_EMAIL" in user.split("\n", 1)[0]:
            return {"matched_application_id": None, "confidence": 0.0, "reason": "Mock: no explicit match."}
        if "SYNTHESIZE_COMPANY_INSIGHTS" in user.split("\n", 1)[0]:
            return self._mock_company_insights(user)
        if "PREPARE_INTERVIEW_INSIGHTS" in user.split("\n", 1)[0]:
            return self._mock_interview_insights(user)
        if "PREPARE_APPLICATION_FORM" in user.split("\n", 1)[0]:
            return self._mock_application_form(user)
        if "NAVIGATE_APPLICATION_FORM" in user.split("\n", 1)[0]:
            return self._mock_browser_navigation(user)
        return {"summary": "Mock answer."}

    @staticmethod
    def _mock_browser_navigation(user: str) -> dict[str, Any]:
        """No spend, no guesses: the next RESOLVED field that has its own element and is not filled yet, else done."""
        def part(name: str) -> Any:
            found = re.search(rf"^{name}:\n(.*)$", user, re.MULTILINE)
            return json.loads(found.group(1)) if found else []

        def wrapped(tag: str) -> list[dict[str, Any]]:
            raw = user.split(f"<{tag}>", 1)[-1].split(f"</{tag}>", 1)[0].strip() if f"<{tag}>" in user else ""
            return json.loads(raw) if raw else []

        elements = {e.get("key"): e for e in part("ELEMENTS")}
        fields = part("FIELDS")
        field_labels = {f.get("ref"): (f.get("label") or "").lower() for f in wrapped("untrusted_field_labels")}
        if not fields:  # nothing to fill yet: open the form with a control that cannot submit
            labels = {e.get("key"): (e.get("label") or "").lower() for e in wrapped("untrusted_element_labels")}
            controls = [k for k, e in elements.items() if e.get("tag") in ("a", "button") and not e.get("submit_like")]
            for wanted in (("decline", "reject", "necessary only"), ("apply", "application")):
                for key in controls:
                    if labels.get(key, "").startswith(wanted):
                        return {"action": "click_control", "element_key": key, "step_note": "Mock: open the form."}
            return {"action": "done", "step_note": "Mock: nothing to open."}
        verbs = {"select": "select_option", "combobox": "select_option", "checkbox": "check_box", "radio": "check_box",
                 "file": "upload_file"}
        for field in fields:
            key = str(field.get("ref") or "").removeprefix("bu_")
            if field.get("status") != "RESOLVED" or field.get("filled") or key not in elements:
                continue
            action = verbs.get(field.get("kind"), "fill_field")
            out = {"action": action, "element_key": key, "answer_ref": field["ref"],
                   "step_note": "Mock: the next resolved field."}
            if action == "upload_file":
                out["file_ref"] = "cover_letter" if "cover" in field_labels.get(field["ref"], "") else "resume"
            return out
        return {"action": "done", "step_note": "Mock: every resolved field is filled."}

    @staticmethod
    def _mock_application_form(user: str) -> dict[str, Any]:
        """Drafts made of the evidence it was given, word for word; every pasted question answered or handed back."""
        evidence = re.search(r"^VERIFIED EVIDENCE:\n(.*)$", user, re.MULTILINE)
        items = json.loads(evidence.group(1))[:2] if evidence else []
        raw = user.split("<untrusted_form_questions>", 1)[-1].rsplit("</untrusted_form_questions>", 1)[0].strip()
        pasted = json.loads(raw) if raw.startswith("[") else []
        task = re.search(r"^TASK:\n(.*)$", user, re.MULTILINE)
        predict = int(json.loads(task.group(1)).get("predict_up_to") or 0) if task else 0
        answer = " ".join(e.get("claim") or "" for e in items).strip()

        def draft(question: str, ref: str | None, category: str, why: str = "") -> dict[str, Any]:
            return {"form_ref": ref, "form_question": question, "form_category": category,
                    "form_decision": "DRAFT" if answer else "NEEDS_INPUT", "form_answer": answer or None,
                    "likely_because": why, "requirement_ids": [], "evidence_ids": [e["id"] for e in items],
                    "knowledge_ids": [], "form_note": "" if answer else "No verified evidence to draft from."}

        out = [draft(q["question"], q["ref"], "EXPERIENCE") for q in pasted if len(q.get("question") or "") >= 8]
        if predict:
            out.append(draft("Describe the experience that best prepares you for this role.", None, "EXPERIENCE",
                             "Forms for this kind of role usually ask for relevant experience."))
        return {"form_questions": out}

    @staticmethod
    def _mock_company_insights(user: str) -> dict[str, Any]:
        """Organises the research it is given and cites it; adds nothing the observations do not say."""
        raw = user.split("<untrusted_research>", 1)[-1].rsplit("</untrusted_research>", 1)[0].strip()
        observations = json.loads(raw) if raw.startswith("[") else []
        def of(category: str, *sources: str) -> list[dict[str, Any]]:
            return [o for o in observations if o.get("category") == category
                    and (not sources or o.get("source_type") in sources)]
        described = [o for o in of("COMPANY", "OFFICIAL_COMPANY", "JOB_DESCRIPTION")
                     if (o.get("data") or {}).get("field") in ("description", "posting_company_context", "mission")]
        culture = of("CULTURE")
        return {
            "company_summary": (described[0]["data"].get("text") or "")[:600] if described else "",
            "summary_source_ids": [described[0]["id"]] if described else [],
            "profile_facts": [],
            "official_values": [{"value": (o["data"].get("value") or "")[:120], "supporting_source_ids": [o["id"]]}
                                for o in of("VALUES", "OFFICIAL_COMPANY") if o["data"].get("value")][:10],
            "culture_themes": [{"theme": (o["data"].get("headline") or o["data"].get("pros") or "")[:120],
                                "sentiment": "MIXED", "supporting_source_ids": [o["id"]]}
                               for o in culture if o["data"].get("headline") or o["data"].get("pros")][:5],
            "interview_stages": [{"name": "Interview", "order": i + 1, "origin": "REPORTED",
                                  "supporting_source_ids": [o["id"]], "rationale": ""}
                                 for i, o in enumerate(of("INTERVIEW_PROCESS")[:1])],
            "reported_questions": [{"question": o["data"]["question"][:300], "category": "OTHER", "stage": "UNKNOWN",
                                    "supporting_source_ids": [o["id"]]}
                                   for o in of("INTERVIEW_QUESTION") if len(o["data"].get("question") or "") >= 8][:20],
            "risks_and_unknowns": [] if of("INTERVIEW_QUESTION") else [
                {"kind": "UNKNOWN", "text": "No interview reports were found for this company.",
                 "supporting_source_ids": []}],
            "research_confidence": 0.6 if observations else 0.1,
        }

    @staticmethod
    def _mock_interview_insights(user: str) -> dict[str, Any]:
        """Questions from the posting's own requirement ids; candidate questions only from evidence it was given."""
        found = re.search(r"^JOB REQUIREMENTS:\n(.*)$", user, re.MULTILINE)
        requirements = json.loads(found.group(1)) if found else {}
        required = [r for r in requirements.get("required") or [] if r.get("id")][:3]
        evidence = re.search(r"^VERIFIED EVIDENCE:\n(.*)$", user, re.MULTILINE)
        evidence_ids = [e["id"] for e in json.loads(evidence.group(1))[:2]] if evidence else []
        questions = [{"question": f"Walk us through how you have applied {r.get('normalized_value') or 'this skill'} "
                                  "in production.", "origin": "ROLE_DERIVED", "category": "TECHNICAL",
                      "stage": "TECHNICAL", "difficulty": "MEDIUM",
                      "rationale": "The posting lists it as a requirement.", "requirement_ids": [r["id"]],
                      "evidence_ids": [], "gap_requirement_ids": [], "preparation": ""} for r in required]
        if evidence_ids:
            questions.append({"question": "Tell me about the project you are proudest of and your part in it.",
                              "origin": "CANDIDATE_SPECIFIC", "category": "BEHAVIORAL", "stage": "HIRING_MANAGER",
                              "difficulty": "MEDIUM", "rationale": "Your strongest evidence will be probed.",
                              "requirement_ids": [], "evidence_ids": evidence_ids, "gap_requirement_ids": [],
                              "preparation": "Use the cited evidence only."})
        return {"focus_areas": [{"area": (r.get("normalized_value") or "Requirement")[:80],
                                 "why": "Stated as required in the posting.", "requirement_ids": [r["id"]]}
                                for r in required],
                "generated_questions": questions,
                "questions_to_ask": [{"stage": "RECRUITER", "question": ("What is the approved base salary and "
                                                                         "total-compensation band for this level?"),
                                      "context": "Compensation"},
                                     {"stage": "HIRING_MANAGER", "question": ("What would success look like in the "
                                                                              "first six months in this role?"),
                                      "context": "Role scope"}],
                "value_prep": [],
                "compensation_questions": [("Could you share the approved base salary and total-compensation band "
                                            "for this level?")]}

    @staticmethod
    def _mock_tailoring(user: str) -> dict[str, Any]:
        """A TailoringResult patch made only of the prompt's own SOURCE RESUME: skills reordered, and the lead
        bullet of each role kept with its evidence ids."""
        found = re.search(r"^SOURCE RESUME:\n(.*)$", user, re.MULTILINE)
        source = json.loads(found.group(1)) if found else {}
        def claim(bullet: dict[str, Any]) -> dict[str, Any]:
            return {"text": bullet["text"], "evidence_ids": [bullet["id"]] if "id" in bullet else []}
        updates = [{"role_index": role["role_index"], "bullets": [claim(b) for b in role["bullets"][:1]]}
                   for role in source.get("roles") or [] if role.get("bullets")][:3]
        highlights = [] if source.get("roles") else [claim(b) for b in (source.get("highlights") or [])[:3]]
        skills = list(reversed(source.get("skills") or []))
        cited = sorted({i for unit in [*highlights, *(b for u in updates for b in u["bullets"])]
                        for i in unit.get("evidence_ids") or []})
        return {"summary": None, "skills_order": skills, "role_updates": updates, "highlights": highlights,
                "selected_evidence_ids": cited}
