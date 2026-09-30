"""Tests for scripts/smoke_scrapers.py — the daily live probe.

Issue #2 collected 42 identical "a source returns no jobs" reports while
every board was fine: the script could not import `gosha` when run the
way the workflow runs it. These tests pin the harness itself, offline,
against fixtures recorded from the live boards.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from gosha.scrapers import bestjobs
from gosha.scrapers.base import RawJob, SearchQuery
from scripts import smoke_scrapers as smoke

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"


def test_script_imports_when_run_as_a_file_from_anywhere(tmp_path):
    """The exact invocation that failed every day since 2026-08-18."""
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "smoke_scrapers.py"), "--help"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--no-jobspy" in proc.stdout


class FakeScraper:
    def __init__(self, name: str, jobs: list[RawJob]) -> None:
        self.name = name
        self._jobs = jobs

    async def search(self, query: SearchQuery) -> list[RawJob]:
        return list(self._jobs)


def _bestjobs_live_sample() -> list[RawJob]:
    """Recorded 2026-09-30; 1 of 5 postings is anonymous (empty companyName)."""
    payload = json.loads(
        (FIXTURES / "bestjobs_with_anonymous.json").read_text(encoding="utf-8")
    )
    return bestjobs._parse(payload, SearchQuery("software engineer", "cluj"))


async def test_one_anonymous_posting_does_not_fail_a_healthy_source():
    jobs = _bestjobs_live_sample()
    assert sum(job.company == "Unknown" for job in jobs) == 1

    result = await smoke.probe(FakeScraper("bestjobs", jobs))

    assert result.healthy, result.problems


async def test_mostly_companyless_sample_fails():
    jobs = [
        RawJob(url=f"https://example.test/{i}", title="Dev", source="bestjobs")
        for i in range(5)
    ]
    result = await smoke.probe(FakeScraper("bestjobs", jobs))

    assert not result.healthy
    assert any("no company" in p for p in result.problems)


async def test_zero_jobs_fails():
    result = await smoke.probe(FakeScraper("hipo", []))
    assert not result.healthy
    assert any("0 jobs" in p for p in result.problems)


async def test_result_file_names_failing_sources(tmp_path, monkeypatch):
    good = FakeScraper("ejobs", _bestjobs_live_sample()[:2])
    for job in good._jobs:
        job.source = "ejobs"
    monkeypatch.setattr(
        smoke, "get_extra_scrapers", lambda: [good, FakeScraper("hipo", [])],
    )
    out = tmp_path / "result.json"

    code = await smoke.main([], jobspy=False, result_file=str(out))

    assert code == 1
    assert json.loads(out.read_text()) == {
        "failing": ["hipo"], "advisory_failing": [], "probed": ["ejobs", "hipo"],
    }


def _jobspy_frame(site: str) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "site": site,
            "job_url": f"https://ro.{site}.test/{i}",
            "title": "Software Engineer",
            "company": "Acme",
            "location": "Cluj-Napoca, Romania",
        }
        for i in range(3)
    ])


async def test_jobspy_probe_uses_production_arguments(monkeypatch):
    jobspy = pytest.importorskip("jobspy")
    calls: list[dict] = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        return _jobspy_frame(kwargs["site_name"][0])

    monkeypatch.setattr(jobspy, "scrape_jobs", fake_scrape_jobs)

    result = await smoke.probe_jobspy("indeed", advisory=False)

    assert result.healthy, result.problems
    (kwargs,) = calls
    assert kwargs["country_indeed"] == "romania"
    assert kwargs["location"] == "Cluj-Napoca, Romania"
    assert kwargs["results_wanted"] == smoke.JOBSPY_RESULTS
    assert kwargs["proxies"] is None


async def test_advisory_jobspy_failure_does_not_fail_the_run(
    tmp_path, monkeypatch,
):
    jobspy = pytest.importorskip("jobspy")

    def fake_scrape_jobs(**kwargs):
        if kwargs["site_name"] == ["linkedin"]:
            return pd.DataFrame()  # throttled datacenter IP
        return _jobspy_frame("indeed")

    monkeypatch.setattr(jobspy, "scrape_jobs", fake_scrape_jobs)
    monkeypatch.setattr(smoke, "get_extra_scrapers", lambda: [])
    out = tmp_path / "result.json"

    code = await smoke.main([], jobspy=True, result_file=str(out))

    assert code == 0
    body = json.loads(out.read_text())
    assert body["failing"] == []
    assert body["advisory_failing"] == ["linkedin"]
