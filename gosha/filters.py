"""Filtering and relevance logic for job matching.

All filters are pure functions that operate on individual job dicts or
DataFrames — no DB or IO dependencies.
"""

from __future__ import annotations

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
    "dublin": {"search": "Dublin, Ireland", "match": ["dublin"]},
    "london": {"search": "London, United Kingdom", "match": ["london"]},
    "berlin": {"search": "Berlin, Germany", "match": ["berlin"]},
    "amsterdam": {"search": "Amsterdam, Netherlands", "match": ["amsterdam"]},
    # Indeed writes Romanian locations as "București, IF, RO" with no
    # country name, so the city list is what catches them.
    "romania": {
        "search": "Romania",
        "match": [
            "romania", "bucharest", "bucuresti", "cluj", "timisoara", "iasi",
            "brasov", "sibiu", "craiova", "constanta", "oradea", "arad",
            "targu mures", "baia mare", "pitesti", "ploiesti", "galati",
            "suceava", "bacau",
        ],
    },
    # Remote
    "remote": {
        "search": "Remote",
        "match": ["remote", "work from home", "wfh", "anywhere"],
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
            "romania", "bucharest", "bucuresti", "cluj", "timisoara", "iasi", "brasov",
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
            "romania", "bucharest", "bucuresti", "cluj", "timisoara", "iasi", "brasov",
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

SENIOR_RE = re.compile(
    r"(?i)\b(?:senior|sr\.?|lead|principal|staff|director|"
    r"manager|head\b|vp|chief|architect)\b"
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
    """Return (search_location, match_substrings) for a user-provided location."""
    normalized = location.lower().strip()
    alias = LOCATION_ALIASES.get(normalized) or LOCATION_ALIASES.get(fold(normalized))
    if alias:
        return alias["search"], alias["match"]
    return location, [normalized]


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
    loc = fold(job_location)
    return any(fold(sub) in loc for sub in match_substrings if sub)


def keyword_wants_entry_level(keyword: str) -> bool:
    """Check if the subscription keyword implies entry-level/intern jobs."""
    kw = keyword.lower()
    return any(w in kw for w in ("intern", "entry", "junior", "graduate", "trainee"))


def keyword_wants_tech(keyword: str) -> bool:
    """Check if the subscription keyword implies tech/CS domain."""
    kw = keyword.lower().strip()
    if kw in KEYWORD_EXPANSIONS:
        return True
    return any(
        w in kw
        for w in (
            "computer", "software", "tech", "data", "engineer",
            "developer", "devops", "cyber", "cloud", "ml", "ai",
        )
    )


def title_is_relevant(
    title: str, keyword: str, experience_level: str | None = None,
) -> bool:
    """Check if a job title is relevant for the given subscription keyword.

    `experience_level` is the board's own seniority label when it has one
    (RawJob.experience_level). An entry-level label satisfies an
    entry-level keyword even when the title does not say "junior", which
    is how most Romanian boards title their entry-level postings; a
    senior-sounding title still disqualifies.
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
        if not board_says_entry and not ENTRY_LEVEL_RE.search(title):
            return False
        if SENIOR_RE.search(title):
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
    """title, experience_level -> bool: relevant for any of `keywords`."""

    def relevant(title: str, experience_level: str | None = None) -> bool:
        return any(title_is_relevant(title, kw, experience_level) for kw in keywords)

    return relevant


def matches_excluded_keywords(text: str, excluded: list[str]) -> bool:
    """Return True if text contains any of the excluded keywords.

    Uses word-boundary matching so excluding "sales" won't reject
    "wholesale" but will reject "Sales Manager".
    """
    if not excluded or not text:
        return False
    text_lower = text.lower()
    for kw in excluded:
        kw_lower = kw.lower().strip()
        if not kw_lower:
            continue
        pattern = r"\b" + re.escape(kw_lower) + r"\b"
        if re.search(pattern, text_lower):
            return True
    return False


def matches_company_blacklist(company: str, blacklist: list[str]) -> bool:
    """Return True if company matches any blacklisted name.

    Uses substring matching so "Google" blocks "Google Ireland".
    """
    if not blacklist or not company:
        return False
    company_lower = company.lower().strip()
    return any(bl.lower().strip() in company_lower for bl in blacklist)


# Strictly internships/traineeships — NOT junior/graduate/entry-level.
# Includes the Romanian terms local boards use (stagiar, practicant).
# Ends on a word boundary for the same reason as ENTRY_LEVEL_RE.
INTERN_RE = re.compile(
    r"(?i)\b(?:intern(?:ship)?s?|trainees?|traineeship|apprentice(?:ship)?s?|"
    r"placements?|co.?op|summer|academy|stagiar\w*|stagi[ui]|practicant\w*|"
    r"practic[aă])(?!\w)"
)


def matches_experience_level(
    title: str, levels: list[str], experience_level: str | None = None,
) -> bool:
    """Check if a job title matches the requested experience levels.

    "intern" means internships/traineeships only — junior roles need the
    "junior" level explicitly (production complaint: intern-only searches
    were receiving Junior Developer postings).

    `experience_level` is the board's own label (Job.experience_level). It
    can only add a match the title missed — an eJobs posting titled "QA
    Engineer" and filed as entry-level is a junior job — never remove one,
    because boards tick several levels and the label keeps the most junior.
    """
    if not levels or "any" in levels:
        return True
    title_lower = str(title).lower() if title else ""

    for level in levels:
        level = level.lower()
        if level in ("intern", "internship"):
            if INTERN_RE.search(title_lower) or experience_level == "intern":
                return True
        elif level == "junior":
            if experience_level == "entry" or re.search(
                r"\b(?:junior|jr\.?|entry.?level|graduate|absolvent\w*)\b", title_lower,
            ):
                return True
        elif level == "mid":
            # Mid-level: not entry-level and not senior
            if experience_level == "mid" or (
                not ENTRY_LEVEL_RE.search(title_lower) and not SENIOR_RE.search(title_lower)
            ):
                return True
        elif level == "senior":
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
    (native adapters) is passed through as the board's seniority label.
    """
    if df.empty or "title" not in df.columns:
        return df

    keywords = [keyword] if isinstance(keyword, str) else keyword
    relevant = relevance_predicate(keywords)

    def level(value: object) -> str | None:
        return value if isinstance(value, str) else None

    if "experience_level" in df.columns:
        mask = [
            relevant(str(t), level(lvl))
            for t, lvl in zip(df["title"], df["experience_level"])
        ]
    else:
        mask = [relevant(str(t)) for t in df["title"]]

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
