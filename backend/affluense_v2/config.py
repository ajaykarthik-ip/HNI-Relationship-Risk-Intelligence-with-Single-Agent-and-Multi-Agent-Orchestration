"""V2-only settings.

Deliberately a separate module from `affluense.config`. V2 reads V1's constants
-- the query ladder, the evidence caps, the pricing -- and never writes them, so
a concurrency or timeout change here can never reach V1.

Everything V1 uses to decide *how much* research to do is imported unchanged:
MAX_QUERIES_PER_ENTITY, NEWS_PER_QUERY, MAX_FULLTEXT_FETCHES, OPENAI_EXTRACT_BATCH
and the fourteen adverse groups. V2 is faster because it schedules that work
concurrently, never because it does less of it.
"""

from __future__ import annotations

import os

from affluense import config as v1

# Re-exported so agents read coverage limits from one obvious place and cannot
# accidentally introduce a V2-only cap. These are V1's values, by reference.
MAX_QUERIES_PER_ENTITY = v1.MAX_QUERIES_PER_ENTITY
NEWS_PER_QUERY = v1.NEWS_PER_QUERY
MAX_FULLTEXT_FETCHES = v1.MAX_FULLTEXT_FETCHES
OPENAI_EXTRACT_BATCH = v1.OPENAI_EXTRACT_BATCH
FULLTEXT_CHARS = v1.FULLTEXT_CHARS
CONTROL_STAKE_PERCENT = v1.CONTROL_STAKE_PERCENT


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# --- identification --------------------------------------------------------
# Wikimedia's User-Agent policy requires a real way to contact the operator and
# refuses requests that carry an obvious placeholder. V1's constant ships with
# `contact: set-your-email-here`, which is why every live run returned 403 from
# en.wikipedia.org, wikidata.org and query.wikidata.org -- and with them went
# registry company discovery, role dates for the tenure filter, and the whole
# of PS2's structured network.
#
# V1's string is frozen, so V2 sends its own. Set AFFLUENSE_CONTACT in .env to
# an address or URL you actually monitor; without it the header still avoids the
# placeholder, but a polite contact is what the policy is really asking for.
CONTACT = os.getenv("AFFLUENSE_CONTACT", "").strip()

# The policy accepts a URL as a contact point, so the project's own repository
# is a real and reachable default -- unlike a placeholder, which is what gets
# a request refused. An address set in .env is still better, because it reaches
# a person rather than an issue tracker.
PROJECT_URL = os.getenv(
    "AFFLUENSE_PROJECT_URL",
    "https://github.com/ajaykarthik-ip/"
    "HNI-Relationship-Risk-Intelligence-with-Single-Agent-and-Multi-Agent-"
    "Orchestration",
)

USER_AGENT = (
    f"affluense-screening/2.0 ({PROJECT_URL}; contact: {CONTACT}) python-httpx"
    if CONTACT else
    f"affluense-screening/2.0 (+{PROJECT_URL}) python-httpx"
)

# Hosts that enforce the policy above, so a 403 from one of them can say what
# to do about it rather than reading as an outage.
CONTACT_SENSITIVE_HOSTS = ("wikipedia.org", "wikidata.org", "wikimedia.org")


# --- transport -------------------------------------------------------------
# One client for the whole run. HTTP/2 multiplexes several requests over one
# connection, which matters when six coroutines are talking to the same feed.
HTTP2 = os.getenv("V2_HTTP2", "1") not in ("0", "false", "False")
CONNECT_TIMEOUT = _float("V2_CONNECT_TIMEOUT", 10.0)
READ_TIMEOUT = _float("V2_READ_TIMEOUT", float(v1.DEFAULT_TIMEOUT))
POOL_LIMIT = _int("V2_POOL_LIMIT", 64)

# Firecrawl's structured scrape runs a model over the page, and routinely takes
# longer than V1's 25s default. Under V1 that raised, retried three times and
# burned 75 seconds to return nothing. Giving it room is *faster*, not slower.
FIRECRAWL_SCRAPE_TIMEOUT = _float("V2_FIRECRAWL_SCRAPE_TIMEOUT", 120.0)
FIRECRAWL_SEARCH_TIMEOUT = _float("V2_FIRECRAWL_SEARCH_TIMEOUT", 60.0)

# robots.txt under V1 went through urllib with no timeout at all, off the
# pooled session, uncached and unmetered. One dead publisher stalled the run.
ROBOTS_TIMEOUT = _float("V2_ROBOTS_TIMEOUT", 8.0)

# --- concurrency -----------------------------------------------------------
# How many entity-level evidence agents may be in flight. The real ceiling is
# the per-host budget in hostpolicy.py; this only stops the task list itself
# from growing without bound.
MAX_ENTITY_CONCURRENCY = _int("V2_MAX_ENTITY_CONCURRENCY", 12)
# Article bodies in flight. Per-domain concurrency stays at 1 (hostpolicy),
# so this is a cap across *different* publishers.
MAX_FULLTEXT_CONCURRENCY = _int("V2_MAX_FULLTEXT_CONCURRENCY", 12)
# Extraction batches in flight.
MAX_ANALYST_CONCURRENCY = _int("V2_MAX_ANALYST_CONCURRENCY", 8)
# Firecrawl scrapes in flight.
MAX_FIRECRAWL_CONCURRENCY = _int("V2_MAX_FIRECRAWL_CONCURRENCY", 4)
# SPARQL fan-out for network candidate generation.
MAX_SPARQL_CONCURRENCY = _int("V2_MAX_SPARQL_CONCURRENCY", 2)

# Threads for V1's synchronous source modules, reached through bridge.SyncFacade.
MAX_BRIDGE_THREADS = _int("V2_MAX_BRIDGE_THREADS", 16)

# --- budgets ---------------------------------------------------------------
# A run-scoped ceiling on OpenAI calls, enforced in the transport so it holds
# for every call site including V1 code reached through the bridge.
#
# V1's budgets are per *instance*: extract.analyse builds a fresh client per
# company, so its documented "one call per company" ceiling is really
# OPENAI_MAX_EXTRACT_CALLS x companies. Under concurrency an unbounded budget
# becomes an unbounded bill, so V2 caps the run. The default sits well above
# any realistic V1 run -- it bounds a loop bug, it does not trim coverage.
OPENAI_MAX_CALLS_PER_RUN = _int("V2_OPENAI_MAX_CALLS", 200)

# Headroom over the batches actually needed, for retries and clustering.
OPENAI_EXTRACT_HEADROOM = _int("V2_OPENAI_EXTRACT_HEADROOM", 4)

# PS2's news-based peer discovery reads names out of headlines. Its own
# ceiling, so widening the industry fan-out can never eat the screening's
# extraction allowance.
OPENAI_MAX_PEER_CALLS = _int("V2_OPENAI_MAX_PEER_CALLS", 24)

# --- retries ---------------------------------------------------------------
MAX_ATTEMPTS = _int("V2_MAX_ATTEMPTS", 3)
BACKOFF_BASE = _float("V2_BACKOFF_BASE", 0.75)
BACKOFF_CAP = _float("V2_BACKOFF_CAP", 12.0)

# Statuses that mean "slow down", not "go away". V1's set, plus timeouts which
# V1 never retried as a status because they arrive as an exception.
RETRY_STATUS = set(v1.RETRY_STATUS)
