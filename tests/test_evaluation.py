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
from dataclasses import replace
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


def test_cli_check_gates_the_dm_score(tmp_path):
    run_cli("--models", "tfidf", "--no-feedback", "--write-results", str(tmp_path))
    baseline = tmp_path / "tfidf.json"
    data = json.loads(baseline.read_text())
    data["dm_searches"]["dm_score"]["generic"]["starved"] = -1  # pretend nothing ever starved
    baseline.write_text(json.dumps(data))

    out = run_cli("--models", "tfidf", "--no-feedback", "--check", str(baseline))
    assert out.returncode == 1
    assert "REGRESSION: DM score generic searches starved" in out.stderr


def test_cli_check_refuses_a_baseline_from_another_eval_set(tmp_path):
    """The held-out set grew from 253 to 258 postings; a baseline from the
    old file compares two different measurements."""
    run_cli("--models", "tfidf", "--no-feedback", "--write-results", str(tmp_path))
    baseline = tmp_path / "tfidf.json"
    data = json.loads(baseline.read_text())
    assert len(data["postings_sha256"]) == len(data["personas_sha256"]) == 16
    data["postings_sha256"] = "0" * 16
    data["feed"]["mean"]["ndcg@10"] -= 0.2  # would pass on the numbers alone
    baseline.write_text(json.dumps(data))

    out = run_cli("--models", "tfidf", "--no-feedback", "--check", str(baseline))
    assert out.returncode == 1
    assert "regenerate the baseline" in out.stderr



# ── Discord DMs as users search (generic searches, filters first) ──────


def test_dm_search_query_is_the_production_query():
    search = ev.DmSearch("computer science internship", ("intern", "junior"))
    assert search.query == (
        "Job role: computer science internship. Experience level: intern, junior. Location: Cluj"
    )


def test_dm_searches_cover_generic_and_persona_keywords_at_every_level_set():
    persona = ev.Persona("u", "en", "py", "python developer", "cv")
    searches = ev.dm_searches([persona])
    generic = [s for s in searches if s.family is None]
    assert len(generic) == len(ev.GENERIC_DM_KEYWORDS) * len(ev.DM_LEVEL_SETS)
    assert {s.levels for s in searches if s.family == "py"} == set(ev.DM_LEVEL_SETS)


def test_dm_pool_applies_the_title_gate_and_the_level_filter_before_scoring():
    postings = [
        posting(("py",), "intern", pid="a", text="Software Engineering Intern"),
        posting(("py",), "senior", pid="b", text="Senior Software Engineer"),
        posting(("acc",), "intern", pid="c", text="Accounting Intern"),
    ]
    pool = ev.dm_pool(postings, ev.DmSearch("computer science internship", ("intern",)))
    # The senior role fails the level filter, the accounting one the tech title gate.
    assert pool.tolist() == [True, False, False]


def test_dm_relevant_is_the_right_field_at_a_wanted_level():
    postings = [
        posting(("java",), "intern", pid="a"),
        posting(("java",), "any", pid="b"),
        posting(("acc",), "intern", pid="c"),
        posting(("py",), "mid", pid="d"),
    ]
    assert ev.dm_relevant(postings, ev.DmSearch("cs", ("intern",))).tolist() == [True, False, False, False]
    # "any" (no level named) is what a junior search is for.
    assert ev.dm_relevant(postings, ev.DmSearch("cs", ("junior",))).tolist() == [False, True, False, False]
    specific = ev.DmSearch("python developer", ("intern", "junior"), family="py")
    assert not ev.dm_relevant(postings, specific).any()


def test_dm_search_metrics_reads_the_batch_best_first():
    scores = np.array([0.9, 0.8, 0.7, 0.2, 0.6])
    pool = np.array([True, True, True, True, False])
    relevant = np.array([False, True, True, True, True])
    m = ev.dm_search_metrics(scores, pool, relevant, threshold=0.5)
    assert m["sent"] == 3  # 0.2 is under the threshold, 0.6 is not in the pool
    assert m["p@5"] == pytest.approx(2 / 3)
    assert m["recall"] == pytest.approx(2 / 3)  # 2 of the 3 relevant in the pool
    assert not m["starved"]
    starved = ev.dm_search_metrics(scores, pool, relevant, threshold=0.95)
    assert starved["starved"] and starved["sent"] == 0 and math.isnan(starved["p@10"])


def test_dm_query_vectors_hub_alpha_subtracts_the_panel_mean():
    from gosha.matching import dm_panel_queries

    encode = hashing_encoder()
    search = ev.DmSearch("computer science internship", ("intern",))
    plain = ev.dm_query_vectors([search], encode)
    hub = ev.dm_query_vectors([search], encode, hub_alpha=0.5)
    panel = encode(dm_panel_queries([ev.DM_LOCATION], ["intern"]))
    assert np.allclose(hub[0], plain[0] - 0.5 * panel.mean(axis=0), atol=1e-6)
    result = ev.evaluate_dm_searches(
        [posting(("py",), "intern", pid="a", text="Software Engineering Intern")],
        [search], encode(["Software Engineering Intern"]), hub, threshold=-1.0,
    )
    assert result["generic"]["searches"] == 1 and result["generic"]["p@5"] == 1.0


# ── owner rule: filter, don't regrade (only what the product would show) ──


def test_dm_pool_runs_the_pipelines_own_filters(monkeypatch):
    import gosha.pipeline as pipeline

    postings = [posting(("py",), "intern", pid="a", text="Software Engineering Intern")]
    search = ev.DmSearch("computer science internship", ("intern",))
    assert ev.dm_pool(postings, search).tolist() == [True]
    monkeypatch.setattr(pipeline, "prefilter", lambda sub, jobs: [])
    assert ev.dm_pool(postings, search).tolist() == [False]


def test_place_filter_runs_only_when_the_set_records_places():
    def placed(pid, location):
        return replace(posting(("py",), "intern", pid=pid, text="Software Engineering Intern"),
                       location=location, location_known=True)

    search = ev.DmSearch("computer science internship", ("intern",))
    postings = [placed("a", "Cluj-Napoca"), placed("b", "Bucuresti"), placed("c", None)]
    assert ev.locations_applied(postings)
    # As production: another city and a posting without a place are out.
    assert ev.dm_pool(postings, search).tolist() == [True, False, False]
    unplaced = [posting(("py",), "intern", pid="a", text="Software Engineering Intern")]
    assert not ev.locations_applied(unplaced)
    assert ev.dm_pool(unplaced, search).tolist() == [True]


def test_load_postings_reads_the_place_when_the_set_has_one(tmp_path):
    row = {"id": "a", "source": "s", "lang": "en", "families": ["py"], "level": "junior",
           "title": "t", "company": "c", "description": "d"}
    path = tmp_path / "postings.jsonl"
    path.write_text(json.dumps(row) + "\n" + json.dumps({**row, "id": "b", "location": None}) + "\n")
    a, b = ev.load_postings(path)
    assert not a.location_known and b.location_known and b.location is None


def test_low_n_is_a_short_page_or_few_relevant():
    assert ev.is_low_n(ev.LOW_N_POSTINGS - 1, 50)
    assert ev.is_low_n(100, ev.LOW_N_RELEVANT - 1)
    assert not ev.is_low_n(ev.LOW_N_POSTINGS, ev.LOW_N_RELEVANT)


def test_dm_rows_flag_low_n_searches_and_keep_them():
    encode = hashing_encoder()
    postings = [posting(("py",), "intern", pid="a", text="Software Engineering Intern")]
    search = ev.DmSearch("python developer", ("intern",), family="py", persona="u1")
    result = ev.evaluate_dm_searches(
        postings, [search], encode([p.title for p in postings]), encode([search.query]), -1.0,
    )
    (row,) = result["searches"]
    assert row["persona"] == "u1" and row["low_n"]
    assert result["specific"]["low_n"] == 1 and result["specific"]["searches"] == 1


def test_scoped_feed_pool_is_the_feeds_scope_filter(monkeypatch):
    import gosha.recommend as recommend

    postings = [
        posting(("py",), "intern", pid="a", text="Software Engineering Intern"),
        posting(("py",), "senior", pid="b", text="Senior Software Engineer"),
    ]
    assert ev.scoped_feed_pool(postings, ("intern", "junior")) == [0]
    monkeypatch.setattr(recommend, "in_search_scope", lambda job, scopes, memo=None: True)
    assert ev.scoped_feed_pool(postings, ("intern", "junior")) == [0, 1]


def test_feed_p_at_10_is_over_the_page_shown():
    assert ev.feed_metrics(np.array([0.9, 0.5]), np.array([2, 0]))["p@10"] == 0.5


def test_scoped_feed_counts_what_is_left_and_who_has_nothing(small_world):
    postings, personas = small_world
    result = ev.run(postings, personas, hashing_encoder(), with_feedback=False)
    scoped = result["feed_scoped"]
    assert set(scoped) == {"production", "cosine"}
    junior = scoped["production"]["junior"]
    assert junior["remaining"] == len(ev.scoped_feed_pool(postings, ("junior",)))
    assert junior["personas"] == 2 and junior["low_n"] == 2  # a 7-posting world is low-n
    for persona, row in junior["per_persona"].items():
        assert ("ndcg@10" in row) == (row["relevant"] > 0), persona
    assert junior["measured"] + junior["no_relevant"] == 2
    json.dumps(result)


# ── the feed shows one copy of a posting (gosha.recommend.first_copies) ──


def _copies():
    """Two city copies of one posting (same title and employer), one other."""
    return [
        posting(pid="cluj", text="Graphics Engineer"),
        posting(pid="tm", text="Graphics Engineer"),
        posting(("acc",), pid="other", text="Accountant"),
    ]


def test_feed_metrics_count_one_copy_of_a_posting():
    scores, gains = np.array([0.9, 0.8, 0.1]), np.array([2, 2, 0])
    assert ev.feed_metrics(scores, gains)["p@10"] == pytest.approx(2 / 3)
    collapsed = ev.feed_metrics(scores, gains, _copies())
    assert collapsed["p@10"] == 0.5 and collapsed["r_precision"] == 1.0


def test_a_rated_copy_hides_its_twin_in_the_feedback_simulation():
    assert ev._unrated(3, {0}, _copies()) == [2]
    assert ev._unrated(3, {0}, ()) == [1, 2]


def test_scoped_feed_drops_courses_but_not_postings_without_entry_evidence():
    team = "You will build data pipelines in Python and SQL with our team in Cluj. " * 4
    postings = [
        posting(("py",), "junior", pid="grad", text="Data Analyst"),
        posting(("py",), "junior", pid="course", text="Curs Consultant ERP"),
        posting(("py",), "junior", pid="silent", text="Data Engineer"),
    ]
    postings = [
        replace(postings[0], description=team + "Recent graduates are welcome."),
        replace(postings[1], description="Acesta nu este un anunț de angajare. " + team),
        replace(postings[2], description=team),
    ]
    # The course is out; the posting without entry evidence stays (the
    # browse feed does not fail closed as DMs do).
    assert ev.scoped_feed_pool(postings, ("intern", "junior")) == [0, 2]
    assert ev.scoped_feed_pool(postings, ("any",)) == [0, 2]


def test_dm_metrics_count_a_postings_copies_once():
    """Two copies of one posting above the threshold are one DM, the
    better-scoring copy (pipeline.deliver_pending)."""
    models = [ev.as_job(p) for p in _copies()]
    scores, pool = np.array([0.5, 0.6, 0.4]), np.array([True, True, True])
    relevant = np.array([True, True, False])
    plain = ev.dm_search_metrics(scores, pool, relevant, 0.3)
    once = ev.dm_search_metrics(scores, pool, relevant, 0.3, models)
    assert (plain["sent"], plain["relevant_in_pool"]) == (3, 2)
    assert (once["sent"], once["relevant_in_pool"], once["recall"]) == (2, 1, 1.0)
    assert ev.one_dm_per_posting(scores, pool, models).tolist() == [False, True, True]
