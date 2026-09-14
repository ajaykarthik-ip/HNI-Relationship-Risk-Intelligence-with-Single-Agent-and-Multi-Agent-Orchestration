"""Writers for the delivered artefacts.

The problem statement asks for JSON or CSV. Both are produced: JSON carries
the full evidence chain, CSV carries the required columns in the shape a
reviewer can open in a spreadsheet.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict

from . import qc, risk_score, usage, validate
from .models import DISCLAIMER


def build_report(result: dict, query_name: str, query_company: str | None) -> dict:
    """Assemble the JSON document, then validate it. Every record keeps its
    source URL."""
    screenings = result["screenings"]
    # A past employer's or a university's adverse news is not the subject's
    # exposure, so it does not drive the headline number.
    flagged = [s for s in screenings if s.negative_news_flag]
    attributable = [s for s in flagged if s.relationship_type in ("control", "unknown")]

    report = {
        "query": {
            "individual": query_name,
            "company": query_company,
            "collected_at": result["collected_at"],
            "sentiment_backend": result["sentiment_backend"],
        },
        "cache": result.get("cache", {}),
        "usage": result.get("usage", {}),
        "subject": asdict(result["subject"]),
        "identity": {
            "biography": result["biography"],
            "wikidata_candidates": result["candidates"],
            "wikidata_claims": result["wikidata_claims"],
            "duckduckgo": result["duckduckgo"],
        },
        # --- Problem Statement 1 deliverable ------------------------------
        "screening": [
            {
                "company_name": s.name,
                "sentiment": s.classification,
                "negative_news_flag": s.negative_news_flag,
                "flag_categories": [f.category for f in s.flags],
                "flag_stages": [f.stage for f in s.flags],
                "sentiment_breakdown": {
                    "negative": s.sentiment.negative,
                    "neutral": s.sentiment.neutral,
                    "positive": s.sentiment.positive,
                },
                "articles_reviewed": s.articles_reviewed,
                "insufficient_coverage": s.insufficient_coverage,
                "relationship": s.relationships,
                "status": s.status,
                "relationship_type": s.relationship_type,
                "jurisdiction": s.jurisdiction,
                "registry_id": s.registry_id,
                "link_confidence": s.link_confidence,
                "link_basis": s.link_basis,
                "aliases": s.aliases,
                "sources": s.sources,
                "source_url": s.source_url,
                "flags": [asdict(f) for f in s.flags],
                "evidence": [asdict(a) for a in s.articles],
            }
            for s in screenings
        ],
        "person_coverage": {
            "classification": result["person_coverage"]["classification"],
            "sentiment_breakdown": result["person_coverage"]["sentiment"],
            "articles": [asdict(a) for a in result["person_coverage"]["articles"]],
        },
        "network": result["connections"],
        "investments": result["investments"],
        "other_affiliations": result["other_affiliations"],
        "firecrawl": result["firecrawl"],
        "counts": {
            "companies_screened": len(screenings),
            "companies_flagged": len(attributable),
            "companies_flagged_including_non_control": len(flagged),
            "articles_reviewed": sum(s.articles_reviewed for s in screenings),
            "connections": len(result["connections"]),
            "investments": len(result["investments"]),
        },
        "notes": result["notes"],
        "disclaimer": DISCLAIMER,
    }

    # The last gate. Pure and idempotent, so it can be run again on improved
    # evidence and produce a verdict computed the same way.
    report = validate.validate_report(report)

    # -- the decision layer -------------------------------------------------
    findings = result.get("findings") or []
    coverage = result.get("coverage") or {}

    report["findings"] = [_finding_dict(f) for f in findings]
    report["coverage"] = coverage
    report["assessment"] = risk_score.assess(
        findings,
        coverage,
        identity_confirmed=not result["subject"].identity_unverified,
        validation=report.get("validation"),
    )
    report["timeline"] = _timeline(findings, result.get("screenings", []))

    # Raises issues, never edits. Every figure above stays traceable to the
    # deterministic layer that produced it.
    fetcher = result.get("fetcher")
    if fetcher is not None:
        report["qc"] = qc.review(fetcher, report)
        # The review spends tokens, and the cost block was computed before it
        # ran. Re-read it so the reported spend is the whole run's.
        report["usage"] = fetcher.meter.summary()

    return report


def _finding_dict(finding) -> dict:
    """A Finding as JSON, with its computed properties made explicit.

    `is_material` is a property rather than a field, and a consumer reading
    the JSON cannot call it — so it is written out.
    """
    row = asdict(finding)
    row["is_material"] = finding.is_material
    return row


def _timeline(findings: list, screenings: list) -> list:
    """Roles and events on one axis.

    Putting them together is what makes an event that predates an appointment
    visible rather than something the reader has to infer.
    """
    entries = []

    for screening in screenings:
        if screening.role_start:
            entries.append({
                "date": str(screening.role_start),
                "kind": "role_start",
                "label": f"Role begins at {screening.canonical_name}",
                "entity": screening.canonical_name,
                "detail": "; ".join(screening.relationships[:2]),
                "finding_id": None,
            })
        if screening.role_end:
            entries.append({
                "date": str(screening.role_end),
                "kind": "role_end",
                "label": f"Role ends at {screening.canonical_name}",
                "entity": screening.canonical_name,
                "detail": "Later events at this company are not attributed",
                "finding_id": None,
            })

    for finding in findings:
        entries.append({
            "date": finding.event_date,
            "kind": "finding",
            "label": finding.what_happened,
            "entity": finding.entity,
            "stage": finding.stage,
            "is_ongoing": finding.is_ongoing,
            "attributed": finding.attributed_to_subject,
            "within_tenure": finding.within_tenure,
            "finding_id": finding.event_id,
        })

    # Undated entries sort last rather than to 1970.
    entries.sort(key=lambda e: (e["date"] is None, e["date"] or ""))
    return entries


def write_json(report: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)


def write_csv(report: dict, path: str) -> None:
    """The columns the problem statement names, plus the provenance a reader
    needs to check any row."""
    columns = [
        "individual", "company_name", "sentiment", "negative_news_flag",
        "flag_categories", "flag_stages", "negative_articles",
        "neutral_articles", "positive_articles", "articles_reviewed",
        "insufficient_coverage",
        "relationship", "relationship_type", "status", "jurisdiction", "registry_id",
        "link_confidence", "sources", "source_url",
        # A spreadsheet gets copied into decks and emails without the caveats
        # around it, so the verdict has to travel in the row itself.
        "validation_status", "validation_issues",
    ]
    individual = report["query"]["individual"]

    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in report["screening"]:
            breakdown = row["sentiment_breakdown"]
            writer.writerow({
                "individual": individual,
                "company_name": row["company_name"],
                "sentiment": row["sentiment"],
                "negative_news_flag": row["negative_news_flag"],
                "flag_categories": "; ".join(row["flag_categories"]),
                "flag_stages": "; ".join(row["flag_stages"]),
                "negative_articles": breakdown["negative"],
                "neutral_articles": breakdown["neutral"],
                "positive_articles": breakdown["positive"],
                "articles_reviewed": row["articles_reviewed"],
                "insufficient_coverage": row.get("insufficient_coverage", False),
                "relationship": "; ".join(row["relationship"]),
                "relationship_type": row.get("relationship_type", ""),
                "status": row["status"],
                "jurisdiction": row["jurisdiction"] or "",
                "registry_id": row["registry_id"] or "",
                "link_confidence": row["link_confidence"],
                "sources": "; ".join(row["sources"]),
                "source_url": row["source_url"] or "",
                "validation_status": (row.get("validation") or {}).get("status", ""),
                "validation_issues": " | ".join(
                    (row.get("validation") or {}).get("issues", [])
                ),
            })


def summarise(report: dict) -> None:
    subject = report["subject"]
    counts = report["counts"]

    assessment = report.get("assessment") or {}
    if assessment:
        print()
        print(f"  {assessment['risk_level']} RISK"
              f"   confidence {assessment['confidence']}"
              f"   → {assessment['recommended_action']}")
        print(f"  {assessment['headline']}")
        for reason in assessment.get("reasons", [])[:4]:
            print(f"    - {reason['detail']}")
        if assessment.get("limitations"):
            print()
            print("    Limitations")
            for limitation in assessment["limitations"][:4]:
                print(f"      - {limitation}")

    coverage = report.get("coverage") or {}
    if coverage:
        print()
        print(f"    Searched {coverage.get('queries_issued', 0)} queries across "
              f"{len(coverage.get('groups_checked') or [])} adverse checks; "
              f"{coverage.get('after_dedup', 0)} items from "
              f"{coverage.get('publishers', 0)} publishers, "
              f"{coverage.get('promotional_excluded', 0)} promotional excluded")

    print()
    print(f"  {subject['resolved_name'] or subject['query_name']}")
    if subject.get("description"):
        print(f"  {subject['description']}")
    print(f"  match confidence {subject['match_confidence']:.2f} — {subject['match_basis'][:150]}")
    print()
    for key, value in counts.items():
        print(f"    {value:>5}  {key.replace('_', ' ')}")

    print()
    print("    Company screening")
    print(
        f"    {'company':38} {'sentiment':10} {'flag':5} {'articles':>8}  verdict"
    )
    for row in report["screening"]:
        flag = "YES" if row["negative_news_flag"] else "-"
        tone = "no data" if row["insufficient_coverage"] else row["sentiment"]
        verdict = (row.get("validation") or {}).get("status", "")
        print(
            f"    {row['company_name'][:36]:38} {tone:10} "
            f"{flag:5} {row['articles_reviewed']:>8}  {verdict}"
        )

    # Only the rows the validator was not satisfied with. A clean row needs no
    # explanation, and printing one for every company buries the ones that do.
    qualified = [
        row for row in report["screening"]
        if (row.get("validation") or {}).get("issues")
    ]
    if qualified:
        print()
        print("    Read with care")
        for row in qualified:
            print(f"      {row['company_name'][:44]}")
            for issue in row["validation"]["issues"]:
                print(f"        - {issue}")

    flagged = [r for r in report["screening"] if r["negative_news_flag"]]
    if flagged:
        print()
        print("    Findings (stage matters: an allegation is not a finding)")
        for row in flagged:
            for flag in row["flags"]:
                print(f"      {row['company_name'][:32]:34} {flag['category']:14} {flag['stage']}")

    if report["notes"]:
        print()
        print("    Notes:")
        for note in report["notes"][:8]:
            print(f"      - {note}")

    if report.get("usage"):
        print(usage.format_summary(report["usage"]))
