"""BestJobs.ro adapter.

Endpoint (probed 2026-06-10):
GET https://api.bestjobs.eu/v1/jobs?keyword=<kw>&location=<city-slug>
Location slugs look like "cluj-napoca-romania". Salary strings are monthly
EUR. The list payload carries no description, so the detail page is
fetched for each result (gosha/scrapers/details.py: capped, cached): it is
a Next.js page whose __NEXT_DATA__ JSON holds props.pageProps.job with the
HTML description and the employer name (probed 2026-09-30).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from gosha.filters import normalize_location
from gosha.scrapers.base import DEFAULT_HEADERS, HTTP_TIMEOUT, RawJob, SearchQuery
from gosha.scrapers.details import fetch_details, html_to_text
from gosha.scrapers.salary import parse_salary_range

log = logging.getLogger(__name__)

API_URL = "https://api.bestjobs.eu/v1/jobs"
JOB_URL_TEMPLATE = "https://www.bestjobs.eu/ro/loc-de-munca/{slug}"
MAX_RESULTS = 100
# Network detail fetches per search (cached ones are free). The first
# cycle after a restart fills the cache; later cycles fetch only new jobs.
MAX_DETAIL_FETCHES = 20

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL,
)


def _extract_detail(page: str) -> dict[str, Any]:
    """{description, company} from a job page's __NEXT_DATA__."""
    match = _NEXT_DATA_RE.search(page)
    if not match:
        return {}
    job = (
        json.loads(match.group(1)).get("props", {}).get("pageProps", {}).get("job")
        or {}
    )
    detail: dict[str, Any] = {}
    description = html_to_text(job.get("description"))
    if description:
        detail["description"] = description
    employer = (job.get("employer") or {}).get("employerName")
    if employer:
        detail["company"] = str(employer).strip()
    return detail


def _apply_details(jobs: list[RawJob], details: dict[str, dict[str, Any]]) -> None:
    for job in jobs:
        detail = details.get(job.url) or {}
        if detail.get("description"):
            job.description = detail["description"]
        # Recruiter-posted jobs have an empty companyName in the list
        # payload; the page sometimes names the employer.
        if job.company == "Unknown" and detail.get("company"):
            job.company = detail["company"]


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
                jobs = _parse(resp.json(), query)
                details = await fetch_details(
                    client, [j.url for j in jobs], _extract_detail, MAX_DETAIL_FETCHES,
                )
                _apply_details(jobs, details)
                return jobs
        except Exception as exc:
            log.warning("BestJobs scrape failed for %r: %s", query.keyword, exc)
            return []
