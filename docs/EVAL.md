# Ranking evaluation

GOSHA's pitch is that it ranks postings against your CV. This page
measures how well it does, on real postings, through the code that
ships. It also lists what the numbers changed and what they cannot tell
you.

```bash
python scripts/eval_ranking.py                       # production model, main set
python scripts/eval_ranking.py --postings eval/heldout/postings.jsonl \
    --personas eval/heldout/personas.jsonl          # the held-out set
python scripts/eval_ranking.py --models tfidf all-mpnet-base-v2 \
    paraphrase-multilingual-mpnet-base-v2 paraphrase-multilingual-mpnet-base-v2@512
python scripts/eval_ranking.py --feed-ranking cosine # the old pure-cosine feed
python scripts/eval_ranking.py --job-text title      # embed titles only
```

`gosha/evaluation.py` holds the metrics and the harness; `scripts/eval_ranking.py`
is the CLI (embeddings are cached on disk, so re-runs take seconds). The
`Ranking eval` workflow re-runs it with the production model on every PR
that touches ranking code, and fails if feed nDCG@10, Discord recall /
off-target rate at the default thresholds, or the DM score's P@10, P@5 and
recall (below) moved by more than 0.02, or the DM score starves more
searches, against
[`eval/results/all-mpnet-base-v2.json`](../eval/results/all-mpnet-base-v2.json)
(main set) or
[`eval/results/heldout/all-mpnet-base-v2.json`](../eval/results/heldout/all-mpnet-base-v2.json)
(held-out set). Each result records a hash of the postings and personas
it was computed on, and the check refuses a baseline from other files: a
change to `eval/` ships with regenerated baselines.

## What is measured

Everything goes through production functions, so only the encoder varies:
job text from `build_job_text`, the CV chunked and mean-pooled by
`embed_long_text`, Discord queries from `build_subscription_query` blended
with the CV by `blend_with_user_vector`, feedback by
`recommend.apply_feedback`.

- **Web feed:** all postings ranked as the feed ranks them
  (`recommend.hybrid_scores`): 0.5 × z(embedding cosine) + 0.5 ×
  z(TF-IDF similarity to the CV text, `gosha/lexical.py`), each z-score
  taken over the candidate set and clipped to ±3, minus a seniority
  penalty for the distance between the CV's level and the posting's
  (`gosha/seniority.py`). `--feed-ranking cosine` ranks by the cosine
  alone, as the feed did until 2026-10-16. Reported:
  nDCG@10 (gain 2^grade − 1) with a 95% bootstrap interval over personas,
  R-precision, MRR and P@10 on grade-2 postings, split by posting language
  and CV language.
- **Discord delivery:** each persona's saved search ("Job role: python
  developer. Location: Cluj"), blended with the CV or alone, against every
  posting. For each threshold: recall of grade-2 postings, the share of
  grade-0 pairs that get through ("off-target"), and how many postings a
  persona would be sent.
- **Discord DMs as users search** (what production sends; owner rule
  2026-10-16: filter, don't regrade). The searches are the three that
  production users type ("computer science internship", "cs entry level",
  "computer science intern") plus each persona's own keyword, each at the
  level sets users choose (intern; intern + junior; junior), in Cluj, with
  no CV (no production user with a search has one). Each search's pool is
  `pipeline.prefilter`, the function the match stage runs before scoring
  (hard filters, then what the keyword implies for the levels). The pool
  is then scored by the DM score (`matching.dm_score`) or by plain cosine.
  A field keyword that the scraper expands into roles ("computer science
  internship", "cs entry level") is also scored through each of those
  roles, and the best one counts (`matching.dm_role_terms`).
  A posting's copies count once, as delivery sends them: best first, a
  copy of a posting already kept is skipped (`pipeline._same_posting_for_dm`).
  Relevant: a computer-science family (a persona's own family for its
  keyword) at a gold level the search asked for (intern → intern; junior
  → junior or unstated). Reported per search, then averaged: "sent"
  (every posting over the threshold: delivery has no per-user, per-search
  or per-company cap since 2026-10-17, owner), P@5 and P@10 over the
  first postings sent, best first (how good the top of a batch is),
  recall, and "starved" (relevant postings in the pool, none sent). A search left with under 10 postings,
  or under 3 relevant ones, is flagged low-n and kept in the means. The
  place filter runs only when the postings record a place; they do not
  yet, so place is not filtered.
- **Web feed with one active search:** the feed's own scope filter
  (`recommend.in_search_scope`: the search's place, the match stage's
  posting gates and levels) first, at each level set, then the production
  ranking or the cosine over what is left. Gold grades unchanged. A
  persona with no grade-2 posting left is counted, not averaged.
- **One copy of a posting:** every feed measurement collapses the copies
  the feed collapses (`recommend.first_copies`: same title and employer
  by `dedup.normalize_key`, the best-ranked copy shown), and in the
  feedback simulation a rated posting hides its other copies.
- **Feedback:** a residual-collection simulation of the Rocchio update,
  through the feed ranking (the update moves the embedding vector; the
  keyword and seniority terms stay).
  Each round the simulated user sees the top 10 unrated postings, likes
  every grade-2 one and dislikes every grade-0 one. nDCG@10 is then
  compared on the postings still unrated, so rated items flatter neither
  side.
- **Baseline:** sublinear TF-IDF cosine between the same CV and job texts
  (`evaluation.tfidf_encoder`). An embedding model has to beat it to earn
  its 2 GB.

## Data

- **202 real postings** (`eval/postings.jsonl`), collected on 2026-10-01
  by `scripts/eval_collect.py` with the production adapters for eJobs,
  BestJobs and Hipo: 28 searches across IT and non-IT roles, including
  explicitly entry-level ones ("junior java", "stagiu", …), because the
  boards skew senior. 147 are in English and 55 in Romanian (stopword
  heuristic).
  - E-mail addresses, phone numbers and links are scrubbed.
  - 9 items were dropped: 6 career-event promotions listed as Hipo
    postings, a volunteer call, a code-licensing offer and one posting too
    ambiguous to label.
  - Descriptions are capped at 2,000 characters.
- **14 synthetic CVs** (`eval/personas.jsonl`): 12 IT students across 10
  role families, plus 2 controls (accounting, customer support). 3 of the
  14 are written in Romanian. The people are invented.
- **Labels.** Each posting carries one or more role families and a level
  (`intern`, `junior`, `any`, `mid`, `senior`), assigned by reading its
  title and opening lines.

  **Two-year rule (owner's decision, 2026-10-16).** A posting whose
  headline requirement is exactly 2 years, or "2+ years", is `junior`.
  This is the product's line (`filters.JUNIOR_MAX_YEARS`). A posting that
  is mid or senior for another reason (its title, or more years asked
  elsewhere) keeps its level.
  - Applied to both sets in one pass, without looking at any ranker
    output: 1 main posting (`ejobs-027`, any → junior) and 8 held-out
    postings (mid → junior).
  - Each changed posting carries a `note` saying so.
  - No grade changed: `junior`, `any` and `mid` are all levels a
    student can apply to.

  **Range rule (owner's decision, 2026-10-16).** A range of years
  ("2–4 years") is labelled by its lower bound, as the filters read it:
  up to 2 is `junior`, 3–4 is `mid`, and 5 or more is `senior`
  (`seniority.SENIOR_MIN_YEARS`). A title, a lead role or a
  "preferred only" requirement still takes precedence.
  - Applied to both sets in one pass, without looking at any ranker
    output. Each changed posting carries a `note` saying so.
  - Main set (5 postings):
    - `bestjobs-032`: 2–4 years, mid → junior;
    - `bestjobs-109`: 1–2 years, any → junior;
    - `bestjobs-101`: 3–5 years, any → mid;
    - `ejobs-032`: 3–5 years, senior → mid;
    - `bestjobs-087`: 5–6 years, any → senior.
  - Held-out set: `heldout-bestjobs-045` (2–3 years), mid → junior.
  - **Two main postings change grades:**
    - `bestjobs-087`: 2 → 1 for `fe` and `fe-ro`, and 1 → 0 for `mob`;
    - `ejobs-032`: 1 → 2 for `acc-ro`.

    Main-set results computed before this date used the old labels.

  **Graduate rule (owner's decision, 2026-10-17).** A graduate
  programme, new-grad or "Global Trainee" role that needs a finished
  degree is `junior`; a training position open to enrolled students stays
  `intern`. Every `intern` posting in both sets was re-read for who may
  apply, without looking at any ranker output.
  - Main set: no change. Its intern postings are open to students (Viking
    "Student or recent graduate", ENF) or do not say before the
    2,000-character cap (the three Emerson postings, BrightClaim).
  - Held-out set: `heldout-ejobs-069` (SANEX R&D internship for a
    "proaspăt absolvent" with finished chemistry studies), intern →
    junior, with a `note`.
  - No grade changed (`intern` and `junior` both count as applicable), and
    the posting's family is `other`, so no DM-eval relevance changes.

  A persona's grade follows from a rule in
  `gosha/evaluation.py`:
  - **2** = same family, at a level a student can apply to;
  - **1** = same family but senior, or an adjacent family (`ADJACENT`);
  - **0** = everything else.

  **The labels were assigned by an AI agent (Claude) during the
  2026-10-01 audit and have not been reviewed by a person.** They are
  plain JSON, one posting per line, so a human pass is cheap and is the
  most valuable next step.

- **Held-out set** (`eval/heldout/`, see its README): 261 postings
  collected on 2026-10-16 and 10 personas, sharing no posting or persona
  with the main set. Ranking choices are made on the main set and only
  reported here; it is never tuned on.
  - **Disclosure: the held-out set is not perfectly clean for anything
    that reads seniority.** `filters.required_years`, which the feed's
    seniority penalty uses (`gosha/seniority.py`), was changed after
    three held-out postings were read:
    - `heldout-ejobs-016`: the negation rule ("not looking for" / "nu
      căutăm"), later rewritten from general phrasing;
    - `heldout-bestjobs-066`: the 8-word gap allowed between the number
      and "experience";
    - `heldout-bestjobs-021`: a nice-to-have ("strongly preferred") in
      the previous clause hid "3-6 years of … experience". The ranking
      agent read this posting's requirement while comparing seniority
      detectors and reported it; the filters test uses a paraphrase.

    Beyond those three, only aggregate level splits and the titles of
    postings whose level changed were looked at, and no rule was written
    from them. Held-out numbers involving seniority (the production feed
    ranking) are therefore slightly optimistic. The cosine-only and TF-IDF
    rows do not read seniority and are unaffected.

## Results (2026-10-17): field searches scored through their roles

**The problem.** Production users search for a field ("computer science
internship", "cs entry level"). Internship ads say "computer science
students"; a junior job names a role (Data Scientist, QA Engineer, Support
Engineer) and rarely the field. In a 48-hour window (production export,
2026-10-17 13:55 UTC), 77 search × posting pairs passed every filter but
scored under the DM threshold, and 71 of them were junior postings.

**The rule.** A keyword that `filters.expand_keyword` expands into roles
is also scored through each role, with its level words removed ("data
scientist intern" becomes "data scientist"), each through the same DM
query (its places, its levels, the panel correction). A posting counts
at its best score, threshold unchanged at 0.34. The search's own query
is one of the rows, so nothing that went out before stops. Any other
keyword is scored as before.

**The bar, written before measuring.**
- (1) The pairs it recovers in that window must be at least as precise
  as the DMs sent today.
- (2) On the 263 hand-judged sent DMs, replayed through today's filters,
  it must not lower precision.
- A fallback threshold was fixed in advance, for the case where the rule
  failed.

| | Today | Role terms |
|---|---|---|
| (1) window: below-threshold pairs recovered | – | 56 of 77: 52 right, 3 borderline, 1 wrong (strict 0.929) |
| (2) judged sent DMs: sent, strict precision | 109, 0.789 | 112, 0.777 (adds 3: right, borderline, wrong) |
| All judged data, strict precision | 0.789 | 0.827 |
| Main, generic searches: sent per search, P@10, recall | 1.1, 1.000, 0.152 | 5.3, 0.967, 0.697 |
| Held-out, generic searches: sent, P@10, recall | 13.1, 0.922, 0.683 | 16.1, 0.917, 0.828 |

Persona-keyword searches and P@5 are unchanged, and no search starved.
The verdicts are gosha-label's (an AI judge).

**The miss and the owner's call.** The rule fails condition (2) by one
pair: of the 3 judged DMs it adds, 1 is wrong (a "Compliance Engineer
Intern", 0.317 → 0.348).
- The pre-registered fallback, a threshold of 0.39 for every search
  (picked on main), passes (1) and (2).
- But on held-out it is worse than today: generic recall 0.618 and P@10
  0.909; persona-keyword recall 0.505, with 9 searches starved instead
  of 2. It raises the bar for searches the rule does not touch.
- **On 2026-10-17 the owner shipped the rule at 0.34 despite the miss on
  (2).** The baselines in `eval/results/` were regenerated with it.

**Not shipped: embedding the role part of a description.** A second
candidate embedded the description's role sections (dropping company,
offer and application sections) instead of its first 1,500 characters.
- It was measured once on the same judged data and failed: strict 0.789
  → 0.771, 14 judged DMs lost (12 right).
- In the window, 15 DMs sent today fell under the threshold.
- The eval sets cannot test such a change: their descriptions were
  stored without line breaks, so no section can be found.

## Results (2026-10-17): the feed's search scope, one copy per posting

**What changed in the feed.**
- **Search scope = the DM match stage's posting gates, minus the entry
  evidence.** `recommend.in_search_scope` now drops ads, AI-training gig
  marketplaces and courses (`filters.looks_like_an_ad`,
  `is_gig_marketplace`, `is_not_a_job`), besides the levels. A test holds
  it to `pipeline.job_matches_subscription` with the evidence switched off.
- **One copy of a posting.** Cross-board dedup keeps one posting's rows
  apart when they name different cities, so a search in either city finds
  its copy. After the scope filter the feed shows one copy, the
  best-ranked, and a posting the user rated hides its other copies. In
  the production window of 2026-10-17 that is 10 pairs among 671 rows,
  all city copies.

**The entry evidence is not used in the feed.** DMs are pushed, so they
fail closed; the feed is browsed. Measured on the scoped feed (intern +
junior search):

| | Main: postings, grade-2 per persona, P@10 | Held-out |
|---|---|---|
| Levels only (before) | 106, 4.6, 0.321 | 150, 8.4, 0.320 |
| + ad / gig / not-a-job | 106, 4.6, 0.321 | 149, 8.4, 0.320 |
| Evidence as a filter | 51, 2.0, 0.210; 4 of 14 personas left nothing relevant | 68, 3.1, 0.140 |
| Evidence as a ranking penalty | 106, 4.6, 0.321 | 149, 8.4, 0.270 |

- As a filter, the evidence drops 55–63% of the relevant postings, mostly
  gold `any`.
- As a ranking penalty (0.25 z-units, a student's penalty for a
  mid-level posting, fixed before measuring), it is neutral on main. On
  held-out it is worse: P@10 −0.050 (95% CI −0.080…−0.020, 5 personas
  worse, none better); junior nDCG@10 −0.027 (−0.042…−0.013). It was not
  shipped, and no other weight was tried.

**Effect on the numbers** (baselines regenerated on 2026-10-17 with this
code, gosha-label's graduate relabel and gosha-pipe's filter changes of
the same day: the graduate rule, months read as years, a required
graduation grade as entry evidence):
- Full feed, main: production 0.759 → 0.755, cosine 0.549 → 0.558. Every
  collapse on the main set is a false merge: `dedup.normalize_key` drops
  parentheses, so Solvd's "Test Automation Engineer" `(Android)`, `(iOS)`
  and `(Web)` are one posting, and so are "Senior Java Developer" and
  "Senior Java Developer (Integration & Azure)". Production's
  cross-board dedup uses the same title rule, so it hides these copies
  already; the fix belongs in `gosha/dedup.py`. Held-out: unchanged.
- Feedback, main: +0.055 → +0.093 (CI +0.005…+0.182), from the collapsed
  order; held-out unchanged.
- DMs: the copy count changes nothing on either set; the eval's copies
  are all senior or mid, or fail the junior gates. The DM score's numbers
  move with the filters: main generic pool 9.3 → 8.0, persona-keyword
  P@5 0.381 → 0.366; held-out generic pool 21.7 → 19.3, P@10 0.902 → 0.922,
  persona-keyword P@5 0.158 → 0.151.
- Scoped feed, held-out junior: 134 → 132 postings (the graduate rule
  makes intern and junior exclusive).

## Results (2026-10-17): the DM junior rule is evidence-based

**The rule.** For DMs to an intern or junior search, a posting whose
title carries no level marker now needs **positive evidence** that
beginners can apply (`filters.has_entry_evidence`, behind
`filters.dm_junior_ok`). Before, it went out unless its text said
otherwise (years asked, "experienced" wording). Evidence is any of:
- a training or junior title;
- the board's "entry" label, with the years agreeing;
- an entry-role or level-one title ("Assistant", "Associate", "Analyst I", "L1");
- at most two years asked, even as a nice-to-have;
- "no experience needed" in its wordings, including "with or without experience" and "an advantage but not mandatory";
- wording addressed to graduates, students, a first job, a new trade or a school-leaving minimum;
- training that teaches the job.

The evidence list was selected on the main set only. The scrape stage and
the web feed keep the old rule. `DM_JUNIOR_RULE = "default"` is the
one-line rollback, and `DM_ENTRY_LABEL_IS_EVIDENCE` switches off the
board label as evidence.

The same change adds two more checks:
- **Not a job.** `filters.is_not_a_job` (DMs only) drops courses and talent-network CV drops posted as jobs.
- **Glued level words.** `filters.split_glued_level` splits a level word glued to the next word ("InternSoftware Engineer"). It applies everywhere titles are read for a level.

**Why.** DMs are the product's main feature, and the owner asked for
junior roles, not unmarked roles. The old rule failed open: every
demanding phrasing that no pattern knew went out. gosha-label's audit of
the 2026-10-17 10:48 production snapshot found 17 of the 96 jobs passing
intern/junior searches wrong. Fourteen of them came through the default,
each through a phrasing no list had ("Strong experience with Java 8+",
"Several years of professional experience"). The evidence rule **fails
closed**: a phrasing it does not know costs a DM, and never sends a wrong
one.

**Postings counted junior-eligible for a DM** (matches the junior level,
has a description, passes `dm_junior_ok`):

| Set | Gold counted relevant | Before P / R | After P / R | Mid + senior let through |
|---|---|---|---|---|
| main (202) | junior or `any` | 0.950 / 0.905 | 1.000 / 0.429 | 5 → 0 |
| main | junior only | 0.230 / 1.000 | 0.511 / 1.000 | |
| held-out (261) | junior or `any` | 0.763 / 0.954 | 0.891 / 0.454 | 29 → 4 |
| held-out | junior only | 0.319 / 0.956 | 0.673 / 0.822 | |

**Production audit** (a check, never tuned on). The real prefilter ran
for all 22 entry-level searches on the 10:48 snapshot, against gosha-label's
hand verdicts:
- wrong jobs went from 17 to 4;
- right jobs went from 74 to 72 (lost: a Software Engineer with no level wording, and a posting asking "24 luni", which is not parsed);
- wrong (search, job) pairs went from 31 to 12.

With the board's "entry" label not counted as evidence, 2 wrong jobs are
left and 4 right ones are lost.

**DM eval** (`dm_score` @ 0.34; the baselines in `eval/results/` were
regenerated with this rule):

| Set | Search | Pool before → after | Relevant in pool | P@5 | P@10 | Recall | Starved |
|---|---|---|---|---|---|---|---|
| main | generic | 30.0 → 9.3 | 25.3 → 8.7 | 0.900 → 1.000 | 0.915 → 1.000 | 0.189 → 0.128 | 0 → 0 |
| main | persona keyword | 35.8 → 12.9 | 2.9 → 1.3 | 0.448 → 0.381 | 0.330 → 0.356 | 0.828 → 0.816 | 3 → 3 |
| held-out | generic | 57.7 → 21.7 | 40.7 → 18.3 | 0.956 → 0.956 | 0.902 → 0.902 | 0.565 → 0.694 | 0 → 0 |
| held-out | persona keyword | 66.3 → 26.7 | 4.6 → 1.8 | 0.284 → 0.158 | 0.208 → 0.131 | 0.758 → 0.815 | 1 → 2 |

**Gold `any` still counts as relevant to a junior search, so DM recall
on `any` is low by design until those labels are reviewed.** The levels
were read from each posting's title and opening lines, and `any` means
"no level stated", not "open to beginners".
- Most of the drop above is `any` postings leaving the pool: "relevant in pool" falls by about 60%.
- The DM eval's recall is measured within the pool, so it can rise while fewer relevant postings are sent.

gosha-label judged the 60 main-set `any` postings the rule drops, reading
their full (2,000-character) descriptions:

| Verdict | Postings | Notes |
|---|---|---|
| Open to beginners | 20 | 15 not computer science; 9 of the 20 came back once their wording was added to the evidence list |
| Correctly dropped | 16 | |
| Unclear | 24 | mostly descriptions cut before the requirements |

The 11 real losses left name no beginner wording at all, for example
customer support that asks only for a language. A labels review should
re-grade `any` against the full text before the DM recall here is read as
a loss.

**Baselines.** `eval/results/all-mpnet-base-v2.json` and
`eval/results/heldout/all-mpnet-base-v2.json` were regenerated on
2026-10-17, so `--check` guards this rule from now on. Their feed numbers
also move:
- main junior: 109 → 100 postings left, nDCG@10 0.719 → 0.701;
- held-out junior: 140 → 135, 0.633 → 0.613.

That comes from the filter changes of 2026-10-16/17 ("experienced" wording,
"N years as <role>"), which the old baseline predated, not from this rule:
the feed does not use it. The 2026-10-16 tables below are kept as they were
measured.

## Results (2026-10-16): Discord DMs, the DM score

Every production DM today goes through one path: a saved search, no CV,
behind an intern/junior level filter. On that path, plain cosine at 0.42
was a recall cliff: the production query format puts computer-science
postings in a narrow 0.38–0.45 band. The score that ships is the **DM
score** (`gosha/matching.py`): the search's cosine minus 0.25 × the mean
cosine of a fixed panel of 40 everyday searches
([`gosha/dm_panel.txt`](../gosha/dm_panel.txt), IT and non-IT, sharing no
search with the personas or users) to the same posting, sent from 0.34,
"Strong match" from 0.47. It is one modified query vector per search, so
scoring costs what it did.

**How it was chosen.** gosha-lead fixed the rule before the run: the best
batch P@10 of the generic searches on the main set, with no more starved
searches than plain cosine at 0.42 (main, held-out and judged real DMs),
and at most 0.10 of held-out recall lost. The grid was α ∈ {0.25, 0.5,
0.75} × threshold. The rule picked α 0.25 @ 0.34. Other settings scored
higher on P@5 and recall: α 0.75 @ 0.05 gave held-out persona-keyword
P@5 0.31 and recall 1.0. They were not taken, because switching the
objective after seeing the results would be tuning on them.

| Set | Score | Searches | Pool | Sent | P@5 (95% CI) | P@10 | Recall | Starved | Low-n |
|---|---|---|---|---|---|---|---|---|---|
| main, generic | **DM score @ 0.34** | 9 | 34.7 | 6.7 | 0.900 (0.833–0.967) | **0.855** | 0.202 | 0 | 3 |
| main, generic | cosine @ 0.42 | 9 | 34.7 | 9.4 | 1.000 | 0.825 | 0.290 | 0 | 3 |
| main, persona keyword | **DM score** | 42 | 40.7 | 13.2 | 0.448 (0.323–0.567) | 0.346 | 0.826 | 3 | 28 |
| main, persona keyword | cosine | 42 | 40.7 | 15.1 | 0.428 (0.307–0.542) | 0.315 | 0.836 | 3 | 28 |
| held-out, generic | **DM score** | 9 | 60.3 | 27.1 | 0.956 (0.889–1.000) | 0.891 | 0.565 | 0 | 0 |
| held-out, generic | cosine | 9 | 60.3 | 31.2 | 0.956 | 0.906 | 0.636 | 0 | 0 |
| held-out, persona keyword | **DM score** | 30 | 69.1 | 23.6 | 0.271 (0.173–0.371) | 0.200 | 0.758 | 1 | 14 |
| held-out, persona keyword | cosine | 30 | 69.1 | 29.5 | 0.211 (0.140–0.287) | 0.166 | 0.768 | 1 | 14 |

Judged real DMs (266 sent pairs from 17 production subscriptions, one AI
judge; a check only, never tuned on): tech share of the top 5 0.804 →
0.830, of the top 10 0.746 → 0.786, no subscription starved.

What this says:

1. **The gain is small, and most of it is sending less.** A generic
   search is sent 6.7 postings instead of 9.4. Main P@10 rises by 0.03,
   main P@5 falls by 0.10 (from a perfect 1.0), and recall falls on both
   sets (main 0.29 → 0.20, held-out 0.64 → 0.57). Every interval overlaps
   plain cosine's.
2. **The filters do most of the work.** After `prefilter`, 81% of a
   main generic search's pool is relevant (28.0 of 34.7). The score
   decides the order and the cut within that pool.
3. **Persona-keyword searches gain on both sets** (held-out P@5 0.21 →
   0.27), but those intervals overlap too.
4. **Low-n is common.** The main set has 6 intern postings, none of them
   computer science. Under the owner's intern rule (an intern-only search
   gets training positions only), every computer-science intern-only
   search on main has an empty pool: 3 of 9 generic searches and 12 of 42
   persona searches have nothing relevant to send. 28 of 42 persona searches are low-n on main
   and 14 of 30 on held-out. Read the persona rows as indicative.

**Feed with one active search** (scope filter first; nDCG@10 over the
personas with a grade-2 posting left, paired difference with a 95%
bootstrap interval):

| Set | Levels | Postings left | Production | Cosine | Production − cosine |
|---|---|---|---|---|---|
| main | intern + junior | 115 | 0.721 | 0.671 | +0.050 (−0.038 … +0.129) |
| main | junior | 109 | 0.719 | 0.668 | +0.051 (−0.033 … +0.134) |
| main | intern | 6 | 2 of 14 personas measured | | |
| held-out | intern + junior | 155 | 0.579 | 0.595 | −0.015 (−0.115 … +0.096) |
| held-out | junior | 140 | 0.633 | 0.605 | +0.028 (−0.067 … +0.138) |
| held-out | intern | 17 | 6 of 10 personas measured | | |

Once a user's search narrows the feed to their level, the hybrid's lead
over the plain cosine is within noise on both sets: most of its full-feed
gain (below) is the seniority penalty, which the level filter already
applies.

## Results (2026-10-16): the ranking that ships

Feed nDCG@10 with a 95% bootstrap interval over personas; R-precision,
MRR and P@10 on grade-2 postings. Results:
[`eval/results/`](../eval/results/) (main) and
[`eval/results/heldout/`](../eval/results/heldout/).

| Set | Ranking | nDCG@10 (95% CI) | R-prec | MRR | P@10 |
|---|---|---|---|---|---|
| main (202 × 14) | **production** (hybrid + seniority) | **0.759** (0.651–0.858) | 0.547 | 0.801 | 0.429 |
| main | embedding cosine only (old feed) | 0.549 (0.449–0.647) | 0.277 | 0.526 | 0.300 |
| main | TF-IDF only | 0.638 (0.527–0.740) | 0.415 | 0.700 | 0.357 |
| held-out (261 × 10) | **production** | **0.604** (0.458–0.711) | 0.362 | 0.804 | 0.400 |
| held-out | embedding cosine only | 0.535 (0.415–0.642) | 0.351 | 0.709 | 0.320 |
| held-out | TF-IDF only | 0.508 (0.378–0.622) | 0.301 | 0.565 | 0.300 |

What this says, in order of confidence:

1. **The production ranking beats both of its parts on both sets.** The
   gain over the old cosine feed is +0.21 on the main set (paired 95%
   interval +0.13 … +0.29) and +0.07 on the held-out set (−0.02 … +0.15,
   not significant). Within one active search's scope it shrinks to noise
   (above).
2. **Read the held-out gain as the real one.** The hybrid, its weights
   and the seniority rules were designed while looking at the main set;
   the held-out set was only reported. A gain three times larger where
   the choices were made is what fitting to that set looks like. The
   held-out number is itself slightly optimistic for the seniority part
   (see the disclosure under Data).
3. **"TF-IDF beats the embeddings" did not replicate.** On the main set
   TF-IDF alone was ahead of the cosine (0.638 vs 0.549), which is what
   started the hybrid; on the held-out set it is behind (0.508 vs 0.535).
   Neither signal is reliably better alone, which is the case for mixing
   them, not for either one.
4. **Intervals are wide.** 14 and 10 personas: differences under ~0.05
   are noise.

How the main-set number was reached, one change at a time (`--no-feedback`;
held-out reported, never tuned on, with the size it had then):

| Change | main | held-out |
|---|---|---|
| embedding cosine (old feed) | 0.546 | 0.543 (253) |
| + TF-IDF hybrid 0.5/0.5, seniority penalty, Romanian diacritics folded | 0.731 | 0.625 (253) / 0.618 (258) |
| + z-scores clipped at ±3 (chosen a priori: a sparse keyword score can sit √N SD out, ~44 at 2,000 postings) | 0.743 | 0.624 (258) |
| + one seniority detector shared with the Discord level filter (`gosha/filters.py`) | 0.750 | 0.617 (258) |
| + final `required_years`, held-out grown to 261 | 0.750 | 0.604 (261) |
| labels: owner's two-year rule (9 postings, no grade changed) | 0.750 | 0.604 (261) |
| labels: ranges by their lower bound (7 postings; 2 main postings change grades) | 0.759 | 0.604 (261) |

Encoders tried inside the hybrid (main set only, an earlier sweep
script, so comparable only with each other): `bge-small-en-v1.5` 0.687,
`bge-base-en-v1.5` 0.698, `multilingual-e5-base` 0.649, `bge-m3` 0.685,
against 0.711 for `all-mpnet-base-v2` in the same sweep. No switch:
none is better, and `bge-m3` / `multilingual-e5-large` (~2.2 GB) do not
fit the production container. Stripping company boilerplate before
embedding gained nothing.

**Feedback through the hybrid** (one round, 👍 grade 2 / 👎 grade 0 on the
top 10, nDCG@10 on what is still unrated):

| like / dislike weight | main: before → after (Δ 95% CI) | held-out: before → after (Δ 95% CI) |
|---|---|---|
| 0.15 / 0.15 | 0.352 → 0.411 (+0.010 … +0.121) | 0.352 → 0.376 (−0.005 … +0.058) |
| **0.3 / 0.3 (production)** | 0.352 → 0.407 (−0.013 … +0.129) | 0.352 → 0.418 (+0.003 … +0.132) |
| 0.5 / 0.5 | 0.352 → 0.419 (−0.005 … +0.142) | 0.352 → 0.471 (+0.018 … +0.247) |
| 0.3 / 0 (likes only) | 0.352 → 0.371 (−0.028 … +0.064) | 0.352 → 0.344 (−0.060 … +0.038) |

Feedback still helps, by less than under the pure cosine (+0.10): the
update moves only the embedding half of the score. The main set does not
separate the weights, so they stay at 0.3 / 0.3 (choosing 0.5 from the
held-out column would be tuning on it). Likes alone (0.3 / 0) show no
gain on either set.

**Discord delivery with a CV** is unchanged by the feed ranking (it
scores the blended cosine, see below); the main-set numbers below still
hold (the label passes moved them by at most 0.002). On the held-out set,
at 0.43: recall 0.852, off-target passed 0.293. Searches without a CV now
use the DM score (above). The unfiltered "search only" sweep in the
results files remains as the old reference: every persona against every
posting.

## Results (2026-10-01): before the hybrid ranking

The feed then ranked by the embedding cosine alone; these numbers are
its "cosine only" row. The Discord and feedback decisions below were
taken then.

### Web feed

| Encoder | window | nDCG@10 (95% CI) | R-prec | MRR | P@10 | EN postings | RO postings | EN CVs | RO CVs |
|---|---|---|---|---|---|---|---|---|---|
| TF-IDF baseline | – | **0.626** (0.521–0.727) | 0.417 | 0.664 | 0.357 | 0.634 | 0.637 | 0.675 | 0.446 |
| `all-mpnet-base-v2` (production) | 384 | 0.546 (0.445–0.645) | 0.282 | 0.526 | 0.300 | 0.562 | 0.637 | 0.582 | 0.415 |
| `all-mpnet-base-v2`, title + company only | 384 | 0.556 (0.468–0.646) | 0.423 | 0.657 | 0.307 | 0.540 | 0.529 | 0.586 | 0.444 |
| `paraphrase-multilingual-mpnet-base-v2` | 128 | 0.485 (0.398–0.578) | 0.317 | 0.538 | 0.271 | 0.484 | 0.528 | 0.491 | 0.465 |
| `paraphrase-multilingual-mpnet-base-v2`, window raised to 512 | 512 | 0.414 (0.309–0.516) | 0.227 | 0.572 | 0.221 | 0.414 | 0.377 | 0.392 | 0.494 |

The title-only and multilingual rows are archived in
[`eval/results/archive/`](../eval/results/archive/): they predate the
hybrid feed ranking and are not gated by CI.

What this says, in order of confidence:

1. **The embedding ranking does not beat keyword matching on this set.**
   The production model's point estimate is below TF-IDF on nDCG@10
   (0.546 vs 0.626), R-precision and MRR. The intervals overlap, so this is
   "no evidence of an advantage", not proof of a disadvantage. It is
   still the opposite of what the pitch implies.
2. **The posting description adds nothing as embedded today.** Title and
   company alone score the same (0.556). Two failure modes are visible in
   the per-persona rankings:
   - **Company boilerplate.** Many postings open with the same paragraph
     ("Xebia is a global AI-first …", "Build your career at SCOR …").
     Two Xebia *Senior Java* ads rank 2nd and 3rd for the *Python*
     student, ahead of every Python posting a student could apply to.
   - **Seniority is invisible.** For the Python and Java students the top
     results are senior roles ("Senior Python AI Engineer", "Staff
     Backend Engineer"); the three Python roles a student could apply to
     rank 7th, 23rd and 31st.
3. **The multilingual model is worse, including on Romanian postings**
   (RO postings 0.528 vs 0.637). Romanian IT ads are full of English
   terms, which the English model handles. Its 128-token window
   truncates the job text and CV chunks; raising it to 512 makes things
   worse (0.414), since the model was trained on short inputs. The
   switch `docs/EMBEDDINGS.md` used to recommend is not supported by
   this eval.

### Discord delivery (production model)

With a CV (the search blended with the CV vector):

| threshold | recall (grade 2) | off-target passed | precision (grade 2) | precision (grade ≥1) | postings sent per persona |
|---|---|---|---|---|---|
| 0.40 (old) | 0.926 | 0.305 | 0.087 | 0.235 | 71.6 |
| **0.43 (new)** | **0.894** | **0.186** | 0.124 | 0.308 | 48.3 |
| 0.45 | 0.851 | 0.127 | 0.160 | 0.363 | 35.8 |
| 0.50 | 0.649 | 0.044 | 0.258 | 0.534 | 16.9 |
| 0.60 | 0.319 | 0.002 | 0.652 | 0.891 | 3.3 |

Search only (users without a CV):

| threshold | recall (grade 2) | off-target passed | precision (grade 2) | precision (grade ≥1) | postings sent per persona |
|---|---|---|---|---|---|
| 0.40 (old) | 0.915 | 0.417 | 0.067 | 0.184 | 91.7 |
| **0.42 (new)** | **0.883** | **0.338** | 0.078 | 0.204 | 76.1 |
| 0.45 | 0.787 | 0.238 | 0.095 | 0.231 | 55.4 |
| 0.50 | 0.606 | 0.102 | 0.153 | 0.312 | 26.6 |

At the old single threshold of 0.40, **a third of off-target postings
reached a CV holder, and 42% reached someone without a CV.** Precision
is low at every threshold that keeps recall high, because the cosine
separates relevant from irrelevant postings weakly (mean feed cosine:
0.56 for grade-2 pairs, 0.33 for grade-0).

**Decision taken (`gosha/matching.py`):** give up at most 5 points of
recall against 0.40 for the largest cut in off-target deliveries.

| Path | New threshold | Recall | Off-target passed | Postings sent |
|---|---|---|---|---|
| With a CV | **0.43** | 0.926 → 0.894 | 30.5% → 18.6% | −33% |
| Search only | **0.42** | 0.915 → 0.883 | 41.7% → 33.8% | −17% |

DMs no longer show "Score: 47%" (a cosine is not a percentage). They show
**"Strong match"** at ≥ 0.60, where 89% of the CV-path pairs are relevant
or adjacent, and **"Good match"** below it.

### Feedback (production model)

One round of 👍/👎 on the top 10, then nDCG@10 on what is still unrated:

| like / dislike weight | before | after | Δ (95% CI) |
|---|---|---|---|
| 0.15 / 0.15 | 0.472 | 0.541 | +0.012 … +0.133 |
| **0.3 / 0.3 (production)** | 0.472 | 0.572 | +0.002 … +0.212 |
| 0.5 / 0.5 | 0.472 | 0.579 | −0.012 … +0.232 |
| 0.3 / 0 (likes only) | 0.472 | 0.534 | −0.004 … +0.135 |

The Rocchio step works: one round of feedback lifts the rest of the
ranking by about 0.1 nDCG@10, and dislikes add to it. The weights stay at
0.3 / 0.3; nothing here says a different weight is better.

This is also why the second, term-counting feedback adjustment that used
to be added to Discord scores is gone (`gosha/feedback.py`). It changed
only the displayed score, never which postings were sent or their order,
and it double-counted feedback the vector already carried.

## Limitations

- **Small and synthetic on the CV side.** 14 personas give wide intervals:
  read differences under ~0.1 nDCG@10 as noise. The CVs are invented.
- **Labels by one annotator, an AI agent, from titles and opening
  lines.** The family/level rule is coarse: it cannot tell a Python data
  engineer from a Python web developer unless the families say so.
- **Place is not filtered.** The DM and scoped-feed sections apply the
  product's own filters, except place: the eval postings record none, and
  production's database holds only 7 of the 202 main and 30 of the 261
  held-out postings. The code applies Cluj once postings carry a
  `location`. Filling it in needs a fresh fetch from the boards. The
  unfiltered sections (full feed, threshold sweeps) still show every
  persona every posting, which overstates real DM volume.
- **Few intern postings.** The main set has 6 (none computer science) and
  the held-out set 17, so intern-only results are low-n or empty.
- **The DM searches are modelled, not sampled.** The three generic
  keywords and the level sets are what production users chose on
  2026-10-16; relevance for a generic search is "any computer-science
  family". The real-DM check rests on one AI judge's labels.
- **Every persona is a student or a career starter.** The seniority
  penalty for mid and senior readers (and `cv_level`'s reading of them)
  is untested; a CV that states experience only as date ranges
  ("2016 – present") reads as junior.
- **One snapshot of the boards, one day.**
- **"Exactly 2 years required", and ranges of years, were labelled
  inconsistently** until 2026-10-16 (main set junior, held-out set mid).
  Both sets now follow the owner's two-year and range rules (see
  Data). Results computed before
  that date counted those held-out postings as level errors.
- **The held-out set has seen a little tuning.** Three held-out postings
  shaped `required_years` rules (see Data), so held-out numbers that
  involve seniority are slightly optimistic.

## What to try next (not done here)

- **A human pass over the labels**, then more personas, with mid and
  senior CVs among them.
- **Feedback weights through the hybrid.** One round now moves the rest
  of the ranking less than it did; whether stronger weights help needs
  more personas than the main set has.
- **CV level from date ranges**, once there are non-student personas to
  check it on.
- **Seniority and TF-IDF in Discord scores.** Measured on the DM
  prototype: a seniority penalty changed nothing (the level filter has
  already removed above-level postings), and TF-IDF added noise. Neither
  is in the DM score.
- **Places in the eval sets**, so the DM and scoped-feed numbers include
  the place filter (after the release: it needs board fetches).
