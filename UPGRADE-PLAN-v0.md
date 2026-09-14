# Affluense — Upgrade Plan

Implementation plan based on inspection of the existing codebase.
5,541 lines Python / 29 modules · 2,504 lines TypeScript / 24 files · 7 observed runs.

**Status:** proposal only. No code written, no commands run.
**Scope:** 11 backend changes, 8 frontend changes, 9 new backend modules.

---

## 0. What the system should do

Two sentences that the rest of this document exists to deliver.

> **1.** Collect broad evidence and actively search adverse news → fetch full articles for important findings → let OpenAI understand and verify the evidence.
>
> **2.** Use that verified evidence to generate an explainable Low / Medium / High / Critical risk assessment, confidence, key findings, timeline, and useful network recommendations.

Read against the current build, those two sentences name every gap:

| Clause | Today | Change |
|---|---|---|
| collect broad evidence | one query per company | B1 |
| actively search adverse news | never happens | B1 |
| fetch full articles | headlines and snippets only | **B11** |
| OpenAI understands the evidence | regex keyword match | B4 |
| …and verifies it | no cross-source check | B5 |
| explainable risk assessment | does not exist | B6 |
| confidence | does not exist | B6 |
| key findings | category-grouped keyword hits | B3 |
| timeline | does not exist | B3, F6 |
| useful network recommendations | scored but unexplained | B10 |

---

## 1. Assessment

The architecture is sound and does **not** need replacing. Identity resolution, two-step candidate confirmation, entity merging with non-merge guards, the corroboration gate, tenure awareness, relevance filtering, the validator, the cost meter and the fetch/cache/job layer all work and are worth keeping.

Three things stop it being a product someone pays for.

### 1. It under-collects adverse evidence

`pipeline.py:549` issues exactly one news query per company: `f'"{company_name}"'`. `pipeline.py:610` does the same for the person. There is **no adverse query anywhere in the news path**. Firecrawl *does* run adverse angles (`pipeline.py:102`) but those results feed company discovery, not the evidence set.

Proof: Ashneer Grover / BharatPe returned 0 negative and 14 positive articles. The EOW complaint — the central fact about him — was never fetched.

### 2. It classifies by keyword, not comprehension

`enrich/risk.py` matches ~60 regex patterns and groups by category. It cannot tell whether an article is about the right company, whether the event fell within the person's tenure, whether two publishers describe the same event, or whether a matter was later dismissed. The output is "4 articles reference fraud", not "an EOW complaint was filed in 2022, charges not framed, reported by three independent outlets".

### 3. It never reaches a conclusion

No overall risk level exists anywhere in the codebase. `output.py:23` counts attributable flags and stops. The user reads eight cards and decides for themselves — the work they were paying to avoid.

> **Restructuring verdict: moderate, not major.** One schema change (`Flag` → `Finding`) ripples to the report and UI. One pipeline stage added between collection and scoring. Six new backend modules. No module is discarded; `enrich/risk.py` is demoted from classifier to prefilter.

---

## 2. Root cause map

Every observed failure traces to one of two places.

| Observed failure | Root cause | Fixed by |
|---|---|---|
| BharatPe: 0 adverse, EOW missed | Retrieval — never searched | B1 |
| Adani Energy: 26 positive / 0 negative | Retrieval + press releases | B1, B2 |
| Grofers flagged "arrest" via old CFO role | Attribution + no comprehension | B4, B8 |
| IL&FS fraud attributed to clean-up chairman | Tenure dates unavailable | B9 |
| Carnegie Endowment as corporate exposure | Entity classification | **done** |
| Adani Ports ×3, Zerodha ×2 | Name normalisation | **done** / B8 |
| No overall risk level | No scoring layer exists | B6 |

**Principle: retrieval before reasoning.** No model can assess evidence that was never collected. B1 and B2 come first, are deterministic, and are free — Google and Bing News RSS cost nothing.

---

## 3. Backend changes

### B1 — Adverse-first evidence collection · priority 1

**Current limitation.** One generic query per company and per person. Adverse events are found only by accident, when they happen to rank in generic coverage.

**Proposal.** A deterministic adverse query ladder. For every screened company and for the subject, issue the base query plus a fixed set of adverse templates built from the existing risk taxonomy: fraud, scam, lawsuit, litigation, investigation, probe, SEBI, RBI, ED, CBI, regulatory action, enforcement, penalty, arrest, charges, allegations, misconduct, corruption, money laundering, insolvency, NCLT, bankruptcy, default, tax, accounting, governance, conflict of interest, controversy, sanctions.

Queries are **templates, not model output**, so the retrieval set is reproducible and auditable.

**Files**

```
NEW  affluense/collect/queries.py
NEW  affluense/collect/__init__.py
     affluense/sources/news.py     collect() accepts a query list
     affluense/pipeline.py         collect_for(), person-level pass
     affluense/config.py           ADVERSE_QUERY_GROUPS, MAX_QUERIES_PER_ENTITY
```

**Data flow**

```
company -> [base, +fraud, +probe, +lawsuit, +regulatory, ...]
        -> Google RSS + Bing RSS (parallel, free)
        -> dedupe(shared seen set)      [already built]
        -> relevance filter             [already built]
        -> evidence set ~10x larger
```

**Accuracy.** The single largest gain available. On Ashneer Grover it is the difference between zero findings and the EOW complaint. It also corrects the positivity bias at source: generic company queries return what companies publish; adverse queries return what journalists publish.

**OpenAI:** none. **Deterministic:** everything.

**Tradeoff.** Run time ~80s → 3–5 min; requests ~45 → ~400, all free sources. Mitigated by existing per-host rate limiting and a configurable cap. **No additional Firecrawl credits** — this touches only the news path.

---

### B2 — Source tiering and promotional exclusion · priority 1

**Current limitation.** `enrich/sentiment.py:aggregate()` counts every article equally. A press release and a Reuters investigation weigh the same. This is how Adani Energy Solutions reached 26 positive / 0 negative.

**Proposal.** A publisher tier map.

| Tier | Contents | Treatment |
|---|---|---|
| 0 | PR Newswire, Business Wire, EIN Presswire, openPR, PRLog; the subject's and their companies' own domains | Excluded from evidence, counted separately as promotional |
| 1 | Reuters, Bloomberg, FT, AP, WSJ, major Indian business desks | Full weight |
| 2 | Trade and regional press | Standard weight |
| 3 | Aggregators, unknown domains | Counted, low weight |

Tone aggregation weights by tier. A material finding requires at least one Tier 1–2 source.

**Files**

```
NEW  affluense/enrich/publishers.py
     affluense/enrich/sentiment.py   aggregate(articles, weights)
     affluense/models.py             Article.publisher_tier, Article.is_promotional
```

**Accuracy.** Removes the largest known source of false positivity and makes every finding defensible by provenance. "Flagged on a Reuters report" is actionable; "flagged on 4 articles" is not.

**OpenAI:** none — domain matching is exact. The model handles only the residue, in B4.

**Tradeoff.** The tier map is a maintained list and will never be complete. Unknown domains default to Tier 3 rather than being dropped, so an unlisted publisher degrades gracefully.

---

### B3 — Replace `Flag` with `Finding` · priority 1

**Current limitation.** `models.Flag` carries category, stage, a generated summary string and up to five URLs. `risk.build_flags()` produces one flag per category per company by grouping keyword hits. It has no concept of a discrete *event*, no event date, no severity, no per-finding confidence, no independent corroboration, and no way to record that two publishers described the same matter or that one contradicted another.

**Proposal.** A `Finding` is one real-world event assembled from one or more articles. It becomes the central object of the report and the unit the UI renders.

```
event_id, what_happened, entity, entity_role,
category, stage, event_date, is_ongoing, severity (1-5),
confidence, attributed_to_subject, within_tenure,
corroborating_publishers[], contradicting_sources[], evidence[]
```

**Files**

```
     affluense/models.py       add Finding, EvidenceItem; keep Flag one release
     affluense/enrich/risk.py  demoted to prefilter; build_flags() unused in main path
     affluense/output.py       report carries findings[]
```

**Tradeoff.** Breaking change to the report JSON and CSV. Single consumer, so contained — but the frontend must ship in the same release.

---

### B4 — AI evidence analysis · priority 1

**Current limitation.** Regex decides what an article means. It cannot distinguish "SEBI clears firm" from "SEBI probes firm", cannot tell a namesake from the target, and cannot read a date out of prose.

**Proposal.** Batched extraction. For each company, all surviving articles go to the model in **one call**, with the target entity, the subject's name, and the known role period. The model returns one strict JSON object per article. It is never asked to assess overall risk, and never asked for facts from memory — only to read the text it is given.

Returned per article:

```
is_about_target, is_journalism, is_promotional, is_relevant,
event_type, event_summary, event_date, stage,
actors[], actor_is_subject, severity, confidence,
supersedes_earlier_report, reason
```

**Files**

```
NEW  affluense/enrich/extract.py
     affluense/sources/openai_client.py   batch helper, larger budget
     affluense/config.py                  OPENAI_MAX_EXTRACT_CALLS
```

**How OpenAI is used.** Heavily and on every run, but bounded: one call per company plus one for the person, ~20–40 articles each. Roughly ₹2–5 per run at `gpt-4o-mini` rates. Temperature 0, JSON mode, routed through the existing `Fetcher` so identical prompts are cached and billed once.

**Stays deterministic.** Article dedup, publisher tiering, date arithmetic, tenure comparison, and the risk rollup. The model reports on evidence; it never computes a score.

**Tradeoff.** The model can be wrong. Mitigated three ways: every extraction cites the article it came from; a finding needs corroboration before it is material (B5); a failed or malformed response falls back to the keyword path rather than producing nothing.

---

### B5 — Event clustering and cross-source verification · priority 2

**Current limitation.** Ten articles about one SEBI notice read as ten pieces of evidence. There is no way to say "three independent outlets report this" versus "one outlet, syndicated ten times".

**Proposal.** Cluster extracted events into findings. A deterministic first pass keys on `(entity, event_type, event_year)`; the model adjudicates only genuinely ambiguous merges and any contradiction — one source reporting charges, another reporting dismissal. Independent corroboration is counted by distinct **publisher**, not by article, so syndication cannot inflate it.

**Files**

```
NEW  affluense/enrich/cluster.py
     affluense/enrich/publishers.py   publisher identity for independence
```

**Accuracy.** Turns article counts into event counts, which is what a reviewer actually needs. It also surfaces contradictions instead of silently taking whichever article was scored last.

**Deterministic:** clustering keys, corroboration counting, publisher independence.

---

### B6 — Risk scoring · priority 1

**Current limitation.** No overall assessment exists.

**Proposal.** A deterministic rubric in Python. The model contributes severity and stage *per finding*; the rollup to an overall level is arithmetic, so the same findings always produce the same level and the reasoning can be printed. Risk and confidence are computed and reported separately.

**Risk inputs** — highest severity among attributable findings, legal stage, recency, whether the matter is ongoing, the subject's actual relationship to the entity, number of independent corroborating publishers, best source tier.

**Confidence inputs** — identity confirmed by the user, evidence volume and coverage completeness, corroboration depth, presence of contradictions, proportion of companies with registry backing, validator status across rows.

**Levels**

| Level | Meaning |
|---|---|
| `LOW` | No material adverse findings in available evidence |
| `MEDIUM` | Historical, resolved, or unattributed matters |
| `HIGH` | Ongoing investigation, regulatory action or litigation attributable to the subject |
| `CRITICAL` | Charges, conviction, sanctions, or fraud with the subject named |

**Files**

```
NEW  affluense/risk_score.py
     affluense/validate.py    coverage feeds confidence
     affluense/output.py      assessment block
```

**Why deterministic.** A compliance decision must be reproducible and explainable. A model that answers "HIGH" differently on two runs of identical evidence is not defensible to a regulator and cannot be unit tested.

**Tradeoff.** The rubric's thresholds are judgement calls needing tuning against real subjects. They live in one module with tests, so tuning is a visible, reviewable change rather than a prompt edit.

---

### B7 — Final QC review · priority 3

**Proposal.** One model call over the assembled assessment. Given the findings, the risk level and the reasons, it is asked: is any claim unsupported by its cited evidence, do any findings contradict, is the stated confidence consistent with the coverage, is any material caveat missing. It returns **issues only — it never edits the data**.

**Files**

```
NEW  affluense/qc.py
     affluense/output.py
```

**Why review rather than rewrite.** A model that can rewrite the report can also launder an error into confident prose. Restricting it to raising issues keeps every number traceable to the deterministic layer that produced it.

---

### B8 — Entity classification · priority 2

**Current limitation.** `resolve/company.py:relationship_type()` returns five values: `control`, `public_office`, `employment`, `philanthropy`, `unknown`. `cfo` currently counts as control — which is how a former Grofers CFO role became attributable exposure.

**Proposal.** Expand to: `current_company`, `former_employer`, `investment`, `board_seat`, `executive_role`, `subsidiary`, `parent`, `nonprofit`, `regulator`, `government_body`, `think_tank`, `trade_association`, `media_role`, `alias`. Deterministic rules run first; the model resolves only what the rules leave as `unknown`, and its answer is recorded with its reasoning.

**Files**

```
NEW  affluense/resolve/relationships.py
     affluense/resolve/company.py    rules move out, guards stay
     affluense/validate.py           attribution follows the new vocabulary
```

**Also fixes.** "Amazon MX Player" treated as a company when the role was TV host; the Zerodha / ZERODHA BROKING LIMITED pair, via a parent/subsidiary relation rather than a name merge.

---

### B9 — Tenure dates from Wikidata · priority 2

**Current limitation.** `enrich/tenure.py` works, but only from period text a page happens to state. `wikidata.companies_for()` does not request the `P580`/`P582` statement qualifiers, so any role found via Wikidata is undated and nothing is filtered.

**Proposal.** Extend the SPARQL in `companies_for()` to return start and end qualifiers, and pass them onto the company record so `role_period()` can use them. Undated still means "do not filter" — never "assume current".

**Files**

```
     affluense/sources/wikidata.py
     affluense/enrich/tenure.py
     affluense/pipeline.py
```

**Accuracy.** Completes the IL&FS fix. The mechanism exists but currently fires only when Firecrawl happened to extract a period string.

---

### B10 — Network quality (Problem Statement 2) · priority 3

**Current limitation.** `network/scoring.py` is good and should be kept — five weighted components, every signal stored with its reason. Two weaknesses: `prominence` rewards fame, which surfaces celebrities when role and industry overlap are weak; and the "why" is a template string rather than a reason.

**Proposal.** Keep the deterministic score exactly as it is. Add a gate — prominence contributes nothing unless role or industry overlap is already non-zero, which removes the famous-stranger failure. Then one batched model call writes a real relevance rationale per suggestion from the stored components, and flags any suggestion whose evidence does not support its score.

**Files**

```
     affluense/network/scoring.py
NEW  affluense/network/explain.py
     affluense/network/output.py
```

**Deterministic:** the score and the ranking. The model writes prose about a number it cannot change.

---

### B11 — Full-article retrieval for material candidates · priority 1

**Current limitation.** Everything downstream sees only an RSS headline and a one-line snippet. That is enough to *notice* a matter and far too little to *assess* one: the stage, the date, who was named, whether a case was later dismissed and whether the subject is even mentioned all live in the body text. Asking a model to judge severity from a headline is asking it to guess.

**Proposal.** Between retrieval (B1/B2) and extraction (B4), fetch the body text of articles that look material — and only those.

**Selection is deterministic and budgeted.** An article qualifies for full-text fetch when the existing `enrich/risk.py` prefilter finds a risk term *and* the publisher is Tier 1–2 *and* it is not already a duplicate of a fetched story. Cap per run (`MAX_FULLTEXT_FETCHES`, default ~40). Everything else stays headline-only, which is sufficient for tone.

**Fetching is free by default.** `requests` + `beautifulsoup4` are already dependencies, and `http.py` already has rate limiting, retries, caching and a `robots_allow()` check. A plain fetch and text extraction handles most news sites at zero cost. Firecrawl is the fallback for JS-walled pages only, behind its own small budget (`MAX_FULLTEXT_FIRECRAWL`, default 0 — opt in).

**Files**

```
NEW  affluense/collect/fulltext.py        selection, fetch, boilerplate stripping
     affluense/models.py                  Article.body_text, Article.fetch_status
     affluense/sources/firecrawl.py       reused as fallback only
     affluense/config.py                  MAX_FULLTEXT_FETCHES, MAX_FULLTEXT_FIRECRAWL
     affluense/enrich/extract.py          prompt carries body_text when present
```

**Data flow**

```
evidence set (B1/B2)
  -> risk prefilter + tier gate      deterministic, free
  -> fetch body text                 plain HTTP, robots-checked, cached
     (Firecrawl only if opted in)
  -> extraction sees full text       B4
  -> findings quote real sentences   B3
```

**Accuracy.** This is what makes a finding quotable. `EvidenceItem.quote` becomes a real sentence from the article rather than a truncated headline, which is the difference between "4 articles reference fraud" and "*the EOW registered an FIR naming Grover on 2022-11-  …*". It also materially improves stage detection: "charges framed", "petition withdrawn", "clean chit" almost never appear in a headline.

**OpenAI:** none for fetching. The longer text raises extraction cost — roughly ₹5–15 per run rather than ₹2–5, still far below the stated threshold.

**Stays deterministic.** Which articles qualify, the fetch itself, robots compliance, caching, boilerplate stripping, the budget.

**Tradeoffs.**

- Adds 30–90s to a run; fetches are parallel and cached.
- Paywalled articles return partial or no text. Recorded as `fetch_status: "paywalled"` and the finding falls back to headline evidence with lower confidence — never silently treated as though the text was read.
- `robots_allow()` must be honoured on every fetch. An article we are not permitted to fetch is skipped and marked, not worked around.

---

## 4. JSON contract

Additions marked `+`; everything unmarked exists today and is unchanged.

```jsonc
  "query", "subject", "identity"                 // unchanged

+ "assessment": {
+   "risk_level": "LOW | MEDIUM | HIGH | CRITICAL",
+   "confidence": "HIGH | MEDIUM | LOW",
+   "headline": "No material adverse findings identified",
+   "reasons": [ { "factor", "detail", "finding_ids" } ],
+   "confidence_basis": [ ... ],
+   "limitations": [ ... ],
+   "recommended_action": "proceed | review | escalate"
+ },

+ "findings": [ {
+   "event_id", "what_happened", "entity", "entity_role",
+   "category", "stage", "event_date", "is_ongoing",
+   "severity": 1-5, "confidence": 0-1,
+   "attributed_to_subject": bool, "within_tenure": bool,
+   "corroborating_publishers": [ "Reuters", "Mint" ],
+   "contradicting_sources": [ ... ],
+   "evidence": [ { "url", "publisher", "tier", "published",
+                   "quote",          // a real sentence when full text was read
+                   "fetch_status"    // "full" | "partial" | "paywalled" | "headline_only"
+                 } ]
+ } ],

  "screening": [ {
      // ... existing company fields ...
+     "relationship_class": "current_company | board_seat | ...",
+     "finding_ids": [ ... ],
+     "coverage": { "journalism", "promotional_excluded", "tier1" },
      "validation": { "status", "issues" }        // already shipped
  } ],

+ "timeline": [ { "date", "kind", "label", "finding_id" } ],
+ "coverage": { "queries_issued", "articles_retrieved", "after_dedup",
+               "promotional_excluded", "publishers", "by_tier",
+               "fulltext_fetched", "fulltext_paywalled" },
+ "qc": { "issues": [ ... ], "reviewed_by" },

  "validation", "usage", "notes", "disclaimer"    // unchanged
```

Two properties worth preserving through review:

1. **Every finding is addressable.** `event_id` lets a company row, a timeline entry and a risk reason all point at the same object instead of duplicating it.
2. **Risk and confidence never collapse into one number.** `HIGH` at high confidence and `HIGH` at low confidence require different actions from the person reading it.

---

## 5. Frontend changes

| # | Section | Component | Consumes |
|---|---|---|---|
| F1 | Executive Risk Summary | `risk-summary.tsx` **new** | `assessment` |
| F2 | Key Findings | `findings.tsx` **new** | `findings[]` |
| F3 | Connected Companies | `v2/screening-view.tsx` *rework* | `screening[]` |
| F4 | News & Sentiment | `coverage.tsx` **new** | `coverage`, `screening[]` |
| F5 | Legal & Regulatory | `findings.tsx` *filtered* | `findings[]` by category |
| F6 | Timeline | `timeline.tsx` **new** | `timeline[]` |
| F7 | Network + Suggestions | `v2/network-view.tsx` *extend* | network report |
| F8 | Evidence & Data Quality | `data-quality.tsx` **new** | `coverage`, `validation`, `qc` |

### The first screen

A risk banner carrying the level, the confidence, a one-line headline and the recommended action — then the reasons that produced it, each linked to the findings behind it. Six figures beneath: companies analysed, evidence items, adverse findings, legal and regulatory matters, network connections, coverage completeness.

For a clean subject it must **not** say "0 negative articles". It says *no material adverse findings identified in the available public evidence*, then shows what was actually checked — queries issued, publishers reached, tier-1 coverage, legal and regulatory checks run — and states any limitation. A low-risk verdict is only worth something if the reader can see the search behind it.

### Also required

- Section navigation, since the report becomes long enough to need it.
- Every finding card expands to its evidence: publisher, tier, date, quote, link.
- Findings outside the subject's tenure render in a separate, visibly demoted group — never in the main list.
- Confidence shown adjacent to risk everywhere the level appears, never on its own.

> **Hard constraint carried from the current build.** Nothing in the UI may be computed client-side. Every number arrives finished from the backend — that rule is what makes the report reproducible from the JSON and the CSV.

---

## 6. Keep, change, remove

### Keep unchanged — this is the load-bearing work

- **Two-step identity flow.** `resolve/candidates.py`, `/api/candidates`, the picker. Confirmed identity is the foundation the whole assessment rests on.
- **Transport layer.** `http.py`, `cache.py`, `usage.py` — rate limiting, retries, robots, caching and the cost meter. B1 multiplies request volume by ten; this layer is what makes that safe.
- **Merge guards.** `STRUCTURAL_TOKENS`, the Wipro / Wipro Enterprises and Tata Motors / Tata Motors Finance tests. These prevent moving one company's adverse news onto another.
- **Corroboration gate** (`_corroborated_stakes`), **relevance filter**, **validator**, **tenure**, **cross-company dedup**. All recently built, all tested, all still correct under the new design.
- **VADER as tone.** Local, free, deterministic and — critically — *separate from risk*. Findings come from evidence extraction, not from tone.

### Change

- **Demote** `enrich/risk.py` from classifier to prefilter. Its patterns are good at *finding candidate articles* and bad at *deciding what they mean*. Keep the taxonomy; stop treating its output as a finding.
- **Rework** `pipeline.py` (671 lines) — split the screening stage into collect → extract → cluster → score. It is already the largest module and will otherwise grow past readability.
- **Fix** `validate.py` — `MIN_LINK_CONFIDENCE` is `< 0.6` and Firecrawl rows score exactly `0.6`, so the weakest links in every report are the ones the validator cannot see. Also widen the one-sided-coverage rule from "zero neutrals" to "≥90% one tone", which is why 26/1/0 passed while 13/0/0 was caught.

### Remove

- `src/app/screening/page.tsx`, `src/app/v1/page.tsx`, `src/data/dossier.json`, `src/data/network.json` — a synthetic demo page and an older dashboard. Shipping a page built on fabricated data alongside a real due-diligence tool is a liability.
- `components/entities-view.tsx`, `output-view.tsx`, `screening-file.tsx`, `network-view.tsx` (root), `live/*`, `lib/types.ts`, `lib/derive.ts` — roughly 600 lines reachable only from the pages above.

### Add

**Backend** — `collect/queries.py`, `collect/fulltext.py`, `enrich/publishers.py`, `enrich/extract.py`, `enrich/cluster.py`, `risk_score.py`, `qc.py`, `resolve/relationships.py`, `network/explain.py`

**Frontend** — five new components (F1, F2, F4, F6, F8)

---

## 7. Build order

Retrieval before reasoning; schema before the things that populate it; the risk banner last, because a confident wrong verdict is worse than no verdict.

| Stage | Work | Why here | Testable on |
|---|---|---|---|
| 1 | B1, B2 | Free, deterministic, largest accuracy gain. Everything downstream reads whatever these produce. | Ashneer Grover — the EOW matter must appear |
| 2 | B3 | Schema first, so B4, B5 and B11 have somewhere to write. | Unit tests only |
| 3 | B11 | Full text before comprehension — the model should read articles, not headlines. Free, deterministic, no model involved. | Ashneer Grover — body text must name him |
| 4 | B4, B5 | Comprehension and corroboration over the enlarged, full-text evidence set. | Adani — 26/0 must become realistic |
| 5 | B8, B9 | Attribution correctness feeds the score. Must land before scoring, or the score inherits the Grofers error. | Ashneer Grover, Uday Kotak |
| 6 | B6 | Only now is there enough correct input for a level to mean anything. | All prior subjects, re-run |
| 7 | F1–F8 | The dashboard follows the contract, once the contract is stable. | Visual review |
| 8 | B7, B10 | Polish. Valuable, not load-bearing. | Regression set |

> **Checkpoint after stage 1.** Re-run Ashneer Grover and Gautam Adani *before* any AI work begins. If adverse retrieval alone surfaces the EOW complaint and corrects the Adani positivity, stages 3 onward are refinement rather than rescue — and the plan can be re-scoped with evidence instead of assumption.

---

## 8. Risks and tradeoffs

| Risk | Severity | Mitigation |
|---|---|---|
| Run time 80s → 4–7 min | MEDIUM | Parallel fetch already exists; progress bar already reports phases. Configurable query and full-text caps. This is a tool people wait for, not a page load. |
| Paywalled article bodies | MEDIUM | Recorded as `fetch_status: "paywalled"`; the finding falls back to headline evidence at lower confidence. Never treated as though the text was read. |
| Scraping article pages at volume | MEDIUM | `robots_allow()` honoured on every fetch, existing per-host rate limiting, cached. An article we may not fetch is skipped and marked, not worked around. |
| Model extraction errors | HIGH | Every extraction cites its article; corroboration required before a finding is material; keyword path as fallback; QC pass raises contradictions; temperature 0. |
| Over-flagging once adverse retrieval lands | HIGH | Retrieval deliberately biased adverse, but *flagging* still requires stage progression or corroboration. Alarm fatigue is the failure mode the existing `is_flagworthy` was written to prevent — keep that discipline. |
| OpenAI cost rises ~100× | LOW | ₹0.01 → roughly ₹5–15 per run once full article text is in the prompts. Bounded by per-purpose call ceilings and the full-text cap. |
| Firecrawl trial exhaustion | MEDIUM | No change — B1 touches only free news sources. Set `FIRECRAWL_TRIAL_CREDITS` so the panel can report what remains. |
| Breaking JSON/CSV contract | LOW | Single consumer, shipped in the same release. |
| Risk rubric mis-tuned | MEDIUM | One module, unit tested, thresholds visible in review. Validate against every subject already run before shipping the banner. |

> **The one that matters.** A due-diligence product fails asymmetrically. A false positive gets checked and dismissed; a false negative gets trusted. Ashneer Grover currently returns *nothing adverse* — that is the failure mode to design against at every stage, and the reason retrieval breadth comes before every other change in this plan.
