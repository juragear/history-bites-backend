"""Unit tests for app.model_provider — prompt version selector.

Step 13e: V2 was removed from the registry (the v2 batch was contaminated;
see the comment in app/model_provider.py for details). Step 13f: V4 added
to address the v3 bottom-5 failure modes. The selector now resolves
v1 + v3 + v4 and explicitly rejects v2 — pointing PROMPT_VERSION=v2 at the
current model+source+filter would silently produce v3-quality output under
a "v2" label, which would corrupt downstream comparisons.

Why these tests matter: without a loud ValueError on unknown / dropped
versions, a stale Railway env var would fall through to whatever default
the function happened to choose. Loud failure at the first generation call
is the correct behaviour.
"""
from __future__ import annotations

import pytest

from app.model_provider import (
    V1_PROMPT,
    V3_PROMPT,
    V4_PROMPT,
    V4_1_PROMPT,
    get_active_prompt,
)


def test_get_active_prompt_returns_v1_for_v1():
    assert get_active_prompt("v1") is V1_PROMPT


def test_get_active_prompt_returns_v3_for_v3():
    assert get_active_prompt("v3") is V3_PROMPT


def test_get_active_prompt_returns_v4_for_v4():
    """Step 13f: V4_PROMPT adds two new rules to V3 (close cleanly,
    stay within source) targeting the v3 bottom-5 failure modes.
    """
    assert get_active_prompt("v4") is V4_PROMPT


def test_get_active_prompt_returns_v4_1_for_v4_1():
    """Step 13f addition: V4_1 is a tonal variant of V4 (warm + lightly
    playful voice for morning delivery). Registry key uses the dotted
    form 'v4.1' to make the relationship to v4 explicit.
    """
    assert get_active_prompt("v4.1") is V4_1_PROMPT


def test_get_active_prompt_rejects_dropped_v2():
    """v2 is deliberately absent from the registry. Pointing
    PROMPT_VERSION=v2 at the new model+source+filter would produce
    v3-quality output under a misleading label.
    """
    with pytest.raises(ValueError) as exc_info:
        get_active_prompt("v2")
    msg = str(exc_info.value)
    assert "v2" in msg
    assert "v1" in msg and "v3" in msg and "v4" in msg


def test_get_active_prompt_unknown_version_raises():
    with pytest.raises(ValueError) as exc_info:
        get_active_prompt("v99")
    msg = str(exc_info.value)
    assert "v99" in msg
    assert "v1" in msg and "v3" in msg and "v4" in msg
