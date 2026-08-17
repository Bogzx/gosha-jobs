"""Configuration loaded from environment variables."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

# Load .env file from project root (no-op if it doesn't exist)
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)


@dataclass(frozen=True)
class VPSConfig:
    """Connection details for a single proxy VPS."""

    host: str
    user: str
    local_port: int
    key_path: str = "/home/app/.ssh/id_rsa"


@dataclass(frozen=True)
class Settings:
    """Application-wide settings populated from environment variables."""

    discord_token: str
    database_url: str
    vps_list: list[VPSConfig] = field(default_factory=list)

    # Scheduler
    scrape_interval_minutes: int = 60

    # Discord channel where job alerts are posted
    alert_channel_id: int = 0

    # Matching
    use_semantic_matching: bool = False
    semantic_model: str = "all-mpnet-base-v2"
    semantic_threshold: float = 0.40

    # Admin user IDs (can run /scrape_now, /status)
    admin_user_ids: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class WebSettings:
    """Settings for the public web API (Discord OAuth, sessions)."""

    session_secret: str
    discord_client_id: str
    discord_client_secret: str
    redirect_uri: str
    public_base_url: str
    cookie_secure: bool
    guild_id: int = 0
    invite_url: str = ""


# 32 bytes is the shortest secret that is not brute-forceable offline
# against a signed cookie; itsdangerous will happily sign with "dev".
MIN_SESSION_SECRET_LEN = 32


def _relaxed_secrets() -> bool:
    """True when the caller is a test suite or explicitly opted out.

    GOSHA_ALLOW_WEAK_SECRET exists so a self-hoster poking at the stack
    locally is not blocked, but it has to be typed on purpose.
    """
    if os.getenv("GOSHA_ALLOW_WEAK_SECRET") == "1":
        return True
    return "PYTEST_CURRENT_TEST" in os.environ


def load_web_settings() -> WebSettings:
    """Build WebSettings from the current environment.

    Raises RuntimeError when a required variable is missing so the API
    fails fast at startup instead of half-working.
    """
    secret = os.getenv("SESSION_SECRET")
    client_id = os.getenv("DISCORD_CLIENT_ID")
    client_secret = os.getenv("DISCORD_CLIENT_SECRET")
    missing = [
        name
        for name, value in (
            ("SESSION_SECRET", secret),
            ("DISCORD_CLIENT_ID", client_id),
            ("DISCORD_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    # The session cookie payload is {"uid": N} over a tiny id space, and
    # admin is an id-membership test — so a guessable secret is a direct
    # path to forging an admin session, not a theoretical weakness.
    # Tests set a short secret deliberately; everything else must not.
    if len(secret) < MIN_SESSION_SECRET_LEN and not _relaxed_secrets():
        raise RuntimeError(
            f"SESSION_SECRET must be at least {MIN_SESSION_SECRET_LEN} "
            "characters. Generate one with: "
            'python -c "import secrets;print(secrets.token_urlsafe(48))"'
        )

    public_base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    redirect_uri = os.getenv(
        "DISCORD_REDIRECT_URI",
        f"{public_base_url}/api/v1/auth/discord/callback",
    )

    guild_raw = os.getenv("DISCORD_GUILD_ID", "0")
    guild_id = int(guild_raw) if guild_raw.isdigit() else 0

    # Explicit override first; the URL scheme is only the default guess.
    # Deriving Secure purely from a config string means a deployment behind
    # a TLS-terminating proxy with an http:// PUBLIC_BASE_URL silently ships
    # session cookies that a downgrade attack can read.
    secure_raw = os.getenv("COOKIE_SECURE", "").strip().lower()
    if secure_raw in ("1", "true", "yes"):
        cookie_secure = True
    elif secure_raw in ("0", "false", "no"):
        cookie_secure = False
    else:
        cookie_secure = public_base_url.startswith("https")

    return WebSettings(
        session_secret=secret,
        discord_client_id=client_id,
        discord_client_secret=client_secret,
        redirect_uri=redirect_uri,
        public_base_url=public_base_url,
        cookie_secure=cookie_secure,
        guild_id=guild_id,
        invite_url=os.getenv("DISCORD_INVITE_URL", ""),
    )


_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?$")
_IP_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
_USERNAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.-]*$")


def _validate_vps_config(index: int, host: str, user: str, port: int, key_path: str) -> bool:
    """Validate VPS config values. Returns True if valid, logs warnings if not."""
    valid = True

    if not (_HOSTNAME_RE.match(host) or _IP_RE.match(host)):
        log.warning("VPS_%d_HOST=%r is not a valid hostname or IP — skipping", index, host)
        valid = False

    if not _USERNAME_RE.match(user):
        log.warning("VPS_%d_USER=%r contains invalid characters — skipping", index, user)
        valid = False

    if not (1024 <= port <= 65535):
        log.warning("VPS_%d_PORT=%d is outside valid range (1024-65535) — skipping", index, port)
        valid = False

    if not key_path or ".." in key_path:
        log.warning("VPS_%d_KEY=%r looks suspicious — skipping", index, key_path)
        valid = False

    return valid


def _parse_vps_list() -> list[VPSConfig]:
    """Parse VPS_* env vars into a list of VPSConfig."""
    global_key = os.getenv("SSH_KEY_PATH", "/home/app/.ssh/id_rsa")
    configs: list[VPSConfig] = []
    for i in range(1, 10):
        host = os.getenv(f"VPS_{i}_HOST")
        if not host:
            continue
        user = os.getenv(f"VPS_{i}_USER", "ubuntu")
        local_port = int(os.getenv(f"VPS_{i}_PORT", str(1079 + i)))
        key_path = os.getenv(f"VPS_{i}_KEY", global_key)

        if not _validate_vps_config(i, host, user, local_port, key_path):
            continue

        configs.append(
            VPSConfig(host=host, user=user, local_port=local_port, key_path=key_path)
        )
    return configs


def _parse_admin_ids() -> set[int]:
    """Parse ADMIN_DISCORD_IDS env var into a set of int."""
    raw = os.getenv("ADMIN_DISCORD_IDS", "")
    ids: set[int] = set()
    for s in raw.split(","):
        s = s.strip()
        if s.isdigit():
            ids.add(int(s))
    return ids


def load_settings() -> Settings:
    """Build a Settings object from the current environment."""
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN environment variable is required but not set. "
            "Set it in your .env file."
        )
    db_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/jobs.db")
    alert_channel = int(os.getenv("ALERT_CHANNEL_ID", "0"))
    interval = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "60"))
    use_semantic = os.getenv("USE_SEMANTIC_MATCHING", "false").lower() == "true"
    semantic_model = os.getenv("SEMANTIC_MODEL", "all-mpnet-base-v2")
    semantic_threshold = float(os.getenv("SEMANTIC_THRESHOLD", "0.40"))

    return Settings(
        discord_token=token,
        database_url=db_url,
        vps_list=_parse_vps_list(),
        scrape_interval_minutes=interval,
        alert_channel_id=alert_channel,
        use_semantic_matching=use_semantic,
        semantic_model=semantic_model,
        semantic_threshold=semantic_threshold,
        admin_user_ids=_parse_admin_ids(),
    )
