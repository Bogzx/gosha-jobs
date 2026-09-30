"""Tests for analytics tracking and the admin API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from gosha.events import Event
from gosha.models import Job


@pytest.mark.asyncio
async def test_pageview_anonymous_and_signed_in(client, web_user, session):
    user, cookies = web_user

    resp = await client.post("/api/v1/events/pageview", json={"path": "/feed"})
    assert resp.status_code == 200

    resp = await client.post(
        "/api/v1/events/pageview", json={"path": "/tracker"}, cookies=cookies
    )
    assert resp.status_code == 200

    events = (
        await session.execute(select(Event).where(Event.event_type == "web.pageview"))
    ).scalars().all()
    assert len(events) == 2
    anonymous = [e for e in events if e.actor_id is None]
    identified = [e for e in events if e.actor_id == user.id]
    assert len(anonymous) == 1 and len(identified) == 1
    assert identified[0].payload["path"] == "/tracker"


@pytest.mark.asyncio
async def test_pageview_rate_limited_per_client(client, session, monkeypatch):
    import gosha.api.analytics as analytics

    for _ in range(analytics.RATE_LIMIT_PER_MINUTE + 10):
        resp = await client.post("/api/v1/events/pageview", json={"path": "/spam"})
        assert resp.status_code == 200  # never breaks the SPA

    events = (
        await session.execute(select(Event).where(Event.event_type == "web.pageview"))
    ).scalars().all()
    assert len(events) == analytics.RATE_LIMIT_PER_MINUTE  # excess dropped


@pytest.mark.asyncio
async def test_admin_endpoints_require_admin(client, web_user):
    _user, cookies = web_user
    for path in ("/api/v1/admin/stats", "/api/v1/admin/usage", "/api/v1/admin/scrape-health"):
        resp = await client.get(path, cookies=cookies)
        assert resp.status_code == 403, path


@pytest.mark.asyncio
async def test_admin_stats(client, admin_user_fixture, session):
    _admin, cookies = admin_user_fixture
    session.add(Job(url="https://s.com/1", title="X", company="Y", source="indeed"))
    await session.commit()

    resp = await client.get("/api/v1/admin/stats", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_users"] == 1
    assert body["total_jobs"] == 1
    assert body["active_jobs"] == 1


@pytest.mark.asyncio
async def test_admin_usage_buckets(client, admin_user_fixture, session):
    admin, cookies = admin_user_fixture
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)

    e1 = Event(event_type="web.pageview", actor_id=admin.id, timestamp=now)
    e2 = Event(event_type="web.pageview", actor_id=None, timestamp=now)
    e3 = Event(event_type="web.pageview", actor_id=admin.id, timestamp=yesterday)
    e4 = Event(event_type="web.signin", actor_id=admin.id, timestamp=now)
    e4.payload = {"new_user": True}
    e5 = Event(event_type="web.apply_click", actor_id=admin.id, timestamp=now)
    session.add_all([e1, e2, e3, e4, e5])
    await session.commit()

    resp = await client.get("/api/v1/admin/usage", params={"days": 7}, cookies=cookies)
    assert resp.status_code == 200
    days = resp.json()["days"]
    today_row = days[-1]
    assert today_row["pageviews"] == 2
    assert today_row["active_users"] == 1
    assert today_row["signups"] == 1
    assert today_row["applies"] == 1
    yesterday_row = days[-2]
    assert yesterday_row["pageviews"] == 1


@pytest.mark.asyncio
async def test_admin_scrape_health(client, admin_user_fixture, session):
    _admin, cookies = admin_user_fixture
    session.add_all([
        Job(url="https://s.com/i1", title="A", company="C", source="indeed"),
        Job(url="https://s.com/i2", title="B", company="C", source="indeed"),
        Job(url="https://s.com/l1", title="C", company="C", source="linkedin"),
    ])
    session.add(Event(event_type="job.discovered", job_id=1))
    await session.commit()

    resp = await client.get("/api/v1/admin/scrape-health", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert body["jobs_by_source"] == {"indeed": 2, "linkedin": 1}
    assert body["last_job_discovered_at"] is not None
    assert isinstance(body["recent_events"], list)
