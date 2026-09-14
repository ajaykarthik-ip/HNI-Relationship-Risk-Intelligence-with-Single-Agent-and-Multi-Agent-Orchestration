"""Shared text and date primitives for the quality layer.

Small on purpose. These are the two things every other module in here needs --
"are these two descriptions about the same thing" and "how far apart are these
two dates" -- and having one implementation means the clustering, the merge
pass and the attribution check cannot quietly disagree about either.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

# Words that carry no evidence about *which* event is being described. Kept
# deliberately generic: legal and reporting filler, not a domain vocabulary.
STOPWORDS = frozenset("""
a an the and or but of in on at to for from by with without into over under
is are was were be been being has have had do does did will would shall should
can could may might must its it he she they them his her their our your
that this these those there here as if then than so such other another
said says reported reportedly according alleged allegedly after before during
new news latest update updates case matter issue report story article
crore lakh million billion rs inr usd percent pc
""".split())

TOKEN = re.compile(r"[a-z0-9]+")

# ISO-ish prefixes the extraction returns: yyyy, yyyy-mm, yyyy-mm-dd.
ISO = re.compile(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?")


def tokens(text: str | None) -> set:
    """Content words, lowercased, stopwords removed.

    Numbers are kept: a figure like "2929" is often the single most
    discriminating token in a financial matter, and dropping it merges two
    unrelated cases at the same company.
    """
    if not text:
        return set()
    return {
        token for token in TOKEN.findall(text.lower())
        if token not in STOPWORDS and len(token) > 2
    }


def similarity(left: str | None, right: str | None) -> float:
    """Jaccard overlap of content words, 0.0 to 1.0.

    Deliberately not embeddings. Two descriptions of one event share proper
    nouns, amounts and agency names; set overlap captures that, costs nothing,
    needs no key, and gives the same answer on every run -- which a clustering
    key has to.
    """
    a, b = tokens(left), tokens(right)
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    if not intersection:
        return 0.0
    return intersection / len(a | b)


def containment(left: str | None, right: str | None) -> float:
    """How much of the *smaller* description is inside the larger one.

    A one-line summary and a detailed one about the same event score poorly on
    Jaccard purely because their lengths differ. Containment does not punish
    that, so the two are used together.
    """
    a, b = tokens(left), tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def parse_date(value) -> date | None:
    """A date from anything the pipeline carries, or None.

    Three formats arrive here: the extraction's ISO strings ("2026", "2026-02",
    "2026-02-25"), RSS pubDate ("Tue, 08 Sep 2026 11:24:15 GMT"), and None. V1
    only ever read a year out of these, which is why a raw RSS string could
    reach the report unparsed.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None

    # RSS first: it also contains four digits, so ISO parsing would half-match
    # it and silently produce the wrong day.
    if "," in text or text.endswith(("GMT", "UTC")) or " +" in text:
        try:
            parsed = parsedate_to_datetime(text)
            if parsed is not None:
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone(timezone.utc)
                return parsed.date()
        except (TypeError, ValueError, IndexError):
            pass

    match = ISO.search(text)
    if not match:
        return None
    year = int(match.group(1))
    if not 1900 <= year <= 2100:
        return None
    month = int(match.group(2) or 1)
    day = int(match.group(3) or 1)
    try:
        return date(year, min(max(month, 1), 12), min(max(day, 1), 28))
    except ValueError:
        return None


def days_apart(left, right) -> int | None:
    """Absolute distance in days, or None if either date is unknown."""
    a, b = parse_date(left), parse_date(right)
    if a is None or b is None:
        return None
    return abs((a - b).days)


def iso(value) -> str | None:
    """A date normalised to yyyy-mm-dd, or the original if unparseable.

    Used when a finding's date reaches the report, so a raw RSS pubDate never
    renders beside ISO ones.
    """
    parsed = parse_date(value)
    if parsed is not None:
        return parsed.isoformat()
    return value if value is None or isinstance(value, str) else None
