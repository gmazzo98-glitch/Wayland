"""
Streamlit-rendering smoke test for the "Own-site Evidence" section of Company Intelligence's crawled
view (docs/CRAWLER_ROADMAP.md T25): quotes are shown with their page, archived reads are labelled,
and the Wayback dating line only appears when the archive actually dated the text.
"""

import textwrap
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from streamlit.testing.v1 import AppTest

from models import Base, Company, RawImportRecord

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


def _db(tmp_path):
    db_path = (tmp_path / "ui.db").as_posix()
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return db_path, sessionmaker(bind=engine)


def _render(db_path: str, company_id: str):
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {_PROJECT_ROOT!r})
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from models import Company
        from views.company_detail import _render_crawled_intelligence

        engine = create_engine({f"sqlite:///{db_path}"!r}, connect_args={{"check_same_thread": False}})
        db = sessionmaker(bind=engine)()
        _render_crawled_intelligence(db, db.query(Company).filter_by(id={company_id!r}).first())
    """)
    return AppTest.from_string(script).run(timeout=20)


def test_own_site_evidence_section_shows_quote_page_archive_label_and_dating(tmp_path):
    db_path, factory = _db(tmp_path)
    with factory() as db:
        db.add(Company(id="c1", legal_name="Acme SRL", registration_number="IT001", country="Italy", segment="SME"))
        db.add(RawImportRecord(company_id="c1", dataset_name="crawler_site_evidence", raw_row={
            "hits": [
                {"claim": "university_partnership", "quote": "Collaboriamo con il Politecnico di Torino su progetti di ricerca.",
                 "page_url": "https://acme.it/ricerca", "page_title": "Ricerca", "via": "live",
                 "first_seen": {"status": "ok", "first_seen_date": "2023-02-08",
                                "note": "earliest archived snapshot containing this exact text"}},
                {"claim": "open_innovation", "quote": "Nel 2022 abbiamo vinto il premio innovazione Smau.",
                 "page_url": "https://acme.it/news", "via": "wayback", "first_seen": {"status": "no_captures"}},
            ],
            "coverage": {"pages_read": 7},
        }))
        db.commit()

    app = _render(db_path, "c1")

    assert not app.exception
    text = " ".join(md.value for md in app.markdown)
    assert "Own-site Evidence" in text
    assert "University / research partnership" in text and "Politecnico di Torino" in text
    assert "https://acme.it/ricerca" in text
    assert "archived copy" in text                                   # the open_innovation hit was read from the archive
    captions = " ".join(c.value for c in app.caption)
    assert "since 2023-02-08" in captions and captions.count("Wayback Machine has had this text") == 1
    assert "Read from the company's own site: 7 page(s)" in captions


def test_no_section_when_nothing_was_found(tmp_path):
    db_path, factory = _db(tmp_path)
    with factory() as db:
        db.add(Company(id="c1", legal_name="Acme SRL", registration_number="IT001", country="Italy", segment="SME"))
        db.add(RawImportRecord(company_id="c1", dataset_name="crawler_site_evidence", raw_row={"hits": [], "coverage": {}}))
        db.commit()
    app = _render(db_path, "c1")
    assert not app.exception
    assert "Own-site Evidence" not in " ".join(md.value for md in app.markdown)
