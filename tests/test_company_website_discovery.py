"""
Website discovery (T24): no live network or crawler is involved here - run_ts_crawler is
monkeypatched, same convention as tests/test_fda_recalls_crawler.py and friends. See
docs/CRAWLER_ROADMAP.md T24 for the AMPLIA ENGINEERING & EQUIPMENT S.R.L. case this is built
from: website_url was None despite the company having a real, content-rich homepage
(amplia-eng.it) that ranked below a business-registry aggregator and the parent group's own
domain in search results.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Company, RawImportRecord
from scrapers import company_website_discovery as wd


def _company(**overrides):
    defaults = dict(
        id="c1", legal_name="AMPLIA ENGINEERING & EQUIPMENT S.R.L.",
        registration_number="IT31461403000", external_ref_id="IT03146140300",
        province="Udine", region="Friuli-Venezia Giulia", country="Italy", website_url=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_strip_legal_form_handles_the_common_italian_and_german_suffixes():
    assert wd._strip_legal_form("AMPLIA ENGINEERING & EQUIPMENT S.R.L.") == "AMPLIA ENGINEERING & EQUIPMENT"
    assert wd._strip_legal_form("Muster GmbH & Co. KG") == "Muster"
    assert wd._strip_legal_form("Acme S.p.A.") == "Acme"
    assert wd._strip_legal_form("No Suffix Here") == "No Suffix Here"


def test_significant_tokens_drops_short_words_and_stopwords():
    assert wd._significant_tokens("AMPLIA ENGINEERING & EQUIPMENT") == ["amplia", "engineering", "equipment"]
    assert wd._significant_tokens("Di E Del") == []


def test_vat_digit_sets_checks_both_fields_and_tolerates_the_known_leading_zero_bug():
    c = _company()
    # registration_number is the corrupted T24 value; external_ref_id is the real VAT -
    # both the real VAT and (defensively) its leading-zero-dropped variant must be checked.
    sets = wd._vat_digit_sets(c)
    assert "31461403000" in sets  # registration_number as-is
    assert "03146140300" in sets  # external_ref_id as-is
    assert "3146140300" in sets  # external_ref_id with its own leading zero tolerated too


def test_search_queries_quotes_the_legal_name_and_adds_a_stripped_variant():
    queries = wd._search_queries(_company())
    assert queries[0] == '"AMPLIA ENGINEERING & EQUIPMENT S.R.L." Udine'
    assert queries[1] == '"AMPLIA ENGINEERING & EQUIPMENT" Udine'


def test_verify_accepts_a_vat_match_even_with_punctuation_in_the_page_text():
    c = _company()
    tokens = wd._significant_tokens(wd._strip_legal_form(c.legal_name))
    vat_sets = wd._vat_digit_sets(c)
    page = "Contact us. P.IVA: 03.146.140.300 - Via Buttrio 36, Pozzuolo del Friuli"
    assert wd._verify(page, "amplia-eng.it", c, tokens, vat_sets) == "vat"


def test_verify_accepts_name_and_location_without_a_vat_on_the_page():
    c = _company()
    tokens = wd._significant_tokens(wd._strip_legal_form(c.legal_name))
    page = "Amplia Engineering & Equipment - based in Udine, part of Amplia Infrastructures."
    assert wd._verify(page, "amplia-eng.it", c, tokens, set()) == "name_and_location"


def test_verify_rejects_an_unrelated_page():
    c = _company()
    tokens = wd._significant_tokens(wd._strip_legal_form(c.legal_name))
    page = "Welcome to a totally unrelated construction company in Milan."
    assert wd._verify(page, "amplia-eng.it", c, tokens, wd._vat_digit_sets(c)) is None


def test_verify_rejects_a_vat_match_on_a_domain_with_no_relation_to_the_company_name():
    """Live bug found via an 8-company spot check (T24 follow-up): FRIDEA S.R.L.'s VAT-shaped
    digit run coincidentally appeared on mflgroup.com, a totally unrelated wire/cable
    machinery company's page. A VAT match alone was not enough evidence."""
    c = _company()
    tokens = wd._significant_tokens(wd._strip_legal_form(c.legal_name))
    vat_sets = wd._vat_digit_sets(c)
    page = "Contact us. P.IVA: 03146140300 - some coincidental page"
    assert wd._verify(page, "totally-unrelated-company.com", c, tokens, vat_sets) is None


def test_verify_rejects_a_business_directory_that_prints_the_real_vat():
    """Live bug: impresaitalia.info (a business directory) correctly published CIDEN S.R.L.'s
    real looked-up VAT - that alone must not be read as 'this is the company's own site'."""
    c = _company()
    tokens = wd._significant_tokens(wd._strip_legal_form(c.legal_name))
    vat_sets = wd._vat_digit_sets(c)
    page = "AMPLIA ENGINEERING EQUIPMENT SRL UDINE - Impresa Italia. P.IVA: 03146140300"
    assert wd._verify(page, "impresaitalia.info", c, tokens, vat_sets) is None


def test_verify_rejects_a_logistics_tracker_that_prints_the_exact_name_and_city():
    """Live bug: myadj.it (a shipping/logistics tracker) legitimately prints a company's
    exact name and city as a shipping destination - that is not the company's own site."""
    c = _company()
    tokens = wd._significant_tokens(wd._strip_legal_form(c.legal_name))
    page = "Amplia Engineering Equipment - Servizio di spedizione a Udine"
    assert wd._verify(page, "myadj.it", c, tokens, set()) is None


def test_domain_matches_company_accepts_a_short_distinctive_token():
    assert wd._domain_matches_company("oxisrl.it", ["oxi"]) is True
    assert wd._domain_matches_company("amplia-eng.it", ["amplia", "engineering", "equipment"]) is True
    assert wd._domain_matches_company("mflgroup.com", ["fridea"]) is False
    assert wd._domain_matches_company("", ["amplia"]) is False
    assert wd._domain_matches_company("amplia-eng.it", []) is False


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'wd.db').as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_discover_website_skips_blocked_domains_and_picks_the_vat_verified_candidate(monkeypatch, db_session):
    c = _company()
    rows = [
        {"company_id": "c1", "url": "https://www.linkedin.com/company/amplia", "domain": "linkedin.com",
         "title": "Amplia on LinkedIn", "page_text": "Amplia Engineering & Equipment company page", "error": None},
        {"company_id": "c1", "url": "https://www.ampliaspa.it/amplia-engineeringequipment/", "domain": "ampliaspa.it",
         "title": "Amplia SpA", "page_text": "Unrelated group holding page, no VAT mentioned.", "error": None},
        {"company_id": "c1", "url": "https://amplia-eng.it/en/", "domain": "amplia-eng.it",
         "title": "Amplia Engineering & Equipment", "page_text": "P.IVA 03146140300 - Via Buttrio 36", "error": None},
    ]
    monkeypatch.setattr(wd, "run_ts_crawler", lambda name, input_rows, **kw: rows)
    monkeypatch.setattr(wd, "rows_for_company", lambda rows, company_id: rows)

    result = wd.discover_website(c, db_session)

    assert result["website_url"] == "https://amplia-eng.it/"
    assert result["confidence"] == "vat"
    rejected = {cand["domain"]: cand.get("rejected") for cand in result["candidates"] if "rejected" in cand}
    assert rejected["linkedin.com"] == "known directory/social domain"
    assert rejected["ampliaspa.it"] == "no verified match (name/VAT/location)"


def test_discover_website_accepts_nothing_when_no_candidate_is_verified(monkeypatch, db_session):
    c = _company()
    rows = [{"company_id": "c1", "url": "https://unrelated.example", "domain": "unrelated.example",
             "title": "x", "page_text": "nothing relevant here", "error": None}]
    monkeypatch.setattr(wd, "run_ts_crawler", lambda name, input_rows, **kw: rows)
    monkeypatch.setattr(wd, "rows_for_company", lambda rows, company_id: rows)

    result = wd.discover_website(c, db_session)
    assert result["website_url"] is None
    assert result["confidence"] is None


def test_discover_website_records_a_crawler_failure_without_raising(monkeypatch, db_session):
    from scrapers.node_crawler_base import CrawlerRunError

    def boom(name, input_rows, **kw):
        raise CrawlerRunError("website-discovery-crawler timed out after 90s")

    monkeypatch.setattr(wd, "run_ts_crawler", boom)
    result = wd.discover_website(_company(), db_session)
    assert result["website_url"] is None
    assert "website-discovery-crawler failed" in result["candidates"][0]["error"]


def test_extract_parent_mentions_from_the_real_amplia_text_english_and_italian():
    english = ("Amplia Engineering & Equipment was established in 2025 from the combination of a "
               "new industrial vision and the expertise of professionals with extensive experience "
               "in major infrastructure projects worldwide. Today, this expertise is brought "
               "together within Amplia Infrastructures, part of the Autostrade per l'Italia Group.")
    italian = ("Amplia Engineering & Equipment nasce nel 2025 dall'incontro tra una nuova visione "
               "industriale e il know-how di professionisti che hanno maturato esperienza su grandi "
               "progetti infrastrutturali a livello internazionale. Queste competenze oggi "
               "confluiscono in Amplia Infrastructures, società del Gruppo Autostrade per l'Italia.")
    assert wd._extract_parent_mentions(english) == ["Amplia Infrastructures", "Autostrade per l'Italia"]
    assert wd._extract_parent_mentions(italian) == ["Amplia Infrastructures", "Autostrade per l'Italia"]


def test_extract_parent_mentions_from_the_real_emag_text():
    assert wd._extract_parent_mentions("All Locations worldwide - EMAG Group") == ["EMAG"]


def test_extract_parent_mentions_finds_nothing_in_ordinary_prose():
    assert wd._extract_parent_mentions("We make high quality industrial valves since 1980.") == []


def test_extract_parent_mentions_handles_a_real_curly_apostrophe():
    """Live bug: the real amplia-eng.it page (fetched fresh, not hand-typed) uses the curly
    right single quote U+2019 throughout ("l’Italia", "dall’incontro"), not the
    ASCII apostrophe every other test here happens to use. A straight-quote-only character
    class silently broke the chain one word early: 'Autostrade' alone instead of 'Autostrade
    per l’Italia'."""
    text = ("Gruppo Autostrade per l’Italia\nAmplia Engineering & Equipment nasce nel 2025 "
            "dall’incontro tra una nuova visione industriale... Queste competenze oggi "
            "confluiscono in Amplia Infrastructures, società del Gruppo Autostrade per "
            "l’Italia.")
    assert wd._extract_parent_mentions(text) == ["Amplia Infrastructures", "Autostrade per l’Italia"]


def test_extract_parent_mentions_does_not_chain_across_navigation_menu_lines():
    """Live bug: page_text comes from the live page's innerText, which puts each nav/menu
    item on its own line. A newline-tolerant word separator chained straight through EMAG's
    real navigation bar into the captured name ("EMAG\\nContatti\\nNewsletter\\nMedia
    Center" instead of just "EMAG")."""
    text = "EMAG\nContatti\nNewsletter\nMedia Center\nAll Locations worldwide - EMAG Group"
    assert wd._extract_parent_mentions(text) == ["EMAG"]


def test_match_parent_company_requires_exact_token_equality_not_just_overlap(db_session):
    """Live bug found during this fix's own validation: this dataset has BOTH 'EMAG SU
    S.R.L.' and 'EMAG MILANO S.R.L.' as separate, real, unrelated companies. A subset/overlap
    match on the shared "EMAG" brand token would wrongly link one as the other's parent."""
    db_session.add_all([
        Company(id="su", legal_name="EMAG SU S.R.L.", registration_number="IT00000000010"),
        Company(id="milano", legal_name="EMAG MILANO S.R.L.", registration_number="IT00000000011"),
        Company(id="holding", legal_name="BigCo Holding S.p.A.", registration_number="IT00000000012"),
    ])
    db_session.commit()

    assert wd._match_parent_company(db_session, "EMAG", exclude_id="su") is None
    match = wd._match_parent_company(db_session, "BigCo Holding", exclude_id="su")
    assert match is not None and match.id == "holding"
    assert wd._match_parent_company(db_session, "Nonexistent Group", exclude_id="su") is None


def test_discover_website_still_accepts_a_vat_verified_page_that_also_mentions_a_parent(monkeypatch, db_session):
    """AMPLIA's own real page: specific VAT evidence wins even though the same page discloses
    a parent relationship - this is genuinely the company's own dedicated site."""
    c = _company()
    rows = [{"company_id": "c1", "url": "https://amplia-eng.it/", "domain": "amplia-eng.it",
             "title": "Amplia", "error": None,
             "page_text": "Amplia Engineering & Equipment s.r.l. P.IVA: 03146140300. "
                           "Part of the Autostrade per l'Italia Group."}]
    monkeypatch.setattr(wd, "run_ts_crawler", lambda name, input_rows, **kw: rows)
    monkeypatch.setattr(wd, "rows_for_company", lambda rows, company_id: rows)

    result = wd.discover_website(c, db_session)
    assert result["website_url"] == "https://amplia-eng.it/"
    assert result["confidence"] == "vat"
    assert result["parent"]["name"] == "Autostrade per l'Italia"
    assert result["parent"]["matched_company_id"] is None  # not in our DB - disclosed, not linked


def test_discover_website_treats_a_weakly_verified_parent_group_page_as_parent_not_own_site(monkeypatch, db_session):
    """EMAG SU S.R.L.'s real case: emag.com's generic "worldwide locations" page only clears
    name_and_location (no VAT), and explicitly reads as being about the parent group - must
    NOT become this subsidiary's own website_url."""
    c = _company(legal_name="EMAG SU S.R.L.", registration_number="IT00000000010",
                  external_ref_id="IT00000000010", province="Milano", region="Lombardia")
    rows = [{"company_id": "c1", "url": "https://www.emag.com/it/contatti/sedi/", "domain": "emag.com",
             "title": "EMAG", "error": None,
             "page_text": "All Locations worldwide - EMAG Group. Emag Su Milano, Italy."}]
    monkeypatch.setattr(wd, "run_ts_crawler", lambda name, input_rows, **kw: rows)
    monkeypatch.setattr(wd, "rows_for_company", lambda rows, company_id: rows)

    result = wd.discover_website(c, db_session)
    assert result["website_url"] is None
    assert result["parent"]["name"] == "EMAG"
    rejected = {cand["domain"]: cand.get("rejected") for cand in result["candidates"]}
    assert rejected["emag.com"] == "parent/group page, not a dedicated site for this company"


def test_discover_website_links_parent_company_id_when_the_parent_is_a_real_db_match(monkeypatch, db_session):
    db_session.add(Company(id="parent1", legal_name="BigCo Holding S.p.A.", registration_number="IT00000000099"))
    db_session.commit()
    c = _company(id="child1")
    rows = [{"company_id": "child1", "url": "https://unrelated.example", "domain": "unrelated.example",
             "title": "x", "error": None,
             "page_text": "Nothing about Amplia here, but it is a member of BigCo Holding."}]
    monkeypatch.setattr(wd, "run_ts_crawler", lambda name, input_rows, **kw: rows)
    monkeypatch.setattr(wd, "rows_for_company", lambda rows, company_id: rows)

    result = wd.discover_website(c, db_session)
    assert result["parent"]["matched_company_id"] == "parent1"
    assert result["parent"]["matched_legal_name"] == "BigCo Holding S.p.A."


def test_ensure_website_discovered_noops_when_a_url_is_already_on_file(db_session):
    company = Company(id="c1", legal_name="X SRL", registration_number="IT00000000001", website_url="https://x.example/")
    db_session.add(company)
    db_session.commit()
    assert wd.ensure_website_discovered(company, db_session) is None
    assert company.website_url == "https://x.example/"


def test_ensure_website_discovered_sets_website_url_and_writes_the_audit_trail(monkeypatch, db_session):
    company = Company(id="c1", legal_name="AMPLIA ENGINEERING & EQUIPMENT S.R.L.",
                       registration_number="IT31461403000", external_ref_id="IT03146140300",
                       province="Udine", country="Italy", website_url=None)
    db_session.add(company)
    db_session.commit()

    rows = [{"company_id": "c1", "url": "https://amplia-eng.it/en/", "domain": "amplia-eng.it",
             "title": "Amplia Engineering & Equipment", "page_text": "P.IVA 03146140300", "error": None}]
    monkeypatch.setattr(wd, "run_ts_crawler", lambda name, input_rows, **kw: rows)
    monkeypatch.setattr(wd, "rows_for_company", lambda rows, company_id: rows)

    result = wd.ensure_website_discovered(company, db_session)

    assert result["website_url"] == "https://amplia-eng.it/"
    assert company.website_url == "https://amplia-eng.it/"
    rec = db_session.query(RawImportRecord).filter_by(company_id="c1", dataset_name=wd.DATASET_NAME).first()
    assert rec is not None
    assert rec.raw_row["website_url"] == "https://amplia-eng.it/"


def test_ensure_website_discovered_leaves_website_url_unset_when_nothing_is_verified(monkeypatch, db_session):
    company = Company(id="c1", legal_name="Unverifiable SRL", registration_number="IT00000000002",
                       province="Udine", country="Italy", website_url=None)
    db_session.add(company)
    db_session.commit()

    rows = [{"company_id": "c1", "url": "https://unrelated.example", "domain": "unrelated.example",
             "title": "x", "page_text": "nothing relevant", "error": None}]
    monkeypatch.setattr(wd, "run_ts_crawler", lambda name, input_rows, **kw: rows)
    monkeypatch.setattr(wd, "rows_for_company", lambda rows, company_id: rows)

    result = wd.ensure_website_discovered(company, db_session)

    assert result["website_url"] is None
    assert company.website_url is None
    rec = db_session.query(RawImportRecord).filter_by(company_id="c1", dataset_name=wd.DATASET_NAME).first()
    assert rec is not None  # the audit trail still records "we looked and found nothing confident"


def test_ensure_website_discovered_links_parent_company_id_on_a_real_db_match(monkeypatch, db_session):
    parent = Company(id="parent1", legal_name="BigCo Holding S.p.A.", registration_number="IT00000000099")
    company = Company(id="c1", legal_name="AMPLIA ENGINEERING & EQUIPMENT S.R.L.",
                       registration_number="IT31461403000", external_ref_id="IT03146140300",
                       province="Udine", country="Italy", website_url=None)
    db_session.add_all([parent, company])
    db_session.commit()

    rows = [{"company_id": "c1", "url": "https://unrelated.example", "domain": "unrelated.example",
             "title": "x", "error": None,
             "page_text": "No VAT or name match here, but it is a member of BigCo Holding."}]
    monkeypatch.setattr(wd, "run_ts_crawler", lambda name, input_rows, **kw: rows)
    monkeypatch.setattr(wd, "rows_for_company", lambda rows, company_id: rows)

    wd.ensure_website_discovered(company, db_session)

    assert company.parent_company_id == "parent1"
    assert company.website_url is None  # the unrelated candidate still isn't verified as ITS OWN site
