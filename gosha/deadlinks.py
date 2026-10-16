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
# Boards whose IT catalog every cycle crawls whole (eJobs' IT browse,
# Hipo's city catalog): a posting missing from it this long is gone. Prod,
# 2026-10-16: hipo 9 of 20 and ejobs 6 of 64 active rows had been unseen
# for 14+ days.
CATALOG_SOURCES = frozenset({"ejobs", "hipo"})
CATALOG_EXPIRY_DAYS = 14


# Boards that keep serving a removed posting with 200 and a notice in the
# page, keyed by host. Measured 2026-10-16 on the eval set's postings: Hipo
# answers "Acest job nu mai este activ!" for filled jobs, so every one of
# them stayed active until UNSEEN_EXPIRY_DAYS. The notice is reported as
# 410 Gone.
SOFT_DEAD_MARKERS: dict[str, tuple[str, ...]] = {
    "www.hipo.ro": ("Acest job nu mai este activ",),
    "hipo.ro": ("Acest job nu mai este activ",),
}


async def _check_url_status(client: httpx.AsyncClient, url: str) -> int | None:
    """HTTP status for the posting URL; None when the check is inconclusive."""
    try:
        markers = SOFT_DEAD_MARKERS.get(httpx.URL(url).host, ())
        resp = None if markers else await client.head(url, follow_redirects=True)
        # Some boards reject HEAD (405), and some servers and CDNs answer
        # HEAD 404 for pages a GET serves fine: a dead verdict expires the
        # posting for everyone, so it is only ever taken from a GET.
        if resp is None or resp.status_code == 405 or resp.status_code in DEAD_STATUSES:
            resp = await client.get(url, follow_redirects=True)
        if resp.status_code == 200 and any(m in resp.text for m in markers):
            return 410
        return resp.status_code
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        # ValueError: a stored URL httpx cannot request (no scheme). It
        # used to escape and end the day's run for every job after it.
        return None


async def expire_unseen_jobs(
    max_unseen_days: int = UNSEEN_EXPIRY_DAYS,
    catalog_days: int = CATALOG_EXPIRY_DAYS,
) -> int:
    """Deactivate active jobs no scrape has returned for `max_unseen_days`,
    or `catalog_days` for the boards crawled whole (CATALOG_SOURCES).

    Bulk, and without a job.expired event per row: the first run on an old
    database can expire thousands of rows at once.
    """
    now = datetime.now(timezone.utc)
    expired = 0
    async with get_session() as session:
        for unseen_for, source_filter in (
            (catalog_days, Job.source.in_(CATALOG_SOURCES)),
            (max_unseen_days, Job.source.not_in(CATALOG_SOURCES)),
        ):
            result = await session.execute(
                update(Job)
                .where(
                    Job.is_active.is_(True),
                    source_filter,
                    Job.last_seen_at < now - timedelta(days=unseen_for),
                )
                .values(is_active=False)
                .execution_options(synchronize_session=False)
            )
            expired += result.rowcount or 0
        await session.commit()
    if expired:
        log.info(
            "Expired %d jobs not seen by any scrape for %d days (%d for %s)",
            expired, max_unseen_days, catalog_days, ", ".join(sorted(CATALOG_SOURCES)),
        )
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
