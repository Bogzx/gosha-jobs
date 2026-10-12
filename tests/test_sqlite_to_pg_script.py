"""Tests for the one-time SQLite -> Postgres data copy script.

The copy logic is database-agnostic (SQLAlchemy), so the test exercises it
with two SQLite files; production runs it with a postgresql+asyncpg dst URL.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from gosha.events import Event
from gosha.models import (
    Application,
    Base,
    CoverLetter,
    Job,
    Subscription,
    User,
    UserJob,
)
from scripts.migrate_sqlite_to_postgres import copy_all


@pytest.mark.asyncio
async def test_copy_all_moves_every_table(tmp_path):
    src_url = f"sqlite+aiosqlite:///{tmp_path / 'src.db'}"
    dst_url = f"sqlite+aiosqlite:///{tmp_path / 'dst.db'}"

    # Seed the source database
    src_engine = create_async_engine(src_url)
    async with src_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(src_engine, expire_on_commit=False)
    async with factory() as session:
        user = User(discord_user_id=111, username="gosha", tier="pro")
        session.add(user)
        await session.flush()

        job1 = Job(url="https://x/1", title="Dev", company="A", source="indeed")
        job2 = Job(url="https://x/2", title="QA", company="B", source="linkedin")
        session.add_all([job1, job2])
        await session.flush()

        sub = Subscription(user_id=user.id)
        sub.keywords = ["dev"]
        sub.locations = ["cluj"]
        session.add(sub)
        await session.flush()

        session.add_all([
            UserJob(user_id=user.id, job_id=job1.id, subscription_id=sub.id),
            Application(user_id=user.id, job_id=job1.id, status="interview"),
            CoverLetter(user_id=user.id, job_id=job1.id, content="Dear..."),
            Event(event_type="job.discovered", job_id=job1.id),
            Event(event_type="web.signin", actor_id=user.id),
            Event(event_type="user.feedback", actor_id=user.id, job_id=job1.id),
        ])
        await session.commit()
    await src_engine.dispose()

    copied = await copy_all(src_url, dst_url)

    assert copied["users"] == 1
    assert copied["jobs"] == 2
    assert copied["subscriptions"] == 1
    assert copied["user_jobs"] == 1
    assert copied["applications"] == 1
    assert copied["cover_letters"] == 1
    assert copied["events"] == 3
    assert copied["outbox"] == 0

    # Verify integrity in the destination
    dst_engine = create_async_engine(dst_url)
    dst_factory = async_sessionmaker(dst_engine, expire_on_commit=False)
    async with dst_factory() as session:
        u = (await session.execute(select(User))).scalar_one()
        assert u.discord_user_id == 111
        assert u.tier == "pro"

        s = (await session.execute(select(Subscription))).scalar_one()
        assert s.keywords == ["dev"]
        assert s.user_id == u.id

        n_jobs = (await session.execute(select(func.count(Job.id)))).scalar()
        assert n_jobs == 2
    await dst_engine.dispose()


@pytest.mark.asyncio
async def test_copy_all_sanitizes_broken_foreign_keys(tmp_path):
    """Old SQLite data can reference deleted rows — nullable FKs get nulled,
    rows with broken required FKs get dropped."""
    src_url = f"sqlite+aiosqlite:///{tmp_path / 'src3.db'}"
    dst_url = f"sqlite+aiosqlite:///{tmp_path / 'dst3.db'}"

    src_engine = create_async_engine(src_url)
    async with src_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(src_engine, expire_on_commit=False)
    async with factory() as session:
        user = User(discord_user_id=222)
        session.add(user)
        await session.flush()
        job = Job(url="https://x/ok", title="Dev", company="A", source="indeed")
        session.add(job)
        await session.flush()
        session.add_all([
            # subscription_id=999 doesn't exist -> nulled (nullable FK)
            UserJob(user_id=user.id, job_id=job.id, subscription_id=999),
        ])
        await session.commit()
        # job_id=12345 doesn't exist -> row dropped (required FK).
        # Insert via raw SQL since SQLite won't enforce it here.
        from sqlalchemy import text
        await session.execute(text(
            f"INSERT INTO applications (user_id, job_id, status, source, applied_at, updated_at) "
            f"VALUES ({user.id}, 12345, 'applied', 'discord', '2026-01-01', '2026-01-01')"
        ))
        await session.commit()
    await src_engine.dispose()

    copied = await copy_all(src_url, dst_url)

    assert copied["user_jobs"] == 1
    assert copied["applications"] == 0  # broken row dropped

    dst_engine = create_async_engine(dst_url)
    dst_factory = async_sessionmaker(dst_engine, expire_on_commit=False)
    async with dst_factory() as session:
        uj = (await session.execute(select(UserJob))).scalar_one()
        assert uj.subscription_id is None  # broken ref nulled
    await dst_engine.dispose()


@pytest.mark.asyncio
async def test_copy_all_refuses_nonempty_destination(tmp_path):
    src_url = f"sqlite+aiosqlite:///{tmp_path / 'src2.db'}"
    dst_url = f"sqlite+aiosqlite:///{tmp_path / 'dst2.db'}"

    for url in (src_url, dst_url):
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(User(discord_user_id=1))
            await session.commit()
        await engine.dispose()

    with pytest.raises(RuntimeError, match="not empty"):
        await copy_all(src_url, dst_url)


def test_naive_sqlite_timestamps_are_marked_utc():
    """asyncpg reads a naive datetime bound to timestamptz as host-local time."""
    from datetime import datetime, timezone

    from scripts.migrate_sqlite_to_postgres import _as_utc, _tz_columns

    naive = datetime(2026, 3, 1, 9, 30)
    aware = datetime(2026, 3, 1, 9, 30, tzinfo=timezone.utc)
    row = _as_utc(
        {"id": 1, "first_seen_at": naive, "last_seen_at": aware, "posted_at": None},
        _tz_columns(Job.__table__),
    )
    assert row["first_seen_at"] == aware and row["first_seen_at"].tzinfo is timezone.utc
    assert row["last_seen_at"] is aware
    assert row["posted_at"] is None and row["id"] == 1


@pytest.mark.asyncio
async def test_copy_all_streams_in_batches(tmp_path, monkeypatch):
    """Batches smaller than the tables: every row arrives, and references
    to rows copied in an earlier batch still count as valid."""
    import scripts.migrate_sqlite_to_postgres as script

    monkeypatch.setattr(script, "BATCH_SIZE", 2)
    src_url = f"sqlite+aiosqlite:///{tmp_path / 'src4.db'}"
    dst_url = f"sqlite+aiosqlite:///{tmp_path / 'dst4.db'}"

    src_engine = create_async_engine(src_url)
    async with src_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(src_engine, expire_on_commit=False)
    async with factory() as session:
        user = User(discord_user_id=333)
        jobs = [
            Job(url=f"https://x/{i}", title="Dev", company="A", source="ejobs")
            for i in range(5)
        ]
        session.add_all([user, *jobs])
        await session.flush()
        session.add_all([UserJob(user_id=user.id, job_id=job.id) for job in jobs])
        session.add_all([Event(event_type="web.pageview", actor_id=user.id) for _ in range(7)])
        await session.commit()
    await src_engine.dispose()

    copied = await copy_all(src_url, dst_url)
    assert copied["jobs"] == 5
    assert copied["user_jobs"] == 5  # job ids from every batch were known
    assert copied["events"] == 7


# Opt-in, like tests/test_database_pg.py: an empty scratch database.
PG_URL = os.getenv("GOSHA_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not PG_URL, reason="GOSHA_TEST_POSTGRES_URL not set")
@pytest.mark.asyncio
async def test_copy_to_postgres_keeps_utc_on_a_host_off_utc(tmp_path, monkeypatch):
    """Run the copy as if on a host in UTC+9: timestamps must not move."""
    import time
    from datetime import datetime, timezone

    src_url = f"sqlite+aiosqlite:///{tmp_path / 'src5.db'}"
    written = datetime(2026, 3, 1, 9, 30, tzinfo=timezone.utc)

    src_engine = create_async_engine(src_url)
    async with src_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(src_engine, expire_on_commit=False)
    async with factory() as session:
        session.add(User(discord_user_id=444, created_at=written))
        await session.commit()
    await src_engine.dispose()

    dst_engine = create_async_engine(PG_URL)
    async with dst_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await dst_engine.dispose()

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        await copy_all(src_url, PG_URL)
    finally:
        monkeypatch.undo()
        time.tzset()

    dst_engine = create_async_engine(PG_URL)
    async with dst_engine.begin() as conn:
        created = (await conn.execute(select(User.created_at))).scalar_one()
        await conn.run_sync(Base.metadata.drop_all)
    await dst_engine.dispose()
    assert created == written
