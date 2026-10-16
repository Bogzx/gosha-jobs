"""Unauthenticated scrape-freshness probe for the out-of-process watchdog.

GET /api/v1/health/freshness → when the last scrape cycle finished, how
many jobs it returned, when a new job was last stored. /health only says
the API process is up; this says whether the *bot* is still doing its job,
which is what the owner needs to hear about when the bot is hung or gone.
"""

from __future__ import annotations

from fastapi import APIRouter

from gosha.services import freshness as service

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/freshness")
async def scrape_freshness() -> dict:
    return await service.scrape_freshness()
