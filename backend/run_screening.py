#!/usr/bin/env python3
"""Affluense screening — Problem Statement 1.

Given an individual's name and one connected company, identify their other
connected companies, gather public information on each, classify sentiment,
flag adverse news, and write the result as JSON and CSV.

    python -m venv .venv
    .venv\\Scripts\\activate          (Windows)
    pip install -r requirements.txt

    python run_screening.py "Ratan Tata" "Tata Sons"
    python run_screening.py "Virat Kohli" "One8 Commune" --out kohli

Every source is free and keyless except Firecrawl, which is optional: put a
key in .env to add web search and page extraction, or omit it entirely.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from affluense import config, output
from affluense.pipeline import Options, run


def main() -> None:
    config.load_env_file()

    parser = argparse.ArgumentParser(
        description="Screen an HNI/UHNI and their connected companies."
    )
    parser.add_argument("name", help='Individual, e.g. "Ratan Tata"')
    parser.add_argument(
        "company", nargs="?", default=None,
        help='One connected company, e.g. "Tata Sons". Used to disambiguate the person.',
    )
    parser.add_argument("--out", help="Output basename (default: from the name)")
    parser.add_argument("--max-news", type=int, default=25, help="Articles per company")
    parser.add_argument("--max-companies", type=int, default=12, help="Companies to screen")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds between requests to the same host")
    parser.add_argument("--workers", type=int, default=6,
                        help="Companies screened in parallel (1 disables threading)")
    parser.add_argument("--cache-dir", default=".cache", help="Response cache directory")
    parser.add_argument("--no-cache", action="store_true", help="Bypass the response cache")

    group = parser.add_argument_group("Firecrawl (optional)")
    group.add_argument(
        "--firecrawl-key", default=os.environ.get("FIRECRAWL_API_KEY"),
        help="Defaults to FIRECRAWL_API_KEY in .env or the environment",
    )
    group.add_argument("--no-firecrawl", action="store_true", help="Skip Firecrawl entirely")
    group.add_argument("--firecrawl-queries", type=int, default=4)
    group.add_argument("--firecrawl-limit", type=int, default=8)
    group.add_argument("--firecrawl-pages", type=int, default=6)
    group.add_argument(
        "--no-extract", action="store_true",
        help="Fetch pages as text only, without schema-guided extraction",
    )

    args = parser.parse_args()

    key = None if args.no_firecrawl else args.firecrawl_key
    options = Options(
        max_news=args.max_news,
        max_companies=args.max_companies,
        delay=args.delay,
        firecrawl_key=key,
        firecrawl_queries=args.firecrawl_queries,
        firecrawl_limit=args.firecrawl_limit,
        firecrawl_pages=args.firecrawl_pages,
        firecrawl_extract=not args.no_extract,
        workers=args.workers,
        cache_dir=None if args.no_cache else args.cache_dir,
    )

    print(f"\nScreening: {args.name}" + (f" via {args.company}" if args.company else ""))
    if key:
        print("  Firecrawl key detected; web search enabled.")
    print()

    from affluense.enrich.sentiment import SentimentAnalyzer
    if "DEGRADED" in SentimentAnalyzer().backend:
        print("  WARNING: vaderSentiment missing; sentiment quality is degraded.")
        print("           Run: pip install -r requirements.txt\n")

    result = run(args.name, args.company, options)
    report = output.build_report(result, args.name, args.company)

    basename = args.out or re.sub(r"[^a-z0-9]+", "-", args.name.lower()).strip("-")
    json_path, csv_path = f"{basename}.json", f"{basename}.csv"
    output.write_json(report, json_path)
    output.write_csv(report, csv_path)

    output.summarise(report)
    print(f"\n  Written to {json_path} and {csv_path}\n")


if __name__ == "__main__":
    sys.exit(main())
