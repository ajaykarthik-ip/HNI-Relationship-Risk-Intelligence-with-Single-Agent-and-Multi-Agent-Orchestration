"""Confidence should follow the evidence, not the article count.

`risk_score._confidence` awards points for retrieval volume: 200+ articles is
+2, identity confirmed is +2, ten adverse checks is +1, a fifth of items from
major publishers is +1. That is six points -- HIGH -- before anything has looked
at whether the *findings* are well sourced. A run that retrieved a great deal
and understood very little scores the same as one that did both.

The penalties that exist are too weak to correct it: a single-source principal
finding costs one point, and only when the *best* material finding is weak.

Rather than replace V1's rubric -- which is shared, frozen, and correct about
the risk *level* -- this applies a cap afterwards. Four conditions, each about
the quality of what was actually established rather than how much was fetched:

    single-sourced      most material findings rest on one publisher
    keyword-derived     most attributable findings were never read by a model
    headline-only       almost nothing was read past the headline
    unverified links    most company rows could not be tied to a registry

Each costs one level. The cap can only ever lower confidence -- it is bounded
by the value V1 computed -- so the deterministic rubric remains the ceiling and
this is a brake on it, never an accelerator. Every trigger appends its reason to
`confidence_basis`, because a number that cannot say why it dropped is one
nobody will act on.
"""

from __future__ import annotations

ORDER = ("LOW", "MEDIUM", "HIGH")

# Share of material findings resting on a single publisher before it counts.
SINGLE_SOURCE_SHARE = 0.5

# Share of attributable findings that were keyword-derived, not model-read.
KEYWORD_SHARE = 0.5

# Below this fraction of retained articles read in full, the assessment is
# substantially a judgement about headlines.
FULLTEXT_SHARE = 0.05

# Share of company rows that could not be matched to a registry entity.
UNVERIFIED_SHARE = 0.5


def _rank(level: str | None) -> int:
    try:
        return ORDER.index((level or "LOW").upper())
    except ValueError:
        return 0


def _material(findings: list) -> list:
    return [f for f in findings if f.get("is_material")]


def _attributable(findings: list) -> list:
    return [f for f in findings if f.get("attributed_to_subject")]


def assess(report: dict) -> tuple:
    """(levels_to_drop, reasons). Pure: reads the report, writes nothing."""
    findings = report.get("findings") or []
    coverage = report.get("coverage") or {}
    screening = report.get("screening") or []

    drops = 0
    reasons: list = []

    material = _material(findings)
    if material:
        thin = [
            f for f in material
            if len(f.get("corroborating_publishers") or []) < 2
        ]
        if len(thin) / len(material) >= SINGLE_SOURCE_SHARE:
            drops += 1
            reasons.append(
                f"{len(thin)} of {len(material)} material finding(s) rest on a "
                "single publisher"
            )

    attributable = _attributable(findings)
    if attributable:
        keyword = [
            f for f in attributable if f.get("derived_from") == "keyword"
        ]
        if len(keyword) / len(attributable) >= KEYWORD_SHARE:
            drops += 1
            reasons.append(
                f"{len(keyword)} of {len(attributable)} attributable finding(s) "
                "come from keyword matching rather than a reading of the text"
            )

    retained = coverage.get("after_dedup", 0)
    read_in_full = coverage.get("fulltext_fetched", 0)
    if retained and (read_in_full / retained) < FULLTEXT_SHARE:
        drops += 1
        reasons.append(
            f"only {read_in_full} of {retained} retained article(s) were read "
            "in full, so most stages rest on headline text"
        )

    if screening:
        unverified = [
            row for row in screening
            if not row.get("registry_id")
        ]
        if len(unverified) / len(screening) >= UNVERIFIED_SHARE:
            drops += 1
            reasons.append(
                f"{len(unverified)} of {len(screening)} company row(s) could "
                "not be matched to a registry entity"
            )

    return drops, reasons


def cap(report: dict) -> dict:
    """Apply the cap in place and return the report.

    Never raises a level. If V1 said MEDIUM, this can only produce MEDIUM or
    LOW, whatever the evidence looks like -- the deterministic rubric stays the
    ceiling.
    """
    assessment = report.get("assessment")
    if not isinstance(assessment, dict):
        return report

    current = assessment.get("confidence")
    drops, reasons = assess(report)
    if not drops:
        return report

    capped = ORDER[max(0, _rank(current) - drops)]
    if _rank(capped) >= _rank(current):
        return report

    assessment["confidence"] = capped
    basis = list(assessment.get("confidence_basis") or [])
    basis.append(
        f"Confidence reduced from {current} to {capped} on evidence quality:"
    )
    basis.extend(f"— {reason}" for reason in reasons)
    assessment["confidence_basis"] = basis

    # The recommended action is stated in terms of confidence, so it has to
    # move with it rather than continue asserting the old pairing.
    assessment["confidence_capped_from"] = current
    return report
