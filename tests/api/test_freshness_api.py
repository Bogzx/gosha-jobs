"""Public scrape-freshness probe used by the out-of-process watchdog."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gosha.services import freshness


@pytest.fixture(autouse=True)
def _no_cache():
    freshness.reset_cache()
    yield
    freshness.reset_cache()


@pytest.mark.asyncio
async def test_freshness_is_public_and_empty_on_a_fresh_install(client):
    resp = await client.get("/api/v1/health/freshness")
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_scrape_cycle_at"] is None
    assert body["last_new_job_at"] is None


@pytest.mark.asyncio
async def test_freshness_reports_last_cycle_and_newest_job(client, session):
    from gosha.events import Event
    from gosha.models import Job

    now = datetime.now(timezone.utc)
    session.add(Job(url="https://x.test/1", title="Dev", company="Acme", source="ejobs",
                    first_seen_at=now - timedelta(hours=2), last_seen_at=now))
    event = Event(event_type="scrape.cycle")
    event.payload = {"total": 120, "sources": 5}
    session.add(event)
    await session.commit()

    body = (await client.get("/api/v1/health/freshness")).json()
    assert body["last_scrape_cycle_jobs"] == 120
    assert body["last_scrape_cycle_at"] is not None
    assert body["last_new_job_at"].startswith((now - timedelta(hours=2)).strftime("%Y-%m-%dT%H"))
    # Timestamps and one count — nothing about users, searches or postings.
    assert set(body) == {
        "now", "last_scrape_cycle_at", "last_scrape_cycle_jobs",
        "last_new_job_at", "last_job_seen_at",
    }


@pytest.mark.asyncio
async def test_freshness_is_cached(client, monkeypatch):
    calls = []
    real = freshness._compute

    async def counting():
        calls.append(1)
        return await real()

    monkeypatch.setattr(freshness, "_compute", counting)
    for _ in range(5):
        await client.get("/api/v1/health/freshness")
    assert len(calls) == 1
