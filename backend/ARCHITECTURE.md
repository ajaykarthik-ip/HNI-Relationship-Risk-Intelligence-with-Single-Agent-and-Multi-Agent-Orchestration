# Architecture

Two deliverables, one codebase:

- **`run_screening.py`** — Problem Statement 1. Given a person and one company, find their other connected companies, gather public information on each, classify sentiment, and flag adverse news.
- **`run_network.py`** — Problem Statement 2. Given the same input, map the person's current network and suggest new connections globally, ranked by an explainable relevance score.

They share ~70% of their machinery — identity resolution, source adapters, transport, output — so that lives in one package rather than being written twice.

---

## 1. Layout

```
backend/
├── run_screening.py            Problem Statement 1 entry point
├── run_network.py              Problem Statement 2 entry point
├── api.py                      HTTP API over both pipelines (FastAPI)
├── requirements.txt
├── .env                        FIRECRAWL_API_KEY (git-ignored)
└── affluense/
    ├── config.py               endpoints, property maps, domain policy
    ├── http.py                 one HTTP client: retries, rate limit, robots.txt
    ├── models.py               the delivered data shapes
    ├── pipeline.py             PS1 orchestration
    ├── output.py               PS1 writers (JSON + CSV)
    ├── sources/                one module per external service
    │   ├── wikipedia.py        biography, fuzzy name resolution
    │   ├── wikidata.py         claims, company links, co-officers
    │   ├── news.py             Google News, Bing News
    │   ├── web.py              DuckDuckGo Instant Answer
    │   └── firecrawl.py        web search, page scrape, schema extraction
    ├── resolve/
    │   ├── person.py           name → one identified human
    │   └── company.py          company name normalisation and merging
    ├── enrich/
    │   ├── sentiment.py        VADER + finance/regulatory lexicon
    │   └── risk.py             adverse-media category and stage
    └── network/                Problem Statement 2
        ├── discover.py         current network, global candidate generation
        ├── scoring.py          relevance model
        ├── pipeline.py         PS2 orchestration
        └── output.py           PS2 writers (JSON + CSV)
```

The dependency direction is strictly one-way: `sources → resolve → enrich → pipeline → output`. No source module imports another, and none of them scores anything. That is what makes a source swappable.

---

## 2. Design principles

**Every record carries its source URL.** Not a convention to remember at each call site — it is in the shape of the data. A claim with no URL cannot be checked, and an unfalsifiable claim about someone's conduct is worse than no claim.

**Sentiment and risk are separate axes.** *"SEBI settles probe against firm for ₹40 crore"* is neutral in tone and maximum in risk. Collapsing them into one "negative" score is the most common mistake in this problem, and it loses the distinction an analyst actually needs. They are computed by two independent modules and reported as two independent fields.

**Every finding carries a stage.** `alleged`, `investigating`, `charged`, `settled`, `dismissed`, `convicted`. *"Firm accused of fraud"* and *"firm convicted of fraud"* share a category and are not the same fact. A tool that renders both as a red FRAUD badge is a defamation claim waiting to happen.

**Degrade, never fail.** Any source can be down, rate-limited, or newly key-walled. Each returns empty and records a note; the report is always produced. OpenCorporates started returning 401 during development and the pipeline did not notice — it, and GDELT, have since been removed (see *Sources removed* below).

**Confidence travels with identity.** Every person match and every company link carries a number and a plain-language basis. The reader can see how much weight a row will bear.

---

## 3. Problem Statement 1 — screening pipeline

```
  input: name + company
        │
        ▼
  ┌─────────────────┐   Wikipedia fuzzy search → candidate titles
  │ 1  RESOLVE      │   rank by name similarity; company verifies the pick
  │    person       │   → Wikidata QID, match_confidence, match_basis
  └────────┬────────┘
           ▼
  ┌─────────────────┐   Wikidata SPARQL: orgs naming the person as
  │ 2  DISCOVER     │   founder / CEO / chair / director / board / owner
  │    companies    │   Firecrawl extraction
  └────────┬────────┘   Firecrawl extraction: roles from page text
           ▼
  ┌─────────────────┐   normalise names → comparison key
  │ 3  MERGE        │   collapse duplicates, keep every alias
  │    entities     │   registry-backed names win over extracted ones
  └────────┬────────┘
           ▼
  ┌─────────────────┐   FOR EACH COMPANY:
  │ 4  SCREEN       │     Google News + Bing News
  │    per company  │     dedupe syndicated copy
  │                 │     sentiment per article → aggregate
  │                 │     risk categories + stage → flags
  └────────┬────────┘
           ▼
  ┌─────────────────┐   report.json   full evidence chain
  │ 5  OUTPUT       │   report.csv    the columns the spec names
  └─────────────────┘
```

**Step 4 is per company, not per person.** The deliverable is company-level sentiment, so company-level collection is the only honest way to produce it. An earlier version collected news for the person and attributed it to companies; that produces a number that cannot be defended.

---

## 4. Problem Statement 2 — network pipeline

```
  input: name + company
        │
        ▼
  1  RESOLVE person                    (shared with PS1)
        │
        ▼
  2  SUBJECT PROFILE
        roles       ← relationships on their companies
        industries  ← P452 on those companies (Q-numbers, not labels)
        countries   ← P17
        │
        ├──────────────────────────────┐
        ▼                              ▼
  3  CURRENT NETWORK            4  CANDIDATE POOL
     co-officers at their          for each industry × role:
       companies (SPARQL)            people holding that role at
     family and memberships          companies in that industry
       (Wikidata claims)             excluding the deceased
     associates named on the         excluding the subject
       same page (Firecrawl)
        │                              │
        └──────────┬───────────────────┘
                   ▼
            5  SCORE AND RANK
               drop anyone already connected
               weighted sum of five components
               keep the signals that produced the score
                   │
                   ▼
            network.json / network.csv
```

### When no industry can be established

Candidate generation needs an industry, and many subjects do not have one. A cricketer with a restaurant chain, a fitness brand and a football club has ten connected companies and **zero** of them recorded in Wikidata with an industry.

The pipeline handles this in three steps rather than returning an empty list:

1. **Share discovery with PS1.** Both problem statements ask the same question — "which companies is this person connected to?" — so they must not answer it differently. An earlier version asked Wikidata alone here and found nothing for a subject where screening found ten.
2. **Resolve discovered names.** Companies found by extraction carry no Wikidata id, so each name is looked up to harvest an industry where one exists.
3. **Reject sectors that are legal forms.** Extraction reads sector words off company names, so "foundation" arrives from SEVVA PATH SANKALP FOUNDATION and "management" from NOMAD MANAGEMENT LLP. Each resolves to a genuine Wikidata item, and each then returns peers with nothing in common with the subject — on one run, five Danish pharmaceutical-foundation executives were suggested for an Indian cricketer. A stoplist drops them before resolution.
4. **Fall back to occupation, and label it.** If no industry survives, candidates are drawn from people sharing the subject's occupation, narrowed by country and ordered by prominence. The output states `candidate_basis: "occupation"`, the notes explain why, and the scores land at 0.10–0.13 against 0.67–0.76 for a genuine industry match — the weak signal produces a weak score.

An empty suggestion list is never returned silently. Silence reads as "this person has no relevant connections", which is a far stronger claim than "the sources carry no industry data for them".

### Relevance model

```
relevance = 0.30 · role_overlap
          + 0.30 · industry_overlap
          + 0.20 · network_proximity
          + 0.10 · geography
          + 0.10 · prominence
```

Each component is in `[0, 1]`, and each is stored on the suggestion alongside a plain-language reason. The score is reproducible from its parts — verified in testing — so a relationship manager can disagree with the *reasoning* rather than with a bare number.

| Component | What it measures |
|---|---|
| `role_overlap` | Holds a role the subject also holds. Full credit for an exact match, partial for any senior role. |
| `industry_overlap` | Share of the subject's industries the candidate operates in. |
| `network_proximity` | Linked to a company the subject's network already reaches. |
| `geography` | Same country as the subject scores highest; a different country still scores, since the brief asks for *global* suggestions. |
| `prominence` | Wikipedia language editions, as a seniority proxy. **Weighted lowest deliberately** — it rewards being well documented, which correlates with seniority but also with fame. Weighting it heavily would surface celebrities over relevant executives. |

Sample output for Azim Premji / Wipro: Narayana Murthy (0.76), Ginni Rometty (0.73), Subroto Bagchi (0.72), Nandan Nilekani (0.71), Larry Ellison (0.70) — Indian IT founders and global IT leadership, which is the correct peer set.

---

## 5. Data sources

Every source below is free and requires no key, except Firecrawl.

| Source | Supplies | Key | Notes |
|---|---|---|---|
| **Wikipedia** API | Biography; **fuzzy name matching** | no | The only source that tolerates misspellings — so it resolves the name before anything else runs |
| **Wikidata** API | Structured claims: employer, positions, education, family, net worth | no | |
| **Wikidata Query Service** | Company officer links; co-officers; global candidate pool | no | Rate-limits hard; queries are kept small and split per property |
| **Google News** RSS | Headlines | no | Highest yield. URLs are opaque redirects |
| **Bing News** RSS | Headlines | no | URLs route via `apiclick.aspx` |
| **DuckDuckGo** Instant Answer | Abstract, related topics | no | The documented API — *not* the HTML results page, which their robots.txt disallows |
| **Firecrawl** | **Web search**, clean page text, schema-guided extraction | yes | Optional. Supplies the one capability the free stack cannot |

### Why Firecrawl earns its key

The free sources cannot search the web. Scraping search-engine result pages is disallowed by robots.txt, and DuckDuckGo's Instant Answer API returns almost nothing for a person.

Measured on a real subject (Virat Kohli), Wikidata alone returned **0 companies**. With Firecrawl search, the pipeline found ZaubaCorp and FalconeBiz — MCA/ROC registry mirrors carrying his Director Identification Number (`06985651`) — and recovered 10 companies plus 16 investments with figures attached (WROGN ₹20cr, Agilitas ₹58cr, Rage Coffee ₹19cr). That is registry-grade data reached through ordinary web search.

### External APIs considered and rejected

**LinkedIn.** The obvious source for Problem Statement 2, and unusable. There is no public people-search or connections API; the Marketing and Talent APIs are partner-gated and do not expose this data; and the User Agreement forbids automated collection. Any design that claims LinkedIn as a data source is describing something that cannot ship. This constraint is the single biggest limit on network coverage and is stated rather than papered over.

**Crunchbase API.** Requires a paid licence. Crunchbase *pages* are reachable through Firecrawl search and are one of the better extraction targets — the investments list for the Kohli run came from one.

**MCA / Registrar of Companies (India).** No free public API. The data reaches this system indirectly through registry-mirror sites found by search, which is second-hand but attributable.

**Social media.** Instagram, Facebook, X and LinkedIn are on a skip list. They are login-walled: on a real run, three of six page fetches returned 403 and consumed credits for nothing. The spec mentions social media as a possible source; this is a deliberate deviation, and the credits are spent on registry and business pages instead — which doubled useful page reads at identical cost.

---

## 6. Data quality

This is where most of the engineering went.

**Person resolution.** Wikidata's search does no fuzzy matching; Wikipedia's does. Querying `"virat kholi"` returned nothing from Wikidata and silently emptied half a report, while Wikipedia matched it correctly. So Wikipedia resolves the spelling first and its title is what Wikidata is asked for.

The supplied company **verifies** the match rather than biasing the search. Adding it to the query string ranked *Ratanji Tata (1871–1918)* above *Ratan Tata (1937–2024)* — the pipeline screened the wrong man. Now name similarity decides, and the company is checked against the article text to confirm.

**Company resolution.** The same company arrives spelled differently from every source:

```
"Vault by Virat Kohli"  "Vault"  "Vault fitness chain"   → one entity
"Blue Tribe Foods"      "Blue Tribe"                     → one entity
"Digit"                 "Go Digit"                       → one entity
```

Names are normalised to a comparison key — legal forms stripped, generic descriptors stripped, the subject's own name removed — and merged. Registry-backed names win, since theirs is the filed one. Every original spelling is kept as an alias. Measured: 6 records → 3 entities.

**Syndication.** RSS appends `" - Publisher"` to headlines, so one wire story arrives as several distinct-looking rows. On a Tata run, 48 stored items were 44 distinct stories. The dedupe key strips the publisher suffix before comparing.

**Keyword precision.** Risk terms are word-boundary regexes. An earlier substring version matched `"ed "` (for Enforcement Directorate) inside *backed*, *supported* and *expanded*, and produced six false "investigation" flags on a clean subject.

**Provenance after redirects.** Aggregator links bounce through their own domain. The resolved URL is recorded, so a page reads as `livemint.com` and not `bing.com`.

**Extraction honesty.** The schema prompt instructs the model to extract only what the page states and return an empty object if the page is not about the subject. Without it an extraction model fills empty fields from its own memory of a famous person — and that invented content would arrive wearing a real source URL. Extracted companies are tagged `"source": "Firecrawl extraction"` and carry a note that they are page text, not registry records. A model's reading of a news article and an MCA filing are not the same class of evidence.

### Sources removed

| Source | Why it was removed |
|---|---|
| **GDELT** DOC 2.0 | Its free DOC endpoint rate-limited (429) on every observed run, returning no articles while costing ~22 requests and several seconds per screening. Google News and Bing News RSS cover the same ground and answer. |
| **OpenCorporates** v0.4 | Unauthenticated access returns 401. Every call was a wasted request that produced a note and no officer records. Re-adding it means obtaining a token and restoring `web.opencorporates_officers`. |

Both were removed rather than left failing: a source that never answers is not graceful degradation, it is a request budget spent on nothing, and it made the notes panel read as if data had been attempted and lost.

---

## 7. Sentiment

**VADER, with a domain lexicon.** VADER is free, keyless, needs no model download, and handles negation and intensifiers — *"not cleared"*, *"deeply flawed"* — which a keyword count cannot. What it lacks is finance and regulatory vocabulary. Measured on the stock lexicon:

| Headline | Stock VADER | With domain lexicon |
|---|---|---|
| Firm faces insolvency petition at NCLT | `0.000` neutral | **−0.700 negative** |
| Regulator penalises bank over KYC lapses | `0.000` neutral | **−0.421 negative** |
| SEBI settles probe against firm for ₹40 crore | `0.000` neutral | **−0.459 negative** |

All three are plainly negative to a compliance reader. ~120 domain tokens close the gap; VADER's machinery still applies on top. Multi-word signals it cannot reach (*"show cause"*, *"class action"*, *"clean chit"*) are applied as a bounded adjustment.

**Thresholds.** ±0.25, not VADER's stock ±0.05. Headlines are short and blunt, and the default boundary flips on almost any adjective, which makes "neutral" meaningless.

**Aggregation is weighted and asymmetric.** A company whose coverage is ≥25% negative classifies negative; a positive verdict requires ≥35%. Adverse coverage matters disproportionately in screening — a company where one story in four is negative is one an analyst must read, even if the rest of the press is good — and asserting that a company is *well* regarded is a stronger claim than flagging it for a look, so it carries a higher bar.

**Absence of coverage is not neutrality.** A company with zero articles is marked `insufficient_coverage`, because "nothing was found" and "coverage was balanced" are different facts that would otherwise share a label.

**Measured.** On a 21-headline holdout never used for tuning: 0.67 accuracy, 0.66 macro F1, negative recall 0.75. On the 60-headline set the lexicon was tuned against: 0.90 and 0.89. The first is the honest estimate; the second is reported only so the gap is visible. Before the domain lexicon existed the tuned set scored 0.65.

**The fallback is a safety net, not an option.** Without `vaderSentiment` the class drops to a bare keyword score: 0.48 holdout accuracy against 0.67, positive F1 of 0.00, and half the adverse headlines missed. It warns loudly when active, because quietly producing much worse output is the failure mode this system is built to avoid.

**Upgrade path.** `SentimentAnalyzer` is one class behind one interface. Swapping in FinBERT is a constructor change, at the cost of a ~400MB model download and a hard `torch` dependency. VADER is the right default for a tool that must run anywhere with `pip install`.

---

## 8. Risk taxonomy

Seven categories — `fraud`, `scam`, `litigation`, `regulatory`, `investigation`, `insolvency`, `arrest` — each a set of word-boundary patterns.

Stage is detected separately and ranked by severity, with **dismissal checked before conviction** so that *"acquitted of all charges"* is not read as a conviction. When several stories cover one matter, the most advanced stage wins.

`negative_news_flag` is true when any flag exists. It is independent of sentiment: a company can read `positive` and still be flagged, which is the intended behaviour and the reason the two are separate fields.

---

## 9. Output contracts

**PS1 — `screening[]`** (JSON) and the CSV row:

```
company_name, sentiment, negative_news_flag, flag_categories, flag_stages,
negative_articles, neutral_articles, positive_articles, articles_reviewed,
relationship, status, jurisdiction, registry_id, link_confidence,
sources, source_url
```

The first three columns are exactly what the problem statement names; the rest is the provenance a reader needs to check any row. The JSON additionally carries `flags[]` with stage and evidence URLs, and `evidence[]` with per-article sentiment scores.

**PS2 — `suggested_connections[]`** and the CSV row:

```
rank, suggested_name, company, role, location, relevance_score,
role_overlap, industry_overlap, network_proximity, geography, prominence,
signals, source_url
```

Score components are exported so the ranking can be audited or re-weighted without re-running collection.

---

## 10. Scalability

**Current state — honest.** Collection is synchronous with a politeness delay, single process, and there is no cache. Screening 12 companies across 3 news feeds is ~40 serial requests. That is correct for a CLI run over one subject and wrong for a book of clients.

**What the design already gets right:** sources are stateless functions over a shared transport, so they parallelise without refactoring; the per-company screen is embarrassingly parallel; scoring is pure and needs no network.

**The HTTP layer.** `api.py` wraps both pipelines. Runs are asynchronous — submit, then poll — because a 40-90 second synchronous request dies in any real proxy. The pipelines take an `on_progress` callback, so a caller sees the same step-by-step lines the CLI prints instead of a spinner. The job store is in memory and single-process, which is the honest shape of items 3 and 4 below not being done yet.

**The path to scale:**

1. **Response cache** — content-addressed by URL + params, with a TTL per source class (registry data monthly, news hourly). Re-running a subject should cost near zero. This is the single highest-value change and the largest current gap.
2. **Concurrency** — a bounded thread pool over the per-company screen, with the rate limiter moved to per-host token buckets so politeness is preserved while unrelated hosts proceed in parallel. Expected 5–10× on wall clock.
3. **Queue and workers** — subjects on a queue, workers pulling; collection and scoring split so a scoring change can be replayed over cached raw material without re-fetching.
4. **Incremental refresh** — store the last-seen article per company and fetch only newer items.
5. **Entity store** — persist resolved people and companies with their confidence, so resolution is done once and improves over time rather than being recomputed per run.

---

## 11. Limitations

Stated plainly, because a screening tool that hides its blind spots is worse than one that has none.

- **Wikidata coverage is thin outside listed-company leadership.** Kohli returns 0 companies from Wikidata despite a real corporate footprint. Firecrawl closes much of this; without a key, coverage for non-public figures is poor.
- **No true registry access.** MCA has no free API. Registry data arrives via mirror sites found by search — attributable, but second-hand.
- **Network suggestions have no tenure check.** Deceased people are excluded, but a candidate may have left the company named.
- **Risk flags are keyword signals, not judgements.** They mark a story worth reading. Precision was measured at 8/8 on one subject; that is a sample, not a benchmark.
- **Sentiment is computed on headlines**, not article bodies. Headlines are written to provoke, and skew negative relative to the reporting beneath them.
- **The evaluation set is small and self-labelled.** 60 tuned headlines and 21 held out, labelled by one person. On the holdout the classifier reaches 0.67 accuracy and 0.66 macro F1, with negative recall 0.75 — that is the honest figure, and the 0.90 on the tuned set is fit, not generalisation. A few hundred headlines labelled by more than one person is what would make these numbers trustworthy.
- **Two of eight adverse headlines were missed on the holdout**, including "third straight quarter of losses" and "promoter pledges additional shares" — the latter is negative only if you know Indian market convention, which a general lexicon does not.

---

## 12. Running it

```bash
python -m venv .venv
.venv\Scripts\activate              # Windows
pip install -r requirements.txt

python run_screening.py "Ratan Tata" "Tata Sons"
python run_network.py   "Azim Premji" "Wipro"
```

Optional: put a Firecrawl key in `.env` as `FIRECRAWL_API_KEY=` to enable web search and extraction. Without it both pipelines run on free sources alone and say so in the output.
