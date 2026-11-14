"""The job detail's "Close the gaps" notes: each gap once, and a partial-evidence hint only for a meaningful shared term."""
from app.db import SessionLocal
from app.models import Evidence, Job
from tests.test_discovery_quality import add, client, user  # noqa: F401  fixtures


def notes_for(owner: int, client, analysis: dict, claims: list[tuple[str, list[str]]]) -> list[dict]:  # noqa: F811
    job_id = add(owner, "Senior Backend Engineer")
    with SessionLocal() as db:
        db.info["user_id"] = owner
        db.get(Job, job_id).fit_analysis = analysis
        for claim, skills in claims:
            db.add(Evidence(user_id=owner, source="resume", verified=True, claim=claim, company="Adori AI",
                            role="Engineer", skills=skills))
        db.commit()
    return client.get(f"/api/jobs/{job_id}").json()["gap_notes"]


def test_a_gap_listed_as_fatal_and_missing_shows_once(user, client):  # noqa: F811
    analysis = {"must_have_missing": ["Security engineering", "Endpoint software", "Rust"],
                "gaps": ["security engineering", "Rust", "Kubernetes"]}
    assert [n["gap"] for n in notes_for(user, client, analysis, [])] == \
        ["Security engineering", "Endpoint software", "Rust", "Kubernetes"]


def test_a_generic_word_alone_is_not_partial_evidence(user, client):  # noqa: F811
    claims = [("Developed data-driven applications that reduced reporting time by 50%", ["Data engineering"]),
              ("Hardened the security of the payments API", ["Security"])]
    notes = {n["gap"]: n["partial"] for n in notes_for(user, client, {"gaps": ["Endpoint software", "Security engineering"]}, claims)}
    assert notes["Endpoint software"] == ""
    assert notes["Security engineering"] == "Adori AI: Hardened the security of the payments API"
