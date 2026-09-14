# PS2 — data sources and where production APIs plug in

Problem Statement 2 asks for the individual's current network, globally
discoverable suggestions matched on role and industry, and a relevance score,
delivered as JSON or CSV.

This documents which source answers which part, what happens when one fails,
and exactly where LinkedIn and Crunchbase would be wired in.

---

## What answers what today

| PS2 requirement | Source | Status |
|---|---|---|
| Current network — co-officers | Wikidata SPARQL (`coofficers`) | Works when Wikimedia accepts the request |
| Current network — family, memberships | Wikidata entity claims | Same |
| Current network — colleagues named in coverage | Firecrawl page extraction (`associates`) | Always available with a Firecrawl key |
| Candidate pool — by industry and role | Wikidata SPARQL (`candidates_by_industry`) | Primary path |
| Candidate pool — by occupation | Wikidata SPARQL (`candidates_by_occupation`) | Fallback when no industry resolves |
| **Candidate pool — from coverage** | **Google / Bing News RSS + extraction** | **V2 addition, free and keyless** |
| Role / industry similarity | `network/scoring.py` | Deterministic |
| Relevance score | `network/scoring.py` | Weighted sum, components exposed |
| JSON / CSV output | `network/output.py` | Both |

---

## The single point of failure V2 removed

Every candidate path in V1 runs through Wikidata. On the first live runs the
Query Service returned **HTTP 403 on every request**, so `candidates_by_industry`
and `candidates_by_occupation` both produced nothing and PS2's deliverable was an
empty list — not a degraded one, an empty one.

Two things address that, and neither replaces Wikidata as the primary source:

**1. Identification.** Wikimedia's User-Agent policy requires a real contact and
refuses requests carrying an obvious placeholder. V1's constant ships with
`contact: set-your-email-here`. V2 sends its own header built from
`AFFLUENSE_CONTACT`, and a 403 from a Wikimedia host now says what to set rather
than reading as an outage.

```
# backend/.env
AFFLUENSE_CONTACT=you@yourdomain.example
```

**2. A second candidate path.** `affluense_v2/network/peers.py` builds the pool
from news coverage: queries of the form `"<role>" "<industry>"` go out through
the existing concurrent transport, and the reasoning layer is asked which people
those headlines name, with what company and title. It is told explicitly to
return nothing when a headline names no individual, and any answer not tied to a
supplied headline is discarded.

When neither Wikidata path resolves an industry, sector strings named on the
companies themselves become the industry identifiers. `scoring` intersects two
sets and never inspects their values, so a stable string key works exactly as a
Q-number does — industry matching keeps working on a run where nothing resolved
to a registry.

News-derived candidates are labelled in the report (`candidate_sources`), carry
the headline and publisher they came from, and the UI states that their role and
company are not registry-verified.

---

## Where LinkedIn and Crunchbase go

Both are the *right* sources for PS2 in production. They carry current
employment, seniority, tenure and funding relationships directly, which is what
the problem statement is really asking for, and neither requires the inference
the news path does. Both are commercial and access-gated, so neither is wired in
here.

**The integration point is one function.** `peers.discover()` returns a list of
candidate dicts, and `agents/network.py` appends that list to the pool before
scoring. A new provider only has to produce the same shape and be appended
alongside:

```python
# affluense_v2/agents/network.py
pool.extend(await news_peers.discover(...))
pool.extend(await linkedin_peers.discover(...))     # same contract
pool.extend(await crunchbase_peers.discover(...))   # same contract
```

### The contract

```python
{
    "name": str,                    # required
    "wikidata_id": str | None,
    "roles": [str],                 # job titles; drives role_overlap
    "companies": [str],             # drives network_proximity
    "matched_industries": [str],    # must intersect profile["industry_qids"]
    "country": str | None,          # drives geography
    "sitelinks": int,               # prominence proxy; 0 when not applicable
    "source": str,                  # shown to the user
    "source_url": str | None,       # the basis for the suggestion
}
```

### Field mapping

| Contract field | LinkedIn | Crunchbase |
|---|---|---|
| `name` | `firstName` + `lastName` | `person.name` |
| `roles` | `positions[].title` | `person.primary_job_title` |
| `companies` | `positions[].companyName` | `person.primary_organization.name` |
| `matched_industries` | `company.industries[]`, mapped through `peers.sector_key` | `organization.categories[]`, same mapping |
| `country` | `geoLocation.country` | `person.location_identifiers[]` |
| `sitelinks` | — (use `0`; prominence is a registry proxy) | — (use `0`) |
| `source_url` | `publicProfileUrl` | `person.permalink` |

### Access notes

- **LinkedIn** — the public Marketing/Talent APIs do not expose third-party
  profile search. People search needs Sales Navigator or a partner agreement;
  scraping profiles breaks the User Agreement and is not an option here.
- **Crunchbase** — `/searches/people` on a paid Enterprise plan covers role,
  organisation and category, which maps cleanly onto the contract above. This is
  the lower-friction of the two.

Both should sit **above** the news path in priority and below Wikidata only where
Wikidata has a stable identifier, because a registry-verified role beats a role
stated in a headline.

---

## Degradation order

The pool is built in this order, and the report says which paths contributed:

1. **Wikidata** — officer relationships, stable identifiers, no inference
2. *(LinkedIn / Crunchbase, when configured)*
3. **News coverage** — free, keyless, always available; role and company come
   from a headline and are marked as unverified
4. **Occupation fallback** — Wikidata, weaker, already labelled as such in the UI

An empty suggestion list is never returned silently: `network/pipeline.py`
records why, and the UI renders that reason instead of an empty table.
