"""Security headers middleware (Codex P2 hardening, Cleanup-A item 1).

Sets standard browser security headers on every response. Wired into the
FastAPI app at construction time alongside CORSMiddleware.

Header rationale:
  - X-Frame-Options: DENY — block framing entirely (clickjacking defense).
    Belt-and-braces with frame-ancestors 'none' below; ancient browsers
    that don't honor CSP still respect this.
  - X-Content-Type-Options: nosniff — block MIME-type sniffing on responses.
  - Referrer-Policy: no-referrer — never leak the request URL to outbound
    links (admin pages may have token-bearing query strings in the future).
  - Permissions-Policy: () — deny every browsable feature. The admin pages
    don't need camera/mic/geo/etc.; opting all of them off shrinks the
    attack surface should a future template ever ship a third-party iframe.
  - Strict-Transport-Security: max-age=31536000; includeSubDomains —
    Railway's edge does not auto-set HSTS as of 2026-04-30 (verified via
    `curl -sI` showing no `strict-transport-security` header). Setting it
    here pins HTTPS for repeat visitors. No `preload` flag — production
    domain is `*.up.railway.app`, not under our own apex; preload submission
    isn't applicable.
  - Content-Security-Policy: conservative defaults plus 'unsafe-inline' for
    script-src and style-src because admin templates (review.html, the
    /admin/login form) ship inline <style> + <script>. Nonce-based CSP is
    a separate refactor — see Cleanup-B docs note. The non-inline parts
    (frame-ancestors 'none', base-uri 'none', form-action 'self',
    object-src 'none') still meaningfully reduce attack surface.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "object-src 'none'"
)


_HEADERS: dict[str, str] = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": _CSP,
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach the security headers above to every response.

    Uses setdefault semantics so a route handler that explicitly sets one
    of these headers (rare, but possible) wins over the default.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for name, value in _HEADERS.items():
            response.headers.setdefault(name, value)
        return response
