"""send_alert is best-effort: a busted webhook must never break the cron run.

These tests guard the three failure paths flagged in Step 10:
  - URL unset/empty: log WARN, don't raise
  - happy path: POST {"text": "..."} with timeout
  - network error: log WARN, don't raise
"""
from __future__ import annotations

import httpx
import pytest

from app import cron


def test_send_alert_no_url_does_not_raise(monkeypatch, caplog):
    """When ALERT_WEBHOOK_URL is None or empty, send_alert logs a warning
    and returns. It must not crash run_generation just because the webhook
    isn't configured."""
    monkeypatch.setattr(cron.settings, "ALERT_WEBHOOK_URL", None)
    # Should not raise.
    cron.send_alert("test message — no webhook configured")

    monkeypatch.setattr(cron.settings, "ALERT_WEBHOOK_URL", "")
    cron.send_alert("test message — empty URL")


def test_send_alert_posts_slack_compatible_payload(monkeypatch):
    """Happy path: POST with body {"text": message} and a short timeout."""
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

    def _fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(
        cron.settings, "ALERT_WEBHOOK_URL", "https://hooks.slack.com/test"
    )
    monkeypatch.setattr(cron.httpx, "post", _fake_post)

    cron.send_alert("approved pool low")

    assert captured["url"] == "https://hooks.slack.com/test"
    assert captured["json"] == {"text": "approved pool low"}
    # 5s timeout per cron.py — webhooks should be fast or we move on.
    assert captured["timeout"] == 5.0


def test_send_alert_swallows_network_errors(monkeypatch):
    """If Slack is down or DNS fails, log and move on. The thing the alert is
    *about* is more important than the alert itself."""

    def _boom(url, json=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        cron.settings, "ALERT_WEBHOOK_URL", "https://hooks.slack.com/test"
    )
    monkeypatch.setattr(cron.httpx, "post", _boom)

    # Should NOT raise — graceful degradation.
    cron.send_alert("this should not crash the cron")


def test_send_alert_swallows_non_2xx(monkeypatch):
    """raise_for_status() raising HTTPStatusError must be caught."""

    class _BadResponse:
        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "500 Server Error",
                request=httpx.Request("POST", "https://hooks.slack.com/test"),
                response=httpx.Response(500),
            )

    def _fake_post(url, json=None, timeout=None):
        return _BadResponse()

    monkeypatch.setattr(
        cron.settings, "ALERT_WEBHOOK_URL", "https://hooks.slack.com/test"
    )
    monkeypatch.setattr(cron.httpx, "post", _fake_post)

    cron.send_alert("server returned 500, but we keep going")
