"""FDA records are company-specific regulatory evidence, never proof of compliance."""

from types import SimpleNamespace

from scrapers import fda_recalls_crawler as fda


def test_only_relevant_sectors_are_requested():
    assert fda.relevant_endpoints(SimpleNamespace(nace_code="C10.71", sector_name="Baked goods")) == ["food"]
    assert fda.relevant_endpoints(SimpleNamespace(nace_code="C21.20", sector_name="Pharmaceuticals")) == ["drug"]
    assert fda.relevant_endpoints(SimpleNamespace(nace_code="C32.50", sector_name="Medical devices")) == ["device"]
    assert fda.relevant_endpoints(SimpleNamespace(nace_code="A01.1", sector_name="Agrifood & Agriculture")) == []


def test_no_fda_hits_remain_non_scoring_evidence(monkeypatch):
    row = {"company_id": "7", "company_name": "Example SRL", "recalls": [], "searches": []}
    monkeypatch.setattr(fda, "run_ts_crawler", lambda name, rows: [row])
    monkeypatch.setattr(fda, "rows_for_company", lambda rows, company_id: rows)
    company = SimpleNamespace(id=7, legal_name="Example SRL", nace_code="C10.73", sector_name="Pasta")
    result = fda._fetch_live(company)
    assert result["signals"] == {}
    assert result["raw_payload"]["recalls"] == []
