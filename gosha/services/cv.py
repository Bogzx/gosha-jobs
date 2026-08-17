"""CV management use cases: upload, read, delete — shared by web and bot."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import gosha.cover_letter as storage
from gosha.domain.errors import FileTooLargeError, ValidationError

log = logging.getLogger(__name__)

MAX_CV_BYTES = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = (".pdf", ".docx", ".txt", ".md")


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


async def upload_cv(user_id: int, filename: str, content: bytes) -> dict:
    """Validate, extract text, store, and refresh the personalization vector."""
    if not (filename or "cv").lower().endswith(ALLOWED_EXTENSIONS):
        raise ValidationError("Supported CV formats: PDF, DOCX, TXT, MD.")
    if len(content) > MAX_CV_BYTES:
        raise FileTooLargeError("CV files can be at most 5 MB.")

    text = storage.extract_text(filename, content)
    if not text or not text.strip():
        raise ValidationError(
            "Couldn't read any text from that file — try a different format."
        )

    storage.save_cv(user_id, text)

    # Best-effort: feed falls back gracefully when the model is unavailable
    try:
        from gosha.embeddings import embed_user_cv
        await embed_user_cv(user_id, text)
    except Exception as exc:
        log.warning("CV embedding failed for user %d: %s", user_id, exc)

    return get_cv(user_id)


async def delete_cv(user_id: int) -> None:
    """Delete the CV and everything derived from it.

    "Delete" has to mean the derived artefacts too. Cover letters are
    written *from* the CV — leaving them behind meant the personal data the
    user asked us to erase was still sitting in the database, quoted back at
    them on the CV page.
    """
    storage.delete_cv(user_id)

    try:
        from sqlalchemy import delete as sql_delete

        from gosha.database import get_session
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
