"""Ops alerts reach the owner by DM only, deduplicated, with recovery."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gosha.alerts import (
    Alerter,
    Dispatch,
    DiscordAlertSink,
    alert_recipients,
    format_dispatch,
    post_dm_sync,
    reset_alerter,
    scheduler_event_conditions,
    sink_for_bot,
)

OWNER = 424242424242424242
SECOND = 515151515151515151


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeChannel:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def send(self, content):
        if self.fail:
            raise RuntimeError("Missing Access")
        self.sent.append(content)


class FakeUser:
    def __init__(self, uid: int, fail: bool = False) -> None:
        self.id = uid
        self.sent: list[str] = []
        self.fail = fail

    async def send(self, content):
        if self.fail:
            raise RuntimeError("50007: Cannot send messages to this user")
        self.sent.append(content)


class FakeBot:
    """A bot with an alert channel configured — which must stay unused."""

    def __init__(self, owners=(OWNER,), admins=(999,), channel_id=42, cached_users=True,
                 closed_dms=(), enabled=True):
        self.alert_channel_id = channel_id
        self.channel = FakeChannel()
        self.users = {
            uid: FakeUser(uid, fail=uid in closed_dms) for uid in (*owners, *admins)
        }
        self.cached_users = cached_users
        self.fetched: list[int] = []
        self.settings = SimpleNamespace(
            alert_channel_id=channel_id, admin_user_ids=set(admins),
            alert_dm_user_ids=set(owners), alerts_enabled=enabled,
        )

    def get_channel(self, _id):
        return self.channel

    def get_user(self, uid):
        return self.users.get(uid) if self.cached_users else None

    async def fetch_user(self, uid):
        self.fetched.append(uid)
        return self.users[uid]

    def dms(self, uid) -> list[str]:
        return self.users[uid].sent


@pytest.fixture(autouse=True)
def _fresh_alerter():
    reset_alerter()
    yield
    reset_alerter()


# ── Alerter: dedup, cooldown, recovery ───────────────────────────────


def test_condition_is_raised_once_then_suppressed_within_cooldown():
    clock = Clock()
    alerter = Alerter(cooldown_seconds=3600, clock=clock)
    first = alerter.decide({"scrape:dead:hipo": "hipo dead"})
    assert first.raised == ["hipo dead"]

    clock.t += 600
    assert not alerter.decide({"scrape:dead:hipo": "hipo dead"})


def test_still_failing_condition_is_repeated_after_cooldown():
    clock = Clock()
    alerter = Alerter(cooldown_seconds=3600, clock=clock)
    alerter.decide({"k": "broken"})
    clock.t += 3601
    reminder = alerter.decide({"k": "broken"})
    assert reminder.raised == []
    assert len(reminder.reminded) == 1
    assert "still failing" in reminder.reminded[0]
    clock.t += 60
    assert not alerter.decide({"k": "broken"})


def test_recovery_is_reported_once_and_only_for_active_conditions():
    clock = Clock()
    alerter = Alerter(cooldown_seconds=3600, clock=clock)
    alerter.decide({"k": "broken"})
    clock.t += 7200
    healed = alerter.decide({}, {"k": "k is back"})
    assert healed.recovered and healed.recovered[0].startswith("k is back")
    assert "2h" in healed.recovered[0]
    # Already resolved, and never-raised keys stay silent.
    assert not alerter.decide({}, {"k": "k is back", "other": "fine"})


def test_unknown_state_neither_raises_nor_resolves():
    """A key in neither map (no baseline after a restart) keeps its state."""
    alerter = Alerter(clock=Clock())
    alerter.decide({"k": "broken"})
    assert not alerter.decide({}, {})
    assert "k" in alerter.active


def test_state_survives_a_restart(tmp_path):
    """A deploy during an outage must not re-page the owner."""
    path = tmp_path / "alerts.json"
    clock = Clock()
    Alerter(cooldown_seconds=3600, state_path=path, clock=clock).decide({"k": "broken"})

    restarted = Alerter(cooldown_seconds=3600, state_path=path, clock=clock)
    assert not restarted.decide({"k": "broken"})
    assert restarted.decide({}, {"k": "ok"}).recovered


def test_corrupt_state_file_is_ignored(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text("{not json")
    alerter = Alerter(state_path=path, clock=Clock())
    assert alerter.decide({"k": "broken"}).raised == ["broken"]


def test_format_dispatch_groups_problems_and_recoveries():
    alerter = Alerter(clock=Clock())
    alerter.decide({"a": "A broke"})
    dispatch = alerter.decide({"b": "B broke"}, {"a": "A ok"})
    text = format_dispatch("Scraper health", dispatch)
    assert "🚨" in text and "B broke" in text
    assert "✅" in text and "A ok" in text


# ── Recipients and transport ─────────────────────────────────────────


def test_recipients_are_the_alert_dm_ids_only():
    settings = SimpleNamespace(admin_user_ids={1, 2}, alert_dm_user_ids={3}, alerts_enabled=True)
    assert alert_recipients(settings) == [3]  # admins are not added
    settings.alerts_enabled = False
    assert alert_recipients(settings) == []
    assert alert_recipients(None) == []


@pytest.mark.asyncio
async def test_sink_dms_every_owner_and_never_posts_to_a_channel():
    bot = FakeBot(owners=(OWNER, SECOND))
    await sink_for_bot(bot).send("**bestjobs** is dead")

    assert bot.channel.sent == []
    assert bot.dms(OWNER) == ["**bestjobs** is dead"]
    assert bot.dms(SECOND) == ["**bestjobs** is dead"]
    assert bot.dms(999) == []  # an admin who is not an alert recipient


@pytest.mark.asyncio
async def test_sink_fetches_uncached_users():
    bot = FakeBot(cached_users=False)
    await sink_for_bot(bot).send("x")
    assert bot.fetched == [OWNER]
    assert bot.dms(OWNER) == ["x"]


@pytest.mark.asyncio
async def test_one_closed_dm_does_not_stop_the_others():
    bot = FakeBot(owners=(OWNER, SECOND), closed_dms=(OWNER,))
    await DiscordAlertSink(bot, [OWNER, SECOND]).send("x")
    assert bot.dms(SECOND) == ["x"]


def test_no_sink_when_disabled_or_nobody_configured():
    assert sink_for_bot(FakeBot(enabled=False)) is None
    assert sink_for_bot(FakeBot(owners=())) is None
    assert sink_for_bot(None) is None


@pytest.mark.asyncio
async def test_long_messages_are_clipped_under_discords_limit():
    bot = FakeBot()
    await sink_for_bot(bot).send("x" * 5000)
    assert len(bot.dms(OWNER)[0]) <= 2000


# ── Config ───────────────────────────────────────────────────────────


def test_alert_settings_from_env(monkeypatch, caplog):
    from gosha.config import load_settings

    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("ADMIN_DISCORD_IDS", "111")
    # A username is the classic mistake: it is skipped with a warning,
    # the <@id> form copied from a mention is accepted.
    monkeypatch.setenv("ALERT_DM_USER_IDS", f"Bogdanila, <@{OWNER}>")
    monkeypatch.setenv("ALERT_COOLDOWN_MINUTES", "30")
    monkeypatch.delenv("ALERTS_ENABLED", raising=False)
    settings = load_settings()
    assert settings.alert_dm_user_ids == {OWNER}
    assert settings.alerts_enabled is True
    assert settings.alert_cooldown_minutes == 30
    assert "Bogdanila" in caplog.text
    assert alert_recipients(settings) == [OWNER]

    monkeypatch.setenv("ALERTS_ENABLED", "false")
    assert alert_recipients(load_settings()) == []


def test_alert_settings_defaults_are_off_without_dm_ids(monkeypatch):
    from gosha.config import load_settings

    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("ADMIN_DISCORD_IDS", "111")
    for name in ("ALERT_DM_USER_IDS", "ALERTS_ENABLED", "ALERT_COOLDOWN_MINUTES"):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings()
    assert settings.alert_cooldown_minutes == 360
    assert alert_recipients(settings) == []  # admins are never paged implicitly


# ── Scheduler events ─────────────────────────────────────────────────


def test_scheduler_error_then_success_is_alert_then_recovery():
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent

    jobs = {"scrape_cycle"}
    err = JobExecutionEvent(EVENT_JOB_ERROR, "scrape_cycle", "default", None,
                            exception=RuntimeError("db down"))
    failing, cleared = scheduler_event_conditions(err, jobs)
    assert "db down" in failing["scheduler:scrape_cycle"] and not cleared

    ok = JobExecutionEvent(EVENT_JOB_EXECUTED, "scrape_cycle", "default", None)
    failing, cleared = scheduler_event_conditions(ok, jobs)
    assert not failing and "scheduler:scrape_cycle" in cleared

    other = JobExecutionEvent(EVENT_JOB_ERROR, "outbox_poller", "default", None)
    assert scheduler_event_conditions(other, jobs) == ({}, {})


def test_repeated_scheduler_errors_page_once():
    from apscheduler.events import EVENT_JOB_ERROR, JobExecutionEvent

    alerter = Alerter(cooldown_seconds=3600, clock=Clock())
    event = JobExecutionEvent(EVENT_JOB_ERROR, "scrape_cycle", "default", None,
                              exception=RuntimeError("boom"))
    assert alerter.decide(*scheduler_event_conditions(event, {"scrape_cycle"}))
    assert not alerter.decide(*scheduler_event_conditions(event, {"scrape_cycle"}))


def test_flapping_scheduler_job_is_not_two_dms_per_run():
    """MAX_INSTANCES → ran → MAX_INSTANCES … used to page on every flip."""
    from apscheduler.events import (
        EVENT_JOB_EXECUTED,
        EVENT_JOB_MAX_INSTANCES,
        JobExecutionEvent,
        JobSubmissionEvent,
    )

    clock = Clock()
    alerter = Alerter(cooldown_seconds=6 * 3600, clock=clock)  # 2h flap window
    jobs = {"scrape_cycle"}
    skipped = JobSubmissionEvent(EVENT_JOB_MAX_INSTANCES, "scrape_cycle", "default", [])
    ran = JobExecutionEvent(EVENT_JOB_EXECUTED, "scrape_cycle", "default", None)
    said: list[Dispatch] = []
    for _ in range(12):  # a day of hourly flips
        for event in (skipped, ran):
            dispatch = alerter.decide(*scheduler_event_conditions(event, jobs))
            if dispatch:
                said.append(dispatch)
            clock.t += 3600
    assert said[0].raised and said[1].recovered
    assert len(said) <= 12  # was 24 (one per event)


def test_failure_back_soon_after_recovery_is_paged_once_it_outlasts_the_window():
    """Lead review (A): raise, recovered at 30m, failing again at 31m must
    not stay silent until ~6h after the recovery."""
    clock = Clock()
    alerter = Alerter(cooldown_seconds=6 * 3600, flap_window_seconds=3600, clock=clock)
    assert alerter.decide({"k": "down"}).raised
    clock.t += 30 * 60
    assert alerter.decide({}, {"k": "up"}).recovered
    clock.t += 60
    assert not alerter.decide({"k": "down"})  # inside the window: maybe a flap
    assert not alerter.is_told("k")
    clock.t += 30 * 60
    assert not alerter.decide({"k": "down"})  # 31m after the recovery
    clock.t += 30 * 60
    paged = alerter.decide({"k": "down"})  # 61m: outlasted the window
    assert len(paged.raised) == 1 and "failing again" in paged.raised[0]
    assert alerter.is_told("k")
    clock.t += 60
    assert alerter.decide({}, {"k": "up"}).recovered  # it was mentioned


def test_flap_back_inside_the_window_clears_silently():
    clock = Clock()
    alerter = Alerter(cooldown_seconds=3600 * 6, flap_window_seconds=3600, clock=clock)
    alerter.decide({"k": "broken"})
    alerter.decide({}, {"k": ""})
    clock.t += 60
    alerter.decide({"k": "broken"})
    clock.t += 60
    assert not alerter.decide({}, {"k": ""})


def test_reraise_after_the_window_is_a_fresh_page():
    clock = Clock()
    alerter = Alerter(cooldown_seconds=6 * 3600, flap_window_seconds=3600, clock=clock)
    alerter.decide({"k": "broken"})
    alerter.decide({}, {"k": ""})
    clock.t += 3601
    assert alerter.decide({"k": "broken"}).raised == ["broken"]


def test_failed_delivery_rolls_back_only_its_own_keys():
    """Lead review (D): a concurrent task's delivered decision survives."""
    clock = Clock()
    alerter = Alerter(cooldown_seconds=3600, clock=clock)
    snapshot = alerter.snapshot()
    mine = alerter.decide({"mine": "x"})
    other = alerter.decide({"other": "y"})  # another task, delivered fine
    assert other.keys == ["other"]
    alerter.rollback(snapshot, mine.keys)
    assert "mine" not in alerter.active and "other" in alerter.active


def test_flapping_memory_survives_a_restart(tmp_path):
    clock = Clock()
    path = tmp_path / "alerts.json"
    first = Alerter(cooldown_seconds=3600, state_path=path, clock=clock)
    first.decide({"k": "broken"})
    first.decide({}, {"k": ""})
    clock.t += 60
    assert not Alerter(cooldown_seconds=3600, state_path=path, clock=clock).decide({"k": "x"})


# ── Out-of-process DM (REST, no discord.py) ──────────────────────────


class _Resp:
    def __init__(self, body: dict) -> None:
        self.status = 200
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_discord(monkeypatch, calls, fail_for=()):
    def fake_urlopen(request, timeout):
        body = json.loads(request.data)
        calls.append({"url": request.full_url, "body": body,
                      "auth": request.get_header("Authorization")})
        if body.get("recipient_id") in fail_for:
            raise OSError("50007")
        if request.full_url.endswith("/users/@me/channels"):
            return _Resp({"id": f"dm-{body['recipient_id']}"})
        return _Resp({"id": "msg"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def test_post_dm_sync_opens_a_dm_and_posts(monkeypatch):
    calls: list[dict] = []
    _fake_discord(monkeypatch, calls)
    monkeypatch.setenv("DISCORD_TOKEN", "bot-token")
    monkeypatch.setenv("ALERT_DM_USER_IDS", str(OWNER))
    monkeypatch.delenv("ALERTS_ENABLED", raising=False)

    assert post_dm_sync("@everyone the bot is down") is True
    assert calls[0]["url"].endswith("/users/@me/channels")
    assert calls[0]["body"] == {"recipient_id": str(OWNER)}
    assert calls[0]["auth"] == "Bot bot-token"
    assert calls[1]["url"].endswith(f"/channels/dm-{OWNER}/messages")
    assert calls[1]["body"]["allowed_mentions"] == {"parse": []}


def test_post_dm_sync_off_switches_and_failures(monkeypatch):
    calls: list[dict] = []
    _fake_discord(monkeypatch, calls, fail_for=(str(OWNER),))
    monkeypatch.setenv("DISCORD_TOKEN", "bot-token")
    monkeypatch.setenv("ALERT_DM_USER_IDS", f"{OWNER},{SECOND}")
    monkeypatch.delenv("ALERTS_ENABLED", raising=False)
    # One closed DM does not stop the next recipient.
    assert post_dm_sync("x") is True
    assert any(c["url"].endswith(f"dm-{SECOND}/messages") for c in calls)

    calls.clear()
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    assert post_dm_sync("x") is False and calls == []

    monkeypatch.delenv("ALERTS_ENABLED")
    monkeypatch.setenv("ALERT_DM_USER_IDS", "")
    assert post_dm_sync("x") is False and calls == []


# ── Delivery: closed DMs, deduplicated per user ──────────────────────


@pytest.mark.asyncio
async def test_closed_dms_are_said_once_per_user_then_recovered():
    from gosha.alerts import report_closed_dms

    bot = FakeBot()
    await report_closed_dms(bot, [7, 8, 7])
    assert len(bot.dms(OWNER)) == 1
    assert "`7`" in bot.dms(OWNER)[0] and "`8`" in bot.dms(OWNER)[0]

    # Every later cycle used to repeat the list.
    for _ in range(5):
        await report_closed_dms(bot, [7, 8])
    await report_closed_dms(bot, [7, 8, 9])
    assert len(bot.dms(OWNER)) == 2 and "`9`" in bot.dms(OWNER)[1]
    assert "`7`" not in bot.dms(OWNER)[1]

    # Nothing to deliver to 8 this cycle: not a recovery.
    await report_closed_dms(bot, [], delivered=[])
    assert len(bot.dms(OWNER)) == 2
    await report_closed_dms(bot, [], delivered=[8, 12345])
    assert len(bot.dms(OWNER)) == 3
    assert "recovered" in bot.dms(OWNER)[2] and "`8`" in bot.dms(OWNER)[2]


@pytest.mark.asyncio
async def test_closed_dms_respect_the_off_switch():
    from gosha.alerts import report_closed_dms

    bot = FakeBot(enabled=False)
    await report_closed_dms(bot, [7])
    assert bot.dms(OWNER) == []


# ── Stale report: a cycle that recorded nothing re-sends nothing ─────


@pytest.mark.asyncio
async def test_a_report_is_judged_once(monkeypatch):
    from gosha import alerts
    from gosha.scrape_health import ScrapeHealth

    events: list[dict] = []

    class Store:
        async def emit(self, event_type, payload=None, **kw):
            events.append(payload)

    monkeypatch.setattr("gosha.events.get_event_store", lambda: Store())
    health = ScrapeHealth()
    health.record_cycle(["ejobs"], {})
    report = health.record_cycle(["ejobs"], {})  # second empty cycle: pages
    bot = FakeBot()
    assert await alerts.publish_scrape_report(bot, 0, report)
    # alerter forgotten (e.g. restart without state) — the stale report
    # would page again if it were judged a second time.
    reset_alerter()
    assert not await alerts.publish_scrape_report(bot, 0, health.last_report)
    assert len(bot.dms(OWNER)) == 1
    # Still a finished cycle for the freshness endpoint.
    assert events[-1] == {"total": 0, "sources": 0, "idle": True}
    assert events[0] == {"total": 0, "sources": 1}


@pytest.mark.asyncio
async def test_cycle_event_carries_the_bots_scrape_interval(monkeypatch):
    """The site watchdog derives its stale limit from it (4h prod schedule)."""
    from gosha import alerts
    from gosha.scrape_health import ScrapeHealth

    events: list[dict] = []

    class Store:
        async def emit(self, event_type, payload=None, **kw):
            events.append(payload)

    monkeypatch.setattr("gosha.events.get_event_store", lambda: Store())
    bot = FakeBot()
    bot.settings.scrape_interval_minutes = 240
    report = ScrapeHealth().record_cycle(["ejobs"], {"ejobs": 5})
    await alerts.publish_scrape_report(bot, 0, report)
    await alerts.publish_scrape_report(bot, 0, report)  # idle re-publish
    assert events == [
        {"total": 5, "sources": 1, "interval_minutes": 240},
        {"total": 0, "sources": 0, "idle": True, "interval_minutes": 240},
    ]


# ── Undelivered alerts are retried, not counted as said ──────────────


@pytest.mark.asyncio
async def test_undelivered_alert_is_raised_again_next_check(tmp_path):
    bot = FakeBot(closed_dms=(OWNER,))  # every DM to the owner fails
    alerter = Alerter(cooldown_seconds=3600, state_path=tmp_path / "s.json", clock=Clock())
    sink = sink_for_bot(bot)
    assert await sink.send("x") is False
    await alerter.update(sink, {"k": "broken"})
    assert "k" not in alerter.active
    assert "k" not in Alerter(state_path=tmp_path / "s.json").active  # persisted too

    bot.users[OWNER].fail = False
    await alerter.update(sink, {"k": "broken"})
    assert len(bot.dms(OWNER)) == 1 and "broken" in bot.dms(OWNER)[0]


@pytest.mark.asyncio
async def test_undelivered_recovery_is_said_again_next_check():
    bot = FakeBot()
    alerter = Alerter(cooldown_seconds=3600, clock=Clock())
    sink = sink_for_bot(bot)
    await alerter.update(sink, {"k": "broken"})
    bot.users[OWNER].fail = True
    await alerter.update(sink, {}, {"k": "fine again"})
    assert "k" in alerter.active
    bot.users[OWNER].fail = False
    await alerter.update(sink, {}, {"k": "fine again"})
    assert len(bot.dms(OWNER)) == 2 and "fine again" in bot.dms(OWNER)[1]


@pytest.mark.asyncio
async def test_scrape_report_retried_when_the_dm_fails(monkeypatch):
    from gosha import alerts
    from gosha.scrape_health import ScrapeHealth

    class Store:
        async def emit(self, *a, **kw):
            pass

    monkeypatch.setattr("gosha.events.get_event_store", lambda: Store())
    bot = FakeBot(closed_dms=(OWNER,))
    health = ScrapeHealth()
    health.record_cycle(["ejobs"], {})
    await alerts.publish_scrape_report(bot, 0, health.record_cycle(["ejobs"], {}))
    bot.users[OWNER].fail = False
    await alerts.publish_scrape_report(bot, 0, health.record_cycle(["ejobs"], {}))
    assert len(bot.dms(OWNER)) == 1 and "0 jobs" in bot.dms(OWNER)[0]


def test_four_hour_cycle_overrun_pages_once_then_recovers():
    """Prod: cycles every 4h, normally 35–60 min. One that is still running
    at the next start (APScheduler MAX_INSTANCES) is abnormal: one page,
    "recovered" when it finishes, nothing for normal cycles after."""
    from apscheduler.events import (
        EVENT_JOB_EXECUTED,
        EVENT_JOB_MAX_INSTANCES,
        JobExecutionEvent,
        JobSubmissionEvent,
    )

    clock = Clock()
    alerter = Alerter(cooldown_seconds=6 * 3600, clock=clock)
    jobs = {"scrape_cycle"}
    skipped = JobSubmissionEvent(EVENT_JOB_MAX_INSTANCES, "scrape_cycle", "default", [])
    ran = JobExecutionEvent(EVENT_JOB_EXECUTED, "scrape_cycle", "default", None)

    page = alerter.decide(*scheduler_event_conditions(skipped, jobs))  # 4h mark, still running
    assert len(page.raised) == 1 and "still going" in page.raised[0]
    clock.t += 30 * 60
    assert alerter.decide(*scheduler_event_conditions(ran, jobs)).recovered
    for _ in range(6):  # a day of normal cycles
        clock.t += 4 * 3600
        assert not alerter.decide(*scheduler_event_conditions(ran, jobs))
