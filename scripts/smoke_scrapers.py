"""Live smoke test: run every adapter against the real endpoints and assert.

This is the outside-in half of the scraper monitoring (the inside-out half
is gosha/scrape_health.py, which watches production yields). A board can
change its markup or API at any time; the unit tests run against recorded
fixtures and will happily stay green while the live site returns nothing.

Exit code 0 when every gating source returned usable jobs, 1 when one did
not, so CI can gate on it. A crash of the harness itself also exits 1 but
writes no --result-file, which is how the workflow tells "a board broke"
from "this script broke" (the latter is what issue #2 was, 42 times). Run
manually from the repo root with:

    python scripts/smoke_scrapers.py              # everything
    python scripts/smoke_scrapers.py ejobs hipo   # a subset
    python scripts/smoke_scrapers.py --no-jobspy  # native adapters only

A source is considered healthy when at least one of the probe queries
returns at least one job whose url/title/company survive validation. Some
boards legitimately have nothing for one narrow query, which is why every
adapter gets several.

Each adapter is only asked the probe queries its applies_to() accepts
(RemoteOK and Jobicy serve remote searches, Hipo Romanian cities), the same
rule production uses.

JobSpy (Indeed, LinkedIn, Glassdoor) is probed once per board, directly —
production goes through SSH tunnels this runner does not have — with the
exact arguments production builds (gosha.scraper.jobspy_kwargs; Glassdoor
gets the city-only location production gives it). Indeed gates. LinkedIn
and Glassdoor are ADVISORY: they throttle or block datacenter IPs such as
GitHub's, so a failure there is reported but does not fail the run or
open an issue.

A source that is REFUSED on every probe (HTTP 403 or 429 on each query,
no job, no exception) is reported as "blocked from CI (advisory)": the
board turns GitHub's runner away (eJobs and Glassdoor do, 2026-10), which
says nothing about production, whose own scrape-health alerts
(gosha/scrape_health.py) cover real outages. It does not fail the run or
touch the issue. Zero jobs with 200s, bad fields and exceptions still fail.
The workflow runs with SCRAPE_BROWSER_FALLBACK=true, as production does,
so a refusal aimed at Python's TLS fingerprint is retried as Chrome first.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# `python scripts/smoke_scrapers.py` puts scripts/ on sys.path, not the
# repo root, so `import gosha` failed before a single board was contacted.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from gosha.scrapers.base import RawJob, SearchQuery  # noqa: E402
from gosha.scrapers.registry import get_extra_scrapers  # noqa: E402

PROBE_QUERIES = [
    SearchQuery(keyword="software engineer", location="cluj", max_age_days=30),
    SearchQuery(keyword="python developer", location="remote", max_age_days=30),
    SearchQuery(keyword="java", location="bucharest", max_age_days=30),
]

# Per-adapter timeout. A hung board must fail the check, not the runner.
QUERY_TIMEOUT = 90.0

# One small JobSpy query per board: enough to prove the library still
# parses the site, few enough requests to stay polite.
JOBSPY_PROBE = SearchQuery(
    keyword="software engineer", location="cluj", max_age_days=30,
)
JOBSPY_RESULTS = 5
JOBSPY_BOARDS = {"indeed": False, "linkedin": True, "glassdoor": True}  # board -> advisory

# How many sampled jobs are validated per query.
SAMPLE_SIZE = 5

# "Go away" answers: the board refuses this client, not a broken adapter.
REFUSED_STATUSES = frozenset({403, 429})
_REFUSED_TEXT = re.compile(r"\b(?:403|429)\b")

# Status codes of the HTTP responses the current probe query received
# (after redirects and the browser fallback). The adapters swallow HTTP
# errors and return [], so this is the only way to tell a refusal from a
# parser that found nothing.
_statuses: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "smoke_statuses", default=None,
)


def _install_status_hook() -> None:
    """Record every httpx response status into `_statuses` (idempotent)."""
    if getattr(httpx.AsyncClient.send, "_smoke_hook", False):
        return
    original = httpx.AsyncClient.send

    async def send(self, request, **kwargs):
        response = await original(self, request, **kwargs)
        seen = _statuses.get()
        if seen is not None:
            seen.append(response.status_code)
        return response

    send._smoke_hook = True  # type: ignore[attr-defined]
    httpx.AsyncClient.send = send  # type: ignore[method-assign]


@dataclass
class SourceResult:
    name: str
    advisory: bool = False
    # Refused (403/429) on every probe: reported, never failing (see top).
    blocked: bool = False
    jobs_found: int = 0
    queries_with_jobs: int = 0
    sampled: int = 0
    # Company presence is judged over every job returned, not the sample:
    # BestJobs ranks its few anonymous (recruiter) postings first, so a
    # top-5 sample was mostly companyless while 95% of results named one.
    company_checked: int = 0
    missing_company: int = 0
    problems: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.jobs_found > 0 and not self.problems


def _validate(job: RawJob, source: str) -> list[str]:
    """Structural assertions on a scraped posting.

    These catch the common silent-breakage shape: the adapter still parses
    *something* (so the count is non-zero) but the fields are empty, which
    downstream turns into "Unknown Title at Unknown" rows in the feed.

    A missing company is judged across the sample instead (see
    _company_problem): boards carry some anonymous postings — BestJobs
    lists recruiter-posted jobs with an empty companyName — and one of
    those must not fail a healthy source.
    """
    problems: list[str] = []
    if not job.url or not job.url.startswith(("http://", "https://")):
        problems.append(f"{source}: job with a non-http url {job.url!r}")
    if not job.title or not job.title.strip():
        problems.append(f"{source}: job {job.url} has an empty title")
    if job.source != source:
        problems.append(
            f"{source}: job {job.url} reports source={job.source!r}"
        )
    if job.salary_min is not None and job.salary_max is not None:
        if job.salary_min > job.salary_max:
            problems.append(
                f"{source}: job {job.url} has salary_min > salary_max"
            )
    return problems


def _has_company(job: RawJob) -> bool:
    return bool(job.company) and job.company != "Unknown"


def _company_problem(result: SourceResult) -> str | None:
    """A source whose results are MOSTLY companyless has a broken parser."""
    if result.company_checked and result.missing_company * 2 > result.company_checked:
        return (
            f"{result.name}: {result.missing_company}/{result.company_checked} "
            f"jobs have no company — the company field is probably no longer parsed"
        )
    return None


def _record_jobs(result: SourceResult, jobs: list[RawJob]) -> None:
    """Fold one query's jobs into the result: counts, validation, samples."""
    if not jobs:
        return
    result.queries_with_jobs += 1
    result.jobs_found += len(jobs)
    # Validate a sample rather than every row — enough to catch a
    # parser that has started returning blanks.
    for job in jobs[:SAMPLE_SIZE]:
        result.problems.extend(_validate(job, result.name))
        result.sampled += 1
    result.company_checked += len(jobs)
    result.missing_company += sum(1 for job in jobs if not _has_company(job))
    for job in jobs[:2]:
        salary = (
            f" | {job.salary_min}-{job.salary_max} {job.salary_currency}"
            if job.salary_min
            else ""
        )
        result.samples.append(
            f"    {job.title[:50]:50s} | {job.company[:24]:24s} "
            f"| {job.location[:30]}{salary}"
        )


def _mark_blocked(result: SourceResult, why: str) -> None:
    result.blocked = True
    result.problems = [f"{result.name}: blocked from CI (advisory) — {why}"]


async def probe(scraper) -> SourceResult:
    _install_status_hook()
    result = SourceResult(name=scraper.name)
    applies_to = getattr(scraper, "applies_to", lambda _query: True)
    queries = [q for q in PROBE_QUERIES if applies_to(q)]
    refused: list[int] = []  # the refusal status of each refused query

    for query in queries:
        seen: list[int] = []
        token = _statuses.set(seen)
        try:
            jobs = await asyncio.wait_for(
                scraper.search(query), timeout=QUERY_TIMEOUT
            )
        except asyncio.TimeoutError:
            result.problems.append(
                f"{scraper.name}: {query.keyword!r} @ {query.location!r} "
                f"timed out after {QUERY_TIMEOUT:.0f}s"
            )
            continue
        except Exception as exc:  # adapters promise never to raise
            result.problems.append(
                f"{scraper.name}: {query.keyword!r} raised "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        finally:
            _statuses.reset(token)

        refusal = [code for code in seen if code in REFUSED_STATUSES]
        print(
            f"{scraper.name:10s} {query.keyword!r} @ {query.location!r}: "
            f"{len(jobs)} jobs"
            + (f" (HTTP {refusal[-1]})" if refusal and not jobs else "")
        )
        if refusal and not jobs:
            refused.append(refusal[-1])
        _record_jobs(result, jobs)

    company = _company_problem(result)
    if company:
        result.problems.append(company)
    if result.jobs_found == 0:
        if queries and not result.problems and len(refused) == len(queries):
            codes = "/".join(str(c) for c in sorted(set(refused)))
            _mark_blocked(
                result, f"HTTP {codes} on all {len(queries)} probe queries",
            )
        else:
            result.problems.append(
                f"{scraper.name}: returned 0 jobs for all "
                f"{len(queries)} probe queries — adapter is probably broken"
            )
    return result


def _cell(row: dict, key: str) -> str:
    value = row.get(key)
    return "" if value is None or str(value) == "nan" else str(value)


def _jobspy_rows_to_raw(df, board: str) -> list[RawJob]:
    """JobSpy DataFrame rows as RawJobs, so one validator covers both."""
    rows = df.to_dict("records") if df is not None else []
    return [
        RawJob(
            url=_cell(row, "job_url") or _cell(row, "job_url_direct"),
            title=_cell(row, "title"),
            company=_cell(row, "company") or "Unknown",
            location=_cell(row, "location"),
            source=_cell(row, "site") or board,
        )
        for row in rows
    ]


async def probe_jobspy(board: str, advisory: bool) -> SourceResult:
    """One direct JobSpy search with the arguments production builds."""
    from functools import partial

    from jobspy import scrape_jobs

    from gosha.filters import city_only, normalize_location
    from gosha.scraper import glassdoor_country, indeed_country, jobspy_kwargs

    query = JOBSPY_PROBE
    location, _subs = normalize_location(query.location)
    if board == "glassdoor":
        country = glassdoor_country(location)
        location = city_only(location)
    else:
        country = indeed_country(location)
    kwargs = jobspy_kwargs(
        [board], query.keyword, location, query.max_age_days,
        proxy=None, country=country,
    )
    kwargs["results_wanted"] = JOBSPY_RESULTS
    kwargs["fetch_description"] = False  # one request, not six

    result = SourceResult(name=board, advisory=advisory)
    try:
        loop = asyncio.get_running_loop()
        df = await asyncio.wait_for(
            loop.run_in_executor(None, partial(scrape_jobs, **kwargs)),
            timeout=QUERY_TIMEOUT,
        )
    except Exception as exc:
        if _REFUSED_TEXT.search(str(exc)):
            _mark_blocked(result, f"jobspy raised {type(exc).__name__}: {exc}")
        else:
            result.problems.append(
                f"{board}: jobspy raised {type(exc).__name__}: {exc}"
            )
        return result

    jobs = _jobspy_rows_to_raw(df, board)
    print(f"{board:10s} {query.keyword!r} @ {location!r}: {len(jobs)} jobs")
    _record_jobs(result, jobs)
    company = _company_problem(result)
    if company:
        result.problems.append(company)
    if result.jobs_found == 0:
        result.problems.append(
            f"{board}: JobSpy returned 0 jobs for {query.keyword!r} "
            f"@ {location!r} — the library or the site changed"
        )
    return result


def _report(results: list[SourceResult]) -> list[SourceResult]:
    """Print the summary; return the sources that should fail the run."""
    print("\n" + "=" * 62)
    for result in results:
        if result.healthy:
            status = "OK  "
        elif result.blocked:
            status = "BLKD"
        else:
            status = "WARN" if result.advisory else "FAIL"
        print(
            f"[{status}] {result.name:10s} "
            f"{result.jobs_found:4d} jobs across "
            f"{result.queries_with_jobs} queries"
            + ("  (blocked from CI, advisory)" if result.blocked
               else "  (advisory)" if result.advisory else "")
        )
        for sample in result.samples[:2]:
            print(sample)

    unhealthy = [r for r in results if not r.healthy]
    if unhealthy:
        print("\nProblems:", file=sys.stderr)
        for result in unhealthy:
            tag = (
                " (not failing the run)" if result.blocked
                else " (advisory, not failing the run)" if result.advisory
                else ""
            )
            for problem in result.problems:
                print(f"  - {problem}{tag}", file=sys.stderr)

    failures = [r for r in unhealthy if not r.advisory and not r.blocked]
    if failures:
        print(
            f"\n{len(failures)}/{len(results)} sources unhealthy: "
            + ", ".join(r.name for r in failures),
            file=sys.stderr,
        )
    else:
        print(f"\nAll gating sources healthy ({len(results)} probed).")
    return failures


async def main(
    only: list[str], jobspy: bool = True, result_file: str | None = None,
) -> int:
    scrapers = get_extra_scrapers()
    boards = dict(JOBSPY_BOARDS) if jobspy else {}
    if only:
        wanted = {name.lower() for name in only}
        scrapers = [s for s in scrapers if s.name.lower() in wanted]
        boards = {b: adv for b, adv in boards.items() if b in wanted}
        if not scrapers and not boards:
            print(f"No sources match {sorted(wanted)}", file=sys.stderr)
            return 1

    results = [await probe(scraper) for scraper in scrapers]
    for board, advisory in boards.items():
        results.append(await probe_jobspy(board, advisory))

    failures = _report(results)

    if result_file:
        Path(result_file).write_text(json.dumps({
            "failing": sorted(r.name for r in failures),
            "advisory_failing": sorted(
                r.name for r in results
                if r.advisory and not r.healthy and not r.blocked
            ),
            "blocked": sorted(r.name for r in results if r.blocked),
            "probed": [r.name for r in results],
        }))
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "sources", nargs="*", help="source names to probe (default: all)"
    )
    parser.add_argument(
        "--no-jobspy", action="store_true",
        help="skip the Indeed/LinkedIn/Glassdoor probes",
    )
    parser.add_argument(
        "--result-file",
        help="write {failing, advisory_failing, blocked, probed} JSON here",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(
        main(args.sources, jobspy=not args.no_jobspy, result_file=args.result_file)
    ))
