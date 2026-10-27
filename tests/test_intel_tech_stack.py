"""
Offline tests for the technology and digital footprint collectors (ticket I18): `site-headers` (extended) and
`tech-stack`. Everything runs on recorded, synthetic pages: no network, and no real company is named.

The false-positive review on real sites (documented in docs/intel/results/I18.md) is what the signature set was
tuned against; these tests pin the behaviour so a later edit cannot quietly widen a pattern.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

import pytest

from intel.collector import Budget, CollectorContext, CollectorResult, check_result, discover
from intel.collectors import site_headers, tech_stack
from intel.collectors.site_headers import FactSink, fetch_homepage, outdated_server_software, tls_probe
from intel.collectors.tech_stack import (
    detect_technologies, find_placeholders, find_social_accounts, find_stale_copyright, find_stuck_counters,
    mixed_languages, outdated_from_detections, pick_internal_links, registrable_domain,
)
from intel.fetch import Page, PoliteFetcher
from intel.ledger import Ledger, quote_in_text

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
COMPANY = {"id": "c1", "legal_name": "ACME MACCHINE S.P.A.", "vat": "01234567890", "city": "Bergamo",
           "website_url": "www.acme-macchine.example"}
HOME_URL = "https://www.acme-macchine.example/"


def page(url: str, raw: str = "", text: str = "", headers: Optional[Dict[str, str]] = None, links: Optional[List[str]] = None,
         ok: bool = True, status: int = 200, note: str = "") -> Page:
    return Page(url=url, ok=ok, status=status, text=text, raw=raw, headers=headers or {}, links=links or [], note=note,
                content_hash="h" + str(abs(hash(url)) % 10_000))


class Recorded:
    """A recorded-page fetcher: a dict of url -> Page, 404 for anything else. No attribute `network_probes`, so a
    collector never makes a direct network probe (TLS) against it."""

    def __init__(self, pages: Dict[str, Page]) -> None:
        self.pages, self.asked = pages, []

    def __call__(self, url: str) -> Page:
        self.asked.append(url)
        hit = self.pages.get(url) or self.pages.get(url + "/") or self.pages.get(url.rstrip("/"))
        return hit or page(url, ok=False, status=404, note="HTTP 404")


def ctx_for(fetch, company=None) -> CollectorContext:
    return CollectorContext(company=company or dict(COMPANY), ledger=Ledger("c1"), budget=Budget(), fetch=fetch)


# ---- a recorded site ---------------------------------------------------------------------------------------

HOME_RAW = """<!doctype html><html lang="it"><head><title>ACME MACCHINE S.P.A.</title>
<meta name="generator" content="WordPress 6.4.2" /><meta name="author" content="Studio Pixel">
<link rel="stylesheet" href="/wp-content/themes/acme/style.css">
<link rel="stylesheet" href="/css/demos/demo-industry.css">
<link rel="alternate" hreflang="en" href="https://www.acme-macchine.example/en/">
<script src="/wp-includes/js/jquery/jquery-1.12.4.min.js"></script>
<script src="https://www.googletagmanager.com/gtm.js?id=GTM-ABC123"></script>
<script src="//js.hs-scripts.com/123456.js"></script>
<script src="https://cdn.iubenda.com/cs/iubenda_cs.js"></script></head>
<body><nav><a href="/chi-siamo">Chi siamo</a> <a href="/prodotti">Prodotti</a> <a href="/vecchia-pagina">Old page</a>
<a href="/area-riservata">Area riservata</a> <a href="/configuratore">Configuratore</a>
<a href="https://www.linkedin.com/company/acme-macchine">in</a> <a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a>
<a href="/downloads/catalogo.pdf">Catalogo</a> <a href="https://elsewhere.example/page">Elsewhere</a></nav>
<footer>(c) 2019 ACME MACCHINE S.P.A. - P.IVA 01234567890 - Bergamo</footer></body></html>"""
HOME_TEXT = "Chi siamo Prodotti Old page Area riservata Configuratore\n(c) 2019 ACME MACCHINE S.P.A. - P.IVA 01234567890 - Bergamo"
HOME_HEADERS = {"Server": "Apache/2.2.15 (CentOS)", "X-Powered-By": "PHP/5.6.40", "Set-Cookie": "PHPSESSID=abc123; path=/",
                "Content-Type": "text/html", "Strict-Transport-Security": "max-age=31536000"}
ABOUT_RAW = """<html><body><h1>Chi siamo</h1><img src="/img/a.jpg" alt="John Doe Team Member Image">
<div class="counters"><strong data-to="250">0</strong> <strong data-to="60">0</strong></div></body></html>"""
RDAP_RAW = ('{"objectClassName":"domain","events":[{"eventAction":"registration","eventDate":"2004-03-02T10:00:00Z"},'
            '{"eventAction":"expiration","eventDate":"2027-03-02T10:00:00Z"}]}')
CDX_RAW = '[["timestamp"],\n["20030801224322"]]'


def site(extra: Optional[Dict[str, Page]] = None) -> Recorded:
    pages = {
        HOME_URL: page(HOME_URL, HOME_RAW, HOME_TEXT, HOME_HEADERS, links=[
            "https://www.acme-macchine.example/chi-siamo", "https://www.acme-macchine.example/prodotti",
            "https://www.acme-macchine.example/vecchia-pagina", "https://www.acme-macchine.example/area-riservata",
            "https://www.acme-macchine.example/downloads/catalogo.pdf", "https://elsewhere.example/page"]),
        "https://www.acme-macchine.example/chi-siamo": page("https://www.acme-macchine.example/chi-siamo", ABOUT_RAW,
                                                           "Chi siamo", {}),
        "https://www.acme-macchine.example/prodotti": page("https://www.acme-macchine.example/prodotti",
                                                          "<html><body>Prodotti</body></html>", "Prodotti"),
        "https://www.acme-macchine.example/area-riservata": page("https://www.acme-macchine.example/area-riservata",
                                                                "<html><body>Login</body></html>", "Login"),
        "https://rdap.org/domain/acme-macchine.example": page("https://rdap.test/domain/acme-macchine.example", RDAP_RAW, "",
                                                             {"Content-Type": "application/rdap+json"}),
        "https://web.archive.org/cdx/search/cdx?url=acme-macchine.example&output=json&limit=1&fl=timestamp&filter=statuscode:200":
            page("https://web.archive.org/cdx/search/cdx?url=acme-macchine.example&output=json&limit=1&fl=timestamp&filter=statuscode:200",
                 CDX_RAW),
    }
    pages.update(extra or {})
    return Recorded(pages)


def facts_by_key(result) -> Dict[str, list]:
    out: Dict[str, list] = {}
    for f in result.facts:
        out.setdefault(f.key, []).append(f)
    return out


# ---- signatures --------------------------------------------------------------------------------------------

def values(dets, key):
    return sorted(d.value for d in dets if d.key == key)


def test_detects_cms_with_version_and_versioned_libraries():
    dets = detect_technologies(HOME_RAW)
    assert values(dets, "cms") == ["WordPress 6.4.2"]
    assert values(dets, "js_framework") == ["jQuery 1.12.4"]          # the bare "jQuery" is superseded by the versioned one
    assert values(dets, "tag_manager") == ["Google Tag Manager GTM-ABC123"]
    assert values(dets, "marketing_automation") == ["HubSpot"]
    assert values(dets, "cookie_consent") == ["iubenda"]
    for d in dets:                                                    # every snippet is verbatim markup
        assert quote_in_text(d.quote, HOME_RAW), d


def test_detects_industrial_and_portal_technologies():
    raw = ('<a href="https://tacton.com/x">cfg</a><iframe src="https://www.partcommunity.com/3d-cad-models/acme"></iframe>'
           '<script src="https://static.zdassets.com/ekr/snippet.js"></script><script>var prestashop = {};</script>'
           '<script src="https://acme.azure-devices.net/x.js"></script><script src="/js/model-viewer.min.js"></script>')
    dets = detect_technologies(raw)
    assert values(dets, "configurator") == ["Tacton CPQ"]
    assert values(dets, "cad_portal") == ["CADENAS PARTcommunity / 3Dfindit"]
    assert values(dets, "live_chat") == ["Zendesk Chat"]
    assert values(dets, "ecommerce_platform") == ["PrestaShop"]
    assert values(dets, "iot_platform") == ["Azure IoT Hub"]
    assert values(dets, "product_3d_viewer") == ["model-viewer (3D)"]


def test_signatures_do_not_fire_on_lookalike_words():
    raw = ('<script src="/js/revue.js"></script><script src="/js/jquery.fancybox.js"></script>'
           '<script src="/assets/mythree.js"></script><p>We moved our hub; the vue of the valley. Wix is a word.</p>'
           '<script src="/js/bootstrap-icons.css"></script>')
    assert detect_technologies(raw) == []


def test_header_signatures_read_the_headers_not_the_markup():
    dets = detect_technologies("<html></html>", "Server: Apache\nX-Wix-Request-Id: abc")
    assert values(dets, "cms") == ["Wix"]


def test_a_universal_analytics_tag_is_outdated_only_when_nothing_modern_can_carry_data():
    ua = '<script>ga("create", "x"); var id = "UA-1234567-1"; </script>'
    dets = detect_technologies(ua)
    assert any(v.startswith("Google Universal Analytics") for v in values(dets, "analytics"))
    out = outdated_from_detections(dets, ua)
    assert out and "Universal Analytics stopped" in out[0][0]
    with_gtm = ua + '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-XYZ9"></script>'
    assert outdated_from_detections(detect_technologies(with_gtm), with_gtm) == []


def test_outdated_front_end_software_is_flagged_with_a_reason():
    raw = ('<script src="/js/jquery-1.12.4.min.js"></script><link href="/css/bootstrap-3.3.7.min.css">'
           '<script src="/js/angular.min.js"></script><object data="intro.swf"></object>')
    out = outdated_from_detections(detect_technologies(raw), raw)
    text = " | ".join(v for v, _, _ in out)
    assert "jQuery 1.12.4" in text and "3.5.0" in text
    assert "Bootstrap 3.3.7" in text and "July 2019" in text
    assert "AngularJS" in text and "Flash" in text
    modern = '<script src="/js/jquery-3.7.1.min.js"></script><link href="/css/bootstrap-5.3.2.min.css">'
    assert outdated_from_detections(detect_technologies(modern), modern) == []


def test_server_software_past_vendor_support_is_flagged_by_the_headers_it_announces():
    hits = outdated_server_software({"Server": "Apache/2.2.15 (CentOS) OpenSSL/1.0.2k-fips", "X-Powered-By": "PHP/5.6.40"}, NOW)
    flat = " | ".join(f"{h}: {s} ended {e}" for h, s, e in hits)
    assert "PHP/5.6.40" in flat and "Apache/2.2.15" in flat and "OpenSSL/1.0.2 ended 2019-12-31" in flat
    assert outdated_server_software({"Server": "nginx/1.25.3", "X-Powered-By": "PHP/8.3.1"}, NOW) == []
    assert outdated_server_software({"Server": "Microsoft-IIS/10.0"}, NOW) == []
    assert outdated_server_software({"Server": "Microsoft-IIS/7.5"}, NOW)
    # a branch whose end of support is still in the future is not flagged
    assert outdated_server_software({"X-Powered-By": "PHP/8.1.2"}, datetime(2025, 6, 1, tzinfo=timezone.utc)) == []


# ---- site quality ------------------------------------------------------------------------------------------

def test_placeholder_text_is_found_in_alt_attributes_and_visible_text():
    hits = find_placeholders(ABOUT_RAW)
    assert [(w, where) for w, where, _ in hits] == [("John Doe", "alt or title attribute")]
    assert quote_in_text(hits[0][2], ABOUT_RAW)
    lorem = "<p>Lorem ipsum dolor sit amet</p>"
    assert find_placeholders(lorem)[0][:2] == ("lorem ipsum", "visible text")
    assert find_placeholders("<h2><a href='/hello-world/'>Hello world!</a></h2>")[0][0].startswith("default WordPress post")


def test_placeholders_inside_scripts_comments_and_form_hints_are_ignored():
    raw = ('<script type="text/template">Lorem ipsum dolor</script><!-- John Doe --><style>.x{content:"lorem ipsum"}</style>'
           '<input placeholder="your name here"><textarea placeholder="John Doe"></textarea><p>Real content.</p>')
    assert find_placeholders(raw) == []


def test_default_server_pages_and_example_addresses_count_as_placeholders():
    assert find_placeholders("<title>IIS Windows Server</title>")[0][0] == "default IIS server page"
    assert find_placeholders("<h1>Welcome to nginx!</h1>")[0][0] == "default nginx server page"
    assert find_placeholders("<p>mail: info@example.com</p>")[0][0] == "example e-mail address"


def test_stuck_counters_need_two_zeros_with_targets_and_no_counter_script():
    stuck = find_stuck_counters(ABOUT_RAW)
    assert stuck and stuck[0] == ["250", "60"] and quote_in_text(stuck[1], ABOUT_RAW)
    assert find_stuck_counters('<strong data-to="250">0</strong>') is None                    # one counter alone
    assert find_stuck_counters(ABOUT_RAW + '<script src="/js/jquery.counterup.min.js"></script>') is None
    assert find_stuck_counters(ABOUT_RAW.replace('<strong data-to="250">0</strong>', '<strong data-to="250">250</strong>')) is None
    assert find_stuck_counters(ABOUT_RAW.replace("<strong data-to", "<strong data-plugin-counter data-to")) is None


def test_stale_copyright_year_is_reported_with_a_quotable_context():
    year, quote = find_stale_copyright("Home\n(c) 2019 ACME MACCHINE S.P.A. - all rights reserved", "<html></html>", NOW)
    assert year == 2019 and quote_in_text(quote, "Home\n(c) 2019 ACME MACCHINE S.P.A. - all rights reserved")
    assert len(quote) >= 12
    assert find_stale_copyright("© 2026 ACME", "", NOW) is None
    assert find_stale_copyright("© 2025 ACME", "", NOW) is None
    assert find_stale_copyright("© 2012-2019 ACME", "", NOW)[0] == 2019                        # a range: the newest year counts
    assert find_stale_copyright("© 2019 ACME", "<script>document.write(new Date().getFullYear())</script>", NOW) is None
    assert find_stale_copyright("© 1999- ACME S.p.A.", "", NOW) is None          # open range: the end year comes from a script
    assert find_stale_copyright("© 1999 - ACME S.p.A.", "", NOW) is None


def test_mixed_languages_needs_enough_words_and_a_real_share():
    italian = "Il nostro gruppo produce macchine per la lavorazione del legno e delle plastiche, sono pensate per tutti i clienti. " * 8
    english = "Our company builds machines for the processing of wood and plastics, which are designed for all of our customers. " * 5
    assert mixed_languages(italian) is None
    result = mixed_languages(italian + english)
    assert result and {result[0], result[1]} == {"it", "en"}
    assert mixed_languages("Il nostro gruppo. " * 3) is None                                   # too short to judge


# ---- social accounts and links -----------------------------------------------------------------------------

def test_social_accounts_are_inventoried_and_share_buttons_ignored():
    raw = ('<a href="https://it.linkedin.com/company/acme-macchine" target="_blank">in</a>'
           '<a href="https://www.facebook.com/AcmeMacchine">fb</a><a href="https://www.facebook.com/sharer/sharer.php?u=1">s</a>'
           '<a href="https://www.facebook.com/pages">pages</a><a href="https://twitter.com/intent/tweet?x=1">t</a>'
           '<meta property="article:publisher" content="https://www.facebook.com/pages/category/Industrial-Company/Acme-123456/">'
           '<a href="https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv">yt</a><a href="https://www.instagram.com/p/xyz/">post</a>'
           '<a href="https://www.linkedin.com/shareArticle?mini=true">share</a>')
    found = {(n, u) for n, u, _ in find_social_accounts(raw)}
    assert ("linkedin", "https://it.linkedin.com/company/acme-macchine") in found
    assert ("facebook", "https://www.facebook.com/AcmeMacchine") in found
    assert ("facebook", "https://www.facebook.com/pages/category/Industrial-Company/Acme-123456") in found
    assert ("youtube", "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv") in found
    assert len(found) == 4                          # no share link, no bare /pages, no single post
    for _, _, snip in find_social_accounts(raw):
        assert quote_in_text(snip, raw)


def test_internal_link_picker_takes_the_homes_own_informative_links_only():
    home = page("https://www.acme-macchine.example/", links=[
        "https://www.acme-macchine.example/", "https://www.acme-macchine.example/blog/post-1",
        "https://www.acme-macchine.example/contatti", "https://www.acme-macchine.example/chi-siamo#team",
        "https://www.acme-macchine.example/catalog.pdf", "https://other.example/chi-siamo",
        "https://acme-macchine.example/prodotti", "https://www.acme-macchine.example/contatti"])
    picked = pick_internal_links(home, 3)
    assert picked == ["https://www.acme-macchine.example/contatti", "https://www.acme-macchine.example/chi-siamo",
                      "https://acme-macchine.example/prodotti"]
    assert pick_internal_links(home, 0) == []


def test_registrable_domain_handles_second_level_country_suffixes():
    assert registrable_domain("www.example.it") == "example.it"
    assert registrable_domain("shop.eu.example.com") == "example.com"
    assert registrable_domain("www.example.co.uk") == "example.co.uk"
    assert registrable_domain("example.com.tr") == "example.com.tr"


# ---- the tech-stack collector end to end on a recorded site ------------------------------------------------

@pytest.fixture()
def run_site():
    fetcher = site()
    ctx = ctx_for(fetcher)
    result = tech_stack.run(ctx, now=NOW)
    return fetcher, ctx, result


def test_collector_emits_a_grounded_checked_ledger_of_facts(run_site):
    fetcher, ctx, result = run_site
    assert check_result(tech_stack.COLLECTOR, result) == []
    ledger = Ledger("c1")
    assert ledger.add_many(result.facts) == []                      # every fact obeys the ledger contract
    by = facts_by_key(result)
    assert [f.value for f in by["digital.cms"]] == ["WordPress 6.4.2"]
    assert [f.value for f in by["digital.tag_manager"]] == ["Google Tag Manager GTM-ABC123"]
    assert {f.value for f in by["digital.site_language"]} == {"it", "en"}
    assert [f.value for f in by["digital.social_linkedin"]] == ["https://www.linkedin.com/company/acme-macchine"]
    assert "digital.social_facebook" not in by                      # the only facebook link is a share button
    assert by["digital.web_agency"][0].value == "Studio Pixel"
    for f in result.facts:
        assert f.key.startswith("digital.") and f.collector.startswith("I18:tech-stack")
        assert f.evidence and all(e.url and e.quote for e in f.evidence)


def test_every_fact_went_through_the_verification_gate(monkeypatch):
    seen = []
    real = site_headers.verify

    def spy(fact, page_text, company, ledger=None, llm=None):
        verdict = real(fact, page_text, company, ledger, llm)
        seen.append((fact.key, fact.value, verdict.decision))
        return verdict

    monkeypatch.setattr(site_headers, "verify", spy)
    result = tech_stack.run(ctx_for(site()), now=NOW)
    assert result.facts and len(seen) == len(result.facts)               # one gate call per emitted fact, none skipped
    emitted = {(f.key, f.value) for f in result.facts}
    assert {(k, v) for k, v, _ in seen} == emitted
    assert all(decision != "rejected" for _, _, decision in seen)
    for f in result.facts:
        if f.key in ("digital.cms", "digital.tag_manager", "digital.social_linkedin"):
            assert f.verified is True                                    # name + own domain on the page


def test_a_rejected_verdict_drops_the_fact_and_says_why(monkeypatch):
    from intel.verify import Verdict

    def reject(fact, page_text, company, ledger=None, llm=None):
        return Verdict(verified=False, confidence=0.05, decision="rejected", reasons=["Collision detected: namesake"])

    monkeypatch.setattr(site_headers, "verify", reject)
    result = tech_stack.run(ctx_for(site()), now=NOW)
    assert result.facts == []
    assert any("rejected by the verification gate: Collision detected" in e for e in result.errors)


def test_quality_issues_state_where_and_quote_the_markup(run_site):
    _, _, result = run_site
    issues = [f.value for f in result.facts if f.key == "digital.quality_issue"]
    joined = " | ".join(issues)
    assert 'placeholder or template text on /chi-siamo: "John Doe" (alt or title attribute)' in joined
    assert "statistic counters ship as 0 on /chi-siamo" in joined
    assert "footer copyright year 2019" in joined
    assert "template demo files still linked on the homepage: /css/demos/demo-industry.css" in joined
    assert "broken internal link /vecchia-pagina (HTTP 404) linked from /" in joined
    counters = next(f for f in result.facts if f.value.startswith("statistic counters"))
    assert counters.verified is False and counters.confidence <= 0.5      # an estimate, not a verdict


def test_outdated_portal_and_configurator_facts_come_from_links_of_the_company_site(run_site):
    _, _, result = run_site
    by = facts_by_key(result)
    assert any("jQuery 1.12.4" in f.value for f in by["digital.outdated_software"])
    assert by["digital.customer_portal"][0].value == "reserved area or customer portal: /area-riservata"
    assert by["digital.configurator"][0].value == "product configurator link: /configuratore"


def test_domain_facts_carry_a_second_evidence_naming_the_company(run_site):
    _, _, result = run_site
    by = facts_by_key(result)
    reg = by["digital.domain_registered_on"][0]
    assert reg.value == "2004-03-02" and reg.grade == "A"
    assert len(reg.evidence) == 2 and "ACME MACCHINE" in reg.evidence[1].quote
    assert reg.evidence[1].url == HOME_URL
    first = by["digital.wayback_first_seen"][0]
    assert first.value == "2003-08-01" and first.grade == "C" and len(first.evidence) == 2
    assert reg.verified and first.verified


def test_domain_facts_are_withheld_when_the_homepage_does_not_name_the_company():
    other = page(HOME_URL, HOME_RAW.replace("ACME MACCHINE S.P.A.", "Someone Else").replace("01234567890", "99999999999"),
                 "Chi siamo Prodotti Someone Else - Torino", HOME_HEADERS, links=[])
    result = tech_stack.run(ctx_for(site({HOME_URL: other})), now=NOW)
    keys = {f.key for f in result.facts}
    assert "digital.domain_registered_on" not in keys and "digital.wayback_first_seen" not in keys
    assert any("domain facts withheld" in e for e in result.errors)
    assert all(f.verified is False for f in result.facts if f.key == "digital.cms")   # the gate: the site names someone else


def test_a_redirect_to_another_domain_is_a_fact_and_domain_facts_follow_the_configured_domain():
    moved = page("https://www.group.example/acme", HOME_RAW.replace("(c) 2019", "(c) 2026"), HOME_TEXT.replace("2019", "2026"), HOME_HEADERS)
    fetcher = site({HOME_URL: moved})
    result = tech_stack.run(ctx_for(fetcher), now=NOW)
    by = facts_by_key(result)
    assert by["digital.site_redirect"][0].value == "acme-macchine.example -> group.example"
    assert by["digital.domain_registered_on"][0].value == "2004-03-02"
    assert any("rdap.org/domain/acme-macchine.example" in u for u in fetcher.asked)


def test_rdap_404_is_unknown_not_absent_and_nothing_is_invented():
    pages = {"https://rdap.org/domain/acme-macchine.example": page("https://rdap.org/domain/acme-macchine.example", ok=False,
                                                                  status=404, note="HTTP 404")}
    result = tech_stack.run(ctx_for(site(pages)), now=NOW)
    assert "digital.domain_registered_on" not in {f.key for f in result.facts}
    assert "digital.domain_registered_on" not in result.checked_absent
    assert any("no RDAP record" in e for e in result.errors)


def test_checked_absent_lists_only_what_was_actually_looked_for(run_site):
    _, _, result = run_site
    assert "digital.cms" not in result.checked_absent and "digital.ecommerce_platform" in result.checked_absent
    assert "digital.social_instagram" in result.checked_absent and "digital.analytics" in result.checked_absent


def test_unreadable_homepage_or_missing_markup_is_reported_not_invented():
    gone = Recorded({HOME_URL: page(HOME_URL, ok=False, status=500, note="HTTP 500"),
                     "http://www.acme-macchine.example/": page("http://www.acme-macchine.example/", ok=False, status=500, note="HTTP 500")})
    res = tech_stack.run(ctx_for(gone), now=NOW)
    assert res.facts == [] and res.checked_absent == [] and res.errors
    bare = Recorded({HOME_URL: page(HOME_URL, raw="", text="Some text", headers={"Server": "x"})})
    res = tech_stack.run(ctx_for(bare), now=NOW)
    assert res.facts == [] and "Page.raw is empty" in res.errors[0]
    assert tech_stack.run(ctx_for(site(), company={"legal_name": "X"}), now=NOW).errors == ["company has no website_url"]


def test_the_collector_respects_the_page_budget():
    fetcher = site()
    ctx = ctx_for(fetcher)
    ctx.budget = Budget(max_pages=2)
    result = tech_stack.run(ctx, now=NOW)
    pages_read = [u for u in fetcher.asked if "acme-macchine.example/" in u and "rdap" not in u and "archive" not in u]
    assert len(pages_read) <= 2
    assert result.pages_fetched >= 1


def test_collector_is_registered_and_declares_its_prefix():
    found = discover()
    assert found["tech-stack"].ticket == "I18" and found["tech-stack"].produces == ("digital.",)
    assert found["site-headers"].produces == ("digital.",)


# ---- the fact sink: the quote rule and the gate -------------------------------------------------------------

def test_fact_sink_drops_a_quote_that_is_not_in_the_text_and_never_raises():
    ctx = ctx_for(Recorded({}))
    from intel.collector import CollectorResult
    result = CollectorResult()
    sink = FactSink(ctx, result, "T:test", anchor_text="ACME MACCHINE S.P.A. Bergamo")
    assert sink.add("digital.cms", "WordPress", quote="a paraphrase that is not there", source_text="some markup text here",
                    url=HOME_URL) is None
    assert sink.dropped == 1 and result.facts == [] and "quote not found" in result.errors[0]
    ok = sink.add("digital.cms", "WordPress", quote="generator WordPress 6.4", source_text="<meta generator WordPress 6.4>", url=HOME_URL)
    assert ok is not None and result.facts == [ok]
    assert sink.add("digital.cms", "WordPress", quote="generator WordPress 6.4", source_text="<meta generator WordPress 6.4>",
                    url=HOME_URL) is None                      # the same fact twice in one run is one fact


# ---- the extended site-headers collector ------------------------------------------------------------------

def header_page(headers: Dict[str, str], url: str = HOME_URL, text: str = HOME_TEXT) -> Page:
    return page(url, "<html></html>", text, headers)


def run_headers(headers, fetch=None, now=NOW):
    return site_headers.run(ctx_for(fetch or (lambda url: header_page(headers))), now=now)


def test_headers_give_server_framework_cdn_panel_and_outdated_software():
    res = run_headers({"Server": "Apache/2.2.15 (CentOS)", "X-Powered-By": "PHP/5.6.40, PleskLin", "CF-RAY": "8a1b2c3d-MXP",
                       "Set-Cookie": "PHPSESSID=abcd; path=/; HttpOnly"})
    by = facts_by_key(res)
    assert by["digital.web_server"][0].value == "Apache/2.2.15 (CentOS)"
    assert {f.value for f in by["digital.web_framework"]} == {"PHP/5.6.40", "PHP"}   # header and session cookie
    assert by["digital.hosting_panel"][0].value == "PleskLin"
    assert by["digital.cdn"][0].value == "Cloudflare" and by["digital.cdn"][0].evidence[0].quote.startswith("CF-RAY")
    outdated = {f.value.split(":")[0] for f in by["digital.outdated_software"]}
    assert {"PHP/5.6.40", "Apache/2.2.15"} <= outdated
    cookie = next(f for f in by["digital.web_framework"] if f.value == "PHP")
    assert "abcd" not in cookie.evidence[0].quote and "PHPSESSID=" in cookie.evidence[0].quote   # the name, never the value
    assert Ledger().add_many(res.facts) == [] and not check_result(site_headers.COLLECTOR, res)


def test_security_headers_summary_names_present_missing_and_a_permissive_csp():
    res = run_headers({"Server": "nginx", "Strict-Transport-Security": "max-age=31536000", "X-Frame-Options": "DENY",
                       "Content-Security-Policy": "default-src * 'unsafe-inline' 'unsafe-eval' data:;"})
    summary = facts_by_key(res)["digital.security_headers"][0]
    assert summary.value.startswith("3 of 6 present: HSTS, CSP, X-Frame-Options (CSP allows any script source")
    assert "missing: X-Content-Type-Options, Referrer-Policy, Permissions-Policy" in summary.value
    assert summary.evidence[0].quote.startswith("Strict-Transport-Security")           # a header the summary rests on
    assert facts_by_key(res)["digital.hsts"][0].value == "max-age=31536000"
    bare = facts_by_key(run_headers({"Server": "nginx"}))["digital.security_headers"][0]
    assert bare.value.startswith("0 of 6 present: none; missing: HSTS")


def test_a_strict_csp_is_not_called_permissive():
    assert site_headers.csp_is_permissive("default-src * 'unsafe-inline'")
    assert not site_headers.csp_is_permissive("default-src 'self'; script-src 'self' https://cdn.example")
    assert not site_headers.csp_is_permissive("default-src *")             # a wildcard alone, without inline scripts


def test_headers_checked_absent_when_nothing_is_announced():
    res = run_headers({"Content-Type": "text/html"})
    assert {"digital.web_server", "digital.web_framework", "digital.cms", "digital.cdn"} <= set(res.checked_absent)


def test_a_failed_https_falls_back_to_plain_http_and_records_why():
    http_page = header_page({"Server": "nginx"}, url="http://www.acme-macchine.example/")
    fetcher = Recorded({HOME_URL: page(HOME_URL, ok=False, status=None, note="SSLError: certificate has expired"),
                        "http://www.acme-macchine.example/": http_page})
    res = site_headers.run(ctx_for(fetcher), now=NOW)
    assert res.pages_fetched == 2
    assert facts_by_key(res)["digital.web_server"][0].value == "nginx"
    got, asked = fetch_homepage(ctx_for(fetcher), CollectorResult())
    assert got is http_page and "plain http only" in got.note and "certificate has expired" in got.note
    assert asked == "https://www.acme-macchine.example"


def test_a_certificate_error_in_the_fetch_note_becomes_a_tls_problem_fact():
    gone = Recorded({HOME_URL: page(HOME_URL, ok=False, status=None, note="SSLError: certificate has expired (_ssl.c:1000)"),
                     "http://www.acme-macchine.example/": page("http://www.acme-macchine.example/", ok=False, status=None,
                                                              note="ConnectionError: refused")})
    res = site_headers.run(ctx_for(gone), now=NOW)
    probs = [f for f in res.facts if f.key == "digital.tls_problem"]
    assert probs and "certificate has expired" in probs[0].value
    assert quote_in_text(probs[0].evidence[0].quote, "SSLError: certificate has expired (_ssl.c:1000)")


class Probing(Recorded):
    network_probes = True


def test_tls_probe_runs_only_for_a_fetcher_that_declares_network_probes(monkeypatch):
    calls: List[str] = []

    def fake_probe(host, **kw):
        calls.append(host)
        return {"ok": True, "version": "TLSv1.3", "issuer": "Let's Encrypt", "not_after": "2027-01-09"}

    monkeypatch.setattr(site_headers, "tls_probe", fake_probe)
    home = header_page({"Server": "nginx"})
    site_headers.run(ctx_for(Recorded({HOME_URL: home})), now=NOW)
    assert calls == []                                              # a recorded-page fetcher never triggers a probe
    res = site_headers.run(ctx_for(Probing({HOME_URL: home})), now=NOW)
    assert calls == ["www.acme-macchine.example"]
    cert = facts_by_key(res)["digital.tls_certificate"][0]
    assert cert.value == "TLSv1.3 certificate issued by Let's Encrypt, valid until 2027-01-09"
    assert "digital.tls_problem" not in facts_by_key(res)


def test_tls_certificate_close_to_expiry_or_untrusted_is_a_problem(monkeypatch):
    home = header_page({"Server": "nginx"})
    monkeypatch.setattr(site_headers, "tls_probe", lambda host, **kw: {"ok": True, "version": "TLSv1.2", "issuer": "R3",
                                                                       "not_after": "2026-11-03"})
    res = site_headers.run(ctx_for(Probing({HOME_URL: home})), now=NOW)
    assert facts_by_key(res)["digital.tls_problem"][0].value == "certificate valid until 2026-11-03 (about to expire)"
    monkeypatch.setattr(site_headers, "tls_probe", lambda host, **kw: {"ok": False, "error": "certificate verification failed: self-signed certificate"})
    res = site_headers.run(ctx_for(Probing({HOME_URL: home})), now=NOW)
    assert facts_by_key(res)["digital.tls_problem"][0].value == "certificate verification failed: self-signed certificate"
    monkeypatch.setattr(site_headers, "tls_probe", lambda host, **kw: {"ok": False, "error": "connection failed: TimeoutError", "unreachable": True})
    res = site_headers.run(ctx_for(Probing({HOME_URL: home})), now=NOW)
    assert "digital.tls_problem" not in facts_by_key(res)           # unreachable is not a certificate finding


def test_tls_probe_never_raises_on_a_dead_host():
    out = tls_probe("host.invalid", timeout=0.5)
    assert out["ok"] is False and out.get("unreachable")


# ---- the fetcher change: Page.raw ----------------------------------------------------------------------------

def _session(body: bytes, ctype: str, url: str = "https://example.test/x"):
    class Raw:
        def read(self, *a, **k):
            return body

    class Response:
        status_code = 200
        encoding = "utf-8"
        apparent_encoding = "utf-8"
        headers = {"Content-Type": ctype}
        raw = Raw()

    Response.url = url

    class Session:
        def __init__(self):
            self.headers = {}

        def get(self, u, **kw):
            return Response()

    return Session()


def test_fetcher_keeps_the_markup_and_the_api_body_in_page_raw():
    html = b'<html><head><script src="/a.js"></script></head><body><img alt="John Doe"><p>Hello</p></body></html>'
    page_ = PoliteFetcher(session=_session(html, "text/html"), respect_robots=False, delay=0).fetch("https://example.test/x")
    assert 'alt="John Doe"' in page_.raw and "John Doe" not in page_.text and "Hello" in page_.text
    api = PoliteFetcher(session=_session(b'{"a": 1}', "application/rdap+json"), respect_robots=False, delay=0).fetch("https://example.test/x")
    assert api.ok and api.raw == '{"a": 1}' and api.text == ""
    old = Page(url="u", ok=True)
    assert old.raw == ""                                             # additive: existing constructors keep working


def test_fetcher_declares_it_talks_to_the_network_and_test_doubles_do_not():
    assert PoliteFetcher.network_probes is True
    assert not getattr(Recorded({}), "network_probes", False)
    assert fetch_homepage is not None
