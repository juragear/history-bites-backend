"""Unit tests for app.judge — Step 14 LLM-as-judge.

Pure-function-ish tests with a mocked provider. Cover:
  - JudgeResult dataclass score-range invariant.
  - Verdict mapping at the T_HIGH / T_LOW boundaries.
  - Happy parse path.
  - JSON parse failure → JudgeError.
  - Score out of [1.0, 5.0] range → JudgeError.
"""
from __future__ import annotations

import json

import pytest

from app.judge import (
    JudgeError,
    JudgeResult,
    Judge,
    T_HIGH,
    T_LOW,
    _verdict_for_score,
)


# --- JudgeResult invariants ---------------------------------------------


def test_judge_result_score_in_range_accepts_endpoints():
    """1.0 and 5.0 are inclusive endpoints."""
    assert JudgeResult(score=1.0, verdict="auto_reject", reason="x").score == 1.0
    assert JudgeResult(score=5.0, verdict="auto_approve", reason="y").score == 5.0


@pytest.mark.parametrize("bad_score", [0.99, 5.01, -1.0, 100.0])
def test_judge_result_rejects_score_out_of_range(bad_score):
    """Construction guards the contract — defense in depth on top of the
    DB CHECK constraint and the parse-time validation in Judge.evaluate."""
    with pytest.raises(ValueError):
        JudgeResult(score=bad_score, verdict="borderline", reason="x")


# --- Verdict mapping ----------------------------------------------------


def test_verdict_mapping_at_thresholds():
    """Boundary semantics: >= T_HIGH is auto_approve; <= T_LOW is auto_reject;
    anything strictly between is borderline. Re-running tune_judge_thresholds
    can shift the constants but not these inequalities."""
    assert _verdict_for_score(T_HIGH) == "auto_approve"
    assert _verdict_for_score(T_HIGH + 0.1) == "auto_approve"
    assert _verdict_for_score(T_LOW) == "auto_reject"
    assert _verdict_for_score(T_LOW - 0.1) == "auto_reject"
    midpoint = (T_HIGH + T_LOW) / 2
    assert _verdict_for_score(midpoint) == "borderline"


# --- Happy + failure parse paths via mocked provider -------------------


class _MockProvider:
    """Minimal ModelProvider stub that returns a canned generate_text payload."""

    def __init__(self, payload: str | Exception):
        self._payload = payload

    async def extract_fact(self, article_extract, *, version=None):  # noqa: D401
        raise NotImplementedError("test stub — Judge only calls generate_text")

    async def generate_text(self, prompt: str) -> str:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _fake_extract():
    """Build a tiny ArticleExtract — Judge only reads .extract."""
    from app.wikipedia import ArticleExtract
    return ArticleExtract(
        page_id=999,
        title="Test",
        extract="Some article context.",
        source_url="https://example.com/test",
    )


async def test_judge_evaluate_happy_path_parses_score_and_reason():
    """Happy parse path. The verdict depends on the live T_HIGH/T_LOW
    constants (which can change as thresholds retune) so we don't assert
    the verdict directly here — that's covered by
    test_verdict_mapping_at_thresholds which tests the function under
    whatever thresholds are currently set."""
    payload = json.dumps({"score": 4.3, "reason": "surprising-angle, clear stakes"})
    judge = Judge(provider=_MockProvider(payload))
    result = await judge.evaluate(_fake_extract(), "Some candidate fact.")
    assert result.score == 4.3
    assert result.verdict == _verdict_for_score(4.3)  # threshold-relative
    assert "surprising-angle" in result.reason


async def test_judge_evaluate_invalid_json_raises_judge_error():
    judge = Judge(provider=_MockProvider("not valid json {{{"))
    with pytest.raises(JudgeError):
        await judge.evaluate(_fake_extract(), "fact")


async def test_judge_evaluate_score_out_of_range_raises_judge_error():
    payload = json.dumps({"score": 7.0, "reason": "overshoot"})
    judge = Judge(provider=_MockProvider(payload))
    with pytest.raises(JudgeError):
        await judge.evaluate(_fake_extract(), "fact")


async def test_judge_evaluate_provider_error_wraps_as_judge_error():
    """Any provider-side exception becomes a JudgeError so the caller has
    a single exception type to catch."""
    judge = Judge(provider=_MockProvider(RuntimeError("provider blew up")))
    with pytest.raises(JudgeError):
        await judge.evaluate(_fake_extract(), "fact")


async def test_judge_evaluate_truncates_long_reason():
    """Reason field is truncated to 300 chars to keep the audit string DB-friendly."""
    long_reason = "x" * 500
    payload = json.dumps({"score": 2.0, "reason": long_reason})
    judge = Judge(provider=_MockProvider(payload))
    result = await judge.evaluate(_fake_extract(), "fact")
    assert len(result.reason) == 300
    assert result.score == 2.0
    # Verdict is threshold-relative — assert it tracks _verdict_for_score
    # rather than hard-coding a specific verdict that would break under
    # threshold retuning (e.g. when shadow mode is active).
    assert result.verdict == _verdict_for_score(2.0)
