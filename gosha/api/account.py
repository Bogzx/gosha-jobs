"""Account-level GDPR endpoints: data export and account erasure."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from gosha.api.deps import ApiError, clear_session_cookie, current_user
from gosha.models import User
from gosha.services import account as service

router = APIRouter(prefix="/account", tags=["account"])


@router.get("/export")
async def export_account(user: User = Depends(current_user)) -> JSONResponse:
    """Download everything we hold about this account (GDPR Art. 15/20)."""
    payload = await service.export_account(user.id)
    response = JSONResponse(payload)
    response.headers["Content-Disposition"] = (
        f'attachment; filename="gosha-export-{user.id}.json"'
    )
    return response


@router.delete("")
async def delete_account(
    confirm: str = "", user: User = Depends(current_user),
) -> JSONResponse:
    """Erase the account and everything derived from it (GDPR Art. 17).

    `confirm=DELETE` is required so a stray link or prefetch can never
    destroy an account. There is no undo and no soft-delete: the row, the
    CV file, the cover letters, the tracker and the event log all go.
    """
    if confirm != "DELETE":
        raise ApiError(
            422,
            "confirmation_required",
            "Add ?confirm=DELETE to permanently erase your account.",
        )

    removed = await service.delete_account(user.id)
    response = JSONResponse({"ok": True, "deleted": removed})
    clear_session_cookie(response)
    return response
