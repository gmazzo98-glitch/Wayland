"""
Imports what Scraper/tools/site_evidence found on companies' OWN websites (Phase 7, T25).

Unlike the Node crawlers, this producer is not called per company from a sweep: a local model
reads pages at ~40 s per passage and a full pass takes days on a spare PC, so the tool
(tools/site_evidence, running under its own supervisor) writes a results folder and this module
turns that folder into signals afterwards. Nothing here reaches the web or a model.

Feeds, one claim to one EXISTING indicator (no new keys, no weight changes - ground rule 1):
    university_partnership -> university_partnership          (value 1.0)
    external_collaboration -> external_collaboration          (value 1.0)
    open_innovation        -> prior_open_innovation_usage     (value 1.0)
    export_share_pct       -> international_sales_volume      (value = the stated percentage)
    export_since_year      -> years_international_activity    (value = this year - the stated year)

Honesty rules, all enforced here and tested:
  * Only positive, quote-verified findings become signals. "Nothing found on the pages we read"
    is never written as 'absent': a company can collaborate without saying so on its site.
  * One row per (company, signal_key) exists in the table, so a second producer would silently
    replace a first. Never a real 'present' row from another source and never a real 'absent';
    those are reported as conflicts and left alone. Only an empty/simulated placeholder, or this
    producer's own earlier row, is replaced.
  * Every signal carries the verbatim quote, the page URL, whether the page was read live or from
    an archived copy (lower confidence), the model that proposed it, and, when the Wayback Machine
    could date it, the earliest archived snapshot containing the same text.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from adapters.base import _upsert_signal, get_or_create_source_health
from models import Company, SignalRecord, SourceHealth
from scrapers.node_crawler_base import save_crawler_blob

SOURCE_NAME = "Site Evidence Reader"
DATASET_NAME = "crawler_site_evidence"
PHASE = 7

# The tool stamps each finding with the rules version that accepted it (claims.RULES_VERSION) and bumps it
# whenever a rule gets stricter; anything older than this is a finding the current rules might reject.
MIN_RULES_VERSION = 4

CONFIDENCE_LIVE = 0.6       # a verified quote from the company's own live page
CONFIDENCE_ARCHIVE = 0.45   # the same, read from an archived copy because the live site failed

CLAIM_TO_SIGNAL = {
    "university_partnership": "university_partnership",
    "external_collaboration": "external_collaboration",
    "open_innovation": "prior_open_innovation_usage",
    "export_share_pct": "international_sales_volume",
    "export_since_year": "years_international_activity",
}


def _short(text: str, n: int = 150) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "..."


def _hint(hit: Dict[str, Any]) -> Optional[str]:
    return hit.get("institution_hint") or hit.get("partner_hint") or hit.get("program_hint")


def _derive_signals(hits: Iterable[Dict[str, Any]], year_now: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
    """One company's confirmed hits -> {signal_key: signal dict}. Pure and unit-tested.

    If a claim was found more than once (a re-run, or live and archived copies), the live one
    wins, then the earliest found: re-importing never changes a signal for no reason."""
    year_now = year_now or datetime.utcnow().year
    best: Dict[str, Dict[str, Any]] = {}
    for hit in hits:
        claim = hit.get("claim")
        if claim not in CLAIM_TO_SIGNAL or not (hit.get("quote") or "").strip():
            continue
        if int(hit.get("rules_version") or 1) < MIN_RULES_VERSION:
            continue
        rank = (0 if hit.get("via") == "live" else 1, hit.get("found_at") or "")
        if claim not in best or rank < best[claim][0]:
            best[claim] = (rank, hit)

    signals: Dict[str, Dict[str, Any]] = {}
    for claim, (_, hit) in best.items():
        key = CLAIM_TO_SIGNAL[claim]
        archived = hit.get("via") == "wayback"
        if claim in ("export_share_pct", "export_since_year"):
            raw = hit.get("value")
            if raw is None:
                continue
            if claim == "export_share_pct":
                value = float(raw)
                summary = f"own site states {value:.0f}% of sales/production goes abroad"
            else:
                value = float(year_now - int(raw))
                if value < 0:
                    continue
                summary = f"own site states exporting/operating abroad since {int(raw)}"
        else:
            value = 1.0
            hint = _hint(hit)
            label = {"university_partnership": "university/research partnership",
                     "external_collaboration": "external collaboration",
                     "open_innovation": "innovation programme or award"}[claim]
            summary = f"own site documents a {label}" + (f" ({_short(hint, 60)})" if hint else "")
        if archived:
            ts = hit.get("snapshot_ts") or ""
            summary += f" [archived copy of {ts[:4]}-{ts[4:6]}-{ts[6:8]}]" if len(ts) >= 8 else " [archived copy]"
        summary += f': "{_short(hit["quote"])}"'

        dating = hit.get("first_seen") or {}
        evidence = {
            "method": "local model proposed one verbatim sentence from the company's own site; code verified it "
                      "is on the page and passes the claim's rules",
            "source_urls": [hit.get("page_url")] if hit.get("page_url") else [],
            "quote": hit["quote"], "page_title": hit.get("page_title"),
            "page_read": "archived copy (live site unreadable)" if archived else "live",
            "snapshot_ts": hit.get("snapshot_ts"), "model": hit.get("model"), "num_ctx": hit.get("num_ctx"),
            "found_at": hit.get("found_at"), "claim": claim, "hint": _hint(hit),
            "rules_version": hit.get("rules_version"),
        }
        if dating.get("status") in ("ok", "bounded"):
            evidence["first_seen_in_archive"] = {
                "date": dating.get("first_seen_date"), "replay_url": dating.get("replay_url"),
                "note": dating.get("note"), "earliest_archived": dating.get("earliest_archived"),
            }
            summary += f" (same text archived since {dating.get('first_seen_date')})"
        signals[key] = {
            "value": value, "status": "present", "summary": summary, "evidence": evidence,
            "confidence": CONFIDENCE_ARCHIVE if archived else CONFIDENCE_LIVE,
        }
    return signals


# ---- reading the tool's results folder ----------------------------------------------------------
def read_results(folder: Path) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Dict[str, Any]]]:
    """(hits by company_id, state by company_id) from a site_evidence output folder.
    Tolerates a torn last line (a crash mid-write) and a missing state file."""
    folder = Path(folder)
    hits: Dict[str, List[Dict[str, Any]]] = {}
    results = folder / "results.jsonl"
    if results.exists():
        for line in results.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("company_id"):
                hits.setdefault(str(row["company_id"]), []).append(row)
    state: Dict[str, Dict[str, Any]] = {}
    for name in ("state.json", "state.json.bak"):
        path = folder / name
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    state = loaded
                    break
            except ValueError:
                continue
    return hits, state


# ---- deciding what may be written ---------------------------------------------------------------
def decide(existing: Optional[SignalRecord]) -> Tuple[str, Optional[str]]:
    """('create'|'update'|'replace'|'skip', reason). The one-row-per-key rule is why this exists."""
    if existing is None:
        return "create", None
    if (existing.source or "") == SOURCE_NAME:
        return "update", None
    real = not existing.is_simulated
    if existing.status == "not_yet_checked" or existing.is_simulated:
        return "replace", None
    if real and existing.status == "present":
        return "skip", f"already supplied by {existing.source}"
    if real and existing.status == "absent":
        return "skip", f"{existing.source} recorded 'absent' - a conflict for a human to settle"
    return "skip", f"existing {existing.status} row from {existing.source}"


def build_plan(db: Session, hits_by_company: Dict[str, List[Dict[str, Any]]], year_now: Optional[int] = None) -> Dict[str, Any]:
    """What an import would do, without writing anything."""
    writes: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    unmatched: List[str] = []
    for reg_no, hits in sorted(hits_by_company.items()):
        company = db.query(Company).filter_by(registration_number=reg_no).first()
        if company is None:
            unmatched.append(reg_no)
            continue
        for key, sig in _derive_signals(hits, year_now).items():
            existing = db.query(SignalRecord).filter_by(company_id=company.id, signal_key=key).first()
            action, reason = decide(existing)
            item = {"company_id": company.id, "registration_number": reg_no, "legal_name": company.legal_name,
                    "signal_key": key, "signal": sig, "action": action,
                    "replaces": None if existing is None else {
                        "source": existing.source, "status": existing.status, "value": existing.numeric_value,
                        "is_simulated": existing.is_simulated}}
            if action == "skip":
                item["reason"] = reason
                conflicts.append(item)
            else:
                writes.append(item)
    return {"writes": writes, "conflicts": conflicts, "unmatched": unmatched}


def apply_plan(db: Session, plan: Dict[str, Any], hits_by_company: Dict[str, List[Dict[str, Any]]],
               state: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    """Writes the plan: signals, one raw blob per company, and the SourceHealth row."""
    written, companies = 0, set()
    for item in plan["writes"]:
        sig = item["signal"]
        _upsert_signal(
            db, item["company_id"], item["signal_key"], SOURCE_NAME, sig["value"], "present", sig["confidence"],
            {"dataset": DATASET_NAME}, is_simulated=False, summary=sig["summary"], evidence=sig["evidence"],
        )
        written += 1
        companies.add((item["company_id"], item["registration_number"]))
    db.commit()
    for company_id, reg_no in companies:
        company = db.query(Company).filter_by(id=company_id).one()
        save_crawler_blob(db, company, DATASET_NAME, {
            "hits": hits_by_company.get(reg_no, []), "coverage": state.get(reg_no, {}),
            "imported_at": datetime.utcnow().isoformat(timespec="seconds"),
        })
    health = get_or_create_source_health(db, SOURCE_NAME, PHASE)
    health.total_calls = SourceHealth.total_calls + len(companies)
    health.last_run_at = datetime.utcnow()
    health.last_status = "success"
    health.mode = "live"
    health.last_error_message = None
    db.commit()
    return {"signals_written": written, "companies": len(companies)}
