"""Filtering and relevance logic for job matching.

All filters are pure functions that operate on individual job dicts or
DataFrames — no DB or IO dependencies.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from typing import Any

import pandas as pd

# ── Keyword expansion ─────────────────────────────────────────────────
# Broad user-facing terms -> specific job board search queries.

KEYWORD_EXPANSIONS: dict[str, list[str]] = {
    "computer science internship": [
        "software engineer intern",
        "software developer intern",
        "web developer intern",
        "data scientist intern",
        "data analyst intern",
        "data engineer intern",
        "machine learning intern",
        "AI intern",
        "backend developer intern",
        "frontend developer intern",
        "full stack intern",
        "devops intern",
        "cloud engineer intern",
        "cybersecurity intern",
        "IT intern",
        "QA intern",
        "computer science intern",
        "junior developer",
        "internship",
    ],
    "computer science": [
        "software engineer",
        "software developer",
        "web developer",
        "data scientist",
        "data analyst",
        "data engineer",
        "machine learning engineer",
        "AI engineer",
        "backend developer",
        "frontend developer",
        "full stack developer",
        "devops engineer",
        "cloud engineer",
        "cybersecurity analyst",
        "systems administrator",
        "QA engineer",
        "junior developer",
        "graduate software",
    ],
    "cs entry level": [
        "junior software engineer",
        "junior developer",
        "junior data analyst",
        "entry level software",
        "entry level developer",
        "entry level data",
        "graduate software engineer",
        "graduate developer",
        "trainee developer",
        "trainee software",
        "associate software engineer",
    ],
    "tech internship": [
        "software engineer intern",
        "data analyst intern",
        "IT intern",
        "devops intern",
        "cloud intern",
        "cybersecurity intern",
        "product manager intern",
        "UX designer intern",
        "tech intern",
    ],
    "data science": [
        "data scientist",
        "data analyst",
        "machine learning engineer",
        "data engineer",
        "business intelligence analyst",
        "AI engineer",
        "NLP engineer",
        "statistician",
    ],
    "software engineering": [
        "software engineer",
        "software developer",
        "backend developer",
        "frontend developer",
        "full stack developer",
        "mobile developer",
        "platform engineer",
        "web developer",
        "application developer",
    ],
}


# ── Location aliases ──────────────────────────────────────────────────

# Matched as whole words on folded text (see location_matches), so the
# spellings with and without diacritics or hyphens are one entry each.
# Indeed writes Romanian locations as "București, IF, RO" with no country
# name, so the city list is what catches them for "romania" (and the
# Europe-wide wildcards, which used to miss Arad, Craiova, Oradea, ...).
ROMANIAN_CITIES: tuple[str, ...] = (
    "bucharest", "bucuresti", "cluj", "timisoara", "iasi", "brasov", "sibiu",
    "craiova", "constanta", "oradea", "arad", "targu mures", "tirgu mures",
    "baia mare", "pitesti", "ploiesti", "galati", "suceava", "bacau",
    "braila", "buzau", "satu mare", "alba iulia", "deva", "hunedoara",
    "bistrita", "botosani", "focsani", "piatra neamt", "ramnicu valcea",
    "targu jiu", "drobeta turnu severin", "resita", "slatina", "tulcea",
    "zalau", "vaslui", "giurgiu", "slobozia", "calarasi", "alexandria",
    "sfantu gheorghe", "miercurea ciuc", "medias", "voluntari", "otopeni",
    "ilfov", "floresti",
)

LOCATION_ALIASES: dict[str, dict[str, Any]] = {
    "cluj": {"search": "Cluj-Napoca, Romania", "match": ["cluj"]},
    "cluj napoca": {"search": "Cluj-Napoca, Romania", "match": ["cluj"]},
    "cluj-napoca": {"search": "Cluj-Napoca, Romania", "match": ["cluj"]},
    "bucharest": {
        "search": "Bucharest, Romania",
        "match": ["bucharest", "bucuresti", "bucurești"],
    },
    "bucuresti": {
        "search": "Bucharest, Romania",
        "match": ["bucharest", "bucuresti", "bucurești"],
    },
    "timisoara": {
        "search": "Timisoara, Romania",
        "match": ["timisoara", "timișoara"],
    },
    "iasi": {"search": "Iasi, Romania", "match": ["iasi", "iași"]},
    "brasov": {"search": "Brasov, Romania", "match": ["brasov", "brașov"]},
    "sibiu": {"search": "Sibiu, Romania", "match": ["sibiu"]},
    "craiova": {"search": "Craiova, Romania", "match": ["craiova"]},
    "constanta": {
        "search": "Constanta, Romania",
        "match": ["constanta", "constanța"],
    },
    "oradea": {"search": "Oradea, Romania", "match": ["oradea"]},
    "targu mures": {"search": "Targu Mures, Romania", "match": ["targu mures", "tirgu mures"]},
    "tirgu mures": {"search": "Targu Mures, Romania", "match": ["targu mures", "tirgu mures"]},
    "dublin": {"search": "Dublin, Ireland", "match": ["dublin"]},
    "london": {"search": "London, United Kingdom", "match": ["london"]},
    "berlin": {"search": "Berlin, Germany", "match": ["berlin"]},
    "amsterdam": {"search": "Amsterdam, Netherlands", "match": ["amsterdam"]},
    # Indeed writes Romanian locations as "București, IF, RO" with no
    # country name, so the city list is what catches them.
    # "RO" as a word: Indeed's "Florești, CJ, RO" names no listed city.
    "romania": {"search": "Romania", "match": ["romania", "ro", *ROMANIAN_CITIES]},
    # Remote. RemoteOK writes "Worldwide"; Romanian boards write "de acasă",
    # "la distanță" or "telemuncă".
    "remote": {
        "search": "Remote",
        "match": [
            "remote", "work from home", "wfh", "anywhere", "worldwide",
            "home office", "homeoffice", "telemunca", "la distanta", "de acasa",
        ],
    },
    # Central / Eastern Europe — popular with Romanian CS students
    "prague": {"search": "Prague, Czech Republic", "match": ["prague", "praha"]},
    "warsaw": {"search": "Warsaw, Poland", "match": ["warsaw", "warszawa"]},
    "budapest": {"search": "Budapest, Hungary", "match": ["budapest"]},
    "krakow": {"search": "Krakow, Poland", "match": ["krakow", "kraków", "cracow"]},
    "vienna": {"search": "Vienna, Austria", "match": ["vienna", "wien"]},
    # Western Europe tech hubs
    "munich": {"search": "Munich, Germany", "match": ["munich", "münchen"]},
    "paris": {"search": "Paris, France", "match": ["paris"]},
    "barcelona": {"search": "Barcelona, Spain", "match": ["barcelona"]},
    "zurich": {"search": "Zurich, Switzerland", "match": ["zurich", "zürich"]},
    # Europe-wide wildcards — popular with students open to relocating
    "europe": {
        "search": "Europe",
        "match": [
            "romania", "ro", *ROMANIAN_CITIES,
            "germany", "berlin", "munich", "münchen", "hamburg", "frankfurt",
            "netherlands", "amsterdam", "eindhoven",
            "ireland", "dublin",
            "united kingdom", "london", "manchester", "edinburgh",
            "france", "paris", "lyon",
            "spain", "barcelona", "madrid",
            "poland", "warsaw", "warszawa", "krakow", "kraków", "wroclaw",
            "czech", "prague", "praha", "brno",
            "hungary", "budapest",
            "austria", "vienna", "wien",
            "switzerland", "zurich", "zürich",
            "sweden", "stockholm", "gothenburg",
            "denmark", "copenhagen",
            "finland", "helsinki",
            "norway", "oslo",
            "portugal", "lisbon", "porto",
            "italy", "milan", "rome",
            "belgium", "brussels",
            "luxembourg",
            "europe",
        ],
    },
    "eu": {
        "search": "Europe",
        "match": [
            "romania", "ro", *ROMANIAN_CITIES,
            "germany", "berlin", "munich", "münchen", "hamburg", "frankfurt",
            "netherlands", "amsterdam", "eindhoven",
            "ireland", "dublin",
            "france", "paris", "lyon",
            "spain", "barcelona", "madrid",
            "poland", "warsaw", "warszawa", "krakow", "kraków", "wroclaw",
            "czech", "prague", "praha", "brno",
            "hungary", "budapest",
            "austria", "vienna", "wien",
            "switzerland", "zurich", "zürich",
            "sweden", "stockholm", "gothenburg",
            "denmark", "copenhagen",
            "finland", "helsinki",
            "norway", "oslo",
            "portugal", "lisbon", "porto",
            "italy", "milan", "rome",
            "belgium", "brussels",
            "luxembourg",
            "europe",
        ],
    },
}

# ── Compiled regexes for title relevance ──────────────────────────────

# Every alternative ends on a word boundary: without it "intern" matched
# "International Sales Manager" and "Internal Auditor", which then passed
# intern-only searches. The Romanian terms are what local boards use
# (stagiar, practicant, absolvent, fără experiență); \b is Unicode-aware,
# so "practică" ends where it should.
ENTRY_LEVEL_RE = re.compile(
    r"(?i)\b(?:intern(?:ship)?s?|junior|jr\.?|graduates?|grad|entry.?level|"
    r"trainees?|traineeship|academy|apprentice(?:ship)?s?|placements?|co.?op|summer|"
    r"stagiar\w*|stagi[ui]|practic(?:ant\w*|[aă])|absolvent\w*|"
    r"[iî]ncep[aă]tor\w*|debutant\w*|f[aă]r[aă]\s+experien[tț][aă])(?!\w)"
)

# "Șef" is Romanian for head/chief ("Contabil Șef", "Șef de echipă").
SENIOR_RE = re.compile(
    r"(?i)\b(?:senior|sr\.?|lead(?:er)?|principal|staff|director|"
    r"manager|head\b|vp|chief|architect|[sșş]ef[aă]?)\b"
)

# Prefix stems spell out their endings: "tech(?:nolog)?\b" never matched
# "Technology", and "comput\b" never matched "Computing". "IT" is matched
# case-sensitively so the pronoun "it" in a title does not count.
TECH_RE = re.compile(
    r"(?i)\b(?:software|develop(?:ment|ers?)|engineer(?:ing|s)?|"
    r"programm(?:er|ing)s?|programator\w*|dezvoltator\w*|coder|coding|"
    r"data|machine.?learning|ml|ai|deep.?learning|nlp|"
    r"devops|dev.?ops|cloud|cyber\w*|security|"
    r"front.?end|back.?end|full.?stack|"
    r"web|python|java|javascript|typescript|\.net|c\+\+|c#|ruby|golang|rust|kotlin|swift|"
    r"qa|sdet|test.?auto\w*|quality.?assur\w*|tester\w*|testare|"
    r"sql|database|dba|"
    r"network\w*|sre|site.?reliab\w*|infrastructure|"
    r"mobile|ios|android|flutter|react|angular|vue|"
    r"tech|technolog(?:y|ies|ie|ii)|computers?|computing|informatic\w*|"
    r"(?-i:IT))(?!\w)"
)

# Titles that match TECH_RE only through generic words ("engineer",
# "technology", "AI") but belong to other industries. Observed leaking
# into production deliveries: industrial engineering and marketing roles.
NON_CS_DOMAIN_RE = re.compile(
    r"(?i)\b(?:maintenance|mechanic(?:al)?|electric(?:al|ian)?|civil|chemical|"
    r"welding|hvac|automotive|manufacturing|process\s+technology|"
    r"marketing|sales|vanzari|v[âa]nz[ăa]ri|antreprenorial\w*|"
    r"recruit(?:er|ment)|accountant|contabil\w*|hr\b|logistics?|warehouse|"
    # Romanian trades that eJobs files under its IT departments
    # ("Tehnician Infrastructura IT & CCTV", "Inginer Mentenanta").
    r"tehnician\w*|technicians?|cctv|mentenan\w*|mecanic\w*|"
    r"construc(?:tii|ții|tion)\w*|[sș]antier\w*|instala[tț]i\w*|laborator\w*|"
    r"bim|revit)\b"
)

# Unambiguous CS signals that override a negative-domain hit
# ("Junior Software Engineer - Process Automation" is still ours).
STRONG_CS_RE = re.compile(
    r"(?i)\b(?:software|develop(?:er|ment)|programm(?:er|ing)|programator\w*|"
    r"dezvoltator\w*|front.?end|back.?end|full.?stack|devops|cyber|"
    r"python|java(?:script)?|typescript|\bqa\b|\bsdet\b|"
    r"data\s+(?:scien|analy|engineer)|machine.?learning|informatic\w*)"
)


# ── Pure functions ────────────────────────────────────────────────────


def fold(text: str) -> str:
    """Lower-case, diacritic-free text for location comparisons.

    Boards spell the same place several ways: "București", "Bucuresti",
    "Cluj-Napoca, România", and Romanian text mixes the comma-below ș/ț
    with the cedilla ş/ţ. Matching raw substrings meant "romania" never
    matched BestJobs' "România", so those postings failed a "romania"
    search.
    """
    decomposed = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def normalize_location(location: str) -> tuple[str, list[str]]:
    """Return (search_location, match_substrings) for a user-provided location.

    "City, Country" as typed into the free-text field ("Cluj-Napoca,
    Romania", "Bucuresti, RO") is looked up by its city: matched as the
    whole string it found nothing but postings that happened to repeat the
    country the same way ("Cluj-Napoca" alone, the common case, failed).
    """
    normalized = location.lower().strip()
    alias = _alias(normalized)
    if alias:
        return alias["search"], alias["match"]
    city = city_only(normalized)
    if city and city != normalized:
        alias = _alias(city)
        if alias:
            return alias["search"], alias["match"]
        return location, [city]
    return location, [normalized]


def _alias(location: str) -> dict[str, Any] | None:
    return (
        LOCATION_ALIASES.get(location)
        or LOCATION_ALIASES.get(fold(location))
        or LOCATION_ALIASES.get(_place(location))
    )


def is_romanian_location(location: str) -> bool:
    """Whether a Romania-only board can serve this search location.

    True for Romanian cities, "Romania" and the Europe-wide wildcards
    (Romania is in them); False for known foreign cities and "remote".
    A place name the alias table does not know is most likely a Romanian
    town typed by a Romanian user, so it is left to the board's own
    lookup.
    """
    key = fold(location.strip())
    if key in ("europe", "eu"):
        return True
    search, _subs = normalize_location(location)
    if "romania" in fold(search):
        return True
    return key not in LOCATION_ALIASES


def expand_keyword(keyword: str) -> list[str]:
    """Return a list of specific search terms for a broad keyword."""
    normalized = keyword.lower().strip()
    return KEYWORD_EXPANSIONS.get(normalized, [keyword])


def location_matches(job_location: str, match_substrings: list[str]) -> bool:
    """Check if a job's location fuzzy-matches any expected substrings.

    Both sides are folded (see fold), so "România" matches "romania".
    Jobs with missing/NaN location data are rejected: when the user has
    explicitly restricted by location, we cannot confirm a match without
    data, so we exclude rather than leak unrelated postings.
    """
    if not job_location or pd.isna(job_location) or str(job_location).lower() == "nan":
        return False
    loc = _place(job_location)
    if any(_place_pattern(sub).search(loc) for sub in match_substrings if sub):
        return True
    # "Romania" with no city is a nationwide (usually remote-in-Romania)
    # posting: open to someone searching any Romanian city.
    return _ROMANIA_ONLY_RE.fullmatch(loc) is not None and any(
        _place(sub) in _ROMANIAN_PLACES for sub in match_substrings if sub
    )


def _place(text: str) -> str:
    """Folded, with the separators boards put inside names as spaces:
    "Târgu-Mureș" and "Targu Mures" are one place."""
    return " ".join(re.sub(r"[-_/.]", " ", fold(text)).split())


@functools.lru_cache(maxsize=1024)
def _place_pattern(sub: str) -> re.Pattern[str]:
    """`sub` as whole words. A plain substring test put "Kiasi" in Iasi and
    "Porto Alegre" in Europe."""
    return re.compile(r"(?<![a-z0-9])" + re.escape(_place(sub)) + r"(?![a-z0-9])")


_ROMANIAN_PLACES = frozenset(_place(city) for city in ROMANIAN_CITIES)
# A location naming the country and nothing more specific: "Romania",
# "România (Remote)", "Remote, RO", "Hybrid - Romania".
_ROMANIA_ONLY_RE = re.compile(
    r"(?:(?:remote|hybrid|hibrid|on site|onsite|ro|romania)[\s,;()|]*)*"
    r"(?:romania|ro)[\s,;()|]*(?:(?:remote|hybrid|hibrid|on site|onsite)[\s,;()|]*)*"
)


# Whole words or word starts: the substring test read "international
# relations" and "internal audit" as internship searches, "retail",
# "training" and "supply chain" as AI ones ("ai" inside the word), and so
# held them to tech titles, which none of their postings had.
_ENTRY_KEYWORD_RE = re.compile(
    r"(?i)\b(?:intern(?:ship)?s?\b|entry|junior|graduate|trainee|stagi|"
    r"practic|absolvent|[iî]ncep[aă]tor)"
)
_TECH_KEYWORD_RE = re.compile(
    r"(?i)\b(?:computer|software|tech|data|engineer|developer|devops|cyber|"
    r"cloud|programator|dezvoltator|informatic|(?:ml|ai|it)\b)"
)


# The internship subset of _ENTRY_KEYWORD_RE (see title_is_relevant).
_INTERN_KEYWORD_RE = re.compile(r"(?i)\b(?:intern(?:ship)?s?\b|trainee|stagi|practic)")


def keyword_wants_entry_level(keyword: str) -> bool:
    """Check if the subscription keyword implies entry-level/intern jobs."""
    return bool(_ENTRY_KEYWORD_RE.search(keyword))


def keyword_wants_tech(keyword: str) -> bool:
    """Check if the subscription keyword implies tech/CS domain.

    Not for a keyword that names another engineering or business field
    itself ("civil engineer", "marketing data"): holding those to CS
    titles rejected every posting they were looking for.
    """
    kw = keyword.lower().strip()
    if kw in KEYWORD_EXPANSIONS:
        return True
    if not _TECH_KEYWORD_RE.search(kw):
        return False
    return not (NON_CS_DOMAIN_RE.search(kw) and not STRONG_CS_RE.search(kw))


def title_is_relevant(
    title: str,
    keyword: str,
    experience_level: str | None = None,
    description: str | None = None,
) -> bool:
    """Check if a job title is relevant for the given subscription keyword.

    `experience_level` is the board's own seniority label when it has one
    (RawJob.experience_level). An entry-level label satisfies an
    entry-level keyword even when the title does not say "junior", which
    is how most Romanian boards title their entry-level postings; a
    senior-sounding title still disqualifies.

    An internship keyword wants an internship or entry-level title (or
    label). A junior one ("junior developer", "absolvent") also takes the
    unmarked postings the junior level takes (_is_junior, with
    `description` for the years asked): requiring the word "junior" kept
    a handful of the eval set's student-level postings.
    """
    if not title or pd.isna(title):
        return False
    title = str(title)
    want_entry = keyword_wants_entry_level(keyword)
    want_tech = keyword_wants_tech(keyword)

    if not want_entry and not want_tech:
        return True  # No filtering for generic keywords

    if want_entry:
        board_says_entry = experience_level in ("intern", "entry")
        says_entry = (
            (board_says_entry or ENTRY_LEVEL_RE.search(title))
            and not SENIOR_RE.search(title)
        )
        if not says_entry and not (
            not _INTERN_KEYWORD_RE.search(keyword)
            and _is_junior(title.lower(), experience_level, description)
        ):
            return False
    if want_tech:
        if not TECH_RE.search(title):
            return False
        # Generic words ("engineer", "technology", "AI") also appear in
        # industrial/marketing roles — reject those unless the title
        # carries an unambiguous CS signal.
        if NON_CS_DOMAIN_RE.search(title) and not STRONG_CS_RE.search(title):
            return False
    return True


def relevance_predicate(keywords: list[str]):
    """title, experience_level[, description] -> bool: relevant for any of `keywords`."""

    def relevant(
        title: str, experience_level: str | None = None, description: str | None = None,
    ) -> bool:
        return any(
            title_is_relevant(title, kw, experience_level, description) for kw in keywords
        )

    return relevant


def matches_excluded_keywords(text: str, excluded: list[str]) -> bool:
    """Return True if text contains any of the excluded keywords.

    Uses word-boundary matching so excluding "sales" won't reject
    "wholesale" but will reject "Sales Manager". Both sides are folded,
    so "vanzari" excludes "Vânzări" and the reverse. The boundary is "no
    letter or digit on either side" rather than \\b, which needs a word
    character next to it: excluding "C++", "C#" or ".NET" never matched.
    """
    if not excluded or not text:
        return False
    folded = fold(text)
    return any(
        _excluded_pattern(kw).search(folded) for kw in excluded if kw and kw.strip()
    )


@functools.lru_cache(maxsize=1024)
def _excluded_pattern(keyword: str) -> re.Pattern[str]:
    # A phrase matches across any whitespace (a line break in a description).
    words = [re.escape(word) for word in fold(keyword).split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)")


def matches_company_blacklist(company: str, blacklist: list[str]) -> bool:
    """Return True if company matches any blacklisted name.

    Uses substring matching so "Google" blocks "Google Ireland"; folded,
    so "Ţiriac" blocks "Tiriac Holdings".
    """
    if not blacklist or not company:
        return False
    company_folded = fold(company).strip()
    return any(
        fold(bl).strip() in company_folded for bl in blacklist if bl and bl.strip()
    )


# Strictly internships/traineeships — NOT junior/graduate/entry-level.
# Includes the Romanian terms local boards use (stagiar, practicant).
# Ends on a word boundary for the same reason as ENTRY_LEVEL_RE.
INTERN_RE = re.compile(
    r"(?i)\b(?:intern(?:ship)?s?|trainees?|traineeship|apprentice(?:ship)?s?|"
    r"placements?|co.?op|summer|academy|stagiar\w*|stagi[ui]|practicant\w*|"
    r"practic[aă])(?!\w)"
)

# Entry-level titles that are not internships: the "junior" level. The
# same Romanian terms as ENTRY_LEVEL_RE ("fără experiență", "începător"),
# which the junior level used to leave out.
JUNIOR_RE = re.compile(
    r"(?i)\b(?:junior|jr\.?|entry.?level|new\s+grads?|graduates?|absolvent\w*|"
    r"[iî]ncep[aă]tor\w*|debutant\w*|f[aă]r[aă]\s+experien[tț][aă]|"
    r"no\s+experience)(?!\w)"
)

# Past junior, short of SENIOR_RE: "Mid-Level", "Medior", "Engineer II".
MID_TITLE_RE = re.compile(
    r"(?i)\b(?:mid(?:dle)?(?:.?level)?|medior|intermediate|experienced|expert|"
    r"ii|iii|iv)\b"
)

# Required experience as postings state it: "3+ years", "minimum 2-4
# years", "two (2) years", "minim 3 ani", "un an de experiență". Only
# counted near the word "experience" (see required_years).
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9,
    "un": 1, "unu": 1, "doi": 2, "doua": 2, "trei": 3, "patru": 4, "cinci": 5,
}
_YEARS_RE = re.compile(
    r"(?<![\w.,])(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")"
    r"(?:\s*\(\d{1,2}\))?\s*\+?\s*"
    r"(?:(?:-|–|to|la|sau|or)\s*\d{1,2}\s*\+?\s*)?"
    r"(?:years?|yrs?|ani|an)\b"
)
# A posting asking for this many years is not one a student can apply to.
# Two is still junior on the boards (the eval set's junior postings ask
# for up to two years); three is where mid-level postings start.
JUNIOR_MAX_YEARS = 2

EXPERIENCE_LEVEL_CHOICES = ("any", "intern", "junior", "mid", "senior")
# What people type into the bot's /edit or the API for the same levels.
_LEVEL_ALIASES = {
    "internship": "intern", "internships": "intern", "stagiu": "intern",
    "practica": "intern", "trainee": "intern",
    "entry": "junior", "entry level": "junior", "entry-level": "junior",
    "graduate": "junior", "absolvent": "junior", "jr": "junior",
    "mid-level": "mid", "mid level": "mid", "middle": "mid", "medior": "mid",
    "senior+": "senior", "lead": "senior", "sr": "senior",
    "all": "any",
}


def canonical_level(level: str) -> str | None:
    """One of EXPERIENCE_LEVEL_CHOICES for a typed level; None if unknown."""
    key = fold(level).strip()
    if key in EXPERIENCE_LEVEL_CHOICES:
        return key
    return _LEVEL_ALIASES.get(key)


# A mention that is a wish, not a requirement: "1 year of Kafka is a plus".
_NICE_TO_HAVE_RE = re.compile(
    r"\b(?:a plus|plus\b|nice to have|preferred|preferably|advantage|bonus|"
    r"ideally|avantaj|constituie un plus|reprezinta un plus|de preferat)"
)


def required_years(description: str | None) -> int | None:
    """The years of experience a posting requires; None if unstated.

    Only numbers next to "experience"/"experiență" count, and only 1-9:
    "a company with 20 years on the market" and "a 1-year contract" are
    not requirements. Nice-to-haves in the same sentence are skipped, and
    of the rest the largest wins: "5+ years of experience overall, 2 years
    with Docker" asks for five.
    """
    if not description:
        return None
    text = fold(description)
    found: list[int] = []
    for m in _YEARS_RE.finditer(text):
        window = text[max(0, m.start() - 80) : m.end() + 60]
        if "experien" not in window:
            continue
        # The rest of this mention's sentence (or bullet).
        tail = re.split(r"[.;\n•]|,\s", text[m.end() : m.end() + 120], maxsplit=1)[0]
        if _NICE_TO_HAVE_RE.search(tail):
            continue
        raw = m.group(1)
        years = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
        if 1 <= years <= 9:
            found.append(years)
    return max(found) if found else None


def _is_junior(title: str, experience_level: str | None, description: str | None) -> bool:
    """A non-internship role a graduate can apply to.

    Most postings name no level at all ("QA Engineer", "Data Engineer"), and
    most of those take graduates: matching only titles that say "junior"
    found 15 of the 112 student-level postings in the eval set
    (eval/postings.jsonl). An unmarked posting is junior unless the board
    files it as mid/senior or it asks for more than JUNIOR_MAX_YEARS.
    """
    if JUNIOR_RE.search(title):
        return True
    # An explicit senior title outranks the board's label, which keeps the
    # most junior of the levels the employer ticked.
    if SENIOR_RE.search(title) or INTERN_RE.search(title) or MID_TITLE_RE.search(title):
        return False
    if experience_level == "entry":
        return True
    if experience_level in ("intern", "mid", "senior"):
        return False
    # In a title the years are the requirement ("Java Developer (3+ years)").
    in_title = [
        int(m.group(1)) for m in _YEARS_RE.finditer(fold(title)) if m.group(1).isdigit()
    ]
    years = max(in_title) if in_title else required_years(description)
    return years is None or years <= JUNIOR_MAX_YEARS


def matches_experience_level(
    title: str,
    levels: list[str],
    experience_level: str | None = None,
    description: str | None = None,
) -> bool:
    """Check if a job title matches the requested experience levels.

    "intern" means internships/traineeships only — junior roles need the
    "junior" level explicitly (production complaint: intern-only searches
    were receiving Junior Developer postings).

    `experience_level` is the board's own label (Job.experience_level). It
    adds a match the title missed — an eJobs posting titled "QA Engineer"
    and filed as entry-level is a junior job — but does not override a
    title that says otherwise ("Senior Java Developer" filed as entry).
    `description` lets the junior level tell an unmarked graduate posting
    from one asking for five years (see _is_junior); without it, unmarked
    titles count as junior.
    """
    wanted = {canonical_level(level) or level.lower() for level in levels or []}
    if not wanted or "any" in wanted:
        return True
    title_lower = str(title).lower() if title else ""

    if "intern" in wanted:
        if INTERN_RE.search(title_lower) or (
            experience_level == "intern" and not SENIOR_RE.search(title_lower)
        ):
            return True
    if "junior" in wanted and _is_junior(title_lower, experience_level, description):
        return True
    if "mid" in wanted:
        # Mid-level: not entry-level and not senior
        if experience_level == "mid" or (
            not ENTRY_LEVEL_RE.search(title_lower) and not SENIOR_RE.search(title_lower)
        ):
            return True
    if "senior" in wanted:
        if SENIOR_RE.search(title_lower) or experience_level == "senior":
            return True
    return False


def city_only(location: str) -> str:
    """Strip the country part from 'City, Country' for Glassdoor compatibility."""
    return location.split(",")[0].strip()


def filter_dataframe(df: pd.DataFrame, keyword: str | list[str]) -> pd.DataFrame:
    """Apply title-relevance filtering to a scraped DataFrame.

    Accepts a single keyword or a list. A job passes if it's relevant
    for *any* of the provided keywords. An `experience_level` column
    (native adapters) is passed through as the board's seniority label,
    and a `description` column for the years of experience asked.
    """
    if df.empty or "title" not in df.columns:
        return df

    keywords = [keyword] if isinstance(keyword, str) else keyword
    relevant = relevance_predicate(keywords)

    def level(value: object) -> str | None:
        return value if isinstance(value, str) else None

    levels = df["experience_level"] if "experience_level" in df.columns else [None] * len(df)
    descriptions = df["description"] if "description" in df.columns else [None] * len(df)
    mask = [
        relevant(str(t), level(lvl), level(desc))
        for t, lvl, desc in zip(df["title"], levels, descriptions)
    ]

    before = len(df)
    filtered = df[pd.Series(mask, index=df.index, dtype=bool)]
    removed = before - len(filtered)
    if removed > 0:
        import logging
        logging.getLogger(__name__).info(
            "Relevance filter: %d -> %d results (removed %d for %r)",
            before, len(filtered), removed, keywords,
        )
    return filtered
