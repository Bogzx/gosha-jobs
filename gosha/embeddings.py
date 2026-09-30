"""Job and CV embeddings stored as float32 bytes on the row.

Vectors come from the same sentence-transformers model the semantic matcher
uses (lazy singleton in gosha/matching.py). At the current scale (thousands
of jobs) numpy brute-force cosine scoring beats running a vector database.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np
from sqlalchemy import or_, select

from gosha import matching
from gosha.database import get_session
from gosha.matching import build_job_text
from gosha.matching import encode_texts as _encode_texts
from gosha.models import Job, User

log = logging.getLogger(__name__)

EMBEDDING_DIM = 768  # all-mpnet-base-v2
DEFAULT_BATCH = 500

# all-mpnet-base-v2 has max_seq_length = 384 tokens and silently drops
# everything past it — and nothing in this repo raises that limit. Passing
# it 8,000 characters therefore embedded roughly the first 1,500 and threw
# away the rest, which for most CVs is everything after the first job.
# "The job feed that reads your CV" read about a quarter of one.
#
# So the CV is windowed instead. ~350 tokens of headroom under the 384
# limit, ~4 characters per token for mixed English/Romanian technical
# prose, and windows overlap so a skill sitting on a boundary is whole in
# at least one of them.
CV_CHUNK_CHARS = 1400
CV_CHUNK_OVERLAP_CHARS = 200
# Ceiling on how much of a CV is considered at all. 40 chunks is ~56k
# characters — far past any real CV, and it bounds the work one upload can
# ask the model to do.
MAX_CV_CHUNKS = 40


def encode_texts(texts: list[str]) -> np.ndarray | None:
    """Encode texts with the shared semantic model. None when unavailable."""
    return _encode_texts(texts)


def encode_with_model(texts: list[str], model_name: str) -> np.ndarray | None:
    """Encode with an explicit model (the re-embed CLI's target)."""
    return _encode_texts(texts, model_name)


# ── Which model made a stored vector ────────────────────────────────────
#
# Vectors from different models live in different spaces; a cosine between
# them is a number with no meaning. Every stored vector therefore records
# its model (jobs.embedding_model, users.cv_embedding_model; NULL = rows
# written before the column existed, i.e. matching.LEGACY_MODEL), and
# readers only use vectors from the model currently configured. After a
# SEMANTIC_MODEL change, stale job vectors are re-encoded on the fly for
# delivery, left out of the feed, and replaced by the hourly backfill or,
# much faster, scripts/reembed.py.


def current_model() -> str:
    return matching.DEFAULT_MODEL


def is_current(model: str | None) -> bool:
    return (model or matching.LEGACY_MODEL) == current_model()


def current_model_clause(column):
    """SQL filter: `column` names the currently configured model."""
    if current_model() == matching.LEGACY_MODEL:
        return or_(column.is_(None), column == current_model())
    return column == current_model()


def stale_model_clause(column, model: str | None = None):
    """SQL filter: `column` names some model other than `model` (default:
    the configured one). Spelled out rather than ~current_model_clause():
    in SQL, NOT (NULL = 'x') is NULL, not true, so negating would silently
    skip every legacy (NULL) row."""
    model = model or current_model()
    if model == matching.LEGACY_MODEL:
        return column.isnot(None) & (column != model)
    return column.is_(None) | (column != model)


def vec_to_bytes(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def bytes_to_vec(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype=np.float32)


async def embed_new_jobs(limit: int = DEFAULT_BATCH) -> int:
    """Embed up to `limit` active jobs with no current-model embedding.

    Covers new jobs and, after a SEMANTIC_MODEL change, jobs embedded by
    the previous model. Returns the number of jobs embedded (0 when the
    model is unavailable).
    """
    async with get_session() as session:
        result = await session.execute(
            select(Job)
            .where(
                Job.is_active.is_(True),
                or_(
                    Job.embedding.is_(None),
                    stale_model_clause(Job.embedding_model),
                ),
            )
            .limit(limit)
        )
        jobs = list(result.scalars().all())

        if not jobs:
            return 0

        texts = [build_job_text(j.title, j.company, j.description) for j in jobs]
        vectors = encode_texts(texts)
        if vectors is None:
            log.debug("Embedding model unavailable — skipping %d jobs", len(jobs))
            return 0

        for job, vec in zip(jobs, vectors):
            job.embedding = vec_to_bytes(vec)
            job.embedding_model = current_model()
        await session.commit()

    log.info("Embedded %d jobs", len(jobs))
    return len(jobs)


def score_jobs_against_query(query_vec: np.ndarray, jobs: list[Job]) -> list[float]:
    """Cosine scores of jobs vs a query vector, reusing stored embeddings.

    Jobs without a stored vector are encoded on the fly (one batch); when
    the model is unavailable they score 0.0. This is what lets the match
    stage avoid re-encoding the same postings for every subscription.
    """
    if not jobs:
        return []

    vectors: list[np.ndarray | None] = [
        bytes_to_vec(job.embedding)
        if job.embedding and is_current(job.embedding_model)
        else None
        for job in jobs
    ]

    missing = [i for i, vec in enumerate(vectors) if vec is None]
    if missing:
        texts = [
            build_job_text(jobs[i].title, jobs[i].company, jobs[i].description)
            for i in missing
        ]
        encoded = encode_texts(texts)
        if encoded is not None:
            for idx, vec in zip(missing, encoded):
                vectors[idx] = np.asarray(vec, dtype=np.float32)

    return [
        float(vec @ query_vec) if vec is not None else 0.0
        for vec in vectors
    ]


def chunk_cv_text(
    text: str,
    chunk_chars: int = CV_CHUNK_CHARS,
    overlap: int = CV_CHUNK_OVERLAP_CHARS,
    max_chunks: int = MAX_CV_CHUNKS,
) -> list[str]:
    """Split a CV into overlapping windows the model can actually read.

    Breaks on whitespace where possible so a window never ends mid-token,
    which would otherwise turn a skill name into two meaningless fragments.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]

    step = max(1, chunk_chars - overlap)
    chunks: list[str] = []
    start = 0
    while start < len(text) and len(chunks) < max_chunks:
        end = start + chunk_chars
        if end < len(text):
            # Prefer the last whitespace in the final quarter of the window.
            boundary = text.rfind(" ", start + (chunk_chars * 3) // 4, end)
            if boundary > start:
                end = boundary
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(start + step, end - overlap)

    return chunks


def embed_long_text(text: str, encode=None) -> np.ndarray | None:
    """Embed a document longer than the model's window, as one vector.

    Every window is encoded, then mean-pooled and re-normalised. Mean
    rather than max because a CV is a description of one person: averaging
    keeps the overall profile while still letting a skill that appears in
    exactly one window pull the vector toward that skill's neighbourhood.
    Under the old truncation that skill contributed nothing at all.
    """
    chunks = chunk_cv_text(text)
    if not chunks:
        return None

    vectors = (encode or encode_texts)(chunks)
    if vectors is None:
        return None

    pooled = np.asarray(vectors, dtype=np.float32).mean(axis=0)
    norm = float(np.linalg.norm(pooled))
    if norm == 0:
        return None
    return (pooled / norm).astype(np.float32)


async def embed_user_cv(user_id: int, cv_text: str) -> bool:
    """Compute and store the embedding of a user's CV text.

    Chunked (see embed_long_text) rather than truncated: the whole CV
    reaches the vector, not just whatever fits in one model window.
    """
    # Encoding is CPU-bound (up to MAX_CV_CHUNKS windows); in a thread so
    # it does not stall the bot's gateway heartbeat or the API event loop.
    pooled = await asyncio.to_thread(embed_long_text, cv_text)
    if pooled is None:
        return False

    async with get_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return False
        user.cv_embedding = vec_to_bytes(pooled)
        user.cv_embedding_model = current_model()
        await session.commit()
    return True


async def clear_user_cv_embedding(user_id: int) -> None:
    """Remove the stored CV embedding (CV deleted)."""
    async with get_session() as session:
        user = await session.get(User, user_id)
        if user is not None:
            user.cv_embedding = None
            user.cv_embedding_model = None
            await session.commit()
