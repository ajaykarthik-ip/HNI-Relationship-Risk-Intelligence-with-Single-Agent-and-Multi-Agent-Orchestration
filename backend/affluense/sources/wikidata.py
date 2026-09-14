"""Wikidata: structured claims, company links, and co-officer connections."""

from __future__ import annotations

from .. import config


def search_entities(fetcher, query: str, limit: int = 10) -> list:
    """Note: this endpoint does NOT tolerate misspellings. Resolve the name
    through Wikipedia first."""
    data = fetcher.get_json(
        config.WIKIDATA_API,
        params={
            "action": "wbsearchentities", "search": query, "language": "en",
            "type": "item", "limit": limit, "format": "json",
        },
    )
    if not data:
        return []
    return [
        {
            "id": entry["id"],
            "label": entry.get("label"),
            "description": entry.get("description"),
            "source_url": f"{config.WIKIDATA_ITEM}{entry['id']}",
        }
        for entry in data.get("search", [])
    ]


def entity(fetcher, qid: str) -> dict:
    data = fetcher.get_json(f"{config.WIKIDATA_ENTITY}{qid}.json")
    if not data:
        return {}
    return data.get("entities", {}).get(qid, {})


def is_human(fetcher, qid: str) -> bool:
    claims = entity(fetcher, qid).get("claims", {})
    instances = [
        c["mainsnak"].get("datavalue", {}).get("value", {}).get("id")
        for c in claims.get("P31", [])
    ]
    return "Q5" in instances


def controllers_of(fetcher, qid: str) -> set:
    """QIDs that own, parent, founded or lead this organisation.

    Used to test a stake a page merely asserts. A joint-venture partner and
    an owner read the same in prose — "acquired Old Mutual's stake" became
    "owns 100% of Old Mutual" — and only a structured source tells them
    apart.
    """
    raw = entity(fetcher, qid).get("claims", {})
    found = set()
    for prop in config.ORG_OWNERSHIP:
        for statement in raw.get(prop, []):
            value = statement.get("mainsnak", {}).get("datavalue", {}).get("value")
            if isinstance(value, dict) and value.get("id"):
                found.add(value["id"])
    return found


def resolve_labels(fetcher, qids: list) -> dict:
    """Q-numbers to readable names, 50 per request."""
    labels: dict = {}
    unique = [q for q in dict.fromkeys(qids) if q]
    for start in range(0, len(unique), 50):
        data = fetcher.get_json(
            config.WIKIDATA_API,
            params={
                "action": "wbgetentities",
                "ids": "|".join(unique[start : start + 50]),
                "props": "labels", "languages": "en", "format": "json",
            },
        )
        if not data:
            continue
        for qid, item in data.get("entities", {}).items():
            labels[qid] = item.get("labels", {}).get("en", {}).get("value", qid)
    return labels


def _snak_value(snak: dict):
    datavalue = snak.get("datavalue", {})
    kind, value = datavalue.get("type"), datavalue.get("value")
    if kind == "wikibase-entityid":
        return {"qid": value.get("id")}
    if kind == "time":
        return {"time": value.get("time", "").lstrip("+")[:10]}
    if kind == "quantity":
        return {"amount": value.get("amount"),
                "unit": value.get("unit", "").rsplit("/", 1)[-1]}
    if kind == "monolingualtext":
        return {"text": value.get("text")}
    return {"text": value} if isinstance(value, str) else {"raw": value}


def claims(fetcher, qid: str) -> dict:
    """The person's own statements, rendered readable and attributed."""
    item_url = f"{config.WIKIDATA_ITEM}{qid}"
    raw_claims = entity(fetcher, qid).get("claims", {})

    collected, pending = {}, []
    for prop, label in config.PERSON_CLAIMS.items():
        entries = []
        for statement in raw_claims.get(prop, []):
            value = _snak_value(statement["mainsnak"])
            qualifiers = statement.get("qualifiers", {})

            def qualifier(code):
                return qualifiers.get(code, [{}])[0].get("datavalue", {}).get("value", {})

            start = qualifier("P580") or {}
            end = qualifier("P582") or {}
            of_org = qualifier("P642") or {}

            entries.append({
                "raw": value,
                "start": start.get("time", "").lstrip("+")[:10] or None,
                "end": end.get("time", "").lstrip("+")[:10] or None,
                "of": of_org.get("id"),
            })
            if value.get("qid"):
                pending.append(value["qid"])
            if of_org.get("id"):
                pending.append(of_org["id"])
        if entries:
            collected[label] = entries

    labels = resolve_labels(fetcher, pending)

    readable = {}
    for label, entries in collected.items():
        rendered = []
        for item in entries:
            raw = item["raw"]
            if "qid" in raw:
                text = labels.get(raw["qid"], raw["qid"])
                url = f"{config.WIKIDATA_ITEM}{raw['qid']}"
            elif "amount" in raw:
                text = f"{raw['amount']} ({labels.get(raw['unit'], raw['unit'])})"
                url = item_url
            else:
                text = raw.get("time") or raw.get("text") or str(raw)
                url = item_url
            rendered.append({
                "value": text, "source": "Wikidata", "source_url": url,
                "start": item["start"], "end": item["end"],
                "of": labels.get(item["of"]) if item["of"] else None,
            })
        readable[label] = rendered
    return readable


def sparql(fetcher, query: str) -> list:
    data = fetcher.get_json(
        config.WDQS,
        params={"query": query, "format": "json"},
        headers={"Accept": "application/sparql-results+json"},
        timeout=config.SPARQL_TIMEOUT,
    )
    if not data:
        fetcher.note("Wikidata Query Service returned nothing (it rate-limits hard)")
        return []
    return data.get("results", {}).get("bindings", [])


def companies_for(fetcher, qid: str) -> list:
    """Organisations naming this person as founder, officer or owner."""
    # The start and end of a role are statement *qualifiers*, so the truthy
    # `wdt:` shortcut cannot reach them -- the full statement node has to be
    # walked. Without this every Wikidata-sourced role was undated, and the
    # tenure filter could never fire on one: IL&FS's fraud stayed attributed
    # to the chairman appointed afterwards to clean it up.
    unions = "\n    UNION\n".join(
        f"""    {{
      ?org p:{prop} ?stmt{prop} .
      ?stmt{prop} ps:{prop} wd:{qid} .
      OPTIONAL {{ ?stmt{prop} pq:P580 ?roleStart . }}
      OPTIONAL {{ ?stmt{prop} pq:P582 ?roleEnd . }}
      BIND("{role}" AS ?relation)
    }}"""
        for prop, role in config.ORG_TO_PERSON.items()
    )
    # Restrict to organisations. "Owned by" otherwise returns a person's
    # house: Antilia (a residential building) was screened as a company and
    # became the only flagged entity in a report, carrying findings from a
    # criminal case that had nothing to do with the subject.
    query = f"""
SELECT DISTINCT ?org ?orgLabel ?relation ?inception ?countryLabel ?industry ?industryLabel ?roleStart ?roleEnd WHERE {{
{unions}
  ?org wdt:P31/wdt:P279* wd:Q43229 .
  OPTIONAL {{ ?org wdt:P571 ?inception . }}
  OPTIONAL {{ ?org wdt:P17 ?country . }}
  OPTIONAL {{ ?org wdt:P452 ?industry . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT 200
"""
    # One row per OPTIONAL combination arrives, so the same company repeats
    # with different industries. Merge on the Q-number.
    merged: dict = {}
    for row in sparql(fetcher, query):
        org_qid = row.get("org", {}).get("value", "").rsplit("/", 1)[-1]
        if not org_qid:
            continue
        company = merged.setdefault(org_qid, {
            "name": row.get("orgLabel", {}).get("value"),
            "wikidata_id": org_qid,
            "relationships": [],
            "inception": None,
            "country": None,
            "industries": [],
            "industry_qids": [],
            "role_start": None,
            "role_end": None,
            "source": "Wikidata Query Service",
            "source_url": f"{config.WIKIDATA_ITEM}{org_qid}",
        })

        # Earliest start and latest end across every role held there: the
        # window during which the subject was involved at all.
        start = (row.get("roleStart", {}).get("value") or "")[:10] or None
        end = (row.get("roleEnd", {}).get("value") or "")[:10] or None
        if start and (not company["role_start"] or start < company["role_start"]):
            company["role_start"] = start
        if end and (not company["role_end"] or end > company["role_end"]):
            company["role_end"] = end

        relation = row.get("relation", {}).get("value")
        if relation and relation not in company["relationships"]:
            company["relationships"].append(relation)
        industry = row.get("industryLabel", {}).get("value")
        if industry and industry not in company["industries"]:
            company["industries"].append(industry)
        industry_qid = row.get("industry", {}).get("value", "").rsplit("/", 1)[-1]
        if industry_qid and industry_qid not in company["industry_qids"]:
            company["industry_qids"].append(industry_qid)
        company["inception"] = company["inception"] or (
            (row.get("inception", {}).get("value") or "")[:10] or None
        )
        company["country"] = company["country"] or row.get("countryLabel", {}).get("value")

    return list(merged.values())


def people_for(fetcher, org_qid: str, limit: int = 25) -> list:
    """People the organisation names as founder, officer, chair or owner.

    The mirror image of `companies_for`, for the company-only entry point:
    the user types "Wipro" and has to be shown whose footprint they mean
    before anything expensive runs.
    """
    unions = "\n    UNION\n".join(
        f'    {{ wd:{org_qid} wdt:{prop} ?person . BIND("{role}" AS ?relation) }}'
        for prop, role in config.ORG_TO_PERSON.items()
    )
    query = f"""
SELECT DISTINCT ?person ?personLabel ?personDescription ?relation WHERE {{
{unions}
  ?person wdt:P31 wd:Q5 .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT {limit}
"""
    merged: dict = {}
    for row in sparql(fetcher, query):
        qid = row.get("person", {}).get("value", "").rsplit("/", 1)[-1]
        if not qid:
            continue
        person = merged.setdefault(qid, {
            "name": row.get("personLabel", {}).get("value"),
            "wikidata_id": qid,
            "description": row.get("personDescription", {}).get("value"),
            "relationships": [],
            "source": "Wikidata Query Service",
            "source_url": f"{config.WIKIDATA_ITEM}{qid}",
        })
        relation = row.get("relation", {}).get("value")
        if relation and relation not in person["relationships"]:
            person["relationships"].append(relation)

    return [p for p in merged.values() if p["name"]]


def coofficers(fetcher, qid: str, org_qids: list) -> list:
    """People holding an officer role at the same organisations.

    One small query per property: the combined UNION form times out on the
    public endpoint.
    """
    if not org_qids:
        return []

    values = " ".join(f"wd:{q}" for q in org_qids[:25])
    people, seen = [], set()

    for prop, role in config.ORG_TO_PERSON.items():
        if prop == "P127":  # "owned by" is often a company, not a person
            continue
        query = f"""
SELECT DISTINCT ?person ?personLabel ?orgLabel WHERE {{
  VALUES ?org {{ {values} }}
  ?org wdt:{prop} ?person .
  FILTER(?person != wd:{qid})
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT 100
"""
        for row in sparql(fetcher, query):
            person_qid = row.get("person", {}).get("value", "").rsplit("/", 1)[-1]
            org_name = row.get("orgLabel", {}).get("value")
            if (person_qid, org_name) in seen:
                continue
            seen.add((person_qid, org_name))
            people.append({
                "name": row.get("personLabel", {}).get("value"),
                "wikidata_id": person_qid,
                "role": role,
                "relationship": f"{role} at {org_name}",
                "via": org_name,
                "source": "Wikidata Query Service",
                "source_url": f"{config.WIKIDATA_ITEM}{person_qid}",
            })
    return people
