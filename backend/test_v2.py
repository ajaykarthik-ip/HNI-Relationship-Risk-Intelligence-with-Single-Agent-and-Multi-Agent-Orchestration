"""Tests for the V2 engine.

No network access and no event-loop plugin: every async test is driven with
`asyncio.run`, so the suite needs nothing beyond the pytest already in
requirements.txt.

What is worth testing here is not "does httpx work" but the three properties
that make V2 safe to prefer over V1:

  ordering    concurrent work must come back in submission order, or two runs
              over identical evidence disagree
  containment a dead source becomes a note; a cancelled run stops
  parity      V2 must ask V1's questions, spend V1's budgets, and hand V1's
              scorer the same shape of evidence

    pytest test_v2.py -q
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from affluense import config as v1config
from affluense.models import Article
from affluense_v2.budgets import Budget, RunBudgets
from affluense_v2.cache_async import AsyncCache
from affluense_v2.concurrency import gather_ordered, map_bounded
from affluense_v2.hostpolicy import (DEFAULT, GateRegistry, TokenBucket,
                                     policy_for)
from affluense_v2.progress import Cancelled, Reporter
from affluense_v2.pipeline import Options as V2Options
from affluense_v2.transport import AsyncResponse, _timeout_kwarg


def run(coro):
    """Drive one coroutine to completion without pytest-asyncio."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Host policy
# ---------------------------------------------------------------------------

def test_aggregators_get_real_concurrency():
    """The two feeds V1 serialises are the whole reason V2 exists."""
    google = policy_for("https://news.google.com/rss/search?q=x")
    bing = policy_for("https://www.bing.com/news/search?q=x")
    assert google.concurrency >= 4
    assert bing.concurrency >= 4


def test_publishers_stay_at_one_request_each():
    """Politeness to individual publishers is unchanged from V1.

    Concurrency during full-text retrieval must come from reading *different*
    sites at once, never from pushing harder on one.
    """
    policy = policy_for("https://some-regional-paper.example/article/1")
    assert policy is DEFAULT
    assert policy.concurrency == 1
    assert policy.rate <= 1.0


def test_wikimedia_is_kept_gentle():
    assert policy_for("https://www.wikidata.org/w/api.php").concurrency <= 2
    assert policy_for("https://en.wikipedia.org/w/api.php").concurrency <= 2


def test_paid_apis_are_not_rate_limited_for_politeness():
    assert policy_for("https://api.openai.com/v1/chat/completions").concurrency >= 4
    assert policy_for("https://api.firecrawl.dev/v2/scrape").concurrency >= 2


def test_longest_matching_host_fragment_wins():
    """query.wikidata.org must not pick up plain wikidata.org's policy."""
    assert policy_for("https://query.wikidata.org/sparql").burst <= 2


def test_token_bucket_enforces_a_rate():
    async def scenario():
        bucket = TokenBucket(rate=20.0, capacity=1.0)
        started = time.monotonic()
        for _ in range(4):
            await bucket.acquire()
        return time.monotonic() - started

    # One token in the bucket, then three refills at 20/s = at least 0.15s.
    assert run(scenario()) >= 0.1


def test_penalise_halves_the_rate():
    bucket = TokenBucket(rate=8.0, capacity=4.0)
    bucket.penalise()
    assert bucket.rate == 4.0
    for _ in range(20):
        bucket.penalise()
    assert bucket.rate >= 0.2, "a 429 must slow us down, never stall us"


def test_gate_registry_reuses_one_gate_per_host():
    async def scenario():
        registry = GateRegistry()
        first = registry.gate("https://news.google.com/a")
        second = registry.gate("https://news.google.com/b")
        other = registry.gate("https://www.bing.com/c")
        return first is second, first is other, registry.describe()

    same, different, described = run(scenario())
    assert same
    assert not different
    assert {row["host"] for row in described} == {"news.google.com", "www.bing.com"}


def test_gate_releases_its_slot_on_failure():
    """A raise inside the gate must not leak the semaphore for the whole run."""
    async def scenario():
        registry = GateRegistry()
        gate = registry.gate("https://example.test/x")
        for _ in range(3):
            try:
                async with gate:
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
        # If the slot leaked, this would hang rather than return.
        async with gate:
            return True

    assert run(asyncio.wait_for(scenario(), timeout=5))


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------

def test_budget_grants_what_it_has_and_no_more():
    budget = Budget("test", 5)
    assert budget.claim(3) == 3
    assert budget.claim(4) == 2
    assert budget.claim(1) == 0
    assert budget.remaining == 0
    assert budget.refused == 3


def test_budget_release_returns_unspent_slots():
    """A call that never happened must not cost the run a slot."""
    budget = Budget("test", 2)
    assert budget.claim(2) == 2
    budget.release(1)
    assert budget.claim(1) == 1


def test_zero_means_unlimited():
    budget = Budget("test", 0)
    assert budget.unlimited
    assert budget.claim(10_000) == 10_000


def test_budget_is_thread_safe():
    """V1 code reached through the bridge claims from worker threads."""
    import threading

    budget = Budget("test", 100)
    granted = []

    def worker():
        granted.append(sum(budget.claim(1) for _ in range(50)))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(granted) == 100, "the ceiling must hold under contention"


def test_extraction_budget_is_sized_from_queued_work():
    """Sized from the batches actually needed, so coverage is never trimmed."""
    budgets = RunBudgets(openai_calls=200, fulltext=40)
    budgets.size_extraction(batches_needed=11, headroom=4)
    assert budgets.openai_extract.limit == 15
    assert budgets.openai_extract.claim(11) == 11


def test_note_once_deduplicates():
    budgets = RunBudgets(openai_calls=1, fulltext=1)
    assert budgets.note_once("openai-budget") is True
    assert budgets.note_once("openai-budget") is False


# ---------------------------------------------------------------------------
# Concurrency helpers
# ---------------------------------------------------------------------------

def test_gather_ordered_returns_submission_order():
    """The single rule that keeps V2 as reproducible as V1."""
    async def slow(value, delay):
        await asyncio.sleep(delay)
        return value

    # Deliberately finishing backwards.
    result = run(gather_ordered([slow("a", 0.03), slow("b", 0.02), slow("c", 0.0)]))
    assert result == ["a", "b", "c"]


def test_gather_ordered_contains_failures():
    async def ok():
        return 1

    async def boom():
        raise ValueError("dead source")

    seen = []
    result = run(gather_ordered(
        [ok(), boom(), ok()],
        on_error=lambda i, e: seen.append((i, str(e))),
    ))
    assert result == [1, None, 1]
    assert seen == [(1, "dead source")]


def test_gather_ordered_reraises_cancellation():
    """A stopped run must stop, not quietly carry on spending credits."""
    async def ok():
        return 1

    async def stop():
        raise Cancelled()

    with pytest.raises(Cancelled):
        run(gather_ordered([ok(), stop()]))


def test_map_bounded_respects_its_limit():
    state = {"live": 0, "peak": 0}

    async def worker(item):
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        await asyncio.sleep(0.01)
        state["live"] -= 1
        return item * 2

    result = run(map_bounded(list(range(10)), worker, limit=3))
    assert result == [i * 2 for i in range(10)]
    assert state["peak"] <= 3


def test_map_bounded_on_empty_input():
    async def worker(item):
        raise AssertionError("must not be called")

    assert run(map_bounded([], worker, limit=4)) == []


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def test_cache_roundtrip_and_shared_keys_with_v1(tmp_path):
    """V2 must read and write the cache V1 wrote, by the same key."""
    from affluense.cache import ResponseCache

    cache = AsyncCache(str(tmp_path), enabled=True)
    key = cache.key("GET", "https://news.google.com/rss", {"q": "x"})
    assert key == ResponseCache.key("GET", "https://news.google.com/rss", {"q": "x"})

    run(cache.set(key, "<rss/>"))
    assert run(cache.get(key, "https://news.google.com/rss")) == "<rss/>"
    assert cache.hits == 1


def test_cache_writes_leave_no_temporary_files(tmp_path):
    """Atomic writes: a reader sees the old entry or the new one, never half."""
    cache = AsyncCache(str(tmp_path), enabled=True)
    key = cache.key("GET", "https://example.test/a")
    run(cache.set(key, "payload"))
    leftovers = [p for p in tmp_path.rglob(".tmp-*")]
    assert leftovers == []


def test_cache_expiry_uses_v1_ttls(tmp_path):
    """News expires in an hour; Wikidata does not. Both come from V1."""
    cache = AsyncCache(str(tmp_path), enabled=True)
    key = cache.key("GET", "https://news.google.com/rss")
    run(cache.set(key, "fresh"))

    # Rewrite the record with an old timestamp, bypassing the writer.
    path = cache._inner._path(key)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["stored_at"] = time.time() - 7200
    path.write_text(json.dumps(record), encoding="utf-8")

    assert run(cache.get(key, "https://news.google.com/rss")) is None


def test_disabled_cache_is_inert(tmp_path):
    cache = AsyncCache(None, enabled=False)
    key = cache.key("GET", "https://example.test/a")
    run(cache.set(key, "x"))
    assert run(cache.get(key, "https://example.test/a")) is None


# ---------------------------------------------------------------------------
# Progress and cancellation
# ---------------------------------------------------------------------------

def test_percent_never_reaches_100_while_running():
    reporter = Reporter()
    for stage in ("identity", "discovery", "evidence", "assignment", "fulltext",
                  "analysis", "findings", "network", "report"):
        reporter.stage(stage, total=1)
        reporter.advance(stage)
        reporter.finish(stage)
    assert reporter.percent() <= 99


def test_percent_rises_as_stages_complete():
    reporter = Reporter()
    reporter.finish("starting")
    first = reporter.percent()
    reporter.stage("identity", total=1)
    reporter.finish("identity")
    second = reporter.percent()
    reporter.stage("discovery", total=2)
    reporter.finish("discovery")
    assert first <= second <= reporter.percent()


def test_snapshot_is_json_serialisable():
    """It is stored on the job and sent to the browser verbatim."""
    reporter = Reporter()
    reporter.stage("evidence", total=3)
    reporter.advance("evidence", by=2)
    payload = reporter.snapshot()
    json.dumps(payload)
    assert payload["engine"] == "v2"
    stages = {s["stage"]: s for s in payload["stages"]}
    assert stages["evidence"]["done"] == 2
    assert stages["evidence"]["total"] == 3


def test_host_cancellation_is_preserved_and_reraised():
    """api.py's own RunCancelled must survive the trip through V2."""
    class HostStop(Exception):
        pass

    def on_progress(line):
        raise HostStop()

    reporter = Reporter(on_progress=on_progress)
    with pytest.raises(Cancelled):
        reporter.say("anything")

    assert reporter.cancelled
    assert isinstance(reporter.cancel_exception, HostStop)
    with pytest.raises(HostStop):
        reporter.raise_host_cancellation()


def test_check_raises_once_cancelled():
    reporter = Reporter()
    reporter.check()
    reporter.request_cancel()
    with pytest.raises(Cancelled):
        reporter.check()


def test_external_cancel_check_is_honoured():
    flag = {"stop": False}
    reporter = Reporter(cancel_check=lambda: flag["stop"])
    reporter.check()
    flag["stop"] = True
    with pytest.raises(Cancelled):
        reporter.check()


def test_timings_are_reported_per_stage():
    reporter = Reporter()
    reporter.stage("evidence", total=1)
    reporter.finish("evidence")
    timings = reporter.timings()
    assert "evidence" in timings
    assert timings["evidence"] >= 0


# ---------------------------------------------------------------------------
# Transport details
# ---------------------------------------------------------------------------

def test_timeout_kwarg_omits_none():
    """httpx reads timeout=None as 'no timeout at all', not 'use the default'."""
    assert _timeout_kwarg(None) == {}
    assert _timeout_kwarg(30.0) == {"timeout": 30.0}


def test_async_response_serves_bytes_and_text():
    """V1's RSS parser reads .content; its body extractor reads .text."""
    response = AsyncResponse("<rss><item/></rss>", "https://x.test")
    assert response.content == b"<rss><item/></rss>"
    assert response.text == "<rss><item/></rss>"
    assert response.status_code == 200
    assert response.from_cache is False


def test_async_response_parses_json():
    response = AsyncResponse('{"a": 1}', "https://x.test")
    assert response.json() == {"a": 1}


# ---------------------------------------------------------------------------
# News: parsing is V1's, scheduling is V2's
# ---------------------------------------------------------------------------

GOOGLE_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Regulator opens probe into Acme - The Wire</title>
    <link>https://news.google.com/rss/articles/ABC</link>
    <source url="https://thewire.test">The Wire</source>
    <pubDate>Mon, 01 Apr 2024 08:00:00 GMT</pubDate>
  </item>
</channel></rss>"""

BING_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Acme faces penalty</title>
    <link>https://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fmint.test%2Fstory&amp;c=1</link>
    <description>The regulator issued a notice.</description>
    <pubDate>Tue, 02 Apr 2024 08:00:00 GMT</pubDate>
  </item>
</channel></rss>"""


class FakeTransport:
    """Just enough transport for the news and fulltext agents."""

    def __init__(self, bodies: dict):
        self.bodies = bodies
        self.notes: list = []
        self.requested: list = []

    async def get(self, url, attempts=None, params=None, headers=None,
                  timeout=None):
        self.requested.append(url)
        for fragment, body in self.bodies.items():
            if fragment in url:
                if isinstance(body, Exception):
                    raise body
                return AsyncResponse(body, url)
        return None

    def note(self, message):
        self.notes.append(message)

    async def robots_allow(self, url):
        return True

    async def prefetch_robots(self, urls):
        return None


def test_google_items_keep_the_publisher_domain():
    from affluense_v2.sources import news_async

    transport = FakeTransport({"news.google.com": GOOGLE_RSS})
    articles = run(news_async.google_news(transport, "acme", 8, about="Acme"))
    assert len(articles) == 1
    assert articles[0].publisher_url == "https://thewire.test"
    assert articles[0].url_is_redirect is True
    assert articles[0].about_company == "Acme"


def test_bing_links_are_unwrapped_to_the_real_article():
    from affluense_v2.sources import news_async

    transport = FakeTransport({"bing.com": BING_RSS})
    articles = run(news_async.bing_news(transport, "acme", 8, about="Acme"))
    assert articles[0].url == "https://mint.test/story"
    assert articles[0].url_is_redirect is False


def test_one_dead_feed_does_not_cost_the_other_its_articles():
    from affluense_v2.sources import news_async

    transport = FakeTransport({
        "news.google.com": RuntimeError("feed down"),
        "bing.com": BING_RSS,
    })
    articles = run(news_async.collect_one(transport, "acme", 8))
    assert len(articles) == 1
    assert transport.notes, "the failure must be recorded, not swallowed"


def test_query_group_is_stamped_on_every_article():
    """Reporting which adverse check found a finding depends on this."""
    from affluense_v2.sources import news_async

    transport = FakeTransport({"bing.com": BING_RSS})
    query_set = [{"query": '"Acme" (fraud)', "group": "fraud"}]
    articles = run(news_async.collect_many(transport, query_set, 8, about="Acme"))
    assert [a.query_group for a in articles] == ["fraud"]


def test_collect_many_asks_v1s_full_question_set():
    """V2 must issue every query V1 would, not a cheaper subset."""
    from affluense.collect import queries
    from affluense_v2.sources import news_async

    transport = FakeTransport({"bing.com": BING_RSS, "news.google.com": GOOGLE_RSS})
    query_set = queries.for_company("Acme Ltd")
    run(news_async.collect_many(transport, query_set, 8, about="Acme Ltd"))

    google = [u for u in transport.requested if "news.google.com" in u]
    bing = [u for u in transport.requested if "bing.com" in u]
    assert len(query_set) == v1config.MAX_QUERIES_PER_ENTITY
    assert len(google) == len(query_set)
    assert len(bing) == len(query_set)


def test_dedupe_spans_the_whole_query_set():
    """The same wire story arriving from two checks must be counted once."""
    from affluense_v2.sources import news_async

    transport = FakeTransport({"bing.com": BING_RSS})
    query_set = [
        {"query": "a", "group": "fraud"},
        {"query": "b", "group": "litigation"},
    ]
    articles = run(news_async.collect_many(transport, query_set, 8))
    assert len(articles) == 1
    assert articles[0].query_group == "fraud", "first check to find it keeps it"


# ---------------------------------------------------------------------------
# Fulltext selection and budget
# ---------------------------------------------------------------------------

def _risky(url, tier=1, stage="charged"):
    article = Article(headline="Probe into Acme", url=url,
                      source="Bing News RSS", source_url="https://bing.test")
    article.risk_categories = ["investigation"]
    article.risk_stage = stage
    article.publisher_tier = tier
    return article


def test_select_claims_the_budget_in_order():
    from affluense_v2.agents import fulltext as agent

    budget = Budget("fulltext", 2)
    articles = [_risky(f"https://p{i}.test/a") for i in range(5)]
    chosen = agent.select(articles, budget)
    assert len(chosen) == 2
    assert budget.remaining == 0
    assert agent.select(articles, budget) == []


def test_select_prefers_better_publishers():
    from affluense_v2.agents import fulltext as agent

    # Both pass V1's worth_fetching rule, so this tests the ordering rather
    # than the filter that runs before it.
    weak = _risky("https://weak.test/a", tier=2)
    strong = _risky("https://strong.test/a", tier=1)
    chosen = agent.select([weak, strong], Budget("fulltext", 1))
    assert chosen == [strong]


def test_select_uses_v1s_worth_fetching_rule():
    """No risk terms means no request. V2 must not widen the net."""
    from affluense.collect import fulltext as v1fulltext
    from affluense_v2.agents import fulltext as agent

    plain = Article(headline="Acme opens an office", url="https://p.test/a",
                    source="Bing News RSS", source_url="https://bing.test")
    assert v1fulltext.worth_fetching(plain) is False
    assert agent.select([plain], Budget("fulltext", 10)) == []


def test_fulltext_budget_defaults_to_v1s_cap():
    budgets = RunBudgets(openai_calls=200, fulltext=v1config.MAX_FULLTEXT_FETCHES)
    assert budgets.fulltext.limit == v1config.MAX_FULLTEXT_FETCHES


# ---------------------------------------------------------------------------
# Analyst
# ---------------------------------------------------------------------------

def test_batches_match_v1s_batch_size():
    from affluense_v2.agents import analyst

    size = v1config.OPENAI_EXTRACT_BATCH
    assert analyst.batches_for([]) == 0
    assert analyst.batches_for([1] * size) == 1
    assert analyst.batches_for([1] * (size + 1)) == 2


def test_analyst_without_a_key_falls_back_to_keywords(monkeypatch):
    """No key, no change: V1's rule, and V2 must keep it."""
    from affluense_v2.agents import analyst

    monkeypatch.setattr(v1config, "OPENAI_API_KEY", "")
    reporter = Reporter()
    transport = FakeTransport({})
    articles = [_risky("https://p.test/a")]
    tally = run(analyst.analyse(transport, reporter, articles, "Acme", "Person",
                                Budget("extract", 10)))
    assert tally["backend"] == "keyword"
    assert tally["sent"] == 0
    assert articles[0].extracted is None


# ---------------------------------------------------------------------------
# Options parity
# ---------------------------------------------------------------------------

def test_v2_options_accept_every_v1_field():
    """Switching engines must not mean rewriting the caller."""
    from dataclasses import fields
    from affluense.pipeline import Options as V1Options

    v1_names = {f.name for f in fields(V1Options)}
    v2_names = {f.name for f in fields(V2Options)}
    missing = v1_names - v2_names
    assert not missing, f"V2 Options is missing V1 fields: {sorted(missing)}"


def test_v2_options_defaults_match_v1_where_they_overlap():
    from dataclasses import fields
    from affluense.pipeline import Options as V1Options

    v1_defaults = {f.name: f.default for f in fields(V1Options)}
    v2_defaults = {f.name: f.default for f in fields(V2Options)}
    for name, default in v1_defaults.items():
        if name in ("on_progress",):
            continue
        assert v2_defaults[name] == default, f"{name} drifted from V1"


def test_building_a_reporter_wires_the_callbacks():
    lines, events = [], []
    options = V2Options(verbose=False, on_progress=lines.append,
                        on_event=events.append)
    reporter = options.build_reporter()
    assert options.reporter is reporter
    reporter.say("  Resolving identity ...")
    assert lines == ["Resolving identity ..."]
    assert events, "structured progress must be emitted too"


def test_describe_reports_v1s_coverage_limits():
    """The engine must advertise V1's caps, not its own."""
    from affluense_v2 import pipeline

    described = pipeline.describe()
    assert described["engine"] == "v2"
    assert described["coverage"]["queries_per_entity"] == v1config.MAX_QUERIES_PER_ENTITY
    assert described["coverage"]["news_per_query"] == v1config.NEWS_PER_QUERY
