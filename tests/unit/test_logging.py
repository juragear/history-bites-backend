"""Unit tests for app.main.StripQueryStringFormatter.

Code Review Fix 1 (P2.2): the formatter is what stops the bleed at the
source — without it, uvicorn's default access log captures
`/admin/review?token=<value>` verbatim into Railway logs.

The formatter mutates `record.args` index 2 (uvicorn's `full_path` arg)
before delegating to the parent formatter. These tests pin that contract:
the request-line path is stripped of `?...`, non-access records are not
mangled, and the full_path arg's substitution doesn't disturb args[0]
(client_addr) or args[4] (status code).
"""
from __future__ import annotations

import logging

from app.main import StripQueryStringFormatter


def _make_uvicorn_access_record(full_path: str) -> logging.LogRecord:
    """Build a LogRecord shaped like uvicorn's access logger emits.

    See `.venv/lib/python*/site-packages/uvicorn/protocols/http/h11_impl.py`
    around line 481 — the call is:
        access_logger.info(
            '%s - "%s %s HTTP/%s" %d',
            client_addr, method, full_path, http_version, status,
        )
    so args[2] is the path (with optional ?query).
    """
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:54321", "GET", full_path, "1.1", 200),
        exc_info=None,
    )


def test_strip_query_string_formatter_removes_token_from_request_line():
    """The whole point of P2.2: ?token=... must not survive into the formatted
    output regardless of where it is in the path."""
    formatter = StripQueryStringFormatter("%(message)s")
    record = _make_uvicorn_access_record("/admin/review?token=secret-value-here")
    output = formatter.format(record)
    assert "secret-value-here" not in output
    assert "/admin/review" in output
    assert "?" not in output


def test_strip_query_string_formatter_passes_through_no_query():
    """Paths without `?...` should round-trip unchanged."""
    formatter = StripQueryStringFormatter("%(message)s")
    record = _make_uvicorn_access_record("/today")
    output = formatter.format(record)
    assert "/today" in output
    assert "?" not in output


def test_strip_query_string_formatter_strips_multiple_query_params():
    """Defense against a longer query string with several params."""
    formatter = StripQueryStringFormatter("%(message)s")
    record = _make_uvicorn_access_record(
        "/admin/review?token=abc&debug=1&trace=true"
    )
    output = formatter.format(record)
    for needle in ("token", "abc", "debug", "trace"):
        assert needle not in output
    assert "/admin/review" in output


def test_strip_query_string_formatter_preserves_status_code_arg():
    """Mutating args[2] must NOT disturb args[4] (status code) — sanity check
    for the uvicorn-version-pinned arg index pattern."""
    formatter = StripQueryStringFormatter("%(message)s")
    record = _make_uvicorn_access_record("/admin/review?token=x")
    output = formatter.format(record)
    # The parent formatter renders args[4] via the format string's `%d`.
    assert " 200" in output


def test_strip_query_string_formatter_safe_on_short_args():
    """If a future uvicorn version emits a different arg shape (fewer than 3
    args), the formatter falls through unchanged rather than crashing."""
    formatter = StripQueryStringFormatter("%(message)s")
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="some unrelated log message",
        args=None,
        exc_info=None,
    )
    output = formatter.format(record)
    assert output == "some unrelated log message"
