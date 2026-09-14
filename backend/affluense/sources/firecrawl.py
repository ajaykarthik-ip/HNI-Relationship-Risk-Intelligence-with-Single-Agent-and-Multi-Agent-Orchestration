"""Firecrawl: the one keyed source, and strictly additive.

It supplies what the free sources cannot: real web search, clean page text,
and schema-guided extraction. With no key set, none of this runs and the
pipeline still produces a full report.
"""

from __future__ import annotations

import urllib.parse

from .. import config

# Asks only for what the page states. The "do not infer" instruction is load
# bearing: an extraction model will otherwise fill these fields from its own
# memory of a well-known person, and that invented content would arrive
# wearing a real source URL.
PROFILE_PROMPT = (
    "Extract only facts about {name} that this page explicitly states. "
    "If the page does not state something, omit that field entirely. "
    "Do not infer, guess, or use outside knowledge. If the page is not about "
    "{name}, return an empty object."
)

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "full_name": {"type": "string"},
        "current_roles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "role": {"type": "string"},
                    "period": {"type": "string"},
                    "sector": {
                        "type": "string",
                        "description": (
                            "The industry this company operates in, in two or "
                            "three words, e.g. 'restaurant chain', 'fitness', "
                            "'football club', 'payments'. Only if stated."
                        ),
                    },
                },
                "required": ["company"],
            },
        },
        "past_roles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "role": {"type": "string"},
                    "period": {"type": "string"},
                    "sector": {
                        "type": "string",
                        "description": (
                            "The industry this company operates in, in two or "
                            "three words, e.g. 'restaurant chain', 'fitness', "
                            "'football club', 'payments'. Only if stated."
                        ),
                    },
                },
                "required": ["company"],
            },
        },
        "investments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "stake_or_amount": {"type": "string"},
                    "year": {"type": "string"},
                    "sector": {"type": "string"},
                },
                "required": ["entity"],
            },
        },
        "associates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "company": {"type": "string"},
                    "relationship": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        "net_worth": {"type": "string"},
        "education": {"type": "array", "items": {"type": "string"}},
        "notable_events": {"type": "array", "items": {"type": "string"}},
    },
}


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")


def is_skipped(url: str) -> bool:
    host = host_of(url)
    return any(host == d or host.endswith("." + d) for d in config.SKIP_DOMAINS)


def scrape_priority(url: str) -> tuple:
    """Lower sorts first. Registry and business sources outrank general pages."""
    host = host_of(url)
    for index, domain in enumerate(config.PREFERRED_DOMAINS):
        if host == domain or host.endswith("." + domain):
            return (0, index)
    return (1, 0)


def rank_for_scraping(results: list) -> tuple:
    """Drop login-walled domains, then put the pages worth paying for first.

    Measured on a real run: 15 of 31 search results were walled, and three of
    the first six were Instagram. Skipping them doubled useful page reads at
    identical cost.
    """
    keep, skipped = [], []
    for item in results:
        (skipped if is_skipped(item["source_url"]) else keep).append(item)
    # Stable sort: within a tier the search engine's own ordering survives.
    return sorted(keep, key=lambda i: scrape_priority(i["source_url"])), skipped


class Firecrawl:
    """Thin client over Firecrawl's search and scrape endpoints.

    Firecrawl moved from v1 to v2 and changed both request and response
    shapes. Rather than pin a version, try v2 and fall back to v1 on the
    first call, then remember which answered.
    """

    def __init__(self, api_key: str, fetcher):
        self.fetcher = fetcher
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.version: str | None = None

    def _call(self, path: str, payload_by_version: dict,
              kind: str | None = None, units: int = 1):
        versions = [self.version] if self.version else ["v2", "v1"]
        for version in versions:
            url = f"{config.FIRECRAWL_BASE}/{version}/{path}"
            data = self.fetcher.post_json(
                url, payload_by_version[version], self.headers,
                kind=kind, units=units,
            )
            if data is not None:
                self.version = version
                return data
        return None

    def search(self, query: str, limit: int = 8) -> list:
        payload = {"query": query, "limit": limit}
        # Search is billed per result returned, and `limit` is the most the
        # call can return, so this is the ceiling on what it can cost.
        data = self._call("search", {"v2": payload, "v1": payload},
                          kind="firecrawl.search", units=limit)
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

    def scrape(self, url: str, name: str, structured: bool = False) -> dict | None:
        prompt = PROFILE_PROMPT.format(name=name)
        if structured:
            payload_by_version = {
                "v2": {
                    "url": url, "onlyMainContent": True,
                    "formats": [
                        "markdown",
                        {"type": "json", "schema": PROFILE_SCHEMA, "prompt": prompt},
                    ],
                },
                "v1": {
                    "url": url, "onlyMainContent": True,
                    "formats": ["markdown", "json"],
                    "jsonOptions": {"schema": PROFILE_SCHEMA, "prompt": prompt},
                },
            }
        else:
            base = {"url": url, "onlyMainContent": True, "formats": ["markdown"]}
            payload_by_version = {"v2": base, "v1": base}

        data = self._call(
            "scrape", payload_by_version,
            kind="firecrawl.scrape.json" if structured else "firecrawl.scrape",
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
