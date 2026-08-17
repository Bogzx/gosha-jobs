"""The CV must be read whole, not just its first window.

all-mpnet-base-v2 truncates at max_seq_length = 384 tokens (~1,500 chars)
and the repo never raises that limit, so the old `cv_text[:8000]` handed
the model four times more text than it could see and silently dropped the
rest. For a two-page CV that is everything after the first job — the
product's namesake claim ("the job feed that reads your CV") applied to
roughly its first quarter.

The fake encoder below deliberately reproduces that behaviour: it only
looks at MODEL_WINDOW_CHARS of whatever it is given. That is what makes
these tests meaningful without downloading a 400 MB model — the assertions
fail against truncation and pass against chunking for the same reason the
real model does.
"""

from __future__ import annotations

import re

import numpy as np
import pytest

from gosha import embeddings

# Stand-in for the model's 384-token window.
MODEL_WINDOW_CHARS = 1500

VOCAB = [
    "kubernetes", "rust", "terraform", "marketing", "copywriting",
    "photoshop", "seo", "branding", "python", "react",
]
_WORD_RE = re.compile(r"[a-z]+")


def fake_encode(texts: list[str]) -> np.ndarray:
    """Bag-of-vocabulary vectors that see only the first window of text."""
    rows = []
    for text in texts:
        window = text.lower()[:MODEL_WINDOW_CHARS]
        tokens = set(_WORD_RE.findall(window))
        vec = np.zeros(embeddings.EMBEDDING_DIM, dtype=np.float32)
        for i, term in enumerate(VOCAB):
            vec[i] = 1.0 if term in tokens else 0.0
        norm = np.linalg.norm(vec)
        if norm:
            vec /= norm
        rows.append(vec)
    return np.stack(rows)


FILLER = (
    "Built and shipped web features end to end, working closely with "
    "designers and product on delivery timelines and quality. "
)


def _long_cv_with_tail_skill() -> str:
    """~6k chars of generic experience, then the skills that matter.

    Mirrors a real two-page CV: the technical-skills block lives at the
    bottom, well past the first model window.
    """
    head = FILLER * 60
    assert len(head) > 4 * MODEL_WINDOW_CHARS
    return head + "\n\nTechnical skills: kubernetes, rust, terraform."


# ---------------------------------------------------------------------------
# Chunking mechanics
# ---------------------------------------------------------------------------


def test_short_text_is_a_single_chunk():
    assert embeddings.chunk_cv_text("Python and React") == ["Python and React"]


def test_empty_text_produces_no_chunks():
    assert embeddings.chunk_cv_text("") == []
    assert embeddings.chunk_cv_text("   \n ") == []


def test_long_text_is_split_into_windows_the_model_can_read():
    cv = _long_cv_with_tail_skill()
    chunks = embeddings.chunk_cv_text(cv)

    assert len(chunks) > 1
    assert all(len(c) <= embeddings.CV_CHUNK_CHARS for c in chunks)


def test_every_part_of_the_cv_lands_in_some_chunk():
    cv = _long_cv_with_tail_skill()
    joined = " ".join(embeddings.chunk_cv_text(cv)).lower()

    for term in ("kubernetes", "rust", "terraform"):
        assert term in joined, f"{term} was dropped by chunking"


def test_chunks_overlap_so_a_skill_on_a_boundary_survives():
    cv = "word " * 2000
    chunks = embeddings.chunk_cv_text(cv)
    # Consecutive windows share text rather than butting up against each
    # other, so a term straddling a boundary is intact in one of them.
    assert len(chunks) >= 2
    total = sum(len(c) for c in chunks)
    assert total > len(cv.strip())


def test_chunk_count_is_bounded():
    huge = "skill " * 200_000
    assert len(embeddings.chunk_cv_text(huge)) <= embeddings.MAX_CV_CHUNKS


# ---------------------------------------------------------------------------
# The claim that matters: the tail changes the ranking
# ---------------------------------------------------------------------------


def test_pooled_vector_carries_a_skill_named_only_at_the_end(monkeypatch):
    monkeypatch.setattr(embeddings, "encode_texts", fake_encode)

    cv = _long_cv_with_tail_skill()
    kubernetes_dim = VOCAB.index("kubernetes")

    truncated = fake_encode([cv[:8000]])[0]          # the old behaviour
    pooled = embeddings.embed_long_text(cv)          # the new behaviour

    assert truncated[kubernetes_dim] == 0.0, "fixture must exercise truncation"
    assert pooled is not None
    assert pooled[kubernetes_dim] > 0.0


def test_tail_skill_moves_its_job_up_the_ranking(monkeypatch):
    """The product claim, asserted end to end.

    Same CV, same jobs, same scoring — only the embedding changes. Under
    truncation the infra role is literally invisible: it scores zero and
    ties with a job the candidate has nothing in common with. Once the
    whole CV is embedded it scores and it wins.
    """
    monkeypatch.setattr(embeddings, "encode_texts", fake_encode)

    cv = _long_cv_with_tail_skill()
    jobs = {
        "infra": "Platform engineer working with kubernetes, terraform and rust",
        "unrelated": "Brand designer for photoshop, branding and seo campaigns",
    }
    job_vectors = {name: fake_encode([text])[0] for name, text in jobs.items()}

    def score(cv_vector, name):
        return float(job_vectors[name] @ cv_vector)

    truncated = fake_encode([cv[:8000]])[0]
    chunked = embeddings.embed_long_text(cv)
    assert chunked is not None

    # Before: the skills block never reached the model, so the matching
    # job was indistinguishable from one the candidate cannot do.
    assert score(truncated, "infra") == pytest.approx(0.0)
    assert score(truncated, "infra") == pytest.approx(score(truncated, "unrelated"))

    # After: it scores, and it outranks the unrelated job.
    assert score(chunked, "infra") > 0.0
    assert score(chunked, "infra") > score(chunked, "unrelated")


@pytest.mark.asyncio
async def test_stored_cv_embedding_uses_the_whole_document(
    patched_db, session, monkeypatch,
):
    from gosha.models import User

    monkeypatch.setattr(embeddings, "encode_texts", fake_encode)
    user = User(discord_user_id=4242)
    session.add(user)
    await session.commit()

    assert await embeddings.embed_user_cv(user.id, _long_cv_with_tail_skill())

    await session.refresh(user)
    stored = embeddings.bytes_to_vec(user.cv_embedding)
    assert stored.shape == (embeddings.EMBEDDING_DIM,)
    assert stored[VOCAB.index("kubernetes")] > 0.0
    assert np.isclose(np.linalg.norm(stored), 1.0, atol=1e-5)


def test_embed_long_text_is_none_without_a_model(monkeypatch):
    monkeypatch.setattr(embeddings, "encode_texts", lambda texts: None)
    assert embeddings.embed_long_text("anything at all") is None
