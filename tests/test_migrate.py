"""Tests for the database migration from old schema to new."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from gosha.models import Base


@pytest_asyncio.fixture
async def old_db():
    """Create a database with the OLD schema and seed it with test data."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    async with engine.begin() as conn:
        # Create OLD tables exactly as the original database.py defined them
        await conn.execute(text("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id BIGINT UNIQUE NOT NULL
            )
        """))
        await conn.execute(text("""
            CREATE TABLE subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                keyword VARCHAR(256) NOT NULL,
                location VARCHAR(256) NOT NULL,
                max_age_days INTEGER NOT NULL DEFAULT 7,
                created_at DATETIME
            )
        """))
        await conn.execute(text("""
            CREATE TABLE seen_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                job_url VARCHAR(1024) NOT NULL,
                seen_at DATETIME,
                UNIQUE(user_id, job_url)
            )
        """))

        # Seed old data
        await conn.execute(text(
            "INSERT INTO users (id, discord_user_id) VALUES (1, 123456789)"
        ))
        await conn.execute(text(
            "INSERT INTO users (id, discord_user_id) VALUES (2, 987654321)"
        ))
        # Old-style subscriptions (single keyword, single location)
        await conn.execute(text("""
            INSERT INTO subscriptions (id, user_id, keyword, location, max_age_days)
            VALUES (1, 1, 'software engineer', 'Cluj-Napoca, Romania', 7)
        """))
        await conn.execute(text("""
            INSERT INTO subscriptions (id, user_id, keyword, location, max_age_days)
            VALUES (2, 2, 'data scientist', 'Bucharest', 14)
        """))
        # Old seen_jobs
        await conn.execute(text("""
            INSERT INTO seen_jobs (user_id, job_url, seen_at)
            VALUES (1, 'https://indeed.com/job/aaa', '2025-01-01T00:00:00')
        """))
        await conn.execute(text("""
            INSERT INTO seen_jobs (user_id, job_url, seen_at)
            VALUES (1, 'https://linkedin.com/job/bbb', '2025-01-02T00:00:00')
        """))
        await conn.execute(text("""
            INSERT INTO seen_jobs (user_id, job_url, seen_at)
            VALUES (2, 'https://indeed.com/job/aaa', '2025-01-03T00:00:00')
        """))

    yield engine
    await engine.dispose()


@pytest.mark.asyncio
async def test_full_migration(old_db):
    """Run migration on an old DB and verify everything is preserved."""
    from gosha.migrate import run_migrations

    async with old_db.begin() as conn:
        await run_migrations(conn)
        # create_all to add new tables (jobs, user_jobs, events)
        await conn.run_sync(Base.metadata.create_all)

    # Verify subscriptions were migrated
    async with old_db.connect() as conn:
        result = await conn.execute(text("SELECT keywords, locations, is_active, boards, experience_levels FROM subscriptions WHERE id = 1"))
        row = result.one()
        assert row[0] == '["software engineer"]'
        assert row[1] == '["Cluj-Napoca, Romania"]'
        assert row[2] == 1  # is_active
        assert row[3] == '["indeed","linkedin","glassdoor"]'
        assert row[4] == '["any"]'

    # Verify second subscription
    async with old_db.connect() as conn:
        result = await conn.execute(text("SELECT keywords, locations FROM subscriptions WHERE id = 2"))
        row = result.one()
        assert row[0] == '["data scientist"]'
        assert row[1] == '["Bucharest"]'

    # Verify jobs were created from seen_jobs URLs
    async with old_db.connect() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM jobs"))
        job_count = result.scalar()
        assert job_count == 2  # Two unique URLs

    # Verify user_jobs were created from seen_jobs
    async with old_db.connect() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM user_jobs"))
        uj_count = result.scalar()
        assert uj_count == 3  # 3 seen_job records

    # Verify user 1 has 2 delivered jobs
    async with old_db.connect() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM user_jobs WHERE user_id = 1"))
        assert result.scalar() == 2

    # Verify user 2 has 1 delivered job
    async with old_db.connect() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM user_jobs WHERE user_id = 2"))
        assert result.scalar() == 1


@pytest.mark.asyncio
async def test_migration_idempotent(old_db):
    """Running migration twice should not duplicate data."""
    from gosha.migrate import run_migrations

    async with old_db.begin() as conn:
        await run_migrations(conn)
        await conn.run_sync(Base.metadata.create_all)

    # Run again
    async with old_db.begin() as conn:
        await run_migrations(conn)

    # Verify no duplicates
    async with old_db.connect() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM user_jobs"))
        assert result.scalar() == 3  # Same as before, not doubled


@pytest.mark.asyncio
async def test_migration_fresh_db():
    """Migration on a brand-new empty DB should not crash."""
    from gosha.migrate import run_migrations

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    async with engine.begin() as conn:
        # No tables exist yet — migration should handle gracefully
        await run_migrations(conn)
        await conn.run_sync(Base.metadata.create_all)

    # Verify tables exist
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM jobs"))
        assert result.scalar() == 0
        result = await conn.execute(text("SELECT COUNT(*) FROM user_jobs"))
        assert result.scalar() == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_json_list_fallback_for_old_string():
    """The _load_json_list helper must handle plain strings from old schema."""
    from gosha.models import _JSONListMixin

    # Old format: plain string (not JSON)
    assert _JSONListMixin._load_json_list("software engineer") == ["software engineer"]
    assert _JSONListMixin._load_json_list("Cluj-Napoca, Romania") == ["Cluj-Napoca, Romania"]

    # New format: JSON list
    assert _JSONListMixin._load_json_list('["software engineer"]') == ["software engineer"]
    assert _JSONListMixin._load_json_list('["a", "b"]') == ["a", "b"]

    # Edge cases
    assert _JSONListMixin._load_json_list("") == []
    assert _JSONListMixin._load_json_list(None) == []
    assert _JSONListMixin._load_json_list("[]") == []


@pytest.mark.asyncio
async def test_migration_adds_web_platform_columns(old_db):
    """A pre-web-platform DB gains the new user/job/subscription/application columns."""
    from sqlalchemy import inspect

    from gosha.migrate import run_migrations

    # Simulate a previous-version DB that already has jobs/applications
    # tables but none of the web-platform columns.
    async with old_db.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url VARCHAR(1024) UNIQUE NOT NULL,
                title VARCHAR(512) NOT NULL,
                company VARCHAR(256) NOT NULL DEFAULT 'Unknown',
                location VARCHAR(256) NOT NULL DEFAULT '',
                description TEXT,
                salary_min FLOAT, salary_max FLOAT, salary_currency VARCHAR(16),
                source VARCHAR(64) NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                first_seen_at DATETIME, last_seen_at DATETIME
            )
        """))
        await conn.execute(text("""
            CREATE TABLE applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                job_id INTEGER NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'applied',
                notes TEXT,
                applied_at DATETIME, updated_at DATETIME
            )
        """))
        await conn.execute(text(
            "INSERT INTO jobs (url, title, source) VALUES ('https://x/1', 'Dev', 'indeed')"
        ))
        await conn.execute(text(
            "INSERT INTO applications (user_id, job_id) VALUES (1, 1)"
        ))

    async with old_db.begin() as conn:
        await run_migrations(conn)
        await conn.run_sync(Base.metadata.create_all)

    async with old_db.connect() as conn:
        cols = {
            table: [
                c["name"]
                for c in (await conn.run_sync(
                    lambda sc, t=table: inspect(sc).get_columns(t)
                ))
            ]
            for table in ("users", "jobs", "subscriptions", "applications")
        }

    for col in ("username", "avatar_url", "in_guild", "cv_embedding", "cv_consent_at", "last_login_at"):
        assert col in cols["users"], f"users.{col} missing"
    for col in ("posted_at", "embedding"):
        assert col in cols["jobs"], f"jobs.{col} missing"
    for col in ("name", "notify_discord"):
        assert col in cols["subscriptions"], f"subscriptions.{col} missing"
    assert "source" in cols["applications"]

    # Existing rows got the defaults
    async with old_db.connect() as conn:
        result = await conn.execute(text("SELECT source FROM applications"))
        assert result.scalar() == "discord"
        result = await conn.execute(text("SELECT in_guild FROM users WHERE id = 1"))
        assert result.scalar() == 0

    # And the rebuild path keeps the new columns when it runs afterwards
    async with old_db.begin() as conn:
        await run_migrations(conn)
    async with old_db.connect() as conn:
        result = await conn.execute(text("SELECT notify_discord FROM subscriptions WHERE id = 1"))
        assert result.scalar() == 1
