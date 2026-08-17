"""Jobs HTTP adapter — translates requests to gosha.services.jobs calls."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from gosha.api.deps import current_user
from gosha.api.schemas import (
    ApplicationOut,
    FeedbackIn,
    JobListOut,
    JobOut,
    JobSummaryOut,
    OkOut,
)
from gosha.models import Application, Job, User
from gosha.services import jobs as jobs_service

router = APIRouter(prefix="/jobs", tags=["jobs"])

LIST_DESCRIPTION_CHARS = 240


def job_to_out(
    job: Job,
    *,
    truncate: bool = True,
    feedback: str | None = None,
    applied: bool = False,
    match_score: float | None = None,
    match_percentile: int | None = None,
    match_reasons: list[str] | None = None,
) -> JobOut:
    description = job.description
    if truncate and description and len(description) > LIST_DESCRIPTION_CHARS:
        description = description[:LIST_DESCRIPTION_CHARS] + "…"
    return JobOut(
        id=job.id,
        url=job.url,
        title=job.title,
        company=job.company,
        location=job.location,
        description=description,
        salary_min=job.salary_min,
        salary_max=job.salary_max,
        salary_currency=job.salary_currency,
        source=job.source,
        posted_at=job.posted_at,
        first_seen_at=job.first_seen_at,
        match_score=match_score,
        match_percentile=match_percentile,
        match_reasons=match_reasons,
        feedback=feedback,
        applied=applied,
    )


def application_to_out(app: Application, job: Job) -> ApplicationOut:
    return ApplicationOut(
        id=app.id,
        job_id=app.job_id,
        status=app.status,
        notes=app.notes,
        applied_at=app.applied_at,
        updated_at=app.updated_at,
        source=app.source,
        job=JobSummaryOut(
            id=job.id,
            title=job.title,
            company=job.company,
            location=job.location,
            url=job.url,
            source=job.source,
        ),
    )


def _csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


@router.get("", response_model=JobListOut)
async def list_jobs(
    user: User = Depends(current_user),
    q: str = "",
    locations: str = "",
    experience: str = "",
    sources: str = "",
    salary_min: int | None = None,
    posted_within_days: int | None = Query(default=None, ge=1, le=90),
    remote: bool = False,
    sort: str = Query(default="date", pattern="^(date|match)$"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=100),
) -> JobListOut:
    filters = jobs_service.JobFilters(
        q=q,
        locations=_csv(locations),
        experience=_csv(experience),
        sources=_csv(sources),
        salary_min=salary_min,
        posted_within_days=posted_within_days,
        remote=remote,
    )
    page_jobs, total = await jobs_service.list_jobs(user.id, filters, page, per_page)

    feedback, applied = await jobs_service.annotate_user_state(
        user.id, [j.id for j in page_jobs]
    )
    items = [
        job_to_out(j, feedback=feedback.get(j.id), applied=j.id in applied)
        for j in page_jobs
    ]
    return JobListOut(items=items, total=total, page=page, per_page=per_page)


@router.get("/{job_id}", response_model=JobOut)
async def job_detail(job_id: int, user: User = Depends(current_user)) -> JobOut:
    job = await jobs_service.get_job(job_id)
    feedback, applied = await jobs_service.annotate_user_state(user.id, [job.id])
    return job_to_out(
        job,
        truncate=False,
        feedback=feedback.get(job.id),
        applied=job.id in applied,
    )


@router.post("/{job_id}/feedback", response_model=OkOut)
async def job_feedback(
    job_id: int, body: FeedbackIn, user: User = Depends(current_user),
) -> OkOut:
    await jobs_service.set_feedback(user.id, job_id, body.feedback)
    return OkOut()


@router.post("/{job_id}/apply-click", response_model=ApplicationOut)
async def apply_click(job_id: int, user: User = Depends(current_user)) -> ApplicationOut:
    """Record that the user clicked Apply — auto-tracks the application."""
    app, job = await jobs_service.record_apply_click(user.id, job_id)
    return application_to_out(app, job)
