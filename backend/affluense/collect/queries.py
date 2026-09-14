"""What to search for.

The pipeline used to ask one question per company -- the company's name in
quotes -- and nothing else. Adverse events were found only when they happened
to rank inside generic coverage, which for a well-funded company they rarely
do. A screening of Ashneer Grover returned 0 negative and 14 positive articles
about BharatPe, because the single query that was asked returns what the
company publishes rather than what journalists publish.

So this module asks the adverse question directly, and asks it many ways.

Everything here is a template. No model writes a query, no query depends on
what an earlier query returned, and the same subject always produces the same
search set -- which is what makes a "nothing found" result mean something.

Two ordering rules matter:

  adverse first     the run may be truncated by a cap or a timeout, and the
                    query that must survive truncation is the one looking for
                    the thing we would be negligent to miss.

  base query kept   removing it would bias the evidence set so far toward
                    adverse coverage that the tone breakdown stops describing
                    the company. Risk and sentiment are separate axes and both
                    have to be fed.
"""

from __future__ import annotations

from .. import config

# Grouped by the kind of matter, because the groups are what a reviewer checks
# off: "did you look for enforcement action?" is a question with an answer.
# Terms inside a group are OR-ed into one query, so each group costs one
# request per feed rather than one per word.
ADVERSE_GROUPS: dict = {
    "fraud": ["fraud", "scam", "embezzlement", "misappropriation", "forgery"],
    "money_laundering": ["money laundering", "hawala", "benami", "round tripping"],
    "corruption": ["bribery", "corruption", "kickback", "conflict of interest"],
    "litigation": ["lawsuit", "sued", "litigation", "court case", "class action"],
    "regulatory": ["SEBI", "RBI", "regulatory action", "penalty", "show cause",
                   "violation", "non-compliance"],
    "enforcement": ["Enforcement Directorate", "CBI", "raid", "summons",
                    "enforcement action"],
    "investigation": ["investigation", "probe", "inquiry", "under scrutiny"],
    "criminal": ["arrest", "FIR", "chargesheet", "charges", "indicted",
                 "convicted", "custody"],
    "insolvency": ["insolvency", "bankruptcy", "NCLT", "liquidation",
                   "loan default", "wilful defaulter"],
    "tax": ["tax evasion", "income tax raid", "tax notice", "tax demand"],
    "accounting": ["accounting irregularities", "restatement", "auditor resigned",
                   "qualified opinion"],
    "governance": ["governance concerns", "board resignation", "whistleblower",
                   "related party transactions"],
    "sanctions": ["sanctions", "blacklisted", "debarred", "banned"],
    "controversy": ["controversy", "allegations", "misconduct", "row"],
}

# Groups worth asking about a *person* as well as their companies. A company
# is investigated; a person is arrested, charged or accused. Asking every
# group of both doubles the request count for little return.
PERSON_GROUPS = (
    "fraud", "money_laundering", "corruption", "litigation",
    "enforcement", "investigation", "criminal", "tax", "controversy",
)


def _or_clause(terms: list) -> str:
    """Terms as one OR group, quoted so multi-word phrases stay phrases."""
    return " OR ".join(f'"{t}"' if " " in t else t for t in terms)


def adverse_queries(subject: str, groups=None, extra: str | None = None) -> list:
    """One query per adverse group, for a company or a person.

    Each is [{"query": ..., "group": ...}]. The group travels with the query
    so a finding can later say which check produced it, and so the report can
    state which checks were run and came back empty -- the difference between
    "no adverse coverage" and "we did not look".
    """
    wanted = groups or list(ADVERSE_GROUPS)
    anchor = f'"{subject}"' + (f' "{extra}"' if extra else "")
    return [
        {"query": f"{anchor} ({_or_clause(ADVERSE_GROUPS[group])})", "group": group}
        for group in wanted
        if group in ADVERSE_GROUPS
    ]


def for_company(name: str, cap: int | None = None) -> list:
    """The full query set for one company: adverse groups, then the base.

    Adverse first so a cap truncates the general coverage rather than the
    check for enforcement action.
    """
    queries = adverse_queries(name)
    queries.append({"query": f'"{name}"', "group": "general"})
    limit = config.MAX_QUERIES_PER_ENTITY if cap is None else cap
    return queries[:limit] if limit else queries


def for_person(name: str, company: str | None = None, cap: int | None = None) -> list:
    """Query set for the individual.

    The supplied company is appended to each adverse query as a disambiguator:
    a common name paired with an adverse term otherwise returns coverage of
    whoever shares it.
    """
    queries = adverse_queries(name, groups=PERSON_GROUPS, extra=company)
    queries.append({"query": f'"{name}"', "group": "general"})
    limit = config.MAX_QUERIES_PER_ENTITY if cap is None else cap
    return queries[:limit] if limit else queries


def groups_checked(queries: list) -> list:
    """Which adverse checks a query set actually ran.

    Reported in the output so a clean result can name what was searched. A
    LOW verdict that cannot list its checks is an assertion, not a finding.
    """
    return sorted({q["group"] for q in queries if q["group"] != "general"})
