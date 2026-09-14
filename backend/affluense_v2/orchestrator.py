"""The V2 orchestrator: deterministic Python driving six concurrent agents.

This is the file the whole engine turns on, so it is worth being explicit about
what it is and is not. It is a dependency graph with budgets, retries and
cancellation. It is not a planner: nothing here decides what to do next based on
what a model said, and no stage can add work that was not in the graph when the
run started. V1's rule holds -- deterministic Python controls the flow and
decides what is true.

The graph, and why it is shaped this way:

    identity
      |-- discovery ----------------------.
      |     (wikidata || firecrawl)       |
      |-- person evidence -----------.    |
      |   starts here, not last      |    |
      |                              |    |
      merge + entity resolution      |    |
      |                              |    |
      company evidence x N ----------+    |
      |   all concurrent             |    |
      assignment (SERIAL)  <---------'    |
      |                                   |
      fulltext (concurrent, budget claimed in order)
      |                                   |
      analysis (concurrent batches)       |
      |                                   |
      findings (concurrent per entity)    |
      |                                   |
      verdict: validate -> risk_score -> qc   (V1, unchanged)

Three things are sequential on purpose, and each one would be a bug if it were
not:

  **assignment**  `news.dedupe` carries a `seen` set across entities, so which
  copy of a syndicated story survives depends on the order entities are visited.
  V1 notes this itself. Concurrency here would make two runs over identical
  evidence produce different reports.

  **budget claiming**  The full-text budget is one allowance for the whole run.
  Claiming it in target order -- controlling interests first -- means a run that
  exhausts it always spends it on the same articles. The *fetching* is
  concurrent; only the claiming is ordered.

  **the verdict chain**  Clustering needs every article's extraction, scoring
  needs every finding, QC needs the assembled report.

The return value is the same dict `affluense.pipeline.run` returns, so
`output.build_report` assembles both engines' reports through identical code.
"""

from __future__ import annotations

import asyncio

from affluense import config as v1config
from affluense import usage as usage_mod
from affluense.enrich import extract, publishers, risk
from affluense.enrich import sentiment as sentiment_mod
from affluense.enrich import tenure
from affluense.http import Fetcher as SyncFetcher
from affluense.models import CompanyScreening, SentimentCounts, utc_now
from affluense.pipeline import _coverage, _evidence, _screening_priority, _stake_of
from affluense.resolve import company as company_resolve
from affluense.resolve import relationships
from affluense.sources import news, web
from affluense.usage import UsageMeter

from . import config as v2config
from .agents import analyst, discovery, evidence, fulltext, identity
from .agents import network as network_agent
from .bridge import Bridge
from .budgets import RunBudgets
from affluense.cache import ResponseCache

from .cache_async import AsyncCache
from .concurrency import gather_ordered, map_bounded
from .progress import Cancelled
from .quality import events as quality_events
from .transport import AsyncFetcher


class Run:
    """One screening run. Holds the shared, deliberately small, mutable state."""

    def __init__(self, name: str, company: str | None, options, reporter):
        self.name = name
        self.company = company
        self.options = options
        self.reporter = reporter
        self.analyzer = sentiment_mod.SentimentAnalyzer()

        self.cache = AsyncCache(
            options.cache_dir, enabled=options.cache_dir is not None,
        )
        self.meter = UsageMeter()
        self.budgets = RunBudgets(
            openai_calls=v2config.OPENAI_MAX_CALLS_PER_RUN,
            fulltext=v1config.MAX_FULLTEXT_FETCHES,
            peers=v2config.OPENAI_MAX_PEER_CALLS,
        )
        self.transport = AsyncFetcher.build(
            self.cache, self.meter, reporter, self.budgets,
        )
        self.bridge = Bridge(self.transport, asyncio.get_running_loop())
        # PS2 runs beside the screening, so it has to be reachable from the
        # teardown path: a task still in flight when the transport closes would
        # fail on a client that no longer exists.
        self.network_task = None

    async def aclose(self) -> None:
        task = self.network_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self.transport.aclose()


async def _prepare(run: Run) -> None:
    """Settle the exchange rate before any billable call.

    Same reason V1 does it first: every credit in the run is then converted at
    one rate, and the report says which rate it was.
    """
    rate, rate_source = await run.bridge.run(usage_mod.resolve_inr_rate)
    run.meter.usd_to_inr = rate
    run.meter.rate_source = rate_source


def _apply_company_corroboration(run: Run, subject, merged: list,
                                 resolved: str) -> None:
    """The supplied company is meant to corroborate the identity.

    If it never turns up among the discovered companies it corroborated
    nothing, and saying it "was used to steer the search" at 95% confidence
    overstates the match. Entering an unrelated company must lower confidence,
    not leave it untouched. V1's rule, verbatim.
    """
    if not run.company:
        return
    wanted = company_resolve.normalise(run.company, resolved)
    seen = any(
        company_resolve.normalise(
            c.get("canonical_name") or c["name"], resolved
        ) == wanted
        for c in merged
        if c.get("source") != "Query input"
    )
    if seen:
        return
    subject.match_confidence = round(min(subject.match_confidence, 0.55), 2)
    subject.match_basis += (
        f" The supplied company '{run.company}' was NOT found among the "
        "connected companies, so it did not corroborate this match. "
        "Confidence is reduced: check that the name and company belong "
        "together."
    )
    run.transport.note(
        f"'{run.company}' was not found among {resolved}'s connected "
        "companies; identity confidence reduced."
    )
    # Flagged, not dropped. The company is still screened -- hiding it would
    # hide the mismatch -- but nothing found there is attributed to a person
    # the evidence never tied to it.
    for record in merged:
        if record.get("source") == "Query input":
            record["corroboration_failed"] = True


def _annotate_local(run: Run, articles: list, record: dict | None) -> None:
    """VADER, keyword risk and tenure. Local, deterministic, no network.

    Kept on the event loop because it is pure CPU over a few dozen short
    strings -- cheaper than the thread hop it would cost to move it.
    """
    run.analyzer.annotate(articles)
    risk.annotate(articles)
    if record is not None:
        outside = tenure.mark_out_of_tenure(articles, record)
        if outside:
            name = record.get("canonical_name") or record["name"]
            run.reporter.say(
                f"    {outside} article(s) fall outside the subject's tenure "
                f"at {name}"
            )


def _build_screening(record: dict, articles: list, counts: dict,
                     classification: str, flags: list) -> CompanyScreening:
    """One company's delivered row. Every field is V1's."""
    company_name = record.get("canonical_name") or record["name"]
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
        sources=record.get(
            "sources",
            [record.get("source")] if record.get("source") else [],
        ),
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
    )


def _post_run_fetcher(run: Run):
    """A synchronous fetcher for the work that happens AFTER the loop closes.

    `output.build_report` runs one last OpenAI call -- `qc.review` -- and it
    runs it in the caller's thread, after `asyncio.run` has already returned
    and torn the loop down. Handing it the bridge facade fails with
    "Event loop is closed": the facade's whole job is to forward onto a loop
    that no longer exists.

    So the post-run phase gets a plain V1 `Fetcher`. It shares this run's meter,
    so the QC call's tokens land in the same usage block, and the same notes
    list, so its warnings reach the same report. One synchronous call has
    nothing to gain from the async transport anyway.
    """
    fetcher = SyncFetcher(
        cache=ResponseCache(
            run.options.cache_dir,
            enabled=run.options.cache_dir is not None,
        ),
        meter=run.meter,
    )
    # The same list object the report already points at, so a note raised
    # during the final review still shows up under "What the sources said".
    fetcher.notes = run.transport.notes
    return fetcher


async def run_screening(name: str, company: str | None, options) -> dict:
    """PS1, and optionally PS2 alongside it. Returns V1's result dict."""
    reporter = options.reporter
    run = Run(name, company, options, reporter)
    try:
        return await _screen(run)
    finally:
        await run.aclose()


async def _screen(run: Run) -> dict:
    options, reporter = run.options, run.reporter
    await _prepare(run)

    # -- 1. identity --------------------------------------------------------
    subject, candidates, bio = await identity.resolve(
        run.bridge, reporter, run.name, run.company, options.confirmed_entity,
    )
    resolved = subject.resolved_name or run.name

    # -- 2. discovery, and the person's own coverage, together --------------
    # The person's evidence needs only the resolved name, so it has no reason
    # to wait for company discovery. V1 runs it last; here it overlaps
    # everything.
    reporter.stage("evidence", total=1)

    discovery_task = discovery.discover(
        run.transport, run.bridge, reporter, subject, run.company, options,
    )
    person_task = evidence.collect_person(
        run.transport, reporter, resolved, run.company,
    )
    found, person_collected = await gather_ordered(
        [discovery_task, person_task],
        on_error=lambda i, e: run.transport.note(
            f"{'Company discovery' if i == 0 else 'Person evidence'} "
            f"failed: {e}"
        ),
    )
    found = found or {"discovered": [], "claims": {}, "connections": [],
                      "firecrawl": {}, "other_affiliations": [],
                      "investments": []}
    person_articles, person_queries = person_collected or ([], [])

    # -- 3. merge -----------------------------------------------------------
    discovered = found["discovered"]
    merged = company_resolve.merge(discovered, resolved)
    reporter.say(
        f"  {len(discovered)} company records merged to {len(merged)} entities"
    )
    _apply_company_corroboration(run, subject, merged, resolved)

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
            name, record.get("relationships", []),
            record.get("status", "active"), _stake_of(record),
        )
        if record.get("corroboration_failed"):
            relationship_class = "uncorroborated_seed"
            basis = (
                f"'{name}' was supplied with the query but nothing else "
                "connects the subject to it. Screened and reported; not "
                "treated as the subject's own exposure."
            )
        record["relationship_class"] = relationship_class
        record["relationship_basis"] = basis
    await run.bridge.run(
        relationships.resolve_unknowns, targets, resolved, say=reporter.say,
    )

    owner_domains = publishers.owner_domains_for(
        resolved, [r.get("canonical_name") or r["name"] for r in targets]
    )

    # -- 4. company evidence, all at once -----------------------------------
    # The person's set already advanced this stage once, so the total is the
    # companies plus that one.
    reporter.stage("evidence", total=len(targets) + 1)

    if options.with_network:
        # PS2 depends only on discovery, so it runs beside the screening rather
        # than as a second job that repeats identity and discovery.
        run.network_task = asyncio.ensure_future(
            network_agent.run(run.transport, run.bridge, reporter, subject,
                              bio, found, options, run.budgets)
        )
    else:
        reporter.skip("network")

    async def collect(record):
        return await evidence.collect_company(
            run.transport, reporter, record, resolved, owner_domains,
        )

    collected = await map_bounded(
        targets, collect, v2config.MAX_ENTITY_CONCURRENCY,
        on_error=lambda i, e: run.transport.note(
            f"Evidence collection failed for "
            f"{targets[i].get('canonical_name') or targets[i]['name']}: {e}"
        ),
    )
    collected = [c if c is not None else [] for c in collected]
    reporter.finish("evidence")

    # -- 5. assignment: serial, and deliberately so -------------------------
    # One story, one company. A group's flagship article comes back from the
    # query for every subsidiary, and counting it once per entity turned a
    # single fact into six and skewed every ratio computed from it.
    reporter.stage("assignment", total=1)
    seen_articles: set = set()
    assigned = []
    for record, articles in zip(targets, collected):
        kept = news.dedupe(articles, seen=seen_articles)
        shared = len(articles) - len(kept)
        if shared:
            name = record.get("canonical_name") or record["name"]
            reporter.say(
                f"    {shared} article(s) already counted for another company, "
                f"not double-counted for {name}"
            )
        assigned.append(kept)

    # The person's articles are de-duplicated last, exactly as V1 does it, so
    # a story shared with a company stays with the company.
    person_articles = news.dedupe(person_articles, seen=seen_articles)
    publishers.annotate(person_articles, owner_domains)
    person_articles, person_promo = publishers.split_promotional(person_articles)
    reporter.advance("assignment")
    reporter.finish("assignment")

    # -- 6. local annotation, then full text --------------------------------
    for record, articles in zip(targets, assigned):
        _annotate_local(run, articles, record)
    _annotate_local(run, person_articles, None)

    # Budget claimed in V1's order -- companies by screening priority, then the
    # person -- then every claimed article fetched at once.
    reporter.stage("fulltext", total=run.budgets.fulltext.limit or 1)
    for record, articles in zip(targets, assigned):
        name = record.get("canonical_name") or record["name"]
        record["fulltext"] = await fulltext.fetch(
            run.transport, run.bridge, reporter, articles,
            run.budgets.fulltext, label=name,
        )
    person_fulltext = await fulltext.fetch(
        run.transport, run.bridge, reporter, person_articles,
        run.budgets.fulltext, label=resolved,
    )
    reporter.finish("fulltext")

    # -- 7. analysis --------------------------------------------------------
    # The extraction budget is sized from the work actually queued, so every
    # article V1 would have sent to the model still gets sent. The run-scoped
    # ceiling in the transport is the safety net underneath.
    entity_sets = [(t, a) for t, a in zip(targets, assigned)]
    batches_needed = sum(analyst.batches_for(a) for _, a in entity_sets)
    batches_needed += analyst.batches_for(person_articles)
    run.budgets.size_extraction(
        batches_needed, v2config.OPENAI_EXTRACT_HEADROOM,
    )

    reporter.stage("analysis", total=len(entity_sets) + 1)

    async def analyse_company(entry):
        record, articles = entry
        name = record.get("canonical_name") or record["name"]
        return await analyst.analyse(
            run.transport, reporter, articles, name, resolved,
            run.budgets.openai_extract,
            role_period=tenure.role_period(record),
        )

    company_extractions, person_extraction = await gather_ordered(
        [
            map_bounded(entity_sets, analyse_company,
                        v2config.MAX_ENTITY_CONCURRENCY),
            analyst.analyse(
                run.transport, reporter, person_articles, resolved, resolved,
                run.budgets.openai_extract,
            ),
        ],
        on_error=lambda i, e: run.transport.note(f"Analysis failed: {e}"),
    )
    company_extractions = company_extractions or [None] * len(entity_sets)
    person_extraction = person_extraction or {
        "sent": 0, "read": 0, "dropped_not_about_target": 0,
        "dropped_promotional": 0, "backend": "keyword",
    }
    for record, tally in zip(targets, company_extractions):
        record["extraction"] = tally or {
            "sent": 0, "read": 0, "dropped_not_about_target": 0,
            "dropped_promotional": 0, "backend": "keyword",
        }
    reporter.finish("analysis")

    # -- 8. findings --------------------------------------------------------
    reporter.stage("findings", total=len(targets) + 1)

    async def findings_for(entry):
        record, articles = entry
        name = record.get("canonical_name") or record["name"]
        kept = extract.keep_relevant(articles)
        counts, classification = sentiment_mod.aggregate(kept)
        # V2's clustering: event identity rather than (category, year), a
        # corroborated stage rather than the maximum one seen, and attribution
        # that consults who the evidence actually names.
        company_findings = await quality_events.build_findings(
            run.bridge, kept, name,
            entity_role="; ".join(record.get("relationships", [])[:3]),
            relationship_type=record.get("relationship_class", "other"),
            subject_name=resolved,
            say=reporter.say,
        )
        # The old flag shape is still emitted so the current dashboard keeps
        # working while the new one is built.
        flags = risk.build_flags(tenure.attributable_articles(kept))
        reporter.advance("findings")
        return (
            _build_screening(record, kept, counts, classification, flags),
            company_findings or [],
        )

    results = await map_bounded(
        entity_sets, findings_for, v2config.MAX_ENTITY_CONCURRENCY,
        on_error=lambda i, e: run.transport.note(
            f"Building findings failed for "
            f"{targets[i].get('canonical_name') or targets[i]['name']}: {e}"
        ),
    )

    screenings, findings, all_evidence = [], [], []
    for (record, articles), result in zip(entity_sets, results):
        if result is None:
            # A failed entity still appears, marked as having no coverage --
            # silently dropping it would read as "nothing was found".
            counts, classification = sentiment_mod.aggregate([])
            screenings.append(
                _build_screening(record, [], counts, classification, [])
            )
            all_evidence += articles
            continue
        screening, company_findings = result
        screenings.append(screening)
        findings += company_findings
        all_evidence += articles

    # -- 9. the individual --------------------------------------------------
    person_articles_kept = extract.keep_relevant(person_articles)
    person_counts, person_class = sentiment_mod.aggregate(person_articles_kept)

    # Being named in an article is not being accused in it. The person's
    # findings are attributed by what the extraction says their role in the
    # event was -- not by the fact that the query carried their name.
    person_findings = await quality_events.build_findings(
        run.bridge, person_articles_kept, resolved,
        entity_role="the individual", relationship_type="person",
        subject_name=resolved, say=reporter.say,
    ) or []
    if person_extraction.get("read", 0) == 0:
        # No model read these, so there is no role to go on. Fall back to
        # attributing them: under-reporting a matter about the subject is the
        # worse error, and the row is labelled as keyword-derived.
        for finding in person_findings:
            finding.attributed_to_subject = True
    findings = person_findings + findings
    # One event named at both the person and the company produced two findings
    # in V1, because the two passes never saw each other. This is where they
    # meet.
    findings = quality_events.merge_across_entities(findings, say=reporter.say)
    all_evidence += person_articles
    reporter.advance("findings")
    reporter.finish("findings")

    # -- 10. coverage and assembly -----------------------------------------
    reporter.stage("report", total=1)
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

    duckduckgo = await run.bridge.run(web.duckduckgo_instant, resolved)

    network_result = None
    if run.network_task is not None:
        try:
            network_result = await run.network_task
        except Cancelled:
            raise
        except Exception as exc:
            # PS2 failing must never cost the screening. The note lands in the
            # report and the PS1 deliverable is unaffected.
            run.transport.note(f"Network analysis failed: {exc}")

    reporter.advance("report")
    reporter.finish("report")

    return {
        "findings": findings,
        "coverage": coverage,
        # Handed to the report builder for its final QC review. Deliberately a
        # synchronous fetcher, not the bridge facade: build_report is called
        # after asyncio.run has closed the loop the facade forwards onto.
        "fetcher": _post_run_fetcher(run),
        "subject": subject,
        "candidates": candidates,
        "biography": bio,
        "wikidata_claims": found["claims"],
        "screenings": screenings,
        "person_coverage": {
            "sentiment": person_counts,
            "classification": person_class,
            "articles": person_articles_kept,
        },
        "connections": found["connections"],
        "investments": found["investments"],
        "other_affiliations": found["other_affiliations"],
        "duckduckgo": duckduckgo or {},
        "firecrawl": found["firecrawl"],
        "sentiment_backend": run.analyzer.backend,
        "notes": run.transport.notes,
        "cache": run.cache.summary,
        # Counted at the call site, multiplied here. No estimate anywhere.
        "usage": run.meter.summary(),
        "collected_at": utc_now(),
        # --- V2 only, additive: never read by V1's output builder ----------
        "engine": "v2",
        "v2": {
            "stage_timings": reporter.timings(),
            "budgets": run.budgets.summary(),
            "hosts": run.transport.describe()["hosts"],
        },
        "network_result": network_result,
    }


async def run_network_only(name: str, company: str | None, options) -> dict:
    """PS2 on its own. Identity and discovery, then the network agent."""
    reporter = options.reporter
    run = Run(name, company, options, reporter)
    try:
        await _prepare(run)
        subject, _candidates, bio = await identity.resolve(
            run.bridge, reporter, run.name, run.company,
            options.confirmed_entity,
        )
        for stage in ("evidence", "assignment", "fulltext", "analysis",
                      "findings"):
            reporter.skip(stage)

        if not subject.wikidata_id:
            reporter.skip("discovery")
            reporter.stage("report", total=1)
            result = network_agent.no_entity_result(subject, run.transport)
            result["biography"] = bio
            result["cache"] = run.cache.summary
            result["usage"] = run.meter.summary()
            result["engine"] = "v2"
            result["v2"] = {"stage_timings": reporter.timings()}
            reporter.finish("report")
            return result

        found = await discovery.discover(
            run.transport, run.bridge, reporter, subject, run.company, options,
        )
        result = await network_agent.run(
            run.transport, run.bridge, reporter, subject, bio, found, options,
            run.budgets,
        )
        reporter.stage("report", total=1)
        reporter.advance("report")
        reporter.finish("report")
        result["engine"] = "v2"
        result["v2"] = {
            "stage_timings": reporter.timings(),
            "budgets": run.budgets.summary(),
            "hosts": run.transport.describe()["hosts"],
        }
        return result
    finally:
        await run.aclose()
