# Handoff: gosha-label — independent held-out ranking test set

Written: 2026-10-16 12:00 UTC, updated on the server 2026-10-16 (see 'Server session') · Branch: wip/agents-2026-10-16 · Base commit: 5490d6a, server work on top of 39a5cd5

## Mission (one paragraph, self-contained)
GOSHA ranks job postings against a student's CV, and the user's goal is the most accurate job finder possible. The lead (orchestrator) is improving the ranking (hybrid lexical scoring, seniority handling and so on) and measures each change with the offline eval: `gosha/evaluation.py` plus `scripts/eval_ranking.py`, on `eval/postings.jsonl` (202 postings collected 2026-10-01) and `eval/personas.jsonl` (14 synthetic CVs), with the method in `docs/EVAL.md`. Tuning on that set risks overfitting. This agent's job was to build an **independent held-out test set**: fresh postings collected today with the production adapters, disjoint from the main set, labelled the same way (families, level, lang), plus 10 new personas, all in the same schema. The lead can then confirm on it that a gain measured on the main set is real.

## File ownership
- **Owned (created by this agent, all new and untracked):**
  - `eval/heldout/postings.jsonl`
  - `eval/heldout/personas.jsonl`
  - `eval/heldout/README.md`
  - `eval/heldout/collect.py`
  - `tests/test_heldout.py` (added on the server, 7 tests)
  - this handoff file
- **Must not touch:** any existing file. The instruction was to create files under `eval/heldout/` only. Other agents are concurrently editing `gosha/evaluation.py`, `gosha/scrapers/*`, `gosha/pipeline.py`, `scripts/eval_ranking.py`, tests and others (see `git status`). Do not edit them from this workstream.
- No git commit or stash: the lead commits.

## Done (verified)
- **`eval/heldout/postings.jsonl`: 253 labelled postings.**
  - Schema: the same keys as `eval/postings.jsonl` (`id, source, lang, families, level, title, company, description, url`), plus an optional `note`. The note is ignored by `evaluation.load_postings`, which reads keys explicitly.
  - IDs are `heldout-<source>-NNN`.
  - Sources: bestjobs 102, ejobs 81, hipo 49, jobicy 18, remoteok 3.
  - Language: en 178, ro 75 (29.6%).
  - Levels: intern 17, junior 36, any 61, mid 69, senior 70.
  - Every one of the 19 `FAMILIES` is present: data 36, ops 31, fs 28, ml 22, support 21, cs 21, be 20, acc 19, sales 14, fe 14, java 13, py 12, other 12, sec 11, qa 10, ba 10, dotnet 9, emb 9, mob 8. 59 postings are multi-family.
  - 0 overlap with `eval/postings.jsonl` by URL and by title+company, 0 duplicate URLs, no description over 2,000 characters, and 0 e-mail addresses left after the scrub.
  - Every label was assigned by reading the full text. 195 postings carry a `note` with the level evidence or the ambiguity. 5 postings are labelled from the title only, because their 2,000 characters are pure company boilerplate (note "description is company boilerplate"): Booking Holdings, ING Hubs, Stripe.
  - Status: loads, verified with the command below.
- **`eval/heldout/personas.jsonl`: 10 new personas.**
  - en: h-ml, h-ops, h-fe, h-mob, h-emb, h-dotnet, h-java.
  - ro: h-data-ro, h-qa-ro, h-cs-ro.
  - h-cs-ro is the non-IT control. All families have an `ADJACENT` rule.
  - The CVs are 1,596–2,034 characters, with sections, and invented people.
  - Grade-2 / grade-1 postings per persona: ml 15/32, data 23/34, ops 17/54, qa 6/4, fe 9/22, mob 5/24, emb 6/3, dotnet 4/36, java 7/34, cs 20/27. All are ≥ 4 grade-2.
  - The main set's 14 personas also have ≥ 4 grade-2 postings on the held-out postings: py 5, java 7, dotnet 4, fe 9, mob 5, ml 15, data 23, emb 6, ops 17, qa 6, acc 16, cs 20.
- **`eval/heldout/README.md`** holds:
  - the date, sources and all 56 (keyword, location) searches;
  - the selection rules and what was dropped, and why;
  - the exact level and lang rules;
  - counts per family, level, language and source, and per persona;
  - the limitations.
- **`eval/heldout/collect.py`** is the collector used. It is a copy of `scripts/eval_collect.py` with (keyword, location) pairs and dedupe against the main set. The repo path is now relative, so it is not machine-specific.
- **Sanity eval:** the TF-IDF baseline on the held-out set (held-out personas) gives feed nDCG@10 **0.513** (95% CI 0.384–0.628), R-prec 0.311, MRR 0.565, P@10 0.300.
- **Tests:** `.venv/bin/python -m pytest -q tests/test_evaluation.py` gives **25 passed** against the current working-tree `gosha/evaluation.py`, which other agents have modified. This agent has no test files of its own.

## In progress (exact state)
- Nothing half-done. All files are written and consistent.
- The working files are **not in the repo and will not transfer**. They lived under the machine-specific scratchpad `/tmp/claude-1000/-data-Repos-gosha-jobs/80b2e745-.../scratchpad/`:
  - `cand.jsonl`, the 675-posting unlabelled pool;
  - `labels*.txt`, the per-index labels;
  - `collect.log`.
- They are not needed, because the final labels are inside `postings.jsonl`. To regenerate a pool, run `collect.py`; the result will differ, since the boards change daily.

## Remaining TODO (prioritized)
1. **A human pass over the labels** (same caveat as the main set). Start with:
   - postings whose `note` contains "boilerplate", "years truncated", "years not in text" or "board files it";
   - the edge-case families: PLC/SCADA as `emb` (posting 78) or `other` (31 and 47 were dropped), data-entry operator as `other`, low-code Copilot Studio as `ml`.
2. **Run the lead's current ranking on the held-out set** and compare with the main set. Note that `scripts/eval_ranking.py` takes no path arguments in the base commit. Either add `--postings/--personas` flags (owned by whoever owns `scripts/eval_ranking.py`) or call `evaluation.run(...)` directly, as in "How to verify".
3. **Thin families:** dotnet (4 grade-2), mob (5), qa and emb (6) give noisy per-persona numbers. Adding a few more entry/mid `.NET`, Android/Flutter and QA postings in a later collection would help. Rerun `collect.py`, then keep only postings whose URL is not in either set.
4. Optionally mention the held-out set in `docs/EVAL.md` (owned by the lead or whoever edits the docs).

## Open review points from the orchestrator
The orchestrator sent no review points before the wrap-up. The task's requirements and their status:
- **(1) Fresh collection, broader queries, entry-level and city variety, Jobicy/RemoteOK, not already in the main set:** done. 56 searches; 0 overlap.
- **(2) ~250 postings, all families, realistic seniority mix, ~25–30% Romanian, scrubbed, capped at 2,000 characters:** done. 253 postings, 29.6% Romanian.
- **(3) Labels from the full text, strict rules, `note` when ambiguous, same schema:** done.
- **(4) 10 new personas:**
  - Done: 3 in Romanian; ml, data, ops, qa, fe, mob, emb, dotnet and java; non-IT control cs; all with `ADJACENT` rules.
  - Deviation: the CVs are 1,596–2,034 characters, inside the asked 1,500–3,000 but at its low end.
- **(5) Loads, and each persona has ≥ 4 grade-2 postings:** done (minimum 4, dotnet).
- **(6) README:** done.
- **Wrap-up order** (stop, tests, handoff, no commit): done.

## Findings not yet acted on / recommendations
- **Board seniority metadata is unreliable.** eJobs/BestJobs/Jobicy `experience_level` contradicted the text several times:
  - "Senior Platform Engineer" (Aumovio) was filed as `entry`;
  - "Junior Data Scientist" (Bosch) as `mid`;
  - "Technical Analyst I" (Varicent) as `senior`;
  - "Senior Data Engineer" (Evoke) as `mid`.

  `gosha/scrapers/base.py` `EXPERIENCE_LEVELS` feeds `filters.matches_experience_level`. Any seniority feature should trust the title and the stated years over the board field. The held-out labels ignore the board field.
- **Boilerplate-only descriptions.** Hipo descriptions for Booking Holdings, ING Hubs, BMW TechWorks, Schaeffler and Vodafone often spend most or all of the 2,000 characters (and `build_job_text`'s 1,500) on company text. The ranker cannot see the role in those cases. Stripping recurring company paragraphs would help; `docs/EVAL.md` already suggests it.
- **Hipo text now contains raw HTML entities** (`&nbsp;`, `&amp;`), which the 2026-10-01 main set did not. Either the Hipo adapter changed or the boards did. Unescaping in the adapter would clean up the embeddings.
- **Remote boards are thin.** Over 8 remote searches, RemoteOK returned about 31 postings and Jobicy about 73. Most were senior and many off-target (sales, customer success).

## How to verify
```bash
cd <repo root>
.venv/bin/python -c "from gosha import evaluation as e; from pathlib import Path; p=e.load_postings(Path('eval/heldout/postings.jsonl')); u=e.load_personas(Path('eval/heldout/personas.jsonl')); print(len(p),len(u)); [print(x.id, sum(e.grade(x.family,q)==2 for q in p)) for x in u]"
# expect: 253 10, then h-ml 15, h-data-ro 23, h-ops 17, h-qa-ro 6, h-fe 9, h-mob 5, h-emb 6, h-dotnet 4, h-java 7, h-cs-ro 20

# TF-IDF sanity run (no torch needed, about 10 s):
.venv/bin/python -c "
from gosha import evaluation as e; from pathlib import Path
p=e.load_postings(Path('eval/heldout/postings.jsonl')); u=e.load_personas(Path('eval/heldout/personas.jsonl'))
r=e.run(p,u,e.tfidf_encoder([e.job_text(x) for x in p]+[x.cv for x in u]),with_feedback=False)
print(r['feed']['mean'], r['feed']['ndcg@10_ci95'])"
# expect ndcg@10 ≈ 0.513 (0.384–0.628) with the base-commit job text and evaluation code; it will move if other agents change build_job_text/embed_long_text

.venv/bin/python -m pytest -q tests/test_evaluation.py   # 25 passed
```
For an embedding model, pass the encoder that `scripts/eval_ranking.py` builds, as `encode`, to `evaluation.run(p, u, encode)`.

## Environment notes
- venv: `<repo>/.venv` (used, not modified).
- No env vars needed.
- External services touched: one collection run on 2026-10-16 against ejobs.ro, bestjobs.eu, hipo.ro, remoteok.com and jobicy.com, from this workstation's IP, with 6 detail fetches per search. JobSpy was not used.
- Machine-specific: the scratchpad under `/tmp/claude-1000/...` (not transferred, not needed). No other absolute paths remain in repo files.

## Lead note on the server (2026-10-16)
CI runs `ruff check .`; eval/heldout/collect.py has 12 findings (E401/E402/E702). Reformat it so
`.venv/bin/ruff check eval/heldout` is clean (no behaviour change). Then continue with TODO 1 and 3.
TODO 2 is now done by gosha-rank (held-out run: production 0.625 vs cosine 0.543).

## Server session (2026-10-16), all done, uncommitted unless noted
**Hard rule from the lead:** this server's IP IS production's scraping IP. Make **no live requests to job boards** (do not run `collect.py`, `scripts/eval_collect.py` or any adapter without mocks) without asking gosha-lead first.

1. **Lead correction (ruff), done; the lead committed it as 39a5cd5.**
   - `eval/heldout/collect.py` is reformatted, and `ruff check eval/heldout` is clean.
   - The behaviour is unchanged: the AST matches apart from the split imports, `# noqa: E402` and the new `if __name__ == "__main__"` guard, and `scrub()` gives identical output on 203 samples.
   - The README count is fixed (57 searches, not 56; the remote group has 8).
   - The new `tests/test_heldout.py` checks: lint is clean; importing the collector does not scrape (it patches `asyncio.run` to raise); `scrub`; schema and counts; disjoint from the main set; ≥ 4 grade-2 per persona, for both persona sets.
   - **Incident:** to prove the test caught the old file, I imported the OLD `collect.py`. It ran a full 57-search collection from this server's IP (about 2 minutes, 5 boards, no JobSpy, no Discord, deployment untouched). The lead was told. The output, 504 candidates, is kept outside the repo in the session scratchpad: `/tmp/claude-1001/-home-ubuntu-gosha-agents/bd50c079-.../scratchpad/cand_2026-10-16_server.jsonl`. It is machine- and session-specific and not needed again.
2. **TODO 1 (label review), done as a second AI pass. A person's pass is still open.**
   - Re-read in full: the 21 flagged postings, plus every senior/non-senior conflict between the label and the title or stated years. That boundary is the only level boundary that changes a grade.
   - Labels changed: 0.
   - Added: evidence notes on `heldout-bestjobs-016` and `-030` (senior in the text).
   - Written down: the rule that "lead" or "architecture decisions" in the duties, without a senior title or 5+ years, is not senior. The main set does the same (its QA posting 111 is `mid`).
   - Closest calls, left `mid`: `heldout-bestjobs-031` (Android) and `heldout-bestjobs-084` (PMO). They are recorded in the README's "Review pass" section.
3. **TODO 3 (thin families), partly done; the pool cannot fill them.**
   - The 504 pool has 359 postings new to both sets, with 0 `.NET` and 0 usable mobile postings.
   - Added 5 postings, so the set is now **258**: `heldout-hipo-050` (qa, `any`, the only new grade-2), `heldout-hipo-051` (qa+emb, senior), `heldout-jobicy-019` (qa, senior), `heldout-ejobs-082` (emb+ml, senior), and `heldout-ejobs-083` (`other`, a hardware "QA" technician as a lexical trap).
   - Grade-2 counts now: h-qa-ro 7 (was 6); dotnet 4, mob 5 and emb 6 are unchanged.
   - README and test counts are updated.
   - TF-IDF with the current code: 0.501 on the old 253 and 0.510 (0.375–0.631) on the 258.
   - **gosha-rank's held-out numbers (0.625 / 0.543) were on the 253 and need a rerun.**
   - Filling dotnet/mob/emb needs a new targeted collection (for example "junior .net", "c# developer" in other cities, "flutter", "ios developer", "android junior", "embedded junior", "qa junior"), from a non-production IP or with the lead's OK.

4. **Targeted dotnet/mob collection (approved by the lead), done; 3 postings added, set now 261.**
   - Ran 12:42:12–13:00:28 UTC: 10 searches, ejobs, bestjobs and hipo only, 200 requests at least 5.5 s apart, browser fallback off.
     - The pacing was enforced by `scratchpad/targeted/run_targeted.py` (a lock on httpx's transport) and checked in the per-request log.
     - Scratchpad files (not in the repo, not needed again): `/tmp/claude-1001/-home-ubuntu-gosha-agents/bd50c079-.../scratchpad/targeted/`, holding `cand_targeted.jsonl`, `requests.log` and `collect.stderr`.
   - **The run overlapped the prod cycle that started at 12:26.** The lead's original check (`grep -c 'jobs for'`) cannot see the JobSpy phase. Prod made no native-board request during my run, and the lead was told.
   - **BestJobs answered 403 to all 10 searches.** **Correction (lead and gosha-scrape, 2026-10-16): this was not an IP block.** It was the TLS fingerprint of this host's `.venv` (uv's standalone CPython 3.11.16 with OpenSSL 3.5.9).
     - The prod image (Debian python:3.11-slim, OpenSSL 3.5.7) gets 200 from the same IP with the same code.
     - So the 0 BestJobs results in both of my runs say nothing about prod.
     - Future live checks run in a throwaway prod-image container (see `.agents/README.md`, "Live checks against job boards"), and only with the lead's OK.
   - Of 57 candidates, 36 were new to both sets. Most were off-target, because the boards match keywords loosely ("c#" returns truck drivers).
   - Added:
     - `heldout-ejobs-084`: dotnet, senior.
     - `heldout-ejobs-085`: dotnet+fs, `any`; the new .NET grade-2.
     - `heldout-ejobs-086`: qa, `mid`, "2+ years". Add it to the owner's "2 years" question below.
   - Labelled with the same rules and without looking at any ranker output.
   - Grade-2 counts now: h-dotnet 5 (was 4), h-qa-ro 8; mob is still 5 (no usable mobile posting came back).
   - TF-IDF with the current code on the 261: nDCG@10 0.501 (0.370–0.614).
   - **gosha-rank's held-out numbers need a rerun on the 261** (I told them about the 258).

### Prod timing and rules (from the lead, 2026-10-16)
- Prod cycles start every 4 h at :26 UTC (04:26, 08:26, 12:26, 16:26 …) and last 35+ minutes.
- A cycle is running if the last `=== Scrape cycle started ===` in `docker logs --since 5h gosha-bot` (read-only) has no later `Scrape health:` line.
- Prod's JobSpy goes through a SOCKS proxy; the native boards use this server's IP.
- No live board requests without the lead's OK, and no approvals while a cycle runs.

### Status: DONE
This agent's work is complete. Nothing is half-done, and every change is in `eval/heldout/`, `tests/test_heldout.py` and this file.

**Resume only if the owner's human label review comes back.** Then:
- apply the owner's decisions (including the "exactly 2 years" rule below) to the held-out set, in one pass, without looking at ranker scores;
- update the README counts and `tests/test_heldout.py`;
- tell gosha-rank to rerun.

The thin families (mob 5, dotnet 5, emb 6) stay thin. The boards have few such postings, and my two runs got no BestJobs postings because of the venv's TLS fingerprint. A collection run in the prod image might add some BestJobs postings; that is the lead's call.

Optional, not owned by me: mention the held-out set in `docs/EVAL.md`.

### How to verify (current)
- Load check: **`261 10`**. Grade-2 counts: h-ml 15, h-data-ro 23, h-ops 17, h-qa-ro 8, h-fe 9, h-mob 5, h-emb 6, h-dotnet 5, h-java 7, h-cs-ro 20.
- `.venv/bin/python -m pytest -q tests/test_heldout.py` gives 11 passed (rechecked after the release, main = 1fe6243, 2026-10-16).
- Released in `main` 1fe6243 (deployed 18:05 UTC). The held-out set and its test in main match this branch.
- Earlier note: The full suite (with the LD_PRELOAD from the README) gave **1268 passed, 9 skipped** on 2026-10-16 at about 13:10 UTC.

## For the owner's human label pass (added by the lead, 2026-10-16)
- ~~"Exactly 2 years required" is labelled inconsistently~~ **RESOLVED by the owner's decision, applied by gosha-label on 2026-10-16. See "Two-year rule applied" below.**

## Two-year rule applied (owner's decision, 2026-10-16), committed in 6808693
Rule: when the headline requirement is exactly 2 years, or "2+ years", the level is `junior` (the product's line, `filters.JUNIOR_MAX_YEARS`). A range such as 2–4 or 2–3, or a posting that is mid or senior for another reason, is unchanged. Applied to both sets in one pass, by reading the text. No ranker output was looked at.

- **Changed (9 postings)**, each with the note `level: 2-year rule (owner 2026-10-16)`, appended to any existing note:
  - main `eval/postings.jsonl`: `ejobs-027` (any → junior; "minim 2 ani" in pharma promotion);
  - held-out, mid → junior: `heldout-ejobs-002`, `heldout-ejobs-005`, `heldout-ejobs-006`, `heldout-ejobs-034`, `heldout-ejobs-045`, `heldout-ejobs-052`, `heldout-ejobs-059`, `heldout-jobicy-014`.
- **Considered and left (the level does not rest on the 2 years):**
  - main: `bestjobs-032` (2–4 years, mid; later changed by the range rule below), `bestjobs-023` (2–5, junior) and `bestjobs-035` (2–3, junior). Both are already correct under the range rule. Also `ejobs-057` (3+ years, QA lead).
  - main, already junior: `bestjobs-050`, `bestjobs-052`, `ejobs-026`, `ejobs-031` and `ejobs-039`.
  - main, "2 years" not about work experience: `ejobs-015` and `bestjobs-042`.
  - held-out: `heldout-ejobs-032` (also 4+ years of SQL), `heldout-ejobs-086` ("Mid Level" title), `heldout-ejobs-074` (Team Leader) and `heldout-bestjobs-045` (2–3 years; later changed by the range rule below).
- **Grades:** the full grade matrix (24 personas × both sets) is identical before and after, because `junior`, `any` and `mid` are all in `APPLICABLE_LEVELS`. Nothing to recompute.
- **Docs:**
  - `eval/heldout/README.md`: the rule, level counts (junior 36 → 44, mid 71 → 63), the lang × level table, the note count (205) and a "Two-year rule applied" section.
  - `docs/EVAL.md` (at the lead's request): the rule in Data → Labels, held-out 258 → 261, the limitation bullet marked resolved, and "What to try next" trimmed.
- **Test:** `tests/test_heldout.py::test_two_year_rule_relabels_are_junior[main|heldout]`. The held-out total is unchanged at 261.
- **CI eval gate:** the change of `postings_sha256` required new baselines. gosha-rank regenerated them, and they are in 6808693 too.
- Full suite: **1327 passed, 9 skipped** (2026-10-16).

## Range rule applied (owner's decision, 2026-10-16), committed in 6808693
Rule: a range of years is labelled by its lower bound, as `filters.required_years` reads it. Up to 2 years is `junior` (`JUNIOR_MAX_YEARS`), 3–4 is `mid`, and 5 or more is `senior` (`seniority.SENIOR_MIN_YEARS`). A title, a lead role or a "preferred only" requirement still takes precedence. It was applied to both sets in one pass, by reading the text; no ranker output was looked at. Candidates came from a scan of every year range in both sets, compared with the current label.

- **Changed (6 postings)**, each with the note `level: range lower-bound rule (owner 2026-10-16)`, appended to any existing note:
  - main `bestjobs-032`: 2–4 years, mid → junior;
  - main `bestjobs-109`: 1–2 years, any → junior;
  - main `bestjobs-101`: 3–5 years, any → mid;
  - main `ejobs-032`: 3–5 years, senior → mid;
  - main `bestjobs-087`: 5–6 years, any → senior;
  - held-out `heldout-bestjobs-045`: 2–3 years, mid → junior.
- The three main postings labelled `any` had their range as the headline requirement. The main set's original method (title and opening lines) most likely missed it.
- **Grade changes, all in the main set:**
  - `bestjobs-087`: fe and fe-ro go 2 → 1, and mob goes 1 → 0 (h-fe and h-mob the same, when run on the main postings);
  - `ejobs-032`: acc-ro goes 1 → 2.

  The held-out grades are unchanged. gosha-rank was told before regenerating; the baselines in 6808693 include this.
- **Considered and left:**
  - main `bestjobs-020`: "2–3 years" is the project's length, and the title says Senior;
  - held-out `heldout-ejobs-016`: "Middle" title;
  - held-out `heldout-bestjobs-080`: 1–3 years under "Preferred" only, so it stays `any`;
  - every other range in both sets already matched its lower bound.
- **Docs:**
  - `eval/heldout/README.md`: the years rule in the level rules, a "Range rule applied" section, level counts (junior 45, mid 62) and the ro row of the lang × level table;
  - `docs/EVAL.md`: a range-rule paragraph in Data → Labels, with the ids and the grade changes, and the limitation bullet.
- **Test:** `tests/test_heldout.py::test_range_rule_relabels_use_the_lower_bound[main|heldout]`. `tests/test_heldout.py` plus `tests/test_evaluation.py` give 37 passed.
- **Full suite (about 13:50 UTC):** 1428 passed, 9 skipped, **9 failed**. The failures are not from this work: `tests/test_alert_scenarios.py` (7) and `tests/test_scrape_health.py` (2) hit `TypeError: empty_stage() takes from 1 to 2 positional arguments but 3 were given` (`gosha/pipeline.py:1105`). That comes from another agent's uncommitted `gosha/pipeline.py` change. The lead was told.

### Status: DONE again
Both owner decisions are applied and committed. Resume only if the owner's human label review comes back.

### Standing instruction (from the lead, 2026-10-16)
gosha-rank may send a grade-rule proposal (for example, how `mid` postings grade for student personas). If one arrives:
- reply with its impact: every (persona, posting id) whose grade would change, before and after, on both sets;
- **do not apply it** until the owner decides.

## Impact of gosha-rank's grade-rule proposal (computed 2026-10-16; owner decided: NOT adopted)
**Decision (owner, via the lead, 2026-10-16): "filter, don't regrade".** No label or grade changes; the eval applies the product's level filter instead (gosha-rank's side). Nothing to do here; the impact table below is kept for reference only. `tests/test_heldout.py` thresholds stay as they are.

Proposal: for student personas, a `mid` posting in the persona's family is grade 1, not 2 (as for `senior`). Labels are unchanged; the rule lives in `gosha/evaluation.py` (gosha-rank's file).

There are two readings:
- **A, "like senior":** remove `mid` from `APPLICABLE_LEVELS`. A mid posting in an adjacent family also drops, from 1 to 0.
- **B:** only the same-family case changes, from 2 to 1; adjacent mid stays 1.

Every persona is a student or career starter, so all of them are affected. Totals:
- main set: A changes 39 cells (17 from 2 → 1, 22 from 1 → 0); B changes 17.
- held-out set: A changes 115 cells (39 from 2 → 1, 76 from 1 → 0); B changes 39.

Under both readings, the grade-2 counts drop below the 4 that `tests/test_heldout.py` requires on the held-out postings:
- held-out personas: h-mob 2, h-dotnet 3, h-java 3;
- main personas on the held-out postings: java 3, dotnet 3, mob 2.

On the main postings, the main set is already thin today (java 2, dotnet 2, py 3). Under A it becomes java 1, py 2, dotnet 2 and emb 2.

**main postings × main personas**

| persona | grade-2 now → A | 2 → 1 (same family, mid) | 1 → 0 (adjacent family, mid; reading A only) |
|---|---|---|---|
| py | 3 → 2 | ejobs-001 | ejobs-007, ejobs-017, ejobs-024 |
| py-ro | 3 → 2 | ejobs-001 | ejobs-007, ejobs-017, ejobs-024 |
| java | 2 → 1 | ejobs-007 | ejobs-001 |
| dotnet | 2 → 2 | – | ejobs-001, ejobs-007 |
| fe | 5 → 4 | hipo-027 | bestjobs-055, bestjobs-101 |
| fe-ro | 5 → 4 | hipo-027 | bestjobs-055, bestjobs-101 |
| mob | 6 → 4 | bestjobs-055, bestjobs-101 | hipo-027 |
| ml | 5 → 5 | – | ejobs-001, ejobs-024 |
| data | 9 → 8 | ejobs-024 | ejobs-021, bestjobs-044 |
| emb | 4 → 2 | bestjobs-044, bestjobs-046 | – |
| ops | 6 → 5 | ejobs-017 | – |
| qa | 12 → 10 | bestjobs-049, bestjobs-055 | – |
| acc-ro | 19 → 15 | ejobs-021, ejobs-032, ejobs-035, bestjobs-065 | – |
| cs | 12 → 12 | – | ejobs-038, ejobs-040, bestjobs-066, bestjobs-067 |

**held-out postings × held-out personas**

| persona | grade-2 now → A | 2 → 1 (same family, mid) | 1 → 0 (adjacent family, mid; reading A only) |
|---|---|---|---|
| h-ml | 15 → 10 | heldout-ejobs-073, heldout-bestjobs-049, heldout-hipo-005, heldout-hipo-019, heldout-hipo-037 | heldout-ejobs-032, heldout-ejobs-037, heldout-bestjobs-007, heldout-bestjobs-039, heldout-bestjobs-041, heldout-bestjobs-042, heldout-hipo-023, heldout-jobicy-003, heldout-jobicy-004 |
| h-data-ro | 23 → 13 | heldout-ejobs-032, heldout-ejobs-037, heldout-bestjobs-007, heldout-bestjobs-039, heldout-bestjobs-041, heldout-bestjobs-042, heldout-bestjobs-049, heldout-hipo-023, heldout-jobicy-003, heldout-jobicy-004 | heldout-ejobs-031, heldout-ejobs-073, heldout-bestjobs-033, heldout-bestjobs-034, heldout-bestjobs-084, heldout-hipo-005, heldout-hipo-019, heldout-hipo-037 |
| h-ops | 17 → 11 | heldout-ejobs-014, heldout-ejobs-037, heldout-ejobs-040, heldout-hipo-026, heldout-jobicy-008, heldout-jobicy-011 | heldout-ejobs-016, heldout-ejobs-039, heldout-ejobs-042, heldout-ejobs-046, heldout-bestjobs-001, heldout-bestjobs-010, heldout-bestjobs-012, heldout-bestjobs-021, heldout-bestjobs-074, heldout-hipo-022, heldout-hipo-028, heldout-hipo-038, heldout-hipo-043 |
| h-qa-ro | 8 → 7 | heldout-ejobs-086 | – |
| h-fe | 9 → 4 | heldout-ejobs-015, heldout-bestjobs-012, heldout-bestjobs-027, heldout-hipo-017, heldout-remoteok-002 | heldout-ejobs-016, heldout-ejobs-025, heldout-ejobs-042, heldout-ejobs-047, heldout-bestjobs-010, heldout-bestjobs-021, heldout-bestjobs-023, heldout-bestjobs-029, heldout-bestjobs-031 |
| h-mob | 5 → 2 | heldout-ejobs-025, heldout-bestjobs-029, heldout-bestjobs-031 | heldout-ejobs-015, heldout-ejobs-016, heldout-ejobs-042, heldout-ejobs-047, heldout-bestjobs-010, heldout-bestjobs-012, heldout-bestjobs-021, heldout-bestjobs-023, heldout-bestjobs-027, heldout-hipo-017, heldout-remoteok-002 |
| h-emb | 6 → 4 | heldout-ejobs-073, heldout-bestjobs-053 | – |
| h-dotnet | 5 → 3 | heldout-ejobs-047, heldout-bestjobs-023 | heldout-ejobs-015, heldout-ejobs-016, heldout-ejobs-023, heldout-ejobs-039, heldout-ejobs-042, heldout-ejobs-046, heldout-bestjobs-001, heldout-bestjobs-010, heldout-bestjobs-012, heldout-bestjobs-021, heldout-hipo-017, heldout-jobicy-006 |
| h-java | 7 → 3 | heldout-ejobs-015, heldout-ejobs-023, heldout-hipo-017, heldout-jobicy-006 | heldout-ejobs-016, heldout-ejobs-039, heldout-ejobs-042, heldout-ejobs-046, heldout-ejobs-047, heldout-bestjobs-001, heldout-bestjobs-010, heldout-bestjobs-012, heldout-bestjobs-021, heldout-bestjobs-023 |
| h-cs-ro | 20 → 19 | heldout-ejobs-028 | heldout-ejobs-010, heldout-ejobs-044, heldout-bestjobs-085, heldout-bestjobs-090 |

## DM level-precision audit (lead's task, 2026-10-17), done, reported
- **Code:** main 5ef26de (formerly d76c467; same `gosha/` tree).
- **Snapshot:** one read-only SELECT, taken after the 10:29 UTC cycle finished: 844 active jobs and 22 active searches, all entry-level. No user ids were selected.
- **Run:** `prefilter()` offline on the snapshot.
- **Result:** 96 jobs / 347 pairs pass. Hand-judged: 74 right, **17 wrong**, 5 borderline.
- **Causes:**
  - 8 jobs: experience demanded with no years and no "experienced".
  - 3 jobs: Hipo text flattened into one sentence.
  - 3 jobs: years phrasing missed.
  - 2 non-jobs.
  - 1 posting where seniority is only in the duties.
- No leaks came from missing descriptions or board labels.
- **Suggested root fix (not applied):** unmarked titles need positive entry evidence.
- Results, the judged list and the scripts are in the lead's scratchpad `audit-label/` (see SUMMARY.md there). The raw snapshot stays in my scratchpad.
- Nothing patched, no live requests, no prod writes. Status: standing by.

## Eval check of the 'evidence' junior rule (lead's question, 2026-10-17 ~11:10 UTC), done, reported
- **Scope:** main set only, read-only, on a scratch copy of the tree.
- **Drop set:** 60 postings are junior-eligible under `DM_JUNIOR_RULE='default'` and not under `'evidence'`. All 60 are gold `any`; none are gold `junior`.
- **Hand-judged on the stored description:** 20 yes (open to 0-2 years), 16 no (asks experience), 24 unclear. 15 of the unclear are cut off before their requirements: eval descriptions are capped at 2000 chars, and 109 of 202 hit the cap.
- **Evidence-rule phrasing gaps:** 9 of the 20 yes would pass if the rule knew their wording.
- **Missing board labels:** eval rows have no board label, so the "entry label" evidence path never fires there.
- **Labels unchanged.** The judged list is in the lead's scratchpad: `audit-label/eval_any_drops_judged.tsv`.

## Release judge (new lead session, 2026-10-17 ~14:00 UTC), waiting for the list
- **Assignment:** judge by hand EVERY row of gosha-pipe's would-send list (and its dropped list),
  forwarded by gosha-lead, on the stored description against the owner's rules (intern-only searches:
  training positions only; junior only where junior was chosen; never mid/senior; 2-year and range
  rules). Report right/wrong/borderline counts, one-line reasons per wrong/borderline row, and right
  jobs the tree drops. Previous lead's scratchpad (read-only reference):
  `/tmp/claude-1001/-home-ubuntu-gosha-jobs/3f714841-0998-4735-a06e-3fd5c1335b14/scratchpad`.
- **Done re-checked:** held-out load 261/10 with the grade-2 counts above; `pytest -q tests/test_heldout.py`
  11 passed (main d4158c7, 2026-10-17).
- **Description-cap plan written** (no board requests; one read-only SELECT of url/length only):
  `/tmp/claude-1001/-home-ubuntu-gosha-agents/029ed740-ffcb-41fe-b820-efdb1fbb8781/scratchpad/desc_cap_plan.md`
  (+ `dbcheck.csv`). Key facts: the cap is the collector's (`scripts/eval_collect.py:48,96`, copied in
  `eval/heldout/collect.py:44,88`), not the adapters (20,000). Main 109/202 cut (85 at 2000 + 24 at
  1990-1999), held-out 217/261. `scrub()` also removes all newlines, which `filters._SENTENCE_SPLIT_RE`
  uses. Ranking reads only 1,500 chars, so the cut matters for level-filter checks, not nDCG.
  Fix: 0 requests for 12 main + 31 held-out rows recoverable from prod's DB; then 94 detail GETs for
  the rest of main, 164 for held-out native + ~8 feed requests for Jobicy/RemoteOK (~258 total).
  Not applied; needs the lead's decision (and OK for any request).
- **Lead decision on the cap plan (gosha-lead, 2026-10-17): NOT now**, because changing eval data moves
  the baselines V3 is released against. After the V3 release:
  (a) collector cap 20000 and keep newlines (scrub must not destroy the sentence boundaries filters uses);
  (b) DB backfill of the 43 rows (12 main, 31 held-out);
  (c) main-set refetch (94 GETs, paced) only with the lead's explicit OK at that time, never during a
  prod cycle. Held-out refetch later and separately. Until then: no eval data changes; stay ready as judge.
- **Release judgement done (2026-10-17), reported to gosha-lead.** Inputs: gosha-pipe's
  `.../d88b10a3-.../scratchpad/ws/` (export 12:15:48Z). Judged list:
  `/tmp/claude-1001/-home-ubuntu-gosha-agents/029ed740-ffcb-41fe-b820-efdb1fbb8781/scratchpad/judge/release_judged.tsv`.
  - (A) would-send: 10/10 new sends right (7981 x7, 7423, 7384, 7506); 9 waiting rows (empty
    descriptions) right to wait.
  - (B) gate flips, 24 distinct (job, search level) = 41 pairs: 21 right (17 blocks, 4 Ascom admits),
    **2 wrong** (8319 Wordpress at intern+junior and junior, 6 pairs: 'min. 24 luni' = 2 years = junior;
    `required_years` reads no months, confirmed on the tree: None, while 'min. 2 ani' gives 2),
    1 borderline (8294 Access 'Software Engineer', graduate grade 9/10, lean admit).
  - Side finding: 11 Hipo descriptions in the export end with inline CSS (`#anunt-content p {...}`)
    after d4158c7's page-body read (for gosha-scrape).
- **All sent DMs judged by hand (owner request via gosha-lead, 2026-10-17), reported.** Input: the lead's
  `.../ba90e926-.../scratchpad/alldms/sent.jsonl` (263 DMs, 104 jobs). Output written there as
  `label_judged.tsv` + `label_summary.md` (copies and `judge.py` in my scratchpad `alldms/`). Did not open the
  lead's own file. Result: 107 right / 132 wrong / 24 borderline; 25 of 104 jobs right everywhere.
  Wrong by primary cause: location 42 (s24 abroad/US 23, s16 non-Cluj 15, s19 US-only 6), not-CS 37,
  level-experienced 35 (13 jobs, nearly all sent 10-16/17), stale 8, cross-board duplicate 6, not-a-job 4.
  24 "right" DMs rest on the owner's 2-year/range rules (8319, 8287, 8314, 8273, 8041).

## Graduate rule applied (owner's decision 2026-10-17), uncommitted — REPORT FOR gosha-lead
Rule: graduate programmes / new-grad / "Global Trainee" roles that need a FINISHED degree are `junior`;
training positions open to enrolled students stay `intern`. Every `intern` posting in both sets
(main 7, held-out 17) was re-read for who may apply; graduate/trainee wording in non-intern postings
was scanned too. No ranker output looked at.
- **Changed: 1 posting.** `heldout-ejobs-069` (SANEX "Internship R&D": "Ești proaspăt absolvent",
  "Studii superioare in Chimie...") intern → junior, note "level: finished-degree graduate rule
  (owner 2026-10-17)". Main set: 0 changes.
- Left `intern` (students may apply): main `ejobs-028` Viking ("Student or recent graduate"),
  `bestjobs-079` ENF (students); held-out `heldout-ejobs-068` EY ("degree or currently studying"),
  `heldout-bestjobs-098` Arcanna ("ongoing or graduated"), and the working-student/student postings.
- Left `intern` for lack of evidence (eligibility falls after the 2,000-char cap): main `ejobs-042/044/046`
  (Emerson), `bestjobs-107`; held-out `heldout-ejobs-009`, `heldout-bestjobs-094/-095/-097/-101`.
  Would be decidable after the post-V3 description backfill.
- Graduate programmes already `junior`, unchanged: `heldout-hipo-009` (Vodafone Discover Graduate),
  `heldout-ejobs-020` (SAP Trainee/Junior Consultant); `ejobs-027`, `heldout-ejobs-055/-070` are junior jobs.
- **Grades: 0 of 11,112 cells change** (24 personas × both sets, computed before/after). 069 is family
  `other` (grade 0 for every persona), so no DM-eval relevance changes either. Counts: held-out intern
  17 → 16, junior 45 → 46 (ro row 2/22 → 1/23).
- **Baselines:** the held-out `postings_sha256` changes, so `eval/results/heldout/*` must be regenerated
  by gosha-rank (metrics should not move). Not regenerated by me.
- Side flag for the owner (outside this rule, not changed): main `bestjobs-107` "Domain Trainee"
  (BrightClaim/Genpact R2R) is gold `intern` but its text asks "Experienced Accounting Knowledge (With
  Closing & reporting experience)".
- Files: `eval/heldout/postings.jsonl` (1 line), `eval/heldout/README.md` (intern rule, counts,
  "Graduate rule applied" section), `docs/EVAL.md` (Graduate rule paragraph), `tests/test_heldout.py`
  (`test_graduate_rule_relabels_are_junior[main|heldout]`; heldout case fails on HEAD's file).
- Verified: `.venv/bin/python -m pytest -q tests/test_heldout.py tests/test_evaluation.py` → 58 passed;
  `ruff check tests/test_heldout.py eval/heldout` clean.
