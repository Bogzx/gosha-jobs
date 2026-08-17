"""Job browsing use cases: filtered listing, detail, feedback, apply-click."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from gosha.database import get_session
from gosha.domain.errors import NotFoundError
from gosha.filters import (
    location_matches,
    matches_company_blacklist,
    matches_excluded_keywords,
    matches_experience_level,
    normalize_location,
)
from gosha.models import Application, Job, Subscription, UserJob

log = logging.getLogger(__name__)

MAX_CANDIDATES = 2000


async def get_user_exclusions(user_id: int) -> tuple[list[str], list[str]]:
    """(blacklisted companies, excluded keywords) across ALL the user's searches.

    The contract: blacklisting a company in any search hides it everywhere —
    feed, browse, and DMs. Paused searches still count; the user's intent
    ("never show me X") doesn't pause with the search.
    """
    async with get_session() as session:
        result = await session.execute(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        subs = result.scalars().all()

    blacklist: list[str] = []
    excluded: list[str] = []
    for sub in subs:
        blacklist.extend(sub.company_blacklist)
        excluded.extend(sub.excluded_keywords)
    return blacklist, excluded


def passes_user_exclusions(
    job: Job, blacklist: list[str], excluded: list[str],
) -> bool:
    if blacklist and matches_company_blacklist(job.company, blacklist):
        return False
    if excluded and matches_excluded_keywords(
        f"{job.title} {job.description or ''}", excluded
    ):
        return False
    return True


@dataclass(frozen=True)
class JobFilters:
    q: str = ""
    locations: list[str] = field(default_factory=list)
    experience: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    salary_min: int | None = None
    posted_within_days: int | None = None
    remote: bool = False


async def list_jobs(
    user_id: int, filters: JobFilters, page: int, per_page: int,
) -> tuple[list[Job], int]:
    """Active jobs matching the filters, newest first, plus total count."""
    stmt = select(Job).where(
        Job.is_active.is_(True),
        # Hide non-canonical cross-board duplicates
        or_(Job.dedup_group_id.is_(None), Job.dedup_group_id == Job.id),
    )

    if filters.q.strip():
        needle = f"%{filters.q.strip()}%"
        stmt = stmt.where(or_(Job.title.ilike(needle), Job.company.ilike(needle)))
    if filters.sources:
        stmt = stmt.where(Job.source.in_([s.lower() for s in filters.sources]))
    if filters.salary_min is not None:
        # Compared on the normalised monthly-RON columns, not the raw
        # amounts: those mix monthly RON, monthly EUR and annual USD, so
        # the old comparison let a USD 3,000/YEAR listing through a
        # "3000 minimum" filter while excluding EUR 2,500/month.
        stmt = stmt.where(
            or_(
                Job.salary_monthly_max_ron >= filters.salary_min,
                Job.salary_monthly_min_ron >= filters.salary_min,
                # No usable salary data — benefit of the doubt. Most
                # Romanian postings omit salary; excluding them would empty
                # the feed for anyone who touches this filter.
                Job.salary_monthly_min_ron.is_(None)
                & Job.salary_monthly_max_ron.is_(None),
            )
        )
    if filters.posted_within_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=filters.posted_within_days)
        stmt = stmt.where(Job.first_seen_at >= cutoff)

    stmt = stmt.order_by(Job.first_seen_at.desc()).limit(MAX_CANDIDATES)

    async with get_session() as session:
        result = await session.execute(stmt)
        candidates = list(result.scalars().all())

    # The user's standing exclusions (blacklists/excluded words from any of
    # their searches) apply to browsing too — "never show me X" means never.
    blacklist, excluded = await get_user_exclusions(user_id)
    if blacklist or excluded:
        candidates = [
            j for j in candidates if passes_user_exclusions(j, blacklist, excluded)
        ]

    # Location + experience need the alias/regex helpers (post-SQL)
    if filters.locations or filters.remote:
        match_subs: list[str] = []
        for loc in filters.locations:
            _search, subs = normalize_location(loc)
            match_subs.extend(subs)
        if filters.remote:
            _search, remote_subs = normalize_location("remote")
            match_subs.extend(remote_subs)
        candidates = [
            j for j in candidates if location_matches(j.location, match_subs)
        ]

    if filters.experience:
        levels = [e.lower() for e in filters.experience]
        candidates = [
            j for j in candidates if matches_experience_level(j.title, levels)
        ]

    total = len(candidates)
    start = (page - 1) * per_page
    return candidates[start : start + per_page], total


async def backfill_salary_normalisation(limit: int = 2000) -> int:
    """Fill salary_monthly_*_ron for rows that predate normalisation.

    Upserts compute these going forward, but a posting that is never
    re-seen would otherwise stay invisible to the salary filter forever.
    Returns the number of rows updated.
    """
    from gosha.salary import normalise_range

    async with get_session() as session:
        result = await session.execute(
            select(Job)
            .where(
                Job.salary_monthly_min_ron.is_(None),
                Job.salary_monthly_max_ron.is_(None),
                or_(Job.salary_min.isnot(None), Job.salary_max.isnot(None)),
            )
            .limit(limit)
        )
        jobs = list(result.scalars().all())
        if not jobs:
            return 0

        updated = 0
        for job in jobs:
            low, high = normalise_range(
                job.salary_min, job.salary_max, job.salary_currency, job.salary_period,
            )
            if low is None and high is None:
                continue
            job.salary_monthly_min_ron = low
            job.salary_monthly_max_ron = high
            updated += 1
        await session.commit()

    log.info("Backfilled salary normalisation for %d jobs", updated)
    return updated


async def get_job(job_id: int) -> Job:
    async with get_session() as session:
        job = await session.get(Job, job_id)
    if job is None:
        raise NotFoundError("Job not found.")
    return job


async def annotate_user_state(
    user_id: int, job_ids: list[int],
) -> tuple[dict[int, str], set[int]]:
    """Return ({job_id: feedback}, {applied job_ids}) for the given jobs."""
    if not job_ids:
        return {}, set()
    async with get_session() as session:
        fb_rows = await session.execute(
            select(UserJob.job_id, UserJob.feedback).where(
                UserJob.user_id == user_id,
                UserJob.job_id.in_(job_ids),
                UserJob.feedback.isnot(None),
            )
        )
        feedback = {job_id: fb for job_id, fb in fb_rows.all()}
        app_rows = await session.execute(
            select(Application.job_id).where(
                Application.user_id == user_id, Application.job_id.in_(job_ids)
            )
        )
        applied = {row[0] for row in app_rows.all()}
    return feedback, applied


async def set_feedback(user_id: int, job_id: int, feedback: str) -> None:
    """Record 👍/👎; creates the user_jobs row when the web user was never DMed."""
    await get_job(job_id)
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        uj = (
            await session.execute(
                select(UserJob).where(
                    UserJob.user_id == user_id, UserJob.job_id == job_id
                )
            )
        ).scalar_one_or_none()
        if uj is None:
            uj = UserJob(user_id=user_id, job_id=job_id)
            session.add(uj)
        uj.feedback = feedback
        uj.feedback_at = now
        await session.commit()

    try:
        from gosha.events import emit_user_feedback
        await emit_user_feedback(job_id, user_id, feedback)
    except Exception:
        pass


async def record_apply_click(user_id: int, job_id: int) -> tuple[Application, Job]:
    """Idempotently track an application when the user clicks Apply."""
    job = await get_job(job_id)

    async with get_session() as session:
        app = (
            await session.execute(
                select(Application).where(
                    Application.user_id == user_id, Application.job_id == job_id
                )
            )
        ).scalar_one_or_none()
        created = app is None
        if app is None:
            app = Application(user_id=user_id, job_id=job_id, source="web")
            session.add(app)
            await session.commit()

    if created:
        try:
            from gosha.events import get_event_store
            await get_event_store().emit(
                "web.apply_click", actor_id=user_id, job_id=job_id
            )
        except Exception:
            pass

    return app, job
