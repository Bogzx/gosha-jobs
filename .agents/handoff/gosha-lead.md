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
