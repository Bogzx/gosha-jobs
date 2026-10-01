"""The public demo feed: real ranking, sample CVs, no sign-in, no user data."""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import func, select

from gosha.api import demo
from gosha.demo_personas import DEMO_PERSONAS
from gosha.embeddings import EMBEDDING_DIM, vec_to_bytes
from gosha.events import Event
from gosha.models import Job, User, UserJob


def unit_vec(axis: int) -> np.ndarray:
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    v[axis] = 1.0
    return v


@pytest.fixture(autouse=True)
def fresh_caches(monkeypatch):
    monkeypatch.setattr(demo, "_vectors", {})
    monkeypatch.setattr(demo, "_candidates", None)


@pytest.fixture
def persona_vectors(monkeypatch):
    """Persona i embeds onto axis i, so each persona has a known best job."""
    import gosha.embeddings as emb

    axis = {p.cv: i for i, p in enumerate(DEMO_PERSONAS)}
    calls = []

    def fake_embed(text, encode=None):
        calls.append(text)
        return unit_vec(axis[text])

    monkeypatch.setattr(emb, "embed_long_text", fake_embed)
    return calls


@pytest.fixture
async def jobs(session):
    rows = [
        Job(url=f"https://d.test/{i}", title=title, company="Acme", source="ejobs",
            description=f"{title} role", embedding=vec_to_bytes(unit_vec(i)))
        for i, title in enumerate(["Python Developer", "Frontend Developer", "Data Analyst"])
    ]
    session.add_all(rows)
    await session.commit()
    return rows


@pytest.mark.asyncio
async def test_personas_are_listed_without_sign_in(client):
    resp = await client.get("/api/v1/demo/personas")
    assert resp.status_code == 200
    ids = [p["id"] for p in resp.json()]
    assert ids == [p.id for p in DEMO_PERSONAS]
    assert all(p["cv"] for p in resp.json())


@pytest.mark.asyncio
async def test_demo_feed_ranks_for_the_chosen_persona(client, jobs, persona_vectors):
    resp = await client.get("/api/v1/demo/feed", params={"persona": DEMO_PERSONAS[1].id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["persona"]["id"] == DEMO_PERSONAS[1].id
    assert body["total"] == 3
    top = body["items"][0]
    assert top["title"] == "Frontend Developer"
    assert top["match_percentile"] == 100
    assert any(s["kind"] == "rank" for s in top["match_signals"])


@pytest.mark.asyncio
async def test_persona_vectors_are_computed_once(client, jobs, persona_vectors):
    for _ in range(3):
        await client.get("/api/v1/demo/feed", params={"persona": DEMO_PERSONAS[0].id})
    assert persona_vectors == [DEMO_PERSONAS[0].cv]


@pytest.mark.asyncio
async def test_unknown_persona_is_404_and_free_text_is_refused(client, persona_vectors):
    resp = await client.get("/api/v1/demo/feed", params={"persona": "nobody"})
    assert resp.status_code == 404
    long_text = "I am a CV " * 20
    resp = await client.get("/api/v1/demo/feed", params={"persona": long_text})
    assert resp.status_code == 422
    assert persona_vectors == []  # the model never saw visitor input


@pytest.mark.asyncio
async def test_demo_writes_no_user_data(client, jobs, persona_vectors, session):
    async def counts():
        return [
            (await session.execute(select(func.count()).select_from(model))).scalar_one()
            for model in (User, UserJob, Event)
        ]

    before = await counts()
    await client.get("/api/v1/demo/feed", params={"persona": DEMO_PERSONAS[2].id})
    assert await counts() == before


@pytest.mark.asyncio
async def test_demo_is_rate_limited(client, jobs, persona_vectors, monkeypatch):
    from gosha import ratelimit

    monkeypatch.setattr(ratelimit, "DEMO", ratelimit.Limit("demo-test", 2, 60))
    params = {"persona": DEMO_PERSONAS[0].id}
    assert (await client.get("/api/v1/demo/feed", params=params)).status_code == 200
    assert (await client.get("/api/v1/demo/feed", params=params)).status_code == 200
    assert (await client.get("/api/v1/demo/feed", params=params)).status_code == 429


@pytest.mark.asyncio
async def test_demo_reports_a_missing_model(client, jobs, monkeypatch):
    import gosha.embeddings as emb

    monkeypatch.setattr(emb, "embed_long_text", lambda text, encode=None: None)
    resp = await client.get("/api/v1/demo/feed", params={"persona": DEMO_PERSONAS[0].id})
    assert resp.status_code == 503
