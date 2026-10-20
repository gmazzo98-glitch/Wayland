"""The importer for Scraper/tools/site_evidence results: mapping, honesty and the one-row-per-key guard."""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Company, RawImportRecord, SignalRecord, SourceHealth
from scrapers import site_evidence_reader as ser


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _company(db, cid="c1", reg="IT001", name="ACME SRL"):
    c = Company(id=cid, legal_name=name, registration_number=reg)
    db.add(c)
    db.commit()
    return c


def _hit(claim="university_partnership", via="live", **kw):
    base = {"company_id": "IT001", "claim": claim, "indicator": ser.CLAIM_TO_SIGNAL.get(claim, "none"),
            "quote": "Collaboriamo con il Politecnico di Torino su progetti di ricerca.",
            "page_url": "https://acme.it/partner", "page_title": "Partner", "via": via, "snapshot_ts": None,
            "found_at": "2026-10-20T12:00:00+00:00", "model": "qwen2.5:3b", "num_ctx": 4096,
            "institution_hint": "Politecnico di Torino", "rules_version": ser.MIN_RULES_VERSION}
    base.update(kw)
    return base


# ---- mapping ---------------------------------------------------------------------------------
def test_each_claim_maps_to_its_existing_indicator_with_the_right_value():
    hits = [
        _hit("university_partnership"),
        _hit("external_collaboration", partner_hint="Linde"),
        _hit("open_innovation", program_hint="premio innovazione Smau"),
        _hit("export_share_pct", value=70.0),
        _hit("export_since_year", value=1998.0),
    ]
    sig = ser._derive_signals(hits, year_now=2026)
    assert set(sig) == {"university_partnership", "external_collaboration", "prior_open_innovation_usage",
                        "international_sales_volume", "years_international_activity"}
    assert sig["university_partnership"]["value"] == 1.0
    assert sig["international_sales_volume"]["value"] == 70.0
    assert sig["years_international_activity"]["value"] == 28.0       # 2026 - 1998, same as the website crawler
    assert all(s["status"] == "present" for s in sig.values())


def test_every_signal_carries_the_quote_the_page_and_the_model():
    s = ser._derive_signals([_hit()], year_now=2026)["university_partnership"]
    assert "Politecnico di Torino" in s["summary"]
    ev = s["evidence"]
    assert ev["quote"].startswith("Collaboriamo") and ev["source_urls"] == ["https://acme.it/partner"]
    assert ev["model"] == "qwen2.5:3b" and ev["page_read"] == "live"


def test_archived_reads_get_lower_confidence_and_say_so():
    live = ser._derive_signals([_hit(via="live")])["university_partnership"]
    arch = ser._derive_signals([_hit(via="wayback", snapshot_ts="20240315000000")])["university_partnership"]
    assert live["confidence"] == ser.CONFIDENCE_LIVE > arch["confidence"] == ser.CONFIDENCE_ARCHIVE
    assert "archived copy of 2024-03-15" in arch["summary"]
    assert arch["evidence"]["page_read"].startswith("archived")


def test_a_live_hit_beats_an_archived_one_and_dating_is_recorded_as_evidence():
    hits = [_hit(via="wayback", snapshot_ts="20200101000000", quote="Vecchia frase del Politecnico di Torino collaboriamo."),
            _hit(via="live", first_seen={"status": "ok", "first_seen_date": "2017-06-01", "replay_url": "https://web.archive.org/x",
                                         "note": "earliest archived snapshot containing this exact text", "earliest_archived": "2004-01-01"})]
    s = ser._derive_signals(hits)["university_partnership"]
    assert s["evidence"]["page_read"] == "live" and s["evidence"]["first_seen_in_archive"]["date"] == "2017-06-01"
    assert "archived since 2017-06-01" in s["summary"]


def test_unusable_hits_are_dropped_not_guessed():
    assert ser._derive_signals([_hit(quote="  ")]) == {}
    assert ser._derive_signals([_hit("export_share_pct")]) == {}                     # no value
    assert ser._derive_signals([_hit("export_since_year", value=2040.0)], year_now=2026) == {}   # future year
    assert ser._derive_signals([_hit("unknown_claim")]) == {}


def test_findings_from_before_the_current_rules_are_never_imported():
    old = _hit(rules_version=ser.MIN_RULES_VERSION - 1)
    unstamped = {k: v for k, v in _hit().items() if k != "rules_version"}
    assert ser._derive_signals([old]) == {} and ser._derive_signals([unstamped]) == {}
    assert ser._derive_signals([_hit()])


# ---- the one-row-per-key guard ---------------------------------------------------------------
def _signal(db, source, status="present", simulated=False, key="university_partnership", cid="c1", value=1.0):
    db.add(SignalRecord(company_id=cid, signal_key=key, source=source, status=status, is_simulated=simulated,
                        numeric_value=value))
    db.commit()


def test_decide_never_overwrites_real_present_or_absent_from_another_source(db):
    _company(db)
    assert ser.decide(None)[0] == "create"
    _signal(db, "Google News RSS")
    sig = db.query(SignalRecord).one()
    action, reason = ser.decide(sig)
    assert action == "skip" and "Google News RSS" in reason
    sig.status = "absent"
    assert ser.decide(sig)[0] == "skip" and "conflict" in ser.decide(sig)[1]
    sig.status, sig.is_simulated = "not_yet_checked", True
    assert ser.decide(sig)[0] == "replace"
    sig.source, sig.is_simulated, sig.status = ser.SOURCE_NAME, False, "present"
    assert ser.decide(sig)[0] == "update"


def test_plan_and_apply_write_signals_blob_and_health_and_are_idempotent(db):
    _company(db)
    _signal(db, "News/Press", status="not_yet_checked", simulated=True, key="external_collaboration", value=None)
    hits = {"IT001": [_hit("university_partnership"), _hit("external_collaboration", partner_hint="Linde")]}
    state = {"IT001": {"status": "done", "pages_read": 7}}

    plan = ser.build_plan(db, hits, year_now=2026)
    assert {w["signal_key"]: w["action"] for w in plan["writes"]} == {
        "university_partnership": "create", "external_collaboration": "replace"}
    assert plan["conflicts"] == [] and plan["unmatched"] == []
    done = ser.apply_plan(db, plan, hits, state)
    assert done == {"signals_written": 2, "companies": 1}

    rows = {r.signal_key: r for r in db.query(SignalRecord).all()}
    assert rows["university_partnership"].source == ser.SOURCE_NAME and rows["university_partnership"].is_simulated is False
    assert rows["external_collaboration"].status == "present" and rows["external_collaboration"].numeric_value == 1.0
    payload = json.loads(rows["university_partnership"].raw_payload_ref)
    assert payload["evidence"]["quote"].startswith("Collaboriamo")
    blob = db.query(RawImportRecord).filter_by(company_id="c1", dataset_name=ser.DATASET_NAME).one()
    assert blob.raw_row["coverage"]["pages_read"] == 7 and len(blob.raw_row["hits"]) == 2
    assert db.query(SourceHealth).filter_by(source_name=ser.SOURCE_NAME).one().last_status == "success"

    again = ser.build_plan(db, hits, year_now=2026)           # second import: updates, creates nothing new
    assert {w["action"] for w in again["writes"]} == {"update"}


def test_a_real_signal_from_another_source_is_left_alone_and_reported(db):
    _company(db)
    _signal(db, "Google News RSS")                              # real 'present' university_partnership
    plan = ser.build_plan(db, {"IT001": [_hit("university_partnership")]})
    assert plan["writes"] == [] and len(plan["conflicts"]) == 1
    assert "Google News RSS" in plan["conflicts"][0]["reason"]
    assert ser.apply_plan(db, plan, {}, {}) == {"signals_written": 0, "companies": 0}
    assert db.query(SignalRecord).one().source == "Google News RSS"


def test_unknown_companies_are_reported_not_created(db):
    plan = ser.build_plan(db, {"IT999": [_hit()]})
    assert plan["unmatched"] == ["IT999"] and plan["writes"] == []
    assert db.query(Company).count() == 0


# ---- reading the tool's folder ----------------------------------------------------------------
def test_read_results_survives_a_torn_line_and_a_corrupt_state_file(tmp_path):
    good = json.dumps(_hit())
    (tmp_path / "results.jsonl").write_text(good + "\n" + '{"company_id": "IT001", "cla', encoding="utf-8")
    (tmp_path / "state.json").write_text('{"IT001": {"status": "do', encoding="utf-8")
    (tmp_path / "state.json.bak").write_text(json.dumps({"IT001": {"status": "done"}}), encoding="utf-8")
    hits, state = ser.read_results(tmp_path)
    assert len(hits["IT001"]) == 1 and state["IT001"]["status"] == "done"
    assert ser.read_results(tmp_path / "missing") == ({}, {})
