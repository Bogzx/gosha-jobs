"""Public demo feed: the real ranking, for a sample CV, without signing in.

A visitor picks one of a few synthetic CVs (gosha/demo_personas.py) and
gets the live postings ranked for it by gosha.recommend.rank_candidates —
the same code a signed-in user's feed runs, "why this matched" included.

What it deliberately does not do: accept any text from the visitor (only
a persona id, so the model never runs on visitor input and nothing about
them is stored), or touch user data. Persona vectors are computed once per
process; the candidate set is cached for a few minutes, so a request costs
one matrix product and a page of explanations.
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
from fastapi import APIRouter, Query, Request

from gosha import ratelimit
from gosha.api.deps import ApiError, client_ip, enforce_rate_limit
from gosha.api.jobs import job_to_out
from gosha.api.schemas import DemoFeedOut, DemoPersonaOut
from gosha.demo_personas import BY_ID, DEMO_PERSONAS, DemoPersona
from gosha.models import Job

router = APIRouter(prefix="/demo", tags=["demo"])

CANDIDATE_TTL_SECONDS = 300
MAX_PAGE = 5

# (model name, vector) per persona id; recomputed if SEMANTIC_MODEL changes.
_vectors: dict[str, tuple[str, np.ndarray]] = {}
_candidates: tuple[float, list[Job]] | None = None
_lock = asyncio.Lock()


def _persona_out(persona: DemoPersona) -> DemoPersonaOut:
    return DemoPersonaOut(
        id=persona.id, label=persona.label, summary=persona.summary, cv=persona.cv,
    )


async def _persona_vector(persona: DemoPersona) -> np.ndarray | None:
    from gosha.embeddings import current_model, embed_long_text

    model = current_model()
    cached = _vectors.get(persona.id)
    if cached is not None and cached[0] == model:
        return cached[1]
    # CPU-bound (the same chunk-and-pool path as an uploaded CV): off the loop.
    vector = await asyncio.to_thread(embed_long_text, persona.cv)
    if vector is not None:
        _vectors[persona.id] = (model, vector)
    return vector


async def _demo_candidates() -> list[Job]:
    global _candidates
    from gosha.recommend import load_candidates

    async with _lock:
        if _candidates is None or time.monotonic() - _candidates[0] > CANDIDATE_TTL_SECONDS:
            _candidates = (time.monotonic(), await load_candidates())
        return _candidates[1]


@router.get("/personas", response_model=list[DemoPersonaOut])
async def personas() -> list[DemoPersonaOut]:
    return [_persona_out(p) for p in DEMO_PERSONAS]


@router.get("/feed", response_model=DemoFeedOut)
async def demo_feed(
    request: Request,
    persona: str = Query(..., max_length=40),
    page: int = Query(default=1, ge=1, le=MAX_PAGE),
    per_page: int = Query(default=20, ge=1, le=50),
) -> DemoFeedOut:
    await enforce_rate_limit(ratelimit.DEMO, client_ip(request))
    chosen = BY_ID.get(persona)
    if chosen is None:
        raise ApiError(404, "not_found", "Unknown demo persona.")

    vector = await _persona_vector(chosen)
    if vector is None:
        raise ApiError(503, "unavailable", "The matching model is not available right now.")

    from gosha.recommend import rank_candidates

    items, total = rank_candidates(
        await _demo_candidates(), vector, cv_text=chosen.cv, page=page, per_page=per_page,
    )
    return DemoFeedOut(
        persona=_persona_out(chosen),
        items=[
            job_to_out(
                item.job,
                match_score=item.score,
                match_percentile=item.percentile,
                match_reasons=item.reasons,
                match_signals=list(item.signals),
            )
            for item in items
        ],
        total=total,
        page=page,
        per_page=per_page,
    )
