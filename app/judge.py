"""LLM-as-judge for v3+ generation gating (D23, Step 14).

Wraps a `ModelProvider` with a few-shot calibration prompt that predicts
Will's 1-5 ordinal rating per D26 for a candidate fact, then maps the
predicted score to a verdict bucket via two thresholds:

  - score >= T_HIGH  -> auto_approve  (skip operator review, ship to pool as approved)
  - score <= T_LOW   -> auto_reject   (skip operator review, persist as rejected for audit)
  - T_LOW < score < T_HIGH -> borderline (route to operator review queue, status=pending_review)

Per D23 the goal is "most volume autopilot, human in the loop on edge cases".
The thresholds are tuned by `scripts/tune_judge_thresholds.py` against the
held-out subset of the v1+v3 calibration set. Re-run that script after a
meaningful chunk of new operator ratings accumulate (see Step 14.5 plan).

Calibration prompt:
  - Loaded from `app/_judge_calibration.md` at module import (one-time disk
    read; no DB hits, no provider calls). The .md file embeds 12 hand-picked
    examples balanced 6 v1 + 6 v3, balanced 6 approve cluster (rating 4-5)
    + 6 reject cluster (rating 1-3). Re-curate by editing the .md file —
    don't query the DB live, the calibration set is supposed to be stable.
  - The prompt template wraps that block with a short instruction ("Will's
    bar is interestingness, not factual correctness — Wikipedia gives us
    correctness for free") and asks the model for `{"score": float,
    "reason": str}` JSON.

Caller contract:
  - `Judge()` is cheap to construct (just reads the .md file once, then
    nothing). The actual model call happens in `evaluate()`. Generation
    pipeline holds a module-level lazy singleton in app/generation.py so we
    don't rebuild the prompt on every cron tick.
  - `evaluate(article_extract, fact_text)` returns a `JudgeResult` or raises
    `JudgeError`. The pipeline catches `JudgeError` and routes the row to
    operator review with a stub reason — never lose a generation to a judge
    outage.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from app.model_provider import ModelProvider, get_provider
from app.wikipedia import ArticleExtract


logger = logging.getLogger(__name__)


JudgeVerdict = Literal["auto_approve", "auto_reject", "borderline"]


@dataclass(frozen=True)
class JudgeResult:
    """Single judge evaluation. Score is the predicted rating (1.0-5.0,
    one-decimal-place precision allowed); verdict maps via T_HIGH / T_LOW;
    reason is a short string the judge writes for audit + future analysis."""

    score: float
    verdict: JudgeVerdict
    reason: str

    def __post_init__(self) -> None:
        if not (1.0 <= self.score <= 5.0):
            raise ValueError(
                f"JudgeResult.score must be in [1.0, 5.0], got {self.score}"
            )


# Thresholds (Step 14).
#
# **SHADOW MODE** — chosen on first run because the threshold tuning script
# couldn't find a (T_HIGH, T_LOW) pair achieving >=0.90 precision on both
# auto_approve and auto_reject buckets against the 59-row v1+v3 holdout
# (full table in Session 14 log). The judge is asymmetrically useful:
# auto_reject at T_LOW=3.0 hit 81% precision on 42 rows (decent, below the
# 0.90 bar); auto_approve maxed out at 57% precision regardless of T_HIGH.
# The model is much better at recognising bad facts than good ones.
#
# In shadow mode, judge_score + judge_verdict + judge_reason still get
# populated on every generated row (so the data accumulates for future
# tuning), but the verdict mapping below falls through to "borderline" for
# every realistic score: T_HIGH=99.0 means no score in [1.0, 5.0] can
# trigger auto_approve, and T_LOW=0.0 means no score can trigger
# auto_reject. Status = 'pending_review' for everything → operator queue
# receives 100% of generated facts, same as pre-Step-14 behaviour, but with
# judge predictions tagged on each row for the next retuning round.
#
# Step 14.5 plan: after ~200 production decisions accumulate, re-run
# `scripts/tune_judge_thresholds.py` with the larger dataset. If a safe
# pair emerges, paste those values here and ship live thresholds.
T_HIGH: float = 99.0  # shadow mode — no auto_approve
T_LOW: float = 0.0    # shadow mode — no auto_reject


class JudgeError(Exception):
    """Raised when the judge can't produce a usable verdict (provider error,
    JSON parse failure, score out of range, etc.). Caller routes to operator
    review."""


def _verdict_for_score(score: float) -> JudgeVerdict:
    if score >= T_HIGH:
        return "auto_approve"
    if score <= T_LOW:
        return "auto_reject"
    return "borderline"


# Path to the markdown calibration block. Loaded once at module import. The
# file is co-located with this module so it travels with the package; no
# runtime DB query, no env-var lookup, no network.
_CALIBRATION_PATH = Path(__file__).parent / "_judge_calibration.md"


def _load_calibration_block() -> str:
    """Read the embedded calibration examples. Raises at import if the file
    is missing or empty — better to fail boot than silently judge with no
    examples."""
    if not _CALIBRATION_PATH.exists():
        raise RuntimeError(
            f"judge calibration block missing at {_CALIBRATION_PATH}. "
            "This file ships with the package and should not be deleted."
        )
    text = _CALIBRATION_PATH.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(
            f"judge calibration block at {_CALIBRATION_PATH} is empty. "
            "Re-generate via the Step 14 build script."
        )
    return text


_CALIBRATION_BLOCK = _load_calibration_block()


# Prompt template. The `{calibration}`, `{extract}`, and `{fact}` placeholders
# are filled by `Judge.evaluate`. Curly braces inside JSON examples need to
# be doubled to escape `.format()` — there are no such braces in this
# template, so no escaping is needed.
_JUDGE_PROMPT_TEMPLATE = """You are a quality reviewer for a daily history app. Will, the app's curator, rates each candidate fact on a 1-5 scale where:

- 5 = Exemplary. The kind of fact he'd proudly send to a friend.
- 4 = Good. Worth shipping with minor concerns.
- 3 = Borderline. Real concerns about angle, completeness, or focus.
- 2 = Mediocre. Issues outweigh strengths.
- 1 = Bad. Tautological, factually wrong, or aggressively boring.

Will rejects anything below 4. The bar is interestingness, not factual correctness — Wikipedia gives us correctness for free.

Below are 12 calibration examples showing how Will rated facts derived from specific Wikipedia article extracts. Study them to understand his taste. Then score the candidate fact at the end.

---

# CALIBRATION EXAMPLES

{calibration}

---

# YOUR TASK

Article extract:
{extract}

Candidate fact:
{fact}

Return JSON with two fields:
- "score": a float between 1.0 and 5.0 (you may use one decimal place, e.g. 4.3, to express confidence within a band)
- "reason": one short sentence (under 200 chars) explaining the score, calling out which of Will's known approve/reject patterns the fact matches

JSON only, no preamble.
"""


# Cap the article extract slice we send to the judge. The Step-13e
# section-aware truncator already caps at 15k chars; this further cap to 5k
# keeps the judge prompt under ~25k chars total. Plenty of context for
# evaluating one fact, well under any model's context window.
_EXTRACT_CHAR_CAP = 5000


class Judge:
    """Stateless wrapper around a ModelProvider with a calibration-tuned
    prompt. One Judge per process; reused across calls.

    Construction is cheap (no I/O — calibration block is loaded at module
    import). Each `evaluate()` call is one provider request + JSON parse +
    threshold mapping.
    """

    def __init__(self, provider: Optional[ModelProvider] = None) -> None:
        self._provider = provider if provider is not None else get_provider()

    async def evaluate(
        self, article_extract: ArticleExtract, fact_text: str
    ) -> JudgeResult:
        """Score one (article, fact) pair.

        `article_extract` is the section-aware-truncated extract from
        `wikipedia.fetch_extract` (already <=15k chars; we further cap the
        slice we send to the judge so prompts stay tight).
        """
        extract_slice = article_extract.extract[:_EXTRACT_CHAR_CAP]
        prompt = _JUDGE_PROMPT_TEMPLATE.format(
            calibration=_CALIBRATION_BLOCK,
            extract=extract_slice,
            fact=fact_text,
        )

        try:
            raw = await self._provider.generate_text(prompt)
        except Exception as exc:
            # Wrap any provider-side failure as JudgeError so the caller has
            # a single exception type to catch. The original exception
            # message is preserved in the chain.
            raise JudgeError(
                f"provider failure during judge evaluation: {exc!r}"
            ) from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "judge_parse_failed",
                extra={"extra": {"raw_preview": raw[:200], "error": str(exc)}},
            )
            raise JudgeError(
                f"judge response was not valid JSON: {exc}"
            ) from exc

        if not isinstance(parsed, dict):
            raise JudgeError(
                f"judge response JSON was not an object: {type(parsed).__name__}"
            )

        score_raw = parsed.get("score")
        if score_raw is None:
            raise JudgeError(f"judge response missing 'score' field: {parsed!r}")
        try:
            score = float(score_raw)
        except (TypeError, ValueError) as exc:
            raise JudgeError(
                f"judge 'score' field not coercible to float: {score_raw!r}"
            ) from exc

        if not (1.0 <= score <= 5.0):
            raise JudgeError(f"judge score out of range [1.0, 5.0]: {score}")

        # Reason is optional but strongly preferred. Truncate to 300 to keep
        # the audit string DB-friendly without over-eager schema policing.
        reason_raw = parsed.get("reason", "")
        reason = str(reason_raw)[:300] if reason_raw is not None else ""

        verdict = _verdict_for_score(score)
        return JudgeResult(score=score, verdict=verdict, reason=reason)
