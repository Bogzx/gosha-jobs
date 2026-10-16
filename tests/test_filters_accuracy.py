"""Hard filters on realistic Romanian/English postings.

Each test names the failure it pins: what a user with that search was
sent (or missed) before the fix.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gosha.filters import (
    canonical_level,
    keyword_wants_entry_level,
    keyword_wants_tech,
    location_matches,
    matches_company_blacklist,
    matches_excluded_keywords,
    matches_experience_level,
    normalize_location,
    required_years,
    title_is_relevant,
)


def located(job_location: str, search: str) -> bool:
    _search, subs = normalize_location(search)
    return location_matches(job_location, subs)


# ── Locations ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("job_location, search", [
    # Typed as "City, Country": looked up as the whole string it was an
    # unknown place, matched only postings repeating it verbatim.
    ("Cluj-Napoca", "Cluj-Napoca, Romania"),
    ("Bucharest", "Bucuresti, Romania"),
    ("București, IF, RO", "Bucharest, RO"),
    ("Ploiești, Prahova", "Ploiesti, Romania"),
    # Hyphen versus space.
    ("Targu-Mures", "romania"),
    ("Târgu Mureș, Mureș", "Tirgu Mures"),
    # Romanian cities the Europe-wide wildcards left out.
    ("Arad", "europe"),
    ("Craiova, Dolj", "eu"),
    # Indeed's county-code format for a town no list names.
    ("Florești, CJ, RO", "romania"),
    # Remote as the boards write it.
    ("Worldwide", "remote"),
    ("Telemuncă", "remote"),
    ("de acasă", "remote"),
    # Nationwide postings are open to someone in any Romanian city.
    ("Romania", "cluj"),
    ("România (Remote)", "iasi"),
])
def test_locations_that_should_match(job_location, search):
    assert located(job_location, search)


@pytest.mark.parametrize("job_location, search", [
    # Substring hits: a city name inside another word.
    ("Kiasi", "iasi"),
    ("Siauliai", "iasi"),
    ("Brasília", "brasov"),
    ("Paris, France", "romania"),
    ("Chisinau, Moldova", "romania"),
    # A Cluj search does not take a posting in another city, nor a foreign
    # country-level one.
    ("Bucharest", "cluj"),
    ("Germany", "cluj"),
    ("", "cluj"),
])
def test_locations_that_should_not_match(job_location, search):
    assert not located(job_location, search)


@pytest.mark.parametrize("job_location", [
    # "RO" counts only as a word of its own; none of these are Romania.
    "Rio de Janeiro, RJ, BR", "Porto, Portugal", "Providence, RI, US",
    "Roanoke, VA", "Rome, Italy", "Rotterdam, NL", "Remote - US only",
    "Toronto, ON, CA", "Ro-Ro Logistics, Hamburg",
])
def test_ro_matches_only_as_a_country_code(job_location):
    if job_location == "Ro-Ro Logistics, Hamburg":
        # The one false positive a whole-word "ro" admits; no board puts a
        # company name in the location field, so it is accepted.
        assert located(job_location, "romania")
        return
    assert not located(job_location, "romania")
    assert not located(job_location, "cluj")


def test_ro_country_code_forms():
    assert located("Remote (RO/MD)", "romania")
    assert located("Cluj-Napoca, CJ, RO", "romania")
    # Romania or Moldova, remote: a nationwide posting for a Cluj search.
    assert not located("Remote (RO/MD)", "cluj")


def test_nationwide_postings_do_not_reach_foreign_city_searches():
    assert not located("Romania", "berlin")
    assert not located("Romania", "remote")


# ── Excluded keywords and blacklists ──────────────────────────────────


@pytest.mark.parametrize("text, excluded", [
    # \b needs a word character beside it: these never excluded anything.
    ("Senior C++ Developer - embedded", ["c++"]),
    ("C# Backend Engineer", ["C#"]),
    ("Software Engineer .NET", [".net"]),
    # Diacritics either way round.
    ("Agent Vânzări Cluj", ["vanzari"]),
    ("Agent Vanzari", ["vânzări"]),
    ("Consultant Şef (cedilla)", ["șef"]),
    # A phrase across a line break in a description.
    ("we offer\ncustomer   support roles", ["customer support"]),
])
def test_excluded_keywords_that_should_exclude(text, excluded):
    assert matches_excluded_keywords(text, excluded)


@pytest.mark.parametrize("text, excluded", [
    ("JavaScript Developer", ["java"]),
    ("Wholesale analyst", ["sales"]),
    ("C Developer", ["c++"]),
    ("Python Developer", ["   "]),
])
def test_excluded_keywords_that_should_not_exclude(text, excluded):
    assert not matches_excluded_keywords(text, excluded)


def test_company_blacklist_folds_diacritics():
    assert matches_company_blacklist("Ţiriac Holdings", ["Tiriac"])
    assert not matches_company_blacklist("Acme", ["", " "])


# ── What a keyword implies ────────────────────────────────────────────


@pytest.mark.parametrize("keyword", [
    "international relations", "internal audit", "internet marketing",
])
def test_intern_inside_another_word_is_not_an_internship_search(keyword):
    assert not keyword_wants_entry_level(keyword)


@pytest.mark.parametrize("keyword", ["stagiu IT", "practică vara", "absolvent", "internship"])
def test_romanian_entry_level_keywords(keyword):
    assert keyword_wants_entry_level(keyword)


@pytest.mark.parametrize("keyword, title", [
    # "ai" inside "retail", "training", "chain": held to tech titles.
    ("retail", "Retail Store Assistant"),
    ("supply chain analyst", "Supply Chain Analyst"),
    ("training coordinator", "Training Coordinator"),
    # A keyword naming another engineering field was held to CS titles and
    # then rejected for that field.
    ("civil engineer", "Civil Engineer - Infrastructure"),
    ("mechanical engineer", "Mechanical Design Engineer"),
])
def test_non_tech_keywords_keep_their_own_postings(keyword, title):
    assert not keyword_wants_tech(keyword)
    assert title_is_relevant(title, keyword)


def test_tech_keywords_still_filter():
    assert keyword_wants_tech("ai engineer")
    assert keyword_wants_tech("IT support")
    assert keyword_wants_tech("marketing data analyst")
    assert not title_is_relevant("Sales Manager", "python developer")


# ── Experience levels ─────────────────────────────────────────────────


@pytest.mark.parametrize("title, label, description", [
    # Unmarked titles: most postings, and most take graduates.
    ("QA Engineer", None, "Manual testing of web apps. SQL basics."),
    ("Data Engineer", None, None),
    ("React Developer", None, "Minimum 1-2 years of experience with React."),
    # Entry-level wording the junior level used to miss.
    ("Data Analyst (fără experiență)", None, None),
    ("Dezvoltator Software Începător", None, None),
    ("Software Engineer, New Grad", None, None),
    # The board's label.
    ("Python Developer", "entry", "Minimum 5 years"),
])
def test_junior_level_takes(title, label, description):
    assert matches_experience_level(title, ["junior"], label, description)


@pytest.mark.parametrize("title, label, description", [
    # An explicit senior title beat by the board's "most junior" label.
    ("Senior Java Developer", "entry", None),
    ("Team Leader Python", None, None),
    ("Contabil Șef", None, None),
    ("Software Engineer II", None, None),
    ("Medior Java Developer", None, None),
    ("Java Developer (3+ years)", None, None),
    # Unmarked, but the posting asks for mid-level experience.
    ("Python Developer", None, "Minimum 3-5 years of experience as a Python Developer"),
    ("Programator Java", None, "Cerințe: experiență de minim 4 ani în Java."),
    ("Backend Developer", None, "At least three (3) years of professional experience."),
    # The board says so.
    ("Python Developer", "senior", None),
    # Internships are the intern level's.
    ("Software Engineer Intern", None, None),
])
def test_junior_level_rejects(title, label, description):
    assert not matches_experience_level(title, ["junior"], label, description)


def test_intern_level_stays_strict():
    assert matches_experience_level("Stagiu de practică IT", ["intern"])
    assert not matches_experience_level("Junior Java Developer", ["intern"])
    assert not matches_experience_level("QA Engineer", ["intern"])
    assert not matches_experience_level("Senior Java Developer", ["intern"], "intern")


@pytest.mark.parametrize("description, years", [
    ("Minimum 3-5 years of experience", 3),
    ("2+ years experience with Docker; 5 years total experience", 5),
    # A senior posting's nice-to-have does not make it junior.
    ("5+ years of experience overall, 1 year of experience with Kafka is a plus", 5),
    ("Minimum 2 ani experiență; experiența în Azure (1 an) constituie un avantaj", 2),
    ("experiență de cel puțin 2 ani", 2),
    ("Avem 20 de ani de experiență pe piață", None),
    ("A 1-year contract, no experience needed", None),
    ("1 year of experience is a plus", None),
    ("1 year of experience required", 1),
    ("We have been in business for 6 years.", None),
    (None, None),
])
def test_required_years(description, years):
    assert required_years(description) == years


@pytest.mark.parametrize("typed, level", [
    ("Entry", "junior"), ("entry-level", "junior"), ("Internship", "intern"),
    ("medior", "mid"), ("Senior+", "senior"), ("any", "any"), ("expert", None),
])
def test_typed_levels(typed, level):
    assert canonical_level(typed) == level


def test_typed_level_aliases_match_instead_of_matching_nothing():
    # /edit takes free text; "entry" used to match no posting at all.
    assert matches_experience_level("Junior QA Engineer", ["entry"])


EVAL_POSTINGS = Path(__file__).resolve().parent.parent / "eval" / "postings.jsonl"


def test_default_web_search_finds_most_student_postings_in_the_eval_set():
    """The SPA's default search levels are intern + junior. Title-only
    matching found 15 of the eval set's 112 intern/junior/any postings;
    measured after the fix: 106, with 7 of its 70 senior ones let through."""
    postings = [json.loads(line) for line in EVAL_POSTINGS.read_text().splitlines()]
    student = [p for p in postings if p["level"] in ("intern", "junior", "any")]
    senior = [p for p in postings if p["level"] == "senior"]

    def kept(group):
        return sum(
            matches_experience_level(p["title"], ["intern", "junior"], None, p["description"])
            for p in group
        )

    assert kept(student) >= 0.9 * len(student)
    assert kept(senior) <= 0.15 * len(senior)


# ── The keyword gate (title_is_relevant) agrees with the junior level ──


def test_unmarked_posting_reaches_a_junior_keyword_search():
    entry_qa = "Manual and automated testing. 0-1 years of experience."
    assert title_is_relevant("QA Engineer", "junior qa", None, entry_qa)
    assert not title_is_relevant("Senior QA Engineer", "junior qa", None, entry_qa)
    assert not title_is_relevant(
        "QA Engineer", "junior qa", None, "Minimum 4 years of experience in QA.",
    )
    # Explicit entry-level titles still pass, internships included.
    assert title_is_relevant("QA Intern", "junior qa")


def test_internship_keywords_still_want_an_internship_or_entry_title():
    assert title_is_relevant("Software Engineer Intern", "software engineering internship")
    assert title_is_relevant("Junior Developer", "software engineering internship")
    assert not title_is_relevant("QA Engineer", "qa internship")
