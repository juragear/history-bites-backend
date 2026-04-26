"""Unit tests for the Step 13e section-aware truncation in app.wikipedia.

These cover the pure helper `_select_sections` end-to-end. The HTTP
boundary (`fetch_extract`) is exercised in integration tests via the
mocked-fixture path; this module tests the truncation logic in isolation.

Why these tests matter: the section sort + cap is the part of Step 13e most
likely to silently drift if someone reorders the priority lists or tweaks
the regex. The fixtures lock in the contract — lead always first, References
always dropped, History wins over Description, hard cap at 15k chars.
"""
from __future__ import annotations

from app.wikipedia import (
    _MAX_EXTRACT_CHARS,
    _MAX_SECTIONS,
    _select_sections,
)


def _make_section(header: str, body: str) -> str:
    """Build a `== Header ==\\nBody` block of the shape the action API returns."""
    return f"== {header} ==\n{body}"


def test_select_sections_drops_references_and_external_links():
    extract = "\n".join(
        [
            "Lead paragraph about the subject.",
            _make_section("History", "Long narrative about origins."),
            _make_section("References", "[1] Some Author, Some Book, 1999."),
            _make_section("External links", "Official website."),
            _make_section("See also", "Related topic."),
            _make_section("Bibliography", "Author, A. (2010)."),
            _make_section("Notes", "Footnote text."),
            _make_section("Citations", "More refs."),
            _make_section("Further reading", "Additional refs."),
            _make_section("Sources", "Source list."),
        ]
    )
    out = _select_sections(extract)
    assert "Lead paragraph" in out
    assert "Long narrative about origins" in out
    # All reference-class sections must be gone.
    for header in (
        "References",
        "External links",
        "See also",
        "Bibliography",
        "Notes",
        "Citations",
        "Further reading",
        "Sources",
    ):
        assert f"== {header} ==" not in out


def test_select_sections_prioritizes_history_over_description():
    """When more sections exist than _MAX_SECTIONS - 1 can hold (besides the
    lead), History/Background/Notable must win over Description/Overview and
    over arbitrary other sections.
    """
    extract = "\n".join(
        [
            "Lead.",
            _make_section("Demographics", "Body D."),
            _make_section("Geography", "Body G."),
            _make_section("Economy", "Body E."),
            _make_section("Climate", "Body C."),
            _make_section("Description", "Body Desc."),
            _make_section("Notable", "Body Not."),
            _make_section("History", "Body Hist."),
            _make_section("Background", "Body Back."),
            _make_section("Politics", "Body P."),
            _make_section("Culture", "Body Cul."),
        ]
    )
    out = _select_sections(extract)
    # _MAX_SECTIONS = 8 means 1 lead + 7 body. With 10 body sections offered,
    # 3 must be dropped — and the 3 dropped must be the lowest-priority ones,
    # NOT History/Background/Notable/Description.
    assert "Body Hist." in out, "History should always survive priority sort"
    assert "Body Back." in out, "Background should always survive priority sort"
    assert "Body Not." in out, "Notable should always survive priority sort"
    assert "Body Desc." in out, "Description should always survive priority sort"
    # Lead always present.
    assert "Lead." in out


def test_select_sections_lead_always_first_in_output():
    """Sort doesn't move the lead. It's the first section in the joined output."""
    extract = (
        "Lead paragraph that should appear first.\n"
        + _make_section("History", "History body.")
        + "\n"
        + _make_section("Background", "Background body.")
    )
    out = _select_sections(extract)
    assert out.startswith("Lead paragraph that should appear first.")


def test_select_sections_hard_caps_at_15k_chars():
    """Even if every section is high-priority and well under the section cap,
    the char hard cap fires."""
    big_body = "x" * 5000
    extract = "\n".join(
        [
            "Lead." + ("." * 100),
            _make_section("History", big_body),
            _make_section("Background", big_body),
            _make_section("Origins", big_body),
            _make_section("Founding", big_body),
        ]
    )
    out = _select_sections(extract)
    assert len(out) <= _MAX_EXTRACT_CHARS


def test_select_sections_respects_section_cap():
    """Section count cap is _MAX_SECTIONS total (lead + body)."""
    extract = "\n".join(
        ["Lead."] + [_make_section(f"Section{i}", f"Body {i}.") for i in range(20)]
    )
    out = _select_sections(extract)
    # Count section headers in output. Lead has no header; everything else does.
    section_header_count = out.count("\n== ") + (1 if out.startswith("== ") else 0)
    assert section_header_count <= _MAX_SECTIONS - 1


def test_select_sections_handles_empty_input():
    assert _select_sections("") == ""


def test_select_sections_lead_only_no_body_sections():
    """Article with only a lead and no headers passes through unchanged."""
    lead = "This is the entire article content with no section headers at all."
    assert _select_sections(lead) == lead
