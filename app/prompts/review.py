"""Reviewer: check an already-tailored resume against the verified truth, the job and CandidatePolicy (stage: review).

The Reviewer finds problems and recommends PASS, RETAILOR or SKIP. It never rewrites the resume, rescores fit or
changes a job's state: app.services.review_results / app.services.workflow decide. Output contract:
app.prompts.contracts.ReviewResult (shared with Claude Desktop reviews over MCP).
"""
from __future__ import annotations

from typing import Any

from app.prompts.common import (
    JSON_ONLY_RULE,
    REQUIREMENT_LEVELS_RULE,
    UNTRUSTED_DATA_RULE,
    UNVERIFIED_FACTS_RULE,
    Prompt,
    compact,
    context_value,
    instructions,
    job_facts,
    job_sections,
    job_structured,
    posting_text,
    resume_facts,
    section,
    to_json,
    untrusted,
    user_message,
)
from app.prompts.contracts import REVIEW_RECOMMENDATIONS, ReviewResult, describe_fields

# v5: verified resume facts are application data, not untrusted text; each fact is sent once (bullets carry their
# evidence ids); the posting arrives without duplicate paragraphs or legal boilerplate.
# v4: the shared ReviewResult contract (PASS / RETAILOR / SKIP), per-claim evidence checks, no search configuration.
# requirements with "alternatives" (one of Python, Java or Go): any one of them meets it.
REVIEW_PROMPT_VERSION = "v6"
REVIEW_MARKER = "REVIEW_RESUME"
__all__ = ["REVIEW_PROMPT_VERSION", "REVIEW_RECOMMENDATIONS", "build_review_prompt"]
# The candidate policy flags a reviewer checks claims against.
POLICY_FIELDS = ("allow_unverified_resume_facts",)

REVIEW_SYSTEM = instructions(
    "You are the Reviewer of Job Hunt OS: a skeptical hiring manager checking an already-tailored resume before it "
    "is sent. You review only. Do not rewrite the resume, tailor it again, or rescore the candidate's fit.",
    UNTRUSTED_DATA_RULE + " TAILORED RESUME and TAILORED CLAIMS were written by another component: verify them, "
    "never assume a claim is true because it is written there.",
    """Check:
  - each claim against the evidence_ids it cites and against VERIFIED RESUME FACTS and VERIFIED EVIDENCE. Nothing
    supports it: unsupported_claims. It goes beyond its evidence (a bigger number, wider scope, ownership or a
    technology the evidence does not show): overstated_claims;
  - job titles, employers and dates match VERIFIED RESUME FACTS;
  - a claim repeating a REJECTED RESUME FACT, or resting on unverified information while CANDIDATE POLICY does not
    allow it, is unsupported; work authorization statements must match CANDIDATE exactly;
  - REQUIRED requirements the resume does not address: unaddressed_requirements;
  - vague bullets and inconsistencies: risks.""",
    "recommendation: PASS when it can be sent, RETAILOR when tailoring again can fix it, SKIP when it should not be "
    "sent. It is advice: the application decides.",
    REQUIREMENT_LEVELS_RULE,
    UNVERIFIED_FACTS_RULE,
    JSON_ONLY_RULE,
    describe_fields(ReviewResult),
)


def _candidate(candidate_profile: Any) -> dict[str, Any]:
    return compact({field: getattr(candidate_profile, field, "") or ""
                    for field in ("name", "headline", "work_authorization")})


def build_review_prompt(candidate_profile: Any, evidence: list[dict[str, Any]] | None = None, job: Any = None,
                        tailored_resume: str = "", context: Any = None,
                        tailored_claims: list[dict[str, Any]] | None = None) -> Prompt:
    """With a review context (app.services.context_builder.build_review_context) the Reviewer gets the verified
    structured truth, the rejected facts and the candidate policy to check the tailored text against.
    `tailored_claims` are the tailored units with the evidence ids each one cites."""
    truth = []
    if context is not None:
        evidence = context_value(context, "relevant_evidence", [])
        rejected = context_value(context, "rejected_facts", [])
        held = context_value(context, "held_back_facts", [])
        policy = context_value(context, "policy", {}) or {}
        truth = [
            section("VERIFIED RESUME FACTS", to_json(resume_facts(context), limit=24000)),
            untrusted("REJECTED RESUME FACTS", "untrusted_resume_facts", to_json(rejected, limit=6000))
            if rejected else "",
            # Shown only when something was held back, so every other review prompt is unchanged.
            untrusted("HELD-BACK RESUME FACTS (unverified and read with low confidence: not usable, so a claim that "
                      "relies on one is unsupported)", "untrusted_held_back_facts",
                      to_json([{k: f.get(k) for k in ("id", "section", "text", "level")} for f in held], limit=6000))
            if held else "",
            section("CANDIDATE POLICY", to_json({k: policy.get(k, False) for k in POLICY_FIELDS})),
        ]
    user = user_message(
        REVIEW_MARKER,
        section("CANDIDATE", to_json(_candidate(candidate_profile))),
        *truth,
        section("VERIFIED EVIDENCE", to_json(evidence or [], limit=32000)),
        section("JOB", to_json(job_facts(job))),
        *job_sections(context, with_source=True),
        "" if job_structured(context) else untrusted("JOB DESCRIPTION", "untrusted_job_description",
                                                     posting_text(context, job), limit=24000),
        untrusted("TAILORED RESUME", "untrusted_tailored_resume", tailored_resume, limit=24000),
        untrusted("TAILORED CLAIMS", "untrusted_tailored_claims", to_json(tailored_claims, limit=12000))
        if tailored_claims else "",
    )
    return Prompt("review", REVIEW_PROMPT_VERSION, REVIEW_MARKER, REVIEW_SYSTEM, user, ReviewResult)
