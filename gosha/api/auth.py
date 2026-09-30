"""Discord OAuth2 login flow.

Scope `identify` gives us the user's id/username/avatar; `guilds.join`
lets the bot add them to the GOSHA server so DM alerts work without any
manual setup (the join-server funnel).
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from gosha import ratelimit
from gosha.api.deps import (
    ApiError,
    clear_session_cookie,
    client_ip,
    enforce_rate_limit,
    set_session_cookie,
)
from gosha.api.schemas import OkOut
from gosha.config import load_web_settings
from gosha.database import get_session
from gosha.models import OAuthConsumedState, OAuthHandoff

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

DISCORD_API = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
OAUTH_STATE_COOKIE = "gosha_oauth_state"
STATE_MAX_AGE = 600  # seconds to complete the OAuth dance


def _state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        load_web_settings().session_secret, salt="gosha-oauth-state"
    )


def make_state() -> str:
    return _state_serializer().dumps(secrets.token_urlsafe(16))


def _verify_state(state: str) -> bool:
    try:
        _state_serializer().loads(state, max_age=STATE_MAX_AGE)
        return True
    except BadSignature:
        return False


def _now() -> datetime:
    return datetime.now(timezone.utc)


# States are single-use: once a callback consumes one, replays fail. The
# record lives in the database (oauth_consumed_states), not process memory,
# so a replay is caught whichever API worker serves it. Rows outlive the
# state's own signature TTL by nothing — past STATE_MAX_AGE the signature
# check rejects the state anyway — and are purged on the next consume.
async def _consume_state(state: str) -> bool:
    """Mark a state used; False when it was already consumed."""
    now = _now()
    async with get_session() as session:
        await session.execute(
            delete(OAuthConsumedState).where(OAuthConsumedState.expires_at < now)
        )
        session.add(OAuthConsumedState(
            state=state[:255], expires_at=now + timedelta(seconds=STATE_MAX_AGE),
        ))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return False
    return True


async def _state_is_valid(state: str) -> bool:
    """Signature + TTL + single-use.

    This proves the state was minted by *us* and has not been replayed. It
    does NOT prove it was minted by the browser presenting it — that is what
    `_state_bound_to_browser` is for.
    """
    return bool(state) and _verify_state(state) and await _consume_state(state)


def _state_bound_to_browser(state: str, cookie_value: str) -> bool:
    """CSRF binding: the state must be one THIS browser started.

    The cookie keeps the last few states so a second sign-in click doesn't
    invalidate a pending authorize window.

    An absent cookie is NOT a pass. Accepting one would let an attacker mint
    a state, authorize their own Discord account, and hand the victim the
    resulting callback URL — logging the victim into the attacker's account,
    where anything the victim then uploads (their CV) belongs to the attacker.
    The cross-browser desktop flow is preserved by the handoff below instead.
    """
    return bool(cookie_value) and state in cookie_value.split("|")


# ---------------------------------------------------------------------------
# Cross-browser handoff
#
# The desktop Discord app opens the callback in the system DEFAULT browser,
# which may not be the browser the user clicked "sign in" from — so the
# callback browser has no state cookie and must never be handed a session.
#
# Instead the callback parks the resolved user id server-side under the
# state, and the browser that actually minted that state (it holds the
# cookie) claims it from /auth/discord/handoff. The session cookie is only
# ever set in the browser that started the flow, so an attacker-supplied
# callback URL grants nothing: the attacker cannot make the victim's browser
# hold a cookie containing the attacker's state, and cannot learn a state
# the victim minted.
# ---------------------------------------------------------------------------

HANDOFF_MAX_AGE = 300  # seconds the originating browser has to collect


# Parked in the database (oauth_handoffs): the callback and the poll that
# collects it are separate requests and may hit different API workers.
async def _park_handoff(state: str, user_id: int, is_new: bool) -> None:
    now = _now()
    async with get_session() as session:
        await session.execute(
            delete(OAuthHandoff).where(
                (OAuthHandoff.expires_at < now) | (OAuthHandoff.state == state[:255])
            )
        )
        session.add(OAuthHandoff(
            state=state[:255], user_id=user_id, is_new=is_new,
            expires_at=now + timedelta(seconds=HANDOFF_MAX_AGE),
        ))
        await session.commit()


async def _claim_handoff(cookie_value: str) -> tuple[int, bool] | None:
    """Pop a completed sign-in for any state this browser minted.

    Claim = a DELETE that must remove exactly one row, so two concurrent
    polls for the same handoff cannot both get a session.
    """
    now = _now()
    states = [s[:255] for s in (cookie_value or "").split("|") if s]
    if not states:
        return None
    async with get_session() as session:
        rows = (await session.execute(
            select(OAuthHandoff).where(OAuthHandoff.state.in_(states))
        )).scalars().all()
        for row in rows:
            claimed = await session.execute(
                delete(OAuthHandoff).where(OAuthHandoff.state == row.state)
            )
            await session.commit()
            expires = row.expires_at
            if expires.tzinfo is None:  # SQLite returns naive UTC
                expires = expires.replace(tzinfo=timezone.utc)
            if claimed.rowcount == 1 and expires >= now:
                return row.user_id, row.is_new
    return None


# Deep-link scheme handled by the Discord mobile app — opens the authorize
# screen in the app instead of a browser login wall.
APP_AUTHORIZE_URL = "discord://-/oauth2/authorize"


@router.get("/discord/login")
async def discord_login(request: Request, format: str = ""):
    await enforce_rate_limit(ratelimit.AUTH, client_ip(request))
    settings = load_web_settings()
    state = make_state()
    params = urlencode({
        "client_id": settings.discord_client_id,
        "redirect_uri": settings.redirect_uri,
        "response_type": "code",
        "scope": "identify guilds.join",
        "state": state,
        "prompt": "none",
    })

    if format == "json":
        # The SPA uses this on mobile: try the app deep link first,
        # fall back to the browser URL. State cookie set either way.
        response: JSONResponse | RedirectResponse = JSONResponse({
            "web_url": f"{AUTHORIZE_URL}?{params}",
            "app_url": f"{APP_AUTHORIZE_URL}?{params}",
        })
    else:
        response = RedirectResponse(f"{AUTHORIZE_URL}?{params}", status_code=307)

    # Keep the last few states so concurrent sign-in attempts (second
    # click, prefetch refresh) don't orphan a pending authorize window.
    previous = request.cookies.get(OAUTH_STATE_COOKIE, "")
    recent = [state] + [s for s in previous.split("|") if s][:2]
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        "|".join(recent),
        max_age=STATE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return response


@router.get("/discord/callback")
async def discord_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    await enforce_rate_limit(ratelimit.AUTH, client_ip(request))
    settings = load_web_settings()

    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE, "")
    if not await _state_is_valid(state):
        raise ApiError(400, "invalid_state", "OAuth state check failed — try signing in again.")
    same_browser = _state_bound_to_browser(state, cookie_state)
    if not code:
        raise ApiError(400, "invalid_request", "Discord did not return a code.")

    async with httpx.AsyncClient(timeout=15) as http:
        token_resp = await http.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": settings.discord_client_id,
                "client_secret": settings.discord_client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token_resp.status_code != 200:
            log.warning("Discord token exchange failed: %s", token_resp.text[:200])
            raise ApiError(502, "discord_error", "Discord sign-in failed — try again.")
        access_token = token_resp.json().get("access_token", "")

        profile_resp = await http.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if profile_resp.status_code != 200:
            raise ApiError(502, "discord_error", "Could not load your Discord profile.")
        profile = profile_resp.json()

        discord_id = int(profile["id"])
        username = profile.get("username")
        avatar_hash = profile.get("avatar")
        avatar_url = (
            f"https://cdn.discordapp.com/avatars/{profile['id']}/{avatar_hash}.png"
            if avatar_hash
            else None
        )

        in_guild = await _join_guild(http, settings.guild_id, discord_id, access_token)

    from gosha.services.users import upsert_discord_user

    user_id, is_new = await upsert_discord_user(
        discord_id, username, avatar_url, in_guild,
    )

    try:
        from gosha.events import get_event_store
        await get_event_store().emit(
            "web.signin", actor_id=user_id, payload={"new_user": is_new}
        )
    except Exception:
        pass

    if not same_browser:
        # Different browser than the one that started the flow (desktop app
        # deep link). Park the result; the originating browser collects it.
        await _park_handoff(state, user_id, is_new)
        return RedirectResponse("/signed-in", status_code=307)

    response = RedirectResponse("/welcome" if is_new else "/", status_code=307)
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    set_session_cookie(response, user_id)
    return response


@router.get("/discord/handoff")
async def discord_handoff(request: Request) -> JSONResponse:
    """Collect a sign-in completed in another browser.

    Only the browser holding the state cookie that minted the state can
    claim it, so this is the CSRF binding — moved from the callback (which
    an attacker can drive) to a request the victim's browser makes itself.
    """
    await enforce_rate_limit(ratelimit.AUTH_HANDOFF, client_ip(request))
    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE, "")
    claimed = await _claim_handoff(cookie_state)
    if claimed is None:
        return JSONResponse({"signed_in": False})

    user_id, is_new = claimed
    response = JSONResponse({"signed_in": True, "new_user": is_new})
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    set_session_cookie(response, user_id)
    return response


async def _join_guild(
    http: httpx.AsyncClient, guild_id: int, discord_id: int, access_token: str,
) -> bool:
    """Best-effort: add the user to the GOSHA guild so the bot can DM them."""
    bot_token = os.getenv("DISCORD_TOKEN", "")
    if not guild_id or not bot_token:
        return False
    try:
        resp = await http.put(
            f"{DISCORD_API}/guilds/{guild_id}/members/{discord_id}",
            json={"access_token": access_token},
            headers={"Authorization": f"Bot {bot_token}"},
        )
        if resp.status_code in (201, 204):
            return True
        # PUT can fail for permission reasons while the user is already in
        check = await http.get(
            f"{DISCORD_API}/guilds/{guild_id}/members/{discord_id}",
            headers={"Authorization": f"Bot {bot_token}"},
        )
        return check.status_code == 200
    except httpx.HTTPError as exc:
        log.warning("Guild join failed for %s: %s", discord_id, exc)
        return False


@router.post("/logout", response_model=OkOut)
async def logout(response: Response) -> OkOut:
    clear_session_cookie(response)
    return OkOut()


# The local-testing backdoor lives in gosha/api/debug_login.py, which
# .dockerignore keeps out of the production image entirely — the route
# cannot be enabled by a stray env var because the code is not there.
