"""Job browsing use cases: filtered listing, detail, feedback, apply-click."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

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
from gosha.services.applications import track_application

log = logging.getLogger(__name__)

# Safety valve on the rows one browse request scans (see list_jobs). Far
# above the active corpus; past it the oldest matches go unlisted and
# `total` undercounts, which is logged.
MAX_SCAN = 50_000
# Descriptions are needed only for excluded keywords, loaded this many
# ids at a time.
_DESCRIPTION_CHUNK = 500


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


def _location_predicate(filters: JobFilters) -> Callable[[str | None], bool] | None:
    """location_matches for the requested places, memoised per location
    string (a scan sees the same few hundred strings thousands of times)."""
    if not (filters.locations or filters.remote):
        return None
    match_subs: list[str] = []
    for loc in filters.locations:
        _search, subs = normalize_location(loc)
        match_subs.extend(subs)
    if filters.remote:
        _search, remote_subs = normalize_location("remote")
        match_subs.extend(remote_subs)

    seen: dict[str | None, bool] = {}

    def matches(location: str | None) -> bool:
        if location not in seen:
            seen[location] = location_matches(location, match_subs)
        return seen[location]

    return matches


async def _without_excluded_keywords(ids: list[int], excluded: list[str]) -> list[int]:
    """`ids` minus jobs whose title or description has an excluded keyword."""
    hits: set[int] = set()
    async with get_session() as session:
        for i in range(0, len(ids), _DESCRIPTION_CHUNK):
            rows = await session.execute(
                select(Job.id, Job.title, Job.description).where(
                    Job.id.in_(ids[i : i + _DESCRIPTION_CHUNK])
                )
            )
            hits.update(
                row.id for row in rows
                if matches_excluded_keywords(f"{row.title} {row.description or ''}", excluded)
            )
    return [job_id for job_id in ids if job_id not in hits]


async def list_jobs(
    user_id: int, filters: JobFilters, page: int, per_page: int,
) -> tuple[list[Job], int]:
    """Active jobs matching the filters, newest first, plus total count.

    Location, experience and the user's standing exclusions need Python
    helpers (diacritic folding, regexes), so they used to run over only
    the newest 2,000 full rows: older matches vanished and `total` was
    wrong (100 Iasi jobs behind 2,000 newer Cluj ones: "Iasi" found none).
    They now run over a slim scan of every SQL match, and full rows are
    loaded for the requested page only.
    """
    conditions = [
        Job.is_active.is_(True),
        # Hide non-canonical cross-board duplicates
        or_(Job.dedup_group_id.is_(None), Job.dedup_group_id == Job.id),
    ]
    if filters.q.strip():
        needle = f"%{filters.q.strip()}%"
        conditions.append(or_(Job.title.ilike(needle), Job.company.ilike(needle)))
    if filters.sources:
        conditions.append(Job.source.in_([s.lower() for s in filters.sources]))
    if filters.salary_min is not None:
        # Compared on the normalised monthly-RON columns, not the raw
        # amounts: those mix monthly RON, monthly EUR and annual USD, so
        # the old comparison let a USD 3,000/YEAR listing through a
        # "3000 minimum" filter while excluding EUR 2,500/month.
        conditions.append(
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
        conditions.append(Job.first_seen_at >= cutoff)
    newest_first = (Job.first_seen_at.desc(), Job.id.desc())
    start = (page - 1) * per_page

    # The user's standing exclusions (blacklists/excluded words from any of
    # their searches) apply to browsing too — "never show me X" means never.
    blacklist, excluded = await get_user_exclusions(user_id)
    location_ok = _location_predicate(filters)
    levels = [e.lower() for e in filters.experience]

    if not (blacklist or excluded or location_ok or levels):
        # Nothing for Python to decide: count and page in SQL.
        async with get_session() as session:
            total = (await session.execute(
                select(func.count(Job.id)).where(*conditions)
            )).scalar_one()
            jobs = (await session.execute(
                select(Job).where(*conditions).order_by(*newest_first)
                .offset(start).limit(per_page)
            )).scalars().all()
        return list(jobs), total

    async with get_session() as session:
        rows = (await session.execute(
            select(Job.id, Job.title, Job.company, Job.location, Job.experience_level)
            .where(*conditions).order_by(*newest_first).limit(MAX_SCAN)
        )).all()
    if len(rows) == MAX_SCAN:
        log.warning("Browse scan hit MAX_SCAN=%d; older matches are not listed", MAX_SCAN)

    ids = [
        row.id for row in rows
        if (not blacklist or not matches_company_blacklist(row.company, blacklist))
        and (location_ok is None or location_ok(row.location))
        and (not levels or matches_experience_level(row.title, levels, row.experience_level))
    ]
    if excluded:
        ids = await _without_excluded_keywords(ids, excluded)

    page_ids = ids[start : start + per_page]
    if not page_ids:
        return [], len(ids)
    async with get_session() as session:
        by_id = {
            job.id: job for job in (await session.execute(
                select(Job).where(Job.id.in_(page_ids))
            )).scalars()
        }
    return [by_id[job_id] for job_id in page_ids if job_id in by_id], len(ids)


# Where the salary backfill resumes, in-process. Rows it cannot normalise
# (an unknown currency) stay NULL and keep matching its query, so without a
# cursor every run re-read the same first `limit` of them and the fillable
# rows behind those were never reached.
_salary_backfill_cursor = 0


async def backfill_salary_normalisation(limit: int = 2000) -> int:
    """Fill salary_monthly_*_ron for rows that predate normalisation.

    Upserts compute these going forward, but a posting that is never
    re-seen would otherwise stay invisible to the salary filter forever.
    Each run continues after the last row the previous one looked at and
    wraps around at the end. Returns the number of rows updated.
    """
    from gosha.salary import normalise_range

    global _salary_backfill_cursor
    async with get_session() as session:
        result = await session.execute(
            select(Job)
            .where(
                Job.id > _salary_backfill_cursor,
                Job.salary_monthly_min_ron.is_(None),
                Job.salary_monthly_max_ron.is_(None),
                # normalise_range ignores amounts <= 0 — no point reading them
                or_(Job.salary_min > 0, Job.salary_max > 0),
            )
            .order_by(Job.id)
            .limit(limit)
        )
        jobs = list(result.scalars().all())
        _salary_backfill_cursor = jobs[-1].id if len(jobs) == limit else 0
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


async def _find_user_job(session, user_id: int, job_id: int) -> UserJob | None:
    return (
        await session.execute(
            select(UserJob).where(UserJob.user_id == user_id, UserJob.job_id == job_id)
        )
    ).scalar_one_or_none()


async def set_feedback(user_id: int, job_id: int, feedback: str) -> None:
    """Record 👍/👎; creates the user_jobs row when the web user was never DMed."""
    await get_job(job_id)
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        # Two passes at most: when a concurrent request (double-click, or a
        # DM delivery) inserts the row between our select and our insert,
        # uq_user_job_delivery rejects ours and the second pass updates
        # theirs instead of answering 500.
        for attempt in range(2):
            uj = await _find_user_job(session, user_id, job_id)
            if uj is None:
                uj = UserJob(user_id=user_id, job_id=job_id)
                session.add(uj)
            uj.feedback = feedback
            uj.feedback_at = now
            try:
                await session.commit()
                break
            except IntegrityError:
                await session.rollback()
                if attempt:
                    raise

    try:
        from gosha.events import emit_user_feedback
        await emit_user_feedback(job_id, user_id, feedback)
    except Exception:
        pass


async def record_apply_click(user_id: int, job_id: int) -> tuple[Application, Job]:
    """Idempotently track an application when the user clicks Apply."""
    job = await get_job(job_id)
    app, created = await track_application(user_id, job_id, "web")

    if created:
        try:
            from gosha.events import get_event_store
            await get_event_store().emit(
                "web.apply_click", actor_id=user_id, job_id=job_id
            )
        except Exception:
            pass

    return app, job
