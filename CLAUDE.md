# Affluense

Screening and network intelligence for high-net-worth individuals. Two
problem statements, one pipeline: *which companies is this person connected
to and what is the public record on each* (PS1), and *who do they know and
who should they meet* (PS2).

# Command rules — read first

**Never execute any of these. The user runs every one of them manually.**

- No installation: `pip install`, `npm install`, `npm ci`
- No builds: `next build`, `npm run build`
- No tests: `pytest`, `npm test`
- No linting or typechecking: `eslint`, `tsc`, `npm run lint`
- No application startup: `uvicorn`, `next dev`, `npm run dev`

Modify code, configuration and documentation only. When work is finished, list
the files changed and tell the user exactly which commands to run themselves.
Reading, grepping and editing are fine; anything that installs, builds, tests,
lints or starts something is not.

# Working rules

- **Fix general rules, not individual cases.** When a screening result is wrong
  for one person, change the rule that produced it and add a test for the rule.
  Never special-case a person or company name. Roughly twenty per-person patches
  were made before this rule existed, and each new name surfaced new ones.
- **Never fabricate a number.** Costs, token counts and exchange rates come from
  counters and API responses, multiplied in Python. An invented figure is worse
  than none, because it looks equally authoritative.

# Layout

- `backend/` — Python. FastAPI (`api.py`) over the pipeline in `affluense/`.
  All logic lives here.
- `affluense-risk-analysis/` — Next.js 16 App Router frontend. Presentation
  only; it never computes a finding.

It is a **multi-source orchestration pipeline, not a multi-agent system.**
Deterministic Python controls the flow and decides what is true. Do not
introduce agents, planners or autonomous loops.

# The two-step flow

The central design decision. Identity is confirmed *before* anything expensive
runs:

```
Input (person, person + company, or company)
  ↓
Cheap candidate search      free sources only, no credits spent
  ↓
OpenAI ranks the candidates optional; deterministic scorer without a key
  ↓
User confirms the entity    POST /api/candidates returns the shortlist
  ↓
Deep research               only now does Firecrawl run
  ↓
Corroboration + entity resolution
  ↓
Company discovery
  ↓
Risk screening (PS1)  |  Network analysis (PS2)
  ↓
VADER sentiment             local, free, reproducible
  ↓
OpenAI on flagged/ambiguous cases only
  ↓
JSON + CSV + UI
```

The pipeline previously guessed an identity silently and researched whatever it
picked. A wrong guess was invisible: a confident 95% match on the wrong person.
`affluense/resolve/candidates.py` asks the question out loud instead, and
`Options.confirmed_entity` carries the answer so the pipeline never re-derives
it.

Three entry shapes, one output: person only, person + company (the company is a
verification signal, never a rescue), and company only (which also returns the
people that company names, because screening is always about a person).

# Where OpenAI is allowed

It is the **reasoning layer, never the search engine.** It only ever sees
evidence a real source already returned.

Use it for:

1. Candidate matching, disambiguation and ranking
2. Ambiguous relationship extraction
3. Risk interpretation of articles that **already** raised a flag
4. Explaining network recommendations

Never use it for:

- Ordinary sentiment — VADER does that. See `backend/ARCHITECTURE.md:237`:
  free, reproducible, defensible to a compliance reader, and 146 articles per
  run would dominate the bill.
- Retrieving facts from memory. It is given text and asked to judge it.

Rules that keep this safe, all in `affluense/sources/openai_client.py`:

- **No key, no change.** Without `OPENAI_API_KEY` everything falls back to the
  deterministic path. Nothing breaks and nothing degrades silently.
- **Cached like any other source.** Calls go through the shared `Fetcher`, so an
  identical prompt is answered from disk and billed once.
- **Budgeted.** Each client has a call ceiling, so a loop bug costs one call.
- **Validated.** Ids the model returns must exist in the input, `temperature` is
  0, and a malformed reply leaves the deterministic order untouched.
- Called over plain HTTPS with `requests`, not the `openai` SDK, so there is no
  dependency to install.

# Deterministic Python keeps

Orchestration, deduplication, corroboration, VADER sentiment, relevance
scoring, validation, output generation.

# Sources

Working and in use: Wikipedia, Wikidata, Wikidata Query Service (SPARQL),
DuckDuckGo Instant Answer, Google News RSS, Bing News RSS, Firecrawl (keyed,
the only metered one), the USD→INR rate endpoint.

Removed, and not to be re-added without fixing the cause: **GDELT** (429 on
every observed run) and **OpenCorporates** (401 without a token). See the
*Sources removed* table in `backend/ARCHITECTURE.md`.

SEC EDGAR is **not** implemented. It covers US filers only, so it returns
nothing for the Indian subjects this tool is tested against. A possible future
source, not a current one.

# Cost model

`affluense/usage.py` meters everything. Firecrawl bills per page and per search
result; OpenAI bills per token, counted from the `usage` block the API itself
returns, never estimated. The USD→INR rate is fetched live with a documented
fallback, and the report always says which was used.

# Secrets

`backend/.env` holds `FIRECRAWL_API_KEY` and `OPENAI_API_KEY`. It is gitignored.
Never print a key, never commit one, and never write a real key into code,
documentation or a commit message.
