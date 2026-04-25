"""Admin endpoints + review HTML page (Step 8).

Bearer-token auth on every route. The auth dependency accepts the token from
EITHER an `Authorization: Bearer <token>` header (curl / Android-style) OR a
`token` query/form param (browser-driven /admin/review page). This dual-source
acceptance is the simplest path that lets:
  - the GET /admin/review HTML page authenticate via `?token=...` in the URL
    (browsers don't send Authorization headers on plain navigations), and
  - the in-page <form action=...> approve/reject POSTs authenticate via a
    hidden `token` field (browsers don't send Authorization headers on plain
    form posts either), and
  - curl / Android / Postman use the standard Authorization header.

The "leak risk" of a token-in-URL is mitigated by HTTPS-everywhere on Railway.
This is a single-user admin surface, not an OAuth provider.

D21d note: /admin/retract is no-new-views, NOT recall. The response body
explicitly says so — Will needs to remember that pushing retract doesn't
remove the fact from devices that already received it via FCM.
"""
from __future__ import annotations

import logging
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Header,
    Query,
    Request,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import generation
from app.config import settings
from app.db import get_db
from app.models import Fact, PoolFact


logger = logging.getLogger(__name__)


_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _extract_token(
    authorization: str | None,
    token_query: str | None,
    token_form: str | None,
) -> str | None:
    """Pull the candidate token from header OR query OR form, in that order.

    Header form: `Authorization: Bearer <token>`. We only accept the literal
    "Bearer" scheme — case-sensitive — to avoid silently accepting weird
    variants. Anything malformed returns None and the caller raises 401.
    """
    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0] == "Bearer":
            return parts[1]
        return None
    if token_query:
        return token_query
    if token_form:
        return token_form
    return None


async def verify_admin_token(
    authorization: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Query()] = None,
    token_form: Annotated[str | None, Form(alias="token")] = None,
) -> None:
    """Bearer-token guard for all /admin/* routes.

    Constant-time comparison via secrets.compare_digest — defends against
    timing oracles even though this is single-user. Costs nothing to do right.
    """
    candidate = _extract_token(authorization, token, token_form)
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not secrets.compare_digest(candidate, settings.ADMIN_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(prefix="/admin", dependencies=[Depends(verify_admin_token)])


# --- response models ---------------------------------------------------------


class GenerateResponse(BaseModel):
    pool_id: int
    fact_preview: str
    category: str | None
    region: str | None
    era: str | None
    model_used: str


class FlushResponse(BaseModel):
    deleted: int


class ScheduleResponse(BaseModel):
    fact_id: int
    scheduled_date: date
    pool_id_consumed: int


class RetractResponse(BaseModel):
    fact_id: int
    scheduled_date: date
    is_retracted: bool
    note: str


class ReviewActionResponse(BaseModel):
    pool_id: int
    status: str
    reviewed_at: datetime | None


# --- POST /admin/generate ----------------------------------------------------


@router.post("/generate", response_model=GenerateResponse)
async def admin_generate(
    db: Annotated[Session, Depends(get_db)],
) -> GenerateResponse:
    """Manually drive one pool generation. Used to top up the queue between
    cron runs (Step 10) and during smoke tests."""
    try:
        row = await generation.generate_one_pool_fact(db)
    except generation.GenerationFailed as exc:
        logger.warning("admin generate failed", extra={"extra": {"error": str(exc)}})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"generation failed: {exc}",
        ) from exc

    return GenerateResponse(
        pool_id=row.id,
        fact_preview=row.fact_text,
        category=row.category,
        region=row.region,
        era=row.era,
        model_used=row.model_used,
    )


# --- POST /admin/flush-pool --------------------------------------------------


@router.post("/flush-pool", response_model=FlushResponse)
def admin_flush_pool(
    db: Annotated[Session, Depends(get_db)],
) -> FlushResponse:
    """Wipe pending_review rows only. Approved/rejected rows survive — those
    are decisions Will already made, and we don't want to lose audit trail or
    re-review work on a panicked flush."""
    deleted = db.execute(
        PoolFact.__table__.delete().where(PoolFact.status == "pending_review")
    ).rowcount
    db.commit()
    logger.info("admin flush-pool", extra={"extra": {"deleted": deleted}})
    return FlushResponse(deleted=deleted)


# --- POST /admin/schedule/{pool_id}/{target_date} ---------------------------


@router.post(
    "/schedule/{pool_id}/{target_date}",
    response_model=ScheduleResponse,
)
def admin_schedule(
    pool_id: int,
    target_date: date,
    db: Annotated[Session, Depends(get_db)],
) -> ScheduleResponse:
    """Pin a specific approved pool row to a specific date.

    Used for launch day, special dates, or when Will wants control over
    sequencing rather than letting the cron picker decide. Mirrors the
    transactional shape of schedule_tomorrows_fact (insert Fact + delete
    PoolFact in one commit, bust /today cache for the affected date).
    """
    pick = db.get(PoolFact, pool_id)
    if pick is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"pool row {pool_id} not found",
        )
    if pick.status != "approved":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"pool row {pool_id} has status {pick.status!r}, must be 'approved' "
                "to schedule"
            ),
        )

    fact = Fact(
        scheduled_date=target_date,
        fact_text=pick.fact_text,
        source_name=pick.source_name,
        source_url=pick.source_url,
        source_license=pick.source_license,
        external_id=pick.external_id,
        language=pick.language,
        category=pick.category,
        region=pick.region,
        era=pick.era,
        model_used=pick.model_used,
        prompt_version=pick.prompt_version,
    )
    db.add(fact)
    db.delete(pick)
    try:
        db.flush()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        existing = db.execute(
            select(Fact.id).where(Fact.scheduled_date == target_date)
        ).scalar_one_or_none()
        logger.warning(
            "admin schedule integrity error",
            extra={
                "extra": {
                    "pool_id": pool_id,
                    "target_date": target_date.isoformat(),
                    "existing_fact_id": existing,
                    "error": str(exc.orig) if exc.orig else str(exc),
                }
            },
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{target_date.isoformat()} is already scheduled "
                f"(fact_id={existing})"
            ),
        ) from exc

    # Local import — main.py imports admin.py at app construction time, so a
    # top-level import would cycle.
    from app.main import invalidate_today_cache

    invalidate_today_cache(target_date)

    logger.info(
        "admin scheduled fact",
        extra={
            "extra": {
                "fact_id": fact.id,
                "scheduled_date": target_date.isoformat(),
                "pool_id_consumed": pool_id,
            }
        },
    )
    return ScheduleResponse(
        fact_id=fact.id,
        scheduled_date=target_date,
        pool_id_consumed=pool_id,
    )


# --- POST /admin/retract/{target_date} (D18 + D21d) -------------------------


_RETRACT_NOTE = (
    "Retract is no-new-views, not recall. Users who already received this "
    "fact via FCM still have it locally. See D21d."
)


@router.post(
    "/retract/{target_date}",
    response_model=RetractResponse,
)
def admin_retract(
    target_date: date,
    db: Annotated[Session, Depends(get_db)],
) -> RetractResponse:
    """Mark a scheduled fact as retracted. D18 + D21d.

    Sets is_retracted=true so /today and /archive stop returning it. Does NOT
    delete the row — we keep it for audit. Does NOT push a recall to FCM
    devices — that's a much bigger system. The D21d reminder in the response
    body is a deliberate UX cue for Will when he uses this in anger.
    """
    fact = db.execute(
        select(Fact).where(
            Fact.scheduled_date == target_date,
            Fact.is_retracted.is_(False),
        )
    ).scalar_one_or_none()
    if fact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"no active fact for {target_date.isoformat()} "
                "(already retracted, or never scheduled)"
            ),
        )

    fact.is_retracted = True
    db.commit()

    from app.main import invalidate_today_cache

    invalidate_today_cache(target_date)

    logger.info(
        "admin retracted fact",
        extra={
            "extra": {
                "fact_id": fact.id,
                "scheduled_date": target_date.isoformat(),
            }
        },
    )
    return RetractResponse(
        fact_id=fact.id,
        scheduled_date=target_date,
        is_retracted=True,
        note=_RETRACT_NOTE,
    )


# --- POST /admin/review/{pool_id} -------------------------------------------


def _apply_review(
    db: Session, pool_id: int, action: Literal["approve", "reject"]
) -> PoolFact:
    """Shared logic for the JSON endpoint and the HTML form post."""
    row = db.get(PoolFact, pool_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"pool row {pool_id} not found",
        )
    if row.status != "pending_review":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"pool row {pool_id} already reviewed (status={row.status!r})"
            ),
        )
    row.status = "approved" if action == "approve" else "rejected"
    row.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    logger.info(
        "admin reviewed pool row",
        extra={
            "extra": {
                "pool_id": row.id,
                "action": action,
                "new_status": row.status,
            }
        },
    )
    return row


@router.post("/review/{pool_id}", response_model=ReviewActionResponse)
async def admin_review(
    pool_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> ReviewActionResponse | RedirectResponse:
    """Approve or reject a pending_review pool row.

    Body shape (one of):
      - form-encoded `action=approve&token=...` — used by the HTML review
        page's <form>s. Returns 303 redirect back to /admin/review so the
        browser doesn't try to re-POST on refresh.
      - JSON `{"action": "approve" | "reject"}` — used by curl / future API
        clients. Returns the updated row as JSON.

    We parse the body manually because FastAPI parameter binding can't cleanly
    accept BOTH a Pydantic body AND a Form() param on the same route — the
    presence of any Form() param forces form-encoded parsing globally on that
    handler. Reading via request.form() / request.json() lazily avoids that
    conflict and lets the same path serve both shapes.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
    raw_action: str | None = None
    is_form = False

    if content_type == "application/json":
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid JSON body: {exc}",
            ) from exc
        if isinstance(body, dict):
            raw_action = body.get("action")
    else:
        # x-www-form-urlencoded or multipart/form-data — both handled here.
        form = await request.form()
        raw_action = form.get("action")  # type: ignore[assignment]
        is_form = True

    if raw_action not in ("approve", "reject"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="action must be 'approve' or 'reject'",
        )
    chosen: Literal["approve", "reject"] = raw_action  # type: ignore[assignment]

    row = _apply_review(db, pool_id, chosen)

    if is_form:
        return RedirectResponse(
            url=f"/admin/review?token={settings.ADMIN_TOKEN}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return ReviewActionResponse(
        pool_id=row.id,
        status=row.status,
        reviewed_at=row.reviewed_at,
    )


# --- GET /admin/review -------------------------------------------------------


@router.get("/review", response_class=HTMLResponse)
def admin_review_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    """Render the review queue. Auth is enforced by the router-level
    dependency, which accepts ?token=... for browser navigations."""
    pending = list(
        db.execute(
            select(PoolFact)
            .where(PoolFact.status == "pending_review")
            .order_by(PoolFact.created_at.desc())
        ).scalars()
    )
    approved_count = db.execute(
        select(func.count())
        .select_from(PoolFact)
        .where(PoolFact.status == "approved")
    ).scalar_one()
    rejected_count = db.execute(
        select(func.count())
        .select_from(PoolFact)
        .where(PoolFact.status == "rejected")
    ).scalar_one()

    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={
            "pending": pending,
            "pending_count": len(pending),
            "approved_count": approved_count,
            "rejected_count": rejected_count,
            "admin_token": settings.ADMIN_TOKEN,
        },
    )
