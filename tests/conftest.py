"""Test fixtures for HistoryBites backend.

Sets up an isolated in-memory SQLite database, monkeypatches every external
boundary (Wikipedia, model provider, FCM, alert webhook), and yields a
TestClient over the FastAPI app for integration tests.

Required env vars are seeded BEFORE app modules are imported so
pydantic-settings boots cleanly without a real .env. The runtime uses
StaticPool plus a shared connection so multiple SessionLocal() calls in the
same test see each other's writes — needed because /health calls SessionLocal
directly while requests use the dependency-overridden version.
"""
from __future__ import annotations

import os

# pydantic-settings reads env at import time. Set placeholders BEFORE the app
# package is imported so Settings() doesn't crash on missing required vars.
# `setdefault` so a developer can still override (e.g. point at a real DB).
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault(
    "WIKIPEDIA_USER_AGENT", "HistoryBitesTests/0.0 (test@example.com)"
)
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault(
    "FIREBASE_SERVICE_ACCOUNT_JSON", '{"project_id": "test-project"}'
)
os.environ.setdefault("MODEL_PROVIDER", "gemini")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("ALERT_WEBHOOK_URL", "")  # send_alert path: "no URL"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import BigInteger, create_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import cron as app_cron  # noqa: E402
from app import db as app_db  # noqa: E402
from app import main as app_main  # noqa: E402
from app.db import Base, get_db  # noqa: E402


# SQLite quirk: BigInteger columns compile to BIGINT, which is NOT a rowid
# alias — so autoincrement on a BigInteger primary key silently fails and
# you get NOT NULL constraint violations on insert. Postgres uses bigserial
# and is fine. The fix for tests is to compile BigInteger as INTEGER on
# SQLite, which is the rowid alias and auto-increments. Production is
# unaffected because this hook only fires for the "sqlite" dialect.
@compiles(BigInteger, "sqlite")
def _bigint_as_integer_on_sqlite(element, compiler, **kw):  # pragma: no cover
    return "INTEGER"


# StaticPool + a single shared connection: every SessionLocal() call returns
# a session bound to the SAME underlying SQLite connection, so writes done in
# the test body are visible to the API code inside a request, and vice versa.
# Without this, /health (which opens its own SessionLocal) would see an empty
# database even after the test fixture inserted rows.
_TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSessionLocal = sessionmaker(
    bind=_TEST_ENGINE,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


# Replace the engine + SessionLocal everywhere they're referenced. /health
# imported SessionLocal directly into app.main's namespace, and the CLI
# entrypoint in app.cron does the same — `from app.db import SessionLocal`
# rebinds the name into each module's globals at import time, which means
# patching app.db.SessionLocal alone doesn't reach those copies.
app_db.engine = _TEST_ENGINE
app_db.SessionLocal = _TestSessionLocal
app_main.SessionLocal = _TestSessionLocal
app_cron.SessionLocal = _TestSessionLocal


def _override_get_db():
    db = _TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


app_main.app.dependency_overrides[get_db] = _override_get_db


# --- per-test reset ---------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state():
    """Recreate tables before every test, drop after.

    Also clears the in-memory /today cache so cache hits from a prior test
    don't bleed into the current one (the cache key is just an ISO date, so
    two tests using "today" would collide).
    """
    Base.metadata.create_all(_TEST_ENGINE)
    app_main._today_cache.clear()
    yield
    Base.metadata.drop_all(_TEST_ENGINE)


# --- core fixtures ----------------------------------------------------------


@pytest.fixture
def db():
    """A session bound to the shared in-memory engine.

    Use this fixture to seed rows directly in test setup. Commits are visible
    to the API request because StaticPool gives both sides the same connection.
    """
    session = _TestSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    """TestClient over the FastAPI app with get_db overridden."""
    return TestClient(app_main.app)


@pytest.fixture
def admin_token() -> str:
    """The token configured for the test environment.

    Centralized so tests don't hard-code the literal — if we change the env
    placeholder, only this fixture needs to update.
    """
    from app.config import settings

    return settings.ADMIN_TOKEN


# --- external-boundary fixtures ---------------------------------------------


@pytest.fixture
def mock_wikipedia(monkeypatch):
    """Stub wikipedia.list_candidates / fetch_extract.

    Returns a state dict so individual tests can mutate the candidate list or
    inject per-title overrides:
      state["candidates"] -> list of Candidate
      state["extract_overrides"][title] -> ArticleExtract OR Exception instance
    """
    from app import wikipedia

    state = {
        "candidates": [
            wikipedia.Candidate(page_id=1001, title="Article One"),
            wikipedia.Candidate(page_id=1002, title="Article Two"),
            wikipedia.Candidate(page_id=1003, title="Article Three"),
        ],
        "extract_overrides": {},
    }

    async def _list_candidates(category):
        return list(state["candidates"])

    async def _fetch_extract(title):
        override = state["extract_overrides"].get(title)
        if isinstance(override, Exception):
            raise override
        if override is not None:
            return override
        cand = next((c for c in state["candidates"] if c.title == title), None)
        page_id = cand.page_id if cand else 9999
        # Step 13e: mock extracts must satisfy MIN_EXTRACT_CHARS (1500) and
        # _looks_infoboxy's paragraph-density check (>=30% of paragraphs are
        # >=200 chars). Two long narrative paragraphs (>200 chars each)
        # totalling >1500 chars satisfies both. Title is interpolated so the
        # extract still varies per candidate, which the side-effect test
        # cases rely on for distinguishability.
        narrative = (
            f"{title} is a test article about an obscure historical topic, "
            "constructed for the integration test suite to exercise the "
            "pre-filter and generation pipeline as Step 13e shipped them. "
            "The first paragraph is intentionally long enough to cross the "
            "two-hundred character threshold that the infobox-shape detector "
            "uses to distinguish narrative prose from infobox fragments, and "
            "the entire mocked extract is deliberately wide enough to clear "
            "the fifteen-hundred character floor that gates thin articles. "
            "Without these properties the integration tests would all skip "
            "via the new no-budget-cost pre-filter paths and look like silent "
            "broken instead of like accurate happy-path coverage."
            "\n\n"
            f"In a second long paragraph, {title} continues with additional "
            "fictional historical context. The mock pretends there were "
            "consequences in the following century, that the practice spread "
            "to neighbouring polities, and that scholarly debate has shifted "
            "over the last fifty years. None of this is real history; it "
            "exists only so the section-aware truncation does not produce a "
            "stub-shaped fixture that the new pre-filter would correctly "
            "reject under realistic conditions, and so the model-provider "
            "stub has enough material to plausibly generate a single fact."
            "\n\n"
            "A third paragraph extends the narrative further so the mock "
            "comfortably clears the fifteen-hundred character floor without "
            "depending on the precise wording of the first two. This block "
            "describes hypothetical archaeological work, a hypothetical "
            "modern dispute over interpretation, and the kind of ancillary "
            "context that real Wikipedia articles include in their second "
            "and third sections after the lead paragraph closes."
        )
        return wikipedia.ArticleExtract(
            page_id=page_id,
            title=title,
            extract=narrative,
            source_url=f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
        )

    monkeypatch.setattr(wikipedia, "list_candidates", _list_candidates)
    monkeypatch.setattr(wikipedia, "fetch_extract", _fetch_extract)
    return state


@pytest.fixture
def mock_provider(monkeypatch):
    """Stub model_provider.get_provider with a controllable fake.

    state["fact_text"] is the canned reply. Set to:
      - a string         -> returned as-is
      - an Exception     -> raised
      - a callable       -> called with the article extract, return value used
    state["calls"] tracks invocation count for assertions.

    Step 14: also patches `app.generation._judge` with a FakeJudge that
    returns a configurable JudgeResult. Default is borderline (score=3.5)
    so existing tests that assert `status == 'pending_review'` keep
    passing without modification. Tests that want auto_approve / auto_reject
    behaviour mutate state["judge_score"] (or judge_verdict / judge_reason
    explicitly) to override.

    state["judge_error"] override: set to an Exception instance to force
    the judge to raise that exception (exercises the
    judge_failed_routing_to_human path).
    """
    from app import generation, model_provider
    from app.judge import JudgeResult

    state = {
        "fact_text": (
            "On April 25, 1859, the test event happened in Testland, marking "
            "a notable moment in fictional historiography."
        ),
        "calls": 0,
        # Step 14 judge state. Defaults to borderline so pre-Step-14 tests
        # that assert status='pending_review' continue passing.
        "judge_score": 3.5,
        "judge_verdict": "borderline",
        "judge_reason": "test fixture default — borderline routes to review",
        "judge_calls": 0,
        "judge_error": None,
    }

    class _FakeProvider:
        async def extract_fact(self, article_extract: str) -> str:
            state["calls"] += 1
            ft = state["fact_text"]
            if isinstance(ft, Exception):
                raise ft
            if callable(ft):
                return ft(article_extract)
            return ft

    class _FakeJudge:
        async def evaluate(self, article_extract, fact_text):
            state["judge_calls"] += 1
            err = state.get("judge_error")
            if err is not None:
                # Mirror the real provider-failure path: raise JudgeError so
                # generate_one_pool_fact's except clause routes to review.
                from app.judge import JudgeError
                raise JudgeError(str(err)) if not isinstance(err, Exception) else err
            return JudgeResult(
                score=state["judge_score"],
                verdict=state["judge_verdict"],
                reason=state["judge_reason"],
            )

    fake_factory = lambda: _FakeProvider()  # noqa: E731
    monkeypatch.setattr(model_provider, "get_provider", fake_factory)
    # generation.py did `from app.model_provider import get_provider`, which
    # rebinds the name into generation's namespace. Patch that copy too.
    monkeypatch.setattr(generation, "get_provider", fake_factory)
    # Inject the fake judge directly into the lazy module global. The real
    # _get_judge() helper checks for None before constructing, so a non-None
    # value here means tests bypass the real Judge() entirely.
    monkeypatch.setattr(generation, "_judge", _FakeJudge())
    return state


@pytest.fixture
def mock_fcm(monkeypatch):
    """Stub fcm.send_to_topic so tests don't hit Firebase.

    state["calls"] is a list of dicts capturing each invocation.
    state["message_id"] is what gets returned (or raised, if it's an Exception).
    """
    from app import fcm

    state = {
        "calls": [],
        "message_id": "projects/test/messages/0:fake-msg-id",
    }

    def _send(*, topic, title, body, data):
        state["calls"].append(
            {"topic": topic, "title": title, "body": body, "data": dict(data)}
        )
        mid = state["message_id"]
        if isinstance(mid, Exception):
            raise mid
        return mid

    monkeypatch.setattr(fcm, "send_to_topic", _send)
    return state


@pytest.fixture
def mock_alert(monkeypatch):
    """Capture cron.send_alert calls without hitting Slack.

    Returns a list. Each appended entry is the alert message string.
    """
    from app import cron

    calls: list[str] = []

    def _send_alert(message: str) -> None:
        calls.append(message)

    monkeypatch.setattr(cron, "send_alert", _send_alert)
    return calls
