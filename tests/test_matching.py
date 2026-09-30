"""Tests for the semantic matching engine."""

from __future__ import annotations

import numpy as np
import pytest

from gosha.matching import (
    SemanticMatcher,
    build_job_text,
    build_subscription_query,
    cosine_similarity,
    cosine_similarity_batch,
)

# ── Pure function tests (no model needed) ─────────────────────────────


class TestBuildSubscriptionQuery:
    def test_basic(self):
        q = build_subscription_query(
            keywords=["software engineer"],
            locations=["Cluj"],
        )
        assert "software engineer" in q
        assert "Cluj" in q

    def test_with_experience(self):
        q = build_subscription_query(
            keywords=["developer"],
            locations=["Berlin"],
            experience_levels=["junior", "intern"],
        )
        assert "junior" in q
        assert "intern" in q

    def test_any_experience_excluded(self):
        q = build_subscription_query(
            keywords=["dev"],
            locations=["London"],
            experience_levels=["any"],
        )
        # "any" shouldn't appear in the query
        assert "Experience level" not in q

    def test_empty_keywords(self):
        q = build_subscription_query(keywords=[], locations=["Dublin"])
        assert "Dublin" in q


class TestBuildJobText:
    def test_basic(self):
        text = build_job_text("Backend Developer", "Google")
        assert "Backend Developer" in text
        assert "Google" in text

    def test_with_description(self):
        text = build_job_text("Dev", "Co", "Build amazing products")
        assert "Build amazing products" in text

    def test_title_repeated(self):
        text = build_job_text("Engineer", "Co")
        # Title should appear twice for emphasis
        assert text.count("Engineer") == 2

    def test_unknown_company_excluded(self):
        text = build_job_text("Dev", "Unknown")
        assert "Unknown" not in text

    def test_long_description_truncated(self):
        desc = "x" * 1000
        text = build_job_text("Dev", "Co", desc)
        # Should use first 500 chars
        assert len(text) < 1100


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = np.array([1.0, 0.0, 0.0])
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = np.array([1.0, 0.0, 0.0])
        b = np.array([0.0, 1.0, 0.0])
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        a = np.array([1.0, 0.0])
        b = np.array([-1.0, 0.0])
        assert cosine_similarity(a, b) == pytest.approx(-1.0)


class TestCosineSimilarityBatch:
    def test_batch(self):
        query = np.array([1.0, 0.0, 0.0])
        candidates = np.array([
            [1.0, 0.0, 0.0],  # identical
            [0.0, 1.0, 0.0],  # orthogonal
            [0.7071, 0.7071, 0.0],  # 45 degrees
        ])
        scores = cosine_similarity_batch(query, candidates)
        assert scores[0] == pytest.approx(1.0)
        assert scores[1] == pytest.approx(0.0)
        assert scores[2] == pytest.approx(0.7071, abs=0.001)


# ── SemanticMatcher tests ─────────────────────────────────────────────


class TestSemanticMatcher:
    def test_threshold(self):
        matcher = SemanticMatcher(threshold=0.5)
        assert matcher.is_match(0.6) is True
        assert matcher.is_match(0.4) is False
        assert matcher.is_match(0.5) is True

    def test_score_jobs_empty(self):
        matcher = SemanticMatcher()
        # Even with no model, empty list should return empty
        query = np.array([1.0, 0.0, 0.0])
        scores = matcher.score_jobs(query, [])
        assert scores == []


# ── Integration tests (only run if sentence-transformers is installed) ─


def _has_sentence_transformers() -> bool:
    try:
        import sentence_transformers  # noqa: F401 - availability probe
        return True
    except ImportError:
        return False


@pytest.mark.skipif(
    not _has_sentence_transformers(),
    reason="sentence-transformers not installed",
)
class TestSemanticMatcherIntegration:
    def test_available(self):
        matcher = SemanticMatcher()
        assert matcher.available is True

    def test_similar_jobs_score_higher(self):
        matcher = SemanticMatcher()
        query_emb = matcher.encode_subscription(
            keywords=["software engineer", "backend developer"],
            locations=["Romania"],
            experience_levels=["junior"],
        )
        assert query_emb is not None

        # Relevant jobs should score higher than completely irrelevant ones
        relevant = "Junior Backend Developer. Junior Backend Developer. at TechCorp. Build REST APIs with Python and PostgreSQL. Software engineering role."
        irrelevant = "Forklift Operator. Forklift Operator. at WarehouseCo. Operate forklifts in warehouse, load and unload shipments."

        score_relevant = matcher.score_single(query_emb, relevant)
        score_irrelevant = matcher.score_single(query_emb, irrelevant)

        assert score_relevant > score_irrelevant
        assert score_relevant > 0.3  # Should have decent similarity

    def test_batch_scoring(self):
        matcher = SemanticMatcher(threshold=0.3)
        query_emb = matcher.encode_subscription(
            keywords=["data scientist"],
            locations=["Berlin"],
        )
        assert query_emb is not None

        jobs = [
            "Data Scientist at MLCo. Machine learning, Python, TensorFlow.",
            "Senior Data Engineer at DataHouse. ETL pipelines, Spark, AWS.",
            "Receptionist at Hotel Grand. Customer service and scheduling.",
        ]

        scores = matcher.score_jobs(query_emb, jobs)
        assert len(scores) == 3
        # Data Scientist should score highest
        assert scores[0] > scores[2]
        # Data Engineer should be somewhat relevant
        assert scores[1] > scores[2]

    def test_encode_subscription_returns_normalized(self):
        matcher = SemanticMatcher()
        emb = matcher.encode_subscription(
            keywords=["developer"],
            locations=["London"],
        )
        assert emb is not None
        # Should be approximately unit norm (normalized)
        norm = np.linalg.norm(emb)
        assert norm == pytest.approx(1.0, abs=0.01)


def test_semantic_model_env_reaches_stored_embeddings():
    """Stored job/CV vectors must use SEMANTIC_MODEL, like the queries.

    gosha.embeddings calls matching.encode_texts without a model name, so
    the module default is what decides the stored vectors' model.
    """
    import os
    import subprocess
    import sys

    code = (
        "import inspect, gosha.matching as m;"
        "print(m.DEFAULT_MODEL,"
        " inspect.signature(m.encode_texts).parameters['model_name'].default)"
    )
    env = {**os.environ, "SEMANTIC_MODEL": "paraphrase-multilingual-mpnet-base-v2"}
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True,
        check=True,
    ).stdout.split()
    assert out == ["paraphrase-multilingual-mpnet-base-v2"] * 2
