"""Tests for the subscriptions (saved searches) API."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from gosha.models import Outbox, Subscription
from tests.conftest import ON_POSTGRES

VALID = {
    "name": "Cluj internships",
    "keywords": ["software engineer intern"],
    "locations": ["cluj"],
    "experience_levels": ["intern"],
    "max_age_days": 7,
}


@pytest.mark.asyncio
async def test_create_and_list(client, web_user, session):
    _user, cookies = web_user
    resp = await client.post("/api/v1/subscriptions", json=VALID, cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Cluj internships"
    assert body["keywords"] == ["software engineer intern"]
    assert body["notify_discord"] is True
    assert body["is_active"] is True

    resp = await client.get("/api/v1/subscriptions", cookies=cookies)
    items = resp.json()["items"]
    assert len(items) == 1


@pytest.mark.asyncio
async def test_first_subscription_enqueues_welcome_dm(client, web_user, session):
    user, cookies = web_user  # web_user fixture has in_guild=True
    await client.post("/api/v1/subscriptions", json=VALID, cookies=cookies)

    rows = (await session.execute(select(Outbox))).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == "welcome"
    assert rows[0].user_id == user.id

    # Second subscription does not enqueue another welcome
    second = dict(VALID, name="Another", keywords=["qa intern"])
    await client.post("/api/v1/subscriptions", json=second, cookies=cookies)
    rows = (await session.execute(select(Outbox))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_tier_limits_subscription_count(client, web_user, session):
    user, cookies = web_user
    for i in range(5):  # free tier cap
        sub = Subscription(user_id=user.id)
        sub.keywords = [f"kw{i}"]
        sub.locations = ["cluj"]
        session.add(sub)
    await session.commit()

    resp = await client.post("/api/v1/subscriptions", json=VALID, cookies=cookies)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "tier_limit"


@pytest.mark.asyncio
async def test_tier_limits_keyword_count(client, web_user):
    _user, cookies = web_user
    payload = dict(VALID, keywords=[f"kw{i}" for i in range(6)])  # free cap is 5
    resp = await client.post("/api/v1/subscriptions", json=payload, cookies=cookies)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "tier_limit"


@pytest.mark.asyncio
async def test_validation_requires_keyword_and_location(client, web_user):
    _user, cookies = web_user
    resp = await client.post(
        "/api/v1/subscriptions",
        json=dict(VALID, keywords=[]),
        cookies=cookies,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_updates_fields(client, web_user):
    _user, cookies = web_user
    created = (
        await client.post("/api/v1/subscriptions", json=VALID, cookies=cookies)
    ).json()

    resp = await client.patch(
        f"/api/v1/subscriptions/{created['id']}",
        json={"notify_discord": False, "salary_min": 2000, "name": "Renamed"},
        cookies=cookies,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["notify_discord"] is False
    assert body["salary_min"] == 2000
    assert body["name"] == "Renamed"
    assert body["keywords"] == VALID["keywords"]  # untouched


@pytest.mark.asyncio
async def test_pause_resume(client, web_user):
    _user, cookies = web_user
    created = (
        await client.post("/api/v1/subscriptions", json=VALID, cookies=cookies)
    ).json()
    sub_id = created["id"]

    resp = await client.post(f"/api/v1/subscriptions/{sub_id}/pause", cookies=cookies)
    assert resp.json()["is_active"] is False
    resp = await client.post(f"/api/v1/subscriptions/{sub_id}/resume", cookies=cookies)
    assert resp.json()["is_active"] is True


@pytest.mark.asyncio
async def test_delete(client, web_user, session):
    _user, cookies = web_user
    created = (
        await client.post("/api/v1/subscriptions", json=VALID, cookies=cookies)
    ).json()

    resp = await client.delete(
        f"/api/v1/subscriptions/{created['id']}", cookies=cookies
    )
    assert resp.status_code == 200
    subs = (await session.execute(select(Subscription))).scalars().all()
    assert subs == []


@pytest.mark.asyncio
async def test_cross_user_404(client, web_user, session):
    from gosha.models import User
    from tests.api.conftest import session_cookie

    user, _cookies = web_user
    sub = Subscription(user_id=user.id)
    sub.keywords = ["x"]
    sub.locations = ["y"]
    session.add(sub)
    other = User(discord_user_id=31337)
    session.add(other)
    await session.commit()

    resp = await client.delete(
        f"/api/v1/subscriptions/{sub.id}", cookies=session_cookie(other.id)
    )
    assert resp.status_code == 404


async def _enforce_pre_migration_delivery_fk(engine) -> None:
    """user_jobs as production has it until ON DELETE SET NULL is migrated
    in: a plain reference, which the delete itself has to clear."""
    async with engine.connect() as conn:
        ddl = (await conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'user_jobs'"
        )).scalar_one()
        await conn.exec_driver_sql("DROP TABLE user_jobs")
        await conn.exec_driver_sql(ddl.replace(" ON DELETE SET NULL", ""))
        await conn.commit()


@pytest.mark.skipif(
    ON_POSTGRES,
    reason="rebuilds user_jobs from sqlite_master; tests/api/test_postgres_api.py "
    "has the Postgres version",
)
@pytest.mark.asyncio
async def test_delete_a_search_that_delivered_a_dm(client, web_user, session, engine):
    """user_jobs.subscription_id references the search: Postgres refused the
    delete (a 500) for any search that had ever delivered a DM."""
    from gosha.models import Job, UserJob

    user, cookies = web_user
    await _enforce_pre_migration_delivery_fk(engine)
    sid = (await client.post("/api/v1/subscriptions", json=VALID, cookies=cookies)).json()["id"]
    job = Job(url="https://j.com/dm", title="Dev", company="Acme", source="ejobs")
    session.add(job)
    await session.flush()
    session.add(UserJob(user_id=user.id, job_id=job.id, subscription_id=sid))
    await session.commit()

    resp = await client.delete(f"/api/v1/subscriptions/{sid}", cookies=cookies)

    assert resp.status_code == 200
    session.expire_all()
    assert await session.get(Subscription, sid) is None
    delivery = (await session.execute(select(UserJob))).scalar_one()
    assert delivery.subscription_id is None  # the history itself stays


@pytest.mark.asyncio
async def test_patch_null_clears_name_and_salary(client, web_user):
    """The SPA sends null for an emptied box; absent fields stay as they are."""
    _user, cookies = web_user
    created = await client.post(
        "/api/v1/subscriptions",
        json=dict(VALID, salary_min=5000),
        cookies=cookies,
    )
    sid = created.json()["id"]

    kept = await client.patch(
        f"/api/v1/subscriptions/{sid}", json={"remote_ok": True}, cookies=cookies,
    )
    assert kept.json()["name"] == "Cluj internships"
    assert kept.json()["salary_min"] == 5000

    cleared = await client.patch(
        f"/api/v1/subscriptions/{sid}",
        json={"name": None, "salary_min": None},
        cookies=cookies,
    )
    assert cleared.status_code == 200
    assert cleared.json()["name"] is None
    assert cleared.json()["salary_min"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    {"name": "n" * 129},  # String(128)
    {"salary_min": 2**31},  # INTEGER
    {"salary_min": -1},
])
async def test_out_of_range_fields_are_422_not_500(client, web_user, override):
    _user, cookies = web_user
    resp = await client.post(
        "/api/v1/subscriptions", json=dict(VALID, **override), cookies=cookies,
    )
    assert resp.status_code == 422
    sid = (await client.post("/api/v1/subscriptions", json=VALID, cookies=cookies)).json()["id"]
    resp = await client.patch(
        f"/api/v1/subscriptions/{sid}", json=override, cookies=cookies,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("method,suffix", [
    ("patch", ""), ("delete", ""), ("post", "/pause"), ("post", "/resume"),
])
async def test_ids_beyond_int32_are_422_not_500(client, web_user, method, suffix):
    _user, cookies = web_user
    kwargs = {"json": {"name": "x"}} if method == "patch" else {}
    resp = await getattr(client, method)(
        f"/api/v1/subscriptions/{2**31}{suffix}", cookies=cookies, **kwargs,
    )
    assert resp.status_code == 422
