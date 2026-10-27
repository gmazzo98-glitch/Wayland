"""
People and contact intelligence (tickets I14 and I95).

Everything here is offline and uses INVENTED companies and people. The first version of these tests mocked the
benchmark company's own pages at the URLs the collector had hard-coded, so they could not fail; these tests make the
fetcher and the search client fake and then check that the collector fetched only what the homepage linked to or the
search returned (a hard-coded page would show up as an unexpected fetch).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from intel.collector import Budget, CollectorContext, check_result
from intel.collectors import contacts, people
from intel.fetch import Page
from intel.ledger import Evidence, Fact, Ledger, quote_in_text
from intel.people import (
    NAME_RE,
    PersonCandidate,
    classify_role,
    company_names,
    employer_is_company,
    extract_emails,
    extract_pec,
    extract_phones,
    is_classified_title,
    is_generic_business_email,
    is_suppressed,
    language_evidence,
    looks_like_organization,
    plausible_person_name,
    rank_pilot_sponsors,
    split_role_employer,
    strip_legal_form,
)
from intel.search import SearchResult

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Role vocabulary, classification, employer parsing
# ---------------------------------------------------------------------------

def test_classify_role_tiers():
    cases = {
        "Direttore Generale": ("general_management", 1, True),
        "Amministratore Delegato": ("general_management", 1, True),
        "Managing Director": ("general_management", 1, True),
        "Amministratore Unico": ("general_management", 1, True),
        "Geschäftsführer": ("general_management", 1, True),
        "Chief Technology Officer": ("operations_technology", 2, True),
        "Direttore Tecnico": ("operations_technology", 2, True),
        "Plant Manager": ("operations_technology", 2, True),
        "Technischer Leiter": ("operations_technology", 2, True),
        "Manager-Export & Distribution Network": ("commercial_export", 3, True),
        "Direttore Commerciale": ("commercial_export", 3, True),
        "Vertriebsleiter": ("commercial_export", 3, True),
        "Responsabile Acquisti": ("purchasing_procurement", 4, True),
        "Einkaufsleiter": ("purchasing_procurement", 4, True),
        "Direttore Marketing": ("other_management", 6, True),
        "Procuratore Speciale": ("signatory", 5, False),
        "Vice Presidente": ("board_parent", 5, False),
    }
    for raw, (group, rank, operating) in cases.items():
        grp, _norm, is_op, _parent, rnk, tier = classify_role(raw)
        assert (grp, rnk, is_op) == (group, rank, operating), raw
        assert tier.startswith(f"Tier {rnk}")


def test_president_and_managing_director_is_a_managing_director():
    grp, norm, is_op, _p, rank, _t = classify_role("Presidente e Amministratore Delegato")
    assert (grp, norm, is_op, rank) == ("general_management", "Managing Director / CEO", True, 1)


def test_foreign_board_member_is_flagged_as_likely_parent_and_not_operating():
    grp, _n, is_op, is_parent, rank, _t = classify_role(
        "Consigliere; Presidente Consiglio Amministrazione", nationality="United States")
    assert (grp, is_op, is_parent, rank) == ("board_parent", False, True, 5)


def test_no_company_name_is_a_parent_marker():
    # the old code treated two specific company names in a role text as "parent"; now only generic markers count
    assert classify_role("CEO of Some Corporation")[3] is False
    assert classify_role("Group CEO")[3] is True


def test_split_role_employer():
    assert split_role_employer("CEO of Some Corporation") == ("CEO", "Some Corporation")
    assert split_role_employer("Manager-Export & Distribution Network at Foo SpA") == (
        "Manager-Export & Distribution Network", "Foo SpA")
    assert split_role_employer("Direttore Generale di Meccanica Brambilla") == (
        "Direttore Generale", "Meccanica Brambilla")
    # a function after "of" is not an employer, and "Head of Sales" is not a title of the vocabulary
    assert split_role_employer("Director of Operations")[1] is None
    assert split_role_employer("Head of Sales") == ("Head of Sales", None)
    assert split_role_employer("Amministratore Delegato") == ("Amministratore Delegato", None)


def test_company_names_and_employer_match():
    names = company_names({"legal_name": "ROSSI VALVOLE S.P.A."})
    assert names == ["ROSSI VALVOLE"]            # the legal form is stripped; no short token is invented from the name
    assert company_names({"legal_name": "ROSSI VALVOLE S.P.A.", "brand": "Rossi Valvole"}) == ["ROSSI VALVOLE"]
    assert company_names({"legal_name": "ROSSI VALVOLE S.P.A.", "brand": "RV Group"}) == ["ROSSI VALVOLE", "RV Group"]
    assert company_names({"legal_name": "AB S.R.L."}) == []                   # too short to identify anything
    assert strip_legal_form("Brambilla Meccanica S.r.l.") == "Brambilla Meccanica"
    assert employer_is_company("Rossi Valvole S.p.A.", names)
    assert not employer_is_company("Gruppo Orion", names)
    assert employer_is_company(None, names)


def test_looks_like_organization_and_plausible_names():
    assert looks_like_organization("Studio Revisione Associati")
    assert looks_like_organization("Alfa & Beta S.p.A.")
    assert not looks_like_organization("Maria Conti")
    assert plausible_person_name("Maria Conti")
    assert plausible_person_name("Giovanni De Luca")
    assert not plausible_person_name("Direttore Generale")
    assert not plausible_person_name("Contatti Azienda")
    assert not plausible_person_name("Mario")
    assert is_classified_title("Direttore Tecnico") and not is_classified_title("Poeta")


def test_a_group_name_is_not_a_person_in_any_of_the_three_languages():
    # regression found on a German-named company: "CEO Alfa Gruppe" was stored as a person called "Alfa Gruppe"
    for org in ("Alfa Gruppe", "Alfa Unternehmensgruppe", "Alfa Gruppo", "Alfa Group", "Beta Stiftung", "Gamma Holding"):
        assert not plausible_person_name(org), org
        assert looks_like_organization(org) or not plausible_person_name(org)
    text = "Lina Rossi (CEO Alfa Gruppe) und Marco Bianchi, Geschäftsführer, leiten Alfa.\nCEO Alfa Gruppe\n"
    found = {c.full_name for c, _t in people._extract_candidates(text, "https://alfa.example/", "company_site", ["Alfa"], True, [])}
    assert found == {"Lina Rossi", "Marco Bianchi"}


def test_name_regex_stays_on_one_line_and_accepts_particles_and_apostrophes():
    import re
    assert re.fullmatch(NAME_RE, "Anna Maria D'Angelo")
    assert re.fullmatch(NAME_RE, "Luca de Santis")
    assert not re.fullmatch(NAME_RE, "Luca\nRossi")


def test_pilot_sponsor_ranking_order():
    p1 = PersonCandidate("Mario Rossi", "Amministratore Delegato", "general_management", "MD", True, False, 1)
    p2 = PersonCandidate("Luigi Verdi", "Direttore Tecnico", "operations_technology", "CTO", True, False, 2)
    p3 = PersonCandidate("Paolo Neri", "Export Manager", "commercial_export", "Export", True, False, 3)
    p4 = PersonCandidate("Anna Bianchi", "Responsabile Acquisti", "purchasing_procurement", "Purchasing", True, False, 4)
    p5 = PersonCandidate("Hans Becker", "Presidente", "board_parent", "Chairman", False, True, 5)
    ranked = rank_pilot_sponsors([p4, p5, p2, p1, p3])
    assert [p.full_name for p in ranked] == ["Mario Rossi", "Luigi Verdi", "Paolo Neri", "Anna Bianchi", "Hans Becker"]


# ---------------------------------------------------------------------------
# Contact extraction and policy (G2, G3)
# ---------------------------------------------------------------------------

def test_extract_phones_and_switchboard():
    text = "Sede operativa: Via Roma 8, 23900 Lecco\nCALL US: +39 0341 123456\nFax: +39 0341 123457"
    phones = extract_phones(text)
    assert phones and "0341 123456" in phones[0][0]
    for _norm, quote in phones:
        assert quote in text


def test_a_vat_number_is_never_read_as_the_switchboard():
    # regression: an 11-digit Italian VAT number printed in a footer was stored as "+39 0067..." by the unlabelled fallback
    footer = "Alfa Beta S.p.A. - Via Roma 1 - 39100 Bolzano\nP.IVA 00123456789 - C.F. 00123456789\nREA BZ-123456"
    assert extract_phones(footer) == []
    assert extract_phones("Partita IVA: 00123456789") == []
    assert extract_phones("Codice Fiscale e P.IVA 00123456789") == []
    assert extract_phones("Registro Imprese 00123456789") == []                # the 00 prefix without a plus is not a phone
    # regression found on a second company: "Tax code 04967750151" was stored as the switchboard
    assert extract_phones("Via Roma 167 · Tax code 04967750151\nShare capital 10,000") == []
    assert extract_phones("Codice fiscale 04967750151") == []
    assert extract_phones("Sede legale\n04967750151\nCapitale sociale") == []         # a solid 11-digit run, no label
    both = "P.IVA 00123456789 - Tel. 0471 954050"                              # the real telephone on the same line stays
    assert [n for n, _ in extract_phones(both)] == ["+39 0471 954050"]
    assert [n for n, _ in extract_phones("Sede: Via Roma 1\n0471 954050")] == ["+39 0471 954050"]


def test_extract_emails_and_pec_with_suppression():
    text = ("Commerciale: vendite@brambilla.example\nPosta certificata: brambilla@pec.it\nInfo: info@brambilla.example\n"
            "Privacy: privacy@brambilla.example\nDPO: dpo@brambilla.example\nAbuse: abuse@brambilla.example")
    found = [e for e, _ in extract_emails(text)]
    assert {"vendite@brambilla.example", "info@brambilla.example", "brambilla@pec.it"} <= set(found)
    assert not {"privacy@brambilla.example", "dpo@brambilla.example", "abuse@brambilla.example"} & set(found)
    pec = extract_pec(text)
    assert pec and pec[0] == "brambilla@pec.it" and "brambilla@pec.it" in pec[1]
    assert is_suppressed("noreply@x.example")


def test_generic_email_distinction_is_not_tied_to_any_brand():
    for generic in ("info@azienda.it", "sales@company.com", "contatti@impresa.it", "acmeinfo@x.example",
                    "brambillasales@x.example", "info.export@x.example", "ufficio.acquisti@x.example"):
        assert is_generic_business_email(generic), generic
    for personal in ("mario.rossi@azienda.it", "g.verdi@company.com", "mrossi@x.example", "luca_bianchi@x.example"):
        assert not is_generic_business_email(personal), personal


def test_language_evidence_needs_two_cues_and_a_real_line():
    it = "Chi siamo\nContattaci per informazioni sui prodotti\nTelefono: 0341 123456"
    out = dict(language_evidence(it))
    assert "it" in out and quote_in_text(out["it"], it)
    assert language_evidence("Home\nFax 0341 1") == []                       # no cue at all
    # regression: a VAT line shows an Italian company, not an Italian-language page, and is never the quote
    assert language_evidence("Alfa Beta S.p.A.\n- P.IVA 01276740162\nSede legale: Via Roma 1\nTelefono 0341 123456") == []
    # regression: "Phone: +39..." plus one more weak word must not make an English site, and a quote is never the phone line
    assert language_evidence("Sales office\nPhone : +39-02-9700071\nOur phone numbers") == []
    en = dict(language_evidence("Phone : +39-02-9700071\nContact us for more information\nAbout us and our products"))
    assert "en" in en and "phone" not in en["en"].lower()
    assert "en" not in dict(language_evidence("Prodotti e servizi\nPhone: 0341 123456\nChi siamo e telefono"))


# ---------------------------------------------------------------------------
# The people collector: registry, own site, search; verification; no hard-coded pages
# ---------------------------------------------------------------------------

SITE = "https://www.brambilla-meccanica.example"
PRESS_URL = "https://notizie.example/brambilla-entra-nel-gruppo-orion"
FAR_URL = "https://altro.example/articolo-lungo"
OTHER_URL = "https://calzature.example/negozio"
BEBEEZ_SEARCH = "https://bebeez.it/?s=MECCANICA+BRAMBILLA"

HOME = Page(url=SITE + "/", ok=True, status=200, title="Meccanica Brambilla",
            text="Meccanica Brambilla\nBenvenuti\nMeccanica Brambilla S.r.l. - Lecco",
            links=[SITE + "/azienda/management", SITE + "/news/2024", SITE + "/contatti", "https://facebook.com/x"],
            content_hash="h-home")
MANAGEMENT = Page(url=SITE + "/azienda/management", ok=True, status=200, title="Management",
                  text=("Il nostro management\nLuca Verdi\nDirettore Generale\nAnna Neri\nResponsabile Acquisti\n"
                        "Meccanica Brambilla S.r.l. - Via Roma 1, Lecco"), content_hash="h-mgmt")
PRESS = Page(url=PRESS_URL, ok=True, status=200, title="Brambilla entra nel gruppo Orion",
             text=("Meccanica Brambilla entra nel gruppo Orion\nMeccanica Brambilla S.r.l. di Lecco ha annunciato l'ingresso "
                   "nel gruppo Orion. Marco Colombo, Export Manager, guiderà le vendite estere di Meccanica Brambilla. "
                   "Paolo Ferrari, CEO of Gruppo Orion, ha commentato l'operazione."), content_hash="h-press")
FAR = Page(url=FAR_URL, ok=True, status=200, title="Articolo",
           text=("Meccanica Brambilla è citata una sola volta, qui. " + "Testo che parla d'altro. " * 40 +
                 "Giulia Rossi, Direttore Generale, dichiara che il negozio apre domani."), content_hash="h-far")
PAGES = {p.url: p for p in (HOME, MANAGEMENT, PRESS, FAR)}


class FakeSearch:
    def __init__(self):
        self.queries = []

    def search(self, query, kind="web", lang="it", n=10):
        self.queries.append((query, lang))
        return [
            SearchResult(url=PRESS_URL, title="Meccanica Brambilla entra nel gruppo Orion", snippet="deal story"),
            SearchResult(url=FAR_URL, title="Meccanica Brambilla", snippet="articolo"),
            SearchResult(url=OTHER_URL, title="Calzature Brambilla", snippet="negozio di scarpe"),
        ]


def _company(**extra):
    base = {"id": None, "legal_name": "MECCANICA BRAMBILLA S.R.L.", "vat": "01234567890", "city": "Lecco (LC)",
            "website_url": SITE,
            "people": [
                {"full_name": "Sig. Elena Galli", "role": "CONSIGLIERE", "nationality": "Italy"},
                {"full_name": "Hans Becker", "role": "CONSIGLIERE", "nationality": "Germany"},
                {"full_name": "Studio Revisione Associati S.p.A.", "role": "ADV"},
                {"full_name": "Piero Gatti", "role": "CONSIGLIERE", "current_or_former": "Former"},
            ]}
    base.update(extra)
    return base


def _run(company=None, search=True, ledger=None, budget=None):
    fetched = []

    def fetch(url):
        fetched.append(url)
        page = PAGES.get(url) or PAGES.get(url.rstrip("/") + "/")        # the real fetcher normalises the root URL
        return page or Page(url=url, ok=False, status=404, note="not found")

    fake = FakeSearch() if search else None
    ctx = CollectorContext(company=company or _company(), ledger=ledger or Ledger(None), budget=budget or Budget(),
                           search=fake, fetch=fetch)
    return people.run(ctx), fetched, fake


def _by_key(result):
    out = {}
    for f in result.facts:
        out.setdefault(f.key, []).append(f)
    return out


def test_people_collector_finds_management_press_and_separates_other_organisations():
    res, fetched, fake = _run()
    assert check_result(people.COLLECTOR, res) == []
    assert Ledger(None).add_many(res.facts) == []                       # every fact is valid for the ledger
    keys = _by_key(res)

    gm = keys["people.general_manager"][0]                              # own site: grade B
    assert gm.value["name"] == "Luca Verdi" and gm.grade == "B" and gm.verified
    assert gm.evidence[0].url == MANAGEMENT.url
    assert keys["people.purchasing_manager"][0].value["name"] == "Anna Neri"

    exp = keys["people.export_manager"][0]                              # searched press: grade C, operating
    assert exp.value["name"] == "Marco Colombo" and exp.grade == "C" and exp.value["is_operating"] is True
    assert exp.evidence[0].url == PRESS_URL

    other = keys["people.other_org_executive"][0]                       # another organisation's CEO is not management
    assert other.value["name"] == "Paolo Ferrari" and other.value["employer"] == "Gruppo Orion"
    assert other.value["is_operating"] is False
    assert "people.managing_director" not in keys

    # the same-brand article that names the company once, far from the person, is dropped (locality), and the
    # unrelated shoe-shop result is never even fetched
    assert all(f.value.get("name") != "Giulia Rossi" for f in res.facts if isinstance(f.value, dict))
    assert OTHER_URL not in fetched


def test_people_collector_fetches_only_what_the_homepage_linked_or_search_returned():
    _res, fetched, fake = _run()
    allowed = {SITE, SITE + "/", SITE + "/azienda/management", PRESS_URL, FAR_URL, BEBEEZ_SEARCH}
    assert set(fetched) <= allowed, set(fetched) - allowed
    assert fake.queries and len(fake.queries) <= Budget().max_search_queries
    assert all("brambilla" in q.lower() for q, _ in fake.queries)
    langs = {lang for _, lang in fake.queries}
    assert {"it", "en", "de"} <= langs                                   # a small budget still reaches every language


def test_people_collector_registry_roster_organisations_and_foreign_directors():
    res, _f, _s = _run()
    keys = _by_key(res)
    firm = keys["people.advisor_firm"][0]
    assert firm.value["entity_type"] == "organization" and firm.grade == "A"
    assert "Studio Revisione" in firm.value["name"]
    boards = {f.value["name"]: f for f in keys["people.board_member"]}
    assert "Elena Galli" in boards                                       # "Sig." stripped
    becker = boards["Hans Becker"]
    assert becker.value["is_parent_director"] is True and "heuristic" in becker.value["parent_basis"]
    assert "people.parent_executive" not in keys                         # nationality alone never confirms a parent
    assert all(f.value.get("name") != "Piero Gatti" for f in res.facts if isinstance(f.value, dict))   # former officer
    assert any("former officer" in e for e in res.errors)


def test_registry_placeholders_and_unknown_roles_are_not_filed_as_firms_or_board_seats():
    rows = [
        {"full_name": "Auditors or advisors are available, yet none related to the selected filter", "role": "ADV"},
        {"full_name": "Mario Verdi", "role": "RESPONSABILE TECNICO"},                  # a role the vocabulary does not know
        {"full_name": "Paola Neri", "role": "CONSIGLIERE"},
    ]
    res, _f, _s = _run(company=_company(people=rows))
    keys = _by_key(res)
    assert "people.advisor_firm" not in keys
    assert not any("selected filter" in str(f.value) for f in res.facts)
    assert keys["people.other_officer"][0].value["name"] == "Mario Verdi"
    assert "Mario Verdi" not in [f.value["name"] for f in keys.get("people.board_member", [])]
    assert "Paola Neri" in [f.value["name"] for f in keys["people.board_member"]]
    assert people._is_placeholder_name("Nessun dato disponibile per il filtro selezionato")
    assert not people._is_placeholder_name("Studio Revisione Associati S.p.A.")


def test_former_officer_rule_does_not_treat_a_future_term_end_as_a_resignation():
    from datetime import datetime, timedelta, timezone
    future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=400)
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=400)
    # in the imported registry data resignation_date can be the end of the CURRENT term: still in office
    assert people._is_former({"current_or_former": "Current", "resignation_date": future}) is False
    assert people._is_former({"current_or_former": None, "resignation_date": future}) is False
    assert people._is_former({"current_or_former": None, "resignation_date": future.isoformat()}) is False
    assert people._is_former({"current_or_former": None, "resignation_date": past}) is True
    assert people._is_former({"current_or_former": "Former", "resignation_date": None}) is True
    assert people._is_former({"current_or_former": "Current", "resignation_date": past}) is False   # the status wins
    assert people._is_former({}) is False


def test_parent_executive_only_when_the_ledger_confirms_the_owner():
    ledger = Ledger(None)
    ledger.add(Fact(key="ownership.direct_owner", value="Gruppo Orion S.p.A.", grade="A",
                    evidence=[Evidence(source_type="registry", ref="AIDA shareholders")]))
    res, _f, _s = _run(ledger=ledger)
    keys = _by_key(res)
    assert "people.other_org_executive" not in keys
    parent = keys["people.parent_executive"][0]
    assert parent.value["name"] == "Paolo Ferrari" and parent.value["is_parent_director"] is True
    assert parent.value["is_operating"] is False


def test_pilot_sponsors_are_operating_management_of_this_company_best_tier_first():
    res, _f, _s = _run()
    summary = [f for f in res.facts if f.key == "people.pilot_sponsors"][0]
    names = [p["name"] for p in summary.value]
    assert names == ["Luca Verdi", "Marco Colombo", "Anna Neri"]        # tier 1, tier 3, tier 4
    assert "Paolo Ferrari" not in names and "Hans Becker" not in names
    assert any(e.url for e in summary.evidence)                         # a non-registry grade needs a URL and a quote


def test_every_web_fact_quote_is_verbatim_on_the_page_it_cites():
    res, _f, _s = _run()
    checked = 0
    for f in res.facts:
        for ev in f.evidence:
            if ev.url and ev.quote:
                assert quote_in_text(ev.quote, PAGES[ev.url].text), (f.key, ev.quote)
                checked += 1
    assert checked >= 4


def test_without_search_nothing_beyond_the_site_is_fetched_and_no_absence_is_claimed():
    res, fetched, _s = _run(search=False)
    assert set(fetched) <= {SITE, SITE + "/", SITE + "/azienda/management", BEBEEZ_SEARCH}
    assert any("no search client" in e for e in res.errors)
    assert res.checked_absent == []                                      # never looked at the press: not "absent"
    assert "people.general_manager" in _by_key(res)


def test_absence_is_claimed_only_after_the_site_and_the_search_were_read():
    res, _f, _s = _run()
    assert "people.managing_director" in res.checked_absent
    assert "people.general_manager" not in res.checked_absent            # found, so not absent


def test_family_surname_is_accepted_but_the_company_name_is_never_a_person():
    page = Page(url="https://www.rossi-valvole.example/", ok=True, status=200, title="Rossi Valvole",
                text="Rossi Valvole S.p.A.\nBiella\nLuca Rossi, Presidente e Amministratore Delegato\n"
                     "Rossi Valvole, Direttore Generale", links=[], content_hash="h")
    company = {"id": None, "legal_name": "ROSSI VALVOLE S.P.A.", "city": "Biella (BI)",
               "website_url": "https://www.rossi-valvole.example", "people": [{"full_name": "Zeno Rossi", "role": "CONSIGLIERE"}]}
    ctx = CollectorContext(company=company, ledger=Ledger(None), budget=Budget(),
                           fetch=lambda u: page if "rossi-valvole" in u else Page(url=u, ok=False))
    res = people.run(ctx)
    md = [f for f in res.facts if f.key == "people.managing_director"]
    assert md and md[0].value["name"] == "Luca Rossi"
    assert not any(f.value.get("name") in ("Rossi Valvole",) for f in res.facts if isinstance(f.value, dict))


def test_site_search_endpoint_follows_only_links_that_carry_the_company_slug():
    deal = "https://bebeez.it/private-equity/meccanica-brambilla-nuovo-socio/"
    unrelated = "https://bebeez.it/private-equity/altra-azienda-acquisita/"
    pages = {
        BEBEEZ_SEARCH: Page(url=BEBEEZ_SEARCH, ok=True, status=200, text="Risultati della ricerca",
                            links=[deal, unrelated, "https://bebeez.it/chi-siamo/"], content_hash="s"),
        deal: Page(url=deal, ok=True, status=200, title="Nuovo socio",
                   text=("Un fondo entra in Meccanica Brambilla S.r.l., azienda di Lecco. Sara Fontana (amministratore "
                         "delegato, che resta alla guida) ha dichiarato che il piano continua."), content_hash="d"),
    }
    fetched = []

    def fetch(url):
        fetched.append(url)
        return pages.get(url) or Page(url=url, ok=False, status=404, note="not found")

    company = _company(website_url=None, people=[])
    res = people.run(CollectorContext(company=company, ledger=Ledger(None), budget=Budget(), fetch=fetch))
    assert deal in fetched and unrelated not in fetched and "https://bebeez.it/chi-siamo/" not in fetched
    md = [f for f in res.facts if f.key == "people.managing_director"]
    assert md and md[0].value["name"] == "Sara Fontana" and md[0].grade == "C" and md[0].evidence[0].url == deal
    assert any("no search client" in e for e in res.errors)          # web search was not available, and it says so


def test_a_slow_empty_web_search_cannot_starve_the_pages_already_found(monkeypatch):
    # regression: under load the web search used the whole 240 s deadline, and the deal stories the declared source
    # had already offered were never read. Reliable pages are read first and the search gets a share of the budget.
    import types
    deal = "https://bebeez.it/private-equity/meccanica-brambilla-nuovo-socio/"
    pages = {
        BEBEEZ_SEARCH: Page(url=BEBEEZ_SEARCH, ok=True, status=200, text="Risultati", links=[deal], content_hash="s"),
        deal: Page(url=deal, ok=True, status=200, title="Nuovo socio",
                   text=("Un fondo entra in Meccanica Brambilla S.r.l., azienda di Lecco. Sara Fontana (amministratore "
                         "delegato, che resta alla guida) ha dichiarato che il piano continua."), content_hash="d"),
    }
    clock = [0.0]
    monkeypatch.setattr(people, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))

    class SlowSilent:
        def __init__(self):
            self.calls = 0

        def search(self, query, kind="web", lang="it", n=10):
            self.calls += 1
            clock[0] += 100.0                       # every query "takes" 100 s and finds nothing
            return []

    slow = SlowSilent()
    company = _company(website_url=None, people=[])
    res = people.run(CollectorContext(company=company, ledger=Ledger(None), budget=Budget(deadline_seconds=240),
                                      search=slow, fetch=lambda u: pages.get(u) or Page(url=u, ok=False, status=404)))
    assert [f.value["name"] for f in res.facts if f.key == "people.managing_director"] == ["Sara Fontana"]
    assert slow.calls == 2                               # stopped at 55% of the budget, not at the deadline
    assert any("search stopped at the deadline" in e for e in res.errors)


def test_web_search_that_returns_nothing_is_reported_and_never_read_as_absence():
    class Silent:
        def search(self, query, kind="web", lang="it", n=10):
            return []

    pages = {SITE: HOME, SITE + "/azienda/management": MANAGEMENT}
    ctx = CollectorContext(company=_company(people=[]), ledger=Ledger(None), budget=Budget(), search=Silent(),
                           fetch=lambda u: pages.get(u.rstrip("/")) or Page(url=u, ok=False, status=404))
    res = people.run(ctx)
    assert any("web search returned no result" in e and "not evidence of absence" in e for e in res.errors)
    assert res.checked_absent == []


def test_names_and_titles_split_over_lines_by_inline_links_are_still_found():
    # text extracted from HTML puts each inline link on its own line: the realistic shape of a news article
    text = ("Il fondo ha acquisito la maggioranza, mentre due manager e soci di Alfa Beta spa,\nGiorgio Salvi\n"
            "(amministratore delegato, che manterrà la carica) e\nLuisa Mancini\n(direttore generale), hanno reinvestito.\n"
            "Carlo Fumagalli\n(già manager di un grande gruppo), azionista e consigliere con delega, entra in società.\n"
            "Elio Sartori\n, amministratore delegato di Fondo Delta, ha commentato l'operazione.")
    names = ["Alfa Beta"]
    found = {c.full_name: c for c, _t in people._extract_candidates(text, "https://x.example/a", "press", names, False, [])}
    assert found["Giorgio Salvi"].normalized_role == "Managing Director / CEO" and found["Giorgio Salvi"].affiliation == "company"
    assert found["Luisa Mancini"].normalized_role == "General Manager"
    assert found["Carlo Fumagalli"].normalized_role == "Executive Director"
    assert found["Elio Sartori"].affiliation == "other_org" and found["Elio Sartori"].employer == "Fondo Delta"
    assert found["Elio Sartori"].is_operating is False
    for cand in found.values():
        assert quote_in_text(cand.quote, text)


def test_only_the_first_characters_of_a_page_are_scanned_for_people():
    head = "Giorgio Salvi, amministratore delegato di Alfa Beta spa, ha commentato. "
    text = head + ("Testo d'altro. " * 20000) + " Luisa Mancini, direttore generale di Alfa Beta spa, ha detto."
    assert len(text) > people.MAX_SCAN_CHARS
    found = {c.full_name for c, _t in people._extract_candidates(text, "https://x.example/a", "press", ["Alfa Beta"], False, [])}
    assert found == {"Giorgio Salvi"}                                    # the second person is past the scan limit


def test_team_page_pairs_do_not_shift_when_names_and_titles_alternate():
    raw = people._raw_matches("Luca Verdi\nDirettore Generale\nAnna Neri\nResponsabile Acquisti\n")
    pairs = {(n, r) for n, r, _s, _i in raw}
    assert ("Luca Verdi", "Direttore Generale") in pairs and ("Anna Neri", "Responsabile Acquisti") in pairs
    assert ("Anna Neri", "Direttore Generale") not in pairs


def test_registry_failure_is_reported_without_leaking_connection_details(monkeypatch):
    import database

    def boom():
        raise RuntimeError("postgresql://user:hunter2@secret-host.example/db refused")

    monkeypatch.setattr(database, "get_db_session", boom)
    company = _company(id="some-company-id", people=None)
    ctx = CollectorContext(company=company, ledger=Ledger("x"), budget=Budget(),
                           fetch=lambda u: Page(url=u, ok=False, status=404))
    res = people.run(ctx)
    text = " ".join(res.errors)
    assert "registry roster not read: RuntimeError" in text
    assert "hunter2" not in text and "secret-host" not in text


def test_people_collector_never_writes_to_the_database_or_company_people():
    import re
    src = (ROOT / "intel" / "collectors" / "people.py").read_text(encoding="utf-8")
    assert not re.search(r"\b(?:db|session)\.(?:add|add_all|commit|delete|execute|merge|flush)\(", src)
    assert not re.search(r"\b(?:INSERT INTO|UPDATE \w+ SET|DELETE FROM)\b", src)
    assert "linkedin.com" not in src.lower()                                  # no LinkedIn-derived data (G2 is open)


# ---------------------------------------------------------------------------
# The contacts collector: evidence must support the claim it is attached to
# ---------------------------------------------------------------------------

def test_contacts_collector_offline_and_evidence_quality():
    site = "https://www.acme-meccanica.example"
    contact_text = ("Contatti - ACME Meccanica\nChi siamo\nContattaci per informazioni\nTelefono: +39 02 1234567\n"
                    "Email: info@acme-meccanica.example\nPEC: acmemeccanica@pec.it\n"
                    "Mario Rossi: mario.rossi@acme-meccanica.example")
    pages = {
        site + "/": Page(url=site + "/", ok=True, status=200, text="ACME Meccanica\nBenvenuti nel sito",
                         links=[site + "/contatti"], content_hash="h-home"),
        site + "/contatti": Page(url=site + "/contatti", ok=True, status=200, text=contact_text, content_hash="h-c"),
    }
    company = {"id": "t1", "legal_name": "ACME MECCANICA S.P.A.", "website_url": site}
    ctx = CollectorContext(company=company, ledger=Ledger("t1"), budget=Budget(),
                           fetch=lambda u: pages.get(u) or Page(url=u, ok=False, status=404))
    res = contacts.run(ctx)
    assert check_result(contacts.COLLECTOR, res) == []
    assert Ledger("t1").add_many(res.facts) == []
    facts = {f.key: f for f in res.facts}
    assert "02 1234567" in facts["contact.switchboard"].value
    assert facts["contact.pec"].value == "acmemeccanica@pec.it"
    assert [f.value for f in res.facts if f.key == "contact.generic_email"] == ["info@acme-meccanica.example"]
    assert not any("mario.rossi" in str(f.value) for f in res.facts)       # a personal address is never emitted

    page_fact = facts["contact.contact_page"]                              # evidenced by the contact page itself
    assert page_fact.value == site + "/contatti" and page_fact.evidence[0].url == site + "/contatti"
    assert quote_in_text(page_fact.evidence[0].quote, contact_text)
    assert not quote_in_text(page_fact.evidence[0].quote, pages[site + "/"].text)

    langs = facts["contact.languages"]
    assert langs.value == ["it"]
    assert all(quote_in_text(e.quote, contact_text) for e in langs.evidence)


def test_contacts_without_contact_page_makes_no_contact_page_fact():
    site = "https://www.plain.example"
    pages = {site + "/": Page(url=site + "/", ok=True, status=200, text="Solo una pagina senza contatti utili",
                              links=[], content_hash="h")}
    ctx = CollectorContext(company={"id": "t2", "legal_name": "PLAIN S.R.L.", "website_url": site}, ledger=Ledger("t2"),
                           budget=Budget(), fetch=lambda u: pages.get(u) or Page(url=u, ok=False, status=404))
    res = contacts.run(ctx)
    assert not any(f.key == "contact.contact_page" for f in res.facts)
    assert "contact.switchboard" in res.checked_absent


# ---------------------------------------------------------------------------
# The runner gives collectors a search client
# ---------------------------------------------------------------------------

def test_intel_run_builds_a_search_client_unless_disabled():
    spec = importlib.util.spec_from_file_location("intel_run", ROOT / "scripts" / "intel_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.build_search(None, enabled=False) is None
    client = mod.build_search(None, enabled=True)
    assert client is not None and hasattr(client, "search")
