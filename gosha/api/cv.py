"""CV HTTP adapter."""

from __future__ import annotations

from fastapi import APIRouter, Depends, UploadFile

from gosha.api.deps import current_user
from gosha.api.schemas import OkOut
from gosha.domain.errors import FileTooLargeError
from gosha.models import User
from gosha.services import cv as service

router = APIRouter(prefix="/cv", tags=["cv"])

# Read the upload in slices so an oversized body is refused after one
# chunk instead of being fully materialised first. Caddy also caps bodies
# at 6 MB (see Caddyfile), but the API must not depend on the edge: it is
# reachable directly from inside the compose network, and self-hosters may
# front it with something else.
UPLOAD_CHUNK_BYTES = 64 * 1024


async def read_capped(file: UploadFile, limit: int) -> bytes:
    """Read at most `limit` bytes, raising as soon as the cap is passed."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise FileTooLargeError(
                f"CV files can be at most {limit // (1024 * 1024)} MB."
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("")
async def get_cv(user: User = Depends(current_user)) -> dict:
    return service.get_cv(user.id)


@router.put("")
async def upload_cv(file: UploadFile, user: User = Depends(current_user)) -> dict:
    content = await read_capped(file, service.MAX_CV_BYTES)
    return await service.upload_cv(user.id, file.filename or "cv", content)


@router.delete("", response_model=OkOut)
async def delete_cv(user: User = Depends(current_user)) -> OkOut:
    await service.delete_cv(user.id)
    return OkOut()
