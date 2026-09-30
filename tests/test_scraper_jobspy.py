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

    assert len(df) == 2
    main, gd = calls
    assert main["site_name"] == ["indeed", "linkedin"]
    assert main["proxies"] == "socks5://127.0.0.1:1080"
    assert main["country_indeed"] == "romania"
    assert main["location"] == "Cluj-Napoca, Romania"
    assert "proxy" not in main
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
