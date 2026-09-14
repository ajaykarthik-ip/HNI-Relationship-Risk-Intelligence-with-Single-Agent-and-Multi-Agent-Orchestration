"""News feeds, concurrently.

The parsing is V1's, imported rather than restated: `_google_item` builds the
Article, `_unwrap_bing` recovers the real URL out of Bing's click tracker, and
`dedupe` collapses syndicated copy on V1's identity key. If those ever change,
both engines change together.

What is new is the shape of the work. V1 walks fifteen queries one at a time
(`news.collect_many`), and each one fetches Google then Bing, so a company's
evidence costs thirty sequential round trips against a one-per-second gate. Here
all thirty are issued at once and the host gates decide the pace.

The de-duplication still happens afterwards, in one deterministic pass, because
`dedupe`'s `seen` set is order-dependent: letting concurrent tasks mutate it
would make two runs over the same evidence disagree about which copy of a wire
story survived.
"""

from __future__ import annotations

import urllib.parse
from xml.etree import ElementTree

from affluense import config as v1config
from affluense.models import Article
from affluense.sources.news import _google_item, _unwrap_bing, dedupe

from ..concurrency import gather_ordered

__all__ = ["collect_many", "collect_one", "google_news", "bing_news", "dedupe"]


async def _rss_items(transport, url: str, limit: int) -> list:
    response = await transport.get(url)
    if response is None:
        return []
    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError:
        transport.note(f"RSS from {url} did not parse")
        return []
    return list(root.iterfind(".//item"))[:limit]


def google_url(query: str) -> str:
    return (
        f"{v1config.GOOGLE_NEWS_RSS}?q={urllib.parse.quote(query)}"
        "&hl=en-IN&gl=IN&ceid=IN:en"
    )


def bing_url(query: str) -> str:
    return f"{v1config.BING_NEWS_RSS}?q={urllib.parse.quote(query)}&format=RSS"


async def google_news(transport, query: str, limit: int,
                      about: str | None = None) -> list:
    url = google_url(query)
    items = await _rss_items(transport, url, limit)
    return [_google_item(item, url, about) for item in items]


async def bing_news(transport, query: str, limit: int,
                    about: str | None = None) -> list:
    url = bing_url(query)
    items = await _rss_items(transport, url, limit)
    articles = []
    for item in items:
        raw = (item.findtext("link") or "").strip()
        resolved = _unwrap_bing(raw)
        articles.append(Article(
            headline=(item.findtext("title") or "").strip(),
            url=resolved,
            publisher_url=resolved,
            published=(item.findtext("pubDate") or "").strip() or None,
            snippet=(item.findtext("description") or "").strip() or None,
            # Only still a redirect if unwrapping failed.
            url_is_redirect=resolved == raw and "bing.com" in raw,
            about_company=about,
            source="Bing News RSS",
            source_url=url,
        ))
    return articles


async def collect_one(transport, query: str, limit: int,
                      about: str | None = None) -> list:
    """Both feeds for one query, concurrently, de-duplicated within the query.

    Matches `news.collect` exactly, including the order the two feeds are
    concatenated in -- which decides which copy of a syndicated story `dedupe`
    keeps.
    """
    feeds = ("Google News", "Bing News")

    def failed(index: int, exc: BaseException) -> None:
        # One feed failing must not cost the other's articles.
        transport.note(f"{feeds[index]} failed for a query: {exc}")

    google, bing = await gather_ordered(
        [
            google_news(transport, query, limit, about),
            bing_news(transport, query, limit, about),
        ],
        on_error=failed,
    )
    return dedupe((google or []) + (bing or []))


async def collect_many(transport, queries: list, limit: int,
                       about: str | None = None, say=None) -> list:
    """The whole query set for one entity, all in flight at once.

    `queries` are {"query", "group"} records from `collect.queries`. The group
    is stamped onto every article it produced, so the report can say which
    adverse check surfaced a finding -- and, for a clean subject, which checks
    ran and came back empty.

    De-duplication spans the set and runs in query order afterwards, exactly as
    V1 does it: an adverse query and the base query return the same story
    constantly, and counting it twice inflates both the evidence count and the
    tone breakdown.
    """
    if not queries:
        return []

    def failed(index: int, exc: BaseException) -> None:
        transport.note(
            f"The {queries[index]['group']} check failed for "
            f"{about or 'the subject'}: {exc}"
        )

    results = await gather_ordered(
        [collect_one(transport, entry["query"], limit, about)
         for entry in queries],
        on_error=failed,
    )

    seen: set = set()
    collected: list = []
    for entry, found in zip(queries, results):
        if found is None:
            continue
        fresh = dedupe(found, seen=seen)
        for article in fresh:
            article.query_group = entry["group"]
        collected += fresh
        if say and fresh and entry["group"] != "general":
            say(f"      {entry['group']}: {len(fresh)} new")

    return collected
