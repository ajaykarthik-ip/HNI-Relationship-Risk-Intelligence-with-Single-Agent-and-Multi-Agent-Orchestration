"""Adverse-media detection: what kind of matter, and how far it has gone.

Two separate questions, deliberately kept apart from sentiment:

  category  what the story is about (fraud, litigation, regulatory, ...)
  stage     how far it has actually progressed (alleged ... convicted)

Stage is the part that matters legally. "Firm accused of fraud" and "firm
convicted of fraud" share a category and are not the same fact, and a tool
that renders both as a red FRAUD badge is a defamation claim waiting to
happen.
"""

from __future__ import annotations

import re
from collections import defaultdict

from ..models import Flag

# Word-boundary patterns throughout. Substring matching produced false
# positives in testing: "ed " for Enforcement Directorate matched "backed",
# "supported" and "expanded".
RISK_TERMS = {
    # "defraud" needs its own entry: \bfraud will not match inside it.
    "fraud": [r"\bfraud", r"\bdefraud", r"\bponzi\b", r"\bembezzl", r"\bsiphon",
              r"\bforgery\b", r"\bmisappropriat", r"\bbriber", r"\blaundering\b"],
    "scam": [r"\bscam", r"\bcheating\b", r"\bduped\b", r"\bswindl"],
    "litigation": [r"\blawsuit", r"\bsued\b", r"\bsues\b", r"\btribunal\b",
                   r"\blitigation\b", r"\bcourt\b", r"\bverdict\b",
                   r"\bpetition\b", r"\bclass action\b"],
    "regulatory": [r"\bsebi\b", r"\brbi\b", r"\bregulator", r"\bpenalt",
                   r"\bfined\b", r"\bshow cause\b", r"\bsanction",
                   r"\bviolation", r"\bnon-?compliance\b"],
    "investigation": [r"\bprobe[sd]?\b", r"\binvestigat", r"\braid(s|ed)?\b",
                      r"\bsummons\b", r"\benforcement directorate\b", r"\bcbi\b",
                      r"\bincome tax (raid|probe|notice|department)",
                      r"\bunder scrutiny\b"],
    "insolvency": [r"\binsolvenc", r"\bbankrupt", r"\bnclt\b",
                   r"\bliquidation\b", r"\bdefault(s|ed)?\b", r"\bwound up\b",
                   r"\bwinding up\b"],
    "arrest": [r"\barrest", r"\bcustody\b", r"\bcharge ?sheet\b", r"\bindict",
               r"\bconvict"],
}

# A fintech that sells fraud detection is not accused of fraud. The word is
# in the product name, and every payments company trips it.
CATEGORY_SUPPRESSORS = {
    "fraud": [
        r"\bfraud[- ](detection|prevention|protection|management|monitoring|"
        r"screening|solution|platform|tool|risk|check|analytics|shield|control)",
        r"\banti[- ]?fraud\b",
        r"\bfraud[- ]?tech\b",
        r"\bagainst fraud\b",
        r"\bcombat(ing)? fraud\b",
        r"\bfight(ing)? fraud\b",
    ],
    "scam": [
        r"\bscam (alert|awareness|prevention|protection|detection)\b",
        r"\bhow to (spot|avoid|report) (a )?scam",
    ],
    "investigation": [
        r"\binvestigative (journalism|report)\b",
        # "Investigation" as a product or a capability, not a proceeding:
        # forensic tooling, incident response, cyber clean rooms.
        r"\b(forensic|threat|incident|security|cyber|digital|malware)[- ]"
        r"(investigation|investigations)\b",
        r"\binvestigation (platform|tool|software|suite|capabilit|service)",
        r"\bclean room\b",
        r"\bunder investigation for (excellence|awards?)\b",
    ],
    "litigation": [
        r"\b(moot|mock) court\b",
        r"\bcourt (of|yard)\b",
    ],
}

COMPILED_SUPPRESSORS = {
    category: [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    for category, patterns in CATEGORY_SUPPRESSORS.items()
}

RISK_PATTERNS = {
    category: [re.compile(term, re.IGNORECASE) for term in terms]
    for category, terms in RISK_TERMS.items()
}

# Checked in order, most conclusive first. Dismissal outranks conviction so
# that "acquitted of all charges" is not read as a conviction.
STAGE_PATTERNS = [
    ("dismissed", [r"\bacquitted\b", r"\bexonerated\b", r"\bdismissed\b",
                   r"\bquashed\b", r"\bclean chit\b", r"\bcleared of\b",
                   r"\bdropped\b"]),
    ("convicted", [r"\bconvicted\b", r"\bfound guilty\b", r"\bsentenced\b"]),
    ("charged", [r"\bcharge ?sheet", r"\bindicted\b", r"\bcharged with\b",
                 r"\barrested\b", r"\bprosecut"]),
    ("settled", [r"\bsettle[ds]?\b", r"\bsettlement\b", r"\bpaid the penalty\b",
                 r"\bdisposed of\b", r"\bconsent order\b"]),
    ("investigating", [r"\bprobe[sd]?\b", r"\binvestigat", r"\braid(s|ed)?\b",
                       r"\bsummons\b", r"\binquiry\b", r"\bunder scrutiny\b",
                       r"\bnotice\b"]),
    ("alleged", [r"\balleged", r"\ballegation", r"\baccus", r"\bclaims?\b",
                 r"\bsuspected\b"]),
]

COMPILED_STAGES = [
    (stage, [re.compile(term, re.IGNORECASE) for term in terms])
    for stage, terms in STAGE_PATTERNS
]

# Ranked worst-last, so the most advanced stage seen wins when a company has
# several stories about the same matter.
STAGE_SEVERITY = {
    "dismissed": 0, "reported": 1, "alleged": 2, "investigating": 3,
    "settled": 4, "charged": 5, "convicted": 6,
}


def classify_risk(text: str) -> list:
    """Which adverse categories a headline touches. A signal to read the
    story, never evidence on its own."""
    haystack = text or ""
    found = []
    for category, patterns in RISK_PATTERNS.items():
        if not any(pattern.search(haystack) for pattern in patterns):
            continue
        suppressors = COMPILED_SUPPRESSORS.get(category, [])
        if any(pattern.search(haystack) for pattern in suppressors):
            continue
        found.append(category)
    return sorted(found)


def detect_stage(text: str) -> str:
    haystack = text or ""
    for stage, patterns in COMPILED_STAGES:
        if any(pattern.search(haystack) for pattern in patterns):
            return stage
    return "reported"


def annotate(articles: list) -> list:
    """Attach risk categories and stage to each article in place."""
    for article in articles:
        text = article.headline or ""
        if article.snippet:
            text = f"{text}. {article.snippet}"
        article.risk_categories = classify_risk(text)
        article.risk_stage = detect_stage(text) if article.risk_categories else None
    return articles


def is_flagworthy(article) -> bool:
    """Whether an article should raise a flag, not merely carry a keyword.

    Regulated industries mention their regulator constantly. Flagging every
    telecom story that says "RBI" flagged seven of eight companies in one
    run, which is alarm fatigue: a flag on everything informs nothing.

    So a keyword match alone is not enough. The article must also show either
    a matter progressing beyond bare mention, or negative tone. This keeps
    the two axes independent — tone is still not risk — while requiring one
    of them to corroborate the other.
    """
    if not article.risk_categories:
        return False
    return (article.risk_stage or "reported") != "reported" or article.sentiment == "negative"


def build_flags(articles: list) -> list:
    """Group risk-bearing articles into one flag per category."""
    by_category = defaultdict(list)
    for article in articles:
        if not is_flagworthy(article):
            continue
        for category in article.risk_categories:
            by_category[category].append(article)

    flags = []
    for category, group in sorted(by_category.items()):
        stage = max(
            (a.risk_stage or "reported" for a in group),
            key=lambda s: STAGE_SEVERITY.get(s, 1),
        )
        dates = sorted(a.published for a in group if a.published)
        headline = group[0].headline or ""
        flags.append(
            Flag(
                category=category,
                stage=stage,
                summary=(
                    f"{len(group)} article(s) reference {category}. "
                    f"Most advanced stage in the coverage: {stage}. "
                    f'Leading item: "{headline[:140]}"'
                ),
                first_reported=dates[0] if dates else None,
                evidence=[a.url for a in group[:5]],
            )
        )
    return flags
