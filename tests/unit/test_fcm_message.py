"""D22 regression guard: build_message must produce a Message with BOTH
AndroidConfig (priority="high") and APNSConfig (apns-priority=10,
content-available, sound) so the same call delivers correctly to Android
today and iOS in Phase 2.5.

If a future refactor drops one of the two configs, these tests fail loudly.
"""
from __future__ import annotations

from firebase_admin import messaging

from app.fcm import build_message


def test_build_message_returns_messaging_message():
    msg = build_message(
        topic="daily-fact",
        title="HistoryBites",
        body="On this day in 1066, the Battle of Hastings reshaped England.",
        data={"fact_id": "42", "scheduled_date": "2026-04-25"},
    )
    assert isinstance(msg, messaging.Message)


def test_build_message_includes_android_high_priority():
    """Without high priority, FCM is throttled into Doze on Samsung/Xiaomi/etc.
    That's the entire reason FCM beat WorkManager for daily delivery (D17)."""
    msg = build_message(
        topic="daily-fact",
        title="HistoryBites",
        body="An interesting fact.",
        data={},
    )
    assert msg.android is not None, "AndroidConfig missing — Doze will eat pushes"
    assert msg.android.priority == "high"


def test_build_message_includes_apns_priority_10_and_aps():
    """D22: iOS path. apns-priority 10 = immediate; content_available wakes
    the app from background; sound forces the visible banner."""
    msg = build_message(
        topic="daily-fact",
        title="HistoryBites",
        body="An interesting fact.",
        data={},
    )
    assert msg.apns is not None, "APNSConfig missing — iOS won't receive"
    assert msg.apns.headers == {"apns-priority": "10"}
    aps = msg.apns.payload.aps
    assert aps.content_available is True
    assert aps.sound == "default"


def test_build_message_coerces_data_values_to_strings():
    """FCM rejects non-string data values with a confusing serialization
    error. send_to_topic coerces inside build_message so callers don't have
    to remember."""
    msg = build_message(
        topic="daily-fact",
        title="HistoryBites",
        body="An interesting fact.",
        data={"fact_id": 42, "scheduled_date": "2026-04-25"},
    )
    # All values must be str. Confirms the dict comprehension didn't get
    # silently removed during a refactor.
    assert all(isinstance(v, str) for v in msg.data.values())
    assert msg.data["fact_id"] == "42"


def test_build_message_includes_notification_title_and_body():
    msg = build_message(
        topic="daily-fact",
        title="HistoryBites",
        body="On this day in 1066, the Battle of Hastings reshaped England.",
        data={},
    )
    assert msg.notification is not None
    assert msg.notification.title == "HistoryBites"
    assert "Hastings" in msg.notification.body


def test_build_message_uses_topic_not_token():
    """D17: topic-based fanout, no per-user tokens."""
    msg = build_message(
        topic="daily-fact",
        title="HistoryBites",
        body="An interesting fact.",
        data={},
    )
    assert msg.topic == "daily-fact"
    assert msg.token is None


# --- Code Review Fix 5 (Chunk 5 P2.3): tenacity retry-EXHAUSTION ---------


def test_send_with_retry_exhaustion_raises_underlying_transient_error(monkeypatch):
    """Chunk 5 P2.3: when a transient firebase exception persists across
    all attempts, the final exception must be the underlying type — NOT
    `tenacity.RetryError`. The FCM decorator uses `reraise=True` for
    exactly this reason: `send_to_topic`'s outer `except Exception:`
    wraps the underlying exception into `FCMError(...)`; if `reraise=True`
    is ever dropped, callers would see `tenacity.RetryError` wrapped into
    `FCMError`, with the underlying cause one level deeper than the
    Chunk 3 audit traced.

    Also pins the attempt count: `stop_after_attempt(3)` means the
    underlying `messaging.send` is invoked exactly 3 times (initial + 2
    retries) before exhaustion.

    Synthetic transient exception: `_is_transient` matches by class name
    (\"UnavailableError\", \"DeadlineExceededError\", etc.) so a stub class
    with the matching `__name__` exercises the same predicate path as a
    real firebase-admin exception, without depending on importing
    firebase-admin's exception hierarchy.
    """
    import tenacity

    from app import fcm as fcm_module

    # Synthetic exception whose class name matches one of `_is_transient`'s
    # transient_names entries (so the predicate returns True).
    class UnavailableError(Exception):
        pass

    call_count = {"n": 0}

    def _always_unavailable(message, app=None):
        call_count["n"] += 1
        raise UnavailableError("simulated sustained FCM outage")

    monkeypatch.setattr(fcm_module.messaging, "send", _always_unavailable)

    fake_message = object()  # _send_with_retry passes through to messaging.send
    fake_app = object()

    import pytest

    with pytest.raises(UnavailableError) as exc_info:
        fcm_module._send_with_retry(fake_message, fake_app)

    # The underlying type bubbles up — NOT tenacity.RetryError
    assert not isinstance(exc_info.value, tenacity.RetryError)
    assert "simulated sustained FCM outage" in str(exc_info.value)

    # 3 attempts total per `stop_after_attempt(3)` in app/fcm.py.
    # Hardcoded here because the @retry decorator's stop= count is awkward
    # to extract at runtime; if the decorator changes, this test will fail
    # and force the change to be deliberate.
    assert call_count["n"] == 3, (
        f"Expected 3 attempts (stop_after_attempt(3)); got {call_count['n']}"
    )


def test_send_with_retry_does_not_retry_permanent_errors(monkeypatch):
    """Chunk 5 P2.3 (companion): `_is_transient` returns False for
    permanent firebase errors (`UnregisteredError`, `InvalidArgumentError`,
    `SenderIdMismatchError`). Those propagate immediately on the first
    attempt — no retry budget burned. This pins the predicate's reject
    list so a future change that accidentally widens transient to
    everything fails loudly here."""

    from app import fcm as fcm_module

    # Synthetic exception whose class name does NOT match transient_names.
    class UnregisteredError(Exception):
        pass

    call_count = {"n": 0}

    def _permanent_failure(message, app=None):
        call_count["n"] += 1
        raise UnregisteredError("topic has no subscribers")

    monkeypatch.setattr(fcm_module.messaging, "send", _permanent_failure)

    import pytest

    with pytest.raises(UnregisteredError):
        fcm_module._send_with_retry(object(), object())

    # Exactly one call — the predicate rejects the retry condition,
    # tenacity propagates immediately.
    assert call_count["n"] == 1, (
        f"Permanent errors must not retry; got {call_count['n']} attempts"
    )
