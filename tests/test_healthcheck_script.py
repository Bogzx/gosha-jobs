"""Run scripts/healthcheck.py the way the container healthcheck does."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "healthcheck.py"


def _run_bot_check(tmp_path: Path, heartbeat: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["GOSHA_HEARTBEAT_FILE"] = str(heartbeat)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "bot"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )


def test_bot_check_imports_gosha_and_passes_on_fresh_heartbeat(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    heartbeat.write_text(str(time.time()))
    result = _run_bot_check(tmp_path, heartbeat)
    assert "ModuleNotFoundError" not in result.stderr
    assert result.returncode == 0, result.stderr


def test_bot_check_fails_without_heartbeat(tmp_path):
    result = _run_bot_check(tmp_path, tmp_path / "missing")
    assert "ModuleNotFoundError" not in result.stderr
    assert result.returncode == 1
    assert "no heartbeat" in result.stderr


# ── Watchdog (gosha/heartbeat.py) ─────────────────────────────────────────
# Docker never restarts an "unhealthy" container; the watchdog turns a
# wedged event loop into an exit, which `restart: unless-stopped` does act
# on. Run in a subprocess: it ends the process it lives in.

def _run_watchdog(tmp_path: Path, body: str, **env_extra: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["GOSHA_HEARTBEAT_FILE"] = str(tmp_path / "heartbeat")
    # The watchdog really pages before exiting; a machine with DISCORD_TOKEN
    # and ALERT_DM_USER_IDS exported must not get a real DM from a test.
    env["ALERTS_ENABLED"] = "false"
    env.update(env_extra)
    code = "import time\nfrom gosha import heartbeat\n" + body
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=SCRIPT.parent.parent, env=env, capture_output=True, text=True, timeout=30,
    )


def test_watchdog_exits_when_the_heartbeat_stops(tmp_path):
    result = _run_watchdog(tmp_path, (
        "heartbeat.touch()\n"
        "heartbeat.start_watchdog(max_age=0.3, poll=0.05)\n"
        "time.sleep(20)\n"  # the 'wedged loop': no more touch() calls
        "print('still running')\n"
    ))
    assert result.returncode == 1, result.stderr
    assert "still running" not in result.stdout
    # The stack of the stuck thread is in the log, so the restart does not
    # erase the evidence.
    assert 'File "<string>", line 5 in <module>' in result.stderr  # the time.sleep(20)


def test_watchdog_leaves_a_beating_process_alone(tmp_path):
    result = _run_watchdog(tmp_path, (
        "heartbeat.touch()\n"
        "heartbeat.start_watchdog(max_age=0.5, poll=0.05)\n"
        "for _ in range(20):\n"
        "    heartbeat.touch()\n"
        "    time.sleep(0.1)\n"
        "print('finished')\n"
    ))
    assert result.returncode == 0, result.stderr
    assert "finished" in result.stdout


def test_watchdog_waits_for_a_first_heartbeat(tmp_path):
    """No stamp yet (or a stale file from a previous container) is not a hang."""
    (tmp_path / "heartbeat").write_text("0")
    os.utime(tmp_path / "heartbeat", (0, 0))
    result = _run_watchdog(tmp_path, (
        "heartbeat.start_watchdog(max_age=0.1, poll=0.05)\n"
        "time.sleep(1)\n"
        "print('finished')\n"
    ))
    assert result.returncode == 0, result.stderr
    assert "finished" in result.stdout


def test_watchdog_can_be_disabled(tmp_path):
    result = _run_watchdog(tmp_path, (
        "assert heartbeat.start_watchdog() is None\n"
        "print('disabled')\n"
    ), GOSHA_WATCHDOG_SECONDS="0")
    assert result.returncode == 0, result.stderr
    assert "disabled" in result.stdout


def test_watchdog_subprocess_never_pages_for_real(tmp_path):
    """The env the watchdog tests run under has alerts off, whatever the
    developer's shell exports."""
    result = _run_watchdog(tmp_path, (
        "from gosha import alerts\n"
        "print('enabled' if alerts.alerts_enabled() else 'off')\n"
    ), DISCORD_TOKEN="x", ALERT_DM_USER_IDS="1")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "off"


def test_watchdog_dms_the_owner_before_exiting(monkeypatch, tmp_path):
    """(f) A wedged loop cannot use discord.py; the REST DM path is used."""
    from gosha import alerts, heartbeat

    monkeypatch.setenv("GOSHA_HEARTBEAT_FILE", str(tmp_path / "heartbeat"))
    sent: list[str] = []
    monkeypatch.setattr(alerts, "post_dm_sync", lambda text, **kw: sent.append(text) or True)
    heartbeat._page_owner(950, 900)
    assert sent and "no heartbeat for 950s" in sent[0]


def test_watchdog_paging_failure_never_blocks_the_exit(monkeypatch, tmp_path):
    from gosha import alerts, heartbeat

    monkeypatch.setenv("GOSHA_HEARTBEAT_FILE", str(tmp_path / "heartbeat"))

    def boom(text, **kw):
        raise RuntimeError("network")

    monkeypatch.setattr(alerts, "post_dm_sync", boom)
    heartbeat._page_owner(950, 900)  # must not raise


def test_watchdog_pages_at_most_once_an_hour(monkeypatch, tmp_path):
    """A bot that wedges after every restart must not page on every restart."""
    from gosha import alerts, heartbeat

    monkeypatch.setenv("GOSHA_HEARTBEAT_FILE", str(tmp_path / "heartbeat"))
    sent: list[str] = []
    monkeypatch.setattr(alerts, "post_dm_sync", lambda text, **kw: sent.append(text) or True)
    heartbeat._page_owner(950, 900)
    heartbeat._page_owner(950, 900)  # the next process, minutes later
    assert len(sent) == 1
    assert (tmp_path / "watchdog_paged_at").exists()

    monkeypatch.setattr(heartbeat.time, "time", lambda: 10**10)  # far later
    heartbeat._page_owner(950, 900)
    assert len(sent) == 2


def test_watchdog_undelivered_page_is_not_counted(monkeypatch, tmp_path):
    from gosha import alerts, heartbeat

    monkeypatch.setenv("GOSHA_HEARTBEAT_FILE", str(tmp_path / "heartbeat"))
    attempts: list[str] = []
    monkeypatch.setattr(alerts, "post_dm_sync", lambda text, **kw: attempts.append(text) and False)
    heartbeat._page_owner(950, 900)
    heartbeat._page_owner(950, 900)
    assert len(attempts) == 2
    assert not (tmp_path / "watchdog_paged_at").exists()
