"""The deliver stage: a match is delivered when Discord accepted the DM.

Delivery used to send "the rows recorded since this cycle started", which
counted a match as delivered before any DM went out: one Discord 503 lost
it for good, a website 👎 recorded mid-cycle was DM'd, and two overlapping
cycles DM'd everything twice.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import gosha.pipeline as pipeline
from gosha.models import Job, Subscription, User, UserJob


class _Channel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content):
        self.sent.append(content)


class _Bot:
    def __init__(self) -> None:
        self.alert_channel_id = 7
        self.channel = _Channel()

    def get_channel(self, _id):
        return self.channel


async def _setup(session, n_jobs: int = 1, discord_id: int = 1001, **sub_kwargs):
    user = User(discord_user_id=discord_id)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id, **sub_kwargs)
    sub.keywords = ["python developer"]
    sub.locations = ["cluj"]
    session.add(sub)
    jobs = [
        Job(url=f"https://d.test/{discord_id}/{i}", title=f"Python Developer {i}",
            company=f"Co{i}", location="Cluj-Napoca, Romania", source="ejobs")
        for i in range(n_jobs)
    ]
    session.add_all(jobs)
    await session.flush()
    rows = [
        UserJob(user_id=user.id, job_id=job.id, subscription_id=sub.id,
                relevance_score=0.5 + i / 100, dm_status=UserJob.DM_PENDING)
        for i, job in enumerate(jobs)
    ]
    session.add_all(rows)
    await session.commit()
    # Plain ids: _statuses() expires the session, and touching an expired
    # ORM attribute in an async test would need IO.
    return user, sub, [job.id for job in jobs], rows


async def _statuses(session) -> dict[int, str | None]:
    session.expire_all()
    rows = (await session.execute(select(UserJob))).scalars().all()
    return {uj.job_id: uj.dm_status for uj in rows}


def _fake_dm(monkeypatch, outcomes: list[str]) -> list[int]:
    calls: list[int] = []

    async def fake(bot, discord_id, embed, view=None):
        calls.append(discord_id)
        return outcomes.pop(0) if outcomes else pipeline.DM_SENT

    monkeypatch.setattr(pipeline, "_dm_user", fake)
    return calls


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried_next_cycle(patched_db, session, monkeypatch):
    _user, _sub, jobs, _rows = await _setup(session, n_jobs=2)
    calls = _fake_dm(monkeypatch, [pipeline.DM_RETRY, pipeline.DM_SENT])

    assert await pipeline.deliver_pending(_Bot()) == 1
    statuses = await _statuses(session)
    # Highest score first: job 1 (0.51) was attempted first and failed.
    assert statuses == {jobs[1]: "pending", jobs[0]: "sent"}

    assert await pipeline.deliver_pending(_Bot()) == 1  # the retry
    assert set((await _statuses(session)).values()) == {"sent"}
    assert len(calls) == 3
    assert await pipeline.deliver_pending(_Bot()) == 0  # nothing re-sent


@pytest.mark.asyncio
async def test_closed_dms_are_given_up_and_reported(patched_db, session, monkeypatch):
    from gosha.alerts import reset_alerter

    reset_alerter()  # the alerter is process-wide; start without known keys
    await _setup(session, n_jobs=3, discord_id=4242)
    calls = _fake_dm(monkeypatch, [pipeline.DM_CLOSED])
    bot = _Bot()
    owner_dms: list[str] = []

    class _Sink:
        async def send(self, text):
            owner_dms.append(text)

    monkeypatch.setattr("gosha.alerts.sink_for_bot", lambda _bot: _Sink())

    assert await pipeline.deliver_pending(bot) == 0
    assert len(calls) == 1  # no point trying the other two
    assert set((await _statuses(session)).values()) == {"failed"}
    # Owners are told by DM; nothing is posted to a channel, nobody mentioned.
    assert "4242" in owner_dms[0] and "<@" not in owner_dms[0]
    assert bot.channel.sent == []
    assert await pipeline.deliver_pending(bot) == 0
    assert len(calls) == 1  # not retried


@pytest.mark.asyncio
async def test_closed_dms_are_reported_once_not_every_cycle(patched_db, session, monkeypatch):
    from gosha import alerts

    alerts.reset_alerter()
    user, sub, _jobs, _rows = await _setup(session, n_jobs=1, discord_id=4242)
    user_id, sub_id = user.id, sub.id
    owner_dms: list[str] = []

    class _Sink:
        async def send(self, text):
            owner_dms.append(text)

    monkeypatch.setattr("gosha.alerts.sink_for_bot", lambda _bot: _Sink())

    async def new_match(n):
        job = Job(url=f"https://d.test/later/{n}", title=f"Later {n}", company="L",
                  source="ejobs")
        session.add(job)
        await session.flush()
        session.add(UserJob(user_id=user_id, job_id=job.id, subscription_id=sub_id,
                            relevance_score=0.5, dm_status=UserJob.DM_PENDING))
        await session.commit()

    _fake_dm(monkeypatch, [pipeline.DM_CLOSED, pipeline.DM_CLOSED])
    await pipeline.deliver_pending(_Bot())
    await new_match(1)
    await pipeline.deliver_pending(_Bot())  # still closed: no second notice
    assert len(owner_dms) == 1 and "4242" in owner_dms[0]

    await new_match(2)
    assert await pipeline.deliver_pending(_Bot()) == 1  # DMs open again
    assert len(owner_dms) == 2 and "4242" in owner_dms[1]
    await pipeline.deliver_pending(_Bot())  # nothing to deliver: silent
    assert len(owner_dms) == 2
    alerts.reset_alerter()


@pytest.mark.asyncio
async def test_rated_and_website_rows_are_never_dmed(patched_db, session, monkeypatch):
    user, _sub, jobs, rows = await _setup(session, n_jobs=2)
    rows[0].feedback = "not_relevant"  # a website 👎 mid-cycle
    extra = Job(url="https://d.test/web", title="Applied on the web", company="W",
                source="ejobs")
    session.add(extra)
    await session.flush()
    extra_id = extra.id
    session.add(UserJob(user_id=user.id, job_id=extra_id))  # apply-click, no search
    await session.commit()
    calls = _fake_dm(monkeypatch, [])

    assert await pipeline.deliver_pending(_Bot()) == 1
    assert len(calls) == 1
    statuses = await _statuses(session)
    assert statuses[jobs[1]] == "sent"
    assert statuses[jobs[0]] == "pending" and statuses[extra_id] is None


@pytest.mark.asyncio
async def test_old_matches_age_out_instead_of_being_sent(patched_db, session, monkeypatch):
    _user, _sub, _jobs, rows = await _setup(session)
    rows[0].delivered_at = datetime.now(timezone.utc) - pipeline.DM_RETRY_WINDOW - timedelta(hours=1)
    await session.commit()
    calls = _fake_dm(monkeypatch, [])
    assert await pipeline.deliver_pending(_Bot()) == 0
    assert calls == []


@pytest.mark.asyncio
async def test_dms_per_user_are_capped_best_first(patched_db, session, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_DMS_PER_USER", 2)
    _user, _sub, jobs, _rows = await _setup(session, n_jobs=5)
    calls = _fake_dm(monkeypatch, [])
    texts: list[str] = []

    async def fake_text(bot, discord_id, text):
        texts.append(text)

    monkeypatch.setattr(pipeline, "_send_text", fake_text)

    assert await pipeline.deliver_pending(_Bot()) == 2
    statuses = await _statuses(session)
    assert [statuses[j] for j in jobs] == ["skipped", "skipped", "skipped", "sent", "sent"]
    assert len(calls) == 2
    assert texts and "**3** more matches" in texts[0]


@pytest.mark.asyncio
async def test_a_dm_exception_never_ends_the_loop(patched_db, session, monkeypatch):
    """_dm_user turns anything (an aiohttp error, an OSError) into a retry."""

    class _User:
        async def create_dm(self):
            raise OSError("connection reset")

    class _B:
        def get_user(self, _id):
            return _User()

    assert await pipeline._dm_user(_B(), 1, embed=None) == pipeline.DM_RETRY


@pytest.mark.asyncio
async def test_overlapping_cycles_are_refused(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_cycle(*_args):
        started.set()
        await release.wait()
        return 0

    monkeypatch.setattr(pipeline, "_run_scrape_cycle", slow_cycle)
    first = asyncio.create_task(pipeline.run_scrape_cycle(None, None, 0))
    await started.wait()
    assert pipeline.cycle_running()
    with pytest.raises(pipeline.ScrapeCycleBusy):
        await pipeline.run_scrape_cycle(None, None, 0)
    release.set()
    assert await first == 0
    assert not pipeline.cycle_running()


# ── match stage ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_delivery_per_duplicate_group_across_cycles(patched_db, session):
    user = User(discord_user_id=2002)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id)
    sub.keywords = ["python developer"]
    sub.locations = ["cluj"]
    session.add(sub)
    linkedin = Job(url="https://li.test/1", title="Python Developer", company="Acme",
                   location="Cluj-Napoca, Romania", source="linkedin")
    session.add(linkedin)
    await session.flush()
    linkedin.dedup_group_id = linkedin.id
    session.add(UserJob(user_id=user.id, job_id=linkedin.id, subscription_id=sub.id,
                        dm_status="sent"))  # DM'd last week
    indeed = Job(url="https://in.test/1", title="Python Developer", company="Acme",
                 location="Cluj-Napoca, Romania", source="indeed")
    session.add(indeed)
    await session.flush()
    indeed.dedup_group_id = linkedin.id  # the same posting on another board
    await session.commit()

    assert await pipeline.run_match_stage([indeed]) == 0


@pytest.mark.asyncio
async def test_a_job_two_searches_match_goes_to_the_discord_one(patched_db, session):
    user = User(discord_user_id=2003)
    session.add(user)
    await session.flush()
    quiet = Subscription(user_id=user.id, notify_discord=False)
    quiet.keywords, quiet.locations = ["python developer"], ["cluj"]
    session.add(quiet)
    await session.flush()  # the website-only search has the lower id
    loud = Subscription(user_id=user.id, notify_discord=True)
    loud.keywords, loud.locations = ["python developer"], ["cluj"]
    session.add(loud)
    job = Job(url="https://o.test/1", title="Python Developer", company="Acme",
              location="Cluj-Napoca, Romania", source="ejobs")
    session.add(job)
    await session.commit()

    assert await pipeline.run_match_stage([job]) == 1
    (uj,) = (await session.execute(select(UserJob))).scalars().all()
    assert uj.subscription_id == loud.id


@pytest.mark.asyncio
async def test_rows_without_pending_status_are_never_dmed(patched_db, session, monkeypatch):
    """Legacy rows, and rows an older release writes while a failed deploy
    is rolled back (it DMs at match time itself), have no dm_status."""
    _user, _sub, _jobs, rows = await _setup(session, n_jobs=2)
    for row in rows:
        row.dm_status = None
    await session.commit()
    calls = _fake_dm(monkeypatch, [])
    assert await pipeline.deliver_pending(_Bot()) == 0
    assert calls == []


@pytest.mark.asyncio
async def test_the_match_stage_marks_new_matches_pending(patched_db, session):
    user = User(discord_user_id=2004)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id)
    sub.keywords, sub.locations = ["python developer"], ["cluj"]
    session.add(sub)
    job = Job(url="https://p.test/1", title="Python Developer", company="Acme",
              location="Cluj-Napoca, Romania", source="ejobs")
    session.add(job)
    await session.commit()

    assert await pipeline.run_match_stage([job]) == 1
    (uj,) = (await session.execute(select(UserJob))).scalars().all()
    assert uj.dm_status == UserJob.DM_PENDING


@pytest.mark.asyncio
async def test_a_twin_is_not_new_after_the_delivered_copy_expires(patched_db, session):
    """Dedup only groups active postings: the twin of an expired, already
    delivered copy is ungrouped again, and must still not be re-sent."""
    user = User(discord_user_id=2005)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id)
    sub.keywords, sub.locations = ["python developer"], ["cluj"]
    session.add(sub)
    sent_copy = Job(url="https://ej.test/1", title="Python Developer", company="Acme SRL",
                    location="Cluj-Napoca, Romania", source="ejobs", is_active=False)
    session.add(sent_copy)
    await session.flush()
    session.add(UserJob(user_id=user.id, job_id=sent_copy.id, subscription_id=sub.id,
                        dm_status="sent"))
    twin = Job(url="https://li.test/1", title="Python Developer", company="Acme SRL",
               location="Cluj-Napoca, Romania", source="linkedin")  # dedup_group_id None
    session.add(twin)
    await session.commit()

    assert await pipeline.run_match_stage([twin]) == 0


@pytest.mark.asyncio
async def test_regex_matching_honours_the_board_level(patched_db, session):
    """Kept by the scrape stage as entry-level, so matched here too."""
    user = User(discord_user_id=2006)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id)
    sub.keywords, sub.locations = ["computer science internship"], ["cluj"]
    session.add(sub)
    job = Job(url="https://ej.test/qa", title="QA Engineer", company="Acme",
              location="Cluj-Napoca, Romania", source="ejobs", experience_level="entry")
    session.add(job)
    await session.commit()

    assert await pipeline.run_match_stage([job]) == 1
