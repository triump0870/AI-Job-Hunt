"""ai_models.router_models: the models a discovered auto router sends requests to

Reported by LiteLLM (complexity_router_config) and refreshed by a sync, so the catalog can say whether a router runs on
local models, cloud models or both. A discovered fact, not configuration: it is not part of a config version.

Additive and nullable: NULL means unknown until the next sync. Downgrade drops the column.

Revision ID: 0068
Revises: 0067
Create Date: 2026-11-09 09:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0068'
down_revision: str | None = '0067'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('ai_models') as batch:
        batch.add_column(sa.Column('router_models', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('ai_models') as batch:
        batch.drop_column('router_models')
