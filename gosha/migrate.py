"""Database migration from old flat schema to new GOSHA schema.

Handles:
  - subscriptions: keyword/location (single strings) → keywords/locations (JSON lists)
  - seen_jobs → user_jobs: preserve delivery history so users aren't re-notified
  - new tables: jobs, events created by create_all()
  - new columns on subscriptions: defaults applied for old rows

Run automatically on startup (idempotent — safe to run multiple times).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection

log = logging.getLogger(__name__)


async def run_migrations(conn: AsyncConnection) -> None:
    """Run all migrations inside an existing connection/transaction."""
    inspector = await conn.run_sync(lambda sync_conn: inspect(sync_conn))
    existing_tables = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())

    log.info("Existing tables: %s", existing_tables)

    # Dialect-aware type/literal fragments (SQLite for legacy DBs; Postgres
    # starts fresh via create_all but must not crash if this runs there).
    is_pg = conn.dialect.name == "postgresql"
    false_lit = "FALSE" if is_pg else "0"
    true_lit = "TRUE" if is_pg else "1"
    blob_type = "BYTEA" if is_pg else "BLOB"
    dt_type = "TIMESTAMP" if is_pg else "DATETIME"

    # 1. Add missing columns to existing tables
    if "users" in existing_tables:
        await _add_missing_columns(conn, "users", {
            "tier": "VARCHAR(32) NOT NULL DEFAULT 'free'",
            "username": "VARCHAR(128)",
            "avatar_url": "VARCHAR(512)",
            "created_at": dt_type,
            "last_login_at": dt_type,
            "in_guild": f"BOOLEAN NOT NULL DEFAULT {false_lit}",
            "cv_embedding": blob_type,
        })

    if "jobs" in existing_tables:
        await _add_missing_columns(conn, "jobs", {
            "posted_at": dt_type,
            "embedding": blob_type,
            "dedup_group_id": "INTEGER",
            "last_checked_at": dt_type,
            # Salary normalisation (gosha/salary.py). Backfilled by
            # gosha.services.jobs.backfill_salary_normalisation, which the
            # bot runs hourly, and recomputed on every upsert.
            "salary_period": "VARCHAR(16)",
            "salary_monthly_min_ron": "FLOAT",
            "salary_monthly_max_ron": "FLOAT",
        })

    if "applications" in existing_tables:
        await _add_missing_columns(conn, "applications", {
            "source": "VARCHAR(16) NOT NULL DEFAULT 'discord'",
        })

    if "subscriptions" in existing_tables:
        await _add_missing_columns(conn, "subscriptions", {
            "remote_ok": f"BOOLEAN NOT NULL DEFAULT {false_lit}",
            "name": "VARCHAR(128)",
            "notify_discord": f"BOOLEAN NOT NULL DEFAULT {true_lit}",
        })

    # 2. Migrate subscriptions if old schema detected
    if "subscriptions" in existing_tables:
        await _migrate_subscriptions(conn, inspector)

    # 3. Migrate seen_jobs → user_jobs
    if "seen_jobs" in existing_tables:
        await _migrate_seen_jobs(conn, existing_tables)

    log.info("Migration complete.")


async def _add_missing_columns(
    conn: AsyncConnection, table: str, columns: dict[str, str],
) -> None:
    """Add columns to a table if they don't already exist."""
    existing = await conn.run_sync(
        lambda sync_conn: [c["name"] for c in inspect(sync_conn).get_columns(table)]
    )
    for col_name, col_type in columns.items():
        if col_name not in existing:
            try:
                await conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"
                ))
                log.info("Added column %s.%s", table, col_name)
            except Exception as e:
                log.debug("Column %s.%s may already exist: %s", table, col_name, e)


async def _migrate_subscriptions(conn: AsyncConnection, inspector) -> None:
    """Migrate old subscription columns to new JSON-list format."""
    columns = await conn.run_sync(
        lambda sync_conn: [c["name"] for c in inspect(sync_conn).get_columns("subscriptions")]
    )

    has_old_keyword = "keyword" in columns
    has_new_keywords = "keywords" in columns

    # Case 1: Old schema only (keyword, location) — need full migration
    if has_old_keyword and not has_new_keywords:
        log.info("Detected old subscription schema — migrating...")

        # Add new columns
        new_cols = [
            ("keywords", "TEXT"),
            ("locations", "TEXT"),
            ("excluded_keywords", "TEXT DEFAULT '[]'"),
            ("company_blacklist", "TEXT DEFAULT '[]'"),
            ("boards", "TEXT DEFAULT '[\"indeed\",\"linkedin\",\"glassdoor\"]'"),
            ("experience_levels", "TEXT DEFAULT '[\"any\"]'"),
            ("remote_ok", "BOOLEAN DEFAULT 0"),
            ("salary_min", "INTEGER"),
            ("is_active", "BOOLEAN DEFAULT 1"),
        ]
        for col_name, col_type in new_cols:
            if col_name not in columns:
                try:
                    await conn.execute(text(
                        f"ALTER TABLE subscriptions ADD COLUMN {col_name} {col_type}"
                    ))
                    log.info("Added column: subscriptions.%s", col_name)
                except Exception as e:
                    # Column might already exist in some edge case
                    log.debug("Column %s may already exist: %s", col_name, e)

        # Copy data from old columns to new JSON format
        await conn.execute(text("""
            UPDATE subscriptions
            SET keywords = '["' || REPLACE(keyword, '"', '\\"') || '"]',
                locations = '["' || REPLACE(location, '"', '\\"') || '"]'
            WHERE keywords IS NULL AND keyword IS NOT NULL
        """))
        log.info("Migrated keyword/location → keywords/locations (JSON)")

        # Set defaults for new columns where NULL
        await conn.execute(text("""
            UPDATE subscriptions SET excluded_keywords = '[]' WHERE excluded_keywords IS NULL
        """))
        await conn.execute(text("""
            UPDATE subscriptions SET company_blacklist = '[]' WHERE company_blacklist IS NULL
        """))
        await conn.execute(text("""
            UPDATE subscriptions SET boards = '["indeed","linkedin","glassdoor"]' WHERE boards IS NULL
        """))
        await conn.execute(text("""
            UPDATE subscriptions SET experience_levels = '["any"]' WHERE experience_levels IS NULL
        """))
        await conn.execute(text("""
            UPDATE subscriptions SET is_active = 1 WHERE is_active IS NULL
        """))
        await conn.execute(text("""
            UPDATE subscriptions SET remote_ok = 0 WHERE remote_ok IS NULL
        """))
        log.info("Applied defaults to new subscription columns.")

    # Case 2: Both old and new columns exist (partially migrated)
    elif has_old_keyword and has_new_keywords:
        # Fill any remaining NULL keywords from old keyword column
        await conn.execute(text("""
            UPDATE subscriptions
            SET keywords = '["' || REPLACE(keyword, '"', '\\"') || '"]'
            WHERE keywords IS NULL AND keyword IS NOT NULL
        """))
        await conn.execute(text("""
            UPDATE subscriptions
            SET locations = '["' || REPLACE(location, '"', '\\"') || '"]'
            WHERE locations IS NULL AND location IS NOT NULL
        """))
        log.info("Backfilled any remaining NULL keywords/locations from old columns.")

        # Rebuild table so old keyword/location columns become nullable.
        # SQLite doesn't support ALTER COLUMN, so we recreate the table.
        # Check if the old columns are still NOT NULL before rebuilding.
        col_info = await conn.run_sync(
            lambda sync_conn: {
                c["name"]: c["nullable"]
                for c in inspect(sync_conn).get_columns("subscriptions")
            }
        )
        if col_info.get("keyword") is False or col_info.get("location") is False:
            await _make_old_columns_nullable(conn)

    # Case 3: New schema only — nothing to do
    elif has_new_keywords:
        log.info("Subscription schema is already new format.")


async def _migrate_seen_jobs(conn: AsyncConnection, existing_tables: list[str]) -> None:
    """Migrate seen_jobs records into user_jobs to preserve delivery history.

    Creates a stub Job record for each unique URL, then maps seen_jobs → user_jobs.
    This prevents users from being re-notified about jobs they already saw.
    """
    has_user_jobs = "user_jobs" in existing_tables
    has_jobs = "jobs" in existing_tables

    # Tables might not exist yet (create_all runs after migration)
    # We'll create them manually if needed
    if not has_jobs:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url VARCHAR(1024) UNIQUE NOT NULL,
                title VARCHAR(512) NOT NULL DEFAULT 'Unknown',
                company VARCHAR(256) NOT NULL DEFAULT 'Unknown',
                location VARCHAR(256) NOT NULL DEFAULT '',
                description TEXT,
                salary_min FLOAT,
                salary_max FLOAT,
                salary_currency VARCHAR(16),
                source VARCHAR(64) NOT NULL DEFAULT 'migrated',
                is_active BOOLEAN NOT NULL DEFAULT 1,
                first_seen_at DATETIME,
                last_seen_at DATETIME
            )
        """))
        log.info("Created jobs table for migration.")

    if not has_user_jobs:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                job_id INTEGER NOT NULL REFERENCES jobs(id),
                subscription_id INTEGER,
                relevance_score FLOAT,
                delivered_at DATETIME,
                feedback VARCHAR(32),
                feedback_at DATETIME,
                UNIQUE(user_id, job_id)
            )
        """))
        log.info("Created user_jobs table for migration.")

    # Check if migration already happened (seen_jobs might be empty or already processed)
    result = await conn.execute(text("SELECT COUNT(*) FROM seen_jobs"))
    seen_count = result.scalar()
    if seen_count == 0:
        log.info("seen_jobs is empty — nothing to migrate.")
        return

    # Check if we already migrated (user_jobs has data)
    result = await conn.execute(text("SELECT COUNT(*) FROM user_jobs"))
    uj_count = result.scalar()
    if uj_count > 0:
        log.info("user_jobs already has %d records — skipping seen_jobs migration.", uj_count)
        return

    log.info("Migrating %d seen_jobs records...", seen_count)
    now = datetime.now(timezone.utc).isoformat()

    # Step 1: Create stub Job records for each unique URL
    await conn.execute(
        text("""
            INSERT OR IGNORE INTO jobs (url, title, company, source, is_active, first_seen_at, last_seen_at)
            SELECT DISTINCT job_url, 'Unknown (migrated)', 'Unknown', 'migrated', 1, :now, :now
            FROM seen_jobs
            WHERE job_url IS NOT NULL AND job_url != ''
        """),
        {"now": now},
    )

    # Step 2: Map seen_jobs → user_jobs via the stub Job records
    await conn.execute(
        text("""
            INSERT OR IGNORE INTO user_jobs (user_id, job_id, delivered_at)
            SELECT sj.user_id, j.id, COALESCE(sj.seen_at, :now)
            FROM seen_jobs sj
            JOIN jobs j ON j.url = sj.job_url
        """),
        {"now": now},
    )

    result = await conn.execute(text("SELECT COUNT(*) FROM user_jobs"))
    migrated = result.scalar()
    log.info("Migrated %d seen_jobs → user_jobs records.", migrated)


async def _make_old_columns_nullable(conn: AsyncConnection) -> None:
    """Rebuild subscriptions table so old keyword/location columns are nullable.

    SQLite doesn't support ALTER COLUMN, so we recreate the table preserving data.
    This is needed because new subscriptions only populate the JSON 'keywords'/'locations'
    columns and leave the old singular columns empty.
    """
    log.info("Rebuilding subscriptions table to make old columns nullable...")

    # The rebuilt table must include every column the current model defines —
    # columns added earlier in run_migrations would otherwise be dropped here.
    old_columns = await conn.run_sync(
        lambda sync_conn: [c["name"] for c in inspect(sync_conn).get_columns("subscriptions")]
    )

    await conn.execute(text("PRAGMA foreign_keys=OFF"))
    await conn.execute(text("ALTER TABLE subscriptions RENAME TO _subscriptions_old"))
    await conn.execute(text("""
        CREATE TABLE subscriptions (
            id INTEGER NOT NULL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            keyword VARCHAR(256) DEFAULT '',
            location VARCHAR(256) DEFAULT '',
            max_age_days INTEGER NOT NULL DEFAULT 7,
            created_at DATETIME NOT NULL,
            keywords TEXT NOT NULL,
            locations TEXT NOT NULL,
            excluded_keywords TEXT NOT NULL DEFAULT '[]',
            company_blacklist TEXT NOT NULL DEFAULT '[]',
            boards TEXT NOT NULL DEFAULT '["indeed","linkedin","glassdoor"]',
            experience_levels TEXT NOT NULL DEFAULT '["any"]',
            remote_ok BOOLEAN NOT NULL DEFAULT 0,
            salary_min INTEGER,
            is_active BOOLEAN NOT NULL DEFAULT 1,
            name VARCHAR(128),
            notify_discord BOOLEAN NOT NULL DEFAULT 1
        )
    """))
    name_expr = "name" if "name" in old_columns else "NULL"
    notify_expr = (
        "COALESCE(notify_discord, 1)" if "notify_discord" in old_columns else "1"
    )
    await conn.execute(
        text(f"""
            INSERT INTO subscriptions
            SELECT id, user_id, keyword, location, max_age_days,
                   COALESCE(created_at, :now),
                   COALESCE(keywords, '["' || keyword || '"]'),
                   COALESCE(locations, '["' || location || '"]'),
                   COALESCE(excluded_keywords, '[]'),
                   COALESCE(company_blacklist, '[]'),
                   COALESCE(boards, '["indeed","linkedin","glassdoor"]'),
                   COALESCE(experience_levels, '["any"]'),
                   COALESCE(remote_ok, 0),
                   salary_min,
                   COALESCE(is_active, 1),
                   {name_expr},
                   {notify_expr}
            FROM _subscriptions_old
        """),
        {"now": datetime.now(timezone.utc).isoformat()},
    )
    await conn.execute(text("DROP TABLE _subscriptions_old"))
    await conn.execute(text("PRAGMA foreign_keys=ON"))

    log.info("Rebuilt subscriptions table — old columns are now nullable.")
