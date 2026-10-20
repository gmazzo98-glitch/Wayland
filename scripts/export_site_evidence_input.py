"""
Writes the company list that Scraper/tools/site_evidence reads (company_id = registration number,
homepage_url, name). Read-only on the database.

    python scripts/export_site_evidence_input.py                      # every company with a website
    python scripts/export_site_evidence_input.py --shortlist-first    # shortlisted / in-pilot companies first
    python scripts/export_site_evidence_input.py --skip-imported      # leave out companies already imported
    python scripts/export_site_evidence_input.py --out some\\file.csv

Default output is %USERPROFILE%\\SiteEvidenceOutput\\companies.csv, the folder run_site_evidence.bat uses.
The tool skips companies it has already finished, so re-exporting a longer list later is safe.
"""

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import get_db_session                                  # noqa: E402
from models import Company, RawImportRecord                          # noqa: E402
from scrapers.site_evidence_reader import DATASET_NAME               # noqa: E402

PRIORITY_STATUSES = ("shortlisted", "in_pilot")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(Path.home() / "SiteEvidenceOutput" / "companies.csv"))
    ap.add_argument("--shortlist-first", action="store_true")
    ap.add_argument("--skip-imported", action="store_true")
    args = ap.parse_args()

    db = get_db_session()
    rows = db.query(Company.id, Company.registration_number, Company.website_url, Company.legal_name,
                    Company.shortlist_status).all()
    imported = set()
    if args.skip_imported:
        imported = {cid for (cid,) in db.query(RawImportRecord.company_id).filter_by(dataset_name=DATASET_NAME)}

    keep = [r for r in rows if (r.website_url or "").strip() and (r.registration_number or "").strip() and r.id not in imported]
    keep.sort(key=lambda r: (0 if args.shortlist_first and r.shortlist_status in PRIORITY_STATUSES else 1, r.registration_number))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["company_id", "homepage_url", "name"])
        for r in keep:
            writer.writerow([r.registration_number, r.website_url.strip(), r.legal_name])
    print(f"{len(keep)} companies written to {out}  ({len(rows) - len(keep)} left out: no website, "
          f"no registration number{', or already imported' if args.skip_imported else ''}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
