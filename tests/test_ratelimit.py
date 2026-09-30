"""Database-backed rate limiting (gosha/ratelimit.py)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gosha import ratelimit
from gosha.models import RateLimitHit
from gosha.ratelimit import Limit, hit

RULE = Limit("test", 3, 60)


async def test_limit_then_reset_per_client(patched_db):
    t = 1_000_040.0  # 20 s into the window starting at 1_000_020
    for _ in range(3):
        assert (await hit(RULE, "1.2.3.4", now=t))[0]
    allowed, retry_after = await hit(RULE, "1.2.3.4", now=t + 10)
    assert not allowed
    assert retry_after == 30  # until the window boundary

    assert (await hit(RULE, "5.6.7.8", now=t + 10))[0]  # other client
    assert (await hit(Limit("other", 3, 60), "1.2.3.4", now=t + 10))[0]  # other scope
    assert (await hit(RULE, "1.2.3.4", now=t + 41))[0]  # next window


async def test_counts_are_shared_not_per_process(patched_db, session: AsyncSession):
    """The count lives in one DB row, not in the calling process."""
    t = 2_000_000.0
    for _ in range(2):
        await hit(RULE, "c", now=t)
    row = (await session.execute(select(RateLimitHit))).scalar_one()
    assert row.count == 2 and row.bucket.startswith("test:c:")


async def test_expired_windows_are_purged(patched_db, session: AsyncSession):
    await hit(RULE, "old", now=3_000_000.0)
    await hit(RULE, "new", now=3_000_000.0 + 3600)  # new window row -> purge
    rows = (await session.execute(select(func.count()).select_from(RateLimitHit))).scalar()
    assert rows == 1


async def test_fails_open_when_the_database_is_down(monkeypatch):
    def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(ratelimit, "get_session", broken)
    assert (await hit(RULE, "x"))[0]
