"""Firecrawl, concurrently and with a timeout that matches the work.

Two V1 behaviours are fixed here, and the second one is the larger win in the
whole engine.

**The version probe is serialised once.** `Firecrawl._call` tries v2 then v1 and
memoises the answer on the instance. Fired concurrently against a cold client,
four scrapes would each probe independently -- four wasted paid calls before any
work happened. Here the first call holds a lock while it settles the version;
everything after it goes straight through, in parallel.

**Structured scrapes get the time they need.** A `json` scrape runs a model over
the page and routinely takes longer than V1's 25-second default. `RETRY_STATUS`
does not cover timeouts, so under V1 a slow page raises, retries three times and
burns 75 seconds to return nothing -- while the credit is still metered on
dispatch. Raising the timeout is therefore *faster*, not slower: a 40-second
page now costs 40 seconds and returns.

Payloads, schema, prompt, ranking and the response shape are V1's, imported, so
what a scrape means is identical between the engines.
"""

from __future__ import annotations

import asyncio

from affluense import config as v1config
from affluense.sources.firecrawl import (PROFILE_PROMPT, PROFILE_SCHEMA,
                                         host_of, rank_for_scraping)

from .. import config as v2config
from ..concurrency import map_bounded

__all__ = ["AsyncFirecrawl", "rank_for_scraping"]


class AsyncFirecrawl:
    """Async client over V2's transport. One instance per run."""

    def __init__(self, api_key: str, transport):
        self.transport = transport
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.version: str | None = None
        self._probe = asyncio.Lock()

    async def _call(self, path: str, payload_by_version: dict,
                    kind: str | None = None, units: int = 1,
                    timeout: float | None = None):
        if self.version is not None:
            return await self._post(self.version, path, payload_by_version,
                                    kind, units, timeout)

        # First call settles the API version for the run. Held under a lock so
        # a concurrent fan-out probes once rather than once per task.
        async with self._probe:
            if self.version is not None:
                return await self._post(self.version, path, payload_by_version,
                                        kind, units, timeout)
            for version in ("v2", "v1"):
                data = await self._post(version, path, payload_by_version,
                                        kind, units, timeout)
                if data is not None:
                    self.version = version
                    return data
            return None

    async def _post(self, version: str, path: str, payload_by_version: dict,
                    kind: str | None, units: int, timeout: float | None):
        url = f"{v1config.FIRECRAWL_BASE}/{version}/{path}"
        return await self.transport.post_json(
            url, payload_by_version[version], self.headers,
            kind=kind, units=units, timeout=timeout,
        )

    # -- search -------------------------------------------------------------
    async def search(self, query: str, limit: int = 8) -> list:
        payload = {"query": query, "limit": limit}
        # Search is billed per result returned, and `limit` is the most the
        # call can return, so this is the ceiling on what it can cost.
        data = await self._call(
            "search", {"v2": payload, "v1": payload},
            kind="firecrawl.search", units=limit,
            timeout=v2config.FIRECRAWL_SEARCH_TIMEOUT,
        )
        if not data:
            return []

        raw = data.get("data")
        # v1 returns a flat list; v2 groups results by category.
        if isinstance(raw, dict):
            results = raw.get("web") or raw.get("results") or []
        elif isinstance(raw, list):
            results = raw
        else:
            results = []

        return [
            {
                "title": item.get("title"),
                "snippet": item.get("description") or item.get("snippet"),
                "query": query,
                "source": "Firecrawl search",
                "source_url": item["url"],
            }
            for item in results
            if isinstance(item, dict) and item.get("url")
        ]

    async def search_many(self, queries: list, limit: int = 8,
                          say=None) -> list:
        """Every search angle at once, results in query order."""
        async def one(query: str) -> list:
            if say:
                say(f"    search: {query[:58]}")
            return await self.search(query, limit=limit)

        # The version probe means the first call cannot overlap the rest, which
        # is exactly what we want: it settles the version, then the remainder
        # run together.
        results = await map_bounded(
            queries, one, v2config.MAX_FIRECRAWL_CONCURRENCY,
            on_error=lambda i, e: self.transport.note(
                f"Firecrawl search failed for {queries[i][:48]}: {e}"
            ),
        )
        flattened: list = []
        for found in results:
            flattened.extend(found or [])
        return flattened

    # -- scrape -------------------------------------------------------------
    async def scrape(self, url: str, name: str,
                     structured: bool = False) -> dict | None:
        prompt = PROFILE_PROMPT.format(name=name)
        if structured:
            payload_by_version = {
                "v2": {
                    "url": url, "onlyMainContent": True,
                    "formats": [
                        "markdown",
                        {"type": "json", "schema": PROFILE_SCHEMA,
                         "prompt": prompt},
                    ],
                },
                "v1": {
                    "url": url, "onlyMainContent": True,
                    "formats": ["markdown", "json"],
                    "jsonOptions": {"schema": PROFILE_SCHEMA, "prompt": prompt},
                },
            }
        else:
            base = {"url": url, "onlyMainContent": True,
                    "formats": ["markdown"]}
            payload_by_version = {"v2": base, "v1": base}

        data = await self._call(
            "scrape", payload_by_version,
            kind="firecrawl.scrape.json" if structured else "firecrawl.scrape",
            timeout=v2config.FIRECRAWL_SCRAPE_TIMEOUT,
        )
        if not data:
            return None

        body = data.get("data") or {}
        markdown = body.get("markdown") or ""
        metadata = body.get("metadata") or {}
        return {
            "title": metadata.get("title"),
            "markdown": markdown[:20000],
            "markdown_truncated": len(markdown) > 20000,
            "extracted": body.get("json") or body.get("extract") or None,
            "source": host_of(url),
            "source_url": metadata.get("sourceURL") or url,
        }

    async def scrape_many(self, items: list, name: str, structured: bool,
                          say=None) -> list:
        """Scrape a ranked list concurrently; pages come back in rank order.

        Rank order matters downstream: `_split_roles` walks the pages in
        sequence, so letting completion order decide would change which page's
        wording won a conflict.
        """
        async def one(item: dict):
            if say:
                say(f"    read:   {item['source_url'][:62]}")
            page = await self.scrape(item["source_url"], name,
                                     structured=structured)
            if page:
                page["found_via"] = item.get("query")
            return page

        pages = await map_bounded(
            items, one, v2config.MAX_FIRECRAWL_CONCURRENCY,
            on_error=lambda i, e: self.transport.note(
                f"Firecrawl scrape failed for {items[i]['source_url']}: {e}"
            ),
        )
        return [p for p in pages if p]
