"""Tests for the pipeline — job upsert, matching, delivery tracking."""

from __future__ import annotations

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gosha.models import Job, Subscription, User, UserJob

# These tests need to monkeypatch gosha.database so the pipeline functions
# use the test DB instead of the global one.


@pytest_asyncio.fixture
async def patched_db(engine, monkeypatch):
    """Patch gosha.database globals so pipeline functions use the test engine."""
    import gosha.database as db_mod

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "_engine", engine)
    monkeypatch.setattr(db_mod, "_session_factory", factory)
    yield factory


# ── upsert_jobs ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_new_jobs(patched_db):
    from gosha.pipeline import upsert_jobs

    df = pd.DataFrame([
        {
            "job_url": "https://indeed.com/job/aaa",
            "title": "Junior Dev",
            "company": "Acme",
            "location": "Cluj",
            "description": "Work here",
            "site": "indeed",
        },
        {
            "job_url": "https://linkedin.com/job/bbb",
            "title": "Backend Intern",
            "company": "StartupX",
            "location": "Bucharest",
            "description": "Build APIs",
            "site": "linkedin",
        },
    ])

    jobs = await upsert_jobs(df)
    assert len(jobs) == 2
    assert jobs[0].url == "https://indeed.com/job/aaa"
    assert jobs[1].company == "StartupX"


@pytest.mark.asyncio
async def test_upsert_deduplicates(patched_db):
    from gosha.pipeline import upsert_jobs

    df1 = pd.DataFrame([{
        "job_url": "https://indeed.com/job/dup",
        "title": "Dev",
        "company": "Acme",
        "location": "Cluj",
        "site": "indeed",
    }])
    df2 = pd.DataFrame([{
        "job_url": "https://indeed.com/job/dup",
        "title": "Dev Updated",
        "company": "Acme Corp",
        "location": "Cluj-Napoca",
        "site": "indeed",
    }])

    jobs1 = await upsert_jobs(df1)
    assert len(jobs1) == 1
    original_id = jobs1[0].id

    jobs2 = await upsert_jobs(df2)
    assert len(jobs2) == 1
    assert jobs2[0].id == original_id  # Same job, updated
    assert jobs2[0].title == "Dev Updated"


@pytest.mark.asyncio
async def test_upsert_skips_empty_urls(patched_db):
    from gosha.pipeline import upsert_jobs

    df = pd.DataFrame([
        {"job_url": "", "title": "No URL", "company": "X", "site": "indeed"},
        {"job_url": "nan", "title": "NaN URL", "company": "Y", "site": "indeed"},
    ])

    jobs = await upsert_jobs(df)
    assert len(jobs) == 0


@pytest.mark.asyncio
async def test_upsert_parses_salary(patched_db):
    from gosha.pipeline import upsert_jobs

    df = pd.DataFrame([{
        "job_url": "https://example.com/salary-job",
        "title": "Paid Dev",
        "company": "RichCo",
        "location": "London",
        "site": "indeed",
        "min_amount": 50000,
        "max_amount": 70000,
        "currency": "GBP",
    }])

    jobs = await upsert_jobs(df)
    assert len(jobs) == 1
    assert jobs[0].salary_min == 50000
    assert jobs[0].salary_max == 70000
    assert jobs[0].salary_currency == "GBP"


# ── job_matches_subscription ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_matches_basic(session: AsyncSession, sample_job, sample_subscription):
    from gosha.pipeline import job_matches_subscription

    assert job_matches_subscription(sample_job, sample_subscription) is True


@pytest.mark.asyncio
async def test_job_excluded_by_keyword(session: AsyncSession, sample_job):
    from gosha.pipeline import job_matches_subscription

    user = User(discord_user_id=987654)
    session.add(user)
    await session.flush()

    sub = Subscription(user_id=user.id)
    sub.keywords = ["software engineer"]
    sub.locations = ["Cluj"]
    sub.excluded_keywords = ["Junior"]  # sample_job title is "Junior Software Engineer"
    sub.company_blacklist = []
    sub.experience_levels = ["any"]
    session.add(sub)
    await session.commit()

    assert job_matches_subscription(sample_job, sub) is False


@pytest.mark.asyncio
async def test_job_excluded_by_company_blacklist(
    session: AsyncSession, sample_job
):
    from gosha.pipeline import job_matches_subscription

    user = User(discord_user_id=987655)
    session.add(user)
    await session.flush()

    sub = Subscription(user_id=user.id)
    sub.keywords = ["software engineer"]
    sub.locations = ["Cluj"]
    sub.excluded_keywords = []
    sub.company_blacklist = ["TechCorp"]  # sample_job company
    sub.experience_levels = ["any"]
    session.add(sub)
    await session.commit()

    assert job_matches_subscription(sample_job, sub) is False


@pytest.mark.asyncio
async def test_job_excluded_by_salary(session: AsyncSession, sample_job):
    from gosha.pipeline import job_matches_subscription

    user = User(discord_user_id=987656)
    session.add(user)
    await session.flush()

    sub = Subscription(user_id=user.id, salary_min=100000)
    sub.keywords = ["software engineer"]
    sub.locations = ["Cluj"]
    sub.excluded_keywords = []
    sub.company_blacklist = []
    sub.experience_levels = ["any"]
    session.add(sub)
    await session.commit()

    # sample_job normalises to ~22,917 RON/month, well under the 100,000
    # RON/month floor. The comparison is on the normalised figures, so the
    # raw "55000 EUR/year" is never mistaken for 55,000 of anything monthly.
    assert job_matches_subscription(sample_job, sub) is False


@pytest.mark.asyncio
async def test_job_location_mismatch(session: AsyncSession, sample_job):
    from gosha.pipeline import job_matches_subscription

    user = User(discord_user_id=987657)
    session.add(user)
    await session.flush()

    sub = Subscription(user_id=user.id)
    sub.keywords = ["software engineer"]
    sub.locations = ["Berlin"]  # sample_job is in Cluj
    sub.excluded_keywords = []
    sub.company_blacklist = []
    sub.experience_levels = ["any"]
    session.add(sub)
    await session.commit()

    assert job_matches_subscription(sample_job, sub) is False


# ── get_undelivered_jobs ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_undelivered_jobs(patched_db, session: AsyncSession):
    from gosha.pipeline import get_undelivered_jobs

    user = User(discord_user_id=111)
    session.add(user)
    await session.flush()

    j1 = Job(url="https://a.com/1", title="A", company="A", source="x")
    j2 = Job(url="https://a.com/2", title="B", company="B", source="x")
    j3 = Job(url="https://a.com/3", title="C", company="C", source="x")
    session.add_all([j1, j2, j3])
    await session.flush()

    # Mark j1 as already delivered
    uj = UserJob(user_id=user.id, job_id=j1.id)
    session.add(uj)
    await session.commit()

    undelivered = await get_undelivered_jobs(user.id, [j1.id, j2.id, j3.id])
    assert undelivered == {j2.id, j3.id}


@pytest.mark.asyncio
async def test_get_undelivered_empty(patched_db):
    from gosha.pipeline import get_undelivered_jobs

    result = await get_undelivered_jobs(999, [])
    assert result == set()


# ── record_delivery ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_delivery(patched_db, session: AsyncSession):
    from gosha.pipeline import record_delivery

    user = User(discord_user_id=222)
    session.add(user)
    await session.flush()

    job = Job(url="https://b.com/1", title="X", company="Y", source="z")
    session.add(job)
    await session.commit()

    uj = await record_delivery(user.id, job.id, relevance_score=0.75)
    assert uj.user_id == user.id
    assert uj.job_id == job.id
    assert uj.relevance_score == 0.75
    assert uj.delivered_at is not None
