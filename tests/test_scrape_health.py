"""Scraper-health monitoring: alert on silence, stay quiet on success."""

from __future__ import annotations

import pytest

from gosha.scrape_health import ScrapeHealth, get_scrape_health, reset_scrape_health


def test_healthy_cycle_produces_no_alerts():
    health = ScrapeHealth()
    report = health.record_cycle(["ejobs", "hipo"], {"ejobs": 12, "hipo": 4})
    assert report.total == 16
    assert report.should_alert is False
    assert report.zero_yield is False


def test_zero_yield_cycle_alerts_immediately():
    """The failure the old code was blind to: nothing scraped at all."""
    health = ScrapeHealth()
    report = health.record_cycle(["ejobs", "hipo", "indeed"], {})

    assert report.zero_yield is True
    assert report.should_alert is True
    assert "0 jobs" in report.alerts[0]
    assert report.zero_cycle_streak == 1


def test_zero_yield_pages_from_the_second_cycle():
    health = ScrapeHealth()
    first = health.record_cycle(["ejobs"], {})
    assert first.conditions == {}  # logged (alerts), not paged
    assert first.should_alert
    second = health.record_cycle(["ejobs"], {})
    assert "scrape:zero_yield" in second.conditions


def test_zero_yield_streak_counts_up_and_resets():
    health = ScrapeHealth()
    for expected in (1, 2, 3):
        report = health.record_cycle(["ejobs"], {})
        assert report.zero_cycle_streak == expected

    recovered = health.record_cycle(["ejobs"], {"ejobs": 3})
    assert recovered.zero_cycle_streak == 0


def test_source_attempted_but_absent_counts_as_zero():
    """A dead adapter returns no rows, so it has no key in per_source.

    Without the `attempted` list it would silently vanish from the
    accounting instead of alerting — which is the whole bug.
    """
    health = ScrapeHealth()
    report = health.record_cycle(["ejobs", "bestjobs"], {"ejobs": 9})
    assert report.per_source == {"bestjobs": 0, "ejobs": 9}
    assert health.streaks["bestjobs"] == 1


def test_per_source_streak_alerts_at_threshold_only_once():
    health = ScrapeHealth(streak_threshold=3, repeat_every=12)

    for _ in range(2):
        report = health.record_cycle(["ejobs", "bestjobs"], {"ejobs": 5})
        assert report.dead_sources == []

    third = health.record_cycle(["ejobs", "bestjobs"], {"ejobs": 5})
    assert third.dead_sources == ["bestjobs"]
    assert "bestjobs" in third.alerts[0]
    assert third.zero_yield is False  # ejobs is fine; this is source-specific

    fourth = health.record_cycle(["ejobs", "bestjobs"], {"ejobs": 5})
    assert fourth.dead_sources == []  # no repeat spam


def test_dead_source_realerts_on_the_repeat_cadence():
    health = ScrapeHealth(streak_threshold=2, repeat_every=3)
    reports = [health.record_cycle(["hipo"], {}) for _ in range(8)]
    alerting = [i for i, r in enumerate(reports) if "hipo" in " ".join(r.alerts)]
    # streak 2 (index 1) trips it, then every 3 cycles after: 5, 8...
    assert alerting[0] == 1
    assert 4 in alerting


def test_recovery_is_reported():
    health = ScrapeHealth(streak_threshold=2)
    health.record_cycle(["remoteok"], {})
    health.record_cycle(["remoteok"], {})
    report = health.record_cycle(["remoteok"], {"remoteok": 7})

    assert report.recovered_sources == ["remoteok"]
    assert "recovered" in report.summary()


def test_singleton_is_resettable():
    reset_scrape_health()
    first = get_scrape_health()
    assert get_scrape_health() is first
    reset_scrape_health()
    assert get_scrape_health() is not first


@pytest.mark.asyncio
async def test_scrape_stage_records_zero_for_a_dead_adapter(
    patched_db, session, monkeypatch,
):
    """End-to-end: a source that returns nothing is visible in the report."""
    import pandas as pd

    import gosha.pipeline as pipeline
    from gosha.models import Subscription, User

    user = User(discord_user_id=7007)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id, max_age_days=7)
    sub.keywords = ["python developer"]
    sub.locations = ["cluj"]
    sub.boards = ["indeed"]
    session.add(sub)
    await session.commit()

    async def no_jobspy(tunnel_manager, keyword, location, max_age_days, boards=None):
        return pd.DataFrame()

    monkeypatch.setattr("gosha.scraper.scrape_jobs_raw", no_jobspy)

    class SilentScraper:
        name = "bestjobs"

        async def search(self, query):
            return []

    monkeypatch.setattr(
        "gosha.scrapers.registry.get_extra_scrapers", lambda: [SilentScraper()]
    )

    class DummyTunnels:
        def active_proxies(self):
            return []

    health = ScrapeHealth()
    jobs = await pipeline.run_scrape_stage(DummyTunnels(), health)

    assert jobs == []
    report = health.last_report
    assert report is not None
    assert report.zero_yield is True
    assert report.per_source == {"bestjobs": 0, "indeed": 0}
    assert report.should_alert is True


@pytest.mark.asyncio
async def test_cycle_dms_health_alert_when_nothing_is_scraped(
    patched_db, monkeypatch,
):
    """The inversion: an empty cycle must reach the owner (by DM only)."""
    import gosha.pipeline as pipeline
    from gosha.alerts import reset_alerter
    from tests.test_alerts import OWNER, FakeBot

    async def empty_stage(tunnel_manager, health=None):
        health.record_cycle(["ejobs"], {})
        return []

    monkeypatch.setattr(pipeline, "run_scrape_stage", empty_stage)
    reset_scrape_health()
    reset_alerter()
    bot = FakeBot()

    total = await pipeline.run_scrape_cycle(bot, None, alert_channel_id=42)
    assert total == 0
    assert bot.dms(OWNER) == []  # one empty cycle can be a blip
    await pipeline.run_scrape_cycle(bot, None, alert_channel_id=42)

    assert bot.dms(OWNER), "zero-yield cycles must alert, not stay silent"
    assert "Scraper health" in bot.dms(OWNER)[0]
    assert bot.channel.sent == []
    reset_scrape_health()
    reset_alerter()


@pytest.mark.asyncio
async def test_cycle_stays_quiet_when_sources_are_healthy(patched_db, monkeypatch):
    import gosha.pipeline as pipeline
    from gosha.alerts import reset_alerter
    from tests.test_alerts import OWNER, FakeBot

    async def healthy_stage(tunnel_manager, health=None):
        health.record_cycle(["ejobs"], {"ejobs": 5})
        return []

    monkeypatch.setattr(pipeline, "run_scrape_stage", healthy_stage)
    reset_scrape_health()
    reset_alerter()
    bot = FakeBot()

    await pipeline.run_scrape_cycle(bot, None, alert_channel_id=42)

    assert bot.dms(OWNER) == [] and bot.channel.sent == []
    reset_scrape_health()
    reset_alerter()


def test_breakdown_shows_scraped_and_kept():
    health = ScrapeHealth()
    report = health.record_cycle(
        ["ejobs", "indeed"], {"ejobs": 212, "indeed": 30}, {"ejobs": 15},
    )
    assert report.kept == {"ejobs": 15, "indeed": 0}
    assert report.breakdown() == "ejobs=212→15, indeed=30→0"
    # Zero kept is not zero scraped: nothing alerts.
    assert not report.should_alert


def test_sources_no_longer_attempted_are_forgotten(tmp_path):
    """Nobody searches remote any more: remoteok's state must not linger."""
    path = tmp_path / "scrape_health.json"
    health = ScrapeHealth(state_path=path)
    for _ in range(3):
        report = health.record_cycle(["ejobs", "remoteok"], {"ejobs": 30})
    assert "scrape:dead:remoteok" in report.conditions

    from gosha.scrape_health import RETIRE_AFTER_CYCLES

    for _ in range(RETIRE_AFTER_CYCLES - 1):
        report = health.record_cycle(["ejobs"], {"ejobs": 30})
        assert report.retired == []
    assert "remoteok" in ScrapeHealth(state_path=path).streaks  # survives a restart
    report = health.record_cycle(["ejobs"], {"ejobs": 30})
    assert set(report.retired) == {"scrape:dead:remoteok", "scrape:collapse:remoteok"}
    assert "remoteok" not in ScrapeHealth(state_path=path).streaks


def test_one_cycle_without_a_source_does_not_forget_it():
    """Lead review (E): get_extra_scrapers() failing once used to wipe every
    extra source's baseline and drop its alerts."""
    health = ScrapeHealth()
    for _ in range(3):
        health.record_cycle(["ejobs", "hipo"], {"ejobs": 300, "hipo": 300})
    report = health.record_cycle(["indeed"], {"indeed": 50})  # extras failed to load
    assert report.retired == []
    assert health.baseline("ejobs") == 300
    health.record_cycle(["ejobs", "hipo"], {"ejobs": 3, "hipo": 300})
    assert "scrape:collapse:ejobs" in health.record_cycle(
        ["ejobs", "hipo"], {"ejobs": 3, "hipo": 300}).conditions


@pytest.mark.asyncio
async def test_retired_source_alert_is_dropped_without_a_message(monkeypatch):
    from gosha import alerts
    from gosha.alerts import get_alerter, reset_alerter
    from tests.test_alerts import OWNER, FakeBot

    class Store:
        async def emit(self, *a, **kw):
            pass

    monkeypatch.setattr("gosha.events.get_event_store", lambda: Store())
    reset_alerter()
    bot = FakeBot()
    health = ScrapeHealth()
    for _ in range(3):
        report = health.record_cycle(["ejobs", "remoteok"], {"ejobs": 30})
        await alerts.publish_scrape_report(bot, 0, report)
    assert len(bot.dms(OWNER)) == 1
    assert "scrape:dead:remoteok" in get_alerter().active

    from gosha.scrape_health import RETIRE_AFTER_CYCLES

    for _ in range(RETIRE_AFTER_CYCLES):
        await alerts.publish_scrape_report(
            bot, 0, health.record_cycle(["ejobs"], {"ejobs": 30}))
    assert "scrape:dead:remoteok" not in get_alerter().active
    assert len(bot.dms(OWNER)) == 1  # no misleading "recovered"
    reset_alerter()
