"""Tests for recording 👍/👎 (what feedback does to rankings: test_recommend.py)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gosha.feedback import record_feedback
from gosha.models import Job, User, UserJob


@pytest_asyncio.fixture
async def patched_db(engine, monkeypatch):
    import gosha.database as db_mod

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "_engine", engine)
    monkeypatch.setattr(db_mod, "_session_factory", factory)
    yield factory


@pytest_asyncio.fixture
async def feedback_data(session: AsyncSession):
    """Create a user with jobs and deliveries for feedback testing."""
    user = User(discord_user_id=5001)
    session.add(user)
    await session.flush()

    jobs = []
    for i, (title, company, desc) in enumerate([
        ("Junior Python Developer", "TechCo", "Build APIs with Django and FastAPI. Python programming."),
        ("Senior Marketing Manager", "AdCorp", "Lead marketing campaigns and brand strategy."),
        ("Data Scientist Intern", "MLStartup", "Machine learning with Python, TensorFlow, PyTorch."),
        ("Sales Representative", "SellCo", "Cold calling and lead generation for B2B products."),
        ("Backend Engineer", "CloudInc", "Microservices with Python, Kubernetes, Docker."),
    ]):
        job = Job(
            url=f"https://test.com/job/{i}",
            title=title,
            company=company,
            description=desc,
            source="indeed",
            location="Cluj",
        )
        session.add(job)
        jobs.append(job)

    await session.flush()

    # Create user_jobs (deliveries)
    user_jobs = []
    for job in jobs:
        uj = UserJob(user_id=user.id, job_id=job.id)
        session.add(uj)
        user_jobs.append(uj)

    await session.commit()
    return {"user": user, "jobs": jobs, "user_jobs": user_jobs}


# ── record_feedback ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_feedback_interested(patched_db, feedback_data):
    uj = feedback_data["user_jobs"][0]
    result = await record_feedback(uj.id, "interested")
    assert result is True


@pytest.mark.asyncio
async def test_record_feedback_not_relevant(patched_db, feedback_data):
    uj = feedback_data["user_jobs"][1]
    result = await record_feedback(uj.id, "not_relevant")
    assert result is True


@pytest.mark.asyncio
async def test_record_feedback_invalid(patched_db, feedback_data):
    uj = feedback_data["user_jobs"][0]
    result = await record_feedback(uj.id, "invalid_feedback")
    assert result is False


@pytest.mark.asyncio
async def test_record_feedback_nonexistent(patched_db):
    result = await record_feedback(99999, "interested")
    assert result is False


@pytest.mark.asyncio
async def test_record_feedback_is_stored(patched_db, feedback_data):
    uj = feedback_data["user_jobs"][2]
    await record_feedback(uj.id, "interested")
    async with patched_db() as session:
        stored = await session.get(UserJob, uj.id)
    assert stored.feedback == "interested"
    assert stored.feedback_at is not None
