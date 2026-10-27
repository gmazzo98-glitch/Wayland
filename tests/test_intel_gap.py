"""
The golden-benchmark scorer (scripts/intel_gap.py) and the ASCO golden file it reads.
"""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("intel_gap", ROOT / "scripts" / "intel_gap.py")
gap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gap)

GOLDEN = ROOT / "docs" / "intel" / "golden" / "asco_filtri.json"


def fact(fid="X-01", weight=2, any_=None, all_=None, closed_by=()):
    match = {}
    if any_:
        match["any"] = any_
    if all_:
        match["all"] = all_
    return {"id": fid, "category": "test", "weight": weight, "claim": "c", "sources": ["s"], "match": match,
            "closed_by": list(closed_by)}


def golden_of(*facts):
    return {"subject": {"legal_name": "T"}, "facts": list(facts)}


def chunk(origin, text, label="c"):
    return (origin, label, text.lower())


def test_shipped_golden_file_is_valid_and_traceable():
    g = gap.load_golden(GOLDEN)
    ids = [f["id"] for f in g["facts"]]
    assert len(ids) == len(set(ids)) >= 40
    assert {f["weight"] for f in g["facts"]} <= {1, 2, 3}
    assert all(f["sources"] for f in g["facts"])
    assert g["subject"]["vat"] == "02688370135"


def test_golden_without_sources_or_with_a_bad_pattern_is_refused(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(golden_of({"id": "A", "category": "c", "weight": 1, "claim": "x", "sources": [],
                                          "match": {"any": ["x"]}})), encoding="utf-8")
    with pytest.raises(gap.GoldenError):
        gap.load_golden(bad)
    bad.write_text(json.dumps(golden_of(fact(any_=["(unclosed"]))), encoding="utf-8")
    with pytest.raises(gap.GoldenError):
        gap.load_golden(bad)


def test_any_and_all_matching_and_weighted_coverage():
    g = golden_of(fact("A", 3, any_=["mott corporation"]), fact("B", 1, all_=["abu dhabi", "pune"]),
                  fact("C", 2, any_=["never present"]))
    chunks = [chunk(gap.ORIGIN_WEB, "ASCO is owned by Mott Corporation"), chunk(gap.ORIGIN_WEB, "an office in Abu Dhabi"),
              chunk(gap.ORIGIN_WEB, "another in Pune")]
    rep = gap.score(g, chunks)
    got = {r["id"]: r["captured"] for r in rep["results"]}
    assert got == {"A": True, "B": True, "C": False}
    assert rep["captured"] == 2 and rep["weighted_coverage"] == round(4 / 6, 4)


def test_web_only_ignores_registry_text():
    g = golden_of(fact("A", 3, any_=["mott corporation"]))
    only_registry = [chunk(gap.ORIGIN_REGISTRY, "shareholder MOTT CORPORATION 100%")]
    assert gap.score(g, only_registry)["captured"] == 1
    assert gap.score(g, only_registry, web_only=True)["captured"] == 0


def test_shipped_patterns_stay_tight():
    """Regressions found while building the baseline: loose patterns scored things the tool does not know."""
    g = gap.load_golden(GOLDEN)
    def captured(text):
        return {r["id"]: r["captured"] for r in gap.score(g, [chunk(gap.ORIGIN_WEB, text)])["results"]}

    # 'samp' must be a whole word, not the 'sample' in a job-postings payload
    assert captured('"roles_sample": []')["PE-01"] is False
    assert captured("chairman steven martin samp")["PE-01"] is True
    # Mott plus an unrelated 2022 appointment date must not count as the 2022 acquisition
    assert captured("mott corporation holds 100%. appointed 2022-05-31")["OW-02"] is False
    assert captured("mott acquired asco filtri on january 13, 2022")["OW-02"] is True
    # an arbitrary 17.14... float is not the 2019 revenue
    assert captured('{"x": 17.1432}')["FI-02"] is False
    assert captured("revenue of 17,140,885 euro in 2019")["FI-02"] is True


def test_run_file_scores_only_the_ledger_not_the_input_company(tmp_path):
    run = tmp_path / "run.json"
    run.write_text(json.dumps({
        "company": {"legal_name": "ASCO", "vat": "02688370135", "city": "Binasco (MI)"},
        "ledger": {"facts": [{"key": "digital.web_server", "value": "Microsoft-IIS/10.0"}]}}), encoding="utf-8")
    chunks = gap.chunks_from_text_file(run)
    rep = gap.score(gap.load_golden(GOLDEN), chunks)
    captured = {r["id"] for r in rep["results"] if r["captured"]}
    assert "DG-02" in captured
    assert "ID-01" not in captured and "ID-02" not in captured   # the VAT and the city were INPUT, not findings


def test_bench_report_ignores_simulated_signals(tmp_path):
    rep = tmp_path / "r.json"
    rep.write_text(json.dumps([{"lane": "news", "signals": [
        {"signal_key": "k", "is_simulated": True, "summary": "mott corporation"},
        {"signal_key": "k2", "is_simulated": False, "summary": "idex corp"}], "blobs": {"crawler_x": {"a": "binasco"}}}]),
        encoding="utf-8")
    text = " ".join(c[2] for c in gap.chunks_from_bench_report(rep))
    assert "mott corporation" not in text and "idex corp" in text and "binasco" in text


def test_cli_require_ticket_exit_code(tmp_path, capsys):
    g = tmp_path / "g.json"
    g.write_text(json.dumps(golden_of(fact("A", 3, any_=["found me"], closed_by=["I99"]))), encoding="utf-8")
    hit, miss = tmp_path / "hit.txt", tmp_path / "miss.txt"
    hit.write_text("we FOUND ME here", encoding="utf-8")
    miss.write_text("nothing", encoding="utf-8")
    assert gap.main(["--golden", str(g), "--corpus-file", str(hit), "--require-ticket", "I99"]) == 0
    assert gap.main(["--golden", str(g), "--corpus-file", str(miss), "--require-ticket", "I99"]) == 1
    assert gap.main(["--golden", str(g)]) == 2   # no data source given
    assert "FAIL" in capsys.readouterr().out
