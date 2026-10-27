"""
Trade fairs, associations and programmes collector (ticket I16).

What it finds, with a verbatim quote for every fact:
  * trade fairs the company itself announces on its own site (news / events pages);
  * fairs and shared exhibition stands announced on a GROUP site (parent / sister company),
    reached through links on the company's own pages, never hard-coded;
  * association memberships the company's own site states;
  * the EU IPCEI Hydrogen matchmaking directory (a public register, grade A/B);
  * EU CORDIS project participation via the public search page (best effort, robots-friendly).

Rules this file follows (roadmap section 5):
  * every network access goes through ctx.fetch (polite, robots-aware), never requests directly;
  * a fact is only emitted with a URL and a verbatim quote checked with quote_in_text() against
    the fetched text; a paraphrase never gets in;
  * what was checked and not found is returned as checked_absent, never invented;
  * a Kompass-style business-directory listing is a *directory* fact, never a trade fair;
  * nothing about any specific company (no names, VATs, slugs) is hard-coded: the plugin list is
    generic and works on companies this collector has never seen.

Each source is a small plugin function with the same shape:
    plugin(ctx, company, result) -> None      (appends facts / checked_absent / errors)
The plugin authoring guide is in docs/intel/results/I16.md.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

from intel.collector import Collector, CollectorContext, CollectorResult
from intel.fetch import normalise_url
from intel.ledger import Evidence, Fact, MIN_QUOTE_CHARS, quote_in_text

# Fair names are the event brands, not company names: they are the vocabulary of the domain.
FAIR_KEYWORDS = (
    "achema", "hydrogen technology expo", "filtech", "sps italia", "sps ipc drives", "ipack-ima",
    "ipackima", "mecspe", "hanover messe", "hannover messe", "k fair", "k-show", "k show",
    "fruit logistica", "fruit_attraction", "interpack", "mdna", "bid", "ecomondo", "drinit",
    "piers", "genera", "simei", "cibus", "cibus tec", "paris air show", "vm", "metav",
    "emo", "bi-mu", "bimu", "xylem", "ifat", "waterbombay", "analytica", "medica", "comaqua",
    "saie", "made expo", "transpotec", "eicma", "host", "cibustec", "pharmag", "expobio",
    "interpoma", "viticulture", "simav", "miwater show", "suny", "agrilevexpo", "fiera",
    "expo", "tradeshow", "trade show", "messe", "exhibition", "stand",
)
FAIR_STRONG = ("achema", "hydrogen technology expo", "filtech", "sps ipc drives", "sps italia",
               "ipack-ima", "ipack-ima", "mecspe", "hanover messe", "hannover messe", "k show",
               "fruit logistica", "fruit attraction", "interpack", "eicma", "host milano",
               "emo milano", "bi-mu", "bimu", "analytica", "medica", "ecomondo", "ifat",
               "interpoma", "saie", "drinit", "simei", "piers", "viticulture expo")
ASSOCIATION_HINTS = ("associazione", "association", "verband", "federazione", "federation",
                     "consorzio", "membership", "member of", "socio", "membro", "albo")
# Pages of the company's own site where fairs and memberships are usually announced.
OWN_SITE_PATH_HINTS = ("news", "event", "fiera", "fiere", "manifestazione", "press", "media",
                       "aggiornamenti", "novita", "aktuelles", "presse", "veranstaltungen", "blog")

MAX_PAGES_PER_SOURCE = 6          # politeness + budget: a plugin reads few pages per site
MAX_GROUP_SITES = 2               # one company rarely has more than one or two group sites


def _brand_tokens(legal_name: str) -> List[str]:
    """Short name tokens the company is announced by (the cleaned full name plus its first word)."""
    cleaned = re.sub(r"\b(s\.?p\.?a\.?|s\.?r\.?l\.?|s\.?a\.?s\.?|s\.?n\.?c\.?|gmbh|in breve|"
                     r"o in forma abbreviata)\b", " ", (legal_name or ""), flags=re.I)
    cleaned = re.sub(r"[^A-Za-z0-9À-ž' ]+", " ", cleaned)
    tokens = [t for t in cleaned.split() if len(t) >= 3]
    if not tokens:
        return []
    return [" ".join(tokens), tokens[0]]


def _sentence_around(text: str, pos: int, radius: int = 400) -> str:
    """Cut a quote around a position, snapped to newline boundaries when possible. Web pages
    put long menus next to the content, so the window is wide and the fair keyword must survive
    inside the quote (the caller checks that)."""
    if not text:
        return ""
    start = max(0, pos - radius)
    end = min(len(text), pos + radius)
    b = text.rfind("\n", start, pos)
    if b != -1 and pos - b < radius:
        start = b + 1
    e = text.find("\n", pos, end)
    if e != -1:
        end = e + 1
    quote = re.sub(r"\s+", " ", text[start:end]).strip()
    return quote[:390]


def _find_fair_mentions(text: str, tokens: List[str]) -> List[Tuple[str, str]]:
    """(fair keyword, quote) pairs found in a page. The company must be named INSIDE the quote:
    a page that merely lists many posts (a search result) is not evidence the company exhibits
    at the fair, so the brand token is required in the quoted sentence itself."""
    lowered = text.lower()
    out: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for kw in FAIR_STRONG:
        for m in re.finditer(r"\b" + re.escape(kw) + r"\b", lowered):
            quote = _sentence_around(text, m.start())
            # the quote itself must name the fair AND the company: a window that lost the
            # keyword (menu text, unrelated sentence) is not evidence of participation
            if len(quote) < MIN_QUOTE_CHARS or quote in seen:
                continue
            if kw not in quote.lower():
                continue
            if not any(t.lower() in quote.lower() for t in tokens):
                continue
            seen.add(quote)
            out.append((kw, quote))
            break
    return out


def _find_membership_mentions(text: str, tokens: List[str]) -> List[Tuple[str, str]]:
    """(association hint, quote) pairs where the page says the company is a member of something."""
    out: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    lowered = text.lower()
    for hint in ASSOCIATION_HINTS:
        for m in re.finditer(hint, lowered):
            quote = _sentence_around(text, m.start())
            if len(quote) < MIN_QUOTE_CHARS or quote in seen:
                continue
            window_ok = any(t.lower() in quote.lower() for t in tokens)
            if not window_ok:
                continue
            seen.add(quote)
            out.append((hint, quote))
            break
    return out


def _fair_year(quote: str) -> Optional[str]:
    years = re.findall(r"\b(19[89]\d|20[0-3]\d)\b", quote)
    return years[-1] if years else None


def _fair_value(fair: str, quote: str, url: str) -> Dict[str, Any]:
    return {"fair": fair, "year": _fair_year(quote)}


def _emit_fair_facts(result: CollectorResult, mentions_by_url: List[Tuple[str, List[Tuple[str, str]]]],
                      pages_by_url: Dict[str, Any], grade: str, confidence: float) -> None:
    """One fact per (fair, year), with every page that shows it as merged evidence."""
    grouped: Dict[Tuple[str, Optional[str]], Fact] = {}
    for url, mentions in mentions_by_url:
        page = pages_by_url[url]
        for fair, quote in mentions:
            if not quote_in_text(quote, page.text or ""):
                continue
            key = (fair, _fair_year(quote))
            fact = grouped.get(key)
            evidence = Evidence(source_type="company_site", url=page.url, quote=quote,
                                content_hash=page.content_hash)
            if fact is None:
                grouped[key] = Fact(
                    key="networks.trade_fair", value=_fair_value(fair, quote, page.url), grade=grade,
                    confidence=confidence, verified=True, collector="I16:networks@1",
                    evidence=[evidence])
            else:
                fact.evidence.append(evidence)
    result.facts.extend(grouped.values())


def _own_site_pages(ctx: CollectorContext, website: str, result: CollectorResult) -> List[str]:
    """Candidate news/events pages of the company's own site, by link text and path hints."""
    pages: List[str] = []
    seen: Set[str] = set()
    home = ctx.fetch(website)
    result.pages_fetched += 1
    if not home.ok or not home.text:
        result.errors.append(f"homepage unreachable: {website}")
        return pages
    if home.links:
        for link in home.links:
            if not link.startswith(website.rstrip("/") + "/"):
                continue
            path = urlparse(link).path.lower()
            if any(h in path for h in OWN_SITE_PATH_HINTS) and link not in seen:
                seen.add(link)
                pages.append(link)
    return pages[:MAX_PAGES_PER_SOURCE]


def _group_sites(home_page, website: str) -> List[str]:
    """External sites the company's own page links to that plausibly belong to its group
    (a parent or sister company: a page that mentions the company and is linked from the
    company's own about/company page). Deduplicated, never a hard-coded domain."""
    own_host = urlparse(website).netloc.removeprefix("www.").lower() if website else ""
    out: List[str] = []
    for link in home_page.links or []:
        host = urlparse(link).netloc.removeprefix("www.").lower()
        if not host or host == own_host:
            continue
        if any(host.endswith(b) for b in ("linkedin.com", "facebook.com", "youtube.com", "twitter.com",
                                          "x.com", "instagram.com", "kompass.com")):
            continue
        if link not in out:
            out.append(link)
    return out[:MAX_GROUP_SITES]


def _plugin_own_site_fairs(ctx: CollectorContext, company: Dict, result: CollectorResult) -> None:
    """Trade fairs announced on the company's own news/events pages (grade B: its own pages)."""
    website = normalise_url(company.get("website_url") or "")
    if not website or not ctx.fetch:
        result.errors.append("no website_url, own-site fair search skipped")
        return
    tokens = _brand_tokens(company.get("legal_name") or "")
    if not tokens:
        return
    mentions_by_url, pages_by_url = [], {}
    for url in [website] + _own_site_pages(ctx, website, result):
        page = ctx.fetch(url)
        result.pages_fetched += 1
        if not page.ok or not page.text:
            continue
        mentions = _find_fair_mentions(page.text, tokens)
        if mentions:
            mentions_by_url.append((url, mentions))
            pages_by_url[url] = page
    if mentions_by_url:
        _emit_fair_facts(result, mentions_by_url, pages_by_url, grade="B", confidence=0.9)
    else:
        result.checked_absent.append("networks.trade_fair.own_site")


def _plugin_group_site_fairs(ctx: CollectorContext, company: Dict, result: CollectorResult) -> None:
    """Fairs announced on a group site (parent / sister). Discovery is link-based: the group
    sites are those the company's own pages link to that are not social / directory sites.
    A group page is only trusted when it names the company (a brand token) near the fair name."""
    website = normalise_url(company.get("website_url") or "")
    if not website or not ctx.fetch:
        return
    tokens = _brand_tokens(company.get("legal_name") or "")
    if not tokens:
        return
    # discover group links from the company page and its about/company page
    candidates: List[str] = []
    seen: Set[str] = set()
    home = ctx.fetch(website)
    result.pages_fetched += 1
    about_candidates = [website]
    if home.ok and home.text:
        for link in home.links or []:
            if link.startswith(website.rstrip("/") + "/") and any(
                    h in urlparse(link).path.lower() for h in ("company", "about", "chi-siamo", "chi_siamo",
                                                              "azienda", "profil", "ueber-uns", "group")):
                about_candidates.append(link)
    for url in about_candidates[:3]:
        page = ctx.fetch(url)
        result.pages_fetched += 1
        if not page.ok or not page.text:
            continue
        for gsite in _group_sites(page, website):
            if gsite not in seen:
                seen.add(gsite)
                candidates.append(gsite)
    for gsite in candidates[:MAX_GROUP_SITES]:
        if not ctx.fetch:
            break
        parsed = urlparse(gsite)
        netloc = parsed.netloc
        bare = netloc[4:] if netloc.startswith("www.") else netloc
        # the site's own links may use either the bare or the www host: accept both
        roots = (f"{parsed.scheme}://{netloc}", f"{parsed.scheme}://www.{bare}")
        if netloc.startswith("www."):
            roots = roots + (f"{parsed.scheme}://{bare}",)
        search_urls = [roots[0] + "/?s=" + quote_plus(tokens[0]), roots[0] + "/"]
        searched_page = None
        for surl in search_urls:
            spage = ctx.fetch(surl)
            result.pages_fetched += 1
            if spage.ok and spage.text and any(t.lower() in spage.text.lower() for t in tokens):
                searched_page = spage
                if "/?s=" in surl:
                    break
            if not spage.ok:
                continue
        if searched_page is None:
            continue
        # fair pages found in the search results or on the group home page
        fair_pages: List[str] = []
        for link in searched_page.links or []:
            if not link.startswith(roots):
                continue
            if any(h in link.lower() for h in
                   ("achema", "expo", "trade-show", "tradeshow", "fair",
                    "messe", "exhibition", "event")):
                fair_pages.append(link)
        # the fair posts first (a page about the fair), the search page only as a last resort
        g_mentions, g_pages = [], {}
        for furl in fair_pages[:3] + [searched_page.url]:
            fpage = ctx.fetch(furl)
            result.pages_fetched += 1
            if not fpage.ok or not fpage.text:
                continue
            mentions = _find_fair_mentions(fpage.text, tokens)
            if mentions:
                g_mentions.append((fpage.url, mentions))
                g_pages[fpage.url] = fpage
        if g_mentions:
            _emit_fair_facts(result, g_mentions, g_pages, grade="C", confidence=0.85)


def _plugin_own_site_associations(ctx: CollectorContext, company: Dict, result: CollectorResult) -> None:
    """Association memberships stated on the company's own pages (grade B)."""
    website = normalise_url(company.get("website_url") or "")
    if not website or not ctx.fetch:
        return
    tokens = _brand_tokens(company.get("legal_name") or "")
    if not tokens:
        return
    pages = [website] + _own_site_pages(ctx, website, result)
    found_any = False
    for url in pages:
        page = ctx.fetch(url)
        result.pages_fetched += 1
        if not page.ok or not page.text:
            continue
        for hint, quote in _find_membership_mentions(page.text, tokens):
            if not quote_in_text(quote, page.text):
                continue
            result.facts.append(Fact(
                key="networks.association", value=quote, grade="B", confidence=0.8,
                verified=True, collector="I16:networks@1",
                evidence=[Evidence(source_type="company_site", url=page.url, quote=quote,
                                   content_hash=page.content_hash)]))
            found_any = True
            break
        if found_any:
            break
    if not found_any:
        result.checked_absent.append("networks.association.own_site")


IPCEI_BASE = "https://ipcei.observatory.clean-hydrogen.europa.eu/directory"


def _plugin_ipcei_hydrogen(ctx: CollectorContext, company: Dict, result: CollectorResult) -> None:
    """EU IPCEI Hydrogen matchmaking directory: a public register of the hydrogen ecosystem.
    The directory's own ?combine= filter finds the company by name; the entry page lists
    association memberships (e.g. H2IT) in plain text. Grade A: a public EU register."""
    if not ctx.fetch:
        return
    tokens = _brand_tokens(company.get("legal_name") or "")
    if not tokens:
        return
    index = ctx.fetch(IPCEI_BASE + "?combine=" + quote_plus(tokens[0]))
    result.pages_fetched += 1
    if not index.ok or not index.text:
        result.checked_absent.append("networks.programme.ipcei_hydrogen")
        return
    lowered = index.text.lower()
    entry_url = None
    for link in index.links or []:
        if "/directory/" in link and link.rstrip("/").split("/")[-1] not in ("directory",):
            slug = link.rstrip("/").split("/")[-1].lower()
            brand_slug = re.sub(r"[^a-z0-9]+", "-", tokens[0].lower()).strip("-")
            if slug == brand_slug or slug.startswith(brand_slug):
                entry_url = link
                break
            if all(t.lower() in slug for t in tokens[0].lower().split() if len(t) >= 3):
                entry_url = link
                break
    if entry_url is None:
        # looked at the register and the company is not in it
        result.checked_absent.append("networks.programme.ipcei_hydrogen")
        return
    entry = ctx.fetch(entry_url)
    result.pages_fetched += 1
    if not entry.ok or not entry.text:
        result.errors.append(f"IPCEI entry unreadable: {entry_url}")
        return
    text = entry.text
    # quote the entry's identity block: the company name heading through its legal address,
    # which is what proves THIS company (name + city) is the register entry
    anchor = text.find("Legal address:")
    if anchor >= 0:
        start = text.lower().rfind(tokens[0].lower(), 0, anchor)
        start = start if start >= 0 else max(0, anchor - 120)
        end = text.find("\n", anchor + 1)
        end = text.find("\n", end + 1) if end != -1 else min(len(text), anchor + 200)
        quote = re.sub(r"\s+", " ", text[start:end]).strip()[:390]
    else:
        pos = text.lower().rfind(tokens[0].lower())
        quote = _sentence_around(text, pos) if pos >= 0 else ""
    if not quote or not quote_in_text(quote, text) or tokens[0].lower() not in quote.lower():
        result.errors.append("IPCEI entry does not name the company verbatim; skipped")
        return
    result.facts.append(Fact(
        key="networks.programme", value="EU IPCEI Hydrogen matchmaking directory: " + (entry.title or entry_url),
        grade="A", confidence=0.95, verified=True, collector="I16:networks@1",
        evidence=[Evidence(source_type="public_register", url=entry.url, quote=quote,
                           content_hash=entry.content_hash)]))
    # the entry's "Association's membership:" line is a second, separate fact
    m = re.search(r"Association's membership:\s*([^\n]{8,300})", text)
    if m and quote_in_text(m.group(0).strip(), text):
        result.facts.append(Fact(
            key="networks.association", value=m.group(1).strip(), grade="A", confidence=0.95,
            verified=True, collector="I16:networks@1",
            evidence=[Evidence(source_type="public_register", url=entry.url, quote=m.group(0).strip()[:390],
                               content_hash=entry.content_hash)]))


CORDIS_SEARCH = "https://cordis.europa.eu/search?q="


def _plugin_cordis(ctx: CollectorContext, company: Dict, result: CollectorResult) -> None:
    """EU CORDIS: participation in EU-funded research projects. Best effort: the public search
    page is JavaScript-rendered, so a miss is recorded as checked_absent only when the page
    could be fetched and named the company; a fetch failure is an error, not an absence."""
    if not ctx.fetch:
        return
    tokens = _brand_tokens(company.get("legal_name") or "")
    if not tokens:
        return
    page = ctx.fetch(CORDIS_SEARCH + quote_plus('"' + tokens[0] + '"'))
    result.pages_fetched += 1
    if not page.ok or not page.text:
        result.errors.append("CORDIS search page unreachable")
        return
    if any(t.lower() in (page.text or "").lower() for t in tokens):
        # The page is JS-rendered; a bare hit without a quote is not a fact. Record the lead only.
        result.errors.append("CORDIS search returned a hit; page is JS-rendered, no quotable text")
    else:
        result.checked_absent.append("networks.programme.cordis")


PLUGINS = (
    _plugin_own_site_fairs,
    _plugin_group_site_fairs,
    _plugin_own_site_associations,
    _plugin_ipcei_hydrogen,
    _plugin_cordis,
)


def run(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult()
    for plugin in PLUGINS:
        try:
            plugin(ctx, ctx.company, result)
        except Exception as exc:   # a broken plugin is reported, never kills the others
            result.errors.append(f"{plugin.__name__}: {type(exc).__name__}: {str(exc)[:120]}")
    if not result.facts and not result.checked_absent:
        result.checked_absent.append("networks.trade_fair")
    return result


COLLECTOR = Collector(
    name="networks", ticket="I16", produces=("networks.",), requires=("web",), refresh_days=180,
    cost_hint="moderate",
    description="Trade fairs (own and group sites), association memberships, IPCEI Hydrogen "
                "directory and CORDIS participation, every fact with a verbatim quote.",
    run=run,
)
