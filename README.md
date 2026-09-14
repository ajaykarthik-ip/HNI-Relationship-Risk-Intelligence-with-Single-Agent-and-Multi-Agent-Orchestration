# Affluense

Client screening for high-net-worth individuals. You give it a name and one
connected company; it maps the rest of their corporate footprint, searches the
public record for adverse events, and returns a risk assessment with the
source behind every claim.

Built for two problem statements: **risk screening** (which companies is this
person tied to, and is there anything adverse) and **network intelligence**
(who do they already know, and who should they meet).

---

## How it works

```
Name + one company
        ↓
Candidate search           free sources only, no credits spent
        ↓
You confirm who you meant  ← nothing expensive runs before this
        ↓
Company discovery          Wikidata, Wikipedia, Firecrawl
        ↓
Entity resolution          merge aliases, classify the relationship
        ↓
Corroboration              a stake claimed on a page must be confirmed
        ↓
Evidence collection        14 adverse checks per company, not one name query
        ↓
Full-article retrieval     bodies fetched for the items that look material
        ↓
Evidence analysis          a model reads each article and says what it means
        ↓
Clustering                 one event, however many outlets reported it
        ↓
Risk assessment            deterministic rubric → LOW / MEDIUM / HIGH / CRITICAL
        ↓
Review                     a second read that raises issues, never edits
        ↓
Dashboard · JSON · CSV
```

### Two engines

The same pipeline runs on either of two execution engines, chosen per run:

| | |
|---|---|
| **V1 · Single agent** | The original. One request at a time per host, everything in sequence. Eight to ten minutes for a full screening. |
| **V2 · Multi-agent** | Six agents — identity, discovery, evidence, full text, analysis, network — working concurrently under a per-host policy. Measured at 40 to 130 seconds on the same subjects. |

V2 asks **exactly the same questions**: the same fourteen adverse checks, the
same query ladder, the same evidence caps, the same deterministic scorer. It is
faster because the work is scheduled concurrently, never because there is less
of it. Politeness is unchanged where it is owed — article bodies are still
fetched one at a time per publisher.

V1 is frozen and remains the baseline. A hash manifest of every V1 module is
checked by the test suite, so an accidental edit fails loudly and names the
file. `backend/bench.py` runs both engines on one subject with a cold cache
each and checks seven quality gates — speed with a different answer is a
failure, not a result.

### The identity step

The pipeline used to guess who you meant and research whatever it picked. When
it guessed wrong the mistake was invisible — a confident report about a
different person of the same name. Now it shows you the candidates, with the
evidence for each, and waits. That step costs nothing, so you can search
freely before committing.

### Adverse search, not general search

Asking for a company by name returns what the company publishes. So every
company and the individual are searched across fourteen adverse categories —
fraud, money laundering, corruption, litigation, regulatory, enforcement,
investigation, criminal, insolvency, tax, accounting, governance, sanctions,
controversy — roughly 100–250 queries a run.

That matters both ways. A `LOW` verdict says *which* checks ran and came back
empty, instead of "no negative articles found".

### What a model does, and what it doesn't

The model reads evidence. It never decides the verdict.

It ranks identity candidates, reads each article to determine what happened and
how far the matter has legally progressed, adjudicates when two sources
contradict each other, and reviews the finished report for unsupported claims.

Everything a compliance decision rests on is deterministic Python: the risk
rubric, corroboration counts, deduplication, attribution, the arithmetic. Given
the same findings, the level is always the same, and the reasoning can be
printed. Sentiment is VADER, running locally.

### Three rules that keep it defensible

**An allegation is not a finding.** Every finding carries the stage it has
reached — reported, alleged, investigating, charged, settled, dismissed,
convicted. A tool that renders "accused" and "convicted" alike is a defamation
claim waiting to happen.

**A company's history is not a person's exposure.** Articles published outside
someone's time at a company are shown, and explicitly not attributed to them.
A regulator, a charity or a television appearance never counts as corporate
exposure at all.

**Corroboration counts publishers, not articles.** Ten syndications of one wire
story are one source.

---

## Running it

Two halves. Backend first.

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
cp .env.example .env            # add your keys
uvicorn api:app --reload
```

```bash
cd affluense-risk-analysis
npm install
npm run dev
```

Then open http://localhost:3000.

Both API keys are optional. Without them the pipeline runs on free, keyless
sources and says so in the output — candidates are ranked by a deterministic
scorer, and evidence falls back to keyword classification.

One setting is worth more than either key:

```
AFFLUENSE_CONTACT=you@yourdomain.example
```

Wikimedia's User-Agent policy refuses requests that carry no real contact, and
a refusal there removes registry company links, role dates for the tenure
filter, and the whole structured network at once. Set it and those come back.

There is also a CLI:

```bash
python run_screening.py "Mukesh Ambani" "Reliance Industries"
python run_network.py   "Azim Premji" "Wipro"
```

And a benchmark, which fixes the subject and gives each engine its own cold
cache so neither can win on a warm one:

```bash
python bench.py "Mukesh Ambani" --company "Reliance Industries"
```

## What a run costs

Firecrawl bills per page; OpenAI bills per token, counted from what the API
itself reports. Nothing is estimated. A screening run is a few rupees of
OpenAI and around 60 Firecrawl credits; a network run is a fraction of that.
The dashboard shows the breakdown per service, per purpose.

News, Wikipedia, Wikidata and the exchange rate are free and keyless.

Both engines meter identically, and the concurrent one is not more expensive:
it makes the same requests, just sooner. Every budget is run-scoped, so a loop
bug costs one call rather than an account.

## Tests

```bash
cd backend
pytest -q
```

No network access: every test runs against fixed inputs or a fake fetcher, so
the suite is deterministic and finishes in seconds. `test_v2_isolation.py` is
the one to run first — it checks that V1 is unchanged and that nothing under
`affluense/` imports V2.

## Further reading

- `backend/ARCHITECTURE.md` — design decisions, data sources, limitations
- `backend/README.md` — CLI flags and the HTTP API
- `backend/V2-ARCHITECTURE.md` — the concurrent engine: agents, host policy,
  budgets, and what stays sequential on purpose
- `backend/PS2-SOURCES.md` — network sources, and where LinkedIn or Crunchbase
  would be integrated
- `v1-flow.md` — the pipeline stage by stage
- `v2-plan.md` — the plan V2 was built from, including the measured bottlenecks
- `UPGRADE-PLAN-v0.md` — how the original design was arrived at
- `CLAUDE.md` — working rules for this repository

## Limitations

LinkedIn is not used: there is no public people-search API and its terms forbid
automated collection. This is the single biggest limit on network coverage.

Coverage is indexed public sources only. Court and registry filings that are
not published online are out of scope, and absence of adverse evidence is
never reported as proof that none exists.

Most companies found by page extraction carry no registry identifier, which is
what limits officer lookup and tenure dates. The report says so per run rather
than presenting a thin network as a complete one.

Social media is deliberately not scraped: the platforms are login-walled, a
fetch returns 403 and spends a credit for nothing. The budget goes to registry
and business pages instead.
