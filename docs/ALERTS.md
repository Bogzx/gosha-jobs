# Scraper-failure alerts

GOSHA tells its owner when job scraping breaks, **by Discord DM only**.
No channel posts and no @mentions; admins are not paged unless their ID is
listed. One switch turns it all off.

## What gets reported

| Failure | Detected by | When the owner hears |
|---|---|---|
| Every source returns 0 jobs | `gosha/scrape_health.py` (per cycle) | the first empty cycle |
| One board returns 0 jobs (adapter broken) | same | 3 empty cycles running |
| A board's yield collapses (e.g. 300 → 3, pagination broke) | same, vs. the median of its last 8 normal cycles | 2 cycles below 25% of that baseline (baselines under 20 jobs are not judged) |
| Recent postings mostly have no description, although that board normally has one | `check_data_quality()` (hourly) | ≥80% empty over the last 24h with ≥10 postings, vs. ≤50% in the week before |
| Active jobs older than 3h have no embedding (feed + semantic matching blind) | same | ≥10 such jobs |
| No new job stored for 24h while searches exist | same | at the next hourly check |
| `scrape_cycle`, `deadlink_check`, `cv_retention` or `data_quality_check` raises, is skipped or misses its run | APScheduler listener in `gosha/main.py` | at once |
| The bot's event loop is wedged | heartbeat watchdog (`gosha/heartbeat.py`) | just before it restarts the bot (DM over Discord's REST API, no event loop needed) |
| The bot is crashed or crash-looping, the server or DB is down | `scripts/site_watchdog.py` on GitHub Actions, every 30 min | site unreachable, or no scrape cycle for 3h, or no new job for 24h |
| The daily live smoke of every board fails | `.github/workflows/scraper-smoke.yml` | when the set of failing sources changes, and on recovery |

A condition that stays broken is repeated once per `ALERT_COOLDOWN_MINUTES`
(6h by default), not every cycle, and a **recovered** DM follows when it
heals. Alert state and yield baselines are kept in `data/alerts_state.json`
and `data/scrape_health.json`, so a deploy during an outage neither
re-pages you nor forgets the baselines. Delete those files to re-baseline.

## Setup

### 1. Your numeric Discord user ID

Discord can only DM a numeric ID, not a username like `@Bogdanila`.

1. Discord → User Settings → **Advanced** → turn on **Developer Mode**.
2. Right-click your own name (in a member list or a message) → **Copy User ID**.
   You get a number like `123456789012345678`.

The bot can only DM you if you share a server with it and that server
allows DMs from members (Server → Privacy Settings → Direct Messages).

### 2. The bot (in-process alerts)

In the server's `.env`:

```
ALERT_DM_USER_IDS=123456789012345678   # your ID; comma-separate several
# ALERTS_ENABLED=true                  # false turns every alert off
# ALERT_COOLDOWN_MINUTES=360
```

Restart the bot. Its log says `Ops alerts will be DMed to [...]` at
startup, or why they are off.

### 3. GitHub Actions (out-of-process: bot down, server down, smoke test)

Repository → Settings → Secrets and variables → Actions:

- Secret `DISCORD_BOT_TOKEN` — the same bot token as `DISCORD_TOKEN`. The
  workflows DM you as the bot through Discord's REST API.
- Secret `ALERT_DM_USER_IDS` — your numeric ID from step 1.
- Optional variables: `ALERTS_ENABLED` (`false` = off), `GOSHA_PUBLIC_URL`
  (default `https://gosha.bogdantruta.com`), `WATCHDOG_SCRAPE_STALE_HOURS`
  (3), `WATCHDOG_NEW_JOB_STALE_HOURS` (24), `WATCHDOG_REMIND_HOURS` (6).

Without both secrets the site watchdog skips itself. Run it once by hand
(Actions → Site watchdog → Run workflow) to check. It reads the public
`GET /api/v1/health/freshness` (timestamps of the last scrape cycle and
newest job, nothing else, cached 60s), so deploy the current code first.

GitHub disables scheduled workflows in a repository with no activity for
60 days; a failing run also emails the workflow's owner.

## Turning it off

`ALERTS_ENABLED=false` in `.env` (bot + heartbeat watchdog) and as a
repository variable (workflows), or leave `ALERT_DM_USER_IDS` empty.
