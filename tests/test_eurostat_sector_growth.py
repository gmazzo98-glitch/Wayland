"""
Tests for the Eurostat sector-growth adapter. No network: _fetch_index_series
is monkeypatched with values shaped like a real sts_inpr_m response, matching
the no-network style test_google_news_rss.py uses for the same reason.
"""

import pytest

from adapters import eurostat_sector_growth as esg


class _Co:
    def __init__(self, nace_code, country="Germany"):
        self.id, self.nace_code, self.country = "cid", nace_code, country


# ------------------------------------------------------------------ NACE section matching

@pytest.mark.parametrize("nace_code,expected", [
    ("C10.51", "C10"),
    ("B05.10", "B05"),
    ("D35.11", "D35"),
    ("E36.00", "E36"),
    ("284900", "C28"),
    ("282992", "C28"),
    ("351100", "D35"),
    ("051000", "B05"),
    ("360000", "E36"),
])
def test_sector_code_extracts_covered_sections(nace_code, expected):
    assert esg._sector_code(nace_code) == expected


@pytest.mark.parametrize("nace_code", ["A01.11", "G45.11", "J62.01", "011100", "451100", "341000", "", None])
def test_sector_code_rejects_uncovered_sections(nace_code):
    assert esg._sector_code(nace_code) is None


# ------------------------------------------------------------------ growth computation

def _series(values, start_year=2024, start_month=1):
    out, y, m = [], start_year, start_month
    for v in values:
        out.append((f"{y:04d}-{m:02d}", v))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def test_trailing_yoy_growth_positive():
    # Prior 12 months average 100, latest 12 months average 110 -> +10%.
    series = _series([100.0] * 12 + [110.0] * 12)
    assert esg._trailing_yoy_growth(series) == pytest.approx(0.10)


def test_trailing_yoy_growth_negative():
    series = _series([100.0] * 12 + [90.0] * 12)
    assert esg._trailing_yoy_growth(series) == pytest.approx(-0.10)


def test_trailing_yoy_growth_requires_24_months():
    with pytest.raises(RuntimeError, match="Need at least 24 months"):
        esg._trailing_yoy_growth(_series([100.0] * 12))


def test_trailing_yoy_growth_rejects_zero_base():
    with pytest.raises(RuntimeError, match="zero"):
        esg._trailing_yoy_growth(_series([0.0] * 12 + [10.0] * 12))


# ------------------------------------------------------------------ live fetch orchestration

def test_fetch_live_covered_sector(monkeypatch):
    monkeypatch.setattr(esg, "_fetch_index_series", lambda nace_r2, geo: _series([100.0] * 12 + [105.0] * 12))
    result = esg._fetch_live(_Co("C10.51", "Germany"))
    sig = result["signals"]["sector_growth_benchmark"]
    assert sig["status"] == "present"
    assert sig["value"] == pytest.approx(5.0)
    assert result["raw_payload"]["nace_r2"] == "C10"
    assert result["raw_payload"]["geo"] == "DE"


def test_fetch_live_uncovered_sector_raises():
    # Agriculture (section A) isn't in sts_inpr_m's coverage — must fail
    # honestly rather than matching against an unrelated industrial index.
    with pytest.raises(RuntimeError, match="outside"):
        esg._fetch_live(_Co("A01.11", "Germany"))


def test_fetch_live_uses_italy_geo(monkeypatch):
    captured = {}

    def _stub(nace_r2, geo):
        captured["geo"] = geo
        return _series([100.0] * 24)

    monkeypatch.setattr(esg, "_fetch_index_series", _stub)
    esg._fetch_live(_Co("C10.51", "Italy"))
    assert captured["geo"] == "IT"


def test_simulate_is_clearly_tagged_not_yet_checked():
    result = esg._simulate(_Co("A01.11", "Germany"))
    sig = result["signals"]["sector_growth_benchmark"]
    assert sig["status"] == "not_yet_checked"
    assert sig["value"] is None
    assert result["confidence"] == 0.0
