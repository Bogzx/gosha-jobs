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
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import load_only

from gosha.database import get_session
from gosha.models import Job

log = logging.getLogger(__name__)

WINDOW_DAYS = 30

# Legal suffixes that make "Acme SRL" and "Acme" the same employer
_LEGAL_SUFFIXES = re.compile(
    r"\b(s\.?r\.?l\.?|s\.?a\.?|inc|llc|ltd|gmbh|srl)\b", re.IGNORECASE
)
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
    """
    title = _PARENTHETICAL.sub(" ", title or "")
    tokens = {
        # Leading marks are hashtags ("#remote") or stray separators ("+").
        token.lstrip("+#")
        for token in _TITLE_SEPARATORS.split(title.lower())
    } - _TITLE_NOISE - {""}
    company = _LEGAL_SUFFIXES.sub("", company or "")
    company_part = _NON_ALNUM.sub("", company.lower())
    if not tokens or tokens <= _PLACEHOLDER_TITLE_TOKENS:
        return None
    if company_part in _PLACEHOLDER_COMPANIES:
        return None
    return f"{'-'.join(sorted(tokens))}::{company_part}"


_GROUP_COLUMNS = load_only(
    Job.id, Job.title, Job.company, Job.is_active, Job.dedup_group_id,
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
        for members in groups.values():
            canonical_id = min(job.id for job in members) if len(members) > 1 else None
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
