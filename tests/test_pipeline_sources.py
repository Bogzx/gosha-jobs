"""Tests for extra-source integration in the scrape stage + posted_at."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import select

import gosha.pipeline as pipeline
from gosha.models import Job, Subscription, User
from gosha.scrapers.base import RawJob


@pytest.mark.asyncio
async def test_upsert_jobs_sets_posted_at(patched_db, session):
    posted = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    df = pd.DataFrame([{
        "job_url": "https://p.com/1",
        "title": "Python Dev",
        "company": "Acme",
        "location": "Cluj-Napoca, Romania",
        "description": "desc",
        "site": "ejobs",
        "min_amount": 5000.0,
        "max_amount": 9000.0,
        "currency": "RON",
        "date_posted": posted,
    }])

    jobs = await pipeline.upsert_jobs(df)
    assert len(jobs) == 1

    stored = (
        await session.execute(select(Job).where(Job.url == "https://p.com/1"))
    ).scalar_one()
    assert stored.posted_at is not None
    assert stored.posted_at.replace(tzinfo=timezone.utc) == posted
    assert stored.source == "ejobs"


@pytest.mark.asyncio
async def test_scrape_stage_includes_extra_sources(patched_db, session, monkeypatch):
    user = User(discord_user_id=5005)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id, max_age_days=7)
    sub.keywords = ["python developer"]
    sub.locations = ["cluj"]
    session.add(sub)
    await session.commit()

    # JobSpy path returns nothing this cycle
    async def fake_jobspy(tunnel_manager, keyword, location, max_age_days, boards=None):
        return pd.DataFrame()

    monkeypatch.setattr("gosha.scraper.scrape_jobs_raw", fake_jobspy)

    class FakeScraper:
        name = "ejobs"

        def __init__(self):
            self.calls: list = []

        async def search(self, query):
            self.calls.append(query)
            return [RawJob(
                url="https://fake.ejobs.ro/python-dev/1",
                title="Python Developer",
                company="FakeCorp",
                location="Cluj-Napoca, Romania",
                source="ejobs",
                posted_at=datetime.now(timezone.utc),
            )]

    fake = FakeScraper()
    monkeypatch.setattr(
        "gosha.scrapers.registry.get_extra_scrapers", lambda: [fake]
    )

    class DummyTunnels:
        def active_proxies(self):
            return []

    jobs = await pipeline.run_scrape_stage(DummyTunnels())

    assert fake.calls, "extra scraper should be invoked"
    urls = {j.url for j in jobs}
    assert "https://fake.ejobs.ro/python-dev/1" in urls

    stored = (
        await session.execute(select(Job).where(Job.source == "ejobs"))
    ).scalars().all()
    assert len(stored) == 1


@pytest.mark.asyncio
async def test_scrape_stage_survives_broken_extra_scraper(patched_db, session, monkeypatch):
    user = User(discord_user_id=5006)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id, max_age_days=7)
    sub.keywords = ["qa"]
    sub.locations = ["cluj"]
    session.add(sub)
    await session.commit()

    async def fake_jobspy(tunnel_manager, keyword, location, max_age_days, boards=None):
        return pd.DataFrame([{
            "job_url": "https://indeed.com/ok",
            "title": "QA Engineer",
            "company": "Fine Inc",
            "location": "Cluj-Napoca, Romania",
            "site": "indeed",
        }])

    monkeypatch.setattr("gosha.scraper.scrape_jobs_raw", fake_jobspy)

    class ExplodingScraper:
        name = "bestjobs"

        async def search(self, query):
            raise RuntimeError("kaboom")  # adapters shouldn't raise, but if one does...

    monkeypatch.setattr(
        "gosha.scrapers.registry.get_extra_scrapers", lambda: [ExplodingScraper()]
    )

    class DummyTunnels:
        def active_proxies(self):
            return []

    jobs = await pipeline.run_scrape_stage(DummyTunnels())
    assert {j.url for j in jobs} == {"https://indeed.com/ok"}  # cycle survived


@pytest.mark.asyncio
async def test_upsert_batches_the_existence_lookup(patched_db, session, monkeypatch):
    """upsert_jobs used to run one SELECT per scraped row inside the loop.

    A broad keyword expands into ~19 searches across 7 sources, so that was
    hundreds of round trips per cycle against Postgres in another container.
    """
    from sqlalchemy import Select

    rows = [
        {
            "job_url": f"https://batch.com/{i}",
            "title": f"Engineer {i}",
            "company": "Acme",
            "location": "Cluj-Napoca, Romania",
            "site": "ejobs",
        }
        for i in range(25)
    ]

    selects: list[str] = []

    import gosha.database as db

    class CountingSession:
        def __init__(self, inner):
            self._inner = inner

        async def execute(self, statement, *a, **kw):
            if isinstance(statement, Select):
                selects.append(str(statement))
            return await self._inner.execute(statement, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    class CountingCtx:
        def __init__(self, ctx):
            self._ctx = ctx

        async def __aenter__(self):
            return CountingSession(await self._ctx.__aenter__())

        async def __aexit__(self, *exc):
            return await self._ctx.__aexit__(*exc)

    monkeypatch.setattr(
        pipeline, "get_session", lambda: CountingCtx(db.get_session())
    )

    jobs = await pipeline.upsert_jobs(pd.DataFrame(rows))

    assert len(jobs) == 25
    job_selects = [s for s in selects if "FROM jobs" in s]
    assert len(job_selects) == 1, f"expected one batched SELECT, got {len(job_selects)}"


@pytest.mark.asyncio
async def test_upsert_handles_the_same_url_twice_in_one_batch(patched_db, session):
    """Two keywords can surface the same posting; the batched lookup must
    not turn that into a duplicate insert."""
    rows = [
        {
            "job_url": "https://dupe.com/1",
            "title": "Engineer",
            "company": "Acme",
            "location": "Cluj",
            "site": "ejobs",
        }
    ] * 2

    jobs = await pipeline.upsert_jobs(pd.DataFrame(rows))
    assert len({id(j) for j in jobs}) == 1

    stored = (
        await session.execute(select(Job).where(Job.url == "https://dupe.com/1"))
    ).scalars().all()
    assert len(stored) == 1


async def _one_sub(session, discord_id: int, keyword: str, location: str = "cluj") -> None:
    user = User(discord_user_id=discord_id)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id, max_age_days=7)
    sub.keywords = [keyword]
    sub.locations = [location]
    sub.boards = ["indeed"]
    session.add(sub)
    await session.commit()


class _NoTunnels:
    def active_proxies(self):
        return []


@pytest.mark.asyncio
async def test_inapplicable_sources_are_not_attempted(patched_db, session, monkeypatch):
    """RemoteOK for a Cluj search is not a broken RemoteOK."""
    from gosha.scrape_health import ScrapeHealth

    await _one_sub(session, 5101, "python developer")

    async def no_jobspy(*_a, **_kw):
        return pd.DataFrame()

    monkeypatch.setattr("gosha.scraper.scrape_jobs_raw", no_jobspy)

    class RemoteOnly:
        name = "remoteok"
        calls = 0

        def applies_to(self, query):
            return query.location == "remote"

        async def search(self, query):
            RemoteOnly.calls += 1
            return []

    monkeypatch.setattr("gosha.scrapers.registry.get_extra_scrapers", lambda: [RemoteOnly()])
    health = ScrapeHealth()
    await pipeline.run_scrape_stage(_NoTunnels(), health)

    assert RemoteOnly.calls == 0
    assert "remoteok" not in health.last_report.per_source


@pytest.mark.asyncio
async def test_health_counts_what_boards_returned_not_what_was_kept(
    patched_db, session, monkeypatch,
):
    from gosha.scrape_health import ScrapeHealth

    await _one_sub(session, 5102, "computer science internship")

    async def jobspy(*_a, **_kw):
        return pd.DataFrame([
            {"job_url": f"https://in.test/{i}", "title": "Senior Accountant", "site": "indeed"}
            for i in range(4)
        ])

    monkeypatch.setattr("gosha.scraper.scrape_jobs_raw", jobspy)

    seen_relevance: list[bool] = []

    class Board:
        name = "ejobs"

        def applies_to(self, query):
            return True

        async def search(self, query):
            seen_relevance.append(query.is_relevant("Agent Vanzari"))
            return [
                RawJob(url="https://e.test/1", title="QA Engineer", source="ejobs",
                       location="Cluj-Napoca, Romania", experience_level="entry"),
                RawJob(url="https://e.test/2", title="Agent Vanzari", source="ejobs",
                       location="Cluj-Napoca, Romania"),
            ]

    monkeypatch.setattr("gosha.scrapers.registry.get_extra_scrapers", lambda: [Board()])
    health = ScrapeHealth()
    jobs = await pipeline.run_scrape_stage(_NoTunnels(), health)

    report = health.last_report
    # Two unique eJobs postings (every expanded term returned the same two),
    # four Indeed ones; only the entry-level QA posting survives the filter.
    assert report.per_source == {"ejobs": 2, "indeed": 4}
    assert report.kept == {"ejobs": 1, "indeed": 0}
    assert {j.url for j in jobs} == {"https://e.test/1"}
    assert not report.dead_sources
    # The subscription's relevance filter reached the adapter.
    assert seen_relevance and not any(seen_relevance)


@pytest.mark.asyncio
async def test_upsert_fits_values_to_column_widths(patched_db, session):
    many_cities = ", ".join(f"City{i}" for i in range(60))  # ~450 chars
    df = pd.DataFrame([
        {"job_url": "https://w.test/1", "title": "T" * 600, "company": "C" * 300,
         "location": many_cities, "site": "bestjobs"},
        {"job_url": "https://w.test/" + "x" * 1100, "title": "Too long a url",
         "site": "bestjobs"},
    ])
    jobs = await pipeline.upsert_jobs(df)

    (job,) = jobs
    assert len(job.title) <= 512 and len(job.company) <= 256
    assert len(job.location) <= 256
    assert job.location.startswith("City0, City1") and not job.location.endswith(",")


@pytest.mark.asyncio
async def test_a_failing_batch_does_not_end_the_cycle(patched_db, session, monkeypatch):
    await _one_sub(session, 5103, "python developer")

    async def jobspy(*_a, **_kw):
        return pd.DataFrame([{"job_url": "https://in.test/ok", "title": "Python Developer",
                              "site": "indeed", "location": "Cluj-Napoca, Romania"}])

    monkeypatch.setattr("gosha.scraper.scrape_jobs_raw", jobspy)

    class Board:
        name = "ejobs"

        def applies_to(self, query):
            return True

        async def search(self, query):
            return [RawJob(url="https://e.test/bad", title="Python Developer", source="ejobs",
                           location="Cluj-Napoca, Romania")]

    monkeypatch.setattr("gosha.scrapers.registry.get_extra_scrapers", lambda: [Board()])
    real_upsert = pipeline.upsert_jobs

    async def flaky_upsert(df):
        if (df["site"] == "ejobs").any():
            raise RuntimeError("value too long for type character varying(256)")
        return await real_upsert(df)

    monkeypatch.setattr(pipeline, "upsert_jobs", flaky_upsert)
    jobs = await pipeline.run_scrape_stage(_NoTunnels())
    assert {j.url for j in jobs} == {"https://in.test/ok"}
