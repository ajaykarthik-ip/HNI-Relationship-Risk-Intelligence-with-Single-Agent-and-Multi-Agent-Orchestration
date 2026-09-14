"""Who published this, and how much it counts.

Every article weighed the same, so a press release and a wire-service
investigation carried identical force. That is how Adani Energy Solutions
reached 26 positive articles and 0 negative ones: the company publishes far
more than journalists write about it, and a generic query returns exactly
that.

Four tiers:

  0  promotional   PR wires and the subject's own domains. Excluded from the
                   evidence set entirely and counted separately, because a
                   company's own announcements are not evidence about the
                   company.
  1  major         wire services and national business desks. A material
                   finding needs at least one of these or a tier 2.
  2  established   trade press, regional and national dailies.
  3  unknown       everything unlisted. Counted at low weight rather than
                   dropped, so an unlisted publisher degrades gracefully
                   instead of vanishing.

The lists are maintained by hand and will never be complete. That is why
unknown is a tier and not a rejection.
"""

from __future__ import annotations

import re
import urllib.parse

PROMOTIONAL = 0
MAJOR = 1
ESTABLISHED = 2
UNKNOWN = 3

# Weight applied to an article's contribution to a company's tone. Tier 0
# never reaches scoring at all; its weight is here only for completeness.
TIER_WEIGHT = {PROMOTIONAL: 0.0, MAJOR: 1.0, ESTABLISHED: 0.8, UNKNOWN: 0.4}

TIER_NAME = {
    PROMOTIONAL: "promotional",
    MAJOR: "tier 1",
    ESTABLISHED: "tier 2",
    UNKNOWN: "tier 3",
}

# Wire services that distribute copy written by the subject of the story.
PRESS_RELEASE_DOMAINS = {
    "prnewswire.com", "prnewswire.co.in", "businesswire.com", "globenewswire.com",
    "einpresswire.com", "einnews.com", "openpr.com", "prlog.org", "pressat.co.uk",
    "newswire.com", "accesswire.com", "prweb.com", "24-7pressrelease.com",
    "issuewire.com", "prfree.org", "pr.com", "medianews4u.com",
    "businesswireindia.com", "prnewswire.com.au", "adityabirla.com",
}

# Phrases that mark syndicated promotional copy whatever the domain.
PROMOTIONAL_MARKERS = re.compile(
    r"\b(press release|prnewswire|businesswire|globe ?newswire|"
    r"sponsored (content|post|feature)|paid (post|content)|advertorial|"
    r"brand (post|connect|studio)|partner content|in association with)\b",
    re.IGNORECASE,
)

MAJOR_DOMAINS = {
    # Wires and international
    "reuters.com", "bloomberg.com", "apnews.com", "ft.com", "wsj.com",
    "nytimes.com", "theguardian.com", "bbc.com", "bbc.co.uk", "economist.com",
    "cnbc.com", "forbes.com", "afp.com",
    # Indian national business desks
    "economictimes.indiatimes.com", "business-standard.com", "livemint.com",
    "thehindubusinessline.com", "financialexpress.com", "moneycontrol.com",
    "thehindu.com", "indianexpress.com", "hindustantimes.com",
    "timesofindia.indiatimes.com", "ndtv.com", "theprint.in", "thewire.in",
    "scroll.in", "business-today.in", "businesstoday.in", "cnbctv18.com",
    "bqprime.com", "ndtvprofit.com",
}

ESTABLISHED_DOMAINS = {
    "deccanherald.com", "telegraphindia.com", "newindianexpress.com",
    "tribuneindia.com", "freepressjournal.in", "dnaindia.com", "firstpost.com",
    "outlookindia.com", "theweek.in", "fortuneindia.com", "entrackr.com",
    "inc42.com", "yourstory.com", "medianama.com", "livelaw.in", "barandbench.com",
    "thehindu.co.in", "rediff.com", "zeebiz.com", "financialtimes.com",
    "techcrunch.com", "wired.com", "arstechnica.com", "theverge.com",
    "caixinglobal.com", "nikkei.com", "scmp.com",
}


# Search engines and aggregators. They carry articles; they do not publish
# them. Counting "bing.com" as an independent source inflated corroboration,
# which is the single number that decides whether a finding is material.
AGGREGATOR_DOMAINS = {
    "news.google.com", "google.com", "bing.com", "msn.com", "news.yahoo.com",
    "yahoo.com", "duckduckgo.com", "flipboard.com", "smartnews.com",
    "inshorts.com", "dailyhunt.in", "newsbreak.com",
}


def domain_of(url: str) -> str:
    """Registrable-ish host, lowercased, without www."""
    host = urllib.parse.urlsplit(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def article_domain(article) -> str:
    """The publisher's domain, preferring what the feed said over the link.

    A feed link is usually a redirector. `publisher_url` is the publisher's
    own address when the feed gave one, which is the whole point of reading it
    out of the RSS rather than following the redirect.
    """
    for candidate in (getattr(article, "publisher_url", None),
                      getattr(article, "url", None)):
        host = domain_of(candidate or "")
        if host and not _matches(host, AGGREGATOR_DOMAINS):
            return host
    return ""


def _matches(host: str, domains: set) -> bool:
    """True for the domain itself or any subdomain of it."""
    return any(host == d or host.endswith("." + d) for d in domains)


def publisher_name(article) -> str:
    """A stable name for counting independent sources.

    Returns "" when the publisher cannot be identified, and the caller must
    treat that as *no* source rather than as one more. A search engine's
    domain standing in for a publisher is how "NDTV, bing.com" came to be
    reported as two independent outlets.
    """
    named = (getattr(article, "publisher", None) or "").strip()
    if named and not _matches(domain_of(named), AGGREGATOR_DOMAINS):
        return named
    return article_domain(article)


def tier_for(article, owner_domains: set | None = None) -> int:
    """Which tier this article's publisher belongs to.

    `owner_domains` are the subject's and their companies' own sites, which
    are promotional whatever else they look like.
    """
    host = article_domain(article)
    text = " ".join(filter(None, [
        getattr(article, "publisher", None),
        getattr(article, "headline", None),
        getattr(article, "snippet", None),
    ]))

    if host and owner_domains and _matches(host, owner_domains):
        return PROMOTIONAL
    if host and _matches(host, PRESS_RELEASE_DOMAINS):
        return PROMOTIONAL
    if PROMOTIONAL_MARKERS.search(text):
        return PROMOTIONAL
    if host and _matches(host, MAJOR_DOMAINS):
        return MAJOR
    if host and _matches(host, ESTABLISHED_DOMAINS):
        return ESTABLISHED

    # Google and Bing route clicks through their own domains, so the URL host
    # is useless and the publisher name is all there is. Match it by label.
    if named_tier(text) is not None:
        return named_tier(text)
    return UNKNOWN


def named_tier(text: str) -> int | None:
    """Tier from a publisher's printed name, for redirected URLs."""
    lowered = (text or "").lower()
    for domain in MAJOR_DOMAINS:
        label = domain.split(".")[0].replace("-", " ")
        if len(label) > 4 and label in lowered:
            return MAJOR
    for domain in ESTABLISHED_DOMAINS:
        label = domain.split(".")[0].replace("-", " ")
        if len(label) > 4 and label in lowered:
            return ESTABLISHED
    return None


def annotate(articles: list, owner_domains: set | None = None) -> None:
    """Stamp tier, publisher and promotional flag onto each article."""
    for article in articles:
        article.publisher_tier = tier_for(article, owner_domains)
        article.publisher_name = publisher_name(article)
        article.is_promotional = article.publisher_tier == PROMOTIONAL


def split_promotional(articles: list) -> tuple:
    """(evidence, promotional). Promotional copy never reaches scoring."""
    evidence = [a for a in articles if not getattr(a, "is_promotional", False)]
    promotional = [a for a in articles if getattr(a, "is_promotional", False)]
    return evidence, promotional


def independent_publishers(articles: list) -> list:
    """Distinct publishers behind a set of articles, sorted by tier.

    An article whose publisher could not be identified contributes nothing.
    Corroboration is the number that decides whether a finding is material, so
    an unidentifiable source must not be able to raise it.
    """
    best: dict = {}
    for article in articles:
        name = getattr(article, "publisher_name", None) or publisher_name(article)
        if not name:
            continue
        tier = getattr(article, "publisher_tier", UNKNOWN)
        if name not in best or tier < best[name]:
            best[name] = tier
    return [name for name, _tier in sorted(best.items(), key=lambda kv: (kv[1], kv[0]))]


def best_tier(articles: list) -> int:
    """The strongest publisher in a set. Lower is better."""
    tiers = [getattr(a, "publisher_tier", UNKNOWN) for a in articles]
    return min(tiers) if tiers else UNKNOWN


def tier_breakdown(articles: list) -> dict:
    """How the evidence splits across tiers, for the coverage report."""
    counts = {TIER_NAME[t]: 0 for t in (MAJOR, ESTABLISHED, UNKNOWN, PROMOTIONAL)}
    for article in articles:
        counts[TIER_NAME[getattr(article, "publisher_tier", UNKNOWN)]] += 1
    return counts


def owner_domains_for(subject_name: str, company_names: list) -> set:
    """Domains that belong to the subject or their companies.

    Deliberately crude: a company's own site is promotional, and guessing
    "<brand>.com" catches most of them. A wrong guess costs one domain being
    treated as promotional, which is the safe direction to be wrong in.
    """
    stems = []
    for name in [subject_name, *(company_names or [])]:
        cleaned = re.sub(r"[^a-z0-9 ]", "", (name or "").lower()).split()
        if cleaned:
            stems.append("".join(cleaned[:2]))
            stems.append(cleaned[0])
    return {f"{s}.com" for s in stems if len(s) > 3} | {
        f"{s}.in" for s in stems if len(s) > 3
    }
