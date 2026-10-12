"""Cover letters HTTP adapter."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from gosha.api.deps import current_user
from gosha.api.schemas import RowId
from gosha.models import User
from gosha.services import cover_letters as service

router = APIRouter(tags=["cover-letters"])


@router.get("/cover-letters")
async def list_cover_letters(user: User = Depends(current_user)) -> dict:
    rows = await service.list_cover_letters(user.id)
    return {
        "items": [
            {
                "id": letter.id,
                "job_id": job.id,
                "job_title": job.title,
                "company": job.company,
                "content": letter.content,
                "created_at": letter.created_at.isoformat() if letter.created_at else None,
            }
            for letter, job in rows
        ]
    }


@router.post("/jobs/{job_id}/cover-letter")
async def generate_for_job(job_id: RowId, user: User = Depends(current_user)) -> dict:
    content, cached = await service.generate_for_job(user, job_id)
    return {"content": content, "cached": cached}
