"""News feeds. All three are free, keyless, and meant to be consumed."""

from __future__ import annotations

import re
import urllib.parse
from xml.etree import ElementTree

from .. import config
from ..models import Article


def _rss_items(fetcher, url: str, limit: int) -> list:
    response = fetcher.get(url)
    if response is None:
        return []
    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError:
        fetcher.note(f"RSS from {url} did not parse")
        return []
    return list(root.iterfind(".//item"))[:limit]


def _google_item(item, feed_url: str, about: str | None) -> Article:
    """One Google News entry.

    The <link> is a news.google.com redirector and the real article URL is not
    in the feed. But <source url="..."> carries the publisher's own domain,
    which is all that is needed to identify and tier them -- and without it
    every article looked like it came from Google.
    """
    source = item.find("source")
    publisher_url = (source.get("url") if source is not None else None) or None
    return Article(
        headline=(item.findtext("title") or "").strip(),
        url=(item.findtext("link") or "").strip(),
        publisher=(item.findtext("source") or "").strip() or None,
        publisher_url=publisher_url,
        published=(item.findtext("pubDate") or "").strip() or None,
        # Still a redirect: the article's own URL is genuinely not available,
        # so its body cannot be fetched. Recorded rather than worked around.
        url_is_redirect=True,
        about_company=about,
        source="Google News RSS",
        source_url=feed_url,
    )


def _unwrap_bing(link: str) -> str:
    """The real article URL out of a Bing click-tracking link.

    Bing wraps every result as apiclick.aspx?...&url=<encoded>&... so the
    destination is right there in the query string. Unwrapping it costs
    nothing and is what lets the article be tiered by publisher and, unlike
    Google's, actually read.
    """
    if "bing.com" not in link:
        return link
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
    target = (query.get("url") or query.get("u") or [""])[0]
    return target if target.startswith("http") else link


def google_news(fetcher, query: str, limit: int, about: str | None = None) -> list:
    url = (
        f"{config.GOOGLE_NEWS_RSS}?q={urllib.parse.quote(query)}"
        "&hl=en-IN&gl=IN&ceid=IN:en"
    )
    return [_google_item(item, url, about) for item in _rss_items(fetcher, url, limit)]


def bing_news(fetcher, query: str, limit: int, about: str | None = None) -> list:
    url = f"{config.BING_NEWS_RSS}?q={urllib.parse.quote(query)}&format=RSS"
    articles = []
    for item in _rss_items(fetcher, url, limit):
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


def identity_key(article) -> str:
    """What makes two rows the same story.

    RSS appends " - Publisher" to headlines, so the same wire story arrives
    as several distinct-looking rows. Strip that suffix before comparing.
    """
    stripped = re.sub(r"\s+-\s+[^-]+$", "", (article.headline or "").lower())
    return re.sub(r"[^a-z0-9]+", "", stripped)[:70]


def dedupe(articles: list, seen: set | None = None) -> list:
    """Collapse syndicated copy.

    `seen` lets one set of keys span several companies. A group's flagship
    story is returned by the query for every subsidiary, and counting it once
    per company inflated a single fact six times across the Adani entities --
    which then skewed every sentiment ratio that was computed from it.
    """
    seen = set() if seen is None else seen
    unique = []
    for article in articles:
        key = identity_key(article)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(article)
    return unique


def collect(fetcher, query: str, limit: int, about: str | None = None) -> list:
    """Every feed for one query, de-duplicated.

    GDELT was removed: its free DOC endpoint rate-limited every request in
    practice, so it contributed no articles while costing 22 requests and
    several seconds per run.
    """
    articles = []
    articles += google_news(fetcher, query, limit, about)
    articles += bing_news(fetcher, query, limit, about)
    return dedupe(articles)


def collect_many(fetcher, queries: list, limit: int, about: str | None = None,
                 say=None) -> list:
    """Run a whole query set and return one de-duplicated evidence list.

    `queries` are {"query", "group"} records from collect.queries. The group
    is stamped onto every article it produced, so the report can later say
    which adverse check surfaced a finding -- and, for a clean subject, which
    checks ran and came back empty.

    De-duplication spans the whole set: an adverse query and the base query
    return the same story constantly, and counting it twice would inflate
    both the evidence count and the tone breakdown.
    """
    seen: set = set()
    collected: list = []

    for entry in queries:
        found = collect(fetcher, entry["query"], limit, about)
        fresh = dedupe(found, seen=seen)
        for article in fresh:
            article.query_group = entry["group"]
        collected += fresh
        if say and fresh and entry["group"] != "general":
            say(f"      {entry['group']}: {len(fresh)} new")

    return collected
