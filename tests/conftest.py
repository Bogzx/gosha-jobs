"""Shared fixtures for the GOSHA test suite."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from gosha.models import Base, Job, Subscription, User

# Use in-memory SQLite for all tests — fast and isolated.
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    """Create a single event loop for the entire test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def engine():
    """Create a fresh in-memory database engine per test."""
    eng = create_async_engine(TEST_DB_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine):
    """Provide an async session bound to the test engine."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess


@pytest_asyncio.fixture
async def patched_db(engine, monkeypatch):
    """Patch gosha.database globals so module-level get_session() uses the test engine."""
    import gosha.database as db_mod

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "_engine", engine)
    monkeypatch.setattr(db_mod, "_session_factory", factory)
    yield factory


@pytest_asyncio.fixture
async def sample_user(session: AsyncSession) -> User:
    """Create a sample user."""
    user = User(discord_user_id=123456789)
    session.add(user)
    await session.commit()
    return user


@pytest_asyncio.fixture
async def sample_job(session: AsyncSession) -> Job:
    """Create a sample job."""
    job = Job(
        url="https://example.com/job/1",
        title="Junior Software Engineer",
        company="TechCorp",
        location="Cluj-Napoca, Romania",
        description="Build cool stuff with Python and React.",
        source="indeed",
        salary_min=40000,
        salary_max=55000,
        salary_currency="EUR",
        salary_period="yearly",
        # ~40-55k EUR/year is ~16.7-22.9k RON/month once normalised
        # (gosha/salary.py). Subscription salary_min is monthly RON.
        salary_monthly_min_ron=16667.0,
        salary_monthly_max_ron=22917.0,
    )
    session.add(job)
    await session.commit()
    return job


@pytest_asyncio.fixture
async def sample_subscription(session: AsyncSession, sample_user: User) -> Subscription:
    """Create a sample subscription."""
    sub = Subscription(
        user_id=sample_user.id,
        max_age_days=7,
    )
    sub.keywords = ["software engineer"]
    sub.locations = ["Cluj"]
    sub.excluded_keywords = []
    sub.company_blacklist = []
    sub.experience_levels = ["any"]
    session.add(sub)
    await session.commit()
    return sub
