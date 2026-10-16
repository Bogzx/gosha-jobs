# Handoff: gosha-label — independent held-out ranking test set

Written: 2026-10-16 12:00 UTC · Branch: wip/agents-2026-10-16 · Base commit: 5490d6a

## Mission (one paragraph, self-contained)
GOSHA ranks job postings against a student's CV, and the user's goal is the most accurate job finder possible. The lead (orchestrator) is improving the ranking (hybrid lexical scoring, seniority handling and so on) and measures each change with the offline eval: `gosha/evaluation.py` plus `scripts/eval_ranking.py`, on `eval/postings.jsonl` (202 postings collected 2026-10-01) and `eval/personas.jsonl` (14 synthetic CVs), with the method in `docs/EVAL.md`. Tuning on that set risks overfitting. This agent's job was to build an **independent held-out test set**: fresh postings collected today with the production adapters, disjoint from the main set, labelled the same way (families, level, lang), plus 10 new personas, all in the same schema. The lead can then confirm on it that a gain measured on the main set is real.

## File ownership
- **Owned (created by this agent, all new and untracked):**
  - `eval/heldout/postings.jsonl`
  - `eval/heldout/personas.jsonl`
  - `eval/heldout/README.md`
  - `eval/heldout/collect.py`
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
