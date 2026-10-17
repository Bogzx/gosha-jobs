# Handoff: gosha-lead — orchestrator

Written: 2026-10-16 · Branch: wip/agents-2026-10-16 · Base commit: 5490d6a

## Mission
Coordinate the GOSHA agents toward the owner's goal ("the best job-finding
platform, find jobs very accurately; check whether it works as intended").
The owner wants the lead to **orchestrate and review, not implement**:
split work by file ownership, spawn agents in herdr, review their diffs
critically, send corrections, run the full suite, and commit integrated
work to the WIP branch. Owner's Discord: @Bogdanila.

## How to drive agents (herdr)
- `herdr agent list` · `herdr agent read <name> | tail -40` · `herdr agent prompt <name> "..."`
- New agent: `herdr pane split --current --direction right --cwd "$PWD" --no-focus`,
  then `herdr agent start <name> --kind claude --pane <id> -- --dangerously-skip-permissions --model opus`;
  a first start in a new folder shows the trust dialog → `herdr agent send-keys <name> enter`.
- `herdr --skill` prints the full guide. `.agents/resume.sh` respawns everyone from handoffs.

## State at handoff (laptop session)
- Five worker agents ran in parallel: scrape, pipe, rank, label, health. Each wrote
  `.agents/handoff/<name>.md`. All work is uncommitted on top of 5490d6a, gathered into
  `wip/agents-2026-10-16` by the lead.
- Ranking baseline reproduced exactly: production all-mpnet-base-v2 nDCG@10 0.546, TF-IDF
  0.626 (docs/EVAL.md). Seniority-aware ranking ~0.67; hybrid embed+TF-IDF+seniority ~0.71
  on the main set (14 personas, wide CIs). Held-out confirmation pending (gosha-rank).

## Baseline at handoff (laptop, 2026-10-16 ~15:08 local)
- Full suite on the WIP branch: **874 passed, 9 skipped** (`.venv/bin/python -m pytest -q`, 27 s).
- Owner said: "do this now and review later. You will still be the orchestrator on the server."
  So the wrap-up was cut short: gosha-pipe, gosha-rank and gosha-health were interrupted and
  wrote their handoffs in ~3 minutes. **Their handoffs and diffs are NOT yet reviewed by the
  lead.** gosha-label and gosha-scrape were reviewed (scrape got three new items, 5–7, added
  to its handoff).
- Owner approved pushing this WIP branch to GitHub (not main).

## First thing to do on the server
1. Re-run the suite (expect 874 passed) and ruff.
2. Review gosha-pipe.md, gosha-rank.md, gosha-health.md against the code and the review points
   below. Note: health says the smoke workflow's DM code is only syntax/structure-tested.
3. Then `.agents/resume.sh` to respawn the workers with their handoffs (after any corrections
   have been written into their handoff files).
4. Strategic priorities from the reviews so far (biggest accuracy levers):
   - Baseline catalog scrape independent of subscriptions (a CV-only user's feed holds only what
     other people's searches pulled in). Owners: scrape + pipe.
   - Feed has no location/eligibility filter: US-only RemoteOK rows and BestJobs remote padding
     reach Romanian students' feeds. Owners: rank (recommend.py) + pipe.
   - LinkedIn capped at 30 results/term while the site shows ~100 (measure on the server).
   - Hybrid + seniority ranking: confirm on eval/heldout before shipping (gosha-rank).
   - Strip invisible Unicode from eJobs text (gosha-scrape item 5).

## Review points sent (verify each was addressed in the workers' handoffs)
1. gosha-pipe: title-relevance gate now runs before semantic matching; for entry-level
   keywords it demands "junior/intern" in the title, contradicting `_is_junior` (most junior
   postings are unmarked) → recall loss for entry-level searches. Must be made consistent.
2. gosha-pipe: `filters.required_years` takes the MIN over mentions ("5+ years overall, 1 year
   Kafka a plus" → junior). Use the headline/max requirement.
3. gosha-pipe: "ro" as a whole-word Romania location token needs negative tests.
4. One seniority detector only: gosha-rank's `gosha/seniority.py` must reuse `gosha.filters`
   (required_years, JUNIOR_RE/MID_TITLE_RE/SENIOR_RE); resolve "expert" (mid vs senior) and
   roman numerals II/III/IV.
5. gosha-scrape: curl_cffi Chrome-TLS impersonation fallback after a 403 (BestJobs) must be
   opt-in (`SCRAPE_BROWSER_FALLBACK`, default off). **Owner decision pending** — it bypasses
   the site's bot protection (ToS risk).
6. gosha-health: owner requirement — ops alerts ONLY as a DM to the owner
   (`ALERT_DM_USER_IDS`, numeric ID), no channel posts/mentions, one switch to disable.

## Next steps for the lead
1. Read every worker handoff; check each review point above against the code.
2. Run the full suite: `.venv/bin/python -m pytest -q`; and the ranking eval
   `.venv/bin/python scripts/eval_ranking.py` (+ held-out flags per gosha-rank's handoff).
3. Resolve cross-agent conflicts (filters.py ↔ seniority.py; pipeline.py edits by
   pipe + health).
4. Ask the owner before: enabling the browser-fingerprint fallback, switching SEMANTIC_MODEL
   (needs re-embedding in prod), merging to main (auto-deploys).
5. Owner setup still needed: put @Bogdanila's numeric Discord user ID in `ALERT_DM_USER_IDS`
   in the production .env (Developer Mode → right-click → Copy User ID).

## Environment notes
- Laptop paths (/data/Repos/gosha-jobs, /tmp/claude-1000/...) do not exist on the server.
- Embedding cache for experiments lived in the laptop scratchpad; re-runs re-download the
  model (~420 MB for all-mpnet-base-v2) and re-encode (~2 min on CPU).

## Server session (2026-10-16, ~13:00 UTC) — state
- Clone: /home/ubuntu/gosha-agents (live deployment is /home/ubuntu/gosha — never touch).
- Baseline reproduced: 874 passed, 9 skipped — only with
  `LD_PRELOAD=$PWD/.venv/lib/python3.11/site-packages/scikit_learn.libs/libgomp-a49a47f9.so.1.0.0`
  (aarch64 static-TLS bug; without it 4 semantic tests skip silently). Prod container unaffected.
- ruff: excluded `.agents` in pyproject; 12 findings remain in eval/heldout/collect.py → gosha-label.
- Reviews of pipe/rank/health done (by read-only reviewer subagents, checked against code); corrections
  appended to each handoff under "Lead review on the server". Key items:
  pipe: required_years drops ≥10 years (HIGH), over-broad nice-to-have, ±80-char window false positives,
  excluded ".NET" misses "ASP.NET", location collisions, location validation.
  rank: stale caches keyed by id only (HIGH), the "failing" test passes only via test-order leakage,
  clear-all eviction mid-build, clip z, one seniority detector AFTER pipe fixes required_years, CI eval gate
  baseline stale.
  health: off-switch semantics differ per path, `<@id>` dropped outside the bot, flapping, closed-DM
  notice dedup (with pipe), test that can send a real DM (fix first), DM retry.
- Prod findings (read-only): BestJobs not blocked from prod IP (fallback unnecessary). Subscriptions #24
  `["iasi","kra","mun"]` and #25 `["llocation:Cluj"]` have malformed locations → owner decision to fix rows.
- Next: respawn workers with `.agents/resume.sh`, then review each unit as they report.

## Progress (server, 2026-10-16 ~14:30 UTC)
Committed on the WIP branch (not pushed): label held-out (39a5cd5, eefe802), scrape (e6fdc92 job_level+LinkedIn
page cache, ac6b1eb per-board retries, 50be546 text cleaning, 85f6409 Indeed junior fallback, 1117b44 baseline
combos off by default), rank (cc2a29d caches/window index/z-clip), health (23aa9a9 alerts round 2 + handoff),
pipe (c5cc7bc corrections checkpoint). Full suite 1237 passed / 9 skipped; ruff clean.
- Postgres: whole suite run once against a throwaway postgres:16 container (removed): no WIP regressions; 2 tests are
  SQLite-only by design (test_subscriptions_api sqlite_master, test_auth id reuse) and break following tests by
  leaving rows. TODO (later): PG job in CI + skip those two on PG. Harness: pytest plugin that patches
  tests.conftest.TEST_DB_URL (see git history of this note for the approach).
- Rule given to all agents: this server's IP is prod's scraping IP → no live board/site requests without lead OK.
- In flight: pipe follow-up 1–9 (multi-city regression, clause-crossing years, age, freshness tz on PG, combo window,
  held-out negation rule) then baseline hook; rank item 6 (seniority imports filters) + item 7 (eval.yml paths;
  baselines after pipe says required_years final) + EVAL.md disclosure; scrape Hipo dates; label targeted collection
  (≤10 searches, before 15:45 UTC); health standing by.

## Deploy checklist (owner, when merging to main)
1. ALERT_DM_USER_IDS=<numeric id> in prod .env; GitHub secrets DISCORD_BOT_TOKEN + ALERT_DM_USER_IDS.
2. Delete data/scrape_health.json once (keep data/alerts_state.json).
3. After deploy: run "Site watchdog" workflow once; watch LinkedIn search-429 counts for 2 cycles before raising
   LINKEDIN_RESULTS_PER_TERM to 60.
4. SCRAPE_BASELINE stays off until the pipe hook is reviewed; SCRAPE_BROWSER_FALLBACK stays off (BestJobs works from prod).
5. Owner decisions: subscriptions #24 ["iasi","kra","mun"] and #25 ["llocation:Cluj"] (prod rows untouched).

## Owner decisions applied (2026-10-16 ~13:20 UTC)
- #2 Subscriptions fixed in PROD (only rows touched): #24 ["iasi","kra","mun"] → ["iasi"] (kra/mun matched
  nothing; user can re-add a real city), #25 ["llocation:Cluj"] → ["Cluj"]. Backup of all subscriptions:
  scratchpad subs_backup_20261016T1315.tsv (session-local; values above are the full rollback).
- #3 SCRAPE_BROWSER_FALLBACK=true appended to /home/ubuntu/gosha/.env (backup .env.bak-20261016T1316). No restart;
  the running 5490d6a code ignores it; active after deploy, and only after a BestJobs 403. Note: BestJobs works
  from prod today (venv 403s were the agents' TLS fingerprint, confirmed with 2 requests).
- #4 "Exactly 2 years = junior" applied to both label sets (gosha-label), then baselines regenerated (gosha-rank).
- End-to-end review launched: 5 read-only reviewers (delivery, scraping, feed, operations, product);
  shared context: scratchpad review_context.md.
Deploy checklist update: step 4 → SCRAPE_BROWSER_FALLBACK is ON by owner decision (already in prod .env).

## Prod writes by owner decision (2026-10-16 ~13:40 UTC) — rollback info
- subscriptions #16 ["Cluj-Napoca","Romania"]→["Cluj-Napoca"], #17 ["Bucuresti","Romania"]→["Bucuresti"].
- subscriptions #16–#20 keywords '["computer science internship — 19 intern/junior roles"]' (literal — escape
  in the text) → '["computer science internship"]'.
- 1,201 active LinkedIn jobs with US locations set is_active=false (none deleted); ids in scratchpad
  deact/ROLLBACK_reactivate_these_ids_20261016.txt. European non-Romanian rows (Munich, Kraków, …) kept by the
  approved rule "outside Romania/Europe/remote". Active jobs: 1,629 → 428.
- Owner decisions: monthly empty-search nudge (default OFF until flipped after deploy) + Apply-click redirect
  tracking → gosha-health; ranges labelled by lower bound → gosha-label.
- Review results (delivery): prod DM precision ~0.5 strict; threshold not the lever; gates are. Details in
  scratchpad review-delivery/ (session-local) and forwarded to gosha-pipe/rank/scrape.

## Owner decisions + review results (2026-10-16 ~15:50 UTC)
- Entry-level rule (owner): intern-only searches get ONLY training positions (internship, traineeship,
  apprenticeship, working student, graduate programme); junior roles only when the search chose junior; never
  mid/senior. → gosha-pipe (shared filters), gosha-rank (eval uses the same functions).
- Eval: mid postings → FILTER, don't regrade (owner). Labels unchanged.
- ALERT_DM_USER_IDS set in prod .env from ADMIN_DISCORD_IDS (owner confirmed it is @Bogdanila); backup of .env made.
- Backups (owner): nightly local backup /home/ubuntu/backups/gosha/backup.sh via crontab 03:10 UTC: pg_dump -Fc
  (verified by pg_restore --list) + data/ (CVs, state) tarred from inside gosha-bot; 14 days; first run 15:47 OK.
  Off-box copy still missing (needs a restic remote from the owner).
- Operations review: prod runs as intended (all scheduled jobs on time, model loaded, 0 5xx); WIP imports and
  starts in the prod image; no schema/dependency changes; rollback safe. Deploy checklist corrections:
  data/scrape_health.json does not exist in prod (drop that step); GitHub secrets DISCORD_BOT_TOKEN +
  ALERT_DM_USER_IDS still to add; freeze the branch and re-run pytest + ruff + both eval --check on the exact
  merge commit; after deploy check /api/v1/health/freshness, "Using semantic matching", DM counts.
- .agents/ added to .dockerignore.

## RELEASED 2026-10-16 17:11 UTC — main = 03e5314 (fast-forward from 5490d6a)
- Release gate (scratchpad release_gate.sh) passed on 03e5314: ruff; pytest CI-identical venv 1588 + full venv 1592;
  real Postgres 15 + full suite 1590; eval --check main+held-out; web eslint/vitest/build; prod-image imports (78
  modules, DM panel 40) and API routes. CI run 38937293880: backend, frontend, images, deploy all green.
- Pre-deploy manual check of every would-send DM (48 → 46 after the duplicate fix a1f5aef): 0 irrelevant.
- Prod after deploy: 4 containers healthy; "Ops alerts will be DMed to [owner]"; apply_clicks + search_nudges tables
  created; /api/v1/health 200; /api/v1/health/freshness 200 (interval 240); forged /api/v1/r/… → 404; SPA 200.
- DM watermark: user_jobs.id > 4418 are the new code's DMs (the 17:07 old-code cycle sent 27).
- Next: manual review of every DM of the first new cycles; watch LinkedIn 429s; then decide LINKEDIN_RESULTS_PER_TERM 60.
- Owner still to do: GitHub secrets DISCORD_BOT_TOKEN + ALERT_DM_USER_IDS (site watchdog / smoke DMs); off-box backup
  target; EMPTY_SEARCH_NUDGE_ENABLED when ready.

## Post-release DM verification (2026-10-16, owner request: every DM checked by hand)
- 17:35 cycle (03e5314): 65 DMs judged by hand → 51 right, 14 wrong (experienced-only roles, description-less
  untitled roles, Alignerr AI-training gigs, one non-CS plant role). Fixed in 1fe6243 (asks_experienced incl.
  'previous experience', lacks_level_evidence, is_gig_marketplace, CS-in-description for generic engineering
  titles, full prefilter re-run at send time). Gate passed; deployed 18:05 UTC (CI 38944175819 green).
- 18:28 cycle (1fe6243): 20 DMs, all from the re-checked queue, judged by hand → 20/20 within the owner's rules
  (intern-only searches: only training positions; junior roles only where junior was chosen; 2-year/range rule).
  0 duplicates per user (title+city check across all 222+ sent rows).
- Pre-release would-send lists (would_send*.csv, scratchpad) were reviewed by hand at each step (48 → 46 → 51/30 → v3b).
- KEEP DOING after each change to matching: export fresh prod data read-only, produce a would-send list with the
  exact commit, judge every row, then gate (scratchpad release_gate.sh) and deploy.

## Lead session 3 (2026-10-17 ~12:10 UTC) — V3 release in progress
- Lead pane runs from /home/ubuntu/gosha-jobs (herdr w5:p1, renamed gosha-lead); workers in /home/ubuntu/gosha-agents,
  started by lead scratchpad launch.sh (session ba90e926) with per-agent assignments, not resume.sh's generic prompt.
- Prod = main = d4158c7 (deployed ~11:30). 11:52 cycle sent 4 DMs, all one job: 8316 Hipo Bosch "Agentic AI Engineer"
  → subs 3/5/25/26 (intern+junior). Judged WRONG (experienced-developer requirements, no entry evidence). The rule on
  main passes it (dm_junior_ok default=True); the uncommitted V3 tree blocks it (evidence=False).
- d2b3ab3's commit message still says "WIP … unreviewed"; gosha-pipe reviewed and approved it. It is on main.
- Uncommitted V3 tree verified by the lead: pytest 1745 passed / 9 skipped (LD_PRELOAD), ruff clean.
- Plan: pipe → eval --check + would-send list (fresh snapshot) + dropped list; label judges every row; lead spot-judges;
  commit on WIP; release_gate.sh on the commit; owner go-ahead before pushing main (auto-deploys).
- Parallel: scrape (empty BestJobs/Hipo/LinkedIn rows after deploy), rank (close classifier, V3 eval check, feed dedup),
  health (today's alerts, Postgres CI job).
- 9 pending user_jobs (8325, 8330 BestJobs; 8275 LinkedIn; empty descriptions) — decision pending.
- ~12:50 UTC: committed 9b0f28a (V3 + scrape's LinkedIn description-keeping dedup + rank's seniority split_glued_level)
  and 038581d (health's Postgres CI job, not gating deploy). Gate: lead scratchpad (ba90e926) release_gate.sh (PG step
  uses GOSHA_TEST_DB_URL now, no pg_plugin) → gate-038581d.log.
- Would-send (pipe, export 12:15:48Z): next cycle identical for main and V3 (10 sends, 9 queued wait). V3 value is in the
  gate flips: 33 main-only (15 already sent by main, 18 would be sent later: Timisoara sub 22 ×12, SOFTONE course ×2…),
  8 tree-only (Ascom "InternSoftware Engineer"). gosha-label judging. Known recall loss: 8319 Wordpress "min. 24 luni"
  (months not parsed) → pipe next round.
- Not in the release (uncommitted, in progress): pipe expired-BestJobs pipeline part + scrape-filter location check +
  months; scrape expired adapter part; rank feed dedup + feed = DM gates; health stuck-alert fix; label desc-cap plan.
- Owner decisions bundled: deploy go-ahead; deactivate 53 empty unlisted rows (scrape's id list) + 299 foreign rows
  from the 2026-10-16 16:56 burst; GitHub secrets (site watchdog OFF without them); off-box backups.
- 2026-10-17 12:41 UTC: 038581d pushed to main (owner go-ahead; CI 39033520470). WIP branch pushed (c773e6b).
  DM watermark: user_jobs.id > 4545 are DMs from the new code. Next: judge every DM of the first cycles by hand.
- 2026-10-17 ~12:50 UTC, owner-approved prod write: 357 jobs set is_active=false (52 empty unlisted BestJobs/LinkedIn
  rows from scrape's list + 27 LinkedIn empty-location rows + the 299-row 2026-10-16 16:56 foreign burst; overlap 21).
  Active jobs 846 → 489. Rollback ids: /home/ubuntu/backups/gosha/ROLLBACK_reactivate_these_ids_20261017.txt.
- Owner decisions 2026-10-17: graduate programmes / new-grad / Global Trainee needing a finished degree = JUNIOR;
  every DM shows 'Level: Internship' / 'Level: Junior'. Workers report only when their whole list is done or blocked.
- Full DM review (all 263 sent DMs, lead + label independently, 233/263 agree): lead scratchpad alldms/ (session ba90e926).
