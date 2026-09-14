"""Tests for the HTTP API.

The pipelines are stubbed, so these exercise the job lifecycle and error
handling without touching the network.

    pytest -q test_api.py
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import api


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api, "store", api.JobStore(max_workers=2))
    return TestClient(api.app)


def wait_for(client, job_id, *, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def stub_screening(monkeypatch, result=None, boom=None):
    def fake_run(name, company, options):
        options.on_progress("resolving identity")
        options.on_progress("screening companies")
        if boom:
            raise boom
        return {"marker": name}

    monkeypatch.setattr(api, "run_screening", fake_run)
    monkeypatch.setattr(
        api.screening_output, "build_report",
        lambda r, n, c: result or {"query": {"individual": n, "company": c},
                                   "screening": [], "counts": {}},
    )


def test_health_reports_optional_capabilities(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "sentiment_degraded" in body
    assert "firecrawl_configured" in body


def test_screening_job_runs_to_completion(client, monkeypatch):
    stub_screening(monkeypatch)

    response = client.post("/api/screening", json={"name": "Ratan Tata",
                                                   "company": "Tata Sons"})
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    final = wait_for(client, job_id)
    assert final["status"] == "done"
    # Progress is surfaced, not swallowed into stdout.
    assert "resolving identity" in final["progress"]

    result = client.get(f"/api/jobs/{job_id}/result").json()
    assert result["query"]["individual"] == "Ratan Tata"


def test_result_is_409_while_still_running(client, monkeypatch):
    """Not 404: the job exists, it simply is not finished. A frontend polling
    for a result must be able to tell those apart."""
    def slow_run(name, company, options):
        time.sleep(0.5)
        return {}

    monkeypatch.setattr(api, "run_screening", slow_run)
    monkeypatch.setattr(api.screening_output, "build_report", lambda r, n, c: {})

    job_id = client.post("/api/screening", json={"name": "Someone"}).json()["job_id"]
    assert client.get(f"/api/jobs/{job_id}/result").status_code == 409
    wait_for(client, job_id)


def test_a_failing_pipeline_becomes_an_error_job_not_a_crash(client, monkeypatch):
    stub_screening(monkeypatch, boom=RuntimeError("source unreachable"))

    job_id = client.post("/api/screening", json={"name": "Someone"}).json()["job_id"]
    final = wait_for(client, job_id)

    assert final["status"] == "error"
    assert "source unreachable" in final["error"]
    assert client.get(f"/api/jobs/{job_id}/result").status_code == 500


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/doesnotexist").status_code == 404
    assert client.delete("/api/jobs/doesnotexist").status_code == 404


def test_jobs_can_be_listed_and_deleted(client, monkeypatch):
    stub_screening(monkeypatch)
    job_id = client.post("/api/screening", json={"name": "Ratan Tata"}).json()["job_id"]
    wait_for(client, job_id)

    assert any(j["job_id"] == job_id for j in client.get("/api/jobs").json()["jobs"])
    assert client.delete(f"/api/jobs/{job_id}").status_code == 200
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_csv_download_streams_the_screening_columns(client, monkeypatch):
    stub_screening(monkeypatch, result={
        "query": {"individual": "Ratan Tata", "company": "Tata Sons"},
        "screening": [{
            "company_name": "Tata Trusts", "sentiment": "negative",
            "negative_news_flag": True, "flag_categories": ["regulatory"],
            "flag_stages": ["investigating"],
            "sentiment_breakdown": {"negative": 9, "neutral": 10, "positive": 5},
            "articles_reviewed": 24, "insufficient_coverage": False,
            "relationship": ["chairperson"], "relationship_type": "control",
            "status": "active",
            "jurisdiction": "India", "registry_id": "Q1",
            "link_confidence": 0.9, "sources": ["Wikidata"],
            "source_url": "https://example.com",
        }],
        "counts": {},
    })
    job_id = client.post("/api/screening", json={"name": "Ratan Tata"}).json()["job_id"]
    wait_for(client, job_id)

    response = client.get(f"/api/jobs/{job_id}/result.csv")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "company_name,sentiment,negative_news_flag" in response.text
    assert "Tata Trusts" in response.text


def test_input_is_validated(client):
    assert client.post("/api/screening", json={"name": "x"}).status_code == 422
    assert client.post("/api/screening", json={}).status_code == 422
    assert client.post(
        "/api/screening", json={"name": "Valid Name", "max_companies": 999}
    ).status_code == 422


def test_firecrawl_can_be_turned_off_per_request(client, monkeypatch):
    seen = {}

    def fake_run(name, company, options):
        seen["key"] = options.firecrawl_key
        return {}

    monkeypatch.setattr(api, "run_screening", fake_run)
    monkeypatch.setattr(api.screening_output, "build_report", lambda r, n, c: {})
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    job_id = client.post("/api/screening", json={"name": "Someone",
                                                 "use_firecrawl": False}).json()["job_id"]
    wait_for(client, job_id)
    assert seen["key"] is None
