"""Cover letter: job + company + verified facts + the candidate's own knowledge answers → a letter they could send.

The letter is written in the candidate's voice ("I"), from verified facts only. It is generated apart from the
resume so that the knowledge base (notice period, work authorization, relocation) reaches the letter and never
the resume rewriting, and so the letter gets a prompt of its own instead of one line in a larger schema.
Output contract: app.prompts.contracts.CoverLetterResult.
"""
from __future__ import annotations

from typing import Any

from app.prompts.common import (
    GAPS_STAY_GAPS_RULE,
    JSON_ONLY_RULE,
    NO_INVENTION_RULE,
    REQUIREMENT_LEVELS_RULE,
    UNTRUSTED_DATA_RULE,
    Prompt,
    compact,
    context_value,
    instructions,
    job_sections,
    job_structured,
    resume_facts,
    section,
    to_json,
    untrusted,
    user_message,
)
from app.prompts.contracts import CoverLetterResult, describe_fields

# v6: written against the parsed posting (JOB REQUIREMENTS, REQUIREMENT MAP) and beside the tailored resume; the
# posting's own terms the facts support (KEYWORD TARGETS) and never the ones they do not.
# v5: evidence ids go in selected_evidence_ids only, never in the letter text.
# v4: verified resume facts are application data, not untrusted text; each fact is sent once.
# v3: about a third shorter (one shape paragraph, one avoid list), no personal name in the instructions.
# v2: typed output contract; gaps stay gaps; only the candidate fields a letter uses; the FitAnalysis only when the
# Ranker completed one; the candidate policy applied before the prompt instead of sent for the model to reason about.
# requirements with "alternatives" (one of Python, Java or Go): any one of them meets it.
COVER_LETTER_PROMPT_VERSION = "v7"
COVER_LETTER_MARKER = "COVER_LETTER"
CANDIDATE_FIELDS = ("name", "headline", "location", "work_authorization")
JOB_FIELDS = ("identity", "role_summary", "responsibilities", "required", "preferred", "technical_domains")
# With parsed requirements the raw posting is only read for what it says about the company and team.
PARSED_POSTING_LIMIT = 8000

COVER_LETTER_SYSTEM = instructions(
    "You write one cover letter, as the candidate, for the job in this message.",
    UNTRUSTED_DATA_RULE,
    REQUIREMENT_LEVELS_RULE,
    NO_INVENTION_RULE + " Every fact, number and outcome comes from VERIFIED RESUME FACTS, VERIFIED EVIDENCE or "
    "KNOWN ANSWERS, and everything about the company from JOB and JOB DESCRIPTION. The writing can be natural; the "
    "truth constraints do not relax.",
    GAPS_STAY_GAPS_RULE,
    """Write in the first person ("I led", "my team"): never the third person, never the candidate's name in the
body. One honest letter, not a profile.
SHAPE: "Dear Hiring Team," (or the first recruiter in RECRUITERS by name). Paragraph 1: the exact role title, what
the role is for (JOB REQUIREMENTS.role_summary and its first responsibilities) and a concrete reason from the
posting. Paragraphs 2-3: each takes one REQUIRED row of REQUIREMENT MAP (the first rows are the strongest) and proves
it with the facts its evidence_ids name, with their real number or scope, in fresh words rather than resume bullets;
they tell the same story as TAILORED RESUME. Use three to five KEYWORD TARGETS "use" terms where they fit, spelled as
given, and never a "do_not_use" term. Paragraph 4: what they bring, anything from KNOWN
ANSWERS the posting raises (work authorization, notice period, relocation, remote), a plain close. Then "Sincerely,"
and the name from CANDIDATE PROFILE. 250-300 words, plain ASCII, no markdown, lists, headings, em dashes or
placeholders such as [Company]. Never cite evidence
ids, fact ids or sources in the letter ("(evidence id 24)", "[E12]"): ids go only in selected_evidence_ids.
AVOID: "passionate about", "thrilled", "I am writing to express my interest" and other cliches; listing technologies;
company claims the posting does not make; salary or visa details it never raises; drawing attention to anything the
candidate lacks.""",
    JSON_ONLY_RULE,
    describe_fields(CoverLetterResult),
)


def build_cover_letter_prompt(context: Any, recruiters: list[dict[str, str]] | None = None) -> Prompt:
    """Build from a cover letter context (app.services.context_builder.build_cover_letter_context)."""
    candidate = context_value(context, "candidate", {}) or {}
    job = context_value(context, "job", {}) or {}
    knowledge = context_value(context, "knowledge", [])
    tailored = context_value(context, "tailored") or {}
    requirements = context_value(context, "requirement_map") or []
    keywords = context_value(context, "keyword_targets") or {}
    fit = compact({key: (context_value(context, "fit_analysis") or {}).get(key)
                   for key in ("strong_matches", "recommended_resume_focus")})
    user = user_message(
        COVER_LETTER_MARKER,
        section("CANDIDATE PROFILE", to_json(compact({k: candidate.get(k) for k in CANDIDATE_FIELDS}))),
        section("VERIFIED RESUME FACTS", to_json(resume_facts(context), limit=16000)),
        section("VERIFIED EVIDENCE", to_json(context_value(context, "relevant_evidence", []), limit=16000)),
        section("KNOWN ANSWERS", to_json(knowledge)) if knowledge else "",
        section("TAILORED RESUME (validated; the resume this letter goes with)", to_json(tailored)) if tailored else "",
        section("JOB", to_json({k: v for k, v in job.items() if k != "description"})),
        *job_sections(context, fields=JOB_FIELDS),
        untrusted("JOB DESCRIPTION", "untrusted_job_description", job.get("description", ""),
                  limit=PARSED_POSTING_LIMIT if job_structured(context) else 24000),
        section("REQUIREMENT MAP (the Ranker's matched requirements and the ids that prove them)",
                to_json(requirements)) if requirements else "",
        section("KEYWORD TARGETS (the posting's terms, checked against the candidate's facts)",
                to_json(keywords)) if keywords else "",
        section("FIT ANALYSIS", to_json(fit)) if fit else "",
        section("RECRUITERS (the first is the primary contact)", to_json(recruiters)) if recruiters else "",
    )
    return Prompt("cover_letter", COVER_LETTER_PROMPT_VERSION, COVER_LETTER_MARKER, COVER_LETTER_SYSTEM, user,
                  CoverLetterResult)
