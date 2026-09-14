"""Affluense — screening and network intelligence for high-net-worth individuals.

Layers, in the order the pipeline uses them:

    sources/   fetch raw material from one external service each
    resolve/   turn names into entities, and collapse duplicate entities
    enrich/    score what was fetched (sentiment, adverse-media risk)
    pipeline   orchestrate the above for one subject
    output     write the delivered JSON and CSV
"""

__version__ = "2.0.0"
