"""Network Agent — who do they know, and who should they meet?

Problem Statement 2. Scoring, gating and rationale-writing are V1's
(`network/scoring.py`, `network/explain.py`), imported unchanged, so a
suggestion means the same thing in both engines.

Two scheduling changes:

**It shares discovery.** In V1 a "both" run submits two jobs, and each one
re-resolves the identity and re-runs `discover_companies` from scratch --
including a second full Firecrawl pass. Here discovery happens once and both
problem statements consume it.

**The SPARQL fan-out is concurrent.** `discover.generate` nests a loop over
industries inside a loop over roles, issuing up to nine queries one after
another against a 75-second timeout (`network/discover.py:337`). The queries are
independent, so they go out together -- and the pool is merged back in V1's
nested order, industry outer and role inner, because that order decides which
company and role label a duplicated person keeps.

    in    Subject, the merged companies, claims and associates from discovery
    out   profile, current network, ranked suggestions
    LLM   one batched rationale call, inside V1's OPENAI_MAX_NETWORK_CALLS
    fails no Wikidata entity -> V1's explicit "no structured network" result.
          No industry -> V1's occupation fallback. Both unchanged.
"""

from __future__ import annotations

from affluense.models import utc_now
from affluense.network import discover, explain, scoring
from affluense.resolve import company as company_resolve
from affluense.sources import wikidata

from .. import config as v2config
from ..concurrency import gather_ordered, map_bounded
from ..network import officers as officer_lookup
from ..network import peers as news_peers
from ..quality import people as quality_people


def no_entity_result(subject, transport) -> dict:
    """What V1 returns when identity resolves to no structured entity."""
    return {
        "subject": subject,
        "profile": {"roles": [], "industries": [], "industry_qids": [],
                    "countries": [], "company_names": []},
        "network": [],
        "suggestions": [],
        "notes": transport.notes + [
            "No Wikidata entity resolved, so no structured network could be "
            "built. Network suggestions depend on officer relationships that "
            "only the structured sources carry."
        ],
        "collected_at": utc_now(),
    }


async def _generate_pool(bridge, reporter, subject_qid: str, profile: dict,
                         industries: int, roles: int) -> list:
    """`discover.generate`, fanned out but merged in V1's order."""
    industry_qids = profile["industry_qids"][:industries]
    if not industry_qids:
        return []

    # Match the subject's own roles where possible, so a chairman is compared
    # against chairs rather than against anyone in the sector.
    subject_roles = {r.lower() for r in profile["roles"]}
    ordered = sorted(
        discover.ROLE_PROPERTIES.items(),
        key=lambda kv: (kv[1].lower() not in subject_roles,),
    )[:roles]

    # The cross-product, in V1's nesting: industry outer, role inner.
    jobs = [
        (industry_qid, prop, role_label)
        for industry_qid in industry_qids
        for prop, role_label in ordered
    ]
    for industry_qid, _prop, role_label in jobs:
        reporter.say(f"    peers: {role_label} in industry {industry_qid}")

    async def one(job):
        industry_qid, prop, role_label = job
        return await bridge.run(
            discover.candidates_by_industry, subject_qid, industry_qid,
            prop, role_label,
        )

    results = await map_bounded(
        jobs, one, v2config.MAX_SPARQL_CONCURRENCY,
        on_error=lambda i, e: bridge.facade.note(
            f"Peer search failed for {jobs[i][2]} in {jobs[i][0]}: {e}"
        ),
    )

    # Merged in submission order, so the pool is identical to V1's.
    pool: dict = {}
    for found in results:
        for person in found or []:
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
                existing["matched_industries"].append(
                    person["matched_industry_qid"]
                )
    return list(pool.values())


async def run(transport, bridge, reporter, subject, bio, found: dict,
              options, budgets) -> dict:
    """The same contract as `affluense.network.pipeline.run`."""
    reporter.stage("network", total=4)
    resolved = subject.resolved_name or subject.query_name

    if not subject.wikidata_id:
        reporter.finish("network")
        return no_entity_result(subject, transport)

    companies = company_resolve.merge(found["discovered"], resolved)
    claims = found["claims"]
    associates = list(found.get("connections", []))
    firecrawl_block = found.get("firecrawl", {})

    profile = discover.subject_profile(companies)
    # "Consultant" arriving from page extraction gave every consultant in the
    # candidate pool a perfect role match -- 30% of the relevance score -- to a
    # role the subject does not meaningfully hold.
    profile["roles"] = quality_people.meaningful_roles(profile.get("roles"))
    # Discovery writes the Q-number as `registry_id`; nothing ever creates a
    # `wikidata_id` key on a company record, so reading that name returns an
    # empty list on every run -- which silently disabled co-officer lookup and,
    # with it, the whole structured half of the current network. Both names are
    # accepted so this keeps working if the field is ever renamed.
    company_qids = [
        qid for qid in (
            c.get("registry_id") or c.get("wikidata_id") for c in companies
        )
        if qid and str(qid).upper().startswith("Q")
    ]

    # Independent of each other: one asks what the companies do, the other who
    # else sits on them. V1 runs them back to back.
    reporter.say("  Industries and current network ...")
    industry_result, network = await gather_ordered(
        [
            bridge.run(discover.industries_for_companies, companies,
                       say=reporter.say),
            bridge.run(discover.current_network, subject.wikidata_id,
                       company_qids, claims, associates),
        ],
        on_error=lambda i, e: transport.note(
            f"{'Industry lookup' if i == 0 else 'Current network'} "
            f"failed: {e}"
        ),
    )
    qids, labels, _resolved_count = industry_result or ([], {}, 0)
    network = list(network or [])

    # PS2 asks for key employees in the company, which nothing was answering.
    company_names = {
        (c.get("registry_id") or c.get("wikidata_id")):
            (c.get("canonical_name") or c.get("name"))
        for c in companies
        if (c.get("registry_id") or c.get("wikidata_id"))
    }
    employees, queried, answered = await officer_lookup.key_employees(
        transport, reporter, company_qids, company_names,
    )
    known = {(p.get("name") or "").lower() for p in network}
    network.extend(
        person for person in employees
        if (person.get("name") or "").lower() not in known
    )
    # Three outcomes, and they mean different things. Reporting only the
    # middle one left a network of one person looking like a complete answer.
    if not company_qids:
        transport.note(
            f"None of the {len(companies)} connected company(ies) resolved to "
            "a registry entity, so their officers could not be looked up. Key "
            "employees are missing from the network below because the "
            "companies are not in Wikidata, not because they have none."
        )
    elif queried and not answered:
        transport.note(
            f"Officer records for {queried} connected company(ies) could not "
            "be retrieved: the Wikidata Query Service did not answer. Key "
            "employees are missing from the network below, which is a gap in "
            "the source rather than a finding about the subject."
        )
    elif answered and not employees:
        transport.note(
            f"{answered} connected company(ies) were checked and name no "
            "officers in Wikidata, so the network below rests on co-officer "
            "and page-extracted ties alone."
        )
    reporter.say(
        f"  {len(employees)} key employee(s) from {answered} of "
        f"{len(company_qids)} registry-matched company(ies)"
    )

    # Page extraction names organisations as readily as people, so "Temasek"
    # and "Asia Society" arrived as members of the subject's personal network.
    network, network_orgs = quality_people.clean_network(
        network, say=reporter.say,
    )
    reporter.advance("network")

    sectors_named: list = []
    if not qids:
        # Wikidata knows nothing about these companies; ask the pages instead.
        reporter.say(
            "  No industry on the company entities; using page sectors ..."
        )
        fallback = await bridge.run(
            discover.industries_from_sectors, companies, say=reporter.say,
        )
        qids, labels, sectors_named = fallback or ([], {}, [])

    if not qids:
        # Neither Wikidata path resolved an industry -- typically because the
        # companies are private, and on a run where the Query Service refused
        # us, always. The sectors the extraction named are still a real
        # industry signal, so they become the identifiers. `scoring` intersects
        # these sets and never inspects the values, so a stable string key
        # works exactly as a Q-number does.
        qids, labels = news_peers.sector_profile(companies, profile)
        if qids:
            reporter.say(
                f"  No registry industry resolved; using {len(qids)} sector(s) "
                "named on the companies themselves"
            )

    profile["industry_qids"] = qids
    profile["industry_labels"] = labels
    profile["sectors_named"] = sectors_named
    if labels:
        profile["industries"] = sorted(
            set(profile["industries"]) | set(labels.values())
        )
    reporter.advance("network")

    reporter.say("  Generating global candidates ...")
    basis = "industry and role"
    pool = await _generate_pool(
        bridge, reporter, subject.wikidata_id, profile,
        options.industries, options.roles,
    )
    registry_pool = len(pool)

    # Wikidata is the better source and stays primary. But it is one source,
    # and when the Query Service refuses a request PS2 would otherwise deliver
    # an empty list. News coverage is free, already fetched concurrently, and
    # does not require the subject's peers to exist in a registry.
    if len(pool) < options.max_suggestions:
        reporter.say("  Widening the pool from news coverage ...")
        from_news = await news_peers.discover(
            transport, reporter, budgets.openai_peers, profile,
            subject.resolved_name or subject.query_name,
            industries=options.industries, roles=options.roles,
            say=reporter.say,
        )
        if from_news:
            known = {(c.get("name") or "").lower() for c in pool}
            added = [
                c for c in from_news
                if (c.get("name") or "").lower() not in known
            ]
            pool.extend(added)
            basis = (
                "industry and role, widened with news coverage"
                if registry_pool else "role and industry from news coverage"
            )
            reporter.say(
                f"  {len(added)} additional candidate(s) from news coverage"
            )
            transport.note(
                f"{len(added)} suggestion(s) were identified from news "
                "coverage rather than a structured registry, so their role "
                "and company come from a headline and are not registry-"
                "verified."
            )

    if not pool:
        # Never return an empty list without saying why. Silence reads as "this
        # person has no relevant connections", which is a different and much
        # stronger claim than "the sources carry no industry data".
        named = profile.get("sectors_named") or []
        reason = (
            f"No industry could be established for {resolved}. "
            f"{len(companies)} connected companies were found"
            + (f", and the sectors named on their pages "
               f"({', '.join(named[:4])}) could not be matched to a Wikidata "
               "industry" if named else ", none recorded in Wikidata with an "
               "industry")
            + ", so no industry-matched peer set could be built."
        )
        transport.note(reason)
        reporter.say(f"  {reason}")

        occupations = [
            entry["source_url"].rsplit("/", 1)[-1]
            for entry in claims.get("occupation", [])
        ]
        countries = [
            entry["source_url"].rsplit("/", 1)[-1]
            for entry in claims.get("country of citizenship", [])
        ]
        if occupations:
            basis = "occupation"
            reporter.say("  Falling back to occupation-matched peers ...")
            found_pools = await gather_ordered([
                bridge.run(
                    discover.candidates_by_occupation, subject.wikidata_id,
                    occupation_qid, countries[0] if countries else None,
                )
                for occupation_qid in occupations[:2]
            ])
            for chunk in found_pools:
                pool.extend(chunk or [])
            if pool:
                transport.note(
                    "Suggestions below are matched on shared occupation, not "
                    "industry, and are therefore weaker."
                )
    reporter.advance("network")

    # An endorser is in an industry's coverage without being in the industry,
    # and a colleague at the subject's own company is not a connection to make.
    # Both were being suggested.
    pool, dropped = quality_people.filter_candidates(
        pool, profile.get("company_names") or [],
        subject_name=resolved, say=reporter.say,
    )

    reporter.say(f"  {len(pool)} candidates found; scoring ...")
    suggestions = scoring.rank(pool, profile, network,
                               limit=options.max_suggestions)

    # Prominence measures how well documented someone is, which tracks
    # seniority and also fame. Left ungated it lifted famous strangers above
    # relevant executives, so it now counts for nothing on its own.
    gated = explain.gate_prominence(suggestions)
    if gated:
        reporter.say(
            f"  {gated} suggestion(s) rescored: prominence alone is not "
            "relevance"
        )

    await bridge.run(explain.add_rationales, suggestions, profile,
                     say=reporter.say)

    reporter.advance("network")
    reporter.finish("network")

    return {
        "subject": subject,
        "biography": bio,
        "profile": profile,
        "network": network,
        "candidate_pool_size": len(pool),
        "candidate_basis": basis,
        "candidate_sources": {
            "registry": registry_pool,
            "news": max(0, len(pool) - registry_pool),
            "filtered_out": len(dropped),
        },
        # Organisations that arrived as network members. Reported rather than
        # discarded: the affiliation is real, it is just not a person.
        "network_affiliations": network_orgs,
        "companies": companies,
        "suggestions": suggestions,
        "firecrawl": firecrawl_block,
        "notes": transport.notes,
        "cache": transport.cache.summary,
        "usage": transport.meter.summary(),
        "collected_at": utc_now(),
    }
