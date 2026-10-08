"""AI briefs must be built from stored evidence and keep valid citations."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import company_brief
from models import Base, Company, RawImportRecord


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

    monkeypatch.setattr(company_brief, "CRAWLER_LLM_API_KEY", "test-key")
    monkeypatch.setattr(company_brief.requests, "post", lambda *a, **kw: Response())
    brief = company_brief.generate_brief(db, company)
    assert brief["findings"] == [{"text": "Named winning supplier in a public notice.", "evidence_ids": ["E1"]}]
    assert brief["evidence"][0]["url"].startswith("https://ted.europa.eu/")
    saved = db.query(RawImportRecord).filter_by(company_id="c1", dataset_name=company_brief.DATASET_NAME).one()
    assert saved.raw_row["evidence_fingerprint"] == company_brief.evidence_fingerprint(
        company_brief.collect_evidence(db, company))
    db.close()
