"""The verdict. Arithmetic, not opinion.

A model contributed severity and stage to each finding. The rollup from
findings to LOW / MEDIUM / HIGH / CRITICAL happens here, in Python, because a
compliance decision has to be three things a model cannot promise:

  reproducible   the same findings must always produce the same level, or the
                 tool cannot be relied on twice
  explainable    "why is this HIGH" must have an answer made of the evidence,
                 not a paraphrase of one
  testable       thresholds in one module with tests around them, so tuning is
                 a reviewable change rather than a prompt edit

Risk and confidence are computed separately and never collapsed. HIGH at high
confidence and HIGH at low confidence call for different actions from whoever
reads it, and a single number hides that.

The asymmetry throughout is deliberate. A false positive gets checked and
dismissed; a false negative gets trusted. Where a rule could go either way,
it goes toward showing the reviewer something.
"""

from __future__ import annotations

from datetime import datetime, timezone

LOW, MEDIUM, HIGH, CRITICAL = "LOW", "MEDIUM", "HIGH", "CRITICAL"
ORDER = {LOW: 0, MEDIUM: 1, HIGH: 2, CRITICAL: 3}

PROCEED, REVIEW, ESCALATE = "proceed", "review", "escalate"

# How far a matter has gone, as a multiplier on its severity. An allegation
# and a conviction share a category and are not the same fact.
STAGE_WEIGHT = {
    "dismissed": 0.15,
    "reported": 0.5,
    "alleged": 0.7,
    "settled": 0.7,
    "investigating": 0.9,
    "charged": 1.15,
    "convicted": 1.3,
}

# Categories that reach CRITICAL once proven. A settled contract dispute never
# should, however large.
GRAVE_CATEGORIES = ("fraud", "corruption", "arrest", "sanctions")

# Older than this and a resolved matter stops driving the headline.
STALE_YEARS = 7


def _year(value: str | None) -> int | None:
    if not value:
        return None
    digits = "".join(c for c in str(value) if c.isdigit())
    for start in range(max(1, len(digits) - 3)):
        chunk = digits[start:start + 4]
        if chunk.startswith(("19", "20")):
            return int(chunk)
    return None


def _age_factor(finding) -> float:
    """Recent matters weigh more. Ongoing ones never decay."""
    if finding.is_ongoing:
        return 1.0
    year = _year(finding.event_date)
    if year is None:
        return 0.85  # undated: discounted, never dismissed
    age = datetime.now(timezone.utc).year - year
    if age <= 2:
        return 1.0
    if age <= STALE_YEARS:
        return 0.8
    return 0.55


def _corroboration_factor(finding) -> float:
    """One publisher can be wrong. Three agreeing rarely are."""
    count = len(finding.corroborating_publishers)
    if count >= 3:
        return 1.0
    if count == 2:
        return 0.9
    if count == 1:
        return 0.75
    return 0.5


def _source_factor(finding) -> float:
    """A wire report is stronger evidence than an unknown aggregator."""
    tiers = [e.tier for e in finding.evidence] or [3]
    best = min(tiers)
    return {0: 0.3, 1: 1.0, 2: 0.9, 3: 0.65}.get(best, 0.65)


def score_finding(finding) -> float:
    """A finding's contribution, 0-5.

    Severity is the base; stage, age, corroboration and source quality can
    only reduce it. Nothing here can inflate a weak finding into a strong one.
    """
    if not finding.attributed_to_subject or not finding.within_tenure:
        return 0.0
    base = float(finding.severity)
    return (
        base
        * STAGE_WEIGHT.get(finding.stage, 0.5)
        * _age_factor(finding)
        * _corroboration_factor(finding)
        * _source_factor(finding)
    )


def _level_for(score: float, finding) -> str:
    """The level one finding alone justifies."""
    grave = finding.category in GRAVE_CATEGORIES
    proven = finding.stage in ("charged", "convicted")

    if grave and proven and score >= 3.4:
        return CRITICAL
    if score >= 3.0:
        return HIGH
    if score >= 1.6:
        return MEDIUM
    if score > 0:
        return LOW
    return LOW


def assess(findings: list, coverage: dict, identity_confirmed: bool,
           validation: dict | None = None) -> dict:
    """The overall assessment. Pure: same inputs, same output, no I/O."""
    material = [f for f in findings if f.is_material]
    scored = sorted(
        ((score_finding(f), f) for f in material), key=lambda pair: -pair[0]
    )

    level = LOW
    for score, finding in scored:
        candidate = _level_for(score, finding)
        if ORDER[candidate] > ORDER[level]:
            level = candidate

    # Several independent material matters are worse than the worst of them
    # alone: a pattern is itself a finding.
    strong = [f for _s, f in scored if _s >= 1.6]
    if len(strong) >= 3 and ORDER[level] < ORDER[HIGH]:
        level = HIGH
    elif len(strong) >= 2 and ORDER[level] < ORDER[MEDIUM]:
        level = MEDIUM

    confidence, confidence_basis = _confidence(
        findings, coverage, identity_confirmed, validation
    )
    reasons = _reasons(scored, findings)
    limitations = _limitations(coverage, validation, findings)

    return {
        "risk_level": level,
        "confidence": confidence,
        "headline": _headline(level, scored, coverage),
        "reasons": reasons,
        "confidence_basis": confidence_basis,
        "limitations": limitations,
        "recommended_action": _action(level, confidence),
        "material_findings": len(material),
        "findings_considered": len(findings),
        "method": (
            "Deterministic rubric over the findings. Severity and stage come "
            "from the evidence; the rollup is arithmetic, so the same findings "
            "always produce the same level."
        ),
    }


def _headline(level: str, scored: list, coverage: dict) -> str:
    if level == LOW:
        checked = len(coverage.get("groups_checked") or [])
        return (
            "No material adverse findings identified in the available public "
            f"evidence ({checked} adverse checks run)."
            if checked else
            "No material adverse findings identified in the available public "
            "evidence."
        )
    if not scored:
        return "Adverse coverage found; see findings."
    top = scored[0][1]
    state = "ongoing" if top.is_ongoing else top.stage
    return (
        f"{top.category.replace('_', ' ').title()} matter at {top.entity} "
        f"({state}), attributable to the subject."
    )


def _reasons(scored: list, findings: list) -> list:
    """Why the level is what it is, in the evidence's own terms."""
    reasons = []
    for score, finding in scored[:5]:
        reasons.append({
            "factor": finding.category,
            "detail": (
                f"{finding.what_happened} — {finding.stage}"
                + (", ongoing" if finding.is_ongoing else "")
                + f", {len(finding.corroborating_publishers)} independent "
                f"publisher(s)"
            ),
            "weight": round(score, 2),
            "finding_ids": [finding.event_id],
        })

    if not reasons:
        excluded = [f for f in findings if not f.is_material]
        if excluded:
            reasons.append({
                "factor": "no attributable findings",
                "detail": (
                    f"{len(excluded)} adverse item(s) were found but none is "
                    "attributable to the subject within their tenure, or none "
                    "is corroborated by an independent publisher."
                ),
                "weight": 0.0,
                "finding_ids": [f.event_id for f in excluded[:5]],
            })
    return reasons


def _confidence(findings, coverage, identity_confirmed, validation) -> tuple:
    """How much the assessment can be relied on. Separate from the level."""
    basis = []
    points = 0

    if identity_confirmed:
        points += 2
        basis.append("Identity confirmed by the user before research began")
    else:
        basis.append("Identity was inferred, not confirmed — rows are coverage "
                     "matching a name")

    evidence = coverage.get("after_dedup", 0)
    if evidence >= 200:
        points += 2
        basis.append(f"{evidence} evidence items from "
                     f"{coverage.get('publishers', 0)} distinct publishers")
    elif evidence >= 60:
        points += 1
        basis.append(f"{evidence} evidence items — moderate coverage")
    else:
        basis.append(f"Only {evidence} evidence items — coverage is thin")

    checks = len(coverage.get("groups_checked") or [])
    if checks >= 10:
        points += 1
        basis.append(f"{checks} adverse checks run across every company")

    tier1 = (coverage.get("by_tier") or {}).get("tier 1", 0)
    if evidence and tier1 / max(evidence, 1) >= 0.2:
        points += 1
        basis.append(f"{tier1} items from major publishers")

    material = [f for f in findings if f.is_material]
    if material:
        best = max(len(f.corroborating_publishers) for f in material)
        if best >= 3:
            points += 1
            basis.append(f"Principal finding corroborated by {best} publishers")
        elif best <= 1:
            points -= 1
            basis.append("Principal finding rests on a single publisher")

    contradicted = [f for f in findings if f.contradicting_sources]
    if contradicted:
        points -= 1
        basis.append(f"{len(contradicted)} finding(s) had conflicting accounts")

    statuses = (validation or {}).get("companies_by_status") or {}
    unverified = statuses.get("unverified", 0)
    if unverified:
        points -= 1
        basis.append(f"{unverified} company row(s) rest on unverified links")

    level = "HIGH" if points >= 5 else "MEDIUM" if points >= 2 else "LOW"
    return level, basis


def _limitations(coverage, validation, findings) -> list:
    """Everything a reader should know before acting on this."""
    limits = []

    paywalled = coverage.get("fulltext_paywalled", 0)
    if paywalled:
        limits.append(
            f"{paywalled} article(s) were paywalled; their findings rest on "
            "headline evidence only."
        )

    statuses = (validation or {}).get("companies_by_status") or {}
    if statuses.get("insufficient_coverage"):
        limits.append(
            f"{statuses['insufficient_coverage']} company had no coverage at "
            "all, so nothing can be said about it either way."
        )
    if statuses.get("unverified"):
        limits.append(
            f"{statuses['unverified']} company could not be matched to a "
            "registry entity."
        )

    out_of_tenure = [f for f in findings if not f.within_tenure]
    if out_of_tenure:
        limits.append(
            f"{len(out_of_tenure)} adverse item(s) fall outside the subject's "
            "time at the company and are reported but not attributed."
        )

    uncorroborated = [
        f for f in findings
        if f.attributed_to_subject and len(f.corroborating_publishers) < 2
    ]
    if uncorroborated:
        limits.append(
            f"{len(uncorroborated)} attributable item(s) have only one "
            "independent source."
        )

    limits.append(
        "Absence of adverse evidence is not proof that none exists. This "
        "covers indexed public sources only; court and registry filings not "
        "published online are out of scope."
    )
    return limits


def _action(level: str, confidence: str) -> str:
    if level == CRITICAL:
        return ESCALATE
    if level == HIGH:
        return REVIEW
    if level == MEDIUM:
        return REVIEW if confidence != "LOW" else ESCALATE
    # A clean result on thin evidence is not a clean result.
    return PROCEED if confidence != "LOW" else REVIEW
