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
        "failing": ["hipo"], "advisory_failing": [], "blocked": [],
        "probed": ["ejobs", "hipo"],
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


async def test_glassdoor_probe_uses_the_city_only_location(monkeypatch):
    jobspy = pytest.importorskip("jobspy")
    calls: list[dict] = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        return _jobspy_frame(kwargs["site_name"][0])

    monkeypatch.setattr(jobspy, "scrape_jobs", fake_scrape_jobs)

    result = await smoke.probe_jobspy("glassdoor", advisory=True)

    assert result.healthy, result.problems
    (kwargs,) = calls
    # As production sends it: Glassdoor has no Romanian domain, so no
    # country argument, and the location without ", Romania".
    assert kwargs["location"] == "Cluj-Napoca"
    assert "country_indeed" not in kwargs
    assert kwargs["fetch_description"] is False


def test_company_presence_is_judged_over_all_results():
    """A few anonymous postings ranked first must not fail a source."""
    from gosha.scrapers.base import RawJob

    result = smoke.SourceResult(name="bestjobs")
    jobs = [
        RawJob(url=f"https://b.test/{i}", title="Dev", company="Unknown", source="bestjobs")
        for i in range(5)
    ] + [
        RawJob(url=f"https://b.test/n{i}", title="Dev", company="Acme", source="bestjobs")
        for i in range(95)
    ]
    smoke._record_jobs(result, jobs)
    assert smoke._company_problem(result) is None

    broken = smoke.SourceResult(name="bestjobs")
    smoke._record_jobs(broken, [
        RawJob(url=f"https://b.test/{i}", title="Dev", source="bestjobs")
        for i in range(10)
    ])
    assert smoke._company_problem(broken) is not None


async def test_advisory_jobspy_failure_does_not_fail_the_run(
    tmp_path, monkeypatch,
):
    jobspy = pytest.importorskip("jobspy")

    def fake_scrape_jobs(**kwargs):
        if kwargs["site_name"] == ["linkedin"]:
            return pd.DataFrame()  # throttled datacenter IP
        return _jobspy_frame(kwargs["site_name"][0])

    monkeypatch.setattr(jobspy, "scrape_jobs", fake_scrape_jobs)
    monkeypatch.setattr(smoke, "get_extra_scrapers", lambda: [])
    out = tmp_path / "result.json"

    code = await smoke.main([], jobspy=True, result_file=str(out))

    assert code == 0
    body = json.loads(out.read_text())
    assert body["failing"] == []
    assert body["advisory_failing"] == ["linkedin"]



# ── Refused by the board (403/429 to GitHub's runner) vs really broken ──

import httpx  # noqa: E402


class HttpScraper:
    """An adapter shaped like the real ones: plain httpx, HTTP errors
    swallowed into an empty list (adapters never raise)."""

    def __init__(self, name: str, statuses: list[int], raise_on: int | None = None):
        self.name = name
        self.statuses = list(statuses)  # one per query, in order
        self.raise_on = raise_on
        self.calls = 0

    async def search(self, query: SearchQuery) -> list[RawJob]:
        status = self.statuses[self.calls % len(self.statuses)]
        self.calls += 1
        if self.raise_on is not None and self.calls == self.raise_on:
            raise RuntimeError("parse error: unexpected payload")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"jobs": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resp = await client.get("https://api.board.test/search")
        if resp.status_code != 200:
            return []  # the adapter logs a warning and gives up
        return []  # 200 but nothing parsed


@pytest.mark.parametrize("status", [403, 429])
async def test_refused_on_every_probe_is_blocked_not_failing(tmp_path, monkeypatch, status):
    monkeypatch.setattr(smoke, "get_extra_scrapers", lambda: [HttpScraper("ejobs", [status])])
    out = tmp_path / "result.json"

    code = await smoke.main([], jobspy=False, result_file=str(out))

    assert code == 0  # not paged, issue not opened (the workflow reads `failing`)
    body = json.loads(out.read_text())
    assert body["failing"] == [] and body["blocked"] == ["ejobs"]
    assert body["advisory_failing"] == []


async def test_blocked_source_is_reported_in_the_log(capsys):
    result = await smoke.probe(HttpScraper("ejobs", [403]))
    assert result.blocked and not result.healthy
    smoke._report([result])
    out = capsys.readouterr()
    assert "[BLKD] ejobs" in out.out and "blocked from CI" in out.out
    assert "HTTP 403 on all" in out.err


async def test_zero_jobs_with_200s_still_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "get_extra_scrapers", lambda: [HttpScraper("ejobs", [200])])
    out = tmp_path / "result.json"
    assert await smoke.main([], jobspy=False, result_file=str(out)) == 1
    body = json.loads(out.read_text())
    assert body["failing"] == ["ejobs"] and body["blocked"] == []


async def test_refused_on_only_some_probes_still_fails():
    result = await smoke.probe(HttpScraper("ejobs", [403, 200, 403]))
    assert not result.blocked and not result.healthy
    assert any("0 jobs" in p for p in result.problems)


async def test_an_exception_is_breakage_even_among_refusals():
    result = await smoke.probe(HttpScraper("ejobs", [403], raise_on=2))
    assert not result.blocked
    assert any("parse error" in p for p in result.problems)


async def test_jobspy_refusal_is_blocked_other_errors_fail(monkeypatch):
    jobspy = pytest.importorskip("jobspy")

    def refused(**kwargs):
        raise Exception("bad response with status code: 403")

    monkeypatch.setattr(jobspy, "scrape_jobs", refused)
    result = await smoke.probe_jobspy("indeed", advisory=False)
    assert result.blocked

    def broken(**kwargs):
        raise KeyError("jobCard")

    monkeypatch.setattr(jobspy, "scrape_jobs", broken)
    result = await smoke.probe_jobspy("indeed", advisory=False)
    assert not result.blocked and not result.healthy


async def test_browser_fallback_turns_a_tls_refusal_into_jobs(monkeypatch):
    """With SCRAPE_BROWSER_FALLBACK=true (the workflow sets it), a 403 to
    Python's handshake is retried as Chrome; the smoke sees the 200."""
    from gosha.scrapers import base

    monkeypatch.setenv("SCRAPE_BROWSER_FALLBACK", "true")
    monkeypatch.setattr(base, "_browser_hosts", set())
    monkeypatch.setattr(
        base.httpx, "AsyncHTTPTransport",
        lambda: httpx.MockTransport(lambda r: httpx.Response(403)),
    )

    async def chrome(request):
        return httpx.Response(200, json={"title": "Dev"}, request=request)

    monkeypatch.setattr(base, "_browser_get", chrome)

    class Adapter:
        name = "bestjobs"

        async def search(self, query):
            async with base.http_client() as client:
                resp = await client.get("https://api.bestjobs.test/jobs")
            if resp.status_code != 200:
                return []
            return [RawJob(url="https://bestjobs.test/1", title=resp.json()["title"],
                           company="Acme", source="bestjobs")]

    result = await smoke.probe(Adapter())
    assert result.healthy and not result.blocked, result.problems


def test_workflow_runs_the_smoke_with_the_browser_fallback():
    workflow = (REPO / ".github/workflows/scraper-smoke.yml").read_text()
    probe_step = workflow.split("Probe every source", 1)[1].split("- name:", 1)[0]
    assert 'SCRAPE_BROWSER_FALLBACK: "true"' in probe_step
