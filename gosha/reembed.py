"""Re-embed stored job and CV vectors with a (new) sentence-transformers model.

The one-off half of a SEMANTIC_MODEL switch (procedure: docs/EMBEDDINGS.md).
Every stored vector records its model, so this is naturally resumable:
each batch commits, and a rerun only touches rows whose recorded model is
not the target. Interrupt it, restart it, run it twice — same result.

Jobs: every job with a vector (active or not — liked/disliked jobs feed the
user's ranking vector even after they close) plus active jobs without one.
CVs: every user with a stored CV file, re-read (decrypted) from disk and
embedded with the same chunk-and-pool method as an upload.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import or_, select

import gosha.cover_letter as cv_storage
from gosha import embeddings, matching
from gosha.database import get_session
from gosha.models import Job, User

log = logging.getLogger(__name__)

DEFAULT_BATCH = 256


@dataclass
class ReembedReport:
    model: str
    dry_run: bool
    jobs_pending: int = 0
    jobs_done: int = 0
    cvs_pending: int = 0
    cvs_done: int = 0
    cvs_failed: int = 0

    def summary(self) -> str:
        if self.dry_run:
            return (
                f"[dry run] {self.model}: {self.jobs_pending} job(s) and "
                f"{self.cvs_pending} CV(s) would be re-embedded."
            )
        return (
            f"{self.model}: re-embedded {self.jobs_done}/{self.jobs_pending} "
            f"job(s), {self.cvs_done}/{self.cvs_pending} CV(s)"
            + (f", {self.cvs_failed} CV(s) failed" if self.cvs_failed else "")
            + "."
        )


def _jobs_needing(model: str):
    return or_(
        Job.embedding.isnot(None) & embeddings.stale_model_clause(Job.embedding_model, model),
        Job.embedding.is_(None) & Job.is_active.is_(True),
    )


async def count_pending(model: str) -> tuple[int, list[int]]:
    """(jobs needing a vector from `model`, user ids whose CV does)."""
    from sqlalchemy import func

    async with get_session() as session:
        jobs = (await session.execute(
            select(func.count()).select_from(Job).where(_jobs_needing(model))
        )).scalar() or 0
        with_cv = set(cv_storage.stored_cv_user_ids())
        users = (await session.execute(
            select(User.id, User.cv_embedding, User.cv_embedding_model)
            .where(User.id.in_(with_cv))
        )).all() if with_cv else []
    cv_ids = [
        uid for uid, vec, used in users
        if vec is None or (used or matching.LEGACY_MODEL) != model
    ]
    return jobs, sorted(cv_ids)


async def reembed_jobs(
    model: str,
    batch_size: int = DEFAULT_BATCH,
    progress: Callable[[int], None] | None = None,
) -> int:
    """Re-embed jobs batch by batch; returns how many were written."""
    done = 0
    while True:
        async with get_session() as session:
            jobs = list((await session.execute(
                select(Job).where(_jobs_needing(model)).order_by(Job.id).limit(batch_size)
            )).scalars().all())
            if not jobs:
                return done
            texts = [
                matching.build_job_text(j.title, j.company, j.description) for j in jobs
            ]
            vectors = await asyncio.to_thread(embeddings.encode_with_model, texts, model)
            if vectors is None:
                raise RuntimeError(
                    f"Model {model!r} is unavailable (sentence-transformers "
                    "not installed, or the model name is wrong)."
                )
            for job, vec in zip(jobs, vectors):
                job.embedding = embeddings.vec_to_bytes(vec)
                job.embedding_model = model
            await session.commit()
        done += len(jobs)
        if progress:
            progress(done)


async def reembed_cvs(
    model: str,
    user_ids: list[int],
    progress: Callable[[int], None] | None = None,
) -> tuple[int, int]:
    """Re-embed the given users' CVs; returns (done, failed)."""
    done = failed = 0

    def encode(chunks: list[str]):
        return embeddings.encode_with_model(chunks, model)

    for uid in user_ids:
        try:
            text = cv_storage.load_cv(uid)
            if not text:
                continue
            pooled = await asyncio.to_thread(embeddings.embed_long_text, text, encode)
            if pooled is None:
                raise RuntimeError("no vector produced")
            async with get_session() as session:
                user = await session.get(User, uid)
                if user is None:
                    continue
                user.cv_embedding = embeddings.vec_to_bytes(pooled)
                user.cv_embedding_model = model
                await session.commit()
            done += 1
        except Exception as exc:  # one bad CV must not stop the run
            failed += 1
            log.warning("Re-embedding the CV of user %d failed: %s", uid, exc)
        if progress:
            progress(done)
    return done, failed


async def reembed(
    model: str | None = None,
    *,
    jobs: bool = True,
    cvs: bool = True,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH,
    progress: Callable[[str, int], None] | None = None,
) -> ReembedReport:
    model = model or matching.DEFAULT_MODEL
    jobs_pending, cv_ids = await count_pending(model)
    report = ReembedReport(
        model=model, dry_run=dry_run,
        jobs_pending=jobs_pending if jobs else 0,
        cvs_pending=len(cv_ids) if cvs else 0,
    )
    if dry_run:
        return report
    if jobs:
        report.jobs_done = await reembed_jobs(
            model, batch_size, (lambda n: progress("jobs", n)) if progress else None,
        )
    if cvs:
        report.cvs_done, report.cvs_failed = await reembed_cvs(
            model, cv_ids, (lambda n: progress("cvs", n)) if progress else None,
        )
    return report
