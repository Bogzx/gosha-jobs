"""The hard guarantee: a company blacklisted (or keyword excluded) in ANY of
the user's searches never appears in their feed, browse results, or DMs."""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gosha import recommend
from gosha.embeddings import EMBEDDING_DIM, vec_to_bytes
from gosha.models import Job, Subscription, User
from gosha.services import jobs as jobs_service


def unit_vec(axis: int = 0) -> np.ndarray:
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    v[axis] = 1.0
    return v


@pytest.fixture
async def user_with_blacklist(session: AsyncSession):
    user = User(discord_user_id=8080, cv_embedding=vec_to_bytes(unit_vec()))
    session.add(user)
    await session.flush()

    sub = Subscription(user_id=user.id)
    sub.keywords = ["developer"]
    sub.locations = ["cluj"]
    sub.company_blacklist = ["Bending Spoons"]
    sub.excluded_keywords = ["gambling"]
    session.add(sub)

    blocked_company = Job(
        url="https://x.com/bs", title="iOS Developer", company="Bending Spoons Milan",
        location="Remote", source="linkedin", embedding=vec_to_bytes(unit_vec()),
    )
    blocked_keyword = Job(
        url="https://x.com/gamble", title="Backend Developer",
        company="LuckyBet", description="Work on our gambling platform",
        location="Cluj-Napoca, Romania", source="indeed",
        embedding=vec_to_bytes(unit_vec()),
    )
    fine = Job(
        url="https://x.com/ok", title="Python Developer", company="NiceCorp",
        location="Cluj-Napoca, Romania", source="indeed",
        embedding=vec_to_bytes(unit_vec()),
    )
    session.add_all([blocked_company, blocked_keyword, fine])
    await session.commit()
    return user


@pytest.mark.asyncio
async def test_feed_never_shows_blacklisted(patched_db, user_with_blacklist):
    items, total = await recommend.get_feed(user_with_blacklist.id, page=1, per_page=50)
    companies = {item.job.company for item in items}
    urls = {item.job.url for item in items}

    assert "Bending Spoons Milan" not in companies
    assert "https://x.com/gamble" not in urls
    assert urls == {"https://x.com/ok"}
    assert total == 1


@pytest.mark.asyncio
async def test_browse_never_shows_blacklisted(patched_db, user_with_blacklist):
    page, total = await jobs_service.list_jobs(
        user_with_blacklist.id, jobs_service.JobFilters(), page=1, per_page=50,
    )
    assert {j.url for j in page} == {"https://x.com/ok"}
    assert total == 1


@pytest.mark.asyncio
async def test_other_users_unaffected(patched_db, session, user_with_blacklist):
    other = User(discord_user_id=9090)
    session.add(other)
    await session.commit()

    page, total = await jobs_service.list_jobs(
        other.id, jobs_service.JobFilters(), page=1, per_page=50,
    )
    assert total == 3  # no exclusions configured for this user
