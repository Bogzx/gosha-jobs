"""Keyword (TF-IDF) similarity between a CV and the candidate postings.

The offline eval (docs/EVAL.md) found that plain sublinear TF-IDF beat
the sentence embeddings on their own: a CV that says "FastAPI,
PostgreSQL, Celery" is best matched by postings that say those words,
and an embedding of 1,500 characters smears them into the company
boilerplate. The two make different mistakes, so the feed ranks on both
(gosha/recommend.py), and the eval (gosha/evaluation.py) scores this
module, not a copy of it.

The IDF is fitted on the postings being ranked: a term every posting
carries ("experience", "team") weighs nothing, one that few carry
("kotlin", "contabilitate") weighs a lot.

Tokenising ~2,000 descriptions is the expensive part, so term counts are
cached per key for the life of the process. The caller picks the key and
must change it when the text changes (recommend.py keys on the job id
and its embedding, which the pipeline recomputes whenever the title,
company or description changes).
"""

from __future__ import annotations

import math
import re
import threading
import unicodedata
from collections import Counter, OrderedDict
from collections.abc import Hashable, Sequence

import numpy as np

_TOKEN = re.compile(r"[^\W\d_][\w+#.]*", re.UNICODE)

# Bounded so a long-running process does not keep every posting it ever
# ranked; the feed window holds a few thousand.
MAX_CACHED = 20_000
_counts: dict[Hashable, Counter] = {}
_lock = threading.Lock()


def _fold(text: str) -> str:
    # Romanian postings are written with and without diacritics, and with
    # both the comma-below (ș, ț) and the cedilla (ş, ţ) forms: one term.
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def terms(text: str | None) -> list[str]:
    """Lowercased, diacritic-free terms; "c++", "c#", "node.js" and ".net"
    survive whole."""
    return [
        t.rstrip(".").lower()
        for t in _TOKEN.findall(_fold(text or ""))
        if len(t.rstrip(".")) > 1
    ]


def is_cached(key: Hashable) -> bool:
    with _lock:
        return key in _counts


def _cached_counts(key: Hashable | None, text: str | None) -> Counter:
    """Term counts for one posting; `text` may be None when `key` is cached."""
    if key is None:
        return Counter(terms(text))
    with _lock:
        hit = _counts.get(key)
    if hit is not None:
        return hit
    if text is None:
        # Evicted between the caller's check and now: rank this one on its
        # embedding alone for this request rather than cache it as empty.
        return Counter()
    counts = Counter(terms(text))
    with _lock:
        if len(_counts) >= MAX_CACHED:
            _counts.clear()  # crude, but rare: the window is a fraction of this
        _counts[key] = counts
    return counts


def clear_cache() -> None:
    with _lock:
        _counts.clear()
        _indexes.clear()


class LexicalIndex:
    """Sublinear TF-IDF over a fixed set of postings, as an inverted index.

    Building it is the costly step (one pass over every posting's terms);
    scoring a CV against it touches only the postings that share a term
    with the CV. The feed builds one per candidate set and reuses it for
    every user ranked against the same set (see `index_for`).
    """

    def __init__(
        self,
        docs: Sequence[str | None],
        keys: Sequence[Hashable | None] | None = None,
    ):
        keys = list(keys) if keys is not None else [None] * len(docs)
        if len(keys) != len(docs):
            raise ValueError("one key per document")
        doc_counts = [_cached_counts(k, d) for k, d in zip(keys, docs)]
        self.size = len(doc_counts)
        self._position = {k: i for i, k in enumerate(keys) if k is not None}
        df: Counter = Counter()
        for counts in doc_counts:
            df.update(counts.keys())
        self.idf = {t: math.log((1 + self.size) / (1 + n)) + 1 for t, n in df.items()}
        # term -> (doc indices, unit-normalised weights)
        postings: dict[str, tuple[list[int], list[float]]] = {}
        for i, counts in enumerate(doc_counts):
            weights = {t: (1 + math.log(c)) * self.idf[t] for t, c in counts.items()}
            norm = math.sqrt(sum(w * w for w in weights.values()))
            if not norm:
                continue
            for t, w in weights.items():
                idx, ws = postings.setdefault(t, ([], []))
                idx.append(i)
                ws.append(w / norm)
        self._postings = {
            t: (np.asarray(idx, dtype=np.int64), np.asarray(ws, dtype=np.float32))
            for t, (idx, ws) in postings.items()
        }

    def score(self, query: str) -> np.ndarray:
        """Cosine of `query` against every posting, in input order."""
        out = np.zeros(self.size, dtype=np.float32)
        counts = Counter(terms(query))
        # Terms no posting has cannot score; they would only inflate the
        # query norm, which scales every score equally anyway.
        q = {t: (1 + math.log(c)) * self.idf[t] for t, c in counts.items() if t in self.idf}
        q_norm = math.sqrt(sum(w * w for w in q.values()))
        if not q_norm:
            return out
        for t, w in q.items():
            idx, ws = self._postings.get(t, (None, None))
            if idx is not None:  # a term only empty-normed postings had
                out[idx] += ws * (w / q_norm)
        return out

    def covers(self, keys: Sequence[Hashable]) -> bool:
        return all(k in self._position for k in keys)

    def score_keys(self, query: str, keys: Sequence[Hashable]) -> np.ndarray:
        """Like score(), for the postings with these keys, in that order."""
        full = self.score(query)
        return full[[self._position[k] for k in keys]] if keys else full[:0]


# Recently built indexes, most recent last. More than one: the public demo
# ranks every candidate while each signed-in user ranks the candidates
# minus the postings they rated, and users with excluded words or
# blacklisted companies see smaller sets still. With a single slot those
# requests evicted each other and every one paid a full rebuild.
MAX_INDEXES = 4
_indexes: OrderedDict[int, LexicalIndex] = OrderedDict()
# Reuse an index built over a superset of the requested postings while the
# extra ones are a small share of it. The IDF moves negligibly with a few
# extra documents; rebuilding per user would cost a full pass each time.
REUSE_SLACK = 0.25


def index_for(docs: Sequence[str | None], keys: Sequence[Hashable]) -> LexicalIndex:
    """An index covering these postings, reused across feed requests.

    The candidate set changes once per scrape cycle; between cycles every
    request asks for nearly the same one. Score through
    `LexicalIndex.score_keys(query, keys)`. `docs[i]` may be None when
    `keys[i]` is already cached (see is_cached).
    """
    with _lock:
        cached = list(_indexes.items())
    best = None
    for ident, index in cached:
        if index.size <= len(keys) * (1 + REUSE_SLACK) and index.covers(keys):
            if best is None or index.size < best[1].size:
                best = (ident, index)
    if best is not None:
        with _lock:
            if best[0] in _indexes:
                _indexes.move_to_end(best[0])
        return best[1]
    index = LexicalIndex(docs, keys)
    with _lock:
        _indexes[id(index)] = index
        while len(_indexes) > MAX_INDEXES:
            _indexes.popitem(last=False)
    return index


def scores(query: str, docs: Sequence[str | None]) -> np.ndarray:
    """One-off: `query` against `docs`, nothing cached."""
    return LexicalIndex(docs).score(query)
