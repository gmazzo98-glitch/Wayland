"""
Offline unit tests for the documents collector and the PDF parsing helpers (ticket I11).

No network: the collector runs against recorded pages (a downloads listing, sample certificate,
manual and report texts taken from real company downloads) through a fake fetcher, exactly like
tests/test_intel_events.py. The ledger rules are exercised through Ledger.add_many.
"""

from __future__ import annotations

import pytest

from intel.collector import CollectorContext, check_result
from intel.collectors.documents import COLLECTOR, run
from intel.fetch import Page
from intel.ledger import Ledger, quote_in_text
from intel import pdf

HOMEPAGE = """Home
Company
Contact
Download
https://www.example-machinery.com/Home/Company
https://www.example-machinery.com/Home/Contatti
https://www.example-machinery.com/Home/Download
"""

DOWNLOAD_PAGE = """Download
Certifications
Our certifications are available for download
ISO 9001:2015 certificate
ISO 45001:2018 certificate
U-STAMP certificate
Manuals
IOMS-BFL-001(17)
IOMS-CPV-001(17)
Links:
https://www.example-machinery.com/docs/ISO_9001-ENG-2007-AQ-ITA-SINCERT-13-20260629.pdf
https://www.example-machinery.com/docs/ISO_45001-ENG-10000421741-MSC-ACCREDIA-ITA-2-20241127.pdf
https://www.example-machinery.com/docs/certificate-U-STAMP-2026-2029.pdf
https://www.example-machinery.com/docs/IOMS-BFL-001(17).pdf
https://www.example-machinery.com/docs/IOMS-CPV-001(17).pdf
https://www.example-machinery.com/docs/Annual-Report.pdf
https://www.example-machinery.com/docs/scanned-policy.pdf
"""

CERT_ISO9001 = """CERTIFICATE
Certificate number: 6957-2007-AQ-ITA-SINCERT
This is to certify that Example Machinery Spa
operates a quality management system complying with
ISO 9001:2015
Place and date: Vimercate (MB), 27 November 2024
DNV - Business Assurance
Expiry date: 29 June 2026
"""

CERT_USTAMP = """CERTIFICATE OF AUTHORIZATION
The named company is authorized by The American Society of Mechanical Engineers
AUTHORIZED: May 01, 2026  EXPIRES: May 01, 2029
NUMBER: 46567
National Board
"""

MANUAL_BFL = """MANUALE D'USO E MANUTENZIONE
DOC NUMBER: IOMS-BFL-001 (17)
OPERATING AND MAINTENANCE MANUAL
FILTRI A SACCO SERIE BFL
BAG FILTERS BFL SERIES
"""

ANNUAL_REPORT = """Annual Report 2024
The company closed the year with revenue of 45.2 million EUR.
Employees: 120 people
EBITDA of 6.1 million was reported.
"""

BAD_PAGE_TEXT = """Welcome
We build machines since 1962. No documents are linked here.
Contact us for more information.
"""


class RecordedFetcher:
    """Mock fetcher returning recorded pages without network access."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.fetched_urls: list[str] = []

    def __call__(self, url: str) -> Page:
        self.fetched_urls.append(url)
        if url in self.pages:
            text = self.pages[url]
            return Page(url=url, ok=True, status=200, text=text,
                        links=[l.strip() for l in text.splitlines() if l.startswith("http")])
        return Page(url=url, ok=False, status=404, note="Not recorded")


def _company() -> dict:
    return {"id": "test-1", "legal_name": "EXAMPLE MACHINERY SPA", "vat": "IT01234567890",
            "website_url": "https://www.example-machinery.com", "country": "Italy", "city": "Binasco"}


def _pages() -> dict[str, str]:
    return {
        "https://www.example-machinery.com": HOMEPAGE,
        "https://www.example-machinery.com/Home/Download": DOWNLOAD_PAGE,
        "https://www.example-machinery.com/docs/ISO_9001-ENG-2007-AQ-ITA-SINCERT-13-20260629.pdf": CERT_ISO9001,
        "https://www.example-machinery.com/docs/ISO_45001-ENG-10000421741-MSC-ACCREDIA-ITA-2-20241127.pdf": CERT_ISO9001,
        "https://www.example-machinery.com/docs/certificate-U-STAMP-2026-2029.pdf": CERT_USTAMP,
        "https://www.example-machinery.com/docs/IOMS-BFL-001(17).pdf": MANUAL_BFL,
        "https://www.example-machinery.com/docs/IOMS-CPV-001(17).pdf": MANUAL_BFL,
        "https://www.example-machinery.com/docs/Annual-Report.pdf": ANNUAL_REPORT,
        "https://www.example-machinery.com/docs/scanned-policy.pdf": " ",
    }


def _run(pages: dict[str, str]):
    fetcher = RecordedFetcher(pages)
    ctx = CollectorContext(company=_company(), ledger=Ledger("test-1"), fetch=fetcher)
    result = run(ctx)
    ledger = Ledger("test-1")
    refused = ledger.add_many(result.facts)
    return result, ledger, refused, ctx


# ---- pdf.py helpers ---------------------------------------------------------------------------------


def test_classify_document_kinds():
    assert pdf.classify_document(CERT_ISO9001, "cert.pdf") == "certificate"
    assert pdf.classify_document(MANUAL_BFL, "IOMS-BFL-001.pdf") == "manual"
    assert pdf.classify_document(ANNUAL_REPORT, "report.pdf") == "report"
    assert pdf.classify_document("   ", "scan.pdf") == "scanned"


def test_certificate_extraction_dates_and_issuer():
    doc = pdf.extract_certificate(CERT_ISO9001, "ISO_9001-ENG-2007-AQ-ITA-SINCERT-13-20260629.pdf")
    assert doc.standard.startswith("iso 9001")
    assert doc.issuer == "DNV"
    assert doc.expires_on == "2026-06-29"
    assert doc.issued_on == "2024-11-27"


def test_certificate_date_from_filename_when_text_is_masked():
    text = "******** **** ******** *** ****"
    doc = pdf.extract_certificate(text, "ISO_9001-ENG-13-20260629.pdf")
    assert doc.standard == "iso 9001"          # from the file name: text layer is masked
    assert doc.expires_on == "2026-06-29"
    assert doc.date_source == "file_name"


def test_module_h1_not_labelled_as_module_h():
    doc = pdf.extract_certificate("MODULO H1 certificate", "PED-H1.pdf")
    assert doc.standard == "ped module h1"
    doc = pdf.extract_certificate("MODULO H certificate", "PED-H.pdf")
    assert doc.standard == "ped module h"


def test_manual_family_code():
    doc = pdf.extract_manual(MANUAL_BFL, "IOMS-BFL-001(17).pdf")
    assert doc.family_code.startswith("IOMS-BFL-001")
    assert "BFL" in doc.title


def test_report_figures_from_first_pages():
    doc = pdf.extract_report(ANNUAL_REPORT, "Annual-Report.pdf")
    assert doc.year == 2024
    assert "45.2" in doc.figures["revenue"]
    assert doc.figures["employees"] == "120"


def test_select_pages_bounds_excerpt():
    long_text = ("page one with a certificate\n" * 50) + "\f" + ("filler\n" * 400)
    excerpt = pdf.select_pages(long_text, max_chars=2000)
    assert len(excerpt) <= 2000
    assert "certificate" in excerpt


# ---- collector --------------------------------------------------------------------------------------


def test_collector_discovers_documents_and_emits_facts():
    result, ledger, refused, ctx = _run(_pages())
    assert not refused, refused
    assert not check_result(COLLECTOR, result)
    keys = {f.key for f in ledger.present()}
    assert "documents.published" in keys
    assert "documents.item" in keys
    assert "certifications.claimed" in keys
    assert "products.manual" in keys
    assert "financials.report" in keys
    published = [f for f in ledger.present("documents.published")][0]
    assert published.value["total_pdfs"] == 7


def test_every_fact_quote_grounded():
    result, ledger, refused, ctx = _run(_pages())
    pages = ctx.fetch.pages if hasattr(ctx.fetch, "pages") else _pages()
    for fact in ledger.facts.values():
        for ev in fact.evidence:
            if ev.quote and fact.key != "documents.published":
                assert ev.url, f"{fact.key}: evidence without url"
    iso = [f for f in ledger.present("certifications.claimed")
           if f.value["standard"].startswith("iso 9001")]
    assert iso, "ISO 9001 must be claimed from its certificate PDF"
    assert iso[0].evidence[0].quote and quote_in_text(iso[0].evidence[0].quote, CERT_ISO9001)


def test_scanned_pdf_reported_not_read():
    result, ledger, refused, ctx = _run(_pages())
    scanned = [f for f in ledger.present("documents.item") if f.value.get("kind") == "scanned"]
    assert scanned, "a scanned PDF must be recorded as scanned, not invented"
    assert any("scanned" in e for e in result.errors)


def test_honest_absence_when_no_documents():
    fetcher = RecordedFetcher({"https://www.example-machinery.com": BAD_PAGE_TEXT})
    ctx = CollectorContext(company=_company(), ledger=Ledger("test-1"), fetch=fetcher)
    result = run(ctx)
    assert result.facts == []
    assert "documents.item" in result.checked_absent
    assert result.errors and "no download/documents page" in result.errors[0]


def test_no_hardcoded_golden_company():
    """The collector must not special-case ASCO Filtri: a generic site works the same way."""
    source = open("intel/collectors/documents.py", encoding="utf-8").read().lower()
    for token in ("ascofiltri", "asco filtri", "asco filtri", "mott", "02897031", "binasco"):
        assert token not in source, f"collector must not hard-code {token!r}"


def test_collector_registered():
    from intel.collector import discover
    registry = discover()
    assert "documents" in registry
    assert registry["documents"].ticket == "I11"
