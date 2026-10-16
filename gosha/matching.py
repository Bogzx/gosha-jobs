"""Semantic matching engine using sentence-transformer embeddings.

Replaces regex-based title filtering with cosine similarity scoring.
Falls back to regex when the model isn't available.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
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

# Discord delivery thresholds and the DM score label, tuned for
# all-mpnet-base-v2 on the offline eval (docs/EVAL.md, scripts/eval_ranking.py).
# A search blended with the user's CV scores on a different scale from the
# search alone (it averages two cosines), so each path has its own cut.
# Rule used: give up at most 5 points of recall against the old 0.40 for the
# largest cut in off-target deliveries. Measured: with a CV, recall
# 0.926 -> 0.894 and off-target pass rate 30.5% -> 18.6%; search only,
# 0.915 -> 0.883 and 41.7% -> 33.8%. At >= 0.60, 89% of CV-path pairs are
# relevant or adjacent, hence the "Strong match" cut.
# Re-tune all three after a SEMANTIC_MODEL change.
DEFAULT_THRESHOLD = 0.42
DEFAULT_CV_THRESHOLD = 0.43
STRONG_MATCH_AT = 0.60

# The Discord DM score (docs/EVAL.md, "Discord DMs"): a search's cosine to
# a posting minus DM_ALPHA times the mean cosine of a fixed panel of
# everyday searches (gosha/dm_panel.txt) to the same posting. Generic tech
# ads sit close to every short query; how close a posting is to searches in
# general is taken off. Used for searches without a CV (all of them in
# production, 2026-10-16); a search blended with a CV keeps the cosine and
# DEFAULT_CV_THRESHOLD.
#
# Chosen on the main eval set by the rule fixed before the run (gosha-lead):
# the best batch P@10 of the generic searches, with no more starved
# searches than plain cosine at 0.42 (eval and judged real DMs) and at most
# 0.10 of held-out recall lost. Pools are the product's own filters
# (pipeline.prefilter). Measured against cosine @ 0.42, no CV:
# - main, generic searches: P@10 0.825 -> 0.855, recall 0.29 -> 0.20
#   (the gain comes from sending fewer: 9.4 -> 6.7 per search);
# - held-out: generic recall 0.64 -> 0.57, persona-keyword P@5 0.21 -> 0.27;
# - judged real DMs (one AI judge): tech share of the top 5 0.80 -> 0.83,
#   no subscription starved.
# On the DM score's scale, not SEMANTIC_THRESHOLD's (a plain cosine).
# Re-pick all three after a SEMANTIC_MODEL or panel change.
DM_PANEL_FILE = Path(__file__).with_name("dm_panel.txt")


def load_dm_panel(path: Path = DM_PANEL_FILE) -> tuple[str, ...]:
    lines = (line.split("#", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines())
    return tuple(line for line in lines if line)


DM_PANEL: tuple[str, ...] = load_dm_panel()
DM_ALPHA = 0.25
DM_THRESHOLD = 0.34
# Same rule as STRONG_MATCH_AT, on main: from here 91% of the pairs sent
# (65 of 616) are relevant or adjacent.
DM_STRONG_MATCH_AT = 0.47

# Lazy-loaded model singleton
_model: Any = None
_model_name: str = ""
# Loading takes seconds and ~2 GB. Callers run in worker threads (CV
# uploads, the public demo), so without this lock a burst of first
# requests after a restart loads one copy each and blows the container's
# memory limit.
_model_lock = threading.Lock()


def _get_model(model_name: str = DEFAULT_MODEL) -> Any:
    """Load the sentence-transformer model (lazy singleton, loaded once)."""
    global _model, _model_name
    if _model is not None and _model_name == model_name:
        return _model
    with _model_lock:
        if _model is not None and _model_name == model_name:
            return _model  # loaded by the thread we waited for
        return _load_model(model_name)


def _load_model(model_name: str) -> Any:
    global _model, _model_name
    try:
        from sentence_transformers import SentenceTransformer

        log.info("Loading semantic model: %s", model_name)
        model = SentenceTransformer(model_name)
        _model, _model_name = model, model_name
        return model
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
        threshold: float = DEFAULT_THRESHOLD,
        cv_threshold: float | None = None,
        dm_threshold: float = DM_THRESHOLD,
    ) -> None:
        self.model_name = model_name
        self.threshold = threshold
        # For queries blended with the user's CV (gosha/pipeline.py).
        self.cv_threshold = DEFAULT_CV_THRESHOLD if cv_threshold is None else cv_threshold
        # For the DM score (dm_query), on its own scale.
        self.dm_threshold = dm_threshold
        self._available: bool | None = None
        # Panel means per (places, levels): encoded once per process.
        self._panel_means: dict[tuple, np.ndarray] = {}
        self._panel_lock = threading.Lock()

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

    def is_match(self, score: float, with_cv: bool = False) -> bool:
        """Check a score against the threshold for its query type."""
        return score >= (self.cv_threshold if with_cv else self.threshold)

    def panel_mean(
        self, locations: list[str], experience_levels: list[str] | None = None,
    ) -> np.ndarray | None:
        """Mean embedding of the panel searches with these places and
        levels (dm_panel_queries). Cached: 40 encodings, about a second."""
        key = (tuple(locations or ()), tuple(experience_levels or ()))
        with self._panel_lock:
            mean = self._panel_means.get(key)
            if mean is None:
                panel = encode_texts(dm_panel_queries(list(key[0]), list(key[1])), self.model_name)
                if panel is None:
                    return None
                mean = self._panel_means[key] = np.asarray(panel, dtype=np.float32).mean(axis=0)
            return mean

    def dm_query(
        self,
        query_embedding: np.ndarray,
        locations: list[str],
        experience_levels: list[str] | None = None,
    ) -> np.ndarray | None:
        """The search's query vector for the DM score: scored against the
        stored job vectors like any query, it gives dm_score. None when
        the panel cannot be encoded."""
        mean = self.panel_mean(locations, experience_levels)
        if mean is None:
            return None
        return dm_query_vector(query_embedding, mean[None, :])

    def is_dm_match(self, score: float) -> bool:
        return score >= self.dm_threshold


def dm_panel_queries(
    locations: list[str], experience_levels: list[str] | None = None,
) -> list[str]:
    """DM_PANEL as queries shaped like a user's search (same places and
    levels), so subtracting them leaves only the difference in role."""
    return [build_subscription_query([k], locations, experience_levels) for k in DM_PANEL]


def dm_query_vector(
    query_emb: np.ndarray, panel_embs: np.ndarray, alpha: float = DM_ALPHA,
) -> np.ndarray:
    """query - alpha * mean(panel).

    For a job vector j, (q - alpha * mean(p)) . j = q . j - alpha * mean(p_k . j):
    the search's cosine minus `alpha` times the panel's mean cosine, in one
    dot product, so postings are scored exactly as they are today.
    """
    query = np.asarray(query_emb, dtype=np.float32)
    panel = np.asarray(panel_embs, dtype=np.float32)
    if panel.ndim != 2 or panel.shape[1:] != query.shape:
        raise ValueError(f"panel {panel.shape} does not match query {query.shape}")
    return (query - alpha * panel.mean(axis=0)).astype(np.float32)


def dm_score(
    query_emb: np.ndarray, panel_embs: np.ndarray, job_embs: np.ndarray,
    alpha: float = DM_ALPHA,
) -> np.ndarray:
    """DM scores of postings `job_embs` (N, D) for one search: its cosine
    to each minus `alpha` times the panel's mean cosine to it."""
    return np.asarray(job_embs, dtype=np.float32) @ dm_query_vector(query_emb, panel_embs, alpha)


def match_label(score: float | None) -> str | None:
    """Words for a Discord delivery score.

    The score is a cosine (or the mean of two), not a probability: shown
    as "Score: 47%" it read as a coin flip for what is a good match. Every
    delivered job already cleared the threshold, so the label only
    separates the ones well above it.
    """
    if score is None:
        return None
    return "Strong match" if score >= STRONG_MATCH_AT else "Good match"


def dm_match_label(score: float | None) -> str | None:
    """match_label for a DM score (its own scale and Strong cut)."""
    if score is None:
        return None
    return "Strong match" if score >= DM_STRONG_MATCH_AT else "Good match"
