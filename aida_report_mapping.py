"""Reviewed source-to-fact connections for the AIDA report importer.

Source identifiers describe a section, line and relative fiscal period. Cell
addresses and dates remain evidence, so a profile can work on next year's file
or on another company's report without pointing to the wrong physical row.
"""
import re

from aida_single_report import DATASET_NAME, _METRICS, key, number, report_date

PROFILE_PREFIX = "aida-report::"
DEFAULT_PROFILE = "Standard AIDA report"
NEW_INDICATOR = "__new__"


def report_sources(report):
    """Every financial point, including blank/unavailable and unknown lines."""
    sources = {}
    occurrences = {}
    latest = int(report["dates"][0][:4])
    for table in report["financial_tables"]:
        for row in table["rows"]:
            stem = "|".join(("financial", key(table["section"]), key(table["basis"]), key(row["label"])))
            occurrences[stem] = occurrences.get(stem, 0) + 1
            stem += f"|{occurrences[stem]}"
            for date, point in row["values"].items():
                offset = latest - int(date[:4])
                period = "latest" if offset == 0 else f"y-{offset}"
                source_id = f"{stem}|{period}"
                if source_id in sources:
                    source_id += "|" + date[5:]
                canonical = _METRICS.get(key(row["label"]))
                unit = table["units"].get(date)
                row_label = key(row["label"])
                if "%" in row["label"]:
                    unit = "%"
                elif canonical == "employees":
                    unit = "employees"
                elif canonical in {"leverage_ratio", "interest_coverage"} or "ratio" in row_label or "volte" in row_label:
                    unit = "times"
                elif re.search(r"\b(?:gg|giorni)\b", row_label):
                    unit = "days"
                sources[source_id] = dict(kind="financial", label=row["label"], section=table["section"],
                                          sheet=table["sheet"], cell=point["cell"], date=date, period=period,
                                          value=point["value"], raw_value=point["value"], unit=unit,
                                          table_unit=table["units"].get(date),
                                          default=f"variable:{canonical}_{period}" if canonical and offset <= 2 else None)
    for field, value in report["company"].items():
        if value is None:
            continue
        # Matching stays anchored to the report's verified fiscal identifier.
        locked = field in {"registration_number", "legal_name"}
        sources[f"company|{field}"] = dict(kind="company", label=field.replace("_", " ").title(),
                                              section="Company details", value=value, raw_value=value,
                                              date=None, period=None, sheet=None, cell=None, unit=None,
                                              default=f"company:{field}", locked=locked)
    occurrences = {}
    for field in report["fields"]:
        stem = f"field|{key(field['section'])}|{key(field['label'])}"
        occurrences[stem] = occurrences.get(stem, 0) + 1
        sources[f"{stem}|{occurrences[stem]}"] = dict(kind="field", label=field["label"], section=field["section"],
                                                       sheet=field["sheet"], cell=field["value_cell"], date=None,
                                                       period=None, value=field["value"], raw_value=field["value"],
                                                       unit=None, default=None)
    return sources


def suggest_report_mapping(report, profile=None):
    profile = profile or {}
    return {sid: (src["default"] if src.get("locked") else profile.get(sid, src["default"]))
            for sid, src in report_sources(report).items()}


def report_targets(db, report):
    from company_service import CANONICAL_SERIES_LABELS, valid_targets_for_column
    targets = valid_targets_for_column(db)
    targets[""] = "— Ignore (kept in the report) —"
    for period in sorted({s["period"] for s in report_sources(report).values() if s["period"]}):
        for base, label in CANONICAL_SERIES_LABELS.items():
            targets.setdefault(f"variable:{base}_{period}", f"Financial fact: {label} — {period}")
    return targets


def profile_key(name):
    if not str(name or "").strip():
        raise ValueError("Mapping profile name is required.")
    return PROFILE_PREFIX + str(name).strip()


def load_report_profile(db, name):
    from company_service import load_mapping_profile
    return load_mapping_profile(db, profile_key(name))


def save_report_profile(db, name, mapping):
    """Caller commits profile and data together."""
    import json
    from models import ColumnMappingProfile
    scoped = profile_key(name)
    if len(scoped) > 150:
        raise ValueError("Mapping profile name is too long.")
    row = db.query(ColumnMappingProfile).filter_by(dataset_name=scoped).first()
    if row is None:
        row = ColumnMappingProfile(dataset_name=scoped)
        db.add(row)
    row.country, row.mapping_json = "Italy", json.dumps(mapping)


def prepare_connections(db, report, mapping=None, new_labels=None):
    """Pure/read-only mapping evaluation used by preview, import and remapping."""
    from company_service import derive_signals_from_variables, _norm
    from indicators import fetch_indicator_defs
    sources = report_sources(report)
    mapping = suggest_report_mapping(report) if mapping is None else {sid: mapping.get(sid) for sid in sources}
    targets = report_targets(db, report)
    definitions = fetch_indicator_defs(db)
    resolved, proposals, groups, direct, company = {}, {}, {}, {}, {}
    warnings = []
    for sid, src in sources.items():
        target = mapping.get(sid) or None
        if src.get("locked"):
            target = src["default"]
        if target == NEW_INDICATOR:
            label = str((new_labels or {}).get(sid, "")).strip()
            if not label:
                raise ValueError(f"Enter a label for the new indicator connected to {src['label']}.")
            indicator = _norm(label)[:100]
            target = f"indicator:{indicator}"
            proposals[sid] = label
        elif target and target not in targets:
            raise ValueError(f"Unknown mapping target for {src['label']}: {target}")
        resolved[sid] = target
        if not target:
            continue
        evidence = {k: src.get(k) for k in ("sheet", "cell", "date", "unit", "table_unit", "raw_value", "label", "period")}
        evidence["column"] = sid
        if target.startswith("company:"):
            field = target.split(":", 1)[1]
            if field in {"registration_number", "legal_name"} and not src.get("locked"):
                raise ValueError("Company identity is fixed by the report; connect other fields separately.")
            value = src["value"]
            if value is None or str(value).strip().lower() in {"", "n.d.", "-"}:
                continue
            if field == "incorporation_date":
                value = report_date(value)
                if value is None:
                    raise ValueError(f"{src['label']} is not a valid incorporation date.")
            elif field == "headcount":
                value = number(value)
                if value is None or value < 0 or not value.is_integer():
                    raise ValueError(f"{src['label']} is not a whole, non-negative employee count.")
                value = int(value)
            else:
                value = str(value).strip()
            if field in company and company[field] != value:
                raise ValueError(f"Two different source values are connected to Company: {field}.")
            company[field] = value
            continue
        value = number(src["value"])
        if target.startswith("variable:"):
            variable = target.split(":", 1)[1]
            base, period = variable.rsplit("_", 1)
            if src.get("date") and src["period"] != period:
                raise ValueError(f"{src['label']} ({src['date']}) must map to its actual period {src['period']}.")
            monetary = base not in {"employees", "leverage_ratio", "interest_coverage"}
            if monetary and value is not None and src["kind"] == "financial":
                unit = key(src["unit"])
                if unit not in {"migl eur", "eur"}:
                    raise ValueError(f"Unknown monetary unit for {src['label']}; choose an appropriate source.")
                if unit == "eur":
                    value /= 1000
            groups.setdefault(variable, []).append((value, evidence))
        elif target.startswith("indicator:"):
            indicator = target.split(":", 1)[1]
            if indicator not in definitions and sid not in proposals:
                raise ValueError(f"Unknown indicator: {indicator}")
            direct.setdefault(indicator, []).append((value, evidence))
    facts, lineage, variable_sources = {}, {}, {}
    for variable, observations in groups.items():
        values = {v for v, _ in observations if v is not None}
        fields = [e for _, e in observations]
        dates = {e["date"] for e in fields if e["date"]}
        conflicted = len(values) > 1 or len(dates) > 1
        if conflicted:
            warnings.append(f"Conflicting observations connected to {variable}; excluded from calculations.")
        facts[variable] = next(iter(values)) if len(values) == 1 and not conflicted else None
        lineage[variable] = fields
        variable_sources[variable] = {**fields[0], "observations": fields}
    drafts = {}
    for name, (value, status, fields, extra) in derive_signals_from_variables(facts, variable_sources).items():
        drafts[name] = {**extra, "value": value, "status": status, "text": None,
                        "basis": extra.get("basis", "Calculated from reviewed facts"),
                        "inputs": {v: facts[v] for v in extra.get("canonical_variables", [])}, "raw_fields": fields}
    for indicator, observations in direct.items():
        values = {v for v, _ in observations if v is not None}
        dates = {e["date"] for _, e in observations if e["date"]}
        if len(values) > 1 or len(dates) > 1:
            raise ValueError(f"Connect just one reporting date/value to direct indicator {indicator}.")
        value = next(iter(values), None)
        drafts[indicator] = dict(value=value, status="present" if value is not None else "not_yet_checked", text=None,
                                 basis="Direct value from the reviewed report connection, in the source unit",
                                 inputs={}, raw_fields=[e for _, e in observations], derived_automatically=False)
    facts["accounts_close"], facts["derived_last_accounts_year"] = report["dates"][0], int(report["dates"][0][:4])
    return dict(mapping=resolved, proposals=proposals, company=company, facts=facts, lineage=lineage,
                drafts=drafts, warnings=warnings, sources=sources)
