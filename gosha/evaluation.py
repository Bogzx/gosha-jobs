"""Offline ranking evaluation: graded relevance, metrics and the harness.

The harness scores labelled postings (eval/postings.jsonl) against
synthetic CV personas (eval/personas.jsonl) through the production code
path — the same job text, CV chunking and pooling, feed scoring
(recommend.hybrid_scores over the embedding cosine, gosha.lexical and
gosha.seniority), Discord query blending and Rocchio update the bot and
API use — so the numbers describe what ships, and only the encoder
varies. It takes an `encode` callable, which
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
    # The board's place, when the set records it ("location" key present;
    # None there is a posting without one, which a place filter rejects).
    location: str | None = None
    location_known: bool = False


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
            location=row.get("location"), location_known="location" in row,
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


def _order(scores: np.ndarray) -> list[int]:
    return ranking(np.asarray(scores).tolist())


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


# How the feed orders postings. "production" is what gosha/recommend.py
# ships; "cosine" is the embedding alone (the ranking before 2026-10-16),
# kept so the two can be compared.
FEED_RANKINGS = ("production", "cosine")


@dataclass
class Vectors:
    jobs: np.ndarray          # (P, D) production job text vectors
    cvs: np.ndarray           # (U, D) chunked + pooled CV vectors
    queries: np.ndarray       # (U, D) Discord subscription queries
    gains: np.ndarray         # (U, P) graded relevance
    keyword: np.ndarray       # (U, P) gosha.lexical TF-IDF cosine, CV vs job text
    penalties: np.ndarray     # (U, P) gosha.seniority level penalty
    # The postings themselves, for what the feed collapses (as_shown()).
    postings: Sequence[Posting] = ()


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
    keyword, penalties = ranking_signals(postings, personas, job_text_variant)
    return Vectors(jobs, cvs, queries, gains, keyword, penalties, list(postings))


def ranking_signals(
    postings: Sequence[Posting], personas: Sequence[Persona],
    job_text_variant: str = "production",
) -> tuple[np.ndarray, np.ndarray]:
    """(keyword, penalties), each (U, P): what the feed adds to the cosine.

    The same calls gosha/recommend.py makes: a TF-IDF index over the job
    texts (fitted on the postings only, as in production), and the gap
    between the CV's level and each posting's.
    """
    from gosha import lexical, seniority

    index = lexical.LexicalIndex([job_text(p, job_text_variant) for p in postings])
    keyword = np.stack([index.score(u.cv) for u in personas]) if personas else (
        np.zeros((0, len(postings))))
    levels = [seniority.posting_level(p.title, p.description) for p in postings]
    penalties = np.array([
        [seniority.level_penalty(seniority.cv_level(u.cv), level) for level in levels]
        for u in personas
    ], dtype=np.float64).reshape(len(personas), len(postings))
    return keyword.astype(np.float64), penalties


def feed_scores(
    vectors: Vectors, u: int, vector: np.ndarray | None = None,
    idx: Sequence[int] | None = None, ranking: str = "production",
) -> np.ndarray:
    """Persona `u`'s feed scores for postings `idx` (default: all).

    `vector` replaces the CV vector (after feedback). The z-scores in
    hybrid_scores are taken over the postings scored, as production takes
    them over the user's candidates (rated postings excluded).
    """
    from gosha.recommend import hybrid_scores

    vector = vectors.cvs[u] if vector is None else vector
    sel = slice(None) if idx is None else list(idx)
    cosines = vectors.jobs[sel] @ vector
    if ranking == "cosine":
        return cosines
    if ranking != "production":
        raise ValueError(f"unknown feed ranking {ranking!r}")
    return hybrid_scores(cosines, vectors.keyword[u, sel], vectors.penalties[u, sel])


def as_shown(order: Sequence[int], postings: Sequence[Posting] = ()) -> list[int]:
    """`order` (indices into `postings`) as the feed shows it: one copy of
    each posting, the first (gosha.recommend.first_copies). Without the
    postings, `order` unchanged."""
    if not postings:
        return list(order)
    from gosha.recommend import first_copies

    return [order[k] for k in first_copies([postings[i] for i in order])]


def feed_metrics(
    scores: np.ndarray, gains: np.ndarray, postings: Sequence[Posting] = (),
) -> dict[str, float]:
    """`postings` (aligned with `scores`) collapse copies as the feed does.
    P@10 is over the first page: ten postings, or all of them when a
    filtered feed has fewer."""
    ranked = [int(gains[i]) for i in as_shown(ranking(scores.tolist()), postings)]
    return {
        "ndcg@10": ndcg_at(ranked, 10),
        "r_precision": r_precision(ranked),
        "mrr": reciprocal_rank(ranked),
        "p@10": sum(1 for g in ranked[:10] if g == 2) / min(10, len(ranked)) if ranked else 0.0,
    }


def _mean_metrics(rows: list[dict[str, float]]) -> dict[str, float]:
    return {k: float(np.mean([r[k] for r in rows])) for k in rows[0]}


def evaluate_feed(
    vectors: Vectors, postings: Sequence[Posting], personas: Sequence[Persona],
    ranking: str = "production",
) -> dict:
    """The web feed: every posting ranked for each persona's CV."""
    per_persona = {}
    by_posting_lang: dict[str, list[dict[str, float]]] = {"en": [], "ro": []}
    for u, persona in enumerate(personas):
        scores = feed_scores(vectors, u, ranking=ranking)
        per_persona[persona.id] = feed_metrics(scores, vectors.gains[u], postings)
        # Same persona, one posting language at a time: does a relevant
        # Romanian posting rank as well as an English one?
        for lang in by_posting_lang:
            idx = [i for i, p in enumerate(postings) if p.lang == lang]
            if any(vectors.gains[u, i] == 2 for i in idx):
                by_posting_lang[lang].append(
                    feed_metrics(scores[idx], vectors.gains[u, idx], [postings[i] for i in idx]))
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


def _at(vectors: Vectors, idx: Sequence[int]) -> list[Posting]:
    return [vectors.postings[i] for i in idx] if vectors.postings else []


def _unrated(n: int, rated: set[int], postings: Sequence[Posting]) -> list[int]:
    """The postings a feed still shows after these ratings: a rated
    posting hides its other copies too (gosha.recommend.get_feed_page)."""
    from gosha.recommend import posting_key

    keys = {posting_key(postings[i]) for i in rated} - {None} if postings else set()
    return [
        i for i in range(n)
        if i not in rated and not (keys and posting_key(postings[i]) in keys)
    ]


def simulate_feedback(
    vectors: Vectors,
    weights: Sequence[tuple[float, float]] = FEEDBACK_WEIGHTS,
    rounds: Sequence[int] = FEEDBACK_ROUNDS,
    shown: int = 10,
    ranking: str = "production",
) -> list[dict]:
    """Residual-collection test of the Rocchio update (recommend.apply_feedback).

    Each round the simulated user sees the top `shown` unrated postings,
    likes every grade-2 one and dislikes every grade-0 one. After the last
    round, nDCG@10 over the postings still unrated is compared with the
    CV-only ranking of the same postings, so rated items cannot flatter
    either side. Feedback moves only the embedding vector; under the
    production ranking the keyword and seniority terms stay as they were.
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
                    unrated = _unrated(len(gains), rated, vectors.postings)
                    scores = feed_scores(vectors, u, vector, unrated, ranking)
                    order = [unrated[i] for i in as_shown(_order(scores), _at(vectors, unrated))]
                    order = order[:shown]
                    for i in order:
                        rated.add(i)
                        if gains[i] == 2:
                            liked.append(vectors.jobs[i])
                        elif gains[i] == 0:
                            disliked.append(vectors.jobs[i])
                    updated = apply_feedback(base, liked, disliked, like_w, dislike_w)
                    vector = updated if updated is not None else base
                rest = _unrated(len(gains), rated, vectors.postings)
                if not any(gains[i] == 2 for i in rest):
                    continue
                before.append(feed_metrics(
                    feed_scores(vectors, u, base, rest, ranking), gains[rest],
                    _at(vectors, rest))["ndcg@10"])
                after.append(feed_metrics(
                    feed_scores(vectors, u, vector, rest, ranking), gains[rest],
                    _at(vectors, rest))["ndcg@10"])
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


# ── Discord DMs as users search ─────────────────────────────────────────
#
# dm_scores above scores every persona's own specific search against every
# posting, before any filter. Real Discord searches look different: the
# words are generic ("computer science internship"), every one names a
# level (intern and/or junior), almost no user has a CV, and the match
# stage drops postings at the wrong level or with a non-tech title before
# a score is ever computed. What a user reads is the first few postings
# of a cycle that clear the threshold, best first (MAX_DMS_PER_USER, and a
# per-search cap). This part measures that.

# What a "computer science" search is after: every software, data, ops,
# security and IT-support family. Business analysis, sales, accounting,
# customer service and "other" are not.
CS_FAMILIES = frozenset({
    "py", "java", "dotnet", "be", "fe", "fs", "mob", "ml", "data", "emb", "ops", "qa",
    "sec", "support",
})
# Searches as production users type them (Discord subscriptions,
# 2026-10-16), and the level sets they choose.
GENERIC_DM_KEYWORDS = ("computer science internship", "cs entry level", "computer science intern")
DM_LEVEL_SETS: tuple[tuple[str, ...], ...] = (("intern",), ("intern", "junior"), ("junior",))
# Gold posting levels each requested level stands for. "any" (the posting
# names no level) is what a junior search is for, not an intern search.
_WANTED_GOLD = {"intern": {"intern"}, "junior": {"junior", "any"}}
DM_LOCATION = "Cluj"
# Owner, 2026-10-16: filter, don't regrade. The eval scores only what the
# product would show (its own filters first); a search or persona left
# with fewer postings than a page, or fewer relevant ones than this, is
# flagged as low-n and kept in the means.
LOW_N_POSTINGS = 10
LOW_N_RELEVANT = 3


def is_low_n(remaining: int, relevant: int) -> bool:
    return remaining < LOW_N_POSTINGS or relevant < LOW_N_RELEVANT


def locations_applied(postings: Sequence[Posting]) -> bool:
    """Whether the place filter can run: every posting records a place.
    Until the set does, searches look anywhere (and the output says so)."""
    return bool(postings) and all(p.location_known for p in postings)


def _search_locations(postings: Sequence[Posting]) -> list[str]:
    return [DM_LOCATION] if locations_applied(postings) else []


def as_job(posting: Posting):
    """A transient gosha.models.Job, so the product's filters read the
    posting exactly as they read a stored one."""
    from gosha.models import Job

    return Job(
        title=posting.title, company=posting.company, description=posting.description,
        location=posting.location, experience_level=None, url=posting.url or None,
    )


@dataclass(frozen=True)
class DmSearch:
    keyword: str
    levels: tuple[str, ...]
    family: str | None = None  # None: a generic computer-science search
    persona: str | None = None  # the persona whose keyword this is

    @property
    def query(self) -> str:
        from gosha.matching import build_subscription_query

        return build_subscription_query([self.keyword], [DM_LOCATION], list(self.levels))


def dm_searches(personas: Sequence[Persona] = ()) -> list[DmSearch]:
    """The generic searches at every level set, then each persona's own
    keyword (a specific search, no CV) at every level set."""
    out = [DmSearch(k, levels) for k in GENERIC_DM_KEYWORDS for levels in DM_LEVEL_SETS]
    out += [DmSearch(u.keyword, levels, u.family, u.id) for u in personas for levels in DM_LEVEL_SETS]
    return out


def as_subscription(search: DmSearch, locations: Sequence[str]):
    """A transient gosha.models.Subscription for this search: no age,
    salary, company or word exclusions (production searches set none)."""
    from gosha.models import Subscription

    return Subscription(
        keywords=[search.keyword], locations=list(locations), remote_ok=False,
        experience_levels=list(search.levels), excluded_keywords=[], company_blacklist=[],
        salary_min=None, max_age_days=None,
    )


def dm_pool(
    postings: Sequence[Posting], search: DmSearch, jobs: Sequence | None = None,
) -> np.ndarray:
    """Postings the match stage would score for this search:
    gosha.pipeline.prefilter, what match_jobs_for_subscription runs before
    scoring. The place filter runs when the set records places
    (locations_applied)."""
    from gosha.pipeline import prefilter

    jobs = [as_job(p) for p in postings] if jobs is None else jobs
    kept = {id(job) for job in prefilter(as_subscription(search, _search_locations(postings)), jobs)}
    return np.array([id(job) in kept for job in jobs], dtype=bool)


def dm_relevant(postings: Sequence[Posting], search: DmSearch) -> np.ndarray:
    """Postings this search is for: the right field at a level it asked for."""
    wanted = set().union(*(_WANTED_GOLD.get(level, set()) for level in search.levels))
    return np.array([
        p.level in wanted
        and (bool(CS_FAMILIES & set(p.families)) if search.family is None
             else search.family in p.families)
        for p in postings
    ], dtype=bool)


def one_dm_per_posting(scores: np.ndarray, pool: np.ndarray, models: Sequence) -> np.ndarray:
    """The pool with each posting once, as one user's batch is delivered:
    best first, a copy of a posting already kept is skipped
    (gosha.pipeline._same_posting_for_dm, deliver_pending; the match
    stage's group check, _one_per_duplicate_group, keeps the best-scoring
    copy of a group too, and its rule is within this one)."""
    from gosha.pipeline import _same_posting_for_dm

    keep = np.zeros(len(pool), dtype=bool)
    kept: list[int] = []
    for i in np.argsort(-np.where(pool, scores, -np.inf), kind="stable")[: int(pool.sum())]:
        if not any(_same_posting_for_dm(models[i], models[j]) for j in kept):
            kept.append(int(i))
            keep[i] = True
    return keep


def dm_search_metrics(
    scores: np.ndarray, pool: np.ndarray, relevant: np.ndarray, threshold: float,
    models: Sequence | None = None,
) -> dict[str, float]:
    """One search: what it is sent, best first, and how much of it fits.

    With `models` (gosha.models.Job per posting) a posting's copies count
    once (one_dm_per_posting), in the pool and in what is sent.
    precision@k is over the first k postings sent (fewer if fewer pass);
    a search with relevant postings in its pool that is sent none of them
    is starved.
    """
    if models is not None:
        pool = one_dm_per_posting(scores, pool, models)
    sent = pool & (scores >= threshold)
    order = np.argsort(-np.where(sent, scores, -np.inf), kind="stable")[: int(sent.sum())]
    hits = relevant[order]
    in_pool = int((pool & relevant).sum())
    return {
        "pool": int(pool.sum()),
        "relevant_in_pool": in_pool,
        "sent": int(sent.sum()),
        "p@5": float(hits[:5].mean()) if len(hits) else math.nan,
        "p@10": float(hits[:10].mean()) if len(hits) else math.nan,
        "recall": float(hits.sum() / in_pool) if in_pool else math.nan,
        "starved": bool(in_pool and not hits.any()),
    }


def dm_query_vectors(
    searches: Sequence[DmSearch], encode: Encode, hub_alpha: float = 0.0,
) -> np.ndarray:
    """(S, D) query vectors; with hub_alpha, minus that share of the mean
    of the matching panel queries (gosha.matching.dm_panel_queries)."""
    from gosha.matching import dm_panel_queries, dm_query_vector

    queries = np.asarray(encode([s.query for s in searches]), dtype=np.float32)
    if not hub_alpha:
        return queries
    panels = {
        levels: np.asarray(encode(dm_panel_queries([DM_LOCATION], list(levels))), dtype=np.float32)
        for levels in {s.levels for s in searches}
    }
    return np.stack([
        dm_query_vector(q, panels[s.levels], hub_alpha) for q, s in zip(queries, searches)
    ])


def dm_term_query_vectors(
    searches: Sequence[DmSearch], encode: Encode, hub_alpha: float,
) -> list[np.ndarray]:
    """Per search, (T, D) DM query vectors, one per term the search is
    scored through (gosha.matching.dm_role_terms), each minus hub_alpha
    times the matching panel mean, as dm_query_vectors does for one."""
    from gosha.matching import dm_role_terms

    out = []
    for search in searches:
        terms = [DmSearch(t, search.levels, search.family, search.persona)
                 for t in dm_role_terms(search.keyword)]
        out.append(dm_query_vectors(terms, encode, hub_alpha))
    return out


def evaluate_dm_searches(
    postings: Sequence[Posting],
    searches: Sequence[DmSearch],
    jobs: np.ndarray,
    queries: np.ndarray,
    threshold: float,
) -> dict:
    """Mean over searches (generic and specific separately) of
    dm_search_metrics, with bootstrap intervals on P@5 / P@10. A search's
    query is one vector, or (T, D) term vectors scored by their best
    (gosha.matching.dm_scores_over_terms)."""
    from gosha.matching import dm_scores_over_terms

    rows = []
    models = [as_job(p) for p in postings]
    for search, query in zip(searches, queries):
        scores = dm_scores_over_terms(jobs, query) if np.ndim(query) == 2 else jobs @ query
        m = dm_search_metrics(
            scores, dm_pool(postings, search, models), dm_relevant(postings, search),
            threshold, models,
        )
        rows.append({"keyword": search.keyword, "levels": list(search.levels),
                     "persona": search.persona, "generic": search.family is None, **m,
                     "low_n": is_low_n(m["pool"], m["relevant_in_pool"])})
    out: dict = {"threshold": threshold, "locations_applied": locations_applied(postings),
                 "searches": rows}
    for group, keep in (("generic", True), ("specific", False)):
        part = [r for r in rows if r["generic"] is keep]
        if not part:
            continue
        summary: dict = {"searches": len(part), "starved": sum(r["starved"] for r in part),
                         "low_n": sum(r["low_n"] for r in part),
                         "no_relevant": sum(r["relevant_in_pool"] == 0 for r in part)}
        for key in ("pool", "relevant_in_pool", "sent", "p@5", "p@10", "recall"):
            values = [r[key] for r in part if not math.isnan(r[key])]
            summary[key] = float(np.mean(values)) if values else math.nan
        for key in ("p@5", "p@10"):
            values = [r[key] for r in part if not math.isnan(r[key])]
            summary[f"{key}_ci95"] = bootstrap_ci(values) if values else (math.nan, math.nan)
        out[group] = summary
    return out


def scoped_feed_pool(postings: Sequence[Posting], levels: Sequence[str]) -> list[int]:
    """Indices of the postings a feed scoped to one active search (Cluj,
    these levels) keeps: gosha.recommend.in_search_scope, as the feed
    applies it. No title gate: the feed has none."""
    from gosha.recommend import SearchScope, in_search_scope

    scope = SearchScope(tuple(_search_locations(postings)), False, tuple(sorted(levels)))
    return [i for i, p in enumerate(postings) if in_search_scope(as_job(p), [scope])]


def evaluate_feed_scoped(
    vectors: Vectors, postings: Sequence[Posting], personas: Sequence[Persona],
    ranking: str = "production",
) -> dict:
    """The web feed for a persona with one active search, at each level
    set users choose: the scope filter first, then the ranking over what
    is left (z-scores over those candidates, as production takes them).
    Gold grades unchanged. A persona with no grade-2 posting left has no
    ranking to measure: counted in "no_relevant" and left out of the
    means; low-n personas are flagged and kept in."""
    out: dict = {"locations_applied": locations_applied(postings)}
    for levels in DM_LEVEL_SETS:
        idx = scoped_feed_pool(postings, levels)
        per_persona = {}
        for u, persona in enumerate(personas):
            gains = vectors.gains[u, idx]
            relevant = int((gains == 2).sum())
            row: dict = {"remaining": len(idx), "relevant": relevant,
                         "low_n": is_low_n(len(idx), relevant)}
            if relevant:
                row.update(feed_metrics(
                    feed_scores(vectors, u, idx=idx, ranking=ranking), gains,
                    [postings[i] for i in idx],
                ))
            per_persona[persona.id] = row
        measured = [r for r in per_persona.values() if r["relevant"]]
        summary: dict = {
            "remaining": len(idx),
            "personas": len(personas),
            "measured": len(measured),
            "no_relevant": len(personas) - len(measured),
            "low_n": sum(r["low_n"] for r in per_persona.values()),
        }
        if measured:
            keys = ("ndcg@10", "r_precision", "mrr", "p@10")
            summary["mean"] = {k: float(np.mean([r[k] for r in measured])) for k in keys}
            summary["ndcg@10_ci95"] = bootstrap_ci([r["ndcg@10"] for r in measured])
        out["+".join(levels)] = {**summary, "per_persona": per_persona}
    return out


def run(
    postings: Sequence[Posting],
    personas: Sequence[Persona],
    encode: Encode,
    *,
    query_location: str | None = "Cluj",
    with_feedback: bool = True,
    job_text_variant: str = "production",
    feed_ranking: str = "production",
) -> dict:
    """Every measurement for one encoder, as plain JSON-able data.

    "feed" is ranked by `feed_ranking` (by default what production ships);
    "feed_cosine" is the encoder's cosine alone, for comparison.
    """
    vectors = encode_all(postings, personas, encode, query_location, job_text_variant)
    dm_cv = dm_scores(vectors, with_cv=True)
    dm_search = dm_scores(vectors, with_cv=False)
    thresholds = sorted({*DM_THRESHOLDS, *production_thresholds()})
    result = {
        "postings": len(postings),
        "personas": len(personas),
        "feed_ranking": feed_ranking,
        "feed": evaluate_feed(vectors, postings, personas, feed_ranking),
        "feed_cosine": evaluate_feed(vectors, postings, personas, "cosine"),
        "feed_scoped": {
            name: evaluate_feed_scoped(vectors, postings, personas, name)
            for name in dict.fromkeys((feed_ranking, "cosine"))
        },
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
    from gosha.matching import DM_ALPHA, DM_THRESHOLD

    searches = dm_searches(personas)
    search_threshold, _ = production_thresholds()
    # "dm_score" is what production sends a search without a CV (a field
    # keyword scored through its role terms, gosha.matching.dm_role_terms);
    # "cosine" the plain cosine at SEMANTIC_THRESHOLD it replaced.
    result["dm_searches"] = {
        "dm_score": evaluate_dm_searches(
            postings, searches, vectors.jobs, dm_term_query_vectors(searches, encode, DM_ALPHA),
            DM_THRESHOLD,
        ),
        "cosine": evaluate_dm_searches(
            postings, searches, vectors.jobs, dm_query_vectors(searches, encode), search_threshold,
        ),
    }
    if with_feedback:
        result["feedback"] = simulate_feedback(vectors, ranking=feed_ranking)
    return result
