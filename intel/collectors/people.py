"""
Collector: people and leadership intelligence (ticket I14, reworked by ticket I95).

Finds who runs the company and who could sponsor a pilot, with a source for every person:
  * the registry roster the app already holds (`company_people`, read only, grade A);
  * the company's own team / leadership / legal-notice pages, found from the links of its own homepage (never guessed);
  * press, deal stories and profile pages found by web SEARCH (intel/search.py, query pack from intel/queryplan.py).

Company-agnostic by construction: no company name, person, VAT number or web page is written in this file (the
benchmark company is judged like any other; tests/test_intel_people.py scans the code against every golden file).

Evidence rules (roadmap section 5):
  * every web fact carries a URL and a verbatim quote that THIS code checked with quote_in_text() against the text it
    fetched; a third-party page must also name the company within LOCALITY_CHARS of the person;
  * every web fact goes through the I06 verification gate (intel.verify.verify) and is dropped when it does not pass;
  * an executive whose source says he works for ANOTHER organisation (a parent, an acquirer, a customer) is never filed
    as this company's operating management: `people.parent_executive` only when the ledger's ownership facts confirm
    the organisation, otherwise `people.other_org_executive`;
  * nothing is written to `company_people`; there is no LinkedIn-derived data (decision G2 is open).
"""

from __future__ import annotations

import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote_plus, urlparse

from intel import queryplan
from intel.collector import Collector, CollectorContext, CollectorResult
from intel.fetch import normalise_url
from intel.ledger import Evidence, Fact, MAX_QUOTE_CHARS, MIN_QUOTE_CHARS, quote_in_text
from intel.people import (
    NAME_RE,
    TITLE_ALT,
    PersonCandidate,
    classify_role,
    clean_name,
    company_names,
    employer_is_company,
    is_classified_title,
    is_suppressed,
    looks_like_organization,
    name_in_text,
    plausible_person_name,
    rank_pilot_sponsors,
    split_role_employer,
    strip_legal_form,
)
from intel.verify import verify

COLLECTOR_VERSION = "I14:people@2"

MAX_OWN_SITE_PAGES = 5          # homepage + links the homepage itself offers
MAX_PRESS_PAGES = 10            # pages fetched from search results
MAX_RESULTS_PER_QUERY = 8
LOCALITY_CHARS = 250            # a third-party page must name the company this close to the person
MAX_CANDIDATES_PER_PAGE = 12

# Sources with an internal site search that a deal story about ANY Italian company can be found with (roadmap section 4
# lists the outlet as a readable deal-story source). {q} is the company name. Read through the polite, robots-aware
# fetcher; only result links whose path carries the company's slug are followed. Add a source here only after reading
# docs/intel/SECURITY_AND_SOURCES.md.
SOURCE_SEARCH_ENDPOINTS: Tuple[Tuple[str, str], ...] = (
    ("bebeez", "https://bebeez.it/?s={q}"),
)

# Words in a URL path of the company's own site that lead to people, leadership or legal-notice pages. The collector
# follows links the homepage offers; it does not invent paths.
STRONG_TEAM_HINTS = ("team", "leadership", "management", "governance", "direzione", "persone", "people", "staff",
                     "organigramma", "geschaeftsleitung", "geschäftsleitung", "vertretung", "organi-sociali")
WEAK_TEAM_HINTS = ("chi-siamo", "chisiamo", "about", "azienda", "company", "unternehmen", "ueber-uns", "uber-uns",
                   "impressum", "note-legali", "legal", "profilo", "societa", "who-we-are", "storia", "history")

# ---------------------------------------------------------------------------------------------------------------
# Text patterns: a person written next to a title, in the shapes news, deal stories, team pages and profile pages use.
# Names are matched case-sensitively ("Mario Rossi"); titles case-insensitively. Every pattern yields
# (name, role_text) and the exact span, which becomes the quote.
# ---------------------------------------------------------------------------------------------------------------

_ONE_TITLE = rf"(?i:{TITLE_ALT})"
# "Presidente e Amministratore Delegato", "CEO & Founder": keep every title, classification picks the strongest
_TITLE = rf"{_ONE_TITLE}(?:[^\S\n]+(?i:e|and|&|/)[^\S\n]+{_ONE_TITLE})*"
_ORG_TOKEN = r"[A-Z0-9][\w&.'’\-]*"
_TAIL = rf"(?:[^\S\n]+(?i:of|at|di|presso|bei|von)[^\S\n]+{_ORG_TOKEN}(?:[^\S\n]+[A-Z0-9&][\w&.'’\-]*){{0,4}})?"
_LEAD = r"(?<![\w'’\-])"

# Text extracted from HTML puts inline links on their own lines ("Mario Rossi\n(amministratore delegato ...)",
# "Mario Rossi\n, CEO of ..."), so ONE line break is allowed between a name and its separator; the name itself stays
# on one line.
_NL = r"[^\S\n]*\n?[^\S\n]*"

_PATTERNS: List[Tuple[re.Pattern, Any]] = [
    # "Mario Rossi, Managing Director", "Mario Rossi - CEO of Some Corporation"
    (re.compile(rf"{_LEAD}(?P<name>{NAME_RE}){_NL}[,–—\-]{_NL}(?P<title>{_TITLE}{_TAIL})(?!\w)"),
     lambda m: (m.group("name"), m.group("title"))),
    # "Mario Rossi (amministratore delegato ...)"
    (re.compile(rf"{_LEAD}(?P<name>{NAME_RE}){_NL}\((?P<title>{_TITLE})[^)]{{0,80}}\)"),
     lambda m: (m.group("name"), m.group("title"))),
    # "Mario Rossi (già manager di un altro gruppo), azionista e consigliere con delega": a background in brackets
    (re.compile(rf"{_LEAD}(?P<name>{NAME_RE}){_NL}\([^)]{{0,160}}\){_NL},{_NL}"
                rf"(?:(?i:azionista|socio|shareholder)[^\S\n]+(?i:e|and)[^\S\n]+)?(?P<title>{_TITLE})(?!\w)"),
     lambda m: (m.group("name"), m.group("title"))),
    # "Amministratore Delegato: Mario Rossi"
    (re.compile(rf"{_LEAD}(?P<title>{_TITLE}){_NL}[:\-–—]{_NL}(?P<name>{NAME_RE})(?![\w])"),
     lambda m: (m.group("name"), m.group("title"))),
    # "General Manager Mario Rossi"
    (re.compile(rf"{_LEAD}(?P<title>{_TITLE})[^\S\n]+(?P<name>{NAME_RE})(?![\w])"),
     lambda m: (m.group("name"), m.group("title"))),
    # profile directories: "Mario Rossi currently works as a Sales Manager at Some Company."
    (re.compile(rf"{_LEAD}(?P<name>{NAME_RE})[^\S\n]*\n?[^\S\n]*(?i:currently[^\S\n]+)?(?i:works|worked|serves|served)"
                rf"[^\S\n]+(?i:as)[^\S\n]+(?i:an?[^\S\n]+)?(?P<title>[A-Za-z\-&][A-Za-z\-& ]{{2,70}}?)[^\S\n]+(?i:at|chez)"
                rf"[^\S\n]+(?P<org>{_ORG_TOKEN}(?:[^\S\n]+[A-Z0-9&][\w&.'’\-]*){{0,4}})"),
     lambda m: (m.group("name"), f"{m.group('title').strip()} at {m.group('org').strip()}")),
]

_NAME_LINE = re.compile(rf"^{NAME_RE}$")
_TITLE_LINE = re.compile(rf"^{_ONE_TITLE}(?!\w)")


def _raw_matches(text: str) -> List[Tuple[str, str, str, int]]:
    """[(name, role_text, exact_span, start_index)] from every pattern plus consecutive name / title lines."""
    out: List[Tuple[str, str, str, int]] = []
    for pat, extract in _PATTERNS:
        for m in pat.finditer(text):
            name, role = extract(m)
            out.append((name, role, m.group(0), m.start()))
    # Team pages: a name on one line and its title on the next (or the reverse)
    lines = [ln.strip() for ln in text.splitlines()]
    cursor = 0
    positions: List[int] = []
    for ln in lines:
        idx = text.find(ln, cursor) if ln else cursor
        positions.append(idx if idx >= 0 else cursor)
        cursor = max(cursor, (idx if idx >= 0 else cursor) + len(ln))
    # A pair CONSUMES both lines, so an interleaved "Name, Title, Name, Title" layout is read as two pairs and never as
    # (second name, first title).
    i = 0
    while i < len(lines) - 1:
        a, b = lines[i], lines[i + 1]
        if a and b and len(a) <= 60 and len(b) <= 90:
            if _NAME_LINE.match(a) and _TITLE_LINE.match(b):
                out.append((a, b, f"{a}\n{b}", positions[i]))
                i += 2
                continue
            if _TITLE_LINE.match(a) and _NAME_LINE.match(b):
                out.append((b, a, f"{a}\n{b}", positions[i]))
                i += 2
                continue
        i += 1
    return out


def _quote_for(span: str, text: str) -> Optional[str]:
    quote = re.sub(r"[ \t]+", " ", span).strip()
    if len(quote) < MIN_QUOTE_CHARS:
        return None
    quote = quote[:MAX_QUOTE_CHARS]
    return quote if quote_in_text(quote, text) else None


def _host(url: str) -> str:
    try:
        host = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _same_site(url: str, own_host: str) -> bool:
    host = _host(url)
    return bool(own_host) and (host == own_host or host.endswith("." + own_host))


def _owner_names(ctx: CollectorContext) -> List[str]:
    """Owner names the ledger already holds (from the ownership collector), used to confirm a parent executive."""
    owners: List[str] = []
    try:
        for key in ("ownership.ultimate_owner", "ownership.direct_owner"):
            for fact in ctx.ledger.present(key):
                val = fact.value
                owners.append(str(val.get("name") if isinstance(val, dict) else val))
    except Exception:
        pass
    return [strip_legal_form(o).lower() for o in owners if o and len(strip_legal_form(o)) >= 4]


def _extract_candidates(
    text: str,
    url: str,
    source_type: str,
    names: Sequence[str],
    own_site: bool,
    owners: Sequence[str],
) -> List[Tuple[PersonCandidate, str]]:
    """People found in one fetched text. Returns (candidate, page_text) pairs; the quote is already grounded."""
    found: List[Tuple[PersonCandidate, str]] = []
    seen: Set[Tuple[str, str]] = set()
    company_tokens = {tok.lower() for n in names for tok in re.split(r"\W+", n) if tok}
    for name, role_text, span, start in _raw_matches(text):
        name = clean_name(name)
        if not plausible_person_name(name):
            continue
        # The company's own name read as a person ("Rossi Meccanica, General Manager") is not a person. A person who
        # merely shares a surname with a family firm ("Luca Rossi") is, so only names made ONLY of company words go.
        name_tokens = {tok.lower() for tok in re.split(r"\W+", name) if tok}
        if name_tokens and name_tokens <= company_tokens:
            continue
        role, employer = split_role_employer(role_text)
        if not is_classified_title(role):
            continue
        if not own_site:
            window = text[max(0, start - LOCALITY_CHARS): start + len(span) + LOCALITY_CHARS]
            if not name_in_text(names, window):
                continue
        quote = _quote_for(span, text)
        if not quote:
            continue
        key = (name.lower(), role.lower())
        if key in seen:
            continue
        seen.add(key)

        affiliation = "company"
        if employer and not employer_is_company(employer, names):
            emp = strip_legal_form(employer).lower()
            affiliation = "parent" if any(emp in o or o in emp for o in owners) else "other_org"
        group, norm, is_op, is_parent, rank, tier = classify_role(role, context_text=span)
        if affiliation != "company":
            is_op = False
            is_parent = affiliation == "parent"
            group, rank = ("board_parent", 5) if is_parent else ("other", 6)
            tier = f"Tier {rank}: executive of another organisation" if not is_parent else \
                "Tier 5: Governance & Parent Board (Holding / Director / Signatory)"
        found.append((PersonCandidate(
            full_name=name, raw_role=role_text.strip(), role_group=group, normalized_role=norm,
            is_operating=is_op, is_parent_director=is_parent, pilot_sponsor_rank=rank, pilot_sponsor_tier=tier,
            source_type=source_type, source_url=url, quote=quote,
            employer=employer if affiliation != "company" else None, affiliation=affiliation,
        ), text))
        if len(found) >= MAX_CANDIDATES_PER_PAGE:
            break
    return found


# ---------------------------------------------------------------------------------------------------------------
# Fact keys
# ---------------------------------------------------------------------------------------------------------------

_KEY_BY_ROLE = {
    "General Manager": "people.general_manager",
    "Managing Director / CEO": "people.managing_director",
    "Founder / Owner": "people.founder",
    "Executive Director": "people.executive_director",
    "Operations / Plant Manager": "people.operations_director",
    "Production / Operations Manager": "people.operations_director",
    "Technical / R&D Director": "people.technical_director",
    "Export Manager": "people.export_manager",
    "Commercial Director": "people.commercial_director",
    "Business Development Director": "people.business_development",
    "Purchasing / Supply Chain Manager": "people.purchasing_manager",
    "Functional Manager": "people.functional_manager",
    "Procuratore / Signatory": "people.signatory",
}


def _fact_key(c: PersonCandidate) -> str:
    if c.entity_type == "organization":
        return "people.advisor_firm"
    if c.affiliation == "parent":
        return "people.parent_executive"
    if c.affiliation == "other_org":
        return "people.other_org_executive"
    if c.normalized_role in _KEY_BY_ROLE:
        return _KEY_BY_ROLE[c.normalized_role]
    # a role the vocabulary does not know is not a board seat: keep it visible without claiming one
    return "people.board_member" if c.role_group == "board_parent" else "people.other_officer"


def _value(c: PersonCandidate) -> Dict[str, Any]:
    value: Dict[str, Any] = {
        "name": c.full_name,
        "role": c.normalized_role,
        "raw_role": c.raw_role,
        "role_group": c.role_group,
        "is_operating": c.is_operating,
        "is_parent_director": c.is_parent_director,
        "pilot_sponsor_rank": c.pilot_sponsor_rank,
        "pilot_sponsor_tier": c.pilot_sponsor_tier,
    }
    if c.entity_type != "person":
        value["entity_type"] = c.entity_type
    if c.employer:
        value["employer"] = c.employer
    if c.affiliation != "company":
        value["affiliation"] = c.affiliation
    if c.nationality:
        value["nationality"] = c.nationality
        if c.is_parent_director and c.source_type == "registry":
            value["parent_basis"] = "foreign nationality on the registry board (heuristic, not confirmed by an ownership source)"
    if c.since:
        value["since"] = c.since
    return value


# ---------------------------------------------------------------------------------------------------------------
# Registry roster (read only)
# ---------------------------------------------------------------------------------------------------------------

def _load_registry_rows(ctx: CollectorContext, result: CollectorResult) -> List[Dict[str, Any]]:
    """Rows of the registry roster: injected by the orchestrator (`company['people']`), or read with a SELECT.
    A failure is reported in result.errors, never swallowed."""
    injected = ctx.company.get("people")
    if injected:
        return [r for r in injected if isinstance(r, dict)]
    company_id = ctx.company.get("id")
    if not company_id:
        result.errors.append("registry roster not read: no company id and no injected roster")
        return []
    db = None
    try:
        from database import get_db_session
        from models import CompanyPerson
        db = get_db_session()
        return [p.to_dict() for p in db.query(CompanyPerson).filter_by(company_id=company_id).all()]
    except Exception as exc:   # report which kind of failure, not its text (it may carry connection details)
        result.errors.append(f"registry roster not read: {type(exc).__name__}")
        return []
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


_PLACEHOLDER_WORDS = re.compile(
    r"\b(available|none|n/a|no data|filter|selected|not available|nessun\w*|non disponibil\w*|nessuna)\b", re.I)


def _is_placeholder_name(name: str) -> bool:
    """The imported registry sometimes holds a sentence where a name should be ("... none related to the selected
    filter"); a real name is short and has none of those words."""
    return len(name.split()) > 7 or bool(_PLACEHOLDER_WORDS.search(name))


def _is_former(row: Dict[str, Any]) -> bool:
    """A registry officer who has left. In the imported registry data `resignation_date` can hold the END OF THE
    CURRENT TERM (a future date), so a date only counts when it is already past; otherwise the status column decides."""
    status = str(row.get("current_or_former") or "").strip().lower()
    if status.startswith(("former", "cessat", "ex ", "ex-")):
        return True
    if status.startswith(("current", "in carica", "attiv")):
        return False
    when = row.get("resignation_date")
    if not when:
        return False
    try:
        if isinstance(when, str):
            when = datetime.fromisoformat(when[:19])
        if getattr(when, "tzinfo", None) is not None:
            when = when.replace(tzinfo=None)
        return when < datetime.now(timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError):
        return False


def _registry_candidates(rows: Sequence[Dict[str, Any]], result: CollectorResult) -> List[PersonCandidate]:
    out: List[PersonCandidate] = []
    seen: Set[Tuple[str, str]] = set()
    former = 0
    for row in rows:
        full_name = re.sub(r"^(?:Sig\.|Signora|Dott\.|Ing\.)\s+", "", str(row.get("full_name") or ""), flags=re.I).strip()
        if not full_name or is_suppressed(full_name) or _is_placeholder_name(full_name):
            continue
        if _is_former(row):
            former += 1
            continue
        role_str = str(row.get("role") or row.get("role_group") or "Board Member")
        nationality = row.get("nationality")
        group, norm, is_op, is_parent, rank, tier = classify_role(role_str, nationality=nationality)
        key = (full_name.lower(), norm.lower())
        if key in seen:
            continue
        seen.add(key)
        appointed = row.get("appointment_date")
        out.append(PersonCandidate(
            full_name=full_name, raw_role=role_str, role_group=group, normalized_role=norm, is_operating=is_op,
            is_parent_director=is_parent, pilot_sponsor_rank=rank, pilot_sponsor_tier=tier, nationality=nationality,
            since=str(appointed)[:10] if appointed else None, source_type="registry", ref="AIDA company_people roster",
            entity_type="organization" if looks_like_organization(full_name) else "person",
        ))
    if former:
        result.errors.append(f"{former} former officer(s) in the registry roster were not listed")
    return out


# ---------------------------------------------------------------------------------------------------------------
# Own site and search discovery
# ---------------------------------------------------------------------------------------------------------------

def _team_links(home_links: Sequence[str], own_host: str, seen_urls: Set[str]) -> List[str]:
    scored: List[Tuple[int, int, str]] = []
    for order, link in enumerate(home_links):
        norm = normalise_url(link)
        if not norm or norm in seen_urls or not _same_site(norm, own_host):
            continue
        path = urlparse(norm).path.lower()
        if any(path.endswith(ext) for ext in (".pdf", ".jpg", ".png", ".zip", ".doc", ".docx", ".xls", ".xlsx")):
            continue
        if any(h in path for h in STRONG_TEAM_HINTS):
            scored.append((0, order, norm))
        elif any(h in path for h in WEAK_TEAM_HINTS):
            scored.append((1, order, norm))
    scored.sort()
    out: List[str] = []
    for _, _, url in scored:
        if url not in out:
            out.append(url)
    return out[: MAX_OWN_SITE_PAGES - 1]


def _search_queries(company: Dict[str, Any], budget: int) -> List[Tuple[str, str]]:
    """(query, language): the people pack and the ownership pack interleaved, so deal stories (which name the managers
    who stay on) are searched even when the budget is small."""
    qcompany = {"legal_name": company.get("legal_name"), "brand": company.get("brand"), "vat": company.get("vat")}
    topics = ("ownership", "people")
    languages = ("it", "en", "de")
    packs: Dict[Tuple[str, str], List[str]] = {}
    for topic in topics:
        for q in queryplan.build_query_pack(qcompany, (topic,)):
            packs.setdefault((topic, q.language), []).append(q.text)
    depth = max((len(v) for v in packs.values()), default=0)
    out: List[Tuple[str, str]] = []
    # name variant outermost, then language, then topic: a small budget still reaches every language (a South Tyrol
    # firm's deal story may be in German) before it spends a second variant on the first language
    for i in range(depth):
        for lang in languages:
            for topic in topics:
                texts = packs.get((topic, lang), [])
                if i < len(texts) and (texts[i], lang) not in out:
                    out.append((texts[i], lang))
                if len(out) >= budget:
                    return out
    return out


def _slug(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", folded).strip("-")


def _site_search_urls(ctx: CollectorContext, names: Sequence[str], own_host: str, seen_urls: Set[str],
                      result: CollectorResult) -> Tuple[List[str], bool]:
    """Deal-story pages found with the internal search of the sources in SOURCE_SEARCH_ENDPOINTS: company name in,
    only links whose PATH carries the company's slug out. Returns (urls, endpoint_was_readable)."""
    urls: List[str] = []
    readable = False
    slugs = [s for s in (_slug(n) for n in names) if len(s) >= 4]
    if not slugs:
        return urls, readable
    for endpoint, template in SOURCE_SEARCH_ENDPOINTS:
        page = ctx.fetch(template.format(q=quote_plus(names[0])))
        result.pages_fetched += 1
        if not page.ok or not page.text:
            result.errors.append(f"{endpoint} site search not readable ({page.note or page.status})")
            continue
        readable = True
        result.queries.append(f"{endpoint}: {names[0]}")
        for link in page.links:
            norm = normalise_url(link)
            if norm and norm not in seen_urls and not _same_site(norm, own_host) \
                    and any(s in urlparse(norm).path.lower() for s in slugs) and norm not in urls:
                urls.append(norm)
    return urls, readable


def _rank_results(ctx: CollectorContext, queries: Sequence[Tuple[str, str]], names: Sequence[str], own_host: str,
                  result: CollectorResult, deadline: float) -> Tuple[List[Any], int]:
    """Search results that look like they are about the company, most frequently returned first.
    Returns (items, raw_result_count): zero raw results over every query means the provider returned nothing at all."""
    hits: Dict[str, Dict[str, Any]] = {}
    raw = 0
    for text, lang in queries:
        if time.monotonic() > deadline:
            result.errors.append("search stopped at the deadline")
            break
        result.queries.append(text)
        try:
            res = ctx.search.search(text, kind="web", lang=lang, n=MAX_RESULTS_PER_QUERY)
        except Exception as exc:
            result.errors.append(f"search failed ({type(exc).__name__}) for one query")
            continue
        for item in res or []:
            raw += 1
            url = normalise_url(getattr(item, "url", "") or "")
            if not url or _same_site(url, own_host):
                continue
            blob = " ".join([getattr(item, "title", "") or "", getattr(item, "snippet", "") or "",
                             re.sub(r"[-_/]+", " ", url)])
            if not name_in_text(names, blob):
                continue
            slot = hits.setdefault(url, {"item": item, "count": 0})
            slot["count"] += 1
    ranked = sorted(hits.values(), key=lambda s: -s["count"])
    return [s["item"] for s in ranked], raw


# ---------------------------------------------------------------------------------------------------------------
# The collector
# ---------------------------------------------------------------------------------------------------------------

def run(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult()
    company = ctx.company
    names = company_names(company)
    website = normalise_url(company.get("website_url") or "")
    own_host = _host(website)
    started = time.monotonic()
    deadline = started + max(30, int(ctx.budget.deadline_seconds))
    max_pages = max(1, int(ctx.budget.max_pages))

    # 1. Registry roster (grade A, read only)
    registry_rows = _load_registry_rows(ctx, result)
    registry_cands = _registry_candidates(registry_rows, result)
    registry_names = [c.full_name for c in registry_cands if c.entity_type == "person"]
    verify_company = dict(company)
    verify_company["people"] = registry_names     # intel.verify uses person names as a discriminator

    web_cands: List[Tuple[PersonCandidate, str, str]] = []   # (candidate, text used for verification, grade)
    own_pages_read = 0
    press_pages_read = 0
    searched = False

    if ctx.fetch is None:
        result.errors.append("no fetcher supplied: only the registry roster was read")
    else:
        owners = _owner_names(ctx)

        # 2. The company's own site: the homepage and the people / leadership / legal pages it links to
        if website:
            home = ctx.fetch(website)
            result.pages_fetched += 1
            seen_urls: Set[str] = {normalise_url(home.url or website)}
            home_text = home.text if home.ok and home.text else ""
            site_pages = []
            if home_text:
                site_pages.append(home)
            elif not home.ok:
                result.errors.append(f"homepage not readable ({home.note or home.status})")
            for url in _team_links(home.links if home.ok else [], own_host, seen_urls):
                if result.pages_fetched >= max_pages or time.monotonic() > deadline:
                    break
                seen_urls.add(url)
                page = ctx.fetch(url)
                result.pages_fetched += 1
                if page.ok and page.text:
                    site_pages.append(page)
            if not any(urlparse(p.url).path.strip("/") for p in site_pages[1:]) and home_text:
                result.errors.append("no people / leadership / legal-notice link found on the homepage")
            own_pages_read = len(site_pages)
            for page in site_pages:
                for cand, text in _extract_candidates(page.text, page.url, "company_site", names, True, owners):
                    # the page may not repeat the company name; the homepage text does, for the verification gate
                    web_cands.append((cand, home_text + "\n" + text, "B"))
        else:
            result.errors.append("company has no website_url: own-site pages not read")
            seen_urls = set()

        # 3. Press, deal stories and profile pages: the internal search of declared sources, then web search
        press_urls: List[str] = []
        if not names:
            result.errors.append("company has no usable name: press and deal stories were not searched")
        else:
            site_urls, site_readable = _site_search_urls(ctx, names, own_host, seen_urls, result)
            press_urls.extend(site_urls)
            searched = searched or site_readable
            if ctx.search is None:
                result.errors.append("no search client supplied: the web was not searched for press and deal stories")
            else:
                queries = _search_queries(company, max(1, int(ctx.budget.max_search_queries)))
                ranked, raw = _rank_results(ctx, queries, names, own_host, result, deadline)
                if queries and raw == 0:
                    result.errors.append(
                        f"web search returned no result for any of {len(result.queries)} queries "
                        "(provider blocked, rate limited or no coverage); this is not evidence of absence")
                else:
                    searched = True
                press_urls.extend(normalise_url(item.url) for item in ranked)
        for url in press_urls:
            if press_pages_read >= MAX_PRESS_PAGES or result.pages_fetched >= max_pages or time.monotonic() > deadline:
                break
            if url in seen_urls:
                continue
            seen_urls.add(url)
            page = ctx.fetch(url)
            result.pages_fetched += 1
            if not page.ok or not page.text:
                continue
            press_pages_read += 1
            if not name_in_text(names, page.text):
                continue
            for cand, text in _extract_candidates(page.text, page.url, "press", names, False, owners):
                web_cands.append((cand, text, "C"))

    # 4. Verify every web candidate with the I06 gate; keep what passes, say what was dropped and why
    kept: List[Tuple[PersonCandidate, Fact]] = []
    dropped = 0
    for cand, text, grade in web_cands:
        fact = Fact(
            key=_fact_key(cand), value=_value(cand), grade=grade, confidence=0.9 if grade == "B" else 0.8,
            collector=COLLECTOR_VERSION,
            evidence=[Evidence(source_type=cand.source_type, url=cand.source_url, quote=cand.quote)],
        )
        verdict = verify(fact, text, verify_company, ctx.ledger)
        if verdict.verified:
            kept.append((cand, fact))
        else:
            dropped += 1
            if dropped <= 8:
                result.errors.append(f"dropped {cand.full_name!r} from {cand.source_url}: {'; '.join(verdict.reasons)[:160]}")
    if dropped > 8:
        result.errors.append(f"... and {dropped - 8} more web candidates failed verification")

    # 5. Facts: registry first, then verified web people (one fact per person and role; evidence merges)
    # One fact per (key, person): the same person in the same role found in two sources is one fact with two evidences,
    # even when the sources word the title differently.
    by_person: Dict[Tuple[str, str], Fact] = {}
    for cand in registry_cands:
        pkey = (_fact_key(cand), cand.full_name.lower())
        if pkey not in by_person:
            by_person[pkey] = Fact(
                key=pkey[0], value=_value(cand), grade="A", confidence=0.95, verified=True, collector=COLLECTOR_VERSION,
                evidence=[Evidence(source_type="registry", ref=cand.ref or "AIDA company_people roster")])
    for cand, fact in kept:
        pkey = (fact.key, cand.full_name.lower())
        existing = by_person.get(pkey)
        if existing is None:
            by_person[pkey] = fact
        else:
            for ev in fact.evidence:
                if all((ev.url, ev.quote) != (e.url, e.quote) for e in existing.evidence):
                    existing.evidence.append(ev)
    result.facts.extend(by_person.values())

    # 6. Who could sponsor a pilot: operating management of THIS company, best tier first
    sponsors = rank_pilot_sponsors([c for c in registry_cands if c.is_operating and c.entity_type == "person"]
                                   + [c for c, _ in kept if c.is_operating and c.affiliation == "company"])
    listed: List[Tuple[PersonCandidate, Fact]] = []
    seen_people: Set[Tuple[str, str]] = set()
    for c in sponsors:
        pkey = (_fact_key(c), c.full_name.lower())
        f = by_person.get(pkey)
        if f is not None and c.pilot_sponsor_rank <= 4 and pkey not in seen_people:
            seen_people.add(pkey)
            listed.append((c, f))
    if listed:
        # evidence with a URL first (a non-registry grade needs one), then the registry references; the grade is the
        # weakest grade among the evidence actually attached
        picked = sorted(listed, key=lambda cf: 0 if any(e.url for e in cf[1].evidence) else 1)[:4]
        evidence = [next((e for e in f.evidence if e.url), f.evidence[0]) for _, f in picked]
        web_grades = [f.grade for _, f in picked if any(e.url for e in f.evidence) and f.grade != "A"]
        grade = max(web_grades) if web_grades else "A"          # "C" > "B" > "A" as strings: the weakest wins
        result.facts.append(Fact(
            key="people.pilot_sponsors",
            value=[{"name": c.full_name, "role": c.normalized_role, "tier": c.pilot_sponsor_tier} for c, _ in listed],
            grade=grade, confidence=0.85, verified=True, collector=COLLECTOR_VERSION, evidence=evidence,
        ))

    # 7. Absence is only claimed when the company's own pages and a search were really read
    if own_pages_read >= 1 and searched and press_pages_read >= 1:
        found_keys = {f.key for f in result.facts}
        for role_key in ("people.general_manager", "people.managing_director", "people.export_manager"):
            if role_key not in found_keys:
                result.checked_absent.append(role_key)
    return result


COLLECTOR = Collector(
    name="people",
    ticket="I14",
    produces=("people.",),
    requires=("web", "search"),
    refresh_days=90,
    cost_hint="moderate",
    description="Registry officers, the company's own leadership pages and searched press: who runs the company, "
                "operating management separated from other organisations' executives, pilot sponsors ranked.",
    run=run,
)
