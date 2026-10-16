"""Invisible characters and leftover entities in scraped text.

eJobs descriptions end their lines with a watermark of variation selectors
and zero-width spaces, invisible in a browser but tokens to TF-IDF, noise to
the embeddings and part of the dedup key; Hipo's JSON-LD ships text whose
entities are escaped twice, so one unescape left "&amp;" and "&nbsp;" in
descriptions (eval/heldout/postings.jsonl, collected 2026-10-16).
"""

from __future__ import annotations

import pandas as pd
import pytest

from gosha.scrapers.base import RawJob, strip_invisible
from gosha.scrapers.details import html_to_text

# Verbatim from eJobs (eval/heldout/postings.jsonl, heldout-ejobs-013).
EJOBS_WATERMARKED = (
    "Cerințe Cloud & AWS︁︀​︂︀︀​"
    "︁︂︉​︇︉"
)


def test_ejobs_watermark_is_stripped():
    assert strip_invisible(EJOBS_WATERMARKED) == "Cerințe Cloud & AWS"
    assert html_to_text(f"<p>{EJOBS_WATERMARKED}</p>") == "Cerințe Cloud & AWS"


@pytest.mark.parametrize(
    "char",
    [
        "️",       # variation selector-16
        "\U000e0100",   # variation selector supplement
        "​", "‌", "‍",  # zero-width space / non-joiner / joiner
        "⁠",       # word joiner
        "﻿",       # BOM / zero-width no-break space
        "­",       # soft hyphen (Cf)
        "‮",       # right-to-left override (Cf)
        "\U000e0041",   # tag character (Cf)
        "\x84",         # C1 control, seen in a RemoteOK description
    ],
)
def test_invisible_characters_are_stripped(char):
    assert strip_invisible(f"Java{char} Developer") == "Java Developer"


def test_visible_text_is_left_alone():
    text = "Inginer software — București, Iași, Țară\nC++ & C#\tcafé 💻"
    assert strip_invisible(text) == text


def test_hipo_double_escaped_entities_are_decoded():
    # As Hipo's JSON-LD carries them after json decoding.
    raw = (
        "Quality &amp;amp; Compliance. Requisition ID:&amp;nbsp; 1728450 "
        "Location:&amp;nbsp; Cluj, RO &amp;#8203;Who we are"
    )
    assert html_to_text(raw) == (
        "Quality & Compliance. Requisition ID: 1728450 Location: Cluj, RO Who we are"
    )


def test_escaped_markup_does_not_survive_as_tags():
    assert html_to_text("&lt;p&gt;Build APIs&lt;/p&gt;&lt;p&gt;Ship&lt;/p&gt;") == "Build APIs\nShip"


def test_single_escaped_text_is_unchanged():
    assert html_to_text("<p>R&amp;D &lt;3</p>") == "R&D <3"


def test_rawjob_cleans_title_company_location_description():
    job = RawJob(
        url="https://x/1",
        title="Java​ Developer️",
        company="Acme﻿ SRL",
        location="Cluj​-Napoca",
        description=EJOBS_WATERMARKED,
    )
    assert (job.title, job.company, job.location, job.description) == (
        "Java Developer", "Acme SRL", "Cluj-Napoca", "Cerințe Cloud & AWS",
    )


async def test_jobspy_text_columns_are_cleaned(monkeypatch):
    from gosha import scraper

    class _Tunnels:
        def active_proxies(self):
            return ["socks5://127.0.0.1:1080"]

    monkeypatch.setattr(scraper, "DELAY_BETWEEN_SEARCHES", 0)
    monkeypatch.setattr(
        scraper, "scrape_jobs",
        lambda **_: pd.DataFrame([{
            "job_url": "https://li/1", "site": "linkedin",
            "title": "Data​ Engineer", "company": "Acme﻿",
            "location": "Cluj", "description": EJOBS_WATERMARKED,
        }, {
            "job_url": "https://li/2", "site": "linkedin",
            "title": "QA", "company": None, "location": None, "description": None,
        }]),
    )
    df = await scraper.scrape_jobs_raw(_Tunnels(), "data engineer", "cluj", 14, ["linkedin"])
    first = df.iloc[0]
    assert (first["title"], first["company"], first["description"]) == (
        "Data Engineer", "Acme", "Cerințe Cloud & AWS",
    )
    assert df.iloc[1]["company"] is None
