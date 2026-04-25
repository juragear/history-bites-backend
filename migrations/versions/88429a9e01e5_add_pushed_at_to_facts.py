"""add pushed_at to facts

Revision ID: 88429a9e01e5
Revises: 0a98a20fdb07
Create Date: 2026-04-25 13:15:00.000000

Step 9: track when run_push successfully delivered each fact to FCM. Nullable
because rows that pre-date Step 9 never had a push event and shouldn't get a
synthesized timestamp. /health uses MAX(pushed_at) — small table, no index.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "88429a9e01e5"
down_revision: Union[str, Sequence[str], None] = "0a98a20fdb07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "facts",
        sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("facts", "pushed_at")
