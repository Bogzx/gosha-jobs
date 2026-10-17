# Handoff: gosha-health — scrape-failure alerting (DM-only to the owner)

Updated: 2026-10-17 ~12:30 UTC · Branch: wip/agents-2026-10-16 · Earlier work committed and released (main = d4158c7). **Latest assignment (alert review + PG CI job) is uncommitted in the working tree: see "Assignment 2026-10-17" below.** Status: all assignments done and commit-ready ((1b) alerts, #3 DM level); standing by.

## Current state (read this first)
- **Live in production**: owner-DM-only ops alerts (bot, heartbeat watchdog, site watchdog, scraper smoke); site-watchdog stale limit follows the 4h schedule; DM Apply-click tracking (`/api/v1/r/<token>`); weekly owner click report (Monday 08:00 UTC, default ON); monthly empty-search nudge (default OFF); `apply_clicks` + `search_nudges` in the GDPR export and account deletion; privacy notice covers both; scraper smoke treats 403/429-on-every-probe as "blocked from CI (advisory)" and runs with SCRAPE_BROWSER_FALLBACK=true. Working tree clean.
- **Owner setup (not an agent's job; touches prod/GitHub). The lead did not say whether these were done at the release; ask before assuming:**
  1. Prod `.env`: `ALERT_DM_USER_IDS=<@Bogdanila's numeric ID>` (`123`, `<@123>`, `<@!123>` all work) and a bot restart. Startup log: `Ops alerts will be DMed to [...]`. Without it, neither ops alerts nor the weekly click report go out.
  2. GitHub Actions secrets `DISCORD_BOT_TOKEN` (= `DISCORD_TOKEN`) and `ALERT_DM_USER_IDS`; then run "Site watchdog" once by hand (should be green).
  3. Apply-click tracking needs `PUBLIC_BASE_URL` and `SESSION_SECRET` in the bot's env. Otherwise the button links straight to the posting.
- **State files**: `data/alerts_state.json` is compatible, keep it. `data/scrape_health.json` should have been deleted once at this deploy, because it has no recorded searches and the regrouped searches could cause a false "yield collapsed" after 2 cycles. If it was not deleted and such a page arrives in the first ~8h, that is the likely cause; it then self-corrects as baselines re-learn.
- **What to watch after release (read-only; ask the lead first)**: the next daily smoke at 06:17 UTC should be green or show eJobs/Glassdoor as `[BLKD]`, with no issue and no DM. The first weekly click report is due Monday 2026-10-19 08:00 UTC.
- **Notes for others**: if gosha-pipe's `baseline_combos(cycle)` ever rotates its searches, those must stay out of `searches_by_source`, or collapse detection goes blind. DM clicks deliberately create no Application row (lead decision).
- No production contact, no real Discord messages, no commits by me.
- **Git is NOT frozen any more** (lead, 2026-10-17 ~12:30 UTC): commits resumed; the lead commits, not me. Never add `Co-Authored-By: Claude`, `Claude-Session:` or 'Generated with Claude Code' lines.

## Assignment 2026-10-17 #3: DM shows the level (owner decision) — DONE, COMMIT-READY (with pipe's part)
- Owner decision (2026-10-17, via lead): every DM shows "Level: Internship" or "Level: Junior" so internship and
  junior are clearly different; omitted when unclear / not an entry-level search.
- Agreed with gosha-pipe (pane w5:p2): `filters.matched_level(title, levels, board_level, description) ->
  "intern" | "junior" | None`. Same branches as matches_experience_level: "intern" when the search wants intern and
  the posting is a training position (so an intern+junior search gets "intern" for an internship), else "junior"
  when the search wants junior and `_is_junior` holds, else None. **gosha-pipe adds matched_level AND the
  `levels=sub.experience_levels` kwarg on deliver_pending's build_job_embed_with_buttons call (pipeline.py, theirs)
  and pings me when it lands.** gosha/delivery.py is unused by the pipeline: untouched.
- Done by me (uncommitted): `gosha/views.py` (pipe's file; the lead assigned me this function, pipe agreed):
  `build_job_embed_with_buttons(..., levels=None)`; `_matched_level(job, levels)` (lazy import of
  filters.matched_level, None without levels); `_LEVEL_NAMES = {"intern": "Internship", "junior": "Junior"}`;
  the "Level" field is the FIRST inline field, omitted on None. New `tests/test_dm_level.py`: the embed alone
  (intern-only → Internship, junior-only → Junior, intern+junior → the posting's level; no field for
  None/[]/["any"]) and at send time through the real `pipeline.deliver_pending` (fake `_dm_user` capturing embeds;
  same 3 cases). Real matched_level, no stubs in the committed tests.
- gosha-pipe landed `filters.matched_level` (end of gosha/filters.py; rule: one level each, a training position
  open to students = intern, a graduate hire = junior) and `levels=sub.experience_levels` on deliver_pending's call
  (pipeline.py:1323). My views.py change + tests/test_dm_level.py commit together with that.
- **Verified** 2026-10-17 (offline): `.venv/bin/python -m pytest -q tests/test_dm_level.py tests/test_dm_embed.py
  tests/test_match_cv.py tests/test_deliver.py tests/test_outbox.py` → 72 passed (test_dm_level.py 10/10, real
  matched_level, no stubs); ruff clean on views.py and test_dm_level.py. Not run: full suite, PG, a live DM.
- Commit set (mine): gosha/views.py, tests/test_dm_level.py. Suggested message: "DMs show the job's level:
  Internship or Junior".

## Assignment 2026-10-17 (new lead): alert review + Postgres CI job
### (1) Ops alerts since 2026-10-16 17:00 UTC (read-only, checked 12:15 UTC 2026-10-17)
Sources: `docker exec gosha-bot cat data/alerts_state.json` / `scrape_health.json` / `click_report.json`,
`docker logs gosha-bot` (only from 11:30 today: the deploy recreated the container, older logs are gone),
read-only SELECTs on `jobs` and `events`, `gh run list/view` for the two workflows.
- **`data:descriptions:bestjobs`: fired, DMed, correct.** Raised 2026-10-16 17:41:25 UTC ("90% of the 40 postings
  ... no description"), `announced=true`, `last_sent` 2026-10-17 06:35:24 → delivered (state rolls back on a failed
  DM). With the 6h cooldown that is likely 3 DMs (raise 17:41, reminders ~00:35 and 06:35; exact reminder times
  are not logged). Real: all 40 BestJobs postings of the last 24h were first seen 2026-10-16 17:xx; 36/40 were
  empty then. Now 18 have descriptions (re-seen through 11:50 today) and **22 are still empty and active, last
  seen 18:25–18:26 on the 16th** (no longer in search results, so no cycle refills them; matching sees titles
  only → for gosha-scrape/gosha-pipe, maybe d2b3ab3's stored-row refresh). Baseline (1–8 days before): 0/39 empty.
- **It is now stuck, silently**: 22/40 = 55% is between the clear rate (<50%) and the alert rate (≥80%), so no
  reminder and no recovery. Around 17:41 today those 40 leave the 24h window; BestJobs adds ~0–3 new postings/day
  outside bursts (Oct: 1,1,33,3,1,41), so `recent < 10` = "can't tell" and the key stays active indefinitely with
  no "recovered" DM until a day with ≥10 new BestJobs postings at <50% empty. Not noisy, but the owner never hears
  it ended. (Superseded by (1b) below.) Earlier idea: clear `data:descriptions:<src>` when the window has too
  few postings to judge AND none of them is empty, or judge descriptions on the last N postings instead of 24h.
- **Weekly click report: sent once on Friday 2026-10-16 17:36:25 UTC** (`click_report.json`), not Monday. Cause:
  `clicks.report_due` is true when there is no stamp yet, so the first boot sends the overdue Mon-10-12 slot at
  once. One-off, by design of the catch-up; next due Mon 2026-10-19 08:00 UTC.
- **No other in-process alert left a trace**: `alerts_state.json` has no other active key and `recent` is empty
  (it only keeps 2h). Scrape cycles (8 `scrape.cycle` events 17:33 → 11:51) all had 8 sources, totals 2431–2634;
  `scrape_health.json` streaks/collapse streaks all 0 (hipo dipped once to 285 vs ~460, above the 25% line).
  No `data/watchdog_paged_at` → the heartbeat watchdog never paged. Since 11:30: no scheduler exceptions, no
  alert-delivery errors. Startup log confirms `Ops alerts will be DMed to [<owner id>]`. LinkedIn gave 115× HTTP
  429 in the 11:33 cycle (yield 268 vs ~315 median: no alert, correctly).
- **Site watchdog is OFF**: all 5 scheduled runs since 2026-10-16 19:59 UTC printed "Site watchdog off: needs
  secrets DISCORD_BOT_TOKEN + ALERT_DM_USER_IDS". GitHub also ran the */30 cron only 5 times in ~14h. So the
  out-of-process "bot/server down" alert does not exist yet → owner setup step 2.
- **Scraper smoke**: 2026-10-17 run (scheduled 06:17, ran 10:44 UTC) green, every source `[OK]`; it closed issue
  #11 (open since 10-05) and tried to DM "recovered", but "DISCORD_BOT_TOKEN / ALERT_DM_USER_IDS not set; Discord
  was not told" (same missing secrets).

### (1b) Stuck-alert root fix (lead-approved ~12:45 UTC; working tree, uncommitted, COMMIT-READY)
- **Lead ruling**: keep scrape_health's first_seen window (my data beat the last_seen metric); 48h rule approved.
  **Correction (from gosha-scrape)**: the 22 BestJobs rows are EXPIRED postings: `bestjobs._apply_details` drops
  expired pages, so neither upsert nor refresh_known_rows (on main since d2b3ab3) sees them. The data fix is the
  expired-deactivation in the tree (pipe's `pipeline._deactivate_expired` + scrape adapter). The "filter drops
  them once their page says Middle" explanation below is superseded.
- **Done: `check_data_quality` ignores deactivated rows** (`gosha/scrape_health.py`, the descriptions query's
  `.where(..., Job.is_active.is_(True))`, baseline included). Before, it counted them, so deactivating the expired
  rows would not have cleared the alert. Test: `test_d_bestjobs_incident_recovers_once_expired_rows_are_deactivated`
  (10-16 shape; at 12:00 next day (40, 22) → no verdict; 22 deactivated → 18 active, 0 empty → cleared →
  "recovered" DM). Fails on HEAD's scrape_health.py (verified by swapping it in), passes now.
- **Done: every open alert can end.** `gosha/alerts.py`: `_Active.judged` (when a detector last called it failing;
  persisted, None in old state files → the clock starts at the first decide() after load);
  `Alerter(unresolved_seconds=DEFAULT_UNRESOLVED_SECONDS=48h)`; decide() closes any open key not judged failing
  for 48h: announced → `Dispatch.closed` line "<text> _(no verdict for 2d0h: closed; it pages again if it comes
  back)_" under "❔ GOSHA — closed without a verdict"; silent flap → closed silently. Not put in `recent`, so a
  return pages afresh. Rolled back like the rest if the DM fails. 48h = 2 runs of the slowest detector
  (deadlink_check / cv_retention, every 24h). docs/ALERTS.md paragraph. For prod's open BestJobs key: the clock
  starts at the first check after the deploy; it closes 48h later unless it fails or clears first.
- **NOT done: the lead's metric change (judge by last_seen_at).** Data (prod, read-only, 12:28 UTC): last_seen_at is
  when a search last KEPT a posting (pipeline.upsert_jobs sets it only for rows that pass the scrape filter), and
  most new postings are kept only in their first cycle: of 41 BestJobs postings first seen in 48h, 38 were last kept
  within 1h of first sight and 3 in the latest cycle (LinkedIn 184/273 first cycle only, 50 latest). So:
  (a) "seen in the latest cycle" leaves 2 BestJobs, 48 LinkedIn new postings now → mostly below the 10-posting
  minimum → detector blind; (b) "last_seen within 24h" rates old postings too: today 94 BestJobs rows, 54 of them
  older with text → the 10-16 incident would have read ~36/94 (38%), below 80%, no alert (estimate: last_seen
  values of that moment are overwritten). The 22 rows are not unlisted: per gosha-pipe's WIP refresh_known_rows
  docstring (pipeline.py:525) the filter drops them once their page says Middle, so upsert never refreshes them.
  That refresh fixes the data, the rate then clears ("recovered"); the 48h rule covers any limbo. I tried the
  "new AND seen in the board's latest cycle" variant, measured it on prod, and reverted it (the only
  scrape_health.py change is the is_active filter above).
- Tests (all fail on HEAD's alerts.py, verified by swapping it in): tests/test_alerts.py
  `test_an_alert_nothing_can_judge_is_closed_with_a_message`, `test_a_failure_judged_once_a_day_is_never_closed`,
  `test_a_silent_flap_is_closed_silently`, `test_an_old_state_file_starts_the_clock_at_load`,
  `test_an_undelivered_close_is_said_again_next_check`; tests/test_alert_scenarios.py, the exact 10-16 shape
  (40 new rows 36 empty + 54 older listed with text): `test_d_bestjobs_incident_recovers_once_expired_rows_are_deactivated`
  (pages 90% of 40; at 12:00 next day (40, 22) → no verdict, as prod; 22 deactivated → recovered DM) and
  `test_d_bestjobs_incident_without_a_verdict_closes_aloud` (out of the window → no verdict at +25h/+47h → closed
  DM at +48h).
- Verify: `.venv/bin/python -m pytest -q tests/test_alerts.py tests/test_alert_scenarios.py tests/test_scrape_health.py
  tests/test_healthcheck_script.py tests/test_site_watchdog.py tests/test_alert_switch.py tests/api/test_freshness_api.py
  tests/test_click_report.py tests/test_nudges.py tests/test_deliver.py` → 248 passed; ruff clean on alerts.py and the
  2 test files.

### (2) Postgres CI job (working tree, uncommitted)
- `.github/workflows/ci.yml`: new job `backend-postgres` (postgres:16-alpine service, same image as
  docker-compose.prod.yml; same install list as `backend`; `pytest -q -rs`). `GOSHA_TEST_DB_URL` → db
  `gosha_test` (whole suite), `GOSHA_TEST_POSTGRES_URL` → db `postgres` (the PG-only tests that drop/recreate
  tables, kept in their own database). **Not added to deploy's `needs`**, so deploy-on-main is unchanged
  (`needs: [backend, frontend, images]`); making it gate the deploy is a one-word change for the owner/lead.
- `tests/conftest.py`: `TEST_DB_URL = os.getenv("GOSHA_TEST_DB_URL") or sqlite memory`, `ON_POSTGRES`, and the
  SQLite `PRAGMA foreign_keys` listener only on SQLite (replaces the old scratchpad `pg_plugin.py`).
- Skipped on PG with a reason: `tests/api/test_subscriptions_api.py::test_delete_a_search_that_delivered_a_dm`
  (rebuilds user_jobs from sqlite_master; tests/api/test_postgres_api.py:63 is its PG twin) and
  `tests/api/test_auth.py::test_deleted_accounts_cookie_does_not_open_the_next_account` (SQLite id reuse).
- Files outside my usual ownership, edited on the lead's assignment: ci.yml, tests/conftest.py, the 2 test files.
- **Verified 2026-10-17 ~12:25 UTC** (offline, on the working tree incl. other agents' uncommitted edits):
  `/tmp/claude-1001/-home-ubuntu-gosha-agents/1f87794d-46b6-407b-a3fc-2ed02ba1bbee/scratchpad/pg_ci_local.sh`
  (throwaway `gosha-agents-pgtest` postgres:16-alpine on 127.0.0.1:55432, removed after; the CI-identical venv
  from the old lead's scratchpad, run with PYTHONDONTWRITEBYTECODE) → **1758 passed, 6 skipped, 0 failed** in
  4m43s. Skips: the 2 above with their reasons + 4 × "sentence-transformers not installed" (as in CI); the 15
  PG-only tests ran. SQLite default: `.venv/bin/python -m pytest -q tests/api/test_auth.py
  tests/api/test_subscriptions_api.py` → 37 passed; ruff clean on the 3 test files; ci.yml parses, deploy
  `needs` unchanged. Not run: the job on GitHub itself (needs a push).

## Mission (one paragraph, self-contained)
User request: "put one agent to check if the jobs are successfully scraped or not, and if they are not send an alert to me @Bogdanila on discord". Later requirement change from the user: **alerts go ONLY as a Discord DM to the owner** (ALERT_DM_USER_IDS, numeric ID). No channel posts, no @mentions, admins are not paged implicitly, and one off switch (`ALERTS_ENABLED=false` or empty `ALERT_DM_USER_IDS`). Detection must cover all-zero cycles, one dead board, yield collapse, empty descriptions, embedding failure, a hung/crashed bot or server down (out of process), and a failing daily smoke workflow, with dedup/cooldown and "recovered" messages.

## File ownership
Mine: gosha/clicks.py, gosha/nudges.py, gosha/api/redirect.py, tests/api/test_apply_redirect.py, tests/test_click_report.py, tests/test_nudges.py, tests/test_startup_new_tables.py (all new, DM features); tests/test_alert_switch.py (new, server session), gosha/alerts.py (new), gosha/scrape_health.py, gosha/heartbeat.py, scripts/healthcheck.py (unchanged), gosha/main.py, gosha/config.py, .env.example, .github/workflows/scraper-smoke.yml, .github/workflows/site-watchdog.yml (new), scripts/site_watchdog.py (new), gosha/services/freshness.py (new), gosha/api/freshness.py (new), docs/ALERTS.md (new), and their tests: tests/test_alerts.py, tests/test_alert_scenarios.py, tests/test_site_watchdog.py, tests/api/test_freshness_api.py (all new), tests/test_scrape_health.py, tests/test_healthcheck_script.py (appended 2 tests).
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
- Nothing half-applied. All 12 lead-review corrections are done (see the last section); nothing uncommitted is waiting on anyone.

## Remaining TODO (prioritized)
1. Owner setup on the server (owner/lead, not an agent: no prod changes): set `ALERT_DM_USER_IDS=<@Bogdanila numeric id>` in .env and restart the bot. In GitHub, add the secrets DISCORD_BOT_TOKEN and ALERT_DM_USER_IDS, deploy (so /api/v1/health/freshness exists), then run "Site watchdog" manually once. After the deploy, delete nothing: the old data/alerts_state.json loads fine (new fields default).
2. (resolved in the 2nd review: per-board grace on removed searches, pipe passes the per-source mapping.)
3. Done earlier but worth knowing: the node-executed page() test now exists (tests/test_alert_switch.py).

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

## Lead review on the server (2026-10-16) — corrections (ALL APPLIED, see "Applied" below)
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

### Applied (server session, 2026-10-16) — each with tests
Full suite after all of it: **1074 passed, 9 skipped, 0 failed** (includes other agents' new tests). ruff clean on all my files; both workflow YAMLs parse.
- **10 (first)**: tests/test_healthcheck_script.py `_run_watchdog` sets `ALERTS_ENABLED=false`; `_page_owner` unit tests use a tmp `GOSHA_HEARTBEAT_FILE`. Test: `test_watchdog_subprocess_never_pages_for_real`.
- **1**: one rule: blank/unset = on, only 1/true/yes/on (trimmed, any case) = on, anything else = off. `alerts.TRUTHY`/`flag_on()`/`alerts_enabled()`; config.env_flag already matched. Mirrored in the smoke JS `page()` and the site-watchdog shell gate (`case`). tests/test_alert_switch.py **executes** the shell gate (bash) and the whole github-script (node, mocked github/core/fetch/fs) for 15 on/off values.
- **2**: `alerts.parse_user_ids` (renamed from `_parse_ids`) and the JS accept `123`, `<@123>`, `<@!123>`; config `_parse_id_list` also strips `<@!`. Tests in test_alert_switch.py (incl. heartbeat path with `<@123>`).
- **3**: Alerter keeps `recent` (key → (since, last told)) for one cooldown. A re-raise within a cooldown of its "recovered" is tracked silently (`announced=False, flapping=True`); the reminder (a cooldown after the recovered DM) says "failing on and off"; a silent re-raise that clears again sends no 2nd recovered. Bound: ≤1 raise/recovered pair per cooldown per key. Plus zero-yield pages only from the 2nd consecutive empty cycle (`ZERO_CYCLES_ALERT=2`; first cycle is still logged). State file gains `recent`, `announced`, `flapping` (old files load). Tests: test_alerts.py flapping tests, test_alert_scenarios.py `test_a_single_empty_cycle_is_a_blip_not_a_page`.
- **4**: `alerts.report_closed_dms(bot, closed, delivered)` — key per user `delivery:closed_dms:<id>`, recovered only when a DM reaches that user. gosha-pipe now calls it every cycle from deliver_pending (their test: tests/test_deliver.py::test_closed_dms_are_reported_once_not_every_cycle).
- **5**: no pipe change needed: CycleReport has `published`; publish_scrape_report judges a report once, and a re-published (stale) one is an idle cycle that still emits `scrape.cycle {"total":0,"sources":0,"idle":true}` (so the watchdog doesn't call an idle bot dead). Test: `test_a_report_is_judged_once`.
- **6**: site watchdog no longer judges no-new-jobs (KEY_NEW_JOBS, WATCHDOG_NEW_JOB_STALE_HOURS removed from script, workflow, docs); keys not in `KEYS` are dropped silently from carried state. The in-process `data:no_new_jobs` stays (it knows about subscriptions).
- **7**: (a) `record_cycle(..., searches=)`; a changed set of searches re-learns every baseline and clears active collapse alerts. gosha-pipe passes `searches=list(scrape_combos)` (their test: tests/test_pipeline_accuracy.py::test_the_scrape_stage_tells_health_which_searches_ran). Signature persisted. (b) `NO_NEW_JOBS_HOURS=48`. (c) `data:embeddings` not raised while the backlog shrank since the previous hourly check, nor on the first check after start when part of the backlog is stale-model (model change); never-embedded backlog still alerts at once. Module state `_previous_backlog`, reset by `reset_scrape_health()`.
- **8**: `DiscordAlertSink.send` returns delivered; `Alerter.snapshot()/restore()/deliver()`; `update()` and publish_scrape_report roll back the decision when the DM reached nobody (raise and recovered both retried). A sink returning None counts as delivered.
- **9**: heartbeat `_page_owner` ≤1 DM/hour via `data/watchdog_paged_at` (next to the heartbeat file); an undelivered page is not stamped.
- **11**: site_watchdog exits 1 only if failing AND the owner wasn't told (DM failed / not configured / switched off / dry-run); `--dry-run` reads but never writes the state file.
- **12**: freshness caches a `_compute` error for 60s too; sources no longer attempted are pruned from scrape_health state and their alert keys dropped silently (`CycleReport.retired` → `Alerter.forget`); "channel" wording removed (scrape_health docstrings; pipe changed the `_post_health_alert` docstring); `ALERT_CHANNEL_ID` commented out in .env.example (config still parses it); docs/ALERTS.md updated for all of the above.
- Lead rule noted: this server is prod's scraping IP — no requests to job boards or the production site (so no `site_watchdog.py --dry-run` against prod from here) without asking gosha-lead.

### Second lead review (A–F) — applied, each with tests
Full suite after: **1159 passed, 9 skipped, 0 failed**; ruff clean; YAML parses. Supersedes the item-3/7/11/12 notes above where they differ.
- **A (HIGH)**: flap suppression is now a short window, not the cooldown: `Alerter(flap_window_seconds=…)`, default 2h (bot), site watchdog 1h (`FLAP_WINDOW_SECONDS`). A re-raise within the window of its "recovered" is held silently; if still failing once the window has passed it is paged at once ("failing again … after recovering"); if it clears inside the window nothing is sent. `recent` is pruned at the window. Watchdog exit code = 1 while any failing key is not `alerter.is_told(key)` (announced AND last DM within the cooldown). Tests: `test_failure_back_soon_after_recovery_is_paged_once_it_outlasts_the_window`, `test_site_down_again_soon_after_recovery_pages_and_stays_red`.
- **B (MED)**: watchdog rolls back the decision in every undelivered branch (dry-run, alerts off, secrets missing, DM failed); file-bytes restore replaced by `alerter.rollback`. Tests: `test_alerts_off_is_not_counted_as_told`, `test_missing_secrets_are_not_counted_as_told`.
- **C (MED)**: no more global baseline reset. `record_cycle(searches=)` takes `{source: searches}` (or one iterable for all). A board that LOST a search gets `SEARCH_CHANGE_GRACE` (=2) cycles in which a below-threshold yield re-baselines instead of alerting (and clears an active collapse); additions and other boards keep their baselines. State: `searches` (12-hex fingerprints), `grace`. gosha-pipe now passes the per-board mapping (`searches_by_source`; their tests in tests/test_pipeline_accuracy.py). Note from pipe: combo keys changed shape, so after the deploy every board's search set changes once — covered by the grace. Limitation: a real breakage within 2 cycles of a removed search on that board is accepted as the new normal (the dead-board alert still catches a drop to 0).
- **D (LOW-MED)**: `Dispatch.keys` lists the keys a message touched; `Alerter.rollback(snapshot, keys)` restores only those (was: whole snapshot). Test: `test_failed_delivery_rolls_back_only_its_own_keys`.
- **E (LOW)**: a source is pruned only after `RETIRE_AFTER_CYCLES` (=24) consecutive cycles not attempted (`absent` in state), so one failed `get_extra_scrapers()` changes nothing. Test: `test_one_cycle_without_a_source_does_not_forget_it`.
- **F (nits)**: shell gate trims only (sed), so `t rue` = off like Python; one ID syntax everywhere: `alerts.USER_ID_PATTERN` `^(?:<@!?(\d+)>|(\d+))$` used by alerts, config (`user_id_from`) and the JS (same regex); `!123`, `<@123`, `123>` rejected everywhere; node test compares JS vs Python per form. Embedding-backfill suppression now measures growth of current-model-embedded jobs in a fixed cohort (first seen before the previous check's cutoff, active or not) — `_previous_cohort`; deactivations no longer look like progress. Test: `test_e_backlog_shrinking_by_deactivation_is_not_progress`.

### Watchdog vs the 4h prod schedule (lead-approved option 2) — applied with tests
Prod starts a cycle every 4h at :26 and each lasts 35–60 min. The `scrape.cycle` event is written when the scrape stage ends, so the newest one is normally up to ~4h old. The old fixed 3h limit would have paged (and then "recovered") on every gap between cycles.
- `publish_scrape_report` puts `interval_minutes` (bot.settings.scrape_interval_minutes) in every `scrape.cycle` payload, idle cycles included.
- Freshness endpoint: new field `scrape_interval_minutes`, taken from the latest event's payload, else the API's own `SCRAPE_INTERVAL_MINUTES` (same .env), else null.
- `site_watchdog.scrape_stale_limit_hours(configured, interval)` = max(WATCHDOG_SCRAPE_STALE_HOURS or 3, 2 × interval + 1h); 9h when the interval is missing or junk. The configured value can only raise the limit.
- Tests: `test_four_hour_schedule_gap_is_not_a_dead_bot` (4h and 8.9h old → no page), `test_four_hour_schedule_pages_after_two_missed_cycles` (9.2h → page, "limit 9h"), `test_unknown_interval_assumes_nine_hours_not_three`, `test_configured_limit_only_ever_raises_the_schedule_floor`, `test_main_reads_the_configured_limit`, `test_cycle_event_carries_the_bots_scrape_interval`, and 2 freshness API tests. Docs and workflow comment updated. Full suite: 1292 passed, 9 skipped.
- In-process keys checked against 4h cycles: `data:no_new_jobs` (48h = 12 cycles) fine. `data:descriptions` (24h = 6 cycles, ≥10 postings) fine. `data:embeddings` (3h grace, hourly `embed_backfill`) fine. `scheduler:scrape_cycle`: the job has max_instances=1 (default), coalesce, misfire grace = interval×30 s (2h). A cycle still running at the next 4h start → EVENT_JOB_MAX_INSTANCES → one page, "recovered" on the EXECUTED that ends it (test `test_four_hour_cycle_overrun_pages_once_then_recovers`). That is abnormal (normal 35–60 min), so paging is right. /scrape_now overlap is ScrapeCycleBusy → logged, returns normally, no page. Trade-off: zero-yield pages after 2 cycles (~8h on prod), dead board after 3 (~12h), collapse after 2 (~8h); the site watchdog does not cover zero-yield, because cycles still finish.

### Note from gosha-pipe's scrape-stage changes (no action needed now)
Combos are keyed (keyword, canonical place, max_age) with merged boards. The SCRAPE_BASELINE hook (default off) adds 7 native-board searches per cycle (boards=(), so no JobSpy board is attempted) and makes zero-subscription cycles real reports instead of idle ones. Turning it on adds searches, so no reset. Turning it off removes searches, so the 2-cycle grace applies. **Caveat**: if `baseline_combos(cycle)` ever rotates searches from cycle to cycle, each cycle removes searches, and the native boards would sit in grace permanently with collapse detection blind. Then rotating searches must stay out of `searches_by_source`, or get a separate record_cycle parameter. gosha-pipe has been told.

### DM features (lead-approved plan) — applied with tests
- **Tables** (gosha/models.py, additive, lead OK): `apply_clicks(id, user_id→users CASCADE, job_id→jobs CASCADE, user_job_id→user_jobs SET NULL, subscription_id→subscriptions SET NULL, clicked_at, click_minute; UNIQUE(user_job_id, click_minute))`, with no IP or UA. `search_nudges(subscription_id PK→subscriptions CASCADE, fingerprint, watched_since, nudged_at, outcome)`. **Prod creates them at startup**: `init_db` (gosha/database.py:28-78) runs `run_migrations` then `Base.metadata.create_all` on every bot and api boot, and create_all creates missing tables. No ALTER is needed (test: tests/test_startup_new_tables.py boots init_db twice on a DB without them). Both are added to scripts/migrate_sqlite_to_postgres.py TABLES and _FK_RULES; the sequence reset now skips tables without an `id` column.
- **Apply clicks** (gosha/clicks.py, gosha/api/redirect.py, router `redirect` added to app.py). `apply_url(uj_id, job_url)` → `{PUBLIC_BASE_URL}/api/v1/r/<URLSafeSerializer(SESSION_SECRET, salt "gosha-apply").dumps(uj_id)>`, falling back to job_url. GET /api/v1/r/{token}: a bad token (forged, other secret, swapped signature, >200 chars), an unknown delivery or a non-http(s) Job.url gives 404. Otherwise it records at most 1 click per delivery per minute (sliding check + unique minute key for races) and answers 302 with no-store, no-referrer, noindex. Fixed a lazy-load-after-rollback bug while testing. Account deletion removes clicks via CASCADE (test). **Not done (needs owner of gosha/services/account.py)**: include apply_clicks in `export_account` (GDPR export).
- **Weekly owner report**: `weekly_click_report_tick` hourly job in main.py (WEEKLY_CLICK_REPORT_ENABLED, default ON). It is due once per Monday-08:00-UTC slot, with the stamp in data/click_report.json written only after delivery. Sent via alerts.sink_for_bot (owner only, ALERTS_ENABLED). Per search: DMs sent / clicks / distinct jobs, top 25, totals and rate, no user names.
- **Empty-search nudge** (gosha/nudges.py): hourly `empty_search_nudge` job; the clocks always run, DMs only if EMPTY_SEARCH_NUDGE_ENABLED (default OFF). 30-day start = max(created_at, watched_since [first sighting = first run after deploy; reset when the fingerprint of criteria + paused/notify changes], last dm_sent_at for the sub, last nudge). Skips paused and website-only searches. 09:00–20:00 Bucharest, ≤20 per run. Closed DMs recorded (no retry until the next window); errors retried next hour. The message names the search, gives a reason, 2 suggestions from its settings (junior/any level, Romania/remote, excluded words, salary min, max age, broader title), `/edit id:<n>`, `/my_searches`, `<site>/searches`, `/pause id:<n>`.
- Both job ids are in main.py `alert_jobs` (a crash pages the owner). Docs: .env.example and a docs/ALERTS.md section.
- views.py hook DONE by gosha-pipe (test: tests/test_dm_embed.py::test_the_apply_button_goes_through_the_click_redirect). Suggested for the lead/PG test owner: add `DROP TABLE apply_clicks`, `DROP TABLE search_nudges` to tests/test_database_pg.py `_PRE_DEPLOY` (PG not available here).
- Full suite after: 1494 passed, 9 skipped; ruff clean.

## Scraper smoke: blocked from CI (release blocker, 2026-10-16)
Lead: the daily smoke failed 3 days because eJobs (api.ejobs.ro) and Glassdoor answer 403 to GitHub runners, while prod scrapes eJobs fine; after the release that would DM the owner daily. I own the workflow and took scripts/smoke_scrapers.py for this.
- Workflow probe step runs with `SCRAPE_BROWSER_FALLBACK: "true"` (prod has it on).
- Script: an httpx `AsyncClient.send` hook records every response status per probe query (contextvar), because adapters swallow HTTP errors into []. A source with 0 jobs, no exception/timeout, and a 403/429 on EVERY query is `blocked` ("blocked from CI (advisory)", `[BLKD]` in the log); JobSpy exceptions mentioning 403/429 too. Blocked is not in `failing` (exit 0, no issue, no DM); result JSON gains `"blocked"`. 0 jobs with 200s, refusals on only some queries, and exceptions still fail.
- Workflow JS: a `blockedNote` is appended to the recovery comment/DM and the failure headline; `core.notice` lists blocked boards.
- Tests: tests/scrapers/test_smoke.py (403/429 blocked, 200 fails, partial refusal fails, exception fails, jobspy 403 vs other, browser fallback turns a TLS 403 into jobs, workflow sets the env); tests/test_alert_switch.py node harness now records issue ops (blocked-only run: no issue, no DM; open issue closes with the note; blocked board not in the signature). docs/ALERTS.md paragraph. No live requests made.

