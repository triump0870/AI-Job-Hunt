"""What each AI worker's own requests say about its model: how much context it uses, which models have done best
for it, which others are worth trying, and the exact change that would switch to one.

AI Orchestration → Models & routing → Workers shows this beside each worker. Read-only and deterministic: no model
is asked anything. A suggestion is a recommendation; a person applies it through the normal draft → validate →
apply path (one PUT to the worker, built here as `apply`), which still refuses anything invalid.

Where the evidence comes from, strongest first:

* the worker's own requests (`best`): models with at least MIN_SAMPLES requests for this worker, ordered by the
  score Reliability already uses (app.services.model_health.score);
* the account's other workers (`OTHER_WORKERS`): a model this worker has not tried enough, judged on how it did
  elsewhere in the account. Latency is left out there: it depends on the other worker's prompts;
* every account on this server (`PLATFORM`): see platform_model_stats, the one deliberate cross-account read;
* the public catalogs (`CATALOG`): app.services.model_market's ranking, from its cache only (nothing is fetched
  while a screen loads), for gateway models nobody here has measured.

A model is suggested only when every gate below holds. Each one exists because a suggestion that fails it would
make the worker worse, and "unknown" never passes a gate on its own:

1. Capacity. The worker's typical request (p90 prompt + p90 answer, never less than the registry minimum) must fit:
   the model's context window is known and large enough, or the model has already answered a request that large
   for this account. A window nobody reported, with no such answer, is not offered. A model that refused a request
   of that size as too large for its per-minute limit is not offered either.
2. Structured output, for workers that parse JSON: the model is known to support it, or at least MIN_SAMPLES of
   its own answers were judged and MIN_SCHEMA_PCT of them were valid. Known unsupported, or unknown and unproven,
   is not offered.
3. Quality floor. At least MIN_SUCCESS_PCT of the requests behind the evidence succeeded (and MIN_SCHEMA_PCT of the
   judged ones were valid JSON), whatever the model in use is doing. "Better than nothing measured" is not better.
4. Comparable evidence. Another worker's requests count only when they were about as large as this worker's
   (at least COMPARABLE_SHARE of its average prompt): answering 1K prompts says nothing about 25K ones.
5. Better than the model in use, by MARGIN, on the same score. A model in use with too few requests here is judged
   on all its requests; with none anywhere, gates 1–4 still apply.

Each worker's entry reports how many models were screened out at which gate, so an empty list explains itself.

Telemetry is not tenancy-filtered automatically: every account read here filters by user_id explicitly. Never raises.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.services import model_health, model_market, model_resolution
from app.services.ai_workers import WORKERS
from app.settings import get_settings

log = logging.getLogger("jobhunt.worker_insights")

WINDOW_DAYS = 30
MAX_ROWS = 20_000
MIN_SAMPLES = model_market.OWN_SAMPLES  # fewer requests than this say too little to rank a model
MAX_BEST = 3
MAX_SUGGESTIONS = 3
MAX_BACKUPS = 4  # the fallback model and three secondary models, as the worker editor offers
MIN_BACKUPS = 2  # a switch tops the chain up to this from the worker's proven models
MARGIN = 2.0  # score points a suggestion must beat the current model by
MIN_SUCCESS_PCT = 90.0  # below this a model is not "doing well", whatever it is compared with
MIN_SCHEMA_PCT = 90.0
COMPARABLE_SHARE = 0.5  # another worker's request counts when it is at least this share of this worker's average
MAX_REFUSALS = 500
NEAR_LIMIT_PCT = 80
PLATFORM_MIN_SAMPLES = 50
PLATFORM_CACHE_SECONDS = 3600.0

_platform_lock = threading.Lock()
_platform_cache: tuple[float, dict[str, dict[str, Any]]] | None = None


def report(db: Session, user_id: int, snapshot: dict[str, Any], *, now: Any = None) -> dict[str, Any]:
    try:
        return _report(db, user_id, snapshot, now=now)
    except Exception:
        log.warning("could not work out worker insights", exc_info=True)
        return {"window_days": WINDOW_DAYS, "min_samples": MIN_SAMPLES, "workers": {}, "error": True}


# ───────────── reading the account's requests ─────────────

def _rows(db: Session, user_id: int, since: Any) -> list[Any]:
    from app.models import LLMRequestLog as Log

    return db.execute(select(
        Log.requested_model, Log.worker_type, Log.status, Log.error_type, Log.latency_ms, Log.input_tokens,
        Log.output_tokens, Log.estimated_cost, Log.cost_source, Log.structured_output_valid, Log.fallback_used,
        Log.estimated_context_tokens,
    ).where(Log.user_id == user_id, Log.provider == "litellm", Log.created_at >= since,
            Log.requested_model.is_not(None), Log.worker_type.is_not(None),
            or_(Log.cost_source.is_(None), Log.cost_source != "not_sent"))
        .order_by(Log.created_at.desc()).limit(MAX_ROWS)).all()


def _stats(items: list[Any], baseline_p95: int | None = None) -> dict[str, Any]:
    """One model's figures over some requests, with the score Reliability orders candidates by."""
    summary = model_market._summary(items)
    health = model_health.summarize([{"status": r.status, "error_type": r.error_type, "latency_ms": r.latency_ms,
                                      "fallback_used": r.fallback_used} for r in items])
    failure = summary["failure_pct"]
    return {"samples": summary["samples"], "success_pct": round(100 - failure, 1) if failure is not None else None,
            "schema_valid_pct": summary["schema_valid_pct"], "p50_ms": summary["p50_ms"], "p95_ms": summary["p95_ms"],
            "cost_per_request": summary["cost_per_request"], "score": model_health.score(health, baseline_p95)}


def _average(values: list[int]) -> int | None:
    return round(sum(values) / len(values)) if values else None


def _context(items: list[Any], window: int | None, model: str | None) -> dict[str, Any]:
    """What the worker's answered requests sent: the provider's own input count, else our estimate."""
    sent = [r.input_tokens or r.estimated_context_tokens for r in items if r.status == "SUCCEEDED"]
    sent = [int(v) for v in sent if v]
    average, p90 = _average(sent), model_market._percentile(sent, 90)
    return {"samples": len(sent), "avg_input_tokens": average, "p90_input_tokens": p90, "window": window,
            "model": model,
            "utilization_pct": round(100 * average / window, 1) if average and window else None,
            "near_limit": bool(p90 and window and 100 * p90 / window >= NEAR_LIMIT_PCT)}


# ───────────── which models a worker may be given ─────────────

def _usable(db: Session, user_id: int, snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The gateway's enabled chat models, with what is known about each. A model LiteLLM is known not to serve, or
    that its last health check found unhealthy, is never offered."""
    from app.models import AIModel

    known = {m.litellm_model_name: m for m in db.scalars(
        select(AIModel).where(AIModel.user_id == user_id, AIModel.removed_at.is_(None)).limit(500)).all()}
    out = {}
    for name, model in (snapshot.get("models") or {}).items():
        row = known.get(name)
        if not model.get("enabled") or model.get("model_type") == "EMBEDDING":
            continue
        if row is not None and (row.present_in_litellm is False or row.health_status == "UNHEALTHY"):
            continue
        out[name] = model
    return out


@dataclass(frozen=True)
class Facts:
    """What the account's requests proved about each model, whichever worker sent them."""
    answered: dict[str, int]  # the largest request (prompt + answer tokens) the model itself answered
    refused: dict[str, int]  # the smallest request it refused as too large for a per-minute limit
    judged: dict[str, tuple[int, float | None]]  # (answers judged as JSON, % of them valid)


def _typical(need: model_market.Requirements) -> int | None:
    """The worker's measured typical request in tokens; None when it has sent nothing to measure."""
    return (need.input_tokens or 0) + (need.output_tokens or 0) if need.basis == "telemetry" else None


def _screen(name: str, model: dict[str, Any], need: model_market.Requirements,
            facts: Facts) -> tuple[str | None, list[str]]:
    """(why the model cannot be given to this worker, or None) and what stood in for a missing fact. Gates 1–2."""
    notes: list[str] = []
    typical, window = _typical(need), model.get("context_window")
    refused = facts.refused.get(name)
    if refused is not None and typical and refused <= typical:
        return "REFUSED_AS_TOO_LARGE", notes
    if need.min_context:
        if window is not None:
            if window < need.min_context:
                return "CONTEXT_TOO_SMALL", notes
        elif typical and facts.answered.get(name, 0) >= typical:
            notes.append("CAPACITY_PROVEN")
        else:
            return "CONTEXT_UNKNOWN", notes
    if need.structured_output:
        supported = model.get("supports_structured_output")
        if supported is False:
            return "NO_STRUCTURED_OUTPUT", notes
        if supported is None:
            judged, valid = facts.judged.get(name, (0, None))
            if judged < MIN_SAMPLES or valid is None or valid < MIN_SCHEMA_PCT:
                return "STRUCTURED_OUTPUT_UNKNOWN", notes
            notes.append("JSON_PROVEN")
    return None, notes


def _good(stats: dict[str, Any]) -> bool:
    """Gate 3: enough of the requests behind these figures worked."""
    success, schema = stats.get("success_pct"), stats.get("schema_valid_pct")
    return success is not None and success >= MIN_SUCCESS_PCT and (schema is None or schema >= MIN_SCHEMA_PCT)


def _size(row: Any) -> int:
    return int(row.input_tokens or row.estimated_context_tokens or 0)


def _facts(db: Session, user_id: int, since: Any, by_model: dict[str, dict[str, list[Any]]]) -> Facts:
    from app.models import LLMRequestLog as Log
    from app.services import rate_limits

    answered: dict[str, int] = {}
    judged: dict[str, tuple[int, float | None]] = {}
    for name, workers in by_model.items():
        rows = [r for items in workers.values() for r in items]
        # A fallback's answer came from another model: it proves nothing about the one that was asked.
        sizes = [_size(r) + int(r.output_tokens or 0) for r in rows if r.status == "SUCCEEDED" and not r.fallback_used]
        answered[name] = max(sizes, default=0)
        verdicts = [r.structured_output_valid for r in rows if r.structured_output_valid is not None]
        judged[name] = (len(verdicts), round(100 * sum(verdicts) / len(verdicts), 1) if verdicts else None)
    refused: dict[str, int] = {}
    for name, message in db.execute(
            select(Log.requested_model, Log.error_message)
            .where(Log.user_id == user_id, Log.status == "FAILED", Log.error_type == "RATE_LIMIT",
                   Log.created_at >= since, Log.requested_model.is_not(None),
                   Log.error_message.ilike("%too large%"))
            .order_by(Log.created_at.desc()).limit(MAX_REFUSALS)).all():
        limit = rate_limits.size_limit(message)
        if limit is not None and limit.counts == "total":
            refused[name] = min(refused.get(name, limit.requested), limit.requested)
    return Facts(answered, refused, judged)


def _switch(snapshot: dict[str, Any], worker_type: str, model: str, current: str | None, proven: list[str],
            usable: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The one change that switches a worker to `model`: the model it used becomes the first backup, its other
    backups keep their order, and proven models fill the chain up to MIN_BACKUPS. Nothing appears twice."""
    config = (snapshot.get("workers") or {}).get(worker_type) or {}
    kept = [current, config.get("fallback_model"), *(config.get("secondary_models") or [])]
    backups: list[str] = []
    for name in [*kept, *proven]:
        if len(backups) >= (MAX_BACKUPS if name in kept else MIN_BACKUPS):
            continue
        if name and name != model and name in usable and name not in backups:
            backups.append(name)
    return {"model_mode": "FIXED", "primary_model": model, "fallback_model": backups[0] if backups else None,
            "secondary_models": backups[1:]}


# ───────────── every account: rates only ─────────────

def _platform_key(provider: str | None, underlying: str | None) -> str | None:
    return f"{(provider or '').strip().lower()}/{underlying.strip().lower()}" if underlying else None


def platform_model_stats(*, now: Any = None, refresh: bool = False) -> dict[str, dict[str, Any]]:
    """How each underlying model did across every account on this server: {provider/model: rates}.

    The one deliberate read across accounts, so it is kept narrow:

    * keyed by the provider's own model, never an account's gateway name;
    * a model appears only once at least `platform_model_stats_min_accounts` distinct accounts and
      PLATFORM_MIN_SAMPLES requests stand behind it;
    * only rates leave this function: no account, request count, prompt, worker or configuration.

    Off (`platform_model_stats_enabled`) or below the thresholds: empty. Aggregated in SQL on a session that belongs
    to no account, and cached in-process for an hour.
    """
    global _platform_cache
    settings = get_settings()
    if not settings.platform_model_stats_enabled:
        return {}
    with _platform_lock:
        if _platform_cache and not refresh and _platform_cache[0] > time.monotonic():
            return _platform_cache[1]
    from app.db import SessionLocal
    from app.models import AIModel
    from app.models import LLMRequestLog as Log

    since = (now or model_health.utcnow()) - timedelta(days=WINDOW_DAYS)
    failed = or_(Log.status != "SUCCEEDED", Log.fallback_used.is_(True))
    provider, underlying = func.lower(func.coalesce(AIModel.provider, "")), func.lower(AIModel.underlying_model)
    accounts = func.count(func.distinct(Log.user_id))
    with SessionLocal() as system:  # no account: the tenancy filter would otherwise narrow AIModel to one
        rows = system.execute(select(
            provider, underlying, func.count(Log.id), func.sum(case((failed, 1), else_=0)),
            func.sum(case((Log.structured_output_valid.is_(True), 1), else_=0)),
            func.sum(case((Log.structured_output_valid.is_not(None), 1), else_=0)),
            func.avg(case((failed, None), else_=Log.latency_ms)),
        ).join(AIModel, and_(AIModel.user_id == Log.user_id, AIModel.litellm_model_name == Log.requested_model))
            .where(Log.provider == "litellm", Log.created_at >= since, Log.user_id.is_not(None),
                   AIModel.underlying_model.is_not(None), AIModel.model_type.notin_(("EMBEDDING", "AUTO_ROUTER")),
                   or_(Log.cost_source.is_(None), Log.cost_source != "not_sent"),
                   or_(Log.error_type.is_(None), Log.error_type.notin_(model_market.NOT_HEALTH)))
            .group_by(provider, underlying)
            .having(func.count(Log.id) >= PLATFORM_MIN_SAMPLES,
                    accounts >= max(2, int(settings.platform_model_stats_min_accounts)))).all()
    out = {}
    for name, model, samples, failures, valid, judged, latency in rows:
        failure_pct = round(100 * (failures or 0) / samples, 1)
        schema_valid = round(100 * valid / judged, 1) if judged else None
        out[f"{name}/{model}"] = {
            "success_pct": round(100 - failure_pct, 1), "schema_valid_pct": schema_valid,
            "avg_ms": round(latency) if latency is not None else None,
            "score": round(max(0.0, 100 - 0.6 * failure_pct - 0.25 * (100 - schema_valid if judged else 0)), 1)}
    with _platform_lock:
        _platform_cache = (time.monotonic() + PLATFORM_CACHE_SECONDS, out)
    return out


def _platform_safely() -> dict[str, dict[str, Any]]:
    try:
        return platform_model_stats()
    except Exception:
        log.warning("could not read platform-wide model figures", exc_info=True)
        return {}


# ───────────── the public catalogs, from their cache ─────────────

def _catalog_scores(db: Session, user_id: int, need: model_market.Requirements) -> dict[str, float]:
    """{gateway model name: catalog score} for this worker's needs. Empty while nobody has loaded the catalogs."""
    try:
        found = model_market.catalog(db, user_id, cached_only=True)
        if not any(s.get("models") for s in found.sources):
            return {}
        ranked = model_market.rank(found.rows, "balanced", model_market.Constraints(in_gateway_only=True), need,
                                   limit=200)
        return {name: s["score"] for s in ranked["suggestions"] for name in s.get("gateway_names") or []}
    except Exception:
        log.info("worker insights left the catalogs out", exc_info=True)
        return {}


# ───────────── the report ─────────────

def _report(db: Session, user_id: int, snapshot: dict[str, Any], *, now: Any = None) -> dict[str, Any]:
    since = (now or model_health.utcnow()) - timedelta(days=WINDOW_DAYS)
    by_worker: dict[str, list[Any]] = defaultdict(list)
    by_pair: dict[tuple[str, str], list[Any]] = defaultdict(list)
    by_model: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
    for row in _rows(db, user_id, since):
        if row.status == "FAILED" and row.error_type in model_market.NOT_HEALTH:
            continue
        by_worker[row.worker_type].append(row)
        by_pair[(row.worker_type, row.requested_model)].append(row)
        by_model[row.requested_model][row.worker_type].append(row)

    models = snapshot.get("models") or {}
    usable = _usable(db, user_id, snapshot)
    facts = _facts(db, user_id, since, by_model)
    platform = _platform_safely()
    measured = {"workers": {w: model_market._tokens(items) for w, items in by_worker.items()}}
    workers: dict[str, Any] = {}
    for worker_type, worker in WORKERS.items():
        config = (snapshot.get("workers") or {}).get(worker_type) or {}
        current = model_resolution.resolve_model(snapshot, worker_type)[0]
        window = config.get("context_window") or (models.get(current) or {}).get("context_window")
        items = by_worker.get(worker_type, [])
        entry: dict[str, Any] = {"requests": len(items), "current": current,
                                 "context": _context(items, window, current), "best": [], "suggestions": []}
        workers[worker_type] = entry
        if worker.capability != "CHAT":
            continue  # one fixed embedding model, no backups: nothing to rank or switch
        need = model_market.requirements_for(worker_type, measured, snapshot)
        entry["needs"] = {"context_tokens": need.min_context, "typical_request_tokens": _typical(need),
                          "structured_output": need.structured_output}
        own = {m: v for (w, m), v in by_pair.items() if w == worker_type}
        proven = {m: v for m, v in own.items() if len(v) >= MIN_SAMPLES}
        fastest = min((p for p in (model_market._percentile(
            [r.latency_ms for r in v if r.status == "SUCCEEDED" and not r.fallback_used and r.latency_ms is not None],
            95) for v in proven.values()) if p), default=None)
        ranked = sorted(((m, _stats(v, fastest)) for m, v in proven.items()),
                        key=lambda x: (-(x[1]["score"] or 0), -x[1]["samples"], x[0]))
        own_stats = dict(ranked)
        # A switch only ever adds backups that have themselves done this worker's job well.
        proven_names = [m for m, stats in ranked if m in usable and _good(stats)]
        current_stats = own_stats.get(current)
        if current_stats is None and current:
            # Too few requests here to rank the model in use: judge it on every worker's requests, so a suggestion
            # still has to beat something real.
            everywhere = [r for v in by_model.get(current, {}).values() for r in v]
            current_stats = _stats(everywhere) if len(everywhere) >= MIN_SAMPLES else None
        current_score = current_stats["score"] if current_stats else None
        entry["current_stats"] = current_stats

        def beats(score: float | None, floor: float | None = current_score) -> bool:
            return score is not None and (floor is None or score >= floor + MARGIN)

        for model, stats in ranked[:MAX_BEST]:
            # Going back to a model is offered only when it is allowed, did well, and did better than the one in use.
            switchable = (model != current and model in usable and _good(stats) and beats(stats["score"])
                          and _screen(model, usable[model], need, facts)[0] is None)
            entry["best"].append({"model": model, **stats, "is_current": model == current,
                                  "apply": _switch(snapshot, worker_type, model, current, proven_names, usable)
                                  if switchable else None})
        entry["suggestions"], entry["screened"] = _suggestions(
            db, user_id, snapshot, worker_type, need, items, current, current_stats, beats, proven, proven_names,
            by_model, usable, facts, platform)
    return {"window_days": WINDOW_DAYS, "min_samples": MIN_SAMPLES, "min_success_pct": MIN_SUCCESS_PCT,
            "workers": workers, "platform": bool(platform)}


def _suggestions(db: Session, user_id: int, snapshot: dict[str, Any], worker_type: str,
                 need: model_market.Requirements, items: list[Any], current: str | None,
                 current_stats: dict[str, Any] | None, beats: Any, proven: dict[str, list[Any]],
                 proven_names: list[str], by_model: dict[str, dict[str, list[Any]]],
                 usable: dict[str, dict[str, Any]], facts: Facts,
                 platform: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Models this worker has not tried enough to rank, that pass every gate; and how many fell at which gate."""
    rejected: Counter[str] = Counter()
    candidates: dict[str, tuple[dict[str, Any], list[str]]] = {}
    for name, model in usable.items():
        if name == current or name in proven:
            continue
        why, notes = _screen(name, model, need, facts)
        if why:
            rejected[why] += 1
        else:
            candidates[name] = (model, notes)
    considered = len(candidates) + sum(rejected.values())
    sizes = [_size(r) for r in items if r.status == "SUCCEEDED" and _size(r)]
    comparable = COMPARABLE_SHARE * sum(sizes) / len(sizes) if sizes else 0
    # Speed counts against the model in use only when that was measured on this worker's own requests.
    baseline = current_stats.get("p95_ms") if current_stats and current in proven else None
    found: list[dict[str, Any]] = []

    def offer(name: str, basis: str, score: float, evidence: dict[str, Any]) -> None:
        found.append({"model": name, "basis": basis, "score": score, "flags": candidates[name][1], **evidence,
                      "apply": _switch(snapshot, worker_type, name, current, proven_names, usable)})

    elsewhere, undecided = [], []
    for name in candidates:
        others = {w: [r for r in v if _size(r) >= comparable] for w, v in by_model.get(name, {}).items()
                  if w != worker_type}
        others = {w: v for w, v in others.items() if v}
        rows = [r for v in others.values() for r in v]
        if len(rows) < MIN_SAMPLES:
            undecided.append(name)  # no comparable evidence in this account: the wider tiers may still speak
            continue
        stats = _stats(rows, baseline)
        if not _good(stats):
            rejected["LOW_SUCCESS"] += 1
        elif not beats(stats["score"]):
            rejected["NOT_BETTER"] += 1
        else:
            used = sorted(others, key=lambda w: -len(others[w]))[:2]
            elsewhere.append((name, stats, [WORKERS[w].label if w in WORKERS else w for w in used]))
    for name, stats, used in sorted(elsewhere, key=lambda x: (-x[1]["score"], -x[1]["samples"], x[0])):
        offer(name, "OTHER_WORKERS", stats["score"], {"stats": stats, "workers": used})

    shared = []
    for name in list(undecided):
        model = candidates[name][0]
        figures = platform.get(_platform_key(model.get("provider"), model.get("underlying_model")) or "")
        if not figures:
            continue
        undecided.remove(name)
        if not _good(figures):
            rejected["LOW_SUCCESS"] += 1
        elif not beats(figures["score"]):
            rejected["NOT_BETTER"] += 1
        else:
            shared.append((name, figures))
    for name, figures in sorted(shared, key=lambda x: (-x[1]["score"], x[0])):
        offer(name, "PLATFORM", figures["score"], {"stats": {k: v for k, v in figures.items() if k != "score"}})

    if undecided and len(found) < MAX_SUGGESTIONS:
        scores = _catalog_scores(db, user_id, need)
        floor = scores.get(current) if current else None
        # Nothing here was measured, so the catalog may only speak for a model whose capacity and JSON support are
        # known facts (no stand-in proof), and only to say it ranks above the model in use on the same scale.
        listed = [(n, scores[n]) for n in undecided if n in scores and not candidates[n][1]
                  and floor is not None and scores[n] >= floor + MARGIN]
        for name, score in sorted(listed, key=lambda x: (-x[1], x[0])):
            offer(name, "CATALOG", score, {"catalog_score": score, "current_catalog_score": floor})
            undecided.remove(name)
    rejected["NO_EVIDENCE"] += len(undecided)
    return found[:MAX_SUGGESTIONS], {"considered": considered,
                                     "rejected": {k: v for k, v in sorted(rejected.items()) if v}}
