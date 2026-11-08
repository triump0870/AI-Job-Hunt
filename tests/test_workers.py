"""Running workers: listing every queue, killing (stop and requeue fresh) and running a task now.

Task rows are written directly, the way the workers write them, and the controls are exercised through the API. The
claims used here (JD parsing, JD recovery, tailoring) work on SQLite; nothing calls a real model or website.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import (
    Job,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobRankingTask,
    ProviderCooldown,
    ResumeVariant,
    TailorTask,
    WorkflowTransition,
)
from app.services import backoff, external_api, jd_recovery, job_description, ranker, tailor_tasks, workers
from app.settings import get_settings
from tests.conftest import make_user


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def clean_cooldown_cache():
    backoff.reset_cache()
    yield
    backoff.reset_cache()


@pytest.fixture
def user_id():
    return make_user(SessionLocal)


@pytest.fixture
def signed_in(monkeypatch):
    def sign_in(uid):
        monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(uid, f"{uid}@jobhunt.test", False))
        return TestClient(app)
    return sign_in


def add_job(db, uid, **fields):
    values = dict(user_id=uid, source="test", source_ref=uuid.uuid4().hex, title="Staff Engineer", company="Canva",
                  description="Build Python services. " * 20, url=f"https://example.com/{uuid.uuid4().hex}")
    values.update(fields)
    job = Job(**values)
    db.add(job)
    db.flush()
    return job


def parse_task(db, uid, job, **fields):
    task = JobDescriptionParseTask(**{"user_id": uid, "job_id": job.id, "description_hash": "h", "status": "PENDING",
                                      "attempt_count": 0, **fields})
    db.add(task)
    db.flush()
    return task


# ───────────── listing ─────────────

def test_every_worker_lists_its_own_tasks_with_counts(user_id, signed_in):
    other = make_user(SessionLocal)
    with SessionLocal() as db:
        a, b, c = (add_job(db, user_id) for _ in range(3))
        parse_task(db, user_id, a, status="PROCESSING", started_at=utcnow(), lock_token="t", locked_by="w",
                   lock_expires_at=utcnow() + timedelta(minutes=5), attempt_count=1)
        parse_task(db, user_id, b, attempt_count=1, next_attempt_at=utcnow() + timedelta(minutes=2),
                   last_error=f"Timeout talking to sk-live-{'x' * 30}")
        db.add(TailorTask(user_id=user_id, job_id=c.id, status="queued", stage="queued", attempts=0))
        theirs = add_job(db, other, company="Hooli")
        parse_task(db, other, theirs)
        db.commit()
    client = signed_in(user_id)
    summary = client.get("/api/workers").json()
    kinds = {w["kind"]: w for w in summary["workers"]}
    assert list(kinds) == ["parsing", "recovery", "ranking", "qualification", "preparation", "application", "insights",
                           "tailoring", "reviewing", "indexing", "resume_parsing"]
    assert kinds["parsing"]["counts"] == {"running": 1, "queued": 0, "retrying": 1, "active": 2}
    assert kinds["tailoring"]["counts"]["queued"] == 1 and summary["active"] == 3
    parsing = client.get("/api/workers/parsing").json()
    statuses = [r["status"] for r in parsing["rows"]]
    assert statuses == ["RUNNING", "RETRYING"]
    retry = parsing["rows"][1]
    assert retry["can_kill"] and retry["can_run_now"] and retry["next_attempt_at"]
    assert "sk-live" not in retry["last_error"]
    assert "Hooli" not in str(parsing) and "metrics" in parsing
    assert client.get("/api/workers/nothing").status_code == 404


def test_the_ranking_tab_keeps_the_tracker_facts(user_id, signed_in):
    with SessionLocal() as db:
        job = add_job(db, user_id, state="RANK_PENDING")
        db.add(JobRankingTask(user_id=user_id, job_id=job.id, status="PENDING"))
        db.commit()
    out = signed_in(user_id).get("/api/workers/ranking").json()
    assert out["tracker"]["counts"]["queued"] >= 1 and "interval_minutes" in out["tracker"]
    assert out["rows"][0]["queue_position"] == 1 and out["rows"][0]["status"] == "QUEUED"


# ───────────── kill ─────────────

def test_killing_a_running_parse_stops_it_for_good_and_the_old_run_cannot_write(user_id, signed_in):
    with SessionLocal() as db:
        job = add_job(db, user_id)
        task = parse_task(db, user_id, job, attempt_count=2)
        db.commit()
        task_id, job_id = task.id, job.id
    with SessionLocal() as db:
        [claim] = [c for c in job_description.claim(db, worker_id="w1", limit=50) if c.task_id == task_id]
    client = signed_in(user_id)
    out = client.post(f"/api/workers/parsing/tasks/{task_id}/kill").json()
    assert (out["stopped"], out["running"], out["cooldowns_cleared"]) == (1, 1, [])
    with SessionLocal() as db:
        task = db.get(JobDescriptionParseTask, task_id)
        assert (task.status, task.lock_token, task.next_attempt_at, task.last_error) == ("FAILED", None, None,
                                                                                         "Stopped by you")
        assert job_description._held(db, claim) is None  # whatever the old run finishes is discarded
        job_description.sweep(db)  # the automatic sweep does not queue it again
        assert db.query(JobDescriptionParseTask).filter_by(job_id=job_id, status="PENDING").count() == 0
    row = next(r for r in client.get("/api/workers/parsing").json()["rows"] if r["task_id"] == task_id)
    assert row["status"] == "STOPPED" and row["can_run_now"] and not row["can_kill"]
    # Retry brings it back when the person asks.
    assert client.post("/api/workers/parsing/retry", json={"task_ids": [task_id]}).json()["retried"]


def test_killing_a_parse_lets_the_ranking_waiting_for_it_run_now(user_id, signed_in, monkeypatch):
    monkeypatch.setattr(get_settings(), "jd_parse_enabled", True)
    with SessionLocal() as db:
        job = add_job(db, user_id)
        # Running, as a claim leaves it (claiming a batch could pick other accounts' leftover tasks first).
        task = parse_task(db, user_id, job, attempt_count=1, status="PROCESSING", locked_by="w1",
                          lock_token=uuid.uuid4().hex,
                          lock_expires_at=utcnow() + timedelta(minutes=5))
        db.commit()
        task_id, job_id = task.id, job.id
    released = []
    monkeypatch.setattr(ranker, "release_parked", lambda db, job_id: released.append(job_id) or 1)
    assert signed_in(user_id).post(f"/api/workers/parsing/tasks/{task_id}/kill").json()["stopped"] == 1
    assert released == [job_id]
    with SessionLocal() as db:  # and ranking reads the raw text instead of queueing the stopped parse again
        assert job_description.hold_for_parse(db, db.get(Job, job_id)) is None


def test_killing_a_running_ranking_takes_the_job_out_of_the_queue(user_id, signed_in):
    with SessionLocal() as db:
        job = add_job(db, user_id, state="RANKING", previous_state="RANK_PENDING")
        task = JobRankingTask(user_id=user_id, job_id=job.id, status="PROCESSING", attempt_count=2, lock_token="tok",
                              locked_by="w1", locked_at=utcnow(), lock_expires_at=utcnow() + timedelta(minutes=10),
                              started_at=utcnow())
        db.add(task)
        db.commit()
        job_id, task_id = job.id, task.id
    signed_in(user_id).post(f"/api/workers/ranking/tasks/{task_id}/kill").raise_for_status()
    with SessionLocal() as db:
        job, task = db.get(Job, job_id), db.get(JobRankingTask, task_id)
        assert job.state.value == "SKIPPED" and task.status == "SKIPPED" and task.last_error == "Stopped by you"
        moves = [m.to_state.value for m in db.scalars(select(WorkflowTransition).where(
            WorkflowTransition.job_id == job_id).order_by(WorkflowTransition.id)).all()]
        assert moves[-2:] == ["RANK_PENDING", "SKIPPED"]  # recorded workflow moves, by the person
        claim = ranker.TaskClaim(task_id, job_id, user_id, "tok", "w1", 2, False, utcnow())
        with pytest.raises(ranker.LeaseLost):
            ranker._owned(db, claim)


def test_kill_all_clears_the_accounts_cooldowns_and_kill_one_does_not(user_id, signed_in):
    with SessionLocal() as db:
        job = add_job(db, user_id)
        task = parse_task(db, user_id, job, attempt_count=1, next_attempt_at=utcnow() + timedelta(hours=1))
        db.add(ProviderCooldown(user_id=user_id, provider="OPENROUTER", until=utcnow() + timedelta(minutes=5),
                                consecutive=3, calls_skipped=0))
        db.commit()
        task_id = task.id
    client = signed_in(user_id)
    assert client.post(f"/api/workers/parsing/tasks/{task_id}/kill").json()["cooldowns_cleared"] == []
    assert len(client.get("/api/workers").json()["cooldowns"]) == 1
    out = client.post("/api/workers/parsing/kill-all").json()
    assert out["cooldowns_cleared"] == ["OPENROUTER"]
    assert client.get("/api/workers").json()["cooldowns"] == []
    assert client.post("/api/workers/kill-all").json()["cooldowns_cleared"] == []


def test_a_task_waiting_its_first_turn_has_nothing_to_kill(user_id, signed_in):
    with SessionLocal() as db:
        task = parse_task(db, user_id, add_job(db, user_id))
        done = parse_task(db, user_id, add_job(db, user_id), status="COMPLETED", completed_at=utcnow())
        db.commit()
        task_id, done_id = task.id, done.id
    client = signed_in(user_id)
    assert client.post(f"/api/workers/parsing/tasks/{task_id}/kill").json()["stopped"] == 0
    assert client.post(f"/api/workers/parsing/tasks/{done_id}/kill").status_code == 409


def test_a_killed_tailoring_run_stops_for_good_and_can_never_save(user_id, signed_in, monkeypatch):
    with SessionLocal() as db:
        db.execute(delete(TailorTask))  # the claim takes the oldest queued task of any account
        job = add_job(db, user_id, status="shortlisted", title="Original title")
        db.add(TailorTask(user_id=user_id, job_id=job.id, status="queued", stage="queued", attempts=0))
        db.commit()
        job_id = job.id
    task_id = tailor_tasks.claim_next(SessionLocal, "worker-1", 900)
    with SessionLocal() as db:
        first_holder = db.get(TailorTask, task_id).locked_by
    assert first_holder.startswith("worker-1#")
    client = signed_in(user_id)
    client.post(f"/api/workers/tailoring/tasks/{task_id}/kill").raise_for_status()
    with SessionLocal() as db:
        task = db.get(TailorTask, task_id)
        assert (task.status, task.error, task.locked_by) == ("cancelled", "Stopped by you", None)
        assert db.get(Job, job_id).auto_tailor_status == "DECLINED"  # automatic tailoring leaves it alone
    assert tailor_tasks.claim_next(SessionLocal, "worker-1", 900) is None  # stopped: not picked up again
    assert tailor_tasks._write(SessionLocal, task_id, first_holder, stage="tailoring") is False

    # Run again on request; then stop it while the model "runs": its save is refused inside the transaction itself,
    # even without a progress checkpoint in between.
    fresh = client.post(f"/api/workers/tailoring/tasks/{task_id}/run-now").json()["task_id"]
    assert tailor_tasks.claim_next(SessionLocal, "worker-1", 900) == fresh

    def tailor(db, job, progress, pages=None):  # stands in for workflow.tailor_job: the model call, then the save
        client.post(f"/api/workers/tailoring/tasks/{fresh}/kill").raise_for_status()  # stopped mid-run
        job.title = "Changed by a stopped run"
        db.commit()
    monkeypatch.setattr(tailor_tasks, "tailor_job", tailor)
    assert tailor_tasks.run_task(SessionLocal, fresh, "worker-1", 900) == "abandoned"
    with SessionLocal() as db:
        assert db.get(Job, job_id).title == "Original title"
        assert db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job_id)) is None
        assert db.get(TailorTask, fresh).status == "cancelled"
        assert db.get(TailorTask, fresh).run_now_at is None  # the run-now marker was used once and cleared
        db.execute(delete(TailorTask))
        db.commit()


def test_a_savepoint_in_a_tailoring_run_never_locks_its_task(user_id, signed_in, monkeypatch):
    """A savepoint released mid-run (retrieval does one) must not lock the task row: on PostgreSQL the lock would last
    until the run's real commit, so the next progress() and any Kill — other connections writing that row — would
    wait for it forever."""
    with SessionLocal() as db:
        db.execute(delete(TailorTask))
        job = add_job(db, user_id, status="shortlisted")
        db.add(TailorTask(user_id=user_id, job_id=job.id, status="queued", stage="queued", attempts=0))
        db.commit()
    task_id = tailor_tasks.claim_next(SessionLocal, "worker-1", 900)
    client = signed_in(user_id)

    def tailor(db, job, progress, pages=None):
        with db.begin_nested():
            db.get(Job, job.id)
        with SessionLocal() as other:  # what Kill does to the row, but failing fast instead of waiting forever
            if other.get_bind().dialect.name == "postgresql":
                other.execute(text("SET LOCAL lock_timeout = '2s'"))
            other.execute(select(TailorTask.id).where(TailorTask.id == task_id).with_for_update()).first()
            other.rollback()
        progress("tailoring")
        client.post(f"/api/workers/tailoring/tasks/{task_id}/kill").raise_for_status()
    monkeypatch.setattr(tailor_tasks, "tailor_job", tailor)
    assert tailor_tasks.run_task(SessionLocal, task_id, "worker-1", 900) == "failed"  # the stub saves no packet
    with SessionLocal() as db:
        assert (db.get(TailorTask, task_id).status, db.get(TailorTask, task_id).error) == ("cancelled", "Stopped by you")
        db.execute(delete(TailorTask))
        db.commit()


# ───────────── run now ─────────────

def test_run_now_jumps_the_queue_and_skips_the_retry_wait(user_id, signed_in):
    with SessionLocal() as db:
        first = parse_task(db, user_id, add_job(db, user_id))
        waiting = parse_task(db, user_id, add_job(db, user_id), attempt_count=1,
                             next_attempt_at=utcnow() + timedelta(hours=1))
        db.commit()
        waiting_id, first_id = waiting.id, first.id
    out = signed_in(user_id).post(f"/api/workers/parsing/tasks/{waiting_id}/run-now").json()
    assert out["run_now"] is True and out["next_attempt_at"] is None
    with SessionLocal() as db:
        claims = job_description.claim(db, worker_id="w", limit=500)
        mine = [c for c in claims if c.task_id in (waiting_id, first_id)]
        assert mine[0].task_id == waiting_id and mine[0].run_now is True
        assert next(c for c in mine if c.task_id == first_id).run_now is False
        assert db.get(JobDescriptionParseTask, waiting_id).run_now_at is None  # consumed by the claim


def test_run_now_on_a_recovery_task_is_carried_by_its_claim(user_id, signed_in):
    with SessionLocal() as db:
        task = JobDescriptionFetchTask(user_id=user_id, job_id=add_job(db, user_id).id, status="PENDING",
                                       attempt_count=1, next_attempt_at=utcnow() + timedelta(hours=1))
        db.add(task)
        db.commit()
        task_id = task.id
    signed_in(user_id).post(f"/api/workers/recovery/tasks/{task_id}/run-now").raise_for_status()
    with SessionLocal() as db:
        claim = next(c for c in jd_recovery.claim(db, worker_id="w", limit=500) if c.task_id == task_id)
    assert claim.run_now is True


def test_run_now_requeues_a_failed_task_and_refuses_running_or_foreign_ones(user_id, signed_in):
    other = make_user(SessionLocal)
    with SessionLocal() as db:
        failed = parse_task(db, user_id, add_job(db, user_id), status="FAILED", attempt_count=3,
                            completed_at=utcnow(), last_error="boom")
        running = parse_task(db, user_id, add_job(db, user_id), status="PROCESSING", lock_token="t")
        theirs = parse_task(db, other, add_job(db, other))
        ranking_job = add_job(db, user_id, state="RANK_PENDING")
        rank = JobRankingTask(user_id=user_id, job_id=ranking_job.id, status="PENDING")
        db.add(rank)
        db.commit()
        ids = failed.id, running.id, theirs.id, rank.id
    client = signed_in(user_id)
    out = client.post(f"/api/workers/parsing/tasks/{ids[0]}/run-now").json()
    assert out["task_id"] != ids[0] and out["status"] == "QUEUED" and out["run_now"] is True
    assert client.post(f"/api/workers/parsing/tasks/{ids[1]}/run-now").status_code == 409
    assert client.post(f"/api/workers/parsing/tasks/{ids[2]}/run-now").status_code == 404
    client.post(f"/api/workers/ranking/tasks/{ids[3]}/run-now").raise_for_status()
    with SessionLocal() as db:
        task = db.get(JobRankingTask, ids[3])
        assert task.force is True and task.run_now_at is not None  # the cadence never holds it back


def test_a_bypassed_call_skips_the_cooldown_but_others_still_wait(user_id):
    with SessionLocal() as db:
        db.add(ProviderCooldown(user_id=user_id, provider="OPENROUTER", until=utcnow() + timedelta(minutes=5),
                                consecutive=1, calls_skipped=0))
        db.commit()
    call = external_api.ExternalCall(provider="OPENROUTER", service="llm", operation="chat", user_id=user_id)
    with pytest.raises(external_api.RateLimitedCooldown):
        external_api.hold_off(call)
    with backoff.bypass():
        external_api.hold_off(call)  # no exception: this task was asked to run now
    with backoff.bypass(False):
        with pytest.raises(external_api.RateLimitedCooldown):
            external_api.hold_off(call)


def test_reset_cooldowns_endpoint(user_id, signed_in):
    with SessionLocal() as db:
        for provider in ("OPENROUTER", "APIFY"):
            db.add(ProviderCooldown(user_id=user_id, provider=provider, until=utcnow() + timedelta(minutes=5),
                                    consecutive=1, calls_skipped=0))
        db.commit()
    client = signed_in(user_id)
    assert client.post("/api/workers/cooldowns/reset?provider=apify").json() == {"cleared": ["APIFY"]}
    assert [c["provider"] for c in client.get("/api/workers").json()["cooldowns"]] == ["OPENROUTER"]


def test_kinds_registry_is_complete():
    assert set(workers.KINDS) == {"parsing", "recovery", "ranking", "qualification", "preparation", "application",
                                  "insights", "tailoring", "reviewing", "indexing", "resume_parsing"}
    for spec in workers.KINDS.values():
        assert hasattr(spec.model, "run_now_at") and spec.max_attempts() >= 1


# ───────────── reviewing: the review step of a tailoring task ─────────────

def test_reviewing_shows_the_tailor_tasks_at_the_review_step(user_id, signed_in):
    with SessionLocal() as db:
        tailoring, reviewing, review_only, reviewed, failed_early = (add_job(db, user_id) for _ in range(5))
        lease = dict(locked_by="w", lock_expires_at=utcnow() + timedelta(minutes=5), started_at=utcnow())
        db.add(TailorTask(user_id=user_id, job_id=tailoring.id, status="running", stage="tailoring", attempts=1, **lease))
        review = TailorTask(user_id=user_id, job_id=reviewing.id, status="running", stage="reviewing", attempts=1, **lease)
        db.add(review)
        db.add(TailorTask(user_id=user_id, job_id=review_only.id, mode="skip", status="queued", stage="queued", attempts=0))
        db.add(TailorTask(user_id=user_id, job_id=reviewed.id, status="succeeded", stage="reviewing", attempts=1,
                          finished_at=utcnow(), result={"reviewer_score": 82}))
        early = TailorTask(user_id=user_id, job_id=failed_early.id, status="failed", stage="tailoring", attempts=1,
                           finished_at=utcnow(), error="boom")
        db.add(early)
        db.commit()
        review_id, early_id, job_ids = review.id, early.id, {reviewing.id, review_only.id, reviewed.id}
    client = signed_in(user_id)
    summary = client.get("/api/workers").json()
    kinds = {w["kind"]: w for w in summary["workers"]}
    assert kinds["reviewing"]["counts"] == {"running": 1, "queued": 1, "retrying": 0, "active": 2}
    assert kinds["reviewing"]["view_of"] == "tailoring"
    # The review step is part of a tailoring task: counted once in the totals.
    assert kinds["tailoring"]["counts"]["active"] == 3 and summary["active"] == 3 and summary["running"] == 2
    rows = client.get("/api/workers/reviewing").json()["rows"]
    assert {r["job_id"] for r in rows} == job_ids
    done = next(r for r in rows if r["status"] == "SUCCEEDED")
    assert done["reviewer_score"] == 82 and done["stage"] == "done"
    # A task outside the review step is not Reviewing's to touch.
    assert client.post(f"/api/workers/reviewing/tasks/{early_id}/run-now").status_code == 404
    out = client.post(f"/api/workers/reviewing/tasks/{review_id}/kill").json()
    assert (out["stopped"], out["running"]) == (1, 1)
    with SessionLocal() as db:
        task = db.get(TailorTask, review_id)
        assert (task.status, task.stage, task.locked_by) == ("cancelled", "reviewing", None)
    stopped = next(r for r in client.get("/api/workers/reviewing").json()["rows"] if r["task_id"] == review_id)
    assert stopped["status"] == "STOPPED"


def test_kill_all_workers_does_not_stop_the_review_step_twice(user_id, signed_in):
    with SessionLocal() as db:
        job = add_job(db, user_id)
        db.add(TailorTask(user_id=user_id, job_id=job.id, status="running", stage="reviewing", attempts=1,
                          locked_by="w", lock_expires_at=utcnow() + timedelta(minutes=5), started_at=utcnow()))
        db.commit()
    with SessionLocal() as db:
        out = workers.kill_all(db, user_id)
    assert "reviewing" not in out["workers"] and out["stopped"] == 1


# ───────────── filters ─────────────

def test_tasks_filter_by_search_and_status_with_counts_per_status(user_id, signed_in):
    with SessionLocal() as db:
        canva = add_job(db, user_id, title="Staff SWE", company="Canva")
        stripe = add_job(db, user_id, title="Backend Lead", company="Stripe")
        grab = add_job(db, user_id, title="AI Engineer", company="Grab")
        parse_task(db, user_id, canva, status="PROCESSING", lock_token="t", started_at=utcnow())
        parse_task(db, user_id, stripe, attempt_count=1, next_attempt_at=utcnow() + timedelta(minutes=5))
        parse_task(db, user_id, grab, status="FAILED", completed_at=utcnow(), last_error="boom")
        db.commit()
        grab_id = grab.id
    client = signed_in(user_id)
    every = client.get("/api/workers/parsing").json()
    assert every["status_counts"] == {"active": 2, "running": 1, "queued": 0, "retrying": 1, "failed": 1, "done": 0,
                                      "all": 3}
    assert [r["company"] for r in client.get("/api/workers/parsing?status=failed").json()["rows"]] == ["Grab"]
    assert [r["company"] for r in client.get("/api/workers/parsing?status=active").json()["rows"]] == ["Canva", "Stripe"]
    named = client.get("/api/workers/parsing?q=stri").json()
    assert [r["company"] for r in named["rows"]] == ["Stripe"] and named["status_counts"]["all"] == 1
    assert [r["company"] for r in client.get("/api/workers/parsing?q=backend lead").json()["rows"]] == ["Stripe"]
    assert [r["job_id"] for r in client.get(f"/api/workers/parsing?q=%23{grab_id}").json()["rows"]] == [grab_id]
    assert client.get("/api/workers/parsing?q=hooli").json()["rows"] == []
    assert client.get("/api/workers/parsing?status=all").json()["total"] == 3
    assert client.get("/api/workers/parsing?status=sleeping").status_code == 422
    # Filters narrow the task list, never the metrics.
    assert every["metrics"] == client.get("/api/workers/parsing?status=failed").json()["metrics"]


def test_activity_counts_the_last_day_hour_by_hour(user_id, signed_in):
    now = utcnow()
    other = make_user(SessionLocal)
    with SessionLocal() as db:
        a, b, c, d = (add_job(db, user_id) for _ in range(4))
        parse_task(db, user_id, a, status="COMPLETED", created_at=now - timedelta(hours=3),
                   started_at=now - timedelta(hours=3) + timedelta(seconds=30),
                   completed_at=now - timedelta(hours=3) + timedelta(seconds=90))
        parse_task(db, user_id, b, status="FAILED", created_at=now - timedelta(minutes=20),
                   started_at=now - timedelta(minutes=19), completed_at=now - timedelta(minutes=18))
        parse_task(db, user_id, c, status="COMPLETED", created_at=now - timedelta(days=3),
                   completed_at=now - timedelta(days=2))  # outside the day
        parse_task(db, user_id, d)  # queued now
        parse_task(db, other, add_job(db, other), status="FAILED", completed_at=now)  # another account
        db.commit()
    out = signed_in(user_id).get("/api/workers/parsing/activity").json()
    assert len(out["hours"]) == 24
    assert (out["done"], out["failed"], out["queued"]) == (1, 1, 3)
    assert out["success_rate"] == 0.5
    assert out["median_run_seconds"] == 60.0 and out["median_wait_seconds"] == 45.0
    assert sum(h["failed"] for h in out["hours"][-2:]) == 1 and out["hours"][-1]["queued"] >= 1
    assert signed_in(user_id).get("/api/workers/nope/activity").status_code == 404


def test_a_failed_task_run_again_cannot_be_run_now_twice(user_id, signed_in):
    with SessionLocal() as db:
        job = add_job(db, user_id)
        failed = parse_task(db, user_id, job, status="FAILED", attempt_count=3, completed_at=utcnow(), last_error="502")
        db.commit()
        failed_id = failed.id
    client = signed_in(user_id)
    assert next(r for r in client.get("/api/workers/parsing").json()["rows"] if r["task_id"] == failed_id)["can_run_now"]
    fresh = client.post(f"/api/workers/parsing/tasks/{failed_id}/run-now").json()
    assert fresh["task_id"] != failed_id and fresh["run_now"] is True
    old = next(r for r in client.get("/api/workers/parsing").json()["rows"] if r["task_id"] == failed_id)
    assert old["can_run_now"] is False
    assert old["retried_by"] == {"task_id": fresh["task_id"], "status": "QUEUED"}
    again = client.post(f"/api/workers/parsing/tasks/{failed_id}/run-now")
    assert again.status_code == 409 and f"#{fresh['task_id']}" in again.json()["detail"]
    with SessionLocal() as db:  # still exactly one new task for the job
        assert len([t for t in db.query(JobDescriptionParseTask).filter_by(job_id=fresh["job_id"]).all()
                    if t.status == "PENDING"]) == 1


# ───────────── bulk retry ─────────────

def test_bulk_retry_queues_each_failed_task_once_and_says_why_others_were_skipped(user_id, signed_in):
    other = make_user(SessionLocal)
    with SessionLocal() as db:
        jobs = [add_job(db, user_id) for _ in range(4)]
        a = parse_task(db, user_id, jobs[0], status="FAILED", attempt_count=3, completed_at=utcnow(), last_error="502")
        b = parse_task(db, user_id, jobs[1], status="FAILED", attempt_count=3, completed_at=utcnow(), last_error="502")
        old = parse_task(db, user_id, jobs[2], status="FAILED", attempt_count=3, completed_at=utcnow())
        parse_task(db, user_id, jobs[2])  # old was already run again
        running = parse_task(db, user_id, jobs[3], status="PROCESSING", lock_token="t", started_at=utcnow())
        theirs = parse_task(db, other, add_job(db, other), status="FAILED", completed_at=utcnow())
        db.commit()
        ids = [a.id, b.id, old.id, running.id, theirs.id]
    client = signed_in(user_id)
    out = client.post("/api/workers/parsing/retry", json={"task_ids": ids + [a.id]}).json()
    assert [r["task_id"] for r in out["retried"]] == [a.id, b.id]
    reasons = {s["task_id"]: s["reason"] for s in out["skipped"]}
    assert reasons[old.id].startswith("Already run again as task #")
    assert reasons[running.id] == "Not failed (running)" and reasons[theirs.id] == "Not found"
    with SessionLocal() as db:
        for r in out["retried"]:
            fresh = db.get(JobDescriptionParseTask, r["new_task_id"])
            assert (fresh.status, fresh.job_id, fresh.run_now_at) == ("PENDING", r["job_id"], None)  # normal order
    again = client.post("/api/workers/parsing/retry", json={"task_ids": [a.id]}).json()
    assert again["retried"] == [] and again["skipped"][0]["reason"].startswith("Already run again")
    assert client.post("/api/workers/parsing/retry", json={"task_ids": []}).status_code == 422
    assert client.post("/api/workers/nope/retry", json={"task_ids": [1]}).status_code == 404


def test_bulk_retry_requeues_failed_tailoring_with_its_mode(user_id, signed_in):
    with SessionLocal() as db:
        db.execute(delete(TailorTask))
        job = add_job(db, user_id, status="shortlisted")
        task = TailorTask(user_id=user_id, job_id=job.id, status="failed", stage="done", attempts=2, mode="tailor",
                          error="Cannot connect to host ollama.com:443", finished_at=utcnow())
        db.add(task)
        db.commit()
        task_id = task.id
    out = signed_in(user_id).post("/api/workers/tailoring/retry", json={"task_ids": [task_id]}).json()
    assert len(out["retried"]) == 1 and out["skipped"] == []
    with SessionLocal() as db:
        fresh = db.get(TailorTask, out["retried"][0]["new_task_id"])
        assert (fresh.status, fresh.mode, fresh.run_now_at) == ("queued", "tailor", None)
        db.execute(delete(TailorTask))
        db.commit()


# ───────────── clear finished ─────────────

def test_clear_finished_hides_the_list_only_and_can_be_undone(user_id, signed_in):
    with SessionLocal() as db:
        done = parse_task(db, user_id, add_job(db, user_id), status="COMPLETED", completed_at=utcnow() - timedelta(minutes=5))
        failed = parse_task(db, user_id, add_job(db, user_id), status="FAILED", completed_at=utcnow() - timedelta(minutes=3))
        waiting = parse_task(db, user_id, add_job(db, user_id))
        db.commit()
        ids = done.id, failed.id, waiting.id
    client = signed_in(user_id)
    before = client.get("/api/workers/parsing").json()
    assert {r["task_id"] for r in before["rows"]} >= set(ids) and before["cleared_at"] is None
    out = client.post("/api/workers/parsing/clear").json()
    assert out["cleared_at"]
    after = client.get("/api/workers/parsing").json()
    shown = {r["task_id"] for r in after["rows"]}
    assert ids[2] in shown and ids[0] not in shown and ids[1] not in shown  # queued work never hides
    assert after["cleared_hidden"] >= 2 and after["status_counts"]["failed"] == 0
    assert next(w for w in client.get("/api/workers").json()["workers"] if w["kind"] == "parsing")["cleared_at"]
    assert client.get("/api/workers/parsing/activity").json()["done"] >= 1  # history and charts keep them
    with SessionLocal() as db:
        assert db.get(JobDescriptionParseTask, ids[0]) is not None  # nothing deleted
    client.delete("/api/workers/parsing/clear").raise_for_status()
    assert {r["task_id"] for r in client.get("/api/workers/parsing").json()["rows"]} >= set(ids)
    # Ranking shares the tracker's own clear.
    client.post("/api/workers/ranking/clear").raise_for_status()
    with SessionLocal() as db:
        assert ranker.finished_cleared_at(db, user_id) is not None
    assert client.post("/api/workers/nope/clear").status_code == 404


def test_performance_counts_the_range_and_clear_finished_never_changes_it(user_id, signed_in):
    from app.models import LLMRequestLog

    now = utcnow()
    with SessionLocal() as db:
        jobs = [add_job(db, user_id) for _ in range(4)]
        db.add_all([
            TailorTask(user_id=user_id, job_id=jobs[0].id, status="succeeded", stage="reviewing", attempts=1,
                       result={"reviewer_score": 80, "auto_approved": True}, created_at=now - timedelta(hours=2),
                       started_at=now - timedelta(hours=2), finished_at=now - timedelta(hours=1)),
            TailorTask(user_id=user_id, job_id=jobs[1].id, status="succeeded", stage="reviewing", attempts=1,
                       result={"reviewer_score": 60}, created_at=now - timedelta(hours=2),
                       started_at=now - timedelta(hours=2), finished_at=now - timedelta(minutes=30)),
            TailorTask(user_id=user_id, job_id=jobs[2].id, status="failed", stage="tailoring", attempts=2,
                       error="Model timed out", created_at=now - timedelta(hours=2), finished_at=now - timedelta(hours=1)),
            JobRankingTask(user_id=user_id, job_id=jobs[3].id, status="FAILED", attempt_count=3, last_error="boom",
                           created_at=now - timedelta(hours=3), completed_at=now - timedelta(hours=2)),
            LLMRequestLog(request_id=str(uuid.uuid4()), user_id=user_id, provider="litellm", stage="REVIEWER",
                          model="m-review", status="SUCCEEDED", input_tokens=100, output_tokens=20, latency_ms=900,
                          created_at=now - timedelta(hours=1)),
        ])
        db.commit()
    client = signed_in(user_id)

    def perf(kind):
        out = client.get(f"/api/workers/{kind}/performance", params={"range": "7d"}).json()
        return {k: v for k, v in out.items() if k not in ("start", "end")}

    before = {kind: perf(kind) for kind in ("tailoring", "reviewing", "ranking", "insights")}
    tailoring = before["tailoring"]
    assert (tailoring["tasks_created"], tailoring["completed"], tailoring["failed"]) == (3, 2, 1)
    assert tailoring["review"]["median_score"] == 70 and tailoring["review"]["auto_approved"] == 1
    assert tailoring["top_errors"] == [{"error": "Model timed out", "tasks": 1}]
    reviewing = before["reviewing"]
    assert reviewing["completed"] == 2 and reviewing["failed"] == 0  # the failed run never reached review
    assert reviewing["llm"]["requests"] == 1 and reviewing["llm"]["models"] == {"m-review": 1}
    assert before["ranking"]["failed"] == 1 and before["ranking"]["ranking"]["rankings"] == 0
    assert before["insights"]["tasks_created"] == 0 and before["insights"]["llm"]["stages"] == [
        "COMPANY_INSIGHTS", "INTERVIEW_INSIGHTS"]
    day = client.get("/api/ranking/tracker").json()["day"]
    assert day["failed"] == 1

    for kind in ("tailoring", "reviewing", "ranking", "insights"):
        client.post(f"/api/workers/{kind}/clear").raise_for_status()
    assert {kind: perf(kind) for kind in before} == before  # clearing the list keeps every figure
    assert client.get("/api/ranking/tracker").json()["day"] == day  # and the tracker's last-day tiles
    client.post("/api/tailor-tasks/clear").raise_for_status()  # the old Tailor clear hides too, never deletes
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(TailorTask).where(TailorTask.user_id == user_id)) == 3
    assert client.get("/api/workers/nope/performance").status_code == 404


# ───────────── nudge forward ─────────────

def _next(client, kind):
    return next(w for w in client.get("/api/workers").json()["workers"] if w["kind"] == kind)["next_stage"]


def test_nudge_moves_a_recovered_description_into_parsing_once(user_id, signed_in, monkeypatch):
    monkeypatch.setattr(get_settings(), "jd_parse_enabled", True)
    with SessionLocal() as db:
        job = add_job(db, user_id)
        stopped = add_job(db, user_id)
        done = JobDescriptionFetchTask(user_id=user_id, job_id=job.id, status="COMPLETED", result_code="RECOVERED")
        done_stopped = JobDescriptionFetchTask(user_id=user_id, job_id=stopped.id, status="COMPLETED", result_code="RECOVERED")
        db.add_all([done, done_stopped])
        parse_task(db, user_id, stopped, status="FAILED", last_error="Stopped by you")
        db.commit()
        job_id = job.id
    client = signed_in(user_id)
    assert _next(client, "recovery") == {"to": "Parsing", "waiting": 1, "job_ids": [job_id]}
    out = client.post("/api/workers/recovery/nudge").json()
    assert (out["to"], out["moved"], out["skipped"]) == ("Parsing", [job_id], [])
    with SessionLocal() as db:
        task = db.scalar(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job_id))
        assert task.status == "PENDING" and task.run_now_at is None
    again = client.post("/api/workers/recovery/nudge").json()
    assert again["moved"] == [] and again["message"]  # idempotent: nothing left to move


def test_nudge_moves_a_parsed_job_into_ranking_and_releases_a_parked_one(user_id, signed_in):
    with SessionLocal() as db:
        fresh, stopped = (add_job(db, user_id) for _ in range(2))
        parked = add_job(db, user_id, state=ranker.S.RANK_PENDING)
        for job in (fresh, parked, stopped):
            parse_task(db, user_id, job, status="COMPLETED")
        db.add(JobRankingTask(user_id=user_id, job_id=parked.id, status="PENDING", attempt_count=0,
                              next_attempt_at=utcnow() + timedelta(minutes=15),
                              last_error="The job description is still being parsed"))
        db.add(JobRankingTask(user_id=user_id, job_id=stopped.id, status="SKIPPED", attempt_count=0,
                              last_error="Stopped by you"))
        db.commit()
        fresh_id, parked_id, stopped_id = fresh.id, parked.id, stopped.id
    out = signed_in(user_id).post("/api/workers/parsing/nudge").json()
    assert out["to"] == "Ranking" and set(out["moved"]) == {fresh_id, parked_id}
    with SessionLocal() as db:
        assert db.scalar(select(JobRankingTask).where(JobRankingTask.job_id == fresh_id)).status == "PENDING"
        task = db.scalar(select(JobRankingTask).where(JobRankingTask.job_id == parked_id))
        assert task.next_attempt_at is None and task.run_now_at is None
        assert db.scalars(select(JobRankingTask).where(JobRankingTask.job_id == stopped_id)).all()[0].last_error == "Stopped by you"
        assert len(db.scalars(select(JobRankingTask).where(JobRankingTask.job_id == stopped_id)).all()) == 1


def test_nudge_queues_qualified_jobs_for_tailoring_only_when_automatic_tailoring_is_on(user_id, signed_in):
    from app.models import AppSetting

    with SessionLocal() as db:
        job = add_job(db, user_id, status="shortlisted", fit_score=82, state=ranker.S.QUALIFIED)
        db.commit()
        job_id = job.id
    client = signed_in(user_id)
    assert _next(client, "ranking") == {"to": "Tailoring", "waiting": 0, "job_ids": []}  # auto-tailor is off
    with SessionLocal() as db:
        row = db.scalar(select(AppSetting).where(AppSetting.user_id == user_id)) or AppSetting(user_id=user_id)
        row.preferences = {**(row.preferences or {}), "auto_tailor": {"enabled": True, "enabled_at": "2020-01-01T00:00:00"}}
        db.add(row)
        db.commit()
    assert _next(client, "ranking")["waiting"] == 1
    out = client.post("/api/workers/ranking/nudge").json()
    assert out["moved"] == [job_id]
    with SessionLocal() as db:
        task = db.scalar(select(TailorTask).where(TailorTask.job_id == job_id))
        assert task.status == "queued" and task.run_now_at is None
    assert client.post("/api/workers/ranking/nudge").json()["moved"] == []


def test_nudge_approves_a_reviewed_packet_only_where_the_accounts_policy_would(user_id, signed_in):
    from app.models import AppSetting

    with SessionLocal() as db:
        high, low = add_job(db, user_id, status="tailored"), add_job(db, user_id, status="tailored")
        db.add_all([ResumeVariant(user_id=user_id, job_id=high.id, version=1, reviewer_score=91, markdown="x"),
                    ResumeVariant(user_id=user_id, job_id=low.id, version=1, reviewer_score=40, markdown="x")])
        row = db.scalar(select(AppSetting).where(AppSetting.user_id == user_id)) or AppSetting(user_id=user_id)
        row.application_mode, row.min_review_score = "manual_approval", 70
        db.add(row)
        db.commit()
        high_id, low_id = high.id, low.id
    client = signed_in(user_id)
    assert client.post("/api/workers/tailoring/nudge").json()["moved"] == []  # manual approval: a person decides
    with SessionLocal() as db:
        db.scalar(select(AppSetting).where(AppSetting.user_id == user_id)).application_mode = "automatic"
        db.commit()
    assert _next(client, "reviewing") == {"to": "Approval", "waiting": 1, "job_ids": [high_id]}
    assert client.post("/api/workers/reviewing/nudge").json()["moved"] == [high_id]
    with SessionLocal() as db:
        assert db.get(Job, high_id).status == "approved" and db.get(Job, low_id).status == "tailored"
        assert db.get(Job, high_id).application.status == "approved"


def test_nudge_sends_a_saved_but_unreviewed_packet_to_review(user_id, signed_in):
    with SessionLocal() as db:
        unreviewed, reviewed, busy = (add_job(db, user_id, status="tailored") for _ in range(3))
        db.add_all([ResumeVariant(user_id=user_id, job_id=unreviewed.id, version=1, reviewer_score=80, markdown="x"),
                    ResumeVariant(user_id=user_id, job_id=unreviewed.id, version=2, markdown="x"),
                    ResumeVariant(user_id=user_id, job_id=reviewed.id, version=1, reviewer_score=80, markdown="x"),
                    ResumeVariant(user_id=user_id, job_id=busy.id, version=1, markdown="x"),
                    TailorTask(user_id=user_id, job_id=busy.id, mode="tailor", status="queued", stage="queued")])
        db.commit()
        unreviewed_id, busy_id = unreviewed.id, busy.id
    client = signed_in(user_id)
    assert _next(client, "tailoring") == {"to": "Reviewing", "waiting": 1, "job_ids": [unreviewed_id]}
    out = client.post("/api/workers/tailoring/nudge").json()
    assert (out["to"], out["moved"], out["skipped"]) == ("Reviewing", [unreviewed_id], [])
    with SessionLocal() as db:
        task = db.scalar(select(TailorTask).where(TailorTask.job_id == unreviewed_id))
        assert (task.mode, task.status, task.run_now_at) == ("review", "queued", None)
        assert db.scalar(select(func.count()).select_from(TailorTask).where(TailorTask.job_id == busy_id)) == 1
    assert client.post("/api/workers/tailoring/nudge").json()["moved"] == []  # already queued
    assert unreviewed_id in [r["job_id"] for r in client.get("/api/workers/reviewing").json()["rows"]]


def test_nudge_can_be_limited_to_chosen_jobs_and_says_why_one_was_not_moved(user_id, signed_in, monkeypatch):
    monkeypatch.setattr(get_settings(), "jd_parse_enabled", True)
    with SessionLocal() as db:
        first, second, parsed = add_job(db, user_id), add_job(db, user_id), add_job(db, user_id)
        for job in (first, second):
            db.add(JobDescriptionFetchTask(user_id=user_id, job_id=job.id, status="COMPLETED", result_code="RECOVERED"))
        parse_task(db, user_id, parsed, status="COMPLETED")
        db.commit()
        first_id, second_id, parsed_id = first.id, second.id, parsed.id
    client = signed_in(user_id)
    assert set(_next(client, "recovery")["job_ids"]) == {first_id, second_id}
    out = client.post("/api/workers/recovery/nudge", json={"job_ids": [first_id, parsed_id]}).json()
    assert out["moved"] == [first_id]
    assert [s["job_id"] for s in out["skipped"]] == [parsed_id] and "Not waiting for Parsing" in out["skipped"][0]["reason"]
    with SessionLocal() as db:  # the second job was not asked for, so it stays where it was
        assert db.scalars(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == second_id)).all() == []


def test_nudge_never_touches_another_account_and_insights_has_no_next_stage(user_id, signed_in, monkeypatch):
    monkeypatch.setattr(get_settings(), "jd_parse_enabled", True)
    other = make_user(SessionLocal)
    with SessionLocal() as db:
        theirs = add_job(db, other)
        db.add(JobDescriptionFetchTask(user_id=other, job_id=theirs.id, status="COMPLETED", result_code="RECOVERED"))
        db.commit()
        theirs_id = theirs.id
    client = signed_in(user_id)
    assert client.post("/api/workers/recovery/nudge").json()["moved"] == []
    with SessionLocal() as db:
        assert db.scalars(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == theirs_id)).all() == []
    assert _next(client, "insights") is None
    assert client.post("/api/workers/insights/nudge").json()["message"] == "This worker has no next stage"
    assert client.post("/api/workers/nonsense/nudge").status_code == 404


def test_run_queued_now_releases_every_waiting_task_without_skipping_cooldowns(user_id, signed_in):
    other = make_user(SessionLocal)
    with SessionLocal() as db:
        queued, waiting, stopped, theirs = (add_job(db, uid) for uid in (user_id, user_id, user_id, other))
        later = utcnow() + timedelta(minutes=15)
        for job, uid, extra in ((queued, user_id, {}), (waiting, user_id, {"next_attempt_at": later, "attempt_count": 1}),
                                (stopped, user_id, {"last_error": "Stopped by you"}), (theirs, other, {"next_attempt_at": later})):
            db.add(JobRankingTask(user_id=uid, job_id=job.id, status="PENDING", **{"attempt_count": 0, **extra}))
        db.commit()
        ids = {"queued": queued.id, "waiting": waiting.id, "stopped": stopped.id, "theirs": theirs.id}
    out = signed_in(user_id).post("/api/workers/ranking/run-queued").json()
    assert out["released"] == 2
    with SessionLocal() as db:
        task = {name: db.scalar(select(JobRankingTask).where(JobRankingTask.job_id == job_id)) for name, job_id in ids.items()}
        assert task["queued"].force and task["waiting"].force and task["waiting"].next_attempt_at is None
        assert all(t.run_now_at is None for t in task.values())  # a provider cooldown still applies
        assert not task["stopped"].force and not task["theirs"].force and task["theirs"].next_attempt_at is not None
    assert signed_in(user_id).post("/api/workers/tailoring/run-queued").status_code == 409


def test_approval_nudge_leaves_held_and_stopped_packets_alone(user_id, signed_in):
    from app.models import AppSetting

    with SessionLocal() as db:
        passed, held, stopped = (add_job(db, user_id, status=st) for st in ("tailored", "pending_approval", "tailored"))
        for job in (passed, held, stopped):
            db.add(ResumeVariant(user_id=user_id, job_id=job.id, version=1, reviewer_score=95, markdown="x"))
        db.add(TailorTask(user_id=user_id, job_id=stopped.id, status="cancelled", stage="done", attempts=1,
                          error="Stopped by you"))
        row = db.scalar(select(AppSetting).where(AppSetting.user_id == user_id)) or AppSetting(user_id=user_id)
        row.application_mode, row.min_review_score = "automatic", 70
        db.add(row)
        db.commit()
        passed_id, held_id, stopped_id = passed.id, held.id, stopped.id
    out = signed_in(user_id).post("/api/workers/reviewing/nudge").json()
    assert out["moved"] == [passed_id]  # a reviewer's hold for a person, and a stopped run, stay as they are
    with SessionLocal() as db:
        assert db.get(Job, held_id).status == "pending_approval" and db.get(Job, stopped_id).status == "tailored"


# ───────────── the model each task uses ─────────────

def llm_row(db, uid, job, *, at, requested="big-model", answered=None, worker_type="JD_PARSER", **fields):
    from app.models import LLMRequestLog

    db.add(LLMRequestLog(request_id=str(uuid.uuid4()), user_id=uid, workflow_type="job", workflow_entity_id=job.id,
                         worker_type=worker_type, stage="OTHER", provider="litellm", requested_model=requested,
                         model=answered or requested, status="SUCCEEDED", created_at=at, **fields))


def test_tasks_show_their_model_route_and_what_changed(user_id, signed_in, monkeypatch):
    from app.services.llm import LLMClient

    monkeypatch.setattr(LLMClient, "route_for", lambda self, worker: {
        "route": "OPENROUTER", "model": "openrouter/free", "primary": "openrouter/free", "mode": None,
        "configured": None, "failed_over_from": None, "backups": []})
    with SessionLocal() as db:
        queued, failed_over, escalated, old = (add_job(db, user_id) for _ in range(4))
        started = utcnow() - timedelta(minutes=1)
        parse_task(db, user_id, queued)
        for job in (failed_over, escalated, old):
            parse_task(db, user_id, job, status="PROCESSING", started_at=started, created_at=started, lock_token="t",
                       locked_by="w", lock_expires_at=utcnow() + timedelta(minutes=5), attempt_count=1)
        llm_row(db, user_id, failed_over, at=utcnow(), requested="backup-model", routing_cause="FAILOVER")
        llm_row(db, user_id, escalated, at=utcnow(), requested="small", answered="large", escalated=True,
                escalation_reason="LOW_CONFIDENCE")
        llm_row(db, user_id, old, at=started - timedelta(hours=1), requested="from-an-earlier-task")
        db.commit()
        queued, failed_over, escalated, old = (type("J", (), {"id": j.id}) for j in (queued, failed_over, escalated, old))
    rows = {r["job_id"]: r for r in signed_in(user_id).get("/api/workers/parsing").json()["rows"]}

    nxt = rows[queued.id]["model"]
    assert nxt == {"model": "openrouter/free", "route": "OPENROUTER", "route_label": "OpenRouter", "via": None,
                   "source": "next", "backups": []}
    assert rows[queued.id]["model_change"] is None
    used = rows[failed_over.id]
    assert used["model"]["source"] == "used" and used["model"]["model"] == "backup-model"
    assert used["model"]["route"] == "LITELLM" and used["model"]["route_label"] == "LiteLLM"
    assert used["model_change"]["kind"] == "FAILOVER" and used["model_change"]["to"] == "backup-model"
    change = rows[escalated.id]["model_change"]
    assert change["kind"] == "ESCALATED" and change["to"] == "large" and change["reason"] == "low confidence"
    assert rows[old.id]["model"]["source"] == "next"  # a request from before this task started is not its own


def test_qualification_tasks_show_the_model_of_the_step_they_are_at(user_id, signed_in, monkeypatch):
    """The graph never calls a model, so a run shows the worker it hands its current step to, and that worker's change."""
    from app.models import JobQualificationTask
    from app.services.llm import LLMClient

    def route_for(self, worker):
        rest = {"failed_over_from": "ranker-main"} if worker == "RANKER" else {"failed_over_from": None}
        model = {"JD_PARSER": "parser-model", "RANKER": "ranker-backup", "JD_RECOVERY": "recovery-model"}[worker]
        return {"route": "LITELLM", "model": model, "primary": model, "mode": None, "configured": None,
                "backups": [], **rest}

    monkeypatch.setattr(LLMClient, "route_for", route_for)

    def run(db, job, **fields):
        db.add(JobQualificationTask(**{"user_id": user_id, "job_id": job.id, "mode": "ON", "status": "PENDING",
                                       "created_at": started, **fields}))

    started = utcnow() - timedelta(minutes=5)
    with SessionLocal() as db:
        fresh, parsed, recovering, parsing, ranking, done, hidden = (add_job(db, user_id) for _ in range(7))
        parsed.structured_description_status = job_description.PARSED
        parsed.structured_description_hash = job_description.description_hash(parsed.description)
        run(db, fresh)  # never started: the parser is next
        run(db, parsed, step="CHECK_JD")  # nothing left to parse: the ranker is next
        run(db, recovering, step="RECOVER_JD_IF_NEEDED", waiting_for="JD_RECOVERY")
        run(db, parsing, step="PARSE_JD", waiting_for="JD_PARSE")
        run(db, ranking, step="RANK", waiting_for="RANKING")
        finished = utcnow()
        run(db, done, status="COMPLETED", step="QUALIFY_ARCHIVE", outcome="QUALIFIED", completed_at=finished)
        run(db, hidden, status="COMPLETED", step="DOMAIN_AND_SEARCH_SCOPE_ADMISSION", outcome="HIDDEN",
            completed_at=finished)
        # the parser's own request, failed over, while the run waits for it; an earlier step's says nothing about ranking
        llm_row(db, user_id, parsing, at=utcnow(), requested="parser-backup", routing_cause="FAILOVER",
                failed_over_from="parser-model")
        llm_row(db, user_id, ranking, at=utcnow() - timedelta(minutes=1), requested="parser-model")
        llm_row(db, user_id, done, at=utcnow() - timedelta(minutes=2), requested="parser-model")
        llm_row(db, user_id, done, at=utcnow() - timedelta(minutes=1), requested="small", answered="large",
                escalated=True, escalation_reason="LOW_CONFIDENCE", worker_type="RANKER")
        db.commit()
        ids = {name: job.id for name, job in (("fresh", fresh), ("parsed", parsed), ("recovering", recovering),
                                              ("parsing", parsing), ("ranking", ranking), ("done", done),
                                              ("hidden", hidden))}
    found = signed_in(user_id).get("/api/workers/qualification").json()["rows"]
    rows = {r["job_id"]: r for r in found}
    model = {name: rows[job_id]["model"] for name, job_id in ids.items()}
    change = {name: rows[job_id]["model_change"] for name, job_id in ids.items()}

    assert (model["fresh"]["model"], model["fresh"]["source"], model["fresh"]["worker"]) == \
        ("parser-model", "next", "JD Parser")
    assert change["fresh"] is None
    assert (model["parsed"]["model"], model["parsed"]["worker"]) == ("ranker-backup", "Ranker")
    assert (model["recovering"]["model"], model["recovering"]["worker"]) == ("recovery-model", "JD Recovery")
    # waiting for the parser's worker, the row still reports what that worker's request did
    assert (model["parsing"]["model"], model["parsing"]["source"]) == ("parser-backup", "used")
    assert change["parsing"]["kind"] == "FAILOVER" and change["parsing"]["from"] == "parser-model"
    # at ranking, the parser's request is not this step's: the Ranker's plan and its resting model are
    assert (model["ranking"]["model"], model["ranking"]["source"]) == ("ranker-backup", "next")
    assert change["ranking"]["kind"] == "RESTING" and change["ranking"]["from"] == "ranker-main"
    # finished: the run's own last request across its steps
    assert (model["done"]["model"], model["done"]["source"], model["done"]["worker"]) == ("large", "used", "Ranker")
    assert change["done"]["kind"] == "ESCALATED"
    assert model["hidden"] == {"model": None, "source": "none"} and change["hidden"] is None


def test_preparation_tasks_follow_the_tailor_task_and_stop_at_a_person(user_id, signed_in, monkeypatch):
    """One tailor task tailors and then reviews, so a run waiting on it follows its stage; at approval no model is next."""
    from app.models import AppSetting, JobPreparationTask
    from app.services.llm import LLMClient

    monkeypatch.setattr(LLMClient, "route_for", lambda self, worker: {
        "route": "LITELLM", "model": {"TAILOR": "tailor-model", "REVIEWER": "review-model"}[worker],
        "primary": "x", "mode": None, "configured": None, "failed_over_from": None, "backups": []})

    def run(db, job, **fields):
        db.add(JobPreparationTask(**{"user_id": user_id, "job_id": job.id, "mode": "ON", "status": "PENDING",
                                     "created_at": started, **fields}))

    started = utcnow() - timedelta(minutes=5)
    with SessionLocal() as db:
        fresh, tailoring, reviewing, approval, unused, done = (add_job(db, user_id) for _ in range(6))
        run(db, fresh)
        run(db, tailoring, step="TAILOR", waiting_for="TAILORING")
        db.add(TailorTask(user_id=user_id, job_id=tailoring.id, status="running", stage="tailoring"))
        run(db, reviewing, step="TAILOR", waiting_for="TAILORING")
        db.add(TailorTask(user_id=user_id, job_id=reviewing.id, status="running", stage="reviewing"))
        run(db, approval, step="AWAIT_APPROVAL", waiting_for="APPROVAL")
        run(db, unused, step="AWAIT_APPROVAL", waiting_for="APPROVAL")
        run(db, done, status="COMPLETED", step="AWAIT_APPROVAL", outcome="APPROVED", completed_at=utcnow())
        llm_row(db, user_id, tailoring, at=utcnow(), requested="tailor-backup", routing_cause="FAILOVER",
                failed_over_from="tailor-model", worker_type="TAILOR")
        llm_row(db, user_id, approval, at=utcnow() - timedelta(minutes=2), requested="tailor-model",
                worker_type="TAILOR")
        llm_row(db, user_id, approval, at=utcnow() - timedelta(minutes=1), requested="old-review",
                worker_type="REVIEWER")
        llm_row(db, user_id, done, at=utcnow() - timedelta(minutes=1), requested="review-model",
                worker_type="REVIEWER")
        db.commit()
        ids = {"fresh": fresh.id, "tailoring": tailoring.id, "reviewing": reviewing.id, "approval": approval.id,
               "unused": unused.id, "done": done.id}
    client = signed_in(user_id)

    def read():
        rows = {r["job_id"]: r for r in client.get("/api/workers/preparation").json()["rows"]}
        return {k: rows[i]["model"] for k, i in ids.items()}, {k: rows[i]["model_change"] for k, i in ids.items()}

    model, change = read()
    assert (model["fresh"]["model"], model["fresh"]["source"], model["fresh"]["worker"]) == \
        ("tailor-model", "next", "Tailor")
    assert (model["tailoring"]["model"], model["tailoring"]["source"]) == ("tailor-backup", "used")
    assert change["tailoring"]["kind"] == "FAILOVER" and change["tailoring"]["from"] == "tailor-model"
    # the job's tailor task has moved on to review, whatever step the graph last recorded
    assert (model["reviewing"]["model"], model["reviewing"]["worker"]) == ("review-model", "Reviewer")
    # waiting for a person: what it last used, never a next model, and today's configuration is no change
    assert (model["approval"]["model"], model["approval"]["source"]) == ("old-review", "used")
    assert change["approval"] is None
    assert model["unused"] == {"model": None, "source": "none"}
    assert (model["done"]["model"], model["done"]["worker"]) == ("review-model", "Reviewer")

    # Claude Desktop tailors and reviews for this account: nothing goes through the gateway next
    with SessionLocal() as db:
        setting = db.scalar(select(AppSetting).where(AppSetting.user_id == user_id))
        if setting is None:
            setting = AppSetting(user_id=user_id)
            db.add(setting)
        setting.preferences = {**(setting.preferences or {}), "execution_mode": "claude_desktop"}
        db.commit()
    model, _ = read()
    assert model["fresh"] == {"model": None, "source": "none"}
    assert model["tailoring"]["model"] == "tailor-backup"  # a request it really made is still a fact


def test_a_waiting_task_says_when_a_backup_will_answer(monkeypatch):
    plan = {"route": "LITELLM", "model": "backup", "primary": "main", "failed_over_from": "main", "mode": "TIER",
            "configured": "FAST", "backups": ["backup"]}
    assert workers._change(plan, None) == {"kind": "RESTING", "from": "main", "to": "backup",
                                           "reason": "its model is resting, so a backup answers"}
    assert workers._via(plan) == "tier FAST" and workers._via({"mode": "AUTO_ROUTER"}) == "auto router"

    class Last:
        escalated, routing_cause, fallback_used, requested_model, model = False, None, False, "old-model", "old-model"
    changed = workers._change({**plan, "failed_over_from": None, "model": "new-model"}, Last())
    assert changed["kind"] == "CONFIG" and changed["from"] == "old-model" and changed["to"] == "new-model"
    Last.fallback_used, Last.model = True, "fallback-model"
    assert workers._change(plan, Last())["kind"] == "FALLBACK"


def test_ranking_and_tailoring_tasks_show_their_model_too(user_id, signed_in, monkeypatch):
    from app.services.llm import LLMClient

    seen = []

    def plan(self, worker):
        seen.append(worker)
        return {"route": "LITELLM", "model": f"{worker.lower()}-model", "primary": f"{worker.lower()}-model",
                "mode": "TIER", "configured": "FAST", "failed_over_from": None, "backups": []}
    monkeypatch.setattr(LLMClient, "route_for", plan)
    with SessionLocal() as db:
        ranked, tailored, reviewed = (add_job(db, user_id, state="RANK_PENDING") for _ in range(3))
        db.add(JobRankingTask(user_id=user_id, job_id=ranked.id, status="PENDING"))
        db.add(TailorTask(user_id=user_id, job_id=tailored.id, status="queued", stage="queued", attempts=0))
        db.add(TailorTask(user_id=user_id, job_id=reviewed.id, status="running", stage="reviewing", attempts=1,
                          started_at=utcnow()))
        db.commit()
        ranked_id, tailored_id, reviewed_id = ranked.id, tailored.id, reviewed.id
    client = signed_in(user_id)
    tracker = client.get("/api/ranking/tracker").json()
    rank = next(t for t in tracker["active"] if t["job_id"] == ranked_id)
    assert rank["model"]["model"] == "ranker-model" and rank["model"]["via"] == "tier FAST"
    tasks = {t["job_id"]: t for t in client.get("/api/tailor-tasks").json()}
    assert tasks[tailored_id]["model"]["model"] == "tailor-model" and tasks[tailored_id]["model"]["source"] == "next"
    assert tasks[reviewed_id]["model"]["model"] == "reviewer-model"  # at its review step: the reviewer's model
    assert {"RANKER", "TAILOR", "REVIEWER"} <= set(seen)


def test_a_ranking_task_reports_its_last_attempt(user_id, signed_in):
    """A ranking waiting for its account clears started_at; the tracker still says when it last ran."""
    at = utcnow() - timedelta(minutes=7)
    with SessionLocal() as db:
        job = add_job(db, user_id, state="RANK_PENDING")
        db.add(JobRankingTask(user_id=user_id, job_id=job.id, status="PENDING", attempt_count=1, last_attempt_at=at,
                              started_at=None))
        db.commit()
        job_id = job.id
    tracker = signed_in(user_id).get("/api/ranking/tracker").json()
    task = next(t for t in tracker["active"] if t["job_id"] == job_id)
    assert task["started_at"] is None and task["last_attempt_at"] == at.isoformat()


def test_finished_tasks_keep_the_model_their_own_last_request_used(user_id, signed_in, monkeypatch):
    from app.services.llm import LLMClient

    monkeypatch.setattr(LLMClient, "route_for", lambda self, worker: {
        "route": "LITELLM", "model": "today-model", "primary": "today-model", "mode": "TIER", "configured": "FAST",
        "failed_over_from": "rested", "backups": []})
    started = utcnow() - timedelta(hours=1)
    done_at = utcnow() - timedelta(minutes=30)
    with SessionLocal() as db:
        done, escalated, silent = (add_job(db, user_id) for _ in range(3))
        for job in (done, escalated, silent):
            parse_task(db, user_id, job, status="COMPLETED", created_at=started, started_at=started,
                       completed_at=done_at, attempt_count=1)
        llm_row(db, user_id, done, at=started + timedelta(minutes=5), requested="old-model")
        llm_row(db, user_id, done, at=utcnow(), requested="a-newer-tasks-model")  # after it finished: not its own
        llm_row(db, user_id, escalated, at=started + timedelta(minutes=5), requested="small", answered="large",
                escalated=True, escalation_reason="SCHEMA_FAILURE")
        db.commit()
        ids = (done.id, escalated.id, silent.id)
    rows = {r["job_id"]: r for r in signed_in(user_id).get("/api/workers/parsing").json()["rows"]}
    first = rows[ids[0]]
    assert first["model"]["model"] == "old-model" and first["model"]["source"] == "used"
    assert first["model_change"] is None  # today's configuration and resting models say nothing about the past
    assert rows[ids[1]]["model_change"]["kind"] == "ESCALATED"
    assert rows[ids[2]]["model"] == {"model": None, "source": "none"} and rows[ids[2]]["model_change"] is None
