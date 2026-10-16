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

from gosha.scrapers.base import RawJob, strip_invisible, strip_title_codes
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


# Requisition codes in titles, as stored from LinkedIn (54 of 7,666 rows on
# 2026-10-16, mostly trainee/junior roles) and eJobs. DMs and the dedup key
# showed them; another board's copy of the same job has none.
@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Trainee Java Developer IRC293492", "Trainee Java Developer"),
        ("Junior QA Engineer I IRC305403", "Junior QA Engineer I"),
        ("ID248805 Junior Quality Assurance Engineer", "Junior Quality Assurance Engineer"),
        ("Junior Full Stack Engineer ID68431", "Junior Full Stack Engineer"),
        ("Software Engineer – RDS Engineering Intern, Summer 2027 (REQ#43448)",
         "Software Engineer – RDS Engineering Intern, Summer 2027"),
        ("Software Test Engineer Intern - JobID-0296", "Software Test Engineer Intern"),
        ("Distributed Systems Engineer (Apache Spark Internals) - Remote Work | REF#294305",
         "Distributed Systems Engineer (Apache Spark Internals) - Remote Work"),
        ("Consultant IT SAP IS-U (Telemuncă) (ID 593)", "Consultant IT SAP IS-U (Telemuncă)"),
        ("Data Engineer [Req ID: 12345]", "Data Engineer"),
        ("Python Developer JR123456", "Python Developer"),
    ],
)
def test_requisition_codes_are_stripped_from_titles(raw, clean):
    assert strip_title_codes(raw) == clean


@pytest.mark.parametrize(
    "title",
    [
        "Data Engineer Intern (2027)",
        "Software Engineer Intern, Summer 2027",
        "C/C++ Developer",
        "Windows 365 Support Engineer",
        "Engineer R2 Platform",
        "Video Producer",
        "ID",  # nothing left without it: kept
        "IRC12345",
        "Junior Android Developer (Kotlin) - Cluj",
    ],
)
def test_titles_without_codes_are_unchanged(title):
    assert strip_title_codes(title) == title


def test_rawjob_titles_lose_their_codes():
    assert RawJob(url="https://x/1", title="Trainee C/C++ Engineer IRC293495").title == (
        "Trainee C/C++ Engineer"
    )


async def test_jobspy_titles_lose_their_codes(monkeypatch):
    from gosha import scraper

    class _Tunnels:
        def active_proxies(self):
            return ["socks5://127.0.0.1:1080"]

    monkeypatch.setattr(scraper, "DELAY_BETWEEN_SEARCHES", 0)
    monkeypatch.setattr(
        scraper, "scrape_jobs",
        lambda **_: pd.DataFrame([{
            "job_url": "https://li/1", "site": "linkedin", "title": "Junior Python Engineer IRC301922",
        }]),
    )
    df = await scraper.scrape_jobs_raw(_Tunnels(), "python developer", "cluj", 14, ["linkedin"])
    assert df.iloc[0]["title"] == "Junior Python Engineer"


# Job sites that repost an employer's ad under their own name (prod
# 2026-10-16: Undelucram.ro 5 rows, Hipo.ro 2, "Entry Level Jobs in TOP
# Companies - Hipo.ro" 1). The DM showed the job site as the employer and
# dedup could not pair the repost with the employer's own ad.
from gosha.scrapers.base import is_reposter, reposter_client  # noqa: E402


def test_reposters_are_recognised():
    for name in ("Undelucram.ro", "Hipo.ro", "Entry Level Jobs in TOP Companies - Hipo.ro"):
        assert is_reposter(name), name
    for name in ("Continental", "Hipodrom SA", "Endava"):
        assert not is_reposter(name), name


@pytest.mark.parametrize(
    ("description", "client"),
    [
        ("Undelucram.ro on behalf of: **Continental Anvelope Romania**\n\nCompany Description: "
         "**Continental** is a leading tire manufacturer", "Continental Anvelope Romania"),
        ("Undelucram.ro on behalf of: **Capgemini Romania** At Capgemini Engineering", "Capgemini Romania"),
        ("Undelucram.ro on behalf of: Capgemini Romania\nAt Capgemini Engineering", "Capgemini Romania"),
        ("**Company Description** Since its spin-off in September 2025 AUMOVIO continues", None),
        ("", None),
    ],
)
def test_reposter_client_is_read_from_on_behalf_of(description, client):
    assert reposter_client(description) == client


def test_hipo_title_names_the_employer_after_an_at_sign():
    job = RawJob(url="https://www.hipo.ro/x", title="Tech I Engineering Intern I @ Vertiv",
                 company="Entry Level Jobs in TOP Companies - Hipo.ro")
    assert (job.title, job.company) == ("Tech I Engineering Intern I", "Vertiv")
    # Only for a reposter: an employer's own title keeps its "@".
    own = RawJob(url="https://x/2", title="Support Engineer @ Home Office", company="Acme")
    assert (own.title, own.company) == ("Support Engineer @ Home Office", "Acme")


async def test_jobspy_reposts_name_the_client(monkeypatch):
    from gosha import scraper

    class _Tunnels:
        def active_proxies(self):
            return ["socks5://127.0.0.1:1080"]

    monkeypatch.setattr(scraper, "DELAY_BETWEEN_SEARCHES", 0)
    monkeypatch.setattr(
        scraper, "scrape_jobs",
        lambda **_: pd.DataFrame([
            {"job_url": "https://li/7621", "site": "linkedin", "title": "Internship IT (8h) - Tires",
             "company": "Undelucram.ro",
             "description": "Undelucram.ro on behalf of: **Continental Anvelope Romania**\n\nCompany"},
            {"job_url": "https://li/8011", "site": "linkedin", "title": "Hardware Test Engineer",
             "company": "Hipo.ro", "description": "**Company Description** AUMOVIO continues"},
        ]),
    )
    df = await scraper.scrape_jobs_raw(_Tunnels(), "internship", "timisoara", 14, ["linkedin"])
    assert list(df["company"]) == ["Continental Anvelope Romania", "Hipo.ro"]
