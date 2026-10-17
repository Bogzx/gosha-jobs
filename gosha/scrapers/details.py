"""Detail-page fetching for boards whose list endpoints carry no description.

BestJobs and Hipo list pages give a title and a company and nothing else,
so those postings used to be embedded as "title, title, at Company" —
which a semantic matcher can do little with. Adapters now fetch the detail
page for the postings a query returns, politely:

- at most `limit` network fetches per search (cached pages are free),
- DETAIL_CONCURRENCY requests in flight per board,
- a cache (DETAIL_TTL_SECONDS) keyed by URL: the scheduler re-runs every
  search each cycle and expands one keyword into several terms, so without
  it the same page would be fetched over and over. Extraction results are
  cached even when empty, so a page whose markup changed is not hammered
  until the TTL passes; network errors are not cached, so they are retried
  next cycle. It is written through to disk (page_store) and read back
  after a restart, so a deploy is not a cold start.

- an hourly budget of network fetches per host (HOST_BUDGET_PER_HOUR),
  kept on disk too, so a restart does not hand out a second one.
  Every keyword term is its own search with its own `limit`, and a
  restart (every deploy) empties the cache, so without it one cycle could
  fetch several hundred pages from one board in a few minutes. With it the
  cache fills over a couple of cycles instead. Callers can ask for the
  URLs left for a later cycle (`deferred`): a posting whose level only its
  page tells should wait for that page rather than be stored without it.

Failures never raise: a posting without a description is still a posting.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from gosha.scrapers import page_store
from gosha.scrapers.base import strip_invisible

log = logging.getLogger(__name__)

DETAIL_TTL_SECONDS = 24 * 3600
DETAIL_CONCURRENCY = 3
MAX_CACHE_ENTRIES = 5000
MAX_DESCRIPTION_CHARS = 20000
HOST_BUDGET_PER_HOUR = 250

_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
# host -> (start of the current hour window, fetches made in it)
_budget: dict[str, tuple[float, int]] = {}

_BREAK_RE = re.compile(r"</p>|</div>|</li>|</h\d>|<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
# Elements whose content is code, not text (an ad's own stylesheet).
_CODE_RE = re.compile(r"<(style|script)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
# After unescaping, only what is plainly a tag: "R&amp;D &lt;3" stays "R&D <3".
_DECODED_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?>")
_ENTITY_RE = re.compile(r"&(?:[a-zA-Z][a-zA-Z0-9]{1,31}|#\d{1,7}|#[xX][0-9a-fA-F]{1,6});")


def html_to_text(markup: str | None) -> str:
    """Readable plain text from an HTML fragment, paragraph breaks kept.

    Hipo's JSON-LD escapes its text twice ("Quality &amp;amp; Compliance",
    "&amp;nbsp;"), and some boards ship escaped markup ("&lt;p&gt;"): up to
    two more unescapes while entities remain, then any tags that decoded.
    Invisible characters are dropped (base.strip_invisible).
    """
    text = _BREAK_RE.sub("\n", _CODE_RE.sub(" ", markup or ""))
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    for _ in range(2):
        if not _ENTITY_RE.search(text):
            break
        text = html_lib.unescape(text)
    if _DECODED_TAG_RE.search(text):
        text = _DECODED_TAG_RE.sub(" ", _BREAK_RE.sub("\n", _CODE_RE.sub(" ", text)))
    # A raw tab or carriage return inside a JSON string (Hipo, Bosch) is
    # whitespace, not something to store or show in a DM.
    text = strip_invisible(text).replace("\xa0", " ").replace("\r\n", "\n")
    text = text.replace("\r", "\n").replace("\t", " ")
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


_PAGES = "detail:"  # + the parser's code version, see _pages_namespace
_BUDGETS = "budget:detail"
PRUNE_EVERY_WRITES = 500
_writes = 0


def _pages_namespace(extract: Callable[[str], dict[str, Any]]) -> str:
    """Pages parsed by this extractor, as long as its code and ours are unchanged."""
    module = getattr(extract, "__module__", None) or "unknown"
    name = getattr(extract, "__qualname__", None) or "extract"
    version = page_store.code_version(module, __name__, "gosha.scrapers.base")
    return f"{_PAGES}{module}.{name}:{version}"


def clear_cache() -> None:
    _cache.clear()
    _budget.clear()
    store = page_store.store()
    store.clear(_PAGES)
    store.clear(_BUDGETS)


def _take_budget(url: str, now: float) -> bool:
    """Spend one network fetch of `url`'s host budget; False when spent."""
    host = urlsplit(url).netloc
    if host not in _budget:
        stored = page_store.store().get(_BUDGETS, host)
        if stored is not None:
            start, value = stored
            _budget[host] = (start, int(value.get("used", 0)))
    start, used = _budget.get(host, (now, 0))
    if now - start >= 3600:
        start, used = now, 0
    if used >= HOST_BUDGET_PER_HOUR:
        return False
    _budget[host] = (start, used + 1)
    page_store.store().put(_BUDGETS, host, {"used": used + 1}, start)
    return True


def _cached(url: str, now: float, namespace: str) -> dict[str, Any] | None:
    entry = _cache.get(url)
    if entry is None:
        entry = page_store.store().get(namespace, url)  # fetched before a restart
        if entry is None:
            return None
        _cache[url] = entry
    fetched_at, detail = entry
    if now - fetched_at > DETAIL_TTL_SECONDS:
        del _cache[url]
        page_store.store().delete(namespace, url)
        return None
    _cache.move_to_end(url)
    return detail


def _store(url: str, detail: dict[str, Any], now: float, namespace: str) -> None:
    _cache[url] = (now, detail)
    _cache.move_to_end(url)
    while len(_cache) > MAX_CACHE_ENTRIES:
        _cache.popitem(last=False)
    global _writes
    store = page_store.store()
    store.put(namespace, url, detail, now)
    _writes += 1
    if _writes % PRUNE_EVERY_WRITES == 0:  # now and then, not on every page
        store.prune(_PAGES, now - DETAIL_TTL_SECONDS, MAX_CACHE_ENTRIES)


async def fetch_details(
    client: httpx.AsyncClient,
    urls: list[str],
    extract: Callable[[str], dict[str, Any]],
    limit: int,
    deferred: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """{url: extracted detail} for `urls`, fetching at most `limit` of them.

    `deferred`, when given, receives the URLs neither cached nor fetched
    because `limit` or the host budget was spent. A fetch that fails is not
    deferred: it is retried next cycle, but its posting is not held back.
    """
    now = page_store.now()
    namespace = _pages_namespace(extract)
    results: dict[str, dict[str, Any]] = {}
    to_fetch: list[str] = []
    for url in dict.fromkeys(urls):  # de-duplicate, keep order
        cached = _cached(url, now, namespace)
        if cached is not None:
            results[url] = cached
        elif len(to_fetch) < limit and _take_budget(url, now):
            to_fetch.append(url)
        elif deferred is not None:
            deferred.add(url)

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
        _store(url, detail, page_store.now(), namespace)
        results[url] = detail

    await asyncio.gather(*(fetch(url) for url in to_fetch))
    return results
