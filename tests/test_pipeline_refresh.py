"""Stored jobs among the rows the scrape filter drops are corrected.

Prod, 2026-10-16: after two deploys emptied the detail-page cache, BestJobs
and Hipo postings were stored before their page was read (no level, no
description), which a junior search's filter lets through. When the next
cycle read the pages (Middle, years asked) the filter dropped them, and the
upsert only ever saw what the filter kept, so 36 BestJobs and 7 Hipo rows
stayed active and empty with DMs pending for them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import select

import gosha.pipeline as pipeline
from gosha.models import Job, Subscription, User, UserJob

URL = "https://www.bestjobs.eu/ro/loc-de-munca/frontend-developer-393"
MIDDLE_PAGE = (
    "We are looking for a Frontend Developer to join our product team in "
    "Cluj-Napoca. You will build React interfaces with TypeScript, work with "
    "designers and backend engineers, and own features end to end. "
    "Requirements: at least 3 years of experience with React in production."
)
LONG_AGO = datetime(2026, 10, 16, 17, 32, tzinfo=timezone.utc)


def _scraped(**overrides) -> pd.DataFrame:
    row = {
        "job_url": URL, "title": "Frontend Developer", "company": "Markit",
        "location": "Cluj-Napoca, Romania", "site": "bestjobs",
        "description": MIDDLE_PAGE, "experience_level": "mid",
    }
    row.update(overrides)
    return pd.DataFrame([row])


async def _stored(session, **overrides) -> int:
    job = Job(
        url=URL, title="Frontend Developer", company="Markit",
        location="Cluj-Napoca, Romania", source="bestjobs", description="",
        first_seen_at=LONG_AGO, last_seen_at=LONG_AGO, is_active=True,
        embedding=b"old", embedding_model="m",
    )
    for key, value in overrides.items():
        setattr(job, key, value)
    session.add(job)
    await session.commit()
    return job.id


async def _job(session, job_id: int) -> Job:
    session.expire_all()
    return (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_a_dropped_row_corrects_the_stored_job(patched_db, session):
    job_id = await _stored(session)

    kept = await pipeline._store_filtered(
        _scraped(), ["cs entry level"], ["junior"], "bestjobs test",
    )

    assert kept == []  # the filter's verdict stands: nothing new to match
    job = await _job(session, job_id)
    assert job.description == MIDDLE_PAGE
    assert job.experience_level == "mid"
    assert job.embedding is None  # re-embedded with the description
    # Not a sighting for any search: it ages out instead of staying alive.
    assert _utc(job.last_seen_at) == LONG_AGO


@pytest.mark.asyncio
async def test_a_dropped_row_does_not_revive_an_inactive_job(patched_db, session):
    job_id = await _stored(session, is_active=False)
    await pipeline.refresh_known_rows(_scraped())
    job = await _job(session, job_id)
    assert job.is_active is False
    assert job.experience_level == "mid"


@pytest.mark.asyncio
async def test_a_dropped_row_inserts_nothing_new(patched_db, session):
    assert await pipeline.refresh_known_rows(_scraped(job_url="https://x.test/new")) == 0
    assert (await session.execute(select(Job))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_dropped_row_without_its_page_keeps_what_is_stored(patched_db, session):
    """A sighting with less data (no page this time) blanks nothing."""
    job_id = await _stored(session, description=MIDDLE_PAGE, experience_level="mid")
    await pipeline.refresh_known_rows(_scraped(description="", experience_level=None))
    job = await _job(session, job_id)
    assert job.description == MIDDLE_PAGE and job.experience_level == "mid"


@pytest.mark.asyncio
async def test_kept_rows_are_still_upserted_and_seen(patched_db, session):
    job_id = await _stored(session)
    junior = _scraped(title="Junior Frontend Developer", experience_level="entry",
                      description=MIDDLE_PAGE.replace("at least 3 years", "no prior"))
    kept = await pipeline._store_filtered(junior, ["cs entry level"], ["junior"], "test")
    assert [j.id for j in kept] == [job_id]
    job = await _job(session, job_id)
    assert _utc(job.last_seen_at) > LONG_AGO + timedelta(hours=1)
    assert job.title == "Junior Frontend Developer"


@pytest.mark.asyncio
async def test_a_pending_dm_is_skipped_once_the_refresh_lands(patched_db, session, monkeypatch):
    """Before: held pending for want of a description ("it may come").
    After the refresh the page says Middle, and the send-time re-check
    skips it: no DM, nothing left pending."""
    job_id = await _stored(session)
    user = User(discord_user_id=5150)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id)
    sub.keywords = ["cs entry level"]
    sub.locations = ["cluj"]
    sub.experience_levels = ["junior"]
    session.add(sub)
    await session.flush()
    session.add(UserJob(user_id=user.id, job_id=job_id, subscription_id=sub.id,
                        relevance_score=0.6, dm_status=UserJob.DM_PENDING))
    await session.commit()

    sent: list[int] = []

    async def fake_dm(bot, discord_id, embed, view=None):
        sent.append(discord_id)
        return pipeline.DM_SENT

    monkeypatch.setattr(pipeline, "_dm_user", fake_dm)

    class _Bot:
        alert_channel_id = None

        def get_channel(self, _id):
            return None

    async def status() -> str | None:
        session.expire_all()
        return (await session.execute(select(UserJob.dm_status))).scalar_one()

    assert await pipeline.deliver_pending(_Bot()) == 0
    assert await status() == UserJob.DM_PENDING  # no evidence yet: waits

    await pipeline._store_filtered(_scraped(), ["cs entry level"], ["junior"], "test")

    assert await pipeline.deliver_pending(_Bot()) == 0
    assert sent == []
    assert await status() == UserJob.DM_SKIPPED
