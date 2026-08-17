"""Account-level GDPR use cases: export everything, erase everything.

Articles 15 (access) and 17 (erasure) are not optional for a service
holding EU students' CVs, and "delete your CV" was never the same promise
as "delete my account" — cover letters, delivery history, applications and
the analytics event log all outlived the CV they were derived from.

Both operations are deliberately implemented as explicit statements rather
than ORM cascades: the `events` table has no foreign keys (events are
immutable by design) and CV text lives on the filesystem, so a cascade
would silently miss both.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select

import gosha.cover_letter as cv_storage
from gosha.database import get_session
from gosha.events import Event
from gosha.models import (
    Application,
    CoverLetter,
    Outbox,
    Subscription,
    User,
    UserJob,
)

log = logging.getLogger(__name__)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


async def export_account(user_id: int) -> dict:
    """Everything held about one user, as portable JSON (GDPR Art. 15/20).

    Job postings themselves are public data scraped from job boards, so
    only the identifiers and the user's own relationship to them are
    included — not a copy of the whole corpus.
    """
    async with get_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return {}

        subs = (
            await session.execute(
                select(Subscription).where(Subscription.user_id == user_id)
            )
        ).scalars().all()
        user_jobs = (
            await session.execute(
                select(UserJob).where(UserJob.user_id == user_id)
            )
        ).scalars().all()
        applications = (
            await session.execute(
                select(Application).where(Application.user_id == user_id)
            )
        ).scalars().all()
        letters = (
            await session.execute(
                select(CoverLetter).where(CoverLetter.user_id == user_id)
            )
        ).scalars().all()
        events = (
            await session.execute(
                select(Event).where(Event.actor_id == user_id)
                .order_by(Event.timestamp.asc())
            )
        ).scalars().all()

        payload = {
            "exported_at": _iso(datetime.now().astimezone()),
            "account": {
                "id": user.id,
                "discord_user_id": str(user.discord_user_id),
                "username": user.username,
                "avatar_url": user.avatar_url,
                "tier": user.tier,
                "in_guild": user.in_guild,
                "created_at": _iso(user.created_at),
                "last_login_at": _iso(user.last_login_at),
                "has_cv_embedding": user.cv_embedding is not None,
            },
            "cv_text": cv_storage.load_cv(user_id),
            "searches": [
                {
                    "id": s.id,
                    "name": s.name,
                    "keywords": s.keywords,
                    "locations": s.locations,
                    "excluded_keywords": s.excluded_keywords,
                    "company_blacklist": s.company_blacklist,
                    "experience_levels": s.experience_levels,
                    "boards": s.boards,
                    "remote_ok": s.remote_ok,
                    "salary_min": s.salary_min,
                    "max_age_days": s.max_age_days,
                    "is_active": s.is_active,
                    "notify_discord": s.notify_discord,
                    "created_at": _iso(s.created_at),
                }
                for s in subs
            ],
            "delivered_jobs": [
                {
                    "job_id": uj.job_id,
                    "subscription_id": uj.subscription_id,
                    "relevance_score": uj.relevance_score,
                    "delivered_at": _iso(uj.delivered_at),
                    "feedback": uj.feedback,
                    "feedback_at": _iso(uj.feedback_at),
                }
                for uj in user_jobs
            ],
            "applications": [
                {
                    "job_id": a.job_id,
                    "status": a.status,
                    "notes": a.notes,
                    "source": a.source,
                    "applied_at": _iso(a.applied_at),
                    "updated_at": _iso(a.updated_at),
                }
                for a in applications
            ],
            "cover_letters": [
                {
                    "job_id": c.job_id,
                    "content": c.content,
                    "created_at": _iso(c.created_at),
                }
                for c in letters
            ],
            "events": [
                {
                    "type": e.event_type,
                    "timestamp": _iso(e.timestamp),
                    "job_id": e.job_id,
                    "subscription_id": e.subscription_id,
                    "payload": e.payload,
                }
                for e in events
            ],
        }

    return payload


async def delete_account(user_id: int) -> dict[str, int]:
    """Erase a user and everything derived from them (GDPR Art. 17).

    Returns per-table counts so the caller can log/confirm what went. The
    CV file is removed first: if the DB transaction fails afterwards the
    user can re-upload, whereas an orphaned CV on disk is the outcome that
    actually matters.
    """
    removed: dict[str, int] = {}

    cv_deleted = cv_storage.delete_cv(user_id)
    removed["cv_files"] = 1 if cv_deleted else 0

    async with get_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return removed

        for label, statement in (
            ("cover_letters", delete(CoverLetter).where(CoverLetter.user_id == user_id)),
            ("applications", delete(Application).where(Application.user_id == user_id)),
            ("delivered_jobs", delete(UserJob).where(UserJob.user_id == user_id)),
            ("outbox", delete(Outbox).where(Outbox.user_id == user_id)),
            ("searches", delete(Subscription).where(Subscription.user_id == user_id)),
            # Events carry actor_id but no FK — a cascade would leave them.
            ("events", delete(Event).where(Event.actor_id == user_id)),
        ):
            result = await session.execute(statement)
            removed[label] = int(result.rowcount or 0)

        # Bulk statement rather than session.delete(user): the ORM
        # relationship cascades would re-issue deletes for rows the
        # statements above already removed.
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()
        removed["account"] = 1

    log.info("Deleted account %d: %s", user_id, removed)
    return removed
