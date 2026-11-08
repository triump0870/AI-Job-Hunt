"""A job with no description can get one from the job screen: read from the posting URL, or pasted by hand.

Reading the posting never saves anything — a page without JobPosting data yields every word on it, so the text comes
back as a draft for the user to check. Saving it parses the posting and queues a ranking against it.
"""
import uuid

from fastapi.testclient import TestClient

from app import workspace
from app.db import SessionLocal
from app.main import app
from app.models import Job
from app.services.lifecycle import WorkflowState as S
from app.workspace import extract_job_posting

client = TestClient(app)
REAL_POSTING = "We are hiring a lead engineer. " * 20


def empty_job(monkeypatch, url=None):
    monkeypatch.setattr(workspace, "fetch_job_posting", lambda *a, **k: {"description": "", "description_source": "page_text"})
    job = client.post("/api/jobs/from-url", json={"url": url or f"https://example.com/nodesc/{uuid.uuid4().hex}",
                                                  "title": "Product Manager", "company": "Paytm"}).json()["job"]
    return job


def test_a_job_added_from_a_url_without_text_does_not_borrow_its_title(monkeypatch):
    job = empty_job(monkeypatch)
    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert detail["description"] == "" and detail["needs_description"] is True


def test_a_title_can_be_corrected_without_queueing_a_ranking(monkeypatch):
    job = empty_job(monkeypatch)
    with SessionLocal() as db:
        before = db.get(Job, job["id"])
        state, dedupe = before.state, before.dedupe_key
    saved = client.put(f"/api/jobs/{job['id']}/title", json={"title": "  Senior   AI Engineer\n"})
    assert saved.status_code == 200
    assert saved.json()["job"]["title"] == "Senior AI Engineer"
    with SessionLocal() as db:
        stored = db.get(Job, job["id"])
        assert stored.title == "Senior AI Engineer"
        assert stored.state is state and stored.dedupe_key == dedupe


def test_a_title_cannot_be_blank_a_placeholder_or_too_long(monkeypatch):
    job = empty_job(monkeypatch)
    for title in ("   ", "Untitled", "x" * 301):
        assert client.put(f"/api/jobs/{job['id']}/title", json={"title": title}).status_code == 422
    assert client.get(f"/api/jobs/{job['id']}").json()["title"] == "Product Manager"
    assert client.put("/api/jobs/999999999/title", json={"title": "AI Engineer"}).status_code == 404


def test_a_pasted_description_is_saved_and_queued_for_ranking(monkeypatch):
    job = empty_job(monkeypatch)
    too_short = client.put(f"/api/jobs/{job['id']}/description", json={"description": "PM role"})
    assert too_short.status_code == 422
    saved = client.put(f"/api/jobs/{job['id']}/description", json={"description": REAL_POSTING})
    assert saved.status_code == 200
    body = saved.json()
    assert body["ranking_queued"] is True and body["job"]["needs_description"] is False
    with SessionLocal() as db:
        stored = db.get(Job, job["id"])
        assert stored.description == REAL_POSTING.strip()
        assert stored.state is S.RANK_PENDING


def test_fetching_returns_a_draft_and_saves_nothing(monkeypatch):
    job = empty_job(monkeypatch)
    monkeypatch.setattr(workspace, "fetch_job_posting",
                        lambda *a, **k: {"description": REAL_POSTING, "description_source": "posting"})
    out = client.post(f"/api/jobs/{job['id']}/description/fetch").json()
    assert out == {"description": REAL_POSTING.strip(), "source": "posting"}
    assert client.get(f"/api/jobs/{job['id']}").json()["description"] == "", "the user saves it, not the fetch"


def test_a_page_with_no_readable_description_is_reported(monkeypatch):
    job = empty_job(monkeypatch)
    response = client.post(f"/api/jobs/{job['id']}/description/fetch")
    assert response.status_code == 422 and "paste the description" in response.json()["detail"]


def test_extraction_says_where_the_text_came_from():
    ld = ('<script type="application/ld+json">{"@type": "JobPosting", "title": "PM", '
          '"hiringOrganization": {"name": "Paytm"}, "description": "<p>Own the roadmap.</p>"}</script>')
    assert extract_job_posting(f"<html><body>{ld}</body></html>")["description_source"] == "posting"
    plain = extract_job_posting("<html><body><nav>Menu</nav><p>Own the roadmap.</p></body></html>")
    assert plain["description_source"] == "page_text" and plain["description"] == "Own the roadmap."
