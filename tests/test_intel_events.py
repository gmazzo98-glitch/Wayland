"""
Offline unit tests for corporate events timeline and collector.

Tests:
1. Event kind normalization to TRIGGER_KINDS.
2. Event deduplication and multi-source merging.
3. Conflicting date detection recorded into conflicts field.
4. Collector execution with recorded pages (zero network).
5. Confirmed absence when no events exist.
"""

from __future__ import annotations

import pytest

from diagnosis.contracts import TRIGGER_KINDS
from intel.collector import Budget, CollectorContext
from intel.collectors.events import run as run_events_collector
from intel.events import (
    CorporateEvent,
    merge_events,
    normalize_event_kind,
    parse_event_year,
)
from intel.fetch import Page
from intel.ledger import Ledger, quote_in_text


class RecordedFetcher:
    """Mock fetcher returning recorded pages without network access."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.fetched_urls = []

    def __call__(self, url: str) -> Page:
        self.fetched_urls.append(url)
        if url in self.pages:
            text = self.pages[url]
            links = [l.strip() for l in text.splitlines() if l.strip().startswith("http")]
            return Page(url=url, ok=True, status=200, text=text, links=links)
        return Page(url=url, ok=False, status=404, note="Not recorded")


def test_event_kind_normalization_matches_trigger_kinds():
    samples = [
        ("Acquisizione aziendale", "ownership_change"),
        ("M&A Buyout", "ownership_change"),
        ("Nuovo stabilimento a Binasco", "expansion"),
        ("Nuova filiale estera", "expansion"),
        ("Investimento di 5 milioni", "investment_announced"),
        ("Nomina nuovo direttore generale", "leadership_change"),
        ("Accordo di partnership commerciale", "partnership"),
        ("Certificazione ISO", "certification"),
        ("random string", "other"),
    ]
    for raw, expected in samples:
        norm = normalize_event_kind(raw)
        assert norm == expected
        assert norm in TRIGGER_KINDS


def test_parse_event_year():
    assert parse_event_year("Nel 2019 l'azienda ha registrato") == 2019
    assert parse_event_year("Fondazione 2002 a Milano") == 2002
    assert parse_event_year("Nessun anno qui") is None


def test_event_merging_and_conflict_detection():
    ev1 = CorporateEvent(
        kind="ownership_change",
        title="2022: Mott Corporation acquires ASCO Filtri",
        description="Mott Corporation acquires ASCO Filtri on 13 January 2022",
        year=2022,
        date="2022-01-13",
        parties=["Mott Corporation", "ASCO Filtri"],
        source_url="https://news.example.com/pr1",
        quote="Mott Corporation acquires ASCO Filtri on 13 January 2022",
    )
    ev2 = CorporateEvent(
        kind="ownership_change",
        title="Mott acquires Asco in 2021",
        description="Mott acquired ASCO Filtri in 2021 according to company website",
        year=2021,
        date="2021",
        parties=["Mott Corporation"],
        source_url="https://mott.example.com/history",
        quote="Mott acquired ASCO Filtri in 2021",
    )

    merged = merge_events([ev1, ev2])
    assert len(merged) == 1
    m = merged[0]
    assert len(m.sources) == 2
    assert "https://news.example.com/pr1" in m.sources
    assert "https://mott.example.com/history" in m.sources
    # Verify conflict detected
    assert len(m.conflicts) == 1
    assert m.conflicts[0]["field"] == "date/year"
    assert "2022 vs 2021" in m.conflicts[0]["note"]


def test_events_collector_with_recorded_pages():
    mock_company_page = """
    ASCO FILTRI S.P.A.
    Our History:
    2002 Founded in Milan by filtration experts.
    2013 New HQ in Binasco with expanded production area.
    2022 Mott deal and Asco Filtration Trading Ltd in Abu Dhabi opened.
    2023 ASCORP Energies India Pvt Ltd in Pune incorporated.
    In 2025 opened a fully automated filter-element production facility in Italy.
    """

    mock_deal_page = """
    Red-Fish Kapital si compra in club deal i trattamenti filtranti Asco Filtri.
    Red-Fish Kapital ha completato l'acquisizione del 100% di Asco Filtri SpA.
    I manager Ennio Michelini e Massimo Mascheroni hanno reinvestito.
    Fatturato FY2018 pari a 19,9 milioni di euro.
    """

    pages = {
        "https://www.ascofiltri.com": mock_company_page,
        "https://www.ascofiltri.com/Home/Company": mock_company_page,
        "https://bebeez.it/?s=Asco+Filtri": '<a href="https://bebeez.it/deal-asco">Deal</a>',
        "https://bebeez.it/deal-asco": mock_deal_page,
    }

    fetcher = RecordedFetcher(pages)
    company = {
        "legal_name": "ASCO FILTRI S.P.A.",
        "brand": "ASCO Filtri",
        "website_url": "https://www.ascofiltri.com",
    }
    ctx = CollectorContext(
        company=company,
        ledger=Ledger(),
        budget=Budget(),
        fetch=fetcher,
    )

    result = run_events_collector(ctx)
    assert len(result.facts) >= 3
    fact_keys = [f.key for f in result.facts]
    assert "events.timeline" in fact_keys
    assert "events.expansion" in fact_keys
    assert "events.ownership_change" in fact_keys

    # Verify every fact has verified quote
    for f in result.facts:
        assert f.verified is True
        for ev in f.evidence:
            if ev.source_type == "company_site":
                assert quote_in_text(ev.quote, mock_company_page)


def test_events_collector_records_checked_absent_when_empty():
    pages = {
        "https://www.emptyfirm.it": "Welcome to our website. We make mechanical parts.",
        "https://bebeez.it/?s=Empty+Firm": "No results found",
    }
    fetcher = RecordedFetcher(pages)
    company = {
        "legal_name": "Empty Firm S.r.l.",
        "brand": "Empty Firm",
        "website_url": "https://www.emptyfirm.it",
    }
    ctx = CollectorContext(
        company=company,
        ledger=Ledger(),
        budget=Budget(),
        fetch=fetcher,
    )

    result = run_events_collector(ctx)
    assert len(result.facts) == 0
    assert "events.timeline" in result.checked_absent


def test_reworked_collector_has_no_hardcoded_golden_urls_or_events():
    """I96: the source must not contain the golden company's source URLs, brand tokens of its
    owners, or pre-written event titles/parties/amounts (the review found all three)."""
    import inspect
    from intel.collectors import events as events_module
    src = inspect.getsource(events_module).lower()
    for token in ("ascofiltri", "asco filtri", "prnewswire", "bebeez.it/private-equity/red-fish",
                  "mottcorp", "marketscreener", "red-fish kapital acquires",
                  "idex corporation", "boris levin", "michelini", "ascorp", "binasco",
                  "eur 19.9m", "2022-01-13"):
        assert token not in src, f"events collector still hard-codes {token!r}"


def test_reworked_collector_builds_events_only_from_page_sentences():
    """A deal page is read generically: events come from sentences that name the company and
    quote-match the fetched text; a sidebar about ANOTHER company's deal is not an event."""
    deal_page = """
    Acme Pressure S.p.A. acquired by Delta Holdings, says the press release of today.
    Acme Pressure ha chiuso il 2024 con ricavi per 12,5 milioni di euro e un ebitda di 2,1 milioni.
    Altro deal: Zeta Capital acquisisce la majority di Unrelated Wheels Srl.
    """
    pages = {
        "https://www.acme-pressure.it": "Acme Pressure S.p.A. builds pressure valves since 1985.",
        "https://bebeez.it/?s=ACME+PRESSURE+.": "Deal\nhttps://bebeez.it/ma-e-corporate-finance/delta-holdings-acquires-acme-pressure",
        "https://bebeez.it/ma-e-corporate-finance/delta-holdings-acquires-acme-pressure": deal_page,
    }
    fetcher = RecordedFetcher(pages)
    company = {"legal_name": "ACME PRESSURE S.P.A.", "brand": "Acme Pressure",
               "website_url": "https://www.acme-pressure.it"}
    ctx = CollectorContext(company=company, ledger=Ledger(), budget=Budget(), fetch=fetcher)
    result = run_events_collector(ctx)
    assert result.facts, "the deal page should yield events"
    for fact in result.facts:
        for ev in fact.evidence:
            if ev.quote:
                assert quote_in_text(ev.quote, deal_page) or "acme-pressure" in (ev.url or ""), \
                    f"quote not grounded in the fetched page: {ev.quote[:60]}"
                assert "unrelated wheels" not in (ev.quote or "").lower(), \
                    "another company's deal was recorded as this company's event"
