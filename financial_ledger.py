"""One annual financial history per company, merged from every source that dated its figures.

Each source (AIDA exports and reports, balance sheet PDFs) contributes observations keyed by the fact and the
actual fiscal year, never by a relative column. The newest year anywhere is the anchor (Latest); Y-1, Y-2 ... are
counted down from it, so a new statement moves every relative label at once and an older upload can never move
the anchor back. Where two sources disagree on the same fact and year the balance sheet wins by default and the
operator can overrule each case. A fact a source does not report stays missing.
"""
import json
import re
from datetime import datetime

from balance_sheet_pdf import DATASET_PREFIX, MONETARY_BASES, key

LEDGER_DATASET = "Merged Annual Financials"
LEDGER_SOURCE = LEDGER_DATASET  # same name, so the signal audit finds the record that backs a signal
PROFILE_PREFIX = "balance-sheet::"
NEW_INDICATOR = "__new__"
CALCULATION_PERIODS = ("latest", "y-1", "y-2")
COST_BASES = {"personnel_costs", "materials", "production_costs"}
_FACT = re.compile(r"^(?P<base>[a-z_]+)_(?P<period>latest|y-(?P<n>\d+))$")


def statement_dataset(period_end):
    return DATASET_PREFIX + period_end


def period_name(anchor, year):
    offset = anchor - year
    return None if offset < 0 else ("latest" if offset == 0 else f"y-{offset}")


def _fiscal_labels(anchor, years):
    return {period_name(anchor, y): f"FY{y}" for y in sorted(years, reverse=True) if period_name(anchor, y)}


def _anchor_year(raw):
    year = raw.get("derived_last_accounts_year")
    if isinstance(year, (int, float)) and 1900 < year < 2200:
        return int(year)
    close = str(raw.get("accounts_close") or "")[:4]
    return int(close) if close.isdigit() else None


def profile_key(name):
    if not str(name or "").strip():
        raise ValueError("Mapping profile name is required.")
    return PROFILE_PREFIX + str(name).strip()


def targets(db):
    from company_service import CANONICAL_SERIES_LABELS, valid_targets_for_column
    out = {"": "— Ignore (kept in the statement) —"}
    for base in MONETARY_BASES:
        out[f"base:{base}"] = f"Financial fact: {CANONICAL_SERIES_LABELS[base]} (every year of the line)"
    out.update({k: v for k, v in valid_targets_for_column(db).items() if k.startswith("indicator:")})
    return out


# ------------------------------------------------------------------------------------------ source observations

def existing_observations(db, company_id):
    """Observations of every non-statement source: {(base, year): [obs]}."""
    from company_service import CANONICAL_SERIES_LABELS
    from models import RawImportRecord
    found = {}
    if not company_id:
        return found
    for record in db.query(RawImportRecord).filter_by(company_id=company_id).all():
        name = record.dataset_name
        raw = record.raw_row if isinstance(record.raw_row, dict) else {}
        if name == LEDGER_DATASET or name.startswith(DATASET_PREFIX):
            continue
        anchor = _anchor_year(raw)
        if anchor is None:
            continue
        for column, value in raw.items():
            match = _FACT.match(column)
            if not match or match["base"] not in CANONICAL_SERIES_LABELS or isinstance(value, bool) \
                    or not isinstance(value, (int, float)):
                continue
            year = anchor - (int(match["n"]) if match["n"] else 0)
            found.setdefault((match["base"], year), []).append(dict(
                dataset=name, kind="other", value=float(value), rounded=True, date=f"{year}-12-31",
                evidence=dict(column=column, label=column, source_file=record.source_filename, dataset=name, date=f"{year}-12-31",
                              unit="thousand EUR")))
    return found


def statement_observations(db, company_id):
    from models import RawImportRecord
    out = {}
    if not company_id:
        return out
    for record in db.query(RawImportRecord).filter_by(company_id=company_id).all():
        if record.dataset_name.startswith(DATASET_PREFIX) and isinstance(record.raw_row, dict):
            out[record.dataset_name] = record.raw_row
    return out


def build_record(statement, mapping, unit, new_labels=None):
    """Observations and direct values of one reviewed statement. Pure."""
    from company_service import _norm
    unit = unit or statement.get("unit")
    if unit not in {"EUR", "thousand EUR"}:
        raise ValueError("Choose whether the statement is in euro or in thousands of euro.")
    period_end = statement.get("period_end")
    if not period_end:
        raise ValueError("The statement date is missing.")
    observations, direct, proposals, warnings = [], [], {}, []
    for line in statement["lines"]:
        target = mapping.get(line["id"]) or None
        if not target:
            continue
        if target == NEW_INDICATOR:
            label = str((new_labels or {}).get(line["id"], "")).strip()
            if not label:
                raise ValueError(f"Enter a label for the new indicator connected to {line['label']}.")
            proposals[line["id"]] = label
            target = f"indicator:{_norm(label)[:100]}"
        for date, value in line["values"].items():
            if value is None:
                continue
            evidence = dict(file=statement["filename"], sha256=statement.get("sha256"), page=line["page"], line_id=line["id"],
                            item=line["item"], label=line["label"], date=date, raw_value=line["raw"].get(date), unit=unit,
                            statement=line["statement"], period_end=period_end)
            if target.startswith("base:"):
                base = target.split(":", 1)[1]
                if base not in MONETARY_BASES:
                    raise ValueError(f"Statement lines cannot feed {base}.")
                converted = value / 1000 if unit == "EUR" else value
                if base in COST_BASES and converted < 0:
                    converted, evidence["sign"] = -converted, "expense printed in brackets; stored as a positive cost"
                evidence["converted_from"] = "EUR" if unit == "EUR" else None
                observations.append(dict(base=base, year=int(date[:4]), date=date, value=converted, evidence=evidence))
            elif target.startswith("indicator:") and date == period_end:
                direct.append(dict(indicator=target.split(":", 1)[1], date=date, value=value, evidence={**evidence, "unit": unit}))
            elif not target.startswith("indicator:"):
                raise ValueError(f"Unknown mapping target for {line['label']}: {target}")
    seen = {}
    for item in direct:
        if item["indicator"] in seen and seen[item["indicator"]] != item["value"]:
            raise ValueError(f"Two statement lines with different values feed the indicator {item['indicator']}.")
        seen[item["indicator"]] = item["value"]
    for base in {o["base"] for o in observations}:
        by_year = {}
        for o in observations:
            if o["base"] == base:
                by_year.setdefault(o["year"], set()).add(round(o["value"], 6))
        for year, values in by_year.items():
            if len(values) > 1:
                warnings.append(f"Several lines of this statement feed {base} for {year} with different values; the sources are compared below.")
    return dict(dataset=statement_dataset(period_end), period_end=period_end, unit=unit, observations=observations,
                direct=direct, proposals=proposals, warnings=warnings)


# ------------------------------------------------------------------------------------------------------ merging

ROUNDING_TOLERANCE = 1.0  # thousand EUR: AIDA prints whole thousands (4.229 = 4,229 k) and can differ from the filed euro figure by up to one


def _agree(a, b):
    tolerance = ROUNDING_TOLERANCE if a.get("rounded") or b.get("rounded") else 0.0005
    return abs(a["value"] - b["value"]) <= tolerance + 1e-9


def merge(candidates, decisions=None):
    """{(base, year): [obs]} -> series, conflicts. Default order: newest balance sheet, then other sources."""
    decisions = decisions or {}
    series, conflicts = {}, []
    for (base, year), options in sorted(candidates.items()):
        ranked = sorted(options, key=lambda o: (0 if o["kind"] == "balance_sheet" else 1, -int(str(o.get("period_end") or "0").replace("-", ""))))
        chosen = ranked[0]
        disputed = [o for o in ranked if not _agree(chosen, o)]
        decision_key = f"{base}|{year}"
        if disputed:
            picked = decisions.get(decision_key)
            override = next((o for o in ranked if o["dataset"] == picked), None)
            chosen = override or chosen
            disputed = [o for o in ranked if not _agree(chosen, o)]
            if disputed:
                conflicts.append(dict(key=decision_key, base=base, year=year, chosen=chosen["dataset"], default=ranked[0]["dataset"],
                                      options=[dict(dataset=o["dataset"], kind=o["kind"], value=o["value"]) for o in ranked]))
        series.setdefault(base, {})[year] = dict(
            value=chosen["value"], dataset=chosen["dataset"], kind=chosen["kind"], evidence=chosen["evidence"],
            corroborated_by=[o["dataset"] for o in ranked if o is not chosen and _agree(chosen, o)])
    return series, conflicts


def _new_candidates(record):
    out = {}
    for o in record["observations"]:
        out.setdefault((o["base"], o["year"]), []).append(dict(
            dataset=record["dataset"], kind="balance_sheet", value=o["value"], rounded=False, date=o["date"],
            period_end=record["period_end"], evidence=o["evidence"]))
    return out


def _all_candidates(db, company_id, new_record=None):
    candidates = existing_observations(db, company_id)
    records = {name: raw for name, raw in statement_observations(db, company_id).items()}
    if new_record is not None:
        records[new_record["dataset"]] = new_record
    for name, record in records.items():
        for o in record.get("observations", []):
            candidates.setdefault((o["base"], o["year"]), []).append(dict(
                dataset=name, kind="balance_sheet", value=o["value"], rounded=False, date=o["date"],
                period_end=record.get("period_end"), evidence=o["evidence"]))
    return candidates, records


def _directs(records):
    best = {}
    for name, record in records.items():
        for item in record.get("direct", []):
            if item["indicator"] not in best or item["date"] > best[item["indicator"]]["date"]:
                best[item["indicator"]] = {**item, "dataset": name}
    return best


def compute(db, company_id, new_record=None, decisions=None):
    """Merged history, relative facts and the signals that follow from them. Read only."""
    from company_service import derive_signals_from_variables
    from models import RawImportRecord
    candidates, records = _all_candidates(db, company_id, new_record)
    saved = db.query(RawImportRecord).filter_by(company_id=company_id, dataset_name=LEDGER_DATASET).first() if company_id else None
    saved_raw = saved.raw_row if saved and isinstance(saved.raw_row, dict) else {}
    decisions = {**(saved_raw.get("_decisions") or {}), **(decisions or {})}
    series, conflicts = merge(candidates, decisions)
    years = {y for by_year in series.values() for y in by_year}
    previous = max((y for (_, y) in existing_observations(db, company_id)), default=None)
    anchor = max(years) if years else None
    facts, sources = {}, {}
    for base, by_year in series.items():
        for year, entry in by_year.items():
            period = period_name(anchor, year)
            facts[f"{base}_{period}"] = entry["value"]
            sources[f"{base}_{period}"] = {**entry["evidence"], "dataset": entry["dataset"], "date": entry["evidence"].get("date") or f"{year}-12-31"}
    window = lambda d: {k: v for k, v in d.items() if k.rsplit("_", 1)[-1] in CALCULATION_PERIODS}  # noqa: E731
    drafts = {}
    for name, (value, status, fields, extra) in derive_signals_from_variables(window(facts), window(sources)).items():
        drafts[name] = dict(value=value, status=status, basis=extra.get("basis", "Calculated from the merged annual facts"),
                            inputs={v: facts.get(v) for v in extra.get("canonical_variables", [])}, raw_fields=fields)
    for indicator, item in _directs(records).items():
        drafts[indicator] = dict(value=item["value"], status="present", basis="Direct value from a reviewed balance sheet line, in the printed unit",
                                 inputs={}, raw_fields=[item["evidence"]])
    return dict(anchor=anchor, previous_anchor=previous, series=series, conflicts=conflicts, facts=facts, sources=sources,
                drafts=drafts, decisions=decisions, labels=_fiscal_labels(anchor, years) if anchor else {},
                missing=sorted(b for b, by_year in series.items() if anchor not in by_year), records=records)


def _derived_keys():
    from aida_import import FINANCIAL_SIGNAL_INPUT_FIELDS
    from company_service import AUTO_DERIVED_TRENDS
    return set(AUTO_DERIVED_TRENDS.values()) | set(FINANCIAL_SIGNAL_INPUT_FIELDS)


def signal_changes(db, company_id, merged):
    """Rows comparing each affected signal with what is stored; `decide` is True when the operator must choose."""
    from aida_single_report import DATASET_NAME as REPORT_DATASET
    from indicators import fetch_indicator_defs
    from models import SignalRecord
    defs = fetch_indicator_defs(db)
    existing = {s.signal_key: s for s in db.query(SignalRecord).filter_by(company_id=company_id).all()} if company_id else {}
    rows = []
    for name, draft in merged["drafts"].items():
        if name not in defs:
            continue
        old = existing.get(name)
        had = old is not None and old.status == "present" and not old.is_simulated and old.numeric_value is not None
        new_value = draft["value"] if draft["status"] == "present" else None
        differs = had and (new_value is None or abs(old.numeric_value - new_value) > 1e-6 * max(1.0, abs(old.numeric_value)))
        if new_value is None and not had:
            continue
        rows.append(dict(signal=name, existing=old.numeric_value if had else None, existing_source=old.source if had else None,
                         new=new_value, decide=bool(differs and old.source != LEDGER_SOURCE), obsolete=False))
    for name, old in existing.items():
        if name in merged["drafts"] or old.status != "present" or old.source not in {LEDGER_SOURCE, REPORT_DATASET}:
            continue
        if name in _derived_keys() or old.source == LEDGER_SOURCE:
            rows.append(dict(signal=name, existing=old.numeric_value, existing_source=old.source, new=None,
                             decide=old.source != LEDGER_SOURCE, obsolete=True))
    return rows


# --------------------------------------------------------------------------------------------- plan and import

def plan_statement(db, statement, mapping, new_labels=None, unit=None, decisions=None):
    """Read-only preview of importing one statement."""
    from models import Company
    company_id = None
    registration = (statement.get("company") or {}).get("registration_number")
    if not registration:
        raise ValueError("The statement has no readable fiscal code, so the company cannot be matched.")
    company = db.query(Company).filter_by(registration_number=registration).first()
    if company is None and (statement["company"].get("legal_name") and
                            db.query(Company).filter_by(legal_name=statement["company"]["legal_name"]).first()):
        raise ValueError("A company with this name has a different registration number. Resolve its identity before importing.")
    if company is not None:
        company_id = company.id
    record = build_record(statement, mapping, unit, new_labels)
    merged = compute(db, company_id, record, decisions)
    return dict(company_id=company_id, action="Update existing company" if company else "Create company",
                record=record, merged=merged, changes=signal_changes(db, company_id, merged) if company_id else [],
                replaced_statement=bool(company_id and statement_dataset(record["period_end"]) in statement_observations(db, company_id)),
                warnings=record["warnings"] + (["Several statements feed this history; check the conflicts below."] if merged["conflicts"] else []))


def _write(db, company, merged, replace, now):
    """Signals and the merged-history record. Caller commits."""
    from indicators import fetch_indicator_defs
    from models import RawImportRecord, SignalRecord
    defs = fetch_indicator_defs(db)
    existing = {s.signal_key: s for s in db.query(SignalRecord).filter_by(company_id=company.id).all()}
    rows = {r["signal"]: r for r in signal_changes(db, company.id, merged)}
    written = cleared = 0
    for name, row in rows.items():
        if row["decide"] and not replace.get(name, True):
            continue
        old = existing.get(name)
        if row["obsolete"]:
            old.status, old.numeric_value, old.text_value, old.fetched_at = "not_yet_checked", None, None, now
            old.raw_payload_ref = json.dumps(dict(dataset=LEDGER_SOURCE, basis="No longer computable from the merged annual facts."))
            cleared += 1
            continue
        draft = merged["drafts"][name]
        sig = old or SignalRecord(company_id=company.id, signal_key=name)
        sig.source, sig.status, sig.numeric_value = LEDGER_SOURCE, draft["status"], draft["value"]
        sig.text_value, sig.confidence, sig.is_simulated, sig.fetched_at = None, 1.0, False, now
        sig.raw_payload_ref = json.dumps(dict(dataset=LEDGER_SOURCE, basis=draft["basis"], raw_fields=draft["raw_fields"],
                                              evidence=dict(inputs=draft["inputs"], anchor_year=merged["anchor"])), ensure_ascii=False, default=str)
        db.add(sig)
        written += 1
    record = db.query(RawImportRecord).filter_by(company_id=company.id, dataset_name=LEDGER_DATASET).first()
    if record is None:
        record = RawImportRecord(company_id=company.id, dataset_name=LEDGER_DATASET)
        db.add(record)
    record.source_filename, record.source_row_index, record.updated_at = "merged from " + ", ".join(
        sorted({e["dataset"] for by_year in merged["series"].values() for e in by_year.values()})), 1, now
    record.raw_row = {**merged["facts"], "derived_last_accounts_year": merged["anchor"], "accounts_close": f"{merged['anchor']}-12-31",
                      "_decisions": merged["decisions"], "_labels": merged["labels"], "_missing_at_anchor": merged["missing"],
                      "_series": {b: {str(y): {k: v for k, v in e.items() if k != "evidence"} for y, e in by_year.items()}
                                  for b, by_year in merged["series"].items()}}
    record.mapping_snapshot = {}
    company.need_score = company.readiness_score = company.last_scored_at = None
    return written, cleared


def import_statement(db, statement, mapping, new_labels=None, unit=None, decisions=None, signal_replace=None,
                     profile_name=None, replace_statement=False):
    """One transaction: company, statement record, merged history and signals."""
    from company_service import create_ad_hoc_indicator
    from models import Company, ColumnMappingProfile, RawImportRecord
    plan = plan_statement(db, statement, mapping, new_labels, unit, decisions)
    if plan["replaced_statement"] and not replace_statement:
        raise ValueError("This company already has a statement for that period end. Select replace to import it again.")
    now = datetime.utcnow()
    try:
        resolved = dict(mapping)
        for line_id, label in plan["record"]["proposals"].items():
            resolved[line_id] = f"indicator:{create_ad_hoc_indicator(db, label, dataset_name=plan['record']['dataset'], source_column=line_id, commit=False)}"
        company = db.get(Company, plan["company_id"]) if plan["company_id"] else Company(
            legal_name=statement["company"]["legal_name"], registration_number=statement["company"]["registration_number"],
            country="Italy", nace_code=None, sector_name=None, shortlist_status="candidate")
        if company.country != "Italy":
            raise ValueError("The matched company's country is not Italy. Resolve its identity before importing.")
        db.add(company)
        db.flush()
        record = plan["record"]
        row = db.query(RawImportRecord).filter_by(company_id=company.id, dataset_name=record["dataset"]).first()
        if row is None:
            row = RawImportRecord(company_id=company.id, dataset_name=record["dataset"])
            db.add(row)
        row.source_filename, row.source_row_index, row.updated_at = statement["filename"], 1, now
        saved_record = {k: v for k, v in record.items() if k != "proposals"}
        row.raw_row = {**saved_record, "_statement": statement, "_unit": record["unit"], "_mapping_profile": profile_name}
        row.mapping_snapshot = resolved
        db.flush()
        # The new record is now stored, so the merge below reads it like any other.
        merged = compute(db, company.id, None, decisions)
        written, cleared = _write(db, company, merged, signal_replace or {}, now)
        if profile_name:
            scoped = profile_key(profile_name)
            profile = db.query(ColumnMappingProfile).filter_by(dataset_name=scoped).first()
            if profile is None:
                profile = ColumnMappingProfile(dataset_name=scoped)
                db.add(profile)
            profile.country, profile.mapping_json = "Italy", json.dumps(resolved)
        db.commit()
        return dict(company_id=company.id, signals_written=written, signals_cleared=cleared, anchor=merged["anchor"],
                    conflicts=len(merged["conflicts"]))
    except Exception:
        db.rollback()
        raise


def rebuild_company(db, company, now=None):
    """Re-merge after another source changed (for example an older AIDA report uploaded after a newer statement).
    No-op when the company has no merged history. Caller commits."""
    from models import RawImportRecord
    if db.query(RawImportRecord.id).filter_by(company_id=company.id, dataset_name=LEDGER_DATASET).first() is None:
        return None
    merged = compute(db, company.id)
    return _write(db, company, merged, {}, now or datetime.utcnow())


def apply_decisions(db, company, decisions):
    """Operator's new choices on conflicting facts, applied to the stored history and the signals. Commits."""
    merged = compute(db, company.id, None, decisions)
    _write(db, company, merged, {}, datetime.utcnow())
    db.commit()
    return merged
