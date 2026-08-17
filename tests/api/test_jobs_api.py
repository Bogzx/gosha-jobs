"""Tests for the jobs browsing API: list filters, detail, feedback, apply-click."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from gosha.models import Application, Job, UserJob


@pytest_asyncio.fixture
async def jobs_fixture(session):
    now = datetime.now(timezone.utc)
    jobs = [
        Job(
            url="https://j.com/cluj-python",
            title="Python Developer Intern",
            company="CodeCorp",
            location="Cluj-Napoca, Romania",
            description="Write Python. " * 30,
            source="indeed",
            salary_min=1000.0,
            salary_max=1500.0,
            salary_currency="EUR",
            salary_period="monthly",
            # Normalised at ingest in production (gosha/salary.py); the
            # filter compares these, not the raw EUR figures.
            salary_monthly_min_ron=5000.0,
            salary_monthly_max_ron=7500.0,
        ),
        Job(
            url="https://j.com/buch-senior",
            title="Senior Java Engineer",
            company="MegaBank",
            location="Bucharest, Romania",
            description="Lead the team.",
            source="linkedin",
            salary_min=4000.0,
            salary_max=5000.0,
            salary_currency="EUR",
            salary_period="monthly",
            salary_monthly_min_ron=20000.0,
            salary_monthly_max_ron=25000.0,
        ),
        Job(
            url="https://j.com/remote-qa",
            title="QA Intern",
            company="RemoteWorks",
            location="Remote",
            description="Test things remotely.",
            source="glassdoor",
        ),
        Job(
            url="https://j.com/old-inactive",
            title="Old Job",
            company="Gone",
            location="Cluj-Napoca, Romania",
            source="indeed",
            is_active=False,
        ),
    ]
    jobs[0].first_seen_at = now - timedelta(days=1)
    jobs[1].first_seen_at = now - timedelta(days=2)
    jobs[2].first_seen_at = now - timedelta(days=20)
    session.add_all(jobs)
    await session.commit()
    return jobs


@pytest.mark.asyncio
async def test_list_requires_auth(client, jobs_fixture):
    resp = await client.get("/api/v1/jobs")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_returns_active_newest_first(client, web_user, jobs_fixture):
    _user, cookies = web_user
    resp = await client.get("/api/v1/jobs", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3  # inactive job excluded
    titles = [item["title"] for item in body["items"]]
    assert titles == ["Python Developer Intern", "Senior Java Engineer", "QA Intern"]
    # List view truncates long descriptions
    assert len(body["items"][0]["description"]) <= 243


@pytest.mark.asyncio
async def test_list_filter_q(client, web_user, jobs_fixture):
    _user, cookies = web_user
    resp = await client.get("/api/v1/jobs", params={"q": "python"}, cookies=cookies)
    assert [i["title"] for i in resp.json()["items"]] == ["Python Developer Intern"]

    resp = await client.get("/api/v1/jobs", params={"q": "megabank"}, cookies=cookies)
    assert [i["company"] for i in resp.json()["items"]] == ["MegaBank"]


@pytest.mark.asyncio
async def test_list_filter_location_alias(client, web_user, jobs_fixture):
    _user, cookies = web_user
    resp = await client.get("/api/v1/jobs", params={"locations": "cluj"}, cookies=cookies)
    assert [i["title"] for i in resp.json()["items"]] == ["Python Developer Intern"]


@pytest.mark.asyncio
async def test_list_filter_location_with_remote(client, web_user, jobs_fixture):
    _user, cookies = web_user
    resp = await client.get(
        "/api/v1/jobs", params={"locations": "cluj", "remote": "true"}, cookies=cookies
    )
    titles = {i["title"] for i in resp.json()["items"]}
    assert titles == {"Python Developer Intern", "QA Intern"}


@pytest.mark.asyncio
async def test_list_filter_sources_and_salary(client, web_user, jobs_fixture):
    _user, cookies = web_user
    resp = await client.get(
        "/api/v1/jobs", params={"sources": "linkedin"}, cookies=cookies
    )
    assert [i["source"] for i in resp.json()["items"]] == ["linkedin"]

    resp = await client.get(
        "/api/v1/jobs", params={"salary_min": 10000}, cookies=cookies
    )
    titles = {i["title"] for i in resp.json()["items"]}
    # Compared as monthly RON: MegaBank (20-25k) clears 10k, the intern
    # (5-7.5k) does not, and QA has no salary data (benefit of the doubt).
    assert titles == {"Senior Java Engineer", "QA Intern"}


@pytest.mark.asyncio
async def test_list_filter_experience(client, web_user, jobs_fixture):
    _user, cookies = web_user
    resp = await client.get(
        "/api/v1/jobs", params={"experience": "intern"}, cookies=cookies
    )
    titles = {i["title"] for i in resp.json()["items"]}
    assert titles == {"Python Developer Intern", "QA Intern"}


@pytest.mark.asyncio
async def test_list_filter_posted_within(client, web_user, jobs_fixture):
    _user, cookies = web_user
    resp = await client.get(
        "/api/v1/jobs", params={"posted_within_days": 7}, cookies=cookies
    )
    titles = {i["title"] for i in resp.json()["items"]}
    assert titles == {"Python Developer Intern", "Senior Java Engineer"}


@pytest.mark.asyncio
async def test_list_pagination(client, web_user, jobs_fixture):
    _user, cookies = web_user
    resp = await client.get(
        "/api/v1/jobs", params={"page": 2, "per_page": 2}, cookies=cookies
    )
    body = resp.json()
    assert body["total"] == 3
    assert body["page"] == 2
    assert len(body["items"]) == 1


@pytest.mark.asyncio
async def test_detail_full_description_and_404(client, web_user, jobs_fixture):
    _user, cookies = web_user
    job_id = jobs_fixture[0].id
    resp = await client.get(f"/api/v1/jobs/{job_id}", cookies=cookies)
    assert resp.status_code == 200
    assert len(resp.json()["description"]) > 300  # not truncated

    resp = await client.get("/api/v1/jobs/999999", cookies=cookies)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_feedback_create_and_update(client, web_user, jobs_fixture, session):
    user, cookies = web_user
    job_id = jobs_fixture[0].id

    resp = await client.post(
        f"/api/v1/jobs/{job_id}/feedback",
        json={"feedback": "interested"},
        cookies=cookies,
    )
    assert resp.status_code == 200

    uj = (
        await session.execute(
            select(UserJob).where(UserJob.user_id == user.id, UserJob.job_id == job_id)
        )
    ).scalar_one()
    assert uj.feedback == "interested"
    assert uj.feedback_at is not None

    # Update flips the value on the same row (no duplicate)
    resp = await client.post(
        f"/api/v1/jobs/{job_id}/feedback",
        json={"feedback": "not_relevant"},
        cookies=cookies,
    )
    assert resp.status_code == 200
    await session.refresh(uj)
    assert uj.feedback == "not_relevant"

    # Reflected in the list payload
    resp = await client.get("/api/v1/jobs", cookies=cookies)
    item = next(i for i in resp.json()["items"] if i["id"] == job_id)
    assert item["feedback"] == "not_relevant"


@pytest.mark.asyncio
async def test_feedback_invalid_value(client, web_user, jobs_fixture):
    _user, cookies = web_user
    resp = await client.post(
        f"/api/v1/jobs/{jobs_fixture[0].id}/feedback",
        json={"feedback": "meh"},
        cookies=cookies,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_apply_click_creates_application_idempotently(
    client, web_user, jobs_fixture, session
):
    user, cookies = web_user
    job_id = jobs_fixture[0].id

    resp1 = await client.post(f"/api/v1/jobs/{job_id}/apply-click", cookies=cookies)
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert body1["status"] == "applied"
    assert body1["source"] == "web"
    assert body1["job"]["title"] == "Python Developer Intern"

    resp2 = await client.post(f"/api/v1/jobs/{job_id}/apply-click", cookies=cookies)
    assert resp2.status_code == 200
    assert resp2.json()["id"] == body1["id"]

    apps = (
        await session.execute(
            select(Application).where(Application.user_id == user.id)
        )
    ).scalars().all()
    assert len(apps) == 1

    # Reflected as applied=true in the list payload
    resp = await client.get("/api/v1/jobs", cookies=cookies)
    item = next(i for i in resp.json()["items"] if i["id"] == job_id)
    assert item["applied"] is True
