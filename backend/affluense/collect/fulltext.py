"""Read the article, not just the headline.

Everything downstream used to see an RSS headline and a one-line snippet.
That is enough to *notice* a matter and far too little to *assess* one. The
stage a case has reached, the date it happened, who was actually named, and
whether it was later dismissed all live in the body text. Asking a model to
judge severity from a headline is asking it to guess.

So articles that look material get fetched in full. Which ones is decided
deterministically and cheaply, before anything expensive happens:

  it carries a risk term        the existing keyword taxonomy, which is good
                                at spotting candidates and bad at judging them
  its publisher is tier 1 or 2  an unknown aggregator is not worth the request
  it is not already fetched     one story, one fetch

Fetching is free. `requests` and `beautifulsoup4` are already dependencies and
the shared Fetcher already does rate limiting, retries, caching and a robots
check. Firecrawl is a fallback for pages that return nothing readable, behind
its own budget that defaults to zero -- it is the only source here that spends
credits, and it should never do so without being asked.

What this module will not do: pretend. A paywalled page is recorded as
paywalled and the finding falls back to headline evidence at lower
confidence. A page robots.txt forbids is skipped and marked, not worked
around.
"""

from __future__ import annotations

import re

from .. import config
from ..enrich import publishers, risk

# Nodes that are never article text.
_STRIP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "form",
               "noscript", "iframe", "figure", "figcaption")

# Where the body usually lives, most specific first.
_BODY_SELECTORS = (
    "article", '[itemprop="articleBody"]', ".article-body", ".articlebody",
    ".story-body", ".story-content", ".entry-content", ".post-content",
    "#article-body", ".content-body", "main",
)

# Copy that means "you cannot read this".
_PAYWALL = re.compile(
    r"\b(subscribe to (continue|read)|already a subscriber|"
    r"this (article|story) is for subscribers|"
    r"sign in to (read|continue)|create an account to (read|continue)|"
    r"unlock this (article|story)|premium (article|story|content))\b",
    re.IGNORECASE,
)

# Below this, whatever came back is navigation, not an article.
MIN_BODY_CHARS = 400


def worth_fetching(article) -> bool:
    """Whether this article's body is worth a request.

    Narrow on purpose. Full text improves *findings*; it does nothing for the
    tone of an ordinary business story, and fetching 400 articles to read 380
    that say nothing would spend the run's time budget on noise.
    """
    if getattr(article, "is_promotional", False):
        return False  # promotional copy is never evidence
    if getattr(article, "body_text", None):
        return False
    if getattr(article, "publisher_tier", 3) > publishers.ESTABLISHED:
        return False
    if getattr(article, "url_is_redirect", False):
        # Google News hides the article behind its own redirector and the real
        # URL is not in the feed, so there is nothing to fetch. Bing's wrapper
        # is unwrapped at collection time and those are fetchable.
        return False
    return bool(getattr(article, "risk_categories", None))


def select(articles: list, cap: int | None = None) -> list:
    """The articles to fetch, best evidence first.

    Ordered by publisher tier then by how far the matter has progressed, so a
    cap truncates the weakest candidates rather than an arbitrary tail.
    """
    limit = config.MAX_FULLTEXT_FETCHES if cap is None else cap
    candidates = [a for a in articles if worth_fetching(a)]
    candidates.sort(key=lambda a: (
        getattr(a, "publisher_tier", 3),
        -risk.STAGE_SEVERITY.get(getattr(a, "risk_stage", None) or "reported", 1),
    ))
    return candidates[:limit] if limit else candidates


def _extract_text(html: str) -> str:
    """Readable body text from a page, or "" if there is none.

    Deliberately simple. A full readability implementation is a project in
    itself; the paragraphs inside the most specific matching container get
    most articles right, and a bad extraction shows up as a short string that
    the caller rejects.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(list(_STRIP_TAGS)):
        tag.decompose()

    container = None
    for selector in _BODY_SELECTORS:
        container = soup.select_one(selector)
        if container is not None:
            break
    container = container or soup

    paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
    text = " ".join(p for p in paragraphs if len(p) > 40)
    return re.sub(r"\s+", " ", text).strip()


def fetch_one(fetcher, article) -> str:
    """Fetch and store one article's body. Returns the fetch status."""
    url = getattr(article, "url", "") or ""
    if not url:
        article.fetch_status = "failed"
        return article.fetch_status

    if not fetcher.robots_allow(url):
        fetcher.note(f"robots.txt disallows fetching {publishers.domain_of(url)}")
        article.fetch_status = "blocked"
        return article.fetch_status

    response = fetcher.get(url, attempts=2)
    if response is None:
        article.fetch_status = "failed"
        return article.fetch_status

    text = _extract_text(getattr(response, "text", "") or "")

    if _PAYWALL.search(text[:1500]) or (text and len(text) < MIN_BODY_CHARS):
        article.body_text = text[: config.FULLTEXT_CHARS] or None
        article.fetch_status = "paywalled" if _PAYWALL.search(text[:1500]) else "partial"
        return article.fetch_status

    if not text:
        article.fetch_status = "failed"
        return article.fetch_status

    article.body_text = text[: config.FULLTEXT_CHARS]
    article.fetch_status = "full"
    return article.fetch_status


def fetch(fetcher, articles: list, cap: int | None = None, say=None) -> dict:
    """Fetch the material articles. Returns a status tally for the report."""
    targets = select(articles, cap)
    tally = {"attempted": len(targets), "full": 0, "partial": 0,
             "paywalled": 0, "blocked": 0, "failed": 0}

    for article in targets:
        tally[fetch_one(fetcher, article)] += 1

    if say and targets:
        say(f"    read {tally['full']} of {len(targets)} article(s) in full"
            + (f", {tally['paywalled']} paywalled" if tally["paywalled"] else ""))
    return tally


def quote_for(article, max_chars: int = 260) -> str | None:
    """A sentence from the article that carries the risk term.

    This is what makes a finding quotable. Falls back to the headline when
    there is no body, and says so through the article's fetch_status rather
    than passing a headline off as a quotation from the text.
    """
    body = getattr(article, "body_text", None)
    if not body:
        return None

    sentences = re.split(r"(?<=[.!?])\s+", body)
    categories = getattr(article, "risk_categories", None) or []
    patterns = [p for c in categories for p in risk.RISK_TERMS.get(c, [])]

    for sentence in sentences:
        if len(sentence) < 40:
            continue
        if any(re.search(p, sentence, re.IGNORECASE) for p in patterns):
            return sentence[:max_chars].strip()

    return next((s.strip()[:max_chars] for s in sentences if len(s) > 60), None)
