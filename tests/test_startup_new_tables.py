"""Production gets apply_clicks and search_nudges at startup, no ALTER needed.

init_db (gosha/database.py) runs migrations and then Base.metadata.create_all
on every boot of the bot and the api; create_all creates tables that do not
exist yet. This boots init_db on a database that has the rest of the schema
(as prod does before this deploy) and checks both tables appear, twice.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

import gosha.database as db_mod
import gosha.events  # noqa: F401 — registers Event with Base
from gosha.models import Base

NEW_TABLES = {"apply_clicks", "search_nudges"}


async def _tables(url: str) -> set[str]:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        names = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    await engine.dispose()
    return names


@pytest.mark.asyncio
async def test_init_db_creates_the_new_tables_on_an_existing_database(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'prod-like.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table in NEW_TABLES:
            await conn.execute(text(f"DROP TABLE {table}"))
    await engine.dispose()
    assert not (NEW_TABLES & await _tables(url))

    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)
    for _ in range(2):  # every boot runs it; the second must be a no-op
        started = await db_mod.init_db(url)
        await started.dispose()
    assert NEW_TABLES <= await _tables(url)
