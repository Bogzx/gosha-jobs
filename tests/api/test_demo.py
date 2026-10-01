"""The public demo feed: real ranking, sample CVs, no sign-in, no user data."""

from __future__ import annotations

import asyncio
import threading
import time
import types

import numpy as np
import pytest
from sqlalchemy import func, select

from gosha import matching
from gosha.api import demo
from gosha.api.jobs import LIST_DESCRIPTION_CHARS
from gosha.demo_personas import DEMO_PERSONAS
from gosha.embeddings import EMBEDDING_DIM, vec_to_bytes
from gosha.events import Event
from gosha.models import Job, User, UserJob


def unit_vec(axis: int) -> np.ndarray:
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    v[axis] = 1.0
    return v


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(demo, "_vectors", None)
    monkeypatch.setattr(demo, "_candidates", None)
    monkeypatch.setattr(demo, "_responses", {})
    # asyncio locks bind to the first loop that waits on them; each test
    # has its own loop.
    monkeypatch.setattr(demo, "_vectors_lock", asyncio.Lock())
    monkeypatch.setattr(demo, "_candidates_lock", asyncio.Lock())


@pytest.fixture
def persona_vectors(monkeypatch):
    """Persona i embeds onto axis i. Slow on purpose, so overlapping first
    requests really do overlap. Returns the list of texts embedded."""
    import gosha.embeddings as emb

    axis = {p.cv: i for i, p in enumerate(DEMO_PERSONAS)}
    calls: list[str] = []

    def fake_embed(text, encode=None):
        calls.append(text)
        time.sleep(0.02)
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


def feed(client, persona=DEMO_PERSONAS[0].id):
    return client.get("/api/v1/demo/feed", params={"persona": persona})


@pytest.mark.asyncio
async def test_personas_are_listed_without_sign_in(client):
    resp = await client.get("/api/v1/demo/personas")
    assert resp.status_code == 200
    assert [p["id"] for p in resp.json()] == [p.id for p in DEMO_PERSONAS]
    assert all(p["cv"] for p in resp.json())


@pytest.mark.asyncio
async def test_demo_feed_ranks_for_the_chosen_persona(client, jobs, persona_vectors):
    resp = await feed(client, DEMO_PERSONAS[1].id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["persona"]["id"] == DEMO_PERSONAS[1].id
    assert body["total"] == 3
    top = body["items"][0]
    assert top["title"] == "Frontend Developer"
    assert top["match_percentile"] == 100
    assert any(s["kind"] == "rank" for s in top["match_signals"])


@pytest.mark.asyncio
async def test_a_burst_of_first_requests_embeds_each_persona_once(
    client, jobs, persona_vectors,
):
    """Right after a restart, parallel visitors must not each trigger the
    model: all persona vectors are built once, under the lock."""
    responses = await asyncio.gather(*(
        feed(client, DEMO_PERSONAS[i % len(DEMO_PERSONAS)].id) for i in range(8)
    ))
    assert all(r.status_code == 200 for r in responses)
    assert sorted(persona_vectors) == sorted(p.cv for p in DEMO_PERSONAS)


def test_concurrent_callers_load_the_model_once(monkeypatch):
    loads = []

    class SlowModel:
        def __init__(self, name):
            loads.append(name)
            time.sleep(0.05)

    monkeypatch.setitem(
        __import__("sys").modules, "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=SlowModel),
    )
    monkeypatch.setattr(matching, "_model", None)
    monkeypatch.setattr(matching, "_model_name", "")

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(matching._get_model("m")))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert loads == ["m"]
    assert len({id(r) for r in results}) == 1


@pytest.mark.asyncio
async def test_demo_shows_at_most_the_top_25_with_list_descriptions(
    client, persona_vectors, session,
):
    long = "Python " * 200
    session.add_all([
        Job(url=f"https://d.test/many{i}", title=f"Job {i}", company="Acme",
            source="ejobs", description=long, embedding=vec_to_bytes(unit_vec(i % 3)))
        for i in range(30)
    ])
    await session.commit()

    body = (await feed(client)).json()
    assert body["total"] == 30
    assert len(body["items"]) == demo.DEMO_LIMIT == 25
    assert all(len(i["description"]) <= LIST_DESCRIPTION_CHARS + 1 for i in body["items"])


@pytest.mark.asyncio
async def test_responses_are_cached_per_persona_and_ranked_off_the_loop(
    client, jobs, persona_vectors, monkeypatch,
):
    import gosha.recommend as recommend

    real = recommend.rank_candidates
    on_loop_thread = []

    def spy(*args, **kwargs):
        on_loop_thread.append(threading.current_thread() is threading.main_thread())
        return real(*args, **kwargs)

    monkeypatch.setattr(recommend, "rank_candidates", spy)

    for _ in range(3):
        assert (await feed(client)).status_code == 200
    assert on_loop_thread == [False]  # once, in a worker thread

    await feed(client, DEMO_PERSONAS[1].id)
    assert len(on_loop_thread) == 2  # another persona, another ranking

    monkeypatch.setattr(demo, "_responses", {
        key: (0.0, value) for key, (_, value) in demo._responses.items()
    })  # expire everything
    await feed(client)
    assert len(on_loop_thread) == 3


@pytest.mark.asyncio
async def test_unknown_persona_is_404_and_free_text_is_refused(client, persona_vectors):
    assert (await feed(client, "nobody")).status_code == 404
    assert (await feed(client, "I am a CV " * 20)).status_code == 422
    assert persona_vectors == []  # the model never saw visitor input


@pytest.mark.asyncio
async def test_demo_writes_no_user_data(client, jobs, persona_vectors, session):
    async def counts():
        return [
            (await session.execute(select(func.count()).select_from(model))).scalar_one()
            for model in (User, UserJob, Event)
        ]

    before = await counts()
    await feed(client, DEMO_PERSONAS[2].id)
    assert await counts() == before


@pytest.mark.asyncio
async def test_demo_is_rate_limited_even_when_cached(client, jobs, persona_vectors, monkeypatch):
    from gosha import ratelimit

    monkeypatch.setattr(ratelimit, "DEMO", ratelimit.Limit("demo-test", 2, 60))
    assert (await feed(client)).status_code == 200
    assert (await feed(client)).status_code == 200
    assert (await feed(client)).status_code == 429


@pytest.mark.asyncio
async def test_demo_reports_a_missing_model_and_retries_later(client, jobs, monkeypatch):
    import gosha.embeddings as emb

    monkeypatch.setattr(emb, "embed_long_text", lambda text, encode=None: None)
    assert (await feed(client)).status_code == 503
    assert demo._vectors is None  # nothing cached; the next request tries again
