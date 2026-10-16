# Handoff: gosha-pipe — filtering, matching flow and delivery

Written: 2026-10-16 12:03 UTC · Branch: wip/agents-2026-10-16 · Base commit: 5490d6a
(Changes are uncommitted in the working tree; the lead commits them. At writing time `git branch` still showed `main`.)

## Mission (one paragraph, self-contained)
The user's goal is the most accurate job finder: given the scraped jobs, each user gets exactly the right ones, once, best first. This agent covers the hard filters (location, experience level, excluded words, salary, freshness), the match stage, Discord DM delivery, the web browse filters and the dead-link checker. Known gap at the start: the web feed (`gosha/recommend.py`, owned by the lead) applies only blacklist and excluded words, with no location or level filter. Everything is checked against real postings: `eval/postings.jsonl` has 202 labelled postings, `eval/personas.jsonl` has 14 personas.

## File ownership
May edit: gosha/pipeline.py (except the body of `_post_health_alert`, owned by gosha-health), gosha/filters.py, gosha/delivery.py, gosha/deadlinks.py, gosha/outbox.py, gosha/queue.py, gosha/feedback.py, gosha/services/jobs.py, gosha/services/subscriptions.py, their tests, and new test files.
Must not edit (read-only): gosha/matching.py, gosha/recommend.py, gosha/embeddings.py, gosha/evaluation.py, scripts/eval_ranking.py (lead); gosha/scraper.py, gosha/scrapers/* (gosha-scrape); gosha/alerts.py (gosha-health); gosha/seniority.py (gosha-rank).

## Done (verified, all tests pass)
Each fix below has a test that fails on the base-commit code (checked by loading HEAD's filters.py and pipeline.py in place of the current ones) and passes now.

**Filters (gosha/filters.py)**
- **Location matching** — `normalize_location` :316, `location_matches` :369, plus `_place`, `_place_pattern`, `_ROMANIA_ONLY_RE` and the `ROMANIAN_CITIES` tuple (about :115).
  - Matching is whole-word on folded text, with hyphens treated as spaces. The old substring test matched "Kiasi" for Iasi and missed "Targu-Mures".
  - Free text typed as "City, Country" ("Cluj-Napoca, Romania", "Bucuresti, RO") is looked up by its city. Before, it matched nothing that didn't repeat the string verbatim.
  - "romania", "europe" and "eu" now share the full Romanian city list. Europe used to miss Arad, Craiova, Oradea and others.
  - "ro" matches as a standalone word, for Indeed's "Florești, CJ, RO".
  - Remote also matches "worldwide" (RemoteOK), "telemuncă", "la distanță", "de acasă" and "home office".
  - A country-only "Romania" / "România (Remote)" posting matches any Romanian city search.
  - Aliases added for Targu Mures / Tirgu Mures.
  - Tests: tests/test_filters_accuracy.py `test_locations_*`, `test_ro_*`.
- **Excluded words** — `matches_excluded_keywords` :513. Both sides are folded (vanzari ↔ Vânzări). The boundary is `(?<!\w)…(?!\w)` instead of `\b`, so excluding "C++", "C#" or ".NET" now works (it never matched before). Phrases match across any whitespace. The company blacklist is folded too.
- **Keyword intent** — `keyword_wants_entry_level` and `keyword_wants_tech` :434 use word-start regexes.
  - "international", "internal" and "internet" no longer count as internship searches.
  - "retail", "training" and "supply chain" no longer count as AI searches: the substring "ai" had held them to tech titles, so they got 0 results.
  - Keywords naming another field ("civil engineer", "mechanical engineer") are no longer forced into CS titles.
  - Romanian entry words now count: stagiu, practică, absolvent, începător.
- **Experience levels** — `matches_experience_level` :677, `_is_junior` :650, `required_years` :622, `canonical_level` :607, `JUNIOR_RE`, `MID_TITLE_RE`.
  - The SPA's default levels are intern + junior. Under the old title-only rule, only 15 of the 112 intern/junior/any eval postings passed. Now 105/112 pass, with 6/70 senior postings let through (0 before). Unmarked titles count as junior unless the board labels them mid/senior or the posting asks for more than 2 years.
  - An explicit senior title now beats a board "entry" label (eJobs keeps the most junior level ticked).
  - SENIOR_RE now also matches "leader" and "Șef".
  - Typed aliases are accepted ("entry" → junior; before, it matched nothing).
  - `required_years` skips nice-to-have mentions ("is a plus", "constituie un avantaj") and takes the max of the rest.
- **Keyword gate** — `title_is_relevant` :449 and `relevance_predicate` take an optional `description`. A junior keyword also accepts what `_is_junior` accepts; internship keywords stay strict. `filter_dataframe` passes the description column.

**Match and deliver (gosha/pipeline.py)**
- `is_fresh_enough` :441 enforces the subscription's `max_age_days` in `job_matches_subscription`. It uses `posted_at`, else `first_seen_at`, compared by calendar day, with naive datetimes treated as UTC. Before, jobs scraped for a 30-day search were DM'd to a 3-day search.
- The match stage applies keyword-implied relevance (`_title_relevant_for_any_keyword`) before the semantic path too. Before, an intern search was DM'd senior roles that another user's search had scraped, because embeddings can't see seniority.
- `deliver_pending`:
  - Rows are sorted best-first before the same-title dedup, so the better-scoring copy wins.
  - Paused searches send nothing; the `Subscription.is_active` filter is at :781.
  - Each DM_SENT is recorded as soon as Discord accepts it (:833). Before, a crash mid-batch re-sent the DM after restart.
  - Closed-DM notices are now an owner DM through `gosha.alerts.sink_for_bot` (:864) instead of an alert-channel post with mentions. This was gosha-health's and the lead's request.
- `run_scrape_stage` :254 counts as attempted only the boards JobSpy actually asks (`gosha.scraper.jobspy_boards_for`). Before, remote searches raised false "indeed returned 0" alerts.
- Tests: tests/test_pipeline_accuracy.py (13 tests; 12 fail on old code), plus tests/test_deliver.py `test_closed_dms_are_given_up_and_reported` updated for the owner DM.

**Browse (gosha/services/jobs.py)**
- `list_jobs` scans duplicates too and collapses them after filtering (`_one_per_group` :120). Dedup groups ignore location, so an "Iasi" filter used to hide the Iasi copy behind a Cluj canonical row.
- `posted_within_days` uses `coalesce(posted_at, first_seen_at)` with a day floor.
- Junior browse shows unmarked titles, judged on title + label only (descriptions are not loaded there).
- Tests: tests/test_browse_accuracy.py.

**Subscriptions (gosha/services/subscriptions.py)**
- `_clean_levels` canonicalises experience levels and raises ValidationError on unknown ones, in both create and update.

**Dead links (gosha/deadlinks.py)**
- A dead verdict (404/410) is taken only from a GET, since some servers answer HEAD 404 for pages that are live.
- `SOFT_DEAD_MARKERS` :54: Hipo answers 200 with "Acest job nu mai este activ!" for filled jobs. Measured live on 2026-10-16: posting 271867 shows the notice, 272274 is live. These are now reported as 410.
- A malformed URL (ValueError) is inconclusive; before, it aborted the whole daily run.
- Tests: tests/test_browse_accuracy.py `test_a_head_404_*`, `test_hipo_*`, `test_a_malformed_*`.

## In progress (exact state)
Nothing half-applied. All edits are complete and the full suite passes.

## Remaining TODO (prioritized)
1. **Web feed hard filters.** `gosha/recommend.get_feed` (lead's file) applies only blacklist and excluded words: no location, no level. Ranking is pure CV cosine, and docs/EVAL.md shows it can't see seniority. Recommendation: hard-filter the feed by the union of the user's active searches' locations (plus remote_ok) and levels, using `filters.matches_experience_level(title, levels, label, description)`, or offer an "only my searches" toggle.
2. **One `required_years` / level implementation.** The lead said gosha-rank's gosha/seniority.py has its own `required_years` / `posting_level` and will be told to import from gosha/filters.py. Check that happened.
3. **JobSpy's `job_level` is dropped** (gosha/scraper.py, gosha-scrape's file). LinkedIn returns "internship" / "entry level" / "mid-senior level". Map it to `experience_level` (intern/entry/mid/senior) so the board-label path works for LinkedIn.
4. **eJobs soft-404.** Pages are 200 for expired postings, and the page carries an `expirationDate` in its Nuxt payload. A detector should use the eJobs `/jobs/{id}` API (the adapter already fetches details) rather than HTML. Also, 429s after about 8 rapid requests are fine (inconclusive), but the checks should be paced.
5. **BestJobs `www` pages answer 403 to this machine's IP**, so from here BestJobs checks are always inconclusive and waste sample budget. Either add `bestjobs` to `UNCHECKABLE_SOURCES` or check through the adapter's detail API, which already detects `expired` (gosha/scrapers/bestjobs.py). Re-test from the production server's IP first.
6. **Regex fallback path floods users** (used when the semantic model fails to load). For generic keywords ("accountant"), `title_is_relevant` returns True for every job, so a user gets every job in their city that any other search scraped. Fix: track which combo scraped each job and match only those in regex mode, or require a keyword token in the title.
7. **"Remote - US only" / "Remote (USA)"** pass a remote search. Add a negative list (us only, usa, united states, canada only) for RemoteOK.
8. **Excluded words also match descriptions.** Excluding "senior" removed 4 of the 26 intern/junior eval postings ("mentored by senior engineers"). Consider title-only matching for level-type words, or a UI hint.
9. `title_is_relevant` rejects "Junior IT Project Manager" for entry keywords, because SENIOR_RE matches "manager". Minor.
10. `gosha/delivery.py` (the channel classes) is unused by the pipeline: only tests import it. The pipeline DMs directly through `_dm_user`. Either wire it in or delete it.

## Open review points from the orchestrator
1. **Keyword gate inconsistent with `_is_junior`:** addressed. The gate now accepts unmarked titles with ≤2 years required for junior keywords; internship keywords stay strict. Tests: `test_unmarked_posting_reaches_a_junior_keyword_search` in both test_filters_accuracy.py and test_pipeline_accuracy.py. Measured over the 14 personas × eval postings, family-matched grade-2 postings passing the gate:

   | Keyword form | Grade-2 passing (old → new) | Same-family seniors passing (old → new) |
   |---|---|---|
   | plain keyword | 92/94 → 92/94 | 49/51 → 49/51 |
   | "junior <kw>" | 11/94 → 70/94 | 0/51 → 6/51 |
   | "<kw> intern" | 11/94 → 11/94 | 0/51 → 0/51 |

2. **`required_years` took the min:** addressed. Nice-to-haves are skipped and the max of the rest is taken. The exact case "5+ years … 1 year of Kafka is a plus" → 5 is tested in `test_required_years`.
3. **"ro" as a whole word:** addressed. Negative tests cover Rio de Janeiro, Porto, Providence RI, Roanoke, Rome, Rotterdam, Toronto and "Remote - US only". The only known false positive is a location containing the word "Ro-Ro"; this is documented in the test. "Remote (RO/MD)" matches romania but not cluj.
4. **Keep the freshness check, immediate DM_SENT recording, best-first dedup, paused-search waiting, Hipo marker, HEAD→GET:** kept.
5. **gosha-health requests:** both done. Closed-DM notice goes to the owner DM sink; `jobspy_boards_for` decides which boards count as attempted.

## Findings not yet acted on / recommendations
See Remaining TODO. Measurements behind them:
- Level filter on eval (intern+junior, with description): intern 6/6, junior 20/20, any 79/86, mid 4/20, senior 6/70. Old code: 6/6, 9/20, 0/86, 0/20, 0/70.
- Dead-link probe, 2026-10-16, from this dev machine:
  - eJobs: 200 for every posting tried, then 429 after about 8 requests.
  - BestJobs www: 403 for every posting tried.
  - Hipo: 200, with the notice text on expired postings.

## How to verify
- `.venv/bin/python -m pytest -q tests/test_filters_accuracy.py tests/test_pipeline_accuracy.py tests/test_browse_accuracy.py`: expected 121 passed.
- `.venv/bin/python -m pytest -q`: expected 858 passed, 9 skipped (2026-10-16 12:00 UTC).
- Measurement scripts are in `.agents/handoff/gosha-pipe-probes/`; run them from the repo root with `PYTHONPATH=.`:
  - `measure4.py`: level-filter split on the eval set.
  - `gate.py <path-to-old-filters.py>`: gate before/after. Create the old copy with `git show 5490d6a:gosha/filters.py > /tmp/filters_old.py`.
  - `probe_dead.py`: live HTTP to eJobs, BestJobs and Hipo; about 36 requests, safe to run.

## Environment notes
- venv: `/data/Repos/gosha-jobs/.venv` (machine-specific path; Python 3.11, CPU torch). Run tests with `PYTHONPATH=.` when invoking scripts directly.
- External services touched: read-only GET/HEAD requests to public posting pages on ejobs.ro, bestjobs.eu and hipo.ro (about 40 requests). No production server, no Discord.
- The original report path `/tmp/claude-1000/-data-Repos/2e9dc50a-.../scratchpad/report-pipe.md` is machine-specific; this handoff replaces it.
