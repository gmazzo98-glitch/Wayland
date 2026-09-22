"""
company_service.companies_not_yet_crawled / phase_source_names — the default filter behind
the Pipeline Health bulk triggers, so re-clicking "Run Phase N" advances through untouched
companies instead of re-spending API/LLM budget on ones already covered.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Company, SignalRecord
from company_service import companies_not_yet_crawled, phase_source_names, COUNTRY_SOURCE_MAP


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _mk(db, **kw):
    c = Company(legal_name=kw.pop("legal_name", "X SRL"), registration_number=kw.pop("registration_number", "R1"), **kw)
    db.add(c)
    db.commit()
    return c


def _signal(db, company_id, source, is_simulated, key="patent_count"):
    db.add(SignalRecord(company_id=company_id, signal_key=key, source=source,
                          numeric_value=1.0, status="present", is_simulated=is_simulated))
    db.commit()


def test_phase_source_names_matches_country_source_map():
    assert phase_source_names("Italy", 1) == COUNTRY_SOURCE_MAP["Italy"]["Phase 1"]
    assert phase_source_names("Germany", 1) == COUNTRY_SOURCE_MAP["Germany"]["Phase 1"]
    # A phase with nothing for this country (e.g. Phase 2 for Italy) returns an empty list,
    # never falls back to Germany's.
    assert phase_source_names("Italy", 2) == []


def test_company_with_no_signals_is_not_yet_crawled(db):
    c = _mk(db, country="Italy")
    assert companies_not_yet_crawled(db, [c], phase=1) == [c]


def test_company_with_a_real_signal_from_the_phase_is_skipped(db):
    c = _mk(db, country="Italy")
    _signal(db, c.id, "EPO OPS", is_simulated=False)  # EPO OPS is a Phase 1 source for Italy
    assert companies_not_yet_crawled(db, [c], phase=1) == []


def test_a_simulated_only_attempt_still_counts_as_not_yet_crawled(db):
    """A company whose only signal is simulated (credentials missing, nothing found, a
    transient failure) must be retried, not skipped forever."""
    c = _mk(db, country="Italy")
    _signal(db, c.id, "EPO OPS", is_simulated=True)
    assert companies_not_yet_crawled(db, [c], phase=1) == [c]


def test_a_signal_from_an_unrelated_phase_does_not_count(db):
    c = _mk(db, country="Italy")
    _signal(db, c.id, "Wappalyzer", is_simulated=False)  # a Phase 4 source, not Phase 1
    assert companies_not_yet_crawled(db, [c], phase=1) == [c]


def test_country_specific_sources_are_respected(db):
    """Arbeitsagentur is only ever a Phase 1 source for Germany (COUNTRY_SOURCE_MAP) — a real
    signal recorded under that name must not count as "done" for an Italian company, even
    though it would for a German one."""
    it = _mk(db, country="Italy", legal_name="IT SRL", registration_number="R-IT")
    de = _mk(db, country="Germany", legal_name="DE GmbH", registration_number="R-DE")
    _signal(db, it.id, "Arbeitsagentur", is_simulated=False)
    _signal(db, de.id, "Arbeitsagentur", is_simulated=False)
    todo = companies_not_yet_crawled(db, [it, de], phase=1)
    assert it in todo  # Arbeitsagentur isn't one of Italy's Phase 1 sources — doesn't count
    assert de not in todo


def test_mixed_batch_only_returns_the_ones_still_needing_the_phase(db):
    done = _mk(db, country="Italy", legal_name="Done SRL", registration_number="R-D")
    todo = _mk(db, country="Italy", legal_name="Todo SRL", registration_number="R-T")
    _signal(db, done.id, "EUIPO", is_simulated=False)
    result = companies_not_yet_crawled(db, [done, todo], phase=1)
    assert result == [todo]


def test_default_country_is_germany_when_unset(db):
    """No country recorded -> treated as Germany (matches get_applicable_sources_for_company's
    own default), so a real Eurostat Export Exposure signal (a Phase 1 source there too) is
    enough on its own to mark the company done for phase 1."""
    c = _mk(db, country=None)
    _signal(db, c.id, "Eurostat Export Exposure", is_simulated=False)
    assert companies_not_yet_crawled(db, [c], phase=1) == []
