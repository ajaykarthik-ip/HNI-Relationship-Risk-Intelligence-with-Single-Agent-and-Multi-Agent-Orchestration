"""Find the subject's current network, and generate global candidates.

Data sources, and why these:

  Wikidata Query Service   the only free, global, structured source of
                           person-to-company officer relationships. Supplies
                           both the existing network (co-officers) and the
                           candidate pool (same role, same industry).
  Firecrawl extraction     optional. Names associates that appear on the same
                           page as the subject, which reaches private-company
                           relationships Wikidata never records.
  LinkedIn                 deliberately NOT used. Its terms forbid automated
                           collection and there is no public people-search
                           API. Any design claiming otherwise does not ship.
  Crunchbase               reachable only through search results here; its own
                           API requires a paid key.
"""

from __future__ import annotations

from .. import config
from ..sources import wikidata

# Officer roles used both to describe the subject and to find peers.
ROLE_PROPERTIES = {
    "P112": "founder",
    "P169": "chief executive officer",
    "P488": "chairperson",
    "P1037": "director / manager",
    "P3320": "board member",
}


def subject_profile(companies: list) -> dict:
    """What the subject looks like, as the traits a peer is matched on."""
    roles, industries, industry_qids, countries = set(), set(), [], set()

    for company in companies:
        for relation in company.get("relationships", []):
            roles.add(relation)
        for industry in company.get("industries", []):
            industries.add(industry)
        for qid in company.get("industry_qids", []):
            if qid not in industry_qids:
                industry_qids.append(qid)
        # Wikidata records carry "country"; discovery records carry
        # "jurisdiction". Both mean the same thing here.
        country = company.get("country") or company.get("jurisdiction")
        if country:
            countries.add(country)

    return {
        "roles": sorted(roles),
        "industries": sorted(industries),
        "industry_qids": industry_qids,
        "countries": sorted(countries),
        "company_names": [c.get("name") for c in companies if c.get("name")],
    }


def current_network(fetcher, qid: str, company_qids: list, claims: dict,
                    associates: list | None = None) -> list:
    """People already tied to the subject: co-officers, family, associates."""
    network = []

    for person in wikidata.coofficers(fetcher, qid, company_qids):
        network.append({
            "name": person["name"],
            "wikidata_id": person.get("wikidata_id"),
            "role": person.get("role"),
            "company": person.get("via"),
            "tie": person["relationship"],
            "tie_type": "co-officer",
            "source": person["source"],
            "source_url": person["source_url"],
        })

    for label in ("spouse", "father", "mother", "sibling", "relative", "member of"):
        for entry in claims.get(label, []):
            network.append({
                "name": entry["value"],
                "wikidata_id": None,
                "role": None,
                "company": None,
                "tie": label,
                "tie_type": "personal" if label != "member of" else "membership",
                "source": entry["source"],
                "source_url": entry["source_url"],
            })

    for person in associates or []:
        network.append({
            "name": person["name"],
            "wikidata_id": None,
            "role": person.get("role"),
            "company": person.get("company"),
            "tie": person.get("relationship") or "named alongside the subject",
            "tie_type": "associate",
            "source": person["source"],
            "source_url": person["source_url"],
        })

    # One row per person, keeping the first (most structured) tie.
    seen, unique = set(), []
    for person in network:
        key = (person["name"] or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(person)
    return unique


# Words that describe a legal form or a generic corporate function, not a line
# of business. Extraction picks them up from company names — "foundation"
# from SEVVA PATH SANKALP FOUNDATION, "management" from NOMAD MANAGEMENT LLP —
# and each resolves to a real Wikidata item, which then returns peers with
# nothing in common with the subject. On one run this produced five Danish
# pharmaceutical-foundation executives as suggested connections for an Indian
# cricketer.
GENERIC_SECTORS = {
    "foundation", "trust", "charity", "charitable trust", "ngo", "non-profit",
    "nonprofit", "management", "holdings", "holding", "holding company",
    "group", "company", "conglomerate", "enterprises", "ventures", "venture",
    "partnership", "llp", "private limited", "limited", "business",
    "services", "consulting", "corporate", "investment", "investments",
    "family office", "other", "various", "diversified",
}


def is_generic_sector(sector: str) -> bool:
    return (sector or "").lower().strip() in GENERIC_SECTORS


def resolve_sector(fetcher, sector: str, cache: dict) -> str | None:
    """Map a sector phrase from a web page to a Wikidata industry item.

    "restaurant chain" and "fitness" are what a news page says; candidate
    generation needs the Q-number Wikidata uses in P452. Search alone is not
    enough — it happily returns a film or a band for the same word — so each
    candidate is verified to be a value some company actually declares as its
    industry before it is used.
    """
    key = sector.lower().strip()
    if key in cache:
        return cache[key]

    cache[key] = None
    for candidate in wikidata.search_entities(fetcher, sector, limit=3):
        qid = candidate["id"]
        rows = wikidata.sparql(
            fetcher, f"SELECT ?org WHERE {{ ?org wdt:P452 wd:{qid} . }} LIMIT 1"
        )
        if rows:
            cache[key] = qid
            return qid
    return None


def industries_from_sectors(fetcher, companies: list, say=None) -> tuple:
    """Derive industry Q-numbers from the sectors named on the source pages.

    This is what lets a subject whose companies are absent from Wikidata still
    be matched against real business peers rather than falling back to people
    who merely share their occupation.
    """
    sectors, rejected = [], []
    for company in companies:
        for sector in company.get("sectors", []) or []:
            if not sector or sector in sectors:
                continue
            if is_generic_sector(sector):
                rejected.append(sector)
                continue
            sectors.append(sector)

    if rejected and say:
        say(f"    ignoring generic sector(s): {', '.join(sorted(set(rejected)))}")

    qids, labels, cache = [], {}, {}
    for sector in sectors[:8]:
        qid = resolve_sector(fetcher, sector, cache)
        if say:
            say(f"    sector {sector!r} -> {qid or 'no Wikidata industry'}")
        if qid and qid not in qids:
            qids.append(qid)

    if qids:
        labels = wikidata.resolve_labels(fetcher, qids)
    return qids, labels, sectors


def industries_for_companies(fetcher, companies: list, say=None) -> tuple:
    """Harvest industry Q-numbers, resolving company names where needed.

    Companies found by extraction carry no Wikidata id, so their industry is
    unknown until the name is looked up. Many private companies are simply not
    in Wikidata at all, which is why the occupation fallback below exists.
    """
    qids, labels, resolved_count = [], {}, 0

    for company in companies:
        for qid, label in zip(company.get("industry_qids", []), company.get("industries", [])):
            if qid not in qids:
                qids.append(qid)
                labels[qid] = label

    if qids:
        return qids, labels, resolved_count

    # Nothing structured yet: try to resolve the discovered names.
    for company in companies[:8]:
        name = company.get("canonical_name") or company.get("name")
        if not name:
            continue
        hits = wikidata.search_entities(fetcher, name, limit=1)
        if not hits:
            continue
        resolved_count += 1
        claims = wikidata.entity(fetcher, hits[0]["id"]).get("claims", {})
        for claim in claims.get("P452", []):
            qid = claim["mainsnak"].get("datavalue", {}).get("value", {}).get("id")
            if qid and qid not in qids:
                qids.append(qid)
        if say:
            say(f"    resolved {name} -> {hits[0]['id']}")

    if qids:
        labels.update(wikidata.resolve_labels(fetcher, qids))
    return qids, labels, resolved_count


def candidates_by_occupation(fetcher, subject_qid: str, occupation_qid: str,
                             country_qid: str | None, limit: int = 40) -> list:
    """Fallback pool: people sharing the subject's occupation.

    Used when no industry can be established — typically a subject whose
    companies are private and absent from Wikidata. Ordered by prominence and
    narrowed by country, because an unfiltered occupation query returns
    thousands of rows and times out.
    """
    country_clause = (
        f"?person wdt:P27 wd:{country_qid} ." if country_qid else ""
    )
    query = f"""
SELECT ?person ?personLabel ?countryLabel ?sitelinks WHERE {{
  ?person wdt:P106 wd:{occupation_qid} .
  ?person wdt:P31 wd:Q5 .
  {country_clause}
  FILTER(?person != wd:{subject_qid})
  FILTER NOT EXISTS {{ ?person wdt:P570 ?dateOfDeath . }}
  OPTIONAL {{ ?person wdt:P27 ?country . }}
  ?person wikibase:sitelinks ?sitelinks .
  FILTER(?sitelinks > 5)
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
ORDER BY DESC(?sitelinks)
LIMIT {limit}
"""
    found = []
    for row in wikidata.sparql(fetcher, query):
        person_qid = row.get("person", {}).get("value", "").rsplit("/", 1)[-1]
        if not person_qid:
            continue
        found.append({
            "name": row.get("personLabel", {}).get("value"),
            "wikidata_id": person_qid,
            "roles": [],
            "companies": [],
            "matched_industries": [],
            "country": row.get("countryLabel", {}).get("value"),
            "sitelinks": int(row.get("sitelinks", {}).get("value", 0) or 0),
            "basis": "shares the subject's occupation",
            "source": "Wikidata Query Service",
            "source_url": f"{config.WIKIDATA_ITEM}{person_qid}",
        })
    return found


def candidates_by_industry(fetcher, subject_qid: str, industry_qid: str,
                           prop: str, role_label: str, limit: int = 60) -> list:
    """People holding one officer role at companies in one industry.

    Excludes the dead. An earlier version returned Andrew Carnegie as a
    suggested connection because he founded an endowment the subject sits on;
    a suggestion list must contain people who can actually take a meeting.
    """
    query = f"""
SELECT DISTINCT ?person ?personLabel ?org ?orgLabel ?countryLabel ?sitelinks WHERE {{
  ?org wdt:P452 wd:{industry_qid} .
  ?org wdt:{prop} ?person .
  ?person wdt:P31 wd:Q5 .
  FILTER(?person != wd:{subject_qid})
  FILTER NOT EXISTS {{ ?person wdt:P570 ?dateOfDeath . }}
  OPTIONAL {{ ?org wdt:P17 ?country . }}
  OPTIONAL {{ ?person wikibase:sitelinks ?sitelinks . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT {limit}
"""
    found = []
    for row in wikidata.sparql(fetcher, query):
        person_qid = row.get("person", {}).get("value", "").rsplit("/", 1)[-1]
        if not person_qid:
            continue
        found.append({
            "name": row.get("personLabel", {}).get("value"),
            "wikidata_id": person_qid,
            "role": role_label,
            "company": row.get("orgLabel", {}).get("value"),
            "company_qid": row.get("org", {}).get("value", "").rsplit("/", 1)[-1],
            "country": row.get("countryLabel", {}).get("value"),
            "sitelinks": int(row.get("sitelinks", {}).get("value", 0) or 0),
            "matched_industry_qid": industry_qid,
            "source": "Wikidata Query Service",
            "source_url": f"{config.WIKIDATA_ITEM}{person_qid}",
        })
    return found


def generate(fetcher, subject_qid: str, profile: dict, industries: int = 3,
             roles: int = 3, say=None) -> list:
    """Build the candidate pool across the subject's top industries and roles."""
    industry_qids = profile["industry_qids"][:industries]
    if not industry_qids:
        return []

    # Match the subject's own roles where possible, so a chairman is compared
    # against chairs rather than against anyone in the sector.
    subject_roles = {r.lower() for r in profile["roles"]}
    ordered = sorted(
        ROLE_PROPERTIES.items(),
        key=lambda kv: (kv[1].lower() not in subject_roles,),
    )[:roles]

    pool: dict = {}
    for industry_qid in industry_qids:
        for prop, role_label in ordered:
            if say:
                say(f"    peers: {role_label} in industry {industry_qid}")
            for person in candidates_by_industry(
                fetcher, subject_qid, industry_qid, prop, role_label
            ):
                key = person["wikidata_id"]
                existing = pool.get(key)
                if existing is None:
                    person["roles"] = [person.pop("role")]
                    person["companies"] = [person.pop("company")]
                    person["matched_industries"] = [person.pop("matched_industry_qid")]
                    pool[key] = person
                    continue
                if person["role"] not in existing["roles"]:
                    existing["roles"].append(person["role"])
                if person["company"] not in existing["companies"]:
                    existing["companies"].append(person["company"])
                if person["matched_industry_qid"] not in existing["matched_industries"]:
                    existing["matched_industries"].append(person["matched_industry_qid"])

    return list(pool.values())
