"""Weekly Apply-click report: owner DM only, once per week, aggregate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gosha import clicks
from gosha.alerts import reset_alerter
from gosha.models import ApplyClick, Job, Subscription, User, UserJob
from tests.test_alerts import OWNER, FakeBot

MONDAY_9 = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)  # a Monday


@pytest.fixture(autouse=True)
def _fresh():
    reset_alerter()
    yield
    reset_alerter()


def test_report_slot_is_the_last_monday_8_utc():
    assert clicks.report_slot(MONDAY_9) == MONDAY_9.replace(hour=8)
    assert clicks.report_slot(MONDAY_9.replace(hour=7)) == datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
    assert clicks.report_slot(datetime(2026, 10, 22, 3, tzinfo=timezone.utc)) == MONDAY_9.replace(hour=8)


def test_due_once_per_slot_whatever_the_deploys():
    assert clicks.report_due(MONDAY_9, None)
    assert not clicks.report_due(MONDAY_9 + timedelta(hours=1), MONDAY_9)  # restarted after sending
    assert clicks.report_due(MONDAY_9 + timedelta(days=7), MONDAY_9)
    # Bot down at 08:00 Monday: sent when it is back, still that week.
    assert clicks.report_due(MONDAY_9 + timedelta(days=2), MONDAY_9 - timedelta(days=7))


async def _seed(session, now):
    user = User(discord_user_id=4242, username="private-name")
    session.add(user)
    await session.flush()
    subs = []
    for kw in (["python"], ["java"]):
        sub = Subscription(user_id=user.id, max_age_days=7, name=None)
        sub.keywords, sub.locations = kw, ["Cluj"]
        session.add(sub)
        subs.append(sub)
    jobs = [Job(url=f"https://example.com/{i}", title="Dev", company="A", source="ejobs")
            for i in range(4)]
    session.add_all(jobs)
    await session.flush()
    ujs = []
    for i, job in enumerate(jobs):
        sub = subs[0] if i < 3 else subs[1]
        uj = UserJob(user_id=user.id, job_id=job.id, subscription_id=sub.id,
                     dm_status=UserJob.DM_SENT, dm_sent_at=now - timedelta(days=1))
        session.add(uj)
        ujs.append(uj)
    await session.flush()
    for uj, minutes in ((ujs[0], 0), (ujs[0], 5), (ujs[1], 0)):
        at = now - timedelta(days=2, minutes=minutes)
        session.add(ApplyClick(user_id=user.id, job_id=uj.job_id, user_job_id=uj.id,
                               subscription_id=uj.subscription_id, clicked_at=at,
                               click_minute=int(at.timestamp() // 60)))
    # Outside the window: not counted.
    old = now - timedelta(days=9)
    session.add(ApplyClick(user_id=user.id, job_id=jobs[3].id, user_job_id=ujs[3].id,
                           subscription_id=subs[1].id, clicked_at=old,
                           click_minute=int(old.timestamp() // 60)))
    await session.commit()
    return subs


@pytest.mark.asyncio
async def test_report_counts_per_search_and_names_no_user(session, patched_db):
    subs = await _seed(session, MONDAY_9)
    text = await clicks.build_weekly_report(MONDAY_9)
    assert "4 job DMs sent, 3 Apply clicks (75% of DMs)" in text
    assert f"#{subs[0].id} python in Cluj: 3 sent, 3 clicks on 2 jobs" in text
    assert f"#{subs[1].id} java in Cluj: 1 sent, 0 clicks on 0 jobs" in text
    assert "private-name" not in text and "4242" not in text


@pytest.mark.asyncio
async def test_empty_week_still_reports(session, patched_db):
    text = await clicks.build_weekly_report(MONDAY_9)
    assert "0 job DMs sent, 0 Apply clicks." in text


@pytest.mark.asyncio
async def test_tick_dms_the_owner_once_per_week(session, patched_db, tmp_path):
    await _seed(session, MONDAY_9)
    bot = FakeBot()
    stamp = tmp_path / "click_report.json"
    assert await clicks.weekly_click_report_tick(bot, stamp, now=MONDAY_9)
    assert not await clicks.weekly_click_report_tick(bot, stamp, now=MONDAY_9 + timedelta(hours=1))
    assert len(bot.dms(OWNER)) == 1 and "weekly Apply clicks" in bot.dms(OWNER)[0]
    assert bot.channel.sent == []  # never a channel
    assert await clicks.weekly_click_report_tick(bot, stamp, now=MONDAY_9 + timedelta(days=7))
    assert len(bot.dms(OWNER)) == 2


@pytest.mark.asyncio
async def test_undelivered_report_is_retried_next_hour(session, patched_db, tmp_path):
    bot = FakeBot(closed_dms=(OWNER,))
    stamp = tmp_path / "click_report.json"
    assert not await clicks.weekly_click_report_tick(bot, stamp, now=MONDAY_9)
    assert not stamp.exists()
    bot.users[OWNER].fail = False
    assert await clicks.weekly_click_report_tick(bot, stamp, now=MONDAY_9 + timedelta(hours=1))


@pytest.mark.asyncio
async def test_alerts_off_sends_nothing(session, patched_db, tmp_path):
    bot = FakeBot(enabled=False)
    assert not await clicks.weekly_click_report_tick(bot, tmp_path / "s.json", now=MONDAY_9)
    assert bot.dms(OWNER) == []


def test_report_flag_defaults_on(monkeypatch):
    from gosha.config import load_settings

    monkeypatch.setenv("DISCORD_TOKEN", "x")
    monkeypatch.delenv("WEEKLY_CLICK_REPORT_ENABLED", raising=False)
    monkeypatch.delenv("EMPTY_SEARCH_NUDGE_ENABLED", raising=False)
    settings = load_settings()
    assert settings.weekly_click_report_enabled is True
    assert settings.empty_search_nudge_enabled is False
    monkeypatch.setenv("WEEKLY_CLICK_REPORT_ENABLED", "false")
    monkeypatch.setenv("EMPTY_SEARCH_NUDGE_ENABLED", "true")
    settings = load_settings()
    assert settings.weekly_click_report_enabled is False
    assert settings.empty_search_nudge_enabled is True
