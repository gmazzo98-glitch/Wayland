"""
Tests for scrapers/competitor_benchmark.py's compute_gap() — the pure gap-math/evidence-shaping
function, kept separate from the DB/subprocess orchestration in sync_competitor_benchmark() so
it's directly testable without a database or a real Node crawler. The orchestration layer's own
"skip when nothing is recorded yet" behavior is covered by test_sync_skips_without_competitors
and test_sync_skips_without_own_signal, using a throwaway SQLite session like the other Phase 7
wrapper tests in tests/test_crawler_scrapers.py.
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Company, Competitor, SignalRecord
from scrapers import competitor_benchmark


def _competitor(name, homepage_url="https://example.com"):
    return Competitor(id=name, name=name, homepage_url=homepage_url)


def _dated_row(score):
    """A digital-maturity-crawler row with no Wayback coverage but a usable vision read —
    exercises the same vision-fallback path tests/test_crawler_scrapers.py already covers for
    the main wrapper, here just as a convenient way to get a non-zero maturity value."""
    return {
        "last_major_redesign_estimate": {"estimated_year": None, "comparisons": []},
        "has_ecommerce": None, "social_presence_links": [], "snapshot_count_last_5_years": None,
        "visual_assessment": {
            "design_modernity_score": score, "design_modernity_reasoning": "x",
            "chatbot_or_ai_assistant_present": False, "personalization_signals_present": False,
            "personalization_evidence": None, "ecommerce_ux_quality": "not_applicable", "summary": "x",
        },
        "field_status": {"last_major_redesign_estimate": "not_found", "has_ecommerce": "not_found",
                          "social_presence_links": "not_found", "visual_assessment": "value"},
    }


def test_compute_gap_averages_usable_competitors():
    own_value = 7.0  # years since this company's own last redesign
    results = [
        {"competitor": _competitor("Alpha"), "row": _dated_row(4), "error": None},  # -> 2.0y
        {"competitor": _competitor("Beta"), "row": _dated_row(5), "error": None},   # -> 0.5y
    ]
    signals = competitor_benchmark.compute_gap(own_value, results)
    gap = signals["competitor_digital_gap"]
    assert gap["value"] == pytest.approx(7.0 - (2.0 + 0.5) / 2)
    assert gap["status"] == "present"
    benchmarked = gap["evidence"]["competitors_benchmarked"]
    assert {b["name"] for b in benchmarked} == {"Alpha", "Beta"}
    assert gap["evidence"]["own_website_digital_maturity"] == 7.0
    assert gap["evidence"]["competitors_skipped"] == []


def test_compute_gap_skips_failed_or_uncovered_competitors_but_still_uses_the_rest():
    own_value = 5.0
    results = [
        {"competitor": _competitor("Alpha"), "row": None, "error": "digital-maturity-crawler timed out"},
        {"competitor": _competitor("Beta"), "row": _dated_row(3), "error": None},  # -> 4.0y
    ]
    signals = competitor_benchmark.compute_gap(own_value, results)
    gap = signals["competitor_digital_gap"]
    assert gap["value"] == pytest.approx(5.0 - 4.0)
    assert len(gap["evidence"]["competitors_benchmarked"]) == 1
    skipped = gap["evidence"]["competitors_skipped"]
    assert len(skipped) == 1 and skipped[0]["name"] == "Alpha"
    assert "timed out" in skipped[0]["reason"]


def test_compute_gap_returns_nothing_when_no_competitor_is_usable():
    results = [
        {"competitor": _competitor("Alpha"), "row": None, "error": "network error"},
        {"competitor": _competitor("Beta"), "row": {
            "last_major_redesign_estimate": {"estimated_year": None, "comparisons": []},
            "has_ecommerce": None, "social_presence_links": [], "snapshot_count_last_5_years": None,
            "field_status": {"last_major_redesign_estimate": "not_found", "has_ecommerce": "not_found",
                              "social_presence_links": "not_found", "visual_assessment": "not_found"},
        }, "error": None},
    ]
    assert competitor_benchmark.compute_gap(5.0, results) == {}


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_sync_skips_without_competitors(db):
    c = Company(legal_name="Acme GmbH", registration_number="R-1", website_url="https://acme.example")
    db.add(c)
    db.commit()
    result = competitor_benchmark.sync_competitor_benchmark(c, db)
    assert result["status"] == "skipped"
    assert "no named competitor" in result["reason"].lower() or "no competitor" in result["reason"].lower()


def test_sync_skips_without_own_signal_even_with_competitors_recorded(db):
    c = Company(legal_name="Acme GmbH", registration_number="R-2", website_url="https://acme.example")
    db.add(c)
    db.commit()
    db.add(Competitor(company_id=c.id, name="Rival Co", homepage_url="https://rival.example"))
    db.commit()
    result = competitor_benchmark.sync_competitor_benchmark(c, db)
    assert result["status"] == "skipped"
    assert "website_digital_maturity" in result["reason"]


def test_sync_skips_when_own_signal_is_only_simulated(db):
    """A simulated own value is exactly the 'not actually measured' case this gate exists for —
    never benchmark competitors against a guess."""
    c = Company(legal_name="Acme GmbH", registration_number="R-3", website_url="https://acme.example")
    db.add(c)
    db.commit()
    db.add(Competitor(company_id=c.id, name="Rival Co", homepage_url="https://rival.example"))
    db.add(SignalRecord(company_id=c.id, signal_key="website_digital_maturity", source="Digital Maturity Crawler",
                        numeric_value=4.0, status="present", is_simulated=True, fetched_at=datetime.utcnow()))
    db.commit()
    result = competitor_benchmark.sync_competitor_benchmark(c, db)
    assert result["status"] == "skipped"


def test_competitor_cap_is_three():
    assert competitor_benchmark.MAX_COMPETITORS == 3
