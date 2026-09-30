"""CV management use cases: upload, read, delete — shared by web and bot."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import gosha.cover_letter as storage
from gosha.database import get_session
from gosha.domain.errors import (
    ConsentRequiredError,
    FileTooLargeError,
    ValidationError,
)
from gosha.models import User

log = logging.getLogger(__name__)

MAX_CV_BYTES = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = (".pdf", ".docx", ".txt", ".md")

# What the user agrees to. The web checkbox (web/src/components/
# CvDropzone.tsx) and the bot's /upload_cv say the same thing.
CONSENT_TEXT = (
    "GOSHA may store my CV as plain text and use it to rank job postings "
    "for me. If I ask for a cover letter, part of my CV is sent to a "
    "third-party AI provider. I can delete it at any time."
)
CONSENT_REQUIRED_MESSAGE = (
    "Storing your CV needs your consent first: " + CONSENT_TEXT
)


async def has_cv_consent(user_id: int) -> bool:
    async with get_session() as session:
        user = await session.get(User, user_id)
        return user is not None and user.cv_consent_at is not None


async def _record_consent(user_id: int) -> None:
    async with get_session() as session:
        user = await session.get(User, user_id)
        if user is not None and user.cv_consent_at is None:
            user.cv_consent_at = datetime.now(timezone.utc)
            await session.commit()


async def require_consent(user_id: int, consent: bool) -> None:
    """Record fresh consent, or confirm one is on file; raise otherwise.

    Called before the CV text is written anywhere. Consent used to live
    only in the browser's localStorage and the bot never asked at all, so
    there was nothing server-side to show that a stored CV was stored
    with the owner's agreement.
    """
    if consent:
        await _record_consent(user_id)
    elif not await has_cv_consent(user_id):
        raise ConsentRequiredError(CONSENT_REQUIRED_MESSAGE)


def get_cv(user_id: int) -> dict:
    """{'has_cv', 'text', 'uploaded_at'} for the user."""
    text = storage.load_cv(user_id)
    uploaded_at = None
    if text is not None:
        path = storage.get_cv_path(user_id)
        try:
            uploaded_at = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        except OSError:
            pass
    return {"has_cv": text is not None, "text": text, "uploaded_at": uploaded_at}


async def upload_cv(
    user_id: int, filename: str, content: bytes, consent: bool = False,
) -> dict:
    """Validate, extract text, store, and refresh the personalization vector."""
    if not (filename or "cv").lower().endswith(ALLOWED_EXTENSIONS):
        raise ValidationError("Supported CV formats: PDF, DOCX, TXT, MD.")
    if len(content) > MAX_CV_BYTES:
        raise FileTooLargeError("CV files can be at most 5 MB.")
    await require_consent(user_id, consent)

    text = storage.extract_text(filename, content)
    if not text or not text.strip():
        raise ValidationError(
            "Couldn't read any text from that file — try a different format."
        )

    await store_cv_text(user_id, text)
    return get_cv(user_id)


async def store_cv_text(user_id: int, text: str) -> None:
    """Save already-extracted CV text and refresh the personalization vector.

    The one write path for both the website and the bot. The bot used to
    call the storage module directly and skipped the embedding, so a CV
    uploaded on Discord never reached the ranking — and a vector left over
    from an older web upload kept ranking the user by their previous CV.
    Callers must have passed require_consent() first.
    """
    storage.save_cv(user_id, text)

    # Best-effort: feed falls back gracefully when the model is unavailable
    try:
        from gosha.embeddings import embed_user_cv
        await embed_user_cv(user_id, text)
    except Exception as exc:
        log.warning("CV embedding failed for user %d: %s", user_id, exc)


async def delete_cv(user_id: int) -> bool:
    """Delete the CV and everything derived from it; True if one existed.

    "Delete" has to mean the derived artefacts too. Cover letters are
    written *from* the CV — leaving them behind meant the personal data the
    user asked us to erase was still sitting in the database, quoted back at
    them on the CV page. Deleting also withdraws the recorded consent, so a
    later upload asks again.
    """
    existed = storage.delete_cv(user_id)

    try:
        from sqlalchemy import delete as sql_delete

        from gosha.models import CoverLetter

        async with get_session() as session:
            await session.execute(
                sql_delete(CoverLetter).where(CoverLetter.user_id == user_id)
            )
            await session.commit()
    except Exception as exc:
        log.warning("Cover-letter cleanup failed for user %d: %s", user_id, exc)

    try:
        from gosha.embeddings import clear_user_cv_embedding
        await clear_user_cv_embedding(user_id)
    except Exception as exc:
        log.warning("CV embedding cleanup failed for user %d: %s", user_id, exc)

    try:
        async with get_session() as session:
            user = await session.get(User, user_id)
            if user is not None and user.cv_consent_at is not None:
                user.cv_consent_at = None
                await session.commit()
    except Exception as exc:
        log.warning("CV consent withdrawal failed for user %d: %s", user_id, exc)

    return existed
