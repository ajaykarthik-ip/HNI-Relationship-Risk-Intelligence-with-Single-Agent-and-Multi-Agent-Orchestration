"""V2's quality layer.

The four failure modes the first live runs exposed -- duplicated findings,
overstated legal stages, confidence that tracked retrieval volume rather than
evidence strength, and non-companies being screened as companies -- all
originate in V1 modules that are frozen. So none of them are edited. Instead
V2 supplies its own implementations and the orchestrator calls those.

That works because of where the seam already sits: V2 builds the `findings`
list itself and hands it to `output.build_report`. Stage, attribution,
corroboration and event identity are all fields on `Finding`, so they are
entirely under V2's control. Only confidence is computed inside V1, and that is
handled by a cap applied afterwards which can lower a level but never raise one.

    events.py       one real-world event is one finding
    staging.py      a stage has to be supported by what the source actually says
    attribution.py  a company's conduct is not automatically a person's
    entities.py     a sector is not a company
    confidence.py   confidence follows evidence quality, not article count

Nothing here knows any person's or company's name. Every rule is structural.
"""
