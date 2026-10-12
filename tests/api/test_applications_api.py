"""Tests for the applications tracker API."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from gosha.models import Application, Job, User


@pytest_asyncio.fixture
async def tracked(client, web_user, session):
    """A user with one tracked application; returns (user, cookies, app, job)."""
    user, cookies = web_user
    job = Job(url="https://a.com/1", title="Dev", company="Acme", source="indeed")
    session.add(job)
    await session.flush()
    app = Application(user_id=user.id, job_id=job.id, source="web")
    session.add(app)
    await session.commit()
    return user, cookies, app, job


@pytest.mark.asyncio
async def test_list_applications(client, tracked):
    _user, cookies, app, job = tracked
    resp = await client.get("/api/v1/applications", cookies=cookies)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == app.id
    assert items[0]["status"] == "applied"
    assert items[0]["job"]["company"] == "Acme"


@pytest.mark.asyncio
async def test_create_application(client, web_user, session):
    _user, cookies = web_user
    job = Job(url="https://a.com/new", title="QA", company="B", source="indeed")
    session.add(job)
    await session.commit()

    resp = await client.post(
        "/api/v1/applications", json={"job_id": job.id}, cookies=cookies
    )
    assert resp.status_code == 200
    assert resp.json()["job_id"] == job.id

    # Creating again returns the same application (idempotent)
    resp2 = await client.post(
        "/api/v1/applications", json={"job_id": job.id}, cookies=cookies
    )
    assert resp2.json()["id"] == resp.json()["id"]


@pytest.mark.asyncio
async def test_create_application_missing_job(client, web_user):
    _user, cookies = web_user
    resp = await client.post(
        "/api/v1/applications", json={"job_id": 99999}, cookies=cookies
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_status_and_notes(client, tracked, session):
    _user, cookies, app, _job = tracked
    resp = await client.patch(
        f"/api/v1/applications/{app.id}",
        json={"status": "interview", "notes": "Tech round on Friday"},
        cookies=cookies,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "interview"
    assert body["notes"] == "Tech round on Friday"

    await session.refresh(app)
    assert app.status == "interview"
    assert app.updated_at >= app.applied_at


@pytest.mark.asyncio
async def test_patch_invalid_status(client, tracked):
    _user, cookies, app, _job = tracked
    resp = await client.patch(
        f"/api/v1/applications/{app.id}", json={"status": "ghosted"}, cookies=cookies
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_status"


@pytest.mark.asyncio
async def test_delete_application(client, tracked, session):
    _user, cookies, app, _job = tracked
    resp = await client.delete(f"/api/v1/applications/{app.id}", cookies=cookies)
    assert resp.status_code == 200
    remaining = (await session.execute(select(Application))).scalars().all()
    assert remaining == []


@pytest.mark.asyncio
async def test_cross_user_access_404(client, tracked, session):
    _user, _cookies, app, _job = tracked
    from tests.api.conftest import session_cookie

    other = User(discord_user_id=777777)
    session.add(other)
    await session.commit()
    other_cookies = session_cookie(other.id)

    resp = await client.patch(
        f"/api/v1/applications/{app.id}", json={"status": "offer"}, cookies=other_cookies
    )
    assert resp.status_code == 404
    resp = await client.delete(f"/api/v1/applications/{app.id}", cookies=other_cookies)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_notes_null_clears_and_absent_keeps(client, tracked):
    _user, cookies, app, _job = tracked
    url = f"/api/v1/applications/{app.id}"
    await client.patch(url, json={"notes": "Call back Friday"}, cookies=cookies)

    kept = await client.patch(url, json={"status": "interview"}, cookies=cookies)
    assert kept.json()["notes"] == "Call back Friday"

    cleared = await client.patch(url, json={"notes": None}, cookies=cookies)
    assert cleared.status_code == 200
    assert cleared.json()["notes"] is None
    assert cleared.json()["status"] == "interview"


@pytest.mark.asyncio
async def test_create_racing_a_concurrent_create_returns_its_row(
    client, tracked, session, monkeypatch,
):
    """Double-click: both requests find nothing, the second insert hits
    uq_user_application. It used to be a 500; now it returns the first's row."""
    from gosha.services import applications as service

    _user, cookies, app, job = tracked
    real_find = service._find_application
    calls = {"n": 0}

    async def find_misses_once(*args):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real_find(*args)

    monkeypatch.setattr(service, "_find_application", find_misses_once)
    resp = await client.post(
        "/api/v1/applications", json={"job_id": job.id}, cookies=cookies,
    )

    assert resp.status_code == 200
    assert resp.json()["id"] == app.id
    rows = (await session.execute(select(Application))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_ids_beyond_int32_are_422_not_500(client, tracked):
    _user, cookies, _app, _job = tracked
    big = 2**31
    assert (await client.patch(
        f"/api/v1/applications/{big}", json={"notes": "x"}, cookies=cookies,
    )).status_code == 422
    assert (await client.delete(
        f"/api/v1/applications/{big}", cookies=cookies,
    )).status_code == 422
    assert (await client.post(
        "/api/v1/applications", json={"job_id": big}, cookies=cookies,
    )).status_code == 422
