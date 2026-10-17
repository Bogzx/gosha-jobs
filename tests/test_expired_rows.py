"""A posting its board still lists but whose own page says expired
(BestJobs keeps listing some): the adapter reports it through
SearchQuery.on_expired, and the scrape stage deactivates its stored row,
so its pending DMs stop and the feed drops it."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import select

import gosha.pipeline as pipeline
from gosha.models import Job, Subscription, User, UserJob
from gosha.scrapers.base import RawJob

EXPIRED = "https://www.bestjobs.eu/ro/loc-de-munca/expired-1"
LIVE = "https://www.bestjobs.eu/ro/loc-de-munca/live-1"
OTHER_BOARD = "https://www.ejobs.ro/user/locuri-de-munca/expired-1"


class _Tunnels:
    def active_proxies(self):
        return []


def _raw(url: str) -> RawJob:
    return RawJob(
        url=url, title="Python Developer", company="Acme",
        location="Cluj-Napoca, Romania", source="bestjobs",
        posted_at=datetime.now(timezone.utc),
    )


class _BestJobs:
    """Lists `live`; reports `expired` the way bestjobs.py does."""

    name = "bestjobs"

    def __init__(self, live: list[str], expired: list[str]):
        self.live, self.expired = live, expired

    async def search(self, query):
        for url in self.expired:
            query.on_expired(url)
        return [_raw(url) for url in self.live]


async def _seed(session):
    user = User(discord_user_id=7007)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id, max_age_days=7)
    sub.keywords = ["python developer"]
    sub.locations = ["cluj"]
    session.add(sub)
    jobs = [
        Job(url=url, title="Python Developer", company="Acme",
            location="Cluj-Napoca, Romania", source=source, description="x" * 300)
        for url, source in ((EXPIRED, "bestjobs"), (LIVE, "bestjobs"),
                            (OTHER_BOARD, "ejobs"))
    ]
    session.add_all(jobs)
    await session.flush()
    session.add(UserJob(user_id=user.id, job_id=jobs[0].id, subscription_id=sub.id,
                        relevance_score=0.6, dm_status=UserJob.DM_PENDING))
    await session.commit()


async def _active(session) -> dict[str, bool]:
    session.expire_all()
    return {j.url: j.is_active for j in (await session.execute(select(Job))).scalars()}


def _run_with(monkeypatch, scraper):
    async def no_jobspy(tunnel_manager, keyword, location, max_age_days, boards=None):
        return pd.DataFrame()

    monkeypatch.setattr("gosha.scraper.scrape_jobs_raw", no_jobspy)
    monkeypatch.setattr("gosha.scrapers.registry.get_extra_scrapers", lambda: [scraper])
    return pipeline.run_scrape_stage(_Tunnels())


@pytest.mark.asyncio
async def test_a_listed_expired_posting_is_deactivated(patched_db, session, monkeypatch):
    await _seed(session)
    events: list[int] = []

    async def record(job_id):
        events.append(job_id)

    monkeypatch.setattr("gosha.events.emit_job_expired", record)
    # The board returns nothing live: the expired URL is still handled.
    await _run_with(monkeypatch, _BestJobs(live=[], expired=[EXPIRED, OTHER_BOARD]))

    active = await _active(session)
    assert active == {EXPIRED: False, LIVE: True, OTHER_BOARD: True}  # by source too
    assert len(events) == 1
    assert len(active) == 3  # nothing inserted

    sent: list[int] = []

    async def fake_dm(bot, discord_id, embed, view=None):
        sent.append(discord_id)
        return pipeline.DM_SENT

    monkeypatch.setattr(pipeline, "_dm_user", fake_dm)
    assert await pipeline.deliver_pending(object()) == 0
    assert sent == []


@pytest.mark.asyncio
async def test_a_posting_also_listed_live_stays_active(patched_db, session, monkeypatch):
    await _seed(session)
    await _run_with(monkeypatch, _BestJobs(live=[EXPIRED, LIVE], expired=[EXPIRED]))

    active = await _active(session)
    assert active[EXPIRED] is True and active[LIVE] is True
