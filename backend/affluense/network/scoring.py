"""Relevance scoring for suggested connections.

The score is a weighted sum of components, each in [0, 1]. It is deliberately
not a black box: every candidate carries the signals that produced its score,
so a relationship manager can see *why* someone is suggested and disagree
with the reasoning rather than with a bare number.

    role_overlap        holds a role the subject also holds
    industry_overlap    operates in the subject's industries
    network_proximity   shares a company with someone already in the network
    geography           operates in a country the subject operates in
    prominence          seniority proxy: Wikipedia language editions

Prominence is a weak proxy and weighted lowest on purpose. It rewards being
well documented, which correlates with seniority but also with fame; using it
heavily would surface celebrities over relevant executives.
"""

from __future__ import annotations

WEIGHTS = {
    "role_overlap": 0.30,
    "industry_overlap": 0.30,
    "network_proximity": 0.20,
    "geography": 0.10,
    "prominence": 0.10,
}

# Above this many Wikipedia language editions, prominence stops adding score.
PROMINENCE_CEILING = 40


def _role_overlap(candidate: dict, profile: dict) -> tuple:
    subject_roles = {r.lower() for r in profile["roles"]}
    candidate_roles = {r.lower() for r in candidate.get("roles", [])}
    shared = subject_roles & candidate_roles
    if shared:
        return 1.0, f"holds the same role as the subject: {', '.join(sorted(shared))}"
    if candidate_roles:
        return 0.4, f"holds a senior role ({', '.join(sorted(candidate_roles))}) but not one the subject holds"
    return 0.0, ""


def _industry_overlap(candidate: dict, profile: dict) -> tuple:
    subject = set(profile["industry_qids"])
    matched = set(candidate.get("matched_industries", []))
    shared = subject & matched
    if not shared or not subject:
        return 0.0, ""
    share = len(shared) / len(subject)
    label_by_qid = profile.get("industry_labels", {})
    names = [label_by_qid.get(q, q) for q in sorted(shared)]
    return min(1.0, share + 0.4), f"operates in {', '.join(names)}"


def _network_proximity(candidate: dict, network_companies: set,
                       network_names: set) -> tuple:
    companies = {c.lower() for c in candidate.get("companies", []) if c}
    shared = companies & network_companies
    if shared:
        return 1.0, f"connected to {', '.join(sorted(shared))}, where the subject's network already reaches"
    if (candidate.get("name") or "").lower() in network_names:
        return 0.0, ""
    return 0.0, ""


def _geography(candidate: dict, profile: dict) -> tuple:
    country = candidate.get("country")
    if country and country in profile["countries"]:
        return 1.0, f"based in {country}, where the subject already operates"
    if country:
        return 0.35, f"based in {country}, extending the network beyond the subject's current markets"
    return 0.0, ""


def _prominence(candidate: dict) -> tuple:
    sitelinks = candidate.get("sitelinks", 0)
    if sitelinks <= 1:
        return 0.0, ""
    value = min(1.0, sitelinks / PROMINENCE_CEILING)
    return value, f"documented in {sitelinks} Wikipedia language editions"


def score_candidate(candidate: dict, profile: dict, network_companies: set,
                    network_names: set) -> dict:
    components, signals = {}, []

    for key, (value, reason) in {
        "role_overlap": _role_overlap(candidate, profile),
        "industry_overlap": _industry_overlap(candidate, profile),
        "network_proximity": _network_proximity(candidate, network_companies, network_names),
        "geography": _geography(candidate, profile),
        "prominence": _prominence(candidate),
    }.items():
        components[key] = round(value, 3)
        if reason:
            signals.append(reason)

    relevance = sum(WEIGHTS[key] * value for key, value in components.items())

    return {
        "name": candidate.get("name"),
        "company": (candidate.get("companies") or [None])[0],
        "companies": candidate.get("companies", []),
        "role": (candidate.get("roles") or [None])[0],
        "roles": candidate.get("roles", []),
        "location": candidate.get("country"),
        "wikidata_id": candidate.get("wikidata_id"),
        "relevance_score": round(min(1.0, relevance), 3),
        "score_components": components,
        "signals": signals,
        "source": candidate.get("source"),
        "source_url": candidate.get("source_url"),
    }


def rank(candidates: list, profile: dict, network: list, limit: int = 25) -> list:
    """Score, drop anyone already connected, and return the best first."""
    network_names = {(p.get("name") or "").lower() for p in network}
    network_companies = {
        (p.get("company") or "").lower() for p in network if p.get("company")
    }
    subject_companies = {(c or "").lower() for c in profile.get("company_names", [])}
    network_companies |= subject_companies

    scored = []
    for candidate in candidates:
        name = (candidate.get("name") or "").lower()
        # A suggestion the subject already knows is not a suggestion.
        if not name or name in network_names:
            continue
        # Wikidata labels fall back to the Q-number when no English label
        # exists; those are unusable as a suggestion.
        if name.startswith("q") and name[1:].isdigit():
            continue
        scored.append(score_candidate(candidate, profile, network_companies, network_names))

    scored.sort(key=lambda row: row["relevance_score"], reverse=True)
    return scored[:limit]
