"""Unit tests for app.main.StripQueryStringFormatter and JSONFormatter.

Code Review Fix 1 (P2.2): StripQueryStringFormatter stops query-string secrets
at the source — without it, uvicorn's default access log captures
`/admin/review?token=<value>` verbatim into Railway logs.

Code Review Fix 3 (P2.1): JSONFormatter renders exception info when
`logger.exception(...)` is called. Without this, the two production catch-all
sites (`admin.py:725` and `cron.py:344`) silently drop the traceback they
explicitly asked for — operators saw only `repr(exc)` in Railway, no stack
frames. These tests pin the formatter's exception-rendering contract.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime

from app.main import JSONFormatter, StripQueryStringFormatter


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


# --- Code Review Fix 3 (P2.1): JSONFormatter exception rendering ----------


def test_json_formatter_renders_exception_traceback():
    """P2.1: logger.exception(...) must produce exc_type, exc_message,
    traceback fields. Before Fix 3, the formatter dropped record.exc_info
    silently and the two production catch-all sites produced log lines with
    no stack frames at all."""
    formatter = JSONFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="crash",
            args=(),
            exc_info=sys.exc_info(),
        )

    output = json.loads(formatter.format(record))

    assert output["exc_type"] == "ValueError"
    assert output["exc_message"] == "boom"
    assert "traceback" in output
    assert "ValueError" in output["traceback"]
    assert "boom" in output["traceback"]


def test_json_formatter_omits_exception_fields_when_no_exc_info():
    """P2.1: regular log calls must not gain traceback fields. The exception
    rendering only fires when exc_info is set on the record."""
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="just a log",
        args=(),
        exc_info=None,
    )

    output = json.loads(formatter.format(record))

    assert "exc_type" not in output
    assert "exc_message" not in output
    assert "traceback" not in output


def test_json_formatter_handles_non_serializable_extra():
    """Safety: a non-JSON-serializable extra value falls back to str() rather
    than crashing the formatter. Without `default=str` on json.dumps, a stray
    datetime / Decimal / Path in `extra` would take the whole log line with
    it instead of just being stringified."""
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="event",
        args=(),
        exc_info=None,
    )
    record.__dict__["extra"] = {"when": datetime(2026, 1, 1)}

    # Should not raise — `default=str` falls back cleanly.
    output = json.loads(formatter.format(record))
    assert "when" in output
    # The datetime got stringified; we don't assert the exact format because
    # datetime.__str__ is "2026-01-01 00:00:00" which is fine for log search.
    assert "2026" in output["when"]


def test_json_formatter_includes_extra_alongside_exception_fields():
    """exc_type / exc_message / traceback go alongside extra, not inside it.
    A logger.exception call with extra={...} must produce both blocks."""
    formatter = JSONFormatter()
    try:
        raise RuntimeError("upstream timeout")
    except RuntimeError:
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="run failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    record.__dict__["extra"] = {"operation": "run_generation", "iter": 3}

    output = json.loads(formatter.format(record))

    # extras land at top level
    assert output["operation"] == "run_generation"
    assert output["iter"] == 3
    # exception fields land at top level too
    assert output["exc_type"] == "RuntimeError"
    assert output["exc_message"] == "upstream timeout"
    assert "traceback" in output
