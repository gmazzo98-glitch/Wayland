"""
Offline unit tests for the ownership classifier and collector (ticket I12). No network.

Tests:
1. Classifier on AIDA blobs: listed-group chain (Mott -> IDEX), family firm, family holding,
   private equity, no data. Golden-style fixtures modelled on the AIDA export shape.
2. Distribution report function reproduces the raw 533 / 139 counts logic (corporate >=50%, foreign >=50%).
3. Collector with recorded pages: classification fact (grade A) when the AIDA blob is present,
   checked_absent when not; EDGAR subsidiary listing and parent-people/previous-owner facts are
   quote-grounded against the recorded text.
"""

from __future__ import annotations

import pytest

from intel.collector import Budget, CollectorContext
from intel.collectors.ownership import run as run_ownership_collector
from intel.fetch import Page
from intel.ledger import Ledger, quote_in_text
from intel.ownership import classify_ownership, profile_to_facts


class RecordedFetcher:
    def __init__(self, pages: dict) -> None:
        self.pages = pages
        self.fetched = []

    def __call__(self, url: str) -> Page:
        self.fetched.append(url)
        if url in self.pages:
            return Page(url=url, ok=True, status=200, text=self.pages[url])
        return Page(url=url, ok=False, status=404, note="not recorded")


class RecordedSearch:
    def __init__(self, results: dict) -> None:
        self.results = results
        self.queries = []

    def search(self, query, kind="web", lang="it", n=10):
        from intel.search import SearchResult
        self.queries.append(query)
        return [SearchResult(**r) for r in self.results.get(query, [])]


IDEX_BLOB = {
    "shareholders": [
        {"name": "MOTT CORPORATION", "type": "Società non finanziaria", "country": "US", "direct": 100.0, "total": 100.0, "stake": 100.0},
    ],
    "controlling_shareholders": [
        {"name": "MOTT CORPORATION", "type": "Società non finanziaria", "level": 1},
        {"name": "IDEX CORP", "type": "Società quotata", "level": 2},
    ],
    "immediate_shareholders": ["MOTT CORPORATION"],
    "bvd_independence_class": "D",
    "group_size": 272,
}

FAMILY_BLOB = {
    "shareholders": [
        {"name": "BORTOLO LUIGI", "type": "Persone fisiche o famiglie", "country": "IT", "direct": 100.0, "total": 100.0, "stake": 100.0},
    ],
    "controlling_shareholders": [],
    "immediate_shareholders": [],
    "bvd_independence_class": "A",
    "group_size": None,
}

FAMILY_HOLDING_BLOB = {
    "shareholders": [
        {"name": "FINANZIARIA ROSSI SRL", "type": "Società non finanziaria (holding)", "country": "IT", "direct": 100.0, "total": 100.0, "stake": 100.0},
        {"name": "GIUSEPPE ROSSI", "type": "Persone fisiche o famiglie", "country": "IT", "direct": None, "total": 55.0, "stake": None},
    ],
    "controlling_shareholders": [{"name": "FINANZIARIA ROSSI SRL", "type": "Società non finanziaria (holding)", "level": 1}],
    "immediate_shareholders": ["FINANZIARIA ROSSI SRL"],
    "bvd_independence_class": "B",
    "group_size": 3,
}

PE_BLOB = {
    "shareholders": [
        {"name": "RED-FISH KAPITAL SPA", "type": "Società non finanziaria (holding di investimento)", "country": "IT", "direct": 100.0, "total": 100.0, "stake": 100.0},
    ],
    "controlling_shareholders": [{"name": "RED-FISH KAPITAL SPA", "type": "holding di investimento", "level": 1}],
    "immediate_shareholders": ["AFI SRL"],
    "bvd_independence_class": "C",
    "group_size": 2,
}


def test_classifies_listed_group_chain():
    p = classify_ownership({"legal_name": "ASCO FILTRI S.P.A."}, IDEX_BLOB)
    assert p.controlled_subsidiary is True
    assert p.owner_type == "listed_group"
    assert p.ultimate_owner == "IDEX CORP"
    assert p.foreign_controlled is True
    assert p.group_size == 272


def test_classifies_family_firm():
    p = classify_ownership({"legal_name": "FAMIGLIA & FIGLI S.R.L."}, FAMILY_BLOB)
    assert p.controlled_subsidiary is False
    assert p.owner_type == "family"
    assert p.family_share == 100.0


def test_classifies_family_holding_as_family_when_individuals_majority():
    p = classify_ownership({"legal_name": "OFFICINE ROSSI S.P.A."}, FAMILY_HOLDING_BLOB)
    assert p.controlled_subsidiary is True
    assert p.owner_type == "family"
    assert p.ultimate_owner == "FINANZIARIA ROSSI SRL"


def test_classifies_private_equity():
    p = classify_ownership({"legal_name": "TRATTAMENTI SRL"}, PE_BLOB)
    assert p.controlled_subsidiary is True
    assert p.owner_type == "private_equity"


def test_classifies_no_data():
    p = classify_ownership({"legal_name": "OPAQUE SRL"}, {})
    assert p.owner_type == "no_data"


def test_profile_to_facts_shape():
    p = classify_ownership({"legal_name": "ASCO FILTRI S.P.A."}, IDEX_BLOB)
    v = profile_to_facts(p, {"legal_name": "ASCO FILTRI S.P.A."})
    assert v["owner_type"] == "listed_group"
    assert v["controlled_subsidiary"] is True
    assert any(c["name"] == "IDEX CORP" for c in v["control_chain"])


def test_raw_counts_logic_matches_533_139_definition():
    """The roadmap's 533 = companies with a corporate holder of 50%+; 139 = 50%+ foreign-owned."""
    blobs = [IDEX_BLOB, FAMILY_BLOB, FAMILY_HOLDING_BLOB, PE_BLOB]
    corporate_50 = foreign_50 = 0
    for b in blobs:
        p = classify_ownership({"legal_name": "X"}, b)
        corp = any((l.get("stake") or 0) >= 50 and "persone fisiche" not in str(l.get("type", "")).lower()
                   for l in (b.get("shareholders") or []))
        forg = any((l.get("stake") or 0) >= 50 and str(l.get("country", "")).upper() not in ("", "-", "IT")
                   for l in (b.get("shareholders") or []))
        corporate_50 += corp
        foreign_50 += forg
        assert corp == p.controlled_subsidiary or p.owner_type == "family"
    assert corporate_50 == 3   # IDEX chain, family holding, PE; the pure family firm is not
    assert foreign_50 == 1     # only the IDEX chain


def test_collector_emits_classification_grade_a():
    ctx = CollectorContext(company={"legal_name": "ASCO FILTRI S.P.A.", "aida_blob": IDEX_BLOB},
                           ledger=Ledger("c1"))
    result = run_ownership_collector(ctx)
    keys = {f.key for f in result.facts}
    assert "ownership.classification" in keys
    assert "ownership.controlled_subsidiary" in keys
    cls = [f for f in result.facts if f.key == "ownership.classification"][0]
    assert cls.grade == "A"
    assert cls.value["owner_type"] == "listed_group"
    ledger = Ledger("c1")
    refused = ledger.add_many(result.facts)
    assert refused == []


def test_collector_absent_without_blob():
    ctx = CollectorContext(company={"legal_name": "ASCO FILTRI S.P.A."}, ledger=Ledger("c1"))
    result = run_ownership_collector(ctx)
    assert "ownership.classification" in result.checked_absent
    assert result.facts == []


EDGAR_TEXT = """IDEX CORPORATION
Subsidiaries of IDEX Corporation as of December 31, 2024:
Asco Filtri SpA, Italy
Mott Corporation, Delaware
Micropump Incorporated, Washington"""

PR_TEXT = ("FARMINGTON, Conn., Jan. 13, 2022 Mott acquires ASCO Filtri, its longtime partner in Europe. "
           "\u201cAll of which are quite complementary to Mott,\u201d said Boris Levin, CEO of Mott Corporation. "
           "ASCO Filtri will retain its team and locations. Ennio Michelini, Managing Director, and "
           "Massimo Mascheroni, General Manager, will join the Mott leadership team.")

BEBEEZ_TEXT = ("Red-Fish Kapital si compra in club deal i trattamenti filtranti Asco Filtri. "
               "l'azienda è stata rilevata tramite la newco AF srl, controllato dal club deal AFI srl "
               "di cui Red-Fish Kapital spa detiene la maggioranza, mentre due manager e soci di Asco Filtri spa, "
               "Ennio Michelini (amministratore delegato) e Massimo Mascheroni (direttore generale), hanno entrambi reinvestito.")


def test_collector_web_evidence_is_quote_grounded():
    search = RecordedSearch({
        '"MOTT CORPORATION" "ASCO FILTRI"': [
            {"url": "https://www.sec.gov/Archives/edgar/data/832101/000083210125000010/iex-202412x31xex21.htm",
             "title": "EX-21", "snippet": "", "provider": "sec"},
        ],
        '"ASCO FILTRI S.P.A." acquisition previous owner': [
            {"url": "https://www.prnewswire.com/news-releases/example.html", "title": "Mott acquires ASCO", "snippet": "", "provider": "duckduckgo"},
        ],
    })
    fetcher = RecordedFetcher({
        "https://www.sec.gov/Archives/edgar/data/832101/000083210125000010/iex-202412x31xex21.htm": EDGAR_TEXT,
        "https://www.prnewswire.com/news-releases/example.html": PR_TEXT,
    })
    ctx = CollectorContext(
        company={"legal_name": "ASCO FILTRI S.P.A.", "website_url": "https://www.ascofiltri.com",
                 "aida_blob": IDEX_BLOB},
        ledger=Ledger("c1"), fetch=fetcher, search=search)
    result = run_ownership_collector(ctx)
    by_key = {}
    for f in result.facts:
        by_key.setdefault(f.key, []).append(f)
    assert "ownership.subsidiary_listing" in by_key
    listing = by_key["ownership.subsidiary_listing"][0]
    assert quote_in_text(listing.evidence[0].quote, EDGAR_TEXT)
    assert "Asco Filtri SpA" in listing.evidence[0].quote
    people = {f.value["name"] for f in by_key.get("ownership.parent_people", [])}
    assert "Boris Levin" in people
    for f in result.facts:
        for ev in f.evidence:
            if ev.quote and ev.url:
                pages = fetcher.pages
                assert any(quote_in_text(ev.quote, pages[u]) for u in [ev.url] if u in pages)
    ledger = Ledger("c1")
    refused = ledger.add_many(result.facts)
    assert refused == []


def test_collector_previous_owners_from_deal_press():
    search = RecordedSearch({
        '"ASCO FILTRI S.P.A." acquisizione cessione capitale': [
            {"url": "https://bebeez.it/private-equity/example/", "title": "Red-Fish", "snippet": "", "provider": "duckduckgo"},
        ],
    })
    fetcher = RecordedFetcher({"https://bebeez.it/private-equity/example/": BEBEEZ_TEXT})
    ctx = CollectorContext(
        company={"legal_name": "ASCO FILTRI S.P.A.", "aida_blob": PE_BLOB},
        ledger=Ledger("c1"), fetch=fetcher, search=search)
    result = run_ownership_collector(ctx)
    prev = [f for f in result.facts if f.key == "ownership.previous_owners"]
    assert prev, "expected previous-owners fact from deal press"
    assert quote_in_text(prev[0].evidence[0].quote, BEBEEZ_TEXT)
    ledger = Ledger("c1")
    assert ledger.add_many(result.facts) == []


def test_collector_family_firm_needs_no_web_owner_evidence():
    ctx = CollectorContext(company={"legal_name": "FAMIGLIA & FIGLI S.R.L.", "aida_blob": FAMILY_BLOB},
                           ledger=Ledger("c1"))
    result = run_ownership_collector(ctx)
    assert result.pages_fetched == 0
    assert "ownership.controlled_subsidiary" in result.checked_absent


# ------------------------------------------------------------------ portfolio distribution (acceptance helpers)

def test_raw_counts_match_roadmap_definition():
    from intel.ownership import raw_counts
    assert raw_counts({}) == {"corporate_holder_ge_50": False, "foreign_holder_ge_50": False}
    assert raw_counts({"shareholders": [
        {"name": "FAMIGLIA", "type": "Persone fisiche o famiglie", "country": "IT", "stake": 100.0},
    ]}) == {"corporate_holder_ge_50": False, "foreign_holder_ge_50": False}
    assert raw_counts({"shareholders": [
        {"name": "MOTT CORPORATION", "type": "Società non finanziaria", "country": "US", "stake": 100.0},
    ]}) == {"corporate_holder_ge_50": True, "foreign_holder_ge_50": True}
    assert raw_counts({"shareholders": [
        {"name": "HOLDING ITALIANA", "type": "Società non finanziaria", "country": "IT", "stake": 60.0},
    ]}) == {"corporate_holder_ge_50": True, "foreign_holder_ge_50": False}


def test_distribution_report_aggregates_and_counts_mismatches():
    from intel.ownership import distribution_report
    rows = [
        ({"legal_name": "SUB ASSET"}, IDEX_BLOB),
        ({"legal_name": "FAM CO"}, FAMILY_BLOB),
        ({"legal_name": "HOLDING CO"}, FAMILY_HOLDING_BLOB),
        ({"legal_name": "PE CO"}, PE_BLOB),
    ]
    rep = distribution_report(rows)
    assert rep["companies"] == 4
    assert rep["raw_corporate_ge_50"] == 3
    assert rep["raw_foreign_ge_50"] == 1
    assert rep["owner_type_distribution"]["listed_group"] == 1
    assert rep["owner_type_distribution"]["family"] == 2
    assert rep["owner_type_distribution"]["private_equity"] == 1


def test_classifier_hand_labelled_golden_shapes():
    """The golden files hand-label the ownership of all 7 benchmark companies. These blobs model the
    AIDA shapes for four of the archetypes; the classifier must type them correctly."""
    # Bertazzon-style: pure individuals -> family, not a controlled subsidiary
    p = classify_ownership({"legal_name": "BERTAZZON 3B S.R.L."}, FAMILY_BLOB)
    assert (p.owner_type, p.controlled_subsidiary) == ("family", False)
    # Isolcell-style: family holding + individuals majority -> family (not private equity, not group)
    p = classify_ownership({"legal_name": "ISOLCELL S.P.A."}, FAMILY_HOLDING_BLOB)
    assert p.owner_type == "family"
    # Pomini-style: foreign industrial group 100% -> controlled, industrial/listed, foreign
    p = classify_ownership({"legal_name": "POMINI R&P S.R.L."}, {
        "shareholders": [
            {"name": "HARBURG-FREUDENBERGER MASCHINENBAU GMBH", "type": "Società non finanziaria",
             "country": "DE", "direct": 100.0, "total": 100.0, "stake": 100.0},
        ],
        "controlling_shareholders": [
            {"name": "HARBURG-FREUDENBERGER MASCHINENBAU GMBH", "type": "Società non finanziaria", "level": 1},
            {"name": "L. POSSEHL & CO. MBH", "type": "Società non finanziaria", "level": 2},
        ],
        "bvd_independence_class": "D", "group_size": 40,
    })
    assert p.controlled_subsidiary is True
    assert p.owner_type in ("industrial_group", "listed_group")
    assert p.foreign_controlled is True
    # IMA/Eurosicma-style: Italian listed group with a chain
    p = classify_ownership({"legal_name": "EUROSICMA S.P.A."}, {
        "shareholders": [
            {"name": "I.M.A. INDUSTRIA MACCHINE AUTOMATICHE S.P.A.", "type": "Società quotata",
             "country": "IT", "direct": 60.0, "total": 60.0, "stake": 60.0},
        ],
        "controlling_shareholders": [
            {"name": "I.M.A. INDUSTRIA MACCHINE AUTOMATICHE S.P.A.", "type": "Società quotata", "level": 1},
        ],
        "bvd_independence_class": "D", "group_size": 60,
    })
    assert p.controlled_subsidiary is True
    assert p.owner_type == "listed_group"
