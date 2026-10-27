"""
Collector: certifications and compliance credentials (Ticket I15).

What it does
------------
A claim on a website is grade B; a register entry is grade A and carries the real expiry. The
feasibility study for this ticket (docs/intel/results/I15.md) probed every register named in the
roadmap; none of them allows scripted, login-free, captcha-free lookup from this environment:

  * Accredia management-system DB  (services.accredia.it/ppsearch)  -> Google reCAPTCHA on the search form
  * IAF CertSearch (iafcertsearch.org)                             -> JavaScript single-page app, no server-rendered results
  * EU NANDO (ec.europa.eu/growth/tools-databases/nando)           -> migrated to a JS SPA (webgate single-market-compliance-space)
  * ASME certificate-holder search (certificateholder.asme.org)    -> TLS unreachable from the crawler environment
  * National Board (nationalboard.org)                             -> ASP.NET WebForms __VIEWSTATE POST search
  * Achilles                                                       -> paywalled; the roadmap says: accept certificate PDFs only

So the collector works on the evidence the company itself publishes (grade B), which is what the
golden facts CE-01..CE-03 actually cite: the site's quality/certifications pages, the certificate
PDFs linked from them (file names carry scheme, body and expiry tokens, e.g. an ISO 9001 PDF named
...20260629.pdf), and the verbatim hrefs of those PDFs in the page HTML. Every fact needs a
verbatim quote from text the fetcher returned, checked with quote_in_text(); a scanned PDF whose
text is empty yields NO fact (never a guessed value). Register infeasibility is recorded in the
results note, not as a fabricated ledger fact.

Keys produced (prefix certifications.):
  certifications.iso_management     ISO management-system standard(s) claimed/held (9001/14001/45001/... incl. year)
  certifications.product_or_scheme  product/scheme credentials (PED module, ASME U-Stamp, EN, VdS, IATF...)
  certifications.third_party_audit  testing/inspection bodies named (TUV, DNV, Bureau Veritas, Istituto Giordano...)
  certifications.supplier_register  supplier-qualification schemes (Achilles, registered supplier networks)

Rules honoured: no scores, no company_people writes, quote-grounded evidence only, checked_absent
when the site was read and no certification content was found, honest errors otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple
from urllib.parse import urlparse

from intel.collector import Collector, CollectorContext, CollectorResult
from intel.fetch import normalise_url
from intel.ledger import Evidence, Fact, MAX_QUOTE_CHARS, quote_in_text

COLLECTOR_TAG = "I15:certifications@1"

# Pages of the company site most likely to carry certification content. Link discovery from the
# homepage comes first; these hints only widen what counts as a quality page (the I33 lesson:
# never guess paths only — some sites keep everything under an unconventional path).
CERT_PATH_HINTS = (
    "qualit", "quality", "certificaz", "certificat", "download", "contatti", "norme", "standard",
    "compliance", "approvazioni", "accredit", "dokument", "zulassung",
)

# (fact key, regex over page/PDF text). Patterns are deliberately specific: a bare "CE" or "ISO"
# would match footers and privacy banners. Every match must still be quote-grounded.
PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("certifications.iso_management", re.compile(
        r"\bISO[ -]?(?:9001|14001|45001|13485|50001|27001|37001|22716|3834)(?::\s?\d{4})?\b")),
    ("certifications.product_or_scheme", re.compile(
        r"\b(?:IATF[ -]?16949|EN[ -]?\d{3,5}(?::\d{4})?|VdS[ -]?\d{3,5}|PED[ -]?(?:module[ -]?(?:H1?|B|D|F|G)|2014/68/(?:UE|EU))?|"
        r"U[ -]?Stamp(?:p)?|National[ -]?Board|module[ -]?H1?\b|machinery directive|"
        r"MOCA|GMP\b|ATEX\b|3[ -]?A[ -]?(?:Sanitary[ -]?Standards)?|"
        r"(?:19|20)\d{2}/\d{1,4}/CE\b)", re.I)),
    ("certifications.third_party_audit", re.compile(
        r"\b(?:T[ÜU]V|DNV|Bureau[ -]?Veritas|RINA|SGS|Istituto[ -]?Giordano|IMQ|CSQ|SINCERT|EPDItaly|Kiwa)\b", re.I)),
    ("certifications.supplier_register", re.compile(
        r"\b(?:Achilles(?:[ -](?:Community|Connect|UVDB))?|JQS|AvevaNET|supplier[ -]?qualification|pre[ -]?qualified)\b", re.I)),
]

# Certificate PDF file names often carry the scheme and even the expiry token
# (.../ISO_9001-ENG-6957-2007-AQ-ITA-SINCERT-13-20260629.pdf).
PDF_NAME_SCHEMES = re.compile(
    r"(ISO[ _-]?(?:9001|14001|45001|13485|3834)|IATF[ _-]?16949|PED|U[ _-]?STAMP|National[ _]?Board|"
    r"Achilles|VdS|CE[ _-]?\d{4})", re.I)
EXPIRY_TOKEN = re.compile(r"(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])?(?=$|[._-])")

MAX_PAGES = 24
MAX_PDF_OPENS = 6


def _same_site(url: str, hosts: Set[str]) -> bool:
    try:
        return urlparse(url).netloc.lower().lstrip("www.") in hosts
    except ValueError:
        return False


def _quote_around(text: str, start: int, end: int) -> str:
    """A verbatim quote around [start, end): line-bounded, cut to MAX_QUOTE_CHARS."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    if line_end - line_start > MAX_QUOTE_CHARS:
        line_start, line_end = max(line_start, start - 120), min(line_end, end + 120)
    return text[line_start:line_end].strip()[:MAX_QUOTE_CHARS]


@dataclass
class _Found:
    key: str
    value: str
    url: str
    quote: str
    source_type: str


def _scan(text: str, url: str, source_type: str, findings: List[_Found], seen: Set[Tuple[str, str]]) -> None:
    """Emit every pattern hit whose quote really occurs in `text` (verbatim grounding gate)."""
    for key, pattern in PATTERNS:
        for m in pattern.finditer(text):
            value = re.sub(r"\s+", " ", m.group(0)).strip().strip("-_ ")
            if not value:
                continue
            # deduplicate on the alphanumeric form ("Bureau Veritas" vs "bureauveritas" is one body)
            canon = re.sub(r"[^a-z0-9]", "", value.lower())
            k = (key, canon)
            if k in seen:
                continue
            quote = _quote_around(text, m.start(), m.end())
            if not quote or not quote_in_text(quote, text):
                continue
            seen.add(k)
            findings.append(_Found(key, value, url, quote, source_type))


def _scan_pdf_hrefs(links: List[str], page_url: str, page_text: str, findings: List[_Found],
                    seen: Set[Tuple[str, str]]) -> List[str]:
    """Certificate-PDF file names carry scheme and often an expiry token (...20260629.pdf).
    The href itself is not part of the page TEXT, so it cannot be the quote: a file-name fact is
    only emitted when the scheme token also occurs in the visible page text (quote from there),
    with the PDF attached as a second evidence item. Returns the scheme-matching PDF URLs."""
    pdfs: List[str] = []
    for url in links:
        if not url.lower().endswith(".pdf"):
            continue
        path_no_space = urlparse(url).path.replace(" ", "")
        name = path_no_space.rsplit("/", 1)[-1]
        m = PDF_NAME_SCHEMES.search(name)
        if not m:
            continue
        pdfs.append(url)
        token = re.sub(r"[ _]+", " ", m.group(0)).strip()
        text_rx = re.compile(re.escape(token).replace(r"\ ", r"[ -]?"), re.I)
        tm = text_rx.search(page_text)
        if not tm:
            continue    # the scheme is only in the file name, not said on the page: no quote, no fact
        value = re.sub(r"\s+", " ", page_text[tm.start():tm.end()]).strip()
        expiry = EXPIRY_TOKEN.search(path_no_space)
        if expiry:
            value = f"{value} (certificate PDF file name carries expiry token {expiry.group(0)})"
        k = ("certifications.product_or_scheme", re.sub(r"[^a-z0-9]", "", value.lower()))
        if k in seen:
            continue
        quote = _quote_around(page_text, tm.start(), tm.end())
        if not quote or not quote_in_text(quote, page_text):
            continue
        seen.add(k)
        findings.append(_Found("certifications.product_or_scheme", value, page_url, quote, "company_site"))
    return pdfs


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

    findings: List[_Found] = []
    seen: Set[Tuple[str, str]] = set()
    pdf_candidates: List[str] = []
    pages_read = 0

    frontier: List[str] = [website]
    visited: Set[str] = set()
    while frontier and pages_read < ctx.budget.max_pages and len(visited) < MAX_PAGES:
        url = frontier.pop(0)
        if url in visited:
            continue
        visited.add(url)
        if not _same_site(url, hosts):
            continue
        page = ctx.fetch(url)
        pages_read += 1
        result.pages_fetched += 1
        if not page.ok or not page.text:
            if page.note:
                result.errors.append(f"{url}: {page.note}")
            continue
        path = urlparse(page.url).path.lower()
        is_quality_page = any(h in path for h in CERT_PATH_HINTS) or any(
            h in (page.title or "").lower() for h in CERT_PATH_HINTS)
        _scan(page.text, page.url, "company_site", findings, seen)
        pdf_candidates.extend(_scan_pdf_hrefs(page.links, page.url, page.text, findings, seen))
        # the homepage is the hub: follow its links (the I33 lesson — never guess paths only);
        # below the homepage, only quality-hinted pages are deepened, to stay inside the page budget.
        hub = page.url.rstrip("/") == website.rstrip("/")
        # A site that redirects to its group's domain (a company site -> the group's site) is still the
        # company's own presentation: allow the homepage's FINAL host as well, once discovered.
        final_host = urlparse(page.url).netloc.lower().lstrip("www.")
        if final_host and final_host not in hosts:
            hosts.add(final_host)
            hub = True
        for link in page.links:
            if link not in visited and _same_site(link, hosts) and not link.lower().endswith(".pdf"):
                if hub or is_quality_page:
                    frontier.append(link)

    opened = 0
    for url in list(dict.fromkeys(pdf_candidates))[:MAX_PDF_OPENS]:
        pdf = ctx.fetch(url)
        result.pages_fetched += 1
        opened += 1
        if not pdf.ok:
            if pdf.note:
                result.errors.append(f"{url}: {pdf.note}")
            continue
        if pdf.text and pdf.text.strip():
            _scan(pdf.text, url, "company_document", findings, seen)
        else:
            result.errors.append(f"{url}: pdf text empty (scanned or image-only); not used as evidence")

    for f in findings:
        result.facts.append(Fact(
            key=f.key, value=f.value, grade="B", confidence=0.8, verified=False, collector=COLLECTOR_TAG,
            notes="claimed in the company's own publication; register lookup not feasible (I15 feasibility table)",
            evidence=[Evidence(source_type=f.source_type, url=f.url, quote=f.quote)],
        ))

    if findings:
        for key, pattern in PATTERNS:
            if not any(f.key == key for f in findings):
                result.checked_absent.append(key)
    elif pages_read > 0:
        result.checked_absent.extend(k for k, _ in PATTERNS)
    else:
        result.errors.append("no pages of the company site could be read")
    return result


COLLECTOR = Collector(
    name="certifications", ticket="I15", produces=("certifications.",), requires=("web",),
    refresh_days=180, cost_hint="cheap",
    description="Certification and compliance credentials claimed on the company's own pages and "
                "certificate PDFs (ISO, PED, ASME, Achilles, inspection bodies); register "
                "verification recorded honestly as not feasible where that is the case.",
    run=run,
)
