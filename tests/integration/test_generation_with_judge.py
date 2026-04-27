"""Integration tests for the Step 14 judge gate in generate_one_pool_fact.

The conftest mock_provider fixture patches `app.generation._judge` with a
FakeJudge. Each test mutates the fixture state to control the judge's
verdict, then asserts the generated row's status + judge fields.
"""
from __future__ import annotations

import random

import pytest

from app.generation import generate_one_pool_fact
from app.models import PoolFact


def _seed():
    random.seed(0)


async def test_generate_one_pool_fact_auto_approve(
    db, mock_wikipedia, mock_provider
):
    """Judge returns score=4.5 → row inserted with status='approved' and
    judge fields populated."""
    _seed()
    mock_provider["judge_score"] = 4.5
    mock_provider["judge_verdict"] = "auto_approve"
    mock_provider["judge_reason"] = "surprising-angle + clear-stakes"

    row = await generate_one_pool_fact(db)

    assert row.status == "approved"
    assert row.judge_score == 4.5
    assert row.judge_verdict == "auto_approve"
    assert "surprising-angle" in row.judge_reason
    assert mock_provider["judge_calls"] == 1


async def test_generate_one_pool_fact_auto_reject(
    db, mock_wikipedia, mock_provider
):
    """Judge returns score=1.5 → row inserted with status='rejected' and
    judge fields populated. Audit trail preserved; scheduler ignores
    rejected rows so the user never sees them."""
    _seed()
    mock_provider["judge_score"] = 1.5
    mock_provider["judge_verdict"] = "auto_reject"
    mock_provider["judge_reason"] = "tautological so-what"

    row = await generate_one_pool_fact(db)

    assert row.status == "rejected"
    assert row.judge_score == 1.5
    assert row.judge_verdict == "auto_reject"
    assert "tautological" in row.judge_reason


async def test_generate_one_pool_fact_borderline(
    db, mock_wikipedia, mock_provider
):
    """Judge returns score=3.5 → row inserted with status='pending_review'
    (borderline routes to operator review queue)."""
    _seed()
    # Fixture default is borderline (3.5) — explicit here for clarity.
    mock_provider["judge_score"] = 3.5
    mock_provider["judge_verdict"] = "borderline"
    mock_provider["judge_reason"] = "good fact, weak ending"

    row = await generate_one_pool_fact(db)

    assert row.status == "pending_review"
    assert row.judge_score == 3.5
    assert row.judge_verdict == "borderline"


async def test_generate_one_pool_fact_judge_error_routes_to_review(
    db, mock_wikipedia, mock_provider
):
    """Judge raises → row inserted with status='pending_review',
    judge_score=3.0 (the stub), judge_verdict='borderline',
    judge_reason explains the failure. Generation continues; we never
    lose a generated fact to a judge outage."""
    from app.judge import JudgeError

    _seed()
    mock_provider["judge_error"] = JudgeError("provider blew up: simulated")

    row = await generate_one_pool_fact(db)

    assert row.status == "pending_review"
    assert row.judge_verdict == "borderline"
    assert row.judge_score == 3.0  # stub
    assert "judge unavailable" in row.judge_reason
    assert "simulated" in row.judge_reason
