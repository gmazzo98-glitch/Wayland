"""
Wraps Scraper/crawlers/job-postings-crawler (Phase 7). Finds a company's open
roles (own careers page, optionally Indeed/Stepstone) and counts
technical/digital roles and qualification share.

Feeds: digital_job_postings, skilled_labour_share, digital_lead_role_present
(a title-keyword scan over the sample of open roles — the closest automatable
proxy for "does a named digital/innovation lead role exist", matching that
indicator's own proxy text: "Job title search ... on company website").
ERP systems age (T3 in the source spreadsheet) is deliberately NOT derived
here: the crawler's roles_sample only carries a title, not a full description,
which isn't enough text to detect an ERP vendor mention without guessing.
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests
from sqlalchemy.orm import Session

from adapters.base import run_adapter
from scrapers.node_crawler_base import (
    CrawlerRunError, run_ts_crawler, rows_for_company, save_crawler_blob,
)

SOURCE_NAME = "Job Postings Crawler"
CRAWLER_DIR = "job-postings-crawler"
DATASET_NAME = "crawler_job_postings"
PHASE = 7
# Minimum plausible listings before the digital-lead gate may be asserted absent.
MIN_LISTINGS_FOR_GATE = 3

# The crawler treats whatever careers_url it's handed AS the careers page. Handing it
# a bare homepage makes its generic adapter harvest every link on that page as a
# "listing" — verified live against a real Italian manufacturer, which produced 12
# "roles" titled "SCARICA IL CATALOGO" / "VISUALIZZA PRODOTTO". So a careers URL is
# probed for first (same approach as scrapers/management_diversity.py's path probe),
# and when none exists the crawler is given no careers source at all rather than the
# homepage — no source means no signal, which is the honest outcome.
CAREERS_PATHS = [
    "/careers", "/jobs", "/work-with-us", "/career", "/join-us",
    "/lavora-con-noi", "/it/lavora-con-noi", "/azienda/lavora-con-noi", "/it/azienda/lavora-con-noi",
    "/carriere", "/risorse-umane", "/posizioni-aperte",
    "/karriere", "/stellenangebote", "/jobs-karriere", "/unternehmen/karriere",
    "/en/careers", "/en/jobs", "/company/careers", "/company/career",
]
_PROBE_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ProjectViennaBot/1.0)"}
_PROBE_TIMEOUT = 6
# Whole discovery (homepage + links + sitemap + path probes) must fit in this budget so
# the crawler subprocess still gets its own full run_timeout inside the adapter's budget.
_DISCOVERY_BUDGET_SECONDS = 45
_MAX_DISCOVERED_CANDIDATES = 6

# A careers page should say so somewhere. Guards against a path that 200s but is
# really a catch-all/marketing page.
_CAREERS_CONTENT_RE = re.compile(
    r"lavora con noi|posizioni aperte|offerte di lavoro|candidatur|curriculum|"    # IT
    r"stellenangebote|karriere|bewerb|offene stellen|"                              # DE
    r"job opening|open position|vacanc|careers|apply now|join our team|work with us",  # EN
    re.I,
)
# Link text a site uses to point at its own careers page — the site tells us where the
# page is, instead of us guessing a path. Live-verified to find pages the fixed path list
# misses (emag.com's /company/career/, sanmarcotaps/heila-style /careers/ via sitemap).
_CAREERS_LINK_TEXT_RE = re.compile(
    r"lavora con noi|lavoraconnoi|posizioni aperte|opportunit[àa] di lavoro|offerte di lavoro|carriere|candidatur|"
    r"risorse umane|entra nel (nostro )?team|unisciti|"
    r"karriere|stellenangebote|offene stellen|"
    r"\bjobs?\b|\bcareers?\b|work with us|join (our|the) team|join us|vacanc|recruit|open positions?",
    re.I,
)
_CAREERS_PATH_RE = re.compile(
    r"lavora|carrier|career|\bjobs?\b|karriere|stellen|recruit|candidat|opportunit|risorse-umane|posizioni|join-us",
    re.I,
)
# When a site has both a careers landing page and an actual listing page, prefer the
# listing (emag.com: /company/career/ vs /company/career/jobs/).
_LISTING_PATH_BONUS_RE = re.compile(r"jobs|posizioni|stellenangebote|offene|open-positions|offerte|vacanc|opportunit", re.I)
# A custom 404 page that renders the site's nav (which contains "Lavora con noi") would
# otherwise pass the content check — the <title> is where a 404 page admits what it is.
_NOT_FOUND_TITLE_RE = re.compile(r"pagina non trovata|page not found|non trovat|nicht gefunden|\b404\b|not found", re.I)


def _normalized(url: str) -> str:
    """scheme+host+path, lowercased, no trailing slash — for comparing a probe's
    landing page against the homepage."""
    try:
        p = urlparse(url)
        return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path.rstrip('/').lower()}"
    except ValueError:
        return (url or "").rstrip("/").lower()


class _LinkCollector(HTMLParser):
    """(href, label) for every <a> on a page, label = visible text + title/aria-label.
    Stdlib only, so the wrapper adds no dependency for a 15-line job."""

    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._label = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        a = dict(attrs)
        self._href = a.get("href")
        self._label = [a.get("title") or "", a.get("aria-label") or ""]

    def handle_data(self, data):
        if self._href is not None:
            self._label.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append(((self._href or "").strip(), " ".join(" ".join(self._label).split())))
            self._href = None
            self._label = []


def _get(url: str):
    return requests.get(url, timeout=_PROBE_TIMEOUT, headers=_PROBE_HEADERS, allow_redirects=True)


def _fetch_homepage(website_url: str):
    """https first (most scheme-less DB URLs are), then plain http — a site with a
    broken certificate or no TLS is still a site."""
    base = website_url if re.match(r"^https?://", website_url, re.I) else f"https://{website_url}"
    try:
        return _get(base)
    except requests.RequestException:
        if not base.lower().startswith("https://"):
            return None
        try:
            return _get("http://" + base[len("https://"):])
        except requests.RequestException:
            return None


def _same_site(url: str, home: str) -> bool:
    a = urlparse(url).netloc.lower().split(":")[0]
    b = urlparse(home).netloc.lower().split(":")[0]
    a, b = a[4:] if a.startswith("www.") else a, b[4:] if b.startswith("www.") else b
    return bool(a) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def _page_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    return " ".join(m.group(1).split()) if m else ""


def _looks_like_careers_page(resp, home_url: str) -> bool:
    if resp.status_code != 200:
        return False
    if _normalized(resp.url) == home_url:
        return False  # soft-404 / catch-all redirect back to the homepage
    text = resp.text or ""
    if _NOT_FOUND_TITLE_RE.search(_page_title(text)):
        return False
    return bool(_CAREERS_CONTENT_RE.search(text))


def _sitemap_candidates(home_url: str, deadline: float) -> list:
    """Careers-looking URLs from /sitemap.xml (following up to 3 nested sitemaps)."""
    try:
        sm = _get(urljoin(home_url, "/sitemap.xml"))
    except requests.RequestException:
        return []
    if sm.status_code != 200 or "<loc>" not in (sm.text or ""):
        return []
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sm.text)
    nested = [l for l in locs if l.lower().endswith(".xml")]
    if nested and len(nested) == len(locs):
        for sub_url in nested[:3]:
            if time.monotonic() > deadline:
                break
            try:
                sub = _get(sub_url)
            except requests.RequestException:
                continue
            locs += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sub.text or "")
    return [l for l in locs if _CAREERS_PATH_RE.search(urlparse(l).path) and _same_site(l, home_url)][:10]


def _apex_homepage(website_url: str):
    """'https://www.<apex>' when the stored site is a deeper subdomain, else None.
    A DUE's record points at spareparts.adue.it (a parts-ordering portal whose own
    careers link 404s) while www.adue.it/career/ lists 11 open roles; TECNOINOX's
    points at b2b.tecnoinox.it, a customer login portal."""
    host = urlparse(website_url if re.match(r"^https?://", website_url or "", re.I) else f"https://{website_url}").netloc
    host = host.split(":")[0].lower()
    labels = [l for l in host.split(".") if l]
    if labels[:1] == ["www"]:
        labels = labels[1:]
    if len(labels) <= 2:
        return None
    return "https://www." + ".".join(labels[-2:])


def _probe_paths_concurrently(home_url: str, home_norm: str, deadline: float):
    """The fixed-path fallback, five requests at a time — sequentially it took 40-48s
    on every site with no careers signpost (the common case), most of the budget."""
    urls = [urljoin(home_url.rstrip("/") + "/", p.lstrip("/")) for p in CAREERS_PATHS]
    remaining = max(1.0, deadline - time.monotonic())
    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="careers-probe") as pool:
        futures = [pool.submit(_get, u) for u in urls]
        try:
            for path, fut in zip(CAREERS_PATHS, futures):
                try:
                    resp = fut.result(timeout=max(0.1, deadline - time.monotonic()))
                except FutureTimeoutError:
                    break
                except requests.RequestException:
                    continue
                if _looks_like_careers_page(resp, home_norm):
                    return {"url": resp.url, "found_via": f"path probe {path}"}
        finally:
            for fut in futures:
                fut.cancel()
    return None


def _discover_careers_page(website_url: str, budget_seconds: float = _DISCOVERY_BUDGET_SECONDS, _try_apex: bool = True):
    """
    Finds the company's real careers page, or returns None. Order of evidence:
      1. links on the homepage whose text or path says "careers" (the site's own
         signpost — most reliable, and the only way to find non-standard paths);
      2. careers-looking URLs in /sitemap.xml;
      3. the fixed path list (last resort — a guess, verified like everything else);
      4. all of the above again on the main www. domain when the stored site is a
         deeper subdomain (a parts portal, a B2B login) — with a smaller budget.
    Every candidate must pass _looks_like_careers_page: a 200 is not enough. Many CMS
    sites answer any unknown path with the homepage (rcm.it: /careers, /jobs and
    /lavora-con-noi all 200'd with byte-identical homepage content, which previously
    became 28 "open roles" made of sector links), and custom 404 pages render the
    normal nav. Returns {"url", "found_via", "site_nav_urls"}: the last is every
    same-site link on the homepage, so the wrapper can refuse "listings" that are
    really the site's own navigation (heila.com/careers/ yielded COMPANY, PRODUCTS &
    SERVICES, MAGAZINE, SERVICE and REQUEST A QUOTE as five open roles).
    """
    if not website_url:
        return None
    deadline = time.monotonic() + budget_seconds
    home = _fetch_homepage(website_url)
    if home is None:
        return None
    home_url = _normalized(home.url)

    scored, nav_urls = {}, set()
    parser = _LinkCollector()
    try:
        parser.feed(home.text or "")
    except Exception:  # a malformed page must not take the whole discovery down
        pass
    for href, label in parser.links:
        if not href or href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urljoin(home.url, href)
        if not full.lower().startswith(("http://", "https://")) or not _same_site(full, home.url):
            continue
        nav_urls.add(_normalized(full))
        path = urlparse(full).path
        score = (2 if _CAREERS_LINK_TEXT_RE.search(label) else 0) + (1 if _CAREERS_PATH_RE.search(path) else 0)
        if score and _LISTING_PATH_BONUS_RE.search(path):
            score += 1
        if score:
            key = _normalized(full)
            if key not in scored or scored[key][0] < score:
                scored[key] = (score, full, f"homepage link '{label[:60]}'")
    candidates = sorted(scored.values(), key=lambda c: -c[0])
    candidates += [(1, loc, "sitemap.xml") for loc in _sitemap_candidates(home.url, deadline)]

    found = None
    seen, tried = set(), 0
    for score, url, via in candidates:
        key = _normalized(url)
        if key in seen or key == home_url:
            continue
        seen.add(key)
        if time.monotonic() > deadline or tried >= _MAX_DISCOVERED_CANDIDATES:
            break
        tried += 1
        try:
            resp = _get(url)
        except requests.RequestException:
            continue
        if _looks_like_careers_page(resp, home_url):
            found = {"url": resp.url, "found_via": via}
            break
    if found is None and time.monotonic() < deadline:
        found = _probe_paths_concurrently(home.url, home_url, deadline)
    if found is not None:
        found["site_nav_urls"] = sorted(nav_urls)[:300]
        return found

    apex = _apex_homepage(website_url) if _try_apex else None
    if apex and _normalized(apex) != home_url:
        via_apex = _discover_careers_page(apex, budget_seconds=min(25.0, budget_seconds), _try_apex=False)
        if via_apex:
            via_apex["found_via"] += f" (on the main domain {urlparse(apex).netloc}, not the stored {urlparse(home.url).netloc})"
            return via_apex
    return None


def _find_careers_url(website_url: str):
    """Returns a URL that is genuinely a careers page, or None (see _discover_careers_page)."""
    found = _discover_careers_page(website_url)
    return found["url"] if found else None


# The crawler's generic adapter treats link-ish elements on the page as listings, so
# a real careers page can still yield non-jobs — "CARICA IL TUO CURRICULUM VITAE"
# (upload your CV) with href="javascript:;" was returned as an open role by a live
# run. A listing is only counted when it points at a real, distinct document.
_CTA_TITLE_RE = re.compile(
    # no word boundaries: the crawler concatenates a link's child text ("Candidatialla posizione")
    r"carica il tuo|curriculum|invia (la tua )?candidatura|upload (your )?cv|^candidat|^apply|^submit|"
    r"privacy|cookie|newsletter|scarica|download|contatt|contact us|vedi tutt|view all",
    re.I,
)
# Site chrome that the crawler's generic extractor can mistake for a list of roles
# when a menu is not wrapped in <nav>/<header> — verified on heila.com/careers/, where
# COMPANY / PRODUCTS & SERVICES / MAGAZINE / SERVICE / REQUEST A QUOTE became five
# "open roles" and the count crossed the gate threshold. Whole-title match only.
_SITE_CHROME_TITLE_RE = re.compile(
    r"^(home|homepage|company|azienda|chi siamo|about( us)?|products?( ?& ?services)?|prodotti|servizi|services?|"
    r"news|magazine|blog|media|gallery|galleria|downloads?|contacts?|contatti|request a quote|richiedi (un )?preventivo|"
    r"careers?|lavora con noi|jobs?|login|area riservata|it|en|de|fr|es|pt|ru|zh|ar|pl|"
    # language switchers (www.adue.it/career/ yielded five of these as open roles)
    r"italiano|english|deutsch|fran[cç]ais|espa[nñ]ol|portugu[eê]s|русский|中文|日本語|한국어|العربية|polski|türkçe|nederlands)$",
    re.I,
)
# A listing the crawler read out of an application form's "position" <select>
# (sources/generic.ts heuristic 3): its URL is the form itself plus this fragment.
_SELECT_OPTION_FRAGMENT = "#posizione="


def _plausible_listings(roles_sample: list, source_urls: list, site_nav_urls: list = None) -> list:
    """Drops CTA buttons, javascript:/anchor hrefs, links back to the careers page
    itself, the site's own navigation links, and chrome-word titles — none of which
    are job postings. site_nav_urls are the homepage's same-site links, collected by
    _discover_careers_page."""
    source_norms = {_normalized(u) for u in source_urls}
    nav_norms = {_normalized(u) for u in (site_nav_urls or [])}
    keep = []
    for r in roles_sample or []:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        if not title or not url.lower().startswith(("http://", "https://")):
            continue
        if _SITE_CHROME_TITLE_RE.match(title):
            continue
        if _SELECT_OPTION_FRAGMENT in url:
            keep.append(r)  # lives on the careers form by construction — the checks below would reject it
            continue
        if _normalized(url) in nav_norms:
            continue
            continue
        if _normalized(url) in source_norms:
            continue
        if _CTA_TITLE_RE.search(title):
            continue
        keep.append(r)
    return keep

DIGITAL_LEAD_TITLE_RE = re.compile(
    r"Head of Digital|Chief Digital Officer|\bCDO\b|Innovation Manager|Head of Innovation|"
    r"Digitalisierungsbeauftragter|Innovationsmanager|Leiter Digitalisierung",
    re.I,
)


def _derive_signals(row: dict) -> dict:
    """
    Reads field_status BEFORE the value. The crawler still emits
    technical_digital_roles_count=0 when it never reached a single source
    (robots disallow, unreachable careers page) and flags that with
    field_status='not_found' — writing that 0 as a scored signal would turn
    "we couldn't look" into "confirmed: this company isn't hiring for digital
    roles", which is exactly the distinction SignalRecord's four-state status
    exists to preserve. Nothing is written unless a source was actually crawled.

    Every signal carries the postings behind it, so "1 digital role" can be
    checked against the actual titles and their URLs rather than taken on faith.
    """
    signals = {}
    field_status = row.get("field_status") or {}
    sources_used = row.get("sources_used") or []
    if not sources_used:
        return signals  # no source reached — every field below would be a fabricated zero

    source_urls = list(dict.fromkeys(s.get("url") for s in sources_used if s.get("url")))
    roles_sample = _plausible_listings(row.get("roles_sample"), source_urls, row.get("site_nav_urls"))
    total_roles = row.get("total_open_roles")
    sampled = [{"label": r.get("title"), "url": r.get("url")} for r in roles_sample if r.get("title")]

    roles_status = field_status.get("technical_digital_roles_count")
    tech_roles = row.get("technical_digital_roles_count")
    # The crawler's own count is computed over every "listing" it extracted, junk
    # included, and can't be recomputed here (only a 10-item sample comes back, and
    # the keyword list lives in the crawler). So if plausibility filtering rejected
    # ALL of the listings, the extraction isn't trustworthy enough to derive a count
    # from either — bortolinkemo.com's only "role" was a CV-upload button, which
    # would otherwise have been reported as "0 of 1 open roles".
    extraction_is_junk = bool(row.get("roles_sample")) and not roles_sample
    if extraction_is_junk:
        return signals

    if roles_status == "value" and tech_roles is not None:
        # The crawler matches keywords against title + snippet + full description,
        # but roles_sample carries titles only and is capped at 10 — so the sample
        # can legitimately show fewer title-matches than the count. Say so rather
        # than presenting the sample as the definitive list of what was counted.
        signals["digital_job_postings"] = {
            "value": float(tech_roles), "status": "present",
            "summary": f"{int(tech_roles)} of {total_roles} open roles matched digital/technical keywords"
                        + (f" ({len(roles_sample)} of the listings shown below survived junk-filtering)"
                           if len(roles_sample) < len(row.get("roles_sample") or []) else ""),
            "evidence": {
                "method": "keyword match over each posting's title, snippet and description",
                "counted": int(tech_roles), "considered": total_roles,
                "source_urls": source_urls,
                "open_roles_sample": sampled,
                "note": "sample shows up to 10 titles; matches can also come from description text not shown here",
            },
        }
    elif roles_status == "not_applicable":
        # A source was crawled and it advertises no open roles at all — a real,
        # established zero for a hiring-velocity signal, not a failed lookup.
        signals["digital_job_postings"] = {
            "value": 0.0, "status": "absent",
            "summary": "careers page crawled, advertises no open roles at all",
            "evidence": {"method": "careers page crawled, zero listings found",
                          "counted": 0, "considered": 0, "source_urls": source_urls},
        }

    qual_share = row.get("technical_qualification_share")
    if field_status.get("technical_qualification_share") == "value" and qual_share is not None:
        pct = float(qual_share) * 100.0 if qual_share <= 1.0 else float(qual_share)
        signals["skilled_labour_share"] = {
            "value": pct, "status": "present",
            "summary": f"{pct:.0f}% of the postings whose description was fetched require a technical/university qualification",
            "evidence": {
                "method": "qualification-keyword match, over postings whose full description was retrieved",
                "share_pct": round(pct, 1), "source_urls": source_urls,
                "open_roles_sample": sampled,
            },
        }

    # Gating indicator (gate_penalty_multiplier=0.7) — a false "absent" here costs the
    # company 30% of its readiness score, so it needs more than one stray link to fire.
    # Generic careers-page scraping is noisy enough that a single plausible listing is
    # not evidence that no digital-lead role exists; below the threshold, stay silent.
    if len(roles_sample) >= MIN_LISTINGS_FOR_GATE or any(
        DIGITAL_LEAD_TITLE_RE.search(r.get("title") or "") for r in roles_sample
    ):
        matched = [r.get("title") for r in roles_sample if DIGITAL_LEAD_TITLE_RE.search(r.get("title") or "")]
        found = bool(matched)
        signals["digital_lead_role_present"] = {
            "value": 1.0 if found else 0.0,
            "status": "present" if found else "absent",
            "summary": (f"matched open role: {matched[0]}" if found
                        else f"no digital/innovation-lead title among {len(roles_sample)} open roles"),
            "evidence": {
                "method": f"title regex over open roles: {DIGITAL_LEAD_TITLE_RE.pattern}",
                "matched_titles": matched, "source_urls": source_urls,
                "open_roles_sample": sampled,
            },
        }

    return signals


def sync_job_postings(company, db_session: Session) -> dict:
    captured = {}

    def _fetch_live(c):
        found = _discover_careers_page(c.website_url)
        careers_url = found["url"] if found else None
        # 100s for the subprocess (killed at that point, Chromium included), inside the
        # adapter's 160s budget together with up to 45s of careers-page discovery.
        rows = run_ts_crawler(CRAWLER_DIR, [{"company_id": c.id, "company_name": c.legal_name,
                                              "careers_url": careers_url or ""}], run_timeout=100)
        matches = rows_for_company(rows, c.id)
        if not matches:
            raise CrawlerRunError("job-postings-crawler returned no row for this company")
        row = matches[0]
        row["careers_discovery"] = (
            {"careers_url": careers_url, "found_via": found["found_via"]} if found
            else {"careers_url": None, "note": "no careers page found via homepage links, sitemap.xml, common paths "
                                                "or the main www. domain"}
        )
        row["site_nav_urls"] = found.get("site_nav_urls", []) if found else []
        captured["row"] = row
        if row.get("error"):
            raise CrawlerRunError(row["error"])
        return {
            "signals": _derive_signals(row),
            "raw_payload": {"total_open_roles": row.get("total_open_roles"), "sources_used": row.get("sources_used"),
                             "careers_discovery": row["careers_discovery"]},
            "confidence": 0.7,
        }

    def _simulate(c):
        return {
            "signals": {},
            "raw_payload": {"note": "job-postings-crawler unavailable"},
            "confidence": 0.5,
        }

    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        # 45s discovery (+25s on the main domain when the stored site is a subdomain)
        # + a 100s subprocess, with headroom.
        credentials_ok=True,
        fetch_live=_fetch_live, simulate=_simulate, timeout=190,
    )

    if captured.get("row"):
        save_crawler_blob(db_session, company, DATASET_NAME, captured["row"])

    return result
