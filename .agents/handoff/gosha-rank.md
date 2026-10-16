# Handoff: gosha-rank — web-feed ranking accuracy (hybrid + seniority), ranking eval

Written: 2026-10-16 12:05 UTC, updated on the server 2026-10-16 (lead-review items 1–5 and 8 done) · Branch: wip/agents-2026-10-16 · Base commit: c67e734 (uncommitted on top)

## Mission (one paragraph, self-contained)
Make the web feed (and the public demo, `gosha/api/demo.py`) rank postings for a CV as accurately as
possible, and make CI measure exactly the ranking that ships. Offline eval (docs/EVAL.md) showed the
production ranking (pure `all-mpnet-base-v2` cosine, nDCG@10 0.546) lost to a plain TF-IDF baseline
(0.626) and was blind to seniority (students were shown "Senior …" first). The lead wrote a hybrid
ranker (z(cosine)·0.5 + z(TF-IDF)·0.5 − seniority penalty); this agent owns reviewing/fixing it,
testing it, putting it in the eval harness + CI gate, choosing weights (validated on BOTH the main
set and the independent held-out set), deciding on the embedding model, and documenting honest
numbers. User goal: GOSHA = the most accurate job finder.

## File ownership
May edit: gosha/recommend.py, gosha/lexical.py, gosha/seniority.py, gosha/matching.py,
gosha/embeddings.py, gosha/evaluation.py, scripts/eval_ranking.py, eval/results/*, docs/EVAL.md,
gosha/api/demo.py, gosha/demo_personas.py, tests/test_recommend.py, tests/test_evaluation.py,
tests/test_matching.py, tests/test_match_*.py, new tests (tests/test_seniority.py, tests/test_lexical.py).
Probably also .github/workflows/eval.yml (CI gate paths/baseline) — confirm with the lead.
Must NOT touch: gosha/pipeline.py, gosha/filters.py, gosha/delivery.py, gosha/services/jobs.py
(gosha-pipe); gosha/scrapers/* (gosha-scrape); scrape_health/main/config (gosha-health);
eval/heldout/* (gosha-label; read-only — never tune on it).
No git commit/push/stash/checkout/reset (the lead commits).

## Done (verified)
- **gosha/lexical.py rewritten** (whole file, ~200 lines), on top of the lead's version:
  - `terms()` now folds diacritics (NFKD, drop combining marks): "experiență"/"experienta"/"experienţă"
    (cedilla) are one term. Lifted main-set production nDCG@10 to 0.731 (lead measured 0.711 without
    folding and with a slightly different seniority regex/IDF corpus — not a clean A/B, see TODO 3).
  - Cache keys are now any `Hashable` (was int) so recommend.py can key on (id, embedding) — see TODO 1.
  - `index_for()` keeps an LRU of `MAX_INDEXES = 4` indexes (was a single `_last` slot) and picks the
    smallest covering index within `REUSE_SLACK = 0.25`; the single slot thrashed between the demo
    (all candidates) and signed-in users (candidates minus rated / excluded words) → full rebuild per request.
  - `LexicalIndex.__init__` validates one key per doc; `score()` tolerates a term whose only postings
    had zero norm. `clear_cache()` clears counts and indexes (the `_last` global is gone; nothing else
    referenced it — grep verified).
- **gosha/evaluation.py: the harness now ranks the feed exactly as production does.**
  - `Vectors` gained `keyword` (U,P; `lexical.LexicalIndex` over job texts, fitted on postings only, as
    prod) and `penalties` (U,P; `seniority.level_penalty(cv_level(cv), posting_level(title, description))`),
    built by new `ranking_signals()`.
  - New `FEED_RANKINGS = ("production", "cosine")` and `feed_scores(vectors, u, vector=None, idx=None,
    ranking=...)` → calls `recommend.hybrid_scores` (z-scores over the scored subset, as prod over the
    user's candidates).
  - `evaluate_feed(..., ranking=)`; `run(..., feed_ranking="production")` returns `feed` (production
    ranking), `feed_cosine` (old pure cosine, for comparison) and `feed_ranking`.
  - `simulate_feedback(..., ranking=)` runs the Rocchio residual simulation through the hybrid
    (feedback moves only the embedding vector; keyword/seniority terms stay). Helper `_order()` added
    because the `ranking` parameter shadows the module function `ranking()` inside it.
- **scripts/eval_ranking.py**: `--feed-ranking {production,cosine}`; `tfidf` spec always uses
  `feed_ranking="cosine"` (a baseline mixed with itself is no baseline); summary table has a
  "feed ranking" column and prints both production and cosine rows; non-production rankings write
  `<model>.cosine.json`. `--postings/--personas` already existed; docstring shows the held-out command
  (`--write-results eval/results/heldout`).
- Tests: `.venv/bin/python -m pytest -q tests/test_recommend.py tests/test_evaluation.py
  tests/test_matching.py tests/test_match_*.py tests/api` → **221 passed, 6 skipped, 1 failed**
  (the failure is the lead's known one, below). tests/test_evaluation.py (incl. CLI `--check` tests) passes
  with the new harness.

## Measured (copy of scratchpad results — the /tmp files will NOT travel)
nDCG@10 (95% bootstrap CI over personas), all-mpnet-base-v2, current code (lead's seniority.py + my lexical.py):

| Set | production (hybrid 0.5 + seniority) | cosine only (old prod) |
|---|---|---|
| main (eval/postings.jsonl 202 × 14 personas) | **0.731** (0.623–0.831), R-prec 0.485, MRR 0.763, P@10 0.421 | 0.546 (0.445–0.645) |
| held-out (eval/heldout 253 postings × 10 personas) | **0.625** (0.482–0.737), R-prec 0.416, MRR 0.737, P@10 0.410 | 0.543 (0.420–0.655) |

Held-out gain (+0.08) is much smaller than main (+0.19): part of the main gain is likely tuned to the
main set. Run with `--no-feedback`; eval/results/*.json are NOT regenerated yet.

Lead's sweep on the main set only (scratch script, TF-IDF IDF fitted on postings+CVs, own seniority regex;
"senior" = z-units penalty for senior postings):
- mpnet: z − 1.0·senior 0.671; hybrid a=0.5 0.643; hybrid a=0.5 − 1.0·senior 0.711; a=0.7 − 1.0·senior 0.716;
  boilerplate stripping no gain.
- TF-IDF alone 0.626; z − 0.3..1.0·senior 0.640.
- bge-small-en-v1.5: cosine 0.558; best hybrid a=0.5 − 1.0·senior 0.687.
- bge-base-en-v1.5: cosine 0.563; hybrid a=0.5 − 1.0·senior 0.698; title-only 0.644.
- multilingual-e5-base (query:/passage: prefixes): cosine 0.488; best hybrid a=0.3 − 1.0·senior 0.649.
- bge-m3: cosine 0.472; hybrid a=0.5 − 0.5/1.0·senior 0.685; title-only 0.475.
- multilingual-e5-large: was still running at handoff; result unknown (and >2 GB RAM model, see Findings).
- Conclusion so far: no model beats mpnet within the hybrid on the main set → keep `SEMANTIC_MODEL`
  (no re-embedding / threshold re-tuning). Held-out not run for other models.

## In progress (exact state)
- Nothing half-applied. Lead-review items 1–5 and 8 are done (see "Server session" below); item 6 waits on
  gosha-pipe's filters work; item 7 (regenerate baselines + eval.yml paths) after 6.
- Old "known failing" test is rewritten and passes alone and in the full suite.

## Remaining TODO (prioritized)
(TODO 1 and the lexical/hybrid parts of TODO 4 are DONE on the server, see below.)
1. **[DONE] Fix gosha/recommend.py (review findings on the lead's code, not yet applied):**
   a. `_needs_text(job, keyword: bool)`: only require lexical caching when `cv_text.strip()`; decide
      `cv_text` before the load at recommend.py:479 (it already is). Then rewrite the failing test:
      request 1 (CV text present via monkeypatched `load_cv`) loads descriptions of uncached postings;
      request 2 loads only the page; and the `[True]`-without-CV-text case loads only the page. Add an
      autouse fixture that clears `recommend._level_cache` and calls `lexical.clear_cache()`.
   b. **Stale caches:** gosha/pipeline.py:131-149 updates title/company/description of existing jobs (and
      re-embeds), but `_level_cache` and lexical counts are keyed by `job.id` only. Key both on
      `(job.id, hash(job.embedding))` — embedding bytes are loaded for ranking and change whenever the text does.
   c. `_loaded_description()` (recommend.py:381) returns None both for "deferred" and "really NULL":
      `_lexical_text()` then caches title-only counts for a deferred-but-evicted job permanently, and
      `posting_level()` never caches jobs whose description is NULL (reloaded every request). Return a
      sentinel for deferred; cache NULL descriptions normally; never cache counts built from a deferred row.
   d. `_level_cache` is mutated from `asyncio.to_thread` workers concurrently: guard with a lock (like lexical).
2. **Single seniority detector (orchestrator order):** make gosha/seniority.py import from gosha.filters
   (read-only): `JUNIOR_RE`, `INTERN_RE`, `ENTRY_LEVEL_RE`, `MID_TITLE_RE`, `SENIOR_RE`, `required_years`,
   `JUNIOR_MAX_YEARS`, `fold`; keep only ranking-specific code (cv_level, `_PENALTY`, level_penalty,
   maybe description entry words). Wait until gosha-pipe's filters API is stable (they were changing
   `required_years` from MIN-over-mentions to "strictest real requirement, ignoring nice-to-have") —
   check with `herdr agent read gosha-pipe`. Resolve consistently with filters: "expert" = MID (filters'
   choice; "Expert contabil"/"Expert achiziții" are not senior), roman "II/III/IV" = MID. Suggested
   posting_level order: title entry (INTERN_RE|JUNIOR_RE|ENTRY_LEVEL_RE) → SENIOR_RE → MID_TITLE_RE →
   board label → required_years (≤2 ENTRY, 3–4 MID, ≥5 SENIOR; filters says 2 is still junior) → UNKNOWN.
   Note SENIOR_RE has bare "staff"/"lead"/"manager" ("Staff Accountant", "Lead Generation" false positives) —
   tell gosha-pipe rather than editing filters.
   cv_level gaps found: CVs expressing experience only as date ranges ("2016 – present") are read as
   JUNIOR → a real senior would get senior postings pushed down. Consider summing non-education date
   ranges. Both eval sets contain only student personas, so mid/senior readers are UNVALIDATED.
3. Choose EMBEDDING_WEIGHT and penalty scale on the main set with the new harness (sweep a ∈ {0.3,0.5,0.7},
   penalty scale ∈ {0.5,1.0}), confirm on held-out; prefer round values; differences < ~0.05 are noise
   (14 / 10 personas). Also A/B diacritic folding in lexical.terms on both sets (keep only if not worse).
4. Write tests/test_seniority.py (EN+RO: "Senior", "Sr.", "Lead", "Șef"/"Şef", "Junior", "Stagiu",
   "Intern" vs "International"/"Internal", "3+ years of experience", "minim 2 ani experiență",
   "over 20 years on the market", CVs: student / 4-year mid / 8-year senior), tests/test_lexical.py
   (folding, c++/c#/node.js tokens, cache keys, index reuse/LRU/slack, evicted key → empty Counter),
   hybrid_scores tests (z-scaling, keyword with ptp 0 → cosine only, penalties subtract).
5. Regenerate results (with feedback): `.venv/bin/python scripts/eval_ranking.py --write-results eval/results`
   and the held-out one into eval/results/heldout; also `--models tfidf`. The CI gate
   (.github/workflows/eval.yml, `--check eval/results/all-mpnet-base-v2.json`) compares `feed` → now the
   production ranking, so the regenerated baseline must be committed together with the code. Add
   gosha/lexical.py and gosha/seniority.py to eval.yml `paths:`; optionally add a held-out `--check` step.
6. Discord path (task 5): measure in the eval whether `dm score − λ·penalty` (cosine units, λ≈0.03–0.05
   per z-unit) improves recall vs off-target at the production thresholds (matching.DEFAULT_CV_THRESHOLD /
   DEFAULT_THRESHOLD). Note pipeline.py:530-545 already hard-filters by subscription experience level and
   keyword-implied entry level, which the eval does not model. If it helps: pure helper in seniority.py
   and send gosha-pipe the exact hook via `herdr agent prompt gosha-pipe '<message>'` (do not edit pipeline.py).
7. docs/EVAL.md: replace "Results (2026-10-01)" framing with the new production numbers on BOTH sets
   (with CIs), cosine-only as the comparison row, model sweep table, honest note on main vs held-out gap;
   check the README ranking claim.

## Open review points from the orchestrator
- "Review the lead's code critically (bugs, thread-safety, cache invalidation, deferred description access,
  perf)" — reviewed; lexical issues fixed; recommend.py issues listed in TODO 1 (NOT yet fixed).
- "Fix test_feed_reads_descriptions_only_for_the_page[True] + cache-clearing fixture" — NOT done (TODO 1a);
  the failure is partly a real bug, see In progress.
- "ONE seniority detector: seniority.py must reuse gosha.filters regexes/required_years; resolve 'expert'
  and roman II/III/IV consistently; wait for gosha-pipe's API" — NOT done (TODO 2); decisions proposed above.
- "Every decision validated on BOTH sets, do not tune on held-out" — held-out harness works; weights not yet re-chosen.
- "Model switch only if clearly better on both sets and fits ~2 GB container; write migration steps, don't flip
  default" — no switch warranted so far (see Measured).
- "Do not git commit" — respected.

## Findings not yet acted on / recommendations
- Feedback (Rocchio) now only moves the embedding half of the score, so its effect in the feed is diluted
  by EMBEDDING_WEIGHT; the harness now measures this (`simulate_feedback(ranking="production")`) — check
  whether LIKE/DISLIKE weights should rise.
- bge-m3 and e5-large are ~2.2 GB fp32: they do not fit the ~2 GB prod container regardless of accuracy.
- `FeedItem.score` stays the raw cosine (API match_score, Discord threshold semantics); percentile is of the
  hybrid score, so it agrees with the order.

## How to verify
- Tests: `.venv/bin/python -m pytest -q tests/test_recommend.py tests/test_evaluation.py tests/test_matching.py tests/test_match_*.py tests/api`
  → expect 1 failure (above) until TODO 1a; then full suite `.venv/bin/python -m pytest -q`.
- Eval main: `.venv/bin/python scripts/eval_ranking.py --no-feedback` → production 0.731, cosine 0.546 (~15 s with warm cache).
- Eval held-out: `.venv/bin/python scripts/eval_ranking.py --no-feedback --postings eval/heldout/postings.jsonl --personas eval/heldout/personas.jsonl`
  → production 0.625, cosine 0.543 (~200 s cold).

## Environment notes
- venv `.venv` (do not modify). sentence-transformers downloads models to ~/.cache/huggingface.
- Embedding cache for the eval CLI: `~/.cache/gosha-eval/all-mpnet-base-v2.npz` (machine-specific; rebuilt automatically, `--cache-dir ""` disables).
- Machine-specific, will NOT transfer: the lead's sweep script and logs under
  /tmp/claude-1000/-data-Repos/2e9dc50a-94ea-4281-8198-f5d45a207a0f/scratchpad/ (exp.py, models.log, cache/) —
  numbers copied above; a background `exp.py` (e5-large) was still running on the old machine.
- `herdr` is the multi-agent CLI used to talk to gosha-pipe / the lead.

## Lead review on the server (2026-10-16) — corrections to apply, in this order
Main-set eval reproduces (production 0.731 vs cosine 0.546). Run tests on this aarch64 server with
`LD_PRELOAD=$PWD/.venv/lib/python3.11/site-packages/scikit_learn.libs/libgomp-a49a47f9.so.1.0.0`.
1. **The "known failing" test only passes by test-order leakage.** tests/api/test_demo.py fills module-level
   `lexical._counts` / `recommend._level_cache` for ids 1..N; later tests reuse ids on fresh SQLite. Run alone,
   `test_feed_reads_descriptions_only_for_the_page[True]` still fails. Add the autouse cache-clearing
   fixture (in tests/conftest.py scope that covers tests/api too — ask the lead if conftest needs touching;
   you may add it) and fix the bug: `_needs_text(job, keyword=bool(cv_text.strip()))` (recommend.py:400).
2. **HIGH stale caches**: key `_level_cache` and lexical counts on `(id, hash(embedding bytes))`
   (recommend.py:378, :609-616). Scenario: title-only first sighting → counts cached → description arrives
   and re-embeds → never reloaded until restart. Fix the lexical.py:17-19 docstring that claims this already.
3. **Deferred vs NULL + clear-all eviction**: `_cached_counts` clears all `_counts` at 20k mid-build
   (lexical.py:78-79) → later keys get empty Counters → broken index cached and reused for everyone.
   Sentinel for "deferred"; real LRU (or snapshot); never cache an index built with a miss; cache genuinely
   NULL descriptions in `_level_cache` (recommend.py:393).
4. Thread-safety: `posting_level` `in` then `[]` (recommend.py:389) → KeyError under concurrent clear;
   use `.get()` + lock.
5. **Clip z-scores to ±3** in hybrid_scores (sparse keyword z can reach √N ≈ 44 and dominate).
6. **One seniority detector (TODO 2)** — sequencing: gosha-pipe is now fixing `filters.required_years`
   (≥10 years dropped, over-broad nice-to-have skip, ±80-char window false positives). Do items 1–5 first,
   then import from filters once pipe reports done. 13/202 main and 22/253 held-out titles disagree today
   (comparison script: /tmp/claude-1001/-home-ubuntu-gosha-jobs/3f714841-0998-4735-a06e-3fd5c1335b14/scratchpad/cmp_seniority.py).
   Decisions: exactly 2 years = ENTRY (matches filters and gold labels 6/8); "expert" = MID; II/III/IV = MID;
   cedilla "Şef"/"Șefă" via fold; keep your `student\w*` (filters lacks "Working Student" — tell gosha-pipe);
   "Staff Accountant"/"Lead Generation" false SENIOR → tell gosha-pipe to fix SENIOR_RE, not locally.
   Empty/undecryptable CV text → UNKNOWN level (no penalty), not JUNIOR.
7. **CI gate is toothless**: eval/results/*.json still hold the 0.546 baseline, so a 0.18 regression passes;
   add gosha/lexical.py and gosha/seniority.py to eval.yml `paths:`. Regenerate results (TODO 5) only after
   items 1–6, commit code + baseline together (via the lead).
8. Lexical superset-index reuse changes IDF (top-10 overlap 8–10/10): prefer exact-key reuse unless the
   rebuild cost is measured to matter; otherwise document it.
9. Held-out gain (+0.08) vs main (+0.19) suggests main-set overfitting: choose weights on main, report
   held-out, prefer round values; no regex/penalty tweaks justified only by main-set failures.
10. Coordinate with gosha-pipe on feed hard filters (their TODO 1: location/level in get_feed — your file).

## Server session (2026-10-16): lead-review corrections applied
Verify: full suite `LD_PRELOAD=... .venv/bin/python -m pytest -q` → **1015 passed, 9 skipped** (other agents'
new tests included); `ruff check` clean on my files.
1. DONE. `tests/conftest.py`: autouse `_fresh_ranking_caches` (calls new `recommend.clear_caches()` before/after
   every test, covers tests/api). `_needs_text(job, cv_text)`: nothing to read without CV text. Needed with it:
   `seniority.cv_level("")`/whitespace → new `CV_UNKNOWN` (no penalty; `level_penalty` returns 0 for unknown
   readers; `_order` skips posting_level entirely) — this is the lead's item-6 decision "empty CV → UNKNOWN",
   applied now. Test `test_feed_reads_descriptions_only_for_the_page[none|vector_only|text]`: text → first
   request reads the whole window, second only the page; others only the page. `vector_only` fails on old code.
2. DONE. `recommend._cache_key(job) = (id, hash(job.embedding))` for lexical counts; `_level_key` adds
   `experience_level` (board label changes without re-embed). lexical.py docstring corrected.
   Test `test_feed_rereads_a_posting_whose_text_changed` (fails on old code), `test_posting_level_follows_the_text`.
3. DONE. `_loaded_description` returns `_DEFERRED` sentinel; `_lexical_text` → None only when deferred;
   `posting_level` caches real NULL descriptions, never a deferred reading. lexical `_counts` is a real LRU
   (OrderedDict, hits move_to_end, evict oldest — a build never evicts its own keys); a deferred-and-uncached
   key is a miss (`_cached_counts` → None), `LexicalIndex.complete=False`, scored 0 for that request, and
   `index_for` does not cache an incomplete index. Test `test_a_deferred_description_is_never_cached_as_missing`.
4. DONE. `_level_cache` is an LRU OrderedDict under `_level_lock`, `.get()` inside the lock.
   Test `test_posting_level_is_safe_under_concurrent_clears`.
5. DONE. `recommend.Z_CLIP = 3.0` in `_zscore`. Clean A/B (`--no-feedback`, same code, Z_CLIP inf vs 3):
   main 0.731 → **0.743** (0.634–0.844); held-out (258 postings) 0.618 → **0.624** (0.469–0.741). Noise-level but
   not worse on either → kept. Test `test_hybrid_scores_clip_a_sparse_keyword_outlier`.
8. DONE (changed design, not just documented). `REUSE_SLACK`/superset reuse removed: `index_for` reuses only
   an index over exactly the same key set. `get_feed` now loads the whole window (no SQL `notin_` on rated /
   applied ids; filtered in Python) and `_order(candidates, vector, cv_text, corpus=window)` scores the user's
   subset against one window index → the IDF is the same for every user and the demo, and no request's ranking
   depends on what other indexes are cached. Measured on this server, 2,000 postings: cold build 1.1 s, warm
   rebuild (counts cached) 0.4 s, score 0.6 ms → per-user exact indexes would cost 0.4 s/request; one window
   index costs that once per window change. Test `test_every_user_shares_one_keyword_index_over_the_window`.
New `tests/test_lexical.py` (9 tests: folding, skill tokens, LRU, miss, exact reuse, bounded index cache).
New tests in tests/test_recommend.py also include `test_no_cv_text_means_no_seniority_penalty`.

Current numbers (`--no-feedback`, all-mpnet-base-v2, this code):
| Set | production | cosine only |
|---|---|---|
| main (202 × 14) | 0.743 (0.634–0.844), R-prec 0.544, MRR 0.799, P@10 0.429 | 0.546 (0.445–0.645) |
| held-out (**258** × 10; gosha-label added 5 QA postings, 253 version = commit 39a5cd5) | 0.624 (0.469–0.741), R-prec 0.402, MRR 0.804, P@10 0.410 | 0.542 (0.418–0.654) |
Not yet regenerated into eval/results (item 7, after item 6).

Rules received on the server: no live requests to job boards or the production site without asking
gosha-lead (this server is prod's scraping IP); read-only docker logs / SELECTs are fine.

Next: item 6 (single seniority detector from gosha.filters) once gosha-pipe reports `required_years` done;
then item 7 (TODO 3 weight sweep on main, report held-out; TODO 5 regenerate results with feedback incl.
held-out; eval.yml paths), TODO 6 (Discord penalty eval), TODO 7 (docs/EVAL.md). Item 10: coordinate with
gosha-pipe on feed hard filters in get_feed (my file) — note get_feed now loads the whole window and filters
exclusions in Python; any hard filter should filter `candidates`, not `window` (the window is the IDF corpus).


## Server session 2 (2026-10-16, after lead commit cc2a29d)
Lead accepted the window-index redesign and Z_CLIP=3 (a priori, held-out confirmation only). Order from the lead:
item 7's eval.yml part now; NO baseline regeneration until item 6 lands (code + baselines committed together);
write the feed hard-filter design (below), implement after item 6; report when 6+7 are done.

### Item 7, CI part: DONE (uncommitted), baselines still pending
- `.github/workflows/eval.yml`: `paths:` + gosha/lexical.py, gosha/seniority.py, gosha/filters.py (seniority will
  read levels through the filters' regexes after item 6); recommend.py was already there. New step "Same on the
  held-out set" (`if: !cancelled()`, pipefail, `--check eval/results/heldout/all-mpnet-base-v2.json`, appended to
  the step summary). **That baseline file does not exist yet**: the step must be committed together with the
  regenerated baselines (item 7, after item 6), or CI fails on the missing file.
- `scripts/eval_ranking.py`: every result now records `postings_sha256` / `personas_sha256` (16-hex sha256 of
  the files); `check()` fails with "regenerate the baseline" when the baseline's model or fingerprints differ
  (the held-out set went 253 → 258; a number-only gate compared two different measurements), and warns when a
  baseline predates fingerprints (all committed ones today: they get regenerated anyway).
  Test: tests/test_evaluation.py `test_cli_check_refuses_a_baseline_from_another_eval_set`.
- Consequence for gosha-label: any change to eval/heldout/* must come with a regenerated
  eval/results/heldout baseline (or CI fails, intentionally).

### Feed hard filters (gosha-pipe TODO 1): PROPOSAL, implement after item 6
Goal: the web feed shows only postings at least one of the user's searches would accept on location and level,
as Discord DMs already do (pipeline.passes_filters), ranked as today.
1. **Rule: per search, then OR.** A posting passes if, for some *active* search s, `location_ok(job, s)` and
   `matches_experience_level(job.title, s.experience_levels, job.experience_level, job.description)`. Not the
   union of all locations × union of all levels: "intern in Bucharest" + "senior in Cluj" must not admit Cluj
   internships. `location_ok` = pipeline._location_ok semantics: no locations → any; else any normalize_location
   match; plus remote when `remote_ok`.
2. **Which searches:** active ones only (`Subscription.is_active`). Exclusions (blacklist / excluded words)
   keep counting paused searches (get_user_exclusions contract: "never show me X" does not pause); location/level
   are a search's scope and pause with it. **No active search → no hard filter** (CV-only users keep today's feed).
   Salary / freshness / keywords: NOT applied (the feed is "what fits your CV", the window is already 30 days,
   keywords are what the ranking replaces). Lead to confirm.
3. **One implementation:** ask gosha-pipe to move `pipeline._location_ok` into filters as a public pure
   `filters.search_location_ok(job_location, locations, remote_ok) -> bool` used by pipeline, services/jobs browse
   and the feed (today it is private to pipeline.py, and services/jobs.py:81 has its own variant).
4. **Where in get_feed:** after the exclusions, on `candidates` only, never on `window` (the window is the
   keyword-IDF corpus shared by every user and the demo). The total and the percentiles are then of the filtered
   set (z-scores were always over the user's own candidates). The newest-first fallback (no vector) is filtered too.
5. **Cost:** location reads `job.location` (always loaded): memoise per distinct location string per request.
   The level check needs the description (unmarked titles: `_is_junior` reads required years), which the feed
   leaves deferred: cache the verdict in an LRU keyed `(*_level_key(job), frozenset(levels))`, like `_level_cache`,
   and add "level verdict not cached" to `_needs_text` (so the first request of a cycle reads each new
   description once, later ones only the page). Never cache a verdict computed from a deferred description.
   Uses the filters' own `required_years` caching if gosha-pipe adds one (their review item: 234 µs/call).
6. **Empty result:** if the filter leaves nothing, return an empty page with total 0. No silent fallback to
   unfiltered postings (that is the "irrelevant jobs" complaint). The SPA should say why; optionally an API
   `?scope=all` to see everything. Both are not my files: lead to decide and route.
7. **Tests (tests/test_recommend.py):** two searches (Bucharest+intern, Cluj+senior) → Cluj intern excluded,
   Bucharest intern and Cluj senior kept; paused search ignored for scope but its blacklist still applies;
   no active search → unfiltered; remote_ok admits "Remote"; filter does not change the window index
   (`lexical._indexes` key set = whole window); second request reads only the page's descriptions.
8. **Eval:** the offline feed eval does not model searches (personas have a CV, not searches), so this is not
   measurable on nDCG; it is a precision rule taken from the DM path. State that in docs/EVAL.md.

### Item 6 (single seniority detector): DONE (uncommitted)
- gosha-pipe reported filters stable. `gosha/seniority.py` now imports INTERN_RE, JUNIOR_RE, ENTRY_LEVEL_RE,
  MID_TITLE_RE, says_senior (not SENIOR_RE.search: "Junior IT Project Manager" stays junior), required_years
  (lru_cached in filters), JUNIOR_MAX_YEARS from gosha.filters; my own title regexes / required_years / company-age
  code are deleted. Kept ranking-only: `_ENTRY_TEXT` (description entry words), `_BOARD_LEVEL`, cv_level, `_PENALTY`.
  Order: title entry → says_senior → MID title → board label → required years (≤2 ENTRY, 3–4 MID,
  ≥SENIOR_MIN_YEARS=5 SENIOR) → description entry words → UNKNOWN. Regexes run on the raw title, as filters does.
  Dropped my local-only senior words ("coordonator", "team leader"; lead item 9: no local tweaks).
- Effect on levels: 25/460 postings change. Against the gold `level` field (intern/junior→entry):
  main 87/102 → **94/102** correct, 2 → 0 entry↔senior flips; held-out 142/148 → **138/152** (more called, fewer right).
  Most held-out losses are "exactly 2 years required": gold labels **conflict between sets** — main 6 junior / 1 mid,
  held-out 7 mid / 0 junior. Kept the lead's rule (2 = ENTRY, consistent with the DM junior filter); flagged to lead.
- nDCG@10 (`--no-feedback`): main 0.743 → **0.750**; held-out(258) 0.624 → **0.617** (noise both ways).
- Reported to gosha-pipe: required_years misses "…is strongly preferred 3-6 years of … experience" when no period
  separates the clauses (nice-to-have word bleeds into the next requirement).
- New tests/test_seniority.py (35 cases: EN+RO titles incl. Șef/Şef, Staff Accountant, Lead Generation, Working
  Student, International/Internal, Expert contabil, II; description years incl. "2 ani" = entry, company age; CV
  levels student/junior/mid/senior/unknown; penalty directions).

### Feed hard filters: DONE (uncommitted), as proposed and approved by gosha-pipe
- `recommend.SearchScope`, `load_search_scopes(user_id)` (active searches only), `in_search_scope(job, scopes, memo)`
  (per search: `filters.search_location_ok(job.location, s.locations, s.remote_ok)` and
  `matches_experience_level(...)`; then any), applied in get_feed to `candidates` (never `window`), for both the
  ranked and newest-first paths. Verdicts cached in `_scope_level_cache` (LRU, same lock/bound, keyed
  `(*_level_key, levels)`), never from a deferred description; location memoised per request.
- `_needs_text` split into `_needs_keyword_text` (every window posting: IDF corpus) and `_needs_level_text`
  (in-scope candidates only, second small load after the scope filter). Before the split, scoped-out postings
  were re-read every request (caught by test).
- Test leak fixed: `test_feed_attaches_explanations` assigned `cover_letter.CV_DIR` without monkeypatch, so user 1
  "had a CV" in every later test.
- Tests (tests/test_recommend.py): per-search-then-OR, paused search ignored but blacklist holds, no active
  search → unfiltered, remote_ok, window index unchanged + scoped-out not re-read, level verdict read once.
- Empty result = empty page, total 0 (no fallback). SPA message / `?scope=all`: not done (not my files) — lead.

### Item 7 baselines: ON HOLD (lead order: only after gosha-pipe says required_years is final)
- I regenerated before that order arrived and then **reverted**: eval/results/*.json restored to HEAD content
  (via `git show HEAD:… >`), eval/results/heldout/ removed. Provisional files (with fingerprints) are in the
  scratchpad, machine-local: /tmp/claude-1001/-home-ubuntu-gosha-agents/5ab8ad8e-f0fd-4b42-bf22-373f67c524bd/scratchpad/provisional_results/
- Provisional nDCG@10 (with feedback simulation; feed numbers equal the `--no-feedback` ones):
  main: production 0.750 (0.639–0.851), cosine 0.546, tfidf 0.626; held-out(258): production 0.617 (0.463–0.735),
  cosine 0.542, tfidf 0.515.
- To finish when pipe says final: `.venv/bin/python scripts/eval_ranking.py --models all-mpnet-base-v2 tfidf
  --write-results eval/results` and the same with `--postings eval/heldout/postings.jsonl --personas
  eval/heldout/personas.jsonl --write-results eval/results/heldout`; check eval inputs are clean vs HEAD first.
  Commit together: those baselines + eval.yml held-out step + seniority.py/recommend.py + scripts/eval_ranking.py.
  Stale, not regenerated: all-mpnet-base-v2.title.json, paraphrase-multilingual-*.json (pre-hybrid "feed"
  semantics, no fingerprints); docs/EVAL.md should label them historical or they get deleted (lead's call).
- docs/EVAL.md: held-out description + **disclosure** added (Data section and Limitations): negation rule
  from heldout-ejobs-016, the 8-word-gap rule from a second held-out posting (gosha-pipe to name); held-out
  seniority-involving numbers slightly optimistic; cosine/TF-IDF rows unaffected. Full results rewrite (TODO 7)
  after final baselines.

## Session 3 (2026-10-16, after lead commit bf5bb9f): lead decisions (a)–(c) DONE, uncommitted
Still held back by the lead until baselines: eval.yml, scripts/eval_ranking.py, tests/test_evaluation.py (in tree).
- (a) Feed scope API + SPA (web/ is mine for this change only):
  - `recommend.get_feed_page(user_id, page, per_page, scope="searches"|"all") -> FeedPage(items, total, scoped,
    hidden_by_scope)`; `get_feed()` unchanged signature (wraps it). `scoped` = scope "searches" and ≥1 active search.
  - `gosha/api/feed.py`: `?scope=searches|all` (Literal, 422 otherwise); response `FeedListOut` (new in
    gosha/api/schemas.py, extends JobListOut) adds `scoped`, `hidden_by_scope`. Tests: tests/api/test_feed.py
    (+2: scoped + hidden count + scope=all + 422; no active search → not scoped).
  - SPA: types.ts `JobList.scoped?/hidden_by_scope?`, `FeedScope`; useJobs.ts `useJobList(mode, filters, enabled,
    scope)` (query key `['jobs', mode, filters, scope]`, adds `&scope=all`); Feed.tsx: line "Matching your active
    searches · Show all", in all-mode "Showing every posting · Only my searches", empty scoped feed with hidden>0
    → "Nothing matches your searches yet / N postings are outside your active searches' locations and levels. Show all".
    Feed.test.tsx +3 tests. `npm ci` (registry only), `npx vitest run` 35/35, `npm run lint` clean, `npm run build` ok.
    package.json has no "test" script; vitest runs via npx.
- (b) eval/results/archive/: all-mpnet-base-v2.title.json, paraphrase-multilingual-mpnet-base-v2{,_seq512}.json moved
  (plain mv, not git mv) + 3-line README; docs/EVAL.md links the archive under the 2026-10-01 table.
- (c) docs/EVAL.md Limitations: "exactly 2 years" label conflict (main 6 junior/1 mid; held-out 7 mid/0 junior),
  product rule 2 = junior, no relabelling; for the human label pass.
- Held-out is now **261** postings (gosha-label): regenerate on the 261 when gosha-pipe says required_years final.
- Full suite 1283 passed, 9 skipped (2 freshness failures seen once were another agent mid-edit; 25/25 on rerun).
Waiting on: gosha-pipe "required_years final" → regenerate baselines (main + held-out 261, mpnet + tfidf) → tell lead.
Then: TODO 7 docs/EVAL.md full results rewrite with the final numbers; TODO 6 (Discord penalty eval).

## Session 4 (2026-10-16): FINAL baselines regenerated, docs/EVAL.md rewritten — ready for the lead's commit
- gosha-pipe declared required_years FINAL; baselines regenerated (with feedback) on the working tree with gosha-pipe's
  **uncommitted gosha/filters.py** (md5 recorded in my scratchpad filters_at_regen.md5, unchanged during the run) →
  **the lead must commit gosha/filters.py together with the baselines**, or CI measures different code.
- Files for that commit: eval/results/all-mpnet-base-v2.json, eval/results/tfidf.json, eval/results/heldout/
  {all-mpnet-base-v2,tfidf}.json (new), .github/workflows/eval.yml, scripts/eval_ranking.py,
  tests/test_evaluation.py, docs/EVAL.md, gosha/filters.py (gosha-pipe's).
- Both CI checks pass locally against the new baselines (`--check` exit 0, main and held-out).
- Final nDCG@10: main production 0.750 (0.639–0.851), cosine 0.546, TF-IDF 0.626; held-out (261) production
  0.604 (0.458–0.711), cosine 0.535, TF-IDF 0.508. Note: TF-IDF < cosine on held-out ("TF-IDF beats
  embeddings" did not replicate). Feedback 0.3/0.3, one round, through the hybrid: main +0.054 (CI −0.013…+0.126),
  held-out +0.066 (+0.003…+0.132); weights kept (main does not separate them). Discord main-set numbers unchanged;
  held-out at production thresholds: CV 0.43 recall 0.852 / off-target 0.293; search 0.42 recall 0.826 / 0.345.
- docs/EVAL.md: new "Results (2026-10-16): the ranking that ships" (both sets, step table, encoder sweep,
  feedback through the hybrid, Discord held-out, hard filters unmeasured); 2026-10-01 section relabelled "before
  the hybrid ranking"; "What is measured" describes the hybrid; CI paragraph covers both sets + fingerprints;
  disclosure names heldout-ejobs-016, heldout-bestjobs-066, heldout-bestjobs-021 (the last one read by me, while
  comparing seniority detectors); Limitations + "What to try next" updated (students-only personas, date-range CVs).
- README (not my file) is now wrong on ranking. Proposed replacements for the lead:
  - L37 "🎯 CV-matched feed": "Every posting is ranked against *your* CV on meaning (sentence embeddings) and on
    the skills it names (TF-IDF), with seniority taken into account: a student sees internships before Senior roles.
    No keyword setup needed. "Matches your python, react, docker" tells you why."
  - L91 "The matching engine": "...Your feed ranks recent jobs on two signals against your CV: embedding cosine and
    TF-IDF keyword overlap, each standardised over the candidate set, minus a seniority penalty
    (gosha/seniority.py, the same level detector as the Discord filter). Your 👍/👎 history nudges the CV vector
    (a Rocchio update), and the feed shows what your active searches would take on location and level (Show all
    lifts that)..."
  - L93 "How well it ranks": "[docs/EVAL.md] scores the ranking on 202 real postings × 14 sample CVs and on an
    independent held-out set (261 × 10). The shipped ranking reaches nDCG@10 0.750 on the main set, against 0.546
    for the old pure-embedding feed, and 0.604 against 0.535 on the held-out set. The held-out gain is the honest
    one: the ranking was designed on the main set. The multilingual model once planned as an upgrade is worse;
    👍/👎 feedback helps modestly. CI re-runs the eval on both sets for every ranking change."
  - L206: "recommend.py   # feed ranking: embedding + keyword + seniority, search scope"
Next (not started): TODO 6 (soft seniority penalty in Discord scores, measured), human label pass is the owner's.

## Session 5 (2026-10-16, after lead commit 5580533)
- README.md ranking text replaced (L37 feature row, L91 matching engine, L93 "How well it ranks", tree comment for
  recommend.py), lead-authorised for this change. L93 quotes main 0.750/0.546 and held-out 0.604/0.535: UPDATE these
  after the relabel regeneration below.
- WAITING for gosha-label: applying the owner's rule "2 years = junior" to both label sets. When it says done:
  1. check eval inputs are committed (`git status --short eval/`), note the posting counts;
  2. regenerate: `.venv/bin/python scripts/eval_ranking.py --models all-mpnet-base-v2 tfidf --write-results eval/results`
     and the same with `--postings eval/heldout/postings.jsonl --personas eval/heldout/personas.jsonl
     --write-results eval/results/heldout` (LD_PRELOAD as usual);
  3. both `--check` commands (main, held-out) must exit 0;
  4. docs/EVAL.md: numbers in the 2026-10-16 table, step table (add a "labels: 2 years = junior" row), feedback and
     Discord held-out lines; replace the "two sets label exactly 2 years differently" Limitation with the rule;
     README L93 numbers;
  5. tell gosha-lead.

## NEW MISSION (owner priority, 2026-10-16): Discord DM relevance — PLAN (written before implementing)
Order: finish the relabel regeneration (Session 5) first when gosha-label reports; then this.
Production DM decision today (read-only reading of gosha/pipeline.py): job_matches_subscription (fresh, blacklist,
salary, search_location_ok, excluded words, matches_experience_level) → _title_relevant_for_any_keyword
(filters.title_is_relevant per keyword) → cosine(job, blend_with_user_vector(query, CV vector)) ≥
DEFAULT_CV_THRESHOLD 0.43 (CV) / DEFAULT_THRESHOLD 0.42 (search only) → delivery sorts a user's batch by stored
relevance_score (pipeline.py:909, already best-first) and caps at MAX_DMS_PER_USER = 10.
The eval's DM path today models only the cosine threshold (no title gate, no level filter), so it is not the
production decision.
1. **Measure the real decision** (gosha/evaluation.py): new `dm_decision()` = title gate (filters.title_is_relevant
   with the persona's keyword, posting label/description) + level filter for two search configs (levels "any", and
   the student config ["intern","junior"]) + score ≥ threshold. Per persona-search: sent, precision (grade 2 and ≥1),
   recall of grade-2, off-target passed, and P@10 / nDCG@10 of the first MAX_DMS_PER_USER sent in score order
   (the batch the user reads). Bootstrap CIs over personas. Location is NOT modelled (labels ignore place). Both sets.
2. **Candidates for the DM score** (absolute scale, since the threshold is absolute and batches are small):
   a) production blended cosine; b) cosine − λ·seniority penalty (λ ∈ {0.02, 0.04} per penalty unit, CV holders
   only); c) cosine + β·TF-IDF(search keyword + CV text vs job text), β ∈ {0.1, 0.2}, IDF on the eval postings
   (in prod: a window-wide index, see cost below); d) b+c. Threshold re-picked per variant on MAIN only (max
   precision at recall ≥ production recall − 0.02, rounded to 0.01), reported on held-out. Ship a variant only if it
   beats production on main and is not worse on held-out beyond noise; otherwise report "no change" with numbers.
3. **Ordering**: delivery already orders by relevance_score; the deliverable makes relevance_score = the new DM
   score, so the batch is best-first by the same number that decided it. Measured as P@10 of the sent batch.
4. **Deliverable**: pure helper in gosha/matching.py (my file), e.g. `dm_score(cosine, keyword_similarity=None,
   reader_level=None, posting_level=None) -> float` + `DM_THRESHOLD` / `DM_CV_THRESHOLD`, with tests; then the
   exact hook for gosha-pipe via herdr (match_jobs_for_subscription: score → dm_score → threshold; store dm_score as
   relevance_score). I do not edit pipeline.py.
5. **Cost/risk to report**: TF-IDF in the bot needs an IDF corpus: the cycle's new jobs are too few, so it would
   be the window (bot process, not the API's cache: one more index build per cycle, ~1 s cold). Seniority needs
   cv_level(CV text) in the bot (CV decrypt per user per cycle). Only proposed if the measured gain pays for it.
The read-only reviewer's prod precision numbers (via the lead) get compared against the eval's when they arrive.

### DM mission: lead changes + prod facts (2026-10-16)
- Lead approved the plan with: (A) DM seniority primarily from the SUBSCRIPTION's experience_levels (penalise
  postings above the search's highest wanted level); cv_level only when the search has no level / "any"; measure
  both. (B) never decrypt CVs per cycle: if cv_level is needed, compute once at CV upload/change and store only the
  level enum; propose where (cv owner / gosha-pipe), with a test; don't edit outside my files.
- Relabel regeneration DONE: scores identical (main 0.750/0.546/0.626, held-out 0.604/0.535/0.508); 4 result files
  differ only in postings_sha256; both --check exit 0. gosha-label had edited docs/EVAL.md (two-year rule, limitation)
  — kept; I added a step-table row + reflow. Report to lead failed once ("agent_blocked"), retried.
- **Prod (read-only aggregate SELECT, READ ONLY transaction):** active+notify_discord subscriptions = 22, ALL with
  levels: ["intern"] 15, ["intern","junior"] 5, ["junior"] 2; none "any". Users with an active Discord search: 12,
  **with a CV: 0**. ⇒ today every DM is the SEARCH-ONLY path (threshold 0.42) behind an intern/junior level filter;
  the CV blend / DM_CV threshold / cv_level matter to nobody yet. Priority: the search-only path with student level
  configs ["intern"], ["intern","junior"], ["junior"].

### DM study results (prototype: scratchpad dm_study.py / dm_pick.py, machine-local; numbers copied here)
Setup: search-only path (prod has 0 CV users), production query text incl. "Experience level: …. Location: Cluj",
gate = filters.title_is_relevant(keyword) + matches_experience_level(levels); configs [intern], [intern,junior],
[junior]; target = grade 2 AND gold level the search wants (intern→intern; junior→junior/any; both→all three);
metrics per persona-search (mean): sent, precision, precision≥1, recall, off-target share of sent, batchP = precision
of the first 10 sent by score (MAX_DMS_PER_USER). [intern] has 4 (main) / 10 (held-out) targets: too few to read.
- Seniority penalty on the DM score: **zero effect** (level filter already removes above-level postings).
- TF-IDF added to the score (+0.2·tfidf): ~+0.04 precision on main, noise.
- Cause of off-target: **hub postings** (generic tech ads: "Application Support DevOps Engineer", "Software Engineer
  (AI Focus)", "Full-Stack Software Engineer – Remote", "DevOps Engineer") score ≥0.42 for every search.
- Hub correction = cosine − mean cosine to a FIXED 40-search panel (IT + non-IT, written independently of the eval
  personas; panel queries in the user's location+levels format). Leave-one-out "other users" panel works in the eval
  but would cancel itself in prod (all 22 prod searches are similar intern/junior tech searches).
- Picked on MAIN by the pre-stated rule (max precision at recall ≥ prod − 0.02): cos − 1.0·panelhub + keyword-overlap
  gate (any keyword term in the job text; = TF-IDF>0, corpus-free), threshold 0.07.
| set / config | prod 0.42: sent, prec, p≥1, recall, off, batchP | picked @0.07 | hub only @0.07 (no gate) |
|---|---|---|---|
| main intern+junior | 22.1, .188, .462, .694, .538, .314 | 10.5, .485, .704, .733, .296, .541 | 16.4, .383, .614, .766, .386, .451 |
| main junior | 20.9*, .182, .481, .697, .519, .307 (*0.43 row; 0.42: 22.6/.174/.470/.711/.530/.307) | 11.0, .437, .683, .734, .317, .510 | 17.4, .328, .576, .768, .424, .412 |
| held-out intern+junior | 42.6, .125, .361, .728, .639, .200 | 14.2, .366, .632, **.454**, .368, .447 | 24.6, .343, .625, .596, .375, .447 |
| held-out junior | 37.3, .113, .359, .648, .641, .200 | 13.6, .308, .551, **.459**, .449, .368 | 22.7, .274, .533, .568, .467, .363 |
- Held-out: precision ~×3, batchP ×2, but recall −0.27 / −0.19 → the picked variant FAILS the pre-stated
  "not worse on held-out" bar. Decomposition: the keyword-overlap gate is the fragile part (RO keywords vs EN text,
  keyword words absent from text); the hub correction carries most of the gain on both sets.
- **Not tuned on held-out**: "hub only" was not picked on main; preferring it now would be informed by held-out.
  Needs a decision + an independent check (the prod reviewer's numbers). Threshold 0.10 is the stricter option
  (main intern+junior prec .584, recall .647, off .199).
- Ordering: delivery sorts by relevance_score; with the hub score stored there, batchP above is what users get.

## Session 6 (2026-10-16, after lead commit 6808693): DM selection redone cleanly; then the lead re-ordered work
Range-rule relabel baselines committed by the lead (main 0.759/0.549/0.638, held-out 0.604/0.535/0.508).
### DM hub correction — what it is and what was chosen where
- Hub correction = for a search s and posting j: cos(q_s, j) − α · mean_k cos(p_k, j), p_k = 40 FIXED panel queries
  (matching DM_PANEL draft: IT + non-IT everyday searches, written independently of the eval personas), built in the
  search's own location + levels format. NOT the users' subscriptions, NOT the personas. Since mean_k(p_k·j) =
  mean(p)·j, it is one vector per search shape: dm_query = q − α·mean(p); per-job cost = the same single dot product
  as today. Panel cost: encode 40 short queries once per distinct (locations, levels) per process (~1–2 s CPU; prod
  has ~8 shapes), cached.
- Clean selection on the FINAL main labels (scratchpad dm_select.py; rule stated in its docstring before running):
  hub-only (no gate), α ∈ {0.5, 0.75, 1.0}, threshold grid 0.01, best mean precision at recall ≥ prod − 0.02 over
  configs [intern,junior] and [junior] → **α 0.75 @ 0.19**; "Strong match" by the old 89%-grade≥1 rule → ≥ 0.26.
  main: precision .181 → .485, recall .706 → .695, batch .311 → .510, sent 22.4 → 10.4 per search.
  held-out (reported once): [intern,junior] prec .125 → .415, recall .728 → .468, batch .200 → .460, sent 42.6 → 16.6;
  [junior] prec .113 → .408, recall .648 → .503, batch .200 → .467, sent 37.3 → 15.1.
  real DMs (third check, lead's labels, not tuned on): AUC tech Y vs N within user .693 → .939, relevant .623 → .842,
  top-10 tech share .78 → .91; at 0.19: tech sent .96 → .66, adjacent .98 → .15, non-tech 1.00 → .11, precision
  .63 → .90, 0/11 users without a tech DM; strong ≥0.26: 14 pairs, all tech.
- Process honesty: my earlier 0.07 was picked on main (pre-stated rule) BEFORE seeing the real-DM labels; but
  dropping the keyword gate (main preferred it) and the earlier 0.12 strong band WERE informed by held-out / real-DM
  data. Superseded by the clean selection above.
- CV path (0 prod users): panel correction gives no convincing gain on main (A: blend − panel ≈ wash; B wrong scale)
  → recommend leaving the CV path as is.
- Draft helper (DM_PANEL, DM_THRESHOLD, dm_query_vector, SemanticMatcher.dm_query/is_dm_match, dm_match_label)
  PARKED in scratchpad matching_dm_draft.py; gosha/matching.py restored to HEAD. Not sent to gosha-pipe.
### NEW ORDER from the lead (FEED/EVAL REVIEW, files in lead scratchpad review-feed/)
1. Rebuild the DM eval around real usage: generic prod-style keywords ("computer science internship"), real hard
   filters (location/level/title gates) BEFORE scoring, no-CV users, prod query text, metric = DM precision@10 per cycle
   per search + starved searches. 2. Decide dm_score on that eval (main → held-out → real DMs); drop TF-IDF/seniority
   unless it says otherwise. 3. Honest claims in README/EVAL (in-sample main gain; held-out +0.07 not significant;
   mostly seniority); real-data feed check; plain cosine inside the filtered pool for no-CV users if confirmed.
4. Feed dedup by title+company (gosha-scrape normalize_key). 5. Propose grade rule mid = 1 for students to gosha-label.

### Session 6b: realistic DM eval + decision proposal (lead order steps 1–2), uncommitted
- gosha/evaluation.py: CS_FAMILIES, GENERIC_DM_KEYWORDS (prod: "computer science internship", "cs entry level",
  "computer science intern"), DM_LEVEL_SETS, DmSearch(keyword, levels, family) + .query (prod build_subscription_query
  with "Cluj" and the levels), dm_searches(personas), dm_pool (title_is_relevant + matches_experience_level; location
  NOT modelled: eval postings have none), dm_relevant (CS family for generic / persona family for specific, AND gold
  level wanted: intern→intern, junior→junior/any), dm_search_metrics (sent, p@5, p@10 of the sent in score order,
  recall in pool, starved = relevant in pool but none sent), dm_query_vectors(searches, encode, hub_alpha),
  evaluate_dm_searches(...). Not yet wired into run()/CLI.
- gosha/matching.py: DM_PANEL (40 fixed searches, data), dm_panel_queries(locations, levels),
  dm_query_vector(query, panel, alpha) = q − α·mean(panel). No threshold/alpha constants yet (await decision).
- Decision driver: scratchpad dm_decide.py (rule in docstring). Lead's rule (max main generic P@10 s.t. no more starved
  + held-out recall loss ≤ 0.10) is saturated → proposed tie-break on main specific P@5 → α 0.75 @ +0.05. Sent to the
  lead? FAILED (agent_blocked) — resend.
- Production today on this eval: main generic pool 12.9/search (10.7 relevant), sent 4.0, recall .28, starved 1/9,
  empty 4/9, P@10 .96; main specific P@10 .315 P@5 .428; held-out generic recall .77 sent 18.0; held-out specific P@10
  .169 P@5 .211; real DMs tech P@10 .746 P@5 .804 starved 0/17.
Full curve (alpha thr | main generic sent recall starved empty | main specific P@10 P@5 | held-out generic recall sent,
held-out specific P@10 P@5 | real DMs tech P@10 P@5 starved):
     0.0 +0.30 |  12.1 0.98 0 3 | 0.302 0.420 | 0.96  27.7 0.157 0.207 | 0.755 0.804 0
     0.0 +0.35 |  10.8 0.89 0 3 | 0.302 0.420 | 0.91  25.1 0.160 0.207 | 0.755 0.804 0
     0.0 +0.40 |   6.7 0.40 0 3 | 0.311 0.425 | 0.81  20.4 0.167 0.211 | 0.748 0.804 0
     0.0 +0.42 |   4.0 0.28 1 4 | 0.315 0.428 | 0.77  18.0 0.169 0.211 | 0.746 0.804 0
     0.0 +0.45 |   2.0 0.12 3 6 | 0.331 0.443 | 0.58  14.6 0.169 0.218 | 0.732 0.782 1
    0.25 -0.10 |  12.9 1.00 0 3 | 0.316 0.427 | 1.00  29.1 0.185 0.267 | 0.790 0.827 0
    0.25 -0.05 |  12.9 1.00 0 3 | 0.316 0.427 | 1.00  29.1 0.185 0.267 | 0.790 0.827 0
    0.25 +0.00 |  12.9 1.00 0 3 | 0.316 0.427 | 1.00  29.1 0.185 0.267 | 0.790 0.827 0
    0.25 +0.05 |  12.9 1.00 0 3 | 0.316 0.427 | 1.00  29.1 0.185 0.267 | 0.790 0.827 0
    0.25 +0.10 |  12.9 1.00 0 3 | 0.316 0.427 | 1.00  29.1 0.185 0.267 | 0.790 0.827 0
    0.25 +0.15 |  12.9 1.00 0 3 | 0.316 0.427 | 1.00  29.1 0.185 0.267 | 0.790 0.827 0
    0.25 +0.20 |  12.7 1.00 0 3 | 0.316 0.427 | 1.00  28.9 0.186 0.267 | 0.790 0.827 0
    0.25 +0.25 |  11.4 0.90 0 3 | 0.316 0.427 | 0.95  27.2 0.189 0.267 | 0.790 0.827 0
    0.25 +0.30 |   8.2 0.56 1 4 | 0.328 0.432 | 0.87  22.6 0.196 0.271 | 0.783 0.827 0
     0.5 -0.10 |  12.9 1.00 0 3 | 0.349 0.473 | 1.00  29.1 0.189 0.293 | 0.819 0.886 0
     0.5 -0.05 |  12.9 1.00 0 3 | 0.349 0.473 | 1.00  29.1 0.189 0.293 | 0.819 0.886 0
     0.5 +0.00 |  12.9 1.00 0 3 | 0.349 0.473 | 1.00  29.1 0.189 0.293 | 0.819 0.886 0
     0.5 +0.05 |  12.9 1.00 0 3 | 0.349 0.473 | 1.00  29.1 0.189 0.293 | 0.819 0.886 0
     0.5 +0.10 |  12.9 1.00 0 3 | 0.349 0.473 | 1.00  29.1 0.189 0.293 | 0.819 0.886 0
     0.5 +0.15 |  12.6 1.00 0 3 | 0.349 0.473 | 0.97  28.4 0.194 0.295 | 0.819 0.886 0
     0.5 +0.20 |  10.7 0.77 0 3 | 0.351 0.473 | 0.91  25.0 0.199 0.298 | 0.812 0.886 0
     0.5 +0.25 |   2.2 0.12 3 6 | 0.396 0.487 | 0.64  15.8 0.243 0.316 | 0.814 0.875 1
     0.5 +0.30 |   1.0 0.04 4 7 | 0.540 0.580 | 0.27   5.3 0.422 0.474 | 0.882 0.888 2
    0.75 -0.10 |  12.9 1.00 0 3 | 0.342 0.487 | 1.00  29.1 0.202 0.307 | 0.867 0.910 0
    0.75 -0.05 |  12.9 1.00 0 3 | 0.342 0.487 | 1.00  29.1 0.202 0.307 | 0.867 0.910 0
    0.75 +0.00 |  12.9 1.00 0 3 | 0.342 0.487 | 1.00  29.1 0.202 0.307 | 0.867 0.910 0
    0.75 +0.05 |  12.9 1.00 0 3 | 0.342 0.487 | 1.00  29.1 0.206 0.308 | 0.867 0.910 0
    0.75 +0.10 |  11.7 0.86 0 3 | 0.342 0.487 | 0.94  26.4 0.211 0.311 | 0.869 0.910 0
    0.75 +0.15 |   3.9 0.21 2 5 | 0.405 0.506 | 0.69  17.0 0.310 0.366 | 0.875 0.898 1
    0.75 +0.20 |   0.7 0.02 4 7 | 0.547 0.605 | 0.18   3.7 0.432 0.462 | 0.865 0.882 2
    0.75 +0.25 |   0.0 0.00 6 9 | 0.674 0.681 | 0.01   0.2 0.578 0.597 | 1.000 1.000 12
    0.75 +0.30 |   0.0 0.00 6 9 | 0.796 0.796 | 0.00   0.0 0.534 0.578 | 1.000 1.000 14
- PANEL FIX: 6 panel entries equalled eval persona keywords ("contabil", "customer support", "data analyst", "devops
  engineer", "frontend developer", "mobile developer"; I wrote the panel after seeing persona keywords). Replaced by
  "game developer", "IT security specialist", "system administrator", "database administrator", "auditor", "bank teller";
  tests/test_matching.py TestDmPanel now asserts no overlap with persona keywords (both sets) or GENERIC_DM_KEYWORDS.
  Decision re-run (scratchpad dm_decide2.txt): unchanged choice α 0.75 @ +0.05 — main specific P@5 .487 (α .5 .467,
  .25 .420, prod .428), P@10 .339; held-out generic recall 1.00, specific P@10 .210 P@5 .308; real DMs tech P@10 .878
  P@5 .910, starved 0/17. Tests: tests/test_evaluation.py +6 DM tests, tests/test_matching.py +4 panel tests (pass).
- Wired (uncommitted): evaluation.run() returns `dm_searches.production` (plain cosine @ DEFAULT_THRESHOLD over
  dm_searches(personas)); scripts/eval_ranking.py prints "Discord DMs as users search" (P@5 with CI, P@10, recall,
  starved). Committed baselines lack the new key (check still passes; regenerate with the DM decision).
  Full suite 1519 passed, 9 skipped.
- WAITING: lead approval of α 0.75 @ +0.05 (sent twice, "agent_blocked": lead is waiting on the owner). Then: DM_ALPHA,
  DM_THRESHOLD, DM_STRONG_MATCH_AT (pick on main), SemanticMatcher.dm_query (panel mean cached per (locations, levels)),
  hub row in run(), regenerate baselines, CI gate on dm_searches, hook to gosha-pipe. Then plan steps 3–4.
- gosha-label's option 3 (score only what the product shows) = what dm_pool/dm_relevant do for DMs; tell gosha-label.

## Session 7 (2026-10-16 ~15:00 UTC): OWNER RULE "filter, don't regrade" applied to the evals — uncommitted
Lead relayed two owner decisions: (1) mid postings: filter, don't regrade — eval applies the product's own
level/location filters before scoring, reports postings left per persona, flags low-n (kept, not hidden);
(2) intern-only searches get only training positions (gosha-pipe implements it in the shared filters; my
eval must call those same functions and RE-RUN AFTER gosha-pipe's commit).

Done (gosha/evaluation.py, scripts/eval_ranking.py, tests/test_evaluation.py +9 tests; full suite 1561 passed, 9 skipped):
- `dm_pool` now calls gosha.pipeline's own `job_matches_subscription` + `_title_relevant_for_any_keyword`
  (with the search's levels) on transient gosha.models Job/Subscription objects (`as_job`, `as_subscription`).
  Before it called title_is_relevant WITHOUT levels — that understated intern+junior pools (12.9 → 34.7 on main).
- Place filter: Posting gained `location` / `location_known`; applied (Cluj, remote_ok False) only when every
  posting has a "location" key (`locations_applied`). Eval sets have NO places; prod has only 7/202 main and
  30/261 held-out eval URLs (read-only aggregate), so adding places needs live board fetches = lead's OK.
- Low-n: `is_low_n(remaining, relevant)` = < LOW_N_POSTINGS (10) or < LOW_N_RELEVANT (3); per-search rows carry
  `persona` and `low_n`; group summaries `low_n`, `no_relevant`.
- Scoped feed: `scoped_feed_pool` (gosha.recommend.in_search_scope, SearchScope(Cluj if places, False, levels)),
  `evaluate_feed_scoped` per level set, production and cosine rankings; z-scores over the remaining candidates;
  personas with 0 grade-2 left counted `no_relevant`, out of means; `run()["feed_scoped"]`.
- `feed_metrics` P@10 now over min(10, n) (unchanged for the full feed).
- eval_ranking.py prints DM table with low-n columns, scoped-feed table, low-n generic searches, and a
  per-persona "postings/relevant left" table (DM and feed, each level set, ⚑ = low-n).
- Outputs (PROVISIONAL: tree has gosha-pipe's uncommitted filters.py/pipeline.py): scratchpad filtered_main.md,
  filtered_heldout.md, dm_decide_filtered.txt.

Key numbers (provisional):
- Main has 6 intern postings, none CS: every intern-only search (generic and specific) has an empty pool; 28/42
  specific DM searches and all 14 personas' feed-intern are low-n. Held-out: 17 intern-scoped feed postings.
- Scoped feed, intern+junior, nDCG@10 production vs cosine: main .721 vs .671; held-out .579 vs .595
  (junior: .719/.668 main, .633/.605 held-out) — no clear winner on eval data.
- DM decision re-run on filtered pools (lead's rule, literally): main generic P@10 no longer saturated
  (prod .825). Rule picks α 0.25 @ +0.34: P@10 .855, recall .29→.20; held-out gen recall .636→.565 (loss .07 ≤ .10);
  held-out spec P@5 .211→.271; real DM tech P@5 .804→.830, starved 0/17. The gain comes from sending fewer
  (9.4→6.7). Old proposal α .75 @ +0.05: main gen P@10 .767 (lower than prod), recall 1.0, spec P@5 .487,
  held-out spec P@5 .308, real P@5 .910. → needs the lead's call; re-run after gosha-pipe commits.
- Session 7 update: gosha-pipe added PUBLIC `pipeline.prefilter(sub, jobs)` (what match_jobs_for_subscription
  runs before scoring; their test test_prefilter_is_what_the_match_stage_scores). dm_pool now calls it (mask by
  object identity); test monkeypatches pipeline.prefilter. Suite 1562 passed, 9 skipped. Re-run on their finished
  (still uncommitted) intern rule: every number above UNCHANGED. Still to do after the lead's commit: re-run once more,
  regenerate baselines (fingerprints unchanged, new keys feed_scoped/dm_searches), then EVAL.md text.
  Lead still owes: DM objective (rule as stated → α .25 @ .34, or P@5 → α .75 @ .05) and OK for live place fetches.

## Session 8 (2026-10-16): DM score IMPLEMENTED for the release (lead decision: rule literally, α 0.25 @ +0.34) — uncommitted, ready
- gosha/matching.py: DM_PANEL now loaded from NEW gosha/dm_panel.txt (load_dm_panel, DM_PANEL_FILE); DM_ALPHA 0.25,
  DM_THRESHOLD 0.34, DM_STRONG_MATCH_AT 0.47 (main, 89%-grade>=1 rule generalised: generic = CS family at applicable
  level, persona = grade>=1; 91% of 65/616 pairs; scratchpad dm_strong.py); dm_query_vector default alpha=DM_ALPHA;
  dm_score(); SemanticMatcher(dm_threshold=...), panel_mean (cached per (locations, levels), lock), dm_query, is_dm_match;
  dm_match_label.
- gosha-pipe applied the hook (match_jobs_for_subscription: no CV → dm_query/is_dm_match, None → plain cosine + warning;
  CV unchanged; views: dm_match_label unless user.cv_embedding). filters.py/prefilter FINAL per gosha-pipe.
- evaluation.run: dm_searches = {"dm_score" (α .25 @ .34), "cosine" (@ .42)}; eval_ranking.check gates dm_score generic
  P@10/recall, specific P@5 (tolerance) and starved (no increase); eval.yml paths + gosha/dm_panel.txt.
- Tests: TestDmScore (6) + panel file test (test_matching), CLI DM gate test (test_evaluation), test_match_cv footer on DM scale.
- Baselines regenerated on the final tree (md5 of filters.py/pipeline.py unchanged before/after): mpnet + tfidf, main + held-out.
  Feed numbers unchanged (0.759 / 0.604). Both --check exit 0. Suite 1576 passed, 9 skipped.
- EVAL.md: CI paragraph, "What is measured" (DM eval, scoped feed), new "Results (2026-10-16): Discord DMs, the DM score"
  (rule, table, real DMs, honest reading: gain small and mostly sending fewer; P@5 main generic 1.0→0.9; recall down;
  low-n), scoped-feed table with paired CIs, full-feed held-out CI (−0.015..+0.153), limitations (place, few interns,
  modelled searches), next steps. README L91/L93 rewritten.
- Post-release: places in the eval sets (board fetches, lead OK), step 4 feed dedup (normalize_key), gosha-label note.
