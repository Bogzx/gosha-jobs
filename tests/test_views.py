"""Discord job-embed buttons after a feedback click."""

from __future__ import annotations

import discord

from gosha.views import JobFeedbackView, _view_after_feedback


class _Message:
    """What View.from_message reads: the message's component rows."""

    def __init__(self, rows: list[dict]) -> None:
        self.components = [discord.components.ActionRow(row) for row in rows]


def _delivered_message() -> _Message:
    return _Message([{"type": 1, "components": [
        {"type": 2, "style": 5, "label": "Apply", "url": "https://jobs.test/1"},
        {"type": 2, "style": 1, "label": "Cover Letter", "custom_id": "cover_letter:9:4"},
        {"type": 2, "style": 3, "label": "Interested", "custom_id": "job_feedback:interested:4"},
        {"type": 2, "style": 2, "label": "Not Relevant",
         "custom_id": "job_feedback:not_relevant:4"},
    ]}])


def test_feedback_keeps_apply_and_cover_letter():
    view = _view_after_feedback(_delivered_message(), "interested")
    by_label = {item.label: item for item in view.children}

    assert by_label["Apply"].url == "https://jobs.test/1"
    assert not by_label["Apply"].disabled
    assert not by_label["Cover Letter"].disabled
    assert by_label["Feedback: Interested"].disabled
    assert by_label["Not Relevant"].disabled


def test_feedback_without_a_message_still_confirms():
    view = _view_after_feedback(None, "not_relevant")
    (item,) = view.children
    assert item.label == "Feedback: Not Relevant" and item.disabled


def test_buttons_have_no_callbacks_of_their_own():
    """Clicks are handled once, by the global on_interaction listener."""
    view = JobFeedbackView(4, job_url="https://jobs.test/1", job_id=9)
    for item in view.children:
        # The instance's callback is the library default, not a handler of
        # the view's (which the old code assigned per button).
        assert getattr(item.callback, "__func__", None) is discord.ui.Button.callback


async def test_cover_letter_embed_uses_the_quota_safe_service(patched_db, session, monkeypatch):
    """Discord goes through the website's claim-before-generate path."""
    import gosha.services.cover_letters as service
    from gosha.domain.errors import GenerationInProgressError, QuotaExceededError
    from gosha.models import Job, User
    from gosha.views import cover_letter_embed

    user = User(discord_user_id=777)
    job = Job(url="https://cl.test/1", title="T" * 400, company="Acme", source="ejobs")
    session.add_all([user, job])
    await session.commit()

    calls: list[tuple[int, int]] = []

    async def letter(u, job_id):
        calls.append((u.id, job_id))
        return "Dear Acme, ...", False

    monkeypatch.setattr(service, "generate_for_job", letter)
    embed = await cover_letter_embed(user, job.id)
    assert isinstance(embed, discord.Embed)
    assert len(embed.title) == 256 and embed.description == "Dear Acme, ..."
    assert calls == [(user.id, job.id)]

    async def busy(u, job_id):
        raise GenerationInProgressError("in progress")

    monkeypatch.setattr(service, "generate_for_job", busy)
    assert "still being written" in await cover_letter_embed(user, job.id)

    async def spent(u, job_id):
        raise QuotaExceededError("You've used all 5 cover letters this month.")

    monkeypatch.setattr(service, "generate_for_job", spent)
    assert "used all 5" in await cover_letter_embed(user, job.id)
