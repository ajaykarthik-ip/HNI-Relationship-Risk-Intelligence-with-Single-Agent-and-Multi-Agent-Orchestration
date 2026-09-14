"""Evidence Agent — what has been published about this entity?

The workhorse, and where most of V1's runtime goes. One instance per company
plus one for the person, all in flight together, each issuing its fifteen
queries across two feeds concurrently instead of thirty round trips in a row.

The query ladder is V1's, imported from `collect.queries` rather than restated:
fourteen adverse groups then the base query, adverse first so a cap truncates
general coverage rather than the check for enforcement action. **V2 asks exactly
the same questions V1 asks.** Nothing here trims coverage to buy speed.

One structural difference from V1, and it is a latency win rather than a
behaviour change: the person's evidence needs only the resolved name and the
company the user typed, so it has no dependency on company discovery at all. V1
nonetheless runs it last, after every company has been screened
(`pipeline.py:784`). Here it starts the moment identity resolves and overlaps
discovery entirely.

    in    an entity name and kind, plus aliases, sectors and owner domains
    out   filtered, annotated, promotion-split articles, and what was asked
    LLM   none
    fails a dead feed is a note, not an exception; the other feed's articles
          still count, and an entity with nothing at all becomes
          insufficient_coverage, which risk_score.py already handles
"""

from __future__ import annotations

from affluense import config as v1config
from affluense.collect import queries
from affluense.enrich import publishers, relevance

from ..sources import news_async


async def collect_company(transport, reporter, record: dict, resolved: str,
                          owner_domains: set) -> list:
    """One company's evidence: search, filter for relevance, split promotion.

    Mirrors `pipeline.collect_for` step for step, including the order the
    filters run in. Relevance before provenance before promotion matters: a
    press release is not evidence about the company that issued it, and
    counting it is how one Adani entity reached 26 positive articles and zero
    negative ones.
    """
    company_name = record.get("canonical_name") or record["name"]
    reporter.say(f"  Screening: {company_name}")

    # The adverse ladder, not one generic query. Asking only the company's name
    # returns what the company publishes; the EOW complaint against a BharatPe
    # founder was never fetched because nothing ever asked for it.
    query_set = queries.for_company(company_name)
    collected = await news_async.collect_many(
        transport, query_set, v1config.NEWS_PER_QUERY, about=company_name,
        say=reporter.say,
    )
    record["queries_issued"] = len(query_set)
    record["groups_checked"] = queries.groups_checked(query_set)

    # Drop anything that is not actually about this company before it can
    # influence sentiment or raise a flag. A name like "More" matches almost
    # any headline, so aliases, sector and the subject's own name have to
    # corroborate it before an article counts as this company's coverage.
    articles, dropped = relevance.filter_for_company(
        collected,
        company_name,
        corroborators=relevance.corroborating_tokens(
            record.get("aliases"), record.get("sectors"), [resolved]
        ),
    )
    if dropped:
        reporter.say(
            f"    dropped {dropped} article(s) not about {company_name}"
        )

    publishers.annotate(articles, owner_domains)
    evidence, promotional = publishers.split_promotional(articles)
    record["promotional_excluded"] = len(promotional)
    if promotional:
        reporter.say(f"    excluded {len(promotional)} promotional item(s)")

    reporter.advance("evidence")
    return evidence


async def collect_person(transport, reporter, resolved: str,
                         company: str | None) -> tuple:
    """The individual's own coverage. Returns (articles, query_set).

    Deliberately unfiltered and un-annotated here. V1 de-duplicates the
    person's articles against everything already assigned to a company, then
    annotates provenance -- and both of those steps depend on results that do
    not exist yet when this starts. So the agent does the part that only needs
    the name, and the orchestrator finishes it in V1's order.

    A matter naming the person is theirs wherever it happened, and it is the
    one thing a company query can miss entirely.
    """
    reporter.say("  Screening the individual ...")
    person_queries = queries.for_person(resolved, company)
    articles = await news_async.collect_many(
        transport, person_queries, v1config.NEWS_PER_QUERY,
        say=reporter.say,
    )
    reporter.advance("evidence")
    return articles, person_queries
