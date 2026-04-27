import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.admin import router as admin_router
from app.config import settings
from app.db import SessionLocal, get_db
from app.models import Fact, PoolFact
from app.schemas import (
    ArchiveItem,
    ArchiveResponse,
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
        return json.dumps(payload)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL.upper())
    root.handlers = [handler]


configure_logging()

logger = logging.getLogger(__name__)

app = FastAPI(title="HistoryBites backend")

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


@app.on_event("startup")
def on_startup() -> None:
    logger.info("app startup", extra={"extra": {"environment": settings.ENVIRONMENT}})


def _fact_to_today(row: Fact, *, is_stale: bool) -> TodayResponse:
    return TodayResponse(
        scheduled_date=row.scheduled_date,
        fact=row.fact_text,
        source_url=row.source_url,
        source_name=row.source_name,
        source_license=row.source_license,
        is_stale=is_stale,
    )


@app.get("/today", response_model=TodayResponse)
def today(db: Annotated[Session, Depends(get_db)]) -> TodayResponse:
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


@app.get("/archive", response_model=ArchiveResponse)
def archive(
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> ArchiveResponse:
    rows = (
        db.execute(
            select(Fact)
            .where(Fact.is_retracted.is_(False))
            .order_by(Fact.scheduled_date.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    items = [
        ArchiveItem(
            scheduled_date=r.scheduled_date,
            fact=r.fact_text,
            source_url=r.source_url,
            source_name=r.source_name,
            source_license=r.source_license,
        )
        for r in rows
    ]
    return ArchiveResponse(items=items, count=len(items))


def _approved_status(approved_count: int) -> str:
    """D8 three-tier mapping (surfaced on /health in Step 14).

    >= APPROVED_TARGET   -> 'ok'    (target buffer met; cron in steady state)
    >= ALERT_THRESHOLD   -> 'warm'  (below target but not paging; cron tops up)
    <  ALERT_THRESHOLD   -> 'low'   (below alert floor; Slack alert fires)
    """
    if approved_count >= settings.APPROVED_TARGET:
        return "ok"
    if approved_count >= settings.APPROVED_ALERT_THRESHOLD:
        return "warm"
    return "low"


@app.get("/health", response_model=HealthResponse)
def health(response: Response) -> HealthResponse:
    try:
        with SessionLocal() as db:
            pending = db.execute(
                select(func.count())
                .select_from(PoolFact)
                .where(PoolFact.status == "pending_review")
            ).scalar_one()
            approved = db.execute(
                select(func.count())
                .select_from(PoolFact)
                .where(PoolFact.status == "approved")
            ).scalar_one()
            latest = db.execute(select(func.max(Fact.scheduled_date))).scalar_one()
            # Step 9: most-recent-wins MAX(pushed_at) — single scalar query
            # against the same Session, no extra round trip beyond the one
            # we'd already need. Small table, no index required.
            last_push_at = db.execute(
                select(func.max(Fact.pushed_at))
            ).scalar_one()
    except SQLAlchemyError as exc:
        logger.warning("health db probe failed", extra={"extra": {"error": str(exc)}})
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(
            status="degraded",
            db="down",
            pool_pending_count=0,
            pool_approved_count=0,
            approved_status="unknown",
            latest_scheduled_date=None,
            last_push_at=None,
        )

    return HealthResponse(
        status="ok",
        db="ok",
        pool_pending_count=pending,
        pool_approved_count=approved,
        approved_status=_approved_status(approved),
        latest_scheduled_date=latest,
        last_push_at=last_push_at,
    )
