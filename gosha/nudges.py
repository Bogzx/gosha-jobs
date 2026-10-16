"""Monthly "this search finds nothing" DM to the search's owner.

A saved search that has not delivered a single job DM for 30 days is
usually too narrow (one city, one level, a rare keyword) and its owner
does not know. Once per 30 days per search, its owner gets a short DM:
which search, why it may be empty, two concrete changes picked from its
own settings, and how to edit it.

The 30 days count from the latest of: the search's creation, the moment
this module first saw it in its current form (an edit or pause/resume
restarts the clock — see SearchNudge.fingerprint), its last delivered DM
and its last nudge. "First saw it" also means the first run after the
deploy that introduces this starts every existing search's clock: searches
starved by bugs that deploy fixes get 30 days to recover before anyone is
told they are empty.

Off unless EMPTY_SEARCH_NUDGE_ENABLED=true. The clocks run either way (the
hourly tick only reads the database and stamps first sightings), so turning
it on later does not wait another 30 days. DMs go out 09:00–20:00 Romanian
time only, at most MAX_PER_RUN per run. A closed DM is recorded and not
retried until the next window; a Discord hiccup is retried next hour.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

EMPTY_FOR = timedelta(days=30)
MAX_PER_RUN = 20
LOCAL_TZ = ZoneInfo("Europe/Bucharest")
SEND_HOURS = range(9, 20)  # local hour of day

SENT = "sent"
CLOSED = "closed"
RETRY = "retry"

_BROAD_PLACES = ("romania", "românia", "remote", "anywhere", "worldwide")


def fingerprint(sub: Any) -> str:
    """Hash of what decides which jobs a search finds, plus paused state."""
    criteria = {
        "keywords": sorted(k.lower() for k in sub.keywords),
        "locations": sorted(loc.lower() for loc in sub.locations),
        "levels": sorted(sub.experience_levels),
        "excluded": sorted(e.lower() for e in sub.excluded_keywords),
        "blacklist": sorted(c.lower() for c in sub.company_blacklist),
        "boards": sorted(sub.boards),
        "remote_ok": bool(sub.remote_ok),
        "salary_min": sub.salary_min,
        "max_age_days": sub.max_age_days,
        "active": bool(sub.is_active),
        "notify": bool(sub.notify_discord),
    }
    return hashlib.sha256(json.dumps(criteria, sort_keys=True).encode()).hexdigest()[:32]


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def suggestions(sub: Any) -> list[tuple[str, str]]:
    """(why it may be empty, what to change) pairs, most useful first."""
    out: list[tuple[str, str]] = []
    levels = {lvl.lower() for lvl in sub.experience_levels}
    if levels and "any" not in levels and "junior" not in levels:
        out.append((
            f"it only accepts the {', '.join(sorted(levels))} level, and many postings "
            f"are labelled junior or not labelled at all",
            "add the **junior** level, or set experience to **any**",
        ))
    places = [loc.lower() for loc in sub.locations]
    if not any(broad in place for place in places for broad in _BROAD_PLACES):
        where = ", ".join(sub.locations[:2]) or "one place"
        out.append((
            f"it only looks in {where}, and many postings name just the country or are remote",
            "add **Romania** or **remote** as a location",
        ))
    if sub.excluded_keywords:
        words = ", ".join(f"“{w}”" for w in sub.excluded_keywords[:3])
        out.append((
            f"every posting that mentions an excluded word ({words}) is dropped",
            "remove some excluded words",
        ))
    if sub.salary_min:
        out.append((
            "few postings state a salary, and a minimum can rule out good ones",
            "lower or remove the minimum salary",
        ))
    if sub.max_age_days and sub.max_age_days < 14:
        out.append((
            f"it only takes postings from the last {sub.max_age_days} days",
            "allow older postings (e.g. 14 or 30 days)",
        ))
    keywords = ", ".join(f"“{k}”" for k in sub.keywords[:3]) or "its keywords"
    out.append((
        f"few postings use the exact words {keywords}",
        "try a broader or more common job title (e.g. “developer” rather than a "
        "specific framework)",
    ))
    return out


def nudge_text(sub: Any, site: str | None = None) -> str:
    """The DM. Short: the search, why it may be empty, two changes, how."""
    keywords = ", ".join(sub.keywords[:3]) or "your keywords"
    places = ", ".join(sub.locations[:3])
    label = f"**#{sub.id}** “{keywords}”" + (f" in {places}" if places else "")
    picks = suggestions(sub)[:2]
    lines = [
        f"👋 Your GOSHA search {label} hasn't found a new job for you in 30 days.",
        "",
        f"It may be empty because {picks[0][0]}.",
        "Two changes that usually help:",
        *(f"• {change[0].upper()}{change[1:]}" for _, change in picks),
        "",
        f"Edit it with `/edit id:{sub.id}` here, `/my_searches` lists them all"
        + (f", or on {site}/searches" if site else "") + ".",
        f"Not looking any more? `/pause id:{sub.id}`. "
        "This reminder comes at most once a month per search.",
    ]
    return "\n".join(lines)


async def _dm_text(bot: Any, discord_id: int, text: str) -> str:
    """SENT, CLOSED (DMs closed / user gone) or RETRY. Never raises."""
    try:
        import discord

        forbidden: tuple[type[BaseException], ...] = (discord.Forbidden, discord.NotFound)
    except Exception:  # discord.py missing: nothing is closed, only failing
        forbidden = ()
    try:
        user = bot.get_user(discord_id) if hasattr(bot, "get_user") else None
        if user is None:
            user = await bot.fetch_user(discord_id)
        await user.send(text)
        return SENT
    except forbidden:
        log.warning("Empty-search nudge: cannot DM user %d (DMs closed)", discord_id)
        return CLOSED
    except Exception as exc:
        log.error("Empty-search nudge to %d failed (retrying next run): %s", discord_id, exc)
        return RETRY


@dataclass
class NudgeRun:
    first_seen: int = 0
    edited: int = 0
    due: list[int] = field(default_factory=list)
    sent: list[int] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)


async def run_empty_search_nudges(
    bot: Any, enabled: bool, now: datetime | None = None, max_per_run: int = MAX_PER_RUN,
) -> NudgeRun:
    """One hourly pass: advance every search's clock; DM the due ones when
    `enabled` and inside the sending hours."""
    from sqlalchemy import func, select

    from gosha.database import get_session
    from gosha.models import SearchNudge, Subscription, User, UserJob

    now = now or datetime.now(timezone.utc)
    run = NudgeRun()
    due: list[tuple[Subscription, int]] = []
    async with get_session() as session:
        subs = (await session.execute(
            select(Subscription, User.discord_user_id).join(User, User.id == Subscription.user_id)
        )).all()
        states = {s.subscription_id: s for s in (await session.execute(select(SearchNudge))).scalars()}
        last_dm = dict((await session.execute(
            select(UserJob.subscription_id, func.max(UserJob.dm_sent_at))
            .where(UserJob.dm_sent_at.isnot(None))
            .group_by(UserJob.subscription_id)
        )).all())
        for sub, discord_id in subs:
            fp = fingerprint(sub)
            state = states.get(sub.id)
            if state is None:
                session.add(SearchNudge(subscription_id=sub.id, fingerprint=fp, watched_since=now))
                run.first_seen += 1
                continue
            if state.fingerprint != fp:
                state.fingerprint, state.watched_since = fp, now
                run.edited += 1
                continue
            if not (sub.is_active and sub.notify_discord):
                continue
            start = max(
                t for t in (
                    _aware(sub.created_at), _aware(state.watched_since),
                    _aware(last_dm.get(sub.id)), _aware(state.nudged_at),
                ) if t is not None
            )
            if now - start >= EMPTY_FOR:
                due.append((sub, int(discord_id)))
                run.due.append(sub.id)
        await session.commit()

    if not enabled or bot is None or not due:
        return run
    if now.astimezone(LOCAL_TZ).hour not in SEND_HOURS:
        return run

    site = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/") or None
    for sub, discord_id in due[:max_per_run]:
        outcome = await _dm_text(bot, discord_id, nudge_text(sub, site))
        if outcome == RETRY:
            run.failed.append(sub.id)
            continue
        (run.sent if outcome == SENT else run.closed).append(sub.id)
        async with get_session() as session:
            state = await session.get(SearchNudge, sub.id)
            if state is not None:
                state.nudged_at, state.outcome = now, outcome
                await session.commit()
    if run.sent or run.closed:
        log.info("Empty-search nudges: %d sent, %d to closed DMs, %d failed",
                 len(run.sent), len(run.closed), len(run.failed))
    return run
