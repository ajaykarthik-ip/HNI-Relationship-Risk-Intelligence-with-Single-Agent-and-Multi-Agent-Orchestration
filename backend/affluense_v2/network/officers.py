"""Officer lookup that fails fast instead of failing slowly.

`wikidata.people_for` unions six officer properties per organisation. Asked for
one company at a time, from the company-only entry point it was written for,
that is fine. Asked for every connected company at once -- which is what PS2's
"key employees in the company" requires -- the Query Service returns 504.

V1 sends these through `sparql()` at `SPARQL_TIMEOUT = 75`, and 504 is in
`RETRY_STATUS`, so one timed-out company costs up to three attempts of 75
seconds. On a subject with eight companies that turned a 46-second network run
into a 236-second one and returned nothing extra.

So this is the same query, issued differently:

  lighter       the roles that actually identify a key employee -- founder,
                chief executive, chair, director, board member, owner -- but
                asked as a values clause rather than a six-way UNION, which is
                what WDQS struggles to plan.

  fails fast    a query that has not answered in 25 seconds will not answer in
                75, and repeating an unchanged heavy query just pays the cost
                again. One retry, then move on.

  capped        the most relevant companies, not all of them. Officer lookup
                has sharply diminishing returns past a subject's principal
                entities, and each one is a full query.

Nothing is silently lost: where the service refuses, the caller reports that
the structured network is unavailable rather than presenting a thin list as a
complete one.
"""

from __future__ import annotations

from affluense import config as v1config

from .. import config as v2config
from ..concurrency import map_bounded

# A VALUES clause the planner can optimise, instead of a UNION per property.
OFFICER_PROPERTIES = tuple(v1config.ORG_TO_PERSON.items())

QUERY = """
SELECT DISTINCT ?person ?personLabel ?personDescription ?prop WHERE {{
  VALUES ?prop {{ {properties} }}
  wd:{org_qid} ?prop ?person .
  ?person wdt:P31 wd:Q5 .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT {limit}
"""


def build_query(org_qid: str, limit: int) -> str:
    properties = " ".join(f"wdt:{prop}" for prop, _ in OFFICER_PROPERTIES)
    return QUERY.format(properties=properties, org_qid=org_qid, limit=limit)


def _role_for(prop_uri: str) -> str | None:
    """The human label for the property that matched."""
    code = (prop_uri or "").rsplit("/", 1)[-1]
    return dict(OFFICER_PROPERTIES).get(code)


async def officers_for(transport, org_qid: str, limit: int | None = None) -> list:
    """People this organisation names as an officer. [] on any failure."""
    limit = limit or v2config.OFFICERS_PER_COMPANY
    data = await transport.get_json(
        v1config.WDQS,
        params={"query": build_query(org_qid, limit), "format": "json"},
        headers={"Accept": "application/sparql-results+json"},
        timeout=v2config.SPARQL_TIMEOUT,
        attempts=v2config.SPARQL_ATTEMPTS,
    )
    if not data:
        return []

    merged: dict = {}
    for row in data.get("results", {}).get("bindings", []):
        qid = row.get("person", {}).get("value", "").rsplit("/", 1)[-1]
        name = row.get("personLabel", {}).get("value")
        # An unlabelled entity comes back as its own Q-number, which is not a
        # name anybody can act on.
        if not qid or not name or name == qid:
            continue
        person = merged.setdefault(qid, {
            "name": name,
            "wikidata_id": qid,
            "description": row.get("personDescription", {}).get("value"),
            "relationships": [],
            "source": "Wikidata Query Service",
            "source_url": f"{v1config.WIKIDATA_ITEM}{qid}",
        })
        role = _role_for(row.get("prop", {}).get("value"))
        if role and role not in person["relationships"]:
            person["relationships"].append(role)
    return list(merged.values())


async def key_employees(transport, reporter, company_qids: list,
                        company_names: dict, limit: int | None = None) -> tuple:
    """(people, companies_queried, companies_answered).

    The counts are returned so the caller can say the structured network was
    unavailable rather than presenting an empty list as a finding.
    """
    wanted = list(company_qids)[: v2config.OFFICER_COMPANY_CAP]
    if not wanted:
        return [], 0, 0

    reporter.say(
        f"  Key employees at {len(wanted)} connected company(ies) ..."
    )

    async def one(qid: str):
        return await officers_for(transport, qid, limit)

    results = await map_bounded(
        wanted, one, v2config.MAX_SPARQL_CONCURRENCY,
        on_error=lambda i, e: transport.note(
            f"Officer lookup failed for {wanted[i]}: {e}"
        ),
    )

    answered = sum(1 for r in results if r)
    found = []
    for qid, people in zip(wanted, results):
        company = company_names.get(qid) or qid
        for person in people or []:
            roles = person.get("relationships") or []
            found.append({
                "name": person["name"],
                "wikidata_id": person.get("wikidata_id"),
                "role": "; ".join(roles) or None,
                "company": company,
                "tie": (
                    f"{roles[0]} at {company}" if roles
                    else f"named as an officer of {company}"
                ),
                "tie_type": "colleague",
                "source": person["source"],
                "source_url": person["source_url"],
            })
    return found, len(wanted), answered
