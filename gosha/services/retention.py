"""CV retention: delete the CVs of users who have stopped using GOSHA.

"Until the user deletes it" is not a retention period; a CV uploaded by a
student who graduated and never came back would otherwise be kept forever.
Once a user has been inactive for CV_RETENTION_MONTHS (default 12; 0
disables), their CV is removed along with everything derived from it — the
same path as a user-initiated delete (services/cv.delete_cv): the file,
the CV embedding, cover letters, and the recorded consent. The account,
saved searches and tracker stay.

"Activity" counts only things the user did: account creation, web sign-in,
uploading the CV, feedback on a job, tracking an application, generating a
cover letter, creating a search, and user-attributed events (`user.*`,
`subscription.*`, `web.*`). Deliveries and matches (`job.*` events) are
the system acting on the user and do not count — otherwise every DM would
keep a CV alive indefinitely.

Event retention (EVENT_RETENTION_MONTHS, default 0 = keep) is the same
idea for the event log — page views, deliveries, sign-ins — which is
otherwise kept until the account is deleted. It is opt-in because it
deletes analytics history, and it never undercuts CV retention, which
reads user activity from those events.

Both run daily from the bot's scheduler; `scripts/cv_retention.py
--dry-run` shows what they would delete.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select

import gosha.cover_letter as cv_storage
from gosha.database import get_session
from gosha.events import Event
from gosha.models import Application, CoverLetter, Subscription, User, UserJob

log = logging.getLogger(__name__)

RETENTION_ENV = "CV_RETENTION_MONTHS"
DEFAULT_RETENTION_MONTHS = 12
EVENT_RETENTION_ENV = "EVENT_RETENTION_MONTHS"
DAYS_PER_MONTH = 30  # a retention window, not a calendar computation

USER_EVENT_PREFIXES = ("user.", "subscription.", "web.")


def _months_from_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        months = int(raw)
    except ValueError:
        log.error(
            "%s=%r is not an integer; using the default of %d months",
            name, raw, default,
        )
        return default
    return max(0, months)


def retention_months() -> int:
    """Configured CV window in months; 0 means CV retention is disabled."""
    return _months_from_env(RETENTION_ENV, DEFAULT_RETENTION_MONTHS)


def event_retention_months() -> int:
    """Event-log window in months, as enforced; 0 means keep everything.

    Never shorter than the CV window: last_activity() reads sign-ins and
    other user events from this table, so pruning them first would make
    active users look inactive and delete their CVs early.
    """
    months = _months_from_env(EVENT_RETENTION_ENV, 0)
    if months and retention_months():
        return max(months, retention_months())
    return months


def _utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything here is UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass
class RetentionReport:
    months: int
    cutoff: datetime | None
    dry_run: bool
    checked: int = 0
    # (user_id, last activity) for every CV past the cutoff
    expired: list[tuple[int, datetime | None]] = field(default_factory=list)
    deleted: int = 0

    def summary(self) -> str:
        if self.cutoff is None:
            return "CV retention disabled (CV_RETENTION_MONTHS=0)."
        verb = "would delete" if self.dry_run else "deleted"
        return (
            f"CV retention ({self.months} months, cutoff "
            f"{self.cutoff:%Y-%m-%d}): checked {self.checked} CV(s), "
            f"{verb} {len(self.expired) if self.dry_run else self.deleted}."
        )


async def _max_by_user(session, column, user_col, user_ids, *where) -> dict[int, datetime]:
    rows = await session.execute(
        select(user_col, func.max(column))
        .where(user_col.in_(user_ids), *where)
        .group_by(user_col)
    )
    return {uid: ts for uid, ts in rows.all() if ts is not None}


async def last_activity(user_ids: list[int]) -> dict[int, datetime | None]:
    """Most recent user-initiated activity per user (None if never seen)."""
    if not user_ids:
        return {}
    latest: dict[int, datetime | None] = {uid: None for uid in user_ids}

    def bump(uid: int, ts: datetime | None) -> None:
        ts = _utc(ts)
        if ts is not None and (latest[uid] is None or ts > latest[uid]):
            latest[uid] = ts

    async with get_session() as session:
        users = await session.execute(
            select(User.id, User.created_at, User.last_login_at)
            .where(User.id.in_(user_ids))
        )
        for uid, created, login in users.all():
            bump(uid, created)
            bump(uid, login)

        sources = [
            (UserJob.feedback_at, UserJob.user_id, ()),
            (Application.updated_at, Application.user_id, ()),
            (CoverLetter.created_at, CoverLetter.user_id, ()),
            (Subscription.created_at, Subscription.user_id, ()),
            (
                Event.timestamp, Event.actor_id,
                (or_(*(Event.event_type.startswith(p) for p in USER_EVENT_PREFIXES)),),
            ),
        ]
        for column, user_col, where in sources:
            for uid, ts in (
                await _max_by_user(session, column, user_col, user_ids, *where)
            ).items():
                bump(uid, ts)

    # Uploading (or re-uploading) the CV is activity too.
    for uid in user_ids:
        path = cv_storage.get_cv_path(uid)
        try:
            bump(uid, datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
        except FileNotFoundError:
            pass
    return latest


async def _users_holding_cv_data() -> tuple[list[int], set[int]]:
    """(ids holding CV data, ids of those with no user row).

    CV data = a CV file, a CV vector, or cover letters derived from a CV.
    """
    ids = set(cv_storage.stored_cv_user_ids())
    async with get_session() as session:
        ids.update(
            uid for (uid,) in (await session.execute(
                select(User.id).where(User.cv_embedding.isnot(None))
            )).all()
        )
        ids.update(
            uid for (uid,) in (await session.execute(
                select(CoverLetter.user_id).distinct()
            )).all()
        )
        known = {
            uid for (uid,) in (await session.execute(
                select(User.id).where(User.id.in_(ids))
            )).all()
        } if ids else set()
    orphans = ids - known
    if orphans:
        # A CV file with no user row (e.g. left over from a failed account
        # erase) belongs to nobody; it is always past retention.
        log.warning("CV data for %d unknown user id(s): %s", len(orphans), sorted(orphans))
    return sorted(ids), orphans


async def purge_stale_cvs(
    months: int | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> RetentionReport:
    """Delete CV data of users inactive for longer than the window."""
    months = retention_months() if months is None else max(0, months)
    if months == 0:
        return RetentionReport(months=0, cutoff=None, dry_run=dry_run)

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=months * DAYS_PER_MONTH)
    report = RetentionReport(months=months, cutoff=cutoff, dry_run=dry_run)

    user_ids, orphans = await _users_holding_cv_data()
    report.checked = len(user_ids)
    activity = await last_activity(sorted(set(user_ids) - orphans))
    report.expired = [
        (uid, activity.get(uid))
        for uid in user_ids
        if uid in orphans or activity.get(uid) is None or activity[uid] < cutoff
    ]

    if not dry_run:
        from gosha.services.cv import delete_cv

        for uid, last_seen in report.expired:
            await delete_cv(uid)
            report.deleted += 1
            log.info(
                "CV retention: deleted CV data of user %d (last active %s)",
                uid, f"{last_seen:%Y-%m-%d}" if last_seen else "never",
            )

    log.info(report.summary())
    return report


@dataclass
class EventRetentionReport:
    months: int
    cutoff: datetime | None
    dry_run: bool
    expired: int = 0
    deleted: int = 0

    def summary(self) -> str:
        if self.cutoff is None:
            return "Event retention disabled (EVENT_RETENTION_MONTHS=0)."
        verb = "would delete" if self.dry_run else "deleted"
        count = self.expired if self.dry_run else self.deleted
        return (
            f"Event retention ({self.months} months, cutoff "
            f"{self.cutoff:%Y-%m-%d}): {verb} {count} event(s)."
        )


async def purge_old_events(
    dry_run: bool = False,
    now: datetime | None = None,
) -> EventRetentionReport:
    """Delete events older than event_retention_months() (0: do nothing)."""
    months = event_retention_months()
    if months == 0:
        return EventRetentionReport(months=0, cutoff=None, dry_run=dry_run)

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=months * DAYS_PER_MONTH)
    report = EventRetentionReport(months=months, cutoff=cutoff, dry_run=dry_run)
    async with get_session() as session:
        report.expired = (await session.execute(
            select(func.count()).select_from(Event).where(Event.timestamp < cutoff)
        )).scalar_one()
        if not dry_run and report.expired:
            result = await session.execute(delete(Event).where(Event.timestamp < cutoff))
            await session.commit()
            report.deleted = result.rowcount or 0

    log.info(report.summary())
    return report
