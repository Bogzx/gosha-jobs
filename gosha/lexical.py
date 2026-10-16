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
cached per key for the life of the process (least recently used first
out). The caller picks the key and must change it when the text changes:
recommend.py keys on the job id plus a hash of its embedding, which the
pipeline drops and recomputes whenever the title, company or description
changes.
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
# ranked; the feed window holds a few thousand. Least recently used out,
# so one index build never evicts the counts it is reading: everything it
# touched is newer than everything it would push out.
MAX_CACHED = 20_000
_counts: OrderedDict[Hashable, Counter] = OrderedDict()
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


def _cached_counts(key: Hashable | None, text: str | None) -> Counter | None:
    """Term counts for one posting.

    `text` None means "not loaded, use the cache" (see index_for); the
    result is then None when the key is not cached after all (evicted
    since the caller checked), rather than an empty Counter that would
    pass for a posting with no words.
    """
    if key is None:
        return Counter(terms(text))
    with _lock:
        hit = _counts.get(key)
        if hit is not None:
            _counts.move_to_end(key)
            return hit
    if text is None:
        return None
    counts = Counter(terms(text))
    with _lock:
        _counts[key] = counts
        _counts.move_to_end(key)
        while len(_counts) > MAX_CACHED:
            _counts.popitem(last=False)
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
        found = [_cached_counts(k, d) for k, d in zip(keys, docs)]
        # A posting whose text was neither given nor cached scores 0 here.
        # Fine for one request; index_for() does not keep such an index.
        self.complete = all(c is not None for c in found)
        doc_counts = [c if c is not None else Counter() for c in found]
        self.size = len(doc_counts)
        self.keys = frozenset(k for k in keys if k is not None)
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

    def score_keys(self, query: str, keys: Sequence[Hashable]) -> np.ndarray:
        """Like score(), for the postings with these keys, in that order."""
        full = self.score(query)
        return full[[self._position[k] for k in keys]] if keys else full[:0]


# Recently built indexes by their exact key set, most recent last. The
# feed builds one over the whole candidate window, the same set the public
# demo ranks, and scores each user's subset of it (score_keys), so every
# request between two scrape cycles shares one index and one IDF: a
# user's ranking does not depend on which other indexes happen to be
# cached. More than one slot for the moment a scrape cycle changes the
# window while requests for the old one are still in flight.
MAX_INDEXES = 4
_indexes: OrderedDict[frozenset, LexicalIndex] = OrderedDict()


def index_for(docs: Sequence[str | None], keys: Sequence[Hashable]) -> LexicalIndex:
    """The index over exactly these postings, reused across feed requests.

    `docs[i]` may be None when `keys[i]` is already cached (see is_cached).
    Score a subset through `LexicalIndex.score_keys(query, subset_keys)`.
    """
    wanted = frozenset(keys)
    with _lock:
        index = _indexes.get(wanted)
        if index is not None:
            _indexes.move_to_end(wanted)
            return index
    index = LexicalIndex(docs, keys)
    if index.complete:
        with _lock:
            _indexes[wanted] = index
            while len(_indexes) > MAX_INDEXES:
                _indexes.popitem(last=False)
    return index


def scores(query: str, docs: Sequence[str | None]) -> np.ndarray:
    """One-off: `query` against `docs`, nothing cached."""
    return LexicalIndex(docs).score(query)
