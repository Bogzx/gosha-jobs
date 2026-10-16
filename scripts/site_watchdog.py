"""Out-of-process dead-man switch: is GOSHA still scraping, seen from outside?

Every in-process alert (gosha/alerts.py) needs a running, unwedged bot with
a working Discord session. This covers the cases where there is none: the
bot crashed or is crash-looping, the event loop is hung, the server or its
database is down. It runs on GitHub Actions (.github/workflows/
site-watchdog.yml), reads the public, unauthenticated

    GET {base}/api/v1/health              API process up
    GET {base}/api/v1/health/freshness    last scrape cycle / last new job

and DMs the owner on Discord (bot token over the REST API — no bot
process needed) when:

* the site does not answer               (key site:down)
* no scrape cycle finished for N hours   (key site:scrape_stale)
* no new job was stored for M hours      (key site:no_new_jobs)

Dedup, reminders and "recovered" messages use the same Alerter as the bot,
with its state in a JSON file the workflow carries between runs (actions/
cache). Exit code 1 while anything is failing, so the run also shows red
(and GitHub mails the workflow's owner) even if the DM cannot be sent.

    python scripts/site_watchdog.py --state-file state.json
    python scripts/site_watchdog.py --dry-run     # print, post nothing

Env: GOSHA_PUBLIC_URL (default https://gosha.bogdantruta.com),
DISCORD_TOKEN (the bot token), ALERT_DM_USER_IDS (comma-separated numeric
user IDs), ALERTS_ENABLED, WATCHDOG_SCRAPE_STALE_HOURS (3),
WATCHDOG_NEW_JOB_STALE_HOURS (24), WATCHDOG_REMIND_HOURS (6).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

# `python scripts/site_watchdog.py` puts scripts/ on sys.path, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gosha.alerts import (  # noqa: E402
    Alerter,
    alerts_enabled,
    format_dispatch,
    post_dm_sync,
)

DEFAULT_BASE_URL = "https://gosha.bogdantruta.com"
TIMEOUT = 20

KEY_DOWN = "site:down"
KEY_SCRAPE = "site:scrape_stale"
KEY_NEW_JOBS = "site:no_new_jobs"
KEY_ENDPOINT = "site:freshness_endpoint"

Fetch = Callable[[str], tuple[int, bytes]]


def http_get(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "gosha-site-watchdog"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def _parse_time(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _hours(delta_seconds: float) -> str:
    return f"{delta_seconds / 3600:.1f}h"


def evaluate(
    base_url: str,
    fetch: Fetch = http_get,
    now: datetime | None = None,
    scrape_stale_hours: float = 3.0,
    new_job_stale_hours: float = 24.0,
) -> tuple[dict[str, str], dict[str, str]]:
    """Probe the site → (failing, cleared) condition maps for the Alerter."""
    now = now or datetime.now(timezone.utc)
    base = base_url.rstrip("/")
    failing: dict[str, str] = {}
    cleared: dict[str, str] = {}

    try:
        status, _ = fetch(f"{base}/api/v1/health")
    except Exception as exc:  # DNS, TLS, timeout, refused
        status, error = 0, exc
    else:
        error = None
    if status != 200:
        why = f"{type(error).__name__}: {error}" if error else f"HTTP {status}"
        failing[KEY_DOWN] = (
            f"**{base} is not answering** ({why}). The site, the API container "
            f"or the whole server is down."
        )
        return failing, cleared
    cleared[KEY_DOWN] = f"{base} answers again."

    try:
        status, body = fetch(f"{base}/api/v1/health/freshness")
        data = json.loads(body) if status == 200 else None
    except Exception:
        status, data = 0, None
    if not isinstance(data, dict):
        failing[KEY_ENDPOINT] = (
            f"The freshness endpoint answered HTTP {status or 'error'}, so this "
            f"watchdog cannot tell whether the bot is scraping. Deploy the "
            f"current code (it adds /api/v1/health/freshness)."
        )
        return failing, cleared
    cleared[KEY_ENDPOINT] = "The freshness endpoint answers again."

    # Scrape cycles stamp an event; before the first one (or if events are
    # unavailable) the newest re-seen job is the next best evidence.
    last_cycle = _parse_time(data.get("last_scrape_cycle_at")) or _parse_time(
        data.get("last_job_seen_at")
    )
    if last_cycle is None:
        failing[KEY_SCRAPE] = "No scrape cycle has ever been recorded."
    else:
        age = (now - last_cycle).total_seconds()
        if age > scrape_stale_hours * 3600:
            failing[KEY_SCRAPE] = (
                f"**No scrape cycle has finished for {_hours(age)}** (last: "
                f"{last_cycle:%Y-%m-%d %H:%M} UTC, limit {scrape_stale_hours:g}h). "
                f"The bot is crashed, crash-looping, hung or stuck in a cycle: "
                f"`docker compose -f docker-compose.prod.yml ps` / `logs bot`."
            )
        else:
            jobs = data.get("last_scrape_cycle_jobs")
            cleared[KEY_SCRAPE] = (
                f"Scrape cycles are running again (last {_hours(age)} ago"
                + (f", {jobs} jobs" if isinstance(jobs, int) else "") + ")."
            )

    last_new = _parse_time(data.get("last_new_job_at"))
    if last_new is not None:
        age = (now - last_new).total_seconds()
        if age > new_job_stale_hours * 3600:
            failing[KEY_NEW_JOBS] = (
                f"**No new job stored for {_hours(age)}** (last: "
                f"{last_new:%Y-%m-%d %H:%M} UTC, limit {new_job_stale_hours:g}h). "
                f"Cycles may run but every board is returning nothing new."
            )
        else:
            cleared[KEY_NEW_JOBS] = f"New jobs are arriving again (newest {_hours(age)} old)."
    return failing, cleared


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def main(argv: list[str] | None = None, fetch: Fetch = http_get) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=os.getenv("GOSHA_PUBLIC_URL") or DEFAULT_BASE_URL)
    parser.add_argument("--state-file", type=Path, default=Path("site-watchdog-state.json"))
    parser.add_argument("--dry-run", action="store_true", help="print, post nothing")
    args = parser.parse_args(argv)

    failing, cleared = evaluate(
        args.url, fetch,
        scrape_stale_hours=_env_float("WATCHDOG_SCRAPE_STALE_HOURS", 3.0),
        new_job_stale_hours=_env_float("WATCHDOG_NEW_JOB_STALE_HOURS", 24.0),
    )
    try:
        previous_state = args.state_file.read_bytes()
    except OSError:
        previous_state = None
    alerter = Alerter(
        cooldown_seconds=_env_float("WATCHDOG_REMIND_HOURS", 6.0) * 3600,
        state_path=args.state_file,
    )
    dispatch = alerter.decide(failing, cleared)

    for text in failing.values():
        print(f"FAILING: {text}")
    if not failing:
        print("OK: the site answers and scraping is fresh.")

    if dispatch:
        message = format_dispatch("site watchdog", dispatch)
        if args.dry_run:
            print(f"--- would post ---\n{message}")
        elif not alerts_enabled():
            print("Alerts disabled (ALERTS_ENABLED=false); nobody was told.")
        elif not (os.getenv("DISCORD_TOKEN", "").strip()
                  and os.getenv("ALERT_DM_USER_IDS", "").strip()):
            print(
                "::warning::DISCORD_TOKEN / ALERT_DM_USER_IDS not set; nobody was told.",
                file=sys.stderr,
            )
        else:
            if not post_dm_sync(message):
                print("::error::Sending the Discord DM failed.", file=sys.stderr)
                # Undelivered: forget this run's decisions so the next run
                # says it again instead of treating it as already sent.
                if previous_state is None:
                    args.state_file.unlink(missing_ok=True)
                else:
                    args.state_file.write_bytes(previous_state)
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
