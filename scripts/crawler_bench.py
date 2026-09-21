"""
Crawler verification harness — fires any subset of the scrapers/adapters at a list
of real companies, against a THROWAWAY SQLite database, and dumps everything they
produced (signals + summaries + evidence, raw crawler blobs, SourceHealth errors,
timings) to a JSON report so the output can be checked against the real websites.

Never touches the configured DATABASE_URL: the SQLite path is forced into the
environment before config.py is imported, so the live Supabase project is not
written to by a verification run. Companies come from a JSON fixture (exported
read-only from the live DB, or hand-written), not from the live DB either.

Usage:
    python scripts/crawler_bench.py --companies companies.json --db bench.db --out report.json
        --lanes jobs,directory [--only "CANGINI BENNE,RCM"] [--log-dir logs/]

Lanes (comma-separated): website, jobs, reviews, directory, digital, news, innovation,
news_rss, wappalyzer, ownsite, all.

--mode queue runs the companies through the REAL background path instead (crawl_jobs manager ->
concurrent per-company steps -> resource governor), `--workers N` companies at a time, and reports
how long each company and the whole batch took plus what each crawler produced. That is the number
to compare with the sequential lanes run: the sum of a company's lane times vs its queue time.

Each report entry is one (company, lane) run:
    {company, lane, elapsed_s, adapter_result, source_health: {mode, last_status, last_error},
     signals: [{signal_key, numeric_value, status, is_simulated, confidence, summary, evidence, raw_payload}],
     blobs: {dataset_name: raw_row}}
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--companies", required=True,
                    help="JSON list of company dicts (id, legal_name, registration_number, website_url, country, sector_name, ...)")
parser.add_argument("--db", required=True, help="throwaway SQLite file to write to (created if missing)")
parser.add_argument("--out", required=True, help="JSON report path (merged into if it exists)")
parser.add_argument("--lanes", default="all")
parser.add_argument("--only", default="", help="comma-separated substrings; only companies whose legal_name contains one")
parser.add_argument("--log-dir", default="", help="directory for per-crawler Node subprocess logs")
parser.add_argument("--mode", choices=("lanes", "queue"), default="lanes",
                    help="lanes = each crawler in turn, timed separately (default); queue = the production background path")
parser.add_argument("--workers", type=int, default=3, help="queue mode: companies in flight at once")
parser.add_argument("--phases", default="7", help="queue mode: comma-separated pipeline phases to run")
args = parser.parse_args()

# Must happen before any project import — config.py reads DATABASE_URL at import time.
db_path = Path(args.db).resolve()
# timeout=60: several steps write at once, and SQLite's default 5s busy-wait is not enough for that
# (the production database is Postgres, which has no such limit).
os.environ["DATABASE_URL"] = "sqlite:///" + db_path.as_posix() + "?timeout=60"
if args.log_dir:
    os.environ["VIENNA_CRAWLER_LOG_DIR"] = str(Path(args.log_dir).resolve())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import SQLALCHEMY_DATABASE_URI  # noqa: E402
assert SQLALCHEMY_DATABASE_URI.startswith("sqlite:///"), "refusing to run against " + SQLALCHEMY_DATABASE_URI

from database import get_db_session, init_db  # noqa: E402
from models import Company, RawImportRecord, SignalRecord, SourceHealth  # noqa: E402
from adapters import google_news_rss  # noqa: E402
from scrapers import (  # noqa: E402
    company_website_crawler, digital_maturity_crawler, directory_listing_crawler,
    innovation_participation_crawler, job_postings_crawler, management_diversity,
    news_signals_crawler, review_crawler, wappalyzer_local,
)

LANES = {
    "website": ("Company Website Crawler", company_website_crawler.sync_company_website),
    "jobs": ("Job Postings Crawler", job_postings_crawler.sync_job_postings),
    "reviews": ("Review Crawler", review_crawler.sync_reviews),
    "directory": ("Directory Listing Crawler", directory_listing_crawler.sync_directory_listing),
    "digital": ("Digital Maturity Crawler", digital_maturity_crawler.sync_digital_maturity),
    "news": ("News Signals Crawler", news_signals_crawler.sync_news_signals),
    "innovation": ("Innovation Participation Crawler", innovation_participation_crawler.sync_innovation_participation),
    "news_rss": ("Google News RSS", google_news_rss.sync_news_rss),
    "wappalyzer": ("Wappalyzer", wappalyzer_local.sync_tech_stack),
    "ownsite": ("Own-Site Scrape", management_diversity.sync_management_diversity),
}


def _load_or_create_companies(db, specs):
    out = []
    for spec in specs:
        c = db.query(Company).filter_by(id=spec["id"]).first()
        if not c:
            c = Company(id=spec["id"], legal_name=spec["legal_name"], registration_number=spec["registration_number"],
                        website_url=spec.get("website_url"), country=spec.get("country") or "Italy",
                        sector_name=spec.get("sector_name"), nace_code=spec.get("nace_code") or "C28",
                        headcount=spec.get("headcount"), segment=spec.get("segment") or "SME")
            db.add(c)
            db.commit()
        out.append(c)
    return out


def _snapshot(db, company, source_name):
    sigs = []
    for s in db.query(SignalRecord).filter_by(company_id=company.id, source=source_name).order_by(SignalRecord.signal_key).all():
        try:
            payload = json.loads(s.raw_payload_ref) if s.raw_payload_ref else {}
        except json.JSONDecodeError:
            payload = {"_unparsed": s.raw_payload_ref}
        sigs.append({
            "signal_key": s.signal_key, "numeric_value": s.numeric_value, "status": s.status,
            "is_simulated": s.is_simulated, "confidence": s.confidence, "summary": s.text_value,
            "evidence": payload.pop("evidence", None), "raw_payload": payload,
        })
    sh = db.query(SourceHealth).filter_by(source_name=source_name).first()
    health = {"mode": sh.mode, "last_status": sh.last_status, "last_error": sh.last_error_message} if sh else None
    blobs = {r.dataset_name: r.raw_row for r in db.query(RawImportRecord).filter_by(company_id=company.id).all()
             if r.dataset_name.startswith("crawler_")}
    return sigs, health, blobs


def run_queue(companies, out_path):
    from crawl_jobs import CrawlJobManager
    from database import SessionFactory
    from resource_governor import get_governor

    phases = tuple(int(p) for p in args.phases.split(",") if p.strip())
    mgr = CrawlJobManager(session_factory=SessionFactory)
    print("queue mode: %d companies, %d at a time, phases %s, slots %s" % (
        len(companies), args.workers, phases, {k: v["capacity"] for k, v in get_governor(None).stats().items()}), flush=True)
    t0 = time.time()
    mgr.submit({c.id: c.legal_name for c in companies}, workers=args.workers, phases=phases)
    peak = {}
    while not mgr.wait(2):
        for name, st in get_governor(None).stats().items():
            peak[name] = max(peak.get(name, 0), st["in_use"])
        snap = mgr.snapshot()
        if snap and snap.running:
            print("  [%4.0fs] %d/%d done; running: %s" % (
                time.time() - t0, snap.done, snap.total,
                "; ".join("%s (%s)" % (i.name[:18], ",".join(r.replace(" Crawler", "")[:12] for r in i.running)) for i in snap.in_flight)), flush=True)
    wall = time.time() - t0
    job = mgr._job
    db = get_db_session()
    report = []
    for outcome in job.results:
        c = next((c for c in companies if c.legal_name == outcome.name), None)
        sigs = db.query(SignalRecord).filter_by(company_id=c.id).all() if c else []
        real = [x for x in sigs if not x.is_simulated and x.status in ("present", "absent")]
        report.append({"company": outcome.name, "seconds": round(outcome.seconds, 1), "error": outcome.error,
                       "source_errors": [list(e) for e in outcome.source_errors],
                       "real_signals": {x.signal_key: [x.numeric_value, x.status] for x in real}})
    print("=== queue results: %d companies in %.0fs wall (%.0fs per company throughput); peak slots in use %s" % (
        len(companies), wall, wall / max(1, len(companies)), peak))
    for r in sorted(report, key=lambda r: r["company"]):
        print("  %-40s %5.0fs  %2d real signals  errors: %s" % (r["company"][:40], r["seconds"], len(r["real_signals"]),
                                                                 r["error"] or "; ".join("%s: %s" % (a, b[:60]) for a, b in r["source_errors"]) or "-"))
    out_path.write_text(json.dumps({"wall_seconds": round(wall, 1), "workers": args.workers, "peak_slots": peak, "companies": report},
                                   indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    db.close()


def main():
    specs = json.loads(Path(args.companies).read_text(encoding="utf-8"))
    if args.only:
        needles = [n.strip().lower() for n in args.only.split(",") if n.strip()]
        specs = [s for s in specs if any(n in s["legal_name"].lower() for n in needles)]
    lanes = list(LANES) if args.lanes == "all" else [l.strip() for l in args.lanes.split(",") if l.strip()]
    unknown = [l for l in lanes if l not in LANES]
    if unknown:
        sys.exit("unknown lane(s): %s; choose from %s" % (unknown, list(LANES)))

    init_db()
    db = get_db_session()
    companies = _load_or_create_companies(db, specs)

    out_path = Path(args.out)
    if args.mode == "queue":
        run_queue(companies, out_path)
        db.close()
        return
    report = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else []

    print("DB: %s\n%d companies x %s" % (SQLALCHEMY_DATABASE_URI, len(companies), lanes), flush=True)
    for company in companies:
        for lane in lanes:
            source_name, fn = LANES[lane]
            print("\n=== %s [%s] %s ===" % (company.legal_name, lane, company.website_url), flush=True)
            t0 = time.time()
            try:
                result = fn(company, db)
            except Exception as e:  # a wrapper should never raise, so this is itself a finding
                result = {"status": "CRASH", "error": "%s: %s" % (type(e).__name__, e)}
                db.rollback()
            elapsed = round(time.time() - t0, 1)
            # Only the top-level keys of the adapter result — the signals dict repeats what's in the DB.
            slim = {k: v for k, v in (result or {}).items() if k != "signals"}
            sigs, health, blobs = _snapshot(db, company, source_name)
            entry = {"company": company.legal_name, "company_id": company.id, "website_url": company.website_url,
                     "lane": lane, "source": source_name, "ran_at": datetime.utcnow().isoformat() + "Z",
                     "elapsed_s": elapsed, "adapter_result": slim, "source_health": health,
                     "signals": sigs, "blobs": blobs}
            report = [r for r in report if not (r["company_id"] == company.id and r["lane"] == lane)]
            report.append(entry)
            out_path.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
            summary = ", ".join("%s=%s/%s" % (s["signal_key"], s["numeric_value"], s["status"]) for s in sigs)
            print("    -> %s mode=%s in %ss; %d signal row(s): %s" % (slim.get("status"), slim.get("mode"), elapsed, len(sigs), summary), flush=True)
            if health and health.get("last_error"):
                print("    !! " + health["last_error"][:300], flush=True)
    db.close()
    print("\nReport: %s" % out_path)


if __name__ == "__main__":
    main()
