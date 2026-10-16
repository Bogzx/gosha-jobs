# Handoff: gosha-rank — web-feed ranking accuracy (hybrid + seniority), ranking eval

Written: 2026-10-16 12:05 UTC · Branch: wip/agents-2026-10-16 (working tree was on `main`, uncommitted) · Base commit: 5490d6a

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
- Nothing half-applied: every edit above is complete and tests pass except the known failure.
- **Known failing test** `tests/test_recommend.py::test_feed_reads_descriptions_only_for_the_page[True]`
  (tests/test_recommend.py:276, `assert set() == {3,4,5}`): with a CV vector, `get_feed` now loads all
  candidates' descriptions (gosha/recommend.py:479 `load_descriptions([j for j in candidates if _needs_text(j)])`).
  In this test the user has a CV *embedding* but no CV *text* (`load_cv` → None), so the keyword index is
  never used, yet `_needs_text` (recommend.py:400) still asks for text because `lexical.is_cached` is
  never true → **bug: CV-less (👍-only) users reload every description on every request.** Next step is TODO 1.

## Remaining TODO (prioritized)
1. **Fix gosha/recommend.py (review findings on the lead's code, not yet applied):**
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
