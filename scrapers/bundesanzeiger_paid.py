"""
Bundesanzeiger Paid Filing Puller (Phase 3 — manual-trigger, budget-gated only).
Populates margin_compression, rd_expense_ratio, interest_coverage_ratio from a
full Bundesanzeiger Jahresabschluss filing.

NOT YET REAL, and deliberately not faked: the sourcing plan calls full-filing
retrieval Medium-High effort (a Playwright search/pay/download session flow
against a fragile HTML-only government site, then pdfplumber table extraction).
Building and validating that against the live site is out of scope for this
pass. BUNDESANZEIGER_PAID_ENABLED stays False by default so real spend is
never accrued against a pull nobody actually performed; flipping the flag on
without implementing _fetch_live raises loudly instead of silently returning
a fabricated "real" filing.
"""

from sqlalchemy.orm import Session
from adapters.base import run_adapter
from config import BUNDESANZEIGER_PAID_ENABLED, SIGNAL_METADATA

SOURCE_NAME = "Bundesanzeiger"
PHASE = 3


def _fetch_live(company) -> dict:
    raise NotImplementedError(
        "Bundesanzeiger paid filing retrieval is not built yet (needs a Playwright "
        "search/pay/download flow + pdfplumber table parsing). "
        "Leave BUNDESANZEIGER_PAID_ENABLED=false until this is implemented."
    )


def _simulate(company) -> dict:
    # Deterministic-but-varied per company so two shortlisted companies never show
    # identical "financials" — still clearly tagged as simulated in the UI.
    seed = sum(ord(c) for c in (company.registration_number or company.legal_name))
    margin = round(5.0 + (seed % 20) + (seed % 7) / 10.0, 1)              # ~5-25%
    rd_ratio = round((seed % 15) + (seed % 3) / 10.0, 1)                  # ~0-15%
    interest_cov = round(1.0 + (seed % 12) + (seed % 7) / 10.0, 1)        # ~1-13x

    return {
        "signals": {
            "margin_compression": {"value": margin, "status": "present"},
            "rd_expense_ratio": {"value": rd_ratio, "status": "present"},
            "interest_coverage_ratio": {"value": interest_cov, "status": "present"},
        },
        "raw_payload": {
            "source": SOURCE_NAME, "registration_number": company.registration_number,
            "note": "Real Bundesanzeiger filing retrieval not yet built — simulated placeholder",
        },
        "confidence": 0.4,
    }


def pull_bundesanzeiger_filing(company, db_session: Session) -> dict:
    """Manual, gated pull — never auto-run across the full batch (Section 3 of the Brief)."""
    return run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=BUNDESANZEIGER_PAID_ENABLED,
        fetch_live=_fetch_live, simulate=_simulate,
        cost_per_call=SIGNAL_METADATA["rd_expense_ratio"]["cost_per_pull"],
    )
