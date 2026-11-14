"""Per-user data isolation, enforced for every database session that belongs to a user.

A session belongs to a user when `session.info["user_id"]` is set: app.auth.get_user_db does this for
every signed-in request, and the scheduler does it for the work it runs on a user's behalf. For such a
session:

* every ORM SELECT, UPDATE and DELETE only touches that user's rows, in every owned table;
* `Session.get()` returns None for another user's row, even when it is already in the identity map
  (see app.db.AppSession);
* new rows are stamped with the user automatically. A row that hangs off a job or an application (a
  tailored resume, an application, its events, follow-ups, workflow history) takes its owner from that
  parent, so work done by the scheduler and the workers — whose sessions belong to no one — is owned too.

Sessions without a user (migrations, the scheduler's system-wide passes, tests of internals) see all rows.
Because the filter lives here rather than in each endpoint, a query that forgets to filter by user
still cannot return another user's data.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from app.models import (AgentEffect, ApplicationClaim, ApplicationClaimKey, ApplicationExecutionAttempt, AtsCredential,
                        AtsRoute, AtsSession, SubmissionIntent,
                        ApplicationExecutionTask, AgentThread, EvidenceResumeLink, RagIndexEntry, RagIndexVersion,
                        AIConfigAuditEntry, AIConfigVersion, AIModel, AIOptimizationDecision, AIOptimizationReport,
                        AIOptimizationTask,
                        AIRoutingConfig, AppSetting, Application, ApplicationCommunicationEvent, ApplicationEmailMessage,
                        ApplicationEvent, ApplicationInboxDigest, CandidateProfile, GmailConnection, LiteLLMConnection,
                        ModelTier, WorkerModelConfig, ModelRouteState, FailoverEvent,
                        ConversationAction, Evidence, FollowUp, InsightObservation, Job, JobDescriptionFetchTask,
                        JobClassificationCache, JobClassificationTask, JobDescriptionParseTask, JobFeedbackEvent, JobInsight, JobInsightTask, JobQualificationTask, JobPreparationTask, ApprovedPacket, JobRanking,
                        JobRankingTask, RemovedPosting,
                        JobRequirementEvidence, JobStructuredDataChange, KnowledgeAnswer, McpToken, Resume,
                        ProviderCooldown, ProviderRateWindow, ResumeDataChange, ResumeDocument, ResumeExtraction,
                        ResumeExtractionFeedback, ResumeParseTask, ResumeVariant,
                        ResumeVersion, SavedSearch,
                        SearchProfile,
                        SearchRun, TailorTask, WorkflowTransition)

OWNED_MODELS = (CandidateProfile, AppSetting, ResumeDocument, KnowledgeAnswer, Evidence, Job, SearchRun, SearchProfile,
                SavedSearch, ProviderCooldown, ProviderRateWindow, Application, ResumeVariant, WorkflowTransition, ApplicationEvent, FollowUp, TailorTask, ResumeVersion,
                JobRankingTask, JobRanking, AIOptimizationTask, AIOptimizationReport, AIOptimizationDecision,
                ConversationAction, McpToken, Resume,
                ResumeDataChange, JobRequirementEvidence, JobStructuredDataChange, JobDescriptionParseTask,
                JobDescriptionFetchTask, RemovedPosting, JobFeedbackEvent, JobClassificationTask, JobClassificationCache,
                LiteLLMConnection, AIModel, ModelTier, WorkerModelConfig, AIRoutingConfig, AIConfigVersion,
                AIConfigAuditEntry, GmailConnection, ApplicationEmailMessage, ApplicationCommunicationEvent,
                ApplicationInboxDigest, JobInsightTask, JobInsight, InsightObservation, ModelRouteState, FailoverEvent,
                AgentThread, AgentEffect, EvidenceResumeLink, RagIndexVersion, RagIndexEntry, JobQualificationTask,
                JobPreparationTask, ApprovedPacket, ApplicationClaim, ApplicationClaimKey, ApplicationExecutionTask,
                ApplicationExecutionAttempt, SubmissionIntent, AtsRoute, AtsCredential, AtsSession, ResumeParseTask,
                ResumeExtraction, ResumeExtractionFeedback)
# Rows that inherit their owner from a parent row: model -> (relationship, foreign key, parent model).
INHERITED_OWNER = {
    Application: ("job", "job_id", Job),
    ResumeVariant: ("job", "job_id", Job),
    TailorTask: ("job", "job_id", Job),
    JobRankingTask: ("job", "job_id", Job),
    JobRanking: ("job", "job_id", Job),
    JobRequirementEvidence: ("job", "job_id", Job),
    JobStructuredDataChange: ("job", "job_id", Job),
    JobDescriptionParseTask: ("job", "job_id", Job),
    JobClassificationTask: ("job", "job_id", Job),
    JobDescriptionFetchTask: ("job", "job_id", Job),
    JobInsightTask: ("job", "job_id", Job),
    JobQualificationTask: ("job", "job_id", Job),
    JobPreparationTask: ("job", "job_id", Job),
    ApprovedPacket: ("job", "job_id", Job),
    ApplicationClaim: ("job", "job_id", Job),
    ApplicationClaimKey: ("claim", "claim_id", ApplicationClaim),
    ApplicationExecutionTask: ("job", "job_id", Job),
    ApplicationExecutionAttempt: ("task", "task_id", ApplicationExecutionTask),
    SubmissionIntent: ("job", "job_id", Job),
    JobInsight: ("job", "job_id", Job),
    InsightObservation: ("insight", "job_insight_id", JobInsight),
    WorkflowTransition: ("job", "job_id", Job),
    ApplicationEvent: ("application", "application_id", Application),
    FollowUp: ("application", "application_id", Application),
}


class NoUserContext(RuntimeError):
    """An operation that needs to know whose data it works on ran without a user."""


def require_user_id(db: Session) -> int:
    user_id = db.info.get("user_id")
    if user_id is None:
        raise NoUserContext("This operation needs a signed-in user or an explicit owner")
    return user_id


def owned_by(obj: Any, user_id: int) -> bool:
    return obj.user_id == user_id if isinstance(obj, OWNED_MODELS) else True


def inherited_owner(session: Session, obj: Any) -> int | None:
    """The owner of a child row, taken from the parent it belongs to."""
    relationship, foreign_key, parent_model = INHERITED_OWNER[type(obj)]
    parent = getattr(obj, relationship, None)
    if parent is None:
        parent_id = getattr(obj, foreign_key, None)
        parent = session.get(parent_model, parent_id) if parent_id is not None else None
    return getattr(parent, "user_id", None)


@event.listens_for(Session, "do_orm_execute")
def _only_the_users_rows(state) -> None:
    user_id = state.session.info.get("user_id")
    if user_id is None or state.is_column_load or state.is_relationship_load:
        return  # relationship loads inherit the criteria of the statement that loaded their parent
    if not (state.is_select or state.is_update or state.is_delete):
        return
    state.statement = state.statement.options(
        *[with_loader_criteria(model, lambda cls: cls.user_id == user_id, include_aliases=True)
          for model in OWNED_MODELS])


@event.listens_for(Session, "before_flush")
def _stamp_owner(session: Session, _flush_context, _instances) -> None:
    user_id = session.info.get("user_id")
    for obj in session.new:
        if not isinstance(obj, OWNED_MODELS) or obj.user_id is not None:
            continue
        # A child row is owned by whoever owns its job, whether or not this session belongs to a user.
        obj.user_id = inherited_owner(session, obj) if type(obj) in INHERITED_OWNER else user_id
