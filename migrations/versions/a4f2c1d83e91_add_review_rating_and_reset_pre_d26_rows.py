"""add review_rating and reset pre-D26 rated rows

Revision ID: a4f2c1d83e91
Revises: 271733dcde42
Create Date: 2026-04-26 00:00:00.000000

Step 13c (D26): primary review label flips from binary action -> 1-5 ordinal.
Two operations in one migration:

  1. Add `review_rating SMALLINT NULL` with CHECK 1-5. Nullable because rows
     reviewed pre-D26 don't have a rating; the endpoint sets it on every
     re-rating going forward.
  2. Reset pre-D26 rated rows back to pending_review so they appear in the
     review queue for re-rating with the new instrument. Tags + notes are
     preserved (review_tags / review_notes columns left intact). Per D26 we
     re-rate rather than mechanically backfilling (approved -> 4, rejected -> 2)
     because the pre-D26 binary labels were already noisy.

The WHERE clause `reviewed_at IS NOT NULL AND review_rating IS NULL` is
intentional: it scopes the reset to rows that were rated under the old
instrument only. Once D26 is live and rows have a `review_rating`, this
clause won't reset them on a re-run -- the migration is idempotent.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4f2c1d83e91"
down_revision: Union[str, Sequence[str], None] = "271733dcde42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pool",
        sa.Column("review_rating", sa.SmallInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_pool_review_rating_range",
        "pool",
        "review_rating IS NULL OR (review_rating BETWEEN 1 AND 5)",
    )
    # D26: reset pre-D26 rated rows to pending for re-rating with the new
    # instrument. Tags + notes preserved.
    op.execute(
        """
        UPDATE pool
        SET status = 'pending_review',
            reviewed_at = NULL
        WHERE reviewed_at IS NOT NULL
          AND review_rating IS NULL
        """
    )


def downgrade() -> None:
    op.drop_constraint("ck_pool_review_rating_range", "pool", type_="check")
    op.drop_column("pool", "review_rating")
