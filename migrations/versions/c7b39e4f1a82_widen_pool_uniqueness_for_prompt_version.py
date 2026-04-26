"""widen pool uniqueness to include prompt_version

Revision ID: c7b39e4f1a82
Revises: a4f2c1d83e91
Create Date: 2026-04-26 02:30:00.000000

Step 13d (D27): broaden the pool unique constraint from
    (source_name, external_id)
to
    (source_name, external_id, prompt_version)

Why: Step 13d runs a v1/v2 A/B against the same Wikipedia articles in the
"boring-even-if-true" cohort. Inserting a v2 row with the same (source_name,
external_id) as the existing v1 row violates the old constraint. Widening the
key by prompt_version unblocks multi-version coexistence in `pool` while still
catching duplicate v1 (or duplicate v2) inserts on the same article — the
intended safety net.

Topic-level dedup (D3) within a single prompt_version is unchanged and still
enforced application-side via `get_used_external_ids` in the generation
pipeline. This migration only relaxes the database-level constraint to allow
*cross-version* coexistence; it does not encourage *same-version* duplicates.

Idempotent: drops uq_pool_source_external if present, creates
uq_pool_source_external_prompt unconditionally. The downgrade is the inverse
but will fail on prod if any cross-version duplicates exist (intended — a
straight downgrade would silently lose data).
"""
from typing import Sequence, Union

from alembic import op


revision: str = "c7b39e4f1a82"
down_revision: Union[str, Sequence[str], None] = "a4f2c1d83e91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("uq_pool_source_external", "pool", type_="unique")
    op.create_unique_constraint(
        "uq_pool_source_external_prompt",
        "pool",
        ["source_name", "external_id", "prompt_version"],
    )


def downgrade() -> None:
    # Will fail loudly if any (source_name, external_id) collisions exist
    # across prompt_versions, which is exactly what we want — silently dropping
    # half the calibration set on a downgrade would be a worse outcome.
    op.drop_constraint("uq_pool_source_external_prompt", "pool", type_="unique")
    op.create_unique_constraint(
        "uq_pool_source_external",
        "pool",
        ["source_name", "external_id"],
    )
