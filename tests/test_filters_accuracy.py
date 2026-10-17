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
    looks_like_an_ad,
    matches_company_blacklist,
    matches_excluded_keywords,
    matches_excluded,
    matches_experience_level,
    normalize_location,
    required_years,
    search_location_ok,
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
    # A search for the country takes the country-level postings.
    ("Romania", "romania"),
    ("România (Remote)", "romania"),
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


@pytest.mark.parametrize("job_location", [
    # Romanian place names (and "RO") abroad: the posting names its country.
    "Porto Velho, RO, BR", "Ro, Emilia-Romagna, Italy", "Calarasi, Moldova",
    "Floresti, Moldova", "Slatina, Croatia", "Alexandria, Egypt", "Arad, Israel",
    "Alexandria, VA, US",
])
def test_a_romanian_name_in_another_country_is_not_romania(job_location):
    assert not located(job_location, "romania")
    assert not located(job_location, "arad" if "Arad" in job_location else "calarasi")


@pytest.mark.parametrize("job_location, search, expected", [
    # Europe still takes European countries, and only those.
    ("Porto Velho, RO, BR", "europe", False),
    ("Alexandria, Egypt", "eu", False),
    ("Ro, Emilia-Romagna, Italy", "europe", True),
    ("Slatina, Croatia", "europe", True),
    ("München, BY, DE", "europe", True),
    # A city named alongside its own country is unaffected.
    ("Arad, Romania", "arad", True),
    ("Cluj-Napoca, Cluj, Romania", "cluj", True),
    ("Florești, CJ, RO", "romania", True),
    ("Berlin, Germany", "europe", True),
    ("Remote: Romania, Poland, Hungary", "cluj", False),
    ("Bucharest, Romania; Warsaw, Poland", "bucharest", True),
])
def test_named_countries(job_location, search, expected):
    assert located(job_location, search) is expected


@pytest.mark.parametrize("job_location, search, expected", [
    # Several places: a country named for one does not veto the others.
    ("Cluj-Napoca / Munich, Germany", "cluj", True),
    ("Cluj-Napoca / Munich, Germany", "munich", True),
    ("Bucharest, Budapest (Hungary)", "bucharest", True),
    ("Cluj, Bucharest, Warsaw (Poland)", "cluj", True),
    ("Cluj, Bucharest, Warsaw (Poland)", "warsaw", True),
    ("Bucharest / Chisinau, Moldova", "bucharest", True),
    ("Bucharest or Remote (USA)", "bucharest", True),
    ("Remote (USA/Europe)", "remote", True),
    # ... while it still applies to its own place.
    ("Arad (Israel), Haifa", "arad", False),
    ("Chisinau / Calarasi, Moldova", "calarasi", False),
    # Moldova is also the Romanian region.
    ("Iasi, Moldova", "iasi", True),
    ("Suceava, Moldova", "romania", True),
])
def test_several_places_in_one_location(job_location, search, expected):
    assert located(job_location, search) is expected


@pytest.mark.parametrize("job_location, search, expected", [
    # US "City, ST" with no country: not the Romanian or European namesake.
    ("Alexandria, VA", "romania", False),
    ("Paris, TX", "europe", False),
    ("Rome, GA", "eu", False),
    # ... but Romanian county codes stay Romanian (AR is also Arkansas).
    ("Arad, AR", "arad", True),
    ("Constanta, CT", "romania", True),
    # "US" as a word in a sentence is not the country.
    ("Hybrid in Bucharest, US company", "bucharest", True),
    ("Remote, US", "remote", False),
    # Remote for another continent, by region.
    ("Remote, LATAM", "remote", False),
    ("Remote (Americas)", "remote", False),
    ("Remote - APAC", "remote", False),
    # ... and a European region anywhere in the string.
    ("Remote US/EU", "remote", True),
    ("Remote - EU", "europe", True),
    ("EMEA / Remote", "europe", True),
])
def test_us_states_regions_and_eu(job_location, search, expected):
    assert located(job_location, search) is expected


@pytest.mark.parametrize("job_location, expected", [
    # RemoteOK: remote, but for someone living elsewhere.
    ("Remote - US only", False),
    ("Remote (USA)", False),
    ("Remote, United States", False),
    ("Remote - Canada only", False),
    ("Remote, Brazil", False),
    # ... and the remote a Romanian user can take.
    ("Remote", True),
    ("Worldwide", True),
    ("Remote (USA, Europe)", True),
    ("Remote - Germany", True),
    ("Romania (Remote)", True),
    ("Remote, EMEA", True),
])
def test_remote_restricted_to_another_continent_is_not_remote(job_location, expected):
    assert located(job_location, "remote") is expected


def test_nationwide_postings_do_not_reach_foreign_city_searches():
    assert not located("Romania", "berlin")
    assert not located("Romania", "remote")


@pytest.mark.parametrize("job_location", [
    "Romania", "România (Remote)", "Remote, Romania", "Hybrid - Romania", "Remote (RO)",
])
def test_a_city_search_takes_country_level_postings_only_with_remote_ok(job_location):
    """Product review on prod data: "Romania"-only postings added 8-12 mostly
    unrelated candidates ("Freelance Viral AI Template Creator") to each Cluj
    and Timisoara intern search."""
    assert not search_location_ok(job_location, ["cluj"])
    assert search_location_ok(job_location, ["cluj"], remote_ok=True)
    assert search_location_ok(job_location, ["romania"])
    if "remote" not in job_location.lower() and "(ro)" not in job_location.lower():
        # Not remote: no reason to offer it to a Berlin search.
        assert not search_location_ok(job_location, ["berlin"], remote_ok=True)


def test_remote_postings_reach_a_city_search_only_with_remote_ok():
    assert not search_location_ok("Remote", ["cluj"])
    assert not search_location_ok("Worldwide", ["timisoara"])
    assert search_location_ok("Remote", ["cluj"], remote_ok=True)


# ── Excluded keywords and blacklists ──────────────────────────────────


@pytest.mark.parametrize("text, excluded", [
    # \b needs a word character beside it: these never excluded anything.
    ("Senior C++ Developer - embedded", ["c++"]),
    ("C# Backend Engineer", ["C#"]),
    ("Software Engineer .NET", [".net"]),
    # ... nor attached to the word they belong to.
    ("ASP.NET Core Developer", [".net"]),
    ("VB.NET programmer", [".NET"]),
    ("Modern C++17 engineer", ["c++"]),
    ("C#10 / .NET 8", ["c#"]),
    ("ASP.NET Core developer, apply on careers.company.net", [".net"]),
    ("ADO.NET and Entity Framework", [".net"]),
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
    # A side that ends in a letter still needs a word boundary there.
    ("Network engineer", [".net"]),
    # C is not C++ or C#.
    ("Senior C++ Developer", ["c"]),
    ("C# Backend Engineer", ["C"]),
    # Not inside a web address.
    ("Apply on careers.company.net/jobs/123", [".net"]),
    ("Details: https://www.acme.net/careers", [".NET"]),
    ("Send your CV to hr@acme.net", [".net"]),
    ("See https://sales.acme.com/join", ["sales"]),
    ("ObjectiveC++ tooling", ["c++"]),
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
    # The board's label, when the posting does not ask for more.
    ("Python Developer", "entry", "Python, Django, SQL."),
    ("Python Developer", "entry", None),
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
    # eJobs and BestJobs file postings as entry-level along with the other
    # levels the employer ticked: 28 of 52 wrong production DMs came
    # through that label. The years asked for still count.
    ("Python Developer", "entry", "Minimum 5 years of experience with Python."),
    ("IT Channel EMEA Data Analyst", "entry", "3+ years of experience in data analysis."),
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
    # Ten and more: dropped as "not a requirement", so these passed junior.
    ("10+ years of professional experience required", 10),
    ("Minimum 12 ani de experiență în dezvoltare software", 12),
    # ... while a company's age still is not one.
    ("We have over 15 years of experience in fintech.", None),
    ("Avem 12 ani de experiență pe piață.", None),
    # A nice-to-have in brackets is not this mention's tail; "plus" alone
    # is a conjunction.
    ("At least 4 years of experience (5 years is a plus)", 4),
    ("5+ years of experience in Java plus Spring Boot", 5),
    ("Nice to have: 3 years of experience with Kafka", None),
    ("Preferably 3 years of experience in Go", None),
    ("Bonus - 2 years of experience with Rust", None),
    # A wish ending the previous clause, its period lost in scraping.
    ("A degree in CS is strongly preferred 2-4 years of backend engineering experience Strong Git", 2),
    ("Kotlin preferred 5+ years of experience in Java", 5),
    # "Experience" in another sentence, or not about the number.
    ("Over 5 years on the market. No prior experience required", None),
    ("Within 3 years you will grow into a lead. Experience with Selenium a plus", None),
    ("Bachelor degree (4 years). Some experience with SQL", None),
    ("We're 4 years old and have grown our experience in sales", None),
    ("We're 4 years old with experience in sales", None),
    # The requirement written experience-first.
    ("Experience: at least 3 years", 3),
    ("Professional experience (min. 4 years) in Java", 4),
    ("Experience:\n- 3+ years in backend development", 3),
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


@pytest.mark.parametrize("description, years", [
    # Measured on eval/postings.jsonl: the wordings the requirement takes.
    ("Your profile: 5+ years of backend software development experience", 5),
    ("Minimum 5 years .NET software development experience", 5),
    ("Minimum three years of JavaScript (ES6) development experience", 3),
    ("4+ years of iOS development in a team environment", 4),
    ("Essential: 5+ years of hands-on Java and Spring Boot development experience", 5),
    ("Cautam un dezvoltator cu 1–3 ani de practica in React", 1),
    # Years it does not want.
    ("Nu cautam un senior cu 10 ani de experienta. Cautam pe cineva cu 2 ani de experienta", 2),
    ("We are not looking for someone with 8 years of experience", None),
    ("Experienta in domeniul promovarii produselor farmaceutice de minim 2 ani;", 2),
    ("Experienta anterioara in domeniu, pe o pozitie similara - min. 5 ani;", 5),
    ("Experienta minima in contabilitate, 1-2 ani;", 1),
    ("Ideal ar fi sa ai intre 1 si 5 ani de experienta in contabilitate.", 1),
    # The employer's boilerplate.
    ("Xebia is a global partner. With over 25 years of experience and a team of 5,000", None),
    ("Homefile is a Romanian company with over 10 years of experience, dedicated to", None),
    ("Acces la experienta acumulata in peste 10 ani de activitate;", None),
    # Nor the candidate's age or driving licence.
    ("Varsta recomandata: 18–40 ani studii: facultatea nu este obligatorie – experienta", None),
    ("Permis de conducere categoria B de minimum 2 ani; experiență la volan", None),
])
def test_required_years_as_postings_write_it(description, years):
    assert required_years(description) == years


# ── Senior words that are not seniority ───────────────────────────────


@pytest.mark.parametrize("title", [
    "Staff Accountant",            # an entry-level accounting title
    "Lead Generation Specialist",  # sales, not a lead
    "Junior IT Project Manager",   # "manager" is the role; "junior" the level
])
def test_titles_that_are_not_senior(title):
    assert matches_experience_level(title, ["junior"])
    assert not matches_experience_level(title, ["senior"])


@pytest.mark.parametrize("title", [
    "Staff Software Engineer", "Staff Engineer", "Tech Lead", "Team Leader Python",
    "Engineering Manager", "Senior Project Manager",
])
def test_titles_that_are_senior(title):
    assert not matches_experience_level(title, ["junior"])
    assert matches_experience_level(title, ["senior"])


def test_a_junior_manager_reaches_an_entry_keyword_search():
    assert title_is_relevant("Junior IT Project Manager", "junior project manager")
    assert not title_is_relevant("Senior IT Project Manager", "junior project manager")


@pytest.mark.parametrize("title", [
    "Working Student Software Development (m/w/d)", "Student Data Analyst",
])
def test_working_students_are_internships(title):
    assert matches_experience_level(title, ["intern"])
    assert title_is_relevant(title, "software engineering internship")


def test_search_location_ok_is_one_rule_for_match_browse_and_feed():
    assert search_location_ok("Cluj-Napoca", ["iasi", "cluj"])
    assert search_location_ok("Anywhere at all", [])
    assert not search_location_ok("Remote", ["cluj"])
    assert search_location_ok("Remote", ["cluj"], remote_ok=True)
    assert not search_location_ok("Remote - US only", ["cluj"], remote_ok=True)
    assert not search_location_ok(None, ["cluj"])


@pytest.mark.parametrize("description, years", [
    # The number and "experience" in different clauses.
    ("Fixed-term contract for 2 years, experience with Excel is a plus", None),
    ("Contract pe 2 ani, experiența în vânzări constituie un avantaj", None),
    ("Bachelor's degree (4 years), experience with SQL", None),
    ("Studii superioare (3 ani licență), experiență în contabilitate", None),
    ("Salary increases every 2 years, experience in sales", None),
    ("Training program lasting 2 years, experience not required", None),
    # ... but a requirement written experience-first may cross one.
    ("Experienta minima in contabilitate, 1-2 ani", 1),
    ("Experience with Excel is a plus, 2 years in finance", None),
    # The candidate's age, per mention.
    ("Ai peste 18 ani și experiență în vânzări", None),
    ("Must be 18 years or older with experience driving", None),
    ("Vârsta minimă 18 ani, experiență de 1 an", 1),
    ("20+ years of experience in enterprise architecture", 20),
    # Ways of writing the requirement that were missed.
    ("Experience 3+ years in Java", 3),
    ("Experiență 2 ani", 2),
    ("Experiență minimă 2 ani", 2),
    ("Experience\n- 3+ years", 3),
    ("Experience. 5 years.", 5),
    ("10+ years required", 10),
    ("7+ yrs exp.", 7),
    # Negation on either side of the number.
    ("We do not require 5 years of experience", None),
    ("You don't need 5 years of experience to apply", None),
    ("Experience of 5 years is not required", None),
    ("Nu este necesară experiența de 3 ani", None),
    ("No need to have 3 years of experience", None),
    # The verifier's general phrasings.
    ("Required: 3+ years in software development", 3),
    ("Have worked 3 years as a developer", 3),
    ("8+ years in the industry, building teams from scratch", 8),
    ("Ideal: 3 years of experience", None),
    ("Experience with Java for over 20 years in our team", None),
])
def test_required_years_reads_one_clause(description, years):
    assert required_years(description) == years


# ── CS-family keywords want CS titles (product review, prod queue) ────


@pytest.mark.parametrize("title", [
    "Game Tester - Cluj-Napoca Office - Day & Night Shift",
    "Business Developer",
    "Business Developer in Bistrița-Năsăud | Cluj",
    "Romanian Linguistic Evaluator & Functional Tester",
    "Freelance Viral AI Template Creator",
    "AI video creator",
    "Video Data Annotation Specialist - AI Training",
    "Customer Support with English & DE/NL/PL/IT",
    "Programator CNC",
    "Working Student Industrial Engineering",
])
def test_non_cs_titles_do_not_reach_a_cs_internship_search(title):
    # Filed as entry-level by the board, which used to be enough.
    assert not title_is_relevant(title, "computer science internship", "entry")
    assert not title_is_relevant(title, "computer science internship", "intern")


@pytest.mark.parametrize("title", [
    "Software Engineer Intern", "Junior QA Tester", "QA Game Tester Intern",
    "Data Analyst Intern", "Game Developer Intern", "Working Student Software Development",
    "Internship - Backend Developer (Java)", "Stagiu IT - Dezvoltator Software",
])
def test_cs_intern_titles_still_reach_it(title):
    assert title_is_relevant(title, "computer science internship", "intern")


# ── Excluded level words read the title only ──────────────────────────


@pytest.mark.parametrize("excluded, senior_title", [
    (["senior"], "Senior Java Developer"), (["Senior"], "Senior Java Developer"),
    (["lead"], "Tech Lead Java"), (["sr"], "Sr. Java Developer"),
    (["manager"], "Engineering Manager"), (["senior level"], "Java Developer, Senior Level"),
])
def test_an_excluded_level_word_is_about_the_title(excluded, senior_title):
    """Excluding "senior" removed 4 of the eval set's 26 intern/junior
    postings: their descriptions say "mentored by senior engineers"."""
    description = (
        "You will be mentored by senior engineers and a team lead "
        "(manager: Ana, Sr. PM), senior level guidance."
    )
    assert not matches_excluded("Junior Java Developer", description, excluded)
    assert matches_excluded(senior_title, description, excluded)


def test_other_excluded_words_still_read_the_description():
    assert matches_excluded("Account Executive", "Sales targets, B2B sales", ["sales"])
    assert matches_excluded("Developer", "Night shifts required", ["night shifts"])
    assert not matches_excluded("Developer", None, ["sales"])


def test_the_entry_label_does_not_override_the_years_for_a_keyword_either():
    asks = "Minimum 4 years of experience in QA."
    assert not title_is_relevant("QA Engineer", "junior qa", "entry", asks)
    assert title_is_relevant("QA Engineer", "junior qa", "entry", "Manual testing.")


@pytest.mark.parametrize("title, passes", [
    # Only the board's label says entry-level: the title must be CS itself.
    ("Consultant IT SAP IS-U (Telemuncă) (ID 593)", False),
    ("Specialist in continut web si digital", False),
    ("Python Developer", True),
    ("QA Engineer", True),
    ("Junior IT Support", True),  # the title says junior itself
])
def test_a_board_entry_label_needs_a_cs_title(title, passes):
    for label in ("entry", "intern"):
        assert title_is_relevant(title, "computer science internship", label) is passes


# ── Production DMs judged wrong (delivery review, 2026-10-16) ─────────


def test_it_in_a_list_of_language_codes_is_italian():
    title = "Oportunitati BPO: HU / DE / NL / FR / IT / ES -Remote&Hibrid"
    assert not title_is_relevant(title, "computer science internship", "entry")
    assert not title_is_relevant("Customer Care DE/NL/PL/IT", "junior developer")
    assert title_is_relevant("Junior IT Support", "computer science internship")
    assert title_is_relevant("IT Intern / Helpdesk", "computer science internship")
    assert title_is_relevant("AI/ML Technical Lead", "computer science")  # not codes


def test_a_pharma_qa_role_is_not_software_qa():
    pharma = (
        "Studii superioare: Facultatea de Chimie, Farmacie. Minim 2-3 ani experiență în "
        "Producție sau QC. Eliberarea seriilor conform GMP."
    )
    assert not title_is_relevant("QA Specialist", "computer science internship", "entry", pharma)
    software = "Manual and automated testing of web apps; Selenium, API tests."
    assert title_is_relevant("QA Specialist", "computer science internship", "entry", software)
    assert title_is_relevant("QA Specialist", "computer science internship", "entry", None)


@pytest.mark.parametrize("title, company", [
    ("Programe de internship, trainee sau joburi entry-level in IT", "JumpStart - A GenZ Career Hub"),
    ("Internship opportunities in IT", "Some Career Hub"),
    ("Joburi IT pentru studenti", "Acme"),
])
def test_career_hub_ads_are_not_jobs(title, company):
    assert looks_like_an_ad(title, company)


@pytest.mark.parametrize("title, company", [
    ("Software Engineering Internship", "Bitdefender"),
    ("Junior Java Developer", "Endava"),
    ("Internship Program - Data Science", "ING Hubs Romania"),
    # A recruiter called "Career Hub" posting a real role (eval set).
    ("Reporting Analyst", "Career Hub"),
])
def test_real_postings_are_not_ads(title, company):
    assert not looks_like_an_ad(title, company)


def test_an_oregon_remote_posting_is_us_only():
    assert not located("Remote, OR, USA", "remote")
    assert located("Bucharest or Remote (EU)", "remote")


@pytest.mark.parametrize("title", [
    "Software Engineer Backend(Django)", "QA/Test Engineer", "Data Engineer",
])
def test_an_internship_keyword_follows_the_searchs_levels(title):
    """6 of 22 production searches ask for intern AND junior under the
    "computer science internship" keyword; untitled junior tech roles never
    got past the internship keyword."""
    kw = "computer science internship"
    assert title_is_relevant(title, kw, None, None, ["intern", "junior"])
    assert not title_is_relevant(title, kw, None, None, ["intern"])
    assert not title_is_relevant(title, kw)  # no levels known: strict as before
    assert not title_is_relevant(
        title, kw, None, "Minimum 4 years of experience.", ["intern", "junior"],
    )


# ── Owner rule: intern-only searches get training positions only ──────


TRAINING = [
    "Working Student Data Engineering", "Trainee SAP Consultant",
    "Software Engineering Internship", "Stagiu de practică IT", "Apprenticeship Software Developer",
    "Student Programme - Data Analytics",
]
# A graduate programme is a junior position (owner, 2026-10-17).
JUNIOR = [
    "Junior Java Developer", "Software Engineer Backend(Django)", "Graduate Software Engineer",
    "Graduate Programme IT",
]


@pytest.mark.parametrize("title", TRAINING)
def test_an_intern_only_search_takes_training_positions(title):
    kw = "computer science internship"
    assert matches_experience_level(title, ["intern"])
    assert title_is_relevant(title, kw, None, None, ["intern"])


@pytest.mark.parametrize("title", JUNIOR)
def test_an_intern_only_search_takes_no_junior_or_unmarked_role(title):
    kw = "computer science internship"
    assert not matches_experience_level(title, ["intern"])
    # The scrape filter too, not only the match stage.
    assert not title_is_relevant(title, kw, None, None, ["intern"])
    # Not through an "entry" label either.
    assert not title_is_relevant(title, kw, "entry", None, ["intern"])
    assert not matches_experience_level(title, ["intern"], "entry")


@pytest.mark.parametrize("title", TRAINING + JUNIOR)
def test_with_junior_chosen_junior_roles_join_the_training_ones(title):
    kw = "computer science internship"
    assert matches_experience_level(title, ["intern", "junior"])
    assert title_is_relevant(title, kw, None, None, ["intern", "junior"])


@pytest.mark.parametrize("title", ["Senior Java Developer", "Medior QA Engineer", "Software Engineer II"])
def test_mid_and_senior_reach_neither(title):
    for levels in (["intern"], ["intern", "junior"], ["junior"]):
        assert not matches_experience_level(title, levels)
