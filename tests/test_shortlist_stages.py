"""The company funnel has one definition (models.SHORTLIST_STAGES) and parked companies stay out of working lists."""

import pytest

from database import ScopedSession, init_db
from models import (Company, SHORTLIST_STAGES, SHORTLIST_STATUSES, is_parked, is_shortlisted, set_shortlist_status,
                    stage_label)
from site_evidence_hub import build_company_list


def test_every_status_has_a_label_and_a_meaning():
    assert set(SHORTLIST_STAGES) == set(SHORTLIST_STATUSES)
    assert all(label and meaning for label, meaning in SHORTLIST_STAGES.values())


def test_shortlisted_and_in_pilot_are_unlocked_candidate_and_parked_are_not():
    assert [s for s in SHORTLIST_STATUSES if is_shortlisted(s)] == ["shortlisted", "in_pilot"]
    assert is_parked("rejected") and not is_parked("candidate")
    assert stage_label("rejected") == "Parked"


def test_set_status_stamps_shortlisted_at_once_and_rejects_unknown_values():
    c = Company(legal_name="A", registration_number="r1")
    set_shortlist_status(c, "shortlisted")
    stamped = c.shortlisted_at
    assert stamped is not None
    set_shortlist_status(c, "in_pilot")
    assert c.shortlisted_at == stamped
    with pytest.raises(ValueError):
        set_shortlist_status(c, "maybe")


def test_parked_companies_are_left_out_of_the_crawl_list():
    init_db()
    db = ScopedSession()
    try:
        for reg, status in (("p-1", "candidate"), ("p-2", "shortlisted"), ("p-3", "rejected")):
            db.add(Company(legal_name=reg, registration_number=reg, website_url=f"https://{reg}.example",
                           shortlist_status=status))
        db.commit()
        csv_text, kept, _ = build_company_list(db)
        assert "p-1" in csv_text and "p-2" in csv_text and "p-3" not in csv_text
        assert csv_text.index("p-2") < csv_text.index("p-1")  # shortlist first
    finally:
        db.query(Company).filter(Company.registration_number.in_(["p-1", "p-2", "p-3"])).delete(synchronize_session=False)
        db.commit()
        db.close()
