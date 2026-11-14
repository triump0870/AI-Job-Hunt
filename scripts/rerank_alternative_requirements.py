"""Re-rank jobs whose posting offers requirements as alternatives ("one of Python, Java, Go ...").

    python scripts/rerank_alternative_requirements.py                 # dry run: which jobs would be re-ranked
    python scripts/rerank_alternative_requirements.py --apply --limit 50
    python scripts/rerank_alternative_requirements.py --json

Before parser v3 / ranking v7 each alternative was its own REQUIRED requirement, so a candidate with Python was shown
Java, Kotlin and Scala as gaps. The requirement rows are not rewritten: job_context.alternative_groups groups them
when they read (from the parser's alternative_group, or for older rows from a shared "one of" / "any of" sentence).
Only the jobs where it finds a group, and that already have a ranking, are queued, through ranker.queue_job with the
normal (non-forced) cadence, so a large account re-ranks gradually. A job already ranked with the current prompt, or
already waiting for the ranker, is passed over, so repeated --limit runs move on through the backlog. A job's current fit analysis stays until its new
ranking lands. Uses DATABASE_URL; run the dry run first.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTOR = "rerank_alternatives"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="queue the re-rankings (default: dry run)")
    parser.add_argument("--limit", type=int, default=100, help="at most this many jobs per run (default 100)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import ACTIVE_RANKING_TASK_STATUSES, Job, JobRankingTask
    from app.prompts.ranking import RANKING_PROMPT_VERSION
    from app.services import job_context, ranker
    from app.services.job_description import is_current

    rows = []
    with SessionLocal() as db:
        for job in db.scalars(select(Job).where(Job.fit_score.is_not(None), Job.structured_description.is_not(None))
                              .order_by(Job.id)).all():
            if len(rows) >= args.limit:
                break
            if not is_current(job) or (job.fit_analysis or {}).get("prompt_version") == RANKING_PROMPT_VERSION:
                continue  # parsed from older wording, or already ranked with alternatives in mind
            # Every read below is scoped to this job's account (app.tenancy filters by db.info["user_id"]).
            db.info["user_id"] = job.user_id
            if db.scalar(select(JobRankingTask.id).where(JobRankingTask.job_id == job.id,
                                                         JobRankingTask.status.in_(ACTIVE_RANKING_TASK_STATUSES))):
                continue  # already waiting for the ranker
            requirements = [r for r in job_context.requirements_of(db, job)
                            if r.requirement_level in (job_context.REQUIRED, job_context.PREFERRED)]
            groups = job_context.alternative_groups(requirements)
            if not groups:
                continue
            row = {"job_id": job.id, "user_id": job.user_id, "title": job.title, "groups": len(set(groups.values()))}
            if args.apply:
                locked = db.scalars(select(Job).where(Job.id == job.id).with_for_update()
                                    .execution_options(populate_existing=True)).one()
                why = ranker.queue_job(db, locked, policy=ranker.policy_for(db, job.user_id), actor=ACTOR,
                                       reason="prompt_changed")
                db.commit()
                row["queued"], row["skipped"] = why is None, why
            rows.append(row)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            status = ("queued" if row.get("queued") else f"skipped: {row['skipped']}") if args.apply else "would queue"
            print(f"job {row['job_id']} (user {row['user_id']}) {row['title']!r}: {row['groups']} group(s), {status}")
        print(f"{len(rows)} job(s)" + ("" if args.apply else " — dry run, pass --apply to queue them"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
