"""Container healthcheck for the bot and api services.

Kept in Python because the runtime image is python:3.11-slim — it has no
curl and no wget, so a shell-based probe would need extra packages baked
into a production image just to answer "are you alive".

    python scripts/healthcheck.py api    # HTTP GET /api/v1/health
    python scripts/healthcheck.py bot    # scheduler heartbeat freshness

Exit 0 healthy, 1 unhealthy.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

API_HEALTH_URL = os.getenv("HEALTHCHECK_URL", "http://127.0.0.1:8000/api/v1/health")
API_TIMEOUT = 5


def check_api() -> int:
    try:
        with urllib.request.urlopen(API_HEALTH_URL, timeout=API_TIMEOUT) as resp:
            if resp.status != 200:
                print(f"api health returned {resp.status}", file=sys.stderr)
                return 1
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"api health unreachable: {exc}", file=sys.stderr)
        return 1
    return 0


def check_bot() -> int:
    # Imported lazily so `healthcheck.py api` does not need the package.
    from gosha.heartbeat import HEARTBEAT_STALE_SECONDS, age_seconds, heartbeat_path

    age = age_seconds()
    if age is None:
        print(f"no heartbeat at {heartbeat_path()}", file=sys.stderr)
        return 1
    if age > HEARTBEAT_STALE_SECONDS:
        print(
            f"heartbeat is {age:.0f}s old (limit {HEARTBEAT_STALE_SECONDS}s) — "
            "the event loop is not running scheduled jobs",
            file=sys.stderr,
        )
        return 1
    return 0


CHECKS = {"api": check_api, "bot": check_bot}


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    check = CHECKS.get(target)
    if check is None:
        print(f"usage: healthcheck.py [{'|'.join(CHECKS)}]", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(check())
