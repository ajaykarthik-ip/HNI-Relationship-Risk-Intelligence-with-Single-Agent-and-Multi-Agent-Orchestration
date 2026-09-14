"""Tests for V2's quality layer.

Every fixture here is synthetic. No test references a real person or company,
because the point of this layer is that it generalises: a rule that only fixes
the subject it was written against is not a rule, it is a patch. What is tested
is the *shape* of each failure -- a matter spanning a year boundary, a headline
whose chargesheet excludes the subject, a sector used as a company name -- so a
subject nobody has searched yet gets the same treatment.

    pytest test_v2_quality.py -q
"""

from __future__ import annotations

import asyncio

import pytest

from affluense.models import Article, EvidenceItem, Finding
from affluense_v2.quality import attribution, confidence, entities, staging
from affluense_v2.quality import events as quality_events
from affluense_v2.quality.text import (containment, days_apart, iso,
                                       parse_date, similarity)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def article(headline="Regulator opens a matter at the company",
            url=None, publisher="Publisher One", published="2026-03-01",
            category="investigation", stage=None, extracted=None,
            summary=None, confidence_score=0.8, actors=None,
            role=None, actor_is_subject=None, tier=1, counter=[0]):
    """One synthetic article. Only the fields the quality layer reads."""
    counter[0] += 1
    item = Article(
        headline=headline,
        url=url or f"https://example{counter[0]}.test/story",
        source="Bing News RSS",
        source_url="https://bing.test",
        publisher=publisher,
        published=published,
    )
    item.publisher_name = publisher
    item.publisher_tier = tier
    item.risk_categories = [category] if category else []
    item.risk_stage = stage

    if extracted is not None:
        item.extracted = extracted
    elif summary is not None or actors is not None or role is not None:
        item.extracted = {
            "subject_role_in_event": role,
            "is_about_target": True,
            "is_journalism": True,
            "is_promotional": False,
            "category": category,
            "event_summary": summary,
            "event_date": published,
            "stage": stage,
            "actors": actors or [],
            "actor_is_subject": (
                actor_is_subject if actor_is_subject is not None
                else role in ("accused", "defendant")
            ),
            "severity": 3,
            "confidence": confidence_score,
            "supersedes_earlier_report": False,
            "reason": None,
        }
    return item


class FakeBridge:
    """Runs V1 callables inline. The contradiction path needs no network here."""

    class _Facade:
        def __init__(self):
            self.notes = []

        def note(self, message):
            self.notes.append(message)

    def __init__(self):
        self.facade = self._Facade()
        self.calls = 0

    async def run(self, fn, *args, **kwargs):
        self.calls += 1
        return fn(self.facade, *args, **kwargs)


def finding(event_id="e1", entity="Entity", category="fraud", stage="alleged",
            date="2026-03-01", publishers=("A",), severity=3,
            attributed=True, urls=("https://a.test/1",), summary="A matter",
            derived="extraction"):
    return Finding(
        event_id=event_id,
        what_happened=summary,
        entity=entity,
        category=category,
        stage=stage,
        event_date=date,
        severity=severity,
        attributed_to_subject=attributed,
        corroborating_publishers=list(publishers),
        derived_from=derived,
        evidence=[
            EvidenceItem(url=u, publisher="P", tier=1) for u in urls
        ],
    )


# ---------------------------------------------------------------------------
# text primitives
# ---------------------------------------------------------------------------

def test_rss_dates_are_parsed_not_left_raw():
    """A raw pubDate reaching the report renders beside ISO dates and looks
    like a bug, because it is one."""
    assert parse_date("Tue, 08 Sep 2026 11:24:15 GMT").isoformat() == "2026-09-08"
    assert iso("Tue, 08 Sep 2026 11:24:15 GMT") == "2026-09-08"


def test_partial_iso_dates_are_accepted():
    assert parse_date("2026").isoformat() == "2026-01-01"
    assert parse_date("2026-07").isoformat() == "2026-07-01"
    assert parse_date("2026-07-14").isoformat() == "2026-07-14"


def test_unparseable_dates_do_not_raise():
    assert parse_date(None) is None
    assert parse_date("") is None
    assert parse_date("sometime last year") is None
    assert days_apart("nonsense", "2026-01-01") is None


def test_similarity_recognises_shared_content():
    a = "Regulator fined the operator 240 crore over disclosure failures"
    b = "Operator fined 240 crore by regulator for disclosure failures"
    assert similarity(a, b) > 0.5
    assert containment(a, b) > 0.7


def test_similarity_separates_unrelated_matters():
    a = "Regulator fined the operator over disclosure failures"
    b = "Court dismissed a land dispute brought by a former supplier"
    assert similarity(a, b) < 0.2


# ---------------------------------------------------------------------------
# staging
# ---------------------------------------------------------------------------

def test_keyword_chargesheet_headline_cannot_establish_charged():
    """A regex cannot tell who a chargesheet names.

    This is the generic form of the failure: a headline containing
    "chargesheet" alongside wording that *excludes* the subject was read as the
    subject being charged, because the pattern has no notion of an actor.
    """
    item = article(
        headline="Agency files chargesheet against three executives, "
                 "excluding the chairman",
        stage="charged",          # what risk.detect_stage would return
        category="fraud",
    )
    assert item.extracted is None
    assert staging.stage_of(item) == "alleged"


def test_keyword_notice_does_not_become_an_investigation():
    """`investigating` matches a bare "notice", so a tax notice became a probe."""
    item = article(headline="Company receives a tax notice",
                   stage="investigating", category="regulatory")
    assert staging.stage_of(item) == "alleged"


def test_model_read_stage_is_trusted():
    item = article(stage="charged", summary="Charged with an offence",
                   role="accused", confidence_score=0.9)
    assert staging.is_grounded(item)
    assert staging.stage_of(item) == "charged"


def test_low_confidence_model_read_is_not_grounded():
    item = article(stage="charged", summary="Possibly charged",
                   role="accused", confidence_score=0.2)
    assert not staging.is_grounded(item)
    assert staging.stage_of(item) == "alleged"


def test_de_escalating_keyword_stages_pass_through():
    """Being wrong towards 'dismissed' under-states our certainty, never the
    subject's exposure, so the keyword is trusted in that direction."""
    for stage in ("dismissed", "settled"):
        item = article(stage=stage, category="litigation")
        assert staging.stage_of(item) == stage


def test_charged_requires_two_independent_publishers():
    single = [article(stage="charged", summary="Charged", role="accused",
                      publisher="Only Outlet")]
    stage, notes = staging.resolve(single)
    assert stage != "charged"
    assert notes and "required" in notes[0]


def test_charged_is_allowed_once_corroborated():
    group = [
        article(stage="charged", summary="Charged", role="accused",
                publisher="Outlet One"),
        article(stage="charged", summary="Charged", role="accused",
                publisher="Outlet Two"),
    ]
    stage, _ = staging.resolve(group)
    assert stage == "charged"


def test_one_stray_article_does_not_lift_the_whole_cluster():
    """V1 took max(stages); nineteen allegations were outranked by one headline."""
    group = [
        article(stage="alleged", summary="Allegation", role="accused",
                publisher=f"Outlet {i}")
        for i in range(19)
    ]
    group.append(article(stage="convicted", summary="Convicted",
                         role="accused", publisher="Lone Outlet"))
    stage, _ = staging.resolve(group)
    assert stage != "convicted"


def test_dismissal_outranks_open_stages():
    group = [
        article(stage="alleged", summary="Allegation", role="accused",
                publisher="Outlet One"),
        article(stage="dismissed", summary="Cleared", role="accused",
                publisher="Outlet Two", published="2026-06-01"),
    ]
    stage, _ = staging.resolve(group)
    assert stage == "dismissed"


def test_conviction_survives_a_dismissal_elsewhere():
    """A conviction and a dismissal in one group is a contradiction, not a
    clearance, and must not be silently resolved to 'cleared'."""
    group = [
        article(stage="convicted", summary="Convicted", role="accused",
                publisher="Outlet One"),
        article(stage="convicted", summary="Convicted", role="accused",
                publisher="Outlet Two"),
        article(stage="dismissed", summary="Separate matter dropped",
                role="accused", publisher="Outlet Three"),
    ]
    stage, _ = staging.resolve(group)
    assert stage == "convicted"


def test_empty_group_is_reported():
    assert staging.resolve([]) == ("reported", [])


# ---------------------------------------------------------------------------
# attribution
# ---------------------------------------------------------------------------

SUBJECT = "Alexandra Whitfield"


def test_subject_named_as_the_party_is_attributed():
    group = [article(role="accused", summary="Accused of an offence",
                     actors=[SUBJECT])]
    attributed, reason = attribution.decide(group, True, SUBJECT)
    assert attributed
    assert "names the subject" in reason


def test_accuser_is_not_attributed():
    """'X says Y committed fraud' makes X the accuser, not the accused."""
    group = [article(role="accuser", summary="Alleges wrongdoing by a partner",
                     actors=["Someone Else"], actor_is_subject=False)]
    attributed, reason = attribution.decide(group, True, SUBJECT)
    assert not attributed
    assert "accuser" in reason


def test_commentator_is_not_attributed():
    group = [article(role="commentator", summary="Comments on a matter",
                     actors=["Another Person"], actor_is_subject=False)]
    attributed, _ = attribution.decide(group, True, SUBJECT)
    assert not attributed


def test_another_individuals_act_is_not_the_subjects_exposure():
    """The generic form of a co-founder's alleged act at a company the subject
    merely invested in being reported as the subject's finding."""
    group = [article(summary="A co-founder is accused of forging signatures",
                     actors=["Marcus Delgado"], role=None,
                     actor_is_subject=False)]
    attributed, reason = attribution.decide(group, True, SUBJECT)
    assert not attributed
    assert "Marcus Delgado" in reason
    assert "context for the company" in reason


def test_company_matter_with_no_named_individual_is_attributed():
    """A regulatory penalty against the company is carried by whoever controls
    it. Exposure is the right rule here; it was only wrong when the evidence
    named somebody else."""
    group = [article(summary="The company was penalised by the regulator",
                     actors=[], role=None, actor_is_subject=False)]
    attributed, reason = attribution.decide(group, True, SUBJECT)
    assert attributed
    assert "company" in reason


def test_no_exposure_means_no_attribution():
    group = [article(summary="A matter at a former employer",
                     actors=[], role=None, actor_is_subject=False)]
    attributed, _ = attribution.decide(group, False, SUBJECT)
    assert not attributed


def test_partial_name_match_counts_as_the_subject():
    assert attribution.names_subject(["A. Whitfield"], SUBJECT)
    assert attribution.names_subject(["Alexandra Whitfield-Brown"], SUBJECT)


def test_initials_do_not_match_everyone():
    assert not attribution.names_subject(["R. K."], SUBJECT)
    assert not attribution.names_subject(["Marcus Delgado"], SUBJECT)


# ---------------------------------------------------------------------------
# entities
# ---------------------------------------------------------------------------

def test_bare_collective_noun_is_not_a_company():
    ok, reason = entities.classify("entertainment")
    assert not ok
    assert "collective noun" in reason


def test_multiword_industry_phrase_is_not_a_company():
    assert not entities.is_organisation("global fintech")
    assert not entities.is_organisation("the hospitality industry")


def test_a_name_used_as_a_sector_elsewhere_is_not_a_company():
    """The structural signal: the extraction itself called it an industry.

    This needs no vocabulary and adapts to domains nobody anticipated.
    """
    vocabulary = entities.sector_vocabulary(
        [{"sectors": ["Speciality Widgets"]}]
    )
    ok, reason = entities.classify("Speciality Widgets", sectors=vocabulary)
    assert not ok
    assert "sector" in reason


def test_legal_form_always_wins():
    for name in ("Northwind Trading Ltd", "Helios Systems Pvt Ltd",
                 "Meridian Holdings LLP"):
        assert entities.is_organisation(name)


def test_nonprofits_and_associations_are_preserved():
    """A naive company filter drops these. They are real organisations, and
    V1 already classifies them as non-exposure relationships downstream."""
    for name in ("Brightpath Foundation", "Karan Family Trust",
                 "National Widget Association", "Fairhaven Institute",
                 "Riverside University", "Hollowmere Football Club"):
        assert entities.is_organisation(name), name


def test_legal_form_survives_a_sector_collision():
    vocabulary = {"technology"}
    assert entities.is_organisation("Technology Partners Ltd", sectors=vocabulary)


def test_partition_moves_rejects_and_records_why():
    records = [
        {"name": "Northwind Trading Ltd", "sectors": ["logistics"]},
        {"name": "logistics", "sectors": []},
    ]
    kept, rejected = entities.partition(records)
    assert [r["name"] for r in kept] == ["Northwind Trading Ltd"]
    assert len(rejected) == 1
    assert rejected[0]["excluded_reason"]
    # The reason travels with the record: a filter that cannot explain itself
    # is one nobody will trust the next time it is wrong.
    assert "Kept as an affiliation" in rejected[0]["link_basis"]
    assert "logistics" in rejected[0]["link_basis"]


def test_empty_and_blank_names_are_rejected_safely():
    assert not entities.is_organisation("")
    assert not entities.is_organisation("   ")
    assert entities.partition([]) == ([], [])


# ---------------------------------------------------------------------------
# events: clustering
# ---------------------------------------------------------------------------

def test_matter_spanning_a_year_boundary_is_one_finding():
    """Nothing happened between 28 December and 4 January except New Year."""
    group = [
        article(published="2025-12-28", category="fraud", stage="alleged",
                summary="Agency opened a case over disclosure failures",
                role="accused", publisher="Outlet One"),
        article(published="2026-01-04", category="fraud", stage="alleged",
                summary="Agency case over disclosure failures continues",
                role="accused", publisher="Outlet Two"),
    ]
    clusters = quality_events.group_events(group)
    assert len(clusters) == 1


def test_one_event_described_with_two_category_words_is_one_finding():
    group = [
        article(published="2026-05-10", category="sanctions",
                summary="Settlement reached over the export matter",
                role="accused", publisher="Outlet One"),
        article(published="2026-05-15", category="other",
                summary="Settlement reached over the export matter",
                role="accused", publisher="Outlet Two"),
    ]
    clusters = quality_events.group_events(group)
    assert len(clusters) == 1


def test_genuinely_distinct_matters_stay_separate():
    """Over-merging under-counts. The window has to be narrow enough that two
    real events at one entity are not collapsed."""
    group = [
        article(published="2021-02-01", category="fraud",
                summary="Case opened over a supplier contract",
                role="accused", publisher="Outlet One"),
        article(published="2026-09-01", category="fraud",
                summary="Unrelated case opened over a property transfer",
                role="accused", publisher="Outlet Two"),
    ]
    clusters = quality_events.group_events(group)
    assert len(clusters) == 2


def test_same_category_far_apart_is_not_merged():
    group = [
        article(published="2020-01-01", category="litigation",
                summary="A dispute with a contractor", role="accused"),
        article(published="2026-01-01", category="litigation",
                summary="A dispute with a landlord", role="accused"),
    ]
    assert len(quality_events.group_events(group)) == 2


def test_findings_carry_normalised_dates():
    group = [article(published="Tue, 08 Sep 2026 11:24:15 GMT",
                     category="fraud", summary="A matter", role="accused")]
    findings = run(quality_events.build_findings(
        FakeBridge(), group, "Entity", "role", "current_company",
        subject_name=SUBJECT,
    ))
    assert findings[0].event_date == "2026-09-08"


def test_event_ids_are_unique():
    group = [
        article(published="2020-01-01", category="litigation",
                summary="A dispute with a contractor", role="accused"),
        article(published="2020-01-20", category="litigation",
                summary="A dispute with a contractor", role="accused"),
        article(published="2026-01-01", category="litigation",
                summary="A different dispute entirely, about a lease",
                role="accused"),
    ]
    findings = run(quality_events.build_findings(
        FakeBridge(), group, "Entity", "role", "current_company",
        subject_name=SUBJECT,
    ))
    ids = [f.event_id for f in findings]
    assert len(ids) == len(set(ids))


def test_build_findings_applies_staging_and_attribution():
    """End to end: a single-publisher charge claim about someone else must not
    arrive as a charged finding attributed to the subject."""
    group = [article(
        published="2026-04-01", category="fraud", stage="charged",
        summary="A director was charged over an invoice scheme",
        actors=["Marcus Delgado"], role=None, actor_is_subject=False,
        publisher="Lone Outlet",
    )]
    findings = run(quality_events.build_findings(
        FakeBridge(), group, "Entity", "role", "current_company",
        subject_name=SUBJECT,
    ))
    assert len(findings) == 1
    assert findings[0].stage != "charged"
    assert not findings[0].attributed_to_subject


def test_bare_keyword_mentions_do_not_become_findings():
    item = article(headline="Company opens a new office", category=None,
                   stage=None)
    findings = run(quality_events.build_findings(
        FakeBridge(), [item], "Entity", "role", "current_company",
        subject_name=SUBJECT,
    ))
    assert findings == []


# ---------------------------------------------------------------------------
# events: cross-entity merge
# ---------------------------------------------------------------------------

def test_shared_source_document_merges_scopes():
    """One article cannot be two events, however many scopes it was found in."""
    merged = quality_events.merge_across_entities([
        finding(event_id="person-other-202605", entity="A Person",
                urls=("https://shared.test/story",), publishers=("A",)),
        finding(event_id="company-sanctions-202605", entity="A Company Ltd",
                urls=("https://shared.test/story",), publishers=("B",)),
    ])
    assert len(merged) == 1
    assert set(merged[0].corroborating_publishers) == {"A", "B"}


def test_similar_summaries_merge_across_scopes():
    merged = quality_events.merge_across_entities([
        finding(event_id="a", entity="A Person", date="2026-05-10",
                summary="Settlement of 275 million reached with the regulator",
                urls=("https://one.test/x",)),
        finding(event_id="b", entity="A Company Ltd", date="2026-05-20",
                summary="Regulator settlement of 275 million reached",
                urls=("https://two.test/y",)),
    ])
    assert len(merged) == 1


def test_unrelated_findings_are_not_merged():
    merged = quality_events.merge_across_entities([
        finding(event_id="a", entity="A Person", category="fraud",
                summary="Case opened over a supplier contract",
                urls=("https://one.test/x",)),
        finding(event_id="b", entity="A Company Ltd", category="insolvency",
                summary="Tribunal admitted an insolvency petition",
                urls=("https://two.test/y",)),
    ])
    assert len(merged) == 2


def test_merge_unions_attribution():
    merged = quality_events.merge_across_entities([
        finding(event_id="a", attributed=False, urls=("https://s.test/1",),
                publishers=("A",)),
        finding(event_id="b", attributed=True, urls=("https://s.test/1",),
                publishers=("B",)),
    ])
    assert len(merged) == 1
    assert merged[0].attributed_to_subject


def test_merge_does_not_smuggle_in_an_uncorroborated_stage():
    """A stage that failed its corroboration test in one scope must not arrive
    through the back door because two scopes were combined."""
    merged = quality_events.merge_across_entities([
        finding(event_id="a", stage="alleged", publishers=("A",),
                urls=("https://s.test/1",)),
        finding(event_id="b", stage="convicted", publishers=("A",),
                urls=("https://s.test/1",)),
    ])
    assert len(merged) == 1
    assert merged[0].stage == "alleged"


def test_merge_allows_a_stage_the_combined_evidence_supports():
    merged = quality_events.merge_across_entities([
        finding(event_id="a", stage="charged", publishers=("A",),
                urls=("https://s.test/1",)),
        finding(event_id="b", stage="charged", publishers=("B",),
                urls=("https://s.test/1",)),
    ])
    assert merged[0].stage == "charged"


def test_merge_is_stable_on_an_empty_list():
    assert quality_events.merge_across_entities([]) == []


# ---------------------------------------------------------------------------
# confidence
# ---------------------------------------------------------------------------

def _report(findings, coverage=None, screening=None, level="HIGH"):
    return {
        "findings": findings,
        "coverage": coverage or {"after_dedup": 100, "fulltext_fetched": 50},
        "screening": screening or [{"registry_id": "Q1"}],
        "assessment": {"confidence": level, "confidence_basis": [],
                       "risk_level": "HIGH"},
    }


def _row(material=True, publishers=("A", "B"), derived="extraction",
         attributed=True):
    return {
        "is_material": material,
        "attributed_to_subject": attributed,
        "corroborating_publishers": list(publishers),
        "derived_from": derived,
    }


def test_single_sourced_material_findings_lower_confidence():
    report = _report([_row(publishers=("A",)), _row(publishers=("B",))])
    confidence.cap(report)
    assert report["assessment"]["confidence"] == "MEDIUM"
    assert any("single publisher" in b
               for b in report["assessment"]["confidence_basis"])


def test_keyword_derived_findings_lower_confidence():
    report = _report([_row(derived="keyword"), _row(derived="keyword")])
    confidence.cap(report)
    assert report["assessment"]["confidence"] == "MEDIUM"


def test_headline_only_coverage_lowers_confidence():
    report = _report(
        [_row()],
        coverage={"after_dedup": 400, "fulltext_fetched": 4},
    )
    confidence.cap(report)
    assert report["assessment"]["confidence"] == "MEDIUM"
    assert any("read in full" in b
               for b in report["assessment"]["confidence_basis"])


def test_unverified_company_rows_lower_confidence():
    report = _report(
        [_row()],
        screening=[{"registry_id": None}, {"registry_id": None},
                   {"registry_id": "Q1"}],
    )
    confidence.cap(report)
    assert report["assessment"]["confidence"] == "MEDIUM"


def test_triggers_stack():
    report = _report(
        [_row(publishers=("A",), derived="keyword")],
        coverage={"after_dedup": 400, "fulltext_fetched": 1},
        screening=[{"registry_id": None}],
    )
    confidence.cap(report)
    assert report["assessment"]["confidence"] == "LOW"


def test_strong_evidence_is_left_alone():
    report = _report(
        [_row(publishers=("A", "B", "C"))],
        coverage={"after_dedup": 100, "fulltext_fetched": 40},
        screening=[{"registry_id": "Q1"}],
    )
    confidence.cap(report)
    assert report["assessment"]["confidence"] == "HIGH"
    assert report["assessment"]["confidence_basis"] == []


def test_confidence_is_never_raised():
    """The cap is a brake. V1's rubric stays the ceiling, always."""
    report = _report(
        [_row(publishers=("A", "B", "C"))],
        coverage={"after_dedup": 100, "fulltext_fetched": 90},
        level="LOW",
    )
    confidence.cap(report)
    assert report["assessment"]["confidence"] == "LOW"


def test_cap_records_where_it_came_from():
    report = _report([_row(publishers=("A",))])
    confidence.cap(report)
    assert report["assessment"]["confidence_capped_from"] == "HIGH"


def test_cap_floors_at_low():
    report = _report(
        [_row(publishers=("A",), derived="keyword")],
        coverage={"after_dedup": 400, "fulltext_fetched": 0},
        screening=[{"registry_id": None}],
        level="LOW",
    )
    confidence.cap(report)
    assert report["assessment"]["confidence"] == "LOW"


def test_cap_survives_a_report_with_no_assessment():
    assert confidence.cap({"findings": []}) == {"findings": []}


def test_no_findings_means_no_finding_based_penalty():
    report = _report([], coverage={"after_dedup": 100, "fulltext_fetched": 50})
    confidence.cap(report)
    assert report["assessment"]["confidence"] == "HIGH"


# ---------------------------------------------------------------------------
# Name and role quality
# ---------------------------------------------------------------------------

from affluense_v2.quality import people as quality_people  # noqa: E402


def test_a_job_title_in_the_name_means_no_individual_was_named():
    """Headlines that never name the person arrive as a position instead."""
    for name in ("Zepto CEO", "Mamaearth CEO", "Acme Founder",
                 "Northwind Chairman"):
        assert not quality_people.looks_like_person(name), name


def test_real_names_survive_the_title_check():
    for name in ("Dana Okonkwo", "Mary Alice Stephenson", "Tomas Lindqvist"):
        assert quality_people.looks_like_person(name), name


def test_a_masthead_of_common_nouns_is_not_a_person():
    """Two capitalised tokens, no legal form -- neither the form check nor the
    word count catches it, but every token is a common noun."""
    for name in ("Business Standard", "Daily Mirror", "National Herald",
                 "Global Capital"):
        assert not quality_people.looks_like_person(name), name


def test_weak_roles_are_dropped_from_the_subject_profile():
    roles = ["Founder", "Consultant", "Chief Executive", "Mentor", "Member"]
    kept = quality_people.meaningful_roles(roles)
    assert kept == ["Founder", "Chief Executive"]


def test_meaningful_roles_is_safe_on_junk():
    assert quality_people.meaningful_roles(None) == []
    assert quality_people.meaningful_roles(["", "   ", None]) == []


def test_a_qualified_consultant_title_is_kept():
    """Only the bare generic word is dropped; a real title survives."""
    assert quality_people.meaningful_roles(["Chief Consultant"]) == \
        ["Chief Consultant"]


# ---------------------------------------------------------------------------
# Clustering: distance alone cannot separate events, wording can
# ---------------------------------------------------------------------------

def test_one_matter_followed_over_months_is_one_finding():
    group = [
        article(published="2026-03-02", category="fraud",
                summary="Agency opened a bank fraud case over the branch deposits",
                role="accused", publisher="Outlet One"),
        article(published="2026-07-14", category="fraud",
                summary="Bank fraud case over the branch deposits continues",
                role="accused", publisher="Outlet Two"),
    ]
    assert len(quality_events.group_events(group)) == 1


def test_two_demands_for_different_amounts_stay_separate():
    """The generic risk of widening the window: distinct matters of the same
    kind at one company, months apart, must not collapse into one."""
    group = [
        article(published="2026-02-05", category="regulatory",
                summary="Company faces a 46 crore goods and services tax demand",
                role="accused", publisher="Outlet One"),
        article(published="2026-04-03", category="regulatory",
                summary="Income tax department issues a 110 crore demand notice",
                role="accused", publisher="Outlet Two"),
    ]
    assert len(quality_events.group_events(group)) == 2


# ---------------------------------------------------------------------------
# An uncorroborated seed company is reported but never attributed
# ---------------------------------------------------------------------------

def test_uncorroborated_seed_company_is_not_the_subjects_exposure():
    """Pairing a person with a company nothing connects them to must not
    produce adverse findings about that person."""
    group = [article(
        published="2026-05-01", category="fraud",
        summary="A founder was convicted over a funds transfer",
        actors=[], role=None, actor_is_subject=False, publisher="Outlet One",
    )]
    findings = run(quality_events.build_findings(
        FakeBridge(), group, "Some Company Ltd", "supplied with the query",
        "uncorroborated_seed", subject_name=SUBJECT,
    ))
    assert len(findings) == 1, "the matter is still reported"
    assert not findings[0].attributed_to_subject
    assert not findings[0].is_material


def test_a_corroborated_company_still_attributes():
    group = [article(
        published="2026-05-01", category="fraud",
        summary="The company was penalised by the regulator",
        actors=[], role=None, actor_is_subject=False, publisher="Outlet One",
    )]
    findings = run(quality_events.build_findings(
        FakeBridge(), group, "Some Company Ltd", "Director", "current_company",
        subject_name=SUBJECT,
    ))
    assert findings[0].attributed_to_subject
