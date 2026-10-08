"""TED award evidence must identify the winner, never infer a private contract."""

from types import SimpleNamespace

from scrapers import ted_awards_crawler as ted


def test_remote_crawler_result_is_wrapped_as_non_scoring_evidence(monkeypatch):
    expected = [{"company_id": "123", "company_name": "Vandeputte Medical",
                 "source": "TED Contract Awards Crawler", "awards": [{"publication_number": "1-2026"}]}]
    monkeypatch.setattr(ted, "run_ts_crawler", lambda name, rows: expected)
    monkeypatch.setattr(ted, "rows_for_company", lambda rows, company_id: rows)
    result = ted._fetch_live(SimpleNamespace(id=123, legal_name="Vandeputte Medical"))
    assert result["signals"] == {}
    assert result["raw_payload"]["awards"][0]["publication_number"] == "1-2026"
