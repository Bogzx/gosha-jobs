"""The detail-page caches survive a restart (scrapers/page_store.py).

Prod, 2026-10-16: the deploys at 17:11 and 18:05 emptied the in-memory
caches, so each following cycle was a cold start: most BestJobs/Hipo pages
over the per-search caps went unread (36 of 40 new BestJobs rows stored
without a description) and LinkedIn got every job page again.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from gosha.scrapers import details, page_store
from gosha.scrapers.page_store import PageStore

PAGE = "https://board.test/job/1"


def _restart(path) -> None:
    """What a deploy does to the process: memory gone, the file kept."""
    details._cache.clear()
    details._budget.clear()
    page_store.use(PageStore(path))


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "scrape_cache.sqlite3"
    page_store.use(PageStore(p))
    details._cache.clear()
    details._budget.clear()
    return p


def extract(text: str) -> dict:
    return {"description": text, "posted_at": datetime(2026, 10, 16, tzinfo=timezone.utc)}


@respx.mock
async def test_a_page_read_before_a_restart_is_not_fetched_again(path):
    route = respx.get(PAGE).mock(return_value=httpx.Response(200, text="Build things"))
    async with httpx.AsyncClient() as client:
        first = await details.fetch_details(client, [PAGE], extract, limit=5)
        _restart(path)
        again = await details.fetch_details(client, [PAGE], extract, limit=5)
    assert route.call_count == 1
    assert again == first  # the date comes back as a datetime


@respx.mock
async def test_the_ttl_counts_across_a_restart(path, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(page_store, "now", lambda: clock[0])
    route = respx.get(PAGE).mock(return_value=httpx.Response(200, text="x"))
    async with httpx.AsyncClient() as client:
        await details.fetch_details(client, [PAGE], extract, limit=5)
        _restart(path)
        clock[0] += details.DETAIL_TTL_SECONDS + 1
        await details.fetch_details(client, [PAGE], extract, limit=5)
    assert route.call_count == 2


@respx.mock
async def test_a_restart_does_not_hand_out_a_second_hourly_budget(path, monkeypatch):
    monkeypatch.setattr(details, "HOST_BUDGET_PER_HOUR", 2)
    route = respx.get(url__regex=r"https://board\.test/job/\d").mock(
        return_value=httpx.Response(200, text="x"),
    )
    async with httpx.AsyncClient() as client:
        await details.fetch_details(client, [f"https://board.test/job/{i}" for i in (1, 2)],
                                    extract, limit=5)
        _restart(path)
        deferred: set[str] = set()
        await details.fetch_details(client, ["https://board.test/job/3"], extract, 5, deferred)
    assert route.call_count == 2
    assert deferred == {"https://board.test/job/3"}


def test_only_jobspy_objects_are_rebuilt(path):
    """A tampered or foreign entry is a miss: nothing is imported for it."""
    import json
    import sqlite3

    store = page_store.store()
    store.put("detail", "ok", {"a": 1}, 1.0)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO pages VALUES ('detail', 'bad', 1.0, ?)",
            (json.dumps({"x": {"$enum": "os:PathLike", "name": "x"}}),),
        )
    assert store.get("detail", "bad") is None
    assert store.get("detail", "ok") == (1.0, {"a": 1})
    assert store.enabled


def test_an_unusable_file_turns_the_store_off_without_raising(tmp_path):
    store = PageStore(tmp_path)  # a directory: sqlite cannot open it
    store.put("detail", "k", {"a": 1}, 1.0)
    assert store.get("detail", "k") is None
    assert not store.enabled


def test_a_value_it_cannot_store_is_skipped(path):
    store = page_store.store()
    store.put("detail", "k", {"thing": object()}, 1.0)
    assert store.get("detail", "k") is None and store.enabled


def test_prune_keeps_the_newest_fresh_entries(path):
    store = page_store.store()
    for i in range(5):
        store.put("detail", str(i), {"i": i}, float(i))
    store.prune("detail", older_than=1.0, keep=2)
    assert [store.get("detail", str(i)) is not None for i in range(5)] == [
        False, False, False, True, True,
    ]


def test_where_the_file_lives(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRAPE_CACHE_PATH", "off")
    assert page_store.default_path() is None
    monkeypatch.setenv("SCRAPE_CACHE_PATH", str(tmp_path / "c.sqlite3"))
    assert page_store.default_path() == tmp_path / "c.sqlite3"
    monkeypatch.delenv("SCRAPE_CACHE_PATH")
    monkeypatch.setattr(page_store, "DATA_DIR", tmp_path / "absent")
    assert page_store.default_path() is None  # a checkout or CI: memory only
    monkeypatch.setattr(page_store, "DATA_DIR", tmp_path)
    assert page_store.default_path() == tmp_path / page_store.FILE_NAME  # the prod volume


@respx.mock
async def test_a_changed_parser_reads_the_pages_again(path, monkeypatch):
    """Persisted pages must not outlive the parser that read them: a deploy
    fixing Hipo's JSON-LD parse would otherwise serve the empty results it
    cached for up to a day."""
    route = respx.get(PAGE).mock(return_value=httpx.Response(200, text="x"))
    async with httpx.AsyncClient() as client:
        await details.fetch_details(client, [PAGE], extract, limit=5)
        _restart(path)
        monkeypatch.setattr(page_store, "_versions", {})
        monkeypatch.setattr(page_store, "code_version", lambda *modules: "fixed-parser")
        await details.fetch_details(client, [PAGE], extract, limit=5)
    assert route.call_count == 2


def test_the_code_version_follows_the_source(tmp_path, monkeypatch):
    import sys
    import types

    module = types.ModuleType("gosha_fake_parser")
    module.__file__ = str(tmp_path / "parser.py")
    (tmp_path / "parser.py").write_text("v1")
    monkeypatch.setitem(sys.modules, "gosha_fake_parser", module)
    monkeypatch.setattr(page_store, "_versions", {})
    v1 = page_store.code_version("gosha_fake_parser")
    (tmp_path / "parser.py").write_text("v2")
    monkeypatch.setattr(page_store, "_versions", {})
    assert page_store.code_version("gosha_fake_parser") != v1
