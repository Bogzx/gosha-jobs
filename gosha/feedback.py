"""Record a user's 👍/👎 on a delivered job.

What the feedback does lives in gosha/recommend.py: a Rocchio update
(apply_feedback) moves the user's CV vector toward liked postings and
away from disliked ones, and that one vector ranks both the web feed and
the Discord deliveries. Its weights come from the offline eval
(docs/EVAL.md).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from gosha.database import get_session
from gosha.models import UserJob

log = logging.getLogger(__name__)


async def record_feedback(
    user_job_id: int,
    feedback: str,
) -> bool:
    """Record user feedback on a delivered job.

    Args:
        user_job_id: The UserJob record ID.
        feedback: "interested" or "not_relevant".

    Returns:
        True if feedback was recorded, False if not found.
    """
    if feedback not in ("interested", "not_relevant"):
        return False

    async with get_session() as session:
        result = await session.execute(
            select(UserJob).where(UserJob.id == user_job_id)
        )
        uj = result.scalar_one_or_none()
        if uj is None:
            return False

        uj.feedback = feedback
        uj.feedback_at = datetime.now(timezone.utc)
        await session.commit()

        # Emit event (best-effort)
        try:
            from gosha.events import emit_user_feedback
            await emit_user_feedback(uj.job_id, uj.user_id, feedback)
        except Exception:
            pass

        return True
