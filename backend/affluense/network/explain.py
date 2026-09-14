"""Why is this person worth meeting?

The scorer already produces a defensible number and stores the signals behind
it. What it produces for the reader is a template string -- "operates in
Financial services" -- which states a fact without making an argument. A
relationship manager deciding whether to ask for an introduction needs the
argument.

Two things happen here, and only one of them involves a model.

Deterministic: a prominence gate. `prominence` is a proxy for seniority
measured by Wikipedia language editions, which also measures fame. Where a
candidate has no role or industry overlap, prominence was the only thing
lifting them, and the result was a famous stranger ranked above a relevant
executive. Prominence now contributes nothing unless something real is
already matching.

Model: the rationale. It is handed the stored components and the signals and
asked to write the argument they support. It cannot change the score, cannot
reorder the list, and cannot introduce a person -- it writes prose about a
number that was already fixed.
"""

from __future__ import annotations

import json

from .. import config
from ..sources import openai_client
from .scoring import WEIGHTS

SYSTEM = (
    "You write one-sentence rationales for suggested business introductions. "
    "You are given each candidate's match signals, already computed. Write "
    "why this person is worth meeting, using only those signals.\n\n"
    "- One sentence, under 30 words, specific.\n"
    "- Never invent a shared connection, deal or history that is not in the "
    "signals.\n"
    "- If the only signal is prominence, say plainly that the match is weak.\n"
    'Reply as JSON: {"rationales": [{"index": int, "why": str, '
    '"supported": bool}]}'
)


def gate_prominence(suggestions: list) -> int:
    """Strip prominence from candidates with nothing else going for them.

    Returns how many were adjusted. The score is recomputed from the same
    weights, so the ranking stays explainable by the same arithmetic.
    """
    adjusted = 0
    for suggestion in suggestions:
        components = suggestion.get("score_components") or {}
        real_overlap = (
            components.get("role_overlap", 0) > 0
            or components.get("industry_overlap", 0) > 0
            or components.get("network_proximity", 0) > 0
        )
        if real_overlap or not components.get("prominence"):
            continue

        components["prominence"] = 0.0
        suggestion["relevance_score"] = round(
            sum(WEIGHTS[k] * v for k, v in components.items() if k in WEIGHTS), 3
        )
        suggestion.setdefault("signals", []).append(
            "prominence not counted: no role, industry or network overlap to "
            "support it"
        )
        adjusted += 1

    suggestions.sort(key=lambda s: -s.get("relevance_score", 0))
    return adjusted


def add_rationales(fetcher, suggestions: list, profile: dict,
                   limit: int = 20, say=None) -> int:
    """Write a real "why" for the top suggestions. Returns how many got one."""
    if not suggestions:
        return 0

    client = openai_client.OpenAIClient(
        fetcher, budget=config.OPENAI_MAX_NETWORK_CALLS,
        purpose="connection rationales",
    )
    if not client.available:
        return 0

    batch = suggestions[:limit]
    answer = client.complete_json(
        SYSTEM,
        json.dumps({
            "subject_profile": {
                "roles": profile.get("roles", []),
                "industries": profile.get("industries", []),
                "countries": profile.get("countries", []),
            },
            "candidates": [
                {
                    "index": i,
                    "name": s.get("name"),
                    "company": s.get("company"),
                    "role": s.get("role"),
                    "score": s.get("relevance_score"),
                    "components": s.get("score_components"),
                    "signals": s.get("signals", []),
                }
                for i, s in enumerate(batch)
            ],
        }, ensure_ascii=False),
        max_tokens=1200,
    )
    if not answer or not isinstance(answer.get("rationales"), list):
        return 0

    written = 0
    for row in answer["rationales"]:
        if not isinstance(row, dict):
            continue
        index = row.get("index")
        why = (row.get("why") or "").strip()
        if not isinstance(index, int) or not 0 <= index < len(batch) or not why:
            continue
        batch[index]["rationale"] = why[:240]
        batch[index]["rationale_supported"] = bool(row.get("supported", True))
        batch[index]["rationale_by"] = config.OPENAI_MODEL
        written += 1

    if say and written:
        say(f"  wrote {written} connection rationale(s)")
    return written
