# Handoff: gosha-scrape — job sourcing (scrapers, JobSpy wrapper, cross-board dedup)

Written: 2026-10-16 ~12:05 UTC · Branch: wip/agents-2026-10-16 · Base commit: 5490d6a

## Current state (2026-10-17 ~11:10 UTC) — read this first
- Hashes after the 2026-10-17 history rewrite (attribution removed): main = **5ef26de** (deployed 10:26 UTC; our
  deferral fix, was d76c467), branch HEAD = **d2b3ab3** (lead's WIP commit with our pipeline refresh, was c3085b7).
  **Git is frozen** (owner): no commits by anyone; workers never commit; no Co-Authored-By/Claude-Session/
  "Generated with Claude Code" lines ever. Never pull/merge/rebase/reset/push.
- Uncommitted, ours, all tested (full suite 1701 passed, 9 skipped, ruff clean; no board traffic with sockets blocked):
  1. **Persistent detail caches** — `gosha/scrapers/page_store.py` (SQLite at `data/scrape_cache.sqlite3`, the prod
     volume; memory-only where `data/` is absent; `SCRAPE_CACHE_PATH` / "off"). details.py (BestJobs/Hipo/eJobs pages +
     hourly host budget) and the LinkedIn job-page cache (scraper.py) write through and read back after a restart.
     Wall-clock TTL (24 h) as before. Namespaces carry a hash of the parser's source (`code_version`), so a deploy
     that changes a parser re-reads its pages; LinkedIn's carries the jobspy version. JSON values; jobspy enums/models
     are rebuilt only from `jobspy.model`; no pickle. A broken file turns the store off (logged), never a scrape.
     Tests: tests/scrapers/test_page_store.py, tests/test_scraper_jobspy.py (restart round trip).
  2. **Stale-row refresh** (pipeline.py, gosha-pipe approved; in d2b3ab3 + a comment): `_row_fields`/`_refresh_fields`
     shared by `upsert_jobs` and `refresh_known_rows`; `_store_filtered` at both scrape call sites refreshes stored jobs
     among filter-dropped rows, without touching last_seen_at/is_active and without inserting. tests/test_pipeline_refresh.py.
  3. **Hipo JSON-LD with raw control characters** — `json.loads(strict=False)`; html_to_text now turns raw \t/\r into
     whitespace for every board. Probe 10:48 UTC (Bosch 270303): "Invalid control character at: line 32 column 97".
  4. **Hipo flattened text (audit cause A)** — Hipo's JSON-LD drops tags without separators at the source. The ad is now
     read from the page body `#anunt-content` when it has structure (≥200 chars, >1 line); JSON-LD stays the source of
     date/location/employment type and the fallback text. Fixture `tests/scrapers/fixtures/hipo_detail_body.html`
     (row 8362, captured 10:59 UTC in the prod image, approved GET). On it `required_years` goes None → 3.
- Lead review (11:30): the memory-only page_store fixture now lives in tests/conftest.py (autouse, whole suite;
  copies removed from tests/scrapers/conftest.py and tests/test_scraper_jobspy.py). A full run with
  page_store.DATA_DIR pointed at an existing directory wrote nothing there. The Hipo body text goes through
  repair_mojibake like the JSON-LD (the captured body is pure ASCII, so it cannot show the repair is never needed);
  test_hipo_body_text_is_repaired_like_the_json_ld. Suite 1702 passed, 9 skipped.
- Live requests used today (all lead-approved, prod image, outside cycles): hipo_bosch_probe 10:48:34, hipo_page_capture
  10:59:14. Next prod cycle 14:29:47 UTC (the 10:26 restart moved the schedule).
- Heal outlook under 1+2+3+4 once deployed: see "Lead roots (2026-10-17)" below.

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
5. **(Adapter side done, item 15; pipeline hook with gosha-pipe.)** Hipo dates: JSON-LD `datePosted` is the refresh date (most cards say today, even old
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

15. **Hipo refresh dates (TODO 5), adapter side** — `gosha/scrapers/base.py` `RawJob.posted_at_is_refresh`
   (default False) → `to_record()["date_posted_is_refresh"]`; `gosha/scrapers/hipo.py` sets it True whenever
   posted_at comes from JSON-LD `datePosted`. Tests: `tests/scrapers/test_details.py`
   `test_hipo_dates_are_marked_as_refresh_dates`, `test_other_boards_dates_are_posting_dates`.
   Prod (read-only, 2026-10-16): 20 active Hipo rows (42 all-time); 10 have posted_at; of those, **8 are dated
   >1 day after first_seen_at** (impossible for a real posting date), 2 within a day of first_seen_at, 4 within
   a day of last_seen_at. Freshness (`pipeline.py` `when = job.posted_at or job.first_seen_at`) then treats
   old ads as new.
   **Pipeline hook sent to gosha-pipe** (herdr, with a proposed test): in `upsert_jobs`, read
   `refreshed = row.get("date_posted_is_refresh") is True`; for an existing row with a refresh date set
   `posted_at = min(existing.posted_at, new posted_at, existing.first_seen_at)` (naive datetimes as UTC);
   other boards replace the date as before; new rows unchanged. The 8 rows heal on their next sighting.
   gosha-pipe implemented the hook (tests/test_pipeline.py::test_refresh_dates_never_move_posted_at_forward), reading
   the flag with `isinstance(v, (bool, np.bool_))`: my spec's `is True` was a bug (a pandas bool column yields
   numpy.bool_, so it was always False).
   Full suite at the time: 1241 passed, 9 skipped, 2 failed in `tests/test_filters_accuracy.py::test_required_years`
   (gosha-pipe's in-flight red tests; `filters.py` untouched by us).

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
   Full suite 1237 passed, 9 skipped; ruff clean. `.env.example`: gosha-health added the SCRAPE_BASELINE
   block (commented, default off) after SCRAPE_BROWSER_FALLBACK. Health needs no change: added searches
   never reset yield baselines (the median rises within a few cycles), and switching the baseline off
   later gives those boards the 2-cycle grace, so no false "collapsed" alert either.

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

### Pipeline side as implemented by gosha-pipe (2026-10-16)
`pipeline._with_baseline` + `run_scrape_stage`, behind SCRAPE_BASELINE (default off); runs with zero subscriptions;
`boards=()` skips JobSpy and counts no JobSpy board attempted; baseline rows go through the normal match.
`cycle = int(time.time() // (4 * 3600))`. Tests: `tests/test_pipeline_baseline.py`. Deviations from this spec
(both accepted): (1) merge key is (keyword, canonical place, max_age_days), so windows stay separate queries
(lead's rule: a 3-day search must not share a capped 14/30-day query); a sub "junior developer"/cluj/7d does not
drop baseline cluj/14d. (2) Each baseline combo is inserted right after the last subscription combo for the same
place, so the eJobs browse / Hipo catalog 10-min caches answer it.
**Before adding any rotation to `baseline_combos(cycle)` (e.g. Tier B):** tell gosha-pipe first (its TODO 11).
A set of searches that changes every cycle would keep the native boards in gosha-health's grace period
forever, so collapse detection would go blind. gosha-pipe will then keep rotating combos out of
`searches_by_source` (or gosha-health adds a separate parameter). Tier A does not rotate, so nothing to do today.
Risk (gosha-pipe's TODO 6): in regex-fallback mode generic keywords already match every job in their city;
baseline rows enlarge that pool.

### Acceptance
- After one deployed cycle: rows upserted per source from baseline combos, and how many are new.
- Feed: for each eval persona, how many of the feed's top 20 are student-level and in the persona's city,
  before vs after (gosha-rank's eval harness). No live probe for this without lead OK.

## Live-request rule (lead, 2026-10-16, corrected)
- No live board request from this server without the lead's OK, and no approvals while a prod cycle runs.
- Cycles start every 4 h at :26 UTC (00:26, 04:26, 08:26, 12:26, 16:26, 20:26) and last 35+ min. A cycle
  is running if the last `=== Scrape cycle started ===` in `docker logs --since 5h gosha-bot` (read-only)
  has no later `Scrape health:` line.
- Log facts (read-only, 2026-10-16 ~13:00): prod JobSpy uses BOTH routes, since SCRAPE_DIRECT=true adds the
  server IP as a route: last 72 h, 1,814 searches "via direct" vs 1,779 "via socks5://127.0.0.1:1080". So
  JobSpy traffic (LinkedIn included) partly comes from this server's IP; native boards use it always.
  BestJobs: no HTTP 403 in the last 120 h of logs up to the 08:26 cycle (every hour 200/307;
  `bestjobs=274→52` in that cycle's health line). The 12:26 cycle had not reached the native phase yet.
  Waiting for the lead's go to analyse the 12:26 cycle's BestJobs result from logs only.

## BestJobs 403 from this host (2026-10-16 ~13:15 UTC, offline + prod logs, no probes)
Observation: prod's 12:26 cycle got `bestjobs=274→52` (200s until ~13:05), while gosha-label's two runs from
this host's shell (~12:30, and 12:42–13:00: 10 searches) got 403 on every BestJobs search; laptop runs with the
WIP code also got 403, and so did my Cluj measurements earlier (httpx and requests; curl and curl_cffi got 200).
**Hypothesis "the WIP HTTP client changes make BestJobs refuse us": rejected on the code.**
- Prod runs `main` = 5490d6a (image gosha-bot:latest, built 2026-10-12). `git diff 5490d6a HEAD -- gosha/scrapers/`:
  with `SCRAPE_BROWSER_FALLBACK` unset, `base.http_client()` builds exactly the old client:
  `httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True, transport=None)`;
  `transport=None` is httpx's default; USER_AGENT/DEFAULT_HEADERS unchanged. bestjobs.py: only the client
  construction and the estimatedSalary line; URL/params unchanged (gosha-label's 403'd URL
  `/v1/jobs?keyword=.net+developer&location=bucharest-romania` has the same shape as prod's 200s).
- No HTTP/2, cookie or redirect change. gosha-label's `run_targeted.py` only wraps
  `httpx.AsyncHTTPTransport.handle_async_request` with a lock + 5.5 s gap (no header/TLS change), fallback off.
- Deploy inputs unchanged: no diff in constraints.txt, requirements*.txt, Dockerfile (pyproject: ruff exclude only).
  httpx 0.28.1 / httpcore 1.0.9 / h11 0.16.0 pinned on both sides.
**Same egress, different runtime.** Host and container leave by the same route (`ip route get 35.244.184.128` →
enp0s6, no Tailscale exit node; container on docker NAT, IPv6 off; BestJobs is IPv4-only, 35.244.184.128 = a
Google Cloud LB); no *_PROXY env on either side. The difference is the Python runtime and so the TLS ClientHello:
prod = Debian `python:3.11-slim` CPython 3.11.17 with the image's system OpenSSL; ours = uv's
python-build-standalone CPython 3.11.16 (`~/.local/share/uv/python/cpython-3.11.16-linux-aarch64-gnu`) with bundled
OpenSSL 3.5.9. Most likely cause: BestJobs' edge refuses this runtime's TLS fingerprint (consistent with curl/
curl_cffi succeeding where Python failed in Cluj). **Not yet confirmed.**
**Consequence:** deploying this branch should not break BestJobs (same image base, pins and request); BestJobs
numbers measured from the uv venv (agents, laptop, CI-like local runs) are not valid evidence of prod behaviour.
**Fix proposal:** no scraper change; keep SCRAPE_BROWSER_FALLBACK off. For agents' live measurements, run them in
the prod runtime: `docker run --rm -v $PWD:/w:ro -w /w gosha-bot:latest python …` (throwaway container from the
image, the running containers untouched). Optional (ours): `scripts/smoke_scrapers.py` prints the runtime
(Python build + `ssl.OPENSSL_VERSION`) next to a 403 so it is attributable.
**Confirmation (needs lead OK, outside a cycle, e.g. 14:00–16:00):** 2 requests to api.bestjobs.eu, ≥60 s apart, one
`http_client().get(API_URL, params={"keyword": "junior developer", "location": "cluj-napoca-romania"})` each (no
adapter search, so no detail fetches): (a) WIP code mounted read-only into a throwaway `gosha-bot:latest`
container; (b) the same script in the host .venv. Expected (a) 200, (b) 403. If (a) is 403 too, one more: 5490d6a
code in the same image.
**Confirmed (lead-approved, 2 of the 3 allowed GETs, 13:11:25 and 13:12:38 UTC, no cycle running).** The same script
(`.agents/handoff/gosha-scrape-probes/bj_probe.py.txt`: one `http_client().get` to
`/v1/jobs?keyword=junior+developer&location=cluj-napoca-romania`, list endpoint only, fallback asserted off), with the WIP code:
- (a) throwaway `docker run --rm --name gosha-agents-probe --network bridge` from `gosha-bot:latest`, code mounted
  read-only, no env file: CPython 3.11.17, OpenSSL 3.5.7 → **200**, HTTP/1.1, `server: nginx`, `via: Varnish, 1.1 google`, 25 items.
- (b) host `.venv`: CPython 3.11.16 (uv standalone), OpenSSL 3.5.9 → **403**, no `server`/`via` header (refused at
  Google's front end before BestJobs' nginx), body "403 Forbidden".
- (c) not needed.
Conclusion: the runtime (its TLS handshake) decides, not our code. Deploying this branch keeps BestJobs working.
Recipe for live checks in the prod image: `.agents/README.md` → "Live checks against job boards".

## DM field audit (owner priority, 2026-10-16 ~13:40 UTC; prod read-only SELECTs, no probes)
A DM (`views.build_job_embed_with_buttons`) shows: title (linked to url), company, location, salary
("min - max CUR", **no period**), the first 300 chars of the description, source. **No posted date.**
Measured on active rows (n: linkedin 1378, bestjobs 88, ejobs 64, glassdoor 57, hipo 20, indeed 19) and on the
130 jobs actually DM'd (`user_jobs.dm_status='sent'`).

| Field | Before (prod, main) | After (this branch) |
|---|---|---|
| Company 'Unknown'/empty | 0 everywhere | — |
| URL tracking/non-canonical | 0 (glassdoor `jl=`, indeed `jk=` are the ids; linkedin `/jobs/view/<id>`) | — |
| Title requisition codes | 38 of 4,553 distinct stored titles (LinkedIn, mostly trainee/junior: "Trainee Java Developer IRC293492", "ID248805 Junior QA…", "(REQ#43448)", "JobID-0296"; eJobs "(ID 593)") | **0** (replayed `strip_title_codes` over all 4,553: 38 stripped, 0 false positives) |
| Invisible chars in DM'd descriptions | eJobs **50 of 53** DMs | 0 (50be546) |
| HTML entities in DM'd descriptions | Hipo **5 of 5** DMs | 0 (50be546) |
| Vague location in DMs | Hipo 2/5 "Toate Orasele", Glassdoor 2/21 | Hipo fixed (city from JSON-LD); Glassdoor unchanged |
| Foreign-city DMs | **19 Glassdoor DMs for Munich/Kraków** (12–15 Oct) from subs located 'mun'/'kra': main's `location_matches` is a substring test | 0: branch matches whole words and `clean_location` drops 'mun'/'kra' (gosha-pipe) |
| Foreign rows in the web feed | LinkedIn: only 115 of 1,378 active rows in Romania (679 explicit USA, most "other" US cities); 1,704 user_jobs rows (0 DMs) | new ones stop (remote gating, e6fdc92 and earlier); stored ones until expiry — one-off deactivation proposed to gosha-pipe/lead |
| posted_at later than our first sighting | eJobs **29/64**, LinkedIn **270/1321** (Romanian: 11/109, avg 76 days later), Hipo 8/10; DMs: eJobs 7/53 and Hipo 3/5 dated after the DM | 0 on next sighting: all three flagged `date_posted_is_refresh`, pipeline keeps min(existing, new, first_seen_at) |
| Salary | DM'd: 1 of 130 had one. Active: eJobs 7/64 (the list has `salary` only when published; formats parse), BestJobs 22/88 EUR monthly (2 without period), LinkedIn 265 USD (US jobs) | period rendering → gosha-pipe |
| No description in DM | eJobs 3/53, LinkedIn 2/34 | — |
| No date | BestJobs 88/88 (the board publishes none), Hipo 10/20 | — |

### Done (uncommitted)
16. **Title codes** — `gosha/scrapers/base.py` `strip_title_codes` (known prefix req/requisition id/ref/job id/id/irc/jr +
    3+ digits, only at the start or end, keeps the title if nothing else would be left), applied in `RawJob.__post_init__`
    and to JobSpy titles in `scraper.scrape_jobs_raw`. Stored rows heal on next sighting (upsert rewrites title).
    Tests: `tests/scrapers/test_text_cleaning.py` (10 real cases, 9 must-not-change, RawJob, JobSpy).
17. **eJobs dates** — `gosha/scrapers/ejobs.py`: list `creationDate` is the republish time (fixture 1961022: list
    2026-06-10T08:40:00Z vs detail creationDate 2026-06-01T15:46:56Z, republishDate 2026-06-10T08:40:00+03:00), now
    `posted_at_is_refresh=True`; `_extract_detail` returns the detail's creationDate as `posted_at` (with a description),
    which replaces the list date with the flag off. The list date still drives the adapter's age cut-off (a reposted ad
    is still open). Not fixed: the list time looks like Bucharest wall-clock labelled Z (one pair of evidence; irrelevant
    once flagged). Tests: `tests/scrapers/test_ejobs_descriptions.py` (4, incl. an end-to-end search with the real fixtures).
18. **LinkedIn dates** — `gosha/scraper.py`: JobSpy rows get `date_posted_is_refresh = (site == "linkedin")`.
    Test: `test_linkedin_dates_are_marked_as_refresh_dates`.
Full suite 1374 passed, 9 skipped (8 transient failures in tests/test_search_input.py from another agent's in-flight
edit; that file passes on rerun, 29/29); ruff clean.

### Sent to gosha-pipe (DM rendering / matching, not ours)
Salary period in the DM; a "Posted" field; the 'mun'/'kra' users (did they mean München/Kraków?); one-off deactivation of
active foreign LinkedIn/Glassdoor rows for the web feed.

### Open (needs data)
- eJobs remote postings show location "Romania" (6/64 active). The fixtures have no work-model field; one detail fetch in
  the prod image (lead OK) would show whether `workModel` exists, then location could read "Remote (Romania)".

## Lead review round (2026-10-16 ~14:00): duplicate DMs, eJobs dates, empty descriptions
Source: lead's review exports (`/tmp/claude-1001/-home-ubuntu-gosha-jobs/3f714841-…/scratchpad/review-delivery/`,
user_jobs.csv + jobs.csv), prod read-only SELECTs. Uncommitted.

19. **Dedup: one posting under several employer spellings** — `gosha/dedup.py`:
    - `company_words` (used by `normalize_key`): drops legal forms, corporate-shape words (`_COMPANY_NOISE`: group,
      holding, global, solution(s), centre/center, romania, international, …), a parent brand in front
      (`_PARENT_BRANDS = {"forvia"}`) and "– A X Company"; keeps the name whole if only noise words.
    - Grouping (`_postings`): same normalized title, then employers that `same_employer` accepts (equal, or one is the
      other's first/last words, ≥5 chars: "continental"/"continental anvelope", "stefanini"/"stefanini emea"), and a
      reposter row (`scrapers.base.is_reposter`: Undelucram.ro, Hipo.ro) joins the one employer of that title that its
      description names (descriptions loaded only for reposter rows). City split as before. Older-row check uses the same rule.
    - `same_posting(title_a, company_a, title_b, company_b, text_a=, text_b=)` for gosha-pipe's expired-delivery check.
    Before → after (lead's export): the 6 split DM pairs (HELLA, DRÄXLMAIER ×2, Continental via Undelucram, Société
    Générale, AUMOVIO via Hipo.ro) **0/6 → 6/6 grouped**; 4 of them now share normalize_key (also covered by pipe's
    expired-delivery check), the other 2 group while active. Active rows (1,621): groups 202 → 207, rows hidden 295 → 308;
    every new merge checked by eye (Stefanini, Amazon/Amazon Science, Ascom/Ascom UMS, Activision/Activision Blizzard,
    Orange/Orange Services, Macrobond, Asirom via Hipo.ro — the Hipo URL names Asirom). Known pre-existing gap: US cities
    are not in `_CITIES`, so US copies in different cities group (TikTok San Jose/Seattle).
    Tests: `tests/test_dedup.py` (5 pair cases, 3 must-not-merge, noise-only name, Continental incl. other city, Hipo.ro
    named/unnamed, Banca Transilvania/Transilvania Bank, `same_posting`).
20. **Reposter → client at the source** — `gosha/scrapers/base.py` `is_reposter`, `reposter_client` ("on behalf of: X",
    bold or plain, first 500 chars); `RawJob.__post_init__`: Hipo cards "X @ Employer" from a reposter → title X, company
    Employer; other reposter rows take the client from the description. `gosha/scraper.py` does the same for JobSpy rows.
    DMs now name the employer, not the job site. Tests in `tests/scrapers/test_text_cleaning.py`.
21. **eJobs dates (lead item 2)** = item 17 above (already done before the request).
22. **Empty descriptions (lead item 3): cap NOT raised.** Of the 21 active Hipo/BestJobs rows without a description, 17
    were last seen before 2026-10-01 (before detail enrichment shipped) and are still `is_active`; among rows seen in the
    last 7 days only hipo 1, bestjobs 1, ejobs 2, linkedin 2 lack one. Detail volume is low (24 h: ~213 Hipo, ~170
    BestJobs page fetches). The lever is expiry of rows not seen for weeks (gosha-pipe): not seen 14 d but active —
    hipo 9/20, bestjobs 8/88, linkedin 19/177, ejobs 6/64, indeed 3/19.
    Observed: active LinkedIn rows went from 1,378 (~13:30) to 177 (~14:10) — someone deactivated rows in prod.
Full suite: 1442 passed, 9 skipped, 9 failed — all `TypeError: ... unexpected keyword argument 'scraped_for'` in
tests/test_alert_scenarios.py / tests/test_scrape_health.py from gosha-pipe's in-flight pipeline.py edit (not ours). Ruff on
gosha/ and tests/ clean (scripts/migrate_sqlite_to_postgres.py has 2 F821 from someone's in-flight edit).

### DM fields: state after a84ba70 and the lead's deactivation of 1,201 US LinkedIn rows (2026-10-16 ~14:20)
The DM-field audit above (items 16–18) and items 19–22 are committed in a84ba70. Remaining on active rows:
- Glassdoor **52/57 foreign** (Munich 35, Kraków 7, Freising 2), from the 'mun'/'kra' subscriptions (the 19 bad DMs).
  The branch stops new ones; the stored ones are still active → candidate for the same deactivation (lead/owner call;
  those two users may have meant München/Kraków).
- LinkedIn: 27 with an empty location (US employers in earlier samples; the match filter rejects them, feed only) and
  61 European (Paris 8, Bordeaux, Brussels, Warsaw, London, Rotterdam, Budapest…, from main's 'remote'/'europe' searches).
- Heal on next sighting (no action): reposter company names linkedin 4 / hipo 1, coded titles linkedin 1 / ejobs 1.
- eJobs "Romania" only: 6 (remote postings; needs one detail fetch in the prod image to see whether `workModel` exists).
- No foreign salary currencies left on active rows (the USD ones were the deactivated US rows).
- gosha-pipe follow-ups (2026-10-16 ~14:30): `pipeline._one_per_duplicate_group` now also calls `dedup.same_posting(...)`
  against the user's expired deliveries (test `test_a_reposted_copy_of_an_expired_dm_is_not_sent_again`); DM salary shows
  its period plus ≈RON/month, and DMs show "Posted …" / "First seen N days ago" (views.py); expiry proposed to the lead
  (14 d for ejobs/hipo full catalogs, 30 d for keyword-search boards). Full suite then: **1528 passed, 9 skipped**; ruff clean.

## BestJobs "88-90% no description" alert (2026-10-17 ~10:00 UTC; read-only + offline, fix uncommitted)
**Cause (not the adapter's parsing, not SCRAPE_BROWSER_FALLBACK, not a broken cache):**
- All 36 empty BestJobs rows (of 40 first seen in 24 h) were stored 2026-10-16 17:31–17:32 in the first cycle after
  the 03e5314 deploy; 27 are "Remote". Every one was last seen 18:26 (the cycle right after the 1fe6243 deploy) and never since.
- Both deploys restarted the bot, which empties the detail cache. `MAX_DETAIL_FETCHES = 20` per search, so in those
  cycles most "wanted" pages (titles relevant at entry level) were not fetched. On main, the scrape filter dropped unmarked
  titles for junior searches (checked offline: main keeps only "Junior Developer" of
  [".NET Software Engineer", "Frontend Developer", ..., "Junior Developer"] for 'cs entry level'). The branch's junior
  level reads the description, so with no page (no level, no description) unmarked titles pass and are stored empty.
- 22:08 cycle: the pages were fetched (e.g. `net-software-engineer-14`, `fluent-english-t3-support-engineer-eu-remote-1`
  at 22:23, 200 OK), BestJobs said Middle/years, so the filter dropped them: "Relevance filter 149→42" at 18:26 became
  "144→18" from 22:23 on (Oradea 141→26 → 141→13). Dropped rows are never upserted, so they stay active and empty.
- Logs: 1,008 api.bestjobs.eu and 116 detail GETs since 18:05, all 200 (detail via 307 /ro/ → /loc-de-munca/); no
  "Detail parse failed", no fallback use. The 4 filled rows of the 40 and every row seen since have descriptions.
- `check_data_quality` judges by first_seen_at over 24 h, so the alert clears by itself ~17:33 UTC today, and would
  come back after every deploy.
- Hipo, same mechanism: 13 of 26 rows from 17:xx are empty (7 never seen again). 6 more (all Bosch Romania; also all 5
  Continental, 2 of 7 AUMOVIO active rows) stay empty and undated although their pages were fetched (200) — a separate
  Hipo page-format question (probe `gosha-scrape-probes/hipo_bosch_probe.py.txt`).
- DMs: at 17:34 (03e5314, before 1fe6243's `lacks_level_evidence`) Hipo "AI/ML Engineer" (no description) went to 5
  users and "Working Student in Embedded…" to ~10; BestJobs 8325/8330 and Hipo 8316 user_jobs are still `pending`.
**Fix (ours, uncommitted):** `details.fetch_details(..., deferred=set)` reports URLs left over because of the per-search
limit or the host budget (not failures). BestJobs (`_without_deferred`) and Hipo drop those postings for this cycle; the
next cycle fetches the page and the filter decides with it. Failed fetches still keep the posting (the alert's job).
Tests: `test_bestjobs_postings_over_the_cap_wait_for_their_page`, `..._not_worth_a_page_are_not_held_back`,
`test_hipo_postings_over_the_cap_wait_for_their_page`, `test_fetch_details_reports_the_pages_left_for_later`; the cap test
updated.
**Also found:** `test_hipo_dates_are_marked_as_refresh_dates` (mine, 7cdb9cd) had no `@respx.mock` and fetched the real
hipo.ro listing on every suite run. Fixed. A full suite run with all sockets blocked (scratch pytest plugin) shows no other
job-board traffic; only huggingface.co lookups from model-loading tests (pass offline; not ours).
**Left for lead/owner:** the 36 BestJobs + 7 Hipo stale empty rows (active, never re-seen) — deactivate or let expiry take
them; the pending user_jobs for them (pipe: "stays pending: it may come" never resolves for these rows).

## Lead roots (2026-10-17 ~10:40–11:10 UTC)
Evidence from the cold-cache cycle 10:29–10:48 (5ef26de): deferral lines BestJobs 3/25/12, Hipo 9; BestJobs 312→28 and
Hipo 464→20 kept; **0 new rows stored empty**. Hipo kept 6 empty = the Bosch rows (parse bug, fixed in 3).
**Stale rows (36 BestJobs + 7 Hipo, first seen 2026-10-16 17:3x, last seen 18:26):** in that cycle the boards still
listed all 7 Hipo and 16 of the 36 BestJobs rows (their pages were fetched); 20 BestJobs were not fetched (deferred — 40
deferrals that cycle — or no longer listed). With (2) deployed, a listed row heals on the first cycle that has its page
(description + level filled; DMs pending for it are skipped at send). last_seen_at is not bumped, so healed rows still
age out: Hipo ~2026-10-30 (14 d catalog rule), BestJobs ~2026-11-15 (30 d). Rows no longer listed stay empty until then;
they cannot be DMed (lacks_level_evidence) but the feed may show them. After the first deployed cycle, a read-only query
lists exactly which of the 36 remain empty.
**check_data_quality:** no change proposed. Its 24 h window is by first_seen_at; after a deploy, (1) means no cold start
and the deferral means pages over a cap are never stored empty, so a deploy cannot produce the burst any more. What still
produces empty rows (failed fetches, pages that parse empty) is what it should alert on. The current alert clears when the
36 rows heal or leave the window (~17:33 UTC 2026-10-17).
