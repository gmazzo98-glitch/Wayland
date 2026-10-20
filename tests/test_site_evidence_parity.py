"""
The reader's findings become signals in two places that must never disagree: the Scraper tool's
tools/site_evidence/signals.py (what the worker ships when a run is started from the app) and this repo's
scrapers/site_evidence_reader.py (the by-hand importer). This runs both on the same findings and compares.
Skipped where the Scraper repository is not checked out next to this one.
"""

import importlib
import sys
from pathlib import Path

import pytest

import config
from scrapers import site_evidence_reader as importer

SCRAPER_ROOT = Path(config.SCRAPER_CRAWLERS_DIR).resolve().parent


def _tool_module(name):
    if not (SCRAPER_ROOT / "tools" / "site_evidence" / f"{name}.py").is_file():
        pytest.skip("the Scraper repository is not next to this one")
    sys.path.insert(0, str(SCRAPER_ROOT))
    try:
        return importlib.import_module(f"tools.site_evidence.{name}")
    finally:
        sys.path.remove(str(SCRAPER_ROOT))


def _hit(claim, quote, **kw):
    return {"claim": claim, "quote": quote, "page_url": "https://acme.it/p", "page_title": "Page", "via": "live",
            "found_at": "2026-10-01T10:00:00", "model": "qwen2.5:3b", "num_ctx": 4096, "rules_version": 4, **kw}


FIXTURES = {
    "all five claims": [
        _hit("university_partnership", "Collaboriamo con il Politecnico di Torino.", institution_hint="Politecnico di Torino",
             first_seen={"status": "ok", "first_seen_date": "2023-02-08", "replay_url": "https://web.archive.org/x", "note": "n",
                         "earliest_archived": "2023"}),
        _hit("external_collaboration", "Partner: Fraunhofer in a joint lab.", partner_hint="Fraunhofer"),
        _hit("open_innovation", "Winner of the 2024 Smau innovation award.", program_hint="Smau"),
        _hit("export_share_pct", "Export is 62% of our turnover.", value=62),
        _hit("export_since_year", "Exporting since 1998.", value=1998)],
    "live beats archived, earlier beats later": [
        _hit("university_partnership", "Archived text about the university.", via="wayback", snapshot_ts="20240315101010",
             found_at="2026-09-01T00:00:00"),
        _hit("university_partnership", "Later live text.", found_at="2026-10-05T00:00:00"),
        _hit("university_partnership", "Earlier live text.", found_at="2026-10-02T00:00:00")],
    "archived only": [_hit("open_innovation", "Archived award text.", via="wayback", snapshot_ts="20240315101010")],
    "old rules refused": [_hit("university_partnership", "Old finding.", rules_version=3),
                          _hit("export_share_pct", "Export 50%.", value=50, rules_version=1)],
    "blank quote, unknown claim, missing value, future year": [
        _hit("university_partnership", "   "), _hit("something_else", "x"), _hit("export_share_pct", "Export.", value=None),
        _hit("export_since_year", "Since 2090.", value=2090)],
    "bounded dating": [_hit("external_collaboration", "Joint project with Siemens.",
                            first_seen={"status": "bounded", "first_seen_date": "2022-05-01", "note": "before"})],
    "dating that failed": [_hit("external_collaboration", "Joint project with Siemens.", first_seen={"status": "no_captures"})],
    "nothing": [],
}


@pytest.mark.parametrize("name", list(FIXTURES))
def test_tool_and_importer_derive_the_same_signals(name):
    tool = _tool_module("signals")
    hits = FIXTURES[name]
    theirs = {s["signal_key"]: s for s in tool.derive_signals(hits, year_now=2026)}
    ours = importer._derive_signals(hits, year_now=2026)
    assert set(theirs) == set(ours)
    for key, signal in ours.items():
        t = theirs[key]
        assert {k: t[k] for k in ("value", "status", "summary", "evidence", "confidence")} == signal, key


def test_the_two_agree_on_the_rules_version_and_the_mapping():
    tool = _tool_module("signals")
    claims = _tool_module("claims")
    assert importer.MIN_RULES_VERSION == claims.RULES_VERSION
    assert importer.CLAIM_TO_SIGNAL == tool.CLAIM_TO_SIGNAL
    assert (importer.CONFIDENCE_LIVE, importer.CONFIDENCE_ARCHIVE) == (tool.CONFIDENCE_LIVE, tool.CONFIDENCE_ARCHIVE)


def test_the_database_function_allows_exactly_the_keys_the_mapping_produces():
    """The SQL function refuses any signal key outside its own list; if a key is ever added to the mapping it must be added there too."""
    sql = (Path(__file__).resolve().parent.parent / "worker" / "supabase_rpc.sql").read_text(encoding="utf-8")
    for key in importer.CLAIM_TO_SIGNAL.values():
        assert f"'{key}'" in sql
    assert f"v_min_rules constant integer := {importer.MIN_RULES_VERSION};" in sql
