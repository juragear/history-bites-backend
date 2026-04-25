"""Railway cron entry points.

Per Backend Architecture's File layout, this module hosts the functions that
Railway's native cron schedule calls. Step 9 ships only run_push (FCM
delivery). Step 10 will add run_generation (the every-6h generation +
scheduling job) and the alert webhook calls.

Importable both ways: a Railway cron `python -m app.cron run_push` style, and
direct calls from FastAPI handlers (POST /admin/push uses this for manual
delivery + smoke testing).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import fcm
from app.config import settings
from app.models import Fact


logger = logging.getLogger(__name__)


# Notification copy. Hardcoded for v1 — there's no localization or A/B testing
# of titles. If these need to vary per fact, lift them into the Fact row.
_PUSH_TITLE = "HistoryBites"


def run_push(db: Session) -> dict[str, Any] | None:
    """Send today's fact to the FCM topic. Returns push metadata or None.

    Idempotency: pushed_at is updated on every successful send. If the cron
    retries due to upstream failure, the most recent successful timestamp
    wins. Client-side notification IDs dedupe at the device level (D17).

    Stale-fallback policy: unlike GET /today (which serves the latest
    available fact when today's row is missing), push only fires when there's
    a fact pinned to today AND it's not retracted. No fact = no push. Step 10
    will turn the "no fact" case into an alert; Step 9 just logs and returns.
    """
    today = date.today()

    fact = db.execute(
        select(Fact).where(
            Fact.scheduled_date == today,
            Fact.is_retracted.is_(False),
        )
    ).scalar_one_or_none()

    if fact is None:
        logger.warning(
            "run_push: no active fact for today, skipping",
            extra={"extra": {"date": today.isoformat()}},
        )
        return None

    message_id = fcm.send_to_topic(
        topic=settings.FCM_TOPIC,
        title=_PUSH_TITLE,
        body=fact.fact_text,
        data={
            "scheduled_date": fact.scheduled_date.isoformat(),
            "source_url": fact.source_url,
            "fact_id": str(fact.id),
        },
    )

    fact.pushed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(fact)

    logger.info(
        "run_push: delivered",
        extra={
            "extra": {
                "fact_id": fact.id,
                "scheduled_date": today.isoformat(),
                "message_id": message_id,
                "pushed_at": fact.pushed_at.isoformat() if fact.pushed_at else None,
            }
        },
    )

    return {
        "message_id": message_id,
        "fact_id": fact.id,
        "scheduled_date": fact.scheduled_date.isoformat(),
        "pushed_at": fact.pushed_at.isoformat() if fact.pushed_at else None,
    }
