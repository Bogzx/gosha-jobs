"""Ops alerts that reach the owner — by Discord DM only.

Detectors (gosha/scrape_health.py, the scheduler listener in main.py)
decide *what* is wrong. This module decides *who hears about it and how
often*:

* Every alert is a DM to each ID in ALERT_DM_USER_IDS (the owner). No
  channel posts, no @mentions, admins are not added automatically.
  ALERTS_ENABLED=false, or an empty ALERT_DM_USER_IDS, turns alerts off.
* Alerts are conditions with a key ("scrape:dead:bestjobs"), not one-off
  messages. A condition that stays bad is repeated once per cooldown
  (ALERT_COOLDOWN_MINUTES), not once per cycle, and when the detector
  reports it healthy again the owner gets a "recovered" message. A
  condition the detector cannot judge (no baseline yet, right after a
  restart) is neither raised nor cleared.
* State survives a restart when a state file is attached (main.py does),
  so a deploy in the middle of an outage does not re-page the owner.

The out-of-process half needs no healthy bot: post_dm_sync() DMs over
Discord's REST API with plain urllib (used by the heartbeat watchdog and
scripts/site_watchdog.py / .github/workflows/site-watchdog.yml).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

# Discord rejects messages over 2000 characters.
DISCORD_LIMIT = 2000
DEFAULT_COOLDOWN_SECONDS = 6 * 3600


class AlertSink(Protocol):
    async def send(self, text: str) -> None: ...


@dataclass
class _Active:
    since: float
    last_sent: float
    text: str


@dataclass
class Dispatch:
    """What one update() decided to say."""

    raised: list[str] = field(default_factory=list)
    reminded: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.raised or self.reminded or self.recovered)


def _fmt_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    return f"{hours // 24}d{hours % 24}h"


class Alerter:
    """Keyed alert state with cooldown and recovery messages."""

    def __init__(
        self,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        state_path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self.state_path = state_path
        self.active: dict[str, _Active] = {}
        if state_path is not None:
            self._load()

    # ── persistence ──────────────────────────────────────────────
    def attach_state_file(self, path: Path) -> None:
        self.state_path = path
        self._load()

    def _load(self) -> None:
        if self.state_path is None:
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.active = {
                key: _Active(float(v["since"]), float(v["last_sent"]), str(v.get("text", "")))
                for key, v in raw.get("active", {}).items()
            }
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("Ignoring unreadable alert state %s: %s", self.state_path, exc)

    def _save(self) -> None:
        if self.state_path is None:
            return
        data = {
            "active": {
                key: {"since": a.since, "last_sent": a.last_sent, "text": a.text}
                for key, a in self.active.items()
            }
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as exc:
            log.warning("Could not persist alert state to %s: %s", self.state_path, exc)

    # ── decisions ────────────────────────────────────────────────
    def decide(
        self, failing: Mapping[str, str], cleared: Mapping[str, str] | Iterable[str] = (),
    ) -> Dispatch:
        """Fold one detector run into the state; return what to say.

        `failing` maps condition key → message for everything bad right
        now. `cleared` holds keys positively known to be healthy (a mapping
        to a recovery message, or bare keys). Keys in neither are left
        alone — "can't tell" must not page anyone, nor resolve anything.
        """
        now = self.clock()
        out = Dispatch()
        for key, text in failing.items():
            current = self.active.get(key)
            if current is None:
                self.active[key] = _Active(since=now, last_sent=now, text=text)
                out.raised.append(text)
            elif now - current.last_sent >= self.cooldown_seconds:
                current.last_sent = now
                current.text = text
                out.reminded.append(
                    f"{text} _(still failing, first seen {_fmt_duration(now - current.since)} ago)_"
                )
            else:
                current.text = text
        cleared_map = cleared if isinstance(cleared, Mapping) else {k: "" for k in cleared}
        for key, text in cleared_map.items():
            if key in failing:
                continue
            current = self.active.pop(key, None)
            if current is None:
                continue
            message = text or f"Recovered: {current.text}"
            out.recovered.append(
                f"{message} _(was failing for {_fmt_duration(now - current.since)})_"
            )
        self._save()
        return out

    async def update(
        self,
        sink: AlertSink | None,
        failing: Mapping[str, str],
        cleared: Mapping[str, str] | Iterable[str] = (),
        title: str = "Alert",
    ) -> Dispatch:
        """decide(), then send one combined message for everything new."""
        dispatch = self.decide(failing, cleared)
        if dispatch and sink is not None:
            await sink.send(format_dispatch(title, dispatch))
        return dispatch


def format_dispatch(title: str, dispatch: Dispatch) -> str:
    lines: list[str] = []
    problems = dispatch.raised + dispatch.reminded
    if problems:
        lines.append(f"🚨 **GOSHA — {title}**")
        lines.extend(f"• {p}" for p in problems)
    if dispatch.recovered:
        if lines:
            lines.append("")
        lines.append(f"✅ **GOSHA — {title}: recovered**")
        lines.extend(f"• {r}" for r in dispatch.recovered)
    return "\n".join(lines)


def _clip(text: str, limit: int = DISCORD_LIMIT - 50) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── Transport (DM only) ──────────────────────────────────────────────


def alerts_enabled() -> bool:
    """ALERTS_ENABLED (default on) — the one switch for every alert path."""
    raw = os.getenv("ALERTS_ENABLED", "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def alert_recipients(settings: Any) -> list[int]:
    """Owner IDs to DM (ALERT_DM_USER_IDS), or [] when alerts are off."""
    if settings is None or not getattr(settings, "alerts_enabled", True):
        return []
    return sorted(set(getattr(settings, "alert_dm_user_ids", set()) or set()))


class DiscordAlertSink:
    """DMs each owner. Never posts to a channel, never raises.

    One recipient with closed DMs or a Discord hiccup costs that DM only.
    """

    def __init__(self, bot: Any, user_ids: Iterable[int]) -> None:
        self.bot = bot
        self.user_ids = list(dict.fromkeys(user_ids))

    async def send(self, text: str) -> None:
        delivered = False
        for uid in self.user_ids:
            delivered |= await self._dm(uid, _clip(text))
        if not delivered:
            # The alert reached nobody. The log is all that is left.
            log.error("Alert reached no one (every DM failed): %s", text)

    async def _dm(self, user_id: int, text: str) -> bool:
        try:
            user = self.bot.get_user(user_id) if hasattr(self.bot, "get_user") else None
            if user is None and hasattr(self.bot, "fetch_user"):
                user = await self.bot.fetch_user(user_id)
            if user is None:
                log.error("Alert recipient %s not found", user_id)
                return False
            await user.send(text)
            return True
        except Exception as exc:
            # Typically 50007: the owner shares no server with the bot or
            # has DMs from server members turned off.
            log.error("Failed to DM alert to %s: %s", user_id, exc)
            return False


def sink_for_bot(bot: Any) -> DiscordAlertSink | None:
    """DM sink for the owners in bot.settings; None when alerts are off."""
    if bot is None:
        return None
    recipients = alert_recipients(getattr(bot, "settings", None))
    if not recipients:
        return None
    return DiscordAlertSink(bot, recipients)


# ── Process-wide alerter ─────────────────────────────────────────────

_alerter: Alerter | None = None


def get_alerter() -> Alerter:
    global _alerter
    if _alerter is None:
        _alerter = Alerter()
    return _alerter


def reset_alerter() -> None:
    global _alerter
    _alerter = None


async def publish_scrape_report(bot: Any, alert_channel_id: int, report: Any) -> Dispatch:
    """Pipeline hook: route a scrape-cycle report through the alerter.

    Never raises — alerting must not cost a cycle its matching/delivery.
    Also leaves a `scrape.cycle` event, which is what the public freshness
    endpoint (gosha/api/freshness.py) reads to tell a live scraper from a
    dead one without access to this process.
    """
    if report is None:
        return Dispatch()
    try:
        from gosha.events import get_event_store

        await get_event_store().emit(
            "scrape.cycle",
            payload={"total": int(report.total), "sources": len(report.per_source)},
        )
    except Exception as exc:
        log.warning("Could not record the scrape.cycle event: %s", exc)
    try:
        dispatch = get_alerter().decide(report.conditions, report.cleared)
        sink = sink_for_bot(bot)  # DM only; the channel id is unused
        if dispatch and sink is not None:
            text = format_dispatch("Scraper health", dispatch)
            breakdown = report.breakdown()
            if breakdown:
                text += f"\nscraped→kept: `{breakdown}`"
            await sink.send(text)
        return dispatch
    except Exception as exc:
        log.error("Failed to publish scraper-health alert: %s", exc)
        return Dispatch()


def scheduler_event_conditions(
    event: Any, alert_jobs: Iterable[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """APScheduler event → (failing, cleared) for the jobs worth paging on.

    One key per job, so a scrape cycle that raises every hour is one alert
    plus a reminder per cooldown, and its next clean run says "recovered".
    """
    from apscheduler.events import (
        EVENT_JOB_ERROR,
        EVENT_JOB_EXECUTED,
        EVENT_JOB_MAX_INSTANCES,
    )

    job_id = getattr(event, "job_id", "?")
    if job_id not in set(alert_jobs):
        return {}, {}
    key = f"scheduler:{job_id}"
    code = getattr(event, "code", None)
    if code == EVENT_JOB_EXECUTED:
        return {}, {key: f"Scheduled job `{job_id}` ran cleanly again."}
    if code == EVENT_JOB_ERROR:
        text = f"Scheduled job `{job_id}` failed: `{getattr(event, 'exception', '?')!r}`"
    elif code == EVENT_JOB_MAX_INSTANCES:
        text = f"Scheduled job `{job_id}` skipped — the previous run is still going."
    else:
        text = (
            f"Scheduled job `{job_id}` missed its run time — the previous run is "
            f"still going or the event loop is blocked."
        )
    return {key: _clip(text, 1500)}, {}


# ── Out-of-process DM (works when discord.py / the event loop does not) ──

DISCORD_API = "https://discord.com/api/v10"


def _parse_ids(raw: str) -> list[int]:
    return [int(s) for s in (p.strip() for p in raw.split(",")) if s.isdigit()]


def _discord_post(path: str, token: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        f"{DISCORD_API}{path}", data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            # Discord rejects bot requests without a DiscordBot user agent.
            "User-Agent": "DiscordBot (https://github.com/Bogzx/gosha-jobs, 1)",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def post_dm_sync(
    text: str,
    token: str | None = None,
    user_ids: Iterable[int] | None = None,
    timeout: float = 10.0,
) -> bool:
    """DM `text` to each owner over Discord's REST API with plain urllib.

    For callers that cannot use discord.py: the heartbeat watchdog (the
    loop is wedged) and the GitHub Actions watchdogs (no bot process).
    Opens the DM channel (POST /users/@me/channels) then posts to it.
    Defaults: DISCORD_TOKEN, ALERT_DM_USER_IDS; ALERTS_ENABLED=false turns
    it off. True when at least one DM went out. Never raises.
    """
    if not alerts_enabled():
        return False
    token = (token if token is not None else os.getenv("DISCORD_TOKEN", "")).strip()
    ids = list(user_ids) if user_ids is not None else _parse_ids(
        os.getenv("ALERT_DM_USER_IDS", "")
    )
    if not token or not ids:
        return False
    delivered = False
    for uid in ids:
        try:
            channel = _discord_post("/users/@me/channels", token, {"recipient_id": str(uid)}, timeout)
            _discord_post(
                f"/channels/{channel['id']}/messages", token,
                {"content": _clip(text), "allowed_mentions": {"parse": []}}, timeout,
            )
            delivered = True
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
            log.error("Discord DM alert to %s failed: %s", uid, exc)
    return delivered
