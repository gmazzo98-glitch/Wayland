"""
Eurostat Sector Export Exposure Adapter (Phase 1 API). Real REST integration
combining two keyless Eurostat datasets: `ext_tec01` (international trade in
goods by enterprise characteristics — the sector's own EXPORT value, by NACE
Rev.2 and reporting country) and `sbs_ovw_act` (structural business
statistics — the sector's own net TURNOVER, indicator NETTUR_MEUR, by NACE
Rev.2 and country). sector_export_exposure's raw range (0.3-0.9, see
indicators.py) is export value / turnover for the company's own NACE
sector — a macro, sector-wide ratio, not a company-specific figure (the
indicator's own label is "Sector Export Pressure (Macro)").

Replaces the Destatis-based producer this indicator's catalog row still
names as source_system — Destatis is Germany-only and its GENESIS table was
never a clean NACE match to begin with (adapters/destatis.py,
[[vienna-api-integration-status]] memory), and this DB's whole cohort is
Italian. ISTAT's own Coeweb foreign-trade portal was checked first and ruled
out: it was retired 2025-09-30 in favour of a new "Foreign Trade Statistics"
database on the same SDMX web service — a real migration, but Eurostat's
ext_tec01/sbs_ovw_act cover the same ground, are EU-wide (so this also
serves the catalog's existing German rows), and this project already has a
working, tested Eurostat REST pattern (eurostat_sector_growth.py) worth
extending rather than standing up a fresh national integration.

Verified live 2026-09-22 against the real API, no key/registration needed:
Italy NACE C28 (this DB's whole cohort), 2024 — exports EUR79.7bn (ext_tec01,
stk_flow=EXP, partner=WORLD, sizeclas=TOTAL) / turnover EUR148.5bn
(sbs_ovw_act, indic_sbs=NETTUR_MEUR) = 0.54, squarely inside the catalog's
own 0.3-0.9 bound for a genuinely export-heavy machinery sector (2023 gave
0.53 — stable year over year). Spot-checked C25/C29/DE-C28 too; all resolved
sensible values with no guessed table codes.

Coverage: whatever NACE sections ext_tec01/sbs_ovw_act both publish for the
company's country — checked live per call, never assumed. Reuses
eurostat_sector_growth.py's own B/C/D/E section restriction (`_sector_code`)
for consistency, since that is the section range this project's other
Eurostat integration already validated; a section outside it, or a sector/
year Eurostat has no data for, falls back to simulated/not_yet_checked
exactly like the sibling adapter, never a guessed ratio.
"""

from typing import Optional, Tuple

import requests
from sqlalchemy.orm import Session

from adapters.base import run_adapter
from adapters.eurostat_sector_growth import COUNTRY_GEO, _sector_code

SOURCE_NAME = "Eurostat Export Exposure"
PHASE = 1
TEC_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/ext_tec01"
SBS_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/sbs_ovw_act"


def _latest_value(payload: dict) -> Optional[Tuple[str, float]]:
    """Eurostat's compact JSON flattens every dimension into one offset; when every
    dimension but `time` is pinned to a single code (as every call below does), that
    flat offset equals the time dimension's own category index directly — the same
    simplification eurostat_sector_growth.py's _fetch_index_series already relies on.
    Returns (period, value) for the most recent period that actually carries a value,
    or None if the whole series is empty (an unmatched geo/sector combination)."""
    values = payload.get("value") or {}
    if not values:
        return None
    time_index = payload["dimension"]["time"]["category"]["index"]
    for period, idx in sorted(time_index.items(), key=lambda kv: kv[1], reverse=True):
        v = values.get(str(idx))
        if v is not None:
            return period, v
    return None


def _fetch_export_value_meur(nace_r2: str, geo: str) -> Tuple[str, float]:
    """(period, export value in million EUR) for the most recent year available —
    world exports, all enterprise sizes, so it matches the sector total in `sbs_ovw_act`."""
    resp = requests.get(TEC_URL, params={
        "format": "JSON", "lang": "EN", "geo": geo, "nace_r2": nace_r2,
        "stk_flow": "EXP", "sizeclas": "TOTAL", "partner": "WORLD", "unit": "THS_EUR",
    }, timeout=20)
    resp.raise_for_status()
    found = _latest_value(resp.json())
    if not found:
        raise RuntimeError(f"ext_tec01 returned no export value for nace_r2={nace_r2} geo={geo}")
    period, thousand_eur = found
    return period, thousand_eur / 1000.0


def _fetch_turnover_meur(nace_r2: str, geo: str, period: str) -> float:
    resp = requests.get(SBS_URL, params={
        "format": "JSON", "lang": "EN", "geo": geo, "nace_r2": nace_r2,
        "indic_sbs": "NETTUR_MEUR", "time": period,
    }, timeout=20)
    resp.raise_for_status()
    values = resp.json().get("value") or {}
    if not values:
        raise RuntimeError(f"sbs_ovw_act returned no turnover for nace_r2={nace_r2} geo={geo} time={period}")
    return next(iter(values.values()))


def _fetch_live(company) -> dict:
    nace_r2 = _sector_code(company.nace_code)
    if nace_r2 is None:
        raise RuntimeError(
            f"NACE code '{company.nace_code}' is outside the covered sections (B/C/D/E) "
            "this project's Eurostat integrations validate against"
        )
    geo = COUNTRY_GEO.get(company.country or "Germany", "DE")

    period, export_meur = _fetch_export_value_meur(nace_r2, geo)
    turnover_meur = _fetch_turnover_meur(nace_r2, geo, period)
    if not turnover_meur or turnover_meur <= 0:
        raise RuntimeError(f"sbs_ovw_act turnover is zero/missing for nace_r2={nace_r2} geo={geo} time={period}")
    ratio = export_meur / turnover_meur

    return {
        "signals": {
            "sector_export_exposure": {
                "value": ratio, "status": "present",
                "summary": f"NACE {nace_r2} in {geo}, {period}: sector exports EUR{export_meur:,.0f}m of "
                           f"EUR{turnover_meur:,.0f}m turnover ({ratio:.0%})",
                "evidence": {
                    "method": "Eurostat ext_tec01 export value (world, all enterprise sizes) over "
                              "sbs_ovw_act net turnover, same NACE 2-digit sector and reporting country",
                    "nace_r2": nace_r2, "geo": geo, "period": period,
                    "export_meur": round(export_meur, 1), "turnover_meur": round(turnover_meur, 1),
                    "source_urls": [TEC_URL, SBS_URL],
                },
            },
        },
        "raw_payload": {"nace_r2": nace_r2, "geo": geo, "period": period, "ratio": ratio,
                          "export_meur": export_meur, "turnover_meur": turnover_meur},
        "confidence": 0.75,
    }


def _simulate(company) -> dict:
    # Writes NO signal at all, unlike the older eurostat_sector_growth.py sibling (which explicitly
    # writes value=None/status=not_yet_checked here) — scoring.py never consults is_simulated, so an
    # explicit write can silently clobber a real prior value with a placeholder if this ever runs
    # again after a transient failure. The Phase 7 crawlers already moved to this "leave the row
    # untouched" convention for the same reason (scrapers/node_crawler_base.py's own docstring);
    # SourceHealth.mode still records simulated so Pipeline Health shows why nothing changed.
    return {"signals": {}, "raw_payload": {"note": "Eurostat export-exposure lookup unavailable"}, "confidence": 0.5}


def sync_sector_export_exposure(company, db_session: Session) -> dict:
    return run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=True,  # keyless public API, same as eurostat_sector_growth.py
        fetch_live=_fetch_live, simulate=_simulate, timeout=30,
    )
