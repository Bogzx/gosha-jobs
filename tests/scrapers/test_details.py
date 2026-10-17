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


@pytest.mark.parametrize("raw", ["\t", "\n", "\r\n"])
def test_hipo_json_ld_with_raw_control_characters_is_read(raw):
    """Probe in the prod image, 2026-10-17 10:48 UTC (Bosch Romania id 270303,
    200 OK): the JobPosting JSON-LD has a raw control character inside its
    description string. Strict json.loads rejected the block, so every Bosch
    and Continental Hipo row (and 2 of 7 AUMOVIO) was stored with neither a
    description nor a date, and DMs went out for some of them."""
    page = fixture("hipo_detail.html").replace(
        "Emerson is a global leader", f"Emerson is a{raw}global leader", 1,
    )
    detail = hipo._extract_detail(page)
    assert detail["posted_at"] == datetime(2026, 9, 30, tzinfo=timezone.utc)
    assert detail["description"].startswith("Emerson is a")
    assert "global leader" in detail["description"]
    # ... and none of them reaches the database or a DM embed.
    assert "\t" not in detail["description"] and "\r" not in detail["description"]


def test_hipo_description_comes_from_the_page_body_when_it_has_structure():
    """Audit 2026-10-17 (cause A): Hipo's JSON-LD text drops its tags without
    a separator, so lines and bullets glued into one sentence: one "is a
    plus" voided every requirement, "3+ years" fused to the word before it.
    3 wrong DMs (rows 8313, 8362, 8368). The page body keeps the bullets."""
    from gosha.filters import required_years

    page = fixture("hipo_detail_body.html")  # AUMOVIO, row 8362
    detail = hipo._extract_detail(page)
    lines = detail["description"].splitlines()
    assert "3+ years work experience with the public cloud provider AWS and/or Azure" in lines
    assert "About the role/ responsibilities:" in lines
    assert required_years(detail["description"]) == 3
    # The JSON-LD still gives the date and the place.
    assert detail["posted_at"].date().isoformat() == hipo._job_posting(page)["datePosted"][:10]
    assert detail["location"].endswith(", Romania")

    glued = html_to_text(hipo._job_posting(page)["qualifications"])
    assert "Azure.About the role" in glued  # what Hipo's JSON-LD says
    assert required_years(glued) is None


def test_hipo_body_text_is_repaired_like_the_json_ld():
    """The JSON-LD path undoes double-encoded UTF-8; the body gets the same."""
    page = fixture("hipo_detail_body.html")
    good = "Experiență în L’Oréal și Ștefănești"
    broken = good.encode("utf-8").decode("latin-1")
    start = page.index('<div id="anunt-content"')
    for text, expected in ((broken, good), (good, good)):
        marked = page[:start] + (
            f'<div id="anunt-content"><p>{text}</p><ul><li>{"Cloud work " * 30}</li>'
            "<li>3+ years with AWS</li></ul></div></body></html>"
        )
        assert hipo._body_description(marked).splitlines()[0] == expected


def test_hipo_falls_back_to_the_json_ld_without_a_structured_body():
    page = fixture("hipo_detail_body.html")
    start = page.index('<div id="anunt-content"')
    one_line = page[:start] + '<div id="anunt-content"><p>Apply on our site.</p></div></body></html>'
    detail = hipo._extract_detail(one_line)
    assert detail["description"].startswith("AUMOVIO")  # the JSON-LD text
    assert "posted_at" in detail


def test_html_to_text_turns_raw_tabs_and_carriage_returns_into_whitespace():
    assert html_to_text("<p>a\tb</p><p>c\rd\r\ne</p>") == "a b\nc\nd\ne"


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
    assert page.call_count == 3  # capped
    assert len(jobs) == 3 and all(j.description for j in jobs)  # the rest wait

    jobs = await bestjobs.BestJobsScraper().search(query)  # next cycle
    assert len(jobs) == 5 and all(j.description for j in jobs)
    assert page.call_count == 5  # only the two not cached yet


@respx.mock
async def test_bestjobs_postings_over_the_cap_wait_for_their_page(monkeypatch):
    """Prod, 2026-10-16: the deploys emptied the detail cache, 20 pages per
    search were fetched and the other postings were stored without a page.
    Unmarked titles with no level and no description pass the junior filter,
    so 36 of 40 new BestJobs rows had no description; the next cycle read
    their pages (Middle), filtered them out and never updated them."""
    monkeypatch.setattr(bestjobs, "MAX_DETAIL_FETCHES", 2)
    payload = _bestjobs_list(4)
    for i, title in enumerate(["Junior Developer", "Frontend Developer", ".NET Engineer", "QA"]):
        payload["items"][i]["title"] = title
    respx.get(bestjobs.API_URL).mock(return_value=httpx.Response(200, json=payload))
    respx.get(url__regex=r"https://www\.bestjobs\.eu/ro/loc-de-munca/job-\d+").mock(
        return_value=httpx.Response(200, text=fixture("bestjobs_detail.html")),
    )
    jobs = await bestjobs.BestJobsScraper().search(SearchQuery("developer", "cluj"))
    assert [j.title for j in jobs] == ["Junior Developer", "Frontend Developer"]
    assert all(j.description for j in jobs)


@respx.mock
async def test_bestjobs_postings_not_worth_a_page_are_not_held_back(monkeypatch):
    """Only pages that were wanted wait: a title irrelevant at entry level
    is not fetched at all, and the pipeline's filter decides on it."""
    monkeypatch.setattr(bestjobs, "MAX_DETAIL_FETCHES", 1)
    payload = _bestjobs_list(3)
    for i, title in enumerate(["Junior Developer", "Senior Architect", "Head of IT"]):
        payload["items"][i]["title"] = title
    respx.get(bestjobs.API_URL).mock(return_value=httpx.Response(200, json=payload))
    page = respx.get(url__regex=r"https://www\.bestjobs\.eu/.*").mock(
        return_value=httpx.Response(200, text=fixture("bestjobs_detail.html")),
    )
    query = SearchQuery("developer", "cluj", relevant=lambda title, _level: "Junior" in title)
    jobs = await bestjobs.BestJobsScraper().search(query)
    assert page.call_count == 1
    assert [j.title for j in jobs] == ["Junior Developer", "Senior Architect", "Head of IT"]


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
    respx.get(hipo.LISTING_URL.format(city="Cluj-Napoca") + "/2").mock(
        return_value=httpx.Response(200, text=listing),  # no new cards: stop
    )
    jobs = await hipo.HipoScraper().search(SearchQuery("anything", "cluj"))
    enriched = [j for j in jobs if j.description]
    assert enriched and len(enriched) == min(len(jobs), hipo.MAX_DETAIL_FETCHES)
    assert all(j.posted_at is not None for j in enriched)


@respx.mock
async def test_hipo_postings_over_the_cap_wait_for_their_page(monkeypatch):
    """Same as BestJobs: 13 of the 26 Hipo rows stored after the 2026-10-16
    deploys had no description, 7 of them never seen again."""
    monkeypatch.setattr(hipo, "MAX_DETAIL_FETCHES", 2)
    listing = fixture("hipo_listing.html")
    respx.get(hipo.LISTING_URL.format(city="Cluj-Napoca")).mock(
        return_value=httpx.Response(200, text=listing),
    )
    page = respx.get(url__regex=r"https://www\.hipo\.ro/locuri-de-munca/locuri_de_munca/.*").mock(
        return_value=httpx.Response(200, text=fixture("hipo_detail.html")),
    )
    respx.get(hipo.LISTING_URL.format(city="Cluj-Napoca") + "/2").mock(
        return_value=httpx.Response(200, text=listing),
    )
    query = SearchQuery("anything", "cluj")
    jobs = await hipo.HipoScraper().search(query)
    assert page.call_count == 2 and len(jobs) == 2 and all(j.description for j in jobs)
    jobs = await hipo.HipoScraper().search(query)  # next cycle
    assert page.call_count == 4 and len(jobs) == 4 and all(j.description for j in jobs)


@respx.mock
async def test_hipo_dates_are_marked_as_refresh_dates():
    """Hipo's JSON-LD datePosted is the day the employer last refreshed the
    ad (most cards say today, even old ids). The flag lets the pipeline keep
    the earliest date instead of making old postings look new: of the 10
    active Hipo rows with a date on 2026-10-16, 8 were dated more than a
    day after we first saw them.

    (Until 2026-10-17 this test had no @respx.mock and fetched the real
    hipo.ro listing on every run.)"""
    listing = fixture("hipo_listing.html")
    respx.get(hipo.LISTING_URL.format(city="Cluj-Napoca")).mock(
        return_value=httpx.Response(200, text=listing),
    )
    respx.get(url__regex=r"https://www\.hipo\.ro/locuri-de-munca/locuri_de_munca/265009/.*").mock(
        side_effect=httpx.ConnectError("boom"),  # one page fails: no date
    )
    respx.get(url__regex=r"https://www\.hipo\.ro/locuri-de-munca/locuri_de_munca/.*").mock(
        return_value=httpx.Response(200, text=fixture("hipo_detail.html")),
    )
    respx.get(hipo.LISTING_URL.format(city="Cluj-Napoca") + "/2").mock(
        return_value=httpx.Response(200, text=listing),
    )
    jobs = await hipo.HipoScraper().search(SearchQuery("anything", "cluj"))
    dated = [j for j in jobs if j.posted_at is not None]
    undated = [j for j in jobs if j.posted_at is None]
    assert len(dated) == 4 and len(undated) == 1
    assert all(j.posted_at_is_refresh for j in dated)
    assert all(j.to_record()["date_posted_is_refresh"] is True for j in dated)
    assert not any(j.posted_at_is_refresh for j in undated)


def test_other_boards_dates_are_posting_dates():
    from gosha.scrapers.base import RawJob

    job = RawJob(url="https://x/1", title="QA", posted_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert job.posted_at_is_refresh is False
    assert job.to_record()["date_posted_is_refresh"] is False


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


@respx.mock
async def test_detail_fetches_share_an_hourly_budget_per_host(monkeypatch):
    """Many terms x per-search limit must not mean hundreds of pages at once."""
    monkeypatch.setattr(details, "HOST_BUDGET_PER_HOUR", 4)
    page = respx.get(url__regex=r"https://board\.test/.*").mock(
        return_value=httpx.Response(200, text="<p>x</p>"),
    )
    async with httpx.AsyncClient() as client:
        for term in range(3):  # three searches, limit 3 each
            urls = [f"https://board.test/{term}/{i}" for i in range(3)]
            await details.fetch_details(client, urls, lambda t: {"d": t}, limit=3)
    assert page.call_count == 4


@respx.mock
async def test_fetch_details_reports_the_pages_left_for_later(monkeypatch):
    """Over the limit or the host budget: deferred. Cached: not. Failed:
    not deferred (retried next cycle, but nobody should wait on it)."""
    monkeypatch.setattr(details, "HOST_BUDGET_PER_HOUR", 3)
    respx.get("https://board.test/bad").mock(side_effect=httpx.ConnectError("boom"))
    respx.get(url__regex=r"https://board\.test/ok/\d").mock(
        return_value=httpx.Response(200, text="<p>x</p>"),
    )
    async with httpx.AsyncClient() as client:
        await details.fetch_details(client, ["https://board.test/ok/0"], lambda t: {"d": t}, 5)
        deferred: set[str] = set()
        urls = ["https://board.test/ok/0", "https://board.test/bad",
                "https://board.test/ok/1", "https://board.test/ok/2", "https://board.test/ok/3"]
        got = await details.fetch_details(client, urls, lambda t: {"d": t}, 3, deferred)
        assert set(got) == {"https://board.test/ok/0", "https://board.test/ok/1"}
        # limit 3 left room for ok/2, but the hourly budget (3) was spent
        assert deferred == {"https://board.test/ok/2", "https://board.test/ok/3"}

        deferred = set()
        await details.fetch_details(client, ["https://board.test/ok/2"], lambda t: {}, 1, deferred)
        assert deferred == {"https://board.test/ok/2"}  # still no budget this hour
