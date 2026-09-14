"""Writers for the Problem Statement 2 deliverable."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict

from .. import usage as usage_mod
from ..models import DISCLAIMER
from .scoring import WEIGHTS

NETWORK_DISCLAIMER = (
    "Suggestions are computed from public officer and industry data. A high "
    "relevance score means structural similarity to the subject's existing "
    "network, not a warm introduction or any stated willingness to meet. "
    "Deceased people are excluded, but tenure overlap is not verified: a "
    "candidate may have left the company named here."
)


def build_report(result: dict, query_name: str, query_company: str | None) -> dict:
    return {
        "query": {
            "individual": query_name,
            "company": query_company,
            "collected_at": result["collected_at"],
        },
        "cache": result.get("cache", {}),
        "subject": asdict(result["subject"]),
        "profile": {
            "roles": result["profile"]["roles"],
            "industries": result["profile"]["industries"],
            "countries": result["profile"]["countries"],
            "companies": result["profile"]["company_names"],
        },
        "current_network": result["network"],
        "candidate_basis": result.get("candidate_basis", "industry and role"),
        "suggested_connections": result["suggestions"],
        "scoring": {
            "weights": WEIGHTS,
            "explanation": (
                "relevance_score is the weighted sum of the components stored "
                "on each suggestion. Every component is in [0, 1]."
            ),
        },
        "counts": {
            "companies": len(result.get("companies", [])),
            "current_network": len(result["network"]),
            "candidate_pool": result.get("candidate_pool_size", 0),
            "suggestions": len(result["suggestions"]),
        },
        "firecrawl": result.get("firecrawl", {}),
        "usage": result.get("usage", {}),
        "notes": result["notes"],
        "disclaimer": f"{DISCLAIMER} {NETWORK_DISCLAIMER}",
    }


def write_json(report: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)


def write_csv(report: dict, path: str) -> None:
    columns = [
        "individual", "rank", "suggested_name", "company", "role", "location",
        "relevance_score", "role_overlap", "industry_overlap",
        "network_proximity", "geography", "prominence", "signals", "source_url",
    ]
    individual = report["query"]["individual"]

    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for index, row in enumerate(report["suggested_connections"], start=1):
            parts = row["score_components"]
            writer.writerow({
                "individual": individual,
                "rank": index,
                "suggested_name": row["name"],
                "company": row["company"] or "",
                "role": row["role"] or "",
                "location": row["location"] or "",
                "relevance_score": row["relevance_score"],
                "role_overlap": parts["role_overlap"],
                "industry_overlap": parts["industry_overlap"],
                "network_proximity": parts["network_proximity"],
                "geography": parts["geography"],
                "prominence": parts["prominence"],
                "signals": " | ".join(row["signals"]),
                "source_url": row["source_url"] or "",
            })


def summarise(report: dict) -> None:
    subject = report["subject"]
    profile = report["profile"]
    counts = report["counts"]

    print()
    print(f"  {subject['resolved_name'] or subject['query_name']}")
    if profile["roles"]:
        print(f"  Roles: {', '.join(profile['roles'][:5])}")
    if profile["industries"]:
        print(f"  Industries: {', '.join(profile['industries'][:5])}")
    print()
    for key, value in counts.items():
        print(f"    {value:>5}  {key.replace('_', ' ')}")

    if report["current_network"]:
        print()
        print("    Current network (sample)")
        for person in report["current_network"][:8]:
            print(f"      {(person['name'] or '')[:34]:36} {person['tie'][:44]}")

    print()
    basis = report.get("candidate_basis", "industry and role")
    print(f"    Suggested connections (matched on {basis})")
    print(f"    {'#':>3}  {'name':28} {'company':26} {'score':>6}")
    for index, row in enumerate(report["suggested_connections"][:15], start=1):
        print(
            f"    {index:>3}  {(row['name'] or '')[:26]:28} "
            f"{(row['company'] or '')[:24]:26} {row['relevance_score']:>6.2f}"
        )

    if report["suggested_connections"]:
        top = report["suggested_connections"][0]
        print()
        print(f"    Why #{1} ({top['name']}):")
        for signal in top["signals"]:
            print(f"      - {signal}")

    if not report["suggested_connections"]:
        print("      none — see notes below for why")

    if report["notes"]:
        print()
        print("    Notes:")
        for note in report["notes"][:6]:
            print(f"      - {note}")

    if report.get("usage"):
        print(usage_mod.format_summary(report["usage"]))
