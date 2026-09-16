"""
Tests for the keyless Google News RSS adapter. No network: _fetch is monkeypatched
with headlines taken verbatim from live runs, so these lock in the precision
failures that were actually observed rather than hypothetical ones.
"""

import pytest

from adapters import google_news_rss as rss


class _Co:
    def __init__(self, name, country="Italy"):
        self.id, self.legal_name, self.country = "cid", name, country


def _stub_feed(monkeypatch, titles):
    monkeypatch.setattr(rss, "_fetch", lambda q, l, c: [
        {"title": t, "url": f"https://news/{i}", "published": "", "outlet": "Outlet"}
        for i, t in enumerate(titles)
    ])


# ------------------------------------------------------------------ name handling

@pytest.mark.parametrize("legal_name,expected", [
    ("CANGINI BENNE S.R.L.", "CANGINI BENNE"),
    ("BREMBO S.P.A.", "BREMBO"),
    ("R.C.M. S.P.A.", "RCM"),          # dotted acronyms are written solid in headlines
    ("Muster Technik GmbH & Co. KG", "Muster Technik"),
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


def test_headline_must_contain_every_distinctive_token():
    tokens = rss._name_tokens("CANGINI BENNE S.R.L.")
    assert rss._is_about_company("Cangini Benne vende al gruppo Lifco AB", tokens)
    assert not rss._is_about_company("Il sindaco Cangini inaugura la scuola", tokens)


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
    _stub_feed(monkeypatch, ["Ferrari rinnova e amplia la partnership tecnica con Brembo"])
    out = rss._fetch_live(_Co("BREMBO S.P.A."))
    sig = out["signals"]["external_collaboration"]
    assert sig["value"] == 1.0 and sig["status"] == "present"
    assert "Ferrari" in sig["summary"]
    assert sig["evidence"]["found"][0]["url"].startswith("https://news/")
    assert "Google News RSS" in sig["evidence"]["method"]


def test_absence_is_never_asserted(monkeypatch):
    """No press coverage is not evidence of no partnerships — a Mittelstand firm
    simply may not be written about."""
    _stub_feed(monkeypatch, ["Marposs punta ai 100 milioni di fatturato in Cina"])
    out = rss._fetch_live(_Co("MARPOSS S.P.A."))
    assert out["signals"] == {}


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
    _stub_feed(monkeypatch, ["Ferrari rinnova e amplia la partnership tecnica con Brembo"])
    monkeypatch.setattr(rss, "NEWSAPI_KEY", "configured")
    out = rss._fetch_live(_Co("BREMBO S.P.A."))
    assert "external_collaboration" not in out["signals"]   # owned by news-signals-crawler
    assert "partnership_news_count" in out["signals"]       # owned by nothing else
