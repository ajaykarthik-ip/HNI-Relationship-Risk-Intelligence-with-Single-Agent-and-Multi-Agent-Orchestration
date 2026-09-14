"""Fulltext Agent — read the article, not just the headline.

Selection, paywall detection, body extraction and the meaning of every
`fetch_status` are V1's, imported from `collect.fulltext`. A headline says a
matter exists; the body says what stage it reached, when, and who was named, and
that judgement must not differ between engines.

What changes is the worst-behaved loop in V1. `fulltext.fetch` walks up to forty
articles one at a time, and each one pays twice:

  robots.txt   V1 goes through `urllib.robotparser`, which uses its own opener
               with **no timeout**, off the pooled session, uncached and
               unmetered. One unresponsive publisher stalls the whole run, and
               because it never reaches the meter the cost is invisible in every
               usage report. Here every distinct origin is warmed concurrently,
               with a timeout, through the ordinary transport.

  the page     a gated GET plus a BeautifulSoup parse, serially.

Politeness is unchanged where it is owed: the host policy allows **one request
at a time per publisher domain**, exactly as strict as V1. The speedup comes
from reading different publishers at once, never from leaning on one.

    in    the assigned article set, in deterministic order, and the run budget
    out   body_text and fetch_status per article, plus the tally the report
          prints
    LLM   none
    fails identical semantics to V1 -- blocked, paywalled, partial, failed --
          and never pretending. A robots timeout is `blocked` with a note
          instead of a hang.
"""

from __future__ import annotations

from affluense import config as v1config
from affluense.collect import fulltext as v1fulltext
from affluense.enrich import publishers

from .. import config as v2config
from ..concurrency import map_bounded

EMPTY_TALLY = {"attempted": 0, "full": 0, "partial": 0, "paywalled": 0,
               "blocked": 0, "failed": 0}


def select(articles: list, budget) -> list:
    """The articles worth reading, best evidence first, within the run budget.

    Selection is V1's -- publisher tier, then how far the matter has progressed,
    so a cap truncates the weakest candidates rather than an arbitrary tail. It
    runs **before** any fan-out and claims its slots in that order, which is
    what keeps two runs spending the budget on the same articles.
    """
    remaining = budget.remaining
    if remaining <= 0:
        return []
    chosen = v1fulltext.select(articles, cap=remaining)
    granted = budget.claim(len(chosen))
    return chosen[:granted]


async def _fetch_one(transport, bridge, article) -> str:
    """Fetch and store one article's body. Returns the fetch status."""
    url = getattr(article, "url", "") or ""
    if not url:
        article.fetch_status = "failed"
        return article.fetch_status

    if not await transport.robots_allow(url):
        transport.note(
            f"robots.txt disallows fetching {publishers.domain_of(url)}"
        )
        article.fetch_status = "blocked"
        return article.fetch_status

    response = await transport.get(url, attempts=2)
    if response is None:
        article.fetch_status = "failed"
        return article.fetch_status

    # BeautifulSoup over a full page is CPU-bound and long enough to matter at
    # this concurrency, so it runs off the event loop.
    text = await bridge.run_bare(
        v1fulltext._extract_text, getattr(response, "text", "") or "",
    )

    paywalled = bool(v1fulltext._PAYWALL.search(text[:1500]))
    if paywalled or (text and len(text) < v1fulltext.MIN_BODY_CHARS):
        article.body_text = text[: v1config.FULLTEXT_CHARS] or None
        article.fetch_status = "paywalled" if paywalled else "partial"
        return article.fetch_status

    if not text:
        article.fetch_status = "failed"
        return article.fetch_status

    article.body_text = text[: v1config.FULLTEXT_CHARS]
    article.fetch_status = "full"
    return article.fetch_status


async def fetch(transport, bridge, reporter, articles: list, budget,
                label: str = "") -> dict:
    """Read the material articles for one entity. Returns the status tally."""
    targets = select(articles, budget)
    tally = dict(EMPTY_TALLY)
    tally["attempted"] = len(targets)
    if not targets:
        return tally

    # Warm every origin at once before the fetch loop, so no single fetch
    # blocks on a robots lookup. Serially this is the slowest thing V1 does.
    await transport.prefetch_robots([getattr(a, "url", "") for a in targets])

    async def one(article):
        return await _fetch_one(transport, bridge, article)

    statuses = await map_bounded(
        targets, one, v2config.MAX_FULLTEXT_CONCURRENCY,
        on_error=lambda i, e: transport.note(
            f"Reading {getattr(targets[i], 'url', '?')} failed: {e}"
        ),
    )
    for status in statuses:
        # A task that raised is counted as a failed fetch rather than dropped,
        # so the tally still adds up to what was attempted.
        tally[status if status in tally else "failed"] += 1

    reporter.advance("fulltext", by=len(targets))
    if targets:
        suffix = f", {tally['paywalled']} paywalled" if tally["paywalled"] else ""
        reporter.say(
            f"    read {tally['full']} of {len(targets)} article(s) in full"
            + suffix + (f" for {label}" if label else "")
        )
    return tally
