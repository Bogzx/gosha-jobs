"""Liveness heartbeat for the bot process.

`restart: unless-stopped` catches a process that *exits*. It does nothing
for the failure mode this service actually has: an asyncio loop that wedges
— a scraper coroutine blocked on a socket, a deadlock in the scheduler —
where the container is up, the Discord gateway looks connected, and no jobs
are ever scraped again.

So the scheduler stamps a file on a short interval. If the loop stops
running jobs the stamp goes stale and the Docker healthcheck marks the
container unhealthy. That is only a label: plain Docker (no Swarm, no
orchestrator) never restarts an unhealthy container, and nothing else here
acts on it. The watchdog below closes the gap. A daemon thread, which keeps
running when the event loop does not, notices that the stamps stopped and
exits the process, which the restart policy *does* act on.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
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

# The watchdog is the blunt instrument, so it waits three times longer than
# the healthcheck: an unhealthy label is cheap, a restart drops the Discord
# session and an in-flight scrape. GOSHA_WATCHDOG_SECONDS overrides it;
# 0 turns the watchdog off.
WATCHDOG_STALE_SECONDS = 900
WATCHDOG_POLL_SECONDS = 30

# monotonic() of the last touch() in this process. The watchdog reads this
# rather than the file, so a data directory it cannot write to (a warning
# in touch()) never looks like a hang, and a stale file left by a previous
# container never counts as this process's stamp.
_last_beat: float | None = None


def heartbeat_path() -> Path:
    raw = os.getenv("GOSHA_HEARTBEAT_FILE")
    return Path(raw) if raw else DEFAULT_HEARTBEAT_PATH


def touch() -> None:
    """Record that the event loop is still running scheduled work."""
    global _last_beat
    _last_beat = time.monotonic()
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


def watchdog_seconds() -> float:
    raw = os.getenv("GOSHA_WATCHDOG_SECONDS", "").strip()
    if not raw:
        return WATCHDOG_STALE_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning("GOSHA_WATCHDOG_SECONDS=%r is not a number — using %ds",
                    raw, WATCHDOG_STALE_SECONDS)
        return WATCHDOG_STALE_SECONDS


def _page_owner(stale: float, limit: float) -> None:
    """DM the owner before exiting, through the one path that works now.

    The loop is wedged, so discord.py cannot send anything; a plain-urllib
    call to Discord's REST API with the bot token can (ALERT_DM_USER_IDS,
    off with ALERTS_ENABLED=false). Bounded by a short timeout so a dead
    network cannot keep the wedged process alive.
    """
    try:
        from gosha.alerts import post_dm_sync

        post_dm_sync(
            f"🚨 **GOSHA bot watchdog**: no heartbeat for {stale:.0f}s (limit "
            f"{limit:.0f}s) — the event loop stopped running scheduled jobs. "
            "Restarting the bot; the thread dump is in its log "
            "(`docker compose logs bot`).",
            timeout=5.0,
        )
    except Exception as exc:  # exiting regardless
        log.error("Watchdog could not page the owner: %s", exc)


def start_watchdog(
    max_age: float | None = None, poll: float = WATCHDOG_POLL_SECONDS
) -> threading.Thread | None:
    """Exit the process when touch() has not run for `max_age` seconds.

    Call once, after the first touch(). Until a first stamp exists the
    watchdog only waits. The stamp is taken before the Discord login, so a
    gateway that never connects (the scheduler starts in on_ready) also
    ends in a restart once `max_age` has passed. Returns the thread, or None
    when disabled (GOSHA_WATCHDOG_SECONDS=0).
    """
    limit = watchdog_seconds() if max_age is None else max_age
    if limit <= 0:
        log.info("Bot watchdog disabled (GOSHA_WATCHDOG_SECONDS=0)")
        return None

    def _watch() -> None:
        while True:
            time.sleep(poll)
            last = _last_beat
            if last is None:
                continue
            stale = time.monotonic() - last
            if stale <= limit:
                continue
            log.critical(
                "Watchdog: no heartbeat for %.0fs (limit %.0fs); the event loop "
                "has stopped running scheduled jobs. Dumping every thread's "
                "stack, then exiting so the container restart policy brings "
                "the bot back.", stale, limit,
            )
            # The stack of the wedged loop is the whole diagnosis; without
            # it a restart erases the evidence.
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            _page_owner(stale, limit)
            for handler in logging.getLogger().handlers:
                try:
                    handler.flush()
                except Exception:  # exiting regardless
                    pass
            # _exit, not sys.exit: this is not the main thread, and the main
            # thread's cleanup is exactly what is stuck.
            os._exit(1)

    thread = threading.Thread(target=_watch, name="heartbeat-watchdog", daemon=True)
    thread.start()
    log.info("Bot watchdog armed: exit after %.0fs without a heartbeat", limit)
    return thread
