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
