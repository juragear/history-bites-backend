"""Pydantic response models for public endpoints (Step 7).

These are the API contract the Android app consumes. Keep field names stable —
renames here are breaking changes for deployed clients.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


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
    items: list[ArchiveItem]
    count: int


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: Literal["ok", "down"]
    pool_pending_count: int
    pool_approved_count: int
    latest_scheduled_date: date | None = None
    last_push_at: datetime | None = None
