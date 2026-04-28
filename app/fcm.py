"""Firebase Cloud Messaging client.

D17 made FCM the daily-delivery mechanism — clients subscribe to one topic,
the server pushes one message, FCM fans out. No per-user tokens.

D22 made the backend iOS-ready from this step: every send emits a Message with
BOTH AndroidConfig and APNSConfig so the same call delivers correctly to
Android (today) and iOS (Phase 2.5). The platform shape lives entirely inside
this module — callers (cron, admin endpoints) pass plain title/body/data and
don't need to know about apns vs fcm primitives.

Initialization is lazy: the firebase_admin app is created on first send rather
than at import time. Two reasons — (a) it lets the FastAPI process boot even
if Firebase is briefly unreachable, and (b) test runs that import this module
shouldn't try to validate the service-account JSON.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import firebase_admin
from firebase_admin import credentials, messaging
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings


logger = logging.getLogger(__name__)


class FCMError(Exception):
    """Raised when FCM rejects the send. Wraps the underlying firebase_admin
    exception with enough context (topic, title) for log/alert triage."""


_app: firebase_admin.App | None = None


def _get_firebase_app() -> firebase_admin.App:
    """Initialize firebase_admin lazily on first call.

    The service-account JSON lives in env (FIREBASE_SERVICE_ACCOUNT_JSON) as a
    single string — Railway env vars don't preserve real newlines well, but
    JSON's `\\n`-encoded private_key handles fine through json.loads.

    Code Review Fix 3 (P3.1 + Fix-2-deferred-P3.2): the cert + init block is
    wrapped to (a) recover from the duplicate-init race that the Chunk 2
    audit flagged — `if _app is None:` is a check-then-set, two threads can
    both pass the None check and the second `initialize_app(cred)` call
    raises `ValueError: app already exists`; (b) wrap the otherwise-bare
    `ValueError` from `credentials.Certificate(...)` (malformed PEM,
    missing private_key field) and any FirebaseError subclass from
    `initialize_app(...)` so callers see a single FCMError type instead of
    a leaky bare ValueError.

    Log lines record only `error_type`, never `str(exc)` — the full chain
    travels via the `from exc` cause to whatever upstream logger.exception
    catches the FCMError (now actually rendered as a traceback in JSON
    logs, per Fix 3 P2.1).
    """
    global _app
    if _app is None:
        try:
            sa_dict = json.loads(settings.FIREBASE_SERVICE_ACCOUNT_JSON)
        except json.JSONDecodeError as exc:
            raise FCMError(
                "FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON; "
                "see server logs for details"
            ) from exc

        try:
            cred = credentials.Certificate(sa_dict)
            _app = firebase_admin.initialize_app(cred)
        except ValueError as exc:
            # Two cases reach this branch:
            #   1. credentials.Certificate(...) on a malformed/incomplete SA
            #      dict — bare ValueError.
            #   2. firebase_admin.initialize_app(...) on the duplicate-init
            #      race — ValueError("the default Firebase app already
            #      exists ...").
            # Try the race-recovery path first; if get_app() also raises
            # ValueError, the default app genuinely doesn't exist and the
            # original ValueError was a real init failure (case 1). Wrap
            # as FCMError with a scrubbed message either way.
            try:
                _app = firebase_admin.get_app()
            except ValueError:
                logger.warning(
                    "firebase init failed",
                    extra={"extra": {"error_type": type(exc).__name__}},
                )
                raise FCMError(
                    "firebase init failed; see server logs for details"
                ) from exc
        except Exception as exc:
            # Any FirebaseError subclass or auth/network error reaching the
            # cert validator path. Same scrubbing posture: type only on the
            # log line; full chain via the cause.
            logger.warning(
                "firebase init failed",
                extra={"extra": {"error_type": type(exc).__name__}},
            )
            raise FCMError(
                "firebase init failed; see server logs for details"
            ) from exc

        logger.info(
            "firebase_admin initialized",
            extra={"extra": {"project_id": sa_dict.get("project_id")}},
        )
    return _app


def _is_transient(exc: BaseException) -> bool:
    """Retry only on transient firebase_admin errors.

    UnregisteredError / InvalidArgumentError / SenderIdMismatchError are
    permanent — retrying them just burns time and produces noisier logs. The
    default tenacity behaviour of retrying everything is wrong here.
    """
    # firebase_admin.exceptions.UnavailableError / DeadlineExceededError /
    # InternalError / etc. all subclass FirebaseError. Those are transient.
    # InvalidArgumentError / NotFoundError / FailedPreconditionError are not.
    transient_names = {
        "UnavailableError",
        "DeadlineExceededError",
        "InternalError",
        "AbortedError",
        "ResourceExhaustedError",
    }
    cls_name = type(exc).__name__
    if cls_name in transient_names:
        return True
    # OSError / network-layer errors before firebase_admin can categorize them.
    return isinstance(exc, OSError)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1),
    retry=retry_if_exception(_is_transient),
    reraise=True,
    # Code Review Fix 3 (P3.2): same rationale as wikipedia._get_json —
    # without this, transient FCM 5xx / DeadlineExceeded retries are silent
    # and the operator can't tell a one-off blip from a sustained outage.
    before_sleep=before_sleep_log(logger, logging.INFO),
)
def _send_with_retry(message: messaging.Message, app: firebase_admin.App) -> str:
    return messaging.send(message, app=app)


def build_message(
    *, topic: str, title: str, body: str, data: dict[str, str]
) -> messaging.Message:
    """Build the platform-aware Message used by send_to_topic.

    Exposed for visual inspection in tests/smoke (so we can assert that BOTH
    AndroidConfig and APNSConfig are present per D22) without actually sending.
    """
    # FCM requires data values to be strings. Coerce defensively — a stray int
    # in the dict otherwise raises a confusing serialization error at send time.
    str_data: dict[str, str] = {k: str(v) for k, v in data.items()}

    return messaging.Message(
        topic=topic,
        notification=messaging.Notification(title=title, body=body),
        data=str_data,
        # Android: high priority bypasses Doze on Samsung/Xiaomi/Oppo/etc., the
        # actual reason FCM beats WorkManager for daily delivery (D17).
        android=messaging.AndroidConfig(priority="high"),
        # iOS (D22): apns-priority 10 = immediate delivery; content_available
        # ensures the system wakes the app even when backgrounded; sound forces
        # the user-visible notification banner.
        apns=messaging.APNSConfig(
            headers={"apns-priority": "10"},
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    content_available=True,
                    sound="default",
                ),
            ),
        ),
    )


def send_to_topic(
    *, topic: str, title: str, body: str, data: dict[str, Any]
) -> str:
    """Send one notification to all subscribers of `topic`.

    Returns the FCM message ID (e.g. "projects/.../messages/0:1234..."). Raises
    FCMError on permanent failures or after retries exhaust on transients.
    """
    message = build_message(topic=topic, title=title, body=body, data=dict(data))
    app = _get_firebase_app()
    try:
        message_id = _send_with_retry(message, app)
    except Exception as exc:
        raise FCMError(
            f"FCM send failed (topic={topic!r}, title={title!r}): {exc}"
        ) from exc
    logger.info(
        "fcm send ok",
        extra={
            "extra": {
                "topic": topic,
                "title": title,
                "message_id": message_id,
            }
        },
    )
    return message_id
