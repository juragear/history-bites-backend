"""Public read endpoints: /v1/today, /v1/archive, /v1/health (Code Review
Fix 4 P2.1 — `/v1/` versioning) + /admin/cron/status (P2.3 — operational
metrics moved off the public health probe).

Covers:
  - /v1/today exact match (D2)
  - /v1/today stale fallback when today's row is missing (D2)
  - /v1/today excludes retracted facts in both branches
  - /v1/today 404 when no fact at all
  - /v1/today cache: second call doesn't re-query DB (D21c)
  - /v1/today Cache-Control header set per Fix 4 P2.4
  - /v1/archive cursor pagination (P2.5): next_before cursor walk + final-page null
  - /v1/archive retracted exclusion + ordering (newest first)
  - /v1/archive limit validation
  - /v1/health thin shape (status + db only) + 503 on DB unreachable
  - /admin/cron/status full operational shape + auth-gated + 503 on DB unreachable
  - Old paths (/today, /archive, /health) return 404 — no compatibility window
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.models import Fact, PoolFact


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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


# --- /v1/today --------------------------------------------------------------


def test_today_returns_exact_match(client, db):
    today = date.today()
    _scheduled(db, scheduled_date=today, fact_text="today's fact", external_id="ex-today")

    resp = client.get("/v1/today")
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

    resp = client.get("/v1/today")
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

    resp = client.get("/v1/today")
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

    resp = client.get("/v1/today")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fact"] == "older OK"
    assert body["is_stale"] is True
    assert body["scheduled_date"] == older.isoformat()


def test_today_404_when_no_facts_at_all(client, db):
    """Cold-start: no facts in DB. Returns 404 with detail message."""
    resp = client.get("/v1/today")
    assert resp.status_code == 404
    assert "no fact" in resp.json()["detail"]


def test_today_cache_hit_skips_db(client, db, monkeypatch):
    """Second /v1/today call within TTL must serve from the in-memory cache.
    We verify by mutating the DB after the first call — if the cache works,
    the second call returns the original fact, not the new one."""
    today = date.today()
    _scheduled(db, scheduled_date=today, fact_text="first", external_id="ex-1")

    first = client.get("/v1/today").json()
    assert first["fact"] == "first"

    # Replace the row's text via a direct DB write. If /v1/today re-queried,
    # it would see "second". Cache hit means it still sees "first".
    db.query(Fact).filter(Fact.external_id == "ex-1").update(
        {"fact_text": "second"}
    )
    db.commit()

    second = client.get("/v1/today").json()
    assert second["fact"] == "first", "cache should have prevented DB re-query"


def test_today_sets_cache_control_header(client, db):
    """Code Review Fix 4 (P2.4): /v1/today must set Cache-Control consistent
    with the in-memory cache TTL so dio (Flutter's HTTP client) and
    Cloudflare (D13, Phase 3) share the same cache window."""
    today = date.today()
    _scheduled(db, scheduled_date=today, fact_text="today's fact", external_id="ex-cc")

    resp = client.get("/v1/today")
    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") == "public, max-age=300"


# --- /v1/archive ------------------------------------------------------------


def test_archive_returns_newest_first_excludes_retracted(client, db):
    today = date.today()
    _scheduled(db, scheduled_date=today - timedelta(days=2), fact_text="2 days ago", external_id="a")
    _scheduled(db, scheduled_date=today - timedelta(days=1), fact_text="1 day ago", external_id="b")
    _scheduled(db, scheduled_date=today, fact_text="today", external_id="c")
    _scheduled(db, scheduled_date=today - timedelta(days=3), fact_text="retracted", external_id="d", is_retracted=True)

    resp = client.get("/v1/archive")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 3  # retracted excluded
    facts = [item["fact"] for item in body["items"]]
    assert facts == ["today", "1 day ago", "2 days ago"]
    # All 3 items + retracted excluded => no more pages
    assert body["next_before"] is None


def test_archive_respects_limit(client, db):
    today = date.today()
    for i in range(10):
        _scheduled(
            db,
            scheduled_date=today - timedelta(days=i),
            fact_text=f"fact-{i}",
            external_id=f"e-{i}",
        )

    resp = client.get("/v1/archive?limit=3")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 3


def test_archive_rejects_invalid_limit(client):
    """Pydantic Query validation: limit must be 1..100."""
    assert client.get("/v1/archive?limit=0").status_code == 422
    assert client.get("/v1/archive?limit=101").status_code == 422


def test_archive_first_page_returns_next_before_cursor(client, db):
    """Code Review Fix 4 (P2.5): when more pages exist, next_before is set
    to the last returned item's scheduled_date so the caller can fetch the
    next page via ?before=<that date>."""
    today = date.today()
    for i in range(50):
        _scheduled(
            db,
            scheduled_date=today - timedelta(days=i),
            fact_text=f"fact-{i}",
            external_id=f"e-{i}",
        )

    resp = client.get("/v1/archive?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 10
    # 50 facts, 10 returned, 40 more — next_before must be set
    assert body["next_before"] is not None
    # Cursor is the LAST item's date (because order is DESC)
    assert body["next_before"] == body["items"][-1]["scheduled_date"]


def test_archive_last_page_returns_null_next_before(client, db):
    """Code Review Fix 4 (P2.5): when this is the final page (fewer items
    than `limit`), next_before is null. Lets Flutter stop paginating."""
    today = date.today()
    for i in range(5):
        _scheduled(
            db,
            scheduled_date=today - timedelta(days=i),
            fact_text=f"fact-{i}",
            external_id=f"e-{i}",
        )

    resp = client.get("/v1/archive?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 5
    assert body["next_before"] is None


def test_archive_pagination_walks_full_dataset(client, db):
    """Code Review Fix 4 (P2.5) — load-bearing test: chained ?before= calls
    walk the full archive without duplicates or gaps. If a future change
    breaks the cursor-driven walk (NULL scheduled_date row, non-unique
    scheduled_date, off-by-one in the limit+1 trick), this fails loudly."""
    today = date.today()
    for i in range(50):
        _scheduled(
            db,
            scheduled_date=today - timedelta(days=i),
            fact_text=f"fact-{i}",
            external_id=f"e-{i}",
        )

    seen_dates: set[str] = set()
    cursor: str | None = None
    iterations = 0
    while iterations < 10:  # sanity bound: 50 / 10 = 5 expected pages
        url = "/v1/archive?limit=10"
        if cursor:
            url += f"&before={cursor}"
        resp = client.get(url)
        assert resp.status_code == 200
        body = resp.json()
        for item in body["items"]:
            d = item["scheduled_date"]
            assert d not in seen_dates, f"duplicate {d} in pagination walk"
            seen_dates.add(d)
        cursor = body["next_before"]
        if cursor is None:
            break
        iterations += 1

    assert len(seen_dates) == 50, "expected all 50 facts across pages"
    assert cursor is None, "final page must have next_before=null"


# --- /v1/health (thin) ------------------------------------------------------


def test_v1_health_returns_thin_shape(client, db):
    """Code Review Fix 4 (P2.3): /v1/health returns ONLY status + db. Pool
    counts, scheduling runway, last_push_at, approved_status all moved to
    /admin/cron/status. Anyone can hit /v1/health without auth, so the
    shape stays minimal: connectivity check for Flutter + Railway."""
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok", "db": "ok"}, (
        f"expected thin {{status, db}} shape; got {body}"
    )

    # Operational metrics that Pre-Fix-4 /health used to expose must be ABSENT.
    for leaked_key in (
        "pool_pending_count",
        "pool_approved_count",
        "latest_scheduled_date",
        "last_push_at",
        "approved_status",
    ):
        assert leaked_key not in body, (
            f"/v1/health must not expose operator metric {leaked_key!r}"
        )


def test_v1_health_503_when_db_unreachable(client, monkeypatch):
    """If the DB probe raises, /v1/health returns 503 with the same thin
    shape: {status: 'degraded', db: 'down'}. No pool counts in the body —
    a 503 from /v1/health doesn't suddenly start leaking metrics."""
    from sqlalchemy.exc import SQLAlchemyError

    from app import main as app_main

    def _broken_session_local():
        raise SQLAlchemyError("simulated outage")

    monkeypatch.setattr(app_main, "SessionLocal", _broken_session_local)

    resp = client.get("/v1/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body == {"status": "degraded", "db": "down"}


# --- /admin/cron/status (rich, auth-gated) ----------------------------------


def test_admin_cron_status_returns_rich_shape(client, admin_token, db):
    """Code Review Fix 4 (P2.3): the rich operational view that pre-Fix-4
    /health returned now lives at /admin/cron/status, gated by the
    standard admin auth."""
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

    resp = client.get("/admin/cron/status", headers=_bearer(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["pool_pending_count"] == 2
    assert body["pool_approved_count"] == 1
    assert body["latest_scheduled_date"] == today.isoformat()
    assert body["last_push_at"] is not None


def test_admin_cron_status_requires_auth(client):
    """Code Review Fix 4 (P2.3): without an admin token, /admin/cron/status
    returns 401 — the operational view is no longer accessible to
    unauthenticated callers."""
    resp = client.get("/admin/cron/status")
    assert resp.status_code == 401


def test_admin_cron_status_503_when_db_unreachable(
    client, admin_token, monkeypatch
):
    """Same 503 shape as /v1/health but with the rich CronStatusResponse
    body (status='degraded', counts=0, approved_status='unknown')."""
    from sqlalchemy.exc import SQLAlchemyError

    from app import admin as app_admin

    def _broken_session_local():
        raise SQLAlchemyError("simulated outage")

    monkeypatch.setattr(app_admin, "SessionLocal", _broken_session_local)

    resp = client.get("/admin/cron/status", headers=_bearer(admin_token))
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db"] == "down"
    assert body["approved_status"] == "unknown"


# --- D8 three-tier approved_status (now on /admin/cron/status) --------------


def _seed_approved(db, n: int) -> None:
    """Seed n approved pool rows. Used by the three-tier tests below."""
    for i in range(n):
        db.add(
            PoolFact(
                fact_text=f"approved {i}",
                source_name="wikipedia",
                source_url=f"u{i}",
                source_license="L",
                external_id=f"a-{i}",
                language="en",
                model_used="m",
                prompt_version="v1",
                status="approved",
            )
        )
    db.commit()


def test_admin_cron_status_approved_ok_at_or_above_target(client, admin_token, db):
    """approved >= APPROVED_TARGET (default 7) -> 'ok'."""
    _seed_approved(db, 7)
    resp = client.get("/admin/cron/status", headers=_bearer(admin_token))
    assert resp.status_code == 200
    assert resp.json()["approved_status"] == "ok"


def test_admin_cron_status_approved_warm_in_band(client, admin_token, db):
    """ALERT_THRESHOLD (3) <= approved < APPROVED_TARGET (7) -> 'warm'."""
    _seed_approved(db, 4)
    resp = client.get("/admin/cron/status", headers=_bearer(admin_token))
    assert resp.status_code == 200
    assert resp.json()["approved_status"] == "warm"


def test_admin_cron_status_approved_low_below_threshold(client, admin_token, db):
    """approved < ALERT_THRESHOLD (3) -> 'low'."""
    _seed_approved(db, 1)
    resp = client.get("/admin/cron/status", headers=_bearer(admin_token))
    assert resp.status_code == 200
    assert resp.json()["approved_status"] == "low"


def test_admin_cron_status_approved_low_at_zero(client, admin_token, db):
    """Empty pool is 'low' (alerting territory)."""
    resp = client.get("/admin/cron/status", headers=_bearer(admin_token))
    assert resp.status_code == 200
    assert resp.json()["approved_status"] == "low"


# --- Old paths return 404 (Code Review Fix 4 P2.1: no compat redirect) -----


def test_old_today_path_returns_404(client, db):
    """No 308 redirect, no compatibility window. Phase 2 starts fresh on
    /v1/today; the old path returns 404 cleanly."""
    today = date.today()
    _scheduled(db, scheduled_date=today, fact_text="today's fact", external_id="ex-today")

    resp = client.get("/today")
    assert resp.status_code == 404


def test_old_archive_path_returns_404(client, db):
    resp = client.get("/archive")
    assert resp.status_code == 404


def test_old_health_path_returns_404(client, db):
    resp = client.get("/health")
    assert resp.status_code == 404
