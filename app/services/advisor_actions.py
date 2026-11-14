"""One-click actions and decision briefs for AI optimization recommendations.

The advisor (app.services.ai_advisor) only ever writes advice. This module turns a report into things a person can
act on, and acts only when that person clicks:

    report + the account's AI configuration draft + its LLM request log for the report's period
    → candidate CONFIG actions, derived by deterministic rules (never by the model): drop failing backup models, replace
      a failing primary with a configured model that works, turn off or retarget a failing escalation, promote a
      healthy escalation model that answers most requests, set a prompt token budget on a worker that trims
    → each recommendation gets the actions for its stage, a decision brief (numbers straight from the request log,
      how sure we are, upside, risk, how to verify, how to undo) and, when the change is a code change, a prompt for
      a coding assistant

    Click → stage: the change is written to the configuration DRAFT (ai_model_config.update_worker) and previewed
    (ai_model_config.preview: diff + validation). Nothing is live yet.
    Apply → ai_model_config.apply with the previewed revision and version: a new config version, audited.
    Cancel → the draft goes back to the active version. Undo → ai_model_config.restore of the version before.

Actions are recomputed from the CURRENT draft every time, so a suggestion whose precondition no longer holds (the model
was already removed, the config changed) is never staged. A draft with someone's unapplied edits is never touched.
Nothing here writes WorkerModelConfig rows directly; every change goes through ai_model_config.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.models import AIOptimizationDecision, AIOptimizationReport, LLMRequestLog
from app.services import ai_model_config
from app.services.ai_workers import MONITORING_STAGE, WORKERS
from app.services.ai_workers import label as worker_label
from app.services.prompt_budget import TRIMMED_WORKERS

# A model's numbers count only with at least MIN_SAMPLE requests on that worker in the report's period.
MIN_SAMPLE = 20
FAILING_RATE = 0.8  # a primary or backup this unreliable is not doing its job
ESCALATION_FAILING_RATE = 0.5  # an escalation is the hard case: half of them failing makes it worse than none
HEALTHY_RATE = 0.15  # a replacement must fail at most this often on the same worker
PROMOTE_ESCALATION_RATE = 0.5  # escalating more often than this means the escalation model does most of the work
BUDGET_MIN_CONTEXT = 2000
BUDGET_STEP = 500
DISMISS_FOR = timedelta(days=30)
STAGED_FOR = timedelta(hours=12)  # a staged change nobody applied stops being offered as "waiting"

FIELD_LABELS = {"primary_model": "primary model", "fallback_model": "fallback model",
                "secondary_models": "further backups", "escalation": "escalation", "model_mode": "model mode",
                "prompt_token_budget": "prompt token budget"}

# Changes that live in code, not configuration: they get a prompt for a coding assistant.
CODE_HINT = re.compile(r"cach|reuse|dedup|duplicate|repeat|backoff|rate.?limit|retr(y|ies)|implement|code|truncat|"
                       r"summari|shorten|strip|trim|prompt|deterministic|validation first|render|playwright|"
                       r"schedule|frequency|actor|skip re-?invocation", re.IGNORECASE)
CODE_CATEGORIES = ("ARCHITECTURE", "PROMPT", "EXTERNAL_API")
MODEL_CATEGORIES = ("RELIABILITY", "MODEL", "COST", "LATENCY")

# Where code for a stage lives (AGENTS.md "Where changes belong"); the prompt tells the assistant to confirm by search.
STAGE_OWNERS = {
    "RANKER": ["app/services/ranker.py"], "TAILOR": ["app/services/tailor_tasks.py", "app/services/tailoring_results.py"],
    "REVIEWER": ["app/services/review_results.py"], "JD_RECOVERY": ["app/services/jd_recovery.py",
                                                                    "app/services/jd_quality.py"],
    "JD_PARSER": ["the JD_PARSER worker's call sites (search `JD_PARSER` in app/services/)", "app/services/jd_quality.py"],
    "DOMAIN_CLASSIFIER": ["app/services/domain_match.py", "app/services/domain_classifier.py"],
    "APPLICATION": ["app/services/application_execution.py", "app/services/answer_resolver.py"],
    "INBOX": ["app/services/application_inbox/"], "COMPANY_INSIGHTS": ["app/services/job_insights/"],
    "INTERVIEW_INSIGHTS": ["app/services/job_insights/"], "ADVISOR": ["app/services/ai_advisor.py"],
    "RESUME_PARSER": ["app/services/resume_extraction/"],
}
TOPICS = (
    ("reuse", re.compile(r"cach|reuse|dedup|duplicate|repeat|re-?invocation", re.IGNORECASE),
     ["app/services/llm.py (LLMClient.generate_prompt, the only generation path)", "app/services/ai_workers.py"],
     [("Key any reuse on the exact inputs (entity, stage, prompt version and an input hash); a changed resume, "
       "job description or prompt version must miss."), "Worker writes after a model call stay lease-guarded."],
     ["a repeated call with identical inputs is served without a model call",
      "changed inputs or a new prompt version always call the model"]),
    ("external", re.compile(r"backoff|rate.?limit|retr(y|ies)|\bweb\b|http|playwright|render|external|apify|actor", re.IGNORECASE),
     ["app/services/external_api.py (external_client / external_operation)", "app/services/backoff.py",
      "app/services/jd_recovery.py", "app/services/hunter.py and app/services/apify_adapters.py"],
     ["Outbound calls stay instrumented through external_client / external_operation.",
      ("Respect access denials, CAPTCHA/anti-bot challenges, login walls and 429s: back off or hand to a person; "
       "never evade them, and never fall back to a browser to defeat an explicit denial.")],
     ["a 429 or challenge backs off and is not retried immediately", "a success clears the cooldown"]),
    ("context", re.compile(r"context|trim|truncat|summari|shorten|token|resume portion|system prompt|prompt", re.IGNORECASE),
     ["app/prompts/ (versioned prompts and contracts)", "app/services/prompt_budget.py",
      "app/services/context_builder.py"],
     ["Bump the prompt version when its behaviour changes and add golden fixtures.",
      "Trim only optional text; never drop the requirements or evidence needed to validate the output."],
     ["the trimmed prompt keeps every required section", "outputs on golden fixtures do not regress"]),
)
ALWAYS_INVARIANTS = (
    "All generation goes through LLMClient.generate_prompt with registered prompts; no direct provider calls.",
    "Model choices are configuration (Settings → AI Models: draft → preview → apply), never hard-coded in a worker.",
    "Keep resume text, job descriptions, answers and secrets out of logs, checkpoints and telemetry.",
    "Every user-owned query stays filtered to the account (app/tenancy.py).",
    "Unknown cost, latency or outcome stays unknown; do not infer it.",
)


class ActionError(Exception):
    """A request about an action that cannot go ahead. `status_code` is the HTTP status to answer with."""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code, self.detail = status_code, message


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def recommendation_key(rec: dict[str, Any]) -> str:
    """Stable across reports for the same advice, so a dismissal outlives the next daily report."""
    text = f"{(rec.get('title') or '').strip().lower()}|{rec.get('affected_stage') or ''}"
    return hashlib.sha1(text.encode()).hexdigest()[:16]


# ───────────── telemetry: how each configured model did on each worker ─────────────

@dataclass
class ModelStat:
    requests: int
    failed: int
    rate_limited: int
    answered_by_backup: int

    @property
    def failure_rate(self) -> float | None:
        return self.failed / self.requests if self.requests else None

    @property
    def enough(self) -> bool:
        return self.requests >= MIN_SAMPLE

    def public(self) -> dict[str, Any]:
        return {"requests": self.requests, "failed": self.failed, "failure_rate": self.failure_rate,
                "rate_limited": self.rate_limited, "answered_by_backup": self.answered_by_backup}


Stats = dict[tuple[str, str], ModelStat]


def model_stats(db: Session, user_id: int, start: datetime, end: datetime) -> Stats:
    """Per (worker, requested model): what the account asked each configured name to do and how it went.

    Keyed by the name the worker REQUESTED (a router alias, an escalation target), not the model that answered: a
    router's successes are logged under the member model that answered, so counting only rows logged under the
    router's own name would make every router look 100% failing.
    """
    rows = db.execute(
        select(LLMRequestLog.worker_type, LLMRequestLog.requested_model, func.count(),
               func.sum(case((LLMRequestLog.status == "FAILED", 1), else_=0)),
               func.sum(case((LLMRequestLog.error_type == "RATE_LIMIT", 1), else_=0)),
               func.sum(case((and_(LLMRequestLog.status == "SUCCEEDED", LLMRequestLog.fallback_used.is_(True)), 1),
                             else_=0)))
        .where(LLMRequestLog.user_id == user_id, LLMRequestLog.created_at >= start, LLMRequestLog.created_at <= end,
               LLMRequestLog.worker_type.is_not(None), LLMRequestLog.requested_model.is_not(None))
        .group_by(LLMRequestLog.worker_type, LLMRequestLog.requested_model)).all()
    return {(w, m): ModelStat(int(n or 0), int(f or 0), int(r or 0), int(b or 0)) for w, m, n, f, r, b in rows}


def _failing(stat: ModelStat | None, rate: float = FAILING_RATE) -> bool:
    return bool(stat and stat.enough and (stat.failure_rate or 0) >= rate)


def _healthy(stat: ModelStat | None) -> bool:
    return bool(stat and stat.enough and (stat.failure_rate or 0) <= HEALTHY_RATE)


# ───────────── candidate configuration actions ─────────────

def _backups(cfg: dict[str, Any]) -> list[str]:
    return [m for m in [cfg.get("fallback_model"), *(cfg.get("secondary_models") or [])] if m]


def _with_backups(names: list[str]) -> dict[str, Any]:
    return {"fallback_model": names[0] if names else None, "secondary_models": names[1:] or None}


def _roles(cfg: dict[str, Any]) -> dict[str, str]:
    roles: dict[str, str] = {}
    backups = _backups(cfg)
    for i, name in enumerate(backups):
        roles.setdefault(name, "fallback" if i == 0 else f"backup {i + 1}")
    if (cfg.get("escalation") or {}).get("model"):
        roles.setdefault(cfg["escalation"]["model"], "escalation")
    if cfg.get("primary_model") and cfg.get("mode") == "FIXED":
        roles[cfg["primary_model"]] = "primary"
    return roles


def _show(field: str, value: Any) -> Any:
    if field == "escalation":
        return "off" if not value else (value.get("model") or value.get("tier") or "on")
    if field == "secondary_models":
        return ", ".join(value) if value else "none"
    return value if value not in (None, "") else "none"


def _action(kind: str, worker: str, cfg: dict[str, Any], payload: dict[str, Any], stats: Stats, *, title: str,
            why: str, upside: str, risk: str, verify: str, models: list[str], decisive: list[str],
            group: str | None = None) -> dict[str, Any]:
    changes = [{"field": f, "label": FIELD_LABELS.get(f, f.replace("_", " ")), "before": _show(f, cfg.get(
        "mode" if f == "model_mode" else f)), "after": _show(f, v)} for f, v in payload.items()]
    after = {**cfg, **payload}
    before_roles, after_roles = _roles(cfg), _roles(after)
    rows = []
    for name in dict.fromkeys(models):
        stat = stats.get((worker, name))
        rows.append({"model": name, "role_before": before_roles.get(name, "—"), "role_after": after_roles.get(name, "—"),
                     **(stat.public() if stat else {"requests": 0, "failed": 0, "failure_rate": None,
                                                    "rate_limited": 0, "answered_by_backup": 0})})
    sample = min((stats[(worker, m)].requests for m in decisive if (worker, m) in stats), default=0)
    confidence = ("HIGH" if sample >= 100 else "MEDIUM") if sample >= MIN_SAMPLE else "LOW"
    fingerprint = hashlib.sha1(json.dumps([kind, worker, payload, {k: cfg.get(k) for k in payload}],
                                          sort_keys=True, default=str).encode()).hexdigest()[:10]
    return {"id": f"{kind}.{worker}.{fingerprint}", "kind": "CONFIG", "type": kind, "worker_type": worker,
            "worker": worker_label(worker), "stage": MONITORING_STAGE.get(worker, "OTHER"), "title": title,
            "why": why, "changes": changes, "payload": payload, "models": rows, "upside": upside, "risk": risk,
            "verify": verify, "group": group,
            "confidence": {"level": confidence, "sample": sample,
                           "text": f"Based on {sample:,} request{'s' if sample != 1 else ''} in this period"
                                   + ("" if sample >= MIN_SAMPLE else " — too few to be sure; consider waiting")}}


def candidate_actions(workers: dict[str, dict[str, Any]], stats: Stats,
                      routing: dict[str, Any] | None = None, stages: list[dict[str, Any]] | None = None
                      ) -> list[dict[str, Any]]:
    """Every configuration change the request log supports, for the current draft. Pure and deterministic."""
    routing_by_worker = {w["worker_type"]: w for w in (routing or {}).get("workers") or []}
    stage_rows = {s["stage"]: s for s in stages or []}
    actions: list[dict[str, Any]] = []
    for worker, cfg in sorted(workers.items()):
        if worker not in WORKERS or not cfg.get("enabled", True) or WORKERS[worker].capability != "CHAT":
            continue
        name = worker_label(worker)
        stat = lambda m, w=worker: stats.get((w, m))
        primary = cfg.get("primary_model") if cfg.get("mode") == "FIXED" else None
        backups = _backups(cfg)
        healthy_backups = sorted((m for m in backups if _healthy(stat(m))), key=lambda m: stat(m).failure_rate or 0)

        # 1. A failing primary, with a configured model that already works on this worker.
        if primary and _failing(stat(primary)) and healthy_backups:
            new = healthy_backups[0]
            rest = [m for m in backups if m != new and not _failing(stat(m))]
            payload = {"primary_model": new, **_with_backups(rest)}
            if (cfg.get("escalation") or {}).get("model") == new:
                payload["escalation"] = None
            actions.append(_action(
                "replace-primary", worker, cfg, payload, stats,
                title=f"Make {new} the {name} primary model",
                why=f"{primary} failed {_pct(stat(primary).failure_rate)} of {stat(primary).requests:,} {name} requests; "
                    f"{new}, already configured as a backup, failed {_pct(stat(new).failure_rate)} of "
                    f"{stat(new).requests:,}. {primary} is taken out of the chain.",
                upside="Most requests are answered on the first try instead of failing over.",
                risk=f"{new} may judge differently from {primary}; compare a few {name} results after the switch.",
                verify=f"{name} failure rate on Monitoring → LLM usage (now {_pct(_stage_failure(stage_rows, worker))}).",
                models=[primary, *backups], decisive=[primary, new]))
        # 2. Backups that fail almost every time only add latency before the request fails anyway.
        failing_backups = [m for m in backups if _failing(stat(m))]
        if failing_backups and not (primary and _failing(stat(primary)) and healthy_backups):
            kept = [m for m in backups if m not in failing_backups]
            actions.append(_action(
                "drop-backups", worker, cfg, _with_backups(kept), stats,
                title=f"Remove failing backup model{'s' if len(failing_backups) > 1 else ''} from {name}",
                why="; ".join(f"{m} failed {_pct(stat(m).failure_rate)} of {stat(m).requests:,} {name} requests"
                              for m in failing_backups) + ". A backup that fails this often only adds a wait before "
                                                          "the request fails.",
                upside="Shorter failure paths and fewer wasted calls (and rate-limit hits) on those providers.",
                risk=("No backup is left: when the primary is down the request fails straight away."
                      if not kept and not primary else
                      "If those providers recover, they are no longer tried; add them back in AI Models."),
                verify=f"{name} failure rate and p95 latency on Monitoring → LLM usage.",
                models=[*(m for m in [primary] if m), *backups], decisive=failing_backups))
        # 3. An escalation target that fails: the hardest cases get a second failure.
        esc = cfg.get("escalation") or {}
        target = esc.get("model")
        if target and _failing(stat(target), ESCALATION_FAILING_RATE):
            why = (f"{name} escalates its hardest requests to {target}, which failed "
                   f"{_pct(stat(target).failure_rate)} of {stat(target).requests:,} of them.")
            actions.append(_action(
                "escalation-off", worker, cfg, {"escalation": None}, stats,
                title=f"Turn off {name} escalation", why=why + " Turning it off keeps the first answer.",
                upside="No second, mostly failing call: lower latency and fewer failed requests.",
                risk="Low-confidence answers are kept as they are instead of getting a second opinion.",
                verify=f"{name} failure rate and p95 latency; spot-check low-confidence {name} results.",
                models=[*(m for m in [primary] if m), target], decisive=[target], group=f"escalation.{worker}"))
            candidates = [m for m in healthy_backups if m != primary and m != target]
            if candidates:
                new = candidates[0]
                actions.append(_action(
                    "escalation-retarget", worker, cfg, {"escalation": {**esc, "model": new}}, stats,
                    title=f"Escalate {name} to {new} instead",
                    why=why + f" {new}, already configured for {name}, failed {_pct(stat(new).failure_rate)} of "
                              f"{stat(new).requests:,}.",
                    upside="Hard cases still get a second opinion, from a model that answers.",
                    risk=f"{new} may be weaker than {target} on the hard cases it now handles.",
                    verify=f"{name} escalation success on Monitoring → Model performance.",
                    models=[*(m for m in [primary] if m), target, new], decisive=[target, new],
                    group=f"escalation.{worker}"))
        # 4. A healthy escalation model that answers most requests anyway: call it directly.
        rate = (routing_by_worker.get(worker) or {}).get("escalation_rate")
        if target and primary and rate is not None and rate >= PROMOTE_ESCALATION_RATE and _healthy(stat(target)):
            rest = [m for m in backups if m not in (target, primary) and not _failing(stat(m))]
            actions.append(_action(
                "promote-escalation", worker, cfg, {"primary_model": target, "escalation": None, **_with_backups(rest)},
                stats, title=f"Use {target} as the {name} primary model",
                why=f"{_pct(rate)} of {name} requests escalate to {target}, which failed only "
                    f"{_pct(stat(target).failure_rate)} of {stat(target).requests:,}. Calling it first saves the "
                    "first pass on most requests.",
                upside="One call instead of two for most requests: lower latency.",
                risk=f"Every {name} request now goes to {target}: check its cost and speed on easy requests.",
                verify=f"{name} p95 latency and failure rate on Monitoring → LLM usage.",
                models=[primary, target, *backups], decisive=[target]))
        # 5. A trimming worker whose prompts run large: set a budget near the average so outliers are trimmed.
        srow = stage_rows.get(MONITORING_STAGE.get(worker, ""))
        avg = (srow or {}).get("avg_context_tokens") or 0
        if worker in TRIMMED_WORKERS and not cfg.get("prompt_token_budget") and avg >= BUDGET_MIN_CONTEXT \
                and (srow or {}).get("requests", 0) >= MIN_SAMPLE:
            budget = int(math.ceil(avg / BUDGET_STEP) * BUDGET_STEP)
            sample = srow["requests"]
            action = _action(
                "prompt-budget", worker, cfg, {"prompt_token_budget": budget}, stats,
                title=f"Cap {name} prompts at {budget:,} tokens",
                why=f"{name} prompts average {avg:,.0f} tokens (p95 {(srow.get('p95_context_tokens') or 0):,.0f}). "
                    f"A {budget:,}-token budget leaves average prompts alone and trims the largest by dropping their "
                    "least relevant items first.",
                upside="Lower input cost and tail latency on the largest requests.",
                risk="Trimmed prompts carry fewer supporting items; compare a few results before and after.",
                verify=f"{name} average context and p95 latency on Monitoring → LLM usage.",
                models=[], decisive=[])
            action["confidence"] = {"level": "HIGH" if sample >= 100 else "MEDIUM", "sample": sample,
                                    "text": f"Based on {sample:,} {name} requests in this period"}
            actions.append(action)
    return actions


def _stage_failure(stage_rows: dict[str, Any], worker: str) -> float | None:
    return (stage_rows.get(MONITORING_STAGE.get(worker, "")) or {}).get("failure_rate")


# ───────────── per recommendation: actions, brief, coding prompt ─────────────

def _workers_for(stage: str) -> set[str]:
    if stage in ("ALL", "OTHER", ""):
        return set(WORKERS)
    return {w for w, s in MONITORING_STAGE.items() if s == stage}


def _text(rec: dict[str, Any]) -> str:
    return " ".join(str(rec.get(k) or "") for k in ("title", "finding", "evidence", "recommended_change"))


def actions_for(rec: dict[str, Any], actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The configuration actions that carry out (part of) this recommendation."""
    stage, category, text = rec.get("affected_stage") or "ALL", rec.get("category") or "", _text(rec)
    workers = _workers_for(stage)
    wants_models = category in MODEL_CATEGORIES or re.search(r"\bmodels?\b|escalat|fallback|backup", text, re.IGNORECASE)
    wants_budget = category in ("CONTEXT", "PROMPT") or re.search(r"context|trim|truncat|token", text, re.IGNORECASE)
    out = []
    for a in actions:
        if a["worker_type"] not in workers:
            continue
        if a["type"] == "prompt-budget":
            if wants_budget:
                out.append(a)
        elif wants_models:
            # A recommendation about every stage applies only where it names one of the action's models.
            if stage in ("ALL", "OTHER") and not any(r["model"] in text for r in a["models"]
                                                     if r["role_before"] != r["role_after"]):
                continue
            out.append(a)
    return out


def needs_code(rec: dict[str, Any], config_actions: list[dict[str, Any]]) -> bool:
    return (rec.get("category") in CODE_CATEGORIES or bool(CODE_HINT.search(rec.get("recommended_change") or ""))
            or not config_actions)


def coding_prompt(rec: dict[str, Any], report: AIOptimizationReport, stage_row: dict[str, Any] | None) -> str:
    """A self-contained task for a coding assistant working in this repository. Built from a template, not a model;
    it carries telemetry numbers and file pointers only, never resume text, prompts or secrets."""
    stage = rec.get("affected_stage") or "ALL"
    text = _text(rec)
    topics = [t for t in TOPICS if t[1].search(text)]
    owners = list(dict.fromkeys([*STAGE_OWNERS.get(stage, []), *(o for t in topics for o in t[2])]))
    invariants = [*(i for t in topics for i in t[3]), *ALWAYS_INVARIANTS]
    tests = [*(x for t in topics for x in t[4]), "the existing suite for every file you touch still passes"]
    period = f"{report.time_range_start:%Y-%m-%d} to {report.time_range_end:%Y-%m-%d}"
    numbers = ""
    if stage_row:
        numbers = (f"\nStage numbers for {period}: {stage_row.get('requests', 0):,} requests, "
                   f"{_pct(stage_row.get('failure_rate'))} failed, retry rate {_pct(stage_row.get('retry_rate'))}, "
                   f"p95 latency {(stage_row.get('p95_latency_ms') or 0) / 1000:.1f}s, average context "
                   f"{(stage_row.get('avg_context_tokens') or 0):,.0f} tokens.")
    lines = [
        f"# Task: {rec.get('title')}",
        "",
        "You are working in the Job Hunt OS repository. Read AGENTS.md first and follow it; it overrides this prompt.",
        "",
        "## Problem (from the AI optimization report)",
        f"- Stage: {stage} · category: {rec.get('category')} · priority: {rec.get('priority')}",
        f"- Finding: {rec.get('finding')}",
        f"- Evidence: {rec.get('evidence')}{numbers}",
        f"- Suggested change: {rec.get('recommended_change')}",
        f"- Expected benefit: {rec.get('expected_benefit')}",
        f"- Known risk: {rec.get('risk')}",
        "",
        ("The report's text may be written by a model: verify every number against the code and the monitoring data "
         "before relying on it. Model or provider choices are configuration (Settings → AI Models), not code — do "
         "not hard-code them."),
        "",
        "## Where to look",
        *(f"- {o}" for o in owners or ["Locate the owner of this stage (AGENTS.md section 2) and search all callers."]),
        "",
        "## Invariants you must keep",
        *(f"- {i}" for i in invariants),
        "",
        "## How to work",
        ("1. Before editing, state the current behaviour, the intended behaviour, the invariant risks and the tests "
         "you will add."),
        "2. Make the smallest compatible change; keep feature-off behaviour unless a change is required.",
        "3. Add or update tests, including:",
        *(f"   - {t}" for t in tests),
        ("4. Run the targeted pytest files, then the full applicable suites (frontend tests only if the frontend "
         "changed)."),
        "",
        "## Done when",
        "- The change addresses the finding above and the tests pass.",
        "- Report the files changed, behaviour, tests run, remaining risks and any migration or flag.",
    ]
    return "\n".join(lines)


def _confidence_for(rec: dict[str, Any], stage_row: dict[str, Any] | None, totals: dict[str, Any]) -> dict[str, Any]:
    sample = (stage_row or {}).get("requests") if stage_row else totals.get("requests")
    sample = int(sample or 0)
    level = ("HIGH" if sample >= 100 else "MEDIUM") if sample >= MIN_SAMPLE else "LOW"
    where = f"{stage_row['stage'].replace('_', ' ').title()} requests" if stage_row else "requests"
    return {"level": level, "sample": sample,
            "text": f"Based on {sample:,} {where} in this period"
                    + ("" if sample >= MIN_SAMPLE else " — too few to act on with confidence")}


def brief(rec: dict[str, Any], report: AIOptimizationReport, actions: list[dict[str, Any]],
          stage_row: dict[str, Any] | None, code: bool) -> dict[str, Any]:
    """What a person needs to decide: how sure this is, what it changes, the trade-off, how to check and undo it."""
    totals = (report.aggregates or {}).get("totals") or {}
    confidence = _confidence_for(rec, stage_row, totals)
    if actions:
        doing = (f"{len(actions)} configuration change{'s' if len(actions) > 1 else ''} below can be staged with one "
                 "click. Each shows its exact diff and is validated before anything goes live; Apply makes a new "
                 "configuration version you can undo.")
    elif code:
        doing = ("This needs a code change, so nothing can be switched on here. Copy the prompt into your coding "
                 "assistant: it carries the finding, the files to look at and the rules the change must keep.")
    else:
        doing = "No one-click change matches this recommendation; change it in Settings yourself if you agree."
    notes = []
    if report.generated_by == "llm":
        notes.append(f"Written by {report.model_name or 'a model'}. Its wording can differ from the request log; the "
                     "model figures in the changes below are counted straight from the log for this period.")
    if any(a["confidence"]["level"] == "LOW" for a in actions):
        notes.append("Some changes rest on few requests: waiting for more usage gives a clearer answer.")
    return {"confidence": confidence, "doing": doing, "upside": rec.get("expected_benefit"), "risk": rec.get("risk"),
            "verify": ("Re-run Analyze after a few days of usage and compare the stage's failure rate and latency"
                       + (f" (now {_pct(stage_row.get('failure_rate'))} failed, p95 "
                          f"{(stage_row.get('p95_latency_ms') or 0) / 1000:.0f}s)." if stage_row else ".")),
            "undo": ("Apply creates a new configuration version; Undo here (or AI Models → History) restores the "
                     "version before it." if actions else "Revert the code change if the numbers get worse."),
            "notes": notes}


# ───────────── read model ─────────────

def _workers(db: Session, user_id: int) -> tuple[dict[str, dict[str, Any]], bool, str | None]:
    """The draft's workers, whether the draft has unapplied edits, and why actions cannot be staged (None if they can)."""
    ai_model_config.ensure_seeded(db, user_id)
    draft = ai_model_config.draft_snapshot(db, user_id)
    active = ai_model_config.active_version(db, user_id)
    if active is None:
        return draft.get("workers") or {}, False, "Apply your AI Models configuration once before using one-click changes."
    pending = ai_model_config.diff(dict(active.configuration_snapshot or {}), draft)
    blocked = ("AI Models has unapplied edits. Apply or discard them first, so a one-click change never ships them "
               "by accident.") if pending else None
    return draft.get("workers") or {}, bool(pending), blocked


def _decisions(db: Session, user_id: int) -> list[AIOptimizationDecision]:
    return list(db.scalars(select(AIOptimizationDecision).where(AIOptimizationDecision.user_id == user_id)
                           .order_by(AIOptimizationDecision.id.desc()).limit(200)).all())


def decision_dict(d: AIOptimizationDecision) -> dict[str, Any]:
    return {"id": d.id, "status": d.status, "action_id": d.action_id, "title": d.title, "report_id": d.report_id,
            "recommendation_key": d.recommendation_key, "config_version": d.config_version,
            "undo_version": d.undo_version, "draft_revision": d.draft_revision, "base_version": d.base_version,
            "detail": d.detail, "expires_at": d.expires_at.isoformat() if d.expires_at else None,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "updated_at": d.updated_at.isoformat() if d.updated_at else None}


def _plan(db: Session, user_id: int, report: AIOptimizationReport) -> tuple[list[dict[str, Any]], str | None]:
    """Each recommendation with its actions, brief and coding prompt; and why staging is blocked (or None)."""
    workers, _pending, blocked = _workers(db, user_id)
    stats = model_stats(db, user_id, report.time_range_start, report.time_range_end)
    aggregates = report.aggregates or {}
    stages = aggregates.get("stages") or []
    stage_rows = {s["stage"]: s for s in stages}
    actions = candidate_actions(workers, stats, aggregates.get("routing"), stages)
    items = []
    for index, rec in enumerate(report.recommendations or []):
        mine = actions_for(rec, actions)
        code = needs_code(rec, mine)
        stage_row = stage_rows.get(rec.get("affected_stage") or "")
        items.append({"index": index, "key": recommendation_key(rec), "actions": mine,
                      "brief": brief(rec, report, mine, stage_row, code),
                      "coding_prompt": coding_prompt(rec, report, stage_row) if code else None})
    return items, blocked


def overview(db: Session, user_id: int, report: AIOptimizationReport | None) -> dict[str, Any]:
    """What the AI optimization screen shows next to the report. Reads only (it may seed the AI configuration once)."""
    if report is None:
        return {"items": [], "blocked": None, "staged": None, "decisions": []}
    items, blocked = _plan(db, user_id, report)
    now = utcnow()
    decisions = _decisions(db, user_id)
    dismissed = {d.recommendation_key: d for d in decisions
                 if d.status == "DISMISSED" and (d.expires_at is None or d.expires_at > now)}
    conn = ai_model_config.connection_for(db, user_id)
    staged = next((d for d in decisions if d.status == "STAGED" and d.draft_revision == conn.draft_revision
                   and d.created_at and now - d.created_at < STAGED_FOR), None)
    for item in items:
        item["dismissed"] = decision_dict(dismissed[item["key"]]) if item["key"] in dismissed else None
        item["applied"] = [decision_dict(d) for d in decisions if d.recommendation_key == item["key"]
                           and d.status in ("APPLIED", "UNDONE")][:3]
    if staged is not None:  # its own edits are what makes the draft dirty: offer Apply/Cancel, not the generic block
        blocked = "A one-click change is staged and waiting: apply or cancel it first."
    return {"items": items, "blocked": blocked, "staged": decision_dict(staged) if staged else None,
            "decisions": [decision_dict(d) for d in decisions[:20]]}


# ───────────── stage → apply / cancel → undo ─────────────

def _report(db: Session, user_id: int, report_id: int) -> AIOptimizationReport:
    report = db.scalar(select(AIOptimizationReport).where(AIOptimizationReport.id == report_id,
                                                          AIOptimizationReport.user_id == user_id))
    if report is None:
        raise ActionError("Report not found", 404)
    return report


def _owned_decision(db: Session, user_id: int, decision_id: int) -> AIOptimizationDecision:
    row = db.scalar(select(AIOptimizationDecision).where(AIOptimizationDecision.id == decision_id,
                                                         AIOptimizationDecision.user_id == user_id))
    if row is None:
        raise ActionError("Decision not found", 404)
    return row


def stage(db: Session, user_id: int, report_id: int, action_id: str) -> dict[str, Any]:
    """Write one action into the AI configuration draft and preview it. Nothing goes live until apply()."""
    report = _report(db, user_id, report_id)
    items, blocked = _plan(db, user_id, report)
    if blocked:
        raise ActionError(blocked)
    found = next(((item, a) for item in items for a in item["actions"] if a["id"] == action_id), None)
    if found is None:
        raise ActionError("This change no longer applies: the configuration or the data changed since the report. "
                          "Reload, or run the analysis again.")
    item, action = found
    rec = (report.recommendations or [])[item["index"]]
    base = ai_model_config.active_version(db, user_id)
    revision = ai_model_config.connection_for(db, user_id).draft_revision
    try:
        ai_model_config.update_worker(db, user_id, action["worker_type"], action["payload"],
                                      expected_revision=revision)
        preview = ai_model_config.preview(db, user_id)
    except Exception:
        db.rollback()
        ai_model_config.discard_draft(db, user_id)
        raise
    if not preview["can_apply"]:
        ai_model_config.discard_draft(db, user_id)  # never leave a refused change waiting in the draft
        return {"decision": None, "action": action, "preview": preview}
    decision = AIOptimizationDecision(
        user_id=user_id, report_id=report.id, recommendation_key=recommendation_key(rec), action_id=action_id,
        status="STAGED", title=action["title"][:300],
        detail={"worker_type": action["worker_type"], "changes": action["changes"], "recommendation": rec.get("title")},
        draft_revision=preview["draft_revision"], base_version=preview["base_version"],
        created_at=utcnow(), updated_at=utcnow())
    db.add(decision)
    db.commit()
    return {"decision": decision_dict(decision), "action": action, "preview": preview,
            "previous_version": base.version_number if base else None}


def apply(db: Session, user_id: int, decision_id: int) -> dict[str, Any]:
    """Make a staged change live through ai_model_config.apply, refusing if the draft or version moved since."""
    decision = _owned_decision(db, user_id, decision_id)
    if decision.status != "STAGED":
        raise ActionError(f"This change is {decision.status.lower()}, not waiting to be applied")
    try:
        result = ai_model_config.apply(db, user_id, expected_revision=decision.draft_revision,
                                       expected_version=decision.base_version,
                                       summary=f"AI optimization: {decision.title}")
    except ai_model_config.ConfigConflict:
        db.rollback()
        decision = _owned_decision(db, user_id, decision_id)
        decision.status, decision.updated_at = "CANCELLED", utcnow()
        db.commit()
        raise ActionError("The AI configuration changed after this was staged, so it was not applied. Reload and "
                          "stage it again.") from None
    decision = _owned_decision(db, user_id, decision_id)
    if result.get("status") == "UNCHANGED":
        decision.status, decision.updated_at = "CANCELLED", utcnow()
        db.commit()
        return {"decision": decision_dict(decision), "result": result}
    decision.status, decision.config_version, decision.updated_at = "APPLIED", result["version"], utcnow()
    db.commit()
    return {"decision": decision_dict(decision), "result": result}


def cancel(db: Session, user_id: int, decision_id: int) -> dict[str, Any]:
    """Throw a staged change away. The draft is reset only while it still holds exactly that change."""
    decision = _owned_decision(db, user_id, decision_id)
    if decision.status != "STAGED":
        raise ActionError(f"This change is {decision.status.lower()}, not staged")
    if ai_model_config.connection_for(db, user_id).draft_revision == decision.draft_revision:
        ai_model_config.discard_draft(db, user_id)
    decision = _owned_decision(db, user_id, decision_id)
    decision.status, decision.updated_at = "CANCELLED", utcnow()
    db.commit()
    return {"decision": decision_dict(decision)}


def undo(db: Session, user_id: int, decision_id: int) -> dict[str, Any]:
    """Roll an applied change back by restoring the version before it, only while it is still the active version."""
    from app.models import AIConfigVersion

    decision = _owned_decision(db, user_id, decision_id)
    if decision.status != "APPLIED" or decision.config_version is None or decision.base_version is None:
        raise ActionError("Only an applied change can be undone")
    _workers_now, pending, _ = _workers(db, user_id)
    if pending:
        raise ActionError("AI Models has unapplied edits. Apply or discard them before undoing.")
    active = ai_model_config.active_version(db, user_id)
    if active is None or active.version_number != decision.config_version:
        raise ActionError(f"The configuration has changed since v{decision.config_version}; restore an older version "
                          "from AI Models → History instead.")
    previous = db.scalar(select(AIConfigVersion).where(AIConfigVersion.user_id == user_id,
                                                       AIConfigVersion.version_number == decision.base_version))
    if previous is None:
        raise ActionError("The version before this change no longer exists", 404)
    result = ai_model_config.restore(db, user_id, previous.id, expected_version=decision.config_version)
    decision = _owned_decision(db, user_id, decision_id)
    decision.status, decision.undo_version, decision.updated_at = "UNDONE", result.get("version"), utcnow()
    db.commit()
    return {"decision": decision_dict(decision), "result": result}


def dismiss(db: Session, user_id: int, report_id: int, key: str) -> dict[str, Any]:
    report = _report(db, user_id, report_id)
    rec = next((r for r in report.recommendations or [] if recommendation_key(r) == key), None)
    if rec is None:
        raise ActionError("Recommendation not found", 404)
    row = AIOptimizationDecision(user_id=user_id, report_id=report.id, recommendation_key=key, status="DISMISSED",
                                 title=(rec.get("title") or "")[:300], expires_at=utcnow() + DISMISS_FOR,
                                 created_at=utcnow(), updated_at=utcnow())
    db.add(row)
    db.commit()
    return {"decision": decision_dict(row)}


def restore_dismissed(db: Session, user_id: int, decision_id: int) -> dict[str, Any]:
    decision = _owned_decision(db, user_id, decision_id)
    if decision.status != "DISMISSED":
        raise ActionError("Only a dismissed recommendation can be restored")
    decision.expires_at, decision.updated_at = utcnow(), utcnow()
    db.commit()
    return {"decision": decision_dict(decision)}
