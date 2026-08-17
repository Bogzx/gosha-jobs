"""Personalized job feed: rank jobs by similarity to the user's CV + feedback.

The user vector is the CV embedding, nudged toward jobs they marked
"interested" and away from "not_relevant" ones. Scoring is numpy cosine
over the candidate set (active jobs from the last FEED_WINDOW_DAYS).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import numpy as np
from sqlalchemy import or_, select

from gosha.database import get_session
from gosha.embeddings import bytes_to_vec
from gosha.models import Application, Job, User, UserJob

log = logging.getLogger(__name__)

FEED_WINDOW_DAYS = 30
FEEDBACK_WEIGHT = 0.3


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
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z+#.]{2,}")


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

        base = bytes_to_vec(user.cv_embedding) if user.cv_embedding else None

        result = await session.execute(
            select(UserJob.feedback, Job.embedding)
            .join(Job, UserJob.job_id == Job.id)
            .where(
                UserJob.user_id == user_id,
                UserJob.feedback.isnot(None),
                Job.embedding.isnot(None),
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

    vector = base.astype(np.float64) if base is not None else np.zeros(
        len(liked[0]) if liked else len(disliked[0]), dtype=np.float64
    )
    if liked:
        vector = vector + FEEDBACK_WEIGHT * np.mean(liked, axis=0)
    if disliked:
        vector = vector - FEEDBACK_WEIGHT * np.mean(disliked, axis=0)

    norm = np.linalg.norm(vector)
    if norm == 0:
        return UserSignal(None, liked_mean)
    return UserSignal((vector / norm).astype(np.float32), liked_mean)


async def get_feed(
    user_id: int, page: int = 1, per_page: int = 50,
) -> tuple[list[FeedItem], int]:
    """Ranked FeedItems for the user's feed, plus the total count.

    Jobs the user already gave feedback on or applied to are excluded.
    Without any personalization signal, falls back to newest-first with
    None scores.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=FEED_WINDOW_DAYS)
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

        stmt = select(Job).where(
            Job.is_active.is_(True),
            Job.first_seen_at >= cutoff,
            # Hide non-canonical cross-board duplicates
            or_(Job.dedup_group_id.is_(None), Job.dedup_group_id == Job.id),
        )
        if user_vector is not None:
            stmt = stmt.where(Job.embedding.isnot(None))
        if excluded_ids:
            stmt = stmt.where(Job.id.notin_(excluded_ids))

        result = await session.execute(stmt)
        candidates = list(result.scalars().all())

        cv_text = ""
        if user_vector is not None:
            user = await session.get(User, user_id)
            if user is not None and user.cv_embedding:
                # CV text lives on disk (gosha/cover_letter.py storage);
                # reasons need the text, loaded lazily to avoid IO when
                # there is no CV.
                from gosha.cover_letter import load_cv
                cv_text = load_cv(user_id) or ""

    # Standing exclusions: blacklisted companies / excluded words from any
    # of the user's searches never appear in the feed.
    from gosha.services.jobs import get_user_exclusions, passes_user_exclusions

    blacklist, excluded = await get_user_exclusions(user_id)
    if blacklist or excluded:
        candidates = [
            j for j in candidates if passes_user_exclusions(j, blacklist, excluded)
        ]

    if user_vector is None:
        candidates.sort(
            key=lambda j: j.first_seen_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        total = len(candidates)
        start = (page - 1) * per_page
        return (
            [
                FeedItem(job, None, None, [], [])
                for job in candidates[start : start + per_page]
            ],
            total,
        )

    if not candidates:
        return [], 0

    matrix = np.stack([bytes_to_vec(j.embedding) for j in candidates])
    scores = matrix @ user_vector
    order = np.argsort(-scores)

    ranked_jobs = [candidates[int(i)] for i in order]
    ranked_scores = [float(max(0.0, min(1.0, scores[int(i)]))) for i in order]
    # Percentiles are computed against the WHOLE candidate set, not the
    # page — otherwise page 2 would relabel its own worst jobs as top
    # matches.
    ranked_percentiles = percentile_ranks(ranked_scores)

    total = len(ranked_jobs)
    start = (page - 1) * per_page
    page_slice = slice(start, start + per_page)

    # Explanations are the expensive part (regex tokenisation over every
    # job description), so they are computed for the page being returned
    # rather than for all ~2000 candidates that get thrown away.
    items: list[FeedItem] = []
    for job, score, percentile in zip(
        ranked_jobs[page_slice],
        ranked_scores[page_slice],
        ranked_percentiles[page_slice],
    ):
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

    return items, total
