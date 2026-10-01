"""Collect real job postings for the ranking eval (eval/postings.jsonl).

Runs the native adapters (eJobs, BestJobs, Hipo, RemoteOK) for a fixed
list of searches, keeps postings with a real description, scrubs contact
details and writes one JSON object per posting. The output is a candidate
pool: a person then picks a balanced subset and adds the labels
(`families`, `level`, `lang`) by hand. See docs/EVAL.md.

JobSpy (Indeed/LinkedIn/Glassdoor) is deliberately not used: it would
scrape from whatever IP runs this, which may be the production server.

    python scripts/eval_collect.py --out /tmp/candidates.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gosha.scrapers import bestjobs, ejobs, hipo  # noqa: E402
from gosha.scrapers.base import SearchQuery  # noqa: E402
from gosha.scrapers.registry import get_extra_scrapers  # noqa: E402

# A spread of IT and non-IT roles, so every persona has relevant postings
# and plenty of plausible distractors. The boards skew senior, so the
# second half asks for entry-level roles explicitly: without it a student
# persona has almost nothing it should rank first.
QUERIES = [
    "python", "java", "frontend", ".net", "devops", "data analyst",
    "machine learning", "embedded", "tester", "android", "contabil",
    "vanzari", "customer support", "internship",
    "junior developer", "junior frontend", "react", "angular", "junior java",
    "junior python", "junior qa", "junior data", "ios", "flutter", "stagiu",
    "junior devops", "junior embedded", "junior contabil",
]
LOCATION = "romania"

# Keep the run polite: a few detail pages per search, not the adapters'
# production caps.
DETAIL_FETCHES_PER_SEARCH = 6
MIN_DESCRIPTION_CHARS = 200
MAX_DESCRIPTION_CHARS = 2000

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_PHONE = re.compile(
    r"(?:\+|\b00)\d[\d\s().-]{7,}\d"          # international: +40 ..., 0049 ...
    r"|\b0\d{2,3}[\s.-]?\d{3}[\s.-]?\d{3,4}\b"  # national: 07xx xxx xxx, 0264 ...
    r"|\d*\[phone\]\d*"                       # leftovers around an earlier scrub
)
_URL = re.compile(r"https?://\S+|www\.\S+")


def scrub(text: str) -> str:
    """Remove contact details a posting may carry (recruiter emails, phones)."""
    text = _EMAIL.sub("[email]", text)
    text = _URL.sub("[link]", text)
    text = _PHONE.sub("[phone]", text)
    return re.sub(r"\s+", " ", text).strip()


async def collect(queries: list[str]) -> list[dict]:
    for module in (bestjobs, ejobs, hipo):
        module.MAX_DETAIL_FETCHES = DETAIL_FETCHES_PER_SEARCH

    seen_urls: set[str] = set()
    seen_keys: set[tuple[str, str]] = set()
    rows: list[dict] = []
    for keyword in queries:
        query = SearchQuery(keyword=keyword, location=LOCATION, max_age_days=30)
        for scraper in get_extra_scrapers():
            jobs = await scraper.search(query)
            kept = 0
            for job in jobs:
                description = scrub(job.description or "")
                key = (job.title.strip().lower(), job.company.strip().lower())
                if (
                    len(description) < MIN_DESCRIPTION_CHARS
                    or job.url in seen_urls
                    or key in seen_keys
                ):
                    continue
                seen_urls.add(job.url)
                seen_keys.add(key)
                rows.append({
                    "source": job.source,
                    "url": job.url,
                    "query": keyword,
                    "title": scrub(job.title),
                    "company": scrub(job.company),
                    "description": description[:MAX_DESCRIPTION_CHARS],
                })
                kept += 1
            print(f"{scraper.name:9s} {keyword!r:20s} {len(jobs):3d} found, {kept:3d} kept",
                  file=sys.stderr)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--queries", nargs="+", default=QUERIES,
                        help="override the search list (default: QUERIES)")
    args = parser.parse_args()
    rows = asyncio.run(collect(args.queries))
    with args.out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"{len(rows)} postings -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
