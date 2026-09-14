# Affluense — how it works, end to end

Two deliverables from one input — **a person's name and one company they are
connected to**:

| | Question it answers |
|---|---|
| **Risk screening** | What else is this person connected to, what is being said about it, and is any of it adverse? |
| **Network** | Who do they already know, and who should they meet? |

---

## 1. The stack

```
   ┌────────────────────────────────────────────────────────────┐
   │  Browser — Next.js 16 + Tailwind      localhost:3000       │
   │  name · company · screening / network / both               │
   └───────────────────────────┬────────────────────────────────┘
                               │  POST, then poll every 1.5s
                               ▼
   ┌────────────────────────────────────────────────────────────┐
   │  FastAPI — api.py                     localhost:8000       │
   │  job queue · progress · JSON + CSV                         │
   └───────────────────────────┬────────────────────────────────┘
                               │
                               ▼
   ┌────────────────────────────────────────────────────────────┐
   │  affluense/  — 24 Python modules                           │
   │                                                            │
   │    sources/   one module per external service              │
   │    resolve/   name → person, and company deduplication     │
   │    enrich/    sentiment, adverse-media risk                │
   │    network/   candidate generation and relevance scoring   │
   └───────────────────────────┬────────────────────────────────┘
                               │
                               ▼
   Wikipedia · Wikidata · Wikidata Query Service · Google News
   Bing News · GDELT · DuckDuckGo · OpenCorporates · Firecrawl
```

Dependencies run one way only: `sources → resolve → enrich → pipeline →
output`. No source module imports another, and none of them scores anything.
That is what makes a source swappable.

---

## 2. A run, step by step

```
  YOU
   │  "Mukesh Ambani" + "Reliance Industries"
   ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ 1  SUBMIT                                                    │
  │    POST /api/screening → {"job_id": "..."}  in about 50ms    │
  │    A run takes 40-90s, which is too long to hold an HTTP     │
  │    request open, so nothing blocks.                          │
  └───────────────────────────┬──────────────────────────────────┘
                              ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ 2  RESOLVE THE PERSON                                        │
  │    Wikipedia resolves the spelling — it tolerates typos,     │
  │    Wikidata does not ("virat kholi" finds him; Wikidata      │
  │    returns nothing).                                         │
  │                                                              │
  │    The company you supplied VERIFIES the match rather than   │
  │    steering the search. Adding it to the query once ranked   │
  │    Ratanji Tata (1871-1918) above Ratan Tata.                │
  │                                                              │
  │    → Wikidata id, match_confidence, match_basis              │
  └───────────────────────────┬──────────────────────────────────┘
                              ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ 3  DISCOVER COMPANIES          (shared by both pipelines)    │
  │                                                              │
  │    Wikidata officer links — founder / CEO / chair / board,   │
  │      filtered to organisations so a person's house is not    │
  │      screened as a company                                   │
  │    OpenCorporates officer records                            │
  │    Firecrawl search + schema extraction — this is what       │
  │      reaches registry mirrors carrying a Director            │
  │      Identification Number                                   │
  └───────────────────────────┬──────────────────────────────────┘
                              ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ 4  MERGE DUPLICATES                                          │
  │    "Vault by Virat Kohli" / "Vault" / "Vault fitness chain"  │
  │      → one entity, every spelling kept as an alias           │
  │    But two records with DIFFERENT registry ids never merge:  │
  │      Wipro and Wipro Enterprises are two real companies.     │
  └───────────────────────────┬──────────────────────────────────┘
                              ▼
         ┌────────────────────┴────────────────────┐
         ▼                                         ▼
  ┌──────────────────┐                   ┌──────────────────────┐
  │ 5a  SCREEN       │                   │ 5b  NETWORK          │
  │                  │                   │                      │
  │ per company,     │                   │ current network:     │
  │ in parallel:     │                   │   co-officers,       │
  │                  │                   │   family, associates │
  │  news from 3     │                   │                      │
  │  feeds           │                   │ candidates: people   │
  │       ↓          │                   │   with the same role │
  │  drop syndicated │                   │   in the same        │
  │  duplicates      │                   │   industry, globally │
  │       ↓          │                   │   (the dead are      │
  │  sentiment       │                   │   excluded)          │
  │  (VADER + ~170   │                   │       ↓              │
  │   finance terms) │                   │ score, rank, explain │
  │       ↓          │                   │                      │
  │  risk category   │                   │                      │
  │  AND stage       │                   │                      │
  └────────┬─────────┘                   └──────────┬───────────┘
           └──────────────────┬─────────────────────┘
                              ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ 6  RETURN                                                    │
  │    Progress lines stream back while it runs — the same ones  │
  │    the CLI prints, so the screen shows what is happening      │
  │    instead of a spinner.                                     │
  │    Then: JSON with the full evidence chain, and a CSV.        │
  └──────────────────────────────────────────────────────────────┘
```

---

## 3. What each output contains

**Screening** — one row per company:

```
company_name      sentiment   flag   categories                 stages
One8 Commune      negative    YES    insolvency; investigation  alleged; investigating
Jio Platforms     positive    -
NOMAD MANAGEMENT  no data     -                                 (no coverage found)
```

Expanding a row shows the link confidence, how the company was matched, each
finding with its stage, and the source articles with per-headline scores.

**Network** — ranked suggestions, each explaining itself:

```
 #  name                   company    role      score
 1  N. R. Narayana Murthy  Infosys    founder    0.69
      holds the same role as the subject: founder
      operates in IT service management
      based in India, where the subject already operates
      documented in 34 Wikipedia language editions
```

```
relevance = 0.30·role_overlap + 0.30·industry_overlap
          + 0.20·network_proximity + 0.10·geography + 0.10·prominence
```

Every component ships with the suggestion, so the ranking can be audited or
re-weighted without re-running collection.

---

## 4. The decisions that matter

**Sentiment and risk are separate axes.** *"SEBI settles probe for ₹40 crore"*
is neutral in tone and serious in risk. A company can read `positive` and
still be flagged. Collapsing the two loses the distinction an analyst needs.

**Every flag carries a stage** — `alleged`, `investigating`, `charged`,
`settled`, `dismissed`, `convicted`. "Accused of fraud" and "convicted of
fraud" are not the same fact, and rendering both as a red FRAUD badge is a
defamation claim waiting to happen.

**A keyword alone does not raise a flag.** Regulated industries name their
regulator constantly; that flagged seven of eight companies in one run, which
tells a reader nothing. A flag now needs a matter progressing past bare
mention, *or* negative tone.

**Absence of coverage is not neutrality.** A company with no articles is
marked `insufficient_coverage` and shown as "no data".

**Every record carries a source URL.** Enforced in the shape of the data, not
remembered at each call site. A claim nobody can check is worse than no claim.

**It never fails silently.** An empty suggestion list explains why. A missing
`vaderSentiment` warns loudly — it costs 0.67 → 0.48 accuracy. A dead source
records a note and the report still ships.

**LinkedIn is not used.** There is no public people-search API and its terms
forbid automated collection. This is the biggest limit on network coverage and
is documented rather than worked around.

---

## 5. Measured

| | |
|---|---|
| Sentiment, **held-out** set (21 headlines, never tuned on) | **0.67** accuracy, 0.66 macro F1 |
| Sentiment, tuned set (60 headlines) | 0.90 — reported only so the gap is visible |
| Before the domain lexicon | 0.65 |
| Without `vaderSentiment` | 0.48, and never predicts positive |
| Cache hit rate, second run | 87% (75s → 43s) |
| Tests | 75, no network, under 2 seconds |

---

## 6. Running it

```bash
# Terminal 1 — backend
cd backend
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn api:app --reload            # docs at /docs

# Terminal 2 — frontend
cd affluense-risk-analysis
npm run dev                         # http://localhost:3000
```

Either pipeline also runs standalone, no server needed:

```bash
python run_screening.py "Ratan Tata" "Tata Sons"
python run_network.py   "Azim Premji" "Wipro"
pytest -q
python eval_sentiment.py --set eval/holdout.json
```

Put a Firecrawl key in `backend/.env` to enable web search and extraction.
Without it everything still runs on free, keyless sources and says so.

---

## 7. Honest limits

- **Wikidata is thin outside listed-company leadership.** A cricketer with a
  restaurant chain has ten companies and none of them recorded there.
- **No true registry access.** MCA has no free API; registry data arrives via
  mirror sites found by search — attributable, but second-hand.
- **Sentiment reads headlines, not article bodies.** Headlines are written to
  provoke and skew negative relative to the reporting beneath them.
- **Network suggestions have no tenure check.** The dead are excluded, but a
  candidate may have left the company named.
- **81 hand-labelled headlines, one labeller.** Enough to catch regressions,
  not enough to trust the accuracy figure.

Full design rationale: [`backend/ARCHITECTURE.md`](backend/ARCHITECTURE.md).
