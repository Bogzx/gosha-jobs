"""Discord delivery reads the CV, not just the subscription string.

Geometry used throughout (unit vectors in the 768-dim model space):
subscription = e0, CV = e1.

    on_query   = e0                 fits the search, says nothing of the CV
    on_cv      = norm(0.3 e0 + e1)  weak on the words, strong on the CV
    query_only = norm(0.45 e0 + e2) just clears 0.40 on the words, off-CV
    unrelated  = e2
"""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gosha.embeddings import EMBEDDING_DIM, vec_to_bytes
from gosha.models import Job, Subscription, User, UserJob
from gosha.pipeline import (
    blend_with_user_vector,
    match_jobs_for_subscription,
    run_match_stage,
)


def unit(*weights: tuple[int, float]) -> np.ndarray:
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    for axis, weight in weights:
        v[axis] = weight
    return v / np.linalg.norm(v)


SUB = unit((0, 1.0))
CV = unit((1, 1.0))
VECTORS = {
    "on_query": unit((0, 1.0)),
    "on_cv": unit((0, 0.3), (1, 1.0)),
    "query_only": unit((0, 0.45), (2, 1.0)),
    "unrelated": unit((2, 1.0)),
}


class FakeMatcher:
    available = True
    threshold = 0.40

    def encode_subscription(self, keywords, locations, experience_levels=None):
        return SUB

    def is_match(self, score: float) -> bool:
        return score >= self.threshold


def make_job(name: str) -> Job:
    return Job(
        url=f"https://example.test/{name}",
        title="Software Engineer",
        company="Acme",
        location="Cluj-Napoca, Romania",
        source="ejobs",
        embedding=vec_to_bytes(VECTORS[name]),
    )


def make_sub() -> Subscription:
    sub = Subscription(user_id=1, max_age_days=7)
    sub.keywords = ["software engineer"]
    sub.locations = ["Cluj"]
    sub.excluded_keywords = []
    sub.company_blacklist = []
    sub.experience_levels = ["any"]
    return sub


def names(matches: list[tuple[Job, float]]) -> set[str]:
    return {job.url.rsplit("/", 1)[-1] for job, _ in matches}


async def test_without_a_cv_matching_is_unchanged():
    jobs = [make_job(n) for n in VECTORS]
    matches = await match_jobs_for_subscription(make_sub(), jobs, FakeMatcher())
    assert names(matches) == {"on_query", "query_only"}


async def test_with_a_cv_the_cv_decides_between_postings():
    jobs = [make_job(n) for n in VECTORS]
    matches = await match_jobs_for_subscription(
        make_sub(), jobs, FakeMatcher(), user_vector=CV,
    )
    # on_cv now reaches the user; query_only (keyword-adjacent, nothing to
    # do with their CV) no longer does. The subscription still anchors:
    # a posting that fits it exactly is kept.
    assert names(matches) == {"on_query", "on_cv"}


def test_blend_keeps_unit_norm_and_ignores_mismatched_models():
    blended = blend_with_user_vector(SUB, CV)
    assert np.linalg.norm(blended) == pytest.approx(1.0, abs=1e-6)
    assert blended @ SUB == pytest.approx(blended @ CV)

    other_model = np.ones(384, dtype=np.float32) / np.sqrt(384)
    np.testing.assert_array_equal(blend_with_user_vector(SUB, other_model), SUB)
    np.testing.assert_array_equal(blend_with_user_vector(SUB, None), SUB)


async def _seed(session: AsyncSession, *, with_cv: bool) -> tuple[User, list[Job]]:
    user = User(discord_user_id=4242, cv_embedding=vec_to_bytes(CV) if with_cv else None)
    session.add(user)
    await session.commit()
    sub = make_sub()
    sub.user_id = user.id
    session.add(sub)
    jobs = [make_job(n) for n in VECTORS]
    session.add_all(jobs)
    await session.commit()
    return user, jobs


async def _delivered(session: AsyncSession, user: User) -> set[str]:
    rows = await session.execute(
        select(Job.url).join(UserJob, UserJob.job_id == Job.id)
        .where(UserJob.user_id == user.id)
    )
    return {url.rsplit("/", 1)[-1] for (url,) in rows.all()}


async def test_match_stage_uses_the_stored_cv(patched_db, session: AsyncSession):
    user, jobs = await _seed(session, with_cv=True)

    await run_match_stage(jobs, FakeMatcher())

    assert await _delivered(session, user) == {"on_query", "on_cv"}


async def test_match_stage_without_cv_matches_on_subscription(
    patched_db, session: AsyncSession,
):
    user, jobs = await _seed(session, with_cv=False)

    await run_match_stage(jobs, FakeMatcher())

    assert await _delivered(session, user) == {"on_query", "query_only"}
