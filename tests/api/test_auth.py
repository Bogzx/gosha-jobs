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


REBOUND_LOGIN = "/api/v1/auth/discord/login?rebound=1"


@pytest.mark.asyncio
@respx.mock
async def test_callback_state_cookie_mismatch_issues_no_session(client, monkeypatch):
    """A cookie that doesn't name this state is not a binding.

    The browser gets no session; it is sent to start a sign-in of its own.
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
    assert resp.headers["location"] == REBOUND_LOGIN
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
    assert resp.headers["location"] == REBOUND_LOGIN
    set_cookies = " ".join(resp.headers.get_list("set-cookie"))
    assert "gosha_session=" not in set_cookies
    # The attacker's code is never even exchanged.
    assert not respx.calls


@pytest.mark.asyncio
@respx.mock
async def test_approval_in_another_browser_never_signs_in_the_starter(
    client, session, monkeypatch,
):
    """Account takeover through the old cross-browser handoff.

    Attacker A starts a sign-in (holds the state cookie) and sends the
    authorize link to victim B. With prompt=none and B's earlier consent,
    opening it is enough: the callback runs in B's browser. That used to
    park B's account under A's state, and A's handoff poll collected a
    session for B. Now nothing is parked and A's poll gets nothing.
    """
    monkeypatch.setenv("DISCORD_TOKEN", "bot-token")
    _mock_discord(discord_id="31337", username="victim")

    login = await client.get("/api/v1/auth/discord/login", follow_redirects=False)
    state = login.headers["location"].split("state=")[1].split("&")[0]
    attacker_cookies = dict(client.cookies)
    client.cookies.clear()

    callback = await client.get(
        "/api/v1/auth/discord/callback",
        params={"code": "victims-code", "state": state},
        follow_redirects=False,
    )
    assert callback.headers["location"] == REBOUND_LOGIN
    assert "gosha_session=" not in " ".join(callback.headers.get_list("set-cookie"))

    for _ in range(2):
        poll = await client.get(
            "/api/v1/auth/discord/handoff", cookies=attacker_cookies,
        )
        assert poll.json() == {"signed_in": False}
        assert "gosha_session=" not in " ".join(poll.headers.get_list("set-cookie"))
    assert (
        await session.execute(select(User).where(User.discord_user_id == 31337))
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
@respx.mock
async def test_callback_browser_signs_itself_in_after_the_restart(
    client, session, monkeypatch,
):
    """The desktop-app case still ends signed in: in the browser Discord
    opened, through a sign-in that browser started itself."""
    monkeypatch.setenv("DISCORD_TOKEN", "bot-token")
    _mock_discord()

    # Browser A starts; the desktop app hands the callback to browser B.
    login = await client.get("/api/v1/auth/discord/login", follow_redirects=False)
    state = login.headers["location"].split("state=")[1].split("&")[0]
    client.cookies.clear()
    callback = await client.get(
        "/api/v1/auth/discord/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )

    # B follows the restart: its own state cookie, its own authorize round.
    restart = await client.get(callback.headers["location"], follow_redirects=False)
    assert restart.headers["location"].startswith("https://discord.com/oauth2/authorize")
    b_state = restart.headers["location"].split("state=")[1].split("&")[0]
    final = await client.get(
        "/api/v1/auth/discord/callback",
        params={"code": "c2", "state": b_state},
        follow_redirects=False,
    )

    assert final.headers["location"] == "/welcome"
    assert "gosha_session=" in " ".join(final.headers.get_list("set-cookie"))
    assert (
        await session.execute(select(User).where(User.discord_user_id == 424242))
    ).scalar_one()


@pytest.mark.asyncio
@respx.mock
async def test_restarted_sign_in_without_cookie_stops_instead_of_looping(
    client, monkeypatch,
):
    monkeypatch.setenv("DISCORD_TOKEN", "bot-token")
    _mock_discord()
    from gosha.api.auth import make_state

    resp = await client.get(
        "/api/v1/auth/discord/callback",
        params={"code": "c", "state": make_state(rebound=True)},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "cookies_required"
    assert "gosha_session=" not in " ".join(resp.headers.get_list("set-cookie"))


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


# ── shared state + rate limits ────────────────────────────────────────


@pytest.mark.asyncio
async def test_consumed_state_lives_in_the_database(client, session):
    """Survives --workers N: nothing about a sign-in is in process memory."""
    from sqlalchemy import select

    from gosha.api import auth
    from gosha.models import OAuthConsumedState

    state = auth.make_state()
    assert await auth._state_is_valid(state)
    assert not await auth._state_is_valid(state)  # replay
    assert (await session.execute(select(OAuthConsumedState))).scalars().one().state == state


@pytest.mark.asyncio
async def test_login_is_rate_limited_per_ip(client, monkeypatch):
    from gosha import ratelimit

    monkeypatch.setattr(ratelimit, "AUTH", ratelimit.Limit("auth", 2, 60))
    codes = [
        (await client.get("/api/v1/auth/discord/login", follow_redirects=False)).status_code
        for _ in range(3)
    ]
    assert codes[:2] == [307, 307]
    assert codes[2] == 429
    resp = await client.get("/api/v1/auth/discord/login", follow_redirects=False)
    assert resp.json()["error"]["code"] == "rate_limited"
    assert int(resp.headers["retry-after"]) >= 1


# ── session cookies ───────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_deleted_accounts_cookie_does_not_open_the_next_account(
    client, session, monkeypatch,
):
    """SQLite hands a deleted user's id to the next new account. The cookie
    names the Discord account too, so a leftover one opens nothing."""
    from gosha.api.deps import COOKIE_NAME, serializer

    monkeypatch.setenv("DISCORD_TOKEN", "bot-token")
    _mock_discord(discord_id="1001")
    state, cookies = _state_for(client)
    signin = await client.get(
        "/api/v1/auth/discord/callback",
        params={"code": "c", "state": state},
        cookies=cookies,
        follow_redirects=False,
    )
    session_cookie = {COOKIE_NAME: signin.cookies[COOKIE_NAME]}
    old_id = serializer().loads(session_cookie[COOKIE_NAME])["uid"]
    assert serializer().loads(session_cookie[COOKIE_NAME])["did"] == 1001
    assert (await client.get("/api/v1/me", cookies=session_cookie)).status_code == 200

    deleted = await client.delete(
        "/api/v1/account", params={"confirm": "DELETE"}, cookies=session_cookie,
    )
    assert deleted.status_code == 200
    newcomer = User(discord_user_id=2002, username="newcomer")
    session.add(newcomer)
    await session.commit()
    assert newcomer.id == old_id  # the reuse this guards against

    resp = await client.get("/api/v1/me", cookies=session_cookie)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_cookies_from_before_the_discord_binding_still_work(client, web_user):
    from gosha.api.deps import COOKIE_NAME, serializer

    user, _cookies = web_user
    legacy = {COOKIE_NAME: serializer().dumps({"uid": user.id})}
    resp = await client.get("/api/v1/me", cookies=legacy)
    assert resp.status_code == 200
    assert resp.json()["id"] == user.id
