"""The baseline catalog (SCRAPE_BASELINE): native boards searched for
student-level tech postings in the main cities every cycle, whatever the
subscriptions ask for, so the web feed has postings to rank for CV-only
users. See gosha-scrape's handoff, "Design: baseline catalog scrape"."""

from __future__ import annotations

import pandas as pd
import pytest

import gosha.pipeline as pipeline
from gosha.models import Subscription, User
from gosha.scrape_health import ScrapeHealth
from gosha.scraper import BASELINE_KEYWORD, BASELINE_LOCATIONS, BASELINE_MAX_AGE_DAYS


class _NoTunnels:
    def active_proxies(self):
        return []


class _Native:
    """A native source serving every location; records what it was asked."""

    name = "ejobs"

    def __init__(self) -> None:
        self.asked: list[tuple[str, str, int]] = []

    def applies_to(self, query) -> bool:
        return True

    async def search(self, query):
        self.asked.append((query.keyword, query.location, query.max_age_days))
        return []


@pytest.fixture
def boards(monkeypatch):
    jobspy: list[tuple] = []

    async def fake_jobspy(_tunnels, keyword, location, max_age_days, boards=None):
        jobspy.append((keyword, location, max_age_days, tuple(boards or ())))
        return pd.DataFrame()

    native = _Native()
    monkeypatch.setattr("gosha.scraper.scrape_jobs_raw", fake_jobspy)
    monkeypatch.setattr("gosha.scrapers.registry.get_extra_scrapers", lambda: [native])
    return jobspy, native


async def _search(session, keyword: str, location: str, days: int, discord_id: int = 9501):
    user = User(discord_user_id=discord_id)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id, max_age_days=days)
    sub.keywords = [keyword]
    sub.locations = [location]
    sub.boards = ["linkedin"]
    session.add(sub)
    await session.commit()


def _baseline_asks(native: _Native) -> list[str]:
    return [loc for kw, loc, days in native.asked
            if kw == BASELINE_KEYWORD and days == BASELINE_MAX_AGE_DAYS]


async def test_off_by_default_nothing_changes(patched_db, session, boards, monkeypatch):
    monkeypatch.delenv("SCRAPE_BASELINE", raising=False)
    jobspy, native = boards
    assert await pipeline.run_scrape_stage(_NoTunnels(), ScrapeHealth()) == []
    assert jobspy == [] and native.asked == []  # no subscriptions: no scraping


async def test_runs_without_subscriptions_on_native_sources_only(
    patched_db, session, boards, monkeypatch,
):
    monkeypatch.setenv("SCRAPE_BASELINE", "true")
    jobspy, native = boards
    health = ScrapeHealth()
    await pipeline.run_scrape_stage(_NoTunnels(), health)

    assert jobspy == []  # boards=() never reaches JobSpy
    assert sorted(set(_baseline_asks(native))) == sorted(BASELINE_LOCATIONS)
    assert health.last_report is not None  # a real cycle, for health


async def test_a_search_with_the_same_key_wins_and_order_keeps_places_together(
    patched_db, session, boards, monkeypatch,
):
    monkeypatch.setenv("SCRAPE_BASELINE", "true")
    jobspy, native = boards
    await _search(session, BASELINE_KEYWORD, "Cluj", BASELINE_MAX_AGE_DAYS)  # same key
    await _search(session, "python developer", "iasi", 7, discord_id=9502)

    await pipeline.run_scrape_stage(_NoTunnels(), ScrapeHealth())

    # The subscription's combo, not a second baseline one for Cluj.
    assert [loc for kw, loc, _d in native.asked if kw == BASELINE_KEYWORD].count("cluj") == 1
    # The baseline Iasi search runs right after the subscription's Iasi
    # one: the boards' list caches (10 min) then answer it.
    places = [loc for _kw, loc, _d in native.asked]
    i = places.index("iasi")
    assert native.asked[i][0] == "python developer"
    assert native.asked[i + 1] == (BASELINE_KEYWORD, "iasi", BASELINE_MAX_AGE_DAYS)
    # JobSpy only for the subscriptions.
    assert {(kw, loc) for kw, loc, *_ in jobspy} == {
        (BASELINE_KEYWORD, "cluj"), ("python developer", "iasi"),
    }


async def test_baseline_searches_count_for_the_native_sources_health(
    patched_db, session, boards, monkeypatch,
):
    monkeypatch.setenv("SCRAPE_BASELINE", "true")
    health = ScrapeHealth()
    seen: list = []
    real = health.record_cycle
    monkeypatch.setattr(
        health, "record_cycle", lambda *a, **k: seen.append(k.get("searches")) or real(*a, **k),
    )
    await pipeline.run_scrape_stage(_NoTunnels(), health)

    (by_source,) = seen
    assert set(by_source) == {"ejobs"}  # no JobSpy board attempted
    assert len(by_source["ejobs"]) == len(BASELINE_LOCATIONS)
