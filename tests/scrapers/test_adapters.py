"""Adapter parsing tests against fixtures recorded from the live endpoints
(scripts/record_fixtures.py, 2026-06-10)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from gosha.scrapers import bestjobs, details, ejobs, hipo, jobicy, remoteok
from gosha.scrapers.base import SearchQuery

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ── RemoteOK ──────────────────────────────────────────────────────────


def test_remoteok_parse_filters_by_keyword():
    feed = load_json("remoteok_feed.json")
    query = SearchQuery(keyword="design", location="remote", max_age_days=365)
    jobs = remoteok._parse(feed, query)

    assert jobs, "expected at least one design-tagged job in the fixture"
    for job in jobs:
        assert job.source == "remoteok"
        assert job.url.startswith("http")
        assert job.title
        # matched via title or tags; description is stripped of HTML
        assert "<" not in job.description


def test_remoteok_parse_respects_age_cutoff():
    feed = load_json("remoteok_feed.json")
    query = SearchQuery(keyword="", location="remote", max_age_days=0)
    assert remoteok._parse(feed, query) == []


def test_remoteok_only_applies_to_remote_queries():
    scraper = remoteok.RemoteOKScraper()
    assert scraper.applies_to(SearchQuery(keyword="x", location="Remote"))
    assert scraper.applies_to(SearchQuery(keyword="x", location="europe"))
    assert not scraper.applies_to(SearchQuery(keyword="x", location="Cluj-Napoca, Romania"))


@pytest.mark.asyncio
async def test_remoteok_search_skips_non_remote():
    scraper = remoteok.RemoteOKScraper()
    result = await scraper.search(SearchQuery(keyword="python", location="cluj"))
    assert result == []  # no network call needed


def test_remoteok_repairs_mojibake_and_drops_non_http_urls():
    now = "2026-10-11T08:00:06+00:00"
    feed = [
        {"legal": "notice"},
        {
            "position": "Python Developer", "company": "Acme", "date": now,
            "url": "https://remoteok.com/remote-jobs/1",
            "description": "<p>you\u00e2\u0080\u0099ll write Python</p>",
        },
        {
            "position": "Python Developer", "company": "Evil", "date": now,
            "url": "javascript:alert(1)",
        },
    ]
    jobs = remoteok._parse(feed, SearchQuery(keyword="python", location="remote", max_age_days=3650))
    assert [j.company for j in jobs] == ["Acme"]
    assert jobs[0].description == "you\u2019ll write Python"


# ── Jobicy ────────────────────────────────────────────────────────────


def _jobicy_item(**overrides) -> dict:
    item = {
        "id": 1, "url": "https://jobicy.com/jobs/1-python-dev",
        "jobTitle": "Python Developer &amp; Data", "companyName": "Acme",
        "jobIndustry": ["Software Engineering"], "jobGeo": "Europe",
        "jobLevel": "Midweight", "jobDescription": "<p>Build <b>APIs</b></p>",
        "pubDate": "2026-10-12T07:02:34+00:00",
        "salaryMin": 60000, "salaryMax": 80000, "salaryCurrency": "EUR",
        "salaryPeriod": "yearly",
    }
    item.update(overrides)
    return item


def test_jobicy_parse_maps_fields():
    feed = [
        _jobicy_item(),
        _jobicy_item(id=2, url="https://jobicy.com/jobs/2", jobTitle="Account Executive",
                     jobIndustry=["Sales"]),
        _jobicy_item(id=3, url="ftp://x", jobTitle="Python Lead"),
    ]
    jobs = jobicy._parse(feed, SearchQuery(keyword="python developer", location="remote",
                                           max_age_days=3650))
    (job,) = jobs
    assert job.title == "Python Developer & Data"
    assert job.location == "Remote (Europe)"
    assert job.description == "Build APIs"
    assert job.experience_level == "mid"
    assert (job.salary_min, job.salary_max, job.salary_currency, job.salary_period) == (
        60000, 80000, "EUR", "yearly",
    )
    assert job.source == "jobicy"


def test_jobicy_only_serves_remote_searches():
    scraper = jobicy.JobicyScraper()
    assert scraper.applies_to(SearchQuery(keyword="x", location="remote"))
    assert scraper.applies_to(SearchQuery(keyword="x", location="Europe"))
    assert not scraper.applies_to(SearchQuery(keyword="x", location="cluj"))


@respx.mock
async def test_jobicy_polls_at_most_once_per_cache_window():
    jobicy.clear_cache()
    route = respx.get(jobicy.API_URL).mock(side_effect=[
        httpx.Response(200, json={"jobs": [_jobicy_item()], "hasMore": True, "nextCursor": "c2"}),
        httpx.Response(200, json={
            "jobs": [_jobicy_item(id=2, url="https://jobicy.com/jobs/2")],
            "hasMore": False, "nextCursor": None,
        }),
    ])
    query = SearchQuery(keyword="python", location="remote", max_age_days=3650)
    first = await jobicy.JobicyScraper().search(query)
    again = await jobicy.JobicyScraper().search(query)
    assert len(first) == len(again) == 2
    assert route.call_count == 2  # two pages, once
    assert route.calls[1].request.url.params["cursor"] == "c2"
    jobicy.clear_cache()


# ── eJobs ─────────────────────────────────────────────────────────────


@pytest.fixture
def ejobs_fresh():
    ejobs.clear_cache()
    details.clear_cache()
    yield
    ejobs.clear_cache()
    details.clear_cache()


def _ejobs_city_map() -> dict[int, str]:
    names, _ids = ejobs._index_cities(load_json("ejobs_cities.json"))
    return names


def test_ejobs_parse_maps_fields_and_levels():
    payload = load_json("ejobs_search.json")
    jobs = ejobs._parse(payload["jobs"], _ejobs_city_map())

    assert len(jobs) == len(payload["jobs"])
    for job in jobs:
        assert job.source == "ejobs"
        assert job.url.startswith("https://www.ejobs.ro/")
        assert job.title
        assert job.location.endswith("Romania")
    by_id = {int(j.url.rsplit("/", 1)[1]): j for j in jobs}
    # careerLevelsIds [3, 10] -> entry; [4, 6] -> mid (the most junior wins)
    assert by_id[1963288].experience_level == "entry"
    assert by_id[1961022].experience_level == "mid"
    assert by_id[1960845].experience_level == "senior"
    assert "București" in by_id[1959988].location


def test_ejobs_parse_drops_postings_older_than_the_window():
    item = {
        "id": 1, "title": "Dev", "slug": "dev", "company": {"name": "X"},
        "locations": [{"cityId": 14}], "creationDate": "2020-01-01T00:00:00Z",
    }
    assert ejobs._parse([item], {14: "Cluj-Napoca"}, max_age_days=14) == []
    assert len(ejobs._parse([item], {14: "Cluj-Napoca"})) == 1


def test_ejobs_parse_salary_when_present():
    item = {
        "id": 1, "title": "Dev", "slug": "dev",
        "company": {"name": "X"},
        "salary": "5000 - 10000 RON",
        "locations": [{"cityId": 14}],
        "creationDate": "2026-06-10T12:00:00Z",
    }
    (job,) = ejobs._parse([item], {14: "Cluj-Napoca"})
    assert job.salary_min == 5000
    assert job.salary_max == 10000
    assert job.salary_currency == "RON"
    assert job.salary_period == "monthly"
    assert job.posted_at is not None
    assert job.location == "Cluj-Napoca, Romania"


def test_ejobs_city_lookup_handles_aliases_and_diacritics(ejobs_fresh):
    _names, ids = ejobs._index_cities(load_json("ejobs_cities.json"))
    ejobs._city_ids.update(ids)
    assert ejobs.city_id_for("cluj") == 14
    assert ejobs.city_id_for("Bucharest") == 1  # via the "bucuresti" alias
    assert ejobs.city_id_for("Timișoara") == 34
    assert ejobs.city_id_for("tokyo") is None


def test_ejobs_applies_only_to_romanian_locations():
    scraper = ejobs.EjobsScraper()
    assert scraper.applies_to(SearchQuery(keyword="x", location="cluj"))
    assert scraper.applies_to(SearchQuery(keyword="x", location="romania"))
    assert not scraper.applies_to(SearchQuery(keyword="x", location="remote"))
    assert not scraper.applies_to(SearchQuery(keyword="x", location="london"))


def _ejobs_page(jobs: list[dict], more: bool = False) -> httpx.Response:
    return httpx.Response(200, json={"jobs": jobs, "morePagesFollow": more})


@respx.mock
async def test_ejobs_tech_search_browses_it_departments_in_the_city(ejobs_fresh):
    respx.get(ejobs.CITIES_URL).mock(
        return_value=httpx.Response(200, json=load_json("ejobs_cities.json")),
    )
    items = load_json("ejobs_search.json")["jobs"]
    listing = respx.get(ejobs.API_URL).mock(side_effect=[
        _ejobs_page(items[:10], more=True), _ejobs_page(items[10:]),
    ])
    respx.get(url__regex=r"https://api\.ejobs\.ro/jobs/\d+").mock(
        return_value=httpx.Response(200, json={"details": {"jobDescription": "<p>Python</p>"}}),
    )

    jobs = await ejobs.EjobsScraper().search(
        SearchQuery(keyword="software engineer intern", location="cluj", max_age_days=3650),
    )

    assert len(jobs) == len(items)  # both pages
    params = listing.calls[0].request.url.params
    assert params["filters.cities"] == "14"  # server-side, not client-side
    assert sorted(params.get_list("filters.departments")) == sorted(
        str(d) for d in ejobs.TECH_DEPARTMENTS
    )
    assert "q" not in params  # the loose free-text search is not used
    assert listing.calls[1].request.url.params["page"] == "2"
    assert any(j.description == "Python" for j in jobs)

    # Another term of the same subscription reuses the browse.
    await ejobs.EjobsScraper().search(
        SearchQuery(keyword="data analyst intern", location="cluj", max_age_days=3650),
    )
    assert listing.call_count == 2


@respx.mock
async def test_ejobs_nationwide_search_has_no_city_filter(ejobs_fresh):
    respx.get(ejobs.CITIES_URL).mock(
        return_value=httpx.Response(200, json=load_json("ejobs_cities.json")),
    )
    listing = respx.get(ejobs.API_URL).mock(return_value=_ejobs_page([]))
    await ejobs.EjobsScraper().search(SearchQuery(keyword="contabil", location="romania"))
    params = listing.calls[0].request.url.params
    # "romania"'s match list names Bucharest; it must not narrow to it.
    assert "filters.cities" not in params
    assert params["q"] == "contabil"


@respx.mock
async def test_ejobs_survives_a_blocked_city_list(ejobs_fresh):
    """/cities answers 403 to some datacenter IPs while /jobs works."""
    respx.get(ejobs.CITIES_URL).mock(return_value=httpx.Response(403))
    item = {
        "id": 5, "title": "Java Developer", "slug": "java", "company": {"name": "X"},
        "locations": [{"cityId": 1}],
    }
    listing = respx.get(ejobs.API_URL).mock(return_value=_ejobs_page([item]))
    respx.get(url__regex=r"https://api\.ejobs\.ro/jobs/\d+").mock(
        return_value=httpx.Response(404),
    )

    jobs = await ejobs.EjobsScraper().search(SearchQuery(keyword="java", location="bucharest"))

    assert [j.location for j in jobs] == ["București, Romania"]
    assert listing.calls[0].request.url.params["filters.cities"] == "1"


@respx.mock
async def test_ejobs_fetches_details_only_for_relevant_titles(ejobs_fresh):
    respx.get(ejobs.CITIES_URL).mock(
        return_value=httpx.Response(200, json=load_json("ejobs_cities.json")),
    )
    items = [
        {"id": 1, "title": "Junior Python Developer", "slug": "a", "locations": [{"cityId": 14}]},
        {"id": 2, "title": "Agent Vanzari", "slug": "b", "locations": [{"cityId": 14}]},
    ]
    respx.get(ejobs.API_URL).mock(return_value=_ejobs_page(items))
    detail = respx.get(url__regex=r"https://api\.ejobs\.ro/jobs/\d+").mock(
        return_value=httpx.Response(200, json={"details": {"jobDescription": "x"}}),
    )
    query = SearchQuery(
        keyword="python developer", location="cluj",
        relevant=lambda title, level=None: "python" in title.lower(),
    )
    jobs = await ejobs.EjobsScraper().search(query)
    assert len(jobs) == 2  # filtering stays in the pipeline
    assert [str(c.request.url) for c in detail.calls] == [f"{ejobs.API_URL}/1"]


# ── BestJobs ──────────────────────────────────────────────────────────


def test_bestjobs_parse_maps_fields():
    payload = load_json("bestjobs_search.json")
    query = SearchQuery(keyword="software", location="cluj")
    jobs = bestjobs._parse(payload, query)

    assert jobs
    for job in jobs:
        assert job.source == "bestjobs"
        assert job.url.startswith("https://www.bestjobs.eu/ro/loc-de-munca/")
        assert job.title
        assert job.company


def test_bestjobs_salary_defaults_to_eur():
    payload = {
        "items": [{
            "slug": "x", "title": "Dev", "companyName": "Y",
            "salary": "900 - 1250",
            "locations": [{"name": "Cluj-Napoca, România"}],
        }]
    }
    jobs = bestjobs._parse(payload, SearchQuery(keyword="dev", location="cluj"))
    assert jobs[0].salary_min == 900
    assert jobs[0].salary_currency == "EUR"


def test_bestjobs_location_slug():
    assert bestjobs.location_to_slug("cluj") == "cluj-napoca-romania"
    assert bestjobs.location_to_slug("Bucharest") == "bucharest-romania"
    assert bestjobs.location_to_slug("Timișoara, România") == "timisoara-romania"


def test_bestjobs_search_locations():
    assert bestjobs.search_locations("cluj") == ["cluj-napoca-romania"]
    # BestJobs reads "romania" and "europe" as "remote", so nationwide
    # means the largest cities one by one, and remote means "europe".
    assert bestjobs.search_locations("romania") == list(bestjobs.NATIONWIDE_CITIES)
    assert bestjobs.search_locations("remote") == ["europe"]


def test_bestjobs_applies_to_romania_and_remote_only():
    scraper = bestjobs.BestJobsScraper()
    assert scraper.applies_to(SearchQuery(keyword="x", location="cluj"))
    assert scraper.applies_to(SearchQuery(keyword="x", location="remote"))
    assert scraper.applies_to(SearchQuery(keyword="x", location="Arad"))
    assert not scraper.applies_to(SearchQuery(keyword="x", location="berlin"))


def test_bestjobs_experience_level_from_career_levels():
    level = bestjobs.experience_level
    assert level([{"slug": "middle-2-5-ani"}, {"slug": "entry-0-2-ani"}]) == "entry"
    assert level([{"slug": "fara-experienta"}]) == "entry"
    assert level([{"slug": "senior-5-10-ani"}, {"slug": "executive-10-ani"}]) == "senior"
    assert level([]) is None


def test_bestjobs_apply_details_drops_expired_and_sets_level():
    jobs = bestjobs._parse({"items": [
        {"slug": "a", "title": "QA Engineer", "companyName": "X"},
        {"slug": "b", "title": "Gone", "companyName": "Y"},
    ]}, SearchQuery(keyword="qa", location="cluj"))
    kept = bestjobs._apply_details(jobs, {
        jobs[0].url: {"experience_level": "entry", "description": "Test things"},
        jobs[1].url: {"expired": True},
    })
    assert [j.title for j in kept] == ["QA Engineer"]
    assert kept[0].experience_level == "entry"


@respx.mock
async def test_bestjobs_nationwide_search_merges_cities():
    details.clear_cache()
    route = respx.get(bestjobs.API_URL).mock(side_effect=lambda request: httpx.Response(
        200, json={"items": [{
            "slug": request.url.params["location"], "title": "Dev",
            "companyName": "X",
        }]},
    ))
    respx.get(url__regex=r"https://www\.bestjobs\.eu/.*").mock(return_value=httpx.Response(404))
    jobs = await bestjobs.BestJobsScraper().search(SearchQuery(keyword="dev", location="romania"))
    assert route.call_count == len(bestjobs.NATIONWIDE_CITIES)
    assert len(jobs) == len(bestjobs.NATIONWIDE_CITIES)
    details.clear_cache()


# ── Hipo ──────────────────────────────────────────────────────────────


def test_hipo_parse_extracts_cards():
    html = (FIXTURES / "hipo_listing.html").read_text(encoding="utf-8")
    jobs = hipo._parse(html, "Cluj-Napoca")

    assert len(jobs) >= 2, "fixture holds multiple job cards"
    for job in jobs:
        assert job.source == "hipo"
        assert job.url.startswith("https://www.hipo.ro/")
        assert job.title
        assert job.company
        assert "Cluj" in job.location


def test_hipo_city_segment_mapping():
    assert hipo.city_segment("cluj") == "Cluj-Napoca"
    assert hipo.city_segment("Bucharest") == "Bucuresti"
    assert hipo.city_segment("romania") == "Toate-Orasele"
    assert hipo.city_segment("london") is None


@pytest.mark.asyncio
async def test_hipo_search_skips_unsupported_cities():
    scraper = hipo.HipoScraper()
    assert await scraper.search(SearchQuery(keyword="x", location="berlin")) == []


def test_hipo_detail_marks_events():
    page = (
        '<script type="application/ld+json">'
        '{"@type": "JobPosting", "title": "CV Clinic", "employmentType": "eveniment"}'
        "</script>"
    )
    assert hipo._extract_detail(page)["not_a_job"] is True


@respx.mock
async def test_hipo_paginates_and_drops_events(monkeypatch):
    monkeypatch.setattr(hipo, "_cache", {})
    details.clear_cache()

    def card(n: int, title: str) -> str:
        return (
            f'<a title="{title}" class="job-title" href="/locuri-de-munca/locuri_de_munca/{n}/X/t">'
            f'</a><p class="company-name"><span>Acme</span>'
        )

    base = hipo.LISTING_URL.format(city="Cluj-Napoca")
    respx.get(base).mock(return_value=httpx.Response(200, text=card(1, "Java Developer")))
    respx.get(base + "/2").mock(return_value=httpx.Response(
        200, text=card(2, "Joburi in Tech - Inscrieri gratuite"),
    ))
    respx.get(base + "/3").mock(return_value=httpx.Response(200, text=card(2, "repeat")))
    respx.get(url__regex=r".*/locuri_de_munca/1/.*").mock(return_value=httpx.Response(
        200, text='<script type="application/ld+json">'
        '{"@type": "JobPosting", "description": "Java", "employmentType": "full-time"}</script>',
    ))
    respx.get(url__regex=r".*/locuri_de_munca/2/.*").mock(return_value=httpx.Response(
        200, text='<script type="application/ld+json">'
        '{"@type": "JobPosting", "employmentType": "eveniment"}</script>',
    ))

    jobs = await hipo.HipoScraper().search(SearchQuery(keyword="java", location="cluj"))

    assert [j.title for j in jobs] == ["Java Developer"]
    assert jobs[0].description == "Java"
    details.clear_cache()


@respx.mock
async def test_feed_sources_back_off_after_a_failure(monkeypatch):
    """An outage must not become one request per keyword term."""
    jobicy.clear_cache()
    route = respx.get(jobicy.API_URL).mock(return_value=httpx.Response(429))
    query = SearchQuery(keyword="python", location="remote", max_age_days=3650)
    for _ in range(5):
        assert await jobicy.JobicyScraper().search(query) == []
    assert route.call_count == 1
    jobicy.clear_cache()

    monkeypatch.setattr(remoteok, "_cache", None)
    feed = respx.get(remoteok.API_URL).mock(return_value=httpx.Response(503))
    for _ in range(5):
        assert await remoteok.RemoteOKScraper().search(query) == []
    assert feed.call_count == 1
    monkeypatch.setattr(remoteok, "_cache", None)
