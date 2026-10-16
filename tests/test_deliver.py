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
    # The rest wait for the next cycle instead of being dropped for good
    # (production lost 8 good Cluj matches of one user's first batch).
    assert [statuses[j] for j in jobs] == ["pending", "pending", "pending", "sent", "sent"]
    assert len(calls) == 2
    assert texts and "**3** more matches" in texts[0]
    assert await pipeline.deliver_pending(_Bot()) == 2  # next cycle: the next best
    statuses = await _statuses(session)
    assert [statuses[j] for j in jobs] == ["pending", "sent", "sent", "sent", "sent"]


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


async def _matches(session, user, sub, specs):
    """Pending deliveries for (title, company, score, posted days ago)."""
    ids = []
    for i, (title, company, score, days) in enumerate(specs):
        job = Job(url=f"https://d.test/m/{sub.id}/{i}", title=title, company=company,
                  location="Cluj-Napoca", source="ejobs",
                  posted_at=datetime.now(timezone.utc) - timedelta(days=days))
        session.add(job)
        await session.flush()
        session.add(UserJob(user_id=user.id, job_id=job.id, subscription_id=sub.id,
                            relevance_score=score, dm_status=UserJob.DM_PENDING))
        ids.append(job.id)
    await session.commit()
    return ids


@pytest.mark.asyncio
async def test_a_search_sends_at_most_five_a_cycle_and_defers_the_rest(
    patched_db, session, monkeypatch,
):
    user, sub, _jobs, rows = await _setup(session, n_jobs=0)
    ids = await _matches(session, user, sub, [
        (f"Role {i}", f"Co{i}", 0.40 + i / 100, 0) for i in range(8)
    ])
    calls = _fake_dm(monkeypatch, [])
    monkeypatch.setattr(pipeline, "_send_text", lambda *a: asyncio.sleep(0))

    assert await pipeline.deliver_pending(_Bot()) == 5
    statuses = await _statuses(session)
    assert [statuses[j] for j in ids] == ["pending"] * 3 + ["sent"] * 5  # best five
    assert await pipeline.deliver_pending(_Bot()) == 3
    assert len(calls) == 8


@pytest.mark.asyncio
async def test_one_company_gets_at_most_three_dms_a_batch(patched_db, session, monkeypatch):
    user, sub, _jobs, _rows = await _setup(session, n_jobs=0)
    ids = await _matches(session, user, sub, [
        (f"Role {i}", "Endava", 0.50 + i / 100, 0) for i in range(4)
    ] + [("Other", "Acme", 0.45, 0)])
    _fake_dm(monkeypatch, [])
    monkeypatch.setattr(pipeline, "_send_text", lambda *a: asyncio.sleep(0))

    assert await pipeline.deliver_pending(_Bot()) == 4
    statuses = await _statuses(session)
    assert statuses[ids[0]] == "pending"  # Endava's fourth, the weakest of its four
    assert statuses[ids[4]] == "sent"


@pytest.mark.asyncio
async def test_a_deferred_match_is_rechecked_when_its_turn_comes(
    patched_db, session, monkeypatch,
):
    """Deferred, it may have gone stale; the search window still holds."""
    user, sub, _jobs, _rows = await _setup(session, n_jobs=0)
    sub.max_age_days = 7
    await session.commit()
    ids = await _matches(session, user, sub, [("Fresh", "A", 0.5, 1), ("Stale", "B", 0.6, 9)])
    calls = _fake_dm(monkeypatch, [])
    assert await pipeline.deliver_pending(_Bot()) == 1
    statuses = await _statuses(session)
    assert statuses[ids[0]] == "sent" and statuses[ids[1]] == "skipped"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unscored_matches_go_after_scored_ones(patched_db, session, monkeypatch):
    user, sub, _jobs, _rows = await _setup(session, n_jobs=0)
    monkeypatch.setattr(pipeline, "MAX_DMS_PER_SEARCH", 1)
    ids = await _matches(session, user, sub, [("Regex", "A", None, 0), ("Scored", "B", 0.3, 0)])
    _fake_dm(monkeypatch, [])
    monkeypatch.setattr(pipeline, "_send_text", lambda *a: asyncio.sleep(0))
    assert await pipeline.deliver_pending(_Bot()) == 1
    statuses = await _statuses(session)
    assert statuses[ids[1]] == "sent" and statuses[ids[0]] == "pending"


# ── One posting, one DM: copies from two boards (pre-release review) ──

ASCOM = [
    ("InternSoftware Engineer", "Ascom UMS", "Cluj-Napoca, Cluj, Romania", "linkedin"),
    ("InternSoftware Engineer", "ascom", "Cluj-Napoca, România", "bestjobs"),
]
MARSH = [
    ("Junior Full-Stack Engineer, Generative AI @ Marsh Tech", "MMC Corporate",
     "Cluj-Napoca, România", "bestjobs"),
    ("Junior Full-Stack Engineer, Generative AI @ Marsh Tech", "Marsh",
     "Cluj-Napoca, CJ, Romania", "indeed"),
]


async def _copies(session, user, sub, specs, scores, status=UserJob.DM_PENDING):
    ids = []
    for (title, company, location, source), score in zip(specs, scores):
        job = Job(url=f"https://d.test/copy/{source}/{len(title)}/{score}", title=title,
                  company=company, location=location, source=source)
        session.add(job)
        await session.flush()
        session.add(UserJob(user_id=user.id, job_id=job.id, subscription_id=sub.id,
                            relevance_score=score, dm_status=status,
                            dm_sent_at=datetime.now(timezone.utc) if status == UserJob.DM_SENT else None))
        ids.append(job.id)
    await session.commit()
    return ids


@pytest.mark.asyncio
async def test_two_board_copies_in_one_batch_are_one_dm_the_better_one(
    patched_db, session, monkeypatch,
):
    user, sub, _jobs, _rows = await _setup(session, n_jobs=0)
    ascom = await _copies(session, user, sub, ASCOM, [0.4472, 0.454])
    marsh = await _copies(session, user, sub, MARSH, [0.3845, 0.3794])
    calls = _fake_dm(monkeypatch, [])

    assert await pipeline.deliver_pending(_Bot()) == 2
    statuses = await _statuses(session)
    assert statuses[ascom[1]] == "sent" and statuses[ascom[0]] == "skipped"  # 0.454 wins
    assert statuses[marsh[0]] == "sent" and statuses[marsh[1]] == "skipped"  # 0.3845 wins
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("pair", [ASCOM, MARSH], ids=["ascom", "marsh"])
async def test_a_copy_of_a_posting_already_dmed_is_not_sent(
    patched_db, session, monkeypatch, pair,
):
    user, sub, _jobs, _rows = await _setup(session, n_jobs=0)
    await _copies(session, user, sub, pair[:1], [0.45], status=UserJob.DM_SENT)  # cycle 1
    (later,) = await _copies(session, user, sub, pair[1:], [0.50])  # cycle 2
    calls = _fake_dm(monkeypatch, [])

    assert await pipeline.deliver_pending(_Bot()) == 0
    assert (await _statuses(session))[later] == "skipped" and calls == []


@pytest.mark.asyncio
async def test_same_title_in_another_city_or_a_short_title_is_not_a_copy(
    patched_db, session, monkeypatch,
):
    user, sub, _jobs, _rows = await _setup(session, n_jobs=0)
    await _copies(session, user, sub, [
        # Another city and employer spelling: two postings. (Same employer in
        # two cities is one posting for gosha.dedup, which ignores cities.)
        ("Junior Full-Stack Engineer, Generative AI @ Marsh Tech", "MMC Corporate",
         "Iași, Romania", "bestjobs"),
        ("Junior Full-Stack Engineer, Generative AI @ Marsh Tech", "Marsh", "Cluj-Napoca", "indeed"),
        ("QA Engineer", "Acme", "Cluj-Napoca", "ejobs"),
        ("QA Engineer", "Beta", "Cluj-Napoca", "bestjobs"),
    ], [0.5, 0.49, 0.48, 0.47])
    _fake_dm(monkeypatch, [])
    assert await pipeline.deliver_pending(_Bot()) == 4
