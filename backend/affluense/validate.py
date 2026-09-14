"""The last gate: does the evidence actually support what we are about to say?

Everything upstream tries to be right. This module assumes it sometimes is
not, and checks the finished result against its own evidence before a reader
sees it. No network calls, no model, no randomness — the same report always
validates the same way, which is what makes it usable as a retry signal as
well as a display filter.

It never deletes a row. A screening that cannot be supported is *labelled*,
because "we found nothing solid about this company" is a real answer and
silently dropping the row would look like the company was never examined.

Four questions, each from something this tool actually got wrong:

  evidence     Does every flag point at an article? A flag with no article
               behind it is an assertion, and this product's whole claim is
               that there is a source behind each one.

  attribution  Is this company the subject's exposure at all? The RBI and the
               Carnegie Endowment both arrived as "his companies" carrying
               other people's news.

  coverage     Is the sample big enough to call? Tata Housing was marked
               negative on six articles, two of them negative.

  contradiction  Do the parts of the row agree? A negative classification with
               no negative articles means something upstream disagreed with
               itself, and a reader must not have to spot that.
"""

from __future__ import annotations

# Below this many articles a tone verdict is a guess dressed as a finding.
# Chosen to match the asymmetric sentiment threshold: at 8 articles a single
# adverse headline is 12.5% and cannot alone flip the row, at 6 it can.
MIN_ARTICLES_FOR_TONE = 8

# A link this weak is page text nobody corroborated.
# A link this weak is page text nobody corroborated. The comparison below is
# <=, not <: a Firecrawl-only link scores exactly 0.6, so a strict < meant the
# weakest rows in every report were the ones the validator could not see.
MIN_LINK_CONFIDENCE = 0.6

# One tone this far above the rest, with no neutral coverage to speak of, is
# the shape promotional copy makes. The rule used to require *zero* neutrals,
# which caught Adani Ports at 13/0/0 and let Adani Energy through at 26/1/0.
ONE_SIDED_SHARE = 0.9

# Relationship types whose adverse news is the subject's own exposure.
ATTRIBUTABLE = ("control", "unknown")

# Verdicts, weakest last. A row takes the worst one it earns.
OK = "ok"
UNCERTAIN = "uncertain"
UNVERIFIED = "unverified"
INSUFFICIENT = "insufficient_coverage"

SEVERITY = {OK: 0, UNCERTAIN: 1, UNVERIFIED: 2, INSUFFICIENT: 3}


def _worst(current: str, candidate: str) -> str:
    return candidate if SEVERITY[candidate] > SEVERITY[current] else current


def is_one_sided(counts: dict) -> bool:
    """Whether a tone breakdown is too lopsided to be real news coverage.

    Real reporting on any substantial company carries neutral items: results,
    appointments, product news. A body of coverage that is 90% one tone is
    usually a press-release feed, and it was 26 positive to 1 neutral that
    slipped past the earlier "no neutrals at all" version of this rule.
    """
    total = sum(counts.values()) if counts else 0
    if not total:
        return False
    dominant = max(counts.get(tone, 0) for tone in ("positive", "negative"))
    return dominant / total >= ONE_SIDED_SHARE


def validate_company(row: dict, identity_confirmed: bool) -> dict:
    """Check one screening row against its own evidence.

    `row` is a built report row, not a dataclass, so this can run on a report
    loaded from disk as easily as on one in memory.

    Returns {status, issues, display_flags} where `display_flags` is the flags
    that survived — a flag with nothing behind it is not shown as a finding.
    """
    issues: list = []
    status = OK

    articles = row.get("articles_reviewed") or 0
    flags = row.get("flags") or []
    counts = row.get("sentiment_breakdown") or {}
    relationship = row.get("relationship_type") or "unknown"

    # -- coverage -----------------------------------------------------------
    if row.get("insufficient_coverage") or articles == 0:
        issues.append(
            "No article about this company survived the relevance filter, so "
            "no tone or risk conclusion is drawn."
        )
        status = _worst(status, INSUFFICIENT)
    elif articles < MIN_ARTICLES_FOR_TONE and row.get("sentiment") != "neutral":
        issues.append(
            f"Only {articles} articles were reviewed, below the {MIN_ARTICLES_FOR_TONE} "
            f"needed for a '{row.get('sentiment')}' verdict to mean much. Treat "
            "the tone as indicative."
        )
        status = _worst(status, UNCERTAIN)

    # -- evidence -----------------------------------------------------------
    supported, unsupported = [], []
    for flag in flags:
        if flag.get("evidence"):
            supported.append(flag)
        else:
            unsupported.append(flag)

    if unsupported:
        categories = sorted({flag.get("category") or "?" for flag in unsupported})
        issues.append(
            f"{len(unsupported)} flag(s) had no article behind them and are "
            f"not shown: {', '.join(categories)}."
        )
        status = _worst(status, UNCERTAIN)

    # -- attribution --------------------------------------------------------
    if supported and relationship not in ATTRIBUTABLE:
        issues.append(
            f"This is a {relationship.replace('_', ' ')} relationship, so its "
            "adverse coverage is reported but is not the subject's own exposure."
        )
        status = _worst(status, UNCERTAIN)

    if (row.get("link_confidence") or 0) <= MIN_LINK_CONFIDENCE:
        issues.append(
            "The link between the subject and this company rests on page text "
            "that no registry confirmed."
        )
        status = _worst(status, UNVERIFIED)

    if not row.get("source_url") and not row.get("registry_id"):
        issues.append("No source URL or registry id backs this company row.")
        status = _worst(status, UNVERIFIED)

    # -- contradiction ------------------------------------------------------
    negative_articles = counts.get("negative", 0)
    if row.get("sentiment") == "negative" and negative_articles == 0:
        issues.append(
            "Classified negative with no negative articles counted — the tone "
            "and the article breakdown disagree."
        )
        status = _worst(status, UNCERTAIN)

    if supported and articles == 0:
        issues.append(
            "Flags were raised with no articles reviewed, which cannot both be "
            "true."
        )
        status = _worst(status, UNVERIFIED)

    # Coverage almost entirely of one tone is the shape promotional copy
    # makes, not the shape news makes.
    total = sum(counts.values()) if counts else 0
    if total >= 8 and is_one_sided(counts):
        dominant = max(counts, key=lambda tone: counts.get(tone, 0))
        share = counts.get(dominant, 0) / total
        issues.append(
            f"{share:.0%} of the coverage is {dominant} with almost no neutral "
            "reporting, which is characteristic of promotional copy rather "
            "than news."
        )
        status = _worst(status, UNCERTAIN)

    # -- identity -----------------------------------------------------------
    if not identity_confirmed:
        issues.append(
            "The subject's identity was inferred rather than confirmed, so this "
            "row is coverage matching a name."
        )
        status = _worst(status, UNVERIFIED)

    return {
        "status": status,
        "issues": issues,
        "supported_flags": supported,
        "withheld_flags": unsupported,
    }


def needs_more_news(row: dict) -> bool:
    """Should the news step be retried for this company?

    Read by the refinement loop. Deliberately narrow: thin coverage and
    single-tone coverage are the two cases a different free query can
    genuinely improve. A misclassified relationship is not, and retrying it
    would burn rounds without changing the answer.
    """
    articles = row.get("articles_reviewed") or 0
    counts = row.get("sentiment_breakdown") or {}
    total = sum(counts.values()) if counts else 0

    if articles == 0:
        return True
    if articles < MIN_ARTICLES_FOR_TONE:
        return True
    if total >= 8 and is_one_sided(counts):
        return True
    return False


def validate_report(report: dict) -> dict:
    """Validate a built screening report in place, and summarise the result.

    Mutates each screening row to carry its own verdict, because the row is
    what the UI renders and a verdict kept somewhere else would not be shown
    next to the claim it qualifies.
    """
    subject = report.get("subject") or {}
    identity_confirmed = not subject.get("identity_unverified", True)

    tally = {OK: 0, UNCERTAIN: 0, UNVERIFIED: 0, INSUFFICIENT: 0}
    attributable_findings = 0

    for row in report.get("screening", []):
        verdict = validate_company(row, identity_confirmed)
        row["validation"] = {
            "status": verdict["status"],
            "issues": verdict["issues"],
        }

        # A flag with no article behind it stops being a finding here.
        if verdict["withheld_flags"]:
            row["flags"] = verdict["supported_flags"]
            row["flag_categories"] = [f.get("category") for f in verdict["supported_flags"]]
            row["flag_stages"] = [f.get("stage") for f in verdict["supported_flags"]]
            row["negative_news_flag"] = bool(verdict["supported_flags"])

        tally[verdict["status"]] += 1
        if row.get("negative_news_flag") and (
            row.get("relationship_type") in ATTRIBUTABLE
        ):
            attributable_findings += 1

    report["validation"] = {
        "identity_confirmed": identity_confirmed,
        "companies_by_status": tally,
        "attributable_findings": attributable_findings,
        "thresholds": {
            "min_articles_for_tone": MIN_ARTICLES_FOR_TONE,
            "min_link_confidence": MIN_LINK_CONFIDENCE,
        },
        "explanation": (
            "Every row was checked against its own evidence before display. "
            "Rows are labelled, never removed: 'nothing solid was found' is an "
            "answer, and dropping the row would look like the company was "
            "never examined. A flag with no article behind it is withheld."
        ),
    }
    return report
