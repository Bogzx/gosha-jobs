"""Jobicy adapter — remote jobs open to Europe, from Jobicy's public API.

Endpoint (probed 2026-10-12, docs https://jobi.cy/apidocs):

    GET https://jobicy.com/api/v2/remote-jobs?count=100&geo=europe[&cursor=…]

No key is needed. The terms ask for two things, both honoured here:
credit with a link to the source (every posting links to its jobicy.com
page and carries source="jobicy"), and polling no more than once an hour
(the feed is fetched at most every CACHE_TTL_SECONDS, a few pages per
fetch). Like RemoteOK, the feed has no server-side keyword search, so it
is filtered by keyword tokens locally and only serves remote-friendly
search locations.
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from gosha.scrapers.base import RawJob, SearchQuery, http_client
from gosha.scrapers.details import html_to_text

log = logging.getLogger(__name__)

API_URL = "https://jobicy.com/api/v2/remote-jobs"
PAGE_SIZE = 100
MAX_PAGES = 3
# Three hours: the API asks for no more than one poll an hour.
CACHE_TTL_SECONDS = 3 * 3600
# After a failed poll the last good feed (or nothing) is served this long
# before trying again: every keyword term of every remote search asks, and
# an outage must not turn into a request per term.
FAILURE_BACKOFF_SECONDS = 3600

REMOTE_LOCATIONS = {"remote", "europe", "eu", "anywhere", "worldwide"}

_LEVELS = {
    "internship": "intern",
    "intern": "intern",
    "entry": "entry",
    "junior": "entry",
    "midweight": "mid",
    "mid": "mid",
    "senior": "senior",
    "lead": "senior",
    "director": "senior",
    "manager": "senior",
}

_cache: tuple[float, list[dict]] | None = None


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)] if value else []


def experience_level(job_level: Any) -> str | None:
    return _LEVELS.get(str(job_level or "").strip().lower())


def _parse(feed: list[dict], query: SearchQuery) -> list[RawJob]:
    """Filter the feed by keyword tokens and posting age."""
    tokens = [t for t in re.split(r"[^a-z0-9+#]+", query.keyword) if len(t) > 1]
    cutoff = datetime.now(timezone.utc) - timedelta(days=query.max_age_days)

    jobs: list[RawJob] = []
    for item in feed:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        title = html.unescape(str(item.get("jobTitle") or "")).strip()
        if not title or not url.startswith(("https://", "http://")):
            continue

        haystack = " ".join([title, *_as_list(item.get("jobIndustry"))]).lower()
        if tokens and not any(token in haystack for token in tokens):
            continue

        posted_at: datetime | None = None
        if item.get("pubDate"):
            try:
                posted_at = datetime.fromisoformat(str(item["pubDate"]))
                if posted_at.tzinfo is None:
                    posted_at = posted_at.replace(tzinfo=timezone.utc)
            except ValueError:
                posted_at = None
        if posted_at is not None and posted_at < cutoff:
            continue

        salary_min = _number(item.get("salaryMin"))
        salary_max = _number(item.get("salaryMax"))
        currency = period = None
        if salary_min or salary_max:
            currency = str(item.get("salaryCurrency") or "") or None
            period = str(item.get("salaryPeriod") or "") or None
        geo = html.unescape(str(item.get("jobGeo") or "")).strip()
        jobs.append(RawJob(
            url=url,
            title=title,
            company=html.unescape(str(item.get("companyName") or "")).strip() or "Unknown",
            location=f"Remote ({geo})" if geo else "Remote",
            description=html_to_text(str(item.get("jobDescription") or "")),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            salary_period=period,
            posted_at=posted_at,
            source="jobicy",
            experience_level=experience_level(item.get("jobLevel")),
        ))
    return jobs


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


async def _feed() -> list[dict]:
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]
    items: list[dict] = []
    cursor: str | None = None
    try:
        async with http_client() as client:
            for _ in range(MAX_PAGES):
                params: dict[str, Any] = {"count": PAGE_SIZE, "geo": "europe"}
                if cursor:
                    params["cursor"] = cursor
                resp = await client.get(API_URL, params=params)
                resp.raise_for_status()
                payload = resp.json()
                items.extend(j for j in payload.get("jobs") or [] if isinstance(j, dict))
                cursor = payload.get("nextCursor")
                if not payload.get("hasMore") or not cursor:
                    break
    except Exception as exc:
        stale = _cache[1] if _cache else []
        _cache = (now - CACHE_TTL_SECONDS + FAILURE_BACKOFF_SECONDS, stale)
        log.warning("Jobicy poll failed (%s); retrying in %ds", exc, FAILURE_BACKOFF_SECONDS)
        return stale
    _cache = (now, items)
    return items


def clear_cache() -> None:
    global _cache
    _cache = None


class JobicyScraper:
    name = "jobicy"

    def applies_to(self, query: SearchQuery) -> bool:
        return query.location.strip().lower() in REMOTE_LOCATIONS

    async def search(self, query: SearchQuery) -> list[RawJob]:
        if not self.applies_to(query):
            return []
        try:
            return _parse(await _feed(), query)
        except Exception as exc:
            log.warning("Jobicy scrape failed: %s", exc)
            return []
