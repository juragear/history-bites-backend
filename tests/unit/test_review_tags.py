"""Unit tests for app.review_tags.

Pure functions — no DB, no fixtures, no monkeypatch.

Covers:
  - validate_tags: normalization, dedupe, palette validation (Step 13a).
  - derive_status_from_rating: 1-5 -> 'approved'/'rejected' threshold, type
    + range guards (Step 13c / D26).
"""
from __future__ import annotations

import pytest

from app.review_tags import (
    ALL_TAGS,
    APPROVE_TAGS,
    REJECT_TAGS,
    InvalidRatingError,
    InvalidTagError,
    derive_status_from_rating,
    validate_tags,
)


def test_validate_none_returns_empty_list():
    assert validate_tags(None) == []


def test_validate_empty_list_returns_empty_list():
    assert validate_tags([]) == []


def test_validate_single_known_tag():
    assert validate_tags(["surprising-angle"]) == ["surprising-angle"]


def test_validate_normalizes_case_and_whitespace():
    """Uppercase + surrounding whitespace are normalized to canonical kebab form."""
    out = validate_tags(["SURPRISING-ANGLE", "  human-scale  "])
    assert out == ["surprising-angle", "human-scale"]


def test_validate_dedupes_preserving_first_seen_order():
    out = validate_tags(["surprising-angle", "surprising-angle", "human-scale"])
    assert out == ["surprising-angle", "human-scale"]


def test_validate_unknown_tag_raises():
    with pytest.raises(InvalidTagError) as exc_info:
        validate_tags(["nonsense-tag"])
    # Error message should include the bad tag and the allowed set so the
    # caller can show something useful.
    assert "nonsense-tag" in str(exc_info.value)


def test_validate_skips_empty_strings_among_valid_tags():
    assert validate_tags(["surprising-angle", "", "  "]) == ["surprising-angle"]


def test_palette_partition_is_disjoint():
    """Sanity check: APPROVE_TAGS and REJECT_TAGS don't overlap, and ALL_TAGS
    is their union. Cheap defense against a future palette edit accidentally
    listing the same tag twice."""
    assert APPROVE_TAGS.isdisjoint(REJECT_TAGS)
    assert ALL_TAGS == APPROVE_TAGS | REJECT_TAGS


# --- derive_status_from_rating (Step 13c / D26) ----------------------------


@pytest.mark.parametrize(
    "rating,expected",
    [
        (1, "rejected"),
        (2, "rejected"),
        (3, "rejected"),  # borderline -> rejected (D26 threshold = 4)
        (4, "approved"),
        (5, "approved"),
    ],
)
def test_derive_status_threshold(rating, expected):
    """The whole point of D26: threshold is 4, not 3. Borderline rates as
    rejected because 'I'm not sure' is the safer default for a daily-fact
    app where a published miss costs more than an unpublished hit."""
    assert derive_status_from_rating(rating) == expected


@pytest.mark.parametrize("rating", [0, 6, -1, 100])
def test_derive_status_out_of_range_raises(rating):
    with pytest.raises(InvalidRatingError):
        derive_status_from_rating(rating)


@pytest.mark.parametrize("rating", ["4", 4.0, None])
def test_derive_status_non_int_raises(rating):
    """Strings and floats fail loudly. The endpoint coerces digit-strings to
    int before calling the helper; the helper itself stays strict."""
    with pytest.raises(InvalidRatingError):
        derive_status_from_rating(rating)  # type: ignore[arg-type]


@pytest.mark.parametrize("rating", [True, False])
def test_derive_status_bool_raises(rating):
    """`True == 1` and `False == 0` in Python, and `isinstance(True, int)` is
    True. Without the explicit `isinstance(x, bool)` guard, derive(True)
    would silently return 'rejected' (because True < 4). Lock that down."""
    with pytest.raises(InvalidRatingError):
        derive_status_from_rating(rating)  # type: ignore[arg-type]
