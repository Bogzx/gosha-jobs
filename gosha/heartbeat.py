"""Liveness heartbeat for the bot process.

`restart: unless-stopped` catches a process that *exits*. It does nothing
for the failure mode this service actually has: an asyncio loop that wedges
— a scraper coroutine blocked on a socket, a deadlock in the scheduler —
where the container is up, the Discord gateway looks connected, and no jobs
are ever scraped again.

So the scheduler stamps a file on a short interval. If the loop stops
running jobs the stamp goes stale and the Docker healthcheck fails, which
is a signal a human (or a restart policy) can act on.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "bot.heartbeat"
)

# Stamp every 30s; the healthcheck allows several missed stamps before it
# calls the process dead, so a slow cycle is not mistaken for a hang.
HEARTBEAT_INTERVAL_SECONDS = 30
HEARTBEAT_STALE_SECONDS = 300


def heartbeat_path() -> Path:
    raw = os.getenv("GOSHA_HEARTBEAT_FILE")
    return Path(raw) if raw else DEFAULT_HEARTBEAT_PATH


def touch() -> None:
    """Record that the event loop is still running scheduled work."""
    path = heartbeat_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not write heartbeat to %s: %s", path, exc)


def age_seconds() -> float | None:
    """Seconds since the last stamp, or None when there has never been one."""
    path = heartbeat_path()
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def is_alive(max_age: float = HEARTBEAT_STALE_SECONDS) -> bool:
    age = age_seconds()
    return age is not None and age <= max_age
