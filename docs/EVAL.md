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
that touches ranking code, and fails if feed nDCG@10, or Discord recall /
off-target rate at the default thresholds, moved by more than 0.02 against
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
  title and opening lines. A persona's grade follows from a rule in
  `gosha/evaluation.py`:
  - **2** = same family, at a level a student can apply to;
  - **1** = same family but senior, or an adjacent family (`ADJACENT`);
  - **0** = everything else.

  **The labels were assigned by an AI agent (Claude) during the
  2026-10-01 audit and have not been reviewed by a person.** They are
  plain JSON, one posting per line, so a human pass is cheap and is the
  most valuable next step.

- **Held-out set** (`eval/heldout/`, see its README): 258 postings
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

## Results (2026-10-16): the ranking that ships

Feed nDCG@10 with a 95% bootstrap interval over personas; R-precision,
MRR and P@10 on grade-2 postings. Results:
[`eval/results/`](../eval/results/) (main) and
[`eval/results/heldout/`](../eval/results/heldout/).

| Set | Ranking | nDCG@10 (95% CI) | R-prec | MRR | P@10 |
|---|---|---|---|---|---|
| main (202 × 14) | **production** (hybrid + seniority) | **0.750** (0.639–0.851) | 0.533 | 0.801 | 0.429 |
| main | embedding cosine only (old feed) | 0.546 (0.445–0.645) | 0.282 | 0.526 | 0.300 |
| main | TF-IDF only | 0.626 (0.521–0.727) | 0.417 | 0.664 | 0.357 |
| held-out (261 × 10) | **production** | **0.604** (0.458–0.711) | 0.362 | 0.804 | 0.400 |
| held-out | embedding cosine only | 0.535 (0.415–0.642) | 0.351 | 0.709 | 0.320 |
| held-out | TF-IDF only | 0.508 (0.378–0.622) | 0.301 | 0.565 | 0.300 |

What this says, in order of confidence:

1. **The production ranking beats both of its parts on both sets.** The
   gain over the old cosine feed is +0.20 on the main set and +0.07 on
   the held-out set.
2. **Read the held-out gain as the real one.** The hybrid, its weights
   and the seniority rules were designed while looking at the main set;
   the held-out set was only reported. A gain three times larger where
   the choices were made is what fitting to that set looks like. The
   held-out number is itself slightly optimistic for the seniority part
   (see the disclosure under Data).
3. **"TF-IDF beats the embeddings" did not replicate.** On the main set
   TF-IDF alone was ahead of the cosine (0.626 vs 0.546), which is what
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
| 0.15 / 0.15 | 0.348 → 0.405 (+0.010 … +0.118) | 0.352 → 0.376 (−0.005 … +0.058) |
| **0.3 / 0.3 (production)** | 0.348 → 0.402 (−0.013 … +0.126) | 0.352 → 0.418 (+0.003 … +0.132) |
| 0.5 / 0.5 | 0.348 → 0.413 (−0.005 … +0.138) | 0.352 → 0.471 (+0.018 … +0.247) |
| 0.3 / 0 (likes only) | 0.348 → 0.364 (−0.031 … +0.060) | 0.352 → 0.344 (−0.060 … +0.038) |

Feedback still helps, by less than under the pure cosine (+0.10): the
update moves only the embedding half of the score. The main set does not
separate the weights, so they stay at 0.3 / 0.3 (choosing 0.5 from the
held-out column would be tuning on it). Likes alone (0.3 / 0) show no
gain on either set.

**Discord delivery** is unchanged by the feed ranking (it scores the
blended cosine, see below); the main-set numbers below still hold. On the
held-out set, at the production thresholds: with a CV (0.43) recall 0.852,
off-target passed 0.293; search only (0.42) recall 0.826, off-target
0.345.

**Feed hard filters** (the user's active searches' locations and
levels, `recommend.in_search_scope`) are not measured: the personas have
a CV, not saved searches.

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
- **No hard filters.** Production filters Discord matches by location,
  experience level, salary and excluded words before scoring, and the
  feed by the user's active searches' locations and levels. Here every
  persona sees every posting, so "postings sent per persona" overstates
  real DM volume. The relative changes still hold.
- **Every persona is a student or a career starter.** The seniority
  penalty for mid and senior readers (and `cv_level`'s reading of them)
  is untested; a CV that states experience only as date ranges
  ("2016 – present") reads as junior.
- **One snapshot of the boards, one day.**
- **The two sets label "exactly 2 years required" differently.** Among
  postings whose level comes from the years they ask for, those asking
  for exactly 2 are labelled 6 junior / 1 mid in the main set and 7 mid /
  0 junior in the held-out set. The product rule is 2 = junior (the
  Discord junior filter, `filters.JUNIOR_MAX_YEARS`, and the feed's
  seniority both use it), so the held-out set scores those 7 as level
  errors. Neither set has been relabelled; this is listed for the human
  label pass.
- **The held-out set has seen a little tuning.** Three held-out postings
  shaped `required_years` rules (see Data), so held-out numbers that
  involve seniority are slightly optimistic.

## What to try next (not done here)

- **A human pass over the labels** (including the "exactly 2 years"
  conflict above), then more personas, with mid and senior CVs among
  them.
- **Feedback weights through the hybrid.** One round now moves the rest
  of the ranking less than it did; whether stronger weights help needs
  more personas than the main set has.
- **CV level from date ranges**, once there are non-student personas to
  check it on.
- **Seniority in Discord scores.** The match stage hard-filters by the
  search's level; whether a soft penalty on top improves recall against
  off-target is measurable with this harness and not yet measured.
