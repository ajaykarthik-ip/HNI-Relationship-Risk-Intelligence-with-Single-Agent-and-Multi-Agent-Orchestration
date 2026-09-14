"""The delivered data shapes.

Every record that leaves this system carries the URL it came from. That rule
is enforced here rather than remembered at each call site.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal

Sentiment = Literal["positive", "neutral", "negative"]

FlagCategory = Literal[
    "fraud", "scam", "litigation", "regulatory", "investigation",
    "insolvency", "arrest",
]

# How far a matter has actually progressed. Reporting an allegation as a
# finding is the central accuracy risk in adverse-media screening, so stage
# travels with every flag and is always rendered.
FlagStage = Literal[
    "alleged", "investigating", "charged", "settled", "dismissed",
    "convicted", "reported",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Article:
    headline: str
    url: str
    source: str
    source_url: str
    publisher: str | None = None
    published: str | None = None
    snippet: str | None = None
    url_is_redirect: bool = False
    # Which company this article was collected for; None means person-level.
    about_company: str | None = None
    sentiment: Sentiment = "neutral"
    sentiment_score: float = 0.0
    risk_categories: list = field(default_factory=list)
    risk_stage: str | None = None
    # Published outside the subject's time at the company, so it is the
    # company's history rather than the subject's exposure. Shown as evidence,
    # never counted toward a flag against the person.
    outside_tenure: bool = False
    tenure_note: str | None = None

    # Which adverse check found it, for reporting what was searched.
    query_group: str | None = None

    # The publisher's own URL, where the feed supplied one. Google News hides
    # the article behind a redirector but names the publisher's domain; Bing
    # wraps the real URL in a click tracker. Without this every article looked
    # like it was published by a search engine.
    publisher_url: str | None = None

    # Provenance. 0 promotional, 1 major, 2 established, 3 unknown.
    publisher_tier: int = 3
    publisher_name: str | None = None
    is_promotional: bool = False

    # Body text, fetched only for articles that look material.
    body_text: str | None = None
    # full | partial | paywalled | blocked | failed | headline_only
    fetch_status: str = "headline_only"

    # What a model read out of this article. Empty until extraction runs.
    extracted: dict | None = None


@dataclass
class Flag:
    category: str
    stage: str
    summary: str
    first_reported: str | None
    evidence: list = field(default_factory=list)


@dataclass
class EvidenceItem:
    """One article standing behind a finding.

    `fetch_status` is carried because a claim read out of a full article and
    one inferred from a headline are not the same quality of evidence, and the
    reader has to be able to tell which they are looking at.
    """

    url: str
    publisher: str
    tier: int
    published: str | None = None
    headline: str | None = None
    quote: str | None = None
    fetch_status: str = "headline_only"


@dataclass
class Finding:
    """One real-world event, assembled from every article describing it.

    This replaces the old per-category Flag. A Flag said "4 articles mention
    fraud"; a Finding says what happened, when, to whom, how far it has gone,
    who reported it, and whether it can be attributed to the subject at all.

    Two fields do the work that stops this product being a liability:

      stage            an allegation is not a finding. A tool that renders
                       "accused" and "convicted" identically is a defamation
                       claim waiting to happen.

      attributed_to_subject  a company's history is not a person's exposure.
                       IL&FS's fraud predated the chairman appointed to clean
                       it up, and was reported as his.
    """

    event_id: str
    what_happened: str
    entity: str
    entity_role: str = ""
    category: str = "other"
    stage: str = "reported"
    event_date: str | None = None
    is_ongoing: bool = False
    # 1 trivial .. 5 severe. Assigned per article by extraction, then the
    # highest corroborated value is kept.
    severity: int = 1
    confidence: float = 0.0
    attributed_to_subject: bool = False
    within_tenure: bool = True
    corroborating_publishers: list = field(default_factory=list)
    contradicting_sources: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    # Which adverse check surfaced it, e.g. "enforcement".
    found_by: str | None = None
    # Set when the deterministic path produced this rather than a model.
    derived_from: str = "extraction"

    @property
    def is_material(self) -> bool:
        """Whether this should drive a risk level.

        Three conditions, all required. One publisher can be wrong, an
        unattributed matter is not the subject's, and a bare mention is not
        an event.
        """
        return (
            self.attributed_to_subject
            and self.within_tenure
            and len(self.corroborating_publishers) >= 1
            and self.severity >= 2
        )


@dataclass
class SentimentCounts:
    negative: int = 0
    neutral: int = 0
    positive: int = 0

    @property
    def total(self) -> int:
        return self.negative + self.neutral + self.positive


@dataclass
class CompanyScreening:
    """One row of the Problem Statement 1 deliverable."""

    name: str
    canonical_name: str
    relationships: list = field(default_factory=list)
    status: str = "active"
    jurisdiction: str | None = None
    registry_id: str | None = None
    link_confidence: float = 0.0
    link_basis: str = ""
    sources: list = field(default_factory=list)
    source_url: str | None = None
    aliases: list = field(default_factory=list)
    # control | employment | philanthropy | unknown. Only "control" entities
    # count toward the headline adverse tally.
    relationship_type: str = "unknown"
    # The finer classification. See resolve/relationships.py.
    relationship_class: str = "other"
    relationship_basis: str = ""
    # Role window, where any source stated one.
    role_start: int | None = None
    role_end: int | None = None

    sentiment: SentimentCounts = field(default_factory=SentimentCounts)
    classification: Sentiment = "neutral"
    articles_reviewed: int = 0
    # Distinguishes "coverage found, tone was balanced" from "nothing found".
    # Both classify as neutral, and they mean very different things.
    insufficient_coverage: bool = False
    negative_news_flag: bool = False
    flags: list = field(default_factory=list)
    articles: list = field(default_factory=list)


@dataclass
class Subject:
    query_name: str
    query_company: str | None
    resolved_name: str | None = None
    description: str | None = None
    wikidata_id: str | None = None
    wikipedia_url: str | None = None
    match_confidence: float = 0.0
    match_basis: str = ""
    # True when the pipeline could not confirm which person this is. Rows are
    # then coverage matching a name, not findings about an individual.
    identity_unverified: bool = True


DISCLAIMER = (
    "Aggregated from public sources. Items are claims made by their publishers, "
    "not verified findings about the named individual. Every flag carries the "
    "stage the matter has reached: an allegation is not a finding. Sentiment is "
    "computed from headline text and is independent of risk, so a neutrally "
    "worded headline can still carry a serious flag. Names collide, so check "
    "link_confidence before relying on any row."
)


def to_dict(obj) -> dict:
    """Dataclass to plain dict, with computed fields the readers expect."""
    data = asdict(obj)
    if isinstance(obj, CompanyScreening):
        data["sentiment"] = {
            "negative": obj.sentiment.negative,
            "neutral": obj.sentiment.neutral,
            "positive": obj.sentiment.positive,
        }
    return data
