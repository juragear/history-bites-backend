"""Unit tests for the Step 13e pre-filter helpers.

Three pure functions split across two modules — covered together because
they're a single conceptual layer (reject low-signal candidates before they
reach the model):

  - app.wikipedia._is_rejected_title — title-shape regex (List/Timeline/
    Society-of/Election/Journal patterns), applied at list_candidates time.
  - app.generation._looks_infoboxy — paragraph-density heuristic, applied
    after fetch_extract.
  - app.generation._is_template_dupe — first-8-words match against the last
    5 facts in the same category, applied after the model call but before
    insert.

These are pure-function tests; no DB, no fixtures, no monkeypatch. The
template-dedup test uses the `db` SQLAlchemy session fixture from conftest
because it inspects pool rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.generation import _is_template_dupe, _looks_infoboxy
from app.models import PoolFact
from app.wikipedia import _is_rejected_title


# --- _is_rejected_title ----------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        # List/Timeline patterns
        "List of strikes in Australia",
        "Lists of Egyptian pharaohs",
        "Timeline of Maori battles",
        "Outline of ancient Rome",
        "Index of Egyptian articles",
        "Glossary of medieval terms",
        # Meta-history patterns
        "History of the term Byzantine",
        "History of the concept of empire",
        "Definition of feudalism",
        "Etymology of Mongol",
        # Society-of-X patterns
        "Canadian Society for History and Philosophy of Mathematics",
        "Royal Institute for the Study of Archaeology",
        "American Academy of the Promotion of Science",
        # Journal/publication wrappers
        "Journal of Roman Studies",
        "Bulletin of the School of Oriental and African Studies",
        "Proceedings of the British Academy",
        # Election articles (the v1/v2 repeat-fact source)
        "1924 Victorian state election",
        "1929 Victorian state election",
        "1952 Victorian state election",
        "2016 Brexit referendum",
        "1865 Sydney by-election",
    ],
)
def test_is_rejected_title_rejects_meta_and_list_shapes(title):
    assert _is_rejected_title(title) is True


@pytest.mark.parametrize(
    "title",
    [
        # Real subject articles — the filter must NOT false-positive on these
        "Tang dynasty",
        "Han dynasty",
        "Battle of Hastings",
        "Mongol Armenia",
        "Hurrem Sultan",
        "Khazar Protectorate over Cherson",
        "Sukh Jiwan Mal",
        "Ancient Egypt",
        "Mali Empire",
        "Hipparchus",
        "Brick Gothic",
    ],
)
def test_is_rejected_title_accepts_real_subject_articles(title):
    assert _is_rejected_title(title) is False


# --- _looks_infoboxy -------------------------------------------------------


def test_looks_infoboxy_rejects_short_fragment_extract():
    """Six short fragments (each < 200 chars) -> 0/6 narrative ratio -> True."""
    extract = "\n\n".join(
        [
            "Born 1234.",
            "Died 1289.",
            "Father: Some Person.",
            "Mother: Other Person.",
            "Spouse: Yet Another.",
            "Issue: Many.",
        ]
    )
    assert _looks_infoboxy(extract) is True


def test_looks_infoboxy_accepts_narrative_extract():
    """Two long narrative paragraphs (each >= 200 chars) -> 2/2 ratio -> False."""
    para_a = "x" * 250
    para_b = "y" * 220
    extract = f"{para_a}\n\n{para_b}"
    assert _looks_infoboxy(extract) is False


def test_looks_infoboxy_rejects_empty_extract():
    """Empty extract has no paragraphs at all -> True (skip)."""
    assert _looks_infoboxy("") is True
    assert _looks_infoboxy("\n\n  \n\n") is True


def test_looks_infoboxy_borderline_30_percent_narrative():
    """At exactly 30% narrative, _looks_infoboxy returns False (>=0.3 threshold).
    With 1 long + 2 short, ratio = 1/3 ≈ 0.333 -> not infoboxy."""
    extract = "\n\n".join(["x" * 250, "short.", "another short."])
    assert _looks_infoboxy(extract) is False


# --- _is_template_dupe ----------------------------------------------------


def _seed_pool_row(
    db, *, category: str, fact_text: str, external_id: str, created_at: datetime
):
    db.add(
        PoolFact(
            fact_text=fact_text,
            source_name="wikipedia",
            source_url=f"https://en.wikipedia.org/wiki/Test_{external_id}",
            source_license="CC BY-SA 4.0",
            external_id=external_id,
            language="en",
            category=category,
            region="x",
            era="x",
            model_used="test:test",
            prompt_version="v1",
            status="rejected",
        )
    )
    db.commit()


def test_template_dupe_returns_true_on_matching_opener(db):
    """First 8 words match (case-insensitive, whitespace-collapsed) -> True."""
    base = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    _seed_pool_row(
        db,
        category="Category:Roman_Republic",
        fact_text="Quintus Sulpicius Camerinus Cornutus was elected to the powerful Roman office of consular tribune twice.",
        external_id="r1",
        created_at=base,
    )
    # Same first 8 words ("quintus sulpicius camerinus cornutus was elected
    # to the"), different 9th word onward — exactly the v1/v2 repeat-fact
    # template shape we want to catch.
    new_fact = "QUINTUS SULPICIUS CAMERINUS CORNUTUS was elected to the office of praetor in 401 BC."
    assert _is_template_dupe(db, "Category:Roman_Republic", new_fact) is True


def test_template_dupe_returns_false_on_distinct_opener(db):
    base = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    _seed_pool_row(
        db,
        category="Category:Roman_Republic",
        fact_text="Quintus Sulpicius Camerinus Cornutus was elected to the powerful Roman office.",
        external_id="r1",
        created_at=base,
    )
    new_fact = "Marcus Aurelius wrote his Meditations during a campaign on the Danube frontier."
    assert _is_template_dupe(db, "Category:Roman_Republic", new_fact) is False


def test_template_dupe_scoped_to_category(db):
    """A matching opener in a DIFFERENT category must not block insertion."""
    base = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    _seed_pool_row(
        db,
        category="Category:Han_dynasty",
        fact_text="The same opener words used here in a different category.",
        external_id="h1",
        created_at=base,
    )
    new_fact = "The same opener words used here for a Roman fact instead."
    assert _is_template_dupe(db, "Category:Roman_Republic", new_fact) is False


def test_template_dupe_returns_false_on_empty_input(db):
    assert _is_template_dupe(db, "Category:Han_dynasty", "") is False
    assert _is_template_dupe(db, "Category:Han_dynasty", "   ") is False
