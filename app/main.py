import json
import logging
import re
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Response,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import wikipedia
from app.admin import (
    admin_unauth_router,
    router as admin_router,
)
from app.config import settings
from app.db import SessionLocal, get_db
from app.models import Fact
from app.schemas import (
    ArchiveItem,
    ArchiveResponse,
    ErrorDetail,
    HealthResponse,
    TodayResponse,
)


CACHE_TTL = timedelta(seconds=300)


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = record.__dict__.get("extra")
        if isinstance(extra, dict):
            payload.update(extra)

        # Code Review Fix 3 (P2.1): render exception info when logger.exception(...)
        # was called or exc_info=True was passed. Without this, the formatter
        # silently drops the traceback that the call site explicitly asked for —
        # `admin.py:725` (admin_run_generation broad except) and `cron.py:344`
        # (cron CLI catch-all) both rely on logger.exception for production
        # diagnostics; before this fix neither produced a stack trace in Railway
        # logs. exc_type / exc_message are split out as separate fields for
        # search ergonomics; traceback carries the full multi-line render via
        # the parent class's formatException helper.
        if record.exc_info:
            exc_type, exc_value, _exc_tb = record.exc_info
            if exc_type is not None:
                payload["exc_type"] = exc_type.__name__
                payload["exc_message"] = str(exc_value) if exc_value else ""
                payload["traceback"] = self.formatException(record.exc_info)

        # `default=str` is a safety net for non-JSON-serializable values that
        # might land in `extra` (e.g. a datetime, a Decimal). Without it, a
        # stray non-serializable extra would crash the formatter mid-emit and
        # take the surrounding log line with it.
        return json.dumps(payload, default=str)


# Code Review Fix 1 (P2.2): match `?` followed by anything up to the next
# whitespace. Strips query strings out of uvicorn access-log request lines
# (and only those — application logs go through the JSON formatter on the
# root logger, which doesn't apply this transform).
_QUERY_STRING_PATTERN = re.compile(r"\?[^\s]*")


class StripQueryStringFormatter(logging.Formatter):
    """Uvicorn access-log formatter that strips `?...` from the path arg.

    Defense against query-string secrets (?token=..., ?api_key=..., etc.)
    landing in stdout / Railway logs verbatim. Without this, hitting
    `/admin/review?token=<value>` from a browser causes uvicorn's default
    access logger to log the full URL including the token, which Railway
    captures into log retention.

    Verified against uvicorn 0.30 series — the access logger calls
    `info('%s - "%s %s HTTP/%s" %d', client_addr, method, full_path,
    http_version, status)` so `record.args[2]` is the path-with-query
    string. If a future uvicorn version reorders args, this falls through
    silently (the isinstance + length guards stay safe) and would need an
    index update; simpler than scanning every arg.
    """

    def format(self, record: logging.LogRecord) -> str:
        if record.args and len(record.args) >= 3:
            args_list = list(record.args)
            full_path = args_list[2]
            if isinstance(full_path, str):
                args_list[2] = _QUERY_STRING_PATTERN.sub("", full_path)
                record.args = tuple(args_list)
        return super().format(record)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL.upper())
    root.handlers = [handler]

    # Code Review Fix 1 (P2.2): override uvicorn.access so the logged request
    # line never contains a query string. We replace the default handler with
    # one that uses StripQueryStringFormatter, then disable propagation so the
    # access record doesn't ALSO go to root (which would re-emit it via the
    # JSON handler with the original args still attached — note that the
    # strip mutates record.args in place, so by the time the root handler
    # would see it, args are clean too; but propagation off is cleaner).
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = []
    access_handler = logging.StreamHandler(sys.stdout)
    access_handler.setFormatter(
        StripQueryStringFormatter(
            '%(asctime)s %(levelname)s uvicorn.access :: %(message)s'
        )
    )
    access_logger.addHandler(access_handler)
    access_logger.propagate = False


configure_logging()

logger = logging.getLogger(__name__)


# Code Review Fix 4 (P3.3): migrate from the deprecated `@app.on_event(...)`
# decorators to FastAPI's lifespan context manager (FastAPI ≥ 0.93).
# Startup is empty by design — every initialization in this codebase is
# lazy (Firebase, Gemini, Wikipedia client). Shutdown calls
# `wikipedia.aclose()` so the module-level httpx singleton drains its
# connection pool gracefully on Railway pod restart instead of having
# in-flight requests torn down ungracefully when uvicorn receives SIGTERM.
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "lifespan: startup", extra={"extra": {"environment": settings.ENVIRONMENT}}
    )
    yield
    await wikipedia.aclose()
    logger.info("lifespan: shutdown — wikipedia client closed")


app = FastAPI(title="HistoryBites backend", lifespan=lifespan)

# Step 12: CORS for any future browser-based admin/dashboard. Bearer-token
# auth in headers doesn't need cookies, so allow_credentials stays False —
# that's also what makes the wildcard "*" actually work (browsers reject
# `*` + credentials=true). Origin list comes from settings.CORS_ORIGINS as a
# comma-separated string so it can be configured via env without code changes.
_cors_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(admin_router)
# Code Review Fix 6 (2026-04-29; replaces Fix 1's `admin_review_page_router`):
# routes that handle their own auth (GET /admin/review redirects to login on
# miss; GET + POST /admin/login bootstrap the session cookie). Everything
# auth-required stays on `admin_router` above.
app.include_router(admin_unauth_router)


# Code Review Fix 4 (P2.1): public endpoints live on a versioned router.
# Mobile Architecture commits to `/v1/` for Phase 2 F2 — this is that
# implementation. Future breaking changes to the public contract bump the
# prefix (e.g. `/v2/today`) without disturbing deployed Flutter clients;
# non-breaking additions stay under `/v1/`. Admin endpoints stay
# unversioned at `/admin/*` because they're internal — no client to break.
public_router = APIRouter(prefix="/v1")


# D21c: date-keyed in-memory cache for /today. Keyed by today's ISO date so a
# midnight rollover naturally evicts stale entries (old key never gets looked
# up again). Writes to facts for a given date bust that specific entry via
# invalidate_today_cache().
_today_cache: dict[str, tuple[TodayResponse, datetime]] = {}


def _cache_get(key: str) -> TodayResponse | None:
    entry = _today_cache.get(key)
    if entry is None:
        return None
    value, expires_at = entry
    if datetime.now(timezone.utc) >= expires_at:
        _today_cache.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: TodayResponse) -> None:
    expires_at = datetime.now(timezone.utc) + CACHE_TTL
    _today_cache[key] = (value, expires_at)


def invalidate_today_cache(scheduled_date: date) -> None:
    """Drop the cache entry for a specific date.

    Called by the scheduler after inserting a new Fact, and by admin
    retract/schedule endpoints (Step 8). Safe to call when the key isn't
    present.
    """
    key = scheduled_date.isoformat()
    if _today_cache.pop(key, None) is not None:
        logger.info("today cache invalidated", extra={"extra": {"date": key}})


def _fact_to_today(row: Fact, *, is_stale: bool) -> TodayResponse:
    return TodayResponse(
        scheduled_date=row.scheduled_date,
        fact=row.fact_text,
        source_url=row.source_url,
        source_name=row.source_name,
        source_license=row.source_license,
        is_stale=is_stale,
    )


@public_router.get(
    "/today",
    response_model=TodayResponse,
    responses={
        404: {
            "model": ErrorDetail,
            "description": "No fact scheduled and no fallback available.",
        },
    },
)
def today(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> TodayResponse:
    # Code Review Fix 4 (P2.4): surface the in-memory cache TTL on the wire
    # so dio (Flutter's HTTP client) and Cloudflare (D13, Phase 3) can share
    # the same cache window. `public` because the response is the same for
    # every user (D7: same-fact-for-everyone). 300s matches CACHE_TTL above.
    response.headers["Cache-Control"] = "public, max-age=300"

    today_date = date.today()
    key = today_date.isoformat()

    cached = _cache_get(key)
    if cached is not None:
        logger.info("today cache hit", extra={"extra": {"date": key}})
        return cached

    exact = db.execute(
        select(Fact).where(
            Fact.scheduled_date == today_date,
            Fact.is_retracted.is_(False),
        )
    ).scalar_one_or_none()

    if exact is not None:
        resp = _fact_to_today(exact, is_stale=False)
        _cache_put(key, resp)
        logger.info(
            "today served exact",
            extra={"extra": {"date": key, "fact_id": exact.id}},
        )
        return resp

    fallback = db.execute(
        select(Fact)
        .where(
            Fact.scheduled_date <= today_date,
            Fact.is_retracted.is_(False),
        )
        .order_by(Fact.scheduled_date.desc())
        .limit(1)
    ).scalar_one_or_none()

    if fallback is None:
        logger.warning(
            "today has no fact and no fallback",
            extra={"extra": {"date": key}},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no fact available yet",
        )

    resp = _fact_to_today(fallback, is_stale=True)
    _cache_put(key, resp)
    logger.info(
        "today served stale fallback",
        extra={
            "extra": {
                "date": key,
                "fact_id": fallback.id,
                "fallback_date": fallback.scheduled_date.isoformat(),
            }
        },
    )
    return resp


@public_router.get("/archive", response_model=ArchiveResponse)
def archive(
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    before: Annotated[date | None, Query()] = None,
) -> ArchiveResponse:
    """Cursor-paginated archive (Code Review Fix 4 P2.5).

    Order: `scheduled_date DESC`. Stable because `scheduled_date` is UNIQUE
    on the facts table — no ties to break. `?before=<date>` walks older
    pages (open interval, `scheduled_date < before`); the response's
    `next_before` is the cursor for the next call, or `null` on the final
    page.

    Implementation note: fetching `limit + 1` rows in one query is the
    cheap way to compute `has_more` without a separate `COUNT(*)` round
    trip. Slice off the extra row before serializing; use it only to set
    `next_before`.
    """
    query = (
        select(Fact)
        .where(Fact.is_retracted.is_(False))
        .order_by(Fact.scheduled_date.desc())
    )
    if before is not None:
        query = query.where(Fact.scheduled_date < before)

    rows = list(db.execute(query.limit(limit + 1)).scalars())
    has_more = len(rows) > limit
    page = rows[:limit]

    items = [
        ArchiveItem(
            scheduled_date=r.scheduled_date,
            fact=r.fact_text,
            source_url=r.source_url,
            source_name=r.source_name,
            source_license=r.source_license,
        )
        for r in page
    ]
    next_before = page[-1].scheduled_date if has_more and page else None
    return ArchiveResponse(items=items, next_before=next_before)


@public_router.get(
    "/health",
    response_model=HealthResponse,
    responses={
        503: {
            "model": ErrorDetail,
            "description": "Database probe failed; service is degraded.",
        },
    },
)
def health(response: Response) -> HealthResponse:
    """Public connectivity probe (Code Review Fix 4 P2.3).

    Status only — no operational metrics. The rich operational view (pool
    counts, scheduling runway, last push time) moved to
    `/admin/cron/status`, which is admin-token-gated. Anyone can hit
    /v1/health without auth, so it stays minimal: connectivity for
    Flutter's HTTP client + Railway's healthcheck plumbing.

    Uses `SessionLocal()` directly (not `Depends(get_db)`) so the existing
    503-on-DB-outage test can monkeypatch this module's `SessionLocal`
    binding to simulate connection failure at session-creation time. The
    rest of the codebase that needs `get_db()` semantics keeps using the
    dependency.
    """
    try:
        with SessionLocal() as db:
            # Trivial scalar query — round-trips the connection without
            # depending on the data shape (works against an empty DB).
            db.execute(select(Fact.id).limit(1)).first()
    except Exception as exc:  # SQLAlchemyError + dialect-level errors
        # Code Review Fix 3 (P2.1) traceback rendering carries the full
        # chain into Railway via logger.exception elsewhere; here a
        # warning with the type name is enough to scope the alert.
        logger.warning(
            "health db probe failed",
            extra={"extra": {"error_type": type(exc).__name__}},
        )
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="degraded", db="down")

    return HealthResponse(status="ok", db="ok")


# Wire the public router last so all decorators above are bound first.
app.include_router(public_router)
