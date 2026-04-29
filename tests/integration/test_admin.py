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


def _cookie(token: str) -> dict[str, str]:
    """Code Review Fix 6: build the cookie jar entry the browser would send
    after a successful /admin/login. Tests that exercise the cookie auth
    path pass this via `cookies=` on the TestClient request."""
    from app.config import settings

    return {settings.ADMIN_COOKIE_NAME: token}


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


def test_auth_post_rejects_query_param(client, admin_token, db):
    """Code Review Fix 1 (P2.1): POST endpoints must NOT accept ?token=... in
    URL — only header or form. Query-string tokens land in access logs /
    proxy logs / browser history, which is a leak surface we want closed.
    The GET /admin/review HTML page is the one exception (covered below)."""
    resp = client.post(f"/admin/flush-pool?token={admin_token}")
    assert resp.status_code == 401, "POST with ?token=... must fail under strict auth"


def test_auth_post_rejects_query_param_on_admin_generate(
    client, admin_token, mock_wikipedia, mock_provider
):
    """Same contract as test_auth_post_rejects_query_param but on /admin/generate
    — sanity check that the strict policy is router-wide, not just one endpoint."""
    resp = client.post(f"/admin/generate?token={admin_token}")
    assert resp.status_code == 401


def test_auth_review_get_rejects_query_param(client, admin_token):
    """Code Review Fix 6 (2026-04-29) regression: the Fix 1 ?token=... entry
    point on GET /admin/review is removed. Query-string tokens are silently
    ignored; the route 303s to /admin/login when there's no valid header or
    cookie. This closes the Chunk 1 P2.2 carryover surface (URL bar / browser
    history / bookmarks / screenshots)."""
    resp = client.get(
        f"/admin/review?token={admin_token}", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_auth_review_get_accepts_bearer_header(client, admin_token):
    """The GET review page accepts the Bearer header — curl / Postman / Phase
    2 mobile clients exercise this path. Browser navigations land via the
    cookie path (covered separately under Fix 6 cookie-auth tests)."""
    resp = client.get("/admin/review", headers=_bearer(admin_token))
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


# --- Code Review Fix 6 (2026-04-29) — cookie-based admin handoff -----------
#
# Replaces the Fix 1 ?token=... query-string entry on GET /admin/review with
# a dedicated /admin/login form + HttpOnly session cookie. Bearer-header
# auth on programmatic clients is unchanged. Tests below cover: login form
# rendering, login POST success/failure, cookie-only auth on /admin/review
# and admin POSTs, the friendly 303-to-login on /admin/review-without-creds,
# the hard-401 retained on POSTs-without-creds, query-string regression,
# logout cookie clearing, logout auth requirement, and log scrubbing.


def test_admin_login_get_renders_form(client):
    """GET /admin/login renders an HTML form with a password input + submit
    + Cache-Control: no-store. No auth required (it's the entry point)."""
    resp = client.get("/admin/login", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers.get("cache-control") == "no-store"
    body = resp.text
    assert 'action="/admin/login"' in body
    assert 'method="POST"' in body or 'method="post"' in body
    assert 'name="token"' in body
    assert 'type="password"' in body


def test_admin_login_get_redirects_when_logged_in(client, admin_token):
    """If the operator hits /admin/login with a valid cookie already set,
    skip the form and redirect to /admin/review."""
    resp = client.get(
        "/admin/login",
        cookies=_cookie(admin_token),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/review"


def test_admin_login_post_success_sets_cookie_and_redirects(
    client, admin_token
):
    """POST /admin/login with a valid token -> 303 to /admin/review and
    Set-Cookie with the configured attributes (HttpOnly, Path=/admin,
    Max-Age=2592000, SameSite=Strict, name from settings)."""
    from app.config import settings

    resp = client.post(
        "/admin/login",
        data={"token": admin_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/review"

    set_cookie = resp.headers.get("set-cookie", "")
    assert f"{settings.ADMIN_COOKIE_NAME}=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Path=/admin" in set_cookie
    assert "SameSite=strict" in set_cookie or "SameSite=Strict" in set_cookie
    assert f"Max-Age={settings.ADMIN_COOKIE_MAX_AGE_SECONDS}" in set_cookie


def test_admin_login_post_failure_returns_401_with_form(client):
    """POST /admin/login with a wrong token -> 401 + form re-rendered with
    error banner. No Set-Cookie. Response is HTML (matches the GET shape)
    so the operator stays on a usable page and can retry."""
    resp = client.post(
        "/admin/login",
        data={"token": "this-is-not-the-token"},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("text/html")
    assert "set-cookie" not in {k.lower() for k in resp.headers.keys()}
    body = resp.text
    assert 'action="/admin/login"' in body
    assert "Invalid token" in body


def test_cookie_satisfies_auth_on_post_route(client, admin_token, db):
    """The session cookie is enough to authenticate an admin POST — no
    Bearer header, no form `token` field, just the cookie. Confirms the
    Fix 6 dep collapse really did add cookie as a valid source."""
    resp = client.post(
        "/admin/flush-pool", cookies=_cookie(admin_token)
    )
    assert resp.status_code == 200


def test_cookie_satisfies_auth_on_review_get(client, admin_token):
    """Browser path: cookie set after login, subsequent GET /admin/review
    renders the page (no header, no query)."""
    resp = client.get(
        "/admin/review", cookies=_cookie(admin_token), follow_redirects=False
    )
    assert resp.status_code == 200


def test_no_auth_get_review_redirects_to_login(client):
    """GET /admin/review with no header / no cookie / no form -> 303 to
    /admin/login. The Fix 6 friendly-redirect: a fresh browser visitor
    sees the login form, not a JSON 401."""
    resp = client.get("/admin/review", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_no_auth_post_returns_401(client):
    """POST routes still 401-hard with no creds. Only /admin/review GET
    does the friendly redirect — POSTs (likely scripts/curl) get the
    machine-readable 401."""
    resp = client.post("/admin/flush-pool")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "missing admin token"


def test_logout_clears_cookie_and_redirects(client, admin_token):
    """POST /admin/logout with a valid cookie -> 303 to /admin/login and
    Set-Cookie with Max-Age=0 (or expired Expires). A subsequent GET
    /admin/review WITHOUT resetting the cookie 303s to login again."""
    from app.config import settings

    resp = client.post(
        "/admin/logout", cookies=_cookie(admin_token), follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"

    set_cookie = resp.headers.get("set-cookie", "")
    assert f"{settings.ADMIN_COOKIE_NAME}=" in set_cookie
    # Browsers treat Max-Age=0 OR an expired Expires as deletion.
    assert "Max-Age=0" in set_cookie or "1970" in set_cookie


def test_logout_requires_auth(client):
    """POST /admin/logout with no creds -> 401. Logout is auth-required
    (you must be logged in to log out — belt-and-braces over SameSite=Strict
    CSRF defense)."""
    resp = client.post("/admin/logout")
    assert resp.status_code == 401


def test_login_failure_logs_at_info_without_token(client, caplog):
    """Failed login attempts log at INFO with `outcome: failure` in extras
    and the submitted token value NEVER appears in the record. Token
    discipline carry-over from Fix 5 Triage 0."""
    import logging

    submitted = "definitely-not-the-real-token-xyz123"

    caplog.set_level(logging.INFO, logger="app.admin")
    resp = client.post(
        "/admin/login", data={"token": submitted}, follow_redirects=False
    )
    assert resp.status_code == 401

    login_records = [
        r for r in caplog.records if r.getMessage() == "admin login"
    ]
    assert len(login_records) >= 1
    failure_records = [
        r
        for r in login_records
        if getattr(r, "extra", {}).get("outcome") == "failure"
    ]
    assert len(failure_records) >= 1, (
        "Expected at least one INFO record with outcome=failure"
    )
    assert failure_records[0].levelname == "INFO"

    # Belt-and-braces: scan every log record from this attempt for the
    # submitted token string. It must NOT appear anywhere — message, args,
    # extras, or formatted output.
    for record in caplog.records:
        assert submitted not in record.getMessage()
        assert submitted not in str(getattr(record, "extra", ""))
        assert submitted not in str(record.args or "")


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


def test_admin_generate_503_does_not_leak_provider_details(
    client, admin_token, monkeypatch
):
    """Code Review Fix 3 (P2.3): /admin/generate 503 detail must not contain
    the chained Gemini error or model name. The pre-fix detail string was
    `f"generation failed: {exc}"` which leaked `gemini-3-flash-preview`,
    `ClientError(...)`, and every attempted Wikipedia title."""
    from app.generation import GenerationFailed

    leaky_msg = (
        "Category:Han_dynasty: 3 attempts exhausted. "
        "Failures: ['Article: provider gemini:gemini-3-flash-preview API "
        "call failed: ClientError(\"503 UNAVAILABLE...\")']"
    )

    async def raise_generation_failed(*args, **kwargs):
        raise GenerationFailed(leaky_msg)

    monkeypatch.setattr(
        "app.generation.generate_one_pool_fact", raise_generation_failed
    )
    resp = client.post("/admin/generate", headers=_bearer(admin_token))

    assert resp.status_code == 503
    detail = resp.json()["detail"]

    assert "see server logs" in detail
    # Concrete leak strings traced in the Chunk 3 audit must NOT appear:
    assert "gemini-3-flash-preview" not in detail
    assert "ClientError" not in detail
    assert "Han_dynasty" not in detail
    assert "UNAVAILABLE" not in detail


def test_admin_run_generation_503_does_not_leak_db_details(
    client, admin_token, monkeypatch
):
    """Code Review Fix 3 (P2.2): /admin/cron/run-generation 503 detail must
    not contain SQLAlchemy schema or DB connection details. The pre-fix
    detail string for an OperationalError leaked DB hostname, IP, port, the
    full SQL statement, and the bind parameters."""
    from sqlalchemy.exc import OperationalError

    async def raise_operational_error(*args, **kwargs):
        raise OperationalError(
            "SELECT count(*) FROM pool WHERE pool.status = %(status_1)s",
            params={"status_1": "pending_review"},
            orig=Exception(
                "connection to server at \"db.internal.example\" "
                "(123.45.67.89), port 12345 failed"
            ),
        )

    monkeypatch.setattr("app.cron.run_generation", raise_operational_error)
    resp = client.post(
        "/admin/cron/run-generation", headers=_bearer(admin_token)
    )

    assert resp.status_code == 503
    detail = resp.json()["detail"]

    assert "see server logs" in detail
    # Concrete leak strings traced in the Chunk 3 audit must NOT appear:
    assert "db.internal.example" not in detail
    assert "12345" not in detail
    assert "OperationalError" not in detail
    assert "SELECT" not in detail
    assert "status_1" not in detail


def test_admin_push_503_does_not_leak_firebase_details(
    client, admin_token, db, mock_fcm
):
    """Code Review Fix 3 (P2.4): /admin/push 503 detail must not contain
    the FCM topic or the firebase-admin exception class. The pre-fix detail
    leaked `topic='daily-fact'`, the notification title, and the
    `UnregisteredError: Requested entity was not found.` chained string."""
    today = date.today()
    db.add(_fact(scheduled_date=today, external_id="ex-today"))
    db.commit()

    leaky_msg = (
        "FCM send failed (topic='daily-fact', title='HistoryBites'): "
        "UnregisteredError: Requested entity was not found."
    )
    mock_fcm["message_id"] = fcm.FCMError(leaky_msg)

    resp = client.post("/admin/push", headers=_bearer(admin_token))

    assert resp.status_code == 503
    detail = resp.json()["detail"]

    assert "see server logs" in detail
    # Concrete leak strings traced in the Chunk 3 audit must NOT appear:
    assert "UnregisteredError" not in detail
    assert "daily-fact" not in detail
    assert "Requested entity" not in detail


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
    shape; prevents browser re-POST on refresh).

    Code Review Fix 6: redirect target is now the bare `/admin/review` (the
    cookie carries auth on the follow-up GET). Pre-Fix-6 redirect was
    `/admin/review?token=<value>` which leaked the token back into the URL
    bar after every form submit."""
    db.add(_pool(external_id="p1"))
    db.commit()
    pool_id = db.query(PoolFact).first().id

    resp = client.post(
        f"/admin/review/{pool_id}",
        data={"rating": "4", "token": admin_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/review"

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
