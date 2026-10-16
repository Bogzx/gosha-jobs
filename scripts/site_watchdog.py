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

N follows the bot's schedule: max(WATCHDOG_SCRAPE_STALE_HOURS or 3,
2 × scrape interval + 1h), the interval coming from the freshness endpoint
(9h when an older deploy does not report it). A cycle is recorded when its
scrape stage ends, so with cycles every 4h the newest record is normally up
to ~4h old; a fixed 3h limit paged twice per cycle gap.

"No new job for a day" is not judged here: the bot's hourly data-quality
check (data:no_new_jobs) knows whether anyone is searching, and while the
bot is dead this watchdog's scrape_stale already covers it. Judging it in
both places paged the owner twice for one problem.

Dedup, reminders and "recovered" messages use the same Alerter as the bot,
with its state in a JSON file the workflow carries between runs (actions/
cache). Exit code 1 while something is failing that the owner has not
been told about in a delivered DM within the cooldown (the DM failed, is
not configured or switched off, or a flap is still inside its window), so
the run shows red and GitHub mails the workflow's owner as the fallback. Once the DM is out the
run is green: otherwise a long outage meant a failure email every 30 min.

    python scripts/site_watchdog.py --state-file state.json
    python scripts/site_watchdog.py --dry-run     # print; post and save nothing

Env: GOSHA_PUBLIC_URL (default https://gosha.bogdantruta.com),
DISCORD_TOKEN (the bot token), ALERT_DM_USER_IDS (comma-separated numeric
user IDs), ALERTS_ENABLED, WATCHDOG_SCRAPE_STALE_HOURS (3; only ever
raises the schedule-derived limit), WATCHDOG_REMIND_HOURS (6).
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
KEY_ENDPOINT = "site:freshness_endpoint"
KEYS = (KEY_DOWN, KEY_SCRAPE, KEY_ENDPOINT)
# Two runs of the */30 schedule: a failure back within an hour of its
# "recovered" waits one run to see whether it is a flap; longer, it pages.
FLAP_WINDOW_SECONDS = 3600

Fetch = Callable[[str], tuple[int, bytes]]

DEFAULT_SCRAPE_STALE_HOURS = 3.0
# An older deploy's freshness endpoint does not report the interval; prod
# runs every 4h, so assume that (2 × 4h + 1h) rather than a limit that
# pages on every gap between cycles.
UNKNOWN_INTERVAL_STALE_HOURS = 9.0


def scrape_stale_limit_hours(configured: float | None, interval_minutes: object) -> float:
    """Hours without a finished cycle before the bot is called dead."""
    floor = UNKNOWN_INTERVAL_STALE_HOURS
    if isinstance(interval_minutes, int) and not isinstance(interval_minutes, bool) \
            and interval_minutes > 0:
        floor = 2 * interval_minutes / 60 + 1
    return max(configured or DEFAULT_SCRAPE_STALE_HOURS, floor)


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
    scrape_stale_hours: float | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Probe the site → (failing, cleared) condition maps for the Alerter.

    `scrape_stale_hours` is the configured limit (WATCHDOG_SCRAPE_STALE_HOURS);
    the one applied is never below 2 × the bot's scrape interval + 1h.
    """
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
    limit = scrape_stale_limit_hours(scrape_stale_hours, data.get("scrape_interval_minutes"))
    if last_cycle is None:
        failing[KEY_SCRAPE] = "No scrape cycle has ever been recorded."
    else:
        age = (now - last_cycle).total_seconds()
        if age > limit * 3600:
            failing[KEY_SCRAPE] = (
                f"**No scrape cycle has finished for {_hours(age)}** (last: "
                f"{last_cycle:%Y-%m-%d %H:%M} UTC, limit {limit:g}h). "
                f"The bot is crashed, crash-looping, hung or stuck in a cycle: "
                f"`docker compose -f docker-compose.prod.yml ps` / `logs bot`."
            )
        else:
            jobs = data.get("last_scrape_cycle_jobs")
            cleared[KEY_SCRAPE] = (
                f"Scrape cycles are running again (last {_hours(age)} ago"
                + (f", {jobs} jobs" if isinstance(jobs, int) else "") + ")."
            )

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
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print what would be said; post nothing, leave the state file alone",
    )
    args = parser.parse_args(argv)

    failing, cleared = evaluate(
        args.url, fetch,
        scrape_stale_hours=_env_float("WATCHDOG_SCRAPE_STALE_HOURS", 0.0) or None,
    )
    alerter = Alerter(
        cooldown_seconds=_env_float("WATCHDOG_REMIND_HOURS", 6.0) * 3600,
        state_path=args.state_file,
        flap_window_seconds=FLAP_WINDOW_SECONDS,
    )
    if args.dry_run:
        alerter.state_path = None  # read the state, never write it
    # Keys this watchdog no longer judges (site:no_new_jobs) would stay
    # "active" in the carried state forever; drop them without a word.
    alerter.forget([k for k in alerter.active if k not in KEYS])
    snapshot = alerter.snapshot()
    dispatch = alerter.decide(failing, cleared)

    for text in failing.values():
        print(f"FAILING: {text}")
    if not failing:
        print("OK: the site answers and scraping is fresh.")

    delivered = False
    if dispatch:
        message = format_dispatch("site watchdog", dispatch)
        if args.dry_run:
            print(f"--- would post ---\n{message}")
        elif not alerts_enabled():
            print("Alerts are switched off (ALERTS_ENABLED); nobody was told.")
        elif not (os.getenv("DISCORD_TOKEN", "").strip()
                  and os.getenv("ALERT_DM_USER_IDS", "").strip()):
            print(
                "::warning::DISCORD_TOKEN / ALERT_DM_USER_IDS not set; nobody was told.",
                file=sys.stderr,
            )
        elif post_dm_sync(message):
            delivered = True
        else:
            print("::error::Sending the Discord DM failed.", file=sys.stderr)
        if not delivered:
            # Nobody heard it: undo this run's decisions so the next run
            # says it again instead of treating it as already said.
            alerter.rollback(snapshot, dispatch.keys)
    # Red while anything failing has not reached the owner in a delivered
    # DM within the cooldown: a flap still inside its window, a failed or
    # unconfigured DM. GitHub's failure email is then the fallback.
    told = all(alerter.is_told(key) for key in failing)
    return 1 if failing and not told else 0

if __name__ == "__main__":
    raise SystemExit(main())
