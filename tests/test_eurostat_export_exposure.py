"""
Tests for the Eurostat export-exposure adapter. No network: _fetch_export_value_meur
and _fetch_turnover_meur are monkeypatched with values shaped like the real
ext_tec01/sbs_ovw_act responses this was verified live against on 2026-09-22
(see the module docstring), matching test_eurostat_sector_growth.py's style.
"""

import pytest

from adapters import eurostat_export_exposure as eee


class _Co:
    def __init__(self, nace_code, country="Italy"):
        self.id, self.nace_code, self.country = "cid", nace_code, country


# ------------------------------------------------------------------ _latest_value

def test_latest_value_picks_the_most_recent_period_with_data():
    payload = {
        "value": {"0": 60.0, "1": 61.0, "2": 79.7},
        "dimension": {"time": {"category": {"index": {"2022": 0, "2023": 1, "2024": 2}}}},
    }
    assert eee._latest_value(payload) == ("2024", 79.7)


def test_latest_value_skips_a_missing_trailing_year():
    # 2024 has no observation yet (still provisional) — must fall back to 2023, not None.
    payload = {
        "value": {"0": 60.0, "1": 61.0},
        "dimension": {"time": {"category": {"index": {"2022": 0, "2023": 1, "2024": 2}}}},
    }
    assert eee._latest_value(payload) == ("2023", 61.0)


def test_latest_value_none_when_the_whole_series_is_empty():
    payload = {"value": {}, "dimension": {"time": {"category": {"index": {"2023": 0}}}}}
    assert eee._latest_value(payload) is None


# ------------------------------------------------------------------ live fetch orchestration

def test_fetch_live_computes_the_export_to_turnover_ratio(monkeypatch):
    # Real 2026-09-22 live values for Italy/NACE C28: 79,744.99 MEUR exports / 148,508.53 MEUR turnover.
    monkeypatch.setattr(eee, "_fetch_export_value_meur", lambda nace_r2, geo: ("2024", 79744.99))
    monkeypatch.setattr(eee, "_fetch_turnover_meur", lambda nace_r2, geo, period: 148508.53)
    result = eee._fetch_live(_Co("C28.11", "Italy"))
    sig = result["signals"]["sector_export_exposure"]
    assert sig["status"] == "present"
    assert sig["value"] == pytest.approx(79744.99 / 148508.53)
    assert 0.3 < sig["value"] < 0.9  # inside the catalog's own raw bound for this genuinely export-heavy sector
    assert result["raw_payload"]["nace_r2"] == "C28"
    assert result["raw_payload"]["geo"] == "IT"
    assert sig["evidence"]["period"] == "2024"


def test_fetch_live_uses_germany_geo(monkeypatch):
    captured = {}

    def _stub_export(nace_r2, geo):
        captured["export_geo"] = geo
        return "2023", 180535.86

    def _stub_turnover(nace_r2, geo, period):
        captured["turnover_geo"] = geo
        return 300000.0

    monkeypatch.setattr(eee, "_fetch_export_value_meur", _stub_export)
    monkeypatch.setattr(eee, "_fetch_turnover_meur", _stub_turnover)
    eee._fetch_live(_Co("C28.11", "Germany"))
    assert captured["export_geo"] == captured["turnover_geo"] == "DE"


def test_fetch_live_uncovered_sector_raises():
    # Same B/C/D/E restriction eurostat_sector_growth.py already validated live.
    with pytest.raises(RuntimeError, match="outside"):
        eee._fetch_live(_Co("A01.11", "Italy"))


def test_fetch_live_raises_when_export_series_is_empty(monkeypatch):
    def _stub(nace_r2, geo):
        raise RuntimeError("ext_tec01 returned no export value for nace_r2=C28 geo=IT")
    monkeypatch.setattr(eee, "_fetch_export_value_meur", _stub)
    with pytest.raises(RuntimeError, match="ext_tec01 returned no export value"):
        eee._fetch_live(_Co("C28.11", "Italy"))


def test_fetch_live_raises_on_zero_turnover(monkeypatch):
    monkeypatch.setattr(eee, "_fetch_export_value_meur", lambda nace_r2, geo: ("2024", 79744.99))
    monkeypatch.setattr(eee, "_fetch_turnover_meur", lambda nace_r2, geo, period: 0.0)
    with pytest.raises(RuntimeError, match="turnover is zero"):
        eee._fetch_live(_Co("C28.11", "Italy"))


def test_simulate_writes_no_signal_at_all():
    """Unlike the older eurostat_sector_growth.py sibling, simulate() here writes nothing —
    see the module's own comment for why an explicit not_yet_checked write is the wrong pattern."""
    result = eee._simulate(_Co("A01.11", "Italy"))
    assert result["signals"] == {}
    assert result["confidence"] == 0.5
