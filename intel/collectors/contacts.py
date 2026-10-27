"""
Collector: Published business contact points (ticket I14, reworked by ticket I95).

Discovers:
  - contact.switchboard (phone number)
  - contact.generic_email (generic published email, e.g. info@, contact@)
  - contact.pec (Certified Electronic Mail)
  - contact.contact_page (URL of the contact page, evidenced by the page's own text)
  - contact.languages (languages the site is written in, each evidenced by a real line of the page)

Rules:
  - Only contact points published by the company itself or verified public registers (G3).
  - Never guess personal email patterns (e.g. firstname.lastname@company.com).
  - Check quotes with quote_in_text() against fetched page text; a quote must SUPPORT the claim it is attached to
    (a switchboard number quotes the number, a contact page quotes a line of that page).
  - Record checked_absent for keys looked for and not found.
  - No company name, address or page of any benchmark company appears in this file.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

from intel.collector import Collector, CollectorContext, CollectorResult
from intel.fetch import Page, normalise_url
from intel.ledger import Evidence, Fact, MIN_QUOTE_CHARS, quote_in_text
from intel.people import (
    extract_emails,
    extract_pec,
    extract_phones,
    is_generic_business_email,
    is_suppressed,
    language_evidence,
)

# Standard contact paths tried ONLY when the homepage offers no contact link (bounded, polite, at most 4)
COMMON_CONTACT_PATHS = [
    "/contatti", "/contact", "/contacts", "/kontakt", "/it/contatti", "/en/contact",
    "/chi-siamo", "/about", "/about-us", "/dove-siamo",
]
CONTACT_PATH_HINTS = ("/contatti", "/contact", "/contacts", "/kontakt", "/dove-siamo", "/recapiti", "/sede")
CONTACT_WORDS = ("contatti", "contattaci", "contact", "kontakt", "telefono", "phone", "e-mail", "email",
                 "indirizzo", "address", "sede", "tel.", "fax")


def _contact_page_quote(text: str) -> Optional[str]:
    """A real line of the contact page that shows it is a contact page (not the first characters of the homepage)."""
    for line in (text or "").splitlines():
        s = line.strip()
        if len(s) >= MIN_QUOTE_CHARS and any(w in s.lower() for w in CONTACT_WORDS):
            s = s[:200]
            if quote_in_text(s, text):
                return s
    return None


def _build_safe_quote(short_target: str, full_text: str, label_hint: str = "") -> str:
    """Ensure quote is at least MIN_QUOTE_CHARS long and occurs verbatim in full_text."""
    if not short_target:
        return ""
    pos = full_text.find(short_target)
    if pos == -1:
        # Try case-insensitive find
        pos = full_text.lower().find(short_target.lower())
        if pos == -1:
            return short_target
        # Adjust short_target to match exact casing in full_text
        short_target = full_text[pos:pos + len(short_target)]

    if len(short_target) >= MIN_QUOTE_CHARS:
        return short_target

    # Expand window to include surrounding context so it reaches MIN_QUOTE_CHARS
    start = max(0, pos - 15)
    end = min(len(full_text), pos + len(short_target) + 15)
    expanded = full_text[start:end].strip()
    if len(expanded) >= MIN_QUOTE_CHARS:
        return expanded
    return short_target


def run(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult()
    website = normalise_url(ctx.company.get("website_url") or "")
    if not website:
        result.errors.append("company has no website_url")
        # Check if database/registry has contact points
        _harvest_registry_contacts(ctx, result)
        return result

    if ctx.fetch is None:
        result.errors.append("no fetcher supplied")
        return result

    seen_urls: Set[str] = set()
    pages_to_visit: List[str] = [website]
    found_keys: Set[str] = set()

    # 1. Fetch homepage
    home_page = ctx.fetch(website)
    result.pages_fetched += 1
    seen_urls.add(normalise_url(home_page.url or website))

    contact_page: Optional[Page] = None     # the fetched contact page: its own text is the evidence
    lang_evidence: Dict[str, Evidence] = {}
    all_links: List[str] = []

    if home_page.ok and home_page.text:
        all_links.extend(home_page.links)
        # Search for contact link
        for link in home_page.links:
            norm_link = normalise_url(link)
            path = urlparse(norm_link).path.lower()
            if any(p in path for p in CONTACT_PATH_HINTS):
                if norm_link not in seen_urls and norm_link not in pages_to_visit:
                    pages_to_visit.append(norm_link)

    # Add standard fallback paths if no contact link was found
    if len(pages_to_visit) == 1:
        for p in COMMON_CONTACT_PATHS[:4]:
            cand = normalise_url(urljoin(website, p))
            if cand not in seen_urls:
                pages_to_visit.append(cand)

    # 2. Visit candidate pages (up to 4 pages)
    for target_url in pages_to_visit[:4]:
        if target_url == website and home_page.ok:
            page = home_page
        else:
            if target_url in seen_urls and target_url != website:
                continue
            seen_urls.add(target_url)
            page = ctx.fetch(target_url)
            result.pages_fetched += 1
            if not page.ok or not page.text:
                continue
            all_links.extend(page.links)

        text = page.text
        # A. Languages: a language counts only when a real line of a fetched page shows the site is written in it
        for lang, line in language_evidence(text):
            if lang not in lang_evidence and quote_in_text(line, text):
                lang_evidence[lang] = Evidence(source_type="company_site", url=page.url, quote=line,
                                               content_hash=page.content_hash)

        # B. The contact page: the fetched page whose PATH says so (never the domain name, never the homepage)
        if contact_page is None and target_url != website and any(
                h in urlparse(page.url).path.lower() for h in CONTACT_PATH_HINTS):
            contact_page = page

        # C. Switchboard Phone
        if "contact.switchboard" not in found_keys:
            phones = extract_phones(text)
            for norm_phone, raw_quote in phones:
                quote = _build_safe_quote(raw_quote, text)
                if quote_in_text(quote, text):
                    result.facts.append(Fact(
                        key="contact.switchboard",
                        value=norm_phone,
                        grade="B",
                        confidence=0.95,
                        verified=True,
                        collector="I14:contacts@2",
                        evidence=[Evidence(
                            source_type="company_site",
                            url=page.url,
                            quote=quote,
                            content_hash=page.content_hash,
                        )]
                    ))
                    found_keys.add("contact.switchboard")
                    break

        # D. Generic Emails
        emails = extract_emails(text)
        for email, raw_quote in emails:
            if not is_generic_business_email(email) and "info@" not in email:
                continue
            fact_key = "contact.generic_email"
            quote = _build_safe_quote(raw_quote, text)
            if quote_in_text(quote, text):
                # Avoid duplicate facts for the same email
                existing = [f for f in result.facts if f.key == fact_key and f.value == email]
                if not existing:
                    result.facts.append(Fact(
                        key=fact_key,
                        value=email,
                        grade="B",
                        confidence=0.95,
                        verified=True,
                        collector="I14:contacts@2",
                        evidence=[Evidence(
                            source_type="company_site",
                            url=page.url,
                            quote=quote,
                            content_hash=page.content_hash,
                        )]
                    ))
                    found_keys.add("contact.generic_email")

        # E. Certified Email (PEC)
        if "contact.pec" not in found_keys:
            pec_tuple = extract_pec(text)
            if pec_tuple:
                pec_email, raw_quote = pec_tuple
                quote = _build_safe_quote(raw_quote, text)
                if quote_in_text(quote, text):
                    result.facts.append(Fact(
                        key="contact.pec",
                        value=pec_email,
                        grade="B",
                        confidence=0.95,
                        verified=True,
                        collector="I14:contacts@2",
                        evidence=[Evidence(
                            source_type="company_site",
                            url=page.url,
                            quote=quote,
                            content_hash=page.content_hash,
                        )]
                    ))
                    found_keys.add("contact.pec")

    # contact.languages: one fact, one evidence per language
    if lang_evidence:
        order = [c for c in ("it", "en", "de", "fr") if c in lang_evidence]
        result.facts.append(Fact(
            key="contact.languages", value=order, grade="B", confidence=0.85, verified=True,
            collector="I14:contacts@2", evidence=[lang_evidence[c] for c in order]))
        found_keys.add("contact.languages")

    # contact.contact_page: the page we actually fetched, quoted from its own text
    if contact_page is not None and "contact.contact_page" not in found_keys:
        quote = _contact_page_quote(contact_page.text)
        if quote:
            result.facts.append(Fact(
                key="contact.contact_page",
                value=contact_page.url,
                grade="B",
                confidence=0.95,
                verified=True,
                collector="I14:contacts@2",
                evidence=[Evidence(
                    source_type="company_site",
                    url=contact_page.url,
                    quote=quote,
                    content_hash=contact_page.content_hash,
                )]
            ))
            found_keys.add("contact.contact_page")

    # Harvest from database/registry if missing on website
    _harvest_registry_contacts(ctx, result, found_keys)

    # 3. Report checked_absent for keys actively looked for
    for required_key in ("contact.switchboard", "contact.generic_email", "contact.pec", "contact.contact_page"):
        if required_key not in found_keys:
            result.checked_absent.append(required_key)

    return result


def _harvest_registry_contacts(
    ctx: CollectorContext,
    result: CollectorResult,
    found_keys: Optional[Set[str]] = None
) -> None:
    """Read verified registry/AIDA contact points if not found on the website."""
    if found_keys is None:
        found_keys = set()
    company = ctx.company

    # Registry PEC
    if "contact.pec" not in found_keys:
        pec = company.get("pec") or company.get("certified_email")
        if pec and not is_suppressed(pec):
            result.facts.append(Fact(
                key="contact.pec",
                value=str(pec).strip().lower(),
                grade="A",
                confidence=0.98,
                verified=True,
                collector="I14:contacts@2",
                evidence=[Evidence(
                    source_type="registry",
                    ref="AIDA / Registro Imprese certified_email",
                )]
            ))
            found_keys.add("contact.pec")

    # Registry Switchboard
    if "contact.switchboard" not in found_keys:
        phone = company.get("phone") or company.get("telephone")
        if phone and not is_suppressed(str(phone)):
            result.facts.append(Fact(
                key="contact.switchboard",
                value=str(phone).strip(),
                grade="A",
                confidence=0.95,
                verified=True,
                collector="I14:contacts@2",
                evidence=[Evidence(
                    source_type="registry",
                    ref="AIDA company profile telephone",
                )]
            ))
            found_keys.add("contact.switchboard")


COLLECTOR = Collector(
    name="contacts",
    ticket="I14",
    produces=("contact.",),
    requires=("web",),
    refresh_days=90,
    cost_hint="cheap",
    description="Published business contact points: switchboard phone, generic emails, PEC, contact page and languages.",
    run=run,
)
