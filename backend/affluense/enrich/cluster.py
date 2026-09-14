"""Ten articles about one notice are one finding, not ten.

Before this, a company's adverse coverage was counted by article. A single
SEBI notice picked up by ten outlets read as ten pieces of evidence, and the
syndicated wire copy behind them read as independent corroboration. Neither
is true, and both inflate a risk score.

Clustering is deterministic. Events group on (category, year), because two
adverse events of the same kind, at the same company, in the same year, are
almost always the same event -- and where they are not, splitting them would
under-count rather than over-count, which is the safe direction.

Corroboration is counted by *publisher*, never by article. That single choice
is what makes "3 independent sources" mean something.

A model is used in exactly one place: deciding what to do when two articles
in a cluster contradict each other -- one reporting charges, another a
dismissal. That is a reading problem, not a counting problem, and getting it
wrong in silence is how a tool reports a cleared matter as an open one.
"""

from __future__ import annotations

import json

from .. import config
from ..collect import fulltext
from ..models import EvidenceItem, Finding
from ..sources import openai_client
from . import publishers, risk

# Stages that mean the matter is over.
CLOSED_STAGES = ("dismissed", "settled", "convicted")

# Stages that mean it is live.
OPEN_STAGES = ("alleged", "investigating", "charged")


def _year_of(value: str | None) -> str:
    """The year an event happened, as a clustering key."""
    if not value:
        return "undated"
    digits = "".join(c for c in str(value) if c.isdigit())
    for start in range(len(digits) - 3):
        chunk = digits[start:start + 4]
        if chunk.startswith(("19", "20")):
            return chunk
    return "undated"


def _event_date(article) -> str | None:
    """The event's own date if the text gave one, else publication date."""
    extracted = article.extracted or {}
    return extracted.get("event_date") or article.published


def _category(article) -> str | None:
    extracted = article.extracted or {}
    if extracted.get("category"):
        return extracted["category"]
    categories = getattr(article, "risk_categories", None) or []
    return categories[0] if categories else None


def _stage(article) -> str:
    extracted = article.extracted or {}
    return extracted.get("stage") or getattr(article, "risk_stage", None) or "reported"


def _evidence_item(article) -> EvidenceItem:
    return EvidenceItem(
        url=article.url,
        publisher=getattr(article, "publisher_name", None)
        or publishers.publisher_name(article),
        tier=getattr(article, "publisher_tier", publishers.UNKNOWN),
        published=article.published,
        headline=article.headline,
        quote=fulltext.quote_for(article),
        fetch_status=getattr(article, "fetch_status", "headline_only"),
    )


def _resolve_contradiction(fetcher, finding: Finding, articles: list) -> None:
    """Decide which of two conflicting accounts of one matter stands.

    Only called when a cluster contains both an open stage and a closed one.
    The deterministic fallback -- take the latest-dated article -- is usually
    right and is what runs without a key.
    """
    # Sort on the date alone. Sorting the pairs compares the Article when two
    # share a publication date -- which syndicated copy about one matter
    # routinely does -- and Article has no ordering, so it raised
    # "'<' not supported between instances of 'Article'".
    dated = sorted(articles, key=lambda a: a.published or "")
    finding.stage = _stage(dated[-1])
    finding.contradicting_sources = [
        f"{_evidence_item(a).publisher}: {_stage(a)}" for a in dated
    ]

    client = openai_client.OpenAIClient(
        fetcher, budget=config.OPENAI_MAX_CLUSTER_CALLS,
        purpose="contradiction review",
    )
    if not client.available:
        return

    answer = client.complete_json(
        "Two or more articles describe the same matter but disagree about how "
        "far it has gone. Decide which account is current, using only the "
        "supplied text and dates. A later dismissal supersedes an earlier "
        "charge; a later charge supersedes an earlier allegation.\n"
        'Reply as JSON: {"current_stage": str, "is_ongoing": bool, '
        '"explanation": str}',
        json.dumps({
            "entity": finding.entity,
            "category": finding.category,
            "accounts": [
                {
                    "publisher": _evidence_item(a).publisher,
                    "published": a.published,
                    "stage": _stage(a),
                    "headline": a.headline,
                    "text": (a.body_text or a.snippet or "")[:1200],
                }
                for a in articles
            ],
        }, ensure_ascii=False),
        max_tokens=400,
    )
    if not answer:
        return

    stage = (answer.get("current_stage") or "").strip().lower()
    if stage in risk.STAGE_SEVERITY:
        finding.stage = stage
    finding.is_ongoing = bool(answer.get("is_ongoing", finding.is_ongoing))
    explanation = (answer.get("explanation") or "").strip()
    if explanation:
        finding.contradicting_sources.append(f"resolved: {explanation}")


def build_findings(fetcher, articles: list, entity: str, entity_role: str,
                   relationship_type: str, say=None) -> list:
    """Group one company's adverse articles into findings.

    `relationship_type` decides attribution. A former employer's matter is
    reported and never attributed; only a live controlling or executive
    relationship makes a company's conduct the subject's exposure.
    """
    from ..resolve import relationships
    # "unknown" is included deliberately. An unclassified relationship is not
    # evidence that the subject is unconnected, and a due-diligence tool fails
    # asymmetrically: a false positive gets checked, a false negative gets
    # trusted. Unknown attributes, and the validator labels the row.
    carries_exposure = (
        relationships.carries_exposure(relationship_type)
        or relationship_type == "unknown"
    )

    clusters: dict = {}
    for article in articles:
        category = _category(article)
        if not category:
            continue
        # A bare keyword with no progression and no negative tone is a
        # mention, not an event. Same gate the old flags used, kept because
        # it is what prevents alarm fatigue.
        if not article.extracted and not risk.is_flagworthy(article):
            continue

        key = (category, _year_of(_event_date(article)))
        clusters.setdefault(key, []).append(article)

    findings = []
    for (category, year), group in sorted(clusters.items()):
        evidence = [_evidence_item(a) for a in group]
        publisher_names = publishers.independent_publishers(group)

        stages = [_stage(a) for a in group]
        stage = max(stages, key=lambda s: risk.STAGE_SEVERITY.get(s, 1))

        severities = [
            (a.extracted or {}).get("severity", 0) for a in group
        ]
        model_severity = max(severities) if any(severities) else 0
        severity = model_severity or _fallback_severity(category, stage)

        confidences = [(a.extracted or {}).get("confidence", 0) for a in group]
        confidence = max(confidences) if any(confidences) else 0.4

        summary = next(
            ((a.extracted or {}).get("event_summary") for a in group
             if (a.extracted or {}).get("event_summary")),
            None,
        ) or f"{category.replace('_', ' ').title()} matter reported at {entity}"

        actor_is_subject = any(
            (a.extracted or {}).get("actor_is_subject") for a in group
        )
        within_tenure = not all(
            getattr(a, "outside_tenure", False) for a in group
        )

        finding = Finding(
            event_id=f"{_slug(entity)}-{category}-{year}",
            what_happened=summary,
            entity=entity,
            entity_role=entity_role,
            category=category,
            stage=stage,
            event_date=_event_date(group[0]),
            is_ongoing=stage in OPEN_STAGES,
            severity=severity,
            confidence=round(confidence, 2),
            # A matter naming the subject is theirs wherever it happened; a
            # company matter is theirs only where they controlled the company.
            attributed_to_subject=bool(actor_is_subject or carries_exposure),
            within_tenure=within_tenure,
            corroborating_publishers=publisher_names,
            evidence=evidence,
            found_by=next((a.query_group for a in group if a.query_group), None),
            derived_from="extraction" if any(a.extracted for a in group) else "keyword",
        )

        open_stages = {s for s in stages if s in OPEN_STAGES}
        closed_stages = {s for s in stages if s in CLOSED_STAGES}
        if open_stages and closed_stages:
            _resolve_contradiction(fetcher, finding, group)
            if say:
                say(f"    conflicting accounts of {category} at {entity}; "
                    f"resolved to {finding.stage}")

        findings.append(finding)

    findings.sort(key=lambda f: (-f.severity, -len(f.corroborating_publishers)))
    return findings


def _fallback_severity(category: str, stage: str) -> int:
    """Severity when no model read the article.

    Deliberately conservative. A keyword match with no comprehension behind it
    should not be able to produce a top-severity finding on its own.
    """
    base = {"fraud": 4, "arrest": 4, "corruption": 4, "sanctions": 4,
            "investigation": 3, "regulatory": 3, "insolvency": 3,
            "litigation": 2, "governance": 2}.get(category, 2)
    if stage in ("convicted", "charged"):
        return min(5, base + 1)
    if stage in ("dismissed", "settled"):
        return max(1, base - 2)
    if stage == "reported":
        return max(1, base - 1)
    return base


def _slug(text: str) -> str:
    cleaned = "".join(c.lower() if c.isalnum() else "-" for c in (text or ""))
    return "-".join(p for p in cleaned.split("-") if p)[:48] or "entity"
