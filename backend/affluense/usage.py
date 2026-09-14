"""What one run actually cost, measured rather than estimated.

Nothing here asks a model what a run cost. Every figure is a counter
incremented at the call site and multiplied by a rate from config, so the
same inputs always produce the same total and the arithmetic can be checked
by hand.

Only Firecrawl bills. Wikidata, Wikipedia, Google News, Bing News and
DuckDuckGo are free and keyless; they are still counted, because "we made
412 requests and paid for 31 of them" is the useful sentence.
"""

from __future__ import annotations

import threading
import urllib.parse

from . import config

# Firecrawl prices per page/result, not per token. A markdown scrape is one
# credit; asking for structured JSON runs an extraction model on the page and
# costs more. Check these against your own plan and override in .env.
CREDITS = {
    "firecrawl.search": config.FIRECRAWL_CREDITS_SEARCH,
    "firecrawl.scrape": config.FIRECRAWL_CREDITS_SCRAPE,
    "firecrawl.scrape.json": config.FIRECRAWL_CREDITS_SCRAPE_JSON,
}


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url or "").netloc or "unknown"


class UsageMeter:
    """Thread-safe tally of calls, credits and money for one run.

    Companies are screened in parallel, so every mutation takes the lock.
    """

    def __init__(self, usd_per_credit: float | None = None,
                 usd_to_inr: float | None = None,
                 rate_source: str = "config default",
                 plan: str | None = None):
        self.usd_per_credit = (
            config.FIRECRAWL_USD_PER_CREDIT if usd_per_credit is None else usd_per_credit
        )
        self.usd_to_inr = config.USD_INR_RATE if usd_to_inr is None else usd_to_inr
        self.rate_source = rate_source
        # Held on the instance rather than read from config inside summary().
        # A meter whose arithmetic changes with the contents of .env cannot be
        # tested, and quietly changed its answer when the plan was added.
        self.plan = config.FIRECRAWL_PLAN if plan is None else plan
        self._lock = threading.Lock()
        self.rows: dict = {}
        # Token counts as reported by the API, never counted or guessed here.
        self.tokens: dict = {"prompt": 0, "completion": 0, "calls": 0,
                             "cached_calls": 0, "prompt_saved": 0,
                             "completion_saved": 0}
        self.token_purposes: dict = {}

    def record(self, url: str, kind: str | None = None, units: int = 1,
               cached: bool = False) -> None:
        """One call against one host.

        A cache hit is recorded as a call that cost nothing, because the
        credits it did not spend are the most interesting number in the
        summary.
        """
        host = _host(url)
        credits_each = CREDITS.get(kind or "", 0)
        with self._lock:
            row = self.rows.setdefault(host, {
                "host": host,
                "calls": 0,
                "cached_calls": 0,
                "credits": 0,
                "credits_saved_by_cache": 0,
            })
            row["calls"] += 1
            if cached:
                row["cached_calls"] += 1
                row["credits_saved_by_cache"] += credits_each * units
            else:
                row["credits"] += credits_each * units

    def record_tokens(self, purpose: str, prompt: int, completion: int,
                      cached: bool = False) -> None:
        """Tokens an OpenAI call reported, split by what it was spent on.

        `prompt` and `completion` come from the API response's own `usage`
        block. Nothing here counts characters or approximates tokens: an
        invented token count is worse than no token count, because it looks
        equally authoritative on the invoice line.
        """
        with self._lock:
            self.tokens["calls"] += 1
            bucket = self.token_purposes.setdefault(purpose, {
                "purpose": purpose, "calls": 0, "prompt": 0, "completion": 0,
            })
            bucket["calls"] += 1
            if cached:
                self.tokens["cached_calls"] += 1
                self.tokens["prompt_saved"] += prompt
                self.tokens["completion_saved"] += completion
            else:
                self.tokens["prompt"] += prompt
                self.tokens["completion"] += completion
                bucket["prompt"] += prompt
                bucket["completion"] += completion

    def _token_usd(self, prompt: int, completion: int) -> float:
        return (
            prompt / 1_000_000 * config.OPENAI_USD_PER_MTOK_IN
            + completion / 1_000_000 * config.OPENAI_USD_PER_MTOK_OUT
        )

    def summary(self) -> dict:
        """Totals. All arithmetic happens here, in Python."""
        with self._lock:
            rows = [dict(r) for r in self.rows.values()]

        on_trial = self.plan == "trial"
        for row in rows:
            row["usd"] = (
                0.0 if on_trial
                else round(row["credits"] * self.usd_per_credit, 6)
            )
            row["inr"] = round(row["usd"] * self.usd_to_inr, 4)
        rows.sort(key=lambda r: (-r["credits"], r["host"]))

        calls = sum(r["calls"] for r in rows)
        cached_calls = sum(r["cached_calls"] for r in rows)
        credits = sum(r["credits"] for r in rows)
        saved = sum(r["credits_saved_by_cache"] for r in rows)
        with self._lock:
            tokens = dict(self.tokens)
            purposes = [dict(p) for p in self.token_purposes.values()]

        # On a trial the credits are real but the money is not. Billing them
        # at list price produced "$0.3307" for a run that cost nothing.
        credit_usd = 0.0 if on_trial else credits * self.usd_per_credit
        saved_usd = 0.0 if on_trial else saved * self.usd_per_credit
        token_usd = self._token_usd(tokens["prompt"], tokens["completion"])
        token_saved_usd = self._token_usd(
            tokens["prompt_saved"], tokens["completion_saved"]
        )
        for bucket in purposes:
            bucket["usd"] = round(
                self._token_usd(bucket["prompt"], bucket["completion"]), 6
            )
            bucket["inr"] = round(bucket["usd"] * self.usd_to_inr, 4)
        purposes.sort(key=lambda b: (-b["usd"], b["purpose"]))

        usd = credit_usd + token_usd
        total_saved_usd = saved_usd + token_saved_usd

        firecrawl = {
            "plan": self.plan,
            "credits_this_run": credits,
            "allowance": config.FIRECRAWL_TRIAL_CREDITS or None,
            "usd": round(credit_usd, 4),
        }
        if on_trial and config.FIRECRAWL_TRIAL_CREDITS:
            firecrawl["runs_left_at_this_rate"] = (
                int(config.FIRECRAWL_TRIAL_CREDITS // credits) if credits else None
            )

        return {
            "billing_unit": (
                "Firecrawl trial credits (no cash cost), plus OpenAI tokens"
                if on_trial
                else "Firecrawl credits, plus OpenAI tokens when a key is set"
            ),
            "note": (
                (
                    "Firecrawl is on a free trial, so its credits cost no money "
                    "— what they cost is trial allowance. "
                    if on_trial else ""
                )
                + "Sentiment is VADER, running locally and free. OpenAI is used "
                "only to rank identity candidates and to re-read articles that "
                "already raised a flag, so token spend scales with findings, "
                "not with articles. Every other source is free and keyless."
            ),
            "firecrawl": firecrawl,
            "rates": {
                "usd_per_credit": 0.0 if on_trial else self.usd_per_credit,
                "usd_per_mtok_in": config.OPENAI_USD_PER_MTOK_IN,
                "usd_per_mtok_out": config.OPENAI_USD_PER_MTOK_OUT,
                "usd_to_inr": self.usd_to_inr,
                "usd_to_inr_source": self.rate_source,
            },
            "totals": {
                "requests": calls,
                "served_from_cache": cached_calls,
                "billable_credits": credits,
                "credits_usd": round(credit_usd, 4),
                "prompt_tokens": tokens["prompt"],
                "completion_tokens": tokens["completion"],
                "total_tokens": tokens["prompt"] + tokens["completion"],
                "openai_calls": tokens["calls"],
                "openai_usd": round(token_usd, 4),
                "usd": round(usd, 4),
                "inr": round(usd * self.usd_to_inr, 2),
                "credits_saved_by_cache": saved,
                "tokens_saved_by_cache": (
                    tokens["prompt_saved"] + tokens["completion_saved"]
                ),
                "usd_saved_by_cache": round(total_saved_usd, 4),
                "inr_saved_by_cache": round(total_saved_usd * self.usd_to_inr, 2),
            },
            "by_host": rows,
            "openai_by_purpose": purposes,
        }


def resolve_inr_rate(fetcher) -> tuple:
    """(rate, source). Live if the free endpoint answers, else the constant.

    The rate is never guessed and never hard-coded silently: the report
    prints which of the two it used, so a stale figure is visible rather
    than quietly wrong.
    """
    data = fetcher.get_json(config.USD_INR_API)
    rate = ((data or {}).get("rates") or {}).get("INR")
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        rate = None
    if rate and rate > 0:
        return rate, f"live from {_host(config.USD_INR_API)}"
    return config.USD_INR_RATE, "fallback constant (live rate unavailable)"


def format_summary(summary: dict) -> str:
    """The one-block console report printed at the end of a run."""
    totals = summary["totals"]
    rates = summary["rates"]
    lines = [
        "",
        "  Cost of this run",
        "  " + "-" * 52,
        f"  {'requests made':<26} {totals['requests']:>10,}",
        f"  {'served from cache':<26} {totals['served_from_cache']:>10,}",
    ]

    firecrawl = summary.get("firecrawl", {})
    if firecrawl.get("plan") == "trial":
        allowance = firecrawl.get("allowance")
        suffix = f" of {allowance:,.0f} trial" if allowance else " trial"
        lines.append(
            f"  {'Firecrawl credits':<26} {totals['billable_credits']:>10,g}"
            f"{suffix}   no cash cost"
        )
    else:
        lines.append(
            f"  {'Firecrawl credits':<26} {totals['billable_credits']:>10,g}"
            f"   ${totals['credits_usd']:.4f}"
        )

    if totals["openai_calls"]:
        lines.append(
            f"  {'OpenAI tokens':<26} {totals['total_tokens']:>10,}"
            f"   ${totals['openai_usd']:.4f}"
        )
        lines.append(
            f"  {'  in / out':<26} "
            f"{totals['prompt_tokens']:,} / {totals['completion_tokens']:,}"
            f"  across {totals['openai_calls']} call(s)"
        )
        for bucket in summary.get("openai_by_purpose", []):
            lines.append(
                f"      {bucket['purpose']:<22} "
                f"{bucket['prompt'] + bucket['completion']:>8,} tok"
                f"   ${bucket['usd']:.4f}"
            )

    lines.append(
        f"  {'spent':<26} {'$' + format(totals['usd'], '.4f'):>10}"
        f"   Rs {totals['inr']:,.2f}"
    )
    if totals["usd_saved_by_cache"]:
        lines.append(
            f"  {'saved by cache':<26} "
            f"{'$' + format(totals['usd_saved_by_cache'], '.4f'):>10}"
            f"   Rs {totals['inr_saved_by_cache']:,.2f}"
        )
    lines += [
        "  " + "-" * 52,
        "  sentiment runs locally on VADER and costs nothing",
        f"  rates: ${rates['usd_per_credit']:.5f}/credit, "
        f"${rates['usd_per_mtok_in']}/${rates['usd_per_mtok_out']} per Mtok in/out, "
        f"1 USD = Rs {rates['usd_to_inr']:.2f} ({rates['usd_to_inr_source']})",
        "",
    ]
    return "\n".join(lines)
