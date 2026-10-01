# Embeddings and switching the model

GOSHA ranks jobs by cosine similarity between sentence-transformers vectors:
job text (`title ×2 + company + first 1,500 chars of description`) against
the user's CV (chunked and mean-pooled, `gosha/embeddings.py`) and, for
Discord delivery, the saved search blended with the CV
(`gosha/pipeline.py:blend_with_user_vector`).

The production default is **`all-mpnet-base-v2`** — English-only, while
eJobs, BestJobs and Hipo postings are partly Romanian. The code is ready
for a multilingual model, but **the ranking eval ([docs/EVAL.md](EVAL.md))
does not support switching to `paraphrase-multilingual-mpnet-base-v2`**:
it ranks worse overall (nDCG@10 0.485 vs 0.546) and worse on Romanian
postings too (0.528 vs 0.637). Any candidate model should beat the
current one on that eval first.

## What makes a switch safe

- **One setting.** `SEMANTIC_MODEL` is read by `gosha.matching.DEFAULT_MODEL`
  and used by every encoder — subscription queries, job vectors, CV vectors,
  in both the `bot` and `api` processes. Set it identically for both.
- **Every stored vector records its model** (`jobs.embedding_model`,
  `users.cv_embedding_model`; NULL = `all-mpnet-base-v2`, i.e. rows written
  before the column existed). Readers only use vectors from the configured
  model. Between the switch and the end of the re-embed:
  - Discord delivery re-encodes stale job vectors on the fly (correct, slower);
  - the web feed leaves stale jobs out and ignores a stale CV vector (users
    see a recency-ordered feed until their CV is re-embedded);
  - the hourly backfill (`embed_new_jobs`) re-embeds 500 stale active jobs
    per run on its own.
- **`scripts/reembed.py`** does the whole corpus in minutes instead:
  batched, one commit per batch, resumable (a rerun skips rows already on
  the target model), with `--dry-run`.

## Why switch — measured

Ten English job phrases and their Romanian translations ("Junior Python
developer" / "Programator Python junior", "Accountant" / "Contabil", …),
encoded by each model on 2026-09-30:

| Model | cos(EN, its RO translation) | cos(EN, unrelated RO) | top-1 translation found |
|---|---|---|---|
| `all-mpnet-base-v2` (current) | 0.376 | 0.095 | 60% |
| `paraphrase-multilingual-mpnet-base-v2` | **0.875** | 0.213 | **100%** |

With the current model a Romanian posting that means *exactly* what an
English saved search asks for scores ~0.38 — below the 0.40 delivery
threshold. `scripts/reembed.py` with the multilingual model re-embedded 117
live-scraped eJobs/BestJobs postings on Postgres in ~5 s on CPU (model
load included); a production corpus of a few thousand takes minutes.

## Before switching: read docs/EVAL.md

The table above measures translation pairs, not the job the model does
here. [`docs/EVAL.md`](EVAL.md) measures the ranking itself on ~200 real
labelled postings, and it changes the picture:

- **Window size.** `paraphrase-multilingual-mpnet-base-v2` reads **128
  tokens**; `all-mpnet-base-v2` reads 384. The job text
  (`build_job_text`: title twice + up to 1,500 description characters)
  and the 1,400-character CV chunks (`gosha/embeddings.py`) are sized for
  384, so after a switch both would be silently cut to roughly their
  first 500-600 characters — mostly company boilerplate on many postings.
  The chunk size would have to follow the model's window first.
- **Ranking quality.** On the same labels the multilingual model scores
  nDCG@10 0.485 against 0.546 for the current one, and raising its window
  to 512 makes it worse (0.414). Romanian IT postings carry enough English
  terms for the English model to place them.

## Procedure

For whichever model wins on `scripts/eval_ranking.py` (none of the tested
ones does yet). `paraphrase-multilingual-mpnet-base-v2` is used as the
example below because it is 768-d like the current model (no schema
change). `intfloat/multilingual-e5-base` (also 768-d) is the other obvious
candidate, but E5 expects `"query: "` / `"passage: "` prefixes, which
`gosha/matching.py` does not add yet, and a 512-token window the CV
chunking would have to follow.

1. **Dry run** (bot container, so it sees the same DB, CV key and files):

   ```bash
   docker compose -f docker-compose.prod.yml run --rm \
     -e SEMANTIC_MODEL=paraphrase-multilingual-mpnet-base-v2 \
     bot python scripts/reembed.py --dry-run
   ```

2. **Pre-download the model** into the shared `hf_cache` volume so the
   restarted services do not both fetch it:

   ```bash
   docker compose -f docker-compose.prod.yml run --rm bot python -c \
     "from sentence_transformers import SentenceTransformer as S; S('paraphrase-multilingual-mpnet-base-v2')"
   ```

3. **Switch**: set `SEMANTIC_MODEL=paraphrase-multilingual-mpnet-base-v2`
   in `.env` and restart `bot` and `api`.

4. **Re-embed** (same command as step 1 without `--dry-run`). Interrupt
   and rerun freely.

5. **Re-tune `SEMANTIC_THRESHOLD`.** Cosine distributions differ between
   models; 0.42 / 0.43 (search alone / blended with the CV) were tuned for
   `all-mpnet-base-v2` on the offline eval — rerun `scripts/eval_ranking.py`
   with the new model and re-tune the same way. Before changing it,
   look at a day of deliveries (`user_jobs.relevance_score`) and feedback:
   pick the threshold that keeps roughly the old delivery volume, then
   adjust on 👍/👎 rates.

**Rollback**: set `SEMANTIC_MODEL` back (or remove it) and rerun
`scripts/reembed.py` — the same mechanism works in both directions.

## Descriptions

A vector is only as good as its text. eJobs, BestJobs and Hipo list pages
carry no description, so their adapters fetch detail pages for the jobs a
search returns (capped per search, 3 concurrent requests, cached for 24 h
per URL; `gosha/scrapers/details.py`). Jobs first stored title-only get
their description the next time they are scraped; `upsert_jobs` then drops
the old title-only vector and the embed step re-encodes the posting.
`scripts/reembed.py` does not refetch descriptions.
