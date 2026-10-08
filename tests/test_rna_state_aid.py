"""
adapters/rna_state_aid.py — no network here: requests.get is stubbed, and parse_month is
exercised against a small hand-written fixture using RNA's real schema (captured live
2026-10-08 against a real October-2026 OpenData_Aiuti file, including the nested
COMPONENTI_AIUTO/STRUMENTI_AIUTO structure that carries the grant amount).
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from adapters import rna_state_aid
from models import Base, Company

NS = "http://www.rna.it/RNA_aiuto/schema"

SAMPLE_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<LISTA_AIUTI xmlns="{NS}">
    <AIUTO>
        <TITOLO_MISURA>Nuova Sabatini - macchinari PMI</TITOLO_MISURA>
        <DES_TIPO_MISURA>Regime di aiuti</DES_TIPO_MISURA>
        <COD_CE_MISURA>SA.47180</COD_CE_MISURA>
        <SOGGETTO_CONCEDENTE>Ministero delle Imprese e del Made in Italy</SOGGETTO_CONCEDENTE>
        <DATA_CONCESSIONE>2026-03-15</DATA_CONCESSIONE>
        <DENOMINAZIONE_BENEFICIARIO>ACME MACCHINE SRL</DENOMINAZIONE_BENEFICIARIO>
        <CODICE_FISCALE_BENEFICIARIO>12345678901</CODICE_FISCALE_BENEFICIARIO>
        <COMPONENTI_AIUTO>
            <COMPONENTE_AIUTO>
                <STRUMENTI_AIUTO>
                    <STRUMENTO_AIUTO>
                        <ELEMENTO_DI_AIUTO>5000.00</ELEMENTO_DI_AIUTO>
                        <IMPORTO_NOMINALE>50000.00</IMPORTO_NOMINALE>
                    </STRUMENTO_AIUTO>
                    <STRUMENTO_AIUTO>
                        <ELEMENTO_DI_AIUTO>1000.00</ELEMENTO_DI_AIUTO>
                        <IMPORTO_NOMINALE>10000.00</IMPORTO_NOMINALE>
                    </STRUMENTO_AIUTO>
                </STRUMENTI_AIUTO>
            </COMPONENTE_AIUTO>
        </COMPONENTI_AIUTO>
    </AIUTO>
    <AIUTO>
        <TITOLO_MISURA>Aiuti per gli investimenti digitali</TITOLO_MISURA>
        <DES_TIPO_MISURA>Regime di aiuti</DES_TIPO_MISURA>
        <COD_CE_MISURA>SA.99999</COD_CE_MISURA>
        <SOGGETTO_CONCEDENTE>Regione Esempio</SOGGETTO_CONCEDENTE>
        <DATA_CONCESSIONE>2026-05-01</DATA_CONCESSIONE>
        <DENOMINAZIONE_BENEFICIARIO>ACME MACCHINE SRL</DENOMINAZIONE_BENEFICIARIO>
        <CODICE_FISCALE_BENEFICIARIO>12345678901</CODICE_FISCALE_BENEFICIARIO>
    </AIUTO>
    <AIUTO>
        <TITOLO_MISURA>Altro regime</TITOLO_MISURA>
        <DATA_CONCESSIONE>2026-01-01</DATA_CONCESSIONE>
        <DENOMINAZIONE_BENEFICIARIO>ALTRA IMPRESA SPA</DENOMINAZIONE_BENEFICIARIO>
        <CODICE_FISCALE_BENEFICIARIO>99988877766</CODICE_FISCALE_BENEFICIARIO>
    </AIUTO>
</LISTA_AIUTI>
"""


@pytest.fixture(autouse=True)
def reset_cache():
    rna_state_aid.reset_cache_for_tests()
    yield
    rna_state_aid.reset_cache_for_tests()


@pytest.fixture
def db():
    # StaticPool (one shared connection for the whole engine, not one per thread): sync_
    # italian_state_aid goes through run_adapter's hard-timeout wrapper, which runs
    # fetch_live on a worker thread — the default SingletonThreadPool would hand that
    # thread a second, empty in-memory database instead of this one.
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_month_sequence_rolls_over_the_year_boundary():
    seq = rna_state_aid._month_sequence(3, now=datetime(2026, 1, 15))
    assert seq == [(2026, 1), (2025, 12), (2025, 11)]


def test_parse_month_sums_multi_instrument_amounts_and_groups_by_company(tmp_path):
    path = tmp_path / "sample.xml"
    path.write_text(SAMPLE_XML, encoding="utf-8")
    records = rna_state_aid.parse_month(path)
    assert len(records) == 3
    acme = [r for r in records if r["CODICE_FISCALE_BENEFICIARIO"] == "12345678901"]
    assert len(acme) == 2
    sabatini = next(r for r in acme if "Sabatini" in r["TITOLO_MISURA"])
    assert sabatini["TOTAL_IMPORTO_NOMINALE"] == 60000.0
    digitale = next(r for r in acme if "digitali" in r["TITOLO_MISURA"])
    assert "TOTAL_IMPORTO_NOMINALE" not in digitale  # no STRUMENTO_AIUTO at all in this one


def test_has_matchable_piva():
    italy_piva = Company(legal_name="X", registration_number="12345678901", country="Italy")
    italy_rea = Company(legal_name="X", registration_number="REA MI-123456", country="Italy")
    germany = Company(legal_name="X", registration_number="HRB 12345", country="Germany")
    assert rna_state_aid.has_matchable_piva(italy_piva) is True
    assert rna_state_aid.has_matchable_piva(italy_rea) is False
    assert rna_state_aid.has_matchable_piva(germany) is False


def test_sync_skips_non_italian_and_rea_only_companies(db):
    germany = Company(legal_name="X GmbH", registration_number="HRB 12345", country="Germany")
    rea_only = Company(legal_name="Y SRL", registration_number="REA MI-123456", country="Italy")
    db.add_all([germany, rea_only])
    db.commit()
    assert rna_state_aid.sync_italian_state_aid(germany, db)["status"] == "skipped"
    assert rna_state_aid.sync_italian_state_aid(rea_only, db)["status"] == "skipped"


def test_sync_finds_grants_for_a_matching_company(db, monkeypatch, tmp_path):
    path = tmp_path / "sample.xml"
    path.write_text(SAMPLE_XML, encoding="utf-8")
    now = datetime.utcnow()
    # Only the current month "has" data in this fake lookback — every other month in the
    # 13-month window returns None, same as a real gap/outage would, so the index isn't
    # fooled into counting the same fixture's 2 records once per month scanned.
    monkeypatch.setattr(rna_state_aid, "download_month",
                        lambda year, month: path if (year, month) == (now.year, now.month) else None)

    company = Company(legal_name="Acme Macchine Srl", registration_number="IT12345678901", country="Italy")
    db.add(company)
    db.commit()

    result = rna_state_aid.sync_italian_state_aid(company, db)
    assert result["status"] == "success"
    from models import SignalRecord
    sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="public_grant_count").one()
    assert sig.numeric_value == 2.0
    assert sig.status == "present"
    assert sig.is_simulated is False
    evidence = __import__("json").loads(sig.raw_payload_ref)["evidence"]
    assert len(evidence["grants"]) == 2
    assert evidence["codice_fiscale_matched"] == "12345678901"


def test_sync_confirms_a_genuine_zero_for_an_unmatched_company(db, monkeypatch, tmp_path):
    path = tmp_path / "sample.xml"
    path.write_text(SAMPLE_XML, encoding="utf-8")
    now = datetime.utcnow()
    monkeypatch.setattr(rna_state_aid, "download_month",
                        lambda year, month: path if (year, month) == (now.year, now.month) else None)

    company = Company(legal_name="Nobody Srl", registration_number="11122233344", country="Italy")
    db.add(company)
    db.commit()

    from models import SignalRecord
    rna_state_aid.sync_italian_state_aid(company, db)
    sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="public_grant_count").one()
    assert sig.numeric_value == 0.0
    assert sig.status == "absent"


def test_index_is_cached_across_calls_within_the_ttl(monkeypatch, tmp_path):
    path = tmp_path / "sample.xml"
    path.write_text(SAMPLE_XML, encoding="utf-8")
    calls = []
    monkeypatch.setattr(rna_state_aid, "download_month", lambda year, month: (calls.append((year, month)), path)[1])

    rna_state_aid.get_index()
    first_call_count = len(calls)
    rna_state_aid.get_index()  # should reuse the cached index, no new downloads
    assert len(calls) == first_call_count


def test_download_month_skips_a_fresh_cached_file(tmp_path, monkeypatch):
    monkeypatch.setattr(rna_state_aid, "CACHE_DIR", tmp_path)
    cache_file = rna_state_aid._cache_path(2026, 1)
    cache_file.write_text(SAMPLE_XML, encoding="utf-8")

    called = []
    monkeypatch.setattr(requests, "get", lambda *a, **k: called.append(1) or pytest.fail("should not re-download"))
    result = rna_state_aid.download_month(2026, 1)
    assert result == cache_file
    assert called == []


def test_download_month_refetches_a_stale_current_month(tmp_path, monkeypatch):
    monkeypatch.setattr(rna_state_aid, "CACHE_DIR", tmp_path)
    now = datetime.utcnow()
    cache_file = rna_state_aid._cache_path(now.year, now.month)
    cache_file.write_text("old content", encoding="utf-8")
    import os
    old_time = (now - timedelta(days=10)).timestamp()
    os.utime(cache_file, (old_time, old_time))

    class _Resp:
        status_code = 200
        content = SAMPLE_XML.encode()

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    result = rna_state_aid.download_month(now.year, now.month)
    assert result.read_text(encoding="utf-8") == SAMPLE_XML


def test_download_month_returns_none_for_a_future_month_with_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(rna_state_aid, "CACHE_DIR", tmp_path)

    class _Resp:
        status_code = 404
        content = b""

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    assert rna_state_aid.download_month(2099, 1) is None
