"""Does the owner hear about it? One test per way scraping can fail.

Each scenario drives the real code path (scrape cycle → ScrapeHealth →
alerts → Discord fakes, or the database checks) and asserts on what lands
in the owner's DMs and the alert channel, not on internal flags.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gosha.alerts import reset_alerter
from gosha.scrape_health import (
    ScrapeHealth,
    check_data_quality,
    get_scrape_health,
    reset_scrape_health,
)
from tests.test_alerts import OWNER, FakeBot


@pytest.fixture(autouse=True)
def _fresh_state():
    reset_scrape_health()
    reset_alerter()
    yield
    reset_scrape_health()
    reset_alerter()


def _stage_returning(monkeypatch, attempted, per_source):
    """Replace the scrape stage with one whose boards return `per_source`."""
    import gosha.pipeline as pipeline

    async def stage(tunnel_manager, health=None):
        health = health or get_scrape_health()
        health.record_cycle(attempted, per_source)
        return []

    monkeypatch.setattr(pipeline, "run_scrape_stage", stage)


async def _cycle(bot):
    import gosha.pipeline as pipeline

    return await pipeline.run_scrape_cycle(bot, None, alert_channel_id=bot.alert_channel_id)


# (a) every source returns 0 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_zero_yield_dms_owner_once_then_reports_recovery(patched_db, monkeypatch):
    bot = FakeBot()
    _stage_returning(monkeypatch, ["ejobs", "hipo"], {})
    await _cycle(bot)

    assert len(bot.dms(OWNER)) == 1
    assert "0 jobs" in bot.dms(OWNER)[0]
    assert bot.channel.sent == []  # DM only, even with a channel configured

    # Still broken next hour: no second page inside the cooldown (before,
    # every empty cycle posted again).
    await _cycle(bot)
    await _cycle(bot)
    assert len(bot.dms(OWNER)) == 1

    _stage_returning(monkeypatch, ["ejobs", "hipo"], {"ejobs": 40, "hipo": 12})
    await _cycle(bot)
    assert len(bot.dms(OWNER)) == 2
    assert "recovered" in bot.dms(OWNER)[1]


@pytest.mark.asyncio
async def test_a_zero_yield_is_one_alert_not_one_per_source(patched_db, monkeypatch):
    bot = FakeBot()
    _stage_returning(monkeypatch, ["bestjobs", "ejobs", "hipo"], {})
    for _ in range(3):  # long enough for every per-source streak to trip
        await _cycle(bot)
    assert len(bot.dms(OWNER)) == 1
    assert "adapter is probably broken" not in bot.dms(OWNER)[0]


@pytest.mark.asyncio
async def test_a_each_cycle_leaves_a_scrape_cycle_event(patched_db, session, monkeypatch):
    from sqlalchemy import select

    from gosha.events import Event

    _stage_returning(monkeypatch, ["ejobs"], {"ejobs": 9})
    await _cycle(FakeBot())
    events = (await session.execute(
        select(Event).where(Event.event_type == "scrape.cycle")
    )).scalars().all()
    assert len(events) == 1 and events[0].payload["total"] == 9


@pytest.mark.asyncio
async def test_a_disabled_alerts_stay_silent(patched_db, monkeypatch):
    bot = FakeBot(enabled=False)
    _stage_returning(monkeypatch, ["ejobs"], {})
    await _cycle(bot)
    assert bot.dms(OWNER) == [] and bot.channel.sent == []


# (b) one board breaks ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_b_one_dead_board_pages_after_three_cycles_and_recovers(patched_db, monkeypatch):
    bot = FakeBot()
    _stage_returning(monkeypatch, ["bestjobs", "ejobs"], {"ejobs": 50})
    for _ in range(2):
        await _cycle(bot)
    assert bot.dms(OWNER) == []  # one or two empty cycles can be a narrow keyword

    await _cycle(bot)
    assert len(bot.dms(OWNER)) == 1
    assert "bestjobs" in bot.dms(OWNER)[0]

    for _ in range(5):
        await _cycle(bot)
    assert len(bot.dms(OWNER)) == 1  # cooldown, not hourly spam

    _stage_returning(monkeypatch, ["bestjobs", "ejobs"], {"ejobs": 50, "bestjobs": 30})
    await _cycle(bot)
    assert len(bot.dms(OWNER)) == 2
    assert "bestjobs" in bot.dms(OWNER)[1] and "recovered" in bot.dms(OWNER)[1]


# (c) yield collapses but is not zero ─────────────────────────────────


def test_c_collapse_against_rolling_baseline():
    health = ScrapeHealth()
    for count in (300, 280, 310, 295):
        assert not health.record_cycle(["ejobs"], {"ejobs": count}).conditions

    first = health.record_cycle(["ejobs"], {"ejobs": 3})
    assert first.conditions == {}  # one bad cycle can be a blip
    second = health.record_cycle(["ejobs"], {"ejobs": 3})
    assert "scrape:collapse:ejobs" in second.conditions
    assert "~298" in second.conditions["scrape:collapse:ejobs"]
    assert second.collapsed_sources == ["ejobs"]

    back = health.record_cycle(["ejobs"], {"ejobs": 290})
    assert "scrape:collapse:ejobs" in back.cleared
    assert not back.conditions


def test_c_collapse_ignores_small_sources_and_normal_variation():
    health = ScrapeHealth()
    for count in (12, 10, 11):
        health.record_cycle(["jobicy"], {"jobicy": count})
    for _ in range(3):  # 12 → 1 is noise at this size
        assert not health.record_cycle(["jobicy"], {"jobicy": 1}).conditions

    big = ScrapeHealth()
    for count in (300, 280, 310):
        big.record_cycle(["ejobs"], {"ejobs": count})
    for _ in range(3):  # half is a quiet day, not a broken paginator
        assert not big.record_cycle(["ejobs"], {"ejobs": 150}).conditions


def test_c_collapse_rebaselines_after_a_long_plateau():
    health = ScrapeHealth(rebaseline_after=5)
    for count in (300, 300, 300):
        health.record_cycle(["ejobs"], {"ejobs": count})
    reports = [health.record_cycle(["ejobs"], {"ejobs": 40}) for _ in range(5)]
    assert "scrape:collapse:ejobs" in reports[3].conditions
    assert "new normal" in reports[4].cleared["scrape:collapse:ejobs"]
    assert not health.record_cycle(["ejobs"], {"ejobs": 40}).conditions


def test_c_baseline_survives_restart(tmp_path):
    path = tmp_path / "scrape_health.json"
    before = ScrapeHealth(state_path=path)
    for count in (300, 300, 300):
        before.record_cycle(["ejobs"], {"ejobs": count})
    after = ScrapeHealth(state_path=path)
    assert after.baseline("ejobs") == 300
    after.record_cycle(["ejobs"], {"ejobs": 3})
    assert "scrape:collapse:ejobs" in after.record_cycle(["ejobs"], {"ejobs": 3}).conditions


@pytest.mark.asyncio
async def test_c_collapse_reaches_the_owner(patched_db, monkeypatch):
    bot = FakeBot()
    for count in (300, 300, 300):
        _stage_returning(monkeypatch, ["ejobs"], {"ejobs": count})
        await _cycle(bot)
    _stage_returning(monkeypatch, ["ejobs"], {"ejobs": 3})
    await _cycle(bot)
    await _cycle(bot)
    assert len(bot.dms(OWNER)) == 1
    assert "collapsed" in bot.dms(OWNER)[0]


# (d) descriptions stop arriving / (e) embeddings stop / no new jobs ──


async def _add_jobs(session, source, n, *, age, description, embedded=True):
    from gosha.embeddings import current_model
    from gosha.models import Job

    now = datetime.now(timezone.utc)
    for i in range(n):
        session.add(Job(
            url=f"https://{source}.test/{age.total_seconds()}/{i}/{description is None}",
            title=f"Engineer {i}", company="Acme", source=source,
            description=description,
            first_seen_at=now - age, last_seen_at=now - age,
            embedding=b"\x00" * 8 if embedded else None,
            embedding_model=current_model() if embedded else None,
        ))
    await session.commit()


LONG = "A real description of the role, its stack and the team. " * 3


@pytest.mark.asyncio
async def test_d_empty_descriptions_alert_against_baseline(patched_db, session):
    await _add_jobs(session, "ejobs", 30, age=timedelta(days=3), description=LONG)
    await _add_jobs(session, "ejobs", 20, age=timedelta(hours=2), description=None)
    # A source that never had descriptions is not "broken".
    await _add_jobs(session, "linkedin", 30, age=timedelta(days=3), description=None)
    await _add_jobs(session, "linkedin", 20, age=timedelta(hours=2), description="")

    report = await check_data_quality()
    assert "data:descriptions:ejobs" in report.conditions
    assert "100%" in report.conditions["data:descriptions:ejobs"]
    assert "data:descriptions:linkedin" not in report.conditions


@pytest.mark.asyncio
async def test_d_descriptions_present_is_healthy(patched_db, session):
    await _add_jobs(session, "ejobs", 30, age=timedelta(days=3), description=LONG)
    await _add_jobs(session, "ejobs", 20, age=timedelta(hours=2), description=LONG)
    report = await check_data_quality()
    assert "data:descriptions:ejobs" in report.cleared
    assert not report.conditions


@pytest.mark.asyncio
async def test_e_embedding_backlog_alerts(patched_db, session):
    await _add_jobs(session, "ejobs", 15, age=timedelta(hours=5), description=LONG,
                    embedded=False)
    # Fresh jobs are still in their grace period.
    await _add_jobs(session, "hipo", 15, age=timedelta(minutes=20), description=LONG,
                    embedded=False)
    report = await check_data_quality()
    assert report.embedding_backlog == 15
    assert "data:embeddings" in report.conditions


@pytest.mark.asyncio
async def test_e_embedding_backlog_clears(patched_db, session):
    await _add_jobs(session, "ejobs", 15, age=timedelta(hours=5), description=LONG)
    report = await check_data_quality()
    assert "data:embeddings" in report.cleared


@pytest.mark.asyncio
async def test_no_new_job_for_a_day_alerts_when_searches_exist(patched_db, session):
    from gosha.models import Subscription, User

    await _add_jobs(session, "ejobs", 5, age=timedelta(hours=30), description=LONG)
    report = await check_data_quality()
    assert "data:no_new_jobs" not in report.conditions  # nobody is searching

    user = User(discord_user_id=1)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id, max_age_days=7)
    sub.keywords, sub.locations, sub.boards = ["python"], ["cluj"], ["indeed"]
    session.add(sub)
    await session.commit()
    report = await check_data_quality()
    assert "data:no_new_jobs" in report.conditions


@pytest.mark.asyncio
async def test_data_quality_alert_reaches_the_owner(patched_db, session):
    from gosha.alerts import get_alerter, sink_for_bot

    await _add_jobs(session, "ejobs", 15, age=timedelta(hours=5), description=LONG,
                    embedded=False)
    bot = FakeBot()
    report = await check_data_quality()
    await get_alerter().update(sink_for_bot(bot), report.conditions, report.cleared,
                               title="Data quality")
    assert len(bot.dms(OWNER)) == 1
    assert "embedding" in bot.dms(OWNER)[0]
