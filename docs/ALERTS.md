# Scraper-failure alerts

GOSHA tells its owner when job scraping breaks, **by Discord DM only**.
No channel posts and no @mentions; admins are not paged unless their ID is
listed. One switch turns it all off.

## What gets reported

| Failure | Detected by | When the owner hears |
|---|---|---|
| Every source returns 0 jobs | `gosha/scrape_health.py` (per cycle) | 2 empty cycles running (one is often a blip) |
| One board returns 0 jobs (adapter broken) | same | 3 empty cycles running |
| A board's yield collapses (e.g. 300 → 3, pagination broke) | same, vs. the median of its last 8 normal cycles | 2 cycles below 25% of that baseline (baselines under 20 jobs are not judged; when a search the board ran is removed, a drop in the next 2 cycles is taken as its new normal instead) |
| Recent postings mostly have no description, although that board normally has one | `check_data_quality()` (hourly) | ≥80% empty over the last 24h with ≥10 postings, vs. ≤50% in the week before |
| Active jobs older than 3h have no embedding (feed + semantic matching blind) | same | ≥10 such jobs, unless older jobs got embedded since the last check (a backfill after a model change is progressing) |
| No new job stored for 48h while searches exist | same | at the next hourly check |
| A user's DMs are closed, so their matches cannot be delivered | delivery (`gosha/pipeline.py` → `report_closed_dms`) | once per user, "recovered" when a DM reaches them again |
| `scrape_cycle`, `deadlink_check`, `cv_retention` or `data_quality_check` raises, is skipped or misses its run | APScheduler listener in `gosha/main.py` | at once |
| The bot's event loop is wedged | heartbeat watchdog (`gosha/heartbeat.py`) | just before it restarts the bot (DM over Discord's REST API, no event loop needed); at most once an hour |
| The bot is crashed or crash-looping, the server or DB is down | `scripts/site_watchdog.py` on GitHub Actions, every 30 min | site unreachable, or no scrape cycle finished for 2 × the scrape interval + 1h (9h with prod's 4h schedule; "no new jobs" is the bot's own check above, not repeated here) |
| The daily live smoke of every board fails | `.github/workflows/scraper-smoke.yml` | when the set of failing sources changes, and on recovery |

A condition that stays broken is repeated once per `ALERT_COOLDOWN_MINUTES`
(6h by default), not every cycle, and a **recovered** DM follows when it
heals. A condition that fails again shortly after its "recovered" (within
2h for the bot's checks, 1h for the site watchdog) is held back once to
see whether it is flapping: if it clears again nothing is sent, and if it
is still failing when that window ends you get "failing again … after
recovering" at once. A DM that reaches nobody is not counted as said; it
is retried at the next check. Alert state and yield baselines are kept in `data/alerts_state.json`
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
- Optional variables: `ALERTS_ENABLED` (see below), `GOSHA_PUBLIC_URL`
  (default `https://gosha.bogdantruta.com`), `WATCHDOG_SCRAPE_STALE_HOURS`,
  `WATCHDOG_REMIND_HOURS` (6).

The "no scrape cycle" limit follows the bot's schedule by itself: the bot
records its `SCRAPE_INTERVAL_MINUTES` with every cycle, the freshness
endpoint reports it, and the watchdog allows 2 × that + 1h (a cycle is
recorded ~35–60 min after it starts, so with a 4h schedule the newest one
is normally up to ~4h old). If the endpoint does not report it (an older
deploy) the limit is 9h. `WATCHDOG_SCRAPE_STALE_HOURS` can only raise the
limit, never lower it below that.

Without both secrets the site watchdog skips itself. Run it once by hand
(Actions → Site watchdog → Run workflow) to check. It reads the public
`GET /api/v1/health/freshness` (timestamps of the last scrape cycle and
newest job, the scrape interval, nothing else, cached 60s), so deploy the current code first.

GitHub disables scheduled workflows in a repository with no activity for
60 days. A site-watchdog run is red while something is failing that you
have not been told about in a delivered DM within the reminder interval
(the DM failed, the secrets are missing, alerts are off, or a re-failure
is still in its flap window); GitHub then emails the workflow's owner as
the fallback. Once you have the DM the run is green, so a long outage is
not an email every 30 minutes.

## Turning it off

`ALERTS_ENABLED=false` in `.env` (bot + heartbeat watchdog) and as a
repository variable (workflows), or leave `ALERT_DM_USER_IDS` empty.

Every path reads the switch the same way: unset or blank = on; `1`,
`true`, `yes`, `on` (any case, surrounding spaces ignored) = on;
**anything else** (`false`, `0`, `off`, `disabled`, a typo) = off. IDs may
be written as `123…`, `<@123…>` or `<@!123…>` everywhere; anything else
(a username, `!123…`) is skipped.

## Weekly Apply-click report (owner) and empty-search nudge (users)

- **Weekly report** (`gosha/clicks.py`, on by default,
  `WEEKLY_CLICK_REPORT_ENABLED=false` turns it off). Every Monday after
  08:00 UTC the owner gets one DM: job DMs sent, Apply clicks and distinct
  jobs clicked per search over the last 7 days. Searches are shown by ID and
  keywords; no user names. Same recipients and off switch as the alerts
  (`ALERT_DM_USER_IDS`, `ALERTS_ENABLED`). Checked hourly with a stamp in
  `data/click_report.json`, so deploys neither skip nor repeat a week.
- **Apply clicks.** DM Apply buttons open `{PUBLIC_BASE_URL}/api/v1/r/<token>`.
  The token is the delivery's ID signed with `SESSION_SECRET`, so it cannot
  be guessed, and the destination is looked up on the server, so nothing in
  the link chooses where the browser goes (no open redirect). The redirect
  stores user, job, search and time, with no IP or user agent, at most once
  per delivery per minute (link previews and double clicks), then answers
  302. Rows are removed with the account.
- **Empty-search nudge** (`gosha/nudges.py`, off until
  `EMPTY_SEARCH_NUDGE_ENABLED=true`). A search that has delivered no job DM
  for 30 days gets one short DM to its owner: what the search is, why it may
  be empty, two changes picked from its settings (e.g. add the junior level,
  add Romania/remote) and how to edit it (`/edit id:<n>` or the website).
  At most once per 30 days per search, 09:00–20:00 Romanian time. The 30
  days count from the latest of the search's creation, its last edit or
  pause/resume, its last job DM and its last nudge — and, for searches that
  existed before this feature, from the first run after the deploy.
