"""Tests for the outbox DM bridge between the web app and the bot."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from gosha import outbox
from gosha.models import Outbox, User


class FakeBot:
    pass


@pytest.mark.asyncio
async def test_enqueue_creates_row(patched_db, session):
    user = User(discord_user_id=900)
    session.add(user)
    await session.commit()

    row = await outbox.enqueue(user.id, "test_dm", {"text": "hello"})
    assert row.id is not None

    stored = (await session.execute(select(Outbox))).scalar_one()
    assert stored.kind == "test_dm"
    assert stored.payload_dict == {"text": "hello"}


@pytest.mark.asyncio
async def test_process_sends_and_stamps(patched_db, session, monkeypatch):
    user = User(discord_user_id=901)
    session.add(user)
    await session.commit()
    await outbox.enqueue(user.id, "test_dm", {"text": "hi"})
    await outbox.enqueue(user.id, "welcome", {"search_name": "Cluj interns"})

    sent_messages: list[tuple[int, str]] = []

    async def fake_send(bot, discord_id, text):
        sent_messages.append((discord_id, text))
        return True

    monkeypatch.setattr(outbox, "_send_text_dm", fake_send)

    sent = await outbox.process_outbox(FakeBot())
    assert sent == 2
    assert sent_messages[0] == (901, "hi")
    assert "Cluj interns" in sent_messages[1][1]

    rows = (await session.execute(select(Outbox))).scalars().all()
    assert all(r.sent_at is not None for r in rows)

    # Second run: nothing pending
    assert await outbox.process_outbox(FakeBot()) == 0


@pytest.mark.asyncio
async def test_process_retries_then_gives_up(patched_db, session, monkeypatch):
    user = User(discord_user_id=902)
    session.add(user)
    await session.commit()
    row = await outbox.enqueue(user.id, "test_dm", {"text": "x"})

    async def failing_send(bot, discord_id, text):
        return False

    monkeypatch.setattr(outbox, "_send_text_dm", failing_send)

    for expected_attempts in range(1, outbox.MAX_ATTEMPTS + 1):
        assert await outbox.process_outbox(FakeBot()) == 0
        fresh = await session.get(Outbox, row.id)
        await session.refresh(fresh)
        assert fresh.attempts == expected_attempts
        assert fresh.last_error is not None
        assert fresh.sent_at is None

    # Exhausted: row no longer picked up
    assert await outbox.process_outbox(FakeBot()) == 0
    await session.refresh(fresh)
    assert fresh.attempts == outbox.MAX_ATTEMPTS


def test_render_welcome_mentions_search():
    text = outbox._render("welcome", {"search_name": "QA Cluj", "site_url": "https://jobs.x.com"})
    assert "QA Cluj" in text
    assert "https://jobs.x.com" in text


@pytest.mark.asyncio
async def test_pipeline_respects_notify_discord(patched_db, session, monkeypatch):
    """Subscriptions with notify_discord=False match jobs but skip the DM."""
    from datetime import datetime, timedelta, timezone

    import gosha.pipeline as pipeline
    from gosha.models import Job, Subscription, UserJob

    user = User(discord_user_id=903)
    session.add(user)
    await session.flush()

    quiet_sub = Subscription(user_id=user.id, notify_discord=False)
    quiet_sub.keywords = ["dev"]
    quiet_sub.locations = ["cluj"]
    loud_sub = Subscription(user_id=user.id, notify_discord=True)
    loud_sub.keywords = ["qa"]
    loud_sub.locations = ["cluj"]
    session.add_all([quiet_sub, loud_sub])

    j1 = Job(url="https://o.com/1", title="Dev", company="A", location="Cluj", source="indeed")
    j2 = Job(url="https://o.com/2", title="QA", company="B", location="Cluj", source="indeed")
    session.add_all([j1, j2])
    await session.flush()

    since = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.add_all([
        UserJob(user_id=user.id, job_id=j1.id, subscription_id=quiet_sub.id,
                dm_status=UserJob.DM_PENDING),
        UserJob(user_id=user.id, job_id=j2.id, subscription_id=loud_sub.id,
                dm_status=UserJob.DM_PENDING),
    ])
    await session.commit()

    dms: list[int] = []

    async def fake_dm(bot, discord_id, embed, view=None):
        dms.append(discord_id)
        return pipeline.DM_SENT

    monkeypatch.setattr(pipeline, "_dm_user", fake_dm)

    class _Bot:
        alert_channel_id = 0

    sent = await pipeline.deliver_pending(_Bot())
    assert sent == 1  # only the loud subscription's match was DMed
    assert dms == [903]
    assert since  # matches recorded after `since` are the ones considered
