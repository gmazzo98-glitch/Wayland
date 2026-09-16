"""
Free News/Press adapter (Phase 4) — Google News RSS, no API key, no registration,
no per-call cost.

Why this exists: the News/Press indicators previously depended on either a paid
NewsAPI plan (the two Node crawlers in Phase 7) or a Google Programmable Search
key (adapters/google_news.py). With neither configured, those rows produced
nothing real — and google_news.py's simulate() path actively wrote placeholder
numbers derived from a hash of the company name. This adapter covers the same
ground from a keyless public feed instead.

Endpoint: https://news.google.com/rss/search?q=<query>&hl=<lang>&gl=<country>&ceid=<country>:<lang>
Verified live against real Italian and German companies. Four things worth knowing,
all established by testing rather than assumption:
  - the recency operator takes days, not months: `when:730d` works, `when:24m`
    silently returns zero results;
  - the legal-form suffix destroys an exact-phrase match — `"R.C.M. S.P.A."`
    returns nothing where `"RCM"` returns results — so the search phrase is built
    from a cleaned company name;
  - the feed ANDs a quoted phrase with any keyword group, which kills recall:
    `"Cangini Benne"` alone returns the story about the company being acquired,
    but `"Cangini Benne" (acquisizione OR ...)` returns zero, because the headline
    says "vende"/"acquista" rather than the keyword searched for. So this adapter
    issues ONE query per company for the name alone and buckets the headlines into
    concepts locally, where the matching is under our control — that is both higher
    recall and one HTTP request per company instead of six;
  - results are noisy, so every headline must actually name the company to count.

ABSENCE IS NEVER ASSERTED HERE. A small Mittelstand manufacturer having no press
coverage is not evidence that it has no partnerships — press_launch_mentions'
own catalog comment makes exactly this point ("a company can still be innovating
quietly without press coverage"). So a query returning nothing leaves the signal
not_yet_checked rather than writing a confirmed zero.
"""

import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
from sqlalchemy.orm import Session

from adapters.base import run_adapter
from config import NEWSAPI_KEY

SOURCE_NAME = "Google News RSS"
PHASE = 4
RSS_URL = "https://news.google.com/rss/search"
RECENCY_DAYS = 730  # the 24-month window the sourcing plan asks for, in the unit the feed accepts
REQUEST_SPACING_SECONDS = 0.8
MAX_ITEMS_PER_QUERY = 25

# Locale per company country — an Italian firm's coverage is in the Italian feed.
LOCALES = {"Italy": ("it", "IT"), "Germany": ("de", "DE")}
DEFAULT_LOCALE = ("en", "US")

# Concept buckets, applied to headlines locally (see the module docstring on why the
# keywords are not part of the query). Multilingual by design: one pass covers IT/DE/EN.
CONCEPT_PATTERNS = {
    "partnership": re.compile(
        r"partnership|partner|collaboraz|cooperaz|accordo con|joint venture|"
        r"kooperation|partnerschaft|zusammenarbeit", re.I),
    "university": re.compile(
        r"universit|ateneo|politecnico|dipartimento|ricerca|spin-?off|"
        r"hochschule|fraunhofer|forschung", re.I),
    "innovation": re.compile(
        r"innovazion|innovativ|digitalizzazion|trasformazione digitale|industria 4\.0|"
        r"innovation|digitalisierung|smart factory", re.I),
    # A bare "lancia"/"launch" is ambiguous in business press — live testing on
    # Datalogic returned "Hydra lancia l'OPA per il delisting", a takeover bid, which
    # would have counted as a product launch. The verb must be tied to a product noun.
    "launch": re.compile(
        r"(lancia|lancio|presenta|debutta|introduce)\s+(il\s+|la\s+|un\s+|una\s+|le\s+|i\s+)?"
        r"(nuov[oaie]\s+)?(prodotto|gamma|serie|modello|soluzione|macchina|linea)|"
        r"nuov[oaie]\s+(prodotto|gamma|serie|modello|soluzione)|"
        r"(launches|unveils|introduces)\s+(a\s+|its\s+|the\s+)?(new\s+)?(product|range|series|model|solution)|"
        r"neues produkt|neue produktreihe|stellt.{0,20}\bvor\b", re.I),
    "ma": re.compile(
        r"acquisizion|acquisisce|acquisita|acquista|rileva|cede|vende|fusione|"
        r"übernahme|übernimmt|akquisition|merger", re.I),
    "open_innovation": re.compile(
        r"acceleratore|incubatore|hackathon|start-?up|premio innovazione|competizione|"
        r"accelerator|inkubator|wettbewerb", re.I),
}

# Signals the paid Phase 7 crawlers own when NEWSAPI_KEY is configured. This adapter
# is the free fallback, so it yields those keys rather than double-writing them —
# the project keeps one producer per signal_key.
NEWSAPI_OWNED_KEYS = {"external_collaboration", "university_partnership",
                      "press_launch_mentions", "prior_open_innovation_usage"}

_STOPWORDS = {"spa", "srl", "s.p.a", "s.r.l", "gmbh", "kg", "ag", "co", "spa.", "group", "gruppo",
              "international", "italia", "italy", "deutschland", "and", "the"}

# Stock-market and results coverage is about the share, not about what the company
# does, and it dominates the feed for listed firms. Datalogic's window was almost
# entirely takeover-bid and half-year-results stories, none of which say anything
# about innovation capacity — they are dropped before any concept matching.
_FINANCE_NOISE_RE = re.compile(
    r"\bopa\b|delisting|piazza affari|borsa|azion|dividendo|bilancio|semestrale|trimestr|"
    r"ricavi|utile|perdita|quotazion|titolo|investor|aktie|dividende|quartalszahlen|"
    r"earnings|shares?\b|stake", re.I,
)

# Legal forms stripped from the search phrase — leaving them in reduces an exact-phrase
# match to zero hits (verified: "R.C.M. S.P.A." -> 0, "RCM" -> 3).
_LEGAL_FORM_RE = re.compile(
    r"\b(s\.?p\.?a\.?|s\.?r\.?l\.?(\s*u\.?n\.?i\.?p\.?e\.?r\.?s\.?o\.?n\.?a\.?l\.?e\.?)?|s\.?a\.?s\.?|s\.?n\.?c\.?|"
    r"gmbh(\s*&\s*co\.?\s*kg)?|ag|kg|ohg|e\.?k\.?|ltd|plc|inc)\b\.?\s*$",
    re.I,
)


def _search_name(legal_name: str) -> str:
    """The company name as it would actually appear in a headline."""
    name = (legal_name or "").strip()
    for _ in range(3):  # e.g. "... GMBH & CO. KG" can need more than one pass
        stripped = _LEGAL_FORM_RE.sub("", name).strip(" .,-")
        if stripped == name:
            break
        name = stripped
    # Dotted acronyms are written solid in headlines ("R.C.M." -> "RCM"), and leaving
    # the dots in both breaks the phrase search and leaves no token long enough to
    # match on (verified: "R.C.M. S.P.A." found nothing, "RCM" found results).
    name = re.sub(r"\b(?:[A-Za-z]\.){2,}", lambda m: m.group(0).replace(".", ""), name)
    return name.strip(" .,-") or (legal_name or "").strip()


def _name_tokens(legal_name: str) -> list:
    """Distinctive words from the legal name — legal-form suffixes carry no
    identifying power and would match almost any article."""
    toks = [t.lower() for t in re.findall(r"[\wÀ-ÿ]{3,}", _search_name(legal_name))]
    return [t for t in toks if t not in _STOPWORDS]


# Names too generic to attribute coverage safely. Live testing: "R.C.M. S.P.A."
# (floor-sweeper maker) matched 11 headlines about "RCM Costruzioni" building dams
# in Genova and Ravenna — a different company entirely — and "MORE S.R.L." would
# match any headline containing the word "more". Attributing another firm's news to
# a prospect is worse than having no signal, so these are skipped outright.
_MIN_SINGLE_TOKEN_LEN = 5
_GENERIC_NAME_WORDS = {"more", "next", "smart", "delta", "alfa", "prima", "sistemi", "system",
                       "systems", "service", "servizi", "tecno", "italtech", "euro", "global"}


def _name_is_distinctive(legal_name: str) -> bool:
    tokens = _name_tokens(legal_name)
    if not tokens:
        return False
    if len(tokens) >= 2:
        return True
    only = tokens[0]
    return len(only) >= _MIN_SINGLE_TOKEN_LEN and only not in _GENERIC_NAME_WORDS


def _is_about_company(title: str, tokens: list) -> bool:
    """Every distinctive token must appear — "Cangini" alone would also match an
    unrelated person named Cangini, which is the noise seen in live testing."""
    t = (title or "").lower()
    return bool(tokens) and all(tok in t for tok in tokens)


def _fetch(query: str, lang: str, country: str) -> list:
    url = (f"{RSS_URL}?q={urllib.parse.quote(query)}&hl={lang}&gl={country}"
           f"&ceid={country}:{lang}")
    resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (compatible; ProjectViennaBot/1.0)"})
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = []
    for item in root.findall(".//item")[:MAX_ITEMS_PER_QUERY]:
        source_el = item.find("source")
        items.append({
            "title": item.findtext("title") or "",
            "url": item.findtext("link") or "",
            "published": item.findtext("pubDate") or "",
            "outlet": source_el.text if source_el is not None else "",
        })
    return items


def _company_headlines(company) -> list:
    """One request: every headline from the window that actually names the company."""
    lang, country = LOCALES.get(company.country or "", DEFAULT_LOCALE)
    name = _search_name(company.legal_name)
    items = _fetch(f'"{name}" when:{RECENCY_DAYS}d', lang, country)
    tokens = _name_tokens(company.legal_name)
    seen, kept = set(), []
    for it in items:
        if not _is_about_company(it["title"], tokens):
            continue
        if _FINANCE_NOISE_RE.search(it["title"]):
            continue  # share-price/results coverage says nothing about innovation capacity
        key = it["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(it)
    return kept


def _cited(items: list) -> list:
    return [{"label": f"{i['title']} — {i['outlet']}" if i["outlet"] else i["title"], "url": i["url"]}
            for i in items]


def _bucket(headlines: list, concept: str) -> list:
    return [h for h in headlines if CONCEPT_PATTERNS[concept].search(h["title"] or "")]


def _fetch_live(company) -> dict:
    signals = {}
    skip_owned = bool(NEWSAPI_KEY)
    if not _name_is_distinctive(company.legal_name):
        return {
            "signals": {},
            "raw_payload": {"skipped": "company name is too generic to attribute news coverage safely",
                             "search_phrase": _search_name(company.legal_name)},
            "confidence": 0.5,
        }
    headlines = _company_headlines(company)
    lang, country = LOCALES.get(company.country or "", DEFAULT_LOCALE)
    search_phrase = _search_name(company.legal_name)

    def add(key, concept, value_fn, summary_prefix):
        items = _bucket(headlines, concept)
        if not items or (key in NEWSAPI_OWNED_KEYS and skip_owned):
            return
        cited = _cited(items)
        signals[key] = {
            "value": value_fn(items), "status": "present",
            "summary": f"{summary_prefix.format(n=len(items))}: "
                        + "; ".join(c["label"] for c in cited[:2])
                        + ("" if len(items) <= 2 else f" (+{len(items) - 2} more)"),
            "evidence": {
                "method": f'Google News RSS search for "{search_phrase}" over the last {RECENCY_DAYS} days; '
                           f"headlines naming the company were then matched against: "
                           f"{CONCEPT_PATTERNS[concept].pattern}",
                "found": cited,
                "locale": f"{lang}-{country}",
                "headlines_about_company": len(headlines),
                "note": "headline-level keyword match, not a read of the article — open the links to confirm",
            },
        }

    count = lambda items: float(len(items))
    flag = lambda items: 1.0

    add("partnership_news_count", "partnership", count, "{n} partnership/collaboration headline(s)")
    add("external_collaboration", "partnership", flag, "collaboration reported in the press ({n} headline(s))")
    add("university_partnership", "university", flag, "university/research coverage ({n} headline(s))")
    add("board_innovation_statements", "innovation", count, "{n} innovation/digitalisation headline(s)")
    add("press_launch_mentions", "launch", count, "{n} product-launch headline(s)")
    add("recent_ma_activity", "ma", flag, "M&A coverage ({n} headline(s))")
    add("prior_open_innovation_usage", "open_innovation", flag, "accelerator/competition coverage ({n} headline(s))")

    return {
        "signals": signals,
        "raw_payload": {"search_phrase": search_phrase, "locale": f"{lang}-{country}",
                         "headlines_about_company": len(headlines),
                         "headlines_by_concept": {c: len(_bucket(headlines, c)) for c in CONCEPT_PATTERNS}},
        "confidence": 0.55,  # a headline keyword match is weaker evidence than a read of the article
    }


def _simulate(company) -> dict:
    # No placeholder values: see the module docstring, and scrapers/node_crawler_base.py
    # on why a fabricated number is indistinguishable from a verified one at scoring time.
    return {"signals": {}, "raw_payload": {"note": "Google News RSS unreachable"}, "confidence": 0.5}


def sync_news_rss(company, db_session: Session) -> dict:
    return run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=True,  # keyless public feed
        fetch_live=_fetch_live, simulate=_simulate, timeout=60,
    )
