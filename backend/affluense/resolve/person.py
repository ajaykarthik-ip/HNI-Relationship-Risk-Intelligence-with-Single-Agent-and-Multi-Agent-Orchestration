"""Resolve a typed name to one identified person.

This is the step that decides whether the whole report is about the right
human. Two failure modes matter:

  misspelling   Wikidata's search does no fuzzy matching. "virat kholi"
                returned nothing and silently emptied half a report, while
                Wikipedia matched it correctly. So Wikipedia resolves the
                spelling first, and its title is what Wikidata is asked for.

  collision     Common names match several people. The company supplied with
                the query is used as the disambiguator, which is exactly why
                the problem statement asks for one.
"""

from __future__ import annotations

from ..models import Subject
from ..sources import wikidata, wikipedia


# Below this, the pipeline must not assert that it knows who this is.
IDENTITY_FLOOR = 0.60


def from_confirmed(fetcher, name: str, company: str | None,
                   confirmed: dict) -> tuple:
    """Build the Subject from a candidate the user picked in the UI.

    A human has already answered "which person is this?", so no inference
    happens here. Confidence is 1.0 and the basis says who decided, which is
    a stronger claim than any heuristic this module can make — and an honest
    one, because it names the decider rather than implying the tool knew.
    """
    qid = confirmed.get("wikidata_id")
    resolved_name = confirmed.get("name") or name

    subject = Subject(query_name=name, query_company=company)
    subject.resolved_name = resolved_name
    subject.description = confirmed.get("description")
    subject.wikipedia_url = confirmed.get("wikipedia_url")
    subject.wikidata_id = qid
    subject.match_confidence = 1.0
    subject.identity_unverified = False
    subject.match_basis = (
        f"Confirmed by the user from a candidate list: '{resolved_name}'"
        + (f" (Wikidata {qid})" if qid else " (no Wikidata entity, so registry-"
           "backed company and connection data will be thin)")
        + "."
    )

    # The biography still has to be fetched: it feeds company discovery.
    bio = {}
    if confirmed.get("wikipedia_url") or resolved_name:
        bio = wikipedia.lookup(fetcher, resolved_name, company) or {}
        if bio.get("unmatched"):
            bio = {}

    return subject, [confirmed], bio


def resolve(fetcher, name: str, company: str | None = None,
            confirmed: dict | None = None) -> tuple:
    """Return (Subject, candidates, biography).

    When no article qualifies, the subject keeps the name that was typed.
    Substituting a different person is never an improvement over admitting
    the name could not be identified.

    `confirmed` short-circuits the whole guess: see `from_confirmed`.
    """
    if confirmed:
        return from_confirmed(fetcher, name, company, confirmed)

    subject = Subject(query_name=name, query_company=company)

    bio = wikipedia.lookup(fetcher, name, company)
    matched = bool(bio) and not bio.get("unmatched")

    resolved_name = bio.get("title") if matched else name
    subject.resolved_name = resolved_name
    subject.description = bio.get("description") if matched else None
    subject.wikipedia_url = bio.get("source_url") if matched else None

    spelling_corrected = matched and resolved_name.lower() != name.lower()

    candidates = []
    qid = None
    if matched:
        candidates = wikidata.search_entities(fetcher, resolved_name)
        if not candidates and spelling_corrected:
            candidates = wikidata.search_entities(fetcher, name)

        for candidate in candidates:
            if wikidata.is_human(fetcher, candidate["id"]):
                candidate["is_human"] = True
                qid = candidate["id"]
                break
            candidate["is_human"] = False

    subject.wikidata_id = qid

    if not matched:
        subject.match_confidence = 0.0
        subject.match_basis = (
            f"No encyclopedia article was identified for '{name}'. "
            f"{bio.get('selection_basis', '')} "
            "The search below runs on the name as typed. Nothing here is "
            "confirmed to concern one particular person, so treat every row "
            "as coverage matching a name rather than a finding about someone."
        ).strip()
    elif qid and company:
        subject.match_confidence = 0.95
        subject.match_basis = (
            f"Wikipedia resolved the query to '{resolved_name}'; Wikidata "
            f"{qid} is the highest-ranked human for that name. The supplied "
            f"company '{company}' was used to steer the search."
        )
    elif qid:
        subject.match_confidence = 0.80
        subject.match_basis = (
            f"Matched to Wikidata {qid}, the highest-ranked human for "
            f"'{resolved_name}'. No company was supplied to disambiguate, so a "
            "name collision cannot be ruled out."
        )
    else:
        subject.match_confidence = 0.45
        subject.match_basis = (
            f"Wikipedia matched '{resolved_name}' but no Wikidata entity was "
            "found. Registry-backed company and connection data will be thin; "
            "check the spelling."
        )

    if spelling_corrected:
        subject.match_basis += (
            f" Note: the query name '{name}' was corrected to '{resolved_name}'."
        )

    subject.identity_unverified = subject.match_confidence < IDENTITY_FLOOR
    return subject, candidates, (bio if matched else {})
