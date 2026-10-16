"""Guards for the JobSpy boundary.

Every pipeline test stubs `scrape_jobs_raw`, so nothing else would notice
if gosha/scraper.py and the installed python-jobspy stopped agreeing.
scrape_jobs takes **kwargs, so a disagreement is silent: `proxy=` (renamed
`proxies=` upstream) was dropped on every call, and requests went direct
instead of through the SSH tunnels. Without `country_indeed`, Romanian
searches ran against indeed.com and came back empty.
"""

from __future__ import annotations

import inspect

import pandas as pd
import pytest

from gosha import scraper
from gosha.scraper import glassdoor_country, indeed_country, jobspy_kwargs

jobspy = pytest.importorskip("jobspy")


def test_installed_jobspy_accepts_every_kwarg_we_pass():
    params = inspect.signature(jobspy.scrape_jobs).parameters
    named = {
        name for name, p in params.items()
        if p.kind is not inspect.Parameter.VAR_KEYWORD
    }
    kwargs = jobspy_kwargs(
        ["indeed", "linkedin"], "python developer", "Cluj-Napoca, Romania",
        14, "socks5://127.0.0.1:1080", "romania",
    )
    unknown = sorted(set(kwargs) - named)
    assert not unknown, (
        f"python-jobspy would silently ignore {unknown}; "
        "check the pin in requirements.txt / constraints.txt"
    )


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Cluj-Napoca, Romania", "romania"),
        ("Romania", "romania"),
        ("London, United Kingdom", "united kingdom"),
        ("Prague, Czech Republic", "czech republic"),
        ("Remote", None),
        ("Europe", None),
        ("Springfield", None),
        ("", None),
    ],
)
def test_indeed_country(location, expected):
    assert indeed_country(location) == expected


def test_glassdoor_country_skips_countries_glassdoor_does_not_serve():
    # jobspy raises "Glassdoor is not available for ROMANIA"
    assert glassdoor_country("Cluj-Napoca, Romania") is None
    assert glassdoor_country("Berlin, Germany") == "germany"


def test_kwargs_omit_country_when_unknown():
    kwargs = jobspy_kwargs(["indeed"], "java", "Remote", 7, None, None)
    assert "country_indeed" not in kwargs
    assert "linkedin_fetch_description" not in kwargs
    assert kwargs["hours_old"] == 7 * 24


class _Tunnels:
    def active_proxies(self) -> list[str]:
        return ["socks5://127.0.0.1:1080"]


async def test_scrape_single_routes_through_proxy_with_country(monkeypatch):
    calls: list[dict] = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame([{"job_url": f"https://x/{kwargs['site_name'][0]}"}])

    monkeypatch.setattr(scraper, "scrape_jobs", fake_scrape_jobs)

    df = await scraper._scrape_single(
        _Tunnels(), "python developer", "Cluj-Napoca, Romania", 14,
        ["indeed", "linkedin", "glassdoor"],
    )

    assert len(df) == 3
    by_board = {c["site_name"][0]: c for c in calls}
    assert sorted(by_board) == ["glassdoor", "indeed", "linkedin"]
    assert all(len(c["site_name"]) == 1 for c in calls)
    for board in ("indeed", "linkedin"):
        main = by_board[board]
        assert main["proxies"] == "socks5://127.0.0.1:1080"
        assert main["country_indeed"] == "romania"
        assert main["location"] == "Cluj-Napoca, Romania"
        assert "proxy" not in main
    gd = by_board["glassdoor"]
    assert gd["site_name"] == ["glassdoor"]
    assert gd["location"] == "Cluj-Napoca"
    assert gd["proxies"] == "socks5://127.0.0.1:1080"
    assert "country_indeed" not in gd


class _NoTunnels:
    def active_proxies(self) -> list[str]:
        return []


async def test_no_tunnels_and_no_direct_skips_the_search(monkeypatch):
    monkeypatch.delenv("SCRAPE_DIRECT", raising=False)
    monkeypatch.setattr(
        scraper, "scrape_jobs", lambda **_: pytest.fail("must not scrape")
    )
    df = await scraper._scrape_single(
        _NoTunnels(), "python", "Cluj-Napoca, Romania", 7, ["indeed"],
    )
    assert df.empty


async def test_direct_scrape_uses_the_server_ip_when_no_tunnel_is_up(monkeypatch):
    monkeypatch.setenv("SCRAPE_DIRECT", "true")
    calls: list[dict] = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame([{"job_url": "https://x/1"}])

    monkeypatch.setattr(scraper, "scrape_jobs", fake_scrape_jobs)
    df = await scraper._scrape_single(
        _NoTunnels(), "python", "Cluj-Napoca, Romania", 7, ["indeed"],
    )
    assert len(df) == 1
    assert calls[0]["proxies"] is None


async def test_direct_is_a_fallback_route_when_the_tunnel_fails(monkeypatch):
    monkeypatch.setenv("SCRAPE_DIRECT", "1")
    monkeypatch.setattr(scraper.random, "shuffle", lambda routes: None)  # tunnel first
    seen: list[object] = []

    def fake_scrape_jobs(**kwargs):
        seen.append(kwargs["proxies"])
        if kwargs["proxies"]:
            raise ConnectionError("tunnel blocked")
        return pd.DataFrame([{"job_url": "https://x/1"}])

    monkeypatch.setattr(scraper, "scrape_jobs", fake_scrape_jobs)
    df = await scraper._scrape_single(
        _Tunnels(), "python", "Cluj-Napoca, Romania", 7, ["indeed"],
    )
    assert len(df) == 1
    assert seen == ["socks5://127.0.0.1:1080", None]


@pytest.mark.parametrize("value", ["", "false", "0", "no"])
def test_scrape_direct_is_off_unless_enabled(monkeypatch, value):
    monkeypatch.setenv("SCRAPE_DIRECT", value)
    assert scraper.scrape_direct_enabled() is False


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("cluj", ["indeed", "linkedin", "glassdoor"]),
        ("romania", ["indeed", "linkedin", "glassdoor"]),
        # "Remote" was a US place to Indeed/Glassdoor and none to LinkedIn.
        ("remote", []),
        ("Remote", []),
        ("worldwide", []),
        # Only LinkedIn knows a continent.
        ("europe", ["linkedin"]),
        ("eu", ["linkedin"]),
    ],
)
def test_jobspy_boards_for(location, expected):
    assert scraper.jobspy_boards_for(location, ["indeed", "linkedin", "glassdoor"]) == expected


async def test_remote_search_is_not_sent_to_jobspy(monkeypatch):
    monkeypatch.setattr(
        scraper, "scrape_jobs", lambda **_: pytest.fail("must not scrape"),
    )
    df = await scraper.scrape_jobs_raw(_Tunnels(), "python developer", "remote", 14)
    assert df.empty


async def test_europe_search_only_asks_linkedin(monkeypatch):
    monkeypatch.setattr(scraper, "DELAY_BETWEEN_SEARCHES", 0)
    calls: list[dict] = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame([{"job_url": "https://x/1", "site": "linkedin"}])

    monkeypatch.setattr(scraper, "scrape_jobs", fake_scrape_jobs)
    df = await scraper.scrape_jobs_raw(_Tunnels(), "python developer", "europe", 14)
    assert len(df) == 1
    assert [c["site_name"] for c in calls] == [["linkedin"]]
    assert calls[0]["location"] == "Europe"


# LinkedIn's "Seniority level" criterion, as python-jobspy 1.3.0 lowercases it
# into job_level (only when fetch_description reads the job page).
@pytest.mark.parametrize(
    ("job_level", "expected"),
    [
        ("internship", "intern"),
        ("entry level", "entry"),
        ("Entry level", "entry"),
        # LinkedIn's one bucket for every experienced role; "mid" because the
        # label outranks the description's years in seniority.posting_level.
        ("mid-senior level", "mid"),
        ("director", "senior"),
        ("executive", "senior"),
        # "Associate" is used for both graduate and 2-3 year roles; the title
        # and description rules decide those.
        ("associate", None),
        ("not applicable", None),
        (None, None),
        (float("nan"), None),
        ("", None),
    ],
)
def test_experience_level_from_job_level(job_level, expected):
    assert scraper.experience_level_from_job_level(job_level) == expected


async def test_scrape_jobs_raw_carries_linkedin_seniority_as_experience_level(monkeypatch):
    monkeypatch.setattr(scraper, "DELAY_BETWEEN_SEARCHES", 0)

    def fake_scrape_jobs(**kwargs):
        return pd.DataFrame([
            {"job_url": "https://li/1", "site": "linkedin", "job_level": "internship"},
            {"job_url": "https://li/2", "site": "linkedin", "job_level": "mid-senior level"},
            {"job_url": "https://in/3", "site": "indeed", "job_level": None},
        ])

    monkeypatch.setattr(scraper, "scrape_jobs", fake_scrape_jobs)
    df = await scraper.scrape_jobs_raw(_Tunnels(), "python developer", "cluj", 14, ["linkedin"])
    levels = dict(zip(df["job_url"], df["experience_level"]))
    assert levels["https://li/1"] == "intern"
    assert levels["https://li/2"] == "mid"
    assert levels["https://in/3"] is None


async def test_scrape_jobs_raw_without_job_level_column(monkeypatch):
    monkeypatch.setattr(scraper, "DELAY_BETWEEN_SEARCHES", 0)
    monkeypatch.setattr(
        scraper, "scrape_jobs",
        lambda **_: pd.DataFrame([{"job_url": "https://in/1", "site": "indeed"}]),
    )
    df = await scraper.scrape_jobs_raw(_Tunnels(), "python developer", "cluj", 14, ["indeed"])
    assert "experience_level" not in df.columns or df["experience_level"].isna().all()


async def test_linkedin_internship_label_is_stored_on_the_job(patched_db, monkeypatch):
    """End to end: the label reaches Job.experience_level through upsert_jobs."""
    from gosha.pipeline import upsert_jobs

    monkeypatch.setattr(scraper, "DELAY_BETWEEN_SEARCHES", 0)
    monkeypatch.setattr(
        scraper, "scrape_jobs",
        lambda **_: pd.DataFrame([{
            "job_url": "https://www.linkedin.com/jobs/view/1", "site": "linkedin",
            "title": "Software Engineer", "company": "Acme",
            "location": "Cluj-Napoca, Romania", "job_level": "internship",
        }]),
    )
    df = await scraper.scrape_jobs_raw(_Tunnels(), "software engineer", "cluj", 14, ["linkedin"])
    (job,) = await upsert_jobs(df)
    assert job.experience_level == "intern"


def test_linkedin_alone_asks_for_more_results():
    """LinkedIn showed 90 results for "python developer" in Cluj (14 days)
    where we kept 30; 51 of the other 60 were title-relevant (2026-10-16)."""
    linkedin = jobspy_kwargs(["linkedin"], "python developer", "Cluj", 14, None, None)
    indeed = jobspy_kwargs(["indeed"], "python developer", "Cluj", 14, None, None)
    assert linkedin["results_wanted"] == scraper.LINKEDIN_RESULTS_PER_TERM
    assert indeed["results_wanted"] == scraper.RESULTS_PER_TERM
    mixed = jobspy_kwargs(["indeed", "linkedin"], "python developer", "Cluj", 14, None, None)
    assert mixed["results_wanted"] == scraper.RESULTS_PER_TERM


async def test_a_failing_board_is_retried_alone_on_the_next_route(monkeypatch):
    """Indeed raising on the tunnel must not throw away LinkedIn's rows, nor
    send LinkedIn (which 429s) a second time: only Indeed tries the next route."""
    monkeypatch.setenv("SCRAPE_DIRECT", "1")
    monkeypatch.setattr(scraper.random, "shuffle", lambda routes: None)  # tunnel first
    seen: list[tuple] = []

    def fake_scrape_jobs(**kwargs):
        seen.append((kwargs["site_name"][0], kwargs["proxies"]))
        if kwargs["proxies"] and kwargs["site_name"] == ["indeed"]:
            raise ConnectionError("tunnel blocked")
        return pd.DataFrame([{"job_url": f"https://x/{kwargs['site_name'][0]}"}])

    monkeypatch.setattr(scraper, "scrape_jobs", fake_scrape_jobs)
    df = await scraper._scrape_single(
        _Tunnels(), "python", "Cluj-Napoca, Romania", 7, ["indeed", "linkedin"],
    )
    assert sorted(df["job_url"]) == ["https://x/indeed", "https://x/linkedin"]
    tunnel = "socks5://127.0.0.1:1080"
    assert len(seen) == 3
    assert set(seen) == {("indeed", tunnel), ("linkedin", tunnel), ("indeed", None)}


async def test_rows_of_working_boards_survive_when_a_board_fails_everywhere(
    monkeypatch, caplog,
):
    monkeypatch.setenv("SCRAPE_DIRECT", "1")
    seen: list[tuple] = []

    def fake_scrape_jobs(**kwargs):
        seen.append((kwargs["site_name"][0], kwargs["proxies"]))
        if kwargs["site_name"] == ["glassdoor"]:
            raise RuntimeError("403")
        return pd.DataFrame([{"job_url": f"https://x/{kwargs['site_name'][0]}"}])

    monkeypatch.setattr(scraper, "scrape_jobs", fake_scrape_jobs)
    with caplog.at_level("WARNING", logger="gosha.scraper"):
        df = await scraper._scrape_single(
            _Tunnels(), "python", "Cluj-Napoca, Romania", 7,
            ["indeed", "linkedin", "glassdoor"],
        )
    assert sorted(df["job_url"]) == ["https://x/indeed", "https://x/linkedin"]
    assert [b for b, _ in seen].count("glassdoor") == 2  # tunnel, then direct
    assert [b for b, _ in seen].count("linkedin") == 1
    assert "glassdoor" in caplog.text and "403" in caplog.text


class _FakeLinkedIn:
    """Stands in for jobspy.linkedin.LinkedIn: counts page fetches."""

    def __init__(self, details: dict[str, dict]):
        self.details = details
        self.fetched: list[str] = []

    def _fetch_details(self, job_id: str) -> dict:
        self.fetched.append(job_id)
        return dict(self.details.get(job_id, {}))


def test_linkedin_detail_pages_are_fetched_once_per_day(monkeypatch):
    """A broad keyword's terms return the same LinkedIn jobs: the 08:26 cycle on
    2026-10-16 got 1,425 JobSpy rows for 343 distinct LinkedIn jobs, each row a
    job-page request, and LinkedIn answered 429 to 196 of them."""
    monkeypatch.setattr(scraper, "_linkedin_details", {})
    clock = [1000.0]
    monkeypatch.setattr(scraper.time, "monotonic", lambda: clock[0])
    li = _FakeLinkedIn({"1": {"description": "Build things", "job_level": "internship"}})
    fetch = scraper._cached_linkedin_details(_FakeLinkedIn._fetch_details)

    assert fetch(li, "1")["description"] == "Build things"
    assert fetch(li, "1")["job_level"] == "internship"
    assert li.fetched == ["1"]

    clock[0] += scraper.LINKEDIN_DETAIL_TTL_SECONDS + 1
    fetch(li, "1")
    assert li.fetched == ["1", "1"]


def test_failed_linkedin_detail_pages_are_retried(monkeypatch):
    """A 429 or redirect comes back as {}: not cached, so the next term retries."""
    monkeypatch.setattr(scraper, "_linkedin_details", {})
    li = _FakeLinkedIn({})
    fetch = scraper._cached_linkedin_details(_FakeLinkedIn._fetch_details)
    assert fetch(li, "2") == {}
    assert fetch(li, "2") == {}
    assert li.fetched == ["2", "2"]


def test_cached_linkedin_details_are_copies(monkeypatch):
    monkeypatch.setattr(scraper, "_linkedin_details", {})
    li = _FakeLinkedIn({"3": {"description": "x"}})
    fetch = scraper._cached_linkedin_details(_FakeLinkedIn._fetch_details)
    fetch(li, "3")["description"] = "changed"
    assert fetch(li, "3")["description"] == "x"


def test_linkedin_detail_cache_is_installed_on_the_pinned_jobspy():
    """The cache wraps a private jobspy method; fail loudly if 1.3.0's name moves."""
    from jobspy.linkedin import LinkedIn

    assert scraper.install_linkedin_detail_cache() is True
    assert getattr(LinkedIn._fetch_details, "gosha_cached", False)
    scraper.install_linkedin_detail_cache()  # idempotent: never wraps twice
    assert not getattr(LinkedIn._fetch_details.__wrapped__, "gosha_cached", False)
