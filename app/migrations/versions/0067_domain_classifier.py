"""job_classification_tasks, job_classification_cache: the hybrid domain classifier

* job_classification_tasks: the queue of app.services.domain_classifier and the record of why each job was kept or hidden.
* job_classification_cache: the model's answer per candidate x posting window x prompt version, per account.
* llm_request_logs.stage gains DOMAIN_CLASSIFIER; job_feedback_events.action gains BULK_DELETED.

Additive: with DOMAIN_CLASSIFIER_MODE=off nothing writes to either table. Downgrade drops both and re-labels the rows
that use the new values (DOMAIN_CLASSIFIER as OTHER, BULK_DELETED as DELETED, which is what it replaced).

Revision ID: 0067
Revises: 0066
Create Date: 2026-11-08 09:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0067'
down_revision: str | None = '0066'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies at this revision.
OLD_STAGES = ('RANKER', 'TAILOR', 'REVIEWER', 'RESUME_PARSER', 'JD_PARSER', 'JD_RECOVERY', 'APPLICATION', 'FOLLOW_UP',
              'INBOX', 'COMPANY_INSIGHTS', 'INTERVIEW_INSIGHTS', 'ADVISOR', 'BROWSER_NAVIGATION', 'OTHER')
NEW_STAGES = ('RANKER', 'TAILOR', 'REVIEWER', 'RESUME_PARSER', 'JD_PARSER', 'JD_RECOVERY', 'APPLICATION', 'FOLLOW_UP',
              'INBOX', 'COMPANY_INSIGHTS', 'INTERVIEW_INSIGHTS', 'ADVISOR', 'BROWSER_NAVIGATION', 'DOMAIN_CLASSIFIER',
              'OTHER')
OLD_ACTIONS = ('DISMISSED', 'REJECTED', 'DELETED', 'SHORTLISTED', 'TAILORED', 'APPROVED', 'APPLIED', 'KEPT')
NEW_ACTIONS = (*OLD_ACTIONS, 'BULK_DELETED')
STATUSES = ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED', 'SKIPPED')
ACTIVE = "status IN ('PENDING', 'PROCESSING')"


def _in(column: str, values: tuple[str, ...]) -> str:
    return "{} IN ({})".format(column, ", ".join(repr(v) for v in values))


def _owner(table: str) -> sa.Column:
    return sa.Column('user_id', sa.Integer(),
                     sa.ForeignKey('users.id', ondelete='CASCADE', name=f'fk_{table}_user_id'), nullable=False)


def _checks(stages: tuple[str, ...], actions: tuple[str, ...]) -> None:
    with op.batch_alter_table('llm_request_logs') as batch:
        batch.drop_constraint('ck_llm_request_logs_stage', type_='check')
        batch.create_check_constraint('ck_llm_request_logs_stage', _in('stage', stages))
    with op.batch_alter_table('job_feedback_events') as batch:
        batch.drop_constraint('ck_job_feedback_events_action', type_='check')
        batch.create_check_constraint('ck_job_feedback_events_action', _in('action', actions))


def upgrade() -> None:
    _checks(NEW_STAGES, NEW_ACTIONS)
    op.create_table(
        'job_classification_tasks',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('job_classification_tasks'),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('jobs.id', ondelete='CASCADE',
                                                        name='fk_job_classification_tasks_job_id'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='PENDING'),
        sa.Column('mode', sa.String(10), nullable=False, server_default='ON'),
        sa.Column('reason', sa.String(40), nullable=False, server_default='new'),
        sa.Column('route', sa.String(20), nullable=False, server_default='ASK'),
        sa.Column('signals', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('title_only', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('legacy_match', sa.Boolean(), nullable=True),
        sa.Column('legacy_reason', sa.String(300), nullable=True),
        sa.Column('model_classification', sa.String(20), nullable=True),
        sa.Column('classification', sa.String(20), nullable=True),
        sa.Column('explanation', sa.String(300), nullable=True),
        sa.Column('matched_skills', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('irrelevance_evidence', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('gate_code', sa.String(40), nullable=True),
        sa.Column('cache_hit', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('prompt_version', sa.String(20), nullable=True),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('locked_by', sa.String(200), nullable=True),
        sa.Column('lock_token', sa.String(36), nullable=True),
        sa.Column('lock_expires_at', sa.DateTime(), nullable=True),
        sa.Column('run_now_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint(_in('status', STATUSES), name='ck_job_classification_tasks_status'),
        sa.CheckConstraint('attempt_count >= 0', name='ck_job_classification_tasks_attempt_count'),
    )
    op.create_index('ix_job_classification_tasks_user_id', 'job_classification_tasks', ['user_id'])
    op.create_index('ix_job_classification_tasks_job_id', 'job_classification_tasks', ['job_id'])
    op.create_index('ix_job_classification_tasks_claim', 'job_classification_tasks', ['status', 'next_attempt_at'])
    op.create_index('ix_job_classification_tasks_user_created', 'job_classification_tasks', ['user_id', 'created_at'])
    op.create_index('uq_job_classification_tasks_active_job', 'job_classification_tasks', ['job_id'], unique=True,
                    postgresql_where=sa.text(ACTIVE), sqlite_where=sa.text(ACTIVE))
    op.create_table(
        'job_classification_cache',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('job_classification_cache'),
        sa.Column('cache_key', sa.String(64), nullable=False),
        sa.Column('answer', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('prompt_version', sa.String(20), nullable=True),
        sa.Column('model', sa.String(300), nullable=True),
        sa.Column('hits', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'cache_key', name='uq_job_classification_cache_user_key'),
    )
    op.create_index('ix_job_classification_cache_user_id', 'job_classification_cache', ['user_id'])
    op.create_index('ix_job_classification_cache_expires', 'job_classification_cache', ['expires_at'])


def downgrade() -> None:
    op.drop_table('job_classification_cache')
    op.drop_table('job_classification_tasks')
    op.execute(sa.text("UPDATE llm_request_logs SET stage = 'OTHER' WHERE stage = 'DOMAIN_CLASSIFIER'"))
    op.execute(sa.text("UPDATE job_feedback_events SET action = 'DELETED' WHERE action = 'BULK_DELETED'"))
    op.execute(sa.text("UPDATE jobs SET domain_match = NULL WHERE domain_match = 'PENDING'"))
    _checks(OLD_STAGES, OLD_ACTIONS)
