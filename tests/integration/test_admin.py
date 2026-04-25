"""Admin endpoints: bearer-token auth (3 sources) + 5 actions.

Covers:
  - Auth via Authorization: Bearer <token>
  - Auth via ?token=... query string (browser /admin/review)
  - Auth via hidden form field `token` (in-page form posts)
  - 401 on missing token / invalid token / wrong scheme
  - POST /admin/generate (success + GenerationFailed -> 503)
  - POST /admin/flush-pool (deletes pending only)
  - POST /admin/schedule/{pool_id}/{target_date} (success + 404 + 400 + 409)
  - POST /admin/retract/{target_date} (success + 404)
  - POST /admin/review/{pool_id} — rating-based (D26): JSON rating=5 ->
    approved, rating=2 -> rejected, rating=3 -> rejected (borderline),
    form 303, 400 on missing/out-of-range/non-int, re-rating overwrites
  - POST /admin/push (success + no-fact -> 400 + FCMError -> 503)
  - POST /admin/cron/run-generation (success summary)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app import fcm
from app.models import Fact, PoolFact


# --- helpers ---------------------------------------------------------------


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _fact(
    *,
    scheduled_date: date,
    external_id: str,
    fact_text: str = "scheduled fact",
    is_retracted: bool = False,
) -> Fact:
    """Build a Fact with is_retracted explicit. SQLite stores
    server_default='false' as text 'false', which doesn't match
    Fact.is_retracted.is_(False) — so we MUST set the column from Python
    in tests, not rely on the server-side default."""
    return Fact(
        scheduled_date=scheduled_date,
        fact_text=fact_text,
        source_name="wikipedia",
        source_url=f"https://en.wikipedia.org/wiki/{external_id}",
        source_license="CC BY-SA 4.0",
        external_id=external_id,
        language="en",
        model_used="m",
        prompt_version="v1",
        is_retracted=is_retracted,
    )


def _pool(
    *,
    external_id: str,
    status: str = "pending_review",
    region: str | None = "NA",
    era: str | None = "Modern",
    fact_text: str = "candidate fact",
) -> PoolFact:
    return PoolFact(
        fact_text=fact_text,
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
        status=status,
    )


# --- auth -------------------------------------------------------------------


def test_auth_accepts_bearer_header(client, admin_token, db):
    resp = client.post("/admin/flush-pool", headers=_bearer(admin_token))
    assert resp.status_code == 200


def test_auth_accepts_query_param(client, admin_token, db):
    resp = client.post(f"/admin/flush-pool?token={admin_token}")
    assert resp.status_code == 200


def test_auth_accepts_form_field(client, admin_token, db):
    """Hidden form field path used by the in-page rating submit button."""
    db.add(_pool(external_id="p1"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    # Form-encoded POST with `token=...` and `rating=4`.
    resp = client.post(
        f"/admin/review/{pool_id}",
        data={"rating": "4", "token": admin_token},
        follow_redirects=False,
    )
    # Successful form submit -> 303 redirect back to /admin/review.
    assert resp.status_code == 303


def test_auth_missing_token_returns_401(client):
    resp = client.post("/admin/flush-pool")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "missing admin token"


def test_auth_invalid_token_returns_401(client):
    resp = client.post(
        "/admin/flush-pool", headers=_bearer("this-is-not-the-token")
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid admin token"


def test_auth_rejects_non_bearer_scheme(client, admin_token):
    """Only literal 'Bearer' (case-sensitive) scheme is accepted. 'Token foo'
    or 'bearer foo' (lowercase) must fail."""
    resp = client.post(
        "/admin/flush-pool", headers={"Authorization": f"Token {admin_token}"}
    )
    assert resp.status_code == 401


# --- /admin/generate --------------------------------------------------------


async def test_admin_generate_success(
    client, admin_token, mock_wikipedia, mock_provider
):
    resp = client.post("/admin/generate", headers=_bearer(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["pool_id"] > 0
    assert body["fact_preview"]
    assert body["model_used"].startswith("gemini:") or body["model_used"].startswith("ollama:")


async def test_admin_generate_failure_returns_503(
    client, admin_token, mock_wikipedia, mock_provider
):
    """When generation can't produce a valid fact, surface as 503 (not 500)."""
    mock_provider["fact_text"] = ""  # always invalid
    resp = client.post("/admin/generate", headers=_bearer(admin_token))
    assert resp.status_code == 503
    assert "generation failed" in resp.json()["detail"]


# --- /admin/flush-pool ------------------------------------------------------


def test_admin_flush_pool_deletes_pending_only(client, admin_token, db):
    """Approved/rejected rows survive flush — they're decisions Will already
    made, no audit-trail loss."""
    db.add_all([
        _pool(external_id="p1", status="pending_review"),
        _pool(external_id="p2", status="pending_review"),
        _pool(external_id="a1", status="approved"),
        _pool(external_id="r1", status="rejected"),
    ])
    db.commit()

    resp = client.post("/admin/flush-pool", headers=_bearer(admin_token))
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 2}

    remaining = {p.external_id for p in db.query(PoolFact).all()}
    assert remaining == {"a1", "r1"}


# --- /admin/schedule/{pool_id}/{target_date} -------------------------------


def test_admin_schedule_success(client, admin_token, db):
    db.add(_pool(external_id="p1", status="approved"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    target = date(2026, 7, 1)
    resp = client.post(
        f"/admin/schedule/{pool_id}/{target.isoformat()}",
        headers=_bearer(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pool_id_consumed"] == pool_id
    assert body["scheduled_date"] == target.isoformat()
    assert body["fact_id"] > 0


def test_admin_schedule_404_if_pool_missing(client, admin_token):
    resp = client.post(
        "/admin/schedule/9999/2026-07-01", headers=_bearer(admin_token)
    )
    assert resp.status_code == 404


def test_admin_schedule_400_if_not_approved(client, admin_token, db):
    db.add(_pool(external_id="p1", status="pending_review"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    resp = client.post(
        f"/admin/schedule/{pool_id}/2026-07-01", headers=_bearer(admin_token)
    )
    assert resp.status_code == 400
    assert "must be 'approved'" in resp.json()["detail"]


def test_admin_schedule_409_on_date_collision(client, admin_token, db):
    target = date(2026, 7, 1)
    # Pre-existing fact for that date.
    db.add(_fact(scheduled_date=target, external_id="ex-existing", fact_text="existing"))
    db.add(_pool(external_id="p1", status="approved"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    resp = client.post(
        f"/admin/schedule/{pool_id}/{target.isoformat()}",
        headers=_bearer(admin_token),
    )
    assert resp.status_code == 409
    assert "already scheduled" in resp.json()["detail"]


# --- /admin/retract ---------------------------------------------------------


def test_admin_retract_success_and_d21d_note(client, admin_token, db):
    target = date(2026, 7, 1)
    db.add(_fact(scheduled_date=target, external_id="ex-r", fact_text="will retract"))
    db.commit()

    resp = client.post(
        f"/admin/retract/{target.isoformat()}", headers=_bearer(admin_token)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_retracted"] is True
    # D21d explainer: the response body documents the no-recall semantics so
    # Will sees it every time he retracts.
    assert "no-new-views" in body["note"]


def test_admin_retract_404_when_no_active_fact(client, admin_token):
    resp = client.post(
        "/admin/retract/2026-07-01", headers=_bearer(admin_token)
    )
    assert resp.status_code == 404


# --- /admin/review/{pool_id} — rating-based (Step 13c / D26) ---------------


def test_admin_review_json_rating_5_approved(client, admin_token, db):
    """JSON rating=5 -> status derives to 'approved' (D26: >=4)."""
    db.add(_pool(external_id="p1"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    resp = client.post(
        f"/admin/review/{pool_id}",
        headers=_bearer(admin_token),
        json={"rating": 5},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["review_rating"] == 5
    assert body["reviewed_at"] is not None


def test_admin_review_json_rating_2_rejected(client, admin_token, db):
    """JSON rating=2 -> status derives to 'rejected' (D26: <=3)."""
    db.add(_pool(external_id="p1"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    resp = client.post(
        f"/admin/review/{pool_id}",
        headers=_bearer(admin_token),
        json={"rating": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["review_rating"] == 2


def test_admin_review_json_rating_3_borderline_is_rejected(
    client, admin_token, db
):
    """The whole point of D26: rating=3 ('borderline') is rejected, not
    approved. Threshold is 4 because a published miss costs more than an
    unpublished hit on a daily-fact app."""
    db.add(_pool(external_id="p1"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    resp = client.post(
        f"/admin/review/{pool_id}",
        headers=_bearer(admin_token),
        json={"rating": 3},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["review_rating"] == 3


def test_admin_review_form_rating_returns_303_redirect(
    client, admin_token, db
):
    """Form-encoded rating submit -> 303 back to /admin/review (HTML page
    shape; prevents browser re-POST on refresh)."""
    db.add(_pool(external_id="p1"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    resp = client.post(
        f"/admin/review/{pool_id}",
        data={"rating": "4", "token": admin_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/admin/review" in resp.headers["location"]

    db.expire_all()
    row = db.get(PoolFact, pool_id)
    assert row.status == "approved"
    assert row.review_rating == 4


def test_admin_review_400_on_missing_rating(client, admin_token, db):
    db.add(_pool(external_id="p1"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    resp = client.post(
        f"/admin/review/{pool_id}",
        headers=_bearer(admin_token),
        json={},
    )
    assert resp.status_code == 400
    assert "rating is required" in resp.json()["detail"]


@pytest.mark.parametrize("bad_rating", [0, 6, -1, 100])
def test_admin_review_400_on_out_of_range_rating(
    client, admin_token, db, bad_rating
):
    db.add(_pool(external_id=f"p-{bad_rating}"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    resp = client.post(
        f"/admin/review/{pool_id}",
        headers=_bearer(admin_token),
        json={"rating": bad_rating},
    )
    assert resp.status_code == 400


def test_admin_review_400_on_non_int_rating(client, admin_token, db):
    """A non-numeric string for rating must 400, not silently coerce."""
    db.add(_pool(external_id="p1"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    resp = client.post(
        f"/admin/review/{pool_id}",
        headers=_bearer(admin_token),
        json={"rating": "four"},
    )
    assert resp.status_code == 400


# --- /admin/review/{pool_id} — tags + notes alongside rating (D26) ---------


def test_admin_review_json_rating_with_tags_and_notes_persisted(
    client, admin_token, db
):
    """JSON rating=5 with valid tags + notes round-trips into review_tags
    (JSON column) and review_notes (TEXT column)."""
    db.add(_pool(external_id="p1"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    resp = client.post(
        f"/admin/review/{pool_id}",
        headers=_bearer(admin_token),
        json={
            "rating": 5,
            "tags": ["surprising-angle", "concrete-detail"],
            "notes": "Nice angle on a familiar topic.",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["review_rating"] == 5
    assert body["review_tags"] == ["surprising-angle", "concrete-detail"]
    assert body["review_notes"] == "Nice angle on a familiar topic."

    # Confirm on the row directly — exercises the SQLAlchemy JSON column on
    # SQLite (and Postgres in prod). round-trips list[str] with no @compiles
    # hook needed.
    db.expire_all()
    row = db.get(PoolFact, pool_id)
    assert row.review_tags == ["surprising-angle", "concrete-detail"]
    assert row.review_notes == "Nice angle on a familiar topic."
    assert row.review_rating == 5


def test_admin_review_form_rating_with_tags_and_notes_persisted(
    client, admin_token, db
):
    """Form-encoded rating=2 with repeated `tags` keys + notes — the path
    the HTML review page actually uses on submit."""
    db.add(_pool(external_id="p1"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    # httpx form encoding: a list-valued data entry becomes repeated keys
    # (`tags=textbooky&tags=obvious`), which is what the browser submits and
    # what request.form().getlist("tags") expects.
    resp = client.post(
        f"/admin/review/{pool_id}",
        data={
            "rating": "2",
            "token": admin_token,
            "tags": ["textbooky", "obvious"],
            "notes": "Reads like a Wikipedia summary.",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db.expire_all()
    row = db.get(PoolFact, pool_id)
    assert row.status == "rejected"
    assert row.review_rating == 2
    assert row.review_tags == ["textbooky", "obvious"]
    assert row.review_notes == "Reads like a Wikipedia summary."


def test_admin_review_unknown_tag_returns_400(client, admin_token, db):
    db.add(_pool(external_id="p1"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    resp = client.post(
        f"/admin/review/{pool_id}",
        headers=_bearer(admin_token),
        json={"rating": 5, "tags": ["nonsense-tag"]},
    )
    assert resp.status_code == 400
    assert "Unknown tag" in resp.json()["detail"]
    assert "nonsense-tag" in resp.json()["detail"]


def test_admin_review_long_notes_silently_truncated_to_500(
    client, admin_token, db
):
    """Spec: silent truncation, not an error — a typo in the notes field
    shouldn't 4xx mid-review."""
    db.add(_pool(external_id="p1"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    long_notes = "x" * 600
    resp = client.post(
        f"/admin/review/{pool_id}",
        headers=_bearer(admin_token),
        json={"rating": 5, "notes": long_notes},
    )
    assert resp.status_code == 200

    db.expire_all()
    row = db.get(PoolFact, pool_id)
    assert row.review_notes is not None
    assert len(row.review_notes) == 500
    assert row.review_notes == "x" * 500


def test_admin_review_no_tags_or_notes_keys_succeeds(
    client, admin_token, db
):
    """A payload with only `rating` succeeds; both tags and notes columns
    end up NULL."""
    db.add(_pool(external_id="p1"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    resp = client.post(
        f"/admin/review/{pool_id}",
        headers=_bearer(admin_token),
        json={"rating": 5},
    )
    assert resp.status_code == 200

    db.expire_all()
    row = db.get(PoolFact, pool_id)
    assert row.status == "approved"
    assert row.review_rating == 5
    assert row.review_tags is None
    assert row.review_notes is None


def test_admin_review_empty_tags_list_normalizes_to_null(
    client, admin_token, db
):
    """Spec: empty cleaned tag list -> NULL, not `[]`. One canonical "no
    tags" state to avoid `tags IS NULL` vs `tags = '[]'` confusion later."""
    db.add(_pool(external_id="p1"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    resp = client.post(
        f"/admin/review/{pool_id}",
        headers=_bearer(admin_token),
        json={"rating": 5, "tags": []},
    )
    assert resp.status_code == 200

    db.expire_all()
    row = db.get(PoolFact, pool_id)
    assert row.review_tags is None


# --- /admin/review/{pool_id} — re-rating allowed (D26) ---------------------


def test_admin_review_re_rating_overwrites_status_and_rating(
    client, admin_token, db
):
    """D26 dropped the once-only guard. Re-rating from 5 -> 2 must flip the
    derived status from approved -> rejected and persist the new rating."""
    db.add(_pool(external_id="p1"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    # First rating: 5 -> approved
    resp = client.post(
        f"/admin/review/{pool_id}",
        headers=_bearer(admin_token),
        json={"rating": 5},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    # Re-rate the same row: 2 -> rejected
    resp = client.post(
        f"/admin/review/{pool_id}",
        headers=_bearer(admin_token),
        json={"rating": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["review_rating"] == 2

    db.expire_all()
    row = db.get(PoolFact, pool_id)
    assert row.status == "rejected"
    assert row.review_rating == 2


def test_admin_review_re_rating_overwrites_tags_and_notes(
    client, admin_token, db
):
    """Re-rating overwrites the prior tags + notes — the new submission is
    the canonical one. (Important for the migration cohort: pre-D26 tags
    were preserved; the re-rate flow can replace them.)"""
    db.add(_pool(external_id="p1"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    # First rating with one set of tags + notes.
    resp = client.post(
        f"/admin/review/{pool_id}",
        headers=_bearer(admin_token),
        json={
            "rating": 4,
            "tags": ["concrete-detail"],
            "notes": "first take",
        },
    )
    assert resp.status_code == 200

    # Re-rate with a different tag set + notes.
    resp = client.post(
        f"/admin/review/{pool_id}",
        headers=_bearer(admin_token),
        json={
            "rating": 2,
            "tags": ["textbooky", "obvious"],
            "notes": "second take",
        },
    )
    assert resp.status_code == 200

    db.expire_all()
    row = db.get(PoolFact, pool_id)
    assert row.review_rating == 2
    assert row.status == "rejected"
    assert row.review_tags == ["textbooky", "obvious"]
    assert row.review_notes == "second take"


# --- /admin/push ------------------------------------------------------------


def test_admin_push_success(client, admin_token, db, mock_fcm):
    today = date.today()
    db.add(_fact(scheduled_date=today, external_id="ex-today", fact_text="today's fact"))
    db.commit()

    resp = client.post("/admin/push", headers=_bearer(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["message_id"] == mock_fcm["message_id"]
    assert body["scheduled_date"] == today.isoformat()
    assert len(mock_fcm["calls"]) == 1


def test_admin_push_400_when_no_today_fact(client, admin_token, mock_fcm):
    resp = client.post("/admin/push", headers=_bearer(admin_token))
    assert resp.status_code == 400
    assert "no active fact" in resp.json()["detail"]


def test_admin_push_503_when_fcm_fails(
    client, admin_token, db, mock_fcm
):
    today = date.today()
    db.add(_fact(scheduled_date=today, external_id="ex-today", fact_text="today's fact"))
    db.commit()

    mock_fcm["message_id"] = fcm.FCMError("FCM unavailable")
    resp = client.post("/admin/push", headers=_bearer(admin_token))
    assert resp.status_code == 503


# --- /admin/cron/run-generation ---------------------------------------------


async def test_admin_run_generation_returns_summary(
    client, admin_token, db, mock_wikipedia, mock_provider, mock_alert
):
    """Smoke: endpoint returns the same structured summary the CLI logs."""
    # No approved pool, no recent facts. run_generation should:
    # - try schedule_tomorrows_fact -> NoApprovedPool -> alert
    # - top up the queue -> generate up to REVIEW_QUEUE_TARGET (20)
    # - approved still 0 -> alert again (approved_low)
    resp = client.post(
        "/admin/cron/run-generation", headers=_bearer(admin_token)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "started_at" in body
    assert "finished_at" in body
    assert body["generated"] >= 1  # produced at least one pool row
    # Two alerts expected: NoApprovedPool + approved_low.
    assert "no_approved_pool" in body["alerts_sent"]
    assert "approved_low" in body["alerts_sent"]
