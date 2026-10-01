"""Public demo feed: the real ranking, for a sample CV, without signing in.

A visitor picks one of a few synthetic CVs (gosha/demo_personas.py) and
gets the live postings ranked for it by gosha.recommend.rank_candidates —
the same code a signed-in user's feed runs, "why this matched" included.

What it deliberately does not do: accept any text from the visitor (only
a persona id, so the model never runs on visitor input and nothing about
them is stored), touch user data, or publish the whole job table — only
the top DEMO_LIMIT postings per persona, with list-length descriptions,
as the signed-in list views get.

Cost control, since this is the one anonymous endpoint that touches the
model: all persona vectors are computed once, together, under a lock (a
burst of first requests after a restart must not each load the ~2 GB
model; gosha/matching.py also serialises the load itself); the candidate
set is cached for CANDIDATE_TTL_SECONDS and each persona's response for
RESPONSE_TTL_SECONDS; ranking runs in the threadpool, off the event loop.
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from gosha import ratelimit
from gosha.api.deps import ApiError, client_ip, enforce_rate_limit
from gosha.api.jobs import job_to_out
from gosha.api.schemas import DemoFeedOut, DemoPersonaOut
from gosha.demo_personas import BY_ID, DEMO_PERSONAS, DemoPersona
from gosha.models import Job

router = APIRouter(prefix="/demo", tags=["demo"])

DEMO_LIMIT = 25
CANDIDATE_TTL_SECONDS = 300
RESPONSE_TTL_SECONDS = 60

# Persona vectors for one model: (model name, {persona id: vector}).
_vectors: tuple[str, dict[str, np.ndarray]] | None = None
_candidates: tuple[float, list[Job]] | None = None
# persona id -> (expires at, response)
_responses: dict[str, tuple[float, DemoFeedOut]] = {}
_vectors_lock = asyncio.Lock()
_candidates_lock = asyncio.Lock()


def _persona_out(persona: DemoPersona) -> DemoPersonaOut:
    return DemoPersonaOut(
        id=persona.id, label=persona.label, summary=persona.summary, cv=persona.cv,
    )


def _embed_personas() -> dict[str, np.ndarray] | None:
    """Every persona's CV vector (the uploaded-CV path); None without a model."""
    from gosha.embeddings import embed_long_text

    vectors = {}
    for persona in DEMO_PERSONAS:
        vector = embed_long_text(persona.cv)
        if vector is None:
            return None
        vectors[persona.id] = vector
    return vectors


async def _persona_vectors() -> dict[str, np.ndarray] | None:
    global _vectors
    from gosha.embeddings import current_model

    model = current_model()
    async with _vectors_lock:
        if _vectors is None or _vectors[0] != model:
            vectors = await run_in_threadpool(_embed_personas)
            if vectors is None:
                return None  # retried on the next request
            _vectors = (model, vectors)
        return _vectors[1]


async def _demo_candidates() -> list[Job]:
    global _candidates
    from gosha.recommend import load_candidates

    async with _candidates_lock:
        if _candidates is None or time.monotonic() - _candidates[0] > CANDIDATE_TTL_SECONDS:
            _candidates = (time.monotonic(), await load_candidates())
        return _candidates[1]


def _build_response(
    persona: DemoPersona, vector: np.ndarray, candidates: list[Job],
) -> DemoFeedOut:
    """Rank and serialise (CPU-bound: tens of ms, so not on the loop)."""
    from gosha.recommend import rank_candidates

    items, total = rank_candidates(
        candidates, vector, cv_text=persona.cv, page=1, per_page=DEMO_LIMIT,
    )
    return DemoFeedOut(
        persona=_persona_out(persona),
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
    )


@router.get("/personas", response_model=list[DemoPersonaOut])
async def personas() -> list[DemoPersonaOut]:
    return [_persona_out(p) for p in DEMO_PERSONAS]


@router.get("/feed", response_model=DemoFeedOut)
async def demo_feed(
    request: Request,
    persona: str = Query(..., max_length=40),
) -> DemoFeedOut:
    await enforce_rate_limit(ratelimit.DEMO, client_ip(request))
    chosen = BY_ID.get(persona)
    if chosen is None:
        raise ApiError(404, "not_found", "Unknown demo persona.")

    cached = _responses.get(chosen.id)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]

    vectors = await _persona_vectors()
    if vectors is None:
        raise ApiError(503, "unavailable", "The matching model is not available right now.")

    response = await run_in_threadpool(
        _build_response, chosen, vectors[chosen.id], await _demo_candidates(),
    )
    _responses[chosen.id] = (time.monotonic() + RESPONSE_TTL_SECONDS, response)
    return response
