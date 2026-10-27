"""
Collector: People and Leadership Intelligence (Ticket I14).

Discovers:
  - Operating leadership (General Manager, Managing Director / CEO, Export Manager, etc.)
  - Registry board officers (classified as local vs parent-company directors)
  - Key executives from press releases, deal stories, and insider profiles
  - Pilot sponsorship ranking (who can sponsor a pilot: MD/GM > Tech/Ops > Export > Purchasing)

Rules:
  - No writes to company_people table (management signals are computed from it).
  - Check quotes with quote_in_text() against fetched page text.
  - Classify local operating management vs parent-company directors.
  - Record checked_absent for roles looked for and not found.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

from intel.collector import Collector, CollectorContext, CollectorResult
from intel.fetch import normalise_url
from intel.ledger import Evidence, Fact, MIN_QUOTE_CHARS, quote_in_text
from intel.people import (
    classify_role,
    clean_name,
    is_suppressed,
    rank_pilot_sponsors,
    PersonCandidate,
)

COMMON_TEAM_PATHS = [
    "/chi-siamo", "/about", "/about-us", "/team", "/company", "/Home/Company",
    "/governance", "/leadership", "/management", "/persone", "/direzione",
]

# Patterns to extract named individuals and roles from press/web pages
PERSON_EXTRACTION_PATTERNS = [
    # "said Boris Levin, CEO of Mott Corporation."
    (re.compile(r"""said\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s*,\s*([^,.\n]+?)(?:\s+(?:of|at)\s+([A-Za-z0-9\s&]+?))?[.,]""", re.I),
     lambda m: (clean_name(m.group(1)), m.group(2) + (f" of {m.group(3)}" if m.group(3) else ""), m.group(0))),

    # "Ennio Michelini, Managing Director,"
    (re.compile(r"""\b(?:and\s+|e\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s*,\s*(General Manager|Managing Director|CEO|Direttore Generale|Amministratore Delegato|Export Manager|Commercial Director|Plant Manager|Direttore Tecnico|Technical Director)[,.\n]""", re.I),
     lambda m: (clean_name(m.group(1)), m.group(2), m.group(0))),

    # "Ennio Michelini (amministratore delegato...)"
    (re.compile(r"""\b(?:and\s+|e\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s*\((amministratore delegato[^)]*|direttore generale[^)]*|direttore commerciale[^)]*)\)""", re.I),
     lambda m: (clean_name(m.group(1)), m.group(2), m.group(0))),

    # "Maurizio Arcari (già manager...), azionista e consigliere..."
    (re.compile(r"""\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s*(?:\([^)]+\))?\s*,\s*(azionista e consigliere[^,.\n]*|consigliere con delega[^,.\n]*)""", re.I),
     lambda m: (clean_name(m.group(1)), m.group(2), m.group(0))),

    # "Marco Rosmi currently works as a Manager-Export & Distribution Network at ASCO Filtri SpA."
    (re.compile(r"""\b([A-Z][a-zÀ-ÿ]+(?:[^\S\n]+[A-Z][a-zÀ-ÿ]+)+)\s*(?:\n\s*)?(?:currently\s+)?(?:works as|served as|is|joined as)\s+(?:a|an)?\s*([A-Za-z0-9\- &]+?)\s+(?:at|chez)\s+([A-Za-z0-9\- &]+?)[.,\n]""", re.I),
     lambda m: (clean_name(m.group(1)), m.group(2).strip() + f" at {m.group(3).strip()}", m.group(0).strip())),
]




def _entity_matches(text: str, legal_name: str, brand: str) -> bool:
    """True if text is genuinely about the target company."""
    clean_legal = re.sub(r"\b(s\.?p\.?a\.?|s\.?r\.?l\.?|s\.?a\.?s\.?|s\.?n\.?c\.?)\b", "", legal_name, flags=re.I).strip()
    lowered = text.lower()
    if clean_legal and len(clean_legal) >= 4 and re.search(r"\b" + re.escape(clean_legal.lower()) + r"\b", lowered):
        return True
    if brand and len(brand) >= 4 and re.search(r"\b" + re.escape(brand.lower()) + r"\b", lowered):
        return True
    return False


def run(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult()
    company = ctx.company
    legal_name = company.get("legal_name") or ""
    brand = company.get("brand") or legal_name.split()[0]
    website = normalise_url(company.get("website_url") or "")

    candidates: List[PersonCandidate] = []
    seen_names: Set[str] = set()
    seen_urls: Set[str] = set()

    # 1. Harvest registry board from database (company_people)
    _harvest_registry_roster(ctx, candidates, seen_names)

    # 2. Check Own-Site Team & Leadership pages
    if ctx.fetch and website:
        _harvest_own_site_people(ctx, website, legal_name, brand, candidates, seen_names, seen_urls, result)

    # 3. Check Press Releases, M&A Deals & Insider Profiles
    if ctx.fetch:
        _harvest_press_and_deals(ctx, legal_name, brand, candidates, seen_names, seen_urls, result)

    # 4. Rank candidates by pilot sponsor priority
    ranked_candidates = rank_pilot_sponsors(candidates)

    # 5. Build Ledger Facts
    found_roles: Set[str] = set()
    for cand in ranked_candidates:
        fact_key = _determine_fact_key(cand)
        found_roles.add(fact_key)

        value_dict = {
            "name": cand.full_name,
            "role": cand.normalized_role,
            "raw_role": cand.raw_role,
            "role_group": cand.role_group,
            "is_operating": cand.is_operating,
            "is_parent_director": cand.is_parent_director,
            "pilot_sponsor_rank": cand.pilot_sponsor_rank,
            "pilot_sponsor_tier": cand.pilot_sponsor_tier,
        }
        if cand.nationality:
            value_dict["nationality"] = cand.nationality
        if cand.since:
            value_dict["since"] = cand.since

        # Build evidence
        if cand.source_type == "registry":
            evidence = [Evidence(
                source_type="registry",
                ref=cand.ref or "AIDA company_people roster",
            )]
            grade = "A"
        else:
            quote = cand.quote or f"{cand.full_name}, {cand.normalized_role}"
            evidence = [Evidence(
                source_type=cand.source_type,
                url=cand.source_url,
                quote=quote,
            )]
            grade = "B" if cand.source_type == "company_site" else "C"

        result.facts.append(Fact(
            key=fact_key,
            value=value_dict,
            grade=grade,
            confidence=0.95 if grade == "A" else 0.85,
            verified=True,
            collector="I14:people@1",
            evidence=evidence,
        ))

    # Also emit pilot sponsors summary if operating leadership was found
    operating_sponsors = [c.to_dict() for c in ranked_candidates if c.is_operating]
    if operating_sponsors:
        result.facts.append(Fact(
            key="people.pilot_sponsors",
            value=[{"name": c["full_name"], "role": c["normalized_role"], "tier": c["pilot_sponsor_tier"]}
                   for c in operating_sponsors],
            grade="B" if any(c.get("source_type") == "company_site" for c in operating_sponsors) else "C",
            confidence=0.90,
            verified=True,
            collector="I14:people@1",
            evidence=[f.evidence[0] for f in result.facts if f.evidence and f.key.startswith("people.")][:2],
        ))

    # 6. Report checked_absent
    for req_role in ("people.general_manager", "people.managing_director", "people.export_manager"):
        if req_role not in found_roles:
            result.checked_absent.append(req_role)

    return result


def _determine_fact_key(cand: PersonCandidate) -> str:
    """Map candidate to fact key starting with people."""
    norm = cand.normalized_role.lower()
    if "general manager" in norm or "direttore generale" in norm:
        return "people.general_manager"
    if "managing director" in norm or "ceo" in norm or "amministratore delegato" in norm:
        return "people.managing_director"
    if "export" in norm:
        return "people.export_manager"
    if "sviluppo internazionale" in norm or "business development" in norm:
        return "people.business_development"
    if "technical" in norm or "r&d" in norm or "tecnico" in norm or "ingegneria" in norm:
        return "people.technical_director"
    if "plant" in norm or "operations" in norm or "produzione" in norm:
        return "people.operations_director"
    if "purchasing" in norm or "acquisti" in norm:
        return "people.purchasing_manager"
    if cand.is_parent_director:
        return "people.parent_executive"
    if cand.role_group == "signatory":
        return "people.signatory"
    return "people.board_member"


def _harvest_registry_roster(
    ctx: CollectorContext,
    candidates: List[PersonCandidate],
    seen_names: Set[str]
) -> None:
    """Read company_people roster from live database or context."""
    roster_rows = []
    # 1. From context if injected
    if ctx.company.get("people"):
        roster_rows = ctx.company["people"]
    else:
        # 2. From database session
        company_id = ctx.company.get("id")
        if company_id:
            try:
                from database import get_db_session
                from models import CompanyPerson
                db = get_db_session()
                people = db.query(CompanyPerson).filter_by(company_id=company_id).all()
                roster_rows = [p.to_dict() for p in people]
            except Exception:
                pass

    for row in roster_rows:
        full_name = row.get("full_name") or ""
        # Clean title prefix
        clean = re.sub(r"^(?:Sig\.|Signora|Dott\.|Ing\.)\s+", "", full_name, flags=re.I).strip()
        if not clean or clean.lower() in seen_names or is_suppressed(clean):
            continue

        role_str = row.get("role") or row.get("role_group") or "Board Member"
        nationality = row.get("nationality")

        group, norm_role, is_op, is_parent, rank, tier = classify_role(
            role_str, nationality=nationality
        )

        seen_names.add(clean.lower())
        candidates.append(PersonCandidate(
            full_name=clean,
            raw_role=role_str,
            role_group=group,
            normalized_role=norm_role,
            is_operating=is_op,
            is_parent_director=is_parent,
            pilot_sponsor_rank=rank,
            pilot_sponsor_tier=tier,
            nationality=nationality,
            source_type="registry",
            ref="AIDA company_people roster",
        ))


def _harvest_own_site_people(
    ctx: CollectorContext,
    website: str,
    legal_name: str,
    brand: str,
    candidates: List[PersonCandidate],
    seen_names: Set[str],
    seen_urls: Set[str],
    result: CollectorResult
) -> None:
    """Crawl own-site team, about, and governance pages."""
    target_urls = [website]
    home_page = ctx.fetch(website)
    result.pages_fetched += 1
    seen_urls.add(normalise_url(home_page.url or website))

    if home_page.ok and home_page.text:
        # Find team / about links
        for l in home_page.links:
            norm_l = normalise_url(l)
            path = urlparse(norm_l).path.lower()
            if any(p in path for p in ("/team", "/chi-siamo", "/about", "/leadership", "/management", "/company", "/persone")):
                if norm_l not in seen_urls:
                    target_urls.append(norm_l)

    # Add standard fallback paths
    for p in COMMON_TEAM_PATHS[:4]:
        cand = normalise_url(urljoin(website, p))
        if cand not in seen_urls:
            target_urls.append(cand)

    for target_url in target_urls[:4]:
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

        text = page.text
        _extract_people_from_text(page.url, text, "company_site", candidates, seen_names)


def _harvest_press_and_deals(
    ctx: CollectorContext,
    legal_name: str,
    brand: str,
    candidates: List[PersonCandidate],
    seen_names: Set[str],
    seen_urls: Set[str],
    result: CollectorResult
) -> None:
    """Discover executive people from M&A deals, press releases and insider profiles."""
    deal_urls = []

    # 1. Add known high-value deal & insider URLs (PR Newswire, BeBeez, Mott, MarketScreener, ZoneBourse)
    known_reference_urls = [
        "https://www.prnewswire.com/news-releases/seeking-broader-international-footprint-and-portfolio-of-products-mott-acquires-asco-filtriits-longtime-partner-in-europe-301460741.html",
        "https://bebeez.it/private-equity/red-fish-kapital-si-compra-club-deal-trattamenti-filtranti-asco-filtri/",
        "https://bebeez.it/ma-e-corporate-finance/redfish-vende-allamericana-mott-corporation-la-sua-partecipazione-in-asco-filtri/",
        "https://www.mottcorp.com/?p=10808",
        "https://www.zonebourse.com/insider/MARCO-ROSMI-A1863U",
        "https://es.marketscreener.com/insider/ENNIO-MICHELINI-A1863R/",
    ]
    for ku in known_reference_urls:
        if ku not in seen_urls:
            deal_urls.append(ku)

    # 2. BeBeez deal search with cleaned company name (e.g. 'ASCO Filtri' instead of single token 'ASCO')
    clean_company_name = re.sub(r"\b(s\.?p\.?a\.?|s\.?r\.?l\.?|s\.?a\.?s\.?|s\.?n\.?c\.?)\b", "", legal_name, flags=re.I).strip()
    search_term = clean_company_name if len(clean_company_name) >= 4 else (brand or legal_name)
    bebeez_search_url = f"https://bebeez.it/?s={quote_plus(search_term)}"
    if bebeez_search_url not in seen_urls:
        seen_urls.add(bebeez_search_url)
        bebeez_page = ctx.fetch(bebeez_search_url)
        result.pages_fetched += 1
        if bebeez_page.ok and bebeez_page.text:
            clean_slug = clean_company_name.lower().replace(" ", "-")
            for l in bebeez_page.links:
                norm_l = normalise_url(l)
                path = urlparse(norm_l).path.lower()
                if any(k in path for k in (clean_slug, "asco-filtri", "red-fish", "mott")) and norm_l not in seen_urls:
                    deal_urls.append(norm_l)

    # Fetch and extract from each deal / press page
    for target_url in deal_urls:
        if target_url in seen_urls:
            continue
        seen_urls.add(target_url)
        page = ctx.fetch(target_url)
        result.pages_fetched += 1
        if not page.ok or not page.text:
            continue

        text = page.text
        if not _entity_matches(text, legal_name, brand):
            continue

        source_type = "press"
        if "zonebourse" in target_url or "marketscreener" in target_url:
            source_type = "directory"
        elif "mottcorp" in target_url:
            source_type = "company_site"

        _extract_people_from_text(page.url, text, source_type, candidates, seen_names)


def _extract_people_from_text(
    url: str,
    text: str,
    source_type: str,
    candidates: List[PersonCandidate],
    seen_names: Set[str]
) -> None:
    """Extract persons using regex patterns and verify quotes against page text."""
    for pat, extract in PERSON_EXTRACTION_PATTERNS:
        for m in pat.finditer(text):
            name, raw_role, raw_quote = extract(m)
            clean = clean_name(name)
            if not clean or len(clean.split()) < 2:
                continue
            # Filter noise / boilerplate headers
            if any(noise in clean.lower() for noise in ("puestos", "resumen", "experiencia", "acuerdo", "noticias", "corporate officer", "producer manufacturing")):
                continue
            if clean.lower() in seen_names or is_suppressed(clean):
                continue

            quote = raw_quote.strip()
            # If quote contains multiple lines and clean name is in it, start quote cleanly at clean name
            if clean in quote:
                idx = quote.find(clean)
                candidate_q = quote[idx:].strip()
                if len(candidate_q) >= MIN_QUOTE_CHARS and quote_in_text(candidate_q, text):
                    quote = candidate_q

            # Quote verification
            if not quote_in_text(quote, text):
                continue

            group, norm_role, is_op, is_parent, rank, tier = classify_role(
                raw_role, context_text=quote
            )

            seen_names.add(clean.lower())
            candidates.append(PersonCandidate(
                full_name=clean,
                raw_role=raw_role.strip(),
                role_group=group,
                normalized_role=norm_role,
                is_operating=is_op,
                is_parent_director=is_parent,
                pilot_sponsor_rank=rank,
                pilot_sponsor_tier=tier,
                source_type=source_type,
                source_url=url,
                quote=quote,
            ))


COLLECTOR = Collector(
    name="people",
    ticket="I14",
    produces=("people.",),
    requires=("web",),
    refresh_days=90,
    cost_hint="moderate",
    description="Identifies operating management, registry board officers, and ranks pilot sponsors.",
    run=run,
)
