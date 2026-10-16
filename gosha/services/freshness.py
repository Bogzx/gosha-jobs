"""Public scrape freshness: is the scraper alive, as seen from the database.

The out-of-process watchdog (scripts/site_watchdog.py, run by GitHub
Actions) needs to tell "the bot is scraping" from "the bot is hung, crashed
or its server is gone" without credentials. The bot container has no HTTP
port; the database is the only thing both sides share, so the API answers
from there.

Only timestamps and one count — nothing about users, searches or postings.
Cached briefly so an unauthenticated endpoint cannot be turned into a
query-per-request load on the database.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

from sqlalchemy import func, select

from gosha.database import get_session
from gosha.events import Event
from gosha.models import Job

CACHE_SECONDS = 60.0

# (monotonic time, the answer or the error it raised). A failure is cached
# too: a database outage must not turn every probe into a fresh query.
_cache: tuple[float, dict | Exception] | None = None
_lock = asyncio.Lock()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


async def _compute() -> dict:
    async with get_session() as session:
        cycle = (await session.execute(
            select(Event.timestamp, Event._payload)
            .where(Event.event_type == "scrape.cycle")
            .order_by(Event.timestamp.desc())
            .limit(1)
        )).first()
        last_new = (await session.execute(select(func.max(Job.first_seen_at)))).scalar()
        last_seen = (await session.execute(select(func.max(Job.last_seen_at)))).scalar()

    cycle_jobs = None
    if cycle is not None:
        try:
            cycle_jobs = int(json.loads(cycle[1] or "{}").get("total"))
        except (ValueError, TypeError):
            cycle_jobs = None
    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "last_scrape_cycle_at": _iso(cycle[0]) if cycle is not None else None,
        "last_scrape_cycle_jobs": cycle_jobs,
        "last_new_job_at": _iso(last_new),
        "last_job_seen_at": _iso(last_seen),
    }


async def scrape_freshness() -> dict:
    global _cache
    async with _lock:
        if _cache is None or time.monotonic() - _cache[0] >= CACHE_SECONDS:
            try:
                result: dict | Exception = await _compute()
            except Exception as exc:
                result = exc
            _cache = (time.monotonic(), result)
        if isinstance(_cache[1], Exception):
            raise _cache[1]
        return _cache[1]


def reset_cache() -> None:
    global _cache
    _cache = None
