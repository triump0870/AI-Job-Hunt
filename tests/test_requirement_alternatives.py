"""Requirements a posting offers as alternatives: "5 years in one of Python, Java, Kotlin ..." is one requirement any
one language meets, never seven, so a Python engineer is not shown Java, Kotlin and Scala as gaps."""
import json

import pytest

from app.models import Job, JobRequirementEvidence
from app.prompts.common import job_sections
from app.prompts.contracts import RankingResult
from app.services import job_context as jc
from app.services import job_description as jd
from tests.test_job_description import (  # noqa: F401  (fixtures)
    add_job,
    client,
    mock_llm,
    session,
    user,
)

ONE_OF = ("At least 5 years of experience coding in one of the following modern programming languages: Python, Java, "
          "Kotlin, Scala, Go, C#, or TypeScript.")
LANGUAGES = ["Python", "Java", "Kotlin", "Scala", "Go", "C#", "TypeScript"]
POSTING = f"<h3>Requirements</h3><ul><li>{ONE_OF}</li><li>Experience with Kafka and Spark is required.</li></ul>"


def _req(index, value, source, level="REQUIRED", group=None):
    return {"id": f"req_{index:03d}", "type": "SKILL", "normalized_value": value, "requirement_level": level,
            "source_text": source, "confidence": 0.95, "is_explicit": True, "is_derived": False,
            "alternative_group": group}


def _parser_answer(groups=True):
    rows = [_req(i, lang, ONE_OF, group="grp_001" if groups else None) for i, lang in enumerate(LANGUAGES, start=1)]
    both = "Experience with Kafka and Spark is required."
    rows += [_req(8, "Kafka", both), _req(9, "Spark", both)]
    return {"structured_description": {"required_skills": LANGUAGES + ["Kafka", "Spark"]}, "requirements": rows}


@pytest.fixture
def parsed_job(user, monkeypatch):  # noqa: F811
    from app.services.llm import LLMClient

    def build(groups=True):
        monkeypatch.setattr(LLMClient, "generate_prompt_json", lambda self, prompt, call=None: _parser_answer(groups))
        job_id = add_job(user, description=POSTING)
        with session(user) as db:
            jd.parse_now(db, db.get(Job, job_id))
            db.commit()
        return job_id
    return build


def test_the_parser_contract_keeps_only_real_groups():
    single = jd.ParsedJobDescription.model_validate({"requirements": [
        _req(1, "Python", ONE_OF, group="grp_001"), _req(2, "Kafka", "x", group="grp_002"),
        _req(3, "Go", "y", group="grp_002", level="PREFERRED")]})
    # One member is no choice, and a group spanning two levels is not one requirement: both are dropped.
    assert all(item.alternative_group is None for item in single.requirements)
    real = jd.ParsedJobDescription.model_validate(_parser_answer())
    assert {item.alternative_group for item in real.requirements[:7]} == {"grp_001"}
    assert all(item.alternative_group is None for item in real.requirements[7:])


def test_parsed_alternatives_are_stored_per_skill_and_read_as_one_requirement(user, parsed_job):  # noqa: F811
    job_id = parsed_job()
    with session(user) as db:
        rows = db.query(JobRequirementEvidence).filter_by(job_id=job_id).order_by(JobRequirementEvidence.id).all()
        assert [r.alternative_group for r in rows[:7]] == ["grp_001"] * 7, "each language keeps its own row"
        context = jc.build_ranking_job_context(db, db.get(Job, job_id))
    grouped = [item for item in context.required if item.get("alternatives")]
    assert len(grouped) == 1 and grouped[0]["alternatives"] == LANGUAGES
    assert grouped[0]["value"].startswith("Any one of: Python, Java")
    assert [item["value"] for item in context.required if not item.get("alternatives")] == ["Kafka", "Spark"]


def test_one_alternative_on_the_resume_covers_the_requirement(user, parsed_job):  # noqa: F811
    job_id = parsed_job()
    with session(user) as db:
        context = jc.build_ranking_job_context(db, db.get(Job, job_id), candidate="Built Python services on Kafka")
    covered = {item["id"] for item in context.candidate_matches}
    assert "grp_001" in covered, "Python alone meets 'one of Python, Java, ...'"
    assert {item["value"] for item in context.candidate_gaps} == {"Spark"}


def test_rows_parsed_before_groups_existed_are_grouped_only_on_an_explicit_choice(user, parsed_job):  # noqa: F811
    job_id = parsed_job(groups=False)
    with session(user) as db:
        context = jc.build_ranking_job_context(db, db.get(Job, job_id))
    grouped = [item for item in context.required if item.get("alternatives")]
    assert len(grouped) == 1 and grouped[0]["alternatives"] == LANGUAGES, "'one of the following' groups them"
    assert {"Kafka", "Spark"} <= {item["value"] for item in context.required}, "'Kafka and Spark' stays two asks"


def test_the_ranker_sees_the_group_once(user, parsed_job):  # noqa: F811
    job_id = parsed_job()
    with session(user) as db:
        context = jc.build_ranking_job_context(db, db.get(Job, job_id))
    [block] = job_sections({"job_context": context.to_dict()})
    body = json.loads(block[block.index("{"):block.rindex("}") + 1])
    values = [item["value"] for item in body["required"]]
    assert sum("Java" in value for value in values) == 1 and len(values) == 3


def test_a_met_group_produces_no_gaps():
    result = RankingResult.model_validate({
        "overall_fit_score": 80, "candidate_fit_score": 80, "resume_fit_score": 80, "seniority_match_score": 80,
        "skills_match_score": 80, "leadership_match_score": 80, "domain_match_score": 80, "location_match_score": 80,
        "qualification_recommendation": "QUALIFY", "reasoning_summary": "ok", "confidence": 0.8,
        "requirement_assessments": [
            {"requirement": "Any one of: Python, Java, Kotlin", "requirement_id": "grp_001", "level": "REQUIRED",
             "status": "MATCH", "supported": "Python", "evidence_ids": []}]})
    assert result.missing_requirements == [] and result.fatal_gaps == []


def test_the_requirements_panel_counts_and_ties_the_alternatives(user, parsed_job):  # noqa: F811
    job_id = parsed_job()
    data = client.get(f"/api/jobs/{job_id}/structured-description").json()
    assert data["counts"]["REQUIRED"] == 3, "the seven languages are one requirement, beside Kafka and Spark"
    tied = [item["value"] for item in data["requirements"] if item.get("alternative_group")]
    assert tied == LANGUAGES
