"""add judge columns to pool

Revision ID: d4e9c5b27a13
Revises: 0b3a8f2e1c4d
Create Date: 2026-04-27 15:00:00.000000

Step 14 (D23): adds the LLM-as-judge fields to `pool`.

  - `judge_score` FLOAT NULL — predicted rating in [1.0, 5.0], one
    decimal place; CHECK constraint enforces the range.
  - `judge_verdict` VARCHAR(20) NULL — one of `auto_approve` /
    `auto_reject` / `borderline`; CHECK constraint enforces the
    enum.
  - `judge_reason` TEXT NULL — short audit string from the judge,
    max ~300 chars at write time but no DB-side length cap.

All three nullable. Pre-Step-14 rows (the 196 v1+v3 rated rows used as
the calibration baseline, plus the 30 v4 + 5 v4.1 already in pool) keep
NULL across the three. Rows generated after Step 14 ships always have
all three populated — judge failures populate verdict='borderline' with
a reason explaining why (see app/generation.py).

The CHECK constraints are belt-and-suspenders against the
threshold-mapping logic in app/judge.py — a bad judge response that
somehow slipped through Python validation can't end up in the DB with
a junk score or unknown verdict. ck_pool_judge_score_range allows
NULL or [1.0, 5.0]; ck_pool_judge_verdict_values allows NULL or one
of the three enum values.

Idempotent on re-run: ADD COLUMN ... will fail loudly if columns
already exist, which is the correct behaviour (don't silently double-
add).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e9c5b27a13"
down_revision: Union[str, Sequence[str], None] = "0b3a8f2e1c4d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("pool", sa.Column("judge_score", sa.Float(), nullable=True))
    op.add_column("pool", sa.Column("judge_verdict", sa.String(length=20), nullable=True))
    op.add_column("pool", sa.Column("judge_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_pool_judge_score_range",
        "pool",
        "judge_score IS NULL OR (judge_score >= 1.0 AND judge_score <= 5.0)",
    )
    op.create_check_constraint(
        "ck_pool_judge_verdict_values",
        "pool",
        "judge_verdict IS NULL OR judge_verdict IN ('auto_approve', 'auto_reject', 'borderline')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_pool_judge_verdict_values", "pool", type_="check")
    op.drop_constraint("ck_pool_judge_score_range", "pool", type_="check")
    op.drop_column("pool", "judge_reason")
    op.drop_column("pool", "judge_verdict")
    op.drop_column("pool", "judge_score")
