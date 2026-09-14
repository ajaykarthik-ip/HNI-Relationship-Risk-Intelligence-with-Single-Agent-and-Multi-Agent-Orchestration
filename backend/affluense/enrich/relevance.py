"""Is this article actually about the company it was collected for?

News APIs match loosely. A phrase query for "Hindustan Computers" returned a
US immigration story — "Whistleblower Lawsuit Uncovers Massive H-1B Fraud" —
which then became the only adverse finding in a report about Shiv Nadar.

Nothing downstream can recover from that: the sentiment is computed on it, the
risk keywords fire on it, and it is presented with a real source URL. The
check has to happen before the article is counted.
"""

from __future__ import annotations

import re

from ..resolve.company import DESCRIPTORS, LEGAL_SUFFIXES

# Words that identify nothing on their own.
UNDISTINCTIVE = set(LEGAL_SUFFIXES) | set(DESCRIPTORS) | {
    "the", "and", "of", "for", "in", "at", "a", "an", "india", "indian",
    "global", "international", "national", "new", "first", "best", "my",
}


def distinctive_tokens(name: str) -> list:
    """The words that make a company name identifiable.

    Short tokens are kept: HCL, SLB and Jio are the whole name, not noise.
    """
    text = re.sub(r"[^\w\s]", " ", (name or "").lower())
    tokens = [t for t in text.split() if t and t not in UNDISTINCTIVE and len(t) > 2]
    # Every token dropped as undistinctive: fall back to the raw words so the
    # filter never degrades into "match anything".
    return tokens or [t for t in text.split() if len(t) > 2]


# A name made only of ordinary English words identifies nothing on its own.
# "More" is a retail chain; requiring the word "more" to appear keeps almost
# every headline ever written, and 20 unrelated articles were scored as that
# company's coverage. Membership here does not drop the token — it says the
# name alone is not enough evidence, so something else has to corroborate it.
COMMON_WORDS = {
    "more", "less", "most", "next", "now", "one", "two", "open", "live",
    "big", "small", "good", "great", "best", "better", "real", "true",
    "free", "fast", "smart", "simple", "easy", "clear", "bright", "sharp",
    "people", "work", "works", "home", "house", "life", "world", "time",
    "money", "health", "food", "water", "power", "energy", "space", "air",
    "wave", "flow", "peak", "edge", "core", "base", "point", "line", "link",
    "way", "path", "step", "move", "give", "make", "think", "learn", "care",
    "family", "friend", "future", "today", "buy", "sell", "shop", "store",
    "market", "value", "trust", "hope", "dream", "rise", "up", "down",
}


def is_weak_name(tokens: list) -> bool:
    """True when the name's distinctive words are all ordinary English.

    Two rare words still identify a company between them, so the test is
    whether *every* token is common, not whether any one of them is.
    """
    return bool(tokens) and all(t in COMMON_WORDS for t in tokens)


def corroborating_tokens(*groups) -> list:
    """Words that confirm a weak name: aliases, sectors, the owner's name."""
    found = []
    for group in groups:
        for value in group or []:
            for token in distinctive_tokens(value):
                if token not in COMMON_WORDS and token not in found:
                    found.append(token)
    return found


def is_about(article, tokens: list) -> bool:
    """Require *every* distinctive token to appear.

    "any token" is too loose — "Hindustan" alone matches Hindustan Times and
    Hindustan Unilever. Requiring all of them means an article about
    Hindustan Computers has to mention both words.
    """
    if not tokens:
        return True

    haystack = f"{article.headline or ''} {article.snippet or ''}".lower()
    return all(re.search(rf"\b{re.escape(token)}", haystack) for token in tokens)


def filter_for_company(articles: list, company_name: str,
                       corroborators: list | None = None) -> tuple:
    """Return (kept, dropped_count) for one company's collected news.

    A weak name needs a second word before an article counts. With nothing to
    corroborate it, every article is dropped and the company is reported as
    having insufficient coverage — which is the honest answer. Scoring
    sentiment on headlines that merely contain the word "more" is not.
    """
    tokens = distinctive_tokens(company_name)

    if is_weak_name(tokens):
        corroborators = [c for c in (corroborators or []) if c not in tokens]
        if not corroborators:
            return [], len(articles)
        kept = [
            article for article in articles
            if is_about(article, tokens)
            and _mentions_any(article, corroborators)
        ]
        return kept, len(articles) - len(kept)

    kept = [article for article in articles if is_about(article, tokens)]
    return kept, len(articles) - len(kept)


def _mentions_any(article, tokens: list) -> bool:
    haystack = f"{article.headline or ''} {article.snippet or ''}".lower()
    return any(re.search(rf"\b{re.escape(token)}", haystack) for token in tokens)
