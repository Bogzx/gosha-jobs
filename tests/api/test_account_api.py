"""GDPR endpoints: export everything, erase everything."""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import select

import gosha.cover_letter as cl_mod
from gosha import embeddings
from gosha.events import Event
from gosha.models import Application, CoverLetter, Job, Subscription, User, UserJob


@pytest.fixture(autouse=True)
def fake_encoder(monkeypatch):
    monkeypatch.setattr(
        embeddings, "encode_texts",
        lambda texts: np.zeros(
            (len(texts), embeddings.EMBEDDING_DIM), dtype=np.float32
        ) + 0.5,
    )


async def _seed_full_account(client, session, user, cookies) -> Job:
    """Give the user one of everything the export/erase paths must cover."""
    await client.put(
        "/api/v1/cv",
        files={"file": ("cv.txt", b"Python, Kubernetes, Go", "text/plain")},
        cookies=cookies,
    )

    job = Job(url="https://j.com/1", title="Dev", company="Acme", source="ejobs")
    session.add(job)
    await session.flush()

    sub = Subscription(user_id=user.id, max_age_days=7, name="internships")
    sub.keywords = ["python"]
    sub.locations = ["cluj"]
    session.add(sub)

    session.add(UserJob(user_id=user.id, job_id=job.id, feedback="interested"))
    session.add(Application(user_id=user.id, job_id=job.id, status="applied"))
    session.add(CoverLetter(user_id=user.id, job_id=job.id, content="Dear Acme"))
    session.add(Event(event_type="web.signin", actor_id=user.id))
    await session.commit()
    return job


@pytest.mark.asyncio
async def test_export_returns_everything_we_hold(client, web_user, session):
    user, cookies = web_user
    await _seed_full_account(client, session, user, cookies)

    resp = await client.get("/api/v1/account/export", cookies=cookies)
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]

    body = resp.json()
    assert body["account"]["discord_user_id"] == str(user.discord_user_id)
    assert "Kubernetes" in body["cv_text"]
    assert [s["name"] for s in body["searches"]] == ["internships"]
    assert len(body["delivered_jobs"]) == 1
    assert body["delivered_jobs"][0]["feedback"] == "interested"
    assert len(body["applications"]) == 1
    assert body["cover_letters"][0]["content"] == "Dear Acme"
    assert [e["type"] for e in body["events"]] == ["web.signin"]


@pytest.mark.asyncio
async def test_export_requires_a_session(client):
    resp = await client.get("/api/v1/account/export")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_requires_explicit_confirmation(client, web_user, session):
    user, cookies = web_user
    resp = await client.delete("/api/v1/account", cookies=cookies)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "confirmation_required"

    still_there = await session.get(User, user.id)
    assert still_there is not None


@pytest.mark.asyncio
async def test_delete_account_cascades_through_everything(
    client, web_user, session, tmp_path,
):
    user, cookies = web_user
    user_id = user.id
    await _seed_full_account(client, session, user, cookies)

    cv_path = cl_mod.get_cv_path(user_id)
    assert cv_path.exists()

    resp = await client.delete(
        "/api/v1/account", params={"confirm": "DELETE"}, cookies=cookies
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    session.expire_all()
    assert await session.get(User, user_id) is None
    assert not cv_path.exists()

    for model, column in (
        (Subscription, Subscription.user_id),
        (UserJob, UserJob.user_id),
        (Application, Application.user_id),
        (CoverLetter, CoverLetter.user_id),
        (Event, Event.actor_id),
    ):
        rows = (
            await session.execute(select(model).where(column == user_id))
        ).scalars().all()
        assert rows == [], f"{model.__name__} rows survived account deletion"

    # Session cookie cleared, so the caller is logged out.
    assert "gosha_session=" in " ".join(resp.headers.get_list("set-cookie"))


@pytest.mark.asyncio
async def test_delete_account_leaves_public_job_rows_alone(
    client, web_user, session,
):
    """Job postings are public data, not the user's personal data."""
    user, cookies = web_user
    job = await _seed_full_account(client, session, user, cookies)
    job_id = job.id

    await client.delete(
        "/api/v1/account", params={"confirm": "DELETE"}, cookies=cookies
    )

    session.expire_all()
    assert await session.get(Job, job_id) is not None


@pytest.mark.asyncio
async def test_deleting_a_cv_also_deletes_its_cover_letters(
    client, web_user, session,
):
    """Letters are derived from the CV; "delete" has to include them."""
    user, cookies = web_user
    user_id = user.id
    await _seed_full_account(client, session, user, cookies)

    resp = await client.delete("/api/v1/cv", cookies=cookies)
    assert resp.status_code == 200

    session.expire_all()
    letters = (
        await session.execute(
            select(CoverLetter).where(CoverLetter.user_id == user_id)
        )
    ).scalars().all()
    assert letters == []


@pytest.mark.asyncio
async def test_privacy_notice_names_the_llm_processor(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    resp = await client.get("/api/v1/legal/privacy")
    assert resp.status_code == 200

    body = resp.json()
    processors = {p["name"] for p in body["third_party_processors"]}
    assert "OpenRouter" in processors
    assert "Discord" in processors
    assert "15,000 characters" in body["llm_disclosure"]
    assert "/api/v1/account/export" in body["your_rights"]["access_and_portability"]


@pytest.mark.asyncio
async def test_privacy_notice_follows_the_configured_provider(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    body = (await client.get("/api/v1/legal/privacy")).json()
    assert any(
        "Gemini" in p["name"] for p in body["third_party_processors"]
    )
