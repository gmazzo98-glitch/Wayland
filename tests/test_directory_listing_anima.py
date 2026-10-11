import pytest
from scrapers import directory_listing_crawler


def test_directory_listing_default_urls_includes_mecspe_and_anima():
    assert "https://www.mecspe.com/portale/it/espositori" in directory_listing_crawler.DEFAULT_DIRECTORY_URLS
    assert "https://www.anima.it/aziende/" in directory_listing_crawler.DEFAULT_DIRECTORY_URLS


def test_directory_listing_derive_signals_multiple_hits():
    hit_rows = [
        {"appears_in_directory": True, "possible_match": False, "status": "ok", "directory_name": "MECSPE", "directory_id": "mecspe"},
        {"appears_in_directory": True, "possible_match": False, "status": "ok", "directory_name": "ANIMA", "directory_id": "anima", "membership_or_exhibitor_since": "settembre 2019"},
    ]
    sig = directory_listing_crawler._derive_signals(hit_rows)["trade_fair_participation"]
    assert sig["value"] == 2.0
    assert "MECSPE" in sig["summary"]
    assert "ANIMA" in sig["summary"]
    assert "member/exhibitor since settembre 2019" in sig["summary"]
