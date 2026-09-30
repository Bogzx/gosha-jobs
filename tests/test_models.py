"""Tests for ORM models — Job, User, Subscription, UserJob."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gosha.models import Job, Subscription, User, UserJob

# ── Job model ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_job(session: AsyncSession):
    job = Job(
        url="https://indeed.com/job/123",
        title="Backend Developer",
        company="Acme",
        location="Bucharest, Romania",
        source="indeed",
    )
    session.add(job)
    await session.commit()

    result = await session.execute(select(Job).where(Job.url == "https://indeed.com/job/123"))
    fetched = result.scalar_one()
    assert fetched.title == "Backend Developer"
    assert fetched.company == "Acme"
    assert fetched.is_active is True
    assert fetched.first_seen_at is not None
    assert fetched.last_seen_at is not None


@pytest.mark.asyncio
async def test_job_url_unique(session: AsyncSession):
    job1 = Job(url="https://example.com/dup", title="Dev", company="X", source="indeed")
    session.add(job1)
    await session.commit()

    job2 = Job(url="https://example.com/dup", title="Dev 2", company="Y", source="linkedin")
    session.add(job2)
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_job_optional_salary(session: AsyncSession):
    job = Job(
        url="https://example.com/no-salary",
        title="Intern",
        company="StartupCo",
        source="glassdoor",
    )
    session.add(job)
    await session.commit()

    result = await session.execute(select(Job).where(Job.id == job.id))
    fetched = result.scalar_one()
    assert fetched.salary_min is None
    assert fetched.salary_max is None
    assert fetched.salary_currency is None


@pytest.mark.asyncio
async def test_job_with_salary(session: AsyncSession, sample_job: Job):
    assert sample_job.salary_min == 40000
    assert sample_job.salary_max == 55000
    assert sample_job.salary_currency == "EUR"


# ── User model ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_user(session: AsyncSession):
    user = User(discord_user_id=999888777)
    session.add(user)
    await session.commit()

    result = await session.execute(
        select(User).where(User.discord_user_id == 999888777)
    )
    fetched = result.scalar_one()
    assert fetched.id is not None
    assert fetched.discord_user_id == 999888777


@pytest.mark.asyncio
async def test_user_discord_id_unique(session: AsyncSession):
    u1 = User(discord_user_id=111)
    session.add(u1)
    await session.commit()

    u2 = User(discord_user_id=111)
    session.add(u2)
    with pytest.raises(IntegrityError):
        await session.commit()


# ── Subscription model ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subscription_json_lists(session: AsyncSession, sample_user: User):
    sub = Subscription(user_id=sample_user.id, max_age_days=14)
    sub.keywords = ["data scientist", "ml engineer"]
    sub.locations = ["Dublin", "London"]
    sub.excluded_keywords = ["sales"]
    sub.company_blacklist = ["BadCorp"]
    sub.experience_levels = ["junior", "mid"]
    sub.boards = ["indeed", "linkedin"]
    session.add(sub)
    await session.commit()

    result = await session.execute(select(Subscription).where(Subscription.id == sub.id))
    fetched = result.scalar_one()
    assert fetched.keywords == ["data scientist", "ml engineer"]
    assert fetched.locations == ["Dublin", "London"]
    assert fetched.excluded_keywords == ["sales"]
    assert fetched.company_blacklist == ["BadCorp"]
    assert fetched.experience_levels == ["junior", "mid"]
    assert fetched.boards == ["indeed", "linkedin"]
    assert fetched.max_age_days == 14


@pytest.mark.asyncio
async def test_subscription_defaults(session: AsyncSession, sample_user: User):
    sub = Subscription(user_id=sample_user.id)
    sub.keywords = ["test"]
    sub.locations = ["anywhere"]
    session.add(sub)
    await session.commit()

    result = await session.execute(select(Subscription).where(Subscription.id == sub.id))
    fetched = result.scalar_one()
    assert fetched.max_age_days == 7
    assert fetched.is_active is True
    assert fetched.remote_ok is False
    assert fetched.salary_min is None


@pytest.mark.asyncio
async def test_subscription_pause_resume(session: AsyncSession, sample_subscription: Subscription):
    assert sample_subscription.is_active is True

    sample_subscription.is_active = False
    await session.commit()

    result = await session.execute(
        select(Subscription).where(Subscription.id == sample_subscription.id)
    )
    fetched = result.scalar_one()
    assert fetched.is_active is False


@pytest.mark.asyncio
async def test_subscription_user_cascade_delete(session: AsyncSession):
    user = User(discord_user_id=777666555)
    session.add(user)
    await session.flush()

    sub = Subscription(user_id=user.id)
    sub.keywords = ["test"]
    sub.locations = ["test"]
    session.add(sub)
    await session.commit()

    # Delete user — subscription should cascade
    await session.delete(user)
    await session.commit()

    result = await session.execute(select(Subscription))
    assert result.scalars().all() == []


# ── UserJob model ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_user_job(session: AsyncSession, sample_user: User, sample_job: Job):
    uj = UserJob(user_id=sample_user.id, job_id=sample_job.id, relevance_score=0.85)
    session.add(uj)
    await session.commit()

    result = await session.execute(
        select(UserJob).where(
            UserJob.user_id == sample_user.id,
            UserJob.job_id == sample_job.id,
        )
    )
    fetched = result.scalar_one()
    assert fetched.relevance_score == 0.85
    assert fetched.feedback is None
    assert fetched.delivered_at is not None


@pytest.mark.asyncio
async def test_user_job_unique_constraint(
    session: AsyncSession, sample_user: User, sample_job: Job
):
    uj1 = UserJob(user_id=sample_user.id, job_id=sample_job.id)
    session.add(uj1)
    await session.commit()

    uj2 = UserJob(user_id=sample_user.id, job_id=sample_job.id)
    session.add(uj2)
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_user_job_feedback(session: AsyncSession, sample_user: User, sample_job: Job):
    from datetime import datetime, timezone

    uj = UserJob(user_id=sample_user.id, job_id=sample_job.id)
    session.add(uj)
    await session.commit()

    # Simulate user giving feedback
    uj.feedback = "interested"
    uj.feedback_at = datetime.now(timezone.utc)
    await session.commit()

    result = await session.execute(select(UserJob).where(UserJob.id == uj.id))
    fetched = result.scalar_one()
    assert fetched.feedback == "interested"
    assert fetched.feedback_at is not None


@pytest.mark.asyncio
async def test_user_job_different_users_same_job(session: AsyncSession, sample_job: Job):
    """Same job can be delivered to different users."""
    u1 = User(discord_user_id=111222333)
    u2 = User(discord_user_id=444555666)
    session.add_all([u1, u2])
    await session.flush()

    uj1 = UserJob(user_id=u1.id, job_id=sample_job.id)
    uj2 = UserJob(user_id=u2.id, job_id=sample_job.id)
    session.add_all([uj1, uj2])
    await session.commit()

    result = await session.execute(
        select(UserJob).where(UserJob.job_id == sample_job.id)
    )
    deliveries = result.scalars().all()
    assert len(deliveries) == 2


# ── Web platform extensions (users/jobs/subscriptions/applications/outbox) ──


@pytest.mark.asyncio
async def test_user_web_columns(session: AsyncSession):
    from gosha.models import Outbox  # noqa: F401 — ensure model imports

    user = User(
        discord_user_id=42,
        username="gosha",
        avatar_url="https://cdn.discordapp.com/avatars/42/a.png",
        in_guild=True,
    )
    session.add(user)
    await session.commit()

    result = await session.execute(select(User).where(User.discord_user_id == 42))
    fetched = result.scalar_one()
    assert fetched.username == "gosha"
    assert fetched.in_guild is True
    assert fetched.cv_embedding is None
    assert fetched.created_at is not None
    assert fetched.last_login_at is None


@pytest.mark.asyncio
async def test_job_embedding_and_posted_at(session: AsyncSession):
    from datetime import datetime, timezone

    job = Job(
        url="https://example.com/emb",
        title="Dev",
        company="X",
        source="indeed",
        embedding=b"\x00\x01\x02",
        posted_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    session.add(job)
    await session.commit()
    assert job.embedding == b"\x00\x01\x02"
    assert job.posted_at is not None


@pytest.mark.asyncio
async def test_subscription_name_and_notify(session: AsyncSession, sample_user: User):
    sub = Subscription(user_id=sample_user.id, name="Cluj internships")
    sub.keywords = ["intern"]
    sub.locations = ["cluj"]
    session.add(sub)
    await session.commit()
    assert sub.name == "Cluj internships"
    assert sub.notify_discord is True


@pytest.mark.asyncio
async def test_application_source_default(session: AsyncSession, sample_user: User, sample_job: Job):
    from gosha.models import Application

    app = Application(user_id=sample_user.id, job_id=sample_job.id)
    session.add(app)
    await session.commit()
    assert app.source == "discord"

    web_app = Application(
        user_id=sample_user.id, job_id=sample_job.id + 1000, source="web"
    )
    assert web_app.source == "web"


@pytest.mark.asyncio
async def test_outbox_roundtrip(session: AsyncSession, sample_user: User):
    import json as _json

    from gosha.models import Outbox

    o = Outbox(
        user_id=sample_user.id,
        kind="test_dm",
        payload=_json.dumps({"text": "hi"}),
    )
    session.add(o)
    await session.commit()

    result = await session.execute(select(Outbox))
    fetched = result.scalar_one()
    assert fetched.kind == "test_dm"
    assert fetched.payload_dict == {"text": "hi"}
    assert fetched.attempts == 0
    assert fetched.sent_at is None
    assert fetched.last_error is None
    assert fetched.created_at is not None


def test_plan_lines_advertise_only_enforced_limits():
    from gosha.models import get_tier_limits, plan_lines

    free = " ".join(plan_lines(get_tier_limits("free")))
    pro = " ".join(plan_lines(get_tier_limits("pro")))

    assert "5 saved searches" in free
    assert "5 AI cover letters a month" in free
    assert "every 5 min" in free
    assert "15 saved searches" in pro
    assert "Unlimited AI cover letters" in pro
    # Nothing implements these; /upgrade used to sell them.
    for text in (free, pro):
        lowered = text.lower()
        assert "email" not in lowered
        assert "priority" not in lowered
        assert "semantic" not in lowered


def test_plan_lines_unlimited_tier():
    from gosha.models import get_tier_limits, plan_lines

    assert plan_lines(get_tier_limits("unlimited"))[0] == "Unlimited saved searches"
