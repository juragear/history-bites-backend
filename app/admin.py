"""Admin endpoints + review HTML page (Step 8; cookie handoff per Code
Review Fix 6, 2026-04-29).

Two authentication channels, one shared verification dep:

  - **Bearer header** — `Authorization: Bearer <token>` for curl, scripts,
    and the Phase 2 mobile client. Unchanged from Fix 1.
  - **Session cookie** — `hb_admin` HttpOnly + Secure + SameSite=Strict +
    Path=/admin, set by POST `/admin/login` after the operator submits the
    token via a form, cleared by POST `/admin/logout`. Replaces the
    `?token=...` query string that GET `/admin/review` accepted in Fix 1.

The single `verify_admin_token` dependency reads (header | cookie | form),
in that order. Query-string tokens are NOT accepted on any route — Fix 6
removes that surface entirely so URL bars, browser history, bookmarks, and
screenshots cannot leak the token.

Two routers reflect the two auth postures:

  - `router` (the main admin router) — applies `verify_admin_token` at the
    router level. Every POST + the operator GETs (`/admin/cron/status`,
    `/admin/logout`) auto-401 on missing/invalid creds.
  - `admin_unauth_router` — has no router-level dep. Hosts the three routes
    that handle their own auth so they can render or redirect instead of
    401'ing: GET `/admin/review` (redirects to /admin/login on miss), GET
    `/admin/login` (renders form), POST `/admin/login` (validates + sets
    cookie). Replaces the Fix 1 `review_page_router` sub-router.

`StripQueryStringFormatter` in `app/main.py:configure_logging` is kept as
defense in depth even though no admin route now reads query-string tokens.

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
    Cookie,
    Depends,
    Form,
    HTTPException,
    Header,
    Request,
    Response,
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
from app.db import SessionLocal, get_db
from app.models import Fact, PoolFact
from app.review_tags import (
    InvalidRatingError,
    InvalidTagError,
    derive_status_from_rating,
    validate_tags,
)
from app.schemas import CronStatusResponse, ErrorDetail


logger = logging.getLogger(__name__)


_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _extract_token(
    authorization: str | None,
    token_cookie: str | None,
    token_form: str | None,
) -> str | None:
    """Pull the candidate token from header > cookie > form, in that order.

    Header form: `Authorization: Bearer <token>`. Only the literal "Bearer"
    scheme (case-sensitive) is accepted to avoid silently accepting weird
    variants. Anything malformed returns None and the caller raises 401.

    The cookie source is the Fix 6 replacement for the Fix 1 `?token=...`
    query path; the form source is unchanged (the HTML review page POSTs
    rating submissions and either the cookie or a hidden token field can
    authenticate them; SameSite=Strict on the cookie covers CSRF).
    """
    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0] == "Bearer":
            return parts[1]
        return None
    if token_cookie:
        return token_cookie
    if token_form:
        return token_form
    return None


def _is_valid_token(candidate: str | None) -> bool:
    """Non-raising constant-time check against settings.ADMIN_TOKEN.

    Used by routes that handle the failure themselves (the /admin/login
    "already logged in" redirect, the /admin/login POST validator, and the
    /admin/review redirect-to-login fallback). `_check_token` wraps this for
    the dependency path that wants a 401 raised.
    """
    if candidate is None:
        return False
    return secrets.compare_digest(candidate, settings.ADMIN_TOKEN)


def _check_token(candidate: str | None) -> None:
    """Constant-time compare against settings.ADMIN_TOKEN. 401 on miss/bad.

    Wraps `_is_valid_token` so the raising path and the boolean path stay in
    sync. Used by `verify_admin_token` so the dependency machinery surfaces
    the standard 401 shape on missing/invalid creds.
    """
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not _is_valid_token(candidate):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _set_admin_cookie(response: Response, token: str) -> None:
    """Attach the admin session cookie to `response`.

    HttpOnly stops JS readers; Secure stops the browser sending it over plain
    HTTP (overrideable for local dev via `ADMIN_COOKIE_SECURE=False`);
    SameSite=Strict stops cross-site auto-send (CSRF defense for the form
    POSTs that previously relied on a hidden token field); Path=/admin
    scopes the cookie to admin routes so it never reaches public paths.
    """
    response.set_cookie(
        key=settings.ADMIN_COOKIE_NAME,
        value=token,
        max_age=settings.ADMIN_COOKIE_MAX_AGE_SECONDS,
        path="/admin",
        secure=settings.ADMIN_COOKIE_SECURE,
        httponly=True,
        samesite="strict",
    )


def _clear_admin_cookie(response: Response) -> None:
    """Drop the admin session cookie via Set-Cookie with Max-Age=0.

    Browsers match cookies on (name, path, domain) for deletion. The
    attributes here MUST mirror `_set_admin_cookie` or the original cookie
    persists in the browser's jar.
    """
    response.delete_cookie(
        key=settings.ADMIN_COOKIE_NAME,
        path="/admin",
        secure=settings.ADMIN_COOKIE_SECURE,
        httponly=True,
        samesite="strict",
    )


async def verify_admin_token(
    authorization: Annotated[str | None, Header()] = None,
    cookie_token: Annotated[
        str | None, Cookie(alias=settings.ADMIN_COOKIE_NAME)
    ] = None,
    token_form: Annotated[str | None, Form(alias="token")] = None,
) -> None:
    """Unified admin auth: accepts header, cookie, or form (in that order).

    Code Review Fix 6 collapsed the Fix 1 strict + with-query split back to
    a single dependency. The cookie path replaces the `?token=...` query
    path; query-string tokens are no longer accepted on any route.
    """
    _check_token(_extract_token(authorization, cookie_token, token_form))


# Main admin router — auto-401s on missing/invalid creds via the unified
# `verify_admin_token` dep applied at the router level. Every POST plus
# operator GETs (cron/status, logout) inherit this. Code Review Fix 4 (P2.2)
# declared the 401 response shape here for OpenAPI consistency; that
# carries over through the Fix 6 dep collapse unchanged.
router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(verify_admin_token)],
    responses={
        401: {
            "model": ErrorDetail,
            "description": "Missing or invalid admin token.",
        },
    },
)

# Code Review Fix 6 (replaces Fix 1's `review_page_router`): hosts the three
# routes that manage their own auth — /admin/login GET (renders form),
# /admin/login POST (validates and sets cookie), /admin/review GET
# (redirects to /admin/login on miss instead of 401'ing). No router-level
# dep so the handlers can choose between rendering, redirecting, and
# 401'ing per-route. Every other admin endpoint stays on `router` with
# auto-401 enforcement.
admin_unauth_router = APIRouter(prefix="/admin")


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


@router.post(
    "/generate",
    response_model=GenerateResponse,
    responses={
        503: {
            "model": ErrorDetail,
            "description": (
                "Generation failed (Wikipedia unavailable, Gemini unavailable, "
                "or the candidate budget was exhausted). Detail string is the "
                "Code Review Fix 3 sentinel; full chain in server logs."
            ),
        },
    },
)
async def admin_generate(
    db: Annotated[Session, Depends(get_db)],
) -> GenerateResponse:
    """Manually drive one pool generation. Used to top up the queue between
    cron runs (Step 10) and during smoke tests."""
    try:
        row = await generation.generate_one_pool_fact(db)
    except generation.GenerationFailed as exc:
        # Code Review Fix 3 (P2.3): the 503 detail used to stringify `exc`
        # verbatim, which leaked `gemini-3-flash-preview API call failed:
        # ClientError("503 UNAVAILABLE...")` plus every attempted Wikipedia
        # title into the response body. `from exc` keeps the cause chain so
        # logger.exception(...) anywhere upstream still gets the full
        # traceback (after Fix 3 P2.1 wired traceback rendering into
        # JSONFormatter). The structured logger.warning below carries
        # str(exc) into Railway server-side; the wire stays clean.
        logger.warning("admin generate failed", extra={"extra": {"error": str(exc)}})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="generation failed; see server logs for details",
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
    responses={
        404: {
            "model": ErrorDetail,
            "description": "Pool row not found (already consumed or never existed).",
        },
        400: {
            "model": ErrorDetail,
            "description": "Pool row exists but its status is not 'approved'.",
        },
        409: {
            "model": ErrorDetail,
            "description": "Target date is already scheduled (race lost).",
        },
    },
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
    responses={
        404: {
            "model": ErrorDetail,
            "description": "No active fact for this date (already retracted or never scheduled).",
        },
    },
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


@router.post(
    "/review/{pool_id}",
    response_model=ReviewActionResponse,
    responses={
        303: {"description": "Form submission redirect back to the review page."},
        400: {
            "model": ErrorDetail,
            "description": (
                "Validation failure: missing/out-of-range rating, unknown tag, "
                "or malformed JSON body."
            ),
        },
        404: {
            "model": ErrorDetail,
            "description": "Pool row not found.",
        },
    },
)
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
        # Code Review Fix 6: redirect to the bare path. The browser carries
        # the admin session cookie automatically on the follow-up GET, so
        # the previous `?token=...` round-trip (which leaked the token into
        # the URL bar after every form submit) is no longer needed.
        return RedirectResponse(
            url="/admin/review",
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


@router.post(
    "/push",
    response_model=PushResponse,
    responses={
        400: {
            "model": ErrorDetail,
            "description": "No active fact for today (not scheduled or retracted).",
        },
        503: {
            "model": ErrorDetail,
            "description": (
                "FCM send failed after retries. Detail string is the Code "
                "Review Fix 3 sentinel; full firebase exception chain is in "
                "server logs."
            ),
        },
    },
)
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
        # Code Review Fix 3 (P2.4): the 503 detail used to stringify the
        # FCMError, which embeds the FCM topic + the firebase-admin exception
        # class verbatim (e.g. `UnregisteredError: Requested entity was not
        # found.`). The structured warning below carries that to Railway;
        # the wire stays clean.
        logger.warning("admin push failed", extra={"extra": {"error": str(exc)}})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="fcm send failed; see server logs for details",
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


@router.post(
    "/cron/run-generation",
    response_model=RunGenerationResponse,
    responses={
        503: {
            "model": ErrorDetail,
            "description": (
                "Unhandled error during the cron run. Detail string is the "
                "Code Review Fix 3 sentinel; full traceback in server logs."
            ),
        },
    },
)
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
        # Code Review Fix 3 (P2.2): the 503 detail used to stringify `exc`,
        # which for the realistic SQLAlchemy OperationalError case dumped the
        # DB hostname + IP + port + full SQL + bind parameters into the
        # response body. logger.exception now actually captures the traceback
        # in Railway (Fix 3 P2.1) so the wire-side scrub doesn't lose any
        # operator information.
        logger.exception(
            "admin run_generation crashed",
            extra={"extra": {"error": repr(exc)}},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="run_generation failed; see server logs for details",
        ) from exc

    return RunGenerationResponse(**summary)


# --- GET /admin/cron/status (Code Review Fix 4 P2.3) ------------------------
#
# Replaces the rich operational shape that pre-Fix-4 /health used to return.
# /v1/health is now a thin status-only probe for unauthenticated callers
# (Flutter, Railway healthcheck); operator-facing metrics live here, gated
# by the strict admin auth.


def _approved_status(approved_count: int) -> Literal["ok", "warm", "low"]:
    """D8 three-tier mapping (originally surfaced via /health in Step 14;
    moved here in Fix 4 alongside the rest of the operational view).

    >= APPROVED_TARGET   -> 'ok'    (target buffer met; cron in steady state)
    >= ALERT_THRESHOLD   -> 'warm'  (below target but not paging; cron tops up)
    <  ALERT_THRESHOLD   -> 'low'   (below alert floor; Slack alert fires)
    """
    if approved_count >= settings.APPROVED_TARGET:
        return "ok"
    if approved_count >= settings.APPROVED_ALERT_THRESHOLD:
        return "warm"
    return "low"


@router.get(
    "/cron/status",
    response_model=CronStatusResponse,
    responses={
        503: {
            "model": ErrorDetail,
            "description": "Database probe failed; metrics unavailable.",
        },
    },
)
def admin_cron_status(response: Response) -> CronStatusResponse:
    """Operator-gated operational view (Code Review Fix 4 P2.3).

    Returns the same shape pre-Fix-4 /health returned: pool counts +
    scheduling runway + last push time + D8 three-tier signal. Gated by
    the standard router-level admin auth so an unauthenticated observer
    can't infer pool size or cron timing by polling.

    Uses `SessionLocal()` directly (not `Depends(get_db)`) so failures at
    session-creation time can be surfaced as a clean 503 with a
    `degraded` body rather than a 500 from the dependency machinery —
    matches the /v1/health failure shape exactly.
    """
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
            latest = db.execute(
                select(func.max(Fact.scheduled_date))
            ).scalar_one()
            # Step 9 carryover: most-recent-wins MAX(pushed_at).
            last_push_at = db.execute(
                select(func.max(Fact.pushed_at))
            ).scalar_one()
    except Exception as exc:  # SQLAlchemyError + dialect-level errors
        logger.warning(
            "cron status db probe failed",
            extra={"extra": {"error_type": type(exc).__name__}},
        )
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return CronStatusResponse(
            status="degraded",
            db="down",
            pool_pending_count=0,
            pool_approved_count=0,
            approved_status="unknown",
            latest_scheduled_date=None,
            last_push_at=None,
        )

    return CronStatusResponse(
        status="ok",
        db="ok",
        pool_pending_count=pending,
        pool_approved_count=approved,
        approved_status=_approved_status(approved),
        latest_scheduled_date=latest,
        last_push_at=last_push_at,
    )


@admin_unauth_router.get(
    "/review",
    responses={
        200: {
            "description": "Renders the review queue HTML page.",
            "content": {"text/html": {}},
        },
        303: {"description": "No valid auth — redirect to /admin/login."},
    },
)
def admin_review_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
    cookie_token: Annotated[
        str | None, Cookie(alias=settings.ADMIN_COOKIE_NAME)
    ] = None,
) -> Response:
    """Render the review queue, or redirect to /admin/login on no creds.

    Code Review Fix 6: this is the only admin route that does manual auth
    instead of inheriting the router-level dep — POST routes 401-hard,
    /admin/review GET 303s to the login form so a browser visitor with no
    cookie sees a usable page instead of a JSON 401. Header auth still
    works for curl + scripts (no behaviour change for programmatic
    callers).
    """
    if not _is_valid_token(_extract_token(authorization, cookie_token, None)):
        return RedirectResponse(
            url="/admin/login", status_code=status.HTTP_303_SEE_OTHER
        )

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

    # Code Review Fix 6: `admin_token` is no longer passed to the template.
    # The hidden form field that previously embedded the token was removed
    # from `review.html`; cookie auth covers the form POSTs.
    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={
            "pending": pending,
            "pending_count": len(pending),
            "approved_count": approved_count,
            "rejected_count": rejected_count,
        },
    )


# --- /admin/login + /admin/logout (Code Review Fix 6, 2026-04-29) -----------
#
# Browser auth flows through these three routes. The login form is
# intentionally minimal — operator-only, one-shot per cookie lifetime, no
# brand. Inline HTML keeps it out of the Jinja templates directory; the
# template surface stays scoped to user-facing review markup.


def _render_login_form(error: str | None = None) -> str:
    """Render the /admin/login HTML form.

    Plain semantic markup — no CSS framework, no JS, no external assets. The
    only conditional element is an error banner shown after a failed POST.
    """
    error_html = (
        f'<p style="color:#b3261e;margin:8px 0;">{error}</p>' if error else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex,nofollow">
  <title>HistoryBites Admin Login</title>
</head>
<body style="font:16px/1.5 -apple-system,BlinkMacSystemFont,sans-serif;max-width:360px;margin:48px auto;padding:0 16px;">
  <h1 style="font-size:20px;margin:0 0 16px;">HistoryBites Admin</h1>
  {error_html}
  <form method="POST" action="/admin/login">
    <label style="display:block;font-size:14px;margin-bottom:8px;">Admin token
      <input type="password" name="token" autofocus required
             style="display:block;width:100%;padding:8px;font:inherit;
                    border:1px solid #ddd;border-radius:6px;margin-top:4px;">
    </label>
    <button type="submit"
            style="display:block;width:100%;padding:10px 16px;font:inherit;
                   background:#0b5fff;color:#fff;border:1px solid #0b5fff;
                   border-radius:6px;cursor:pointer;margin-top:12px;">
      Sign in
    </button>
  </form>
</body>
</html>"""


@admin_unauth_router.get(
    "/login",
    responses={
        200: {
            "description": "Renders the admin login form.",
            "content": {"text/html": {}},
        },
        303: {"description": "Already authenticated — redirect to /admin/review."},
    },
)
def admin_login_page(
    cookie_token: Annotated[
        str | None, Cookie(alias=settings.ADMIN_COOKIE_NAME)
    ] = None,
) -> Response:
    """Render the login form, or redirect to /admin/review if already logged in.

    No auth dep on this route — it's the entry point for unauthenticated
    operators. The "already logged in" check is done manually in the body so
    a missing/invalid cookie falls through to render the form rather than
    raising 401.
    """
    if _is_valid_token(cookie_token):
        return RedirectResponse(
            url="/admin/review", status_code=status.HTTP_303_SEE_OTHER
        )
    # Cache-Control: no-store keeps the form (and any post-failure error
    # banner) out of browser/intermediary caches.
    return HTMLResponse(
        content=_render_login_form(),
        headers={"Cache-Control": "no-store"},
    )


@admin_unauth_router.post(
    "/login",
    responses={
        303: {"description": "Login OK — Set-Cookie + redirect to /admin/review."},
        401: {
            "description": "Bad token — re-render form with error.",
            "content": {"text/html": {}},
        },
    },
)
def admin_login_submit(
    token: Annotated[str | None, Form()] = None,
) -> Response:
    """Validate the submitted token and either set the cookie + redirect or
    re-render the form with an error.

    INFO log carries the outcome only; the submitted token value is NEVER
    logged on either path. Failed attempts log at INFO (not WARNING) because
    in single-operator pre-launch a fail is overwhelmingly an operator
    typo, not an attack — Slack-noise discipline.
    """
    if _is_valid_token(token):
        logger.info("admin login", extra={"extra": {"outcome": "success"}})
        response = RedirectResponse(
            url="/admin/review", status_code=status.HTTP_303_SEE_OTHER
        )
        # `token` is non-None here because _is_valid_token returned True.
        _set_admin_cookie(response, token)  # type: ignore[arg-type]
        return response

    logger.info("admin login", extra={"extra": {"outcome": "failure"}})
    return HTMLResponse(
        content=_render_login_form(error="Invalid token"),
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/logout",
    responses={
        303: {"description": "Logout — clear cookie + redirect to /admin/login."},
    },
)
def admin_logout() -> Response:
    """Clear the admin session cookie. Auth required (you must be logged in
    to log out — SameSite=Strict already covers most CSRF risk; the auth
    requirement is belt-and-braces).
    """
    response = RedirectResponse(
        url="/admin/login", status_code=status.HTTP_303_SEE_OTHER
    )
    _clear_admin_cookie(response)
    return response
