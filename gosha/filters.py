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
    "ilfov", "floresti", "dej",
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
            "europe", "eu", "emea",
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
            "europe", "eu", "emea",
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
    r"bim|revit|"
    # Matched TECH_RE through "Developer", "Tester", "AI" or "IT" (Italy's
    # code), and reached CS internship searches in production.
    r"business\s+develop\w*|game\s+test\w*|linguist\w*|locali[sz]ation|evaluators?|"
    r"annotat\w*|creators?|customer\s+(?:support|service|care)|call\s+cent\w*|"
    r"industrial|cnc|bpo)\b"
)

# Unambiguous CS signals that override a negative-domain hit
# ("Junior Software Engineer - Process Automation" is still ours).
STRONG_CS_RE = re.compile(
    r"(?i)\b(?:software|(?<!business )(?<!sales )develop(?:er|ment)|programm(?:er|ing)|"
    r"programator(?!\w*\s+cnc)\w*|"
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
    if not remote_ok:
        return False
    # A posting naming only the country ("Romania", "România (Remote)") has
    # no city: for a Romanian city search it is one more remote-ish option,
    # so only with remote_ok. Taken by every city search, it added 8-12
    # mostly unrelated candidates per Cluj or Timisoara search (prod, 2026-10).
    return location_matches(job_location, _match_subs("remote")) or (
        is_romania_only(job_location)
        and any(p in _ROMANIAN_PLACES for loc in locations for p in map(_place, _match_subs(loc)))
    )


def is_romania_only(job_location: str | None) -> bool:
    """A location naming Romania and no city: "Romania", "Remote, RO"."""
    if not job_location or pd.isna(job_location):
        return False
    return _ROMANIA_ONLY_RE.fullmatch(_place(str(job_location))) is not None


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
_OPTION_PREFIX_RE = re.compile(r"(?i)^[\s*=:]*(?:\w*location\s*[:=])?[\s*=:]*")
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


def split_locations(text: str) -> list[str]:
    """Places typed into one field, separated by commas or semicolons.

    A country (or country code) right after a place belongs to it: "Cluj-
    Napoca, Romania" is one place. Split on the comma it was stored as
    ["Cluj-Napoca", "Romania"], and "Romania" made the search nationwide.
    """
    places: list[str] = []
    joined = False  # the last place already carries its country
    for part in re.split(r"[,;]", text or ""):
        part = part.strip()
        if not part:
            continue
        key = _place(part)
        is_country = key in _COUNTRY_NAMES or key in _COUNTRY_CODES
        last_is_country = bool(places) and (
            _place(places[-1]) in _COUNTRY_NAMES or _place(places[-1]) in _COUNTRY_CODES
        )
        if is_country and places and not joined and not last_is_country:
            places[-1] = f"{places[-1]}, {part}"
            joined = True
            continue
        places.append(part)
        joined = False
    return places


def place_suggestions(fragment: str, limit: int = 3) -> list[str]:
    """Known places starting with `fragment`: "kra" → ["Krakow"], "bac" →
    ["Bacau"]."""
    key = _place(fragment)
    if not key:
        return []
    names = {
        alias["search"].split(",")[0] for name, alias in LOCATION_ALIASES.items()
        if _place(name).startswith(key)
    } | {city.title() for city in ROMANIAN_CITIES if city.startswith(key)}
    return sorted(names)[:limit]


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


def canonical_keyword(keyword: str) -> str:
    """The keyword without a suggestion label's description: the bot's
    autocomplete shows "computer science internship — 19 intern/junior
    roles", and a user who typed it rather than picking it stored it, then
    searched the boards for it literally instead of the 19 terms."""
    return re.sub(r"\s+[—–]\s+.*$", "", str(keyword)).strip()


def expand_keyword(keyword: str) -> list[str]:
    """Return a list of specific search terms for a broad keyword."""
    keyword = canonical_keyword(keyword)
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
    if any(
        _place_pattern(sub).search(part) and _counts_in(_place(sub), countries, loc)
        for part, countries in _location_parts(str(job_location))
        for sub in match_substrings if sub
    ):
        return True
    return False


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
    # "us" alone is matched by _US_WORD_RE: "US company" is not a place.
    "united states": "united states", "usa": "united states",
    "u s": "united states", "u s a": "united states", "america": "united states",
    # Regions: remote for another continent.
    "latam": "latam", "latin america": "latam", "americas": "americas",
    "north america": "north america", "south america": "latam", "apac": "apac",
    "asia pacific": "apac", "asia": "asia",
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
# "US" as the country: on its own, last, or "US only" / "US-based".
_US_WORD_RE = re.compile(r"(?<![a-z0-9])us(?=\s*(?:only|based)\b|\s*$|\s*[),])")
# "City, ST" with no country: a US state. Not the codes Romanian counties
# share (AR Arad, CT Constanța, IL Ialomița, MS Mureș).
_US_STATE_CODES = frozenset(
    "al ak az ca co de fl ga hi id in ia ks ky la me md ma mi mn mo mt ne nv nh nj nm "
    "ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy dc".split()
)
# A remote posting open to anyone, or to someone in Europe.
_REMOTE_REGION_RE = re.compile(
    r"(?<![a-z0-9])(?:europe|european|eu|emea|cet|worldwide|anywhere|global)(?![a-z0-9])"
)


# Places in one location string: "Cluj-Napoca / Munich, Germany",
# "Bucharest or Remote (USA)". Never inside brackets ("Remote (USA/Europe)").
# "or" only between places, not Oregon's code in "Remote, OR, USA".
_PLACE_SEPARATOR_RE = re.compile(
    r"(?:\s*[/;|&]\s*|(?<![,\s])\s+(?:or|sau|and|si)\s+)(?![^()]*\))"
)
_COMMA_OUTSIDE_BRACKETS_RE = re.compile(r",(?![^()]*\))")
_BRACKETED_RE = re.compile(r"\([^()]*\)")
# Romanian cities in the historic region of Moldova: "Iași, Moldova" is
# in Romania. (Călărași and Florești are also towns in the Republic.)
_MOLDAVIA_CITIES = frozenset({
    "iasi", "bacau", "suceava", "vaslui", "botosani", "piatra neamt", "focsani", "galati",
})


@functools.lru_cache(maxsize=4096)
def _location_parts(job_location: str) -> tuple[tuple[str, frozenset[str]], ...]:
    """(part, countries named for it) for each place named.

    A country applies to the place it is written with, not to every
    place in the string: "Bucharest, Budapest (Hungary)" is in Bucharest
    too. A bracketed country is its own comma part's ("Warsaw (Poland)");
    a plain one ("Chisinau, Moldova") and Indeed's trailing country code
    ("Porto Velho, RO, BR") are their whole piece's.
    """
    out: list[tuple[str, frozenset[str]]] = []
    for piece in _PLACE_SEPARATOR_RE.split(fold(job_location)):
        parts = _COMMA_OUTSIDE_BRACKETS_RE.split(piece)
        shared = _country_names(_BRACKETED_RE.sub(" ", piece))
        codes = [part.strip() for part in parts]
        if len(codes) >= 3 and re.fullmatch(r"[a-z]{2}", codes[-1]):
            shared |= {_COUNTRY_CODES.get(codes[-1], codes[-1])}
        elif len(codes) == 2 and codes[-1] in _US_STATE_CODES:
            shared |= {"united states"}
        for part in parts:
            bracketed = " ".join(_BRACKETED_RE.findall(part))
            out.append((_place(part), frozenset(shared | _country_names(bracketed))))
    return tuple(out)


def _country_names(text: str) -> set[str]:
    place = _place(text)
    found = {_COUNTRY_NAMES[m.group(0)] for m in _COUNTRY_NAME_RE.finditer(place)}
    if _US_WORD_RE.search(place):
        found.add("united states")
    return found


def _counts_in(sub: str, countries: frozenset[str], loc: str) -> bool:
    """Whether a match on `sub` holds for a place at `loc` naming `countries`."""
    if not countries:
        return True
    if sub == "ro":  # a country code, but not when another country is named
        return "romania" in countries
    city_country = _CITY_COUNTRY.get(sub)
    if city_country is not None:
        if sub in _MOLDAVIA_CITIES and "moldova" in countries:
            return True
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


# A level word glued to the next word by a scraper that dropped the space
# ("InternSoftware Engineer", Ascom on LinkedIn and Indeed): split where a
# level word runs into a capitalised word. Case-sensitive on purpose, so
# "Internal" and "Seniority" stay whole.
_GLUED_LEVEL_RE = re.compile(
    r"\b((?:[Ii]ntern(?:ship)?s?|INTERN(?:SHIP)?S?|[Jj]unior|JUNIOR|[Ss]enior|SENIOR|"
    r"[Tt]rainee|TRAINEE|[Gg]raduate|GRADUATE|[Ss]tudent|STUDENT|[Ss]tagiar|STAGIAR|"
    r"[Pp]rincipal|PRINCIPAL|[Ll]ead|LEAD))(?=[A-Z][a-z])"
)


def split_glued_level(title: str) -> str:
    """"InternSoftware Engineer" -> "Intern Software Engineer"."""
    return _GLUED_LEVEL_RE.sub(r"\1 ", title)


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
    kw = canonical_keyword(keyword).lower()
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
    levels: list[str] | None = None,
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
    a handful of the eval set's student-level postings. So does an
    internship keyword when the search's `levels` also ask for junior
    (6 of 22 production searches): "Software Engineer Backend(Django)".
    """
    if not title or pd.isna(title):
        return False
    title = split_glued_level(str(title))
    want_entry = keyword_wants_entry_level(keyword)
    want_tech = keyword_wants_tech(keyword)

    if not want_entry and not want_tech:
        return True  # No filtering for generic keywords

    if want_entry and _training_only(keyword, levels):
        # Owner rule: an internship search without the junior level gets
        # training positions only (internship, traineeship, apprenticeship,
        # working student, stagiu/practică), from the title or the board's
        # own "intern" label. No junior, unmarked or "entry"-labelled regular
        # jobs, and no graduate hires (owner, 2026-10-17: junior).
        training = INTERN_RE.search(title) or (
            experience_level == "intern"
            and (not want_tech or STRONG_CS_RE.search(title))
        )
        if not training or says_senior(title) or _graduate_hire(title, description):
            return False
    elif want_entry:
        # The board's label, when the years asked for agree (_is_junior).
        board_says_entry = experience_level == "intern" or (
            experience_level == "entry" and _asks_junior_years(title, description)
        )
        # Nor the label alone for a tech search: it must be a CS title
        # ("Consultant IT SAP" filed entry-level by eJobs is not).
        if board_says_entry and want_tech and not (
            ENTRY_LEVEL_RE.search(title) or STRONG_CS_RE.search(title)
        ):
            board_says_entry = False
        says_entry = (
            (board_says_entry or ENTRY_LEVEL_RE.search(title))
            and not says_senior(title)
        )
        junior_too = not _INTERN_KEYWORD_RE.search(keyword) or _wants_junior(levels)
        if not says_entry and not (
            junior_too and _is_junior(title.lower(), experience_level, description)
        ):
            return False
    if want_tech:
        # "HU / DE / NL / FR / IT / ES": languages, not IT.
        tech_title = _CODE_LIST_RE.sub(" ", title)
        # SAP / Salesforce-type platforms are IT for a training position
        # ("Trainee SAP Consultant"); "Salesforce Specialist" is sales.
        if not (
            TECH_RE.search(tech_title)
            or (INTERN_RE.search(tech_title) and _IT_PLATFORM_RE.search(tech_title))
        ):
            return False
        if _is_non_software_qa(tech_title, description):
            return False
        title = tech_title
        # Generic words ("engineer", "technology", "AI") also appear in
        # industrial/marketing roles — reject those unless the title
        # carries an unambiguous CS signal.
        if NON_CS_DOMAIN_RE.search(title) and not STRONG_CS_RE.search(title):
            return False
        if _engineering_not_cs(title, description):
            return False
    return True


# A title that is tech only through "engineer(ing)" ("Working Student – Line
# Engineering Support", "Process Engineer") is CS when its description says
# so: a CS degree, a language, software, data work.
_GENERIC_TECH_WORDS = frozenset({"engineer", "engineers", "engineering"})
_CS_TEXT_RE = re.compile(
    r"\b(?:computer\s+(?:science|engineering)|calculatoare|informatic\w*|software|"
    r"programm\w*|programare|coding|scripting|developers?|python|java\w*|c\+\+|c#|sql|"
    r"power\s*bi|data\s+(?:scien\w*|analy\w*|engineer\w*)|machine\s+learning|"
    r"embedded|firmware|automation|automatizar\w*|web|cloud|devops|networks?|"
    r"cyber\w*|linux|api)\b"
)


def _engineering_not_cs(title: str, description: str | None) -> bool:
    """A generic engineering title whose description shows no CS: the Bosch
    plant's line engineering working student (data input, production
    equipment, a mechanical or electrical degree). Kept when there is no
    description to read."""
    if not description:
        return False
    words = {m.group(0).lower() for m in TECH_RE.finditer(title)}
    if not words or not words <= _GENERIC_TECH_WORDS or STRONG_CS_RE.search(title):
        return False
    return not (_CS_TEXT_RE.search(fold(description)) or re.search(r"\bIT\b", description))


_IT_PLATFORM_RE = re.compile(r"(?i)\b(?:sap|abap|erp|crm|salesforce|servicenow|dynamics)\b")
# A slash list of language or country codes ("DE/NL/PL/IT"), not "AI/ML".
_LANGUAGE_CODE = (
    r"(?:DE|NL|PL|IT|FR|ES|HU|EN|RO|UK|US|GB|PT|SE|DK|FI|NO|CZ|SK|BG|GR|TR|RU|AT|CH|BE)"
)
_CODE_LIST_RE = re.compile(
    r"\b" + _LANGUAGE_CODE + r"(?:\s*/\s*" + _LANGUAGE_CODE + r")+\b"
)
_QA_TITLE_RE = re.compile(r"(?i)\b(?:qa|qc|quality)\b")
_SOFTWARE_TITLE_RE = re.compile(
    r"(?i)\b(?:software|test(?:er|ing)?|automation|developer|web|app|game|it)\b"
)
# A QA role in pharma or chemistry production ("eliberarea seriilor", GMP).
_LAB_QA_RE = re.compile(
    r"\b(?:gmp|farmac\w*|pharma\w*|chimi\w*|chemi\w*|laborator\w*|qc|medicament\w*|"
    r"biolog\w*|clinic\w*|seriilor|batch release)\b"
)
_SOFTWARE_TEXT_RE = re.compile(
    r"\b(?:software|selenium|cypress|playwright|api|web|aplicati\w*|apps?|automation|"
    r"test automation|testare software|frontend|backend|jira)\b"
)


def _is_non_software_qa(title: str, description: str | None) -> bool:
    """"QA Specialist" at a pharma plant: the description is about GMP and
    batch release, not software (a production DM judged wrong)."""
    if not description or not _QA_TITLE_RE.search(title) or _SOFTWARE_TITLE_RE.search(title):
        return False
    text = fold(description)
    return bool(_LAB_QA_RE.search(text)) and not _SOFTWARE_TEXT_RE.search(text)


# Career-hub and aggregator listings: an ad for many jobs, not a job.
_AD_TITLE_RE = re.compile(
    r"(?i)\b(?:programe\s+de|joburi|locuri\s+de\s+munca|oportunit[aă][tț]i|opportunities)\b"
)
_AD_COMPANY_RE = re.compile(r"(?i)\b(?:career\s+hub|hub\s+de\s+cariera|job\s*board)\b")
_ROLE_RE = re.compile(r"(?i)\b(?:engineer\w*|developer|analyst|specialist|scientist|designer)\b")


def looks_like_an_ad(title: str | None, company: str | None) -> bool:
    """"Programe de internship, trainee sau joburi entry-level in IT" from a
    career hub: no role in the title, and the title or the company says
    it is a listing. (A recruiter named "Career Hub" posting a "Reporting
    Analyst" is a job.)"""
    title = str(title or "")
    if _ROLE_RE.search(title) or STRONG_CS_RE.search(title):
        return False
    return bool(
        _AD_TITLE_RE.search(fold(title)) or (company and _AD_COMPANY_RE.search(fold(company)))
    )


# AI-training gig marketplaces: piecework labelling and grading of model
# output, posted as "Software Engineer (AI Training)" or "Back End Developer
# (AI Infrastructure)" in every city. Not jobs (production DMs judged wrong,
# 2026-10-16). "Outlier" only as the whole name.
_GIG_COMPANY_RE = re.compile(
    r"\b(?:alignerr|remotasks|mercor|toloka|appen|data\s*annotation(?:\.tech)?)\b|"
    r"^outlier(?:\s+ai)?$|\btelus\s+(?:international|digital)\s+ai\b"
)


def is_gig_marketplace(company: str | None) -> bool:
    """The company is an AI-training gig marketplace (Alignerr, Outlier, ...)."""
    return bool(company) and bool(_GIG_COMPANY_RE.search(" ".join(fold(company).split())))


# Postings that are not a job (gosha-label's audit, cause E, 2026-10-17):
# a course or training programme posted as a job ("Curs Consultant ERP
# ENTERSOFTONE Training Academy": "Acesta nu este un anunț de angajare",
# "Înscrie-te până la 23 octombrie"), and a talent-network CV drop ("Join
# Our Talent Network": "If you didnt identify a suitable open position,
# you can submit your CV here"). Read from the description, never from
# title words alone: "Academy" and "Talent" also title real internships,
# and real postings end with "join our talent community" or "use this
# general application" (Arch, AutoZone), or say an internship pool is "not
# a job posting for a specific role" (Qualcomm).
_SAYS_NOT_A_JOB_RE = re.compile(
    r"\b(?:nu\s+(?:este|e)\s+(?:un\s+)?anunt\s+de\s+angajare|"
    r"not\s+an?\s+(?:job|employment)\s+(?:offer|posting|ad\w*|opening))\b"
    r"(?!\s+for\s+(?:a|one)\s+(?:specific|single|particular))"
)
_COURSE_FEE_RE = re.compile(
    r"\b(?:course|tuition|program(?:me)?|enrol?ment|registration|participation)\s+fees?\b|"
    r"\btaxa\s+de\s+(?:participare|inscriere|curs|scolarizare)\b|"
    r"\b(?:pretul|costul)\s+(?:cursului|programului)\b"
)
# A course and a call to sign up for it, both in the text. Not "enrolled"
# ("students currently enrolled in a university" is an internship's
# requirement) nor the courses a job offers as a benefit, which come
# without a sign-up.
_COURSE_RE = re.compile(r"\b(?:curs(?:ul|ului|uri|urile)?|courses?|bootcamp)\b")
_SIGN_UP_RE = re.compile(
    r"\b(?:enrol(?:l)?\s+(?:now|today|by|before|in\s+(?:the|our|this)\s+"
    r"(?:course|programme|program|bootcamp))|"
    r"register\s+(?:now|today|for\s+(?:the|our|this)\s+(?:course|programme|program|bootcamp))|"
    r"inscrie-te|inscrie\s+te|inscrierile|locuri\s+limitate)\b"
)
# A CV drop: the text sends the reader who found no suitable position to
# leave a CV here, or the title is the talent network and the text asks
# for a CV for future openings.
_NO_SUITABLE_ROLE_RE = re.compile(
    r"\b(?:didn'?t|did\s+not|didnt|don'?t|do\s+not|dont)\s+(?:identify|find|see)\s+"
    r"(?:a\s+|an\s+)?suitable\s+(?:open\s+)?(?:position|role|job|vacancy)\b"
)
_CV_HERE_RE = re.compile(r"\bsubmit\s+your\s+(?:cv|resume|application)\s+here\b")
_TALENT_TITLE_RE = re.compile(
    r"\btalent\s+(?:network|community|pool)\b|\b(?:spontaneous|unsolicited)\s+application\b|"
    r"\bcandidatur\w*\s+spontan\w*"
)
_FUTURE_OPENINGS_RE = re.compile(
    r"\bfuture\s+(?:opportunities|openings|roles|positions|vacancies)\b|\boportunitati\s+viitoare\b"
)


def is_not_a_job(title: str | None, description: str | None) -> bool:
    """The posting says it is not a job: a course or training programme to
    sign up (or pay) for, or a talent-network/CV-drop page with no role.
    Decided by the description; the title only corroborates a CV drop."""
    if not description:
        return False
    text = " ".join(fold(description).split())
    if _SAYS_NOT_A_JOB_RE.search(text) or _COURSE_FEE_RE.search(text):
        return True
    if _COURSE_RE.search(text) and _SIGN_UP_RE.search(text):
        return True
    if _NO_SUITABLE_ROLE_RE.search(text) and _CV_HERE_RE.search(text):
        return True
    return bool(_TALENT_TITLE_RE.search(fold(title or "")) and _FUTURE_OPENINGS_RE.search(text))


def _training_only(keyword: str, levels: list[str] | None) -> bool:
    """An internship-type keyword, and levels known and without junior."""
    return (
        bool(_INTERN_KEYWORD_RE.search(keyword)) and levels is not None
        and not _wants_junior(levels)
    )


def _wants_junior(levels: list[str] | None) -> bool:
    """The search's levels take junior roles (junior, mid or any)."""
    wanted = {canonical_level(level) for level in levels or []}
    return bool(wanted & {"junior", "mid", "any"})


def relevance_predicate(keywords: list[str], levels: list[str] | None = None):
    """title, experience_level[, description] -> bool: relevant for any of
    `keywords`, for a search wanting `levels`."""

    def relevant(
        title: str, experience_level: str | None = None, description: str | None = None,
    ) -> bool:
        return any(
            title_is_relevant(title, kw, experience_level, description, levels)
            for kw in keywords
        )

    return relevant


def matches_excluded(title: str | None, description: str | None, excluded: list[str]) -> bool:
    """A posting has an excluded word: a level word ("senior", "lead",
    "manager") in its title, any other in its title or description.

    Excluding "senior" read "mentored by senior engineers" in a junior
    posting's description, and removed 4 of the eval set's 26 intern and
    junior postings.
    """
    if not excluded:
        return False
    level_words = [kw for kw in excluded if kw and is_level_word(kw)]
    other_words = [kw for kw in excluded if kw and not is_level_word(kw)]
    return matches_excluded_keywords(title or "", level_words) or matches_excluded_keywords(
        f"{title or ''} {description or ''}", other_words,
    )


def is_level_word(keyword: str) -> bool:
    """A seniority word or phrase: "senior", "Sr", "lead", "entry level"."""
    text = keyword.strip()
    return bool(
        canonical_level(text)
        or any(
            pattern.fullmatch(text)
            for pattern in (SENIOR_RE, JUNIOR_RE, INTERN_RE, MID_TITLE_RE, ENTRY_LEVEL_RE)
        )
        or re.fullmatch(r"(?i)(?:senior|junior|mid|entry|lead)[\s-]*level", text)
    )


def matches_excluded_keywords(text: str, excluded: list[str]) -> bool:
    """Return True if text contains any of the excluded keywords.

    Uses word-boundary matching so excluding "sales" won't reject
    "wholesale" but will reject "Sales Manager". Both sides are folded,
    so "vanzari" excludes "Vânzări" and the reverse. The boundary is "no
    letter or digit on either side" rather than \\b, which needs a word
    character next to it: excluding "C++", "C#" or ".NET" never matched.
    Web addresses do not count: ".NET" is not in "careers.company.net".
    """
    if not excluded or not text:
        return False
    folded = _WEB_ADDRESS_RE.sub(" ", fold(text))
    return any(
        _excluded_pattern(kw).search(folded) for kw in excluded if kw and kw.strip()
    )


# URLs, e-mail addresses and bare host names, except the .NET names
# themselves ("ASP.NET" looks like a host name).
_WEB_ADDRESS_RE = re.compile(
    r"\bhttps?://\S+|\bwww\.\S+|\S+@\S+|"
    r"\b(?!(?:asp|vb|ado)\.net\b)[a-z0-9-]+(?:\.[a-z0-9-]+)*"
    r"\.(?:com|net|org|ro|io|eu|co|info|biz|dev|app|uk|de)\b(?:/\S*)?"
)


@functools.lru_cache(maxsize=1024)
def _excluded_pattern(keyword: str) -> re.Pattern[str]:
    # A phrase matches across any whitespace (a line break in a description).
    # The boundary only on a side that ends in a word character: ".NET" is
    # in "ASP.NET Core" and "C++" in "C++17", but "java" not in "JavaScript".
    folded = fold(keyword).strip()
    body = r"\s+".join(re.escape(word) for word in folded.split())
    before = r"(?<!\w)" if re.match(r"\w", folded) else ""
    # "+" and "#" too: excluding "C" is not excluding "C++" or "C#".
    after = r"(?![\w+#])" if re.search(r"\w$", folded) else ""
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
    r"(?:student|trainee)\s+(?:program(?:me)?s?|scheme)|"
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

# Owner, 2026-10-17: internship and junior are different levels. A training
# position open to students is an internship; one that needs a finished
# degree (a graduate programme, Tenaris's "Junior Engineer (Global Trainee)"
# "for recent graduates" with a Bachelor's) is a junior position. A degree
# named alone ("Bachelor's degree in CS") does not say finished: internships
# write it about the degree in progress.
_FINISHED_DEGREE_RE = re.compile(
    r"\b(?:completed|finished|obtained)\s+(?:a\s+|your\s+|their\s+)?(?:university\s+|"
    r"bachelor'?s?\s+|master'?s?\s+|higher\s+)?(?:degree|studies|education)\b|"
    r"\b(?:have|has|you'?ve)\s+(?:recently\s+)?graduated\b|"
    r"\bstudii\s+(?:superioare\s+)?finalizate\b|\bdiploma\s+de\s+(?:licenta|master|absolvire)\b"
)
# Written for recent graduates: enough for a trainee title, which says
# nothing either way, but not for an internship title ("Software Engineer
# Intern ... Recent graduates are welcome").
_RECENT_GRADUATES_RE = re.compile(
    r"\b(?:recent(?:ly)?|new(?:ly)?|fresh)\s+graduat\w*|\bproaspat\w*\s+absolvent\w*"
)
# The same posting open to people still studying.
_OPEN_TO_STUDENTS_RE = re.compile(
    r"\bstudent\w*|\bundergraduat\w*|\b(?:currently\s+)?(?:enrolled|studying)\b|"
    r"\b(?:currently\s+)?pursuing\b|\b(?:final|last|penultimate)[\s-]+year\b|"
    r"\bmasterand\w*|\bin\s+curs\s+de\s+(?:finalizare|absolvire)\b|\banul\s+(?:[ivx]+|\d)\b"
)
# Training words that do not say who may apply: a "Global Trainee" programme
# may want graduates; an internship takes students.
_TRAINEE_WORD_RE = re.compile(r"(?i)\b(?:trainees?|traineeship|academy)\b")


def _graduate_hire(title: str, description: str | None) -> bool:
    """A training position that needs a finished degree: the text asks for
    one, or (unless the title says internship) is written for recent
    graduates; and it is not open to students."""
    if not description:
        return False
    text = fold(description)
    if _OPEN_TO_STUDENTS_RE.search(text):
        return False
    if _FINISHED_DEGREE_RE.search(text):
        return True
    internship_title = INTERN_RE.search(_TRAINEE_WORD_RE.sub(" ", title or ""))
    return not internship_title and bool(_RECENT_GRADUATES_RE.search(text))


def is_training_position(
    title: str, board_level: str | None = None, description: str | None = None,
) -> bool:
    """The intern level: a training title (internship, traineeship,
    apprenticeship, working student, stagiu) or the board's "intern" label,
    not a senior title, and not a graduate hire (_graduate_hire)."""
    title = split_glued_level(str(title or "")).lower()
    if says_senior(title):
        return False
    if not (INTERN_RE.search(title) or board_level == "intern"):
        return False
    return not _graduate_hire(title, description)


# Past junior, short of SENIOR_RE: "Mid-Level", "Medior", "Engineer II".
MID_TITLE_RE = re.compile(
    r"(?i)\b(?:mid(?:dle)?(?:.?level)?|medior|intermediate|experienced|expert|"
    r"ii|iii|iv)\b"
)

# Required experience as postings state it: "3+ years", "minimum 2-4
# years", "two (2) years", "minim 3 ani", "un an de experiență", "min. 24
# luni", "18 months". Only counted near the word "experience" (see
# required_years).
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9,
    "un": 1, "unu": 1, "doi": 2, "doua": 2, "trei": 3, "patru": 4, "cinci": 5,
}
_YEARS_RE = re.compile(
    r"(?<![\w.,])(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")"
    r"(?:\s*\(\d{1,2}\))?\s*\+?\s*"
    r"(?:(?:-|–|to|la|sau|or|si|and)\s*\d{1,2}\s*\+?\s*)?"
    r"(?P<unit>years?|yrs?|y(?=\+|\s+of\b)|ani|an|months?|luni|luna)\b"
)


def _is_months(m: re.Match[str]) -> bool:
    return m.group("unit").startswith(("mon", "lun"))
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
# The wish written before the number: a heading ("Nice to have: 3 years",
# "Bonus - 2 years") or an adverb ("Preferably 3 years"). Not a bare
# "preferred", which ends the previous clause once scraping has lost its
# period ("a degree is strongly preferred 3-6 years of experience").
_NICE_TO_HAVE_BEFORE_RE = re.compile(
    r"(?:\b(?:nice to have|preferred|bonus|optional|plus|ideal|ideally)\s*[:\-–]|"
    r"\b(?:preferably|ideally|optionally|de preferat))\s*$"
)
# Sentences and bullets ("min." is not an end). A line ending in ":" is a
# heading for the next one ("Experience:\n- 3+ years"), so it stays attached.
_SENTENCE_SPLIT_RE = re.compile(r"(?<!\bmin)(?<!\bnr)[.;!?](?=\s|$)|(?<!:)\n|•")
# "Experience." or "Required experience" on its own: the next line's heading.
_HEADING_RE = re.compile(
    r"\W*(?:(?:required|relevant|professional|work|previous|prior|minimum)\s+)?"
    r"experien\w*(?:\s+(?:required|necesar\w*|minim\w*|profesional\w*|anterioar\w*))?\W*"
)
# Clauses. The years and "experience" must share one: "Fixed-term contract
# for 2 years, experience with Excel" is two. Brackets do not break one
# ("experiența în Azure (1 an) constituie un avantaj").
_CLAUSE_BREAK_RE = re.compile(r",\s|\s[-–|]\s")
_EXPERIENCE_WORD = r"(?:experien\w*|exp\b|practic[ae]\b)"
# Between the years and "experience": "3+ years of professional
# experience", "5+ years of backend software development experience", "3
# ani de experiență". A few words, none of them saying the years are
# something else ("4 years old", "5 years on the market"). "4+ years of
# Swift development", "8+ years in the industry" name the experience
# without the word, and "10+ years required" needs no word at all. "3+
# years as DevOps/Database engineer" names a role instead (Meteocontrol's
# posting, sent to junior searches on 2026-10-16).
_ROLE_WORD = (
    r"(?:engineer|developer|programmer|analyst|tester|administrator|architect|"
    r"consultant|designer|specialist|scientist|accountant|"
    r"inginer|programator|dezvoltator|analist|contabil)s?\b"
)
_GAP_WORD = r"(?!old\b|ago\b|on\b|in\s+business\b|pe\b|we\b|our\b|you\b|will\b)\S+\s+"
_YEARS_THEN_EXPERIENCE_RE = re.compile(
    r"^[\s'’)+]*(?:"
    r"(?:" + _GAP_WORD + r"){0,8}" + _EXPERIENCE_WORD + r"|"
    r"(?:of|de|in)\s+(?:" + _GAP_WORD + r"){0,7}"
    r"(?:development|programming|engineering|dezvoltare|programare)\b|"
    r"in\s+(?:(?:the|a|this)\s+)?(?:industry|field|domain|domeniu|similar\s+(?:role|position))\b|"
    r"(?:as|ca)\s+(?:an?\s+|un\s+|o\s+)?(?:\S+\s+){0,4}?" + _ROLE_WORD + r"|"
    r"(?:required|needed|necesar\w*|obligatori\w*)\b)"
)
# "Experience 3+ years", "experiență minimă 2 ani", "experience: at least 3
# years", "experiență în domeniul ... de minim 2 ani": "experience" a word
# or two before the number, or further back with a requirement word just
# before it.
_REQUIREMENT_WORD = (
    r"(?:min(?:im(?:um|al|a)?)?\.?|at least|cel putin|of|de|over|peste|more than|"
    r"mai mult de|:|-|–|,|\()"
)
_EXPERIENCE_THEN_YEARS_RE = re.compile(
    _EXPERIENCE_WORD + r"(?:\s*:)?(?:\s+\S+){0,2}?\s*$|"
    + _EXPERIENCE_WORD + r".{0,80}?" + _REQUIREMENT_WORD + r"\s*$",
    re.S,
)
_ONLY_REQUIREMENT_WORDS_RE = re.compile(r"\s*(?:" + _REQUIREMENT_WORD + r"\s*)*")
# "Have worked 3 years as a developer": experience without the word.
_WORKED_RE = re.compile(r"\b(?:worked|working|lucrat)\s+(?:for\s+)?(?:at least\s+|over\s+)?$")
# Years the posting says it does not ask for. General phrasing, either
# side of the number: "we are not looking for / do not require / don't
# need 5 years", "nu căutăm / nu este necesară ...", "5 years ... is not
# required".
_NOT_WANTED_BEFORE_RE = re.compile(
    r"\b(?:not looking for|not seeking|(?:do|does|did) not (?:require|need|ask for|expect)|"
    r"(?:don't|doesn't|dont|doesnt) (?:require|need|ask for|expect)|"
    r"no need (?:for|to have|to)|without|"
    r"nu cautam|nu avem nevoie de|nu (?:este|e|sunt) (?:necesar\w*|obligatori\w*)|"
    r"nu (?:necesita|solicitam|cerem|pretindem))\b"
)
_NOT_WANTED_AFTER_RE = re.compile(
    r"\b(?:not (?:required|necessary|needed|mandatory)|optional|"
    r"nu (?:este|e|sunt) (?:necesar\w*|obligatori\w*))\b"
)
# Years of something other than experience, right after the number.
_OTHER_YEARS_RE = re.compile(r"^\s*(?:contract\w*|warranty|garantie|ago|in urma)\b")
# A company's age, not the candidate's experience: "on the market", "pe
# piață", "10 ani de activitate", and for ten years and more "we have 15
# years of experience", "a company with over 10 years of experience".
_COMPANY_AGE_RE = re.compile(
    r"\b(?:on the market|pe piata|in business|founded|since|de activitate|"
    r"of activity|of history|in our (?:team|company)|in the company|"
    r"in echipa noastra|in compania noastra)\b"
)
_COMPANY_SUBJECT_RE = re.compile(
    r"\b(?:we have|we've|we bring|avem|are peste|has over|have over|has more than|"
    r"with over|with more than|cu peste|in peste|company|compania|companie|firma)\b"
)
# The candidate's age, per mention: "18 years or older", "vârsta minimă 18
# ani", "ai peste 18 ani".
_AGE_AFTER_RE = re.compile(
    r"^\s*(?:old|or older|or over|and over|and older|of age|impliniti|de ani impliniti)\b"
)
_AGE_WORD_RE = re.compile(r"\b(?:varsta\w*|age|aged|majorat\w*)\b")
_AGE_BEFORE_RE = re.compile(r"\b(?:peste|over|at least|minim\w*|must be|be|esti|ai)\s*$")


@functools.lru_cache(maxsize=4096)
def required_years(description: str | None) -> int | None:
    """The years of experience a posting requires; None if unstated.

    A number counts only when "experience"/"experiență" names it in the
    same clause: a few words after it ("3+ years of professional
    experience") or before it ("Experience: at least 3 years"). Written
    experience-first, the number may sit in the next clause when nothing
    but a requirement word comes before it ("experiență minimă în
    contabilitate, 1-2 ani"). "A company with 20 years on the market", "a
    1-year contract", "Within 3 years you will grow into a lead", "18 years
    or older" are not requirements. A range counts as its lower bound
    ("3-5 years" is 3). Nice-to-haves and years the posting says it does
    not need are skipped, and of the rest the largest wins: "5+ years of
    experience overall, 2 years with Docker" asks for five.

    Cached: the match stage asks for every subscription and keyword.
    """
    if not description:
        return None
    found: list[int] = []
    for sentence in _sentences(fold(description)):
        for m in _YEARS_RE.finditer(sentence):
            years = _requirement(sentence, m)
            if years is not None:
                found.append(years)
    return max(found) if found else None


def _sentences(text: str) -> list[str]:
    """Sentences, with a lone "Experience." heading joined to the next."""
    out: list[str] = []
    heading = ""
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if _HEADING_RE.fullmatch(sentence):
            heading += sentence.strip() + ": "
            continue
        out.append(heading + sentence)
        heading = ""
    return out


def _requirement(sentence: str, m: re.Match[str]) -> int | None:
    head, rest = sentence[: m.start()], sentence[m.end() :]
    head_clause = _CLAUSE_BREAK_RE.split(head)[-1]
    rest_clause = _CLAUSE_BREAK_RE.split(rest, maxsplit=1)[0]
    # This mention's own tail: up to a bracket or the next number ("At
    # least 4 years (5 years is a plus)" asks for four).
    tail = re.split(r"[(\d]", rest_clause, maxsplit=1)[0]
    raw = m.group(1)
    years = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
    if _is_months(m):
        # Rounded up, so "more than JUNIOR_MAX_YEARS" holds exactly when
        # the months are more than 24: "min. 24 luni" (8319) is two years,
        # "18 luni" is two, 30 months is three.
        years = -(-years // 12)
    if not 1 <= years <= 30 or _OTHER_YEARS_RE.match(rest_clause):
        return None

    if _YEARS_THEN_EXPERIENCE_RE.match(rest_clause) or _WORKED_RE.search(head_clause):
        wish = tail
    else:
        before = _EXPERIENCE_THEN_YEARS_RE.search(head)
        if before is None:
            return None
        crosses = before.start() < len(head) - len(head_clause)
        if crosses and not _ONLY_REQUIREMENT_WORDS_RE.fullmatch(head_clause):
            return None
        wish = head[before.start() :] + tail
    if _NICE_TO_HAVE_RE.search(wish) or _NICE_TO_HAVE_BEFORE_RE.search(head):
        return None
    if _NOT_WANTED_BEFORE_RE.search(head_clause) or _NOT_WANTED_AFTER_RE.search(tail):
        return None
    clause = head_clause + m.group(0) + rest_clause
    if _COMPANY_AGE_RE.search(clause):
        return None
    if years >= 10 and _COMPANY_SUBJECT_RE.search(head_clause):
        return None
    if (
        _AGE_AFTER_RE.match(rest_clause)
        or _AGE_WORD_RE.search(clause)
        or (16 <= years <= 19 and _AGE_BEFORE_RE.search(head_clause))
    ):
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
    # One level each (owner, 2026-10-17): a training position is the intern
    # level, even titled "Graduate Intern" or "Trainee/Junior Consultant".
    if is_training_position(title, experience_level, description):
        return False
    if JUNIOR_RE.search(title):
        return True
    # An explicit senior title outranks the board's label, which keeps the
    # most junior of the levels the employer ticked.
    if says_senior(title) or MID_TITLE_RE.search(title):
        return False
    if experience_level in ("mid", "senior"):
        return False
    # An "entry" label still needs the years to agree: eJobs and BestJobs
    # keep the most junior of the levels the employer ticked, and 28 of 52
    # wrong production DMs came through the label alone.
    return _asks_junior_years(title, description)


def _asks_junior_years(title: str, description: str | None) -> bool:
    """No more than JUNIOR_MAX_YEARS asked for, or none stated, and not a
    posting written for experienced people (asks_experienced)."""
    # In a title the years are the requirement ("Java Developer (3+ years)");
    # months there are a duration ("12 month contract").
    in_title = [
        int(m.group(1)) for m in _YEARS_RE.finditer(fold(title))
        if m.group(1).isdigit() and not _is_months(m)
    ]
    years = max(in_title) if in_title else required_years(description)
    if years is not None and years > JUNIOR_MAX_YEARS:
        return False
    return not asks_experienced(description)


# Postings that say who they want without a number: "we are looking for an
# experienced Software Developer", "an experienced and motivated System
# Engineer", "Proven customer-facing experience in a technical support
# role", "Previous experience working in localization engineering", "We are
# looking for Artificial Intelligence Engineering Experts", "Proven
# expert-level track record" (NTT DATA's AI Engineer, 2026-10-16).
# Production DMs judged wrong (2026-10-16) were sent to junior
# searches through these. Not the team ("alongside experienced
# colleagues", "join our experienced and motivated team"), nor tools
# ("proven experience working with Excel" is how data-analyst postings
# open to graduates put it).
_EXPERIENCED_RE = re.compile(
    r"\b(?:looking for|searching for|seeking|hiring|we need|in search of)\s+"
    r"(?:an?\s+|one\s+)?"
    r"(?:(?:highly|very|motivated|talented|skilled|passionate|dedicated|driven)\s+(?:and\s+)?)*"
    r"(?:experienced|seasoned)\b(?!\s+(?:team|colleagues|people|peers|mentors)\b)|"
    r"\b(?:experienced|seasoned)\s+and\s+(?:motivated|passionate|driven|dedicated|skilled|talented)\s+"
    r"(?!(?:team|colleagues|people|peers|mentors|professionals)\b)|"
    r"\b(?:looking for|searching for|seeking|hiring|in search of)\s+(?:an?\s+)?"
    r"(?:(?!(?:to|who|that|and|or|future|young|aspiring|become)\b)[\w-]+\s+){0,4}?experts?\b|"
    r"\bproven\s+(?:[\w-]+\s+){0,3}?experience\s+(?:as|in)\b|"
    r"\bproven\s+(?:[\w-]+\s+){0,3}?track\s+record\b|"
    r"\bprevious\s+(?:[\w-]+\s+){0,2}?experience\s+(?:working\s+)?(?:in|as|with)\b|"
    r"\bcautam\s+(?:un|o)\s+(?:\w+\s+){0,3}?experimentat\w*"
)


_NO_BEFORE_RE = re.compile(r"\b(?:no|not|without|fara)\s+(?:\w+\s+)?$")
# Wishes said in other words: "Previous experience in a regulated industry
# would be highly valuable" (Perficient's associate QA posting).
_WISH_RE = re.compile(
    r"\b(?:valuable|beneficial|desirable|an asset|welcome[ds]?|helpful|appreciated)\b"
)


@functools.lru_cache(maxsize=4096)
def asks_experienced(description: str | None) -> bool:
    """The description asks for an experienced candidate in words, as a
    requirement: not as a nice-to-have ("proven experience with Docker is
    a plus") nor negated ("we are not looking for an experienced ...")."""
    if not description:
        return False
    for sentence in _sentences(fold(description)):
        for m in _EXPERIENCED_RE.finditer(sentence):
            # Either side: "Preferred qualifications: proven experience in
            # backend development", "... is a plus".
            if _NICE_TO_HAVE_RE.search(sentence) or _WISH_RE.search(sentence):
                continue
            if _NOT_WANTED_BEFORE_RE.search(sentence[: m.end()]):
                continue
            # "No previous experience required", "... is not required".
            if _NO_BEFORE_RE.search(sentence[: m.start()]) or _NOT_WANTED_AFTER_RE.search(
                sentence[m.end():]
            ):
                continue
            return True
    return False


# Below this a description says nothing about the level (an empty Hipo
# posting, a LinkedIn card cut to a line): see lacks_level_evidence.
MIN_EVIDENCE_CHARS = 200


def lacks_level_evidence(
    title: str | None, experience_level: str | None, description: str | None,
) -> bool:
    """A title with no level marker (intern, trainee, working student,
    junior, graduate, entry level) and no description to read the level
    from. The junior level takes unmarked titles on what the description
    says (years, "experienced"); with none, "AI/ML Engineer" went to junior
    searches on no evidence. For the DMs only: the scrape stage and the
    feed see postings before their description is fetched."""
    title = split_glued_level(str(title or ""))
    if (
        ENTRY_LEVEL_RE.search(title) or JUNIOR_RE.search(title) or INTERN_RE.search(title)
        or experience_level == "intern"
    ):
        return False
    return len(" ".join((description or "").split())) < MIN_EVIDENCE_CHARS


# ── DM junior rule: positive evidence (switchable) ────────────────────
# Which rule decides whether an unmarked title goes to a junior DM search:
#   "evidence": only on positive evidence that beginners can apply
#               (has_entry_evidence). Fails closed: a phrasing it does not
#               know costs a DM, it never sends a wrong one.
#   "default":  unless the text says otherwise (years, "experienced"), the
#               rule the scrape stage and the web feed keep. The one-line
#               rollback.
# gosha-label's audit of the 2026-10-17 10:48 prod snapshot: 17 of 96 jobs
# passing intern/junior searches were wrong, 14 through the default, each
# through a phrasing no pattern list had ("Strong experience with Java
# 8+", "Several years of professional experience"). "evidence" since
# 2026-10-17: see docs/EVAL.md.
DM_JUNIOR_RULE = "evidence"
DM_ENTRY_LABEL_IS_EVIDENCE = True

_ZERO_YEARS_RE = re.compile(
    r"(?<![\w.,])0\s*(?:\+|-|–|to|la|sau|or)?\s*(?:\d{1,2}\s*)?(?:years?|yrs?|ani|an)\b|"
    r"\b(?:up to|max(?:imum)?\.?|less than|under|sub|pana la|maxim\w*)\s+"
    r"(?:1|2|one|two|un|doi|doua)\s*(?:years?|yrs?|ani|an)\b"
)
_NO_EXPERIENCE_RE = re.compile(
    r"\bno\s+(?:prior\s+|previous\s+|professional\s+|relevant\s+)?(?:work\s+)?experience\b|"
    r"\bexperience\s+(?:is\s+)?not\s+(?:required|necessary|mandatory|needed)\b|"
    r"\bwithout\s+(?:any\s+)?(?:prior\s+|previous\s+)?(?:work\s+)?experience\b|"
    r"\bfara\s+(?:\w+\s+)?experienta\b|"
    r"\bnu\s+(?:este|e)\s+(?:necesara|obligatorie)\s+experienta\b|"
    r"\bexperienta\s+(?:anterioara\s+|profesionala\s+)?nu\s+(?:este|e)\s+"
    r"(?:necesara|obligatorie)\b"
)
# Who the posting is for, or what it starts: graduates, students, a first
# job, a career start; "we train you", "basic knowledge of", "eager to
# learn".
# "Graduate" as who the role is for, not a degree asked for ("University
# graduate in Computer Science", "graduated from a technical university",
# "absolvent al unei facultăți tehnice", which postings for experienced
# people carry too): recent/new graduates, graduates welcome, for
# graduates, a graduate programme/role; "proaspăt absolvent", "studenți și
# absolvenți", "pentru absolvenți".
_GRADUATE_AUDIENCE = (
    r"\b(?:recent|fresh|new)\s+grad(?:uate)?s?\b|"
    r"\bgraduates?\s+(?:are\s+|is\s+)?(?:very\s+|more\s+than\s+|also\s+)?"
    r"(?:welcome|encouraged|invited|eligible)\b|"
    r"\b(?:for|open\s+to|welcome|welcomes|welcoming|ideal\s+for|suitable\s+for|aimed\s+at|"
    r"targeted\s+at|applications\s+from)\s+(?:\w+\s+){0,2}?graduates\b|"
    r"\bgraduate\s+(?:program(?:me)?s?|scheme|role|roles|position|positions|job|jobs|"
    r"opportunit\w+|trainee\w*|intake)\b|"
    r"\b(?:students?|interns?)\s+(?:and|or|&)\s+(?:recent\s+)?graduates\b|"
    r"\bproaspat\w*\s+absolvent\w*|"
    r"\b(?:studenti|elevi)\s+(?:si|sau)\s+absolvent\w*|\babsolvent\w*\s+(?:si|sau)\s+student\w*|"
    r"\b(?:pentru|deschis\w*\s+(?:si\s+)?(?:pentru|catre))\s+absolvent\w*|"
    r"\babsolvent\w*\s+(?:sunt\s+)?(?:bineveniti|incurajati)"
)
_ENTRY_AUDIENCE_RE = re.compile(
    _GRADUATE_AUDIENCE + r"|\bstudent\w*|"
    r"\bentry[\s-]+level\b|\bjuniors?\b|"
    r"\bcareer\s+starters?\b|"
    r"\b(?:start|kick[\s-]*start|launch|begin|jump[\s-]*start)\s+(?:your|their)\s+"
    r"(?:professional\s+)?career\b|"
    r"\b(?:first|1st)\s+job\b|\bprimul\s+(?:tau\s+)?(?:loc\s+de\s+munca|job)\b|"
    r"\b(?:la\s+)?inceput(?:ul)?\s+de\s+cariera\b|\binceputul\s+carierei\b|"
    r"\bbeginners?\b|\bincepator\w*|\bdebutant\w*|\btrainee\w*|"
    r"\bwe\s+(?:will\s+)?train\s+you\b|\bte\s+(?:vom\s+)?forma\w*\b|"
    r"\bdon'?t\s+(?:yet\s+)?meet\s+all\b"
)
# Not evidence on their own: "basic knowledge/understanding of", "eager to
# learn". Postings for experienced people write them next to the experience
# they demand (Siemens SRM 8266: "Basic understanding of system integration"
# beside "Oracle database administration experience", "Tcl/Tk experience";
# NTT QA 8380). Of the 25 judged postings whose only evidence they were,
# 2 were right (sent DMs 0 of 12, gosha-label's audit 1 of 7, eval 1 of 6).
# Not evidence when the posting is above them ("mentor junior engineers")
# or turns them away ("students will not be considered").
_ABOVE_AUDIENCE_RE = re.compile(
    r"\b(?:mentor\w*|coach\w*|guid\w*|lead\w*|supervis\w*|manag\w*|train\w*|support\w*|"
    r"onboard\w*|teach\w*|help\w*|grow\w*|develop\w*)\s+(?:\w+\s+){0,2}$"
)
# Juniors, students, trainees as the people the role works with, not as
# who it is for: "work with junior and senior engineers", "alongside
# juniors", "a team of juniors and seniors", "collaborate with students".
_PEOPLE_RE = re.compile(r"junior|student|trainee|beginner|incepator|debutant|absolvent|grad")
_COLLEAGUES_BEFORE_RE = re.compile(
    r"\b(?:with|alongside|among|amongst|beside|team\s+of|teams\s+of|cu|alaturi\s+de|"
    r"impreuna\s+cu|echipa\s+de|echipe\s+de)\s+(?:[\w-]+\s+){0,2}$|"
    r"\b(?:seniors?|mid\w*|experienced)\s+(?:and|or|&|si|sau)\s+$"
)
_COLLEAGUES_AFTER_RE = re.compile(
    r"^\w*\s*(?:,\s*)?(?:(?:and|or|&|si|sau|to)\s+)?(?:mid\w*\s*(?:,\s*)?(?:and\s+|or\s+|si\s+)?)?"
    r"(?:seniors?|seniori|experienced|principal)\b"
)
_TURNED_AWAY_RE = re.compile(
    r"\bnot\s+(?:be\s+)?(?:considered|eligible|accepted|suitable)\b|\bnu\s+(?:vor|sunt)\b"
)
# Titles at the bottom of a ladder that do not say "junior": assistant,
# clerk, associate, and level one of a numbered ladder ("Technical Analyst
# I", "Engineer 1", "L1 Service Technician", "Level 1 Support"). The
# numeral is matched case-sensitively: "I" is not the pronoun.
_ENTRY_ROLE_TITLE_RE = re.compile(
    r"(?i)\b(?:assistant|asistent\w*|clerk|associate)\b|"
    r"(?i:\b(?:analyst|engineer|developer|specialist|consultant|technician|administrator|"
    r"accountant|tester|scientist|representative|agent))\s+(?:I|1)(?![\w.])|"
    r"(?i:\b(?:level\s*1|l1|tier\s*1|nivel\s*1))\b"
)
# A minimum graduation grade asked for: a graduate hire. "Candidates must
# hold a completed university degree and have achieved a minimum graduation
# grade of 9/10" (The Access Group's "Software Engineer", 8294, whose
# experience is all "might also include"), "minimum GPA of 3.5", "media de
# absolvire minim 8". A degree alone is not one: postings for experienced
# people ask for degrees too.
_GRADE_REQUIRED_RE = re.compile(
    r"\b(?:graduation|degree|diploma|licenta|absolvire)\s+(?:grade|average|mark|gpa)s?\b|"
    r"\bgpa\b|\bgrade\s+point\s+average\b|"
    r"\bmedi[ae]\s+(?:generala\s+)?(?:de\s+|la\s+)?(?:absolvire|licenta)\b|"
    r"\bnota\s+(?:minima\s+)?(?:de\s+|la\s+)?(?:absolvire|licenta)\b"
)
# The same level one named in the text: "As a Technical Analyst I", "As a
# Level 1 Customer Support Consultant". Read on the raw text (case kept).
_LEVEL_ONE_TEXT_RE = re.compile(
    r"\b[Aa]s\s+an?\s+(?:[\w&/-]+\s+){0,3}?"
    r"(?:(?i:analyst|engineer|developer|specialist|consultant|technician|administrator|"
    r"accountant|tester|scientist)\s+(?:I|1)(?![\w.])|(?i:level|tier)\s*1\b)"
)
# Wordings of "beginners welcome" beyond _ENTRY_AUDIENCE_RE, one class each
# (found on the main eval set's "any" postings, 2026-10-17):
# - experience not needed, in other words: "nu aveți nevoie de experiență",
#   "you don't need (prior) experience", "nu căutăm neapărat experiență";
# - with or without it: "chiar dacă ai sau nu experiență", "with or
#   without experience", "cu sau fără experiență";
# - an advantage but not required: "Experiența în sales reprezintă un
#   avantaj, dar nu este obligatorie";
# - training that teaches the job: "necessary training will be ensured",
#   "o perioadă de training în care vei învăța", "te învățăm tot";
# - a new trade: "te califica într-o nouă meserie", "reconversie
#   profesională", "career changers";
# - a school-leaving minimum: "Cerința minimă: diploma de bacalaureat".
_ENTRY_TEXT_RE = re.compile(
    r"\bnu\s+(?:ai|aveti|avem|e|este|sunt)\s+(?:nevoie\s+de|necesar\w*\s+)\s*"
    r"(?:\w+\s+){0,2}?experient\w*|"
    r"\b(?:you\s+)?(?:don'?t|do\s+not|dont)\s+need\s+(?:any\s+)?(?:prior\s+|previous\s+)?"
    r"(?:work\s+)?experience\b|"
    r"\b(?:not\s+necessarily|nu\s+(?:cautam|cerem)\s+neaparat)\s+(?:\w+\s+){0,2}?experien\w*|"
    r"\b(?:ai\s+sau\s+nu|cu\s+sau\s+fara|indiferent\s+daca\s+ai)\s+(?:\w+\s+)?experienta\b|"
    r"\b(?:with\s+or\s+without|whether\s+or\s+not\s+you\s+have)\s+(?:\w+\s+)?experience\b|"
    r"\bexperien\w*[^.;]{0,80}?\b(?:avantaj|advantage|a\s+plus|un\s+plus)\b[^.;]{0,30}?"
    r"\b(?:dar|but)\s+(?:it\s+is\s+|is\s+)?(?:nu\s+(?:este|e)\s+(?:obligatori\w*|necesar\w*)|"
    r"not\s+(?:mandatory|required|necessary|a\s+must))|"
    r"\b(?:necessary|full|complete|all\s+the|initial)\s+training\s+(?:will\s+be\s+|is\s+)?"
    r"(?:ensured|provided|given|offered)\b|"
    r"\btraining\s+(?:complet|integral)\b|\b(?:instruire|formare)\s+complet\w*|"
    r"\bperioad\w*\s+de\s+(?:training|instruire|formare|pregatire)\b|\btraining\s+period\b|"
    r"\b(?:te|va)\s+(?:vom\s+)?invat(?:am|a)\b|\bwe\s+(?:will\s+)?teach\s+you\b|"
    r"\b(?:te\s+)?califica\w*\s+(?:intr-o|intr\s+o|in\s+o)\s+(?:noua\s+)?meserie\b|"
    r"\breconversie\s+profesional\w*|\bcareer\s+chang(?:e|ers?)\b|"
    r"\b(?:minim\w*|cel\s+putin|at\s+least|minimum)\s+(?:\w+\s+){0,3}?"
    r"(?:bacalaureat|studii\s+medii|liceu|high[\s-]+school)\b"
)


def has_entry_evidence(
    title: str | None, experience_level: str | None, description: str | None,
) -> bool:
    """Positive evidence that beginners can apply: a training or junior
    title, the board's entry label with the years agreeing (while
    DM_ENTRY_LABEL_IS_EVIDENCE), an entry-role or level-one title
    (assistant, clerk, associate, "Analyst I", "L1"), at most two years
    asked (even as a wish), "0-2 years", "no experience required" in any
    of its wordings, a graduation grade asked for and no years, or wording
    for graduates, students, a first job, a new trade, "we train you"."""
    # Outside the cache: DM_ENTRY_LABEL_IS_EVIDENCE is a switch.
    if (
        DM_ENTRY_LABEL_IS_EVIDENCE and experience_level == "entry"
        and _asks_junior_years(split_glued_level(str(title or "")), description)
    ):
        return True
    return _entry_evidence(title, experience_level == "intern", description)


@functools.lru_cache(maxsize=4096)
def _entry_evidence(title: str | None, intern_label: bool, description: str | None) -> bool:
    title = split_glued_level(str(title or ""))
    if (
        ENTRY_LEVEL_RE.search(title) or JUNIOR_RE.search(title) or INTERN_RE.search(title)
        or intern_label
        or _ENTRY_ROLE_TITLE_RE.search(title)
    ):
        return True
    if not description:
        return False
    years = required_years(description)
    if years is not None and years <= JUNIOR_MAX_YEARS:
        return True
    if _LEVEL_ONE_TEXT_RE.search(description):
        return True
    text = fold(description)
    if years is None and _GRADE_REQUIRED_RE.search(text):
        return True
    # "Minim 1-2 ani reprezintă un avantaj": at most two years, as a wish.
    wished = required_years(_NICE_TO_HAVE_RE.sub(" ", text))
    if wished is not None and wished <= JUNIOR_MAX_YEARS:
        return True
    for sentence in _sentences(text):
        if (
            _ZERO_YEARS_RE.search(sentence) or _NO_EXPERIENCE_RE.search(sentence)
            or _ENTRY_TEXT_RE.search(sentence)
        ):
            return True
        if _TURNED_AWAY_RE.search(sentence):
            continue
        for m in _ENTRY_AUDIENCE_RE.finditer(sentence):
            before, after = sentence[: m.start()], sentence[m.end() :]
            if _ABOVE_AUDIENCE_RE.search(before):
                continue
            if _PEOPLE_RE.search(m.group(0)) and (
                _COLLEAGUES_BEFORE_RE.search(before) or _COLLEAGUES_AFTER_RE.match(after)
            ):
                continue
            return True
    return False


def dm_junior_ok(
    title: str | None, experience_level: str | None, description: str | None,
) -> bool:
    """Whether a posting may go to an intern/junior DM search on its level,
    under DM_JUNIOR_RULE. The one switch for the DM junior rule: the
    match stage asks this after matches_experience_level has passed."""
    if DM_JUNIOR_RULE == "default":
        return True
    return has_entry_evidence(title, experience_level, description)


def entry_level_search(levels: list[str] | None, keywords: list[str] | None) -> bool:
    """A search whose matches must be intern or junior: its levels say so,
    or, with no level restriction, its keyword does ("software intern")."""
    wanted = {canonical_level(level) or level.lower() for level in levels or []}
    if wanted and "any" not in wanted:
        return wanted <= {"intern", "junior"}
    return any(keyword_wants_entry_level(kw) for kw in keywords or [])


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
    title_lower = split_glued_level(str(title)).lower() if title else ""

    if "intern" in wanted and is_training_position(title_lower, experience_level, description):
        # Training positions only: a training title or the board's own
        # "intern" label, never a senior title or a graduate hire.
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


def filter_dataframe(
    df: pd.DataFrame, keyword: str | list[str], levels: list[str] | None = None,
    *, locations: list[str] | None = None, remote_ok: bool = False,
) -> pd.DataFrame:
    """Apply title-relevance filtering to a scraped DataFrame.

    Accepts a single keyword or a list. A job passes if it's relevant
    for *any* of the provided keywords. An `experience_level` column
    (native adapters) is passed through as the board's seniority label,
    and a `description` column for the years of experience asked.
    With `locations` (the query's place), a job must also be somewhere
    its searches take (search_location_ok, as at match time): boards
    answer a Cluj query with US postings, and 1,201 of them were stored.
    """
    if df.empty or "title" not in df.columns:
        return df

    keywords = [keyword] if isinstance(keyword, str) else keyword
    relevant = relevance_predicate(keywords, levels)

    def level(value: object) -> str | None:
        return value if isinstance(value, str) else None

    levels = df["experience_level"] if "experience_level" in df.columns else [None] * len(df)
    descriptions = df["description"] if "description" in df.columns else [None] * len(df)
    places = df["location"] if "location" in df.columns else [None] * len(df)
    mask = [
        relevant(str(t), level(lvl), level(desc))
        and (locations is None or search_location_ok(level(place), locations, remote_ok))
        for t, lvl, desc, place in zip(df["title"], levels, descriptions, places)
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


def matched_level(
    title: str, levels: list[str] | None, board_level: str | None, description: str | None,
) -> str | None:
    """The level a search took a posting at, for the DM ("Level: Internship"):
    "intern" when the search wants interns and it is a training position
    (it wins for an intern+junior search), "junior" when the search wants
    junior and it is a junior role, else None (any/mid/senior searches, or
    neither). The intern and junior branches of matches_experience_level."""
    wanted = {canonical_level(level) or level.lower() for level in levels or []}
    title = split_glued_level(str(title or "")).lower()
    if "intern" in wanted and is_training_position(title, board_level, description):
        return "intern"
    if "junior" in wanted and _is_junior(title, board_level, description):
        return "junior"
    return None
