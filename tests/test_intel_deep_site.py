"""
Unit tests for deep site reader collector (Ticket I10).

Offline tests using recorded HTML fixtures (zero network access).
Validates:
1. Document and link classification heuristics.
2. Safe quote extraction and guaranteed quote_in_text grounding.
3. Collector execution with RecordedFetcher across multi-page mock site.
4. Extraction of identity, contacts, locations, products, certifications, history, documents.
5. Confirmed absence (checked_absent) handling when content is missing.
"""

from __future__ import annotations

from urllib.parse import urljoin
import pytest

from intel.collector import Budget, CollectorContext
from intel.collectors.deep_site import (
    COLLECTOR,
    _classify_doc_type,
    _classify_url_category,
    _is_doc_url,
    _safe_quote,
    run as run_deep_site_collector,
)
from intel.fetch import Page, html_to_text, normalise_url
from intel.ledger import Ledger, quote_in_text


class RecordedFetcher:
    """Mock fetcher returning pre-recorded HTML pages without network access."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.fetched_urls: list[str] = []

    def __call__(self, url: str) -> Page:
        self.fetched_urls.append(url)
        clean = url.rstrip("/")
        for k, html in self.pages.items():
            if k.rstrip("/") == clean:
                title, text, hrefs = html_to_text(html)
                links = [normalise_url(urljoin(url, h)) for h in hrefs if h]
                return Page(
                    url=url,
                    ok=True,
                    status=200,
                    text=text,
                    links=links,
                    content_type="text/html",
                )
        return Page(url=url, ok=False, status=404, note="Not recorded in test mock")


# Sample HTML fixtures
MOCK_HOME_HTML = """
<!DOCTYPE html>
<html>
<head><title>Officine Meccaniche Rossi S.p.A. - Leader nella Meccanica</title></head>
<body>
  <nav>
    <a href="/chi-siamo">Chi Siamo</a>
    <a href="/prodotti">Prodotti & Soluzioni</a>
    <a href="/qualita-certificazioni">Qualità e Certificazioni</a>
    <a href="/contatti">Contatti</a>
    <a href="/download">Area Download</a>
  </nav>
  <main>
    <h1>Benvenuti in Officine Meccaniche Rossi S.p.A.</h1>
    <p>Officine Meccaniche Rossi S.p.A. progetta e realizza componenti meccanici di precisione e sistemi di filtrazione industriale.</p>
  </main>
  <footer>
    <p>Officine Meccaniche Rossi S.p.A. - P.IVA 01234567890 - C.F. 01234567890</p>
    <p>Capitale Sociale € 1.500.000,00 i.v. - REA MI-987654</p>
    <p>Sede Legale: Via Garibaldi 42, 20121 Milano (MI)</p>
    <a href="https://facebook.com/rossispa">Facebook</a>
  </footer>
</body>
</html>
"""

MOCK_ABOUT_HTML = """
<!DOCTYPE html>
<html>
<head><title>Chi Siamo - Storia e Valori</title></head>
<body>
  <h1>La nostra storia</h1>
  <p>2002
  FOUNDATION
  Officine Meccaniche Rossi S.p.A. was founded in Milan by filtration experts.</p>
  <p>Nel 2012 abbiamo aperto la filiale commerciale Rossi Deutschland GmbH a Monaco di Baviera.</p>
  <p>In 2025 opened a fully automated production facility in Italy to expand capacity.</p>
  <p>We operate an engineering division dedicated to process filtration solutions.</p>
</body>
</html>
"""

MOCK_PRODUCTS_HTML = """
<!DOCTYPE html>
<html>
<head><title>Prodotti - Riduttori e Filtrazione Industriale</title></head>
<body>
  <h1>Le Nostre Linee di Prodotto</h1>
  <div class="product-item">
    <h2>Filtri di Processo e Cartucce Filtranti</h2>
    <p>Process filtration systems tailored for petrochemical and power generation applications.</p>
  </div>
</body>
</html>
"""

MOCK_CERTS_HTML = """
<!DOCTYPE html>
<html>
<head><title>Certificazioni e Qualità</title></head>
<body>
  <h1>Qualità Certificata</h1>
  <p>Il nostro sistema di gestione è conforme agli standard internazionali:</p>
  <p>Certificazione ISO 9001 per la gestione della qualità rilasciata da DNV.</p>
  <p>Certificazione ambientale ISO 14001:2015.</p>
</body>
</html>
"""

MOCK_CONTACTS_HTML = """
<!DOCTYPE html>
<html>
<head><title>Contatti e Sedi</title></head>
<body>
  <h1>Mettiti in contatto con noi</h1>
  <p>Sede e Stabilimento: Via Roma 100, 20082 Binasco (MI)</p>
  <p>Telefono centralino: +39 02 897031</p>
  <p>Email informazioni: info@rossimeccanica.com</p>
  <p>Posta Elettronica Certificata (PEC): rossimeccanica@pec.it</p>
</body>
</html>
"""

MOCK_DOWNLOAD_HTML = """
<!DOCTYPE html>
<html>
<head><title>Documenti e Download</title></head>
<body>
  <h1>Download Area</h1>
  <p>Explore and download our certifications and manuals.</p>
  <ul>
    <li><a href="/docs/certificato-iso-9001.pdf">Certificato ISO 9001 DNV</a></li>
    <li><a href="/docs/manuale-uso-manutenzione.pdf">Manuale Uso e Manutenzione</a></li>
    <li><a href="/docs/catalogo-generale-2024.pdf">Catalogo Generale Prodotti 2024</a></li>
  </ul>
</body>
</html>
"""


def test_classify_doc_type():
    assert _classify_doc_type("https://example.com/iso9001_cert.pdf", "Certificato") == "certificate"
    assert _classify_doc_type("https://example.com/ped_directive.pdf", "") == "certificate"
    assert _classify_doc_type("https://example.com/ioms_manual.pdf", "Manuale istruzioni") == "manual"
    assert _classify_doc_type("https://example.com/product_catalogue.pdf", "Catalogo 2024") == "catalog"
    assert _classify_doc_type("https://example.com/esg_report.pdf", "Bilancio di sostenibilità") == "report"
    assert _classify_doc_type("https://example.com/terms.pdf", "Condizioni generali") == "other"


def test_classify_url_category():
    assert _classify_url_category("https://example.com/") == "home"
    assert _classify_url_category("https://example.com/contatti") == "contacts"
    assert _classify_url_category("https://example.com/chi-siamo") == "about"
    assert _classify_url_category("https://example.com/our-history") == "history"
    assert _classify_url_category("https://example.com/download") == "downloads"
    assert _classify_url_category("https://example.com/qualita-certificazioni") == "quality_certifications"
    assert _classify_url_category("https://example.com/prodotti") == "products"


def test_is_doc_url():
    assert _is_doc_url("https://example.com/file.pdf") is True
    assert _is_doc_url("https://example.com/file.DOCX") is True
    assert _is_doc_url("https://example.com/file.xlsx") is True
    assert _is_doc_url("https://example.com/page.html") is False
    assert _is_doc_url("https://example.com/about") is False


def test_safe_quote_guarantees_exact_substring():
    text = "Line 1\nOfficine Meccaniche Rossi S.p.A. con sede a Milano\nLine 3"
    start = text.index("Officine")
    end = start + len("Officine Meccaniche Rossi S.p.A.")
    quote = _safe_quote(text, start, end)
    assert quote_in_text(quote, text) is True
    assert "Officine Meccaniche Rossi S.p.A." in quote


def test_deep_site_collector_full_run():
    pages = {
        "https://www.rossimeccanica.com": MOCK_HOME_HTML,
        "https://www.rossimeccanica.com/chi-siamo": MOCK_ABOUT_HTML,
        "https://www.rossimeccanica.com/prodotti": MOCK_PRODUCTS_HTML,
        "https://www.rossimeccanica.com/qualita-certificazioni": MOCK_CERTS_HTML,
        "https://www.rossimeccanica.com/contatti": MOCK_CONTACTS_HTML,
        "https://www.rossimeccanica.com/download": MOCK_DOWNLOAD_HTML,
    }

    fetcher = RecordedFetcher(pages)
    company = {
        "legal_name": "Officine Meccaniche Rossi S.p.A.",
        "brand": "Rossi Meccanica",
        "website_url": "https://www.rossimeccanica.com",
    }
    ctx = CollectorContext(
        company=company,
        ledger=Ledger(),
        budget=Budget(max_pages=20),
        fetch=fetcher,
    )

    result = run_deep_site_collector(ctx)
    assert COLLECTOR.name == "deep-site"
    assert len(result.facts) >= 8

    # Verify fact keys coverage
    keys = {f.key for f in result.facts}
    assert "identity.registered_office" in keys
    assert "identity.share_capital" in keys
    assert "identity.founded_year" in keys
    assert "contact.switchboard" in keys
    assert "contact.generic_email" in keys
    assert "contact.pec" in keys
    assert "events.facility_opening" in keys
    assert "documents.inventory" in keys

    # Verify every emitted fact passes quote_in_text on its source URL page
    for fact in result.facts:
        assert len(fact.evidence) > 0
        for ev in fact.evidence:
            url = ev.url
            clean = url.rstrip("/")
            source_html = None
            for p_url, p_html in pages.items():
                if p_url.rstrip("/") == clean:
                    source_html = p_html
                    break
            assert source_html is not None, f"Page not found in mock for {url}"
            _, source_text, _ = html_to_text(source_html)
            assert quote_in_text(ev.quote, source_text), (
                f"Quote '{ev.quote}' not grounded in {url}"
            )


def test_deep_site_collector_records_checked_absent_on_empty_site():
    pages = {
        "https://www.emptysite.com": "<html><body><p>Under construction.</p></body></html>",
    }
    fetcher = RecordedFetcher(pages)
    company = {
        "legal_name": "Vuoto S.r.l.",
        "brand": "Vuoto",
        "website_url": "https://www.emptysite.com",
    }
    ctx = CollectorContext(
        company=company,
        ledger=Ledger(),
        budget=Budget(max_pages=5),
        fetch=fetcher,
    )

    result = run_deep_site_collector(ctx)
    assert len(result.facts) == 0
    assert len(result.checked_absent) > 0
    assert "contact.switchboard" in result.checked_absent
    assert "contact.pec" in result.checked_absent
    assert "identity.founded_year" in result.checked_absent
