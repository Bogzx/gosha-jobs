"""Tests for CV upload/management and cover letter endpoints."""

from __future__ import annotations

import numpy as np
import pytest

import gosha.cover_letter as cl_mod
from gosha import embeddings
from gosha.models import CoverLetter, Job


@pytest.fixture(autouse=True)
def cv_dir(tmp_path, monkeypatch):
    """Store CV files in a temp dir during tests."""
    monkeypatch.setattr(cl_mod, "CV_DIR", tmp_path / "cvs")
    return tmp_path / "cvs"


@pytest.fixture(autouse=True)
def fake_encoder(monkeypatch):
    monkeypatch.setattr(
        embeddings, "encode_texts",
        lambda texts: np.zeros((len(texts), embeddings.EMBEDDING_DIM), dtype=np.float32) + 0.5,
    )


def _upload(client, cookies, filename="cv.txt", content=b"Python and React skills"):
    return client.put(
        "/api/v1/cv",
        files={"file": (filename, content, "text/plain")},
        cookies=cookies,
    )


@pytest.mark.asyncio
async def test_upload_get_delete_cv(client, web_user, session):
    user, cookies = web_user

    resp = await _upload(client, cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_cv"] is True
    assert "Python and React" in body["text"]

    # Embedding stored
    await session.refresh(user)
    assert user.cv_embedding is not None

    resp = await client.get("/api/v1/cv", cookies=cookies)
    assert resp.json()["has_cv"] is True
    assert "Python" in resp.json()["text"]

    resp = await client.delete("/api/v1/cv", cookies=cookies)
    assert resp.status_code == 200
    await session.refresh(user)
    assert user.cv_embedding is None

    resp = await client.get("/api/v1/cv", cookies=cookies)
    assert resp.json()["has_cv"] is False
    assert resp.json()["text"] is None


@pytest.mark.asyncio
async def test_upload_rejects_bad_extension(client, web_user):
    _user, cookies = web_user
    resp = await _upload(client, cookies, filename="cv.exe", content=b"MZ...")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_upload_rejects_oversize(client, web_user):
    _user, cookies = web_user
    resp = await _upload(client, cookies, content=b"x" * (5 * 1024 * 1024 + 1))
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "file_too_large"


@pytest.mark.asyncio
async def test_upload_is_read_in_chunks_and_aborted_early():
    """The oversize check must not materialise the whole body first.

    Reading it all and then measuring is a one-request OOM for any signed-in
    user; the guard has to stop at the cap.
    """
    from gosha.api.cv import read_capped
    from gosha.domain.errors import FileTooLargeError

    class CountingUpload:
        """Pretends to be an endless upload; counts what was consumed."""

        def __init__(self) -> None:
            self.consumed = 0

        async def read(self, size: int = -1) -> bytes:
            self.consumed += size
            return b"x" * size

    upload = CountingUpload()
    limit = 5 * 1024 * 1024
    with pytest.raises(FileTooLargeError):
        await read_capped(upload, limit)

    # Stopped within one chunk of the limit rather than reading forever.
    from gosha.api.cv import UPLOAD_CHUNK_BYTES

    assert upload.consumed <= limit + UPLOAD_CHUNK_BYTES


@pytest.mark.asyncio
async def test_cover_letter_generation_and_cache(client, web_user, session, monkeypatch):
    user, cookies = web_user
    await _upload(client, cookies)

    job = Job(url="https://c.com/1", title="Dev", company="Acme", source="indeed")
    session.add(job)
    await session.commit()

    async def fake_generate(prompt: str) -> str:
        return "Dear Acme, I am a great fit."

    from gosha import llm
    monkeypatch.setattr(llm, "generate", fake_generate)

    resp = await client.post(f"/api/v1/jobs/{job.id}/cover-letter", cookies=cookies)
    assert resp.status_code == 200
    assert resp.json() == {
        "content": "Dear Acme, I am a great fit.",
        "cached": False,
    }

    resp = await client.post(f"/api/v1/jobs/{job.id}/cover-letter", cookies=cookies)
    assert resp.json()["cached"] is True

    resp = await client.get("/api/v1/cover-letters", cookies=cookies)
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["job_title"] == "Dev"
    assert items[0]["company"] == "Acme"


@pytest.mark.asyncio
async def test_cover_letter_requires_cv(client, web_user, session):
    _user, cookies = web_user
    job = Job(url="https://c.com/2", title="Dev", company="B", source="indeed")
    session.add(job)
    await session.commit()

    resp = await client.post(f"/api/v1/jobs/{job.id}/cover-letter", cookies=cookies)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "no_cv"


@pytest.mark.asyncio
async def test_cover_letter_quota(client, web_user, session, monkeypatch):
    user, cookies = web_user
    await _upload(client, cookies)

    # Free tier: 5 letters/month. Seed 5 existing for other jobs.
    for i in range(5):
        j = Job(url=f"https://c.com/q{i}", title=f"J{i}", company="X", source="indeed")
        session.add(j)
        await session.flush()
        session.add(CoverLetter(user_id=user.id, job_id=j.id, content="..."))
    new_job = Job(url="https://c.com/new", title="New", company="Y", source="indeed")
    session.add(new_job)
    await session.commit()

    resp = await client.post(f"/api/v1/jobs/{new_job.id}/cover-letter", cookies=cookies)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "quota_exceeded"
