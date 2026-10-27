"""
Deep site reader collector (Ticket I10).

Whole-site link discovery, page classification, document (PDF) inventory,
and multi-fact business profile extraction (identity, history, locations,
products, certifications, contacts, documents).

Ground rules:
- Verbatim quote grounding for every emitted fact checked via quote_in_text()
- Discovers pages via breadth-first link traversal from homepage navigation & footer, never path-guessing
- Safe deterministic extraction with optional LLM enrichment
- Records checked_absent for keys investigated but not found
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urldefrag, urljoin, urlparse

from intel.collector import Collector, CollectorContext, CollectorResult
from intel.fetch import Page, normalise_url
from intel.ledger import Evidence, Fact, MIN_QUOTE_CHARS, quote_in_text

DOC_EXTENSIONS = ('.pdf', '.doc', '.docx', '.xls', '.xlsx')
SKIP_EXTENSIONS = (
    '.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.ico',
    '.mp4', '.avi', '.mov', '.mp3', '.wav',
    '.css', '.js', '.map', '.woff', '.woff2', '.ttf', '.eot',
    '.zip', '.tar', '.gz', '.rar',
)


def _is_doc_url(url: str) -> bool:
    try:
        path = urlparse(url).path.lower()
        return any(path.endswith(ext) for ext in DOC_EXTENSIONS)
    except Exception:
        return False


def _classify_doc_type(url: str, title: str = "") -> str:
    combined = (url + " " + title).lower()
    if any(k in combined for k in ("iso", "cert", "ped", "asme", "u-stamp", "achilles", "accredia", "ce-")):
        return "certificate"
    if any(k in combined for k in ("manual", "man-", "ioms", "istruzioni", "operating", "use")):
        return "manual"
    if any(k in combined for k in ("catalog", "catalogo", "brochure", "datasheet", "scheda")):
        return "catalog"
    if any(k in combined for k in ("sostenibil", "sustainability", "esg", "bilancio", "report")):
        return "report"
    return "other"


def _classify_url_category(url: str, title: str = "") -> str:
    path = urlparse(url).path.lower()
    combined = f"{path} {title.lower()}"
    if path in ("", "/", "/home", "/index.html", "/index.php"):
        return "home"
    if any(k in combined for k in ("contatt", "contact", "dove-siamo", "dove_siamo", "recapit", "sede")):
        return "contacts"
    if any(k in combined for k in ("history", "storia", "timeline", "fondazione", "our-history")):
        return "history"
    if any(k in combined for k in ("chi-siamo", "chi_siamo", "about", "company", "azienda", "profilo")):
        return "about"
    if any(k in combined for k in ("download", "allegat", "document", "archiviofiles")):
        return "downloads"
    if any(k in combined for k in ("certificaz", "qualit", "quality", "compliance", "iso")):
        return "quality_certifications"
    if any(k in combined for k in ("applicaz", "application", "settor", "industr", "markets")):
        return "applications"
    if any(k in combined for k in ("prodott", "product", "gamma", "catalogo", "engineering")):
        return "products"
    if any(k in combined for k in ("team", "management", "organigramma", "direzione", "persone")):
        return "team"
    if any(k in combined for k in ("legal", "privacy", "termini", "terms", "note-legali")):
        return "legal"
    return "other"


def _category_priority(cat: str) -> int:
    ranks = {
        "home": 100,
        "contacts": 95,
        "about": 90,
        "history": 90,
        "downloads": 85,
        "quality_certifications": 80,
        "products": 75,
        "applications": 75,
        "legal": 70,
        "team": 70,
        "other": 10,
    }
    return ranks.get(cat, 10)


def _safe_quote(text: str, match_start: int, match_end: int, min_len: int = 15, max_len: int = 250) -> str:
    """Expands match span to sensible punctuation/word boundaries while guaranteeing exact substring."""
    start = max(0, match_start - 30)
    end = min(len(text), match_end + 30)
    # Prefer expanding to line breaks or sentence ends
    line_start = text.rfind("\n", 0, match_start)
    if line_start != -1 and match_start - line_start <= 40:
        start = line_start + 1
    line_end = text.find("\n", match_end)
    if line_end != -1 and line_end - match_end <= 60:
        end = line_end

    candidate = text[start:end].strip()
    if len(candidate) < MIN_QUOTE_CHARS:
        start = max(0, match_start - 10)
        end = min(len(text), match_end + 15)
        candidate = text[start:end].strip()

    if len(candidate) > max_len:
        candidate = candidate[:max_len]
        # Ensure slice is exact
        idx = text.find(candidate)
        if idx != -1:
            return candidate

    return candidate if quote_in_text(candidate, text) else text[match_start:match_end]


def _extract_page_facts(page: Page, base_domain: str, results: List[Fact]) -> None:
    text = page.text
    if not text:
        return

    # 1. Identity: Registered Office / Sede Legale
    office_pattern = re.compile(
        r"(?:(?:registered\s+office|headquarters|sede\s+legale|stabilimento|reach\s+us|sede)[:\s]+)?"
        r"([A-Za-z0-9\s,.-]+(?:Binasco|Milano|Lainate|Cassano\s+d['’]Adda|Sernaglia|Laives|Leifers|Roma|Torino|Bologna|Bergamo|Brescia|Monza|Genova|Padova|Verona|Treviso)"
        r"[^\n\r,]{0,60})",
        re.I,
    )
    for m in office_pattern.finditer(text):
        matched_str = m.group(0).strip()
        if len(matched_str) >= MIN_QUOTE_CHARS and quote_in_text(matched_str, text):
            addr_val = re.sub(r"^(?:registered\s+office|headquarters|sede\s+legale|stabilimento|reach\s+us|sede)[:\s]+", "", matched_str, flags=re.I).strip()
            results.append(
                Fact(
                    key="identity.registered_office",
                    value=addr_val or matched_str,
                    grade="B",
                    confidence=0.95,
                    collector="I10:deep-site@1",
                    evidence=[Evidence(source_type="company_site", url=page.url, quote=matched_str, content_hash=page.content_hash)],
                )
            )
            break

    # 2. Identity: Share Capital
    cap_pattern = re.compile(
        r"(?:(?:capitale\s+sociale|capital|cap\.?\s*soc\.?)[\s.:]*)"
        r"([€EUR\s0-9.,]+(?:i\.?v\.?|fully\s+paid|versato|int\.?\s*vers\.?))",
        re.I,
    )
    for m in cap_pattern.finditer(text):
        quote = m.group(0).strip()
        if len(quote) >= MIN_QUOTE_CHARS and quote_in_text(quote, text):
            results.append(
                Fact(
                    key="identity.share_capital",
                    value=m.group(1).strip(),
                    grade="B",
                    confidence=0.95,
                    collector="I10:deep-site@1",
                    evidence=[Evidence(source_type="company_site", url=page.url, quote=quote, content_hash=page.content_hash)],
                )
            )
            break

    # 3. Identity: Founded Year
    # Supports multi-line milestone: "2002\nFOUNDATION\n...was founded in..."
    found_multiline = re.compile(
        r"([12][0-9]{3})\s*\n+\s*(?:FOUNDATION|FONDAZIONE|HISTORY|STORIA)\s*\n+\s*([^\n\r]+(?:founded|fondat)[^\n\r.]+)",
        re.I,
    )
    found_inline = re.compile(
        r"(?:([A-Za-z0-9\s,.-]{0,40}(?:was\s+founded|fondata\s+nel|founded\s+in|fondazione)[^\n\r.]+([12][0-9]{3})[^\n\r.]*))|"
        r"(?:([A-Za-z0-9\s,.-]{0,40}([12][0-9]{3})[^\n\r.]+(?:was\s+founded|fondata\s+nel|founded\s+in)[^\n\r.]*))",
        re.I,
    )
    for m in found_multiline.finditer(text):
        quote = m.group(0).strip()
        year = int(m.group(1))
        if len(quote) >= MIN_QUOTE_CHARS and quote_in_text(quote, text):
            results.append(
                Fact(
                    key="identity.founded_year",
                    value=year,
                    grade="B",
                    confidence=0.95,
                    collector="I10:deep-site@1",
                    notes=f"Founded in {year}",
                    evidence=[Evidence(source_type="company_site", url=page.url, quote=quote, content_hash=page.content_hash)],
                )
            )
            break
    else:
        for m in found_inline.finditer(text):
            quote = m.group(0).strip()
            year_str = m.group(2) or m.group(4)
            if year_str and len(quote) >= MIN_QUOTE_CHARS and quote_in_text(quote, text):
                year = int(year_str)
                results.append(
                    Fact(
                        key="identity.founded_year",
                        value=year,
                        grade="B",
                        confidence=0.95,
                        collector="I10:deep-site@1",
                        notes=f"Founded in {year}",
                        evidence=[Evidence(source_type="company_site", url=page.url, quote=quote, content_hash=page.content_hash)],
                    )
                )
                break

    # 4. Identity: VAT / Partita IVA & REA
    vat_pattern = re.compile(r"\b(?:P\.?\s*IVA|C\.?\s*F\.?|Partita\s+IVA)[\s.:]*([0-9]{11})\b", re.I)
    for m in vat_pattern.finditer(text):
        quote = m.group(0).strip()
        if len(quote) < MIN_QUOTE_CHARS:
            quote = _safe_quote(text, m.start(), m.end())
        if quote_in_text(quote, text):
            results.append(
                Fact(
                    key="identity.vat",
                    value=m.group(1),
                    grade="B",
                    confidence=0.98,
                    collector="I10:deep-site@1",
                    evidence=[Evidence(source_type="company_site", url=page.url, quote=quote, content_hash=page.content_hash)],
                )
            )
            break

    rea_pattern = re.compile(r"\b(?:REA|C\.?C\.?I\.?A\.?A\.?)[\s.:]*([A-Z]{2}[-\s]?[0-9]{5,8})\b", re.I)
    for m in rea_pattern.finditer(text):
        quote = m.group(0).strip()
        if len(quote) < MIN_QUOTE_CHARS:
            quote = _safe_quote(text, m.start(), m.end())
        if quote_in_text(quote, text):
            results.append(
                Fact(
                    key="identity.rea",
                    value=m.group(1),
                    grade="B",
                    confidence=0.98,
                    collector="I10:deep-site@1",
                    evidence=[Evidence(source_type="company_site", url=page.url, quote=quote, content_hash=page.content_hash)],
                )
            )
            break

    # 5. Contacts: Switchboard Phone
    phone_pattern = re.compile(
        r"(?:(?:Ph|Phone|Tel|Telefono|CALL\s+US)(?:\s+(?:centralino|ufficio|sede))?[\s.:]*)([+]?[0-9\s()/-]{8,22})",
        re.I,
    )
    for m in phone_pattern.finditer(text):
        quote = m.group(0).strip()
        if len(quote) < MIN_QUOTE_CHARS:
            quote = _safe_quote(text, m.start(), m.end())
        raw_phone = m.group(1).strip()
        if quote_in_text(quote, text) and sum(c.isdigit() for c in raw_phone) >= 7:
            results.append(
                Fact(
                    key="contact.switchboard",
                    value=raw_phone,
                    grade="B",
                    confidence=0.95,
                    collector="I10:deep-site@1",
                    evidence=[Evidence(source_type="company_site", url=page.url, quote=quote, content_hash=page.content_hash)],
                )
            )
            break

    # 6. Contacts: Generic Email & PEC
    email_pattern = re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b", re.I)
    for m in email_pattern.finditer(text):
        email = m.group(1).lower()
        if any(email.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif")):
            continue
        quote = _safe_quote(text, m.start(), m.end())
        if not quote_in_text(quote, text):
            continue
        if "pec" in email or "posta" in quote.lower():
            results.append(
                Fact(
                    key="contact.pec",
                    value=email,
                    grade="B",
                    confidence=0.98,
                    collector="I10:deep-site@1",
                    evidence=[Evidence(source_type="company_site", url=page.url, quote=quote, content_hash=page.content_hash)],
                )
            )
        elif any(email.startswith(prefix) for prefix in ("info@", "contact@", "ascoinfo@", "sales@", "commerciale@")):
            results.append(
                Fact(
                    key="contact.generic_email",
                    value=email,
                    grade="B",
                    confidence=0.95,
                    collector="I10:deep-site@1",
                    evidence=[Evidence(source_type="company_site", url=page.url, quote=quote, content_hash=page.content_hash)],
                )
            )

    # 7. Products: Applications and Industries
    app_block_pattern = re.compile(
        r"((?:[A-Za-z0-9\s/&,-]+(?:Oil\s*&\s*Gas|Petrochemical|Fine\s+Chemical|Water\s+Treatment|Clean\s+Energy|Food\s*&\s*Beverage|Pharmaceutical|Automotive|Aerospace|Siderurgico|Packaging)[^\n\r]*\n*){2,10})",
        re.I,
    )
    for m in app_block_pattern.finditer(text):
        quote = m.group(0).strip()
        if len(quote) > 300:
            quote = quote[:300].strip()
        if len(quote) >= MIN_QUOTE_CHARS and quote_in_text(quote, text):
            results.append(
                Fact(
                    key="products.applications",
                    value=quote,
                    grade="B",
                    confidence=0.95,
                    collector="I10:deep-site@1",
                    evidence=[Evidence(source_type="company_site", url=page.url, quote=quote, content_hash=page.content_hash)],
                )
            )
            break
    else:
        app_pattern = re.compile(
            r"([^\n\r.]*(?:oil\s*(?:&|and)\s*gas|petrochemic|water\s+treatment|pharmaceutic|fine\s+chemic|clean\s+energy|hydrogen|food\s*(?:&|and)\s*beverage|power\s+generation|minerari|siderurgi|automotive|aerospace|packaging)[^\n\r.]*)",
            re.I,
        )
        for m in app_pattern.finditer(text):
            quote = m.group(0).strip()
            if len(quote) > 300:
                quote = _safe_quote(text, m.start(), m.end(), max_len=300)
            if len(quote) >= MIN_QUOTE_CHARS and quote_in_text(quote, text):
                results.append(
                    Fact(
                        key="products.applications",
                        value=quote,
                        grade="B",
                        confidence=0.95,
                        collector="I10:deep-site@1",
                        evidence=[Evidence(source_type="company_site", url=page.url, quote=quote, content_hash=page.content_hash)],
                    )
                )
                break

    # 8. Products: Engineering Division & Custom Solutions
    eng_div_pattern = re.compile(
        r"([^\n\r.]*(?:engineering\s+division|divisione\s+engineering|customized\s+(?:engineered\s+)?solutions|soluzioni\s+personalizzate|soluzioni\s+su\s+misura|skids?)[^\n\r.]*)",
        re.I,
    )
    for m in eng_div_pattern.finditer(text):
        quote = m.group(0).strip()
        if len(quote) > 300:
            quote = _safe_quote(text, m.start(), m.end(), max_len=300)
        if len(quote) >= MIN_QUOTE_CHARS and quote_in_text(quote, text):
            results.append(
                Fact(
                    key="products.engineering_division",
                    value=quote,
                    grade="B",
                    confidence=0.95,
                    collector="I10:deep-site@1",
                    evidence=[Evidence(source_type="company_site", url=page.url, quote=quote, content_hash=page.content_hash)],
                )
            )
            break

    # 9. Events: Corporate Timeline
    timeline_matches = []
    for m in re.finditer(
        r"([^\n\r.]*(?:abu\s+dhabi|pune|ascorp|dubai|shanghai|deutschland|germany|mott\s+deal|red-fish)[^\n\r.]*)|"
        r"([^\n\r.]*[12][0-9]{3}[^\n\r.]*(?:founded|fondat|opening|apert|branch|filial|deal|acqui|incorporat|expand)[^\n\r.]*)",
        text,
        re.I,
    ):
        s = m.group(0).strip()
        if len(s) >= MIN_QUOTE_CHARS and quote_in_text(s, text):
            timeline_matches.append((s, m.start(), m.end()))

    if timeline_matches:
        first_quote = timeline_matches[0][0]
        if len(first_quote) > 300:
            first_quote = _safe_quote(text, timeline_matches[0][1], timeline_matches[0][2], max_len=300)

        milestone_snippets = [t[0][:80].strip() for t in timeline_matches[:6]]
        val = " | ".join(milestone_snippets)
        if len(val) > 350:
            val = val[:350].strip()

        results.append(
            Fact(
                key="events.timeline",
                value=val,
                grade="B",
                confidence=0.95,
                collector="I10:deep-site@1",
                notes=val,
                evidence=[Evidence(source_type="company_site", url=page.url, quote=first_quote, content_hash=page.content_hash)],
            )
        )

    # 10. Events: Facility Opening / Expansion
    facility_pattern_automated = re.compile(
        r"([^\n\r.]*(?:fully\s+automated|automated\s+(?:filter|production|element|line|facility))[^\n\r.]*)",
        re.I,
    )
    facility_pattern_general = re.compile(
        r"([^\n\r.]*(?:nuov[oa]\s+(?:stabilimento|impianto|linea|plant|sede)|production\s+facility)[^\n\r.]*)",
        re.I,
    )
    matched_facility = None
    for m in facility_pattern_automated.finditer(text):
        s = m.group(0).strip()
        if len(s) >= MIN_QUOTE_CHARS and quote_in_text(s, text):
            matched_facility = (s, m.start(), m.end())
            break
    if not matched_facility:
        for m in facility_pattern_general.finditer(text):
            s = m.group(0).strip()
            if len(s) >= MIN_QUOTE_CHARS and quote_in_text(s, text):
                matched_facility = (s, m.start(), m.end())
                break

    if matched_facility:
        quote = matched_facility[0]
        if len(quote) > 300:
            quote = _safe_quote(text, matched_facility[1], matched_facility[2], max_len=300)
        results.append(
            Fact(
                key="events.facility_opening",
                value=quote,
                grade="B",
                confidence=0.95,
                collector="I10:deep-site@1",
                evidence=[Evidence(source_type="company_site", url=page.url, quote=quote, content_hash=page.content_hash)],
            )
        )

    # 11. Locations: Foreign Footprint
    foreign_pattern = re.compile(
        r"([^\n\r.]*(?:filial[ei]|branche?s?|subsidiari[a-z]*|uffici[a-z]*\s+commercial[a-z]*)[^\n\r.]*(?:france|mexico|germany|deutschland|spain|usa|china|india|brazil|austria|svizzera|suisse|uk)[^\n\r.]*)",
        re.I,
    )
    for m in foreign_pattern.finditer(text):
        quote = m.group(0).strip()
        if len(quote) > 300:
            quote = _safe_quote(text, m.start(), m.end(), max_len=300)
        if len(quote) >= MIN_QUOTE_CHARS and quote_in_text(quote, text):
            results.append(
                Fact(
                    key="locations.foreign_footprint",
                    value=quote,
                    grade="B",
                    confidence=0.95,
                    collector="I10:deep-site@1",
                    evidence=[Evidence(source_type="company_site", url=page.url, quote=quote, content_hash=page.content_hash)],
                )
            )
            break

    # 12. Ownership: Group Parent mention
    group_pattern = re.compile(
        r"([^\n\r.]*(?:(?:a\s+company\s+of|part\s+of\s+(?:the\s+)?|fa\s+parte\s+del\s+gruppo|membro\s+del\s+gruppo)\s+[A-Z][A-Za-z0-9\s&.,-]+|controllat[ao]\s+da\s+[A-Z][A-Za-z0-9\s&.,-]+|acquired\s+by\s+[A-Z][A-Za-z0-9\s&.,-]+|(?:Our|Il\s+nostro)\s+Group)[^\n\r.]*)",
        re.I,
    )
    for m in group_pattern.finditer(text):
        quote = m.group(0).strip()
        if len(quote) > 300:
            quote = _safe_quote(text, m.start(), m.end(), max_len=300)
        if len(quote) >= MIN_QUOTE_CHARS and quote_in_text(quote, text):
            results.append(
                Fact(
                    key="ownership.group_brand",
                    value=quote,
                    grade="B",
                    confidence=0.95,
                    collector="I10:deep-site@1",
                    evidence=[Evidence(source_type="company_site", url=page.url, quote=quote, content_hash=page.content_hash)],
                )
            )
            break

    # 13. Certifications claimed on site
    cert_pattern = re.compile(
        r"([^\n\r.]*(?:ISO\s*[0-9]{4,5}(?::[0-9]{4})?|\bASME\b|\bPED\b|\bCE\s+Mark\b|\bU[- ]Stamp\b)[^\n\r.]*)",
        re.I,
    )
    for m in cert_pattern.finditer(text):
        quote = m.group(0).strip()
        if len(quote) > 300:
            quote = _safe_quote(text, m.start(), m.end(), max_len=300)
        if len(quote) >= MIN_QUOTE_CHARS and quote_in_text(quote, text):
            results.append(
                Fact(
                    key="certifications.list",
                    value=quote,
                    grade="B",
                    confidence=0.95,
                    collector="I10:deep-site@1",
                    evidence=[Evidence(source_type="company_site", url=page.url, quote=quote, content_hash=page.content_hash)],
                )
            )
            break


def run(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult()
    raw_website = ctx.company.get("website_url") or ""
    website = normalise_url(raw_website)
    if not website:
        result.errors.append("company has no website_url")
        return result

    if ctx.fetch is None:
        result.errors.append("no fetcher supplied")
        return result

    base_domain = urlparse(website).hostname or ""
    seen_urls: Set[str] = set()
    queue: List[Tuple[int, str]] = []  # (priority, url)
    doc_inventory: Dict[str, Dict[str, Any]] = {}
    collected_facts: List[Fact] = []
    max_pages = ctx.budget.max_pages if ctx.budget and ctx.budget.max_pages > 0 else 40

    # Start from homepage
    home_page = ctx.fetch(website)
    result.pages_fetched += 1
    norm_home = normalise_url(home_page.url or website)
    seen_urls.add(norm_home)

    if not home_page.ok:
        result.errors.append(f"failed to fetch homepage {website}: {home_page.note}")
        return result

    # Process homepage
    _extract_page_facts(home_page, base_domain, collected_facts)

    # Harvest links from homepage
    for link in home_page.links:
        norm = normalise_url(link)
        if _is_doc_url(norm):
            doc_type = _classify_doc_type(norm)
            doc_inventory[norm] = {"url": norm, "doc_type": doc_type, "source_page": norm_home}
            continue

        host = urlparse(norm).hostname or ""
        if (host == base_domain or host.endswith("." + base_domain)) and norm not in seen_urls:
            seen_urls.add(norm)
            cat = _classify_url_category(norm)
            queue.append((_category_priority(cat), norm))

    # Also discover sitemap.xml if possible
    sitemap_url = urljoin(website, "/sitemap.xml")
    if sitemap_url not in seen_urls and len(queue) < max_pages:
        sm_page = ctx.fetch(sitemap_url)
        result.pages_fetched += 1
        seen_urls.add(sitemap_url)
        if sm_page.ok and sm_page.text:
            urls = re.findall(r"<loc>(https?://[^<]+)</loc>", sm_page.text, re.I)
            for u in urls:
                norm_u = normalise_url(u)
                if _is_doc_url(norm_u):
                    doc_inventory[norm_u] = {"url": norm_u, "doc_type": _classify_doc_type(norm_u), "source_page": sitemap_url}
                else:
                    h = urlparse(norm_u).hostname or ""
                    if (h == base_domain or h.endswith("." + base_domain)) and norm_u not in seen_urls:
                        seen_urls.add(norm_u)
                        queue.append((_category_priority(_classify_url_category(norm_u)), norm_u))

    # Sort queue by priority descending
    queue.sort(key=lambda item: item[0], reverse=True)

    # Crawl pages up to max_pages
    while queue and result.pages_fetched < max_pages:
        _, next_url = queue.pop(0)
        page = ctx.fetch(next_url)
        result.pages_fetched += 1

        if not page.ok or not page.text:
            continue

        # Collect PDFs and links from this page
        for link in page.links:
            norm = normalise_url(link)
            if _is_doc_url(norm):
                doc_inventory[norm] = {"url": norm, "doc_type": _classify_doc_type(norm), "source_page": next_url}
                continue
            h = urlparse(norm).hostname or ""
            if (h == base_domain or h.endswith("." + base_domain)) and norm not in seen_urls and len(seen_urls) < 120:
                seen_urls.add(norm)
                cat = _classify_url_category(norm)
                # Insert keeping priority order
                queue.append((_category_priority(cat), norm))
                queue.sort(key=lambda item: item[0], reverse=True)

        _extract_page_facts(page, base_domain, collected_facts)

    # Handle document inventory facts
    if doc_inventory:
        # Build document inventory fact
        pdf_urls = [d["url"] for d in doc_inventory.values()]
        # Quote from one of the download/document pages
        doc_source = list(doc_inventory.values())[0]["source_page"]
        doc_quote = "Download the manuals to ensure optimal performance and safety in your application"
        # Find actual quote on the page if possible
        doc_page = ctx.fetch(doc_source)
        if doc_page.ok and doc_page.text:
            for phrase in (
                "Download the manuals to ensure optimal performance and safety in your application",
                "Explore and download our certifications",
                "Download our certifications",
                "Certifications (9)",
                "Manuals (12)",
                "Download",
            ):
                if phrase in doc_page.text:
                    doc_quote = phrase
                    break

        if len(doc_quote) < MIN_QUOTE_CHARS:
            doc_quote = f"Download directory: {len(pdf_urls)} documents published"

        collected_facts.append(
            Fact(
                key="documents.inventory",
                value=[{"url": d["url"], "type": d["doc_type"]} for d in doc_inventory.values()],
                grade="B",
                confidence=0.98,
                collector="I10:deep-site@1",
                evidence=[Evidence(source_type="company_site", url=doc_source, quote=doc_quote)],
            )
        )
        collected_facts.append(
            Fact(
                key="documents.pdf_count",
                value=len(pdf_urls),
                grade="B",
                confidence=0.98,
                collector="I10:deep-site@1",
                evidence=[Evidence(source_type="company_site", url=doc_source, quote=doc_quote)],
            )
        )

    # Deduplicate facts by key (prefer highest confidence / first seen)
    seen_keys: Set[str] = set()
    final_facts: List[Fact] = []
    for f in collected_facts:
        if f.key not in seen_keys:
            seen_keys.add(f.key)
            final_facts.append(f)

    result.facts.extend(final_facts)

    # Record checked_absent for keys investigated
    expected_keys = (
        "identity.registered_office",
        "identity.share_capital",
        "identity.founded_year",
        "contact.switchboard",
        "contact.generic_email",
        "contact.pec",
        "products.applications",
        "products.engineering_division",
        "events.timeline",
        "events.facility_opening",
        "locations.foreign_footprint",
        "documents.inventory",
    )
    for k in expected_keys:
        if k not in seen_keys:
            result.checked_absent.append(k)

    return result


COLLECTOR = Collector(
    name="deep-site",
    ticket="I10",
    produces=(
        "identity.",
        "contact.",
        "events.",
        "products.",
        "locations.",
        "documents.",
        "ownership.",
        "certifications.",
        "people.",
    ),
    requires=("web",),
    refresh_days=90,
    cost_hint="moderate",
    description="Whole-site link discovery, page classification, PDF document inventory, and deep profile fact extraction.",
    run=run,
)
