"""Hipo.ro adapter — student/graduate-focused Romanian board.

No public API; we parse the IT-Software listing HTML per city:
https://www.hipo.ro/locuri-de-munca/cautajob/IT-Software/<City>
(card structure probed 2026-06-10: a.job-title[title][href] + p.company-name).
Keyword relevance is left to GOSHA's own filters/semantic matching since
Hipo's IT-Software domain already narrows the field.

Detail pages carry a schema.org JobPosting JSON-LD block (probed
2026-09-30) with the description, qualifications and datePosted; they are
fetched per result through gosha/scrapers/details.py (capped, cached).

Probed 2026-10-12: the listing is paginated by a path suffix
(.../IT-Software/Bucuresti/2, 40 cards a page; a `?page=` query is
ignored), and career fairs and workshops are listed as cards too. Their
JSON-LD says employmentType "eveniment", which is how they are dropped.

Probed 2026-10-16: Bucharest's catalog runs to 9 pages (332 cards) and
the all-cities one to 12 (462), ordered by refresh date; the old 4-page
cap kept 160 of each. The listing page names only the city that was
searched, so an all-cities posting's real place comes from its JSON-LD
jobLocation (addressLocality "BUCURESTI , Ilfov ,").
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from gosha.filters import normalize_location
from gosha.scrapers.base import RawJob, SearchQuery, http_client
from gosha.scrapers.details import fetch_details, html_to_text, repair_mojibake

log = logging.getLogger(__name__)

BASE_URL = "https://www.hipo.ro"
LISTING_URL = BASE_URL + "/locuri-de-munca/cautajob/IT-Software/{city}"

# Hipo ignores keywords (city catalog pages), so keyword-expanded searches
# would refetch the same pages — cache per city for a few minutes.
CACHE_TTL_SECONDS = 600
_cache: dict[str, tuple[float, list[str]]] = {}
# Pages of one catalog. The all-cities one ran to 12 and Bucharest's to 9
# (2026-10-16); smaller cities fit on one. The cap only stops a runaway.
MAX_PAGES = 20
# schema.org employmentType values that are not jobs.
NON_JOB_TYPES = {"eveniment", "event"}

# City segment names hipo uses in its catalog URLs
CITY_SEGMENTS = {
    "bucharest": "Bucuresti",
    "bucuresti": "Bucuresti",
    "cluj-napoca": "Cluj-Napoca",
    "timisoara": "Timisoara",
    "iasi": "Iasi",
    "brasov": "Brasov",
    "sibiu": "Sibiu",
    "craiova": "Craiova",
    "constanta": "Constanta",
    "oradea": "Oradea",
    "romania": "Toate-Orasele",
}
ALL_CITIES = "Toate-Orasele"

# Network detail fetches per search. A city page lists ~20-40 postings and
# every keyword term re-searches the same city, so the cache does the work.
MAX_DETAIL_FETCHES = 20

_LD_JSON_RE = re.compile(
    r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.DOTALL,
)
# JobPosting fields that carry prose, in reading order. Hipo often repeats
# one paragraph across several of them; duplicates are dropped.
_PROSE_FIELDS = ("description", "responsibilities", "qualifications", "skills")


def _job_posting(page: str) -> dict[str, Any]:
    for block in _LD_JSON_RE.findall(page):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return {}


def _locality(posting: dict[str, Any]) -> str:
    """"Bucuresti, Ilfov" from jobLocation's "BUCURESTI , Ilfov ,"."""
    places = posting.get("jobLocation") or []
    places = [places] if isinstance(places, dict) else places
    names: list[str] = []
    for place in places:
        address = place.get("address") if isinstance(place, dict) else None
        if not isinstance(address, dict):
            continue
        for part in str(address.get("addressLocality") or "").split(","):
            part = repair_mojibake(part).strip()
            if part.isupper():
                part = part.title()
            if part and part not in names:
                names.append(part)
    return ", ".join(names)


def _extract_detail(page: str) -> dict[str, Any]:
    """{description, posted_at, location} from a job page's JobPosting JSON-LD."""
    posting = _job_posting(page)
    parts: list[str] = []
    for key in _PROSE_FIELDS:
        text = html_to_text(repair_mojibake(str(posting.get(key) or "")))
        if text and text not in parts:
            parts.append(text)
    detail: dict[str, Any] = {}
    if posting:
        types = posting.get("employmentType") or []
        types = [types] if isinstance(types, str) else types
        if any(str(t).strip().lower() in NON_JOB_TYPES for t in types):
            detail["not_a_job"] = True
    if parts:
        detail["description"] = "\n\n".join(parts)[:20000]
    locality = _locality(posting)
    if locality:
        detail["location"] = f"{locality}, Romania"
    raw_date = posting.get("datePosted")
    if raw_date:
        try:
            posted = datetime.fromisoformat(str(raw_date))
            detail["posted_at"] = (
                posted if posted.tzinfo else posted.replace(tzinfo=timezone.utc)
            )
        except ValueError:
            pass
    return detail


_CARD_RE = re.compile(
    r'<a\s+title="(?P<title>[^"]+)"\s+class="job-title"\s+href="(?P<href>[^"]+)".*?'
    r'<p class="company-name">\s*(?:<span>)?(?P<company>[^<]*)',
    re.DOTALL,
)


def city_segment(location: str) -> str | None:
    """Map a user location to hipo's URL segment; None when unsupported."""
    search_loc, _subs = normalize_location(location)
    city = search_loc.split(",")[0].strip().lower()
    city = (
        city.replace("ă", "a").replace("â", "a").replace("î", "i")
        .replace("ș", "s").replace("ş", "s").replace("ț", "t").replace("ţ", "t")
    )
    return CITY_SEGMENTS.get(city)


def _parse(html: str, segment: str) -> list[RawJob]:
    """Cards of one catalog page. `segment` is the city's URL segment."""
    # The all-cities catalog says nothing about the city on the card; the
    # detail page does (_locality).
    location = "Romania" if segment == ALL_CITIES else f"{segment.replace('-', ' ')}, Romania"
    jobs: list[RawJob] = []
    for match in _CARD_RE.finditer(html):
        href = html_lib.unescape(match.group("href").strip())
        title = html_lib.unescape(match.group("title")).strip()
        company = html_lib.unescape(match.group("company")).strip() or "Unknown"
        if not href or not title:
            continue
        url = href if href.startswith("http") else BASE_URL + href
        jobs.append(RawJob(
            url=url,
            title=title,
            company=company,
            location=location,
            description="",
            source="hipo",
        ))
    return jobs


async def _listing_pages(segment: str) -> list[str]:
    """Every catalog page for a city (cached), stopping at the first page
    that adds no new card."""
    now = time.monotonic()
    cached = _cache.get(segment)
    if cached is not None and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    pages: list[str] = []
    seen: set[str] = set()
    async with http_client() as client:
        for number in range(1, MAX_PAGES + 1):
            url = LISTING_URL.format(city=segment)
            if number > 1:
                url += f"/{number}"
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError:
                if number == 1:
                    raise
                break  # a later page failing still leaves the earlier ones
            hrefs = {m.group("href") for m in _CARD_RE.finditer(resp.text)}
            if not hrefs - seen:
                break
            seen |= hrefs
            pages.append(resp.text)
    _cache[segment] = (now, pages)
    return pages


class HipoScraper:
    name = "hipo"

    def applies_to(self, query: SearchQuery) -> bool:
        return city_segment(query.location) is not None  # hipo is Romania-only

    async def search(self, query: SearchQuery) -> list[RawJob]:
        segment = city_segment(query.location)
        if segment is None:
            return []
        try:
            jobs: list[RawJob] = []
            seen: set[str] = set()
            for html in await _listing_pages(segment):
                for job in _parse(html, segment):
                    if job.url not in seen:
                        seen.add(job.url)
                        jobs.append(job)
            async with http_client() as client:
                details = await fetch_details(
                    client,
                    [j.url for j in jobs if query.is_relevant(j.title, "entry")],
                    _extract_detail, MAX_DETAIL_FETCHES,
                )
            kept: list[RawJob] = []
            for job in jobs:
                detail = details.get(job.url) or {}
                if detail.get("not_a_job"):
                    continue
                if detail.get("description"):
                    job.description = detail["description"]
                if detail.get("posted_at"):
                    # datePosted is the last refresh (see RawJob.posted_at_is_refresh).
                    job.posted_at = detail["posted_at"]
                    job.posted_at_is_refresh = True
                if detail.get("location"):
                    job.location = detail["location"]
                kept.append(job)
            return kept
        except Exception as exc:
            log.warning("Hipo scrape failed for %r: %s", query.location, exc)
            return []
