"""Live smoke test: run every adapter against the real endpoints and assert.

This is the outside-in half of the scraper monitoring (the inside-out half
is gosha/scrape_health.py, which watches production yields). A board can
change its markup or API at any time; the unit tests run against recorded
fixtures and will happily stay green while the live site returns nothing.

Exit code 0 when every adapter returned usable jobs, 1 otherwise, so CI can
gate on it. Run manually with:

    python scripts/smoke_scrapers.py            # all adapters
    python scripts/smoke_scrapers.py ejobs hipo # a subset

A source is considered healthy when at least one of the probe queries
returns at least one job whose url/title/company survive validation. Some
boards legitimately have nothing for one narrow query, which is why every
adapter gets several.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field

from gosha.scrapers.base import RawJob, SearchQuery
from gosha.scrapers.registry import get_extra_scrapers

PROBE_QUERIES = [
    SearchQuery(keyword="software engineer", location="cluj", max_age_days=30),
    SearchQuery(keyword="python developer", location="remote", max_age_days=30),
    SearchQuery(keyword="java", location="bucharest", max_age_days=30),
]

# Per-adapter timeout. A hung board must fail the check, not the runner.
QUERY_TIMEOUT = 90.0


@dataclass
class SourceResult:
    name: str
    jobs_found: int = 0
    queries_with_jobs: int = 0
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
    """
    problems: list[str] = []
    if not job.url or not job.url.startswith(("http://", "https://")):
        problems.append(f"{source}: job with a non-http url {job.url!r}")
    if not job.title or not job.title.strip():
        problems.append(f"{source}: job {job.url} has an empty title")
    if not job.company or job.company == "Unknown":
        problems.append(f"{source}: job {job.url} has no company")
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


async def probe(scraper) -> SourceResult:
    result = SourceResult(name=scraper.name)

    for query in PROBE_QUERIES:
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

        print(
            f"{scraper.name:10s} {query.keyword!r} @ {query.location!r}: "
            f"{len(jobs)} jobs"
        )
        if jobs:
            result.queries_with_jobs += 1
            result.jobs_found += len(jobs)
            # Validate a sample rather than every row — enough to catch a
            # parser that has started returning blanks.
            for job in jobs[:5]:
                result.problems.extend(_validate(job, scraper.name))
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

    if result.jobs_found == 0:
        result.problems.append(
            f"{scraper.name}: returned 0 jobs for all "
            f"{len(PROBE_QUERIES)} probe queries — adapter is probably broken"
        )
    return result


async def main(only: list[str]) -> int:
    scrapers = get_extra_scrapers()
    if only:
        wanted = {name.lower() for name in only}
        scrapers = [s for s in scrapers if s.name.lower() in wanted]
        if not scrapers:
            print(f"No adapters match {sorted(wanted)}", file=sys.stderr)
            return 1

    results = [await probe(scraper) for scraper in scrapers]

    print("\n" + "=" * 62)
    for result in results:
        status = "OK  " if result.healthy else "FAIL"
        print(
            f"[{status}] {result.name:10s} "
            f"{result.jobs_found:4d} jobs across "
            f"{result.queries_with_jobs}/{len(PROBE_QUERIES)} queries"
        )
        for sample in result.samples[:2]:
            print(sample)

    failures = [r for r in results if not r.healthy]
    if failures:
        print("\nProblems:", file=sys.stderr)
        for result in failures:
            for problem in result.problems:
                print(f"  - {problem}", file=sys.stderr)
        print(
            f"\n{len(failures)}/{len(results)} sources unhealthy: "
            + ", ".join(r.name for r in failures),
            file=sys.stderr,
        )
        return 1

    print(f"\nAll {len(results)} sources healthy.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "sources", nargs="*", help="adapter names to probe (default: all)"
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.sources)))
