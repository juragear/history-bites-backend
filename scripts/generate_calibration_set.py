"""Top up the pool to N total rows by repeatedly calling
generate_one_pool_fact. One-shot ops tool used during the calibration rating
sprint (Step 13b) and any future top-ups.

Resumable by design: re-runs read the current pool count and only fill the
gap. Existing rated rows (approved, rejected, pending_review from earlier
sessions) are preserved — this is "top up to target", not "regenerate".

Usage (from repo root, with .venv active):

    .venv/bin/python scripts/generate_calibration_set.py --count 150 --confirm

    # Cost preview only:
    .venv/bin/python scripts/generate_calibration_set.py --count 150 --dry-run

The DATABASE_URL env var controls which DB is targeted. Set it to the Railway
public-proxy URL when running against prod from a local shell (Sessions 5/6/8
pattern).

Out of scope (intentionally — this is ops tooling, not production code):
  - tests
  - retry orchestration on top of generate_one_pool_fact's existing 3-attempt
    budget; if a single call gives up, log and move on
  - Slack alerting; the script is run interactively, the operator sees stdout
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import timedelta

from sqlalchemy import func, select

from app.db import SessionLocal
from app.generation import GenerationFailed, generate_one_pool_fact
from app.models import PoolFact


# Per-call estimate, rounded up. Gemini 2.5 Flash pricing (~$0.30/M input,
# ~$2.50/M output) at ~800 input + ~100 output tokens lands at ~$0.00049
# worst case per call. We round to $0.0005 so a 150-fact run estimates at
# ~$0.075 typical (one attempt per success) and ~$0.225 worst-case (three
# attempts per success). Adjust if Gemini pricing changes.
PER_CALL_USD_TYPICAL = 0.0005
WORST_CASE_ATTEMPT_MULTIPLIER = 3  # MAX_CANDIDATE_ATTEMPTS in generation.py

PROGRESS_EVERY = 10


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        stream=sys.stdout,
    )
    # The generation module logs structured "extra" dicts for prod JSON. Here
    # we just want a readable trace, so no JSON formatter wiring.


logger = logging.getLogger("calibration")


def status_breakdown(session) -> dict[str, int]:
    rows = session.execute(
        select(PoolFact.status, func.count()).group_by(PoolFact.status)
    ).all()
    return {status: count for status, count in rows}


def total_count(session) -> int:
    return session.execute(select(func.count(PoolFact.id))).scalar_one()


def fmt_duration(seconds: float) -> str:
    td = timedelta(seconds=int(seconds))
    minutes, secs = divmod(int(td.total_seconds()), 60)
    return f"{minutes}m{secs:02d}s"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Top up the pool to N total rows.",
    )
    p.add_argument(
        "--count",
        type=int,
        default=150,
        help="Target total pool size (default: 150). The script generates "
        "max(0, count - current_pool_size) facts.",
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Skip the y/N prompt. Required when running non-interactively.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and the cost estimate, then exit without "
        "generating anything.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Set log level to DEBUG.",
    )
    return p.parse_args()


def print_plan(target: int, current: int, gap: int) -> None:
    typical = gap * PER_CALL_USD_TYPICAL
    worst = gap * WORST_CASE_ATTEMPT_MULTIPLIER * PER_CALL_USD_TYPICAL
    print(f"target:         {target}")
    print(f"current pool:   {current}")
    print(f"to generate:    {gap}")
    print(
        f"cost estimate:  ~${typical:.2f} typical, "
        f"~${worst:.2f} worst-case (3 attempts/fact)"
    )


async def run() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    # Plan
    with SessionLocal() as session:
        current = total_count(session)
        breakdown = status_breakdown(session)
    target = args.count
    gap = max(0, target - current)

    print("--- pool calibration top-up ---")
    print(f"breakdown:      {breakdown or '{}'}")
    print_plan(target, current, gap)

    if gap == 0:
        print(
            f"pool already at or above target "
            f"(current={current}, target={target}), nothing to do"
        )
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

    # Generate
    print()
    started = time.monotonic()
    successes = 0
    failures = 0
    calls_made = 0
    last_log_at = started

    while successes < gap:
        # One session per call mirrors the prod cron pattern (tight tx
        # scope, no long-lived transaction holding row locks).
        with SessionLocal() as session:
            calls_made += 1
            try:
                row = await generate_one_pool_fact(session)
                successes += 1
                logger.info(
                    "generate ok pool_id=%s category=%s region=%s era=%s",
                    row.id,
                    row.category,
                    row.region,
                    row.era,
                )
            except GenerationFailed as exc:
                failures += 1
                logger.warning("GenerationFailed: %s", exc)
                # Don't break — exhausted categories are individual misses.
                continue
            except KeyboardInterrupt:
                print("\ninterrupted by user.")
                break
            except Exception as exc:
                logger.exception("aborting: unexpected error: %s", exc)
                return 2

        if successes and successes % PROGRESS_EVERY == 0 and time.monotonic() - last_log_at > 0:
            elapsed = time.monotonic() - started
            rate = successes / elapsed if elapsed > 0 else 0
            remaining = (gap - successes) / rate if rate > 0 else 0
            pct = 100.0 * successes / gap if gap else 0
            print(
                f"progress={successes}/{gap} ({pct:.1f}%) "
                f"elapsed={fmt_duration(elapsed)} "
                f"estimated_remaining={fmt_duration(remaining)} "
                f"failures={failures}"
            )
            last_log_at = time.monotonic()

    elapsed = time.monotonic() - started
    print()
    print("--- done ---")
    print(f"successes:      {successes}")
    print(f"GenerationFailed: {failures}")
    print(f"total calls:    {calls_made}")
    print(f"elapsed:        {fmt_duration(elapsed)}")

    with SessionLocal() as session:
        final_breakdown = status_breakdown(session)
        final_total = total_count(session)
    print(f"final pool:     total={final_total} {final_breakdown}")
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
