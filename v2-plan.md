# Affluense V2 — plan

A concurrent execution engine alongside V1, not a replacement for it. V1 stays
frozen and runnable as the stable baseline; V2 is a sibling package with its own
orchestration layer that reuses V1's deterministic components wholesale.

Status: **plan only.** No code written yet.
Companion document: `v1-flow.md` (how the current system works).

---

## 0. One thing to settle first

`CLAUDE.md` says: *"It is a multi-source orchestration pipeline, not a multi-agent
system. Deterministic Python controls the flow and decides what is true. Do not
introduce agents, planners or autonomous loops."* The V2 brief asks for multi-agent
orchestration. These are reconcilable, and the reconciliation is the design:

**In V2, "agent" means a bounded concurrent stage worker with a typed input/output
contract.** No planner. No autonomous loop. No agent decides a risk score. The
orchestrator is deterministic Python that owns the DAG, budgets, retries and
cancellation. What changes versus V1 is *scheduling*, not *authority*.

That matters because of the second finding: **the 500–600s is not a single-agent
problem, it is an I/O scheduling problem.** Adding LLM agents would make it slower.
Every second of the current runtime is spent either sleeping in a rate-limit gate or
blocking on a serial HTTP call. V2's speedup comes from concurrency, not from agents.
The agent decomposition is worth doing anyway — it is what makes the concurrency
legible and testable — but it is the packaging, not the mechanism.

---

## 1. V1 preservation

### Isolation model

**A sibling package, `backend/affluense_v2/` — not `affluense/v2/`.**

Why sibling: a subpackage shares `affluense/__init__.py`, shares the test suite's
import surface, and — the real risk — makes `from .. import http` natural, which is
one keystroke from "just add an async mode to `Fetcher`". A sibling can only reach V1
through explicit absolute `from affluense import ...` imports. That is read-only by
construction and visible in every diff.

### The one-way import rule

> **V2 imports V1. V1 never imports V2. Ever.**

This single rule is the isolation guarantee. Enforced mechanically in
`test_v2_isolation.py`:

```python
def test_v1_never_imports_v2():
    for path in pathlib.Path("affluense").rglob("*.py"):
        assert "affluense_v2" not in path.read_text()
```

### The unchanged-V1 guarantee

Three layers, in increasing strength:

1. **The existing 134 tests in `test_affluense.py` + 9 in `test_api.py`.** They already
   lock V1's behaviour. They must stay green at every phase.
2. **A file hash manifest.** `test_v2_isolation.py` also hashes every file under
   `backend/affluense/` against a checked-in `v1-manifest.json`. Any accidental edit to
   a V1 module fails the suite immediately, with the filename. Cheap, and it turns
   "please don't touch V1" into something the machine checks instead of something a
   human has to remember.
3. **Git.** Tag the current commit `v1-baseline` before Phase 1 so V1 is recoverable by
   hash, not by memory.

### The one unavoidable exception: `api.py`

`api.py` must change — something has to route `engine: "v2"` somewhere. There is no way
around this if both engines share one job store and one polling endpoint.

A separate `api_v2.py` mounted as a FastAPI sub-app was considered. Cleaner on paper,
but it forces two job stores and two base URLs onto the frontend, which breaks shared
polling, shared cancellation and the comparison view — the whole point of the exercise.
Not worth it.

**Recommended:** an additive edit of roughly 15 lines.

- Add `engine: Literal["v1", "v2"] = "v1"` to `ScreeningRequest` and `NetworkRequest`.
- Add `engine` to `Job` and to `Job.summary()`.
- Replace the direct `run_screening(...)` call with a dispatch dict lookup.

Default `"v1"`. Every existing caller — the current frontend, `run_screening.py`, the
tests — is byte-identical in behaviour. `api.py` is excluded from the hash manifest and
gets its own before/after assertion instead: a test that posts without `engine` and
asserts the V1 path ran.

`run_screening.py`, `run_network.py`, and all 30 modules under `affluense/` are touched
by nothing.

---

## 2. V2 architecture

### The dependency graph is not the obvious one

The sketched architecture puts Identity, Company and News/Risk as siblings. Two
corrections from the code:

**Correction 1 — News/Risk cannot be a sibling of Company.** `collect_for`
(`pipeline.py:629`) builds its query set from `record["canonical_name"]`, which only
exists after discovery and `company_resolve.merge`. Company news is strictly downstream
of company discovery.

**Correction 2 — but *person* news genuinely is a sibling, and V1 gets this badly
wrong.** `queries.for_person(resolved, company)` needs only the resolved name and the
user's typed company string. It has no dependency on discovery at all. Yet V1 runs it
*dead last*, at `pipeline.py:784`, after the entire company screening loop has finished.
That is 20 serial HTTP requests plus a full-text pass plus an OpenAI extraction call
sitting on the critical path for no reason. **Moving person evidence to start the
instant identity resolves is free latency, and it is the clearest single structural win
in the codebase.**

### The real DAG

```
                            ORCHESTRATOR  (deterministic, asyncio)
                     owns: DAG · budgets · retries · cancellation · progress
                                          │
                                   ┌──────┴──────┐
                                   │  IDENTITY   │  subject + confirmed entity
                                   └──────┬──────┘
                        ┌─────────────────┼─────────────────┐
                        │                 │                 │
                        ▼                 ▼                 ▼
              ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
              │  DISCOVERY   │   │  EVIDENCE    │   │  (PS2 only)  │
              │              │   │  [person]    │   │   NETWORK    │
              │ wikidata ∥   │   │              │   │   waits on   │
              │ firecrawl ∥  │   │ starts NOW,  │   │   discovery  │
              │ seed         │   │ not last     │   │              │
              └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
                     │                  │                  │
          merge + entity resolution     │                  │
          (deterministic, ~0ms)         │                  │
                     │                  │                  │
                     ▼                  │                  │
        ┌────────────────────────┐      │                  │
        │  EVIDENCE  × N         │      │                  │
        │  one per company,      │      │                  │
        │  all concurrent        │      │                  │
        └────────┬───────────────┘      │                  │
                 └──────────┬───────────┘                  │
                            ▼                              │
              ┌───────────────────────────┐                │
              │ ASSIGN  (SERIAL — must be)│                │
              │ cross-company dedupe,     │                │
              │ deterministic ordering    │                │
              └────────────┬──────────────┘                │
                           ▼                               │
              ┌──────────────────────────┐                 │
              │  FULLTEXT                │                 │
              │  concurrent, one global  │                 │
              │  budget, robots prefetch │                 │
              └────────────┬─────────────┘                 │
                           ▼                               │
              ┌──────────────────────────┐                 │
              │  ANALYST  (LLM)          │                 │
              │  batched, concurrent     │                 │
              │  across companies        │                 │
              └────────────┬─────────────┘                 │
                           ▼                               │
              ┌───────────────────────────┐                │
              │  CLUSTER → findings       │                │
              │  deterministic; LLM only  │                │
              │  on stage contradiction   │                │
              └────────────┬──────────────┘                │
                           └───────────┬───────────────────┘
                                       ▼
                    ┌────────────────────────────────────┐
                    │  VERDICT — NOT AN AGENT            │
                    │  validate.py → risk_score.assess   │
                    │  → qc.review (1 LLM call)          │
                    │  100% reused from V1, unchanged    │
                    └────────────────────────────────────┘
```

### Six agents

| # | Agent | LLM? | Concurrency |
|---|---|---|---|
| 1 | Identity | LLM for candidate ranking only (already V1) | — |
| 2 | Discovery | No (Firecrawl's own extraction aside) | 2 internal branches ∥ |
| 3 | Evidence | No | N+1 instances ∥ |
| 4 | Fulltext | No | ~12 in flight |
| 5 | Analyst | **Yes** | batches ∥ |
| 6 | Network | 1 LLM call (rationales) | ∥ with 3–5 |

### What stays deterministic and never becomes an agent

Non-negotiable, and it is most of the value of the system:

- `validate.py`, `risk_score.py` — the verdict. Agents produce evidence; Python
  produces the number.
- `enrich/sentiment.py` (VADER), `enrich/risk.py` (keyword prefilter),
  `enrich/relevance.py`, `enrich/tenure.py`, `enrich/publishers.py` — all pure functions.
- `resolve/company.py` merge and normalisation.
- `news.dedupe` and cross-company article assignment.
- `output.py` report assembly.
- The DAG itself, budgets, retries, cancellation.

---

## 3. Parallelism

### Safe to parallelise

| Operation | V1 today | V2 |
|---|---|---|
| Google News RSS | 1 req/s, gated on `news.google.com` | 6 concurrent, token bucket |
| Bing News RSS | 1 req/s, gated on `bing.com` | 6 concurrent, token bucket |
| Google ∥ Bing per query | already parallel-ish across workers | explicit `gather` per query |
| 15 queries within one entity | **serial** (`news.py:152`) | all 15 concurrent |
| Company-level research | 6 threads, but starved by host gates | N concurrent, real parallelism |
| Person evidence | **runs last, fully serial** | starts at t=0 alongside discovery |
| Full-text retrieval | **fully serial** (`fulltext.py:164`) | ~12 concurrent, 1/domain |
| robots.txt | **serial, untimed, uncached** | prefetched concurrently, disk-cached |
| OpenAI extraction | **serial per company** | concurrent batches |
| Firecrawl searches | **serial** (`pipeline.py:448`) | 4 concurrent |
| Firecrawl scrapes | **serial** (`pipeline.py:463`) | 4 concurrent |
| Wikidata claims / companies_for / coofficers | serial | 3 concurrent |
| Network candidate SPARQL | nested serial loops (`discover.py:337`) | industry × role fan-out |
| PS1 screening ∥ PS2 network | **two separate jobs, duplicate discovery** | one discovery, both consume |

### Must stay sequential — and why

1. **Article assignment / cross-company dedupe.** `pipeline.py:759-769`. V1's own comment
   is right: doing this in the pool hands a shared story to whichever worker finished
   first, making runs non-reproducible. Keep it a serial pass over the deterministically
   sorted `targets`. It costs microseconds.
2. **Full-text budget accounting.** `MAX_FULLTEXT_FETCHES` is one global budget.
   Concurrent *fetching* is fine; concurrent *budget claiming* is not. Claim slots under
   a lock in one pass, then fan out on the claimed list — deterministic selection,
   concurrent execution.
3. **Extraction → clustering → findings → scoring → QC.** A hard chain. Clustering needs
   every article's `extracted` field; scoring needs every finding; QC needs the assembled
   report.
4. **Firecrawl API version probe.** `_call` (`firecrawl.py:150`) tries v2 then v1 and
   memoises on `self.version`. Six concurrent scrapes on a cold client would each
   independently probe — potentially **six wasted paid calls**. Probe once, serially,
   then fan out.
5. **Identity resolution.** Everything depends on it.

### Mechanism: asyncio, and no new dependency

`httpx>=0.27` is already in `requirements.txt` and already installed. Use
`httpx.AsyncClient` with `asyncio.TaskGroup` and per-host semaphores. **Do not add
aiohttp, celery, ray, langgraph, or an agent framework.** None of them solve anything
here.

Replace V1's "1 second between requests per host" with **per-host concurrency + token
bucket**, in `affluense_v2/hostpolicy.py`:

| Host class | Concurrent | Rate | Rationale |
|---|---|---|---|
| `news.google.com`, `bing.com` | 6 | 5/s | Public keyless RSS built for aggregation |
| `wikidata.org`, `wikipedia.org`, `query.wikidata.org` | 2 | 2/s | WMF asks for restraint; low volume anyway |
| `api.openai.com` | 8 | 8/s | Paid API with its own documented limits |
| `api.firecrawl.dev` | 4 | 4/s | Paid API |
| publisher domains (fulltext) | **1 per domain**, 12 global | 1/s/domain | Politeness to small publishers preserved exactly |

Politeness is *preserved where it is owed* — to individual publishers — and relaxed
where it is not: two aggregators built for machine consumption, and two paid APIs with
a contract in place.

### The V1 source modules not worth rewriting

`wikidata.py`, `wikipedia.py`, `web.py` take a `fetcher` and call `.get_json()`
synchronously. Rewriting all of them async is a lot of work for stages that account for
a small share of runtime.

**`affluense_v2/bridge.py`** solves this. It exposes a `SyncFacade` with `.get()`,
`.get_json()`, `.post_json()`, `.note()`, `.robots_allow()`, `.meter`, `.cache` — the
exact surface V1's source modules use — and forwards each call to the async transport
via `asyncio.run_coroutine_threadsafe(...).result()`. Run those source functions inside
`asyncio.to_thread`. Result: `wikidata.companies_for(facade, qid)` works **unchanged**,
shares the same cache, the same meter, the same host limiter, and **no V1 file is
touched**.

Only the four high-volume paths get true async: news, fulltext, OpenAI, Firecrawl. That
is where all the time is. This is the single biggest scope reduction in the plan.

---

## 4. Bottlenecks — verified against the code

**Request counts below are exact** (derived from config defaults and loop structure).
**Latencies are assumptions**, not measurements. Per the project's no-fabricated-numbers
rule, Phase 0 measures them before anything is optimised.

### Confirmed

**(a) The host gate is the master constraint.** `Fetcher._wait` (`http.py:65-81`) keys on
`netloc` at `DEFAULT_DELAY = 1.0`, and `api.py` never overrides it. The entire news stage
talks to exactly **two** hostnames. So the run's news throughput ceiling is **2 requests
per second, total** — regardless of `workers: 6`. The six threads spend most of their
life in `time.sleep(wait)`. `_wait` stamps `_host_last_call` at slot-grab time, so it
gates request *starts*, not start-to-finish — the floor is exactly
`n_requests × delay` per host.

**(b) News volume.** `for_company` = 14 adverse groups + 1 general = 15 queries (capped
at `MAX_QUERIES_PER_ENTITY=15`). `news.collect` hits both feeds (`news.py:131-132`) = 30
requests/company. `for_person` = 9 groups + 1 = 10 queries = 20 requests. At 8 companies:
**260 requests across 2 hosts → 130 each → ~130s floor.** At `max_companies=12`: ~190s.

**(c) The serial analysis loop.** `pipeline.py:772` runs `screen()` one company at a time.
Deliberate, and the stated reasons (shared budgets, reproducibility) are legitimate — but
they argue for *deterministic selection*, not *serial execution*. Both are achievable.

**(d) OpenAI budgets are per-instance, not per-run.** `extract.analyse` constructs a
fresh `OpenAIClient(budget=OPENAI_MAX_EXTRACT_CALLS)` on **every call**
(`extract.py:214`) — so the real ceiling is 20 × 12 companies = 240, not the "one
extraction call per company plus the person" claimed in the comment at `config.py:106`.
Same pattern in `cluster._resolve_contradiction` (`cluster.py:100`), where
`OPENAI_MAX_CLUSTER_CALLS=6` is per *finding*. `relationships.resolve_unknowns`
(`budget=1`) and `qc.review` are genuinely once-per-run.

This is a **correctness issue in V1's cost ceiling**, not just a performance one. With
concurrency, an unbounded budget becomes an unbounded bill very quickly. V2 uses a
run-scoped `budgets.py` object. *(Not proposed as a V1 fix — V1 is frozen. Recorded here
because it changes what V2 must not inherit.)*

**(e) Full-text is the worst code path in the system.** `fulltext.fetch`
(`fulltext.py:164`) loops serially over up to 40 articles. Each one pays twice:

- `fetcher.robots_allow(url)` → `urllib.robotparser.read()` (`http.py:200`) — **no
  timeout, not through the pooled session, not disk-cached, not metered, not
  rate-limited.** One unresponsive publisher blocks the entire pipeline on an OS-default
  socket wait. This is also invisible in `usage.py`, so it never appears in any cost or
  request count.
- Then the page GET (1s host gate + latency) and a BeautifulSoup parse.

**(f) Firecrawl is the highest-variance stage.** 4 searches then 6 structured scrapes,
all serial (`pipeline.py:448`, `pipeline.py:463`). A structured scrape runs a model over
the page. `post_json` defaults to `DEFAULT_TIMEOUT = 25` with `attempts=3` — and
`RETRY_STATUS` does not cover timeouts, so a slow scrape raises `RequestException` and
retries: **75 seconds burned per page, producing nothing**, while the credit is still
metered on dispatch. Six pages worst-case is 450s of pure timeout. If this is firing, it
alone explains the gap between a 300s run and a 600s one.

### Hidden sequential loops

- **`discover.generate` (`network/discover.py:337`)** — nested
  `for industry_qid: for prop, role:` issuing SPARQL serially, each against
  `SPARQL_TIMEOUT=75`. With `industries=3, roles=3` that is up to 9 serial SPARQL queries.
- **`industries_for_companies` (`discover.py:212`)** — serial `claims()` over 8 companies.
- **`industries_from_sectors` (`discover.py:181`)** — serial `resolve_sector` over 8
  sectors, each doing a search *plus* a SPARQL.
- **`_corroborated_stakes` (`pipeline.py:325`)** — serial `wikidata.search_entities` per
  promoted stake.
- **`JobStore(max_workers=2)`** (`api.py`) — only two jobs run at once. In `mode: "both"`,
  screening and network run **as two separate jobs that each re-resolve identity and
  re-run `discover_companies` from scratch**, including a second full Firecrawl pass. The
  disk cache absorbs some of this (Firecrawl TTL is 7 days), but not the latency of a
  cold first run.

### Modelled budget, cold cache, 8 companies

| Stage | Modelled | Driver |
|---|---|---|
| Identity | 5–10s | ~5 requests, one host |
| Wikidata discovery | 20–40s | 3–6 SPARQL, serial |
| **Firecrawl discovery** | **60–450s** | 10 serial paid calls; timeout retries |
| **Company news** | **120–150s** | 240 req ÷ 2 hosts × 1s |
| **Full-text** | **100–180s** | ≤40 × (untimed robots + gated GET + parse) |
| **OpenAI extraction** | **200–380s** | ~24 serial calls × (1s gate + latency) |
| Person evidence | 25–60s | 20 req + fulltext + 1 LLM call, all last |
| Cluster / validate / score / QC | 10–25s | mostly deterministic + 1 LLM call |

Sums to the observed 500–600s with Firecrawl behaving, and explains the tail when it
does not. **Phase 0 replaces every one of these numbers with a measurement.**

---

## 5. Performance strategy

No reduction in query coverage, article counts, or evidence checks anywhere in this plan.
`MAX_QUERIES_PER_ENTITY`, `NEWS_PER_QUERY`, `MAX_FULLTEXT_FETCHES` and the 14 adverse
groups are **identical in V2**. That is also what makes the benchmark fair.

### High impact

| # | Change | Modelled | Mechanism |
|---|---|---|---|
| H1 | Per-host concurrency for news | 120–150s → **25–40s** | 6 concurrent + token bucket, 2 hosts |
| H2 | Concurrent OpenAI extraction | 200–380s → **40–70s** | batches in flight across companies |
| H3 | Concurrent full-text + robots cache | 100–180s → **25–40s** | 12 in flight, 1/domain; robots prefetched, timed, disk-cached |
| H4 | Firecrawl: right timeout + concurrent | 60–450s → **30–60s** | 120s timeout for structured scrapes, 4 concurrent, version probed once |
| H5 | Person evidence at t=0 | saves **25–60s** | removes it from the critical path entirely |

H4 is worth stressing: raising the timeout is *faster*, not slower. Today a 40-second
scrape costs 75 seconds and returns nothing. Giving it 120 seconds costs 40 and returns
the page.

### Medium impact

| # | Change | Modelled |
|---|---|---|
| M1 | Discovery fan-out (wikidata ∥ firecrawl ∥ seed) | 15–30s |
| M2 | Shared discovery for `mode: "both"` | 80–500s **in that mode only** |
| M3 | Concurrent SPARQL in network candidate generation | 20–60s (PS2) |
| M4 | Run-scoped budgets | bounds cost; prevents concurrency multiplying spend |
| M5 | `JobStore(max_workers=4)` | benchmark throughput only |

### Low impact

| # | Change |
|---|---|
| L1 | Atomic cache writes (tmp + `os.replace`) — correctness under concurrency, not speed |
| L2 | HTTP/2 on `AsyncClient` |
| L3 | Skip `_extract_text` on obvious non-articles before parsing |
| L4 | Reuse one `AsyncClient` across the whole run |

### Target

**500–600s → 100–180s. A 3–5× improvement, with identical coverage.** Contingent on
Phase 0 confirming the split. If Phase 0 shows Firecrawl dominating, H4 alone may get
most of it.

---

## 6. Agent design

### 1. Identity Agent

- **In:** `name`, `company?`, `confirmed_entity?`
- **Out:** `Subject`, candidate list, biography
- **Sources:** Wikipedia (titles + REST summary), Wikidata; OpenAI for ranking only
- **Concurrent:** Wikipedia ∥ Wikidata lookups
- **Depends on:** nothing
- **LLM:** only `OPENAI_MAX_CANDIDATE_CALLS` for ranking, exactly as V1
- **Fallback:** no key → deterministic scorer. No Wikidata → subject with
  `wikidata_id=None`; downstream degrades as in V1.
- **Reuse:** `resolve/person.py`, `resolve/candidates.py` via `SyncFacade`, unchanged

### 2. Discovery Agent

- **In:** `Subject`, `company?`
- **Out:** discovered companies, claims, connections, investments, firecrawl block
- **Sources:** Wikidata SPARQL + entity API, Firecrawl search/scrape
- **Concurrent:** three internal branches in a `TaskGroup` — (a) `claims` +
  `companies_for` + `coofficers`, (b) 4 Firecrawl searches then 4 concurrent scrapes,
  (c) seed company (instant). Version probe serial before the scrape fan-out.
- **Depends on:** Identity
- **LLM:** no — Firecrawl's structured extraction is a source feature, billed as credits
- **Fallback:** no Firecrawl key → Wikidata + seed only (V1 behaviour). Branch failure is
  contained; the other branches still deliver, and the failure lands in `notes` and in
  `coverage`.
- **Reuse:** `pipeline._split_roles`, `_investments`, `_controlling_investments`,
  `_corroborated_stakes`, `_associates`, `resolve/company.merge` — all pure, imported
  directly

### 3. Evidence Agent (N+1 instances)

The workhorse. One instance per company, plus one for the person.

- **In:** entity name + kind (`company` | `person`), aliases, sectors, subject name,
  owner domains
- **Out:** filtered, annotated, promotional-split article list + `queries_issued` +
  `groups_checked`
- **Sources:** Google News RSS, Bing News RSS
- **Concurrent:** all 15 queries × 2 feeds concurrent within an instance; all N+1
  instances concurrent with each other. Bounded by the host token bucket, not by a worker
  count.
- **Depends on:** company instances → Discovery + merge. **Person instance → Identity
  only.**
- **LLM:** none
- **Fallback:** a failed feed is a note, not an exception; the other feed's articles still
  count. A fully failed entity yields `insufficient_coverage=True`, which `risk_score.py`
  already handles.
- **Reuse:** `collect/queries.py` verbatim, `news._google_item`/`_unwrap_bing`/
  `identity_key`/`dedupe`, `enrich/relevance.py`, `enrich/publishers.py`

### 4. Fulltext Agent

- **In:** the assigned, deduped article set; global budget
- **Out:** `body_text` + `fetch_status` per article; status tally
- **Sources:** publisher sites direct
- **Concurrent:** deterministic selection (existing `select()` sort) and budget claim
  happen in one serial pass; the resulting list is fetched ~12 concurrent, **max 1 per
  domain**. robots.txt for all distinct domains prefetched concurrently first, **with a
  timeout**, and cached to disk.
- **Depends on:** assignment
- **LLM:** none
- **Fallback:** identical semantics to V1 — `blocked` / `paywalled` / `partial` /
  `failed`, never pretend. New: a robots timeout is `blocked` with a note, instead of
  hanging.
- **Reuse:** `fulltext.worth_fetching`, `select`, `_extract_text`, `_PAYWALL`,
  `quote_for` — all pure, imported directly

### 5. Analyst Agent (LLM)

- **In:** articles per entity, entity name, subject name, role period
- **Out:** `article.extracted` populated in place; per-entity tally
- **Sources:** OpenAI chat completions
- **Concurrent:** batches of `OPENAI_EXTRACT_BATCH=12` issued concurrently across all
  entities, up to 8 in flight
- **Depends on:** Fulltext
- **LLM:** yes — the only genuinely LLM-native agent
- **Fallback:** no key → keyword annotation, `backend="keyword"`, zero behaviour change.
  Budget exhausted → remaining articles keep keyword annotation with a note, exactly as
  V1. Malformed JSON → discarded, deterministic order untouched.
- **Critical:** `temperature=0` preserved; **budget is run-scoped**, so concurrency cannot
  multiply spend
- **Reuse:** `extract._payload_for`, `_apply`, `normalise_category`, `keep_relevant`, the
  system prompt — verbatim. Only the call scheduling is new.

### 6. Network Agent (PS2)

- **In:** `Subject`, merged companies, claims, associates
- **Out:** profile, current network, ranked suggestions
- **Sources:** Wikidata SPARQL
- **Concurrent:** industry × role candidate SPARQL fan-out; `industries_for_companies`
  concurrent; runs in parallel with Evidence/Fulltext/Analyst since it shares only
  Discovery's output
- **Depends on:** Discovery (**not** on screening)
- **LLM:** one batched rationale call (`OPENAI_MAX_NETWORK_CALLS`)
- **Fallback:** no Wikidata entity → V1's explicit "no structured network" result,
  unchanged. No industry → V1's occupation fallback, unchanged.
- **Reuse:** `network/discover.py`, `scoring.py`, `explain.py`, `output.py` — all reused;
  only orchestration changes

### Verdict stage — explicitly not an agent

`validate.validate_report` → `risk_score.assess` → `qc.review`. Imported from V1 and run
unchanged, single-threaded, at the end. **No agent produces a risk level, a score, a
confidence, or a limitation.** Agents produce evidence; `risk_score.py` produces the
verdict. This is the constraint that makes V2 comparable to V1 at all — if the scorer
differed, the benchmark would be meaningless.

---

## 7. Shared infrastructure

### Reuse unchanged (import, never edit)

| Module | Why it is safe |
|---|---|
| `models.py` | Pure dataclasses. **Must** be shared — identical output shape is the whole benchmark. |
| `collect/queries.py` | Pure templates. **Must** be shared — identical query coverage. |
| `enrich/sentiment.py` | VADER, stateless per instance |
| `enrich/risk.py`, `relevance.py`, `tenure.py`, `publishers.py` | Pure functions |
| `enrich/cluster.py` | Pure except one optional LLM call |
| `enrich/extract.py` | Prompt + `_payload_for` + `_apply` are pure |
| `collect/fulltext.py` | `worth_fetching`, `select`, `_extract_text`, `quote_for` all pure |
| `resolve/company.py`, `person.py`, `candidates.py`, `relationships.py` | Deterministic |
| `validate.py`, `risk_score.py`, `qc.py` | **The verdict. Non-negotiable.** |
| `output.py` | Identical report shape for both engines |
| `usage.py` `UsageMeter` | Already `threading.Lock`-protected; safe from the event loop |
| `sources/wikidata.py`, `wikipedia.py`, `web.py` | Via `SyncFacade` + `to_thread`, unchanged |
| `sources/news.py` parsers | `_google_item`, `_unwrap_bing`, `identity_key`, `dedupe` |
| `sources/firecrawl.py` | `rank_for_scraping`, `scrape_priority`, `is_skipped`, `PROFILE_SCHEMA`, `PROFILE_PROMPT` |
| `cache.py` | `ResponseCache.key` and `ttl_for` reused; V2 wraps rather than edits |

### Must NOT be shared

| Thing | Why |
|---|---|
| **`http.Fetcher`** | V2 needs async + per-host semaphores + token buckets. Sharing means editing `http.py`, which means editing V1. V2 gets `affluense_v2/transport.py`. The `SyncFacade` gives V1's source modules the *interface* without sharing the *implementation*. |
| **`pipeline.Options` / `pipeline.run`** | V2 gets its own. Reusing would couple V2's knobs to V1's dataclass. |
| **`config.py` constants** | Read them; never write them. V2's concurrency and timeout knobs live in `affluense_v2/config.py`. A timeout change must never reach V1. |
| **`api.py` `_PHASES` regexes** | V1 infers progress by regexing printed strings. V2 emits structured progress events. Two separate `run_progress` paths, selected by `job.engine`. |
| **`ResponseCache.set`** | Non-atomic `write_text`. Under V1's 1 req/s it is fine; under V2's concurrency two writers can interleave. `get()` catches `ValueError` and counts a miss, so it *degrades safely* — but V2 gets `cache_async.py` writing tmp + `os.replace`. **Same directory, same key function, same TTLs** — so a V1 run and a V2 run still share cache entries, which matters for benchmarking. |
| **`Firecrawl.version`** | Per-instance already. V2 must probe once before fanning out. |

### One subtlety worth naming

V2 writes to the **same `.cache/` directory** with the **same key derivation**. That is
intentional — it lets a V2 run reuse a V1 run's Firecrawl scrapes (7-day TTL) instead of
paying twice. But it means the benchmark **must** control cache state explicitly, or V2
wins for free. Section 9 handles this.

---

## 8. Frontend

### Naming collision, first

`src/components/v2/` already exists and means **the redesigned UI**, not the engine. Do
not overload it. Use `Engine` as the type name and put the selector at
`src/components/engine-selector.tsx`, outside `v2/`.

### API design

```python
# api.py — additive only
Engine = Literal["v1", "v2"]

class ScreeningRequest(BaseModel):
    ...
    engine: Engine = "v1"          # default preserves every existing caller

@dataclass
class Job:
    ...
    engine: Engine = "v1"

SCREENING_ENGINES = {"v1": run_screening, "v2": run_screening_v2}
NETWORK_ENGINES   = {"v1": run_network,   "v2": run_network_v2}
```

`Job.summary()` gains `"engine": self.engine` and `"stage_timings": self.stage_timings`.
Both engines keep the same endpoints, the same job store, the same polling, the same
cancellation, the same CSV route.

`/health` gains `"engines": ["v1", "v2"]` so the UI can hide the selector if V2 is not
deployed.

### Progress

V1's `run_progress` regexes printed strings (`api.py:_PHASES`). That approach does not
survive concurrency — with six evidence agents running at once, "Screening: X" lines
arrive interleaved and the `screened / expected` count becomes meaningless.

So: **branch on `job.engine` inside `run_progress`.** V1's path is untouched, character
for character. V2 emits structured events:

```json
{"stage": "evidence", "done": 5, "total": 9, "percent": 61, "elapsed_s": 34.2}
```

V2 appends a human-readable line to `progress` as well, so the existing `<RunStatus>` log
view keeps working with no change.

### UI

1. **`src/lib/api.ts`** — add `export type Engine = "v1" | "v2";`, add `engine?: Engine`
   to `RunOptions`, add `engine` to the `Job` interface, include
   `engine: options.engine ?? "v1"` in both payload branches of `runJob`.
2. **`src/components/engine-selector.tsx`** — a two-button segmented control matching the
   existing `MODES` control at `page.tsx:322-348`. Same classes, same `role="group"`, same
   blurb pattern. It should look like it was always there.
   - *V1 — Stable pipeline.* "The current engine. Sequential, proven, ~8–10 minutes."
   - *V2 — Concurrent orchestration.* "Same sources and same queries, executed in parallel."
3. **`src/app/page.tsx`** — `const [engine, setEngine] = useState<Engine>("v1")`, render
   the selector under the existing mode control, pass `engine` into `runJob`. Store runs
   keyed by `${kind}-${engine}` so a V1 and a V2 result for the same subject coexist and
   can be compared side by side.
4. **`src/components/v2/run-status.tsx`** — a small engine badge next to the phase label,
   plus `elapsed` from the existing timer.

**V1 behaviour is untouched throughout.** Default `"v1"`, same payload shape plus one
field, same report rendering — `ScreeningReportView` needs no change because
`output.build_report` is shared.

---

## 9. Benchmarking

`backend/bench.py`, run manually.

### Fairness controls

Identical across engines, asserted before the comparison is reported:

- Same `name` / `company` / `confirmed` entity (pre-resolve once, pass to both — so
  identity variance cannot skew it)
- Same `max_companies`, `max_news`
- Same `MAX_QUERIES_PER_ENTITY`, `NEWS_PER_QUERY`, `MAX_FULLTEXT_FETCHES`,
  `OPENAI_EXTRACT_BATCH`
- Same `OPENAI_MODEL`, same Firecrawl plan
- **Same cache state.** This is the one that quietly ruins benchmarks. Give each engine
  its own cold directory: `.cache-bench/<run_id>/v1` and `.cache-bench/<run_id>/v2`. Then
  run a second pass with both warm. Report both — cold is the honest headline, warm is
  what day-to-day use feels like.
- **Run order alternated** across repetitions so news-feed latency drift does not
  systematically favour whichever went second.
- 3 repetitions minimum; report median, not mean.

### Metrics

| Metric | Source |
|---|---|
| Total runtime | wall clock around `run()` |
| Time per stage | V2: native timers. V1: timestamp every `on_progress` line — **no V1 edit needed**, `Options.on_progress` already gives the hook. |
| HTTP requests | `meter.summary()["requests"]`, broken down by host |
| OpenAI calls | `meter.summary()` token block, by `purpose` |
| Prompt / completion tokens | `usage` block from the API — never estimated |
| Firecrawl credits | `meter` credit counters |
| Cache hits / misses | `fetcher.cache.summary` |
| Articles retrieved / after dedupe | `coverage` |
| Queries issued / groups checked | `coverage` |
| Full-text attempted / full / paywalled / blocked | `coverage` |
| Findings count + ids | `report["findings"]` |
| Risk level, score, confidence | `report["assessment"]` |
| Validation issues | `report["validation"]` |
| QC issues | `report["qc"]` |

### Quality gates — V2 fails the benchmark if any of these break

1. `coverage.queries_issued` (V2) **≥** V1. Never fewer.
2. `coverage.groups_checked` (V2) **⊇** V1. Same 14 adverse groups.
3. Finding ids (V2) **⊇** V1's, modulo news that genuinely moved between runs.
4. `assessment.level` **identical**. A different verdict means a bug, not a speedup.
5. `assessment.confidence` identical or higher.
6. OpenAI calls **≤** 1.2× V1. Concurrency must not become a spending increase.
7. Firecrawl credits **≤** V1.

`bench.py` writes `bench-<run_id>.json` and prints a side-by-side table with a per-gate
PASS/FAIL. **Speed with a different answer is a failure, not a win** — that is the whole
point of gates 3–5.

---

## 10. Migration plan

V1 runnable after every phase. Nothing in V1 is edited except the ~15 additive lines in
`api.py` at Phase 1.

**Phase 0 — Measure (no V2 code).** `bench.py` with the `on_progress` timestamper. Run V1
three times cold, three warm. Produce the real stage split. **Every latency number in
section 4 is replaced by a measurement before anything is optimised.** If the split
contradicts the model, the phase order changes.
*Exit: a measured baseline.*

**Phase 1 — Skeleton + the switch.** Create `affluense_v2/` with `config.py`,
`pipeline.py`, `budgets.py`, `progress.py`. `run_screening_v2` **just calls V1's `run()`
in a thread.** Add the `engine` field to `api.py`. Add `test_v2_isolation.py` with the
hash manifest.
*Exit: selecting V2 in the API produces a byte-identical report to V1, and every existing
test is green. The plumbing is proven before any behaviour changes.*

**Phase 2 — Async transport.** `transport.py` (AsyncFetcher, host policy, token buckets),
`cache_async.py` (atomic writes, V1's key/TTL), `bridge.py` (`SyncFacade`). Port Identity
+ Discovery to the orchestrator, still sequential.
*Exit: V2 produces an equivalent report through its own transport. Modest speedup from
discovery fan-out.*

**Phase 3 — Evidence Agent (the big one).** Concurrent news across queries, feeds and
entities. Person evidence moved to t=0. Serial assignment preserved.
*Exit: expect the largest single drop. Gates 1–3 must pass.*

**Phase 4 — Fulltext Agent.** Concurrent fetching, 1/domain, robots prefetched + timed +
cached.
*Exit: fulltext stage down sharply; blocked/paywalled/failed tallies comparable to V1.*

**Phase 5 — Analyst Agent.** Concurrent OpenAI batches, run-scoped budgets.
*Exit: extraction stage down sharply; gate 6 must pass — calls must not exceed 1.2× V1.*

**Phase 6 — Firecrawl + Network.** Concurrent scrapes with a correct 120s timeout and
one-shot version probe. Network Agent parallel to screening; shared discovery for
`mode: "both"`.
*Exit: the variance tail is gone.*

**Phase 7 — Frontend + final benchmark.** Engine selector, engine badge, structured V2
progress. Full 3× cold + 3× warm benchmark, all seven gates.
*Exit: a defensible V1-vs-V2 table.*

Phases 3, 4, 5 are independent of each other. If Phase 0 says extraction dominates, do 5
first.

---

## 11. Risks

| Risk | Reality | Mitigation |
|---|---|---|
| **Race on article assignment** | Real. V1's comment already identifies it. | Assignment stays a serial pass over deterministically sorted `targets`. Never inside a TaskGroup. |
| **Duplicate evidence** | Real. `news.dedupe`'s `seen` set is mutable shared state; concurrent mutation would produce different runs. | Collect concurrently into per-entity lists; dedupe serially afterwards. Concurrency touches *fetching*, never the `seen` set. |
| **Nondeterminism** | The honest risk. Concurrent completion order must not reach the output. | Every fan-out returns results into a **pre-ordered list by index**, never by completion. `temperature=0` preserved. Budgets claimed in deterministic order before fan-out. Gates 3/4 catch regressions. |
| **Increased OpenAI spend** | Real, and worsened by V1's per-instance budgets. | Run-scoped `budgets.py` with an async lock. Ceiling ≤ V1's intended ceiling. Gate 6 fails the build. |
| **Rate limits / 429s** | Real for Bing and Google at 6 concurrent. | Token bucket *plus* semaphore. Adaptive backoff: a 429 halves that host's bucket for the rest of the run and notes it. Per-publisher stays at 1 concurrent. If sustained 429s appear, lower the news bucket — coverage is never reduced, only pacing. |
| **Firecrawl double-billing on version probe** | Real — 6 concurrent cold scrapes could each probe v2→v1. | Probe once serially, memoise, then fan out. |
| **Cache corruption** | Low; V1 already degrades safely (`get` catches `ValueError` → counts a miss). | `cache_async.py` writes tmp + `os.replace`. |
| **Cancellation** | V1's cooperative model (raise inside `on_progress`) does not map to asyncio. | `asyncio.Event` checked at await points + `TaskGroup` cancellation. Critically: cancellation must not leave a paid Firecrawl call in flight uncounted — the meter records on dispatch, which already handles this. |
| **Error handling / partial failure** | With N concurrent agents, one failure must not sink the run. | Every agent returns a result-or-failure record. A failed entity yields `insufficient_coverage=True`, which `risk_score.py` already handles and `_limitations` already reports. `TaskGroup`'s fail-fast is wrong here — use `gather(return_exceptions=True)` at agent boundaries. |
| **Shared mutable state** | `UsageMeter` is lock-protected. `fetcher.notes` is lock-protected. Article objects are mutated in place by `annotate` — but each article belongs to exactly one entity after assignment, so no two tasks touch the same object. | Verified. Assert it in a test: no article id appears in two entity buckets. |
| **Scope creep into a real agent framework** | The genuine project risk. | No framework. No planner. No autonomous loop. Six functions with typed contracts and an `asyncio` DAG. If a phase starts wanting a graph library, that is the signal to stop. |
| **V1 drift** | Someone edits a V1 module "just a little". | Hash manifest test. Fails loudly with the filename. |

---

## A. Recommended V2 architecture

The DAG in section 2. Summarised: **a deterministic asyncio orchestrator over six
concurrent stage agents, with per-host bounded concurrency replacing V1's global
1-req/s gate, and V1's deterministic verdict stage reused unchanged.**

## B. Agent count

**Six** — Identity, Discovery, Evidence (N+1 instances), Fulltext, Analyst, Network. Plus
a deterministic orchestrator and a deterministic verdict stage that are explicitly *not*
agents. Only one (Analyst) is LLM-native.

## C. Files to add

```
backend/affluense_v2/
  __init__.py  config.py  transport.py  hostpolicy.py  cache_async.py
  bridge.py  budgets.py  progress.py  orchestrator.py  pipeline.py
  agents/
    __init__.py  identity.py  discovery.py  evidence.py
    fulltext.py  analyst.py  network.py
backend/bench.py
backend/test_v2.py
backend/test_v2_isolation.py
backend/v1-manifest.json
backend/V2-ARCHITECTURE.md

affluense-risk-analysis/src/components/engine-selector.tsx
```

## D. Files to reuse (import, read-only)

`models.py` · `collect/queries.py` · `collect/fulltext.py` (pure fns) ·
`enrich/{sentiment,risk,relevance,tenure,publishers,cluster,extract}.py` ·
`resolve/{person,company,candidates,relationships}.py` ·
`sources/{wikidata,wikipedia,web}.py` (via `SyncFacade`) · `sources/news.py` (parsers) ·
`sources/firecrawl.py` (ranking + schema) · `validate.py` · `risk_score.py` · `qc.py` ·
`output.py` · `usage.py` · `cache.py` (key + TTL) · `config.py` (read-only)

## E. Files to leave untouched

**Everything under `backend/affluense/` — all 30 modules.** Plus `run_screening.py`,
`run_network.py`, `eval_sentiment.py`, `test_affluense.py`, `test_api.py`, and `archive/`.

**One exception:** `backend/api.py` — ~15 additive lines. It is excluded from the hash
manifest and covered by a dedicated default-path test instead.

Frontend edits are additive: `src/lib/api.ts`, `src/app/page.tsx`,
`src/components/v2/run-status.tsx`.

## F. Expected performance

| | Cold cache |
|---|---|
| V1 baseline | 500–600s (to be measured in Phase 0) |
| V2 target | **100–180s** |
| Speedup | **3–5×** |
| Coverage | **identical** — same 15 queries/entity, same 14 adverse groups, same article and fulltext caps |
| OpenAI calls | **≤ 1.2× V1**, budget-enforced |
| Firecrawl credits | **≤ V1** |
| Risk verdict | **identical** — same deterministic scorer |

All modelled from exact request counts × assumed latencies. **Phase 0 replaces the
latency assumptions with measurements before any optimisation is written.**

## G. Implementation steps

0. Instrument and measure V1 → real stage split *(no V2 code)*
1. `affluense_v2/` skeleton + `engine` field + isolation tests → V2 delegates to V1,
   identical output
2. Async transport + cache + `SyncFacade` → Identity, Discovery ported
3. **Evidence Agent** → concurrent news, person evidence at t=0
4. **Fulltext Agent** → concurrent fetch, robots prefetched/timed/cached
5. **Analyst Agent** → concurrent OpenAI, run-scoped budgets
6. **Firecrawl + Network** → concurrent scrapes, correct timeout, shared discovery
7. Frontend selector + full benchmark against all seven gates

V1 runs unchanged after every single one.

---

## Open items before Phase 1

- **Phase 0 is not optional.** If the measured stage split contradicts the model in
  section 4, the phase order should change — and reordering is cheap only before code is
  written against it.
- **V1's OpenAI ceiling is roughly 12× what it documents.** The per-instance budget at
  `extract.py:214` means the real extraction ceiling is 20 calls × 12 companies, not the
  "one per company plus the person" claimed at `config.py:106`. V2 fixes it for itself
  with run-scoped budgets; V1's ceiling stays as-is because V1 is frozen.
