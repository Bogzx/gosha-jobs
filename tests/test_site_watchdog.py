"""Out-of-process watchdog (f, g): the owner hears about a dead bot or site."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from scripts import site_watchdog as wd

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
BASE = "https://gosha.test"


def fetcher(health=200, freshness=None, freshness_status=200, raise_exc=None):
    def fetch(url):
        if raise_exc is not None:
            raise raise_exc
        if url.endswith("/api/v1/health"):
            return health, b'{"ok": true}'
        return freshness_status, json.dumps(freshness or {}).encode()
    return fetch


def fresh(cycle_age_h=0.5, new_age_h=2.0, jobs=120):
    return {
        "last_scrape_cycle_at": (NOW - timedelta(hours=cycle_age_h)).isoformat(),
        "last_scrape_cycle_jobs": jobs,
        "last_new_job_at": (NOW - timedelta(hours=new_age_h)).isoformat(),
        "last_job_seen_at": (NOW - timedelta(hours=cycle_age_h)).isoformat(),
    }


def test_healthy_site_clears_everything():
    failing, cleared = wd.evaluate(BASE, fetcher(freshness=fresh()), now=NOW)
    assert failing == {}
    assert {wd.KEY_DOWN, wd.KEY_SCRAPE, wd.KEY_ENDPOINT} <= set(cleared)


def test_server_down_is_reported():
    failing, _ = wd.evaluate(BASE, fetcher(raise_exc=OSError("Connection refused")), now=NOW)
    assert "not answering" in failing[wd.KEY_DOWN]
    failing, _ = wd.evaluate(BASE, fetcher(health=502), now=NOW)
    assert "HTTP 502" in failing[wd.KEY_DOWN]


def test_hung_or_crashed_bot_is_reported():
    """The API is up but no cycle has finished for hours: the bot is gone."""
    data = fresh(cycle_age_h=7)
    data["scrape_interval_minutes"] = 60
    failing, cleared = wd.evaluate(BASE, fetcher(freshness=data), now=NOW)
    assert "No scrape cycle has finished for 7.0h" in failing[wd.KEY_SCRAPE]
    assert "limit 3h" in failing[wd.KEY_SCRAPE]
    assert wd.KEY_DOWN in cleared


def _with_interval(cycle_age_h, interval):
    data = fresh(cycle_age_h=cycle_age_h)
    if interval is not None:
        data["scrape_interval_minutes"] = interval
    return fetcher(freshness=data)


def test_four_hour_schedule_gap_is_not_a_dead_bot():
    """Prod starts a cycle every 4h; the newest record is up to ~4h old."""
    failing, _ = wd.evaluate(BASE, _with_interval(4.0, 240), now=NOW)
    assert failing == {}
    failing, _ = wd.evaluate(BASE, _with_interval(8.9, 240), now=NOW)
    assert failing == {}


def test_four_hour_schedule_pages_after_two_missed_cycles():
    failing, _ = wd.evaluate(BASE, _with_interval(9.2, 240), now=NOW)
    assert "limit 9h" in failing[wd.KEY_SCRAPE]


def test_unknown_interval_assumes_nine_hours_not_three():
    """An older deploy's endpoint does not report the interval."""
    assert wd.evaluate(BASE, _with_interval(8.5, None), now=NOW)[0] == {}
    failing, _ = wd.evaluate(BASE, _with_interval(9.5, None), now=NOW)
    assert "limit 9h" in failing[wd.KEY_SCRAPE]


def test_configured_limit_only_ever_raises_the_schedule_floor():
    assert wd.scrape_stale_limit_hours(None, 240) == 9
    assert wd.scrape_stale_limit_hours(2, 240) == 9  # cannot page on a normal gap
    assert wd.scrape_stale_limit_hours(12, 240) == 12
    assert wd.scrape_stale_limit_hours(None, 60) == 3
    assert wd.scrape_stale_limit_hours(None, None) == 9
    assert wd.scrape_stale_limit_hours(None, "240") == 9  # junk → the safe default
    assert wd.scrape_stale_limit_hours(None, True) == 9


def test_main_reads_the_configured_limit(tmp_path, dm_env, monkeypatch):
    sent, _ = dm_env
    monkeypatch.setenv("WATCHDOG_SCRAPE_STALE_HOURS", "12")
    monkeypatch.setattr(wd, "datetime", _FrozenDatetime)
    state = tmp_path / "s.json"
    assert wd.main(["--url", BASE, "--state-file", str(state)],
                   fetch=_with_interval(10, 240)) == 0
    assert sent == []


def test_no_new_jobs_is_left_to_the_bot():
    """data:no_new_jobs (in-process) owns it; judging it here too paged twice."""
    failing, cleared = wd.evaluate(BASE, fetcher(freshness=fresh(new_age_h=30)), now=NOW)
    assert failing == {}
    assert not any("no_new_jobs" in key for key in cleared)


def test_retired_key_in_carried_state_is_dropped_silently(tmp_path, dm_env, monkeypatch):
    sent, _ = dm_env
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"active": {"site:no_new_jobs": {
        "since": 1.0, "last_sent": 1.0, "text": "No new job stored for 30h"}}}))
    monkeypatch.setattr(wd, "datetime", _FrozenDatetime)
    assert wd.main(["--url", BASE, "--state-file", str(state)], fetch=fetcher(freshness=fresh())) == 0
    assert sent == []
    assert "site:no_new_jobs" not in json.loads(state.read_text())["active"]


def test_missing_freshness_endpoint_says_the_watchdog_is_blind():
    failing, _ = wd.evaluate(BASE, fetcher(freshness_status=404), now=NOW)
    assert wd.KEY_ENDPOINT in failing


def test_falls_back_to_last_seen_job_before_the_first_cycle_event():
    data = fresh()
    data["last_scrape_cycle_at"] = None
    failing, _ = wd.evaluate(BASE, fetcher(freshness=data), now=NOW)
    assert failing == {}


@pytest.fixture
def dm_env(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "bot-token")
    monkeypatch.setenv("ALERT_DM_USER_IDS", "424242")
    monkeypatch.delenv("ALERTS_ENABLED", raising=False)
    sent: list[str] = []
    ok = {"value": True}

    def fake_dm(text, *args, **kwargs):
        sent.append(text)
        return ok["value"]

    monkeypatch.setattr(wd, "post_dm_sync", fake_dm)
    return sent, ok


def test_main_dms_once_per_outage_then_recovery(tmp_path, dm_env, monkeypatch):
    sent, _ = dm_env
    state = tmp_path / "state.json"
    down = fetcher(raise_exc=OSError("refused"))
    # Green once the owner has the DM: red runs email every 30 minutes.
    assert wd.main(["--url", BASE, "--state-file", str(state)], fetch=down) == 0
    assert wd.main(["--url", BASE, "--state-file", str(state)], fetch=down) == 0
    assert len(sent) == 1 and "not answering" in sent[0]

    monkeypatch.setattr(wd, "datetime", _FrozenDatetime)
    up = fetcher(freshness=fresh())
    assert wd.main(["--url", BASE, "--state-file", str(state)], fetch=up) == 0
    assert len(sent) == 2 and "recovered" in sent[1]


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def test_undelivered_dm_is_retried_next_run(tmp_path, dm_env):
    sent, ok = dm_env
    ok["value"] = False
    state = tmp_path / "state.json"
    down = fetcher(raise_exc=OSError("refused"))
    # Red while nobody was told: GitHub's failure email is the fallback.
    assert wd.main(["--url", BASE, "--state-file", str(state)], fetch=down) == 1
    ok["value"] = True
    assert wd.main(["--url", BASE, "--state-file", str(state)], fetch=down) == 0
    assert len(sent) == 2  # the failed one is said again, not swallowed


def test_alerts_disabled_sends_nothing(tmp_path, dm_env, monkeypatch):
    sent, _ = dm_env
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    down = fetcher(raise_exc=OSError("refused"))
    assert wd.main(["--url", BASE, "--state-file", str(tmp_path / "s.json")], fetch=down) == 1
    assert sent == []


def test_unconfigured_dm_keeps_the_run_red(tmp_path, dm_env, monkeypatch):
    sent, _ = dm_env
    monkeypatch.setenv("ALERT_DM_USER_IDS", "")
    down = fetcher(raise_exc=OSError("refused"))
    state = tmp_path / "s.json"
    assert wd.main(["--url", BASE, "--state-file", str(state)], fetch=down) == 1
    assert sent == []


def test_dry_run_leaves_the_state_file_alone(tmp_path, dm_env):
    sent, _ = dm_env
    down = fetcher(raise_exc=OSError("refused"))
    missing = tmp_path / "never.json"
    assert wd.main(["--url", BASE, "--state-file", str(missing), "--dry-run"], fetch=down) == 1
    assert not missing.exists()

    state = tmp_path / "state.json"
    wd.main(["--url", BASE, "--state-file", str(state)], fetch=down)  # real run: said once
    before = state.read_bytes()
    wd.main(["--url", BASE, "--state-file", str(state), "--dry-run"],
            fetch=fetcher(freshness=fresh()))
    assert state.read_bytes() == before
    assert len(sent) == 1


def _at(monkeypatch, when):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    monkeypatch.setattr(wd, "datetime", Frozen)
    monkeypatch.setattr(wd.Alerter, "__init__", _clocked(wd.Alerter.__init__, when))


def _clocked(init, when):
    def __init__(self, *args, **kwargs):
        init(self, *args, **kwargs)
        self.clock = lambda: when.timestamp()
    return __init__


def test_site_down_again_soon_after_recovery_pages_and_stays_red(tmp_path, dm_env, monkeypatch):
    """Lead review (A): down, recovered, down again 10 min later used to be
    no DM and a green run for ~6h."""
    sent, _ = dm_env
    state = tmp_path / "state.json"
    args = ["--url", BASE, "--state-file", str(state)]
    down = fetcher(raise_exc=OSError("refused"))
    t0 = NOW
    _at(monkeypatch, t0)
    assert wd.main(args, fetch=down) == 0  # DM delivered: green
    _at(monkeypatch, t0 + timedelta(minutes=30))
    assert wd.main(args, fetch=fetcher(freshness=fresh(cycle_age_h=-0.5))) == 0
    assert len(sent) == 2 and "recovered" in sent[1]

    _at(monkeypatch, t0 + timedelta(minutes=40))
    assert wd.main(args, fetch=down) == 1  # maybe a flap: silent, but red
    _at(monkeypatch, t0 + timedelta(minutes=70))
    assert wd.main(args, fetch=down) == 1
    assert len(sent) == 2
    _at(monkeypatch, t0 + timedelta(minutes=100))
    assert wd.main(args, fetch=down) == 0  # outlasted the hour: paged
    assert len(sent) == 3 and "failing again" in sent[2]


def test_alerts_off_is_not_counted_as_told(tmp_path, dm_env, monkeypatch):
    """Lead review (B): run 2 used to exit 0 although nobody was told."""
    sent, _ = dm_env
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    state = tmp_path / "s.json"
    down = fetcher(raise_exc=OSError("refused"))
    for _ in range(2):
        assert wd.main(["--url", BASE, "--state-file", str(state)], fetch=down) == 1
    monkeypatch.delenv("ALERTS_ENABLED")
    assert wd.main(["--url", BASE, "--state-file", str(state)], fetch=down) == 0
    assert len(sent) == 1  # said as soon as it could be


def test_missing_secrets_are_not_counted_as_told(tmp_path, dm_env, monkeypatch):
    sent, _ = dm_env
    monkeypatch.setenv("DISCORD_TOKEN", "")
    state = tmp_path / "s.json"
    down = fetcher(raise_exc=OSError("refused"))
    for _ in range(2):
        assert wd.main(["--url", BASE, "--state-file", str(state)], fetch=down) == 1
    assert sent == []


# ── Workflows: wired to DM the owner, never a channel/webhook ───────

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def test_site_watchdog_workflow_runs_the_script_with_dm_secrets():
    wf = yaml.safe_load((WORKFLOWS / "site-watchdog.yml").read_text())
    job = wf["jobs"]["watchdog"]
    assert job["env"]["DISCORD_TOKEN"] == "${{ secrets.DISCORD_BOT_TOKEN }}"
    assert job["env"]["ALERT_DM_USER_IDS"] == "${{ secrets.ALERT_DM_USER_IDS }}"
    runs = " ".join(step.get("run", "") for step in job["steps"])
    assert "scripts/site_watchdog.py" in runs


def test_smoke_workflow_dms_on_failure_change_and_recovery():
    text = (WORKFLOWS / "scraper-smoke.yml").read_text()
    wf = yaml.safe_load(text)
    steps = wf["jobs"]["smoke"]["steps"]
    track = next(s for s in steps if s.get("id") == "track")
    script = track["with"]["script"]
    assert track["env"]["DISCORD_BOT_TOKEN"] == "${{ secrets.DISCORD_BOT_TOKEN }}"
    assert "/users/@me/channels" in script
    # Opened, commented on a changed signature, closed on recovery.
    assert script.count("await page(") == 3
    fallback = next(s for s in steps if s.get("name", "").startswith("Page if"))
    assert "post_dm_sync" in fallback["run"]
    assert "webhook" not in text.lower()
