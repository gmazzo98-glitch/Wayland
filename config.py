"""
Project Vienna Config: Signal Definitions, Freshness Windows, and Pipeline Controls.
Strictly based on GG_Dashboard_Technical_Brief.docx & GG_Signal_Sourcing_Plan.docx
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()  # loads .env if present — see .env.example for what it can set

def get_config_var(key: str, default: str = None) -> str:
    """Retrieve config from env var or streamlit.secrets if available."""
    val = os.getenv(key)
    if val:
        return val
    try:
        import streamlit as st
        if hasattr(st, "secrets") and key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return default

# Database URI (SQLite default for simple local/cloud deployment).
# `or` rather than getenv's own default arg — a DATABASE_URL line present in
# .env but left blank must still fall back to SQLite, not try to connect to an empty string.
raw_db_url = get_config_var("DATABASE_URL")
if raw_db_url and raw_db_url.strip():
    raw_db_url = raw_db_url.strip()
    # Normalize legacy Heroku/Supabase postgres:// schemes to postgresql://
    if raw_db_url.startswith("postgres://"):
        raw_db_url = raw_db_url.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URI = raw_db_url
else:
    DB_PATH = os.path.join(os.path.dirname(__file__), "vienna.db")
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{DB_PATH}"

# ---------------------------------------------------------------------------
# Source credentials (Section 7 of the Technical Brief: budget/access are open
# decisions, not assumptions — every key below is read from the environment,
# never hardcoded, and every adapter falls back to a clearly-labeled simulated
# value when its credentials are missing. Each adapter module documents where
# to obtain its own credentials (free registration URL) in its docstring.
# ---------------------------------------------------------------------------
EPO_OPS_CONSUMER_KEY = get_config_var("EPO_OPS_CONSUMER_KEY")
EPO_OPS_CONSUMER_SECRET = get_config_var("EPO_OPS_CONSUMER_SECRET")

EUIPO_CLIENT_ID = get_config_var("EUIPO_CLIENT_ID")
EUIPO_CLIENT_SECRET = get_config_var("EUIPO_CLIENT_SECRET")
# EUIPO's token endpoint is issued per-app on dev.euipo.europa.eu — confirm the
# current value there rather than trusting a hardcoded default.
EUIPO_TOKEN_URL = os.getenv("EUIPO_TOKEN_URL", "https://auth.euipo.europa.eu/oidc/accessToken")
EUIPO_SEARCH_URL = os.getenv("EUIPO_SEARCH_URL", "https://api.euipo.europa.eu/trademark-search/trademarks")

DESTATIS_USERNAME = os.getenv("DESTATIS_USERNAME")
DESTATIS_PASSWORD = os.getenv("DESTATIS_PASSWORD")
# GENESIS-Online table code to pull sector export exposure from — deliberately not
# defaulted. The sourcing plan names the API but not a specific table, and guessing
# a table ID would silently fabricate a "real" number; this is an open decision
# (Section 7 of the Technical Brief) left for a human to confirm on genesis.destatis.de.
DESTATIS_EXPORT_TABLE_CODE = os.getenv("DESTATIS_EXPORT_TABLE_CODE")

# Publicly documented, non-secret defaults (no registration gate) — confirmed against
# each source's own published API docs / open-source reference clients. Still
# overridable via env var in case either publisher rotates them.
EU_FUNDING_API_KEY = os.getenv("EU_FUNDING_API_KEY", "SEDIA")
ARBEITSAGENTUR_API_KEY = os.getenv("ARBEITSAGENTUR_API_KEY", "jobboerse-jobsuche")

GOOGLE_CSE_API_KEY = os.getenv("GOOGLE_CSE_API_KEY")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID")

# ---------------------------------------------------------------------------
# Node-based crawlers (scrapers/*_crawler.py) — the 8 independent Crawlee
# packages under the sibling Scraper/crawlers/ folder (not part of this git
# repo). Same honest-pipeline contract as everything else: each wrapper falls
# back to run_adapter's simulate() path whenever the crawler folder, Node, or
# a required key is missing, rather than guessing.
# ---------------------------------------------------------------------------
# Defaults to the sibling "Scraper/crawlers" folder next to this project
# (".../Wayland/Project Vienna" and ".../Wayland/Scraper" are siblings on this
# machine). Override if the crawlers ever move or run from a different host.
SCRAPER_CRAWLERS_DIR = os.getenv("SCRAPER_CRAWLERS_DIR") or str(
    Path(__file__).resolve().parent.parent / "Scraper" / "crawlers"
)

# Crawler Worker (worker_hub.py): lets a helper's own computer run the crawlers when this
# app is hosted somewhere that has no Scraper/crawlers folder (Streamlit Cloud). The installer
# the Crawler Setup page hands out carries this PUBLIC value (the worker_shim/ service's URL)
# and nothing secret — the worker's own per-install token is what authorises it.
WORKER_SHIM_URL = get_config_var("WORKER_SHIM_URL")

# news-signals-crawler / innovation-participation-crawler still call the Anthropic API
# directly for classification (unconverted — they're gated primarily on NEWSAPI_KEY
# below anyway, and their News/Press signals already have a free path via
# adapters/google_news_rss.py, so converting these two was left out of scope).
# Optional for both: missing key just means a lower-confidence classification, not a
# blocker, per each crawler's own graceful-degradation behavior.
CRAWLER_ANTHROPIC_API_KEY = os.getenv("CRAWLER_ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY")

# company-website-crawler's LLM field extraction — converted off Anthropic (cost) onto
# any OpenAI-compatible endpoint. Defaults to Groq: free, no credit card, rate-limited
# only. Point these three at Gemini/Cerebras/a local Ollama instead with no code change —
# see Scraper/crawlers/company-website-crawler/.env.example for exact values per provider.
CRAWLER_LLM_API_KEY = os.getenv("CRAWLER_LLM_API_KEY")
CRAWLER_LLM_BASE_URL = os.getenv("CRAWLER_LLM_BASE_URL", "https://api.groq.com/openai/v1")
# Verified live against GET https://api.groq.com/openai/v1/models on 2026-09-16 —
# llama-3.3-70b-versatile (this default's first value) 404s, doesn't exist on Groq's
# current catalog at all. Their catalog moves; re-check against your own key if this
# 404s later. gpt-oss-120b is one of only two Groq models documented to support
# strict schema-guaranteed tool calling (the other: gpt-oss-20b, smaller/faster).
CRAWLER_LLM_MODEL = os.getenv("CRAWLER_LLM_MODEL", "openai/gpt-oss-120b")

# Optional SECOND provider for the same extraction. The free tier's per-minute token cap is the
# hard limit on how fast company-website-crawler can go (see resource_governor.py), and running
# two crawls against ONE account only makes both hit 429s. A second free account (e.g. Cerebras
# or Google AI Studio — both OpenAI-compatible) is the only way past it: when the primary
# answers 429, the call goes to this one instead of waiting the cap out. Unset = no fallback.
CRAWLER_LLM_FALLBACK_API_KEY = os.getenv("CRAWLER_LLM_FALLBACK_API_KEY")
CRAWLER_LLM_FALLBACK_BASE_URL = os.getenv("CRAWLER_LLM_FALLBACK_BASE_URL")
CRAWLER_LLM_FALLBACK_MODEL = os.getenv("CRAWLER_LLM_FALLBACK_MODEL")

# THIRD+ providers, numbered (CRAWLER_LLM_FALLBACK2_*, CRAWLER_LLM_FALLBACK3_*, ...) — each one
# configured is another genuinely separate token-per-minute bucket the crawler can fall through to
# (llm.ts's loadProviders() reads the same numbering). Not a hard cap at 5; add another suffix
# here and to FALLBACK_SUFFIXES in llm.ts if that's ever not enough.
CRAWLER_LLM_EXTRA_FALLBACK_SUFFIXES = ("2", "3", "4", "5")


def _numbered_llm_fallbacks(getenv=os.getenv) -> list:
    """Pulled out as a function of `getenv` (defaults to os.getenv) purely so a test can pass a
    fake env dict's .get instead of monkeypatching the real environment for a module-level constant."""
    fallbacks = []
    for s in CRAWLER_LLM_EXTRA_FALLBACK_SUFFIXES:
        key = getenv(f"CRAWLER_LLM_FALLBACK{s}_API_KEY")
        if not key:
            continue
        fallbacks.append({"suffix": s, "api_key": key,
                            "base_url": getenv(f"CRAWLER_LLM_FALLBACK{s}_BASE_URL"),
                            "model": getenv(f"CRAWLER_LLM_FALLBACK{s}_MODEL")})
    return fallbacks


CRAWLER_LLM_EXTRA_FALLBACKS = _numbered_llm_fallbacks()

# news-signals-crawler / innovation-participation-crawler: without a real key
# these default to SEARCH_PROVIDER=mock, which returns FIXED FAKE Wikipedia
# search results — that is never acceptable to present as a live pull, so the
# wrappers below only ever invoke the crawler with SEARCH_PROVIDER=newsapi,
# and skip straight to simulate() when this is unset (see scrapers/news_signals_crawler.py).
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY")

# digital-maturity-crawler: optional, improves cms_platform/has_ecommerce detection.
# Missing key just means data_source="heuristic" for that company, not a failure.
BUILTWITH_API_KEY = os.getenv("BUILTWITH_API_KEY")

# linkedin-profile-crawler stays off by default — the sourcing plan explicitly
# names LinkedIn a "highest-risk scrape target" and says to buy via a compliant
# reseller rather than scrape directly (see vienna-api-integration-status memory).
# Flipping this on is a deliberate per-deployment decision, not a credential check.
LINKEDIN_CRAWLER_ENABLED = os.getenv("LINKEDIN_CRAWLER_ENABLED", "false").lower() == "true"
LINKEDIN_LI_AT = os.getenv("LINKEDIN_LI_AT")

# review-crawler's Mode B (Kununu/Glassdoor employer reviews) is a direct,
# robots.txt-respecting scrape — a different risk profile than the paid-reseller
# path scrapers/kununu_light.py is deliberately gated behind, but still a fresh
# decision worth its own explicit flag rather than defaulting to on.
KUNUNU_CRAWLER_ENABLED = os.getenv("KUNUNU_CRAWLER_ENABLED", "false").lower() == "true"
# review-crawler's Mode A (Google Maps customer reviews -> product_quality_trend) is
# OFF by default too, since the 2026-09-18 verification pass: robots.txt allows
# /maps/search/ and /maps/place/, but the Google Maps Additional Terms of Service
# prohibit copying content and using Maps to "create or augment ... any business
# listings database", which storing ratings and review snippets per prospect is —
# and the crawler dismisses Google's GDPR consent screen on every run. On top of the
# ToS question the signal is worthless for this segment: the 16 real manufacturers
# checked had 4-67 reviews each, and the 12-month trend needs 10+ dated reviews in
# each of two consecutive years. Trustpilot was dropped outright (its robots.txt is
# `User-agent: *` / `Disallow: /`). Flip this on only as a deliberate decision.
REVIEW_CRAWLER_MODE_A_ENABLED = os.getenv("REVIEW_CRAWLER_MODE_A_ENABLED", "false").lower() == "true"

# Real per-document/reseller pulls are a genuine build (headless-browser PDF parsing,
# compliant reseller contracts) that's out of scope for this pass — see
# scrapers/bundesanzeiger_paid.py and scrapers/kununu_light.py for what's missing.
# These stay simulated until explicitly flipped on.
BUNDESANZEIGER_PAID_ENABLED = os.getenv("BUNDESANZEIGER_PAID_ENABLED", "false").lower() == "true"
KUNUNU_RESELLER_ENABLED = os.getenv("KUNUNU_RESELLER_ENABLED", "false").lower() == "true"

# Source -> env vars a human needs to set for that source to run live instead of
# simulated. Empty list means "no registration gate, live by default" (still subject
# to network/parse failures, which fall back to simulated per-run).
SOURCE_CREDENTIAL_VARS = {
    "EPO OPS": ["EPO_OPS_CONSUMER_KEY", "EPO_OPS_CONSUMER_SECRET"],
    "EUIPO": ["EUIPO_CLIENT_ID", "EUIPO_CLIENT_SECRET"],
    "Destatis": ["DESTATIS_USERNAME", "DESTATIS_PASSWORD", "DESTATIS_EXPORT_TABLE_CODE"],
    "EU Funding Portal": [],
    # Eurostat's dissemination API is public/keyless, same posture as EU Funding Portal.
    "Eurostat Sector Growth": [],
    "Eurostat Export Exposure": [],
    "Arbeitsagentur": [],
    "Wappalyzer": [],
    "Google News": ["GOOGLE_CSE_API_KEY", "GOOGLE_CSE_ID"],
    # Keyless public RSS feed — the free default for News/Press when no CSE key is set.
    "Google News RSS": [],
    "Own-Site Scrape": [],
    "Handelsregister Free Snapshot": [],
    # Phase 7 — Node-based Crawlee crawlers (scrapers/*_crawler.py). Empty list means
    # "runs without a key, just with weaker extraction" (matches each crawler's own
    # graceful-degradation behavior); News Signals / Innovation Participation hard-gate
    # on NEWSAPI_KEY because their un-keyed default is a FAKE fixture provider, not a
    # plain absence of enrichment — see scrapers/news_signals_crawler.py.
    # Company Website Crawler's fields are all LLM-extracted — no key means it
    # returns not_found for every one of them, so it's a real gate, not an enhancement.
    "Company Website Crawler": ["CRAWLER_LLM_API_KEY"],
    "Job Postings Crawler": [],
    "News Signals Crawler": ["NEWSAPI_KEY"],
    "Directory Listing Crawler": [],
    "Innovation Participation Crawler": ["NEWSAPI_KEY"],
    "Digital Maturity Crawler": [],
}

# Paid/flag-gated sources are switched on by an explicit enable flag rather than a
# credential var — "real" here means a deliberate risk/ToS decision, not just a key.
SOURCE_PAID_ENABLE_FLAGS = {
    "Bundesanzeiger": BUNDESANZEIGER_PAID_ENABLED,
    "Kununu Reseller": KUNUNU_RESELLER_ENABLED,
    "LinkedIn Profile Crawler": LINKEDIN_CRAWLER_ENABLED,
    # Off unless at least one of its two modes is deliberately switched on.
    "Review Crawler": REVIEW_CRAWLER_MODE_A_ENABLED or KUNUNU_CRAWLER_ENABLED,
}

def has_credentials(source_name: str) -> bool:
    """True if this source is currently configured to run live instead of simulated."""
    if source_name in SOURCE_PAID_ENABLE_FLAGS:
        return SOURCE_PAID_ENABLE_FLAGS[source_name]
    required = SOURCE_CREDENTIAL_VARS.get(source_name, [])
    return all(get_config_var(var) for var in required)

# Indicator catalog (weights, axis, normalization bounds, freshness) lives in the
# IndicatorDefinition table now — see indicators.py for the seed and
# indicators.fetch_indicator_defs() for the accessor. This replaces the old
# hardcoded SIGNAL_METADATA/FRESHNESS_WINDOWS dicts so the whole catalog is
# editable from the Indicator Weights page without a code change.

# Pipeline Phases (Section 3 of Technical Brief). Phase 6 extends the original
# five with indicators that aren't pipeline-automatable at all — first-contact
# interview data, entered by hand from the Company Intelligence page.
PHASE_CONFIG = {
    1: {"name": "Phase 1: Free Documented APIs", "auto_run": True, "requires_approval": False},
    2: {"name": "Phase 2: Handelsregister Base & Free Scraping", "auto_run": True, "requires_approval": False},
    3: {"name": "Phase 3: Targeted Paid Document Pulls", "auto_run": False, "requires_approval": True},
    4: {"name": "Phase 4: Website & Light Social Layer", "auto_run": True, "requires_approval": False},
    5: {"name": "Phase 5: Paid Social & Review Data", "auto_run": False, "requires_approval": True},
    6: {"name": "Phase 6: Manual / First-Contact Data", "auto_run": False, "requires_approval": False},
    # Phase 7 — the 8 Node/Crawlee crawlers under Scraper/crawlers/. Deliberately
    # NOT auto_run: each call spawns a subprocess (Node/Playwright startup, sometimes
    # an LLM extraction call) that can take tens of seconds per company, so it must
    # stay an explicit, on-demand trigger rather than firing on every company creation
    # like Phase 1/4 do. requires_approval=False because nothing here is a paid/gated
    # pull by default — the two ToS-sensitive sources (LinkedIn, Kununu-via-crawler)
    # have their own dedicated off-by-default flags instead (see config.py above).
    7: {"name": "Phase 7: Crawler Deep Enrichment (slower, Node-based)", "auto_run": False, "requires_approval": False},
}

# Shortlist Gate Thresholds
SHORTLIST_MIN_COMPLETENESS_PCT = 40.0
SHORTLIST_MIN_PRELIMINARY_SCORE = 50.0
