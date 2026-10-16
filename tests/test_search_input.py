"""What a search is saved with is what the matcher and the scrapers can use.

Found in production (read-only): active searches with locations
["iasi", "kra", "mun"] and ["llocation:Cluj"] (a slash-command option name
pasted into the value). The first two tokens match no place, the last
matches nothing at all, and each costs failing Glassdoor requests every
cycle. A level the bot's /edit stored as typed ("fresher") then made the
search unsaveable from the website, which sends the stored levels back.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import select

import gosha.pipeline as pipeline
from gosha.bot import SubscriptionCog
from gosha.domain.errors import ValidationError
from gosha.filters import clean_location
from gosha.models import Subscription, User
from gosha.services import subscriptions as subs_service


@pytest.mark.parametrize("typed, cleaned", [
    ("llocation:Cluj", "Cluj"),
    ("location: Iasi", "Iasi"),
    ("Location=Cluj-Napoca", "Cluj-Napoca"),
    ("iasi", "iasi"),
    ("Cluj-Napoca, Romania", "Cluj-Napoca, Romania"),
    # Short but known.
    ("eu", "eu"),
    ("ro", "ro"),
    ("UK", "UK"),
    # Unknown, but long enough to be a town the alias table lacks.
    ("Mioveni", "Mioveni"),
    # Unknown and short: a fragment, nothing a board can look up.
    ("kra", None),
    ("mun", None),
    ("x", None),
    ("location:", None),
])
def test_clean_location(typed, cleaned):
    assert clean_location(typed) == cleaned


async def _user(session, discord_id: int = 9101) -> User:
    user = User(discord_user_id=discord_id)
    session.add(user)
    await session.commit()
    return user


async def test_locations_are_cleaned_when_a_search_is_saved(patched_db, session):
    user = await _user(session)
    sub = await subs_service.create_subscription(user, subs_service.SubscriptionInput(
        keywords=["python"], locations=["llocation:Cluj", "iasi", "Iasi"],
    ))
    assert sub.locations == ["Cluj", "iasi"]

    with pytest.raises(ValidationError, match="Krakow"):  # a suggestion
        await subs_service.create_subscription(user, subs_service.SubscriptionInput(
            keywords=["python"], locations=["iasi", "kra"],
        ))
    with pytest.raises(ValidationError):
        await subs_service.update_subscription(user, sub.id, {"locations": ["mun"]})


async def _legacy_search(session, user: User, **fields) -> int:
    """A row as the bot used to store it, bypassing the service."""
    sub = Subscription(user_id=user.id)
    sub.keywords = ["python"]
    sub.locations = fields.get("locations", ["iasi"])
    sub.experience_levels = fields.get("experience_levels", ["any"])
    session.add(sub)
    await session.commit()
    return sub.id


async def test_a_legacy_search_stays_saveable_from_the_website(patched_db, session):
    user = await _user(session)
    sub_id = await _legacy_search(
        session, user, locations=["iasi", "kra", "mun"], experience_levels=["fresher", "junior"],
    )
    # The SPA sends the stored values back with the user's edit.
    sub = await subs_service.update_subscription(user, sub_id, {
        "locations": ["iasi", "kra", "mun"],
        "experience_levels": ["fresher", "junior", "Internship"],
        "max_age_days": 3,
    })
    assert sub.locations == ["iasi"]
    assert sub.experience_levels == ["junior", "intern"]
    assert sub.max_age_days == 3
    # A new unknown value is still refused.
    with pytest.raises(ValidationError):
        await subs_service.update_subscription(user, sub_id, {"experience_levels": ["guru"]})


def _interaction(discord_id: int, sent: list[str]):
    async def send_message(content: str = "", **kwargs) -> None:
        sent.append(content or getattr(kwargs.get("embed"), "description", "") or "")

    return SimpleNamespace(
        user=SimpleNamespace(id=discord_id),
        response=SimpleNamespace(send_message=send_message, is_done=lambda: True),
    )


async def _stored(session, discord_id: int) -> list[Subscription]:
    session.expire_all()
    return list((await session.execute(
        select(Subscription).join(User).where(User.discord_user_id == discord_id)
    )).scalars().all())


async def test_the_bot_stores_what_the_matcher_can_use(patched_db, session):
    cog = SubscriptionCog(SimpleNamespace(settings=None))  # type: ignore[arg-type]
    sent: list[str] = []

    await cog.subscribe.callback(
        cog, _interaction(9201, sent), keyword="python", location="llocation:Cluj, Iasi",
        experience="Entry, internship",
    )
    (sub,) = await _stored(session, 9201)
    assert sub.locations == ["Cluj", "Iasi"]
    assert sub.experience_levels == ["junior", "intern"]

    sent.clear()
    await cog.edit.callback(cog, _interaction(9201, sent), id=sub.id, experience="fresher")
    (sub,) = await _stored(session, 9201)
    assert sub.experience_levels == ["junior", "intern"]  # refused, unchanged
    assert "fresher" in sent[0]

    sent.clear()
    await cog.edit.callback(cog, _interaction(9201, sent), id=sub.id, location="iasi, kra")
    (sub,) = await _stored(session, 9201)
    assert sub.locations == ["Cluj", "Iasi"]
    assert "kra" in sent[0]

    sent.clear()
    await cog.subscribe.callback(cog, _interaction(9202, sent), keyword="python", location="mun")
    assert await _stored(session, 9202) == []
    assert "mun" in sent[0]


# ── At scrape time: rows saved before validation existed ──────────────


class _NoTunnels:
    def active_proxies(self):
        return []


async def test_unusable_location_tokens_never_reach_the_scrapers(
    patched_db, session, monkeypatch, caplog,
):
    user = await _user(session, 9301)
    await _legacy_search(session, user, locations=["iasi", "kra", "mun"])
    await _legacy_search(session, user, locations=["llocation:Cluj"])

    asked: list[str] = []

    async def jobspy(_tunnels, keyword, location, *_a, **_kw):
        asked.append(location)
        return pd.DataFrame()

    monkeypatch.setattr("gosha.scraper.scrape_jobs_raw", jobspy)
    monkeypatch.setattr("gosha.scrapers.registry.get_extra_scrapers", lambda: [])
    monkeypatch.setattr(pipeline, "_skipped_locations", set())
    from gosha.scrape_health import ScrapeHealth

    with caplog.at_level("WARNING", logger="gosha.pipeline"):
        await pipeline.run_scrape_stage(_NoTunnels(), ScrapeHealth())
        await pipeline.run_scrape_stage(_NoTunnels(), ScrapeHealth())

    assert sorted(asked) == ["cluj", "cluj", "iasi", "iasi"]
    skipped = [r for r in caplog.records if "kra" in r.getMessage()]
    assert len(skipped) == 1  # logged once, not every cycle


async def test_searches_sharing_a_place_are_scraped_once(patched_db, session, monkeypatch):
    """Two searches for "python" in Cluj with different windows or boards
    (or "cluj" / "Cluj-Napoca") sent the same JobSpy query twice a cycle."""
    user = await _user(session, 9401)
    for locations, days, boards in (
        (["cluj"], 7, ["linkedin"]), (["Cluj-Napoca"], 14, ["indeed"]), (["Cluj"], 7, ["linkedin"]),
    ):
        sub = Subscription(user_id=user.id, max_age_days=days)
        sub.keywords = ["Python"]
        sub.locations = locations
        sub.boards = boards
        session.add(sub)
    await session.commit()

    asked: list[tuple] = []

    async def jobspy(_tunnels, keyword, location, max_age_days, boards=None):
        asked.append((keyword, location, max_age_days, tuple(boards)))
        return pd.DataFrame()

    monkeypatch.setattr("gosha.scraper.scrape_jobs_raw", jobspy)
    monkeypatch.setattr("gosha.scrapers.registry.get_extra_scrapers", lambda: [])
    from gosha.scrape_health import ScrapeHealth

    await pipeline.run_scrape_stage(_NoTunnels(), ScrapeHealth())
    # The widest window and every board asked for; each search's own
    # window is still enforced at the match stage (is_fresh_enough).
    assert asked == [("python", "cluj", 14, ("indeed", "linkedin"))]
