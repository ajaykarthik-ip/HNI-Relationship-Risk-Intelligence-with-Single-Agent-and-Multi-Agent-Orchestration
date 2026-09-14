"""V2's additions to Problem Statement 2.

V1's network pipeline is architecturally complete -- it identifies the current
network, generates a global candidate pool, scores relevance on role, industry,
proximity, geography and prominence, and writes JSON and CSV. What it has is a
single point of failure: every one of those steps reaches Wikidata, and when the
Query Service refuses the request the entire deliverable is an empty list.

That is exactly what the first live runs showed. `query.wikidata.org` returned
403 on every run, so `candidates_by_industry` and `candidates_by_occupation`
both produced nothing, and PS2 would have returned no suggestions at all.

So this package adds a second, independent path to the same output shape:

    peers.py    candidate discovery from news coverage, which is free, keyless,
                already fetched concurrently by V2, and does not depend on the
                subject or their peers existing in a structured registry.

Wikidata stays the primary source where it is reachable -- it is higher quality
and carries stable identifiers. This is a fallback, not a replacement, and the
report says which path produced the list.
"""
