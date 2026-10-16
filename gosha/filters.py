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
            "croatia", "bulgaria", "serbia", "greece", "slovakia", "slovenia",
            "estonia", "latvia", "lithuania", "moldova",
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
            "croatia", "bulgaria", "serbia", "greece", "slovakia", "slovenia",
            "estonia", "latvia", "lithuania", "moldova",
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
    r"(?:working\s+)?students?|"
    r"stagiar\w*|stagi[ui]|practic(?:ant\w*|[aă])|absolvent\w*|"
    r"[iî]ncep[aă]tor\w*|debutant\w*|f[aă]r[aă]\s+experien[tț][aă])(?!\w)"
)

# "Șef" is Romanian for head/chief ("Contabil Șef", "Șef de echipă").
# "Staff" only as the engineering grade ("Staff Accountant" is an entry
# title); "lead" not in "Lead Generation" (a sales role). "Manager" is a
# role more than a level, see says_senior.
SENIOR_RE = re.compile(
    r"(?i)\b(?:senior|sr\.?|lead(?:er)?(?!\s+gen)|principal|"
    r"staff(?=\s+(?:software|engineer|developer|data|machine|ml|ai|backend|back.?end|"
    r"frontend|front.?end|full.?stack|platform|infrastructure|security|site|sre|"
    r"product|designer|scientist|research|architect|qa|test|mobile|ios|android|cloud|devops))|"
    r"director|manager|head\b|vp|chief|architect|[sșş]ef[aă]?)\b"
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


def search_location_ok(
    job_location: str | None, locations: list[str], remote_ok: bool = False,
) -> bool:
    """Whether a posting's location fits a search's locations.

    No locations: anywhere. Otherwise any of them (see location_matches),
    or a remote posting when `remote_ok`. One implementation for the match
    stage, browse and the feed.
    """
    if not locations:
        return True
    if any(location_matches(job_location, _match_subs(loc)) for loc in locations):
        return True
    return remote_ok and location_matches(job_location, _match_subs("remote"))


@functools.lru_cache(maxsize=1024)
def _match_subs_cached(location: str) -> tuple[str, ...]:
    return tuple(normalize_location(location)[1])


def _match_subs(location: str) -> list[str]:
    return list(_match_subs_cached(location))


def _alias(location: str) -> dict[str, Any] | None:
    return (
        LOCATION_ALIASES.get(location)
        or LOCATION_ALIASES.get(fold(location))
        or LOCATION_ALIASES.get(_place(location))
    )


# A slash-command option name pasted into its value: "location:Cluj",
# "llocation:Cluj" (an active production search matched nothing for it).
_OPTION_PREFIX_RE = re.compile(r"(?i)^\s*\w*location\s*[:=]\s*")
# Shorter than this and naming no known place: a fragment ("kra", "mun"),
# not a town the alias table lacks.
MIN_UNKNOWN_PLACE_LEN = 4


def is_known_place(location: str) -> bool:
    """Whether the alias table or the country lists name this place."""
    key = location.lower().strip()
    if _alias(key) or _alias(city_only(key)):
        return True
    place = _place(key)
    return place in _CITY_COUNTRY or place in _COUNTRY_NAMES or place in _COUNTRY_CODES


def clean_location(location: str) -> str | None:
    """A search location as the scrapers and the matcher can use it.

    Strips a pasted option prefix; None for an empty value or a short
    token that names no place ("kra"), which matches nothing and costs a
    failing board request every cycle. A longer unknown name is kept: it
    is most likely a Romanian town the boards can look up themselves.
    """
    text = _OPTION_PREFIX_RE.sub("", str(location)).strip()
    if not text:
        return None
    if len(_place(text)) < MIN_UNKNOWN_PLACE_LEN and not is_known_place(text):
        return None
    return text


def place_suggestions(fragment: str, limit: int = 3) -> list[str]:
    """Known places starting with `fragment`: "kra" → ["Krakow"]."""
    key = _place(fragment)
    if not key:
        return []
    names = sorted({
        alias["search"].split(",")[0] for name, alias in LOCATION_ALIASES.items()
        if _place(name).startswith(key)
    })
    return names[:limit]


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
    countries = _named_countries(str(job_location))
    if any(
        _place_pattern(sub).search(loc) and _counts_in(_place(sub), countries, loc)
        for sub in match_substrings if sub
    ):
        return True
    # "Romania" with no city is a nationwide (usually remote-in-Romania)
    # posting: open to someone searching any Romanian city.
    return _ROMANIA_ONLY_RE.fullmatch(loc) is not None and any(
        _place(sub) in _ROMANIAN_PLACES for sub in match_substrings if sub
    )


# Where the cities the aliases name are. A posting that names a country
# other than a city's own is not in that city: "Arad, Israel", "Slatina,
# Croatia", "Calarasi, Moldova" all matched Romanian searches.
_CITY_COUNTRY: dict[str, str] = {
    **{city: "romania" for city in ROMANIAN_CITIES},
    **{c: "germany" for c in ("berlin", "munich", "munchen", "hamburg", "frankfurt")},
    **{c: "netherlands" for c in ("amsterdam", "eindhoven")},
    "dublin": "ireland",
    **{c: "united kingdom" for c in ("london", "manchester", "edinburgh")},
    **{c: "france" for c in ("paris", "lyon")},
    **{c: "spain" for c in ("barcelona", "madrid")},
    **{c: "poland" for c in ("warsaw", "warszawa", "krakow", "cracow", "wroclaw")},
    **{c: "czech" for c in ("prague", "praha", "brno")},
    "budapest": "hungary",
    **{c: "austria" for c in ("vienna", "wien")},
    "zurich": "switzerland",
    **{c: "sweden" for c in ("stockholm", "gothenburg")},
    "copenhagen": "denmark", "helsinki": "finland", "oslo": "norway",
    **{c: "portugal" for c in ("lisbon", "porto")},
    **{c: "italy" for c in ("milan", "rome")},
    "brussels": "belgium",
}
EUROPEAN_COUNTRIES = frozenset({
    "romania", "germany", "netherlands", "ireland", "united kingdom", "france",
    "spain", "poland", "czech", "hungary", "austria", "switzerland", "sweden",
    "denmark", "finland", "norway", "portugal", "italy", "belgium", "luxembourg",
    "croatia", "bulgaria", "serbia", "greece", "slovakia", "slovenia", "estonia",
    "latvia", "lithuania", "moldova", "ukraine", "cyprus", "malta", "iceland",
    "albania",
})
# Country names as postings write them (folded, as _place leaves them).
_COUNTRY_NAMES: dict[str, str] = {
    **{c: c for c in EUROPEAN_COUNTRIES},
    "deutschland": "germany", "holland": "netherlands", "uk": "united kingdom",
    "england": "united kingdom", "scotland": "united kingdom",
    "great britain": "united kingdom", "espana": "spain", "polska": "poland",
    "czechia": "czech", "italia": "italy", "republic of moldova": "moldova",
    "united states": "united states", "usa": "united states", "us": "united states",
    "u s": "united states", "u s a": "united states", "america": "united states",
    "canada": "canada", "brazil": "brazil", "brasil": "brazil", "mexico": "mexico",
    "argentina": "argentina", "colombia": "colombia", "chile": "chile",
    "egypt": "egypt", "israel": "israel", "turkey": "turkey", "turkiye": "turkey",
    "india": "india", "pakistan": "pakistan", "philippines": "philippines",
    "australia": "australia", "new zealand": "new zealand", "south africa": "south africa",
    "nigeria": "nigeria", "kenya": "kenya", "japan": "japan", "china": "china",
    "singapore": "singapore", "vietnam": "vietnam", "indonesia": "indonesia",
    "malaysia": "malaysia", "russia": "russia", "belarus": "belarus",
}
_COUNTRY_NAME_RE = re.compile(
    r"(?<![a-z0-9])(?:"
    + "|".join(re.escape(n) for n in sorted(_COUNTRY_NAMES, key=len, reverse=True))
    + r")(?![a-z0-9])"
)
# Indeed's "City, ST, CC": the last of three or more parts is the country
# code ("Porto Velho, RO, BR" is Rondônia, Brazil). The European ones by
# name, so a Europe search can take them; any other code is just abroad.
_COUNTRY_CODES = {
    "ro": "romania", "de": "germany", "nl": "netherlands", "ie": "ireland",
    "gb": "united kingdom", "uk": "united kingdom", "fr": "france", "es": "spain",
    "pl": "poland", "cz": "czech", "hu": "hungary", "at": "austria",
    "ch": "switzerland", "se": "sweden", "dk": "denmark", "fi": "finland",
    "no": "norway", "pt": "portugal", "it": "italy", "be": "belgium",
    "lu": "luxembourg", "hr": "croatia", "bg": "bulgaria", "rs": "serbia",
    "gr": "greece", "sk": "slovakia", "si": "slovenia", "ee": "estonia",
    "lv": "latvia", "lt": "lithuania", "md": "moldova", "ua": "ukraine",
    "us": "united states",
}
# A remote posting open to anyone, or to someone in Europe.
_REMOTE_REGION_RE = re.compile(
    r"(?<![a-z0-9])(?:europe|european|eu|emea|cet|worldwide|anywhere|global)(?![a-z0-9])"
)


@functools.lru_cache(maxsize=4096)
def _named_countries(job_location: str) -> frozenset[str]:
    """The countries a posting's location names; empty if none."""
    found = {_COUNTRY_NAMES[m.group(0)] for m in _COUNTRY_NAME_RE.finditer(_place(job_location))}
    parts = [part.strip() for part in fold(job_location).split(",")]
    if len(parts) >= 3 and re.fullmatch(r"[a-z]{2}", parts[-1]):
        found.add(_COUNTRY_CODES.get(parts[-1], parts[-1]))
    return frozenset(found)


def _counts_in(sub: str, countries: frozenset[str], loc: str) -> bool:
    """Whether a match on `sub` holds for a posting at `loc` naming `countries`."""
    if not countries:
        return True
    if sub == "ro":  # a country code, but not when another country is named
        return "romania" in countries
    city_country = _CITY_COUNTRY.get(sub)
    if city_country is not None:
        return city_country in countries
    if sub in _REMOTE_PLACES:
        # "Remote - US only", "Remote (USA)": remote for someone elsewhere.
        return bool(countries & EUROPEAN_COUNTRIES) or bool(_REMOTE_REGION_RE.search(loc))
    return True


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
_REMOTE_PLACES = frozenset(_place(w) for w in LOCATION_ALIASES["remote"]["match"])
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
    r"(?i)\b(?:intern(?:ship)?s?\b|entry|junior|graduate|trainee|students?\b|stagi|"
    r"practic|absolvent|[iî]ncep[aă]tor)"
)
_TECH_KEYWORD_RE = re.compile(
    r"(?i)\b(?:computer|software|tech|data|engineer|developer|devops|cyber|"
    r"cloud|programator|dezvoltator|informatic|(?:ml|ai|it)\b)"
)


# The internship subset of _ENTRY_KEYWORD_RE (see title_is_relevant).
_INTERN_KEYWORD_RE = re.compile(
    r"(?i)\b(?:intern(?:ship)?s?\b|trainee|students?\b|stagi|practic)"
)


def says_senior(title: str) -> bool:
    """A senior title. "Manager" alone is the role, not the level, when the
    title also says junior or intern ("Junior IT Project Manager")."""
    words = {m.group(0).lower() for m in SENIOR_RE.finditer(title)}
    if not words:
        return False
    return not (words == {"manager"} and ENTRY_LEVEL_RE.search(title))


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
            and not says_senior(title)
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
    # The boundary only on a side that ends in a word character: ".NET" is
    # in "ASP.NET Core" and "C++" in "C++17", but "java" not in "JavaScript".
    folded = fold(keyword).strip()
    body = r"\s+".join(re.escape(word) for word in folded.split())
    before = r"(?<!\w)" if re.match(r"\w", folded) else ""
    after = r"(?!\w)" if re.search(r"\w$", folded) else ""
    return re.compile(before + body + after)


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
    r"placements?|co.?op|summer|academy|(?:working\s+)?students?|"
    r"stagiar\w*|stagi[ui]|practicant\w*|practic[aă])(?!\w)"
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
    r"(?:(?:-|–|to|la|sau|or|si|and)\s*\d{1,2}\s*\+?\s*)?"
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
# Not a bare "plus": "5+ years of Java plus Spring Boot" is a requirement.
_NICE_TO_HAVE_RE = re.compile(
    r"\b(?:a plus|un plus|nice to have|preferred|preferably|advantage|bonus|"
    r"ideally|avantaj|de preferat)"
)
# The same wish written before the number: "Nice to have: 3 years of ...".
_NICE_TO_HAVE_BEFORE_RE = re.compile(
    r"\b(?:nice to have|preferred|preferably|ideally|bonus|de preferat|optional)\W*$"
)
# Sentences and bullets ("min." is not an end). A line ending in ":" is a
# heading for the next one ("Experience:\n- 3+ years"), so it stays attached.
_SENTENCE_SPLIT_RE = re.compile(r"(?<!\bmin)(?<!\bnr)[.;!?](?=\s|$)|(?<!:)\n|•")
# Between the years and "experience": "3+ years of professional
# experience", "5+ years of backend software development experience", "3
# ani de experiență". A few words, none of them saying the years are
# something else ("4 years old", "5 years on the market"). "4+ years of
# Swift development" names the experience without the word.
_GAP_WORD = r"(?!old\b|ago\b|on\b|in\s+business\b|pe\b|we\b|our\b|you\b|will\b)\S+\s+"
_YEARS_THEN_EXPERIENCE_RE = re.compile(
    r"^[\s'’)]*(?:"
    r"(?:" + _GAP_WORD + r"){0,8}(?:experien|practic[ae]\b)|"
    r"(?:of|de)\s+(?:" + _GAP_WORD + r"){0,7}"
    r"(?:development|programming|engineering|dezvoltare|programare)\b)"
)
# Years the posting says it does not want: "we are not looking for a
# senior with 10 years of experience", "no need for 5 years".
_NOT_WANTED_RE = re.compile(
    r"\b(?:not looking for|nu cautam|we don't need|we do not need|no need for|"
    r"nu avem nevoie de)\b"
)
# "Experience: at least 3 years", "experiență în domeniul ... de minim 2
# ani", "experiență minimă în contabilitate, 1-2 ani": the number right
# after a requirement word, with "experience" earlier in the sentence.
_EXPERIENCE_THEN_YEARS_RE = re.compile(
    r"experien\w*\b.{0,80}?(?:min(?:im(?:um|al)?)?\.?|at least|cel putin|of|de|"
    r"over|peste|more than|mai mult de|:|-|,|\()\s*$",
    re.S,
)
# A company's age, not the candidate's experience: "on the market", "pe
# piață", "10 ani de activitate", and for ten years and more "we have 15
# years of experience", "a company with over 10 years of experience". Nor
# the candidate's age ("vârsta recomandată: 18–40 ani").
_COMPANY_AGE_RE = re.compile(
    r"\b(?:on the market|pe piata|in business|founded|since|de activitate|"
    r"of activity|of history|varsta|age|aged)\b"
)
_COMPANY_SUBJECT_RE = re.compile(
    r"\b(?:we have|we've|we bring|avem|are peste|has over|have over|has more than|"
    r"with over|with more than|cu peste|in peste|company|compania|companie|firma)\b"
)


@functools.lru_cache(maxsize=4096)
def required_years(description: str | None) -> int | None:
    """The years of experience a posting requires; None if unstated.

    A number counts only when "experience"/"experiență" names it in the
    same sentence: a few words after it ("3+ years of professional
    experience") or just before it after a requirement word ("experience:
    at least 3 years"). "A company with 20 years on the market", "a 1-year
    contract", "Within 3 years you will grow into a lead" are not
    requirements. A range counts as its lower bound ("3-5 years" is 3).
    Nice-to-haves are skipped, and of the rest the largest wins: "5+ years
    of experience overall, 2 years with Docker" asks for five.

    Cached: the match stage asks for every subscription and keyword.
    """
    if not description:
        return None
    found: list[int] = []
    for sentence in _SENTENCE_SPLIT_RE.split(fold(description)):
        for m in _YEARS_RE.finditer(sentence):
            years = _requirement(sentence, m)
            if years is not None:
                found.append(years)
    return max(found) if found else None


def _requirement(sentence: str, m: re.Match[str]) -> int | None:
    head, rest = sentence[: m.start()], sentence[m.end() :]
    if not (
        _YEARS_THEN_EXPERIENCE_RE.match(rest) or _EXPERIENCE_THEN_YEARS_RE.search(head)
    ):
        return None
    # This mention's own tail: up to a bracket, the next clause or the
    # next number ("At least 4 years (5 years is a plus)" asks for four).
    tail = re.split(r"[(\d]|,\s", rest, maxsplit=1)[0]
    if (
        _NICE_TO_HAVE_RE.search(tail)
        or _NICE_TO_HAVE_BEFORE_RE.search(head)
        or _NOT_WANTED_RE.search(head)
    ):
        return None
    raw = m.group(1)
    years = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
    if not 1 <= years <= 30 or _COMPANY_AGE_RE.search(sentence):
        return None
    if years >= 10 and _COMPANY_SUBJECT_RE.search(head):
        return None
    return years


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
    if says_senior(title) or INTERN_RE.search(title) or MID_TITLE_RE.search(title):
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
            experience_level == "intern" and not says_senior(title_lower)
        ):
            return True
    if "junior" in wanted and _is_junior(title_lower, experience_level, description):
        return True
    if "mid" in wanted:
        # Mid-level: not entry-level and not senior
        if experience_level == "mid" or (
            not ENTRY_LEVEL_RE.search(title_lower) and not says_senior(title_lower)
        ):
            return True
    if "senior" in wanted:
        if says_senior(title_lower) or experience_level == "senior":
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
