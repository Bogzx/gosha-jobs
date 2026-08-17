"""Tests for web settings loading."""

from __future__ import annotations

import pytest

from gosha.config import load_web_settings


def _set_required(monkeypatch) -> None:
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("DISCORD_CLIENT_ID", "123")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "abc")


def test_web_settings_defaults(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("DISCORD_REDIRECT_URI", raising=False)
    ws = load_web_settings()
    assert ws.session_secret == "test-secret"
    assert ws.discord_client_id == "123"
    assert ws.discord_client_secret == "abc"
    assert ws.redirect_uri == "http://localhost:8000/api/v1/auth/discord/callback"
    assert ws.cookie_secure is False


def test_web_settings_prod(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://jobs.bogdantruta.com")
    monkeypatch.delenv("DISCORD_REDIRECT_URI", raising=False)
    ws = load_web_settings()
    assert ws.redirect_uri == (
        "https://jobs.bogdantruta.com/api/v1/auth/discord/callback"
    )
    assert ws.cookie_secure is True


def test_web_settings_explicit_redirect(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("DISCORD_REDIRECT_URI", "https://other.example/cb")
    ws = load_web_settings()
    assert ws.redirect_uri == "https://other.example/cb"


def test_web_settings_optional_fields(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("DISCORD_GUILD_ID", "999888")
    monkeypatch.setenv("DISCORD_INVITE_URL", "https://discord.gg/abc")
    ws = load_web_settings()
    assert ws.guild_id == 999888
    assert ws.invite_url == "https://discord.gg/abc"


def test_web_settings_missing_secret_raises(monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("DISCORD_CLIENT_ID", "1")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "x")
    with pytest.raises(RuntimeError):
        load_web_settings()


def test_short_session_secret_is_rejected_outside_tests(monkeypatch):
    """`SESSION_SECRET=dev` signs a {"uid": N} cookie over a tiny id space,
    and admin is an id-membership test — so a weak secret is the practical
    admin-forgery path, not a theoretical one."""
    _set_required(monkeypatch)
    monkeypatch.setenv("SESSION_SECRET", "dev")
    # Simulate a real process: no pytest marker, no opt-out.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("GOSHA_ALLOW_WEAK_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="at least 32"):
        load_web_settings()


def test_weak_secret_opt_out_is_explicit(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("SESSION_SECRET", "dev")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("GOSHA_ALLOW_WEAK_SECRET", "1")

    assert load_web_settings().session_secret == "dev"


def test_long_session_secret_accepted_without_opt_out(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("SESSION_SECRET", "s" * 48)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("GOSHA_ALLOW_WEAK_SECRET", raising=False)

    assert load_web_settings().session_secret == "s" * 48


def test_cookie_secure_can_be_set_explicitly(monkeypatch):
    """Behind a TLS-terminating proxy the base URL may be http:// while the
    browser connection is https — the guess is then wrong in the unsafe
    direction."""
    _set_required(monkeypatch)
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("COOKIE_SECURE", "true")
    assert load_web_settings().cookie_secure is True

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://gosha.example")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    assert load_web_settings().cookie_secure is False
