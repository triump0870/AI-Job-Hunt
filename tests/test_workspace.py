import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import desc, select

from app.db import SessionLocal
from app.main import app
from app.models import Job, ResumeVariant, SearchRun
from app.settings import get_settings
from app.workspace import extract_job_posting, location_key
from tests.resume_support import use_resume_text

client = TestClient(app)


# A posting long enough to rank: app.services.ranker holds back listings with nothing to read.
POSTING = ("Lead Python platform teams building services on Kubernetes and AWS. You will own the roadmap for "
           "our data platform, mentor five engineers, and work with product to ship reliably. We look for expe"
           "rience with FastAPI, distributed systems and a track record of leading delivery.")

@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


def make_job(title="Lead AI Engineer", company=None, location="Remote"):
    tag = uuid.uuid4().hex[:8]
    response = client.post("/api/jobs/from-url", json={
        "url": f"https://example.com/jobs/{tag}", "title": title, "company": company or f"Acme {tag}",
        "location": location, "description": POSTING})
    assert response.status_code == 200, response.text
    return response.json()["job"]


def test_packet_flows_from_triage_to_tracker():
    use_resume_text(client, "Built Python FastAPI services handling 10k requests per second\nLed 5 engineers on AWS",
                    name="Test Candidate", headline="Engineer", email="t@example.com", phone="123",
                    target_roles=["Lead AI Engineer"])
    client.post("/api/evidence", json={"claim": "Built Python FastAPI services at 10k rps", "company": "Acme",
                                       "skills": ["Python", "FastAPI"]})
    job = make_job()

    queue = client.get("/api/jobs/search", params={"queue": True, "q": job["company"]}).json()
    assert [item["id"] for item in queue["items"]] == [job["id"]]
    assert client.post(f"/api/jobs/{job['id']}/triage", json={"action": "shortlist"}).json()["status"] == "shortlisted"
    assert client.get("/api/jobs/search", params={"queue": True, "q": job["company"]}).json()["total"] == 0
    assert any(item["id"] == job["id"] for item in client.get("/api/tailor/queue").json())
    assert client.get(f"/api/tailor/{job['id']}").json()["resume"] is None

    assert client.post(f"/api/jobs/{job['id']}/tailor").status_code == 200
    resume = client.get(f"/api/tailor/{job['id']}").json()["resume"]
    assert resume["reviewer_score"] == 86 and resume["edits"][0]["section"] == "SUMMARY"
    edited = client.patch(f"/api/resumes/{resume['id']}/edits/0",
                          json={"text": "Custom summary line", "status": "accepted"}).json()
    assert "Custom summary line" in edited["resume"]["markdown"]
    last = len(resume["edits"]) - 1
    bullet = resume["edits"][last]["after"]
    reverted = client.patch(f"/api/resumes/{resume['id']}/edits/{last}", json={"status": "reverted"}).json()
    assert f"- {bullet}" not in reverted["resume"]["markdown"]
    assert client.patch(f"/api/resumes/{resume['id']}/edits/99", json={"status": "accepted"}).status_code == 404

    waiting = client.get("/api/approvals").json()
    assert any(row["id"] == job["id"] and row["waiting"] for row in waiting)
    detail = client.get(f"/api/approvals/{job['id']}").json()
    assert len(detail["checklist"]) == 6
    assert detail["resume"]["review"] and {note["tone"] for note in detail["resume"]["review"]} <= {"good", "warn", "bad"}
    assert client.get(f"/api/jobs/{job['id']}/apply-kit").status_code == 400
    before = client.get("/api/workspace/summary").json()["approved"]
    assert client.post(f"/api/jobs/{job['id']}/approve").status_code == 200
    after = client.get("/api/workspace/summary").json()["approved"]  # what the browser compares to raise a notification
    assert after["count"] == before["count"] + 1 and after["latest_at"] and after["latest_at"] >= (before["latest_at"] or "")

    kit = client.get(f"/api/jobs/{job['id']}/apply-kit").json()
    assert [f["kind"] for f in kit["files"]] == ["PDF", "DOCX", "TXT"]
    for item in kit["files"]:
        assert client.get(item["href"]).status_code == 200

    assert client.post(f"/api/jobs/{job['id']}/applied", json={"recruiter_name": "Priya"}).status_code == 200
    tracker = client.get("/api/tracker").json()
    row = next(a for a in tracker["applications"] if a["job_id"] == job["id"])
    assert row["stage"] == "applied" and row["recruiter_name"] == "Priya"
    assert row["timeline"][0]["kind"] == "applied"

    moved = client.post(f"/api/applications/{row['id']}/stage", json={"stage": "screening"}).json()
    assert moved["stage"] == "screening" and moved["timeline"][-1]["title"] == "Moved to Screening"
    assert client.post(f"/api/applications/{row['id']}/stage", json={"stage": "nope"}).status_code == 422
    future = (datetime.now(UTC) + timedelta(days=3)).replace(tzinfo=None).isoformat()
    interview = client.post(f"/api/applications/{row['id']}/events",
                            json={"kind": "interview", "title": "Panel", "occurred_at": future}).json()
    assert interview["stage"] == "interviewing"
    assert client.post(f"/api/applications/{row['id']}/followup-draft").json()["draft"]
    assert client.put(f"/api/applications/{row['id']}/recruiter",
                      json={"recruiter_name": "Deepak", "recruiter_email": "d@example.com"}).json()["recruiter_name"] == "Deepak"

    overview = client.get("/api/workspace/overview").json()
    assert [f["label"] for f in overview["funnel"]][:2] == ["Discovered", "Shortlisted"]
    assert any(u["kind"] == "interview" for u in overview["upcoming"])
    assert {"by_location", "by_role", "fit_vs_outcome", "outcomes", "inbox", "pass_reasons"} <= overview.keys()
    today = client.get("/api/workspace/today").json()
    assert {"actions", "followups", "week", "funnel"} <= today.keys()
    summary = client.get("/api/workspace/summary").json()
    assert summary["counts"]["tracker"] >= 1


def test_skip_tailoring_uses_the_master_resume_unchanged():
    master = ("# Test Candidate\nEngineer\n## Experience\n### Lead | Acme | Remote | 2020 - 2024\n"
              "- Built Python FastAPI services handling 10k requests per second\n")
    use_resume_text(client, master, name="Test Candidate", headline="Engineer", email="t@example.com", phone="123")
    job = make_job()
    assert client.post(f"/api/jobs/{job['id']}/skip-tailoring").status_code == 409  # not shortlisted yet
    client.post(f"/api/jobs/{job['id']}/triage", json={"action": "shortlist"})

    out = client.post(f"/api/jobs/{job['id']}/skip-tailoring").json()
    assert out["untailored"] is True and out["reviewer_score"] == 86 and out["cover_letter"] == ""
    resume = client.get(f"/api/tailor/{job['id']}").json()["resume"]
    assert resume["untailored"] is True and resume["edits"] == []
    assert "### Lead | Acme | Remote | 2020 - 2024" in resume["markdown"]
    assert "- Built Python FastAPI services handling 10k requests per second" in resume["markdown"]
    assert client.patch(f"/api/resumes/{resume['id']}/edits/0", json={"status": "accepted"}).status_code == 404

    assert any(row["id"] == job["id"] and row["waiting"] for row in client.get("/api/approvals").json())
    assert client.post(f"/api/jobs/{job['id']}/approve").status_code == 200
    assert client.post(f"/api/jobs/{job['id']}/skip-tailoring").status_code == 409  # already approved
    assert client.post("/api/jobs/999999/skip-tailoring").status_code == 404


def test_bulk_tailor_processes_the_queue_in_batches(monkeypatch):
    import app.main as main

    use_resume_text(client, "Built Python FastAPI services at 10k rps", name="Test Candidate", headline="Engineer")
    first, second, broken, has_packet, not_shortlisted = [make_job()["id"] for _ in range(5)]
    for job_id in (first, second, broken, has_packet):
        client.post(f"/api/jobs/{job_id}/triage", json={"action": "shortlist"})
    assert client.post(f"/api/jobs/{has_packet}/tailor").status_code == 200

    real_tailor = main.tailor_job

    def flaky_tailor(db, job, pages=None):
        if job.id == broken:
            raise RuntimeError("model timed out")
        return real_tailor(db, job, pages=pages)

    monkeypatch.setattr(main, "tailor_job", flaky_tailor)
    ids = [first, second, broken, has_packet, not_shortlisted, 999999]
    out = client.post("/api/jobs/bulk-tailor", json={"ids": ids}).json()
    assert [d["id"] for d in out["done"]] == [first] and out["remaining"] == [second, broken]
    assert out["skipped"] == 2 and out["missing"] == 1 and out["done"][0]["reviewer_score"] == 86

    out = client.post("/api/jobs/bulk-tailor", json={"ids": out["remaining"], "batch_size": 5}).json()
    assert [d["id"] for d in out["done"]] == [second] and out["remaining"] == []
    assert out["errors"] == [{"id": broken, "error": "model timed out"}]
    assert client.get(f"/api/tailor/{first}").json()["resume"]["untailored"] is False
    assert client.get(f"/api/tailor/{broken}").json()["resume"] is None

    skipped = client.post("/api/jobs/bulk-tailor", json={"ids": [broken, has_packet], "mode": "skip",
                                                         "regenerate": True, "batch_size": 5}).json()
    assert sorted(d["id"] for d in skipped["done"]) == sorted([broken, has_packet])
    assert all(d["untailored"] for d in skipped["done"])
    assert client.get(f"/api/tailor/{has_packet}").json()["resume"]["version"] == 2

    assert client.post("/api/jobs/bulk-tailor", json={"ids": []}).status_code == 422
    assert client.post("/api/jobs/bulk-tailor", json={"ids": [first], "mode": "nope"}).status_code == 422


def test_search_filters_unranked_jobs():
    tag = uuid.uuid4().hex[:8]
    ranked = make_job(company=f"Rankco {tag} A")
    unranked = make_job(company=f"Rankco {tag} B")
    with SessionLocal() as db:  # what the asynchronous ranker leaves on a qualified job (tests/test_ranker.py)
        row = db.get(Job, ranked["id"])
        row.fit_score, row.status = 80.0, "shortlisted"
        db.commit()

    out = client.get("/api/jobs/search", params={"q": f"Rankco {tag}", "stage": "unranked"}).json()
    assert [item["id"] for item in out["items"]] == [unranked["id"]] and out["total"] == 1
    selected = client.get("/api/jobs/search", params={"q": f"Rankco {tag}", "stage": "unranked", "ids_only": True}).json()
    assert selected["ids"] == [unranked["id"]]
    assert [item["id"] for item in client.get("/api/jobs/search", params={"q": f"Rankco {tag}", "stage": "all"}).json()["items"]] \
        == [ranked["id"], unranked["id"]]
    shortlisted = client.get("/api/jobs/search", params={"q": f"Rankco {tag}", "stage": "shortlisted"}).json()
    assert [item["id"] for item in shortlisted["items"]] == [ranked["id"]]


def test_triage_actions_and_filters():
    job = make_job(company=f"Talent Staffing {uuid.uuid4().hex[:6]}")
    assert client.post(f"/api/jobs/{job['id']}/triage", json={"action": "explode"}).status_code == 422
    hidden = client.get("/api/jobs/search", params={"q": job["company"], "hide_agencies": True}).json()
    assert hidden["total"] == 0
    assert client.post("/api/jobs/bulk-triage", json={"ids": [job["id"]], "action": "maybe"}).json()["changed"] == 1
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "maybe"
    assert client.get("/api/jobs/999999").status_code == 404


def test_search_profile_save_run_and_detail(monkeypatch):
    bad = client.put("/api/search/profile", json={"roles": ["AI"], "sites": ["myspace"]})
    assert bad.status_code == 422
    saved = client.put("/api/search/profile", json={"roles": ["AI Engineer"], "locations": ["Remote", "Pune"],
                                                    "sites": ["linkedin"], "schedule": "daily"})
    assert saved.status_code == 200
    started = []

    def fake_start(_db, payload):
        started.append(payload["location"])
        return SimpleNamespace(id=len(started), status="READY", input_json={"location": payload["location"]})

    monkeypatch.setattr("app.workspace.start_apify_search", fake_start)
    response = client.post("/api/search/profile/run")
    assert response.status_code == 200 and started == ["Remote", "Pune"]

    from tests.conftest import ensure_test_user

    db = SessionLocal()
    db.info["user_id"] = ensure_test_user().id  # rows created here belong to the account the API acts as
    run = SearchRun(external_run_id=uuid.uuid4().hex, actor_id="actor",
                    input_json={"searchTerms": ["AI Engineer"], "location": "Remote", "sites": ["linkedin"]},
                    status="SUCCEEDED", item_count=5, created_jobs=1)
    db.add(run)
    db.commit()
    db.add(Job(source="apify:linkedin", source_ref=uuid.uuid4().hex, title="AI Engineer", company="Pulled Co",
               description=POSTING, search_run_id=run.id))
    db.commit()
    run_id = run.id
    db.close()
    detail = client.get(f"/api/search/apify/runs/{run_id}/detail").json()
    assert detail["counts"]["duplicates"] == 4 and detail["results"][0]["company"] == "Pulled Co"
    queued = client.post(f"/api/search/apify/runs/{run_id}/rank")
    assert queued.status_code == 202 and len(queued.json()["queued"]) == 1


def test_the_tracker_can_open_the_packet_that_was_submitted():
    """The tracker card names the submitted resume so the screen can open it; an applied packet is read-only."""
    job = make_job(company=f"Epsilon {uuid.uuid4().hex[:6]}")
    client.post(f"/api/jobs/{job['id']}/triage", json={"action": "shortlist"})
    assert client.post(f"/api/jobs/{job['id']}/tailor").status_code == 200
    with SessionLocal() as db:
        variant = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job["id"])
                            .order_by(desc(ResumeVariant.version)))
        variant.reviewer_score = 90
        db.commit()
        resume_id = variant.id
    client.post(f"/api/jobs/{job['id']}/approve", json={})
    client.post(f"/api/jobs/{job['id']}/applied", json={})

    card = next(a for a in client.get("/api/tracker").json()["applications"] if a["job_id"] == job["id"])
    assert card["resume_id"] == resume_id, "the tracker knows which packet was submitted"
    assert "resume v" in card["docs"]

    opened = client.get(f"/api/tailor/{job['id']}").json()
    assert opened["resume"]["id"] == resume_id and opened["job"]["status"] == "applied"


def test_tracker_stages_preferences_and_evidence_parsing():
    stages = client.put("/api/tracker/stages", json={"extra_stages": ["offer"]}).json()
    assert [s["id"] for s in stages["stages"]][-2:] == ["offer", "closed"]
    assert client.put("/api/tracker/stages", json={"extra_stages": ["party"]}).status_code == 422
    prefs = client.put("/api/preferences", json={"weekly_target": 12, "followup_days": 9}).json()
    assert prefs["weekly_target"] == 12 and prefs["followup_days"] == 9 and len(prefs["stage_models"]) == 4

    # Unique text: a persistent TEST_DATABASE_URL keeps claims from earlier runs.
    text = f"Cut cloud spend by 35% across 3 regions in 2024 ({uuid.uuid4().hex[:8]})"
    use_resume_text(client, f"{text}\nshort")
    parsed = client.post("/api/profile/resume/parse-evidence").json()
    assert parsed["added"] >= 1
    evidence = client.get("/api/evidence").json()
    claim = next(e for e in evidence if e["claim"] == text)
    assert claim["verified"] is False
    assert client.patch(f"/api/evidence/{claim['id']}", json={"verified": True}).json()["verified"] is True


def test_knowledge_edit_and_url_guards():
    created = client.post("/api/knowledge", json={"question": f"Notice period {uuid.uuid4().hex}", "answer": "60 days"}).json()
    updated = client.put(f"/api/knowledge/{created['id']}", json={"question": f"Notice period {uuid.uuid4().hex}?", "answer": "45 days",
                                                                   "verified": True})
    assert updated.status_code == 200 and updated.json()["verified"] is True
    blocked = client.post("/api/jobs/from-url", json={"url": "http://127.0.0.1:8000/admin"})
    assert blocked.status_code == 422


def test_board_import_errors_are_readable(monkeypatch):
    import httpx

    def missing_board(_db, token, **_filters):
        request = httpx.Request("GET", f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
        raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))

    monkeypatch.setattr("app.main.import_greenhouse", missing_board)
    response = client.post("/api/import/greenhouse/nope")
    assert response.status_code == 404
    assert response.json()["detail"] == "No public Greenhouse board named 'nope'"


def test_board_import_can_be_limited_to_teams_and_roles(monkeypatch):
    posting = lambda i, title, team: {"id": i, "title": title, "content": "<p>Build things</p>", "absolute_url": f"https://x.test/{i}",
                                      "location": {"name": "Remote"}, "departments": [{"name": team}]}
    base = int(uuid.uuid4().int % 10**9)  # the test database is shared, so every run posts new ids
    board = [posting(base + 1, "Backend Engineer", "Engineering"), posting(base + 2, "Data Engineer", "Data"),
             posting(base + 3, "Account Executive", "Sales"), posting(base + 4, "Staff Engineer", "Engineering")]
    monkeypatch.setattr("app.services.sources._fetch_greenhouse", lambda token: board)

    teams = client.get("/api/import/greenhouse/acme/teams").json()["teams"]
    assert teams == [{"name": "Engineering", "count": 2}, {"name": "Data", "count": 1}, {"name": "Sales", "count": 1}]

    only_team = client.post("/api/import/greenhouse/acme", json={"teams": ["engineering"]}).json()
    assert (only_team["seen"], only_team["total"]) == (2, 4)
    both = client.post("/api/import/greenhouse/acme", json={"teams": ["Data", "Sales"], "roles": ["engineer"]}).json()
    assert (both["seen"], both["created"]) == (1, 1)  # Data Engineer only: in a chosen team and a matching title
    everything = client.post("/api/import/greenhouse/acme").json()
    assert (everything["seen"], everything["total"]) == (4, 4)


def test_board_preview_lists_roles_and_import_can_take_only_the_chosen(monkeypatch):
    posting = lambda i, title, team: {"id": i, "title": title, "content": "<p>Build things</p>", "absolute_url": f"https://x.test/{i}",
                                      "location": {"name": "Remote"}, "departments": [{"name": team}]}
    base = int(uuid.uuid4().int % 10**9)
    board = [posting(base + 1, "Backend Engineer", "Engineering"), posting(base + 2, "Data Engineer", "Data"),
             posting(base + 3, "Account Executive", "Sales")]
    monkeypatch.setattr("app.services.sources._fetch_greenhouse", lambda token: board)

    preview = client.post("/api/import/greenhouse/acme/preview", json={"roles": ["engineer"]}).json()
    assert preview["total"] == 3 and [j["title"] for j in preview["jobs"]] == ["Backend Engineer", "Data Engineer"]
    assert preview["jobs"][0]["team"] == "Engineering" and not any(j["in_pipeline"] for j in preview["jobs"])

    chosen = str(base + 2)
    result = client.post("/api/import/greenhouse/acme", json={"roles": ["engineer"], "source_refs": [chosen]}).json()
    assert (result["seen"], result["created"]) == (1, 1)  # only the ticked role was added
    none = client.post("/api/import/greenhouse/acme", json={"roles": ["engineer"], "source_refs": []}).json()
    assert (none["seen"], none["created"]) == (0, 0)  # an explicit empty selection never means "everything"
    again = client.post("/api/import/greenhouse/acme/preview", json={"roles": ["engineer"]}).json()
    assert {j["source_ref"]: j["in_pipeline"] for j in again["jobs"]} == {str(base + 1): False, chosen: True}


def test_board_preview_errors_are_readable(monkeypatch):
    def gone(token):
        raise httpx.HTTPStatusError("nope", request=httpx.Request("GET", "https://x.test"),
                                    response=httpx.Response(404))
    monkeypatch.setattr("app.services.sources._fetch_lever", gone)
    response = client.post("/api/import/lever/nope/preview", json={})
    assert response.status_code == 404 and "Lever" in response.json()["detail"]


def test_rank_batch_queues_unranked_jobs_without_ranking_them():
    job = make_job()
    response = client.post("/api/jobs/rank-batch")
    assert response.status_code == 202 and job["id"] in response.json()["queued"]
    tasks = client.get("/api/ranking/tasks", params={"job_ids": str(job["id"])}).json()
    assert tasks["active"] == 1 and tasks["tasks"][0]["status"] == "PENDING"
    assert client.get(f"/api/jobs/{job['id']}").json()["fit"] is None  # nothing was ranked inside the request
    again = client.post("/api/jobs/rank-batch").json()
    assert job["id"] in again["queued"] and len(client.get("/api/ranking/tasks", params={
        "job_ids": str(job["id"])}).json()["tasks"]) == 1  # queueing twice never creates a second task
    assert client.get("/api/ranking/tasks", params={"job_ids": "1,x"}).status_code == 422


def test_extract_job_posting_reads_json_ld():
    html = """<html><head><title>Careers</title><script type="application/ld+json">
    {"@type": "JobPosting", "title": "Staff ML Engineer", "hiringOrganization": {"name": "Quantiphi"},
     "jobLocation": {"address": {"addressLocality": "Bengaluru", "addressCountry": "IN"}},
     "description": "<p>Build <b>RAG</b> systems</p>"}</script></head><body></body></html>"""
    data = extract_job_posting(html)
    assert data["title"] == "Staff ML Engineer" and data["company"] == "Quantiphi"
    assert data["location"] == "Bengaluru, IN" and "RAG" in data["description"]


def test_tailor_and_approval_queues_filter_and_sort():
    use_resume_text(client, "Built Python FastAPI services at 10k rps", name="Test Candidate", headline="Engineer")
    tag = uuid.uuid4().hex[:8]
    tailored, skipped, bare, approved = (make_job(title=f"Engineer {tag} {n}", company=f"Filter {tag} {n}")["id"]
                                         for n in range(4))
    for job_id in (tailored, skipped, bare, approved):
        client.post(f"/api/jobs/{job_id}/triage", json={"action": "shortlist"})
    assert client.post(f"/api/jobs/{tailored}/tailor").status_code == 200
    assert client.post(f"/api/jobs/{skipped}/skip-tailoring").status_code == 200
    assert client.post(f"/api/jobs/{approved}/tailor").status_code == 200
    assert client.post(f"/api/jobs/{approved}/approve").status_code == 200

    def tailor(**params):
        out = client.get("/api/tailor/queue", params={"q": tag, "with_facets": True, **params}).json()
        return {r["id"] for r in out["items"]}, out

    ids, out = tailor()
    assert ids == {tailored, skipped, bare} and out["total_all"] >= 3 and "Manual" in out["boards"]
    assert tailor(status="not_tailored")[0] == {bare}
    assert tailor(status="has_packet")[0] == {tailored, skipped}
    assert tailor(status="untailored")[0] == {skipped}
    assert tailor(min_review=1)[0] == {tailored, skipped}
    assert tailor(q=f"Filter {tag} 2")[0] == {bare}
    assert tailor(board="Greenhouse")[0] == set()
    # Without with_facets the endpoint still returns a plain list.
    assert isinstance(client.get("/api/tailor/queue", params={"q": tag}).json(), list)
    assert client.get("/api/tailor/queue", params={"status": "nope"}).status_code == 422
    assert client.get("/api/tailor/queue", params={"sort": "nope"}).status_code == 422

    def approvals(**params):
        return [r["id"] for r in client.get("/api/approvals", params={"q": tag, **params}).json()]

    assert set(approvals()) == {tailored, skipped, approved}
    assert approvals(status="approved") == [approved]
    assert set(approvals(status="waiting")) == {tailored, skipped}
    assert approvals(sort="company") == sorted([tailored, skipped, approved])
    assert client.get("/api/approvals", params={"status": "not_tailored"}).status_code == 422


def applied_application():
    use_resume_text(client, "Built Python FastAPI services at 10k rps", name="Test Candidate", headline="Engineer")
    job = make_job()
    client.post(f"/api/jobs/{job['id']}/triage", json={"action": "shortlist"})
    assert client.post(f"/api/jobs/{job['id']}/tailor").status_code == 200
    assert client.post(f"/api/jobs/{job['id']}/approve").status_code == 200
    assert client.post(f"/api/jobs/{job['id']}/applied", json={"recruiter_name": "Priya"}).status_code == 200
    return next(a for a in client.get("/api/tracker").json()["applications"] if a["job_id"] == job["id"])


def test_application_keeps_several_recruiters():
    row = applied_application()
    assert row["recruiters"] == [{"name": "Priya", "email": "", "phone": ""}]
    url = f"/api/applications/{row['id']}/recruiters"
    out = client.put(url, json={"recruiters": [
        {"name": " Karan Gupta ", "email": "Karan.Gupta@Example.com", "phone": "+91 98765-43210"},
        {"name": "Azhar Ahmed", "phone": "8904304811"},
        {"email": "talent@example.com"}]}).json()
    assert out["recruiters"] == [{"name": "Karan Gupta", "email": "Karan.Gupta@example.com", "phone": "+91 98765-43210"},
                                 {"name": "Azhar Ahmed", "email": "", "phone": "8904304811"},
                                 {"name": "", "email": "talent@example.com", "phone": ""}]
    assert out["recruiter_name"] == "Karan Gupta"  # the first recruiter is mirrored for older readers

    # The single-recruiter endpoint edits only the first one.
    out = client.put(f"/api/applications/{row['id']}/recruiter", json={"recruiter_name": "Deepak", "recruiter_email": ""}).json()
    assert [r["name"] for r in out["recruiters"]] == ["Deepak", "Azhar Ahmed", ""]
    assert out["recruiters"][0]["phone"] == "+91 98765-43210"

    assert client.put(url, json={"recruiters": [{"name": "X", "email": "not-an-email"}]}).status_code == 422
    assert client.put(url, json={"recruiters": [{"name": "X", "phone": "call me"}]}).status_code == 422
    assert client.put(url, json={"recruiters": [{}]}).status_code == 422
    assert client.put(url, json={"recruiters": [{"name": str(n)} for n in range(21)]}).status_code == 422
    assert client.put(url, json={"recruiters": []}).json()["recruiters"] == []


def test_interview_is_scheduled_in_its_time_zone_with_a_contact():
    row = applied_application()
    url = f"/api/applications/{row['id']}/events"
    local = (datetime.now(UTC) + timedelta(days=2)).replace(hour=16, minute=0, second=0, microsecond=0, tzinfo=None)
    out = client.post(url, json={"kind": "interview", "title": "Telephonic", "local_time": local.isoformat(),
                                 "timezone": "Asia/Kolkata", "contact_name": "Azhar Ahmed",
                                 "contact_email": "azhar@example.com", "contact_phone": "+91 8904304811"}).json()
    event = next(e for e in out["timeline"] if e["kind"] == "interview")
    assert event["at"] == (local - timedelta(hours=5, minutes=30)).isoformat()  # stored as UTC
    assert event["timezone"] == "Asia/Kolkata" and event["at_local"].endswith("16:00 IST")
    assert (event["contact_name"], event["contact_email"], event["contact_phone"]) == (
        "Azhar Ahmed", "azhar@example.com", "+91 8904304811")
    assert out["next_date"].endswith("16:00 IST")

    bad = {"kind": "interview", "title": "Panel", "local_time": local.isoformat()}
    assert client.post(url, json={**bad, "timezone": "Mars/Olympus"}).status_code == 422
    assert client.post(url, json=bad).status_code == 422  # a local time needs a zone
    assert client.post(url, json={**bad, "timezone": "UTC", "contact_email": "nope"}).status_code == 422
    # An offset-aware occurred_at is converted, not truncated.
    out = client.post(url, json={"kind": "note", "title": "Call", "occurred_at": "2026-09-16T16:00:00+05:30"}).json()
    assert any(e["at"] == "2026-09-16T10:30:00" for e in out["timeline"])


def test_location_filters_group_by_city():
    assert location_key("Bengaluru, Karnataka, India") == location_key("Bengaluru, India") == "Bengaluru"
    assert location_key("Remote - India") == location_key("remote") == "Remote"
    assert location_key("") == location_key(None) == "Not listed"


def test_discovery_tailor_and_approvals_filter_by_location():
    use_resume_text(client, "Built Python FastAPI services at 10k rps", name="Test Candidate", headline="Engineer")
    tag = uuid.uuid4().hex[:8]
    blr, blr2, ggn = (make_job(title=f"Engineer {tag} {n}", company=f"Place {tag} {n}", location=where)["id"]
                      for n, where in enumerate(["Bengaluru, Karnataka, India", "Bengaluru, India", "Gurugram, India"]))

    def search(**params):
        out = client.get("/api/jobs/search", params={"q": tag, **params}).json()
        return {r["id"] for r in out["items"]}, out

    ids, out = search()
    assert ids == {blr, blr2, ggn}
    facets = dict(out["locations"])
    assert facets["Bengaluru"] >= 2 and facets["Gurugram"] >= 1, "one option per city, with its count"
    assert search(location="Bengaluru")[0] == {blr, blr2}
    assert search(location="Gurugram")[0] == {ggn}

    for job_id in (blr, blr2, ggn):
        client.post(f"/api/jobs/{job_id}/triage", json={"action": "shortlist"})
    tailor = client.get("/api/tailor/queue", params={"q": tag, "location": "Bengaluru", "with_facets": True}).json()
    assert {r["id"] for r in tailor["items"]} == {blr, blr2} and "Gurugram" in dict(tailor["locations"])

    assert client.post(f"/api/jobs/{ggn}/tailor").status_code == 200
    assert client.post(f"/api/jobs/{blr}/tailor").status_code == 200
    approvals = client.get("/api/approvals", params={"q": tag, "location": "Gurugram", "with_facets": True}).json()
    assert [r["id"] for r in approvals["items"]] == [ggn]
    assert {"Bengaluru", "Gurugram"} <= set(dict(approvals["locations"])), "options come from before filtering"


def test_a_job_carries_its_posting_date_apart_from_when_it_was_found():
    job = make_job()
    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert detail["posted_at"] is None and detail["posted_raw"] is None, "unknown stays unknown, never the found date"
    assert detail["posted"].endswith("ago"), "`posted` is still when it was found, for older screens"
    with SessionLocal() as db:
        row = db.get(Job, job["id"])
        row.posted_at, row.posted_date_raw = datetime(2026, 9, 14), "2026-09-14"
        db.commit()
    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert detail["posted_at"].startswith("2026-09-14") and detail["posted_ago"].endswith("d ago")


def test_discovery_filters_by_found_or_posted_date_and_hides_applied_jobs():
    from app.models import Application

    tag = uuid.uuid4().hex[:8]
    fresh, old, never_posted, applied = (make_job(title=f"Engineer {tag} {n}", company=f"Dated {tag} {n}")["id"]
                                         for n in range(4))
    now = datetime.now(UTC).replace(tzinfo=None)
    with SessionLocal() as db:
        db.get(Job, fresh).posted_at = now - timedelta(hours=5)
        stale = db.get(Job, old)
        stale.discovered_at, stale.posted_at = now - timedelta(days=20), now - timedelta(days=40)
        db.get(Job, applied).posted_at = now - timedelta(days=2)
        db.get(Job, applied).status = "applied"
        db.add(Application(job_id=applied, user_id=db.get(Job, applied).user_id, applied_at=now))
        db.commit()

    def ids(**params):
        out = client.get("/api/jobs/search", params={"q": tag, "per_page": 200, **params}).json()
        return {r["id"] for r in out["items"]}, out

    everything, out = ids()
    assert applied not in everything and out["applied_hidden"] >= 1, "applied jobs stay out of Discovery"
    assert applied in ids(include_applied=True)[0] and applied in ids(stage="applied")[0]
    assert ids(within="7d")[0] == {fresh, never_posted}, "found in the last 7 days"
    assert ids(within="24h", date_field="posted")[0] == {fresh}, "a job without a posting date is never 'recent'"
    assert ids(within="30d", date_field="posted")[0] == {fresh}, "posted 40 days ago is out"
    assert client.get("/api/jobs/search", params={"within": "2y"}).status_code == 422
    newest = client.get("/api/jobs/search", params={"q": tag, "sort": "posted", "include_applied": True}).json()
    assert [r["id"] for r in newest["items"]][:3] == [fresh, applied, old], "newest posting first, undated last"


def _sent(stage="applied", location="Remote", title="Lead AI Engineer", fit=80.0, days_ago=5, history=(), app_id=None):
    """An in-memory application for the Overview outcome helpers: `history` lists earlier stages it passed through,
    as a stage name or (stage, meta)."""
    events = [SimpleNamespace(kind="stage", detail=past, meta=None) if isinstance(past, str)
              else SimpleNamespace(kind="stage", detail=past[0], meta=past[1]) for past in history]
    job = SimpleNamespace(location=location, title=title, fit_score=fit)
    return SimpleNamespace(id=app_id or uuid.uuid4().int, stage=stage, events=events, job=job,
                           applied_at=datetime(2026, 9, 30) - timedelta(days=days_ago))


def test_overview_outcomes_by_location_role_and_fit():
    from app.workspace import _fit_band, _outcome_rows, _outcomes, _role_bucket

    apps = [
        _sent("rejected", "Bengaluru, Karnataka, India", fit=92, history=["screening"]),  # replied, then rejected
        _sent("interviewing", "Bengaluru", fit=78, history=["screening"]),
        _sent("applied", "Remote - India", title="Data Scientist", fit=55, days_ago=30),  # no reply after 3 weeks
        _sent("applied", "Remote", fit=None),
        _sent("offer", "Pune", history=["screening", "interviewing"]),
    ]
    by_location = {r["label"]: r for r in _outcome_rows(apps, lambda a: location_key(a.job.location), {})}
    assert by_location["Bengaluru"] == {"label": "Bengaluru", "applied": 2, "replies": 2, "interviews": 1,
                                        "rejected": 1, "rate": "100%"}
    assert by_location["Remote"]["applied"] == 2 and by_location["Remote"]["rate"] == "0%"

    roles = ["Lead AI Engineer", "Data Scientist"]
    by_role = {r["label"]: r["applied"] for r in _outcome_rows(apps, lambda a: _role_bucket(a.job.title, roles), {})}
    assert by_role == {"Lead AI Engineer": 4, "Data Scientist": 1}

    bands = _outcome_rows(apps, lambda a: _fit_band(a.job.fit_score), {}, order=["90+", "75–89", "60–74", "Under 60",
                                                                             "Unscored"])
    assert [r["label"] for r in bands] == ["90+", "75–89", "Under 60", "Unscored"]

    out = _outcomes(apps, datetime(2026, 9, 30), {})
    counts = {b["id"]: b["n"] for b in out["buckets"]}
    assert counts == {"rejected": 1, "interviewing": 1, "no_response": 1, "awaiting": 1, "offer": 1}
    assert out["answered"] == 3 and out["response_rate"] == "60%"


def test_outcome_rows_fold_the_long_tail_into_other():
    from app.workspace import _outcome_rows

    apps = [_sent(location=f"City {i}") for i in range(8)]
    rows = _outcome_rows(apps, lambda a: a.job.location, {}, limit=4)
    assert [r["label"] for r in rows][-1] == "Other" and len(rows) == 4
    assert sum(r["applied"] for r in rows) == 8


def test_overview_counts_reasons_for_passing_on_jobs():
    def passed(reason):
        rows = client.get("/api/workspace/overview").json()["pass_reasons"]
        return next((r["n"] for r in rows if r["id"] == reason), 0)

    before = passed("compensation")
    job = make_job()
    assert client.post(f"/api/jobs/{job['id']}/reject", json={"reason": "compensation"}).status_code == 200
    assert passed("compensation") == before + 1


@pytest.mark.parametrize(("title", "expected"), [
    ("Senior Product Manager", "Product Manager"),            # a target role, levels aside
    ("Associate Product Manager - Payments", "Associate Product Manager"),  # the most specific target role wins
    ("Software Engineering Manager", "Engineering Manager"),  # shares "manager" with a target, but is not one
    ("Staff Software Engineer", "Software Engineer"),
    ("Senior Backend Developer (Python)", "Software Engineer"),
    ("Lead AI/ML Engineer", "AI/ML Engineer"),
    ("Senior AI platform Engineer", "AI/ML Engineer"),
    ("Software Engineer II, Machine Learning", "AI/ML Engineer"),
    ("AI Forward Deployed Engineer", "Forward Deployed / Solutions"),
    ("Principal Data Scientist", "Data Scientist"),
    ("Data Engineer III", "Data Engineer"),
    ("Site Reliability Engineer", "Platform / DevOps / SRE"),
    ("Senior Technical Writer - Docs", "Technical Writer"),   # unrecognised: the title without its level
    ("Senior", "Other roles"),
])
def test_role_bucket_groups_titles_by_target_role_then_role_type(title, expected):
    from app.workspace import _role_bucket

    assert _role_bucket(title, ["Product Manager", "Associate Product Manager"]) == expected



def test_outcomes_count_inbox_replies_and_past_rejections_but_not_undone_inbox_moves():
    from app.workspace import _outcome_rows, _outcomes

    by_inbox = {"source": "application_inbox", "email_event_id": 7}
    undone = {"source": "application_inbox_correction", "email_event_id": 7}
    apps = [
        # An Inbox email moved it to screening, then a correction moved it back: no reply after all.
        _sent("applied", days_ago=30, history=[("screening", by_inbox), ("applied", undone)], app_id=1),
        # The employer asked for documents by email; the stage never moved.
        _sent("applied", days_ago=30, app_id=2),
        # Rejected, then closed: the rejection still counts as an answer.
        _sent("closed", days_ago=30, history=["rejected"], app_id=3),
    ]
    emails = {2: {"DOCUMENT_REQUEST"}}
    row = _outcome_rows(apps, lambda a: "all", emails)[0]
    assert (row["replies"], row["rejected"]) == (1, 1)
    out = _outcomes(apps, datetime(2026, 9, 30), emails)
    assert {b["id"]: b["n"] for b in out["buckets"]} == {"no_response": 1, "in_process": 1, "closed": 1}
    assert out["answered"] == 2


def test_overview_inbox_counts_need_a_connected_gmail_account():
    from app.models import GmailConnection, User
    from app.workspace import _inbox_counts

    since = datetime(2026, 1, 1)
    with SessionLocal() as db:
        db.query(GmailConnection).delete()
        user_id = db.scalar(select(User.id).limit(1))
        db.add(GmailConnection(user_id=user_id, email_address="t@example.com", status="REVOKED"))
        db.flush()
        assert _inbox_counts(db, since) is None
        db.query(GmailConnection).update({"status": "CONNECTED"})
        assert _inbox_counts(db, since) is not None
        db.rollback()


def test_easy_apply_reads_explicit_signals_only():
    from app.workspace import easy_apply

    def linkedin(raw=None):
        return Job(source="apify:linkedin", raw=raw or {})

    assert easy_apply(linkedin({"easyApply": True})) and easy_apply(linkedin({"directapply": "true"}))
    assert easy_apply(linkedin({"applyType": "EASY_APPLY"}))
    assert not easy_apply(linkedin({"easyApply": False, "applyType": "EXTERNAL"}))
    assert not easy_apply(linkedin())  # no signal is not a guess either way
    # A search that asked the board for Easy Apply jobs only.
    assert easy_apply(linkedin(), {"easyApply": True})
    assert easy_apply(Job(source="apify:zip_recruiter", raw={}), {"easyApply": True})
    assert not easy_apply(Job(source="apify:google", raw={}), {"easyApply": True})  # Google Jobs ignores the input
    assert easy_apply(Job(source="apify:linkedin", raw={}), {"directApply": True})


def test_approvals_filter_to_easy_apply_jobs():
    plain, easy = make_job(), make_job()
    with SessionLocal() as db:
        run = SearchRun(actor_id="openclawai/job-board-scraper", status="SUCCEEDED", external_run_id=uuid.uuid4().hex,
                        input_json={"easyApply": True}, user_id=db.get(Job, easy["id"]).user_id)
        db.add(run)
        db.flush()
        for job_id in (plain["id"], easy["id"]):
            tailored = db.get(Job, job_id)
            tailored.status = "tailored"
            # Approvals lists a packet once its latest version is reviewed.
            db.add(ResumeVariant(job_id=job_id, user_id=tailored.user_id, version=1, markdown="# Packet",
                                 reviewer_score=80))
        job = db.get(Job, easy["id"])
        job.source, job.search_run_id = "apify:linkedin", run.id
        db.commit()

    rows = client.get("/api/approvals", params={"easy_apply": "only", "with_facets": True}).json()
    ids = {r["id"] for r in rows["items"]}
    assert easy["id"] in ids and plain["id"] not in ids and rows["easy_apply"] >= 1
    assert all(r["easy_apply"] for r in rows["items"])
    every = {r["id"]: r["easy_apply"] for r in client.get("/api/approvals").json()}
    assert every[plain["id"]] is False and every[easy["id"]] is True
    assert client.get("/api/approvals", params={"easy_apply": "maybe"}).status_code == 422
