"""
Feeds competitor_digital_gap (indicators.py) — its own comment calls this "explicitly a
derived variable, not a new independent data source: re-runs Digital Maturity of Core
Website and Online Market Presence against named competitors," and names the missing piece
plainly: "which companies are whose competitors isn't captured anywhere today." models.py's
Competitor table is that piece; this module is the re-run.

For each of a company's recorded Competitor rows (up to MAX_COMPETITORS, manually entered —
see Competitor's own docstring for why this is deliberately not auto-discovered), runs
digital-maturity-crawler against that competitor's own homepage exactly the way
scrapers/digital_maturity_crawler.py runs it for a real company (same env, same signal-deriving
function), then compares the company's own already-measured website_digital_maturity against
the competitors' average. A positive gap means this company's site is MORE neglected than its
named peers' — a sharper, more specific NEED signal than an absolute score alone.

Depends on Digital Maturity Crawler having already written this company's OWN
website_digital_maturity signal. Phase 7's steps for one company run concurrently
(company_service._run_steps_concurrently) with no ordering guarantee between them, so on a
company's very FIRST Phase 7 run this may skip with a clear reason even though Digital
Maturity Crawler ran moments earlier in the same batch — a second run (this tool's normal
re-crawl rhythm; see company_website_crawler's own trend-signal notes for the same pattern)
picks up the now-present value. This is an honest gap, not a bug: writing a gap against a
value that might not have landed yet would risk comparing against a stale or missing number.
"""

from datetime import datetime
from sqlalchemy.orm import Session

from adapters.base import run_adapter
from models import Competitor, RawImportRecord, SignalRecord
from scrapers import digital_maturity_crawler
from scrapers.node_crawler_base import CrawlerRunError, rows_for_company, run_ts_crawler

SOURCE_NAME = "Competitor Benchmark"
CRAWLER_DIR = "digital-maturity-crawler"
DATASET_NAME = "competitor_benchmark"
PHASE = 7

# Matches competitor_digital_gap's own proxy text ("2-3 named direct competitors") — a hard
# cap, not a default, so a long list someone pastes in doesn't quietly multiply this adapter's
# runtime (each one is a full digital-maturity-crawler call, ~RUN_TIMEOUT_SECONDS worst case).
MAX_COMPETITORS = 3


def _crawl_competitor(competitor: Competitor) -> dict:
    """Runs digital-maturity-crawler against one competitor's homepage, same env/timeout as a
    real company gets (scrapers/digital_maturity_crawler.py) — the competitor's own `id` stands
    in for company_id, which the TS crawler only ever uses as an opaque row key. Raises
    CrawlerRunError on a missing/empty row, same contract every other Phase 7 wrapper relies on."""
    rows = run_ts_crawler(
        CRAWLER_DIR, [{"company_id": competitor.id, "homepage_url": competitor.homepage_url}],
        env_overrides=digital_maturity_crawler.build_crawler_env(),
        run_timeout=digital_maturity_crawler.RUN_TIMEOUT_SECONDS,
    )
    matches = rows_for_company(rows, competitor.id)
    if not matches:
        raise CrawlerRunError(f"digital-maturity-crawler returned no row for competitor '{competitor.name}'")
    return matches[0]


def _own_maturity_signal(db: Session, company_id: str):
    """The company's own already-measured website_digital_maturity SignalRecord, or None if it
    hasn't been produced yet (not yet run, or every attempt so far was simulated) — see this
    module's own docstring on why a missing value means 'skip', never a guessed gap."""
    sig = db.query(SignalRecord).filter_by(company_id=company_id, signal_key="website_digital_maturity").first()
    if not sig or sig.is_simulated or sig.numeric_value is None:
        return None
    return sig


def compute_gap(own_value: float, competitor_results: list) -> dict:
    """
    Pure function over already-crawled competitor rows (no network/DB) — the actual gap math
    and evidence shaping, kept separate from _fetch_live's orchestration so it's directly
    testable. `competitor_results` is [{"competitor": Competitor, "row": dict | None,
    "error": str | None}, ...], one entry per attempted competitor.

    Returns {} if no competitor yielded a usable website_digital_maturity (every crawl failed,
    or none had Wayback/vision coverage either) — never a gap computed from zero evidence.
    """
    usable, skipped = [], []
    for entry in competitor_results:
        competitor = entry["competitor"]
        if entry.get("error"):
            skipped.append({"name": competitor.name, "homepage_url": competitor.homepage_url, "reason": entry["error"]})
            continue
        row = entry["row"]
        signals = digital_maturity_crawler._derive_signals(row)
        maturity = signals.get("website_digital_maturity")
        if not maturity:
            skipped.append({"name": competitor.name, "homepage_url": competitor.homepage_url,
                             "reason": "no website_digital_maturity signal (no Wayback or vision coverage for this site)"})
            continue
        usable.append({"name": competitor.name, "homepage_url": competitor.homepage_url,
                        "website_digital_maturity": maturity["value"], "summary": maturity["summary"],
                        "source_method": maturity["evidence"].get("method")})

    if not usable:
        return {}

    avg_competitor_age = sum(u["website_digital_maturity"] for u in usable) / len(usable)
    gap = own_value - avg_competitor_age
    names = ", ".join(f"{u['name']} ({u['website_digital_maturity']:.1f}y)" for u in usable)
    return {
        "competitor_digital_gap": {
            "value": round(gap, 2), "status": "present",
            "summary": f"{own_value:.1f} years since this company's own last redesign vs "
                       f"{avg_competitor_age:.1f}y average across {len(usable)} named competitor(s): {names}",
            "evidence": {
                "method": "re-ran Digital Maturity Crawler (Wayback structural diff + vision assessment, same "
                          "as the company's own) against each named competitor's own homepage",
                "own_website_digital_maturity": own_value,
                "competitors_benchmarked": usable,
                "competitors_skipped": skipped,
            },
        },
    }


def sync_competitor_benchmark(company, db_session: Session) -> dict:
    competitors = [c for c in (company.competitors or []) if c.homepage_url][:MAX_COMPETITORS]
    if not competitors:
        return {"status": "skipped",
                "reason": "No named competitor with a homepage recorded — add one or more in Company Intelligence."}

    own_signal = _own_maturity_signal(db_session, company.id)
    if own_signal is None:
        return {"status": "skipped",
                "reason": "This company's own website_digital_maturity hasn't been measured yet — "
                          "run Digital Maturity Crawler (or re-run Phase 7) first, then retry."}

    captured = {}

    def _fetch_live(c):
        results = []
        for competitor in competitors:
            try:
                row = _crawl_competitor(competitor)
            except Exception as e:  # noqa: BLE001 — one competitor's failure must not sink the rest
                results.append({"competitor": competitor, "row": None, "error": str(e)})
                continue
            results.append({"competitor": competitor, "row": row, "error": None})
        captured["results"] = results

        signals = compute_gap(own_signal.numeric_value, results)
        if not signals:
            raise CrawlerRunError("None of the recorded competitors could be assessed "
                                  "(every crawl failed, or found no Wayback/vision coverage)")
        usable_count = len(signals["competitor_digital_gap"]["evidence"]["competitors_benchmarked"])
        return {
            "signals": signals,
            "raw_payload": {"competitors_recorded": len(competitors), "competitors_usable": usable_count},
            # Below company-website-crawler's 0.7/0.75 own-signal confidence: a gap is only as
            # trustworthy as the thinnest leg it rests on, and 1-2 peers is a small sample.
            "confidence": 0.6 if usable_count >= 2 else 0.45,
        }

    def _simulate(c):
        return {"signals": {}, "raw_payload": {"note": "competitor benchmark unavailable"}, "confidence": 0.5}

    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=True, fetch_live=_fetch_live, simulate=_simulate,
        timeout=len(competitors) * digital_maturity_crawler.RUN_TIMEOUT_SECONDS + 60,
    )

    if captured.get("results"):
        rec = db_session.query(RawImportRecord).filter_by(company_id=company.id, dataset_name=DATASET_NAME).first()
        if rec is None:
            rec = RawImportRecord(company_id=company.id, dataset_name=DATASET_NAME)
            db_session.add(rec)
        rec.source_filename = "Competitor Benchmark — per-competitor digital-maturity-crawler rows"
        rec.raw_row = {
            "competitors": [
                {"name": r["competitor"].name, "homepage_url": r["competitor"].homepage_url,
                 "row": r["row"], "error": r["error"]}
                for r in captured["results"]
            ],
        }
        rec.updated_at = datetime.utcnow()
        db_session.commit()

    return result
