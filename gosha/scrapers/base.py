"""Core types for scraper adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/html;q=0.9",
}
HTTP_TIMEOUT = 25.0

# Seniority as a board states it in structured data, independent of the
# title: eJobs' careerLevelsIds, Jobicy's jobLevel. Romanian postings
# rarely put "junior" in the title while the board files them as
# entry-level, so title-only filtering threw most of them away.
EXPERIENCE_LEVELS = ("intern", "entry", "mid", "senior")


@dataclass(frozen=True)
class SearchQuery:
    """One scrape request: a keyword in a location.

    `relevant` is the pipeline's title filter for the subscription this
    search serves. Adapters use it to spend their detail-page budget on
    postings that will be kept, instead of on "Agent Vanzari" results a
    fuzzy board search returned for "python". It never removes a job from
    the result — filtering stays in the pipeline.
    """

    keyword: str
    location: str
    max_age_days: int = 14
    relevant: Callable[[str, str | None], bool] | None = field(
        default=None, compare=False, repr=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "keyword", self.keyword.strip().lower())
        object.__setattr__(self, "location", self.location.strip())

    def is_relevant(self, title: str, experience_level: str | None = None) -> bool:
        return self.relevant is None or self.relevant(title, experience_level)


@dataclass
class RawJob:
    """A scraped posting, source-agnostic."""

    url: str
    title: str
    company: str = "Unknown"
    location: str = ""
    description: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    # What the amounts are per: "monthly", "yearly", "hourly", ... Adapters
    # must state it, because boards disagree — eJobs quotes monthly RON and
    # RemoteOK quotes annual USD, and without the label the two are
    # indistinguishable numbers. See gosha/salary.py.
    salary_period: str | None = None
    posted_at: datetime | None = None
    source: str = "unknown"
    # One of EXPERIENCE_LEVELS when the board says so; None otherwise.
    experience_level: str | None = None

    def to_record(self) -> dict[str, Any]:
        """Column names match what gosha.pipeline.upsert_jobs reads."""
        return {
            "job_url": self.url,
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "description": self.description,
            "site": self.source,
            "min_amount": self.salary_min,
            "max_amount": self.salary_max,
            "currency": self.salary_currency,
            "interval": self.salary_period,
            "date_posted": self.posted_at,
            "experience_level": self.experience_level,
        }


class BaseScraper(Protocol):
    """Adapter interface; implementations live in this package."""

    name: str

    def applies_to(self, query: SearchQuery) -> bool:
        """Whether this board can serve the query's location at all.

        A board that cannot (RemoteOK for "Cluj", Hipo for "Berlin") is not
        asked and is not counted as attempted, so scrape health does not
        mistake "not applicable" for "broken".
        """
        ...

    async def search(self, query: SearchQuery) -> list[RawJob]:
        """Return matching jobs, or [] on any failure (never raise)."""
        ...
