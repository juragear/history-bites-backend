"""schedule_tomorrows_fact: variety picker + idempotency + NoApprovedPool.

D21b: prefer an approved pool row whose region AND era are both absent from
the last 3 scheduled facts; fall back to oldest approved if none. Empty
history -> "everything is preferred".

D21a: FOR UPDATE SKIP LOCKED on the approved-row select. Not testable here
(SQLite doesn't honor row-level locking), but the SELECT still executes
correctly — we just lose the cross-process race protection that lives in
production. Tests cover the variety logic that runs ON TOP of that lock.

Idempotency: calling twice for the same target_date returns None on the
second call.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.generation import (
    NoApprovedPool,
    schedule_tomorrows_fact,
)
from app.models import Fact, PoolFact


def _approved_pool(
    *,
    fact_text: str = "approved pool fact",
    external_id: str,
    region: str | None = None,
    era: str | None = None,
    category: str | None = "test",
    created_at: datetime | None = None,
) -> PoolFact:
    return PoolFact(
        fact_text=fact_text,
        source_name="wikipedia",
        source_url=f"https://en.wikipedia.org/wiki/{external_id}",
        source_license="CC BY-SA 4.0",
        external_id=external_id,
        language="en",
        category=category,
        region=region,
        era=era,
        model_used="test:test",
        prompt_version="v1",
        status="approved",
        created_at=created_at or datetime.now(timezone.utc),
    )


def _scheduled_fact(
    *,
    scheduled_date: date,
    external_id: str,
    region: str | None = None,
    era: str | None = None,
) -> Fact:
    return Fact(
        scheduled_date=scheduled_date,
        fact_text=f"scheduled-{external_id}",
        source_name="wikipedia",
        source_url=f"https://en.wikipedia.org/wiki/{external_id}",
        source_license="CC BY-SA 4.0",
        external_id=external_id,
        language="en",
        category="test",
        region=region,
        era=era,
        model_used="test:test",
        prompt_version="v1",
    )


def test_schedule_tomorrows_fact_picks_preferred_when_history_full(db, monkeypatch):
    """3 recent facts cover regions {NA, EU, AS} and eras {Modern}. An approved
    pool row from region=Africa, era=Ancient is preferred over a region=NA
    pool row even if the latter is older."""
    # Don't bust the /today cache via main import — keeps the test focused.
    target = date(2026, 5, 1)

    # Recent history: NA-Modern, EU-Modern, AS-Modern.
    db.add(_scheduled_fact(scheduled_date=date(2026, 4, 30), external_id="r1", region="NA", era="Modern"))
    db.add(_scheduled_fact(scheduled_date=date(2026, 4, 29), external_id="r2", region="EU", era="Modern"))
    db.add(_scheduled_fact(scheduled_date=date(2026, 4, 28), external_id="r3", region="AS", era="Modern"))

    # Approved pool: oldest is NA-Modern (would lose), newer is Africa-Ancient (preferred).
    db.add(_approved_pool(
        external_id="p1", region="NA", era="Modern",
        created_at=datetime(2026, 4, 20, tzinfo=timezone.utc),
    ))
    db.add(_approved_pool(
        external_id="p2", region="Africa", era="Ancient",
        created_at=datetime(2026, 4, 25, tzinfo=timezone.utc),
    ))
    db.commit()

    fact = schedule_tomorrows_fact(db, target_date=target)
    assert fact is not None
    assert fact.external_id == "p2"
    assert fact.region == "Africa"
    assert fact.scheduled_date == target

    # The picked PoolFact is consumed.
    remaining = db.query(PoolFact).all()
    assert {p.external_id for p in remaining} == {"p1"}


def test_schedule_tomorrows_fact_falls_back_when_no_preferred(db):
    """If every approved row collides with recent regions/eras, the oldest
    approved is used (fallback)."""
    target = date(2026, 5, 1)

    db.add(_scheduled_fact(scheduled_date=date(2026, 4, 30), external_id="r1", region="NA", era="Modern"))
    db.add(_scheduled_fact(scheduled_date=date(2026, 4, 29), external_id="r2", region="EU", era="Modern"))

    # All approved rows collide with recent.
    db.add(_approved_pool(
        external_id="p1", region="NA", era="Modern",
        created_at=datetime(2026, 4, 20, tzinfo=timezone.utc),
    ))
    db.add(_approved_pool(
        external_id="p2", region="EU", era="Modern",
        created_at=datetime(2026, 4, 25, tzinfo=timezone.utc),
    ))
    db.commit()

    fact = schedule_tomorrows_fact(db, target_date=target)
    assert fact is not None
    # Oldest approved wins on fallback.
    assert fact.external_id == "p1"


def test_schedule_tomorrows_fact_empty_history_picks_oldest(db):
    """First-run case: no scheduled facts yet. Variety picker degrades to
    'oldest approved'."""
    target = date(2026, 5, 1)

    db.add(_approved_pool(
        external_id="p1", region="NA", era="Modern",
        created_at=datetime(2026, 4, 20, tzinfo=timezone.utc),
    ))
    db.add(_approved_pool(
        external_id="p2", region="Africa", era="Ancient",
        created_at=datetime(2026, 4, 25, tzinfo=timezone.utc),
    ))
    db.commit()

    fact = schedule_tomorrows_fact(db, target_date=target)
    assert fact is not None
    assert fact.external_id == "p1"


def test_schedule_tomorrows_fact_idempotent(db):
    """Second call for the same target_date returns None — there's already
    a Fact row for that date (the early-exit branch in is_already_scheduled)."""
    target = date(2026, 5, 1)

    db.add(_approved_pool(
        external_id="p1",
        created_at=datetime(2026, 4, 20, tzinfo=timezone.utc),
    ))
    db.add(_approved_pool(
        external_id="p2",
        created_at=datetime(2026, 4, 25, tzinfo=timezone.utc),
    ))
    db.commit()

    first = schedule_tomorrows_fact(db, target_date=target)
    assert first is not None
    assert first.external_id == "p1"  # oldest wins (empty history)

    second = schedule_tomorrows_fact(db, target_date=target)
    assert second is None
    # Pool consumed exactly once — second call must NOT touch p2.
    remaining = {p.external_id for p in db.query(PoolFact).all()}
    assert remaining == {"p2"}


def test_schedule_tomorrows_fact_no_approved_raises(db):
    """Pool has rows but all are pending_review — D21b: NoApprovedPool."""
    target = date(2026, 5, 1)

    pending = _approved_pool(external_id="x")
    pending.status = "pending_review"
    db.add(pending)
    db.commit()

    with pytest.raises(NoApprovedPool):
        schedule_tomorrows_fact(db, target_date=target)


def test_schedule_tomorrows_fact_default_target_is_tomorrow(db, monkeypatch):
    """Default target_date is date.today() + 1 (UTC, per D15)."""
    fixed_today = date(2026, 6, 10)

    class _FakeDate(date):
        @classmethod
        def today(cls):
            return fixed_today

    monkeypatch.setattr("app.generation.date", _FakeDate)

    db.add(_approved_pool(external_id="p1"))
    db.commit()

    fact = schedule_tomorrows_fact(db)  # no target_date kwarg
    assert fact is not None
    assert fact.scheduled_date == date(2026, 6, 11)
