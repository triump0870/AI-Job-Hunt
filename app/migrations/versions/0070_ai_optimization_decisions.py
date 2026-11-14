"""ai_optimization_decisions: what a person did with an AI optimization recommendation

One row per decision: a one-click change staged in the AI configuration draft, applied as a config version, undone or
cancelled, or a recommendation dismissed for a while. The configuration change itself still goes only through the
draft → preview → apply flow and its versions; this table links a version back to the recommendation behind it.

Additive: a new owned table. Downgrade drops it.

Revision ID: 0070
Revises: 0069
Create Date: 2026-11-14 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0070'
down_revision: str | None = '0069'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = ("STAGED", "APPLIED", "UNDONE", "CANCELLED", "DISMISSED")


def upgrade() -> None:
    op.create_table(
        'ai_optimization_decisions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE',
                                                         name='fk_ai_optimization_decisions_user_id'), nullable=False),
        sa.Column('report_id', sa.Integer(), sa.ForeignKey('ai_optimization_reports.id', ondelete='SET NULL',
                                                           name='fk_ai_optimization_decisions_report_id'),
                  nullable=True),
        sa.Column('recommendation_key', sa.String(40), nullable=False),
        sa.Column('action_id', sa.String(160), nullable=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('title', sa.String(300), nullable=False, server_default=''),
        sa.Column('detail', sa.JSON(), nullable=True),
        sa.Column('draft_revision', sa.Integer(), nullable=True),
        sa.Column('base_version', sa.Integer(), nullable=True),
        sa.Column('config_version', sa.Integer(), nullable=True),
        sa.Column('undo_version', sa.Integer(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"status IN ({', '.join(repr(s) for s in STATUSES)})",
                           name='ck_ai_optimization_decisions_status'),
    )
    op.create_index('ix_ai_optimization_decisions_user_id', 'ai_optimization_decisions', ['user_id'])
    op.create_index('ix_ai_optimization_decisions_report_id', 'ai_optimization_decisions', ['report_id'])
    op.create_index('ix_ai_optimization_decisions_user_key', 'ai_optimization_decisions',
                    ['user_id', 'recommendation_key'])


def downgrade() -> None:
    op.drop_index('ix_ai_optimization_decisions_user_key', table_name='ai_optimization_decisions')
    op.drop_index('ix_ai_optimization_decisions_report_id', table_name='ai_optimization_decisions')
    op.drop_index('ix_ai_optimization_decisions_user_id', table_name='ai_optimization_decisions')
    op.drop_table('ai_optimization_decisions')
