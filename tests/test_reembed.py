"""Re-embedding with a new model (gosha/reembed.py) and stale-vector safety."""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import gosha.cover_letter as storage
from gosha import embeddings, matching
from gosha.embeddings import EMBEDDING_DIM, bytes_to_vec, vec_to_bytes
from gosha.models import Job, User
from gosha.reembed import count_pending, reembed

NEW = "paraphrase-multilingual-mpnet-base-v2"


def axis(i: int) -> np.ndarray:
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    v[i] = 1.0
    return v


# The "legacy model" writes axis 0, the "new model" writes axis 1.
LEGACY_VEC, NEW_VEC = axis(0), axis(1)


@pytest.fixture(autouse=True)
def cv_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "CV_DIR", tmp_path / "cvs")


@pytest.fixture
def encoders(monkeypatch):
    calls: list[tuple[str, int]] = []

    def encode_with_model(texts, model_name):
        calls.append((model_name, len(texts)))
        vec = NEW_VEC if model_name == NEW else LEGACY_VEC
        return np.stack([vec for _ in texts])

    monkeypatch.setattr(embeddings, "encode_with_model", encode_with_model)
    monkeypatch.setattr(
        embeddings, "encode_texts",
        lambda texts: encode_with_model(texts, matching.DEFAULT_MODEL),
    )
    return calls


@pytest.fixture
async def corpus(patched_db, session: AsyncSession):
    def job(n: int, *, active=True, vec=True) -> Job:
        return Job(
            url=f"https://j.test/{n}", title=f"Dev {n}", company="Acme",
            source="ejobs", is_active=active,
            embedding=vec_to_bytes(LEGACY_VEC) if vec else None,
        )

    jobs = [
        job(1), job(2), job(3),
        job(4, active=False),            # closed but may carry feedback
        job(5, vec=False),               # never embedded
        job(6, active=False, vec=False),  # closed, never embedded: skip
    ]
    with_vec = User(discord_user_id=1, cv_embedding=vec_to_bytes(LEGACY_VEC))
    without_vec = User(discord_user_id=2)
    session.add_all([*jobs, with_vec, without_vec])
    await session.commit()
    storage.save_cv(with_vec.id, "Python developer. " * 20)
    storage.save_cv(without_vec.id, "Data engineer. " * 20)
    return {"jobs": jobs, "users": [with_vec, without_vec]}


async def test_dry_run_counts_and_changes_nothing(corpus, encoders):
    report = await reembed(NEW, dry_run=True)
    assert (report.jobs_pending, report.cvs_pending) == (5, 2)
    assert encoders == []
    assert "would be re-embedded" in report.summary()


async def test_reembeds_in_batches_then_has_nothing_left(
    corpus, encoders, session: AsyncSession,
):
    report = await reembed(NEW, batch_size=2)

    assert (report.jobs_done, report.cvs_done, report.cvs_failed) == (5, 2, 0)
    assert [n for model, n in encoders if model == NEW][:3] == [2, 2, 1]
    session.expire_all()
    for job in (await session.execute(select(Job).order_by(Job.id))).scalars():
        if job.url.endswith("/6"):
            assert job.embedding is None  # closed and never embedded
            continue
        assert job.embedding_model == NEW
        np.testing.assert_array_equal(bytes_to_vec(job.embedding), NEW_VEC)
    for user in (await session.execute(select(User))).scalars():
        assert user.cv_embedding_model == NEW

    assert await count_pending(NEW) == (0, [])  # rerun is a no-op


async def test_interrupted_run_resumes_where_it_stopped(
    corpus, encoders, monkeypatch, session: AsyncSession,
):
    real = embeddings.encode_with_model
    batches = {"n": 0}

    def flaky(texts, model_name):
        batches["n"] += 1
        if batches["n"] == 2:
            raise KeyboardInterrupt  # operator hits ^C mid-run
        return real(texts, model_name)

    monkeypatch.setattr(embeddings, "encode_with_model", flaky)
    with pytest.raises(KeyboardInterrupt):
        await reembed(NEW, batch_size=2, cvs=False)
    assert (await count_pending(NEW))[0] == 3  # first batch committed

    monkeypatch.setattr(embeddings, "encode_with_model", real)
    report = await reembed(NEW, batch_size=2, cvs=False)
    assert report.jobs_done == 3
    assert (await count_pending(NEW))[0] == 0


async def test_after_a_switch_stale_vectors_are_not_mixed_in(
    corpus, encoders, monkeypatch, session: AsyncSession,
):
    """SEMANTIC_MODEL changed, re-embed not run yet: nothing compares
    new-model queries with old-model vectors."""
    from gosha.pipeline import _cv_vector_for
    from gosha.recommend import get_feed

    monkeypatch.setattr(matching, "DEFAULT_MODEL", NEW)
    user = corpus["users"][0]

    assert not embeddings.is_current(None)
    assert await _cv_vector_for(user) is None  # stale CV vector ignored
    items, total = await get_feed(user.id)
    assert all(item.score is None for item in items)  # no stale ranking

    # Delivery scoring re-encodes stale job vectors with the new model.
    scores = embeddings.score_jobs_against_query(NEW_VEC, corpus["jobs"][:2])
    assert scores == pytest.approx([1.0, 1.0])

    # The hourly backfill also picks stale active jobs up.
    first_id = corpus["jobs"][0].id
    assert await embeddings.embed_new_jobs() == 4
    session.expire_all()
    refreshed = await session.get(Job, first_id)
    assert refreshed.embedding_model == NEW


async def test_one_unreadable_cv_does_not_stop_the_run(
    corpus, encoders, monkeypatch,
):
    good, bad = corpus["users"]
    real_load = storage.load_cv

    def load(uid):
        if uid == bad.id:
            raise RuntimeError("corrupt")
        return real_load(uid)

    monkeypatch.setattr(storage, "load_cv", load)
    report = await reembed(NEW, jobs=False)
    assert (report.cvs_done, report.cvs_failed) == (1, 1)
