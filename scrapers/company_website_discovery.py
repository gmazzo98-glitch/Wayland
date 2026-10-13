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

# A proper-noun phrase: starts with a capitalized word (tolerating a leading Italian "l'"
# elision, e.g. "l'Italia"), then up to 4 more words that are either directly another
# capitalized word or a short lowercase connector ("per", "di", ...) followed by one -
# this is what lets "Autostrade per l'Italia" extract cleanly as one phrase while a
# surrounding sentence's ordinary lowercase prose never gets swept in. Deliberately
# case-SENSITIVE (capitalization is the actual signal that this is a proper noun, not
# ordinary text) - every regex below that uses this must scope re.IGNORECASE to its
# trigger keywords only, via an inline (?i: ... ) group, never to the whole pattern.
# _APOSTROPHE covers both the ASCII apostrophe and the curly right single quote (U+2019) -
# found live: a real fetched page uses the curly form throughout ("l’Italia",
# "dall’incontro"), which a straight-quote-only class silently fails to match, breaking
# the chain one word early ("Autostrade" alone instead of "Autostrade per l'Italia").
_APOSTROPHE = "'’"
_CONNECTOR = r"(?:per|di|del|della|dei|delle|la|le|lo|the|of|and|e)"
_PROPER_WORD = r"(?:l[" + _APOSTROPHE + r"])?[A-ZÀ-Ý][\w" + _APOSTROPHE + r".À-ÿ-]*"
# [ \t]+ (never \s+) between words within the phrase itself: page_text comes from the live
# page's innerText, which puts each nav/menu item on its own line - a newline-tolerant
# separator chained straight through a site's navigation bar into the captured "name" (found
# live: "EMAG\nContatti\nNewsletter\nMedia Center" instead of just "EMAG"). The trigger
# keyword's OWN separator before the phrase starts can stay \s+; only the inter-word chaining
# inside the phrase needs to stay on one line.
_NAME_PHRASE = _PROPER_WORD + r"(?:[ \t]+(?:" + _CONNECTOR + r"[ \t]+)?" + _PROPER_WORD + r"){0,4}"

# Phrasings a company's own page commonly uses to disclose a parent/group relationship
# (Italian + English - this dataset is overwhelmingly Italian SMEs). Best-effort, not
# exhaustive: see _extract_parent_mentions's docstring for why precision here matters less
# than it looks - a bad extraction almost never survives the DB-matching step that follows.
_PARENT_TRIGGER_RE = re.compile(
    r"(?i:part of(?: the)?|member of(?: the)?|a (?:subsidiary|division|company) of|subsidiary of|"
    r"parte (?:del gruppo|di)|societ[aà] del gruppo|filiale di|controllata (?:da|dal gruppo)|"
    r"facente parte (?:del gruppo|di)|brought together within|confluiscono in)\s+"
    r"(" + _NAME_PHRASE + r")"
)
_GROUP_SUFFIX_RE = re.compile(r"(" + _NAME_PHRASE + r")[ \t]+(?i:Group|Gruppo)\b")
_GRUPPO_PREFIX_RE = re.compile(r"(?i:Gruppo)[ \t]+(" + _NAME_PHRASE + r")")
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|il|la|lo|gli|le)\s+", re.IGNORECASE)


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


_TRAILING_GROUP_RE = re.compile(r"\s+(?:Group|Gruppo)\s*$", re.IGNORECASE)


def _clean_parent_name(raw: str) -> str:
    # The trigger-phrase regex (e.g. "part of the X Group") has no way to know "Group" isn't
    # part of the name itself - it's just another capitalized word to _NAME_PHRASE - so this
    # strips it after the fact rather than teaching the pattern to look ahead for it. Strip
    # trailing sentence punctuation BEFORE the Group/Gruppo removal, not after: _PROPER_WORD's
    # character class allows an embedded "." (for abbreviations like "Co."), so a capture that
    # ends a sentence - "...the X Group." - still has that period attached, which would
    # otherwise block _TRAILING_GROUP_RE's \s*$ anchor from matching at all.
    name = _LEADING_ARTICLE_RE.sub("", raw.strip()).strip(" ,.;:-")
    name = _TRAILING_GROUP_RE.sub("", name)
    return name.strip(" ,.;:-")


def _extract_parent_mentions(page_text: str) -> list:
    """Best-effort scan for a parent/group relationship the page discloses about itself
    (see docs/CRAWLER_ROADMAP.md T24's AMPLIA/EMAG cases). A noisy or over-eager extraction
    here is low-risk, not a correctness problem: every extracted name still has to clear
    _match_parent_company's strict exact-token-set check against a REAL company in our own DB
    before anything gets linked or shown as matched - an unmatched garbage string is at worst
    an unlinked, informational "possible parent mention" in the audit trail."""
    text = page_text or ""
    seen, out = set(), []
    for rx in (_PARENT_TRIGGER_RE, _GROUP_SUFFIX_RE, _GRUPPO_PREFIX_RE):
        for m in rx.finditer(text):
            name = _clean_parent_name(m.group(1))
            key = name.lower()
            if name and len(name) >= 3 and key not in seen:
                seen.add(key)
                out.append(name)
    return out


def _match_parent_company(db: Session, extracted_name: str, exclude_id: str):
    """A confident match requires the EXTRACTED name's significant tokens to equal (not just
    overlap or be a subset of) some other real company's own name tokens - deliberately
    strict, since this result can create a real parent_company_id link, not just a label.
    Found necessary live: this dataset has BOTH 'EMAG SU S.R.L.' and 'EMAG MILANO S.R.L.' as
    separate, real companies - a subset/overlap match on the shared "EMAG" brand token would
    have linked one as the other's "parent" when they are actually unrelated siblings under
    a common (not-in-our-database) group. Exact-set equality correctly rejects that match
    (tokens {"emag"} != {"emag","milano"}) at the cost of also missing a genuinely-correct
    link whenever the extracted name is a shortened form of the real company's full name -
    an honest miss over a wrong link, consistent with this module throughout (see T20)."""
    from models import Company  # local: avoid a module-level circular import with company_service

    tokens = frozenset(_significant_tokens(extracted_name))
    if not tokens:
        return None
    seed = next(iter(tokens))
    candidates = (
        db.query(Company)
        .filter(Company.legal_name.ilike(f"%{seed}%"), Company.id != exclude_id)
        .limit(20)
        .all()
    )
    for cand in candidates:
        cand_tokens = frozenset(_significant_tokens(_strip_legal_form(cand.legal_name or "")))
        if cand_tokens and tokens == cand_tokens:
            return cand
    return None


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


def discover_website(company, db: Session) -> dict:
    """Runs the website-discovery-crawler (search + per-candidate page fetch) and judges the
    results against facts already on file. Returns {"website_url": str|None,
    "confidence": "vat"|"name_and_location"|None, "parent": {...}|None, "candidates": [...]} -
    candidates lists every URL considered and why it was accepted/rejected, for the audit
    trail regardless of outcome.

    A page that discloses a parent/group relationship is handled two different ways depending
    on how strong the OWN-site evidence is (see docs/CRAWLER_ROADMAP.md T24's second
    follow-up): a VAT match is specific enough that the page is still accepted as this
    company's own site even if it ALSO mentions a parent (AMPLIA's own amplia-eng.it happily
    discloses being part of Amplia Infrastructures). A weaker name_and_location match on a
    page that explicitly reads as being about a parent/group, though, is NOT accepted as the
    company's own site - EMAG SU S.R.L.'s case: emag.com/it/contatti/sedi/ is EMAG Group's
    generic "worldwide locations" page, not a dedicated site, and attributing the whole
    international group's digital footprint to one small Italian subsidiary is exactly the
    wrong-answer-worse-than-no-answer risk this module exists to avoid (T20).
    """
    queries = _search_queries(company)
    input_row = {"company_id": company.id, "query1": queries[0],
                 "query2": queries[1] if len(queries) > 1 else ""}
    try:
        rows = run_ts_crawler(CRAWLER_DIR, [input_row], run_timeout=90)
    except CrawlerRunError as e:
        return {"website_url": None, "confidence": None, "parent": None,
                "candidates": [{"error": f"website-discovery-crawler failed: {e}"}]}

    tokens = _significant_tokens(_strip_legal_form(company.legal_name or ""))
    vat_digit_sets = _vat_digit_sets(company)
    candidates = []
    accepted_url, accepted_confidence = None, None
    parent_hits = []  # [{"name": str, "matched_company_id": str|None, "matched_legal_name": str|None}]

    for row in rows_for_company(rows, company.id):
        domain = row.get("domain")
        url = row.get("url")
        page_text = row.get("page_text") or ""
        if row.get("error"):
            candidates.append({"url": url, "domain": domain, "rejected": row["error"]})
            continue
        if domain in BLOCKED_DOMAINS:
            candidates.append({"url": url, "domain": domain, "rejected": "known directory/social domain"})
            continue

        mentions = []
        for name in _extract_parent_mentions(page_text):
            match = _match_parent_company(db, name, company.id)
            hit = {"name": name, "matched_company_id": match.id if match else None,
                   "matched_legal_name": match.legal_name if match else None}
            mentions.append(hit)
            parent_hits.append(hit)

        confidence = _verify(page_text, domain, company, tokens, vat_digit_sets)
        entry = {"url": url, "domain": domain}
        if mentions:
            entry["parent_mentions"] = mentions

        if confidence == "vat":
            entry.update(accepted=True, confidence=confidence)
            if accepted_url is None or confidence == "vat":
                accepted_url, accepted_confidence = url, confidence
        elif confidence and mentions:
            # Weaker evidence AND the page explicitly reads as being about a parent/group -
            # this is "we found evidence of a parent", not "we found this company's own site".
            entry["rejected"] = "parent/group page, not a dedicated site for this company"
        elif confidence:
            entry.update(accepted=True, confidence=confidence)
            if accepted_url is None:
                accepted_url, accepted_confidence = url, confidence
        else:
            entry["rejected"] = "no verified match (name/VAT/location)"
        candidates.append(entry)

    website_url = None
    if accepted_url:
        parsed = urlparse(accepted_url)
        website_url = f"{parsed.scheme}://{parsed.netloc}/"

    parent = None
    if parent_hits:
        matched = [h for h in parent_hits if h["matched_company_id"]]
        best = matched[0] if matched else parent_hits[0]
        parent = best

    return {"website_url": website_url, "confidence": accepted_confidence, "parent": parent,
            "candidates": candidates}


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

    result = discover_website(company, db)
    if result["website_url"]:
        company.website_url = result["website_url"]
    parent = result.get("parent")
    if parent and parent.get("matched_company_id") and not company.parent_company_id:
        company.parent_company_id = parent["matched_company_id"]
    if result["website_url"] or (parent and parent.get("matched_company_id")):
        db.commit()
    result["checked_at"] = datetime.utcnow().isoformat()
    save_crawler_blob(db, company, DATASET_NAME, result)
    return result
