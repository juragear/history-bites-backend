"""Cleanup-A item 1 (Codex P2): security headers middleware.

Verifies every response carries the standard browser security headers, on
both public (`/v1/*`) and admin (`/admin/*`) routes, regardless of status
code (200, 401, 404 paths all attract the headers).
"""
from __future__ import annotations


_REQUIRED_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


def _assert_security_headers(response) -> None:
    for name, value in _REQUIRED_HEADERS.items():
        assert response.headers.get(name) == value, (
            f"missing or wrong {name} header: {response.headers.get(name)!r}"
        )
    csp = response.headers.get("Content-Security-Policy", "")
    # Spot-check the CSP directives we care about; full string is asserted
    # in test_security_headers_csp_shape below.
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'none'" in csp
    assert "form-action 'self'" in csp
    assert "object-src 'none'" in csp
    assert "default-src 'self'" in csp


def test_security_headers_on_health(client):
    """Public unauth endpoint gets the headers."""
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    _assert_security_headers(resp)


def test_security_headers_on_today_404(client, db):
    """Headers attach on error responses, not just 200s."""
    resp = client.get("/v1/today")
    assert resp.status_code == 404
    _assert_security_headers(resp)


def test_security_headers_on_admin_unauth_login(client):
    """The /admin/login form (unauth router) gets the headers."""
    resp = client.get("/admin/login")
    assert resp.status_code == 200
    _assert_security_headers(resp)


def test_security_headers_on_admin_review_redirect(client):
    """/admin/review without a cookie redirects to /admin/login (303);
    headers attach on the redirect response too. follow_redirects=False
    so we can inspect the 303 itself."""
    resp = client.get("/admin/review", follow_redirects=False)
    assert resp.status_code == 303
    _assert_security_headers(resp)


def test_security_headers_on_admin_401(client):
    """Auth-required admin route without a token = 401, with headers."""
    resp = client.get("/admin/cron/status")
    assert resp.status_code == 401
    _assert_security_headers(resp)


def test_security_headers_csp_shape(client):
    """Pin the CSP string so accidental edits surface in CI rather than
    in production. CSP is the most likely header to need tweaking when
    a new template ships; the test forces the change to land via diff."""
    resp = client.get("/v1/health")
    csp = resp.headers["Content-Security-Policy"]
    expected = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "object-src 'none'"
    )
    assert csp == expected
