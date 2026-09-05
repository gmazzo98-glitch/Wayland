"""
Company Management & Country-Specific Ingestion Service.
Supports German (DE) and Italian (IT) companies with automated registration
normalization, indicator signal initialization, and country-gated API execution.
"""

import io
import csv
import json
import re
import itertools
from datetime import datetime
import pandas as pd
from sqlalchemy.orm import Session

from models import Company, SignalRecord, ColumnMappingProfile, IndicatorDefinition, PilotOutcome, RawImportRecord, CompanyPerson
from indicators import fetch_indicator_defs, TREND_INDICATOR_KEYS, CAT_CONTEXT
from utils import normalize_registration_nr
from adapters import epo_ops, euipo, destatis, eu_funding, arbeitsagentur, google_news
from scrapers import handelsregister_free, wappalyzer_local, management_diversity

SUPPORTED_COUNTRIES = ["Germany", "Italy"]

# Source applicability by country:
# - Universal / EU: EPO OPS, EUIPO, EU Funding Portal, Wappalyzer, Google News, Own-Site Scrape
# - Germany only: Destatis, Arbeitsagentur, Handelsregister Free Snapshot, Bundesanzeiger, Kununu Reseller
# - Italy only: Italian national registers / ISTAT (future integration hooks)
COUNTRY_SOURCE_MAP = {
    "Germany": {
        "Phase 1": ["EPO OPS", "EUIPO", "Destatis", "EU Funding Portal", "Arbeitsagentur"],
        "Phase 2": ["Handelsregister Free Snapshot"],
        "Phase 3": ["Bundesanzeiger"],
        "Phase 4": ["Wappalyzer", "Google News", "Own-Site Scrape"],
        "Phase 5": ["Kununu Reseller"],
    },
    "Italy": {
        "Phase 1": ["EPO OPS", "EUIPO", "EU Funding Portal"],
        "Phase 2": [],  # German Handelsregister not applicable
        "Phase 3": [],  # Bundesanzeiger not applicable
        "Phase 4": ["Wappalyzer", "Google News", "Own-Site Scrape"],
        "Phase 5": [],  # Kununu (DACH focus) not applicable
    }
}

GERMAN_ONLY_SOURCES = {
    "Destatis", "Arbeitsagentur", "Handelsregister Free Snapshot",
    "Bundesanzeiger", "Kununu Reseller"
}


def is_source_applicable(source_name: str, country: str) -> bool:
    """Checks if an ingestion source / API is applicable for the given country."""
    if country == "Italy" and source_name in GERMAN_ONLY_SOURCES:
        return False
    return True


def get_applicable_sources_for_company(company: Company) -> list:
    """Returns a list of all source names applicable to the company's country."""
    country = company.country or "Germany"
    country_phases = COUNTRY_SOURCE_MAP.get(country, COUNTRY_SOURCE_MAP["Germany"])
    applicable = []
    for sources in country_phases.values():
        applicable.extend(sources)
    return applicable


def sync_company_applicable_sources(company: Company, db: Session, phases: list = None) -> dict:
    """
    Executes live/simulated pipeline adapters for a single company,
    activating ONLY the sources applicable to that company's country.
    """
    if phases is None:
        phases = [1, 4]

    results = {}
    country = company.country or "Germany"

    if 1 in phases:
        # EU / Universal Phase 1 APIs (Both DE & IT)
        results["EPO OPS"] = epo_ops.sync_company_patents(company, db)
        results["EUIPO"] = euipo.sync_company_trademarks(company, db)
        results["EU Funding Portal"] = eu_funding.sync_company_grants(company, db)

        # Germany-specific Phase 1 APIs
        if country == "Germany":
            results["Destatis"] = destatis.sync_sector_export_exposure(company, db)
            results["Arbeitsagentur"] = arbeitsagentur.sync_job_velocity(company, db)

    if 2 in phases and country == "Germany":
        results["Handelsregister"] = handelsregister_free.index_handelsregister_snapshot(company, db)

    if 4 in phases:
        # Phase 4 Web & Social (Both DE & IT)
        results["Wappalyzer"] = wappalyzer_local.sync_tech_stack(company, db)
        results["Management Diversity"] = management_diversity.sync_management_diversity(company, db)
        results["Partnership News"] = google_news.sync_partnership_news(company, db)
        results["Innovation Statements"] = google_news.sync_innovation_statements(company, db)

    return results


def create_company(db: Session, data: dict, auto_sync: bool = False) -> tuple:
    """
    Creates a new target company with country-aware registration normalization,
    initializes all SignalRecord rows, and optionally triggers applicable APIs.

    Returns: (Company, None) on success or (None, error_message) on failure.
    """
    legal_name = (data.get("legal_name") or "").strip()
    raw_reg_nr = (data.get("registration_number") or "").strip()
    country = data.get("country", "Germany").strip()
    if country not in SUPPORTED_COUNTRIES:
        country = "Germany"

    if not legal_name:
        return None, "Legal Name is required."
    if not raw_reg_nr:
        return None, "Registration Number is required."

    norm_reg_nr = normalize_registration_nr(raw_reg_nr, country=country)
    if not norm_reg_nr:
        return None, f"Invalid registration number format for {country}: '{raw_reg_nr}'"

    # Check for duplicate registration number
    existing = db.query(Company).filter_by(registration_number=norm_reg_nr).first()
    if existing:
        return None, f"Company with registration number '{norm_reg_nr}' already exists ({existing.legal_name})."

    # Headcount & Segment derivation
    headcount = data.get("headcount")
    if headcount is not None and str(headcount).strip() != "":
        try:
            headcount = int(headcount)
        except ValueError:
            headcount = None

    segment = data.get("segment")
    if not segment or segment not in ("Midcap", "SME"):
        if headcount is not None:
            segment = "SME" if headcount < 250 else "Midcap"
        else:
            segment = "Midcap"

    nace_code = (data.get("nace_code") or "A01.1").strip()
    sector_name = (data.get("sector_name") or "Agrifood & Agriculture").strip()
    website_url = (data.get("website_url") or "").strip() or None
    shortlist_status = data.get("shortlist_status", "candidate")

    company = Company(
        legal_name=legal_name,
        registration_number=norm_reg_nr,
        country=country,
        nace_code=nace_code,
        sector_name=sector_name,
        website_url=website_url,
        segment=segment,
        headcount=headcount,
        headcount_source_tier="T1" if headcount else None,
        shortlist_status=shortlist_status,
        shortlisted_at=datetime.utcnow() if shortlist_status in ("shortlisted", "in_pilot") else None,
    )
    db.add(company)
    db.flush()

    # Initialize all active indicator definitions as not_yet_checked SignalRecords
    indicator_defs = fetch_indicator_defs(db)
    for sig_key, defn in indicator_defs.items():
        source_sys = defn.get("source_system") or "Unknown"
        is_app = is_source_applicable(source_sys, country)

        sig_rec = SignalRecord(
            company_id=company.id,
            signal_key=sig_key,
            source=source_sys,
            numeric_value=None,
            text_value=None,
            status="not_yet_checked",
            confidence=1.0,
            is_simulated=True,
            fetched_at=datetime.utcnow(),
            raw_payload_ref=json.dumps({
                "initialized": True,
                "applicable_for_country": is_app,
                "country": country,
                "signal_key": sig_key
            })
        )
        db.add(sig_rec)

    db.commit()

    # Optional immediate live API sync
    if auto_sync:
        sync_company_applicable_sources(company, db, phases=[1, 4])

    return company, None


def get_csv_template() -> str:
    """Returns a CSV string template with German and Italian sample companies."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "legal_name", "registration_number", "country", "nace_code",
        "sector_name", "website_url", "segment", "headcount"
    ])
    writer.writerow([
        "BioBavaria SmartFarming GmbH", "HRB 789012", "Germany", "A01.11",
        "Agrifood & Smart Farming", "https://biobavaria.de", "Midcap", "320"
    ])
    writer.writerow([
        "AgroTech Lombardia S.r.l.", "IT09876543210", "Italy", "A01.13",
        "Horticulture & Vertical Farming", "https://agrotech-lombardia.it", "SME", "45"
    ])
    writer.writerow([
        "Emilia Romagna Precision AG", "REA BO-1234567", "Italy", "A01.61",
        "Agricultural Machinery & Robotics", "https://er-precision.it", "Midcap", "410"
    ])
    return output.getvalue()


def import_companies_from_csv(db: Session, csv_content_or_file, auto_sync: bool = False) -> dict:
    """
    Imports multiple companies from CSV file buffer or text.
    Handles German and Italian entries with column mapping and validation.
    """
    if hasattr(csv_content_or_file, "read"):
        content = csv_content_or_file.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
    else:
        content = str(csv_content_or_file)

    try:
        df = pd.read_csv(io.StringIO(content))
    except Exception as e:
        return {"created": 0, "skipped": 0, "errors": [f"Could not parse CSV: {str(e)}"]}

    # Clean and normalize column names
    col_map = {c: c.lower().strip().replace(" ", "_") for c in df.columns}
    df = df.rename(columns=col_map)

    required_cols = ["legal_name", "registration_number"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        return {
            "created": 0,
            "skipped": 0,
            "errors": [f"Missing required CSV column(s): {', '.join(missing)}. Required: legal_name, registration_number"]
        }

    created_count = 0
    skipped_count = 0
    errors = []

    for idx, row in df.iterrows():
        row_dict = row.to_dict()
        row_num = idx + 2  # 1-indexed header + 1

        # Clean NaNs
        cleaned_data = {}
        for k, v in row_dict.items():
            if pd.isna(v):
                cleaned_data[k] = None
            else:
                cleaned_data[k] = str(v).strip()

        comp, err = create_company(db, cleaned_data, auto_sync=auto_sync)
        if comp:
            created_count += 1
        else:
            skipped_count += 1
            errors.append(f"Row {row_num} ('{cleaned_data.get('legal_name', 'Unknown')}'): {err}")

    return {
        "created": created_count,
        "skipped": skipped_count,
        "errors": errors
    }


# =============================================================================
# Flexible column-mapping data feeder
#
# Unlike import_companies_from_csv above (one fixed 8-column shape), this lets
# a dataset with ARBITRARY column names/order be mapped onto Company fields
# and IndicatorDefinition signals interactively, then replayed on future
# uploads of the same shape via a saved ColumnMappingProfile. See the
# "Flexible Column-Mapping Data Feeder" plan for the full design rationale.
# =============================================================================

_GROUP_SUFFIX_RE = re.compile(r'^(?P<base>.+)_(?P<suffix>latest|y-\d+)$')

# Fixed Company-field targets a column can be mapped onto, keyed by the
# normalized names real-world exports are likely to use. Auto-suggestion only
# — the mapping UI lets the user pick any of these regardless of this table.
COMPANY_FIELD_ALIASES = {
    "legal_name": "company:legal_name", "ragione_sociale": "company:legal_name",
    "company_name": "company:legal_name", "name": "company:legal_name",
    "registration_number": "company:registration_number", "partita_iva": "company:registration_number",
    "vat_number": "company:registration_number", "p_iva": "company:registration_number",
    "nace_code": "company:nace_code", "ateco_code": "company:nace_code",
    "sector_name": "company:sector_name", "ateco_description": "company:sector_name",
    "website_url": "company:website_url", "website": "company:website_url",
    "region": "company:region", "province": "company:province", "legal_form": "company:legal_form",
    "incorporation_date": "company:incorporation_date",
    "status": "company:registry_status", "registry_status": "company:registry_status",
    "headcount": "company:headcount", "employees": "company:headcount",
    "number_of_employees": "company:headcount",
    "external_ref_id": "company:external_ref_id", "company_id": "company:external_ref_id",
    "company_id_by_aida": "company:external_ref_id", "aida_company_id": "company:external_ref_id",
}

# Base names (after suffix-stripping) that alias onto a computed trend
# indicator — only offered/suggested when the column group actually has 2+
# timepoints (see detect_column_groups / compute_group_value).
TREND_BASE_ALIASES = {"revenue": "revenue_trend", "ebit": "ebit_trend", "gross_margin": "margin_compression"}

# Base names that alias onto a direct-value (non-trend) indicator whose own
# key doesn't literally match the stripped base name.
DIRECT_BASE_ALIASES = {
    "interest_coverage": "interest_coverage_ratio",
    "total_assets": "total_assets",
    "cash": "cash_position",
    "total_debt": "debt_level",
    "leverage_ratio": "leverage_ratio",
    "subsidiary_count": "subsidiary_participations",
}

STRING_COMPANY_FIELDS = {
    "legal_name", "nace_code", "sector_name", "website_url",
    "region", "province", "legal_form", "registry_status", "external_ref_id",
}

COMPANY_TARGET_LABELS = {
    "company:legal_name": "Company: Legal Name",
    "company:registration_number": "Company: Registration Number (required, exactly one)",
    "company:nace_code": "Company: NACE/ATECO Code",
    "company:sector_name": "Company: Sector Name",
    "company:website_url": "Company: Website",
    "company:region": "Company: Region",
    "company:province": "Company: Province",
    "company:legal_form": "Company: Legal Form",
    "company:incorporation_date": "Company: Incorporation Date",
    "company:registry_status": "Company: Registry Status",
    "company:headcount": "Company: Headcount",
    "company:external_ref_id": "Company: External Reference ID",
}


def _norm(s) -> str:
    return str(s).strip().lower().replace(" ", "_")


def create_ad_hoc_indicator(db: Session, label: str, dataset_name: str = None, source_column: str = None) -> str:
    """
    Creates a new context-axis, unscored (weight=0) IndicatorDefinition for a
    column the user chose to map as "genuinely new" rather than link to an
    existing one — same pattern the catalog already uses for informational
    tags (product_type_tag, family_ownership_share, etc.). It's saved into
    the same IndicatorDefinition table every other indicator lives in, so it
    shows up on the Indicator Weights page immediately (no separate list) —
    editable/re-weightable there like any other row, just starting inert.

    dataset_name/source_column (when given) go into source_system/proxy/
    comment so the row is self-explanatory on that page rather than a bare
    label with no context for where it came from.

    Returns the new indicator's key; no-ops (returns the existing key) if one
    with that exact key already exists.
    """
    key = _norm(label)[:100]
    if not key:
        return None
    existing = db.query(IndicatorDefinition).filter_by(key=key).first()
    if existing:
        return key
    origin = f"column '{source_column}' in the '{dataset_name}' dataset" if source_column and dataset_name else "an uploaded dataset column"
    db.add(IndicatorDefinition(
        key=key, label=str(label).strip()[:255] or key, category=CAT_CONTEXT, axis="context",
        weight=0.0, phase=1, is_active=True, source_system=dataset_name,
        proxy=f"As provided in {origin} — not yet mapped onto an existing indicator.",
        comment=(
            f"Created automatically from {origin} via the Flexible Data Import feeder "
            f"on {datetime.utcnow().strftime('%Y-%m-%d')}. Informational/context by default (weight 0, "
            f"never summed into Need/Readiness) — re-categorize the Axis/Weight above if this should "
            f"actually be scored."
        ),
        source_description=dataset_name,
    ))
    db.commit()
    return key


def parse_uploaded_file(file_or_buffer, filename: str = None) -> pd.DataFrame:
    """
    Reads an uploaded company dataset (.csv or .xlsx, one row per company)
    into a DataFrame with normalized column headers — same lower/strip/
    space-to-underscore normalization import_companies_from_csv already uses
    (deliberately NOT touching hyphens: detect_column_groups' 'y-1'-style
    suffix pattern depends on the hyphen surviving normalization).
    """
    name = (filename or getattr(file_or_buffer, "name", "") or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xls"):
        df = pd.read_excel(file_or_buffer)
    else:
        if hasattr(file_or_buffer, "read"):
            content = file_or_buffer.read()
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
        else:
            content = str(file_or_buffer)
        df = pd.read_csv(io.StringIO(content))
    df = df.rename(columns={c: str(c).strip().lower().replace(" ", "_") for c in df.columns})
    return df


def detect_column_groups(columns: list) -> dict:
    """
    Groups normalized column names sharing a '<base>_latest' / '<base>_y-1' /
    '<base>_y-2' ... convention into time-series variables, so the mapping UI
    can offer computed trend indicators (revenue_trend, ebit_trend,
    margin_compression) only where a real multi-year series exists. A column
    with no recognized suffix is its own single-point group (base = the
    whole column name, suffix 'value').

    Returns {base: {"points": {suffix: original_column_name}, "is_timeseries": bool}}.
    """
    groups = {}
    for col in columns:
        match = _GROUP_SUFFIX_RE.match(col)
        base, suffix = (match.group("base"), match.group("suffix")) if match else (col, "value")
        groups.setdefault(base, {"points": {}})["points"][suffix] = col
    for g in groups.values():
        g["is_timeseries"] = len(g["points"]) >= 2
    return groups


def suggest_mapping(db: Session, groups: dict, existing_profile: dict = None) -> dict:
    """
    Auto-suggests a target for each detected column group: an existing saved
    profile's assignment (reviewed, never silently trusted — see the mapping
    UI), else a known alias, else an exact match against an indicator's own
    key/label, else None (genuinely unrecognized — the user must assign it).
    """
    indicator_defs = fetch_indicator_defs(db)
    key_by_label = {_norm(defn["label"]): key for key, defn in indicator_defs.items()}

    suggestions = {}
    for base, group in groups.items():
        norm_base = _norm(base)

        if existing_profile and base in existing_profile:
            suggestions[base] = existing_profile[base]
            continue

        target = COMPANY_FIELD_ALIASES.get(norm_base)
        if not target and group["is_timeseries"] and norm_base in TREND_BASE_ALIASES:
            target = f"indicator:{TREND_BASE_ALIASES[norm_base]}"
        if not target and norm_base in DIRECT_BASE_ALIASES:
            target = f"indicator:{DIRECT_BASE_ALIASES[norm_base]}"
        if not target and norm_base in indicator_defs:
            target = f"indicator:{norm_base}"
        if not target and norm_base in key_by_label:
            target = f"indicator:{key_by_label[norm_base]}"
        suggestions[base] = target
    return suggestions


def valid_targets_for_group(db: Session, group: dict) -> dict:
    """
    The full set of {target_key: display_label} the mapping UI may offer for
    one column group — trend-style indicators only appear for a group with
    2+ timepoints, so a single column can never be wired to a formula that
    structurally needs two (the bug-safety mechanism, not a post-hoc check).
    """
    indicator_defs = fetch_indicator_defs(db)
    options = {"": "— Ignore —"}
    options.update(COMPANY_TARGET_LABELS)
    for key, defn in sorted(indicator_defs.items(), key=lambda kv: (kv[1]["category"], kv[1]["label"])):
        if key in TREND_INDICATOR_KEYS and not group["is_timeseries"]:
            continue
        options[f"indicator:{key}"] = f"{defn['category']} — {defn['label']}"
    return options


def _json_safe(v):
    """Coerces a raw pandas cell value into something json.dumps/JSON-column
    can actually serialize — numpy scalars (int64/float64/bool_), Timestamps,
    and NaN all fail otherwise."""
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if hasattr(v, "item"):  # numpy scalar (int64, float64, bool_, ...)
        try:
            v = v.item()
        except (TypeError, ValueError):
            pass
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def _clean_str(v):
    """Coerces a raw pandas cell value to a plain str for a String column —
    guards against writing a numpy float/int straight into a VARCHAR column,
    which SQLite tolerates silently but Postgres may not adapt cleanly."""
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    s = str(v).strip()
    return s or None


def _to_float(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pick_latest_and_base(point_values: dict):
    """point_values: {suffix: raw_cell_value}. Picks the most-recent value
    ('latest', else the lone 'value', else the smallest y-N as a proxy) and
    the furthest-back value (largest y-N, or None if there isn't one)."""
    if "latest" in point_values:
        latest_val = point_values["latest"]
    elif "value" in point_values:
        latest_val = point_values["value"]
    else:
        y_points = sorted((int(s.split("-")[1]), s) for s in point_values if s.startswith("y-"))
        latest_val = point_values[y_points[0][1]] if y_points else None

    y_points_all = sorted((int(s.split("-")[1]), s) for s in point_values if s.startswith("y-"))
    base_val = point_values[y_points_all[-1][1]] if y_points_all else None
    return latest_val, base_val


def compute_group_value(group: dict, row: dict, indicator_key: str = None):
    """
    Resolves one column-group's contribution to a target for a single row.
    Returns (numeric_value_or_None, status) with status 'present' or
    'not_yet_checked' — a missing/zero base is left not_yet_checked rather
    than faked into a number, same tri-state honesty rule the rest of the
    signal pipeline follows.

    indicator_key in TREND_INDICATOR_KEYS computes a change between the
    'latest' point and the furthest-back 'y-N' point (% change, abs-based for
    signed metrics like EBIT so a sign flip in the base doesn't invert the
    trend direction; margin_compression is a direct point-decline instead of
    a %, matching that indicator's own 0-25 "decline points" definition).
    Anything else just takes the single most recent point.
    """
    point_values = {suffix: row.get(col) for suffix, col in group["points"].items()}
    latest_val, base_val = _pick_latest_and_base(point_values)
    latest_num = _to_float(latest_val)

    if indicator_key in TREND_INDICATOR_KEYS:
        base_num = _to_float(base_val)
        if latest_num is None or base_num is None:
            return None, "not_yet_checked"
        if indicator_key == "margin_compression":
            return max(0.0, base_num - latest_num), "present"
        if base_num == 0:
            return None, "not_yet_checked"
        return (latest_num - base_num) / abs(base_num) * 100.0, "present"

    if latest_num is None:
        return None, "not_yet_checked"
    return latest_num, "present"


def _parse_date(val):
    try:
        ts = pd.to_datetime(val, errors="coerce")
    except (TypeError, ValueError):
        return None
    if ts is None or pd.isna(ts):
        return None
    return ts.to_pydatetime()


def apply_data_import(db: Session, df: pd.DataFrame, mapping: dict, dataset_name: str,
                       country: str = "Italy", overwrite_conflicts: bool = False,
                       dry_run: bool = False, source_filename: str = None) -> dict:
    """
    Applies a reviewed column mapping ({group_base: 'company:<field>' |
    'indicator:<key>' | None}) to every row of df, one row per company.

    Reuses create_company() for brand-new companies (dedup check, segment/
    headcount derivation, full not-yet-checked signal scaffold), then writes
    two things per row on a real (non-dry-run) pass: the mapped signals as
    real SignalRecords (is_simulated=False, status='present') tagged
    source=dataset_name — the structured side — and the row's complete
    original column set as a RawImportRecord blob, independent of what got
    mapped — the blob side, for retroactively applying a new mapping idea
    later without re-uploading the file.

    Conflict rule: a company that already has a *present* signal sourced
    from this exact dataset_name is a conflict — real runs skip it unless
    overwrite_conflicts is set; a *different* dataset_name for the same
    company just merges in, no flag. dataset_name is therefore what scopes
    "same vs. different dataset type" per the user's own rule.

    dry_run is a genuinely separate read-only classification pass (file
    already parsed into df; only Company lookups happen, zero writes) rather
    than a transaction-rollback trick — create_company() commits internally,
    so it can't be safely wrapped in a savepoint for preview purposes.

    Returns {"created", "merged", "overwritten": int,
             "conflicts": [{"legal_name", "registration_number"}...],
             "errors": [str...]}.
    """
    result = {"created": 0, "merged": 0, "overwritten": 0, "conflicts": [], "errors": []}

    if not dataset_name or not str(dataset_name).strip():
        result["errors"].append("Dataset name is required.")
        return result
    dataset_name = str(dataset_name).strip()

    groups = detect_column_groups(list(df.columns))

    reg_bases = [b for b, t in mapping.items() if t == "company:registration_number"]
    if len(reg_bases) != 1:
        result["errors"].append(
            f"Exactly one column must be mapped to Registration Number (found {len(reg_bases)})."
        )
        return result
    reg_base = reg_bases[0]

    indicator_defs = fetch_indicator_defs(db)

    for idx, row in df.iterrows():
        row_dict = row.to_dict()
        row_num = idx + 2  # 1-indexed header + 1

        reg_points = {suffix: row_dict.get(col) for suffix, col in groups[reg_base]["points"].items()}
        raw_reg, _ = _pick_latest_and_base(reg_points)
        norm_reg_nr = (
            normalize_registration_nr(str(raw_reg).strip(), country=country)
            if raw_reg is not None and not (isinstance(raw_reg, float) and pd.isna(raw_reg)) and str(raw_reg).strip()
            else ""
        )
        if not norm_reg_nr:
            result["errors"].append(f"Row {row_num}: could not read a registration number.")
            continue

        existing = db.query(Company).filter_by(registration_number=norm_reg_nr).first()

        # Resolve every mapped field/indicator for this row up front.
        company_field_updates = {}
        signal_updates = {}  # signal_key -> (value, status)
        for base, target in mapping.items():
            if not target or base == reg_base:
                continue
            group = groups.get(base)
            if not group:
                continue
            point_values = {suffix: row_dict.get(col) for suffix, col in group["points"].items()}

            if target.startswith("company:"):
                field = target.split(":", 1)[1]
                val, _ = _pick_latest_and_base(point_values)
                if val is not None and not (isinstance(val, float) and pd.isna(val)):
                    company_field_updates[field] = val
            elif target.startswith("indicator:"):
                sig_key = target.split(":", 1)[1]
                if sig_key in indicator_defs:
                    signal_updates[sig_key] = compute_group_value(group, row_dict, sig_key)

        is_conflict = False
        if existing:
            is_conflict = db.query(SignalRecord).filter_by(
                company_id=existing.id, source=dataset_name, status="present"
            ).first() is not None

        if is_conflict and not (overwrite_conflicts and not dry_run):
            result["conflicts"].append({"legal_name": existing.legal_name, "registration_number": norm_reg_nr})
            continue

        if dry_run:
            if not existing:
                result["created"] += 1
            else:
                result["merged"] += 1
            continue

        # --- real write path ---
        if not existing:
            create_data = {
                "legal_name": _clean_str(company_field_updates.get("legal_name")) or f"Unnamed ({norm_reg_nr})",
                "registration_number": norm_reg_nr,
                "country": country,
                "nace_code": _clean_str(company_field_updates.get("nace_code")),
                "sector_name": _clean_str(company_field_updates.get("sector_name")),
                "website_url": _clean_str(company_field_updates.get("website_url")),
                "headcount": company_field_updates.get("headcount"),
            }
            company, err = create_company(db, create_data, auto_sync=False)
            if err:
                result["errors"].append(f"Row {row_num} ('{create_data['legal_name']}'): {err}")
                continue
            was_new = True
        else:
            company = existing
            was_new = False

        for field, val in company_field_updates.items():
            if field == "incorporation_date":
                val = _parse_date(val)
                if val is None:
                    continue
            elif field == "headcount":
                try:
                    val = int(float(val))
                except (TypeError, ValueError):
                    continue
                company.headcount_source_tier = "T1"
            elif field in STRING_COMPANY_FIELDS:
                val = _clean_str(val)
                if val is None:
                    continue
            if hasattr(company, field):
                setattr(company, field, val)

        fetched_at = datetime.utcnow()
        for sig_key, (value, status) in signal_updates.items():
            sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key=sig_key).first()
            if not sig:
                sig = SignalRecord(company_id=company.id, signal_key=sig_key, source=dataset_name)
                db.add(sig)
            sig.status = status
            sig.numeric_value = value if status == "present" else None
            sig.confidence = 1.0
            sig.is_simulated = False
            sig.source = dataset_name
            sig.fetched_at = fetched_at
            sig.raw_payload_ref = json.dumps({"dataset": dataset_name, "signal_key": sig_key})

        # Blob side: the complete original row, every column, untouched by
        # the mapping — independent of which subset just got interpreted
        # into signals above. One row per (company, dataset); re-importing
        # the same dataset overwrites it, matching the structured side.
        raw_rec = db.query(RawImportRecord).filter_by(company_id=company.id, dataset_name=dataset_name).first()
        if not raw_rec:
            raw_rec = RawImportRecord(company_id=company.id, dataset_name=dataset_name)
            db.add(raw_rec)
        raw_rec.source_filename = source_filename
        raw_rec.source_row_index = row_num
        raw_rec.raw_row = {col: _json_safe(v) for col, v in row_dict.items()}
        raw_rec.mapping_snapshot = mapping
        raw_rec.updated_at = fetched_at

        db.commit()

        if was_new:
            result["created"] += 1
        elif is_conflict:
            result["overwritten"] += 1
        else:
            result["merged"] += 1

    return result


def save_mapping_profile(db: Session, dataset_name: str, country: str, mapping: dict) -> ColumnMappingProfile:
    dataset_name = str(dataset_name).strip()
    profile = db.query(ColumnMappingProfile).filter_by(dataset_name=dataset_name).first()
    if not profile:
        profile = ColumnMappingProfile(dataset_name=dataset_name)
        db.add(profile)
    profile.country = country
    profile.mapping_json = json.dumps(mapping)
    profile.updated_at = datetime.utcnow()
    db.commit()
    return profile


def load_mapping_profile(db: Session, dataset_name: str) -> dict:
    profile = db.query(ColumnMappingProfile).filter_by(dataset_name=str(dataset_name).strip()).first()
    if not profile:
        return {}
    try:
        return json.loads(profile.mapping_json)
    except (TypeError, ValueError):
        return {}


def list_mapping_profiles(db: Session) -> list:
    return db.query(ColumnMappingProfile).order_by(ColumnMappingProfile.dataset_name).all()


def delete_companies(db: Session, company_ids: list) -> dict:
    """
    Permanently deletes the given companies. SignalRecords cascade via the
    Company.signals relationship (cascade="all, delete-orphan" in models.py);
    PilotOutcome rows don't have that cascade (a company shouldn't silently
    take its own pilot history down without that being visible), so they're
    deleted explicitly here instead of letting a FK constraint fail the
    whole operation.

    Irreversible. The caller (the Manage Companies UI) is responsible for
    confirming with the user before calling this — this function does not
    re-confirm.

    Returns {"deleted", "signals_deleted", "pilot_outcomes_deleted", "raw_import_records_deleted": int}.
    """
    deleted = 0
    signals_deleted = 0
    pilots_deleted = 0
    raw_deleted = 0
    people_deleted = 0
    for company_id in company_ids:
        company = db.query(Company).filter_by(id=company_id).first()
        if not company:
            continue
        signals_deleted += db.query(SignalRecord).filter_by(company_id=company_id).count()
        pilots = db.query(PilotOutcome).filter_by(company_id=company_id).all()
        pilots_deleted += len(pilots)
        for pilot in pilots:
            db.delete(pilot)
        raw_records = db.query(RawImportRecord).filter_by(company_id=company_id).all()
        raw_deleted += len(raw_records)
        for rec in raw_records:
            db.delete(rec)
        people = db.query(CompanyPerson).filter_by(company_id=company_id).all()
        people_deleted += len(people)
        for person in people:
            db.delete(person)
        db.delete(company)
        deleted += 1
    db.commit()
    return {
        "deleted": deleted, "signals_deleted": signals_deleted,
        "pilot_outcomes_deleted": pilots_deleted, "raw_import_records_deleted": raw_deleted,
        "people_deleted": people_deleted,
    }


# =============================================================================
# Management & board roster import
#
# A different shape from the flexible column-mapping feeder above: one source
# row is still one company, but several columns each pack MULTIPLE people's
# values into a single cell, newline-stacked in matching order across columns
# (AIDA's own export convention — e.g. 'DM\nNome completo' holds N directors'
# names, 'DM\nCarica' the same N people's roles, in the same order). That
# can't go through a one-value-per-column mapper; it needs exploding into one
# CompanyPerson row per person. See the "Management & Board Roster Import"
# plan for the full design rationale, including why matching is done by
# legal_name rather than the source file's own BvD ID column (verified
# directly against real data: BvD ID doesn't reliably match the
# registration_number already on file for the same company, legal_name does).
# =============================================================================

# Normalized (lower/strip) Italian sub-label -> CompanyPerson structured column.
# Anything not listed here still survives in full inside raw_fields.
_PERSON_FIELD_ALIASES = {
    "nome completo": "full_name",
    "carica": "role",
    "età": "age",
    "eta": "age",
    "genere": "gender",
    "paese di nazionalità": "nationality",
    "paese di nazionalita": "nationality",
    "data nomina": "appointment_date",
    "data dimissioni": "resignation_date",
    "attuale o precedente": "current_or_former",
}


def parse_roster_file(file_or_buffer, filename: str = None) -> pd.DataFrame:
    """
    Reads an uploaded management/board-style dataset (.xls/.xlsx/.csv) with
    headers LEFT UNTOUCHED apart from stripping — deliberately not lowercased
    or space-to-underscore normalized like parse_uploaded_file, because
    detect_multivalue_groups needs the exact original 'Prefix\\n...' header
    text to find the group boundary.

    AIDA's own multi-sheet exports (verified against a real file) put a
    small "search strategy" cover sheet first and the actual data on a
    sheet named "Risultati" — pandas' default (first sheet) would silently
    read the wrong, tiny sheet. When there's more than one sheet, this
    prefers one named "risultati" (case-insensitive); otherwise falls back
    to whichever sheet has the most rows, since the real data table is
    essentially always the biggest sheet in these exports.
    """
    name = (filename or getattr(file_or_buffer, "name", "") or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xls"):
        all_sheets = pd.read_excel(file_or_buffer, sheet_name=None)
        if len(all_sheets) == 1:
            df = next(iter(all_sheets.values()))
        else:
            risultati = next((n for n in all_sheets if n.strip().lower() == "risultati"), None)
            sheet_name = risultati or max(all_sheets, key=lambda n: len(all_sheets[n]))
            df = all_sheets[sheet_name]
    else:
        if hasattr(file_or_buffer, "read"):
            content = file_or_buffer.read()
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
        else:
            content = str(file_or_buffer)
        df = pd.read_csv(io.StringIO(content))
    df = df.rename(columns={c: str(c).strip() for c in df.columns})
    return df


def detect_multivalue_groups(columns: list) -> dict:
    """
    Groups columns sharing a 'Prefix\\n...' header convention — the text
    before a header's FIRST newline is the group name (e.g. 'DM\\nCarica' and
    'DM\\nEtà' both belong to group 'DM'). A column with no '\\n' in its
    header, or the only column under a given prefix, isn't a real roster
    group (2+ columns required). Generic on purpose — not hardcoded to
    'DM'/'ADV', so it also picks up whatever a future source calls its
    groups, as long as it uses the same one-cell-per-attribute convention.

    Returns {group_name: [original_column_name, ...]}.
    """
    groups = {}
    for col in columns:
        if "\n" in col:
            prefix, _, _rest = col.partition("\n")
            prefix = prefix.strip()
            if prefix:
                groups.setdefault(prefix, []).append(col)
    return {g: cols for g, cols in groups.items() if len(cols) >= 2}


def explode_person_group(row: dict, group_columns: list) -> list:
    """
    Splits each of this group's cells (for one company's row) on '\\n' and
    zips them positionally into one raw dict per person — e.g. the 3rd line
    of every column in the group together form person #2's record. Uses
    zip_longest so one column having fewer stacked lines than another for
    this row degrades to a None for that one field on the extra
    person(s), rather than crashing or silently dropping the whole group.

    Returns a list of {sub_label: value} dicts, sub_label being the
    original column header with the group prefix stripped (e.g.
    'Nome completo', 'Carica', 'Età').
    """
    split_by_col = {}
    for col in group_columns:
        val = row.get(col)
        sub_label = col.partition("\n")[2].strip() or col
        if isinstance(val, str) and val.strip():
            split_by_col[sub_label] = [v.strip() for v in val.split("\n")]
        else:
            split_by_col[sub_label] = []

    max_len = max((len(v) for v in split_by_col.values()), default=0)
    if max_len == 0:
        return []

    people = []
    for i in range(max_len):
        person = {}
        for sub_label, values in split_by_col.items():
            person[sub_label] = values[i] if i < len(values) else None
        people.append(person)
    return people


def _person_structured_fields(raw_person: dict) -> dict:
    """Best-effort maps a raw {sub_label: value} dict onto CompanyPerson's
    structured columns via _PERSON_FIELD_ALIASES; everything stays in
    raw_fields regardless of whether it also got mapped here."""
    structured = {}
    for sub_label, value in raw_person.items():
        # NOT _norm() — that replaces spaces with underscores (for the other
        # importer's column-base names), but these alias keys are natural-
        # language phrases ("nome completo") that need to stay space-separated.
        field = _PERSON_FIELD_ALIASES.get(str(sub_label).strip().lower())
        if not field or value in (None, ""):
            continue
        if field == "age":
            try:
                structured[field] = int(float(value))
            except (TypeError, ValueError):
                pass
        elif field in ("appointment_date", "resignation_date"):
            structured[field] = _parse_date(value)
        else:
            structured[field] = str(value)[:255]

    # Some groups (e.g. AIDA's ADV group) split the name into separate
    # 'Nome'/'Cognome' columns instead of one 'Nome completo' — and reuse
    # those same two columns for a company-type advisor (Nome blank,
    # Cognome holding the firm's name, e.g. "KPMG S.P.A."). Only synthesize
    # when no alias already produced a full_name, so DM's own 'Nome
    # completo' (already handled above) always wins where both exist.
    if not structured.get("full_name"):
        by_norm = {str(k).strip().lower(): v for k, v in raw_person.items()}
        nome = by_norm.get("nome")
        cognome = by_norm.get("cognome")
        joined = " ".join(p for p in (nome, cognome) if p)
        if joined:
            structured["full_name"] = joined[:255]
    return structured


def import_company_people(db: Session, df: pd.DataFrame, dataset_name: str,
                           legal_name_column: str = "Ragione sociale",
                           source_filename: str = None,
                           overwrite_conflicts: bool = False, dry_run: bool = False) -> dict:
    """
    Explodes every detected multi-value group in df into CompanyPerson rows,
    matching each source row to an existing Company by exact legal_name
    (see module docstring above for why not the source's own BvD ID column).
    Never creates a new Company from this file alone — a row that doesn't
    match an existing company is reported in "unmatched", not guessed at.

    Conflict rule mirrors apply_data_import: a company that already has
    CompanyPerson rows under this exact dataset_name is a conflict — real
    runs skip it unless overwrite_conflicts, dry runs just report it.

    Returns {"matched", "people_created", "people_updated": int,
             "unmatched": [legal_name...], "conflicts": [{"legal_name",...}],
             "errors": [str...]}.
    """
    result = {"matched": 0, "people_created": 0, "people_updated": 0,
              "unmatched": [], "conflicts": [], "errors": []}

    if not dataset_name or not str(dataset_name).strip():
        result["errors"].append("Dataset name is required.")
        return result
    dataset_name = str(dataset_name).strip()

    if legal_name_column not in df.columns:
        result["errors"].append(f"Column '{legal_name_column}' (company legal name) not found in the uploaded file.")
        return result

    groups = detect_multivalue_groups(list(df.columns))
    if not groups:
        result["errors"].append("No multi-value 'Prefix\\n...' column groups detected in this file.")
        return result

    for _, row in df.iterrows():
        row_dict = row.to_dict()
        legal_name = _clean_str(row_dict.get(legal_name_column))
        if not legal_name:
            continue

        company = db.query(Company).filter_by(legal_name=legal_name).first()
        if not company:
            result["unmatched"].append(legal_name)
            continue

        is_conflict = db.query(CompanyPerson).filter_by(company_id=company.id, dataset_name=dataset_name).first() is not None
        if is_conflict and not (overwrite_conflicts and not dry_run):
            result["conflicts"].append({"legal_name": legal_name, "registration_number": company.registration_number})
            continue

        result["matched"] += 1
        if dry_run:
            continue

        fetched_at = datetime.utcnow()
        for role_group, group_columns in groups.items():
            people = explode_person_group(row_dict, group_columns)
            for position, raw_person in enumerate(people):
                person = db.query(CompanyPerson).filter_by(
                    company_id=company.id, dataset_name=dataset_name,
                    role_group=role_group, position_in_row=position,
                ).first()
                if not person:
                    person = CompanyPerson(
                        company_id=company.id, dataset_name=dataset_name,
                        role_group=role_group, position_in_row=position,
                    )
                    db.add(person)
                    result["people_created"] += 1
                else:
                    result["people_updated"] += 1
                for field, value in _person_structured_fields(raw_person).items():
                    setattr(person, field, value)
                person.raw_fields = {k: _json_safe(v) for k, v in raw_person.items()}
                person.updated_at = fetched_at

        db.commit()

    return result
