# Embeddings and switching the model

GOSHA ranks jobs by cosine similarity between sentence-transformers vectors:
job text (`title ×2 + company + first 1,500 chars of description`) against
the user's CV (chunked and mean-pooled, `gosha/embeddings.py`) and, for
Discord delivery, the saved search blended with the CV
(`gosha/pipeline.py:blend_with_user_vector`).

The production default is still **`all-mpnet-base-v2`** — English-only,
while eJobs, BestJobs and Hipo postings are largely Romanian. The code is
ready for a multilingual model; the switch itself is an operator decision
because it needs a re-embed and a threshold re-tune.

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

## Procedure

Recommended target: **`paraphrase-multilingual-mpnet-base-v2`** — 768
dimensions like the current model (no schema change), 50+ languages
including Romanian, same sentence-transformers API, ~1.1 GB download.
Alternative: `intfloat/multilingual-e5-base` (also 768-d, often stronger on
retrieval) — but E5 expects `"query: "` / `"passage: "` prefixes on its
inputs, which `gosha/matching.py` does not add yet; do that first if you
choose it.

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
   models; 0.40 was chosen for `all-mpnet-base-v2`. Before changing it,
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
