"""List approved pool rows in a curatable format for launch lineup picking.

One-shot read-only ops tool (Step 15). Lives in scripts/ permanently — useful
any time a launch lineup needs re-curation (e.g. after Step 14.5 retunes the
prompt or operator-rating drifts the approved set).

Sorts approved rows by (prompt_version preference, rating descending) so the
strongest current-prompt facts surface first. Default preference order is
v3 / v4.1 / v4 / v1 (most recent production voice -> oldest), since v3 is
the active production prompt as of Step 14 and v4.1 is the warmer tonal
variant from Step 13f addition.

Usage (from repo root, with .venv active):

    .venv/bin/python scripts/list_approved_for_launch.py
    .venv/bin/python scripts/list_approved_for_launch.py --prefer v3,v4.1,v4,v1 --limit 30
    .venv/bin/python scripts/list_approved_for_launch.py --limit 73          # show all approved

The DATABASE_URL env var (auto-loaded from .env via pydantic-settings)
controls which DB is targeted. Set to the Railway public-proxy URL when
running against prod from a local shell.

Out of scope (intentional, ops not production):
  - Tests
  - DB writes (read-only SELECT against pool)
  - Any randomness — operator picks from the printed list (no random module)
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import PoolFact


# Default preference order. Higher-ranked versions sort first.
DEFAULT_PREFERENCE = ["v3", "v4.1", "v4", "v1"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "List approved pool rows in a curatable format. Sort by "
            "(prompt_version preference, rating desc) so the strongest "
            "candidates surface first."
        ),
    )
    p.add_argument(
        "--prefer",
        default=",".join(DEFAULT_PREFERENCE),
        help=(
            "Comma-separated preference order for prompt_versions. Earlier "
            "entries sort first. Unknown versions sort last. "
            f"Default: {','.join(DEFAULT_PREFERENCE)}"
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=30,
        help=(
            "Cap on rows printed. Default 30 — full pool of 73 approved is "
            "more than the curation choice needs and burns scrollback."
        ),
    )
    return p.parse_args()


def sort_key(row: PoolFact, preference: list[str]) -> tuple[int, int, int]:
    """Sort: preferred prompt_version first, then highest rating, then lowest pool_id (stable within group)."""
    try:
        version_rank = preference.index(row.prompt_version)
    except ValueError:
        version_rank = len(preference)  # unknown versions sort last
    rating_rank = -(row.review_rating or 0)  # higher rating first via negation
    return (version_rank, rating_rank, row.id)


def main() -> int:
    args = parse_args()
    preference = [v.strip() for v in args.prefer.split(",") if v.strip()]

    with SessionLocal() as session:
        rows = list(
            session.scalars(
                select(PoolFact).where(PoolFact.status == "approved")
            )
        )

    rows.sort(key=lambda r: sort_key(r, preference))
    rows = rows[: args.limit]

    if not rows:
        print("No approved rows found.", file=sys.stderr)
        return 1

    # Group by prompt_version for readability. Within each group, rows are
    # already sorted by rating desc (from sort_key above).
    by_version: dict[str, list[PoolFact]] = {}
    for r in rows:
        by_version.setdefault(r.prompt_version, []).append(r)

    # Print groups in preference order; unknown versions appended after.
    print(f"Approved pool rows (top {len(rows)} by preference + rating):")
    print(f"Preference order: {' > '.join(preference)}")
    print()

    seen_versions: set[str] = set()
    for v in preference:
        if v in by_version:
            _print_group(v, by_version[v])
            seen_versions.add(v)
    for v, group in by_version.items():
        if v not in seen_versions:
            _print_group(v, group)

    return 0


def _print_group(version: str, group: list[PoolFact]) -> None:
    print(f"\n=== {version} (n={len(group)}) ===\n")
    for r in group:
        tags_str = ",".join(r.review_tags or []) or "(none)"
        print(
            f"[pool_id={r.id}] {r.region or '?'} / {r.era or '?'} | "
            f"rating={r.review_rating} | tags: {tags_str}"
        )
        print(f"> {r.fact_text}")
        if r.review_notes:
            # Single-line collapse for note display
            note_oneline = " ".join(r.review_notes.split())
            print(f"notes: \"{note_oneline}\"")
        print()


if __name__ == "__main__":
    sys.exit(main())
