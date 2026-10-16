# Handoff: gosha-health — scrape-failure alerting (DM-only to the owner)

Written: 2026-10-16 ~12:06 UTC · Branch: wip/agents-2026-10-16 · Base commit: 5490d6a

## Mission (one paragraph, self-contained)
User request: "put one agent to check if the jobs are successfully scraped or not, and if they are not send an alert to me @Bogdanila on discord". Later requirement change from the user: **alerts go ONLY as a Discord DM to the owner** (ALERT_DM_USER_IDS, numeric ID). No channel posts, no @mentions, admins are not paged implicitly, and one off switch (`ALERTS_ENABLED=false` or empty `ALERT_DM_USER_IDS`). Detection must cover all-zero cycles, one dead board, yield collapse, empty descriptions, embedding failure, a hung/crashed bot or server down (out of process), and a failing daily smoke workflow, with dedup/cooldown and "recovered" messages.

## File ownership
Mine: gosha/alerts.py (new), gosha/scrape_health.py, gosha/heartbeat.py, scripts/healthcheck.py (unchanged), gosha/main.py, gosha/config.py, .env.example, .github/workflows/scraper-smoke.yml, .github/workflows/site-watchdog.yml (new), scripts/site_watchdog.py (new), gosha/services/freshness.py (new), gosha/api/freshness.py (new), docs/ALERTS.md (new), and their tests: tests/test_alerts.py, tests/test_alert_scenarios.py, tests/test_site_watchdog.py, tests/api/test_freshness_api.py (all new), tests/test_scrape_health.py, tests/test_healthcheck_script.py (appended 2 tests).
Minimal edits outside ownership: gosha/pipeline.py `_post_health_alert` body (~line 1018-1030) now delegates to `gosha.alerts.publish_scrape_report` (gosha-pipe was told). gosha/api/app.py: added `"freshness",` to the router tuple in `_mount_routers` (~line 163). README.md: one paragraph before "Backups are built and tested…" pointing to docs/ALERTS.md.
Do NOT touch: gosha/scrapers/*, matching.py, recommend.py, filters.py (other agents); the rest of pipeline.py is gosha-pipe's.

## Done (verified)
- **gosha/alerts.py**: `Alerter` holds keyed conditions. `decide(failing, cleared)` raises new keys, repeats after the cooldown, sends "recovered" only for keys that were active, and leaves unknown keys alone. State can be persisted as JSON. `DiscordAlertSink` sends DMs only (get_user→fetch_user; one closed DM doesn't stop the others; never raises). `sink_for_bot(bot)` returns None when alerts are off or there are no IDs. `publish_scrape_report` (pipeline hook) also emits a `scrape.cycle` event `{total, sources}`. `scheduler_event_conditions` maps APScheduler events to the key `scheduler:<job>`, and EVENT_JOB_EXECUTED clears it. `post_dm_sync` DMs over Discord's REST API with urllib (POST /users/@me/channels, then /channels/{id}/messages, `Authorization: Bot`), honours ALERTS_ENABLED and is stdlib-only. Tests: tests/test_alerts.py.
- **gosha/scrape_health.py**: CycleReport now carries `conditions`/`cleared` (keys `scrape:zero_yield`, `scrape:dead:<src>`, `scrape:collapse:<src>`). Zero yield suppresses the per-source dead lines. New yield-collapse rule: count < 0.25 × median of the last 8 normal cycles, baseline ≥ 20, for 2 cycles; after 24 collapsed cycles the new level is accepted as normal. State can be persisted (`attach_state_file`). New `check_data_quality()` (DB, hourly): `data:descriptions:<src>` fires when ≥80% of postings in the last 24h are empty (<40 chars, ≥10 postings) while the 1–8 day baseline was ≤50% empty. `data:embeddings` fires when ≥10 active jobs older than 3h lack a current-model embedding. `data:no_new_jobs` fires when there's been no new job for 24h while active subscriptions exist. Old tests are kept and pass; tests/test_alert_scenarios.py covers cases (a)–(e) end to end.
- **gosha/config.py:59-61, 244-280, 311-313**: `alerts_enabled` (ALERTS_ENABLED, default true), `alert_dm_user_ids` (ALERT_DM_USER_IDS; accepts `<@id>`, skips usernames with a warning), `alert_cooldown_minutes` (ALERT_COOLDOWN_MINUTES, default 360). `alert_dm_admins` was removed, so admins are not paged.
- **gosha/main.py:53-80**: attaches state files `data/alerts_state.json` and `data/scrape_health.json` (in the data/ volume), sets the cooldown, and logs once whether alerts are on or off. Lines ~120-185: the scheduler listener goes through the alerter (now also EXECUTED, which produces the recovery). New hourly job `data_quality_check`, first run 30 min after boot.
- **gosha/heartbeat.py:96 `_page_owner`**: the watchdog DMs the owner via `post_dm_sync` before `os._exit` (5s timeout, never raises). The bot container already has DISCORD_TOKEN.
- **Freshness endpoint**: `GET /api/v1/health/freshness`, unauthenticated, cached 60s. Returns only `now, last_scrape_cycle_at, last_scrape_cycle_jobs, last_new_job_at, last_job_seen_at`. Files: gosha/services/freshness.py, gosha/api/freshness.py. Tests: tests/api/test_freshness_api.py.
- **scripts/site_watchdog.py + .github/workflows/site-watchdog.yml** (cron */30): probes /health and /health/freshness. Keys `site:down`, `site:scrape_stale` (>3h), `site:no_new_jobs` (>24h), `site:freshness_endpoint`. Reuses Alerter with state in actions/cache (pinned SHA 55cc834… restore/save). DMs via post_dm_sync, with secrets `DISCORD_BOT_TOKEN` (→ env DISCORD_TOKEN) and `ALERT_DM_USER_IDS`. The job skips itself without them. It exits 1 while failing, and if the DM fails the state is restored so the next run retries. Tests: tests/test_site_watchdog.py.
- **.github/workflows/scraper-smoke.yml**: the github-script step (`id: track`) has `page()`, which DMs via the REST API. It is called on issue create, on a comment for a changed failure signature, and on close/recovery (3 calls). A fallback step "Page if the issue tracking itself failed" uses post_dm_sync. The JS was checked with `node --check` and the YAML parses. A structural test is in tests/test_site_watchdog.py.
- **.env.example**: alert block after ADMIN_DISCORD_IDS (DM-only, how to copy the numeric ID). The `SCRAPE_BROWSER_FALLBACK` lines requested by the sourcing agent are added after SCRAPE_DIRECT.
- **docs/ALERTS.md**: failure → detector → when table, owner setup steps (numeric ID via Developer Mode → Copy User ID, .env, GitHub secrets), off switch.
- Last full suite run (after all edits above, before this handoff): **874 passed, 9 skipped, 0 failed** (`.venv/bin/python -m pytest -q`). ruff check on all my files: clean.

## In progress (exact state)
- Nothing half-applied. The original task's report file (/tmp/claude-1000/-data-Repos/2e9dc50a-…/scratchpad/report-health.md, machine-specific) was NOT written; this handoff replaces it.

## Remaining TODO (prioritized)
1. Closed-DM notice (gosha/pipeline.py ~861-872, gosha-pipe's code, now an owner DM through sink_for_bot) is **not deduplicated**: it DMs every cycle while any user has closed DMs. Next step: route it through `get_alerter().update(sink_for_bot(bot), {"delivery:closed_dms": text}, {} if closed_dms else {"delivery:closed_dms": ""}, title="Delivery")`, or drop it to a log line. Coordinate with gosha-pipe or the lead.
2. Owner setup on the server: set `ALERT_DM_USER_IDS=<@Bogdanila numeric id>` in .env and restart the bot. In GitHub, add the secrets DISCORD_BOT_TOKEN and ALERT_DM_USER_IDS, deploy (so /api/v1/health/freshness exists), then run "Site watchdog" manually once.
3. Optional: a node-executed behaviour test of the smoke `page()` JS with mocked github/core/fetch (currently only syntax and structure are checked).

## Open review points from the orchestrator
- Make alerting reliable and reach @Bogdanila: addressed (DM-only per the later change).
- Audit cases (a)–(g) with tests: (a)(b)(c)(d)(e) are in tests/test_alert_scenarios.py, (f) in tests/test_site_watchdog.py and test_healthcheck_script.py (watchdog DM), (g) in tests/test_site_watchdog.py (workflow wiring). Addressed.
- Dedup/cooldown + recovered message: addressed (Alerter, persisted).
- Yield collapse + empty-description rate: addressed.
- Out-of-process dead-man switch, preferring a DM via bot token over a webhook: addressed (no webhook code is left).
- Minimal public freshness endpoint that leaks nothing: addressed.
- Requirement change (DM-only, ALERTS_ENABLED switch, no admin auto-DM, all alert paths): addressed. The closed-DM notice was changed by gosha-pipe at my request (see TODO 1 about dedup).
- Relayed to gosha-pipe and applied by them: count JobSpy boards as attempted only via `jobspy_boards_for` (pipeline.py:254). Without it, remote-only deployments get false dead-source alerts.

## Audit findings (what was broken before)
- (a) All sources 0: posted to the alert channel every cycle (spam), no mention/DM, and nothing at all if ALERT_CHANNEL_ID was unset or not cached.
- (b) One board dead: channel only, after 3 cycles.
- (c) Yield collapse: undetected.
- (d) Empty descriptions: undetected.
- (e) Embedding failure: only a log warning (and `embed_new_jobs` returns 0 at debug level when the model is unavailable). Matching silently fell back to regex, and the feed was blind to the jobs.
- (f) Hung bot: the watchdog restarted it silently. Crashed bot or server down: nobody was told, and `/api/v1/health` only says the API is up.
- (g) Smoke failure: only a GitHub issue, so nobody was told outside the repo.

## How to verify
- `.venv/bin/python -m pytest -q tests/test_alerts.py tests/test_alert_scenarios.py tests/test_scrape_health.py tests/test_healthcheck_script.py tests/test_site_watchdog.py tests/api/test_freshness_api.py` → 75 passed.
- `.venv/bin/python -m pytest -q` → 874 passed, 9 skipped at the time of writing (other agents may have changed things since).
- `.venv/bin/ruff check gosha/alerts.py gosha/scrape_health.py scripts/site_watchdog.py` → clean.
- Dry run against prod (read-only GETs): `python3 scripts/site_watchdog.py --dry-run --state-file /tmp/x.json` (sends nothing).

## Environment notes
- venv: /data/Repos/gosha-jobs/.venv (machine-specific path). No production contact was made and no real Discord messages were sent; everything was tested with mocks.
- Runtime state files created by the bot in prod: data/alerts_state.json and data/scrape_health.json. Delete them to re-baseline.
- New env/secrets: ALERT_DM_USER_IDS, ALERTS_ENABLED, ALERT_COOLDOWN_MINUTES (bot .env). GitHub secrets: DISCORD_BOT_TOKEN, ALERT_DM_USER_IDS. Optional GitHub vars: ALERTS_ENABLED, GOSHA_PUBLIC_URL, WATCHDOG_*_HOURS.
- herdr agent names used: gosha-pipe.

## Lead review on the server (2026-10-16) — corrections to apply, in this order
Verified: no channel/@mention ops path remains; all paths are owner DMs. 69 health tests pass.
`page()` JS was executed in node with mocks (12 scenarios): REST calls, Bot header, multi-ID, error
handling and no secret leakage are correct. Fix these (each with a test):
1. **One off-switch rule everywhere.** Today `config.env_flag` (only 1/true/yes/on = on),
   `alerts.alerts_enabled()` (alerts.py:206, only 0/false/no/off = off), `page()`
   (scraper-smoke.yml:85, `=== 'false'`, no trim) and site-watchdog.yml:46 (only `false`) disagree:
   `ALERTS_ENABLED=disabled` silences the bot but not the heartbeat; `0` still DMs from the smoke
   workflow. Rule: anything not truthy (1/true/yes/on, trimmed, case-insensitive) = off. Mirror it in
   the JS and the shell gate.
2. **`<@id>` parsing everywhere.** `alerts._parse_ids` (alerts.py:351) and the JS regex
   (scraper-smoke.yml:84) drop `<@123>` that config accepts → heartbeat silently sends nothing.
3. **Flapping.** Scheduler keys clear on every EVENT_JOB_EXECUTED (MAX_INSTANCES → alert → recovered,
   forever; cooldown doesn't apply). Single transient zero-yield cycle also alerts+recovers. Add
   hysteresis: suppress a re-raise within the cooldown of a recovery, or require N clean runs to clear.
4. **Closed-DM notice dedup** (pipeline.py:861-870, gosha-pipe's code): agree the hook with gosha-pipe —
   route through `get_alerter().update(..., {"delivery:closed_dms": text})`. You provide the helper;
   gosha-pipe calls it.
5. **Stale report re-published** when there are no subscriptions: run_scrape_stage returns early
   (pipeline.py:234) and pipeline.py:956 re-publishes `health.last_report`. Ask gosha-pipe to clear it /
   return the report; you own `_post_health_alert`.
6. **Duplicate "no new jobs" alert**: `data:no_new_jobs` (in-process) and `site:no_new_jobs` (watchdog)
   both fire. Keep the watchdog's only for "scrape stale"; drop its no-new-jobs, or the in-process one.
7. **False positives**: collapse baseline should be normalised by searches attempted (or reset when the
   set of subscription searches changes); `data:no_new_jobs` → 48h; suppress `data:embeddings` while a
   model-change backfill is progressing.
8. **Retry on failed DM**: `sink.send` returns delivered/not; roll back `last_sent` if not (as
   site_watchdog.py:217 already does).
9. **Heartbeat `_page_owner` rate limit** via a timestamp file in data/ (≤1 per hour).
10. **Test safety (do first)**: tests/test_healthcheck_script.py:54 runs a subprocess with full os.environ
    and an unmocked `_page_owner` → a real DM if run where DISCORD_TOKEN/ALERT_DM_USER_IDS are exported.
    Set `ALERTS_ENABLED=false` in `_run_watchdog`'s env.
11. Site watchdog: exit 0 once the DM is delivered (GitHub emails a failure every 30 min otherwise);
    `--dry-run` must not write the state file.
12. Low: freshness `_compute` failure is not cached (cache the error for 60s too); prune state keys for
    sources no longer attempted; drop stale "ops channel" wording (pipeline.py:1021 docstring is pipe's
    file — ask; scrape_health.py:34, CycleReport.summary); comment out `ALERT_CHANNEL_ID` in .env.example.
