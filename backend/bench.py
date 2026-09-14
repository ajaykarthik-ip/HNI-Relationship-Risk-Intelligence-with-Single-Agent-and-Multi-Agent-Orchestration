#!/usr/bin/env python3
"""V1 against V2, fairly.

    python bench.py "Mukesh Ambani" --company "Reliance Industries"
    python bench.py "Ashneer Grover" --company BharatPe --repeat 3
    python bench.py "Azim Premji" --company Wipro --warm

A benchmark that is not controlled is worse than no benchmark, because it
produces a number people quote. Four things are controlled here:

  identity      the candidate is resolved ONCE and handed to both engines, so
                neither can win by picking a different, thinner subject.

  cache         each engine gets its own cold directory. Both write V1's cache
                keys to the same layout, so sharing one directory would let
                whichever ran second reuse the first's Firecrawl scrapes and
                appear several times faster for free.

  order         engines alternate across repetitions, so news-feed latency
                drift does not systematically favour whichever went second.

  coverage      V2 reads MAX_QUERIES_PER_ENTITY, NEWS_PER_QUERY and the rest
                straight from V1's config, and the gates below fail the run if
                it asked fewer questions anyway.

Speed with a different answer is a failure, not a result. Gates 2-5 exist to
say so out loud.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import os
import shutil
import statistics
import sys
import time
from datetime import datetime, timezone

from affluense import config, output as screening_output
from affluense.cache import ResponseCache
from affluense.http import Fetcher
from affluense.pipeline import Options as V1Options
from affluense.pipeline import run as run_v1
from affluense.resolve import candidates as candidates_mod
from affluense_v2.pipeline import Options as V2Options
from affluense_v2.pipeline import build_report as v2_build_report
from affluense_v2.pipeline import run as run_v2

BENCH_ROOT = pathlib.Path(".cache-bench")


# ---------------------------------------------------------------------------
# Stage timing
# ---------------------------------------------------------------------------

# V1 says where it is by printing. Timestamping those lines gives per-stage
# wall clock without touching a single V1 file -- `Options.on_progress` is
# already the hook.
V1_STAGE_MARKERS = (
    ("identity", ("resolving identity",)),
    ("discovery", ("wikidata claims", "company links", "co-officer",
                   "firecrawl: search", "search:", "read:")),
    ("merge", ("company records merged",)),
    ("evidence", ("screening:",)),
    ("person", ("screening the individual",)),
)


def _stage_of(line: str) -> str | None:
    lowered = line.strip().lower()
    for stage, markers in V1_STAGE_MARKERS:
        if any(lowered.startswith(m) or m in lowered for m in markers):
            return stage
    return None


class StageTimer:
    """Wall clock per stage, inferred from the progress lines a run emits."""

    def __init__(self):
        self.started = time.monotonic()
        self.totals: dict = {}
        self._current: str | None = None
        self._since = self.started
        self.lines = 0

    def observe(self, line: str) -> None:
        self.lines += 1
        stage = _stage_of(line)
        if stage is None or stage == self._current:
            return
        now = time.monotonic()
        if self._current is not None:
            self.totals[self._current] = round(
                self.totals.get(self._current, 0.0) + (now - self._since), 2
            )
        self._current = stage
        self._since = now

    def finish(self) -> dict:
        now = time.monotonic()
        if self._current is not None:
            self.totals[self._current] = round(
                self.totals.get(self._current, 0.0) + (now - self._since), 2
            )
            self._current = None
        return dict(self.totals)


# ---------------------------------------------------------------------------
# Running one engine
# ---------------------------------------------------------------------------

def _cache_dir(run_id: str, engine: str, warm: bool) -> str:
    path = BENCH_ROOT / run_id / engine
    if not warm and path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def run_engine(engine: str, name: str, company: str | None, confirmed: dict | None,
               args, run_id: str) -> dict:
    """One engine, one run. Returns the report plus how long everything took."""
    cache_dir = _cache_dir(run_id, engine, args.warm)
    firecrawl_key = os.environ.get("FIRECRAWL_API_KEY") if args.firecrawl else None
    timer = StageTimer()

    shared = dict(
        max_companies=args.max_companies,
        max_news=args.max_news,
        firecrawl_key=firecrawl_key,
        cache_dir=cache_dir,
        verbose=args.verbose,
        on_progress=timer.observe,
        confirmed_entity=confirmed,
    )

    started = time.monotonic()
    if engine == "v1":
        result = run_v1(name, company, V1Options(workers=args.workers, **shared))
    else:
        result = run_v2(name, company, V2Options(**shared))
    elapsed = time.monotonic() - started

    # Each engine assembles through its own entry point. Both call V1's
    # builder; V2 additionally applies its confidence cap, and measuring V2
    # without it would benchmark something nobody runs.
    if engine == "v1":
        report = screening_output.build_report(result, name, company)
    else:
        report = v2_build_report(result, name, company)
    total = time.monotonic() - started

    stages = timer.finish()
    # V2 measures its own stages directly, which is better than inferring them
    # from log lines. Where it has them, they win.
    native = (result.get("v2") or {}).get("stage_timings")
    if native:
        stages = native

    return {
        "engine": engine,
        "seconds": round(elapsed, 2),
        "seconds_with_report": round(total, 2),
        "stages": stages,
        "progress_lines": timer.lines,
        "report": report,
        "budgets": (result.get("v2") or {}).get("budgets"),
        "hosts": (result.get("v2") or {}).get("hosts"),
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def metrics(run: dict) -> dict:
    """Everything worth comparing, pulled from the delivered report.

    Every figure here is counted by the pipeline itself -- requests by the
    meter, tokens by the API's own usage block, credits at the call site.
    Nothing is estimated.
    """
    report = run["report"]
    usage = report.get("usage") or {}
    totals = usage.get("totals") or {}
    coverage = report.get("coverage") or {}
    assessment = report.get("assessment") or {}
    cache = report.get("cache") or {}
    findings = report.get("findings") or []

    return {
        "seconds": run["seconds"],
        "seconds_with_report": run["seconds_with_report"],
        "http_requests": totals.get("requests", 0),
        "served_from_cache": totals.get("served_from_cache", 0),
        "cache_hit_rate": cache.get("hit_rate", 0),
        "openai_calls": totals.get("openai_calls", 0),
        "prompt_tokens": totals.get("prompt_tokens", 0),
        "completion_tokens": totals.get("completion_tokens", 0),
        "total_tokens": totals.get("total_tokens", 0),
        "firecrawl_credits": totals.get("billable_credits", 0),
        "queries_issued": coverage.get("queries_issued", 0),
        "groups_checked": sorted(coverage.get("groups_checked") or []),
        "articles_retrieved": coverage.get("articles_retrieved", 0),
        "after_dedup": coverage.get("after_dedup", 0),
        "fulltext_attempted": coverage.get("fulltext_attempted", 0),
        "fulltext_fetched": coverage.get("fulltext_fetched", 0),
        "articles_analysed": coverage.get("articles_analysed_by_model", 0),
        "analysis_backend": coverage.get("analysis_backend", "keyword"),
        "publishers": coverage.get("publishers", 0),
        "companies_screened": len(report.get("screening") or []),
        "findings": len(findings),
        "finding_ids": sorted(f.get("id") for f in findings if f.get("id")),
        "risk_level": assessment.get("risk_level"),
        "confidence": assessment.get("confidence"),
        "recommended_action": assessment.get("recommended_action"),
        "identity_confirmed": bool(
            (report.get("validation") or {}).get("identity_confirmed")
        ),
        "attributable_findings": (
            (report.get("validation") or {}).get("attributable_findings", 0)
        ),
        "qc_issues": len((report.get("qc") or {}).get("issues") or []),
        "stages": run["stages"],
    }


# ---------------------------------------------------------------------------
# Quality gates
# ---------------------------------------------------------------------------

def gates(v1: dict, v2: dict) -> list:
    """V2 must not have bought its speed with coverage or a different verdict."""
    v1_ids, v2_ids = set(v1["finding_ids"]), set(v2["finding_ids"])
    missing = sorted(v1_ids - v2_ids)
    openai_ceiling = max(1, round(v1["openai_calls"] * 1.2))

    return [
        {
            "gate": "queries_issued >= V1",
            "passed": v2["queries_issued"] >= v1["queries_issued"],
            "detail": f"v1={v1['queries_issued']} v2={v2['queries_issued']}",
        },
        {
            "gate": "adverse groups superset",
            "passed": set(v2["groups_checked"]) >= set(v1["groups_checked"]),
            "detail": (
                f"v1={len(v1['groups_checked'])} v2={len(v2['groups_checked'])}"
            ),
        },
        {
            "gate": "findings superset",
            "passed": not missing,
            "detail": (
                "identical" if not missing
                else f"missing from v2: {', '.join(missing[:4])}"
            ),
            "soft": True,   # news genuinely moves between two cold runs
        },
        # Kept strict, and kept as a HARD gate. V2's quality layer can
        # legitimately lower a level -- when attribution stops crediting the
        # subject with someone else's conduct, the level should fall -- but
        # that is a result to read and explain, never something to hide behind
        # a loosened threshold. The quality-aware view below says which
        # direction the difference went and why.
        {
            "gate": "risk level identical",
            "passed": v1["risk_level"] == v2["risk_level"],
            "detail": f"v1={v1['risk_level']} v2={v2['risk_level']}",
        },
        {
            "gate": "confidence not lower",
            "passed": _confidence_rank(v2["confidence"]) >= _confidence_rank(v1["confidence"]),
            "detail": f"v1={v1['confidence']} v2={v2['confidence']}",
            # Soft by design: the confidence cap exists to lower this when the
            # evidence is thin, so a drop here is the feature working.
            "soft": True,
        },
        {
            "gate": "openai calls <= 1.2x V1",
            "passed": v2["openai_calls"] <= openai_ceiling,
            "detail": f"v1={v1['openai_calls']} v2={v2['openai_calls']} cap={openai_ceiling}",
        },
        {
            "gate": "firecrawl credits <= V1",
            "passed": v2["firecrawl_credits"] <= v1["firecrawl_credits"],
            "detail": (
                f"v1={v1['firecrawl_credits']} v2={v2['firecrawl_credits']}"
            ),
        },
    ]


CONFIDENCE_ORDER = ("low", "medium", "high")


def _confidence_rank(value) -> int:
    try:
        return CONFIDENCE_ORDER.index(str(value).lower())
    except ValueError:
        return -1


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

ROWS = (
    ("Runtime (s)", "seconds"),
    ("Runtime incl. report (s)", "seconds_with_report"),
    ("HTTP requests", "http_requests"),
    ("Served from cache", "served_from_cache"),
    ("OpenAI calls", "openai_calls"),
    ("Prompt tokens", "prompt_tokens"),
    ("Completion tokens", "completion_tokens"),
    ("Firecrawl credits", "firecrawl_credits"),
    ("Queries issued", "queries_issued"),
    ("Articles retrieved", "articles_retrieved"),
    ("Articles after dedupe", "after_dedup"),
    ("Full text attempted", "fulltext_attempted"),
    ("Full text read", "fulltext_fetched"),
    ("Articles model-read", "articles_analysed"),
    ("Independent publishers", "publishers"),
    ("Companies screened", "companies_screened"),
    ("Findings", "findings"),
    ("Attributable findings", "attributable_findings"),
    ("Risk level", "risk_level"),
    ("Confidence", "confidence"),
    ("QC issues", "qc_issues"),
)


def print_table(v1: dict, v2: dict) -> None:
    print()
    print(f"{'Measure':<28}{'V1':>16}{'V2':>16}   Delta")
    print("-" * 76)
    for label, key in ROWS:
        a, b = v1.get(key), v2.get(key)
        delta = ""
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if key.startswith("seconds") and a and b:
                delta = f"{a / b:.2f}x faster" if b else ""
            elif a != b:
                delta = f"{b - a:+g}"
        print(f"{label:<28}{str(a):>16}{str(b):>16}   {delta}")

    print()
    print("Stage timings (seconds)")
    print("-" * 76)
    names = sorted(set(v1["stages"]) | set(v2["stages"]))
    for stage in names:
        print(f"{stage:<28}{v1['stages'].get(stage, '-'):>16}"
              f"{v2['stages'].get(stage, '-'):>16}")


QUALITY_ROWS = (
    ("Findings", "findings"),
    ("Attributable findings", "attributable_findings"),
    ("Risk level", "risk_level"),
    ("Confidence", "confidence"),
)


def print_quality(v1: dict, v2: dict) -> None:
    """Where the engines disagree about the *answer*, and which way.

    Separate from the timing table on purpose. A lower V2 level is not
    automatically a regression -- de-duplicated findings and corrected
    attribution both reduce it legitimately -- but it is always something to
    read rather than something to average away.
    """
    print()
    print("Quality comparison")
    print("-" * 76)
    for label, key in QUALITY_ROWS:
        a, b = v1.get(key), v2.get(key)
        if a == b:
            print(f"  {label:<26} {str(a):>12}  ==  {str(b):<12}")
            continue
        direction = "lower in V2" if _is_lower(key, a, b) else "higher in V2"
        print(f"  {label:<26} {str(a):>12}  ->  {str(b):<12} ({direction})")

    v1_ids = set(v1.get("finding_ids") or [])
    v2_ids = set(v2.get("finding_ids") or [])
    if v1_ids - v2_ids:
        print(f"  only in V1: {', '.join(sorted(v1_ids - v2_ids)[:6])}")
    if v2_ids - v1_ids:
        print(f"  only in V2: {', '.join(sorted(v2_ids - v1_ids)[:6])}")
    print("  Note: V2 clusters by event rather than category+year, requires "
          "corroboration")
    print("        before advancing a stage, and does not attribute a company "
          "matter to")
    print("        the subject when the evidence names someone else. Fewer or "
          "lower-staged")
    print("        findings are expected consequences of those rules, not "
          "lost coverage —")
    print("        check 'Queries issued' and 'Articles retrieved' above to "
          "confirm.")


def _is_lower(key: str, a, b) -> bool:
    if key == "risk_level":
        order = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
        try:
            return order.index(str(b).upper()) < order.index(str(a).upper())
        except ValueError:
            return False
    if key == "confidence":
        return _confidence_rank(b) < _confidence_rank(a)
    try:
        return float(b) < float(a)
    except (TypeError, ValueError):
        return False


def print_gates(checks: list) -> bool:
    print()
    print("Quality gates")
    print("-" * 76)
    hard_failed = False
    for check in checks:
        soft = check.get("soft")
        if check["passed"]:
            mark = "PASS"
        elif soft:
            mark = "WARN"
        else:
            mark = "FAIL"
            hard_failed = True
        print(f"  [{mark}] {check['gate']:<34} {check['detail']}")
    return not hard_failed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def resolve_once(name: str, company: str | None, use_cache: bool) -> dict | None:
    """Pick the subject once, so both engines research the same person.

    Without this, a namesake collision in one engine's candidate ranking would
    show up as a coverage difference and be read as a V2 regression.
    """
    fetcher = Fetcher(cache=ResponseCache(".cache" if use_cache else None,
                                          enabled=use_cache))
    found = candidates_mod.discover(fetcher, name, company, mode="person")
    rows = found.get("candidates") or []
    if not rows:
        print("  No candidate resolved; both engines will guess identically.")
        return None
    top = rows[0]
    print(f"  Subject fixed: {top['name']} ({top.get('wikidata_id') or 'no QID'})")
    return {
        "id": top["id"],
        "name": top["name"],
        "kind": top.get("kind", "person"),
        "wikidata_id": top.get("wikidata_id"),
        "wikipedia_url": top.get("wikipedia_url"),
        "description": top.get("description"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark V1 against V2.")
    parser.add_argument("name")
    parser.add_argument("--company", default=None)
    parser.add_argument("--repeat", type=int, default=1,
                        help="Repetitions per engine; the median is reported.")
    parser.add_argument("--max-companies", type=int, default=8)
    parser.add_argument("--max-news", type=int, default=25)
    parser.add_argument("--workers", type=int, default=6,
                        help="V1 only. V2's concurrency is per host.")
    parser.add_argument("--no-firecrawl", dest="firecrawl", action="store_false")
    parser.add_argument("--warm", action="store_true",
                        help="Keep the cache between repetitions.")
    parser.add_argument("--no-confirm", dest="confirm", action="store_false",
                        help="Let each engine resolve the identity itself.")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    print(f"Affluense benchmark {run_id}")
    print(f"  subject : {args.name}" + (f" / {args.company}" if args.company else ""))
    print(f"  cache   : {'warm' if args.warm else 'cold, separate per engine'}")
    print(f"  firecrawl: {'on' if args.firecrawl else 'off'}")

    confirmed = resolve_once(args.name, args.company, True) if args.confirm else None

    runs: dict = {"v1": [], "v2": []}
    for repetition in range(args.repeat):
        # Alternate, so feed latency drift does not always favour one engine.
        order = ("v1", "v2") if repetition % 2 == 0 else ("v2", "v1")
        for engine in order:
            print(f"\n[{repetition + 1}/{args.repeat}] {engine} ...")
            try:
                run = run_engine(engine, args.name, args.company, confirmed,
                                 args, f"{run_id}-{repetition}")
            except Exception as exc:                      # noqa: BLE001
                print(f"  {engine} failed: {type(exc).__name__}: {exc}")
                continue
            runs[engine].append(metrics(run))
            print(f"  {engine} finished in {run['seconds']}s")

    if not runs["v1"] or not runs["v2"]:
        print("\nBoth engines must complete at least once to compare.")
        return 1

    def median_of(samples: list) -> dict:
        chosen = sorted(samples, key=lambda m: m["seconds"])[len(samples) // 2]
        if len(samples) > 1:
            chosen = dict(chosen)
            chosen["seconds"] = round(
                statistics.median(s["seconds"] for s in samples), 2
            )
        return chosen

    v1, v2 = median_of(runs["v1"]), median_of(runs["v2"])
    print_table(v1, v2)
    print_quality(v1, v2)
    passed = print_gates(gates(v1, v2))

    payload = {
        "run_id": run_id,
        "subject": {"name": args.name, "company": args.company,
                    "confirmed": confirmed},
        "settings": {
            "repeat": args.repeat, "warm": args.warm,
            "firecrawl": args.firecrawl,
            "max_companies": args.max_companies, "max_news": args.max_news,
            "queries_per_entity": config.MAX_QUERIES_PER_ENTITY,
            "news_per_query": config.NEWS_PER_QUERY,
            "fulltext_cap": config.MAX_FULLTEXT_FETCHES,
            "openai_model": config.OPENAI_MODEL,
        },
        "v1": {"median": v1, "runs": runs["v1"]},
        "v2": {"median": v2, "runs": runs["v2"]},
        "gates": gates(v1, v2),
        "passed": passed,
    }
    out = pathlib.Path(args.out or f"bench-{run_id}.json")
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nWritten to {out}")
    print("PASS" if passed else "FAIL — V2 changed the answer, not just the clock.")
    return 0 if passed else 2


if __name__ == "__main__":
    sys.exit(main())
