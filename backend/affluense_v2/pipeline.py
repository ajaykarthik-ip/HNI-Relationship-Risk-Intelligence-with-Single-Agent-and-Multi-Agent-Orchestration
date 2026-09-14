"""V2's entry points. Same signatures as V1's, same return shapes.

    from affluense_v2.pipeline import Options, run
    result = run("Mukesh Ambani", "Reliance Industries", Options())
    report = affluense.output.build_report(
        result, "Mukesh Ambani", "Reliance Industries")

The report builder, the validator and the risk scorer are V1's, untouched, and
they are handed exactly the dict V1's own pipeline would have handed them. That
is what makes the two engines comparable at all: if the verdict were computed
differently, a benchmark between them would measure nothing.

`run` is synchronous because its callers are: `api.py` submits work to a thread
pool, and `bench.py` is a script. The event loop is created here, lives for one
run, and is torn down with the transport.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from affluense import output as screening_output

from . import config as v2config
from . import orchestrator
from .progress import Cancelled, Reporter
from .quality import confidence as quality_confidence


@dataclass
class Options:
    """V1's Options, plus what V2 needs to schedule and report.

    Every field V1 has keeps V1's name and default, so a caller can switch
    engines by changing one import. `workers` is accepted and ignored: V2's
    concurrency comes from the per-host policy, not from a worker count, and
    silently rejecting a field the API already sends would be worse than
    documenting that it no longer applies.
    """

    # --- V1-compatible -----------------------------------------------------
    max_news: int = 25
    max_companies: int = 12
    delay: float = 1.0
    firecrawl_key: str | None = None
    firecrawl_queries: int = 4
    firecrawl_limit: int = 8
    firecrawl_pages: int = 6
    firecrawl_extract: bool = True
    verbose: bool = True
    on_progress: object = None
    workers: int = 6          # accepted for compatibility; see above
    cache_dir: str | None = ".cache"
    confirmed_entity: dict | None = None

    # --- network (PS2) -----------------------------------------------------
    max_suggestions: int = 25
    industries: int = 3
    roles: int = 3

    # --- V2 only -----------------------------------------------------------
    # Structured progress, so the API does not have to regex printed lines.
    on_event: object = None
    # Run PS2 alongside PS1 off one shared discovery, instead of as a second
    # job that repeats identity resolution and company discovery.
    with_network: bool = False
    # Set by `run`; agents read the reporter from here.
    reporter: object = field(default=None, repr=False)

    def build_reporter(self, cancel_check=None) -> Reporter:
        reporter = Reporter(
            on_progress=self.on_progress,
            on_event=self.on_event,
            verbose=self.verbose,
            cancel_check=cancel_check,
        )
        self.reporter = reporter
        return reporter


def _execute(coro_factory, options: Options):
    """Drive one run to completion, mapping cancellation back to the host.

    When a job is cancelled the host's callback raises its own exception --
    `api.RunCancelled`. V2 catches that at the progress boundary so it can stop
    every in-flight task cleanly, then re-raises the original object here. The
    API therefore still sees the type it already knows how to report, and a
    stopped job is marked cancelled rather than failed.
    """
    reporter = options.reporter
    try:
        return asyncio.run(coro_factory())
    except Cancelled:
        if reporter is not None:
            reporter.raise_host_cancellation()
        raise


def run(name: str, company: str | None, options: Options) -> dict:
    """PS1. Returns the dict `affluense.output.build_report` expects."""
    options.build_reporter()
    return _execute(
        lambda: orchestrator.run_screening(name, company, options), options,
    )


def run_network(name: str, company: str | None, options: Options) -> dict:
    """PS2. Returns the dict `affluense.network.output.build_report` expects."""
    options.build_reporter()
    return _execute(
        lambda: orchestrator.run_network_only(name, company, options), options,
    )


def build_report(result: dict, name: str, company: str | None) -> dict:
    """V1's report, then V2's confidence cap.

    The assembly, the validator and the risk scorer are V1's and run unchanged,
    so the delivered shape is identical and the two engines stay comparable.
    The only V2 addition is a cap that can lower confidence when the findings
    are thinly sourced, keyword-derived, judged from headlines, or resting on
    company links that were never verified. It can never raise a level -- V1's
    rubric remains the ceiling.
    """
    report = screening_output.build_report(result, name, company)
    report["engine"] = "v2"
    return quality_confidence.cap(report)


def describe() -> dict:
    """What the engine is, for /health and the benchmark header."""
    return {
        "engine": "v2",
        "concurrency": {
            "entities": v2config.MAX_ENTITY_CONCURRENCY,
            "fulltext": v2config.MAX_FULLTEXT_CONCURRENCY,
            "analyst": v2config.MAX_ANALYST_CONCURRENCY,
            "firecrawl": v2config.MAX_FIRECRAWL_CONCURRENCY,
            "sparql": v2config.MAX_SPARQL_CONCURRENCY,
        },
        "budgets": {
            "openai_calls_per_run": v2config.OPENAI_MAX_CALLS_PER_RUN,
            "fulltext_fetches": v2config.MAX_FULLTEXT_FETCHES,
        },
        "coverage": {
            "queries_per_entity": v2config.MAX_QUERIES_PER_ENTITY,
            "news_per_query": v2config.NEWS_PER_QUERY,
            "extract_batch": v2config.OPENAI_EXTRACT_BATCH,
        },
    }
