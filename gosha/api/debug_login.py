"""Local-testing sign-in backdoor — NEVER shipped to production.

`.dockerignore` excludes this file, so the production image simply does not
contain the route: `gosha.api.app._mount_routers` skips modules that fail to
import. The DEBUG_LOGIN env gate below is defence in depth for anyone running
from a source checkout.
"""

from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from gosha.api.deps import ApiError, set_session_cookie

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/debug-login")
async def debug_login(uid: int) -> RedirectResponse:
    """Session for an arbitrary user id. Requires DEBUG_LOGIN=1."""
    if os.getenv("DEBUG_LOGIN") != "1":
        raise ApiError(404, "not_found", "Not found.")
    response = RedirectResponse("/", status_code=307)
    set_session_cookie(response, uid)
    return response
