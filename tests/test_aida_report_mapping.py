import json
from io import BytesIO
from unittest.mock import patch

import openpyxl
import pytest
from sqlalchemy import event
from streamlit.testing.v1 import AppTest

from aida_single_report import DATASET_NAME, parse_report, plan_import, import_report
from aida_report_mapping import (
    NEW_INDICATOR, PROFILE_PREFIX, report_sources, suggest_report_mapping,
    load_report_profile, prepare_connections, report_targets,
)
from models import Company, CompanyPerson, ColumnMappingProfile, IndicatorDefinition, RawImportRecord, SignalRecord
from tests.test_report_upload import db, report_file  # noqa: F401 (shared isolated DB fixture)


def extra_report(second_company=False):
    workbook = openpyxl.load_workbook(report_file())
    ws = workbook.active
    ws["A28"] = "B.7. Servizi"
    for first, last, value in ((17, 20, 300), (21, 24, 400), (25, 29, 200), (30, 34, 100)):
        ws.merge_cells(start_row=28, end_row=28, start_column=first, end_column=last)
        ws.cell(28, first, value)
    if second_company:
        ws["B1"], ws["AB3"] = "SECOND EXAMPLE S.P.A.", "11111111111"
        for row in ws:
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("31/12/"):
                    cell.value = "31/12/" + str(int(cell.value[-4:]) + 1)
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return parse_report(output)


def source_id(report, label="B.7. Servizi", period="latest"):
    return next(sid for sid, src in report_sources(report).items() if src["label"] == label and src["period"] == period)


def test_all_financial_points_are_connectable_including_unknown_and_unavailable():
    report = extra_report()
    financial = [s for s in report_sources(report).values() if s["kind"] == "financial"]
    assert len(financial) == sum(len(row["values"]) for table in report["financial_tables"] for row in table["rows"])
    assert any(s["value"] == "n.d." for s in financial)
    sid = source_id(report)
    assert suggest_report_mapping(report)[sid] is None
    assert report_sources(report)[sid]["cell"] == "U28"


def test_unknown_line_connects_to_existing_indicator_with_exact_evidence(db):
    report = extra_report()
    sid = source_id(report)
    mapping = suggest_report_mapping(report)
    mapping[sid] = "indicator:cost_services_k"
    preview = plan_import(db, report, mapping)
    assert any(c["indicator"] == "cost_services_k" and c["value"] == 400 for c in preview["connections"])
    assert db.query(Company).count() == 0
    import_report(db, report, mapping=mapping, profile_name="Reviewed costs")
    sig = db.query(SignalRecord).filter_by(signal_key="cost_services_k").one()
    assert sig.numeric_value == 400
    field = json.loads(sig.raw_payload_ref)["raw_fields"][0]
    assert (field["column"], field["sheet"], field["cell"], field["date"], field["unit"]) == (
        sid, "Page 1", "U28", "2024-12-31", "migl EUR")
    rec = db.query(RawImportRecord).one()
    assert rec.mapping_snapshot[sid] == "indicator:cost_services_k"
    assert rec.raw_row[sid] == 400


def test_saved_profile_reuses_connections_for_another_company_and_year(db):
    report = extra_report()
    sid = source_id(report)
    mapping = suggest_report_mapping(report)
    mapping[sid] = "indicator:cost_services_k"
    import_report(db, report, mapping=mapping, profile_name="Reviewed costs")
    next_report = extra_report(second_company=True)
    assert source_id(next_report) == sid
    profile = load_report_profile(db, "Reviewed costs")
    suggested = suggest_report_mapping(next_report, profile)
    assert suggested[sid] == "indicator:cost_services_k"
    import_report(db, next_report, mapping=suggested, profile_name="Reviewed costs")
    assert db.query(Company).count() == 2
    assert db.query(ColumnMappingProfile).one().dataset_name == PROFILE_PREFIX + "Reviewed costs"
    assert db.query(SignalRecord).filter_by(signal_key="cost_services_k", numeric_value=400).count() == 2


def test_remapping_clears_old_connections_and_does_not_replace_people(db):
    report = extra_report()
    sid = source_id(report)
    mapping = suggest_report_mapping(report)
    mapping[sid] = "indicator:cost_services_k"
    import_report(db, report, mapping=mapping, profile_name="Reviewed costs")
    person_ids = [p.id for p in db.query(CompanyPerson).all()]
    mapping[sid] = None
    import_report(db, report, replace_report=True, remap_only=True, mapping=mapping, profile_name="Reviewed costs")
    sig = db.query(SignalRecord).filter_by(signal_key="cost_services_k").one()
    assert sig.status == "not_yet_checked" and sig.numeric_value is None
    assert [p.id for p in db.query(CompanyPerson).all()] == person_ids
    assert load_report_profile(db, "Reviewed costs")[sid] is None


def test_new_indicator_is_created_only_on_confirm_with_zero_weight(db):
    report = extra_report()
    sid = source_id(report)
    mapping = suggest_report_mapping(report)
    mapping[sid] = NEW_INDICATOR
    labels = {sid: "Reported advisory costs"}
    plan_import(db, report, mapping, labels)
    assert db.query(IndicatorDefinition).filter_by(key="reported_advisory_costs").count() == 0
    import_report(db, report, mapping=mapping, new_labels=labels, profile_name="Reviewed costs")
    definition = db.query(IndicatorDefinition).filter_by(key="reported_advisory_costs").one()
    assert definition.axis == "context" and definition.weight == 0
    assert db.query(SignalRecord).filter_by(signal_key=definition.key).one().numeric_value == 400
    assert load_report_profile(db, "Reviewed costs")[sid] == "indicator:reported_advisory_costs"


def test_new_indicator_and_profile_roll_back_with_failed_report_import(db):
    report = extra_report()
    sid = source_id(report)
    mapping = suggest_report_mapping(report)
    mapping[sid] = NEW_INDICATOR

    def fail(*args):
        raise RuntimeError("injected people failure")

    event.listen(CompanyPerson, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError):
            import_report(db, report, mapping=mapping, new_labels={sid: "Rollback custom cost"}, profile_name="Rollback")
    finally:
        event.remove(CompanyPerson, "before_insert", fail)
    assert db.query(IndicatorDefinition).filter_by(key="rollback_custom_cost").count() == 0
    assert db.query(ColumnMappingProfile).count() == 0
    assert db.query(Company).count() == 0


def test_financial_fact_connections_reject_wrong_years(db):
    report = extra_report()
    mapping = suggest_report_mapping(report)
    mapping[source_id(report, "Ricavi delle vendite", "y-2")] = "variable:revenue_latest"
    with pytest.raises(ValueError, match="actual period"):
        plan_import(db, report, mapping)


def test_direct_indicator_cannot_silently_take_several_years(db):
    report = extra_report()
    mapping = suggest_report_mapping(report)
    for period in ("latest", "y-1"):
        mapping[source_id(report, period=period)] = "indicator:cost_services_k"
    with pytest.raises(ValueError, match="one reporting date"):
        plan_import(db, report, mapping)


def test_older_period_can_be_connected_without_losing_actual_date(db):
    report = extra_report()
    mapping = suggest_report_mapping(report)
    mapping[source_id(report, "Ricavi delle vendite", "y-10")] = "variable:revenue_y-10"
    prepared = prepare_connections(db, report, mapping)
    assert prepared["facts"]["revenue_y-10"] == 4089
    assert prepared["lineage"]["revenue_y-10"][0]["date"] == "2014-12-31"


def test_ignoring_mapped_revenue_clears_derived_trends(db):
    report = extra_report()
    mapping = suggest_report_mapping(report)
    import_report(db, report, mapping=mapping)
    for sid, target in list(mapping.items()):
        if target and target.startswith("variable:revenue_"):
            mapping[sid] = None
    import_report(db, report, mapping=mapping, replace_report=True, remap_only=True)
    sig = db.query(SignalRecord).filter_by(signal_key="revenue_trend").one()
    assert sig.status == "not_yet_checked" and sig.numeric_value is None
    assert "revenue_latest" not in db.query(RawImportRecord).one().raw_row


def test_mapping_editor_applies_connection_from_saved_report(db):
    report = extra_report()
    import_report(db, report)
    record = db.query(RawImportRecord).one()
    # This editor test exercises filters, dropdown, apply, and the shared DB
    # write path; the source file is not needed again for remapping.
    app = AppTest.from_string("""
import streamlit as st
from views.aida_single_report import render_saved_mapping
render_saved_mapping(st.session_state['mapping_db'], st.session_state['record'])
""")
    app.session_state["mapping_db"] = db
    app.session_state["record"] = record
    # Use an in-memory Session on this thread too: pysqlite's default in-memory
    # connection cannot be shared with AppTest's script thread. Patch only the
    # UI callbacks; database integration is covered above with real sessions.
    sid = source_id(report)
    target_labels = report_targets(db, report)
    with patch("views.aida_single_report.load_report_profile", return_value={}), \
         patch("company_service.list_mapping_profiles", return_value=[]), \
         patch("views.aida_single_report.report_targets", return_value=target_labels), \
         patch("views.aida_single_report.plan_import", return_value={"warnings": [], "connections": []}), \
         patch("views.aida_single_report.import_report", return_value={"signals_updated": 1, "signals_skipped": []}) as apply:
        app.run(timeout=30)
        assert not app.exception
        app.text_input(key=f"aida_saved_map_{record.id}_search").set_value("B.7. Servizi").run()
        assert not app.exception
        picker = next(s for s in app.selectbox if s.label.startswith("Connect B.7. Servizi (2024"))
        picker.set_value("indicator:cost_services_k").run()
        app.button(key=f"aida_apply_mapping_{record.id}").click().run()
        assert not app.exception and app.success
        assert apply.call_args.kwargs["mapping"][sid] == "indicator:cost_services_k"
        assert apply.call_args.kwargs["remap_only"] is True


def test_explicit_row_units_override_the_generic_table_currency(db):
    report = extra_report()
    row = report["financial_tables"][0]["rows"][0]
    row["label"] = "Profitability (%)"
    sid = source_id(report, "Profitability (%)")
    assert report_sources(report)[sid]["unit"] == "%"
    mapping = suggest_report_mapping(report)
    mapping[sid] = "variable:cash_latest"
    with pytest.raises(ValueError, match="monetary unit"):
        prepare_connections(db, report, mapping)


def test_new_indicator_can_be_created_once_for_every_period_of_a_line():
    import aida_report_mapping as m
    from test_report_upload import report_file
    report = parse_report(report_file(), "aida.xlsx")
    sources = m.report_sources(report)
    first = next(sid for sid, s in sources.items() if s["period"] == "latest" and s["label"] == "EBITDA")
    mapping = {sid: None for sid in sources}
    mapping[first] = m.NEW_INDICATOR
    other = next(sid for sid in sources if m.line_stem(sid) == m.line_stem(first) and sources[sid]["period"] == "y-1")
    mapping2 = dict(mapping)
    mapping2[other] = "indicator:revenue_trend"  # an explicit choice must survive
    expanded, labels = m.expand_new_indicators(sources, mapping2, {first: "EBITDA margin"}, {first})
    periods = {sources[s]["period"] for s, t in expanded.items() if t == m.NEW_INDICATOR}
    assert {"latest", "y-2"} <= periods and "y-1" not in periods
    assert labels[first] == "EBITDA margin (latest)" and expanded[other] == "indicator:revenue_trend"
    # Not ticked: only the chosen source is created.
    single, _ = m.expand_new_indicators(sources, mapping, {first: "X"}, set())
    assert [s for s, t in single.items() if t == m.NEW_INDICATOR] == [first]


def test_whole_line_new_indicators_import_as_one_indicator_per_year(db):
    import aida_report_mapping as m
    report = parse_report(report_file(), "aida.xlsx")
    sources = m.report_sources(report)
    first = next(sid for sid, s in sources.items() if s["period"] == "latest" and s["label"] == "Dipendenti")
    mapping = {sid: None for sid in sources}
    mapping[first] = m.NEW_INDICATOR
    mapping, labels = m.expand_new_indicators(sources, mapping, {first: "Headcount note"}, {first})
    import_report(db, report, mapping=mapping, new_labels=labels)
    keys = {s.signal_key for s in db.query(SignalRecord).filter_by(status="present")}
    assert {"headcount_note_(latest)", "headcount_note_(y-1)", "headcount_note_(y-2)"} <= keys
