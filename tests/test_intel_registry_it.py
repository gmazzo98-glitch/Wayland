"""
Offline unit tests for the Italian registry collector (ticket I20), recorded pages only, no network.

Covers:
1. Imprint extraction: VAT, REA, share capital, registered office, PEC — quote-grounded.
2. checked_absent when the site has no imprint block.
3. No website -> honest error, no absence.
4. Revenue history from a recorded public-directory page (search client mocked): year->amount
   series, quote-grounded verbatim; only when >= 3 years.
5. Graceful degradation without a search client (no financials fact, no crash).
6. Every emitted fact validates against the ledger rules; collector prefix discipline.
"""

from __future__ import annotations

from intel.collector import Budget, CollectorContext, check_result, discover
from intel.collectors.registry_it import COLLECTOR, run
from intel.fetch import Page
from intel.ledger import Ledger, quote_in_text

HOME_TEXT = "Example industries S.p.A. Home Products Contact us today for offers."
HOME_LINKS = ["https://www.example.com/contatti", "https://www.example.com/products"]
CONTACT_TEXT = (
    "Contatti\n"
    "Example Industries SPA - C.F./P.IVA 01234567890 REA e C.C.I.A.A.: MI-1234567 Capital: € 1.000.000,00 i.v.\n"
    "Via Roma 10, 20121 Milano (MI)\n"
    "PEC: example@pec.it\n"
    "Phone +39 02 1234567"
)
REVENUE_DIR_TEXT = (
    "Example Industries S.p.A. Milano: Fatturato, Bilancio e Dati Aziendali\n"
    "Anno Fatturato\n"
    "2019 17.140.885\n"
    "2020 18.044.327\n"
    "2021 22.550.000\n"
    "2022 28.220.000\n"
)


class RecordedFetcher:
    def __init__(self, pages: dict) -> None:
        self.pages = pages
        self.fetched = []

    def __call__(self, url: str) -> Page:
        self.fetched.append(url)
        if url in self.pages:
            text, links = self.pages[url]
            return Page(url=url, ok=True, status=200, content_type="text/html", text=text, links=links)
        return Page(url=url, ok=False, status=404, note="not recorded")


class MockSearch:
    def __init__(self, results) -> None:
        self.results = results
        self.queries = []

    def search(self, query, kind="web", lang="it", n=10):
        self.queries.append(query)
        return self.results


class MockResult:
    def __init__(self, url) -> None:
        self.url = url
        self.title = ""
        self.snippet = ""
        self.date = ""
        self.provider = "mock"
        self.query = ""


def _ctx(pages: dict, search=None, website="https://www.example.com") -> CollectorContext:
    return CollectorContext(
        company={"id": "t1", "legal_name": "EXAMPLE INDUSTRIES S.P.A.", "vat": "01234567890",
                 "website_url": website},
        ledger=Ledger("t1"), budget=Budget(), fetch=RecordedFetcher(pages), search=search,
    )


def test_imprint_extraction_all_fields():
    pages = {
        "https://www.example.com": (HOME_TEXT, HOME_LINKS),
        "https://www.example.com/contatti": (CONTACT_TEXT, []),
    }
    res = run(_ctx(pages))
    values = {f.key: f.value for f in res.facts}
    assert values["identity.vat"] == "01234567890"
    assert values["identity.rea"] == "MI-1234567"
    assert "1.000.000" in values["identity.share_capital"] and "i.v." in values["identity.share_capital"]
    assert "Milano" in values["identity.registered_office"]
    assert values["contact.pec"] == "example@pec.it"
    # the directory-search fallback runs (topaziende template is fetched, not recorded -> not ok),
    # so the financials key is honestly recorded as checked-and-absent
    assert "financials.revenue_history" in res.checked_absent
    assert "identity.vat" not in res.checked_absent


def test_imprint_quote_grounded_and_valid():
    pages = {
        "https://www.example.com": (HOME_TEXT, HOME_LINKS),
        "https://www.example.com/contatti": (CONTACT_TEXT, []),
    }
    ctx = _ctx(pages)
    res = run(ctx)
    assert not check_result(COLLECTOR, res)
    texts = {u: p[0] for u, p in pages.items()}
    for fact in res.facts:
        fact.validate()
        ev = fact.evidence[0]
        assert ev.url in texts
        assert quote_in_text(ev.quote, texts[ev.url]), f"quote not grounded for {fact.key}"


def test_checked_absent_when_no_imprint():
    pages = {"https://www.example.com": ("We make fine products. Contact us.", [])}
    res = run(_ctx(pages))
    assert res.facts == []
    assert set(res.checked_absent) >= {"identity.vat", "identity.rea", "contact.pec"}


def test_no_website_is_error_not_absence():
    ctx = CollectorContext(company={"id": "t2", "legal_name": "X", "website_url": ""},
                           ledger=Ledger("t2"), budget=Budget(), fetch=RecordedFetcher({}))
    res = run(ctx)
    assert res.facts == [] and res.checked_absent == [] and res.errors


def test_revenue_history_from_directory_page():
    dir_url = "https://dir.example.com/example-industries"
    pages = {
        "https://www.example.com": (HOME_TEXT, HOME_LINKS),
        "https://www.example.com/contatti": (CONTACT_TEXT, []),
        dir_url: (REVENUE_DIR_TEXT, []),
    }
    search = MockSearch([MockResult(dir_url)])
    res = run(_ctx(pages, search=search))
    fin = [f for f in res.facts if f.key == "financials.revenue_history"]
    assert len(fin) == 1
    value = fin[0].value
    assert value["2019"] == "17.140.885" and len(value) >= 3
    assert fin[0].grade == "C"
    assert quote_in_text(fin[0].evidence[0].quote, REVENUE_DIR_TEXT)
    assert "financials.revenue_history" not in res.checked_absent


def test_revenue_history_needs_three_years():
    thin = ("Example industries: Fatturato storico\n2019 17.140.885\n2020 18.044.327\n")
    dir_url = "https://dir.example.com/thin"
    pages = {
        "https://www.example.com": (HOME_TEXT, HOME_LINKS),
        "https://www.example.com/contatti": (CONTACT_TEXT, []),
        dir_url: (thin, []),
    }
    res = run(_ctx(pages, search=MockSearch([MockResult(dir_url)])))
    assert not [f for f in res.facts if f.key == "financials.revenue_history"]
    assert "financials.revenue_history" in res.checked_absent


def test_no_search_client_still_tries_directory_fallback():
    pages = {
        "https://www.example.com": (HOME_TEXT, HOME_LINKS),
        "https://www.example.com/contatti": (CONTACT_TEXT, []),
    }
    res = run(_ctx(pages, search=None))
    assert not [f for f in res.facts if f.key.startswith("financials.")]
    # without a reachable directory page, the financials key is honestly absent (not silently missing)
    assert "financials.revenue_history" in res.checked_absent


def test_registered_and_declared_prefixes():
    registry = discover()
    assert "registry-it" in registry
    c = registry["registry-it"]
    assert c.ticket == "I20"
    for prefix in ("identity.", "contact.pec", "financials."):
        assert prefix in c.produces
    assert not c.may_emit("certifications.iso_management")


def test_revenue_page_about_a_different_company_is_rejected():
    """A directory page whose table is plausible but which names a DIFFERENT company must not
    become the target's financials (the live Isolcell run caught exactly this: a search for
    Isolcell returned a large utility's page)."""
    other_company_dir = (
        "Hera SpA Bologna: Fatturato, Bilancio e Dati Aziendali\n"
        "Anno Fatturato\n"
        "2019 1.203.381.000\n"
        "2020 7.053.798.818\n"
        "2021 10.377.096.151\n"
    )
    dir_url = "https://dir.example.com/fatturato-hera-spa"
    pages = {
        "https://www.example.com": (HOME_TEXT, HOME_LINKS),
        "https://www.example.com/contatti": (CONTACT_TEXT, []),
        dir_url: (other_company_dir, []),
    }
    res = run(_ctx(pages, search=MockSearch([MockResult(dir_url)])))
    assert not [f for f in res.facts if f.key == "financials.revenue_history"]
    assert "financials.revenue_history" in res.checked_absent
