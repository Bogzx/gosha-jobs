"""Application tracker use cases."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from gosha.database import get_session
from gosha.domain.errors import InvalidStatusError, NotFoundError
from gosha.models import APPLICATION_STATUSES, Application, Job

# Default for a field the caller did not send; None is a value (it clears).
UNCHANGED: Any = object()


async def _find_application(session, user_id: int, job_id: int) -> Application | None:
    return (
        await session.execute(
            select(Application).where(
                Application.user_id == user_id, Application.job_id == job_id,
            )
        )
    ).scalar_one_or_none()


async def track_application(user_id: int, job_id: int, source: str) -> tuple[Application, bool]:
    """(application, created): the user's row for this job, inserted if new.

    Idempotent under concurrency too: a double-click sends two requests that
    both find nothing, and the second insert hits uq_user_application. That
    one now returns the row the first created instead of a 500.
    """
    async with get_session() as session:
        app = await _find_application(session, user_id, job_id)
        if app is not None:
            return app, False
        app = Application(user_id=user_id, job_id=job_id, source=source)
        session.add(app)
        try:
            await session.commit()
            return app, True
        except IntegrityError:
            await session.rollback()
            app = await _find_application(session, user_id, job_id)
            if app is None:  # not the duplicate we expected
                raise
            return app, False


async def list_applications(user_id: int) -> list[tuple[Application, Job]]:
    async with get_session() as session:
        result = await session.execute(
            select(Application, Job)
            .join(Job, Application.job_id == Job.id)
            .where(Application.user_id == user_id)
            .order_by(Application.updated_at.desc())
        )
        return [(app, job) for app, job in result.all()]


async def create_application(user_id: int, job_id: int) -> tuple[Application, Job]:
    """Idempotent: returns the existing application when already tracked."""
    async with get_session() as session:
        job = await session.get(Job, job_id)
    if job is None:
        raise NotFoundError("Job not found.")
    app, _created = await track_application(user_id, job_id, "web")
    return app, job


async def _own_application(user_id: int, application_id: int) -> tuple[Application, Job]:
    async with get_session() as session:
        row = (
            await session.execute(
                select(Application, Job)
                .join(Job, Application.job_id == Job.id)
                .where(
                    Application.id == application_id,
                    Application.user_id == user_id,
                )
            )
        ).one_or_none()
    if row is None:
        raise NotFoundError("Application not found.")
    return row[0], row[1]


async def update_application(
    user_id: int,
    application_id: int,
    status: str | None = None,
    notes: str | None = UNCHANGED,
) -> tuple[Application, Job]:
    """Change status and/or notes. Notes left out are kept; notes=None
    clears them (the SPA sends null for an emptied box)."""
    if status is not None and status not in APPLICATION_STATUSES:
        raise InvalidStatusError(
            f"Status must be one of: {', '.join(APPLICATION_STATUSES)}."
        )

    app, job = await _own_application(user_id, application_id)
    async with get_session() as session:
        app = await session.merge(app)
        if status is not None:
            app.status = status
        if notes is not UNCHANGED:
            app.notes = notes
        app.updated_at = datetime.now(timezone.utc)
        await session.commit()
    return app, job


async def delete_application(user_id: int, application_id: int) -> None:
    app, _job = await _own_application(user_id, application_id)
    async with get_session() as session:
        app = await session.merge(app)
        await session.delete(app)
        await session.commit()
