"""Wrapper around JobSpy that routes requests through SOCKS5 tunnels (and,
with SCRAPE_DIRECT=true, the server's own connection).

This module only handles raw scraping — no filtering, no DB writes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from functools import cache, partial, wraps
from typing import TYPE_CHECKING

import pandas as pd
from jobspy import scrape_jobs

from gosha.filters import city_only, expand_keyword, normalize_location
from gosha.scrapers import page_store
from gosha.scrapers.base import is_reposter, reposter_client, strip_invisible, strip_title_codes

if TYPE_CHECKING:
    from gosha.ssh_tunnels import SSHTunnelManager

log = logging.getLogger(__name__)

RESULTS_PER_TERM = 30
# LinkedIn showed 90 results for "python developer" in Cluj-Napoca within
# 14 days where we kept 30, and 51 of the other 60 were title-relevant
# (2026-10-16), among them a junior trainee programme and a junior
# full-stack role. Kept at 30 until the 429s below are under control:
# every extra row is a list-page share and, uncached, a job page.
LINKEDIN_RESULTS_PER_TERM = 30
DELAY_BETWEEN_SEARCHES = 2  # seconds

# fetch_description reads one LinkedIn job page per row, and JobSpy dedups
# only within a call. The terms of one keyword return the same jobs, and so
# do the cycles 4 hours apart: the 08:26 cycle of 2026-10-16 got 1,425
# JobSpy rows for 343 distinct LinkedIn jobs, and LinkedIn answered 429 to
# 196 job pages and 57 searches. A job page read in the last day is reused.
LINKEDIN_DETAIL_TTL_SECONDS = 24 * 3600
_LINKEDIN_DETAIL_PRUNE_AT = 5000
_linkedin_details: dict[str, tuple[float, dict]] = {}


@cache
def _linkedin_namespace() -> str:
    """The job pages python-jobspy parsed, while that version is installed."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return f"linkedin:jobspy-{version('python-jobspy')}"
    except PackageNotFoundError:  # pragma: no cover - jobspy imported from a checkout
        return "linkedin:jobspy"


def _cached_linkedin_details(fetch):
    """Wrap jobspy's LinkedIn._fetch_details with the job-page cache.

    Only pages that gave a description are kept: a 429, an error or a
    redirect comes back as {} and the next term or cycle tries again. The
    cache is written through to disk (scrapers/page_store), so a deploy
    does not send LinkedIn every job page again.
    """

    @wraps(fetch)
    def fetch_details(self, job_id: str) -> dict:
        now = time.time()
        hit = _linkedin_details.get(job_id)
        if hit is None:
            hit = page_store.store().get(_linkedin_namespace(), job_id)
            if hit is not None:
                _linkedin_details[job_id] = hit
        if hit and now - hit[0] < LINKEDIN_DETAIL_TTL_SECONDS:
            return dict(hit[1])
        details = fetch(self, job_id)
        if details and details.get("description"):
            if len(_linkedin_details) >= _LINKEDIN_DETAIL_PRUNE_AT:
                for key, (at, _) in list(_linkedin_details.items()):
                    if now - at >= LINKEDIN_DETAIL_TTL_SECONDS:
                        _linkedin_details.pop(key, None)
                page_store.store().prune(
                    "linkedin:", now - LINKEDIN_DETAIL_TTL_SECONDS, _LINKEDIN_DETAIL_PRUNE_AT,
                )
            _linkedin_details[job_id] = (now, dict(details))
            page_store.store().put(_linkedin_namespace(), job_id, details, now)
        return details

    fetch_details.gosha_cached = True  # type: ignore[attr-defined]
    return fetch_details


def install_linkedin_detail_cache() -> bool:
    """Put the cache on jobspy's LinkedIn scraper (once). False if it moved.

    _fetch_details is private to python-jobspy 1.3.0 (pinned in
    constraints.txt); tests/test_scraper_jobspy.py fails if an upgrade
    renames it. Without it, scraping works as before, uncached.
    """
    try:
        from jobspy.linkedin import LinkedIn
    except ImportError:  # pragma: no cover - layout of an unpinned jobspy
        log.warning("jobspy.linkedin.LinkedIn not found; LinkedIn job pages are not cached")
        return False
    fetch = getattr(LinkedIn, "_fetch_details", None)
    if fetch is None:
        log.warning("LinkedIn._fetch_details not found; LinkedIn job pages are not cached")
        return False
    if not getattr(fetch, "gosha_cached", False):
        LinkedIn._fetch_details = _cached_linkedin_details(fetch)
    return True


install_linkedin_detail_cache()


def _jobspy_country(name: str) -> object | None:
    """JobSpy's Country enum member for a country name, or None."""
    try:
        from jobspy.model import Country
    except ImportError:  # pragma: no cover - layout of an unpinned jobspy
        return None
    try:
        country = Country.from_string(name)
    except ValueError:
        return None
    return None if country.name == "WORLDWIDE" else country


def indeed_country(location: str) -> str | None:
    """`country_indeed` for a normalised 'City, Country' search location.

    JobSpy picks the Indeed domain from `country_indeed`, which defaults to
    "usa" — so without it "Cluj-Napoca, Romania" was searched on indeed.com
    and returned nothing, while ro.indeed.com has the postings. None for
    locations that name no country JobSpy knows ("Remote", "Europe"), which
    keeps JobSpy's default.
    """
    parts = [p.strip() for p in location.split(",") if p.strip()]
    for candidate in (parts[-1:] if parts else []) + [location.strip()]:
        if _jobspy_country(candidate) is not None:
            return candidate.lower()
    return None


def glassdoor_country(location: str) -> str | None:
    """Like indeed_country, but only where JobSpy has a Glassdoor domain.

    Glassdoor derives its domain from the same argument and raises for
    countries it does not serve (Romania among them); those keep the
    glassdoor.com default they have always used.
    """
    name = indeed_country(location)
    country = _jobspy_country(name) if name else None
    if country is None:
        return None
    try:
        country.get_glassdoor_url()  # type: ignore[attr-defined]
    except Exception:
        return None
    return name


def jobspy_kwargs(
    boards: list[str],
    search_term: str,
    location: str,
    max_age_days: int,
    proxy: str | None,
    country: str | None,
) -> dict:
    """Keyword arguments for one jobspy.scrape_jobs call.

    One builder for every call site (and the scraper smoke), because
    scrape_jobs takes **kwargs: a misspelt or renamed argument is dropped
    in silence. That is how `proxy=` — renamed `proxies=` upstream — sent
    every JobSpy request direct instead of through the SSH tunnels.
    tests/test_scraper_jobspy.py checks these names against the installed
    signature.

    LinkedIn alone gets LINKEDIN_RESULTS_PER_TERM; a call that mixes it
    with other boards keeps the common cap.

    `fetch_description` (python-jobspy >= 1.2; it replaced the LinkedIn-only
    `linkedin_fetch_description`) fetches each posting's description where
    the list view has none: LinkedIn and, since 1.3, Glassdoor. The
    embeddings and cover letters need the description, not just a title.
    """
    kwargs: dict = {
        "site_name": boards,
        "search_term": search_term,
        "location": location,
        "results_wanted": (
            LINKEDIN_RESULTS_PER_TERM if boards == ["linkedin"] else RESULTS_PER_TERM
        ),
        "hours_old": max_age_days * 24,
        "proxies": proxy,
        "fetch_description": True,
    }
    if country:
        kwargs["country_indeed"] = country
    return kwargs


# Search locations JobSpy cannot serve, per board (measured 2026-10-16,
# python-jobspy 1.3.0, "software engineer", 30 results a board):
# - "Remote" is a US place to Indeed and Glassdoor (indeed.com: New York,
#   Denver, Chicago) and no place at all to LinkedIn (Pune, New York,
#   Singapore); 1 of the 90 rows had "remote" in its location. LinkedIn
#   ignores is_remote, and Indeed's remote filter returns nothing on
#   ro.indeed.com. The rows were stored all the same and ranked in
#   Romanian students' feeds, which do not filter by location.
# - "Europe" is a place to LinkedIn (London, Dublin, ...) but not to
#   Indeed or Glassdoor, which return nothing for it.
# Remote searches are served by the native feeds (Jobicy, RemoteOK,
# BestJobs' remote listings) instead.
_REMOTE_LOCATIONS = {"remote", "anywhere", "worldwide"}
_CONTINENT_LOCATIONS = {"europe", "eu"}


def jobspy_boards_for(location: str, boards: list[str]) -> list[str]:
    """The JobSpy boards that can serve a search location (user spelling)."""
    key = location.strip().lower()
    if key in _REMOTE_LOCATIONS:
        return []
    if key in _CONTINENT_LOCATIONS:
        return [b for b in boards if b == "linkedin"]
    return list(boards)


# LinkedIn's "Seniority level" criterion (python-jobspy lowercases it into
# job_level, read from the job page when fetch_description is on) as the
# board label filters.matches_experience_level and seniority.posting_level
# honour. "associate" covers graduate and 2-3 year roles alike and
# "not applicable" says nothing; both stay unknown, for the title and
# description rules to decide. "mid-senior level" is LinkedIn's one bucket
# for experienced roles: "mid", since the label outranks the description's
# years in the ranking.
_JOB_LEVELS = {
    "internship": "intern",
    "entry level": "entry",
    "mid-senior level": "mid",
    "director": "senior",
    "executive": "senior",
}


def experience_level_from_job_level(job_level: object) -> str | None:
    """intern/entry/mid/senior for a JobSpy job_level, or None."""
    if not isinstance(job_level, str):
        return None
    return _JOB_LEVELS.get(job_level.strip().lower())


# Indeed ANDs the words of a search: "junior java" in Bucharest (14 days)
# found 0 postings while "java" found 45, some of them junior ("Programator
# Java Junior"), because few postings carry the seniority word at all. So a
# term with one is also searched on Indeed without it, for at most
# INDEED_FALLBACK_SEARCHES terms per keyword (a broad keyword expands into
# 19 terms). The pipeline's filter_dataframe, run with the subscription's
# own keyword, keeps the entry-level part of the wider result.
INDEED_FALLBACK_SEARCHES = 2
_SENIORITY_WORD_RE = re.compile(
    r"(?i)(?<!\w)(?:junior|jr\.?|entry[\s-]+level|intern(?:ship)?s?|"
    r"trainee(?:ship)?s?|graduates?|new\s+grads?|stagi[ui]|stagiar\w*|"
    r"practic[aă]|absolvent\w*)(?!\w)"
)


def without_seniority_words(term: str) -> str | None:
    """`term` with its seniority words removed; None if it had none or is
    nothing else ("junior")."""
    stripped = " ".join(_SENIORITY_WORD_RE.sub(" ", term).split())
    if not stripped or stripped == " ".join(term.split()):
        return None
    return stripped


def indeed_fallback_terms(terms: list[str]) -> list[str]:
    """The extra Indeed searches for a keyword's terms (see above)."""
    searched = {t.strip().lower() for t in terms}
    extra: list[str] = []
    for term in terms:
        stripped = without_seniority_words(term)
        if stripped and stripped.lower() not in searched:
            searched.add(stripped.lower())
            extra.append(stripped)
            if len(extra) == INDEED_FALLBACK_SEARCHES:
                break
    return extra


def scrape_direct_enabled() -> bool:
    """SCRAPE_DIRECT=true lets JobSpy also use the server's own IP."""
    return os.getenv("SCRAPE_DIRECT", "").strip().lower() in ("1", "true", "yes", "on")


# ── Baseline catalog ────────────────────────────────────────────────────
# Searches run every cycle whatever the subscriptions are. The web feed
# ranks every stored posting, and what gets stored is what subscriptions
# search: on 2026-10-16 that was 22 searches from 12 users, all of them
# "computer science internship" variants and mostly in Cluj, so a junior
# user's feed, or a Bucharest one's, had almost nothing that fit.
#
# Native boards only (eJobs, BestJobs, Hipo; BestJobs, RemoteOK and Jobicy
# for "remote"): no LinkedIn, Indeed or Glassdoor request, since LinkedIn
# already answers 110-372 job pages and 27-112 searches per cycle with 429.
# eJobs and Hipo ignore a tech keyword (IT-department browse, city catalog,
# both cached 10 minutes), so the keyword only steers BestJobs' search and
# the filter: "junior developer" keeps intern, junior and unmarked
# (<= 2 years asked) tech titles, and drops senior and non-tech ones.
BASELINE_KEYWORD = "junior developer"
BASELINE_LOCATIONS = ("bucuresti", "cluj", "timisoara", "iasi", "oradea", "romania", "remote")
BASELINE_MAX_AGE_DAYS = 14
# One cycle's worst case with cold caches: every search reads up to its
# adapter's page cap (eJobs 6, Hipo 20, Jobicy 3, BestJobs one per city
# queried, RemoteOK 1) plus eJobs' /cities once, and fetches up to its
# detail cap (eJobs 25, Hipo 20, BestJobs 20). Typical is far lower: about
# 55 list requests for the board sizes measured on 2026-10-16 (eJobs
# Bucharest 3 pages, nationwide 6; Hipo Bucharest 9, nationwide 12, the
# others 1-2), and detail pages are cached for 24 h. Asserted in
# tests/test_scraper_baseline.py.
BASELINE_MAX_LIST_REQUESTS = 171
BASELINE_MAX_DETAIL_FETCHES = 410


@dataclass(frozen=True)
class BaselineCombo:
    """One baseline search, in the shape of pipeline._scrape_combos' keys."""

    keyword: str  # what is searched
    location: str  # user spelling, as in Subscription.locations
    boards: tuple[str, ...]  # JobSpy boards; () = native sources only
    max_age_days: int
    filter_keywords: tuple[str, ...]  # for filter_dataframe / relevance_predicate


def baseline_enabled() -> bool:
    """SCRAPE_BASELINE=true turns the baseline catalog on (default off)."""
    return os.getenv("SCRAPE_BASELINE", "").strip().lower() in ("1", "true", "yes", "on")


def baseline_combos(cycle: int = 0) -> list[BaselineCombo]:
    """The baseline searches for scrape cycle number `cycle` (no I/O).

    `cycle` is for a later rotation over more searches; every cycle gets
    the same searches today. Before adding a rotation, tell gosha-pipe:
    searches that change every cycle keep the native boards in the
    scrape-health grace period (gosha/scrape_health.py), so rotating combos
    must stay out of the pipeline's searches_by_source.
    """
    if not baseline_enabled():
        return []
    return [
        BaselineCombo(
            keyword=BASELINE_KEYWORD, location=location, boards=(),
            max_age_days=BASELINE_MAX_AGE_DAYS, filter_keywords=(BASELINE_KEYWORD,),
        )
        for location in BASELINE_LOCATIONS
    ]


async def _scrape_single(
    tunnel_manager: SSHTunnelManager,
    search_term: str,
    location: str,
    max_age_days: int,
    boards: list[str],
) -> pd.DataFrame:
    """Run one JobSpy scrape through a randomly chosen active proxy.

    Indeed/LinkedIn get the full location; Glassdoor gets city-only.
    """
    # Each route is a SOCKS5 tunnel URL, or None for the server's own
    # connection when SCRAPE_DIRECT is on. Shuffling spreads searches across
    # the available IPs; later routes are the fallback when one is blocked.
    proxies: list[str | None] = list(tunnel_manager.active_proxies())
    if scrape_direct_enabled():
        proxies.append(None)
    if not proxies:
        log.warning(
            "No active proxies and SCRAPE_DIRECT is off — skipping '%s' in '%s'",
            search_term, location,
        )
        return pd.DataFrame()

    random.shuffle(proxies)

    def call(board: str, proxy: str | None):
        # One call per board: Glassdoor needs a city-only location, and
        # LinkedIn takes its own LINKEDIN_RESULTS_PER_TERM.
        if board == "glassdoor":
            kwargs = jobspy_kwargs(
                [board], search_term, city_only(location), max_age_days,
                proxy, glassdoor_country(location),
            )
        else:
            kwargs = jobspy_kwargs(
                [board], search_term, location, max_age_days,
                proxy, indeed_country(location),
            )
        return partial(scrape_jobs, **kwargs)

    # A board that raises tries the next route on its own: the boards that
    # answered keep their rows and are not asked again (LinkedIn 429s).
    pending = list(boards)
    frames: list[pd.DataFrame] = []
    loop = asyncio.get_running_loop()
    for proxy in proxies:
        if not pending:
            break
        route = proxy or "direct (server IP)"
        log.info(
            "Searching '%s' in '%s' (max_age=%dd) on %s via %s",
            search_term, location, max_age_days, ",".join(pending), route,
        )
        results = await asyncio.gather(
            *(loop.run_in_executor(None, call(board, proxy)) for board in pending),
            return_exceptions=True,
        )
        failed: list[str] = []
        for board, result in zip(pending, results):
            if isinstance(result, BaseException):
                log.warning(
                    "%s failed for '%s' in '%s' via %s: %s",
                    board, search_term, location, route, result,
                )
                failed.append(board)
            elif result is not None and not result.empty:
                frames.append(result)
        pending = failed

    if pending:
        log.error(
            "All routes failed for %s on '%s' in '%s'",
            ",".join(pending), search_term, location,
        )
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    log.info("Found %d results for '%s' in '%s'", len(df), search_term, location)
    return df


async def scrape_jobs_raw(
    tunnel_manager: SSHTunnelManager,
    keyword: str,
    location: str,
    max_age_days: int,
    boards: list[str] | None = None,
) -> pd.DataFrame:
    """Expand keyword, normalize location, scrape each term, merge & dedup.

    Returns a deduplicated DataFrame of raw job results.
    No relevance filtering — that happens in the pipeline.
    """
    if boards is None:
        boards = ["indeed", "linkedin", "glassdoor"]
    boards = jobspy_boards_for(location, boards)
    if not boards:
        log.info("JobSpy cannot search %r; left to the native remote feeds", location)
        return pd.DataFrame()

    terms = expand_keyword(keyword)
    search_location, _match_subs = normalize_location(location)
    log.info(
        "Keyword '%s' -> %d terms, location '%s' -> '%s'",
        keyword, len(terms), location, search_location,
    )

    all_frames: list[pd.DataFrame] = []
    for term in terms:
        df = await _scrape_single(
            tunnel_manager, term, search_location, max_age_days, boards,
        )
        if not df.empty:
            all_frames.append(df)
        await asyncio.sleep(DELAY_BETWEEN_SEARCHES)

    if "indeed" in boards:
        for term in indeed_fallback_terms(terms):
            log.info("Indeed fallback: '%s' without its seniority word", term)
            df = await _scrape_single(
                tunnel_manager, term, search_location, max_age_days, ["indeed"],
            )
            if not df.empty:
                all_frames.append(df)
            await asyncio.sleep(DELAY_BETWEEN_SEARCHES)

    if not all_frames:
        return pd.DataFrame()

    merged = pd.concat(all_frames, ignore_index=True)

    # Deduplicate by job URL, keeping a copy that has a description: LinkedIn
    # answers 429 to a job page for one term and serves it for the next
    # (job 8275 was stored empty that way on 2026-10-17).
    url_col = "job_url" if "job_url" in merged.columns else "link"
    if url_col in merged.columns:
        if "description" in merged.columns:
            has_text = merged["description"].map(lambda v: isinstance(v, str) and bool(v.strip()))
            merged = merged.loc[has_text.sort_values(ascending=False, kind="stable").index]
        merged = merged.drop_duplicates(subset=[url_col], keep="first").sort_index()

    # The native adapters clean their text in RawJob; JobSpy's rows here.
    for column in ("title", "company", "location", "description"):
        if column in merged.columns:
            merged[column] = merged[column].map(
                lambda v: strip_invisible(v).strip() if isinstance(v, str) else v
            )
    if "title" in merged.columns:
        merged["title"] = merged["title"].map(
            lambda v: strip_title_codes(v) if isinstance(v, str) else v
        )
    # LinkedIn reposts by job sites name the employer "on behalf of: X".
    if "company" in merged.columns and "description" in merged.columns:
        reposts = merged["company"].map(lambda v: isinstance(v, str) and is_reposter(v))
        if reposts.any():
            clients = merged.loc[reposts, "description"].map(reposter_client)
            merged.loc[reposts, "company"] = clients.where(
                clients.notna(), merged.loc[reposts, "company"],
            )

    # LinkedIn's card date is the repost date: on 2026-10-16, 11 of the 109
    # active Romanian LinkedIn rows were dated on average 76 days after we
    # first saw them. Marked as a refresh date (RawJob.posted_at_is_refresh),
    # so the pipeline keeps the earliest. Indeed and Glassdoor showed none.
    if "site" in merged.columns:
        merged["date_posted_is_refresh"] = merged["site"].eq("linkedin")

    if "job_level" in merged.columns:
        merged["experience_level"] = merged["job_level"].map(experience_level_from_job_level)

    log.info("Raw scrape: %d unique results for '%s' in '%s'", len(merged), keyword, location)
    return merged
