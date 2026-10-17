"""Tests for the CV-based recommendation engine (fake vectors, no model)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import count

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gosha import recommend
from gosha.embeddings import EMBEDDING_DIM, vec_to_bytes
from gosha.models import Application, Job, User, UserJob


def unit_vec(axis: int) -> np.ndarray:
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    v[axis] = 1.0
    return v


_untitled = count(1)


def make_job(url: str, axis: int | None, **kwargs) -> Job:
    job = Job(
        url=url,
        # Distinct by default: one title at one employer is one posting.
        title=kwargs.pop("title", f"Dev {next(_untitled)}"),
        company=kwargs.pop("company", "Acme"),
        source=kwargs.pop("source", "indeed"),
        **kwargs,
    )
    if axis is not None:
        job.embedding = vec_to_bytes(unit_vec(axis))
    return job


@pytest.mark.asyncio
async def test_feed_ranks_by_cosine(patched_db, session: AsyncSession):
    user = User(discord_user_id=1, cv_embedding=vec_to_bytes(unit_vec(0)))
    session.add(user)
    job_a = make_job("https://r.com/a", 0, title="Python Dev")
    job_b = make_job("https://r.com/b", 1, title="Accountant")
    session.add_all([job_a, job_b])
    await session.commit()

    items, total = await recommend.get_feed(user.id, page=1, per_page=10)

    assert total == 2
    assert [item.job.id for item in items] == [job_a.id, job_b.id]
    assert items[0].score > 0.9
    assert items[1].score < 0.1


@pytest.mark.asyncio
async def test_feed_feedback_refinement(patched_db, session: AsyncSession):
    """Without a CV, liked jobs steer the user vector."""
    user = User(discord_user_id=2)
    session.add(user)
    liked = make_job("https://r.com/liked", 1)
    session.add(liked)
    await session.flush()
    session.add(
        UserJob(user_id=user.id, job_id=liked.id, feedback="interested")
    )
    cand_match = make_job("https://r.com/m", 1, title="Like the liked one")
    cand_other = make_job("https://r.com/o", 2, title="Different")
    session.add_all([cand_match, cand_other])
    await session.commit()

    items, _ = await recommend.get_feed(user.id, page=1, per_page=10)

    ids = [item.job.id for item in items]
    # The liked job itself has feedback → excluded from the feed
    assert liked.id not in ids
    assert ids[0] == cand_match.id


@pytest.mark.asyncio
async def test_feed_excludes_not_relevant_and_applied(patched_db, session: AsyncSession):
    user = User(discord_user_id=3, cv_embedding=vec_to_bytes(unit_vec(0)))
    session.add(user)
    bad = make_job("https://r.com/bad", 0)
    applied = make_job("https://r.com/applied", 0)
    fresh = make_job("https://r.com/fresh", 0)
    session.add_all([bad, applied, fresh])
    await session.flush()
    session.add_all([
        UserJob(user_id=user.id, job_id=bad.id, feedback="not_relevant"),
        Application(user_id=user.id, job_id=applied.id, source="web"),
    ])
    await session.commit()

    items, total = await recommend.get_feed(user.id, page=1, per_page=10)
    ids = [item.job.id for item in items]
    assert ids == [fresh.id]
    assert total == 1


@pytest.mark.asyncio
async def test_feed_fallback_recency_without_signal(patched_db, session: AsyncSession):
    """No CV, no feedback -> newest-first with null scores."""
    user = User(discord_user_id=4)
    session.add(user)
    now = datetime.now(timezone.utc)
    old = make_job("https://r.com/old", None)
    old.first_seen_at = now - timedelta(days=5)
    new = make_job("https://r.com/new", None)
    new.first_seen_at = now
    session.add_all([old, new])
    await session.commit()

    items, total = await recommend.get_feed(user.id, page=1, per_page=10)

    assert total == 2
    assert [item.job.id for item in items] == [new.id, old.id]
    assert all(item.score is None for item in items)


@pytest.mark.asyncio
async def test_feed_dislikes_alone_fall_back_to_recency(patched_db, session: AsyncSession):
    """No CV and only a 👎: the vector was -disliked, so the feed ranked
    "least like what you turned down" first (accounting on top for a
    Python student). Dislikes give no direction; newest-first instead."""
    user = User(discord_user_id=12)
    session.add(user)
    now = datetime.now(timezone.utc)
    disliked = make_job("https://r.com/dis", 0, title="Python Dev")
    python_job = make_job("https://r.com/py", 0, title="Python Backend")
    python_job.first_seen_at = now
    accounting = make_job("https://r.com/acc", 1, title="Accountant")
    accounting.first_seen_at = now - timedelta(days=1)
    session.add_all([disliked, python_job, accounting])
    await session.flush()
    session.add(UserJob(user_id=user.id, job_id=disliked.id, feedback="not_relevant"))
    await session.commit()

    assert await recommend.build_user_vector(user.id) is None
    items, total = await recommend.get_feed(user.id, page=1, per_page=10)

    assert [item.job.id for item in items] == [python_job.id, accounting.id]
    assert total == 2  # the disliked job itself stays out
    assert all(item.score is None for item in items)


def test_apply_feedback_needs_a_cv_or_a_like():
    v = unit_vec(0)
    assert recommend.apply_feedback(None, [], [v]) is None
    # With a CV, dislikes still push away from what was disliked.
    out = recommend.apply_feedback(unit_vec(1), [], [v])
    assert out is not None and out[0] < 0 < out[1]


@pytest.mark.asyncio
async def test_feed_skips_stale_jobs(patched_db, session: AsyncSession):
    user = User(discord_user_id=5, cv_embedding=vec_to_bytes(unit_vec(0)))
    session.add(user)
    stale = make_job("https://r.com/stale", 0)
    stale.first_seen_at = datetime.now(timezone.utc) - timedelta(days=45)
    session.add(stale)
    await session.commit()

    items, total = await recommend.get_feed(user.id, page=1, per_page=10)
    assert items == [] and total == 0


@pytest.mark.asyncio
async def test_feed_pagination(patched_db, session: AsyncSession):
    user = User(discord_user_id=6, cv_embedding=vec_to_bytes(unit_vec(0)))
    session.add(user)
    for i in range(7):
        session.add(make_job(f"https://r.com/p{i}", 0))
    await session.commit()

    page1, total = await recommend.get_feed(user.id, page=1, per_page=3)
    page2, _ = await recommend.get_feed(user.id, page=2, per_page=3)
    page3, _ = await recommend.get_feed(user.id, page=3, per_page=3)

    assert total == 7
    assert len(page1) == 3 and len(page2) == 3 and len(page3) == 1
    all_ids = [item.job.id for item in page1 + page2 + page3]
    assert len(set(all_ids)) == 7


@pytest.mark.asyncio
async def test_feed_reports_percentiles_not_raw_cosine(
    patched_db, session: AsyncSession,
):
    """Raw cosine against a sentence embedding lands ~0.15-0.45, so a badge
    thresholded at 75%/50% painted almost every job grey at "23%". The
    percentile spreads the same ordering across the full range."""
    user = User(discord_user_id=10, cv_embedding=vec_to_bytes(unit_vec(0)))
    session.add(user)

    # Four jobs at descending, realistically-narrow similarities.
    for i, weight in enumerate((0.45, 0.35, 0.25, 0.15)):
        vec = np.zeros(EMBEDDING_DIM, dtype=np.float32)
        vec[0] = weight
        vec[1] = float(np.sqrt(1 - weight**2))
        job = make_job(f"https://r.com/pc{i}", None)
        job.embedding = vec_to_bytes(vec)
        session.add(job)
    await session.commit()

    items, _ = await recommend.get_feed(user.id, page=1, per_page=10)

    # Every raw score is in the "looks like a bad match" band...
    assert all(0.1 < item.score < 0.5 for item in items)
    # ...but the percentiles use the whole scale, best first.
    percentiles = [item.percentile for item in items]
    assert percentiles == [100, 67, 33, 0]


@pytest.mark.asyncio
async def test_percentiles_are_relative_to_all_candidates_not_the_page(
    patched_db, session: AsyncSession,
):
    """Page 2 must not relabel its own worst jobs as top matches."""
    user = User(discord_user_id=11, cv_embedding=vec_to_bytes(unit_vec(0)))
    session.add(user)
    for i in range(10):
        weight = 0.5 - i * 0.03
        vec = np.zeros(EMBEDDING_DIM, dtype=np.float32)
        vec[0] = weight
        vec[1] = float(np.sqrt(1 - weight**2))
        job = make_job(f"https://r.com/pg{i}", None)
        job.embedding = vec_to_bytes(vec)
        session.add(job)
    await session.commit()

    page1, _ = await recommend.get_feed(user.id, page=1, per_page=5)
    page2, _ = await recommend.get_feed(user.id, page=2, per_page=5)

    assert page1[0].percentile == 100
    # The best job on page 2 is still a mid-pack match overall.
    assert page2[0].percentile < 60
    assert page2[-1].percentile == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cv", ["none", "vector_only", "text"])
async def test_feed_reads_descriptions_only_for_the_page(
    patched_db, session: AsyncSession, monkeypatch, cv,
):
    """Ranking needs the vectors, not the text: every /feed request loaded
    the description of every posting from the last 30 days. With CV text,
    the first request reads each new posting once (keyword terms and
    seniority are cached); after that, and always without CV text (no
    keyword score, no seniority penalty), only the page is read."""
    user = User(
        discord_user_id=21 + len(cv),
        cv_embedding=vec_to_bytes(unit_vec(0)) if cv != "none" else None,
    )
    session.add(user)
    now = datetime.now(timezone.utc)
    for i in range(5):
        weight = 0.9 - i * 0.1
        vec = np.zeros(EMBEDDING_DIM, dtype=np.float32)
        vec[0] = weight
        vec[1] = float(np.sqrt(1 - weight**2))
        job = make_job(f"https://r.com/d{i}", None, description=f"Posting {i}: Python")
        job.embedding = vec_to_bytes(vec)
        job.first_seen_at = now - timedelta(hours=i)  # same order either way
        session.add(job)
    await session.commit()
    all_ids = {job.id for job in (await recommend.load_candidates())}

    import gosha.cover_letter

    monkeypatch.setattr(
        gosha.cover_letter, "load_cv", lambda _uid: "Python student" if cv == "text" else None,
    )
    loaded: list[list[Job]] = []
    real = recommend.load_candidates

    async def spy(*args, **kwargs):
        jobs = await real(*args, **kwargs)
        loaded.append(jobs)
        return jobs

    monkeypatch.setattr(recommend, "load_candidates", spy)

    async def request() -> tuple[list[str], set[int], set[int]]:
        items, total = await recommend.get_feed(user.id, page=1, per_page=2)
        assert total == 5
        read = {job.id for job in loaded[-1] if "description" in job.__dict__}
        return [item.job.description for item in items], {item.job.id for item in items}, read

    descriptions, page_ids, read = await request()
    assert descriptions == ["Posting 0: Python", "Posting 1: Python"]
    assert read == (all_ids if cv == "text" else page_ids)

    descriptions, page_ids, read = await request()
    assert descriptions == ["Posting 0: Python", "Posting 1: Python"]
    assert read == page_ids


def _vec(cosine: float) -> bytes:
    """A unit vector at this cosine to unit_vec(0)."""
    vec = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    vec[0] = cosine
    vec[1] = float(np.sqrt(1 - cosine**2))
    return vec_to_bytes(vec)


@pytest.mark.asyncio
async def test_feed_rereads_a_posting_whose_text_changed(
    patched_db, session: AsyncSession, monkeypatch,
):
    """A posting first seen title-only gets its description (and a new
    embedding) from a later scrape; the keyword terms cached for its
    title-only version must not stand in for it until a restart."""
    import gosha.cover_letter

    monkeypatch.setattr(gosha.cover_letter, "load_cv", lambda _uid: "Kotlin")
    user = User(discord_user_id=31, cv_embedding=vec_to_bytes(unit_vec(0)))
    mobile = make_job("https://r.com/m", None, title="Mobile Developer")
    mobile.embedding = _vec(0.6)
    accountant = make_job("https://r.com/a", None, title="Accountant", description="Excel, SAP")
    accountant.embedding = _vec(0.8)
    sales = make_job("https://r.com/s", None, title="Sales", description="B2B")
    sales.embedding = _vec(0.7)
    session.add_all([user, mobile, accountant, sales])
    await session.commit()

    items, _ = await recommend.get_feed(user.id)
    assert [item.job.id for item in items] == [accountant.id, sales.id, mobile.id]

    # What the pipeline does when the detail page arrives: new text, the
    # old vector dropped and recomputed.
    mobile.description = "Android apps in Kotlin"
    mobile.embedding = _vec(0.75)
    await session.commit()

    items, _ = await recommend.get_feed(user.id)
    # Only the keyword match can put it first: its cosine is second.
    assert [item.job.id for item in items] == [mobile.id, accountant.id, sales.id]


@pytest.mark.asyncio
async def test_posting_level_follows_the_text(patched_db, session: AsyncSession):
    job = make_job("https://r.com/lvl", 0, title="Backend Developer", description=None)
    session.add(job)
    await session.commit()
    assert recommend.posting_level(job) == "unknown"
    # A posting that really has no description is read in full: cached.
    assert recommend._level_key(job) in recommend._level_cache

    job.description = "Minimum 7 years of experience with Go"
    job.embedding = vec_to_bytes(unit_vec(1))
    await session.commit()
    assert recommend.posting_level(job) == "senior"


@pytest.mark.asyncio
async def test_a_deferred_description_is_never_cached_as_missing(
    patched_db, session: AsyncSession,
):
    """Ranking a posting whose description was left in the database and
    whose cached reading was evicted: it is scored without the text for
    that request, and nothing built from the gap is kept."""
    from gosha import lexical

    session.add(make_job("https://r.com/k", 0, title="Developer", description="Kotlin, 8 years of experience"))
    await session.commit()
    [job] = await recommend.load_candidates(with_description=False)
    key = recommend._cache_key(job)

    recommend._order([job], unit_vec(0), "Kotlin")
    assert not lexical.is_cached(key)
    assert not lexical._indexes
    assert recommend._level_key(job) not in recommend._level_cache

    await recommend.load_descriptions([job])
    recommend._order([job], unit_vec(0), "Kotlin")
    assert lexical.is_cached(key)
    assert recommend._level_cache[recommend._level_key(job)] == "senior"


@pytest.mark.asyncio
async def test_every_user_shares_one_keyword_index_over_the_window(
    patched_db, session: AsyncSession, monkeypatch,
):
    """The IDF is fitted on the whole window, not on each user's
    candidates minus what they rated: one index for every request, the
    same as the demo's, and a ranking that does not depend on which other
    indexes happen to be cached."""
    from gosha import lexical

    import gosha.cover_letter

    monkeypatch.setattr(gosha.cover_letter, "load_cv", lambda _uid: "Python Django")
    user = User(discord_user_id=41, cv_embedding=vec_to_bytes(unit_vec(0)))
    jobs = [
        make_job(f"https://r.com/w{i}", None, title=title, description=text)
        for i, (title, text) in enumerate([
            ("Python Developer", "Django, PostgreSQL"),
            ("Backend Engineer", "Python services"),
            ("Accountant", "Excel"),
            ("Data Analyst", "Python, SQL"),
        ])
    ]
    for i, job in enumerate(jobs):
        job.embedding = _vec(0.9 - i * 0.1)
    session.add_all([user, *jobs])
    await session.flush()
    session.add(UserJob(user_id=user.id, job_id=jobs[0].id, feedback="not_relevant"))
    await session.commit()

    items, total = await recommend.get_feed(user.id)
    assert total == 3 and jobs[0].id not in {item.job.id for item in items}
    assert len(lexical._indexes) == 1
    [index] = lexical._indexes.values()
    assert {k[0] for k in index.keys} == {job.id for job in jobs}

    window = await recommend.load_candidates()
    recommend.rank_candidates(window, unit_vec(0), cv_text="Python Django")
    assert list(lexical._indexes.values()) == [index]


def test_posting_level_is_safe_under_concurrent_clears():
    import threading

    jobs = [
        Job(id=i, url=f"u{i}", title="Senior Dev", company="A", source="x",
            description="text", embedding=vec_to_bytes(unit_vec(i % 3)))
        for i in range(50)
    ]
    errors: list[BaseException] = []
    stop = threading.Event()

    def read():
        try:
            while not stop.is_set():
                for job in jobs:
                    assert recommend.posting_level(job) == "senior"
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(4)]
    for t in threads:
        t.start()
    for _ in range(300):
        recommend.clear_caches()
    stop.set()
    for t in threads:
        t.join()
    assert not errors


def test_hybrid_scores_clip_a_sparse_keyword_outlier():
    """One posting sharing a rare term with the CV, out of 400 sharing
    none, is ~20 standard deviations out; unclipped, its keyword term alone
    would outvote a cosine at the very bottom of the set."""
    n = 400
    cosines = np.linspace(0.6, 0.2, n)
    keyword = np.zeros(n)
    keyword[-1] = 0.3  # the worst cosine
    final = recommend.hybrid_scores(cosines, keyword, None)
    z_cosine = recommend._zscore(cosines)
    assert final[-1] == pytest.approx(0.5 * z_cosine[-1] + 0.5 * recommend.Z_CLIP)
    assert final[-1] < final[0]


def test_no_cv_text_means_no_seniority_penalty():
    from gosha import seniority

    assert seniority.cv_level("") == seniority.CV_UNKNOWN
    assert seniority.cv_level("   ") == seniority.CV_UNKNOWN
    assert seniority.level_penalty(seniority.CV_UNKNOWN, seniority.SENIOR) == 0.0
    assert seniority.cv_level("Software developer") == seniority.JUNIOR


def test_percentile_ranks_handles_ties_and_edges():
    assert recommend.percentile_ranks([]) == []
    assert recommend.percentile_ranks([0.3]) == [50]
    assert recommend.percentile_ranks([0.1, 0.2, 0.3]) == [0, 50, 100]
    # Ties share the midpoint of their block rather than jumping.
    assert recommend.percentile_ranks([0.2, 0.2, 0.9]) == [25, 25, 100]


class TestWhyThisMatched:
    """The explanation behind the badge — see gosha/recommend.explain_match."""

    CV = "Python developer with Docker and Postgres experience"
    JOB = (
        "Backend engineer. Python and Docker daily. Docker Compose, Python "
        "services, and heavy Kubernetes usage. Kubernetes knowledge required."
    )

    def test_names_the_shared_terms(self):
        _reasons, signals = recommend.explain_match(
            cv_text=self.CV, job_text=self.JOB,
            percentile=90, total_candidates=200,
        )
        skill = next(s for s in signals if s.kind == "skill")
        assert "python" in skill.text and "docker" in skill.text

    def test_names_the_gap(self):
        """The actionable half: the term standing between CV and posting."""
        _reasons, signals = recommend.explain_match(
            cv_text=self.CV, job_text=self.JOB,
            percentile=90, total_candidates=200,
        )
        gap = next(s for s in signals if s.kind == "gap")
        assert "kubernetes" in gap.text

    def test_reports_rank_within_the_candidate_set(self):
        _reasons, signals = recommend.explain_match(
            cv_text=self.CV, job_text=self.JOB,
            percentile=92, total_candidates=342,
        )
        rank = next(s for s in signals if s.kind == "rank")
        assert "Top 8%" in rank.text
        assert "342" in rank.text

    def test_mid_pack_jobs_get_a_position_not_a_top_claim(self):
        _reasons, signals = recommend.explain_match(
            cv_text=self.CV, job_text=self.JOB,
            percentile=40, total_candidates=100,
        )
        rank = next(s for s in signals if s.kind == "rank")
        assert "Top" not in rank.text
        assert "#60" in rank.text

    def test_feedback_influence_is_only_claimed_when_real(self):
        weak = recommend.explain_match(
            cv_text=self.CV, job_text=self.JOB, percentile=80,
            total_candidates=50, liked_similarity=0.1,
        )[1]
        assert not any(s.kind == "liked" for s in weak)

        strong = recommend.explain_match(
            cv_text=self.CV, job_text=self.JOB, percentile=80,
            total_candidates=50, liked_similarity=0.8,
        )[1]
        assert any(s.kind == "liked" for s in strong)

    def test_no_cv_means_no_skill_or_gap_claims(self):
        _reasons, signals = recommend.explain_match(
            cv_text="", job_text=self.JOB, percentile=80, total_candidates=50,
        )
        kinds = {s.kind for s in signals}
        assert "skill" not in kinds and "gap" not in kinds

    def test_gaps_ignore_one_off_words_and_stopwords(self):
        gaps = recommend.match_gaps(
            "Python developer", "We need Python and one mention of rust. The team is great."
        )
        # "rust" appears once — incidental, not something the posting leans on.
        assert gaps == []
        assert "the" not in gaps


@pytest.mark.asyncio
async def test_feed_attaches_explanations(
    patched_db, session: AsyncSession, tmp_path, monkeypatch,
):
    """End to end: the feed carries the explanation, not just a number."""
    import gosha.cover_letter as cl_mod

    # monkeypatch, not an assignment: user 1's CV outlived this test and
    # every later feed test's user 1 had it.
    monkeypatch.setattr(cl_mod, "CV_DIR", tmp_path / "cvs")
    user = User(discord_user_id=20, cv_embedding=vec_to_bytes(unit_vec(0)))
    session.add(user)
    await session.flush()
    cl_mod.save_cv(user.id, "Python and Docker engineer")

    job = make_job(
        "https://r.com/why", 0,
        title="Python Engineer",
        description="Python and Docker every day. Docker Swarm, Python tooling, "
                    "and Kubernetes. Kubernetes is essential here.",
    )
    session.add(job)
    await session.commit()

    items, _ = await recommend.get_feed(user.id, page=1, per_page=10)
    kinds = {s.kind for s in items[0].signals}

    assert "skill" in kinds
    assert "gap" in kinds
    assert items[0].reasons  # compact card line still populated


def test_match_reasons_overlap():
    cv = "Experienced with Python, React and Docker deployments"
    jd = "We need Python and Docker skills. Docker is used daily, Kubernetes a plus"
    reasons = recommend.match_reasons(cv, jd)
    assert set(reasons) >= {"python", "docker"}
    # Docker appears twice in the JD -> ranked first
    assert reasons[0] == "docker"


def test_match_reasons_ignores_stopwords():
    cv = "I have experience and skills with the best team"
    jd = "We are the best team with experience and skills"
    reasons = recommend.match_reasons(cv, jd)
    assert "and" not in reasons
    assert "the" not in reasons
    assert "with" not in reasons


def test_match_reasons_keep_romanian_words_whole():
    cv = "Experiență cu aplicații React și TypeScript; cunoștințe de accesibilitate."
    jd = "Dezvoltăm aplicații React. Cerințe: aplicații web, TypeScript, accesibilitate."
    reasons = recommend.match_reasons(cv, jd)
    assert reasons[0] == "aplicații"  # three times in the posting, whole word
    assert {"react", "typescript", "accesibilitate"} <= set(recommend.match_reasons(cv, jd, top_k=5))
    assert not any(r.startswith("aplica") and r != "aplicații" for r in reasons)
    assert "cunoștințe" not in reasons and "și" not in reasons


def test_tokens_keep_skill_punctuation():
    # Unchanged by the Unicode fix: "+", "#" and inner dots stay part of a
    # term; terms are three characters or longer, as before.
    assert recommend._tokens("C++, Node.js and .NET.") == ["c++", "node.js", "and", "net"]


# ── feed hard filters: the user's active searches' location and level ──


def _search(user_id: int, locations, levels, *, active=True, remote_ok=False, blacklist=()):
    from gosha.models import Subscription

    sub = Subscription(user_id=user_id, max_age_days=7, remote_ok=remote_ok, is_active=active)
    sub.keywords = ["developer"]
    sub.locations = list(locations)
    sub.excluded_keywords = []
    sub.company_blacklist = list(blacklist)
    sub.experience_levels = list(levels)
    return sub


async def _feed_ids(user_id: int) -> set[int]:
    items, total = await recommend.get_feed(user_id, per_page=100)
    assert total == len(items)
    return {item.job.id for item in items}


@pytest.mark.asyncio
async def test_feed_keeps_what_some_active_search_would_take(patched_db, session: AsyncSession):
    """Per search, then any: "intern in Bucharest" plus "senior in Cluj"
    does not admit a Cluj internship, as the DM match stage would not."""
    user = User(discord_user_id=51, cv_embedding=vec_to_bytes(unit_vec(0)))
    session.add(user)
    await session.flush()
    jobs = {
        name: make_job(f"https://r.com/s/{name}", 0, title=title, location=where, description="")
        for name, title, where in [
            ("cluj_intern", "Software Intern", "Cluj-Napoca"),
            ("buc_intern", "Software Intern", "București"),
            ("cluj_senior", "Senior Developer", "Cluj-Napoca"),
            ("iasi_dev", "Developer", "Iasi"),
        ]
    }
    session.add_all([
        *jobs.values(),
        _search(user.id, ["Bucharest"], ["intern"]),
        _search(user.id, ["Cluj"], ["senior"]),
    ])
    await session.commit()

    assert await _feed_ids(user.id) == {jobs["buc_intern"].id, jobs["cluj_senior"].id}


@pytest.mark.asyncio
async def test_paused_searches_do_not_narrow_the_feed_but_their_blacklist_holds(
    patched_db, session: AsyncSession,
):
    user = User(discord_user_id=52, cv_embedding=vec_to_bytes(unit_vec(0)))
    session.add(user)
    await session.flush()
    kept = make_job("https://r.com/p/kept", 0, title="Developer", company="Good", location="Iasi")
    hidden = make_job("https://r.com/p/hidden", 0, title="Developer", company="Acme", location="Cluj")
    session.add_all([
        kept, hidden,
        _search(user.id, ["Cluj"], ["senior"], active=False, blacklist=["Acme"]),
    ])
    await session.commit()

    assert await _feed_ids(user.id) == {kept.id}


@pytest.mark.asyncio
async def test_no_active_search_means_no_scope_filter(patched_db, session: AsyncSession):
    user = User(discord_user_id=53, cv_embedding=vec_to_bytes(unit_vec(0)))
    jobs = [
        make_job("https://r.com/n/a", 0, title="Senior Developer", location="Iasi"),
        make_job("https://r.com/n/b", 0, title="Intern", location="Timisoara"),
    ]
    session.add_all([user, *jobs])
    await session.commit()
    assert await _feed_ids(user.id) == {job.id for job in jobs}


@pytest.mark.asyncio
async def test_remote_ok_admits_remote_postings(patched_db, session: AsyncSession):
    user = User(discord_user_id=54, cv_embedding=vec_to_bytes(unit_vec(0)))
    session.add(user)
    await session.flush()
    remote = make_job("https://r.com/r/remote", 0, title="Developer", location="Remote")
    elsewhere = make_job("https://r.com/r/far", 0, title="Developer", location="Timisoara")
    session.add_all([remote, elsewhere, _search(user.id, ["Cluj"], ["any"], remote_ok=True)])
    await session.commit()
    assert await _feed_ids(user.id) == {remote.id}


@pytest.mark.asyncio
async def test_scope_filter_leaves_the_keyword_window_whole(
    patched_db, session: AsyncSession, monkeypatch,
):
    """The scope narrows this user's candidates, not the IDF corpus every
    user and the demo share."""
    from gosha import lexical

    import gosha.cover_letter

    monkeypatch.setattr(gosha.cover_letter, "load_cv", lambda _uid: "Python developer")
    user = User(discord_user_id=55, cv_embedding=vec_to_bytes(unit_vec(0)))
    session.add(user)
    await session.flush()
    jobs = [
        make_job(f"https://r.com/i/{city}", 0, title="Python Developer", location=city,
                 description="Python, Django")
        for city in ("Cluj-Napoca", "Iasi", "Timisoara")
    ]
    session.add_all([*jobs, _search(user.id, ["Cluj"], ["any"])])
    await session.commit()

    loaded: list[list[Job]] = []
    real = recommend.load_candidates

    async def spy(*args, **kwargs):
        jobs = await real(*args, **kwargs)
        loaded.append(jobs)
        return jobs

    monkeypatch.setattr(recommend, "load_candidates", spy)
    assert await _feed_ids(user.id) == {jobs[0].id}
    [index] = lexical._indexes.values()
    assert {k[0] for k in index.keys} == {job.id for job in jobs}

    # The postings scoped out were tokenised for the IDF; their seniority
    # is never needed, so the next request does not read them again.
    assert await _feed_ids(user.id) == {jobs[0].id}
    assert {job.id for job in loaded[-1] if "description" in job.__dict__} == {jobs[0].id}


@pytest.mark.asyncio
async def test_level_scope_reads_each_description_once(
    patched_db, session: AsyncSession, monkeypatch,
):
    """An unmarked title needs its description to tell a graduate posting
    from one asking for five years; the verdict is cached like the level."""
    user = User(discord_user_id=56, cv_embedding=vec_to_bytes(unit_vec(0)))
    session.add(user)
    await session.flush()
    graduate = make_job("https://r.com/l/grad", 0, title="QA Engineer", location="Cluj",
                        description=_TEAM + "Recent graduates are welcome.")
    experienced = make_job("https://r.com/l/exp", 1, title="QA Engineer", location="Cluj",
                           description=_TEAM + "At least 5 years of experience in QA.")
    session.add_all([graduate, experienced, _search(user.id, ["Cluj"], ["junior"])])
    await session.commit()

    loaded: list[list[Job]] = []
    real = recommend.load_candidates

    async def spy(*args, **kwargs):
        jobs = await real(*args, **kwargs)
        loaded.append(jobs)
        return jobs

    monkeypatch.setattr(recommend, "load_candidates", spy)
    for request in range(2):
        assert await _feed_ids(user.id) == {graduate.id}
        read = {job.id for job in loaded[-1] if "description" in job.__dict__}
        # First request: both read for the verdict; then only the page.
        assert read == ({graduate.id, experienced.id} if request == 0 else {graduate.id})


# ── one posting once: copies the cross-board dedup keeps apart ──


def _vec_at(cosine: float) -> bytes:
    """A unit vector at this cosine to unit_vec(0)."""
    vec = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    vec[0], vec[1] = cosine, float(np.sqrt(1 - cosine**2))
    return vec_to_bytes(vec)


def _city_copies() -> tuple[Job, Job]:
    """One posting listed per city (LinkedIn does this): dedup keeps the
    two rows apart so a Timișoara search still finds its copy."""
    cluj = make_job("https://r.com/d/cluj", None, title="Graphics Engineer",
                    company="Accenture", location="Cluj-Napoca, CJ, Romania")
    cluj.embedding = _vec_at(0.9)
    timisoara = make_job("https://r.com/d/tm", None, title="Graphics Engineer",
                         company="Accenture SRL", location="Timișoara, TM, Romania")
    timisoara.embedding = _vec_at(0.8)
    return cluj, timisoara


@pytest.mark.asyncio
async def test_feed_shows_one_copy_of_a_posting_the_best_ranked(
    patched_db, session: AsyncSession,
):
    user = User(discord_user_id=60, cv_embedding=vec_to_bytes(unit_vec(0)))
    cluj, timisoara = _city_copies()
    other = make_job("https://r.com/d/other", None, title="Shader Developer", company="Accenture")
    other.embedding = _vec_at(0.7)
    session.add_all([user, cluj, timisoara, other])
    await session.commit()

    items, total = await recommend.get_feed(user.id, per_page=10)

    assert [item.job.id for item in items] == [cluj.id, other.id]
    assert total == 2
    assert [item.percentile for item in items] == [100, 0]


@pytest.mark.asyncio
async def test_the_copy_in_the_users_search_scope_is_the_one_shown(
    patched_db, session: AsyncSession,
):
    user = User(discord_user_id=61, cv_embedding=vec_to_bytes(unit_vec(0)))
    session.add(user)
    await session.flush()
    cluj, timisoara = _city_copies()
    session.add_all([cluj, timisoara, _search(user.id, ["Timisoara"], ["any"])])
    await session.commit()

    assert await _feed_ids(user.id) == {timisoara.id}


@pytest.mark.asyncio
async def test_a_rated_copy_hides_the_other_copies(patched_db, session: AsyncSession):
    user = User(discord_user_id=62, cv_embedding=vec_to_bytes(unit_vec(0)))
    session.add(user)
    cluj, timisoara = _city_copies()
    session.add_all([cluj, timisoara])
    await session.flush()
    session.add(UserJob(user_id=user.id, job_id=cluj.id, feedback="not_relevant"))
    await session.commit()

    assert await _feed_ids(user.id) == set()


@pytest.mark.asyncio
async def test_newest_first_feed_shows_one_copy_too(patched_db, session: AsyncSession):
    user = User(discord_user_id=63)
    cluj, timisoara = _city_copies()
    now = datetime.now(timezone.utc)
    cluj.first_seen_at = now - timedelta(days=1)
    timisoara.first_seen_at = now
    session.add_all([user, cluj, timisoara])
    await session.commit()

    items, total = await recommend.get_feed(user.id, per_page=10)
    assert [item.job.id for item in items] == [timisoara.id] and total == 1


def test_unidentifiable_postings_are_never_collapsed():
    """No real employer name: two "Developer" ads may be two employers
    (dedup.normalize_key returns None), so both stay."""
    jobs = [make_job(f"https://r.com/d/u{i}", 0, title="Developer", company="Unknown")
            for i in range(2)]
    items, total = recommend.rank_candidates(jobs, unit_vec(0))
    assert total == 2 and {id(item.job) for item in items} == {id(job) for job in jobs}


def test_the_demo_ranking_shows_one_copy_too():
    cluj, timisoara = _city_copies()
    items, total = recommend.rank_candidates([timisoara, cluj], unit_vec(0))
    assert [item.job for item in items] == [cluj] and total == 1


# ── one rule set: the feed scope applies the DM match stage's gates ──

_TEAM = "You will build data pipelines in Python and SQL with our team in Cluj. " * 4
_GATE_POSTINGS = [
    # (title, company, board label, description)
    ("Curs Consultant ERP", "EnterSoft", None,
     "Acesta nu este un anunț de angajare. Înscrie-te până la 23 octombrie. " + _TEAM),
    ("Software Engineer (AI Training)", "Alignerr", None, _TEAM),
    ("Programe de internship, trainee sau joburi entry-level in IT", "Career Hub", None, _TEAM),
    ("Data Analyst", "Acme", None, None),                           # no evidence either way
    ("Data Analyst", "Beta", None, _TEAM + "Recent graduates are welcome."),
    ("Data Analyst", "Gamma", None, _TEAM + "We require 5+ years of experience in SQL."),
    ("Data Analyst", "Delta", None, _TEAM),                         # says nothing of the level
    ("Software Engineering Intern", "Epsilon", None, _TEAM),
    ("Data Analyst", "Zeta", "intern", _TEAM),
    ("Senior Data Analyst", "Eta", None, _TEAM),
]
_GATE_SEARCHES = [
    (["intern"], ["developer"]),
    (["junior"], ["developer"]),
    (["intern", "junior"], ["developer"]),
    (["any"], ["software intern"]),     # entry level by its keyword
    (["any"], ["developer"]),
    (["senior"], ["developer"]),
]


@pytest.mark.parametrize("levels, keywords", _GATE_SEARCHES)
def test_feed_scope_takes_what_the_dm_match_stage_takes(levels, keywords, monkeypatch):
    """pipeline.job_matches_subscription is the oracle (freshness, salary
    and the user's exclusions aside, which the feed handles itself),
    without the entry evidence: DMs fail closed on it, the feed does not."""
    from gosha import filters
    from gosha.pipeline import job_matches_subscription

    sub = _search(1, ["Cluj"], levels)
    sub.keywords = list(keywords)
    scope = recommend.scope_of(sub)
    monkeypatch.setattr(filters, "DM_JUNIOR_RULE", "default")
    for title, company, label, description in _GATE_POSTINGS:
        job = Job(url="https://r.com/g", title=title, company=company, source="ejobs",
                  location="Cluj-Napoca", experience_level=label, description=description)
        assert recommend.in_search_scope(job, [scope]) == job_matches_subscription(
            job, sub, require_evidence=False,
        ), (title, company)


@pytest.mark.asyncio
async def test_feed_scope_drops_courses_but_keeps_postings_without_entry_evidence(
    patched_db, session: AsyncSession,
):
    user = User(discord_user_id=64, cv_embedding=vec_to_bytes(unit_vec(0)))
    session.add(user)
    await session.flush()
    course, gig, ad, bare, graduates, years, silent, intern, *_ = (
        make_job(f"https://r.com/g/{i}", 0, title=title, company=company,
                 experience_level=label, description=description, location="Cluj-Napoca")
        for i, (title, company, label, description) in enumerate(_GATE_POSTINGS)
    )
    session.add_all([course, gig, ad, bare, graduates, years, silent, intern,
                     _search(user.id, ["Cluj"], ["intern", "junior"])])
    await session.commit()

    assert await _feed_ids(user.id) == {bare.id, graduates.id, silent.id, intern.id}


@pytest.mark.asyncio
async def test_a_kept_copy_is_in_one_of_the_viewers_searches(patched_db, session: AsyncSession):
    """Copies are collapsed after the scope filter: with searches in both
    cities the best-ranked copy stands, and it is in one of them; with a
    search in one city, that city's copy stands even when it ranks lower."""
    for discord_id, cities, expected in ((65, ["Cluj", "Timisoara"], "cluj"),
                                         (66, ["Timisoara"], "tm"), (67, ["Cluj"], "cluj")):
        user = User(discord_user_id=discord_id, cv_embedding=vec_to_bytes(unit_vec(0)))
        session.add(user)
        await session.flush()
        session.add_all([_search(user.id, [city], ["any"]) for city in cities])
        await session.commit()
        if discord_id == 65:
            copies = dict(zip(("cluj", "tm"), _city_copies()))
            session.add_all(copies.values())
            await session.commit()
        items, total = await recommend.get_feed(user.id, per_page=10)
        assert [item.job.id for item in items] == [copies[expected].id] and total == 1
        scopes = await recommend.load_search_scopes(user.id)
        assert all(recommend.in_search_scope(item.job, scopes) for item in items)
