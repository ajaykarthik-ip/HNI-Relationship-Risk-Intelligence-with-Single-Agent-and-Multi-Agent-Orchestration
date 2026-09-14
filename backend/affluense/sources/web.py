"""Small keyless sources: DuckDuckGo's Instant Answer API.

OpenCorporates was removed: unauthenticated access returns 401, so every
call was a wasted request that produced a note and no records.
"""

from __future__ import annotations

import urllib.parse

from .. import config


def duckduckgo_instant(fetcher, name: str) -> dict:
    """The documented Instant Answer API, not the HTML results page (which
    DuckDuckGo's robots.txt disallows)."""
    params = {"q": name, "format": "json", "no_html": 1, "skip_disambig": 1}
    data = fetcher.get_json(config.DUCKDUCKGO_IA, params=params)
    if not data:
        return {}

    query_url = f"{config.DUCKDUCKGO_IA}?{urllib.parse.urlencode(params)}"
    related = [
        {"value": topic["Text"], "source": "DuckDuckGo",
         "source_url": topic["FirstURL"]}
        for topic in data.get("RelatedTopics", [])
        if "Text" in topic and topic.get("FirstURL")
    ]
    return {
        "abstract": data.get("AbstractText") or None,
        "abstract_source": data.get("AbstractSource"),
        "source": "DuckDuckGo",
        "source_url": data.get("AbstractURL") or query_url,
        "related_topics": related[:25],
    }
