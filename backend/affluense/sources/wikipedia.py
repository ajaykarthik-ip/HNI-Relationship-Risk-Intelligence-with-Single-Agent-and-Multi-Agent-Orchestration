"""Wikipedia: biography text and a fuzzy-matching name resolver.

Wikipedia's search tolerates misspellings; Wikidata's does not. That makes
this the right place to correct a query name before anything else runs.
"""

from __future__ import annotations

import difflib
import re
import urllib.parse

from .. import config


def _article_url(title: str) -> str:
    return f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"


def search_titles(fetcher, query: str, limit: int = 5) -> list:
    data = fetcher.get_json(
        config.WIKIPEDIA_API,
        params={
            "action": "query", "list": "search", "srsearch": query,
            "srlimit": limit, "format": "json",
        },
    )
    if not data:
        return []
    return [hit["title"] for hit in data.get("query", {}).get("search", [])]


# A candidate whose name barely resembles the query is not the subject, no
# matter what else its article mentions. Searching "Aravind Srinivas
# Perplexity" returned Andy Konwinski — similarity 0.47 — and the company
# bonus pushed him past everyone, producing a confident report about the
# wrong human.
NAME_SIMILARITY_FLOOR = 0.55

# Wikipedia descriptions of things that cannot be the subject. "Aravind
# Srinivas" matched "Aravind 2", whose own description reads "2013 Indian
# film", and the run attached a criminal case to a real person's name.
NON_PERSON = re.compile(
    r"\b(film|movie|album|song|single|soundtrack|novel|book|manga|comic|"
    r"building|skyscraper|temple|palace|residence|village|town|city|district|"
    r"state|river|mountain|island|species|genus|plant|animal|"
    r"video game|tv series|television series|web series|band|play|"
    r"magazine|newspaper|award|festival|dynasty|language|software|"
    r"company|corporation|organisation|organization|university|college)\b",
    re.IGNORECASE,
)


def looks_like_a_person(summary: dict) -> bool:
    """Reject an article that describes a thing rather than a human."""
    description = (summary.get("description") or "").strip()
    extract = (summary.get("extract") or "")[:200]
    if description and NON_PERSON.search(description):
        return False
    # Wikipedia marks disambiguation and list pages in its summary type.
    if summary.get("type") not in (None, "standard"):
        return False
    return not NON_PERSON.search(extract[:80])


def _summary(fetcher, title: str) -> dict:
    quoted = urllib.parse.quote(title.replace(" ", "_"))
    return fetcher.get_json(f"{config.WIKIPEDIA_REST}{quoted}") or {}


def summary(fetcher, title: str) -> dict:
    """Public alias. Candidate discovery needs the same cheap summary."""
    return _summary(fetcher, title)


def choose_title(fetcher, titles: list, name: str, company: str | None) -> tuple:
    """Pick the article that is actually about the person asked for.

    Three rules, each from a real failure:

      similarity floor  a candidate must actually resemble the query name.
                        Without it the company bonus alone promoted a
                        different person to 95% confidence.
      person check      the article must describe a human, not a film.
      company verifies  the supplied company breaks ties between plausible
                        candidates; it never rescues an implausible one.

    Returns (None, reason, 0.0) when nothing qualifies. Naming nobody is the
    correct answer far more often than naming the closest available match.
    """
    if not titles:
        return None, "no article matched the name", 0.0

    scored = []
    for index, title in enumerate(titles):
        similarity = difflib.SequenceMatcher(None, title.lower(), name.lower()).ratio()
        if similarity < NAME_SIMILARITY_FLOOR:
            continue
        # Search order breaks exact ties only.
        scored.append([title, similarity, similarity - index * 0.001, ""])

    if not scored:
        return (
            None,
            f"no article had a name close enough to '{name}' "
            f"(closest: {titles[0]!r})",
            0.0,
        )

    scored.sort(key=lambda row: row[2], reverse=True)

    # Drop candidates that are not people, cheapest-first.
    people = []
    for row in scored[:4]:
        summary = _summary(fetcher, row[0])
        if looks_like_a_person(summary):
            row.append(summary)
            people.append(row)
    if not people:
        return None, f"the closest article, {scored[0][0]!r}, is not about a person", 0.0

    # The company breaks ties among candidates that already qualify.
    if company and len(people) > 1 and people[0][2] - people[1][2] < 0.12:
        for row in people[:3]:
            summary = row[4]
            text = " ".join(
                filter(None, [summary.get("extract", ""), summary.get("description", "")])
            ).lower()
            if company.lower() in text:
                row[2] += 0.30
                row[3] = f"the article names the supplied company '{company}'"
        people.sort(key=lambda row: row[2], reverse=True)

    title, similarity, _score, reason = people[0][:4]
    basis = f"closest title match to '{name}' (similarity {similarity:.2f})"
    if reason:
        basis += f", and {reason}"
    return title, basis, min(1.0, similarity)


def lookup(fetcher, name: str, company: str | None = None) -> dict:
    """Biography for the article that best matches the person asked for."""
    titles = search_titles(fetcher, name)
    if company:
        extra = search_titles(fetcher, f"{name} {company}")
        titles += [t for t in extra if t not in titles]

    if not titles:
        fetcher.note(f"No Wikipedia article matched '{name}'")
        return {}

    title, basis, score = choose_title(fetcher, titles, name, company)
    if title is None:
        fetcher.note(f"No Wikipedia article identified for '{name}': {basis}")
        return {"unmatched": True, "selection_basis": basis, "candidates": titles[:5]}

    page_url = _article_url(title)

    summary = _summary(fetcher, title)
    extract = fetcher.get_json(
        config.WIKIPEDIA_API,
        params={
            "action": "query", "prop": "extracts", "explaintext": 1,
            "titles": title, "format": "json",
        },
    ) or {}
    pages = extract.get("query", {}).get("pages", {})
    body = next(iter(pages.values()), {}).get("extract", "") if pages else ""

    return {
        "title": title,
        "selection_basis": basis,
        "selection_score": round(score, 3),
        "description": summary.get("description"),
        "summary": summary.get("extract"),
        "article_text": body[:20000],
        "article_truncated": len(body) > 20000,
        "other_matches": [
            {"title": t, "source_url": _article_url(t)} for t in titles if t != title
        ],
        "source": "Wikipedia",
        "source_url": page_url,
    }
