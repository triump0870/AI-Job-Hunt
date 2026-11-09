"""Golden prompt fixtures (tests/fixtures/prompt_golden): one small, fictional case per LLM worker.

Each fixture holds the builder's input, facts that must and must not reach the prompt, a valid answer, invalid
answers with the diagnosis they must get, and claims a generated answer must never make. They are used by
tests/test_prompt_golden.py (deterministic: schema, facts, evidence, prohibited claims, classifications — never exact
prose) and by scripts/evaluate_prompts.py (live models, to compare models or prompt versions).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.prompts import (
    build_application_email_match_prompt,
    build_application_form_prompt,
    build_application_inbox_prompt,
    build_application_question_prompt,
    build_browser_navigation_prompt,
    build_company_insights_prompt,
    build_cover_letter_prompt,
    build_domain_classifier_prompt,
    build_followup_prompt,
    build_inbox_classification_prompt,
    build_interview_insights_prompt,
    build_job_description_parser_prompt,
    build_job_description_recovery_prompt,
    build_ranking_prompt,
    build_resume_parser_prompt,
    build_review_prompt,
    build_tailoring_prompt,
)
from app.prompts.common import Prompt

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "prompt_golden"


def shared() -> dict[str, Any]:
    return json.loads((FIXTURES / "_shared.json").read_text())


def names() -> list[str]:
    return sorted(p.stem for p in FIXTURES.glob("*.json") if not p.stem.startswith("_"))


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def build(fixture: dict[str, Any]) -> Prompt:
    """The production prompt for a fixture, built by the worker's real builder."""
    base, given = shared(), fixture["input"]
    profile, job, evidence = SimpleNamespace(**base["profile"]), SimpleNamespace(**base["job"]), base["evidence"]
    name = fixture["prompt"]
    if name == "domain_classifier":
        return build_domain_classifier_prompt(given["candidate"], SimpleNamespace(title=given["title"], company=given["company"]),
                                              given["posting"])
    if name == "ranking":
        return build_ranking_prompt(candidate_profile=profile, resume=given["resume"], evidence=evidence, job=job)
    if name == "tailoring":
        return build_tailoring_prompt(profile, job, {**given["context"], "relevant_evidence": evidence})
    if name == "cover_letter":
        return build_cover_letter_prompt({**given["context"], "relevant_evidence": evidence,
                                          "job": {**base["job"], "description": job.description}})
    if name == "review":
        return build_review_prompt(candidate_profile=profile, evidence=evidence, job=job,
                                   tailored_resume=given["tailored_resume"], tailored_claims=given["tailored_claims"])
    if name == "resume_parser":
        return build_resume_parser_prompt(given["resume_text"])
    if name == "job_description_parser":
        return build_job_description_parser_prompt(job, job.description)
    if name == "job_description_recovery":
        return build_job_description_recovery_prompt(job, given["blocks"], page_title=given.get("page_title"))
    if name == "application_question":
        return build_application_question_prompt(given["question"], given["context"])
    if name == "inbox":
        return build_inbox_classification_prompt(given["sender"], given["subject"], given["body"],
                                                 given["known_applications"])
    if name == "application_inbox":
        return build_application_inbox_prompt(given["application"], given["email"], rule_hint=given.get("rule_hint"))
    if name == "application_email_match":
        return build_application_email_match_prompt(given["candidates"], given["email"])
    if name == "followup":
        return build_followup_prompt(profile, job=job, application=given["application"],
                                     recruiters=given["recruiters"], resume=given["resume"], evidence=evidence)
    if name == "company_insights":
        return build_company_insights_prompt({"title": job.title, "company": job.company, "location": job.location},
                                             given["observations"])
    if name == "interview_insights":
        return build_interview_insights_prompt(job={"title": job.title, "company": job.company},
                                               context=given["context"], fit_analysis=given["fit_analysis"],
                                               evidence=evidence, research=given["research"], stage=given["stage"])
    if name == "application_form":
        return build_application_form_prompt(job={"title": job.title, "company": job.company},
                                             context=given["context"], fit_analysis=given["fit_analysis"],
                                             evidence=evidence, knowledge=given["knowledge"], company=given["company"],
                                             form_questions=given["form_questions"])
    if name == "browser_navigation":
        return build_browser_navigation_prompt(job={"title": "Staff Platform Engineer", "company": job.company},
                                               fields=given["fields"], files=given["files"],
                                               elements=given["elements"], page_text=given["page_text"],
                                               history=given["history"], step=given["step"],
                                               max_steps=given["max_steps"])
    raise KeyError(f"no builder for golden prompt {name}")


def generated_text(output: Any) -> str:
    """Every string in an answer, for prohibited-claim checks."""
    if isinstance(output, dict):
        return " ".join(generated_text(v) for v in output.values())
    if isinstance(output, list):
        return " ".join(generated_text(v) for v in output)
    return output if isinstance(output, str) else ""


def prohibited_claims(fixture: dict[str, Any], output: Any) -> list[str]:
    text = generated_text(output).lower()
    return [claim for claim in fixture.get("prohibited_claims", []) if claim.lower() in text]


def cited_evidence(output: Any) -> set[int]:
    """Every evidence id an answer cites, wherever it cites it."""
    found: set[int] = set()
    if isinstance(output, dict):
        for key, value in output.items():
            if key in ("evidence_ids", "selected_evidence_ids", "relevant_evidence_ids") and isinstance(value, list):
                found |= {v for v in value if isinstance(v, int)}
            else:
                found |= cited_evidence(value)
    elif isinstance(output, list):
        for value in output:
            found |= cited_evidence(value)
    return found


def known_evidence() -> set[int]:
    return {item["id"] for item in shared()["evidence"]}
