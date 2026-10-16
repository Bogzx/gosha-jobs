"""Cross-board job deduplication.

The same posting often appears on Indeed, LinkedIn, and the Romanian
boards. Rows with the same normalized (title, company) within the recent
window form a group: the lowest-id row is canonical (dedup_group_id points
at itself), the rest point at it. Browse/feed only show canonical rows;
each source URL stays in the DB.

Groups are rebuilt on every run, not only added to. A group loses members
when postings expire (dead link, no longer scraped), age out of the window
or change title; a row left pointing at a canonical that is gone would be
hidden from browse and the feed for good. So a group that still has two
live members re-elects its lowest id, a lone survivor goes back to NULL,
and an older row stays hidden only while its canonical is live, still
canonical and still the same posting.

Same title and employer is not always the same posting: Endava hiring a
"Java Developer" in Cluj and another in Iasi are two jobs, and boards strip
the city from titles, so the key cannot tell them apart. Members of a key
whose locations name different cities are therefore kept in separate
groups (locations_compatible) — hiding the Iasi copy behind the Cluj one
made it unfindable from an Iasi search, since browse and the feed filter
by location after dropping non-canonical rows.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import load_only

from gosha.database import get_session
from gosha.filters import fold
from gosha.models import Job

log = logging.getLogger(__name__)

WINDOW_DAYS = 30

# Legal suffixes that make "Acme SRL" and "Acme" the same employer
_LEGAL_SUFFIXES = re.compile(
    r"\b(s\.?r\.?l\.?|s\.?a\.?|inc|llc|ltd|gmbh|srl)\b", re.IGNORECASE
)
# Romanian legal-form prefix ("S.C. Acme S.R.L.") and a trailing country
# ("Endava Romania SRL"): eJobs names the local entity, LinkedIn the brand.
_LEGAL_PREFIX = re.compile(r"^\s*s\.?\s?c\.?\s+", re.IGNORECASE)
_COUNTRY_SUFFIX = re.compile(r"\s+romania\s*$", re.IGNORECASE)
_PARENTHETICAL = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# "+" and "#" stay inside title tokens: "C++ Developer", "C# Developer" and
# "C Developer" at one employer are three different jobs.
_TITLE_SEPARATORS = re.compile(r"[^a-z0-9+#]+")

# Title tokens that boards bolt on without changing the actual role
_TITLE_NOISE = frozenset({
    "m", "f", "d", "mfd", "remote", "hybrid", "onsite", "on", "site",
    "urgent", "new", "hiring", "now", "ro", "romania", "cluj", "napoca",
    "bucharest", "bucuresti", "timisoara", "iasi", "brasov", "full", "time",
    "part", "job", "position", "role",
})

# What gets stored when a board leaves the employer out ("Unknown" is the
# scrapers' and pipeline's default). Two "Unknown" postings with the same
# title are different employers as often as not, so they are never grouped.
_PLACEHOLDER_COMPANIES = frozenset({"", "unknown", "confidential", "na"})
# Likewise the pipeline's "Unknown Title" (and "Unknown (migrated)" stubs).
_PLACEHOLDER_TITLE_TOKENS = frozenset({"unknown", "title"})


def normalize_key(title: str, company: str) -> str | None:
    """Order-insensitive, decoration-insensitive identity for a posting.

    "Senior Python Developer (Remote) - Cluj" and "Python Developer Senior"
    at the same employer map to the same key; different roles don't.
    None when the posting cannot be identified safely: no real employer
    name, or no title left once the decorations are stripped.

    Both sides are folded to ASCII first (filters.fold): the separators
    below are ASCII, so "București" split into "bucure" + "ti" and escaped
    the city noise list, and "Română" became "romn" next to another
    board's "Romana" — the same posting, never grouped.
    """
    title = _PARENTHETICAL.sub(" ", fold(title or ""))
    tokens = {
        # Leading marks are hashtags ("#remote") or stray separators ("+").
        token.lstrip("+#")
        for token in _TITLE_SEPARATORS.split(title.lower())
    } - _TITLE_NOISE - {""}
    company = _LEGAL_SUFFIXES.sub("", fold(company or ""))
    company = _LEGAL_PREFIX.sub("", company).strip(" .,-")
    company = _COUNTRY_SUFFIX.sub("", company) or company
    company_part = _NON_ALNUM.sub("", company.lower())
    if not tokens or tokens <= _PLACEHOLDER_TITLE_TOKENS:
        return None
    if company_part in _PLACEHOLDER_COMPANIES:
        return None
    return f"{'-'.join(sorted(tokens))}::{company_part}"


# Cities a posting can name, folded, each mapped to one spelling. Romanian
# cities first (the boards' home turf), then the foreign hubs students
# search. A location naming none of them ("Romania", "Remote", "") is
# compatible with any other.
_CITIES = {
    "bucuresti": "bucuresti", "bucharest": "bucuresti",
    "cluj": "cluj", "timisoara": "timisoara", "iasi": "iasi",
    "brasov": "brasov", "sibiu": "sibiu", "craiova": "craiova",
    "constanta": "constanta", "oradea": "oradea", "arad": "arad",
    "targu mures": "targu mures", "targu-mures": "targu mures",
    "baia mare": "baia mare", "pitesti": "pitesti", "ploiesti": "ploiesti",
    "galati": "galati", "suceava": "suceava", "bacau": "bacau",
    "braila": "braila", "alba iulia": "alba iulia",
    "satu mare": "satu mare", "deva": "deva", "otopeni": "otopeni",
    "london": "london", "dublin": "dublin", "berlin": "berlin",
    "munich": "munich", "munchen": "munich", "amsterdam": "amsterdam",
    "warsaw": "warsaw", "warszawa": "warsaw", "krakow": "krakow",
    "prague": "prague", "praha": "prague", "budapest": "budapest",
    "vienna": "vienna", "wien": "vienna", "paris": "paris",
    "barcelona": "barcelona", "madrid": "madrid", "lisbon": "lisbon",
    "zurich": "zurich", "sofia": "sofia", "chisinau": "chisinau",
}
_CITY_RE = re.compile(
    r"\b(" + "|".join(sorted((re.escape(c) for c in _CITIES), key=len, reverse=True))
    + r")\b"
)


def location_cities(location: str | None) -> frozenset[str]:
    """The known cities a location string names ("Cluj-Napoca, CJ, RO")."""
    return frozenset(_CITIES[m] for m in _CITY_RE.findall(fold(location or "")))


def locations_compatible(a: str | None, b: str | None) -> bool:
    """Could two postings with these locations be the same posting?

    Only when they name no city at all (one side) or share one: a posting
    listed for 20 cities matches each of them, "Romania" matches anything.
    """
    cities_a, cities_b = location_cities(a), location_cities(b)
    return not cities_a or not cities_b or bool(cities_a & cities_b)


def _split_by_location(members: list[Job]) -> list[list[Job]]:
    """Members of one key, in id order, grouped so that every pair in a
    group has compatible locations."""
    clusters: list[list[Job]] = []
    for job in sorted(members, key=lambda j: j.id):
        for cluster in clusters:
            if all(locations_compatible(job.location, other.location) for other in cluster):
                cluster.append(job)
                break
        else:
            clusters.append([job])
    return clusters


_GROUP_COLUMNS = load_only(
    Job.id, Job.title, Job.company, Job.location, Job.is_active, Job.dedup_group_id,
)


async def assign_dedup_groups(window_days: int = WINDOW_DAYS) -> int:
    """Regroup recent active jobs by normalized key. Returns duplicates marked."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    async with get_session() as session:
        # Grouping needs a few short columns; descriptions and vectors stay
        # in the DB.
        result = await session.execute(
            select(Job).options(_GROUP_COLUMNS).where(
                Job.is_active.is_(True),
                Job.first_seen_at >= cutoff,
            )
        )
        jobs = list(result.scalars().all())

        groups: dict[str, list[Job]] = {}
        # job id -> the canonical id it should point at (None: shown as is)
        target: dict[int, int | None] = {}
        for job in jobs:
            key = normalize_key(job.title, job.company)
            if key is None:
                target[job.id] = None
            else:
                groups.setdefault(key, []).append(job)
        for same_key in groups.values():
            for members in _split_by_location(same_key):
                canonical_id = members[0].id if len(members) > 1 else None
                for job in members:
                    target[job.id] = canonical_id

        duplicates = 0
        released = 0
        changed = False
        for job in jobs:
            wanted = target[job.id]
            if wanted is not None and wanted != job.id:
                duplicates += 1
            if job.dedup_group_id != wanted:
                if job.dedup_group_id not in (None, job.id):
                    released += 1  # was hidden behind some other row
                job.dedup_group_id = wanted
                changed = True

        # Older active rows are not regrouped, but they must not stay hidden
        # behind a canonical that has expired, stopped being canonical, or
        # turned into a different posting.
        result = await session.execute(
            select(Job).options(_GROUP_COLUMNS).where(
                Job.is_active.is_(True),
                or_(Job.first_seen_at < cutoff, Job.first_seen_at.is_(None)),
                Job.dedup_group_id.isnot(None),
                Job.dedup_group_id != Job.id,
            )
        )
        older = list(result.scalars().all())
        if older:
            canonical_ids = {job.dedup_group_id for job in older}
            result = await session.execute(
                select(Job).options(_GROUP_COLUMNS).where(Job.id.in_(canonical_ids))
            )
            canonicals = {job.id: job for job in result.scalars().all()}
            for job in older:
                canonical = canonicals.get(job.dedup_group_id)
                key = normalize_key(job.title, job.company)
                if (
                    canonical is not None
                    and canonical.is_active
                    and canonical.dedup_group_id == canonical.id
                    and key is not None
                    and key == normalize_key(canonical.title, canonical.company)
                    and locations_compatible(job.location, canonical.location)
                ):
                    continue
                job.dedup_group_id = None
                released += 1
                changed = True

        if changed:
            await session.commit()

    if duplicates:
        log.info("Dedup: %d duplicate postings grouped", duplicates)
    if released:
        log.info("Dedup: %d postings released from groups that broke up", released)
    return duplicates
