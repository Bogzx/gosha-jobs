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
    assert bot.dms(OWNER) == []  # one empty cycle can be a blip

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


@pytest.mark.asyncio
async def test_a_single_empty_cycle_is_a_blip_not_a_page(patched_db, monkeypatch):
    """Before: one empty cycle paged, and the next one said "recovered"."""
    bot = FakeBot()
    _stage_returning(monkeypatch, ["ejobs"], {"ejobs": 40})
    await _cycle(bot)
    for _ in range(3):
        _stage_returning(monkeypatch, ["ejobs"], {})
        await _cycle(bot)
        _stage_returning(monkeypatch, ["ejobs"], {"ejobs": 40})
        await _cycle(bot)
    assert bot.dms(OWNER) == []


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


def test_c_changed_searches_rebaseline_instead_of_collapsing():
    """A subscription that drove most of a board's volume was deleted."""
    health = ScrapeHealth()
    both = [("python", "cluj"), ("java", "bucuresti")]
    for _ in range(3):
        health.record_cycle(["ejobs"], {"ejobs": 300}, searches=both)
    one = [("python", "cluj")]
    for _ in range(3):
        report = health.record_cycle(["ejobs"], {"ejobs": 30}, searches=one)
        assert not report.conditions
    assert health.baseline("ejobs") == 30


def test_c_frequent_edits_do_not_blind_collapse_detection():
    """Lead review (C): every change used to clear every baseline."""
    health = ScrapeHealth()
    for _ in range(3):
        health.record_cycle(["ejobs"], {"ejobs": 300}, searches=["a", "b"])
    # A user edits a search every few cycles; the yield is unaffected, so
    # the baseline is kept (it used to be thrown away on every edit).
    for i in range(4):
        health.record_cycle(["ejobs"], {"ejobs": 300}, searches=["a", f"edit{i}"])
        for _ in range(2):
            health.record_cycle(["ejobs"], {"ejobs": 300}, searches=["a", f"edit{i}"])
    assert health.baseline("ejobs") == 300
    # The board then really breaks, outside the 2-cycle grace: paged.
    health.record_cycle(["ejobs"], {"ejobs": 3}, searches=["a", "edit3"])
    report = health.record_cycle(["ejobs"], {"ejobs": 3}, searches=["a", "edit3"])
    assert "scrape:collapse:ejobs" in report.conditions


def test_c_added_searches_grant_no_grace():
    health = ScrapeHealth()
    for _ in range(3):
        health.record_cycle(["ejobs"], {"ejobs": 300}, searches=["a"])
    health.record_cycle(["ejobs"], {"ejobs": 3}, searches=["a", "b"])
    report = health.record_cycle(["ejobs"], {"ejobs": 3}, searches=["a", "b"])
    assert "scrape:collapse:ejobs" in report.conditions


def test_c_grace_only_for_the_board_that_lost_a_search():
    health = ScrapeHealth()
    for _ in range(3):
        health.record_cycle(["ejobs", "hipo"], {"ejobs": 300, "hipo": 300},
                            searches={"ejobs": ["a", "b"], "hipo": ["a"]})
    for _ in range(2):
        report = health.record_cycle(["ejobs", "hipo"], {"ejobs": 30, "hipo": 30},
                                     searches={"ejobs": ["a"], "hipo": ["a"]})
    assert "scrape:collapse:hipo" in report.conditions
    assert "scrape:collapse:ejobs" not in report.conditions
    assert health.baseline("ejobs") is None or health.baseline("ejobs") < 100


def test_c_grace_expires():
    health = ScrapeHealth()
    for _ in range(3):
        health.record_cycle(["ejobs"], {"ejobs": 300}, searches=["a", "b"])
    for _ in range(3):  # the removal did not change the yield
        health.record_cycle(["ejobs"], {"ejobs": 300}, searches=["a"])
    health.record_cycle(["ejobs"], {"ejobs": 3}, searches=["a"])
    report = health.record_cycle(["ejobs"], {"ejobs": 3}, searches=["a"])
    assert "scrape:collapse:ejobs" in report.conditions


def test_c_rebaseline_clears_an_active_collapse(tmp_path):
    path = tmp_path / "scrape_health.json"
    health = ScrapeHealth(state_path=path)
    for _ in range(3):
        health.record_cycle(["ejobs"], {"ejobs": 300}, searches=["a", "b"])
    health.record_cycle(["ejobs"], {"ejobs": 3}, searches=["a", "b"])
    assert "scrape:collapse:ejobs" in health.record_cycle(
        ["ejobs"], {"ejobs": 3}, searches=["b", "a"]).conditions  # same set
    restarted = ScrapeHealth(state_path=path)  # signature survives a restart
    report = restarted.record_cycle(["ejobs"], {"ejobs": 3}, searches=["a"])
    assert "scrape:collapse:ejobs" in report.cleared
    assert not report.conditions


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


# Prod, BestJobs: 40 postings first seen 2026-10-16 ~17:00, 36 of them
# empty; the board also listed older postings that kept their text. By
# 12:00 the next day 18 had their text; the 22 empty ones were expired
# postings (bestjobs drops expired pages, so no upsert or refresh ever
# refilled them): 22/40 = 55%, neither failing nor healthy. last_seen_at is when a search last
# KEPT a posting, and most are kept only in their first cycle, so it
# cannot tell which postings a board still lists: judging by it would
# have rated the 54 older ones too (36/94, no alert) or almost none.
INCIDENT = datetime(2026, 10, 16, 17, 41, tzinfo=timezone.utc)


async def _bestjobs_incident(session):
    from gosha.embeddings import current_model
    from gosha.models import Job

    def job(url, first_seen, description):
        return Job(
            url=url, title="Engineer", company="Acme", source="bestjobs",
            description=description, first_seen_at=first_seen, last_seen_at=INCIDENT,
            embedding=b"\x00" * 8, embedding_model=current_model(),
        )

    old = [job(f"https://bestjobs.test/old/{i}", INCIDENT - timedelta(days=3), LONG)
           for i in range(54)]
    new = [job(f"https://bestjobs.test/new/{i}", INCIDENT - timedelta(minutes=40),
               None if i < 36 else LONG) for i in range(40)]
    session.add_all(old + new)
    await session.commit()
    return old, new


@pytest.mark.asyncio
async def test_d_bestjobs_incident_recovers_once_expired_rows_are_deactivated(
    patched_db, session,
):
    from gosha.alerts import Alerter, sink_for_bot

    old, new = await _bestjobs_incident(session)
    now = [INCIDENT.timestamp()]
    alerter = Alerter(clock=lambda: now[0])
    bot = FakeBot()

    async def check(at):
        now[0] = at.timestamp()
        report = await check_data_quality(now=at)
        await alerter.update(sink_for_bot(bot), report.conditions, report.cleared,
                             title="Data quality")
        return report

    # The new postings are judged on their own: the 54 older ones the
    # board still lists, with their text, do not dilute 36/40 into 36/94.
    report = await check(INCIDENT)
    assert "90% of the 40 postings" in report.conditions["data:descriptions:bestjobs"]
    assert len(bot.dms(OWNER)) == 1

    next_noon = INCIDENT + timedelta(hours=18, minutes=19)
    for row in new[:14]:  # their detail pages were read next cycle
        row.description = LONG
    await session.commit()
    report = await check(next_noon)
    assert report.descriptions["bestjobs"][:2] == (40, 22)  # prod at 12:00
    assert "data:descriptions:bestjobs" not in report.conditions
    assert "data:descriptions:bestjobs" not in report.cleared
    assert len(bot.dms(OWNER)) == 1

    # The expired postings are deactivated: they no longer count, the 18
    # live ones all have text, and the owner hears it is healthy.
    for row in new[14:36]:
        row.is_active = False
    await session.commit()
    report = await check(next_noon + timedelta(hours=1))
    assert "data:descriptions:bestjobs" in report.cleared
    assert len(bot.dms(OWNER)) == 2 and "recovered" in bot.dms(OWNER)[1]


@pytest.mark.asyncio
async def test_d_bestjobs_incident_without_a_verdict_closes_aloud(patched_db, session):
    """The 40 postings age out of the window and BestJobs adds too few new
    ones to judge: after 48h without a verdict the owner hears it closed."""
    from gosha.alerts import Alerter, sink_for_bot

    await _bestjobs_incident(session)
    now = [INCIDENT.timestamp()]
    alerter = Alerter(clock=lambda: now[0])
    bot = FakeBot()

    async def check(at):
        now[0] = at.timestamp()
        report = await check_data_quality(now=at)
        await alerter.update(sink_for_bot(bot), report.conditions, report.cleared,
                             title="Data quality")
        return report

    await check(INCIDENT)
    assert len(bot.dms(OWNER)) == 1
    for hours in (25, 47):
        report = await check(INCIDENT + timedelta(hours=hours))
        assert "data:descriptions:bestjobs" not in report.conditions
        assert "data:descriptions:bestjobs" not in report.cleared
    assert len(bot.dms(OWNER)) == 1
    await check(INCIDENT + timedelta(hours=48))
    assert len(bot.dms(OWNER)) == 2
    assert "closed without a verdict" in bot.dms(OWNER)[1]
    assert "data:descriptions:bestjobs" not in alerter.active


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
async def _add_subscription(session):
    from gosha.models import Subscription, User

    user = User(discord_user_id=1)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id, max_age_days=7)
    sub.keywords, sub.locations, sub.boards = ["python"], ["cluj"], ["indeed"]
    session.add(sub)
    await session.commit()


@pytest.mark.asyncio
async def test_no_new_job_for_two_days_alerts_when_searches_exist(patched_db, session):
    await _add_jobs(session, "ejobs", 5, age=timedelta(hours=50), description=LONG)
    report = await check_data_quality()
    assert "data:no_new_jobs" not in report.conditions  # nobody is searching

    await _add_subscription(session)
    report = await check_data_quality()
    assert "data:no_new_jobs" in report.conditions


@pytest.mark.asyncio
async def test_a_quiet_day_is_not_no_new_jobs(patched_db, session):
    """24h was a false positive on narrow searches over a weekend."""
    await _add_jobs(session, "ejobs", 5, age=timedelta(hours=30), description=LONG)
    await _add_subscription(session)
    report = await check_data_quality()
    assert "data:no_new_jobs" in report.cleared


async def _add_reembed_backlog(session, n, tag):
    from gosha.models import Job

    now = datetime.now(timezone.utc)
    for i in range(n):
        session.add(Job(
            url=f"https://ejobs.test/old-model/{tag}/{i}", title="Engineer", company="Acme",
            source="ejobs", description=LONG, first_seen_at=now - timedelta(hours=5),
            last_seen_at=now, embedding=b"\x00" * 8, embedding_model="some-old-model",
        ))
    await session.commit()


async def _reembed(session, n):
    from sqlalchemy import select

    from gosha.embeddings import current_model
    from gosha.models import Job

    rows = (await session.execute(
        select(Job).where(Job.embedding_model == "some-old-model").limit(n)
    )).scalars()
    for job in rows:
        job.embedding_model = current_model()
    await session.commit()


async def _deactivate(session, n):
    from sqlalchemy import select

    from gosha.models import Job

    for job in (await session.execute(select(Job).where(Job.is_active.is_(True)).limit(n))).scalars():
        job.is_active = False
    await session.commit()


@pytest.mark.asyncio
async def test_e_model_change_backfill_is_not_an_embedding_failure(patched_db, session):
    await _add_reembed_backlog(session, 40, "a")
    first = await check_data_quality()  # right after a restart: wait and see
    assert first.embedding_backlog == 40
    assert "data:embeddings" not in first.conditions | first.cleared

    await _reembed(session, 15)  # the backfill re-embedded some
    progressing = await check_data_quality()
    assert progressing.embedding_backlog == 25
    assert "data:embeddings" not in progressing.conditions

    stuck = await check_data_quality()  # no progress for an hour
    assert "data:embeddings" in stuck.conditions


@pytest.mark.asyncio
async def test_e_backlog_shrinking_by_deactivation_is_not_progress(patched_db, session):
    """Lead review (F): jobs going inactive shrank the backlog and looked
    like a working backfill."""
    await _add_reembed_backlog(session, 40, "a")
    await check_data_quality()
    await _deactivate(session, 15)  # dead links pruned; nothing re-embedded
    report = await check_data_quality()
    assert report.embedding_backlog == 25
    assert "data:embeddings" in report.conditions


@pytest.mark.asyncio
async def test_e_unembedded_backlog_alerts_at_once(patched_db, session):
    """Never-embedded jobs are not a model change: no grace hour."""
    await _add_jobs(session, "ejobs", 15, age=timedelta(hours=5), description=LONG,
                    embedded=False)
    assert "data:embeddings" in (await check_data_quality()).conditions


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
