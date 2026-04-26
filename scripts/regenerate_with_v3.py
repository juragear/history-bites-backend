"""Targeted A/B regeneration: v3 prompt + Gemini 3 Flash + section-aware
source against the v1 boring-even-if-true cohort.

Step 13e ops tool. Mirrors the structure of `scripts/regenerate_with_v2.py`
(now historical) but with three meaningful differences:

  1. Forces `version="v3"` via the new explicit kwarg path on
     `provider.extract_fact(extract.extract, version="v3")` — no more
     runtime mutation of `settings.PROMPT_VERSION` mid-script. The kwarg
     was added in Step 13e specifically to make this kind of override
     cleanly attributable at the call site.
  2. Honours the new pre-filter (extract length floor + infobox-shape
     detector). Per-row outcome counter records `skip-thin` and
     `skip-infoboxy` so we can see how many of the v1 boring articles were
     genuinely too thin to ever produce good content — that's a
     category-curation signal for later.
  3. Will run on Gemini 3 Flash Preview (the model upgrade landed in the
     same step). Each call is ~3x more expensive than 2.5 Flash but still
     well under $0.10 for the full ~65-row cohort.

What it deliberately does NOT do:
  - Call get_used_external_ids / dedup. Duplicate external_id is the point;
    the widened uniqueness constraint (D27, migration c7b39e4f1a82) makes
    that legal.
  - Touch v1 rows. The A/B comparison depends on both versions surviving.
  - Mutate Railway env vars. PROMPT_VERSION on Railway stays v1 throughout
    Step 13e.

Usage (from repo root, with .venv active):

    # Plan only — no generation, no DB writes.
    .venv/bin/python scripts/regenerate_with_v3.py --dry-run

    # Spot-check 3 rows end-to-end before committing to the full run.
    .venv/bin/python scripts/regenerate_with_v3.py --limit 3 --confirm

    # Full A/B regeneration (~65 rows, ~$0.10 in Gemini 3 spend).
    .venv/bin/python scripts/regenerate_with_v3.py --confirm

The DATABASE_URL env var (from .env, auto-loaded by pydantic-settings)
controls which DB is targeted. Set to the Railway public-proxy URL when
running against prod from a local shell.
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
from app.generation import (
    MIN_EXTRACT_CHARS,
    _looks_infoboxy,
    is_valid,
)
from app.model_provider import ModelProviderError, get_provider
from app.models import PoolFact


# Per-call estimate for Gemini 3 Flash Preview. Pricing was not finalised
# at preview-launch time; we estimate ~3x the 2.5 Flash baseline ($0.0005)
# = $0.0015. Conservative — adjust if Google publishes preview pricing
# below this. Worst-case multiplier doesn't apply: this script is
# single-attempt-per-row (log + skip on any failure mode).
PER_CALL_USD_TYPICAL = 0.0015

PROGRESS_EVERY = 10

TARGET_TAG = "boring-even-if-true"
SOURCE_PROMPT_VERSION = "v1"
TARGET_PROMPT_VERSION = "v3"

logger = logging.getLogger("regenerate_v3")


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
            "Regenerate the v1 boring-even-if-true cohort with the v3 prompt "
            "and Gemini 3 Flash. Adds new pool rows with prompt_version='v3', "
            "same external_id."
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
    NULL), then Python filters by tag membership for SQLite/Postgres
    portability. Working set is small (~150 rows), so the in-memory pass is
    fine.
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
        r for r in all_v1_tagged if r.review_tags and TARGET_TAG in r.review_tags
    ]
    if limit is not None:
        matching = matching[:limit]
    return matching


def title_from_source_url(source_url: str) -> str:
    """Pull the Wikipedia article title from a stored source_url.

    source_urls in pool look like:
      https://en.wikipedia.org/wiki/Some_Article_Title
    Title comes after `/wiki/`; URL-decode percent escapes; underscores
    stay (the action API extracts endpoint accepts them).
    """
    marker = "/wiki/"
    idx = source_url.find(marker)
    if idx < 0:
        raise ValueError(
            f"source_url does not look like a Wikipedia article URL: {source_url!r}"
        )
    raw = source_url[idx + len(marker):]
    return unquote(raw)


def print_plan(rows: list[PoolFact], limit: int | None) -> None:
    n = len(rows)
    typical = n * PER_CALL_USD_TYPICAL
    print("--- v3 regeneration plan ---")
    print(f"target tag:         {TARGET_TAG!r}")
    print(f"source prompt:      {SOURCE_PROMPT_VERSION}")
    print(f"target prompt:      {TARGET_PROMPT_VERSION}")
    print(f"model:              gemini:{settings.GEMINI_MODEL}")
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
    """Regenerate one row with V3.

    Returns (outcome, detail) where outcome is one of:
      "ok"           -> v3 row inserted
      "skip-404"     -> Wikipedia 4xx OR action API "missing"
      "skip-fetch"   -> non-4xx fetch error
      "skip-thin"    -> extract under MIN_EXTRACT_CHARS post-truncation
      "skip-infoboxy"-> _looks_infoboxy returned True
      "skip-model"   -> provider error
      "skip-invalid" -> validation failed (empty / >MAX_FACT_CHARS)
      "skip-dup"     -> integrity error (a v3 row already exists for this id)
    detail carries a short error string for logging on failure.
    """
    title = title_from_source_url(v1_row.source_url)
    try:
        extract = await wikipedia.fetch_extract(title)
    except wikipedia.WikipediaNotFound:
        return "skip-404", f"action API missing for {title!r}"
    except httpx.HTTPStatusError as exc:
        if 400 <= exc.response.status_code < 500:
            return "skip-404", f"wikipedia {exc.response.status_code} for {title!r}"
        return "skip-fetch", f"wikipedia {exc.response.status_code} for {title!r}"
    except Exception as exc:
        return "skip-fetch", f"wikipedia error for {title!r}: {exc!r}"

    if len(extract.extract) < MIN_EXTRACT_CHARS:
        return "skip-thin", f"extract_chars={len(extract.extract)}"
    if _looks_infoboxy(extract.extract):
        return "skip-infoboxy", None

    try:
        # Step 13e: explicit version override via kwarg. No mutation of
        # settings.PROMPT_VERSION; the regen run is fully attributable.
        fact_text = await provider.extract_fact(
            extract.extract, version=TARGET_PROMPT_VERSION
        )
    except ModelProviderError as exc:
        return "skip-model", str(exc)

    if not is_valid(fact_text):
        return "skip-invalid", f"len={len(fact_text)}"

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

    provider = get_provider()
    model_used = f"{settings.MODEL_PROVIDER}:{_model_name()}"

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
                logger.info("regen ok v1_pool_id=%s -> %s", v1_row.id, detail)
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
    for outcome in (
        "ok",
        "skip-404",
        "skip-fetch",
        "skip-thin",
        "skip-infoboxy",
        "skip-model",
        "skip-invalid",
        "skip-dup",
    ):
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
