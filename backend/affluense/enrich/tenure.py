"""When did they hold the role, and when was the story published?

Uday Kotak became non-executive chairman of IL&FS in 2018, appointed by the
government to lead the clean-up of a fraud that had already happened. The
screening attached IL&FS's fraud, scam and investigation flags to him. Every
one of those articles predates the day he walked in.

Reporting a predecessor's fraud as the subject's exposure is the kind of
error that ends a contract, and no amount of sentiment tuning catches it: the
articles are real, adverse, and genuinely about the company. Only the dates
separate them.

So where a role's start date is known, articles published before it are
marked. They stay in the evidence -- the company's history is still worth
seeing -- but they cannot raise a flag against the person.

Known limit: start dates come from the period text a page states, such as
"2018-present". Wikidata carries them as statement qualifiers that the
current SPARQL queries do not request, so a role found only in Wikidata has
no date and is treated as undated. Undated means "do not filter", never
"assume it is current" -- guessing here would silently drop real findings.
"""

from __future__ import annotations

import email.utils
import re
from datetime import datetime, timezone

# A four-digit year in a period string: "2018-present", "since 2018",
# "1991 to 2012", "Chairman (2018-2021)".
YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")

# Phrases that mean the role is still held, so the end year is open.
CURRENT = re.compile(r"\b(present|current|ongoing|incumbent|till date|to date)\b",
                     re.IGNORECASE)


def article_year(published: str | None) -> int | None:
    """The year an article was published, from an RSS date string.

    RSS dates are RFC 2822 ("Mon, 12 Feb 2024 10:30:00 GMT"). Anything that
    does not parse returns None, and an article with no date is never
    filtered -- an unparsed date must not become a reason to discard
    evidence.
    """
    if not published:
        return None

    try:
        parsed = email.utils.parsedate_to_datetime(published)
    except (TypeError, ValueError, IndexError):
        parsed = None

    if parsed is not None:
        return parsed.year

    # ISO-ish fallback, for sources that do not follow RFC 2822.
    match = YEAR.search(published)
    if match:
        year = int(match.group(1))
        if 1900 <= year <= datetime.now(timezone.utc).year + 1:
            return year
    return None


def _year_in(value) -> int | None:
    """The year inside an ISO date or a bare year. None for anything else."""
    if not value:
        return None
    match = YEAR.search(str(value))
    return int(match.group(1)) if match else None


def role_period(record: dict) -> tuple:
    """(start_year, end_year) for the subject's role at this company.

    Either may be None. A single year with "present" alongside it is a start;
    a single year alone is ambiguous and is therefore treated as a start
    only, because the alternative -- treating it as an end date -- would
    filter out everything after it.
    """
    # Wikidata's qualifiers are the better source when present: they are
    # structured dates rather than a phrase a page happened to print.
    structured_start = _year_in(record.get("role_start"))
    structured_end = _year_in(record.get("role_end"))
    if structured_start or structured_end:
        return structured_start, structured_end

    text = " ".join(filter(None, [
        str(record.get("period") or ""),
        str(record.get("role_period") or ""),
    ])).strip()
    if not text:
        return None, None

    years = [int(y) for y in YEAR.findall(text)]
    if not years:
        return None, None

    start = min(years)
    if CURRENT.search(text) or len(years) == 1:
        return start, None
    return start, max(years)


def mark_out_of_tenure(articles: list, record: dict) -> int:
    """Flag articles published outside the subject's time at the company.

    Returns how many were marked. Marking, not removing: the article is still
    shown as the company's coverage, and the reader can see both the story and
    the reason it is not attributed.
    """
    start, end = role_period(record)
    if start is None and end is None:
        return 0

    marked = 0
    for article in articles:
        year = article_year(article.published)
        if year is None:
            continue
        if start is not None and year < start:
            article.outside_tenure = True
            article.tenure_note = (
                f"published {year}, before the subject's role began in {start}"
            )
            marked += 1
        elif end is not None and year > end:
            article.outside_tenure = True
            article.tenure_note = (
                f"published {year}, after the subject's role ended in {end}"
            )
            marked += 1
    return marked


def attributable_articles(articles: list) -> list:
    """The articles that may raise a flag against the person."""
    return [a for a in articles if not getattr(a, "outside_tenure", False)]
