"""Who did you mean? — the cheap step that runs before anything expensive.

The pipeline used to resolve an identity silently and then spend Firecrawl
credits researching whatever it picked. When it picked wrong, the mistake was
invisible: a confident 95% match, a full report, and no way for the reader to
know it was about a different person of the same name.

This module answers the same question out loud instead. It asks only free,
keyless sources — Wikipedia search, its REST summaries, Wikidata search and
the Wikidata Query Service — ranks what comes back, and hands the user a short
list with the evidence for each. Nothing here scrapes, and nothing here costs
a credit. Deep research starts only after a human has pointed at a row.

Three entry shapes, one output:

  person only      "Virat Kohli"                  -> which Virat Kohli?
  person + company "Virat Kohli" + "One8"         -> the company verifies
  company only     "Wipro"                        -> the company, and the
                                                     people who run it

OpenAI, when a key is present, reorders the shortlist and explains each row.
It is given only candidates that a free source already returned, so it cannot
introduce a person who does not exist. Without a key the deterministic scorer
decides, and the flow is identical.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field

from .. import config
from ..sources import openai_client, wikidata, wikipedia

# Below this the name simply is not the one that was typed. Lifted from the
# same failure that produced wikipedia.NAME_SIMILARITY_FLOOR: without a floor,
# a bonus signal alone can promote an unrelated person to the top.
NAME_FLOOR = 0.45

# How many free-source hits to summarise. Each summary is one cached GET, so
# this is the only real cost of the whole step.
MAX_LOOKUPS = 6

# What the UI shows. More than this is a list to scroll, not a choice to make.
MAX_CANDIDATES = 6


@dataclass
class Candidate:
    """One possible answer to "who did you mean?", with its evidence."""

    id: str
    name: str
    kind: str  # "person" | "company"
    description: str | None = None
    extract: str | None = None
    wikidata_id: str | None = None
    wikipedia_url: str | None = None
    thumbnail: str | None = None
    score: float = 0.0
    evidence: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    source_url: str | None = None
    ranked_by: str = "deterministic scorer"

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "extract": (self.extract or "")[:400] or None,
            "wikidata_id": self.wikidata_id,
            "wikipedia_url": self.wikipedia_url,
            "thumbnail": self.thumbnail,
            "score": round(self.score, 3),
            "evidence": self.evidence,
            "sources": sorted(set(self.sources)),
            "source_url": self.source_url,
            "ranked_by": self.ranked_by,
        }


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def _looks_like_an_organisation(text: str) -> bool:
    return bool(re.search(
        r"\b(compan|corporation|conglomerate|bank|group|holdings?|limited|"
        r"ltd|inc|plc|business|enterprise|firm|manufacturer|retailer|"
        r"brand|startup|subsidiar)\w*\b",
        text or "", re.IGNORECASE,
    ))


# ---------------------------------------------------------------------------
# Gathering — free sources only
# ---------------------------------------------------------------------------

def _from_wikipedia(fetcher, query: str, company: str | None,
                    want: str) -> list:
    """Wikipedia search hits, summarised.

    The company is searched as a second query rather than appended to the
    first: "Virat Kohli One8" as one string matches articles about neither.
    """
    titles = wikipedia.search_titles(fetcher, query, limit=MAX_LOOKUPS)
    if company:
        for title in wikipedia.search_titles(fetcher, f"{query} {company}", limit=3):
            if title not in titles:
                titles.append(title)

    found = []
    for title in titles[:MAX_LOOKUPS]:
        if _similarity(title, query) < NAME_FLOOR and (
            not company or company.lower() not in title.lower()
        ):
            continue

        summary = wikipedia.summary(fetcher, title)
        if not summary or summary.get("type") not in (None, "standard"):
            continue

        is_person = wikipedia.looks_like_a_person(summary)
        kind = "person" if is_person else "company"
        if want == "person" and not is_person:
            continue
        if want == "company" and is_person:
            continue

        found.append(Candidate(
            id=f"wikipedia:{title}",
            name=summary.get("title") or title,
            kind=kind,
            description=summary.get("description"),
            extract=summary.get("extract"),
            wikipedia_url=(summary.get("content_urls", {})
                           .get("desktop", {}).get("page")),
            thumbnail=(summary.get("thumbnail") or {}).get("source"),
            sources=["Wikipedia"],
            source_url=(summary.get("content_urls", {})
                        .get("desktop", {}).get("page")),
        ))
    return found


def _from_wikidata(fetcher, query: str, want: str) -> list:
    """Wikidata search hits, classified by whether the item is a human."""
    found = []
    for entry in wikidata.search_entities(fetcher, query, limit=MAX_LOOKUPS):
        if _similarity(entry.get("label") or "", query) < NAME_FLOOR:
            continue

        is_person = wikidata.is_human(fetcher, entry["id"])
        kind = "person" if is_person else "company"
        if want == "person" and not is_person:
            continue
        if want == "company" and is_person:
            continue
        # Wikidata holds far more than organisations. Without this, "Wipro"
        # offers the Wipro campus and a Wipro share class as candidates.
        if kind == "company" and not _looks_like_an_organisation(
            entry.get("description") or ""
        ):
            continue

        found.append(Candidate(
            id=f"wikidata:{entry['id']}",
            name=entry.get("label") or query,
            kind=kind,
            description=entry.get("description"),
            wikidata_id=entry["id"],
            sources=["Wikidata"],
            source_url=entry.get("source_url"),
        ))
    return found


def _merge(groups: list) -> list:
    """One row per real-world entity, however many sources found it.

    Keyed on the name, because a Wikipedia article and a Wikidata item for the
    same person carry different ids and must not appear as two choices.
    """
    merged: dict = {}
    for candidate in [c for group in groups for c in group]:
        key = (candidate.name or "").strip().lower()
        if not key:
            continue

        existing = merged.get(key)
        if existing is None:
            merged[key] = candidate
            continue

        # Keep the richer record, and carry the other's identifiers across.
        existing.wikidata_id = existing.wikidata_id or candidate.wikidata_id
        existing.wikipedia_url = existing.wikipedia_url or candidate.wikipedia_url
        existing.thumbnail = existing.thumbnail or candidate.thumbnail
        existing.description = existing.description or candidate.description
        existing.extract = existing.extract or candidate.extract
        existing.sources += candidate.sources
        existing.source_url = existing.source_url or candidate.source_url
        if existing.wikidata_id and existing.id.startswith("wikipedia:"):
            existing.id = f"wikidata:{existing.wikidata_id}"

    return list(merged.values())


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def _score(candidate: Candidate, query: str, company: str | None) -> None:
    """Deterministic score and the evidence behind it.

    Every component is stated in `evidence`, so a user who disagrees with the
    order can see which signal caused it rather than arguing with a number.
    """
    similarity = _similarity(candidate.name, query)
    candidate.score = similarity
    candidate.evidence = [f"name matches '{query}' ({similarity:.0%})"]

    if company:
        haystack = " ".join(filter(None, [
            candidate.description, candidate.extract, candidate.name,
        ])).lower()
        if company.lower() in haystack:
            candidate.score += 0.30
            candidate.evidence.append(
                f"their article names '{company}', the company you supplied"
            )

    # A Wikidata item means registry-grade company and connection data exists
    # for this entity. A Wikipedia-only match will produce a thinner report.
    if candidate.wikidata_id:
        candidate.score += 0.10
        candidate.evidence.append(
            f"has a Wikidata entity ({candidate.wikidata_id}), so company and "
            "network data can be looked up"
        )

    if candidate.description:
        candidate.evidence.append(candidate.description)


def _rerank_with_openai(fetcher, candidates: list, query: str,
                        company: str | None, want: str) -> list:
    """Let the model reorder the shortlist and say why.

    It is handed the candidates and nothing else. Ids that are not in the
    input are discarded, so the model cannot introduce a person, and a failed
    or malformed reply leaves the deterministic order untouched.
    """
    client = openai_client.OpenAIClient(
        fetcher, budget=config.OPENAI_MAX_CANDIDATE_CALLS, purpose="candidate ranking"
    )
    if not client.available or len(candidates) < 2:
        return candidates

    listing = [
        {
            "id": c.id,
            "name": c.name,
            "kind": c.kind,
            "description": c.description,
            "extract": (c.extract or "")[:300],
        }
        for c in candidates
    ]

    system = (
        "You disambiguate entities for a financial due-diligence tool. "
        "You are given candidates already retrieved from Wikipedia and "
        "Wikidata. Decide which the user most likely meant. Use only the "
        "text provided; never add entities and never rely on memory for "
        "facts. Reply as JSON: {\"ranking\": [{\"id\": str, \"confidence\": "
        "0-1, \"reason\": str}], \"ambiguous\": bool}. `reason` must be one "
        "short sentence citing the provided text. Set `ambiguous` true when "
        "two candidates are genuinely hard to tell apart."
    )
    user = json.dumps({
        "query_name": query,
        "query_company": company,
        "looking_for": want,
        "candidates": listing,
    }, ensure_ascii=False)

    answer = client.complete_json(system, user, max_tokens=600)
    if not answer or not isinstance(answer.get("ranking"), list):
        return candidates

    by_id = {c.id: c for c in candidates}
    ordered, seen = [], set()
    for index, row in enumerate(answer["ranking"]):
        if not isinstance(row, dict):
            continue
        candidate = by_id.get(row.get("id"))
        if candidate is None or candidate.id in seen:
            continue

        seen.add(candidate.id)
        candidate.ranked_by = f"OpenAI ({config.OPENAI_MODEL})"
        # Its position drives the order; the deterministic score is kept in
        # the payload so the two can be compared.
        candidate.score = max(candidate.score, 0.0) + (len(answer["ranking"]) - index)
        reason = (row.get("reason") or "").strip()
        if reason:
            candidate.evidence.insert(0, reason)
        ordered.append(candidate)

    # Anything the model omitted keeps its deterministic place at the end.
    ordered += [c for c in candidates if c.id not in seen]
    return ordered


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def discover(fetcher, query: str, company: str | None = None,
             mode: str = "person", say=lambda m: None) -> dict:
    """Candidates for a typed query. Free sources only; no deep research.

    `mode` is "person" or "company" and says what the *query* names. A
    company-only search additionally returns the people that company names as
    founders and officers, because the screening that follows is always about
    a person.
    """
    query = (query or "").strip()
    company = (company or "").strip() or None
    if not query:
        return {"query": {"name": query, "company": company, "mode": mode},
                "candidates": [], "notes": ["No name was supplied."]}

    want = "company" if mode == "company" else "person"

    say("  Wikipedia and Wikidata candidates ...")
    found = _merge([
        _from_wikipedia(fetcher, query, company, want),
        _from_wikidata(fetcher, query, want),
    ])

    # A company-only query still has to end at a person, so offer the people
    # that company names. These are registry-grade: the organisation's own
    # Wikidata entity states them.
    related_people = []
    if want == "company":
        for candidate in found[:2]:
            if not candidate.wikidata_id:
                continue
            say(f"  People named by {candidate.name} ...")
            for person in wikidata.people_for(fetcher, candidate.wikidata_id):
                related_people.append(Candidate(
                    id=f"wikidata:{person['wikidata_id']}",
                    name=person["name"],
                    kind="person",
                    description=person.get("description"),
                    wikidata_id=person["wikidata_id"],
                    sources=["Wikidata Query Service"],
                    source_url=person["source_url"],
                    score=0.5,
                    evidence=[
                        f"{candidate.name} names them as "
                        f"{', '.join(person['relationships']) or 'connected'}"
                    ],
                ))

    for candidate in found:
        _score(candidate, query, company)
    found.sort(key=lambda c: c.score, reverse=True)
    found = found[:MAX_CANDIDATES]

    if found:
        found = _rerank_with_openai(fetcher, found, query, company, want)

    people = {c.id: c for c in related_people}
    for candidate in found:
        people.pop(candidate.id, None)
    related = sorted(people.values(), key=lambda c: c.score, reverse=True)[:MAX_CANDIDATES]

    notes = list(fetcher.notes)
    if not found:
        notes.append(
            f"No free source returned a {want} matching '{query}'. Check the "
            "spelling, or supply a connected company to narrow the search."
        )

    return {
        "query": {"name": query, "company": company, "mode": mode},
        "candidates": [c.as_dict() for c in found],
        "related_people": [c.as_dict() for c in related],
        "reasoning_layer": openai_client.describe_configuration(),
        "usage": fetcher.meter.summary(),
        "notes": notes,
    }
