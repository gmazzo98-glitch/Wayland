"""
Tests for the keyless Google News RSS adapter. No network: _fetch is monkeypatched
with headlines taken verbatim from live runs, so these lock in the precision
failures that were actually observed rather than hypothetical ones.
"""

import pytest

from adapters import google_news_rss as rss


class _Co:
    def __init__(self, name, country="Italy", sector_name=None, province=None, region=None):
        self.id, self.legal_name, self.country = "cid", name, country
        self.sector_name, self.province, self.region = sector_name, province, region


def _stub_feed(monkeypatch, titles, outlet="Outlet"):
    monkeypatch.setattr(rss, "_fetch", lambda q, l, c: [
        {"title": t, "url": f"https://news/{i}", "published": "", "outlet": outlet}
        for i, t in enumerate(titles)
    ])


# ------------------------------------------------------------------ name handling

@pytest.mark.parametrize("legal_name,expected", [
    ("CANGINI BENNE S.R.L.", "CANGINI BENNE"),
    ("BREMBO S.P.A.", "BREMBO"),
    ("R.C.M. S.P.A.", "RCM"),          # dotted acronyms are written solid in headlines
    ("Muster Technik GmbH & Co. KG", "Muster Technik"),
    # "& C." and lone initials never appear in a headline — with them in, the quoted
    # phrase search for Biglia and A Due returned nothing, ever (2026-09-18 run).
    ("OFFICINE E. BIGLIA & C. S.P.A.", "OFFICINE BIGLIA"),
    ("A DUE DI SQUERI DONATO & C. S.P.A.", "A DUE DI SQUERI DONATO"),
    ("SAN MARCO RUBINETTERIA - S.R.L.", "SAN MARCO RUBINETTERIA"),
    ("FIMER TECNOLOGIA DELL'IMBOTTIGLIAMENTO SRL", "FIMER TECNOLOGIA DELL'IMBOTTIGLIAMENTO"),
])
def test_search_name_strips_legal_form(legal_name, expected):
    assert rss._search_name(legal_name) == expected


def test_generic_names_are_not_attributed():
    """'RCM' matched 11 headlines about an unrelated dam-building contractor, and
    'MORE' would match any headline containing the word."""
    assert rss._name_is_distinctive("BREMBO S.P.A.")
    assert rss._name_is_distinctive("CANGINI BENNE S.R.L.")
    assert not rss._name_is_distinctive("R.C.M. S.P.A.")
    assert not rss._name_is_distinctive("MORE S.R.L.")
    assert not rss._name_is_distinctive("TURO ITALIA S.P.A.")   # "turo" alone is the car-sharing company


def test_short_single_token_names_need_a_discriminator():
    """Probed live: "Fimer" alone returns 25 headlines about the solar-inverter FIMER,
    not the bottling-machine maker. A lone short token is only attributed together
    with a place/sector word from the company record."""
    assert rss._attribution_mode("FIMER S.R.L.") == "tokens+discriminator"
    assert rss._attribution_mode("BIGLIA S.P.A.") == "tokens+discriminator"
    assert rss._attribution_mode("TECNOINOX S.R.L.") == "tokens"        # 9 chars, not a word
    assert rss._attribution_mode("CANGINI BENNE S.R.L.") == "tokens"    # two tokens must both appear
    assert rss._attribution_mode("MORE S.R.L.") == "skip"


def test_discriminators_come_from_the_record_not_the_boilerplate():
    co = _Co("FIMER S.R.L.", province="Asti",
             sector_name="Fabbricazione di macchine automatiche per la dosatura, la confezione e per l'imballaggio (incluse parti e accessori)")
    assert rss._discriminators(co) == ["asti", "automatiche", "confezione", "dosatura", "imballaggio"]


def test_headline_must_contain_every_distinctive_token():
    tokens = rss._name_tokens("CANGINI BENNE S.R.L.")
    assert rss._is_about_company("Cangini Benne vende al gruppo Lifco AB", tokens)
    assert not rss._is_about_company("Il sindaco Cangini inaugura la scuola", tokens)


def test_tokens_match_whole_words_only():
    """Substring matching accepted "due" inside "Le due aziende" and "mac" inside
    "macchine" (2026-09-18 verification run)."""
    a_due = rss._name_tokens("A DUE DI SQUERI DONATO & C. S.P.A.")
    assert a_due == ["due", "squeri", "donato"]
    assert rss._is_about_company("Le due aziende di Donato Squeri crescono", a_due)   # all three present, as words
    assert not rss._is_about_company("Le due aziende parmensi che crescono", a_due)
    vibro = rss._name_tokens("VIBRO-MAC S.R.L.")
    assert not rss._is_about_company("Vibrofinitrice per macchine utensili", vibro)
    assert rss._is_about_company("Vibro-Mac apre un nuovo stabilimento", vibro)
    assert rss._is_about_company("Vibromac apre un nuovo stabilimento", vibro)   # hyphenated name written solid
    san_marco = rss._name_tokens("SAN MARCO RUBINETTERIA - S.R.L.")
    assert not rss._is_about_company("Sanità, Marco Rossi visita la rubinetteria", san_marco)


# ------------------------------------------------------------------ classification

def test_finance_coverage_is_dropped(monkeypatch):
    """Datalogic's window was almost all takeover-bid and results stories, which say
    nothing about innovation capacity."""
    _stub_feed(monkeypatch, [
        "Datalogic, perdita semestrale si allarga a 3,3 milioni di euro",
        "Datalogic verso l'addio a Piazza Affari: Hydra lancia l'Opa per il delisting",
        "Datalogic apre un nuovo centro di ricerca con l'università di Bologna",
    ])
    kept = rss._company_headlines(_Co("DATALOGIC S.P.A."))
    assert [h["title"] for h in kept] == ["Datalogic apre un nuovo centro di ricerca con l'università di Bologna"]


def test_finance_filter_no_longer_eats_innovazione(monkeypatch):
    """The bare `azion` pattern matched inside innovAZIONe / collaborAZIONe and the
    outlet suffix 'ristorAZIONemoderna.it', deleting the exact headlines the
    innovation and partnership buckets exist to find (2026-09-18 run)."""
    _stub_feed(monkeypatch, [
        "Tecnoinox svela Itineris: la cucina professionale mobile - ristorazionemoderna.it",
        "Tecnoinox e il Politecnico di Milano: collaborazione per l'innovazione - Outlet",
        "Tecnoinox, utile in crescita nel semestre - Outlet",
    ], outlet="Outlet")
    kept = rss._company_headlines(_Co("TECNOINOX S.R.L."))
    assert [h["headline"] for h in kept] == [
        "Tecnoinox svela Itineris: la cucina professionale mobile - ristorazionemoderna.it",
        "Tecnoinox e il Politecnico di Milano: collaborazione per l'innovazione",
    ]


def test_finance_headline_that_also_carries_a_concept_is_kept(monkeypatch):
    _stub_feed(monkeypatch, ["Datalogic, ricavi record grazie alla partnership con Amazon"])
    kept = rss._company_headlines(_Co("DATALOGIC S.P.A."))
    assert len(kept) == 1


def test_takeover_bid_is_not_a_product_launch():
    """'lancia l'OPA' is launching a tender offer — counting it as a product launch
    reported 4 launches for a company whose news was entirely about a buyout."""
    assert not rss.CONCEPT_PATTERNS["launch"].search("Hydra lancia un'OPA a 5,7 euro per azione")
    assert rss.CONCEPT_PATTERNS["launch"].search("Marposs presenta la nuova gamma di sensori")
    assert rss.CONCEPT_PATTERNS["launch"].search("L'azienda lancia il nuovo modello compatto")


def test_real_partnership_and_ma_headlines_are_classified():
    assert rss.CONCEPT_PATTERNS["partnership"].search(
        "Ferrari rinnova e amplia la partnership tecnica con Brembo")
    assert rss.CONCEPT_PATTERNS["ma"].search(
        "Brembo, l'acquisizione della divisione sospensioni di Tenneco")
    assert rss.CONCEPT_PATTERNS["ma"].search("Cangini Benne vende al gruppo Lifco AB")
    assert rss.CONCEPT_PATTERNS["university"].search(
        "Nuovo laboratorio con il Politecnico di Milano")


# ------------------------------------------------------------------ signal contract

def test_signals_carry_linked_evidence(monkeypatch):
    _stub_feed(monkeypatch, ["Amazon rinnova e amplia la partnership tecnica con Datalogic"])
    out = rss._fetch_live(_Co("DATALOGIC S.P.A."))
    sig = out["signals"]["external_collaboration"]
    assert sig["value"] == 1.0 and sig["status"] == "present"
    assert "Amazon" in sig["summary"]
    assert sig["evidence"]["found"][0]["url"].startswith("https://news/")
    assert "Google News RSS" in sig["evidence"]["method"]


def test_short_name_headline_without_discriminator_is_not_attributed(monkeypatch):
    fimer = _Co("FIMER S.R.L.", province="Asti",
                sector_name="Fabbricazione di macchine per l'imbottigliamento (incluse parti e accessori)")
    _stub_feed(monkeypatch, [
        "Fotovoltaico: Fimer assegnata alla MA Solar Italy Limited",     # the solar-inverter FIMER
        "Fimer di Asti presenta la nuova linea di imbottigliamento",       # this company, named by town
    ])
    out = rss._fetch_live(fimer)
    assert out["raw_payload"]["attribution_mode"] == "tokens+discriminator"
    assert out["raw_payload"]["dropped_no_discriminator"] == 1
    assert [h["headline"] for h in out["raw_payload"]["kept_headlines"]] == [
        "Fimer di Asti presenta la nuova linea di imbottigliamento"]
    assert out["signals"]["press_launch_mentions"]["value"] == 1.0


def test_absence_is_never_asserted(monkeypatch):
    """No press coverage is not evidence of no partnerships — a Mittelstand firm
    simply may not be written about."""
    _stub_feed(monkeypatch, ["Datalogic punta ai 100 milioni di fatturato in Cina"])
    out = rss._fetch_live(_Co("DATALOGIC S.P.A."))
    assert out["signals"] == {}


def test_zero_signal_run_still_records_what_it_read(monkeypatch):
    """Before, a company with three genuine headlines and no concept hit looked
    identical to one the feed had never heard of."""
    _stub_feed(monkeypatch, ["Datalogic punta ai 100 milioni di fatturato in Cina", "Il meteo di domani"])
    captured = {}
    rss._fetch_live(_Co("DATALOGIC S.P.A."), captured)
    payload = captured["payload"]
    assert payload["headlines_fetched"] == 2 and payload["headlines_about_company"] == 1
    assert payload["dropped_not_about_company"] == 1
    assert payload["kept_headlines"][0]["headline"] == "Datalogic punta ai 100 milioni di fatturato in Cina"


def test_generic_name_short_circuits_before_any_request(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not have issued a request for a generic name")
    monkeypatch.setattr(rss, "_fetch", boom)
    out = rss._fetch_live(_Co("MORE S.R.L."))
    assert out["signals"] == {}
    assert "too generic" in out["raw_payload"]["skipped"]


def test_newsapi_crawlers_keep_ownership_of_their_keys(monkeypatch):
    """One producer per signal_key: when the paid crawlers can run, the free fallback
    yields the keys they own and keeps only the ones nothing else produces."""
    _stub_feed(monkeypatch, ["Amazon rinnova e amplia la partnership tecnica con Datalogic"])
    monkeypatch.setattr(rss, "NEWSAPI_KEY", "configured")
    out = rss._fetch_live(_Co("DATALOGIC S.P.A."))
    assert "external_collaboration" not in out["signals"]   # owned by news-signals-crawler
    assert "partnership_news_count" in out["signals"]       # owned by nothing else
