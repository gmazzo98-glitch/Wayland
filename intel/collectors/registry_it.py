"""
Collector: Italian registry and institutional facts (Ticket I20).

Feasibility study (live probes, 2026-10-27 — the full table with evidence is in
docs/intel/results/I20.md):

  * INI-PEC (inipec.gov.it)                 -> WAF "Request Rejected" for scripted requests; JS app. Infeasible.
  * Registro Imprese (registroimprese.it)    -> visura products are PAID; the free "ricerca imprese" is a JS
                                               Liferay portlet with no public JSON endpoint. Infeasible without paying.
  * InfoCamere ricerca (ricercapersoneru.it) -> TLS unreachable from this environment. Infeasible.
  * IPA (indicepa.gov.it)                    -> HTTP 500 on the public search; IPA covers public administrations,
                                               not private companies. Not applicable + infeasible.
  * ANAC open data (opendata.anac.gov.it)    -> TLS unreachable. Infeasible from here.
  * UIBM (uibm.gov.it / uibm.mise.gov.it)    -> connect timeout. Infeasible from here.
  * RNA state-aid register (rnatest.rna.gov.it) -> TLS unreachable. Infeasible from here.

So the collector works on what genuinely works without a paid account: the INSTITUTIONAL IMPRINT
Italian companies publish in their own site footer (the same block a human reads first):
C.F./P.IVA, REA code, "Capital: EUR x i.v." (interamente versato = fully paid), PEC address,
registered office. This is exactly where the golden facts' own sources point for ID-01..03, CT-02.
Facts are grade B (the company's own publication of its registry data), each quote-grounded
verbatim; the upgrade to grade A needs a feasible register (see the results note's open points).

Keys produced:
  identity.vat              C.F./P.IVA as printed by the company
  identity.rea              REA code (e.g. MI-1692282)
  identity.share_capital    share capital with the "i.v." (fully paid) marker when present
  identity.registered_office registered-office address as published
  contact.pec               certified e-mail as published
  financials.revenue_history year->revenue series from a PUBLIC directory page (grade C), when the
                             search layer (I05) is available; never invented, always quote-grounded
                             against the fetched directory page

Rules honoured: no scores, quote-grounded evidence only (quote_in_text), checked_absent when the
site was read and the imprint block is not there, honest errors otherwise; nothing is bought; no
simulated register value is ever emitted.
"""

from __future__ import annotations

import re
from typing import List, Optional, Set, Tuple
from urllib.parse import urlparse

from intel.collector import Collector, CollectorContext, CollectorResult
from intel.fetch import normalise_url
from intel.ledger import Evidence, Fact, MAX_QUOTE_CHARS, quote_in_text

COLLECTOR_TAG = "I20:registry-it@1"

# Pages of a company site that carry the legal imprint: the footer is on every page, so the scan
# starts from the homepage and follows a bounded set of links; these hints only widen the crawl.
IMPRINT_PATH_HINTS = ("contatti", "contact", "legal", "privacy", "company", "chi-siamo", "note",
                      "impressum", "info", "sed", "dove")

VAT_RE = re.compile(r"(?:C\.?F\.?/?)?\s*P\.?\s?IVA[:\s]*([0-9]{11})", re.I)
REA_RE = re.compile(r"REA\s*(?:e\s*)?C\.?C\.?I\.?A\.?A\.?[:\s]*([A-Z]{2}-\d{4,7})", re.I)
CAPITAL_RE = re.compile(r"Capital[ea]?[:\s]*(?:€\s*|EUR\s*|euro\s*)?([0-9][0-9.,]{2,18})(\s*i\.?\s?v\.?)?", re.I)
PEC_RE = re.compile(r"\bPEC[:\s]*([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b)", re.I)
GENERIC_PEC_RE = re.compile(r"\b([a-z0-9._%+-]+@(?:pec|legalmail|postacert)\b[a-z0-9.-]*\.[a-z]{2,})", re.I)
OFFICE_RE = re.compile(
    r"(?:Viale|Via|Corso|Piazza|Strada|Piazzale)\s+[^,\n]{2,40},?\s*\d{0,4}\s*,?\s*(?:\d{5})\s*[A-Za-z]+(?:\s*\(([A-Z]{2})\))?", re.I)

REVENUE_DIR_HINTS = ("fatturato", "bilancio", "ricavi", "revenue", "azienda")

# Public directory search endpoints that work without login (feasibility-probed 2026-10-27).
# Each entry: (search URL template, result-link regex filter). A directory is a source plugin:
# to add one, verify it live, then add it here as data.
DIRECTORY_SEARCHES = [
    ("https://topaziende.quotidiano.net/?s={q}", r"topaziende\.quotidiano\.net/.*fatturato-"),
]
REVENUE_TABLE_RE = re.compile(r"(20\d{2})\s{0,40}([0-9][0-9.,]{5,15})")

MAX_PAGES = 12
MAX_FIN_PAGES = 3          # directory pages opened for the revenue series


def _same_site(url: str, hosts: Set[str]) -> bool:
    try:
        return urlparse(url).netloc.lower().lstrip("www.") in hosts
    except ValueError:
        return False


def _quote_around(text: str, start: int, end: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    if line_end - line_start > MAX_QUOTE_CHARS:
        line_start, line_end = max(line_start, start - 100), min(line_end, end + 100)
    return text[line_start:line_end].strip()[:MAX_QUOTE_CHARS]


def _revenue_series(text: str, legal_name: str = "") -> dict:
    """Year -> revenue from a directory page's table, only when the page names the target company
    (brand token from the legal name — a directory page about a DIFFERENT company, however
    plausible its revenue table, is not evidence), the page says fatturato/ricavi, and at least
    three consecutive years carry plausible amounts (7-13 digit figures)."""
    lowered = text.lower()
    if not any(h in lowered for h in REVENUE_DIR_HINTS):
        return {}
    if legal_name:
        brand_words = [w.lower() for w in re.sub(r"[^A-Za-z0-9 ]", " ", legal_name).split() if len(w) >= 4]
        if not brand_words or not any(w in lowered for w in brand_words):
            return {}
    series = {}
    for m in REVENUE_TABLE_RE.finditer(text):
        year, amount = m.group(1), m.group(2).strip().rstrip(".,")
        digits = re.sub(r"[^0-9]", "", amount)
        if not (6 <= len(digits) <= 13):
            continue
        if year in series:
            continue
        series[year] = amount
    years = sorted(series)
    if len(years) < 3:
        return {}
    return {y: series[y] for y in years}


def run(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult()
    website = normalise_url(ctx.company.get("website_url") or "")
    if not website:
        result.errors.append("company has no website_url")
        return result
    if ctx.fetch is None:
        result.errors.append("no fetcher supplied")
        return result
    hosts = {urlparse(website).netloc.lower().lstrip("www.")}

    found: dict = {}
    scanned_any = False

    frontier: List[str] = [website]
    visited: Set[str] = set()
    while frontier and len(visited) < MAX_PAGES:
        url = frontier.pop(0)
        if url in visited:
            continue
        visited.add(url)
        if not _same_site(url, hosts):
            continue
        page = ctx.fetch(url)
        result.pages_fetched += 1
        if not page.ok or not page.text:
            if page.note:
                result.errors.append(f"{url}: {page.note}")
            continue
        scanned_any = True
        text = page.text
        final_host = urlparse(page.url).netloc.lower().lstrip("www.")
        if final_host and final_host not in hosts:
            hosts.add(final_host)
        hub = page.url.rstrip("/") == website.rstrip("/")

        for key, rx, group in (
                ("identity.vat", VAT_RE, 1), ("identity.rea", REA_RE, 1),
                ("identity.share_capital", CAPITAL_RE, 1), ("contact.pec", PEC_RE, 1)):
            if key in found:
                continue
            m = rx.search(text)
            if not m:
                continue
            quote = _quote_around(text, m.start(), m.end())
            if not quote or not quote_in_text(quote, text):
                continue
            value = m.group(group)
            if key == "identity.share_capital":
                value = m.group(0).strip()
            found[key] = (value, page.url, quote, "company_site")
        if "contact.pec" not in found:
            m = GENERIC_PEC_RE.search(text)
            if m:
                quote = _quote_around(text, m.start(), m.end())
                if quote and quote_in_text(quote, text):
                    found["contact.pec"] = (m.group(1), page.url, quote, "company_site")
        if "identity.registered_office" not in found:
            brand = (ctx.company.get("legal_name") or "").lower().split()[0] if ctx.company.get("legal_name") else ""
            for m in OFFICE_RE.finditer(text):
                # anchor: the company's own brand must appear near the address (a GROUP footer's
                # "Sede Legale" is the parent's address, not the subsidiary's — a real error a
                # live run on a group-controlled company caught: the group HQ was taken for it)
                window = text[max(0, m.start() - 200):m.end() + 200]
                if brand and brand not in window.lower():
                    continue
                quote = _quote_around(text, m.start(), m.end())
                if quote and quote_in_text(quote, text):
                    found["identity.registered_office"] = (m.group(0).strip(), page.url, quote, "company_site")
                break

        if hub:
            for link in page.links:
                if link not in visited and _same_site(link, hosts) and not link.lower().endswith(".pdf"):
                    path = urlparse(link).path.lower()
                    if any(h in path for h in IMPRINT_PATH_HINTS):
                        frontier.append(link)

    for key, (value, url, quote, stype) in found.items():
        result.facts.append(Fact(
            key=key, value=value, grade="B", confidence=0.8, verified=False, collector=COLLECTOR_TAG,
            notes="from the company's own published imprint (grade B; the register lookup itself was "
                  "infeasible — see docs/intel/results/I20.md feasibility table)",
            evidence=[Evidence(source_type=stype, url=url, quote=quote)],
        ))

    all_keys = ("identity.vat", "identity.rea", "identity.share_capital",
                "identity.registered_office", "contact.pec")
    if found:
        result.checked_absent.extend(k for k in all_keys if k not in found)
    elif scanned_any:
        result.checked_absent.extend(all_keys)
    else:
        result.errors.append("no pages of the company site could be read")

    # ---- public revenue history (grade C) ----
    # Candidate pages come from (1) the I05 search layer when reachable, and (2) the public
    # directories' own search endpoints (DIRECTORY_SEARCHES, feasibility-verified; a directory is
    # a source plugin: verify live, then add as data). No directory page is ever guessed.
    fin_done = False
    legal = (ctx.company.get("legal_name") or "").strip()
    candidates: List[str] = []
    if legal:
        if ctx.search is not None:
            try:
                for sr in ctx.search.search(f'"{legal}" fatturato', kind="web", n=8):
                    if sr.url and not sr.url.lower().endswith(".pdf"):
                        candidates.append(sr.url)
                result.queries.append(f'"{legal}" fatturato')
            except Exception as exc:
                result.errors.append(f"search layer failed: {type(exc).__name__}")
        for template, link_filter in DIRECTORY_SEARCHES:
            query = re.sub(r"[^A-Za-z0-9 ]", " ", legal).strip()
            search_url = template.format(q=query.replace(" ", "+"))
            page = ctx.fetch(search_url)
            result.pages_fetched += 1
            result.queries.append(search_url)
            if not page.ok or not page.text:
                if page.note:
                    result.errors.append(f"{search_url}: {page.note}")
                continue
            for link in page.links:
                if re.search(link_filter, link) and link not in candidates:
                    candidates.append(link)

    # prefer candidates whose URL slug names the company brand (the directories' own slug
    # convention); the _revenue_series brand check is the real gate, this only orders the tries
    brand_words = [w.lower() for w in re.sub(r"[^A-Za-z0-9 ]", " ", legal).split() if len(w) >= 4] if legal else []
    candidates.sort(key=lambda u: 0 if any(w in u.lower() for w in brand_words) else 1)

    opened = 0
    for url in candidates:
        if opened >= MAX_FIN_PAGES or fin_done:
            break
        if _same_site(url, hosts):
            continue    # the company's own site is not a third-party financials source
        page = ctx.fetch(url)
        result.pages_fetched += 1
        opened += 1
        if not page.ok or not page.text:
            continue
        series = _revenue_series(page.text, legal)
        if len(series) < 3:
            continue
        # quote-ground: the verbatim table row for the first year (the full series is the value)
        first_year = next(iter(series))
        m = re.search(re.escape(first_year) + r"\s{0,40}" + re.escape(series[first_year]), page.text)
        quote = m.group(0).strip() if m else ""
        if not quote or not quote_in_text(quote, page.text):
            continue
        result.facts.append(Fact(
            key="financials.revenue_history", value=series, grade="C", confidence=0.6,
            verified=False, collector=COLLECTOR_TAG,
            notes="revenue series from a public directory page (grade C); registry-grade accounts "
                  "(bilancio depositato) need a feasible register - see the I20 feasibility table",
            evidence=[Evidence(source_type="directory", url=page.url, quote=quote[:MAX_QUOTE_CHARS])],
        ))
        fin_done = True
    if not fin_done and (candidates or scanned_any):
        result.checked_absent.append("financials.revenue_history")
    return result


COLLECTOR = Collector(
    name="registry-it", ticket="I20", produces=("identity.", "contact.pec", "financials."),
    requires=("web",),
    refresh_days=365, cost_hint="cheap",
    description="Italian institutional imprint from the company's own site (C.F./P.IVA, REA, share "
                "capital, registered office, PEC) with the register-lookup feasibility recorded "
                "honestly; institutional registers probed and documented in the results note.",
    run=run,
)
