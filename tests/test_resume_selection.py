"""One resume selection, end to end.

* The active version is the only selection, and only an explicit choice changes it (uploads and edits never do).
* Every workflow resolves its resume through resume_verification.selected / for_job: the job's own resume (pinned by its
  search setup or by the person, or matched by tags), else the active one, in the form the candidate policy names.
* A setup's resume is acknowledged: changing it moves the setup's jobs, except the ones the person pinned themselves.
"""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.db import SessionLocal
from app.main import app as web_app
from app.models import CandidateProfile, Job, Resume, SavedSearch, SearchRun
from app.services import discovery, resume_matching
from app.services import resume_verification as rv
from app.services import structured_resume as sr
from app.services.context_builder import build_ranking_context, build_review_context
from app.services.resume_format import parse_resume
from tests.test_discovery_quality import RESUME_A, RESUME_B, add, job_of
from tests.test_discovery_quality import resumes as resumes  # noqa: PLC0414  fixtures, re-exported
from tests.test_discovery_quality import user as user  # noqa: PLC0414

client = TestClient(web_app)

STRUCTURED = """# Jane Doe
Staff Engineer
Pune | jane@example.com

## Summary
Builds data platforms.

## Skills
- Data: Kafka, Spark

## Experience
### Lead Engineer | Zendrive (India) | Bengaluru | Jun 2024 - Present
- Reduced incidents by 45% across Kafka pipelines
- Owned ingestion

### Developer | Pixel Co | Remote | Jan 2020 - May 2024
- Built dashboards in React

## Education
B.Tech | IIT Bombay | 2016
"""


def session(user_id):
    db = SessionLocal()
    db.info["user_id"] = user_id
    return db


def profile_of(db, user_id):
    return db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user_id))


def set_policy(**changes):
    assert client.put("/api/settings/resume/policy", json=changes).status_code == 200


# ───────────── rendering the structured data ─────────────

def test_the_structured_document_reads_back_as_the_same_roles():
    data = sr.materialize(sr.parse_deterministic(STRUCTURED))
    document = sr.render_document(data)
    parsed = parse_resume(document)
    assert [(r.title, r.company, r.location, r.dates) for r in parsed.roles] == [
        ("Lead Engineer", "Zendrive (India)", "Bengaluru", "Jun 2024 - Present"),
        ("Developer", "Pixel Co", "Remote", "Jan 2020 - May 2024")]
    assert parsed.roles[0].bullets == ["Reduced incidents by 45% across Kafka pipelines", "Owned ingestion"]
    assert parsed.name == "Jane Doe" and "## Education" in document


def test_the_document_shows_corrections_and_leaves_out_what_was_rejected():
    data = sr.materialize(sr.parse_deterministic(STRUCTURED))
    lead, developer = data["experience"]
    lead.update(job_title="Principal Engineer", start_date="2024-07")  # corrected in Resume Data
    lead["responsibilities"][0]["verification"] = {"status": "REJECTED"}
    developer["verification"] = {"status": "REJECTED"}
    parsed = parse_resume(sr.render_document(data))
    assert [(r.title, r.dates) for r in parsed.roles] == [("Principal Engineer", "Jul 2024 - Present")]
    assert parsed.roles[0].bullets == ["Reduced incidents by 45% across Kafka pipelines"]


def test_a_project_achievement_the_description_already_says_is_not_repeated():
    project = {"name": "Knowledge Assistant", "verification": {"status": "VERIFIED"},
               "description": "Designed RAG pipelines, resulting in 30% faster document retrieval for internal teams.",
               "achievements": [{"text": "resulting in 30% faster document retrieval for internal teams.",
                                 "verification": {"status": "VERIFIED"}},
                                {"text": "Cut hosting cost by 20%", "verification": {"status": "VERIFIED"}}]}
    document = sr.render_document({"projects": [project]})
    assert "- resulting in 30%" not in document, "the fragment is already in the description line"
    assert "- Cut hosting cost by 20%" in document, "a new achievement is still listed"
    assert document.count("30% faster document retrieval") == 1


def test_a_role_without_dates_keeps_its_location():
    data = sr.materialize(sr.parse_deterministic(STRUCTURED))
    lead = data["experience"][0]
    for key in ("start_date", "end_date", "raw_start_date", "raw_end_date", "is_current", "current"):
        lead.pop(key, None)
    lead["location"] = "Bengaluru (hybrid)"  # corrected in Resume Data
    document = sr.render_document(data)
    parsed = parse_resume(document)
    assert [(r.title, r.company, r.location, r.dates) for r in parsed.roles] == [
        ("Lead Engineer", "Zendrive (India)", "Bengaluru (hybrid)", ""),
        ("Developer", "Pixel Co", "Remote", "Jan 2020 - May 2024")]
    from app.services.resume_format import render_resume

    again = parse_resume(render_resume(parsed))  # a tailored resume renders it the same way
    assert (again.roles[0].location, again.roles[0].dates) == ("Bengaluru (hybrid)", "")


# ───────────── the selection never moves by itself ─────────────

def test_only_the_first_resume_becomes_active_by_itself(user):
    with session(user) as db:
        profile = profile_of(db, user)
        first = rv.record_version(db, profile, RESUME_A, source="manual_text")
        second = rv.record_version(db, profile, RESUME_B, source="manual_text")
        assert first.is_active and not second.is_active
        assert profile.master_resume == RESUME_A, "the master text mirrors the active version"
        assert rv.save_text_version(db, profile, RESUME_B + "\n- Mentored two engineers").is_active is False
        rv.set_active(db, second)
        assert profile.master_resume == RESUME_B and not db.get(Resume, first.id).is_active
        profile.master_resume = "stale text from an old page"  # whatever happens to the mirror ...
        assert rv.active_resume(db, profile).id == second.id, "... never decides which resume is active"
        db.rollback()


def test_every_workflow_reads_the_same_resume_in_the_same_form(user, resumes):
    active, ml = resumes
    job_id = add(user, "Machine Learning Engineer")
    with session(user) as db:
        job = db.get(Job, job_id)
        job.resume_id, job.resume_choice = ml, resume_matching.USER
        profile = profile_of(db, user)
        view = rv.for_job(db, profile, job)
        assert view.chosen_resume.id == ml and view.resume_source == "structured"
        assert build_ranking_context(db, profile, job).resume["id"] == ml, "ranking"
        assert build_review_context(db, profile, job).resume["id"] == ml, "review"
        assert rv.selected(db, profile).chosen_resume.id == active, "no job: the active resume"
        db.rollback()


def test_the_text_as_written_is_what_ranking_reads_when_that_is_the_source(user, resumes):
    job_id = add(user, "Backend Engineer")
    set_policy(resume_source="master_text")
    with session(user) as db:
        context = build_ranking_context(db, profile_of(db, user), db.get(Job, job_id))
        assert context.resume_text == RESUME_A and context.relevant_experience == []
        assert "summary" not in context.candidate and "verified_background" not in context.candidate, \
            "nothing parsed from the data travels beside the text"
        assert context.education == [] and context.certifications == []
        db.rollback()


def test_editing_structured_data_changes_the_document_but_not_the_selection(user, resumes):
    active, ml = resumes
    with session(user) as db:
        resume = db.get(Resume, ml)
        data = dict(rv.ensure_structured(db, resume))
        data["summary"] = {**data["summary"], "text": "Ranks things for a living"}
        resume.structured_data = data
        flag_modified(resume, "structured_data")
        db.commit()
    with session(user) as db:
        assert db.get(Resume, active).is_active and not db.get(Resume, ml).is_active
        job = Job(user_id=user, title="x", company="y", resume_id=ml)
        assert "Ranks things for a living" in rv.for_job(db, profile_of(db, user), job).master_resume


# ───────────── search setups acknowledge their resume ─────────────

def _setup_with_jobs(user, resume_id):
    with session(user) as db:
        setup = SavedSearch(user_id=user, name=f"ML {uuid.uuid4().hex[:6]}",
                            settings={"roles": ["ML Engineer"], "resume_mode": "fixed", "resume_id": resume_id})
        db.add(setup)
        db.flush()
        run = SearchRun(user_id=user, provider="apify", saved_search_id=setup.id, external_run_id=uuid.uuid4().hex)
        db.add(run)
        db.commit()
        return setup.id, run.id


def test_changing_a_setups_resume_moves_its_jobs_except_the_ones_you_pinned(user, resumes):
    active, ml = resumes
    setup_id, run_id = _setup_with_jobs(user, ml)
    follows, pinned, applied = (add(user, "Machine Learning Engineer", search_run_id=run_id) for _ in range(3))
    elsewhere = add(user, "Machine Learning Engineer")  # found by no setup
    assert job_of(follows).resume_id == ml and job_of(follows).resume_choice == resume_matching.SETUP
    with session(user) as db:
        db.get(Job, pinned).resume_choice = resume_matching.USER  # the person chose it on the job
        db.get(Job, applied).status = "applied"
        db.commit()

    with session(user) as db:
        setup = db.get(SavedSearch, setup_id)
        setup.settings = {**setup.settings, "resume_id": active}
        out = discovery.follow_setup(db, setup, actor="test")
        db.commit()
    assert out["changed"] == [follows]
    assert job_of(follows).resume_id == active, "the setup's jobs follow its resume"
    assert job_of(pinned).resume_id == ml, "a resume you pinned on the job wins"
    assert job_of(applied).resume_id == ml, "what was sent stays what was sent"
    assert elsewhere not in out["changed"], "only this setup's jobs"


def test_the_setup_api_reports_the_jobs_that_moved(user, resumes):
    _, ml = resumes
    setup_id, run_id = _setup_with_jobs(user, ml)
    job_id = add(user, "Machine Learning Engineer", search_run_id=run_id)
    setup = client.get("/api/search/setups").json()["setups"]
    card = next(s for s in setup if s["id"] == setup_id)
    body = {k: card[k] for k in ("name", "schedule_kind", "every_hours", "time_of_day", "weekdays", "timezone", "enabled")
            if k in card}
    body["settings"] = {**card["settings"], "resume_mode": "active", "resume_id": None}
    response = client.put(f"/api/search/setups/{setup_id}", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["resume_jobs"]["changed"] == [job_id]
    moved = job_of(job_id)
    assert moved.resume_id is None and moved.resume_choice == resume_matching.SETUP, "now the active resume"
    unchanged = client.put(f"/api/search/setups/{setup_id}", json=body).json()
    assert "resume_jobs" not in unchanged, "saving without a resume change moves nothing"


def test_resume_data_names_the_setups_that_use_each_version(user, resumes):
    _, ml = resumes
    setup_id, _ = _setup_with_jobs(user, ml)
    versions = {v["id"]: v for v in client.get("/api/settings/resume").json()["versions"]}
    assert setup_id in [s["id"] for s in versions[ml]["setups"]]
    viewed = client.get("/api/settings/resume", params={"resume_id": ml}).json()
    assert viewed["current"]["id"] == ml and viewed["active"]["id"] != ml, "viewing never activates"


# ───────────── one screen: each resume with its file, data and target roles ─────────────

def _upload(name, text):
    import io

    return client.post("/api/profile/resume", files={"file": (name, io.BytesIO(text.encode()), "text/plain")}).json()


def test_each_resume_carries_its_own_file_data_and_target_roles(user):
    first, second = _upload("backend.txt", RESUME_A), _upload("ml.txt", RESUME_B)
    assert client.put(f"/api/resumes/{second['resume_id']}/tags",
                      json={"domain": "AI / ML", "roles": ["ML Engineer"]}).status_code == 200
    cards = {v["id"]: v for v in client.get("/api/settings/resume").json()["versions"]}
    one, two = cards[first["resume_id"]], cards[second["resume_id"]]
    assert one["file"]["filename"] == "backend.txt" and two["file"]["filename"] == "ml.txt", "its own file"
    assert two["roles"] == ["ML Engineer"] and two["domain"] == "AI / ML" and one["roles"] == [], "its own roles"
    assert one["is_active"] and not two["is_active"]
    assert {"reviewed_percent", "verification_status", "setups"} <= two.keys()
    data = client.get("/api/settings/resume", params={"resume_id": two["id"]}).json()["current"]
    assert "Trained ranking models in PyTorch" in str(data["structured"]), "its own structured data"


def test_a_file_from_before_versions_shows_up_as_a_resume(user):
    from app.models import ResumeDocument

    with session(user) as db:
        db.add(ResumeDocument(user_id=user, original_filename="old.txt", media_type="text/plain", storage_path="/nowhere",
                              checksum=uuid.uuid4().hex, size=10, extracted_text=RESUME_B, active=False))
        db.commit()
    cards = client.get("/api/settings/resume").json()["versions"]
    assert [c["file"]["filename"] for c in cards if c["file"]] == ["old.txt"]
    assert not any(c["is_active"] for c in cards if c["file"]), "listing a file never makes it active"


def test_deleting_a_resume_removes_its_file_too(user):
    _upload("backend.txt", RESUME_A)
    second = _upload("ml.txt", RESUME_B)
    out = client.delete(f"/api/resumes/{second['resume_id']}").json()
    assert out["file_deleted"] is True
    cards = client.get("/api/settings/resume").json()["versions"]
    assert [c["file"]["filename"] for c in cards] == ["backend.txt"], "no orphan file comes back as a resume"


def test_a_different_file_with_the_same_text_is_its_own_resume(user):
    first, second = _upload("backend.txt", RESUME_A), _upload("backend-copy.txt", RESUME_A + "\n\n")
    assert first["resume_id"] != second["resume_id"], "a distinct upload is never hidden behind another file"
    files = sorted(c["file"]["filename"] for c in client.get("/api/settings/resume").json()["versions"] if c["file"])
    assert files == ["backend-copy.txt", "backend.txt"]
    assert _upload("backend.txt", RESUME_A)["resume_id"] == first["resume_id"], "the same file again: the same resume"


def test_blank_resume_text_is_refused_not_silently_ignored(user):
    _upload("backend.txt", RESUME_A)
    for method in (client.patch, client.put):
        response = method("/api/profile", json={"master_resume": "   "})
        assert response.status_code == 422 and "delete it" in response.text
    assert client.get("/api/profile").json()["master_resume"].strip() == RESUME_A.strip()


def test_the_legacy_file_delete_route_is_gone(user):
    _upload("backend.txt", RESUME_A)
    assert client.delete("/api/profile/resume").status_code == 405, "removing a resume goes through DELETE /api/resumes/{id}"
    assert client.get("/api/profile/resume").status_code == 200, "the active resume's file still downloads"
