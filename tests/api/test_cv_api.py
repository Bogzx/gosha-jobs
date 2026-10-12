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


def _upload(
    client, cookies, filename="cv.txt", content=b"Python and React skills",
    consent: bool | None = True,
):
    return client.put(
        "/api/v1/cv",
        files={"file": (filename, content, "text/plain")},
        data={} if consent is None else {"consent": str(consent).lower()},
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


# ── consent ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_without_consent_is_refused_and_stores_nothing(
    client, web_user, session, cv_dir,
):
    user, cookies = web_user

    resp = await _upload(client, cookies, consent=None)

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "consent_required"
    assert not (cv_dir / f"{user.id}.txt").exists()
    await session.refresh(user)
    assert user.cv_consent_at is None
    assert user.cv_embedding is None


@pytest.mark.asyncio
async def test_consent_is_recorded_server_side_once(client, web_user, session):
    user, cookies = web_user

    assert (await _upload(client, cookies)).status_code == 200
    await session.refresh(user)
    first = user.cv_consent_at
    assert first is not None

    # A re-upload does not need the flag again and keeps the original time.
    assert (await _upload(client, cookies, consent=None)).status_code == 200
    await session.refresh(user)
    assert user.cv_consent_at == first


@pytest.mark.asyncio
async def test_deleting_the_cv_withdraws_consent(client, web_user, session):
    user, cookies = web_user
    assert (await _upload(client, cookies)).status_code == 200

    assert (await client.delete("/api/v1/cv", cookies=cookies)).status_code == 200
    await session.refresh(user)
    assert user.cv_consent_at is None

    resp = await _upload(client, cookies, consent=None)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "consent_required"


@pytest.mark.asyncio
async def test_cv_round_trip_is_encrypted_at_rest(client, web_user, cv_dir, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("CV_ENCRYPTION_KEY", Fernet.generate_key().decode())
    user, cookies = web_user

    assert (await _upload(client, cookies)).status_code == 200

    stored = cv_dir / f"{user.id}.enc"
    assert stored.exists() and not (cv_dir / f"{user.id}.txt").exists()
    assert b"React" not in stored.read_bytes()
    body = (await client.get("/api/v1/cv", cookies=cookies)).json()
    assert "Python and React" in body["text"]
    assert body["uploaded_at"]

    export = (await client.get("/api/v1/account/export", cookies=cookies)).json()
    assert "Python and React" in export["cv_text"]

    assert (await client.delete("/api/v1/cv", cookies=cookies)).status_code == 200
    assert not stored.exists()


@pytest.mark.asyncio
async def test_cv_upload_is_rate_limited_per_user(client, web_user, monkeypatch):
    from gosha import ratelimit

    monkeypatch.setattr(ratelimit, "CV_UPLOAD", ratelimit.Limit("cv-upload", 2, 3600))
    _user, cookies = web_user
    codes = [(await _upload(client, cookies)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


@pytest.mark.asyncio
async def test_wrong_cv_key_does_not_break_me(client, web_user, monkeypatch):
    """/me is the first call the SPA makes; with a wrong CV_ENCRYPTION_KEY it
    used to decrypt the CV just to say has_cv, and 500 for every CV holder."""
    from cryptography.fernet import Fernet

    from gosha import cv_crypto

    user, cookies = web_user
    monkeypatch.setenv(cv_crypto.KEY_ENV, Fernet.generate_key().decode())
    cl_mod.save_cv(user.id, "Python and React skills")
    monkeypatch.setenv(cv_crypto.KEY_ENV, Fernet.generate_key().decode())

    resp = await client.get("/api/v1/me", cookies=cookies)

    assert resp.status_code == 200
    assert resp.json()["has_cv"] is True


# ── cover-letter claims and the quota ledger ──────────────────────────


async def _job(session, url: str = "https://c.com/claim") -> Job:
    job = Job(url=url, title="Dev", company="Acme", source="indeed")
    session.add(job)
    await session.commit()
    return job


def _fake_llm(monkeypatch, reply: str = "Dear Acme, hire me.", during=None) -> dict:
    from gosha import llm

    calls = {"n": 0}

    async def fake_generate(prompt: str) -> str:
        calls["n"] += 1
        if during is not None:
            await during()
        return reply

    monkeypatch.setattr(llm, "generate", fake_generate)
    return calls


async def _letters(session, user_id: int) -> list[CoverLetter]:
    from sqlalchemy import select

    session.expire_all()
    return list((await session.execute(
        select(CoverLetter).where(CoverLetter.user_id == user_id).order_by(CoverLetter.id)
    )).scalars())


@pytest.mark.asyncio
async def test_letter_in_progress_is_409_never_the_placeholder(
    client, web_user, session, monkeypatch,
):
    """A second request while the first is generating used to get the claim
    row's "__generating__" back as a cached letter."""
    user, cookies = web_user
    user_id = user.id
    await _upload(client, cookies)
    job_id = (await _job(session)).id
    second: dict = {}

    async def second_click() -> None:
        resp = await client.post(f"/api/v1/jobs/{job_id}/cover-letter", cookies=cookies)
        second["status"], second["body"] = resp.status_code, resp.json()
        # Mid-generation the claim is the one row: kept, not deleted.
        rows = await _letters(session, user_id)
        second["rows"] = [r.content for r in rows]

    calls = _fake_llm(monkeypatch, during=second_click)
    first = await client.post(f"/api/v1/jobs/{job_id}/cover-letter", cookies=cookies)

    assert first.json() == {"content": "Dear Acme, hire me.", "cached": False}
    assert second["status"] == 409
    assert second["body"]["error"]["code"] == "generation_in_progress"
    assert second["rows"] == [cl_mod.PENDING_CONTENT]
    assert calls["n"] == 1
    # The letter landed in the claimed row: still one row for the pair.
    assert [r.content for r in await _letters(session, user_id)] == ["Dear Acme, hire me."]


@pytest.mark.asyncio
async def test_claim_left_by_a_dead_request_is_reclaimed(
    client, web_user, session, monkeypatch,
):
    from datetime import datetime, timedelta, timezone

    user, cookies = web_user
    await _upload(client, cookies)
    job = await _job(session)
    session.add(CoverLetter(
        user_id=user.id, job_id=job.id, content=cl_mod.PENDING_CONTENT,
        created_at=datetime.now(timezone.utc) - cl_mod.PENDING_TTL - timedelta(minutes=1),
    ))
    await session.commit()
    _fake_llm(monkeypatch)

    resp = await client.post(f"/api/v1/jobs/{job.id}/cover-letter", cookies=cookies)

    assert resp.json() == {"content": "Dear Acme, hire me.", "cached": False}
    # The dead claim's slot was handed back rather than charged twice.
    assert [r.content for r in await _letters(session, user.id)] == ["Dear Acme, hire me."]


@pytest.mark.asyncio
async def test_duplicate_rows_for_a_job_do_not_500(client, web_user, session, monkeypatch):
    """Races before claims were locked left two rows for one (user, job);
    scalar_one_or_none() made every later request for it a 500."""
    from datetime import datetime, timedelta, timezone

    user, cookies = web_user
    await _upload(client, cookies)
    job = await _job(session)
    now = datetime.now(timezone.utc)
    session.add_all([
        CoverLetter(user_id=user.id, job_id=job.id, content="older",
                    created_at=now - timedelta(days=2)),
        CoverLetter(user_id=user.id, job_id=job.id, content="newer",
                    created_at=now - timedelta(days=1)),
    ])
    await session.commit()
    calls = _fake_llm(monkeypatch)

    resp = await client.post(f"/api/v1/jobs/{job.id}/cover-letter", cookies=cookies)

    assert resp.status_code == 200
    assert resp.json() == {"content": "newer", "cached": True}
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_regenerating_an_expired_letter_needs_quota(
    client, web_user, session, monkeypatch,
):
    """Letters past 30 days are regenerated — that is a new letter and
    used to skip the quota check entirely."""
    from datetime import datetime, timedelta, timezone

    user, cookies = web_user
    await _upload(client, cookies)
    stale_job = await _job(session, "https://c.com/stale")
    session.add(CoverLetter(
        user_id=user.id, job_id=stale_job.id, content="Old letter",
        created_at=datetime.now(timezone.utc) - timedelta(days=40),
    ))
    for i in range(5):  # free tier: 5 a month, all used
        other = await _job(session, f"https://c.com/used{i}")
        session.add(CoverLetter(user_id=user.id, job_id=other.id, content="..."))
    await session.commit()
    calls = _fake_llm(monkeypatch)

    resp = await client.post(f"/api/v1/jobs/{stale_job.id}/cover-letter", cookies=cookies)

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "quota_exceeded"
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_regenerated_letter_replaces_the_expired_one(
    client, web_user, session, monkeypatch,
):
    from datetime import datetime, timedelta, timezone

    user, cookies = web_user
    await _upload(client, cookies)
    job = await _job(session)
    session.add(CoverLetter(
        user_id=user.id, job_id=job.id, content="Old letter",
        created_at=datetime.now(timezone.utc) - timedelta(days=40),
    ))
    await session.commit()
    _fake_llm(monkeypatch)

    resp = await client.post(f"/api/v1/jobs/{job.id}/cover-letter", cookies=cookies)

    assert resp.json() == {"content": "Dear Acme, hire me.", "cached": False}
    # The old row stays as last month's quota record, without its text.
    assert [r.content for r in await _letters(session, user.id)] == ["", "Dear Acme, hire me."]
    listed = (await client.get("/api/v1/cover-letters", cookies=cookies)).json()["items"]
    assert [item["content"] for item in listed] == ["Dear Acme, hire me."]


@pytest.mark.asyncio
async def test_deleting_the_cv_does_not_reset_the_monthly_quota(
    client, web_user, session, monkeypatch,
):
    """The letters are the quota counter; deleting them with the CV gave a
    fresh five: delete, re-upload, generate, repeat."""
    from datetime import datetime, timedelta, timezone

    user, cookies = web_user
    await _upload(client, cookies)
    last_month = await _job(session, "https://c.com/last-month")
    session.add(CoverLetter(
        user_id=user.id, job_id=last_month.id, content="Old",
        created_at=datetime.now(timezone.utc) - timedelta(days=40),
    ))
    for i in range(5):
        other = await _job(session, f"https://c.com/used{i}")
        session.add(CoverLetter(user_id=user.id, job_id=other.id, content=f"Letter {i}"))
    await session.commit()

    assert (await client.delete("/api/v1/cv", cookies=cookies)).status_code == 200

    # Text gone everywhere; this month's rows stay, last month's are deleted.
    assert [r.content for r in await _letters(session, user.id)] == [""] * 5
    assert (await client.get("/api/v1/cover-letters", cookies=cookies)).json()["items"] == []

    await _upload(client, cookies)
    calls = _fake_llm(monkeypatch)
    fresh = await _job(session, "https://c.com/fresh")
    resp = await client.post(f"/api/v1/jobs/{fresh.id}/cover-letter", cookies=cookies)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "quota_exceeded"
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_cv_deleted_mid_generation_keeps_the_letter_out_of_storage(
    client, web_user, session, monkeypatch,
):
    user, cookies = web_user
    await _upload(client, cookies)
    job = await _job(session)

    async def delete_cv() -> None:
        assert (await client.delete("/api/v1/cv", cookies=cookies)).status_code == 200

    _fake_llm(monkeypatch, during=delete_cv)
    resp = await client.post(f"/api/v1/jobs/{job.id}/cover-letter", cookies=cookies)

    assert resp.status_code == 200
    assert [r.content for r in await _letters(session, user.id)] == [""]


# ── upload robustness ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rejected_upload_records_no_consent(client, web_user, session):
    """Consent covers a stored CV; an unreadable file stores nothing, so it
    must not leave a consent on file that skips the next upload's question."""
    user, cookies = web_user
    resp = await _upload(client, cookies, filename="cv.pdf", content=b"not a pdf")
    assert resp.status_code == 422
    await session.refresh(user)
    assert user.cv_consent_at is None


@pytest.mark.asyncio
async def test_upload_parses_off_the_event_loop(client, web_user, monkeypatch):
    """A slow parse (a crafted PDF took 3 s) used to stall every request."""
    import threading

    _user, cookies = web_user
    seen: dict = {}
    real_extract = cl_mod.extract_text

    def spy(filename: str, content: bytes):
        seen["thread"] = threading.current_thread()
        return real_extract(filename, content)

    monkeypatch.setattr(cl_mod, "extract_text", spy)
    assert (await _upload(client, cookies)).status_code == 200
    assert seen["thread"] is not threading.main_thread()
