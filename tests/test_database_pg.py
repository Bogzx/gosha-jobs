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
