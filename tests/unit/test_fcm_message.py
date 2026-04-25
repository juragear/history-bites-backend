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
