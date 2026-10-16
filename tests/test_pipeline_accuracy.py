"""Given the scraped jobs, each user gets the right ones, once, best first.

Each test pins a failure the match or deliver stage had: what a user was
sent (or missed) before the fix.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

import gosha.pipeline as pipeline
from gosha.embeddings import EMBEDDING_DIM, vec_to_bytes
from gosha.matching import SemanticMatcher
from gosha.models import Job, Subscription, User, UserJob

NOW = datetime.now(timezone.utc)
ON_QUERY = np.zeros(EMBEDDING_DIM, dtype=np.float32)
ON_QUERY[0] = 1.0


class FakeMatcher:
    """Every posting scores 1.0: only the hard filters decide."""

    available = True
    threshold = 0.40
    cv_threshold = 0.40

    def encode_subscription(self, keywords, locations, experience_levels=None):
        return ON_QUERY

    is_match = SemanticMatcher.is_match


def make_sub(keyword: str, levels: list[str] | None = None, max_age_days: int = 7) -> Subscription:
    sub = Subscription(user_id=1, max_age_days=max_age_days)
    sub.keywords = [keyword]
    sub.locations = ["cluj"]
    sub.excluded_keywords = []
    sub.company_blacklist = []
    sub.experience_levels = levels or ["any"]
    return sub


def make_job(title: str, **kwargs) -> Job:
    return Job(
        url=f"https://jobs.test/{title.replace(' ', '-')}",
        title=title,
        company=kwargs.pop("company", "Acme"),
        location=kwargs.pop("location", "Cluj-Napoca, Romania"),
        source="ejobs",
        embedding=vec_to_bytes(ON_QUERY),
        **kwargs,
    )


def titles(matches) -> set[str]:
    return {job.title for job, _score in matches}


# ── Match stage ───────────────────────────────────────────────────────


async def test_an_intern_search_is_not_sent_another_searchs_senior_roles():
    """The jobs scored for a search include every other search's results.
    The semantic path ignored what the keyword implies, and embeddings do
    not see seniority: an intern search received senior Java roles a
    "java developer" search had scraped."""
    jobs = [
        make_job("Senior Java Developer"),
        make_job("Java Developer Intern"),
        make_job("Lead Backend Engineer"),
    ]
    matches = await pipeline.match_jobs_for_subscription(
        make_sub("software engineer intern"), jobs, FakeMatcher(),
    )
    assert titles(matches) == {"Java Developer Intern"}


async def test_an_unmarked_posting_reaches_a_junior_keyword_search():
    entry = "Manual testing of our web apps. 0-1 years of experience."
    jobs = [
        make_job("QA Engineer", description=entry),
        make_job("Senior QA Engineer", description=entry),
        make_job("QA Automation Engineer", description="Minimum 5 years of experience."),
    ]
    matches = await pipeline.match_jobs_for_subscription(
        make_sub("junior qa"), jobs, FakeMatcher(),
    )
    assert titles(matches) == {"QA Engineer"}


async def test_the_junior_level_reads_the_years_a_posting_asks_for():
    """The SPA's default levels are intern + junior; title-only matching
    passed only titles saying "junior", so most graduate postings were
    never sent. Unmarked titles now pass unless the posting asks for more
    than two years."""
    jobs = [
        make_job("Python Developer", description="Python, Django. 1 year of experience."),
        make_job("Backend Developer", description="Minimum 4 years of experience with Python."),
    ]
    matches = await pipeline.match_jobs_for_subscription(
        make_sub("python developer", ["intern", "junior"]), jobs, FakeMatcher(),
    )
    assert titles(matches) == {"Python Developer"}


@pytest.mark.parametrize("posted_days_ago, first_seen_days_ago, fresh", [
    (2, 0, True),
    (20, 0, False),     # first scraped today, posted three weeks ago
    (None, 20, False),  # no board date: judged on when we first saw it
    (None, 1, True),
    (7, 0, True),       # date-only boards: the whole 7th day counts
])
def test_freshness_follows_the_searchs_max_age(posted_days_ago, first_seen_days_ago, fresh):
    """A 30-day search's results were scored for a 3-day search too."""
    job = make_job("Python Developer")
    job.posted_at = (
        None if posted_days_ago is None else NOW - timedelta(days=posted_days_ago)
    )
    job.first_seen_at = NOW - timedelta(days=first_seen_days_ago)
    assert pipeline.is_fresh_enough(job, 7) is fresh
    assert pipeline.job_matches_subscription(job, make_sub("python developer")) is fresh


def test_freshness_handles_naive_datetimes_and_missing_dates():
    job = make_job("Python Developer")
    job.posted_at = (NOW - timedelta(days=1)).replace(tzinfo=None)  # as SQLite returns it
    assert pipeline.is_fresh_enough(job, 3)
    job.posted_at = None
    assert pipeline.is_fresh_enough(job, 3)  # nothing known: not held against it


def test_freshness_counts_days_in_the_postings_own_time_zone(monkeypatch):
    """A date-only board posting at local midnight (+03:00) is 21:00 the
    day before in UTC: the 7th day was a day short."""

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(pipeline, "datetime", _Clock)
    bucharest = timezone(timedelta(hours=3))
    job = make_job("Python Developer")
    job.posted_at = datetime(2026, 10, 9, 0, 0, tzinfo=bucharest)
    assert pipeline.is_fresh_enough(job, 7)
    job.posted_at = datetime(2026, 10, 8, 23, 30, tzinfo=bucharest)
    assert not pipeline.is_fresh_enough(job, 7)
    job.posted_at = datetime(2026, 10, 9, 0, 0)  # naive: UTC
    assert pipeline.is_fresh_enough(job, 7)


# ── Deliver stage ─────────────────────────────────────────────────────


class _Bot:
    alert_channel_id = 0

    def get_channel(self, _id):
        return None


async def _pending(session, specs, *, active: bool = True, discord_id: int = 7001):
    """One user, one search, a pending delivery per (title, company, score)."""
    user = User(discord_user_id=discord_id)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id, is_active=active)
    sub.keywords = ["python developer"]
    sub.locations = ["cluj"]
    session.add(sub)
    await session.flush()
    ids = []
    for i, (title, company, score) in enumerate(specs):
        job = Job(url=f"https://d.test/{discord_id}/{i}", title=title, company=company,
                  location="Cluj", source="ejobs")
        session.add(job)
        await session.flush()
        session.add(UserJob(user_id=user.id, job_id=job.id, subscription_id=sub.id,
                            relevance_score=score, dm_status=UserJob.DM_PENDING))
        ids.append(job.id)
    await session.commit()
    return sub.id, ids


async def _statuses(session) -> dict[int, str | None]:
    session.expire_all()
    rows = (await session.execute(select(UserJob))).scalars().all()
    return {uj.job_id: uj.dm_status for uj in rows}


def _record_dms(monkeypatch, fail_on: int | None = None) -> list[str]:
    sent: list[str] = []

    async def fake(bot, discord_id, embed, view=None):
        if fail_on is not None and len(sent) == fail_on:
            raise asyncio.CancelledError  # the bot shut down mid-batch
        sent.append(embed.title)
        return pipeline.DM_SENT

    monkeypatch.setattr(pipeline, "_dm_user", fake)
    return sent


async def test_of_two_board_copies_the_better_match_is_sent_first(
    patched_db, session, monkeypatch,
):
    """The same-title check ran in row order before sorting, so the older
    copy was kept whatever its score, and DMs went out oldest first."""
    _sub, ids = await _pending(session, [
        ("Python Developer", "Acme", 0.45),   # older copy, weaker
        ("Django Developer", "Beta", 0.50),
        ("Python Developer", "Acme", 0.60),   # the other board's copy
    ])
    sent = _record_dms(monkeypatch)

    assert await pipeline.deliver_pending(_Bot()) == 2
    assert ["Python" in sent[0], "Django" in sent[1]] == [True, True]
    statuses = await _statuses(session)
    assert statuses[ids[2]] == UserJob.DM_SENT
    assert statuses[ids[0]] == UserJob.DM_SKIPPED


async def test_regex_fallback_matches_do_not_outrank_scored_ones(
    patched_db, session, monkeypatch,
):
    """A cycle without the model stored every match as 1.0, above any
    cosine: its copy beat the better-scored one, and its DMs went first."""
    regex_matches = await pipeline.match_jobs_for_subscription(
        make_sub("python developer"), [make_job("Python Developer")],
    )
    assert [score for _job, score in regex_matches] == [None]

    _sub, ids = await _pending(session, [
        ("Python Developer", "Acme", None),   # matched in a regex cycle
        ("Django Developer", "Beta", 0.50),
        ("Python Developer", "Acme", 0.60),
    ])
    sent = _record_dms(monkeypatch)
    assert await pipeline.deliver_pending(_Bot()) == 2
    assert ["Python" in sent[0], "Django" in sent[1]] == [True, True]
    statuses = await _statuses(session)
    assert statuses[ids[2]] == UserJob.DM_SENT
    assert statuses[ids[0]] == UserJob.DM_SKIPPED


async def test_a_paused_search_sends_nothing_until_resumed(patched_db, session, monkeypatch):
    sub_id, ids = await _pending(session, [("Python Developer", "Acme", 0.5)], active=False)
    sent = _record_dms(monkeypatch)

    assert await pipeline.deliver_pending(_Bot()) == 0
    assert sent == []
    assert (await _statuses(session))[ids[0]] == UserJob.DM_PENDING

    sub = await session.get(Subscription, sub_id)
    sub.is_active = True
    await session.commit()
    assert await pipeline.deliver_pending(_Bot()) == 1


async def test_a_dm_is_recorded_the_moment_discord_accepts_it(
    patched_db, session, monkeypatch,
):
    """Statuses were written once per user: a shutdown after the first DM
    of a batch left it pending, and the restart sent it again."""
    _sub, ids = await _pending(session, [
        ("Python Developer", "Acme", 0.6), ("Django Developer", "Beta", 0.5),
    ])
    _record_dms(monkeypatch, fail_on=1)

    with pytest.raises(asyncio.CancelledError):
        await pipeline.deliver_pending(_Bot())

    statuses = await _statuses(session)
    assert statuses[ids[0]] == UserJob.DM_SENT
    assert statuses[ids[1]] == UserJob.DM_PENDING

    resent = _record_dms(monkeypatch)
    assert await pipeline.deliver_pending(_Bot()) == 1
    assert len(resent) == 1


# ── Scrape stage health accounting ────────────────────────────────────


class _NoTunnels:
    def active_proxies(self):
        return []


async def test_jobspy_boards_not_asked_are_not_counted_as_attempted(
    patched_db, session, monkeypatch,
):
    """JobSpy skips remote searches, so Indeed "returning nothing" for one
    was a false dead-source alert."""
    from gosha.scrape_health import ScrapeHealth

    user = User(discord_user_id=7101)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id, max_age_days=7)
    sub.keywords = ["python developer"]
    sub.locations = ["remote"]
    sub.boards = ["indeed", "linkedin"]
    session.add(sub)
    await session.commit()

    async def no_jobspy(*_a, **_kw):
        return pd.DataFrame()

    monkeypatch.setattr("gosha.scraper.scrape_jobs_raw", no_jobspy)
    monkeypatch.setattr("gosha.scrapers.registry.get_extra_scrapers", lambda: [])
    health = ScrapeHealth()
    await pipeline.run_scrape_stage(_NoTunnels(), health)

    assert "indeed" not in health.last_report.per_source
    assert "linkedin" not in health.last_report.per_source


async def test_the_scrape_stage_tells_health_which_searches_ran(
    patched_db, session, monkeypatch,
):
    """Deleting a big search used to read as a yield collapse; health
    re-learns its baselines when the set of searches changes."""
    from gosha.scrape_health import ScrapeHealth

    user = User(discord_user_id=7102)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id, max_age_days=7)
    sub.keywords = ["python developer"]
    sub.locations = ["cluj"]
    sub.boards = ["linkedin"]
    session.add(sub)
    await session.commit()

    async def no_jobspy(*_a, **_kw):
        return pd.DataFrame()

    monkeypatch.setattr("gosha.scraper.scrape_jobs_raw", no_jobspy)
    monkeypatch.setattr("gosha.scrapers.registry.get_extra_scrapers", lambda: [])
    health = ScrapeHealth()
    seen: list = []
    real = health.record_cycle

    def spy(*args, **kwargs):
        seen.append(kwargs.get("searches"))
        return real(*args, **kwargs)

    monkeypatch.setattr(health, "record_cycle", spy)
    await pipeline.run_scrape_stage(_NoTunnels(), health)

    # Per board: removing a search only Indeed served re-learns Indeed's
    # baseline, not every board's.
    assert seen == [{"linkedin": {("python developer", "cluj", ("linkedin",), 7)}}]


async def test_searches_are_reported_per_board_that_ran_them(patched_db, session, monkeypatch):
    from gosha.scrape_health import ScrapeHealth
    from gosha.scrapers.base import SearchQuery

    user = User(discord_user_id=7103)
    session.add(user)
    await session.flush()
    for location in ("cluj", "remote"):
        sub = Subscription(user_id=user.id, max_age_days=7)
        sub.keywords = ["python developer"]
        sub.locations = [location]
        sub.boards = ["indeed"]
        session.add(sub)
    await session.commit()

    class _RomaniaOnly:
        name = "ejobs"

        def applies_to(self, query: SearchQuery) -> bool:
            return query.location != "remote"

        async def search(self, query):
            return []

    async def no_jobspy(*_a, **_kw):
        return pd.DataFrame()

    monkeypatch.setattr("gosha.scraper.scrape_jobs_raw", no_jobspy)
    monkeypatch.setattr("gosha.scrapers.registry.get_extra_scrapers", lambda: [_RomaniaOnly()])
    health = ScrapeHealth()
    seen: list = []
    real = health.record_cycle
    monkeypatch.setattr(
        health, "record_cycle",
        lambda *a, **k: seen.append(k.get("searches")) or real(*a, **k),
    )
    await pipeline.run_scrape_stage(_NoTunnels(), health)

    cluj = ("python developer", "cluj", ("indeed",), 7)
    # JobSpy sends Indeed no remote search, and eJobs serves Romania only.
    assert seen == [{"indeed": {cluj}, "ejobs": {cluj}}]


# ── Match-stage cost: subscriptions × jobs × keywords, on the event loop ──


def test_place_and_age_are_checked_before_the_description_is_read(monkeypatch):
    read: list[str] = []
    for name in ("matches_experience_level", "matches_excluded_keywords"):
        real = getattr(pipeline, name)
        monkeypatch.setattr(
            pipeline, name, lambda *a, _real=real, _n=name, **k: read.append(_n) or _real(*a, **k),
        )
    sub = make_sub("java developer", ["junior"])
    sub.excluded_keywords = ["sales"]
    far = make_job("Java Developer", location="Berlin, Germany", description="2 years")
    stale = make_job("Java Developer II", posted_at=NOW - timedelta(days=30))

    assert not pipeline.job_matches_subscription(far, sub)
    assert not pipeline.job_matches_subscription(stale, sub)
    assert read == []
    assert pipeline.job_matches_subscription(make_job("Java Developer"), sub)
    assert set(read) == {"matches_experience_level", "matches_excluded_keywords"}


def test_a_description_is_read_for_its_years_once_per_cycle_not_per_search():
    from gosha.filters import required_years

    required_years.cache_clear()
    job = make_job(
        "Java Developer", description="Minimum 2 years of experience with Java. (cache test)",
    )
    subs = [make_sub(kw, ["junior"]) for kw in ("junior java", "java developer", "junior dev")]
    for sub in subs * 20:
        assert pipeline.job_matches_subscription(job, sub)
        assert pipeline._title_relevant_for_any_keyword(
            job.title, sub.keywords, None, job.description,
        )
    assert required_years.cache_info().misses == 1
