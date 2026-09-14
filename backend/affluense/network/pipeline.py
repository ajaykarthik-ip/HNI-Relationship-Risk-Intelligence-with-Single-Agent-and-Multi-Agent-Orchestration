"""Orchestration for Problem Statement 2."""

from __future__ import annotations

from dataclasses import dataclass

from .. import usage as usage_mod
from ..cache import ResponseCache
from ..http import Fetcher
from ..models import utc_now
from ..pipeline import Options as ScreeningOptions
from ..pipeline import discover_companies
from ..resolve import company as company_resolve
from ..resolve import person as person_resolve
from ..sources import firecrawl as fc
from ..sources import wikidata
from . import discover, explain, scoring


@dataclass
class Options:
    max_suggestions: int = 25
    industries: int = 3
    roles: int = 3
    delay: float = 1.0
    firecrawl_key: str | None = None
    firecrawl_limit: int = 8
    firecrawl_pages: int = 4
    verbose: bool = True
    # Called with each progress line. Lets an API surface live progress
    # without capturing stdout.
    on_progress: object = None
    cache_dir: str | None = ".cache"
    # The candidate the user picked in the UI. See screening Options.
    confirmed_entity: dict | None = None


def _say(options: Options, message: str) -> None:
    if options.verbose:
        print(message, flush=True)
    if options.on_progress is not None:
        options.on_progress(message.strip())


def run(name: str, company: str | None, options: Options) -> dict:
    fetcher = Fetcher(
        delay=options.delay,
        cache=ResponseCache(options.cache_dir, enabled=options.cache_dir is not None),
    )
    rate, rate_source = usage_mod.resolve_inr_rate(fetcher)
    fetcher.meter.usd_to_inr = rate
    fetcher.meter.rate_source = rate_source

    # -- 1. resolve ---------------------------------------------------------
    _say(options, "  Resolving identity ...")
    subject, candidates_seen, bio = person_resolve.resolve(
        fetcher, name, company, confirmed=options.confirmed_entity
    )
    resolved = subject.resolved_name or name

    if not subject.wikidata_id:
        return {
            "subject": subject,
            "profile": {"roles": [], "industries": [], "industry_qids": [],
                        "countries": [], "company_names": []},
            "network": [],
            "suggestions": [],
            "notes": fetcher.notes + [
                "No Wikidata entity resolved, so no structured network could be "
                "built. Network suggestions depend on officer relationships that "
                "only the structured sources carry."
            ],
            "collected_at": utc_now(),
        }

    # -- 2. the subject's own footprint ------------------------------------
    # Uses the same discovery as the screening pipeline. Asking Wikidata alone
    # returned zero companies for subjects whose businesses are private, while
    # screening found ten for the same person.
    _say(options, "  Companies and industries ...")
    screening_options = ScreeningOptions(
        delay=options.delay,
        firecrawl_key=options.firecrawl_key,
        firecrawl_limit=options.firecrawl_limit,
        firecrawl_pages=options.firecrawl_pages,
        firecrawl_extract=True,
        verbose=options.verbose,
        cache_dir=options.cache_dir,
    )
    found = discover_companies(fetcher, subject, company, screening_options,
                               say=lambda m: _say(options, m))
    companies = company_resolve.merge(found["discovered"], resolved)
    claims = found["claims"]
    associates = list(found.get("connections", []))

    profile = discover.subject_profile(companies)
    qids, labels, resolved_count = discover.industries_for_companies(
        fetcher, companies, say=lambda m: _say(options, m)
    )
    sectors_named: list = []
    if not qids:
        # Wikidata knows nothing about these companies; ask the pages instead.
        _say(options, "  No industry on the company entities; using page sectors ...")
        qids, labels, sectors_named = discover.industries_from_sectors(
            fetcher, companies, say=lambda m: _say(options, m)
        )

    profile["industry_qids"] = qids
    profile["industry_labels"] = labels
    profile["sectors_named"] = sectors_named
    if labels:
        profile["industries"] = sorted(set(profile["industries"]) | set(labels.values()))

    # -- 3. current network -------------------------------------------------
    _say(options, "  Current network ...")
    firecrawl_block = found.get("firecrawl", {})

    company_qids = [c["wikidata_id"] for c in companies if c.get("wikidata_id")]
    network = discover.current_network(
        fetcher, subject.wikidata_id, company_qids, claims, associates
    )

    # -- 4. candidate generation and scoring -------------------------------
    _say(options, "  Generating global candidates ...")
    basis = "industry and role"
    pool = discover.generate(
        fetcher, subject.wikidata_id, profile,
        industries=options.industries, roles=options.roles,
        say=lambda m: _say(options, m),
    )

    if not pool:
        # Never return an empty list without saying why. Silence reads as
        # "this person has no relevant connections", which is a different
        # and much stronger claim than "the sources carry no industry data".
        named = profile.get("sectors_named") or []
        reason = (
            f"No industry could be established for {resolved}. "
            f"{len(companies)} connected companies were found"
            + (f", and the sectors named on their pages ({', '.join(named[:4])}) "
               "could not be matched to a Wikidata industry"
               if named else ", none recorded in Wikidata with an industry")
            + ", so no industry-matched peer set could be built."
        )
        fetcher.note(reason)
        _say(options, f"  {reason}")

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
            _say(options, "  Falling back to occupation-matched peers ...")
            for occupation_qid in occupations[:2]:
                pool.extend(discover.candidates_by_occupation(
                    fetcher, subject.wikidata_id, occupation_qid,
                    countries[0] if countries else None,
                ))
            if pool:
                fetcher.note(
                    "Suggestions below are matched on shared occupation, not "
                    "industry, and are therefore weaker."
                )

    _say(options, f"  {len(pool)} candidates found; scoring ...")
    suggestions = scoring.rank(pool, profile, network, limit=options.max_suggestions)

    # Prominence measures how well documented someone is, which tracks
    # seniority and also fame. Left ungated it lifted famous strangers above
    # relevant executives, so it now counts for nothing on its own.
    gated = explain.gate_prominence(suggestions)
    if gated:
        _say(options, f"  {gated} suggestion(s) rescored: prominence alone is not relevance")

    explain.add_rationales(fetcher, suggestions, profile,
                           say=lambda m: _say(options, m))

    return {
        "subject": subject,
        "biography": bio,
        "profile": profile,
        "network": network,
        "candidate_pool_size": len(pool),
        "candidate_basis": basis,
        "companies": companies,
        "suggestions": suggestions,
        "firecrawl": firecrawl_block,
        "notes": fetcher.notes,
        "cache": fetcher.cache.summary,
        "usage": fetcher.meter.summary(),
        "collected_at": utc_now(),
    }
