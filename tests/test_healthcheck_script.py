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
