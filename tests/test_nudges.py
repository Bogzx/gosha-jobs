"""Monthly empty-search nudge: right searches, right time, once per window.

Every DM here goes to a fake bot; nothing reaches Discord.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from gosha import nudges
from gosha.models import Job, SearchNudge, Subscription, User, UserJob

# 10:00 in Bucharest (UTC+3 in October): inside the sending hours.
DEPLOY = datetime(2026, 10, 16, 7, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


class FakeUser:
    def __init__(self, fail=None):
        self.sent: list[str] = []
        self.fail = fail

    async def send(self, text):
        if self.fail is not None:
            raise self.fail
        self.sent.append(text)


class FakeBot:
    def __init__(self):
        self.users: dict[int, FakeUser] = {}

    def get_user(self, uid):
        return self.users.setdefault(uid, FakeUser())

    def dms(self, uid) -> list[str]:
        return self.users.get(uid, FakeUser()).sent


async def _search(session, *, discord_id=5001, created=DEPLOY - 90 * DAY, **kw):
    user = User(discord_user_id=discord_id)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id, max_age_days=kw.pop("max_age_days", 7),
                       created_at=created)
    sub.keywords = kw.pop("keywords", ["python developer"])
    sub.locations = kw.pop("locations", ["Cluj"])
    sub.experience_levels = kw.pop("levels", ["senior"])
    sub.excluded_keywords = kw.pop("excluded", [])
    for key, value in kw.items():
        setattr(sub, key, value)
    session.add(sub)
    await session.commit()
    return sub


async def _run(bot, now, enabled=True):
    return await nudges.run_empty_search_nudges(bot, enabled=enabled, now=now)


@pytest.mark.asyncio
async def test_first_run_after_deploy_starts_the_clock_and_sends_nothing(session, patched_db):
    """Old searches starved by bugs this deploy fixes get 30 days first."""
    sub = await _search(session)
    bot = FakeBot()
    run = await _run(bot, DEPLOY)
    assert run.first_seen == 1 and run.due == []
    for days in (1, 10, 29):
        assert (await _run(bot, DEPLOY + days * DAY)).due == []
    assert bot.dms(5001) == []

    run = await _run(bot, DEPLOY + 30 * DAY)
    assert run.sent == [sub.id]
    assert len(bot.dms(5001)) == 1


@pytest.mark.asyncio
async def test_at_most_once_per_30_days(session, patched_db):
    await _search(session)
    bot = FakeBot()
    await _run(bot, DEPLOY)
    await _run(bot, DEPLOY + 30 * DAY)
    for hours in range(1, 24 * 29, 7):
        await _run(bot, DEPLOY + 30 * DAY + timedelta(hours=hours))
    assert len(bot.dms(5001)) == 1
    await _run(bot, DEPLOY + 60 * DAY)
    assert len(bot.dms(5001)) == 2


@pytest.mark.asyncio
async def test_a_delivered_job_restarts_the_clock(session, patched_db):
    sub = await _search(session)
    bot = FakeBot()
    await _run(bot, DEPLOY)
    job = Job(url="https://example.com/1", title="Dev", company="A", source="ejobs")
    session.add(job)
    await session.flush()
    session.add(UserJob(user_id=sub.user_id, job_id=job.id, subscription_id=sub.id,
                        dm_status=UserJob.DM_SENT, dm_sent_at=DEPLOY + 20 * DAY))
    await session.commit()
    assert (await _run(bot, DEPLOY + 30 * DAY)).due == []
    assert (await _run(bot, DEPLOY + 50 * DAY)).sent == [sub.id]


@pytest.mark.asyncio
async def test_a_new_search_waits_30_days_from_creation(session, patched_db):
    bot = FakeBot()
    await _run(bot, DEPLOY)
    sub = await _search(session, created=DEPLOY + 5 * DAY)
    await _run(bot, DEPLOY + 5 * DAY)  # first sighting
    assert (await _run(bot, DEPLOY + 34 * DAY)).due == []
    assert (await _run(bot, DEPLOY + 35 * DAY)).sent == [sub.id]


@pytest.mark.asyncio
async def test_an_edit_restarts_the_clock(session, patched_db):
    sub = await _search(session)
    bot = FakeBot()
    await _run(bot, DEPLOY)
    sub.experience_levels = ["junior"]
    await session.commit()
    run = await _run(bot, DEPLOY + 25 * DAY)
    assert run.edited == 1
    assert (await _run(bot, DEPLOY + 54 * DAY)).due == []
    assert (await _run(bot, DEPLOY + 55 * DAY)).sent == [sub.id]


@pytest.mark.asyncio
async def test_pause_and_resume_restart_the_clock(session, patched_db):
    sub = await _search(session)
    bot = FakeBot()
    await _run(bot, DEPLOY)
    sub.is_active = False
    await session.commit()
    assert (await _run(bot, DEPLOY + 31 * DAY)).due == []  # paused: never nudged
    sub.is_active = True
    await session.commit()
    await _run(bot, DEPLOY + 40 * DAY)  # resumed: the clock starts again
    assert (await _run(bot, DEPLOY + 60 * DAY)).due == []
    assert (await _run(bot, DEPLOY + 70 * DAY)).sent == [sub.id]


@pytest.mark.asyncio
async def test_website_only_searches_are_not_nudged(session, patched_db):
    await _search(session, notify_discord=False)
    bot = FakeBot()
    await _run(bot, DEPLOY)
    assert (await _run(bot, DEPLOY + 31 * DAY)).due == []


@pytest.mark.asyncio
async def test_off_by_default_but_the_clock_still_runs(session, patched_db):
    """Turned on after 40 days: due at once, not 30 days after switching on."""
    sub = await _search(session)
    bot = FakeBot()
    await _run(bot, DEPLOY, enabled=False)
    run = await _run(bot, DEPLOY + 40 * DAY, enabled=False)
    assert run.due == [sub.id] and run.sent == []
    assert bot.dms(5001) == []
    assert (await _run(bot, DEPLOY + 40 * DAY + timedelta(hours=1))).sent == [sub.id]


@pytest.mark.asyncio
async def test_no_dms_at_night(session, patched_db):
    await _search(session)
    bot = FakeBot()
    await _run(bot, DEPLOY)
    night = DEPLOY + 30 * DAY + timedelta(hours=16)  # 02:00 in Bucharest
    run = await _run(bot, night)
    assert run.due and run.sent == []
    assert (await _run(bot, night + timedelta(hours=8))).sent  # 10:00


@pytest.mark.asyncio
async def test_closed_dms_are_recorded_not_retried_hourly(session, patched_db):
    import discord

    sub = await _search(session)
    bot = FakeBot()
    response = type("R", (), {"status": 403, "reason": "Forbidden"})()
    bot.users[5001] = FakeUser(fail=discord.Forbidden(response, "Cannot send messages"))
    await _run(bot, DEPLOY)
    assert (await _run(bot, DEPLOY + 30 * DAY)).closed == [sub.id]
    assert (await _run(bot, DEPLOY + 30 * DAY + timedelta(hours=1))).due == []
    state = (await session.execute(
        select(SearchNudge).execution_options(populate_existing=True))).scalar_one()
    assert state.outcome == "closed"


@pytest.mark.asyncio
async def test_a_discord_hiccup_is_retried_next_hour(session, patched_db):
    sub = await _search(session)
    bot = FakeBot()
    bot.users[5001] = FakeUser(fail=RuntimeError("503"))
    await _run(bot, DEPLOY)
    assert (await _run(bot, DEPLOY + 30 * DAY)).failed == [sub.id]
    bot.users[5001].fail = None
    assert (await _run(bot, DEPLOY + 30 * DAY + timedelta(hours=1))).sent == [sub.id]


@pytest.mark.asyncio
async def test_per_run_cap(session, patched_db):
    for i in range(5):
        await _search(session, discord_id=6000 + i)
    bot = FakeBot()
    await _run(bot, DEPLOY)
    run = await nudges.run_empty_search_nudges(bot, enabled=True, now=DEPLOY + 30 * DAY,
                                               max_per_run=2)
    assert len(run.sent) == 2 and len(run.due) == 5


@pytest.mark.asyncio
async def test_deleting_a_search_drops_its_nudge_state(session, patched_db):
    sub = await _search(session)
    await _run(FakeBot(), DEPLOY)
    await session.delete(sub)
    await session.commit()
    assert (await session.execute(select(SearchNudge))).first() is None


# ── The message ───────────────────────────────────────────────────────


def _sub(**kw):
    sub = Subscription(user_id=1, max_age_days=kw.pop("max_age_days", 7))
    sub.id = 12
    sub.keywords = kw.pop("keywords", ["react native developer"])
    sub.locations = kw.pop("locations", ["Oradea"])
    sub.experience_levels = kw.pop("levels", ["senior"])
    sub.excluded_keywords = kw.pop("excluded", [])
    sub.salary_min = kw.pop("salary_min", None)
    return sub


def test_message_says_what_why_two_fixes_and_how_to_edit():
    text = nudges.nudge_text(_sub(), site="https://gosha.test")
    assert "#12" in text and "react native developer" in text and "Oradea" in text
    assert "30 days" in text
    assert "only accepts the senior level" in text
    assert text.count("\n• ") == 2
    assert "**junior**" in text and "**Romania** or **remote**" in text
    assert "/edit id:12" in text and "https://gosha.test/searches" in text
    assert "/pause id:12" in text
    assert len(text) < 900  # short


def test_suggestions_follow_the_search():
    broad = _sub(levels=["any"], locations=["Romania"], excluded=["php", "sales"])
    text = nudges.nudge_text(broad)
    assert "junior" not in text and "remote" not in text
    assert "excluded word" in text
    assert "/searches" not in text  # no site configured: Discord only
    fallback = nudges.nudge_text(_sub(levels=["junior"], locations=["remote"], max_age_days=30))
    assert "broader or more common job title" in fallback


def test_fingerprint_changes_on_criteria_and_pause_only():
    sub = _sub()
    sub.is_active, sub.notify_discord, sub.remote_ok = True, True, False
    sub.company_blacklist, sub.boards = [], ["indeed"]
    base = nudges.fingerprint(sub)
    sub.name = "renamed"
    assert nudges.fingerprint(sub) == base
    sub.locations = ["Oradea", "Cluj"]
    assert nudges.fingerprint(sub) != base
    sub.locations = ["Oradea"]
    sub.is_active = False
    assert nudges.fingerprint(sub) != base
