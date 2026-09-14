# Issues found and how they were fixed

Every entry below is a defect that showed up in a real run, not a
hypothetical. Two at the end are still **open**.

---

## Identity resolution

### The wrong person was screened
`"Ratan Tata" + "Tata Sons"` resolved to **Ratanji Tata (1871–1918)**, because
the supplied company was added to the search query and changed the ranking.
The company now *verifies* the match instead of steering it: name similarity
picks the article, then the company is checked against the article text.

### A misspelling silently emptied half the report
`"virat kholi"` returned nothing from Wikidata, which does no fuzzy matching,
so companies and connections both came back as 0.
Wikipedia resolves the spelling first (it tolerates typos) and its corrected
title is what Wikidata is asked for. The correction is recorded in the output.

### The supplied company was never checked
`"Mukesh Ambani" + "Tata Sons"` reported **95% confidence** and claimed the
company "was used to steer the search" — but Tata Sons appeared nowhere in his
ten companies.
If the supplied company is absent from the discovered set, confidence drops to
0.55 and the basis says explicitly that it did not corroborate the match.

---

## Entity quality

### A private residence was screened as a company
**Antilia** — Mukesh Ambani's house — arrived via Wikidata's "owned by" and
became the *only* flagged entity, carrying findings from a criminal case that
had nothing to do with him.
Company discovery now requires `?org wdt:P31/wdt:P279* wd:Q43229` — an
organisation. The filter also surfaced Reliance Foundation and Reliance Retail,
which had been crowded out.

### The same company appeared three times
"Vault by Virat Kohli", "Vault" and "Vault fitness chain" counted as three
entities; so did "Blue Tribe Foods" / "Blue Tribe" and "Digit" / "Go Digit".
Names normalise to a comparison key — legal forms, descriptors and the
subject's own name stripped — then merge, keeping every spelling as an alias.
Measured: 6 records → 3 entities.

### Two real companies were merged into one
The fix above went too far: **Wipro Enterprises** normalised to "wipro" and
vanished into **Wipro**, under-reporting the footprint.
Records carrying different registry identifiers never merge, whatever their
names look like. A registry id is an identity.

### One company showed eleven spellings of the same role
Reliance listed "Chairman & Managing Director", "Chairman and MD", "Chairman
and Managing Director", "Founder", "founder", "Various roles"…
Roles normalise the way company names do: abbreviations expand (two passes, so
`CMD` → `chairman and managing director` → `chair …`), gendered and neutral
spellings unify, and the fullest phrasing survives. "Former Chairman" stays
distinct from "Chairman", because tenure matters.

---

## Adverse-media detection

### "ED" matched "backed", "supported" and "expanded"
Substring matching on `"ed "` (for Enforcement Directorate) produced six false
"investigation" flags on a clean subject.
All risk terms became word-boundary regexes. A test pins the exact headlines
that used to fire.

### Seven of eight companies were flagged
Every Jio entity carried `regulatory · reported` — telecom and banking news
names its regulator constantly. A flag on everything informs nothing.
A keyword alone no longer raises a flag: the article must also show a matter
progressing past bare mention, **or** negative tone. The two axes stay
independent; one just has to corroborate the other.

### The articles behind a flag were invisible
One8 Commune carried four findings and **not one** of the articles that caused
them appeared in the output — the code kept the first six by collection order.
Flagged articles are now retained first. A flag citing a URL with no headline
attached contradicts the whole provenance rule.

---

## Sentiment

### VADER scored finance headlines as neutral
`"Firm faces insolvency petition at NCLT"` → **0.000**. So did
`"Regulator penalises bank over KYC lapses"`.
A ~170-term finance and regulatory lexicon was added on top of VADER, which
keeps its negation and intensifier handling. All three test headlines now score
clearly negative.

### The lexicon had token-level holes
VADER matches tokens exactly, so `"regulatory"`, `"shuts"`, `"raided"` and
`"dues"` were all absent even though `"regulator"` and `"raid"` were present.
`"Shuts Down Due To Regulatory Issues"` scored 0.00.
~45 inflections added. One8 Commune went from 5 negative articles to 12.

### A company with no coverage read "neutral"
Four Kohli LLPs showed `neutral` on **zero** articles, which reads as "balanced
coverage" rather than "nothing found".
Zero-coverage rows carry `insufficient_coverage` and display as "no data".

### A company with four findings still read "neutral"
One8 Commune sat at 30% negative coverage, just under a 0.35 threshold.
Thresholds are now asymmetric: **negative ≥ 0.25**, positive ≥ 0.35. Adverse
coverage matters disproportionately in screening, and claiming a company is
*well* regarded is a stronger assertion than flagging it for a look.

### The evaluation number was flattering itself
After tuning the lexicon on a 60-headline set, accuracy read 0.90 — but the
tuning had used that same set.
A 21-headline holdout was written afterwards and never tuned on: **0.67
accuracy, 0.66 macro F1**. Both numbers are published, with the holdout named
as the one to believe.

### A missing dependency degraded quality silently
Without `vaderSentiment` the classifier falls back to bare keywords: holdout
accuracy **0.48**, positive F1 **0.00**, half of adverse headlines missed. It
printed one soft line and carried on.
It now raises a `RuntimeWarning` naming the measured drop, both CLIs warn
before running, and the backend label reads `DEGRADED`.

---

## Network suggestions

### The network pipeline found 0 companies where screening found 10
It queried Wikidata alone, so any subject whose companies are private returned
nothing — and then produced an empty suggestion list with no explanation.
Company discovery was extracted into one function both pipelines call. Same
question, same answer.

### An empty result never said why
Zero suggestions rendered as silence, which reads as "this person has no
relevant connections" — a far stronger claim than "the sources carry no
industry data".
When no industry can be established the pipeline says so in the notes, then
falls back to occupation-matched peers and **labels them as weaker**. Scores
land at 0.10–0.13 against 0.67–0.76 for a real industry match.

### "Foundation" was treated as an industry
Extraction reads sector words off company names, so `SEVVA PATH SANKALP
FOUNDATION` yielded "foundation" — which resolved to a real Wikidata item and
returned five Danish pharmaceutical-foundation executives as peers for an
Indian cricketer.
A stoplist drops legal forms and generic corporate functions before resolution,
and prints what it ignored.

### A long-dead founder was suggested as a connection
Andrew Carnegie (d. 1919) appeared as a suggested contact because he founded an
endowment the subject sits on.
Candidate queries exclude anyone with a date of death. A suggestion list has to
contain people who can take a meeting.

### Everyone scored as "extending beyond the subject's markets"
The geography component never fired: `subject_profile` read `country`, while
discovery records carry `jurisdiction`. The profile's country list was always
empty, so Indian peers of an Indian subject scored as foreign.
Both keys are read now.

---

## Cost and performance

### Half the Firecrawl budget went to Instagram
Three of six page reads were login-walled social URLs returning 403 — paid for,
zero return — and the budget ran out before reaching pages that mattered.
**Wrogn was in the search results and never read.**
Login-walled domains are skipped before a scrape is attempted, and registry and
business sources are ranked first. Measured: 15 of 31 results skipped free,
useful reads 3 → 6 at identical cost, and Moneycontrol then supplied WROGN
₹20cr, Agilitas ₹58cr, Rage Coffee ₹19cr.

### Anchoring the search on the company lost the registry pages
Adding `"One8 Commune"` to the role query narrowed results enough that
ZaubaCorp and FalconeBiz — which carry the filed company list and the Director
Identification Number — never appeared. Companies dropped 10 → 4.
The role query is unanchored; the company became a separate, later angle.

### One 429 dropped an entire source
DuckDuckGo returns 202 and GDELT returns 429 under load, and a single one
discarded the whole source.
Retry with backoff on 202/429/502/503/504. News items went 26 → 39 on the same
query.

### The query service timed out on every run
A single five-way `UNION` across 40 companies returned 504 consistently.
Split into one small query per property. Connections went 6 → 35.

### Every run re-fetched everything
No cache, no concurrency: ~40 serial requests per screening.
Content-addressed disk cache with a TTL per source class — news hourly,
registry weekly, **Firecrawl weekly so credits are never spent twice** — plus a
thread pool over the per-company screen and per-host rate limiting. Second run:
**87% cache hit rate, 75s → 43s**.

---

## Provenance

### Syndicated copy was counted several times
48 stored news items were 44 distinct stories; RSS appends `" - Publisher"`, so
one wire story arrived as three rows.
The dedupe key strips the publisher suffix before comparing.

### Pages were attributed to the aggregator, not the publisher
Google and Bing links bounce through their own domains, so a fetched page
recorded `bing.com` as its source.
The post-redirect URL is recorded. A page now reads `livemint.com`.

---

## Frontend and API

### "Backend unreachable" with the backend running
`OPTIONS /health → 400`. A leftover dev server held port 3000, Next fell back to
**3001**, and that origin was not in the CORS allow-list.
Any loopback port is accepted for local work; `ALLOWED_ORIGINS` pins it down for
deployment. External origins are still rejected.

### The CSV endpoint crashed after a successful run
A new field was added to the CSV row but not to the column list, so
`DictWriter` raised after the JSON had already been written.
Column list corrected, and a test now writes a CSV from a full report and
asserts the header.

---

## Identity, second pass

### The closest available name was returned as a match
`"Aravind Srinivas" + "Perplexity"` reported **Andy Konwinski at 95%
confidence** — a different person — and attributed six adverse findings to the
query name. He has no Wikipedia article, so he was never a candidate; Andy
Konwinski scored 0.47 on name similarity and the company bonus (+0.30, because
his article names Perplexity) carried him past everyone.
A candidate must now clear a **0.55 name-similarity floor** before the company
bonus applies at all. The company breaks ties between plausible candidates; it
can no longer rescue an implausible one. Returning nobody is now a valid answer.

### A film was screened as a person
`"Aravind Srinivas"` matched **"Aravind 2", a 2013 Indian film**, and the run
attached a sex-determination racket arrest to a real person's name.
Candidates whose Wikipedia description names a film, album, building, town,
company or similar are rejected outright, along with disambiguation and list
pages. The check reads a field the code already had and was ignoring.

### A red verdict rendered on an unidentified subject
The run above correctly reported 45% confidence and still displayed
*"1 of 2 companies carry adverse coverage"* in red, as a finding.
Subjects now carry `identity_unverified` below 0.60. The banner turns amber and
reads *"Could not confirm who this is, so nothing below is attributed to a
person"*, the adverse stat is relabelled "unattributed", and the heading shows
the **resolved** name rather than the query — it previously showed "Aravind
Srinivas" above a report about Andy Konwinski.

### The same company appeared twice in one report
"Perplexity AI" rendered as two rows, one from Wikidata with a registry id and
one from extraction without. Keying the merge on the id itself caused it.
Merging is now two-pass: group by normalised name first, then split a group
only when it contains **two different** registry ids. Wipro and Wipro
Enterprises still separate; identical names merge.

### React duplicate-key warning
Two company rows sharing a display name produced
`Encountered two children with the same key`.
The key is now the name plus the registry id, falling back to the index.

---

## What the identity fix changed in practice

Before, `"Aravind Srinivas" + "Perplexity"` screened Andy Konwinski's companies
at 95% confidence. After, the resolver declines to name anyone, the search runs
on the name as typed, and Firecrawl returns **his actual footprint** —
Perplexity (CEO and co-founder), OpenAI, Google, Google DeepMind — with the
banner stating plainly that the identity is unverified.

Refusing to guess produced a better answer than guessing did.

---

## Still open

### Brand variants of one company still split
"Perplexity" and "Perplexity AI" remain two rows, as do "UC Berkeley" and
"University of California Berkeley". The normaliser strips legal forms and
generic descriptors but not product suffixes or abbreviations.
Low severity: it inflates the company count without inventing anything.

### Employers are screened as connected companies
OpenAI and Google appear for a former employee, and their general news is read
as his coverage.
Defensible for a screening tool — an employment history is part of a footprint
— but the relationship type should be surfaced so a reader can tell a
directorship from a job.
