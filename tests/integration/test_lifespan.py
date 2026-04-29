"""Lifespan + OpenAPI surface regression tests (Code Review Fix 4 P2.2 + P3.3).

Two distinct contracts pinned here because both are framework-level concerns
that don't fit neatly under any one endpoint's test file:

  P2.2 — every endpoint declares its realistic non-2xx codes via `responses=`,
         so OpenAPI consumers (Flutter codegen, generated client types, manual
         readers of /openapi.json) see the full failure surface, not just
         200 + 422. Pre-Fix-4 probe via TestClient showed 401/404/409/503
         absent from the spec entirely.

  P3.3 — FastAPI's `lifespan=` context replaces the deprecated
         `@app.on_event(...)` decorators. Shutdown calls `wikipedia.aclose()`
         to drain the httpx singleton's connection pool gracefully on Railway
         pod restart instead of letting in-flight requests get torn down
         ungracefully when uvicorn receives SIGTERM.
"""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


# --- P3.3: lifespan calls wikipedia.aclose() ----------------------------


def test_lifespan_invokes_wikipedia_aclose_on_shutdown():
    """The lifespan context's shutdown phase must call wikipedia.aclose()
    so the module-level httpx client drains its pool. Without this, the
    aclose() function (added in Step 13e for exactly this purpose) is dead
    code and Railway pod restarts tear down in-flight requests
    ungracefully."""
    aclose_calls = 0

    async def fake_aclose():
        nonlocal aclose_calls
        aclose_calls += 1

    # Patch the binding at app.wikipedia.aclose because that's what the
    # lifespan calls; main.py imports `wikipedia` and references
    # `wikipedia.aclose`, so patching the function on the module reaches
    # the actual call site.
    with patch("app.wikipedia.aclose", fake_aclose):
        # `with TestClient(app)` triggers the lifespan context — startup
        # on enter, shutdown on exit. Without the `with`, lifespan never
        # fires (TestClient instances created bare don't manage lifespan).
        with TestClient(app) as client:
            # One request to confirm the app is alive inside the lifespan
            # window. Body irrelevant — we just want the context entered.
            client.get("/v1/health")
        # Exiting the TestClient context fires the shutdown phase.

    assert aclose_calls == 1, (
        f"Expected wikipedia.aclose() to be called exactly once on shutdown; "
        f"got {aclose_calls} calls"
    )


# --- P2.2: OpenAPI declares 4xx/5xx for every endpoint ------------------


def test_openapi_declares_realistic_non_2xx_responses():
    """Code Review Fix 4 (P2.2): every endpoint must declare the realistic
    non-2xx codes its handler actually raises. Pre-Fix-4 probe showed 200
    + 422 only across all 11 endpoints; this test pins the post-fix
    declarations so a future route addition that forgets `responses=`
    fails loudly instead of silently shipping with a half-documented
    OpenAPI spec.

    Asserts the declared codes are a SUPERSET of the expected set per
    endpoint — extra declarations are fine (they're forward-compatible);
    missing declarations are the regression we want to catch.
    """
    client = TestClient(app)
    spec = client.get("/openapi.json").json()
    paths = spec.get("paths", {})

    # Per-endpoint expected non-2xx codes traced from the handler bodies.
    # 422 is auto-included by FastAPI for any endpoint with path/query
    # validation; we don't assert on it because its presence is implicit.
    expected: dict[tuple[str, str], set[str]] = {
        # Public
        ("/v1/today", "get"): {"200", "404"},
        ("/v1/archive", "get"): {"200"},
        ("/v1/health", "get"): {"200", "503"},
        # Admin (401 inherited from router-level dependency on `admin_router`)
        ("/admin/generate", "post"): {"200", "401", "503"},
        (
            "/admin/schedule/{pool_id}/{target_date}",
            "post",
        ): {"200", "401", "404", "400", "409"},
        ("/admin/retract/{target_date}", "post"): {"200", "401", "404"},
        ("/admin/review/{pool_id}", "post"): {"200", "401", "404", "400"},
        ("/admin/push", "post"): {"200", "401", "400", "503"},
        ("/admin/cron/run-generation", "post"): {"200", "401", "503"},
        ("/admin/cron/status", "get"): {"200", "401", "503"},
        ("/admin/logout", "post"): {"303", "401"},
        # Code Review Fix 6: /admin/review redirects to /admin/login on
        # missing auth (303) instead of 401'ing — declared explicitly on the
        # route, not inherited. /admin/login GET + POST live on the
        # `admin_unauth_router` (no router-level auth dep), so their
        # response codes come from the per-route declarations.
        ("/admin/review", "get"): {"200", "303"},
        ("/admin/login", "get"): {"200", "303"},
        ("/admin/login", "post"): {"303", "401"},
    }

    missing: list[str] = []
    for (path, method), expected_codes in expected.items():
        ops = paths.get(path)
        assert ops is not None, f"path {path!r} missing from /openapi.json"
        op = ops.get(method)
        assert op is not None, f"{method.upper()} {path} missing from /openapi.json"
        declared = set(op.get("responses", {}).keys())
        not_declared = expected_codes - declared
        if not_declared:
            missing.append(
                f"{method.upper()} {path}: missing {sorted(not_declared)} "
                f"(declared: {sorted(declared)})"
            )

    assert not missing, (
        "OpenAPI declarations missing realistic non-2xx codes:\n"
        + "\n".join(f"  - {m}" for m in missing)
    )


def test_openapi_uses_error_detail_model_for_non_2xx():
    """Code Review Fix 4 (P2.2): the canonical envelope for non-2xx is the
    `ErrorDetail` model (matching Code Review Fix 3's sentinel `{detail: str}`
    shape across all admin 503 paths). Spot-check a few representative
    routes to confirm the schema linkage."""
    client = TestClient(app)
    spec = client.get("/openapi.json").json()
    paths = spec.get("paths", {})

    # /v1/today's 404 should reference ErrorDetail.
    today_404 = paths["/v1/today"]["get"]["responses"]["404"]
    schema_ref = (
        today_404.get("content", {})
        .get("application/json", {})
        .get("schema", {})
        .get("$ref", "")
    )
    assert schema_ref.endswith("/ErrorDetail"), (
        f"Expected /v1/today 404 to reference ErrorDetail; got {schema_ref!r}"
    )

    # /admin/generate's 503 should reference ErrorDetail.
    gen_503 = paths["/admin/generate"]["post"]["responses"]["503"]
    schema_ref = (
        gen_503.get("content", {})
        .get("application/json", {})
        .get("schema", {})
        .get("$ref", "")
    )
    assert schema_ref.endswith("/ErrorDetail"), (
        f"Expected /admin/generate 503 to reference ErrorDetail; got {schema_ref!r}"
    )
