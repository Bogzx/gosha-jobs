"""GET /api/v1/r/{token}: a DM's Apply link (gosha/clicks.py).

Unauthenticated (the DM reader is not signed in on the website). The token
is a signed delivery id; the destination is that delivery's posting, looked
up here, so nothing in the URL chooses where the browser goes.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from gosha import clicks

router = APIRouter(tags=["redirect"])

_HEADERS = {
    # Every click must reach us (and be counted), not a cache.
    "Cache-Control": "no-store",
    # The posting site need not learn the token.
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
}


@router.get("/r/{token}", include_in_schema=False)
async def follow_apply_link(token: str) -> RedirectResponse:
    user_job_id = clicks.user_job_from(token)
    target = await clicks.record_click(user_job_id) if user_job_id is not None else None
    if target is None:
        raise HTTPException(status_code=404, detail="Unknown link")
    return RedirectResponse(target, status_code=302, headers=_HEADERS)
