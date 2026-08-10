"""
Seed Script for Project Vienna.
Populates sample Agrifood companies (Midcaps & SMEs) with realistic tri-state signals.
"""

import json
from datetime import datetime, timedelta
from database import init_db, get_db_session
from models import Company, SignalRecord, SourceHealth
from config import SIGNAL_METADATA
from utils import normalize_handelsregister_nr

SAMPLE_COMPANIES = [
    {
        "legal_name": "AgriTech Innovationen GmbH",
        "registration_number": "HRB-104928",
        "nace_code": "A01.11",
        "sector_name": "Agrifood & Smart Farming",
        "country": "Germany",
        "website_url": "https://agritech-innovationen.de",
        "segment": "Midcap",
        "is_shortlisted": True,
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
        "is_shortlisted": False,
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
        "is_shortlisted": False,
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
        "is_shortlisted": True,
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
        "is_shortlisted": False,
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
    }
]

def seed_database():
    init_db()
    db = get_db_session()
    
    try:
        # Clear existing entries
        db.query(SignalRecord).delete()
        db.query(Company).delete()
        db.commit()

        for comp_data in SAMPLE_COMPANIES:
            reg_nr = normalize_handelsregister_nr(comp_data["registration_number"])
            company = Company(
                legal_name=comp_data["legal_name"],
                registration_number=reg_nr,
                nace_code=comp_data["nace_code"],
                sector_name=comp_data["sector_name"],
                country=comp_data["country"],
                website_url=comp_data["website_url"],
                segment=comp_data["segment"],
                is_shortlisted=comp_data["is_shortlisted"],
                shortlisted_at=datetime.utcnow() if comp_data["is_shortlisted"] else None
            )
            db.add(company)
            db.flush()

            signals_dict = comp_data.get("signals", {})
            for sig_key in SIGNAL_METADATA.keys():
                meta = SIGNAL_METADATA[sig_key]
                if sig_key in signals_dict and signals_dict[sig_key] is not None:
                    sig_info = signals_dict[sig_key]
                    val = sig_info.get("value")
                    st = sig_info.get("status", "present")
                    source = sig_info.get("source", meta["source"])
                else:
                    val = None
                    st = "not_yet_checked"
                    source = meta["source"]

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
