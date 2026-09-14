"""Async implementations of the four high-volume V1 sources.

Everything else -- Wikipedia, Wikidata, DuckDuckGo -- is reached through
`bridge.SyncFacade` and runs V1's own module unchanged. Only the paths that
dominate the runtime are rewritten here, and each one reuses V1's parsing and
payload construction so the two engines cannot disagree about what a source
returned.
"""
