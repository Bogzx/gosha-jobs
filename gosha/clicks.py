"""Apply-click tracking for Discord DMs, and the owner's weekly click report.

A DM's Apply button links to GOSHA, not straight to the posting:

    {PUBLIC_BASE_URL}/api/v1/r/<token>

The token is the delivery's UserJob id signed with SESSION_SECRET
(itsdangerous, salt "gosha-apply"). Nothing else is in it — the posting URL
is looked up on the server from that delivery's Job — so the redirect can
only ever lead to a posting GOSHA itself sent that user: no open redirect,
and ids cannot be guessed or walked. Following it records one ApplyClick
(user, job, search, time; no IP, no user agent) and answers 302 to the
posting. Discord's link unfurler, mail scanners and double clicks hit links
too, so a delivery counts at most one click per minute.

When the bot cannot sign (no SESSION_SECRET, PUBLIC_BASE_URL unset) the
button falls back to the posting URL itself: tracking is optional, the DM
is not.

The owner gets one aggregate DM a week (weekly_click_report_tick): DMs
sent, clicks and distinct jobs clicked per search over the last 7 days.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SALT = "gosha-apply"
ROUTE = "/api/v1/r/"
DEDUP_SECONDS = 60
MAX_TOKEN_CHARS = 200


def _is_web_url(url: object) -> bool:
    return isinstance(url, str) and url.lower().startswith(("http://", "https://"))


def _serializer():
    """URLSafeSerializer over SESSION_SECRET, or None when it is not set up.

    load_web_settings() applies the same secret rules as the website (no
    weak secret outside tests). The bot container reads the same .env.
    """
    try:
        from itsdangerous import URLSafeSerializer

        from gosha.config import load_web_settings

        return URLSafeSerializer(load_web_settings().session_secret, salt=SALT)
    except Exception as exc:
        log.debug("Apply-click signing unavailable: %s", exc)
        return None


def token_for(user_job_id: int) -> str | None:
    serializer = _serializer()
    return None if serializer is None else serializer.dumps(int(user_job_id))


def user_job_from(token: str) -> int | None:
    """The UserJob id a token was signed for, or None (forged, mangled)."""
    if not token or len(token) > MAX_TOKEN_CHARS:
        return None
    serializer = _serializer()
    if serializer is None:
        return None
    try:
        from itsdangerous import BadData

        value = serializer.loads(token)
    except BadData:
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def apply_url(user_job_id: int | None, job_url: str | None) -> str | None:
    """The URL a DM's Apply button opens: the tracked redirect when
    possible, else the posting itself (never worse than before)."""
    if user_job_id is None or not _is_web_url(job_url):
        return job_url
    base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not _is_web_url(base):
        return job_url
    token = token_for(user_job_id)
    return job_url if token is None else f"{base}{ROUTE}{token}"


async def record_click(user_job_id: int, now: datetime | None = None) -> str | None:
    """Record a click on delivery `user_job_id`; return the posting URL to
    redirect to, or None when there is nothing safe to send the user to."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from gosha.database import get_session
    from gosha.models import ApplyClick, Job, UserJob

    now = now or datetime.now(timezone.utc)
    async with get_session() as session:
        row = (await session.execute(
            select(UserJob, Job).join(Job, Job.id == UserJob.job_id)
            .where(UserJob.id == user_job_id)
        )).first()
        if row is None:
            return None
        uj, job = row
        target = job.url  # read now: a rollback below expires the objects
        if not _is_web_url(target):
            return None
        recent = (await session.execute(
            select(ApplyClick.id).where(
                ApplyClick.user_job_id == uj.id,
                ApplyClick.clicked_at >= now - timedelta(seconds=DEDUP_SECONDS),
            ).limit(1)
        )).first()
        if recent is None:
            session.add(ApplyClick(
                user_id=uj.user_id, job_id=uj.job_id, user_job_id=uj.id,
                subscription_id=uj.subscription_id, clicked_at=now,
                click_minute=int(now.timestamp() // 60),
            ))
            try:
                await session.commit()
            except IntegrityError:
                # A concurrent hit in the same minute already counted it.
                await session.rollback()
        return target


# ── Weekly owner report ──────────────────────────────────────────────

REPORT_WEEKDAY = 0  # Monday
REPORT_HOUR_UTC = 8
REPORT_WINDOW = timedelta(days=7)
REPORT_MAX_LINES = 25


def report_slot(now: datetime) -> datetime:
    """The most recent Monday 08:00 UTC at or before `now`."""
    now = now.astimezone(timezone.utc)
    slot = (now - timedelta(days=(now.weekday() - REPORT_WEEKDAY) % 7)).replace(
        hour=REPORT_HOUR_UTC, minute=0, second=0, microsecond=0,
    )
    return slot if slot <= now else slot - timedelta(days=7)


def report_due(now: datetime, last_sent: datetime | None) -> bool:
    """Due once per weekly slot: checked hourly, so neither a deploy (which
    restarts interval timers) nor a bot that was down at 08:00 skips a week,
    and a restart after sending does not send it twice."""
    return last_sent is None or last_sent < report_slot(now)


def _search_label(sub: Any) -> str:
    keywords = ", ".join(sub.keywords[:3]) or "?"
    places = ", ".join(sub.locations[:2])
    name = f"{sub.name} — " if getattr(sub, "name", None) else ""
    return f"#{sub.id} {name}{keywords}" + (f" in {places}" if places else "")


async def build_weekly_report(now: datetime | None = None) -> str:
    """Owner-only aggregate: per search, DMs sent / clicks / jobs clicked."""
    from sqlalchemy import distinct, func, select

    from gosha.database import get_session
    from gosha.models import ApplyClick, Subscription, UserJob

    now = now or datetime.now(timezone.utc)
    since = now - REPORT_WINDOW
    async with get_session() as session:
        sent = dict((await session.execute(
            select(UserJob.subscription_id, func.count(UserJob.id))
            .where(UserJob.dm_sent_at >= since, UserJob.dm_sent_at < now)
            .group_by(UserJob.subscription_id)
        )).all())
        clicks = {
            sub_id: (int(n), int(jobs)) for sub_id, n, jobs in (await session.execute(
                select(ApplyClick.subscription_id, func.count(ApplyClick.id),
                       func.count(distinct(ApplyClick.job_id)))
                .where(ApplyClick.clicked_at >= since, ApplyClick.clicked_at < now)
                .group_by(ApplyClick.subscription_id)
            )).all()
        }
        ids = [i for i in set(sent) | set(clicks) if i is not None]
        subs = {
            s.id: s for s in (await session.execute(
                select(Subscription).where(Subscription.id.in_(ids))
            )).scalars()
        } if ids else {}

    total_sent = sum(int(n) for n in sent.values())
    total_clicks = sum(n for n, _ in clicks.values())
    rate = f" ({100 * total_clicks / total_sent:.0f}% of DMs)" if total_sent else ""
    lines = [
        f"📊 **GOSHA — weekly Apply clicks** ({since:%d %b} – {now:%d %b})",
        f"{total_sent} job DMs sent, {total_clicks} Apply clicks{rate}.",
    ]
    rows = sorted(
        set(sent) | set(clicks),
        key=lambda i: (-int(sent.get(i, 0)), -clicks.get(i, (0, 0))[0], i is None, i or 0),
    )
    for sub_id in rows[:REPORT_MAX_LINES]:
        n_sent = int(sent.get(sub_id, 0))
        n_clicks, n_jobs = clicks.get(sub_id, (0, 0))
        label = (
            _search_label(subs[sub_id]) if sub_id in subs
            else "deleted search" if sub_id is not None else "no search recorded"
        )
        lines.append(f"• {label}: {n_sent} sent, {n_clicks} clicks on {n_jobs} jobs")
    if len(rows) > REPORT_MAX_LINES:
        lines.append(f"…and {len(rows) - REPORT_MAX_LINES} more searches.")
    return "\n".join(lines)


def _read_stamp(path: Path) -> datetime | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        value = datetime.fromisoformat(raw["last_sent"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _write_stamp(path: Path, when: datetime) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_sent": when.isoformat()}), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not record the weekly click report time in %s: %s", path, exc)


async def weekly_click_report_tick(
    bot: Any, stamp_path: Path, now: datetime | None = None,
) -> bool:
    """Hourly: DM the owner the weekly report when its slot is due.

    Through alerts.sink_for_bot: owner IDs only, off with ALERTS_ENABLED.
    The stamp is written only after a delivered DM, so a failed one is
    retried next hour. True when the report went out.
    """
    from gosha.alerts import sink_for_bot

    now = now or datetime.now(timezone.utc)
    if not report_due(now, _read_stamp(stamp_path)):
        return False
    sink = sink_for_bot(bot)
    if sink is None:
        return False
    text = await build_weekly_report(now)
    if await sink.send(text) is False:
        return False
    _write_stamp(stamp_path, now)
    return True
