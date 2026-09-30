"""One-time data copy from the SQLite database to Postgres.

Usage:
    python scripts/migrate_sqlite_to_postgres.py \
        sqlite+aiosqlite:///data/jobs.db \
        postgresql+asyncpg://gosha:PASSWORD@localhost:5432/gosha

The destination schema is created from the current models; the destination
must be empty (no users) so a half-done copy can't silently merge. Primary
keys are preserved so foreign keys stay valid; Postgres sequences are reset
afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine

# Runnable as `python scripts/<name>.py` from anywhere: that puts scripts/
# on sys.path, not the repo root, so `import gosha` would fail.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gosha.events import Event  # noqa: E402
from gosha.models import (  # noqa: E402
    Application,
    Base,
    CoverLetter,
    Job,
    Outbox,
    Subscription,
    User,
    UserJob,
)

log = logging.getLogger(__name__)

# Copy order respects foreign-key dependencies.
TABLES = [
    User.__table__,
    Job.__table__,
    Subscription.__table__,
    UserJob.__table__,
    Application.__table__,
    CoverLetter.__table__,
    Event.__table__,
    Outbox.__table__,
]

BATCH_SIZE = 1000


async def copy_all(src_url: str, dst_url: str) -> dict[str, int]:
    """Copy every table from src to dst. Returns row counts per table."""
    src = create_async_engine(src_url)
    dst = create_async_engine(dst_url)
    copied: dict[str, int] = {}

    try:
        # Old installs predate the web-platform columns/tables — bring the
        # source up to the current schema first (idempotent).
        async with src.begin() as conn:
            from gosha.migrate import run_migrations

            await run_migrations(conn)
            await conn.run_sync(Base.metadata.create_all)

        async with dst.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            existing_users = (
                await conn.execute(select(func.count()).select_from(User.__table__))
            ).scalar()
            if existing_users:
                raise RuntimeError(
                    f"Destination database is not empty ({existing_users} users) — "
                    "refusing to copy into it."
                )

        # Old SQLite installs never enforced foreign keys — null out or drop
        # references to rows that no longer exist before Postgres rejects them.
        copied_ids: dict[str, set] = {}

        for table in TABLES:
            async with src.connect() as src_conn:
                rows = [dict(r) for r in (await src_conn.execute(select(table))).mappings().all()]

            rows, dropped = _sanitize_foreign_keys(table.name, rows, copied_ids)
            if dropped:
                log.warning("%s: dropped %d rows with broken references", table.name, dropped)

            async with dst.begin() as dst_conn:
                for start in range(0, len(rows), BATCH_SIZE):
                    batch = rows[start : start + BATCH_SIZE]
                    if batch:
                        await dst_conn.execute(insert(table), batch)

            copied_ids[table.name] = {r["id"] for r in rows if "id" in r}
            copied[table.name] = len(rows)
            log.info("Copied %5d rows -> %s", len(rows), table.name)

        # Postgres: bump sequences past the copied primary keys.
        if dst.dialect.name == "postgresql":
            async with dst.begin() as conn:
                for table in TABLES:
                    await conn.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM {table.name}), 1))"
                    ))
    finally:
        await src.dispose()
        await dst.dispose()

    return copied


# table -> (column, referenced table, nullable)
_FK_RULES: dict[str, list[tuple[str, str, bool]]] = {
    "subscriptions": [("user_id", "users", False)],
    "user_jobs": [
        ("user_id", "users", False),
        ("job_id", "jobs", False),
        ("subscription_id", "subscriptions", True),
    ],
    "applications": [("user_id", "users", False), ("job_id", "jobs", False)],
    "cover_letters": [("user_id", "users", False), ("job_id", "jobs", False)],
    "outbox": [("user_id", "users", False)],
}


def _sanitize_foreign_keys(
    table_name: str, rows: list[dict], copied_ids: dict[str, set],
) -> tuple[list[dict], int]:
    """Null out broken nullable FKs; drop rows whose required FKs are broken."""
    rules = _FK_RULES.get(table_name)
    if not rules:
        return rows, 0

    kept: list[dict] = []
    dropped = 0
    for row in rows:
        ok = True
        for column, ref_table, nullable in rules:
            value = row.get(column)
            if value is None or value in copied_ids.get(ref_table, set()):
                continue
            if nullable:
                row[column] = None
            else:
                ok = False
                break
        if ok:
            kept.append(row)
        else:
            dropped += 1
    return kept, dropped


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    counts = asyncio.run(copy_all(sys.argv[1], sys.argv[2]))
    total = sum(counts.values())
    print(f"Done — {total} rows copied across {len(counts)} tables.")


if __name__ == "__main__":
    main()
