#!/usr/bin/env python3
"""Affluense network intelligence — Problem Statement 2.

Given an individual's name and one connected company, map their current
network and suggest new connections globally, ranked by relevance.

    python run_network.py "Ratan Tata" "Tata Sons"
    python run_network.py "Azim Premji" "Wipro" --max-suggestions 40

Relevance is a weighted, explainable score: every suggestion carries the
components that produced it and the reasons in plain language.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from affluense import config
from affluense.network import output
from affluense.network.pipeline import Options, run


def main() -> None:
    config.load_env_file()

    parser = argparse.ArgumentParser(
        description="Map an HNI/UHNI's network and suggest new connections."
    )
    parser.add_argument("name", help='Individual, e.g. "Ratan Tata"')
    parser.add_argument(
        "company", nargs="?", default=None,
        help='One connected company. Used to disambiguate the person.',
    )
    parser.add_argument("--out", help="Output basename (default: <name>-network)")
    parser.add_argument("--max-suggestions", type=int, default=25)
    parser.add_argument("--industries", type=int, default=3, help="Industries to search")
    parser.add_argument("--roles", type=int, default=3, help="Role types to search")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--cache-dir", default=".cache")
    parser.add_argument("--no-cache", action="store_true", help="Bypass the response cache")

    group = parser.add_argument_group("Firecrawl (optional)")
    group.add_argument("--firecrawl-key", default=os.environ.get("FIRECRAWL_API_KEY"))
    group.add_argument("--no-firecrawl", action="store_true")
    group.add_argument("--firecrawl-limit", type=int, default=8)
    group.add_argument("--firecrawl-pages", type=int, default=4)

    args = parser.parse_args()

    key = None if args.no_firecrawl else args.firecrawl_key
    options = Options(
        max_suggestions=args.max_suggestions,
        industries=args.industries,
        roles=args.roles,
        delay=args.delay,
        firecrawl_key=key,
        firecrawl_limit=args.firecrawl_limit,
        firecrawl_pages=args.firecrawl_pages,
        cache_dir=None if args.no_cache else args.cache_dir,
    )

    print(f"\nNetwork: {args.name}" + (f" via {args.company}" if args.company else ""))
    if key:
        print("  Firecrawl key detected; associate extraction enabled.")
    print()

    from affluense.enrich.sentiment import SentimentAnalyzer
    if "DEGRADED" in SentimentAnalyzer().backend:
        print("  WARNING: vaderSentiment missing; sentiment quality is degraded.")
        print("           Run: pip install -r requirements.txt\n")

    result = run(args.name, args.company, options)
    report = output.build_report(result, args.name, args.company)

    basename = args.out or (
        re.sub(r"[^a-z0-9]+", "-", args.name.lower()).strip("-") + "-network"
    )
    json_path, csv_path = f"{basename}.json", f"{basename}.csv"
    output.write_json(report, json_path)
    output.write_csv(report, csv_path)

    output.summarise(report)
    print(f"\n  Written to {json_path} and {csv_path}\n")


if __name__ == "__main__":
    sys.exit(main())
