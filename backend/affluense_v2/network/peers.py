"""Peer discovery that does not require a structured registry.

PS2 asks for globally discoverable people in similar roles and industries.
V1 answers it entirely through Wikidata SPARQL, which is the right primary
source -- stable identifiers, real officer relationships, no inference. But it
is also a single point of failure, and when the Query Service refuses a request
the suggestion list is empty rather than degraded.

This is the second path. It uses what V2 already has: the free news feeds, the
concurrent transport, and the reasoning layer that is allowed to read text a
real source returned.

    1  build queries from the subject's own roles and industries
    2  fetch news for each, concurrently, through the existing host policy
    3  ask the model to name the people those headlines are about, and the
       company and role stated for each
    4  hand the result to V1's scorer unchanged

Three rules keep it honest:

  no invention      the model is given headlines and asked who they name. It is
                    explicitly told to return nothing when a headline names no
                    individual, and anything it returns that is not supported by
                    a headline in the batch is discarded.

  no self-suggestion the subject, and anyone already in their network, is
                    removed before scoring -- a suggestion someone already knows
                    is not a suggestion.

  provenance kept   every candidate carries the headline and publisher it came
                    from, so a relationship manager can see the basis rather
                    than a bare name.

**Production sources.** LinkedIn (Sales Navigator / Talent Solutions) and
Crunchbase are the right sources for this in production: they carry current
employment, seniority and funding relationships directly, which is what PS2 is
really asking for, and neither requires the inference this module does. Both are
commercial and gated, so they are not wired in here. The integration point is
`discover()` -- it returns candidate dicts in the shape `scoring.rank` expects,
so an additional provider only has to produce that shape and be appended to the
pool in `agents/network.py`. See `PS2-SOURCES.md` for the field mapping.
"""

from __future__ import annotations

import json

from affluense import config as v1config

from .. import config as v2config
from ..concurrency import map_bounded
from ..sources import news_async
from ..sources.openai_async import AsyncOpenAIClient

# A pseudo-identifier for an industry we know only as a string. `scoring`
# intersects `profile["industry_qids"]` with `candidate["matched_industries"]`
# and never inspects the values, so a stable string key works exactly as a
# Q-number does -- and keeps the scorer usable when nothing resolved.
SECTOR_PREFIX = "sector:"

# Seniority words that make a headline worth asking about. Generic job
# vocabulary, not a domain list.
ROLE_TERMS = (
    "founder", "co-founder", "chief executive", "CEO", "chairman",
    "managing director", "chief technology officer", "CTO", "president",
)

SYSTEM = (
    "You extract named individuals from news headlines for a professional "
    "network mapping tool. You are given headlines already retrieved for one "
    "industry and one seniority level.\n\n"
    "Rules:\n"
    "- Report ONLY people a supplied headline actually names. Never add anyone "
    "from your own knowledge.\n"
    "- If a headline names no individual, skip it. Returning nothing is "
    "correct and expected.\n"
    "- company must be the organisation the headline associates that person "
    "with. Omit it if the headline does not state one.\n"
    "- role must be the job title the headline states. Omit it if absent. "
    "Never infer a title from the company.\n"
    "- in_industry: does the COMPANY actually operate in the industry named in "
    "`industry`? Judge the company, not the search that found it. A bank "
    "executive appearing in a search about cosmetics retail is NOT in that "
    "industry. Set false whenever the company's line of business is different, "
    "and false when you cannot tell.\n"
    "- Skip journalists, analysts and commentators quoted about someone else.\n"
    "- Skip brand ambassadors, endorsers and celebrities appearing in "
    "marketing coverage. They are in the industry's news without being in the "
    "industry.\n"
    "- Skip anyone who is the subject named in `exclude`.\n\n"
    'Reply as JSON: {"people": [{"name": str, "company": str|null, '
    '"role": str|null, "in_industry": bool, "headline_id": int}]}'
)


def sector_key(label: str) -> str:
    """A stable industry identifier from a sector name."""
    return SECTOR_PREFIX + " ".join((label or "").lower().split())


def sector_profile(companies: list, profile: dict) -> tuple:
    """Industry identifiers from company sectors, for when Wikidata gave none.

    Returns (ids, labels). The scorer treats these exactly as it treats
    Q-numbers, so industry overlap keeps working on a run where nothing
    resolved to a registry entity.
    """
    ids: list = []
    labels: dict = {}
    for company in companies or []:
        for sector in (company.get("sectors") or []):
            if not isinstance(sector, str) or not sector.strip():
                continue
            key = sector_key(sector)
            if key not in labels:
                ids.append(key)
                labels[key] = sector.strip()
    # Industries already named on the profile are just as usable as a label.
    for industry in (profile.get("industries") or []):
        if not isinstance(industry, str) or not industry.strip():
            continue
        key = sector_key(industry)
        if key not in labels:
            ids.append(key)
            labels[key] = industry.strip()
    return ids, labels


def build_queries(labels: dict, roles: list, industries: int,
                  role_count: int) -> list:
    """One query per (industry, seniority) pair, most specific first."""
    subject_roles = [r for r in (roles or []) if isinstance(r, str) and r.strip()]
    # The subject's own titles first, so peers matching them rank highest, then
    # generic seniority terms to widen the pool.
    ordered_roles: list = []
    for role in subject_roles + list(ROLE_TERMS):
        lowered = role.lower().strip()
        if lowered and lowered not in [r.lower() for r in ordered_roles]:
            ordered_roles.append(role.strip())
    ordered_roles = ordered_roles[:max(1, role_count)]

    queries = []
    for key, label in list(labels.items())[:max(1, industries)]:
        for role in ordered_roles:
            queries.append({
                "query": f'"{role}" "{label}"',
                "industry_id": key,
                "industry_label": label,
                "role": role,
            })
    return queries


def _payload(articles: list, offset: int) -> list:
    return [
        {
            "id": offset + index,
            "headline": article.headline,
            "publisher": getattr(article, "publisher_name", None)
            or article.publisher,
        }
        for index, article in enumerate(articles)
    ]


async def _read_batch(client, batch: list, offset: int, spec: dict,
                      exclude: str | None) -> list:
    """One model call over one batch of headlines."""
    answer = await client.complete_json(
        SYSTEM,
        json.dumps({
            "industry": spec["industry_label"],
            "seniority": spec["role"],
            "exclude": exclude or "",
            "headlines": _payload(batch, offset),
        }, ensure_ascii=False),
        max_tokens=1200,
    )
    if not answer or not isinstance(answer.get("people"), list):
        return []

    by_index = {offset + i: a for i, a in enumerate(batch)}
    found = []
    for row in answer["people"]:
        if not isinstance(row, dict):
            continue
        name = (row.get("name") or "").strip()
        if not name or len(name) < 3:
            continue
        # An id the batch did not contain means the answer is not grounded in
        # anything we supplied. Discarded rather than guessed at.
        article = by_index.get(row.get("headline_id"))
        if article is None:
            continue
        company = (row.get("company") or "").strip() or None
        role = (row.get("role") or "").strip() or None

        # The industry the *search* used is not the industry the *person* is
        # in. Attributing it unconditionally credited a housing-finance
        # executive with cosmetics retail, purely because he turned up in that
        # query -- and industry_overlap is 30% of the relevance score, so it
        # promoted people with nothing in common with the subject.
        in_industry = bool(row.get("in_industry"))
        found.append({
            "name": name,
            "wikidata_id": None,
            "roles": [role] if role else [],
            "companies": [company] if company else [],
            "matched_industries": [spec["industry_id"]] if in_industry else [],
            "industry_confirmed": in_industry,
            "country": None,
            # No registry presence to measure, so prominence contributes
            # nothing. That is correct: it is a proxy for documentation, and
            # these candidates are not documented in a registry at all.
            "sitelinks": 0,
            "source": "News coverage",
            "source_url": getattr(article, "url", None),
            "evidence_headline": article.headline,
            "evidence_publisher": getattr(article, "publisher_name", None)
            or article.publisher,
        })
    return found


def _merge(pool: dict, candidate: dict) -> None:
    """One row per person, accumulating roles, companies and industries."""
    key = candidate["name"].lower()
    existing = pool.get(key)
    if existing is None:
        pool[key] = candidate
        return
    for field in ("roles", "companies", "matched_industries"):
        for value in candidate.get(field, []):
            if value and value not in existing[field]:
                existing[field].append(value)
    # Confirmed in any industry is confirmed: one query judging them out of
    # scope should not erase another that judged them in it.
    if candidate.get("industry_confirmed"):
        existing["industry_confirmed"] = True


async def discover(transport, reporter, budget, profile: dict,
                   subject_name: str | None, industries: int = 3,
                   roles: int = 3, per_query: int | None = None,
                   say=None) -> list:
    """Candidates in the shape `scoring.rank` expects. Never raises."""
    labels = profile.get("industry_labels") or {}
    if not labels:
        return []

    specs = build_queries(labels, profile.get("roles") or [], industries, roles)
    if not specs:
        return []

    client = AsyncOpenAIClient(transport, budget, purpose="peer discovery")
    if not client.configured:
        # Without a key there is no safe way to turn a headline into a person:
        # a regex over names produces publishers, agencies and place names.
        # Returning nothing is better than returning noise.
        transport.note(
            "Peer discovery from news needs OPENAI_API_KEY to read names out "
            "of headlines; no suggestions were generated from this path."
        )
        return []

    limit = per_query or v1config.NEWS_PER_QUERY

    async def one(spec: dict) -> list:
        if say:
            say(f"    peers: {spec['role']} in {spec['industry_label']}")
        articles = await news_async.collect_one(
            transport, spec["query"], limit,
        )
        if not articles:
            return []
        size = max(1, v1config.OPENAI_EXTRACT_BATCH)
        batches = [
            (start, articles[start:start + size])
            for start in range(0, len(articles), size)
        ]
        results = await map_bounded(
            batches,
            lambda entry: _read_batch(
                client, entry[1], entry[0], spec, subject_name,
            ),
            v2config.MAX_ANALYST_CONCURRENCY,
        )
        flattened: list = []
        for chunk in results:
            flattened.extend(chunk or [])
        return flattened

    found = await map_bounded(
        specs, one, v2config.MAX_ENTITY_CONCURRENCY,
        on_error=lambda i, e: transport.note(
            f"Peer search failed for {specs[i]['query']}: {e}"
        ),
    )

    pool: dict = {}
    for chunk in found:
        for candidate in chunk or []:
            _merge(pool, candidate)
    return list(pool.values())
