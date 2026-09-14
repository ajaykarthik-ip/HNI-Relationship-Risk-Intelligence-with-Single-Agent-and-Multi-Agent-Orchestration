"""One real-world event is one finding.

V1 clusters on `(category, calendar year)`. That key is wrong in three generic
ways, and all three showed up on the first live runs:

  year boundaries   an ongoing matter reported in December and again in January
                    becomes two findings. Nothing happened between them except
                    New Year.

  category drift    the extraction names an event type in its own words, and
                    `normalise_category` maps anything unrecognised to "other".
                    One settlement described once as a sanctions matter and once
                    as "other" becomes two findings of the same event.

  scope blindness   the person-level pass and the company-level pass never see
                    each other, so a matter naming both is reported twice.

So the key here is the *event*, not the bucket. Two articles describe the same
event when they are close in time and describe the same thing -- measured by
content-word overlap on the model's own summaries, which is deterministic, free,
and needs no key. Category becomes an attribute of the event rather than part of
its identity, and a second pass merges across entity scopes.

The windows are deliberately generous in the merge direction and conservative in
the split direction. Over-merging under-counts, under-merging over-counts, and
for a risk tool inflating the number of distinct adverse events is the worse
error -- it is what turns one matter into a pattern.
"""

from __future__ import annotations

from affluense.enrich import publishers
from affluense.enrich.cluster import (CLOSED_STAGES, OPEN_STAGES, _category,
                                      _evidence_item, _event_date,
                                      _fallback_severity, _resolve_contradiction,
                                      _slug)
from affluense.enrich import risk
from affluense.models import Finding
from affluense.resolve import relationships

from . import attribution, staging
from .text import containment, days_apart, iso, similarity

# Same category, this close together: the same matter being followed.
SAME_CATEGORY_DAYS = 45

# Beyond that window, same-category reports still merge -- but only when the
# text agrees they are about the same thing. A long-running enforcement matter
# is reported for months, while two tax demands for different amounts at the
# same company months apart are genuinely different events. Distance alone
# cannot tell them apart; the wording can.
SAME_CATEGORY_EXTENDED_DAYS = 200
EXTENDED_JACCARD = 0.35

# Different categories need a stronger textual match, and get a wider window,
# because a settlement and the charge it settles are months apart and get
# described in different words.
CROSS_CATEGORY_DAYS = 180

# Content-word overlap thresholds. Jaccard punishes length differences, so a
# short summary and a detailed one about one event are also allowed through on
# containment.
SIMILAR_JACCARD = 0.45
SIMILAR_CONTAINMENT = 0.70

# Merging two entity scopes is a stronger claim than merging within one, so it
# needs either a shared source document or a clear textual match.
CROSS_ENTITY_JACCARD = 0.50
CROSS_ENTITY_DAYS = 120


def _summary(article) -> str | None:
    extracted = getattr(article, "extracted", None) or {}
    return extracted.get("event_summary") or article.headline


def _sort_key(article) -> tuple:
    """Deterministic ordering, so clusters form identically on every run."""
    return (str(_event_date(article) or ""), getattr(article, "url", "") or "")


def _same_event(cluster: list, article) -> bool:
    """Whether this article describes an event the cluster already holds."""
    category = _category(article)
    summary = _summary(article)
    date = _event_date(article)

    for member in cluster:
        gap = days_apart(date, _event_date(member))
        member_summary = _summary(member)
        same_category = _category(member) == category

        # Two reports of the same kind of matter, days apart at the same
        # entity. Splitting these is what produced duplicate findings either
        # side of a year boundary.
        if same_category and (gap is None or gap <= SAME_CATEGORY_DAYS):
            return True

        # Further apart, and of the same kind: one ongoing matter followed over
        # months, or two separate ones. The summaries decide.
        if (same_category and gap is not None
                and gap <= SAME_CATEGORY_EXTENDED_DAYS):
            if (similarity(summary, member_summary) >= EXTENDED_JACCARD
                    or containment(summary, member_summary) >= SIMILAR_CONTAINMENT):
                return True

        # Different words for the same event. Needs a real textual match.
        if gap is not None and gap <= CROSS_CATEGORY_DAYS:
            if (similarity(summary, member_summary) >= SIMILAR_JACCARD
                    or containment(summary, member_summary) >= SIMILAR_CONTAINMENT):
                return True

    return False


def group_events(articles: list) -> list:
    """Articles grouped by the event they describe, in deterministic order."""
    clusters: list = []
    for article in sorted(articles, key=_sort_key):
        for cluster in clusters:
            if _same_event(cluster, article):
                cluster.append(article)
                break
        else:
            clusters.append([article])
    return clusters


def _eligible(articles: list) -> list:
    """The articles that may form findings at all. V1's gate, unchanged.

    A bare keyword with no progression and no negative tone is a mention, not
    an event, and letting those through is what produces alarm fatigue.
    """
    keep = []
    for article in articles:
        if not _category(article):
            continue
        if not getattr(article, "extracted", None) and not risk.is_flagworthy(article):
            continue
        keep.append(article)
    return keep


def _dominant_category(group: list) -> str:
    """The category most of the evidence agrees on.

    Ties break towards the more severe category, and "other" only wins when
    nothing else was offered -- a real category is always more informative than
    the fallback bucket.
    """
    counts: dict = {}
    for article in group:
        category = _category(article)
        if category:
            counts[category] = counts.get(category, 0) + 1
    if not counts:
        return "other"
    named = {c: n for c, n in counts.items() if c != "other"}
    pool = named or counts
    return max(sorted(pool), key=lambda c: (pool[c], _fallback_severity(c, "alleged")))


async def build_findings(bridge, articles: list, entity: str, entity_role: str,
                         relationship_type: str, subject_name: str | None = None,
                         say=None) -> list:
    """V2's replacement for `cluster.build_findings`. Same return contract."""
    carries_exposure = (
        relationships.carries_exposure(relationship_type)
        or relationship_type == "unknown"
        # A matter that names the individual is theirs wherever it happened.
        or relationship_type == "person"
    )
    # The company supplied with the query is added to the screening set by
    # definition, but when nothing corroborates the link the user may simply
    # have paired the wrong two names. Screening it is still right -- saying
    # nothing would hide the mismatch -- but its matters must not become the
    # subject's, or a wrong input silently produces adverse findings about
    # someone with no connection to any of it.
    if relationship_type == "uncorroborated_seed":
        carries_exposure = False

    findings = []
    for group in group_events(_eligible(articles)):
        category = _dominant_category(group)
        evidence = [_evidence_item(a) for a in group]
        publisher_names = publishers.independent_publishers(group)

        # Stage, corroborated rather than maximised.
        stage, stage_notes = staging.resolve(group)
        for note in stage_notes:
            if say:
                say(f"    {entity}: {note}")

        severities = [(a.extracted or {}).get("severity", 0) for a in group]
        model_severity = max(severities) if any(severities) else 0
        severity = model_severity or _fallback_severity(category, stage)

        confidences = [(a.extracted or {}).get("confidence", 0) for a in group]
        confidence = max(confidences) if any(confidences) else 0.4

        summary = next(
            ((a.extracted or {}).get("event_summary") for a in group
             if (a.extracted or {}).get("event_summary")),
            None,
        ) or f"{category.replace('_', ' ').title()} matter reported at {entity}"

        attributed, reason = attribution.decide(
            group, carries_exposure, subject_name,
        )
        if say and not attributed:
            say(f"    {entity}: not attributed to the subject — {reason}")

        within_tenure = not all(
            getattr(a, "outside_tenure", False) for a in group
        )

        finding = Finding(
            event_id=f"{_slug(entity)}-{category}-{_stamp(group)}",
            what_happened=summary,
            entity=entity,
            entity_role=entity_role,
            category=category,
            stage=stage,
            # Normalised here, so a raw RSS pubDate never reaches the report
            # beside ISO dates from the extraction.
            event_date=iso(_event_date(group[0])),
            is_ongoing=stage in OPEN_STAGES,
            severity=severity,
            confidence=round(confidence, 2),
            attributed_to_subject=attributed,
            within_tenure=within_tenure,
            corroborating_publishers=publisher_names,
            evidence=evidence,
            found_by=next((a.query_group for a in group if a.query_group), None),
            derived_from=(
                "extraction" if any(a.extracted for a in group) else "keyword"
            ),
        )

        stages_seen = {staging.stage_of(a) for a in group}
        if stages_seen & set(OPEN_STAGES) and stages_seen & set(CLOSED_STAGES):
            # A reading problem, not a counting one: V1's model call decides
            # which account is current.
            await bridge.run(_resolve_contradiction, finding, group)
            if say:
                say(f"    conflicting accounts of {category} at {entity}; "
                    f"resolved to {finding.stage}")

        findings.append(finding)

    findings = _consolidate(findings)
    findings.sort(key=lambda f: (-f.severity, -len(f.corroborating_publishers)))
    return _dedupe_ids(findings)


def _stamp(group: list) -> str:
    """A stable date fragment for the event id."""
    from .text import parse_date

    for article in group:
        parsed = parse_date(_event_date(article))
        if parsed is not None:
            return parsed.strftime("%Y%m")
    return "undated"


def _consolidate(findings: list) -> list:
    """Fold findings that share an entity, a category and a month.

    `group_events` can still split one matter in two: `_stamp` takes the first
    dated article in a group, so two groups spanning different ranges both
    stamp the same month while the specific articles compared sit outside the
    similarity window. The old code noticed this and appended "-2" to the id --
    which is the identity check admitting the two are the same event and then
    reporting them separately anyway.

    Same entity, same category, same month is one matter. Anything genuinely
    distinct at that resolution is rare, and under-counting is the safe
    direction for a risk tool.
    """
    merged: dict = {}
    order: list = []
    for finding in findings:
        existing = merged.get(finding.event_id)
        if existing is None:
            merged[finding.event_id] = finding
            order.append(finding.event_id)
            continue
        _merge(existing, finding)
    return [merged[event_id] for event_id in order]


def _dedupe_ids(findings: list) -> list:
    """Last resort: ids must be unique even after consolidation."""
    seen: dict = {}
    for finding in findings:
        base = finding.event_id
        count = seen.get(base, 0)
        seen[base] = count + 1
        if count:
            finding.event_id = f"{base}-{count + 1}"
    return findings


# ---------------------------------------------------------------------------
# Cross-entity merge
# ---------------------------------------------------------------------------

def _evidence_urls(finding) -> set:
    return {
        getattr(item, "url", None) for item in (finding.evidence or [])
        if getattr(item, "url", None)
    }


def _same_underlying_event(left, right) -> bool:
    """Whether two findings from different scopes describe one event."""
    # A shared source document is conclusive: one article cannot be two events.
    if _evidence_urls(left) & _evidence_urls(right):
        return True

    gap = days_apart(left.event_date, right.event_date)
    if gap is not None and gap > CROSS_ENTITY_DAYS:
        return False

    return (
        similarity(left.what_happened, right.what_happened) >= CROSS_ENTITY_JACCARD
        or containment(left.what_happened, right.what_happened) >= SIMILAR_CONTAINMENT
    )


def _merge(base, other):
    """Fold `other` into `base`, keeping the stronger evidence on each axis."""
    urls = _evidence_urls(base)
    base.evidence = list(base.evidence) + [
        item for item in (other.evidence or [])
        if getattr(item, "url", None) not in urls
    ]
    base.corroborating_publishers = sorted(
        set(base.corroborating_publishers) | set(other.corroborating_publishers)
    )
    base.severity = max(base.severity, other.severity)
    base.confidence = max(base.confidence, other.confidence)
    # Attribution is a union: if either scope established the matter is the
    # subject's, it is.
    base.attributed_to_subject = (
        base.attributed_to_subject or other.attributed_to_subject
    )
    base.within_tenure = base.within_tenure or other.within_tenure
    base.contradicting_sources = list(
        dict.fromkeys(
            list(base.contradicting_sources) + list(other.contradicting_sources)
        )
    )
    if other.category != "other" and base.category == "other":
        base.category = other.category
    if not base.found_by:
        base.found_by = other.found_by

    # A more advanced stage survives the merge only if the combined evidence
    # clears the same corroboration bar a single scope would have had to.
    candidate = max(
        (base.stage, other.stage), key=lambda s: staging.severity_of(s),
    )
    required = staging.CORROBORATION.get(candidate, staging.DEFAULT_CORROBORATION)
    if len(base.corroborating_publishers) >= required:
        base.stage = candidate
    base.is_ongoing = base.stage in OPEN_STAGES

    # Prefer the fuller description of what happened.
    if len(other.what_happened or "") > len(base.what_happened or ""):
        base.what_happened = other.what_happened
    return base


def merge_across_entities(findings: list, say=None) -> list:
    """Collapse findings from different scopes that describe one event.

    Runs once, after every entity has produced its findings. Ordering is by
    evidence strength so the survivor is the better-sourced record, and the
    scan is deterministic.
    """
    ordered = sorted(
        findings,
        key=lambda f: (
            -len(f.corroborating_publishers), -f.severity, f.event_id,
        ),
    )
    kept: list = []
    merged_count = 0

    for finding in ordered:
        for existing in kept:
            if _same_underlying_event(existing, finding):
                _merge(existing, finding)
                merged_count += 1
                break
        else:
            kept.append(finding)

    if say and merged_count:
        say(f"    merged {merged_count} duplicate finding(s) describing the "
            "same event across entities")

    kept.sort(key=lambda f: (-f.severity, -len(f.corroborating_publishers)))
    return kept
