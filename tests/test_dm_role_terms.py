"""The DM score through a field keyword's roles (owner decision, 2026-10-17).

A "computer science internship" search that chose junior was not sent junior
CS jobs: the posting describes a role ("Software Engineer", "Data Scientist"),
the query a field, and 71 of the 77 missed DMs of 2026-10-17 were junior. The
match stage now scores a field keyword through each role it stands for
(gosha-rank's SemanticMatcher.dm_term_queries) and keeps the best term.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

import gosha.pipeline as pipeline
from gosha.embeddings import current_model, vec_to_bytes
from gosha.matching import SemanticMatcher
from gosha.models import Job, Subscription

RIVERBED = (Path(__file__).parent / "fixtures" / "riverbed_netim_8280.txt").read_text()


def _sub(keyword: str, levels: list[str]) -> Subscription:
    sub = Subscription(id=10, max_age_days=7, remote_ok=False, is_active=True)
    sub.keywords = [keyword]
    sub.locations = ["cluj"]
    sub.experience_levels = levels
    return sub


def _job(title: str, description: str, vec: np.ndarray | None = None) -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        id=None, url="https://j.test/8280", title=title, company="Riverbed Technology",
        location="Cluj-Napoca, Cluj, Romania", description=description, source="linkedin",
        experience_level="entry", is_active=True, posted_at=now, first_seen_at=now,
        embedding=None if vec is None else vec_to_bytes(np.asarray(vec, dtype=np.float32)),
        embedding_model=current_model(),
    )


class _TermMatcher:
    """Two DM term rows: the field query, and a role that fits the posting."""

    available = True
    threshold = cv_threshold = 2.0
    dm_threshold = 0.5
    is_match = SemanticMatcher.is_match
    is_dm_match = SemanticMatcher.is_dm_match

    def __init__(self, rows):
        self.rows = np.asarray(rows, dtype=np.float32)

    def encode_subscription(self, keywords, locations, experience_levels):
        return self.rows[0]

    def dm_term_queries(self, keywords, locations, experience_levels):
        return self.rows


async def test_the_best_term_is_the_stored_dm_score():
    job = _job("Software Engineer, NetIM", RIVERBED, [0.0, 1.0, 0.0])
    sub = _sub("computer science internship", ["junior"])
    field_only = await pipeline.match_jobs_for_subscription(
        sub, [job], _TermMatcher([[1.0, 0.2, 0.0]]),
    )
    assert field_only == []  # 0.2 < 0.5
    matches = await pipeline.match_jobs_for_subscription(
        sub, [job], _TermMatcher([[1.0, 0.2, 0.0], [0.0, 0.8, 0.0]]),
    )
    assert [round(score, 3) for _job, score in matches] == [0.8]


def _has_sentence_transformers() -> bool:
    try:
        import sentence_transformers  # noqa: F401 - availability probe
        return True
    except ImportError:
        return False


class _SingleQuery(SemanticMatcher):
    """Today's scoring before the change: no term queries."""

    dm_term_queries = None


@pytest.mark.skipif(not _has_sentence_transformers(), reason="sentence-transformers not installed")
class TestWithTheModel:
    async def test_a_junior_role_posting_crosses_for_a_field_search(self):
        # Riverbed "Software Engineer, NetIM" (8280): '1 - 5 years of Java',
        # junior under the range rule; DM score 0.286 against the field.
        sub = _sub("computer science internship", ["junior"])
        job = _job("Software Engineer, NetIM", RIVERBED)
        assert await pipeline.match_jobs_for_subscription(sub, [job], _SingleQuery()) == []
        matches = await pipeline.match_jobs_for_subscription(sub, [job], SemanticMatcher())
        assert [m.title for m, _score in matches] == ["Software Engineer, NetIM"]

    async def test_a_role_keyword_keeps_its_single_query_score(self):
        sub = _sub("java developer", ["junior"])
        job = _job("Java Developer", RIVERBED)
        before, after = _SingleQuery(), SemanticMatcher()
        before.dm_threshold = after.dm_threshold = -1.0  # compare the scores themselves
        today = await pipeline.match_jobs_for_subscription(sub, [job], before)
        now = await pipeline.match_jobs_for_subscription(sub, [job], after)
        assert today and [round(s, 5) for _j, s in now] == [round(s, 5) for _j, s in today]
