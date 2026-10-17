"""BestJobs.ro adapter.

Endpoint (probed 2026-06-10):
GET https://api.bestjobs.eu/v1/jobs?keyword=<kw>&location=<city-slug>
Location slugs look like "cluj-napoca-romania". Salary strings are monthly
EUR. The list payload carries no description, so the detail page is
fetched for each result (gosha/scrapers/details.py: capped, cached): it is
a Next.js page whose __NEXT_DATA__ JSON holds props.pageProps.job with the
HTML description and the employer name (probed 2026-09-30).

Probed again 2026-10-12:
- `location` is honoured as a ranking hint (75% of a Cluj search is in
  Cluj), but "romania" and "europe" are not places to it: both come back
  ~100% remote. Without a location the API geolocates the caller's IP. So
  a nationwide search asks for the largest cities one by one, and a
  remote one passes "europe", which is what yields remote postings.
- `limit` and paging are ignored; every call returns up to 100 items.
- The detail page's job carries `careerLevels` (Entry / Fără experiență /
  Middle / Senior / Executive) and `expired`. Details are fetched for the
  titles that would be relevant at entry level, and the board's own level
  then decides (RawJob.experience_level).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from gosha.filters import fold, normalize_location
from gosha.scrapers.base import RawJob, SearchQuery, http_client
from gosha.scrapers.details import fetch_details, html_to_text
from gosha.scrapers.salary import parse_salary_range

log = logging.getLogger(__name__)

API_URL = "https://api.bestjobs.eu/v1/jobs"
JOB_URL_TEMPLATE = "https://www.bestjobs.eu/ro/loc-de-munca/{slug}"
MAX_RESULTS = 100
# Network detail fetches per search (cached ones are free). The first
# cycle after a restart fills the cache; later cycles fetch only new jobs.
MAX_DETAIL_FETCHES = 20

REMOTE_LOCATIONS = {"remote", "anywhere", "worldwide"}
NATIONWIDE_LOCATIONS = {"romania", "europe", "eu"}
NATIONWIDE_CITIES = ("bucuresti", "cluj-napoca", "timisoara", "iasi")

# careerLevels slugs on the detail page, most junior first: an employer
# who ticks several says who may apply.
_CAREER_LEVELS = (
    ("fara-experienta", "entry"),
    ("entry", "entry"),
    ("middle", "mid"),
    ("senior", "senior"),
    ("executive", "senior"),
)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL,
)


def experience_level(career_levels: list[Any] | None) -> str | None:
    slugs = [
        str(level.get("slug") or "") for level in career_levels or []
        if isinstance(level, dict)
    ]
    for prefix, label in _CAREER_LEVELS:
        if any(slug.startswith(prefix) for slug in slugs):
            return label
    return None


def _extract_detail(page: str) -> dict[str, Any]:
    """{description, company, experience_level, expired} from a job page."""
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
    level = experience_level(job.get("careerLevels"))
    if level:
        detail["experience_level"] = level
    if job.get("expired"):
        detail["expired"] = True
    return detail


def _without_deferred(jobs: list[RawJob], deferred: set[str]) -> list[RawJob]:
    """The jobs whose page is not left for a later cycle.

    Over the per-search cap a page waits for the next cycle (the cache keeps
    the ones fetched). Its posting waits with it: without the page, an
    unmarked title ("Frontend Developer") has no level and no description,
    so the junior filter lets it through, and once the page is read and
    says Middle the posting is filtered out and never updated again. After
    the 2026-10-16 deploys emptied the cache, 36 of the 40 BestJobs postings
    stored were such rows, still active and description-less.
    """
    if deferred:
        log.info("BestJobs: %d postings wait for their page next cycle", len(deferred))
    return [j for j in jobs if j.url not in deferred]


def _apply_details(
    jobs: list[RawJob], details: dict[str, dict[str, Any]],
    on_expired: Callable[[str], None] | None = None,
) -> list[RawJob]:
    """Fold detail pages into the jobs; drops postings the page calls expired
    and reports them to `on_expired`. The API keeps listing some of them
    (8311, 8312 on 2026-10-17), so without the report their stored rows
    stayed active: the page answers 200, which deadlinks takes as alive."""
    kept: list[RawJob] = []
    for job in jobs:
        detail = details.get(job.url) or {}
        if detail.get("expired"):
            if on_expired is not None:
                on_expired(job.url)
            continue
        if detail.get("description"):
            job.description = detail["description"]
        # Recruiter-posted jobs have an empty companyName in the list
        # payload; the page sometimes names the employer.
        if job.company == "Unknown" and detail.get("company"):
            job.company = detail["company"]
        if detail.get("experience_level"):
            job.experience_level = detail["experience_level"]
        kept.append(job)
    return kept


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

        # Only what the employer stated. `estimatedSalary` is BestJobs' own
        # guess for postings whose salary is confidential (the detail page
        # says salaryConfidential); it was stored as the posting's salary,
        # shown as such and used by the salary filter — on 2026-10-16 for
        # 36 of the 76 salaried postings in three searches.
        salary_text = item.get("salary") or ""
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


def search_locations(location: str) -> list[str | None]:
    """BestJobs `location` values to query for one search location."""
    key = fold(location.strip())
    if key in REMOTE_LOCATIONS:
        return ["europe"]
    if key in NATIONWIDE_LOCATIONS:
        return list(NATIONWIDE_CITIES)
    return [location_to_slug(location) or None]


class BestJobsScraper:
    name = "bestjobs"

    def applies_to(self, query: SearchQuery) -> bool:
        # BestJobs lists Romanian jobs plus remote ones; a foreign city's
        # search would return Romanian postings ranked by distance.
        key = fold(query.location.strip())
        if key in REMOTE_LOCATIONS or key in NATIONWIDE_LOCATIONS:
            return True
        search_loc, _subs = normalize_location(query.location)
        return "romania" in fold(search_loc) or key == fold(search_loc)

    async def search(self, query: SearchQuery) -> list[RawJob]:
        if not self.applies_to(query):
            return []
        try:
            async with http_client() as client:
                jobs: list[RawJob] = []
                seen: set[str] = set()
                for location in search_locations(query.location):
                    params: dict[str, Any] = {"keyword": query.keyword}
                    if location:
                        params["location"] = location
                    resp = await client.get(API_URL, params=params)
                    resp.raise_for_status()
                    for job in _parse(resp.json(), query):
                        if job.url not in seen:
                            seen.add(job.url)
                            jobs.append(job)
                # A title that would be relevant at entry level is worth a
                # page fetch: the page says whether it IS entry level.
                wanted = [j.url for j in jobs if query.is_relevant(j.title, "entry")]
                deferred: set[str] = set()
                details = await fetch_details(
                    client, wanted, _extract_detail, MAX_DETAIL_FETCHES, deferred,
                )
                return _apply_details(
                    _without_deferred(jobs, deferred), details, query.on_expired,
                )
        except Exception as exc:
            log.warning("BestJobs scrape failed for %r: %s", query.keyword, exc)
            return []
