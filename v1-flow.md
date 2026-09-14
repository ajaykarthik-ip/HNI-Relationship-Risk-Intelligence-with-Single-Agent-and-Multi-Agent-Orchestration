# How Affluense works

A walkthrough of the whole system, stage by stage, naming the modules that do
the work and the reasons behind the decisions that aren't obvious.

Two questions are answered from one input:

- **PS1 — risk screening.** Which companies is this person tied to, what does
  the public record say about each, and is there anything adverse?
- **PS2 — network intelligence.** Who do they already know, and who should
  they meet?

Input is always the same: a person's name plus one connected company.

---

## Layout

```
backend/
  api.py                    FastAPI: jobs, polling, cancellation
  affluense/
    config.py               every setting, all env-overridable
    http.py                 the one transport: rate limits, retries, robots, cache
    cache.py                per-source TTLs
    usage.py                the cost meter
    models.py               Article · Finding · EvidenceItem · CompanyScreening
    resolve/
      candidates.py         "who did you mean?"
      person.py             the confirmed subject
      company.py            name normalisation and merging
      relationships.py      14 relationship classes
    collect/
      queries.py            the adverse query ladder
      fulltext.py           article body retrieval
    sources/
      wikipedia.py  wikidata.py  news.py  web.py  firecrawl.py  openai_client.py
    enrich/
      publishers.py         source tiering
      relevance.py          is this article even about the company?
      risk.py               the keyword taxonomy (now a prefilter)
      extract.py            the model reads each article
      cluster.py            articles → events
      sentiment.py          VADER + finance lexicon
      tenure.py             role dates vs article dates
    validate.py             the last gate
    risk_score.py           the verdict
    qc.py                   a second read
    output.py               report assembly, JSON, CSV
    network/                PS2: discover, scoring, explain, pipeline, output

affluense-risk-analysis/    Next.js dashboard. Presentation only.
```

The shape to keep in mind: **deterministic Python decides what is true; a model
is consulted at four fixed points and can never change a number.**

---

## Phase 1 — Who did you mean?

`POST /api/candidates` · free · answers in seconds

The pipeline used to guess an identity and research whatever it picked. A wrong
guess was invisible — a confident, complete report about a different person of
the same name. So the question is now asked out loud.

**`resolve/candidates.py`** searches Wikipedia (titles + REST summaries) and
Wikidata, merges what comes back into one row per real-world entity, and scores
each on name similarity, whether the supplied company appears in their article,
and whether a Wikidata entity exists at all.

Every row carries its evidence, because the user is being asked to make the
call the tool used to make silently. A confident list with no reasoning would
just move the guess rather than remove it.

If `OPENAI_API_KEY` is set, the shortlist is reordered by the model — given
only candidates a free source already returned, so it cannot introduce a person
who doesn't exist. Without a key the deterministic scorer decides and the flow
is identical.

Three entry shapes:

| Input | Behaviour |
|---|---|
| Person | Which person? |
| Person + company | The company is a verification signal, never a rescue |
| Company | Returns the company *and* the people it names as officers |

Nothing expensive runs until a row is clicked. The chosen candidate is echoed
back as `confirmed`, and `resolve/person.py:from_confirmed()` builds the
subject at confidence 1.0 with a basis that names *who decided* — an honest
claim, rather than implying the tool knew.

---

## Phase 2 — Research

`POST /api/screening` · 5–10 minutes · submits a job, polls for progress

### 1. Company discovery

Three sources feed `discover_companies()` in `pipeline.py`:

- **Wikidata** — organisations naming the subject as founder, officer, chair or
  owner. The SPARQL walks full statement nodes rather than the `wdt:` shortcut,
  so `P580`/`P582` role-start and role-end qualifiers come back too. Without
  those every Wikidata role was undated and the tenure filter could never fire.
- **Firecrawl** — a handful of profile pages, read for roles, stakes and
  associates. Tagged as page text, never as registry record.
- **The query itself** — the supplied company is part of the answer by
  definition.

### 2. Entity resolution

`resolve/company.py` reduces each name to a comparison key: `&` → `and`,
acronyms expanded (`SEZ` → special economic zone), legal suffixes and connector
words stripped. Then `_absorb_longer_forms()` folds a short name into the filed
name of the same company.

The guard matters more than the merge. Absorption happens **only when every
extra word describes corporate structure**, never a business line. So
*Adani Ports* merges into *Adani Ports and Special Economic Zone*, while
*Wipro* / *Wipro Enterprises* and *Tata Motors* / *Tata Motors Finance* stay
apart. A wrong merge moves one company's adverse news onto another.

### 3. Relationship classification

`resolve/relationships.py` assigns one of fourteen classes. Only five carry the
subject's own exposure:

| Carries exposure | Reported, never attributed |
|---|---|
| `current_company` `former_company` `executive_role` `board_seat` `control` | `former_employer` `employment` `investment` `nonprofit` `think_tank` `regulator` `government_body` `trade_association` `media_role` `subsidiary` `parent` |

Order matters: what the *organisation* is outranks what the role is called,
because public bodies hand their outside directors exactly the titles a
controlling role is spelled with. A seat on the RBI board is a public
appointment. A television hosting contract is not a directorship.

"Former" appears in the exposure column deliberately — *when* an event happened
is the tenure check's job, and gating it twice loses real findings.

Deterministic rules settle almost everything; the model is asked only about
what they leave as `other`, and its answer is stored with its reasoning.

### 4. Stake corroboration

A percentage stated on a page is a lead, not a fact. `_corroborated_stakes()`
promotes a holding to a screened company only when Wikidata confirms the
ownership, or the company carries the subject's own name.

This exists because an extractor read "acquired Old Mutual's stake" and
recorded the subject as *owning Old Mutual*. Screening that company would have
attached a stranger's adverse coverage to a real person.

### 5. Evidence collection

`collect/queries.py` — the change that mattered most.

Asking for a company by name returns what the company publishes. So every
company and the individual are searched across **fourteen adverse categories**:

```
fraud · money_laundering · corruption · litigation · regulatory
enforcement · investigation · criminal · insolvency · tax
accounting · governance · sanctions · controversy
```

Terms inside a category are OR-ed into one query, so a category costs one
request per feed rather than one per word. Adverse queries run **first**, so a
cap truncates general coverage rather than the check for enforcement action.

Roughly 100–250 queries a run against Google News and Bing News RSS — both
free and keyless.

The query group travels with every article it produced. That is what lets a
finding say which check surfaced it, and lets a clean result list the checks
that ran and came back empty.

### 6. Relevance and provenance

**`enrich/relevance.py`** drops articles that aren't about the company. Every
distinctive token must appear — "any token" matched Hindustan Times for
Hindustan Computers. A name made only of common English words ("More") needs a
corroborating word from the aliases, sector or subject name before anything
counts.

**`enrich/publishers.py`** assigns a tier:

| Tier | What | Treatment |
|---|---|---|
| 0 | PR wires, the subject's own domains | Excluded from evidence entirely |
| 1 | Reuters, Bloomberg, FT, major Indian business desks | Full weight |
| 2 | Trade and regional press | Standard weight |
| 3 | Unknown | Counted, low weight |

Both feeds hand out redirect URLs, so the publisher is read from what the feed
says rather than from the link: Google News supplies `<source url="…">`, and
Bing's click tracker carries the real URL in a query parameter. Before that,
every article looked like it was published by a search engine — and `bing.com`
was being counted as an independent source, inflating the one number that
decides whether a finding is material.

Aggregators are never counted as publishers. An article whose source cannot be
identified contributes nothing rather than contributing one.

### 7. Cross-company de-duplication

A group's flagship story comes back from the query for every subsidiary.
Counting it once per entity turned a single fact into six across the Adani
companies and skewed every ratio computed from it.

Fetching runs in parallel; **assignment runs single-threaded** over a
deterministically sorted target list, so the same run always gives the same
article to the same company. Inside the thread pool it would go to whichever
worker finished first.

### 8. Tenure

`enrich/tenure.py` compares an article's publication date against the role
window from Wikidata qualifiers or a stated period. Articles outside it are
**marked, not removed** — the company's history is still worth seeing, but it
cannot raise a flag against the person.

IL&FS is the case: a chairman appointed in 2018 to clean up a fraud that
preceded him, with that fraud reported as his.

An undated role filters nothing. Undated means *do not filter*, never *assume
current* — guessing would silently drop real findings.

### 9. Full-article retrieval

`collect/fulltext.py` fetches bodies for articles that already look material:
they carry a risk term, their publisher is tier 1–2, and their URL is real
rather than a redirector. Budgeted across the whole run, so one company can't
exhaust it on routine coverage.

Free: `requests` + `beautifulsoup4`, through the same rate-limited, cached,
robots-checked transport. Firecrawl is a fallback for JS-walled pages behind
its own budget, **defaulting to zero** — it is the only source here that spends
credits and should never do so unasked.

Paywalls and robots refusals are recorded as `paywalled` / `blocked`, and the
finding falls back to headline evidence. Never presented as though the text was
read.

### 10. Evidence analysis

`enrich/extract.py` — batched, one call per company. The model is handed the
articles and returns strict JSON per article:

```
is_about_target · is_journalism · is_promotional
event_type · event_summary · event_date · stage
subject_role_in_event · actor_is_subject · severity · confidence
```

`subject_role_in_event` is the field that stops the worst class of error. Being
*named* in an article is not being *accused* in it: "X says Y committed fraud"
makes X the accuser. Attribution requires `accused` or `defendant` — the
model's own `actor_is_subject` is not trusted alone, because it had set it true
for exactly that sentence.

Failure is designed for. A malformed reply, an id that matches no article, a
missing key — each leaves the article on the keyword path. Degraded, never
empty.

### 11. Clustering

`enrich/cluster.py` groups events on `(category, year)` and produces a
`Finding`. Two adverse events of the same kind, at the same company, in the
same year are almost always the same event; where they aren't, splitting
under-counts rather than over-counts, which is the safe direction.

**Corroboration counts publishers, never articles.** That single choice is what
makes "3 independent sources" mean something.

When a cluster contains both an open stage and a closed one — one source
reporting charges, another a dismissal — the model adjudicates. That is a
reading problem, and getting it wrong in silence is how a cleared matter gets
reported as open. Without a key, the latest-dated account stands.

### 12. The verdict

`risk_score.py`. The model contributed severity and stage *per finding*; the
rollup is arithmetic.

```
score = severity
      × stage_weight        dismissed 0.15 … convicted 1.30
      × age_factor          ongoing never decays
      × corroboration       1 publisher 0.75, 3+ 1.0
      × source_quality      tier 1 1.0, tier 3 0.65
```

Nothing can inflate a weak finding; every factor only reduces. Unattributed or
out-of-tenure findings score **zero**.

| Level | Meaning |
|---|---|
| `LOW` | No material adverse findings in available evidence |
| `MEDIUM` | Historical, resolved, or unattributed matters |
| `HIGH` | Ongoing investigation, regulatory action or litigation, attributable |
| `CRITICAL` | Charges, conviction, sanctions or fraud with the subject named |

Three independent material matters raise the level on their own: a pattern is
itself a finding.

**Confidence is computed separately and never collapsed into the level.** HIGH
at high confidence and HIGH at low confidence call for different actions, and
one number hides that. Confidence reads identity certainty, evidence volume,
checks run, tier-1 share, corroboration depth, contradictions, and how many
company rows rest on unverified links.

Why deterministic: a compliance decision has to be reproducible, explainable
and testable. A model answering "HIGH" differently on two runs of identical
evidence is not defensible to a regulator and cannot be unit tested.

### 13. Validation and review

**`validate.py`** checks every row against its own evidence — coverage depth,
whether each flag points at an article, attribution, contradictions, and
one-sided coverage at ≥90% of a single tone, which is the shape promotional
copy makes. Rows are **labelled, never removed**: "nothing solid was found" is
a real answer, and dropping the row would look like the company was never
examined.

It is pure and idempotent, so it can run before and after any refinement.

**`qc.py`** takes one last read of the finished assessment and **raises issues
only — it never edits**. A model that can rewrite a finding can launder an
error into confident prose; restricting it keeps every number traceable to the
layer that produced it. Deterministic checks run with or without a key.

---

## The data model

```
Article          one retrieved item: headline, publisher, tier, body, extraction
   ↓  clustered by (category, year)
Finding          one real-world event
                 what_happened · stage · severity · event_date · is_ongoing
                 attributed_to_subject · within_tenure
                 corroborating_publishers[] · evidence[]
   ↓  scored and rolled up
Assessment       risk_level · confidence · reasons[] · limitations[] · action
```

`Finding.is_material` requires all four: attributed, within tenure, at least
one identified publisher, severity ≥ 2.

---

## Where the model is used

| Stage | Purpose | Falls back to |
|---|---|---|
| Candidate ranking | Which person did you mean | Deterministic scorer |
| Relationship classification | Only the unresolved residue | `other` |
| Evidence analysis | What each article says | Keyword taxonomy |
| Contradiction review | Which account is current | Latest-dated source |
| Final review | Raise issues with the report | Deterministic checks |
| Network rationales | Why this person is worth meeting | Stored signal strings |

**Never** used for: the risk level, corroboration counts, deduplication,
attribution, sentiment, or any arithmetic.

Every call runs at `temperature: 0`, in JSON mode, through the shared `Fetcher`
so identical prompts are cached and billed once, under a per-purpose call
ceiling so a loop bug costs one call rather than an account.

---

## PS2 — Network

`network/pipeline.py` reuses the same identity and company discovery, then:

- **`discover.py`** builds the current network from Wikidata co-officers,
  family claims and named associates, and generates a global candidate pool by
  shared industry and shared occupation.
- **`scoring.py`** scores each candidate on five weighted components —
  role overlap (0.30), industry overlap (0.30), network proximity (0.20),
  geography (0.10), prominence (0.10) — storing the signals that produced the
  number so a relationship manager can disagree with the reasoning rather than
  a bare score.
- **`explain.py`** gates prominence to zero unless role or industry overlap is
  already non-zero. Prominence measures how well documented someone is, which
  tracks seniority *and* fame; ungated it surfaced famous strangers above
  relevant executives. Then one batched call writes a real rationale per
  suggestion — prose about a number it cannot change.

**LinkedIn is not used.** There is no public people-search API and its terms
forbid automated collection. This is the single biggest limit on network
coverage, and it is documented rather than worked around.

---

## Cost

`usage.py` counts at the call site and multiplies by rates in config. Nothing
is estimated.

- **Firecrawl** — per page and per search result. On `FIRECRAWL_PLAN=trial` the
  panel reports credits consumed and **no cash cost**, because reporting a list
  price you aren't paying is a fabricated number wearing a currency symbol.
- **OpenAI** — per token, taken from the `usage` block the API itself returns.
- **Everything else** — Wikipedia, Wikidata, SPARQL, Google News, Bing News,
  DuckDuckGo, the USD→INR rate — free and keyless.

A typical screening run: ~62 Firecrawl credits, ~85,000 OpenAI tokens, about
₹2.50–3.00 of real spend.

---

## Jobs and cancellation

Runs take minutes, too long to hold an HTTP request open. Work is submitted as
a job and polled.

Cancellation is cooperative: `POST /api/jobs/{id}/cancel` sets an event, and the
pipeline's progress callback checks it on every step. There is no safe way to
kill a thread mid-request, so the step reporter is the checkpoint — a stop
lands within a second or two and never mid-write. Without it, closing the
browser left the worker spending credits on a report nobody would read.

Progress percentage is computed **server-side**, because only the server has
the full history: the API sends the last 20 progress lines, and a client
counting phases in that window loses them on a long run.

---

## What it will not do

- Report absence of evidence as proof that none exists.
- Present an allegation as a finding.
- Attribute a company's conduct to someone who did not answer for it.
- Count syndicated copies of one story as independent corroboration.
- Score sentiment as risk — they are separate axes throughout.
- Let a model change a number.

## Known limits

Coverage is indexed public sources only; court and registry filings not
published online are out of scope. The publisher tier list and the adverse
taxonomy are weighted toward Indian business media and regulators, so a
non-Indian subject will score more sources as unknown tier. Thin-profile
subjects give thin reports — honestly labelled, but thin. And Google News
hides article URLs behind a redirector, so those items stay headline-only.
