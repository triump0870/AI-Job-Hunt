"""Audit trail for operations requested from a conversation (Claude Desktop over MCP).

Argument summaries keep IDs, counts, filters and text lengths only, so resumes, reviews and secrets never
end up in the audit table.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import ConversationAction

SOURCE_CLAUDE_DESKTOP = "CLAUDE_DESKTOP"
# Scalar arguments kept as they are (filters and identifiers). Everything else is reduced to its size.
KEPT_SCALARS = {"job_id", "job_ids", "minimum_fit_score", "posted_after", "location", "company", "limit",
                "operation_id", "recommendation", "review_score", "confidence", "tailored_resume_version",
                "reviewer_score_above", "include_blocked", "offset", "formats", "include_content", "outcome",
                "blocker_reason", "question_ids", "answer_type", "category", "sensitive", "required",
                "who_applies", "cover_letter_files", "query", "stage", "within_days", "application_id",
                "followup_id", "channel"}


def summarize_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in arguments.items():
        if value is None:
            continue
        if key in KEPT_SCALARS and isinstance(value, (int, float, bool)):
            out[key] = value
        elif key in KEPT_SCALARS and isinstance(value, str):
            out[key] = value[:120]
        elif key in KEPT_SCALARS and isinstance(value, list) and all(isinstance(v, int) for v in value):
            out[key] = value[:50]
        elif isinstance(value, list):
            out[key] = {"count": len(value),
                        "job_ids": [v["job_id"] for v in value if isinstance(v, dict) and isinstance(v.get("job_id"), int)][:50]}
        elif isinstance(value, dict):
            out[key] = {"job_id": value.get("job_id") if isinstance(value.get("job_id"), int) else None,
                        "fields": sorted(str(k) for k in value)[:30]}
        elif isinstance(value, str):
            out[key] = {"length": len(value)}
    return out


def record(db: Session, *, tool_name: str, arguments: dict[str, Any], result_status: str,
           entity_type: str | None = None, entity_id: int | None = None, operation_id: str | None = None,
           result_summary: dict[str, Any] | None = None, source: str = SOURCE_CLAUDE_DESKTOP) -> ConversationAction:
    row = ConversationAction(source=source, tool_name=tool_name, entity_type=entity_type, entity_id=entity_id,
                             operation_id=operation_id, arguments_summary=summarize_arguments(arguments),
                             result_status=result_status, result_summary=result_summary)
    db.add(row)
    db.commit()
    return row


def latest_operation(db: Session, operation_id: str | None) -> ConversationAction | None:
    query = select(ConversationAction).where(ConversationAction.operation_id.is_not(None))
    if operation_id:
        query = query.where(ConversationAction.operation_id == operation_id)
    return db.scalar(query.order_by(desc(ConversationAction.id)).limit(1))
