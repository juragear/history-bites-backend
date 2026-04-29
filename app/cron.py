"""Railway cron entry points.

Per Backend Architecture's File layout, this module hosts the functions that
Railway's native cron schedule calls. Step 9 shipped run_push (FCM delivery).
Step 10 adds run_generation (the every-6h generation + scheduling job),
send_alert (Slack/Discord-compatible webhook), and the CLI entrypoint that
Railway cron actually invokes (`python -m app.cron run_generation` /
`python -m app.cron run_push`).

Importable both ways: as a module by FastAPI handlers (e.g. POST /admin/push),
and as a script by Railway cron via __main__.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import fcm
from app.config import settings
from app.db import SessionLocal
from app.models import Fact, PoolFact


logger = logging.getLogger(__name__)


# Notification copy. Hardcoded for v1 — there's no localization or A/B testing
# of titles. If these need to vary per fact, lift them into the Fact row.
_PUSH_TITLE = "HistoryBites"

# Hard cap on generation iterations per run. Defensive guard so a runaway
# generator (e.g. provider returning empty strings on every call) can't burn
# Gemini quota indefinitely. REVIEW_QUEUE_TARGET defaults to 20, so 30
# iterations leaves comfortable slack while still capping spend.
_MAX_GENERATION_ITERS = 30

# httpx timeout for send_alert. Webhooks should be fast; if Slack is slow we'd
# rather log a warning than block the cron pod.
_ALERT_TIMEOUT_S = 5.0


def send_alert(message: str) -> None:
    """POST a plain-text alert to ALERT_WEBHOOK_URL. Slack/Discord-compatible.

    Graceful degradation: if ALERT_WEBHOOK_URL is unset (None or empty), we log
    a WARNING with the alert payload and return — we don't crash the cron over
    missing alert plumbing. This matches the pydantic-settings shape from
    Step 9 (str | None = None).

    Network errors / non-2xx responses are logged but NOT raised — alerts are
    best-effort. The thing the alert is *about* is more important than the
    alert itself; we never want a busted webhook to break run_generation.
    """
    url = settings.ALERT_WEBHOOK_URL
    if not url:
        logger.warning(
            "alert webhook not configured, logging only",
            extra={"extra": {"message": message}},
        )
        return

    payload = {"text": message}
    # Code Review Fix 2 (P2.2): context-managed httpx.Client instead of the
    # httpx.post(...) shortcut. Same semantics — raise_for_status still routes
    # 4xx/5xx into the same except clause as connection errors — but the client
    # lifecycle is explicit and consistent with the rest of the codebase
    # (wikipedia._client, OllamaProvider). Sync function, so httpx.Client (not
    # AsyncClient).
    try:
        with httpx.Client(timeout=_ALERT_TIMEOUT_S) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(
            "alert webhook delivery failed",
            extra={
                "extra": {
                    "error": repr(exc),
                    "message": message,
                }
            },
        )
        return

    logger.info(
        "alert sent",
        extra={"extra": {"message": message}},
    )


def _count_pool(session: Session, status: str) -> int:
    return session.execute(
        select(func.count())
        .select_from(PoolFact)
        .where(PoolFact.status == status)
    ).scalar_one()


async def run_generation(session: Session) -> dict[str, Any]:
    """Every-6h cron: schedule tomorrow + top up the review queue + alert if low.

    Order of operations matters:
      1. Try to schedule tomorrow's fact NOW. If we run at 18:00 UTC and the
         pool is already low, scheduling first guarantees that today's
         generation work isn't blocked by review backlog (D8).
      2. Top up the review queue by calling generate_one_pool_fact in a loop
         until pending_review >= REVIEW_QUEUE_TARGET, generation fails, or we
         hit _MAX_GENERATION_ITERS.
      3. Re-count approved pool rows. If under APPROVED_ALERT_THRESHOLD (D8),
         send_alert so Will knows the review queue needs attention.

    Failure modes:
      - NoApprovedPool from schedule_tomorrows_fact: alert (no fact for
        tomorrow!) and continue with topup. Returning early would skip the
        topup and dig the hole deeper.
      - GenerationFailed mid-topup: log warning, break loop, continue to
        approved-count alert. Don't bail the whole cron — partial progress is
        still progress.
      - Any other exception: log and re-raise. Cron pod exits non-zero so
        Railway surfaces the failure.

    Returns a summary dict for /admin/cron/run-generation to echo back. Never
    returns None — even a fully-failed run produces a structured summary.
    """
    # Local import to avoid a circular import at module load time (generation
    # transitively imports app.main via schedule_tomorrows_fact's
    # invalidate_today_cache local import — fine at call time, fragile at
    # import time).
    from app import generation

    summary: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scheduled": None,
        "generated": 0,
        "generation_failures": 0,
        "pending_after": 0,
        "approved_after": 0,
        "alerts_sent": [],
    }

    # 1) Schedule tomorrow.
    target_date = date.today() + timedelta(days=1)
    try:
        fact = generation.schedule_tomorrows_fact(session)
    except generation.NoApprovedPool:
        msg = (
            f"HistoryBites: no approved pool rows for {target_date.isoformat()}. "
            "Tomorrow has no scheduled fact. Review the queue."
        )
        logger.warning(
            "run_generation: no approved pool",
            extra={"extra": {"target_date": target_date.isoformat()}},
        )
        send_alert(msg)
        summary["alerts_sent"].append("no_approved_pool")
    else:
        if fact is None:
            # Either already scheduled (idempotent) or lost-race rollback.
            summary["scheduled"] = "already_scheduled_or_race"
        else:
            summary["scheduled"] = {
                "fact_id": fact.id,
                "scheduled_date": fact.scheduled_date.isoformat(),
            }

    # 2) Top up the review queue.
    target = settings.REVIEW_QUEUE_TARGET
    iters = 0
    while iters < _MAX_GENERATION_ITERS:
        pending = _count_pool(session, "pending_review")
        if pending >= target:
            logger.info(
                "run_generation: review queue at target, stopping topup",
                extra={"extra": {"pending": pending, "target": target}},
            )
            break
        try:
            await generation.generate_one_pool_fact(session)
        except generation.GenerationFailed as exc:
            logger.warning(
                "run_generation: generation failed during topup, breaking",
                extra={
                    "extra": {
                        "error": str(exc),
                        "iter": iters,
                        "generated_so_far": summary["generated"],
                    }
                },
            )
            summary["generation_failures"] += 1
            break
        summary["generated"] += 1
        iters += 1
    else:
        # while-else: ran past the hard cap without breaking. Defensive log.
        logger.warning(
            "run_generation: hit MAX_GENERATION_ITERS, stopping",
            extra={"extra": {"iters": iters, "cap": _MAX_GENERATION_ITERS}},
        )

    # 3) Re-count and alert if approved is dangerously low.
    summary["pending_after"] = _count_pool(session, "pending_review")
    summary["approved_after"] = _count_pool(session, "approved")

    if summary["approved_after"] < settings.APPROVED_ALERT_THRESHOLD:
        msg = (
            f"HistoryBites: approved pool low "
            f"(approved={summary['approved_after']}, "
            f"threshold={settings.APPROVED_ALERT_THRESHOLD}). "
            "Review pending rows so future days have content."
        )
        logger.warning(
            "run_generation: approved below threshold",
            extra={
                "extra": {
                    "approved": summary["approved_after"],
                    "threshold": settings.APPROVED_ALERT_THRESHOLD,
                }
            },
        )
        send_alert(msg)
        summary["alerts_sent"].append("approved_low")

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    logger.info(
        "run_generation: complete",
        extra={"extra": summary},
    )
    return summary


def run_push(db: Session) -> dict[str, Any] | None:
    """Send today's fact to the FCM topic. Returns push metadata or None.

    Idempotency: pushed_at is updated on every successful send. If the cron
    retries due to upstream failure, the most recent successful timestamp
    wins. Client-side notification IDs dedupe at the device level (D17).

    Stale-fallback policy: unlike GET /today (which serves the latest
    available fact when today's row is missing), push only fires when there's
    a fact pinned to today AND it's not retracted. No fact = no push +
    Step 10 alert.
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
        send_alert(
            f"HistoryBites: run_push skipped — no active fact for "
            f"{today.isoformat()}. Devices will not receive a push today."
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


# --- CLI entrypoint -----------------------------------------------------------
#
# Railway cron's [[cron]] command field runs a shell command, not a Python
# function reference. So we expose `python -m app.cron <name>` and dispatch.
# Each subcommand owns its own SessionLocal — Railway cron pods are short-lived
# and we want explicit cleanup, not whatever get_db() does for FastAPI.


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "usage: python -m app.cron {run_generation|run_push}",
            file=sys.stderr,
        )
        return 2

    cmd = argv[1]

    if cmd == "run_push":
        with SessionLocal() as session:
            try:
                run_push(session)
            except fcm.FCMError as exc:
                # Code Review Fix 5 (Forensics tertiary): use logger.exception
                # so JSONFormatter (Fix 3 P2.1) renders exc_type / exc_message /
                # traceback into the structured log line. logger.error doesn't
                # set record.exc_info, so the formatter would have emitted only
                # `error: <repr>` — defeating Fix 3 on this branch. The sibling
                # run_generation branch below was migrated correctly during
                # Fix 3; this catches up. Surfaced by Push-Stale Forensics
                # 2026-04-28 because no scheduled run_push had ever fired in
                # production until Will registered the cron post-Forensics, so
                # the gap was latent.
                logger.exception("run_push: FCM send failed")
                send_alert(
                    f"HistoryBites: run_push failed to send to FCM "
                    f"({type(exc).__name__}); see Railway logs"
                )
                return 1
        return 0

    if cmd == "run_generation":
        with SessionLocal() as session:
            try:
                asyncio.run(run_generation(session))
            except Exception as exc:
                # Code Review Fix 3 (P3.3): Slack is a third-party SaaS;
                # `repr(exc)` of an OperationalError leaks the DB hostname /
                # port into the alert channel. Send only the type name, which
                # is enough to distinguish "DB outage" vs "config bug" at
                # glance. Full traceback stays in Railway via logger.exception
                # (now actually rendered, per Fix 3 P2.1).
                logger.exception(
                    "run_generation: unhandled error",
                    extra={"extra": {"error": repr(exc)}},
                )
                send_alert(
                    f"HistoryBites: run_generation crashed "
                    f"({type(exc).__name__}); see Railway logs"
                )
                return 1
        return 0

    print(f"unknown command: {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
