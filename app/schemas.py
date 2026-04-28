"""Pydantic response models for public + admin endpoints.

These are the API contract Phase 2 Flutter consumes. Keep field names stable —
renames here are breaking changes for deployed clients.

Code Review Fix 4 (Pre-F2 contract hygiene):
  - Added `ErrorDetail` as the canonical envelope for non-2xx responses
    declared via `responses=` on each route, so Flutter codegen sees the full
    failure surface (P2.2).
  - `HealthResponse` slimmed to `{status, db}` for the public `/v1/health`
    probe; the rich operational shape moved to `CronStatusResponse` served
    by `/admin/cron/status` (P2.3).
  - `ArchiveResponse` lost `count` (was misleadingly equal to `len(items)`)
    and gained `next_before` for cursor pagination (P2.5).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """Standard error envelope for non-2xx responses across the API.

    Code Review Fix 3 standardised the sentinel detail strings on admin 503
    paths (`"<op> failed; see server logs for details"`). Code Review Fix 4
    declares this shape via `responses=` on each route so OpenAPI consumers
    (Flutter codegen, generated client types, manual readers of /openapi.json)
    see the full failure surface, not just 200 + 422.
    """

    detail: str


class TodayResponse(BaseModel):
    scheduled_date: date
    fact: str
    source_url: str
    source_name: str
    source_license: str
    is_stale: bool = Field(
        description=(
            "True when the returned row's scheduled_date is before today — "
            "the app can show a 'not quite today's fact' banner."
        )
    )


class ArchiveItem(BaseModel):
    """Archive rows are always delivered content, so no is_stale field."""

    scheduled_date: date
    fact: str
    source_url: str
    source_name: str
    source_license: str


class ArchiveResponse(BaseModel):
    """Cursor-paginated archive (Code Review Fix 4 P2.5).

    `next_before` is the cursor for the next page: pass it back as
    `?before=<date>` to fetch older facts. `null` when this is the final
    page. The cursor is the last item's `scheduled_date`, exploiting the
    UNIQUE constraint on facts.scheduled_date for stable ordering.
    """

    items: list[ArchiveItem]
    next_before: date | None = Field(
        description=(
            "Cursor for the next page. Null when this is the final page. "
            "Pass back as ?before=<date> to fetch older facts."
        )
    )


class HealthResponse(BaseModel):
    """Public health probe (Code Review Fix 4 P2.3).

    Status only — no operational metrics. Anyone can hit /v1/health
    without auth, so the response stays minimal: connectivity check for
    Flutter's HTTP client and Railway's healthcheck plumbing. The rich
    operational view (pool counts, scheduling runway, last push time)
    moved to /admin/cron/status which is admin-token-gated.
    """

    status: Literal["ok", "degraded"]
    db: Literal["ok", "down"]


class CronStatusResponse(BaseModel):
    """Admin-gated operational view (Code Review Fix 4 P2.3).

    Returns the full shape that pre-Fix-4 /health returned. Used by Will
    from a browser bookmark for ops; not exposed to Flutter.

    `approved_status` is the D8 three-tier signal:
      'ok'   when approved >= APPROVED_TARGET (default 7)
      'warm' when APPROVED_ALERT_THRESHOLD <= approved < APPROVED_TARGET
      'low'  when approved < APPROVED_ALERT_THRESHOLD
      'unknown' when the DB probe failed (status='degraded' path).
    """

    status: Literal["ok", "degraded"]
    db: Literal["ok", "down"]
    pool_pending_count: int
    pool_approved_count: int
    approved_status: Literal["ok", "warm", "low", "unknown"] = "unknown"
    latest_scheduled_date: date | None = None
    last_push_at: datetime | None = None
