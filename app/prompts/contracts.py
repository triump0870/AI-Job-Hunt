"""Output contracts: the typed JSON each machine-facing worker must return.

A prompt names its contract (`Prompt.output_schema`). The LLM gateway (app.services.llm) then, for every provider:

* sends the contract as a JSON Schema when the model is known to support structured output (otherwise JSON mode, or
  nothing), and records which enforcement the request had;
* validates every answer against the contract before a service sees it. Valid JSON is not enough: contracts forbid
  fields they do not define, so an answer in another worker's shape (a Tailor answer carrying `candidate_fit_score`)
  fails, and is classified as POSSIBLE_CROSS_WORKER_OUTPUT rather than quietly accepted;
* retries once with a short schema correction, then fails. Missing fields are never inferred.

Contracts are deliberately non-overlapping: the Ranker's fields (scores, matches, gaps) exist only on RankingResult,
the Tailor's (a patch of the source resume) only on TailoringResult, the Reviewer's (claims found, a recommendation)
only on ReviewResult. The system prompts list fields with `describe_fields`, generated from these models, so a
prompt and its contract cannot drift apart.

Everything here is provider-independent: pydantic models and plain JSON Schema, no SDK.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

# Routing signals a model may add for quality escalation (app.services.model_resolution.AMBIGUITY_FIELDS). They are
# read from the raw answer by the gateway and are never part of a worker's result.
SIGNAL_FIELDS = frozenset({"ambiguous", "needs_escalation", "fatal_requirement_uncertain"})
MATCH_STATUSES = ("MATCH", "PARTIAL_MATCH", "MISSING", "UNKNOWN")
REQUIREMENT_LEVELS = ("REQUIRED", "PREFERRED", "INFORMATIONAL", "UNKNOWN")
QUALIFICATION_RECOMMENDATIONS = ("QUALIFY", "ARCHIVE")
REVIEW_RECOMMENDATIONS = ("PASS", "RETAILOR", "SKIP")
APPLICATION_DECISIONS = ("ANSWER", "UNKNOWN", "NEEDS_INPUT", "SKIP_APPLICATION")
INBOX_CATEGORIES = ("RECRUITER_RESPONSE", "INTERVIEW_INVITATION", "ASSESSMENT", "REJECTION", "OFFER",
                    "REQUEST_FOR_INFORMATION", "AUTOMATED_CONFIRMATION", "UNKNOWN")
RECOMMENDATION_CATEGORIES = ("COST", "LATENCY", "CONTEXT", "MODEL", "PROMPT", "RELIABILITY", "ARCHITECTURE",
                             "EXTERNAL_API")
RECOMMENDATION_PRIORITIES = ("HIGH", "MEDIUM", "LOW")
DOMAIN_CLASSIFICATIONS = ("MATCH", "NO_MATCH", "UNCERTAIN")
ADVISOR_STAGES = ("RANKER", "TAILOR", "REVIEWER", "RESUME_PARSER", "JD_PARSER", "JD_RECOVERY", "DOMAIN_CLASSIFIER", "APPLICATION", "FOLLOW_UP",
                  "INBOX", "COMPANY_INSIGHTS", "INTERVIEW_INSIGHTS", "ADVISOR", "OTHER", "ALL")


def _ids(values: Any) -> Any:
    """Evidence ids arrive as 12, "12" or "E12"; anything else is left for validation to reject."""
    if not isinstance(values, list):
        return values
    out = []
    for value in values:
        if isinstance(value, str) and re.fullmatch(r"\s*[Ee]?\d+\s*", value):
            value = int(value.strip().lstrip("Ee"))
        out.append(value)
    return list(dict.fromkeys(out)) if all(isinstance(v, int) for v in out) else out


def _upper(value: Any) -> Any:
    return value.strip().upper().replace(" ", "_").replace("-", "_") if isinstance(value, str) else value


def _phrase(value: Any) -> Any:
    """A short free-text field as models actually send it: null or "N/A" is empty, a list of phrases is one phrase.
    Anything else is left for validation to reject."""
    if value is None:
        return ""
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        value = "; ".join(v.strip() for v in value if v.strip())
    if isinstance(value, str) and value.strip().upper().strip(".") in ("N/A", "NA", "NONE", "-"):
        return ""
    return value


def _text_id(value: Any) -> Any:
    """An id copied from the input: 7 and "7" are the same id."""
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


def _clean_strings(values: Any) -> Any:
    return [v.strip() for v in values if isinstance(v, str) and v.strip()] if isinstance(values, list) else values


EvidenceIds = Annotated[list[int], BeforeValidator(_ids), Field(default_factory=list, max_length=20)]
Score = Annotated[float, Field(ge=0, le=100)]
Confidence = Annotated[float, Field(ge=0, le=1)]
Phrases = Annotated[list[str], BeforeValidator(_clean_strings), Field(default_factory=list, max_length=40)]


class Contract(BaseModel):
    """A worker's output. Unknown fields are an error, never ignored."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    worker: ClassVar[str] = ""


# ───────────── Ranker ─────────────

class RequirementAssessment(Contract):
    requirement: str = Field(min_length=1, max_length=300, description="the requirement as the posting states it")
    requirement_id: Annotated[str | None, BeforeValidator(_text_id)] = Field(
        default=None, max_length=60, description="its id from JOB REQUIREMENTS, or null")
    level: Annotated[Literal["REQUIRED", "PREFERRED", "INFORMATIONAL", "UNKNOWN"], BeforeValidator(_upper)] = Field(
        default="UNKNOWN", description="copied from JOB REQUIREMENTS, never reclassified")
    status: Annotated[Literal["MATCH", "PARTIAL_MATCH", "MISSING", "UNKNOWN"], BeforeValidator(_upper)] = Field(
        description="MATCH fully supported; PARTIAL_MATCH part supported; MISSING the facts show it is absent; "
                    "UNKNOWN the facts cannot tell")
    supported: Annotated[str, BeforeValidator(_phrase)] = Field(
        default="", max_length=400, description="what the candidate's facts do support")
    unsupported: Annotated[str, BeforeValidator(_phrase)] = Field(
        default="", max_length=400, description="what they do not support")
    evidence_ids: EvidenceIds = Field(description="VERIFIED EVIDENCE ids behind `supported`")
    fatal: bool = Field(default=False, description="true only for an explicit hard requirement that is MISSING")

    @model_validator(mode="after")
    def _only_missing_is_fatal(self) -> RequirementAssessment:
        """The rule the prompt states, applied by code: only a MISSING requirement can be fatal (unknown lowers
        confidence instead). A model's fatal flag on anything else would archive a job by itself."""
        if self.status != "MISSING":
            self.fatal = False
        return self


class RankingResult(Contract):
    worker: ClassVar[str] = "RANKER"

    # The application knows which job it asked about (app.services.ranker.validate_output fills a missing one); a
    # different id is still rejected there.
    job_id: int | None = Field(default=None, description="the JOB ID given in the input")
    overall_fit_score: Score
    confidence: Confidence = Field(description="how complete the posting and the candidate's facts are")
    candidate_fit_score: Score = Field(description="does the person fit, from all their facts")
    resume_fit_score: Score = Field(description="does the ACTIVE RESUME, as written, show that fit")
    seniority_match_score: Score
    skills_match_score: Score
    leadership_match_score: Score
    domain_match_score: Score
    location_match_score: Score = Field(description="location, remote policy and work authorization")
    requirement_assessments: list[RequirementAssessment] = Field(
        default_factory=list, max_length=60, description="one entry per requirement you assessed")
    strong_matches: Phrases = Field(description="derived from MATCH assessments when omitted")
    partial_matches: Phrases = Field(description="derived from PARTIAL_MATCH assessments when omitted")
    missing_requirements: Phrases = Field(description="derived from MISSING assessments when omitted")
    fatal_gaps: Phrases = Field(description="explicit hard requirements the facts show are not met")
    resume_gaps: Phrases = Field(description="supported facts the resume does not show")
    relevant_evidence_ids: EvidenceIds = Field(description="only ids that appear in VERIFIED EVIDENCE")
    recommended_resume_focus: Phrases = Field(description="what the resume should lead with")
    qualification_recommendation: Annotated[str, BeforeValidator(_upper)] = Field(
        description=f"one of {', '.join(QUALIFICATION_RECOMMENDATIONS)}")
    reasoning_summary: str = Field(min_length=1, max_length=2000, description="two or three sentences")

    @field_validator("qualification_recommendation")
    @classmethod
    def _recommendation(cls, value: str) -> str:
        if value not in QUALIFICATION_RECOMMENDATIONS:
            raise ValueError(f"must be one of {', '.join(QUALIFICATION_RECOMMENDATIONS)}")
        return value

    @model_validator(mode="after")
    def _derive_lists(self) -> RankingResult:
        """The match lists are a projection of the assessments. A list the model already wrote is kept."""
        by_status: dict[str, list[RequirementAssessment]] = {s: [] for s in MATCH_STATUSES}
        for item in self.requirement_assessments:
            by_status[item.status].append(item)
        # A model sometimes lists a requirement by its JOB REQUIREMENTS id ("req_003"); people read these lists, so
        # an id it also assessed is replaced by that requirement's text.
        names = {a.requirement_id: a.requirement for a in self.requirement_assessments if a.requirement_id}
        for field in ("strong_matches", "partial_matches", "missing_requirements", "fatal_gaps", "resume_gaps"):
            setattr(self, field, [names.get(value, value) for value in getattr(self, field)])
        if not self.strong_matches:
            self.strong_matches = [a.requirement for a in by_status["MATCH"]]
        if not self.partial_matches:
            self.partial_matches = [
                "; ".join(filter(None, [a.requirement, f"supported: {a.supported}" if a.supported else "",
                                        f"not shown: {a.unsupported}" if a.unsupported else ""]))
                for a in by_status["PARTIAL_MATCH"]]
        if not self.missing_requirements:
            self.missing_requirements = [a.requirement for a in by_status["MISSING"] if not a.fatal]
        if self.requirement_assessments or not self.fatal_gaps:
            # Fatal gaps decide archiving (app.services.ranker.qualify), so with assessments they are what the
            # assessments show — MISSING and fatal — never a list the model wrote alongside them.
            self.fatal_gaps = [a.requirement for a in by_status["MISSING"] if a.fatal]
        return self


# ───────────── Tailor ─────────────

# A tailored resume cites a fact per bullet, so its id lists are longer than one assessment's: a summary drawing on
# a whole career, or seven roles of bullets, cites more than 20 distinct facts. The limits are sanity bounds only.
ClaimEvidenceIds = Annotated[list[int], BeforeValidator(_ids), Field(default_factory=list, max_length=40)]
PatchEvidenceIds = Annotated[list[int], BeforeValidator(_ids), Field(default_factory=list, max_length=200)]


class Claim(Contract):
    """One generated factual unit and the evidence that supports it."""

    text: str = Field(min_length=1, max_length=600)
    evidence_ids: ClaimEvidenceIds = Field(description="ids of the source bullets or RELEVANT EVIDENCE it rests on")


class SummaryClaim(Claim):
    """The rewritten summary: a few sentences, so longer than a bullet (the stored summary allows 1500
    characters, app.services.tailoring_results.TailoredResumeIn)."""

    text: str = Field(min_length=1, max_length=1500, description="at most 1500 characters")


class RoleUpdate(Contract):
    role_index: int = Field(ge=0, description="a role_index from SOURCE RESUME")
    bullets: list[Claim] = Field(min_length=1, max_length=8,
                                 description="at most this role's max_bullets (LENGTH PLAN), most relevant first")


class TailoringResult(Contract):
    """A patch applied to the source resume by a deterministic renderer; never a whole resume."""

    worker: ClassVar[str] = "TAILOR"

    summary: SummaryClaim | None = Field(default=None, description="null keeps the source summary")
    skills_order: Phrases = Field(description="skills copied from SOURCE RESUME, most relevant first")
    role_updates: list[RoleUpdate] = Field(default_factory=list, max_length=20,
                                           description="rewritten bullets for SOURCE RESUME roles")
    highlights: list[Claim] = Field(default_factory=list, max_length=30,
                                    description="only when SOURCE RESUME has no roles")
    # Derived from the units above (app.services.tailoring_results stores the ids of the units it keeps); what a
    # model lists here is added, never required, so a long resume is not rejected for citing many facts.
    selected_evidence_ids: PatchEvidenceIds = Field(
        description="optional; every evidence id used anywhere above, derived from them when omitted")

    @field_validator("role_updates", "highlights", mode="before")
    @classmethod
    def _null_is_nothing(cls, value: Any) -> Any:
        """Models answer null for "nothing here" (as they do for summary). That means no rewritten roles or
        highlights, exactly what an empty list says; it adds nothing, so it is accepted rather than failing a good
        answer. The not-empty check below still rejects a result that changes nothing."""
        return [] if value is None else value

    @field_validator("role_updates", mode="before")
    @classmethod
    def _no_bullets_is_no_update(cls, value: Any) -> Any:
        """The prompt tells the model to return no bullets for a role with max_bullets 0, and models do so as
        {"role_index": 3, "bullets": []}. A role without bullets is no update (the renderer skips it either way), so
        it is left out rather than failing the whole answer."""
        if not isinstance(value, list):
            return value
        return [u for u in value if not (isinstance(u, dict) and "bullets" in u and not u["bullets"])]

    @model_validator(mode="after")
    def _not_empty(self) -> TailoringResult:
        if self.summary is None and not self.skills_order and not self.role_updates and not self.highlights:
            raise ValueError("a tailoring result must change at least one of summary, skills_order, role_updates "
                             "or highlights")
        return self

    @model_validator(mode="after")
    def _derive_selected(self) -> TailoringResult:
        self.selected_evidence_ids = sorted(self.cited_ids())
        return self

    def cited_ids(self) -> set[int]:
        units = ([self.summary] if self.summary else []) + self.highlights + [
            b for role in self.role_updates for b in role.bullets]
        return {i for unit in units for i in unit.evidence_ids} | set(self.selected_evidence_ids)


# ───────────── Reviewer ─────────────

class ReviewResult(Contract):
    worker: ClassVar[str] = "REVIEWER"

    review_score: Score
    confidence: Confidence
    unsupported_claims: Phrases = Field(description="claims no VERIFIED fact or evidence supports")
    overstated_claims: Phrases = Field(description="claims that go beyond what their evidence says")
    unaddressed_requirements: Phrases = Field(description="REQUIRED requirements the resume does not address")
    risks: Phrases = Field(description="policy, consistency or presentation concerns")
    strengths: Phrases = Field(description="what the resume does well for this job")
    recommendation: Annotated[Literal["PASS", "RETAILOR", "SKIP"], BeforeValidator(_upper)] = Field(
        description="advice only: the application decides the job's state")
    summary: str = Field(default="", max_length=2000, description="one or two sentences")


# ───────────── writing workers ─────────────

class CoverLetterResult(Contract):
    worker: ClassVar[str] = "COVER_LETTER"

    cover_letter: str = Field(min_length=1, max_length=8000, description='the whole letter, "\\n\\n" between paragraphs')
    selected_evidence_ids: EvidenceIds = Field(description="the VERIFIED EVIDENCE ids the letter relies on")


class FollowUpResult(Contract):
    worker: ClassVar[str] = "FOLLOWUP"

    draft: str = Field(min_length=1, max_length=4000, description="the message, ready for the candidate to edit")
    evidence_ids: EvidenceIds = Field(description="VERIFIED EVIDENCE ids the draft mentions, if any")


class ApplicationAnswerResult(Contract):
    worker: ClassVar[str] = "APPLICATION_ANSWER"

    decision: Annotated[Literal["ANSWER", "UNKNOWN", "NEEDS_INPUT", "SKIP_APPLICATION"], BeforeValidator(_upper)] = \
        Field(description=f"one of {', '.join(APPLICATION_DECISIONS)}")
    answer: str | None = Field(default=None, max_length=4000, description="null unless decision is ANSWER")
    sources: Annotated[list[Literal["profile", "policy", "knowledge", "resume", "evidence"]],
                       BeforeValidator(lambda v: [s.strip().lower() for s in v] if isinstance(v, list) else v)] = \
        Field(default_factory=list, description="which of profile, policy, knowledge, resume, evidence it came from")
    evidence_ids: EvidenceIds
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _answer_only_when_answering(self) -> ApplicationAnswerResult:
        if self.decision == "ANSWER" and not (self.answer or "").strip():
            raise ValueError("decision ANSWER needs an answer")
        if self.decision != "ANSWER" and self.answer:
            raise ValueError("answer must be null unless decision is ANSWER")
        return self


class InboxClassificationResult(Contract):
    worker: ClassVar[str] = "INBOX_CLASSIFIER"

    category: Annotated[str, BeforeValidator(_upper)] = Field(description="one of the categories above")
    confidence: Confidence
    application_id: int | None = Field(default=None, description="a KNOWN APPLICATIONS id, or null")
    company: str | None = Field(default=None, max_length=300)
    role: str | None = Field(default=None, max_length=300)
    summary: str = Field(min_length=1, max_length=500, description="one sentence")
    action_required: bool
    deadline: str | None = Field(default=None, max_length=40, description="ISO 8601 date or datetime, or null")

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        if value not in INBOX_CATEGORIES:
            raise ValueError(f"must be one of {', '.join(INBOX_CATEGORIES)}")
        return value


# ───────────── Application Inbox (app.services.application_inbox) ─────────────

APPLICATION_EMAIL_EVENT_TYPES = (
    "APPLICATION_CONFIRMATION", "RECRUITER_RESPONSE", "INTERVIEW_INVITATION", "INTERVIEW_RESCHEDULE",
    "ASSESSMENT_INVITATION", "ASSESSMENT_REMINDER", "ADDITIONAL_INFORMATION_REQUEST", "DOCUMENT_REQUEST",
    "FOLLOWUP_RESPONSE", "APPLICATION_ON_HOLD", "APPLICATION_REJECTION", "OFFER", "OFFER_UPDATE", "BACKGROUND_CHECK",
    "ONBOARDING", "GENERAL_RECRUITING_UPDATE", "AUTOMATED_NOTIFICATION", "UNKNOWN")
# What the email says about where the application stands. The tracker's own stages are decided by code from this.
APPLICATION_STAGES_DETECTED = (
    "APPLIED", "RECRUITER_SCREEN", "ASSESSMENT", "HIRING_MANAGER", "TECHNICAL_INTERVIEW", "SYSTEM_DESIGN",
    "BEHAVIORAL", "FINAL_INTERVIEW", "HR_DISCUSSION", "OFFER", "ON_HOLD", "REJECTED", "UNKNOWN")
# A recommendation only: app.services.application_inbox.stage_policy validates it and decides.
TRACKER_ACTIONS = ("NO_CHANGE", "MOVE_TO_ACKNOWLEDGED", "MOVE_TO_SCREENING", "MOVE_TO_INTERVIEWING",
                   "MOVE_TO_TAKEHOME", "MOVE_TO_OFFER", "MOVE_TO_ON_HOLD", "MOVE_TO_REJECTED")
# Interview-round names a model may use for the action; all of them are the tracker's Interviewing column.
_INTERVIEW_ACTIONS = {"MOVE_TO_" + s for s in ("TECHNICAL_INTERVIEW", "SYSTEM_DESIGN", "BEHAVIORAL", "HIRING_MANAGER",
                                                "FINAL_INTERVIEW", "HR_DISCUSSION", "INTERVIEW")}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})?$")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_OFFSET = re.compile(r"^(UTC)?[+-](0\d|1[0-4]):[0-5]\d$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _tracker_action(value: Any) -> Any:
    value = _upper(value)
    return "MOVE_TO_INTERVIEWING" if value in _INTERVIEW_ACTIONS else value


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and (not value.strip() or value.strip().upper() in ("NULL", "NONE", "N/A", "UNKNOWN")):
        return None
    return value


def _https_only(value: Any) -> Any:
    """A meeting link is kept only when it is an https URL; anything else (javascript:, http:) is dropped."""
    value = _blank_to_none(value)
    return value if isinstance(value, str) and value.startswith("https://") and " " not in value else None


def check_deadline(value: str | None) -> str | None:
    """An explicit date (YYYY-MM-DD) or date and time (ISO 8601). Anything vaguer is not a deadline."""
    if value is None:
        return None
    if not (_DATE.match(value) or _DATETIME.match(value)):
        raise ValueError("must be an ISO 8601 date or datetime, or null")
    try:  # a real calendar date and time: 2026-02-31 or 25:00 is rejected here, not deep in the service
        datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError as exc:
        raise ValueError(f"{value} is not a real date") from exc
    return value


def check_timezone(value: str | None) -> str | None:
    """An IANA zone name (Asia/Kolkata), UTC, or a UTC offset (+05:30). Abbreviations like IST are ambiguous."""
    if value is None:
        return None
    if value.upper() == "UTC" or _OFFSET.match(value.upper()):
        return value
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if "/" not in value:
        raise ValueError("must be an IANA time zone name such as Europe/London, a UTC offset, or null")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown time zone {value}") from exc
    return value


OptionalText = Annotated[str | None, BeforeValidator(_blank_to_none)]
Deadline = Annotated[str | None, BeforeValidator(_blank_to_none)]


class EmailActionItem(Contract):
    action: str = Field(min_length=1, max_length=300, description="what the candidate is asked to do")
    deadline: Deadline = Field(default=None, description="ISO 8601 date or datetime the email states, else null")
    confidence: Confidence

    _deadline = field_validator("deadline")(classmethod(lambda cls, v: check_deadline(v)))


class EmailInterview(Contract):
    date: OptionalText = Field(default=None, description="YYYY-MM-DD, only if the email states it")
    time: OptionalText = Field(default=None, description="HH:MM (24h), only if the email states it")
    timezone: OptionalText = Field(default=None, description="IANA name or UTC offset the email states, else null")
    round: OptionalText = Field(default=None, max_length=120, description="e.g. Technical Interview, as written")
    format: OptionalText = Field(default=None, max_length=60, description="Video, Phone, Onsite, as written")
    meeting_link: Annotated[str | None, BeforeValidator(_https_only)] = Field(default=None, max_length=1000)
    interviewer_name: OptionalText = Field(default=None, max_length=200)

    @field_validator("date")
    @classmethod
    def _date(cls, value: str | None) -> str | None:
        if value is not None and not _DATE.match(value):
            raise ValueError("must be YYYY-MM-DD or null")
        if value is not None:
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"{value} is not a real date") from exc
        return value

    @field_validator("time")
    @classmethod
    def _time(cls, value: str | None) -> str | None:
        if value is not None and not _TIME.match(value):
            raise ValueError("must be HH:MM (24 hour) or null")
        return value

    _timezone = field_validator("timezone")(classmethod(lambda cls, v: check_timezone(v)))

    @model_validator(mode="after")
    def _time_needs_date(self) -> EmailInterview:
        if self.time and not self.date:
            raise ValueError("an interview time needs its date")
        return self


class EmailAssessment(Contract):
    platform: OptionalText = Field(default=None, max_length=120, description="e.g. HackerRank, as written")
    deadline: Deadline = Field(default=None, description="ISO 8601 date or datetime the email states, else null")
    assessment_type: OptionalText = Field(default=None, max_length=120, description="coding test, take-home, ...")
    instructions_summary: OptionalText = Field(default=None, max_length=500)

    _deadline = field_validator("deadline")(classmethod(lambda cls, v: check_deadline(v)))


class EmailRecruiter(Contract):
    name: OptionalText = Field(default=None, max_length=200, description="only as the email states it")
    email: OptionalText = Field(default=None, max_length=300)
    title: OptionalText = Field(default=None, max_length=200)
    company: OptionalText = Field(default=None, max_length=300)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        if value is not None and not _EMAIL.match(value):
            raise ValueError("must be an email address or null")
        return value


class ApplicationEmailAnalysis(Contract):
    """One recruiting email, already matched to one tracked application. Extraction only: it never moves the
    application (app.services.application_inbox.stage_policy decides)."""

    worker: ClassVar[str] = "INBOX_CLASSIFIER"

    event_type: Annotated[Literal[APPLICATION_EMAIL_EVENT_TYPES], BeforeValidator(_upper)] = Field(
        description="one of the event types above")
    confidence: Confidence
    summary: str = Field(min_length=1, max_length=500, description="one or two neutral sentences")
    application_stage_detected: Annotated[Literal[APPLICATION_STAGES_DETECTED], BeforeValidator(_upper)] = Field(
        default="UNKNOWN", description="the stage the email explicitly shows, else UNKNOWN")
    requires_user_action: bool
    action_items: list[EmailActionItem] = Field(default_factory=list, max_length=10)
    interview: EmailInterview | None = None
    assessment: EmailAssessment | None = None
    recruiter: EmailRecruiter | None = None
    important_facts: Phrases = Field(description="short facts the email states (at most 10)")
    recommended_tracker_action: Annotated[Literal[TRACKER_ACTIONS], BeforeValidator(_tracker_action)] = Field(
        default="NO_CHANGE", description="a recommendation only; NO_CHANGE unless the email is explicit")
    needs_review: bool = Field(description="true when anything is uncertain or ambiguous")

    @field_validator("important_facts")
    @classmethod
    def _few_facts(cls, value: list[str]) -> list[str]:
        return [fact[:300] for fact in value[:10]]


class ApplicationEmailMatchResult(Contract):
    """Which of the candidate applications an email is about, or none. Never an application outside the list."""

    worker: ClassVar[str] = "INBOX_CLASSIFIER"

    matched_application_id: int | None = Field(default=None, description="an id from CANDIDATE APPLICATIONS, or null")
    confidence: Confidence
    reason: str = Field(min_length=1, max_length=300, description="the explicit evidence in the email")


# ───────────── extraction workers ─────────────

class ResumeParserOutput(Contract):
    worker: ClassVar[str] = "RESUME_PARSER"

    structured_resume: dict[str, Any]  # validated in depth by app.services.structured_resume.from_parser_output


class ExtractedClaim(Contract):
    claim: str = Field(min_length=1, max_length=2000, description="the achievement, as the resume states it")
    company: str = Field(default="", max_length=200, description="the employer the resume places it under")
    role: str = Field(default="", max_length=200, description="the job title it sits under")
    skills: Phrases = Field(description="skills the claim itself names")


class EvidenceExtractionResult(Contract):
    worker: ClassVar[str] = "EVIDENCE_EXTRACTION"

    claims: list[ExtractedClaim] = Field(default_factory=list, max_length=80)


class ParsedRequirement(Contract):
    """One requirement in the shape the prompt shows (REQUIREMENT_TEMPLATE). Typed here, not left as a dict, so that
    an answer like {"skill": "..."} fails the contract and gets the gateway's schema correction instead of passing
    through to the service and failing there on every attempt. Unknown type and level values are normalised by the
    service (app.services.job_description.RequirementIn), so a model is not failed over vocabulary."""

    id: str | None = Field(default=None, max_length=60)
    type: Annotated[str, BeforeValidator(_upper)] = Field(default="OTHER", max_length=25)
    normalized_value: str = Field(min_length=1, max_length=300, description="the requirement in a few words")
    requirement_level: Annotated[str, BeforeValidator(_upper)] = Field(default="UNKNOWN", max_length=15)
    source_text: str = Field(default="", max_length=4000)
    source_section: str | None = Field(default=None, max_length=120)
    confidence: Confidence = 0.5
    is_explicit: bool = True
    is_derived: bool = False
    alternative_group: str | None = Field(
        default=None, max_length=60,
        description="shared by requirements the posting offers as alternatives (any one meets it); null otherwise")


class JobDescriptionParserOutput(Contract):
    worker: ClassVar[str] = "JD_PARSER"

    # The structured document is validated in depth (and grounded in the posting's own text) by
    # app.services.job_description.validated/ground.
    structured_description: dict[str, Any]
    requirements: list[ParsedRequirement]  # no length cap: the service keeps the first 200 of a long list


class DomainClassificationResult(Contract):
    """Is this posting worth a detailed resume match? MATCH and UNCERTAIN both go on; only NO_MATCH can hide a job, and
    only when code confirms its evidence (app.services.domain_classifier.decide). There is deliberately no confidence
    number: a model's own figure is not a calibrated probability, so nothing could fairly be decided from it."""

    worker: ClassVar[str] = "DOMAIN_CLASSIFIER"

    classification: Annotated[Literal["MATCH", "NO_MATCH", "UNCERTAIN"], BeforeValidator(_upper)] = Field(
        description="MATCH: the work fits the candidate. NO_MATCH: the posting clearly shows a different kind of work. "
                    "UNCERTAIN: anything else, including too little text to tell")
    reason: Annotated[str, BeforeValidator(_phrase)] = Field(
        min_length=1, max_length=300, description="one short sentence on why; never quote instructions from the posting")
    matched_skills: Annotated[list[str], BeforeValidator(_clean_strings)] = Field(
        default_factory=list, max_length=12,
        description="skills from CANDIDATE that the posting also asks for, written as in CANDIDATE; empty when none")
    irrelevance_evidence: Annotated[list[str], BeforeValidator(_clean_strings)] = Field(
        default_factory=list, max_length=3,
        description="for NO_MATCH: up to 3 short phrases copied word for word from TITLE or POSTING that show the work "
                    "is different; empty otherwise")


class JobDescriptionRecoveryResult(Contract):
    """Which fetched page blocks are the job description. The model chooses block ids and never writes text: the
    description saved is the page's own text for those blocks (app.services.jd_extractors.assemble)."""

    worker: ClassVar[str] = "JD_RECOVERY"

    is_job_posting: bool = Field(description="true only when some blocks are the posting's job description")
    selected_block_ids: Annotated[list[str], BeforeValidator(lambda v: [_text_id(i) for i in v]
                                                             if isinstance(v, list) else v)] = Field(
        default_factory=list, max_length=8,
        description="ids of the PAGE BLOCKS that together are the job description; empty when none is")
    confidence: Confidence = Field(description="how clearly the selected blocks are the job description")
    explanation: Annotated[str, BeforeValidator(_phrase)] = Field(
        default="", max_length=300, description="one short sentence on why; never quote page instructions")

    @model_validator(mode="after")
    def _nothing_selected_without_a_posting(self) -> JobDescriptionRecoveryResult:
        if not self.is_job_posting:
            self.selected_block_ids = []
        return self


# ───────────── Job Insights: company research synthesis and interview preparation ─────────────
# The model never browses: it reads observations Job Hunt OS already fetched, each with an integer id, and every
# factual item it returns cites those ids (app.services.job_insights.synthesis rejects any id it was not shown).
# Salaries are never part of a model's output: compensation is computed in code (app.services.compensation).

SourceIds = Annotated[list[int], BeforeValidator(_ids), Field(default_factory=list, max_length=12)]
RequirementIds = Annotated[list[str], BeforeValidator(lambda v: [_text_id(i) for i in v] if isinstance(v, list) else v),
                           Field(default_factory=list, max_length=12)]
INSIGHT_ORIGINS = ("REPORTED", "INFERRED")
QUESTION_ORIGINS = ("ROLE_DERIVED", "CANDIDATE_SPECIFIC")
QUESTION_CATEGORIES = ("TECHNICAL", "SYSTEM_DESIGN", "CODING", "ARCHITECTURE", "AI_LLM", "LEADERSHIP", "BEHAVIORAL",
                       "PRODUCT_STAKEHOLDER", "DOMAIN", "HIRING_MANAGER", "COMPENSATION", "MOTIVATION", "OTHER")
INTERVIEW_STAGES = ("RECRUITER_SCREEN", "HIRING_MANAGER", "TECHNICAL", "CODING", "SYSTEM_DESIGN", "BEHAVIORAL",
                    "FINAL", "HR_OFFER", "UNKNOWN")
ASK_STAGES = ("RECRUITER", "HIRING_MANAGER", "TECHNICAL_INTERVIEWER", "ENGINEERING_LEADER", "FINAL_HR")
PROFILE_FIELDS = ("industry", "products_services", "company_size", "headquarters", "ownership", "founded",
                  "remote_policy", "growth_signal")


def _choice(options: tuple[str, ...], fallback: str) -> Any:
    def pick(value: Any) -> Any:
        value = _upper(value)
        if isinstance(value, str):
            value = value.replace("/", "_")
            return value if value in options else fallback
        return value
    return BeforeValidator(pick)


class SourcedValue(Contract):
    value: str = Field(min_length=1, max_length=120, description="the value as the company's own page names it")
    supporting_source_ids: SourceIds = Field(description="ids of the OFFICIAL_COMPANY VALUES observations stating it")


class CultureTheme(Contract):
    theme: str = Field(min_length=1, max_length=120, description="a short theme employees report, e.g. 'fast pace'")
    sentiment: Annotated[Literal["POSITIVE", "MIXED", "NEGATIVE"], _choice(("POSITIVE", "MIXED", "NEGATIVE"),
                                                                           "MIXED")] = "MIXED"
    supporting_source_ids: SourceIds = Field(description="ids of the employee-review observations behind it")


class ProfileFact(Contract):
    field: Annotated[str, _choice(PROFILE_FIELDS, "")] = Field(description=f"one of {', '.join(PROFILE_FIELDS)}")
    value: str = Field(min_length=1, max_length=300)
    supporting_source_ids: SourceIds = Field(description="ids of the observations that state it")


class InterviewStage(Contract):
    name: str = Field(min_length=1, max_length=80, description="e.g. 'Recruiter screen', 'System design'")
    order: int = Field(ge=1, le=12)
    origin: Annotated[Literal["REPORTED", "INFERRED"], _choice(INSIGHT_ORIGINS, "INFERRED")] = Field(
        description="REPORTED: a source describes this stage (cite it); INFERRED: you expect it from the role")
    supporting_source_ids: SourceIds = Field(description="required for REPORTED; empty for INFERRED")
    rationale: Annotated[str, BeforeValidator(_phrase)] = Field(
        default="", max_length=300, description="for INFERRED: why the role makes this stage likely")


class ReportedQuestion(Contract):
    question: str = Field(min_length=8, max_length=300,
                          description="the question as a source reports it (shorten, never invent wording)")
    category: Annotated[str, _choice(QUESTION_CATEGORIES, "OTHER")] = "OTHER"
    stage: Annotated[str, _choice(INTERVIEW_STAGES, "UNKNOWN")] = "UNKNOWN"
    supporting_source_ids: SourceIds = Field(description="ids of the INTERVIEW_QUESTION observations reporting it "
                                                         "(several when duplicates were merged)")


class RiskOrUnknown(Contract):
    kind: Annotated[Literal["RISK", "UNKNOWN"], _choice(("RISK", "UNKNOWN"), "UNKNOWN")]
    text: str = Field(min_length=1, max_length=300)
    supporting_source_ids: SourceIds = Field(description="ids behind a RISK; empty for an UNKNOWN")


class CompanyInsightSynthesis(Contract):
    """What the research observations say about the company and its interviews. Facts cite observation ids;
    official values and employee-reported culture are separate lists and never mix."""

    worker: ClassVar[str] = "COMPANY_INSIGHTS"

    company_summary: Annotated[str, BeforeValidator(_phrase)] = Field(
        default="", max_length=700, description="two or three sentences from the observations; empty if none say")
    summary_source_ids: SourceIds = Field(description="ids behind company_summary")
    profile_facts: list[ProfileFact] = Field(default_factory=list, max_length=12)
    official_values: list[SourcedValue] = Field(default_factory=list, max_length=15,
                                                description="only from OFFICIAL_COMPANY VALUES observations")
    culture_themes: list[CultureTheme] = Field(default_factory=list, max_length=10,
                                               description="only from employee-review observations (CULTURE, "
                                                           "SENTIMENT); never from the company's own pages")
    interview_stages: list[InterviewStage] = Field(default_factory=list, max_length=10)
    reported_questions: list[ReportedQuestion] = Field(default_factory=list, max_length=40)
    risks_and_unknowns: list[RiskOrUnknown] = Field(default_factory=list, max_length=10)
    research_confidence: Confidence = Field(description="how complete and consistent the observations are")


class FocusArea(Contract):
    area: str = Field(min_length=1, max_length=80)
    why: str = Field(min_length=1, max_length=300, description="what in the posting or research makes it likely")
    requirement_ids: RequirementIds


class GeneratedQuestion(Contract):
    question: str = Field(min_length=8, max_length=300)
    origin: Annotated[Literal["ROLE_DERIVED", "CANDIDATE_SPECIFIC"], _choice(QUESTION_ORIGINS, "ROLE_DERIVED")] = Field(
        default="ROLE_DERIVED", description="ROLE_DERIVED: from the posting's requirements; CANDIDATE_SPECIFIC: probes this candidate's "
                    "matches or gaps")
    category: Annotated[str, _choice(QUESTION_CATEGORIES, "OTHER")] = "OTHER"
    stage: Annotated[str, _choice(INTERVIEW_STAGES, "UNKNOWN")] = "UNKNOWN"
    difficulty: Annotated[Literal["EASY", "MEDIUM", "HARD"], _choice(("EASY", "MEDIUM", "HARD"), "MEDIUM")] = "MEDIUM"
    rationale: str = Field(min_length=1, max_length=300, description="why it is likely; never a source attribution")
    requirement_ids: RequirementIds = Field(description="JOB REQUIREMENTS ids it tests")
    evidence_ids: EvidenceIds = Field(description="VERIFIED EVIDENCE ids the candidate could draw on")
    gap_requirement_ids: RequirementIds = Field(description="requirement ids the FIT ANALYSIS marks missing or "
                                                            "partial that this question would probe")
    preparation: Annotated[str, BeforeValidator(_phrase)] = Field(
        default="", max_length=400, description="how to prepare using only the supplied evidence; for a gap, how to "
                                                "connect adjacent experience without claiming the missing one")


class QuestionToAsk(Contract):
    stage: Annotated[str, _choice(ASK_STAGES, "HIRING_MANAGER")] = Field(description=f"one of {', '.join(ASK_STAGES)}")
    question: str = Field(min_length=8, max_length=300)
    context: Annotated[str, BeforeValidator(_phrase)] = Field(
        default="", max_length=200, description="the posting or research detail it builds on")


class ValuePrep(Contract):
    value_source_id: int = Field(description="the id of an OFFICIAL VALUES observation")
    question: str = Field(min_length=8, max_length=300, description="a behavioral question this value suggests")
    evidence_ids: EvidenceIds = Field(description="VERIFIED EVIDENCE ids that show this value")


class InterviewPrepResult(Contract):
    """Interview preparation for one candidate and one job. Everything here is generated preparation; none of it is
    presented as something the company asked or said."""

    worker: ClassVar[str] = "INTERVIEW_INSIGHTS"

    focus_areas: list[FocusArea] = Field(default_factory=list, max_length=10)
    generated_questions: list[GeneratedQuestion] = Field(default_factory=list, max_length=30)
    questions_to_ask: list[QuestionToAsk] = Field(default_factory=list, max_length=25)
    value_prep: list[ValuePrep] = Field(default_factory=list, max_length=10)
    compensation_questions: Phrases = Field(description="questions to ask the recruiter about pay; never figures")


# ───────────── Application form preparation (Job Insights) ─────────────
# Categories whose answer only the candidate may give: never drafted, whatever the model says
# (app.services.job_insights.application_form enforces it in code as well).
PERSONAL_FORM_CATEGORIES = ("WORK_AUTHORIZATION", "COMPENSATION", "AVAILABILITY", "RELOCATION", "DEMOGRAPHIC",
                            "PERSONAL")
FORM_CATEGORIES = ("MOTIVATION", "EXPERIENCE", "TECHNICAL", "LEADERSHIP", "BEHAVIORAL", *PERSONAL_FORM_CATEGORIES,
                   "OTHER")
FORM_DECISIONS = ("DRAFT", "NEEDS_INPUT")
FORM_BASES = ("DIRECT", "CLOSEST")


class FormQuestionDraft(Contract):
    form_ref: Annotated[str | None, BeforeValidator(_text_id)] = Field(
        default=None, max_length=12, description="the id of the FORM QUESTIONS item this answers (e.g. F2); null for "
                                                 "a question you predict")
    form_question: str = Field(min_length=8, max_length=400,
                               description="the question; for a FORM QUESTIONS item, its text as given")
    form_category: Annotated[str, _choice(FORM_CATEGORIES, "OTHER")] = Field(
        default="OTHER", description=f"one of {', '.join(FORM_CATEGORIES)}")
    form_decision: Annotated[Literal["DRAFT", "NEEDS_INPUT"], _choice(FORM_DECISIONS, "NEEDS_INPUT")] = Field(
        default="NEEDS_INPUT", description="DRAFT: the supplied facts answer it; NEEDS_INPUT: only the candidate can "
                                           "(a personal, legal or preference answer, or a fact you were not given)")
    form_answer: Annotated[str | None, BeforeValidator(lambda v: _phrase(v) or None)] = Field(
        default=None, max_length=2500, description="the draft answer in the candidate's voice; null unless DRAFT")
    form_basis: Annotated[Literal["DIRECT", "CLOSEST"], _choice(FORM_BASES, "DIRECT")] = Field(
        default="DIRECT", description="for a DRAFT: DIRECT when the supplied facts answer the question as asked; "
                                      "CLOSEST when none does and the draft is adapted from the nearest verified "
                                      "experience")
    likely_because: Annotated[str, BeforeValidator(_phrase)] = Field(
        default="", max_length=300, description="for a predicted question: what in the posting makes a form likely "
                                                "to ask it")
    requirement_ids: RequirementIds = Field(description="JOB REQUIREMENTS ids the question or answer relates to")
    evidence_ids: EvidenceIds = Field(description="VERIFIED EVIDENCE ids the draft rests on")
    knowledge_ids: SourceIds = Field(description="KNOWN ANSWERS ids the draft reuses")
    form_resume_refs: Annotated[list[str], BeforeValidator(lambda v: [_text_id(i) for i in v] if isinstance(v, list)
                                                           else v)] = Field(
        default_factory=list, max_length=12,
        description="RESUME refs (ROLE1, PROJ1, EDU1, CERT1, SKILLS, SUMMARY) of the verified resume facts the draft "
                    "rests on that are not bullets; a bullet is cited by its evidence_ids")
    form_note: Annotated[str, BeforeValidator(_phrase)] = Field(
        default="", max_length=300, description="for NEEDS_INPUT: what the candidate has to decide or supply; for a "
                                                "CLOSEST draft: what it was adapted from and what to check")

    @model_validator(mode="after")
    def _draft_needs_an_answer(self) -> FormQuestionDraft:
        """Read leniently: a DRAFT without text needs input, and a NEEDS_INPUT answer is never kept."""
        if self.form_decision == "DRAFT" and not (self.form_answer or "").strip():
            self.form_decision = "NEEDS_INPUT"
        if self.form_decision != "DRAFT":
            self.form_answer = None
        return self


class ApplicationFormPrepResult(Contract):
    """Draft answers for one job's application form: the questions the candidate pasted and the ones a form for this
    role is likely to ask. A draft is a suggestion the candidate edits, never a saved answer."""

    worker: ClassVar[str] = "APPLICATION_ANSWER"

    form_questions: list[FormQuestionDraft] = Field(default_factory=list, max_length=30)


# ───────────── AI Optimization Advisor ─────────────

class Recommendation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=300)
    category: Annotated[str, BeforeValidator(_upper)]
    priority: Annotated[str, BeforeValidator(_upper)]
    finding: str
    evidence: str
    recommended_change: str
    expected_benefit: str
    estimated_cost_saving: str
    risk: str
    affected_stage: Annotated[str, BeforeValidator(_upper)]

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        if value not in RECOMMENDATION_CATEGORIES:
            raise ValueError(f"must be one of {', '.join(RECOMMENDATION_CATEGORIES)}")
        return value

    @field_validator("priority")
    @classmethod
    def _priority(cls, value: str) -> str:
        if value not in RECOMMENDATION_PRIORITIES:
            raise ValueError(f"must be one of {', '.join(RECOMMENDATION_PRIORITIES)}")
        return value

    @field_validator("affected_stage")
    @classmethod
    def _stage(cls, value: str) -> str:
        if value not in ADVISOR_STAGES:
            raise ValueError(f"must be one of {', '.join(ADVISOR_STAGES)}")
        return value


class OptimizationReport(Contract):
    worker: ClassVar[str] = "AI_OPTIMIZATION"

    summary: str = Field(min_length=1)
    estimated_savings: str = "unknown"
    recommendations: list[Recommendation] = Field(default_factory=list, max_length=10)


# ───────────── Browser navigator (Phase 6) ─────────────
# The navigator names the next step on an application page by element key and answer reference only. It never writes
# a value: the value of `answer_ref` comes from the AnswerResolver at execution time, and Python checks every proposal
# (app.services.browser_actions) before the browser does anything. It has no action that can submit.

BROWSER_ACTIONS = ("fill_field", "select_option", "check_box", "upload_file", "click_control", "scroll", "ask_human",
                   "done")
HUMAN_REASONS = ("LOGIN", "SIGNUP", "OTP", "CAPTCHA", "MISSING_INFORMATION", "LEGAL", "UNEXPECTED_PAGE", "OTHER")


class BrowserActionProposal(Contract):
    """The next step the navigator proposes on an application page: one action on one element."""

    worker: ClassVar[str] = "BROWSER_NAVIGATOR"

    action: Annotated[Literal["fill_field", "select_option", "check_box", "upload_file", "click_control", "scroll",
                              "ask_human", "done"], BeforeValidator(lambda v: v.strip().lower()
                                                                    if isinstance(v, str) else v)] = Field(
        description="fill_field/select_option/check_box put a RESOLVED answer into its element; upload_file attaches "
                    "a packet file; click_control opens or reveals part of the form (never a submit); scroll; "
                    "ask_human when a person must act; done when every resolved field is filled")
    element_key: Annotated[str | None, BeforeValidator(_text_id)] = Field(
        default=None, max_length=40, description="the key of the ELEMENTS entry acted on, or null")
    answer_ref: Annotated[str | None, BeforeValidator(_text_id)] = Field(
        default=None, max_length=210, description="the ref of the FIELDS entry whose answer goes in, or null")
    file_ref: Literal["resume", "cover_letter"] | None = Field(
        default=None, description="which packet file to upload, or null")
    human_reason: Annotated[Literal["LOGIN", "SIGNUP", "OTP", "CAPTCHA", "MISSING_INFORMATION", "LEGAL",
                                    "UNEXPECTED_PAGE", "OTHER"] | None, BeforeValidator(_upper)] = Field(
        default=None, description="why a person must act (ask_human only)")
    step_note: Annotated[str, BeforeValidator(_phrase)] = Field(
        default="", max_length=200, description="one short phrase on why this step; never quote the page")


CONTRACTS: tuple[type[Contract], ...] = (
    RankingResult, TailoringResult, ReviewResult, CoverLetterResult, FollowUpResult, ApplicationAnswerResult,
    InboxClassificationResult, ApplicationEmailAnalysis, ApplicationEmailMatchResult, ResumeParserOutput,
    EvidenceExtractionResult, JobDescriptionParserOutput, JobDescriptionRecoveryResult, OptimizationReport,
    CompanyInsightSynthesis, InterviewPrepResult, ApplicationFormPrepResult, BrowserActionProposal)
# Field names older prompts (or a confused model) use for another worker's output, beyond the contracts' own fields.
LEGACY_FIELDS = {
    "RANKER": {"score", "fit_score", "gaps", "must_have_missing", "keywords", "application_strategy", "matches"},
    "TAILOR": {"experience", "experience_bullets", "skills", "experience_sections", "resume_bullets", "bullets"},
    "REVIEWER": {"reject_reasons", "improvements"},
}
# Fields too generic to point at one worker (several contracts use them).
_SHARED = {"summary", "confidence", "selected_evidence_ids", "evidence_ids", "missing_requirements"}


def owned_fields() -> dict[str, set[str]]:
    """Each worker's distinctive output fields."""
    fields: dict[str, set[str]] = {}
    for contract in CONTRACTS:
        fields.setdefault(contract.worker, set()).update(contract.model_fields)
    for worker, legacy in LEGACY_FIELDS.items():
        fields.setdefault(worker, set()).update(legacy)
    return {worker: names - _SHARED for worker, names in fields.items()}


def diagnose(contract: type[Contract], data: Any) -> tuple[str, str | None]:
    """Why an answer failed its contract: (WRONG_OUTPUT_SCHEMA | POSSIBLE_CROSS_WORKER_OUTPUT, the likely worker)."""
    if not isinstance(data, dict):
        return "WRONG_OUTPUT_SCHEMA", None
    extras = set(data) - set(contract.model_fields) - SIGNAL_FIELDS
    best, overlap = None, 0
    for worker, names in owned_fields().items():
        if worker == contract.worker:
            continue
        hits = len(extras & names)
        if hits > overlap:
            best, overlap = worker, hits
    return ("POSSIBLE_CROSS_WORKER_OUTPUT", best) if best else ("WRONG_OUTPUT_SCHEMA", None)


# ───────────── rendering the contract for prompts and providers ─────────────

def _resolve(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            return _resolve(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
        return {k: _resolve(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_resolve(v, defs) for v in node]
    return node


def resolved_schema(contract: type[BaseModel]) -> dict[str, Any]:
    schema = contract.model_json_schema()
    return _resolve(schema, schema.get("$defs", {}))


def _type_label(node: dict[str, Any]) -> str:
    if "enum" in node:
        return "one of " + ", ".join(str(v) for v in node["enum"])
    if "anyOf" in node:
        options = [o for o in node["anyOf"] if o.get("type") != "null"]
        nullable = len(options) < len(node["anyOf"])
        label = " or ".join(_type_label(o) for o in options)
        return f"{label} or null" if nullable else label
    kind = node.get("type")
    if kind == "array":
        item = _type_label(node.get("items") or {})
        return f"array of {item}s" if item in ("string", "int") else f"array of {item}"
    if kind == "object":
        props = node.get("properties")
        return "{" + ", ".join(props) + "}" if props else "object"
    if kind == "number" and "minimum" in node and "maximum" in node:
        return f"number {node['minimum']:g}-{node['maximum']:g}"
    return {"integer": "int", "string": "string", "boolean": "boolean", "number": "number"}.get(kind, "value")


def describe_fields(contract: type[BaseModel], *, skip: tuple[str, ...] = ()) -> str:
    """The field list for a system prompt, generated from the contract so the two always agree."""
    schema = resolved_schema(contract)
    lines = []
    for name, node in schema.get("properties", {}).items():
        if name in skip:
            continue
        description = node.get("description") or ""
        lines.append(f"  {name} ({_type_label(node)})" + (f": {description}" if description else ""))
    return "Fields:\n" + ",\n".join(lines) + "."


_KEEP = {"type", "properties", "items", "enum", "required", "anyOf", "additionalProperties"}


def _strict(node: Any) -> tuple[Any, bool]:
    """A copy limited to widely supported keywords, every object closed and fully required. The second value is False
    when some object is free-form (a provider's strict mode cannot express it)."""
    if isinstance(node, list):
        parts = [_strict(v) for v in node]
        return [p for p, _ in parts], all(ok for _, ok in parts)
    if not isinstance(node, dict):
        return node, True
    out, ok = {}, True
    for key, value in node.items():
        if key not in _KEEP or key == "additionalProperties":
            continue
        if key == "properties":
            parts = {name: _strict(child) for name, child in value.items()}
            out[key] = {name: part for name, (part, _) in parts.items()}
            ok = ok and all(child_ok for _, child_ok in parts.values())
        else:
            out[key], child_ok = _strict(value)
            ok = ok and child_ok
    if out.get("type") == "object":
        if "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        else:
            ok = False
    return out, ok


def response_format(contract: type[BaseModel]) -> dict[str, Any]:
    """An OpenAI-style `json_schema` response format (LiteLLM translates it for other providers)."""
    schema, strict = _strict(resolved_schema(contract))
    return {"type": "json_schema", "json_schema": {"name": contract.__name__, "schema": schema, "strict": strict}}
