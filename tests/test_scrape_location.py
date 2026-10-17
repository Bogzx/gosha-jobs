"""The scrape filter keeps only postings somewhere their searches take.

Boards answer a Cluj query with postings from anywhere (LinkedIn: India,
Paris, San Jose; Indeed: "Remote, USA"). The scrape filter checked titles
only, so 1,201 US rows and a 299-row burst (2026-10-16 16:56) were stored,
and upsert reactivated deactivated US rows on their next sighting. It now
applies search_location_ok, the check the match stage uses.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import select

import gosha.pipeline as pipeline
from gosha.filters import filter_dataframe
from gosha.models import Job, Subscription, User
from gosha.scrapers.base import RawJob


def _df(*locations):
    return pd.DataFrame([
        {"job_url": f"https://x.test/{i}", "title": "Python Developer", "location": loc}
        for i, loc in enumerate(locations)
    ])


def _kept(df):
    return list(df["location"])


def test_a_city_query_keeps_only_that_city():
    df = _df("Cluj-Napoca, Cluj, Romania", "San Jose, CA, USA", "India",
             "Paris, Île-de-France, France", "Remote, USA", "Remote", None, "")
    kept = filter_dataframe(df, "python developer", locations=["cluj"])
    assert _kept(kept) == ["Cluj-Napoca, Cluj, Romania"]


def test_remote_ok_adds_remote_postings_for_europe_only():
    df = _df("Cluj-Napoca, Romania", "Remote", "Remote, USA", "Romania",
             "Remote - Germany", "New York, NY, USA")
    kept = filter_dataframe(df, "python developer", locations=["cluj"], remote_ok=True)
    assert _kept(kept) == ["Cluj-Napoca, Romania", "Remote", "Romania", "Remote - Germany"]


def test_a_remote_query_keeps_remote_postings_we_serve():
    df = _df("Remote", "Worldwide", "Remote, USA", "Remote (USA)", "Bucharest, Romania")
    kept = filter_dataframe(df, "python developer", locations=["remote"])
    assert _kept(kept) == ["Remote", "Worldwide"]


def test_without_locations_the_filter_is_unchanged():
    df = _df("San Jose, CA, USA", None)
    assert len(filter_dataframe(df, "python developer")) == 2


class _Tunnels:
    def active_proxies(self):
        return []


class _Board:
    name = "ejobs"

    def __init__(self, locations):
        self.locations = locations

    async def search(self, query):
        return [
            RawJob(url=f"https://board.test/{i}", title="Python Developer", company="Acme",
                   location=loc, source="ejobs", posted_at=datetime.now(timezone.utc))
            for i, loc in enumerate(self.locations)
        ]


async def _search(session, location: str, remote_ok: bool = False):
    user = User(discord_user_id=8008)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id, max_age_days=7, remote_ok=remote_ok)
    sub.keywords = ["python developer"]
    sub.locations = [location]
    session.add(sub)
    await session.commit()


def _boards(monkeypatch, board):
    async def no_jobspy(tunnel_manager, keyword, location, max_age_days, boards=None):
        return pd.DataFrame()

    monkeypatch.setattr("gosha.scraper.scrape_jobs_raw", no_jobspy)
    monkeypatch.setattr("gosha.scrapers.registry.get_extra_scrapers", lambda: [board])


async def _stored(session) -> dict[str, bool]:
    session.expire_all()
    return {j.location: j.is_active for j in (await session.execute(select(Job))).scalars()}


@pytest.mark.asyncio
async def test_the_scrape_stage_stores_only_postings_its_searches_take(
    patched_db, session, monkeypatch,
):
    await _search(session, "cluj")
    _boards(monkeypatch, _Board(["Cluj-Napoca, Romania", "New York, NY, USA", "Remote"]))
    jobs = await pipeline.run_scrape_stage(_Tunnels())
    assert [j.location for j in jobs] == ["Cluj-Napoca, Romania"]
    assert await _stored(session) == {"Cluj-Napoca, Romania": True}


@pytest.mark.asyncio
async def test_a_remote_ok_search_also_stores_remote_postings(patched_db, session, monkeypatch):
    await _search(session, "cluj", remote_ok=True)
    _boards(monkeypatch, _Board(["Cluj-Napoca, Romania", "Remote", "Remote, USA"]))
    await pipeline.run_scrape_stage(_Tunnels())
    assert await _stored(session) == {"Cluj-Napoca, Romania": True, "Remote": True}


@pytest.mark.asyncio
async def test_a_deactivated_foreign_row_is_not_revived(patched_db, session, monkeypatch):
    await _search(session, "cluj")
    session.add(Job(url="https://board.test/0", title="Python Developer", company="Acme",
                    location="New York, NY, USA", source="ejobs", is_active=False))
    await session.commit()
    _boards(monkeypatch, _Board(["New York, NY, USA"]))
    await pipeline.run_scrape_stage(_Tunnels())
    assert await _stored(session) == {"New York, NY, USA": False}
