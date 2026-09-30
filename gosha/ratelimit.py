"""Fixed-window rate limiting shared through the database.

The previous limiter (pageview only) was a dict in the API process: each
uvicorn worker had its own counts, so N workers meant N times the limit,
and a restart forgot everything. Counts now live in `rate_limit_hits`, one
row per (scope, client, window), incremented with a single atomic upsert
(`INSERT … ON CONFLICT DO UPDATE … RETURNING`) on both Postgres and SQLite,
so concurrent workers cannot race past the limit.

Fixed windows allow a burst of up to 2x the limit across a window
boundary; for abuse protection on a handful of endpoints that is fine and
keeps it to one row write per request. Expired rows are deleted whenever a
new window row is created, so the table stays small without a cron.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete

from gosha.database import get_session
from gosha.models import RateLimitHit

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limit:
    scope: str
    limit: int
    window_seconds: int


# Sign-in start and the OAuth callback: a person needs a few; a script
# hammering Discord's token endpoint through us needs many.
AUTH = Limit("auth", 20, 60)
# The SPA polls the handoff every 2 s for up to 5 min (web/src/lib/signin.ts)
# — 30 a minute per waiting tab, so this leaves room for two tabs.
AUTH_HANDOFF = Limit("auth-handoff", 90, 60)
# Each CV upload parses a file and runs the embedding model.
CV_UPLOAD = Limit("cv-upload", 10, 3600)
# Unauthenticated analytics writes (over-limit events are dropped silently).
PAGEVIEW = Limit("pageview", 30, 60)


def _insert_for(dialect: str):
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


async def hit(rule: Limit, client: str, now: float | None = None) -> tuple[bool, int]:
    """Count one request; (allowed, seconds until the window resets).

    Fails open: if the database is unreachable the request is allowed and
    the error logged — a limiter must not become the outage.
    """
    now = time.time() if now is None else now
    window = int(now // rule.window_seconds)
    reset_at = (window + 1) * rule.window_seconds
    retry_after = max(1, int(reset_at - now))
    bucket = f"{rule.scope}:{client}:{window}"[:255]
    expires = datetime.fromtimestamp(reset_at, tz=timezone.utc)

    try:
        async with get_session() as session:
            insert = _insert_for(session.bind.dialect.name)
            stmt = (
                insert(RateLimitHit)
                .values(bucket=bucket, count=1, expires_at=expires)
                .on_conflict_do_update(
                    index_elements=[RateLimitHit.bucket],
                    set_={"count": RateLimitHit.count + 1},
                )
                .returning(RateLimitHit.count)
            )
            count = (await session.execute(stmt)).scalar_one()
            if count == 1:
                await session.execute(
                    delete(RateLimitHit).where(
                        RateLimitHit.expires_at
                        < datetime.fromtimestamp(now, tz=timezone.utc)
                    )
                )
            await session.commit()
    except Exception as exc:
        log.warning("Rate limiter unavailable (%s) — allowing request", exc)
        return True, retry_after

    return count <= rule.limit, retry_after
