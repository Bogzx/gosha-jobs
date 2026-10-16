"""Job-centric pipeline with three decoupled stages.

Stage 1 — SCRAPE:  Fetch from job boards, upsert into Job table.
Stage 2 — MATCH:   For each subscription, score jobs, enqueue deliveries.
Stage 3 — DELIVER: Pull from delivery queue, send via channels (Discord DM, etc.).

Each stage can run independently. The DB is the glue between them.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import select, update

from gosha.database import get_session
from gosha.dedup import normalize_key
from gosha.filters import (
    canonical_keyword,
    city_only,
    fold,
    clean_location,
    filter_dataframe,
    looks_like_an_ad,
    matches_company_blacklist,
    matches_excluded,
    matches_experience_level,
    normalize_location,
    search_location_ok,
    title_is_relevant,
)
from gosha.matching import SemanticMatcher
from gosha.models import Job, Subscription, User, UserJob
from gosha.queue import enqueue_deliveries_batch
from gosha.salary import meets_minimum, normalise_range
from gosha.scrape_health import CycleReport, ScrapeHealth
from gosha.scrapers.base import EXPERIENCE_LEVELS

if TYPE_CHECKING:
    from gosha.bot import JobBot
    from gosha.ssh_tunnels import SSHTunnelManager

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Stage 1 — SCRAPE: fetch raw jobs and store in DB
# ═══════════════════════════════════════════════════════════════════════


async def upsert_jobs(df: pd.DataFrame) -> list[Job]:
    """Insert new jobs or update last_seen_at for existing ones.

    Returns the list of Job objects (both new and updated).
    """
    if df.empty:
        return []

    jobs: list[Job] = []
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        # One SELECT for the whole batch instead of one per scraped row.
        # A broad keyword expands into ~19 searches x 30 results across 7
        # sources, so the per-row lookup was hundreds of round trips per
        # cycle against a Postgres in another container.
        urls = [
            str(u)
            for u in (
                df.get("job_url", pd.Series(dtype=object))
                .fillna(
                    df.get("job_url_direct", pd.Series(dtype=object))
                )
                .fillna(df.get("link", pd.Series(dtype=object)))
                .tolist()
            )
            if u and str(u) != "nan"
        ]
        existing_by_url: dict[str, Job] = {}
        if urls:
            found = await session.execute(select(Job).where(Job.url.in_(urls)))
            existing_by_url = {job.url: job for job in found.scalars().all()}

        for _, row in df.iterrows():
            url = str(
                row.get("job_url")
                or row.get("job_url_direct")
                or row.get("link")
                or ""
            )
            if not url or url == "nan" or len(url) > _URL_MAX:
                continue

            # Fitted to the column widths: Postgres rejects an over-long
            # value and rolls back the whole batch, and a BestJobs posting
            # listing 25 cities is a 300-character location.
            title = _fit(_clean(row.get("title"), "Unknown Title"), 512)
            company = _fit(_clean(row.get("company"), "Unknown"), 256)
            location = _fit(_clean(row.get("location"), ""), 256)
            description = _clean(row.get("description"), "")
            source = _fit(_clean(row.get("site"), "unknown"), 64)

            salary_min = _parse_float(row, "min_amount")
            salary_max = _parse_float(row, "max_amount")
            salary_currency = (
                _fit(str(row["currency"]), 16)
                if "currency" in row and pd.notna(row.get("currency"))
                else None
            )
            # JobSpy calls it "interval"; our adapters emit the same key.
            salary_period = (
                _fit(str(row["interval"]), 16)
                if "interval" in row and pd.notna(row.get("interval"))
                else None
            )
            # Normalise once, here, so the filter and the sort can compare
            # eJobs' monthly RON with RemoteOK's annual USD.
            monthly_min, monthly_max = normalise_range(
                salary_min, salary_max, salary_currency, salary_period,
            )
            posted_at = _parse_datetime(row, "date_posted")
            # Hipo's date is the last refresh (RawJob.posted_at_is_refresh):
            # an upper bound of the posting date. Absent (JobSpy) means not.
            refreshed = _flag(row, "date_posted_is_refresh")
            level = row.get("experience_level")
            experience_level = level if level in EXPERIENCE_LEVELS else None

            existing = existing_by_url.get(url)

            if existing:
                existing.last_seen_at = now
                existing.is_active = True
                # The vector embeds title, company and description; when
                # any of them changes (typically a description arriving
                # from a detail page for a job first seen title-only),
                # drop it so the embed step re-encodes the posting.
                if (
                    (title != "Unknown Title" and title != existing.title)
                    or (company != "Unknown" and company != existing.company)
                    or (description and description != existing.description)
                ):
                    existing.embedding = None
                    existing.embedding_model = None
                if title != "Unknown Title":
                    existing.title = title
                if company != "Unknown":
                    existing.company = company
                if location:
                    existing.location = location
                if description:
                    existing.description = description
                if salary_min is not None:
                    existing.salary_min = salary_min
                if salary_max is not None:
                    existing.salary_max = salary_max
                if salary_currency:
                    existing.salary_currency = salary_currency
                if salary_period:
                    existing.salary_period = salary_period
                if monthly_min is not None:
                    existing.salary_monthly_min_ron = monthly_min
                if monthly_max is not None:
                    existing.salary_monthly_max_ron = monthly_max
                if posted_at is not None and refreshed:
                    # The posting is no newer than the earliest date we
                    # know of, our first sighting included: a later refresh
                    # must not make an old ad look new.
                    existing.posted_at = min(
                        d for d in (
                            _utc(existing.posted_at), _utc(posted_at),
                            _utc(existing.first_seen_at),
                        ) if d is not None
                    )
                elif posted_at is not None:
                    existing.posted_at = posted_at
                if experience_level is not None:
                    existing.experience_level = experience_level
                jobs.append(existing)
            else:
                job = Job(
                    url=url,
                    title=title,
                    company=company,
                    location=location,
                    description=description,
                    salary_min=salary_min,
                    salary_max=salary_max,
                    salary_currency=salary_currency,
                    salary_period=salary_period,
                    salary_monthly_min_ron=monthly_min,
                    salary_monthly_max_ron=monthly_max,
                    source=source,
                    first_seen_at=now,
                    last_seen_at=now,
                    posted_at=posted_at,
                    experience_level=experience_level,
                )
                session.add(job)
                jobs.append(job)
                # A batch can contain the same URL twice (two keywords hit
                # the same posting); without this the second occurrence
                # would insert a duplicate and trip the unique constraint.
                existing_by_url[url] = job

        await session.commit()

    # Emit events for new jobs (best-effort)
    try:
        from gosha.events import emit_job_discovered
        for job in jobs:
            if job.id and job.first_seen_at == now:  # Newly created
                await emit_job_discovered(job.id, job.source, job.url)
    except Exception:
        pass

    log.info("Upserted %d jobs into DB", len(jobs))
    return jobs


async def run_scrape_stage(
    tunnel_manager: SSHTunnelManager,
    health: ScrapeHealth | None = None,
    *,
    scraped_for: dict[tuple, set[int]] | None = None,
) -> list[Job]:
    """Stage 1: scrape all active subscriptions and upsert results.

    Returns all jobs found (new + updated) that passed the relevance
    filter. Per-source yields are folded into `health` (default: the
    process-wide tracker) so a board that has quietly stopped returning
    anything gets alerted on — see gosha/scrape_health.py. Health is judged
    on what each board RETURNED, before the relevance filter: a board that
    answers with 200 postings none of which suit an internship search is
    working; one that answers with nothing is not.
    """
    from gosha.scrape_health import get_scrape_health
    from gosha.scraper import baseline_combos, jobspy_boards_for, scrape_jobs_raw

    global last_scraped_for
    if scraped_for is None:
        scraped_for = {}
    last_scraped_for = scraped_for

    health = health or get_scrape_health()
    attempted: set[str] = set()
    scraped: dict[str, set[str]] = {}
    # Which searches each board ran: a board that lost one re-learns its
    # yield baseline (gosha/scrape_health.py) without the others doing so.
    searches_by_source: dict[str, set[tuple]] = {}

    async with get_session() as session:
        result = await session.execute(
            select(Subscription).where(Subscription.is_active.is_(True))
            .order_by(Subscription.id)
        )
        all_subs = result.scalars().all()

    # The baseline catalog (SCRAPE_BASELINE, default off): student-level
    # tech searches on the native boards every cycle, subscriptions or not.
    baseline = baseline_combos(int(time.time() // BASELINE_CYCLE_SECONDS))
    if not all_subs and not baseline:
        # Not a failure — nobody has asked for anything, so no alert.
        log.info("No active subscriptions — nothing to scrape.")
        return []

    scrape_combos = _with_baseline(_scrape_combos(all_subs), baseline)
    # What the searches behind each query ask for: the scrape filter keeps
    # what any of them could take (each search's own levels apply at match).
    levels_for = _levels_per_group(all_subs)

    all_jobs: list[Job] = []
    for combo, keywords in scrape_combos.items():
        keyword, location, boards, max_age_days = combo
        if not boards:  # native sources only (the baseline catalog)
            continue
        # Only the boards JobSpy actually asks for this location: none for
        # "remote", LinkedIn alone for "europe". Counting the rest as
        # attempted raised "indeed returned nothing" alerts for searches
        # Indeed was never sent.
        for board in jobspy_boards_for(location, list(boards)):
            attempted.add(board)
            searches_by_source.setdefault(board, set()).add(combo)
        try:
            df = await scrape_jobs_raw(
                tunnel_manager, keyword, location, max_age_days, boards=list(boards),
            )
        except Exception as exc:  # one search must not end the cycle
            log.error("JobSpy search %r in %r failed: %s", keyword, location, exc)
            continue
        if df.empty:
            continue
        _count_scraped(scraped, df)
        df = filter_dataframe(df, keywords, levels_for.get(_group_of(combo)))
        upserted = await _upsert_safely(df, f"JobSpy {keyword!r} in {location!r}")
        _note_scraped(scraped_for, combo, upserted)
        all_jobs.extend(upserted)

    # Extra sources (Romanian boards + remote feeds): direct APIs, no
    # proxies. Additive for every subscription regardless of its boards
    # list, wherever the source can serve the location.
    all_jobs.extend(
        await _scrape_extra_sources(
            scrape_combos, attempted, scraped, searches_by_source, scraped_for, levels_for,
        )
    )

    # Deduplicate by ID
    seen_ids: set[int] = set()
    unique: list[Job] = []
    kept: dict[str, int] = {}
    for j in all_jobs:
        if j.id not in seen_ids:
            seen_ids.add(j.id)
            unique.append(j)
            kept[j.source] = kept.get(j.source, 0) + 1

    # The searches too, per board: a board that lost one re-learns its
    # yield baseline instead of a deleted big search reading as a collapse.
    health.record_cycle(
        attempted, {source: len(urls) for source, urls in scraped.items()}, kept,
        searches=searches_by_source,
    )

    log.info("Scrape stage complete: %d unique jobs", len(unique))
    return unique


# Locations of active searches that name no place ("kra"), logged once per
# process rather than every cycle.
_skipped_locations: set[str] = set()


def _scrape_combos(subs: list[Subscription]) -> dict[tuple, list[str]]:
    """(keyword, location, boards, max_age_days) -> the searches' keywords.

    One query per keyword, place and window: searches differing only in
    boards, or naming one city two ways ("cluj", "Cluj-Napoca"), each sent
    the same query again; the boards asked for are merged. Windows stay
    apart, because the boards cap a result list: a 3-day search sharing a
    30-day query could lose its fresh postings to older ones. Locations that name no place (saved before validation, see
    filters.clean_location) are not sent to the boards at all: each one
    cost a failing request per board per cycle.
    """
    groups: dict[tuple[str, str, int], dict[str, Any]] = {}
    for sub in subs:
        for raw_location in sub.locations:
            location = clean_location(raw_location)
            if location is None:
                if raw_location not in _skipped_locations:
                    _skipped_locations.add(raw_location)
                    log.warning(
                        "Not scraping location %r (search #%s): it names no place",
                        raw_location, sub.id,
                    )
                continue
            place = normalize_location(location)[0].lower()
            for keyword in map(canonical_keyword, sub.keywords):
                key = (keyword.lower().strip(), place, sub.max_age_days)
                group = groups.setdefault(key, {
                    "location": location.lower(), "boards": set(), "keywords": [],
                })
                group["boards"].update(sub.boards or ())
                group["keywords"].append(keyword)
    return {
        (keyword, g["location"], tuple(sorted(g["boards"])), max_age): g["keywords"]
        for (keyword, _place, max_age), g in groups.items()
    }


# The scrape stage's record of which jobs each search group's queries
# returned in the latest cycle (search_group -> job ids).
last_scraped_for: dict[tuple, set[int]] | None = None


def search_group(keyword: str, location: str, max_age_days: int | None) -> tuple:
    """What one scrape query stands for: keyword, canonical place, window
    (the grouping of _scrape_combos). Keys `scraped_for`."""
    place = clean_location(location) or location
    return (
        canonical_keyword(keyword).lower(), normalize_location(place)[0].lower(), max_age_days,
    )


def _group_of(combo: tuple) -> tuple:
    keyword, location, _boards, max_age_days = combo
    return search_group(keyword, location, max_age_days)


def _levels_per_group(subs: list[Subscription]) -> dict[tuple, list[str]]:
    """search_group -> the union of the experience levels its searches want."""
    out: dict[tuple, list[str]] = {}
    for sub in subs:
        for keyword in sub.keywords:
            for location in sub.locations:
                levels = out.setdefault(search_group(keyword, location, sub.max_age_days), [])
                levels.extend(lv for lv in (sub.experience_levels or ["any"]) if lv not in levels)
    return out


def _note_scraped(
    scraped_for: dict[tuple, set[int]] | None, combo: tuple, jobs: list[Job],
) -> None:
    """Record which jobs a query returned (after the relevance filter)."""
    if scraped_for is None:
        return
    keyword, location, _boards, max_age_days = combo
    scraped_for.setdefault(search_group(keyword, location, max_age_days), set()).update(
        job.id for job in jobs
    )


def _scraped_by_own_searches(
    sub: Subscription, scraped_for: dict[tuple, set[int]],
) -> set[int]:
    return set().union(*(
        scraped_for.get(search_group(keyword, location, sub.max_age_days), set())
        for keyword in sub.keywords for location in sub.locations
    ))


# Scrape cycles are four hours apart: the cycle number baseline_combos
# rotates on, stable across restarts.
BASELINE_CYCLE_SECONDS = 4 * 3600


def _with_baseline(
    combos: dict[tuple, list[str]], baseline: list[Any],
) -> dict[tuple, list[str]]:
    """`combos` plus the baseline searches (gosha.scraper.BaselineCombo).

    A baseline search a subscription already makes (same keyword, place and
    window) is left to the subscription. The rest run right after the
    subscriptions' searches for the same place, while eJobs' browse and
    Hipo's city catalog are still in their 10-minute caches.
    """
    if not baseline:
        return combos

    def place(location: str) -> str:
        return normalize_location(location)[0].lower()

    taken = {(kw, place(loc), days) for kw, loc, _boards, days in combos}
    ordered = list(combos.items())
    for extra in baseline:
        key = (extra.keyword.lower().strip(), place(extra.location), extra.max_age_days)
        if key in taken:
            continue
        taken.add(key)
        item = (
            (key[0], extra.location.lower(), tuple(sorted(extra.boards)), extra.max_age_days),
            list(extra.filter_keywords),
        )
        same_place = [i for i, ((_kw, loc, _b, _d), _k) in enumerate(ordered) if place(loc) == key[1]]
        ordered.insert(same_place[-1] + 1 if same_place else len(ordered), item)
    return dict(ordered)


def _count_scraped(scraped: dict[str, set[str]], df: pd.DataFrame) -> None:
    """Fold a raw result frame's URLs into the per-source tally."""
    if "job_url" not in df.columns:
        return
    sites = df["site"] if "site" in df.columns else pd.Series("unknown", index=df.index)
    for site, url in zip(sites, df["job_url"]):
        if isinstance(url, str) and url:
            scraped.setdefault(str(site or "unknown"), set()).add(url)


async def _upsert_safely(df: pd.DataFrame, what: str) -> list[Job]:
    """upsert_jobs that logs instead of raising.

    One batch the database rejects must cost that batch, not the whole
    cycle's matching and delivery for every user.
    """
    if df.empty:
        return []
    try:
        return await upsert_jobs(df)
    except Exception as exc:
        log.error("Storing %d jobs from %s failed: %s", len(df), what, exc)
        return []


# Keyword expansion fans a broad term into up to ~19 searches. eJobs, Hipo,
# RemoteOK and Jobicy answer every term of a combo from one cached listing,
# so in practice only BestJobs makes a request per term.
MAX_EXTRA_TERMS = 20


async def _scrape_extra_sources(
    scrape_combos: dict[tuple, list[str]],
    attempted: set[str] | None = None,
    scraped: dict[str, set[str]] | None = None,
    searches_by_source: dict[str, set[tuple]] | None = None,
    scraped_for: dict[tuple, set[int]] | None = None,
    levels_for: dict[tuple, list[str]] | None = None,
) -> list[Job]:
    """Run the plugin scrapers for every (keyword, location) combo.

    A source is added to `attempted` when it applies to at least one
    combo's location, so a source that returns nothing is still counted —
    otherwise a dead adapter would vanish from the yield accounting instead
    of raising an alert. A source that applies nowhere (RemoteOK when
    nobody searches remote) is not attempted, and so cannot look broken.
    """
    from gosha.filters import expand_keyword, relevance_predicate
    from gosha.scrapers.base import SearchQuery

    try:
        from gosha.scrapers.registry import get_extra_scrapers
        scrapers = get_extra_scrapers()
    except Exception as exc:
        log.warning("Extra scrapers unavailable: %s", exc)
        return []

    collected: list[Job] = []
    for combo, keywords in scrape_combos.items():
        keyword, location, _boards, max_age_days = combo
        terms = expand_keyword(keyword)[:MAX_EXTRA_TERMS]
        levels = (levels_for or {}).get(_group_of(combo))
        relevant = relevance_predicate(keywords, levels)
        for scraper in scrapers:
            probe = SearchQuery(keyword=keyword, location=location, max_age_days=max_age_days)
            if not _applies(scraper, probe):
                continue
            if attempted is not None:
                attempted.add(scraper.name)
            if searches_by_source is not None:
                searches_by_source.setdefault(scraper.name, set()).add(combo)
            frames: list[pd.DataFrame] = []
            for term in terms:
                query = SearchQuery(
                    keyword=term, location=location, max_age_days=max_age_days,
                    relevant=relevant,
                )
                try:
                    raw_jobs = await scraper.search(query)
                except Exception as exc:  # adapters shouldn't raise; belt & braces
                    log.warning("%s scraper failed for %r: %s", scraper.name, term, exc)
                    raw_jobs = []
                if raw_jobs:
                    frames.append(pd.DataFrame([r.to_record() for r in raw_jobs]))

            if not frames:
                continue
            df = pd.concat(frames, ignore_index=True)
            df = df.drop_duplicates(subset=["job_url"], keep="first")
            if scraped is not None:
                _count_scraped(scraped, df)
            df = filter_dataframe(df, keywords, levels)
            jobs = await _upsert_safely(df, f"{scraper.name} {keyword!r} in {location!r}")
            _note_scraped(scraped_for, combo, jobs)
            if jobs:
                collected.extend(jobs)
                log.info(
                    "%s: %d jobs for %r in %r", scraper.name, len(jobs), keyword, location,
                )
    return collected


def _applies(scraper: object, query: object) -> bool:
    applies_to = getattr(scraper, "applies_to", None)
    if applies_to is None:
        return True
    try:
        return bool(applies_to(query))
    except Exception as exc:
        log.warning("%s.applies_to failed: %s", getattr(scraper, "name", scraper), exc)
        return False


# ═══════════════════════════════════════════════════════════════════════
# Stage 2 — MATCH: score jobs against subscriptions, enqueue deliveries
# ═══════════════════════════════════════════════════════════════════════


def job_matches_subscription(job: Job, sub: Subscription) -> bool:
    """Check if a stored Job matches a Subscription's criteria.

    Cheapest first: this runs for every subscription × job on the event
    loop, and the checks that read the description (excluded words, the
    years a junior posting asks for) cost far more than a date or place
    comparison. Most jobs fail on place or age.
    """
    if not is_fresh_enough(job, sub.max_age_days):
        return False
    if matches_company_blacklist(job.company, sub.company_blacklist):
        return False
    # Compared on the normalised monthly-RON figures so a subscription's
    # "salary_min" means the same thing regardless of which board the
    # posting came from.
    if not meets_minimum(
        sub.salary_min, job.salary_monthly_min_ron, job.salary_monthly_max_ron,
    ):
        return False
    if not search_location_ok(job.location, sub.locations, sub.remote_ok):
        return False
    if looks_like_an_ad(job.title, job.company):
        return False
    if matches_excluded(job.title, job.description, sub.excluded_keywords):
        return False
    return matches_experience_level(
        job.title, sub.experience_levels, job.experience_level, job.description,
    )


# The Romanian boards date postings in local time.
BOARD_TZ = ZoneInfo("Europe/Bucharest")


def is_fresh_enough(job: Job, max_age_days: int | None) -> bool:
    """Posted within the subscription's max_age_days, as far as we know.

    The boards are asked for postings that recent, but the match stage
    scores every job the cycle scraped, for anyone: a search with a 30-day
    window brought in postings that a 3-day search was then sent. Judged
    on the board's posted date, else on when the posting was first seen.
    Compared by calendar day, since most boards give a date and no time,
    in the boards' zone (Europe/Bucharest): a date at local midnight is
    21:00 the day before in UTC, which is how Postgres hands it back, and
    the last day of the window was a day short.
    """
    if not max_age_days:
        return True
    when = job.posted_at or job.first_seen_at
    if when is None:
        return True
    if when.tzinfo is None:  # SQLite hands back naive UTC
        when = when.replace(tzinfo=timezone.utc)
    oldest = (datetime.now(BOARD_TZ) - timedelta(days=max_age_days)).date()
    return when.astimezone(BOARD_TZ).date() >= oldest


def _title_relevant_for_any_keyword(
    title: str,
    keywords: list[str],
    experience_level: str | None = None,
    description: str | None = None,
    levels: list[str] | None = None,
) -> bool:
    """Check if title is relevant for at least one subscription keyword.

    `experience_level` (the board's label), `description` and the search's
    `levels` as the scrape stage's filter uses them, so a posting kept
    there is not dropped here.
    """
    for kw in keywords:
        if title_is_relevant(title, kw, experience_level, description, levels):
            return True
    return False


def blend_with_user_vector(
    query_emb: np.ndarray, user_vector: np.ndarray | None,
) -> np.ndarray:
    """The subscription query pulled toward the user's CV.

    Discord delivery used to score jobs against the subscription string
    alone ("Job role: software engineer. Location: Cluj"), so the product's
    promise — the job feed that reads your CV — held on the website and
    not in the DMs. The subscription still anchors the query (it is what
    the user asked this search for); the CV decides between postings that
    fit it equally well on paper.

    The midpoint (q + u) / 2, deliberately NOT re-normalised: a job's score
    is then the mean of its cosine to the search and to the CV, which stays
    on the scale the configured threshold was tuned for. Re-normalising
    divides by |q + u| = sqrt(2 + 2 q.u), inflating every score by
    sqrt(2 / (1 + q.u)); a search and a CV in the same field have
    q.u ~ 0.5 under all-mpnet-base-v2 (measured), i.e. +15%, which let
    more jobs through for CV holders instead of better ones.
    Vectors from different models (shape mismatch) are not mixed.
    """
    if user_vector is None:
        return query_emb
    query = np.asarray(query_emb, dtype=np.float32)
    user = np.asarray(user_vector, dtype=np.float32)
    if query.shape != user.shape:
        log.warning(
            "CV vector %s and subscription vector %s differ in shape — "
            "embedded with different models? Matching on the subscription only.",
            user.shape, query.shape,
        )
        return query
    return ((query + user) / 2).astype(np.float32)


def prefilter(sub: Subscription, jobs: list[Job]) -> list[Job]:
    """The jobs a search can be matched with, before any scoring: the hard
    filters (job_matches_subscription) and what the keyword itself implies
    for the search's levels. Public: gosha.evaluation builds its DM pool
    with it, so the eval and the match stage cannot drift apart.

    What the keyword implies: "software engineer intern" wants entry-level
    titles in tech. The scrape stage applied this to the search's own
    results; the jobs here also come from everyone else's searches, and the
    embeddings cannot see seniority (docs/EVAL.md), so without it an intern
    search was sent the senior roles another user's "java developer" search
    had found.
    """
    candidates = [job for job in jobs if job_matches_subscription(job, sub)]
    if not sub.keywords:
        return candidates
    return [
        job for job in candidates
        if _title_relevant_for_any_keyword(
            job.title, sub.keywords, job.experience_level, job.description,
            sub.experience_levels,
        )
    ]


async def match_jobs_for_subscription(
    sub: Subscription,
    jobs: list[Job],
    semantic_matcher: SemanticMatcher | None = None,
    user_vector: np.ndarray | None = None,
    scraped_for: dict[tuple, set[int]] | None = None,
) -> list[tuple[Job, float | None]]:
    """Return (job, score) pairs for jobs matching a subscription.

    If a SemanticMatcher is provided and available, uses cosine similarity
    for scoring — against the subscription blended with `user_vector` (the
    user's CV, refined by feedback) when there is one. Otherwise falls back
    to regex with score None (no similarity was measured).
    """
    candidates = prefilter(sub, jobs)
    if not candidates:
        return []
    keywords = sub.keywords

    # Semantic matching path — reuses embeddings stored at scrape time
    # instead of re-encoding the same postings for every subscription.
    if semantic_matcher and semantic_matcher.available:
        # Encoding runs in a thread: on the loop it stalled the Discord
        # gateway (missed heartbeats, "interaction failed").
        query_emb = await asyncio.to_thread(
            semantic_matcher.encode_subscription,
            keywords, sub.locations, sub.experience_levels,
        )
        if query_emb is not None:
            from gosha.embeddings import score_jobs_against_query_async

            # Blended only when the vectors share a space (same shape).
            with_cv = user_vector is not None and np.shape(user_vector) == np.shape(query_emb)
            query_emb = blend_with_user_vector(query_emb, user_vector)
            is_match = functools.partial(semantic_matcher.is_match, with_cv=with_cv)
            if not with_cv:
                # The DM score (gosha.matching, gosha-rank): the query minus
                # DM_ALPHA times the mean of a panel of other searches for
                # the same places and levels, filtered on its own threshold.
                # The panel is encoded once per (places, levels): ~1 s.
                dm_query = getattr(semantic_matcher, "dm_query", None)
                dm_q = None
                if dm_query is not None:
                    dm_q = await asyncio.to_thread(
                        dm_query, query_emb, sub.locations, sub.experience_levels,
                    )
                if dm_q is not None:
                    query_emb, is_match = dm_q, semantic_matcher.is_dm_match
                elif dm_query is not None:
                    log.warning("DM query unavailable for search #%s: plain cosine", sub.id)
            # The score stored and sorted on is this one (the DM score
            # without a CV, the blended cosine with one).
            scores = await score_jobs_against_query_async(query_emb, candidates)
            matches = [
                (job, score)
                for job, score in zip(candidates, scores)
                if is_match(score)
            ]
            # Sort by score descending
            matches.sort(key=lambda x: x[1], reverse=True)
            return matches

    # Regex fallback path: title relevance (applied above) decides, and
    # only among the jobs this search's own queries returned. A generic
    # keyword ("accountant") passes every title, so without the model its
    # search was sent every job in its city that any other search scraped.
    # No score: a stored 1.0 sorted above every cosine at delivery (and
    # read as a "Strong match").
    if scraped_for is not None:
        own = _scraped_by_own_searches(sub, scraped_for)
        candidates = [job for job in candidates if job.id in own]
    return [(job, None) for job in candidates]


async def _cv_vector_for(user: User) -> np.ndarray | None:
    """The user's ranking vector when they have uploaded a CV, else None.

    Users without a CV keep pure subscription matching: a vector built from
    a handful of thumbs-up alone is too thin to override what they asked for.
    """
    from gosha.embeddings import is_current

    if not user.cv_embedding or not is_current(user.cv_embedding_model):
        return None
    try:
        from gosha.recommend import build_user_signal

        return (await build_user_signal(user.id)).vector
    except Exception as exc:
        log.warning("CV vector unavailable for user %d: %s", user.id, exc)
        return None


async def run_match_stage(
    jobs: list[Job],
    semantic_matcher: SemanticMatcher | None = None,
    *,
    scraped_for: dict[tuple, set[int]] | None = None,
) -> int:
    """Stage 2: match jobs against all active subscriptions and enqueue deliveries.

    Feedback reaches this stage through the user's vector (CV refined by
    👍/👎, gosha/recommend.py), which is blended into each search's query.
    The stored score is that blended cosine, the number the delivery
    threshold was compared with.

    A user gets one delivery per cross-board duplicate group (Job.
    dedup_group_id, gosha/dedup.py), across cycles: the Indeed copy of a
    LinkedIn posting DM'd last week is not a new job.

    Returns total number of new deliveries enqueued.
    """
    async with get_session() as session:
        result = await session.execute(
            select(Subscription, User)
            .join(User)
            .where(Subscription.is_active.is_(True))
        )
        # Discord-notifying searches first: a user gets one delivery per
        # job, recorded against the first search that matched it, and only
        # a notifying search's delivery is DM'd. Website-only searches
        # first meant a job both matched was never DM'd.
        sub_user_pairs = sorted(result.all(), key=lambda pair: not pair[0].notify_discord)

    if not sub_user_pairs:
        return 0

    # user_id -> CV vector refined by feedback (the one the web feed ranks
    # by); None without a CV. Only needed on the semantic path.
    user_vectors: dict[int, np.ndarray | None] = {}

    # Build batch delivery items
    delivery_items: list[dict] = []
    group_of: dict[int, int] = {}
    key_of: dict[int, str | None] = {}
    posting_of: dict[int, tuple[str | None, str | None, str | None]] = {}

    for sub, user in sub_user_pairs:
        if semantic_matcher is not None and user.id not in user_vectors:
            user_vectors[user.id] = await _cv_vector_for(user)

        matches = await match_jobs_for_subscription(
            sub, jobs, semantic_matcher, user_vectors.get(user.id), scraped_for,
        )
        for job, score in matches:
            group_of[job.id] = job.dedup_group_id or job.id
            key_of[job.id] = normalize_key(job.title or "", job.company or "")
            posting_of[job.id] = (job.title, job.company, job.description)
            delivery_items.append({
                "user_id": user.id,
                "job_id": job.id,
                "subscription_id": sub.id,
                "score": score,
            })

    delivery_items = await _one_per_duplicate_group(
        delivery_items, group_of, key_of, posting_of,
    )
    if not delivery_items:
        log.info("Match stage: no new matches found.")
        return 0

    created = await enqueue_deliveries_batch(delivery_items)
    log.info("Match stage complete: %d new deliveries enqueued", created)
    return created


async def _one_per_duplicate_group(
    items: list[dict],
    group_of: dict[int, int],
    key_of: dict[int, str | None] | None = None,
    posting_of: dict[int, tuple[str | None, str | None, str | None]] | None = None,
) -> list[dict]:
    """Drop items whose duplicate group the user already has a delivery in.

    Two checks, because the group link alone is not durable: dedup only
    groups active postings, so once the copy a user was sent expires, its
    twin on another board is ungrouped again and would look new. The
    posting's identity (gosha.dedup.normalize_key: title and employer) is
    therefore also compared with the user's deliveries of the dedup window
    whose job is no longer active. Active ones need no identity check (the
    group covers them), and leaving them out keeps a second, distinct
    "Software Engineer" opening at a large employer from being dropped.

    `posting_of` (job id -> title, company, description) adds the grouping's
    own rule, gosha.dedup.same_posting: a repost by an aggregator whose
    text names the employer ("Undelucram" for Continental Anvelope) is the
    posting the user already had, though title + employer differ.

    Within the batch the first item per (user, group) wins, which is the
    one from the first (Discord-notifying) search that matched.
    """
    from gosha.dedup import WINDOW_DAYS, normalize_key, same_posting

    key_of = key_of or {}
    posting_of = posting_of or {}
    expired: dict[int, list[tuple[str | None, str | None, str | None]]] = {}
    groups = set(group_of.values())
    users = {item["user_id"] for item in items}
    taken: set[tuple[int, int]] = set()
    taken_keys: set[tuple[int, str]] = set()
    if groups and users:
        since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
        async with get_session() as session:
            rows = await session.execute(
                select(UserJob.user_id, Job.dedup_group_id, Job.id)
                .join(Job, UserJob.job_id == Job.id)
                .where(
                    UserJob.user_id.in_(users),
                    (Job.dedup_group_id.in_(groups)) | (Job.id.in_(groups)),
                )
            )
            for user_id, group_id, job_id in rows.all():
                taken.add((user_id, group_id or job_id))
            if any(key_of.values()) or posting_of:
                recent = await session.execute(
                    select(UserJob.user_id, Job.title, Job.company, Job.description)
                    .join(Job, UserJob.job_id == Job.id)
                    .where(
                        UserJob.user_id.in_(users),
                        UserJob.delivered_at >= since,
                        Job.is_active.is_(False),
                    )
                )
                for user_id, title, company, description in recent.all():
                    key = normalize_key(title or "", company or "")
                    if key:
                        taken_keys.add((user_id, key))
                    expired.setdefault(user_id, []).append((title, company, description))

    kept: list[dict] = []
    for item in items:
        group = (item["user_id"], group_of.get(item["job_id"], item["job_id"]))
        key = key_of.get(item["job_id"])
        if group in taken or (key and (item["user_id"], key) in taken_keys):
            continue
        posting = posting_of.get(item["job_id"])
        if posting and any(
            same_posting(posting[0], posting[1], title, company, posting[2], text)
            for title, company, text in expired.get(item["user_id"], ())
        ):
            continue
        # Within the batch the group decides: active twins were grouped
        # just before matching (assign_dedup_groups).
        taken.add(group)
        kept.append(item)
    if len(kept) < len(items):
        log.info(
            "Match stage: %d matches skipped — already delivered, or a "
            "cross-board duplicate of one", len(items) - len(kept),
        )
    return kept


# ═══════════════════════════════════════════════════════════════════════
# Stage 3 — DELIVER: send notifications to users
# ═══════════════════════════════════════════════════════════════════════

# DMs per user per cycle. A new subscriber's first cycle can match dozens
# of postings; the best ones go out, the rest wait for the next cycles
# (and are on the website) and the user is told how many more there are.
MAX_DMS_PER_USER = 10
# ... per search per cycle. Production, 30 days, 178 search-cycles: p50 1,
# p90 4, max 25 (a search matching "kra"/"mun" as substrings); 5 defers
# rows in 11 of them, none a search's first cycle.
MAX_DMS_PER_SEARCH = 5
# ... per company per user per cycle: one employer's ten openings are not
# ten different chances.
MAX_DMS_PER_COMPANY = 3


def _title_words(title: str | None) -> str | None:
    """The title case-, space- and punctuation-folded, if 3+ words long."""
    words = re.findall(r"[a-z0-9]+", fold(title or ""))
    return " ".join(words) if len(words) >= 3 else None


def _city(location: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", fold(city_only(location or ""))))


def _same_posting_for_dm(a: Job, b: Job) -> bool:
    """One posting for DM purposes: the dedup rule (gosha.dedup), the same
    title + employer key, or the same 3+ word title in the same city
    whatever the employer is called on each board."""
    from gosha.dedup import normalize_key, same_posting

    if same_posting(a.title, a.company, b.title, b.company, a.description, b.description):
        return True
    key = normalize_key(a.title or "", a.company or "")
    if key and key == normalize_key(b.title or "", b.company or ""):
        return True
    title = _title_words(a.title)
    city = _city(a.location)
    return bool(title and city) and title == _title_words(b.title) and city == _city(b.location)


async def _dmed_jobs(user_ids: list[int]) -> dict[int, list[Job]]:
    """user id -> the jobs already DM'd to them, within the dedup window."""
    from gosha.dedup import WINDOW_DAYS

    if not user_ids:
        return {}
    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    async with get_session() as session:
        rows = await session.execute(
            select(UserJob.user_id, Job)
            .join(Job, UserJob.job_id == Job.id)
            .where(
                UserJob.user_id.in_(user_ids),
                UserJob.dm_status == UserJob.DM_SENT,
                UserJob.dm_sent_at >= since,
            )
        )
        out: dict[int, list[Job]] = {}
        for user_id, job in rows.all():
            out.setdefault(user_id, []).append(job)
    return out


def _best_first(row: tuple) -> tuple:
    uj = row[0]
    score = uj.relevance_score
    return (score is None, -(score or 0.0), uj.id)


def _within_caps(sendable: list[tuple]) -> list[tuple]:
    """The best-first rows that fit the per-user, per-search and
    per-company caps; the others wait."""
    batch: list[tuple] = []
    per_search: dict[int, int] = {}
    per_company: dict[str, int] = {}
    for row in sendable:
        _uj, job, _user, sub = row
        company = fold(job.company or "").strip()
        if len(batch) >= MAX_DMS_PER_USER:
            break
        if per_search.get(sub.id, 0) >= MAX_DMS_PER_SEARCH:
            continue
        if company and per_company.get(company, 0) >= MAX_DMS_PER_COMPANY:
            continue
        per_search[sub.id] = per_search.get(sub.id, 0) + 1
        if company:
            per_company[company] = per_company.get(company, 0) + 1
        batch.append(row)
    return batch
# A match whose DM keeps failing for transient reasons (Discord 5xx, rate
# limits, a deploy mid-loop) is retried each cycle for this long.
DM_RETRY_WINDOW = timedelta(days=2)


async def deliver_pending(bot: JobBot) -> int:
    """Send every pending Discord DM; return how many were sent.

    "Pending" is a saved search's match the match stage marked pending
    (UserJob.dm_status), that the user has not rated on the website,
    recorded within DM_RETRY_WINDOW. The previous
    version sent the rows recorded since the cycle started, which marked a
    match delivered before any DM went out: one Discord 503 lost it for
    good, and a website 👎 recorded mid-cycle was DM'd as a match.
    """
    from gosha.views import build_job_embed_with_buttons

    cutoff = datetime.now(timezone.utc) - DM_RETRY_WINDOW
    async with get_session() as session:
        result = await session.execute(
            select(UserJob, Job, User, Subscription)
            .join(Job, UserJob.job_id == Job.id)
            .join(User, UserJob.user_id == User.id)
            .join(Subscription, UserJob.subscription_id == Subscription.id)
            .where(
                UserJob.dm_status == UserJob.DM_PENDING,
                UserJob.feedback.is_(None),
                UserJob.delivered_at >= cutoff,
                Job.is_active.is_(True),
                # Paused searches wait: a match whose DM failed, or that
                # was queued just before the user hit pause, was sent
                # anyway. It goes out if the search resumes in the window.
                Subscription.is_active.is_(True),
            )
            .order_by(UserJob.id.asc())
        )
        rows = result.all()

    by_user: dict[int, list[tuple[UserJob, Job, User, Subscription]]] = {}
    for row in rows:
        by_user.setdefault(row[2].id, []).append(row)
    dmed = await _dmed_jobs(list(by_user))

    sent = 0
    closed_dms: list[int] = []  # discord ids whose DMs are closed
    delivered_discord_ids: set[int] = set()  # ... and those a DM reached
    for items in by_user.values():
        statuses: dict[int, str] = {}
        sendable: list[tuple[UserJob, Job, User, Subscription]] = []
        # What this user was already DM'd, then what this batch keeps.
        seen_jobs: list[Job] = list(dmed.get(items[0][2].id, ()))
        # Best first, before the duplicate check below: in id order the
        # copy kept was the older one, whatever its score, and the DMs
        # went out in the order the rows were written. Unscored (regex
        # fallback) matches after every scored one.
        items = sorted(items, key=_best_first)
        for uj, job, user, sub in items:
            # Web-managed searches can opt out of Discord notifications;
            # matches still show up on the website.
            if not sub.notify_discord:
                statuses[uj.id] = UserJob.DM_SKIPPED
                continue
            # A match deferred by the caps below waited a cycle or more:
            # still within its search's window?
            if not is_fresh_enough(job, sub.max_age_days):
                statuses[uj.id] = UserJob.DM_SKIPPED
                continue
            # One posting, one DM: a copy from another board, in this batch
            # (best first, so the better-scoring copy is the one kept) or
            # already DM'd. Companies differ between boards ("Ascom UMS" /
            # "ascom", "MMC Corporate" / "Marsh").
            if any(_same_posting_for_dm(job, other) for other in seen_jobs):
                statuses[uj.id] = UserJob.DM_SKIPPED
                continue
            seen_jobs.append(job)
            sendable.append((uj, job, user, sub))

        # The caps defer: what does not fit stays pending for the next
        # cycle (best first again, among whatever is new by then) until
        # DM_RETRY_WINDOW. Marking it skipped lost it for good: production
        # dropped 8 good Cluj matches of one user's first batch.
        batch = _within_caps(sendable)
        overflow = len(sendable) - len(batch)

        sent_at: dict[int, datetime] = {}
        for uj, job, user, sub in batch:
            kw_str = ", ".join(map(canonical_keyword, sub.keywords[:3]))[:80]
            loc_str = ", ".join(sub.locations[:2])[:60]
            embed, view = build_job_embed_with_buttons(
                job, uj.id, uj.relevance_score, f"{kw_str} in {loc_str}",
                scored_with_cv=user.cv_embedding is not None,
            )
            outcome = await _dm_user(bot, user.discord_user_id, embed, view)
            if outcome == DM_SENT:
                delivered_discord_ids.add(user.discord_user_id)
                statuses[uj.id] = UserJob.DM_SENT
                sent_at[uj.id] = datetime.now(timezone.utc)
                sent += 1
                # Recorded as soon as Discord accepts it: a crash or deploy
                # later in this user's batch must not send it again.
                await _record_dm_statuses(
                    {uj.id: UserJob.DM_SENT}, {uj.id: sent_at[uj.id]},
                )
                try:
                    from gosha.events import emit_job_delivered
                    await emit_job_delivered(job.id, user.id, "discord_dm")
                except Exception:
                    pass
            elif outcome == DM_CLOSED:
                # DMs closed: give up on this user's batch, don't retry.
                for other, *_ in sendable[:MAX_DMS_PER_USER]:
                    statuses.setdefault(other.id, UserJob.DM_FAILED)
                closed_dms.append(user.discord_user_id)
                break
            # DM_RETRY: left pending for the next cycle.

        if overflow > 0 and sent_at:
            site = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
            where = f"<{site}/feed>" if site else "the website"
            await _send_text(
                bot, items[0][2].discord_user_id,
                f"…and **{overflow}** more matches: the best of them come in the "
                f"next few hours, and all of them are on {where}.",
            )

        # The rest (skipped, failed) once per user; sent ones are already
        # recorded, and writing them again is harmless.
        await _record_dm_statuses(statuses, sent_at)

    # Ops alerts are owner DMs only (gosha/alerts.py): no channel posts, no
    # mentions of the users concerned. One alert key per user: said once,
    # reminded per cooldown, "recovered" when a DM reaches them again —
    # before, every cycle repeated the whole list. Never raises.
    from gosha.alerts import report_closed_dms

    await report_closed_dms(bot, closed_dms, delivered_discord_ids)

    return sent


async def _record_dm_statuses(
    statuses: dict[int, str], sent_at: dict[int, datetime],
) -> None:
    if not statuses:
        return
    try:
        async with get_session() as session:
            for uj_id, status in statuses.items():
                await session.execute(
                    update(UserJob)
                    .where(UserJob.id == uj_id)
                    .values(dm_status=status, dm_sent_at=sent_at.get(uj_id))
                )
            await session.commit()
    except Exception as exc:
        log.error("Recording DM status for %d deliveries failed: %s", len(statuses), exc)


# ═══════════════════════════════════════════════════════════════════════
# Orchestrator — runs all three stages in sequence
# ═══════════════════════════════════════════════════════════════════════


class ScrapeCycleBusy(RuntimeError):
    """A scrape cycle is already running in this process."""


# One cycle at a time, shared by the scheduler and /scrape_now. APScheduler
# only serialises its own runs; a /scrape_now during a scheduled cycle ran
# both, and each delivered the other's new matches — two DMs per job.
_cycle_lock = asyncio.Lock()


def cycle_running() -> bool:
    return _cycle_lock.locked()


async def run_scrape_cycle(
    bot: JobBot,
    tunnel_manager: SSHTunnelManager,
    alert_channel_id: int,
    use_semantic: bool = False,
    semantic_model: str = "all-mpnet-base-v2",
    semantic_threshold: float = 0.42,
    semantic_threshold_cv: float | None = None,
) -> int:
    """Execute one full scrape -> match -> deliver cycle.

    Returns the number of DMs sent. Raises ScrapeCycleBusy when another
    cycle is already running.
    """
    if _cycle_lock.locked():
        raise ScrapeCycleBusy("a scrape cycle is already running")
    async with _cycle_lock:
        return await _run_scrape_cycle(
            bot, tunnel_manager, alert_channel_id, use_semantic,
            semantic_model, semantic_threshold, semantic_threshold_cv,
        )


async def _run_scrape_cycle(
    bot: JobBot,
    tunnel_manager: SSHTunnelManager,
    alert_channel_id: int,
    use_semantic: bool,
    semantic_model: str,
    semantic_threshold: float,
    semantic_threshold_cv: float | None,
) -> int:
    from gosha.scrape_health import get_scrape_health

    log.info("=== Scrape cycle started ===")
    health = get_scrape_health()

    # Stage 1: Scrape
    # Which jobs each search's own queries returned: the regex fallback
    # matches a search against those only (see match_jobs_for_subscription).
    # Left by the stage in last_scraped_for, so a stand-in stage taking
    # (tunnels, health) still works.
    global last_scraped_for
    last_scraped_for = None
    jobs = await run_scrape_stage(tunnel_manager, health)
    scraped_for = last_scraped_for

    # Health alerting happens BEFORE anything can return early: a cycle
    # that found nothing is exactly the cycle worth shouting about.
    await _post_health_alert(bot, alert_channel_id, health.last_report)

    if jobs:
        # Embed freshly scraped jobs so the feed and the matcher can rank
        # them (best-effort)
        try:
            from gosha.embeddings import embed_new_jobs
            await embed_new_jobs()
        except Exception as exc:
            log.warning("Job embedding step failed: %s", exc)

        # Group cross-board duplicates before matching (best-effort)
        try:
            from gosha.dedup import assign_dedup_groups
            await assign_dedup_groups()
        except Exception as exc:
            log.warning("Dedup step failed: %s", exc)

        # The Job objects from the scrape stage predate both steps: without
        # a reload the matcher saw no stored embedding (and re-encoded every
        # job once per search) and no duplicate groups.
        jobs = await _reload_jobs([job.id for job in jobs])

        # Stage 2: Match (with optional semantic scoring)
        matcher = None
        if use_semantic:
            matcher = SemanticMatcher(
                model_name=semantic_model, threshold=semantic_threshold,
                cv_threshold=semantic_threshold_cv,
            )
            # The first `available` loads the model; do that off the loop.
            if await asyncio.to_thread(lambda: matcher.available):
                log.info(
                    "Using semantic matching (model=%s, threshold=%.2f, with CV %.2f)",
                    semantic_model, matcher.threshold, matcher.cv_threshold,
                )
            else:
                log.warning("Semantic matching requested but not available — falling back to regex")
                matcher = None

        await run_match_stage(jobs, matcher, scraped_for=scraped_for)
    else:
        log.warning("No jobs found this cycle.")

    # Stage 3: Deliver — this cycle's matches and any earlier ones whose
    # DM failed for a transient reason.
    total_sent = await deliver_pending(bot)

    log.info("=== Scrape cycle finished — %d DMs sent ===", total_sent)
    return total_sent


async def _reload_jobs(job_ids: list[int]) -> list[Job]:
    if not job_ids:
        return []
    async with get_session() as session:
        result = await session.execute(
            select(Job).where(Job.id.in_(job_ids), Job.is_active.is_(True))
        )
        return list(result.scalars().all())


async def _post_health_alert(
    bot: JobBot, alert_channel_id: int, report: CycleReport | None,
) -> None:
    """Push scraper-health alerts to the owner by DM (gosha/alerts.py).

    Deliberately silent on success. The previous behaviour — a message only
    when deliveries went out — meant a dead scraper looked exactly like a
    quiet week. Cooldown, recovery messages and recipients live in
    gosha/alerts.py; this never raises.
    """
    from gosha.alerts import publish_scrape_report

    await publish_scrape_report(bot, alert_channel_id, report)


# ═══════════════════════════════════════════════════════════════════════
# Delivery helpers (kept for backward compat with existing tests)
# ═══════════════════════════════════════════════════════════════════════


async def get_undelivered_jobs(user_id: int, job_ids: list[int]) -> set[int]:
    """Return the subset of job_ids not yet delivered to this user."""
    if not job_ids:
        return set()
    async with get_session() as session:
        result = await session.execute(
            select(UserJob.job_id).where(
                UserJob.user_id == user_id,
                UserJob.job_id.in_(job_ids),
            )
        )
        already = {row[0] for row in result.all()}
    return set(job_ids) - already


async def record_delivery(
    user_id: int,
    job_id: int,
    subscription_id: int | None = None,
    relevance_score: float | None = None,
) -> UserJob:
    """Record that a job was delivered to a user."""
    async with get_session() as session:
        uj = UserJob(
            user_id=user_id,
            job_id=job_id,
            subscription_id=subscription_id,
            relevance_score=relevance_score,
        )
        session.add(uj)
        await session.commit()
        return uj


DM_SENT = "sent"
DM_CLOSED = "closed"  # Forbidden: DMs closed or the bot is blocked
DM_RETRY = "retry"    # anything else: try again next cycle


async def _dm_user(
    bot: JobBot, discord_id: int, embed: object, view: object | None = None,
) -> str:
    """Send an embed to a user via DM; return DM_SENT, DM_CLOSED or DM_RETRY.

    Never raises: one user's failure (a 503, a dropped connection) must not
    end the loop for everyone after them.
    """
    import discord as _discord

    try:
        user = bot.get_user(discord_id)
        if user is None:
            user = await bot.fetch_user(discord_id)
        dm = await user.create_dm()
        kwargs: dict = {"embed": embed}
        if view is not None:
            kwargs["view"] = view
        await dm.send(**kwargs)
        if view is not None and hasattr(view, "stop"):
            # Buttons are handled by the global on_interaction listener
            # (views.setup_interaction_handler); a live view would handle
            # every click a second time and stay in memory until restart.
            view.stop()
        return DM_SENT
    except _discord.Forbidden:
        log.warning("Cannot DM user %d — DMs are closed", discord_id)
        return DM_CLOSED
    except _discord.NotFound:
        log.warning("Discord user %d no longer exists", discord_id)
        return DM_CLOSED
    except Exception as exc:
        log.error("Failed to DM user %d (will retry): %s", discord_id, exc)
        return DM_RETRY


async def _send_text(bot: JobBot, discord_id: int, text: str) -> None:
    try:
        user = bot.get_user(discord_id) or await bot.fetch_user(discord_id)
        await (await user.create_dm()).send(text)
    except Exception as exc:
        log.warning("Follow-up DM to %d failed: %s", discord_id, exc)


# ═══════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════


_URL_MAX = 1024  # Job.url


def _fit(text: str, limit: int) -> str:
    """`text` cut to a column width, on a separator when there is one."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(", "), cut.rfind(" "))
    return (cut[:boundary] if boundary > limit // 2 else cut).rstrip(" ,;-")


def _clean(value: object, default: str) -> str:
    """Clean a DataFrame cell value into a string."""
    if value is None:
        return default
    s = str(value)
    if s in ("nan", "None", ""):
        return default
    return s


def _parse_float(row: pd.Series, col: str) -> float | None:
    """Safely parse a float from a DataFrame row."""
    if col not in row or not pd.notna(row.get(col)):
        return None
    try:
        return float(row[col])
    except (ValueError, TypeError):
        return None


def _flag(row: pd.Series, col: str) -> bool:
    """A boolean column; False when absent or NaN (numpy bools included)."""
    value = row.get(col)
    return isinstance(value, (bool, np.bool_)) and bool(value)


def _utc(value: datetime | None) -> datetime | None:
    """Aware in UTC; SQLite hands back naive UTC."""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _parse_datetime(row: pd.Series, col: str) -> datetime | None:
    """Safely parse a datetime (or date) from a DataFrame row."""
    if col not in row:
        return None
    value = row.get(col)
    try:
        if value is None or pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):
        return value
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    try:
        parsed = pd.to_datetime(str(value), utc=True)
        return parsed.to_pydatetime()
    except (ValueError, TypeError):
        return None
