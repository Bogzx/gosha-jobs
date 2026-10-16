"""Browse filters, saved-search validation and dead-link verdicts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from gosha import deadlinks
from gosha.domain.errors import ValidationError
from gosha.models import Job, User
from gosha.services import jobs as jobs_service
from gosha.services import subscriptions as subs_service

NOW = datetime.now(timezone.utc)


async def _user(session, discord_id: int = 9001) -> User:
    user = User(discord_user_id=discord_id)
    session.add(user)
    await session.commit()
    return user


def _job(url: str, **kwargs) -> Job:
    return Job(
        url=url, title=kwargs.pop("title", "Java Developer"),
        company=kwargs.pop("company", "Endava"), source="ejobs",
        location=kwargs.pop("location", "Cluj-Napoca"),
        first_seen_at=kwargs.pop("first_seen_at", NOW), **kwargs,
    )


# ── Browse ────────────────────────────────────────────────────────────


async def test_a_location_filter_finds_the_copy_in_that_city(patched_db, session):
    """Cross-board groups ignore location: one employer's "Java Developer"
    in Cluj and in Iasi is one group, canonical in Cluj. Duplicates were
    hidden before the location filter ran, so "Iasi" found nothing."""
    user = await _user(session)
    cluj = _job("https://b.test/cluj")
    session.add(cluj)
    await session.flush()
    cluj.dedup_group_id = cluj.id
    iasi = _job("https://b.test/iasi", location="Iași", dedup_group_id=cluj.id)
    session.add(iasi)
    await session.commit()

    found, total = await jobs_service.list_jobs(
        user.id, jobs_service.JobFilters(locations=["iasi"]), 1, 50,
    )
    assert [job.url for job in found] == ["https://b.test/iasi"] and total == 1

    # Both cities asked for: still one card, the canonical one.
    found, total = await jobs_service.list_jobs(
        user.id, jobs_service.JobFilters(locations=["iasi", "cluj"]), 1, 50,
    )
    assert [job.url for job in found] == ["https://b.test/cluj"] and total == 1


async def test_a_group_whose_canonical_expired_is_listed_with_or_without_filters(
    patched_db, session,
):
    """Unfiltered browse kept canonical rows only, so a group whose
    canonical posting had expired vanished; a filtered browse showed its
    live copy."""
    user = await _user(session)
    cluj = _job("https://b.test/cluj", is_active=False)
    session.add(cluj)
    await session.flush()
    cluj.dedup_group_id = cluj.id
    session.add_all([
        _job("https://b.test/iasi", location="Iasi", dedup_group_id=cluj.id),
        _job("https://b.test/brasov", location="Brasov", dedup_group_id=cluj.id,
             first_seen_at=NOW - timedelta(hours=1)),
        _job("https://b.test/solo", title="Solo"),
    ])
    await session.commit()

    unfiltered = await jobs_service.list_jobs(user.id, jobs_service.JobFilters(), 1, 50)
    filtered = await jobs_service.list_jobs(
        user.id, jobs_service.JobFilters(experience=["junior"]), 1, 50,
    )
    for found, total in (unfiltered, filtered):
        assert sorted(job.url for job in found) == ["https://b.test/iasi", "https://b.test/solo"]
        assert total == 2


async def test_posted_within_reads_the_boards_date(patched_db, session):
    """first_seen_at alone called a month-old posting "posted today" the
    first time a scrape returned it."""
    user = await _user(session)
    session.add_all([
        _job("https://b.test/old", title="Old", posted_at=NOW - timedelta(days=25)),
        _job("https://b.test/new", title="New", posted_at=NOW - timedelta(days=1)),
        _job("https://b.test/undated", title="Undated"),
        _job("https://b.test/stale", title="Stale", first_seen_at=NOW - timedelta(days=9)),
    ])
    await session.commit()

    found, total = await jobs_service.list_jobs(
        user.id, jobs_service.JobFilters(posted_within_days=3), 1, 50,
    )
    assert {job.title for job in found} == {"New", "Undated"} and total == 2


async def test_junior_browse_shows_unmarked_titles_but_not_senior_ones(patched_db, session):
    user = await _user(session)
    session.add_all([
        _job("https://b.test/qa", title="QA Engineer", company="A"),
        _job("https://b.test/sr", title="Senior QA Engineer", company="B"),
        _job("https://b.test/mid", title="QA Engineer", company="C", experience_level="mid"),
    ])
    await session.commit()

    found, _total = await jobs_service.list_jobs(
        user.id, jobs_service.JobFilters(experience=["junior"]), 1, 50,
    )
    assert [job.url for job in found] == ["https://b.test/qa"]


# ── Saved searches ────────────────────────────────────────────────────


async def test_experience_levels_are_stored_as_the_matcher_spells_them(patched_db, session):
    user = await _user(session)
    sub = await subs_service.create_subscription(user, subs_service.SubscriptionInput(
        keywords=["python"], locations=["cluj"], experience_levels=["Entry", "Internship"],
    ))
    assert sub.experience_levels == ["junior", "intern"]

    # Unknown levels used to be stored and then match nothing, silently.
    with pytest.raises(ValidationError):
        await subs_service.update_subscription(user, sub.id, {"experience_levels": ["expert"]})
    with pytest.raises(ValidationError):
        await subs_service.create_subscription(user, subs_service.SubscriptionInput(
            keywords=["python"], locations=["cluj"], experience_levels=["guru"],
        ))


# ── Dead links ────────────────────────────────────────────────────────


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_head_404_is_confirmed_with_a_get():
    """Some servers answer HEAD 404 for a page GET serves: the posting was
    expired for everyone on the strength of a HEAD."""
    def handler(request):
        return httpx.Response(404 if request.method == "HEAD" else 200)

    async with _client(handler) as client:
        assert await deadlinks._check_url_status(client, "https://board.test/job/1") == 200


async def test_a_real_404_is_still_dead():
    async with _client(lambda request: httpx.Response(404)) as client:
        assert await deadlinks._check_url_status(client, "https://board.test/job/1") == 404


async def test_hipo_notice_page_counts_as_gone():
    """Hipo serves filled jobs with 200 and a notice (measured on the eval
    set's postings, 2026-10-16), so none of them ever expired."""
    def handler(request):
        body = (
            "<h2 class='title'>Acest job nu mai este activ!</h2>"
            if "271867" in str(request.url) else "<h1>DevOps Engineer</h1>"
        )
        return httpx.Response(200, text=body)

    base = "https://www.hipo.ro/locuri-de-munca/locuri_de_munca"
    async with _client(handler) as client:
        assert await deadlinks._check_url_status(client, f"{base}/271867/X/DevOps") == 410
        assert await deadlinks._check_url_status(client, f"{base}/272274/X/Analist") == 200


async def test_a_bot_wall_is_inconclusive_not_dead():
    async with _client(lambda request: httpx.Response(403)) as client:
        assert await deadlinks._check_url_status(client, "https://board.test/job/1") == 403
    assert 403 not in deadlinks.DEAD_STATUSES


async def test_a_malformed_url_does_not_end_the_run():
    async with _client(lambda request: httpx.Response(200)) as client:
        assert await deadlinks._check_url_status(client, "not a url") is None
