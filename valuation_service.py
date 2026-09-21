"""
Database side of the valuation feature: gathers each company's imported financials and
builds the context valuation.py works from. The engine itself never touches the database.

Financials are read from the raw imported rows (RawImportRecord) — the '<field>_latest /
_y-1 / _y-2' columns of whatever dataset carried them (AIDA today). Only real imported
columns are used; simulated signals are never read.
"""

from typing import Dict, List, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Company, RawImportRecord
import valuation_models
from valuation_data import (
    load_assumptions, load_reference, load_sector_map, seed_valuation_defaults,
)
from valuation import (
    Financials, ValuationContext, best_financials, extract_financials,
    peers_from_universe, value_company,
)


def _company_meta(c: Company) -> dict:
    return {"id": c.id, "legal_name": c.legal_name, "country": c.country, "nace_code": c.nace_code,
            "headcount": c.headcount, "registration_number": c.registration_number,
            "need_score": c.need_score, "readiness_score": c.readiness_score,
            "shortlist_status": c.shortlist_status}


def load_company_meta(db: Session) -> List[dict]:
    return [_company_meta(c) for c in db.query(Company).order_by(Company.legal_name).all()]


def financial_dataset_names(db: Session, unit_of) -> List[str]:
    """Datasets whose rows carry revenue columns — probed with one row each, so a
    crawler dataset's blobs are never pulled just to find out they have no financials."""
    names = [n for (n,) in db.query(RawImportRecord.dataset_name).distinct().all()]
    out = []
    for n in names:
        row = db.query(RawImportRecord.raw_row).filter(RawImportRecord.dataset_name == n).first()
        if row and extract_financials(row[0], n, unit_of(n)) is not None:
            out.append(n)
    return out


def load_financials(db: Session, assumptions, company_ids=None) -> Dict[str, Financials]:
    """{company_id: Financials} — the best financial dataset per company."""
    unit_of = lambda ds: assumptions.num("financials_unit_eur", ds)      # noqa: E731
    datasets = financial_dataset_names(db, unit_of)
    if not datasets:
        return {}
    q = db.query(RawImportRecord.company_id, RawImportRecord.dataset_name, RawImportRecord.raw_row) \
          .filter(RawImportRecord.dataset_name.in_(datasets))
    if company_ids is not None:
        q = q.filter(RawImportRecord.company_id.in_(list(company_ids)))
    candidates: Dict[str, List[Financials]] = {}
    for cid, ds, raw in q.all():
        fin = extract_financials(raw, ds, unit_of(ds))
        if fin is not None:
            candidates.setdefault(cid, []).append(fin)
    return {cid: best_financials(fs) for cid, fs in candidates.items()}


def build_context(db: Session, companies: List[dict], fins: Dict[str, Financials]) -> ValuationContext:
    """Assumptions + reference data + sector map, with peer medians computed from the
    financials of every company passed in."""
    ctx = ValuationContext(assumptions=load_assumptions(db), reference=load_reference(db),
                           sector_map=load_sector_map(db))
    ctx.peers = peers_from_universe(companies, fins, ctx)
    return ctx


def load_universe(db: Session) -> Tuple[List[dict], Dict[str, Financials], ValuationContext]:
    """Everything needed to value any company: seeds defaults if this is the first use."""
    seed_valuation_defaults(db)
    companies = load_company_meta(db)
    assumptions = load_assumptions(db)
    fins = load_financials(db, assumptions)
    return companies, fins, build_context(db, companies, fins)


def universe_fingerprint(db: Session) -> str:
    """Changes whenever anything a valuation depends on changes — the cache key for the UI.
    A plain string, since Streamlit cannot hash SQLAlchemy Row objects."""
    def stamp(model, col):
        return tuple(db.query(func.count(), func.max(col)).select_from(model).one())
    return repr((
        stamp(RawImportRecord, RawImportRecord.updated_at),
        stamp(valuation_models.ValuationAssumption, valuation_models.ValuationAssumption.updated_at),
        stamp(valuation_models.ValuationSectorMap, valuation_models.ValuationSectorMap.updated_at),
        stamp(valuation_models.ValuationReference, valuation_models.ValuationReference.fetched_at),
        stamp(Company, Company.headcount),
    ))


def value_one(db: Session, company_id: str, country_override=None):
    companies, fins, ctx = load_universe(db)
    meta = next((c for c in companies if c["id"] == company_id), None)
    return value_company(meta, fins.get(company_id), ctx, country_override) if meta else None
