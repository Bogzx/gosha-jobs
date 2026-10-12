"""RemoteOK adapter — public JSON feed at https://remoteok.com/api.

The feed is global (no server-side search), so we fetch once per cycle
(module-level cache) and filter by keyword tokens locally. Only relevant
when the query is remote-friendly: location 'remote' or worldwide-ish.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from gosha.scrapers.base import DEFAULT_HEADERS, HTTP_TIMEOUT, RawJob, SearchQuery
from gosha.scrapers.details import html_to_text, repair_mojibake

log = logging.getLogger(__name__)

API_URL = "https://remoteok.com/api"
CACHE_TTL_SECONDS = 600
# After a failed fetch the last good feed (or nothing) is served this long:
# every keyword term asks, and an outage must not mean a request per term.
FAILURE_BACKOFF_SECONDS = 600

_cache: tuple[float, list[dict]] | None = None

REMOTE_LOCATIONS = {"remote", "europe", "eu", "anywhere", "worldwide"}


def _strip_html(html: str) -> str:
    """HTML to readable plain text. The feed double-encodes some UTF-8
    ("youâ\x80\x99ll"), which repair_mojibake undoes."""
    return html_to_text(repair_mojibake(html or ""))


def _text(value: object) -> str:
    return repair_mojibake(str(value or "")).strip()


def _parse(feed: list[dict], query: SearchQuery) -> list[RawJob]:
    """Filter the global feed by keyword tokens and posting age."""
    tokens = [t for t in re.split(r"[^a-z0-9+#]+", query.keyword) if len(t) > 1]
    cutoff = datetime.now(timezone.utc) - timedelta(days=query.max_age_days)

    jobs: list[RawJob] = []
    for item in feed:
        if not isinstance(item, dict) or not item.get("position"):
            continue

        haystack = " ".join([
            str(item.get("position", "")),
            " ".join(str(t) for t in item.get("tags") or []),
        ]).lower()
        if tokens and not any(token in haystack for token in tokens):
            continue

        posted_at: datetime | None = None
        raw_date = item.get("date")
        if raw_date:
            try:
                posted_at = datetime.fromisoformat(str(raw_date))
                if posted_at.tzinfo is None:
                    posted_at = posted_at.replace(tzinfo=timezone.utc)
            except ValueError:
                posted_at = None
        if posted_at is not None and posted_at < cutoff:
            continue

        salary_min = float(item["salary_min"]) if item.get("salary_min") else None
        salary_max = float(item["salary_max"]) if item.get("salary_max") else None

        jobs.append(RawJob(
            url=str(item.get("url", "")),
            title=_text(item.get("position")),
            company=_text(item.get("company")) or "Unknown",
            location=_text(item.get("location")) or "Remote",
            description=_strip_html(str(item.get("description", ""))),
            salary_min=salary_min or None,
            salary_max=salary_max or None,
            salary_currency="USD" if (salary_min or salary_max) else None,
            # RemoteOK publishes ANNUAL USD. Left unlabelled it looks like
            # a huge monthly figure next to eJobs' monthly RON.
            salary_period="yearly" if (salary_min or salary_max) else None,
            posted_at=posted_at,
            source="remoteok",
        ))
    # The url is shown as a link; anything but http(s) is dropped.
    return [j for j in jobs if j.url.startswith(("https://", "http://"))]


class RemoteOKScraper:
    name = "remoteok"

    def applies_to(self, query: SearchQuery) -> bool:
        return query.location.strip().lower() in REMOTE_LOCATIONS

    async def search(self, query: SearchQuery) -> list[RawJob]:
        if not self.applies_to(query):
            return []
        global _cache
        now = time.monotonic()
        try:
            if _cache is None or now - _cache[0] > CACHE_TTL_SECONDS:
                async with httpx.AsyncClient(
                    headers=DEFAULT_HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True,
                ) as client:
                    resp = await client.get(API_URL)
                resp.raise_for_status()
                data = resp.json()
                _cache = (now, data if isinstance(data, list) else [])
            return _parse(_cache[1], query)
        except Exception as exc:
            log.warning("RemoteOK scrape failed: %s", exc)
            stale = _cache[1] if _cache else []
            _cache = (now - CACHE_TTL_SECONDS + FAILURE_BACKOFF_SECONDS, stale)
            try:
                return _parse(stale, query)
            except Exception:
                return []
