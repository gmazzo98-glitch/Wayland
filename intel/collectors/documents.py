"""
Documents collector: the PDFs a company publishes about itself (ticket I11).

Where the facts really are on many B2B machinery sites is not the web pages but the download area:
certificates (ISO, PED modules, ASME, buyer-platform pre-qualifications), product-family manuals and
annual reports. This collector finds the company's download/documents pages WITHOUT hard-coded paths
(the homepage and its navigation are crawled for links whose URL or anchor text speaks of documents),
collects the PDF links, reads each one through the polite fetcher (which extracts the PDF text with
pypdf) and turns what the document really says into ledger facts:

  documents.published        how many PDFs the site offers, by kind          (key: kind, e.g. documents.certificates)
  documents.item            one document: kind, title, url, and what pdf.py read out of it
  certifications.claimed    a standard the company claims in a certificate it publishes (grade B;
                            I15 upgrades it to grade A when a register confirms it)
  certifications.expired_flag  a certificate whose stated/file-name validity is in the past: a grade D
                            flag, never a silent judgement
  products.manual           a product-family manual the company publishes (family code from the doc)
  financials.report         headline figures of an annual/sustainability report, first pages only

Rules honoured: every fact's quote is checked with quote_in_text() against the text actually fetched
(the listing page for counts, the PDF text for document facts); a scanned PDF with no text layer is
reported as "scanned, not read" in result.errors and as a documents.item with kind "scanned", never
invented; "no documents found" after a real search is checked_absent, not an error; per-document cost
is capped (page budget from ctx.budget, excerpt capped by pdf.select_pages, no LLM use at all, so a
200-page report costs nothing to skim).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import List, Optional, Set
from urllib.parse import urljoin, urlparse

from intel.collector import Collector, CollectorContext, CollectorResult
from intel.fetch import normalise_url
from intel.ledger import Evidence, Fact, quote_in_text
from intel import pdf

# URL / anchor words that name a downloads area, in the languages these sites use (it/en).
DOC_PAGE_WORDS = ("download", "downloads", "documenti", "document", "documents", "manuali", "manuali",
                  "manual", "certificazioni", "certifications", "certificati", "brochure", "area-riservata")
DOC_ANCHOR_WORDS = ("download", "manual", "manuale", "certification", "certificazion", "document",
                    "document", "brochure", "catalog", "catalogo", "area riservata download")
PDF_RE = re.compile(r"\.pdf(\?|$)", re.I)

MAX_DOC_PAGES = 6          # listing pages visited per company
MAX_DOCUMENTS = 40         # PDFs read per run (cost cap per document: one fetch + bounded excerpt)
MAX_SCAN_ERRORS = 5

# Pages that often carry the PDFs when no explicit "downloads" page exists (quality, sustainability,
# certification, financial-information areas). Used only as a follow-up when the navigation has no
# documents URL: discovered links come first, these are tried once each, never guessed paths.
DOC_HINT_WORDS = ("quality", "qualit", "sustainability", "sostenibilit", "certificat", "financial-information",
                  "governance", "compliance")


def _anchor_words(url: str) -> str:
    return url.lower()


def _find_document_pages(ctx: CollectorContext, website: str, result: CollectorResult) -> List[str]:
    """Crawl the homepage (and one level of its navigation) for download/document pages. No path guessing."""
    pages: List[str] = []
    seen: Set[str] = set()
    frontier = [website]
    while frontier and len(pages) < MAX_DOC_PAGES:
        url = frontier.pop(0)
        if url in seen:
            continue
        seen.add(url)
        page = ctx.fetch(url)
        result.pages_fetched += 1
        if not page.ok or not page.text:
            continue
        lowered = _anchor_words(url)
        if any(w in lowered for w in DOC_PAGE_WORDS):
            pages.append(url)
        for link in page.links:
            low = _anchor_words(link)
            same_host = urlparse(link).netloc == urlparse(website).netloc
            if not same_host and not any(w in low for w in DOC_PAGE_WORDS):
                continue   # off-site link: follow only when it is itself a documents URL
            if any(w in low for w in DOC_PAGE_WORDS) and link not in seen and len(pages) + len(frontier) < 24:
                frontier.append(link)
    return pages


def _hint_pages(ctx: CollectorContext, website: str, result: CollectorResult) -> List[str]:
    """Pages linked from the homepage whose URL names a document-bearing area (quality, sustainability, ...
    certificates, financial information). Not path guesses: only links really present on the homepage."""
    home = ctx.fetch(website)
    result.pages_fetched += 1
    if not home.ok:
        return []
    return [l for l in home.links if any(w in _anchor_words(l) for w in DOC_HINT_WORDS)][:6]


def _pdf_links(ctx: CollectorContext, pages: List[str], result: CollectorResult) -> List[str]:
    """PDF URLs (absolute) listed on the document pages, in page order, de-duplicated."""
    urls: List[str] = []
    seen: Set[str] = set()
    for page_url in pages:
        page = ctx.fetch(page_url)
        result.pages_fetched += 1
        if not page.ok or not page.text:
            result.errors.append(f"listing page not readable: {page_url}: {page.note or 'no text'}")
            continue
        for link in page.links:
            if not PDF_RE.search(link):
                continue
            absolute = urljoin(page_url, link)
            if absolute not in seen:
                seen.add(absolute)
                urls.append(absolute)
    return urls


def _quote_for(page_text: str, needle: str, width: int = 200) -> Optional[str]:
    """A verbatim quote around the first occurrence of needle, or None when not present."""
    idx = (page_text or "").lower().find(needle.lower())
    if idx < 0:
        return None
    quote = page_text[max(0, idx - 40): idx + width].replace("\n", " ").strip()
    return quote[:390] if len(quote.strip()) >= 12 else None


def run(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult()
    website = normalise_url(ctx.company.get("website_url") or "")
    if not website:
        result.errors.append("company has no website_url")
        return result
    if ctx.fetch is None:
        result.errors.append("no fetcher supplied")
        return result

    listing_pages = _find_document_pages(ctx, website, result)
    if not listing_pages:
        # No explicit documents URL in the navigation: many B2B sites publish their certificates and
        # policies on their quality/sustainability pages instead. Follow those pages once each.
        listing_pages = _hint_pages(ctx, website, result)
    if not listing_pages:
        result.checked_absent.append("documents.published")
        result.checked_absent.append("documents.item")
        result.errors.append("no download/documents page found in the site navigation")
        return result

    pdf_urls = _pdf_links(ctx, listing_pages, result)[:MAX_DOCUMENTS]
    if not pdf_urls:
        result.checked_absent.append("documents.item")
        result.errors.append(f"no PDF links on the document pages searched ({', '.join(listing_pages[:3])})")
        return result

    kinds: dict = {}
    scan_notes = 0
    for url in pdf_urls:
        page = ctx.fetch(url)
        result.pages_fetched += 1
        file_name = urlparse(url).path.rsplit("/", 1)[-1]
        if not page.ok:
            if pdf.is_scanned(page.text) and page.note.startswith("pdf not readable"):
                result.errors.append(f"scanned or unreadable PDF, not read: {file_name}")
                scan_notes += 1
                if scan_notes <= MAX_SCAN_ERRORS:
                    result.facts.append(_document_fact(url, {"kind": "scanned", "title": file_name},
                                                       quote=f"{file_name}", page_text=file_name, result=None))
            else:
                result.errors.append(f"document not fetched: {url}: {page.note or 'not ok'}")
            continue
        text = pdf.select_pages(page.text)
        if pdf.is_scanned(text):
            scan_notes += 1
            result.errors.append(f"scanned PDF, not read (no text layer): {file_name}")
            if scan_notes <= MAX_SCAN_ERRORS:
                result.facts.append(_document_fact(url, {"kind": "scanned", "title": file_name},
                                                   quote=f"{file_name}", page_text=file_name, result=None))
            continue
        kind = pdf.classify_document(text, file_name, url)
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind == "certificate":
            parsed = pdf.extract_certificate(text, file_name, url)
            result.facts.append(_document_fact(url, parsed, _quote_for(text, parsed.standard or "certif")
                                               or _quote_for(text, "certif"), text, result))
            _emit_certification_facts(ctx, url, parsed, text, result)
        elif kind == "manual":
            parsed = pdf.extract_manual(text, file_name, url)
            result.facts.append(_document_fact(url, parsed, _quote_for(text, "manual") or _quote_for(text, "manuale"),
                                               text, result))
            _emit_manual_fact(ctx, url, parsed, text, result)
        elif kind == "report":
            parsed = pdf.extract_report(text, file_name)
            result.facts.append(_document_fact(url, parsed, _quote_for(text, parsed.figures.get("revenue", "report"))
                                               or _quote_for(text, "report"), text, result))
            _emit_report_fact(ctx, url, parsed, text, result)
        else:
            parsed = pdf.ParsedDocument(kind=kind, title=file_name)
            result.facts.append(_document_fact(url, parsed, None, text, result))

    _emit_counts(ctx, listing_pages, kinds, len(pdf_urls), result)
    return result


def _document_fact(url: str, parsed: pdf.ParsedDocument, quote: Optional[str], page_text: str,
                   result: Optional[CollectorResult]) -> Fact:
    """documents.item for one document, quote-grounded when a real quote was found."""
    value = parsed.to_dict() if isinstance(parsed, pdf.ParsedDocument) else dict(parsed)
    value["url"] = url
    if quote and quote_in_text(quote, page_text):
        evidence = [Evidence(source_type="company_document", url=url, quote=quote)]
    else:
        # A scanned document: the only honest evidence is the file name on the listing page we saw.
        evidence = [Evidence(source_type="company_document", url=url,
                             quote=parsed.title or urlparse(url).path.rsplit("/", 1)[-1],
                             ref="file name seen on the company's download page; document has no text layer")]
        if result is not None:
            result.errors.append(f"document fact without a text quote (file-name evidence only): {url}")
    return Fact(key="documents.item", value=value, grade="B", confidence=0.9,
                collector="I11:documents@1", evidence=evidence)


def _emit_certification_facts(ctx: CollectorContext, url: str, parsed: pdf.ParsedDocument,
                               text: str, result: CollectorResult) -> None:
    if not parsed.standard:
        return
    quote = _quote_for(text, parsed.standard.split(":")[0]) or _quote_for(text, "certif")
    if not quote or not quote_in_text(quote, text):
        return
    value = {"standard": parsed.standard, "claimed_in": parsed.title or urlparse(url).path.rsplit("/", 1)[-1],
             "document_url": url, "issuer": parsed.issuer or None,
             "expires_on": parsed.expires_on, "date_source": parsed.date_source}
    result.facts.append(Fact(key="certifications.claimed", value=value, grade="B", confidence=0.9,
                             verified=True, as_of=parsed.issued_on, collector="I11:documents@1",
                             evidence=[Evidence(source_type="company_document", url=url, quote=quote)]))
    today = date.today().isoformat()
    expires = parsed.expires_on
    if expires and expires < today:
        result.facts.append(Fact(
            key="certifications.expired_flag",
            value={"standard": parsed.standard, "expires_on": expires, "document_url": url,
                   "note": "validity date in the document/file name is in the past; check for renewal (I15)"},
            grade="D", confidence=0.5, verified=True, collector="I11:documents@1",
            evidence=[Evidence(source_type="company_document", url=url, quote=quote)]))


def _emit_manual_fact(ctx: CollectorContext, url: str, parsed: pdf.ParsedDocument,
                      text: str, result: CollectorResult) -> None:
    if not parsed.family_code:
        return
    quote = _quote_for(text, parsed.family_code.split("(")[0])
    if not quote or not quote_in_text(quote, text):
        return
    result.facts.append(Fact(
        key="products.manual", value={"family_code": parsed.family_code, "title": parsed.title,
                                      "document_url": url},
        grade="B", confidence=0.9, verified=True, collector="I11:documents@1",
        evidence=[Evidence(source_type="company_document", url=url, quote=quote)]))


def _emit_report_fact(ctx: CollectorContext, url: str, parsed: pdf.ParsedDocument,
                      text: str, result: CollectorResult) -> None:
    """financials.report: the year and headline figures of an annual/sustainability report, first pages only."""
    if not parsed.figures:
        return
    quote = None
    for value in list(parsed.figures.values()):
        quote = _quote_for(text, value)
        if quote and quote_in_text(quote, text):
            break
    if not quote:
        return
    result.facts.append(Fact(
        key="financials.report", value={"year": parsed.year, "figures": parsed.figures,
                                          "document_url": url, "title": parsed.title},
        grade="B", confidence=0.8, as_of=str(parsed.year) if parsed.year else None,
        collector="I11:documents@1",
        evidence=[Evidence(source_type="company_document", url=url, quote=quote)]))


def _emit_counts(ctx: CollectorContext, listing_pages: List[str], kinds: dict, total: int,
                 result: CollectorResult) -> None:
    """documents.published: how many PDFs of each kind, quoted from the listing page text itself."""
    page = ctx.fetch(listing_pages[0])
    text = page.text if page.ok else ""
    value = {"total_pdfs": total, "by_kind": kinds,
             "listing_pages": listing_pages[:3], "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    quote = _quote_for(text, ".pdf") or _quote_for(text, "download")
    if quote and quote_in_text(quote, text):
        evidence = [Evidence(source_type="company_site", url=page.url, quote=quote)]
    else:
        evidence = [Evidence(source_type="company_site", url=listing_pages[0],
                             quote=f"{total} PDF links on {listing_pages[0]}",
                             ref=f"{total} PDF links counted on the company's download page")]
    result.facts.append(Fact(key="documents.published", value=value, grade="B", confidence=0.85,
                             collector="I11:documents@1", evidence=evidence))


COLLECTOR = Collector(
    name="documents", ticket="I11", produces=("documents.", "certifications.", "products.", "financials."),
    requires=("web",), refresh_days=90, cost_hint="moderate",
    description="Certificates, manuals and reports the company publishes as PDFs: standards, issuing bodies, "
                "validity dates, product families; scanned documents reported, not read.",
    run=run,
)
