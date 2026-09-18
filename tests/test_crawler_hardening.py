"""
Tests for the 2026-09-18 scraper hardening pass: no fabricated placeholders from
the legacy web scrapers, honest handling of unknown Wayback data, fuzzy directory
matches scoring nothing, careers-page discovery from the site's own links, and a
subprocess timeout that actually kills the crawler's process tree.

Same rules as tests/test_crawler_scrapers.py: no real Node crawler is spawned
against a website and the configured DATABASE_URL is never touched.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import requests

from adapters import google_news
from scrapers import (
    digital_maturity_crawler, directory_listing_crawler, job_postings_crawler,
    management_diversity, node_crawler_base, wappalyzer_local,
)


class _Resp:
    def __init__(self, code, url, text=""):
        self.status_code, self.url, self.text = code, url, text


# ---------------------------------------------------------------- no more placeholders

def test_legacy_web_scrapers_no_longer_fabricate_on_simulate():
    """wappalyzer / own-site / Google CSE used to hash the company name into a
    placeholder that scoring.py treated exactly like a verified value. An
    unreachable site now leaves the signal not_yet_checked."""

    class _C:
        legal_name = "Any Company S.R.L."
        website_url = "https://example.com"

    assert wappalyzer_local._simulate(_C())["signals"] == {}
    assert management_diversity._simulate(_C())["signals"] == {}
    assert google_news._simulate(_C())["signals"] == {}
    assert google_news._simulate_innovation_statements(_C())["signals"] == {}


# ---------------------------------------------------------------- digital maturity

def test_digital_maturity_unknown_archive_activity_is_not_scored_as_zero():
    base = {"has_ecommerce": True,
            "social_presence_links": [{"platform": "linkedin", "url": "x"}, {"platform": "instagram", "url": "y"}],
            "field_status": {"has_ecommerce": "value", "social_presence_links": "value"}}
    known = digital_maturity_crawler._derive_signals({**base, "snapshot_count_last_5_years": 3})
    unknown = digital_maturity_crawler._derive_signals({**base, "snapshot_count_last_5_years": None})
    assert known["online_market_presence"]["value"] == pytest.approx(4.0)    # 2 + 2 + 0, out of 5
    assert unknown["online_market_presence"]["value"] == pytest.approx(5.0)  # 4 of the 4 points measured
    assert unknown["online_market_presence"]["evidence"]["score_breakdown"]["archive_activity"] is None
    assert "unknown" in unknown["online_market_presence"]["summary"]


def test_digital_maturity_stable_history_is_a_floor_not_nothing():
    """fimer.it: three consecutive yearly captures within 0.2% of each other. That is
    evidence of NO redesign since the oldest capture and used to write nothing."""
    from datetime import datetime
    row = {
        "last_major_redesign_estimate": {"estimated_year": None, "comparisons": [
            {"from_year": 2022, "to_year": 2023, "node_count_change_ratio": 0.002, "layout_tags_changed": False, "substantial_change": False},
            {"from_year": 2023, "to_year": 2024, "node_count_change_ratio": 0.001, "layout_tags_changed": False, "substantial_change": False},
        ]},
        "homepage_url": "https://www.fimer.it/",
        "field_status": {"last_major_redesign_estimate": "value"},
    }
    sig = digital_maturity_crawler._derive_signals(row)["website_digital_maturity"]
    assert sig["value"] == pytest.approx(min(datetime.utcnow().year - 2022, 8))
    assert sig["status"] == "present" and "at least" in sig["summary"]
    assert sig["evidence"]["stable_since_year"] == 2022
    # no comparisons at all (every capture failed) still writes nothing
    assert digital_maturity_crawler._derive_signals({
        "last_major_redesign_estimate": {"estimated_year": None, "comparisons": []},
        "field_status": {"last_major_redesign_estimate": "not_found"}}) == {}


# ---------------------------------------------------------------- directory listing

def test_directory_fuzzy_match_is_not_a_confirmed_listing():
    """The crawler flags a fuzzy hit as appears_in_directory=true AND possible_match=true.
    Only a human can confirm it, so it scores 0 and stays in the evidence."""
    rows = [{"appears_in_directory": True, "possible_match": True, "status": "ok", "directory_name": "MECSPE",
             "listing_url": "https://m/brembomatic-pedrali", "directory_url": "https://m"}]
    sig = directory_listing_crawler._derive_signals(rows)["trade_fair_participation"]
    assert sig["value"] == 0.0 and sig["status"] == "absent"
    assert sig["evidence"]["found"][0]["url"] == "https://m/brembomatic-pedrali"
    assert "needs human verification" in sig["evidence"]["found"][0]["label"]


def test_directory_exact_match_is_still_a_hit():
    rows = [{"appears_in_directory": True, "possible_match": False, "status": "ok", "directory_name": "MECSPE",
             "listing_url": "https://m/cangini", "directory_url": "https://m"}]
    sig = directory_listing_crawler._derive_signals(rows)["trade_fair_participation"]
    assert sig["value"] == 1.0 and sig["status"] == "present"


# ---------------------------------------------------------------- review crawler

def test_review_crawler_refuses_an_unconfident_search_match():
    """The crawler's fallback is "the platform's top result, whatever it was" — the
    2026-09-18 run matched a warehouse listing (2 reviews) and, for other names,
    unrelated homonyms. Only its own confident matches are used, and the listing
    travels in the evidence so a human can check it."""
    from scrapers import review_crawler
    trend = {"last_12_months_avg": 4.0, "prior_12_months_avg": 4.5}
    unconfident = {"google": {"status": "ok", "matched_via_search": True, "rating_trend": trend,
                              "search_match": {"name": "POLO Motorrad Store", "url": "https://g/polo", "confident": False}}}
    assert review_crawler._derive_signals(unconfident) == {}
    confident = {"google": {"status": "ok", "matched_via_search": True, "rating_trend": trend, "review_count": 40,
                            "search_match": {"name": "Brembo S.p.A.", "url": "https://g/brembo", "confident": True}}}
    sig = review_crawler._derive_signals(confident)["product_quality_trend"]
    assert sig["evidence"]["matched_listing"] == "Brembo S.p.A."
    assert sig["evidence"]["matched_listing_url"] == "https://g/brembo"


def test_review_crawler_is_off_unless_a_mode_is_enabled(monkeypatch):
    """Both modes default off; with nothing enabled no Node process may be spawned."""
    from scrapers import review_crawler
    assert review_crawler.MODE_A_SOURCES == ["google"]          # Trustpilot: robots.txt Disallow: /
    monkeypatch.setattr(review_crawler, "REVIEW_CRAWLER_MODE_A_ENABLED", False)
    monkeypatch.setattr(review_crawler, "KUNUNU_CRAWLER_ENABLED", False)

    def boom(*a, **k):
        raise AssertionError("review-crawler must not be spawned with both modes off")
    monkeypatch.setattr(review_crawler, "run_ts_crawler", boom)
    calls = []

    def fake_run_adapter(db, company, source, phase, credentials_ok, fetch_live, simulate, **kw):
        calls.append(credentials_ok)
        return {"status": "success", "mode": "simulated", "signals": simulate(company)["signals"]}
    monkeypatch.setattr(review_crawler, "run_adapter", fake_run_adapter)
    out = review_crawler.sync_reviews(type("C", (), {"id": "c", "legal_name": "X", "website_url": None})(), None)
    assert calls == [False] and out["signals"] == {}


# ---------------------------------------------------------------- careers-page discovery

CAREERS_PAGE = "<html><title>Lavora con noi</title><body><h1>Posizioni aperte</h1></body></html>"
HOME_URL = "https://www.example.com/"


def _site(pages):
    """requests.get stand-in: pages maps a URL to (status, url_after_redirects, html);
    'HOME' is the homepage; anything else is a 404."""
    def fake_get(url, **kwargs):
        if url.rstrip("/") == HOME_URL.rstrip("/"):
            return _Resp(*pages["HOME"])
        if url in pages:
            return _Resp(*pages[url])
        return _Resp(404, url, "<html><title>404</title></html>")
    return fake_get


def test_discover_careers_page_follows_the_homepage_link_before_guessing_paths(monkeypatch):
    home = '<html><body><nav><a href="/company/career/">Career</a><a href="/products">Products</a></nav></body></html>'
    monkeypatch.setattr(job_postings_crawler.requests, "get", _site({
        "HOME": (200, HOME_URL, home),
        "https://www.example.com/company/career/": (200, "https://www.example.com/company/career/", CAREERS_PAGE),
    }))
    found = job_postings_crawler._discover_careers_page("www.example.com")
    assert (found["url"], found["found_via"]) == ("https://www.example.com/company/career/", "homepage link 'Career'")


def test_discover_careers_page_prefers_the_listing_page_over_a_careers_landing_page(monkeypatch):
    home = '<a href="/company/career/">Career</a><a href="/company/career/jobs/">Jobs</a>'
    monkeypatch.setattr(job_postings_crawler.requests, "get", _site({
        "HOME": (200, HOME_URL, home),
        "https://www.example.com/company/career/": (200, "https://www.example.com/company/career/", CAREERS_PAGE),
        "https://www.example.com/company/career/jobs/": (200, "https://www.example.com/company/career/jobs/", CAREERS_PAGE),
    }))
    found = job_postings_crawler._discover_careers_page("www.example.com")
    assert found["url"] == "https://www.example.com/company/career/jobs/"


def test_discover_careers_page_rejects_a_custom_404_that_renders_the_nav(monkeypatch):
    """A custom 404 page still shows the site nav, which contains 'Lavora con noi' —
    the content check alone would accept it; the <title> gives it away."""
    home = '<html><body><nav><a href="/lavora-con-noi">Lavora con noi</a></nav></body></html>'
    nav_404 = ('<html><title>Pagina non trovata</title><body><nav><a href="/lavora-con-noi">Lavora con noi</a>'
               '</nav></body></html>')

    def fake_get(url, **kw):
        if url.rstrip("/") == HOME_URL.rstrip("/"):
            return _Resp(200, HOME_URL, home)
        return _Resp(200, url, nav_404)

    monkeypatch.setattr(job_postings_crawler.requests, "get", fake_get)
    assert job_postings_crawler._discover_careers_page("www.example.com") is None


def test_discover_careers_page_falls_back_to_the_sitemap(monkeypatch):
    sitemap = ("<urlset><url><loc>https://www.example.com/about/</loc></url>"
               "<url><loc>https://www.example.com/careers/</loc></url></urlset>")
    monkeypatch.setattr(job_postings_crawler.requests, "get", _site({
        "HOME": (200, HOME_URL, "<html><body>no links here</body></html>"),
        "https://www.example.com/sitemap.xml": (200, "https://www.example.com/sitemap.xml", sitemap),
        "https://www.example.com/careers/": (200, "https://www.example.com/careers/", CAREERS_PAGE),
    }))
    found = job_postings_crawler._discover_careers_page("www.example.com")
    assert (found["url"], found["found_via"]) == ("https://www.example.com/careers/", "sitemap.xml")


def test_discover_careers_page_never_follows_offsite_links(monkeypatch):
    home = '<a href="https://www.linkedin.com/company/example/jobs/">Jobs</a>'
    fetched = []

    def fake_get(url, **kw):
        fetched.append(url)
        if url.rstrip("/") == HOME_URL.rstrip("/"):
            return _Resp(200, HOME_URL, home)
        return _Resp(404, url, "")

    monkeypatch.setattr(job_postings_crawler.requests, "get", fake_get)
    assert job_postings_crawler._discover_careers_page("www.example.com") is None
    assert not any("linkedin.com" in u for u in fetched)


def test_discover_careers_page_still_probes_common_paths_last(monkeypatch):
    monkeypatch.setattr(job_postings_crawler.requests, "get", _site({
        "HOME": (200, HOME_URL, "<html><body>no links here</body></html>"),
        "https://www.example.com/lavora-con-noi": (200, "https://www.example.com/lavora-con-noi", CAREERS_PAGE),
    }))
    found = job_postings_crawler._discover_careers_page("www.example.com")
    assert found["url"] == "https://www.example.com/lavora-con-noi"
    assert found["found_via"] == "path probe /lavora-con-noi"


def test_discover_careers_page_returns_the_homepage_nav_links(monkeypatch):
    home = '<a href="/company/">Company</a><a href="/products/">Products</a><a href="/careers/">Careers</a>'
    monkeypatch.setattr(job_postings_crawler.requests, "get", _site({
        "HOME": (200, HOME_URL, home),
        "https://www.example.com/careers/": (200, "https://www.example.com/careers/", CAREERS_PAGE),
    }))
    found = job_postings_crawler._discover_careers_page("www.example.com")
    assert found["site_nav_urls"] == ["https://www.example.com/careers", "https://www.example.com/company",
                                      "https://www.example.com/products"]


def test_discover_careers_page_falls_back_to_the_main_domain(monkeypatch):
    """A DUE's record points at spareparts.adue.it (its own careers link 404s) while
    www.adue.it/career/ lists 11 open roles."""
    def fake_get(url, **kw):
        if url.startswith("https://spareparts.example.com"):
            return _Resp(200 if url.rstrip("/") == "https://spareparts.example.com" else 404,
                         url, "<html><a href='/career/index.html'>CAREER</a></html>")
        if url.rstrip("/") == "https://www.example.com":
            return _Resp(200, "https://www.example.com/", '<a href="/career/">Career</a>')
        if url == "https://www.example.com/career/":
            return _Resp(200, url, CAREERS_PAGE)
        return _Resp(404, url, "")

    monkeypatch.setattr(job_postings_crawler.requests, "get", fake_get)
    found = job_postings_crawler._discover_careers_page("spareparts.example.com")
    assert found["url"] == "https://www.example.com/career/"
    assert "main domain www.example.com" in found["found_via"]
    assert job_postings_crawler._apex_homepage("www.example.com") is None      # nothing deeper to fall back to
    assert job_postings_crawler._apex_homepage("b2b.tecnoinox.it") == "https://www.tecnoinox.it"


def test_site_navigation_and_chrome_words_are_not_listings():
    """heila.com/careers/ yielded COMPANY / PRODUCTS & SERVICES / MAGAZINE / SERVICE /
    REQUEST A QUOTE as five open roles — the site's own menu, not wrapped in <nav>."""
    roles = [
        {"title": "COMPANY", "url": "https://heila.com/company/"},
        {"title": "PRODUCTS & SERVICES", "url": "https://heila.com/products-services/"},
        {"title": "MAGAZINE", "url": "https://heila.com/magazine/"},
        {"title": "Some Other Page", "url": "https://heila.com/other/"},          # in the nav, odd title
        {"title": "Service Engineer", "url": "https://heila.com/careers/service-engineer/"},
    ]
    nav = ["https://heila.com/company/", "https://heila.com/products-services/", "https://heila.com/magazine/",
           "https://heila.com/other/", "https://heila.com/careers/"]
    kept = job_postings_crawler._plausible_listings(roles, ["https://heila.com/careers/"], nav)
    assert [r["title"] for r in kept] == ["Service Engineer"]
    signals = job_postings_crawler._derive_signals({
        "technical_digital_roles_count": 0, "total_open_roles": 5, "roles_sample": roles, "site_nav_urls": nav,
        "sources_used": [{"url": "https://heila.com/careers/"}],
        "field_status": {"technical_digital_roles_count": "value"},
    })
    assert "digital_lead_role_present" not in signals   # one real listing is below the gate threshold


def test_digital_count_is_recounted_over_the_listings_that_survived_filtering():
    """bortolinkemo.com: the crawler extracted 10 "listings" (8 menu items + 2 real
    roles) and counted 4 keyword matches, most from the menu pages' text. With the
    complete sample and per-listing verdicts, the wrapper recounts over the two."""
    form = "https://www.bortolinkemo.com/it/lavora-con-noi"
    nav = ["https://www.bortolinkemo.com/it/azienda", "https://www.bortolinkemo.com/it/servizi",
           "https://www.bortolinkemo.com/it/news", "https://www.bortolinkemo.com/it/contatti"]
    sample = [{"title": t, "url": u, "matches_digital_keywords": m, "qualification_match": q}
              for t, u, m, q in [
                  ("Azienda", nav[0], True, False), ("Servizi", nav[1], True, None),
                  ("News", nav[2], True, False), ("Contatti", nav[3], False, None),
                  ("SOFTWARISTA C# E PLC SIEMENS", form + "#posizione=SOFTWARISTA", True, None),
                  ("Progettista Meccanico", form + "#posizione=Progettista", False, None),
              ]]
    signals = job_postings_crawler._derive_signals({
        "technical_digital_roles_count": 4, "total_open_roles": 6, "technical_qualification_share": 0.0,
        "roles_sample": sample, "site_nav_urls": nav, "sources_used": [{"url": form}],
        "field_status": {"technical_digital_roles_count": "value", "technical_qualification_share": "value"},
    })
    counted = signals["digital_job_postings"]
    assert counted["value"] == 1.0 and "1 of 2" in counted["summary"]
    assert counted["evidence"]["matched_titles"] == ["SOFTWARISTA C# E PLC SIEMENS"]
    assert "skilled_labour_share" not in signals   # no description was fetched for the two real roles

    # a partial sample with junk in it and no way to recount asserts no count at all
    signals = job_postings_crawler._derive_signals({
        "technical_digital_roles_count": 4, "total_open_roles": 40,
        "roles_sample": [{"title": "Azienda", "url": nav[0]}, {"title": "Progettista", "url": form + "/p1"}],
        "site_nav_urls": nav, "sources_used": [{"url": form}],
        "field_status": {"technical_digital_roles_count": "value"},
    })
    assert "digital_job_postings" not in signals


def test_select_option_listings_survive_the_self_link_filter():
    """bortolinkemo.com lists its open roles as <option>s of the application form; the
    crawler reports them with a #posizione= fragment on the form's own URL."""
    form = "https://www.bortolinkemo.com/it/lavora-con-noi"
    roles = [
        {"title": "SOFTWARISTA C# E PLC SIEMENS", "url": form + "#posizione=SOFTWARISTA%20C%23%20E%20PLC%20SIEMENS"},
        {"title": "Progettista Meccanico", "url": form + "#posizione=Progettista%20Meccanico"},
        {"title": "CARICA IL TUO CURRICULUM VITAE", "url": "javascript:;"},
    ]
    kept = job_postings_crawler._plausible_listings(roles, [form], [form])
    assert [r["title"] for r in kept] == ["SOFTWARISTA C# E PLC SIEMENS", "Progettista Meccanico"]


def test_fetch_homepage_falls_back_to_plain_http(monkeypatch):
    def fake_get(url, **kw):
        if url.startswith("https://"):
            raise requests.RequestException("certificate verify failed")
        return _Resp(200, url, "<html></html>")

    monkeypatch.setattr(job_postings_crawler.requests, "get", fake_get)
    assert job_postings_crawler._fetch_homepage("www.example.com").url == "http://www.example.com"


# ---------------------------------------------------------------- subprocess timeout

@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_run_kills_the_whole_process_tree_on_timeout():
    """subprocess.run(shell=True, timeout=...) only killed the shell on Windows and then
    blocked on the pipes until the node grandchild exited by itself (measured live: a
    3s timeout returned after 40s). _run must return promptly AND leave no node behind."""
    marker = "VIENNA_TREE_TEST_%d" % os.getpid()
    script = ("const {spawn}=require('child_process');"
              "spawn(process.execPath,['-e','setTimeout(()=>{},60000)','" + marker + "'],{stdio:'inherit'});"
              "setTimeout(()=>{},60000);")
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        node_crawler_base._run([shutil.which("node"), "-e", script, marker], cwd=Path.cwd(),
                               env=dict(os.environ), timeout=2)
    assert time.time() - t0 < 20, "timeout was not enforced"
    time.sleep(1)
    if os.name == "nt":
        ps = ("(Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
              "Where-Object { $_.CommandLine -like '*" + marker + "*' } | Measure-Object).Count")
        survivors = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                   capture_output=True, text=True).stdout.strip()
    else:
        survivors = subprocess.run(["pgrep", "-fc", marker], capture_output=True, text=True).stdout.strip()
    assert survivors in ("", "0"), "orphaned node processes survived the timeout: %s" % survivors
