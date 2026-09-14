"""Settings and shared constants. No logic, no network calls."""

from __future__ import annotations

import os
import pathlib


def load_env_file(path: pathlib.Path | None = None) -> None:
    """Read KEY=VALUE lines from a .env at the project root.

    Values already exported in the real environment win, so an explicit
    export overrides the file. Deliberately dependency-free.
    """
    env_path = path or pathlib.Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


# Loaded here, at import, and not left to the caller. Every setting below is
# read once at module level, so a .env loaded afterwards would arrive too late
# and the key would read as absent -- silently, and only for the settings that
# are constants rather than per-request lookups. Callers may still call this
# again; it is idempotent.
load_env_file()

USER_AGENT = (
    "affluense-screening/2.0 (public-information research; "
    "contact: set-your-email-here) python-requests"
)

# Statuses that mean "slow down", not "go away".
RETRY_STATUS = {202, 429, 502, 503, 504}

# --- what a run costs ------------------------------------------------------
# Firecrawl bills per page and per search result, not per token. These are
# list defaults; your plan may differ, so every one is overridable from .env.
# Nothing in this codebase estimates a cost: these rates are multiplied by
# counters incremented at the call site.
FIRECRAWL_CREDITS_SEARCH = float(os.getenv("FIRECRAWL_CREDITS_SEARCH", "1"))
FIRECRAWL_CREDITS_SCRAPE = float(os.getenv("FIRECRAWL_CREDITS_SCRAPE", "1"))
# Structured extraction runs a model over the page, and costs more than the
# markdown fetch it includes.
FIRECRAWL_CREDITS_SCRAPE_JSON = float(os.getenv("FIRECRAWL_CREDITS_SCRAPE_JSON", "5"))

# Hobby plan list price: $16 for 3,000 credits. Set FIRECRAWL_USD_PER_CREDIT
# in .env to your plan's real rate.
FIRECRAWL_USD_PER_CREDIT = float(
    os.getenv("FIRECRAWL_USD_PER_CREDIT", str(16.0 / 3000.0))
)

# "trial" or "paid". On a free trial no money changes hands, and reporting a
# list price as if it did is a fabricated number wearing a currency symbol.
# What matters on a trial is the allowance being consumed, so that is what is
# reported instead.
FIRECRAWL_PLAN = os.getenv("FIRECRAWL_PLAN", "trial").strip().lower()
# The trial's total credit allowance, for "used X of Y". 0 means unknown, and
# the report then shows credits consumed without a remaining figure rather
# than inventing a denominator.
FIRECRAWL_TRIAL_CREDITS = float(os.getenv("FIRECRAWL_TRIAL_CREDITS", "0"))

# Fallback only. The meter fetches the live rate first and falls back here,
# and the report always says which of the two it used.
USD_INR_RATE = float(os.getenv("USD_INR_RATE", "88.0"))
USD_INR_API = "https://open.er-api.com/v6/latest/USD"

# --- OpenAI ----------------------------------------------------------------
# The reasoning layer, not the search engine. It ranks identity candidates,
# reads ambiguous relationships, and gives a second opinion on articles that
# already raised a flag. It never scores ordinary sentiment: VADER does that,
# for free and reproducibly.
#
# Called over plain HTTPS through the shared Fetcher rather than through the
# `openai` SDK, so there is no extra dependency to install and every call is
# rate-limited, retried and cached like any other source.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
# A placeholder is not a key. Treating one as configured produces 401s that
# look like outages.
if OPENAI_API_KEY.lower().startswith("your_") or OPENAI_API_KEY.lower() == "changeme":
    OPENAI_API_KEY = ""

OPENAI_BASE = os.getenv("OPENAI_BASE", "https://api.openai.com/v1")
OPENAI_CHAT = f"{OPENAI_BASE}/chat/completions"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "40"))

# Per *million* tokens, which is how OpenAI publishes them. Defaults are
# gpt-4o-mini list price; set these in .env to match the model you choose.
# Cost is computed from the token counts the API itself returns, never from
# an estimate made here.
OPENAI_USD_PER_MTOK_IN = float(os.getenv("OPENAI_USD_PER_MTOK_IN", "0.15"))
OPENAI_USD_PER_MTOK_OUT = float(os.getenv("OPENAI_USD_PER_MTOK_OUT", "0.60"))

# Ceilings, so a pathological run cannot empty the account. Each is the most
# calls one run may make for that purpose.
OPENAI_MAX_CANDIDATE_CALLS = int(os.getenv("OPENAI_MAX_CANDIDATE_CALLS", "2"))
OPENAI_MAX_REVIEW_CALLS = int(os.getenv("OPENAI_MAX_REVIEW_CALLS", "15"))
# One extraction call per company plus the person, with headroom for retries.
OPENAI_MAX_EXTRACT_CALLS = int(os.getenv("OPENAI_MAX_EXTRACT_CALLS", "20"))
# Articles per extraction call. Larger batches are cheaper but more likely to
# have the model lose track of which answer belongs to which article.
OPENAI_EXTRACT_BATCH = int(os.getenv("OPENAI_EXTRACT_BATCH", "12"))
OPENAI_MAX_CLUSTER_CALLS = int(os.getenv("OPENAI_MAX_CLUSTER_CALLS", "6"))
OPENAI_MAX_QC_CALLS = int(os.getenv("OPENAI_MAX_QC_CALLS", "2"))
OPENAI_MAX_NETWORK_CALLS = int(os.getenv("OPENAI_MAX_NETWORK_CALLS", "2"))

# --- evidence collection ---------------------------------------------------
# Queries per company or person, adverse groups first. 0 means no cap.
# Each query costs two free RSS requests, so the ceiling is time, not money.
MAX_QUERIES_PER_ENTITY = int(os.getenv("MAX_QUERIES_PER_ENTITY", "15"))

# Articles per query per feed. Lower than the old per-company figure because
# there are now many queries rather than one.
NEWS_PER_QUERY = int(os.getenv("NEWS_PER_QUERY", "8"))

# Body text is fetched only for articles that already look material. This is
# the ceiling per run.
MAX_FULLTEXT_FETCHES = int(os.getenv("MAX_FULLTEXT_FETCHES", "40"))
# Firecrawl as a fallback for JS-walled pages. Defaults to 0: opt in, because
# it is the only source here that spends credits.
MAX_FULLTEXT_FIRECRAWL = int(os.getenv("MAX_FULLTEXT_FIRECRAWL", "0"))
# Characters of body text kept per article. Enough for an extraction model to
# read the substance without paying for the whole page.
FULLTEXT_CHARS = int(os.getenv("FULLTEXT_CHARS", "6000"))

# An equity stake at or above this level is control, whatever job title a
# page happens to print. Below it the holding stays an investment: a minority
# shareholder does not answer for the company's conduct.
CONTROL_STAKE_PERCENT = 50.0

DEFAULT_DELAY = 1.0
DEFAULT_TIMEOUT = 25
SPARQL_TIMEOUT = 75

# --- endpoints -------------------------------------------------------------
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIPEDIA_REST = "https://en.wikipedia.org/api/rest_v1/page/summary/"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_ENTITY = "https://www.wikidata.org/wiki/Special:EntityData/"
WIKIDATA_ITEM = "https://www.wikidata.org/wiki/"
WDQS = "https://query.wikidata.org/sparql"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
BING_NEWS_RSS = "https://www.bing.com/news/search"
DUCKDUCKGO_IA = "https://api.duckduckgo.com/"
FIRECRAWL_BASE = "https://api.firecrawl.dev"

# --- Wikidata property maps ------------------------------------------------
# Read off the person's own entity.
PERSON_CLAIMS = {
    "P106": "occupation",
    "P108": "employer",
    "P39": "position held",
    "P1830": "owner of",
    "P69": "educated at",
    "P27": "country of citizenship",
    "P937": "work location",
    "P2218": "net worth",
    "P26": "spouse",
    "P22": "father",
    "P25": "mother",
    "P1038": "relative",
    "P3373": "sibling",
    "P463": "member of",
}

# Found on an organisation, pointing back at a person.
ORG_TO_PERSON = {
    "P112": "founder",
    "P169": "chief executive officer",
    "P488": "chairperson",
    "P1037": "director / manager",
    "P3320": "board member",
    "P127": "owned by",
}

# Properties that tie an organisation to whoever controls it. Used to check
# a stake claimed in prose against a structured source before that company is
# screened as the subject's own.
ORG_OWNERSHIP = ("P127", "P749", "P112", "P169", "P488", "P1037", "P3320")

# --- Firecrawl domain policy ----------------------------------------------
# Login-walled or JS-only: a scrape here returns 403 and costs a credit.
SKIP_DOMAINS = {
    "instagram.com", "facebook.com", "x.com", "twitter.com", "linkedin.com",
    "youtube.com", "youtu.be", "tiktok.com", "pinterest.com", "threads.net",
    "reddit.com", "quora.com", "whatsapp.com", "t.me",
}

# Where credits have measurably paid off. Registry mirrors first: these carry
# Director Identification Numbers and filing-backed company lists.
PREFERRED_DOMAINS = [
    "zaubacorp.com", "falconebiz.com", "tofler.in", "indiafilings.com",
    "quickcompany.in", "opencorporates.com", "crunchbase.com",
    "bloomberg.com", "reuters.com", "forbes.com", "moneycontrol.com",
    "economictimes.indiatimes.com", "business-standard.com", "livemint.com",
    "thehindubusinessline.com", "financialexpress.com",
]
