# Affluense — HNI screening and network intelligence

Two Python solutions over one codebase, for the two problem statements.

| | Entry point | Answers |
|---|---|---|
| **PS1** | `run_screening.py` | Which companies is this person connected to, what is the sentiment around each, and is there adverse news? |
| **PS2** | `run_network.py` | Who does this person already know, and who should they meet — ranked by relevance? |

Full design, data-source justification and limitations: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate           # Windows
source .venv/bin/activate        # macOS / Linux
pip install -r requirements.txt
```

Both keys are optional, and both go in `.env`:

```
FIRECRAWL_API_KEY=fc-your-key-here
OPENAI_API_KEY=your_openai_api_key_here
```

**Firecrawl** enables web search and page extraction. Without it both pipelines
run on free, keyless sources alone and say so in the output.

On a free trial, leave `FIRECRAWL_PLAN=trial` (the default). The cost panel then
reports credits consumed and states plainly that no money was spent, instead of
multiplying credits by a list price you are not paying. Set
`FIRECRAWL_TRIAL_CREDITS` to your allowance to get "used X of Y" and an estimate
of runs remaining. Switch to `FIRECRAWL_PLAN=paid` when you start paying.

**OpenAI** is the reasoning layer, not a search engine: it ranks identity
candidates and re-reads articles that already raised a flag. Ordinary sentiment
stays on VADER, which is local and free — see [ARCHITECTURE.md](ARCHITECTURE.md).
Without a key the deterministic scorer decides and nothing else changes. It is
called over plain HTTPS, so there is no extra package to install.

Optional OpenAI settings, all with working defaults:

```
OPENAI_MODEL=gpt-4o-mini
OPENAI_USD_PER_MTOK_IN=0.15      # for the cost meter, per million tokens
OPENAI_USD_PER_MTOK_OUT=0.60
OPENAI_MAX_CANDIDATE_CALLS=2     # ceilings, so a loop bug costs one call
OPENAI_MAX_REVIEW_CALLS=15
```

## Run

```bash
python run_screening.py "Ratan Tata" "Tata Sons"
python run_network.py   "Azim Premji" "Wipro"
```

Each writes `<name>.json` (full evidence chain) and `<name>.csv` (the columns the brief names).

### Useful flags

```
--max-companies 12       companies to screen
--max-news 25            articles per company
--workers 6              companies screened in parallel
--no-cache               bypass the response cache
--no-firecrawl           free sources only
--max-suggestions 25     PS2: how many connections to return
```

## Output

**PS1** — one row per company:

```
company_name                      sentiment  flag   categories                 stages
Tata Trusts                       positive   True   investigation; regulatory  investigating; reported
Sir Dorabji Tata & Allied Trusts  positive   False
Tata Housing Development Company  neutral    False
```

Sentiment and the adverse-news flag are **independent**. A company can read `positive` and still be flagged — *"SEBI settles probe for ₹40 crore"* is neutral in tone and serious in risk, and collapsing the two loses the distinction an analyst needs.

Every flag carries a **stage** — `alleged`, `investigating`, `charged`, `settled`, `dismissed`, `convicted`. An allegation is not a finding.

**PS2** — ranked suggestions with explainable scores:

```
 #  name                   company    role     score
 1  N. R. Narayana Murthy  Infosys    founder   0.76
 2  Ginni Rometty          IBM        chair     0.73
 3  Subroto Bagchi         Mindtree   founder   0.72
```

```
relevance = 0.30·role_overlap + 0.30·industry_overlap
          + 0.20·network_proximity + 0.10·geography + 0.10·prominence
```

Every component is exported on each suggestion, so the ranking can be audited or re-weighted without re-running collection.

## Data sources

Free and keyless: Wikipedia, Wikidata, Wikidata Query Service, Google News RSS, Bing News RSS, DuckDuckGo Instant Answer. Keyed and optional: Firecrawl. GDELT and OpenCorporates were removed after rate-limiting (429) and key-walling (401) made them answer nothing on every run.

**LinkedIn is not used.** There is no public people-search API and its terms forbid automated collection. This is the single biggest limit on network coverage and is documented rather than worked around.

## HTTP API

```bash
uvicorn api:app --reload
```

Interactive docs at `http://127.0.0.1:8000/docs`.

### Step one: who did you mean?

Free sources only, answered in seconds, no credits spent. Ask this before
starting research so the expensive step runs against the right person:

```bash
curl -X POST localhost:8000/api/candidates \
  -H "Content-Type: application/json" \
  -d '{"name":"Virat Kohli","company":"One8 Commune"}'
# -> {"candidates": [{"id": "wikidata:Q170296", "name": "Virat Kohli",
#      "evidence": ["name matches 'Virat Kohli' (100%)", ...]}], ...}
```

Set `"mode": "company"` to start from a company instead; the reply then also
carries `related_people`, the people that company names as founders and
officers.

With `OPENAI_API_KEY` set, the shortlist is reordered and explained by the
model. Without it the deterministic scorer decides and the flow is identical.

### Step two: deep research

A run takes 40-90 seconds, too long to hold an HTTP request open through a
browser or proxy, so work is submitted as a job and polled. Pass the candidate
the user chose as `confirmed` and the pipeline skips its own identity guess:

```bash
curl -X POST localhost:8000/api/screening \
  -H "Content-Type: application/json" \
  -d '{"name":"Ratan Tata","company":"Tata Sons",
       "confirmed":{"id":"wikidata:Q311440","name":"Ratan Tata",
                    "kind":"person","wikidata_id":"Q311440"}}'
# -> {"job_id": "31b0e0290fb1", "status": "queued", ...}

curl localhost:8000/api/jobs/31b0e0290fb1          # status + live progress
curl localhost:8000/api/jobs/31b0e0290fb1/result   # the report
curl -O localhost:8000/api/jobs/31b0e0290fb1/result.csv
```

| Route | Purpose |
|---|---|
| `GET /health` | Readiness, and whether Firecrawl, OpenAI and VADER are actually available |
| `POST /api/candidates` | Who did you mean? Free sources only, answered synchronously |
| `POST /api/screening` | Problem Statement 1. Returns `202` with a job id |
| `POST /api/network` | Problem Statement 2. Returns `202` with a job id |
| `GET /api/jobs` | All jobs |
| `GET /api/jobs/{id}` | Status and the progress lines the CLI would print |
| `GET /api/jobs/{id}/result` | The report. `409` while still running, `500` if it failed |
| `GET /api/jobs/{id}/result.csv` | Same data as CSV |
| `DELETE /api/jobs/{id}` | Discard a job |

Polling returns the same progress lines the CLI prints, so a frontend can show
what the pipeline is doing rather than a spinner. CORS is open to
`localhost:3000` by default; override with `ALLOWED_ORIGINS`.

Jobs are held in memory. That is deliberate for an assessment deliverable and
is the first thing to replace for real use.

## Tests

```bash
pytest -q
```

61 tests, no network access, under a second. They cover the cases where this
system got things wrong during development, so a regression re-breaks a test
rather than a report:

| Area | What is pinned |
|---|---|
| Sentiment | The six finance headlines stock VADER scored `0.000` neutral |
| | Negation: "not cleared" must score below "cleared" |
| | Asymmetric aggregation thresholds |
| Risk | Word boundaries — "backed"/"expanded" must not match "ED" |
| | Dismissal outranks conviction: "acquitted of all charges" |
| | A flag takes the most advanced stage in its coverage |
| Resolution | "Ratan Tata" + "Tata Sons" must not return Ratanji Tata (1871-1918) |
| | Company name variants collapse; registry names win |
| News | Syndicated copy de-duplicates across publisher suffixes |
| Firecrawl | Login-walled domains skipped; registry sources ranked first |
| Scoring | Relevance reproducible from its components; no empty explanations |
| | Existing connections and unlabelled Wikidata items excluded |
| Cache | Key stability, per-source TTLs, roundtrip, disabled mode |
| API | Job lifecycle, `409` while running vs `404` when absent, a failing pipeline becoming an error job rather than a crash, CSV streaming, input validation |

## Measured quality

```bash
python eval_sentiment.py                          # the tuned set
python eval_sentiment.py --set eval/holdout.json  # held out, never tuned on
```

Two numbers, because only one of them is an honest estimate:

| Set | Accuracy | Macro F1 | Negative recall |
|---|---|---|---|
| `eval/headlines.json` (60, **tuned on**) | 0.90 | 0.89 | 1.00 |
| `eval/holdout.json` (21, **never tuned on**) | 0.67 | 0.66 | 0.75 |

The lexicon was extended after reading the errors on the first set, so **0.90 measures fit, not generalisation**. The holdout was written afterwards and never used for tuning; **0.67 is the number to believe.** The gap between them is the ordinary cost of hand-tuning a lexicon on a small sample, and the honest fix is a larger labelled set, not more tuning.

Before the lexicon work the tuned set scored **0.65 / 0.64**, so the gain is real — it just isn't 0.90.

### Why vaderSentiment is a hard requirement

Measured by accident when the dependency was missing from an install:

| Backend | Holdout accuracy | Macro F1 | Positive F1 | Adverse headlines missed |
|---|---|---|---|---|
| VADER + domain lexicon | 0.67 | 0.66 | 0.62 | 2 of 8 |
| keyword fallback only | 0.48 | 0.40 | **0.00** | 4 of 8 |

The fallback never predicted positive once and missed half the adverse coverage. It exists so the package imports without the dependency, not so it can be used — both CLIs and the evaluation script now print a warning when it is active.

Negative recall is reported separately because the errors are not symmetric: missing an adverse story is the costly failure in screening, while calling ordinary news negative only wastes an analyst's minute.

## Caching

Responses are cached to `.cache/` with a TTL per source class — news hourly, registry data weekly, Firecrawl calls weekly so a re-run never spends credits twice. Measured on a five-company screen: **87% hit rate** on the second run, 75s → 43s.

## Layout

```
run_screening.py  run_network.py  ARCHITECTURE.md
affluense/
  config  http  cache  models  pipeline  output
  sources/   wikipedia  wikidata  news  web  firecrawl
  resolve/   person  company
  enrich/    sentiment  risk
  network/   discover  scoring  pipeline  output
```
