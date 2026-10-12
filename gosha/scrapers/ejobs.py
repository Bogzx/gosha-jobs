"""eJobs.ro adapter — Romania's largest job board.

Endpoints (probed 2026-10-12, from the filters the ejobs.ro frontend sends):

    GET https://api.ejobs.ro/jobs?page&pageSize
        &q=<text>                    free-text search
        &filters.cities=<id>         server-side city filter
        &filters.departments=<id>    repeatable; values OR together
    GET https://api.ejobs.ro/cities?pageSize=500   id -> name/slug
    GET https://api.ejobs.ro/jobs/{id}             details.jobDescription

What changed from the first version, and why:

- Location was filtered client-side over the first 100 results
  NATIONWIDE ("the cityId param is ignored" — it is, but filters.cities is
  not), so a Cluj search saw the ~10 Cluj jobs that happened to rank in
  Romania's top 100.
- `/cities` returns 25 cities unless asked for more, so postings in any
  other town had no location at all.
- `q` is a loose OR search: "software engineer intern" matched 1,500
  postings, warehouse and accounting jobs first. For tech keywords the
  adapter now browses the IT departments of the city instead (a few
  hundred postings at most, all of them candidates) and leaves relevance
  to the pipeline's title filter and the semantic matcher. Non-tech
  keywords still use `q`.
- Each posting carries careerLevelsIds; they become RawJob.experience_level
  so an entry-level posting titled "QA Engineer" survives an internship
  search's title filter.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from gosha.filters import fold, is_romanian_location, keyword_wants_tech, normalize_location
from gosha.scrapers.base import DEFAULT_HEADERS, HTTP_TIMEOUT, RawJob, SearchQuery
from gosha.scrapers.details import fetch_details, html_to_text
from gosha.scrapers.salary import parse_salary_range

log = logging.getLogger(__name__)

API_URL = "https://api.ejobs.ro/jobs"
CITIES_URL = "https://api.ejobs.ro/cities"
JOB_URL_TEMPLATE = "https://www.ejobs.ro/user/locuri-de-munca/{slug}/{id}"
PAGE_SIZE = 100
# Bucharest's IT departments run to ~3 pages; the cap only stops a runaway.
MAX_PAGES = 6
CITY_PAGE_SIZE = 500
# Network detail fetches per search; cached pages are free (details.py).
MAX_DETAIL_FETCHES = 25

# IT Software, IT Hardware, Internet / e-Commerce, Telecomunicații,
# Statistică / Matematică — ids from /departments.
TECH_DEPARTMENTS = (57, 58, 31, 38, 46)

# /career-levels: 10 Fără experiență, 3 Entry-Level (<2 ani),
# 4 Mid-Level (2-5 ani), 6 Senior-Level (>5 ani), 5 Manager / Executiv.
# Employers often tick several; the most junior one wins, since it says
# who may apply.
_CAREER_LEVELS = ((10, "entry"), (3, "entry"), (4, "mid"), (6, "senior"), (5, "senior"))

# Every keyword term of one tech subscription browses the same city, so a
# browse is cached for a few minutes: one set of requests per cycle.
BROWSE_TTL_SECONDS = 600
_browse_cache: dict[tuple, tuple[float, list[dict]]] = {}

# The largest cities, from /cities (2026-10-12). Used when that endpoint
# fails: it answers 403 to some datacenter IPs (GitHub's runners since
# 2026-10-05) while /jobs still works, and a failed lookup used to throw
# away the whole board.
FALLBACK_CITIES = {
    1: "București", 14: "Cluj-Napoca", 34: "Timișoara", 21: "Iași",
    15: "Constanța", 16: "Craiova", 10: "Brașov", 5: "Bacău", 9: "Brăila",
    20: "Galați", 23: "Oradea", 25: "Ploiești", 4: "Arad", 6: "Baia Mare",
    32: "Sibiu", 24: "Pitești", 35: "Târgu Mureș", 33: "Suceava",
    30: "Satu Mare", 28: "Râmnicu Vâlcea", 51: "Alba Iulia", 17: "Deva",
    8: "Bistrița", 44: "Piatra Neamț", 69: "Târgu Jiu", 19: "Focșani",
    82: "Otopeni",
}

_cities: dict[int, str] | None = None
_city_ids: dict[str, int] = {}

_PAREN_RE = re.compile(r"\s*\(.*?\)\s*")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(text)).strip("-")


def _extract_description(detail: dict) -> str:
    """Readable plain-text description from a /jobs/{id} payload."""
    details = detail.get("details") or {}
    parts = [details.get("jobDescription"), details.get("idealCandidate")]
    return html_to_text("\n\n".join(p for p in parts if p))


def _extract_detail(body: str) -> dict[str, Any]:
    description = _extract_description(json.loads(body))
    return {"description": description} if description else {}


def experience_level(career_level_ids: list[Any] | None) -> str | None:
    ids = set(career_level_ids or [])
    for level_id, label in _CAREER_LEVELS:
        if level_id in ids:
            return label
    return None


def _index_cities(payload: dict) -> tuple[dict[int, str], dict[str, int]]:
    """(id -> display name, slug -> id) from a /cities payload."""
    names: dict[int, str] = {}
    ids: dict[str, int] = {}
    for city in payload.get("cities", []):
        if city.get("id") is None or not city.get("name"):
            continue
        city_id = int(city["id"])
        # "Iași (Iași)" -> "Iași": the county in brackets is noise here.
        name = _PAREN_RE.sub(" ", str(city["name"])).strip()
        names[city_id] = name
        ids.setdefault(_slug(name), city_id)
        if city.get("slug"):
            ids.setdefault(_slug(str(city["slug"])), city_id)
    return names, ids


async def _get_cities(client: httpx.AsyncClient) -> dict[int, str]:
    global _cities
    if _cities is not None:
        return _cities
    try:
        resp = await client.get(CITIES_URL, params={"pageSize": CITY_PAGE_SIZE})
        resp.raise_for_status()
        names, ids = _index_cities(resp.json())
        _cities = names
    except (httpx.HTTPError, ValueError) as exc:
        # Not cached: the next search retries the full list.
        log.warning("eJobs city list unavailable (%s); using the built-in table", exc)
        names, ids = _index_cities({"cities": [
            {"id": cid, "name": name} for cid, name in FALLBACK_CITIES.items()
        ]})
    _city_ids.clear()
    _city_ids.update(ids)
    return names


def _nationwide(location: str) -> bool:
    return fold(location.strip()) in ("romania", "europe", "eu")


def city_id_for(location: str) -> int | None:
    """eJobs city id for a search location, None when unknown/nationwide."""
    search_loc, match_subs = normalize_location(location)
    candidates = [search_loc.split(",")[0], location, *match_subs]
    for candidate in candidates:
        city_id = _city_ids.get(_slug(candidate))
        if city_id is not None:
            return city_id
    return None


async def _browse(
    client: httpx.AsyncClient, params: dict[str, Any],
) -> list[dict]:
    """Every page of one listing (up to MAX_PAGES), cached briefly."""
    key = tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in params.items()))
    now = time.monotonic()
    cached = _browse_cache.get(key)
    if cached is not None and now - cached[0] < BROWSE_TTL_SECONDS:
        return cached[1]

    items: list[dict] = []
    seen: set[Any] = set()
    for page in range(1, MAX_PAGES + 1):
        resp = await client.get(
            API_URL, params={**params, "page": page, "pageSize": PAGE_SIZE},
        )
        resp.raise_for_status()
        payload = resp.json()
        batch = [j for j in payload.get("jobs") or [] if isinstance(j, dict)]
        new = [j for j in batch if j.get("id") not in seen]
        seen.update(j.get("id") for j in new)
        items.extend(new)
        if not new or not payload.get("morePagesFollow"):
            break

    # Drop expired listings so free-text searches don't accumulate for the
    # life of the process.
    for stale in [k for k, (at, _) in _browse_cache.items() if now - at >= BROWSE_TTL_SECONDS]:
        del _browse_cache[stale]
    _browse_cache[key] = (now, items)
    return items


def clear_cache() -> None:
    """Forget cities and cached listings (tests, manual re-probe)."""
    global _cities
    _cities = None
    _city_ids.clear()
    _browse_cache.clear()


def _parse(
    items: list[dict],
    city_names: dict[int, str],
    max_age_days: int | None = None,
) -> list[RawJob]:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max_age_days)
        if max_age_days
        else None
    )
    jobs: list[RawJob] = []
    for item in items:
        job_id = item.get("id")
        if not job_id or not item.get("title"):
            continue

        posted_at: datetime | None = None
        raw_date = item.get("creationDate")
        if raw_date:
            try:
                posted_at = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
            except ValueError:
                posted_at = None
        if cutoff and posted_at and posted_at < cutoff:
            continue

        names = [
            city_names.get(loc.get("cityId"), "")
            for loc in item.get("locations") or []
            if isinstance(loc, dict)
        ]
        location_text = ", ".join(dict.fromkeys(n for n in names if n))

        salary_min, salary_max, currency = parse_salary_range(
            item.get("salary"), default_currency="RON",
        )
        company = (item.get("company") or {}).get("name") or "Unknown"
        jobs.append(RawJob(
            url=JOB_URL_TEMPLATE.format(slug=item.get("slug") or "job", id=job_id),
            title=str(item["title"]).strip(),
            company=str(company).strip() or "Unknown",
            location=f"{location_text}, Romania" if location_text else "Romania",
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            # eJobs quotes gross monthly figures.
            salary_period="monthly" if (salary_min or salary_max) else None,
            posted_at=posted_at,
            source="ejobs",
            experience_level=experience_level(item.get("careerLevelsIds")),
        ))
    return jobs


def _detail_url(job: RawJob) -> str:
    return f"{API_URL}/{job.url.rstrip('/').rsplit('/', 1)[-1]}"


class EjobsScraper:
    name = "ejobs"

    def applies_to(self, query: SearchQuery) -> bool:
        return is_romanian_location(query.location)

    async def search(self, query: SearchQuery) -> list[RawJob]:
        if not self.applies_to(query):
            return []
        try:
            async with httpx.AsyncClient(
                headers=DEFAULT_HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True,
            ) as client:
                city_names = await _get_cities(client)
                params: dict[str, Any] = {}
                # Nationwide first: "romania"'s match list names Bucharest,
                # which would otherwise narrow it to one city.
                if not _nationwide(query.location):
                    city_id = city_id_for(query.location)
                    if city_id is None:
                        return []  # a town eJobs does not list
                    params["filters.cities"] = city_id
                if keyword_wants_tech(query.keyword):
                    params["filters.departments"] = list(TECH_DEPARTMENTS)
                else:
                    params["q"] = query.keyword

                jobs = _parse(await _browse(client, params), city_names, query.max_age_days)
                wanted = [j for j in jobs if query.is_relevant(j.title, j.experience_level)]
                details = await fetch_details(
                    client, [_detail_url(j) for j in wanted], _extract_detail,
                    MAX_DETAIL_FETCHES,
                )
                for job in wanted:
                    description = (details.get(_detail_url(job)) or {}).get("description")
                    if description:
                        job.description = description
                return jobs
        except Exception as exc:
            log.warning("eJobs scrape failed for %r in %r: %s", query.keyword, query.location, exc)
            return []
