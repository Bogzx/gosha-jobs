"""Every DM says whether the job is an internship or a junior job.

Owner decision (2026-10-17): internship and junior must be clearly
different for users, so the DM shows the posting's level among the
search's levels ("Level: Internship" / "Level: Junior"), and nothing when
that level is unclear or the search is not an entry-level one.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import gosha.pipeline as pipeline
from gosha.models import Job, Subscription, User, UserJob
from gosha.views import build_job_embed_with_buttons

NOW = datetime.now(timezone.utc)
TEXT = (
    "You will build data pipelines in Python and SQL with our team in Cluj, with a "
    "mentor and a buddy for your first projects. Recent graduates are welcome."
)
INTERN = dict(title="Python Developer Intern", url="https://d.test/intern")
JUNIOR = dict(title="Junior Python Developer", url="https://d.test/junior")


def _job(posting: dict, id: int = 7) -> Job:
    return Job(id=id, company="Acme", location="Cluj-Napoca, Romania", source="ejobs",
               description=TEXT, first_seen_at=NOW, **posting)


def _level(posting: dict, levels: list[str] | None) -> str | None:
    embed, _view = build_job_embed_with_buttons(_job(posting), 1, levels=levels)
    fields = {field.name: field.value for field in embed.fields}
    if "Level" in fields:
        assert embed.fields[0].name == "Level" and embed.fields[0].inline
    return fields.get("Level")


@pytest.mark.parametrize("posting, levels, shown", [
    (INTERN, ["intern"], "Internship"),
    (JUNIOR, ["junior"], "Junior"),
    # A search for both: the level of this posting.
    (INTERN, ["intern", "junior"], "Internship"),
    (JUNIOR, ["intern", "junior"], "Junior"),
])
def test_the_dm_names_the_level(posting, levels, shown):
    assert _level(posting, levels) == shown


@pytest.mark.parametrize("levels", [None, [], ["any"]])
def test_no_level_field_outside_entry_level_searches(levels):
    assert _level(JUNIOR, levels) is None


# ── At send time: deliver_pending passes the search's levels ──────────


async def _pending(session, levels: list[str], postings: list[dict]) -> None:
    user = User(discord_user_id=1001)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id)
    sub.keywords = ["python developer"]
    sub.locations = ["cluj"]
    sub.experience_levels = levels
    session.add(sub)
    jobs = [Job(company=f"Co{i}", location="Cluj-Napoca, Romania", source="ejobs",
                description=TEXT, first_seen_at=NOW, **posting)
            for i, posting in enumerate(postings)]
    session.add_all(jobs)
    await session.flush()
    session.add_all(
        UserJob(user_id=user.id, job_id=job.id, subscription_id=sub.id,
                relevance_score=0.6, dm_status=UserJob.DM_PENDING)
        for job in jobs
    )
    await session.commit()


def _sent_levels(monkeypatch) -> dict[str, str | None]:
    sent: dict[str, str | None] = {}

    async def fake(bot, discord_id, embed, view=None):
        sent[embed.title] = next(
            (field.value for field in embed.fields if field.name == "Level"), None,
        )
        return pipeline.DM_SENT

    monkeypatch.setattr(pipeline, "_dm_user", fake)
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize("levels, postings, shown", [
    (["intern"], [INTERN], {"Python Developer Intern": "Internship"}),
    (["junior"], [JUNIOR], {"Junior Python Developer": "Junior"}),
    (["intern", "junior"], [INTERN, JUNIOR],
     {"Python Developer Intern": "Internship", "Junior Python Developer": "Junior"}),
])
async def test_sent_dms_name_the_level(patched_db, session, monkeypatch, levels, postings,
                                       shown):
    sent = _sent_levels(monkeypatch)
    await _pending(session, levels, postings)
    assert await pipeline.deliver_pending(object()) == len(postings)
    assert sent == shown
