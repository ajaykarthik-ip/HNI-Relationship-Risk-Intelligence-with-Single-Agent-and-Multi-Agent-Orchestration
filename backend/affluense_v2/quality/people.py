"""Is this a person, and are they worth suggesting?

Three generic filters that PS2's first live run showed were missing.

**Organisations in the network list.** Firecrawl's `associates` extraction
returns whatever a page names beside the subject, and pages name companies as
readily as people. The run listed "Temasek — subsidiary", "Asia Society —
founder" and "Business Standard — board member" under *people already in the
network*. Two signals catch that without a name list: the shape of the name
itself, and the shape of the tie. A tie that describes a role the **subject**
holds -- founder, board member, subsidiary -- means the other end of it is an
organisation, because you are not the founder of a person.

**Endorsers suggested as business peers.** A brand ambassador is in the
coverage of an industry without being in the industry. V1 already has the
vocabulary for this in `relationships.MEDIA_ROLE`; it simply was never applied
to candidates.

**Colleagues suggested as new connections.** Someone at the subject's own
company is not a connection to make -- they already work together. The scorer
uses the subject's companies for proximity scoring but never for exclusion.

Nothing here knows a name. Every rule is structural.
"""

from __future__ import annotations

import re

from affluense.resolve.relationships import BOARD_ROLE, MEDIA_ROLE, OWNER_ROLE

from .entities import LEGAL_FORM, NON_PROFIT_FORM
from .text import tokens

# Ties whose other end is an organisation. "Founder" describes what the subject
# is *to* the named thing, so the named thing is a company, not a colleague.
ORG_SHAPED_TIE = re.compile(
    r"\b(subsidiary|parent|holding|division|unit|brand|portfolio|"
    r"investment|investor in|stake|shareholding|owns?|owned)\b",
    re.IGNORECASE,
)

# Roles that carry a real financial or executive interest. Present alongside a
# media word, the substance wins -- "Shareholder / Ambassador" is a holding.
SUBSTANTIVE_ROLE = re.compile(
    r"\b(founder|co-?founder|chief|ceo|cto|cfo|coo|managing director|"
    r"director|chairman|chairperson|president|partner|promoter|owner|"
    r"shareholder|investor|head of|vice[- ]president|executive)\b",
    re.IGNORECASE,
)


def looks_like_person(name: str | None) -> bool:
    """Whether a name plausibly belongs to an individual.

    Deliberately conservative in the direction of *excluding*: a dropped
    organisation costs a row in a list, while an organisation presented as a
    person someone should meet is simply wrong.
    """
    raw = (name or "").strip()
    if not raw:
        return False
    if LEGAL_FORM.search(raw) or NON_PROFIT_FORM.search(raw):
        return False
    words = [w for w in re.split(r"\s+", raw) if w]
    # A single word is a brand far more often than a full name, and a full name
    # is what a suggestion needs to be actionable anyway.
    if len(words) < 2:
        return False
    if len(words) > 6:
        return False
    return True


def is_organisation_tie(relationship: str | None) -> bool:
    """Whether a tie describes the subject's role *at* the named thing."""
    text = relationship or ""
    if ORG_SHAPED_TIE.search(text):
        return True
    # "founder", "board member", "chairman" describe what the subject is to an
    # organisation. A tie to a person reads "co-founder", "colleague", "spouse".
    if re.search(r"\bco-?founder\b", text, re.IGNORECASE):
        return False
    return bool(OWNER_ROLE.search(text) or BOARD_ROLE.search(text))


def clean_network(network: list, say=None) -> tuple:
    """Keep the people. Returns (people, organisations_removed).

    Removed rows are handed back rather than dropped, so a caller can report
    them as affiliations instead of losing the fact entirely.
    """
    people, organisations = [], []
    for entry in network or []:
        name = entry.get("name")
        tie = entry.get("tie") or entry.get("relationship")
        # A Wikidata co-officer is a person by construction -- the query
        # filters on P31=Q5 -- so structural sources are trusted outright.
        if entry.get("wikidata_id") and entry.get("tie_type") == "co-officer":
            people.append(entry)
            continue
        if not looks_like_person(name) or is_organisation_tie(tie):
            organisations.append(entry)
            continue
        people.append(entry)

    if say and organisations:
        say(f"    {len(organisations)} entry(ies) in the network were "
            "organisations rather than people, and are listed as affiliations")
    return people, organisations


def is_media_role(roles) -> bool:
    """True when every role is an endorsement rather than a position.

    An ambassador appears in an industry's coverage without being in the
    industry, so suggesting them as a peer is a category error.
    """
    values = [r for r in (roles or []) if isinstance(r, str) and r.strip()]
    if not values:
        return False
    for role in values:
        if SUBSTANTIVE_ROLE.search(role):
            return False
    return any(MEDIA_ROLE.search(role) for role in values)


def _company_keys(names) -> set:
    keys = set()
    for name in names or []:
        if isinstance(name, str) and name.strip():
            key = " ".join(sorted(tokens(name)))
            if key:
                keys.add(key)
    return keys


def works_with_subject(candidate: dict, subject_companies) -> bool:
    """Whether this candidate already works at one of the subject's companies.

    A colleague is not a new connection. Compared on distinctive word sets so
    "Nykaa" matches "Nykaa E-Retail Ltd" without matching "Nykaa" inside an
    unrelated phrase.
    """
    wanted = _company_keys(subject_companies)
    if not wanted:
        return False
    for company in candidate.get("companies") or []:
        key = " ".join(sorted(tokens(company)))
        if not key:
            continue
        for target in wanted:
            if key == target:
                return True
            # One name contained in the other, as a whole-word set.
            left, right = set(key.split()), set(target.split())
            if left and right and (left <= right or right <= left):
                return True
    return False


def filter_candidates(candidates: list, subject_companies, subject_name=None,
                      say=None) -> tuple:
    """Drop endorsers, existing colleagues and anything that is not a person.

    Returns (kept, dropped_with_reasons).
    """
    kept, dropped = [], []
    for candidate in candidates or []:
        name = candidate.get("name")
        reason = None
        if not looks_like_person(name):
            reason = "not a personal name"
        elif is_media_role(candidate.get("roles")):
            reason = "an endorsement role, not a position in the industry"
        elif works_with_subject(candidate, subject_companies):
            reason = "already works at one of the subject's own companies"
        if reason:
            dropped.append({"name": name, "reason": reason})
            continue
        kept.append(candidate)

    if say and dropped:
        say(f"    {len(dropped)} candidate(s) dropped: "
            + "; ".join(f"{d['name']} ({d['reason']})" for d in dropped[:3])
            + ("..." if len(dropped) > 3 else ""))
    return kept, dropped
