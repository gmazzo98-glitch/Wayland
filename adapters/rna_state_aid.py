"""
Registro Nazionale degli Aiuti di Stato (RNA) Adapter (Phase 1 API, Italy only).

Real integration against RNA's public Open Data feed: one static XML file per calendar
month — https://www.rna.gov.it/sites/rna.mise.gov.it/files/opendata/OpenData_Aiuti_<year>_
<month>.xml — confirmed live 2026-10-08 (no API key, no registration, no cookie consent
needed for this specific static path, despite the site's own search UI being cookie-gated
behind a consent wall; the download URL was found via that UI's own AJAX call, then
verified to work directly with a plain GET). Each file lists every individual state-aid
grant registered that month, nationwide, with the beneficiary's Codice Fiscale/Partita IVA.
RNA registration is mandatory for any Italian company receiving state aid (L. 234/2012), so
a clean miss across the lookback window is a genuine checked absence, not a search-coverage
gap — same reasoning directory-listing-crawler uses for an exact-name non-match.

Feeds public_grant_count for Italy specifically — a materially more precise source than
adapters/eu_funding.py's broad EU-wide text search (an actual registered grant with a
date/amount/measure name, not a text match against a press release or call announcement).
Registered AFTER "EU Funding Portal" in company_service.py's Phase 1 step list for Italian
companies specifically so this one's value is what SignalRecord ends up keeping for them —
see that module's own comment on why, and eu_funding.py's docstring for the same note from
the other side.

One thing this was scoped to detect but genuinely can't: the "Transizione 4.0/5.0"
digitalization tax credit. Checked live against a real month's file (October 2026, 5,420
records, 61 distinct measure titles) — it is not there by name or anything resembling it.
That credit is self-assessed via tax return (F24 offset) against a beneficiary list MIMIT
hands to Agenzia delle Entrate internally, not a "concessione" that goes through RNA's
normal registration flow, so there is no public per-company registry of recipients. Not
guessed around — just not buildable from this source, so energy_transition_capex is
deliberately NOT fed from here (see scrapers/company_website_crawler.py's sustainability-PDF
read for the real producer of that one).
"""

import os
import re
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from sqlalchemy.orm import Session

from adapters.base import run_adapter
from utils import normalize_registration_nr

SOURCE_NAME = "RNA Aiuti di Stato"
PHASE = 1

BASE_URL = "https://www.rna.gov.it/sites/rna.mise.gov.it/files/opendata/OpenData_Aiuti_{year}_{month:02d}.xml"
# public_grant_count's own freshness_days=365 (indicators.py) is about recent funding
# specifically ("recently receiving public... funding" — its own rationale), not a cache
# lifetime; 13 months gets a full calendar year's coverage plus a little slack. Each month is
# ~15MB nationwide, so this is also a real download-volume judgment call, not just a
# freshness one — happy to widen it if a shorter window turns out to miss real signal.
LOOKBACK_MONTHS = 13
CACHE_DIR = Path(os.getenv("RNA_CACHE_DIR") or Path(tempfile.gettempdir()) / "vienna_rna_aiuti_cache")
# RNA's own open-data page states a weekly update cadence; only the CURRENT month's file is
# ever re-downloaded, and only once it's older than this — every prior month is immutable
# history once published, so it's cached indefinitely.
CURRENT_MONTH_MAX_AGE = timedelta(days=7)
REQUEST_TIMEOUT = 60
_RECORD_FIELDS = ("CODICE_FISCALE_BENEFICIARIO", "DENOMINAZIONE_BENEFICIARIO", "TITOLO_MISURA",
                  "DATA_CONCESSIONE", "COD_CE_MISURA", "SOGGETTO_CONCEDENTE", "DES_TIPO_MISURA")
_PIVA_RE = re.compile(r"^\d{11}$")

_index_cache: Optional[Dict[str, List[Dict]]] = None
_index_built_at: Optional[datetime] = None
_INDEX_TTL = timedelta(hours=24)


def _month_sequence(months_back: int, now: Optional[datetime] = None) -> List[Tuple[int, int]]:
    """The current month and the `months_back - 1` before it, newest first."""
    now = now or datetime.utcnow()
    out = []
    y, m = now.year, now.month
    for _ in range(months_back):
        out.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def _cache_path(year: int, month: int) -> Path:
    return CACHE_DIR / f"OpenData_Aiuti_{year}_{month:02d}.xml"


def download_month(year: int, month: int) -> Optional[Path]:
    """Returns the local path to one month's XML, downloading/refreshing it first if
    needed. None if the file has never been downloaded and the live fetch also failed (a
    future month that doesn't exist yet, or a transient network error)."""
    path = _cache_path(year, month)
    now = datetime.utcnow()
    is_current_month = (year, month) == (now.year, now.month)
    if path.exists():
        age = now - datetime.utcfromtimestamp(path.stat().st_mtime)
        if not is_current_month or age < CURRENT_MONTH_MAX_AGE:
            return path
    try:
        resp = requests.get(BASE_URL.format(year=year, month=month), timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return path if path.exists() else None
    if resp.status_code != 200:
        return path if path.exists() else None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)
    return path


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _extract_record(aiuto_elem) -> Dict:
    record: Dict = {}
    total_amount, has_amount = 0.0, False
    for child in aiuto_elem.iter():
        tag = _strip_ns(child.tag)
        if tag in _RECORD_FIELDS and child.text:
            record[tag] = child.text.strip()
        elif tag == "IMPORTO_NOMINALE" and child.text:
            try:
                total_amount += float(child.text)
                has_amount = True
            except ValueError:
                pass
    if has_amount:
        record["TOTAL_IMPORTO_NOMINALE"] = round(total_amount, 2)
    return record


def parse_month(path: Path) -> List[Dict]:
    """Streams one month's file rather than loading the whole ~15MB DOM at once —
    clearing each <AIUTO> after reading it bounds memory to roughly one record at a time."""
    records = []
    for _, elem in ET.iterparse(str(path), events=("end",)):
        if _strip_ns(elem.tag) == "AIUTO":
            record = _extract_record(elem)
            if record.get("CODICE_FISCALE_BENEFICIARIO"):
                records.append(record)
            elem.clear()
    return records


def _build_index(months_back: int = LOOKBACK_MONTHS) -> Dict[str, List[Dict]]:
    index: Dict[str, List[Dict]] = {}
    for year, month in _month_sequence(months_back):
        path = download_month(year, month)
        if not path:
            continue
        try:
            for record in parse_month(path):
                index.setdefault(record["CODICE_FISCALE_BENEFICIARIO"], []).append(record)
        except ET.ParseError:
            continue  # a torn/partial download for one month must not lose every other month
    return index


def get_index(force_refresh: bool = False) -> Dict[str, List[Dict]]:
    """Builds once per process (or once per _INDEX_TTL) and reuses across every company in
    the same run — rebuilding per call would mean re-downloading ~200MB per company."""
    global _index_cache, _index_built_at
    now = datetime.utcnow()
    if force_refresh or _index_cache is None or (_index_built_at and now - _index_built_at > _INDEX_TTL):
        _index_cache = _build_index()
        _index_built_at = now
    return _index_cache


def reset_cache_for_tests() -> None:
    global _index_cache, _index_built_at
    _index_cache, _index_built_at = None, None


def _fetch_live(company) -> dict:
    code = _to_piva(company)
    records = get_index().get(code, [])
    count = len(records)
    grants = [{"measure": r.get("TITOLO_MISURA"), "granted_on": r.get("DATA_CONCESSIONE"),
               "granting_body": r.get("SOGGETTO_CONCEDENTE"), "amount_eur": r.get("TOTAL_IMPORTO_NOMINALE"),
               "state_aid_case": r.get("COD_CE_MISURA")} for r in records]
    named = "; ".join(g["measure"] for g in grants[:3] if g["measure"])
    return {
        "signals": {"public_grant_count": {
            "value": float(count), "status": "present" if count > 0 else "absent",
            "summary": f"{count} registered state-aid grant(s) in RNA over the last {LOOKBACK_MONTHS} months"
                       + (f": {named}" if named else ""),
            "evidence": {"method": f"Registro Nazionale Aiuti di Stato (RNA) open data, last {LOOKBACK_MONTHS} months",
                         "source_urls": ["https://www.rna.gov.it/open-data/aiuti"],
                         "codice_fiscale_matched": code, "grants": grants},
        }},
        "raw_payload": {"codice_fiscale": code, "grants_found": count, "lookback_months": LOOKBACK_MONTHS},
        # A match (or clean miss) against a mandatory government registry, not a text search —
        # higher than eu_funding.py's 0.7 broad-keyword-match confidence.
        "confidence": 0.85,
    }


def _simulate(company) -> dict:
    return {"signals": {}, "raw_payload": {"note": "RNA state aid registry unavailable"}, "confidence": 0.5}


def _to_piva(company) -> Optional[str]:
    """normalize_registration_nr always returns an Italian Partita IVA as "IT" + 11 digits
    (its own canonical form) — RNA's CODICE_FISCALE_BENEFICIARIO field is the bare 11 digits,
    no "IT" (confirmed against the live October 2026 file). None for anything else (a REA
    registration, a 16-char Codice Fiscale, or no usable number at all)."""
    if (company.country or "Germany") != "Italy":
        return None
    normalized = normalize_registration_nr(company.registration_number, country="Italy")
    bare = normalized[2:] if normalized.startswith("IT") else normalized
    return bare if _PIVA_RE.match(bare) else None


def has_matchable_piva(company) -> bool:
    """True only when this company could actually be looked up in RNA. company_service.py
    uses this to decide EU Funding Portal vs. RNA for Phase 1's public_grant_count slot —
    see its own comment on why only one of them should write it."""
    return _to_piva(company) is not None


def sync_italian_state_aid(company, db_session: Session) -> dict:
    if not has_matchable_piva(company):
        return {"status": "skipped",
                "reason": "RNA only covers Italian companies with an 11-digit Partita IVA on record"}
    return run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=True, fetch_live=_fetch_live, simulate=_simulate,
        # The first call in a process builds the index (up to ~13 downloads + streaming
        # parses) — every call after that reuses it and returns almost immediately.
        timeout=180,
    )
