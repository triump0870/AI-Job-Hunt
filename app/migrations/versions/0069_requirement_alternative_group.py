"""job_requirement_evidence.alternative_group: requirements a posting offers as interchangeable alternatives

"At least 5 years in one of Python, Java, Kotlin, ..." is one requirement met by any one language, not seven. Each
language keeps its own row (corrections and highlighting work per skill); rows with the same group id are read as
one requirement by every task (app.services.job_context.requirement_groups).

Additive and nullable: NULL means not part of a group. Rows parsed before this column are grouped at read time when
their shared sentence explicitly says "one of" / "any of", so no data is rewritten. Downgrade drops the column.

Revision ID: 0069
Revises: 0068
Create Date: 2026-11-14 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0069'
down_revision: str | None = '0068'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('job_requirement_evidence') as batch:
        batch.add_column(sa.Column('alternative_group', sa.String(60), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('job_requirement_evidence') as batch:
        batch.drop_column('alternative_group')
