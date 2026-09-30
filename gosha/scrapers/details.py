"""Detail-page fetching for boards whose list endpoints carry no description.

BestJobs and Hipo list pages give a title and a company and nothing else,
so those postings used to be embedded as "title, title, at Company" —
which a semantic matcher can do little with. Adapters now fetch the detail
page for the postings a query returns, politely:

- at most `limit` network fetches per search (cached pages are free),
- DETAIL_CONCURRENCY requests in flight per board,
- a process-wide cache (DETAIL_TTL_SECONDS) keyed by URL: the scheduler
  re-runs every search each cycle and expands one keyword into several
  terms, so without it the same page would be fetched over and over.
  Extraction results are cached even when empty, so a page whose markup
  changed is not hammered until the TTL passes; network errors are not
  cached, so they are retried next cycle.

Failures never raise: a posting without a description is still a posting.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import httpx

log = logging.getLogger(__name__)

DETAIL_TTL_SECONDS = 24 * 3600
DETAIL_CONCURRENCY = 3
MAX_CACHE_ENTRIES = 5000
MAX_DESCRIPTION_CHARS = 20000

_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

_BREAK_RE = re.compile(r"</p>|</div>|</li>|</h\d>|<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(markup: str | None) -> str:
    """Readable plain text from an HTML fragment, paragraph breaks kept."""
    text = _BREAK_RE.sub("\n", markup or "")
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_DESCRIPTION_CHARS]


_MOJIBAKE_MARKERS = ("Ã", "Â", "â\x80", "Ä", "È")


def repair_mojibake(text: str) -> str:
    """Undo UTF-8 that was decoded as Latin-1 and re-encoded ("LâOrÃ©al").

    Some Hipo pages ship their JSON-LD double-encoded while the rest of the
    page is fine. The repair only applies when the text survives a Latin-1
    round trip AND comes out with fewer mojibake markers — genuine
    Romanian (ă, ș, ț are outside Latin-1) is never touched.
    """
    if not any(marker in text for marker in _MOJIBAKE_MARKERS):
        return text
    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text

    def markers(s: str) -> int:
        return sum(s.count(m) for m in _MOJIBAKE_MARKERS)

    return repaired if markers(repaired) < markers(text) else text


def clear_cache() -> None:
    _cache.clear()


def _cached(url: str, now: float) -> dict[str, Any] | None:
    entry = _cache.get(url)
    if entry is None:
        return None
    fetched_at, detail = entry
    if now - fetched_at > DETAIL_TTL_SECONDS:
        del _cache[url]
        return None
    _cache.move_to_end(url)
    return detail


def _store(url: str, detail: dict[str, Any], now: float) -> None:
    _cache[url] = (now, detail)
    _cache.move_to_end(url)
    while len(_cache) > MAX_CACHE_ENTRIES:
        _cache.popitem(last=False)


async def fetch_details(
    client: httpx.AsyncClient,
    urls: list[str],
    extract: Callable[[str], dict[str, Any]],
    limit: int,
) -> dict[str, dict[str, Any]]:
    """{url: extracted detail} for `urls`, fetching at most `limit` of them."""
    now = time.monotonic()
    results: dict[str, dict[str, Any]] = {}
    to_fetch: list[str] = []
    for url in dict.fromkeys(urls):  # de-duplicate, keep order
        cached = _cached(url, now)
        if cached is not None:
            results[url] = cached
        elif len(to_fetch) < limit:
            to_fetch.append(url)

    semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def fetch(url: str) -> None:
        async with semaphore:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.debug("Detail fetch failed for %s: %s", url, exc)
                return
        try:
            detail = extract(resp.text)
        except Exception as exc:  # markup drift must not kill the search
            log.warning("Detail parse failed for %s: %s", url, exc)
            detail = {}
        _store(url, detail, time.monotonic())
        results[url] = detail

    await asyncio.gather(*(fetch(url) for url in to_fetch))
    return results
