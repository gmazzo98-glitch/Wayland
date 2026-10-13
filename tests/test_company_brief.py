"""AI briefs must be built from stored evidence and keep valid citations."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import company_brief
from models import Base, Company, RawImportRecord


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _company(db, **overrides):
    defaults = dict(id="c1", legal_name="Example GmbH", registration_number="R1")
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    return company


def test_generate_brief_discards_uncited_claims_and_saves_trace(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    company = Company(id="c1", legal_name="Example GmbH", registration_number="R1")
    db.add(company)
    db.add(RawImportRecord(company_id="c1", dataset_name="crawler_ted_contract_awards",
                           raw_row={"awards": [{"publication_number": "123-2025", "title": "Machines",
                                                 "buyer": "City", "winner": "Example GmbH",
                                                 "url": "https://ted.europa.eu/en/notice/-/detail/123-2025"}]}))
    db.commit()

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": '{"findings": ['
                '{"text": "Named winning supplier in a public notice.", "evidence_ids": ["E1"]},'
                '{"text": "Uncited revenue claim.", "evidence_ids": ["E999"]}'
                ']}'}}]}

    # company_brief reads config.crawler_llm_settings() fresh on every call now (a plain
    # module-level CRAWLER_LLM_API_KEY stayed stuck on whatever was configured the first time
    # anything imported this module - see that function's own docstring, docs/CRAWLER_ROADMAP.md
    # T24), so controlling what it sees means monkeypatching that function.
    monkeypatch.setattr(company_brief.config, "crawler_llm_settings", lambda: {
        "api_key": "test-key", "base_url": "https://api.groq.com/openai/v1", "model": "test-model",
        "fallback_api_key": None, "fallback_base_url": None, "fallback_model": None, "extra_fallbacks": [],
    })
    monkeypatch.setattr(company_brief.requests, "post", lambda *a, **kw: Response())
    brief = company_brief.generate_brief(db, company)
    assert brief["findings"] == [{"text": "Named winning supplier in a public notice.", "evidence_ids": ["E1"]}]
    assert brief["evidence"][0]["url"].startswith("https://ted.europa.eu/")
    saved = db.query(RawImportRecord).filter_by(company_id="c1", dataset_name=company_brief.DATASET_NAME).one()
    assert saved.raw_row["evidence_fingerprint"] == company_brief.evidence_fingerprint(
        company_brief.collect_evidence(db, company))
    db.close()


def test_collect_evidence_covers_every_crawled_dataset_not_just_the_original_narrow_four(db):
    """The actual live bug this fixes (docs/CRAWLER_ROADMAP.md T24's fourth follow-up): a
    young/small company with none of the original four narrow evidence types (AMPLIA's real
    case - 0 TED awards, no FDA relevance, no patents, no priced catalog products) came back
    with a completely EMPTY evidence list, so the whole "AI evidence brief" expander never
    even appeared - even though real crawled data existed for the company's own website,
    digital presence, hiring, directories, and a disclosed parent relationship."""
    company = _company(db, legal_name="AMPLIA ENGINEERING & EQUIPMENT S.R.L.", registration_number="IT03146140300")

    db.add(RawImportRecord(company_id="c1", dataset_name="crawler_company_website", raw_row={
        "homepage_url": "https://amplia-eng.it/",
        "product_types": ["attrezzature per ponti", "gru speciali"],
        "has_sustainability_report": {"present": False},
    }))
    db.add(RawImportRecord(company_id="c1", dataset_name="crawler_digital_maturity", raw_row={
        "homepage_url": "https://amplia-eng.it/", "cms_platform": None, "has_ecommerce": False,
        "social_presence_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/amplia"}],
        "snapshot_count_last_5_years": 0,
    }))
    db.add(RawImportRecord(company_id="c1", dataset_name="crawler_job_postings", raw_row={
        "careers_url": "https://amplia-eng.it/lavora-con-noi", "total_open_roles": 0, "roles_sample": [],
    }))
    db.add(RawImportRecord(company_id="c1", dataset_name="crawler_directory_anima", raw_row={
        "directory_name": "ANIMA", "appears_in_directory": True, "possible_match": True,  # fuzzy, NOT confirmed
        "listing_url": "https://www.anima.it/aziende/some-other-company.kl",
    }))
    db.add(RawImportRecord(company_id="c1", dataset_name="crawler_website_discovery", raw_row={
        "parent": {"name": "Autostrade per l’Italia", "matched_company_id": None, "matched_legal_name": None},
    }))
    db.commit()

    evidence = company_brief.collect_evidence(db, company)
    kinds = {item["kind"] for item in evidence}
    assert kinds == {
        "company_website_product_lines", "digital_presence", "hiring_signal", "parent_group_relationship",
    }
    # The fuzzy ANIMA match must NOT appear as evidence - only a CONFIRMED listing should.
    assert not any(item["kind"] == "trade_directory_listing" for item in evidence)
    parent_item = next(item for item in evidence if item["kind"] == "parent_group_relationship")
    assert "NOT a separate company record" in parent_item["details"]


def test_collect_evidence_reports_a_confirmed_directory_listing(db):
    company = _company(db)
    db.add(RawImportRecord(company_id="c1", dataset_name="crawler_directory_mecspe", raw_row={
        "directory_name": "MECSPE", "appears_in_directory": True, "possible_match": False,
        "listing_url": "https://www.mecspe.com/espositori/example-gmbh", "membership_or_exhibitor_since": "2024",
    }))
    db.commit()

    evidence = company_brief.collect_evidence(db, company)
    assert len(evidence) == 1
    assert evidence[0]["kind"] == "trade_directory_listing"
    assert evidence[0]["url"] == "https://www.mecspe.com/espositori/example-gmbh"


def test_collect_evidence_links_a_matched_parent_company_distinctly_from_an_unmatched_one(db):
    company = _company(db)
    db.add(RawImportRecord(company_id="c1", dataset_name="crawler_website_discovery", raw_row={
        "parent": {"name": "BigCo Holding", "matched_company_id": "parent1", "matched_legal_name": "BigCo Holding S.p.A."},
    }))
    db.commit()

    evidence = company_brief.collect_evidence(db, company)
    assert len(evidence) == 1
    assert "BigCo Holding S.p.A." in evidence[0]["label"]
    assert "matches a real company record" in evidence[0]["details"]


def test_collect_evidence_returns_empty_when_nothing_was_ever_crawled(db):
    company = _company(db)
    assert company_brief.collect_evidence(db, company) == []
