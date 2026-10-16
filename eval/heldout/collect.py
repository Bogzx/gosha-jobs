"""Collector used for eval/heldout on 2026-10-16: scripts/eval_collect.py with (keyword, location) pairs.

Usage: .venv/bin/python eval/heldout/collect.py /tmp/candidates.jsonl   (writes the unlabelled candidate pool)
Skips anything already in eval/postings.jsonl. Not deterministic: the boards change daily."""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # repo root (was a machine-specific absolute path)
sys.path.insert(0, str(REPO))

from gosha.scrapers import bestjobs, ejobs, hipo  # noqa: E402
from gosha.scrapers.base import SearchQuery  # noqa: E402
from gosha.scrapers.registry import get_extra_scrapers  # noqa: E402

QUERIES = [
    ("python developer", "Cluj"), ("java developer", "Bucuresti"), ("c# developer", "Iasi"),
    ("frontend developer", "Timisoara"), ("node.js", "romania"), ("php developer", "romania"),
    ("full stack", "Cluj"), ("mobile developer", "romania"), ("kotlin", "romania"), ("react native", "romania"),
    ("data engineer", "Bucuresti"), ("business intelligence", "romania"), ("power bi", "romania"),
    ("data scientist", "romania"), ("ai engineer", "romania"), ("c++", "Iasi"), ("firmware", "romania"),
    ("automotive software", "Timisoara"), ("cloud engineer", "romania"), ("site reliability", "romania"),
    ("kubernetes", "romania"), ("qa automation", "Cluj"), ("tester manual", "romania"),
    ("cyber security", "romania"), ("helpdesk", "romania"), ("suport it", "romania"),
    ("business analyst", "romania"), ("project manager", "romania"), ("contabil", "Cluj"),
    ("economist", "romania"), ("agent vanzari", "romania"), ("call center", "Iasi"),
    ("operator date", "romania"),
    # entry level
    ("intern", "Bucuresti"), ("internship it", "Cluj"), ("practica", "romania"), ("absolvent", "romania"),
    ("entry level", "romania"), ("junior software", "Timisoara"), ("junior .net", "romania"),
    ("junior frontend", "Iasi"), ("junior data analyst", "romania"), ("junior tester", "romania"),
    ("junior android", "romania"), ("trainee", "romania"), ("graduate program", "romania"),
    ("junior machine learning", "romania"), ("junior c++", "romania"), ("stagiu", "Bucuresti"),
    # remote
    ("python", "remote"), ("developer", "remote"), ("data", "remote"), ("devops", "remote"),
    ("frontend", "remote"), ("qa", "remote"), ("machine learning", "remote"), ("support", "remote"),
]
DETAIL_FETCHES_PER_SEARCH = 6
MIN_DESCRIPTION_CHARS = 200
MAX_DESCRIPTION_CHARS = 2000
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_PHONE = re.compile(r"(?:\+|\b00)\d[\d\s().-]{7,}\d|\b0\d{2,3}[\s.-]?\d{3}[\s.-]?\d{3,4}\b|\d*\[phone\]\d*")
_URL = re.compile(r"https?://\S+|www\.\S+")


def scrub(text):
    text = _EMAIL.sub("[email]", text)
    text = _URL.sub("[link]", text)
    text = _PHONE.sub("[phone]", text)
    return re.sub(r"\s+", " ", text).strip()


async def collect(out: Path):
    for m in (bestjobs, ejobs, hipo):
        m.MAX_DETAIL_FETCHES = DETAIL_FETCHES_PER_SEARCH
    seen_urls, seen_keys = set(), set()
    for line in (REPO / "eval/postings.jsonl").read_text().splitlines():
        r = json.loads(line)
        seen_urls.add(r["url"])
        seen_keys.add((r["title"].strip().lower(), r["company"].strip().lower()))
    n = 0
    with out.open("w", encoding="utf-8") as fh:
        for kw, loc in QUERIES:
            q = SearchQuery(keyword=kw, location=loc, max_age_days=30)
            for s in get_extra_scrapers():
                if not s.applies_to(q):
                    continue
                try:
                    jobs = await s.search(q)
                except Exception as e:
                    print(f"{s.name} {kw} failed {e}", file=sys.stderr)
                    continue
                kept = 0
                for j in jobs:
                    d = scrub(j.description or "")
                    key = (scrub(j.title).strip().lower(), scrub(j.company).strip().lower())
                    if len(d) < MIN_DESCRIPTION_CHARS or j.url in seen_urls or key in seen_keys:
                        continue
                    seen_urls.add(j.url)
                    seen_keys.add(key)
                    fh.write(json.dumps({"source": j.source, "url": j.url, "query": kw, "qloc": loc,
                        "location": j.location, "board_level": j.experience_level,
                        "title": scrub(j.title), "company": scrub(j.company),
                        "description": d[:MAX_DESCRIPTION_CHARS]}, ensure_ascii=False) + "\n")
                    fh.flush()
                    kept += 1
                    n += 1
                print(f"{s.name:9s} {kw!r:26s} {loc:10s} {len(jobs):3d} found {kept:3d} kept", file=sys.stderr)
    print(n, "->", out, file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(collect(Path(sys.argv[1])))
