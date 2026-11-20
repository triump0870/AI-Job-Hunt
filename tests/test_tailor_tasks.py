import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, update

from app.db import SessionLocal
from app.main import app
from app.models import TailorTask
from app.services import tailor_tasks
from app.settings import get_settings
from tests.resume_support import use_resume_text

client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_llm_and_empty_queue(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    with SessionLocal() as db:
        db.execute(delete(TailorTask))
        db.commit()


def shortlisted_job(client=client):
    tag = uuid.uuid4().hex[:8]
    job = client.post("/api/jobs/from-url", json={
        "url": f"https://example.com/jobs/{tag}", "title": "Lead AI Engineer", "company": f"Acme {tag}",
        "location": "Remote", "description": "Python FastAPI AWS and team leadership"}).json()["job"]
    assert client.post(f"/api/jobs/{job['id']}/triage", json={"action": "shortlist"}).status_code == 200
    return job["id"]


def tasks_by_job():
    return {t["job_id"]: t for t in client.get("/api/tailor-tasks").json()}


def test_tasks_queue_run_in_the_background_and_report_progress(monkeypatch):
    use_resume_text(client, "Built Python FastAPI services at 10k rps", name="Test Candidate", headline="Engineer")
    first, second, broken, skip = (shortlisted_job() for _ in range(4))
    not_shortlisted = client.post("/api/jobs/from-url", json={
        "url": f"https://example.com/jobs/{uuid.uuid4().hex}", "title": "Engineer", "company": "Other",
        "description": "Python"}).json()["job"]["id"]

    out = client.post("/api/tailor-tasks", json={"ids": [first, second, broken, not_shortlisted, 999999]}).json()
    assert [t["job_id"] for t in out["queued"]] == [first, second, broken]
    assert {s["id"]: s["reason"] for s in out["skipped"]} == {not_shortlisted: "not in the Tailor queue", 999999: "not found"}
    assert client.post("/api/tailor-tasks", json={"ids": [first]}).json()["skipped"] == [{"id": first, "reason": "already queued"}]
    assert client.post("/api/tailor-tasks", json={"ids": [skip], "mode": "skip"}).json()["queued"][0]["mode"] == "skip"
    assert {t["status"] for t in tasks_by_job().values()} == {"queued"}

    stages = []
    real_tailor = tailor_tasks.tailor_job

    def flaky_tailor(db, job, progress, pages=None):
        if job.id == broken:
            raise RuntimeError("model timed out")

        def spy(stage):
            progress(stage)
            with SessionLocal() as s:
                stages.append(s.get(TailorTask, next(t["id"] for t in tasks_by_job().values() if t["job_id"] == job.id)).stage)
        return real_tailor(db, job, progress=spy, pages=pages)

    monkeypatch.setattr(tailor_tasks, "tailor_job", flaky_tailor)
    assert tailor_tasks.run_pending(SessionLocal) == 4
    assert "tailoring" in stages and "reviewing" in stages

    tasks = tasks_by_job()
    assert tasks[first]["status"] == "succeeded" and tasks[first]["progress"] == 100
    assert tasks[first]["result"]["reviewer_score"] == 86 and tasks[first]["result"]["untailored"] is False
    assert tasks[broken]["status"] == "failed" and tasks[broken]["error"] == "model timed out"
    assert tasks[skip]["result"]["untailored"] is True
    assert client.get(f"/api/tailor/{second}").json()["resume"]["version"] == 1

    # A job with a packet is only redone on request.
    assert client.post("/api/tailor-tasks", json={"ids": [first]}).json()["skipped"][0]["reason"] == "already has a packet"
    assert len(client.post("/api/tailor-tasks", json={"ids": [first], "regenerate": True}).json()["queued"]) == 1
    assert client.post("/api/tailor-tasks/cancel", json={}).json() == {"cancelled": 1}
    # Clearing hides finished tasks (Running workers → Tailoring); it never deletes them, so performance keeps them.
    assert client.post("/api/tailor-tasks/clear").json()["cleared"] == 5
    assert len(client.get("/api/tailor-tasks").json()) == 5
    assert next(w for w in client.get("/api/workers").json()["workers"] if w["kind"] == "tailoring")["cleared_at"]


def test_interrupted_task_is_requeued_then_failed():
    job_id = shortlisted_job()
    task_id = client.post("/api/tailor-tasks", json={"ids": [job_id]}).json()["queued"][0]["id"]
    for attempt in range(tailor_tasks.MAX_ATTEMPTS):
        assert tailor_tasks.claim_next(SessionLocal, "crashed-worker", 60) == task_id
        assert tasks_by_job()[job_id]["status"] == "running"
        with SessionLocal() as db:  # the worker died: its lease runs out
            db.execute(update(TailorTask).where(TailorTask.id == task_id)
                       .values(lock_expires_at=tailor_tasks.utcnow() - timedelta(seconds=1)))
            db.commit()
    assert tailor_tasks.claim_next(SessionLocal, "next-worker", 60) is None
    task = tasks_by_job()[job_id]
    assert task["status"] == "failed" and task["error"].startswith("Interrupted")


def signed_up():
    account = TestClient(app)
    password = "correct horse battery"
    assert account.post("/api/auth/signup", json={"email": f"{uuid.uuid4().hex[:8]}@example.com", "password": password,
                                                  "confirm_password": password}).status_code == 201
    return account


def test_tasks_are_private_to_their_account(anonymous):
    alice, bob = signed_up(), signed_up()
    job_id = shortlisted_job(alice)
    task_id = alice.post("/api/tailor-tasks", json={"ids": [job_id]}).json()["queued"][0]["id"]
    assert bob.get("/api/tailor-tasks").json() == []
    assert bob.post("/api/tailor-tasks", json={"ids": [job_id]}).json()["skipped"] == [{"id": job_id, "reason": "not found"}]
    assert bob.post("/api/tailor-tasks/cancel", json={"ids": [task_id]}).json() == {"cancelled": 0}
    assert [t["id"] for t in alice.get("/api/tailor-tasks").json()] == [task_id]
    assert TestClient(app).get("/api/tailor-tasks").status_code == 401


def test_queueing_a_task_wakes_the_tailor_worker_instead_of_waiting_for_the_poll():
    import asyncio
    import time

    claimed_at = []

    use_resume_text(client, "Built Python FastAPI services at 10k rps", name="Test Candidate", headline="Engineer")

    async def scenario():
        worker = tailor_tasks.TailorWorker(SessionLocal, concurrency=1, poll_seconds=30.0)  # a poll alone would take 30 s
        real = tailor_tasks.claim_next

        def spy(*args, **kwargs):
            task_id = real(*args, **kwargs)
            if task_id is not None:
                claimed_at.append(time.monotonic())
            return task_id

        tailor_tasks.claim_next = spy
        try:
            await worker.start()
            await asyncio.sleep(0.3)  # the first poll found nothing
            started = time.monotonic()
            await asyncio.to_thread(lambda: client.post("/api/tailor-tasks", json={"ids": [shortlisted_job()]}))
            for _ in range(50):
                if claimed_at:
                    break
                await asyncio.sleep(0.1)
            return started
        finally:
            tailor_tasks.claim_next = real
            await worker.stop()

    started = asyncio.run(scenario())
    assert claimed_at and claimed_at[0] - started < 5


def test_a_packet_whose_review_failed_is_reviewed_without_tailoring_again(monkeypatch):
    from app.services import workflow

    use_resume_text(client, "Built Python FastAPI services at 10k rps", name="Test Candidate", headline="Engineer")
    job_id, fresh = shortlisted_job(), shortlisted_job()
    assert client.post("/api/tailor-tasks", json={"ids": [job_id], "mode": "skip"}).json()["queued"]
    real_review = workflow.review_resume

    def down(db, resume):
        raise RuntimeError("reviewer timed out")

    monkeypatch.setattr(workflow, "review_resume", down)
    assert tailor_tasks.run_pending(SessionLocal) == 1
    task = tasks_by_job()[job_id]
    assert task["status"] == "failed" and task["error"] == "reviewer timed out"
    saved = client.get(f"/api/tailor/{job_id}").json()["resume"]
    assert saved["version"] == 1 and saved["reviewer_score"] is None  # the packet was saved before the review

    out = client.post("/api/tailor-tasks", json={"ids": [job_id, fresh], "mode": "review"}).json()
    assert [t["job_id"] for t in out["queued"]] == [job_id] and out["queued"][0]["mode"] == "review"
    assert out["skipped"] == [{"id": fresh, "reason": "no packet to review"}]
    monkeypatch.setattr(workflow, "review_resume", real_review)
    assert tailor_tasks.run_pending(SessionLocal) == 1
    task = next(t for t in client.get("/api/tailor-tasks").json() if t["id"] == out["queued"][0]["id"])
    assert task["status"] == "succeeded", task["error"]
    assert task["result"]["version"] == 1
    reviewed = client.get(f"/api/tailor/{job_id}").json()["resume"]
    assert reviewed["version"] == 1 and reviewed["reviewer_score"] is not None  # the same version, now reviewed

    again = client.post("/api/tailor-tasks", json={"ids": [job_id], "mode": "review"}).json()
    assert again["skipped"] == [{"id": job_id, "reason": "already reviewed"}]
    redo = client.post("/api/tailor-tasks", json={"ids": [job_id], "mode": "review", "regenerate": True}).json()
    assert [t["mode"] for t in redo["queued"]] == ["review"]  # Redo reviews a reviewed packet again


def tailored_job():
    job_id = shortlisted_job()
    client.post("/api/tailor-tasks", json={"ids": [job_id]})
    assert tailor_tasks.run_pending(SessionLocal) == 1
    return job_id


def test_an_approved_packet_is_never_reviewed_again():
    use_resume_text(client, "Built Python FastAPI services at 10k rps", name="Test Candidate", headline="Engineer")
    approved = tailored_job()
    assert client.post(f"/api/jobs/{approved}/approve", json={}).status_code == 200
    out = client.post("/api/tailor-tasks", json={"ids": [approved], "mode": "review", "regenerate": True}).json()
    assert out["skipped"] == [{"id": approved, "reason": "not in the Tailor queue"}]


def test_revise_makes_each_run_a_revision_pass_on_the_latest_review(monkeypatch):
    from app.models import ResumeVariant

    use_resume_text(client, "Built Python FastAPI services at 10k rps", name="Test Candidate", headline="Engineer")
    job_id = tailored_job()
    with SessionLocal() as db:
        latest = db.query(ResumeVariant).filter(ResumeVariant.job_id == job_id).one().id

    out = client.post("/api/tailor-tasks", json={"ids": [job_id], "regenerate": True, "revise": True}).json()
    task_id = out["queued"][0]["id"]
    with SessionLocal() as db:
        assert db.get(TailorTask, task_id).options == {"revise_from": latest}

    asked, given = [], []
    monkeypatch.setattr(tailor_tasks, "_revision_feedback",
                        lambda db, job, variant_id, requests=None: asked.append(variant_id) or {"fix": ["cite evidence"]})
    real_tailor = tailor_tasks.tailor_job

    def spy(db, job, progress, pages=None, feedback=None):
        given.append(feedback)
        return real_tailor(db, job, progress=progress, pages=pages, feedback=feedback)

    monkeypatch.setattr(tailor_tasks, "tailor_job", spy)
    assert tailor_tasks.run_pending(SessionLocal) == 1
    assert asked == [latest] and given == [{"fix": ["cite evidence"]}]
    with SessionLocal() as db:
        assert db.get(TailorTask, task_id).result["version"] == 2

    # Retry from Running workers keeps the revision pass.
    from app.services import workers
    with SessionLocal() as db:
        db.info["user_id"] = db.get(TailorTask, task_id).user_id
        task = db.get(TailorTask, task_id)
        workers._requeue_finished(db, workers.KINDS["tailoring"], task, task.user_id)
    with SessionLocal() as db:
        retried = db.query(TailorTask).filter(TailorTask.job_id == job_id, TailorTask.status == "queued").one()
        assert retried.options == {"revise_from": latest}


def test_sending_a_packet_back_queues_a_revision_with_the_persons_requests(monkeypatch):
    """Needs revision is never a dead end: it queues the revision pass at once, and what the person ticked reaches
    the Tailor's previous review as person_requests."""
    use_resume_text(client, "Built Python FastAPI services at 10k rps", name="Test Candidate", headline="Engineer")
    job_id = tailored_job()
    out = client.post(f"/api/jobs/{job_id}/revision",
                      json={"notes": "", "requests": ["Unsupported: the S3 claim", "  Shorter summary  ", ""]}).json()
    assert out["status"] == "pending_approval"
    [queued] = out["retailor"]["queued"]
    with SessionLocal() as db:
        options = db.get(TailorTask, queued["id"]).options
    assert options["person_requests"] == ["Unsupported: the S3 claim", "Shorter summary"] and options["revise_from"]

    given = []
    real_tailor = tailor_tasks.tailor_job
    monkeypatch.setattr(tailor_tasks, "tailor_job", lambda db, job, progress, pages=None, feedback=None:
                        given.append(feedback) or real_tailor(db, job, progress=progress, pages=pages,
                                                              feedback=feedback))
    assert tailor_tasks.run_pending(SessionLocal) == 1
    assert given[0]["previous_review"]["person_requests"] == ["Unsupported: the S3 claim", "Shorter summary"]

    again = client.post(f"/api/jobs/{job_id}/revision", json={"retailor": False}).json()
    assert again["retailor"] is None, "the person can send it back without queueing a pass"


def test_sending_back_in_bulk_starts_each_revision():
    use_resume_text(client, "Built Python FastAPI services at 10k rps", name="Test Candidate", headline="Engineer")
    job_id = tailored_job()
    out = client.post("/api/approvals/bulk", json={"action": "revise", "job_ids": [job_id]}).json()
    assert out["done"] == [job_id] and [t["job_id"] for t in out["retailor"]["queued"]] == [job_id]
    with SessionLocal() as db:
        assert "person_requests" not in (db.get(TailorTask, out["retailor"]["queued"][0]["id"]).options or {})
