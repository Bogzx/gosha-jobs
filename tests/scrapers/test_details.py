"""Detail-page descriptions for BestJobs and Hipo (fixtures recorded
2026-09-30, trimmed to the parts the parsers read)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from gosha.scrapers import bestjobs, details, hipo
from gosha.scrapers.base import SearchQuery
from gosha.scrapers.details import html_to_text

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def fresh_caches(monkeypatch):
    details.clear_cache()
    monkeypatch.setattr(hipo, "_cache", {})
    yield
    details.clear_cache()


def test_html_to_text_unescapes_and_keeps_paragraphs():
    text = html_to_text("<p><b>Python</b>&nbsp;&amp; Django</p><ul><li>Go</li><li>SQL</li></ul>")
    assert text == "Python & Django\nGo\nSQL"


def test_bestjobs_detail_extraction():
    detail = bestjobs._extract_detail(fixture("bestjobs_detail.html"))
    assert detail["company"] == "OKKWEBMEDIA SRL"
    assert detail["description"].startswith("Căutăm Wordpress developer")
    assert "<" not in detail["description"] and "&nbsp;" not in detail["description"]
    assert len(detail["description"]) > 1000


def test_hipo_detail_extraction():
    detail = hipo._extract_detail(fixture("hipo_detail.html"))
    assert detail["posted_at"] == datetime(2026, 9, 30, tzinfo=timezone.utc)
    text = detail["description"]
    assert text.startswith("Emerson is a global leader")
    assert "functional specifications" in text
    # Hipo repeats one paragraph across several JobPosting fields.
    assert text.count("You will translate business needs") == 1


def test_changed_markup_yields_no_detail_rather_than_an_error():
    assert bestjobs._extract_detail("<html>redesigned</html>") == {}
    assert hipo._extract_detail("<html><script type='application/ld+json'>{bad</script>") == {}


def _bestjobs_list(n: int) -> dict:
    items = json.loads(fixture("bestjobs_with_anonymous.json"))["items"]
    rows = []
    for i in range(n):
        item = dict(items[i % len(items)])
        item["slug"] = f"job-{i}"
        rows.append(item)
    return {"items": rows}


@respx.mock
async def test_bestjobs_search_fetches_capped_cached_details(monkeypatch):
    monkeypatch.setattr(bestjobs, "MAX_DETAIL_FETCHES", 3)
    respx.get(bestjobs.API_URL).mock(return_value=httpx.Response(200, json=_bestjobs_list(5)))
    page = respx.get(url__regex=r"https://www\.bestjobs\.eu/ro/loc-de-munca/job-\d+").mock(
        return_value=httpx.Response(200, text=fixture("bestjobs_detail.html")),
    )
    query = SearchQuery("software engineer", "cluj")

    jobs = await bestjobs.BestJobsScraper().search(query)
    assert len(jobs) == 5
    assert sum(bool(j.description) for j in jobs) == 3  # capped
    assert page.call_count == 3

    jobs = await bestjobs.BestJobsScraper().search(query)  # next cycle
    assert sum(bool(j.description) for j in jobs) == 5
    assert page.call_count == 5  # only the two not cached yet


@respx.mock
async def test_bestjobs_detail_names_the_employer_of_anonymous_postings():
    payload = json.loads(fixture("bestjobs_with_anonymous.json"))
    respx.get(bestjobs.API_URL).mock(return_value=httpx.Response(200, json=payload))
    respx.get(url__regex=r"https://www\.bestjobs\.eu/.*").mock(
        return_value=httpx.Response(200, text=fixture("bestjobs_detail.html")),
    )
    jobs = await bestjobs.BestJobsScraper().search(SearchQuery("software engineer", "cluj"))
    assert all(j.company != "Unknown" for j in jobs)


@respx.mock
async def test_detail_failures_leave_the_listing_intact():
    respx.get(bestjobs.API_URL).mock(return_value=httpx.Response(200, json=_bestjobs_list(2)))
    respx.get(url__regex=r"https://www\.bestjobs\.eu/.*").mock(
        side_effect=httpx.ConnectError("boom"),
    )
    jobs = await bestjobs.BestJobsScraper().search(SearchQuery("dev", "cluj"))
    assert len(jobs) == 2 and all(j.description == "" for j in jobs)
    assert details._cache == {}  # network errors are retried next cycle


@respx.mock
async def test_hipo_search_adds_descriptions_and_dates():
    listing = fixture("hipo_listing.html")
    respx.get(hipo.LISTING_URL.format(city="Cluj-Napoca")).mock(
        return_value=httpx.Response(200, text=listing),
    )
    respx.get(url__regex=r"https://www\.hipo\.ro/locuri-de-munca/locuri_de_munca/.*").mock(
        return_value=httpx.Response(200, text=fixture("hipo_detail.html")),
    )
    jobs = await hipo.HipoScraper().search(SearchQuery("anything", "cluj"))
    enriched = [j for j in jobs if j.description]
    assert enriched and len(enriched) == min(len(jobs), hipo.MAX_DETAIL_FETCHES)
    assert all(j.posted_at is not None for j in enriched)


async def test_new_description_resets_the_stale_embedding(patched_db, session):
    """A job first stored title-only is re-embedded once a description arrives."""
    import pandas as pd

    from gosha.models import Job
    from gosha.pipeline import upsert_jobs

    row = {"job_url": "https://j.test/1", "title": "Dev", "company": "Acme",
           "site": "hipo", "description": ""}
    (job,) = await upsert_jobs(pd.DataFrame([row]))
    job_id = job.id
    stored = await session.get(Job, job_id)
    stored.embedding, stored.embedding_model = b"\x00" * 8, "all-mpnet-base-v2"
    await session.commit()

    await upsert_jobs(pd.DataFrame([{**row, "description": "Python and SQL"}]))
    session.expire_all()
    stored = await session.get(Job, job_id)
    assert stored.description == "Python and SQL"
    assert stored.embedding is None and stored.embedding_model is None


def test_double_encoded_json_ld_is_repaired():
    """Bytes as served by a Hipo page on 2026-09-30 (JSON-LD only)."""
    from gosha.scrapers.details import repair_mojibake

    served = b"At L\xc3\xa2\xc2\x80\xc2\x99Or\xc3\x83\xc2\xa9al, we".decode("utf-8")
    assert repair_mojibake(served) == "At L’Oréal, we"


@pytest.mark.parametrize("text", [
    "Căutăm dezvoltator în Iași, știi să lucrezi în echipă",  # real Romanian
    "Café Olé — plain Latin-1 is not mojibake",
    "ASCII only",
])
def test_mojibake_repair_leaves_good_text_alone(text):
    from gosha.scrapers.details import repair_mojibake

    assert repair_mojibake(text) == text
