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
async def test_cycle_posts_health_alert_when_nothing_is_scraped(
    patched_db, monkeypatch,
):
    """The inversion: an empty cycle must reach the alert channel."""
    import gosha.pipeline as pipeline
    from gosha.scrape_health import CycleReport

    sent: list[str] = []

    class FakeChannel:
        async def send(self, content):
            sent.append(content)

    class FakeBot:
        alert_channel_id = 42

        def get_channel(self, _id):
            return FakeChannel()

    async def empty_stage(tunnel_manager, health=None):
        report = CycleReport(
            total=0, per_source={"ejobs": 0}, attempted=["ejobs"], zero_yield=True,
        )
        report.alerts.append("**Scrape produced 0 jobs**")
        if health is not None:
            health.last_report = report
        return []

    monkeypatch.setattr(pipeline, "run_scrape_stage", empty_stage)
    reset_scrape_health()

    total = await pipeline.run_scrape_cycle(FakeBot(), None, alert_channel_id=42)

    assert total == 0
    assert sent, "a zero-yield cycle must alert, not stay silent"
    assert "Scraper health" in sent[0]
    reset_scrape_health()


@pytest.mark.asyncio
async def test_cycle_stays_quiet_when_sources_are_healthy(patched_db, monkeypatch):
    import gosha.pipeline as pipeline
    from gosha.scrape_health import CycleReport

    sent: list[str] = []

    class FakeChannel:
        async def send(self, content):
            sent.append(content)

    class FakeBot:
        alert_channel_id = 42

        def get_channel(self, _id):
            return FakeChannel()

    async def healthy_stage(tunnel_manager, health=None):
        if health is not None:
            health.last_report = CycleReport(
                total=5, per_source={"ejobs": 5}, attempted=["ejobs"],
            )
        return []

    monkeypatch.setattr(pipeline, "run_scrape_stage", healthy_stage)
    reset_scrape_health()

    await pipeline.run_scrape_cycle(FakeBot(), None, alert_channel_id=42)

    assert sent == []
    reset_scrape_health()
