"""One last read of the finished report.

The assessment is assembled by rules, which means it is consistent and also
that it cannot notice when it is consistently wrong. A second reader helps --
but only as a reader.

This module raises issues. It never edits the report. That restriction is the
whole design: a model that can rewrite a finding can also launder an error
into confident prose, and every number in the output has to stay traceable to
the deterministic layer that produced it. The worst outcome here is a useless
note. The worst outcome from a rewriting reviewer is an invented fact wearing
a real source URL.

Without an API key the deterministic checks below still run, because most of
what goes wrong is countable rather than subtle.
"""

from __future__ import annotations

import json

from . import config
from .sources import openai_client

SYSTEM = (
    "You are reviewing a completed due-diligence assessment before it reaches "
    "a reader. You are given the risk level, the reasons, and the findings "
    "with their evidence.\n\n"
    "Raise an issue when, and only when:\n"
    "- a stated reason is not supported by the evidence cited for it\n"
    "- two findings describe the same event and should have been merged\n"
    "- a finding's stage overstates what its evidence shows\n"
    "- the confidence stated is inconsistent with the coverage described\n"
    "- a material caveat is missing\n\n"
    "Do not suggest rewording. Do not propose new findings. If the assessment "
    "is sound, return an empty list.\n"
    'Reply as JSON: {"issues": [{"severity": "low|medium|high", '
    '"about": str, "issue": str}]}'
)


def _deterministic_issues(report: dict) -> list:
    """Checks that need no model, and no network."""
    issues = []
    assessment = report.get("assessment") or {}
    findings = report.get("findings") or []

    level = assessment.get("risk_level")
    if level and level != "LOW" and not assessment.get("reasons"):
        issues.append({
            "severity": "high", "about": "assessment",
            "issue": f"Risk level is {level} but no reason was recorded.",
        })

    for finding in findings:
        if not finding.get("evidence"):
            issues.append({
                "severity": "high", "about": finding.get("event_id"),
                "issue": "Finding has no evidence behind it.",
            })
        if finding.get("stage") in ("charged", "convicted") and \
                len(finding.get("corroborating_publishers") or []) < 2:
            issues.append({
                "severity": "medium", "about": finding.get("event_id"),
                "issue": (
                    "A finding at charged or convicted stage rests on a single "
                    "publisher. Stages this advanced deserve corroboration."
                ),
            })

    coverage = report.get("coverage") or {}
    if assessment.get("risk_level") == "LOW" and coverage.get("after_dedup", 0) < 40:
        issues.append({
            "severity": "medium", "about": "assessment",
            "issue": (
                "A LOW verdict on fewer than 40 evidence items reflects thin "
                "coverage more than a clean record."
            ),
        })

    if assessment.get("confidence") == "HIGH" and coverage.get("fulltext_paywalled", 0) > 5:
        issues.append({
            "severity": "low", "about": "confidence",
            "issue": "Confidence is HIGH despite several paywalled sources.",
        })

    return issues


def _compact(report: dict) -> dict:
    """What the reviewer sees. Trimmed to keep one call sufficient."""
    assessment = report.get("assessment") or {}
    return {
        "risk_level": assessment.get("risk_level"),
        "confidence": assessment.get("confidence"),
        "reasons": assessment.get("reasons"),
        "limitations": assessment.get("limitations"),
        "coverage": report.get("coverage"),
        "findings": [
            {
                "event_id": f.get("event_id"),
                "what_happened": f.get("what_happened"),
                "entity": f.get("entity"),
                "category": f.get("category"),
                "stage": f.get("stage"),
                "severity": f.get("severity"),
                "attributed_to_subject": f.get("attributed_to_subject"),
                "within_tenure": f.get("within_tenure"),
                "publishers": f.get("corroborating_publishers"),
                "evidence": [
                    {"publisher": e.get("publisher"), "quote": e.get("quote"),
                     "published": e.get("published")}
                    for e in (f.get("evidence") or [])[:4]
                ],
            }
            for f in (report.get("findings") or [])[:12]
        ],
    }


def review(fetcher, report: dict) -> dict:
    """Return {issues, reviewed_by}. Never mutates the report's data."""
    issues = _deterministic_issues(report)
    reviewed_by = "deterministic checks"

    client = openai_client.OpenAIClient(
        fetcher, budget=config.OPENAI_MAX_QC_CALLS, purpose="final review"
    )
    if client.available:
        answer = client.complete_json(
            SYSTEM, json.dumps(_compact(report), ensure_ascii=False),
            max_tokens=900,
        )
        if answer and isinstance(answer.get("issues"), list):
            reviewed_by = f"deterministic checks + {config.OPENAI_MODEL}"
            for row in answer["issues"]:
                if not isinstance(row, dict) or not row.get("issue"):
                    continue
                issues.append({
                    "severity": (row.get("severity") or "low").lower(),
                    "about": str(row.get("about") or "assessment"),
                    "issue": str(row["issue"])[:400],
                })

    order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda i: order.get(i.get("severity", "low"), 2))
    return {
        "issues": issues,
        "reviewed_by": reviewed_by,
        "note": (
            "The reviewer raises issues only. It cannot change a finding, a "
            "level or a number — every figure in this report comes from the "
            "deterministic layer."
        ),
    }
