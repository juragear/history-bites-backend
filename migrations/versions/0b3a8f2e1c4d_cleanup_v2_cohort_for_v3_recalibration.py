"""cleanup v2 cohort for v3 recalibration

Revision ID: 0b3a8f2e1c4d
Revises: c7b39e4f1a82
Create Date: 2026-04-26 12:00:00.000000

Step 13e: hard DELETE of all `pool` rows with `prompt_version='v2'`.

The v2 batch (~100 rows in prod at run time) is contaminated and not useful
calibration data. Each row was generated against:

  - Gemini 2.5 Flash (the v3 step upgrades to Gemini 3 Flash Preview)
  - REST `/page/summary/` extracts only (~800 chars; v3 uses the action API
    extracts endpoint with section-aware truncation up to 15k chars)
  - No working pre-filter (List/Timeline/Society-of/Election articles slipped
    through; v3 adds title regex + extract-length floor + infobox-shape
    detector)
  - The v2 prompt rules (v3 rewrites the prompt to address six failure
    clusters Will surfaced from the v2 review)

Keeping v2 rows alongside v3 rows would skew any downstream comparison —
they're not v1's baseline, they're not v3's output, they're a one-off
diagnostic batch. The historical V2_PROMPT text is preserved verbatim in
the Session 13d Claude Code Log entry; the v2 row content itself is not
worth backing up.

The 150 v1 rows (33 approved + 117 rejected) are untouched — they remain
the calibration baseline.

Idempotent: re-running the DELETE is a no-op once the v2 rows are gone.
Downgrade is intentionally a no-op — the rows are gone, restoring them is
not worth it.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0b3a8f2e1c4d"
down_revision: Union[str, Sequence[str], None] = "c7b39e4f1a82"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DELETE FROM pool WHERE prompt_version = 'v2'")


def downgrade() -> None:
    # Irreversible. v2 rows were diagnostic-grade; backup not warranted.
    pass
