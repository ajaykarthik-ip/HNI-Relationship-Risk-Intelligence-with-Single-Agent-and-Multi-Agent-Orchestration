"""Tests for V2's PS2 additions.

Synthetic throughout. PS2's failure mode was not a wrong answer but an empty
one: every candidate path went through Wikidata, so when the Query Service
refused a request the deliverable was a blank list. What is tested here is that
the second path produces candidates in the shape V1's scorer already consumes,
and that industry matching still works when nothing resolved to a registry.

    pytest test_v2_network.py -q
"""

from __future__ import annotations

import asyncio

from affluense.network import scoring
from affluense_v2.network import peers


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Industry identity without a registry
# ---------------------------------------------------------------------------

def test_sector_keys_are_stable_and_normalised():
    assert peers.sector_key("Payments") == peers.sector_key("  payments  ")
    assert peers.sector_key("Football Club") == "sector:football club"


def test_sector_profile_builds_industry_ids_from_company_sectors():
    """When Wikidata gives no Q-numbers, the sectors the extraction named are
    still a real industry signal."""
    companies = [
        {"name": "Northwind Trading Ltd", "sectors": ["logistics"]},
        {"name": "Helios Systems Pvt Ltd", "sectors": ["payments", "logistics"]},
    ]
    ids, labels = peers.sector_profile(companies, {"industries": []})
    assert ids == ["sector:logistics", "sector:payments"]
    assert labels["sector:logistics"] == "logistics"


def test_sector_profile_includes_industries_already_on_the_profile():
    ids, labels = peers.sector_profile([], {"industries": ["Renewables"]})
    assert "sector:renewables" in ids
    assert labels["sector:renewables"] == "Renewables"


def test_sector_profile_is_empty_when_nothing_is_known():
    assert peers.sector_profile([], {"industries": []}) == ([], {})


def test_scorer_treats_sector_keys_exactly_like_q_numbers():
    """The scorer intersects two sets and never inspects the values, which is
    what makes a string key work in place of a registry identifier."""
    profile = {
        "roles": ["Chief Executive"],
        "industry_qids": ["sector:payments"],
        "industry_labels": {"sector:payments": "payments"},
        "industries": ["payments"],
        "countries": [],
        "company_names": [],
    }
    candidate = {
        "name": "Dana Okonkwo",
        "roles": ["Chief Executive"],
        "companies": ["Meridian Payments Ltd"],
        "matched_industries": ["sector:payments"],
        "country": None,
        "sitelinks": 0,
        "source": "News coverage",
        "source_url": "https://example.test/story",
    }
    ranked = scoring.rank([candidate], profile, network=[], limit=10)
    assert len(ranked) == 1
    assert ranked[0]["relevance_score"] > 0
    assert ranked[0]["score_components"]["industry_overlap"] > 0
    assert ranked[0]["score_components"]["role_overlap"] == 1.0


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------

def test_queries_cover_industry_by_role():
    labels = {"sector:payments": "payments", "sector:logistics": "logistics"}
    built = peers.build_queries(labels, ["Chief Executive"],
                                industries=2, role_count=2)
    assert len(built) == 4
    assert all(spec["industry_label"] in ("payments", "logistics")
               for spec in built)
    assert any("Chief Executive" in spec["query"] for spec in built)


def test_subject_roles_are_tried_before_generic_seniority():
    labels = {"sector:payments": "payments"}
    built = peers.build_queries(labels, ["Managing Partner"],
                                industries=1, role_count=3)
    assert built[0]["role"] == "Managing Partner"


def test_queries_fall_back_to_generic_seniority_without_subject_roles():
    labels = {"sector:payments": "payments"}
    built = peers.build_queries(labels, [], industries=1, role_count=2)
    assert len(built) == 2
    assert all(spec["role"] for spec in built)


def test_no_industries_means_no_queries():
    assert peers.build_queries({}, ["CEO"], 3, 3) == []


# ---------------------------------------------------------------------------
# Discovery behaviour
# ---------------------------------------------------------------------------

class FakeTransport:
    def __init__(self):
        self.notes = []

    def note(self, message):
        self.notes.append(message)


class FakeReporter:
    def say(self, message):
        pass


def test_discovery_is_a_noop_without_industries():
    found = run(peers.discover(
        FakeTransport(), FakeReporter(), budget=None,
        profile={"industry_labels": {}, "roles": []}, subject_name="A Person",
    ))
    assert found == []


def test_discovery_declines_rather_than_guessing_without_a_key(monkeypatch):
    """Reading a person out of a headline needs comprehension. A regex over
    capitalised words returns agencies, publishers and place names, so
    returning nothing is the correct degraded behaviour."""
    from affluense import config as v1config
    from affluense_v2.budgets import Budget

    monkeypatch.setattr(v1config, "OPENAI_API_KEY", "")
    transport = FakeTransport()
    found = run(peers.discover(
        transport, FakeReporter(), Budget("peers", 5),
        profile={"industry_labels": {"sector:payments": "payments"},
                 "roles": ["CEO"]},
        subject_name="A Person",
    ))
    assert found == []
    assert any("OPENAI_API_KEY" in note for note in transport.notes)


def test_merge_accumulates_one_row_per_person():
    pool = {}
    peers._merge(pool, {
        "name": "Dana Okonkwo", "roles": ["CEO"],
        "companies": ["Meridian Payments Ltd"],
        "matched_industries": ["sector:payments"],
    })
    peers._merge(pool, {
        "name": "dana okonkwo", "roles": ["Founder"],
        "companies": ["Northwind Trading Ltd"],
        "matched_industries": ["sector:logistics"],
    })
    assert len(pool) == 1
    row = next(iter(pool.values()))
    assert sorted(row["roles"]) == ["CEO", "Founder"]
    assert len(row["companies"]) == 2
    assert len(row["matched_industries"]) == 2


# ---------------------------------------------------------------------------
# Output contract — PS2's actual deliverable
# ---------------------------------------------------------------------------

def test_ranked_output_carries_name_company_and_relevance_score():
    """PS2 asks for exactly these three fields in JSON or CSV."""
    profile = {
        "roles": ["Founder"], "industry_qids": ["sector:fintech"],
        "industry_labels": {"sector:fintech": "fintech"},
        "industries": ["fintech"], "countries": [], "company_names": [],
    }
    candidates = [
        {"name": "Priya Raghunathan", "roles": ["Founder"],
         "companies": ["Helios Systems Pvt Ltd"],
         "matched_industries": ["sector:fintech"], "country": "Singapore",
         "sitelinks": 0, "source": "News coverage",
         "source_url": "https://example.test/a"},
        {"name": "Tomas Lindqvist", "roles": ["Chief Executive"],
         "companies": ["Northwind Trading Ltd"], "matched_industries": [],
         "country": None, "sitelinks": 0, "source": "News coverage",
         "source_url": "https://example.test/b"},
    ]
    ranked = scoring.rank(candidates, profile, network=[], limit=10)
    assert len(ranked) == 2
    for row in ranked:
        assert row["name"]
        assert "company" in row
        assert isinstance(row["relevance_score"], float)
        assert 0.0 <= row["relevance_score"] <= 1.0
        assert row["source_url"]
    # Ordered best first.
    assert ranked[0]["relevance_score"] >= ranked[1]["relevance_score"]
    # The closer match wins on role and industry.
    assert ranked[0]["name"] == "Priya Raghunathan"


def test_people_already_in_the_network_are_not_suggested():
    profile = {
        "roles": ["Founder"], "industry_qids": [], "industry_labels": {},
        "industries": [], "countries": [], "company_names": [],
    }
    candidate = {"name": "Dana Okonkwo", "roles": ["Founder"],
                 "companies": [], "matched_industries": [], "country": None,
                 "sitelinks": 0, "source": "News coverage", "source_url": "x"}
    network = [{"name": "Dana Okonkwo", "company": None}]
    assert scoring.rank([candidate], profile, network, limit=10) == []


# ---------------------------------------------------------------------------
# Person vs organisation, and candidate filtering
# ---------------------------------------------------------------------------

from affluense_v2.quality import people as quality_people  # noqa: E402


def test_single_word_names_are_not_people():
    """A one-word name is a brand far more often than an individual, and a
    suggestion needs a full name to be actionable anyway."""
    assert not quality_people.looks_like_person("Meridian")
    assert not quality_people.looks_like_person("Brightpath")
    assert quality_people.looks_like_person("Dana Okonkwo")


def test_organisational_forms_are_not_people():
    for name in ("Fairhaven Institute", "Brightpath Foundation",
                 "Northwind Trading Ltd", "National Widget Association"):
        assert not quality_people.looks_like_person(name), name


def test_blank_and_overlong_names_are_rejected():
    assert not quality_people.looks_like_person("")
    assert not quality_people.looks_like_person(None)
    assert not quality_people.looks_like_person("a b c d e f g h")


def test_a_tie_describing_the_subjects_own_role_means_an_organisation():
    """You are not the founder of a person. The shape of the tie says which
    end of it is a company."""
    assert quality_people.is_organisation_tie("founder")
    assert quality_people.is_organisation_tie("board member")
    assert quality_people.is_organisation_tie("subsidiary")
    assert quality_people.is_organisation_tie("chairman")


def test_a_peer_tie_is_not_an_organisation_tie():
    assert not quality_people.is_organisation_tie("co-founder")
    assert not quality_people.is_organisation_tie("Husband")
    assert not quality_people.is_organisation_tie("colleague")
    assert not quality_people.is_organisation_tie(None)


def test_clean_network_separates_organisations_from_people():
    network = [
        {"name": "Sanjay Nayar", "tie": "Husband", "tie_type": "personal"},
        {"name": "Meridian", "tie": "subsidiary", "tie_type": "associate"},
        {"name": "Fairhaven Institute", "tie": "founder",
         "tie_type": "associate"},
        {"name": "Dana Okonkwo", "tie": "co-founder", "tie_type": "associate"},
    ]
    people, organisations = quality_people.clean_network(network)
    assert [p["name"] for p in people] == ["Sanjay Nayar", "Dana Okonkwo"]
    assert len(organisations) == 2


def test_registry_cooficers_are_trusted_outright():
    """The SPARQL filters on P31=Q5, so these are people by construction even
    when the name shape is unusual."""
    network = [{"name": "Prince", "tie": "co-officer", "tie_type": "co-officer",
                "wikidata_id": "Q123"}]
    people, organisations = quality_people.clean_network(network)
    assert len(people) == 1
    assert organisations == []


def test_endorsement_roles_are_not_business_peers():
    assert quality_people.is_media_role(["Brand Ambassador"])
    assert quality_people.is_media_role(["brand ambassador", "endorser"])
    assert not quality_people.is_media_role(["Chief Executive"])
    assert not quality_people.is_media_role([])


def test_a_substantive_role_outranks_an_endorsement_word():
    """'Shareholder / Ambassador' carries a real financial interest."""
    assert not quality_people.is_media_role(["Shareholder and Ambassador"])
    assert not quality_people.is_media_role(["Founder", "Brand Ambassador"])


def test_colleagues_at_the_subjects_own_company_are_excluded():
    """A colleague is not a connection to make — they already work together."""
    candidate = {"name": "Dana Okonkwo", "companies": ["Northwind Trading"]}
    assert quality_people.works_with_subject(candidate, ["Northwind Trading"])
    assert quality_people.works_with_subject(
        candidate, ["Northwind Trading Ltd"]
    ), "a longer filed name must still match"


def test_unrelated_companies_do_not_count_as_shared():
    candidate = {"name": "Dana Okonkwo", "companies": ["Helios Systems"]}
    assert not quality_people.works_with_subject(candidate, ["Northwind Trading"])
    assert not quality_people.works_with_subject(candidate, [])


def test_filter_candidates_applies_all_three_rules():
    candidates = [
        {"name": "Dana Okonkwo", "roles": ["Chief Executive"],
         "companies": ["Helios Systems Pvt Ltd"]},
        {"name": "Meridian", "roles": ["Founder"], "companies": []},
        {"name": "Priya Raghunathan", "roles": ["Brand Ambassador"],
         "companies": ["Some Brand"]},
        {"name": "Tomas Lindqvist", "roles": ["Director"],
         "companies": ["Northwind Trading Ltd"]},
    ]
    kept, dropped = quality_people.filter_candidates(
        candidates, ["Northwind Trading"],
    )
    assert [c["name"] for c in kept] == ["Dana Okonkwo"]
    reasons = {d["name"]: d["reason"] for d in dropped}
    assert "not a personal name" in reasons["Meridian"]
    assert "endorsement" in reasons["Priya Raghunathan"]
    assert "already works" in reasons["Tomas Lindqvist"]


def test_filter_is_safe_on_empty_input():
    assert quality_people.filter_candidates([], []) == ([], [])


# ---------------------------------------------------------------------------
# Industry must describe the candidate, not the query that found them
# ---------------------------------------------------------------------------

def test_industry_is_only_claimed_when_the_model_confirms_it():
    """The generic failure: a candidate inherits the industry of the search
    that surfaced them, which is 30% of the relevance score."""
    spec = {"industry_id": "sector:cosmetics", "industry_label": "cosmetics",
            "role": "CEO"}

    class Article:
        headline = "Bank appoints a new chief executive"
        publisher = "Outlet"
        publisher_name = "Outlet"
        url = "https://example.test/a"

    class Client:
        configured = True

        async def complete_json(self, system, user, max_tokens=0):
            return {"people": [
                {"name": "Kalyan Kumar", "company": "Central Bank",
                 "role": "CEO", "in_industry": False, "headline_id": 0},
                {"name": "Dana Okonkwo", "company": "Glow Cosmetics Ltd",
                 "role": "CEO", "in_industry": True, "headline_id": 0},
            ]}

    found = run(peers._read_batch(Client(), [Article()], 0, spec, None))
    by_name = {c["name"]: c for c in found}
    assert by_name["Kalyan Kumar"]["matched_industries"] == []
    assert by_name["Dana Okonkwo"]["matched_industries"] == ["sector:cosmetics"]


def test_an_unconfirmed_candidate_scores_no_industry_overlap():
    profile = {
        "roles": ["Founder"], "industry_qids": ["sector:cosmetics"],
        "industry_labels": {"sector:cosmetics": "cosmetics"},
        "industries": ["cosmetics"], "countries": [], "company_names": [],
    }
    unconfirmed = {"name": "Kalyan Kumar", "roles": ["CEO"],
                   "companies": ["Central Bank"], "matched_industries": [],
                   "country": None, "sitelinks": 0, "source": "News coverage",
                   "source_url": "x"}
    ranked = scoring.rank([unconfirmed], profile, network=[], limit=5)
    assert ranked[0]["score_components"]["industry_overlap"] == 0.0


def test_merge_keeps_a_confirmation_from_any_query():
    pool = {}
    peers._merge(pool, {"name": "Dana Okonkwo", "roles": [], "companies": [],
                        "matched_industries": [], "industry_confirmed": False})
    peers._merge(pool, {"name": "Dana Okonkwo", "roles": [], "companies": [],
                        "matched_industries": ["sector:cosmetics"],
                        "industry_confirmed": True})
    assert next(iter(pool.values()))["industry_confirmed"] is True
