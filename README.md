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

There is also a CLI:

```bash
python run_screening.py "Ratan Tata" "Tata Sons"
python run_network.py   "Azim Premji" "Wipro"
```

## What a run costs

Firecrawl bills per page; OpenAI bills per token, counted from what the API
itself reports. Nothing is estimated. A screening run is a few rupees of
OpenAI and around 60 Firecrawl credits, and the dashboard shows the breakdown.

News, Wikipedia, Wikidata and the exchange rate are free and keyless.

## Further reading

- `backend/ARCHITECTURE.md` — design decisions, data sources, limitations
- `backend/README.md` — CLI flags and the HTTP API
- `UPGRADE-PLAN.md` — how the current design was arrived at
- `CLAUDE.md` — working rules for this repository

## Limitations

LinkedIn is not used: there is no public people-search API and its terms forbid
automated collection. This is the single biggest limit on network coverage.

Coverage is indexed public sources only. Court and registry filings that are
not published online are out of scope, and absence of adverse evidence is
never reported as proof that none exists.
