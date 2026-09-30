"""Encryption at rest for stored CV text.

CVs are the most sensitive thing GOSHA holds, and they used to sit on disk
as plain `data/cvs/<uid>.txt`, bind-mounted read-write into two containers
and copied into every backup. They are now Fernet-encrypted (AES-128-CBC +
HMAC-SHA256) with a key from the environment; see gosha/cover_letter.py for
the file layout and the transparent migration of old plaintext files.

Configuration:

    CV_ENCRYPTION_KEY=<key>[,<old key>...]

The first key encrypts; every listed key is tried for decryption, so a key
is rotated by prepending the new one and later dropping the old. Generate a
key with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Without a key the service refuses to start (require_key_configured), unless
GOSHA_ENV is development/dev/test or the test suite is running: plaintext
storage is a local-development convenience, never a production fallback.
Losing the key makes every stored CV unreadable — back it up separately
from the data.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

KEY_ENV = "CV_ENCRYPTION_KEY"
_DEV_ENVS = {"development", "dev", "test"}


class CvKeyError(RuntimeError):
    """The CV key is missing where it is required, malformed, or wrong."""


def plaintext_allowed() -> bool:
    """True only for local development and the test suite."""
    if os.getenv("GOSHA_ENV", "").strip().lower() in _DEV_ENVS:
        return True
    return "PYTEST_CURRENT_TEST" in os.environ


def cipher() -> MultiFernet | None:
    """The configured cipher, or None when no key is set.

    Read on every call: it is cheap, and it keeps a key change (or a test's
    monkeypatch) effective without a process-level cache to invalidate.
    """
    raw = os.getenv(KEY_ENV, "").strip()
    if not raw:
        return None
    try:
        return MultiFernet([Fernet(k.strip()) for k in raw.split(",") if k.strip()])
    except (ValueError, TypeError) as exc:
        raise CvKeyError(
            f"{KEY_ENV} is not a valid Fernet key (32 url-safe base64 bytes). "
            f"Generate one with: python -c \"from cryptography.fernet import "
            f"Fernet; print(Fernet.generate_key().decode())\""
        ) from exc


def require_key_configured() -> None:
    """Fail fast at startup when CVs would otherwise be stored in the clear."""
    if cipher() is None and not plaintext_allowed():
        raise CvKeyError(
            f"{KEY_ENV} is not set. CVs are encrypted at rest and GOSHA will "
            "not store them in plaintext outside development. Set the key in "
            ".env for BOTH the bot and api services (same value), or set "
            "GOSHA_ENV=development for a local, plaintext setup."
        )


def encrypt(text: str) -> bytes:
    c = cipher()
    if c is None:
        raise CvKeyError(f"{KEY_ENV} is not set; cannot encrypt a CV.")
    return c.encrypt(text.encode("utf-8"))


def decrypt(token: bytes) -> str:
    c = cipher()
    if c is None:
        raise CvKeyError(
            f"This CV is encrypted but {KEY_ENV} is not set in this process."
        )
    try:
        return c.decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise CvKeyError(
            f"A stored CV could not be decrypted with any key in {KEY_ENV} — "
            "wrong or rotated-out key?"
        ) from exc
