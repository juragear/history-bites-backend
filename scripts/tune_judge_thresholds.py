"""Tune T_HIGH / T_LOW for app.judge against the held-out calibration subset.

One-shot ops tool (Step 14). Read-only against prod. Picks a random subset
of the v1+v3 calibration set, runs the judge on each row, and grids over
candidate threshold pairs to compute precision-at-threshold + coverage.
Prints a recommendation. Re-run any time after the calibration set grows
or the judge prompt changes — produces a fresh recommendation each run
(seeded for reproducibility).

Usage (from repo root, with .venv active):

    .venv/bin/python scripts/tune_judge_thresholds.py
    .venv/bin/python scripts/tune_judge_thresholds.py --holdout-frac 0.3 --seed 42
    .venv/bin/python scripts/tune_judge_thresholds.py --max-rows 30   # cheap dry-run

The DATABASE_URL env var (auto-loaded from .env via pydantic-settings)
controls which DB is targeted. Set to the Railway public-proxy URL when
running against prod.

Out of scope (intentional, ops not production):
  - Tests
  - DB writes (read-only SELECT against pool)
  - Persisting the recommendation anywhere — operator pastes the result
    into app/judge.py manually before pushing
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from itertools import product

from sqlalchemy import select

from app.db import SessionLocal
from app.judge import Judge, JudgeError
from app.models import PoolFact
from app.wikipedia import ArticleExtract


logger = logging.getLogger("tune_judge_thresholds")


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
            "Tune T_HIGH / T_LOW thresholds for app.judge against a "
            "held-out subset of the v1+v3 calibration set."
        ),
    )
    p.add_argument(
        "--holdout-frac",
        type=float,
        default=0.3,
        help=(
            "Fraction of the calibration set held out for evaluation. "
            "Default 0.3 (30%%). The held-out subset is what the judge is "
            "scored against; the remainder is the implicit train set "
            "(used in the few-shot calibration block at app/_judge_calibration.md)."
        ),
    )
    p.add_argument(
        "--seed",
        type=int,
        default=42,
        help="random.seed for the holdout split (deterministic re-runs).",
    )
    p.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help=(
            "Cap on rows to evaluate. Default: no cap (run on the full "
            "holdout). Useful for cheap dry-runs (e.g. --max-rows 10)."
        ),
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Set log level to DEBUG.",
    )
    return p.parse_args()


def load_calibration_rows() -> list[PoolFact]:
    """Pull v1 + v3 rated rows. v4 / v4.1 deliberately excluded — different
    prompt voice; would muddy threshold tuning."""
    stmt = (
        select(PoolFact)
        .where(
            PoolFact.review_rating.is_not(None),
            PoolFact.prompt_version.in_(("v1", "v3")),
        )
        .order_by(PoolFact.id.asc())
    )
    with SessionLocal() as session:
        return list(session.scalars(stmt))


async def evaluate_holdout(
    judge: Judge, rows: list[PoolFact]
) -> list[dict]:
    """Run the judge on each held-out row. Returns one dict per row.

    For threshold tuning we feed the judge a minimal stub extract (title +
    fact only) plus the candidate fact. The judge's job at runtime is to
    evaluate (article, fact); for tuning we degrade to (fact-only) which
    slightly hurts accuracy but is the cheapest way to validate that judge
    scores correlate with operator ratings. The absolute scores aren't what
    matters — the threshold mapping is.
    """
    results: list[dict] = []
    n = len(rows)
    for i, r in enumerate(rows, start=1):
        article = ArticleExtract(
            page_id=int(r.external_id) if r.external_id.isdigit() else 0,
            title=r.source_url.rsplit("/", 1)[-1],
            extract=f"(holdout context — actual article not refetched at tuning time)",
            source_url=r.source_url,
        )
        try:
            jr = await judge.evaluate(article, r.fact_text)
            results.append(
                {
                    "pool_id": r.id,
                    "prompt_version": r.prompt_version,
                    "true_rating": r.review_rating,
                    "true_status": "approved" if r.review_rating >= 4 else "rejected",
                    "judge_score": jr.score,
                    "judge_verdict": jr.verdict,
                    "judge_reason": jr.reason,
                }
            )
        except JudgeError as exc:
            logger.warning("eval_failed pool_id=%s err=%s", r.id, exc)
            results.append(
                {
                    "pool_id": r.id,
                    "prompt_version": r.prompt_version,
                    "true_rating": r.review_rating,
                    "true_status": "approved" if r.review_rating >= 4 else "rejected",
                    "error": str(exc),
                }
            )
        if i % 5 == 0:
            print(f"  ... {i}/{n} evaluated")
    return results


def compute_metrics(
    results: list[dict], t_high: float, t_low: float
) -> dict:
    """For a (T_HIGH, T_LOW) pair, compute auto_approve / auto_reject
    precision and coverage (% of rows that bypass operator review)."""
    scored = [r for r in results if "judge_score" in r]
    n = len(scored)

    auto_approve = [r for r in scored if r["judge_score"] >= t_high]
    auto_reject = [r for r in scored if r["judge_score"] <= t_low]
    borderline = [r for r in scored if t_low < r["judge_score"] < t_high]

    aa_correct = sum(1 for r in auto_approve if r["true_status"] == "approved")
    ar_correct = sum(1 for r in auto_reject if r["true_status"] == "rejected")

    return {
        "t_high": t_high,
        "t_low": t_low,
        "auto_approve_count": len(auto_approve),
        "auto_approve_precision": (
            aa_correct / len(auto_approve) if auto_approve else None
        ),
        "auto_reject_count": len(auto_reject),
        "auto_reject_precision": (
            ar_correct / len(auto_reject) if auto_reject else None
        ),
        "borderline_count": len(borderline),
        "coverage": (n - len(borderline)) / n if n else 0.0,
    }


async def run() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    print("--- Threshold tuning for app.judge ---")
    rows = load_calibration_rows()
    print(f"calibration set size (v1+v3 rated): {len(rows)}")

    random.seed(args.seed)
    random.shuffle(rows)
    split = int(len(rows) * (1 - args.holdout_frac))
    holdout = rows[split:]
    if args.max_rows is not None:
        holdout = holdout[: args.max_rows]
    print(
        f"holdout: {len(holdout)} rows "
        f"(seed={args.seed}, holdout_frac={args.holdout_frac})"
    )

    holdout_approved = sum(1 for r in holdout if r.review_rating >= 4)
    holdout_rejected = sum(1 for r in holdout if r.review_rating <= 3)
    print(
        f"holdout class balance: "
        f"approved={holdout_approved} ({100 * holdout_approved / len(holdout):.0f}%), "
        f"rejected={holdout_rejected} ({100 * holdout_rejected / len(holdout):.0f}%)"
    )
    print()

    judge = Judge()
    print("evaluating judge on holdout (one Gemini call per row)...")
    results = await evaluate_holdout(judge, holdout)
    print()

    n_errors = sum(1 for r in results if "error" in r)
    if n_errors:
        print(f"WARN: {n_errors}/{len(results)} judge evaluations errored")

    # Grid over reasonable threshold pairs.
    high_grid = [3.8, 4.0, 4.2, 4.4, 4.6]
    low_grid = [2.0, 2.3, 2.5, 2.8, 3.0]
    metrics = [compute_metrics(results, h, l) for h, l in product(high_grid, low_grid)]

    print("=== Threshold grid (sorted by coverage among rows where both precisions >=0.90) ===")
    print(
        f"{'T_HIGH':>8} {'T_LOW':>7} {'AA_n':>6} {'AA_prec':>9} "
        f"{'AR_n':>6} {'AR_prec':>9} {'border':>8} {'coverage':>10}"
    )
    for m in sorted(metrics, key=lambda x: -x["coverage"]):
        aa_p = (
            f"{m['auto_approve_precision']:.2f}"
            if m["auto_approve_precision"] is not None
            else "—"
        )
        ar_p = (
            f"{m['auto_reject_precision']:.2f}"
            if m["auto_reject_precision"] is not None
            else "—"
        )
        print(
            f"{m['t_high']:>8.1f} {m['t_low']:>7.1f} "
            f"{m['auto_approve_count']:>6} {aa_p:>9} "
            f"{m['auto_reject_count']:>6} {ar_p:>9} "
            f"{m['borderline_count']:>8} {m['coverage']:>10.1%}"
        )

    # Recommendation: highest coverage where both precisions >=0.90.
    safe = [
        m
        for m in metrics
        if (m["auto_approve_precision"] or 0) >= 0.90
        and (m["auto_reject_precision"] or 0) >= 0.90
    ]
    print()
    if safe:
        best = max(safe, key=lambda x: x["coverage"])
        print(f"=== Recommendation ===")
        print(f"  T_HIGH = {best['t_high']}")
        print(f"  T_LOW  = {best['t_low']}")
        print(
            f"  auto_approve precision: {best['auto_approve_precision']:.2f} "
            f"({best['auto_approve_count']} rows)"
        )
        print(
            f"  auto_reject precision:  {best['auto_reject_precision']:.2f} "
            f"({best['auto_reject_count']} rows)"
        )
        print(f"  borderline (route to operator): {best['borderline_count']} rows")
        print(f"  coverage (auto-decided): {best['coverage']:.1%}")
        print()
        print("Paste these values into app/judge.py: T_HIGH and T_LOW.")
    else:
        print("=== NO SAFE THRESHOLD PAIR FOUND ===")
        print(
            "No (T_HIGH, T_LOW) pair achieved >=0.90 precision on both "
            "buckets. Options:"
        )
        print("  1. Ship the judge in shadow mode (set T_HIGH=99.0, T_LOW=0.0")
        print("     in app/judge.py — every score becomes 'borderline' so")
        print("     no auto-decision happens, but judge predictions are still")
        print("     persisted for future analysis).")
        print("  2. Iterate on the calibration block or the judge prompt and re-run.")
        print("  3. Accept lower precision (re-run with manually-relaxed grid).")

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
