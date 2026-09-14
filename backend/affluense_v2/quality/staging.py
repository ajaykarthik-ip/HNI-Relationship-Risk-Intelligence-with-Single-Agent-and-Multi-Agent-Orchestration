"""How far a matter has actually gone, according to what the source says.

The stage is the field that stops this product being a liability: rendering
"accused" and "convicted" identically is a defamation claim waiting to happen.
V1 gets it wrong in two specific, generic ways.

**A regex over a headline cannot know who was charged.** `risk.STAGE_PATTERNS`
matches `charge ?sheet|indicted|arrested|prosecut` anywhere in a headline and
returns "charged". A headline of the form "agency files chargesheet against
three executives, *excluding* X" therefore reports X as charged. The pattern has
no notion of an actor, and no notion of negation. The same applies downward:
"investigating" matches a bare `notice`, so any tax or compliance notice becomes
an investigation.

**One article lifts the whole cluster.** `cluster.py` takes `max(stages)` by
severity, so a single stray "arrested" headline raises a group of twenty
allegations to "charged".

Two rules fix both, without naming anything:

  grounded escalation   a stage *above* an allegation has to come from the
                        model, which read the body text and was asked who did
                        what. Keyword-derived escalation caps at "alleged".
                        De-escalation -- dismissed, settled -- passes through
                        untouched, because that is the safe direction.

  corroborated advance  "charged" and "convicted" need more than one
                        independent publisher. A single outlet asserting the
                        most severe stage is exactly the claim that should
                        need a second source.

Dismissal precedence is preserved: a cleared matter must never read as an open
one, so a grounded dismissal outranks everything below a conviction.
"""

from __future__ import annotations

from affluense.enrich import risk

# V1's ordering, imported rather than restated so the two engines cannot drift
# about which stage outranks which.
SEVERITY = dict(risk.STAGE_SEVERITY)

# The most a keyword match may assert on its own. Anything the regex found
# above this is an inference about an actor that the regex is not able to make.
KEYWORD_CEILING = "alleged"

# Stages that reduce exposure. A keyword is trusted for these because being
# wrong in this direction under-reports our own certainty, never the subject's.
DE_ESCALATING = ("dismissed", "settled")

# Independent publishers required before a stage may be asserted.
CORROBORATION = {"convicted": 2, "charged": 2}
DEFAULT_CORROBORATION = 1

# Confidence below which the model's own reading is not treated as grounded.
# Mirrors the threshold extract._apply already uses to decide whether to
# override the keyword annotation.
GROUNDED_CONFIDENCE = 0.5


def severity_of(stage: str | None) -> int:
    return SEVERITY.get(stage or "reported", 1)


def is_grounded(article) -> bool:
    """True when a model read this article and was confident about the stage.

    "Grounded" means something specific here: the stage came from a system that
    was given the text and asked who did what, rather than from a pattern that
    matched a word somewhere in a headline.
    """
    extracted = getattr(article, "extracted", None) or {}
    if not extracted.get("stage"):
        return False
    try:
        return float(extracted.get("confidence") or 0) >= GROUNDED_CONFIDENCE
    except (TypeError, ValueError):
        return False


def stage_of(article) -> str:
    """One article's stage, after the grounding rule.

    Identical to V1's `_stage` when the model read the article. Capped when the
    only thing behind it is a keyword.
    """
    extracted = getattr(article, "extracted", None) or {}
    grounded = is_grounded(article)
    stage = extracted.get("stage") or getattr(article, "risk_stage", None) \
        or "reported"

    if grounded:
        return stage
    if stage in DE_ESCALATING:
        return stage
    if severity_of(stage) > severity_of(KEYWORD_CEILING):
        return KEYWORD_CEILING
    return stage


def _publisher(article) -> str:
    name = (getattr(article, "publisher_name", None)
            or getattr(article, "publisher", None) or "")
    return name.strip().lower() or (getattr(article, "url", "") or "")


def resolve(articles: list) -> tuple:
    """The stage a group of articles actually supports. Returns (stage, notes).

    Walks down from the most severe stage present and stops at the first one
    with enough independent publishers behind it. That is a corroboration test,
    not a vote: nineteen allegations do not outvote two credible reports of a
    charge, and one report of a charge does not carry it alone.
    """
    if not articles:
        return "reported", []

    entries = [(stage_of(a), _publisher(a), is_grounded(a)) for a in articles]
    notes: list = []

    # Dismissal precedence. A grounded dismissal outranks everything short of a
    # conviction, because reporting a cleared matter as open is the worse error.
    dismissed = [e for e in entries if e[0] == "dismissed" and e[2]]
    convicted = [e for e in entries if e[0] == "convicted" and e[2]]
    if dismissed and not convicted:
        return "dismissed", notes

    present = sorted(
        {stage for stage, _, _ in entries},
        key=severity_of,
        reverse=True,
    )

    for stage in present:
        if stage == "dismissed":
            continue
        threshold = severity_of(stage)
        # Publishers reporting this stage *or worse*. A publisher that said
        # "convicted" also supports "charged".
        backing = {
            publisher for candidate, publisher, _ in entries
            if severity_of(candidate) >= threshold and publisher
        }
        required = CORROBORATION.get(stage, DEFAULT_CORROBORATION)
        if len(backing) >= required:
            return stage, notes
        notes.append(
            f"{stage} reported by {len(backing)} publisher(s); "
            f"{required} required, so the matter is recorded at a lower stage"
        )

    # Everything present failed its corroboration test.
    return "reported", notes


def keyword_only(articles: list) -> bool:
    """True when nothing in this group was actually read by a model."""
    return not any(is_grounded(a) for a in articles)
