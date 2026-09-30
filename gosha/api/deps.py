"""Shared API dependencies: session cookies, current user, admin guard."""

from __future__ import annotations

import logging
import os

from fastapi import Depends, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from gosha.config import load_web_settings
from gosha.database import get_session
from gosha.models import User

log = logging.getLogger(__name__)

COOKIE_NAME = "gosha_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


class ApiError(Exception):
    """Error carrying the machine code + HTTP status for the JSON envelope."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


def client_ip(request: Request) -> str:
    """The caller's IP as uvicorn resolved it (trusted proxies only)."""
    return request.client.host if request.client else "unknown"


async def enforce_rate_limit(rule, client: str) -> None:
    """Raise 429 (with Retry-After) when `client` is over `rule`."""
    from gosha.ratelimit import hit

    allowed, retry_after = await hit(rule, client)
    if not allowed:
        raise ApiError(
            429, "rate_limited",
            f"Too many requests — try again in {retry_after} s.",
            headers={"Retry-After": str(retry_after)},
        )


def serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        load_web_settings().session_secret, salt="gosha-session"
    )


def set_session_cookie(response: Response, user_id: int) -> None:
    settings = load_web_settings()
    response.set_cookie(
        COOKIE_NAME,
        serializer().dumps({"uid": user_id}),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def _session_user_id(request: Request) -> int | None:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        data = serializer().loads(raw, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    return uid if isinstance(uid, int) else None


async def current_user(request: Request) -> User:
    """Resolve the signed-in user or raise 401."""
    uid = _session_user_id(request)
    if uid is None:
        raise ApiError(401, "unauthenticated", "Sign in with Discord to continue.")
    async with get_session() as session:
        user = await session.get(User, uid)
    if user is None:
        raise ApiError(401, "unauthenticated", "Session is no longer valid.")
    return user


async def optional_user(request: Request) -> User | None:
    """Resolve the signed-in user, or None for anonymous requests."""
    uid = _session_user_id(request)
    if uid is None:
        return None
    async with get_session() as session:
        return await session.get(User, uid)


def is_admin(user: User) -> bool:
    raw = os.getenv("ADMIN_DISCORD_IDS", "")
    admin_ids = {s.strip() for s in raw.split(",") if s.strip()}
    return str(user.discord_user_id) in admin_ids


async def admin_user(user: User = Depends(current_user)) -> User:
    """Resolve the signed-in admin or raise 403."""
    if not is_admin(user):
        raise ApiError(403, "forbidden", "Admin access required.")
    return user
