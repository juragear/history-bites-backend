"""Unit tests for app.fcm._get_firebase_app — Code Review Fix 3 (P3.1 +
Fix-2-deferred-P3.2): exception hygiene + duplicate-init race recovery.

Pre-fix shape (audited in Chunk 1 P3.1 / Chunk 2 P3.2 / Chunk 3 P3.1):
  - Only JSONDecodeError was wrapped in FCMError.
  - credentials.Certificate(...) raised bare ValueError on malformed SA.
  - firebase_admin.initialize_app(...) raised bare ValueError on the
    check-then-set race window (two threads both pass `if _app is None`).

Fix 3 wraps the cert + init block to:
  1. Wrap any ValueError / Exception in FCMError with a scrubbed message
     ("firebase init failed; see server logs for details").
  2. Recover from the duplicate-init race via firebase_admin.get_app().
  3. Log only `error_type`, never `str(exc)` — full chain travels via the
     `from exc` cause to upstream logger.exception (now actually rendered
     into JSON logs after Fix 3 P2.1).

These tests pin all three contracts. They mock `credentials.Certificate`
and `firebase_admin.initialize_app` / `get_app` rather than `_get_firebase_app`
itself — the constraint is to test the function's behaviour, not stub it
out.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest


def _reset_app_global():
    """Reset the module-level _app singleton between tests so each test
    exercises the lazy-init path. firebase_admin's own `_apps` dict is also
    sticky; tests that need a clean slate also clear that."""
    import app.fcm as fcm_module

    fcm_module._app = None


def test_get_firebase_app_wraps_bad_cert_in_fcm_error(monkeypatch):
    """Code Review Fix 3 (P3.1): malformed SA dict -> FCMError, not bare
    ValueError. The conftest fixture's minimal `{"project_id": "test-project"}`
    is missing the required private_key/client_email fields, so the real
    credentials.Certificate(...) raises ValueError on it — exactly the
    pre-fix leak path."""
    import app.fcm as fcm_module

    _reset_app_global()

    with pytest.raises(fcm_module.FCMError) as exc_info:
        fcm_module._get_firebase_app()

    # Sentinel message — does NOT contain the bare ValueError details
    assert "see server logs" in str(exc_info.value)
    # `from exc` chain preserves the original for upstream logger.exception
    assert exc_info.value.__cause__ is not None
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_get_firebase_app_log_records_error_type_not_message(monkeypatch, caplog):
    """Code Review Fix 3 (P3.1): the warning log records `error_type` only.
    Without this scrubbing posture, an exception message containing the
    project_id (or worse, parts of the SA dict) would leak into Railway
    logs by way of the warning's extras."""
    import app.fcm as fcm_module

    _reset_app_global()

    # Pick a project_id that's distinctive so we can assert it's not in the
    # log line — if a future iteration adds `error=str(exc)` to the warning
    # extras, this test catches it.
    monkeypatch.setattr(
        fcm_module.settings,
        "FIREBASE_SERVICE_ACCOUNT_JSON",
        '{"type": "service_account", "project_id": "leakable-project-id-here"}',
    )

    with caplog.at_level(logging.WARNING, logger="app.fcm"):
        with pytest.raises(fcm_module.FCMError):
            fcm_module._get_firebase_app()

    # The warning record's extras carry the type name, nothing else
    warning_records = [
        r for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert len(warning_records) >= 1, "Expected at least one warning log"

    # The leakable content must NOT appear in any rendered log message
    log_text = caplog.text
    assert "leakable-project-id-here" not in log_text


def test_get_firebase_app_wraps_json_decode_in_fcm_error(monkeypatch):
    """Code Review Fix 3 (P3.1): JSONDecodeError -> FCMError with sentinel
    message. The pre-fix code wrapped this case but included the JSON
    parser's exception message verbatim (column / line numbers, byte
    offsets); the new shape is a fixed sentinel."""
    import app.fcm as fcm_module

    _reset_app_global()
    monkeypatch.setattr(
        fcm_module.settings,
        "FIREBASE_SERVICE_ACCOUNT_JSON",
        "not valid json {{{",
    )

    with pytest.raises(fcm_module.FCMError) as exc_info:
        fcm_module._get_firebase_app()

    assert "see server logs" in str(exc_info.value)
    # Original JSON error preserved on the cause chain
    assert exc_info.value.__cause__ is not None


def test_get_firebase_app_recovers_from_duplicate_init_race(monkeypatch):
    """Code Review Fix 3 (P3.1 + Fix-2-deferred-P3.2): the duplicate-init
    race recovery. Simulates two threads concurrently passing
    `if _app is None`: the first reaches initialize_app() successfully; the
    second's initialize_app() raises ValueError because firebase_admin's
    own `_apps` dict already has a default entry. The fix recovers via
    `firebase_admin.get_app()` and returns the existing instance."""
    import app.fcm as fcm_module

    _reset_app_global()

    # Stub the cert validator so we don't need a real PEM
    fake_cred = SimpleNamespace()
    monkeypatch.setattr(
        fcm_module.credentials, "Certificate", lambda d: fake_cred
    )

    # First initialize_app call would normally succeed; we'll skip it by
    # pre-setting a fake "existing app" and forcing initialize_app to raise.
    fake_existing_app = SimpleNamespace(name="[DEFAULT]")

    def _initialize_app_already_exists(cred):
        raise ValueError(
            "the default Firebase app already exists. This means you "
            "called initialize_app() more than once without providing an "
            "app name as the second argument."
        )

    monkeypatch.setattr(
        fcm_module.firebase_admin,
        "initialize_app",
        _initialize_app_already_exists,
    )
    monkeypatch.setattr(
        fcm_module.firebase_admin,
        "get_app",
        lambda: fake_existing_app,
    )

    # Should NOT raise — race recovery kicks in
    result = fcm_module._get_firebase_app()
    assert result is fake_existing_app


def test_get_firebase_app_race_recovery_falls_through_when_no_default_app(
    monkeypatch,
):
    """Edge case: ValueError from initialize_app might NOT be the race —
    it could be a malformed cred (a bare ValueError from a different
    cause). In that case `get_app()` also raises ValueError (no default
    app exists), and the function should fall through to FCMError rather
    than masking the real init failure."""
    import app.fcm as fcm_module

    _reset_app_global()

    fake_cred = SimpleNamespace()
    monkeypatch.setattr(
        fcm_module.credentials, "Certificate", lambda d: fake_cred
    )
    monkeypatch.setattr(
        fcm_module.firebase_admin,
        "initialize_app",
        lambda cred: (_ for _ in ()).throw(ValueError("genuinely broken cred")),
    )
    monkeypatch.setattr(
        fcm_module.firebase_admin,
        "get_app",
        lambda: (_ for _ in ()).throw(ValueError("no default app")),
    )

    with pytest.raises(fcm_module.FCMError) as exc_info:
        fcm_module._get_firebase_app()

    assert "see server logs" in str(exc_info.value)
    # The ORIGINAL initialize_app ValueError chains via `from exc`, not the
    # secondary get_app failure
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "genuinely broken cred" in str(exc_info.value.__cause__)
