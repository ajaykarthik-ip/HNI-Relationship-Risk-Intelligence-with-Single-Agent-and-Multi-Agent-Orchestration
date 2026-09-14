#!/usr/bin/env python3
"""
hni_lookup.py — collect public information about a named individual.

Given a person's name, this queries a set of free, no-key, publicly documented
endpoints and writes everything it finds to a single JSON file. Every fact
carries the URL it came from, so nothing in the output is unattributable.

Sources used (all free, no API key, all intended for programmatic access):

  Wikipedia REST + Action API   biography, summary, article text
  Wikidata Action API           structured claims (employer, positions, net worth)
  Wikidata Query Service        companies founded/led, co-officers at those companies
  DuckDuckGo Instant Answer     official public API, abstract + related topics
  Google News RSS               news headlines
  Bing News RSS                 news headlines
  GDELT DOC 2.0 API             global news index, free and public
  OpenCorporates v0.4           company officer search (best effort without a token)

Deliberately NOT used: LinkedIn (its terms forbid automated collection, and
there is no public people-search API), and scraping of search-engine result
pages, which their robots.txt disallows. Where the script does fetch an
ordinary web page, it reads that site's robots.txt first and obeys it.

Usage
    python -m venv .venv
    .venv\\Scripts\\activate          (Windows)      source .venv/bin/activate  (macOS/Linux)
    pip install requests beautifulsoup4

    python hni_lookup.py "Ratan Tata"
    python hni_lookup.py "Ratan Tata" --out tata.json --fetch-pages --max-news 60

The output is a report, not a verdict. Treat every news item as a claim made by
a publisher, not as an established fact about the person.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from xml.etree import ElementTree

try:
    import requests
except ImportError:  # pragma: no cover - guidance is more useful than a traceback
    sys.exit("Missing dependency. Run: pip install requests beautifulsoup4")

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency. Run: pip install requests beautifulsoup4")


# Statuses that mean "slow down", not "go away".
RETRY_STATUS = {202, 429, 502, 503, 504}

USER_AGENT = (
    "hni-lookup/1.0 (public-information research script; "
    "contact: set-your-email-here) python-requests"
)

# Terms that mark a headline as worth a human reading. These are *signals*, not
# findings: "probe" in a headline says a story exists, nothing more.
RISK_TERMS = {
    "fraud": [r"\bfraud", r"\bponzi\b", r"\bembezzl", r"\bsiphon", r"\bforgery\b",
              r"\bmisappropriat"],
    "scam": [r"\bscam", r"\bcheating\b", r"\bduped\b", r"\bswindl"],
    "litigation": [r"\blawsuit", r"\bsued\b", r"\bsues\b", r"\btribunal\b",
                   r"\blitigation\b", r"\bcourt\b", r"\bverdict\b", r"\bpetition\b"],
    "regulatory": [r"\bsebi\b", r"\brbi\b", r"\bregulator", r"\bpenalt",
                   r"\bfined\b", r"\bshow cause\b", r"\bsanction"],
    "investigation": [r"\bprobe[sd]?\b", r"\binvestigat", r"\braid(s|ed)?\b",
                      r"\bsummons\b", r"\benforcement directorate\b", r"\bcbi\b",
                      r"\bincome tax (raid|probe|notice|department)"],
    "insolvency": [r"\binsolvenc", r"\bbankrupt", r"\bnclt\b", r"\bliquidation\b",
                   r"\bdefault(s|ed)?\b", r"\bwound up\b"],
    "arrest": [r"\barrest", r"\bcustody\b", r"\bcharge ?sheet\b", r"\bindict",
               r"\bconvict"],
}

# Compiled once; these run over every headline from every source.
RISK_PATTERNS = {
    category: [re.compile(term, re.IGNORECASE) for term in terms]
    for category, terms in RISK_TERMS.items()
}

# Wikidata properties read off the person's own entity.
PERSON_CLAIMS = {
    "P106": "occupation",
    "P108": "employer",
    "P39": "position held",
    "P1830": "owner of",
    "P69": "educated at",
    "P27": "country of citizenship",
    "P937": "work location",
    "P2218": "net worth",
    "P26": "spouse",
    "P22": "father",
    "P25": "mother",
    "P1038": "relative",
    "P3373": "sibling",
    "P463": "member of",
}

# Properties on an *organisation* that point back at a person.
ORG_TO_PERSON = {
    "P112": "founder",
    "P169": "chief executive officer",
    "P488": "chairperson",
    "P1037": "director / manager",
    "P3320": "board member",
    "P127": "owned by",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fact(value, source: str, url: str, **extra) -> dict:
    """Every value in the report travels with the place it came from."""
    record = {"value": value, "source": source, "source_url": url}
    record.update(extra)
    return record


@dataclass
class Fetcher:
    """HTTP with a polite delay, a real user agent, and robots.txt awareness."""

    delay: float = 1.0
    timeout: int = 25
    session: requests.Session = field(default_factory=requests.Session)
    notes: list = field(default_factory=list)
    _robots: dict = field(default_factory=dict)
    _last_call: float = 0.0

    def __post_init__(self) -> None:
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en"})

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_call = time.monotonic()

    def get(self, url: str, attempts: int = 3, **kwargs):
        """GET with backoff. Several of these endpoints throttle politely
        rather than failing, so one 429 is not a reason to drop a source."""
        timeout = kwargs.pop("timeout", self.timeout)
        for attempt in range(1, attempts + 1):
            self._wait()
            try:
                response = self.session.get(url, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                if attempt == attempts:
                    self.note(f"Request failed for {url}: {exc}")
                    return None
                time.sleep(self.delay * 2 * attempt)
                continue

            if response.status_code == 200:
                return response
            if response.status_code in RETRY_STATUS and attempt < attempts:
                time.sleep(self.delay * 3 * attempt)
                continue
            self.note(f"HTTP {response.status_code} from {url}")
            return None
        return None

    def get_json(self, url: str, **kwargs):
        response = self.get(url, **kwargs)
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            self.note(f"Response from {url} was not JSON")
            return None

    def post_json(self, url: str, payload: dict, headers: dict, attempts: int = 3):
        """POST with the same backoff policy as GET. Used for Firecrawl."""
        for attempt in range(1, attempts + 1):
            self._wait()
            try:
                response = self.session.post(
                    url, json=payload, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                if attempt == attempts:
                    self.note(f"Request failed for {url}: {exc}")
                    return None
                time.sleep(self.delay * 2 * attempt)
                continue

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    self.note(f"Response from {url} was not JSON")
                    return None
            if response.status_code in RETRY_STATUS and attempt < attempts:
                time.sleep(self.delay * 3 * attempt)
                continue
            # Never echo the response body: it can repeat the API key back.
            self.note(f"HTTP {response.status_code} from {url}")
            return None
        return None

    def robots_allow(self, url: str) -> bool:
        """Ask the site whether this script may read the page."""
        parts = urllib.parse.urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots.get(root)
        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(f"{root}/robots.txt")
            try:
                parser.read()
            except Exception:
                # No reachable robots.txt: stay conservative and skip the site.
                self.note(f"Could not read robots.txt for {root}; skipping its pages")
                self._robots[root] = False
                return False
            self._robots[root] = parser
        if parser is False:
            return False
        return parser.can_fetch(USER_AGENT, url)

    def note(self, message: str) -> None:
        self.notes.append(message)


# --------------------------------------------------------------------------
# Wikipedia
# --------------------------------------------------------------------------

def wikipedia(fetcher: Fetcher, name: str) -> dict:
    api = "https://en.wikipedia.org/w/api.php"
    search = fetcher.get_json(
        api,
        params={
            "action": "query",
            "list": "search",
            "srsearch": name,
            "srlimit": 5,
            "format": "json",
        },
    )
    if not search:
        return {}

    hits = search.get("query", {}).get("search", [])
    if not hits:
        fetcher.note(f"No Wikipedia article matched '{name}'")
        return {}

    title = hits[0]["title"]
    page_url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"

    summary = fetcher.get_json(
        f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title.replace(' ', '_'))}"
    ) or {}

    extract = fetcher.get_json(
        api,
        params={
            "action": "query",
            "prop": "extracts",
            "explaintext": 1,
            "titles": title,
            "format": "json",
        },
    ) or {}
    pages = extract.get("query", {}).get("pages", {})
    body = next(iter(pages.values()), {}).get("extract", "") if pages else ""

    return {
        "title": fact(title, "Wikipedia", page_url),
        "description": fact(summary.get("description"), "Wikipedia", page_url),
        "summary": fact(summary.get("extract"), "Wikipedia", page_url),
        "article_text": fact(body[:20000], "Wikipedia", page_url, truncated=len(body) > 20000),
        "other_matches": [
            fact(
                hit["title"],
                "Wikipedia",
                f"https://en.wikipedia.org/wiki/{urllib.parse.quote(hit['title'].replace(' ', '_'))}",
            )
            for hit in hits[1:]
        ],
    }


# --------------------------------------------------------------------------
# Wikidata
# --------------------------------------------------------------------------

def wikidata_find_person(fetcher: Fetcher, name: str) -> tuple[str | None, list]:
    """Return the best-matching human entity and every candidate considered."""
    result = fetcher.get_json(
        "https://www.wikidata.org/w/api.php",
        params={
            "action": "wbsearchentities",
            "search": name,
            "language": "en",
            "type": "item",
            "limit": 10,
            "format": "json",
        },
    )
    if not result:
        return None, []

    candidates = []
    for entry in result.get("search", []):
        candidates.append(
            {
                "id": entry["id"],
                "label": entry.get("label"),
                "description": entry.get("description"),
                "source_url": f"https://www.wikidata.org/wiki/{entry['id']}",
            }
        )

    # Prefer an entity that is actually a human; disambiguation is the whole
    # game here, so record why the pick was made rather than trusting rank 1.
    for candidate in candidates:
        entity = fetcher.get_json(
            f"https://www.wikidata.org/wiki/Special:EntityData/{candidate['id']}.json"
        )
        if not entity:
            continue
        claims = entity["entities"][candidate["id"]].get("claims", {})
        instance_of = [
            c["mainsnak"].get("datavalue", {}).get("value", {}).get("id")
            for c in claims.get("P31", [])
        ]
        if "Q5" in instance_of:
            candidate["is_human"] = True
            return candidate["id"], candidates
        candidate["is_human"] = False

    return None, candidates


def resolve_labels(fetcher: Fetcher, qids: list) -> dict:
    """Turn Q-numbers into readable names, 50 at a time."""
    labels: dict = {}
    unique = [q for q in dict.fromkeys(qids) if q]
    for start in range(0, len(unique), 50):
        batch = unique[start : start + 50]
        data = fetcher.get_json(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbgetentities",
                "ids": "|".join(batch),
                "props": "labels",
                "languages": "en",
                "format": "json",
            },
        )
        if not data:
            continue
        for qid, entity in data.get("entities", {}).items():
            labels[qid] = entity.get("labels", {}).get("en", {}).get("value", qid)
    return labels


def _snak_value(snak: dict):
    """Flatten the handful of Wikidata value shapes this script cares about."""
    datavalue = snak.get("datavalue", {})
    kind, value = datavalue.get("type"), datavalue.get("value")
    if kind == "wikibase-entityid":
        return {"qid": value.get("id")}
    if kind == "time":
        return {"time": value.get("time", "").lstrip("+")[:10]}
    if kind == "quantity":
        return {"amount": value.get("amount"), "unit": value.get("unit", "").rsplit("/", 1)[-1]}
    if kind == "monolingualtext":
        return {"text": value.get("text")}
    return {"text": value} if isinstance(value, str) else {"raw": value}


def wikidata_claims(fetcher: Fetcher, qid: str) -> dict:
    entity_url = f"https://www.wikidata.org/wiki/{qid}"
    data = fetcher.get_json(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json")
    if not data:
        return {}

    claims = data["entities"][qid].get("claims", {})
    collected: dict = {}
    pending_qids: list = []

    for prop, label in PERSON_CLAIMS.items():
        entries = []
        for statement in claims.get(prop, []):
            value = _snak_value(statement["mainsnak"])
            qualifiers = statement.get("qualifiers", {})
            start = qualifiers.get("P580", [{}])[0].get("datavalue", {}).get("value", {})
            end = qualifiers.get("P582", [{}])[0].get("datavalue", {}).get("value", {})
            of_org = qualifiers.get("P642", [{}])[0].get("datavalue", {}).get("value", {})
            entry = {
                "raw": value,
                "start": (start or {}).get("time", "").lstrip("+")[:10] or None,
                "end": (end or {}).get("time", "").lstrip("+")[:10] or None,
                "of": (of_org or {}).get("id"),
            }
            entries.append(entry)
            for key in ("qid",):
                if value.get(key):
                    pending_qids.append(value[key])
            if entry["of"]:
                pending_qids.append(entry["of"])
        if entries:
            collected[label] = entries

    labels = resolve_labels(fetcher, pending_qids)

    readable: dict = {}
    for label, entries in collected.items():
        rendered = []
        for entry in entries:
            raw = entry["raw"]
            if "qid" in raw:
                text = labels.get(raw["qid"], raw["qid"])
                url = f"https://www.wikidata.org/wiki/{raw['qid']}"
            elif "amount" in raw:
                text = f"{raw['amount']} ({labels.get(raw['unit'], raw['unit'])})"
                url = entity_url
            else:
                text = raw.get("time") or raw.get("text") or str(raw)
                url = entity_url
            rendered.append(
                fact(
                    text,
                    "Wikidata",
                    url,
                    start=entry["start"],
                    end=entry["end"],
                    of=labels.get(entry["of"]) if entry["of"] else None,
                )
            )
        readable[label] = rendered

    return readable


def sparql(fetcher: Fetcher, query: str) -> list:
    """Run a query against the public Wikidata Query Service."""
    data = fetcher.get_json(
        "https://query.wikidata.org/sparql",
        params={"query": query, "format": "json"},
        headers={"Accept": "application/sparql-results+json"},
        timeout=75,
    )
    if not data:
        fetcher.note("Wikidata Query Service returned nothing (it rate-limits aggressively)")
        return []
    return data.get("results", {}).get("bindings", [])


def companies_from_sparql(fetcher: Fetcher, qid: str) -> list:
    """Organisations that name this person as founder, CEO, chair, or director."""
    unions = "\n    UNION\n".join(
        f'    {{ ?org wdt:{prop} wd:{qid} . BIND("{role}" AS ?relation) }}'
        for prop, role in ORG_TO_PERSON.items()
    )
    query = f"""
SELECT DISTINCT ?org ?orgLabel ?relation ?inception ?countryLabel ?industryLabel WHERE {{
{unions}
  OPTIONAL {{ ?org wdt:P571 ?inception . }}
  OPTIONAL {{ ?org wdt:P17 ?country . }}
  OPTIONAL {{ ?org wdt:P452 ?industry . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT 200
"""
    rows = sparql(fetcher, query)

    # One row per company. The query returns a row per OPTIONAL combination,
    # so the same company arrives several times with different industries.
    merged: dict = {}
    for row in rows:
        org_qid = row.get("org", {}).get("value", "").rsplit("/", 1)[-1]
        if not org_qid:
            continue
        company = merged.setdefault(
            org_qid,
            {
                "name": row.get("orgLabel", {}).get("value"),
                "wikidata_id": org_qid,
                "relationships": [],
                "inception": None,
                "country": None,
                "industries": [],
                "source": "Wikidata Query Service",
                "source_url": f"https://www.wikidata.org/wiki/{org_qid}",
            },
        )
        relation = row.get("relation", {}).get("value")
        if relation and relation not in company["relationships"]:
            company["relationships"].append(relation)
        industry = row.get("industryLabel", {}).get("value")
        if industry and industry not in company["industries"]:
            company["industries"].append(industry)
        company["inception"] = company["inception"] or (
            (row.get("inception", {}).get("value") or "")[:10] or None
        )
        company["country"] = company["country"] or row.get("countryLabel", {}).get("value")

    return list(merged.values())


def connections_from_sparql(fetcher: Fetcher, qid: str, org_qids: list) -> list:
    """Other people holding an officer role at the same organisations.

    Run as one small query per property rather than a single UNION of all of
    them: the public query service times out on the combined form.
    """
    if not org_qids:
        return []

    values = " ".join(f"wd:{q}" for q in org_qids[:25])
    people: list = []
    seen: set = set()

    for prop, role in ORG_TO_PERSON.items():
        if prop == "P127":  # "owned by" is often a company, not a person
            continue
        query = f"""
SELECT DISTINCT ?person ?personLabel ?orgLabel WHERE {{
  VALUES ?org {{ {values} }}
  ?org wdt:{prop} ?person .
  FILTER(?person != wd:{qid})
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT 100
"""
        for row in sparql(fetcher, query):
            person_qid = row.get("person", {}).get("value", "").rsplit("/", 1)[-1]
            org_name = row.get("orgLabel", {}).get("value")
            if (person_qid, org_name) in seen:
                continue
            seen.add((person_qid, org_name))
            people.append(
                {
                    "name": row.get("personLabel", {}).get("value"),
                    "wikidata_id": person_qid,
                    "relationship": f"{role} at {org_name}",
                    "via": org_name,
                    "source": "Wikidata Query Service",
                    "source_url": f"https://www.wikidata.org/wiki/{person_qid}",
                }
            )

    return people


# --------------------------------------------------------------------------
# News and open web
# --------------------------------------------------------------------------

def classify_risk(text: str) -> list:
    """Keyword signals on a headline. Marks a story as worth reading; it is
    never evidence on its own, and the caller must not treat it as a finding."""
    haystack = text or ""
    return sorted(
        category
        for category, patterns in RISK_PATTERNS.items()
        if any(pattern.search(haystack) for pattern in patterns)
    )


def google_news(fetcher: Fetcher, name: str, limit: int) -> list:
    url = (
        "https://news.google.com/rss/search?q="
        + urllib.parse.quote(f'"{name}"')
        + "&hl=en-IN&gl=IN&ceid=IN:en"
    )
    response = fetcher.get(url)
    if response is None:
        return []
    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError:
        fetcher.note("Google News RSS did not parse")
        return []

    items = []
    for item in list(root.iterfind(".//item"))[:limit]:
        title = (item.findtext("title") or "").strip()
        items.append(
            {
                "headline": title,
                "publisher": (item.findtext("source") or "").strip() or None,
                "published": (item.findtext("pubDate") or "").strip() or None,
                "url": (item.findtext("link") or "").strip(),
                "url_is_redirect": True,  # Google News links resolve via their own domain
                "risk_signals": classify_risk(title),
                "source": "Google News RSS",
                "source_url": url,
            }
        )
    return items


def bing_news(fetcher: Fetcher, name: str, limit: int) -> list:
    url = "https://www.bing.com/news/search?q=" + urllib.parse.quote(f'"{name}"') + "&format=RSS"
    response = fetcher.get(url)
    if response is None:
        return []
    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError:
        fetcher.note("Bing News RSS did not parse")
        return []

    items = []
    for item in list(root.iterfind(".//item"))[:limit]:
        title = (item.findtext("title") or "").strip()
        items.append(
            {
                "headline": title,
                "publisher": None,
                "published": (item.findtext("pubDate") or "").strip() or None,
                "url": (item.findtext("link") or "").strip(),
                "url_is_redirect": True,  # Bing routes clicks via apiclick.aspx
                "snippet": (item.findtext("description") or "").strip() or None,
                "risk_signals": classify_risk(title),
                "source": "Bing News RSS",
                "source_url": url,
            }
        )
    return items


def gdelt_news(fetcher: Fetcher, name: str, limit: int) -> list:
    """GDELT indexes global news and is free to query without a key."""
    params = {
        "query": f'"{name}"',
        "mode": "artlist",
        "maxrecords": min(limit, 250),
        "format": "json",
        "sort": "datedesc",
    }
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    data = fetcher.get_json(url, params=params)
    if not data:
        return []

    items = []
    for article in data.get("articles", [])[:limit]:
        title = article.get("title", "")
        items.append(
            {
                "headline": title,
                "publisher": article.get("domain"),
                "published": article.get("seendate"),
                "url": article.get("url"),
                "language": article.get("language"),
                "country": article.get("sourcecountry"),
                "risk_signals": classify_risk(title),
                "source": "GDELT DOC 2.0",
                "source_url": f"{url}?{urllib.parse.urlencode(params)}",
            }
        )
    return items


def duckduckgo_instant(fetcher: Fetcher, name: str) -> dict:
    """DuckDuckGo's documented Instant Answer API — not its HTML results page."""
    params = {"q": name, "format": "json", "no_html": 1, "skip_disambig": 1}
    url = "https://api.duckduckgo.com/"
    data = fetcher.get_json(url, params=params)
    if not data:
        return {}

    query_url = f"{url}?{urllib.parse.urlencode(params)}"
    related = []
    for topic in data.get("RelatedTopics", []):
        if "Text" in topic and topic.get("FirstURL"):
            related.append(fact(topic["Text"], "DuckDuckGo", topic["FirstURL"]))

    return {
        "abstract": fact(data.get("AbstractText") or None, "DuckDuckGo", data.get("AbstractURL") or query_url),
        "abstract_source": data.get("AbstractSource"),
        "related_topics": related[:25],
    }


def opencorporates_officers(fetcher: Fetcher, name: str) -> list:
    """Company officer records. Works without a token at a low rate limit; if
    the endpoint now demands one, the failure is recorded rather than hidden."""
    url = "https://api.opencorporates.com/v0.4/officers/search"
    params = {"q": name, "per_page": 30}
    data = fetcher.get_json(url, params=params)
    if not data:
        fetcher.note(
            "OpenCorporates returned no data. Unauthenticated access is now "
            "restricted; a free API token from opencorporates.com would enable it."
        )
        return []

    officers = []
    for entry in data.get("results", {}).get("officers", []):
        officer = entry.get("officer", {})
        company = officer.get("company", {}) or {}
        officers.append(
            {
                "officer_name": officer.get("name"),
                "position": officer.get("position"),
                "company_name": company.get("name"),
                "company_number": company.get("company_number"),
                "jurisdiction": company.get("jurisdiction_code"),
                "start_date": officer.get("start_date"),
                "end_date": officer.get("end_date"),
                "source": "OpenCorporates",
                "source_url": officer.get("opencorporates_url")
                or f"{url}?{urllib.parse.urlencode(params)}",
            }
        )
    return officers


def read_page(fetcher: Fetcher, url: str, name: str) -> dict | None:
    """Pull the sentences that actually mention the person, robots.txt permitting."""
    if not fetcher.robots_allow(url):
        fetcher.note(f"robots.txt disallows fetching {url}")
        return None

    response = fetcher.get(url)
    if response is None:
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" "))

    surname = name.split()[-1]
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", text)
        if surname.lower() in sentence.lower()
    ]
    if not sentences:
        return None

    # Aggregator links bounce through their own domain; the resolved URL is the
    # real provenance, so record that rather than the link that was followed.
    final_url = response.url
    return {
        "title": (soup.title.string or "").strip() if soup.title else None,
        "mentions": sentences[:12],
        "source": urllib.parse.urlsplit(final_url).netloc,
        "source_url": final_url,
        "requested_url": url if final_url != url else None,
    }


# --------------------------------------------------------------------------
# Firecrawl (optional)
#
# Everything above works without any key. Firecrawl is the one keyed service
# here, and it is strictly additive: it supplies real web search, clean page
# text, and schema-guided extraction. With no key set, none of this runs.
# --------------------------------------------------------------------------

FIRECRAWL_BASE = "https://api.firecrawl.dev"

# Asks for facts the page actually states. The "do not infer" instruction
# matters: an extraction model will otherwise fill these fields from its own
# memory of a well-known person, and that invented content would arrive
# wearing a real source URL.
PROFILE_PROMPT = (
    "Extract only facts about {name} that this page explicitly states. "
    "If the page does not state something, omit that field entirely. "
    "Do not infer, guess, or use outside knowledge. If the page is not about "
    "{name}, return an empty object."
)

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "full_name": {"type": "string"},
        "current_roles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "role": {"type": "string"},
                    "period": {"type": "string"},
                },
                "required": ["company"],
            },
        },
        "past_roles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "role": {"type": "string"},
                    "period": {"type": "string"},
                },
                "required": ["company"],
            },
        },
        "investments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "stake_or_amount": {"type": "string"},
                    "year": {"type": "string"},
                },
                "required": ["entity"],
            },
        },
        "associates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "relationship": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        "net_worth": {"type": "string"},
        "education": {"type": "array", "items": {"type": "string"}},
        "notable_events": {"type": "array", "items": {"type": "string"}},
    },
}


# Login-walled or JS-only. A scrape here returns 403 and costs a credit for
# nothing; on the first Kohli run these burned half the page budget.
SKIP_DOMAINS = {
    "instagram.com", "facebook.com", "x.com", "twitter.com", "linkedin.com",
    "youtube.com", "youtu.be", "tiktok.com", "pinterest.com", "threads.net",
    "reddit.com", "quora.com", "whatsapp.com", "t.me",
}

# Where credits have actually paid off. Registry mirrors first: these carry
# Director Identification Numbers and filing-backed company lists.
PREFERRED_DOMAINS = [
    "zaubacorp.com", "falconebiz.com", "tofler.in", "indiafilings.com",
    "quickcompany.in", "opencorporates.com", "crunchbase.com",
    "bloomberg.com", "reuters.com", "forbes.com", "moneycontrol.com",
    "economictimes.indiatimes.com", "business-standard.com", "livemint.com",
    "thehindubusinessline.com", "financialexpress.com",
]


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")


def is_skipped(url: str) -> bool:
    host = host_of(url)
    return any(host == d or host.endswith("." + d) for d in SKIP_DOMAINS)


def scrape_priority(url: str) -> tuple:
    """Lower sorts first. Registry and business sources outrank general pages."""
    host = host_of(url)
    for index, domain in enumerate(PREFERRED_DOMAINS):
        if host == domain or host.endswith("." + domain):
            return (0, index)
    return (1, 0)


def rank_for_scraping(results: list) -> tuple:
    """Drop the walled domains, then put the pages worth paying for first."""
    keep, skipped = [], []
    for item in results:
        (skipped if is_skipped(item["source_url"]) else keep).append(item)
    # Stable sort: within a tier, the search engine's own ordering survives.
    return sorted(keep, key=lambda item: scrape_priority(item["source_url"])), skipped


class Firecrawl:
    """Thin client over Firecrawl's search and scrape endpoints.

    Firecrawl moved from v1 to v2 and changed both the request and response
    shapes. Rather than pin a version, this tries v2 and falls back to v1 on
    the first call, then remembers which one answered.
    """

    def __init__(self, api_key: str, fetcher: Fetcher):
        self.fetcher = fetcher
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.version: str | None = None
        self.credits_note: list = []

    def _call(self, path: str, payload_by_version: dict):
        versions = [self.version] if self.version else ["v2", "v1"]
        for version in versions:
            url = f"{FIRECRAWL_BASE}/{version}/{path}"
            data = self.fetcher.post_json(url, payload_by_version[version], self.headers)
            if data is not None:
                self.version = version
                return data
        return None

    def search(self, query: str, limit: int = 8) -> list:
        """Web search. This is the capability the free sources could not provide."""
        data = self._call(
            "search",
            {
                "v2": {"query": query, "limit": limit},
                "v1": {"query": query, "limit": limit},
            },
        )
        if not data:
            return []

        raw = data.get("data")
        # v1 returns a flat list; v2 returns results grouped by category.
        if isinstance(raw, dict):
            results = raw.get("web") or raw.get("results") or []
        elif isinstance(raw, list):
            results = raw
        else:
            results = []

        found = []
        for item in results:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            found.append(
                {
                    "title": item.get("title"),
                    "snippet": item.get("description") or item.get("snippet"),
                    "query": query,
                    "source": "Firecrawl search",
                    "source_url": item["url"],
                }
            )
        return found

    def scrape(self, url: str, name: str, structured: bool = False) -> dict | None:
        """Fetch one page as markdown, optionally with schema-guided extraction."""
        prompt = PROFILE_PROMPT.format(name=name)
        if structured:
            payload_by_version = {
                "v2": {
                    "url": url,
                    "onlyMainContent": True,
                    "formats": [
                        "markdown",
                        {"type": "json", "schema": PROFILE_SCHEMA, "prompt": prompt},
                    ],
                },
                "v1": {
                    "url": url,
                    "onlyMainContent": True,
                    "formats": ["markdown", "json"],
                    "jsonOptions": {"schema": PROFILE_SCHEMA, "prompt": prompt},
                },
            }
        else:
            payload_by_version = {
                "v2": {"url": url, "onlyMainContent": True, "formats": ["markdown"]},
                "v1": {"url": url, "onlyMainContent": True, "formats": ["markdown"]},
            }

        data = self._call("scrape", payload_by_version)
        if not data:
            return None

        body = data.get("data") or {}
        markdown = body.get("markdown") or ""
        metadata = body.get("metadata") or {}
        extracted = body.get("json") or body.get("extract") or None

        return {
            "title": metadata.get("title"),
            "markdown": markdown[:20000],
            "markdown_truncated": len(markdown) > 20000,
            "extracted": extracted,
            "source": urllib.parse.urlsplit(url).netloc,
            "source_url": metadata.get("sourceURL") or url,
        }


def firecrawl_pass(firecrawl: Firecrawl, name: str, args) -> dict:
    """Search a few targeted angles, then read the best pages properly."""
    queries = [
        f'"{name}" founder OR chairman OR director company',
        f'"{name}" investments OR stake OR shareholding',
        f'"{name}" net worth profile biography',
        f'"{name}" lawsuit OR fraud OR investigation OR probe',
    ][: args.firecrawl_queries]

    results: list = []
    for query in queries:
        print(f"    search: {query[:58]}", flush=True)
        results.extend(firecrawl.search(query, limit=args.firecrawl_limit))

    # De-duplicate by URL, keeping the first query that surfaced each page.
    seen: set = set()
    unique = []
    for item in results:
        if item["source_url"] in seen:
            continue
        seen.add(item["source_url"])
        unique.append(item)

    ranked, skipped = rank_for_scraping(unique)
    if skipped:
        print(
            f"    skipping {len(skipped)} login-walled result(s) "
            f"(no credit spent)",
            flush=True,
        )

    pages = []
    for item in ranked[: args.firecrawl_pages]:
        print(f"    read:   {item['source_url'][:62]}", flush=True)
        page = firecrawl.scrape(item["source_url"], name, structured=args.firecrawl_extract)
        if page:
            page["found_via"] = item["query"]
            pages.append(page)

    return {
        "search_results": unique,
        "pages": pages,
        "skipped_domains": [item["source_url"] for item in skipped],
        "api_version": firecrawl.version,
    }


# Playing for a team or fronting a brand is not a directorship. These belong
# in the report, but not in a list a compliance reader treats as filings.
CORPORATE_INTEREST = re.compile(
    r"\b(shareholder|stakeholder|owner|co-owner|investor|partner|director|"
    r"founder|co-founder|promoter|chairman|chairperson|proprietor|board)\b",
    re.IGNORECASE,
)

NON_CORPORATE_ROLE = re.compile(
    r"\b(cricketer|footballer|player|captain|athlete|sportsperson|batsman|bowler|"
    r"brand ambassador|ambassador|endorser|spokesperson|face of|mentor|patron)\b",
    re.IGNORECASE,
)


def companies_from_extraction(pages: list) -> tuple:
    """Fold extracted roles into company records.

    Returns (companies, other_affiliations). Both are kept clearly separate
    from the registry-backed rows: these came from a model reading a web page,
    so they are marked as such and carry the URL they were read from.
    """
    companies: dict = {}
    other: dict = {}

    for page in pages:
        extracted = page.get("extracted") or {}
        if not isinstance(extracted, dict):
            continue
        for key, status in (("current_roles", "current"), ("past_roles", "former")):
            for entry in extracted.get(key) or []:
                if not isinstance(entry, dict):
                    continue
                company_name = (entry.get("company") or "").strip()
                if not company_name:
                    continue
                role = (entry.get("role") or "").strip()

                # A mixed role like "Shareholder/Ambassador" carries a real
                # financial interest, so an ownership word outranks the
                # endorsement word and keeps the row with the companies.
                non_corporate = bool(NON_CORPORATE_ROLE.search(role)) and not (
                    CORPORATE_INTEREST.search(role)
                )
                bucket = other if non_corporate else companies
                record = bucket.setdefault(
                    company_name.lower(),
                    {
                        "name": company_name,
                        "relationships": [],
                        "status": status,
                        "period": entry.get("period"),
                        "evidence": "Extracted from page text by Firecrawl, not a registry record",
                        "source": "Firecrawl extraction",
                        "source_url": page["source_url"],
                    },
                )
                if role and role not in record["relationships"]:
                    record["relationships"].append(role)

    return list(companies.values()), list(other.values())


def investments_from_extraction(pages: list) -> list:
    """Lift investments out of the per-page payloads into one attributed list.

    Both problem statements ask for investments explicitly, so they belong at
    the top level rather than buried inside a page's extraction blob.
    """
    found: dict = {}
    for page in pages:
        extracted = page.get("extracted") or {}
        if not isinstance(extracted, dict):
            continue
        for entry in extracted.get("investments") or []:
            if not isinstance(entry, dict):
                continue
            entity = (entry.get("entity") or "").strip()
            if not entity:
                continue
            record = found.setdefault(
                entity.lower(),
                {
                    "entity": entity,
                    "stake_or_amount": entry.get("stake_or_amount") or None,
                    "year": entry.get("year") or None,
                    "source": "Firecrawl extraction",
                    "source_url": page["source_url"],
                },
            )
            # Prefer a page that actually stated a figure over one that did not.
            if not record["stake_or_amount"] and entry.get("stake_or_amount"):
                record["stake_or_amount"] = entry["stake_or_amount"]
                record["source_url"] = page["source_url"]
    return list(found.values())



# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def dedupe_news(items: list) -> list:
    seen, unique = set(), []
    for item in items:
        key = re.sub(r"[^a-z0-9]+", "", (item.get("headline") or "").lower())[:90]
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def collect(name: str, args) -> dict:
    fetcher = Fetcher(delay=args.delay)

    report = {
        "query": {
            "name": name,
            "collected_at": utc_now(),
            "script": "hni_lookup.py",
        },
        "identity": {},
        "wikidata": {},
        "companies": [],
        "connections": [],
        "officer_records": [],
        "news": [],
        "web": {},
        "pages_read": [],
        "counts": {},
        "notes": [],
        "disclaimer": (
            "Aggregated from public sources. Items are claims made by their "
            "publishers, not verified findings about the named individual. "
            "Names can collide, so check the match note before relying on any row. "
            "Connections are not filtered by time period: a company's historical "
            "founder can appear alongside its current directors. Risk signals are "
            "keyword matches on headlines and mark a story worth reading, nothing more."
        ),
    }

    print(f"  Wikipedia ...", flush=True)
    report["identity"]["wikipedia"] = wikipedia(fetcher, name)

    print(f"  Wikidata ...", flush=True)
    qid, candidates = wikidata_find_person(fetcher, name)
    report["identity"]["wikidata_candidates"] = candidates
    report["identity"]["wikidata_id"] = qid
    report["identity"]["match_note"] = (
        f"Matched to {qid} because it is the highest-ranked result that is a human (P31=Q5)."
        if qid
        else "No human entity matched on Wikidata; company and connection data will be thin."
    )

    if qid:
        report["wikidata"] = wikidata_claims(fetcher, qid)
        print(f"  Wikidata Query Service: companies ...", flush=True)
        report["companies"] = companies_from_sparql(fetcher, qid)
        org_qids = [c["wikidata_id"] for c in report["companies"] if c.get("wikidata_id")]
        print(f"  Wikidata Query Service: co-officers ...", flush=True)
        report["connections"] = connections_from_sparql(fetcher, qid, org_qids)

        # Family and membership ties are connections too, from a different property.
        for label in ("spouse", "father", "mother", "sibling", "relative", "member of"):
            for entry in report["wikidata"].get(label, []):
                report["connections"].append(
                    {
                        "name": entry["value"],
                        "relationship": label,
                        "via": "Wikidata claim",
                        "source": entry["source"],
                        "source_url": entry["source_url"],
                    }
                )

    print(f"  OpenCorporates ...", flush=True)
    report["officer_records"] = opencorporates_officers(fetcher, name)

    print(f"  DuckDuckGo Instant Answer ...", flush=True)
    report["web"]["duckduckgo"] = duckduckgo_instant(fetcher, name)

    print(f"  News: Google, Bing, GDELT ...", flush=True)
    news = []
    news += google_news(fetcher, name, args.max_news)
    news += bing_news(fetcher, name, args.max_news)
    news += gdelt_news(fetcher, name, args.max_news)
    report["news"] = dedupe_news(news)

    if args.firecrawl_key:
        print("  Firecrawl: web search and page extraction ...", flush=True)
        client = Firecrawl(args.firecrawl_key, fetcher)
        report["firecrawl"] = firecrawl_pass(client, name, args)
        report["web"]["search_results"] = report["firecrawl"]["search_results"]

        if args.firecrawl_extract:
            pages = report["firecrawl"]["pages"]
            extracted, other = companies_from_extraction(pages)
            known = {c["name"].lower() for c in report["companies"]}
            new_companies = [c for c in extracted if c["name"].lower() not in known]
            report["companies"].extend(new_companies)
            report["companies_from_extraction"] = len(new_companies)
            report["other_affiliations"] = other
            report["investments"] = investments_from_extraction(pages)

            # Everything the schema pulled, kept whole and attributed.
            report["extracted_profile"] = [
                {
                    "source_url": page["source_url"],
                    "found_via": page.get("found_via"),
                    "data": page["extracted"],
                }
                for page in report["firecrawl"]["pages"]
                if page.get("extracted")
            ]
    else:
        report["notes_setup"] = (
            "Firecrawl not used. Set FIRECRAWL_API_KEY to add web search and "
            "page extraction; everything else runs without any key."
        )

    if args.fetch_pages:
        print(f"  Reading top pages ...", flush=True)
        # Google News links resolve through their own domain, which robots.txt
        # disallows; take direct publisher URLs first.
        direct = [
            item["url"]
            for item in report["news"]
            if item.get("url") and not item.get("url_is_redirect")
        ]
        targets = direct[: args.max_pages]
        if not targets:
            fetcher.note("No directly fetchable article URLs were available")
        for url in targets:
            page = read_page(fetcher, url, name)
            if page:
                report["pages_read"].append(page)

    flagged = [item for item in report["news"] if item["risk_signals"]]
    report["counts"] = {
        "companies": len(report["companies"]),
        "connections": len(report["connections"]),
        "officer_records": len(report["officer_records"]),
        "news_items": len(report["news"]),
        "news_with_risk_signals": len(flagged),
        "pages_read": len(report["pages_read"]),
        "firecrawl_search_results": len(report.get("web", {}).get("search_results", [])),
        "firecrawl_pages": len(report.get("firecrawl", {}).get("pages", [])),
        "firecrawl_pages_skipped": len(report.get("firecrawl", {}).get("skipped_domains", [])),
        "investments": len(report.get("investments", [])),
        "other_affiliations": len(report.get("other_affiliations", [])),
    }
    report["risk_signal_summary"] = {
        category: sum(1 for item in flagged if category in item["risk_signals"])
        for category in RISK_TERMS
    }
    report["notes"] = fetcher.notes
    return report


def summarise(report: dict) -> None:
    counts = report["counts"]
    name = report["query"]["name"]
    print()
    print(f"  {name}")
    wiki = report["identity"].get("wikipedia", {}).get("description", {})
    if wiki.get("value"):
        print(f"  {wiki['value']}")
    print()
    for key, value in counts.items():
        print(f"    {value:>5}  {key.replace('_', ' ')}")

    if report.get("companies_from_extraction"):
        print(f"\n    {report['companies_from_extraction']} of those companies came "
              f"from page extraction, not a registry.")

    if report.get("investments"):
        print()
        print("    Investments found:")
        for item in report["investments"][:12]:
            amount = f" ({item['stake_or_amount']})" if item.get("stake_or_amount") else ""
            print(f"      {item['entity']}{amount}")

    if report.get("other_affiliations"):
        print()
        print("    Other affiliations (not directorships):")
        for item in report["other_affiliations"][:8]:
            print(f"      {item['name']} — {', '.join(item['relationships'])}")

    signals = {k: v for k, v in report.get("risk_signal_summary", {}).items() if v}
    if signals:
        print()
        print("    Headlines carrying risk terms (a reading list, not a finding):")
        for category, count in sorted(signals.items(), key=lambda kv: -kv[1]):
            print(f"      {count:>3}  {category}")

    if report["companies"]:
        print()
        print("    Companies found:")
        for company in report["companies"][:12]:
            roles = ", ".join(company.get("relationships", [])) or "linked"
            print(f"      {company['name']} — {roles}")

    if report["notes"]:
        print()
        print("    Notes:")
        for note in report["notes"][:10]:
            print(f"      - {note}")


def load_env_file() -> None:
    """Read KEY=VALUE lines from a .env beside this script.

    Values already present in the real environment win, so an explicitly
    exported key overrides the file.
    """
    env_path = pathlib.Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


def main() -> None:
    load_env_file()

    parser = argparse.ArgumentParser(
        description="Collect public information about a named individual into a JSON file."
    )
    parser.add_argument("name", help='Full name, e.g. "Ratan Tata"')
    parser.add_argument("--out", help="Output path (default: <slug>.json)")
    parser.add_argument("--max-news", type=int, default=40, help="Max items per news source")
    parser.add_argument(
        "--fetch-pages",
        action="store_true",
        help="Also open the top news URLs and extract sentences naming the person",
    )
    parser.add_argument("--max-pages", type=int, default=8, help="How many pages to open")
    parser.add_argument(
        "--delay", type=float, default=1.0, help="Seconds between requests (be polite)"
    )

    group = parser.add_argument_group(
        "Firecrawl (optional)",
        "Adds real web search and clean page extraction. Reads FIRECRAWL_API_KEY "
        "from the environment; everything else in this script needs no key.",
    )
    group.add_argument(
        "--firecrawl-key",
        default=os.environ.get("FIRECRAWL_API_KEY"),
        help="API key (default: FIRECRAWL_API_KEY environment variable)",
    )
    group.add_argument(
        "--no-firecrawl", action="store_true", help="Ignore the key and skip Firecrawl"
    )
    group.add_argument(
        "--firecrawl-queries", type=int, default=4, help="How many search angles (max 4)"
    )
    group.add_argument(
        "--firecrawl-limit", type=int, default=8, help="Results per search"
    )
    group.add_argument(
        "--firecrawl-pages", type=int, default=6, help="How many result pages to read"
    )
    group.add_argument(
        "--firecrawl-extract",
        action="store_true",
        help="Run schema-guided extraction on each page read (uses more credits)",
    )
    args = parser.parse_args()

    if args.no_firecrawl:
        args.firecrawl_key = None
    if args.firecrawl_key:
        print("  Firecrawl key detected; web search enabled.")

    print(f"\nCollecting public information on: {args.name}\n")
    report = collect(args.name, args)

    slug = re.sub(r"[^a-z0-9]+", "-", args.name.lower()).strip("-")
    out_path = args.out or f"{slug}.json"
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)

    summarise(report)
    print(f"\n  Written to {out_path}\n")


if __name__ == "__main__":
    main()
