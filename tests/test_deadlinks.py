"""Tests for dead-link detection (404/410 postings get deactivated)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gosha import deadlinks
from gosha.models import Job


def make_job(url: str, days_old: int = 10, **kwargs) -> Job:
    job = Job(
        url=url,
        title=kwargs.pop("title", "Dev"),
        company="Acme",
        # A board whose links can be checked (see UNCHECKABLE_SOURCES).
        source=kwargs.pop("source", "ejobs"),
        **kwargs,
    )
    job.first_seen_at = datetime.now(timezone.utc) - timedelta(days=days_old)
    return job


@pytest.mark.asyncio
async def test_dead_jobs_deactivated(patched_db, session, monkeypatch):
    dead = make_job("https://x.com/dead")
    alive = make_job("https://x.com/alive")
    session.add_all([dead, alive])
    await session.commit()

    async def fake_status(client, url: str) -> int | None:
        return 404 if "dead" in url else 200

    monkeypatch.setattr(deadlinks, "_check_url_status", fake_status)

    expired = await deadlinks.check_dead_links()
    assert expired == 1

    await session.refresh(dead)
    await session.refresh(alive)
    assert dead.is_active is False
    assert alive.is_active is True
    assert dead.last_checked_at is not None
    assert alive.last_checked_at is not None


@pytest.mark.asyncio
async def test_fresh_jobs_not_checked(patched_db, session, monkeypatch):
    fresh = make_job("https://x.com/fresh", days_old=2)
    session.add(fresh)
    await session.commit()

    calls: list[str] = []

    async def fake_status(client, url: str) -> int | None:
        calls.append(url)
        return 404

    monkeypatch.setattr(deadlinks, "_check_url_status", fake_status)
    await deadlinks.check_dead_links()

    assert calls == []
    await session.refresh(fresh)
    assert fresh.is_active is True


@pytest.mark.asyncio
async def test_network_errors_leave_job_active(patched_db, session, monkeypatch):
    job = make_job("https://x.com/flaky")
    session.add(job)
    await session.commit()

    async def fake_status(client, url: str) -> int | None:
        return None  # timeout / connection error

    monkeypatch.setattr(deadlinks, "_check_url_status", fake_status)
    expired = await deadlinks.check_dead_links()

    assert expired == 0
    await session.refresh(job)
    assert job.is_active is True
    assert job.last_checked_at is not None  # still stamped — retried later


@pytest.mark.asyncio
async def test_oldest_checked_first_and_sample_capped(patched_db, session, monkeypatch):
    now = datetime.now(timezone.utc)
    never_checked = make_job("https://x.com/never")
    recently_checked = make_job("https://x.com/recent")
    recently_checked.last_checked_at = now - timedelta(hours=1)
    session.add_all([never_checked, recently_checked])
    await session.commit()

    checked: list[str] = []

    async def fake_status(client, url: str) -> int | None:
        checked.append(url)
        return 200

    monkeypatch.setattr(deadlinks, "_check_url_status", fake_status)
    await deadlinks.check_dead_links(sample_size=1)

    assert checked == ["https://x.com/never"]  # never-checked goes first


@pytest.mark.asyncio
async def test_bot_walled_sources_do_not_use_the_check_budget(patched_db, session, monkeypatch):
    """LinkedIn/Indeed/Glassdoor answer 999/403/429 to live and dead postings
    alike; sampled first (never checked), they crowded out boards whose
    checks work."""
    walled = [
        make_job(f"https://www.{source}.com/{i}", source=source)
        for i, source in enumerate(sorted(deadlinks.UNCHECKABLE_SOURCES))
    ]
    checkable = make_job("https://www.ejobs.ro/job/1")
    checkable.last_checked_at = datetime.now(timezone.utc) - timedelta(days=1)
    session.add_all([*walled, checkable])
    await session.commit()

    checked: list[str] = []

    async def fake_status(client, url: str) -> int | None:
        checked.append(url)
        return 999

    monkeypatch.setattr(deadlinks, "_check_url_status", fake_status)
    await deadlinks.check_dead_links(sample_size=1)

    assert checked == ["https://www.ejobs.ro/job/1"]


@pytest.mark.asyncio
async def test_jobs_no_scrape_returns_any_more_expire(patched_db, session, monkeypatch):
    now = datetime.now(timezone.utc)
    gone = make_job("https://www.linkedin.com/gone", source="linkedin", days_old=60)
    gone.last_seen_at = now - timedelta(days=deadlinks.UNSEEN_EXPIRY_DAYS + 1)
    still_listed = make_job("https://www.linkedin.com/listed", source="linkedin", days_old=60)
    still_listed.last_seen_at = now - timedelta(days=2)
    session.add_all([gone, still_listed])
    await session.commit()

    async def fake_status(client, url: str) -> int | None:
        raise AssertionError("bot-walled boards are not link-checked")

    monkeypatch.setattr(deadlinks, "_check_url_status", fake_status)
    assert await deadlinks.check_dead_links() == 1

    await session.refresh(gone)
    await session.refresh(still_listed)
    assert gone.is_active is False
    assert still_listed.is_active is True



@pytest.mark.asyncio
async def test_full_catalog_boards_expire_after_two_weeks_unseen(patched_db, session):
    """eJobs' IT browse and Hipo's city catalog are crawled whole: a posting
    missing from them for 14 days is gone (prod, 2026-10-16: hipo 9/20 and
    ejobs 6/64 active rows unseen that long). Keyword-search boards keep 30
    days: a live posting can drop out of their results by ranking alone."""
    now = datetime.now(timezone.utc)

    def unseen(source: str, days: int) -> Job:
        job = make_job(f"https://{source}.test/{days}", source=source, days_old=60)
        job.last_seen_at = now - timedelta(days=days)
        return job

    rows = {
        ("ejobs", 15): unseen("ejobs", 15), ("hipo", 15): unseen("hipo", 15),
        ("ejobs", 10): unseen("ejobs", 10),
        ("linkedin", 15): unseen("linkedin", 15), ("bestjobs", 15): unseen("bestjobs", 15),
        ("linkedin", 31): unseen("linkedin", 31),
    }
    session.add_all(rows.values())
    await session.commit()

    assert await deadlinks.expire_unseen_jobs() == 3
    for job in rows.values():
        await session.refresh(job)
    assert {key for key, job in rows.items() if not job.is_active} == {
        ("ejobs", 15), ("hipo", 15), ("linkedin", 31),
    }
