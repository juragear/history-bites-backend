"""Cleanup-A item 2 (Codex P2): admin-gated /docs, /redoc, /openapi.json
in production.

Dev / test mode (`ENVIRONMENT != "production"`): unrestricted, defaults
behave like FastAPI's. Production: require admin auth via header or cookie.

Tests cover all three routes (HTML for /docs and /redoc, JSON for
/openapi.json) under both env modes. Prod mode is tested by monkeypatching
`settings.ENVIRONMENT = "production"` before each request, since the gate
dependency reads `settings.ENVIRONMENT` at request time.
"""
from __future__ import annotations

import pytest

from app.config import settings


# --- dev / test mode (ENVIRONMENT != "production"): unrestricted ----------


def test_docs_unrestricted_in_dev(client):
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_redoc_unrestricted_in_dev(client):
    resp = client.get("/redoc")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_openapi_json_unrestricted_in_dev(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("openapi", "").startswith("3.")
    assert body["info"]["title"] == "HistoryBites backend"


# --- production mode: require admin auth ----------------------------------


@pytest.fixture
def prod_env(monkeypatch):
    """Flip settings.ENVIRONMENT to 'production' for the test body. The
    gate dependency reads this at request time, so the change takes effect
    immediately without re-importing the app."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    yield


def test_docs_unauth_in_prod_returns_401(client, prod_env):
    resp = client.get("/docs")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "missing admin token"


def test_redoc_unauth_in_prod_returns_401(client, prod_env):
    resp = client.get("/redoc")
    assert resp.status_code == 401


def test_openapi_json_unauth_in_prod_returns_401(client, prod_env):
    resp = client.get("/openapi.json")
    assert resp.status_code == 401


def test_docs_with_bearer_in_prod_returns_200(client, admin_token, prod_env):
    resp = client.get("/docs", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_redoc_with_bearer_in_prod_returns_200(client, admin_token, prod_env):
    resp = client.get("/redoc", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200


def test_openapi_json_with_bearer_in_prod_returns_200(
    client, admin_token, prod_env
):
    resp = client.get(
        "/openapi.json", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["info"]["title"] == "HistoryBites backend"


def test_docs_with_invalid_bearer_in_prod_returns_401(client, prod_env):
    resp = client.get("/docs", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid admin token"


def test_docs_with_cookie_in_prod_returns_200(client, admin_token, prod_env):
    """Cookie auth path: the gate accepts the admin session cookie via the
    same alias as verify_admin_token. (In a real browser the cookie's path
    is `/admin` so it wouldn't reach `/docs` — but TestClient sends cookies
    regardless of path, and the server-side dep doesn't check path. Bearer
    is the operator-friendly path in production.)"""
    cookies = {settings.ADMIN_COOKIE_NAME: admin_token}
    resp = client.get("/docs", cookies=cookies)
    assert resp.status_code == 200


def test_non_bearer_auth_scheme_in_prod_returns_401(client, prod_env):
    """A non-Bearer Authorization header (e.g. Basic) is rejected — same
    behavior as verify_admin_token."""
    resp = client.get("/docs", headers={"Authorization": "Basic Zm9vOmJhcg=="})
    assert resp.status_code == 401
