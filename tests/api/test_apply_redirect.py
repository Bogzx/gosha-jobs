"""DM Apply links: signed, recorded once per minute, never an open redirect."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from gosha import clicks
from gosha.models import ApplyClick, Job, Subscription, User, UserJob


async def _delivery(session, url="https://www.ejobs.ro/user/locuri-de-munca/dev/1", n=1):
    user = User(discord_user_id=7000 + n)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id, max_age_days=7)
    sub.keywords, sub.locations = ["python"], ["Cluj"]
    job = Job(url=url, title="Python developer", company="Acme", source="ejobs")
    session.add_all([sub, job])
    await session.flush()
    uj = UserJob(user_id=user.id, job_id=job.id, subscription_id=sub.id,
                 dm_status=UserJob.DM_SENT, dm_sent_at=datetime.now(timezone.utc))
    session.add(uj)
    await session.commit()
    return user, job, sub, uj


async def _clicks(session) -> list[ApplyClick]:
    return list((await session.execute(
        select(ApplyClick).execution_options(populate_existing=True)
    )).scalars())


@pytest.mark.asyncio
async def test_apply_link_redirects_to_the_posting_and_records_the_click(client, session):
    user, job, sub, uj = await _delivery(session)
    url = clicks.apply_url(uj.id, job.url)
    assert url.startswith("http://localhost:8000/api/v1/r/")

    resp = await client.get(url.removeprefix("http://localhost:8000"))
    assert resp.status_code == 302
    assert resp.headers["location"] == job.url
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"

    [click] = await _clicks(session)
    assert (click.user_id, click.job_id, click.subscription_id, click.user_job_id) == (
        user.id, job.id, sub.id, uj.id)


def test_no_ip_or_user_agent_is_stored():
    assert set(ApplyClick.__table__.c.keys()) == {
        "id", "user_id", "job_id", "user_job_id", "subscription_id",
        "clicked_at", "click_minute",
    }


@pytest.mark.asyncio
async def test_prefetchers_and_double_clicks_count_once_per_minute(client, session, patched_db):
    _, job, _, uj = await _delivery(session)
    path = clicks.apply_url(uj.id, job.url).removeprefix("http://localhost:8000")
    for _ in range(3):  # Discord unfurler, a mail scanner, the user
        assert (await client.get(path)).status_code == 302
    assert len(await _clicks(session)) == 1

    later = datetime.now(timezone.utc) + timedelta(seconds=clicks.DEDUP_SECONDS + 5)
    assert await clicks.record_click(uj.id, now=later) == job.url
    assert len(await _clicks(session)) == 2


@pytest.mark.asyncio
async def test_same_minute_race_is_absorbed_by_the_unique_key(session, patched_db, monkeypatch):
    """Two hits that both pass the 'recent click?' check before either commits."""
    _, job, _, uj = await _delivery(session)
    now = datetime.now(timezone.utc).replace(second=10)
    assert await clicks.record_click(uj.id, now=now) == job.url
    monkeypatch.setattr(clicks, "DEDUP_SECONDS", 0)  # the check sees nothing
    assert await clicks.record_click(uj.id, now=now + timedelta(seconds=1)) == job.url
    assert len(await _clicks(session)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [
    "", "1", "MQ", "MQ.forged-signature", "x" * 500, "../../etc/passwd",
])
async def test_forged_or_mangled_tokens_are_404(client, session, token):
    await _delivery(session)
    resp = await client.get(f"/api/v1/r/{token}")
    assert resp.status_code in (404, 405)  # "" hits /r/ itself
    assert await _clicks(session) == []


@pytest.mark.asyncio
async def test_ids_cannot_be_walked(client, session):
    """A token is bound to its id: signing id 1 says nothing about id 2."""
    _, _, _, uj = await _delivery(session, n=1)
    _, _, _, other = await _delivery(session, url="https://example.com/2", n=2)
    sig = clicks.token_for(uj.id).rsplit(".", 1)[1]
    forged = clicks.token_for(other.id).rsplit(".", 1)[0] + "." + sig
    assert (await client.get(f"/api/v1/r/{forged}")).status_code == 404


@pytest.mark.asyncio
async def test_token_signed_with_another_secret_is_rejected(client, session, monkeypatch):
    _, _, _, uj = await _delivery(session)
    monkeypatch.setenv("SESSION_SECRET", "some-other-secret")
    token = clicks.token_for(uj.id)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    assert (await client.get(f"/api/v1/r/{token}")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_url", ["javascript:alert(1)", "data:text/html,x", "//evil.test"])
async def test_never_redirects_to_a_non_web_url(client, session, bad_url):
    _, _, _, uj = await _delivery(session, url=bad_url)
    resp = await client.get(f"/api/v1/r/{clicks.token_for(uj.id)}")
    assert resp.status_code == 404
    assert await _clicks(session) == []


@pytest.mark.asyncio
async def test_deleted_delivery_is_404(client, session):
    _, _, _, uj = await _delivery(session)
    token = clicks.token_for(uj.id)
    await session.delete(uj)
    await session.commit()
    assert (await client.get(f"/api/v1/r/{token}")).status_code == 404


def test_apply_url_falls_back_to_the_posting(monkeypatch):
    """Tracking is optional; the Apply button is not."""
    posting = "https://example.com/job"
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("DISCORD_CLIENT_ID", "1")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "x")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert clicks.apply_url(5, posting) == posting  # no site to link to
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://gosha.test/")
    assert clicks.apply_url(5, posting).startswith("https://gosha.test/api/v1/r/")
    assert clicks.apply_url(None, posting) == posting
    assert clicks.apply_url(5, "javascript:x") == "javascript:x"  # views drops it
    assert clicks.apply_url(5, None) is None
    monkeypatch.delenv("SESSION_SECRET")
    assert clicks.apply_url(5, posting) == posting  # cannot sign


@pytest.mark.asyncio
async def test_account_deletion_takes_the_clicks_with_it(client, session, patched_db):
    from gosha.services.account import delete_account

    user, job, _, uj = await _delivery(session)
    await clicks.record_click(uj.id)
    await delete_account(user.id)
    assert (await session.execute(select(func.count(ApplyClick.id)))).scalar() == 0
