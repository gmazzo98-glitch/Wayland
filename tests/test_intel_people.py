"""
Unit tests for People and Contact Intelligence (Ticket I14).

Tests:
  - Role classification and pilot sponsor ranking (MD/GM > Tech/Ops > Export > Purchasing > Parent)
  - Business contact extraction (switchboard, generic email, PEC, contact page, languages)
  - Policy adherence: suppression list, no guessed personal emails (G3), no authenticated LinkedIn (G2)
  - Contacts and People collectors with recorded pages (offline, no network)
  - Verbatim quote grounding and ledger acceptance
"""

from __future__ import annotations

import pytest

from intel.collector import Budget, CollectorContext, check_result
from intel.collectors import contacts, people
from intel.fetch import Page
from intel.ledger import Evidence, Fact, Ledger, quote_in_text
from intel.people import (
    classify_role,
    clean_name,
    detect_languages,
    extract_emails,
    extract_pec,
    extract_phones,
    is_generic_business_email,
    is_suppressed,
    rank_pilot_sponsors,
    PersonCandidate,
    PILOT_SPONSOR_RANKS,
)


# ---------------------------------------------------------------------------
# Role Classification & Pilot Sponsor Ranking Tests
# ---------------------------------------------------------------------------

def test_classify_role_and_pilot_sponsors():
    # Tier 1: General Management / C-Level
    grp, norm, is_op, is_parent, rank, tier = classify_role("Direttore Generale")
    assert grp == "general_management"
    assert is_op is True
    assert is_parent is False
    assert rank == 1
    assert "Tier 1" in tier

    grp, norm, is_op, is_parent, rank, tier = classify_role("Amministratore Delegato")
    assert grp == "general_management"
    assert rank == 1

    grp, norm, is_op, is_parent, rank, tier = classify_role("Managing Director")
    assert grp == "general_management"
    assert rank == 1

    grp, norm, is_op, is_parent, rank, tier = classify_role("General Manager")
    assert grp == "general_management"
    assert rank == 1

    # Tier 2: Operations & Technology
    grp, norm, is_op, is_parent, rank, tier = classify_role("Chief Technology Officer")
    assert grp == "operations_technology"
    assert is_op is True
    assert rank == 2
    assert "Tier 2" in tier

    grp, norm, is_op, is_parent, rank, tier = classify_role("Direttore Tecnico")
    assert grp == "operations_technology"
    assert rank == 2

    grp, norm, is_op, is_parent, rank, tier = classify_role("Plant Manager")
    assert grp == "operations_technology"
    assert rank == 2

    # Tier 3: Commercial & Export
    grp, norm, is_op, is_parent, rank, tier = classify_role("Manager-Export & Distribution Network")
    assert grp == "commercial_export"
    assert is_op is True
    assert rank == 3
    assert "Tier 3" in tier

    grp, norm, is_op, is_parent, rank, tier = classify_role("Direttore Commerciale")
    assert grp == "commercial_export"
    assert rank == 3

    # Tier 4: Purchasing & Procurement
    grp, norm, is_op, is_parent, rank, tier = classify_role("Responsabile Acquisti")
    assert grp == "purchasing_procurement"
    assert is_op is True
    assert rank == 4
    assert "Tier 4" in tier

    # Tier 5: Foreign Group / Parent Directors
    grp, norm, is_op, is_parent, rank, tier = classify_role(
        "Consigliere; Presidente Consiglio Amministrazione", nationality="United States"
    )
    assert grp == "board_parent"
    assert is_op is False
    assert is_parent is True
    assert rank == 5
    assert "Tier 5" in tier

    # Signatory (Procuratore)
    grp, norm, is_op, is_parent, rank, tier = classify_role("Procuratore Speciale", nationality="Italy")
    assert grp == "signatory"
    assert is_op is False
    assert rank == 5


def test_pilot_sponsor_ranking_order():
    p1 = PersonCandidate("Mario Rossi", "Amministratore Delegato", "general_management", "MD", True, False, 1)
    p2 = PersonCandidate("Luigi Verdi", "Direttore Tecnico", "operations_technology", "CTO", True, False, 2)
    p3 = PersonCandidate("Marco Rosmi", "Export Manager", "commercial_export", "Export", True, False, 3)
    p4 = PersonCandidate("Anna Neri", "Responsabile Acquisti", "purchasing_procurement", "Purchasing", True, False, 4)
    p5 = PersonCandidate("Steven Samp", "Presidente", "board_parent", "Chairman", False, True, 5)

    ranked = rank_pilot_sponsors([p4, p5, p2, p1, p3])
    names = [p.full_name for p in ranked]
    assert names == ["Mario Rossi", "Luigi Verdi", "Marco Rosmi", "Anna Neri", "Steven Samp"]


# ---------------------------------------------------------------------------
# Contact Extraction & Policy Tests (G2, G3)
# ---------------------------------------------------------------------------

def test_extract_phones_and_switchboard():
    sample_text = """
    Contatti della sede:
    Sede operativa: Viale delle Scienze 8, 20082 Binasco (MI)
    CALL US: +39 02 897031
    Fax: +39 02 89703410
    """
    phones = extract_phones(sample_text)
    assert len(phones) >= 1
    found_nums = [p[0] for p in phones]
    assert any("+39 02 897031" in n or "02 897031" in n for n in found_nums)
    # Check that quote is in sample_text
    for norm, quote in phones:
        assert quote in sample_text


def test_extract_emails_and_pec_with_suppression():
    sample_text = """
    Per informazioni commerciali: ascoinfo@mottcorp.com
    Posta certificata: ascofiltri@pec.it
    Supporto tecnico: info@ascofiltri.com
    Privacy: privacy@ascofiltri.com
    DPO: dpo@ascofiltri.com
    Abuse contact: abuse@ascofiltri.com
    """
    emails = extract_emails(sample_text)
    email_addresses = [e[0] for e in emails]

    # Published business emails included
    assert "ascoinfo@mottcorp.com" in email_addresses
    assert "info@ascofiltri.com" in email_addresses
    assert "ascofiltri@pec.it" in email_addresses

    # Suppressed emails excluded (G3 compliance)
    assert "privacy@ascofiltri.com" not in email_addresses
    assert "dpo@ascofiltri.com" not in email_addresses
    assert "abuse@ascofiltri.com" not in email_addresses

    # PEC extraction
    pec_tuple = extract_pec(sample_text)
    assert pec_tuple is not None
    assert pec_tuple[0] == "ascofiltri@pec.it"
    assert "ascofiltri@pec.it" in pec_tuple[1]


def test_generic_email_distinction():
    assert is_generic_business_email("info@azienda.it") is True
    assert is_generic_business_email("sales@company.com") is True
    assert is_generic_business_email("contatti@impresa.it") is True
    assert is_generic_business_email("ascoinfo@mottcorp.com") is True
    assert is_generic_business_email("mario.rossi@azienda.it") is False
    assert is_generic_business_email("g.verdi@company.com") is False


def test_language_detection():
    html_sample = """
    <html lang="it">
    <head><title>Azienda Meccanica</title></head>
    <body>
      <a href="/en/about">English</a>
      <a href="/it/chi-siamo">Italiano</a>
      <h1>Benvenuti nel nostro sito</h1>
      <p>Contattaci per informazioni sui nostri prodotti.</p>
    </body>
    </html>
    """
    langs = detect_languages(html_sample, ["https://example.com/en/about", "https://example.com/it/chi-siamo"])
    assert "it" in langs
    assert "en" in langs


# ---------------------------------------------------------------------------
# Offline Mock Collector Tests
# ---------------------------------------------------------------------------

def test_contacts_collector_offline():
    company = {
        "id": "test-company-1",
        "legal_name": "ACME MECCANICA S.P.A.",
        "website_url": "https://www.acme-meccanica.it",
    }
    home_html = """
    <html>
      <head><title>ACME Meccanica Home</title></head>
      <body>
        <a href="/contatti">Contatti</a>
        <p>Benvenuti in ACME Meccanica, macchinari industriali.</p>
      </body>
    </html>
    """
    contatti_text = """
    Contatti - ACME Meccanica
    Sede e stabilimento: Via dell'Industria 12, Milano
    Telefono: +39 02 1234567
    Email: info@acme-meccanica.it
    PEC: acmemeccanica@pec.it
    """

    def mock_fetch(url):
        if "contatti" in url:
            return Page(url=url, ok=True, status=200, text=contatti_text,
                        links=[], headers={"server": "nginx"}, content_hash="hash_contatti")
        return Page(url=url, ok=True, status=200, text="Benvenuti in ACME Meccanica",
                    links=["https://www.acme-meccanica.it/contatti"],
                    headers={"server": "nginx"}, content_hash="hash_home")

    ctx = CollectorContext(company=company, ledger=Ledger(company["id"]), budget=Budget(), fetch=mock_fetch)
    res = contacts.run(ctx)

    # Validate output
    refused = check_result(contacts.COLLECTOR, res)
    assert refused == [], f"Unexpected refusals: {refused}"

    keys = {f.key: f.value for f in res.facts}
    assert "contact.switchboard" in keys
    assert "+39 02 1234567" in keys["contact.switchboard"] or "02 1234567" in keys["contact.switchboard"]
    assert "contact.generic_email" in keys
    assert keys["contact.generic_email"] == "info@acme-meccanica.it"
    assert "contact.pec" in keys
    assert keys["contact.pec"] == "acmemeccanica@pec.it"
    assert "contact.contact_page" in keys
    assert keys["contact.contact_page"] == "https://www.acme-meccanica.it/contatti"

    # Verify quotes
    for f in res.facts:
        for ev in f.evidence:
            if ev.quote:
                assert len(ev.quote) >= 12
                # Must be verified against page text
                if "contatti" in ev.url:
                    assert quote_in_text(ev.quote, contatti_text)


def test_people_collector_offline():
    company = {
        "id": "test-company-2",
        "legal_name": "ASCO FILTRI S.P.A.",
        "brand": "Asco Filtri",
        "website_url": "https://www.ascofiltri.com",
        "people": [
            {"full_name": "Sig. Steven Martin Samp", "role": "CONSIGLIERE;PRESIDENTE CONSIGLIO AMMINISTRAZIONE", "nationality": "United States"},
            {"full_name": "Sig. Renato Erta", "role": "CONSIGLIERE", "nationality": "Italy"},
            {"full_name": "Sig. Stefano Mura", "role": "PROCURATORE", "nationality": "Italy"},
        ]
    }

    press_text = """
    Seeking broader international footprint and portfolio of products, Mott acquires ASCO Filtri.
    said Boris Levin, CEO of Mott Corporation.
    Ennio Michelini, Managing Director, and Massimo Mascheroni, General Manager, will join the Mott leadership team and will continue to manage ASCO Filtri business.
    """

    bebeez_text = """
    Red-Fish Kapital si compra in club deal i trattamenti filtranti Asco Filtri.
    Ennio Michelini (amministratore delegato, che manterrà la carica) e Massimo Mascheroni (direttore generale), hanno entrambi reinvestito nella società.
    Maurizio Arcari (già manager del gruppo internazionale Emerson Automation), azionista e consigliere con delega allo sviluppo internazionale.
    """

    zonebourse_text = """
    Marco Rosmi currently works as a Manager-Export & Distribution Network at ASCO Filtri SpA.
    """

    def mock_fetch(url):
        u = url.lower()
        if "prnewswire" in u or "mott" in u:
            return Page(url=url, ok=True, status=200, text=press_text, content_hash="h1")
        if "bebeez" in u:
            return Page(url=url, ok=True, status=200, text=bebeez_text, content_hash="h2")
        if "zonebourse" in u:
            return Page(url=url, ok=True, status=200, text=zonebourse_text, content_hash="h3")
        return Page(url=url, ok=True, status=200, text="Asco Filtri homepage", content_hash="h0")

    ctx = CollectorContext(company=company, ledger=Ledger(company["id"]), budget=Budget(), fetch=mock_fetch)
    res = people.run(ctx)

    refused = check_result(people.COLLECTOR, res)
    assert refused == [], f"Unexpected refusals: {refused}"

    names = {f.value.get("name") if isinstance(f.value, dict) else "" for f in res.facts}
    assert "Massimo Mascheroni" in names
    assert "Ennio Michelini" in names
    assert "Marco Rosmi" in names
    assert "Maurizio Arcari" in names
    assert "Boris Levin" in names
    assert "Steven Martin Samp" in names
    assert "Renato Erta" in names
    assert "Stefano Mura" in names

    # Check classifications
    for f in res.facts:
        if isinstance(f.value, dict):
            val = f.value
            if val["name"] == "Massimo Mascheroni":
                assert val["is_operating"] is True
                assert val["pilot_sponsor_rank"] == 1
                assert f.key == "people.general_manager"
            elif val["name"] == "Ennio Michelini":
                assert val["is_operating"] is True
                assert val["pilot_sponsor_rank"] == 1
                assert f.key == "people.managing_director"
            elif val["name"] == "Marco Rosmi":
                assert val["is_operating"] is True
                assert val["pilot_sponsor_rank"] == 3
                assert f.key == "people.export_manager"
            elif val["name"] == "Steven Martin Samp":
                assert val["is_parent_director"] is True
                assert val["is_operating"] is False
                assert val["pilot_sponsor_rank"] == 5
