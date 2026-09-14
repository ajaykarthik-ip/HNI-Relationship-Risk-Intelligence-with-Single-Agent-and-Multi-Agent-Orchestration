"""Orchestration for one subject.

The order matters and is the architecture:

    1  resolve   the typed name to one person (company used to disambiguate)
    2  discover  connected companies, from registries and extraction
    3  merge     duplicate spellings of the same company
    4  screen    per company: gather news, score sentiment, detect risk
    5  assemble  the delivered record

Step 4 is per company, not per person. The deliverable is company-level
sentiment, so company-level collection is the only way to produce it
honestly.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import config
from . import usage as usage_mod
from .cache import ResponseCache
from .collect import fulltext, queries
from .enrich import (cluster, extract, publishers, relevance, risk,
                     sentiment as sentiment_mod, tenure)
from .http import Fetcher
from .models import CompanyScreening, SentimentCounts, utc_now
from .resolve import company as company_resolve
from .resolve import person as person_resolve
from .resolve import relationships
from .sources import firecrawl as fc
from .sources import news, web, wikidata

# Roles that are not directorships. Playing for a team or fronting a brand
# belongs in the report, but not in a list a compliance reader reads as
# filings.
NON_CORPORATE_ROLE = re.compile(
    r"\b(cricketer|footballer|player|captain|athlete|sportsperson|batsman|"
    r"bowler|brand ambassador|ambassador|endorser|spokesperson|face of|"
    r"mentor|patron)\b",
    re.IGNORECASE,
)

# A mixed role such as "Shareholder/Ambassador" carries a real financial
# interest, so an ownership word outranks the endorsement word.
CORPORATE_INTEREST = re.compile(
    r"\b(shareholder|stakeholder|owner|co-owner|investor|partner|director|"
    r"founder|co-founder|promoter|chairman|chairperson|proprietor|board)\b",
    re.IGNORECASE,
)


@dataclass
class Options:
    max_news: int = 25
    max_companies: int = 12
    delay: float = 1.0
    firecrawl_key: str | None = None
    firecrawl_queries: int = 4
    firecrawl_limit: int = 8
    firecrawl_pages: int = 6
    firecrawl_extract: bool = True
    verbose: bool = True
    # Called with each progress line. Lets an API surface live progress
    # without capturing stdout.
    on_progress: object = None
    workers: int = 6
    cache_dir: str | None = ".cache"
    # The candidate the user picked in the UI, as returned by
    # resolve.candidates.discover(). When present the pipeline skips its own
    # identity guess entirely: a human has already answered that question, and
    # re-deriving it can only disagree with them.
    confirmed_entity: dict | None = None


def _stake_of(record: dict) -> float | None:
    """The equity percentage a promoted stake recorded, if any.

    `_controlling_investments` writes the stake into the role as
    "shareholder (65%)", which is where the relationship classifier reads it
    back from.
    """
    for role in record.get("relationships", []) or []:
        match = re.search(r"\((\d+(?:\.\d+)?)%\)", str(role))
        if match:
            return float(match.group(1))
    return None


def _coverage(targets: list, person_queries: list, all_evidence: list,
              promotional_total: int, fulltext_total: dict,
              extraction_total: dict) -> dict:
    """What was actually searched, counted from the run itself.

    This is what turns a LOW verdict into a statement a reader can weigh. A
    clean result that cannot say how hard it looked is an assertion.
    """
    groups = set(queries.groups_checked(person_queries))
    issued = len(person_queries)
    for record in targets:
        groups |= set(record.get("groups_checked") or [])
        issued += record.get("queries_issued", 0)

    return {
        "groups_checked": sorted(groups),
        "groups_available": sorted(queries.ADVERSE_GROUPS),
        "queries_issued": issued,
        "articles_retrieved": len(all_evidence) + promotional_total,
        "after_dedup": len(all_evidence),
        "promotional_excluded": promotional_total,
        "fulltext_attempted": fulltext_total.get("attempted", 0),
        "fulltext_fetched": fulltext_total.get("full", 0),
        "fulltext_paywalled": fulltext_total.get("paywalled", 0),
        "fulltext_blocked": fulltext_total.get("blocked", 0),
        "articles_sent_to_model": extraction_total.get("sent", 0),
        "articles_analysed_by_model": extraction_total.get("read", 0),
        "analysis_backend": extraction_total.get("backend", "keyword"),
        "publishers": len(publishers.independent_publishers(all_evidence)),
        "by_tier": publishers.tier_breakdown(all_evidence),
    }


def _say(options: Options, message: str) -> None:
    if options.verbose:
        print(message, flush=True)
    if options.on_progress is not None:
        options.on_progress(message.strip())


def _evidence(articles: list, keep: int = 8) -> list:
    """Choose which articles ship with a company row.

    Flagged articles come first. Taking the first N by collection order meant
    a company could carry four findings while none of the articles that
    produced them appeared in the output — the flag cited a URL the reader
    could not see a headline for.
    """
    flagged = [a for a in articles if a.risk_categories]
    rest = [a for a in articles if not a.risk_categories]
    return (flagged + rest)[:keep]


def _search_angles(name: str, company: str | None) -> list:
    """Search angles, broadest first.

    The role query is deliberately NOT anchored on the supplied company.
    Anchoring it narrowed the result set enough to lose the registry mirrors
    (ZaubaCorp, FalconeBiz) that carry the filed company list and the
    Director Identification Number — the single most valuable pages in a run.
    Disambiguation is already handled during identity resolution; the company
    is used here only as an extra angle, last.
    """
    angles = [
        f'"{name}" founder OR chairman OR director company',
        f'"{name}" investments OR stake OR shareholding',
        f'"{name}" net worth profile biography',
        f'"{name}" lawsuit OR fraud OR investigation OR probe',
    ]
    if company:
        angles.append(f'"{name}" "{company}" role OR stake OR leadership')
    return angles


def _split_roles(pages: list) -> tuple:
    """Extracted roles into (companies, other affiliations)."""
    companies: dict = {}
    other: dict = {}

    for page in pages:
        extracted = page.get("extracted") or {}
        if not isinstance(extracted, dict):
            continue
        for key, status in (("current_roles", "active"), ("past_roles", "former")):
            for entry in extracted.get(key) or []:
                if not isinstance(entry, dict):
                    continue
                name = (entry.get("company") or "").strip()
                if not name:
                    continue
                role = (entry.get("role") or "").strip()

                non_corporate = bool(NON_CORPORATE_ROLE.search(role)) and not (
                    CORPORATE_INTEREST.search(role)
                )
                bucket = other if non_corporate else companies

                record = bucket.setdefault(name.lower(), {
                    "name": name,
                    "relationships": [],
                    "status": status,
                    "period": entry.get("period"),
                    "sectors": [],
                    "link_basis": "Extracted from page text, not a registry record",
                    "source": "Firecrawl extraction",
                    "sources": ["Firecrawl extraction"],
                    "source_url": page["source_url"],
                })
                if role and role not in record["relationships"]:
                    record["relationships"].append(role)
                sector = (entry.get("sector") or "").strip()
                if sector and sector not in record["sectors"]:
                    record["sectors"].append(sector)

    return list(companies.values()), list(other.values())


def _investments(pages: list) -> list:
    found: dict = {}
    for page in pages:
        extracted = page.get("extracted") or {}
        if not isinstance(extracted, dict):
            continue
        for entry in extracted.get("investments") or []:
            if not isinstance(entry, dict):
                continue
            entity = (entry.get("entity") or "").strip()
            if not entity:
                continue
            # Crunchbase writes "obfuscated" where a figure is withheld.
            # Rendering that verbatim presents a placeholder as data.
            amount = (entry.get("stake_or_amount") or "").strip()
            if amount.lower() in {"obfuscated", "undisclosed", "n/a", "na",
                                  "unknown", "not disclosed", "-", "none"}:
                amount = ""
            record = found.setdefault(entity.lower(), {
                "entity": entity,
                "stake_or_amount": amount or None,
                "year": entry.get("year") or None,
                "sector": (entry.get("sector") or "").strip() or None,
                "source": "Firecrawl extraction",
                "source_url": page["source_url"],
            })
            if not record["stake_or_amount"] and amount:
                record["stake_or_amount"] = amount
                record["source_url"] = page["source_url"]
    return list(found.values())


# "65%", "61.7 per cent". A bare money figure is not a stake: knowing someone
# put a crore into a company says nothing about how much of it they hold.
STAKE_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|per\s?cent\b)", re.IGNORECASE)

# Prose that states control without printing a number.
CONTROL_LANGUAGE = re.compile(
    r"\b(majority (?:stake|shareholder|owner|holding)|controlling (?:stake|interest)|"
    r"wholly[- ]owned|whole[- ]?owned|fully owned)\b",
    re.IGNORECASE,
)


def stake_percent(text: str | None) -> float | None:
    """The equity percentage a stake description states, if it states one."""
    if not text:
        return None
    if CONTROL_LANGUAGE.search(text):
        return 100.0
    values = [float(v) for v in STAKE_PERCENT.findall(text)]
    values = [v for v in values if 0 < v <= 100]
    return max(values) if values else None


def _controlling_investments(investments: list, threshold: float) -> list:
    """Investments large enough to be control, promoted to screening targets.

    A 65% holding is the subject's exposure whether or not any page happens to
    name them a director there, so the size of the stake decides this and not
    the job title. Holdings below the threshold, and amounts quoted with no
    percentage, stay in the investment list untouched.
    """
    promoted = []
    for entry in investments:
        entity = (entry.get("entity") or "").strip()
        percent = stake_percent(entry.get("stake_or_amount"))
        if not entity or percent is None or percent < threshold:
            continue
        source = entry.get("source") or "Firecrawl extraction"
        promoted.append({
            "name": entity,
            "relationships": [f"shareholder ({percent:g}%)"],
            "status": "active",
            "sectors": [s for s in (entry.get("sector"),) if s],
            "link_basis": (
                f"Holds a {percent:g}% stake, at or above the {threshold:g}% "
                "threshold this tool treats as control"
            ),
            "source": source,
            "sources": [source],
            "source_url": entry.get("source_url"),
        })
    return promoted


def _shares_a_name(entity: str, anchors: list) -> bool:
    """True when the company carries the subject's or a known company's name.

    "Zurich Kotak General Insurance" is corroborated by the word Kotak. This
    is checked first because it costs nothing.
    """
    tokens = set(relevance.distinctive_tokens(entity))
    if not tokens:
        return False
    for anchor in anchors:
        shared = tokens & set(relevance.distinctive_tokens(anchor))
        if shared:
            return True
    return False


def _corroborated_stakes(fetcher, promoted: list, subject, org_qids: list,
                         name_anchors: list, say) -> list:
    """Keep only the stakes a structured source or a shared name confirms.

    An extraction model reads "acquired Old Mutual's 26% stake" and records
    the subject as owning Old Mutual. Screening that company then attaches
    real adverse coverage to a company they have no holding in, which is the
    worst failure this tool has. A percentage in prose is therefore a lead,
    not a fact: Wikidata has to agree that the company is controlled by the
    subject or by one of their companies.
    """
    anchors = {qid for qid in [subject.wikidata_id, *org_qids] if qid}
    kept = []

    for record in promoted:
        name = record["name"]

        if _shares_a_name(name, name_anchors):
            record["link_basis"] += "; carries the subject's own name"
            kept.append(record)
            continue

        if not anchors:
            say(f"    stake in {name} left unscreened: nothing to check it against")
            continue

        matches = [
            item for item in wikidata.search_entities(fetcher, name, limit=3)
            if not wikidata.is_human(fetcher, item["id"])
        ]
        confirmed = next(
            (
                item for item in matches
                if item["id"] in anchors
                or wikidata.controllers_of(fetcher, item["id"]) & anchors
            ),
            None,
        )

        if confirmed:
            record["registry_id"] = confirmed["id"]
            record["source_url"] = confirmed["source_url"]
            record["sources"] = sorted(set(record["sources"]) | {"Wikidata"})
            record["link_basis"] += "; ownership confirmed on Wikidata"
            kept.append(record)
        else:
            # Said out loud rather than dropped silently: the stake is still
            # listed under investments, and this explains why it has no card.
            say(f"    stake in {name} not corroborated, left as an investment")
            fetcher.note(
                f"A stake in '{name}' was claimed by a page but no structured "
                "source ties it to the subject, so it was not screened."
            )

    return kept


def _associates(pages: list) -> list:
    people: dict = {}
    for page in pages:
        extracted = page.get("extracted") or {}
        if not isinstance(extracted, dict):
            continue
        for entry in extracted.get("associates") or []:
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or "").strip()
            if not name:
                continue
            people.setdefault(name.lower(), {
                "name": name,
                "role": entry.get("role"),
                "company": entry.get("company"),
                "relationship": entry.get("relationship") or "named alongside the subject",
                "via": "page extraction",
                "source": "Firecrawl extraction",
                "source_url": page["source_url"],
            })
    return list(people.values())


def discover_companies(fetcher, subject, company, options, say=lambda m: None):
    """Find every company connected to the subject, from all available sources.

    Shared by both problem statements. Screening and network mapping ask the
    same question — "which companies is this person connected to?" — and must
    not answer it differently. PS2 previously asked Wikidata alone and found
    nothing for subjects whose companies are not in it, while PS1 found ten.
    """
    discovered: list = []
    claims: dict = {}
    connections: list = []
    firecrawl_block: dict = {}
    other_affiliations: list = []
    investments: list = []
    resolved = subject.resolved_name or subject.query_name


    if subject.wikidata_id:
        say("  Wikidata claims and company links ...")
        claims = wikidata.claims(fetcher, subject.wikidata_id)
        for record in wikidata.companies_for(fetcher, subject.wikidata_id):
            discovered.append({
                "name": record["name"],
                "relationships": record["relationships"],
                "jurisdiction": record.get("country"),
                "registry_id": record.get("wikidata_id"),
                "industries": record.get("industries", []),
                # Statement qualifiers P580/P582. Where present these are a
                # better tenure source than any phrase a page prints, and they
                # are what lets an article predating the role be excluded.
                "role_start": record.get("role_start"),
                "role_end": record.get("role_end"),
                "status": "former" if record.get("role_end") else "active",
                "link_basis": "Named on the organisation's Wikidata entity",
                "source": record["source"],
                "sources": [record["source"]],
                "source_url": record["source_url"],
            })

        org_qids = [c["registry_id"] for c in discovered if c.get("registry_id")]
        say("  Co-officer connections ...")
        connections = wikidata.coofficers(fetcher, subject.wikidata_id, org_qids)

        for label in ("spouse", "father", "mother", "sibling", "relative", "member of"):
            for entry in claims.get(label, []):
                connections.append({
                    "name": entry["value"], "relationship": label,
                    "via": "Wikidata claim", "source": entry["source"],
                    "source_url": entry["source_url"],
                })

    if options.firecrawl_key:
        say("  Firecrawl: search and extraction ...")
        client = fc.Firecrawl(options.firecrawl_key, fetcher)

        results = []
        for query in _search_angles(resolved, company)[: options.firecrawl_queries]:
            say(f"    search: {query[:58]}")
            results.extend(client.search(query, limit=options.firecrawl_limit))

        seen, unique = set(), []
        for item in results:
            if item["source_url"] not in seen:
                seen.add(item["source_url"])
                unique.append(item)

        ranked, skipped = fc.rank_for_scraping(unique)
        if skipped:
            say(f"    skipping {len(skipped)} login-walled result(s), no credit spent")

        pages = []
        for item in ranked[: options.firecrawl_pages]:
            say(f"    read:   {item['source_url'][:62]}")
            page = client.scrape(item["source_url"], resolved, structured=options.firecrawl_extract)
            if page:
                page["found_via"] = item["query"]
                pages.append(page)

        extracted_companies, other_affiliations = _split_roles(pages)
        discovered.extend(extracted_companies)
        investments = _investments(pages)
        # A controlling shareholding is a connected company by any reading of
        # the brief, so it is screened rather than listed and left alone --
        # but only once something other than the page itself confirms it.
        promoted = _controlling_investments(investments, config.CONTROL_STAKE_PERCENT)
        if promoted:
            discovered.extend(_corroborated_stakes(
                fetcher, promoted, subject,
                [c["registry_id"] for c in discovered if c.get("registry_id")],
                [resolved] + [c["name"] for c in discovered],
                say,
            ))
        connections.extend(_associates(pages))

        firecrawl_block = {
            "api_version": client.version,
            "search_results": unique,
            "pages": pages,
            "skipped_domains": [i["source_url"] for i in skipped],
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


    return {
        "discovered": discovered,
        "claims": claims,
        "connections": connections,
        "firecrawl": firecrawl_block,
        "other_affiliations": other_affiliations,
        "investments": investments,
    }


SCREENING_PRIORITY = {"control": 0, "unknown": 1, "employment": 2,
                      "public_office": 3, "philanthropy": 4}


def _screening_priority(record: dict) -> int:
    """Rank for truncation: control first, philanthropy last."""
    name = record.get("canonical_name") or record.get("name") or ""
    kind = company_resolve.relationship_type(name, record.get("relationships", []))
    rank = SCREENING_PRIORITY.get(kind, 1)
    # A company he left is weaker evidence than one he still holds.
    return rank * 2 + (1 if record.get("status") == "former" else 0)


def run(name: str, company: str | None, options: Options) -> dict:
    fetcher = Fetcher(
        delay=options.delay,
        cache=ResponseCache(options.cache_dir, enabled=options.cache_dir is not None),
    )
    # Settled once, before any billable call, so every credit in this run is
    # converted at the same rate.
    rate, rate_source = usage_mod.resolve_inr_rate(fetcher)
    fetcher.meter.usd_to_inr = rate
    fetcher.meter.rate_source = rate_source
    analyzer = sentiment_mod.SentimentAnalyzer()

    # -- 1. resolve ---------------------------------------------------------
    _say(options, "  Resolving identity ...")
    subject, candidates, bio = person_resolve.resolve(
        fetcher, name, company, confirmed=options.confirmed_entity
    )
    resolved = subject.resolved_name or name

    # -- 2. discover (shared with the network pipeline) --------------------
    found = discover_companies(fetcher, subject, company, options,
                               say=lambda m: _say(options, m))
    discovered = found["discovered"]
    claims = found["claims"]
    connections = found["connections"]
    firecrawl_block = found["firecrawl"]
    other_affiliations = found["other_affiliations"]
    investments = found["investments"]

    # -- 3. merge -----------------------------------------------------------
    merged = company_resolve.merge(discovered, resolved)
    _say(options, f"  {len(discovered)} company records merged to {len(merged)} entities")

    # The supplied company is meant to corroborate the identity. If it never
    # turns up among the discovered companies it corroborated nothing, and
    # saying it "was used to steer the search" at 95% confidence overstates
    # the match. Entering an unrelated company must lower confidence, not
    # leave it untouched.
    if company:
        wanted = company_resolve.normalise(company, resolved)
        seen = any(
            company_resolve.normalise(c.get("canonical_name") or c["name"], resolved) == wanted
            for c in merged
            if c.get("source") != "Query input"
        )
        if not seen:
            subject.match_confidence = round(min(subject.match_confidence, 0.55), 2)
            subject.match_basis += (
                f" The supplied company '{company}' was NOT found among the "
                "connected companies, so it did not corroborate this match. "
                "Confidence is reduced: check that the name and company belong "
                "together."
            )
            fetcher.note(
                f"'{company}' was not found among {resolved}'s connected "
                "companies; identity confidence reduced."
            )

    # -- 4. screen each company --------------------------------------------
    # Each company is independent, so they are screened in parallel. The rate
    # limiter is per host, which keeps politeness intact while unrelated
    # services proceed at the same time.
    # --max-companies truncates, so the order decides what is dropped. A
    # controlling interest must never be cut in favour of a former employer.
    merged.sort(key=_screening_priority)
    targets = merged[: options.max_companies]

    # Classify every relationship before any evidence is judged. Attribution
    # decides whether a company's conduct is the subject's exposure, and a
    # finding built before that is known cannot be placed.
    for record in targets:
        name = record.get("canonical_name") or record["name"]
        relationship_class, basis = relationships.classify(
            name, record.get("relationships", []), record.get("status", "active"),
            _stake_of(record),
        )
        record["relationship_class"] = relationship_class
        record["relationship_basis"] = basis
    relationships.resolve_unknowns(fetcher, targets, resolved,
                                   say=lambda m: _say(options, m))

    owner_domains = publishers.owner_domains_for(
        resolved, [r.get("canonical_name") or r["name"] for r in targets]
    )

    # One full-text budget for the whole run, shared out as it is spent. A
    # per-company cap would let the first company exhaust the request budget
    # on routine coverage while a later one with a real matter gets nothing.
    fulltext_remaining = [config.MAX_FULLTEXT_FETCHES]

    def fulltext_budget() -> int:
        return max(0, fulltext_remaining[0])

    def spend_fulltext(tally: dict) -> None:
        fulltext_remaining[0] -= tally.get("attempted", 0)

    def collect_for(record: dict) -> list:
        """Fetch and filter one company's news. Network-bound, so parallel."""
        company_name = record.get("canonical_name") or record["name"]
        _say(options, f"  Screening: {company_name}")

        # The adverse ladder, not one generic query. Asking only the company's
        # name returns what the company publishes; the EOW complaint against a
        # BharatPe founder was never fetched because nothing ever asked for it.
        query_set = queries.for_company(company_name)
        collected = news.collect_many(
            fetcher, query_set, config.NEWS_PER_QUERY, about=company_name,
            say=lambda m: _say(options, m),
        )
        record["queries_issued"] = len(query_set)
        record["groups_checked"] = queries.groups_checked(query_set)

        # Drop anything that is not actually about this company before it can
        # influence sentiment or raise a flag.
        # A name like "More" matches almost any headline, so aliases, sector
        # and the subject's own name have to corroborate it before an article
        # counts as this company's coverage.
        articles, dropped = relevance.filter_for_company(
            collected,
            company_name,
            corroborators=relevance.corroborating_tokens(
                record.get("aliases"), record.get("sectors"), [resolved]
            ),
        )
        if dropped:
            _say(options, f"    dropped {dropped} article(s) not about {company_name}")

        # Provenance before scoring. A press release is not evidence about the
        # company that issued it, and counting it is how one Adani entity
        # reached 26 positive articles and zero negative ones.
        publishers.annotate(articles, owner_domains)
        evidence, promotional = publishers.split_promotional(articles)
        record["promotional_excluded"] = len(promotional)
        if promotional:
            _say(options, f"    excluded {len(promotional)} promotional item(s)")
        return evidence

    def screen(record: dict, articles: list) -> tuple:
        """Analyse one company's evidence. Returns (screening, findings)."""
        company_name = record.get("canonical_name") or record["name"]

        analyzer.annotate(articles)
        risk.annotate(articles)

        # A story from before the subject arrived is the company's history,
        # not their exposure. IL&FS's fraud predated the chairman appointed
        # to clean it up, and was reported as his.
        outside = tenure.mark_out_of_tenure(articles, record)
        if outside:
            _say(options, f"    {outside} article(s) fall outside the subject's tenure at {company_name}")

        # Read the articles that look material. A headline says a matter
        # exists; the body says what stage it reached, when, and who was named.
        fetched = fulltext.fetch(fetcher, articles, cap=fulltext_budget(),
                                 say=lambda m: _say(options, m))
        spend_fulltext(fetched)
        record["fulltext"] = fetched

        # Then have the evidence read rather than pattern-matched.
        record["extraction"] = extract.analyse(
            fetcher, articles, company_name, resolved,
            role_period=tenure.role_period(record),
            say=lambda m: _say(options, m),
        )
        articles = extract.keep_relevant(articles)

        # Tone is computed on everything the company's coverage contains;
        # findings only on what can be attributed to the person.
        counts, classification = sentiment_mod.aggregate(articles)
        findings = cluster.build_findings(
            fetcher, articles, company_name,
            entity_role="; ".join(record.get("relationships", [])[:3]),
            relationship_type=record.get("relationship_class", "other"),
            say=lambda m: _say(options, m),
        )
        # The old flag shape is still emitted so the current dashboard keeps
        # working while the new one is built. It is dropped in one change once
        # the frontend consumes findings.
        flags = risk.build_flags(tenure.attributable_articles(articles))

        is_registry = "wikidata" in (record.get("source") or "").lower()
        return CompanyScreening(
            name=company_name,
            canonical_name=company_name,
            relationships=record.get("relationships", []),
            status=record.get("status", "active"),
            jurisdiction=record.get("jurisdiction"),
            registry_id=record.get("registry_id"),
            link_confidence=0.9 if is_registry else 0.6,
            link_basis=record.get("link_basis", ""),
            sources=record.get("sources", [record.get("source")] if record.get("source") else []),
            source_url=record.get("source_url"),
            aliases=record.get("aliases", []),
            relationship_type=relationships.legacy_type(
                record.get("relationship_class", "other")
            ),
            relationship_class=record.get("relationship_class", "other"),
            relationship_basis=record.get("relationship_basis", ""),
            role_start=tenure.role_period(record)[0],
            role_end=tenure.role_period(record)[1],
            sentiment=SentimentCounts(**counts),
            classification=classification,
            articles_reviewed=len(articles),
            insufficient_coverage=len(articles) == 0,
            negative_news_flag=bool(flags),
            flags=flags,
            articles=_evidence(articles),
        ), findings

    # Fetching is network-bound and parallel; assignment and analysis are not.
    if options.workers > 1 and len(targets) > 1:
        with ThreadPoolExecutor(max_workers=options.workers) as pool:
            collected = list(pool.map(collect_for, targets))
    else:
        collected = [collect_for(record) for record in targets]

    # One story, one company. A group's flagship article comes back from the
    # query for every subsidiary, and counting it once per entity turned a
    # single fact into six and skewed every ratio computed from it.
    #
    # Assignment runs single-threaded over `targets`, which is already sorted
    # deterministically, so the same run always gives the same article to the
    # same company. Doing this inside the thread pool would hand it to
    # whichever worker finished first.
    seen_articles: set = set()
    assigned = []
    for record, articles in zip(targets, collected):
        kept = news.dedupe(articles, seen=seen_articles)
        shared = len(articles) - len(kept)
        if shared:
            name = record.get("canonical_name") or record["name"]
            _say(options, f"    {shared} article(s) already counted for another company, not double-counted for {name}")
        assigned.append(kept)

    # Analysis runs in order rather than in a pool: full-text fetching and
    # extraction are budgeted across the whole run, and a shared budget spent
    # by whichever worker got there first would make runs non-reproducible.
    screenings, findings = [], []
    all_evidence: list = []
    for record, articles in zip(targets, assigned):
        screening, company_findings = screen(record, articles)
        screenings.append(screening)
        findings += company_findings
        all_evidence += articles

    # -- person-level coverage ---------------------------------------------
    # The adverse ladder runs against the individual too. A matter naming the
    # person is theirs wherever it happened, and is the one thing a company
    # query can miss entirely.
    _say(options, "  Screening the individual ...")
    person_queries = queries.for_person(resolved, company)
    person_articles = news.collect_many(
        fetcher, person_queries, config.NEWS_PER_QUERY,
        say=lambda m: _say(options, m),
    )
    person_articles = news.dedupe(person_articles, seen=seen_articles)
    publishers.annotate(person_articles, owner_domains)
    person_articles, person_promo = publishers.split_promotional(person_articles)

    analyzer.annotate(person_articles)
    risk.annotate(person_articles)
    person_fulltext = fulltext.fetch(fetcher, person_articles,
                                     cap=fulltext_budget(),
                                     say=lambda m: _say(options, m))
    person_extraction = extract.analyse(
        fetcher, person_articles, resolved, resolved,
        say=lambda m: _say(options, m),
    )
    person_articles = extract.keep_relevant(person_articles)
    person_counts, person_class = sentiment_mod.aggregate(person_articles)

    # Being named in an article is not being accused in it. "Grover claimed
    # Koladiya committed data theft" and "Grover criticised a tax notice" were
    # both scored as adverse findings about him; he is the accuser in one and
    # a commentator in the other.
    #
    # So the person's findings are attributed by what the extraction says his
    # role in the event was -- not by the fact that the query carried his name.
    person_findings = cluster.build_findings(
        fetcher, person_articles, resolved, entity_role="the individual",
        relationship_type="person", say=lambda m: _say(options, m),
    )
    if person_extraction.get("read", 0) == 0:
        # No model read these, so there is no role to go on. Fall back to
        # attributing them: under-reporting a matter about the subject is the
        # worse error, and the row is labelled as keyword-derived.
        for finding in person_findings:
            finding.attributed_to_subject = True
    findings = person_findings + findings

    all_evidence += person_articles
    promotional_total = len(person_promo) + sum(
        r.get("promotional_excluded", 0) for r in targets
    )
    fulltext_total = dict(person_fulltext)
    extraction_total = dict(person_extraction)
    for record in targets:
        for key, value in (record.get("fulltext") or {}).items():
            fulltext_total[key] = fulltext_total.get(key, 0) + value
        for key in ("sent", "read"):
            extraction_total[key] = (
                extraction_total.get(key, 0)
                + (record.get("extraction") or {}).get(key, 0)
            )
        backend = (record.get("extraction") or {}).get("backend")
        if backend and backend != "keyword":
            extraction_total["backend"] = backend

    coverage = _coverage(targets, person_queries, all_evidence,
                         promotional_total, fulltext_total, extraction_total)

    # -- 5. assemble --------------------------------------------------------
    return {
        "findings": findings,
        "coverage": coverage,
        # Handed to the report builder so the final review can run through the
        # same rate-limited, cached, metered transport as everything else.
        "fetcher": fetcher,
        "subject": subject,
        "candidates": candidates,
        "biography": bio,
        "wikidata_claims": claims,
        "screenings": screenings,
        "person_coverage": {
            "sentiment": person_counts,
            "classification": person_class,
            "articles": person_articles,
        },
        "connections": connections,
        "investments": investments,
        "other_affiliations": other_affiliations,
        "duckduckgo": web.duckduckgo_instant(fetcher, resolved),
        "firecrawl": firecrawl_block,
        "sentiment_backend": analyzer.backend,
        "notes": fetcher.notes,
        "cache": fetcher.cache.summary,
        # Counted at the call site, multiplied here. No estimate anywhere.
        "usage": fetcher.meter.summary(),
        "collected_at": utc_now(),
    }
