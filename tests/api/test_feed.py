"""Tests for the personalized feed endpoint."""

from __future__ import annotations

import numpy as np
import pytest

from gosha.embeddings import EMBEDDING_DIM, vec_to_bytes
from gosha.models import Job


def unit_vec(axis: int) -> np.ndarray:
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    v[axis] = 1.0
    return v


@pytest.mark.asyncio
async def test_feed_requires_auth(client):
    resp = await client.get("/api/v1/feed")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_feed_ranked_with_scores(client, web_user, session):
    user, cookies = web_user
    user.cv_embedding = vec_to_bytes(unit_vec(0))
    match = Job(
        url="https://f.com/match", title="Python Dev", company="A", source="indeed",
        embedding=vec_to_bytes(unit_vec(0)),
    )
    other = Job(
        url="https://f.com/other", title="Chef", company="B", source="indeed",
        embedding=vec_to_bytes(unit_vec(1)),
    )
    session.add_all([match, other])
    await session.commit()

    resp = await client.get("/api/v1/feed", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert [i["title"] for i in body["items"]] == ["Python Dev", "Chef"]
    assert body["items"][0]["match_score"] > 0.9
    assert isinstance(body["items"][0]["match_reasons"], list)


@pytest.mark.asyncio
async def test_feed_fallback_without_cv(client, web_user, session):
    _user, cookies = web_user
    session.add(Job(url="https://f.com/x", title="Anything", company="C", source="indeed"))
    await session.commit()

    resp = await client.get("/api/v1/feed", cookies=cookies)
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["match_score"] is None


@pytest.mark.asyncio
async def test_feed_says_when_active_searches_narrowed_it(client, web_user, session):
    """Scoped by default, with the count it hid; ?scope=all shows everything."""
    from gosha.models import Subscription

    user, cookies = web_user
    user.cv_embedding = vec_to_bytes(unit_vec(0))
    sub = Subscription(user_id=user.id, max_age_days=7)
    sub.keywords = ["developer"]
    sub.locations = ["Cluj"]
    sub.excluded_keywords = []
    sub.company_blacklist = []
    sub.experience_levels = ["any"]
    session.add_all([
        sub,
        Job(url="https://f.com/cluj", title="Python Dev", company="A", source="ejobs",
            location="Cluj-Napoca", embedding=vec_to_bytes(unit_vec(0))),
        Job(url="https://f.com/iasi", title="Python Dev", company="B", source="ejobs",
            location="Iasi", embedding=vec_to_bytes(unit_vec(0))),
    ])
    await session.commit()

    body = (await client.get("/api/v1/feed", cookies=cookies)).json()
    assert body["scoped"] is True and body["hidden_by_scope"] == 1
    assert [i["location"] for i in body["items"]] == ["Cluj-Napoca"]

    body = (await client.get("/api/v1/feed?scope=all", cookies=cookies)).json()
    assert body["scoped"] is False and body["hidden_by_scope"] == 0
    assert body["total"] == 2

    resp = await client.get("/api/v1/feed?scope=nonsense", cookies=cookies)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_feed_without_active_searches_is_not_scoped(client, web_user, session):
    _user, cookies = web_user
    session.add(Job(url="https://f.com/y", title="Anything", company="C", source="indeed"))
    await session.commit()
    body = (await client.get("/api/v1/feed", cookies=cookies)).json()
    assert body["scoped"] is False and body["hidden_by_scope"] == 0
