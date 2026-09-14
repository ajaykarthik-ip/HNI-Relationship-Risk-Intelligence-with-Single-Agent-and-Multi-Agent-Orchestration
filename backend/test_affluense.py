"""Tests for the screening and network pipelines.

No network access: every test runs against fixed inputs or a fake fetcher, so
the suite is deterministic and runs in under a second.

    pip install pytest
    pytest -q
"""

from __future__ import annotations

import threading

import pytest

from affluense.cache import ResponseCache, ttl_for
from affluense.enrich import relevance, risk, tenure
from affluense.enrich.sentiment import SentimentAnalyzer, aggregate
from affluense import config, pipeline, risk_score, validate
from affluense.collect import fulltext, queries
from affluense.enrich import cluster, extract
from affluense.enrich import publishers
from affluense.models import EvidenceItem, Finding
from affluense.resolve import relationships
from affluense.usage import UsageMeter
from affluense.models import Article
from affluense.network import discover, scoring
from affluense.resolve.company import (
    merge, merge_roles, normalise, normalise_role, relationship_type,
)
from affluense.sources import firecrawl as fc
from affluense.sources import news, wikipedia


def article(headline: str, **kwargs) -> Article:
    return Article(
        headline=headline,
        url=kwargs.pop("url", "https://example.com/a"),
        source="test",
        source_url="https://example.com",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Sentiment
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def analyzer():
    return SentimentAnalyzer()


@pytest.mark.parametrize("headline", [
    # Stock VADER scores all of these 0.000 neutral. They are why the domain
    # lexicon exists.
    "Firm faces insolvency petition at NCLT",
    "Regulator penalises bank over KYC lapses",
    "SEBI settles probe against firm for Rs 40 crore",
    # These were missed a second time because VADER matches tokens exactly and
    # the inflections were absent from the lexicon.
    "After the founder's exit, the outlet shuts down due to regulatory issues",
    "Restaurant among establishments raided for smoking violations",
    "Outlet shut down after owner's exit; denies Rs 2 crore dues",
])
def test_finance_negatives_are_negative(analyzer, headline):
    label, score = analyzer.classify(headline)
    assert label == "negative", f"{headline!r} scored {score}"


@pytest.mark.parametrize("headline", [
    "Company posts record profit, shares surge",
    "Fund closes above target, oversubscribed",
    "Restaurant chain expands to three new cities",
])
def test_positives_survive_the_domain_lexicon(analyzer, headline):
    assert analyzer.classify(headline)[0] == "positive"


@pytest.mark.parametrize("headline", [
    "Firm opens new office in Pune",
    "Company to announce quarterly results on Tuesday",
])
def test_ordinary_business_news_stays_neutral(analyzer, headline):
    assert analyzer.classify(headline)[0] == "neutral"


def test_negation_is_handled(analyzer):
    """The reason for using VADER rather than a keyword count."""
    plain = analyzer.score("The executive was cleared")
    negated = analyzer.score("The executive was not cleared")
    assert negated < plain


def test_aggregation_is_asymmetric():
    """Adverse coverage is weighted: a quarter negative classifies negative,
    while a positive verdict needs a higher share."""
    def counts(negative, neutral, positive):
        return ([article("x", sentiment="negative")] * negative
                + [article("x", sentiment="neutral")] * neutral
                + [article("x", sentiment="positive")] * positive)

    assert aggregate(counts(12, 24, 4))[1] == "negative"   # 0.30 negative
    assert aggregate(counts(5, 30, 5))[1] == "neutral"     # balanced
    assert aggregate(counts(1, 9, 10))[1] == "positive"
    assert aggregate(counts(0, 0, 0))[1] == "neutral"      # no coverage


# ---------------------------------------------------------------------------
# Risk taxonomy
# ---------------------------------------------------------------------------

def test_risk_terms_respect_word_boundaries():
    """An earlier substring match on "ed " (Enforcement Directorate) fired on
    "backed", "supported" and "expanded", producing false flags."""
    assert risk.classify_risk("Foundation's initiative is backing social causes") == []
    assert risk.classify_risk("Firm backed by investors expanded operations") == []


def test_risk_categories_detected():
    assert "regulatory" in risk.classify_risk("SEBI fined the firm over disclosure lapses")
    assert "insolvency" in risk.classify_risk("NCLT admits insolvency petition")
    assert "fraud" in risk.classify_risk("Executive accused in alleged Ponzi scheme")


def test_dismissal_outranks_conviction():
    """Checked first on purpose, so an acquittal is not read as a conviction."""
    assert risk.detect_stage("Executive acquitted of all charges") == "dismissed"
    assert risk.detect_stage("Executive convicted of fraud") == "convicted"
    assert risk.detect_stage("Executive accused of wrongdoing") == "alleged"
    assert risk.detect_stage("Company opens a new plant") == "reported"


def test_flag_takes_the_most_advanced_stage():
    articles = risk.annotate([
        article("Firm accused of fraud by former partner"),
        article("Executive charged with fraud, chargesheet filed"),
    ])
    flags = risk.build_flags(articles)
    fraud = [f for f in flags if f.category == "fraud"]
    assert fraud and fraud[0].stage == "charged"


def test_a_keyword_alone_does_not_raise_a_flag():
    """Regulated industries name their regulator constantly. Flagging every
    telecom story that says "RBI" flagged seven of eight companies in one
    run, which tells a reader nothing."""
    routine = risk.annotate([
        article("Jio adds 4 million subscribers as RBI clears payments licence")
    ])[0]
    assert "regulatory" in routine.risk_categories
    assert routine.risk_stage == "reported"
    assert not risk.is_flagworthy(routine)
    assert risk.build_flags([routine]) == []


def test_a_progressing_matter_raises_a_flag():
    progressing = risk.annotate([article("SEBI probes the firm over disclosures")])[0]
    assert risk.is_flagworthy(progressing)
    assert risk.build_flags([progressing])


def test_negative_tone_raises_a_flag_even_without_a_stage_verb():
    """Keeps terse adverse headlines, which is why tone is allowed to
    corroborate rather than being ignored entirely."""
    terse = risk.annotate([article("Outlet shut following court order")])[0]
    terse.sentiment = "negative"
    assert risk.is_flagworthy(terse)


def test_articles_not_about_the_company_are_dropped():
    """A phrase query for "Hindustan Computers" returned a US immigration
    story, which became the only adverse finding in a report about a man who
    had nothing to do with it."""
    articles = [
        article("Whistleblower Lawsuit Uncovers Massive H-1B Fraud"),
        article("Hindustan Computers founder recalls the early days"),
    ]
    kept, dropped = relevance.filter_for_company(articles, "Hindustan Computers")
    assert dropped == 1
    assert len(kept) == 1


def test_every_distinctive_token_is_required():
    """"any token" is too loose — "Hindustan" alone matches Hindustan Times."""
    articles = [article("Hindustan Times reports on the budget")]
    kept, _ = relevance.filter_for_company(articles, "Hindustan Computers")
    assert kept == []


def test_legal_forms_do_not_count_as_distinctive():
    assert relevance.distinctive_tokens("HCL Technologies Limited") == ["hcl"]
    assert relevance.distinctive_tokens("Razorpay") == ["razorpay"]


@pytest.mark.parametrize("headline", [
    "Cohesity Adds Managed Clean Room To HCLTech VaultNXT",
    "Firm launches forensic investigation platform for enterprises",
    "Razorpay launches AI fraud detection for merchants",
    "Vendor ships anti-fraud tooling for payment gateways",
])
def test_risk_words_inside_product_names_do_not_flag(headline):
    """Security and payments vendors describe their products with the exact
    vocabulary this classifier watches for. Both of the last two false
    positives in real reports were of this kind."""
    assert risk.classify_risk(headline) == []


def test_selling_fraud_detection_is_not_being_accused_of_fraud():
    """Every payments company trips the keyword otherwise."""
    assert risk.classify_risk("Razorpay launches AI fraud detection for merchants") == []
    assert risk.classify_risk("Firm rolls out anti-fraud tooling") == []
    assert "fraud" in risk.classify_risk("Company defrauded investors, says complaint")


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("variants", [
    ["Vault by Virat Kohli", "Vault", "Vault fitness chain"],
    ["Blue Tribe Foods", "Blue Tribe"],
    ["Digit", "Go Digit"],
    ["ONE8 SPORTS PRIVATE LIMITED", "One8 Sports"],
])
def test_company_variants_normalise_together(variants):
    keys = {normalise(name, "Virat Kohli") for name in variants}
    assert len(keys) == 1, f"{variants} produced {keys}"


def test_merge_collapses_duplicates_and_keeps_aliases():
    records = [
        {"name": n, "source": "Firecrawl extraction", "relationships": ["Investor"]}
        for n in ["Vault by Virat Kohli", "Vault", "Blue Tribe Foods", "Blue Tribe"]
    ]
    merged = merge(records, "Virat Kohli")
    assert len(merged) == 2
    assert all(entry["aliases"] for entry in merged)


def test_registry_name_wins_over_extracted_name():
    merged = merge([
        {"name": "One8 Sports", "source": "Firecrawl extraction", "relationships": []},
        {"name": "ONE8 SPORTS PRIVATE LIMITED", "source": "Wikidata Query Service",
         "relationships": []},
    ], "Virat Kohli")
    assert len(merged) == 1
    assert merged[0]["canonical_name"] == "ONE8 SPORTS PRIVATE LIMITED"


class FakeFetcher:
    """Serves canned Wikipedia responses; records nothing, hits no network."""

    def __init__(self, summaries):
        self.summaries = summaries
        self.descriptions = {}
        self.notes = []

    def get_json(self, url, **kwargs):
        for title, extract in self.summaries.items():
            if title.replace(" ", "_") in url:
                return {
                    "extract": extract,
                    "description": self.descriptions.get(title, "businessperson"),
                    "type": "standard",
                }
        return {}

    def note(self, message):
        self.notes.append(message)


def test_company_verifies_the_person_rather_than_biasing_the_search():
    """Querying "Ratan Tata Tata Sons" ranks Ratanji Tata (1871-1918) first.
    Taking the top hit screened the wrong man."""
    fetcher = FakeFetcher({
        "Ratanji Tata": "An Indian businessman of the nineteenth century.",
        "Ratan Tata": "Chairman of Tata Sons and the Tata Group.",
    })
    title, basis, _ = wikipedia.choose_title(
        fetcher, ["Ratanji Tata", "Ratan Tata"], "Ratan Tata", "Tata Sons"
    )
    assert title == "Ratan Tata"
    assert "Tata Sons" in basis


def test_a_name_that_does_not_resemble_the_query_is_refused():
    """Searching "Aravind Srinivas Perplexity" returns Andy Konwinski, whose
    article names Perplexity. The company bonus alone promoted him to a 95%
    confident report about the wrong human."""
    fetcher = FakeFetcher({
        "Andy Konwinski": "A computer scientist who co-founded Perplexity and Databricks.",
        "Denis Yarats": "A computer scientist at Perplexity.",
    })
    title, basis, _ = wikipedia.choose_title(
        fetcher, ["Perplexity AI", "Denis Yarats", "Andy Konwinski"],
        "Aravind Srinivas", "Perplexity",
    )
    assert title is None, f"matched {title!r}"
    assert "close enough" in basis


def test_an_article_about_a_film_is_never_the_subject():
    """"Aravind Srinivas" matched "Aravind 2", a 2013 Indian film, and the run
    attached a criminal case to a real person's name."""
    fetcher = FakeFetcher({"Aravind 2": "A horror film released in 2013."})
    fetcher.descriptions = {"Aravind 2": "2013 Indian film"}
    title, basis, _ = wikipedia.choose_title(fetcher, ["Aravind 2"], "Aravind 2", None)
    assert title is None
    assert "not about a person" in basis


def test_exact_name_match_wins_without_a_company():
    title, _, _ = wikipedia.choose_title(
        FakeFetcher({}), ["Ratanji Tata", "Ratan Tata"], "Ratan Tata", None
    )
    assert title == "Ratan Tata"


# ---------------------------------------------------------------------------
# News handling
# ---------------------------------------------------------------------------

def test_syndicated_copy_is_deduplicated():
    """RSS appends " - Publisher", so one wire story arrives several times."""
    articles = [
        article("Tata Sons faces IPO deadline with RBI decision - Livemint"),
        article("Tata Sons faces IPO deadline with RBI decision - TradingView"),
        article("A genuinely different story about something else"),
    ]
    assert len(news.dedupe(articles)) == 2


# ---------------------------------------------------------------------------
# Firecrawl domain policy
# ---------------------------------------------------------------------------

def test_login_walled_domains_are_skipped():
    assert fc.is_skipped("https://www.instagram.com/p/abc/")
    assert fc.is_skipped("https://in.linkedin.com/in/someone")
    assert not fc.is_skipped("https://www.zaubacorp.com/VIRAT-KOHLI-06985651")


def test_registry_sources_are_read_before_general_pages():
    results = [
        {"source_url": "https://example.com/blog"},
        {"source_url": "https://www.instagram.com/p/abc/"},
        {"source_url": "https://www.zaubacorp.com/X"},
        {"source_url": "https://www.crunchbase.com/person/x"},
    ]
    ranked, skipped = fc.rank_for_scraping(results)
    assert len(skipped) == 1
    assert ranked[0]["source_url"].endswith("zaubacorp.com/X")
    assert "instagram" not in " ".join(r["source_url"] for r in ranked)


# ---------------------------------------------------------------------------
# Sector resolution
# ---------------------------------------------------------------------------

class SectorFetcher:
    """Fakes Wikidata search and SPARQL for sector resolution."""

    def __init__(self, search_hits, used_as_industry):
        self.search_hits = search_hits
        self.used_as_industry = used_as_industry
        self.notes = []
        self.sparql_calls = 0
        self.search_calls = 0

    def note(self, message):
        self.notes.append(message)


@pytest.fixture
def patched_wikidata(monkeypatch):
    def install(fetcher):
        def search(_f, query, limit=10):
            fetcher.search_calls += 1
            return [{"id": qid} for qid in fetcher.search_hits.get(query.lower(), [])]

        def sparql(_f, query):
            fetcher.sparql_calls += 1
            qid = query.split("wd:")[1].split(" ")[0].strip(" .}")
            return [{"org": {"value": "x"}}] if qid in fetcher.used_as_industry else []

        monkeypatch.setattr(discover.wikidata, "search_entities", search)
        monkeypatch.setattr(discover.wikidata, "sparql", sparql)
        monkeypatch.setattr(discover.wikidata, "resolve_labels",
                            lambda _f, qids: {q: f"label-{q}" for q in qids})
    return install


def test_sector_resolution_skips_items_no_company_declares(patched_wikidata):
    """Searching "fitness" returns films and bands as well as the industry.
    Only an item some company actually declares as its industry is usable."""
    fetcher = SectorFetcher(
        search_hits={"fitness": ["Q_film", "Q_industry"]},
        used_as_industry={"Q_industry"},
    )
    patched_wikidata(fetcher)
    assert discover.resolve_sector(fetcher, "fitness", {}) == "Q_industry"


def test_sector_resolution_returns_none_when_nothing_qualifies(patched_wikidata):
    fetcher = SectorFetcher(search_hits={"vibes": ["Q_band"]}, used_as_industry=set())
    patched_wikidata(fetcher)
    assert discover.resolve_sector(fetcher, "vibes", {}) is None


def test_sector_resolution_is_cached(patched_wikidata):
    fetcher = SectorFetcher(
        search_hits={"fitness": ["Q_industry"]}, used_as_industry={"Q_industry"}
    )
    patched_wikidata(fetcher)
    cache = {}
    discover.resolve_sector(fetcher, "fitness", cache)
    calls = fetcher.search_calls
    discover.resolve_sector(fetcher, "Fitness", cache)  # case-insensitive
    assert fetcher.search_calls == calls, "a cached sector must not re-query"


def test_industries_come_from_the_sectors_named_on_pages(patched_wikidata):
    """The point of the sector field: companies absent from Wikidata still
    yield an industry, so peers are business people rather than whoever
    shares the subject's occupation."""
    fetcher = SectorFetcher(
        search_hits={"restaurant chain": ["Q_rest"], "fitness": ["Q_fit"]},
        used_as_industry={"Q_rest", "Q_fit"},
    )
    patched_wikidata(fetcher)
    companies = [
        {"name": "One8 Commune", "sectors": ["restaurant chain"]},
        {"name": "Vault", "sectors": ["fitness"]},
        {"name": "Nameless Co", "sectors": []},
    ]
    qids, labels, named = discover.industries_from_sectors(fetcher, companies)
    assert set(qids) == {"Q_rest", "Q_fit"}
    assert named == ["restaurant chain", "fitness"]
    assert labels


@pytest.mark.parametrize("sector", ["foundation", "Management", "holdings", "LLP"])
def test_generic_sectors_are_rejected(sector):
    """A legal form is not a line of business. Accepting "foundation" as an
    industry returned Danish pharmaceutical-foundation executives as peers for
    an Indian cricketer."""
    assert discover.is_generic_sector(sector)


@pytest.mark.parametrize("sector", ["restaurant chain", "fitness", "football club", "payments"])
def test_real_sectors_are_kept(sector):
    assert not discover.is_generic_sector(sector)


def test_generic_sectors_are_filtered_before_resolution(patched_wikidata):
    fetcher = SectorFetcher(
        search_hits={"restaurant chain": ["Q_rest"], "foundation": ["Q_found"]},
        used_as_industry={"Q_rest", "Q_found"},
    )
    patched_wikidata(fetcher)
    qids, _labels, named = discover.industries_from_sectors(fetcher, [
        {"name": "One8 Commune", "sectors": ["restaurant chain"]},
        {"name": "Sevva Path Sankalp Foundation", "sectors": ["foundation"]},
    ])
    assert qids == ["Q_rest"]
    assert named == ["restaurant chain"]


@pytest.mark.parametrize("variants", [
    ["Chairman & Managing Director", "Chairman and MD", "Chairman and Managing Director"],
    ["CEO", "Chief Executive Officer", "chief executive officer"],
    ["Member of the Board", "Board Member"],
])
def test_role_spellings_collapse(variants):
    """One company showed eleven near-identical relationships."""
    assert len({normalise_role(v) for v in variants}) == 1


def test_merge_roles_keeps_the_fullest_phrasing():
    merged = merge_roles(["Chairman and MD", "Chairman and Managing Director", "CMD"])
    assert merged == ["Chairman and Managing Director"]


def test_company_merge_collapses_role_spellings():
    merged = merge([{
        "name": "Reliance Industries", "source": "Firecrawl extraction",
        "relationships": ["Chairman & Managing Director", "Chairman and MD",
                          "Founder", "founder", "Various roles"],
    }], "Mukesh Ambani")
    roles = merged[0]["relationships"]
    assert len(roles) == 2, roles
    assert any("Managing Director" in r for r in roles)


def test_one_record_with_an_id_and_one_without_still_merge():
    """Keying on the registry id split "Perplexity AI" into two rows — one
    from Wikidata, one from extraction — in the same report."""
    merged = merge([
        {"name": "Perplexity AI", "registry_id": "Q124333951",
         "source": "Wikidata Query Service", "relationships": ["founder"]},
        {"name": "Perplexity AI", "source": "Firecrawl extraction",
         "relationships": ["Co-founder"]},
    ], "Aravind Srinivas")
    assert len(merged) == 1
    assert merged[0]["registry_id"] == "Q124333951"


def test_distinct_registry_ids_are_never_merged():
    """Wipro and Wipro Enterprises normalise to the same brand key because
    "enterprises" is a descriptor. They are two filed companies, and merging
    them under-reports the subject's footprint."""
    merged = merge([
        {"name": "Wipro", "registry_id": "Q1364176",
         "source": "Wikidata Query Service", "relationships": ["founder"]},
        {"name": "Wipro Enterprises", "registry_id": "Q65118056",
         "source": "Wikidata Query Service", "relationships": ["chairperson"]},
    ], "Azim Premji")
    assert len(merged) == 2


def test_brand_variants_without_registry_ids_still_merge():
    merged = merge([
        {"name": "Vault by Virat Kohli", "source": "Firecrawl extraction",
         "relationships": ["Investor"]},
        {"name": "Vault fitness chain", "source": "Firecrawl extraction",
         "relationships": ["Owner"]},
    ], "Virat Kohli")
    assert len(merged) == 1


def test_gendered_and_neutral_chair_spellings_are_one_role():
    assert normalise_role("Chairman") == normalise_role("chairperson")
    assert merge_roles(["Chairman", "chairperson", "Chairwoman"]) == ["chairperson"]


def test_a_former_role_stays_distinct_from_a_current_one():
    """Tenure matters to a compliance reader, so "Former Chairman" must not
    collapse into "Chairman"."""
    assert normalise_role("Former Chairman") != normalise_role("Chairman")


@pytest.mark.parametrize("pair", [
    ("SLB", "Schlumberger"),
    ("Hindustan Computers", "HCL"),
    ("Perplexity AI", "Perplexity"),
])
def test_known_rebrands_and_abbreviations_resolve_together(pair):
    """Normalisation cannot see that SLB is Schlumberger renamed; both
    appeared as separate rows in a real report."""
    assert normalise(pair[0]) == normalise(pair[1])


@pytest.mark.parametrize("name,roles,expected", [
    ("Razorpay", ["Co-Founder & CEO"], "control"),
    ("HCL Technologies", ["Chairman"], "control"),
    ("Schlumberger", ["Wireline Field Engineer"], "employment"),
    ("OpenAI", ["Research Scientist"], "employment"),
    ("Shiv Nadar University", ["Founder"], "philanthropy"),
    ("Some Trust", [], "philanthropy"),
])
def test_relationship_type(name, roles, expected):
    """A past job and a directorship are not the same exposure: a former
    employer's regulatory news was being flagged under the subject."""
    assert relationship_type(name, roles) == expected


def test_merged_companies_keep_their_sectors():
    merged = merge([
        {"name": "Vault by Virat Kohli", "source": "Firecrawl extraction",
         "relationships": ["Investor"], "sectors": ["fitness"]},
        {"name": "Vault", "source": "Firecrawl extraction",
         "relationships": ["Owner"], "sectors": ["gyms"]},
    ], "Virat Kohli")
    assert len(merged) == 1
    assert set(merged[0]["sectors"]) == {"fitness", "gyms"}


def test_profile_reads_the_country_under_either_key(patched_wikidata):
    """Wikidata records carry "country"; discovery records carry
    "jurisdiction". Reading only one left every candidate reported as
    "extending the network beyond the subject's current markets", including
    people in the subject's own country."""
    from_wikidata = discover.subject_profile([{"name": "A", "country": "India", "relationships": []}])
    from_discovery = discover.subject_profile([{"name": "B", "jurisdiction": "India", "relationships": []}])
    assert from_wikidata["countries"] == ["India"]
    assert from_discovery["countries"] == ["India"]


def test_same_country_scores_above_a_different_one(profile):
    def candidate(name, country):
        return {"name": name, "wikidata_id": name, "roles": ["founder"],
                "companies": ["Co"], "matched_industries": ["Q1540863"],
                "country": country, "sitelinks": 5, "source": "t", "source_url": "u"}

    ranked = scoring.rank([candidate("Abroad", "Spain"), candidate("Home", "India")],
                          profile, [])
    assert ranked[0]["name"] == "Home"


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------

@pytest.fixture
def profile():
    return {
        "roles": ["founder", "chairperson"],
        "industries": ["information technology consulting"],
        "industry_qids": ["Q1540863"],
        "industry_labels": {"Q1540863": "information technology consulting"},
        "countries": ["India"],
        "company_names": ["Wipro"],
    }


def test_relevance_is_reproducible_from_its_components(profile):
    candidate = {
        "name": "A Peer", "wikidata_id": "Q1", "roles": ["founder"],
        "companies": ["Infosys"], "matched_industries": ["Q1540863"],
        "country": "India", "sitelinks": 20,
        "source": "test", "source_url": "https://example.com",
    }
    scored = scoring.score_candidate(candidate, profile, set(), set())
    recomputed = sum(
        scoring.WEIGHTS[key] * value
        for key, value in scored["score_components"].items()
    )
    assert scored["relevance_score"] == pytest.approx(min(1.0, recomputed), abs=1e-3)
    assert scored["signals"], "a score with no explanation is not usable"


def test_people_already_in_the_network_are_not_suggested(profile):
    candidates = [
        {"name": "Known Person", "wikidata_id": "Q1", "roles": ["founder"],
         "companies": ["Wipro"], "matched_industries": ["Q1540863"],
         "country": "India", "sitelinks": 5, "source": "t", "source_url": "u"},
        {"name": "New Person", "wikidata_id": "Q2", "roles": ["founder"],
         "companies": ["Infosys"], "matched_industries": ["Q1540863"],
         "country": "India", "sitelinks": 5, "source": "t", "source_url": "u"},
    ]
    network = [{"name": "Known Person", "company": "Wipro"}]
    ranked = scoring.rank(candidates, profile, network)
    assert [row["name"] for row in ranked] == ["New Person"]


def test_unlabelled_wikidata_items_are_dropped(profile):
    """Labels fall back to the Q-number when no English label exists; those
    are unusable as a suggestion."""
    ranked = scoring.rank([{
        "name": "Q12345", "wikidata_id": "Q12345", "roles": ["founder"],
        "companies": [], "matched_industries": [], "country": None,
        "sitelinks": 0, "source": "t", "source_url": "u",
    }], profile, [])
    assert ranked == []


def test_same_role_outranks_unrelated_role(profile):
    def candidate(name, role):
        return {"name": name, "wikidata_id": name, "roles": [role],
                "companies": ["Some Co"], "matched_industries": ["Q1540863"],
                "country": "India", "sitelinks": 5, "source": "t", "source_url": "u"}

    ranked = scoring.rank(
        [candidate("Director Person", "director / manager"),
         candidate("Founder Person", "founder")],
        profile, [],
    )
    assert ranked[0]["name"] == "Founder Person"


def test_occupation_only_candidates_score_far_below_industry_peers(profile):
    """The fallback pool must not look as good as a real industry match.

    When no industry can be established the pipeline suggests people sharing
    the subject's occupation. Those suggestions are real but weak, and the
    score has to say so rather than presenting them as equivalent.
    """
    strong = {
        "name": "Industry Peer", "wikidata_id": "Q1", "roles": ["founder"],
        "companies": ["Infosys"], "matched_industries": ["Q1540863"],
        "country": "India", "sitelinks": 20, "source": "t", "source_url": "u",
    }
    weak = {
        "name": "Occupation Peer", "wikidata_id": "Q2", "roles": [],
        "companies": [], "matched_industries": [], "country": "India",
        "sitelinks": 20, "source": "t", "source_url": "u",
    }
    ranked = scoring.rank([weak, strong], profile, [])
    assert ranked[0]["name"] == "Industry Peer"
    assert ranked[0]["relevance_score"] > 3 * ranked[1]["relevance_score"]


def test_a_candidate_with_no_signal_scores_near_zero(profile):
    ranked = scoring.rank([{
        "name": "Unrelated Person", "wikidata_id": "Q9", "roles": [],
        "companies": [], "matched_industries": [], "country": None,
        "sitelinks": 0, "source": "t", "source_url": "u",
    }], profile, [])
    assert ranked[0]["relevance_score"] == 0.0


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def test_cache_keys_are_stable_and_order_independent():
    first = ResponseCache.key("GET", "https://x/y", {"a": 1, "b": 2})
    second = ResponseCache.key("GET", "https://x/y", {"b": 2, "a": 1})
    assert first == second
    assert first != ResponseCache.key("GET", "https://x/y", {"a": 2})


def test_ttl_is_chosen_per_source_class():
    """News must stay fresh; paid calls must not be paid for twice."""
    assert ttl_for("https://news.google.com/rss/search?q=x") == 3600
    assert ttl_for("https://api.firecrawl.dev/v2/search") > 24 * 3600
    assert ttl_for("https://www.wikidata.org/w/api.php") > 24 * 3600


def test_cache_roundtrip(tmp_path):
    cache = ResponseCache(str(tmp_path))
    key = cache.key("GET", "https://news.google.com/x")
    assert cache.get(key, "https://news.google.com/x") is None
    cache.set(key, "payload")
    assert cache.get(key, "https://news.google.com/x") == "payload"
    assert cache.summary["hits"] == 1


def test_disabled_cache_never_stores(tmp_path):
    cache = ResponseCache(str(tmp_path), enabled=False)
    key = cache.key("GET", "https://x")
    cache.set(key, "payload")
    assert cache.get(key, "https://x") is None


# ---------------------------------------------------------------------------
# Controlling stakes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("65%", 65.0),
    ("61.7%", 61.7),
    ("51 per cent", 51.0),
    ("majority stake", 100.0),
    ("wholly-owned", 100.0),
    ("19%", 19.0),
    # An amount is not a stake. Knowing the money says nothing about control.
    ("Rs 80000 crore", None),
    ("", None),
    (None, None),
])
def test_stake_percent_reads_only_real_percentages(text, expected):
    assert pipeline.stake_percent(text) == expected


def test_controlling_stakes_become_screening_targets():
    """A 65% holding is exposure whether or not a page names him a director."""
    promoted = pipeline._controlling_investments([
        {"entity": "HZL", "stake_or_amount": "65%"},
        {"entity": "BALCO", "stake_or_amount": "51%"},
        {"entity": "Anglo American", "stake_or_amount": "19%"},
        {"entity": "Vedanta", "stake_or_amount": "Rs 80000 crore"},
    ], 50.0)

    assert [r["name"] for r in promoted] == ["HZL", "BALCO"]
    # The stake has to survive into the role, or relationship_type cannot
    # tell that this is control.
    assert relationship_type("HZL", promoted[0]["relationships"]) == "control"


def test_control_outranks_a_former_role_when_the_list_is_truncated():
    rows = [
        {"name": "Old Employer", "relationships": ["analyst"], "status": "former"},
        {"name": "Family Foundation", "relationships": ["trustee"], "status": "active"},
        {"name": "HZL", "relationships": ["shareholder (65%)"], "status": "active"},
        {"name": "Former Chair Co", "relationships": ["chairman"], "status": "former"},
    ]
    ordered = [r["name"] for r in sorted(rows, key=pipeline._screening_priority)]
    assert ordered[0] == "HZL"
    assert ordered[-1] == "Family Foundation"


# ---------------------------------------------------------------------------
# Public bodies and weak company names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,roles", [
    ("Reserve Bank of India", ["Board Member"]),
    ("Securities and Exchange Board of India", ["Member"]),
    ("Confederation of Indian Industry", ["President"]),
])
def test_a_public_appointment_is_not_a_corporate_holding(name, roles):
    assert relationship_type(name, roles) == "public_office"


def test_an_ordinary_bank_is_still_a_company():
    """"bank" must not become a blanket exemption."""
    assert relationship_type("Bank of India", ["Chairman"]) == "control"
    assert relationship_type("HDFC Bank", ["Director"]) == "control"


def test_a_common_word_name_is_weak():
    assert relevance.is_weak_name(relevance.distinctive_tokens("More"))
    assert not relevance.is_weak_name(relevance.distinctive_tokens("Hindalco Industries"))
    # Two ordinary words still identify nothing between them.
    assert relevance.is_weak_name(relevance.distinctive_tokens("Simple Energy"))


def _article(headline):
    return Article(headline=headline, url="u", source="s", source_url="u")


def test_a_weak_name_needs_corroboration():
    articles = [
        _article("More people are buying EVs this year"),
        _article("More Retail expands to 20 new cities"),
        _article("Read more about the market rally"),
    ]
    tokens = relevance.corroborating_tokens(["More Retail"], ["Retail"], ["K M Birla"])
    kept, dropped = relevance.filter_for_company(articles, "More", corroborators=tokens)

    assert [a.headline for a in kept] == ["More Retail expands to 20 new cities"]
    assert dropped == 2


def test_a_weak_name_with_nothing_to_corroborate_it_scores_nothing():
    """Silence beats sentiment computed on the word "more"."""
    articles = [_article("More rain expected"), _article("Tell me more")]
    kept, dropped = relevance.filter_for_company(articles, "More", corroborators=[])
    assert kept == [] and dropped == 2


# ---------------------------------------------------------------------------
# Cost metering
# ---------------------------------------------------------------------------

def test_only_firecrawl_is_billed():
    """Every other source is free, and must cost nothing."""
    meter = UsageMeter(usd_per_credit=0.01, usd_to_inr=90.0)
    meter.record("https://news.google.com/rss/search")
    meter.record("https://www.wikidata.org/w/api.php")
    meter.record("https://www.bing.com/news/search")

    totals = meter.summary()["totals"]
    assert totals["requests"] == 3
    assert totals["billable_credits"] == 0
    assert totals["usd"] == 0 and totals["inr"] == 0


def test_credits_and_currency_are_multiplied_not_estimated():
    meter = UsageMeter(usd_per_credit=0.01, usd_to_inr=90.0, plan="paid")
    meter.record("https://api.firecrawl.dev/v2/search", "firecrawl.search", units=8)
    meter.record("https://api.firecrawl.dev/v2/scrape", "firecrawl.scrape.json")

    totals = meter.summary()["totals"]
    # 8 search results at 1 credit + one json scrape at 5 credits.
    assert totals["billable_credits"] == 13
    assert totals["usd"] == pytest.approx(0.13)
    assert totals["inr"] == pytest.approx(11.70)


def test_a_cache_hit_costs_nothing_and_is_reported_as_saved():
    meter = UsageMeter(usd_per_credit=0.01, usd_to_inr=90.0, plan="paid")
    meter.record("https://api.firecrawl.dev/v2/scrape", "firecrawl.scrape.json")
    meter.record("https://api.firecrawl.dev/v2/scrape", "firecrawl.scrape.json",
                 cached=True)

    totals = meter.summary()["totals"]
    assert totals["billable_credits"] == 5
    assert totals["credits_saved_by_cache"] == 5
    assert totals["usd"] == pytest.approx(0.05)
    assert totals["usd_saved_by_cache"] == pytest.approx(0.05)


def test_the_meter_survives_parallel_screening():
    """Companies are screened on a thread pool; counters must not be lost."""
    meter = UsageMeter(usd_per_credit=0.01, usd_to_inr=90.0)

    def hit():
        for _ in range(200):
            meter.record("https://api.firecrawl.dev/v2/scrape", "firecrawl.scrape")

    threads = [threading.Thread(target=hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert meter.summary()["totals"]["requests"] == 1600
    assert meter.summary()["totals"]["billable_credits"] == 1600


# ---------------------------------------------------------------------------
# Final validation
# ---------------------------------------------------------------------------

def _row(**overrides):
    """A screening row that passes every check, for one field to be broken."""
    row = {
        "company_name": "Acme Industries",
        "sentiment": "positive",
        "negative_news_flag": False,
        "flags": [],
        "flag_categories": [],
        "flag_stages": [],
        "sentiment_breakdown": {"negative": 2, "neutral": 6, "positive": 8},
        "articles_reviewed": 16,
        "insufficient_coverage": False,
        "relationship_type": "control",
        "link_confidence": 0.9,
        "registry_id": "Q123",
        "source_url": "https://example.com/acme",
    }
    row.update(overrides)
    return row


def test_a_clean_row_validates_ok():
    verdict = validate.validate_company(_row(), identity_confirmed=True)
    assert verdict["status"] == "ok"
    assert verdict["issues"] == []


def test_a_flag_with_no_article_behind_it_is_withheld():
    """The product's claim is a source behind every finding."""
    row = _row(
        negative_news_flag=True,
        flags=[
            {"category": "fraud", "stage": "alleged", "evidence": []},
            {"category": "litigation", "stage": "alleged",
             "evidence": ["https://example.com/story"]},
        ],
    )
    verdict = validate.validate_company(row, identity_confirmed=True)

    assert [f["category"] for f in verdict["supported_flags"]] == ["litigation"]
    assert [f["category"] for f in verdict["withheld_flags"]] == ["fraud"]
    assert verdict["status"] == "uncertain"


def test_a_tone_verdict_on_a_thin_sample_is_qualified():
    """Tata Housing was marked negative on six articles, two of them negative."""
    row = _row(
        sentiment="negative",
        articles_reviewed=6,
        sentiment_breakdown={"negative": 2, "neutral": 3, "positive": 1},
    )
    verdict = validate.validate_company(row, identity_confirmed=True)
    assert verdict["status"] == "uncertain"
    assert any("6 articles" in issue for issue in verdict["issues"])


def test_all_one_tone_with_no_neutrals_is_called_out():
    """The shape promotional copy makes: Ford Credit, 13 of 13 positive."""
    row = _row(
        articles_reviewed=13,
        sentiment_breakdown={"negative": 0, "neutral": 0, "positive": 13},
    )
    verdict = validate.validate_company(row, identity_confirmed=True)
    assert verdict["status"] == "uncertain"
    assert any("promotional" in issue for issue in verdict["issues"])


def test_no_coverage_is_insufficient_not_neutral():
    row = _row(articles_reviewed=0, insufficient_coverage=True, sentiment="neutral")
    verdict = validate.validate_company(row, identity_confirmed=True)
    assert verdict["status"] == "insufficient_coverage"


def test_an_uncorroborated_link_is_unverified():
    row = _row(link_confidence=0.4, registry_id=None)
    verdict = validate.validate_company(row, identity_confirmed=True)
    assert verdict["status"] == "unverified"


def test_a_negative_verdict_with_no_negative_articles_is_a_contradiction():
    row = _row(
        sentiment="negative",
        sentiment_breakdown={"negative": 0, "neutral": 9, "positive": 9},
    )
    verdict = validate.validate_company(row, identity_confirmed=True)
    assert any("disagree" in issue for issue in verdict["issues"])


def test_an_unconfirmed_identity_taints_every_row():
    verdict = validate.validate_company(_row(), identity_confirmed=False)
    assert verdict["status"] == "unverified"
    assert any("identity was inferred" in issue for issue in verdict["issues"])


def test_validation_is_pure_and_re_runnable():
    """The refinement loop validates, retries, then validates again."""
    report = {
        "subject": {"identity_unverified": False},
        "screening": [_row(), _row(company_name="Beta Ltd", articles_reviewed=3,
                                   sentiment="negative")],
    }
    first = validate.validate_report(report)
    statuses = [r["validation"]["status"] for r in first["screening"]]
    second = validate.validate_report(first)

    assert [r["validation"]["status"] for r in second["screening"]] == statuses
    assert second["validation"]["companies_by_status"]["ok"] == 1


def test_retry_is_asked_for_only_where_a_new_query_would_help():
    # Thin coverage and single-tone coverage: a different query may help.
    assert validate.needs_more_news(_row(articles_reviewed=0))
    assert validate.needs_more_news(_row(articles_reviewed=4))
    assert validate.needs_more_news(_row(
        articles_reviewed=13,
        sentiment_breakdown={"negative": 0, "neutral": 0, "positive": 13},
    ))
    # A healthy spread needs nothing, and a misclassified relationship is not
    # something a news query can fix.
    assert not validate.needs_more_news(_row())
    assert not validate.needs_more_news(_row(relationship_type="philanthropy"))


def test_trial_credits_are_not_reported_as_money():
    """A free trial that reports "$0.33" is a fabricated number."""
    meter = UsageMeter(usd_per_credit=0.01, usd_to_inr=90.0, plan="trial")
    meter.record("https://api.firecrawl.dev/v2/scrape", "firecrawl.scrape.json")
    meter.record_tokens("candidate ranking", prompt=400, completion=132)

    summary = meter.summary()
    totals = summary["totals"]

    # The credits were really spent; the money was not.
    assert totals["billable_credits"] == 5
    assert totals["credits_usd"] == 0
    assert summary["firecrawl"]["plan"] == "trial"
    # OpenAI is real spend on any plan, and must survive the trial rule.
    assert totals["openai_usd"] > 0
    assert totals["usd"] == pytest.approx(totals["openai_usd"])


def test_the_meter_does_not_depend_on_the_env_file():
    """Two meters, same calls, different plans, different answers."""
    calls = [("https://api.firecrawl.dev/v2/scrape", "firecrawl.scrape.json")]
    paid = UsageMeter(usd_per_credit=0.01, plan="paid")
    trial = UsageMeter(usd_per_credit=0.01, plan="trial")
    for meter in (paid, trial):
        for url, kind in calls:
            meter.record(url, kind)

    assert paid.summary()["totals"]["credits_usd"] == pytest.approx(0.05)
    assert trial.summary()["totals"]["credits_usd"] == 0


# ---------------------------------------------------------------------------
# Entity resolution: the Adani Ports case
# ---------------------------------------------------------------------------

def test_ampersand_survives_normalisation():
    """"&" carries meaning. Stripping it split one company into three."""
    assert normalise("Adani Ports & SEZ") == normalise(
        "Adani Ports and Special Economic Zone Ltd"
    )


def test_known_acronyms_expand():
    assert "special economic zone" in normalise("Adani Ports & SEZ")
    assert normalise("Reliance Intl") == normalise("Reliance International")


def test_connectors_do_not_distinguish_a_name():
    assert normalise("Tata Sons and Company") == normalise("Tata Sons")


def test_the_short_form_is_absorbed_into_the_filed_name():
    """Three rows for Adani Ports became one."""
    merged = merge([
        {"name": "Adani Ports", "registry_id": None, "relationships": ["Promoter"],
         "sources": ["Firecrawl extraction"]},
        {"name": "Adani Ports & SEZ", "registry_id": "Q16058076",
         "relationships": ["chairperson"], "sources": ["Wikidata"]},
        {"name": "Adani Ports and Special Economic Zone Ltd", "registry_id": None,
         "relationships": ["founder"], "sources": ["Firecrawl extraction"]},
    ])

    assert len(merged) == 1
    entity = merged[0]
    assert entity["registry_id"] == "Q16058076"
    # Every role from every spelling survives on the single row.
    assert "Promoter" in entity["relationships"]
    assert "founder" in entity["relationships"]


def test_a_different_company_sharing_a_prefix_is_not_absorbed():
    """The guard. Wipro and Wipro Enterprises are two filed companies, and
    merging them would move one company's adverse news onto the other."""
    merged = merge([
        {"name": "Wipro", "registry_id": "Q211531", "sources": ["Wikidata"]},
        {"name": "Wipro Enterprises", "registry_id": "Q8027529",
         "sources": ["Wikidata"]},
    ])
    assert len(merged) == 2


def test_a_business_line_is_not_a_structural_word():
    """"Tata Motors Finance" names a different company from "Tata Motors"."""
    merged = merge([
        {"name": "Tata Motors", "registry_id": None, "sources": ["x"]},
        {"name": "Tata Motors Finance", "registry_id": None, "sources": ["x"]},
    ])
    assert len(merged) == 2


# ---------------------------------------------------------------------------
# One story, one company
# ---------------------------------------------------------------------------

def test_a_shared_story_is_counted_once_across_companies():
    """A group's flagship article is returned for every subsidiary."""
    shared = article("Adani Group faces SEBI questions - Reuters")
    seen = set()

    first = news.dedupe([shared, article("Adani Ports wins concession")], seen=seen)
    second = news.dedupe([shared, article("Adani Green adds capacity")], seen=seen)

    assert len(first) == 2
    # The shared story went to the first company and is not counted again.
    assert len(second) == 1
    assert "Green" in second[0].headline


def test_dedupe_without_a_shared_set_is_unchanged():
    duplicates = [article("Same story - Mint"), article("Same story - ET")]
    assert len(news.dedupe(duplicates)) == 1


# ---------------------------------------------------------------------------
# Non-corporate entities
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Carnegie Endowment for International Peace",
    "Bill & Melinda Gates Foundation",
    "Brookings Institution",
    "Council on Foreign Relations",
])
def test_a_think_tank_is_not_a_corporate_holding(name):
    assert relationship_type(name, ["board member", "Chairman"]) == "philanthropy"


def test_an_ordinary_company_is_untouched_by_the_wider_rule():
    assert relationship_type("Adani Ports", ["Chairman"]) == "control"
    assert relationship_type("Trust Fintech Limited", ["Founder"]) == "control"


# ---------------------------------------------------------------------------
# Tenure
# ---------------------------------------------------------------------------

def test_article_year_reads_rss_dates():
    assert tenure.article_year("Mon, 12 Feb 2024 10:30:00 GMT") == 2024
    assert tenure.article_year("2018-09-21T00:00:00Z") == 2018
    assert tenure.article_year(None) is None
    assert tenure.article_year("not a date") is None


@pytest.mark.parametrize("period,expected", [
    ("2018-present", (2018, None)),
    ("since 2018", (2018, None)),
    ("1991 to 2012", (1991, 2012)),
    ("Chairman (2018-2021)", (2018, 2021)),
    ("", (None, None)),
])
def test_role_period_is_parsed(period, expected):
    assert tenure.role_period({"period": period}) == expected


def test_articles_predating_the_role_cannot_flag_the_person():
    """IL&FS: appointed in 2018 to clean up a fraud that preceded him."""
    articles = [
        article("IL&FS fraud uncovered, auditors accused",
                published="Tue, 02 Oct 2018 08:00:00 GMT"),
        article("IL&FS defaults trigger crisis",
                published="Sat, 01 Sep 2012 08:00:00 GMT"),
    ]
    marked = tenure.mark_out_of_tenure(articles, {"period": "2018-present"})

    assert marked == 1
    assert articles[1].outside_tenure is True
    assert "before the subject's role began in 2018" in articles[1].tenure_note
    # The 2018 article is within tenure and still counts.
    assert tenure.attributable_articles(articles) == [articles[0]]


def test_an_undated_role_filters_nothing():
    """Guessing a start date would silently drop real findings."""
    articles = [article("Old story", published="Sat, 01 Sep 2012 08:00:00 GMT")]
    assert tenure.mark_out_of_tenure(articles, {}) == 0
    assert tenure.attributable_articles(articles) == articles


def test_an_undated_article_is_never_filtered():
    articles = [article("No date given", published=None)]
    assert tenure.mark_out_of_tenure(articles, {"period": "2018-present"}) == 0


@pytest.mark.parametrize("name", [
    "Trust Fintech Limited",
    "Apollo Hospitals Enterprise Limited",
    "Foundation Holdings Ltd",
    "Academy Sports Retail",
])
def test_a_commercial_business_line_overrides_a_charitable_word(name):
    """"Trust Fintech Limited" is a listed company, not a charity."""
    assert relationship_type(name, ["Founder"]) == "control"


@pytest.mark.parametrize("name", [
    "Tata Trusts",
    "Sir Dorabji Tata and Allied Trusts",
    "Carnegie Endowment for International Peace",
    "Sarala Birla Academy",
])
def test_a_real_charity_still_reads_as_philanthropy(name):
    assert relationship_type(name, ["Chairman"]) == "philanthropy"


# ---------------------------------------------------------------------------
# B1 — adverse-first retrieval
# ---------------------------------------------------------------------------

def test_every_company_gets_adverse_queries_not_just_its_name():
    """The bug that made BharatPe read clean: only one query was ever asked."""
    built = queries.for_company("BharatPe")
    groups = {q["group"] for q in built}

    assert "general" in groups
    assert {"fraud", "litigation", "regulatory", "investigation"} <= groups
    # Adverse first, so a cap truncates general coverage rather than a check.
    assert built[0]["group"] != "general"


def test_the_company_disambiguates_a_person_query():
    built = queries.for_person("Ashneer Grover", "BharatPe")
    adverse = [q for q in built if q["group"] != "general"]
    assert adverse and all('"BharatPe"' in q["query"] for q in adverse)


def test_query_sets_are_capped_and_reproducible():
    first = queries.for_company("Adani Enterprises", cap=5)
    second = queries.for_company("Adani Enterprises", cap=5)
    assert len(first) == 5
    assert [q["query"] for q in first] == [q["query"] for q in second]


def test_groups_checked_excludes_the_base_query():
    built = queries.for_company("Wipro")
    assert "general" not in queries.groups_checked(built)


# ---------------------------------------------------------------------------
# B2 — source tiering
# ---------------------------------------------------------------------------

def _art(url, publisher=None, headline="A headline", sentiment="neutral"):
    a = article(headline, url=url, publisher=publisher)
    a.sentiment = sentiment
    return a


def test_press_releases_are_excluded_from_evidence():
    articles = [
        _art("https://www.prnewswire.com/x", "PR Newswire"),
        _art("https://www.reuters.com/y", "Reuters"),
    ]
    publishers.annotate(articles)
    evidence, promotional = publishers.split_promotional(articles)

    assert [a.publisher for a in evidence] == ["Reuters"]
    assert len(promotional) == 1


def test_a_companys_own_domain_is_promotional():
    articles = [_art("https://adanigroup.com/news", "Adani Group")]
    publishers.annotate(articles, owner_domains={"adanigroup.com"})
    assert articles[0].is_promotional


def test_tiers_are_assigned_by_domain():
    reuters = _art("https://www.reuters.com/a", "Reuters")
    trade = _art("https://inc42.com/b", "Inc42")
    unknown = _art("https://somesite.example/c", "Some Site")
    publishers.annotate([reuters, trade, unknown])

    assert reuters.publisher_tier == publishers.MAJOR
    assert trade.publisher_tier == publishers.ESTABLISHED
    assert unknown.publisher_tier == publishers.UNKNOWN


def test_syndication_does_not_inflate_independent_sources():
    """Ten copies of one wire story are one source, not ten."""
    same = [_art(f"https://www.reuters.com/{i}", "Reuters") for i in range(10)]
    publishers.annotate(same)
    assert publishers.independent_publishers(same) == ["Reuters"]


def test_a_major_publisher_outweighs_an_unknown_one():
    """Adani read 26 positive / 0 negative because every source weighed alike."""
    promo_ish = [_art(f"https://blog.example/{i}", "Blog", sentiment="positive")
                 for i in range(6)]
    serious = [_art(f"https://www.reuters.com/{i}", "Reuters", sentiment="negative")
               for i in range(3)]
    publishers.annotate(promo_ish + serious)

    _counts, weighted = aggregate(promo_ish + serious)
    _counts2, unweighted = aggregate(promo_ish + serious, weighted=False)
    assert weighted == "negative"
    assert unweighted in ("negative", "neutral")


# ---------------------------------------------------------------------------
# B8 — relationship classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,roles,status,expected,attributes", [
    ("BharatPe", ["Co-founder", "Managing Director"], "former", "former_company", True),
    ("Third Unicorn", ["Co-founder"], "active", "current_company", True),
    ("ZeroPe", ["Board Member"], "active", "board_seat", True),
    ("Grofers", ["Chief Financial Officer"], "former", "former_employer", False),
    ("Amazon MX Player", ["Host"], "active", "media_role", False),
    ("Reserve Bank of India", ["Board Member"], "former", "regulator", False),
    ("Carnegie Endowment for International Peace", ["board member"], "former",
     "think_tank", False),
    ("Azim Premji Foundation", ["Founder"], "active", "nonprofit", False),
])
def test_relationships_are_classified_and_attributed(name, roles, status,
                                                     expected, attributes):
    found, _basis = relationships.classify(name, roles, status)
    assert found == expected
    assert relationships.carries_exposure(found) is attributes


def test_a_former_founder_role_still_attributes():
    """Timing is the tenure check's job. Gating it twice loses real findings:
    Grover left BharatPe before the complaint, and it is still about him."""
    found, _ = relationships.classify("BharatPe", ["Co-founder"], "former")
    assert relationships.carries_exposure(found)


def test_a_controlling_stake_outranks_the_job_title():
    found, basis = relationships.classify("HZL", ["shareholder (65%)"],
                                          "active", stake_percent=65.0)
    assert found == "current_company"
    assert "65%" in basis


def test_a_minority_stake_is_an_investment():
    found, _ = relationships.classify("Anglo American", ["shareholder (19%)"],
                                      "active", stake_percent=19.0)
    assert found == "investment"
    assert not relationships.carries_exposure(found)


def test_legacy_types_stay_valid_for_the_current_ui():
    for cls in (relationships.CARRIES_EXPOSURE
                + relationships.REPORTED_NOT_ATTRIBUTED + ("other",)):
        assert relationships.legacy_type(cls) in (
            "control", "public_office", "philanthropy", "employment", "unknown"
        )


# ---------------------------------------------------------------------------
# B6 — risk scoring
# ---------------------------------------------------------------------------

def _finding(**kw):
    defaults = dict(
        event_id="e1", what_happened="Something happened", entity="Acme",
        category="regulatory", stage="reported", event_date="2024-01-01",
        is_ongoing=False, severity=3, confidence=0.8,
        attributed_to_subject=True, within_tenure=True,
        corroborating_publishers=["Reuters", "Mint"],
        evidence=[EvidenceItem(url="u", publisher="Reuters", tier=1)],
    )
    defaults.update(kw)
    return Finding(**defaults)


COVERAGE = {"after_dedup": 300, "publishers": 40, "by_tier": {"tier 1": 90},
            "groups_checked": ["fraud", "litigation", "regulatory",
                               "investigation", "criminal", "insolvency",
                               "tax", "governance", "sanctions", "corruption"]}


def test_no_findings_is_low_risk_with_a_reason():
    result = risk_score.assess([], COVERAGE, identity_confirmed=True)
    assert result["risk_level"] == "LOW"
    assert "No material adverse findings" in result["headline"]
    # Not "0 negative articles": it must say what was checked.
    assert "adverse checks run" in result["headline"]


def test_an_ongoing_charge_for_fraud_is_critical():
    finding = _finding(category="fraud", stage="charged", is_ongoing=True,
                       severity=5,
                       corroborating_publishers=["Reuters", "Mint", "BS"])
    result = risk_score.assess([finding], COVERAGE, identity_confirmed=True)
    assert result["risk_level"] == "CRITICAL"
    assert result["recommended_action"] == "escalate"


def test_a_dismissed_matter_does_not_drive_the_headline():
    dismissed = _finding(category="fraud", stage="dismissed", severity=5)
    result = risk_score.assess([dismissed], COVERAGE, identity_confirmed=True)
    assert result["risk_level"] in ("LOW", "MEDIUM")


def test_an_unattributed_finding_scores_nothing():
    """A former employer's matter is reported, never counted as the person's."""
    other = _finding(attributed_to_subject=False, severity=5, stage="charged")
    assert risk_score.score_finding(other) == 0.0
    result = risk_score.assess([other], COVERAGE, identity_confirmed=True)
    assert result["risk_level"] == "LOW"


def test_an_out_of_tenure_finding_scores_nothing():
    """IL&FS: the fraud predated the chairman appointed to clean it up."""
    old = _finding(within_tenure=False, severity=5, stage="charged")
    assert risk_score.score_finding(old) == 0.0


def test_a_single_publisher_weakens_a_finding():
    many = _finding(corroborating_publishers=["Reuters", "Mint", "BS"])
    one = _finding(corroborating_publishers=["Reuters"])
    assert risk_score.score_finding(many) > risk_score.score_finding(one)


def test_confidence_is_separate_from_risk():
    """HIGH/HIGH and HIGH/LOW demand different actions from the reader."""
    finding = _finding(category="fraud", stage="charged", severity=5,
                       is_ongoing=True)
    thin = {"after_dedup": 12, "publishers": 3, "by_tier": {}, "groups_checked": []}

    rich = risk_score.assess([finding], COVERAGE, identity_confirmed=True)
    poor = risk_score.assess([finding], thin, identity_confirmed=False)

    assert rich["risk_level"] == poor["risk_level"]
    assert rich["confidence"] != poor["confidence"]


def test_a_clean_result_on_thin_evidence_is_not_a_clean_result():
    thin = {"after_dedup": 8, "publishers": 2, "by_tier": {}, "groups_checked": []}
    result = risk_score.assess([], thin, identity_confirmed=False)
    assert result["risk_level"] == "LOW"
    assert result["confidence"] == "LOW"
    assert result["recommended_action"] == "review"


def test_several_medium_matters_raise_the_level():
    findings = [
        _finding(event_id=f"e{i}", category="litigation", stage="investigating",
                 severity=3, is_ongoing=True)
        for i in range(3)
    ]
    result = risk_score.assess(findings, COVERAGE, identity_confirmed=True)
    assert result["risk_level"] == "HIGH"


def test_scoring_is_reproducible():
    finding = _finding(category="fraud", stage="charged", severity=4)
    first = risk_score.assess([finding], COVERAGE, identity_confirmed=True)
    second = risk_score.assess([finding], COVERAGE, identity_confirmed=True)
    assert first == second


def test_limitations_are_always_stated():
    result = risk_score.assess([], COVERAGE, identity_confirmed=True)
    assert any("not proof" in limit for limit in result["limitations"])


def test_near_total_one_sided_coverage_is_caught():
    """26 positive / 1 neutral / 0 negative slipped past the zero-neutrals rule."""
    row = _row(articles_reviewed=27,
               sentiment_breakdown={"negative": 0, "neutral": 1, "positive": 26})
    verdict = validate.validate_company(row, identity_confirmed=True)
    assert verdict["status"] == "uncertain"
    assert any("promotional" in issue for issue in verdict["issues"])
    assert validate.needs_more_news(row)


def test_healthy_mixed_coverage_is_not_flagged_as_promotional():
    row = _row(articles_reviewed=30,
               sentiment_breakdown={"negative": 3, "neutral": 13, "positive": 14})
    verdict = validate.validate_company(row, identity_confirmed=True)
    assert verdict["status"] == "ok"
    assert not validate.needs_more_news(row)


def test_a_firecrawl_only_link_is_flagged_unverified():
    """0.6 is exactly what a Firecrawl-only link scores, and a strict "<"
    meant the weakest rows in every report were invisible to the validator."""
    row = _row(link_confidence=0.6, registry_id=None)
    verdict = validate.validate_company(row, identity_confirmed=True)
    assert verdict["status"] == "unverified"


def test_a_registry_backed_link_still_passes():
    row = _row(link_confidence=0.9, registry_id="Q123")
    verdict = validate.validate_company(row, identity_confirmed=True)
    assert verdict["status"] == "ok"


# ---------------------------------------------------------------------------
# Redirect URLs: the cause of three separate bugs
# ---------------------------------------------------------------------------

def test_a_search_engine_is_not_a_publisher():
    """"Reported independently by 2 publishers — NDTV, bing.com" counted a
    redirector as a source, inflating the number that decides materiality."""
    a = article("Story", url="https://www.bing.com/news/apiclick.aspx?url=x")
    assert publishers.publisher_name(a) == ""

    named = article("Story", url="https://news.google.com/rss/articles/CBMi")
    named.publisher_url = "https://www.ndtv.com"
    assert publishers.publisher_name(named) == "ndtv.com"


def test_unidentifiable_publishers_do_not_corroborate():
    anonymous = [article("Story", url="https://www.bing.com/news/apiclick.aspx")
                 for _ in range(5)]
    real = article("Story", url="https://www.reuters.com/a", publisher="Reuters")
    publishers.annotate(anonymous + [real])
    assert publishers.independent_publishers(anonymous + [real]) == ["Reuters"]


def test_bing_links_are_unwrapped_to_the_real_article():
    wrapped = ("https://www.bing.com/news/apiclick.aspx?ref=FexRss&aid=&tid=abc"
               "&url=https%3A%2F%2Fwww.livemint.com%2Fstory-123&c=1")
    assert news._unwrap_bing(wrapped) == "https://www.livemint.com/story-123"
    # A link that is not a Bing wrapper is left alone.
    assert news._unwrap_bing("https://www.reuters.com/x") == "https://www.reuters.com/x"


def test_a_google_article_is_tiered_by_its_publisher_not_by_google():
    """Every article looked tier 3 because every URL was news.google.com."""
    a = article("Story", url="https://news.google.com/rss/articles/CBMi")
    a.publisher_url = "https://www.reuters.com"
    publishers.annotate([a])
    assert a.publisher_tier == publishers.MAJOR


def test_an_unwrapped_bing_article_becomes_fetchable():
    """Full articles read stayed at 0: every URL was flagged a redirect."""
    a = article("Firm probed by SEBI", url="https://www.livemint.com/story-123")
    a.url_is_redirect = False
    a.publisher_tier = publishers.MAJOR
    a.risk_categories = ["regulatory"]
    assert fulltext.worth_fetching(a)

    google = article("Firm probed by SEBI", url="https://news.google.com/rss/x")
    google.url_is_redirect = True
    google.publisher_tier = publishers.MAJOR
    google.risk_categories = ["regulatory"]
    assert not fulltext.worth_fetching(google)


# ---------------------------------------------------------------------------
# Accuser is not accused
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role,expected", [
    ("accused", True),
    ("defendant", True),
    ("accuser", False),
    ("plaintiff", False),
    ("commentator", False),
    ("unrelated", False),
])
def test_only_the_accused_is_attributed(role, expected):
    """"Grover claimed Koladiya committed data theft" made Grover the accuser,
    and was scored as an adverse finding about Grover."""
    a = article("Someone said something")
    extract._apply(a, {
        "is_about_target": True, "event_type": "fraud", "stage": "alleged",
        "subject_role_in_event": role,
        # The model asserted this; the role has to agree before it is believed.
        "actor_is_subject": True,
        "severity": 4, "confidence": 0.9,
    })
    assert a.extracted["actor_is_subject"] is expected
    assert a.extracted["subject_role_in_event"] == role


def test_articles_sharing_a_publication_date_do_not_crash_clustering(monkeypatch):
    """Sorting (date, Article) pairs compared Articles when dates tied, which
    syndicated copy about one matter does constantly. It crashed the whole run
    with "'<' not supported between instances of 'Article'"."""
    # The deterministic path: no key, so no model is consulted and the latest
    # dated account stands.
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")

    same_day = "Mon, 06 Mar 2020 10:00:00 GMT"
    articles = []
    for stage, publisher in (("charged", "Reuters"), ("dismissed", "Mint"),
                             ("investigating", "NDTV")):
        a = article("Bank founder matter", url=f"https://www.{publisher.lower()}.com/x",
                    published=same_day, publisher=publisher)
        a.risk_categories = ["fraud"]
        a.risk_stage = stage
        a.sentiment = "negative"
        articles.append(a)
    publishers.annotate(articles)

    findings = cluster.build_findings(
        None, articles, "Yes Bank", entity_role="Founder",
        relationship_type="current_company",
    )
    assert len(findings) == 1
    # The contradiction is recorded rather than silently resolved to whichever
    # article happened to be scored last.
    assert findings[0].contradicting_sources
    assert len(findings[0].corroborating_publishers) == 3
