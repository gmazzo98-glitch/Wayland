"""
Kununu Culture Rating Puller (Phase 5 — manual-trigger, budget-gated only).
Populates kununu_rating from a light scrape of the company's public aggregate
rating page, per the sourcing plan's own note: no official API, low-volume
scrape of the aggregate rating only.

NOT YET REAL, and deliberately not faked: Kununu has no public company-search
or slug-lookup API, so a company can't be reliably mapped to its Kununu profile
URL from just its legal name — that mapping needs either a manual company->slug
table or a compliant reseller (the sourcing plan's own recommendation once past
Stage 1). Guessing a URL and scraping whatever it returns would silently produce
noise, which is worse than clearly staying simulated. KUNUNU_RESELLER_ENABLED
stays False by default.
"""

from sqlalchemy.orm import Session
from adapters.base import run_adapter
from config import KUNUNU_RESELLER_ENABLED, SIGNAL_METADATA

SOURCE_NAME = "Kununu Reseller"
PHASE = 5


def _fetch_live(company) -> dict:
    raise NotImplementedError(
        "No company->Kununu-profile mapping exists yet (Kununu has no public "
        "search/slug API). Leave KUNUNU_RESELLER_ENABLED=false until a mapping "
        "or a compliant reseller integration is wired in."
    )


def _simulate(company) -> dict:
    seed = sum(ord(c) for c in (company.registration_number or company.legal_name))
    rating = round(2.5 + (seed % 25) / 10.0, 1)  # ~2.5-5.0
    return {
        "signals": {"kununu_rating": {"value": rating, "status": "present"}},
        "raw_payload": {
            "source": SOURCE_NAME, "registration_number": company.registration_number,
            "note": "Real Kununu profile lookup not yet built — simulated placeholder",
        },
        "confidence": 0.4,
    }


def pull_kununu_rating(company, db_session: Session) -> dict:
    """Manual, gated pull — final shortlist only (Phase 5 of the sourcing plan)."""
    return run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=KUNUNU_RESELLER_ENABLED,
        fetch_live=_fetch_live, simulate=_simulate,
        cost_per_call=SIGNAL_METADATA["kununu_rating"]["cost_per_pull"],
    )
