"""
Writes docs/indicator_catalog.json: every indicator in the catalog (indicators.py's
INDICATOR_SEED), merged with scripts/indicator_gaps.py's GAP_PLAN classification of how
each one is currently wired up (or isn't).

This is the machine-readable replacement for handing GG_Indicators_Structured.xlsx to an
agent at the start of every session. It never needs the live database — everything in it
is static repo metadata — so it's safe to commit and cheap to regenerate. It answers, per
indicator: what it is (label/category/axis/weight/gate/tier), where its data is supposed
to come from (source_system, a legacy grouping tag), and its CURRENT wiring status per
GAP_PLAN (LIVE/COMPUTE/IMPORT/RUN/GATED/NOT_ITALY/BUILD/MANUAL) with the producer file
where one exists.

For live per-company coverage percentages against the real database, use
scripts/indicator_gaps.py instead (or pass --live here to merge both into one file).

Usage:
    python scripts/generate_indicator_catalog.py             # writes docs/indicator_catalog.json
    python scripts/generate_indicator_catalog.py --live       # also merges live coverage %
    python scripts/generate_indicator_catalog.py --check      # exit 1 if the committed file is stale
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indicators import INDICATOR_SEED  # noqa: E402

_spec_path = Path(__file__).resolve().parent / "indicator_gaps.py"
import importlib.util
_spec = importlib.util.spec_from_file_location("indicator_gaps", _spec_path)
gaps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gaps)

OUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "indicator_catalog.json"

# Substring -> producer file, matched against each GAP_PLAN row's "where" text. Order matters
# (first match wins) since some substrings nest (e.g. "AIDA" inside a longer phrase).
PRODUCER_FILE_HINTS = [
    ("sync_management_composition_signals", "company_service.py"),
    ("sync_succession_signal", "company_service.py"),
    ("company_people roster", "company_service.py"),
    ("company_people", "company_service.py"),
    ("AIDA raw", "aida_import.py"),
    ("AIDA import", "aida_import.py"),
    ("AIDA re-export", "aida_import.py"),
    ("WAYLAND_", "aida_import.py"),
    ("EPO OPS", "adapters/epo_ops.py"),
    ("EUIPO", "adapters/euipo.py"),
    ("EU Funding", "adapters/eu_funding.py"),
    ("Eurostat", "adapters/eurostat_sector_growth.py"),
    ("Destatis", "adapters/destatis.py"),
    ("Arbeitsagentur", "adapters/arbeitsagentur.py"),
    ("Google News RSS", "adapters/google_news_rss.py"),
    ("RSS adapter", "adapters/google_news_rss.py"),
    ("Wappalyzer", "scrapers/wappalyzer_local.py"),
    ("own-site", "scrapers/management_diversity.py"),
    ("digital-maturity crawler", "scrapers/digital_maturity_crawler.py"),
    ("job-postings crawler", "scrapers/job_postings_crawler.py"),
    ("job crawler", "scrapers/job_postings_crawler.py"),
    ("directory-listing crawler", "scrapers/directory_listing_crawler.py"),
    ("company-website crawler", "scrapers/company_website_crawler.py"),
    ("company-website LLM", "scrapers/company_website_crawler.py"),
    ("website LLM", "scrapers/company_website_crawler.py"),
    ("website crawler", "scrapers/company_website_crawler.py"),
    ("news-signals-crawler", "scrapers/news_signals_crawler.py"),
    ("LinkedIn", "scrapers/linkedin_profile_crawler.py"),
    ("Kununu", "scrapers/kununu_light.py"),
    ("Google Maps", "scrapers/review_crawler.py"),
    ("Bundesanzeiger", "scrapers/bundesanzeiger_paid.py"),
    ("Handelsregister", "scrapers/handelsregister_free.py"),
]

CLASS_MEANING = {
    "LIVE": "Real data already populated for most companies.",
    "COMPUTE": "The inputs are already in the database; a compute pass over them writes the signal (no network call).",
    "IMPORT": "The inputs sit in files already on disk (the raw AIDA exports); mapping the columns writes the signal, no new pull.",
    "RUN": "A working, free adapter/crawler exists and simply hasn't been run over the company list yet (not an execution/scheduling gap, not a missing scraper).",
    "GATED": "Built, but deliberately blocked by a missing key, an off-by-default flag, or a policy/budget decision (e.g. LinkedIn, Kununu, Google Maps ToS).",
    "NOT_ITALY": "A producer exists but is Germany-only (Destatis/Arbeitsagentur/Bundesanzeiger); the Italian cohort needs an equivalent free source.",
    "BUILD": "No producer yet. A concrete free source exists (see status_note) but nothing extracts it today.",
    "MANUAL": "Tier T3 by design: collected by hand in the first-contact questionnaire after shortlisting, not automatable.",
    "UNCLASSIFIED": "Not yet triaged in GAP_PLAN (scripts/indicator_gaps.py) - add it there.",
}


def producer_file_for(where: str):
    for needle, path in PRODUCER_FILE_HINTS:
        if needle.lower() in where.lower():
            return path
    return None


def build_catalog(live_counts=None, total=None):
    indicators = []
    for d in INDICATOR_SEED:
        key = d["key"]
        cls, where, effort = gaps.GAP_PLAN.get(key, ("UNCLASSIFIED", "not in GAP_PLAN yet - add it there", ""))
        row = {
            "key": key,
            "label": d["label"],
            "category": d.get("category"),
            "axis": d.get("axis"),
            "weight": d.get("weight", 0.0),
            "is_gate": bool(d.get("is_gate", False)),
            "invert": bool(d.get("invert", False)),
            "phase": d.get("phase"),
            "automation_tier": d.get("automation_tier"),
            "redundancy_group": d.get("redundancy_group"),
            "axis_modifier": d.get("axis_modifier") or None,
            "source_system": d.get("source_system"),
            "proxy": d.get("proxy"),
            "rationale": d.get("rationale"),
            "comment": d.get("comment"),
            "source_description": d.get("source_description"),
            "freshness_days": d.get("freshness_days"),
            "raw_min": d.get("raw_min"),
            "raw_max": d.get("raw_max"),
            "status": cls,
            "status_note": where,
            "status_effort": effort or None,
            "producer_file": producer_file_for(where),
        }
        if live_counts is not None:
            real = live_counts.get(key, 0)
            row["live_companies_with_real_value"] = real
            row["live_coverage_pct"] = round(100 * real / total) if total else 0
        indicators.append(row)
    return indicators


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="also connect to DATABASE_URL and merge live coverage %%")
    ap.add_argument("--check", action="store_true", help="exit 1 if docs/indicator_catalog.json is stale instead of writing it")
    args = ap.parse_args()

    live_counts = total = None
    if args.live:
        import os
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        url = os.environ["DATABASE_URL"].replace(":5432/", ":6543/")
        total, live_counts = gaps.real_counts(gaps._connect(url))

    indicators = build_catalog(live_counts, total)
    doc = {
        "$comment": "Generated by scripts/generate_indicator_catalog.py - do not hand-edit. "
                     "Re-run it after changing indicators.py's INDICATOR_SEED or scripts/indicator_gaps.py's GAP_PLAN.",
        "indicator_count": len(indicators),
        "status_meaning": CLASS_MEANING,
        "axis_meaning": {
            "need": "Scores how much the company needs help (the problem side).",
            "readiness": "Scores how ready the company is to act on a pilot (the capability side). Never blended with need.",
            "both": "Scores on both axes independently.",
            "context": "Not scored (weight 0). Segment tag, trend context, or a computed passthrough.",
        },
        "tier_meaning": {
            "T1": "Structured register/API - deterministic, no interpretation needed.",
            "T2": "Scrape + extract - needs a crawler and/or LLM extraction over unstructured pages.",
            "T3": "Manual - first-contact interview, tier PHASE_MANUAL, not automatable by design.",
        },
        "indicators": indicators,
    }

    rendered = json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=False) + "\n"

    if args.check:
        current = OUT_PATH.read_text(encoding="utf-8") if OUT_PATH.exists() else ""
        if current != rendered:
            print(f"{OUT_PATH} is stale - run: python scripts/generate_indicator_catalog.py")
            sys.exit(1)
        print(f"{OUT_PATH} is up to date")
        return

    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUT_PATH} ({len(indicators)} indicators)")


if __name__ == "__main__":
    main()
