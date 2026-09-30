"""Wrapper around JobSpy that routes requests through SOCKS5 tunnels.

This module only handles raw scraping — no filtering, no DB writes.
"""

from __future__ import annotations

import asyncio
import logging
import random
from functools import partial
from typing import TYPE_CHECKING

import pandas as pd
from jobspy import scrape_jobs

from gosha.filters import city_only, expand_keyword, normalize_location

if TYPE_CHECKING:
    from gosha.ssh_tunnels import SSHTunnelManager

log = logging.getLogger(__name__)

RESULTS_PER_TERM = 30
DELAY_BETWEEN_SEARCHES = 2  # seconds


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
    """
    kwargs: dict = {
        "site_name": boards,
        "search_term": search_term,
        "location": location,
        "results_wanted": RESULTS_PER_TERM,
        "hours_old": max_age_days * 24,
        "proxies": proxy,
    }
    if country:
        kwargs["country_indeed"] = country
    if "linkedin" in boards:
        kwargs["linkedin_fetch_description"] = True
    return kwargs


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
    proxies = tunnel_manager.active_proxies()
    if not proxies:
        log.warning("No active proxies — skipping '%s' in '%s'", search_term, location)
        return pd.DataFrame()

    random.shuffle(proxies)
    gd_location = city_only(location)

    # Split boards into glassdoor vs others (glassdoor needs city-only location)
    gd_boards = [b for b in boards if b == "glassdoor"]
    main_boards = [b for b in boards if b != "glassdoor"]

    last_error: Exception | None = None
    for proxy in proxies:
        try:
            log.info(
                "Searching '%s' in '%s' (max_age=%dd) via %s",
                search_term, location, max_age_days, proxy,
            )
            loop = asyncio.get_running_loop()
            frames: list[pd.DataFrame] = []

            if main_boards:
                df_main: pd.DataFrame = await loop.run_in_executor(
                    None,
                    partial(
                        scrape_jobs,
                        **jobspy_kwargs(
                            main_boards, search_term, location, max_age_days,
                            proxy, indeed_country(location),
                        ),
                    ),
                )
                frames.append(df_main)

            if gd_boards:
                try:
                    df_gd: pd.DataFrame = await loop.run_in_executor(
                        None,
                        partial(
                            scrape_jobs,
                            **jobspy_kwargs(
                                gd_boards, search_term, gd_location,
                                max_age_days, proxy, glassdoor_country(location),
                            ),
                        ),
                    )
                    frames.append(df_gd)
                except Exception as gd_exc:
                    log.warning(
                        "Glassdoor failed for '%s': %s", search_term, gd_exc,
                    )

            if not frames:
                return pd.DataFrame()

            df = pd.concat(frames, ignore_index=True)
            log.info("Found %d results for '%s' in '%s'", len(df), search_term, location)
            return df

        except Exception as exc:
            log.error("Proxy %s failed for '%s': %s", proxy, search_term, exc)
            last_error = exc

    log.error(
        "All proxies failed for '%s' in '%s'. Last error: %s",
        search_term, location, last_error,
    )
    return pd.DataFrame()


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

    if not all_frames:
        return pd.DataFrame()

    merged = pd.concat(all_frames, ignore_index=True)

    # Deduplicate by job URL
    url_col = "job_url" if "job_url" in merged.columns else "link"
    if url_col in merged.columns:
        merged = merged.drop_duplicates(subset=[url_col], keep="first")

    log.info("Raw scrape: %d unique results for '%s' in '%s'", len(merged), keyword, location)
    return merged
