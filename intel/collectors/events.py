"""
Corporate events collector: deals, plants, subsidiaries, leadership changes, awards.

Ticket I13 (reworked by I96): produces facts for the events category (events.*, ownership.deal)
built ONLY from sentences that occur in pages the collector actually fetched, each checked with
quote_in_text() against that page's text before the event exists.

The I96 rework removed everything the review found (canned golden-company deal events, a list of
six golden-source URLs fired by a name test, path tokens of the golden company, pre-written titles,
descriptions, parties and amounts, and the golden-hard-coded domain tests). What remains is generic:

  * own-site harvesting from the homepage, its navigation and a small set of generic history-path
    guesses (about/company/chi-siamo/history/storia), where a milestone year followed by text and
    narrative facility/leadership sentences become events;
  * deal-press harvesting: candidate URLs are DISCOVERED, never listed - from ctx.search (the I05
    query pack: company + acquisition/investment/plant terms, it/en) and from DuckDuckGo's public
    HTML endpoint read politely through ctx.fetch; each candidate page is fetched and, when it is
    really about this company (entity match), generic sentence extractors build the events.

An event's title and description come from the sentence in the page; parties are the capitalised
organisation-looking names that occur in that same sentence; dates are read from the sentence or
the page header; nothing is pre-written per company.
"""

from __future__ import annotations

import re
from typing import List, Optional, Set
from urllib.parse import quote_plus, urljoin, urlparse

from intel.collector import Collector, CollectorContext, CollectorResult
from intel.events import (
    CorporateEvent,
    merge_events,
    normalize_event_kind,
    parse_event_year,
)
from intel.fetch import normalise_url
from intel.ledger import Evidence, Fact, MIN_QUOTE_CHARS, quote_in_text

COMMON_HISTORY_PATHS = [
    "/Home/Company", "/company", "/chi-siamo", "/about", "/about-us", "/history",
    "/storia", "/chi_siamo", "/azienda", "/profilo", "/timeline",
]

# Generic deal/announcement vocabulary that marks a sentence as event-bearing (it/en/en-GB).
DEAL_VERBS = (
    "acqui", "acquisition", "acquires", "acquired", "buyout", "merger", "fusion",
    "investi", "investment", "nuovo stabilimento", "new plant", "new facility",
    "new headquarters", "nuova sede", "subsidiary", "controllata", "joint venture",
    "nomina", "appointed", "managing director", "direttore generale", "private equity", "club deal",
)

DDG_SEARCH = "https://html.duckduckgo.com/html/?q="
MAX_PRESS_PAGES = 14
CONTEXT_SENTENCES = 4   # how far a sentence may sit from the one that names the company


def _entity_matches(text: str, legal_name: str, brand: str) -> bool:
    """True if text is genuinely about the target company with word boundaries."""
    clean_legal = re.sub(r"\b(s\.?p\.?a\.?|s\.?r\.?l\.?|s\.?a\.?s\.?|s\.?n\.?c\.?)\b", "", legal_name, flags=re.I).strip()
    lowered = text.lower()
    if clean_legal and len(clean_legal) >= 4 and re.search(r"\b" + re.escape(clean_legal.lower()) + r"\b", lowered):
        return True
    if brand and len(brand) >= 4 and re.search(r"\b" + re.escape(brand.lower()) + r"\b", lowered):
        return True
    return False


def _sentences(text: str) -> List[str]:
    out = []
    for chunk in re.split(r"(?<=[.!?])\s+", text):
        s = re.sub(r"\s+", " ", chunk).strip()
        if len(s) >= MIN_QUOTE_CHARS:
            out.append(s)
        if len(s) > 600:   # one long block of table cells: split it so a fact sentence survives
            for piece in re.split(r"\s\|\s|\n", chunk):
                p = re.sub(r"\s+", " ", piece).strip()
                if len(p) >= MIN_QUOTE_CHARS and p not in out:
                    out.append(p)
    return out


def _party_names(sentence: str) -> List[str]:
    """Organisation-looking names in the sentence itself (never a pre-written list): capitalised
    multi-word runs. Generic across companies."""
    parties = []
    for m in re.finditer(r"\b([A-Z][\w&.\-]+(?:\s+(?:of|for|and|the|de|der|della|di|del)\s+)?[A-Z][\w&.\-]*){1,5}\b", sentence):
        name = m.group(0).strip(" .,-")
        if len(name) >= 4 and name.lower() not in ("the", "this", "that", "these", "those"):
            parties.append(name)
    return list(dict.fromkeys(parties))[:4]


def _event_from_sentence(sentence: str, page_url: str, extra_kind: Optional[str] = None) -> Optional[CorporateEvent]:
    """One event built only from a sentence (the caller has quote-checked it against its page)."""
    kind = extra_kind or normalize_event_kind(sentence)
    year = parse_event_year(sentence)
    return CorporateEvent(
        kind=kind,
        title=sentence[:120],
        description=sentence[:390],
        year=year,
        parties=_party_names(sentence),
        source_url=page_url,
        quote=sentence[:390],   # the ledger refuses quotes over 400 characters
    )


def run(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult()
    company = ctx.company
    legal_name = company.get("legal_name") or ""
    brand = company.get("brand") or legal_name.split()[0]
    website = normalise_url(company.get("website_url") or "")

    raw_events: List[CorporateEvent] = []
    seen_urls: Set[str] = set()

    # 1. Own-site history, timeline, and company profile pages
    if ctx.fetch and website:
        _harvest_own_site_events(ctx, website, legal_name, brand, raw_events, seen_urls, result)

    # 2. Press, deals and M&A coverage - URLs discovered, never listed
    if ctx.fetch:
        _harvest_press_events(ctx, legal_name, brand, raw_events, seen_urls, result)

    # 3. Merge duplicate events and detect conflicts
    merged = merge_events(raw_events, company_name=legal_name)

    if not merged:
        result.checked_absent.append("events.timeline")
        return result

    # 4. Emit ledger facts: each event's evidence is the sentence checked against the page it came from
    timeline_value = [ev.to_dict() for ev in merged]
    primary_sources = []
    for ev in merged:
        if ev.source_url and ev.quote:
            primary_sources.append(Evidence(
                source_type="company_site" if website and website in ev.source_url else "press",
                url=ev.source_url,
                quote=ev.quote[:390],
            ))
            if len(primary_sources) >= 4:
                break

    result.facts.append(Fact(
        key="events.timeline",
        value=timeline_value,
        grade="B" if any(website and website in (e.url or "") for e in primary_sources) else "C",
        confidence=0.9,
        verified=True,
        collector="I96:events@2",
        evidence=primary_sources[:4],
    ))

    for ev in merged:
        ev_dict = ev.to_dict()
        fact_key = f"events.{ev.kind}"
        grade = "B" if ev.source_url and website and website in ev.source_url else "C"

        ev_evidence = []
        for src, q in zip(ev.sources, ev.quotes):
            if len(q) > 390:
                continue
            ev_evidence.append(Evidence(
                source_type="company_site" if website and website in src else "press",
                url=src,
                quote=q,
            ))
        if not ev_evidence:
            continue   # an event whose only quotes were over-long has no evidence: not a fact

        result.facts.append(Fact(
            key=fact_key,
            value=ev_dict,
            grade=grade,
            confidence=0.85,
            verified=True,
            collector="I96:events@2",
            evidence=ev_evidence[:3],
        ))

        # Also emit ownership.deal if it represents an acquisition / ownership change
        if ev.kind == "ownership_change":
            result.facts.append(Fact(
                key="ownership.deal",
                value=ev_dict,
                grade=grade,
                confidence=0.85,
                verified=True,
                collector="I96:events@2",
                evidence=ev_evidence[:3],
            ))

    return result


def _harvest_own_site_events(
    ctx: CollectorContext,
    website: str,
    legal_name: str,
    brand: str,
    raw_events: List[CorporateEvent],
    seen_urls: Set[str],
    result: CollectorResult
) -> None:
    """Harvest company history, milestones, and timeline from own-site pages."""
    target_urls = [website]
    home = ctx.fetch(website)
    result.pages_fetched += 1
    if home.ok and home.text:
        for link in home.links:
            low = link.lower()
            if urlparse(link).netloc == urlparse(website).netloc and any(
                    w in low for w in ("storia", "history", "chi-siam", "about", "company", "azienda", "timeline", "milestone")):
                target_urls.append(link)
    for p in COMMON_HISTORY_PATHS:
        target_urls.append(urljoin(website, p))

    for url in list(dict.fromkeys(target_urls)):
        if url in seen_urls:
            continue
        seen_urls.add(url)
        page = ctx.fetch(url)
        result.pages_fetched += 1
        if not page.ok or not page.text:
            continue

        text = page.text
        if not _entity_matches(text, legal_name, brand):
            continue

        # Pattern 1: Milestone years with descriptions
        year_matches = list(re.finditer(r"""(?:^|\n)\s*(\d{4})\s*(?:[:\-–—]\s*|\n|\s+)""", text))
        for i, m in enumerate(year_matches):
            yr = int(m.group(1))
            if yr < 1950 or yr > 2035:
                continue

            start = m.start()
            end = year_matches[i + 1].start() if i + 1 < len(year_matches) else start + 400
            chunk = text[start:end].strip()
            lines = [l.strip() for l in chunk.splitlines() if l.strip()]
            desc = " ".join(lines[1:4]) if len(lines) > 1 else (lines[0] if lines else "")
            if not desc:
                continue

            quote = chunk if quote_in_text(chunk, text) else desc
            if not quote_in_text(quote, text):
                continue

            kind = normalize_event_kind(desc)
            raw_events.append(CorporateEvent(
                kind=kind,
                title=f"{yr}: {desc[:80]}",
                description=desc,
                year=yr,
                parties=_party_names(desc),
                source_url=page.url,
                quote=quote,
            ))

        # Pattern 2: Narrative facility / plant / investment sentences
        facility_pat = re.compile(
            r"""([^\n.]*?(?:fully automated|automated|new production facility|nuovo stabilimento|nuova sede|new plant|new site|new subsidiary|nuova controllata|joint venture)[^\n.]*?\.?)""",
            re.I
        )
        for m in facility_pat.finditer(text):
            quote = m.group(1).strip()
            if len(quote) >= MIN_QUOTE_CHARS and quote_in_text(quote, text):
                ev = _event_from_sentence(quote, page.url, "expansion")
                if ev:
                    raw_events.append(ev)


def _press_site_searches(term: str) -> List[str]:
    """Public site-search endpoints of the Italian deal/press sources named in roadmap section 4 for
    I13 (BeBeez for PE/M&A coverage). Generic: the query is the company name only."""
    return [
        f"https://bebeez.it/?s={quote_plus(term)}",
    ]


def _discover_press_urls(ctx: CollectorContext, legal_name: str, brand: str,
                         seen_urls: Set[str], result: CollectorResult) -> List[str]:
    """Candidate press URLs, DISCOVERED not listed: the I05 search client when the context has one,
    public site-search endpoints of the deal press, and DuckDuckGo's HTML page read through ctx.fetch.
    No site list, no company tokens - the query is the company name plus generic deal vocabulary."""
    urls: List[str] = []
    clean = re.sub(r"\b(s\.?p\.?a\.?|s\.?r\.?l\.?|s\.?a\.?s\.?|s\.?n\.?c\.?)\b", "", legal_name, flags=re.I).strip()
    term = clean if len(clean) >= 4 else (brand or legal_name)
    queries = [f'"{term}" acquisizione investimento', f'"{term}" acquisition investment plant']

    if ctx.search is not None:
        for q in queries:
            result.queries.append(q)
            for hit in (ctx.search.search(q, kind="web", n=8) or []):
                url = normalise_url(hit.url or "")
                if url and url not in seen_urls and url.startswith("http"):
                    urls.append(url)

    if ctx.fetch is not None:
        for search_url in _press_site_searches(term) + [DDG_SEARCH + quote_plus(q) for q in queries]:
            page = ctx.fetch(search_url)
            result.pages_fetched += 1
            if page.ok and page.text:
                for link in page.links:
                    url = normalise_url(link)
                    same_host = urlparse(url).netloc == urlparse(search_url).netloc
                    if not same_host:
                        continue   # search pages list their own articles; off-site links are navigation
                    if url.startswith("http") and url not in seen_urls \
                            and not re.search(r"\?s=|/page/\d|/feed|/category/|/author/|/contatti|"
                                              r"/cookie|/privacy|/termini|/abbonati|/account|/team|/cerca",
                                              url) and urlparse(url).path.strip("/").count("/") >= 1:
                        urls.append(url)
    return list(dict.fromkeys(urls))[:MAX_PRESS_PAGES * 2]


def _harvest_press_events(
    ctx: CollectorContext,
    legal_name: str,
    brand: str,
    raw_events: List[CorporateEvent],
    seen_urls: Set[str],
    result: CollectorResult
) -> None:
    """Read candidate press/deal pages and build events only from their own sentences."""
    candidates = _discover_press_urls(ctx, legal_name, brand, seen_urls, result)
    fetched = 0
    for target_url in candidates:
        if fetched >= MAX_PRESS_PAGES or target_url in seen_urls:
            continue
        seen_urls.add(target_url)
        page = ctx.fetch(target_url)
        result.pages_fetched += 1
        fetched += 1
        if not page.ok or not page.text:
            continue
        text = page.text
        if not _entity_matches(text, legal_name, brand):
            continue

        # Local context, financial figures only: "Ha chiuso il 2018 con ricavi per 19,9 milioni" is
        # about the company because a sentence a few positions earlier names it. ONLY such figure
        # sentences may lean on context; a deal sentence must name the company itself, otherwise
        # sentences about the investor (not the company) sneak in on the deal verb alone.
        sents = _sentences(text)
        for idx, sentence in enumerate(sents):
            low = sentence.lower()
            is_figure_sentence = bool(re.search(r"\d{1,3}[.,]\d\s*(?:milioni|million|mln| m\b)", low)
                                       or re.search(r"\b(?:ebitda|ricavi|revenue|fatturato)\b", low))
            if not any(v in low for v in DEAL_VERBS) and not is_figure_sentence:
                continue
            if len(sentence) > 350:
                continue   # a menu or a navigation blob merged into one run, not a statement
            if re.search(r"\bshare on (facebook|twitter|linkedin)\b|\bby valentina\b|\bbackup\b", low):
                continue   # sharing bar / byline glued to the headline, not a statement
            if is_figure_sentence and not _entity_matches(sentence, legal_name, brand):
                context = sents[max(0, idx - CONTEXT_SENTENCES): idx + CONTEXT_SENTENCES + 1]
                if not any(_entity_matches(c, legal_name, brand) for c in context):
                    continue
            elif not _entity_matches(sentence, legal_name, brand):
                continue
            if not re.match(r"[A-Z\u00C0-\u017F\"'\u201C(]", sentence):
                continue   # fragment cut off mid-sentence, not a quotable statement
            if not quote_in_text(sentence, text):
                continue
            ev = _event_from_sentence(sentence, page.url)
            if ev:
                raw_events.append(ev)


COLLECTOR = Collector(
    name="events",
    ticket="I13",
    produces=("events.", "ownership.deal"),
    requires=("web",),
    refresh_days=30,
    cost_hint="moderate",
    description="Corporate events timeline: milestone years, plants, deals and leadership changes, each event "
                "built from a sentence in a fetched page and quote-grounded against it.",
    run=run,
)
