"""Subscriptions HTTP adapter."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from gosha.api.deps import current_user
from gosha.api.schemas import (
    OkOut,
    RowId,
    SubscriptionIn,
    SubscriptionOut,
    SubscriptionPatchIn,
)
from gosha.models import Subscription, User
from gosha.services import subscriptions as service

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


def subscription_to_out(sub: Subscription) -> SubscriptionOut:
    return SubscriptionOut(
        id=sub.id,
        name=sub.name,
        keywords=sub.keywords,
        locations=sub.locations,
        excluded_keywords=sub.excluded_keywords,
        company_blacklist=sub.company_blacklist,
        experience_levels=sub.experience_levels,
        remote_ok=sub.remote_ok,
        salary_min=sub.salary_min,
        max_age_days=sub.max_age_days,
        is_active=sub.is_active,
        notify_discord=sub.notify_discord,
        created_at=sub.created_at,
    )


@router.get("")
async def list_subscriptions(user: User = Depends(current_user)) -> dict:
    subs = await service.list_subscriptions(user.id)
    return {"items": [subscription_to_out(s).model_dump(mode="json") for s in subs]}


@router.post("", response_model=SubscriptionOut)
async def create_subscription(
    body: SubscriptionIn, user: User = Depends(current_user),
) -> SubscriptionOut:
    sub = await service.create_subscription(
        user,
        service.SubscriptionInput(
            keywords=body.keywords,
            locations=body.locations,
            name=body.name,
            excluded_keywords=body.excluded_keywords,
            company_blacklist=body.company_blacklist,
            experience_levels=body.experience_levels,
            remote_ok=body.remote_ok,
            salary_min=body.salary_min,
            max_age_days=body.max_age_days,
            notify_discord=body.notify_discord,
        ),
    )
    return subscription_to_out(sub)


@router.patch("/{subscription_id}", response_model=SubscriptionOut)
async def update_subscription(
    subscription_id: RowId,
    body: SubscriptionPatchIn,
    user: User = Depends(current_user),
) -> SubscriptionOut:
    sub = await service.update_subscription(
        user, subscription_id, body.model_dump(exclude_unset=True),
    )
    return subscription_to_out(sub)


@router.post("/{subscription_id}/pause", response_model=SubscriptionOut)
async def pause_subscription(
    subscription_id: RowId, user: User = Depends(current_user),
) -> SubscriptionOut:
    return subscription_to_out(
        await service.set_subscription_active(user.id, subscription_id, False)
    )


@router.post("/{subscription_id}/resume", response_model=SubscriptionOut)
async def resume_subscription(
    subscription_id: RowId, user: User = Depends(current_user),
) -> SubscriptionOut:
    return subscription_to_out(
        await service.set_subscription_active(user.id, subscription_id, True)
    )


@router.delete("/{subscription_id}", response_model=OkOut)
async def delete_subscription(
    subscription_id: RowId, user: User = Depends(current_user),
) -> OkOut:
    await service.delete_subscription(user.id, subscription_id)
    return OkOut()
