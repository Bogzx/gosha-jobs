"""Analytics HTTP adapter — SPA pageview pings."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from gosha.api.deps import optional_user
from gosha.api.schemas import OkOut, PageviewIn
from gosha.models import User
from gosha.services import admin_stats as service

router = APIRouter(prefix="/events", tags=["analytics"])

# Unauthenticated write endpoint on a public site — cap events per client
# so nobody can flood the events table. Over-limit requests still return
# OK (analytics must never break the SPA); the event is silently dropped.
RATE_LIMIT_PER_MINUTE = 30
_buckets: dict[str, tuple[float, int]] = {}
_MAX_BUCKETS = 10_000


def _allow(client_key: str, now: float | None = None) -> bool:
    now = now if now is not None else time.monotonic()
    window_start, count = _buckets.get(client_key, (now, 0))
    if now - window_start >= 60:
        window_start, count = now, 0
    if count >= RATE_LIMIT_PER_MINUTE:
        _buckets[client_key] = (window_start, count)
        return False
    if len(_buckets) > _MAX_BUCKETS:  # bound memory under address churn
        _buckets.clear()
    _buckets[client_key] = (window_start, count + 1)
    return True


@router.post("/pageview", response_model=OkOut)
async def pageview(
    request: Request,
    body: PageviewIn,
    user: User | None = Depends(optional_user),
) -> OkOut:
    # Key on the session when there is one. uvicorn is told which proxy
    # networks to trust (docker-compose.prod.yml), but a signed-in user is
    # a stronger identity than any IP: rotating X-Forwarded-For or hopping
    # networks buys a fresh bucket, rotating a signed session does not.
    client_key = (
        f"user:{user.id}"
        if user is not None
        else f"ip:{request.client.host if request.client else 'unknown'}"
    )
    if _allow(client_key):
        await service.record_pageview(user.id if user else None, body.path)
    return OkOut()
