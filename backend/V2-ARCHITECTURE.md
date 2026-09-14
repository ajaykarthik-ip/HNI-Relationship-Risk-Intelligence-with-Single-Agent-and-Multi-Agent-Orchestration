# Affluense V2 — the concurrent engine

V2 is a second execution engine living beside V1, not a replacement for it.
Both are selectable at runtime, both produce the identical report through the
identical `output.build_report`, and V1 remains the stable baseline that V2 is
measured against.

Read `v1-flow.md` first. This document only covers what differs.

---

## The one-sentence version

**V2 changes scheduling, not authority.** Every number in the delivered report
is still produced by V1's deterministic layer — `validate.py` gates it,
`risk_score.py` scores it, `output.py` assembles it. What changed is that the
I/O now happens concurrently, under a per-host policy, instead of serially
behind a single one-second gate.

---

## Why "agents" here does not mean autonomous agents

`CLAUDE.md` is explicit: *"a multi-source orchestration pipeline, not a
multi-agent system … do not introduce agents, planners or autonomous loops."*
That rule is intact.

In V2, an **agent** is a bounded concurrent stage worker with a typed input and
output. It does not plan. It does not loop on its own judgement. It cannot add
work that was not in the graph when the run started. And no agent produces a
risk level, a score, a confidence or a limitation.

The reason V2 is faster has nothing to do with agents. The 500–600 second
runtime was an I/O scheduling problem: every second was spent either sleeping in
a rate-limit gate or blocking on a serial HTTP call. Adding model calls would
have made it slower. The agent decomposition is how the concurrency is made
legible and testable — it is the packaging, not the mechanism.

---

## The dependency graph

```
                      ORCHESTRATOR  (deterministic, asyncio)
               owns: DAG · budgets · retries · cancellation · progress
                                    │
                             ┌──────┴──────┐
                             │  IDENTITY   │
                             └──────┬──────┘
                  ┌─────────────────┼─────────────────┐
                  ▼                 ▼                 ▼
          ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
          │  DISCOVERY   │  │  EVIDENCE    │  │   NETWORK    │
          │ wikidata ∥   │  │  [person]    │  │  (PS2, when  │
          │ firecrawl ∥  │  │ starts here, │  │   requested) │
          │ seed         │  │  not last    │  │              │
          └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
                 │                 │                 │
        merge + entity resolution  │                 │
                 │                 │                 │
                 ▼                 │                 │
        ┌────────────────────┐     │                 │
        │  EVIDENCE × N      │     │                 │
        │  all concurrent    │     │                 │
        └────────┬───────────┘     │                 │
                 └────────┬────────┘                 │
                          ▼                          │
              ┌───────────────────────────┐          │
              │ ASSIGNMENT  (SERIAL)      │          │
              └────────────┬──────────────┘          │
                           ▼                         │
              ┌───────────────────────────┐          │
              │ FULLTEXT                  │          │
              │ claimed in order,         │          │
              │ fetched concurrently      │          │
              └────────────┬──────────────┘          │
                           ▼                         │
              ┌───────────────────────────┐          │
              │ ANALYST (LLM)             │          │
              │ batches concurrent        │          │
              └────────────┬──────────────┘          │
                           ▼                         │
              ┌───────────────────────────┐          │
              │ FINDINGS (per entity)     │          │
              └────────────┬──────────────┘          │
                           └──────────┬──────────────┘
                                      ▼
                   ┌──────────────────────────────────┐
                   │ VERDICT — NOT AN AGENT           │
                   │ validate → risk_score → qc       │
                   │ 100% V1, unchanged               │
                   └──────────────────────────────────┘
```

### The correction to the obvious design

Company evidence *cannot* be a sibling of company discovery: `collect_company`
builds its query set from `record["canonical_name"]`, which only exists after
`company_resolve.merge`.

But **person** evidence genuinely is a sibling. `queries.for_person` needs only
the resolved name and the company the user typed. V1 nonetheless runs it dead
last, after every company has been screened (`affluense/pipeline.py:784`) — 20
serial requests plus a full-text pass plus a model call, sitting on the critical
path for no reason. In V2 it starts the moment identity resolves.

---

## What must stay sequential, and why

Three things. Each would be a bug if it were concurrent.

**Assignment.** `news.dedupe` carries a `seen` set across entities, so which
copy of a syndicated story survives depends on the order entities are visited.
V1's own comment says this. Concurrency here would make two runs over identical
evidence produce different reports.

**Budget claiming.** The full-text allowance is one budget for the whole run.
Claiming it in target order — controlling interests first — means a run that
exhausts it always spends it on the same articles. The *fetching* is concurrent;
only the claiming is ordered.

**The verdict chain.** Clustering needs every article's extraction, scoring
needs every finding, QC needs the assembled report.

Everywhere else, `concurrency.gather_ordered` and `map_bounded` return results
in **submission order, never completion order**. That single rule is what keeps
V2 as reproducible as V1.

---

## Host policy: the core change

V1 gates every request at `DEFAULT_DELAY = 1.0` seconds per netloc
(`affluense/http.py:_wait`). One policy for four very different counterparties —
and because the entire news stage talks to exactly two hostnames, the run is
capped at **two requests per second**, no matter how many workers run.

`affluense_v2/hostpolicy.py` sets it per host:

| Host class | Concurrent | Rate | Why |
|---|---|---|---|
| `news.google.com`, `bing.com` | 6 | 5/s | Keyless public RSS built for aggregation |
| Wikipedia / Wikidata / WDQS | 2 | 2/s | WMF asks for restraint; low volume anyway |
| `api.openai.com` | 8 | 8/s | Paid API with its own documented limits |
| `api.firecrawl.dev` | 4 | 4/s | Paid API |
| **every publisher domain** | **1** | 1/s | Unchanged from V1 |

Politeness is preserved exactly where it is owed — to individual publishers —
and relaxed where there was never an argument for it. Concurrency during
full-text retrieval comes from reading *different* sites at once.

A 429 halves that host's rate for the rest of the run and says so in the notes.

---

## The bridge: why V2 is not a rewrite

`wikidata.py`, `wikipedia.py`, `web.py`, `cluster.py`, `qc.py` and
`relationships.py` all take a `fetcher` and call it synchronously. Rewriting
them async would be a lot of work for stages that are a small share of the
runtime, and every rewritten line is a line that can disagree with V1.

So they are not rewritten. `bridge.SyncFacade` presents exactly the surface
those modules use — `get`, `get_json`, `post_json`, `note`, `robots_allow`,
`meter`, `cache`, `notes` — and forwards each call onto the running loop via
`run_coroutine_threadsafe`. The V1 function executes in a worker thread and
shares V2's cache, meter, host gates and budgets as if it had been written for
them.

Only four paths get real async implementations, because that is where the time
is: **news, full text, OpenAI, Firecrawl.**

---

## Bottlenecks fixed

| V1 behaviour | Where | V2 |
|---|---|---|
| 1 req/s across two news hosts | `http.py:_wait` | per-host concurrency + token bucket |
| 15 queries serial per entity | `news.py:152` | all concurrent |
| Person evidence runs last | `pipeline.py:784` | starts at t=0 |
| Full text fully serial | `fulltext.py:164` | 12 concurrent, 1/domain |
| robots.txt: **no timeout**, uncached, unmetered, off-session | `http.py:200` | prefetched concurrently, timed, cached, metered |
| Firecrawl scrapes serial, 25s timeout → 3 retries → 75s for nothing | `pipeline.py:463` | 4 concurrent, 120s timeout, version probed once |
| OpenAI extraction serial per company | `extract.py` | concurrent batches |
| Nested serial SPARQL for peers | `network/discover.py:337` | industry × role fan-out |
| `mode: "both"` repeats identity + discovery | `api.py` | one discovery, both consumers |

### The two that matter most

**Firecrawl's timeout.** A structured scrape runs a model over the page and
routinely exceeds 25s. `RETRY_STATUS` does not cover timeouts, so a slow page
raises, retries three times and burns 75 seconds returning nothing — while the
credit is still metered on dispatch. **Raising the timeout to 120s is faster,
not slower.**

**robots.txt.** V1 reads it through `urllib.robotparser`, which uses its own
opener with no timeout, off the pooled session, uncached and unmetered. One
unresponsive publisher stalls the whole run, and because it never reaches the
meter the cost is invisible in every usage report you have ever looked at.

---

## Budgets

V1's budgets are per **instance**. `extract.analyse` builds a fresh
`OpenAIClient(budget=OPENAI_MAX_EXTRACT_CALLS)` on every call
(`affluense/enrich/extract.py:214`), so its real ceiling is 20 calls × the
number of companies — not the "one extraction call per company plus the person"
its comment claims. `cluster._resolve_contradiction` does the same per *finding*.

At one request per second that is merely surprising. With eight calls in flight
it is a spending risk. So V2 makes every budget belong to the run:

- **`openai_calls`** — an absolute ceiling enforced *inside the transport*, so
  it binds every call site including V1 clients reached through the bridge,
  which know nothing about V2. Default 200 (`V2_OPENAI_MAX_CALLS`). It bounds a
  loop bug; it does not trim coverage.
- **`openai_extract`** — sized at dispatch from the batches actually queued, so
  every article V1 would have sent still gets sent.
- **`fulltext`** — V1's `MAX_FULLTEXT_FETCHES`, claimed in deterministic order.

Exhaustion is never an error. The caller falls back to the deterministic path
exactly as it does when no key is set, and the run says so in its notes.

*(V1's per-instance ceiling is not fixed. V1 is frozen.)*

---

## Coverage parity

`affluense_v2/config.py` imports V1's evidence limits **by reference**:
`MAX_QUERIES_PER_ENTITY`, `NEWS_PER_QUERY`, `MAX_FULLTEXT_FETCHES`,
`OPENAI_EXTRACT_BATCH`, `FULLTEXT_CHARS`, `CONTROL_STAKE_PERCENT`. The query
ladder itself comes from `collect/queries.py`, unmodified — the same fourteen
adverse groups, adverse first.

`test_v2_isolation.py` asserts this, and `bench.py` fails the benchmark if V2
issued fewer queries or checked fewer groups than V1. **V2 is never allowed to
be faster by asking less.**

---

## Cancellation

Cooperative, for the same reason as V1: there is no safe way to kill work
mid-request.

`api.py`'s `on_progress` raises its own `RunCancelled` when a job is stopped.
V2's `Reporter.say` catches that at the progress boundary, stores the exception,
sets its cancel flag and raises `Cancelled`. The transport checks the flag before
every HTTP call, `gather_ordered` re-raises cancellation rather than swallowing
it, and `pipeline._execute` re-raises the **original host exception** at the top.

So `api.py` still sees the type it already knows how to report, and a stopped
job is marked `cancelled` rather than `error`.

---

## Failure containment

One dead source must not sink a run.

- `gather_ordered` turns a failure into a note and a `None` in place.
- A failed entity still appears in the report, marked `insufficient_coverage` —
  which `risk_score.py` already handles and `_limitations` already reports.
  Silently dropping it would read as "nothing was found", which is a different
  and much stronger claim.
- PS2 failing never costs the PS1 deliverable.
- Cancellation is the one exception: it always propagates.

---

## V1 isolation

> **V2 imports V1. V1 never imports V2.**

Enforced by `test_v2_isolation.py`, which parses every V1 module's imports and
hashes every V1 file against `v1-manifest.json`. An accidental edit fails the
suite and names the file.

`api.py` is the single V1-adjacent file that changed — additively, to route
`engine` to an engine. It is excluded from the manifest and pinned by
behavioural tests instead: omitting `engine` must mean V1, and V1's
string-matching progress path must still work.

Regenerate the manifest only after an intentional V1 change:

```
python test_v2_isolation.py --update
```

---

## Layout

```
backend/affluense_v2/
  config.py          V2 knobs; V1's coverage limits re-exported by reference
  hostpolicy.py      per-host concurrency, token buckets, 429 backoff
  transport.py       AsyncFetcher: cache, retries, meter, robots, OpenAI ceiling
  cache_async.py     V1's keys and TTLs, atomic writes, off the loop
  bridge.py          SyncFacade — run V1's sync sources on the async transport
  budgets.py         run-scoped ceilings, safe from loop and threads
  concurrency.py     gather_ordered / map_bounded: ordered, contained
  progress.py        structured stage events + cooperative cancellation
  orchestrator.py    the DAG
  pipeline.py        Options + run() + run_network(), V1-compatible signatures
  sources/
    news_async.py       V1's parsers, concurrent scheduling
    firecrawl_async.py  one version probe, then concurrent scrapes
    openai_async.py     V1's payload, run-scoped budget
  agents/
    identity.py  discovery.py  evidence.py  fulltext.py  analyst.py  network.py
```

---

## Benchmarking

`bench.py` controls the four things that ruin a benchmark:

- **identity** resolved once and handed to both engines
- **cache** a cold directory per engine (they share a key layout, so one
  directory would let the second engine reuse the first's paid scrapes)
- **order** engines alternate across repetitions
- **coverage** asserted, not assumed

Seven gates. Speed with a different answer is a failure:

1. `queries_issued` ≥ V1
2. adverse groups ⊇ V1
3. findings ⊇ V1 *(soft — news genuinely moves between two cold runs)*
4. risk level identical
5. confidence not lower
6. OpenAI calls ≤ 1.2× V1
7. Firecrawl credits ≤ V1

---

## Tuning

Every knob is an environment variable, and none of them reach V1.

| Variable | Default | What it does |
|---|---|---|
| `V2_OPENAI_MAX_CALLS` | 200 | Absolute OpenAI ceiling for one run |
| `V2_MAX_ENTITY_CONCURRENCY` | 12 | Entity agents in flight |
| `V2_MAX_FULLTEXT_CONCURRENCY` | 12 | Article bodies in flight (still 1/domain) |
| `V2_MAX_ANALYST_CONCURRENCY` | 8 | Extraction batches in flight |
| `V2_MAX_FIRECRAWL_CONCURRENCY` | 4 | Scrapes in flight |
| `V2_FIRECRAWL_SCRAPE_TIMEOUT` | 120 | Structured scrapes need more than 25s |
| `V2_ROBOTS_TIMEOUT` | 8 | V1 had none at all |
| `V2_HTTP2` | 1 | Falls back cleanly if `h2` is absent |

If sustained 429s appear, lower the news bucket in `hostpolicy.py`. Coverage is
never reduced to fix pacing — only the pacing is.
