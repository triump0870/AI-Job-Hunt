"""job_insight_tasks.reason BEFORE_APPROVAL

Research may run when a packet reaches Approvals, before the person decides, for accounts that turn it on (Running
workers → Settings → Job insights, "Research before approval"). Those tasks are recorded with their own reason. This
re-creates the reason check with the new value; nothing else changes.

Revision ID: 0071
Revises: 0070
Create Date: 2026-11-20 10:00:00
"""
from collections.abc import Sequence

from alembic import op

revision: str = '0071'
down_revision: str | None = '0070'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of app.models.JOB_INSIGHT_TASK_REASONS before and at this revision.
OLD_REASONS = ('AUTO_APPROVED', 'USER_REQUEST', 'REFRESH', 'BACKLOG')
NEW_REASONS = (*OLD_REASONS, 'BEFORE_APPROVAL')


def _check(reasons: Sequence[str]) -> None:
    with op.batch_alter_table('job_insight_tasks') as batch:
        batch.drop_constraint('ck_job_insight_tasks_reason', type_='check')
        batch.create_check_constraint('ck_job_insight_tasks_reason',
                                      "reason IN ({})".format(", ".join(repr(r) for r in reasons)))


def upgrade() -> None:
    _check(NEW_REASONS)


def downgrade() -> None:
    op.execute("UPDATE job_insight_tasks SET reason = 'AUTO_APPROVED' WHERE reason = 'BEFORE_APPROVAL'")
    _check(OLD_REASONS)
