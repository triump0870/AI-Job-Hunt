"""Every production LLM prompt lives in this package.

Each prompt module has a builder that returns a provider-independent `Prompt` (trusted instructions in `system`,
dynamic data in `user`, the worker's output contract in `output_schema`) and a version constant that services store
with the result. Services must not define prompts inline. Output contracts live in app.prompts.contracts;
OUTPUT_CONTRACTS maps each machine-facing prompt to its contract. Conventions: docs/prompts.md.
"""
from app.prompts.agents import AGENT_PROMPT_VERSION
from app.prompts.ai_optimization import AI_OPTIMIZATION_PROMPT_VERSION, build_ai_optimization_prompt
from app.prompts.application import (
                                       APPLICATION_QUESTION_PROMPT_VERSION,
                                       build_application_question_prompt,
)
from app.prompts.application_form import APPLICATION_FORM_PROMPT_VERSION, build_application_form_prompt
from app.prompts.application_inbox import (
                                       APPLICATION_INBOX_PROMPT_VERSION,
                                       build_application_email_match_prompt,
                                       build_application_inbox_prompt,
)
from app.prompts.browser_navigation import BROWSER_NAVIGATION_PROMPT_VERSION, build_browser_navigation_prompt
from app.prompts.common import Prompt
from app.prompts.company_insights import (
                                       COMPANY_INSIGHTS_PROMPT_VERSION,
                                       build_company_insights_prompt,
)
from app.prompts.contracts import (
                                       ApplicationAnswerResult,
                                       ApplicationEmailAnalysis,
                                       ApplicationFormPrepResult,
                                       ApplicationEmailMatchResult,
                                       BrowserActionProposal,
                                       CompanyInsightSynthesis,
                                       CoverLetterResult,
                                       EvidenceExtractionResult,
                                       FollowUpResult,
                                       InboxClassificationResult,
                                       InterviewPrepResult,
                                       JobDescriptionParserOutput,
                                       DomainClassificationResult,
                                       JobDescriptionRecoveryResult,
                                       OptimizationReport,
                                       RankingResult,
                                       ResumeParserOutput,
                                       ReviewResult,
                                       TailoringResult,
)
from app.prompts.cover_letter import COVER_LETTER_PROMPT_VERSION, build_cover_letter_prompt
from app.prompts.followup import FOLLOWUP_PROMPT_VERSION, build_followup_prompt
from app.prompts.inbox import (
                                       INBOX_CATEGORIES,
                                       INBOX_PROMPT_VERSION,
                                       build_inbox_classification_prompt,
)
from app.prompts.interview_insights import (
                                       INTERVIEW_INSIGHTS_PROMPT_VERSION,
                                       build_interview_insights_prompt,
)
from app.prompts.job_description_parser import (
                                       JOB_DESCRIPTION_PARSER_PROMPT_VERSION,
                                       build_job_description_parser_prompt,
)
from app.prompts.domain_classifier import DOMAIN_CLASSIFIER_PROMPT_VERSION, build_domain_classifier_prompt
from app.prompts.job_description_recovery import (
                                       JOB_DESCRIPTION_RECOVERY_PROMPT_VERSION,
                                       build_job_description_recovery_prompt,
)
from app.prompts.mcp_application import MCP_APPLICATION_PROMPT_VERSION
from app.prompts.mcp_followup import MCP_FOLLOWUP_PROMPT_VERSION
from app.prompts.mcp_review import MCP_REVIEW_PROMPT_VERSION
from app.prompts.mcp_tailoring import MCP_TAILORING_PROMPT_VERSION
from app.prompts.model_tests import (
                                       MODEL_TEST_PROMPT_VERSION,
                                       build_model_health_prompt,
                                       build_worker_test_prompt,
)
from app.prompts.ranking import RANKING_PROMPT_VERSION, build_ranking_prompt
from app.prompts.resume_parser import (
                                       EVIDENCE_EXTRACTION_PROMPT_VERSION,
                                       RESUME_PARSER_PROMPT_VERSION,
                                       build_evidence_extraction_prompt,
                                       build_resume_parser_prompt,
)
from app.prompts.review import REVIEW_PROMPT_VERSION, build_review_prompt
from app.prompts.tailoring import TAILOR_PROMPT_VERSION, build_tailoring_prompt

PROMPT_VERSIONS = {
    "ranking": RANKING_PROMPT_VERSION,
    "tailoring": TAILOR_PROMPT_VERSION,
    "cover_letter": COVER_LETTER_PROMPT_VERSION,
    "review": REVIEW_PROMPT_VERSION,
    "followup": FOLLOWUP_PROMPT_VERSION,
    "resume_parser": RESUME_PARSER_PROMPT_VERSION,
    "job_description_parser": JOB_DESCRIPTION_PARSER_PROMPT_VERSION,
    "job_description_recovery": JOB_DESCRIPTION_RECOVERY_PROMPT_VERSION,
    "domain_classifier": DOMAIN_CLASSIFIER_PROMPT_VERSION,
    "evidence_extraction": EVIDENCE_EXTRACTION_PROMPT_VERSION,
    "application_question": APPLICATION_QUESTION_PROMPT_VERSION,
    "application_form": APPLICATION_FORM_PROMPT_VERSION,
    "inbox": INBOX_PROMPT_VERSION,
    "application_inbox": APPLICATION_INBOX_PROMPT_VERSION,
    "application_email_match": APPLICATION_INBOX_PROMPT_VERSION,
    "agents": AGENT_PROMPT_VERSION,
    "ai_optimization": AI_OPTIMIZATION_PROMPT_VERSION,
    "company_insights": COMPANY_INSIGHTS_PROMPT_VERSION,
    "interview_insights": INTERVIEW_INSIGHTS_PROMPT_VERSION,
    "browser_navigation": BROWSER_NAVIGATION_PROMPT_VERSION,
    "mcp_tailoring": MCP_TAILORING_PROMPT_VERSION,
    "mcp_review": MCP_REVIEW_PROMPT_VERSION,
    "mcp_application": MCP_APPLICATION_PROMPT_VERSION,
    "mcp_followup": MCP_FOLLOWUP_PROMPT_VERSION,
    "model_health": MODEL_TEST_PROMPT_VERSION,
    "worker_test": MODEL_TEST_PROMPT_VERSION,
}

# Prompt name → the output contract its answers must satisfy (app.services.llm checks every answer against it).
OUTPUT_CONTRACTS = {
    "ranking": RankingResult,
    "tailoring": TailoringResult,
    "review": ReviewResult,
    "cover_letter": CoverLetterResult,
    "followup": FollowUpResult,
    "resume_parser": ResumeParserOutput,
    "job_description_parser": JobDescriptionParserOutput,
    "job_description_recovery": JobDescriptionRecoveryResult,
    "domain_classifier": DomainClassificationResult,
    "evidence_extraction": EvidenceExtractionResult,
    "application_question": ApplicationAnswerResult,
    "application_form": ApplicationFormPrepResult,
    "inbox": InboxClassificationResult,
    "application_inbox": ApplicationEmailAnalysis,
    "application_email_match": ApplicationEmailMatchResult,
    "ai_optimization": OptimizationReport,
    "company_insights": CompanyInsightSynthesis,
    "interview_insights": InterviewPrepResult,
    "browser_navigation": BrowserActionProposal,
}
# Worker prompt names reserved in app.services.ai_workers with no production prompt yet (nothing calls them).
RESERVED_PROMPTS: tuple[str, ...] = ()
# Conversation instructions (Claude Desktop over MCP, the ADK agents): not model calls the backend makes. Their saved
# results are validated by the backend (app.services.tailoring_results, review_results, job_hunt_agent.tools).
CONVERSATION_PROMPTS = ("agents", "mcp_tailoring", "mcp_review", "mcp_application", "mcp_followup")

__all__ = [
                                       "CONVERSATION_PROMPTS",
                                       "INBOX_CATEGORIES",
                                       "OUTPUT_CONTRACTS",
                                       "PROMPT_VERSIONS",
                                       "RESERVED_PROMPTS",
                                       "Prompt",
                                       "build_ai_optimization_prompt",
                                       "build_application_email_match_prompt",
                                       "build_application_form_prompt",
                                       "build_application_inbox_prompt",
                                       "build_application_question_prompt",
                                       "build_browser_navigation_prompt",
                                       "build_company_insights_prompt",
                                       "build_cover_letter_prompt",
                                       "build_domain_classifier_prompt",
                                       "build_evidence_extraction_prompt",
                                       "build_followup_prompt",
                                       "build_inbox_classification_prompt",
                                       "build_interview_insights_prompt",
                                       "build_job_description_parser_prompt",
                                       "build_job_description_recovery_prompt",
                                       "build_model_health_prompt",
                                       "build_ranking_prompt",
                                       "build_resume_parser_prompt",
                                       "build_review_prompt",
                                       "build_tailoring_prompt",
                                       "build_worker_test_prompt",
]
