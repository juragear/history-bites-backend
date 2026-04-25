"""add review_tags and review_notes to pool

Revision ID: 271733dcde42
Revises: 88429a9e01e5
Create Date: 2026-04-25 18:00:00.000000

Step 13a: tagged review UI. Two new nullable columns on the `pool` table so
Will can attach structured commentary to each approve/reject action.

  - `review_tags` — JSON list of kebab-case strings drawn from the palette in
    app.review_tags. JSON (not ARRAY) deliberately: SQLAlchemy `JSON` maps to
    JSONB on Postgres and serialized TEXT on SQLite, so the test suite (Session
    11 conftest, in-memory SQLite) doesn't need a `@compiles` hook. We don't
    need `@>` containment queries today; if we ever do, JSONB covers it.
  - `review_notes` — TEXT, ≤500 chars (truncation enforced in the endpoint, not
    the column, so a typo doesn't 4xx mid-review).

Both default to NULL for existing rows. No backfill — pre-Step-13a reviews
simply have no tags or notes attached, which is fine.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "271733dcde42"
down_revision: Union[str, Sequence[str], None] = "88429a9e01e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pool",
        sa.Column("review_tags", sa.JSON(), nullable=True),
    )
    op.add_column(
        "pool",
        sa.Column("review_notes", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pool", "review_notes")
    op.drop_column("pool", "review_tags")
