"""Core types for scraper adapters."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

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

# Characters that render as nothing: format characters (category Cf: zero-
# width space/joiners, word joiner, BOM, soft hyphen, bidi controls, tag
# characters), variation selectors, and control characters other than tab
# and newline. eJobs ends description lines with a watermark of variation
# selectors and zero-width spaces ("Cloud & AWS\ufe01\ufe00\u200b..."), and
# RemoteOK has stray C1 controls; they became TF-IDF tokens, embedding
# noise and part of the dedup key.
_INVISIBLE_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u00ad\u0600-\u0605\u061c\u06dd\u070f"
    "\u08e2\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f"
    "\ufe00-\ufe0f\ufeff\ufff9-\ufffb\U000110bd\U0001bca0-\U0001bca3"
    "\U0001d173-\U0001d17a\U000e0001\U000e0020-\U000e007f\U000e0100-\U000e01ef]"
)


def strip_invisible(text: str) -> str:
    """`text` without characters that render as nothing (see _INVISIBLE_RE)."""
    return _INVISIBLE_RE.sub("", text)

# Hosts that refused a plain request (403) and answered one made with a
# browser's TLS fingerprint. Process-wide, so the refused request is paid
# once per host and process, not once per search.
_browser_hosts: set[str] = set()
# Headers curl has already applied to the body it hands back.
_DECODED_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


async def _browser_get(request: httpx.Request) -> httpx.Response | None:
    """`request` re-sent by curl_cffi with Chrome's TLS fingerprint.

    None when curl_cffi is missing or the request fails: the caller then
    keeps the response it already has.
    """
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:  # pragma: no cover - curl_cffi ships with jobspy
        return None
    timeout = request.extensions.get("timeout", {}).get("read") or HTTP_TIMEOUT
    try:
        async with AsyncSession(impersonate="chrome", timeout=timeout) as session:
            resp = await session.get(
                str(request.url),
                headers={
                    k: v for k, v in request.headers.items()
                    if k.lower() not in ("host", "accept-encoding", "connection")
                },
            )
    except Exception as exc:
        log.debug("Browser-fingerprint retry of %s failed: %s", request.url, exc)
        return None
    return httpx.Response(
        resp.status_code,
        headers=[
            (k, v) for k, v in resp.headers.items() if k.lower() not in _DECODED_HEADERS
        ],
        content=resp.content,
        request=request,
    )


class BrowserFallbackTransport(httpx.AsyncBaseTransport):
    """httpx transport that retries a 403'd GET with a browser fingerprint.

    BestJobs answers 403 to every request whose TLS handshake is Python's
    (httpx, requests) from some networks while curl and browsers get 200
    from the same address (measured 2026-10-16: the whole board, API and
    website, went dark for that host). The adapter then logged a warning
    and returned nothing, which only the zero-yield alert would notice
    hours later. curl_cffi, which JobSpy already depends on for the same
    reason, makes the handshake look like Chrome's. It is only used for a
    host that has refused a plain request, and only for GETs.

    OFF unless SCRAPE_BROWSER_FALLBACK=true: a 403 aimed at a TLS
    fingerprint is the board's bot protection, and getting around it is
    the operator's call (terms of service), not a default.
    """

    def __init__(self) -> None:
        self._plain = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.method == "GET" and host in _browser_hosts:
            response = await _browser_get(request)
            if response is not None:
                return response
        response = await self._plain.handle_async_request(request)
        if response.status_code != 403 or request.method != "GET":
            return response
        retried = await _browser_get(request)
        if retried is None or retried.status_code == 403:
            return response
        if host not in _browser_hosts:
            log.info("%s refuses Python's TLS fingerprint; using a browser's", host)
            _browser_hosts.add(host)
        await response.aclose()
        return retried

    async def aclose(self) -> None:
        await self._plain.aclose()


def browser_fallback_enabled() -> bool:
    """SCRAPE_BROWSER_FALLBACK=true opts in to BrowserFallbackTransport."""
    value = os.getenv("SCRAPE_BROWSER_FALLBACK", "")
    return value.strip().lower() in ("1", "true", "yes", "on")


def http_client() -> httpx.AsyncClient:
    """The HTTP client every adapter uses: shared headers, timeout and
    redirects, plus the browser-fingerprint fallback when opted in."""
    return httpx.AsyncClient(
        headers=DEFAULT_HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True,
        transport=BrowserFallbackTransport() if browser_fallback_enabled() else None,
    )


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

    def __post_init__(self) -> None:
        # Every adapter's text goes through here on its way to the pipeline.
        for name in ("title", "company", "location", "description"):
            value = getattr(self, name)
            if isinstance(value, str):
                setattr(self, name, strip_invisible(value).strip())

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
