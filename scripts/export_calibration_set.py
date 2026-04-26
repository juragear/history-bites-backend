"""Export the calibration set to CSV for offline analysis.

One-shot ops tool, supporting Step 14 prep (LLM-as-judge build). Reads every
rated row from the prod `pool` table, writes a CSV with the columns the
analysis pipeline needs, and prints a summary block to stdout that Will can
paste into chat for the analysis discussion.

Lives in `scripts/` permanently — we may want to re-run this after Step 14's
judge ships to compare judge predictions against the human calibration set.

Usage (from repo root, with .venv active):

    # Default — 150 rated rows, CSV at /tmp/calibration_export_<ts>.csv
    .venv/bin/python scripts/export_calibration_set.py

    # Specific output path
    .venv/bin/python scripts/export_calibration_set.py --output /tmp/foo.csv

    # Include un-rated pending rows too (rare; for full-pool snapshots)
    .venv/bin/python scripts/export_calibration_set.py --include-pending

The DATABASE_URL env var controls which DB is targeted. Set it to the Railway
public-proxy URL when running against prod from a local shell (Sessions 5/6/8
pattern). pydantic-settings auto-loads .env so just running from repo root
typically works without an explicit export.

Out of scope (intentionally — this is ops tooling, not production code):
  - tests
  - HTTP exposure (calibration data shouldn't be public; SELECT-only via
    direct DB access keeps it inside operator-only territory)
  - DB writes of any kind — read-only, no UPDATE / DELETE / ALTER

Tag serialization quirk: review_tags is a JSON list in the DB. We `repr()`
the Python list into the CSV cell ("['surprising-angle', 'concrete-detail']")
so it round-trips via `ast.literal_eval` on read. Comma-separated unquoted
strings would break the day someone adds a tag with a comma in it.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import PoolFact


logger = logging.getLogger("calibration_export")


CSV_FIELDNAMES = [
    "id",
    "category",
    "region",
    "era",
    "fact_text",
    "external_id",
    "source_url",
    "model_used",
    "prompt_version",
    "status",
    "review_rating",
    "review_tags",
    "review_notes",
    "reviewed_at",
    "created_at",
]


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        stream=sys.stdout,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Export the calibration set (rated pool rows) to CSV.",
    )
    p.add_argument(
        "--output",
        type=str,
        default=None,
        help=(
            "CSV output path. Default: /tmp/calibration_export_<UTC-iso>.csv "
            "(timestamped so repeat runs don't overwrite each other)."
        ),
    )
    p.add_argument(
        "--include-pending",
        action="store_true",
        help=(
            "Include un-rated rows (review_rating IS NULL). Default off — "
            "the calibration set is rated rows only."
        ),
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Set log level to DEBUG.",
    )
    return p.parse_args()


def default_output_path() -> Path:
    """Timestamp the default path so repeat runs don't clobber each other.

    Colons are illegal in Windows paths and awkward on macOS, so we use a
    flat 'YYYY-MM-DDTHH-MM-SS' shape instead of a strict ISO-8601 string.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    return Path("/tmp") / f"calibration_export_{ts}.csv"


def serialize_tags(tags: list[str] | None) -> str:
    """Stable round-trip format for review_tags.

    `repr([])` -> "[]" and `repr(None)` -> "None" — both unambiguous on read
    via `ast.literal_eval`. We emit "" for None so the CSV cell is empty,
    matching how we treat all the other nullable columns.
    """
    if tags is None:
        return ""
    return repr(list(tags))


def serialize_dt(dt) -> str:
    if dt is None:
        return ""
    return dt.isoformat()


def row_to_dict(row: PoolFact) -> dict[str, str]:
    return {
        "id": str(row.id),
        "category": row.category or "",
        "region": row.region or "",
        "era": row.era or "",
        "fact_text": row.fact_text,
        "external_id": row.external_id,
        "source_url": row.source_url,
        "model_used": row.model_used,
        "prompt_version": row.prompt_version,
        "status": row.status,
        "review_rating": "" if row.review_rating is None else str(row.review_rating),
        "review_tags": serialize_tags(row.review_tags),
        "review_notes": row.review_notes or "",
        "reviewed_at": serialize_dt(row.reviewed_at),
        "created_at": serialize_dt(row.created_at),
    }


def fetch_rows(include_pending: bool) -> list[PoolFact]:
    """One SELECT against prod, ordered by id ASC for stable diffs."""
    stmt = select(PoolFact).order_by(PoolFact.id.asc())
    if not include_pending:
        stmt = stmt.where(PoolFact.review_rating.is_not(None))
    with SessionLocal() as session:
        return list(session.execute(stmt).scalars())


def write_csv(rows: list[PoolFact], output_path: Path) -> None:
    """csv.QUOTE_MINIMAL is the default — it auto-escapes embedded newlines,
    quotes, and commas in fact_text / review_notes. We don't pre-process: the
    operator's exact note text matters for Step 14 prompt construction."""
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=CSV_FIELDNAMES,
            quoting=csv.QUOTE_MINIMAL,
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row_to_dict(row))


# --- summary stats ----------------------------------------------------------


def _pct(n: int, total: int) -> float:
    return 100.0 * n / total if total else 0.0


def print_summary(rows: list[PoolFact], output_path: Path) -> None:
    total = len(rows)
    print()
    print("=== Calibration Set Summary ===")
    print(f"Exported: {total} rows")
    print(f"Output: {output_path}")

    if total == 0:
        print()
        print("(No rows matched the filter. Nothing to summarize.)")
        return

    # Rating distribution. Filter to the rated-only subset for this section
    # because pending rows have NULL rating; they can't contribute to a
    # rating histogram.
    rated = [r for r in rows if r.review_rating is not None]
    rated_total = len(rated)
    print()
    print("Rating distribution:")
    rating_counter = Counter(r.review_rating for r in rated)
    for r in (1, 2, 3, 4, 5):
        n = rating_counter.get(r, 0)
        print(f"  {r}: {n:>3} ({_pct(n, rated_total):>5.1f}%)")

    # Derived status (D26: >=4 approved, <=3 rejected). We compute from
    # rating rather than reading row.status so the summary matches D26
    # semantics even if a row's status got out of sync somehow.
    approved = sum(1 for r in rated if r.review_rating >= 4)
    rejected = sum(1 for r in rated if r.review_rating <= 3)
    print()
    print("Status (derived from rating per D26):")
    print(
        f"  approved (rating >= 4): {approved:>3} "
        f"({_pct(approved, rated_total):>5.1f}%)"
    )
    print(
        f"  rejected (rating <= 3): {rejected:>3} "
        f"({_pct(rejected, rated_total):>5.1f}%)"
    )

    # Top 5 most-applied tags (by frequency). Tags are JSON lists of strings;
    # explode and count.
    print()
    print("Top 5 most-applied tags (by frequency):")
    tag_counter: Counter[str] = Counter()
    for r in rated:
        for t in r.review_tags or []:
            tag_counter[t] += 1
    for tag, n in tag_counter.most_common(5):
        print(f"  {tag}: {n}")
    if not tag_counter:
        print("  (no tags applied)")

    # Region distribution: rows + approve rate. Only counts rows where the
    # category was tagged with a region (region IS NOT NULL).
    print()
    print("Region distribution (rows / approve rate):")
    by_region: dict[str | None, list[PoolFact]] = {}
    for r in rated:
        by_region.setdefault(r.region, []).append(r)
    # Sort by row count desc, NULL region last for readability.
    sorted_regions = sorted(
        by_region.items(),
        key=lambda kv: (kv[0] is None, -len(kv[1]), kv[0] or ""),
    )
    for region, region_rows in sorted_regions:
        n = len(region_rows)
        appr = sum(1 for r in region_rows if r.review_rating >= 4)
        label = region if region else "(no region)"
        print(f"  {label}: {n} rows / {_pct(appr, n):.1f}% approved")

    # Era distribution: same shape as region.
    print()
    print("Era distribution (rows / approve rate):")
    by_era: dict[str | None, list[PoolFact]] = {}
    for r in rated:
        by_era.setdefault(r.era, []).append(r)
    sorted_eras = sorted(
        by_era.items(),
        key=lambda kv: (kv[0] is None, -len(kv[1]), kv[0] or ""),
    )
    for era, era_rows in sorted_eras:
        n = len(era_rows)
        appr = sum(1 for r in era_rows if r.review_rating >= 4)
        label = era if era else "(no era)"
        print(f"  {label}: {n} rows / {_pct(appr, n):.1f}% approved")

    # Notes coverage.
    notes_rows = [r for r in rated if r.review_notes]
    notes_count = len(notes_rows)
    print()
    print("Notes coverage:")
    print(
        f"  Rows with notes: {notes_count} ({_pct(notes_count, rated_total):.1f}%)"
    )
    if notes_rows:
        avg_len = sum(len(r.review_notes) for r in notes_rows) / notes_count
        print(f"  Avg notes length (where present): {avg_len:.0f} chars")
    else:
        print("  Avg notes length (where present): n/a")

    # Borderline rows — the calibration lever for D26 threshold tuning later.
    borderline = sum(1 for r in rated if r.review_rating == 3)
    print()
    print(f"Borderline (rating=3) rows: {borderline}")


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    output_path = Path(args.output) if args.output else default_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(
        "starting export include_pending=%s output=%s",
        args.include_pending,
        output_path,
    )
    rows = fetch_rows(args.include_pending)
    logger.info("fetched %d rows", len(rows))

    write_csv(rows, output_path)
    logger.info("wrote csv path=%s rows=%d", output_path, len(rows))

    print_summary(rows, output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
