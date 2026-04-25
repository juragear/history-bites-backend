"""Unit tests for app.review_tags.validate_tags.

Pure function — no DB, no fixtures, no monkeypatch. Just normalization +
dedupe + palette validation. Step 13a.
"""
from __future__ import annotations

import pytest

from app.review_tags import (
    ALL_TAGS,
    APPROVE_TAGS,
    REJECT_TAGS,
    InvalidTagError,
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
