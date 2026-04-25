"""Public read endpoints: /today, /archive, /health.

Covers:
  - /today exact match (D2)
  - /today stale fallback when today's row is missing (D2)
  - /today excludes retracted facts in both branches
  - /today 404 when no fact at all
  - /today cache: second call doesn't re-query DB (D21c)
  - /archive limit + retracted exclusion + ordering (newest first)
  - /health counts pool rows + reports latest scheduled / last push
  - /health degrades to 503 when DB is unreachable
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.models import Fact, PoolFact


def _scheduled(
    db,
    *,
    scheduled_date: date,
    fact_text: str = "scheduled fact",
    external_id: str = "ext-001",
    is_retracted: bool = False,
    pushed_at: datetime | None = None,
):
    row = Fact(
        scheduled_date=scheduled_date,
        fact_text=fact_text,
        source_name="wikipedia",
        source_url=f"https://en.wikipedia.org/wiki/{external_id}",
        source_license="CC BY-SA 4.0",
        external_id=external_id,
        language="en",
        category="test",
        region="NA",
        era="Modern",
        model_used="test:test",
        prompt_version="v1",
        is_retracted=is_retracted,
        pushed_at=pushed_at,
    )
    db.add(row)
    db.commit()
    return row


# --- /today -----------------------------------------------------------------


def test_today_returns_exact_match(client, db):
    today = date.today()
    _scheduled(db, scheduled_date=today, fact_text="today's fact", external_id="ex-today")

    resp = client.get("/today")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fact"] == "today's fact"
    assert body["scheduled_date"] == today.isoformat()
    assert body["is_stale"] is False
    assert body["source_url"].endswith("ex-today")


def test_today_falls_back_to_most_recent_past_when_no_today_row(client, db):
    """D2 stale fallback: if no row for today, return the most recent past
    fact with is_stale=true. Better than 404."""
    today = date.today()
    yesterday = today - timedelta(days=1)
    _scheduled(db, scheduled_date=yesterday, fact_text="yesterday's", external_id="ex-y")

    resp = client.get("/today")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fact"] == "yesterday's"
    assert body["is_stale"] is True
    assert body["scheduled_date"] == yesterday.isoformat()


def test_today_skips_retracted_in_exact_match(client, db):
    """Retracted today's row is invisible — fall through to the past fact."""
    today = date.today()
    yesterday = today - timedelta(days=1)
    _scheduled(db, scheduled_date=today, external_id="ex-bad", is_retracted=True)
    _scheduled(db, scheduled_date=yesterday, fact_text="yesterday OK", external_id="ex-y")

    resp = client.get("/today")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fact"] == "yesterday OK"
    assert body["is_stale"] is True


def test_today_skips_retracted_in_fallback(client, db):
    """Retracted past facts are excluded from the fallback chain too."""
    today = date.today()
    older = today - timedelta(days=5)
    yesterday = today - timedelta(days=1)
    _scheduled(db, scheduled_date=yesterday, external_id="ex-y", is_retracted=True)
    _scheduled(db, scheduled_date=older, fact_text="older OK", external_id="ex-o")

    resp = client.get("/today")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fact"] == "older OK"
    assert body["is_stale"] is True
    assert body["scheduled_date"] == older.isoformat()


def test_today_404_when_no_facts_at_all(client, db):
    """Cold-start: no facts in DB. Returns 404 with detail message."""
    resp = client.get("/today")
    assert resp.status_code == 404
    assert "no fact" in resp.json()["detail"]


def test_today_cache_hit_skips_db(client, db, monkeypatch):
    """Second /today call within TTL must serve from the in-memory cache.
    We verify by mutating the DB after the first call — if the cache works,
    the second call returns the original fact, not the new one."""
    today = date.today()
    _scheduled(db, scheduled_date=today, fact_text="first", external_id="ex-1")

    first = client.get("/today").json()
    assert first["fact"] == "first"

    # Replace the row's text via a direct DB write. If /today re-queried, it
    # would see "second". Cache hit means it still sees "first".
    db.query(Fact).filter(Fact.external_id == "ex-1").update(
        {"fact_text": "second"}
    )
    db.commit()

    second = client.get("/today").json()
    assert second["fact"] == "first", "cache should have prevented DB re-query"


# --- /archive ---------------------------------------------------------------


def test_archive_returns_newest_first_excludes_retracted(client, db):
    today = date.today()
    _scheduled(db, scheduled_date=today - timedelta(days=2), fact_text="2 days ago", external_id="a")
    _scheduled(db, scheduled_date=today - timedelta(days=1), fact_text="1 day ago", external_id="b")
    _scheduled(db, scheduled_date=today, fact_text="today", external_id="c")
    _scheduled(db, scheduled_date=today - timedelta(days=3), fact_text="retracted", external_id="d", is_retracted=True)

    resp = client.get("/archive")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3  # retracted excluded
    facts = [item["fact"] for item in body["items"]]
    assert facts == ["today", "1 day ago", "2 days ago"]


def test_archive_respects_limit(client, db):
    today = date.today()
    for i in range(10):
        _scheduled(
            db,
            scheduled_date=today - timedelta(days=i),
            fact_text=f"fact-{i}",
            external_id=f"e-{i}",
        )

    resp = client.get("/archive?limit=3")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    assert len(body["items"]) == 3


def test_archive_rejects_invalid_limit(client):
    """Pydantic Query validation: limit must be 1..100."""
    assert client.get("/archive?limit=0").status_code == 422
    assert client.get("/archive?limit=101").status_code == 422


# --- /health ----------------------------------------------------------------


def test_health_reports_pool_counts_and_dates(client, db):
    today = date.today()
    pushed_at = datetime.now(timezone.utc)

    _scheduled(db, scheduled_date=today, external_id="e-today", pushed_at=pushed_at)

    db.add_all([
        PoolFact(
            fact_text="p1", source_name="wikipedia", source_url="u", source_license="L",
            external_id="p1", language="en", model_used="m", prompt_version="v1",
            status="pending_review",
        ),
        PoolFact(
            fact_text="p2", source_name="wikipedia", source_url="u", source_license="L",
            external_id="p2", language="en", model_used="m", prompt_version="v1",
            status="pending_review",
        ),
        PoolFact(
            fact_text="a1", source_name="wikipedia", source_url="u", source_license="L",
            external_id="a1", language="en", model_used="m", prompt_version="v1",
            status="approved",
        ),
    ])
    db.commit()

    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["pool_pending_count"] == 2
    assert body["pool_approved_count"] == 1
    assert body["latest_scheduled_date"] == today.isoformat()
    assert body["last_push_at"] is not None


def test_health_503_when_db_unreachable(client, monkeypatch):
    """If the DB probe raises SQLAlchemyError, /health returns 503 with a
    'degraded' body. Required for Railway healthcheck visibility."""
    from sqlalchemy.exc import SQLAlchemyError

    from app import main as app_main

    def _broken_session_local():
        raise SQLAlchemyError("simulated outage")

    monkeypatch.setattr(app_main, "SessionLocal", _broken_session_local)

    resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db"] == "down"
