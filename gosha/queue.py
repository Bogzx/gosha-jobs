"""DB-backed job queue for decoupling scraping, matching, and delivery.

Three queue stages:
  1. SCRAPED  — raw jobs stored, waiting to be matched to subscriptions
  2. MATCHED  — job matched to a user/subscription, waiting for delivery
  3. DELIVERED — delivery complete (terminal state)
  4. FAILED   — delivery failed (can be retried)

Using the jobs and user_jobs tables as the queue avoids adding Redis/RabbitMQ
while keeping stages decoupled.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import func, select

from gosha.database import get_session
from gosha.models import Job, User, UserJob

log = logging.getLogger(__name__)


class DeliveryStatus(str, Enum):
    PENDING = "pending"      # Matched, not yet delivered
    DELIVERED = "delivered"  # Successfully sent to user
    FAILED = "failed"        # Delivery attempt failed


# ---------------------------------------------------------------------------
# Stage 1: Scrape queue — mark freshly upserted jobs as needing matching
# ---------------------------------------------------------------------------
# Jobs are "in the scrape queue" implicitly when last_seen_at is recent
# and they haven't been matched yet for a given subscription cycle.
# No extra column needed — the pipeline queries by last_seen_at window.


async def get_fresh_jobs(since: datetime) -> list[Job]:
    """Return all jobs seen since the given timestamp."""
    async with get_session() as session:
        result = await session.execute(
            select(Job)
            .where(Job.last_seen_at >= since, Job.is_active.is_(True))
            .order_by(Job.last_seen_at.desc())
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Stage 2: Match queue — create pending delivery records
# ---------------------------------------------------------------------------


async def enqueue_delivery(
    user_id: int,
    job_id: int,
    subscription_id: int | None = None,
    relevance_score: float | None = None,
) -> UserJob | None:
    """Create a pending delivery record. Returns None if already exists."""
    async with get_session() as session:
        # Check for existing delivery (any status)
        existing = await session.execute(
            select(UserJob).where(
                UserJob.user_id == user_id,
                UserJob.job_id == job_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return None

        uj = UserJob(
            user_id=user_id,
            job_id=job_id,
            subscription_id=subscription_id,
            relevance_score=relevance_score,
            dm_status=UserJob.DM_PENDING,
        )
        session.add(uj)
        await session.commit()
        return uj


async def enqueue_deliveries_batch(
    items: list[dict],
) -> int:
    """Batch-enqueue multiple delivery records.

    Each item: {"user_id": int, "job_id": int, "subscription_id": int|None, "score": float|None}
    Returns the number of new records created (skips duplicates).
    """
    if not items:
        return 0

    created = 0
    async with get_session() as session:
        # Batch check for existing deliveries
        pairs = [(item["user_id"], item["job_id"]) for item in items]
        user_ids = [p[0] for p in pairs]
        job_ids = [p[1] for p in pairs]

        existing_result = await session.execute(
            select(UserJob.user_id, UserJob.job_id).where(
                UserJob.user_id.in_(set(user_ids)),
                UserJob.job_id.in_(set(job_ids)),
            )
        )
        existing_pairs = {(row[0], row[1]) for row in existing_result.all()}

        for item in items:
            pair = (item["user_id"], item["job_id"])
            if pair in existing_pairs:
                continue

            uj = UserJob(
                user_id=item["user_id"],
                job_id=item["job_id"],
                subscription_id=item.get("subscription_id"),
                relevance_score=item.get("score"),
                # The deliver stage DMs pending rows only (UserJob.dm_status).
                dm_status=UserJob.DM_PENDING,
            )
            session.add(uj)
            existing_pairs.add(pair)  # Prevent in-batch duplicates
            created += 1

        if created > 0:
            await session.commit()

    log.info("Enqueued %d new deliveries (skipped %d existing)", created, len(items) - created)
    return created


# ---------------------------------------------------------------------------
# Stage 3: Delivery queue — get pending items, mark as delivered/failed
# ---------------------------------------------------------------------------


async def get_pending_deliveries(limit: int = 100) -> list[tuple[UserJob, Job, User]]:
    """Return pending deliveries (enqueued but not yet sent), oldest first."""
    async with get_session() as session:
        result = await session.execute(
            select(UserJob, Job, User)
            .join(Job, UserJob.job_id == Job.id)
            .join(User, UserJob.user_id == User.id)
            .where(UserJob.feedback.is_(None))
            .where(UserJob.delivered_at.is_(None))
            .order_by(UserJob.id.asc())
            .limit(limit)
        )
        return list(result.all())


async def mark_delivered(user_job_id: int) -> None:
    """Mark a UserJob as successfully delivered by setting delivered_at."""
    async with get_session() as session:
        result = await session.execute(
            select(UserJob).where(UserJob.id == user_job_id)
        )
        uj = result.scalar_one_or_none()
        if uj is not None and uj.delivered_at is None:
            uj.delivered_at = datetime.now(timezone.utc)
            await session.commit()


async def get_delivery_stats() -> dict:
    """Return delivery queue statistics."""
    async with get_session() as session:
        total = await session.execute(select(func.count(UserJob.id)))
        total_count = total.scalar() or 0

        with_feedback = await session.execute(
            select(func.count(UserJob.id)).where(UserJob.feedback != None)  # noqa: E711
        )
        feedback_count = with_feedback.scalar() or 0

        interested = await session.execute(
            select(func.count(UserJob.id)).where(UserJob.feedback == "interested")
        )
        interested_count = interested.scalar() or 0

    return {
        "total_delivered": total_count,
        "with_feedback": feedback_count,
        "interested": interested_count,
        "not_relevant": feedback_count - interested_count,
    }
