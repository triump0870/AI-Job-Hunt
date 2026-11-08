"""Compare the old domain filter with the hybrid pipeline on an account's own decisions, before replacing it.

    python scripts/domain_classifier_eval.py --user 1                        # rules only: no model, no spend
    python scripts/domain_classifier_eval.py --user 1 --export-labels 80     # a sample to label by hand (data/real/)
    python scripts/domain_classifier_eval.py --user 1 --live --max-calls 100 # also ask the configured model: SPENDS

Labels, most trusted first: `data/real/domain_labels.csv` (job_id,relevant, written by a person), then what the person
did (APPLIED, APPROVED, KEPT, SHORTLISTED with no later rejection, or a fit of 70 and up = relevant; REJECTED, DISMISSED
= irrelevant). Mass deletions are unlabelled: tidying is not a judgement on a job.

Never changes a job. The session is rolled back at the end, and a live run writes only the gateway's own telemetry
rows. Real titles go only to the gitignored data/real/ folder, never into docs/. Without a person's labels, recall is
measured on the jobs the person saw, which the old filter already let through: read it next to the hand-labelled
sample of hidden jobs, which is where its false negatives actually are.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REAL = ROOT / "data" / "real"
LABELS = REAL / "domain_labels.csv"
POSITIVE = {"APPLIED", "APPROVED", "KEPT", "SHORTLISTED"}
NEGATIVE = {"REJECTED", "DISMISSED"}
PROMOTION = {"max_false_negative_rate": 0.02, "min_no_match_precision": 0.9}


def wilson(part: int, whole: int, z: float = 1.96) -> list[float] | None:
    """A 95% interval: on a couple of hundred labels a single rate says less than it seems to."""
    if not whole:
        return None
    p = part / whole
    centre = (p + z * z / (2 * whole)) / (1 + z * z / whole)
    margin = z * math.sqrt(p * (1 - p) / whole + z * z / (4 * whole * whole)) / (1 + z * z / whole)
    return [round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)]


def rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def hand_labels() -> dict[int, bool]:
    if not LABELS.exists():
        return {}
    with LABELS.open() as handle:
        rows = list(csv.DictReader(handle))
    return {int(r["job_id"]): r["relevant"].strip().lower() in ("1", "true", "yes", "y")
            for r in rows if r.get("job_id") and (r.get("relevant") or "").strip()}


def labelled_jobs(db: Any, user_id: int) -> list[tuple[Any, bool, str]]:
    """(job, relevant, where the label came from). Jobs that were deleted have no description left, so their row is a
    title and a company, labelled as `description_lost` in the report."""
    from sqlalchemy import select

    from app.models import Job, JobFeedbackEvent

    hand = hand_labels()
    jobs = {j.id: j for j in db.scalars(select(Job).where(Job.user_id == user_id)).all()}
    acts: dict[tuple[str, str], dict[str, Any]] = {}
    for e in db.scalars(select(JobFeedbackEvent).where(JobFeedbackEvent.user_id == user_id)).all():
        entry = acts.setdefault((e.title, e.company), {"actions": set(), "job_id": e.job_id})
        entry["actions"].add(e.action)
        entry["job_id"] = entry["job_id"] or e.job_id
    out: list[tuple[Any, bool, str]] = []
    seen: set[int] = set()
    for job_id, relevant in hand.items():
        if job_id in jobs:
            out.append((jobs[job_id], relevant, "hand"))
            seen.add(job_id)
    for (title, company), entry in acts.items():
        positive, negative = bool(entry["actions"] & POSITIVE), bool(entry["actions"] & NEGATIVE)
        if positive == negative:  # neither (only mass deletes), or both (they changed their mind): not a label
            continue
        job = jobs.get(entry["job_id"])
        if job is not None and job.id in seen:
            continue
        if job is not None:
            seen.add(job.id)
        out.append((job or SimpleNamespace(id=None, title=title, company=company, description="", search_run_id=None),
                    positive, "action" if job is not None else "description_lost"))
    for job in jobs.values():
        if job.id not in seen and job.fit_score is not None and job.fit_score >= 70:
            out.append((job, True, "fit"))
    return out


def export_labels(db: Any, user_id: int, count: int) -> Path:
    """A random sample of hidden jobs (one per title and company), for a person to fill in `relevant`."""
    from sqlalchemy import select

    from app.models import Job

    hidden = db.scalars(select(Job).where(Job.user_id == user_id, Job.domain_match == "NOT_MATCHING")).all()
    unique = list({(j.title.lower(), (j.company or "").lower()): j for j in hidden}.values())
    random.Random(7).shuffle(unique)
    REAL.mkdir(parents=True, exist_ok=True)
    path = REAL / "domain_labels_todo.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["job_id", "relevant", "title", "company", "hidden_because"])
        for job in unique[:count]:
            writer.writerow([job.id, "", job.title, job.company, job.domain_reason])
    return path


def scores(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    tp = sum(1 for r in rows if r["relevant"] and r[key])
    fn = sum(1 for r in rows if r["relevant"] and not r[key])
    fp = sum(1 for r in rows if not r["relevant"] and r[key])
    tn = sum(1 for r in rows if not r["relevant"] and not r[key])
    return {"recall": rate(tp, tp + fn), "false_negative_rate": rate(fn, tp + fn), "fnr_95": wilson(fn, tp + fn),
            "precision": rate(tp, tp + fp), "no_match_precision": rate(tn, tn + fn), "forwarded": tp + fp,
            "hidden": fn + tn, "false_negatives": fn, "irrelevant_filtered": rate(tn, tn + fp)}


def verdict(hybrid: dict[str, Any], legacy: dict[str, Any], measured: bool) -> str:
    if not measured:
        return "NOT MEASURED: the model was not asked. These numbers are the rules alone; run --live before deciding."
    fnr = hybrid["false_negative_rate"]
    precision = hybrid["no_match_precision"]
    if fnr is None or precision is None:
        return "NOT ENOUGH LABELS: label a sample with --export-labels."
    if fnr <= PROMOTION["max_false_negative_rate"] and fnr <= (legacy["false_negative_rate"] or 0) \
            and precision >= PROMOTION["min_no_match_precision"]:
        return "MEETS THE BAR for DOMAIN_CLASSIFIER_MODE=on. Changing the mode stays a person's decision."
    return "BELOW THE BAR: keep DOMAIN_CLASSIFIER_MODE off or shadow."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", type=int, required=True)
    parser.add_argument("--live", action="store_true", help="also ask the configured model (spends money)")
    parser.add_argument("--max-calls", type=int, default=0, help="required with --live: the most model calls to make")
    parser.add_argument("--export-labels", type=int, default=0, metavar="N", help="write N hidden jobs to label")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.live and args.max_calls <= 0:
        parser.error("--live needs --max-calls: a live run spends money, so its size is always stated")
    os.environ["DOMAIN_CLASSIFIER_MODE"] = "on"  # this process only: learn() counts the cleaned way, for the replay

    from app.db import SessionLocal
    from app.prompts import DOMAIN_CLASSIFIER_PROMPT_VERSION, build_domain_classifier_prompt
    from app.prompts.contracts import DomainClassificationResult
    from app.services import domain_classifier as dc
    from app.services import domain_match as dm
    from app.services import feedback_learning, search_scope
    from app.services.llm import LLMCall, LLMClient, LLMError

    db = SessionLocal()
    db.info["user_id"] = args.user
    try:
        if args.export_labels:
            print(export_labels(db, args.user, args.export_labels))
            return 0
        legacy_rules = dm.rules_for(db, args.user)
        cases = labelled_jobs(db, args.user)
        with db.begin_nested() as saved:
            feedback_learning.learn(db, args.user)
            db.flush()
            rules = dm.rules_for(db, args.user)
            saved.rollback()
        who = dc.candidate(db, args.user)
        client = LLMClient.for_user(db, args.user) if args.live else None
        rows, latencies, seen, calls = [], [], {}, 0
        tokens_in, tokens_out, costs, models = [], [], [], {}
        for job, relevant, source in cases:
            run_id = getattr(job, "search_run_id", None)
            scoped = search_scope.verdict_for(db, job, run_id) if run_id else None
            legacy_kept = dm.assess(job, legacy_rules).match and (scoped is None or scoped.match)
            place, doubts = search_scope.hybrid(db, job, run_id, rules) if run_id else (None, ())
            triaged = dm.triage(job, rules, doubts)
            route = triaged.route if place is None else dm.HARD
            row = {"job_id": job.id, "title": job.title, "relevant": relevant, "label": source, "legacy_kept": legacy_kept,
                   "route": route, "signals": list(triaged.signals), "model": None, "gate_code": None, "cached": False,
                   "legacy_reason": dm.assess(job, legacy_rules).reason}
            kept = route != dm.HARD  # rules alone: an ASK goes on
            if route == dm.ASK and client is not None:
                window = dc.posting_window(job.description)
                key = dc.cache_key(dc.fingerprint(who), DOMAIN_CLASSIFIER_PROMPT_VERSION, job.title, job.company or "", window)
                answer = seen.get(key)
                row["cached"] = answer is not None
                if answer is None and calls < args.max_calls:
                    calls += 1
                    started = time.perf_counter()
                    try:
                        result = client.generate_prompt(
                            build_domain_classifier_prompt(who, job, window),
                            LLMCall(workflow_type="job", entity_id=job.id,
                                    validate=DomainClassificationResult.model_validate))
                        answer = result.validated
                        seen[key] = answer
                        latencies.append(round((time.perf_counter() - started) * 1000))
                        models[result.model] = models.get(result.model, 0) + 1
                        if result.usage is not None:  # what the provider reported; unknown stays out of the totals
                            if result.usage.input_tokens is not None:
                                tokens_in.append(result.usage.input_tokens)
                            if result.usage.output_tokens is not None:
                                tokens_out.append(result.usage.output_tokens)
                            if result.usage.reported_cost is not None:
                                costs.append(result.usage.reported_cost)
                    except LLMError as exc:
                        row["gate_code"] = f"{dc.UNAVAILABLE}:{type(exc).__name__}"
                if answer is not None:
                    decision = dc.decide(answer, title=job.title, posting=window, rules=rules,
                                         full_text=dc.job_skill_text(job))
                    row.update(model=answer.classification, classification=decision.classification,
                               gate_code=decision.gate_code, reason=decision.reason, evidence=decision.evidence,
                               action=dc.settle(decision, title=job.title, skills=decision.job_skills, rules=rules,
                                                title_only=not window))
                    kept = decision.classification != dc.NO_MATCH
                else:
                    row.setdefault("classification", None)
            row["hybrid_kept"] = kept
            rows.append(row)
        asked = sum(r["route"] == dm.ASK for r in rows)
        answered = sum(r.get("model") is not None for r in rows)
        legacy, hybrid = scores(rows, "legacy_kept"), scores(rows, "hybrid_kept")
        report = {
            "cases": len(rows), "relevant": sum(r["relevant"] for r in rows),
            "labels": {k: sum(r["label"] == k for r in rows) for k in ("hand", "action", "fit", "description_lost")},
            "legacy": legacy, "hybrid": hybrid,
            "routes": {k: sum(r["route"] == k for r in rows) for k in (dm.HARD, dm.POSITIVE, dm.ASK)},
            "needs_review": sum(r.get("action") == dc.REVIEW_JOB for r in rows),
            "needs_review_relevant": sum(r.get("action") == dc.REVIEW_JOB and r["relevant"] for r in rows),
            "ask_rate": rate(asked, len(rows)), "model_calls": calls, "answered": answered, "cache_hits": sum(r["cached"] for r in rows),
            "not_read_by_the_model": asked - answered, "latency_p50_ms": round(statistics.median(latencies)) if latencies else None,
            "latency_p95_ms": sorted(latencies)[int(len(latencies) * 0.95) - 1] if len(latencies) > 1 else None,
            "gate_codes": {c: sum(r["gate_code"] == c for r in rows) for c in {r["gate_code"] for r in rows} if c},
            "models": models, "input_tokens": sum(tokens_in) if tokens_in else None,
            "output_tokens": sum(tokens_out) if tokens_out else None,
            "tokens_per_call": round((sum(tokens_in) + sum(tokens_out)) / len(tokens_in)) if tokens_in else None,
            "reported_cost": round(sum(costs), 6) if costs else None, "calls_with_cost": len(costs),
            "verdict": verdict(hybrid, legacy, measured=bool(args.live) and answered == asked)}
        REAL.mkdir(parents=True, exist_ok=True)
        (REAL / "domain_filter_comparison.json").write_text(json.dumps({**report, "rows": rows}, indent=1, default=str))
        if args.json:
            print(json.dumps(report, indent=1))
        else:
            print(f"{len(rows)} labelled jobs ({report['relevant']} relevant): {report['labels']}")
            for name, s in (("old filter", legacy), ("hybrid", hybrid)):
                print(f"  {name:11} recall {s['recall']}  false negatives {s['false_negatives']} "
                      f"(rate {s['false_negative_rate']}, 95% {s['fnr_95']})  precision {s['precision']}  "
                      f"irrelevant filtered {s['irrelevant_filtered']}")
            print(f"  routes {report['routes']}  sent to the model {report['ask_rate']}  calls {calls}  "
                  f"p50 {report['latency_p50_ms']} ms  p95 {report['latency_p95_ms']} ms")
            print(f"  models {report['models']}  tokens per call {report['tokens_per_call']}  "
                  f"reported cost {report['reported_cost']} (on {report['calls_with_cost']} calls)  "
                  f"gate codes {report['gate_codes']}")
            print(f"  left for the person to review: {report['needs_review']} "
                  f"({report['needs_review_relevant']} of them labelled relevant)")
            print(f"  {report['verdict']}")
            print(f"  rows, with titles: {REAL / 'domain_filter_comparison.json'}")
        return 0
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
