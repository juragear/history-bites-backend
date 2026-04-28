"""run_generation + run_push + the python -m app.cron CLI dispatcher.

run_generation has three phases (schedule tomorrow, top up, alert if low) and
multiple failure modes; we exercise each independently. run_push has the
no-fact and FCM-error paths. The CLI dispatcher tests cover argument parsing
and exit codes — the entrypoint Railway actually invokes.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app import cron, fcm
from app.config import settings
from app.models import Fact, PoolFact


# --- helpers ----------------------------------------------------------------


def _approved_pool(
    *,
    external_id: str,
    region: str | None = "NA",
    era: str | None = "Modern",
    fact_text: str = "approved",
) -> PoolFact:
    return PoolFact(
        fact_text=fact_text,
        source_name="wikipedia",
        source_url=f"https://en.wikipedia.org/wiki/{external_id}",
        source_license="CC BY-SA 4.0",
        external_id=external_id,
        language="en",
        category="test",
        region=region,
        era=era,
        model_used="test:test",
        prompt_version="v1",
        status="approved",
    )


def _fact(
    *,
    scheduled_date: date,
    external_id: str,
    fact_text: str = "scheduled",
    is_retracted: bool = False,
    pushed_at: datetime | None = None,
) -> Fact:
    """is_retracted set explicitly — see test_admin._fact for SQLite quirk."""
    return Fact(
        scheduled_date=scheduled_date,
        fact_text=fact_text,
        source_name="wikipedia",
        source_url=f"https://en.wikipedia.org/wiki/{external_id}",
        source_license="CC BY-SA 4.0",
        external_id=external_id,
        language="en",
        model_used="test:test",
        prompt_version="v1",
        is_retracted=is_retracted,
        pushed_at=pushed_at,
    )


# --- run_generation ---------------------------------------------------------


async def test_run_generation_happy_path_schedules_and_tops_up(
    db, mock_wikipedia, mock_provider, mock_alert, monkeypatch
):
    """With approved pool available, run_generation should schedule tomorrow
    AND top up to REVIEW_QUEUE_TARGET. Alerts should NOT fire."""
    # One approved pool row so schedule_tomorrows_fact succeeds.
    db.add(_approved_pool(external_id="approved-1"))
    # Lower the target so the topup loop terminates quickly.
    monkeypatch.setattr(settings, "REVIEW_QUEUE_TARGET", 2)
    monkeypatch.setattr(settings, "APPROVED_ALERT_THRESHOLD", 0)
    db.commit()

    summary = await cron.run_generation(db)

    assert summary["scheduled"] is not None
    assert summary["scheduled"] != "already_scheduled_or_race"
    assert summary["generated"] >= 2
    assert summary["alerts_sent"] == []  # no alerts fired
    assert summary["pending_after"] >= 2


async def test_run_generation_alerts_when_no_approved_pool(
    db, mock_wikipedia, mock_provider, mock_alert, monkeypatch
):
    """No approved rows -> NoApprovedPool -> alert + continue with topup."""
    monkeypatch.setattr(settings, "REVIEW_QUEUE_TARGET", 1)
    monkeypatch.setattr(settings, "APPROVED_ALERT_THRESHOLD", 0)

    summary = await cron.run_generation(db)

    assert "no_approved_pool" in summary["alerts_sent"]
    # Topup still ran.
    assert summary["generated"] >= 1
    # At least one alert message contains "no approved pool" wording.
    assert any("no approved pool" in m for m in mock_alert)


async def test_run_generation_alerts_when_approved_low(
    db, mock_wikipedia, mock_provider, mock_alert, monkeypatch
):
    """Approved count below APPROVED_ALERT_THRESHOLD at end of run -> alert."""
    db.add(_approved_pool(external_id="a1"))
    db.commit()

    monkeypatch.setattr(settings, "REVIEW_QUEUE_TARGET", 1)
    # Threshold high so 1 approved row is "low" (and after scheduling,
    # approved count goes to 0 anyway).
    monkeypatch.setattr(settings, "APPROVED_ALERT_THRESHOLD", 3)

    summary = await cron.run_generation(db)
    assert "approved_low" in summary["alerts_sent"]


async def test_run_generation_continues_when_generation_fails(
    db, mock_wikipedia, mock_provider, mock_alert, monkeypatch
):
    """If generate_one_pool_fact raises GenerationFailed mid-topup, log and
    break — DON'T bail the whole cron. Partial progress is still progress."""
    from app.model_provider import ModelProviderError

    db.add(_approved_pool(external_id="a1"))
    db.commit()

    monkeypatch.setattr(settings, "REVIEW_QUEUE_TARGET", 5)
    monkeypatch.setattr(settings, "APPROVED_ALERT_THRESHOLD", 0)

    # Force every provider call to fail -> GenerationFailed every iter.
    mock_provider["fact_text"] = ModelProviderError("simulated provider down")

    summary = await cron.run_generation(db)

    # Tomorrow scheduled OK from the seed approved row.
    assert summary["scheduled"] is not None
    # Topup tried but failed every time.
    assert summary["generation_failures"] >= 1
    # Run completed without raising.
    assert "finished_at" in summary


async def test_run_generation_idempotent_when_already_scheduled(
    db, mock_wikipedia, mock_provider, mock_alert, monkeypatch
):
    """If tomorrow is already scheduled, schedule -> 'already_scheduled_or_race',
    topup still runs."""
    tomorrow = date.today() + timedelta(days=1)
    db.add(_fact(scheduled_date=tomorrow, external_id="pre-existing"))
    db.commit()

    monkeypatch.setattr(settings, "REVIEW_QUEUE_TARGET", 1)
    monkeypatch.setattr(settings, "APPROVED_ALERT_THRESHOLD", 0)

    summary = await cron.run_generation(db)

    assert summary["scheduled"] == "already_scheduled_or_race"
    assert summary["generated"] >= 1


# --- run_push ---------------------------------------------------------------


def test_run_push_success_marks_pushed_at(db, mock_fcm):
    today = date.today()
    db.add(_fact(scheduled_date=today, external_id="ex-today"))
    db.commit()

    result = cron.run_push(db)

    assert result is not None
    assert result["message_id"] == mock_fcm["message_id"]
    assert result["pushed_at"] is not None

    # Most-recent-wins: pushed_at is now set on the row.
    fact = db.query(Fact).filter(Fact.external_id == "ex-today").one()
    assert fact.pushed_at is not None

    # FCM was called with topic + correct body.
    assert len(mock_fcm["calls"]) == 1
    call = mock_fcm["calls"][0]
    assert call["topic"] == settings.FCM_TOPIC
    assert call["title"] == "HistoryBites"


def test_run_push_returns_none_and_alerts_when_no_active_fact(
    db, mock_fcm, mock_alert
):
    """No fact for today -> no push, alert fires, return None.
    NOT the same as /today's stale fallback — push is strict."""
    result = cron.run_push(db)

    assert result is None
    assert mock_fcm["calls"] == []  # FCM never called
    # Alert message specifically about run_push skipping.
    assert any("run_push skipped" in m for m in mock_alert)


def test_run_push_excludes_retracted(db, mock_fcm, mock_alert):
    """A retracted today's fact shouldn't be pushed."""
    today = date.today()
    db.add(_fact(scheduled_date=today, external_id="ex-r", is_retracted=True))
    db.commit()

    result = cron.run_push(db)
    assert result is None
    assert mock_fcm["calls"] == []


# --- CLI dispatch -----------------------------------------------------------


def test_cli_unknown_command_exits_2(capsys):
    """Bad command -> exit code 2 (argparse-style 'misuse')."""
    rc = cron._main(["app.cron", "frobnicate"])
    assert rc == 2


def test_cli_no_args_exits_2(capsys):
    rc = cron._main(["app.cron"])
    assert rc == 2


def test_cli_run_push_returns_0_on_success(monkeypatch, mock_fcm):
    """python -m app.cron run_push -> exit 0 even when there's no fact
    (run_push handles that internally, returns None, doesn't raise)."""
    rc = cron._main(["app.cron", "run_push"])
    # No fact for today -> alerts fired but rc=0 because no exception.
    assert rc == 0


def test_cli_run_push_returns_1_on_fcm_error(monkeypatch, db, mock_fcm):
    """python -m app.cron run_push -> exit 1 if FCMError leaks out."""
    today = date.today()
    db.add(_fact(scheduled_date=today, external_id="ex-today"))
    db.commit()

    mock_fcm["message_id"] = fcm.FCMError("simulated FCM outage")

    rc = cron._main(["app.cron", "run_push"])
    assert rc == 1


def test_cli_run_generation_returns_0(
    monkeypatch, mock_wikipedia, mock_provider, mock_alert
):
    """python -m app.cron run_generation -> exit 0 on normal completion."""
    monkeypatch.setattr(settings, "REVIEW_QUEUE_TARGET", 1)
    monkeypatch.setattr(settings, "APPROVED_ALERT_THRESHOLD", 0)
    rc = cron._main(["app.cron", "run_generation"])
    assert rc == 0


def test_cli_run_generation_returns_1_on_unhandled_exception(monkeypatch):
    """If run_generation raises something it didn't catch, the CLI returns 1
    so Railway surfaces the failure. Simulate by patching run_generation
    directly to raise RuntimeError."""

    async def _boom(session):
        raise RuntimeError("simulated catastrophic failure")

    monkeypatch.setattr(cron, "run_generation", _boom)
    rc = cron._main(["app.cron", "run_generation"])
    assert rc == 1


def test_cli_run_generation_crash_alert_does_not_leak_repr(monkeypatch):
    """Code Review Fix 3 (P3.3): the cron crash handler used to send
    `repr(exc)` to the Slack webhook, which for an OperationalError leaks
    the DB hostname / port into the alert channel. The fix sends only the
    exception type name; the full traceback stays in Railway via
    logger.exception (which renders correctly after Fix 3 P2.1)."""
    sent_alerts: list[str] = []
    monkeypatch.setattr(cron, "send_alert", lambda msg: sent_alerts.append(msg))

    async def _operational_error(session):
        from sqlalchemy.exc import OperationalError

        raise OperationalError(
            "SELECT 1",
            params={},
            orig=Exception(
                "connection to server at \"db.internal.example\" "
                "(123.45.67.89), port 12345 failed"
            ),
        )

    monkeypatch.setattr(cron, "run_generation", _operational_error)
    rc = cron._main(["app.cron", "run_generation"])

    assert rc == 1
    assert len(sent_alerts) == 1
    msg = sent_alerts[0]

    # Type name + sentinel are present
    assert "OperationalError" in msg
    assert "see Railway logs" in msg

    # Concrete leak strings must NOT reach Slack
    assert "db.internal.example" not in msg
    assert "12345" not in msg
    assert "SELECT 1" not in msg
