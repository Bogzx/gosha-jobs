"""Tests for the offline ranking eval (gosha/evaluation.py, scripts/eval_ranking.py).

No torch: the harness is driven by a fake encoder or the TF-IDF baseline,
so this runs in CI next to everything else. The model numbers themselves
come from `python scripts/eval_ranking.py` (docs/EVAL.md).
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from gosha import evaluation as ev
from gosha.recommend import apply_feedback

ROOT = Path(__file__).resolve().parent.parent
POSTINGS = ROOT / "eval" / "postings.jsonl"
PERSONAS = ROOT / "eval" / "personas.jsonl"


def posting(families=("py",), level="junior", lang="en", pid="p", text="python") -> ev.Posting:
    return ev.Posting(
        id=pid, source="test", lang=lang, families=tuple(families), level=level,
        title=text, company="Acme", description=text,
    )


def hashing_encoder(dim: int = 256):
    """Deterministic bag-of-words vectors: shared words -> higher cosine."""
    def encode(texts):
        out = np.zeros((len(texts), dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in text.lower().split():
                h = int(hashlib.md5(word.encode()).hexdigest(), 16)
                out[row, h % dim] += 1.0
            norm = np.linalg.norm(out[row])
            if norm:
                out[row] /= norm
        return out
    return encode


# ── grading ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("families", "level", "expected"), [
    (("py",), "junior", 2),
    (("py",), "mid", 2),          # students apply to 2-4 year roles too
    (("py",), "senior", 1),       # right field, wrong level
    (("java",), "junior", 1),     # adjacent field
    (("java",), "senior", 0),     # adjacent AND senior: not for this persona
    (("acc",), "any", 0),
    (("ml", "py"), "senior", 1),  # any listed family counts
])
def test_grade(families, level, expected):
    assert ev.grade("py", posting(families, level)) == expected


# ── metrics, hand-computed ──────────────────────────────────────────────


def test_ndcg_hand_computed():
    # DCG [2,0,1] = 3/log2(2) + 0 + 1/log2(4) = 3.5
    # IDCG [2,1,0] = 3 + 1/log2(3)
    assert ev.ndcg_at([2, 0, 1], k=3) == pytest.approx(3.5 / (3 + 1 / math.log2(3)))
    assert ev.ndcg_at([2, 1, 0]) == pytest.approx(1.0)
    assert ev.ndcg_at([0, 0, 0]) == 0.0


def test_ndcg_cutoff_ignores_items_below_k():
    assert ev.ndcg_at([0, 2], k=1) == 0.0


def test_r_precision_and_mrr():
    assert ev.r_precision([2, 0, 2, 1]) == pytest.approx(0.5)  # R=2, one in top 2
    assert ev.r_precision([0, 1]) == 0.0
    assert ev.reciprocal_rank([0, 1, 2]) == pytest.approx(1 / 3)
    assert ev.reciprocal_rank([1, 1]) == 0.0


def test_ranking_is_descending_and_stable():
    assert ev.ranking([0.1, 0.5, 0.5, 0.2]) == [1, 2, 3, 0]


def test_bootstrap_ci_is_reproducible_and_brackets_the_mean():
    values = [0.2, 0.4, 0.9, 0.5, 0.7]
    lo, hi = ev.bootstrap_ci(values)
    assert (lo, hi) == ev.bootstrap_ci(values)
    assert lo <= np.mean(values) <= hi
    assert ev.bootstrap_ci([0.5] * 4) == (pytest.approx(0.5), pytest.approx(0.5))


def test_threshold_sweep_counts():
    scores = np.array([[0.9, 0.5, 0.1]])
    gains = np.array([[2, 0, 2]])
    row = ev.threshold_sweep(scores, gains, thresholds=[0.4])[0]
    assert row["recall"] == pytest.approx(0.5)
    assert row["off_target_pass_rate"] == pytest.approx(1.0)
    assert row["precision"] == pytest.approx(0.5)
    assert row["per_persona_passed"] == pytest.approx(2.0)


def test_tfidf_encoder_unit_vectors_and_overlap():
    encode = ev.tfidf_encoder(["python django api", "java spring", "contabil saga"])
    vecs = encode(["python api developer", "python django", "contabil"])
    assert np.allclose(np.linalg.norm(vecs, axis=1), 1.0)
    assert vecs[0] @ vecs[1] > vecs[0] @ vecs[2]


# ── apply_feedback: the production Rocchio step ────────────────────────


def test_apply_feedback_matches_rocchio_formula():
    base = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    liked = [np.array([0.0, 1.0, 0.0])]
    disliked = [np.array([0.0, 0.0, 1.0])]
    out = apply_feedback(base, liked, disliked, like_weight=0.5, dislike_weight=0.25)
    expected = np.array([1.0, 0.5, -0.25])
    assert np.allclose(out, expected / np.linalg.norm(expected))


def test_apply_feedback_without_signal_or_cancelled():
    assert apply_feedback(None, [], []) is None
    v = np.array([1.0, 0.0])
    assert apply_feedback(None, [v], [v], 0.3, 0.3) is None
    only_likes = apply_feedback(None, [v], [])
    assert np.allclose(only_likes, v)


# ── the committed dataset ───────────────────────────────────────────────


def test_dataset_files_are_valid():
    postings = ev.load_postings(POSTINGS)
    personas = ev.load_personas(PERSONAS)
    assert len({p.id for p in postings}) == len(postings) >= 150
    assert {p.lang for p in postings} == {"en", "ro"}
    assert len({u.id for u in personas}) == len(personas) >= 12
    for persona in personas:
        relevant = [p for p in postings if ev.grade(persona.family, p) == 2]
        assert len(relevant) >= 2, f"{persona.id} has too few relevant postings"


def test_dataset_has_no_contact_details():
    text = POSTINGS.read_text(encoding="utf-8")
    assert "@gmail" not in text
    assert not any(ch.isdigit() for ch in "".join(
        token for token in text.split() if token.startswith("+40")))


def test_unknown_label_is_rejected(tmp_path):
    bad = tmp_path / "p.jsonl"
    row = {"id": "x", "source": "s", "lang": "en", "families": ["nope"], "level": "junior",
           "title": "t", "company": "c", "description": "d"}
    bad.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bad label"):
        ev.load_postings(bad)


# ── the harness, end to end on a fake encoder ──────────────────────────


@pytest.fixture
def small_world():
    postings = [
        posting(("py",), "junior", "en", "a", "junior python django developer"),
        posting(("py",), "senior", "en", "b", "senior python architect"),
        posting(("java",), "junior", "ro", "c", "programator java spring"),
        posting(("acc",), "any", "ro", "d", "contabil saga balanta"),
        posting(("sales",), "any", "en", "e", "sales representative cold calling"),
        posting(("py",), "any", "ro", "f", "programator python flask"),
        posting(("acc",), "junior", "en", "g", "junior accountant saga"),
    ]
    personas = [
        ev.Persona("u1", "en", "py", "python developer", "student python django fastapi"),
        ev.Persona("u2", "ro", "acc", "contabil", "studenta contabil saga balanta"),
    ]
    return postings, personas


def test_run_end_to_end_with_fake_encoder(small_world):
    postings, personas = small_world
    result = ev.run(postings, personas, hashing_encoder())

    assert result["postings"] == 7 and result["personas"] == 2
    assert 0.0 <= result["feed"]["mean"]["ndcg@10"] <= 1.0
    assert set(result["feed"]["per_persona"]) == {"u1", "u2"}
    # A higher bar never lets more through.
    recalls = [r["recall"] for r in result["dm_with_cv"]["sweep"]]
    assert recalls == sorted(recalls, reverse=True)
    assert len(result["feedback"]) == len(ev.FEEDBACK_WEIGHTS) * len(ev.FEEDBACK_ROUNDS)
    json.dumps(result)  # plain data, writable as results JSON


def test_feedback_simulation(small_world):
    postings, personas = small_world
    vectors = ev.encode_all(postings, personas, hashing_encoder())
    rows = ev.simulate_feedback(vectors, weights=((0.0, 0.0), (0.5, 0.5)), rounds=(1,), shown=1)
    no_op, real = rows
    # Zero weights cannot change the ranking; a real update is measured
    # on the postings the simulated user has not rated yet.
    assert no_op["personas"] >= 1
    assert no_op["ndcg@10_before"] == no_op["ndcg@10_after"]
    assert real["personas"] == no_op["personas"]


def test_feedback_simulation_with_nothing_left_to_rank(small_world):
    postings, personas = small_world
    vectors = ev.encode_all(postings, personas, hashing_encoder())
    row = ev.simulate_feedback(vectors, weights=((0.3, 0.3),), rounds=(1,), shown=10)[0]
    assert row["personas"] == 0 and "ndcg@10_after" not in row


def test_dm_blend_uses_production_function(small_world):
    postings, personas = small_world
    vectors = ev.encode_all(postings, personas, hashing_encoder())
    with_cv = ev.dm_scores(vectors, with_cv=True)
    search = ev.dm_scores(vectors, with_cv=False)
    cv_only = vectors.jobs @ vectors.cvs[0]
    # pipeline.blend_with_user_vector: score = mean of the two cosines.
    assert np.allclose(with_cv[0], (search[0] + cv_only) / 2, atol=1e-6)


# ── the CLI and its regression gate (TF-IDF: no torch needed) ──────────


def run_cli(*args):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "eval_ranking.py"), *args],
        capture_output=True, text=True, cwd=ROOT, timeout=300,
    )


def test_cli_writes_results_and_passes_its_own_check(tmp_path):
    out = run_cli("--models", "tfidf", "--no-feedback", "--write-results", str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert "| `tfidf` |" in out.stdout
    baseline = tmp_path / "tfidf.json"
    assert json.loads(baseline.read_text())["feed"]["mean"]["ndcg@10"] > 0

    again = run_cli("--models", "tfidf", "--no-feedback", "--check", str(baseline))
    assert again.returncode == 0, again.stderr


def test_cli_check_fails_on_regression(tmp_path):
    run_cli("--models", "tfidf", "--no-feedback", "--write-results", str(tmp_path))
    baseline = tmp_path / "tfidf.json"
    data = json.loads(baseline.read_text())
    data["feed"]["mean"]["ndcg@10"] += 0.2  # pretend the past was much better
    baseline.write_text(json.dumps(data))

    out = run_cli("--models", "tfidf", "--no-feedback", "--check", str(baseline))
    assert out.returncode == 1
    assert "REGRESSION: feed nDCG@10" in out.stderr
