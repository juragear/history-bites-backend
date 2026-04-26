"""Targeted A/B regeneration: v2 prompt against the v1 boring-even-if-true cohort.

Step 13d ops tool. Lives in `scripts/` permanently — v3+ iterations may want
to reuse the cohort-targeting pattern.

What it does:
  1. Connect to the configured DB (prod via Railway public proxy in
     practice — set DATABASE_URL in the local shell to point there).
  2. Find every v1 pool row tagged `boring-even-if-true` (the dominant v1
     failure mode at 65/150 rows in the calibration set).
  3. For each, fetch the same Wikipedia article (via title parsed from the
     v1 source_url) and call the model with V2_PROMPT.
  4. Insert a new pool row with `prompt_version='v2'`, same external_id,
     same category/region/era, status='pending_review'.

What it deliberately does NOT do:
  - Call get_used_external_ids / dedup. The whole point is duplicate
    external_id, distinguished by prompt_version. The widened uniqueness
    constraint (D27, migration c7b39e4f1a82) makes that legal.
  - Touch the v1 row. The A/B comparison depends on both versions surviving.
  - Mutate Railway env vars. PROMPT_VERSION on Railway stays v1 throughout
    Step 13d; the v2 selection is local-only via runtime override of
    settings.PROMPT_VERSION at script entry.

Usage (from repo root, with .venv active):

    # Plan only — no generation, no DB writes.
    .venv/bin/python scripts/regenerate_with_v2.py --dry-run

    # Spot-check 3 rows end-to-end before committing to the full run.
    .venv/bin/python scripts/regenerate_with_v2.py --limit 3 --confirm

    # Full A/B regeneration (~65 rows, ~$0.03 in Gemini spend).
    .venv/bin/python scripts/regenerate_with_v2.py --confirm

The DATABASE_URL env var (from .env, auto-loaded by pydantic-settings)
controls which DB is targeted. Set it to the Railway public-proxy URL when
running against prod from a local shell.

Out of scope (intentionally — ops tooling, not production code):
  - tests; provider behavior is exercised implicitly by the run
  - retry orchestration on top of the provider's existing failure modes;
    on a per-row error, log and skip
  - Slack alerting; the script is run interactively, the operator sees stdout
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import timedelta
from urllib.parse import unquote

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import wikipedia
from app.config import settings
from app.db import SessionLocal
from app.generation import is_valid
from app.model_provider import ModelProviderError, get_provider
from app.models import PoolFact


# Cost estimate constants — mirror generate_calibration_set.py for
# consistency. Per-call ~$0.0005 typical; 3× worst-case multiplier doesn't
# apply here because the regen path is single-attempt-per-row (no retry on
# provider error — log + skip).
PER_CALL_USD_TYPICAL = 0.0005

PROGRESS_EVERY = 10

TARGET_TAG = "boring-even-if-true"
SOURCE_PROMPT_VERSION = "v1"
TARGET_PROMPT_VERSION = "v2"

logger = logging.getLogger("regenerate_v2")


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        stream=sys.stdout,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Regenerate the v1 boring-even-if-true cohort with the v2 prompt. "
            "Adds new pool rows with prompt_version='v2', same external_id."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Cap on number of rows to regenerate. Default: process all "
            "matching rows. Useful for spot-checks (e.g. --limit 3)."
        ),
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Skip the y/N prompt. Required for non-interactive runs.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the plan, sample external_ids, and cost estimate; do not "
            "touch Wikipedia, the model, or the DB."
        ),
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Set log level to DEBUG.",
    )
    return p.parse_args()


def fetch_v1_boring_rows(limit: int | None = None) -> list[PoolFact]:
    """Read the v1 boring cohort.

    Two-step filter: SQL narrows to (prompt_version='v1', review_tags IS NOT
    NULL), then Python filters by tag membership. The Python pass keeps the
    query portable across SQLite (tests) and Postgres (prod) without a
    JSON_CONTAINS hack — and the working set is small (~150 rows) so the
    in-memory filter is fine.
    """
    stmt = (
        select(PoolFact)
        .where(
            PoolFact.review_tags.is_not(None),
            PoolFact.prompt_version == SOURCE_PROMPT_VERSION,
        )
        .order_by(PoolFact.id.asc())
    )
    with SessionLocal() as session:
        all_v1_tagged = list(session.scalars(stmt))
    matching = [
        r for r in all_v1_tagged
        if r.review_tags and TARGET_TAG in r.review_tags
    ]
    if limit is not None:
        matching = matching[:limit]
    return matching


def title_from_source_url(source_url: str) -> str:
    """Pull the Wikipedia article title from a stored source_url.

    Wikipedia source_urls in pool look like:
      https://en.wikipedia.org/wiki/Some_Article_Title
    Title comes after `/wiki/`; URL-decode percent escapes; underscores stay
    (the REST summary endpoint accepts them and quote()s them itself).
    """
    marker = "/wiki/"
    idx = source_url.find(marker)
    if idx < 0:
        raise ValueError(
            f"source_url does not look like a Wikipedia article URL: {source_url!r}"
        )
    raw = source_url[idx + len(marker):]
    return unquote(raw)


def print_plan(
    rows: list[PoolFact],
    limit: int | None,
) -> None:
    n = len(rows)
    typical = n * PER_CALL_USD_TYPICAL
    print("--- v2 regeneration plan ---")
    print(f"target tag:         {TARGET_TAG!r}")
    print(f"source prompt:      {SOURCE_PROMPT_VERSION}")
    print(f"target prompt:      {TARGET_PROMPT_VERSION}")
    print(f"matching v1 rows:   {n}")
    if limit is not None:
        print(f"limit applied:      {limit}")
    print(f"cost estimate:      ~${typical:.4f} typical")
    print()
    print("first 5 (id, category, external_id, source_url):")
    for r in rows[:5]:
        print(
            f"  pool_id={r.id} category={r.category} "
            f"external_id={r.external_id} url={r.source_url}"
        )


async def regenerate_one(
    v1_row: PoolFact,
    provider,
    model_used: str,
) -> tuple[str, str | None]:
    """Regenerate one row with V2.

    Returns (outcome, detail) where outcome is one of:
      "ok"          -> v2 row inserted
      "skip-404"    -> Wikipedia returned 4xx (article moved/deleted)
      "skip-fetch"  -> non-4xx fetch error
      "skip-model"  -> provider error
      "skip-invalid"-> validation failed (empty / >280 chars)
      "skip-dup"    -> integrity error (a v2 row already exists for this id)
    detail carries a short error string for logging on failure.
    """
    title = title_from_source_url(v1_row.source_url)
    try:
        extract = await wikipedia.fetch_extract(title)
    except httpx.HTTPStatusError as exc:
        if 400 <= exc.response.status_code < 500:
            return "skip-404", f"wikipedia {exc.response.status_code} for {title!r}"
        return "skip-fetch", f"wikipedia {exc.response.status_code} for {title!r}"
    except Exception as exc:
        return "skip-fetch", f"wikipedia error for {title!r}: {exc!r}"

    try:
        fact_text = await provider.extract_fact(extract.extract)
    except ModelProviderError as exc:
        return "skip-model", str(exc)

    if not is_valid(fact_text):
        return "skip-invalid", f"len={len(fact_text)}"

    # Use the v1 row's stored metadata for category/region/era so the v1/v2
    # rows match on every attribute except fact_text + prompt_version. Use
    # the freshly-fetched extract for source_url and external_id (Wikipedia
    # may have updated either via redirect; the page_id is the canonical
    # identifier and we should record what we actually fetched).
    new_row = PoolFact(
        fact_text=fact_text,
        source_name=v1_row.source_name,
        source_url=extract.source_url,
        source_license=v1_row.source_license,
        external_id=str(extract.page_id),
        language=v1_row.language,
        category=v1_row.category,
        region=v1_row.region,
        era=v1_row.era,
        model_used=model_used,
        prompt_version=TARGET_PROMPT_VERSION,
        status="pending_review",
    )
    with SessionLocal() as session:
        session.add(new_row)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            return "skip-dup", str(exc.orig) if exc.orig else str(exc)
        # Refresh inside the session so we can log the assigned id.
        session.refresh(new_row)
        return "ok", f"new pool_id={new_row.id} external_id={new_row.external_id}"


def _model_name() -> str:
    if settings.MODEL_PROVIDER == "gemini":
        return settings.GEMINI_MODEL
    return settings.OLLAMA_MODEL


def fmt_duration(seconds: float) -> str:
    td = timedelta(seconds=int(seconds))
    minutes, secs = divmod(int(td.total_seconds()), 60)
    return f"{minutes}m{secs:02d}s"


async def run() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    # Plan
    rows = fetch_v1_boring_rows(limit=args.limit)
    print_plan(rows, args.limit)

    if not rows:
        print("\nno matching rows. exit.")
        return 0

    if args.dry_run:
        print("\n[dry-run] not generating. exit.")
        return 0

    if not args.confirm:
        try:
            answer = input("\nProceed? [y/N]: ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("aborted.")
            return 1

    # Force v2 for the duration of this script. The change is in-process
    # only — Railway's PROMPT_VERSION env var is unaffected, so the prod
    # cron continues to generate v1 rows in parallel. Documented in the
    # docstring; this is the spec-endorsed pattern (per the Step 13d prompt).
    print(
        f"\nforcing settings.PROMPT_VERSION={TARGET_PROMPT_VERSION!r} "
        f"for the duration of this script (was {settings.PROMPT_VERSION!r})"
    )
    settings.PROMPT_VERSION = TARGET_PROMPT_VERSION

    provider = get_provider()
    model_used = f"{settings.MODEL_PROVIDER}:{_model_name()}"

    # Counters
    started = time.monotonic()
    counters: dict[str, int] = {}
    last_log_at = started

    print()
    print("--- regenerating ---")

    try:
        for i, v1_row in enumerate(rows, start=1):
            outcome, detail = await regenerate_one(v1_row, provider, model_used)
            counters[outcome] = counters.get(outcome, 0) + 1
            if outcome == "ok":
                logger.info(
                    "regen ok v1_pool_id=%s -> %s", v1_row.id, detail
                )
            else:
                logger.warning(
                    "regen %s v1_pool_id=%s :: %s", outcome, v1_row.id, detail
                )

            if i % PROGRESS_EVERY == 0 and time.monotonic() - last_log_at > 0:
                elapsed = time.monotonic() - started
                rate = i / elapsed if elapsed > 0 else 0
                remaining = (len(rows) - i) / rate if rate > 0 else 0
                pct = 100.0 * i / len(rows)
                ok = counters.get("ok", 0)
                print(
                    f"progress={i}/{len(rows)} ({pct:.1f}%) ok={ok} "
                    f"elapsed={fmt_duration(elapsed)} "
                    f"estimated_remaining={fmt_duration(remaining)}"
                )
                last_log_at = time.monotonic()
    except KeyboardInterrupt:
        print("\ninterrupted by user.")

    elapsed = time.monotonic() - started
    print()
    print("--- done ---")
    print(f"elapsed:            {fmt_duration(elapsed)}")
    print(f"calls attempted:    {sum(counters.values())}")
    for outcome in ("ok", "skip-404", "skip-fetch", "skip-model", "skip-invalid", "skip-dup"):
        if outcome in counters:
            print(f"  {outcome:<14} {counters[outcome]}")
    actual_typical = sum(counters.values()) * PER_CALL_USD_TYPICAL
    print(f"cost estimate:      ~${actual_typical:.4f} (based on actual calls)")

    return 0


def main() -> None:
    try:
        rc = asyncio.run(run())
    except KeyboardInterrupt:
        print("\ninterrupted.")
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
