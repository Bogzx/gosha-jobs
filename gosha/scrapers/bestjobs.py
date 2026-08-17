"""BestJobs.ro adapter.

Endpoint (probed 2026-06-10):
GET https://api.bestjobs.eu/v1/jobs?keyword=<kw>&location=<city-slug>
Location slugs look like "cluj-napoca-romania". Salary strings are monthly
EUR. The list payload carries no description.
"""

from __future__ import annotations

import logging
import re

import httpx

from gosha.filters import normalize_location
from gosha.scrapers.base import DEFAULT_HEADERS, HTTP_TIMEOUT, RawJob, SearchQuery
from gosha.scrapers.salary import parse_salary_range

log = logging.getLogger(__name__)

API_URL = "https://api.bestjobs.eu/v1/jobs"
JOB_URL_TEMPLATE = "https://www.bestjobs.eu/ro/loc-de-munca/{slug}"
MAX_RESULTS = 100


def location_to_slug(location: str) -> str:
    """'Cluj-Napoca, Romania' -> 'cluj-napoca-romania'."""
    search_loc, _subs = normalize_location(location)
    slug = search_loc.lower()
    slug = re.sub(r"[ăâ]", "a", slug)
    slug = re.sub(r"[î]", "i", slug)
    slug = re.sub(r"[șş]", "s", slug)
    slug = re.sub(r"[țţ]", "t", slug)
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug


def _parse(payload: dict, query: SearchQuery) -> list[RawJob]:
    jobs: list[RawJob] = []
    for item in payload.get("items", []):
        slug = item.get("slug")
        if not slug or not item.get("title"):
            continue

        locations = item.get("locations") or []
        location_text = ", ".join(
            str(loc.get("name", "")) for loc in locations if isinstance(loc, dict)
        ) or "Romania"

        salary_text = item.get("salary") or item.get("estimatedSalary") or ""
        salary_min, salary_max, currency = parse_salary_range(
            salary_text, default_currency="EUR",
        )

        jobs.append(RawJob(
            url=JOB_URL_TEMPLATE.format(slug=slug),
            title=str(item.get("title", "")),
            company=str(item.get("companyName") or "Unknown"),
            location=location_text,
            description="",
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            # BestJobs quotes monthly figures, usually in EUR.
            salary_period="monthly" if (salary_min or salary_max) else None,
            posted_at=None,  # list payload has no posting date
            source="bestjobs",
        ))
    return jobs[:MAX_RESULTS]


class BestJobsScraper:
    name = "bestjobs"

    async def search(self, query: SearchQuery) -> list[RawJob]:
        try:
            params = {"keyword": query.keyword, "limit": MAX_RESULTS}
            slug = location_to_slug(query.location)
            # Remote/Europe-wide queries skip the location filter
            if slug and query.location.strip().lower() not in ("remote", "europe", "eu"):
                params["location"] = slug
            async with httpx.AsyncClient(
                headers=DEFAULT_HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True,
            ) as client:
                resp = await client.get(API_URL, params=params)
                resp.raise_for_status()
                return _parse(resp.json(), query)
        except Exception as exc:
            log.warning("BestJobs scrape failed for %r: %s", query.keyword, exc)
            return []
