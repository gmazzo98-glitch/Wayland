"""
Project Vienna Config: Signal Definitions, Freshness Windows, and Pipeline Controls.
Strictly based on GG_Dashboard_Technical_Brief.docx & GG_Signal_Sourcing_Plan.docx
"""

import os
from dotenv import load_dotenv

load_dotenv()  # loads .env if present — see .env.example for what it can set

# Database URI (SQLite default for simple local/cloud deployment)
DB_PATH = os.path.join(os.path.dirname(__file__), "vienna.db")
SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")

# ---------------------------------------------------------------------------
# Source credentials (Section 7 of the Technical Brief: budget/access are open
# decisions, not assumptions — every key below is read from the environment,
# never hardcoded, and every adapter falls back to a clearly-labeled simulated
# value when its credentials are missing. Each adapter module documents where
# to obtain its own credentials (free registration URL) in its docstring.
# ---------------------------------------------------------------------------
EPO_OPS_CONSUMER_KEY = os.getenv("EPO_OPS_CONSUMER_KEY")
EPO_OPS_CONSUMER_SECRET = os.getenv("EPO_OPS_CONSUMER_SECRET")

EUIPO_CLIENT_ID = os.getenv("EUIPO_CLIENT_ID")
EUIPO_CLIENT_SECRET = os.getenv("EUIPO_CLIENT_SECRET")
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
    "Arbeitsagentur": [],
    "Wappalyzer": [],
    "Google News": ["GOOGLE_CSE_API_KEY", "GOOGLE_CSE_ID"],
    "Own-Site Scrape": [],
    "Handelsregister Free Snapshot": [],
}

# Paid sources (Phase 3/5) are gated by an explicit enable flag, not a credential var —
# "real" here means a compliant scraper/contract being switched on, not just a key.
SOURCE_PAID_ENABLE_FLAGS = {
    "Bundesanzeiger": BUNDESANZEIGER_PAID_ENABLED,
    "Kununu Reseller": KUNUNU_RESELLER_ENABLED,
}

def has_credentials(source_name: str) -> bool:
    """True if this source is currently configured to run live instead of simulated."""
    if source_name in SOURCE_PAID_ENABLE_FLAGS:
        return SOURCE_PAID_ENABLE_FLAGS[source_name]
    required = SOURCE_CREDENTIAL_VARS.get(source_name, [])
    return all(os.getenv(var) for var in required)

# Freshness window per signal_key in days (Section 2.2 of Brief)
FRESHNESS_WINDOWS = {
    # Financial signals (Annual filings from Bundesanzeiger)
    "interest_coverage_ratio": 365,
    "rd_expense_ratio": 365,
    "margin_compression": 365,
    
    # Innovation & IP signals (Medium-term updates)
    "patent_count": 90,
    "patent_ipc_diversity": 90,
    "trademark_count": 90,
    
    # Trade & Grants
    "sector_export_exposure": 180,
    "public_grant_count": 180,
    
    # Digital & Job posting velocity (Fast changing)
    "job_posting_velocity": 14,
    "tech_stack_intensity": 60,
    
    # Social & Culture
    "kununu_rating": 30,
    "management_diversity": 180,
    "partnership_news_count": 30,
}

# Signal metadata and two-axis alignment.
# "invert": True means a LOWER raw value indicates MORE pressure/need (e.g. thin
# interest cover), so normalization flips the scale before it feeds the need axis.
SIGNAL_METADATA = {
    # NEED AXIS (Could Innovate: Export pressure, margin compression, digital gaps)
    "margin_compression": {"axis": "need", "label": "Margin Compression", "source": "Bundesanzeiger", "phase": 3, "cost_per_pull": 5.0},
    "interest_coverage_ratio": {"axis": "need", "label": "Interest Coverage Ratio (Financial Pressure)", "source": "Bundesanzeiger", "phase": 3, "cost_per_pull": 5.0, "invert": True},
    "sector_export_exposure": {"axis": "need", "label": "Sector Export Pressure", "source": "Destatis", "phase": 1, "cost_per_pull": 0.0},
    "job_posting_velocity": {"axis": "need", "label": "Job Posting Velocity", "source": "Arbeitsagentur", "phase": 1, "cost_per_pull": 0.0},
    "tech_stack_intensity": {"axis": "need", "label": "Digital Intensity Index", "source": "Wappalyzer", "phase": 4, "cost_per_pull": 0.0},

    # READINESS AXIS (Is Innovating: R&D spend, patents, trademarks, grants, culture)
    "rd_expense_ratio": {"axis": "readiness", "label": "R&D Spend Ratio", "source": "Bundesanzeiger", "phase": 3, "cost_per_pull": 5.0},
    "patent_count": {"axis": "readiness", "label": "Patent Count", "source": "EPO OPS", "phase": 1, "cost_per_pull": 0.0},
    "patent_ipc_diversity": {"axis": "readiness", "label": "Patent IPC Class Diversity", "source": "EPO OPS", "phase": 1, "cost_per_pull": 0.0},
    "trademark_count": {"axis": "readiness", "label": "Trademark Count", "source": "EUIPO", "phase": 1, "cost_per_pull": 0.0},
    "public_grant_count": {"axis": "readiness", "label": "Public Grant Count", "source": "EU Funding Portal", "phase": 1, "cost_per_pull": 0.0},
    "management_diversity": {"axis": "readiness", "label": "Management Diversity / Background", "source": "Own-Site Scrape", "phase": 4, "cost_per_pull": 0.0},
    "kununu_rating": {"axis": "readiness", "label": "Kununu Culture Rating", "source": "Kununu Reseller", "phase": 5, "cost_per_pull": 2.5},
    "partnership_news_count": {"axis": "readiness", "label": "Partnership News Signals", "source": "Google News", "phase": 4, "cost_per_pull": 0.0},
}

# Pipeline Phases (Section 3 of Technical Brief)
PHASE_CONFIG = {
    1: {"name": "Phase 1: Free Documented APIs", "auto_run": True, "requires_approval": False},
    2: {"name": "Phase 2: Handelsregister Base & Free Scraping", "auto_run": True, "requires_approval": False},
    3: {"name": "Phase 3: Targeted Paid Document Pulls", "auto_run": False, "requires_approval": True},
    4: {"name": "Phase 4: Website & Light Social Layer", "auto_run": True, "requires_approval": False},
    5: {"name": "Phase 5: Paid Social & Review Data", "auto_run": False, "requires_approval": True},
}

# Shortlist Gate Thresholds
SHORTLIST_MIN_COMPLETENESS_PCT = 40.0
SHORTLIST_MIN_PRELIMINARY_SCORE = 50.0
