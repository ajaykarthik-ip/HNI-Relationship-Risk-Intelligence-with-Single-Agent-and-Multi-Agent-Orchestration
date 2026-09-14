"""Disk cache for V2: V1's keys and TTLs, atomic writes, off the event loop.

Deliberately the *same* directory and the *same* key derivation as V1, so a V2
run can reuse a V1 run's Firecrawl scrapes instead of paying for them twice.
That sharing is also why `bench.py` gives each engine its own cold directory --
otherwise whichever engine ran second would win for free.

Two things differ from `affluense.cache.ResponseCache`, and neither changes what
is stored:

  atomic writes   V1 calls `path.write_text`, which is fine at one request per
                  second and not fine with twelve coroutines in flight. A
                  half-written file is read back as invalid JSON, which V1
                  already survives (it counts a miss) -- but writing through a
                  temporary file and renaming means it never happens.

  off the loop    File I/O is blocking, so it runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import tempfile
import threading
import time

from affluense.cache import ResponseCache, ttl_for


class AsyncCache:
    """Async wrapper over V1's cache layout.

    Key derivation, sharding and TTLs are V1's, imported rather than restated,
    so the two engines can never drift into disagreeing about what a cache hit
    is.
    """

    def __init__(self, directory: str | None = ".cache", enabled: bool = True):
        self._inner = ResponseCache(directory, enabled=enabled)
        self._lock = threading.Lock()

    # -- delegated surface, so anything expecting a V1 cache still works ----
    @property
    def enabled(self) -> bool:
        return self._inner.enabled

    @property
    def directory(self) -> pathlib.Path | None:
        return self._inner.directory

    @property
    def hits(self) -> int:
        return self._inner.hits

    @property
    def misses(self) -> int:
        return self._inner.misses

    @property
    def summary(self) -> dict:
        return self._inner.summary

    @staticmethod
    def key(method: str, url: str, params=None, payload=None) -> str:
        return ResponseCache.key(method, url, params, payload)

    # -- async I/O ----------------------------------------------------------
    async def get(self, key: str, url: str):
        if not self._inner.enabled:
            return None
        return await asyncio.to_thread(self._get_blocking, key, url)

    async def set(self, key: str, body) -> None:
        if not self._inner.enabled:
            return
        await asyncio.to_thread(self._set_blocking, key, body)

    # -- blocking implementations, run in a worker thread -------------------
    def _get_blocking(self, key: str, url: str):
        path = self._inner._path(key)
        if not path.exists():
            with self._lock:
                self._inner.misses += 1
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A torn or corrupt entry is a miss, never an error. V1 behaves the
            # same way; atomic writes below mean V2 should not produce them.
            with self._lock:
                self._inner.misses += 1
            return None

        if time.time() - record.get("stored_at", 0) > ttl_for(url):
            with self._lock:
                self._inner.misses += 1
            return None

        with self._lock:
            self._inner.hits += 1
        return record.get("body")

    def _set_blocking(self, key: str, body) -> None:
        path = self._inner._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"stored_at": time.time(), "body": body}, ensure_ascii=False
            )
            # Write beside the target, then rename. os.replace is atomic on
            # POSIX and on Windows, so a reader sees either the old entry or
            # the new one and never a partial file.
            handle, tmp = tempfile.mkstemp(
                dir=str(path.parent), prefix=".tmp-", suffix=".json"
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            pass  # A cache that cannot write must not break the run.
