"""The salary filter, end to end across mixed-currency sources."""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import select

from gosha.models import Job


def _job(session, **kwargs):
    job = Job(
        url=kwargs.pop("url"),
        title=kwargs.pop("title", "Engineer"),
        company=kwargs.pop("company", "Acme"),
        location=kwargs.pop("location", "Cluj-Napoca, Romania"),
        source=kwargs.pop("source", "ejobs"),
        **kwargs,
    )
    session.add(job)
    return job


@pytest.mark.asyncio
async def test_filter_compares_across_currencies_and_periods(
    client, web_user, session,
):
    """Three postings that look ordered one way in raw numbers and the
    opposite way once normalised."""
    _user, cookies = web_user

    # 90,000 USD/year ~ 34,500 RON/month — the best paid.
    _job(
        session, url="https://r.com/remote", source="remoteok",
        salary_min=90_000, salary_max=90_000, salary_currency="USD",
        salary_period="yearly",
        salary_monthly_min_ron=34_500, salary_monthly_max_ron=34_500,
    )
    # 2,000 EUR/month ~ 10,000 RON/month.
    _job(
        session, url="https://r.com/best", source="bestjobs",
        salary_min=2000, salary_max=2000, salary_currency="EUR",
        salary_period="monthly",
        salary_monthly_min_ron=10_000, salary_monthly_max_ron=10_000,
    )
    # 4,000 RON/month — an internship.
    _job(
        session, url="https://r.com/ejobs", source="ejobs",
        salary_min=4000, salary_max=4000, salary_currency="RON",
        salary_period="monthly",
        salary_monthly_min_ron=4000, salary_monthly_max_ron=4000,
    )
    await session.commit()

    resp = await client.get(
        "/api/v1/jobs", params={"salary_min": 9000}, cookies=cookies
    )
    urls = {item["url"] for item in resp.json()["items"]}

    # The raw numbers would have ranked the 4,000 RON internship above the
    # 2,000 EUR role; normalised, the internship is the one excluded.
    assert urls == {"https://r.com/remote", "https://r.com/best"}


@pytest.mark.asyncio
async def test_jobs_without_salary_data_are_not_hidden(client, web_user, session):
    _user, cookies = web_user
    _job(session, url="https://r.com/nosalary")
    await session.commit()

    resp = await client.get(
        "/api/v1/jobs", params={"salary_min": 20_000}, cookies=cookies
    )
    assert [i["url"] for i in resp.json()["items"]] == ["https://r.com/nosalary"]


@pytest.mark.asyncio
async def test_upsert_normalises_salary_at_ingest(patched_db, session):
    """RemoteOK's annual USD must not land in the DB as a monthly figure."""
    import gosha.pipeline as pipeline

    df = pd.DataFrame([{
        "job_url": "https://remoteok.com/1",
        "title": "Backend Engineer",
        "company": "Remote Inc",
        "location": "Remote",
        "site": "remoteok",
        "min_amount": 90_000.0,
        "max_amount": 140_000.0,
        "currency": "USD",
        "interval": "yearly",
    }])

    await pipeline.upsert_jobs(df)

    stored = (
        await session.execute(select(Job).where(Job.url == "https://remoteok.com/1"))
    ).scalar_one()

    # Raw values are preserved for display...
    assert stored.salary_min == 90_000
    assert stored.salary_currency == "USD"
    assert stored.salary_period == "yearly"
    # ...and the comparable figure is monthly RON, not 90,000-of-something.
    assert 30_000 < stored.salary_monthly_min_ron < 40_000
    assert stored.salary_monthly_max_ron > stored.salary_monthly_min_ron


@pytest.mark.asyncio
async def test_backfill_fills_rows_scraped_before_normalisation(
    patched_db, session,
):
    from gosha.services.jobs import backfill_salary_normalisation

    _job(
        session, url="https://old.com/1", source="ejobs",
        salary_min=7000, salary_max=9000, salary_currency="RON",
        salary_period="monthly",
    )
    _job(session, url="https://old.com/2", source="ejobs")  # no salary at all
    await session.commit()

    assert await backfill_salary_normalisation() == 1

    session.expire_all()
    filled = (
        await session.execute(select(Job).where(Job.url == "https://old.com/1"))
    ).scalar_one()
    assert filled.salary_monthly_min_ron == 7000
    assert filled.salary_monthly_max_ron == 9000

    # Nothing to compute for a job with no salary — and no second pass.
    assert await backfill_salary_normalisation() == 0


@pytest.mark.asyncio
async def test_api_exposes_the_comparable_figure(client, web_user, session):
    _user, cookies = web_user
    _job(
        session, url="https://r.com/api", source="remoteok",
        salary_min=120_000, salary_max=120_000, salary_currency="USD",
        salary_period="yearly",
        salary_monthly_min_ron=46_000, salary_monthly_max_ron=46_000,
    )
    await session.commit()

    item = (await client.get("/api/v1/jobs", cookies=cookies)).json()["items"][0]
    assert item["salary_currency"] == "USD"
    assert item["salary_period"] == "yearly"
    assert item["salary_monthly_min_ron"] == 46_000
