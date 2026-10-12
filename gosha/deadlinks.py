"""Dead-link detection: postings that 404 get marked inactive.

Job boards remove filled postings; keeping them in the feed wastes the
user's time. A daily job samples active postings older than MIN_AGE_DAYS
(never-checked first, then stalest) and deactivates the ones that are
definitively gone (404/410). Network errors are inconclusive — the job
stays active and gets retried on a later run.

Boards that wall off bots (UNCHECKABLE_SOURCES) are left out of the sample.
Their postings, like any posting the scrapes have stopped returning, expire
once no scrape has seen them for UNSEEN_EXPIRY_DAYS (expire_unseen_jobs,
run at the start of the same daily job).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update

from gosha.database import get_session
from gosha.models import Job
from gosha.scrapers.base import DEFAULT_HEADERS

log = logging.getLogger(__name__)

MIN_AGE_DAYS = 7
SAMPLE_SIZE = 200
CHECK_TIMEOUT = 10.0
DEAD_STATUSES = {404, 410}

# Boards that answer an unauthenticated client with a bot wall (LinkedIn's
# 999, Indeed's and Glassdoor's 403/429) for live and removed postings
# alike, so a check never comes back 404/410. In the sample they only spent
# the daily budget: the boards whose checks do work (eJobs, BestJobs, Hipo,
# RemoteOK, Jobicy) waited weeks between checks. Values of Job.source.
UNCHECKABLE_SOURCES = frozenset({"linkedin", "indeed", "glassdoor"})

# A posting no scrape has returned for this long is treated as gone.
# upsert_jobs bumps last_seen_at (and reactivates the row) whenever any
# search returns it again, so a wrong call undoes itself. Searches only
# return recent postings, so for the boards above this is in effect a
# lifetime of about this long after the posting drops out of the results.
UNSEEN_EXPIRY_DAYS = 30


async def _check_url_status(client: httpx.AsyncClient, url: str) -> int | None:
    """HTTP status for the posting URL; None when the check is inconclusive."""
    try:
        resp = await client.head(url, follow_redirects=True)
        if resp.status_code == 405:  # some boards reject HEAD
            resp = await client.get(url, follow_redirects=True)
        return resp.status_code
    except httpx.HTTPError:
        return None


async def expire_unseen_jobs(max_unseen_days: int = UNSEEN_EXPIRY_DAYS) -> int:
    """Deactivate active jobs no scrape has returned for `max_unseen_days`.

    Bulk, and without a job.expired event per row: the first run on an old
    database can expire thousands of rows at once.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_unseen_days)
    async with get_session() as session:
        result = await session.execute(
            update(Job)
            .where(
                Job.is_active.is_(True),
                Job.last_seen_at < cutoff,
            )
            .values(is_active=False)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    expired = result.rowcount or 0
    if expired:
        log.info("Expired %d jobs not seen by any scrape for %d days", expired, max_unseen_days)
    return expired


async def check_dead_links(sample_size: int = SAMPLE_SIZE) -> int:
    """Expire unseen jobs, then check a sample of aging active ones.

    Returns how many were expired, by either rule.
    """
    # First, so postings that are gone anyway do not take sample slots.
    unseen = await expire_unseen_jobs()
    cutoff = datetime.now(timezone.utc) - timedelta(days=MIN_AGE_DAYS)

    async with get_session() as session:
        result = await session.execute(
            select(Job)
            .where(
                Job.is_active.is_(True),
                Job.first_seen_at < cutoff,
                Job.source.notin_(UNCHECKABLE_SOURCES),
            )
            .order_by(Job.last_checked_at.asc().nullsfirst())
            .limit(sample_size)
        )
        jobs = list(result.scalars().all())

    if not jobs:
        return unseen

    now = datetime.now(timezone.utc)
    expired = 0
    async with httpx.AsyncClient(
        headers=DEFAULT_HEADERS, timeout=CHECK_TIMEOUT,
    ) as client:
        for job in jobs:
            status = await _check_url_status(client, job.url)

            async with get_session() as session:
                fresh = await session.get(Job, job.id)
                if fresh is None:
                    continue
                fresh.last_checked_at = now
                if status in DEAD_STATUSES:
                    fresh.is_active = False
                    expired += 1
                await session.commit()

            if status in DEAD_STATUSES:
                try:
                    from gosha.events import emit_job_expired
                    await emit_job_expired(job.id)
                except Exception:
                    pass

    if expired:
        log.info("Dead-link check: expired %d of %d sampled jobs", expired, len(jobs))
    return unseen + expired
