"""
Ownership collector: who really controls the company, from the AIDA blob plus corporate-owner web evidence.

Ticket I12. Two layers, in the discipline of the reference collector (site_headers.py):

1. Classification (pure, intel/ownership.py): the AIDA blob already lists shareholders and the control
   chain; the classifier types the ultimate owner (family / family_holding / private_equity /
   industrial_group / listed_group / public_body / unknown / no_data) and sets the controlled-subsidiary
   flag. Emitted as a grade-A `ownership.classification` fact (registry reference, no quote needed).
   The blob reaches the collector through ctx (company dict key `aida_blob`, injected by the runner for
   DB-backed runs) or is simply absent in web-only runs.

2. Corporate-owner web evidence (grade B/C): for companies whose controller is a corporate holder,
   the collector searches the open web (SEC EDGAR full-text for listed parents: Exhibit 21 lists
   subsidiaries by name; press wires and deal press for acquisitions) and the company's own site, and
   stores `ownership.previous_owners` and `ownership.parent_people` with verbatim, quote-grounded
   evidence. Every quote is checked with quote_in_text() against the fetched text before an Evidence
   is built; a fact that cannot be quoted is not emitted.

It never writes to the database and never touches company_service.py. Own-site and press reading are
discovered by search, never hard-coded per company.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

from intel.collector import Collector, CollectorContext, CollectorResult
from intel.fetch import normalise_url
from intel.ledger import Evidence, Fact, MIN_QUOTE_CHARS, quote_in_text
from intel.ownership import classify_ownership, profile_to_facts

COLLECTOR_TAG = "I12:ownership@1"

_FALLBACK_SEARCH = None   # lazily built intel.search.SearchClient for runs with no injected client

DEAL_PAT = re.compile(
    r"([^\n.]{0,160}?\b(?:acquis[ct]|buyout|bought|purchase[ds]?|takeover|compra|ced[ae]|entrato|entrata)\b[^\n.]{0,200}?\.?)",
    re.I,
)
PREV_OWNER_PAT = re.compile(
    r"([^\n.]{0,140}?\b(?:vend[ea]|sells?|sold|disinvest[^\s]*|precedente|previous(?:ly)? owned|through the newco|tramite la newco|"
    r"club deal|capital gain|plusvalenza|reinvest[^\s]*|riinvest[^\s]*|già manager|ex[- ]manager)\b[^\n.]{0,200}?\.?)",
    re.I,
)
EXEC_PAT = re.compile(
    r"([^.\n]{0,140}?\b(?:ceo|chief executive officer|president|chairman|c\.e\.o\.|amministratore delegato|"
    r"managing director|general manager|direttore generale)[^.\n]{0,180}?\.?)",
    re.I,
)


def _entity_in(text: str, legal_name: str, brand: str) -> bool:
    clean = re.sub(r"\b(s\.?p\.?a\.?|s\.?r\.?l\.?|s\.?a\.?s\.?|s\.?n\.?c\.?|gmbh|s\.?m\.?b\.?h\.?)\b", "", legal_name, flags=re.I).strip()
    lowered = text.lower()
    if clean and len(clean) >= 4 and re.search(r"\b" + re.escape(clean.lower()) + r"\b", lowered):
        return True
    if brand and len(brand) >= 4 and re.search(r"\b" + re.escape(brand.lower()) + r"\b", lowered):
        return True
    return False


def _owner_in(text: str, owner_name: str) -> bool:
    return bool(owner_name and re.search(r"\b" + re.escape(owner_name.lower()) + r"\b", text.lower()))


def _quote_from(text: str, pattern: re.Pattern, anchor: str) -> Optional[str]:
    """Longest verbatim sentence around the anchor that really occurs in the text."""
    for m in pattern.finditer(text):
        q = m.group(1).strip()
        if anchor.lower() in q.lower() and len(q) >= MIN_QUOTE_CHARS and quote_in_text(q, text):
            return q
    return None


def _search(ctx: CollectorContext, query: str, n: int = 8) -> List[Any]:
    """Search through the injected client (the orchestrator's path). When no client is supplied
    (scripts/intel_run.py builds none), fall back to the shared intel/search.py SearchClient with its
    default free providers (SEC EDGAR full-text, Google News RSS, GDELT, DuckDuckGo). Never raises."""
    if ctx.search is not None:
        try:
            return ctx.search.search(query, kind="web", n=n)
        except Exception:
            return []
    if ctx.fetch is None:
        return []
    global _FALLBACK_SEARCH
    if _FALLBACK_SEARCH is None:
        try:
            from intel.search import SearchClient
            _FALLBACK_SEARCH = SearchClient()
        except Exception:
            _FALLBACK_SEARCH = False
    if _FALLBACK_SEARCH is False:
        return []
    try:
        return _FALLBACK_SEARCH.search(query, kind="web", n=n)
    except Exception:
        return []


def _fetch(ctx: CollectorContext, url: str, result: CollectorResult):
    if ctx.fetch is None:
        return None
    try:
        page = ctx.fetch(url)
    except Exception as exc:
        result.errors.append(f"fetch {url}: {type(exc).__name__}")
        return None
    result.pages_fetched += 1
    return page if getattr(page, "ok", False) and getattr(page, "text", "") else None


def _press_source_type(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "sec.gov" in host:
        return "public_register"
    if any(d in host for d in ("prnewswire.com", "businesswire.com", "globenewswire.com")):
        return "press"
    return "press"


# Footprint markers: a country named as the site of a branch/subsidiary/affiliate entity.
FOOTPRINT_PAT = re.compile(
    r"([^\n.]{0,140}?\b(?:branch\w*|subsidiar\w+|affiliate\w*|filiale\w*|sede di rappresentanza|"
    r"legal entit\w+|sales office|represented in|present in)\b[^.\n]{0,200}\.?)", re.I)
# Own-site acquisition statements: the company itself says who acquired it (its group chain).
SITE_ACQ_PAT = re.compile(
    r"([^.\n]{0,160}?\b(?:acquired|acquires|ha acquisito|acquisisce|acquisita da|acquisito da|"
    r"part of the group|del gruppo|a majority stake)\b[^.\n]{0,200}\.?)", re.I)
COUNTRY_PAT = re.compile(
    r"\b(france|messico|mexico|germany|germania|spain|spagna|usa|united states|stati uniti|united kingdom|"
    r"regno unito|china|cina|india|abu dhabi|uae|dubai|emirates|brazil|brasile|japan|giappone|"
    r"switzerland|svizzera|austria|belgium|belgio|netherlands|olanda|poland|polonia|romania|"
    r"russia|turchia|turkey|canada|australia|south africa|sud africa|egitto|egypt|arabia)\b", re.I)


def _extract_footprint(text: str) -> List[Dict[str, Any]]:
    """Foreign branches / subsidiaries the company's own pages state."""
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for m in FOOTPRINT_PAT.finditer(text):
        q = re.sub(r"\s+", " ", m.group(1)).strip()
        countries = sorted({c.lower() for c in COUNTRY_PAT.findall(q)})
        if not countries:
            continue
        key = "|".join(countries)
        if key in seen or len(q) < MIN_QUOTE_CHARS:
            continue
        seen.add(key)
        out.append({"kind": "foreign_presence", "countries": countries, "quote": q[:400]})
    return out


def _extract_site_acquisition(text: str, brand: str) -> List[Dict[str, Any]]:
    """The company's own pages naming its acquirer / group parent ("a listed group ... acquired the
    holding ... along with <company>", "parte del gruppo X"). Returns values with the verbatim quote attached."""
    out: List[Dict[str, Any]] = []
    for m in SITE_ACQ_PAT.finditer(text):
        q = re.sub(r"\s+", " ", m.group(1)).strip()
        if len(q) < MIN_QUOTE_CHARS:
            continue
        acq_m = re.search(
            r"\b([A-Z][\w&'\-]+(?:\s+[A-Z][\w&'\-]+){0,4}(?:\s+(?:Inc|Corp|Corporation|SpA|S\.p\.A\.|Srl|GmbH|Group|Holding|PLC|Ltd|Limited))?)"
            r"[^.;:]{0,90}?\s+"
            r"(?:acquired|acquires|ha acquisito|acquisisce)\b", q)
        acquirer = acq_m.group(1).strip() if acq_m else None
        if acquirer and brand.lower() in acquirer.lower():
            acquirer = None
        out.append({"quote": q[:400], "acquirer_named": acquirer,
                    "note": "the company's own page describes its acquisition / group"})
    return out


SITE_OWNER_PAT = re.compile(
    r"([^\n.]{0,100}?\b(?:a\s+(?:wholly[- ]owned\s+)?(?:subsidiary|part|member|company|brand)\s+(?:of|in)\b|"
    r"parte\s+(?:del|della)\s+gruppo|del\s+gruppo\b|un\w*\s+azienda\s+del\s+gruppo\b|"
    r"part\s+of\s+(?:an?\s+\w+\s+){0,2}group\s+(?:named|called)\b|part\s+of\s+the\b|group\s+company\b)"
    r"\s+([A-Z][\w&'À-ù\-]+(?:\s+[A-Z][\w&'À-ù\-]+){0,4}(?:\s+(?:SpA|S\.p\.A\.|Srl|S\.r\.l\.|GmbH|AG|Spa|Inc|Corp|Corporation|Group|Holding|PLC|Ltd|Limited))?)\b(.{0,120}?)\.?)", re.I)


def _extract_site_owner(text: str, brand: str) -> List[Tuple[str, str]]:
    """Own-site statements like 'X: a Y company' / 'parte del gruppo Y'. The owner is a clean
    company name (leading capitalized words only, no sentence fragments, no newlines)."""
    out: List[Tuple[str, str]] = []
    for m in SITE_OWNER_PAT.finditer(text):
        owner = re.sub(r"\s+", " ", m.group(2) or "").strip()
        q = re.sub(r"\s+", " ", m.group(0)).strip()
        if not owner or len(q) < MIN_QUOTE_CHARS:
            continue
        if brand and brand.lower() in owner.lower():
            continue
        # keep only the leading name-like tokens: Capitalized words (+ legal suffixes), stop at the
        # first lowercase word or sentence boundary
        words = owner.split()
        kept: List[str] = []
        for w in words:
            if w[:1].isupper() or w.lower() in ("spa", "s.p.a.", "srl", "s.r.l.", "gmbh", "ag", "inc",
                                                "corp", "corporation", "group", "holding", "plc", "ltd", "limited"):
                kept.append(w)
            else:
                break
        owner = " ".join(kept).strip(" .,;:-")
        if len(owner) < 3 or not any(ch.isalpha() for ch in owner):
            continue
        out.append((owner, q[:400]))
    return out


def _deal_press_evidence(ctx: CollectorContext, legal_name: str, brand: str,
                         known_owners: List[str], result: CollectorResult,
                         company_domains: Optional[List[str]] = None):
    """Search the deal press for an acquirer, previous owners and parent-company executives.
    Returns (acquirer, previous_owner_value_or_None, parent_people, primary_evidence_dict).

    Sources: the shared search client's free providers (news, SEC) and the Italian M&A trade press
    site search (BeBeez) read through the polite fetcher — discovered by company name, never per-company
    hard-coding."""
    acquirer: Optional[str] = None
    acquirer_candidates: List[str] = []   # every buyer named in deal press, in page order
    prev_value: Optional[Dict[str, Any]] = None
    people: List[Dict[str, Any]] = []
    ev: Dict[str, Any] = {}
    queries = [f'"{brand}"', f'"{brand}" acquisizione', f'"{brand}" acquired']
    for query in queries:
        result.queries.append(query)
    candidates: List[str] = []
    seen: Set[str] = set()
    for query in queries:
        for res in _search(ctx, query, 8):
            url = getattr(res, "url", "")
            if not url or url in seen or "sec.gov" in urlparse(url).netloc:
                continue
            if any(d in urlparse(url).netloc for d in ("linkedin.com", "facebook.com", "instagram.com",
                                                      "news.google.com")):
                continue   # news.google.com links are redirect wrappers without the article text
            candidates.append(url)
            seen.add(url)
    # Italian M&A / private-equity trade press: BeBeez site search by brand
    if ctx.fetch is not None and brand:
        page = _fetch(ctx, "https://bebeez.it/?s=" + quote_plus(brand), result)
        if page is not None:
            clean_slug = re.sub(r"[^a-z0-9]+", "-", brand.lower()).strip("-")
            for link in page.links:
                path = urlparse(link).path.lower()
                if "bebeez.it" in link and any(k in path for k in (clean_slug, "private-equity", "ma-e-corporate-finance", "mnt")) \
                        and link not in seen and "bebeez.it/?s=" not in link and "/tag/" not in path and "/category/" not in path:
                    candidates.append(link)
                    seen.add(link)
    for url in candidates[:8]:
        page = _fetch(ctx, url, result)
        if page is None:
            continue
        text = re.sub(r"\s+", " ", page.text or "")   # readability keeps newlines at block edges
        if not _entity_in(text, legal_name, brand):
            continue
        # acquirer: 'X acquires <company>' or '<company> ... acquired by X'. The candidate must be
        # on ONE line (a newline inside the name means the pattern spanned two sentences) and must
        # read like a company, not page furniture ('Società Share on Facebook').
        for sent in re.split(r"(?<=[.!?])\s+", text):
            if not any(v in sent.lower() for v in ("acquis", "compra", "rileva", "acquire", "bought", "buys")):
                continue
            # find every capitalized company-looking name, then check whether a deal verb follows
            # within 80 non-period characters (overlapping: the verb may sit inside another match)
            for nm in re.finditer(r"\b(?:L[’']|l[’']|Un'|un')?([A-Z][\w&'À-ù\-]+(?:\s+(?:dell'|l'|un'|dell’|l’)?[A-Z][\w&'À-ù\-]+){0,4}"
                          r"(?:\s+(?:Inc|Corp|Corporation|SpA|S\.p\.A\.|Srl|GmbH|Group|Holding|PLC|Ltd|Limited|& Co\.?))?)", sent):
                cand = nm.group(1)
                after = sent[nm.end(1):nm.end(1) + 90]
                if not re.match(r"[^.;]{0,80}?\s+"
                                r"(?:ha rilevato|rileva|ha acquistato|acquist\w+|acquires|acquired|ha acquisito|acquisisce|ha comprato|compra|bought|buys|si è aggiudicata?|entra nel capitale)\b", after):
                    continue
                cand = re.sub(r"^americana\s+", "", cand, flags=re.I).strip()
                junk = ("share", "comment", "newsletter", "contatti", "search", "home", "login",
                        "abbonati", "leggi", "sponsor", "advert", "cookie", "subscribe", "bebeez",
                        "facebook", "twitter", "exit", "tags:", "iscriviti")
                if brand.lower() not in cand.lower() and len(cand) >= 4 \
                        and not any(j in cand.lower() for j in junk) \
                        and _entity_in(sent, legal_name, brand):
                    quote = sent.strip()
                    if quote_in_text(quote, page.text or ""):
                        if cand not in acquirer_candidates:
                            acquirer_candidates.append(cand)
                        if acquirer is None:
                            acquirer = cand
                            ev = {"url": page.url, "quote": quote[:400]}
                        break   # one buyer per sentence
        if prev_value is None:
            q = _quote_from(text, PREV_OWNER_PAT, brand)
            if q:
                prev_value = {"evidence_url": page.url, "quote": q,
                              "summary": _sentence_around(text, brand, 240)}
                ev.setdefault("url", page.url)
                ev.setdefault("quote", q)
        for pm in EXEC_PAT.finditer(text):
            q = pm.group(1).strip()
            org = next((on for on in known_owners if _owner_in(q, on)), None)
            if (acquirer and _owner_in(q, acquirer)) or org:
                if len(q) >= MIN_QUOTE_CHARS and quote_in_text(q, text):
                    person = _person_from_quote(q, [acquirer] if acquirer else known_owners)
                    if person and person["name"] not in {p["name"] for p in people}:
                        person["source_url"] = page.url
                        people.append(person)
            if len(people) >= 3:
                break
        if acquirer and prev_value and len(people) >= 1:
            break
    # The acquirer's own site announces the deal with its executives named: one more source.
    # Try every owner the press or the site named (the immediate acquirer's page is the one that
    # quotes its CEO; a later group parent may also have announced it).
    owner_candidates = list(dict.fromkeys(acquirer_candidates + known_owners))
    for owner in owner_candidates[:3]:
        if len(people) >= 3:
            break
        deal_page = _owner_site_deal_page(ctx, owner, brand, result, company_domains)
        if deal_page is None or not _entity_in(deal_page.text or "", brand, brand):
            continue
        text = deal_page.text
        for pm in EXEC_PAT.finditer(re.sub(r"\s+", " ", text)):
            q = pm.group(1).strip()
            # on the owner's own site, the executive IS the owner's: accept sentences that quote
            # a person + role here even when the company name is in another sentence
            if len(q) >= MIN_QUOTE_CHARS and quote_in_text(q, text) \
                    and (_owner_in(q, owner) or _owner_in(deal_page.text or "", owner)):
                person = _person_from_quote(q, [owner])
                if person and person["name"] not in {p["name"] for p in people}:
                    person["source_url"] = deal_page.url
                    people.append(person)
        if prev_value is None:
            q = _quote_from(text, PREV_OWNER_PAT, brand)
            if q:
                prev_value = {"evidence_url": deal_page.url, "quote": q,
                              "summary": _sentence_around(text, brand, 240)}
                ev.setdefault("url", deal_page.url)
                ev.setdefault("quote", q)
        if people and acquirer is None:
            acquirer = owner
            ev.setdefault("url", deal_page.url)
    return acquirer, prev_value, people, ev


def _root_host(host: str) -> str:
    h = (host or "").lower().strip()
    return h[4:] if h.startswith("www.") else h


def _owner_site_deal_page(ctx: CollectorContext, owner: str, brand: str,
                          result: CollectorResult,
                          company_domains: Optional[List[str]] = None) -> Optional[Any]:
    """Find the acquirer's own announcement of the deal: resolve the owner's site from its name
    (first from the external domains the company's own pages link — the acquired company usually
    links its parent — then from the shared search client), and use the site's own search
    (?s=brand, the WordPress default) or scan its news pages for a page mentioning the brand.
    Returns the fetched Page or None. Discovered by name, never hard-coded per company."""
    if ctx.fetch is None or not owner or not brand:
        return None
    owner_domain = None
    for host in (company_domains or []):
        if _brand_in_url_owner(host.lower(), owner):
            owner_domain = f"https://{host.lower()}"
            break
    if owner_domain is None:
        for res in _search(ctx, owner, 5):
            url = getattr(res, "url", "")
            host = urlparse(url).netloc.lower()
            if host and not any(d in host for d in ("linkedin.com", "facebook.com", "instagram.com",
                                                    "news.google.com", "sec.gov", "bebeez.it")):
                if _brand_in_url_owner(host, owner):
                    owner_domain = f"https://{host}"
                    break
    if owner_domain is None:
        return None
    tried: Set[str] = set()
    for probe in (owner_domain + "/?s=" + quote_plus(brand),
                  owner_domain + "/search/?q=" + quote_plus(brand),
                  owner_domain + "/news/", owner_domain + "/press/"):
        if probe in tried:
            continue
        tried.add(probe)
        page = _fetch(ctx, probe, result)
        if page is None:
            continue
        # direct hits: a link whose path mentions the brand
        for link in page.links:
            if _root_host(urlparse(link).netloc) == _root_host(urlparse(owner_domain).netloc) \
                    and brand.lower().replace(" ", "-") in link.lower():
                target = _fetch(ctx, link, result)
                if target is not None and _entity_in(target.text or "", brand, brand):
                    return target
        # the probe page itself may be the deal page (some sites map ?s= straight to a result)
        if _entity_in(page.text or "", brand, brand) and len(page.text or "") > 400 \
                and urlparse(page.url).path.strip("/") and "?s=" not in page.url and "search" not in page.url.lower():
            return page
    return None


def _profile_paths(homepage_page) -> List[str]:
    """Candidate about/company pages from the site's own navigation, including the language variants
    the site itself offers (many multilingual sites keep the ownership statement only on /en/ pages)."""
    keywords = ("company", "about", "chi-siamo", "chi_siamo", "azienda", "storia", "profilo",
                "gruppo", "group", "mission", "who-we-are", "ueber-uns", "uber-uns", "geschichte")
    roots = {urlparse(homepage_page.url if hasattr(homepage_page, "url") else "").scheme + "://" + urlparse(homepage_page.url).netloc} if hasattr(homepage_page, "url") else set()
    for link in homepage_page.links:
        p = urlparse(link)
        if p.path.lower() in ("/en/", "/de/", "/fr/", "/es/", "/it/") and p.netloc:
            roots.add(f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}")
    out = []
    home_host = urlparse(homepage_page.url).netloc.lower()
    for link in homepage_page.links:
        p = urlparse(link)
        if p.netloc.lower() != home_host:
            continue
        if not any(k in p.path.lower() for k in keywords):
            continue
        if link not in out:
            out.append(link)
        for root in roots:
            variant = root.rstrip("/") + p.path
            if variant not in out:
                out.append(variant)
    return out[:8]


def _brand_in_url_owner(host: str, owner: str) -> bool:
    """The owner's own domain usually contains its name's dominant token."""
    tokens = [t for t in re.split(r"[^a-z0-9]+", owner.lower()) if len(t) >= 4 and t not in
              ("corporation", "company", "group", "holding", "society", "international", "industries",
               "industrial", "technologies", "technology", "systems", "gmbh", "spa", "srl")]
    return any(t in host for t in tokens) if tokens else False


def _sentence_with(text: str, needle: str) -> Optional[str]:
    idx = text.find(needle)
    if idx < 0:
        return None
    start = text.rfind(".", 0, idx) + 1
    end = text.find(".", idx)
    end = end + 1 if end > 0 else min(len(text), idx + 300)
    return re.sub(r"\s+", " ", text[start:end]).strip()[:400]


def _edgar_subsidiary_evidence(ctx: CollectorContext, legal_name: str, brand: str,
                               owners: List[str], result: CollectorResult) -> None:
    """SEC EDGAR full-text: a listed parent's Exhibit 21 names its subsidiaries."""
    seen: Set[str] = set()
    for owner in owners:
        for res in _search(ctx, f'"{owner}" "{brand}"', 8):
            url = getattr(res, "url", "")
            if "sec.gov" not in urlparse(url).netloc or url in seen:
                continue
            seen.add(url)
            page = _fetch(ctx, url, result)
            if page is None:
                continue
            if not _entity_in(page.text, legal_name, brand):
                continue
            q = _sub_line_quote(page.text, brand.lower())
            if q:
                result.facts.append(Fact(
                    key="ownership.subsidiary_listing",
                    value={"parent": owner, "listed_in": url,
                           "note": "the parent's filing lists the company among its subsidiaries"},
                    grade="A", confidence=0.95, verified=True, collector=COLLECTOR_TAG,
                    evidence=[Evidence(source_type="public_register", url=page.url, quote=q,
                                       content_hash=page.content_hash)]))
                return


def run(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult()
    company = ctx.company
    legal_name = company.get("legal_name") or ""
    brand = (company.get("brand") or legal_name.split()[0] if legal_name else "") or ""

    # ---------------------------------------------------------------- 1. classification (grade A, offline)
    aida_blob = company.get("aida_blob")
    profile = None
    if isinstance(aida_blob, dict):
        profile = classify_ownership(company, aida_blob)
        value = profile_to_facts(profile, company)
        result.facts.append(Fact(
            key="ownership.classification", value=value, grade="A", confidence=0.95, verified=True,
            collector=COLLECTOR_TAG,
            evidence=[Evidence(source_type="registry", ref="AIDA Raw Exports: shareholders, controlling_shareholders, bvd_independence_class")]))
        if profile.controlled_subsidiary:
            flag_value = {
                "flag": "controlled_subsidiary",
                "value": True,
                "basis": profile.control_basis,
                "ultimate_owner": profile.ultimate_owner,
                "owner_type": profile.owner_type,
                "foreign_controlled": profile.foreign_controlled,
            }
            result.facts.append(Fact(
                key="ownership.controlled_subsidiary", value=flag_value, grade="A", confidence=0.95,
                verified=True, collector=COLLECTOR_TAG,
                evidence=[Evidence(source_type="registry", ref="AIDA Raw Exports: controlling_shareholders")]))
        else:
            result.checked_absent.append("ownership.controlled_subsidiary")

    owner_names: List[str] = []
    if profile:
        for c in profile.control_chain:
            n = (c.get("name") or "").strip()
            if n and n not in owner_names:
                owner_names.append(n)
    owner_name = owner_names[-1] if owner_names else ""

    # ---------------------------------------------------------------- 1b. locations footprint (grade B, own site)
    # The group's foreign footprint (branches, subsidiaries, sales companies) is ownership evidence
    # (where control created entities), produced from the company's own history/locations pages.
    website = normalise_url(company.get("website_url") or "")
    profile_candidates: List[str] = []   # discovered about/company pages (site navigation)
    if website and ctx.fetch is not None:
        fixed = [website, urljoin(website, "/Home/Company"), urljoin(website, "/company"),
                 urljoin(website, "/chi-siamo"), urljoin(website, "/about"), urljoin(website, "/about-us"),
                 urljoin(website, "/storia"), urljoin(website, "/azienda"), urljoin(website, "/profilo"),
                 urljoin(website, "/gruppo"), urljoin(website, "/group"), urljoin(website, "/locations")]
        home = _fetch(ctx, website, result)
        if home is not None:
            profile_candidates = _profile_paths(home)
        for url in list(dict.fromkeys(fixed + profile_candidates)):
            page = _fetch(ctx, url, result)
            if page is None:
                continue
            text = page.text
            if not _entity_in(text, legal_name, brand):
                continue
            emitted = False
            for item in _extract_footprint(text):
                q = item.pop("quote")
                if not quote_in_text(q, text):
                    continue
                result.facts.append(Fact(
                    key="locations.footprint", value=item, grade="B", confidence=0.85, verified=True,
                    collector=COLLECTOR_TAG,
                    evidence=[Evidence(source_type="company_site", url=page.url, quote=q,
                                       content_hash=page.content_hash)]))
                emitted = True
            if emitted:
                break
        if not any(f.key == "locations.footprint" for f in result.facts):
            result.checked_absent.append("locations.footprint")

    # ---------------------------------------------------------------- 1c. web-only control-chain evidence
    # Without the AIDA blob (web-only runs) or on top of it: discover the corporate owner from the
    # company's own site and deal press. A company that calls itself "X: a Y company" or whose deal
    # press names an acquirer is group-controlled, whatever the registry says.
    web_owners: List[str] = list(owner_names)
    site_acquirer: Optional[str] = None
    site_ev_url: Optional[str] = None
    site_ev_quote: Optional[str] = None
    external_domains: List[str] = []
    if ctx.fetch is not None and legal_name:
        brand_clean = _brand_for_search(legal_name)
        if website:
            site_fixed = [website, urljoin(website, "/Home/Company"), urljoin(website, "/company"),
                          urljoin(website, "/chi-siamo"), urljoin(website, "/about")]
            for url in list(dict.fromkeys(site_fixed + profile_candidates)):
                page = _fetch(ctx, url, result)
                if page is None:
                    continue
                if not _entity_in(page.text, legal_name, brand):
                    continue
                site_claims = _extract_site_owner(page.text, brand_clean)
                for owner, q in site_claims:
                    if not quote_in_text(q, page.text):
                        continue
                    result.facts.append(Fact(
                        key="ownership.site_group_claim",
                        value={"source": "own site", "owner": owner,
                               "note": "the company presents itself as part of this group"},
                        grade="B", confidence=0.85, verified=True, collector=COLLECTOR_TAG,
                        evidence=[Evidence(source_type="company_site", url=page.url, quote=q,
                                           content_hash=page.content_hash)]))
                    if owner not in web_owners:
                        web_owners.append(owner)
                for link in page.links:
                    host = urlparse(link).netloc.lower()
                    if host and host not in external_domains \
                            and not any(k in host for k in ("linkedin.", "facebook.", "instagram.", "youtube.", "twitter.", "x.com", "google.")):
                        external_domains.append(host)
                for claim in _extract_site_acquisition(page.text, brand_clean):
                    q = claim.pop("quote")
                    if not quote_in_text(q, page.text):
                        continue
                    value = dict(claim)
                    value["source"] = "own site"
                    result.facts.append(Fact(
                        key="ownership.site_acquisition", value=value, grade="B", confidence=0.9,
                        verified=True, collector=COLLECTOR_TAG,
                        evidence=[Evidence(source_type="company_site", url=page.url, quote=q,
                                           content_hash=page.content_hash)]))
                    acq = claim.get("acquirer_named")
                    if acq and acq not in web_owners:
                        web_owners.append(acq)
                    if acq and site_acquirer is None:
                        site_acquirer = acq
                        site_ev_url = page.url
                        site_ev_quote = q
                if site_claims or any(f.key == "ownership.site_acquisition" for f in result.facts):
                    break
        press_owner, press_prev, press_people, press_ev = _deal_press_evidence(
            ctx, legal_name, brand_clean, web_owners, result, external_domains)
        if press_owner and press_owner not in web_owners:
            web_owners.append(press_owner)
        # A named acquirer in the deal press (or an own-site group claim) means the company is a
        # controlled subsidiary: emit the flag with its basis, from the web, whatever AIDA says.
        if (press_owner or site_acquirer) and not profile:
            flag_basis = press_owner or site_acquirer
            flag_ev_url = (press_ev or {}).get("url") or site_ev_url
            flag_ev_quote = (press_ev or {}).get("quote") or site_ev_quote
            result.facts.append(Fact(
                key="ownership.controlled_subsidiary",
                value={"flag": "controlled subsidiary of", "value": True, "basis": flag_basis,
                       "ultimate_owner": flag_basis,
                       "owner_type": "group-controlled (from web evidence)",
                       "control_basis": f"acquired by {flag_basis}",
                       "evidence_url": flag_ev_url},
                grade="C", confidence=0.85, verified=True, collector=COLLECTOR_TAG,
                evidence=[Evidence(source_type="press", url=flag_ev_url, quote=flag_ev_quote)]))
        elif (press_owner or site_acquirer) and profile and not any(
                f.key == "ownership.controlled_subsidiary" for f in result.facts):
            pass   # the AIDA-backed flag is already emitted in section 1
        if press_owner and not profile and press_ev:
            result.facts.append(Fact(
                key="ownership.classification",
                value={"legal_name": legal_name, "ultimate_owner": press_owner,
                       "owner_type": "group (from web evidence)",
                       "controlled_subsidiary": True,
                       "control_basis": "an acquirer is named in deal press",
                       "evidence_url": press_ev.get("url")},
                grade="C", confidence=0.8, verified=True, collector=COLLECTOR_TAG,
                evidence=[Evidence(source_type="press", url=press_ev.get("url"),
                                   quote=press_ev.get("quote"))]))
        if press_prev:
            result.facts.append(Fact(
                key="ownership.previous_owners", value=press_prev, grade="C", confidence=0.8,
                verified=True, collector=COLLECTOR_TAG,
                evidence=[Evidence(source_type="press", url=press_ev.get("url", ""),
                                   quote=press_prev.get("quote"))]))
        for person in press_people:
            result.facts.append(Fact(
                key="ownership.parent_people", value=person, grade="C", confidence=0.8,
                verified=True, collector=COLLECTOR_TAG,
                evidence=[Evidence(source_type="press", url=person.get("source_url", ""),
                                   quote=person.get("source_quote"))]))
        if web_owners:
            _edgar_subsidiary_evidence(ctx, legal_name, brand_clean, web_owners, result)

    # ---------------------------------------------------------------- 2. AIDA-backed corporate-owner web evidence
    is_corporate = bool(profile and profile.controlled_subsidiary and owner_names)
    checked_press = False
    if is_corporate and ctx.fetch is not None:
        checked_press = True
        seen: Set[str] = set()
        prev_owners: List[Dict[str, Any]] = []
        parent_people: List[Dict[str, Any]] = []

        # 2a. SEC EDGAR full-text: a listed parent's Exhibit 21 names its subsidiaries (grade A/C).
        edgar_results = []
        for on in owner_names:
            edgar_results.extend(_search(ctx, f'"{on}" "{_brand_for_search(legal_name)}"', 8))
        for res in edgar_results:
            url = getattr(res, "url", "")
            if "sec.gov" in urlparse(url).netloc and url not in seen:
                seen.add(url)
                page = _fetch(ctx, url, result)
                if page is None:
                    continue
                text = page.text
                sub_token = _brand_for_search(legal_name).lower()
                if not _entity_in(text, legal_name, sub_token):
                    continue
                q = _quote_from(text, DEAL_PAT, sub_token) or _sub_line_quote(text, sub_token)
                if q:
                    result.facts.append(Fact(
                        key="ownership.subsidiary_listing",
                        value={"parent": owner_name, "listed_in": url,
                               "note": "the parent's filing lists the company among its subsidiaries"},
                        grade="A", confidence=0.95, verified=True, collector=COLLECTOR_TAG,
                        evidence=[Evidence(source_type="public_register", url=page.url, quote=q,
                                           content_hash=page.content_hash)]))

        # 2b. Deal press: previous owners and deal people (grade C), own-site "a <owner> company" (grade B).
        candidates: List[Tuple[str, str]] = []  # (url, kind)
        for query in (f'"{legal_name}" acquisition previous owner',
                      f'"{legal_name}" acquisizione cessione capitale',
                      *[f'"{on}" "{_brand_for_search(legal_name)}"' for on in owner_names]):
            for res in _search(ctx, query, 8):
                url = getattr(res, "url", "")
                if not url or url in seen or "sec.gov" in urlparse(url).netloc:
                    continue
                candidates.append((url, "press"))
                seen.add(url)
            if len(candidates) >= 6:
                break
        website = normalise_url(company.get("website_url") or "")
        if website:
            candidates.insert(0, (website, "company_site"))

        for url, kind in candidates[:8]:
            page = _fetch(ctx, url, result)
            if page is None:
                continue
            text = page.text
            if not (_entity_in(text, legal_name, brand) or any(_owner_in(text, on) for on in owner_names)):
                continue
            source_type = "company_site" if kind == "company_site" else _press_source_type(url)
            grade = "B" if kind == "company_site" else "C"

            if not prev_owners:
                q = _quote_from(text, PREV_OWNER_PAT, _brand_for_search(legal_name))
                if q:
                    prev_owners.append({
                        "note": "deal-press evidence about who held the company before the current owner",
                        "source": page.url,
                    })
                    result.facts.append(Fact(
                        key="ownership.previous_owners", value={
                            "evidence_url": page.url,
                            "summary": _sentence_around(text, owner_name or brand, 240),
                        }, grade=grade, confidence=0.75, verified=True, collector=COLLECTOR_TAG,
                        evidence=[Evidence(source_type=source_type, url=page.url, quote=q,
                                           content_hash=page.content_hash)]))

            for m in EXEC_PAT.finditer(text):
                q = m.group(1).strip()
                q_owner = any(_owner_in(q, on) for on in owner_names)
                if not q_owner:
                    # the sentence may cut just before the organisation ("... CEO of the acquirer.")
                    tail = text[m.end():m.end() + 80].lstrip(" ,:;")
                    if any(_owner_in(tail[:len(on) + 30], on) for on in owner_names):
                        q = (q + " " + tail.split(".")[0]).strip()
                        q_owner = True
                if q_owner and len(q) >= MIN_QUOTE_CHARS and quote_in_text(q, text):
                    person = _person_from_quote(q, owner_names)
                    if person and not any(p["name"] == person["name"] for p in parent_people):
                        parent_people.append(person)
                        result.facts.append(Fact(
                            key="ownership.parent_people", value=person, grade=grade, confidence=0.8,
                            verified=True, collector=COLLECTOR_TAG,
                            evidence=[Evidence(source_type=source_type, url=page.url, quote=q,
                                               content_hash=page.content_hash)]))
                if len(parent_people) >= 3:
                    break
            if len(prev_owners) >= 1 and len(parent_people) >= 3:
                break

    if not profile:
        result.checked_absent.append("ownership.classification")
    if profile and not is_corporate:
        pass   # no web owner evidence to look for: the owner is a person/family, not a corporate parent
    elif is_corporate and not checked_press:
        pass
    if is_corporate and not any(f.key == "ownership.previous_owners" for f in result.facts):
        result.checked_absent.append("ownership.previous_owners")
    if not any(f.key == "ownership.site_group_claim" for f in result.facts) and ctx.fetch is not None:
        result.checked_absent.append("ownership.site_group_claim")
    if not any(f.key == "ownership.site_acquisition" for f in result.facts) and ctx.fetch is not None:
        result.checked_absent.append("ownership.site_acquisition")
    if not any(f.key == "ownership.parent_people" for f in result.facts) and ctx.fetch is not None \
            and (is_corporate or any(f.key == "ownership.site_group_claim" for f in result.facts)):
        result.checked_absent.append("ownership.parent_people")
    return result


def _brand_for_search(legal_name: str) -> str:
    clean = re.sub(r"\b(s\.?p\.?a\.?|s\.?r\.?l\.?|s\.?a\.?s\.?|s\.?n\.?c\.?)\b", "", legal_name, flags=re.I).strip()
    clean = re.sub(r"[\s.,;:!?\-]+$", "", clean).strip()
    return clean or legal_name


def _sentence_around(text: str, anchor: str, width: int) -> str:
    idx = text.lower().find((anchor or " ").lower())
    if idx < 0:
        return ""
    return re.sub(r"\s+", " ", text[max(0, idx - 60):idx + width]).strip()[:400]


def _sub_line_quote(text: str, token: str) -> Optional[str]:
    for line in re.split(r"[\n\r]", text):
        s = line.strip()
        if token in s.lower() and len(s) >= MIN_QUOTE_CHARS:
            q = s[:200]
            if quote_in_text(q, text):
                return q
    return None


def _person_from_quote(quote: str, owner_names: List[str]) -> Optional[Dict[str, Any]]:
    m = re.search(r"\b([A-Z][a-zA-Zà-ù'\-]+)\s+([A-Z][a-zA-Zà-ù'\-]+)\b", quote)
    if not m:
        return None
    name = f"{m.group(1)} {m.group(2)}"
    org = next((on for on in owner_names if _owner_in(quote, on)), "")
    # the name must not be the owner company's own words (e.g. the owner's own name matched as a person)
    if org and _owner_in(name, org):
        return None
    role = "executive"
    rl = quote.lower()
    if "ceo" in rl or "chief executive" in rl:
        role = "CEO"
    elif "president" in rl or "chairman" in rl:
        role = "president"
    elif "amministratore delegato" in rl or re.search(r"\bad\b", rl):
        role = "managing director"
    elif "general manager" in rl or "direttore generale" in rl:
        role = "general manager"
    return {"name": name, "role": role, "organisation": org, "source_quote": quote[:200]}


COLLECTOR = Collector(
    name="ownership",
    run=run,
    ticket="I12",
    produces=("ownership.", "locations."),
    requires=("web",),
    refresh_days=120,
    cost_hint="moderate",
    description="Classifies the AIDA control chain (owner type, controlled-subsidiary flag) and gathers "
                "quote-grounded web evidence about corporate owners (EDGAR subsidiary listings, deal press, "
                "previous owners, parent executives).",
)
