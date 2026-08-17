"""Tests for cross-board job deduplication."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gosha import dedup
from gosha.models import Job


def test_normalize_key_ignores_case_punctuation_and_legal_suffixes():
    a = dedup.normalize_key("Senior Python Developer!", "Acme S.R.L.")
    b = dedup.normalize_key("senior python developer", "ACME SRL")
    assert a == b

    c = dedup.normalize_key("QA Engineer", "Other Corp")
    assert a != c


def test_normalize_key_ignores_decorations_and_word_order():
    base = dedup.normalize_key("Senior Python Developer", "Acme")
    assert dedup.normalize_key("Senior Python Developer (Remote)", "Acme") == base
    assert dedup.normalize_key("Python Developer Senior [Hybrid]", "Acme") == base
    assert dedup.normalize_key("Senior Python Developer - Cluj-Napoca", "Acme") == base
    # A genuinely different seniority stays distinct
    assert dedup.normalize_key("Junior Python Developer", "Acme") != base


@pytest.mark.asyncio
async def test_assign_groups_marks_duplicates(patched_db, session):
    j1 = Job(url="https://indeed.com/x", title="Python Dev", company="Acme", source="indeed")
    j2 = Job(url="https://linkedin.com/x", title="Python Dev", company="Acme", source="linkedin")
    j3 = Job(url="https://ejobs.ro/x", title="python  dev", company="ACME", source="ejobs")
    unique = Job(url="https://indeed.com/y", title="QA", company="Other", source="indeed")
    session.add_all([j1, j2, j3, unique])
    await session.commit()

    marked = await dedup.assign_dedup_groups()
    assert marked == 2  # two non-canonical duplicates

    for job in (j1, j2, j3, unique):
        await session.refresh(job)
    canonical = min(j1.id, j2.id, j3.id)
    assert j1.dedup_group_id == canonical
    assert j2.dedup_group_id == canonical
    assert j3.dedup_group_id == canonical
    assert unique.dedup_group_id is None or unique.dedup_group_id == unique.id


@pytest.mark.asyncio
async def test_assign_groups_skips_old_and_inactive(patched_db, session):
    old = Job(url="https://a.com/1", title="Same", company="Co", source="indeed")
    old.first_seen_at = datetime.now(timezone.utc) - timedelta(days=60)
    inactive = Job(
        url="https://a.com/2", title="Same", company="Co", source="linkedin",
        is_active=False,
    )
    fresh = Job(url="https://a.com/3", title="Same", company="Co", source="ejobs")
    session.add_all([old, inactive, fresh])
    await session.commit()

    marked = await dedup.assign_dedup_groups()
    assert marked == 0  # only one in-window active job with that key

    await session.refresh(fresh)
    assert fresh.dedup_group_id in (None, fresh.id)


@pytest.mark.asyncio
async def test_browse_hides_non_canonical_duplicates(patched_db, session, web_env_unused=None):
    from gosha.services import jobs as jobs_service

    j1 = Job(url="https://indeed.com/d", title="Dup Dev", company="Acme", source="indeed")
    j2 = Job(url="https://linkedin.com/d", title="Dup Dev", company="Acme", source="linkedin")
    session.add_all([j1, j2])
    await session.commit()
    await dedup.assign_dedup_groups()

    page, total = await jobs_service.list_jobs(
        user_id=1, filters=jobs_service.JobFilters(), page=1, per_page=50,
    )
    urls = {j.url for j in page}
    assert total == 1
    assert urls == {"https://indeed.com/d"}  # canonical (lowest id) survives


@pytest.mark.asyncio
async def test_feed_hides_non_canonical_duplicates(patched_db, session):
    import numpy as np

    from gosha import recommend
    from gosha.embeddings import EMBEDDING_DIM, vec_to_bytes
    from gosha.models import User

    vec = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    vec[0] = 1.0
    user = User(discord_user_id=606, cv_embedding=vec_to_bytes(vec))
    session.add(user)
    j1 = Job(
        url="https://indeed.com/f", title="Feed Dup", company="Acme",
        source="indeed", embedding=vec_to_bytes(vec),
    )
    j2 = Job(
        url="https://linkedin.com/f", title="Feed Dup", company="Acme",
        source="linkedin", embedding=vec_to_bytes(vec),
    )
    session.add_all([j1, j2])
    await session.commit()
    await dedup.assign_dedup_groups()

    items, total = await recommend.get_feed(user.id, page=1, per_page=10)
    assert total == 1
    assert [item.job.url for item in items] == ["https://indeed.com/f"]
