"""Discovery Agent — which organisations is this person connected to?

Shared by both problem statements, exactly as V1's `discover_companies` is:
screening and network mapping ask the same question and must not answer it
differently.

Three branches feed it, and in V1 they run one after another even though only
one of them depends on anything the others produce:

    wikidata    claims and companies_for concurrently, then coofficers, which
                needs the organisation QIDs the company lookup returned
    firecrawl   four searches at once, then the ranked pages at once
    seed        the company supplied with the query, free and instant

Here the wikidata and firecrawl branches run together and the assembly happens
afterwards **in V1's order** -- registry companies first, then extracted ones,
then corroborated stakes, then the seed. That order decides which spelling of a
name wins in `company_resolve.merge`, so it is not an implementation detail.

    in    Subject, optional company
    out   the same dict V1's discover_companies returns
    LLM   none of its own; Firecrawl's structured extraction is a source
          feature, billed in credits
    fails either branch can fail without the other; a run with no Firecrawl key
          takes the Wikidata-plus-seed path exactly as V1 does
"""

from __future__ import annotations

from affluense import config as v1config
from affluense.pipeline import (_associates, _controlling_investments,
                                _corroborated_stakes, _investments,
                                _search_angles, _split_roles)
from affluense.resolve import company as company_resolve
from affluense.sources import wikidata

from ..concurrency import gather_ordered
from ..quality import entities as quality_entities
from ..sources.firecrawl_async import AsyncFirecrawl, rank_for_scraping

# Wikidata claim labels that name a person connected to the subject.
RELATION_LABELS = ("spouse", "father", "mother", "sibling", "relative",
                   "member of")


async def _wikidata_branch(bridge, subject) -> dict:
    """Claims, company links and co-officers for one Wikidata entity."""
    if not subject.wikidata_id:
        return {"claims": {}, "companies": [], "coofficers": []}

    # Independent of each other, so both at once. coofficers is not: it needs
    # the organisation QIDs that companies_for returns.
    claims, companies = await gather_ordered([
        bridge.run(wikidata.claims, subject.wikidata_id),
        bridge.run(wikidata.companies_for, subject.wikidata_id),
    ])
    claims = claims or {}
    companies = companies or []

    org_qids = [c.get("wikidata_id") for c in companies if c.get("wikidata_id")]
    coofficers = await bridge.run(
        wikidata.coofficers, subject.wikidata_id, org_qids,
    )
    return {
        "claims": claims,
        "companies": companies,
        "coofficers": coofficers or [],
    }


async def _firecrawl_branch(transport, reporter, resolved: str,
                            company: str | None, options) -> dict:
    """Search angles and the pages worth paying for, concurrently."""
    if not options.firecrawl_key:
        return {"pages": [], "unique": [], "skipped": [], "version": None,
                "ran": False}

    reporter.say("  Firecrawl: search and extraction ...")
    client = AsyncFirecrawl(options.firecrawl_key, transport)

    angles = _search_angles(resolved, company)[: options.firecrawl_queries]
    results = await client.search_many(
        angles, limit=options.firecrawl_limit, say=reporter.say,
    )

    seen, unique = set(), []
    for item in results:
        if item["source_url"] not in seen:
            seen.add(item["source_url"])
            unique.append(item)

    ranked, skipped = rank_for_scraping(unique)
    if skipped:
        reporter.say(
            f"    skipping {len(skipped)} login-walled result(s), "
            "no credit spent"
        )

    pages = await client.scrape_many(
        ranked[: options.firecrawl_pages], resolved,
        structured=options.firecrawl_extract, say=reporter.say,
    )
    return {"pages": pages, "unique": unique, "skipped": skipped,
            "version": client.version, "ran": True}


async def discover(transport, bridge, reporter, subject, company: str | None,
                   options) -> dict:
    """The same contract as `affluense.pipeline.discover_companies`."""
    reporter.stage("discovery", total=2)
    resolved = subject.resolved_name or subject.query_name

    if subject.wikidata_id:
        reporter.say("  Wikidata claims and company links ...")
    if options.firecrawl_key:
        reporter.say("  Co-officer connections and page extraction ...")

    wikidata_result, firecrawl_result = await gather_ordered(
        [
            _wikidata_branch(bridge, subject),
            _firecrawl_branch(transport, reporter, resolved, company, options),
        ],
        on_error=lambda i, e: transport.note(
            f"{'Wikidata' if i == 0 else 'Firecrawl'} discovery failed: {e}. "
            "The other sources still ran."
        ),
    )
    wikidata_result = wikidata_result or {"claims": {}, "companies": [],
                                          "coofficers": []}
    firecrawl_result = firecrawl_result or {"pages": [], "unique": [],
                                            "skipped": [], "version": None,
                                            "ran": False}
    reporter.advance("discovery")

    # -- assembly, in V1's order -------------------------------------------
    discovered: list = []
    connections: list = []
    claims = wikidata_result["claims"]

    for record in wikidata_result["companies"]:
        discovered.append({
            "name": record["name"],
            "relationships": record["relationships"],
            "jurisdiction": record.get("country"),
            "registry_id": record.get("wikidata_id"),
            "industries": record.get("industries", []),
            # Statement qualifiers P580/P582. Where present these are a better
            # tenure source than any phrase a page prints, and they are what
            # lets an article predating the role be excluded.
            "role_start": record.get("role_start"),
            "role_end": record.get("role_end"),
            "status": "former" if record.get("role_end") else "active",
            "link_basis": "Named on the organisation's Wikidata entity",
            "source": record["source"],
            "sources": [record["source"]],
            "source_url": record["source_url"],
        })

    connections.extend(wikidata_result["coofficers"])
    for label in RELATION_LABELS:
        for entry in claims.get(label, []):
            connections.append({
                "name": entry["value"], "relationship": label,
                "via": "Wikidata claim", "source": entry["source"],
                "source_url": entry["source_url"],
            })

    other_affiliations: list = []
    investments: list = []
    firecrawl_block: dict = {}

    if firecrawl_result["ran"]:
        pages = firecrawl_result["pages"]
        extracted_companies, other_affiliations = _split_roles(pages)
        # A sector is not a company. `_split_roles` accepts whatever string the
        # extraction put in `company`, so an industry name would otherwise be
        # screened as an entity and fifteen adverse queries would run against a
        # whole field of activity. Rejected names are kept as affiliations
        # rather than dropped.
        extracted_companies, not_companies = quality_entities.partition(
            extracted_companies, say=reporter.say,
        )
        other_affiliations = list(other_affiliations) + not_companies
        discovered.extend(extracted_companies)
        investments = _investments(pages)

        # A controlling shareholding is a connected company by any reading of
        # the brief, so it is screened rather than listed and left alone -- but
        # only once something other than the page itself confirms it.
        promoted = _controlling_investments(
            investments, v1config.CONTROL_STAKE_PERCENT,
        )
        if promoted:
            corroborated = await bridge.run(
                _corroborated_stakes,
                promoted,
                subject,
                [c["registry_id"] for c in discovered if c.get("registry_id")],
                [resolved] + [c["name"] for c in discovered],
                reporter.say,
            )
            discovered.extend(corroborated or [])
        connections.extend(_associates(pages))

        firecrawl_block = {
            "api_version": firecrawl_result["version"],
            "search_results": firecrawl_result["unique"],
            "pages": pages,
            "skipped_domains": [i["source_url"]
                                for i in firecrawl_result["skipped"]],
        }

    # The company supplied with the query is part of the answer by definition.
    if company and not any(
        company_resolve.normalise(c["name"], resolved)
        == company_resolve.normalise(company, resolved)
        for c in discovered
    ):
        discovered.append({
            "name": company,
            "relationships": ["supplied with the query"],
            "status": "active",
            "link_basis": "Provided as the seed company in the request",
            "source": "Query input",
            "sources": ["Query input"],
            "source_url": None,
        })

    reporter.advance("discovery")
    reporter.finish("discovery")

    return {
        "discovered": discovered,
        "claims": claims,
        "connections": connections,
        "firecrawl": firecrawl_block,
        "other_affiliations": other_affiliations,
        "investments": investments,
    }
