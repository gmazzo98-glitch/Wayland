"""
Finds a company's own official homepage when none is on file, so every website-dependent
Phase 7 crawler (company_website_crawler, digital_maturity_crawler, product_catalog_crawler,
review_crawler, ...) has something to work from instead of silently simulating with "no
website_url on record". Previously there was no discovery step at all — website_url only ever
got populated by whatever the AIDA import happened to carry, or a manual correction.

The case that prompted this (see docs/CRAWLER_ROADMAP.md T24): AMPLIA ENGINEERING & EQUIPMENT
S.R.L. has a real, content-rich homepage (amplia-eng.it) that a human would find in one search,
but website_url was None, so the whole company looked empty. Reverse-engineering what a human
actually does with a search-results page is the point of this module: skim past business
directories/social profiles that rank highly but aren't the company's own site, open the
candidate that looks like a dedicated corporate domain, and confirm it's actually THIS company
(not a similarly-named parent/sibling/unrelated one) before trusting it — never just take the
top hit.

The actual web search + page fetching happens in Scraper/crawlers/website-discovery-crawler (a
Playwright crawler, wrapped the same way as every other Phase 7 source below) rather than a
plain `requests` call from here: DuckDuckGo (and every other free search engine tried —
Mojeek, Startpage, Marginalia — see docs/CRAWLER_ROADMAP.md T24) returns an explicit
automated-traffic block to plain HTTP clients AND to a vanilla headless browser from a shared
dev-sandbox network, so this needs the same real-browser infrastructure the other 10 Phase 7
crawlers already use, run from wherever this actually executes in production (the user's own
machine, or a Crawler Worker's) rather than this repo's dev environment.

Deliberately conservative: a candidate is only accepted when it's independently VERIFIED
against facts already on file (the VAT/registration number, or the legal name together with
the registered province) found on the page itself. An unverified candidate is recorded (so a
human reviewing the Raw Data tab can confirm it manually) but never written to website_url —
a wrong guess here would silently feed every downstream crawler the wrong company's data,
which is worse than finding nothing (see T20's false-attribution lesson).
"""

import re
from datetime import datetime
from urllib.parse import urlparse
from typing import Optional

from sqlalchemy.orm import Session

from scrapers.node_crawler_base import CrawlerRunError, run_ts_crawler, rows_for_company

DATASET_NAME = "crawler_website_discovery"
CRAWLER_DIR = "website-discovery-crawler"

# Domains that carry real, often detailed information ABOUT a company but are never the
# company's OWN site. These consistently outrank a small company's own domain in search
# results (confirmed live for AMPLIA: a business registry/aggregator and the parent group's
# domain both ranked above the company's own amplia-eng.it) and would, if picked, feed every
# downstream Phase 7 crawler a directory/social page instead of the real site - worse than
# finding nothing. Kept in sync with the crawler's own copy
# (website-discovery-crawler/src/search.ts:BLOCKED_DOMAINS, which skips fetching these at all
# as an efficiency shortcut) - this copy is the one that actually decides acceptance. Not
# exhaustive; extend as new false positives turn up.
BLOCKED_DOMAINS = {
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
    "pitchbook.com", "crunchbase.com", "rocketreach.co", "zoominfo.com", "dnb.com",
    "opencorporates.com", "bloomberg.com", "wikipedia.org", "indeed.com",
    "glassdoor.com", "glassdoor.it", "atoka.io", "cribis.com", "infobel.com",
    "kompass.com", "it.kompass.com", "europages.com", "europages.it",
    "ufficiocamerale.it", "aziende.it", "fatturatoitalia.it", "registroaziende.it",
    "informazione-aziende.it", "companyreports.it", "quotidiano.net", "creditsafe.com",
    "icmq.it", "paginegialle.it", "paginebianche.it", "trovaziende.it", "reportaziende.it",
    "ebitda.it", "guidafinanza.it", "google.com", "bing.com", "duckduckgo.com",
    "aziende.money.it",
    # Trade-fair exhibitor directories reprint an exhibitor's own name and city verbatim,
    # which clears the name_and_location tier on their own domain - confirmed live for AMPLIA
    # (saiebologna.it's exhibitor page passed verification; happened to lose to a VAT match on
    # the real site in that run, but would win outright for a company whose VAT doesn't appear
    # on its own homepage). directory-listing-crawler already treats these as directories to
    # search WITHIN, never as a candidate homepage - same rule applies here.
    "saiebologna.it", "mecspe.com",
}

_LEGAL_FORM_RE = re.compile(
    r"\b(S\.?\s?R\.?\s?L\.?|S\.?\s?P\.?\s?A\.?|S\.?\s?A\.?\s?S\.?|S\.?\s?N\.?\s?C\.?|"
    r"SOCIET[AÀ]'?\s+A\s+RESPONSABILIT[AÀ]'?\s+LIMITATA|GMBH & CO\.?\s?KG|GMBH|AG|E\s?K|"
    r"& C\.?|CO\.?\s?KG|LTD|LLC|INC|CORP)\.?\s*$",
    re.IGNORECASE,
)

# Matches a run of 10-11 digits allowing interior spaces/dots/dashes (how VATs are often
# printed: "P.IVA 03146140300", "IT 03146140300", "03.146.140.300") - used to pull every
# VAT-shaped token out of a candidate page so it can be compared, digits-only, against the
# company's own identifiers.
_VAT_TOKEN_RE = re.compile(r"(?:\d[\s.\-]?){10,11}")

_STOPWORDS = {"di", "del", "della", "dei", "delle", "e", "and", "the", "of", "for", "italia", "italy"}


def _strip_legal_form(legal_name: str) -> str:
    name = (legal_name or "").strip()
    prev = None
    while prev != name:
        prev = name
        name = _LEGAL_FORM_RE.sub("", name).strip(" .,-")
    return name or (legal_name or "").strip()


def _significant_tokens(name: str) -> list:
    words = re.findall(r"[A-Za-zÀ-ÿ0-9]+", name.lower())
    return [w for w in words if len(w) >= 3 and w not in _STOPWORDS]


def _vat_digit_sets(company) -> set:
    """Every distinct 10/11-digit identifier already on file for this company, digits only -
    checks BOTH fields because registration_number is corrupted for a large fraction of the
    AIDA-imported rows (see docs/CRAWLER_ROADMAP.md T24) and external_ref_id isn't reliably a
    VAT for every row either (it holds a Codice Fiscale for at least one company checked) -
    so neither field alone can be trusted as "the" VAT, but a page matching either is real
    evidence."""
    out = set()
    for raw in (getattr(company, "registration_number", None), getattr(company, "external_ref_id", None)):
        digits = re.sub(r"\D", "", raw or "")
        if len(digits) in (10, 11):
            out.add(digits)
            if len(digits) == 11 and digits[0] == "0":
                out.add(digits[1:])  # tolerate the same leading-zero loss on the PAGE's own text
    return out


def _search_queries(company) -> list:
    legal_name = company.legal_name or ""
    stripped = _strip_legal_form(legal_name)
    location = getattr(company, "province", None) or getattr(company, "region", None) or company.country or ""
    queries = [f'"{legal_name}" {location}'.strip()]
    if stripped.lower() != legal_name.lower():
        queries.append(f'"{stripped}" {location}'.strip())
    return queries


def _domain_matches_company(domain: str, tokens: list) -> bool:
    """A real company's own site overwhelmingly carries its own name in the domain; a
    directory or an unrelated page essentially never does. Found necessary live (T24
    follow-up, 8-company spot check): a VAT-shaped digit run coincidentally appeared on a
    totally unrelated company's page (mflgroup.com, for FRIDEA S.R.L.), and a business
    directory legitimately printed a real looked-up VAT (impresaitalia.info, for CIDEN
    S.R.L.) - "the VAT appears somewhere on this page" alone was not sufficient, since
    directories exist specifically to publish that information about OTHER companies.
    Requiring the domain to actually echo the company's own name catches both; it would
    reject a company whose branding bears no resemblance to its legal name at all, which is
    the right trade-off here (an honest miss beats a confidently wrong answer - see T20)."""
    if not domain or not tokens:
        return False
    host = domain.split(".")[0].replace("-", "").replace("_", "")
    return any(t in host for t in tokens)


def _verify(page_text: str, domain: str, company, tokens: list, vat_digit_sets: set) -> Optional[str]:
    """Returns the confidence tier ('vat' or 'name_and_location') the page clears, or None.
    Domain correlation is a prerequisite for BOTH tiers, not just name_and_location - a VAT
    match on a domain with zero lexical relation to the company is exactly the false-positive
    pattern found live (see _domain_matches_company)."""
    page_text = page_text or ""
    if not _domain_matches_company(domain, tokens):
        return None
    if vat_digit_sets:
        page_vats = {re.sub(r"[\s.\-]", "", m.group(0)) for m in _VAT_TOKEN_RE.finditer(page_text)}
        if page_vats & vat_digit_sets:
            return "vat"
    if tokens:
        lowered = page_text.lower()
        matched = sum(1 for t in tokens if t in lowered)
        location = (getattr(company, "province", None) or getattr(company, "region", None) or "").lower()
        if matched == len(tokens) and (not location or location in lowered):
            return "name_and_location"
    return None


def discover_website(company) -> dict:
    """Runs the website-discovery-crawler (search + per-candidate page fetch) and judges the
    results against facts already on file. Returns {"website_url": str|None,
    "confidence": "vat"|"name_and_location"|None, "candidates": [...]} - candidates lists every
    URL considered and why it was accepted/rejected, for the audit trail regardless of outcome.
    """
    queries = _search_queries(company)
    input_row = {"company_id": company.id, "query1": queries[0],
                 "query2": queries[1] if len(queries) > 1 else ""}
    try:
        rows = run_ts_crawler(CRAWLER_DIR, [input_row], run_timeout=90)
    except CrawlerRunError as e:
        return {"website_url": None, "confidence": None,
                "candidates": [{"error": f"website-discovery-crawler failed: {e}"}]}

    tokens = _significant_tokens(_strip_legal_form(company.legal_name or ""))
    vat_digit_sets = _vat_digit_sets(company)
    candidates = []
    accepted_url, accepted_confidence = None, None

    for row in rows_for_company(rows, company.id):
        domain = row.get("domain")
        url = row.get("url")
        if row.get("error"):
            candidates.append({"url": url, "domain": domain, "rejected": row["error"]})
            continue
        if domain in BLOCKED_DOMAINS:
            candidates.append({"url": url, "domain": domain, "rejected": "known directory/social domain"})
            continue
        confidence = _verify(row.get("page_text"), domain, company, tokens, vat_digit_sets)
        if confidence:
            candidates.append({"url": url, "domain": domain, "accepted": True, "confidence": confidence})
            if accepted_url is None or confidence == "vat":
                accepted_url, accepted_confidence = url, confidence
        else:
            candidates.append({"url": url, "domain": domain, "rejected": "no verified match (name/VAT/location)"})

    website_url = None
    if accepted_url:
        parsed = urlparse(accepted_url)
        website_url = f"{parsed.scheme}://{parsed.netloc}/"
    return {"website_url": website_url, "confidence": accepted_confidence, "candidates": candidates}


def ensure_website_discovered(company, db: Session) -> Optional[dict]:
    """Called once per company at the start of a Phase 7 pass, before the per-source steps are
    dispatched, so a URL found here is already on `company` by the time Company Website
    Crawler and friends read it (see company_service.run_company_phases /
    sync_company_applicable_sources, both of which call this right after loading `company` and
    before building their step list). No-ops instantly if website_url is already set. Returns
    None when skipped, else the raw discover_website() result (always saved to the audit trail
    via save_crawler_blob, whether or not a URL was found, so "we looked and found nothing
    confident" is visible on the Raw Data tab rather than silent)."""
    if company.website_url:
        return None
    from scrapers.node_crawler_base import save_crawler_blob  # local: avoid a circular import

    result = discover_website(company)
    if result["website_url"]:
        company.website_url = result["website_url"]
        db.commit()
    result["checked_at"] = datetime.utcnow().isoformat()
    save_crawler_blob(db, company, DATASET_NAME, result)
    return result
