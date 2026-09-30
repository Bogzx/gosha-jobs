"""Analytics HTTP adapter — SPA pageview pings."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from gosha import ratelimit
from gosha.api.deps import client_ip, optional_user
from gosha.api.schemas import OkOut, PageviewIn
from gosha.models import User
from gosha.services import admin_stats as service

router = APIRouter(prefix="/events", tags=["analytics"])

# Unauthenticated write endpoint on a public site — cap events per client
# so nobody can flood the events table. Over-limit requests still return
# OK (analytics must never break the SPA); the event is silently dropped.
# Counted in the database (gosha/ratelimit.py), so the cap holds across
# API workers.
RATE_LIMIT_PER_MINUTE = ratelimit.PAGEVIEW.limit


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
    client_key = f"user:{user.id}" if user is not None else f"ip:{client_ip(request)}"
    allowed, _retry = await ratelimit.hit(ratelimit.PAGEVIEW, client_key)
    if allowed:
        await service.record_pageview(user.id if user else None, body.path)
    return OkOut()
