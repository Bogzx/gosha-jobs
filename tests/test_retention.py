"""CV retention (gosha/services/retention.py)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import gosha.cover_letter as storage
from gosha.embeddings import EMBEDDING_DIM, vec_to_bytes
from gosha.events import Event
from gosha.models import CoverLetter, Job, User, UserJob
from gosha.services import retention
from gosha.services.retention import purge_stale_cvs, retention_months

NOW = datetime(2027, 6, 1, tzinfo=timezone.utc)
LONG_AGO = NOW - timedelta(days=500)
RECENTLY = NOW - timedelta(days=10)
VEC = vec_to_bytes(np.ones(EMBEDDING_DIM, dtype=np.float32))


@pytest.fixture(autouse=True)
def cv_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "CV_DIR", tmp_path / "cvs")
    return tmp_path / "cvs"


def store_cv(uid: int, uploaded: datetime) -> None:
    path = storage.save_cv(uid, f"CV of user {uid}")
    ts = uploaded.timestamp()
    os.utime(path, (ts, ts))


async def make_user(session: AsyncSession, discord_id: int, **kwargs) -> User:
    user = User(
        discord_user_id=discord_id, created_at=LONG_AGO, cv_embedding=VEC, **kwargs,
    )
    session.add(user)
    await session.commit()
    store_cv(user.id, LONG_AGO)
    return user


@pytest.fixture
async def population(patched_db, session: AsyncSession, cv_dir):
    job = Job(url="https://j.test/1", title="Dev", company="Acme", source="ejobs")
    session.add(job)
    await session.commit()

    signed_in = await make_user(session, 1, last_login_at=RECENTLY)
    stale = await make_user(session, 2)
    session.add(CoverLetter(
        user_id=stale.id, job_id=job.id, content="Dear Acme", created_at=LONG_AGO,
    ))
    gave_feedback = await make_user(session, 3)
    session.add(UserJob(
        user_id=gave_feedback.id, job_id=job.id, delivered_at=LONG_AGO,
        feedback="interested", feedback_at=RECENTLY,
    ))
    only_dmed = await make_user(session, 4)
    session.add(Event(
        event_type="job.delivered", actor_id=only_dmed.id, timestamp=RECENTLY,
    ))
    reuploaded = await make_user(session, 5)
    store_cv(reuploaded.id, RECENTLY)
    browsed = await make_user(session, 6)
    session.add(Event(event_type="web.pageview", actor_id=browsed.id, timestamp=RECENTLY))
    await session.commit()

    store_cv(9999, RECENTLY)  # orphan: no user row
    return {
        "signed_in": signed_in.id, "stale": stale.id,
        "gave_feedback": gave_feedback.id, "only_dmed": only_dmed.id,
        "reuploaded": reuploaded.id, "browsed": browsed.id, "orphan": 9999,
    }


async def test_dry_run_reports_and_deletes_nothing(population, cv_dir):
    report = await purge_stale_cvs(months=12, dry_run=True, now=NOW)

    expired = {uid for uid, _ in report.expired}
    assert expired == {population["stale"], population["only_dmed"], population["orphan"]}
    assert report.deleted == 0
    assert report.checked == 7
    assert len(list(cv_dir.iterdir())) == 7
    assert "would delete 3" in report.summary()


async def test_purge_deletes_cv_vector_and_letters_of_inactive_users(
    population, session: AsyncSession, cv_dir,
):
    report = await purge_stale_cvs(months=12, now=NOW)

    assert report.deleted == 3
    for key in ("stale", "only_dmed", "orphan"):
        assert storage.load_cv(population[key]) is None, key
    for key in ("signed_in", "gave_feedback", "reuploaded", "browsed"):
        assert storage.load_cv(population[key]) is not None, key

    session.expire_all()
    stale = await session.get(User, population["stale"])
    assert stale is not None, "retention removes CV data, not the account"
    assert stale.cv_embedding is None
    letters = await session.execute(
        select(CoverLetter).where(CoverLetter.user_id == population["stale"])
    )
    assert letters.scalars().all() == []
    kept = await session.get(User, population["signed_in"])
    assert kept.cv_embedding is not None


async def test_zero_months_disables_retention(population, cv_dir):
    report = await purge_stale_cvs(months=0, now=NOW)
    assert report.cutoff is None and report.expired == []
    assert len(list(cv_dir.iterdir())) == 7


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 12), ("18", 18), ("0", 0), ("-3", 0), ("soon", 12)],
)
def test_retention_months_from_env(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(retention.RETENTION_ENV, raising=False)
    else:
        monkeypatch.setenv(retention.RETENTION_ENV, raw)
    assert retention_months() == expected
