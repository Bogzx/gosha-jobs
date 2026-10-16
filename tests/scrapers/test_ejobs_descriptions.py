"""Tests for eJobs description enrichment from the detail endpoint."""

from __future__ import annotations

import json
from pathlib import Path

from gosha.scrapers import ejobs

FIXTURES = Path(__file__).parent / "fixtures"


def test_extract_description_from_detail():
    detail = json.loads((FIXTURES / "ejobs_detail.json").read_text(encoding="utf-8"))
    text = ejobs._extract_description(detail)

    assert text, "real detail fixture should yield a description"
    assert "<" not in text  # HTML stripped
    assert len(text) > 100


def test_extract_description_handles_missing_details():
    assert ejobs._extract_description({}) == ""
    assert ejobs._extract_description({"details": {}}) == ""
    assert ejobs._extract_description({"details": {"jobDescription": None}}) == ""


def test_extract_detail_wraps_the_description():
    detail = json.loads((FIXTURES / "ejobs_detail.json").read_text(encoding="utf-8"))
    extracted = ejobs._extract_detail(json.dumps(detail))
    assert extracted["description"] == ejobs._extract_description(detail)
    assert ejobs._extract_detail(json.dumps({"details": {}})) == {}


# ── Dates ─────────────────────────────────────────────────────────────
# The list's creationDate is the republish time: posting 1961022 is listed
# with creationDate 2026-06-10T08:40:00Z while its detail page says
# creationDate 2026-06-01T15:46:56Z, republishDate 2026-06-10T08:40:00+03:00.
# On 2026-10-16, 29 of the 64 active eJobs rows in prod were dated more than
# a day after we first saw them, and 7 of 53 eJobs DMs after they were sent.

from datetime import datetime, timezone  # noqa: E402

import httpx  # noqa: E402
import respx  # noqa: E402

from gosha.scrapers import details  # noqa: E402
from gosha.scrapers.base import SearchQuery  # noqa: E402


def _listing() -> list[dict]:
    return json.loads((FIXTURES / "ejobs_search.json").read_text(encoding="utf-8"))["jobs"]


def test_list_dates_are_marked_as_refresh_dates():
    jobs = ejobs._parse(_listing(), {14: "Cluj-Napoca"})
    assert jobs and all(j.posted_at is not None for j in jobs)
    assert all(j.posted_at_is_refresh for j in jobs)


def test_detail_gives_the_first_posting_date():
    detail = (FIXTURES / "ejobs_detail.json").read_text(encoding="utf-8")
    assert ejobs._extract_detail(detail)["posted_at"] == datetime(
        2026, 6, 1, 15, 46, 56, tzinfo=timezone.utc,
    )


def test_detail_without_a_date_keeps_only_the_description():
    body = json.dumps({"details": {"jobDescription": "<p>Build APIs for our clients.</p>"}})
    assert ejobs._extract_detail(body) == {"description": "Build APIs for our clients."}


@respx.mock
async def test_search_uses_the_detail_date_as_the_posting_date():
    ejobs.clear_cache()
    details.clear_cache()
    item = next(i for i in _listing() if i["id"] == 1961022)
    respx.get(ejobs.CITIES_URL).mock(return_value=httpx.Response(
        200, json={"cities": [{"id": 14, "name": "Cluj-Napoca", "slug": "cluj-napoca"}]},
    ))
    respx.get(url__regex=rf"{ejobs.API_URL}/\d+$").mock(return_value=httpx.Response(
        200, text=(FIXTURES / "ejobs_detail.json").read_text(encoding="utf-8"),
    ))
    respx.get(url__regex=rf"{ejobs.API_URL}\?.*").mock(return_value=httpx.Response(
        200, json={"jobs": [item], "totalCount": 1, "morePagesFollow": False},
    ))
    try:
        (job,) = await ejobs.EjobsScraper().search(
            SearchQuery(keyword="software engineer", location="cluj", max_age_days=3650),
        )
    finally:
        ejobs.clear_cache()
        details.clear_cache()
    assert job.posted_at == datetime(2026, 6, 1, 15, 46, 56, tzinfo=timezone.utc)
    assert job.posted_at_is_refresh is False
    assert job.description
