"""Sentiment classification for news headlines.

VADER is the base model: free, no key, no download, and it handles negation
and intensifiers ("not cleared", "deeply flawed") which a bare keyword count
cannot. What it does not have is finance and regulatory vocabulary. Measured
on the stock lexicon:

    "Firm faces insolvency petition at NCLT"        ->  0.000  neutral
    "Regulator penalises bank over KYC lapses"      ->  0.000  neutral
    "SEBI settles probe against firm for Rs 40 cr"  ->  0.000  neutral

All three are plainly negative to a compliance reader. The domain lexicon
below closes that gap; the VADER machinery around it still applies.
"""

from __future__ import annotations

import re
import warnings

from .publishers import TIER_WEIGHT

# Single tokens, scored on VADER's -4..+4 scale. Token-level entries let
# VADER apply its own negation and degree modifiers on top.
DOMAIN_LEXICON = {
    # regulatory and legal
    "probe": -2.0, "probes": -2.0, "probed": -2.0,
    "investigation": -1.9, "investigations": -1.9, "investigating": -1.9,
    "penalty": -2.2, "penalties": -2.2, "penalised": -2.4, "penalized": -2.4,
    "fined": -2.3, "fine": -1.5, "sanctions": -2.2, "sanctioned": -2.2,
    "lapses": -1.8, "violation": -2.3, "violations": -2.3, "breach": -2.2,
    # VADER matches tokens exactly, so every inflection needs its own entry.
    # Their absence was measurable: "Shuts Down Due To Regulatory Issues"
    # scored 0.000 because neither "shuts" nor "regulatory" was present.
    # Softened: naming a regulator is routine ("files prospectus with the
    # regulator"). The adverse weight belongs on what the regulator did.
    "regulator": -0.6, "regulators": -0.6, "regulatory": -1.0,
    "bars": -2.4, "barring": -2.0, "debarred": -2.6,
    "downgrades": -1.9, "ousts": -2.4, "retrenchment": -2.0,
    "suit": -1.6, "suits": -1.4, "summoned": -2.0,
    "raided": -2.5, "raiding": -2.5,
    "shut": -1.8, "shuts": -1.8, "shutting": -1.8, "shuttered": -2.0,
    "closure": -1.8, "closures": -1.8, "closes": -0.8, "closed": -0.8,
    "halted": -2.0, "halt": -1.8, "suspended": -2.2, "suspension": -2.2,
    "banned": -2.6, "ban": -2.2, "barred": -2.4, "blacklisted": -3.0,
    "dues": -1.2, "unpaid": -2.0, "arrears": -1.8, "overdue": -1.6,
    "resigned": -1.0, "resigns": -1.0, "ousted": -2.4, "sacked": -2.4,
    "exits": -0.8, "quits": -1.2, "withdraws": -1.0, "scrapped": -1.6,
    "controversy": -2.2, "controversial": -2.0, "backlash": -2.4,
    "criticised": -2.0, "criticized": -2.0, "flak": -1.8, "row": -1.4,
    "notices": -1.0, "seized": -2.6, "freeze": -2.0, "frozen": -2.0,
    "misconduct": -2.8, "irregularities": -2.4, "noncompliance": -2.2,
    "summons": -2.0, "subpoena": -2.0, "raid": -2.5, "raids": -2.5,
    "lawsuit": -2.2, "lawsuits": -2.2, "sued": -2.3, "sues": -2.1,
    "litigation": -1.8, "tribunal": -1.2, "petition": -1.0,
    "allegation": -2.2, "allegations": -2.2, "alleged": -1.8,
    "accused": -2.4, "accuses": -2.3,
    # criminal
    "fraud": -3.4, "fraudulent": -3.4, "scam": -3.2, "ponzi": -3.4,
    "embezzlement": -3.3, "bribery": -3.3, "laundering": -3.3,
    "arrest": -3.0, "arrested": -3.2, "custody": -2.4,
    "chargesheet": -3.0, "indicted": -3.1, "convicted": -3.4, "guilty": -3.2,
    "acquitted": 1.8, "exonerated": 2.2, "cleared": 1.6, "quashed": 1.2,
    # financial distress
    "insolvency": -2.8, "insolvent": -2.8, "bankruptcy": -3.0,
    "bankrupt": -3.0, "liquidation": -2.6, "default": -2.0,
    "defaulted": -2.3, "defaults": -2.1, "writedown": -2.0,
    "downgrade": -1.9, "downgraded": -1.9, "slump": -2.0, "slumps": -2.0,
    "plunge": -2.3, "plunged": -2.3, "layoffs": -2.0, "shutdown": -1.8,
    "losses": -1.8, "loss": -1.4, "distress": -2.2,
    # positive business vocabulary
    "oversubscribed": 2.2, "profit": 1.8, "profits": 1.8, "profitable": 2.0,
    "surge": 2.2, "surges": 2.2, "surged": 2.2, "rally": 1.8,
    "expansion": 1.4, "expands": 1.4, "milestone": 1.8, "record": 1.2,
    "acquires": 0.9, "acquisition": 0.6, "partnership": 1.2,
    "funding": 1.0, "raised": 0.9, "growth": 1.5, "upgrade": 1.8,
    "awarded": 1.8, "wins": 2.0, "won": 1.8, "launch": 0.8,
    "raises": 1.4, "raise": 1.0, "upgrades": 1.8, "upgraded": 1.8,
    "grows": 1.5, "grew": 1.5, "beats": 1.4, "doubling": 1.2,
    "leased": 1.2, "secures": 1.4, "secured": 1.2, "turnaround": 1.6,
}

# Multi-word signals VADER's token lexicon cannot reach. Applied as a bounded
# nudge after the base score.
PHRASE_ADJUSTMENTS = {
    r"\bshow cause\b": -1.2,
    r"\bclass action\b": -1.5,
    r"\bwound up\b": -1.5,
    r"\bwinding up\b": -1.5,
    r"\bsteps down\b": -0.6,
    r"\bstepped down\b": -0.6,
    r"\bresigns? amid\b": -1.5,
    r"\bunder scrutiny\b": -1.4,
    r"\bred flag\b": -1.5,
    r"\bcourt orders?\b": -1.0,
    r"\ball-time high\b": 1.5,
    r"\babove target\b": 1.2,
    r"\bclean chit\b": 2.0,
    # Resolution language. Without these, "court dismisses petition" and
    # "regulator closes inquiry" score negative on their subject matter alone,
    # which inverts the meaning of the headline.
    r"\bno finding against\b": 2.4,
    r"\bdismisses?\b.{0,20}\bpetition\b": 2.4,
    r"\bcloses?\b.{0,30}\b(inquiry|probe|investigation)\b": 2.4,
    r"\bacquitted of\b": 2.0,
    r"\bloss narrows\b": 1.6,
    r"\bpre-?leased\b": 1.5,
    r"\blays? off\b": -2.2,
    r"\bsearches premises\b": -2.6,
    r"\bfunding winter\b": -1.8,
    r"\bwithout admitting\b": -1.0,
    r"\bred herring prospectus\b": 0.8,
}

# Headline text is short and blunt; VADER's stock +/-0.05 boundary flips on
# almost any adjective. A wider dead zone keeps "neutral" meaningful.
POSITIVE_AT = 0.25
NEGATIVE_AT = -0.25

# Asymmetric on purpose. In screening, adverse coverage matters
# disproportionately: a company where one story in four is negative is one an
# analyst must read, even when the rest of the press is good. Calling that
# "neutral" buries it. A positive verdict has to clear a higher bar, because
# claiming a company is well regarded is a stronger assertion than flagging
# it for a look.
NEGATIVE_SHARE_THRESHOLD = 0.25
POSITIVE_SHARE_THRESHOLD = 0.35


class SentimentAnalyzer:
    """VADER plus a finance/regulatory lexicon, with a keyword fallback."""

    def __init__(self) -> None:
        self._phrases = {
            re.compile(pattern, re.IGNORECASE): weight
            for pattern, weight in PHRASE_ADJUSTMENTS.items()
        }
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

            self._vader = SentimentIntensityAnalyzer()
            self._vader.lexicon.update(DOMAIN_LEXICON)
            self.backend = "vader+domain-lexicon"
        except ImportError:
            self._vader = None
            self.backend = "domain-lexicon-only (DEGRADED)"
            # Measured on the holdout set: the fallback scores 0.48 accuracy
            # against 0.67 with VADER, never predicts positive at all, and
            # misses half the adverse headlines. That is too large a drop to
            # mention in passing.
            warnings.warn(
                "vaderSentiment is not installed. Sentiment is running on the "
                "keyword fallback, which measures 0.48 accuracy against 0.67 "
                "with VADER and misses roughly half of adverse headlines. "
                "Run: pip install -r requirements.txt",
                RuntimeWarning,
                stacklevel=2,
            )

    def score(self, text: str) -> float:
        """Compound score in [-1, 1]."""
        if not text:
            return 0.0

        if self._vader is not None:
            base = self._vader.polarity_scores(text)["compound"]
        else:
            base = self._fallback_score(text)

        for pattern, weight in self._phrases.items():
            if pattern.search(text):
                base += weight / 4.0  # phrase weights share VADER's -4..4 scale

        return max(-1.0, min(1.0, base))

    def _fallback_score(self, text: str) -> float:
        """Used only when vaderSentiment is not installed."""
        tokens = re.findall(r"[a-z']+", text.lower())
        hits = [DOMAIN_LEXICON[t] for t in tokens if t in DOMAIN_LEXICON]
        if not hits:
            return 0.0
        return max(-1.0, min(1.0, sum(hits) / (len(hits) * 4.0) * 1.5))

    def classify(self, text: str) -> tuple:
        """Return (label, score)."""
        value = self.score(text)
        if value >= POSITIVE_AT:
            return "positive", round(value, 4)
        if value <= NEGATIVE_AT:
            return "negative", round(value, 4)
        return "neutral", round(value, 4)

    def annotate(self, articles: list) -> list:
        """Attach sentiment to each article in place."""
        for article in articles:
            text = article.headline or ""
            if article.snippet:
                text = f"{text}. {article.snippet}"
            article.sentiment, article.sentiment_score = self.classify(text)
        return articles


def aggregate(articles: list, weighted: bool = True):
    """Roll article sentiment into one classification for a company.

    Not a simple majority, and not symmetric: adverse coverage is weighted,
    because a company with a quarter of its coverage negative is the one an
    analyst must read.

    Shares are computed on *publisher-weighted* counts when `weighted` is on,
    so a wire-service report outweighs an unknown aggregator. The counts
    returned are the raw article counts -- those are what a reader recognises,
    and a fractional article in the UI would be nonsense. Pass weighted=False
    to score purely by article count.

    Returns (counts_dict, classification).
    """
    counts = {"negative": 0, "neutral": 0, "positive": 0}
    weights = {"negative": 0.0, "neutral": 0.0, "positive": 0.0}

    for article in articles:
        counts[article.sentiment] = counts.get(article.sentiment, 0) + 1
        weight = 1.0
        if weighted:
            weight = TIER_WEIGHT.get(getattr(article, "publisher_tier", 3), 0.4)
        weights[article.sentiment] = weights.get(article.sentiment, 0.0) + weight

    total = sum(counts.values())
    if total == 0:
        return counts, "neutral"

    weighted_total = sum(weights.values())
    if weighted_total == 0:
        # Every article came from a zero-weight publisher. There is coverage
        # but nothing that should move a verdict.
        return counts, "neutral"

    negative_share = weights["negative"] / weighted_total
    positive_share = weights["positive"] / weighted_total

    if negative_share >= NEGATIVE_SHARE_THRESHOLD:
        return counts, "negative"
    if positive_share >= POSITIVE_SHARE_THRESHOLD and positive_share > negative_share:
        return counts, "positive"
    return counts, "neutral"
