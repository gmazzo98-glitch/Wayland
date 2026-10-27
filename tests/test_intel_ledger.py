"""
The evidence ledger, the collector contract and the reference collector (intel/).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from intel import collector as coll_mod
from intel.collector import Budget, Collector, CollectorContext, CollectorResult, check_result, discover
from intel.collectors import site_headers
from intel.fetch import Page, html_to_text, normalise_url
from intel.ledger import Evidence, Fact, Ledger, LedgerError, fact_id, quote_in_text


def web_fact(key="ownership.direct_owner", value="Mott Corporation", grade="C", quote="Mott acquired ASCO Filtri", **kw):
    return Fact(key=key, value=value, grade=grade, collector="T:test",
                evidence=[Evidence(source_type="press", url="https://example.com/a", quote=quote)], **kw)


# ---- the rules of the contract --------------------------------------------------------------------

def test_a_web_fact_needs_a_url_and_a_real_quote():
    web_fact().validate()
    with pytest.raises(LedgerError):
        Fact(key="ownership.direct_owner", value="x", grade="C",
             evidence=[Evidence(source_type="press", url="https://e.com", quote=None)]).validate()
    with pytest.raises(LedgerError):
        web_fact(quote="too short").validate()
    with pytest.raises(LedgerError):
        Fact(key="ownership.direct_owner", value="x", grade="C",
             evidence=[Evidence(source_type="press", url=None, quote="a long enough verbatim quote")]).validate()


def test_a_fact_without_evidence_or_with_a_bad_key_grade_status_is_refused():
    for bad in (
        Fact(key="Owner", value="x", grade="C", evidence=[Evidence("press", "https://e.com", "a long enough quote here")]),
        web_fact(grade="Z"),
        web_fact(status="maybe"),
        Fact(key="a.b", value="x", grade="C", evidence=[]),
        web_fact(confidence=1.5),
        web_fact(value=None),
    ):
        with pytest.raises(LedgerError):
            bad.validate()


def test_grade_a_may_cite_a_registry_reference_and_unverified_inference_cannot_be_confident():
    Fact(key="ownership.ultimate_owner", value="IDEX CORP", grade="A", confidence=0.95,
         evidence=[Evidence(source_type="registry", ref="AIDA Raw Exports: controlling_shareholders")]).validate()
    with pytest.raises(LedgerError):
        Fact(key="ownership.ultimate_owner", value="IDEX", grade="A", evidence=[Evidence(source_type="registry")]).validate()
    with pytest.raises(LedgerError):
        web_fact(grade="D", confidence=0.9).validate()
    web_fact(grade="D", confidence=0.4).validate()


def test_quote_grounding_rejects_paraphrases_and_survives_whitespace_and_case():
    page = "Mott   Corporation\nacquired ASCO Filtri,\nits longtime partner in Europe."
    assert quote_in_text("mott corporation acquired asco filtri", page)
    assert not quote_in_text("Mott bought ASCO Filtri outright", page)
    assert not quote_in_text("short", "short")


def test_same_fact_found_twice_merges_evidence_and_keeps_the_stronger_grade():
    led = Ledger("c1")
    first = led.add(web_fact(grade="C", confidence=0.6))
    again = web_fact(grade="B", confidence=0.8)
    again.evidence = [Evidence(source_type="company_site", url="https://asco.example/about", quote="part of the Mott group")]
    led.add(again)
    assert len(led.facts) == 1
    assert first.grade == "B" and first.confidence == 0.8 and len(first.evidence) == 2
    led.add(web_fact(grade="C", confidence=0.6))   # identical evidence again: no duplicate
    assert len(first.evidence) == 2


def test_add_many_reports_refusals_instead_of_raising():
    led = Ledger()
    errors = led.add_many([web_fact(), web_fact(key="ownership.other", quote="x")])
    assert len(led.facts) == 1 and len(errors) == 1


def test_conflicts_only_for_single_valued_keys():
    led = Ledger()
    for v, q in (("66", "66 employees at Binasco"), ("74", "74 addetti nel 2024")):
        led.add(Fact(key="identity.employees", value=v, grade="C",
                     evidence=[Evidence(source_type="directory", url=f"https://d.example/{v}", quote=q + " in the company")]))
    for v in ("France", "Mexico"):   # many-valued: not a conflict
        led.add(Fact(key="locations.branch_country", value=v, grade="B",
                     evidence=[Evidence(source_type="company_site", url="https://asco.example", quote=f"a branch in {v} since 2005")]))
    conflicts = led.conflicts()
    assert [c["key"] for c in conflicts] == ["identity.employees"]
    assert {v["value"] for v in conflicts[0]["values"]} == {"66", "74"}


def test_staleness_and_roundtrip():
    led = Ledger("c1")
    led.add(web_fact())
    assert not led.is_stale("ownership.", 30)
    assert led.is_stale("people.", 30)   # nothing there: stale, i.e. needs a run
    later = datetime.now(timezone.utc) + timedelta(days=40)
    assert led.is_stale("ownership.", 30, now=later)
    back = Ledger.from_dict(led.to_dict())
    assert set(back.facts) == set(led.facts) and back.company_id == "c1"
    assert fact_id("a.b", " X  y ") == fact_id("a.b", "x y")


def test_ledger_survives_the_database_round_trip():
    from database import get_db_session, init_db
    from intel.ledger import load_ledger, save_ledger
    from models import Company
    init_db()
    db = get_db_session()
    try:
        c = Company(legal_name="Ledger Test S.r.l.", registration_number="LT" + uuid.uuid4().hex[:10], country="Italy")
        db.add(c)
        db.commit()
        led = Ledger(c.id)
        led.add(web_fact())
        save_ledger(db, c.id, led)
        assert len(load_ledger(db, c.id).facts) == 1
        led2 = load_ledger(db, c.id)
        led2.add(web_fact(key="ownership.other", value="IDEX", quote="IDEX bought Mott in 2024"))
        save_ledger(db, c.id, led2)   # upsert, not a second row
        assert len(load_ledger(db, c.id).facts) == 2
        from models import RawImportRecord
        assert db.query(RawImportRecord).filter_by(company_id=c.id).count() == 1
        db.query(RawImportRecord).filter_by(company_id=c.id).delete()
        db.delete(c)
        db.commit()
    finally:
        db.close()


# ---- the collector contract -----------------------------------------------------------------------

def make_collector(**kw):
    base = dict(name="demo", ticket="I99", produces=("ownership.",), run=lambda ctx: CollectorResult())
    base.update(kw)
    return Collector(**base)


def test_collector_validation():
    make_collector().validate()
    for bad in (make_collector(name="Demo Name"), make_collector(ticket=""), make_collector(produces=()),
                make_collector(produces=("nonsense.x",)), make_collector(requires=("teleport",))):
        with pytest.raises(ValueError):
            bad.validate()


def test_a_collector_may_only_emit_under_its_declared_prefixes():
    c = make_collector()
    res = CollectorResult(facts=[web_fact(), web_fact(key="people.manager", value="X", quote="X is the general manager")])
    problems = check_result(c, res)
    assert len(problems) == 1 and "people.manager" in problems[0]


def test_discover_finds_the_reference_collector_and_rejects_duplicates(monkeypatch):
    found = discover()
    assert "site-headers" in found and found["site-headers"].ticket == "I02"
    dup = make_collector(name="site-headers")
    fake_mod = type("M", (), {"COLLECTOR": dup})
    real_import = coll_mod.importlib.import_module
    monkeypatch.setattr(coll_mod.importlib, "import_module",
                        lambda name, *a, **k: fake_mod if name.endswith(".zzz_dup") else real_import(name, *a, **k))
    monkeypatch.setattr(coll_mod.pkgutil, "iter_modules",
                        lambda path: [type("I", (), {"name": "site_headers"}), type("I", (), {"name": "zzz_dup"})])
    with pytest.raises(ValueError, match="two collectors"):
        discover()


# ---- the reference collector ----------------------------------------------------------------------

def ctx_with(page, website="www.asco.example"):
    return CollectorContext(company={"website_url": website}, ledger=Ledger(), budget=Budget(),
                            fetch=lambda url: page)


def test_site_headers_emits_grounded_facts_and_states_what_it_checked():
    page = Page(url="https://www.asco.example/", ok=True, status=200,
                headers={"Server": "Microsoft-IIS/10.0", "X-Powered-By": "ASP.NET", "Content-Type": "text/html"})
    res = site_headers.run(ctx_with(page))
    by_key = {f.key: f for f in res.facts}
    assert by_key["digital.web_server"].value == "Microsoft-IIS/10.0"
    assert by_key["digital.web_framework"].value == "ASP.NET"
    assert by_key["digital.web_server"].evidence[0].quote == "Server: Microsoft-IIS/10.0"
    assert "digital.cms" in res.checked_absent
    led = Ledger()
    assert led.add_many(res.facts) == [] and not check_result(site_headers.COLLECTOR, res)


def test_site_headers_records_problems_instead_of_inventing_facts():
    assert site_headers.run(ctx_with(Page(url="u", ok=False, note="timeout"))).facts == []
    assert "no website_url" in site_headers.run(ctx_with(None, website="")).errors[0]


# ---- the fetcher's pure parts ---------------------------------------------------------------------

def test_html_to_text_drops_scripts_and_returns_title_and_links():
    title, text, hrefs = html_to_text(
        "<html><head><title> ASCO </title><script>var x=1</script></head>"
        "<body><h1>Filtri</h1><a href='/Home/Company'>Company</a><style>p{}</style></body></html>")
    assert title == "ASCO" and "Filtri" in text and "var x" not in text and hrefs == ["/Home/Company"]


def test_normalise_url():
    assert normalise_url("www.ascofiltri.com") == "https://www.ascofiltri.com"
    assert normalise_url("https://a.com/x#frag") == "https://a.com/x"
    assert normalise_url("") == ""
