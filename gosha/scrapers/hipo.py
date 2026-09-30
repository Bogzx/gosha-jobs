"""Hipo.ro adapter — student/graduate-focused Romanian board.

No public API; we parse the IT-Software listing HTML per city:
https://www.hipo.ro/locuri-de-munca/cautajob/IT-Software/<City>
(card structure probed 2026-06-10: a.job-title[title][href] + p.company-name).
Keyword relevance is left to GOSHA's own filters/semantic matching since
Hipo's IT-Software domain already narrows the field.

Detail pages carry a schema.org JobPosting JSON-LD block (probed
2026-09-30) with the description, qualifications and datePosted; they are
fetched per result through gosha/scrapers/details.py (capped, cached).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from gosha.filters import normalize_location
from gosha.scrapers.base import DEFAULT_HEADERS, HTTP_TIMEOUT, RawJob, SearchQuery
from gosha.scrapers.details import fetch_details, html_to_text, repair_mojibake

log = logging.getLogger(__name__)

BASE_URL = "https://www.hipo.ro"
LISTING_URL = BASE_URL + "/locuri-de-munca/cautajob/IT-Software/{city}"

# Hipo ignores keywords (city catalog pages), so keyword-expanded searches
# would refetch the same page — cache per city for a few minutes.
CACHE_TTL_SECONDS = 600
_cache: dict[str, tuple[float, str]] = {}

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


def _extract_detail(page: str) -> dict[str, Any]:
    """{description, posted_at} from a job page's JobPosting JSON-LD."""
    posting = _job_posting(page)
    parts: list[str] = []
    for key in _PROSE_FIELDS:
        text = html_to_text(repair_mojibake(str(posting.get(key) or "")))
        if text and text not in parts:
            parts.append(text)
    detail: dict[str, Any] = {}
    if parts:
        detail["description"] = "\n\n".join(parts)[:20000]
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


def _parse(html: str, city_label: str) -> list[RawJob]:
    jobs: list[RawJob] = []
    for match in _CARD_RE.finditer(html):
        href = match.group("href").strip()
        title = match.group("title").strip()
        company = match.group("company").strip() or "Unknown"
        if not href or not title:
            continue
        url = href if href.startswith("http") else BASE_URL + href
        jobs.append(RawJob(
            url=url,
            title=title,
            company=company,
            location=f"{city_label}, Romania" if city_label != "Toate-Orasele" else "Romania",
            description="",
            source="hipo",
        ))
    return jobs


class HipoScraper:
    name = "hipo"

    async def search(self, query: SearchQuery) -> list[RawJob]:
        segment = city_segment(query.location)
        if segment is None:
            return []  # hipo is Romania-only
        try:
            now = time.monotonic()
            cached = _cache.get(segment)
            if cached is not None and now - cached[0] < CACHE_TTL_SECONDS:
                html = cached[1]
            else:
                async with httpx.AsyncClient(
                    headers=DEFAULT_HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True,
                ) as client:
                    resp = await client.get(LISTING_URL.format(city=segment))
                    resp.raise_for_status()
                    html = resp.text
                _cache[segment] = (now, html)
            jobs = _parse(html, segment.replace("-", " "))
            async with httpx.AsyncClient(
                headers=DEFAULT_HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True,
            ) as client:
                details = await fetch_details(
                    client, [j.url for j in jobs], _extract_detail, MAX_DETAIL_FETCHES,
                )
            for job in jobs:
                detail = details.get(job.url) or {}
                if detail.get("description"):
                    job.description = detail["description"]
                if detail.get("posted_at"):
                    job.posted_at = detail["posted_at"]
            return jobs
        except Exception as exc:
            log.warning("Hipo scrape failed for %r: %s", query.location, exc)
            return []
