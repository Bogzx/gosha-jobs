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
