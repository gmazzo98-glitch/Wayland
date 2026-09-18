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
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
from sqlalchemy.orm import Session

from adapters.base import run_adapter
from config import NEWSAPI_KEY
from scrapers.node_crawler_base import save_crawler_blob

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
              "international", "italia", "italy", "deutschland", "and", "the",
              # Italian articles/prepositions that survive the 3-letter token cut
              # ("DELL'IMBOTTIGLIAMENTO" splits into "dell" + "imbottigliamento")
              "dei", "del", "della", "delle", "dell", "degli", "con", "per", "nel", "nella"}

# Stock-market and results coverage is about the share, not about what the company
# does, and it dominates the feed for listed firms. Datalogic's window was almost
# entirely takeover-bid and half-year-results stories, none of which say anything
# about innovation capacity — they are dropped before any concept matching.
# Every term is word-anchored: the earlier bare `azion` also matched inside
# innovAZIONe, collaborAZIONe and digitalizzAZIONe (and the outlet suffix
# "ristorAZIONemoderna.it"), which silently deleted the exact headlines the
# innovation and partnership buckets exist to find.
_FINANCE_NOISE_RE = re.compile(
    r"\bopa\b|\bdelisting\b|piazza affari|\bborsa\b|\bazion[ei]\b|\bazionist[aei]\b|\bdividend[oi]\b|"
    r"\bbilancio\b|\bsemestrale\b|\btrimestr\w*|\bricavi\b|\butile\b|\bperdita\b|\bquotazion\w*|"
    r"\btitolo\b|\binvestor\w*|\baktie\w*|\bdividende\b|\bquartalszahlen\b|"
    r"\bearnings\b|\bshares?\b|\bstake\b", re.I,
)

# Legal forms stripped from the search phrase — leaving them in reduces an exact-phrase
# match to zero hits (verified: "R.C.M. S.P.A." -> 0, "RCM" -> 3).
_LEGAL_FORM_RE = re.compile(
    r"\b(s\.?p\.?a\.?|s\.?r\.?l\.?(\s*u\.?n\.?i\.?p\.?e\.?r\.?s\.?o\.?n\.?a\.?l\.?e\.?)?|s\.?a\.?s\.?|s\.?n\.?c\.?|"
    r"gmbh(\s*&\s*co\.?\s*kg)?|ag|kg|ohg|e\.?k\.?|ltd|plc|inc)\b\.?\s*$",
    re.I,
)
# "& C." — the Italian partners' placeholder ("OFFICINE E. BIGLIA & C. S.P.A.") — and
# lone initials. Neither ever appears in a headline; left in, they made Biglia and
# A Due permanently invisible (the quoted phrase never matched anything).
_AMPERSAND_C_RE = re.compile(r"\s*&\s*c\.?\s*$", re.I)
_INITIAL_RE = re.compile(r"\b[A-Za-z]\.\s*")


def _ascii(text: str) -> str:
    """Lowercase, accents stripped — so 'Società' and 'Societa' compare equal."""
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()


def _word(token: str) -> str:
    """Regex for the token as a whole word (a hyphen or space counts as a boundary)."""
    return r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])"


def _search_name(legal_name: str) -> str:
    """The company name as it would actually appear in a headline."""
    name = (legal_name or "").strip()
    for _ in range(4):  # e.g. "... & C. S.P.A." / "... GMBH & CO. KG" need more than one pass
        stripped = _LEGAL_FORM_RE.sub("", name).strip(" .,-")
        stripped = _AMPERSAND_C_RE.sub("", stripped).strip(" .,-")
        if stripped == name:
            break
        name = stripped
    # Dotted acronyms are written solid in headlines ("R.C.M." -> "RCM"), and leaving
    # the dots in both breaks the phrase search and leaves no token long enough to
    # match on (verified: "R.C.M. S.P.A." found nothing, "RCM" found results).
    name = re.sub(r"\b(?:[A-Za-z]\.){2,}", lambda m: m.group(0).replace(".", ""), name)
    name = " ".join(_INITIAL_RE.sub("", name).split())
    return name.strip(" .,-") or (legal_name or "").strip()


def _name_tokens(legal_name: str) -> list:
    """Distinctive words from the legal name — legal-form suffixes carry no
    identifying power and would match almost any article."""
    toks = [_ascii(t) for t in re.findall(r"[\wÀ-ÿ]{3,}", _search_name(legal_name))]
    return [t for t in toks if t not in _STOPWORDS]


# Names too generic to attribute coverage safely. Live testing: "R.C.M. S.P.A."
# (floor-sweeper maker) matched 11 headlines about "RCM Costruzioni" building dams
# in Genova and Ravenna — a different company entirely — and "MORE S.R.L." would
# match any headline containing the word "more". Attributing another firm's news to
# a prospect is worse than having no signal, so these are skipped outright.
_MIN_SINGLE_TOKEN_LEN = 5
_GENERIC_NAME_WORDS = {"more", "next", "smart", "delta", "alfa", "prima", "sistemi", "system",
                       "systems", "service", "servizi", "tecno", "italtech", "euro", "global"}
# A single short brand token is collision-prone even when it isn't a dictionary word:
# probed live, "Fimer" (bottling machines) returns 25 headlines about FIMER the solar
# inverter maker, "Turo" (pumps) returns the car-sharing company, "Biglia" a
# footballer. Below this length a lone token is only attributed when the headline
# also carries a discriminator taken from the company record (its province/region,
# or a specific word from its sector description) — see _discriminators.
_SINGLE_TOKEN_SAFE_LEN = 8
_SECTOR_GENERIC_WORDS = {"fabbricazione", "produzione", "macchine", "macchina", "apparecchi", "apparecchiature",
                         "attrezzature", "incluse", "parti", "accessori", "altre", "altri", "industrie",
                         "industria", "impiego", "generale", "materiale", "commercio", "ingrosso",
                         "dettaglio", "servizi", "attivita", "manifatturiere", "prodotti", "articoli"}


def _attribution_mode(legal_name: str) -> str:
    """'skip' (too generic), 'tokens' (every distinctive token must appear in the
    headline) or 'tokens+discriminator' (a lone short token also needs a place/sector
    word from the company record in the headline)."""
    tokens = _name_tokens(legal_name)
    if not tokens:
        return "skip"
    if len(tokens) >= 2:
        return "tokens"
    only = tokens[0]
    if len(only) < _MIN_SINGLE_TOKEN_LEN or only in _GENERIC_NAME_WORDS:
        return "skip"
    return "tokens" if len(only) >= _SINGLE_TOKEN_SAFE_LEN else "tokens+discriminator"


def _name_is_distinctive(legal_name: str) -> bool:
    return _attribution_mode(legal_name) != "skip"


def _discriminators(company) -> list:
    """Words that tie a headline to THIS company when its name alone can't: the
    province/region on the record and the specific nouns of its sector description
    ("imbottigliamento", "rubinetti"), minus the boilerplate every ATECO label has."""
    words = set()
    for attr in ("province", "region"):
        value = getattr(company, attr, None)
        if value and len(str(value).strip()) >= 4:
            words.add(_ascii(str(value).strip()))
    for w in re.findall(r"[\wÀ-ÿ]{6,}", getattr(company, "sector_name", None) or ""):
        w = _ascii(w)
        if w not in _SECTOR_GENERIC_WORDS:
            words.add(w)
    return sorted(words)


def _is_about_company(title: str, tokens: list) -> bool:
    """Every distinctive token must appear as a whole word — "Cangini" alone would
    also match an unrelated person named Cangini, and a substring test matched
    "due" inside "Le due aziende" and "mac" inside "macchine" in live testing. A
    hyphenated name is also accepted written solid ("Vibro-Mac" / "Vibromac")."""
    if not tokens:
        return False
    t = _ascii(title)
    if all(re.search(_word(tok), t) for tok in tokens):
        return True
    return len(tokens) > 1 and re.search(_word("".join(tokens)), t) is not None


def _has_discriminator(title: str, discriminators: list) -> bool:
    t = _ascii(title)
    return any(re.search(_word(d), t) for d in discriminators)


def _headline_text(title: str, outlet: str) -> str:
    """The headline without Google News' trailing ' - Outlet' — matching the outlet
    name lit up the finance filter on 'ristorazionemoderna.it' and would let an
    outlet named after a university count as university coverage."""
    t = (title or "").strip()
    if outlet and t.lower().endswith(" - " + outlet.strip().lower()):
        return t[: -(len(outlet.strip()) + 3)].strip()
    return t


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


def _filter_headlines(items: list, company) -> tuple:
    """Keeps the headlines that are about THIS company; returns (kept, stats) so a
    run that ends with no signal still explains what it saw and dropped."""
    tokens = _name_tokens(company.legal_name)
    mode = _attribution_mode(company.legal_name)
    discriminators = _discriminators(company) if mode == "tokens+discriminator" else []
    stats = {"attribution_mode": mode, "discriminators": discriminators, "headlines_fetched": len(items),
             "dropped_not_about_company": 0, "dropped_no_discriminator": 0, "dropped_finance_noise": 0}
    seen, kept = set(), []
    for it in items:
        headline = _headline_text(it["title"], it.get("outlet") or "")
        if not _is_about_company(headline, tokens):
            stats["dropped_not_about_company"] += 1
            continue
        if mode == "tokens+discriminator" and not _has_discriminator(headline, discriminators):
            stats["dropped_no_discriminator"] += 1
            continue
        # Share-price/results coverage says nothing about innovation capacity — unless
        # the same headline also carries a concept ("ricavi record grazie alla
        # partnership con..."), in which case the concept is the point.
        if _FINANCE_NOISE_RE.search(headline) and not any(p.search(headline) for p in CONCEPT_PATTERNS.values()):
            stats["dropped_finance_noise"] += 1
            continue
        key = headline.lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append({**it, "headline": headline})
    return kept, stats


def _company_headlines(company) -> list:
    """One request: every headline from the window that actually names the company."""
    lang, country = LOCALES.get(company.country or "", DEFAULT_LOCALE)
    name = _search_name(company.legal_name)
    items = _fetch(f'"{name}" when:{RECENCY_DAYS}d', lang, country)
    return _filter_headlines(items, company)[0]


def _cited(items: list) -> list:
    return [{"label": f"{i.get('headline', i['title'])} — {i['outlet']}" if i["outlet"] else i.get("headline", i["title"]),
             "url": i["url"]} for i in items]


def _bucket(headlines: list, concept: str) -> list:
    return [h for h in headlines if CONCEPT_PATTERNS[concept].search(h.get("headline", h["title"]) or "")]


def _fetch_live(company, captured: dict = None) -> dict:
    signals = {}
    skip_owned = bool(NEWSAPI_KEY)
    captured = captured if captured is not None else {}
    if not _name_is_distinctive(company.legal_name):
        captured["payload"] = {"skipped": "company name is too generic to attribute news coverage safely",
                               "search_phrase": _search_name(company.legal_name)}
        return {"signals": {}, "raw_payload": captured["payload"], "confidence": 0.5}
    lang, country = LOCALES.get(company.country or "", DEFAULT_LOCALE)
    search_phrase = _search_name(company.legal_name)
    items = _fetch(f'"{search_phrase}" when:{RECENCY_DAYS}d', lang, country)
    headlines, stats = _filter_headlines(items, company)

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

    captured["payload"] = {
        "search_phrase": search_phrase, "locale": f"{lang}-{country}", **stats,
        "headlines_about_company": len(headlines),
        "headlines_by_concept": {c: len(_bucket(headlines, c)) for c in CONCEPT_PATTERNS},
        # Kept so a zero-signal run leaves a trace of what it read — before this, a
        # company with three genuine headlines and no concept hit looked identical
        # to one the feed had never heard of.
        "kept_headlines": [{"headline": h["headline"], "outlet": h["outlet"], "url": h["url"],
                             "published": h["published"]} for h in headlines],
        "fetched_at": datetime.utcnow().isoformat() + "Z",
    }
    return {
        "signals": signals,
        "raw_payload": captured["payload"],
        "confidence": 0.55,  # a headline keyword match is weaker evidence than a read of the article
    }


def _simulate(company) -> dict:
    # No placeholder values: see the module docstring, and scrapers/node_crawler_base.py
    # on why a fabricated number is indistinguishable from a verified one at scoring time.
    return {"signals": {}, "raw_payload": {"note": "Google News RSS unreachable"}, "confidence": 0.5}


def sync_news_rss(company, db_session: Session) -> dict:
    captured = {}
    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=True,  # keyless public feed
        fetch_live=lambda c: _fetch_live(c, captured), simulate=_simulate, timeout=60,
    )
    if captured.get("payload"):
        # Same blob-per-(company, dataset) convention as the Phase 7 crawlers, so the
        # search phrase, attribution mode and every kept headline are inspectable on
        # the company page even when nothing was scored.
        save_crawler_blob(db_session, company, "crawler_news_rss", captured["payload"])
    return result
