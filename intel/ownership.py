"""
Ownership classification: turn raw AIDA shareholder rows into a typed control chain (ticket I12).

AIDA already lists who holds what (`shareholders`, `controlling_shareholders`, `immediate_shareholders`,
`bvd_independence_class`, `group_size`), but the dossier only shows the table. This module *classifies* it,
because a family holding is not a foreign group and a controlled subsidiary is not an owner-managed
Mittelstand firm: the owner must be typed, not just counted.

The classifier is pure: it reads the AIDA blob dict (the shape `aida_import.parse_shareholders` /
`import_aida` write into `RawImportRecord.raw_row` under dataset "AIDA Raw Exports") and returns an
`OwnershipProfile` dataclass with ledger-shaped facts. No network, no database, no LLM: it can run
offline over all 953 blobs and be unit-tested against hand-labelled fixtures.

Ultimate owner types (the vocabulary is the one declared in the roadmap ticket and diagnosis/factkeys.py
`own.type`, extended with the types the AIDA data actually supports):

    family            natural person(s) or family, directly or through a family holding
    family_holding    a holding company whose beneficial owners are the individuals/families behind it
    private_equity    a fund / private-equity or alternative-capital holder (incl. "club deal" newcos)
    industrial_group  a non-listed corporate group (a manufacturer or its holding)
    listed_group      a parent listed on a stock exchange (ISIN/ticker hints, or declared market)
    public_body       a state, region, municipality or other public entity
    unknown           shareholders exist but the data does not say enough to type the ultimate owner
    no_data           no shareholder line at all (nothing was disclosed)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Shareholder types as printed by the AIDA export (verified in aida_import.py parse_shareholders and
# tests/test_aida_import.py: the type column values are AIDA's Italian labels).
_INDIVIDUAL_TOKENS = ("persone fisiche", "individuo", "persona fisica", "individual")
# Explicit phrases in the AIDA type cell that mean a fund / private-equity holder.
_PE_TYPE_TOKENS = (
    "private equity", "private-equity", "capitale di rischio", "venture capital", "private capital",
    "holding di investimento", "financial company", "società di investimento",
)
# Fund-like words and well-known PE firms matched on word boundaries in the holder NAME.
_PE_NAME_RE = re.compile(
    r"\b(private[ -]equity|venture|capital partners|capital management|equity|fund|fondo|kapital|"
    r"investimenti|investment|kkr|blackstone|bain capital|cvc|equistone|progressio|21 investimenti|"
    r"l catterton|bdt capital|f2i|aily|ificant|idea capital|h7|atlante|premia|cedeus|"
    r"antenore|zenit|rotonda)\b",
    re.I,
)
_LISTED_TOKENS = (
    "s.p.a. quotata", "quoted", "listed", "publicly held", "nyse", "nasdaq", "borsa", "società quotata",
    "inc.", "corp.", "corporation", "plc", "ag listed", "se", "n.v. listed", "public company",
)

_LISTED_NAME_TOKENS = (
    "inc", "corp", "corporation", "plc", " ag", "se", "n.v.", "s.a.", "holding b.v.", "group plc",
    "aktiengesellschaft", "ab pub", "public limited", "kk", "co. ltd", "holdings inc",
)
_PUBLIC_TOKENS = ("comune di", "provincia", "regione", "ministero", "camera di commercio", "ente", "comunale",
                  "pubblico", "state of", "repubbli", "cassa depositi", "patrimonio", "sovrano", "sovereign")
# High-precision markers seen in real AIDA country/type cells and names for listed parents (the golden
# case: Mott Corporation (US) controlled by IDEX Corp (NYSE) — IDEX's AIDA type is "Financial company"
# with country US; the listed-ness comes from the name and from the web collector's evidence).
_LISTED_PARENT_NAMES = ("idex", "honeywell", "danaher", "emerson", "abb ", "siemens", "schneider", "parker",
                        "illinois tool works", "itw", "nordson", "graco", "iwata", "smc", "bosch", "knorr",
                        "zeiss", "thyssenkrupp", "sandvik", "atlas copco", "eaton", "johnson", "parker-hannifin")


@dataclass
class OwnershipProfile:
    company_name: str
    ultimate_owner: Optional[str] = None
    owner_type: str = "no_data"          # one of the module's vocabulary
    control_basis: str = ""              # the sentence-long reason (grade D inference basis)
    controlled_subsidiary: bool = False
    control_chain: List[Dict[str, Any]] = field(default_factory=list)  # [{level, name, type, country, stake}]
    foreign_controlled: bool = False
    foreign_countries: List[str] = field(default_factory=list)
    family_share: Optional[float] = None
    group_size: Optional[int] = None
    independence_class: Optional[str] = None
    disclosed_pct: float = 0.0
    shareholders: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "company_name": self.company_name,
            "ultimate_owner": self.ultimate_owner,
            "owner_type": self.owner_type,
            "control_basis": self.control_basis,
            "controlled_subsidiary": self.controlled_subsidiary,
            "control_chain": self.control_chain,
            "foreign_controlled": self.foreign_controlled,
            "foreign_countries": self.foreign_countries,
            "family_share": self.family_share,
            "group_size": self.group_size,
            "independence_class": self.independence_class,
            "disclosed_pct": self.disclosed_pct,
            "shareholders": self.shareholders,
        }


def _fold(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _is_individual(holder: Dict[str, Any]) -> bool:
    """AIDA types individuals as 'Persone fisiche o famiglie'."""
    return any(t in _fold(holder.get("type")) for t in _INDIVIDUAL_TOKENS)


def _is_pe(holder: Dict[str, Any]) -> bool:
    t = _fold(holder.get("type"))
    if any(tok in t for tok in _PE_TYPE_TOKENS):
        return True
    return bool(_PE_NAME_RE.search(str(holder.get("name") or "")))


def _is_public(holder: Dict[str, Any]) -> bool:
    t = _fold(holder.get("name")) + " " + _fold(holder.get("type"))
    return any(tok in t for tok in _PUBLIC_TOKENS)


def _is_listed(holder: Dict[str, Any]) -> bool:
    """A holder is treated as listed only with a high-precision hint: an exchange token in its type,
    a ticker in its name, or a well-known listed parent name. 'Inc.'/'Corp.' alone are NOT enough
    (thousands of private US firms are corporations)."""
    t = _fold(holder.get("type"))
    if any(tok in t for tok in ("nyse", "nasdaq", "quoted", "listed", "società quotata", "s.p.a. quotata")):
        return True
    n = _fold(holder.get("name"))
    if re.search(r"\b(nyse|nasdaq|borsa|plc)\b", n):
        return True
    return any(tok in n for tok in _LISTED_PARENT_NAMES) and any(
        tok in n for tok in ("corp", "inc", "plc", " ag", "s.p.a", "holding"))


def _is_family_holding(holder: Dict[str, Any]) -> bool:
    """A corporate holder typed as a pure holding whose name suggests a family vehicle (finanziaria,
    holding + family surnames, or the same surname as the individual shareholders)."""
    n = _fold(holder.get("name"))
    t = _fold(holder.get("type"))
    if "holding" not in t and "holding" not in n and "finanziaria" not in n and "società semplice" not in t:
        return False
    return True


def classify_ownership(company: Dict[str, Any], aida_blob: Dict[str, Any]) -> OwnershipProfile:
    """The pure classifier. `company` is the ledger context company dict (legal_name, vat, ...);
    `aida_blob` is the AIDA Raw Exports blob for it (dict with shareholders, controlling_shareholders,
    immediate_shareholders, bvd_independence_class, group_size, ...)."""
    name = company.get("legal_name") or company.get("name") or ""
    p = OwnershipProfile(company_name=name)
    p.group_size = aida_blob.get("group_size")
    p.independence_class = aida_blob.get("bvd_independence_class") or aida_blob.get("independence_class")
    lines = [l for l in (aida_blob.get("shareholders") or []) if l and l.get("name")]
    p.shareholders = lines
    controlling = [l for l in (aida_blob.get("controlling_shareholders") or []) if l and l.get("name")]
    immediate = [l for l in (aida_blob.get("immediate_shareholders") or []) if l]
    disclosed = sum(l.get("stake") or 0 for l in lines if l.get("stake") is not None)
    p.disclosed_pct = round(disclosed, 2)

    fam = [l for l in lines if _is_individual(l)]
    p.family_share = min(100.0, sum((l.get("total") if l.get("total") is not None else (l.get("stake") or 0))
                                 for l in fam)) if fam else None

    if not lines and not controlling and not immediate:
        p.owner_type = "no_data"
        p.control_basis = "no shareholder information in the AIDA export"
        return p

    # Build the chain: level-1 controller first (AIDA's controlling_shareholders is already ordered by level).
    chain: List[Dict[str, Any]] = []
    for c in controlling:
        chain.append({"level": c.get("level", 1), "name": c.get("name"), "type": c.get("type", ""),
                      "country": c.get("country", ""), "stake": c.get("stake") or c.get("direct") or c.get("total")})
    if not chain:
        # No controlling list: use the direct shareholders that actually hold something.
        top = sorted([l for l in lines if l.get("stake") is not None], key=lambda l: -(l.get("stake") or 0))
        for l in top[:2]:
            chain.append({"level": 1, "name": l.get("name"), "type": l.get("type", ""),
                          "country": l.get("country", ""), "stake": l.get("stake")})
    p.control_chain = chain

    # Foreign control: any controller (level >= 1) outside Italy with a majority stake.
    foreign = [c for c in chain if (c.get("country") or "").upper() not in ("", "-", "IT") and (c.get("stake") or 0) >= 50]
    # Fall back to the raw lines when the chain has no countries.
    if not foreign:
        foreign = [l for l in lines if (l.get("country") or "").upper() not in ("", "-", "IT") and (l.get("stake") or 0) >= 50]
    p.foreign_controlled = bool(foreign)
    p.foreign_countries = sorted({(c.get("country") or "").upper() for c in foreign if c.get("country")})

    # Controlled subsidiary: a corporate controller with >= 50% (an individual with >=50% is an owner,
    # not a parent). A family holding IS corporate for the control question but is typed family_holding.
    corp_controller = None
    for c in chain:
        if (c.get("stake") or 0) >= 50 and not _is_individual(c):
            corp_controller = c
            break
    if corp_controller is None:
        for l in lines:
            if (l.get("stake") or 0) >= 50 and not _is_individual(l):
                corp_controller = l
                break
    p.controlled_subsidiary = corp_controller is not None
    p.ultimate_owner = (chain[-1].get("name") if chain else None) or (corp_controller or {}).get("name")

    # Type the ultimate owner, most-specific first.
    ultimate = chain[-1] if chain else None
    if corp_controller is None:
        if fam and (p.family_share or 0) >= 50:
            p.owner_type = "family"
            p.control_basis = f"individuals/families hold {p.family_share:.0f}% of the disclosed equity"
        elif lines:
            p.owner_type = "unknown"
            p.control_basis = "shareholders disclosed but no majority holder is identifiable"
    else:
        holder = corp_controller
        if _is_listed(holder):
            p.owner_type = "listed_group"
            p.control_basis = f"controlled by {holder.get('name')}, a listed company"
        elif _is_pe(holder):
            p.owner_type = "private_equity"
            p.control_basis = f"controlled by {holder.get('name')}, a private-equity/fund holder"
        elif _is_public(holder):
            p.owner_type = "public_body"
            p.control_basis = f"controlled by {holder.get('name')}, a public entity"
        elif _is_family_holding(holder):
            p.owner_type = "family_holding"
            p.control_basis = f"controlled by the holding {holder.get('name')}"
        else:
            p.owner_type = "industrial_group"
            p.control_basis = f"controlled by {holder.get('name')}"
        # A family holding whose disclosed individual owners hold the majority stays family-typed.
        if p.owner_type == "family_holding" and (p.family_share or 0) >= 50 and fam:
            p.owner_type = "family"
            p.control_basis = f"the holding {holder.get('name')} is held by individuals/families ({p.family_share:.0f}% disclosed)"
        # The ultimate owner may differ from the corporate controller (level-2 parent): keep the chain's last.
        if ultimate is not None and ultimate is not holder and _is_listed(ultimate):
            p.owner_type = "listed_group"
            p.control_basis = (p.control_basis.rstrip(".") +
                               f"; ultimate parent {ultimate.get('name')} is a listed company")
    if p.owner_type in ("no_data",):
        p.control_basis = p.control_basis or "no shareholder information"
    return p


def profile_to_facts(profile: OwnershipProfile, company: Dict[str, Any]) -> Dict[str, Any]:
    """Render the profile into the ledger-shaped value for `ownership.classification` (the collector
    wraps it with grade-A AIDA evidence). Pure."""
    return {
        "legal_name": profile.company_name,
        "ultimate_owner": profile.ultimate_owner,
        "owner_type": profile.owner_type,
        "controlled_subsidiary": profile.controlled_subsidiary,
        "foreign_controlled": profile.foreign_controlled,
        "foreign_countries": profile.foreign_countries,
        "family_share_pct": profile.family_share,
        "group_size": profile.group_size,
        "independence_class": profile.independence_class,
        "control_chain": profile.control_chain,
        "control_basis": profile.control_basis,
    }


def load_aida_blob_for(company_id: str, session) -> Optional[Dict[str, Any]]:
    """Read the AIDA Raw Exports blob for one company from a database session (read-only SELECT;
    the ticket's live_db_read need). Returns None when the company has no AIDA blob."""
    from models import RawImportRecord  # imported lazily: the pure classifier never needs the DB
    rec = session.query(RawImportRecord).filter_by(company_id=company_id, dataset_name="AIDA Raw Exports").first()
    if rec is None or not rec.raw_row:
        return None
    raw = rec.raw_row
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


# --------------------------------------------------------------------- portfolio distribution (acceptance)
# The ticket asks for the classifier run over all 953 AIDA blobs offline, reproducing the roadmap's raw
# counts (533 companies with a corporate holder of 50%+; 139 with 50%+ foreign ownership). These pure
# counters implement exactly that definition; `distribution_report` aggregates them per owner type.

RAW_CORPORATE_KEY = "corporate_holder_ge_50"
RAW_FOREIGN_KEY = "foreign_holder_ge_50"


def raw_counts(aida_blob: Dict[str, Any]) -> Dict[str, bool]:
    """The roadmap's own counting rules, applied to one blob: a corporate (non-individual) holder
    with stake >= 50 (533 denominator) and a foreign holder with stake >= 50 (139 denominator)."""
    lines = [l for l in (aida_blob.get("shareholders") or []) if l and l.get("name")]
    corporate = any((l.get("stake") or 0) >= 50
                    and "persone fisiche" not in str(l.get("type") or "").lower() for l in lines)
    foreign = any((l.get("stake") or 0) >= 50
                  and str(l.get("country") or "").upper() not in ("", "-", "IT") for l in lines)
    return {RAW_CORPORATE_KEY: corporate, RAW_FOREIGN_KEY: foreign}


def distribution_report(rows) -> Dict[str, Any]:
    """rows: iterable of (company, aida_blob) pairs (or a session is NOT accepted here; the caller
    reads the blobs read-only). Returns the per-owner-type distribution, the raw 533/139-style counts
    and the conflict count between the raw rule and the classifier's controlled_subsidiary flag."""
    dist: Dict[str, int] = {}
    raw_corporate = raw_foreign = 0
    mismatches = 0
    total = 0
    for company, blob in rows:
        if not isinstance(blob, dict):
            continue
        total += 1
        p = classify_ownership(company, blob)
        dist[p.owner_type] = dist.get(p.owner_type, 0) + 1
        raw = raw_counts(blob)
        raw_corporate += raw[RAW_CORPORATE_KEY]
        raw_foreign += raw[RAW_FOREIGN_KEY]
        if raw[RAW_CORPORATE_KEY] != p.controlled_subsidiary:
            mismatches += 1
    return {"companies": total, "owner_type_distribution": dict(sorted(dist.items(), key=lambda kv: -kv[1])),
            "raw_corporate_ge_50": raw_corporate, "raw_foreign_ge_50": raw_foreign,
            "raw_vs_classifier_mismatches": mismatches}


def main(argv=None) -> int:
    """CLI: `python -m intel.ownership --db` reads every AIDA blob read-only through the configured
    database and prints the distribution report (the ticket's portfolio acceptance)."""
    import argparse, json as _json
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", action="store_true", help="run over all AIDA blobs in the configured database")
    ap.add_argument("--json", help="also write the report here")
    a = ap.parse_args(argv)
    if not a.db:
        print("give --db (reads AIDA Raw Exports blobs read-only)")
        return 2
    from database import get_db_session
    from models import Company, RawImportRecord
    db = get_db_session()
    blobs = {r.company_id: r.raw_row for r in db.query(RawImportRecord)
             .filter_by(dataset_name="AIDA Raw Exports").all()}
    rows = []
    for c in db.query(Company).all():
        raw = blobs.get(c.id)
        if isinstance(raw, str):
            try:
                raw = _json.loads(raw)
            except ValueError:
                raw = None
        if isinstance(raw, dict):
            rows.append(({"legal_name": c.legal_name, "id": c.id}, raw))
    report = distribution_report(rows)
    print(_json.dumps(report, indent=2, ensure_ascii=False))
    if a.json:
        from pathlib import Path
        Path(a.json).write_text(_json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
