"""Cover letter use cases: listing and quota-guarded generation."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import delete, func, select

import gosha.cover_letter as cl
from gosha.database import get_session
from gosha.domain.errors import (
    GenerationFailedError,
    NoCvError,
    NotFoundError,
    QuotaExceededError,
)
from gosha.models import CoverLetter, Job, User

log = logging.getLogger(__name__)

# Placeholder content for a quota slot that has been claimed but whose
# letter has not come back yet. Never returned to a user: the row is either
# replaced by the real letter or deleted.
_PENDING = "__generating__"


async def _monthly_usage(session, user_id: int) -> int:
    """Letters generated this calendar month, counted in the caller's
    transaction so the count and the claim share one lock."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    result = await session.execute(
        select(func.count(CoverLetter.id)).where(
            CoverLetter.user_id == user_id,
            CoverLetter.created_at >= month_start,
        )
    )
    return result.scalar() or 0


async def _release_pending(user_id: int, job_id: int) -> None:
    """Hand back a claimed slot when generation did not produce a letter."""
    try:
        async with get_session() as session:
            await session.execute(
                delete(CoverLetter).where(
                    CoverLetter.user_id == user_id,
                    CoverLetter.job_id == job_id,
                    CoverLetter.content == _PENDING,
                )
            )
            await session.commit()
    except Exception as exc:  # never mask the original failure
        log.warning("Could not release cover-letter quota slot: %s", exc)


async def list_cover_letters(user_id: int) -> list[tuple[CoverLetter, Job]]:
    async with get_session() as session:
        result = await session.execute(
            select(CoverLetter, Job)
            .join(Job, CoverLetter.job_id == Job.id)
            .where(
                CoverLetter.user_id == user_id,
                # In-flight quota claims are bookkeeping, not letters.
                CoverLetter.content != _PENDING,
            )
            .order_by(CoverLetter.created_at.desc())
        )
        return [(letter, job) for letter, job in result.all()]


async def generate_for_job(user: User, job_id: int) -> tuple[str, bool]:
    """Returns (content, was_cached). Cached re-reads don't consume quota."""
    async with get_session() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise NotFoundError("Job not found.")

        cached = (
            await session.execute(
                select(CoverLetter).where(
                    CoverLetter.user_id == user.id, CoverLetter.job_id == job_id
                )
            )
        ).scalar_one_or_none()

    if cl.load_cv(user.id) is None:
        raise NoCvError("Upload your CV first so the letter has something to work with.")

    if cached is None:
        # Quota is checked under a row lock on the user. Without it this is
        # read-then-act: N concurrent requests all read the same usage
        # count, all see room, and all call the LLM. That is real money on
        # a paid API, and the free tier's "5 per month" was advisory at
        # best. SQLite has no row locks and no concurrency to protect
        # against, so it degrades to the plain check.
        limit = int(user.limits["cover_letters_per_month"])
        async with get_session() as session:
            if session.bind.dialect.name == "postgresql":
                await session.execute(
                    select(User.id).where(User.id == user.id).with_for_update()
                )
            usage = await _monthly_usage(session, user.id)
            if usage >= limit:
                raise QuotaExceededError(
                    f"You've used all {limit} cover letters this month."
                )
            # Claim the slot inside the lock: the row is written before the
            # lock is released, so a concurrent request sees the new count.
            session.add(
                CoverLetter(user_id=user.id, job_id=job_id, content=_PENDING)
            )
            await session.commit()

        try:
            content, was_cached = await cl.generate_cover_letter(
                user.id, job_id, force_regenerate=True,
            )
        except Exception:
            await _release_pending(user.id, job_id)
            raise
        if content is None:
            # Generation failed — give the slot back rather than charging
            # the user for a letter they never received.
            await _release_pending(user.id, job_id)
            raise GenerationFailedError(
                "The AI service is unavailable right now — try again in a bit."
            )
        return content, was_cached

    content, was_cached = await cl.generate_cover_letter(user.id, job_id)
    if content is None:
        raise GenerationFailedError(
            "The AI service is unavailable right now — try again in a bit."
        )
    return content, was_cached
