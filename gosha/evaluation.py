"""Offline ranking evaluation: graded relevance, metrics and the harness.

The harness scores labelled postings (eval/postings.jsonl) against
synthetic CV personas (eval/personas.jsonl) through the production code
path — the same job text, CV chunking and pooling, Discord query blending
and Rocchio update the bot and API use — so the numbers describe what
ships, and only the encoder varies. It takes an `encode` callable, which
lets the tests drive it with a fake encoder and no torch.
scripts/eval_ranking.py is the command-line front end; docs/EVAL.md has
the method and the results.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

Encode = Callable[[list[str]], np.ndarray]

# ── Labels ──────────────────────────────────────────────────────────────
#
# Each posting carries one or more role families and a seniority level,
# assigned by reading it (docs/EVAL.md says who labelled what). A
# persona's grade for a posting follows from those labels:
#
#   2  same family, at a level a student can apply to
#   1  same family but senior/lead, or an adjacent family at an
#      applicable level (a Python student shown a Java junior role)
#   0  everything else
#
# The rule lives in code rather than in a hand-written qrels file so it
# can be read, tested and changed in one place.

FAMILIES = {
    "py": "Python backend",
    "java": "Java / JVM backend",
    "dotnet": "C# / .NET",
    "be": "other backend (Node, PHP, generic)",
    "fe": "frontend",
    "fs": "full-stack",
    "mob": "mobile",
    "ml": "machine learning / AI",
    "data": "data engineering / analytics / BI",
    "emb": "embedded / C / C++ / hardware",
    "ops": "DevOps / cloud / SRE",
    "qa": "QA / testing",
    "sec": "security",
    "support": "IT support",
    "ba": "business analysis / delivery / management",
    "acc": "accounting / finance",
    "sales": "sales",
    "cs": "customer service",
    "other": "anything else",
}

ADJACENT: dict[str, frozenset[str]] = {
    "py": frozenset({"be", "java", "dotnet", "fs", "ml", "data", "ops"}),
    "java": frozenset({"be", "py", "dotnet", "fs"}),
    "dotnet": frozenset({"be", "java", "py", "fs"}),
    "fe": frozenset({"fs", "mob"}),
    "mob": frozenset({"fe", "fs"}),
    "ml": frozenset({"data", "py"}),
    "data": frozenset({"ml", "ba"}),
    "emb": frozenset(),
    "ops": frozenset({"support", "sec", "be"}),
    "qa": frozenset(),
    "acc": frozenset(),
    "cs": frozenset({"sales", "support"}),
}

LEVELS = ("intern", "junior", "any", "mid", "senior")
# Levels a student persona would apply to. "mid" (2-4 years asked) is in:
# students do apply to those, and the boards have few true junior roles.
APPLICABLE_LEVELS = frozenset({"intern", "junior", "any", "mid"})


@dataclass(frozen=True)
class Posting:
    id: str
    source: str
    lang: str
    families: tuple[str, ...]
    level: str
    title: str
    company: str
    description: str
    url: str = ""


@dataclass(frozen=True)
class Persona:
    id: str
    lang: str
    family: str
    keyword: str
    cv: str


def load_postings(path: Path) -> list[Posting]:
    postings = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        posting = Posting(
            id=row["id"], source=row["source"], lang=row["lang"],
            families=tuple(row["families"]), level=row["level"],
            title=row["title"], company=row["company"],
            description=row["description"], url=row.get("url", ""),
        )
        unknown = set(posting.families) - FAMILIES.keys()
        if unknown or posting.level not in LEVELS:
            raise ValueError(f"{posting.id}: bad label {unknown or posting.level}")
        postings.append(posting)
    return postings


def load_personas(path: Path) -> list[Persona]:
    personas = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            persona = Persona(**json.loads(line))
            if persona.family not in ADJACENT:
                raise ValueError(f"{persona.id}: no adjacency rule for {persona.family}")
            personas.append(persona)
    return personas


def grade(persona_family: str, posting: Posting) -> int:
    applicable = posting.level in APPLICABLE_LEVELS
    if persona_family in posting.families:
        return 2 if applicable else 1
    if applicable and ADJACENT[persona_family] & set(posting.families):
        return 1
    return 0


# ── Metrics ─────────────────────────────────────────────────────────────


def ranking(scores: Sequence[float]) -> list[int]:
    """Indices by descending score; ties keep input order (stable)."""
    return sorted(range(len(scores)), key=lambda i: -scores[i])


def dcg(gains: Sequence[float]) -> float:
    return sum((2 ** g - 1) / math.log2(rank + 2) for rank, g in enumerate(gains))


def ndcg_at(ranked_gains: Sequence[int], k: int = 10) -> float:
    """nDCG@k with exponential gain; `ranked_gains` is the whole list in
    ranked order (the ideal ordering is computed from the same items)."""
    ideal = dcg(sorted(ranked_gains, reverse=True)[:k])
    return dcg(ranked_gains[:k]) / ideal if ideal else 0.0


def r_precision(ranked_gains: Sequence[int], relevant: int = 2) -> float:
    """Share of the top R that are grade `relevant`, R = how many exist."""
    total = sum(1 for g in ranked_gains if g >= relevant)
    if total == 0:
        return 0.0
    return sum(1 for g in ranked_gains[:total] if g >= relevant) / total


def reciprocal_rank(ranked_gains: Sequence[int], relevant: int = 2) -> float:
    for rank, g in enumerate(ranked_gains, start=1):
        if g >= relevant:
            return 1.0 / rank
    return 0.0


def bootstrap_ci(
    values: Sequence[float], resamples: int = 2000, seed: int = 0,
) -> tuple[float, float]:
    """95% percentile bootstrap interval of the mean (seeded: reproducible)."""
    data = np.asarray(values, dtype=np.float64)
    if len(data) == 0:
        return (0.0, 0.0)
    rng = np.random.default_rng(seed)
    means = data[rng.integers(0, len(data), size=(resamples, len(data)))].mean(axis=1)
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


# ── Keyword baseline ────────────────────────────────────────────────────

_TOKEN = re.compile(r"[^\W\d_][\w+#.]*", re.UNICODE)


def _terms(text: str) -> list[str]:
    return [t.rstrip(".").lower() for t in _TOKEN.findall(text) if len(t) > 1]


def tfidf_encoder(corpus: Sequence[str]) -> Encode:
    """A sublinear TF-IDF encoder fitted on `corpus`, as unit vectors.

    The baseline an embedding model has to beat: no dependency beyond
    numpy, so it also runs where torch is not installed.
    """
    docs = [Counter(_terms(text)) for text in corpus]
    vocab = {term: i for i, term in enumerate(sorted({t for d in docs for t in d}))}
    df = np.zeros(len(vocab))
    for d in docs:
        for term in d:
            df[vocab[term]] += 1
    idf = np.log((1 + len(docs)) / (1 + df)) + 1

    def encode(texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), len(vocab)), dtype=np.float32)
        for row, text in enumerate(texts):
            for term, count in Counter(_terms(text)).items():
                if term in vocab:
                    out[row, vocab[term]] = (1 + math.log(count)) * idf[vocab[term]]
            norm = np.linalg.norm(out[row])
            if norm:
                out[row] /= norm
        return out

    return encode


# ── Harness ─────────────────────────────────────────────────────────────

DM_THRESHOLDS = (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60)
FEEDBACK_WEIGHTS = ((0.0, 0.0), (0.15, 0.15), (0.3, 0.3), (0.5, 0.5), (0.3, 0.0), (0.5, 0.15))
FEEDBACK_ROUNDS = (1, 3)
SCORE_BANDS = (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60)


@dataclass
class Vectors:
    jobs: np.ndarray          # (P, D) production job text vectors
    cvs: np.ndarray           # (U, D) chunked + pooled CV vectors
    queries: np.ndarray       # (U, D) Discord subscription queries
    gains: np.ndarray         # (U, P) graded relevance


def job_text(posting: Posting, variant: str = "production") -> str:
    """The text a posting is embedded as.

    "production" is gosha.matching.build_job_text (title twice, company,
    first 1,500 description characters). "title" drops the description:
    a probe of how much the description helps, given that many postings
    open with company boilerplate.
    """
    from gosha.matching import build_job_text

    if variant == "production":
        return build_job_text(posting.title, posting.company, posting.description)
    if variant == "title":
        return build_job_text(posting.title, posting.company, None)
    raise ValueError(f"unknown job text variant {variant!r}")


def encode_all(
    postings: Sequence[Posting],
    personas: Sequence[Persona],
    encode: Encode,
    query_location: str | None = "Cluj",
    job_text_variant: str = "production",
) -> Vectors:
    from gosha.embeddings import embed_long_text
    from gosha.matching import build_subscription_query

    jobs = np.asarray(encode([
        job_text(p, job_text_variant) for p in postings
    ]), dtype=np.float32)
    cvs = np.stack([embed_long_text(u.cv, encode) for u in personas]).astype(np.float32)
    queries = np.asarray(encode([
        build_subscription_query([u.keyword], [query_location] if query_location else [])
        for u in personas
    ]), dtype=np.float32)
    gains = np.array([[grade(u.family, p) for p in postings] for u in personas])
    return Vectors(jobs, cvs, queries, gains)


def feed_metrics(scores: np.ndarray, gains: np.ndarray) -> dict[str, float]:
    ranked = [int(gains[i]) for i in ranking(scores.tolist())]
    return {
        "ndcg@10": ndcg_at(ranked, 10),
        "r_precision": r_precision(ranked),
        "mrr": reciprocal_rank(ranked),
        "p@10": sum(1 for g in ranked[:10] if g == 2) / 10,
    }


def _mean_metrics(rows: list[dict[str, float]]) -> dict[str, float]:
    return {k: float(np.mean([r[k] for r in rows])) for k in rows[0]}


def evaluate_feed(
    vectors: Vectors, postings: Sequence[Posting], personas: Sequence[Persona],
) -> dict:
    """The web feed: every posting ranked by cosine to the CV vector."""
    per_persona = {}
    by_posting_lang: dict[str, list[dict[str, float]]] = {"en": [], "ro": []}
    for u, persona in enumerate(personas):
        scores = vectors.jobs @ vectors.cvs[u]
        per_persona[persona.id] = feed_metrics(scores, vectors.gains[u])
        # Same persona, one posting language at a time: does a relevant
        # Romanian posting rank as well as an English one?
        for lang in by_posting_lang:
            idx = [i for i, p in enumerate(postings) if p.lang == lang]
            if any(vectors.gains[u, i] == 2 for i in idx):
                by_posting_lang[lang].append(
                    feed_metrics(scores[idx], vectors.gains[u, idx]))
    ndcgs = [m["ndcg@10"] for m in per_persona.values()]
    rows = list(per_persona.values())
    return {
        "mean": _mean_metrics(rows),
        "ndcg@10_ci95": bootstrap_ci(ndcgs),
        "by_cv_lang": {
            lang: _mean_metrics([per_persona[p.id] for p in personas if p.lang == lang])
            for lang in sorted({p.lang for p in personas})
        },
        "by_posting_lang": {
            lang: {**_mean_metrics(rows_), "personas": len(rows_)}
            for lang, rows_ in by_posting_lang.items() if rows_
        },
        "per_persona": per_persona,
    }


def dm_scores(vectors: Vectors, with_cv: bool) -> np.ndarray:
    """(U, P) Discord delivery scores, as gosha/pipeline.py computes them."""
    from gosha.pipeline import blend_with_user_vector

    rows = []
    for u in range(len(vectors.queries)):
        query = vectors.queries[u]
        if with_cv:
            query = blend_with_user_vector(query, vectors.cvs[u])
        rows.append(vectors.jobs @ query)
    return np.stack(rows)


def threshold_sweep(
    scores: np.ndarray, gains: np.ndarray, thresholds: Sequence[float] = DM_THRESHOLDS,
) -> list[dict[str, float]]:
    """Pooled over personas: what a delivery threshold lets through."""
    out = []
    relevant = gains == 2
    off_target = gains == 0
    for t in thresholds:
        passed = scores >= t
        n_passed = int(passed.sum())
        out.append({
            "threshold": t,
            "recall": float((passed & relevant).sum() / max(1, relevant.sum())),
            "off_target_pass_rate": float((passed & off_target).sum() / max(1, off_target.sum())),
            "precision": float((passed & relevant).sum() / n_passed) if n_passed else 0.0,
            "precision_any": float((passed & (gains >= 1)).sum() / n_passed) if n_passed else 0.0,
            "per_persona_passed": n_passed / scores.shape[0],
        })
    return out


def score_bands(
    scores: np.ndarray, gains: np.ndarray, edges: Sequence[float] = SCORE_BANDS,
) -> list[dict]:
    """How often a delivery score in each band is a grade 2 / 1 / 0 posting."""
    bounds = [-1.0, *edges, 2.0]
    out = []
    for lo, hi in zip(bounds, bounds[1:]):
        mask = (scores >= lo) & (scores < hi)
        n = int(mask.sum())
        out.append({
            "from": lo, "to": hi, "n": n,
            "grade2": float((gains[mask] == 2).mean()) if n else 0.0,
            "grade1": float((gains[mask] == 1).mean()) if n else 0.0,
            "grade0": float((gains[mask] == 0).mean()) if n else 0.0,
        })
    return out


def simulate_feedback(
    vectors: Vectors,
    weights: Sequence[tuple[float, float]] = FEEDBACK_WEIGHTS,
    rounds: Sequence[int] = FEEDBACK_ROUNDS,
    shown: int = 10,
) -> list[dict]:
    """Residual-collection test of the Rocchio update (recommend.apply_feedback).

    Each round the simulated user sees the top `shown` unrated postings,
    likes every grade-2 one and dislikes every grade-0 one. After the last
    round, nDCG@10 over the postings still unrated is compared with the
    CV-only ranking of the same postings, so rated items cannot flatter
    either side.
    """
    from gosha.recommend import apply_feedback

    out = []
    for like_w, dislike_w in weights:
        for n_rounds in rounds:
            before, after = [], []
            for u in range(len(vectors.cvs)):
                gains = vectors.gains[u]
                base = vectors.cvs[u]
                rated: set[int] = set()
                liked: list[np.ndarray] = []
                disliked: list[np.ndarray] = []
                vector = base
                for _ in range(n_rounds):
                    scores = vectors.jobs @ vector
                    order = [i for i in ranking(scores.tolist()) if i not in rated][:shown]
                    for i in order:
                        rated.add(i)
                        if gains[i] == 2:
                            liked.append(vectors.jobs[i])
                        elif gains[i] == 0:
                            disliked.append(vectors.jobs[i])
                    updated = apply_feedback(base, liked, disliked, like_w, dislike_w)
                    vector = updated if updated is not None else base
                rest = [i for i in range(len(gains)) if i not in rated]
                if not any(gains[i] == 2 for i in rest):
                    continue
                before.append(feed_metrics(vectors.jobs[rest] @ base, gains[rest])["ndcg@10"])
                after.append(feed_metrics(vectors.jobs[rest] @ vector, gains[rest])["ndcg@10"])
            row = {"like_weight": like_w, "dislike_weight": dislike_w,
                   "rounds": n_rounds, "personas": len(before)}
            if before:  # else every relevant posting was already rated
                delta = np.asarray(after) - np.asarray(before)
                row |= {
                    "ndcg@10_before": float(np.mean(before)),
                    "ndcg@10_after": float(np.mean(after)),
                    "delta_ci95": bootstrap_ci(delta.tolist()),
                }
            out.append(row)
    return out


def production_thresholds() -> tuple[float, float]:
    """(search only, with a CV) delivery thresholds the bot uses by default."""
    from gosha.matching import DEFAULT_CV_THRESHOLD, DEFAULT_THRESHOLD

    return DEFAULT_THRESHOLD, DEFAULT_CV_THRESHOLD


def run(
    postings: Sequence[Posting],
    personas: Sequence[Persona],
    encode: Encode,
    *,
    query_location: str | None = "Cluj",
    with_feedback: bool = True,
    job_text_variant: str = "production",
) -> dict:
    """Every measurement for one encoder, as plain JSON-able data."""
    vectors = encode_all(postings, personas, encode, query_location, job_text_variant)
    dm_cv = dm_scores(vectors, with_cv=True)
    dm_search = dm_scores(vectors, with_cv=False)
    thresholds = sorted({*DM_THRESHOLDS, *production_thresholds()})
    result = {
        "postings": len(postings),
        "personas": len(personas),
        "feed": evaluate_feed(vectors, postings, personas),
        "dm_with_cv": {
            "sweep": threshold_sweep(dm_cv, vectors.gains, thresholds),
            "bands": score_bands(dm_cv, vectors.gains),
        },
        "dm_search_only": {
            "sweep": threshold_sweep(dm_search, vectors.gains, thresholds),
            "bands": score_bands(dm_search, vectors.gains),
        },
        "cosine": {
            "feed_grade2_mean": float((vectors.jobs @ vectors.cvs.T).T[vectors.gains == 2].mean()),
            "feed_grade0_mean": float((vectors.jobs @ vectors.cvs.T).T[vectors.gains == 0].mean()),
        },
    }
    if with_feedback:
        result["feedback"] = simulate_feedback(vectors)
    return result
