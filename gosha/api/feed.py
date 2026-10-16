"""Personalized feed endpoint — thin route over gosha.recommend."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query

from gosha.api.deps import current_user
from gosha.api.jobs import job_to_out
from gosha.api.schemas import FeedListOut
from gosha.models import User
from gosha.recommend import get_feed_page

router = APIRouter(prefix="/feed", tags=["feed"])


@router.get("", response_model=FeedListOut)
async def feed(
    user: User = Depends(current_user),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=100),
    # "searches": only what one of the user's active searches would take
    # on location and level; "all": every posting in the window.
    scope: Literal["searches", "all"] = Query(default="searches"),
) -> FeedListOut:
    items, total, scoped, hidden_by_scope = await get_feed_page(
        user.id, page=page, per_page=per_page, scope=scope,
    )
    return FeedListOut(
        items=[
            job_to_out(
                item.job,
                match_score=item.score,
                match_percentile=item.percentile,
                match_reasons=item.reasons,
                match_signals=list(item.signals),
            )
            for item in items
        ],
        total=total,
        page=page,
        per_page=per_page,
        scoped=scoped,
        hidden_by_scope=hidden_by_scope,
    )
