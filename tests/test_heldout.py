"""Tests for the held-out ranking eval set (eval/heldout/).

The set only works as a check on overfitting if it stays independent of
eval/postings.jsonl and labelled the same way, so these pin the
properties eval/heldout/README.md promises. No network: the collector is
imported, never run.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from gosha import evaluation as ev

ROOT = Path(__file__).resolve().parent.parent
HELDOUT = ROOT / "eval" / "heldout"
POSTINGS = HELDOUT / "postings.jsonl"
PERSONAS = HELDOUT / "personas.jsonl"
KEYS = {"id", "source", "lang", "families", "level", "title", "company", "description", "url"}


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def no_run(coro):
    coro.close()
    raise AssertionError("eval/heldout/collect.py started a collection on import")


def load_collector():
    # Importing must not start a scrape (the run is behind `if __name__ == "__main__"`);
    # if that regresses, fail here instead of hitting the live boards.
    spec = importlib.util.spec_from_file_location("heldout_collect", HELDOUT / "collect.py")
    module = importlib.util.module_from_spec(spec)
    with patch.object(asyncio, "run", no_run):
        spec.loader.exec_module(module)
    return module


def ruff() -> str | None:
    local = Path(sys.executable).with_name("ruff")
    return str(local) if local.exists() else shutil.which("ruff")


@pytest.mark.skipif(ruff() is None, reason="ruff not installed")
def test_heldout_is_lint_clean():
    # CI runs `ruff check .`; the collector once failed it with E401/E402/E702.
    result = subprocess.run([ruff(), "check", str(HELDOUT)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_collector_imports_without_collecting():
    module = load_collector()
    assert len(module.QUERIES) == 57
    assert module.MAX_DESCRIPTION_CHARS == 2000


def test_collector_scrub_removes_contact_details():
    scrub = load_collector().scrub
    text = "Mail a.b+c@x.co.uk or call +40 721 123 456,  0264 123 4567, see https://x.y/z www.q.ro ok"
    assert scrub(text) == "Mail [email] or call [phone], [phone], see [link] [link] ok"


def test_postings_schema_and_counts():
    email = load_collector()._EMAIL
    data = rows(POSTINGS)
    assert len(data) == 261
    for row in data:
        assert KEYS <= set(row) <= KEYS | {"note"}, row["id"]
        assert row["id"].startswith(f"heldout-{row['source']}-")
        assert row["lang"] in {"en", "ro"}
        assert row["level"] in ev.LEVELS
        assert row["families"] and set(row["families"]) <= set(ev.FAMILIES), row["id"]
        assert len(row["description"]) <= 2000
        assert not email.search(row["description"]), row["id"]
    assert len({r["id"] for r in data}) == len(data)
    assert len({r["url"] for r in data}) == len(data)
    assert {f for r in data for f in r["families"]} == set(ev.FAMILIES)
    assert len(ev.load_postings(POSTINGS)) == 261


def test_postings_disjoint_from_main_set():
    main = rows(ROOT / "eval" / "postings.jsonl")
    held = rows(POSTINGS)

    def key(r):
        return r["title"].strip().lower(), r["company"].strip().lower()

    assert not {r["url"] for r in main} & {r["url"] for r in held}
    assert not {key(r) for r in main} & {key(r) for r in held}


@pytest.mark.parametrize("personas", [PERSONAS, ROOT / "eval" / "personas.jsonl"], ids=["heldout", "main"])
def test_every_persona_has_enough_relevant_postings(personas):
    # Fewer than 4 grade-2 postings makes a persona's nDCG@10 mostly noise.
    postings = ev.load_postings(POSTINGS)
    people = ev.load_personas(personas)
    if personas == PERSONAS:
        assert len(people) == 10
        assert not {p.id for p in people} & {p.id for p in ev.load_personas(ROOT / "eval" / "personas.jsonl")}
    for persona in people:
        assert persona.family in ev.ADJACENT, persona.id
        assert sum(ev.grade(persona.family, p) == 2 for p in postings) >= 4, persona.id


TWO_YEAR_NOTE = "level: 2-year rule (owner 2026-10-16)"
TWO_YEAR_RELABELS = {
    ROOT / "eval" / "postings.jsonl": {"ejobs-027"},
    POSTINGS: {
        "heldout-ejobs-002", "heldout-ejobs-005", "heldout-ejobs-006", "heldout-ejobs-034",
        "heldout-ejobs-045", "heldout-ejobs-052", "heldout-ejobs-059", "heldout-jobicy-014",
    },
}


@pytest.mark.parametrize("path", list(TWO_YEAR_RELABELS), ids=["main", "heldout"])
def test_two_year_rule_relabels_are_junior(path):
    # Owner's decision: exactly 2 years (or "2+ years" as the headline) is junior, in both sets.
    noted = {r["id"]: r["level"] for r in rows(path) if TWO_YEAR_NOTE in r.get("note", "")}
    assert set(noted) == TWO_YEAR_RELABELS[path]
    assert set(noted.values()) == {"junior"}


RANGE_NOTE = "level: range lower-bound rule (owner 2026-10-16)"
RANGE_RELABELS = {
    ROOT / "eval" / "postings.jsonl": {
        "bestjobs-032": "junior", "bestjobs-109": "junior", "bestjobs-101": "mid",
        "ejobs-032": "mid", "bestjobs-087": "senior",
    },
    POSTINGS: {"heldout-bestjobs-045": "junior"},
}


@pytest.mark.parametrize("path", list(RANGE_RELABELS), ids=["main", "heldout"])
def test_range_rule_relabels_use_the_lower_bound(path):
    # Owner's decision: a range of years is labelled by its lower bound, as the filters read it.
    noted = {r["id"]: r["level"] for r in rows(path) if RANGE_NOTE in r.get("note", "")}
    assert noted == RANGE_RELABELS[path]


GRADUATE_NOTE = "level: finished-degree graduate rule (owner 2026-10-17)"
GRADUATE_RELABELS = {
    ROOT / "eval" / "postings.jsonl": set(),
    POSTINGS: {"heldout-ejobs-069"},
}


@pytest.mark.parametrize("path", list(GRADUATE_RELABELS), ids=["main", "heldout"])
def test_graduate_rule_relabels_are_junior(path):
    # Owner's decision: a graduate programme that needs a finished degree is junior, not intern.
    noted = {r["id"]: r["level"] for r in rows(path) if GRADUATE_NOTE in r.get("note", "")}
    assert set(noted) == GRADUATE_RELABELS[path]
    assert set(noted.values()) <= {"junior"}
