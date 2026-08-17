"""Personalized feed endpoint — thin route over gosha.recommend."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from gosha.api.deps import current_user
from gosha.api.jobs import job_to_out
from gosha.api.schemas import JobListOut
from gosha.models import User
from gosha.recommend import get_feed

router = APIRouter(prefix="/feed", tags=["feed"])


@router.get("", response_model=JobListOut)
async def feed(
    user: User = Depends(current_user),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=100),
) -> JobListOut:
    items, total = await get_feed(user.id, page=page, per_page=per_page)
    return JobListOut(
        items=[
            job_to_out(
                item.job,
                match_score=item.score,
                match_percentile=item.percentile,
                match_reasons=item.reasons,
            )
            for item in items
        ],
        total=total,
        page=page,
        per_page=per_page,
    )
