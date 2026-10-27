"""
Golden-benchmark scorer for the deep-intelligence program (docs/INTEL_ROADMAP.md).

A "golden" file (docs/intel/golden/*.json) is what a human researcher found about one company with a browser.
This script checks, fact by fact, whether the TOOL holds the same information, and prints a coverage number plus
the list of misses with the ticket that is supposed to close each. It is how every ticket proves progress:
`--require-ticket I12` exits non-zero until every golden fact assigned to I12 is captured.

What a "capture" means: the fact's regex patterns hit somewhere in the text the tool stored for the company
(registry columns, people, signals with their summaries and raw payloads, raw import blobs, crawler blobs). It proves
the tool HOLDS the information; it does not prove the page shows it or that it is right. Treat it as the floor of
a ticket's acceptance, never the whole of it: the ticket text lists what else must be verified by eye.

Sources of the tool's text (combine as needed):
  --db                     the configured database (DATABASE_URL), read-only SELECTs; needs the golden's company_id
  --bench-report PATH      a report written by scripts/crawler_bench.py (repeatable): a fresh run, crawlers only
  --corpus-file PATH       any text file (repeatable), e.g. a rendered dossier exported to text

Examples:
  python scripts/intel_gap.py --golden docs/intel/golden/asco_filtri.json --db
  python scripts/intel_gap.py --golden docs/intel/golden/asco_filtri.json --bench-report bench/report.json --web-only
  python scripts/intel_gap.py --golden docs/intel/golden/asco_filtri.json --db --require-ticket I12

--web-only keeps only text that came from crawlers/adapters/web (drops registry, AIDA and board-roster data), which
is the number that measures the collection layer itself. Never run a bench with the live DATABASE_URL (the bench
harness refuses to); this script only reads.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ORIGIN_REGISTRY = "registry"   # company row, AIDA imports, board roster, computed ratios
ORIGIN_WEB = "web"             # crawlers, adapters, web research

# SignalRecord.source values that are the user's own imported / computed data, not collection
REGISTRY_SOURCES = ("AIDA", "Board Roster Analysis", "Computed", "Manual", "Data Import")


def _norm(text: str) -> str:
    return (text or "").lower()


def _dump(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


# ------------------------------------------------------------------------------------------------ golden file

class GoldenError(ValueError):
    pass


def load_golden(path: Path) -> Dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    facts = data.get("facts")
    if not isinstance(facts, list) or not facts:
        raise GoldenError(f"{path}: no facts")
    seen = set()
    for f in facts:
        fid = f.get("id")
        if not fid or fid in seen:
            raise GoldenError(f"{path}: missing or duplicate fact id {fid!r}")
        seen.add(fid)
        for key in ("category", "claim", "weight", "match"):
            if key not in f:
                raise GoldenError(f"{path}: fact {fid} lacks {key!r}")
        if not f.get("sources"):
            raise GoldenError(f"{path}: fact {fid} has no sources; a golden fact must be checkable")
        match = f["match"]
        if not (match.get("any") or match.get("all")):
            raise GoldenError(f"{path}: fact {fid} has an empty match")
        for pat in list(match.get("any", [])) + list(match.get("all", [])):
            try:
                re.compile(pat, re.I | re.S)
            except re.error as exc:
                raise GoldenError(f"{path}: fact {fid} has an invalid pattern {pat!r}: {exc}") from exc
    return data


# ------------------------------------------------------------------------------------------------ corpus

Chunk = Tuple[str, str, str]   # (origin, label, lower-cased text)


def chunks_from_session(db, company_id: str) -> List[Chunk]:
    """Everything the database holds for one company. Read-only."""
    from models import Company, CompanyPerson, RawImportRecord, SignalRecord

    out: List[Chunk] = []
    company = db.query(Company).filter_by(id=company_id).first()
    if company is None:
        raise GoldenError(f"company {company_id} not found in the database")
    out.append((ORIGIN_REGISTRY, "company row", _norm(_dump(company.to_dict()))))
    for p in db.query(CompanyPerson).filter_by(company_id=company_id).all():
        out.append((ORIGIN_REGISTRY, f"person {p.role_group}", _norm(_dump(p.to_dict()))))
    for s in db.query(SignalRecord).filter_by(company_id=company_id).all():
        if s.is_simulated:
            continue   # a simulated placeholder is not information the tool holds
        origin = ORIGIN_REGISTRY if str(s.source or "").startswith(REGISTRY_SOURCES) else ORIGIN_WEB
        text = " ".join([str(s.signal_key), str(s.source), _dump(s.numeric_value), str(s.text_value or ""),
                         str(s.raw_payload_ref or "")])
        out.append((origin, f"signal {s.signal_key} ({s.source})", _norm(text)))
    for r in db.query(RawImportRecord).filter_by(company_id=company_id).all():
        origin = ORIGIN_WEB if str(r.dataset_name).startswith("crawler_") else ORIGIN_REGISTRY
        out.append((origin, f"blob {r.dataset_name}", _norm(_dump(r.raw_row))))
    return out


def chunks_from_bench_report(path: Path) -> List[Chunk]:
    """A scripts/crawler_bench.py report: signals (with summaries and payloads) and crawler blobs, all web origin."""
    rep = json.loads(Path(path).read_text(encoding="utf-8"))
    out: List[Chunk] = []
    for entry in rep:
        lane = entry.get("lane", "?")
        for s in entry.get("signals", []):
            if s.get("is_simulated"):
                continue
            out.append((ORIGIN_WEB, f"bench {lane} signal {s.get('signal_key')}", _norm(_dump(s))))
        for name, blob in (entry.get("blobs") or {}).items():
            out.append((ORIGIN_WEB, f"bench blob {name}", _norm(_dump(blob))))
    return out


def chunks_from_text_file(path: Path) -> List[Chunk]:
    """A plain text file, or a scripts/intel_run.py output: for that only the LEDGER FACTS count, never the input
    company block next to them (otherwise the VAT number and city we started from would score as 'found')."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    if str(path).lower().endswith(".json"):
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("ledger"), dict):
            data = data["ledger"]
        if isinstance(data, dict) and isinstance(data.get("facts"), list):
            return [(ORIGIN_WEB, f"ledger fact {f.get('key')}", _norm(_dump(f))) for f in data["facts"]]
    return [(ORIGIN_WEB, f"file {Path(path).name}", _norm(raw))]


# ------------------------------------------------------------------------------------------------ scoring

def _fact_hits(fact: Dict[str, Any], chunks: Iterable[Chunk]) -> List[str]:
    """Labels of the chunks that satisfy the fact (all `all` patterns somewhere across the chunk set of one chunk,
    or any `any` pattern). A fact is judged chunk by chunk first; `all` may also be satisfied across chunks."""
    match = fact["match"]
    all_pats = [re.compile(p, re.I | re.S) for p in match.get("all", [])]
    any_pats = [re.compile(p, re.I | re.S) for p in match.get("any", [])]
    labels: List[str] = []
    chunks = list(chunks)
    if all_pats:
        # every pattern must be found, in the same chunk or spread over several chunks of the same origin
        for origin in {c[0] for c in chunks}:
            same = [c for c in chunks if c[0] == origin]
            joined = "\n".join(c[2] for c in same)
            if all(p.search(joined) for p in all_pats):
                hit = [c[1] for c in same if any(p.search(c[2]) for p in all_pats)]
                labels.extend(hit[:3])
        return sorted(set(labels))
    for origin, label, text in chunks:
        if any(p.search(text) for p in any_pats):
            labels.append(label)
    return sorted(set(labels))


def score(golden: Dict[str, Any], chunks: List[Chunk], web_only: bool = False) -> Dict[str, Any]:
    use = [c for c in chunks if c[0] == ORIGIN_WEB] if web_only else list(chunks)
    results: List[Dict[str, Any]] = []
    for fact in golden["facts"]:
        hits = _fact_hits(fact, use)
        results.append({
            "id": fact["id"], "category": fact["category"], "weight": fact["weight"], "claim": fact["claim"],
            "captured": bool(hits), "where": hits[:3], "closed_by": fact.get("closed_by", []),
        })
    by_cat: Dict[str, Dict[str, float]] = OrderedDict()
    for r in results:
        c = by_cat.setdefault(r["category"], {"facts": 0, "captured": 0, "weight": 0.0, "weight_captured": 0.0})
        c["facts"] += 1
        c["weight"] += r["weight"]
        if r["captured"]:
            c["captured"] += 1
            c["weight_captured"] += r["weight"]
    total_w = sum(r["weight"] for r in results)
    got_w = sum(r["weight"] for r in results if r["captured"])
    return {
        "subject": golden.get("subject", {}), "web_only": web_only,
        "facts": len(results), "captured": sum(1 for r in results if r["captured"]),
        "weighted_coverage": round(got_w / total_w, 4) if total_w else 0.0,
        "by_category": by_cat, "results": results,
    }


def missing_for_ticket(report: Dict[str, Any], ticket: str) -> List[Dict[str, Any]]:
    return [r for r in report["results"] if ticket in r["closed_by"] and not r["captured"]]


def format_report(report: Dict[str, Any], show_captured: bool = False) -> str:
    subj = report["subject"]
    mode = "web/crawler text only" if report["web_only"] else "everything the tool holds"
    lines = [f"Golden benchmark: {subj.get('legal_name', '?')}  [{mode}]",
             f"Captured {report['captured']} of {report['facts']} facts; weighted coverage "
             f"{report['weighted_coverage'] * 100:.0f}%", ""]
    lines.append(f"{'category':16s} {'facts':>6s} {'captured':>9s} {'weighted':>9s}")
    for cat, c in report["by_category"].items():
        pct = c["weight_captured"] / c["weight"] * 100 if c["weight"] else 0
        lines.append(f"{cat:16s} {int(c['facts']):6d} {int(c['captured']):9d} {pct:8.0f}%")
    lines.append("")
    misses = sorted((r for r in report["results"] if not r["captured"]), key=lambda r: (-r["weight"], r["id"]))
    lines.append(f"MISSING ({len(misses)}), most important first:")
    for r in misses:
        tickets = ",".join(r["closed_by"]) or "-"
        lines.append(f"  [{r['weight']}] {r['id']:6s} -> {tickets:12s} {r['claim'][:110]}")
    if show_captured:
        lines.append("")
        lines.append("CAPTURED:")
        for r in report["results"]:
            if r["captured"]:
                lines.append(f"  [{r['weight']}] {r['id']:6s} in {', '.join(r['where'])[:90]}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------ CLI

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True, help="golden file, e.g. docs/intel/golden/asco_filtri.json")
    ap.add_argument("--db", action="store_true", help="read the company from the configured database (read-only)")
    ap.add_argument("--company-id", help="override the golden's company_id (with --db)")
    ap.add_argument("--bench-report", action="append", default=[], help="crawler_bench.py JSON report (repeatable)")
    ap.add_argument("--corpus-file", action="append", default=[], help="plain text file to include (repeatable)")
    ap.add_argument("--web-only", action="store_true", help="count only crawler/web text, not registry/AIDA data")
    ap.add_argument("--show-captured", action="store_true")
    ap.add_argument("--json", dest="json_out", help="write the full result to this path")
    ap.add_argument("--require-ticket", help="exit 1 unless every golden fact assigned to this ticket is captured")
    ap.add_argument("--min-coverage", type=float, help="exit 1 if weighted coverage is below this (0-1)")
    a = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    golden = load_golden(Path(a.golden))
    chunks: List[Chunk] = []
    if a.db:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from database import get_db_session
        db = get_db_session()
        try:
            chunks += chunks_from_session(db, a.company_id or golden["subject"]["company_id"])
        finally:
            db.close()
    for p in a.bench_report:
        chunks += chunks_from_bench_report(Path(p))
    for p in a.corpus_file:
        chunks += chunks_from_text_file(Path(p))
    if not chunks:
        print("error: give at least one of --db, --bench-report, --corpus-file", file=sys.stderr)
        return 2

    report = score(golden, chunks, web_only=a.web_only)
    print(format_report(report, show_captured=a.show_captured))
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    code = 0
    if a.require_ticket:
        missing = missing_for_ticket(report, a.require_ticket)
        if missing:
            print(f"\nFAIL: ticket {a.require_ticket} still misses " + ", ".join(m["id"] for m in missing))
            code = 1
        else:
            print(f"\nOK: every golden fact assigned to {a.require_ticket} is captured")
    if a.min_coverage is not None and report["weighted_coverage"] < a.min_coverage:
        print(f"\nFAIL: weighted coverage {report['weighted_coverage']:.2f} < {a.min_coverage:.2f}")
        code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
