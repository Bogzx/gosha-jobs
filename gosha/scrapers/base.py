"""Core types for scraper adapters."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class SearchQuery:
    """One scrape request: a keyword in a location."""

    keyword: str
    location: str
    max_age_days: int = 14

    def __post_init__(self) -> None:
        object.__setattr__(self, "keyword", self.keyword.strip().lower())
        object.__setattr__(self, "location", self.location.strip())


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
        }


class BaseScraper(Protocol):
    """Adapter interface; implementations live in this package."""

    name: str

    async def search(self, query: SearchQuery) -> list[RawJob]:
        """Return matching jobs, or [] on any failure (never raise)."""
        ...
