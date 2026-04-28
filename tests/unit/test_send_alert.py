"""send_alert is best-effort: a busted webhook must never break the cron run.

These tests guard the three failure paths flagged in Step 10:
  - URL unset/empty: log WARN, don't raise
  - happy path: POST {"text": "..."} with timeout
  - network error: log WARN, don't raise

Code Review Fix 2 (P2.2): mocking pattern moved from `cron.httpx.post` to
`cron.httpx.Client` because `send_alert` now uses a context-managed Client
(`with httpx.Client(...) as client: client.post(...)`) instead of the
`httpx.post(...)` shortcut. The new lifecycle assertion (`client.closed`)
pins the context-manager exit behaviour so a future refactor that drops the
`with`-block fails loudly here.
"""
from __future__ import annotations

import httpx

from app import cron


class _OkResponse:
    def raise_for_status(self) -> None:
        return None


def _make_fake_client_class(
    instances: list, *, post_side_effect=None, response=None
):
    """Build a per-test FakeClient class.

    `instances` is the list new clients will append themselves to (one per
    `httpx.Client(...)` call). `post_side_effect`, if given, is called as
    `post_side_effect(url, json)` and its return value is returned (or
    raised, if it's an Exception). Otherwise `response` is returned (default:
    a `_OkResponse`).
    """

    class _FakeClient:
        def __init__(self, *, timeout=None):
            self.timeout = timeout
            self.closed = False
            self.posts: list[dict] = []
            instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.closed = True
            # Don't suppress exceptions — let them propagate so send_alert's
            # outer try/except can catch HTTPError as it would in production.
            return False

        def post(self, url, json=None):
            self.posts.append({"url": url, "json": json})
            if post_side_effect is not None:
                result = post_side_effect(url, json)
                if isinstance(result, Exception):
                    raise result
                return result
            if response is not None:
                return response
            return _OkResponse()

    return _FakeClient


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
    """Happy path: open a Client with a 5s timeout, POST {"text": message},
    close the client cleanly via the context manager."""
    instances: list = []
    monkeypatch.setattr(
        cron.httpx, "Client", _make_fake_client_class(instances)
    )
    monkeypatch.setattr(
        cron.settings, "ALERT_WEBHOOK_URL", "https://hooks.slack.com/test"
    )

    cron.send_alert("approved pool low")

    # Code Review Fix 2 (P2.2): client lifecycle assertions.
    assert len(instances) == 1, "Expected exactly one httpx.Client to be created"
    client = instances[0]
    # 5s timeout per cron.py — webhooks should be fast or we move on.
    assert client.timeout == 5.0
    assert client.posts == [
        {
            "url": "https://hooks.slack.com/test",
            "json": {"text": "approved pool low"},
        }
    ]
    assert client.closed is True, "Client must be closed via the with-block exit"


def test_send_alert_swallows_network_errors(monkeypatch):
    """If Slack is down or DNS fails, log and move on. The thing the alert is
    *about* is more important than the alert itself."""
    instances: list = []

    def _boom(url, json):
        return httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        cron.httpx,
        "Client",
        _make_fake_client_class(instances, post_side_effect=_boom),
    )
    monkeypatch.setattr(
        cron.settings, "ALERT_WEBHOOK_URL", "https://hooks.slack.com/test"
    )

    # Should NOT raise — graceful degradation.
    cron.send_alert("this should not crash the cron")

    # Even on failure, the context manager must close the client.
    assert len(instances) == 1
    assert instances[0].closed is True


def test_send_alert_swallows_non_2xx(monkeypatch):
    """raise_for_status() raising HTTPStatusError must be caught."""
    instances: list = []

    class _BadResponse:
        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "500 Server Error",
                request=httpx.Request("POST", "https://hooks.slack.com/test"),
                response=httpx.Response(500),
            )

    monkeypatch.setattr(
        cron.httpx,
        "Client",
        _make_fake_client_class(instances, response=_BadResponse()),
    )
    monkeypatch.setattr(
        cron.settings, "ALERT_WEBHOOK_URL", "https://hooks.slack.com/test"
    )

    cron.send_alert("server returned 500, but we keep going")

    assert len(instances) == 1
    assert instances[0].closed is True
