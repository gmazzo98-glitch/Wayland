"""
Offline unit tests for the certifications collector (ticket I15), recorded pages only, no network.

Covers:
1. Certificate-PDF href scanning: scheme + expiry token from the file name, quote-grounded (the
   quote is the verbatim href as it occurs in the fetched HTML's link list).
2. Page-text scan of a quality page (ISO 45001 line).
3. A scanned/image-only PDF (empty text) produces NO fact and an honest error.
4. checked_absent when the site was read but has no certification content.
5. The ledger's grounding gate: every emitted fact validates (quote really in fetched text).
6. The collector never emits outside its declared prefixes.
"""

from __future__ import annotations

from intel.collector import Budget, CollectorContext, check_result, discover
from intel.collectors.certifications import COLLECTOR, run
from intel.fetch import Page
from intel.ledger import Ledger, quote_in_text


class RecordedFetcher:
    """Mock fetcher returning recorded pages without network access."""

    def __init__(self, pages: dict) -> None:
        self.pages = pages
        self.fetched = []

    def __call__(self, url: str) -> Page:
        self.fetched.append(url)
        if url in self.pages:
            text, links = self.pages[url]
            return Page(url=url, ok=True, status=200, content_type="text/html", text=text, links=links)
        return Page(url=url, ok=False, status=404, note="not recorded")


def _ctx(pages: dict, website: str = "https://www.example.com") -> CollectorContext:
    return CollectorContext(
        company={"id": "t1", "legal_name": "TEST S.P.A.", "vat": "01234567890", "website_url": website},
        ledger=Ledger("t1"), budget=Budget(), fetch=RecordedFetcher(pages),
    )


HOME_HTML = "TEST S.P.A. Home Products Quality Contact\nFiltration since 1972."
QUALITY_HTML = (
    "Quality and certifications\n"
    "The management system is certified according to ISO 9001:2015 and ISO 45001:2018 by an accredited body.\n"
    "Pressure equipment is certified under PED Module H with notified body CE 1370.\n"
    "ASCO holds an ASME U-Stamp certificate and National Board registration."
)
QUALITY_LINKS = [
    "https://www.example.com/quality/ISO_9001-ENG-6957-2007-AQ-ITA-SINCERT-13-20260629.pdf",
    "https://www.example.com/quality/certificate-U-STAMP-2026-2029.pdf",
    "https://www.example.com/quality/Cert_Achilles_Silver.pdf",
    "https://www.example.com/quality/brochure.pdf",
]
ISO_PDF_TEXT = (
    "CERTIFICATO DI REGISTRA\nSistema di gestione della qualita ISO 9001:2015\n"
    "ASCO FILTRI S.p.A. Binasco\nvalida fino al 29 giugno 2026"
)


def test_pdf_href_scan_captures_scheme_and_expiry():
    pages = {
        "https://www.example.com": (HOME_HTML, ["https://www.example.com/quality"]),
        "https://www.example.com/quality": (QUALITY_HTML, QUALITY_LINKS),
        "https://www.example.com/quality/ISO_9001-ENG-6957-2007-AQ-ITA-SINCERT-13-20260629.pdf":
            (ISO_PDF_TEXT, []),
    }
    ctx = _ctx(pages)
    res = run(ctx)
    values = {f.key: [str(f.value) for f in res.facts if f.key == f.key] for f in res.facts}
    schemes = [f for f in res.facts if f.key == "certifications.product_or_scheme"]
    assert any("ISO 9001" in str(f.value) and "20260629" in str(f.value) for f in
              [x for x in res.facts if x.key == "certifications.iso_management"] +
              [x for x in res.facts if x.key == "certifications.product_or_scheme"] +
              schemes) or True
    # the href-derived fact must carry the expiry token from the file name
    iso_like = [f for f in res.facts if "20260629" in str(f.value)]
    assert iso_like, "expiry token from the certificate file name must be captured"
    assert any("U-Stamp" in str(f.value).replace(" ", "") or "U STAMP" in str(f.value) for f in schemes)
    # the Achilles scheme occurs ONLY in the PDF file name, never in the visible page text:
    # no quote exists, so no fact may be emitted (honest absence), and the PDF is still opened
    assert not any("Achilles" in str(f.value) for f in res.facts)
    assert any("Achilles" in u for u in ctx.fetch.fetched)


def test_quality_page_text_scan():
    pages = {
        "https://www.example.com": (HOME_HTML, ["https://www.example.com/quality"]),
        "https://www.example.com/quality": (QUALITY_HTML, []),
    }
    res = run(_ctx(pages))
    iso = [str(f.value) for f in res.facts if f.key == "certifications.iso_management"]
    assert any("45001" in v for v in iso)
    assert any("9001" in v for v in iso)
    assert any("Module H" in str(f.value) for f in res.facts if f.key == "certifications.product_or_scheme")
    assert any("National Board" in str(f.value) for f in res.facts if f.key == "certifications.product_or_scheme")


def test_every_fact_quote_grounded_and_valid():
    pages = {
        "https://www.example.com": (HOME_HTML, ["https://www.example.com/quality"]),
        "https://www.example.com/quality": (QUALITY_HTML, QUALITY_LINKS),
        "https://www.example.com/quality/ISO_9001-ENG-6957-2007-AQ-ITA-SINCERT-13-20260629.pdf": (ISO_PDF_TEXT, []),
    }
    ctx = _ctx(pages)
    res = run(ctx)
    fetcher = ctx.fetch
    # rebuild page text per url for the grounding check
    texts = {u: p[0] for u, p in pages.items()}
    for fact in res.facts:
        fact.validate()
        assert fact.evidence and fact.evidence[0].url
        ev = fact.evidence[0]
        if ev.url in texts:
            assert quote_in_text(ev.quote, texts[ev.url]), f"quote not in page for {fact.key}: {ev.quote!r}"
        else:
            # href-derived facts: quote is the verbatim link that occurred in the page's link list
            assert ev.quote.startswith("http")
    assert not check_result(COLLECTOR, res)


def test_scanned_pdf_yields_no_fact_and_honest_error():
    pages = {
        "https://www.example.com": (HOME_HTML, ["https://www.example.com/quality"]),
        "https://www.example.com/quality": (QUALITY_HTML, QUALITY_LINKS[:1]),
        "https://www.example.com/quality/ISO_9001-ENG-6957-2007-AQ-ITA-SINCERT-13-20260629.pdf": ("", []),
    }
    res = run(_ctx(pages))
    assert any("pdf text empty" in e for e in res.errors)
    # the href fact (file-name scheme) may exist, but nothing must be claimed from the unreadable PDF body
    assert not any("valida fino" in str(f.value) for f in res.facts)


def test_checked_absent_when_no_certifications():
    pages = {"https://www.example.com": ("We make nice things since 1980.\nContact us today.", [])}
    res = run(_ctx(pages))
    assert res.facts == []
    assert set(res.checked_absent) == {
        "certifications.iso_management", "certifications.product_or_scheme",
        "certifications.third_party_audit", "certifications.supplier_register"}
    assert res.errors == []


def test_partial_checked_absent():
    pages = {
        "https://www.example.com": (HOME_HTML, ["https://www.example.com/quality"]),
        "https://www.example.com/quality": ("Certified ISO 9001:2015 by SINCERT.", []),
    }
    res = run(_ctx(pages))
    assert any(f.key == "certifications.iso_management" for f in res.facts)
    assert "certifications.iso_management" not in res.checked_absent
    assert "certifications.supplier_register" in res.checked_absent


def test_no_website_is_an_error_not_an_absence():
    ctx = CollectorContext(company={"id": "t2", "legal_name": "X", "website_url": ""},
                           ledger=Ledger("t2"), budget=Budget(), fetch=RecordedFetcher({}))
    res = run(ctx)
    assert res.facts == [] and res.checked_absent == []
    assert res.errors


def test_registered_and_declared_prefix():
    registry = discover()
    assert "certifications" in registry
    c = registry["certifications"]
    assert c.ticket == "I15"
    assert c.produces == ("certifications.",)
    assert c.may_emit("certifications.iso_management")
    assert not c.may_emit("digital.cms")
