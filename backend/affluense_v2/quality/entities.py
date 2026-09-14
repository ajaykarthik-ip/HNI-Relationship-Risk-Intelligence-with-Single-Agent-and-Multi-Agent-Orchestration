"""A sector is not a company.

`pipeline._split_roles` accepts whatever string the extraction put in
`current_roles[].company`. There is no test that the string names an
organisation, so an industry -- "Bollywood", "fintech", "Indian cinema" --
becomes a screened entity. Fifteen adverse queries then run against an entire
sector, and whatever that returns is reported as the subject's corporate
footprint.

Two generic signals catch this without naming anything:

  structural   the same string appears as a *sector* somewhere in the same
               extraction set. The model was asked separately for a company and
               for the industry it operates in; when a value shows up on both
               sides, the model used it as a category. This needs no vocabulary
               at all and adapts to whatever domain a subject is in.

  lexical      the name, stripped of legal suffixes, is a bare collective or
               industry noun. The word list here is generic English -- industry,
               sector, market, cinema, sports -- not a list of companies.

What is deliberately NOT rejected: nonprofits, trusts, foundations, societies,
associations, institutes, universities and councils. Those are real
organisations. V1 already classifies them as non-exposure relationships in
`relationships.classify`, which is the right place to decide they do not carry
the subject's risk -- dropping them here would lose the affiliation entirely.

Rejected names are not discarded either. They go to `other_affiliations`, the
structure V1 already uses for roles that belong in the report but not in a list
a compliance reader reads as filings.
"""

from __future__ import annotations

import re

# Legal forms. A name carrying one of these is an organisation, full stop --
# this check runs before everything else and nothing below can override it.
LEGAL_FORM = re.compile(
    r"\b(ltd|limited|llp|llc|inc|incorporated|plc|pvt|private|corp|"
    r"corporation|co|company|gmbh|sa|nv|bv|ag|pte|holdings?|group|"
    r"industries|enterprises|ventures|partners|associates|technologies|"
    r"solutions|systems|labs|laboratories|works|mills|motors|bank|"
    r"foundation|trust|society|association|institute|university|college|"
    r"council|federation|academy|charity|ngo|club|fc|sc|afc)\b",
    re.IGNORECASE,
)

# Organisation forms that are emphatically real and must survive. Kept separate
# from LEGAL_FORM so the intent is explicit and testable: these are the ones a
# naive "is it a company?" filter would wrongly discard.
NON_PROFIT_FORM = re.compile(
    r"\b(foundation|trust|society|association|institute|university|college|"
    r"council|federation|academy|charity|ngo|mission|fund)\b",
    re.IGNORECASE,
)

# Bare collective nouns. These describe a field of activity, never a filed
# entity. Generic English, deliberately not domain names.
COLLECTIVE_NOUNS = frozenset("""
industry industries sector sectors market markets economy business businesses
trade commerce cinema film films movie movies television tv radio media
entertainment music sports sport athletics politics government governance
technology tech fintech edtech healthtech agritech biotech
finance banking insurance realty estate infrastructure manufacturing retail
hospitality aviation telecom telecommunications pharma pharmaceuticals
energy power mining agriculture education healthcare fashion beauty
startup startups ecommerce advertising marketing consulting
public private sector world global national international
""".split())

# Patterns that describe a category rather than an entity, whatever the words.
CATEGORY_SHAPE = re.compile(
    r"^(the\s+)?[a-z\s]*\b(industry|sector|market|business|world|scene|"
    r"circuit|community|ecosystem|space)\b\s*$",
    re.IGNORECASE,
)

SUFFIX = re.compile(r"[^a-z0-9\s]", re.IGNORECASE)


def _normalise(name: str) -> str:
    return SUFFIX.sub(" ", (name or "").lower()).strip()


def _content_words(name: str) -> list:
    return [word for word in _normalise(name).split() if word]


def sector_vocabulary(records: list) -> set:
    """Every string the extraction used as a *sector* anywhere in this run.

    This is the structural signal, and it is what makes the filter adapt to
    domains nobody anticipated: whatever a model decides to call an industry,
    it gets treated as one when it also turns up as a company name.
    """
    vocabulary: set = set()
    for record in records or []:
        for sector in (record.get("sectors") or []):
            if isinstance(sector, str) and sector.strip():
                vocabulary.add(_normalise(sector))
    return vocabulary


def classify(name: str, roles=None, sectors: set | None = None) -> tuple:
    """(is_organisation, reason). Structural checks first, vocabulary second."""
    raw = (name or "").strip()
    if not raw:
        return False, "no name given"

    normalised = _normalise(raw)
    words = _content_words(raw)
    if not words:
        return False, "no usable name"

    # A stated legal or organisational form settles it. Nothing below may
    # overturn this, which is what keeps foundations and clubs in.
    if LEGAL_FORM.search(raw):
        return True, "carries a legal or organisational form"

    # Structural: the extraction itself used this string as an industry.
    if sectors and normalised in sectors:
        return False, "used as a sector elsewhere in this run, so it is a category"

    # A phrase that names a field of activity.
    if CATEGORY_SHAPE.match(normalised):
        return False, "names a field of activity rather than an organisation"

    # A bare collective noun, alone or with only collective nouns beside it
    # ("indian cinema", "global fintech").
    if all(word in COLLECTIVE_NOUNS for word in words):
        return False, "a collective noun for an industry, not an entity"

    return True, "no signal that this is a category"


def is_organisation(name: str, roles=None, sectors: set | None = None) -> bool:
    return classify(name, roles, sectors)[0]


def is_nonprofit(name: str) -> bool:
    """Real organisations that a naive company filter would wrongly drop."""
    return bool(NON_PROFIT_FORM.search(name or ""))


def partition(companies: list, say=None) -> tuple:
    """Split extracted companies into (organisations, rejected).

    Rejected records are handed back so the caller can put them in
    `other_affiliations` rather than losing them. Each carries the reason it
    was moved, because a filter that cannot explain itself is one nobody will
    trust the next time it is wrong.
    """
    vocabulary = sector_vocabulary(companies)
    organisations, rejected = [], []

    for record in companies or []:
        name = record.get("canonical_name") or record.get("name") or ""
        ok, reason = classify(name, record.get("relationships"), vocabulary)
        if ok:
            organisations.append(record)
            continue
        moved = dict(record)
        moved["excluded_reason"] = reason
        moved["link_basis"] = (
            f"Named in page text as '{name}', but {reason}. Kept as an "
            "affiliation rather than screened as a company."
        )
        rejected.append(moved)
        if say:
            say(f"    '{name}' is not screened as a company: {reason}")

    return organisations, rejected
