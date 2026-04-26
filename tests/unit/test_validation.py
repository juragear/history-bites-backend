"""is_valid edge cases.

D5/D20: a fact is valid iff it's non-empty after strip() AND no longer than
MAX_FACT_CHARS chars (400 as of Step 13e; was 280 in v1/v2). No regex, no n-gram check — copyright safety relies on V1_PROMPT
and human review. These tests guard against accidental relaxations during
future refactors (e.g. someone trimming inside is_valid and double-trimming
the caller).
"""
from __future__ import annotations

from app.generation import is_valid


def test_is_valid_accepts_normal_fact():
    assert is_valid("On this day in 1899, something interesting happened.") is True


def test_is_valid_rejects_empty_string():
    assert is_valid("") is False


def test_is_valid_rejects_whitespace_only():
    # The model occasionally returns a single newline / spaces when it can't
    # find a fact worth extracting. We must catch that, not pass it through.
    assert is_valid("   ") is False
    assert is_valid("\n") is False
    assert is_valid("\t\n  ") is False


def test_is_valid_accepts_exactly_400_chars():
    fact = "x" * 400
    assert len(fact) == 400
    assert is_valid(fact) is True


def test_is_valid_rejects_401_chars():
    # Boundary regression: < and <= confusion is the kind of bug a reviewer
    # might introduce while "tightening" validation.
    fact = "x" * 401
    assert is_valid(fact) is False


def test_is_valid_accepts_long_fact_with_leading_whitespace():
    # strip() is for emptiness only, not length normalization. A fact that's
    # 400 visible chars + leading whitespace is still 400+ chars total, so it
    # should be rejected. Document the actual behavior.
    fact = "  " + "x" * 399
    assert len(fact) == 401
    assert is_valid(fact) is False


def test_is_valid_accepts_unicode():
    # Wikipedia extracts include accented characters, em dashes, etc. Make
    # sure len() (codepoints) isn't conflated with bytes.
    fact = "Café Müller — a 1978 dance piece by Pina Bausch."
    assert is_valid(fact) is True
