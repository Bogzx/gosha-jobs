"""The bot's /upload_cv and /delete_cv go through the same CV service as
the website: consent first, embedding on upload, full cleanup on delete.

Before, /upload_cv wrote the file and nothing else (no consent, no
embedding — Discord-uploaded CVs never reached the ranking) and /delete_cv
removed only the file, leaving the CV vector and the cover letters that
quote the CV behind.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import gosha.cover_letter as cv_storage
from gosha import embeddings
from gosha.bot import SubscriptionCog
from gosha.models import CoverLetter, Job, User

CV_TEXT = b"Python developer with Django, PostgreSQL and Kubernetes experience."
DISCORD_ID = 777000111


@pytest.fixture(autouse=True)
def cv_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cv_storage, "CV_DIR", tmp_path / "cvs")
    return tmp_path / "cvs"


@pytest.fixture(autouse=True)
def fake_encoder(monkeypatch):
    monkeypatch.setattr(
        embeddings, "encode_texts",
        lambda texts: np.full(
            (len(texts), embeddings.EMBEDDING_DIM), 0.5, dtype=np.float32,
        ),
    )


class FakeResponse:
    def __init__(self, sent: list[str]) -> None:
        self._sent = sent
        self._done = False

    async def send_message(self, content: str = "", **kwargs) -> None:
        self._sent.append(content)
        self._done = True

    async def defer(self, **kwargs) -> None:
        self._done = True

    def is_done(self) -> bool:
        return self._done


class FakeInteraction:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.user = SimpleNamespace(id=DISCORD_ID)
        self.response = FakeResponse(self.sent)

        async def followup_send(content: str = "", **kwargs) -> None:
            self.sent.append(content)

        self.followup = SimpleNamespace(send=followup_send)


class FakeAttachment:
    filename = "cv.txt"
    size = len(CV_TEXT)

    async def read(self) -> bytes:
        return CV_TEXT


def make_cog() -> SubscriptionCog:
    return SubscriptionCog(SimpleNamespace(settings=None))  # type: ignore[arg-type]


async def upload(consent: bool | None) -> FakeInteraction:
    cog = make_cog()
    interaction = FakeInteraction()
    await cog.upload_cv.callback(cog, interaction, FakeAttachment(), consent)
    return interaction


async def get_user(session: AsyncSession) -> User | None:
    session.expire_all()
    return (await session.execute(
        select(User).where(User.discord_user_id == DISCORD_ID)
    )).scalar_one_or_none()


async def test_upload_without_consent_asks_and_stores_nothing(
    patched_db, session: AsyncSession, cv_dir,
):
    interaction = await upload(consent=None)

    assert "consent: True" in interaction.sent[-1]
    user = await get_user(session)
    assert user is not None and user.cv_consent_at is None
    assert not (cv_dir / f"{user.id}.txt").exists()


async def test_upload_with_consent_stores_embeds_and_records_consent(
    patched_db, session: AsyncSession, cv_dir,
):
    interaction = await upload(consent=True)

    assert interaction.sent[-1].startswith("CV uploaded!")
    user = await get_user(session)
    assert user.cv_consent_at is not None
    assert user.cv_embedding is not None  # reaches the ranking now
    assert (cv_dir / f"{user.id}.txt").exists()

    # Consent is on record: the next upload needs no flag.
    again = await upload(consent=None)
    assert again.sent[-1].startswith("CV uploaded!")


async def test_delete_removes_vector_letters_and_consent(
    patched_db, session: AsyncSession, cv_dir,
):
    await upload(consent=True)
    user = await get_user(session)
    job = Job(url="https://j.test/1", title="Dev", company="Acme", source="ejobs")
    session.add(job)
    await session.flush()
    session.add(CoverLetter(user_id=user.id, job_id=job.id, content="Dear Acme"))
    await session.commit()

    cog = make_cog()
    interaction = FakeInteraction()
    await cog.delete_cv_cmd.callback(cog, interaction)

    assert interaction.sent[-1].startswith("CV deleted")
    user = await get_user(session)
    assert user.cv_embedding is None
    assert user.cv_consent_at is None
    assert not (cv_dir / f"{user.id}.txt").exists()
    letters = await session.execute(
        select(CoverLetter).where(CoverLetter.user_id == user.id)
    )
    assert letters.scalars().all() == []
