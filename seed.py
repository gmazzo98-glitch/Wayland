"""
Seed Script for Project Vienna.
Populates sample Agrifood companies (Midcaps & SMEs) with realistic tri-state signals.
"""

import json
from datetime import datetime, timedelta
from database import init_db, get_db_session
from models import Company, SignalRecord, SourceHealth, PilotOutcome
from indicators import fetch_indicator_defs
from utils import normalize_registration_nr, normalize_handelsregister_nr

SAMPLE_COMPANIES = [
    {
        "legal_name": "AgriTech Innovationen GmbH",
        "registration_number": "HRB-104928",
        "nace_code": "A01.11",
        "sector_name": "Agrifood & Smart Farming",
        "country": "Germany",
        "website_url": "https://agritech-innovationen.de",
        "segment": "Midcap",
        "headcount": 420,
        "shortlist_status": "shortlisted",
        "signals": {
            "margin_compression": {"value": 14.5, "status": "present", "source": "Bundesanzeiger"},
            "sector_export_exposure": {"value": 0.72, "status": "present", "source": "Destatis"},
            "job_posting_velocity": {"value": 12.0, "status": "present", "source": "Arbeitsagentur"},
            "tech_stack_intensity": {"value": 8.0, "status": "present", "source": "Wappalyzer"},
            "rd_expense_ratio": {"value": 9.2, "status": "present", "source": "Bundesanzeiger"},
            "interest_coverage_ratio": {"value": 6.5, "status": "present", "source": "Bundesanzeiger"},
            "patent_count": {"value": 5.0, "status": "present", "source": "EPO OPS"},
            "patent_ipc_diversity": {"value": 3.0, "status": "present", "source": "EPO OPS"},
            "trademark_count": {"value": 3.0, "status": "present", "source": "EUIPO"},
            "public_grant_count": {"value": 2.0, "status": "present", "source": "EU Funding Portal"},
            "management_diversity": {"value": 4.0, "status": "present", "source": "Own-Site Scrape"},
            "kununu_rating": {"value": 4.2, "status": "present", "source": "Kununu Reseller"},
            "partnership_news_count": {"value": 4.0, "status": "present", "source": "Google News"},
        }
    },
    {
        "legal_name": "Bavaria BioAgrar AG",
        "registration_number": "HRB-883012",
        "nace_code": "A01.41",
        "sector_name": "Dairy & Livestock Tech",
        "country": "Germany",
        "website_url": "https://bavaria-bioagrar.de",
        "segment": "Midcap",
        "headcount": 610,
        "shortlist_status": "candidate",
        "signals": {
            "margin_compression": {"value": 18.0, "status": "present", "source": "Bundesanzeiger"},
            "sector_export_exposure": {"value": 0.65, "status": "present", "source": "Destatis"},
            "job_posting_velocity": {"value": 7.0, "status": "present", "source": "Arbeitsagentur"},
            "tech_stack_intensity": {"value": 4.0, "status": "present", "source": "Wappalyzer"},
            "rd_expense_ratio": {"value": 2.1, "status": "present", "source": "Bundesanzeiger"},
            "interest_coverage_ratio": None,
            "patent_count": {"value": 0.0, "status": "absent", "source": "EPO OPS"}, # Confirmed zero patents
            "patent_ipc_diversity": {"value": 0.0, "status": "absent", "source": "EPO OPS"},
            "trademark_count": {"value": 1.0, "status": "present", "source": "EUIPO"},
            "public_grant_count": {"value": 0.0, "status": "absent", "source": "EU Funding Portal"},
            "management_diversity": None,
            "kununu_rating": None,
            "partnership_news_count": {"value": 1.0, "status": "present", "source": "Google News"},
        }
    },
    {
        "legal_name": "Niedersachsen Saatgut & Processing GmbH",
        "registration_number": "HRB-452109",
        "nace_code": "C10.51",
        "sector_name": "Seed Technology & Processing",
        "country": "Germany",
        "website_url": "https://niedersachsen-saatgut.de",
        "segment": "SME",
        "headcount": 68,
        "shortlist_status": "candidate",
        "signals": {
            "margin_compression": {"value": 8.2, "status": "present", "source": "Bundesanzeiger"},
            "sector_export_exposure": {"value": 0.55, "status": "present", "source": "Destatis"},
            "job_posting_velocity": {"value": 3.0, "status": "present", "source": "Arbeitsagentur"},
            "tech_stack_intensity": {"value": 3.0, "status": "present", "source": "Wappalyzer"},
            "rd_expense_ratio": None, # Not checked yet
            "interest_coverage_ratio": None,
            "patent_count": {"value": 1.0, "status": "present", "source": "EPO OPS"},
            "patent_ipc_diversity": {"value": 1.0, "status": "present", "source": "EPO OPS"},
            "trademark_count": {"value": 2.0, "status": "present", "source": "EUIPO"},
            "public_grant_count": None,
            "management_diversity": None,
            "kununu_rating": None,
            "partnership_news_count": None,
        }
    },
    {
        "legal_name": "Greenhouse Automation & Vertical Farming GmbH",
        "registration_number": "HRB-670192",
        "nace_code": "A01.13",
        "sector_name": "Vertical Farming & Horticulture",
        "country": "Germany",
        "website_url": "https://greenhouse-automation.de",
        "segment": "Midcap",
        "headcount": 310,
        "shortlist_status": "in_pilot",
        "signals": {
            "margin_compression": {"value": 22.0, "status": "present", "source": "Bundesanzeiger"},
            "sector_export_exposure": {"value": 0.80, "status": "present", "source": "Destatis"},
            "job_posting_velocity": {"value": 15.0, "status": "present", "source": "Arbeitsagentur"},
            "tech_stack_intensity": {"value": 9.0, "status": "present", "source": "Wappalyzer"},
            "rd_expense_ratio": {"value": 11.5, "status": "present", "source": "Bundesanzeiger"},
            "interest_coverage_ratio": {"value": 9.0, "status": "present", "source": "Bundesanzeiger"},
            "patent_count": {"value": 7.0, "status": "present", "source": "EPO OPS"},
            "patent_ipc_diversity": {"value": 4.0, "status": "present", "source": "EPO OPS"},
            "trademark_count": {"value": 4.0, "status": "present", "source": "EUIPO"},
            "public_grant_count": {"value": 3.0, "status": "present", "source": "EU Funding Portal"},
            "management_diversity": {"value": 6.0, "status": "present", "source": "Own-Site Scrape"},
            "kununu_rating": {"value": 4.5, "status": "present", "source": "Kununu Reseller"},
            "partnership_news_count": {"value": 6.0, "status": "present", "source": "Google News"},
        }
    },
    {
        "legal_name": "Mecklenburg Landtechnik & Robotics KGaA",
        "registration_number": "HRA-310459",
        "nace_code": "A01.61",
        "sector_name": "Agricultural Machinery & Robotics",
        "country": "Germany",
        "website_url": "https://mecklenburg-robotics.de",
        "segment": "Midcap",
        "headcount": 540,
        "shortlist_status": "rejected",
        "signals": {
            "margin_compression": {"value": 11.0, "status": "present", "source": "Bundesanzeiger"},
            "sector_export_exposure": {"value": 0.60, "status": "present", "source": "Destatis"},
            "job_posting_velocity": {"value": 5.0, "status": "present", "source": "Arbeitsagentur"},
            "tech_stack_intensity": {"value": 6.0, "status": "present", "source": "Wappalyzer"},
            "rd_expense_ratio": {"value": 6.5, "status": "present", "source": "Bundesanzeiger"},
            "interest_coverage_ratio": {"value": 4.0, "status": "present", "source": "Bundesanzeiger"},
            "patent_count": {"value": 2.0, "status": "present", "source": "EPO OPS"},
            "patent_ipc_diversity": {"value": 2.0, "status": "present", "source": "EPO OPS"},
            "trademark_count": {"value": 1.0, "status": "present", "source": "EUIPO"},
            "public_grant_count": {"value": 1.0, "status": "present", "source": "EU Funding Portal"},
            "management_diversity": None,
            "kununu_rating": None,
            "partnership_news_count": {"value": 2.0, "status": "present", "source": "Google News"},
        }
    },
    {
        "legal_name": "AgroTech Italia S.p.A.",
        "registration_number": "IT09876543210",
        "nace_code": "A01.11",
        "sector_name": "Agrifood & Smart Farming",
        "country": "Italy",
        "website_url": "https://agrotech-italia.it",
        "segment": "Midcap",
        "headcount": 340,
        "shortlist_status": "candidate",
        "signals": {
            "tech_stack_intensity": {"value": 7.0, "status": "present", "source": "Wappalyzer"},
            "patent_count": {"value": 4.0, "status": "present", "source": "EPO OPS"},
            "patent_ipc_diversity": {"value": 2.0, "status": "present", "source": "EPO OPS"},
            "trademark_count": {"value": 3.0, "status": "present", "source": "EUIPO"},
            "public_grant_count": {"value": 2.0, "status": "present", "source": "EU Funding Portal"},
            "partnership_news_count": {"value": 3.0, "status": "present", "source": "Google News"},
        }
    }
]

def _seed_demo_pilot_outcomes(db, companies_by_name: dict, indicator_defs: dict):
    """
    Two illustrative PilotOutcome rows so calibration.py (Section 6's offline
    join/report script) has something to run against on a fresh seed, not
    just an empty table. need_score_at_start / signal_snapshot_json below are
    each company's CURRENT computed score used as a stand-in for a historical
    snapshot — there's no real score history in this demo dataset to back-date
    from, so started_at/ended_at are illustrative timestamps only, not a claim
    that scoring looked any different back then.
    """
    from scoring import calculate_company_scores

    def _snapshot(company):
        signals = db.query(SignalRecord).filter_by(company_id=company.id).all()
        scores = calculate_company_scores(signals, indicator_defs)
        snapshot = {
            sig.signal_key: {"value": sig.numeric_value, "status": sig.status}
            for sig in signals if sig.status != "not_yet_checked"
        }
        return scores, snapshot

    # A pilot already concluded (successfully) before this dataset's start.
    agritech = companies_by_name.get("AgriTech Innovationen GmbH")
    if agritech:
        scores, snapshot = _snapshot(agritech)
        db.add(PilotOutcome(
            company_id=agritech.id,
            pilot_label="AgriTech Innovationen - 2026 Q1 demand-forecasting pilot",
            started_at=datetime.utcnow() - timedelta(days=200),
            ended_at=datetime.utcnow() - timedelta(days=140),
            need_score_at_start=scores["need_score"],
            readiness_score_at_start=scores["readiness_score"],
            completeness_pct_at_start=scores["total_completeness_pct"],
            signal_snapshot_json=json.dumps(snapshot),
            outcome_success=True,
            outcome_metric=18.5,  # e.g. % forecast-accuracy improvement — metric definition is per-pilot
            scored_at=datetime.utcnow() - timedelta(days=140),
            notes="Demo/seed outcome for calibration.py — not a real GG engagement.",
        ))

    # A pilot currently in flight — outcome fields stay null until it concludes.
    greenhouse = companies_by_name.get("Greenhouse Automation & Vertical Farming GmbH")
    if greenhouse:
        scores, snapshot = _snapshot(greenhouse)
        db.add(PilotOutcome(
            company_id=greenhouse.id,
            pilot_label="Greenhouse Automation - climate-control optimization pilot",
            started_at=datetime.utcnow() - timedelta(days=25),
            ended_at=None,
            need_score_at_start=scores["need_score"],
            readiness_score_at_start=scores["readiness_score"],
            completeness_pct_at_start=scores["total_completeness_pct"],
            signal_snapshot_json=json.dumps(snapshot),
            outcome_success=None,
            outcome_metric=None,
            scored_at=None,
            notes="Demo/seed outcome for calibration.py — still running, not yet scored.",
        ))

    # A concluded, unsuccessful pilot — part of why this company is now
    # 'rejected' rather than back in the candidate pool. Gives calibration.py
    # at least one example on each side of outcome_success to compare.
    mecklenburg = companies_by_name.get("Mecklenburg Landtechnik & Robotics KGaA")
    if mecklenburg:
        scores, snapshot = _snapshot(mecklenburg)
        db.add(PilotOutcome(
            company_id=mecklenburg.id,
            pilot_label="Mecklenburg Landtechnik - 2025 predictive-maintenance pilot",
            started_at=datetime.utcnow() - timedelta(days=260),
            ended_at=datetime.utcnow() - timedelta(days=190),
            need_score_at_start=scores["need_score"],
            readiness_score_at_start=scores["readiness_score"],
            completeness_pct_at_start=scores["total_completeness_pct"],
            signal_snapshot_json=json.dumps(snapshot),
            outcome_success=False,
            outcome_metric=-4.0,  # negative = missed target, metric definition is per-pilot
            scored_at=datetime.utcnow() - timedelta(days=190),
            notes="Demo/seed outcome for calibration.py — stalled at approval stage, never fully staffed.",
        ))


def seed_database():
    init_db()
    db = get_db_session()

    try:
        # Clear existing entries
        db.query(PilotOutcome).delete()
        db.query(SignalRecord).delete()
        db.query(Company).delete()
        db.commit()

        indicator_defs = fetch_indicator_defs(db)
        companies_by_name = {}

        for comp_data in SAMPLE_COMPANIES:
            country = comp_data.get("country", "Germany")
            reg_nr = normalize_registration_nr(comp_data["registration_number"], country=country)
            status = comp_data["shortlist_status"]
            company = Company(
                legal_name=comp_data["legal_name"],
                registration_number=reg_nr,
                nace_code=comp_data["nace_code"],
                sector_name=comp_data["sector_name"],
                country=comp_data["country"],
                website_url=comp_data["website_url"],
                segment=comp_data["segment"],
                headcount=comp_data.get("headcount"),
                headcount_source_tier="T1" if comp_data.get("headcount") else None,
                shortlist_status=status,
                shortlisted_at=datetime.utcnow() if status in ("shortlisted", "in_pilot") else None,
            )
            db.add(company)
            db.flush()
            companies_by_name[comp_data["legal_name"]] = company

            signals_dict = comp_data.get("signals", {})
            for sig_key, defn in indicator_defs.items():
                if sig_key in signals_dict and signals_dict[sig_key] is not None:
                    sig_info = signals_dict[sig_key]
                    val = sig_info.get("value")
                    st = sig_info.get("status", "present")
                    source = sig_info.get("source", defn.get("source_system") or "Unknown")
                else:
                    val = None
                    st = "not_yet_checked"
                    source = defn.get("source_system") or "Unknown"

                # Stagger fetched_at to demonstrate stale checking
                fetched_at = datetime.utcnow()
                if sig_key == "job_posting_velocity" and company.segment == "SME":
                    fetched_at = datetime.utcnow() - timedelta(days=20) # Stale for 14d limit

                sig_rec = SignalRecord(
                    company_id=company.id,
                    signal_key=sig_key,
                    source=source,
                    numeric_value=val,
                    status=st,
                    confidence=0.9,
                    is_simulated=True,  # seed data is hand-authored demo data, never a real fetch
                    fetched_at=fetched_at,
                    raw_payload_ref=json.dumps({"seeded": True, "simulated": True, "signal_key": sig_key})
                )
                db.add(sig_rec)

        db.flush()
        _seed_demo_pilot_outcomes(db, companies_by_name, indicator_defs)

        db.commit()
        print("Database seeded successfully with sample Agrifood companies!")
    except Exception as e:
        db.rollback()
        print(f"Error seeding database: {e}")
        raise e
    finally:
        db.close()

if __name__ == "__main__":
    seed_database()
