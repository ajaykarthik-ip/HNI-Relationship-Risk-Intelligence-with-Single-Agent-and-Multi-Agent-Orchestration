"""Content-addressed response cache.

Re-running a subject should cost close to nothing. News moves hourly,
registry data moves monthly, and a Firecrawl scrape costs real money — so
the TTL is chosen per source class rather than set globally.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import threading
import time
import urllib.parse

# Seconds. Keyed by host fragment, longest match wins.
TTL_BY_HOST = {
    "wikidata.org": 7 * 24 * 3600,
    "wikipedia.org": 7 * 24 * 3600,
    "query.wikidata.org": 24 * 3600,
    "api.duckduckgo.com": 24 * 3600,
    # Paid calls: cache hard, so a re-run never spends credits twice.
    "api.firecrawl.dev": 7 * 24 * 3600,
    # News must stay fresh.
    "news.google.com": 3600,
    "bing.com": 3600,
}

DEFAULT_TTL = 6 * 3600


def ttl_for(url: str) -> int:
    host = urllib.parse.urlsplit(url).netloc.lower()
    best, best_len = DEFAULT_TTL, -1
    for fragment, ttl in TTL_BY_HOST.items():
        if fragment in host and len(fragment) > best_len:
            best, best_len = ttl, len(fragment)
    return best


class ResponseCache:
    """Disk cache. Keys are stable across runs; misses are silent."""

    def __init__(self, directory: str | None = ".cache", enabled: bool = True):
        self.enabled = enabled and directory is not None
        self.directory = pathlib.Path(directory) if directory else None
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        if self.enabled:
            self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(method: str, url: str, params=None, payload=None) -> str:
        blob = json.dumps(
            {"m": method, "u": url, "p": params or {}, "b": payload or {}},
            sort_keys=True, default=str,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> pathlib.Path:
        # Shard by first two characters: one flat directory of thousands of
        # files is slow to list on Windows.
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str, url: str):
        if not self.enabled:
            return None
        path = self._path(key)
        if not path.exists():
            with self._lock:
                self.misses += 1
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

        if time.time() - record.get("stored_at", 0) > ttl_for(url):
            with self._lock:
                self.misses += 1
            return None

        with self._lock:
            self.hits += 1
        return record.get("body")

    def set(self, key: str, body) -> None:
        if not self.enabled:
            return
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"stored_at": time.time(), "body": body}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass  # A cache that cannot write must not break the run.

    @property
    def summary(self) -> dict:
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }
