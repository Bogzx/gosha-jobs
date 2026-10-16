"""Baseline catalog: searches run every cycle whatever the subscriptions are.

The web feed ranks every stored posting, and today's 22 subscriptions are all
"computer science internship" searches, mostly in Cluj: a junior user, or
anyone in Bucharest, found almost nothing. The baseline is native boards
only — no LinkedIn, Indeed or Glassdoor request (LinkedIn already answers
110-372 job pages and 27-112 searches per cycle with 429).
"""

from __future__ import annotations

import pytest

from gosha import scraper
from gosha.filters import relevance_predicate
from gosha.scrapers import bestjobs, ejobs, hipo, jobicy
from gosha.scrapers.base import SearchQuery
from gosha.scrapers.registry import get_extra_scrapers


@pytest.fixture
def baseline_on(monkeypatch):
    monkeypatch.setenv("SCRAPE_BASELINE", "true")


@pytest.mark.parametrize("value", [None, "", "false", "0", "no", "off", "nope"])
def test_baseline_is_off_unless_enabled(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SCRAPE_BASELINE", raising=False)
    else:
        monkeypatch.setenv("SCRAPE_BASELINE", value)
    assert scraper.baseline_combos(0) == []


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", " TRUE "])
def test_baseline_switches_on(monkeypatch, value):
    monkeypatch.setenv("SCRAPE_BASELINE", value)
    assert scraper.baseline_combos(0)


def test_baseline_is_native_only(baseline_on):
    combos = scraper.baseline_combos(0)
    assert [c.location for c in combos] == list(scraper.BASELINE_LOCATIONS)
    for combo in combos:
        assert combo.boards == ()  # no JobSpy request at all
        assert combo.keyword == "junior developer"
        assert combo.filter_keywords == ("junior developer",)
        assert combo.max_age_days == 14


def test_baseline_is_pure_and_tier_a_ignores_the_cycle(baseline_on):
    assert scraper.baseline_combos(0) == scraper.baseline_combos(0) == scraper.baseline_combos(7)


def test_every_baseline_location_is_served_by_a_native_source(baseline_on):
    sources = get_extra_scrapers()
    for combo in scraper.baseline_combos(0):
        query = SearchQuery(keyword=combo.keyword, location=combo.location)
        assert [s.name for s in sources if s.applies_to(query)], combo.location


def test_the_baseline_keyword_keeps_student_level_tech_titles():
    keep = relevance_predicate(["junior developer"])
    for title in (
        "Junior Java Developer", "Software Engineer Intern", "Stagiar IT",
        "QA Engineer", "Data Analyst", "DevOps Engineer", "Frontend Developer",
        "Machine Learning Engineer", "Embedded Software Engineer",
    ):
        assert keep(title), title
    for title in ("Senior Java Developer", "Contabil", "Agent vanzari"):
        assert not keep(title), title


def _worst_case(location: str, name: str) -> tuple[int, int]:
    """(list requests, detail fetches) one search can make, cold caches."""
    if name == "ejobs":
        return ejobs.MAX_PAGES, ejobs.MAX_DETAIL_FETCHES
    if name == "hipo":
        return hipo.MAX_PAGES, hipo.MAX_DETAIL_FETCHES
    if name == "bestjobs":
        return len(bestjobs.search_locations(location)), bestjobs.MAX_DETAIL_FETCHES
    if name == "remoteok":
        return 1, 0
    if name == "jobicy":
        return jobicy.MAX_PAGES, 0
    raise AssertionError(f"new source {name!r}: add its cost here and to the budget")


def test_baseline_request_budget(baseline_on):
    """The budget constants are the worst case of one cycle with cold caches;
    changing a location, a source or an adapter's cap must update them."""
    lists = details = 0
    for combo in scraper.baseline_combos(0):
        query = SearchQuery(keyword=combo.keyword, location=combo.location)
        for source in get_extra_scrapers():
            if source.applies_to(query):
                n_list, n_detail = _worst_case(combo.location, source.name)
                lists += n_list
                details += n_detail
    lists += 1  # eJobs' /cities, once per process
    assert lists == scraper.BASELINE_MAX_LIST_REQUESTS
    assert details == scraper.BASELINE_MAX_DETAIL_FETCHES
