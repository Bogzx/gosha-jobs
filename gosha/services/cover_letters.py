"""Cover letter use cases: listing and quota-guarded generation."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import delete, func, select

import gosha.cover_letter as cl
from gosha.database import get_session
from gosha.domain.errors import (
    GenerationFailedError,
    GenerationInProgressError,
    NoCvError,
    NotFoundError,
    QuotaExceededError,
)
from gosha.models import CoverLetter, Job, User

log = logging.getLogger(__name__)

_IN_PROGRESS_MESSAGE = (
    "This cover letter is still being written — it will be ready in a moment."
)


async def _monthly_usage(session, user_id: int) -> int:
    """Letters generated this calendar month, counted in the caller's
    transaction so the count and the claim share one lock."""
    result = await session.execute(
        select(func.count(CoverLetter.id)).where(
            CoverLetter.user_id == user_id,
            CoverLetter.created_at >= cl.month_start(),
        )
    )
    return result.scalar() or 0


async def _release_claim(claim_id: int) -> None:
    """Hand back a claimed slot when generation did not produce a letter."""
    try:
        async with get_session() as session:
            await session.execute(
                delete(CoverLetter).where(
                    CoverLetter.id == claim_id,
                    CoverLetter.content == cl.PENDING_CONTENT,
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
                # In-flight quota claims and letters erased with the CV are
                # bookkeeping, not letters.
                CoverLetter.content.notin_([cl.PENDING_CONTENT, cl.ERASED_CONTENT]),
            )
            .order_by(CoverLetter.created_at.desc())
        )
        return [(letter, job) for letter, job in result.all()]


async def _claim_slot(user: User, job_id: int) -> tuple[int | None, str | None]:
    """(claim row id, None), or (None, letter) when one appeared meanwhile.

    Quota is checked under a row lock on the user. Without it this is
    read-then-act: N concurrent requests all read the same usage count, all
    see room, and all call the LLM. That is real money on a paid API, and
    the free tier's "5 per month" was advisory at best. SQLite has no row
    locks and no concurrency to protect against, so it degrades to the
    plain check.
    """
    limit = int(user.limits["cover_letters_per_month"])
    now = datetime.now(timezone.utc)
    async with get_session() as session:
        if session.bind.dialect.name == "postgresql":
            await session.execute(
                select(User.id).where(User.id == user.id).with_for_update()
            )
        # Re-read under the lock: the request that held it may have just
        # claimed or finished this very letter.
        latest = await cl.latest_letter_row(session, user.id, job_id)
        if latest is not None and cl.is_fresh_letter(latest, now):
            return None, latest.content
        if latest is not None and cl.is_in_flight(latest, now):
            raise GenerationInProgressError(_IN_PROGRESS_MESSAGE)
        # Claims left by requests that died mid-generation: the slots go back.
        await session.execute(
            delete(CoverLetter)
            .where(
                CoverLetter.user_id == user.id,
                CoverLetter.content == cl.PENDING_CONTENT,
                CoverLetter.created_at < now - cl.PENDING_TTL,
            )
            # "fetch": drop the deleted rows from the session by primary key.
            # The default evaluates the WHERE in Python, which fails on
            # SQLite's naive datetimes.
            .execution_options(synchronize_session="fetch")
        )
        usage = await _monthly_usage(session, user.id)
        if usage >= limit:
            raise QuotaExceededError(
                f"You've used all {limit} cover letters this month."
            )
        # Claim the slot inside the lock: the row is written before the
        # lock is released, so a concurrent request sees the new count —
        # and, for this job, a letter in progress.
        claim = CoverLetter(
            user_id=user.id, job_id=job_id, content=cl.PENDING_CONTENT,
            created_at=now,
        )
        session.add(claim)
        await session.commit()
        return claim.id, None


async def generate_for_job(user: User, job_id: int) -> tuple[str, bool]:
    """Returns (content, was_cached). Cached re-reads don't consume quota;
    everything else does, including regenerating an expired letter."""
    async with get_session() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise NotFoundError("Job not found.")
        latest = await cl.latest_letter_row(session, user.id, job_id)

    if cl.load_cv(user.id) is None:
        raise NoCvError("Upload your CV first so the letter has something to work with.")

    if latest is not None and cl.is_fresh_letter(latest):
        return latest.content, True
    if latest is not None and cl.is_in_flight(latest):
        raise GenerationInProgressError(_IN_PROGRESS_MESSAGE)

    claim_id, cached = await _claim_slot(user, job_id)
    if cached is not None:
        return cached, True

    try:
        content, _was_cached = await cl.generate_cover_letter(
            user.id, job_id, claim_id=claim_id,
        )
    except Exception:
        await _release_claim(claim_id)
        raise
    if content is None:
        # Generation failed — give the slot back rather than charging
        # the user for a letter they never received.
        await _release_claim(claim_id)
        raise GenerationFailedError(
            "The AI service is unavailable right now — try again in a bit."
        )
    return content, False
