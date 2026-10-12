"""API behaviour only Postgres shows (opt-in, like tests/test_database_pg.py).

Set GOSHA_TEST_POSTGRES_URL to an empty scratch database, e.g.
postgresql+asyncpg://postgres:pw@127.0.0.1:5432/gosha_test — every table in
it is dropped and recreated per test. SQLite cannot reproduce what these
cover: 32-bit INTEGER columns, row locks, and requests that really overlap.
"""

from __future__ import annotations

import asyncio
import os

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

import gosha.cover_letter as cl_mod
import gosha.events  # noqa: F401 — registers Event with Base
from gosha import embeddings
from gosha.models import Application, Base, CoverLetter, Job, Subscription, UserJob

PG_URL = os.getenv("GOSHA_TEST_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(not PG_URL, reason="GOSHA_TEST_POSTGRES_URL not set")

SEARCH = {"keywords": ["python"], "locations": ["cluj"]}


@pytest_asyncio.fixture
async def engine():
    """Replaces the SQLite engine, so `client` and `session` run on Postgres."""
    eng = create_async_engine(PG_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
def fake_encoder(monkeypatch):
    monkeypatch.setattr(
        embeddings, "encode_texts",
        lambda texts: np.full((len(texts), embeddings.EMBEDDING_DIM), 0.5, dtype=np.float32),
    )


async def _jobs(session, n: int, prefix: str = "j") -> list[int]:
    jobs = [
        Job(url=f"https://pg.test/{prefix}{i}", title="Dev", company="Acme",
            location="Cluj", source="ejobs")
        for i in range(n)
    ]
    session.add_all(jobs)
    await session.commit()
    return [job.id for job in jobs]


@pytest.mark.asyncio
async def test_delete_a_search_that_delivered_a_dm(client, web_user, session):
    user, cookies = web_user
    # The reference as production has it until ON DELETE SET NULL is migrated in.
    await session.execute(text(
        "ALTER TABLE user_jobs DROP CONSTRAINT user_jobs_subscription_id_fkey"
    ))
    await session.execute(text(
        "ALTER TABLE user_jobs ADD CONSTRAINT user_jobs_subscription_id_fkey "
        "FOREIGN KEY (subscription_id) REFERENCES subscriptions (id)"
    ))
    await session.commit()
    sid = (await client.post("/api/v1/subscriptions", json=SEARCH, cookies=cookies)).json()["id"]
    [job_id] = await _jobs(session, 1)
    session.add(UserJob(user_id=user.id, job_id=job_id, subscription_id=sid))
    await session.commit()

    resp = await client.delete(f"/api/v1/subscriptions/{sid}", cookies=cookies)

    assert resp.status_code == 200
    session.expire_all()
    assert (await session.execute(select(UserJob.subscription_id))).scalar_one() is None


@pytest.mark.asyncio
async def test_out_of_range_input_is_422_not_500(client, web_user):
    _user, cookies = web_user
    huge = 99_999_999_999
    checks = [
        await client.post("/api/v1/subscriptions", json=dict(SEARCH, name="n" * 200), cookies=cookies),
        await client.post("/api/v1/subscriptions", json=dict(SEARCH, salary_min=3_000_000_000), cookies=cookies),
        await client.get(f"/api/v1/jobs/{huge}", cookies=cookies),
        await client.get("/api/v1/jobs", params={"salary_min": huge}, cookies=cookies),
        await client.patch(f"/api/v1/applications/{huge}", json={"notes": "x"}, cookies=cookies),
        await client.post("/api/v1/applications", json={"job_id": huge}, cookies=cookies),
        await client.delete(f"/api/v1/subscriptions/{huge}", cookies=cookies),
        await client.post(f"/api/v1/jobs/{huge}/cover-letter", cookies=cookies),
    ]
    assert [r.status_code for r in checks] == [422] * len(checks)


@pytest.mark.asyncio
async def test_double_submits_are_idempotent(client, web_user, session):
    user, cookies = web_user
    job_ids = await _jobs(session, 5)
    for job_id in job_ids:
        for path, body in (
            (f"/api/v1/jobs/{job_id}/apply-click", None),
            (f"/api/v1/jobs/{job_id}/feedback", {"feedback": "interested"}),
            ("/api/v1/applications", {"job_id": job_id}),
        ):
            pair = await asyncio.gather(*(
                client.post(path, json=body, cookies=cookies) for _ in range(2)
            ))
            assert [r.status_code for r in pair] == [200, 200], path

    session.expire_all()
    assert (await session.execute(select(func.count(Application.id)))).scalar() == 5
    assert (await session.execute(select(func.count(UserJob.id)))).scalar() == 5


@pytest.mark.asyncio
async def test_concurrent_searches_cannot_exceed_the_plan(client, web_user, session):
    user, cookies = web_user  # free: 5 searches
    results = await asyncio.gather(*(
        client.post("/api/v1/subscriptions", json=dict(SEARCH, name=f"s{i}"), cookies=cookies)
        for i in range(10)
    ))
    assert sorted(r.status_code for r in results) == [200] * 5 + [403] * 5
    count = (await session.execute(
        select(func.count(Subscription.id)).where(Subscription.user_id == user.id)
    )).scalar()
    assert count == 5


def _slow_llm(monkeypatch) -> dict:
    from gosha import llm

    calls = {"n": 0}

    async def slow_generate(prompt: str) -> str:
        calls["n"] += 1
        await asyncio.sleep(0.3)  # a real call takes seconds
        return "Dear team, ..."

    monkeypatch.setattr(llm, "generate", slow_generate)
    return calls


@pytest.mark.asyncio
async def test_cover_letter_quota_holds_under_concurrency(
    client, web_user, session, monkeypatch,
):
    """12 requests for 12 jobs at once on a 5-a-month plan: 5 letters, not 12."""
    user, cookies = web_user
    cl_mod.save_cv(user.id, "Python developer with Django experience")
    job_ids = await _jobs(session, 12)
    calls = _slow_llm(monkeypatch)

    results = await asyncio.gather(*(
        client.post(f"/api/v1/jobs/{job_id}/cover-letter", cookies=cookies)
        for job_id in job_ids
    ))

    assert sorted(r.status_code for r in results) == [200] * 5 + [403] * 7
    assert calls["n"] == 5
    rows = (await session.execute(
        select(CoverLetter.content).where(CoverLetter.user_id == user.id)
    )).scalars().all()
    assert rows == ["Dear team, ..."] * 5


@pytest.mark.asyncio
async def test_cover_letter_double_click_generates_once(
    client, web_user, session, monkeypatch,
):
    user, cookies = web_user
    cl_mod.save_cv(user.id, "Python developer")
    [job_id] = await _jobs(session, 1)
    calls = _slow_llm(monkeypatch)

    first, second = await asyncio.gather(*(
        client.post(f"/api/v1/jobs/{job_id}/cover-letter", cookies=cookies)
        for _ in range(2)
    ))

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    ok = first if first.status_code == 200 else second
    assert ok.json() == {"content": "Dear team, ...", "cached": False}
    assert calls["n"] == 1
    rows = (await session.execute(
        select(CoverLetter.content).where(CoverLetter.user_id == user.id)
    )).scalars().all()
    assert rows == ["Dear team, ..."]
    # And the pair is readable afterwards (duplicates made this a 500).
    again = await client.post(f"/api/v1/jobs/{job_id}/cover-letter", cookies=cookies)
    assert again.json() == {"content": "Dear team, ...", "cached": True}
