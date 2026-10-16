"""Saved-search (subscription) use cases with tier-limit enforcement."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select, update

from gosha.config import load_web_settings
from gosha.database import get_session
from gosha.domain.errors import NotFoundError, TierLimitError, ValidationError
from gosha.filters import (
    EXPERIENCE_LEVEL_CHOICES,
    canonical_level,
    clean_location,
    fold,
    place_suggestions,
)
from gosha.models import Subscription, User, UserJob

log = logging.getLogger(__name__)


@dataclass
class SubscriptionInput:
    keywords: list[str]
    locations: list[str]
    name: str | None = None
    excluded_keywords: list[str] = field(default_factory=list)
    company_blacklist: list[str] = field(default_factory=list)
    experience_levels: list[str] = field(default_factory=lambda: ["any"])
    remote_ok: bool = False
    salary_min: int | None = None
    max_age_days: int = 7
    notify_discord: bool = True


def _clean_list(values: list[str], cap: int = 50) -> list[str]:
    return [v.strip() for v in values if v and v.strip()][:cap]


def clean_levels(values: list[str], stored: list[str] | None = None) -> list[str]:
    """Experience levels as the matcher spells them; ["any"] when empty.

    An unknown level ("entry", say) used to be stored as typed and then
    matched no posting at all, silently. One already in `stored` (what the
    search has now: the bot's /edit used to save levels as typed) is
    dropped instead of refused, since the website sends the stored levels
    back with every edit and the search could not be saved at all.
    """
    levels: list[str] = []
    for value in _clean_list(values):
        level = canonical_level(value)
        if level is None:
            if value in (stored or ()):
                log.info("Dropping stored experience level %r: no such level", value)
                continue
            raise ValidationError(
                f"Unknown experience level {value!r}: use one of "
                f"{', '.join(EXPERIENCE_LEVEL_CHOICES)}."
            )
        if level not in levels:
            levels.append(level)
    return levels or ["any"]


def clean_locations(values: list[str], stored: list[str] | None = None) -> list[str]:
    """Locations as the scrapers can look them up (filters.clean_location).

    "llocation:Cluj" is saved as "Cluj"; a short token naming no place
    ("kra") is refused, with the places it might be the start of. As for
    levels, one already in `stored` is dropped rather than refused.
    Duplicates ("iasi", "Iasi") are kept once.
    """
    locations: list[str] = []
    seen: set[str] = set()
    for value in _clean_list(values):
        location = clean_location(value)
        if location is None:
            if value in (stored or ()):
                log.info("Dropping stored location %r: names no place", value)
                continue
            hint = place_suggestions(value)
            raise ValidationError(
                f"Unknown location {value!r}"
                + (f" — did you mean {' or '.join(hint)}?" if hint else
                   ": type a city, a country, \"remote\" or \"europe\".")
            )
        key = fold(location)
        if key not in seen:
            seen.add(key)
            locations.append(location)
    return locations


def _check_tier_lists(user: User, keywords: list[str], locations: list[str]) -> None:
    limits = user.limits
    if len(keywords) > int(limits["max_keywords_per_sub"]):
        raise TierLimitError(
            f"Your plan allows {limits['max_keywords_per_sub']} keywords per search."
        )
    if len(locations) > int(limits["max_locations_per_sub"]):
        raise TierLimitError(
            f"Your plan allows {limits['max_locations_per_sub']} locations per search."
        )


async def list_subscriptions(user_id: int) -> list[Subscription]:
    async with get_session() as session:
        result = await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.created_at.desc())
        )
        return list(result.scalars().all())


async def create_subscription(user: User, data: SubscriptionInput) -> Subscription:
    keywords = _clean_list(data.keywords)
    locations = clean_locations(data.locations)
    if not keywords or not locations:
        raise ValidationError("At least one keyword and one location are required.")
    _check_tier_lists(user, keywords, locations)
    levels = clean_levels(data.experience_levels)

    async with get_session() as session:
        # Count under a lock on the user row, or two concurrent creates both
        # see room and the plan's limit is exceeded. SQLite has no row locks
        # (and no concurrent writers to lock against).
        if session.bind.dialect.name == "postgresql":
            await session.execute(
                select(User.id).where(User.id == user.id).with_for_update()
            )
        count = (
            await session.execute(
                select(func.count(Subscription.id)).where(
                    Subscription.user_id == user.id
                )
            )
        ).scalar() or 0
        if count >= int(user.limits["max_subscriptions"]):
            raise TierLimitError(
                f"Your plan allows {user.limits['max_subscriptions']} searches — "
                "delete one first or upgrade."
            )

        sub = Subscription(
            user_id=user.id,
            name=data.name,
            remote_ok=data.remote_ok,
            salary_min=data.salary_min,
            max_age_days=data.max_age_days,
            notify_discord=data.notify_discord,
        )
        sub.keywords = keywords
        sub.locations = locations
        sub.excluded_keywords = _clean_list(data.excluded_keywords)
        sub.company_blacklist = _clean_list(data.company_blacklist)
        sub.experience_levels = levels
        session.add(sub)
        await session.commit()
        is_first = count == 0

    try:
        from gosha.events import emit_subscription_created
        await emit_subscription_created(user.id, sub.id, keywords)
    except Exception:
        pass

    # First search + reachable on Discord -> welcome DM so the user knows
    # alerts are live (the zero-setup funnel).
    if is_first and user.in_guild:
        try:
            from gosha.outbox import enqueue
            await enqueue(user.id, "welcome", {
                "search_name": data.name or ", ".join(keywords[:2]),
                "site_url": load_web_settings().public_base_url,
            })
        except Exception as exc:
            log.warning("Welcome DM enqueue failed: %s", exc)

    return sub


async def get_own_subscription(user_id: int, subscription_id: int) -> Subscription:
    async with get_session() as session:
        sub = (
            await session.execute(
                select(Subscription).where(
                    Subscription.id == subscription_id,
                    Subscription.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
    if sub is None:
        raise NotFoundError("Search not found.")
    return sub


async def update_subscription(
    user: User, subscription_id: int, patch: dict,
) -> Subscription:
    """Apply a partial update; `patch` keys mirror SubscriptionInput fields.

    A key that is absent leaves the field alone. For the nullable fields
    (name, salary_min) a None value clears them — the SPA sends null when
    the user empties the box. For the others None also means "unchanged".
    """
    sub = await get_own_subscription(user.id, subscription_id)

    keywords = (
        _clean_list(patch["keywords"]) if patch.get("keywords") is not None
        else sub.keywords
    )
    locations = (
        clean_locations(patch["locations"], stored=sub.locations)
        if patch.get("locations") is not None else sub.locations
    )
    if not keywords or not locations:
        raise ValidationError("At least one keyword and one location are required.")
    _check_tier_lists(user, keywords, locations)
    levels = (
        clean_levels(patch["experience_levels"], stored=sub.experience_levels)
        if patch.get("experience_levels") is not None else None
    )

    async with get_session() as session:
        sub = await session.merge(sub)
        sub.keywords = keywords
        sub.locations = locations
        if "name" in patch:
            sub.name = patch["name"]
        if patch.get("excluded_keywords") is not None:
            sub.excluded_keywords = _clean_list(patch["excluded_keywords"])
        if patch.get("company_blacklist") is not None:
            sub.company_blacklist = _clean_list(patch["company_blacklist"])
        if levels is not None:
            sub.experience_levels = levels
        if patch.get("remote_ok") is not None:
            sub.remote_ok = patch["remote_ok"]
        if "salary_min" in patch:
            sub.salary_min = patch["salary_min"]
        if patch.get("max_age_days") is not None:
            sub.max_age_days = patch["max_age_days"]
        if patch.get("notify_discord") is not None:
            sub.notify_discord = patch["notify_discord"]
        await session.commit()
    return sub


async def set_subscription_active(
    user_id: int, subscription_id: int, active: bool,
) -> Subscription:
    sub = await get_own_subscription(user_id, subscription_id)
    async with get_session() as session:
        sub = await session.merge(sub)
        sub.is_active = active
        await session.commit()
    return sub


async def delete_subscription(user_id: int, subscription_id: int) -> None:
    sub = await get_own_subscription(user_id, subscription_id)
    async with get_session() as session:
        # Delivery history outlives the search: user_jobs.subscription_id
        # references it (no ON DELETE action), so Postgres refused the
        # delete for any search that ever sent a DM.
        await session.execute(
            update(UserJob)
            .where(UserJob.subscription_id == subscription_id)
            .values(subscription_id=None)
        )
        sub = await session.merge(sub)
        await session.delete(sub)
        await session.commit()
    try:
        from gosha.events import emit_subscription_deleted
        await emit_subscription_deleted(user_id, subscription_id)
    except Exception:
        pass
