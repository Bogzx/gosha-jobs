"""GDPR endpoints: export everything, erase everything."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest
from sqlalchemy import select

import gosha.cover_letter as cl_mod
from gosha import embeddings
from gosha.events import Event
from gosha.models import (
    Application,
    ApplyClick,
    CoverLetter,
    Job,
    SearchNudge,
    Subscription,
    User,
    UserJob,
)


@pytest.fixture(autouse=True)
def fake_encoder(monkeypatch):
    monkeypatch.setattr(
        embeddings, "encode_texts",
        lambda texts: np.zeros(
            (len(texts), embeddings.EMBEDDING_DIM), dtype=np.float32
        ) + 0.5,
    )


async def _seed_full_account(client, session, user, cookies) -> Job:
    """Give the user one of everything the export/erase paths must cover."""
    await client.put(
        "/api/v1/cv",
        files={"file": ("cv.txt", b"Python, Kubernetes, Go", "text/plain")},
        data={"consent": "true"},
        cookies=cookies,
    )

    job = Job(url="https://j.com/1", title="Dev", company="Acme", source="ejobs")
    session.add(job)
    await session.flush()

    sub = Subscription(user_id=user.id, max_age_days=7, name="internships")
    sub.keywords = ["python"]
    sub.locations = ["cluj"]
    session.add(sub)
    await session.flush()

    uj = UserJob(user_id=user.id, job_id=job.id, feedback="interested")
    session.add(uj)
    await session.flush()
    clicked = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    session.add(ApplyClick(user_id=user.id, job_id=job.id, user_job_id=uj.id,
                           subscription_id=sub.id, clicked_at=clicked,
                           click_minute=int(clicked.timestamp() // 60)))
    session.add(SearchNudge(subscription_id=sub.id, fingerprint="f" * 64,
                            watched_since=clicked, nudged_at=clicked, outcome="sent"))
    session.add(Application(user_id=user.id, job_id=job.id, status="applied"))
    session.add(CoverLetter(user_id=user.id, job_id=job.id, content="Dear Acme"))
    session.add(Event(event_type="web.signin", actor_id=user.id))
    await session.commit()
    return job


@pytest.mark.asyncio
async def test_export_returns_everything_we_hold(client, web_user, session):
    user, cookies = web_user
    await _seed_full_account(client, session, user, cookies)

    resp = await client.get("/api/v1/account/export", cookies=cookies)
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]

    body = resp.json()
    assert body["account"]["discord_user_id"] == str(user.discord_user_id)
    assert body["account"]["cv_consent_at"]  # recorded at upload, exported
    assert "Kubernetes" in body["cv_text"]
    assert [s["name"] for s in body["searches"]] == ["internships"]
    assert len(body["delivered_jobs"]) == 1
    assert body["delivered_jobs"][0]["feedback"] == "interested"
    assert len(body["applications"]) == 1
    assert body["cover_letters"][0]["content"] == "Dear Acme"
    assert [e["type"] for e in body["events"]] == ["web.signin"]
    [click] = body["apply_clicks"]
    assert click["job_id"] and click["subscription_id"] and click["clicked_at"].startswith("2026-10-01")
    [nudge] = body["search_nudges"]
    assert nudge["outcome"] == "sent" and nudge["nudged_at"].startswith("2026-10-01")


@pytest.mark.asyncio
async def test_export_requires_a_session(client):
    resp = await client.get("/api/v1/account/export")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_requires_explicit_confirmation(client, web_user, session):
    user, cookies = web_user
    resp = await client.delete("/api/v1/account", cookies=cookies)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "confirmation_required"

    still_there = await session.get(User, user.id)
    assert still_there is not None


@pytest.mark.asyncio
async def test_delete_account_cascades_through_everything(
    client, web_user, session, tmp_path,
):
    user, cookies = web_user
    user_id = user.id
    await _seed_full_account(client, session, user, cookies)

    cv_path = cl_mod.get_cv_path(user_id)
    assert cv_path.exists()

    resp = await client.delete(
        "/api/v1/account", params={"confirm": "DELETE"}, cookies=cookies
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    session.expire_all()
    assert await session.get(User, user_id) is None
    assert not cv_path.exists()

    for model, column in (
        (Subscription, Subscription.user_id),
        (UserJob, UserJob.user_id),
        (ApplyClick, ApplyClick.user_id),
        (Application, Application.user_id),
        (CoverLetter, CoverLetter.user_id),
        (Event, Event.actor_id),
    ):
        rows = (
            await session.execute(select(model).where(column == user_id))
        ).scalars().all()
        assert rows == [], f"{model.__name__} rows survived account deletion"
    assert (await session.execute(select(SearchNudge))).scalars().all() == []

    # Session cookie cleared, so the caller is logged out.
    assert "gosha_session=" in " ".join(resp.headers.get_list("set-cookie"))


@pytest.mark.asyncio
async def test_delete_account_leaves_public_job_rows_alone(
    client, web_user, session,
):
    """Job postings are public data, not the user's personal data."""
    user, cookies = web_user
    job = await _seed_full_account(client, session, user, cookies)
    job_id = job.id

    await client.delete(
        "/api/v1/account", params={"confirm": "DELETE"}, cookies=cookies
    )

    session.expire_all()
    assert await session.get(Job, job_id) is not None


@pytest.mark.asyncio
async def test_deleting_a_cv_also_deletes_its_cover_letters(
    client, web_user, session,
):
    """Letters are derived from the CV; "delete" has to include them.

    Their text, that is: this month's rows stay, empty, as the monthly
    quota record (see test_cv_api's quota-reset test).
    """
    user, cookies = web_user
    user_id = user.id
    await _seed_full_account(client, session, user, cookies)

    resp = await client.delete("/api/v1/cv", cookies=cookies)
    assert resp.status_code == 200

    session.expire_all()
    letters = (
        await session.execute(
            select(CoverLetter).where(CoverLetter.user_id == user_id)
        )
    ).scalars().all()
    assert [letter.content for letter in letters] == [""]
    assert (await client.get("/api/v1/cover-letters", cookies=cookies)).json() == {
        "items": []
    }
    export = (await client.get("/api/v1/account/export", cookies=cookies)).json()
    assert [c["content"] for c in export["cover_letters"]] == [None]


@pytest.mark.asyncio
async def test_privacy_notice_names_the_llm_processor(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    resp = await client.get("/api/v1/legal/privacy")
    assert resp.status_code == 200

    body = resp.json()
    processors = {p["name"] for p in body["third_party_processors"]}
    assert "OpenRouter" in processors
    assert "Discord" in processors
    assert "15,000 characters" in body["llm_disclosure"]
    assert "/api/v1/account/export" in body["your_rights"]["access_and_portability"]


@pytest.mark.asyncio
async def test_privacy_notice_follows_the_configured_provider(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    body = (await client.get("/api/v1/legal/privacy")).json()
    assert any(
        "Gemini" in p["name"] for p in body["third_party_processors"]
    )


@pytest.mark.asyncio
async def test_privacy_notice_names_deepseek_when_it_is_the_provider(client, monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    body = (await client.get("/api/v1/legal/privacy")).json()
    deepseek = [p for p in body["third_party_processors"] if p["name"] == "DeepSeek API"]
    assert deepseek, body["third_party_processors"]
    assert "China" in deepseek[0]["operator"]


@pytest.mark.asyncio
async def test_privacy_notice_names_the_edge_proxy_only_when_configured(client, monkeypatch):
    monkeypatch.delenv("PRIVACY_EDGE_PROXY", raising=False)
    body = (await client.get("/api/v1/legal/privacy")).json()
    assert "Cloudflare" not in {p["name"] for p in body["third_party_processors"]}
    assert "Cloudflare" not in body["limits"]["third_party_assets"]

    monkeypatch.setenv("PRIVACY_EDGE_PROXY", "Cloudflare")
    body = (await client.get("/api/v1/legal/privacy")).json()
    cloudflare = [p for p in body["third_party_processors"] if p["name"] == "Cloudflare"]
    assert cloudflare and "CV upload" in cloudflare[0]["receives"]
    assert "blocks it" in body["limits"]["third_party_assets"]


@pytest.mark.asyncio
async def test_privacy_contact_accepts_an_email(client, monkeypatch):
    monkeypatch.delenv("PRIVACY_CONTACT", raising=False)
    assert (await client.get("/api/v1/legal/privacy")).json()["contact"].startswith("https://")
    monkeypatch.setenv("PRIVACY_CONTACT", "privacy@example.org")
    body = (await client.get("/api/v1/legal/privacy")).json()
    assert body["contact"] == "mailto:privacy@example.org"


@pytest.mark.asyncio
async def test_privacy_retention_follows_the_settings(client, monkeypatch):
    monkeypatch.setenv("CV_RETENTION_MONTHS", "0")
    monkeypatch.delenv("EVENT_RETENTION_MONTHS", raising=False)
    body = (await client.get("/api/v1/legal/privacy")).json()
    assert body["retention"]["cv_text"] == "Until you delete it or delete your account."
    assert "indefinitely" in body["retention"]["events"]
    assert "turned off" in body["limits"]["cv_storage"]

    monkeypatch.setenv("CV_RETENTION_MONTHS", "12")
    monkeypatch.setenv("EVENT_RETENTION_MONTHS", "18")
    body = (await client.get("/api/v1/legal/privacy")).json()
    assert "12 months after your last activity" in body["retention"]["cv_text"]
    assert body["retention"]["events"].startswith("18 months")
    assert "60 seconds" in body["retention"]["rate_limit_counters"]


@pytest.mark.asyncio
async def test_privacy_notice_discloses_connection_data_and_rights(client):
    body = (await client.get("/api/v1/legal/privacy")).json()
    connection = [g for g in body["data_we_hold"] if g["category"] == "Connection data"]
    ip_item = next(i for i in connection[0]["items"] if "IP address" in i)
    # Every endpoint that keys a rate limit on the IP is named.
    for use in ("sign in", "demo", "page view"):
        assert use in ip_item
    assert "ANSPDCP" in body["your_rights"]["complaint"]
    assert "timestamp" in body["your_rights"]["consent"]


@pytest.mark.asyncio
async def test_privacy_notice_discloses_dm_clicks_nudges_and_owner_report(client, monkeypatch):
    monkeypatch.delenv("EMPTY_SEARCH_NUDGE_ENABLED", raising=False)
    monkeypatch.delenv("WEEKLY_CLICK_REPORT_ENABLED", raising=False)
    body = (await client.get("/api/v1/legal/privacy")).json()
    [activity] = [g for g in body["data_we_hold"] if g["category"] == "Activity"]
    click = next(i for i in activity["items"] if "Apply buttons" in i)
    assert "no IP address" in click
    nudge = next(i for i in activity["items"] if "found nothing" in i)
    assert "once a month" in nudge and "currently off" in nudge
    # Weekly owner report: on by default, aggregate, no names.
    assert "Once a week the operator" in activity["why"]
    assert "no names or Discord ids" in activity["why"]
    assert "Apply clicks" in body["your_rights"]["note"]
    assert set(body["retention"]) >= {"apply_clicks", "search_nudges"}

    monkeypatch.setenv("EMPTY_SEARCH_NUDGE_ENABLED", "true")
    monkeypatch.setenv("WEEKLY_CLICK_REPORT_ENABLED", "false")
    body = (await client.get("/api/v1/legal/privacy")).json()
    [activity] = [g for g in body["data_we_hold"] if g["category"] == "Activity"]
    assert any("currently on" in i for i in activity["items"])
    assert "operator" not in activity["why"]


@pytest.mark.asyncio
async def test_delete_account_removes_clicks_and_nudges_itself(
    client, web_user, session,
):
    """Explicit deletes, not the FK cascades: SQLite only cascades with
    PRAGMA foreign_keys on, which the app does not set."""
    from gosha.services.account import delete_account

    user, cookies = web_user
    await _seed_full_account(client, session, user, cookies)
    removed = await delete_account(user.id)
    assert removed["apply_clicks"] == 1
    assert removed["search_nudges"] == 1


@pytest.mark.asyncio
async def test_export_lists_only_this_users_clicks_and_nudges(
    client, web_user, session,
):
    user, cookies = web_user
    await _seed_full_account(client, session, user, cookies)
    other = User(discord_user_id=999_000)
    session.add(other)
    await session.flush()
    sub = Subscription(user_id=other.id, max_age_days=7)
    sub.keywords, sub.locations = ["java"], ["iasi"]
    job = Job(url="https://j.com/2", title="Dev", company="B", source="ejobs")
    session.add_all([sub, job])
    await session.flush()
    now = datetime.now(timezone.utc)
    session.add(ApplyClick(user_id=other.id, job_id=job.id, subscription_id=sub.id,
                           clicked_at=now, click_minute=int(now.timestamp() // 60)))
    session.add(SearchNudge(subscription_id=sub.id, fingerprint="0" * 64))
    await session.commit()

    body = (await client.get("/api/v1/account/export", cookies=cookies)).json()
    assert len(body["apply_clicks"]) == 1 and len(body["search_nudges"]) == 1
    assert body["apply_clicks"][0]["subscription_id"] != sub.id
