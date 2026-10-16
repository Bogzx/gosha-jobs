"""Personalized job feed: rank jobs by similarity to the user's CV + feedback.

The user vector is the CV embedding, nudged toward jobs they marked
"interested" and away from "not_relevant" ones. Scoring is numpy cosine
over the candidate set (active jobs from the last FEED_WINDOW_DAYS).
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import numpy as np
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import or_, select
from sqlalchemy.orm import defer
from sqlalchemy.orm.attributes import set_committed_value

from gosha import filters, lexical, seniority
from gosha.database import get_session
from gosha.embeddings import bytes_to_vec, current_model_clause, is_current
from gosha.matching import build_job_text
from gosha.models import Application, Job, Subscription, User, UserJob

log = logging.getLogger(__name__)

FEED_WINDOW_DAYS = 30
# Rocchio weights for thumbs-up / thumbs-down (see apply_feedback). Kept at
# 0.3 / 0.3 on the offline eval (docs/EVAL.md): one round of feedback lifts
# nDCG@10 on the not-yet-rated postings by +0.10 (95% CI +0.002..+0.21);
# likes alone (0.3 / 0) gain less, and 0.5 / 0.5 is no better within noise.
LIKE_WEIGHT = 0.3
DISLIKE_WEIGHT = 0.3
# Share of the embedding cosine in the feed score; the rest is TF-IDF
# keyword similarity to the CV (gosha/lexical.py). Both are z-scored over
# the candidate set first, so the weight means what it says. On the
# offline eval (docs/EVAL.md) neither signal alone beats the mix.
EMBEDDING_WEIGHT = 0.5


class FeedItem(NamedTuple):
    """One ranked posting.

    `score` is the raw cosine — useful for debugging and for the delivery
    threshold. `percentile` is what the UI shows: raw cosine against a
    768-dim sentence embedding lands in a narrow band (roughly 0.15–0.45
    in production), so rendering it as a percentage against 75%/50%
    thresholds painted essentially every job grey at "23%". The ordering
    was always right; only the number was meaningless.
    """

    job: Job
    score: float | None
    percentile: int | None
    reasons: list[str]
    # No default: a NamedTuple default would be one list shared by every
    # instance. Both construction sites pass it explicitly.
    signals: list[MatchSignal]


def percentile_ranks(scores: list[float]) -> list[int]:
    """Map raw scores onto 0-100 by position within this candidate set.

    Uses the midpoint of the tied range, so equal scores get equal
    percentiles and the single best job in a set of n does not always read
    as a flat 100%.
    """
    n = len(scores)
    if n == 0:
        return []
    if n == 1:
        # A percentile of one thing is not information. Call it the middle.
        return [50]

    order = sorted(range(n), key=lambda i: scores[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        # Everything strictly below, plus half of the tied block.
        midpoint = i + (j - i) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = midpoint
        i = j + 1

    return [int(round(rank / (n - 1) * 100)) for rank in ranks]

# Dots and pluses are inside the class on purpose: "node.js", ".net",
# "c++" and "c#" are all skill names.
# Unicode letters, not [a-zA-Z]: an ASCII class cut Romanian words at the
# first diacritic, so "experiență" came out as "experien" in the reasons.
_WORD_RE = re.compile(r"[^\W\d_](?:[^\W\d_]|[+#.]){2,}")


def _tokens(text: str) -> list[str]:
    """Lowercased terms, with sentence punctuation stripped.

    Without the rstrip, "Kubernetes." at the end of a sentence and
    "Kubernetes" mid-sentence are two different tokens, so a term the
    posting actually repeats looks like two separate one-off mentions.
    Trailing dots only — the "+" in "c++" and the "#" in "c#" are part of
    the name, and "node.js" keeps its internal dot.
    """
    return [t.rstrip(".").lower() for t in _WORD_RE.findall(text or "") if t.rstrip(".")]

# Words that overlap in almost every CV/job pair — useless as "why this
# matches" explanations. Small curated EN+RO set, not a full NLP stopword list.
_STOPWORDS = frozenset({
    "and", "the", "with", "for", "you", "are", "our", "your", "will",
    "have", "has", "this", "that", "from", "not", "all", "can", "who",
    "what", "when", "where", "how", "than", "then", "them", "they",
    "experience", "experienced", "skills", "skill", "work", "working",
    "team", "teams", "job", "jobs", "role", "company", "position",
    "looking", "join", "about", "into", "able", "best", "well", "good",
    "new", "use", "using", "used", "need", "needs", "plus", "daily",
    "etc", "more", "most", "other", "also", "must", "should", "would",
    "si", "sau", "este", "sunt", "pentru", "care", "din", "intr", "una",
    "echipa", "companie", "rol", "munca", "experienta", "abilitati",
    # The same words as written, with diacritics, plus frequent posting verbs.
    "și", "într", "echipă", "echipei", "muncă", "experiență", "abilități",
    "cunoștințe", "cerințe", "vei", "oferim", "căutăm", "cautam", "avem",
    "unei", "unui", "unor", "acest", "această", "aceasta", "prin", "dar",
    "mai", "fie", "lor", "ale", "cel", "cea", "cele", "după", "până", "despre",
})


def match_reasons(cv_text: str, job_text: str, top_k: int = 3) -> list[str]:
    """Top informative terms shared by the CV and the job text.

    Ranked by term frequency in the job text (what the employer emphasizes),
    ties broken alphabetically for determinism.
    """
    cv_tokens = set(_tokens(cv_text)) - _STOPWORDS
    if not cv_tokens:
        return []

    job_counts = Counter(_tokens(job_text))
    shared = [
        (count, token)
        for token, count in job_counts.items()
        if token in cv_tokens
    ]
    shared.sort(key=lambda pair: (-pair[0], pair[1]))
    return [token for _, token in shared[:top_k]]


def match_gaps(cv_text: str, job_text: str, top_k: int = 2) -> list[str]:
    """Terms the employer stresses that the CV does not mention.

    The honest half of "why this matched". A ranking that only ever tells
    you what you already have is flattering and useless; knowing the one
    word standing between a CV and a posting is the actionable part.
    """
    if not cv_text:
        return []
    cv_tokens = set(_tokens(cv_text))

    job_counts = Counter(_tokens(job_text))
    missing = [
        (count, token)
        for token, count in job_counts.items()
        # Repeated at least twice: a term the posting leans on, not an
        # incidental word. Stopwords are noise in both directions.
        if token not in cv_tokens and token not in _STOPWORDS and count >= 2
    ]
    missing.sort(key=lambda pair: (-pair[0], pair[1]))
    return [token for _, token in missing[:top_k]]


class MatchSignal(NamedTuple):
    """One human-readable reason a job is where it is in the ranking.

    `kind` lets the UI style them differently; `text` is already phrased
    for display. Kept server-side because every input (the CV text, the
    liked-job centroid, the size of the candidate set) lives here — the
    client would otherwise need the whole corpus to say anything true.
    """

    kind: str  # "skill" | "gap" | "liked" | "rank"
    text: str


# Above this cosine to the centroid of thumbs-upped jobs, the feedback
# signal is doing real work and is worth telling the user about.
LIKED_SIMILARITY_FLOOR = 0.5


def explain_match(
    *,
    cv_text: str,
    job_text: str,
    percentile: int | None,
    total_candidates: int,
    liked_similarity: float | None = None,
) -> tuple[list[str], list[MatchSignal]]:
    """(shared terms, display signals) explaining one ranked job.

    The shared terms are returned separately because the compact job card
    only has room for that one line.
    """
    shared = match_reasons(cv_text, job_text)
    signals: list[MatchSignal] = []

    if percentile is not None and total_candidates > 1:
        if percentile >= 75:
            signals.append(MatchSignal(
                "rank",
                f"Top {max(1, 100 - percentile)}% of "
                f"{total_candidates} jobs ranked for you",
            ))
        else:
            signals.append(MatchSignal(
                "rank",
                f"Ranked #{max(1, round((100 - percentile) / 100 * total_candidates))} "
                f"of {total_candidates}",
            ))

    if shared:
        signals.append(MatchSignal(
            "skill", "Your CV mentions " + ", ".join(shared),
        ))

    if liked_similarity is not None and liked_similarity >= LIKED_SIMILARITY_FLOOR:
        signals.append(MatchSignal(
            "liked", "Close to jobs you marked interested",
        ))

    gaps = match_gaps(cv_text, job_text)
    if gaps:
        signals.append(MatchSignal(
            "gap", "Not in your CV: " + ", ".join(gaps),
        ))

    return shared, signals


class UserSignal(NamedTuple):
    """Everything the ranking knows about one user.

    `liked_mean` is kept alongside the blended vector so "why this
    matched" can say whether the feedback nudge is what put a job where it
    is, rather than guessing.
    """

    vector: np.ndarray | None
    liked_mean: np.ndarray | None


async def build_user_vector(user_id: int) -> np.ndarray | None:
    """CV embedding refined by feedback; None when there is no signal at all."""
    return (await build_user_signal(user_id)).vector


async def build_user_signal(user_id: int) -> UserSignal:
    """CV embedding refined by feedback, plus the raw feedback centroid."""
    async with get_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return UserSignal(None, None)

        # A CV vector from another model is in another space: ignore it
        # until it is re-embedded (scripts/reembed.py) rather than rank by it.
        base = (
            bytes_to_vec(user.cv_embedding)
            if user.cv_embedding and is_current(user.cv_embedding_model)
            else None
        )

        result = await session.execute(
            select(UserJob.feedback, Job.embedding)
            .join(Job, UserJob.job_id == Job.id)
            .where(
                UserJob.user_id == user_id,
                UserJob.feedback.isnot(None),
                Job.embedding.isnot(None),
                current_model_clause(Job.embedding_model),
            )
        )
        liked: list[np.ndarray] = []
        disliked: list[np.ndarray] = []
        for feedback, raw in result.all():
            if feedback == "interested":
                liked.append(bytes_to_vec(raw))
            elif feedback == "not_relevant":
                disliked.append(bytes_to_vec(raw))

    if base is None and not liked and not disliked:
        return UserSignal(None, None)

    liked_mean: np.ndarray | None = None
    if liked:
        raw_mean = np.mean(liked, axis=0)
        liked_norm = np.linalg.norm(raw_mean)
        if liked_norm > 0:
            liked_mean = (raw_mean / liked_norm).astype(np.float32)

    return UserSignal(apply_feedback(base, liked, disliked), liked_mean)


def apply_feedback(
    base: np.ndarray | None,
    liked: list[np.ndarray],
    disliked: list[np.ndarray],
    like_weight: float = LIKE_WEIGHT,
    dislike_weight: float = DISLIKE_WEIGHT,
) -> np.ndarray | None:
    """Rocchio relevance feedback, as a unit vector.

    base + like_weight * mean(liked) - dislike_weight * mean(disliked),
    where `base` is the CV vector (absent: start from zero, so likes alone
    still rank). None when there is no positive signal or it cancels out.
    Pure, so the offline eval (gosha/evaluation.py) runs exactly this.
    """
    if base is None and not liked:
        # Dislikes alone say what the user does not want, not what they do.
        # Ranking by -mean(disliked) put "least like what you turned down"
        # first: accounting and sales on top for a Python student who
        # disliked one Python posting. The caller falls back to newest-first.
        return None
    dim = len(base) if base is not None else len(liked[0])
    vector = (
        np.asarray(base, dtype=np.float64) if base is not None
        else np.zeros(dim, dtype=np.float64)
    )
    if liked:
        vector = vector + like_weight * np.mean(liked, axis=0)
    if disliked:
        vector = vector - dislike_weight * np.mean(disliked, axis=0)

    norm = np.linalg.norm(vector)
    if norm == 0:
        return None
    return (vector / norm).astype(np.float32)


# A z-score past this is "as close as it gets". TF-IDF scores are sparse
# (most postings share no term with the CV), so the one posting that does
# can sit sqrt(N) ~ 40 deviations out and outvote every other signal.
Z_CLIP = 3.0


def _zscore(values: np.ndarray) -> np.ndarray:
    std = float(values.std())
    if not std:
        return np.zeros_like(values, dtype=np.float64)
    return np.clip((values - values.mean()) / std, -Z_CLIP, Z_CLIP)


def hybrid_scores(
    cosines: np.ndarray,
    keyword: np.ndarray | None,
    penalties: np.ndarray | None,
    embedding_weight: float = EMBEDDING_WEIGHT,
) -> np.ndarray:
    """The feed's ranking score for each candidate.

    embedding_weight * z(cosine) + (1 - embedding_weight) * z(keyword)
    - seniority penalty, every term in standard deviations of this user's
    own candidate set (clipped to ±Z_CLIP): a raw cosine and a TF-IDF cosine live on different
    scales, and a fixed penalty means the same thing for every user.
    Without keyword scores (no CV text: a user ranked on 👍 alone) the
    cosine carries the whole weight. Pure, so the offline eval
    (gosha/evaluation.py) runs exactly this.
    """
    score = _zscore(np.asarray(cosines, dtype=np.float64))
    if keyword is not None and len(keyword) and float(np.ptp(keyword)) > 0:
        score = (
            embedding_weight * score
            + (1 - embedding_weight) * _zscore(np.asarray(keyword, dtype=np.float64))
        )
    if penalties is not None:
        score = score - penalties
    return score


_DEFERRED = object()


def _loaded_description(job: Job) -> str | None | object:
    """The description if it is in memory (possibly None: there is none),
    or _DEFERRED if load_candidates() left it in the database."""
    if "description" in sa_inspect(job).unloaded:
        return _DEFERRED
    return job.description


def _cache_key(job: Job) -> tuple[int, int] | None:
    """What the per-posting caches (seniority level here, term counts in
    gosha/lexical.py) are keyed on: the id alone would serve a posting's
    first, title-only reading forever after its description arrives. The
    pipeline drops and recomputes the embedding whenever the title,
    company or description changes, so its bytes identify the text."""
    if job.id is None:
        return None
    return (job.id, hash(job.embedding))


# Reading a posting's seniority needs its description, which the feed
# otherwise leaves in the database. Least recently used out; the lock
# because _order() runs in worker threads (asyncio.to_thread).
_level_cache: OrderedDict[tuple, str] = OrderedDict()
_level_lock = threading.Lock()


def _level_key(job: Job) -> tuple | None:
    key = _cache_key(job)
    # The board's label is read too, and is updated without a re-embed.
    return None if key is None else (*key, job.experience_level)


def posting_level(job: Job) -> str:
    key = _level_key(job)
    if key is not None:
        with _level_lock:
            level = _level_cache.get(key)
            if level is not None:
                _level_cache.move_to_end(key)
                return level
    description = _loaded_description(job)
    deferred = description is _DEFERRED
    level = seniority.posting_level(
        job.title, None if deferred else description, job.experience_level,
    )
    # A reading without the description is a guess for this request only;
    # a posting that really has none is read in full and kept.
    if key is not None and not deferred:
        with _level_lock:
            _level_cache[key] = level
            while len(_level_cache) > lexical.MAX_CACHED:
                _level_cache.popitem(last=False)
    return level


class SearchScope(NamedTuple):
    """Where and at what level one of the user's active searches looks."""

    locations: tuple[str, ...]
    remote_ok: bool
    levels: tuple[str, ...]


async def load_search_scopes(user_id: int) -> list[SearchScope]:
    """The scopes of the user's active searches. Paused searches do not
    narrow the feed (their exclusions still apply: get_user_exclusions)."""
    async with get_session() as session:
        result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user_id, Subscription.is_active.is_(True),
            )
        )
        subs = result.scalars().all()
    return [
        SearchScope(tuple(s.locations), bool(s.remote_ok), tuple(sorted(s.experience_levels)))
        for s in subs
    ]


def _restricts_level(levels: tuple[str, ...]) -> bool:
    """As matches_experience_level reads them: none or "any" is no filter."""
    wanted = {filters.canonical_level(level) or level.lower() for level in levels}
    return bool(wanted) and "any" not in wanted


# matches_experience_level verdicts per posting and level set: unmarked
# titles need the description (the years it asks for), which the feed
# leaves in the database. Same keys, lock and bound as _level_cache.
_scope_level_cache: OrderedDict[tuple, bool] = OrderedDict()


def _scope_level_key(job: Job, levels: tuple[str, ...]) -> tuple | None:
    key = _level_key(job)
    return None if key is None else (*key, levels)


def _level_matches(job: Job, levels: tuple[str, ...]) -> bool:
    if not _restricts_level(levels):
        return True
    key = _scope_level_key(job, levels)
    if key is not None:
        with _level_lock:
            hit = _scope_level_cache.get(key)
            if hit is not None:
                _scope_level_cache.move_to_end(key)
                return hit
    description = _loaded_description(job)
    deferred = description is _DEFERRED
    ok = filters.matches_experience_level(
        job.title, list(levels), job.experience_level, None if deferred else description,
    )
    if key is not None and not deferred:
        with _level_lock:
            _scope_level_cache[key] = ok
            while len(_scope_level_cache) > lexical.MAX_CACHED:
                _scope_level_cache.popitem(last=False)
    return ok


def _needs_text_for_scopes(job: Job, scopes: list[SearchScope]) -> bool:
    """Whether the scope filter has to read this job's description."""
    for scope in scopes:
        if not _restricts_level(scope.levels):
            continue
        key = _scope_level_key(job, scope.levels)
        if key is None:
            return True
        with _level_lock:
            if key not in _scope_level_cache:
                return True
    return False


def in_search_scope(job: Job, scopes: list[SearchScope], location_memo: dict | None = None) -> bool:
    """Whether some active search would take this posting on location and
    level, as the Discord match stage does (pipeline.passes_filters): per
    search, then any of them. "Intern in Bucharest" plus "senior in Cluj"
    does not admit a Cluj internship. No active search: no restriction.
    """
    if not scopes:
        return True
    memo = location_memo if location_memo is not None else {}
    for scope in scopes:
        where = (job.location, scope.locations, scope.remote_ok)
        if where not in memo:
            memo[where] = filters.search_location_ok(
                job.location, list(scope.locations), scope.remote_ok,
            )
        if memo[where] and _level_matches(job, scope.levels):
            return True
    return False


def clear_caches() -> None:
    """Forget every cached seniority level, level verdict, term count and
    keyword index."""
    with _level_lock:
        _level_cache.clear()
        _scope_level_cache.clear()
    lexical.clear_cache()


# What ranking for a CV with text reads from a posting's description
# (without CV text: no keyword score, no seniority penalty, nothing). The
# keyword terms of every posting in the window, which is the IDF corpus;
# the seniority level only of the user's own candidates.
def _needs_keyword_text(job: Job) -> bool:
    key = _cache_key(job)
    return key is None or not lexical.is_cached(key)


def _needs_level_text(job: Job) -> bool:
    key = _level_key(job)
    if key is None:
        return True
    with _level_lock:
        return key not in _level_cache


def _deferred(job: Job) -> bool:
    return _loaded_description(job) is _DEFERRED


async def get_feed(
    user_id: int, page: int = 1, per_page: int = 50,
) -> tuple[list[FeedItem], int]:
    """Ranked FeedItems for the user's feed, plus the total count.

    Jobs the user already gave feedback on or applied to are excluded, and
    so is what none of the user's active searches would take on location
    and level (in_search_scope); with no active search, nothing is.
    Without any personalization signal (no current CV vector and no 👍;
    see apply_feedback), falls back to newest-first with None scores.
    """
    user_vector, liked_mean = await build_user_signal(user_id)

    async with get_session() as session:
        seen_result = await session.execute(
            select(UserJob.job_id).where(
                UserJob.user_id == user_id, UserJob.feedback.isnot(None)
            )
        )
        excluded_ids = {row[0] for row in seen_result.all()}
        applied_result = await session.execute(
            select(Application.job_id).where(Application.user_id == user_id)
        )
        excluded_ids.update(row[0] for row in applied_result.all())

    # Standing exclusions: blacklisted companies / excluded words from any
    # of the user's searches never appear in the feed.
    from gosha.services.jobs import get_user_exclusions, passes_user_exclusions

    blacklist, excluded = await get_user_exclusions(user_id)

    # Descriptions are the bulk of a row and ranking does not read them:
    # only the page returned needs one (filled below), unless excluded
    # words have to be matched against every candidate's text.
    with_description = bool(excluded)
    # The whole window, rated postings included: it is what the keyword
    # IDF is fitted on (_order), the same for every user and the demo.
    window = await load_candidates(
        embedded_only=user_vector is not None, with_description=with_description,
    )
    candidates = [j for j in window if j.id not in excluded_ids]

    cv_text = ""
    if user_vector is not None:
        async with get_session() as session:
            user = await session.get(User, user_id)
            has_cv = user is not None and user.cv_embedding
        if has_cv:
            # CV text lives on disk (gosha/cover_letter.py storage);
            # reasons need the text, loaded lazily to avoid IO when
            # there is no CV.
            from gosha.cover_letter import load_cv
            from gosha.cv_crypto import CvKeyError
            try:
                cv_text = load_cv(user_id) or ""
            except CvKeyError as exc:
                # Rank without match reasons rather than fail the feed.
                log.error("Feed for user %d without CV reasons: %s", user_id, exc)

    if blacklist or excluded:
        candidates = [
            j for j in candidates if passes_user_exclusions(j, blacklist, excluded)
        ]

    # Descriptions are read only where nothing is cached yet: after the
    # first request of a scrape cycle, a handful, not ~2,000.
    scopes = await load_search_scopes(user_id)
    ranks_on_text = user_vector is not None and bool(cv_text.strip())
    if not with_description:
        in_candidates = {id(j) for j in candidates}
        await load_descriptions([
            j for j in window
            if (ranks_on_text and _needs_keyword_text(j))
            or (id(j) in in_candidates and _needs_text_for_scopes(j, scopes))
        ])
    if scopes:
        # The candidates only: the window stays the keyword-IDF corpus.
        memo: dict = {}
        candidates = [j for j in candidates if in_search_scope(j, scopes, memo)]
    if ranks_on_text and not with_description:
        await load_descriptions([
            j for j in candidates if _deferred(j) and _needs_level_text(j)
        ])

    start = (page - 1) * per_page
    if user_vector is None:
        candidates.sort(
            key=lambda j: j.first_seen_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        page_jobs = candidates[start : start + per_page]
        if not with_description:
            await load_descriptions(page_jobs)
        return [FeedItem(job, None, None, [], []) for job in page_jobs], len(candidates)

    # Building the keyword index is a CPU pass over every new posting:
    # off the event loop.
    ranked = await asyncio.to_thread(_order, candidates, user_vector, cv_text, window)
    page_rows = ranked[start : start + per_page]
    if not with_description:
        await load_descriptions([job for job, _, _ in page_rows])
    return (
        _explain(page_rows, len(ranked), cv_text=cv_text, liked_mean=liked_mean),
        len(ranked),
    )


async def load_candidates(
    excluded_ids: set[int] | frozenset[int] = frozenset(),
    embedded_only: bool = True,
    with_description: bool = True,
) -> list[Job]:
    """Active, canonical jobs first seen within FEED_WINDOW_DAYS.

    `embedded_only` keeps only jobs with a vector from the configured
    model — what a ranking needs. `with_description=False` leaves the
    description in the database; reading it then raises until
    load_descriptions() has filled it in.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=FEED_WINDOW_DAYS)
    async with get_session() as session:
        stmt = select(Job).where(
            Job.is_active.is_(True),
            Job.first_seen_at >= cutoff,
            # Hide non-canonical cross-board duplicates
            or_(Job.dedup_group_id.is_(None), Job.dedup_group_id == Job.id),
        )
        if embedded_only:
            stmt = stmt.where(
                Job.embedding.isnot(None), current_model_clause(Job.embedding_model),
            )
        if excluded_ids:
            stmt = stmt.where(Job.id.notin_(excluded_ids))
        if not with_description:
            stmt = stmt.options(defer(Job.description))
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def load_descriptions(jobs: list[Job]) -> None:
    """Fill in the description of jobs loaded without one (load_candidates)."""
    if not jobs:
        return
    async with get_session() as session:
        result = await session.execute(
            select(Job.id, Job.description).where(Job.id.in_([job.id for job in jobs]))
        )
        descriptions = dict(result.all())
    for job in jobs:
        # As if loaded with the row: no pending change for a later session.
        set_committed_value(job, "description", descriptions.get(job.id))


def rank_candidates(
    candidates: list[Job],
    vector: np.ndarray,
    *,
    cv_text: str = "",
    liked_mean: np.ndarray | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[FeedItem], int]:
    """One page of `candidates` ranked by cosine to `vector`, explained.

    Shared by the signed-in feed and the public demo (gosha/api/demo.py),
    so the demo shows exactly the ranking users get.
    """
    ranked = _order(candidates, vector, cv_text)
    start = (page - 1) * per_page
    return (
        _explain(
            ranked[start : start + per_page], len(ranked),
            cv_text=cv_text, liked_mean=liked_mean,
        ),
        len(ranked),
    )


def _order(
    candidates: list[Job],
    vector: np.ndarray,
    cv_text: str = "",
    corpus: list[Job] | None = None,
) -> list[tuple[Job, float, int]]:
    """(job, clipped cosine, percentile) for every candidate, best first.

    `corpus` (a superset of `candidates`, default the candidates) is what
    the keyword IDF is fitted on: the feed passes the whole window so one
    index serves every user (lexical.index_for).

    Ordered by hybrid_scores(): the embedding cosine, TF-IDF similarity to
    the CV text and the seniority gap between the CV and the posting. The
    cosine is still what is returned as the score (the Discord delivery
    threshold and the API's match_score speak cosine); the percentile is
    of the hybrid score, so it agrees with the order.
    """
    if not candidates:
        return []

    matrix = np.stack([bytes_to_vec(j.embedding) for j in candidates])
    cosines = matrix @ vector

    keyword = None
    if cv_text.strip():
        corpus = candidates if corpus is None else corpus
        corpus_keys = [_cache_key(j) for j in corpus]
        if all(k is not None for k in corpus_keys):
            index = lexical.index_for([_lexical_text(j) for j in corpus], corpus_keys)
            keyword = index.score_keys(cv_text, [_cache_key(j) for j in candidates])
        else:  # unsaved rows (tests): nothing stable to cache on
            keyword = lexical.scores(cv_text, [_lexical_text(j) or "" for j in candidates])

    reader = seniority.cv_level(cv_text)
    penalties = None
    if reader != seniority.CV_UNKNOWN:
        penalties = np.array([
            seniority.level_penalty(reader, posting_level(j)) for j in candidates
        ])
    final = hybrid_scores(cosines, keyword, penalties)
    order = np.argsort(-final, kind="stable")

    ranked_jobs = [candidates[int(i)] for i in order]
    ranked_scores = [float(max(0.0, min(1.0, cosines[int(i)]))) for i in order]
    # Percentiles are computed against the WHOLE candidate set, not the
    # page — otherwise page 2 would relabel its own worst jobs as top
    # matches.
    ranked_percentiles = percentile_ranks([float(final[int(i)]) for i in order])
    return list(zip(ranked_jobs, ranked_scores, ranked_percentiles))


def _lexical_text(job: Job) -> str | None:
    """What the keyword index reads for a job, as the embedding does.

    None when the description was not loaded: the index then uses the
    term counts it cached for this posting (see _needs_keyword_text), or, if they
    were evicted meanwhile, scores it 0 for this request and caches
    nothing (lexical.LexicalIndex.complete).
    """
    description = _loaded_description(job)
    if description is _DEFERRED:
        return None
    return build_job_text(job.title, job.company, description)


def _explain(
    page_rows: list[tuple[Job, float, int]],
    total: int,
    *,
    cv_text: str,
    liked_mean: np.ndarray | None,
) -> list[FeedItem]:
    """FeedItems for one page of _order()'s output.

    Explanations are the expensive part (regex tokenisation over every
    job description), so they are computed for the page being returned
    rather than for all ~2000 candidates that get thrown away.
    """
    items: list[FeedItem] = []
    for job, score, percentile in page_rows:
        liked_similarity = None
        if liked_mean is not None and job.embedding:
            liked_similarity = float(bytes_to_vec(job.embedding) @ liked_mean)

        reasons, signals = explain_match(
            cv_text=cv_text,
            job_text=f"{job.title} {job.description or ''}",
            percentile=percentile,
            total_candidates=total,
            liked_similarity=liked_similarity,
        )
        items.append(FeedItem(job, score, percentile, reasons, signals))
    return items
