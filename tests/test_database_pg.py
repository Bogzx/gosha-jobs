"""Startup schema work against a real Postgres (opt-in).

Set GOSHA_TEST_POSTGRES_URL to an empty scratch database, e.g.
postgresql+asyncpg://postgres:pw@127.0.0.1:5432/gosha_test — the test drops
and recreates every table in it. Skipped otherwise; SQLite cannot reproduce
Postgres's transactional DDL and lock behaviour.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import gosha.events  # noqa: F401 — registers Event with Base
from gosha.models import Base

PG_URL = os.getenv("GOSHA_TEST_POSTGRES_URL", "")
REPO = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(not PG_URL, reason="GOSHA_TEST_POSTGRES_URL not set")

# What production's schema lacks before this branch's first deploy.
_PRE_DEPLOY = [
    "ALTER TABLE users DROP COLUMN cv_consent_at",
    "ALTER TABLE users DROP COLUMN cv_embedding_model",
    "ALTER TABLE jobs DROP COLUMN embedding_model",
    "ALTER TABLE jobs DROP COLUMN salary_period",
    "DROP TABLE oauth_consumed_states",
    "DROP TABLE oauth_handoffs",
    "DROP TABLE rate_limit_hits",
]


async def _reset_to_pre_deploy_schema() -> None:
    engine = create_async_engine(PG_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        for stmt in _PRE_DEPLOY:
            await conn.execute(text(stmt))
    await engine.dispose()


async def _column_types() -> dict[str, str]:
    engine = create_async_engine(PG_URL)
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT table_name || '.' || column_name, data_type "
            "FROM information_schema.columns WHERE table_schema = 'public'"
        ))).all()
    await engine.dispose()
    return dict(rows)


def test_bot_and_api_booting_together_both_migrate_cleanly():
    """Both services run init_db at the same moment on a deploy; the loser
    used to crash with "current transaction is aborted"."""
    asyncio.run(_reset_to_pre_deploy_schema())

    script = (
        "import asyncio, sys; from gosha.database import init_db; "
        "asyncio.run(init_db(sys.argv[1]))"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, PG_URL], cwd=REPO,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(3)
    ]
    results = [(p.wait(timeout=60), p.stderr.read()) for p in procs]
    for code, err in results:
        assert code == 0, err[-2000:]

    types = asyncio.run(_column_types())
    assert types["users.cv_consent_at"] == "timestamp with time zone"
    assert types["users.cv_embedding_model"] == "character varying"
    assert types["jobs.embedding_model"] == "character varying"
    assert "rate_limit_hits.bucket" in types


# What production's schema lacked before DM tracking (and the rest of the
# 2026-10 schema changes).
_PRE_DM_TRACKING = [
    "ALTER TABLE user_jobs DROP COLUMN dm_status",
    "ALTER TABLE user_jobs DROP COLUMN dm_sent_at",
    "ALTER TABLE jobs DROP COLUMN experience_level",
    "DROP INDEX ix_jobs_active_first_seen",
    "DROP INDEX ix_cover_letters_user_job_created",
    "ALTER TABLE user_jobs DROP CONSTRAINT user_jobs_subscription_id_fkey",
    "ALTER TABLE user_jobs ADD CONSTRAINT user_jobs_subscription_id_fkey "
    "FOREIGN KEY (subscription_id) REFERENCES subscriptions (id)",
]


async def _pre_dm_tracking_with_a_delivery() -> None:
    engine = create_async_engine(PG_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        for stmt in _PRE_DM_TRACKING:
            await conn.execute(text(stmt))
        await conn.execute(text(
            "INSERT INTO users (id, discord_user_id, tier, in_guild) "
            "VALUES (1, 42, 'free', false)"
        ))
        await conn.execute(text(
            "INSERT INTO subscriptions (id, user_id, max_age_days, created_at, keywords, "
            "locations, excluded_keywords, company_blacklist, boards, experience_levels, "
            "remote_ok, is_active, notify_discord) VALUES (1, 1, 7, now(), '[\"x\"]', "
            "'[\"cluj\"]', '[]', '[]', '[]', '[\"any\"]', false, true, true)"
        ))
        await conn.execute(text(
            "INSERT INTO jobs (id, url, title, company, location, source, is_active, "
            "first_seen_at, last_seen_at) "
            "VALUES (1, 'https://j/1', 'Dev', 'X', 'Cluj', 'ejobs', true, now(), now())"
        ))
        await conn.execute(text(
            "INSERT INTO user_jobs (user_id, job_id, subscription_id, delivered_at) "
            "VALUES (1, 1, 1, now())"
        ))
    await engine.dispose()


def test_dm_tracking_migration_and_subscription_delete():
    asyncio.run(_pre_dm_tracking_with_a_delivery())

    async def constraint_oid() -> int:
        engine = create_async_engine(PG_URL)
        async with engine.connect() as conn:
            oid = (await conn.execute(text(
                "SELECT oid FROM pg_constraint "
                "WHERE conname = 'user_jobs_subscription_id_fkey'"
            ))).scalar_one()
        await engine.dispose()
        return oid

    async def boot_twice_then_delete() -> tuple:
        from gosha.database import init_db

        oids = []
        for _ in range(2):  # idempotent
            engine = await init_db(PG_URL)
            await engine.dispose()
            oids.append(await constraint_oid())
        # Rebuilt once, then left alone: re-creating it on every boot locks
        # user_jobs and re-validates the key with a full scan.
        assert oids[0] == oids[1]
        engine = create_async_engine(PG_URL)
        async with engine.begin() as conn:
            rule = (await conn.execute(text(
                "SELECT confdeltype FROM pg_constraint "
                "WHERE conname = 'user_jobs_subscription_id_fkey'"
            ))).scalar_one()
            status = (await conn.execute(text(
                "SELECT dm_status FROM user_jobs"
            ))).scalar_one()
            index = (await conn.execute(text(
                "SELECT count(*) FROM pg_indexes WHERE indexname IN "
                "('ix_jobs_active_first_seen', 'ix_cover_letters_user_job_created')"
            ))).scalar_one()
            # The delete that used to fail with a foreign-key violation.
            await conn.execute(text("DELETE FROM subscriptions WHERE id = 1"))
            orphan = (await conn.execute(text(
                "SELECT subscription_id FROM user_jobs"
            ))).scalar_one()
        await engine.dispose()
        return rule, status, index, orphan

    rule, status, index, orphan = asyncio.run(boot_twice_then_delete())
    assert rule in ("n", b"n")  # SET NULL (asyncpg returns "char" as bytes)
    assert status is None  # history is not a DM candidate, so never re-sent
    assert index == 2
    assert orphan is None
    types = asyncio.run(_column_types())
    assert types["user_jobs.dm_sent_at"] == "timestamp with time zone"
    assert types["jobs.experience_level"] == "character varying"
