"""Admin endpoints + review HTML page (Step 8; auth split per Code Review
Fix 1).

Bearer-token auth on every route. **Two** auth dependencies:

  - `verify_admin_token_strict` — accepts the token from `Authorization:
    Bearer <token>` header OR a hidden `token` form field. The default for
    every admin route. Query-string tokens are NOT accepted because
    query-string secrets land in access logs / proxy logs / browser history.

  - `verify_admin_token_with_query` — additionally accepts `?token=...` in
    the URL. Used ONLY on the GET `/admin/review` HTML page, where browser
    navigations can't set Authorization headers. The query-string surface is
    isolated to this one read-only HTML route via a separate sub-router
    (`review_page_router`) so a future POST endpoint added to the main
    `router` automatically inherits the strict posture.

Code Review Fix 1 (P2.1 + P2.2): paired with `StripQueryStringFormatter` in
`app/main.py:configure_logging` which strips `?...` from uvicorn access-log
request lines. Together: query-string tokens are accepted on exactly one
HTTP route AND never persisted to logs, even on that route.

D21d note: /admin/retract is no-new-views, NOT recall. The response body
explicitly says so — Will needs to remember that pushing retract doesn't
remove the fact from devices that already received it via FCM.
"""
from __future__ import annotations

import logging
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated

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

from app import cron, fcm, generation
from app.config import settings
from app.db import get_db
from app.models import Fact, PoolFact
from app.review_tags import (
    InvalidRatingError,
    InvalidTagError,
    derive_status_from_rating,
    validate_tags,
)


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

    Either of `token_query` / `token_form` may be passed as None to indicate
    "this auth variant does not accept this source" (the strict variant
    passes None for `token_query` so URL-based tokens are never even
    considered).
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


def _check_token(candidate: str | None) -> None:
    """Constant-time compare against settings.ADMIN_TOKEN. 401 on miss/bad.

    Shared by both auth-dependency variants (strict + with-query). Centralised
    so the comparison + 401 shape are identical regardless of which route
    triggered the check.
    """
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


async def verify_admin_token_strict(
    authorization: Annotated[str | None, Header()] = None,
    token_form: Annotated[str | None, Form(alias="token")] = None,
) -> None:
    """Default admin auth: accepts header or form ONLY — not query string.

    Used on every admin endpoint EXCEPT the GET `/admin/review` HTML page
    (which uses `verify_admin_token_with_query` because browser navigations
    can't set Authorization headers). Query-string tokens are rejected here
    because they end up in access logs, proxy logs, browser history, and
    Referer headers — defense-in-depth complementing
    `app/main.py:StripQueryStringFormatter`.
    """
    candidate = _extract_token(authorization, token_query=None, token_form=token_form)
    _check_token(candidate)


async def verify_admin_token_with_query(
    authorization: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Query()] = None,
    token_form: Annotated[str | None, Form(alias="token")] = None,
) -> None:
    """Admin auth that ADDITIONALLY accepts `?token=...` in the URL.

    Use ONLY on the GET `/admin/review` HTML page. Browsers can't set
    Authorization headers on a plain navigation, and a hidden form field
    can't be embedded in a navigation either, so the query-string path is
    the only practical option for that one route. The
    `StripQueryStringFormatter` in `app/main.py` ensures the token still
    doesn't land in access logs even when used via this path.
    """
    candidate = _extract_token(authorization, token_query=token, token_form=token_form)
    _check_token(candidate)


# Main admin router — strict auth (no query-string token). Every admin POST
# endpoint registered against this router automatically inherits the strict
# posture. New endpoints get strict auth for free.
router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(verify_admin_token_strict)],
)

# Separate sub-router for the GET /admin/review HTML page only. Isolating
# the query-friendly auth posture to this one router means it can't bleed
# into other endpoints by accident — a future POST added to `router` above
# is naturally strict; only GETs explicitly registered HERE accept ?token=.
review_page_router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(verify_admin_token_with_query)],
)


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
    review_rating: int | None = None
    review_tags: list[str] | None = None
    review_notes: str | None = None


class PushResponse(BaseModel):
    message_id: str
    fact_id: int
    scheduled_date: date
    pushed_at: datetime | None


class RunGenerationResponse(BaseModel):
    """Mirror of cron.run_generation's summary dict.

    Loose typing on `scheduled` and `alerts_sent` because the cron summary
    intentionally varies (None, "already_scheduled_or_race", or a fact dict).
    Pydantic v2 with arbitrary types disabled would still accept dict[str,
    Any]-style fields via this model — keeps the wire shape honest.
    """

    started_at: str
    finished_at: str
    scheduled: dict | str | None
    generated: int
    generation_failures: int
    pending_after: int
    approved_after: int
    alerts_sent: list[str]


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


_NOTES_MAX_LEN = 500


def _normalize_notes(raw: str | None) -> str | None:
    """Silently truncate to 500 chars and collapse empty -> None.

    Silent (not error) is intentional: a typo in the notes field shouldn't
    interrupt the review flow. ≤500 keeps the column bounded for grep sanity
    later. Empty string normalizes to None so blank-on-submit doesn't write
    "" to the DB, distinct from "no notes attached."
    """
    if raw is None:
        return None
    truncated = raw[:_NOTES_MAX_LEN]
    return truncated or None


def _apply_review(
    db: Session,
    pool_id: int,
    rating: int,
    *,
    cleaned_tags: list[str],
    truncated_notes: str | None,
) -> PoolFact:
    """Shared logic for the JSON endpoint and the HTML form post.

    `rating` is a validated 1-5 int. Status derives via
    `derive_status_from_rating` (>=4 approved, <=3 rejected — D26).

    `cleaned_tags` has already been through `validate_tags`; an empty list is
    canonicalized to NULL on the row so "no tags" is one state, not two.
    `truncated_notes` has already been clipped to <= _NOTES_MAX_LEN.

    Re-rating is allowed by design (D26). The Session 8 once-only guard was
    removed so a row that was previously rated can be re-rated; the new
    rating, tags, and notes overwrite the prior values, and `reviewed_at` is
    refreshed to the latest decision time.
    """
    row = db.get(PoolFact, pool_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"pool row {pool_id} not found",
        )
    new_status = derive_status_from_rating(rating)
    row.review_rating = rating
    row.status = new_status
    row.reviewed_at = datetime.now(timezone.utc)
    # Empty cleaned list -> None so SQL doesn't have to distinguish between
    # NULL and `[]`. Avoids a future `tags IS NULL` vs `tags = '[]'` trap.
    row.review_tags = cleaned_tags or None
    row.review_notes = truncated_notes
    db.commit()
    db.refresh(row)
    logger.info(
        "admin reviewed pool row",
        extra={
            "extra": {
                "pool_id": row.id,
                "rating": rating,
                "new_status": row.status,
                "tag_count": len(cleaned_tags),
                "notes_len": len(truncated_notes) if truncated_notes else 0,
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
    """Rate a pool row 1-5 (D26). Status derives: >=4 approved, <=3 rejected.

    Body shape (one of):
      - form-encoded `rating=5&token=...&tags=...&tags=...&notes=...` —
        used by the HTML review page. Repeated `tags` keys are read via
        `form.getlist("tags")` (FastAPI's standard list-from-form pattern).
        Returns 303 redirect back to /admin/review so the browser doesn't
        try to re-POST on refresh.
      - JSON `{"rating": 5, "tags": [...], "notes": "..."}` — used by
        curl / future API clients. Returns the updated row as JSON.

    `rating` is required and must be an int in [1, 5]. Anything else 400s.
    Tags must come from the palette in app.review_tags; unknown tags 400.
    Notes get silently truncated to 500 chars (a typo shouldn't 4xx
    mid-review).

    Re-rating is allowed (D26): the once-only guard from Session 8 was
    removed so a previously-rated row can be re-rated and the new
    rating/tags/notes overwrite the prior values.

    We parse the body manually because FastAPI parameter binding can't cleanly
    accept BOTH a Pydantic body AND a Form() param on the same route — the
    presence of any Form() param forces form-encoded parsing globally on that
    handler. Reading via request.form() / request.json() lazily avoids that
    conflict and lets the same path serve both shapes.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
    raw_rating: object = None
    raw_tags: list[str] | None = None
    raw_notes: str | None = None
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
            raw_rating = body.get("rating")
            tags_field = body.get("tags")
            if tags_field is not None:
                if not isinstance(tags_field, list):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="tags must be a list of strings",
                    )
                raw_tags = [str(t) for t in tags_field]
            notes_field = body.get("notes")
            if notes_field is not None:
                raw_notes = str(notes_field)
    else:
        # x-www-form-urlencoded or multipart/form-data — both handled here.
        form = await request.form()
        raw_rating = form.get("rating")
        # Repeated `tags` keys come back via getlist; absent key -> [].
        tags_list = form.getlist("tags")
        raw_tags = [str(t) for t in tags_list] if tags_list else None
        notes_value = form.get("notes")
        raw_notes = str(notes_value) if notes_value is not None else None
        is_form = True

    # Coerce + validate `rating`. Forms always deliver strings; JSON should
    # deliver int but we coerce a digit-string for tolerance. Bools sneak past
    # `isinstance(x, int)` in Python, so derive_status_from_rating filters
    # them — but we also catch the obvious "missing entirely" case here for a
    # cleaner error message than the helper's generic repr.
    if raw_rating is None or raw_rating == "":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="rating is required (int 1-5)",
        )
    if isinstance(raw_rating, bool):
        # Same guard as derive_status_from_rating — surface as 400 here so the
        # endpoint doesn't bubble an InvalidRatingError on a bool payload.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"rating must be an int 1-5, got {raw_rating!r}",
        )
    if isinstance(raw_rating, int):
        rating_int = raw_rating
    else:
        try:
            rating_int = int(str(raw_rating))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"rating must be an int 1-5, got {raw_rating!r}",
            ) from exc

    try:
        cleaned_tags = validate_tags(raw_tags)
    except InvalidTagError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    truncated_notes = _normalize_notes(raw_notes)

    try:
        row = _apply_review(
            db,
            pool_id,
            rating_int,
            cleaned_tags=cleaned_tags,
            truncated_notes=truncated_notes,
        )
    except InvalidRatingError as exc:
        # Range / type violation from the helper. Mapped to 400 here so the
        # endpoint owns the HTTP-shape decision and the helper stays pure.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    if is_form:
        return RedirectResponse(
            url=f"/admin/review?token={settings.ADMIN_TOKEN}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return ReviewActionResponse(
        pool_id=row.id,
        status=row.status,
        reviewed_at=row.reviewed_at,
        review_rating=row.review_rating,
        review_tags=row.review_tags,
        review_notes=row.review_notes,
    )


# --- POST /admin/push --------------------------------------------------------


@router.post("/push", response_model=PushResponse)
def admin_push(
    db: Annotated[Session, Depends(get_db)],
) -> PushResponse:
    """Manually trigger today's FCM push.

    Two reasons this exists in Step 9 (before Step 10 wires the actual cron):
      1. Smoke test surface — exercises run_push end-to-end against live FCM.
      2. Operational tool — if the cron fires but FCM was briefly down, Will
         can re-trigger after recovery without waiting for tomorrow.

    400 if there's no active fact for today (retracted or never scheduled).
    503 if FCM rejects after retries — surfaces the underlying error.
    """
    try:
        result = cron.run_push(db)
    except fcm.FCMError as exc:
        logger.warning("admin push failed", extra={"extra": {"error": str(exc)}})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"fcm send failed: {exc}",
        ) from exc

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no active fact for today (not scheduled or retracted)",
        )

    return PushResponse(
        message_id=result["message_id"],
        fact_id=result["fact_id"],
        scheduled_date=date.fromisoformat(result["scheduled_date"]),
        pushed_at=(
            datetime.fromisoformat(result["pushed_at"])
            if result.get("pushed_at")
            else None
        ),
    )


# --- POST /admin/cron/run-generation -----------------------------------------


@router.post("/cron/run-generation", response_model=RunGenerationResponse)
async def admin_run_generation(
    db: Annotated[Session, Depends(get_db)],
) -> RunGenerationResponse:
    """Manually trigger the every-6h generation cron.

    Same job that Railway's [[cron]] entry runs via `python -m app.cron
    run_generation`. Useful for:
      - Smoke testing generation + scheduling without waiting up to 6h.
      - Recovering from a failed cron run by re-driving locally.
      - Pre-launch pool warming (Will hits this until approved >= some buffer).

    Returns the same structured summary the CLI logs. 503 only if the run
    raises something it didn't catch — NoApprovedPool / GenerationFailed /
    alert webhook failures are all handled inside run_generation and surfaced
    in the response body, not as HTTP errors.
    """
    try:
        summary = await cron.run_generation(db)
    except Exception as exc:
        logger.exception(
            "admin run_generation crashed",
            extra={"extra": {"error": repr(exc)}},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"run_generation failed: {exc}",
        ) from exc

    return RunGenerationResponse(**summary)


# --- GET /admin/review -------------------------------------------------------
#
# Lives on `review_page_router`, NOT `router`. The sub-router pattern keeps the
# query-string-friendly auth (`verify_admin_token_with_query`) isolated to this
# one HTML page and out of the strict-by-default policy on every POST endpoint.


@review_page_router.get("/review", response_class=HTMLResponse)
def admin_review_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    """Render the review queue. Auth is enforced by the router-level
    dependency on `review_page_router`, which accepts ?token=... for
    browser navigations (the only route in the codebase that does)."""
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
