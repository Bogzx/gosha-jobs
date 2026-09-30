"""Semantic matching engine using sentence-transformer embeddings.

Replaces regex-based title filtering with cosine similarity scoring.
Falls back to regex when the model isn't available.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

# One model for everything that lands in the same vector space: job and CV
# embeddings stored by gosha/embeddings.py (bot AND api processes) and the
# subscription queries scored against them. Those stored vectors used to
# ignore SEMANTIC_MODEL and always use the hardcoded default, so setting
# it made the match stage compare vectors from two different models —
# silently, when the dimensions happen to agree (768 for both mpnet
# variants). Changing it still means re-embedding every stored vector.
LEGACY_MODEL = "all-mpnet-base-v2"
DEFAULT_MODEL = os.getenv("SEMANTIC_MODEL", LEGACY_MODEL)

# Lazy-loaded model singleton
_model: Any = None
_model_name: str = ""


def _get_model(model_name: str = DEFAULT_MODEL) -> Any:
    """Load the sentence-transformer model (lazy singleton)."""
    global _model, _model_name
    if _model is not None and _model_name == model_name:
        return _model
    try:
        from sentence_transformers import SentenceTransformer

        log.info("Loading semantic model: %s", model_name)
        _model = SentenceTransformer(model_name)
        _model_name = model_name
        return _model
    except ImportError:
        log.warning(
            "sentence-transformers not installed — semantic matching disabled. "
            "Install with: pip install sentence-transformers"
        )
        return None


def is_available(model_name: str = DEFAULT_MODEL) -> bool:
    """Check if semantic matching is available."""
    return _get_model(model_name) is not None


def encode_texts(texts: list[str], model_name: str = DEFAULT_MODEL) -> np.ndarray | None:
    """Encode a list of texts into embedding vectors.

    Returns an (N, D) numpy array, or None if the model isn't available.
    """
    model = _get_model(model_name)
    if model is None:
        return None
    return model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)


def encode_single(text: str, model_name: str = DEFAULT_MODEL) -> np.ndarray | None:
    """Encode a single text into an embedding vector."""
    result = encode_texts([text], model_name)
    if result is None:
        return None
    return result[0]


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two normalized vectors."""
    return float(np.dot(a, b))


def cosine_similarity_batch(
    query: np.ndarray, candidates: np.ndarray
) -> np.ndarray:
    """Compute cosine similarities between a query and multiple candidates.

    Args:
        query: (D,) normalized query vector
        candidates: (N, D) normalized candidate matrix

    Returns:
        (N,) array of similarity scores
    """
    return candidates @ query


def build_subscription_query(
    keywords: list[str],
    locations: list[str],
    experience_levels: list[str] | None = None,
) -> str:
    """Build a natural-language query string from subscription parameters.

    This becomes the embedding for matching against job text.
    """
    parts = []

    if keywords:
        parts.append(f"Job role: {', '.join(keywords)}")

    if experience_levels and "any" not in experience_levels:
        parts.append(f"Experience level: {', '.join(experience_levels)}")

    if locations:
        parts.append(f"Location: {', '.join(locations)}")

    return ". ".join(parts) if parts else " ".join(keywords)


def build_job_text(title: str, company: str, description: str | None = None) -> str:
    """Build a text representation of a job for embedding.

    Weights title more heavily by repeating it.
    """
    parts = [title, title]  # Title repeated for emphasis
    if company and company != "Unknown":
        parts.append(f"at {company}")
    if description:
        # Use first 1500 chars of description — more context helps matching
        parts.append(description[:1500])
    return ". ".join(parts)


class SemanticMatcher:
    """Scores jobs against subscriptions using embedding similarity.

    Usage:
        matcher = SemanticMatcher(model_name="all-mpnet-base-v2", threshold=0.35)

        # Pre-compute subscription embedding
        query_emb = matcher.encode_subscription(keywords, locations, exp_levels)

        # Score a batch of jobs
        scores = matcher.score_jobs(query_emb, jobs_texts)
        # scores[i] >= threshold means job i is a match
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        threshold: float = 0.40,
    ) -> None:
        self.model_name = model_name
        self.threshold = threshold
        self._available: bool | None = None

    @property
    def available(self) -> bool:
        if self._available is None:
            self._available = is_available(self.model_name)
        return self._available

    def encode_subscription(
        self,
        keywords: list[str],
        locations: list[str],
        experience_levels: list[str] | None = None,
    ) -> np.ndarray | None:
        """Encode a subscription into a query embedding."""
        query_text = build_subscription_query(keywords, locations, experience_levels)
        return encode_single(query_text, self.model_name)

    def score_jobs(
        self,
        query_embedding: np.ndarray,
        job_texts: list[str],
    ) -> list[float]:
        """Score a list of job texts against a subscription embedding.

        Returns a list of similarity scores (0.0 to 1.0).
        """
        if not job_texts:
            return []

        job_embeddings = encode_texts(job_texts, self.model_name)
        if job_embeddings is None:
            return [0.0] * len(job_texts)

        scores = cosine_similarity_batch(query_embedding, job_embeddings)
        return scores.tolist()

    def score_single(
        self,
        query_embedding: np.ndarray,
        job_text: str,
    ) -> float:
        """Score a single job against a subscription embedding."""
        job_emb = encode_single(job_text, self.model_name)
        if job_emb is None:
            return 0.0
        return cosine_similarity(query_embedding, job_emb)

    def is_match(self, score: float) -> bool:
        """Check if a score exceeds the match threshold."""
        return score >= self.threshold
