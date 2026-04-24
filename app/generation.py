"""Generation pipeline: Wikipedia article -> Gemini/Ollama -> pool row.

Single public orchestrator: `generate_one_pool_fact(session)`. It picks a random
curated category, lists candidate articles, filters out ones already used in
either `facts` or `pool` (D3: topic-level uniqueness), and tries up to 3
candidates in sequence. For each, it fetches the extract, asks the configured
ModelProvider for a one-sentence fact, validates, and inserts into `pool` with
`status='pending_review'` (D9).

Validation (D5/D20): non-empty after strip() AND len <= 280. No n-gram check,
no semantic similarity. Copyright safety relies on V1_PROMPT + human review.

The retry budget is candidate-focused, not unconditional:
  - 4xx from Wikipedia fetch_extract (stale title, redirect, deleted)
    -> skip to next candidate, does NOT count against the 3-try budget
  - IntegrityError on insert (lost a race to another generator)
    -> skip to next candidate, does NOT count against the budget
  - Provider error / validation failure / non-4xx HTTP error
    -> counts against the budget
Budget exhausted or candidates exhausted -> GenerationFailed.
"""
from __future__ import annotations

import logging
import random
from datetime import date, timedelta

import httpx
from sqlalchemy import select, union_all
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import wikipedia
from app.config import settings
from app.model_provider import ModelProviderError, get_provider
from app.models import Fact, PoolFact


logger = logging.getLogger(__name__)


MAX_CANDIDATE_ATTEMPTS = 3


class GenerationFailed(Exception):
    """Raised when every attempted candidate failed. Caller decides what to do."""


class NoApprovedPool(Exception):
    """Raised when schedule_tomorrows_fact finds zero approved pool rows (D21b)."""


def is_valid(fact: str) -> bool:
    return bool(fact.strip()) and len(fact) <= 280


def get_used_external_ids(session: Session, source_name: str) -> set[str]:
    """Union of external_ids already present in facts OR pool for this source.

    Checked before calling the model so we don't burn a Gemini call on an
    article we'd just reject at insert time. The UNIQUE constraints on each
    table still catch concurrent races.
    """
    stmt = union_all(
        select(Fact.external_id).where(Fact.source_name == source_name),
        select(PoolFact.external_id).where(PoolFact.source_name == source_name),
    )
    return set(session.execute(stmt).scalars())


def _model_name() -> str:
    if settings.MODEL_PROVIDER == "gemini":
        return settings.GEMINI_MODEL
    return settings.OLLAMA_MODEL


async def generate_one_pool_fact(session: Session) -> PoolFact:
    category, region, era = random.choice(wikipedia.CATEGORIES)
    logger.info(
        "generation start",
        extra={"extra": {"category": category, "region": region, "era": era}},
    )

    candidates = await wikipedia.list_candidates(category)
    used = get_used_external_ids(session, "wikipedia")
    fresh = [c for c in candidates if str(c.page_id) not in used]
    logger.info(
        "candidates listed",
        extra={
            "extra": {
                "category": category,
                "returned": len(candidates),
                "fresh": len(fresh),
            }
        },
    )

    if not fresh:
        logger.warning(
            "no fresh candidates",
            extra={"extra": {"category": category, "returned": len(candidates)}},
        )
        raise GenerationFailed(
            f"no fresh candidates in {category!r} (all {len(candidates)} already used)"
        )

    random.shuffle(fresh)
    provider = get_provider()
    model_used = f"{settings.MODEL_PROVIDER}:{_model_name()}"

    attempts = 0
    failures: list[str] = []

    for candidate in fresh:
        if attempts >= MAX_CANDIDATE_ATTEMPTS:
            break

        logger.info(
            "candidate attempt",
            extra={
                "extra": {
                    "title": candidate.title,
                    "page_id": candidate.page_id,
                    "attempts_used": attempts,
                }
            },
        )

        try:
            extract = await wikipedia.fetch_extract(candidate.title)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if 400 <= status < 500:
                logger.warning(
                    "wikipedia 4xx, picking different article",
                    extra={
                        "extra": {
                            "title": candidate.title,
                            "status": status,
                        }
                    },
                )
                continue  # no budget cost
            logger.warning(
                "wikipedia non-4xx http error",
                extra={
                    "extra": {
                        "title": candidate.title,
                        "status": status,
                        "error": str(exc),
                    }
                },
            )
            attempts += 1
            failures.append(f"{candidate.title}: wikipedia {status}")
            continue
        except Exception as exc:
            logger.warning(
                "wikipedia fetch_extract failed",
                extra={
                    "extra": {"title": candidate.title, "error": repr(exc)}
                },
            )
            attempts += 1
            failures.append(f"{candidate.title}: wikipedia {exc!r}")
            continue

        try:
            fact_text = await provider.extract_fact(extract.extract)
        except ModelProviderError as exc:
            logger.warning(
                "provider error",
                extra={
                    "extra": {"title": candidate.title, "error": str(exc)}
                },
            )
            attempts += 1
            failures.append(f"{candidate.title}: provider {exc}")
            continue

        if not is_valid(fact_text):
            logger.warning(
                "fact failed validation",
                extra={
                    "extra": {
                        "title": candidate.title,
                        "length": len(fact_text),
                        "fact": fact_text,
                    }
                },
            )
            attempts += 1
            failures.append(
                f"{candidate.title}: invalid (len={len(fact_text)})"
            )
            continue

        row = PoolFact(
            fact_text=fact_text,
            source_name="wikipedia",
            source_url=extract.source_url,
            source_license="CC BY-SA 4.0",
            external_id=str(extract.page_id),
            language="en",
            category=category,
            region=region,
            era=era,
            model_used=model_used,
            prompt_version=settings.PROMPT_VERSION,
            status="pending_review",
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            logger.warning(
                "integrity error (lost race), trying another candidate",
                extra={
                    "extra": {
                        "title": candidate.title,
                        "external_id": str(extract.page_id),
                        "error": str(exc.orig) if exc.orig else str(exc),
                    }
                },
            )
            continue  # no budget cost

        logger.info(
            "generation success",
            extra={
                "extra": {
                    "pool_id": row.id,
                    "title": extract.title,
                    "external_id": row.external_id,
                    "model_used": row.model_used,
                    "fact_preview": row.fact_text[:80],
                }
            },
        )
        return row

    logger.warning(
        "generation failed, budget exhausted",
        extra={
            "extra": {
                "category": category,
                "attempts": attempts,
                "failures": failures,
            }
        },
    )
    raise GenerationFailed(
        f"{category}: {attempts} attempts exhausted. Failures: {failures}"
    )


def is_already_scheduled(session: Session, target_date: date) -> bool:
    """Idempotency guard for schedule_tomorrows_fact — avoids double-scheduling."""
    return (
        session.execute(
            select(Fact.id).where(Fact.scheduled_date == target_date)
        ).first()
        is not None
    )


def recent_facts(session: Session, n: int = 3) -> list[Fact]:
    """The last N scheduled facts, newest first. May return fewer than N (D21b)."""
    return list(
        session.execute(
            select(Fact).order_by(Fact.scheduled_date.desc()).limit(n)
        ).scalars()
    )


def schedule_tomorrows_fact(
    session: Session, target_date: date | None = None
) -> Fact | None:
    """Promote one approved pool row to tomorrow's scheduled Fact (D21a/b).

    - target_date defaults to date.today() + 1 (server runs UTC per D15).
    - If that date is already scheduled, log and return None (idempotent).
    - Selects approved pool rows with FOR UPDATE SKIP LOCKED (D21a) so a
      concurrent scheduler can't grab the same row.
    - Variety picker (D21b): prefer an approved row whose region AND era are
      both absent from the last 3 scheduled facts; fall back to oldest
      approved if no such row exists. When there's 0-3 recent history, the
      filter naturally degrades — empty history -> everything is "preferred".
    - One transaction: insert Fact + delete PoolFact. IntegrityError (unique
      collision — a peer scheduled the same date in parallel) rolls back and
      returns None so the caller can no-op the cron run.
    """
    if target_date is None:
        target_date = date.today() + timedelta(days=1)

    if is_already_scheduled(session, target_date):
        logger.info(
            "already scheduled, idempotent no-op",
            extra={"extra": {"scheduled_date": target_date.isoformat()}},
        )
        return None

    recent = recent_facts(session, 3)
    recent_regions = {f.region for f in recent if f.region is not None}
    recent_eras = {f.era for f in recent if f.era is not None}

    approved_stmt = (
        select(PoolFact)
        .where(PoolFact.status == "approved")
        .order_by(PoolFact.created_at.asc())
        .with_for_update(skip_locked=True)
    )
    approved = list(session.execute(approved_stmt).scalars())

    if not approved:
        logger.warning(
            "no approved pool rows available",
            extra={"extra": {"scheduled_date": target_date.isoformat()}},
        )
        raise NoApprovedPool(
            f"no approved pool rows available for {target_date.isoformat()}"
        )

    if recent:
        preferred = [
            p
            for p in approved
            if p.region not in recent_regions and p.era not in recent_eras
        ]
        if preferred:
            pick = preferred[0]
            variety = "preferred"
        else:
            pick = approved[0]
            variety = "fallback"
    else:
        pick = approved[0]
        variety = "empty-history"

    logger.info(
        "picked approved pool row",
        extra={
            "extra": {
                "pool_id": pick.id,
                "variety": variety,
                "region": pick.region,
                "era": pick.era,
                "recent_regions": sorted(recent_regions),
                "recent_eras": sorted(recent_eras),
                "approved_total": len(approved),
            }
        },
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
    session.add(fact)
    session.delete(pick)
    try:
        session.flush()
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        logger.warning(
            "integrity error on schedule (lost race)",
            extra={
                "extra": {
                    "scheduled_date": target_date.isoformat(),
                    "pool_id": pick.id,
                    "error": str(exc.orig) if exc.orig else str(exc),
                }
            },
        )
        return None

    # D21c: bust any cached /today entry that would have returned stale data
    # for this date. Import locally to avoid a circular import (main imports
    # generation for admin endpoints in Step 8).
    from app.main import invalidate_today_cache

    invalidate_today_cache(target_date)

    logger.info(
        "scheduled fact",
        extra={
            "extra": {
                "fact_id": fact.id,
                "scheduled_date": target_date.isoformat(),
                "pool_id_consumed": pick.id,
                "external_id": fact.external_id,
                "region": fact.region,
                "era": fact.era,
                "variety": variety,
            }
        },
    )
    return fact
