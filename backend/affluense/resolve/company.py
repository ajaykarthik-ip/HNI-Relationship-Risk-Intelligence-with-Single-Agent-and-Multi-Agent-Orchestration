"""Company name normalisation and merging.

The same company arrives spelled differently from every source:

    "Vault by Virat Kohli"  /  "Vault"  /  "Vault fitness chain"
    "Blue Tribe Foods"      /  "Blue Tribe"
    "Digit"                 /  "Go Digit"

Left alone these inflate the company count, which is precisely how a
screening tool misleads. Normalising to a comparison key lets them merge
while every original spelling is kept as an alias.
"""

from __future__ import annotations

import re

# Legal forms and filing noise, stripped for comparison only.
LEGAL_SUFFIXES = [
    "private limited", "pvt ltd", "pvt. ltd.", "pvt limited", "public limited",
    "limited", "ltd", "llp", "llc", "inc", "incorporated", "corporation",
    "corp", "company", "co", "plc", "gmbh", "pte", "sa", "nv", "bv", "ag",
    "holdings", "group", "ventures", "enterprises", "foundation", "trust",
]

# Generic descriptors a page may append to a brand name.
DESCRIPTORS = [
    "chain", "app", "foods", "food", "coffee", "brand", "brands", "labs",
    "technologies", "technology", "solutions", "services", "industries",
    "sports", "fitness", "retail", "capital", "partners", "management",
]

PREFIXES = ["the", "go", "my", "get"]

# Words that join a name together without identifying anything. Dropped from
# the comparison key so "Adani Ports and Special Economic Zone" and
# "Adani Ports & SEZ" reconcile once the ampersand and the acronym are
# expanded.
CONNECTORS = {"and", "of", "for", "the", "a", "an"}

# Acronyms that appear in filed company names, expanded before comparison.
# Curated like NAME_ALIASES and for the same reason: a general acronym
# expander would decide that "SA" means "South Africa" inside a French
# company name. Only forms seen in real filed names belong here.
ABBREVIATIONS = {
    "sez": "special economic zone",
    "psu": "public sector undertaking",
    "nbfc": "non banking financial",
    "amc": "asset management",
    "intl": "international",
    "natl": "national",
    "mfg": "manufacturing",
    "constr": "construction",
    "indus": "industries",
    "infra": "infrastructure",
    "tech": "technologies",
    "auto": "automobiles",
    "fin": "financial",
}

# Words a longer variant of the same name may add without naming a different
# company. "Adani Ports" and "Adani Ports and Special Economic Zone" are one
# entity; "Tata Motors" and "Tata Motors Finance" are two, because "finance"
# names a distinct business. Only words that describe corporate *structure*
# belong here -- never a business line.
STRUCTURAL_TOKENS = {
    "special", "economic", "zone", "zones", "area", "areas",
    "india", "indian", "bharat", "international", "global", "worldwide",
    "national", "overseas", "asia", "europe", "america",
    "public", "private", "sector", "undertaking",
} | CONNECTORS

# Curated, not general. Normalisation cannot see that SLB is Schlumberger
# renamed, or that HCL stands for Hindustan Computers — both appeared as
# duplicate rows in real reports. Only pairs that are genuinely the same
# entity belong here.
NAME_ALIASES = {
    "slb": "schlumberger",
    "hindustan computers": "hcl",
    "hcltech": "hcl",
    "hcl tech": "hcl",
    "hindustan computer": "hcl",
    "perplexity ai": "perplexity",
    "uc berkeley": "university california berkeley",
    "university california berkeley": "university california berkeley",
    "meta platforms": "meta",
    "alphabet inc": "alphabet",
    "x corp": "twitter",
}


def normalise(name: str, owner_name: str | None = None) -> str:
    """Reduce a company name to a comparison key.

    The owner's name is stripped too, so "Vault by Virat Kohli" and "Vault"
    reconcile.
    """
    if not name:
        return ""

    text = name.lower().strip()

    if owner_name:
        for part in owner_name.lower().split():
            if len(part) > 2:
                text = re.sub(rf"\b{re.escape(part)}\b", " ", text)
        text = re.sub(r"\bby\b", " ", text)

    # "&" carries meaning and must survive punctuation stripping. Without
    # this, "Adani Ports & SEZ" became "adani ports sez" while the filed name
    # became "adani ports and special economic zone", and one company was
    # screened three times.
    text = text.replace("&", " and ")

    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    # Expand known acronyms, then drop the words that join a name without
    # identifying it.
    words = [ABBREVIATIONS.get(word, word) for word in text.split()]
    text = " ".join(words)
    text = " ".join(w for w in text.split() if w not in CONNECTORS)

    # Strip trailing legal forms and descriptors, longest first so
    # "private limited" goes before "limited".
    changed = True
    while changed:
        changed = False
        for suffix in sorted(LEGAL_SUFFIXES + DESCRIPTORS, key=len, reverse=True):
            if text.endswith(" " + suffix):
                text = text[: -(len(suffix) + 1)].strip()
                changed = True

    for prefix in PREFIXES:
        if text.startswith(prefix + " "):
            text = text[len(prefix) + 1 :].strip()

    text = re.sub(r"\s+", " ", text).strip()
    return NAME_ALIASES.get(text, text)


def _is_registry(record: dict) -> bool:
    source = (record.get("source") or "").lower()
    return "wikidata" in source


def _combine(records: list) -> dict:
    """Fold several records for one company into a single entity.

    A registry-backed record keeps its identity, because its name is the
    filed one. Every other spelling survives as an alias.
    """
    records = sorted(records, key=lambda r: (not _is_registry(r), -len(r.get("name") or "")))
    primary = dict(records[0])
    primary["canonical_name"] = primary.get("name")

    aliases, relationships, sectors, sources = [], [], [], []
    for record in records:
        name = record.get("name")
        if name and name != primary["canonical_name"] and name not in aliases:
            aliases.append(name)
        relationships += record.get("relationships", []) or []
        sectors += record.get("sectors", []) or []
        sources += record.get("sources", []) or ([record["source"]] if record.get("source") else [])
        for field in ("registry_id", "jurisdiction", "source_url", "link_basis", "period"):
            primary[field] = primary.get(field) or record.get(field)

    primary["aliases"] = sorted(aliases)
    primary["relationships"] = merge_roles(relationships)
    primary["sectors"] = sorted(set(sectors))
    primary["sources"] = sorted(set(sources))
    return primary


def merge(companies: list, owner_name: str | None = None) -> list:
    """Collapse records that describe the same company.

    Two passes. Names are grouped first, then a group is split only if it
    genuinely contains two filed companies — that is, two *different*
    registry ids. Keying on the id directly was wrong: a record carrying an
    id and one without it then failed to merge, and "Perplexity AI" appeared
    twice in the same report.
    """
    buckets: dict = {}
    for record in companies:
        key = normalise(record.get("name", ""), owner_name)
        if key:
            buckets.setdefault(key, []).append(record)

    merged = []
    for group in buckets.values():
        ids = {r["registry_id"] for r in group if r.get("registry_id")}

        if len(ids) <= 1:
            merged.append(_combine(group))
            continue

        # Genuinely distinct filed companies sharing a brand name, such as
        # Wipro and Wipro Enterprises. Records with no id join the one whose
        # name they match, otherwise the first.
        by_id = {registry_id: [] for registry_id in sorted(ids)}
        unidentified = []
        for record in group:
            if record.get("registry_id"):
                by_id[record["registry_id"]].append(record)
            else:
                unidentified.append(record)

        for record in unidentified:
            name = (record.get("name") or "").lower()
            target = next(
                (
                    registry_id
                    for registry_id, rows in by_id.items()
                    if any((r.get("name") or "").lower() == name for r in rows)
                ),
                next(iter(by_id)),
            )
            by_id[target].append(record)

        merged.extend(_combine(rows) for rows in by_id.values() if rows)

    return _absorb_longer_forms(merged, owner_name)


def _absorb_longer_forms(entities: list, owner_name: str | None) -> list:
    """Fold a short name into the longer filed name of the same company.

    "Adani Ports" and "Adani Ports and Special Economic Zone" survived name
    normalisation as two entities, and were screened twice against the same
    news. The short form is absorbed only when the longer name adds nothing
    that could name a different company: every extra word has to describe
    corporate structure rather than a line of business.

    That guard is the whole point. "Wipro" must not absorb into "Wipro
    Enterprises", and "Tata Motors" must not absorb into "Tata Motors
    Finance" -- both are separate filed companies, and merging them would
    attach one company's adverse news to another.
    """
    keyed = [
        (normalise(e.get("canonical_name") or e.get("name") or "", owner_name), e)
        for e in entities
    ]
    absorbed: set = set()

    for index, (short_key, short) in enumerate(keyed):
        if not short_key or index in absorbed:
            continue
        short_words = short_key.split()

        for other, (long_key, long_entity) in enumerate(keyed):
            if other == index or other in absorbed or not long_key:
                continue
            long_words = long_key.split()
            if len(long_words) <= len(short_words):
                continue
            if long_words[: len(short_words)] != short_words:
                continue

            # Two different registry ids mean two filed companies, whatever
            # the names look like.
            short_id = short.get("registry_id")
            long_id = long_entity.get("registry_id")
            if short_id and long_id and short_id != long_id:
                continue

            extra = set(long_words[len(short_words):])
            if not extra <= STRUCTURAL_TOKENS:
                continue

            merged_entity = _combine([long_entity, short])
            # _combine ranks by registry backing then name length, so the
            # filed name stays canonical and the short form becomes an alias.
            # It rebuilds aliases from the records' own names, so spellings
            # already folded in during the first pass have to be carried
            # across by hand or they are lost here.
            carried = set(long_entity.get("aliases") or [])
            carried |= set(short.get("aliases") or [])
            carried |= {short.get("canonical_name") or short.get("name") or ""}
            carried.discard(merged_entity.get("canonical_name"))
            carried.discard("")
            merged_entity["aliases"] = sorted(
                set(merged_entity.get("aliases") or []) | carried
            )
            keyed[other] = (long_key, merged_entity)
            absorbed.add(index)
            break

    return [entity for index, (_key, entity) in enumerate(keyed)
            if index not in absorbed]


ROLE_ABBREVIATIONS = {
    "md": "managing director",
    "ceo": "chief executive officer",
    "cfo": "chief financial officer",
    "cto": "chief technology officer",
    "coo": "chief operating officer",
    "cmd": "chairman and managing director",
    "mgr": "manager",
    # Gendered and neutral spellings of the same office.
    "chairman": "chair",
    "chairwoman": "chair",
    "chairperson": "chair",
    "chairmen": "chair",
}

# Dropped before comparison: grammar, and placeholders that carry no role.
ROLE_STOPWORDS = {"and", "of", "the", "a", "an", "at", "in", "various", "roles",
                  "role", "other", "misc", "general"}


def normalise_role(role: str) -> str:
    """Comparison key for a role string.

    Abbreviations expand to several words, so the expansion has to happen
    before tokens are counted. Order is discarded: "chairman and managing
    director" and "managing director and chairman" are one role.
    """
    if not role:
        return ""

    text = role.lower().replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)

    # Two passes: an abbreviation can expand into words that are themselves
    # mapped. "CMD" becomes "chairman and managing director", and "chairman"
    # then has to become "chair" like every other spelling of that office.
    words = []
    for word in text.split():
        for part in ROLE_ABBREVIATIONS.get(word, word).split():
            words.append(ROLE_ABBREVIATIONS.get(part, part))

    meaningful = sorted({w for w in words if w and w not in ROLE_STOPWORDS})
    return " ".join(meaningful)


def merge_roles(roles: list) -> list:
    """Collapse spellings, keeping the fullest phrasing of each role."""
    best: dict = {}
    for role in roles:
        key = normalise_role(role)
        if not key:
            continue
        current = best.get(key)
        if current is None or len(role) > len(current):
            best[key] = role
    return sorted(best.values())


# A past job and a directorship are not the same exposure. Reading a former
# employer's regulatory news as the subject's risk flagged Schlumberger under
# a fintech founder who left it years ago.
CONTROL_ROLE = re.compile(
    r"\b(founder|co-?founder|promoter|owner|co-?owner|proprietor|chair|"
    r"chairman|chairperson|director|managing director|partner|designated "
    r"partner|board|trustee|shareholder|stakeholder|chief executive|ceo|"
    r"cfo|coo|cto|president|managing partner|general partner)\b",
    re.IGNORECASE,
)

EMPLOYMENT_ROLE = re.compile(
    r"\b(engineer|researcher|research scientist|scientist|intern|analyst|"
    r"associate|consultant|instructor|lecturer|professor|developer|"
    r"employee|staff|manager|vp|vice president|head of|specialist|"
    r"wireline|technician|architect)\b",
    re.IGNORECASE,
)

# Non-profits and academic bodies. A seat on one is not a corporate holding,
# however senior the title. Ratan Tata sat on the board of the Carnegie
# Endowment for International Peace, and 25 articles of a Washington think
# tank's coverage were screened as part of his corporate footprint.
# Plurals are spelled out. "\btrust\b" does not match "Tata Trusts", which is
# why two of Ratan Tata's charities were screened as ordinary companies.
PHILANTHROPIC_NAME = re.compile(
    r"\b(foundations?|endowments?|trusts?|universit(?:y|ies)|colleges?|"
    r"schools?|institutes?|institutions?|academ(?:y|ies|ia)|"
    r"charit(?:y|ies|able)|philanthrop\w*|"
    r"hospitals?|societ(?:y|ies)|ngos?|non[- ]?profits?|not[- ]for[- ]profit|"
    r"think tanks?|council on|centre for|center for|"
    r"museums?|librar(?:y|ies)|observator(?:y|ies)|conservanc(?:y|ies)|"
    r"relief|humanitarian|welfare|aid)\b",
    re.IGNORECASE,
)

# A charitable word inside an ordinary company's name. "Trust Fintech Limited"
# is a listed company; "Apollo Hospitals Enterprise" sells healthcare. The
# philanthropic words appear anywhere in a name, so a commercial business line
# in the same name overrides them -- the same suppressor idea the risk
# taxonomy uses to stop a fraud-detection vendor being flagged for fraud.
COMMERCIAL_NAME = re.compile(
    r"\b(fintech|bank|banking|finance|financial|capital|securities|"
    r"insurance|broking|brokers?|asset management|mutual fund|"
    r"motors|automobiles?|industries|industrial|manufacturing|"
    r"technologies|technology|software|systems|digital|"
    r"pharma\w*|chemicals?|steel|cement|mining|petro\w*|refiner\w*|"
    r"energy|power|electric|utilities|ports?|logistics|shipping|"
    r"telecom\w*|communications?|media|entertainment|"
    r"retail|consumer|hotels?|resorts?|realty|estates?|infra\w*|"
    r"enterprises?|holdings?|ventures?|partners|"
    r"private limited|pvt|plc|incorporated)\b",
    re.IGNORECASE,
)


# A seat on a central bank board, a regulator or an industry body is a public
# appointment, not a corporate holding. Kumar Mangalam Birla sat on the RBI
# central board, and reading the RBI's regulatory news as *his* exposure made
# a central bank the only adverse finding in his report. Terms here have to be
# specific: "bank" alone would catch Bank of India, which is an ordinary
# commercial bank.
PUBLIC_BODY_NAME = re.compile(
    r"\b(reserve bank|central bank|federal reserve|monetary authority|"
    r"securities and exchange board|regulatory authority|"
    r"ministry|government|parliament|senate|"
    r"commission|bureau|niti aayog|planning board|"
    r"world bank|united nations|international monetary fund|"
    r"world economic forum|world health organization|"
    r"chamber of commerce|confederation of|federation of|"
    r"industry association|trade association|"
    r"advisory (?:board|panel|committee|council)|"
    r"task force|expert (?:group|committee|panel))\b",
    re.IGNORECASE,
)


def is_philanthropic(name: str) -> bool:
    """A charity or academic body, not a company that borrowed the word."""
    text = name or ""
    if not PHILANTHROPIC_NAME.search(text):
        return False
    return not COMMERCIAL_NAME.search(text)


def relationship_type(name: str, roles: list) -> str:
    """control | public_office | employment | philanthropy | unknown.

    A controlling role wins: someone can be both a founder and an engineer at
    their own company, and the directorship is what a screening cares about.
    """
    joined = " ".join(roles or [])
    # Checked before control, because the public bodies that appoint outside
    # directors hand them exactly the titles a control role is spelled with.
    if PUBLIC_BODY_NAME.search(name or ""):
        return "public_office"
    if CONTROL_ROLE.search(joined):
        return "philanthropy" if is_philanthropic(name) else "control"
    if is_philanthropic(name):
        return "philanthropy"
    if EMPLOYMENT_ROLE.search(joined):
        return "employment"
    return "unknown"
