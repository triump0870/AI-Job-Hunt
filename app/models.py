from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (JSON, Boolean, CheckConstraint, Date, DateTime, Enum, Float, ForeignKey, Index, Integer, Numeric,
                        String, Text, UniqueConstraint, false, text, true)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db import Base
from app.services.lifecycle import ErrorCategory, WorkflowState, parse_state, validate_transition


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _enum_type(enum: type) -> Enum:
    # Stored as VARCHAR; the named CHECK constraints below (and in migration 0002) reject other strings.
    return Enum(enum, native_enum=False, create_constraint=False, length=32, validate_strings=True,
                values_callable=lambda members: [m.value for m in members])


def _in_check(column: str, enum: type) -> str:
    return f"{column} IN ({', '.join(repr(m.value) for m in enum)})"


def _values(values: tuple[str, ...]) -> str:
    return ", ".join(repr(v) for v in values)


def _owner(table: str, unique: bool = False, required: bool = True):
    """The account that owns a row: every owned table carries it, indexed, and the row dies with the account.

    Migration 0007 makes it NOT NULL once every row has an owner; an install that still has pre-accounts
    data keeps it nullable in the database until the first account claims that data.
    """
    return mapped_column(ForeignKey("users.id", ondelete="CASCADE", name=f"fk_{table}_user_id"), nullable=not required,
                         index=True, unique=unique)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(String(300))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Everything this account owns. Deleting an account cascades in the database, so these never load
    # rows just to delete them (passive_deletes).
    profile: Mapped[CandidateProfile | None] = relationship(back_populates="user", uselist=False, passive_deletes=True)
    settings: Mapped[AppSetting | None] = relationship(back_populates="user", uselist=False, passive_deletes=True)
    resumes: Mapped[list[ResumeDocument]] = relationship(back_populates="user", passive_deletes=True)
    structured_resumes: Mapped[list[Resume]] = relationship(back_populates="user", passive_deletes=True)
    resume_data_changes: Mapped[list[ResumeDataChange]] = relationship(back_populates="user", passive_deletes=True)
    knowledge_answers: Mapped[list[KnowledgeAnswer]] = relationship(back_populates="user", passive_deletes=True)
    evidence: Mapped[list[Evidence]] = relationship(back_populates="user", passive_deletes=True)
    jobs: Mapped[list[Job]] = relationship(back_populates="user", passive_deletes=True)
    job_classification_tasks: Mapped[list[JobClassificationTask]] = relationship(back_populates="user", passive_deletes=True)
    job_classification_cache: Mapped[list[JobClassificationCache]] = relationship(back_populates="user", passive_deletes=True)
    search_runs: Mapped[list[SearchRun]] = relationship(back_populates="user", passive_deletes=True)
    search_profiles: Mapped[list[SearchProfile]] = relationship(back_populates="user", passive_deletes=True)
    saved_searches: Mapped[list[SavedSearch]] = relationship(back_populates="user", passive_deletes=True)
    provider_cooldowns: Mapped[list[ProviderCooldown]] = relationship(back_populates="user", passive_deletes=True)
    provider_rate_windows: Mapped[list[ProviderRateWindow]] = relationship(back_populates="user", passive_deletes=True)
    model_route_states: Mapped[list[ModelRouteState]] = relationship(back_populates="user", passive_deletes=True)
    failover_events: Mapped[list[FailoverEvent]] = relationship(back_populates="user", passive_deletes=True)
    agent_threads: Mapped[list[AgentThread]] = relationship(back_populates="user", passive_deletes=True)
    agent_effects: Mapped[list[AgentEffect]] = relationship(back_populates="user", passive_deletes=True)
    qualification_tasks: Mapped[list[JobQualificationTask]] = relationship(back_populates="user",
                                                                           passive_deletes=True)
    preparation_tasks: Mapped[list[JobPreparationTask]] = relationship(back_populates="user", passive_deletes=True)
    approved_packets: Mapped[list[ApprovedPacket]] = relationship(back_populates="user", passive_deletes=True)
    application_claims: Mapped[list[ApplicationClaim]] = relationship(back_populates="user", passive_deletes=True)
    application_claim_keys: Mapped[list[ApplicationClaimKey]] = relationship(back_populates="user", passive_deletes=True)
    application_execution_tasks: Mapped[list[ApplicationExecutionTask]] = relationship(back_populates="user", passive_deletes=True)
    application_execution_attempts: Mapped[list[ApplicationExecutionAttempt]] = relationship(back_populates="user", passive_deletes=True)
    submission_intents: Mapped[list[SubmissionIntent]] = relationship(back_populates="user", passive_deletes=True)
    ats_routes: Mapped[list[AtsRoute]] = relationship(back_populates="user", passive_deletes=True)
    ats_credentials: Mapped[list[AtsCredential]] = relationship(back_populates="user", passive_deletes=True)
    ats_sessions: Mapped[list[AtsSession]] = relationship(back_populates="user", passive_deletes=True)
    evidence_resume_links: Mapped[list[EvidenceResumeLink]] = relationship(back_populates="user",
                                                                           passive_deletes=True)
    rag_index_versions: Mapped[list[RagIndexVersion]] = relationship(back_populates="user", passive_deletes=True)
    rag_index_entries: Mapped[list[RagIndexEntry]] = relationship(back_populates="user", passive_deletes=True)
    applications: Mapped[list[Application]] = relationship(back_populates="user", passive_deletes=True)
    resume_variants: Mapped[list[ResumeVariant]] = relationship(back_populates="user", passive_deletes=True)
    tailor_tasks: Mapped[list[TailorTask]] = relationship(back_populates="user", passive_deletes=True)
    followups: Mapped[list[FollowUp]] = relationship(back_populates="user", passive_deletes=True)
    application_events: Mapped[list[ApplicationEvent]] = relationship(back_populates="user", passive_deletes=True)
    transitions: Mapped[list[WorkflowTransition]] = relationship(back_populates="user", passive_deletes=True)
    resume_versions: Mapped[list[ResumeVersion]] = relationship(back_populates="user", passive_deletes=True)
    job_requirements: Mapped[list[JobRequirementEvidence]] = relationship(back_populates="user", passive_deletes=True)
    job_parse_tasks: Mapped[list[JobDescriptionParseTask]] = relationship(back_populates="user", passive_deletes=True)
    resume_parse_tasks: Mapped[list[ResumeParseTask]] = relationship(back_populates="user", passive_deletes=True)
    resume_extractions: Mapped[list[ResumeExtraction]] = relationship(back_populates="user", passive_deletes=True)
    resume_extraction_feedback: Mapped[list[ResumeExtractionFeedback]] = relationship(back_populates="user",
                                                                                      passive_deletes=True)
    job_fetch_tasks: Mapped[list[JobDescriptionFetchTask]] = relationship(back_populates="user", passive_deletes=True)
    job_insight_tasks: Mapped[list[JobInsightTask]] = relationship(back_populates="user", passive_deletes=True)
    job_insights: Mapped[list[JobInsight]] = relationship(back_populates="user", passive_deletes=True)
    insight_observations: Mapped[list[InsightObservation]] = relationship(back_populates="user", passive_deletes=True)
    removed_postings: Mapped[list[RemovedPosting]] = relationship(back_populates="user", passive_deletes=True)
    job_feedback: Mapped[list[JobFeedbackEvent]] = relationship(back_populates="user", passive_deletes=True)
    job_structured_changes: Mapped[list[JobStructuredDataChange]] = relationship(back_populates="user",
                                                                                 passive_deletes=True)
    ranking_tasks: Mapped[list[JobRankingTask]] = relationship(back_populates="user", passive_deletes=True)
    rankings: Mapped[list[JobRanking]] = relationship(back_populates="user", passive_deletes=True)
    optimization_tasks: Mapped[list[AIOptimizationTask]] = relationship(back_populates="user", passive_deletes=True)
    optimization_reports: Mapped[list[AIOptimizationReport]] = relationship(back_populates="user", passive_deletes=True)
    conversation_actions: Mapped[list[ConversationAction]] = relationship(back_populates="user", passive_deletes=True)
    mcp_tokens: Mapped[list[McpToken]] = relationship(back_populates="user", passive_deletes=True)
    litellm_connection: Mapped[LiteLLMConnection | None] = relationship(back_populates="user", uselist=False,
                                                                         passive_deletes=True)
    ai_models: Mapped[list[AIModel]] = relationship(back_populates="user", passive_deletes=True)
    model_tiers: Mapped[list[ModelTier]] = relationship(back_populates="user", passive_deletes=True)
    worker_model_configs: Mapped[list[WorkerModelConfig]] = relationship(back_populates="user", passive_deletes=True)
    ai_routing_config: Mapped[AIRoutingConfig | None] = relationship(back_populates="user", uselist=False,
                                                                      passive_deletes=True)
    ai_config_versions: Mapped[list[AIConfigVersion]] = relationship(back_populates="user", passive_deletes=True)
    ai_config_audit: Mapped[list[AIConfigAuditEntry]] = relationship(back_populates="user", passive_deletes=True)
    gmail_connection: Mapped[GmailConnection | None] = relationship(back_populates="user", uselist=False,
                                                                    passive_deletes=True)
    application_email_messages: Mapped[list[ApplicationEmailMessage]] = relationship(back_populates="user",
                                                                                     passive_deletes=True)
    application_communication_events: Mapped[list[ApplicationCommunicationEvent]] = relationship(
        back_populates="user", passive_deletes=True)
    application_inbox_digests: Mapped[list[ApplicationInboxDigest]] = relationship(back_populates="user",
                                                                                   passive_deletes=True)


class UserSession(Base):
    """A signed-in browser. Only the SHA-256 of the cookie token is stored."""

    __tablename__ = "user_sessions"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_user_sessions_token_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE", name="fk_user_sessions_user_id"),
                                         index=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("candidate_profiles", unique=True)
    user: Mapped[User] = relationship(back_populates="profile")
    name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(300), default="")
    phone: Mapped[str] = mapped_column(String(100), default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    linkedin_url: Mapped[str] = mapped_column(String(500), default="")
    github_url: Mapped[str] = mapped_column(String(500), default="")
    headline: Mapped[str] = mapped_column(String(500), default="")
    target_roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    preferred_locations: Mapped[list[str]] = mapped_column(JSON, default=list)
    work_authorization: Mapped[str] = mapped_column(Text, default="")
    compensation_notes: Mapped[str] = mapped_column(Text, default="")
    master_resume: Mapped[str] = mapped_column(Text, default="")
    search_preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("app_settings", unique=True)
    user: Mapped[User] = relationship(back_populates="settings")
    llm_provider: Mapped[str] = mapped_column(String(30), default="openrouter")
    model: Mapped[str] = mapped_column(String(300), default="openrouter/free")
    application_mode: Mapped[str] = mapped_column(String(30), default="manual_approval")
    min_review_score: Mapped[int] = mapped_column(Integer, default=70)
    # Cadence, tracker stages and other small UI preferences.
    preferences: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict, nullable=True)
    # The user's own API keys, encrypted with SECRET_KEY (app.security). Never returned by the API.
    openrouter_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    openai_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    anthropic_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    gemini_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    apify_api_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ResumeDocument(Base):
    __tablename__ = "resume_documents"
    __table_args__ = (UniqueConstraint("user_id", "checksum", name="uq_resume_documents_user_checksum"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("resume_documents")
    user: Mapped[User] = relationship(back_populates="resumes")
    original_filename: Mapped[str] = mapped_column(String(500))
    media_type: Mapped[str] = mapped_column(String(150))
    storage_path: Mapped[str] = mapped_column(String(1000))
    checksum: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


RESUME_VERIFICATION_STATUSES = ("UNVERIFIED", "PARTIALLY_VERIFIED", "VERIFIED", "NEEDS_REVIEW")
RESUME_CHANGE_TYPES = ("ADD", "UPDATE", "REMOVE", "REORDER", "VERIFY", "REJECT", "UNVERIFY", "PARSE",
                       "REPARSE_ACCEPT", "REPARSE_KEEP", "VERIFY_RESUME")
RESUME_CHANGE_SOURCES = ("PARSER", "USER", "SYSTEM")


class Resume(Base):
    """One version of the candidate's resume: the original file, its cleaned raw text and the structured data.

    Versions are never overwritten: a new upload (or new master text) is a new row. `raw_text` is the read-only
    reference; `structured_data` (app.services.structured_resume) is what AI workflows read, and only the parts
    the user verified are trusted (app.services.resume_verification).
    """

    __tablename__ = "resumes"
    __table_args__ = (
        UniqueConstraint("candidate_profile_id", "version", name="uq_resumes_profile_version"),
        CheckConstraint(f"verification_status IN ({', '.join(repr(v) for v in RESUME_VERIFICATION_STATUSES)})",
                        name="ck_resumes_verification_status"),
        Index("uq_resumes_active_profile", "candidate_profile_id", unique=True,
              postgresql_where=text("is_active IS TRUE"), sqlite_where=text("is_active = 1")),
        Index("uq_resumes_default_profile", "candidate_profile_id", unique=True,
              postgresql_where=text("is_default IS TRUE"), sqlite_where=text("is_default = 1")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("resumes")
    user: Mapped[User] = relationship(back_populates="structured_resumes")
    candidate_profile_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE", name="fk_resumes_candidate_profile_id"), index=True)
    # The uploaded file, while its row exists. The source_* columns keep the provenance after it is deleted.
    resume_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("resume_documents.id", ondelete="SET NULL", name="fk_resumes_resume_document_id"), nullable=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    version: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20), default="upload")  # upload | manual_text | migration
    source_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_media_type: Mapped[str | None] = mapped_column(String(150), nullable=True)
    source_file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_storage_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    raw_text_hash: Mapped[str] = mapped_column(String(64))
    raw_text: Mapped[str] = mapped_column(Text, default="")
    structured_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    parser: Mapped[str | None] = mapped_column(String(20), nullable=True)  # deterministic | llm
    parser_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Incremented on every change to structured_data; edits must name the revision they started from.
    revision: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_status: Mapped[str] = mapped_column(String(20), default="UNVERIFIED")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # A re-parse waiting for the user to keep or replace each differing value.
    pending_reparse: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # What this resume is for (app.services.resume_matching): one or more domains ("AI / ML", "Data"), each with the
    # roles it targets ([{"domain": "AI / ML", "roles": ["AI Engineer"]}, ...]). New jobs are matched to the
    # best-tagged resume. A library file shares the tags of its version. `domain` (the first group's) and `roles` (every
    # group's) are mirrors kept for older readers; only resume_matching.set_tags writes the three. `domains` NULL means
    # a resume tagged before groups existed: its one group is domain + roles.
    domain: Mapped[str | None] = mapped_column(String(80), nullable=True)
    roles: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    domains: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ResumeDataChange(Base):
    """Append-only history of how each structured resume field changed, and who changed it."""

    __tablename__ = "resume_data_changes"
    __table_args__ = (
        CheckConstraint(f"change_type IN ({', '.join(repr(v) for v in RESUME_CHANGE_TYPES)})",
                        name="ck_resume_data_changes_change_type"),
        CheckConstraint(f"source IN ({', '.join(repr(v) for v in RESUME_CHANGE_SOURCES)})",
                        name="ck_resume_data_changes_source"),
        Index("ix_resume_data_changes_resume_changed", "resume_id", "changed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("resume_data_changes")
    user: Mapped[User] = relationship(back_populates="resume_data_changes")
    resume_id: Mapped[int] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE", name="fk_resume_data_changes_resume_id"))
    field_path: Mapped[str] = mapped_column(String(300))
    item_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    old_value: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    change_type: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(10), default="USER")
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=now)


RESUME_PARSE_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED")
ACTIVE_RESUME_PARSE_STATUSES = ("PENDING", "PROCESSING")


class ResumeParseTask(Base):
    """One AI structuring of a resume version (app.services.resume_parsing), claimed by the scheduler.

    A resume has at most one PENDING or PROCESSING task (uq_resume_parse_tasks_active_resume). The task carries the
    text hash and the structured-data revision it was queued against: the result fills the data only while nobody
    has reviewed or edited it since, and is offered as a comparison otherwise. `staged` holds the grounded model
    output between steps (never a graph checkpoint); it is cleared once applied.
    """

    __tablename__ = "resume_parse_tasks"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(RESUME_PARSE_STATUSES)})", name="ck_resume_parse_tasks_status"),
        CheckConstraint("attempt_count >= 0", name="ck_resume_parse_tasks_attempt_count"),
        Index("uq_resume_parse_tasks_active_resume", "resume_id", unique=True,
              postgresql_where=text(f"status IN ({_values(ACTIVE_RESUME_PARSE_STATUSES)})"),
              sqlite_where=text(f"status IN ({_values(ACTIVE_RESUME_PARSE_STATUSES)})")),
        Index("ix_resume_parse_tasks_claim", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("resume_parse_tasks")
    user: Mapped[User] = relationship(back_populates="resume_parse_tasks")
    resume_id: Mapped[int] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE", name="fk_resume_parse_tasks_resume_id"), index=True)
    raw_text_hash: Mapped[str] = mapped_column(String(64))
    base_revision: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    reason: Mapped[str] = mapped_column(String(40), default="upload")  # upload | user_request
    step: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # filled | compare | kept | not_configured | gone (app.services.resume_parsing.RESULTS)
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    run_now_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    agent_thread_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_threads.id", ondelete="SET NULL", name="fk_resume_parse_tasks_agent_thread_id"),
        nullable=True)
    staged: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # What grounding dropped and kept, counts only: {"roles": 1, "bullets": 3, "skills": 2, "kept_roles": 5, ...}
    counts: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ResumeExtraction(Base):
    """How one resume version was read (app.services.resume_extraction, RESUME_EXTRACTION_V2): the reader, the quality
    report, where each section starts and how its heading was placed, and the parse view the regex parser reads.

    Derived and never authoritative: Resume.raw_text stays what the file says, and the view holds only its words with
    structure marked. Counts, kinds and the document's own headings; no data a person typed. One per version.
    """

    __tablename__ = "resume_extractions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("resume_extractions")
    user: Mapped[User] = relationship(back_populates="resume_extractions")
    resume_id: Mapped[int] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE", name="fk_resume_extractions_resume_id"), unique=True)
    pipeline_version: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(20))  # pdfium | pypdf | docx | text
    quality: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # QualityReport.summary()
    sections: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # sections.Heading.as_dict()
    parse_view: Mapped[str] = mapped_column(Text, default="")
    # Each line's look (glyph height, weight, left edge, style, list item, columns), compact, so the view can be
    # rebuilt from Resume.raw_text when a person assigns a heading; {"<line>": kind or "none"} are those choices.
    layout: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    overrides: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    # Corrections made to the text and values held back for review (Phases 5 – 6); empty until then.
    corrections: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    review: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ResumeExtractionFeedback(Base):
    """A person's decision about how a resume was read (a heading assigned to a section, a correction accepted or
    rejected), kept to learn the account's own headings and to measure the reader. Never another account's."""

    __tablename__ = "resume_extraction_feedback"
    __table_args__ = (Index("ix_resume_extraction_feedback_user_kind", "user_id", "kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("resume_extraction_feedback")
    user: Mapped[User] = relationship(back_populates="resume_extraction_feedback")
    resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL", name="fk_resume_extraction_feedback_resume_id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))  # section | correction | value
    subject: Mapped[str] = mapped_column(String(300))  # the heading or term as written
    before: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    after: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class KnowledgeAnswer(Base):
    __tablename__ = "knowledge_answers"
    __table_args__ = (UniqueConstraint("user_id", "question_key", name="uq_knowledge_user_question_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("knowledge_answers")
    user: Mapped[User] = relationship(back_populates="knowledge_answers")
    question: Mapped[str] = mapped_column(Text)
    question_key: Mapped[str] = mapped_column(String(64), index=True)
    answer: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(80), default="general")
    answer_type: Mapped[str] = mapped_column(String(40), default="text")
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    source_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    # A question an application form asked that nobody has answered yet (app.services.knowledge): the choices
    # the form offered, and which job and form field asked it.
    answer_options: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    request_context: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("evidence")
    user: Mapped[User] = relationship(back_populates="evidence")
    company: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(200), default="")
    claim: Mapped[str] = mapped_column(Text)
    context: Mapped[str] = mapped_column(Text, default="")
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    verified: Mapped[bool] = mapped_column(default=True)
    # Provenance. `resume` evidence is created only from structured resume items the user verified
    # (app.services.resume_verification); `fact_key` lets one fact be reused across resume versions.
    source: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual")  # manual | resume | llm_extraction
    resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL", name="fk_evidence_resume_id"), nullable=True, index=True)
    structured_item_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source_file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fact_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SearchProfile(Base):
    """A saved Hunter search the scheduler re-runs on its own cadence (app.services.hunter)."""

    __tablename__ = "search_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_search_profiles_user_name"),
        Index("ix_search_profiles_next_run_at", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("search_profiles")
    user: Mapped[User] = relationship(back_populates="search_profiles")
    name: Mapped[str] = mapped_column(String(120))
    roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    locations: Mapped[list[str]] = mapped_column(JSON, default=list)
    sites: Mapped[list[str]] = mapped_column(JSON, default=list)
    hours_old: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_results: Mapped[int] = mapped_column(Integer, default=20)
    remote_only: Mapped[bool] = mapped_column(Boolean, default=False)
    job_type: Mapped[str] = mapped_column(String(30), default="")
    country: Mapped[str] = mapped_column(String(60), default="")
    mode: Mapped[str] = mapped_column(String(20), default="primary")
    require_posted_date: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    run_every_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)



SCHEDULE_KINDS = ("manual", "interval", "daily", "weekly")


class SavedSearch(Base):
    """A named Discovery search setup: the whole search profile, plus its own schedule.

    The settings blob is exactly what app.workspace.SearchProfileIn produces, so a setup stays in
    step with the search form without a column per field.
    """

    __tablename__ = "saved_searches"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_saved_searches_user_name"),
        CheckConstraint(f"schedule_kind IN ({_values(SCHEDULE_KINDS)})", name="ck_saved_searches_schedule_kind"),
        Index("ix_saved_searches_next_run_at", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("saved_searches")
    user: Mapped[User] = relationship(back_populates="saved_searches")
    name: Mapped[str] = mapped_column(String(120))
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    schedule_kind: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual")
    every_hours: Mapped[int] = mapped_column(Integer, default=24, server_default="24")
    time_of_day: Mapped[str] = mapped_column(String(5), default="07:00", server_default="07:00")
    weekdays: Mapped[list[int]] = mapped_column(JSON, default=list)
    timezone: Mapped[str] = mapped_column(String(60), default="UTC", server_default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


JD_STATUSES = ("RAW", "NORMALIZED", "PARSED", "NEEDS_REVIEW", "MANUALLY_CORRECTED")
JD_PARSE_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED")
ACTIVE_JD_PARSE_STATUSES = ("PENDING", "PROCESSING")
JOB_CLASSIFICATION_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED")
ACTIVE_JOB_CLASSIFICATION_STATUSES = ("PENDING", "PROCESSING")
REQUIREMENT_TYPES = ("SKILL", "EXPERIENCE", "RESPONSIBILITY", "LEADERSHIP", "EDUCATION", "LOCATION",
                     "WORK_AUTHORIZATION", "VISA", "COMPENSATION", "DOMAIN", "SENIORITY", "OTHER")
REQUIREMENT_LEVELS = ("REQUIRED", "PREFERRED", "INFORMATIONAL", "UNKNOWN")
JOB_CHANGE_SOURCES = ("PARSER", "USER", "SYSTEM")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "source", "source_ref", name="uq_jobs_user_source_ref"),
        CheckConstraint(_in_check("state", WorkflowState), name="ck_jobs_state"),
        CheckConstraint(_in_check("previous_state", WorkflowState), name="ck_jobs_previous_state"),
        CheckConstraint(_in_check("error_category", ErrorCategory), name="ck_jobs_error_category"),
        CheckConstraint(f"structured_description_status IN ({_values(JD_STATUSES)})",
                        name="ck_jobs_structured_description_status"),
        CheckConstraint("attempt_count >= 0", name="ck_jobs_attempt_count"),
        CheckConstraint("(locked_at IS NULL AND locked_by IS NULL AND lock_token IS NULL AND lock_expires_at IS NULL)"
                        " OR (locked_at IS NOT NULL AND locked_by IS NOT NULL AND lock_token IS NOT NULL"
                        " AND lock_expires_at IS NOT NULL)", name="ck_jobs_lease"),
        Index("ix_jobs_workflow_claim", "state", "next_attempt_at"),
        Index("ix_jobs_lock_expires_at", "lock_expires_at"),
        Index("ix_jobs_compensation_checked_at", "compensation_checked_at"),
        # Hunter deduplication: board job ID, then canonical URL, then company + title + city.
        Index("ix_jobs_user_board_job_id", "user_id", "source_board", "source_job_id"),
        Index("ix_jobs_user_canonical_url", "user_id", "canonical_url"),
        Index("ix_jobs_user_canonical_apply_url", "user_id", "canonical_apply_url"),
        Index("ix_jobs_user_dedupe_key", "user_id", "dedupe_key"),
        Index("ix_jobs_posted_at", "posted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("jobs")
    source: Mapped[str] = mapped_column(String(50), default="manual")
    source_ref: Mapped[str] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(300))
    company: Mapped[str] = mapped_column(String(300))
    location: Mapped[str] = mapped_column(String(300), default="")
    url: Mapped[str] = mapped_column(String(1200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    fit_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    fit_analysis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(50), default="discovered")
    search_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    triaged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Why the candidate turned this job down (app.workspace REJECTION_REASONS). The reason is optional: a job
    # can be rejected without one, and then only rejected_at is set.
    rejection_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rejection_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Canonical workflow state (app.services.lifecycle). `status` above is the legacy dashboard status.
    state: Mapped[WorkflowState] = mapped_column(_enum_type(WorkflowState), default=WorkflowState.DISCOVERED,
                                                 server_default=WorkflowState.DISCOVERED.value)
    previous_state: Mapped[WorkflowState | None] = mapped_column(_enum_type(WorkflowState), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_category: Mapped[ErrorCategory | None] = mapped_column(_enum_type(ErrorCategory), nullable=True)
    retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Structured job description (app.services.job_description). `description` above stays the employer's raw text
    # and is never rewritten; these hold the cleaned text and the machine-readable version built from it.
    normalized_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_description: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    structured_description_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    structured_description_status: Mapped[str] = mapped_column(String(25), default="RAW", server_default="RAW")
    structured_description_parsed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The raw description the structured version was parsed from, and a re-parse waiting for the user's choices.
    structured_description_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    structured_description_candidate: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Description provenance (app.services.jd_recovery). `description` stays the text every workflow reads; these
    # say where it came from. NULL description_source is the text the job board sent (pre-provenance rows included).
    # A recovered posting that replaces a board's stub keeps the stub in original_description, never discarded.
    description_source: Mapped[str | None] = mapped_column(String(20), nullable=True)  # SOURCE | RECOVERED | USER
    description_quality: Mapped[str | None] = mapped_column(String(20), nullable=True)  # jd_quality.QUALITY_LABELS
    description_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    recovered_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_fetch_method: Mapped[str | None] = mapped_column(String(20), nullable=True)  # HTTP | PLAYWRIGHT | LLM_ASSISTED
    description_source_url: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    description_final_url: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    description_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    description_recovery_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Domain filter (app.services.domain_match): MATCH, NOT_MATCHING (hidden, never recovered, parsed or ranked),
    # KEPT (the person kept it anyway; never re-assessed) or PENDING (waiting for app.services.domain_classifier: held,
    # like a hidden job, from every worker). NULL = not assessed yet.
    domain_match: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    domain_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    domain_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # When the posting URL was last confirmed to still exist (app.services.job_removal.listing_check).
    listing_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The listing check found the posting closed but kept the job because the person approved or applied to it.
    # Cleared when a later check finds it open again.
    posting_closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    posting_closed_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # The resume ranking, tailoring and review use for this job. NULL = the account's active resume.
    resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL", name="fk_jobs_resume_id"), nullable=True, index=True)
    # How resume_id was chosen: AUTO (matched to resume tags), SETUP (the saved search pinned it), USER (chosen by the
    # person), ACTIVE (the person pinned the active resume). NULL = never chosen; the active resume is used.
    # Re-matching only ever changes AUTO and NULL jobs.
    resume_choice: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Automatic tailoring's decision for this job (app.services.auto_tailor), kept when task history is cleared:
    # QUEUED (it queued the job once) or DECLINED (the person cancelled a tailoring task for it). NULL = undecided;
    # only an undecided job is ever queued automatically.
    auto_tailor_status: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # The person starred this job to come back to it (Approvals). A bookmark only: it changes no workflow state.
    starred: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    # Salary enrichment (app.services.compensation): posting salary plus the status of other sources.
    compensation: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    compensation_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Hunter (app.services.hunter): the posting as the board stated it, and why this job was found.
    # posted_at is the employer's posting date and is never replaced by discovered_at; unknown stays NULL.
    source_board: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_job_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    direct_apply_url: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    canonical_apply_url: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(700), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    posted_date_raw: Mapped[str | None] = mapped_column(String(200), nullable=True)
    scraped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    job_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    remote_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    seniority: Mapped[str | None] = mapped_column(String(100), nullable=True)
    skills: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    experience: Mapped[str | None] = mapped_column(String(200), nullable=True)
    salary_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    salary_interval: Mapped[str | None] = mapped_column(String(20), nullable=True)
    salary_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    matched_search_role: Mapped[str | None] = mapped_column(String(300), nullable=True)
    matched_search_location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    apify_actor_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    apify_run_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    search_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_profiles.id", ondelete="SET NULL", name="fk_jobs_search_profile_id"), nullable=True, index=True)
    # Every search that found this posting: role, location, board, actor and run.
    discoveries: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    user: Mapped[User] = relationship(back_populates="jobs")
    resumes: Mapped[list[ResumeVariant]] = relationship(back_populates="job", cascade="all, delete-orphan")
    tailor_tasks: Mapped[list[TailorTask]] = relationship(back_populates="job", cascade="all, delete-orphan")
    ranking_tasks: Mapped[list[JobRankingTask]] = relationship(back_populates="job", cascade="all, delete-orphan",
                                                               passive_deletes=True)
    rankings: Mapped[list[JobRanking]] = relationship(back_populates="job", cascade="all, delete-orphan",
                                                      passive_deletes=True, order_by="JobRanking.version")
    requirements: Mapped[list[JobRequirementEvidence]] = relationship(back_populates="job", cascade="all, delete-orphan",
                                                                      passive_deletes=True)
    parse_tasks: Mapped[list[JobDescriptionParseTask]] = relationship(back_populates="job", cascade="all, delete-orphan",
                                                                      passive_deletes=True)
    fetch_tasks: Mapped[list[JobDescriptionFetchTask]] = relationship(back_populates="job", cascade="all, delete-orphan",
                                                                      passive_deletes=True)
    qualification_tasks: Mapped[list[JobQualificationTask]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True)
    preparation_tasks: Mapped[list[JobPreparationTask]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True)
    insight_tasks: Mapped[list[JobInsightTask]] = relationship(back_populates="job", cascade="all, delete-orphan",
                                                               passive_deletes=True)
    insight: Mapped[JobInsight | None] = relationship(back_populates="job", uselist=False, cascade="all, delete-orphan",
                                                      passive_deletes=True)
    application: Mapped[Application | None] = relationship(back_populates="job", uselist=False, cascade="all, delete-orphan")
    transitions: Mapped[list[WorkflowTransition]] = relationship(back_populates="job", cascade="all, delete-orphan",
                                                                 passive_deletes=True,
                                                                 order_by="WorkflowTransition.id")

    @validates("state")
    def _guard_state(self, _key: str, value: object) -> WorkflowState:
        """No arbitrary strings, and no state change that the lifecycle does not allow."""
        target = parse_state(value)
        current = getattr(self, "state", None)
        if current is not None and current != target:
            validate_transition(current, target, previous_state=self.previous_state)
        return target

    @validates("previous_state")
    def _guard_previous_state(self, _key: str, value: object) -> WorkflowState | None:
        return None if value is None else parse_state(value)


class JobRequirementEvidence(Base):
    """One requirement the posting states (or one signal the system derived), with the sentence it came from.

    `is_explicit` rows quote the employer; `is_derived` rows are the system's inference and are never presented as
    something the employer said.
    """

    __tablename__ = "job_requirement_evidence"
    __table_args__ = (
        CheckConstraint(f"requirement_type IN ({_values(REQUIREMENT_TYPES)})",
                        name="ck_job_requirement_evidence_type"),
        CheckConstraint(f"requirement_level IN ({_values(REQUIREMENT_LEVELS)})",
                        name="ck_job_requirement_evidence_level"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_job_requirement_evidence_confidence"),
        CheckConstraint("origin IN ('PARSER', 'USER')", name="ck_job_requirement_evidence_origin"),
        Index("ix_job_requirement_evidence_job_type", "job_id", "requirement_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_requirement_evidence")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE", name="fk_job_requirement_evidence_job_id"), index=True)
    item_id: Mapped[str] = mapped_column(String(60))  # stable within one parse, e.g. "req_001"
    requirement_type: Mapped[str] = mapped_column(String(25))
    normalized_value: Mapped[str] = mapped_column(String(300))
    requirement_level: Mapped[str] = mapped_column(String(15), default="UNKNOWN")
    source_text: Mapped[str] = mapped_column(Text, default="")
    source_section: Mapped[str | None] = mapped_column(String(120), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    is_explicit: Mapped[bool] = mapped_column(Boolean, default=True)
    is_derived: Mapped[bool] = mapped_column(Boolean, default=False)
    # Who settled this row's level: the parser, or the user (a correction or a re-parse decision). A parser never
    # replaces a USER row.
    origin: Mapped[str] = mapped_column(String(10), default="PARSER", server_default="PARSER")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    job: Mapped[Job] = relationship(back_populates="requirements")
    user: Mapped[User] = relationship(back_populates="job_requirements")


class JobStructuredDataChange(Base):
    """Append-only history of how a job's structured description changed, and who changed it."""

    __tablename__ = "job_structured_data_changes"
    __table_args__ = (
        CheckConstraint(f"change_source IN ({_values(JOB_CHANGE_SOURCES)})",
                        name="ck_job_structured_data_changes_source"),
        Index("ix_job_structured_data_changes_job_created", "job_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_structured_data_changes")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE", name="fk_job_structured_data_changes_job_id"))
    field_path: Mapped[str] = mapped_column(String(300))
    old_value: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    change_source: Mapped[str] = mapped_column(String(10), default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    job: Mapped[Job] = relationship()
    user: Mapped[User] = relationship(back_populates="job_structured_changes")


class JobClassificationTask(Base):
    """One queued read of a job by the domain classifier, and afterwards the record of why it was kept or hidden.

    At most one active task per job (uq_job_classification_tasks_active_job). `mode` is ON (its verdict moves the job)
    or SHADOW (recorded for comparison, the job is never touched). `model_classification` is the model's answer,
    `classification` what code made of it, and `gate_code` why they differ. Metadata only: no prompt or posting text.
    """

    __tablename__ = "job_classification_tasks"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(JOB_CLASSIFICATION_STATUSES)})", name="ck_job_classification_tasks_status"),
        CheckConstraint("attempt_count >= 0", name="ck_job_classification_tasks_attempt_count"),
        Index("uq_job_classification_tasks_active_job", "job_id", unique=True,
              postgresql_where=text(f"status IN ({_values(ACTIVE_JOB_CLASSIFICATION_STATUSES)})"),
              sqlite_where=text(f"status IN ({_values(ACTIVE_JOB_CLASSIFICATION_STATUSES)})")),
        Index("ix_job_classification_tasks_claim", "status", "next_attempt_at"),
        Index("ix_job_classification_tasks_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_classification_tasks")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE", name="fk_job_classification_tasks_job_id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    mode: Mapped[str] = mapped_column(String(10), default="ON")
    reason: Mapped[str] = mapped_column(String(40), default="new")  # new | recheck | description_changed | user_request
    route: Mapped[str] = mapped_column(String(20), default="ASK")  # HARD | POSITIVE | ASK
    signals: Mapped[list[str]] = mapped_column(JSON, default=list)  # the doubts that sent it to the model
    title_only: Mapped[bool] = mapped_column(Boolean, default=False)
    legacy_match: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # what the old filter says (comparison)
    legacy_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    model_classification: Mapped[str | None] = mapped_column(String(20), nullable=True)
    classification: Mapped[str | None] = mapped_column(String(20), nullable=True)
    explanation: Mapped[str | None] = mapped_column(String(300), nullable=True)
    matched_skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    irrelevance_evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    gate_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    prompt_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    run_now_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates="job_classification_tasks")


class JobClassificationCache(Base):
    """The model's answer for one candidate × posting window × prompt version, so a repost or a duplicate costs nothing.

    Per account, kept after the job is deleted, and read back through the evidence gate like a fresh answer. The key is
    a hash, and no prompt or posting text is stored."""

    __tablename__ = "job_classification_cache"
    __table_args__ = (UniqueConstraint("user_id", "cache_key", name="uq_job_classification_cache_user_key"),
                      Index("ix_job_classification_cache_expires", "expires_at"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_classification_cache")
    cache_key: Mapped[str] = mapped_column(String(64))
    answer: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    prompt_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    model: Mapped[str | None] = mapped_column(String(300), nullable=True)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)

    user: Mapped[User] = relationship(back_populates="job_classification_cache")


class JobDescriptionParseTask(Base):
    """One queued parse of a job description, claimed by the scheduler like a ranking task.

    A job has at most one PENDING or PROCESSING task at a time (uq_jd_parse_tasks_active_job), and a task carries
    the hash of the description it was created for, so the same text is never queued twice.
    """

    __tablename__ = "job_description_parse_tasks"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(JD_PARSE_STATUSES)})", name="ck_jd_parse_tasks_status"),
        CheckConstraint("attempt_count >= 0", name="ck_jd_parse_tasks_attempt_count"),
        Index("uq_jd_parse_tasks_active_job", "job_id", unique=True,
              postgresql_where=text(f"status IN ({_values(ACTIVE_JD_PARSE_STATUSES)})"),
              sqlite_where=text(f"status IN ({_values(ACTIVE_JD_PARSE_STATUSES)})")),
        Index("ix_jd_parse_tasks_claim", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_description_parse_tasks")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE", name="fk_jd_parse_tasks_job_id"), index=True)
    description_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    reason: Mapped[str] = mapped_column(String(40), default="new")  # new | user_request | description_changed
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_category: Mapped[ErrorCategory | None] = mapped_column(_enum_type(ErrorCategory), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Asked to run now from Running workers (app.services.workers): claimed first, without waiting. Cleared on claim.
    run_now_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    job: Mapped[Job] = relationship(back_populates="parse_tasks")
    user: Mapped[User] = relationship(back_populates="job_parse_tasks")


JD_FETCH_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED", "BLOCKED", "SKIPPED")
ACTIVE_JD_FETCH_STATUSES = ("PENDING", "PROCESSING")
JD_FETCH_METHODS = ("HTTP", "PLAYWRIGHT", "LLM_ASSISTED")


class JobDescriptionFetchTask(Base):
    """One attempt to recover a job's description from its posting URL, claimed by the scheduler.

    A job has at most one PENDING or PROCESSING task (uq_jd_fetch_tasks_active_job). Finished tasks are kept: they are
    the job's recovery history and the source of the recovery metrics in Monitoring.
    """

    __tablename__ = "job_description_fetch_tasks"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(JD_FETCH_STATUSES)})", name="ck_jd_fetch_tasks_status"),
        CheckConstraint("attempt_count >= 0", name="ck_jd_fetch_tasks_attempt_count"),
        CheckConstraint(f"fetch_method IS NULL OR fetch_method IN ({_values(JD_FETCH_METHODS)})",
                        name="ck_jd_fetch_tasks_fetch_method"),
        Index("uq_jd_fetch_tasks_active_job", "job_id", unique=True,
              postgresql_where=text(f"status IN ({_values(ACTIVE_JD_FETCH_STATUSES)})"),
              sqlite_where=text(f"status IN ({_values(ACTIVE_JD_FETCH_STATUSES)})")),
        Index("ix_jd_fetch_tasks_claim", "status", "next_attempt_at"),
        Index("ix_jd_fetch_tasks_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_description_fetch_tasks")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE", name="fk_jd_fetch_tasks_job_id"), index=True)
    source_url: Mapped[str] = mapped_column(String(1200), default="")
    final_url: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    reason: Mapped[str] = mapped_column(String(40), default="auto")  # auto | user_request | backlog
    # Why the task ended the way it did: RECOVERED, UNCHANGED, KEPT_EXISTING, JOB_REMOVED, LOGIN_REQUIRED, ...
    result_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    fetch_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_category: Mapped[ErrorCategory | None] = mapped_column(_enum_type(ErrorCategory), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    redirect_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recovered_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    playwright_used: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    llm_used: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    llm_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Safe per-stage notes (extractor, confidence, validation reasons); never page text.
    diagnostics: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Asked to run now from Running workers (app.services.workers): claimed first, without waiting. Cleared on claim.
    run_now_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    job: Mapped[Job] = relationship(back_populates="fetch_tasks")
    user: Mapped[User] = relationship(back_populates="job_fetch_tasks")


JOB_INSIGHT_TASK_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "PARTIAL", "FAILED", "SKIPPED")
ACTIVE_JOB_INSIGHT_TASK_STATUSES = ("PENDING", "PROCESSING")
JOB_INSIGHT_TASK_REASONS = ("AUTO_APPROVED", "USER_REQUEST", "REFRESH", "BACKLOG")
JOB_INSIGHT_STATUSES = ("NONE", "COMPLETED", "PARTIAL", "FAILED")
INSIGHT_CATEGORIES = ("COMPENSATION", "COMPANY", "VALUES", "CULTURE", "SENTIMENT", "INTERVIEW_PROCESS",
                      "INTERVIEW_QUESTION")
INSIGHT_SOURCE_TYPES = ("EMPLOYER", "JOB_DESCRIPTION", "RECRUITER", "OFFICIAL_COMPANY", "GLASSDOOR", "PAYSCALE",
                        "INDEED", "LEVELS_FYI", "PUBLIC_WEB")
INSIGHT_MATCH_STATUSES = ("ACCEPTED", "NEEDS_REVIEW", "REJECTED")


class JobInsightTask(Base):
    """One queued research run for a job's insights (app.services.job_insights), claimed by the scheduler.

    A job has at most one PENDING or PROCESSING task (uq_job_insight_tasks_active_job), so approval, a person's
    Refresh and the backlog sweep can never research the same job twice at once. Finished tasks are kept: they say
    which sources answered (`source_status`) and why a run failed.
    """

    __tablename__ = "job_insight_tasks"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(JOB_INSIGHT_TASK_STATUSES)})", name="ck_job_insight_tasks_status"),
        CheckConstraint(f"reason IN ({_values(JOB_INSIGHT_TASK_REASONS)})", name="ck_job_insight_tasks_reason"),
        CheckConstraint("attempt_count >= 0", name="ck_job_insight_tasks_attempt_count"),
        Index("uq_job_insight_tasks_active_job", "job_id", unique=True,
              postgresql_where=text(f"status IN ({_values(ACTIVE_JOB_INSIGHT_TASK_STATUSES)})"),
              sqlite_where=text(f"status IN ({_values(ACTIVE_JOB_INSIGHT_TASK_STATUSES)})")),
        Index("ix_job_insight_tasks_claim", "status", "next_attempt_at"),
        Index("ix_job_insight_tasks_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_insight_tasks")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE", name="fk_job_insight_tasks_job_id"), index=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL", name="fk_job_insight_tasks_application_id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    reason: Mapped[str] = mapped_column(String(20), default="AUTO_APPROVED")
    # A person's Refresh: researched again even when cached observations are still fresh.
    force_refresh: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_category: Mapped[ErrorCategory | None] = mapped_column(_enum_type(ErrorCategory), nullable=True)
    # {source name: {status, detail, observations, cached, fetched_at}}: what each source returned on this run.
    source_status: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Asked to run now from Running workers (app.services.workers): claimed first, without waiting. Cleared on claim.
    run_now_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # JOB_INSIGHTS_GRAPH_MODE=on (app.agents.job_insights): the graph thread running this task, and the validated
    # synthesis between SYNTHESIZE_INSIGHTS and PERSIST_INSIGHTS (kept out of the checkpoint; cleared on settle).
    agent_thread_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_threads.id", ondelete="SET NULL", name="fk_job_insight_tasks_agent_thread_id"),
        nullable=True, index=True)
    staged: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # FULL (research, then every model section) or APPLICATION_FORM (only the application-form drafts: no source is
    # called and no other section is written). NULL on rows from before the column means FULL.
    scope: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    job: Mapped[Job] = relationship(back_populates="insight_tasks")
    user: Mapped[User] = relationship(back_populates="job_insight_tasks")


class JobInsight(Base):
    """A job's research package: one row per job, rewritten section by section as research succeeds.

    Every list item carries `kind` (FACT, DERIVED, GENERATED or UNKNOWN), and every FACT names the observations it rests
    on (`source_ids` → insight_observations.id). A section is only replaced by a run that produced it, so a failed
    refresh never erases what the last good run found. `recruiter_input` is the person's own entry and research never
    writes it.
    """

    __tablename__ = "job_insights"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(JOB_INSIGHT_STATUSES)})", name="ck_job_insights_status"),
        UniqueConstraint("job_id", name="uq_job_insights_job_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_insights")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE", name="fk_job_insights_job_id"))
    status: Mapped[str] = mapped_column(String(20), default="NONE")
    # Identity the research matched against (app.services.job_insights.matching), never inferred by a model.
    company_name: Mapped[str] = mapped_column(String(300), default="")
    canonical_company: Mapped[str] = mapped_column(String(300), default="")
    company_domain: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # The company's own website: from the posting's data, or entered by the person. Never guessed.
    company_website: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    company_website_source: Mapped[str | None] = mapped_column(String(20), nullable=True)  # POSTING | USER
    original_title: Mapped[str] = mapped_column(String(300), default="")
    normalized_title: Mapped[str] = mapped_column(String(300), default="")
    seniority: Mapped[str | None] = mapped_column(String(40), nullable=True)
    discipline: Mapped[str | None] = mapped_column(String(60), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    country: Mapped[str | None] = mapped_column(String(120), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Sections (each written whole, only by a run that produced it).
    compensation: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    company: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    interview_process: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    reported_questions: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    generated_questions: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    questions_to_ask: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    focus_areas: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    value_prep: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    compensation_prep: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    risks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    unknowns: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # Application-form questions with what to answer (app.services.job_insights.application_form): the person's
    # verified library answer, a draft grounded in verified evidence, or NEEDS_INPUT. Never saved as an answer.
    application_questions: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # The questions the person pasted from the real form: [{id, question, added_at}]. Research reads it, never writes it.
    application_form_input: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # The person's own notes from the recruiter: {recruiter_name, budget_low, budget_high, currency, period, basis,
    # interview_process, recruiter_questions, comments}. Research reads it and never writes it.
    recruiter_input: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # What produced the generated sections: {company_insights: v1, interview_insights: v1}, the model, a confidence.
    prompt_versions: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # first successful run
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # latest successful run
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)  # the latest run's failure, if any
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    job: Mapped[Job] = relationship(back_populates="insight")
    user: Mapped[User] = relationship(back_populates="job_insights")
    observations: Mapped[list[InsightObservation]] = relationship(
        back_populates="insight", cascade="all, delete-orphan", passive_deletes=True, order_by="InsightObservation.id")


class InsightObservation(Base):
    """One researched fact as its source stated it: a salary range, a stated value, a reported interview question.

    Concise normalized extracts only (`data`), never a page or a review copied whole. Provenance travels with it:
    source type, provider, actor, URL or run reference, when it was observed and fetched, how well it matched this
    job's company, role and location, and whether that match was accepted.
    """

    __tablename__ = "insight_observations"
    __table_args__ = (
        CheckConstraint(f"category IN ({_values(INSIGHT_CATEGORIES)})", name="ck_insight_observations_category"),
        CheckConstraint(f"source_type IN ({_values(INSIGHT_SOURCE_TYPES)})",
                        name="ck_insight_observations_source_type"),
        CheckConstraint(f"match_status IN ({_values(INSIGHT_MATCH_STATUSES)})",
                        name="ck_insight_observations_match_status"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_insight_observations_confidence"),
        Index("ix_insight_observations_insight_category", "job_insight_id", "category"),
        Index("ix_insight_observations_cache", "user_id", "cache_key", "fetched_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("insight_observations")
    job_insight_id: Mapped[int] = mapped_column(
        ForeignKey("job_insights.id", ondelete="CASCADE", name="fk_insight_observations_job_insight_id"), index=True)
    category: Mapped[str] = mapped_column(String(30))
    source_type: Mapped[str] = mapped_column(String(30))
    source_name: Mapped[str] = mapped_column(String(40))  # the adapter that produced it, e.g. glassdoor_compensation
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)  # APIFY | WEB | None (stored data)
    actor_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    source_reference: Mapped[str | None] = mapped_column(String(300), nullable=True)  # e.g. apify run/dataset id
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    match_scope: Mapped[str | None] = mapped_column(String(30), nullable=True)
    match_status: Mapped[str] = mapped_column(String(20), default="ACCEPTED")
    match_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    recency: Mapped[str | None] = mapped_column(String(10), nullable=True)  # HIGH | MEDIUM | LOW | UNKNOWN
    observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # company/role/location/source: an observation another job of the same account can reuse while it is fresh.
    cache_key: Mapped[str | None] = mapped_column(String(400), nullable=True)
    # Replaced by a newer answer from the same source. Kept only while a section kept from an earlier run still cites
    # it (a refresh whose model step failed), so a displayed fact never loses its provenance; never used as current data.
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    insight: Mapped[JobInsight] = relationship(back_populates="observations")
    user: Mapped[User] = relationship(back_populates="insight_observations")


class RemovedPosting(Base):
    """A posting deleted because it no longer exists (app.services.job_removal). The next import of the same
    posting is skipped, so a dead job does not come back with every search."""

    __tablename__ = "removed_postings"
    __table_args__ = (Index("ix_removed_postings_user_ref", "user_id", "source", "source_ref"),
                      Index("ix_removed_postings_user_url", "user_id", "canonical_url"),
                      Index("ix_removed_postings_user_dedupe", "user_id", "dedupe_key"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("removed_postings")
    source: Mapped[str] = mapped_column(String(50), default="")
    source_ref: Mapped[str] = mapped_column(String(500), default="")
    canonical_url: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(700), nullable=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    company: Mapped[str] = mapped_column(String(300), default="")
    reason: Mapped[str] = mapped_column(String(40), default="JOB_REMOVED")
    removed_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)

    user: Mapped[User] = relationship(back_populates="removed_postings")


# BULK_DELETED: a cleanup of many jobs at once. Remembered, but it says nothing about the person's taste (never learned).
FEEDBACK_ACTIONS = ("DISMISSED", "REJECTED", "DELETED", "SHORTLISTED", "TAILORED", "APPROVED", "APPLIED", "KEPT",
                    "BULK_DELETED")
NEGATIVE_FEEDBACK = ("DISMISSED", "REJECTED", "DELETED")


class JobFeedbackEvent(Base):
    """What the person did with a job, kept after the job itself is deleted: the domain filter learns from it
    (app.services.feedback_learning). Only the terms needed to explain a learned rule are stored."""

    __tablename__ = "job_feedback_events"
    __table_args__ = (CheckConstraint(f"action IN ({_values(FEEDBACK_ACTIONS)})", name="ck_job_feedback_events_action"),
                      Index("ix_job_feedback_events_user_created", "user_id", "created_at"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_feedback_events")
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the job may be deleted afterwards
    action: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(300), default="")
    company: Mapped[str] = mapped_column(String(300), default="")
    terms: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    user: Mapped[User] = relationship(back_populates="job_feedback")


class WorkflowTransition(Base):
    """Append-only history of every workflow state change."""

    __tablename__ = "workflow_transitions"
    __table_args__ = (
        CheckConstraint(_in_check("from_state", WorkflowState), name="ck_workflow_transitions_from_state"),
        CheckConstraint(_in_check("to_state", WorkflowState), name="ck_workflow_transitions_to_state"),
        CheckConstraint(_in_check("error_category", ErrorCategory), name="ck_workflow_transitions_error_category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = _owner("workflow_transitions")
    from_state: Mapped[WorkflowState | None] = mapped_column(_enum_type(WorkflowState), nullable=True)
    to_state: Mapped[WorkflowState] = mapped_column(_enum_type(WorkflowState))
    actor: Mapped[str] = mapped_column(String(200), default="system")
    reason: Mapped[str] = mapped_column(Text, default="")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    error_category: Mapped[ErrorCategory | None] = mapped_column(_enum_type(ErrorCategory), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    job: Mapped[Job] = relationship(back_populates="transitions")
    user: Mapped[User] = relationship(back_populates="transitions")


class SchedulerJob(Base):
    """One row per scheduled job: the lease that stops two app instances running it at once, and its last run."""

    __tablename__ = "scheduler_jobs"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_trigger: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    last_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failure_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class SearchRun(Base):
    __tablename__ = "search_runs"
    __table_args__ = (UniqueConstraint("user_id", "external_run_id", name="uq_search_runs_user_external_run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("search_runs")
    user: Mapped[User] = relationship(back_populates="search_runs")
    provider: Mapped[str] = mapped_column(String(50), default="apify")
    external_run_id: Mapped[str] = mapped_column(String(200), index=True)
    actor_id: Mapped[str] = mapped_column(String(300), default="")
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(50), default="READY")
    dataset_id: Mapped[str] = mapped_column(String(200), default="")
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    created_jobs: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Hunter run tracking (app.services.hunter). Runs started elsewhere leave these empty.
    adapter: Mapped[str | None] = mapped_column(String(50), nullable=True)
    hunter_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    search_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_profiles.id", ondelete="SET NULL", name="fk_search_runs_search_profile_id"),
        nullable=True, index=True)
    roles: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    sites: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    search_params: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fallback_for_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # The saved Discovery setup that started this run (app.services.saved_searches). Empty for the rest.
    saved_search_id: Mapped[int | None] = mapped_column(
        ForeignKey("saved_searches.id", ondelete="SET NULL", name="fk_search_runs_saved_search_id"),
        nullable=True, index=True)

    # What Apify itself reported about the run (app.services.sources). NULL whenever Apify did not report it:
    # nothing here is guessed.
    compute_units: Mapped[float | None] = mapped_column(Float, nullable=True)
    usage_total_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8), nullable=True)
    memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dataset_item_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ResumeVariant(Base):
    __tablename__ = "resume_variants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = _owner("resume_variants")
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    markdown: Mapped[str] = mapped_column(Text, default="")
    cover_letter: Mapped[str] = mapped_column(Text, default="")
    reviewer_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    reviewer_notes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    job: Mapped[Job] = relationship(back_populates="resumes")
    user: Mapped[User] = relationship(back_populates="resume_variants")


class TailorTask(Base):
    """One queued, running or finished background tailoring run for a job (app.services.tailor_tasks)."""

    __tablename__ = "tailor_tasks"
    __table_args__ = (
        CheckConstraint("mode IN ('tailor', 'skip', 'review')", name="ck_tailor_tasks_mode"),
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name="ck_tailor_tasks_status"),
        Index("ix_tailor_tasks_status", "status", "id"),
        # At most one queued or running task per job, so two paths queueing the same job at once cannot both win.
        Index("uq_tailor_tasks_active_job", "job_id", unique=True,
              postgresql_where=text("status IN ('queued', 'running')"),
              sqlite_where=text("status IN ('queued', 'running')")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = _owner("tailor_tasks")
    # tailor | skip (the master resume unchanged, then reviewed) | review (only review the latest saved packet)
    mode: Mapped[str] = mapped_column(String(10), default="tailor")
    regenerate: Mapped[bool] = mapped_column(Boolean, default=False)
    # How to tailor this job: {"pages": 1 | 2 | 3} (app.services.resume_length). None = the account's default.
    options: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    stage: Mapped[str] = mapped_column(String(20), default="queued")  # queued | ranking | tailoring | reviewing | done
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Asked to run now from Running workers (app.services.workers): claimed first, without waiting. Cleared on claim.
    run_now_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    job: Mapped[Job] = relationship(back_populates="tailor_tasks")
    user: Mapped[User] = relationship(back_populates="tailor_tasks")


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), unique=True)
    user_id: Mapped[int] = _owner("applications")
    status: Mapped[str] = mapped_column(String(50), default="pending_approval")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    followup_due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recruiter_name: Mapped[str] = mapped_column(String(200), default="")
    recruiter_email: Mapped[str] = mapped_column(String(300), default="")
    # Every recruiter for this application: [{name, email, phone}]. The first is mirrored above (app.services.contacts).
    contacts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    approved_resume_id: Mapped[int | None] = mapped_column(ForeignKey("resume_variants.id", ondelete="SET NULL"), nullable=True)
    # Approved deliberately although the reviewer scored the packet below the account's minimum, with the
    # reason the person gave. The score itself stays on the approved resume variant.
    approval_override: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    approval_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Submission (app.services.apply_queue): the reference the portal showed, who submitted it, and why the last
    # attempt stopped short of submitting ({reason, detail, question_ids, recorded_at}; cleared on submission).
    confirmation_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    submitted_via: Mapped[str | None] = mapped_column(String(40), nullable=True)
    apply_blocker: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Post-submission tracker column: applied, acknowledged, screening, interviewing, ..., closed.
    stage: Mapped[str | None] = mapped_column(String(50), default="applied", nullable=True)
    # Archived off the tracker board by the person (app.services.tracker.archive_older_than); NULL when on the board.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    job: Mapped[Job] = relationship(back_populates="application")
    user: Mapped[User] = relationship(back_populates="applications")
    followups: Mapped[list[FollowUp]] = relationship(back_populates="application", cascade="all, delete-orphan")
    events: Mapped[list[ApplicationEvent]] = relationship(back_populates="application", cascade="all, delete-orphan",
                                                          order_by="ApplicationEvent.occurred_at")


class ApplicationEvent(Base):
    __tablename__ = "application_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = _owner("application_events")
    kind: Mapped[str] = mapped_column(String(40), default="note")
    title: Mapped[str] = mapped_column(String(300), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # Interviews: the time zone they were scheduled in and an optional contact (timezone, contact_name,
    # contact_email, contact_phone).
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    application: Mapped[Application] = relationship(back_populates="events")
    user: Mapped[User] = relationship(back_populates="application_events")


class FollowUp(Base):
    __tablename__ = "followups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = _owner("followups")
    due_at: Mapped[datetime] = mapped_column(DateTime)
    channel: Mapped[str] = mapped_column(String(30), default="email")
    draft: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="draft")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # What produced the draft: prompt version, provider, model, AI config version. NULL on drafts made before it existed.
    generation: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    application: Mapped[Application] = relationship(back_populates="followups")
    user: Mapped[User] = relationship(back_populates="followups")


# ───────────── Application Inbox (app.services.application_inbox) ─────────────

GMAIL_CONNECTION_STATUSES = ("CONNECTED", "CONNECTION_REQUIRED", "REVOKED", "ERROR")
EMAIL_MATCH_STATUSES = ("MATCHED", "NEEDS_REVIEW", "UNMATCHED", "IGNORED")
EMAIL_MATCH_METHODS = ("DETERMINISTIC", "LLM_ASSISTED", "USER", "NONE")
EMAIL_ANALYSIS_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED")
EMAIL_EVENT_TYPES = (
    "APPLICATION_CONFIRMATION", "RECRUITER_RESPONSE", "INTERVIEW_INVITATION", "INTERVIEW_RESCHEDULE",
    "ASSESSMENT_INVITATION", "ASSESSMENT_REMINDER", "ADDITIONAL_INFORMATION_REQUEST", "DOCUMENT_REQUEST",
    "FOLLOWUP_RESPONSE", "APPLICATION_ON_HOLD", "APPLICATION_REJECTION", "OFFER", "OFFER_UPDATE", "BACKGROUND_CHECK",
    "ONBOARDING", "GENERAL_RECRUITING_UPDATE", "AUTOMATED_NOTIFICATION", "UNKNOWN")
EMAIL_STAGE_DECISIONS = ("APPLIED", "SUGGESTED", "NONE", "INVALID")
EMAIL_REVIEW_STATUSES = ("OPEN", "ACCEPTED", "DISMISSED")


class GmailConnection(Base):
    """An account's read-only Gmail connection. Only the OAuth refresh token is kept, encrypted with SECRET_KEY
    (app.security); access tokens live in memory for one sync and are never stored or logged."""

    __tablename__ = "gmail_connections"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(GMAIL_CONNECTION_STATUSES)})", name="ck_gmail_connections_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("gmail_connections", unique=True)
    email_address: Mapped[str] = mapped_column(String(320), default="")
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    scopes: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(30), default="CONNECTED")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Gmail's mailbox history id at the last successful sync: the incremental checkpoint.
    history_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # One sync per account at a time (a guarded UPDATE takes it; an expired lease is taken over).
    sync_locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sync_lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Where the running (or last) sync is: {phase, label, done, total, percent, started_at, updated_at}. Counts only,
    # never message content; read by GET /api/application-inbox/status for the progress bar.
    sync_progress: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    user: Mapped[User] = relationship(back_populates="gmail_connection")


class ApplicationEmailMessage(Base):
    """One Gmail message the inbox worker has seen: the deduplication record and the analysis queue.

    Saved before any model call, so a failed analysis never loses the message. Unmatched messages keep metadata only.
    """

    __tablename__ = "application_email_messages"
    __table_args__ = (
        UniqueConstraint("user_id", "gmail_message_id", name="uq_application_email_messages_user_message"),
        CheckConstraint(f"match_status IN ({_values(EMAIL_MATCH_STATUSES)})",
                        name="ck_application_email_messages_match_status"),
        CheckConstraint(f"match_method IN ({_values(EMAIL_MATCH_METHODS)})",
                        name="ck_application_email_messages_match_method"),
        CheckConstraint(f"analysis_status IN ({_values(EMAIL_ANALYSIS_STATUSES)})",
                        name="ck_application_email_messages_analysis_status"),
        Index("ix_application_email_messages_analysis", "analysis_status", "next_attempt_at"),
        Index("ix_application_email_messages_thread", "user_id", "gmail_thread_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("application_email_messages")
    gmail_message_id: Mapped[str] = mapped_column(String(64))
    gmail_thread_id: Mapped[str] = mapped_column(String(64), default="")
    sender: Mapped[str] = mapped_column(String(500), default="")
    sender_domain: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(String(1000), default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    snippet: Mapped[str] = mapped_column(Text, default="")
    # The relevant part of the body (quoted replies removed), redacted and capped. Empty for unmatched mail.
    relevant_text: Mapped[str] = mapped_column(Text, default="")
    automated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL", name="fk_application_email_messages_application_id"),
        nullable=True, index=True)
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    match_signals: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    match_method: Mapped[str] = mapped_column(String(20), default="NONE")
    match_status: Mapped[str] = mapped_column(String(20), default="UNMATCHED")
    # The other applications the email could belong to, when the match is ambiguous: [{application_id, score}].
    match_candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    analysis_status: Mapped[str] = mapped_column(String(20), default="SKIPPED")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    user: Mapped[User] = relationship(back_populates="application_email_messages")


class ApplicationCommunicationEvent(Base):
    """What one matched email means for an application: the Email activity timeline entry.

    The model extracts; code decides (stage_decision). A person's correction (user_corrected) is never overwritten.
    """

    __tablename__ = "application_communication_events"
    __table_args__ = (
        UniqueConstraint("user_id", "gmail_message_id", name="uq_application_communication_events_user_message"),
        CheckConstraint(f"event_type IN ({_values(EMAIL_EVENT_TYPES)})",
                        name="ck_application_communication_events_event_type"),
        CheckConstraint(f"stage_decision IN ({_values(EMAIL_STAGE_DECISIONS)})",
                        name="ck_application_communication_events_stage_decision"),
        CheckConstraint(f"review_status IN ({_values(EMAIL_REVIEW_STATUSES)})",
                        name="ck_application_communication_events_review_status"),
        Index("ix_application_communication_events_user_received", "user_id", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("application_communication_events")
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE", name="fk_application_communication_events_application_id"),
        nullable=True, index=True)
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("application_email_messages.id", ondelete="SET NULL",
                   name="fk_application_communication_events_message_id"), nullable=True)
    gmail_message_id: Mapped[str] = mapped_column(String(64))
    gmail_thread_id: Mapped[str] = mapped_column(String(64), default="")
    event_type: Mapped[str] = mapped_column(String(40), default="UNKNOWN")
    sender: Mapped[str] = mapped_column(String(500), default="")
    subject: Mapped[str] = mapped_column(String(1000), default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    requires_action: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    action_items: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    interview: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    assessment: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    recruiter: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    important_facts: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    classification_method: Mapped[str] = mapped_column(String(10), default="RULE")  # RULE | LLM | USER
    detected_stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    recommended_tracker_action: Mapped[str | None] = mapped_column(String(60), nullable=True)
    stage_decision: Mapped[str] = mapped_column(String(20), default="NONE")
    stage_from: Mapped[str | None] = mapped_column(String(50), nullable=True)
    stage_to: Mapped[str | None] = mapped_column(String(50), nullable=True)
    stage_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    review_status: Mapped[str] = mapped_column(String(20), default="OPEN")
    user_corrected: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Each correction a person made: [{field, old, new, at}].
    corrections: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # The tracker timeline entry (ApplicationEvent) this email produced, kept in step with corrections.
    application_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    user: Mapped[User] = relationship(back_populates="application_communication_events")


class ApplicationInboxDigest(Base):
    """The daily Application Inbox digest: one per account and day, rewritten by later syncs that day."""

    __tablename__ = "application_inbox_digests"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_application_inbox_digests_user_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("application_inbox_digests")
    date: Mapped[date] = mapped_column(Date)
    applications_checked: Mapped[int] = mapped_column(Integer, default=0)
    messages_scanned: Mapped[int] = mapped_column(Integer, default=0)
    messages_matched: Mapped[int] = mapped_column(Integer, default=0)
    new_updates: Mapped[int] = mapped_column(Integer, default=0)
    needs_action_count: Mapped[int] = mapped_column(Integer, default=0)
    interviews: Mapped[int] = mapped_column(Integer, default=0)
    assessments: Mapped[int] = mapped_column(Integer, default=0)
    rejections: Mapped[int] = mapped_column(Integer, default=0)
    offers: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    # needs_attention, stage_changes, new_responses, upcoming, waiting — built from the database, never by a model.
    sections: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # The day's sync runs and their counters, for Monitoring.
    sync_runs: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    user: Mapped[User] = relationship(back_populates="application_inbox_digests")


class ResumeVersion(Base):
    """An immutable snapshot of the resume text a candidate profile used, one row per distinct content.

    Rankings point at the snapshot they were made with, so they stay reproducible after the resume is edited,
    replaced or its uploaded file is deleted.
    """

    __tablename__ = "resume_versions"
    __table_args__ = (
        UniqueConstraint("candidate_profile_id", "content_hash", name="uq_resume_versions_profile_hash"),
        UniqueConstraint("candidate_profile_id", "version", name="uq_resume_versions_profile_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("resume_versions")
    candidate_profile_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE", name="fk_resume_versions_candidate_profile_id"),
        index=True)
    # The uploaded file the text came from, when it matches one. The snapshot outlives the file.
    resume_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("resume_documents.id", ondelete="SET NULL", name="fk_resume_versions_resume_document_id"),
        nullable=True)
    version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    user: Mapped[User] = relationship(back_populates="resume_versions")


RANKING_TASK_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED")
ACTIVE_RANKING_TASK_STATUSES = ("PENDING", "PROCESSING")
RANKING_DECISIONS = ("QUALIFIED", "ARCHIVED")


class JobRanking(Base):
    """One versioned, append-only fit analysis of a job, with the exact inputs it was made from.

    Automatic rankings are unique per job, candidate profile, resume content, prompt version, profile facts,
    evidence and posting (uq_job_rankings_inputs), so repeating the same work never creates a second row.
    A ranking the user explicitly asked for (forced) is always a new version.
    """

    __tablename__ = "job_rankings"
    __table_args__ = (
        UniqueConstraint("job_id", "version", name="uq_job_rankings_job_version"),
        Index("uq_job_rankings_inputs", "job_id", "candidate_profile_id", "resume_content_hash", "prompt_version",
              "profile_hash", "evidence_hash", "job_hash", unique=True,
              postgresql_where=text("forced IS FALSE"), sqlite_where=text("forced = 0")),
        CheckConstraint(f"decision IN ({_values(RANKING_DECISIONS)})", name="ck_job_rankings_decision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_rankings")
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE", name="fk_job_rankings_job_id"),
                                        index=True)
    version: Mapped[int] = mapped_column(Integer)
    forced: Mapped[bool] = mapped_column(Boolean, default=False)
    trigger: Mapped[str] = mapped_column(String(40), default="new")
    # Inputs: which profile and which exact resume content, and hashes of everything else the model saw.
    candidate_profile_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE", name="fk_job_rankings_candidate_profile_id"),
        index=True)
    resume_id: Mapped[int] = mapped_column(
        ForeignKey("resume_versions.id", ondelete="CASCADE", name="fk_job_rankings_resume_id"), index=True)
    resume_version: Mapped[int] = mapped_column(Integer)
    resume_content_hash: Mapped[str] = mapped_column(String(64))
    profile_hash: Mapped[str] = mapped_column(String(64))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    job_hash: Mapped[str] = mapped_column(String(64))
    # Structured output, validated before it is stored.
    overall_fit_score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    candidate_fit_score: Mapped[float] = mapped_column(Float)
    resume_fit_score: Mapped[float] = mapped_column(Float)
    seniority_match_score: Mapped[float] = mapped_column(Float)
    skills_match_score: Mapped[float] = mapped_column(Float)
    leadership_match_score: Mapped[float] = mapped_column(Float)
    domain_match_score: Mapped[float] = mapped_column(Float)
    location_match_score: Mapped[float] = mapped_column(Float)
    strong_matches: Mapped[list[str]] = mapped_column(JSON, default=list)
    partial_matches: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_requirements: Mapped[list[str]] = mapped_column(JSON, default=list)
    fatal_gaps: Mapped[list[str]] = mapped_column(JSON, default=list)
    resume_gaps: Mapped[list[str]] = mapped_column(JSON, default=list)
    relevant_evidence_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    # Evidence ids the model cited that were not in the verified evidence it was given; never trusted.
    discarded_evidence_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    recommended_resume_focus: Mapped[list[str]] = mapped_column(JSON, default=list)
    qualification_recommendation: Mapped[str] = mapped_column(String(20))
    reasoning_summary: Mapped[str] = mapped_column(Text, default="")
    # The deterministic decision made from this output and the policy in force when it was stored.
    decision: Mapped[str] = mapped_column(String(20))
    decision_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(300))
    prompt_version: Mapped[str] = mapped_column(String(20))
    config_version: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the AI configuration it ran under
    raw_output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    job: Mapped[Job] = relationship(back_populates="rankings")
    user: Mapped[User] = relationship(back_populates="rankings")


class JobRankingTask(Base):
    """A unit of ranking work, claimed by the ranking worker with FOR UPDATE SKIP LOCKED and a lease.

    A job has at most one PENDING or PROCESSING task at a time (uq_job_ranking_tasks_active_job).
    """

    __tablename__ = "job_ranking_tasks"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(RANKING_TASK_STATUSES)})", name="ck_job_ranking_tasks_status"),
        CheckConstraint("attempt_count >= 0", name="ck_job_ranking_tasks_attempt_count"),
        CheckConstraint(_in_check("error_category", ErrorCategory), name="ck_job_ranking_tasks_error_category"),
        Index("uq_job_ranking_tasks_active_job", "job_id", unique=True,
              postgresql_where=text(f"status IN ({_values(ACTIVE_RANKING_TASK_STATUSES)})"),
              sqlite_where=text(f"status IN ({_values(ACTIVE_RANKING_TASK_STATUSES)})")),
        Index("ix_job_ranking_tasks_claim", "status", "next_attempt_at"),
        Index("ix_job_ranking_tasks_lock_expires_at", "lock_expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_ranking_tasks")
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE", name="fk_job_ranking_tasks_job_id"),
                                        index=True)
    # The profile and resume snapshot the ranking used; filled in when the task runs.
    candidate_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="SET NULL", name="fk_job_ranking_tasks_candidate_profile_id"),
        nullable=True)
    resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resume_versions.id", ondelete="SET NULL", name="fk_job_ranking_tasks_resume_id"), nullable=True)
    ranking_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_rankings.id", ondelete="SET NULL", name="fk_job_ranking_tasks_ranking_id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    # Why the ranking was requested: new, user_request, profile_changed, resume_changed, prompt_changed.
    reason: Mapped[str] = mapped_column(String(40), default="new")
    # A user-requested ranking: runs even with auto-ranking off and always stores a new version.
    force: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_category: Mapped[ErrorCategory | None] = mapped_column(_enum_type(ErrorCategory), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Asked to run now from Running workers (app.services.workers): claimed first, without waiting. Cleared on claim.
    run_now_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    job: Mapped[Job] = relationship(back_populates="ranking_tasks")
    user: Mapped[User] = relationship(back_populates="ranking_tasks")


LLM_STAGES = ("RANKER", "TAILOR", "REVIEWER", "RESUME_PARSER", "JD_PARSER", "JD_RECOVERY", "APPLICATION", "FOLLOW_UP",
              "INBOX", "COMPANY_INSIGHTS", "INTERVIEW_INSIGHTS", "ADVISOR", "BROWSER_NAVIGATION", "DOMAIN_CLASSIFIER", "OTHER")
LLM_ERROR_TYPES = ("TIMEOUT", "RATE_LIMIT", "INVALID_JSON", "SCHEMA_VALIDATION", "PROVIDER_ERROR", "CONTEXT_OVERFLOW",
                   "CONFIGURATION", "OTHER")
OPTIMIZATION_TASK_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED")


class LLMRequestLog(Base):
    """One LLM request made anywhere in the system, recorded by the shared LLM layer (app.services.llm_telemetry).

    Metadata and token counts only: prompt and response text are stored only when LLM_PROMPT_DEBUG is on.
    Metrics a provider does not report stay NULL unless they can be estimated, and `usage_source` says which.
    Not tenancy-filtered automatically (a request may belong to no account); app.services.monitoring always
    filters by user_id.
    """

    __tablename__ = "llm_request_logs"
    __table_args__ = (
        CheckConstraint(f"stage IN ({_values(LLM_STAGES)})", name="ck_llm_request_logs_stage"),
        CheckConstraint("status IN ('SUCCEEDED', 'FAILED')", name="ck_llm_request_logs_status"),
        CheckConstraint(f"error_type IN ({_values(LLM_ERROR_TYPES)})", name="ck_llm_request_logs_error_type"),
        Index("ix_llm_request_logs_user_created", "user_id", "created_at"),
        Index("ix_llm_request_logs_stage_created", "stage", "created_at"),
        Index("ix_llm_request_logs_model_created", "model", "created_at"),
        Index("ix_llm_request_logs_created_at", "created_at"),
        Index("ix_llm_request_logs_worker_created", "worker_type", "created_at"),
        Index("ix_llm_request_logs_config_created", "config_version", "created_at"),
        UniqueConstraint("request_id", name="uq_llm_request_logs_request_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36))
    # The generic external-call row for the same provider request (external_api_request_logs.request_id), so the
    # External APIs tab and this LLM-specific row are two views of one call. Plain id, not a foreign key: the two
    # logs have their own retention and either may be pruned first.
    external_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_llm_request_logs_user_id"), nullable=True)
    workflow_type: Mapped[str | None] = mapped_column(String(30), nullable=True)  # job | application | profile | system | agent
    workflow_entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stage: Mapped[str] = mapped_column(String(30), default="OTHER")
    provider: Mapped[str] = mapped_column(String(30))
    requested_model: Mapped[str | None] = mapped_column(String(300), nullable=True)
    model: Mapped[str | None] = mapped_column(String(300), nullable=True)
    prompt_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_source: Mapped[str] = mapped_column(String(20), default="none")  # provider_reported | estimated | none
    estimated_context_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_window_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_utilization_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    context_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 8), nullable=True)
    cost_source: Mapped[str] = mapped_column(String(20), default="unknown")  # free | pricing_table | provider_reported | unknown
    pricing_effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="SUCCEEDED")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_output_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    structured_output_retry_count: Mapped[int] = mapped_column(Integer, default=0)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The posting side of context selection (app.services.job_context): the raw description against what was sent.
    raw_job_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selected_job_context_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_context_reduction_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Dynamic context selection (app.services.context_builder): what could have been sent versus what was.
    full_available_context_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selected_context_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_reduction_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Evidence retrieval for that context (RAG_RETRIEVAL_MODE=on, app.services.rag_retrieval): how long it took and
    # whether its result was used ("on") or the deterministic builder's ("fallback"). NULL: no retrieval ran.
    retrieval_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retrieval_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    debug_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    debug_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Model routing (app.services.model_resolution): which worker asked, what it was configured to use, and what
    # actually answered. NULL on requests made before AI model configuration existed, and on direct-provider mode
    # for the routing columns that only a gateway reports.
    worker_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)  # FIXED | TIER | AUTO_ROUTER
    configured_model: Mapped[str | None] = mapped_column(String(200), nullable=True)  # the logical assignment
    config_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # What LiteLLM reported: its call id, the model group (alias) that answered, and the auto router's decision.
    litellm_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    model_group: Mapped[str | None] = mapped_column(String(200), nullable=True)
    routing_tier: Mapped[str | None] = mapped_column(String(30), nullable=True)
    routing_cause: Mapped[str | None] = mapped_column(String(60), nullable=True)
    failed_over_from: Mapped[str | None] = mapped_column(String(200), nullable=True)  # FAILOVER: the resting model
    # Infrastructure fallback (LiteLLM answered from another model group) and quality escalation (Job Hunt OS asked
    # a stronger model because the first answer was not good enough) are separate facts on purpose.
    fallback_used: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    escalation_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Structured output (app.prompts.contracts): how the worker's contract was enforced (native_schema, json_mode,
    # prompt_only, none), why an answer failed it (INVALID_JSON, WRONG_OUTPUT_SCHEMA, POSSIBLE_CROSS_WORKER_OUTPUT,
    # SEMANTIC_VALIDATION), and which context builder version prepared the input. NULL on older rows.
    schema_enforcement: Mapped[str | None] = mapped_column(String(20), nullable=True)
    validation_failure_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    context_builder_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Performance facts (app.services.model_performance). Each is NULL unless the provider or gateway reported it or
    # Job Hunt OS measured it: NULL means unknown, never zero.
    #   ttft_ms          time to first token; only a streaming adapter can report it (none streams today)
    #   generation_ms    the model call's own duration as LiteLLM reports it (response duration minus gateway overhead)
    #   queue_wait_ms    time spent waiting for the worker's max-concurrency slot (measured; NULL when no limit is set)
    #   gateway_overhead_ms  LiteLLM's own overhead as reported in x-litellm-overhead-duration-ms
    ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generation_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    queue_wait_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gateway_overhead_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    service_tier: Mapped[str | None] = mapped_column(String(30), nullable=True)  # as the provider reported it
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LLMUsageDaily(Base):
    """Daily totals kept after raw request rows pass LLM_TELEMETRY_RETENTION_DAYS."""

    __tablename__ = "llm_usage_daily"
    __table_args__ = (
        UniqueConstraint("user_id", "day", "stage", "provider", "model", "prompt_version", name="uq_llm_usage_daily_key"),
        Index("ix_llm_usage_daily_user_day", "user_id", "day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_llm_usage_daily_user_id"), nullable=True)
    day: Mapped[date] = mapped_column(Date)
    stage: Mapped[str] = mapped_column(String(30))
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(300), default="")
    prompt_version: Mapped[str] = mapped_column(String(20), default="")
    requests: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    structured_output_failures: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 8), nullable=True)
    unpriced_requests: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms_sum: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms_max: Mapped[int] = mapped_column(Integer, default=0)
    context_tokens_sum: Mapped[int] = mapped_column(Integer, default=0)


EXTERNAL_STATUSES = ("SUCCEEDED", "FAILED")
# Failure categories the instrumentation layer maps every exception and HTTP status onto.
EXTERNAL_ERROR_TYPES = ("TIMEOUT", "CONNECTION", "DNS", "RATE_LIMIT", "CLIENT_ERROR", "SERVER_ERROR",
                        "INVALID_RESPONSE", "RETRY_EXHAUSTED", "CONFIGURATION", "SDK_ERROR", "OTHER")
# Providers seen so far. Deliberately NOT a database constraint: a new integration must not need a migration.
KNOWN_EXTERNAL_PROVIDERS = ("APIFY", "OPENROUTER", "OPENAI", "ANTHROPIC", "GEMINI", "LITELLM", "OLLAMA", "EMAIL",
                            "GMAIL", "GOOGLE_OAUTH", "SEARCH", "JOB_BOARD", "WEB", "OTHER")
SYSTEM_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
SYSTEM_LOG_CATEGORIES = ("EXTERNAL_API", "LLM", "SCHEDULER", "WORKFLOW", "APIFY", "APPLICATION", "EMAIL", "SYSTEM")


class ExternalAPIRequestLog(Base):
    """One call to a third-party service, recorded by the instrumentation layer (app.services.external_api).

    Written automatically for everything that leaves the process through the shared HTTP client or a wrapped SDK
    boundary; business services never write these rows themselves. Metadata only — no headers, no credentials and
    no request or response bodies unless EXTERNAL_API_DEBUG_PAYLOADS is on, and even then redacted and capped.
    Like llm_request_logs this table is not tenancy-filtered (a call may belong to no account); the monitoring
    service always filters by user_id.
    """

    __tablename__ = "external_api_request_logs"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(EXTERNAL_STATUSES)})", name="ck_external_api_request_logs_status"),
        CheckConstraint(f"error_type IS NULL OR error_type IN ({_values(EXTERNAL_ERROR_TYPES)})",
                        name="ck_external_api_request_logs_error_type"),
        UniqueConstraint("request_id", name="uq_external_api_request_logs_request_id"),
        Index("ix_external_api_request_logs_user_created", "user_id", "created_at"),
        Index("ix_external_api_request_logs_provider_created", "provider", "created_at"),
        Index("ix_external_api_request_logs_operation_created", "service", "operation", "created_at"),
        Index("ix_external_api_request_logs_status_created", "status", "created_at"),
        Index("ix_external_api_request_logs_created_at", "created_at"),
        Index("ix_external_api_request_logs_trace_id", "trace_id"),
        Index("ix_external_api_request_logs_workflow", "workflow_type", "workflow_entity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36))
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_external_api_request_logs_user_id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(30), default="OTHER")  # APIFY, OPENROUTER, … open set
    service: Mapped[str] = mapped_column(String(60), default="")  # the API within the provider
    operation: Mapped[str] = mapped_column(String(60), default="")  # actor_run_start, chat_completion, …
    workflow_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    workflow_entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    workflow: Mapped[str | None] = mapped_column(String(60), nullable=True)  # the trace's name: hunter, ranking, api…
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL", name="fk_external_api_request_logs_job_id"), nullable=True, index=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL", name="fk_external_api_request_logs_application_id"),
        nullable=True, index=True)
    http_method: Mapped[str | None] = mapped_column(String(10), nullable=True)
    endpoint_host: Mapped[str | None] = mapped_column(String(200), nullable=True)
    endpoint_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="SUCCEEDED")
    http_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    rate_limit_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate_limit_reset_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 8), nullable=True)
    cost_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    cost_source: Mapped[str] = mapped_column(String(20), default="unknown")  # provider_reported | pricing_table | free | unknown
    # Safe, provider-specific facts: actor and run ids, dataset counts, model names. Redacted before it is stored.
    request_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # The redacted request and response ({"request": {...}, "response": {...}}), only when the account turned on
    # capture or the server set EXTERNAL_API_DEBUG_PAYLOADS; cleared after llm_prompt_debug_retention_days.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ServerSetting(Base):
    """A server-wide setting an admin changed (app.services.server_settings): a worker limit in Running workers →
    Settings, or the agent and application configuration in Settings → AI agents & automation. Stored as text (a
    number, a mode, a switch). Not owned by an account. No row means the environment value.
    """

    __tablename__ = "server_settings"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(500))
    updated_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ProviderCooldown(Base):
    """How long one account must leave a provider alone after it said 429 (app.services.backoff).

    One row per account and provider, written only when a provider rate limits and cleared on the next
    success, so the common path never touches it.
    """

    __tablename__ = "provider_cooldowns"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_provider_cooldowns_user_provider"),
        Index("ix_provider_cooldowns_until", "until"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("provider_cooldowns")
    user: Mapped[User] = relationship(back_populates="provider_cooldowns")
    provider: Mapped[str] = mapped_column(String(230))  # a provider, or LITELLM:<model> (external_api.cooldown_provider)
    until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Rate limits in a row: the exponent behind the delay. Reset to zero by the next successful call.
    consecutive: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Calls not made because the cooldown was still running — what the backoff saved.
    calls_skipped: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_limited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ProviderRateWindow(Base):
    """What a provider last said about one account's per-minute token window for one model (app.services.token_pacing).

    Read from the rate-limit headers of each response (Groq, OpenAI: `x-ratelimit-*-tokens`) and lowered by what each
    request sent since then reserves, so a request that would certainly be refused for the minute is held until the
    window resets instead. The provider's next answer overwrites the figures: they are a cache, never configuration.
    """

    __tablename__ = "provider_rate_windows"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_provider_rate_windows_user_provider"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("provider_rate_windows")
    user: Mapped[User] = relationship(back_populates="provider_rate_windows")
    provider: Mapped[str] = mapped_column(String(230))  # LITELLM:<model> (external_api.cooldown_provider)
    tokens_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_reset_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Tokens reserved by requests sent but not answered yet: kept off the provider's figure, which has not seen them.
    tokens_reserved: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # The request counters, with the window they count: Groq's are per day, OpenAI's per minute. Shown, never paced on.
    requests_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requests_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requests_reset_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    requests_window: Mapped[str | None] = mapped_column(String(10), nullable=True)  # "day", "minute" or unknown
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ExternalAPIUsageDaily(Base):
    """Daily totals kept after raw rows pass EXTERNAL_API_LOG_RETENTION_DAYS."""

    __tablename__ = "external_api_usage_daily"
    __table_args__ = (
        UniqueConstraint("user_id", "day", "provider", "service", "operation", name="uq_external_api_usage_daily_key"),
        Index("ix_external_api_usage_daily_user_day", "user_id", "day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_external_api_usage_daily_user_id"), nullable=True)
    day: Mapped[date] = mapped_column(Date)
    provider: Mapped[str] = mapped_column(String(30))
    service: Mapped[str] = mapped_column(String(60), default="")
    operation: Mapped[str] = mapped_column(String(60), default="")
    requests: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    rate_limited: Mapped[int] = mapped_column(Integer, default=0)
    server_errors: Mapped[int] = mapped_column(Integer, default=0)
    timeouts: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms_sum: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms_max: Mapped[int] = mapped_column(Integer, default=0)
    request_bytes: Mapped[int] = mapped_column(Integer, default=0)
    response_bytes: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 8), nullable=True)
    unpriced_requests: Mapped[int] = mapped_column(Integer, default=0)


class SystemLogEntry(Base):
    """An operational event worth keeping: an external call that failed, a scheduler run, a workflow milestone.

    This is the Monitoring → Logs table. Messages are redacted (app.services.redaction) before they are stored.
    """

    __tablename__ = "system_logs"
    __table_args__ = (
        CheckConstraint(f"level IN ({_values(SYSTEM_LOG_LEVELS)})", name="ck_system_logs_level"),
        CheckConstraint(f"category IN ({_values(SYSTEM_LOG_CATEGORIES)})", name="ck_system_logs_category"),
        Index("ix_system_logs_user_created", "user_id", "created_at"),
        Index("ix_system_logs_category_created", "category", "created_at"),
        Index("ix_system_logs_level_created", "level", "created_at"),
        Index("ix_system_logs_created_at", "created_at"),
        Index("ix_system_logs_trace_id", "trace_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    level: Mapped[str] = mapped_column(String(10), default="INFO")
    category: Mapped[str] = mapped_column(String(20), default="SYSTEM")
    provider: Mapped[str | None] = mapped_column(String(30), nullable=True)
    operation: Mapped[str | None] = mapped_column(String(60), nullable=True)
    workflow: Mapped[str | None] = mapped_column(String(60), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_system_logs_user_id"), nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The generic external-call row this event describes, when it has one.
    external_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class AIOptimizationReport(Base):
    """Recommendations from the AI Optimization Advisor. Advice only: nothing in it is ever applied automatically."""

    __tablename__ = "ai_optimization_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("ai_optimization_reports")
    time_range_start: Mapped[datetime] = mapped_column(DateTime)
    time_range_end: Mapped[datetime] = mapped_column(DateTime)
    summary: Mapped[str] = mapped_column(Text, default="")
    cost_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    latency_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    context_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    model_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    prompt_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    reliability_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    recommendations: Mapped[list[Any]] = mapped_column(JSON, default=list)
    estimated_savings: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # The exact deterministic aggregates the report was built from.
    aggregates: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    generated_by: Mapped[str] = mapped_column(String(10), default="rules")  # llm | rules
    model_provider: Mapped[str | None] = mapped_column(String(30), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    user: Mapped[User] = relationship(back_populates="optimization_reports")


class AIOptimizationTask(Base):
    """A queued analysis of an account's LLM telemetry, processed by the scheduler's ai_optimization job."""

    __tablename__ = "ai_optimization_tasks"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(OPTIMIZATION_TASK_STATUSES)})", name="ck_ai_optimization_tasks_status"),
        Index("uq_ai_optimization_tasks_active_user", "user_id", unique=True,
              postgresql_where=text("status IN ('PENDING', 'PROCESSING')"),
              sqlite_where=text("status IN ('PENDING', 'PROCESSING')")),
        Index("ix_ai_optimization_tasks_claim", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("ai_optimization_tasks")
    time_range_start: Mapped[datetime] = mapped_column(DateTime)
    time_range_end: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    trigger: Mapped[str] = mapped_column(String(20), default="manual")  # manual | scheduled
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    report_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_optimization_reports.id", ondelete="SET NULL", name="fk_ai_optimization_tasks_report_id"),
        nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates="optimization_tasks")


CONVERSATION_ACTION_STATUSES = ("OK", "PARTIAL", "REJECTED", "ERROR")


class ConversationAction(Base):
    """Audit trail of an operation requested from a conversation (Claude Desktop over MCP).

    Stores which tool ran, on what, and how it ended. `arguments_summary` holds IDs, counts, filters and text
    lengths only: never resume text, tokens or environment values. Bulk saves share one `operation_id`.
    """

    __tablename__ = "conversation_actions"
    __table_args__ = (
        CheckConstraint(f"result_status IN ({_values(CONVERSATION_ACTION_STATUSES)})",
                        name="ck_conversation_actions_result_status"),
        Index("ix_conversation_actions_user_created", "user_id", "created_at"),
        Index("ix_conversation_actions_operation_id", "operation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("conversation_actions")
    source: Mapped[str] = mapped_column(String(30), default="CLAUDE_DESKTOP")
    tool_name: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    arguments_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_status: Mapped[str] = mapped_column(String(20), default="OK")
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    user: Mapped[User] = relationship(back_populates="conversation_actions")


class McpToken(Base):
    """A personal access token for the local MCP server. Only the SHA-256 of the token is stored."""

    __tablename__ = "mcp_tokens"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_mcp_tokens_token_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("mcp_tokens")
    name: Mapped[str] = mapped_column(String(100), default="Claude Desktop")
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates="mcp_tokens")


# ───────────── AI model configuration (AI Orchestration → Models) ─────────────
# PostgreSQL is the source of truth for what an account wants its AI workers to use. The editable rows below are
# the *desired* (draft) configuration; an Apply validates them, pushes the parts that live inside LiteLLM, and
# freezes the result as an AIConfigVersion. Workers only ever read the latest version (the *active* one).

AI_MODEL_TYPES = ("FAST", "BALANCED", "REASONING", "DEEP_REASONING", "EMBEDDING", "AUTO_ROUTER", "CUSTOM")
WORKER_MODEL_MODES = ("FIXED", "TIER", "AUTO_ROUTER")
REASONING_EFFORTS = ("low", "medium", "high")
LITELLM_CONNECTION_STATUSES = ("UNKNOWN", "CONNECTED", "DISCONNECTED", "AUTH_FAILED", "UNAVAILABLE")
LITELLM_SYNC_MODES = ("disabled", "admin_api", "config_file")
AI_SYNC_STATUSES = ("SYNCED", "PENDING", "APPLYING", "OUT_OF_SYNC", "FAILED")
AI_CONFIG_SOURCES = ("USER", "SYSTEM", "MIGRATION")
AUTO_ROUTER_CLASSIFIERS = ("DISABLED", "HEURISTIC", "HEURISTIC_V2", "LLM", "CUSTOM")
MODEL_HEALTH_STATUSES = ("UNKNOWN", "HEALTHY", "UNHEALTHY")
AI_AUDIT_RESULTS = ("SUCCEEDED", "FAILED", "REJECTED")


class LiteLLMConnection(Base):
    """How one account reaches its LiteLLM proxy, and the synchronization state of its AI configuration.

    The API key is encrypted with SECRET_KEY (app.security) and never returned by the API. `draft_revision`
    increases on every edit to the desired configuration, so an Apply can tell that what it previewed is still
    what it is about to deploy.
    """

    __tablename__ = "litellm_connections"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(LITELLM_CONNECTION_STATUSES)})", name="ck_litellm_connections_status"),
        CheckConstraint(f"sync_status IN ({_values(AI_SYNC_STATUSES)})", name="ck_litellm_connections_sync_status"),
        CheckConstraint(f"sync_mode IN ({_values(LITELLM_SYNC_MODES)})", name="ck_litellm_connections_sync_mode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("litellm_connections", unique=True)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_mode: Mapped[str] = mapped_column(String(20), default="disabled")
    status: Mapped[str] = mapped_column(String(20), default="UNKNOWN")
    litellm_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_discovery_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sync_status: Mapped[str] = mapped_column(String(20), default="SYNCED")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    applying_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    draft_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    user: Mapped[User] = relationship(back_populates="litellm_connection")


class AIModel(Base):
    """One model an account can assign to workers: a LiteLLM model name (alias) and what is known about it.

    Discovered facts (provider, underlying model, context window, capabilities, health) come from LiteLLM and,
    for Ollama deployments, from Ollama itself. A fact nobody reported stays NULL: unknown, never guessed.
    `overrides` holds what the user asserted instead. A `managed` model is one Job Hunt OS deploys into LiteLLM
    itself from `deployment_params` (for example a local Ollama model); other models are only referenced.
    """

    __tablename__ = "ai_models"
    __table_args__ = (
        UniqueConstraint("user_id", "litellm_model_name", name="uq_ai_models_user_name"),
        CheckConstraint(f"model_type IN ({_values(AI_MODEL_TYPES)})", name="ck_ai_models_model_type"),
        CheckConstraint(f"health_status IN ({_values(MODEL_HEALTH_STATUSES)})", name="ck_ai_models_health_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("ai_models")
    display_name: Mapped[str] = mapped_column(String(200), default="")
    litellm_model_name: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str | None] = mapped_column(String(60), nullable=True)
    underlying_model: Mapped[str | None] = mapped_column(String(300), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    model_type: Mapped[str] = mapped_column(String(20), default="CUSTOM")
    supports_structured_output: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    supports_tools: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    supports_reasoning: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    supports_vision: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # The context the deployment actually runs with (Ollama num_ctx when set) and the model's own maximum.
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # EMBEDDING models only: the length of the vectors it returns (LiteLLM model_info.output_vector_size, or measured
    # by an explicit model test). NULL = unknown, and an embedding model of unknown size cannot be applied.
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Auto routers only: the models LiteLLM says it sends requests to, its LLM classifier included. Discovered, not
    # configuration. NULL = unknown.
    router_models: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    overrides: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Failover for every worker that uses this model (app.services.failover): the models tried, in order, when it is
    # rate limited or failing, and how long it rests before it is tried again (NULL = the account's Reliability
    # setting). Configuration: versioned with the rest.
    fallback_models: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    rest_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Removed from Job Hunt OS by a person (ai_model_config.remove_model): out of the configuration and every picker,
    # and a sync leaves it removed. NULL = in use. A discovered model stays in LiteLLM.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    managed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Managed models only: the LiteLLM deployment (litellm_params) Job Hunt OS deploys. Never a literal secret —
    # credentials must be LiteLLM `os.environ/NAME` references, which validation enforces.
    deployment_params: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    present_in_litellm: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(20), default="DISCOVERED")  # DISCOVERED | USER | SEED
    health_status: Mapped[str] = mapped_column(String(20), default="UNKNOWN")
    health_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    health_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    health_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates="ai_models")


class ModelTier(Base):
    """A logical tier (FAST, PRIMARY_REASONING, …) that workers can point at instead of naming a model."""

    __tablename__ = "model_tiers"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_model_tiers_user_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("model_tiers")
    name: Mapped[str] = mapped_column(String(60))
    description: Mapped[str] = mapped_column(String(300), default="")
    litellm_model_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    user: Mapped[User] = relationship(back_populates="model_tiers")


class WorkerModelConfig(Base):
    """Which model one AI worker uses and how it calls it. Blank generation settings mean "the model's default".

    `escalation` is the worker's quality-escalation policy (app.services.model_resolution), deliberately separate
    from `fallback_model`, which is LiteLLM's infrastructure fallback for timeouts, rate limits and outages.
    """

    __tablename__ = "worker_model_configs"
    __table_args__ = (
        UniqueConstraint("user_id", "worker_type", name="uq_worker_model_configs_user_worker"),
        CheckConstraint(f"model_mode IN ({_values(WORKER_MODEL_MODES)})", name="ck_worker_model_configs_model_mode"),
        CheckConstraint(f"reasoning_effort IN ({_values(REASONING_EFFORTS)})",
                        name="ck_worker_model_configs_reasoning_effort"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("worker_model_configs")
    worker_type: Mapped[str] = mapped_column(String(40))
    model_mode: Mapped[str] = mapped_column(String(20), default="TIER")
    primary_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tier_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    fallback_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Further models to try after `fallback_model`, in order (app.services.failover). Part of the versioned config.
    secondary_models: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Tokens the built prompt may use (app.services.prompt_budget): over it, the context builders drop their least
    # relevant items before the request is sent. Blank = no trimming. Part of the versioned config.
    prompt_token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(10), nullable=True)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_concurrency: Mapped[int | None] = mapped_column(Integer, nullable=True)
    use_structured_output: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    escalation: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    user: Mapped[User] = relationship(back_populates="worker_model_configs")


class AIRoutingConfig(Base):
    """The account's LiteLLM auto router: the classifier LiteLLM runs and the model behind each complexity tier."""

    __tablename__ = "ai_routing_configs"
    __table_args__ = (
        CheckConstraint(f"classifier IN ({_values(AUTO_ROUTER_CLASSIFIERS)})", name="ck_ai_routing_configs_classifier"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("ai_routing_configs", unique=True)
    auto_router_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_router_model_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # True: Job Hunt OS deploys the router into LiteLLM from the settings below. False: it uses a router someone
    # already defined in LiteLLM under that name, and only routes workers to it.
    manage_auto_router: Mapped[bool] = mapped_column(Boolean, default=True)
    classifier: Mapped[str] = mapped_column(String(20), default="HEURISTIC")
    classifier_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    classifier_plugin: Mapped[str | None] = mapped_column(String(300), nullable=True)
    tier_models: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # SIMPLE/MEDIUM/COMPLEX/REASONING
    default_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Send Job Hunt OS request metadata (worker, workflow, retry number, …) with every LiteLLM request.
    send_metadata: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    user: Mapped[User] = relationship(back_populates="ai_routing_config")


class AIConfigVersion(Base):
    """An applied AI configuration, frozen. Never updated: a rollback creates a new version from an old snapshot.

    The snapshot holds no secrets (no API keys, no credential values); app.services.ai_model_config redacts it.
    """

    __tablename__ = "ai_config_versions"
    __table_args__ = (
        UniqueConstraint("user_id", "version_number", name="uq_ai_config_versions_user_version"),
        CheckConstraint(f"source IN ({_values(AI_CONFIG_SOURCES)})", name="ck_ai_config_versions_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("ai_config_versions")
    version_number: Mapped[int] = mapped_column(Integer)
    configuration_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    changed_by: Mapped[str] = mapped_column(String(320), default="")
    source: Mapped[str] = mapped_column(String(20), default="USER")
    change_summary: Mapped[str] = mapped_column(Text, default="")
    changes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    sync_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    restored_from_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    user: Mapped[User] = relationship(back_populates="ai_config_versions")


class AIConfigAuditEntry(Base):
    """Every attempt to change the AI configuration or its LiteLLM state, including the ones that failed."""

    __tablename__ = "ai_config_audit"
    __table_args__ = (
        CheckConstraint(f"source IN ({_values(AI_CONFIG_SOURCES)})", name="ck_ai_config_audit_source"),
        CheckConstraint(f"result IN ({_values(AI_AUDIT_RESULTS)})", name="ck_ai_config_audit_result"),
        Index("ix_ai_config_audit_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("ai_config_audit")
    action: Mapped[str] = mapped_column(String(30))  # SEED | APPLY | ROLLBACK | VERIFY | DISCOVER | CONNECTION
    source: Mapped[str] = mapped_column(String(20), default="USER")
    actor: Mapped[str] = mapped_column(String(320), default="")
    result: Mapped[str] = mapped_column(String(20), default="SUCCEEDED")
    version_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    changes: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    sync_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    user: Mapped[User] = relationship(back_populates="ai_config_audit")


ROUTE_STATES = ("HEALTHY", "DEGRADED", "COOLING")
FAILOVER_CAUSES = ("RATE_LIMIT", "ERROR_RATE", "LATENCY", "SCHEMA", "MANUAL", "RECOVERED")


class ModelRouteState(Base):
    """The runtime health of one model, overall (worker_type '') or for one worker (app.services.model_health).

    An overlay on the active AI configuration: failover reads it to pick a model and never edits the configuration.
    """

    __tablename__ = "model_route_states"
    __table_args__ = (
        UniqueConstraint("user_id", "worker_type", "model", name="uq_model_route_states_route"),
        CheckConstraint(f"state IN ({_values(ROUTE_STATES)})", name="ck_model_route_states_state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("model_route_states")
    user: Mapped[User] = relationship(back_populates="model_route_states")
    worker_type: Mapped[str] = mapped_column(String(40), default="", server_default="")
    model: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(12), default="HEALTHY", server_default="HEALTHY")
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    cooling_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    probe_successes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class FailoverEvent(Base):
    """One automatic (or manual) switch away from, or back to, a model. The browser-notification feed."""

    __tablename__ = "failover_events"
    __table_args__ = (
        Index("ix_failover_events_user_created", "user_id", "created_at"),
        CheckConstraint(f"cause IN ({_values(FAILOVER_CAUSES)})", name="ck_failover_events_cause"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("failover_events")
    user: Mapped[User] = relationship(back_populates="failover_events")
    worker_type: Mapped[str] = mapped_column(String(40), default="", server_default="")
    from_model: Mapped[str] = mapped_column(String(200))
    to_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cause: Mapped[str] = mapped_column(String(16))
    algorithm: Mapped[str | None] = mapped_column(String(20), nullable=True)
    message: Mapped[str] = mapped_column(String(400))
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


AGENT_THREAD_STATUSES = ("QUEUED", "RUNNING", "INTERRUPTED", "COMPLETED", "FAILED", "STOPPED")
ACTIVE_AGENT_THREAD_STATUSES = ("QUEUED", "RUNNING")


class AgentThread(Base):
    """One LangGraph thread (app.agents): who owns it, where it stands, and who holds it.

    The graph's resumable state lives in the LangGraph checkpoint tables under `thread_id`; this row is the owner and
    the lease. The thread id is built on the server (app.agents.threads.thread_id_for) and never taken from a caller,
    and every checkpointer access first loads this row through the account's own session. A checkpoint is
    computation state only: it never decides a job's state, an application or an approval.
    """

    __tablename__ = "agent_threads"
    __table_args__ = (
        UniqueConstraint("thread_id", name="uq_agent_threads_thread_id"),
        UniqueConstraint("user_id", "graph", "subject", name="uq_agent_threads_subject"),
        CheckConstraint(f"status IN ({_values(AGENT_THREAD_STATUSES)})", name="ck_agent_threads_status"),
        CheckConstraint("attempt_count >= 0", name="ck_agent_threads_attempt_count"),
        Index("ix_agent_threads_claim", "status", "lock_expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("agent_threads")
    user: Mapped[User] = relationship(back_populates="agent_threads")
    thread_id: Mapped[str] = mapped_column(String(200))
    graph: Mapped[str] = mapped_column(String(60))
    graph_version: Mapped[str] = mapped_column(String(20))
    subject: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", server_default="QUEUED")
    # The graph's input, kept until the first checkpoint exists (ids, enums and counts only; checked like a checkpoint).
    input: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # What a person is asked while the thread waits (metadata only), cleared on resume.
    interrupt: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checkpoint_pruned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentEffect(Base):
    """A side effect a graph has made, recorded once (app.agents.effects.once).

    LangGraph may run a node again after a crash; the unique key makes the second run find the first one's record
    instead of repeating the effect.
    """

    __tablename__ = "agent_effects"
    __table_args__ = (
        UniqueConstraint("agent_thread_id", "effect_key", name="uq_agent_effects_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("agent_effects")
    user: Mapped[User] = relationship(back_populates="agent_effects")
    agent_thread_id: Mapped[int] = mapped_column(
        ForeignKey("agent_threads.id", ondelete="CASCADE", name="fk_agent_effects_agent_thread_id"), index=True)
    effect_key: Mapped[str] = mapped_column(String(120))
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


QUALIFICATION_TASK_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED")
ACTIVE_QUALIFICATION_TASK_STATUSES = ("PENDING", "PROCESSING")
QUALIFICATION_MODES = ("ON", "SHADOW")


class JobQualificationTask(Base):
    """One run of the qualification graph (app.agents.qualification) for one job, claimed like every queued worker.

    The graph conducts the existing workers (JD recovery, parsing, ranking) through their own task rows; this row is
    its place in Running workers: its lease, attempts, the step it waits at and how it ended. ON runs drive the job;
    SHADOW runs only predict what the graph would decide and compare it with what the current path did (`parity`,
    ids and enums only). A job has at most one active task per mode (uq_job_qualification_tasks_active_job).
    """

    __tablename__ = "job_qualification_tasks"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(QUALIFICATION_TASK_STATUSES)})",
                        name="ck_job_qualification_tasks_status"),
        CheckConstraint(f"mode IN ({_values(QUALIFICATION_MODES)})", name="ck_job_qualification_tasks_mode"),
        CheckConstraint("attempt_count >= 0", name="ck_job_qualification_tasks_attempt_count"),
        Index("uq_job_qualification_tasks_active_job", "job_id", "mode", unique=True,
              postgresql_where=text(f"status IN ({_values(ACTIVE_QUALIFICATION_TASK_STATUSES)})"),
              sqlite_where=text(f"status IN ({_values(ACTIVE_QUALIFICATION_TASK_STATUSES)})")),
        Index("ix_job_qualification_tasks_claim", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_qualification_tasks")
    user: Mapped[User] = relationship(back_populates="qualification_tasks")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE", name="fk_job_qualification_tasks_job_id"), index=True)
    job: Mapped[Job] = relationship(back_populates="qualification_tasks")
    agent_thread_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_threads.id", ondelete="SET NULL", name="fk_job_qualification_tasks_agent_thread_id"),
        nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String(10), default="ON")
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    reason: Mapped[str] = mapped_column(String(40), default="new")
    # The graph node the run is at (or ended in), and what it waits for while PENDING between steps.
    step: Mapped[str | None] = mapped_column(String(60), nullable=True)
    waiting_for: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # QUALIFIED, ARCHIVED, HIDDEN, SKIPPED, BLOCKED, REMOVED, TRIAGED, STOPPED, FAILED, ALREADY_DECIDED
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    result_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    parity: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    paid_calls: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(30), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    run_now_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


PREPARATION_TASK_STATUSES = QUALIFICATION_TASK_STATUSES
ACTIVE_PREPARATION_TASK_STATUSES = ACTIVE_QUALIFICATION_TASK_STATUSES
PREPARATION_MODES = QUALIFICATION_MODES


class JobPreparationTask(Base):
    """One run of the resume preparation graph (app.agents.preparation) for one job, claimed like every queued worker.

    The graph conducts the existing tailor worker (tailor_tasks), the deterministic evidence checks and the review
    decision (review_results), then waits for a person's approval. `revision` counts tailoring passes after the
    first (bounded by the account's `max_revisions`); `decision` is the last review decision; `person_decision` is
    what a person chose while the run waited (approved, rejected, revise). ON runs drive the job; SHADOW runs only
    compare. A job has at most one active task per mode (uq_job_preparation_tasks_active_job).
    """

    __tablename__ = "job_preparation_tasks"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(PREPARATION_TASK_STATUSES)})", name="ck_job_preparation_tasks_status"),
        CheckConstraint(f"mode IN ({_values(PREPARATION_MODES)})", name="ck_job_preparation_tasks_mode"),
        CheckConstraint("attempt_count >= 0", name="ck_job_preparation_tasks_attempt_count"),
        CheckConstraint("revision >= 0", name="ck_job_preparation_tasks_revision"),
        Index("uq_job_preparation_tasks_active_job", "job_id", "mode", unique=True,
              postgresql_where=text(f"status IN ({_values(ACTIVE_PREPARATION_TASK_STATUSES)})"),
              sqlite_where=text(f"status IN ({_values(ACTIVE_PREPARATION_TASK_STATUSES)})")),
        Index("ix_job_preparation_tasks_claim", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_preparation_tasks")
    user: Mapped[User] = relationship(back_populates="preparation_tasks")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE", name="fk_job_preparation_tasks_job_id"), index=True)
    job: Mapped[Job] = relationship(back_populates="preparation_tasks")
    agent_thread_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_threads.id", ondelete="SET NULL", name="fk_job_preparation_tasks_agent_thread_id"),
        nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String(10), default="ON")
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    reason: Mapped[str] = mapped_column(String(40), default="new")
    step: Mapped[str | None] = mapped_column(String(60), nullable=True)
    waiting_for: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # APPROVED, REJECTED, AWAITING, NEEDS_HUMAN, REMOVED, TRIAGED, STOPPED, FAILED, SKIPPED, BLOCKED
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    result_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    person_decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    variant_id: Mapped[int | None] = mapped_column(
        ForeignKey("resume_variants.id", ondelete="SET NULL", name="fk_job_preparation_tasks_variant_id"),
        nullable=True)
    parity: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    run_now_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ApprovedPacket(Base):
    """The exact packet a person approved (app.services.approval): an immutable snapshot and its digest.

    Written once inside workflow.approve_job and never updated, except to record that the approval was revoked
    (`revoked_at`, `revoked_reason`) because the packet was edited afterwards or approved again. The live approval of
    an application is its newest row with `revoked_at` NULL. Export, packet issuance and application compare the
    packet they are about to use against `digest`; a mismatch needs a new approval, never a new digest.
    """

    __tablename__ = "approved_packets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("approved_packets")
    user: Mapped[User] = relationship(back_populates="approved_packets")
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE", name="fk_approved_packets_application_id"), index=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE", name="fk_approved_packets_job_id"), index=True)
    job: Mapped[Job] = relationship()
    variant_id: Mapped[int | None] = mapped_column(
        ForeignKey("resume_variants.id", ondelete="SET NULL", name="fk_approved_packets_variant_id"), nullable=True)
    variant_version: Mapped[int] = mapped_column(Integer)
    serializer_version: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64))
    # {resume_markdown, cover_letter, attachments}: what the recruiter receives, as approved.
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    reviewer_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    override: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)


# ───────────── Application execution (Phase 5, app.services.apply_queue / application_execution) ─────────────
# A claim reserves one posting for one submission before anything external starts (apply_queue.begin_submission).
# Its alias keys (`application_claim_keys`) are unique per account while the claim is held, so a duplicate job row or
# another URL of the same posting cannot be claimed a second time. Executions fill forms; they never submit (Phase 5).

APPLICATION_CLAIM_STATUSES = ("CLAIMED", "UNCERTAIN", "RECORDED", "RELEASED")
HELD_APPLICATION_CLAIM_STATUSES = ("CLAIMED", "UNCERTAIN", "RECORDED")
APPLICATION_CLAIM_CLIENTS = ("mcp", "apply_kit", "worker")
APPLICATION_EXECUTION_TASK_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED")
ACTIVE_APPLICATION_EXECUTION_TASK_STATUSES = ("PENDING", "PROCESSING")
APPLICATION_EXECUTION_MODES = ("OBSERVE", "ASSIST", "GUARDED_AUTO")
APPLICATION_BACKENDS = ("ats_api", "playwright", "browser_use_webui")
# The roadmap's execution states (docs/architecture/agentic-roadmap.md §5). SUBMITTING, CONFIRMING and CONFIRMED are
# reached only by a GUARDED_AUTO run holding a SubmissionIntent (application_execution.set_stage refuses them
# otherwise, and so does the column).
APPLICATION_EXECUTION_STAGES = ("QUEUED", "DISCOVERING", "ROUTED", "FORM_ANALYZING", "ANSWERING", "FILLING",
                                "PRE_SUBMIT_VALIDATION", "READY_TO_SUBMIT", "SUBMITTING", "CONFIRMING", "CONFIRMED",
                                "NEEDS_INPUT", "NEEDS_HUMAN", "RETRYING", "BLOCKED", "SUBMISSION_UNCERTAIN", "FAILED",
                                "CANCELLED")


class ApplicationClaim(Base):
    """One reservation of a posting for one submission (apply_queue.begin_submission), bound to the job, its approval
    snapshot and digest. CLAIMED until the client reports; past its lease it becomes UNCERTAIN (something may have
    been submitted) and only a person resolves it. RECORDED and RELEASED are final; a RECORDED claim keeps its keys."""

    __tablename__ = "application_claims"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(APPLICATION_CLAIM_STATUSES)})", name="ck_application_claims_status"),
        CheckConstraint(f"client IN ({_values(APPLICATION_CLAIM_CLIENTS)})", name="ck_application_claims_client"),
        UniqueConstraint("token", name="uq_application_claims_token"),
        Index("ix_application_claims_lease", "status", "lease_expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("application_claims")
    user: Mapped[User] = relationship(back_populates="application_claims")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE", name="fk_application_claims_job_id"), index=True)
    job: Mapped[Job] = relationship()
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL", name="fk_application_claims_application_id"),
        nullable=True)
    approved_packet_id: Mapped[int | None] = mapped_column(
        ForeignKey("approved_packets.id", ondelete="SET NULL", name="fk_application_claims_approved_packet_id"),
        nullable=True)
    packet_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)  # None: a pre-Phase-4 (LEGACY) approval
    identity: Mapped[str] = mapped_column(String(800))
    token: Mapped[str] = mapped_column(String(64))
    client: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="CLAIMED")
    claimed_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(200), nullable=True)
    execution_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    keys: Mapped[list[ApplicationClaimKey]] = relationship(back_populates="claim", cascade="all, delete-orphan",
                                                           passive_deletes=True)


class ApplicationClaimKey(Base):
    """One alias of a claimed posting (`apply:`, `url:`, `board:`, `dedupe:` …). `held` mirrors the claim's status;
    while it is true the key is unique within the account (uq_application_claim_keys_held)."""

    __tablename__ = "application_claim_keys"
    __table_args__ = (
        Index("uq_application_claim_keys_held", "user_id", "key", unique=True,
              postgresql_where=text("held"), sqlite_where=text("held")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("application_claim_keys")
    user: Mapped[User] = relationship(back_populates="application_claim_keys")
    claim_id: Mapped[int] = mapped_column(
        ForeignKey("application_claims.id", ondelete="CASCADE", name="fk_application_claim_keys_claim_id"), index=True)
    claim: Mapped[ApplicationClaim] = relationship(back_populates="keys")
    key: Mapped[str] = mapped_column(String(800))
    held: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class ApplicationExecutionTask(Base):
    """One run of the application execution worker (app.agents.application) for one approved job: inspect the form
    (OBSERVE) or fill it in an ephemeral browser (ASSIST), up to READY_TO_SUBMIT. Never submits.

    `stage` is the execution state (APPLICATION_EXECUTION_STAGES); `status` is the queue status every worker shares.
    `form` is the analysed form's structure and `plan` each field's answer source and status — never a value.
    A job has at most one active task (uq_application_execution_tasks_active_job).
    """

    __tablename__ = "application_execution_tasks"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(APPLICATION_EXECUTION_TASK_STATUSES)})",
                        name="ck_application_execution_tasks_status"),
        CheckConstraint(f"mode IN ({_values(APPLICATION_EXECUTION_MODES)})",
                        name="ck_application_execution_tasks_mode"),
        CheckConstraint(f"stage IN ({_values(APPLICATION_EXECUTION_STAGES)})",
                        name="ck_application_execution_tasks_stage"),
        CheckConstraint("attempt_count >= 0", name="ck_application_execution_tasks_attempt_count"),
        Index("uq_application_execution_tasks_active_job", "job_id", unique=True,
              postgresql_where=text(f"status IN ({_values(ACTIVE_APPLICATION_EXECUTION_TASK_STATUSES)})"),
              sqlite_where=text(f"status IN ({_values(ACTIVE_APPLICATION_EXECUTION_TASK_STATUSES)})")),
        Index("ix_application_execution_tasks_claim", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("application_execution_tasks")
    user: Mapped[User] = relationship(back_populates="application_execution_tasks")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE", name="fk_application_execution_tasks_job_id"), index=True)
    job: Mapped[Job] = relationship()
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL", name="fk_application_execution_tasks_application_id"),
        nullable=True)
    approved_packet_id: Mapped[int | None] = mapped_column(
        ForeignKey("approved_packets.id", ondelete="SET NULL",
                   name="fk_application_execution_tasks_approved_packet_id"), nullable=True)
    packet_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_thread_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_threads.id", ondelete="SET NULL", name="fk_application_execution_tasks_agent_thread_id"),
        nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String(20), default="OBSERVE")
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    stage: Mapped[str] = mapped_column(String(30), default="QUEUED")
    reason: Mapped[str] = mapped_column(String(40), default="user_request")
    route: Mapped[str | None] = mapped_column(String(40), nullable=True)
    backend: Mapped[str | None] = mapped_column(String(30), nullable=True)  # APPLICATION_BACKENDS, chosen at ROUTE
    form_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    form: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # {kind: NEEDS_INPUT|NEEDS_HUMAN, reason, question_ids, field_ids}
    interrupt: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    confirmed_question_ids: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    result_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    run_now_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ApplicationExecutionAttempt(Base):
    """One attempt of an execution task: how far it got and how it ended. Metadata only."""

    __tablename__ = "application_execution_attempts"
    __table_args__ = (UniqueConstraint("task_id", "attempt", name="uq_application_execution_attempts_task_attempt"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("application_execution_attempts")
    user: Mapped[User] = relationship(back_populates="application_execution_attempts")
    task_id: Mapped[int] = mapped_column(
        ForeignKey("application_execution_tasks.id", ondelete="CASCADE",
                   name="fk_application_execution_attempts_task_id"), index=True)
    task: Mapped[ApplicationExecutionTask] = relationship()
    attempt: Mapped[int] = mapped_column(Integer)
    stage_reached: Mapped[str | None] = mapped_column(String(30), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    form_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # {format: sha256} of the packet files uploaded in this attempt (ASSIST)
    file_digests: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ───────────── Guarded submission and ATS logins (Phase 6, app.services.submission / ats_map / ats_auth) ─────────────
# A SubmissionIntent is written, bound to the posting's claim, before the one click that may submit an application; it
# is CLICKING from the commit just before that click, so a crash afterwards is never mistaken for "not submitted".

SUBMISSION_INTENT_STATUSES = ("ARMED", "CLICKING", "CONFIRMED", "UNCERTAIN", "ABORTED", "RESOLVED_SUBMITTED",
                              "RESOLVED_NOT_SUBMITTED")
# Statuses after which the click may have happened: never clicked again, only reconciled.
CLICKED_INTENT_STATUSES = ("CLICKING", "CONFIRMED", "UNCERTAIN", "RESOLVED_SUBMITTED", "RESOLVED_NOT_SUBMITTED")
ATS_CREDENTIAL_STATUSES = ("PENDING", "ACTIVE")


class SubmissionIntent(Base):
    """One authorized final click of one execution, bound to the posting's pre-submit claim (one intent per claim).

    ARMED: the claim is held and the intent recorded; nothing has been clicked. CLICKING: committed immediately before
    the click; from here on the outcome is a fact to establish, never a retry. CONFIRMED: the confirmation verifier
    found the site's evidence and the submission was recorded. UNCERTAIN: anything less (or a crash after CLICKING);
    a person resolves it (RESOLVED_SUBMITTED / RESOLVED_NOT_SUBMITTED). ABORTED: given up before the click.
    `evidence` is metadata only: response statuses, hosts, path patterns, a confirmation id; never page text."""

    __tablename__ = "submission_intents"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(SUBMISSION_INTENT_STATUSES)})", name="ck_submission_intents_status"),
        UniqueConstraint("claim_id", name="uq_submission_intents_claim"),
        Index("ix_submission_intents_task", "execution_task_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("submission_intents")
    user: Mapped[User] = relationship(back_populates="submission_intents")
    claim_id: Mapped[int] = mapped_column(
        ForeignKey("application_claims.id", ondelete="CASCADE", name="fk_submission_intents_claim_id"))
    claim: Mapped[ApplicationClaim] = relationship()
    execution_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("application_execution_tasks.id", ondelete="SET NULL",
                   name="fk_submission_intents_execution_task_id"), nullable=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE", name="fk_submission_intents_job_id"), index=True)
    job: Mapped[Job] = relationship()
    status: Mapped[str] = mapped_column(String(30), default="ARMED")
    packet_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    form_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backend: Mapped[str] = mapped_column(String(30))
    route: Mapped[str] = mapped_column(String(240))  # ats_map route key ("greenhouse:acme")
    domain: Mapped[str] = mapped_column(String(255))  # the only host a non-GET request may reach after the click
    file_digests: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    clicking_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AtsRoute(Base):
    """Where an account's applications go: one ATS tenant (ats_map), its apply and login domains and what runs there
    taught Job Hunt OS (the backend, a login wall, the last outcome). Facts from postings and runs; never a model's."""

    __tablename__ = "ats_routes"
    __table_args__ = (UniqueConstraint("user_id", "ats", "tenant", name="uq_ats_routes_user_ats_tenant"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("ats_routes")
    user: Mapped[User] = relationship(back_populates="ats_routes")
    ats: Mapped[str] = mapped_column(String(40))
    tenant: Mapped[str] = mapped_column(String(200), default="")
    apply_domain: Mapped[str] = mapped_column(String(255))
    login_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auth_required: Mapped[str] = mapped_column(String(20), default="none")  # none | optional | account | unsupported
    backend: Mapped[str | None] = mapped_column(String(30), nullable=True)
    source_board: Mapped[str | None] = mapped_column(String(50), nullable=True)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    form_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_outcome: Mapped[str | None] = mapped_column(String(60), nullable=True)
    needs_login: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class AtsCredential(Base):
    """A login a person saved for one ATS domain: the username and the password, encrypted (app.security). Write-only:
    no API returns the password; it is decrypted only to fill that domain's own login form (ats_auth.login_secret).
    PENDING: generated for a signup the person has not finished; ACTIVE once they saved the login."""

    __tablename__ = "ats_credentials"
    __table_args__ = (
        UniqueConstraint("user_id", "domain", name="uq_ats_credentials_user_domain"),
        CheckConstraint(f"status IN ({_values(ATS_CREDENTIAL_STATUSES)})", name="ck_ats_credentials_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("ats_credentials")
    user: Mapped[User] = relationship(back_populates="ats_credentials")
    domain: Mapped[str] = mapped_column(String(255))
    username: Mapped[str] = mapped_column(String(320))
    password_encrypted: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AtsSession(Base):
    """A browser session a person logged in to on one ATS domain, kept so the next application there needs no login:
    the site's storage state (cookies, local storage), encrypted. Captured only when the person clicks "Save this
    login"; used only by that account's application browser for that domain, never by JD recovery."""

    __tablename__ = "ats_sessions"
    __table_args__ = (UniqueConstraint("user_id", "domain", name="uq_ats_sessions_user_domain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("ats_sessions")
    user: Mapped[User] = relationship(back_populates="ats_sessions")
    domain: Mapped[str] = mapped_column(String(255))
    ats: Mapped[str | None] = mapped_column(String(40), nullable=True)
    storage_state_encrypted: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ───────────── Candidate knowledge retrieval (RAG, app.services.rag_*) ─────────────
# Evidence ↔ resume tags are a person's (and the verifier's) statement of which resume a fact belongs to; retrieval
# for a job only ever considers the evidence of the job's own resume. The index tables are *derived*: they never
# supersede the Evidence / KnowledgeAnswer rows they describe and can be dropped and rebuilt at any time. The vectors
# themselves live in `rag_vectors`, created by app.services.vector_store.setup() (pgvector), not by Alembic.

EVIDENCE_LINK_ORIGINS = ("AUTO", "USER")
RAG_COLLECTIONS = ("CANDIDATE_EVIDENCE", "ATS_ANSWER")
RAG_SOURCE_TYPES = ("evidence", "knowledge_answer")
RAG_INDEX_STATUSES = ("BUILDING", "SERVING", "RETIRED")
RAG_ENTRY_STATUSES = ("PENDING", "PROCESSING", "ACTIVE", "FAILED", "DEACTIVATED")


class EvidenceResumeLink(Base):
    """Which resume a piece of evidence belongs to. AUTO links come from verifying a resume's facts
    (resume_verification.sync_evidence) or from extracting them from one; USER links are a person's own. A person's
    removal of an AUTO link is kept (`excluded`) so automation never re-adds it. Evidence without any link that is not
    excluded applies to every resume."""

    __tablename__ = "evidence_resume_links"
    __table_args__ = (
        UniqueConstraint("evidence_id", "resume_id", name="uq_evidence_resume_links_pair"),
        CheckConstraint(f"origin IN ({_values(EVIDENCE_LINK_ORIGINS)})", name="ck_evidence_resume_links_origin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("evidence_resume_links")
    user: Mapped[User] = relationship(back_populates="evidence_resume_links")
    evidence_id: Mapped[int] = mapped_column(
        ForeignKey("evidence.id", ondelete="CASCADE", name="fk_evidence_resume_links_evidence_id"), index=True)
    resume_id: Mapped[int] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE", name="fk_evidence_resume_links_resume_id"), index=True)
    origin: Mapped[str] = mapped_column(String(10), default="AUTO", server_default="AUTO")
    excluded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class RagIndexVersion(Base):
    """One embedding index of an account: one embedding model at one vector size and one document format.

    Changing any of them makes a new version that is built (BUILDING) beside the one in use, and replaces it
    (SERVING) only once it covers every eligible source; vectors of different versions are never compared."""

    __tablename__ = "rag_index_versions"
    __table_args__ = (
        UniqueConstraint("user_id", "identity_hash", name="uq_rag_index_versions_identity"),
        CheckConstraint(f"status IN ({_values(RAG_INDEX_STATUSES)})", name="ck_rag_index_versions_status"),
        CheckConstraint("dimensions > 0", name="ck_rag_index_versions_dimensions"),
        Index("uq_rag_index_versions_serving", "user_id", unique=True,
              postgresql_where=text("status = 'SERVING'"), sqlite_where=text("status = 'SERVING'")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("rag_index_versions")
    user: Mapped[User] = relationship(back_populates="rag_index_versions")
    identity_hash: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(200))
    underlying_model: Mapped[str | None] = mapped_column(String(300), nullable=True)
    dimensions: Mapped[int] = mapped_column(Integer)
    doc_format_version: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="BUILDING", server_default="BUILDING")
    eligible_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    embedded_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    serving_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RagIndexEntry(Base):
    """One source row (an Evidence or KnowledgeAnswer row) in one index version, and its embedding work.

    `content_hash` is the hash of the text the stored vector was made from; retrieval uses a vector only while it still
    equals the live row's hash, so an edit is never answered with the old text's vector. No text is stored here."""

    __tablename__ = "rag_index_entries"
    __table_args__ = (
        UniqueConstraint("index_version_id", "source_type", "source_id", name="uq_rag_index_entries_source"),
        CheckConstraint(f"status IN ({_values(RAG_ENTRY_STATUSES)})", name="ck_rag_index_entries_status"),
        CheckConstraint(f"collection IN ({_values(RAG_COLLECTIONS)})", name="ck_rag_index_entries_collection"),
        CheckConstraint(f"source_type IN ({_values(RAG_SOURCE_TYPES)})", name="ck_rag_index_entries_source_type"),
        CheckConstraint("attempt_count >= 0", name="ck_rag_index_entries_attempt_count"),
        Index("ix_rag_index_entries_claim", "status", "next_attempt_at"),
        Index("ix_rag_index_entries_lookup", "user_id", "source_type", "source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("rag_index_entries")
    user: Mapped[User] = relationship(back_populates="rag_index_entries")
    index_version_id: Mapped[int] = mapped_column(
        ForeignKey("rag_index_versions.id", ondelete="CASCADE", name="fk_rag_index_entries_index_version_id"),
        index=True)
    collection: Mapped[str] = mapped_column(String(30))
    source_type: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[int] = mapped_column(Integer)
    verification_status: Mapped[str] = mapped_column(String(20), default="VERIFIED")
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", server_default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Running workers (app.services.workers, kind "indexing"): when the latest attempt started and the entry last
    # finished (ACTIVE, DEACTIVATED or FAILED), and a person's Run now (claimed first, past cooldowns, then cleared).
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    run_now_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


# Registers the per-user isolation hooks on every session, and keeps the derived retrieval index in step with evidence
# and answers on every flush (app.services.rag_index); both need the models above.
from app import tenancy  # noqa: E402,F401
from app.services import rag_index  # noqa: F401
