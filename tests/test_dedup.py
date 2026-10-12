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


def test_normalize_key_keeps_language_symbols():
    """"C++", "C#" and "C" are different jobs, not one after punctuation."""
    keys = {
        dedup.normalize_key(title, "Acme")
        for title in ("C++ Developer", "C# Developer", "C Developer")
    }
    assert len(keys) == 3
    assert dedup.normalize_key("Developer C++ (Remote)", "Acme") == dedup.normalize_key(
        "C++ Developer", "Acme",
    )
    # A hashtag is decoration, not part of the role.
    assert dedup.normalize_key("Python Developer #remote", "Acme") == dedup.normalize_key(
        "Python Developer", "Acme",
    )


def test_normalize_key_refuses_placeholders():
    """No employer, or no title, is not an identity: never group on it."""
    assert dedup.normalize_key("Python Developer", "Unknown") is None
    assert dedup.normalize_key("Python Developer", "") is None
    assert dedup.normalize_key("Python Developer", "N/A") is None
    assert dedup.normalize_key("Unknown Title", "Acme") is None
    assert dedup.normalize_key("Remote (m/f/d)", "Acme") is None
    assert dedup.normalize_key("Python Developer", "Acme") is not None


async def _group_ids(session, *jobs: Job) -> list[int | None]:
    for job in jobs:
        await session.refresh(job)
    return [job.dedup_group_id for job in jobs]


@pytest.mark.asyncio
async def test_unknown_employers_and_different_languages_not_merged(patched_db, session):
    session.add_all([
        Job(url="https://a.com/u1", title="Accountant", company="Unknown", source="indeed"),
        Job(url="https://b.com/u2", title="Accountant", company="Unknown", source="linkedin"),
        Job(url="https://a.com/c1", title="C++ Developer", company="Acme", source="indeed"),
        Job(url="https://a.com/c2", title="C# Developer", company="Acme", source="indeed"),
    ])
    await session.commit()

    assert await dedup.assign_dedup_groups() == 0


@pytest.mark.asyncio
async def test_lone_survivor_is_released_when_canonical_expires(patched_db, session):
    """The canonical's link died: the duplicate is now the only live copy and
    must show up again instead of pointing at a dead row forever."""
    j1 = Job(url="https://indeed.com/e", title="Go Dev", company="Acme", source="indeed")
    j2 = Job(url="https://ejobs.ro/e", title="Go Dev", company="Acme", source="ejobs")
    session.add_all([j1, j2])
    await session.commit()
    await dedup.assign_dedup_groups()
    assert await _group_ids(session, j2) == [j1.id]

    j1.is_active = False
    await session.commit()
    assert await dedup.assign_dedup_groups() == 0
    assert await _group_ids(session, j2) == [None]

    from gosha.services import jobs as jobs_service

    page, total = await jobs_service.list_jobs(
        user_id=1, filters=jobs_service.JobFilters(), page=1, per_page=50,
    )
    assert [job.url for job in page] == ["https://ejobs.ro/e"] and total == 1


@pytest.mark.asyncio
async def test_group_reelects_a_live_canonical(patched_db, session):
    j1, j2, j3 = (
        Job(url=f"https://x.com/r{i}", title="Rust Dev", company="Acme", source="indeed")
        for i in range(3)
    )
    session.add_all([j1, j2, j3])
    await session.commit()
    await dedup.assign_dedup_groups()

    j1.is_active = False
    await session.commit()
    assert await dedup.assign_dedup_groups() == 1
    assert await _group_ids(session, j2, j3) == [j2.id, j2.id]


@pytest.mark.asyncio
async def test_title_change_leaves_the_group(patched_db, session):
    j1 = Job(url="https://x.com/t1", title="QA Engineer", company="Acme", source="indeed")
    j2 = Job(url="https://x.com/t2", title="QA Engineer", company="Acme", source="ejobs")
    session.add_all([j1, j2])
    await session.commit()
    await dedup.assign_dedup_groups()

    j2.title = "Senior QA Engineer"
    await session.commit()
    await dedup.assign_dedup_groups()
    assert await _group_ids(session, j1, j2) == [None, None]


@pytest.mark.asyncio
async def test_canonical_aging_out_of_the_window_releases_the_member(patched_db, session):
    """Feed and grouping use a 30-day window: a member whose canonical aged
    out of it would otherwise be hidden from a feed that no longer has the
    canonical either."""
    old = Job(url="https://x.com/a1", title="Data Analyst", company="Acme", source="indeed")
    old.first_seen_at = datetime.now(timezone.utc) - timedelta(days=40)
    recent = Job(url="https://x.com/a2", title="Data Analyst", company="Acme", source="ejobs")
    session.add_all([old, recent])
    await session.commit()
    old.dedup_group_id = old.id
    recent.dedup_group_id = old.id  # grouped back when both were recent
    await session.commit()

    await dedup.assign_dedup_groups()
    assert await _group_ids(session, recent) == [None]


@pytest.mark.asyncio
async def test_older_rows_follow_their_canonical(patched_db, session):
    """Outside the window rows are not regrouped, but a duplicate is only
    hidden while its canonical is still live."""
    long_ago = datetime.now(timezone.utc) - timedelta(days=60)
    rows = [
        Job(url=f"https://x.com/o{i}", title=title, company="Acme", source="indeed")
        for i, title in enumerate(("Java Dev", "Java Dev", "SRE", "SRE"))
    ]
    for job in rows:
        job.first_seen_at = long_ago
    session.add_all(rows)
    await session.commit()
    live_canon, kept, dead_canon, orphan = rows
    live_canon.dedup_group_id = live_canon.id
    kept.dedup_group_id = live_canon.id
    dead_canon.dedup_group_id = dead_canon.id
    dead_canon.is_active = False
    orphan.dedup_group_id = dead_canon.id
    await session.commit()

    await dedup.assign_dedup_groups()
    assert await _group_ids(session, kept, orphan) == [live_canon.id, None]
