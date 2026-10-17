"""Detail pages that survive a restart: a small SQLite file next to the data.

The detail-page caches (details.py for BestJobs/Hipo/eJobs, the LinkedIn
job-page cache in gosha/scraper.py) lived in memory, so every deploy was a
cold start. On 2026-10-16 two deploys an hour apart emptied them; the next
cycles refetched everything they could within the per-search caps and
stored the rest without a page (36 of 40 new BestJobs rows had no
description), and LinkedIn answered the extra job pages with 429s.

Entries are written through to `data/scrape_cache.sqlite3` and read back
after a restart, keyed by (namespace, key), stamped with wall-clock time so
a TTL means the same before and after a restart. A namespace carries the
version of the code that produced its entries (code_version), so a page
parsed by an older parser is not served after a deploy changes it. `data/` is the volume the
bot already keeps across deploys (docker-compose.prod.yml). Where it does
not exist (a checkout, CI, tests) nothing is written and the caches work
in memory as before. SCRAPE_CACHE_PATH names another file, or "off".

Values are JSON. The LinkedIn pages carry jobspy objects (a JobType enum,
a Compensation model); those are stored tagged and rebuilt only from
jobspy's own model module, never from an arbitrary import path, and
nothing is unpickled. A store that fails (disk full, locked, corrupt) logs
once and turns itself off: a cache must never cost a scrape.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
FILE_NAME = "scrape_cache.sqlite3"
# Only these modules' enums and models are rebuilt from stored values.
_REBUILDABLE_MODULES = ("jobspy.model",)

_OFF = {"", "0", "off", "false", "no", "none"}


def default_path() -> Path | None:
    """SCRAPE_CACHE_PATH, else data/scrape_cache.sqlite3 when data/ exists."""
    configured = os.getenv("SCRAPE_CACHE_PATH")
    if configured is not None:
        return None if configured.strip().lower() in _OFF else Path(configured)
    return DATA_DIR / FILE_NAME if DATA_DIR.is_dir() else None


def _encode(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, Enum):
        cls = type(value)
        return {"$enum": f"{cls.__module__}:{cls.__qualname__}", "name": value.name}
    if hasattr(value, "model_dump") and hasattr(type(value), "model_validate"):
        cls = type(value)
        return {"$model": f"{cls.__module__}:{cls.__qualname__}",
                "data": value.model_dump(mode="json")}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"cannot store {type(value).__name__}")


def _rebuildable(path: str) -> type:
    module_name, _, name = path.partition(":")
    if module_name not in _REBUILDABLE_MODULES or not name.isidentifier():
        raise ValueError(f"not rebuilt: {path}")
    import importlib

    return getattr(importlib.import_module(module_name), name)


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"$dt"}:
        return datetime.fromisoformat(value["$dt"])
    if set(value) == {"$enum", "name"}:
        return _rebuildable(value["$enum"])[value["name"]]
    if set(value) == {"$model", "data"}:
        return _rebuildable(value["$model"]).model_validate(value["data"])
    return {k: _decode(v) for k, v in value.items()}


class PageStore:
    """(namespace, key) -> (fetched_at, dict), in one SQLite file.

    Thread-safe: the LinkedIn cache is called from JobSpy's worker threads.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._failed = path is None

    @property
    def enabled(self) -> bool:
        return not self._failed

    def _connection(self) -> sqlite3.Connection | None:
        if self._failed:
            return None
        if self._conn is None:
            assert self.path is not None
            conn = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pages (namespace TEXT NOT NULL, key TEXT NOT NULL,"
                " fetched_at REAL NOT NULL, value TEXT NOT NULL, PRIMARY KEY (namespace, key))"
            )
            conn.commit()
            self._conn = conn
        return self._conn

    def _run(self, what: str, fn):
        with self._lock:
            try:
                conn = self._connection()
                return None if conn is None else fn(conn)
            except (sqlite3.Error, OSError) as exc:
                log.warning("Scrape cache %s failed (%s); caching in memory only", what, exc)
                self._failed = True
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except sqlite3.Error:
                        pass
                    self._conn = None
                return None

    def get(self, namespace: str, key: str) -> tuple[float, dict] | None:
        """(fetched_at as time.time(), value), however old; None if absent."""
        def read(conn: sqlite3.Connection):
            return conn.execute(
                "SELECT fetched_at, value FROM pages WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()

        row = self._run("read", read)
        if row is None:
            return None
        try:
            return row[0], _decode(json.loads(row[1]))
        except (ValueError, KeyError, TypeError, AttributeError, ImportError) as exc:
            # Written by another version (a renamed jobspy enum): a miss.
            log.debug("Scrape cache entry %s/%s unreadable: %s", namespace, key, exc)
            return None

    def put(self, namespace: str, key: str, value: dict, fetched_at: float) -> None:
        try:
            encoded = json.dumps(_encode(value))
        except (TypeError, ValueError) as exc:
            log.debug("Scrape cache entry %s/%s not stored: %s", namespace, key, exc)
            return

        def write(conn: sqlite3.Connection):
            conn.execute(
                "INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?)",
                (namespace, key, fetched_at, encoded),
            )
            conn.commit()

        self._run("write", write)

    def delete(self, namespace: str, key: str) -> None:
        def remove(conn: sqlite3.Connection):
            conn.execute("DELETE FROM pages WHERE namespace = ? AND key = ?", (namespace, key))
            conn.commit()

        self._run("delete", remove)

    def prune(self, prefix: str, older_than: float, keep: int) -> None:
        """In the namespaces starting with `prefix`: drop entries fetched
        before `older_than`, then all but the newest `keep`."""
        def prune_rows(conn: sqlite3.Connection):
            n = len(prefix)
            conn.execute(
                "DELETE FROM pages WHERE substr(namespace, 1, ?) = ? AND fetched_at < ?",
                (n, prefix, older_than),
            )
            conn.execute(
                "DELETE FROM pages WHERE substr(namespace, 1, ?) = ? AND rowid NOT IN ("
                "SELECT rowid FROM pages WHERE substr(namespace, 1, ?) = ?"
                " ORDER BY fetched_at DESC LIMIT ?)",
                (n, prefix, n, prefix, keep),
            )
            conn.commit()

        self._run("prune", prune_rows)

    def clear(self, prefix: str | None = None) -> None:
        """Everything, or the namespaces starting with `prefix`."""
        def wipe(conn: sqlite3.Connection):
            if prefix is None:
                conn.execute("DELETE FROM pages")
            else:
                conn.execute(
                    "DELETE FROM pages WHERE substr(namespace, 1, ?) = ?", (len(prefix), prefix),
                )
            conn.commit()

        self._run("clear", wipe)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


_store: PageStore | None = None


def store() -> PageStore:
    """The process-wide store, opened on first use at default_path()."""
    global _store
    if _store is None:
        _store = PageStore(default_path())
        if _store.enabled:
            log.info("Detail-page cache persisted at %s", _store.path)
    return _store


def use(new: PageStore | None) -> None:
    """Replace the process-wide store (tests; None reopens the default)."""
    global _store
    if _store is not None and _store is not new:
        _store.close()
    _store = new


def now() -> float:
    return time.time()


_versions: dict[tuple[str, ...], str] = {}


def code_version(*modules: str) -> str:
    """A short hash of these modules' source files.

    Part of a namespace, so that what a parser extracted is kept across
    restarts only while that parser is unchanged: a deploy that fixes a
    parser (Hipo's JSON-LD, 2026-10-17) re-reads the pages, and one that
    changes something else keeps them.
    """
    key = tuple(modules)
    if key not in _versions:
        digest = hashlib.sha256()
        for name in modules:
            module = sys.modules.get(name)
            source = getattr(module, "__file__", None)
            try:
                digest.update(Path(source).read_bytes() if source else name.encode())
            except OSError:
                digest.update(name.encode())
        _versions[key] = digest.hexdigest()[:12]
    return _versions[key]
