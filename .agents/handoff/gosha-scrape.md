# Handoff: gosha-scrape — job sourcing (scrapers, JobSpy wrapper, cross-board dedup)

Written: 2026-10-16 ~12:05 UTC · Branch: wip/agents-2026-10-16 · Base commit: 5490d6a

## Mission (one paragraph, self-contained)
Make GOSHA find the jobs that actually exist for CS students in Romania (+ remote-EU),
with accurate fields. Concretely: verify every source (eJobs, BestJobs, Hipo, RemoteOK,
Jobicy natively; Indeed, LinkedIn, Glassdoor via python-jobspy 1.3.0) against the boards'
own sites for realistic student queries, fix missed jobs (pagination, location mapping,
caps), wrong fields (salary, location, dates), and cross-board dedup false/missed merges;
recommend new high-value sources. User goal: "best job-finding platform, find jobs VERY
accurately". Live measurements were made from a university network in Cluj
(egress 193.231.20.20, AS24839 UBB), NOT the production server.

## File ownership
May edit: `gosha/scraper.py`, `gosha/scrapers/*`, `gosha/dedup.py`, `gosha/salary.py`,
`scripts/smoke_scrapers.py`, `tests/scrapers/*`, `tests/test_scraper_jobspy.py`,
`tests/test_dedup.py` (+ new test files for these).
Must NOT edit: `gosha/scrape_health.py` and `.env.example` (owned by agent **gosha-health**
since 2026-10-16), `gosha/pipeline.py`, `gosha/filters.py`, `gosha/services/*`,
`gosha/recommend.py` (other agents). Suggestions for those are listed below.
Do not git commit/push/stash/reset (the lead commits). Do not touch production or send
Discord messages.

## Done (verified)
All in the working tree, uncommitted. `pytest` on the subset below: **166 passed**;
`ruff check .`: clean.

1. **Opt-in browser-TLS fallback for 403s** — `gosha/scrapers/base.py:35-127`
   (`_browser_get`, `BrowserFallbackTransport`, `browser_fallback_enabled`, `http_client`).
   All five adapters now build their client with `http_client()` (ejobs.py:279,
   bestjobs.py:197, hipo.py:198/237, remoteok.py:105, jobicy.py:137) instead of repeating
   the `httpx.AsyncClient(...)` arguments. When `SCRAPE_BROWSER_FALLBACK=true`, a GET answered
   with 403 is retried via curl_cffi (`impersonate="chrome"`; curl_cffi 0.16.3 is already
   installed as a python-jobspy dependency and pinned in constraints.txt). A host that
   accepts the retry is remembered process-wide, so the doomed plain request is paid once.
   **Default OFF** (orchestrator review point 1). Why: from this network BestJobs answered
   403 to *every* request with Python's TLS handshake (httpx and requests, API and website)
   while curl and curl_cffi got 200 from the same IP, so BestJobs returned 0 jobs on all
   3 smoke probes. CI (GitHub/Azure IPs) still gets 200 from BestJobs with plain httpx, but
   eJobs 403s GitHub IPs (failing daily smoke since 2026-10-05, issue #11 open).
   **Risk:** this gets around the board's bot protection. Whether that is allowed under
   BestJobs' terms is the owner's call. BestJobs' robots.txt allows `User-agent: *`
   everything (see below).
   Tests: `tests/scrapers/test_http_client.py` (5 tests: retry and remember the host,
   other statuses untouched, refused retry keeps the 403, failed retry keeps the 403,
   off by default). `tests/scrapers/conftest.py` is autouse: it unsets the env var and stubs
   `_browser_get`, so no test can reach a real board.
2. **No more JobSpy "remote" searches; "europe"/"eu" go only to LinkedIn** —
   `gosha/scraper.py:113-136` (`jobspy_boards_for`), applied in `scrape_jobs_raw`
   at `gosha/scraper.py:249`. Evidence (2026-10-16, "software engineer", 30/board):
   "Remote" on Indeed/Glassdoor = indeed.com/glassdoor.com US jobs (New York, Denver,
   Chicago); on LinkedIn = Pune, New York, Singapore. **1 of 90 rows** had "remote" in its
   location. LinkedIn ignores `is_remote` in jobspy 1.3.0 (it logs "isn't supported"), and
   Indeed `is_remote=True` with `country_indeed=romania` returns 0. Those rows were still
   upserted, and the feed (`recommend.py`) has no location filter, so US jobs were ranked
   in Romanian students' feeds. "Europe": LinkedIn returns London/Dublin etc., Indeed 0.
   Tests: `tests/test_scraper_jobspy.py` (`test_jobspy_boards_for`,
   `test_remote_search_is_not_sent_to_jobspy`, `test_europe_search_only_asks_linkedin`).
   Approved by the orchestrator.
3. **Hipo: read every page** — `gosha/scrapers/hipo.py:52` `MAX_PAGES` 4 → 20.
   Live: Bucuresti IT-Software has 9 pages/332 cards, Toate-Orasele (nationwide) 12/462,
   Cluj 1/23, Iasi 1/23. The old 4-page cap kept 160, so **52% of Bucharest and 65% of
   nationwide Hipo postings were never seen**. Pages are ordered by refresh date.
   Test: `test_hipo_reads_past_the_fourth_page`. Approved.
4. **Hipo: real location instead of "Toate Orasele, Romania"** — `hipo.py:70`
   (`ALL_CITIES`), `_parse` at `hipo.py:164` now takes the URL segment (the old code compared
   `"Toate Orasele"` with `"Toate-Orasele"`, so the nationwide branch never fired, and every
   nationwide Hipo job got the location "Toate Orasele, Romania", which matches no city
   filter). `_locality` at `hipo.py:97` reads JSON-LD `jobLocation.address.addressLocality`
   ("BUCURESTI , Ilfov ," → "Bucuresti, Ilfov, Romania") from detail pages and overrides
   the card location (`hipo.py:252`). Titles, companies and hrefs are now `html.unescape`d.
   Tests: `test_hipo_all_cities_catalog_is_located_in_romania_not_toate_orasele`,
   `test_hipo_detail_names_the_real_city`. Approved.
5. **BestJobs: stop storing BestJobs' own salary estimate as the posting's salary** —
   `gosha/scrapers/bestjobs.py:149`. The list item's `estimatedSalary` is BestJobs' guess
   for postings whose detail page says `salaryConfidential: true`. Live: of 300 postings in
   3 searches, 40 had a stated salary and 36 only an estimate, so about half the
   BestJobs salaries we showed and filtered on were guesses. Stated salaries confirmed as
   monthly EUR on detail pages ("4500 - 6500 €/luna"). Test:
   `test_bestjobs_estimate_is_not_the_postings_salary`. Approved.
6. **Dedup: fold diacritics, reduce local entities to the brand** — `gosha/dedup.py:49-50,86`
   and the company lines in `normalize_key`. The ASCII separators split "București" into
   "bucure"+"ti" (escaping the city-noise list), and "Română" became "romn" next to
   another board's "Romana". "S.C. X S.R.L." and "X Romania SRL" now reduce to "x".
   Measured on 1,345 live unique rows: cross-board groups **24 → 30**, rows hidden 58 → 64
   (e.g. Genpact Indeed↔Glassdoor "Genpact" vs "Genpact Romania Srl.", E.ON eJobs↔LinkedIn
   "E.ON ENERGIE ROMÂNIA SA" vs "E.ON Energie Romania"). Tests:
   `test_normalize_key_folds_diacritics`, `test_normalize_key_reduces_local_entities_to_the_brand`.
   Approved.
7. **Dedup: same title + employer in different cities are different postings** —
   `gosha/dedup.py:107-157` (`_CITIES`, `location_cities`, `locations_compatible`,
   `_split_by_location`), used at `dedup.py:190` and for older rows at `dedup.py:235`;
   `Job.location` is now loaded in `_GROUP_COLUMNS`. Why: browse (`services/jobs.py`
   `list_jobs`) and the feed drop non-canonical rows *before* the location filter, so an Iasi
   copy grouped behind a Cluj canonical was unfindable from an Iasi search. A location that
   names no known city ("Romania", "Remote") is compatible with anything, and multi-city
   listings match each of their cities. Live example split: Vodafone "Sales Advisor -
   Vodafone Store …" in Cluj/Brașov/București. Tests: `test_locations_compatible`,
   `test_same_role_in_two_cities_is_two_postings` (incl. an Iasi browse), plus orchestrator
   point 3: `test_shared_city_or_no_city_still_groups`,
   `test_multi_city_and_remote_copies_group`. Approved.

## In progress (exact state)
- Nothing is half-applied. Every edit above is complete and tested.
- An unfinished **measurement**: LinkedIn yield beyond the 30-result cap. LinkedIn's site
  shows **106** results for "python developer" in Cluj-Napoca within 14 days
  (`f_TPR=r1209600`), and **99** for "internship software" in Romania; we fetch 30
  (`RESULTS_PER_TERM`, `gosha/scraper.py:28`). The follow-up run with `results_wanted=110`
  got a **LinkedIn 429** from this IP, so how many of the 106 are relevant is unmeasured.
  Next step: on the server, run
  `.agents/handoff/gosha-scrape-probes/` style one-off:
  `scrape_jobs(site_name=["linkedin"], search_term="python developer", location="Cluj-Napoca, Romania", results_wanted=110, hours_old=336)`
  through a tunnel, then count title-relevant rows and rows past index 30.
- The report originally requested at `/tmp/claude-1000/-data-Repos/2e9dc50a-…/scratchpad/report-scrape.md`
  was NOT written. This handoff replaces it, because /tmp does not travel.

## Remaining TODO (prioritized)
1. Owner decision on `SCRAPE_BROWSER_FALLBACK` (ToS). If production's IP is blocked by
   BestJobs, scrape health will show `bestjobs=0` streaks. Check production logs for
   "BestJobs scrape failed … 403" before deciding.
2. LinkedIn cap (see In progress). If most of the ~76 missing results are relevant, raise
   `results_wanted` for LinkedIn only (e.g. 75), and weigh the extra `fetch_description`
   requests and the 429 risk (LinkedIn 429'd this IP twice in ~10 minutes of probing).
3. "junior X" recall on Indeed/LinkedIn: Indeed ANDs terms. "junior java" in Bucharest
   (14 days) = **0**, "java" = **45** (a few of them junior, e.g. "Programator Java Junior",
   which appears with no `hours_old`). Option in `scraper.py`: also search the term without
   the seniority word ("junior", "entry level", "intern") and let `filter_dataframe` keep
   entry-level titles. This costs one extra search per term, so cap it.
4. Optional new source (NOT implemented, owner decision): SmartRecruiters Posting API
   (see Findings). Its robots.txt disallows `*`.
5. Hipo dates: JSON-LD `datePosted` is the refresh date (most cards say today, even old
   IDs such as Vodafone "Discover Graduate Programme 2027" id 271007). It is stored as
   `posted_at`. Treat it as "last refreshed", or don't overwrite an existing `posted_at` on
   upsert (that lives in pipeline.py, not ours).

## Open review points from the orchestrator
1. Browser fallback opt-in, default off, tests off, report what it does and the risk:
   **addressed** (Done #1). The `.env.example` lines were sent to gosha-health with
   `herdr agent prompt gosha-health '…'` (exact text in Environment notes). Robots.txt per
   board: **addressed** (Findings → robots.txt).
2. estimatedSalary, JobSpy remote gating, Hipo pagination + JSON-LD location, city split,
   folding: **approved and kept**.
3. Dedup tests for "Cluj-Napoca, Bucuresti, Iasi" vs "Iasi" and "Remote" vs "Cluj":
   **addressed** (`tests/test_dedup.py`: `test_shared_city_or_no_city_still_groups`,
   `test_multi_city_and_remote_copies_group`).
4. Per-board table: **addressed** below. Where the board's own count could not be
   observed, it says so.

5. **NEW (lead review at wrap-up, not yet addressed):** eJobs descriptions carry invisible
   Unicode, variation selectors and zero-width chars (e.g. "Cerințe Cloud & AWS︁︀​︂︀︀​︁︂︉​︇︉"; visible in
   eval/heldout/postings.jsonl heldout-ejobs-013/-025). Likely a watermark. They pollute embeddings,
   TF-IDF tokens, dedup keys and DM text. Strip U+FE00–FE0F, U+E0100–E01EF, U+200B–U+200D,
   U+2060, U+FEFF (and other Cf-category chars) in `scrapers/details.html_to_text` and in the
   adapters' title/company handling. Add a test with that exact string.
6. **NEW (lead):** Hipo *descriptions* still contain raw HTML entities (`&nbsp;`, `&amp;`) per
   gosha-label's held-out collection; titles are unescaped now, descriptions too? Verify and fix.
7. **NEW (lead), priority:** your coverage findings are the biggest accuracy levers. Implement them
   in this order, after the owner's go-ahead where noted:
   (a) a baseline catalog scrape (eJobs IT browse + Hipo catalog for the top cities, plus Jobicy
   remote) independent of subscriptions; coordinate with gosha-pipe (pipeline.py owner);
   (b) the LinkedIn cap measurement on the server, then raise `results_wanted` for LinkedIn only;
   (c) "junior X" fallback term for Indeed/LinkedIn;
   (d) SmartRecruiters: owner decision (robots.txt disallows `*`).

## Findings not yet acted on / recommendations

### Per-board table (live, 2026-10-16, max_age 14 days, from the Cluj university IP)
"Ours" is what the adapter or JobSpy returned for the query, before the pipeline's title
filter; "rel" is how many pass `filters.relevance_predicate`. "Site" is the board's own
number for the same query.

| Board | Query @ location | Ours (rel) | Board site shows | Missing | Field accuracy issues |
|---|---|---|---|---|---|
| eJobs | python developer @ Cluj | 93 (51) — whole IT browse | q-search 32 hits, 29 title-relevant | 3 of 29 (2 are non-IT "developer") → **1 real miss** (C# .NET Full Stack Dev, dept 106 "Automatizări") | header "N joburi" = sum of `positions`, not postings (86 vs 69 listed); site paging lists exactly our 69 IDs |
| eJobs | junior java @ București | 75 (52) | 81 hits / 58 relevant | 0 | descriptions only for ≤25 relevant/search (cache fills over cycles) |
| eJobs | internship software @ Romania | 521 (39) | 419 hits / 27 relevant | 2 (1 is "Programator CNC") | — |
| eJobs | stagiu it @ Romania | 5 (5) | 5 | 0 | keyword not seen as tech/entry → no filtering (filters.py) |
| eJobs | data analyst @ Iași | 51 (13) | 36 / 2 relevant | 1 ("Automation Engineer") | — |
| BestJobs | python developer @ Cluj | 100 (96) | client-rendered app on the same API; API says total=100, pages=1; paging params (offset/page/start/from/skip/cursor) ignored | unknown; **100 cap hit** | 49/100 are "Remote" (board pads by distance); no posted date anywhere; ~half of salaries were estimates (fixed) |
| BestJobs | junior java @ București | 24 (17) | same API | under cap | 5 remote |
| BestJobs | data analyst @ Iași | 50 (42) | same API | — | **0 in Iași, 50/50 remote**; the slug is right, BestJobs has none in Iași |
| BestJobs | software engineer @ remote | 100 (95) | same API | cap hit | — |
| Hipo | (city catalog; keyword ignored) București | 154 → now all | 9 pages / 332 cards | **was 52% missing → 0%** (fixed) | dates are refresh dates; detail (desc/date/city) only for ≤20 relevant per search |
| Hipo | Romania (Toate-Orasele) | 158 → now all | 12 pages / 462 | **was 65% → 0%** (fixed) | location was "Toate Orasele, Romania" (fixed) |
| Hipo | Cluj / Iași | 23 / 23 | 1 page / 23 | 0 | — |
| RemoteOK | software engineer @ remote | 9 (4) | global feed of 99 | n/a | mostly US-bound: 42 blank locations, plus Kansas, Boston, "Remote - US"… stored and shown in feeds |
| Jobicy | software engineer @ remote | 86 (81) | geo=europe feed | n/a | good: "Remote (Europe/UK/Portugal)" |
| Indeed | python developer @ Cluj | 9 (8) | site 403s curl; JobSpy got 9 < cap 30 | ~0 | `country_indeed=romania` correct; all have desc + date |
| Indeed | junior java @ București | 0 | — | real 0 (AND search) | see TODO 3 |
| Indeed | software engineer @ Remote | 30 → now skipped | indeed.com US | — | was US-only (fixed) |
| LinkedIn | python developer @ Cluj | 30 (30) | **106** (guest search, f_TPR 14d) | **≤72% missing** (relevance of the rest unmeasured) | desc needs `fetch_description` (on in prod) |
| LinkedIn | internship software @ Romania | 30 (19) | **99** | ≤70% | — |
| LinkedIn | software engineer @ Remote | 30 → now skipped | worldwide | — | Pune/NY/Singapore (fixed) |
| Glassdoor | python developer @ Cluj | 10 (9) | not observed | — | worked from this IP; 403 from GitHub (advisory in smoke) |

### Coverage & query plan (pipeline.py, read-only for us)
- What gets scraped = the active subscriptions' keyword × location × boards × max_age
  (`pipeline.py` run_scrape_stage). **No baseline catalog scrape**: a CV-only user's feed
  holds only what other people's subscriptions pulled in. eJobs' IT-department browse
  (≈93 Cluj, ≈246 București, ≈53 Iași, ≈520 nationwide in 14 days, 1 request per 100) and
  Hipo's catalog make a cheap baseline. Recommend a fixed baseline: tech catalog for the
  top cities + remote, independent of subscriptions.
- Feed pollution: anything upserted is ranked in every feed, which has no location
  filter (`recommend.py`). RemoteOK US jobs and BestJobs remote padding enter this way.
  Recommend a location or eligibility filter in the feed, or not storing rows that match
  no subscription location (pipeline owner).
- `filters.py` (other owner): `keyword_wants_entry_level` lacks Romanian
  "stagiu/practică/stagiar"; `keyword_wants_tech` uses substrings ("ai" matches "trainee",
  "email"; "ml" matches "html") and misses "java", "python", "qa", "it". Effects: "stagiu IT"
  is unfiltered (Hipo 153/153 kept); "junior java" uses eJobs' loose `q` instead of the
  IT-department browse.
- eJobs never serves remote searches (`is_romanian_location("remote")` is False), yet
  eJobs detail has `workModel` REMOTE/HYBRID/OFFICE. Romanian remote jobs from eJobs are
  invisible to remote subscriptions.
- `pipeline._one_per_duplicate_group` compares inactive deliveries by `normalize_key`
  only, so after the Cluj copy expires it can still block an Iasi twin. Add a location check
  there (pipeline owner).
- Canonical of a dedup group = lowest id. Prefer the member with the longest description
  or a native source (better card).

### For gosha-health (scrape_health.py / pipeline accounting)
- `run_scrape_stage` does `attempted.update(boards)` per combo. Since my change, remote
  combos make no JobSpy request, so a remote-only deployment would get false "dead source"
  alerts for indeed/linkedin/glassdoor. Count a board as attempted only if
  `gosha.scraper.jobspy_boards_for(location, boards)` includes it. Sent to gosha-health.
- CI smoke has failed daily since 2026-10-05: eJobs 403s GitHub IPs (`/cities` and
  `/jobs`). Glassdoor 403s too (advisory). Issue #11 is open.

### robots.txt (fetched 2026-10-16)
- api.ejobs.ro: none (501). www.ejobs.ro: job pages allowed; disallows `*/pagina` beyond
  page 10 and some query params. We use only api.ejobs.ro.
- api.bestjobs.eu and www.bestjobs.eu: `User-agent: *` / `Disallow:` (empty), so all
  allowed. Specific SEO bots are blocked. The 403 is TLS-fingerprint based, not robots.
- www.hipo.ro: disallows only /public/prvfiles/, /objects/, /angajator/. Our
  /locuri-de-munca/… paths are allowed.
- remoteok.com: Allow /, Crawl-delay 1. /api allowed (attribution link required; we
  store the remoteok URL).
- jobicy.com: /api/v2/remote-jobs not disallowed. Our params (count, geo, cursor) are
  allowed.
- api.smartrecruiters.com: `User-agent: * Disallow: /` (only LinkedInBot allowed
  /v1/companies/). boards-api.greenhouse.io: only /embed/ disallowed. api.lever.co: Allow /,
  Crawl-delay 1.

### New sources (researched, not implemented)
- **SmartRecruiters Posting API** (`GET https://api.smartrecruiters.com/v1/companies/{id}/postings?country=ro&limit=100&offset=`,
  detail at each posting's `ref`): no key, structured `experienceLevel`
  (internship/entry_level/…), `releasedDate`, `location.city/remote/hybrid`, and full
  `jobAd.sections`. Romania today: BoschGroup 85 (**29 internships + 7 entry-level**),
  Endava 64, Continental 39, Accesa 29, Aumovio 26 (243 total). Overlap with what we
  already scrape is partial (108/243 titles also seen in our 1,345 rows; eJobs has 73 Bosch
  postings). Highest-value student source found, but robots.txt disallows `*`. It is a
  documented public API meant for career sites, so this is the owner's call. If approved:
  one adapter with a per-company list, cached per cycle like hipo.py, `applies_to` = Romanian
  locations, level mapping internship→intern, entry_level/associate→entry.
- Greenhouse: of the tokens tried, only cresta (5 RO) has Romanian jobs. Lever: none
  found. Workable: account probes inconclusive (unknown accounts return 200 with 0). Low
  value without a curated employer list.
- eJobs RSS (`www.ejobs.ro/rss-listings.xml`): title + link only. Not useful.

## How to verify
```bash
cd /data/Repos/gosha-jobs            # machine-specific path; on the server use the repo root
.venv/bin/python -m pytest -q tests/scrapers tests/test_dedup.py tests/test_scraper_jobspy.py \
    tests/test_pipeline_sources.py tests/test_pipeline.py tests/test_salary.py   # expect 166 passed
.venv/bin/ruff check .                                                            # expect clean
.venv/bin/python scripts/smoke_scrapers.py --no-jobspy        # live native boards
SCRAPE_BROWSER_FALLBACK=true .venv/bin/python scripts/smoke_scrapers.py --no-jobspy bestjobs
```
Expected live (from the Cluj network): eJobs ~93 Cluj / ~15 java Bucharest; Hipo now
returns all pages (java @ bucharest ≈ 300+ instead of 154); BestJobs 0 without the flag
from a blocked network, 100/query with it; RemoteOK/Jobicy non-zero.
Probe scripts used for the numbers above are in `.agents/handoff/gosha-scrape-probes/*.py.txt`
(renamed so `ruff check .` in CI skips them). They hard-code `sys.path.insert(0,
"/data/Repos/gosha-jobs")` (machine-specific) and read or write `collected.json` in the
current directory (that 1,345-row dataset was in /tmp and did not travel; regenerate it with
`collect.py`, ~3 min, and it sends LinkedIn requests).

## Environment notes
- venv: `.venv` (Python 3.11, CPU torch, jobspy 1.3.0, curl_cffi 0.16.3). Do not reinstall.
- New env var: `SCRAPE_BROWSER_FALLBACK` (default off). Lines sent to gosha-health for
  `.env.example`:
  ```
  # Opt-in. When a native board (eJobs, BestJobs, Hipo, RemoteOK, Jobicy) answers 403
  # to a plain request, retry it with a Chrome TLS fingerprint (curl_cffi, already
  # installed with python-jobspy). BestJobs refuses Python TLS handshakes from some
  # networks; this gets around that bot protection, so check the board terms first.
  # SCRAPE_BROWSER_FALLBACK=false
  ```
- External services touched (read-only GETs, modest volume): api.ejobs.ro, www.ejobs.ro,
  api.bestjobs.eu, www.bestjobs.eu, www.hipo.ro, remoteok.com, jobicy.com, Indeed/LinkedIn/Glassdoor
  via JobSpy (direct, no tunnels), api.smartrecruiters.com, boards-api.greenhouse.io,
  api.lever.co, apply.workable.com. LinkedIn rate-limited (429) this IP twice.
  No production server, DB or Discord was touched.
- Module-level caches (process-wide, reset on restart): ejobs `_browse_cache` (10 min),
  hipo `_cache` (10 min), details `_cache` (24 h, 250 fetches/host/hour), remoteok (10 min),
  jobicy (3 h), and base `_browser_hosts`.

## Lead note on the server (2026-10-16)
- Production logs (read-only, last 7 days): BestJobs returns ~271–281 raw / ~52 kept per cycle, no 403s.
  The prod IP is not blocked → keep `SCRAPE_BROWSER_FALLBACK` off (recommendation to owner: never enable).
- New item from gosha-pipe's TODO 3: map JobSpy `job_level` (LinkedIn "internship"/"entry level"/
  "mid-senior level") to `experience_level` (intern/entry/mid/senior) in gosha/scraper.py, with tests.
- Then continue Remaining TODO 2 (LinkedIn cap — measure from this server, gently; it 429s) and 3.
- Run tests here with `LD_PRELOAD=$PWD/.venv/lib/python3.11/site-packages/scikit_learn.libs/libgomp-a49a47f9.so.1.0.0`.

## Server session (2026-10-16 ~12:20–12:45 UTC) — gosha-scrape
All uncommitted, in the working tree. `tests/test_scraper_jobspy.py` 49 passed; full suite
1036 passed, 9 skipped, 1 failed (`tests/test_heldout.py::test_postings_schema_and_counts`,
258 != 253: gosha-label's in-flight eval/heldout edit, not ours). `ruff check .` clean.

### Done
8. **JobSpy `job_level` → `experience_level`** (lead note item 2) — `gosha/scraper.py`
   `_JOB_LEVELS`, `experience_level_from_job_level`, applied at the end of `scrape_jobs_raw`
   (adds an `experience_level` column that `pipeline.upsert_jobs` already reads).
   internship→intern, entry level→entry, mid-senior level→**mid** (the label outranks the
   description's years in `seniority.posting_level`, so "senior" would over-penalise 2–3 yr roles),
   director/executive→senior, associate/not applicable/None/NaN→None (title rules decide).
   Prod today: all 704 LinkedIn jobs seen in 3 days have `experience_level` NULL.
   Tests: `test_experience_level_from_job_level` (11 cases),
   `test_scrape_jobs_raw_carries_linkedin_seniority_as_experience_level`,
   `test_scrape_jobs_raw_without_job_level_column`,
   `test_linkedin_internship_label_is_stored_on_the_job` (end to end through upsert_jobs).
9. **LinkedIn job-page cache (proposal; in the tree, lead asked for numbers first — revert if
   rejected)** — `gosha/scraper.py` `_cached_linkedin_details`, `install_linkedin_detail_cache`
   (called at import). Wraps python-jobspy 1.3.0's private `LinkedIn._fetch_details` with a
   process-wide 24 h cache keyed by job id; only results with a description are cached, so a 429
   (`{}`) is retried by the next term/cycle; copies in and out; pruned at 5000 entries. A test fails
   if a jobspy upgrade renames the method. Tests: `test_linkedin_detail_pages_are_fetched_once_per_day`,
   `test_failed_linkedin_detail_pages_are_retried`, `test_cached_linkedin_details_are_copies`,
   `test_linkedin_detail_cache_is_installed_on_the_pinned_jobspy`.
10. **One JobSpy call per board** (Indeed and LinkedIn concurrently via `asyncio.gather`, Glassdoor
   as before), so LinkedIn can have its own `LINKEDIN_RESULTS_PER_TERM`. **Kept at 30** (no change)
   until the lead approves raising it. Any board raising still fails the route (next proxy).
   Tests: `test_linkedin_alone_asks_for_more_results`, `test_one_failing_board_fails_the_route`,
   updated `test_scrape_single_routes_through_proxy_with_country`.

### Numbers (prod, read-only: `docker logs gosha-bot`, one SELECT with default_transaction_read_only)
- LinkedIn 429s per cycle, last 3 days: detail (job page) **110–372**, search (list page)
  **27–112** (e.g. 08:26 today: 196 detail + 57 search, of ~200 searches).
- Stored descriptions are NOT the damage: of 704 LinkedIn jobs seen in 3 days, **697 (99%)** have
  ≥200 chars, 7 empty, 0 short; of 155 first seen in 3 days, 148 have one. Reason: upsert keeps the
  old description when a re-scrape brings none (`pipeline.py:148`), and most 429'd pages are re-fetches.
- The waste: 08:26 cycle = **1,425 JobSpy rows from 130 successful searches, 343 distinct LinkedIn
  jobs** seen in that cycle → each LinkedIn job page fetched ~3–4× per cycle (same jobs across the 19
  expanded terms, and again every 4 h). The real damage is the **search-level 429s**, which cut
  LinkedIn result lists short (lost coverage), caused by IP reputation from the page volume.
- LinkedIn cap (TODO 2) measured: "python developer" @ Cluj-Napoca, 14 days, list pages only
  (fetch_description off, ~9 requests): LinkedIn served **90**; first 30 → 27 title-relevant;
  **rows 31–90 → 51 of 60 title-relevant** (incl. "On-the-job Trainee Program, Junior Developer"
  Yonder, "Junior Full-Stack Engineer" Marsh, 2 "Associate …"). No 429.
  **Rule breach to note:** this probe ran from the server IP at ~12:31, during the 12:26 prod cycle,
  before the lead's rule (lead OK first, small, spaced, never during a cycle) reached me. Prod's 429
  count for that cycle (69 detail + 4 search at 12:4x) is in line with other cycles.
- Also seen in prod logs (gosha-pipe's area): duplicate combos ("computer science internship" × cluj
  twice per cycle), garbage locations searched ('kra', 'mun', 'llocation:cluj' → 19 Glassdoor errors
  per cycle), and a keyword stored as 'computer science internship — 19 intern/junior roles'.

11. **Per-board routes (lead follow-up to e6fdc92)** — `gosha/scraper.py` `_scrape_single`: every board
   (Glassdoor included, with its city-only location) is one JobSpy call, gathered with
   `return_exceptions=True`. Rows from boards that answered are kept; only the boards that raised try the
   next route; each failure is logged (`<board> failed for … via <route>`), and boards that fail on every
   route get one ERROR. Behaviour change: Glassdoor used to be skipped after one failure; it now tries the
   next route like the others. Tests: `test_a_failing_board_is_retried_alone_on_the_next_route`
   (indeed raises on the tunnel → linkedin called once, indeed retried direct, both rows returned),
   `test_rows_of_working_boards_survive_when_a_board_fails_everywhere`. Full suite 1095 passed, 9 skipped.

12. **Invisible characters and double-escaped entities (lead review points 5 and 6)** —
   `gosha/scrapers/base.py` `_INVISIBLE_RE`/`strip_invisible` (Cf chars incl. U+200B–200F, U+2060–2064,
   U+FEFF, soft hyphen, bidi, tag chars; variation selectors U+FE00–FE0F and U+E0100–E01EF; C0/C1 controls
   except tab/newline) and `RawJob.__post_init__` (cleans title/company/location/description for every native
   adapter); `gosha/scrapers/details.py` `html_to_text` now unescapes up to twice more while entities remain
   (Hipo JSON-LD is escaped twice: "&amp;amp;", "&amp;nbsp;", "&amp;#8203;"), strips tags that only appear after
   decoding (strict tag regex, so "R&D <3" survives), then strips invisibles; `gosha/scraper.py`
   `scrape_jobs_raw` cleans JobSpy's text columns. Held-out set: entities 29 → 0 postings, invisibles 43 → 0.
   Prod (read-only, active rows): eJobs **54/64** watermarked, LinkedIn 18/1373, BestJobs 4/88, Indeed 1/19;
   Hipo 8/20 with entities. No backfill (lead, 50be546): pipeline.py:136-149 overwrites a changed
   description and drops the embedding, and the cleaned text differs from the watermarked one, so active
   rows heal (and re-embed, ~90 rows) on their next sighting after deploy; expired rows don't matter.
   Tests: `tests/scrapers/test_text_cleaning.py` (18, incl. the exact eJobs string from heldout-ejobs-013).
   Note on 11: Glassdoor's "location '…' not found" returns None in jobspy (log only, no exception), so
   unresolvable locations are NOT retried on later routes.

13. **"junior X" fallback on Indeed (TODO 3)** — `gosha/scraper.py` `_SENIORITY_WORD_RE`,
   `without_seniority_words`, `indeed_fallback_terms`, `INDEED_FALLBACK_SEARCHES = 2`, used at the end of
   `scrape_jobs_raw`: a term with a seniority word (junior, jr., entry level, intern/internship, trainee,
   graduate, new grad, stagiu/stagiar, practică, absolvent) is also searched on **Indeed only** without it,
   at most 2 extra searches per keyword, skipping stripped terms already searched; only when Indeed is in
   the boards. No pipeline change: run_scrape_stage's `filter_dataframe(df, keywords)` with the
   subscription's keyword keeps the entry-level part (junior keyword: intern/junior titles and unmarked
   ≤2-year postings; intern keyword: intern-titled only). Cost: +1 Indeed search for a 1-term keyword
   ("junior java"), +2 for "computer science internship" (19 terms) — Indeed has shown no 429s in prod logs.
   Not measured live (needs lead OK). Tests (offline): `test_without_seniority_words` (15),
   `test_indeed_fallback_terms_are_capped_and_new`,
   `test_junior_keyword_also_searches_indeed_without_the_seniority_word`,
   `test_broad_entry_keyword_adds_at_most_two_indeed_searches`,
   `test_no_fallback_without_indeed_or_without_a_seniority_word`,
   `test_fallback_rows_go_through_the_keywords_level_filter`. Full suite 1179 passed, 9 skipped.

Lead decision (e6fdc92): keep the cache, keep LinkedIn at 30; raise to 60 only after a deployed cycle
shows search-429s down (owner deploys).

### Proposed next steps (need lead OK)
a. Keep the cache (9). Expected: LinkedIn job-page requests per cycle from ~1,000+ to roughly the
   number of jobs not seen in the last 24 h (~50–150 after the first cycle).
b. Then (separately, after a cycle or two shows fewer search 429s) raise LINKEDIN_RESULTS_PER_TERM
   to 60: +3 list pages per LinkedIn search, job pages only for new jobs.
c. Optional: fetch descriptions only for title-relevant rows (fetch_description=False + our own
   page fetch for rows passing `relevance_predicate`). Bigger change, reimplements jobspy parsing.
d. Optional backoff: after a search-level 429, skip LinkedIn for the rest of the cycle's searches on
   that route (needs a hook into jobspy's log or session; not done).
e. Ask gosha-pipe to dedupe identical keyword×location combos per cycle and drop garbage locations.

### Remaining after this
TODO 3 ("junior X" fallback for Indeed): design only so far — expand_keyword gives 1 term for
"junior java" (Indeed 0) but 19 for "computer science internship"; a stripped variant for every term
would double searches. Plan: Indeed-only stripped variant, at most 2 per keyword. Not started.
Lead review points 5 and 6: done (item 12). 7a (baseline catalog, with gosha-pipe) still open.

## Design: baseline catalog scrape (review point 7a)
Status: **Tier A scraper side implemented** (lead OK, item 14 below); **Tier B NOT approved** until a deployed
cycle shows LinkedIn search-429s down. Pipeline side is gosha-pipe's (`run_scrape_stage`), specified below as
an interface only; the lead forwards the spec to gosha-pipe after its current correction is committed.
Lead decision on DMs: baseline jobs go through the normal per-subscription match like any other job (same
filters), no special path.

14. **Implemented (uncommitted):** `gosha/scraper.py` `BaselineCombo`, `baseline_enabled`
   (`SCRAPE_BASELINE`, default off), `baseline_combos(cycle)`, `BASELINE_KEYWORD = "junior developer"`,
   `BASELINE_LOCATIONS` (bucuresti, cluj, timisoara, iasi, oradea, romania, remote), `BASELINE_MAX_AGE_DAYS = 14`,
   budget constants `BASELINE_MAX_LIST_REQUESTS = 171`, `BASELINE_MAX_DETAIL_FETCHES = 410` (worst case per
   cycle with cold caches, from the adapters' page/detail caps; typical ≈55 list requests, details cached 24 h).
   Every combo has `boards=()`. Tests: `tests/test_scraper_baseline.py` (17): off by default / switch values,
   native-only shape, pure and cycle-independent, every location served by a native source, what the keyword's
   filter keeps/drops, and `test_baseline_request_budget` (recomputes the worst case from the adapters'
   constants and `applies_to`; fails on a new source, location or cap until the constants are updated).
   Full suite 1237 passed, 9 skipped; ruff clean. `.env.example` line requested from gosha-health.

### Why (prod, read-only, 2026-10-16)
- What gets scraped = active subscriptions only: **22 searches from 12 users**, keywords all one family
  ("computer science internship" 13, "… — 19 intern/junior roles" 5, "cs entry level" 2, "computer science
  intern(1)" 2); locations cluj 11, timisoara 3, romania 3, oradea 2, iasi/bucuresti/cluj-napoca/remote 1
  each (+3 that name no place). Boards: all `indeed,linkedin,glassdoor`.
- The web feed (`recommend.py`) ranks every upserted row, and `filter_dataframe` with intern keywords keeps
  intern-titled rows only. So a CV-only user ("junior python developer", or anyone in Bucharest — the largest
  market: eJobs ≈246 IT postings/14 d, Hipo 332 cards) sees almost nothing that fits.

### Interface (ours: gosha/scraper.py)
```python
@dataclass(frozen=True)
class BaselineCombo:
    keyword: str                 # what is searched
    location: str                # user spelling, as Subscription.locations ("cluj", "remote")
    boards: tuple[str, ...]      # JobSpy boards; () = native sources only, no JobSpy request
    max_age_days: int            # 14
    filter_keywords: tuple[str, ...]  # what filter_dataframe / relevance_predicate get

def baseline_combos(cycle: int) -> list[BaselineCombo]: ...
```
Pure (no I/O), deterministic in `cycle` (rotates the JobSpy part). `SCRAPE_BASELINE=false` → `[]`
(the `.env.example` line goes to gosha-health, its owner).

### Contents
**Tier A — native only, every cycle (`boards=()`):** keyword `"junior developer"` (wants entry + tech:
`title_is_relevant` keeps intern, junior and unmarked ≤2-year tech titles — one keyword covers the student
levels, expands to 1 term) × locations `bucuresti, cluj, timisoara, iasi, oradea, romania` + `remote`.
eJobs and Hipo ignore the keyword for tech searches (IT-department browse / city catalog, both cached
10 min, so a city a subscription already searched costs nothing more); BestJobs searches it; Jobicy/RemoteOK
serve `remote`.
Requests per cycle, from measured sizes: eJobs ≈13 browse pages (100/page: Bucharest 3, nationwide 6) + ≤25 detail
fetches/search, 24 h detail cache → ≤150 the first cycle, then new postings only (≈10–30); Hipo ≈26 list
pages (Buc 9, Toate-Orasele 12, others 1–2) + ≤20 details/search, cached; BestJobs 6 searches + ≤20 details
each, cached; Jobicy 0–1 (3 h cache); RemoteOK 1. **≈40 list requests per cycle, details mostly cache hits,
zero LinkedIn/Indeed/Glassdoor.** (Corrected: ≈55 list requests, worst case 171; see item 14.) All under `details.HOST_BUDGET_PER_HOUR` (250/host/hour).
**Tier B — LinkedIn rotation (after the LinkedIn-cap decision and lead OK):** 2 LinkedIn-only combos
per cycle from the eval personas' tech families (python developer, java developer, .net developer, frontend
developer, mobile developer, machine learning engineer, data analyst, embedded software engineer, devops
engineer, qa engineer) × (bucuresti, cluj) = 20 combos, full rotation in 10 cycles (≈40 h; the 14-day window
makes that enough). Searched by family keyword, filtered with `filter_keywords=("junior developer",)`.
Cost ≈ 2 × (3 list pages + new job pages, cached) ≈ 6 list + ≤40 job pages per cycle, against today's
≈130–200 searches and 110–372 job-page 429s per cycle: **<5% more LinkedIn traffic**. Indeed and Glassdoor
are left out (Indeed: 9 rows for python @ Cluj; Glassdoor has no Romanian domain).

### Pipeline side (gosha-pipe implements; spec only)
1. In `run_scrape_stage`, after `scrape_combos = _scrape_combos(all_subs)`, call
   `baseline_combos(cycle)` (`cycle`: e.g. an in-process counter, or `int(time.time() // (4 * 3600))`).
   Run it also when there are no active subscriptions (today the stage returns early).
2. Merge: a baseline combo whose (keyword, normalized place) group already exists is dropped (the
   subscription's combo wins); otherwise add `(keyword, location, boards, max_age_days) -> list(filter_keywords)`.
   Note: a baseline combo has a different keyword from the subscriptions', so it is not dropped for the
   same city; eJobs' browse and Hipo's catalog are cached only 10 min, so running each baseline combo next to
   the subscription combos for the same place (or right after them) turns those into cache hits.
3. `boards == ()` → skip `scrape_jobs_raw` for it (no JobSpy call, JobSpy boards not counted attempted);
   `_scrape_extra_sources` runs it as any combo.
4. DMs (lead decision): baseline rows go through the normal per-subscription match like any other job,
   same filters, no special path.
5. Health: baseline combos make sources attempted every cycle (steadier yields); `searches_by_source`
   changes once on rollout, so gosha-health's per-source baselines re-learn — tell gosha-health.

### Acceptance
- After one deployed cycle: rows upserted per source from baseline combos, and how many are new.
- Feed: for each eval persona, how many of the feed's top 20 are student-level and in the persona's city,
  before vs after (gosha-rank's eval harness). No live probe for this without lead OK.
