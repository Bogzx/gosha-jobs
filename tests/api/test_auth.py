"""Tests for the Discord OAuth flow (Discord mocked with respx)."""

from __future__ import annotations

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from gosha.models import User

DISCORD_API = "https://discord.com/api/v10"


def _state_for(client) -> tuple[str, dict]:
    """Generate a signed state the same way the login route does."""
    from gosha.api.auth import OAUTH_STATE_COOKIE, make_state

    state = make_state()
    return state, {OAUTH_STATE_COOKIE: state}


def _mock_discord(discord_id: str = "424242", username: str = "gosha",
                  avatar: str | None = "abc123", guild_put_status: int = 201):
    respx.post(f"{DISCORD_API}/oauth2/token").mock(
        return_value=Response(200, json={
            "access_token": "user-access-token",
            "token_type": "Bearer",
        })
    )
    respx.get(f"{DISCORD_API}/users/@me").mock(
        return_value=Response(200, json={
            "id": discord_id,
            "username": username,
            "avatar": avatar,
        })
    )
    respx.put(url__regex=rf"{DISCORD_API}/guilds/\d+/members/\d+").mock(
        return_value=Response(guild_put_status)
    )
    respx.get(url__regex=rf"{DISCORD_API}/guilds/\d+/members/\d+").mock(
        return_value=Response(404)
    )


@pytest.mark.asyncio
async def test_login_redirects_to_discord(client):
    resp = await client.get("/api/v1/auth/discord/login", follow_redirects=False)
    assert resp.status_code == 307
    location = resp.headers["location"]
    assert location.startswith("https://discord.com/oauth2/authorize")
    assert "client_id=1234" in location
    assert "identify+guilds.join" in location or "identify%20guilds.join" in location
    assert "state=" in location
    # State cookie set for CSRF binding
    assert any("gosha_oauth_state" in h for h in resp.headers.get_list("set-cookie"))


@pytest.mark.asyncio
@respx.mock
async def test_callback_creates_user_and_joins_guild(client, session, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "bot-token")
    _mock_discord()
    state, cookies = _state_for(client)

    resp = await client.get(
        "/api/v1/auth/discord/callback",
        params={"code": "the-code", "state": state},
        cookies=cookies,
        follow_redirects=False,
    )

    assert resp.status_code == 307
    assert resp.headers["location"] == "/welcome"
    set_cookies = " ".join(resp.headers.get_list("set-cookie"))
    assert "gosha_session=" in set_cookies

    user = (
        await session.execute(select(User).where(User.discord_user_id == 424242))
    ).scalar_one()
    assert user.username == "gosha"
    assert user.avatar_url == "https://cdn.discordapp.com/avatars/424242/abc123.png"
    assert user.in_guild is True
    assert user.last_login_at is not None


@pytest.mark.asyncio
@respx.mock
async def test_callback_existing_user_redirects_home(client, session, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "bot-token")
    session.add(User(discord_user_id=424242, username="old-name"))
    await session.commit()
    _mock_discord(username="new-name")
    state, cookies = _state_for(client)

    resp = await client.get(
        "/api/v1/auth/discord/callback",
        params={"code": "c", "state": state},
        cookies=cookies,
        follow_redirects=False,
    )

    assert resp.headers["location"] == "/"
    user = (
        await session.execute(select(User).where(User.discord_user_id == 424242))
    ).scalar_one()
    await session.refresh(user)
    assert user.username == "new-name"


@pytest.mark.asyncio
@respx.mock
async def test_callback_guild_join_failure_is_non_fatal(client, session, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "bot-token")
    _mock_discord(guild_put_status=403)
    state, cookies = _state_for(client)

    resp = await client.get(
        "/api/v1/auth/discord/callback",
        params={"code": "c", "state": state},
        cookies=cookies,
        follow_redirects=False,
    )

    assert resp.status_code == 307  # login still succeeds
    user = (
        await session.execute(select(User).where(User.discord_user_id == 424242))
    ).scalar_one()
    assert user.in_guild is False


@pytest.mark.asyncio
async def test_callback_bad_state_rejected(client):
    resp = await client.get(
        "/api/v1/auth/discord/callback",
        params={"code": "c", "state": "forged-state"},
        cookies={"gosha_oauth_state": "forged-state"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_state"


@pytest.mark.asyncio
@respx.mock
async def test_callback_state_cookie_mismatch_issues_no_session(client, monkeypatch):
    """A cookie that doesn't name this state is not a binding.

    The browser gets no session — the sign-in is parked for whichever
    browser actually minted the state (see the handoff tests below).
    """
    monkeypatch.setenv("DISCORD_TOKEN", "bot-token")
    _mock_discord()
    state, _cookies = _state_for(client)
    other_state, _ = _state_for(client)
    resp = await client.get(
        "/api/v1/auth/discord/callback",
        params={"code": "c", "state": state},
        cookies={"gosha_oauth_state": other_state},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/signed-in"
    assert "gosha_session=" not in " ".join(resp.headers.get_list("set-cookie"))


@pytest.mark.asyncio
@respx.mock
async def test_callback_without_cookie_never_issues_a_session(client, monkeypatch):
    """The login-CSRF fix.

    An attacker can mint a state, authorize their OWN Discord account, and
    hand the victim the resulting callback URL. The victim's browser has no
    state cookie, so the callback must not put a session cookie on it —
    otherwise the victim is signed into the attacker's account and the CV
    they upload lands in the attacker's hands.
    """
    monkeypatch.setenv("DISCORD_TOKEN", "bot-token")
    _mock_discord()
    state, _cookies = _state_for(client)

    resp = await client.get(
        "/api/v1/auth/discord/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )

    assert resp.status_code == 307
    assert resp.headers["location"] == "/signed-in"
    set_cookies = " ".join(resp.headers.get_list("set-cookie"))
    assert "gosha_session=" not in set_cookies


@pytest.mark.asyncio
@respx.mock
async def test_cross_browser_handoff_signs_in_the_originating_browser(
    client, session, monkeypatch,
):
    """Desktop app flow, preserved safely.

    Browser A clicks sign-in (gets the state cookie). The desktop Discord
    app opens the callback in Browser B, which has no cookie and therefore
    gets no session. Browser A collects the completed sign-in from
    /auth/discord/handoff, because only A holds the cookie naming the state.
    """
    monkeypatch.setenv("DISCORD_TOKEN", "bot-token")
    _mock_discord()

    # Browser A starts the flow
    login = await client.get("/api/v1/auth/discord/login", follow_redirects=False)
    state = login.headers["location"].split("state=")[1].split("&")[0]

    # Browser B completes the OAuth exchange — no cookie, no session.
    # (The shared test client keeps A's cookie jar; clear it to be B.)
    client.cookies.clear()
    callback = await client.get(
        "/api/v1/auth/discord/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    assert "gosha_session=" not in " ".join(callback.headers.get_list("set-cookie"))

    # Browser A collects it
    from gosha.api.auth import OAUTH_STATE_COOKIE

    handoff = await client.get(
        "/api/v1/auth/discord/handoff",
        cookies={OAUTH_STATE_COOKIE: state},
    )
    assert handoff.json() == {"signed_in": True, "new_user": True}
    assert "gosha_session=" in " ".join(handoff.headers.get_list("set-cookie"))

    user = (
        await session.execute(select(User).where(User.discord_user_id == 424242))
    ).scalar_one()
    assert user.username == "gosha"


@pytest.mark.asyncio
@respx.mock
async def test_handoff_not_claimable_without_the_matching_state_cookie(
    client, monkeypatch,
):
    """A parked sign-in belongs to the browser that minted the state.

    An attacker who knows their own state cannot make the victim's browser
    claim it, and a browser holding an unrelated state gets nothing.
    """
    monkeypatch.setenv("DISCORD_TOKEN", "bot-token")
    _mock_discord()
    state, _ = _state_for(client)
    unrelated, _ = _state_for(client)

    await client.get(
        "/api/v1/auth/discord/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )

    from gosha.api.auth import OAUTH_STATE_COOKIE

    empty = await client.get("/api/v1/auth/discord/handoff")
    assert empty.json() == {"signed_in": False}
    assert "gosha_session=" not in " ".join(empty.headers.get_list("set-cookie"))

    wrong = await client.get(
        "/api/v1/auth/discord/handoff",
        cookies={OAUTH_STATE_COOKIE: unrelated},
    )
    assert wrong.json() == {"signed_in": False}


@pytest.mark.asyncio
@respx.mock
async def test_handoff_is_single_use(client, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "bot-token")
    _mock_discord()
    state, cookies = _state_for(client)

    await client.get(
        "/api/v1/auth/discord/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    first = await client.get("/api/v1/auth/discord/handoff", cookies=cookies)
    assert first.json()["signed_in"] is True

    second = await client.get("/api/v1/auth/discord/handoff", cookies=cookies)
    assert second.json() == {"signed_in": False}


@pytest.mark.asyncio
@respx.mock
async def test_callback_state_is_single_use(client, monkeypatch):
    """Replaying a callback URL fails even with the right cookie."""
    monkeypatch.setenv("DISCORD_TOKEN", "bot-token")
    _mock_discord()
    state, cookies = _state_for(client)

    first = await client.get(
        "/api/v1/auth/discord/callback",
        params={"code": "c", "state": state},
        cookies=cookies,
        follow_redirects=False,
    )
    assert first.status_code == 307

    replay = await client.get(
        "/api/v1/auth/discord/callback",
        params={"code": "c", "state": state},
        cookies=cookies,
        follow_redirects=False,
    )
    assert replay.status_code == 400


@pytest.mark.asyncio
@respx.mock
async def test_second_login_click_keeps_first_state_valid(client, monkeypatch):
    """Clicking sign-in twice must not orphan the first authorize window:
    the cookie keeps recent states, so completing with the FIRST one works."""
    monkeypatch.setenv("DISCORD_TOKEN", "bot-token")
    _mock_discord()

    first = await client.get("/api/v1/auth/discord/login", follow_redirects=False)
    first_state = first.headers["location"].split("state=")[1].split("&")[0]
    second = await client.get(
        "/api/v1/auth/discord/callback".replace("/callback", "/login"),
        cookies={"gosha_oauth_state": first_state},
        follow_redirects=False,
    )
    cookie_header = next(
        h for h in second.headers.get_list("set-cookie") if "gosha_oauth_state" in h
    )
    combined_cookie = cookie_header.split("gosha_oauth_state=")[1].split(";")[0]
    # URL-decode the pipe separator if the framework encoded it
    from urllib.parse import unquote
    combined_cookie = unquote(combined_cookie)
    assert first_state in combined_cookie.split("|")

    resp = await client.get(
        "/api/v1/auth/discord/callback",
        params={"code": "c", "state": first_state},
        cookies={"gosha_oauth_state": combined_cookie},
        follow_redirects=False,
    )
    assert resp.status_code == 307


@pytest.mark.asyncio
async def test_login_json_format_returns_app_deep_link(client):
    resp = await client.get("/api/v1/auth/discord/login", params={"format": "json"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["web_url"].startswith("https://discord.com/oauth2/authorize")
    assert body["app_url"].startswith("discord://-/oauth2/authorize")
    # Same state in both URLs, and the CSRF cookie is set
    assert body["web_url"].split("state=")[1] == body["app_url"].split("state=")[1]
    assert any("gosha_oauth_state" in h for h in resp.headers.get_list("set-cookie"))


@pytest.mark.asyncio
async def test_debug_login_disabled_by_default(client, monkeypatch):
    monkeypatch.delenv("DEBUG_LOGIN", raising=False)
    resp = await client.get(
        "/api/v1/auth/debug-login", params={"uid": 1}, follow_redirects=False
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_logout_clears_cookie(client, web_user):
    _user, cookies = web_user
    resp = await client.post("/api/v1/auth/logout", cookies=cookies)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    set_cookie = " ".join(resp.headers.get_list("set-cookie"))
    assert "gosha_session=" in set_cookie  # deletion sets empty value
