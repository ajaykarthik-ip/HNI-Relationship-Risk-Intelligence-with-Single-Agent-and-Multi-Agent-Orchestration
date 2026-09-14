# Archive

Superseded work, kept for reference. Nothing here is imported or run by the
current system.

## `hni_lookup.py`

The first implementation: a single 1,445-line script that collected public
information about one named person. It was replaced by the `affluense/`
package, which splits the same work across 24 modules and adds what this
script never had — a company as a second input, sentiment analysis,
per-company screening, relevance scoring, caching, and tests.

Useful only as a record of the starting point.

## JSON outputs

Runs produced by that script, in its own output format (a flat `companies` /
`news` shape, not the `screening` rows the current system emits):

| File | Notes |
|---|---|
| `ratan-tata.json` | Free sources only |
| `virat-kohli-v2.json` | The first Firecrawl run — 10 companies, 16 investments |
| `virat-kholi.json` | A misspelled query. Wikidata's search does no fuzzy matching, so it returned nothing and the report came back near-empty. This file is the evidence behind the name-resolution fix in `resolve/person.py`. |
