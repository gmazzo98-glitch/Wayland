"""
Eurostat Short-Term Business Statistics Adapter — Sector Growth Benchmark
(Phase 1 API). Real REST integration against Eurostat's `sts_inpr_m`
dataset ("Production in industry - monthly data"), matched by the company's
NACE Rev.2 section+2-digit code. Public, keyless dissemination API — no
registration gate, unlike Destatis (see adapters/destatis.py).

This exists to answer a question the Revenue Trend indicator's own comment
already raises (indicators.py): "Check whether stagnation is company-specific
or an entire-sector pattern before treating it as an internal NEED signal."
sector_growth_benchmark is that sector-level check; revenue_growth_vs_sector
(computed in company_service.py from this plus revenue_trend) is the actual
company-vs-market differential.

Coverage: NACE sections B, C, D, E only (mining, manufacturing, energy,
water/waste) — that's what sts_inpr_m covers. A company in agriculture,
trade, or services (A, F, G-U) has no benchmark here and falls back to
simulated/not_yet_checked rather than being matched against an unrelated
industrial index. Extending coverage to services (Eurostat's sts_setu_m,
whose NACE grouping isn't a clean 2-digit split) is a real follow-up
decision, not guessed at here — same posture as destatis.py's open table-code
question.
"""

import re
import requests
from sqlalchemy.orm import Session
from adapters.base import run_adapter
from config import has_credentials

SOURCE_NAME = "Eurostat Sector Growth"
PHASE = 1
BASE_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/sts_inpr_m"
DATASET = "sts_inpr_m"
COVERED_SECTIONS = {"B", "C", "D", "E"}

# Eurostat's own geo code for each country this tool supports (NACE Rev.2 is
# the EU-wide standard; Italy's ATECO is a direct national extension of it,
# so the same nace_code parsing applies to both countries).
COUNTRY_GEO = {"Germany": "DE", "Italy": "IT"}


def _sector_code(nace_code: str) -> str:
    """'C10.51' -> 'C10'. Returns None outside sts_inpr_m's covered sections."""
    if not nace_code:
        return None
    code = str(nace_code).strip()
    m = re.match(r"([A-Za-z])(\d{2})", code)
    if m:
        if m.group(1).upper() not in COVERED_SECTIONS:
            return None
        return f"{m.group(1).upper()}{m.group(2)}"
    # Bare-digit NACE/ATECO codes (e.g. AIDA's '284900'): derive the section
    # letter from the 2-digit division.
    m = re.match(r"(\d{2})", code)
    if not m:
        return None
    division = int(m.group(1))
    if 5 <= division <= 9:
        section = "B"
    elif 10 <= division <= 33:
        section = "C"
    elif division == 35:
        section = "D"
    elif 36 <= division <= 39:
        section = "E"
    else:
        return None
    return f"{section}{m.group(1)}"


def _fetch_index_series(nace_r2: str, geo: str) -> list:
    """Returns [(period, value), ...] sorted ascending — seasonally/calendar
    adjusted production volume index, 2021=100."""
    resp = requests.get(
        BASE_URL,
        params={
            "format": "JSON", "lang": "EN", "geo": geo,
            "s_adj": "SCA", "unit": "I21", "indic_bt": "PRD",
            "nace_r2": nace_r2,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    values = data.get("value") or {}
    if not values:
        raise RuntimeError(f"Eurostat {DATASET} returned no data for nace_r2={nace_r2} geo={geo}")

    time_index = data["dimension"]["time"]["category"]["index"]  # {"2024-01": idx, ...}
    ordered = sorted(time_index.items(), key=lambda kv: kv[1])
    return [(period, values[str(idx)]) for period, idx in ordered if str(idx) in values]


def _trailing_yoy_growth(series: list) -> float:
    """
    Trailing-12-month average vs. the prior 12-month average, as a fraction —
    smooths month-to-month index noise the same way revenue_trend's 3-fiscal-
    year window smooths a single company's figures, so the two sides of the
    eventual comparison are on comparable footing.
    """
    if len(series) < 24:
        raise RuntimeError(f"Need at least 24 months of {DATASET} data, got {len(series)}")
    values = [v for _, v in series]
    latest_avg = sum(values[-12:]) / 12
    prior_avg = sum(values[-24:-12]) / 12
    if prior_avg == 0:
        raise RuntimeError(f"Prior 12-month average is zero for {DATASET} — cannot compute growth")
    return (latest_avg - prior_avg) / prior_avg


def _fetch_live(company) -> dict:
    nace_r2 = _sector_code(company.nace_code)
    if nace_r2 is None:
        raise RuntimeError(
            f"NACE code '{company.nace_code}' is outside {DATASET}'s covered sections (B/C/D/E)"
        )
    geo = COUNTRY_GEO.get(company.country or "Germany", "DE")

    series = _fetch_index_series(nace_r2, geo)
    growth = _trailing_yoy_growth(series)
    growth_pct = round(growth * 100.0, 2)

    return {
        "signals": {"sector_growth_benchmark": {
            "value": growth_pct,
            "status": "present",
            "summary": (
                f"Eurostat {DATASET} production index for {nace_r2} ({geo}): "
                f"trailing 12-month avg vs. prior 12-month avg = {growth_pct:+.1f}%"
            ),
            "evidence": {
                "dataset": DATASET, "nace_r2": nace_r2, "geo": geo,
                "source_url": f"https://ec.europa.eu/eurostat/databrowser/product/view/{DATASET}",
            },
        }},
        "raw_payload": {
            "source": SOURCE_NAME, "dataset": DATASET, "nace_r2": nace_r2, "geo": geo,
            "series_tail": series[-24:],
        },
        "confidence": 0.85,
    }


def _simulate(company) -> dict:
    return {
        "signals": {"sector_growth_benchmark": {"value": None, "status": "not_yet_checked"}},
        "raw_payload": {
            "source": SOURCE_NAME,
            "note": f"NACE code '{company.nace_code}' outside {DATASET} coverage (sections B/C/D/E), "
                    f"or Eurostat was unreachable.",
        },
        "confidence": 0.0,
    }


def sync_sector_growth_benchmark(company, db_session: Session) -> dict:
    return run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=has_credentials(SOURCE_NAME),
        fetch_live=_fetch_live, simulate=_simulate,
    )
