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


class TestDmPanel:
    """The hub-correction panel: fixed data, never the users' or personas' searches."""

    def test_panel_is_fixed_distinct_and_broad(self):
        from gosha.matching import DM_PANEL

        assert len(DM_PANEL) == 40
        assert len({k.lower() for k in DM_PANEL}) == len(DM_PANEL)
        # IT and non-IT work alike: a panel of tech searches would subtract
        # exactly what a computer-science search is looking for.
        assert {"accountant", "nurse", "driver", "software developer"} <= set(DM_PANEL)

    def test_panel_is_not_derived_from_the_eval_personas(self):
        import json
        from pathlib import Path

        from gosha.matching import DM_PANEL
        from gosha.evaluation import GENERIC_DM_KEYWORDS

        root = Path(__file__).resolve().parent.parent
        keywords = {k.lower() for k in GENERIC_DM_KEYWORDS}
        for path in (root / "eval" / "personas.jsonl", root / "eval" / "heldout" / "personas.jsonl"):
            keywords |= {json.loads(line)["keyword"].lower() for line in path.read_text().splitlines() if line.strip()}
        assert not keywords & {k.lower() for k in DM_PANEL}

    def test_panel_queries_take_the_searchs_place_and_levels(self):
        from gosha.matching import DM_PANEL, dm_panel_queries

        queries = dm_panel_queries(["Cluj"], ["intern"])
        assert len(queries) == len(DM_PANEL)
        assert queries[0] == build_subscription_query([DM_PANEL[0]], ["Cluj"], ["intern"])

    def test_dm_query_vector_is_cosine_minus_alpha_panel_mean(self):
        from gosha.matching import dm_query_vector

        rng = np.random.default_rng(0)
        q, panel, jobs = rng.normal(size=8), rng.normal(size=(5, 8)), rng.normal(size=(3, 8))
        got = jobs @ dm_query_vector(q, panel, alpha=0.75)
        want = jobs @ q - 0.75 * (jobs @ panel.T).mean(axis=1)
        assert np.allclose(got, want, atol=1e-5)
        with pytest.raises(ValueError):
            dm_query_vector(q, panel[:, :4])

    def test_panel_is_read_from_its_data_file(self, tmp_path):
        from gosha.matching import DM_PANEL, DM_PANEL_FILE, load_dm_panel

        assert DM_PANEL == load_dm_panel(DM_PANEL_FILE)
        f = tmp_path / "panel.txt"
        f.write_text("# header\nnurse\n\n  driver  # inline\n", encoding="utf-8")
        assert load_dm_panel(f) == ("nurse", "driver")


class TestDmScore:
    """The Discord DM score: search cosine minus DM_ALPHA x panel mean."""

    def test_constants_are_on_the_dm_scale(self):
        from gosha.matching import DM_ALPHA, DM_STRONG_MATCH_AT, DM_THRESHOLD

        assert DM_ALPHA == 0.25 and DM_THRESHOLD == 0.34
        assert DM_THRESHOLD < DM_STRONG_MATCH_AT

    def test_dm_score_is_cosine_minus_alpha_panel_mean(self):
        from gosha.matching import DM_ALPHA, dm_score

        rng = np.random.default_rng(1)
        q, panel, jobs = rng.normal(size=8), rng.normal(size=(5, 8)), rng.normal(size=(3, 8))
        want = jobs @ q - DM_ALPHA * (jobs @ panel.T).mean(axis=1)
        assert np.allclose(dm_score(q, panel, jobs), want, atol=1e-5)

    def test_matcher_dm_query_encodes_the_panel_once_per_scope(self, monkeypatch):
        import gosha.matching as m

        calls = []

        def fake_encode(texts, model_name=None):
            calls.append(list(texts))
            return np.stack([np.full(4, len(t), dtype=np.float32) for t in texts])

        monkeypatch.setattr(m, "encode_texts", fake_encode)
        matcher = m.SemanticMatcher()
        q = np.ones(4, dtype=np.float32)
        first = matcher.dm_query(q, ["Cluj"], ["intern"])
        again = matcher.dm_query(q, ["Cluj"], ["intern"])
        assert len(calls) == 1 and calls[0] == m.dm_panel_queries(["Cluj"], ["intern"])
        panel = np.stack([np.full(4, len(t), dtype=np.float32) for t in calls[0]])
        assert np.allclose(first, m.dm_query_vector(q, panel)) and np.allclose(first, again)
        matcher.dm_query(q, ["Cluj"], ["junior"])
        assert len(calls) == 2  # another scope, another encoding

    def test_matcher_dm_query_is_none_without_a_model(self, monkeypatch):
        import gosha.matching as m

        monkeypatch.setattr(m, "encode_texts", lambda texts, model_name=None: None)
        assert m.SemanticMatcher().dm_query(np.ones(4), ["Cluj"], ["intern"]) is None

    def test_dm_match_uses_its_own_threshold(self):
        from gosha.matching import DEFAULT_THRESHOLD, DM_THRESHOLD, SemanticMatcher

        matcher = SemanticMatcher()
        assert matcher.is_dm_match(DM_THRESHOLD) and not matcher.is_dm_match(DM_THRESHOLD - 0.01)
        assert matcher.is_dm_match(DEFAULT_THRESHOLD - 0.05) != matcher.is_match(DEFAULT_THRESHOLD - 0.05)
        assert SemanticMatcher(dm_threshold=0.5).dm_threshold == 0.5

    def test_dm_match_label(self):
        from gosha.matching import DM_STRONG_MATCH_AT, dm_match_label

        assert dm_match_label(None) is None
        assert dm_match_label(DM_STRONG_MATCH_AT) == "Strong match"
        assert dm_match_label(DM_STRONG_MATCH_AT - 0.01) == "Good match"


class TestDmRoleTerms:
    """A field keyword is scored through the roles it stands for (owner
    priority 2026-10-17: junior CS jobs describe a role, not the field)."""

    def test_a_field_keyword_adds_its_roles_without_level_words(self):
        from gosha.matching import dm_role_terms

        terms = dm_role_terms("computer science internship")
        assert terms[0] == "computer science internship"
        assert {"data scientist", "software engineer", "QA"} <= set(terms[1:])
        assert not any("intern" in t.lower() or "junior" in t.lower() for t in terms[1:])
        assert len({t.lower() for t in terms}) == len(terms)

    def test_any_other_keyword_is_scored_as_today(self):
        from gosha.matching import dm_role_terms

        assert dm_role_terms("computer science intern") == ["computer science intern"]
        assert dm_role_terms("python developer") == ["python developer"]

    def test_the_best_term_scores_a_posting(self):
        from gosha.matching import dm_scores_over_terms

        jobs = np.array([[1.0, 0.0], [0.0, 1.0]])
        terms = np.array([[0.4, 0.1], [0.0, 0.5]])
        assert np.allclose(dm_scores_over_terms(jobs, terms), [0.4, 0.5])

    def test_row_zero_is_todays_query_so_no_score_falls(self, monkeypatch):
        import gosha.matching as m

        def encode(texts, model_name):
            return np.array([[len(t) / 100, 1.0] for t in texts], dtype=np.float32)

        monkeypatch.setattr(m, "encode_texts", encode)
        matcher = SemanticMatcher(model_name="fake")
        args = (["computer science internship"], ["Cluj"], ["intern", "junior"])
        terms = matcher.dm_term_queries(*args)
        today = matcher.dm_query(m.encode_texts([m.build_subscription_query(*args)], "fake")[0], *args[1:])
        assert len(terms) == len(m.dm_role_terms(args[0][0]))
        assert np.allclose(terms[0], today)
        jobs = np.random.default_rng(0).normal(size=(5, 2))
        assert (m.dm_scores_over_terms(jobs, terms) >= jobs @ today - 1e-6).all()
        assert len(matcher.dm_term_queries(["python developer"], ["Cluj"], ["junior"])) == 1
