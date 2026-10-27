"""
People and contact intelligence for Project Vienna (ticket I14, reworked by ticket I95).

Shared, company-agnostic helpers used by the `people` and `contacts` collectors. Nothing in this file may name a
company, a person, a VAT number or a web page of any benchmark company: the code is judged on companies it has not
seen (roadmap section 5, rule 6). A test (tests/test_intel_people.py) scans these files against every golden file.

Ground rules:
  1. No writes to the `company_people` table (management signals are computed from it).
  2. Only published business contact points; never guess an e-mail pattern for a person (decision G3).
  3. LinkedIn: no authenticated crawling and no LinkedIn-derived data (decision G2 is unanswered).
  4. Separate the company's own operating management from executives of another organisation (a parent, an acquirer).
  5. Rank who can sponsor a startup pilot (MD/GM > Tech/Ops > Commercial/Export > Purchasing).
  6. Support a suppression list for compliance.

The role vocabulary below is the single source for both classification (`classify_role`) and title extraction
(`TITLE_ALT`), so the two cannot drift apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

# Pilot sponsor rank definitions (1 = highest priority for pilot outreach, 5 = governance/parent)
PILOT_SPONSOR_RANKS = {
    "general_management": 1,        # MD, CEO, General Manager, Amministratore Delegato, Direttore Generale
    "operations_technology": 2,     # COO, CTO, Plant Manager, Tech Director, R&D, Direttore Tecnico, Direttore Produzione
    "commercial_export": 3,         # Commercial Director, Export Manager, Sales VP, Direttore Commerciale
    "purchasing_procurement": 4,    # Purchasing Director, Supply Chain Manager, Responsabile Acquisti
    "board_parent": 5,              # Board of Directors, Chairman, Parent Company Exec, Foreign Group Officer
    "signatory": 5,                 # Procuratore, Procuratore Speciale, Legal Representative
    "other_management": 6,          # finance, HR, marketing, quality: operating managers, not pilot sponsors
    "other": 6,
}

PILOT_SPONSOR_TIERS = {
    1: "Tier 1: General Management (CEO / MD / GM)",
    2: "Tier 2: Operations & Technology (CTO / Plant / Tech Dir)",
    3: "Tier 3: Commercial & Export (Export / Sales / Business Dev)",
    4: "Tier 4: Procurement & Purchasing (Purchasing / Supply Chain)",
    5: "Tier 5: Governance & Parent Board (Holding / Director / Signatory)",
    6: "Tier 6: Other / Advisory",
}

# Suppression list: contact points or names that must not be collected or emitted (G3 policy)
DEFAULT_SUPPRESSION_LIST: Set[str] = {
    "abuse@", "postmaster@", "hostmaster@", "privacy@", "dpo@", "rpd@", "gdpr@",
    "noreply@", "no-reply@", "donotreply@",
}


@dataclass
class PersonCandidate:
    full_name: str
    raw_role: str
    role_group: str                         # general_management, operations_technology, commercial_export, etc.
    normalized_role: str                   # e.g. "General Manager", "Managing Director", "Consigliere"
    is_operating: bool = True              # Local operational management vs parent/holding
    is_parent_director: bool = False       # Group/holding foreign executive
    pilot_sponsor_rank: int = 6            # 1..6 (1 = highest priority)
    pilot_sponsor_tier: str = "Tier 6"
    level: str = "executive"               # c-level, executive, director, board, signatory
    since: Optional[str] = None
    nationality: Optional[str] = None
    source_type: str = "company_site"
    source_url: Optional[str] = None
    quote: Optional[str] = None
    ref: Optional[str] = None              # for grade A registry
    notes: Optional[str] = None
    employer: Optional[str] = None         # organisation the SOURCE says the person works for, when it says one
    affiliation: str = "company"           # company | parent | other_org (employer is another organisation)
    entity_type: str = "person"            # person | organization (registry rows can be audit firms)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class ContactPoints:
    switchboard: Optional[str] = None
    switchboard_quote: Optional[str] = None
    generic_emails: List[str] = field(default_factory=list)
    email_quotes: Dict[str, str] = field(default_factory=dict)
    pec: Optional[str] = None
    pec_quote: Optional[str] = None
    contact_page: Optional[str] = None
    languages: List[str] = field(default_factory=list)
    locations: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v}


# ---------------------------------------------------------------------------
# Policy & Suppression checks (G2, G3)
# ---------------------------------------------------------------------------

def clean_name(name: str) -> str:
    """Clean title prefixes, conjunctions, and trailing noise from personal names."""
    if not name:
        return ""
    lines = [l.strip() for l in name.splitlines() if l.strip()]
    if lines:
        name = lines[-1]
    name = re.sub(r"^(?:and|e|ed|sig\.|signora|mr\.|ms\.|dott\.|ing\.)\s+", "", name.strip(), flags=re.I).strip()
    name = re.sub(r"\s+(?:currently|served|works)$", "", name, flags=re.I).strip()
    return name


def is_suppressed(item: str, extra_suppression: Optional[Iterable[str]] = None) -> bool:
    """Return True if the email, phone or name matches the suppression list."""
    if not item:
        return True
    lowered = item.strip().lower()
    if "there is no " in lowered or "nessun dato" in lowered:
        return True
    suppression = set(DEFAULT_SUPPRESSION_LIST)
    if extra_suppression:
        suppression.update(s.lower() for s in extra_suppression)
    for sup in suppression:
        if sup in lowered or lowered == sup:
            return True
    return False


_GENERIC_PREFIXES = (
    "info", "contact", "contacts", "contatti", "commerciale", "sales", "vendite",
    "export", "office", "ufficio", "amministrazione", "segreteria", "headquarters",
    "hq", "support", "servizio", "customercare", "marketing", "acquisti", "purchasing",
    "inquiry", "mail", "pec", "kontakt", "vertrieb", "buero", "service",
)
# A department word glued to the end of a brand ("<brand>info@", "<brand>sales@") is still a generic mailbox.
_GENERIC_SUFFIXES = ("info", "sales", "office", "contatti", "commerciale", "export", "service", "support")


def is_generic_business_email(email: str) -> bool:
    """True if email is a standard business department or general mailbox (never guessed personal)."""
    if not email or "@" not in email:
        return False
    local_part = email.split("@")[0].lower()
    if any(local_part == p or local_part.startswith(p + ".") or local_part.startswith(p + "_") or local_part.startswith(p + "-")
           for p in _GENERIC_PREFIXES):
        return True
    # "<word>info" without a dot: one token, so it cannot be "first.last"
    if "." not in local_part and "_" not in local_part:
        return local_part.startswith(_GENERIC_PREFIXES[:1]) or any(
            local_part.endswith(s) and len(local_part) > len(s) for s in _GENERIC_SUFFIXES)
    return False


# ---------------------------------------------------------------------------
# Role vocabulary: one list drives classification AND title extraction
# ---------------------------------------------------------------------------

# (phrases, role_group, normalized_role, is_operating, rank, usable_as_extraction_title)
# Order matters: the first entry whose phrase occurs in the role text wins, so specific entries come first.
ROLE_VOCAB: List[Tuple[Tuple[str, ...], str, str, bool, int, bool]] = [
    # Tier 1: general management
    (("general manager", "direttore generale", "general director"),
     "general_management", "General Manager", True, 1, True),
    (("managing director", "amministratore delegato", "amministratore unico", "chief executive officer", "ceo",
      "geschäftsführer", "geschaeftsfuehrer", "geschäftsleiter", "vorstandsvorsitzender"),
     "general_management", "Managing Director / CEO", True, 1, True),
    (("founder", "co-founder", "fondatore", "socio fondatore"),
     "general_management", "Founder / Owner", True, 1, True),
    (("executive chairman", "presidente operativo", "co-ceo", "direttore esecutivo", "consigliere delegato",
      "consigliere con delega"),
     "general_management", "Executive Director", True, 1, True),
    # Tier 2: operations and technology
    (("chief operating officer", "coo", "direttore operativo", "direttore di stabilimento", "direttore stabilimento",
      "plant manager", "betriebsleiter"),
     "operations_technology", "Operations / Plant Manager", True, 2, True),
    (("chief technology officer", "cto", "direttore tecnico", "technical director", "technischer leiter",
      "technischer direktor", "r&d director", "responsabile r&d", "engineering director", "direttore ingegneria",
      "direttore r&d"),
     "operations_technology", "Technical / R&D Director", True, 2, True),
    (("direttore produzione", "responsabile produzione", "production manager", "operations manager", "produktionsleiter"),
     "operations_technology", "Production / Operations Manager", True, 2, True),
    # Tier 3: commercial and export
    (("export manager", "manager export", "manager-export", "direttore export", "responsabile export", "exportleiter",
      "international sales"),
     "commercial_export", "Export Manager", True, 3, True),
    (("direttore commerciale", "responsabile commerciale", "commercial director", "chief commercial officer", "cco",
      "sales director", "sales manager", "direttore vendite", "vertriebsleiter"),
     "commercial_export", "Commercial Director", True, 3, True),
    (("business development", "responsabile sviluppo", "international development", "sviluppo internazionale"),
     "commercial_export", "Business Development Director", True, 3, True),
    # Tier 4: purchasing and procurement
    (("direttore acquisti", "responsabile acquisti", "purchasing manager", "procurement director",
      "head of supply chain", "supply chain manager", "einkaufsleiter", "leiter einkauf"),
     "purchasing_procurement", "Purchasing / Supply Chain Manager", True, 4, True),
    # Operating managers of other functions: real managers, not pilot sponsors
    (("chief financial officer", "cfo", "direttore finanziario", "direttore amministrativo", "responsabile amministrativo",
      "finance director", "hr director", "direttore risorse umane", "marketing director", "direttore marketing",
      "quality manager", "responsabile qualità", "responsabile qualita"),
     "other_management", "Functional Manager", True, 6, True),
    # Tier 5: governance, board and signatories
    (("vice presidente consiglio amministrazione", "vice presidente", "vicepresidente", "vice president"),
     "board_parent", "Vice President", False, 5, True),
    (("presidente consiglio amministrazione", "presidente del consiglio di amministrazione", "president", "chairman",
      "presidente"),
     "board_parent", "President / Chairman", False, 5, True),
    (("procuratore speciale", "procuratore"),
     "signatory", "Procuratore / Signatory", False, 5, True),
    (("presidente del collegio sindacale", "sindaco effettivo", "sindaco supplente", "collegio sindacale"),
     "board_parent", "Statutory Auditor", False, 5, False),
    (("consigliere", "board member", "director", "amministratore"),
     "board_parent", "Board Director", False, 5, False),
]


def _phrase_alternation(phrases: Iterable[str]) -> str:
    return "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in sorted(set(phrases), key=len, reverse=True))


# Compiled once: (pattern, role_group, normalized_role, is_operating, rank)
ROLE_PATTERNS: List[Tuple[re.Pattern, str, str, bool, int]] = [
    (re.compile(r"(?<!\w)(?:" + _phrase_alternation(phrases) + r")(?!\w)", re.I), grp, norm, op, rnk)
    for phrases, grp, norm, op, rnk, _ in ROLE_VOCAB
]

# Titles a text extractor may look for next to a person's name (bare "director" or "consigliere" are too vague)
TITLE_ALT: str = _phrase_alternation(p for phrases, _g, _n, _o, _r, usable in ROLE_VOCAB if usable for p in phrases)


# Words that follow "of" in "Director of Operations": a function, not an employer
_FUNCTION_WORDS = {
    "sales", "operations", "operation", "production", "quality", "purchasing", "procurement", "supply", "chain",
    "finance", "financial", "marketing", "technology", "technical", "engineering", "international", "business",
    "development", "human", "resources", "it", "r&d", "research", "logistics", "manufacturing", "export",
    "commercial", "administration", "vendite", "produzione", "qualità", "qualita", "acquisti", "finanza",
    "marketing", "sviluppo", "ricerca", "logistica", "vertrieb", "einkauf", "produktion", "forschung",
}

_EMPLOYER_RE = re.compile(
    r"^(?P<role>.+)\s+(?:of|at|di|presso|bei|von|chez)\s+(?P<org>[A-Z0-9][\w&.,'’\- ]{1,70})$", re.U)


def split_role_employer(raw_role: str) -> Tuple[str, Optional[str]]:
    """'CEO of Some Corporation' -> ('CEO', 'Some Corporation'). 'Director of Operations' keeps its function and
    returns no employer. Used so an executive of ANOTHER organisation is never filed as this company's manager."""
    text = re.sub(r"\s+", " ", (raw_role or "").strip())
    m = _EMPLOYER_RE.match(text)
    if not m:
        return text, None
    role, org = m.group("role").strip(" ,;"), m.group("org").strip(" ,;.")
    if not role or not org:
        return text, None
    # the part before "of/at" must itself be a recognised title, otherwise "Head of Sales" would parse as employer "Sales"
    if not any(pat.search(role) for pat, *_ in ROLE_PATTERNS):
        return text, None
    org_words = [w.lower() for w in re.split(r"[\s/]+", org) if w]
    if org_words and all(w in _FUNCTION_WORDS or w in {"and", "e", "&", "the", "of", "di"} for w in org_words):
        return role, None
    return role, org


_LEGAL_FORM_TAIL = re.compile(
    r"[\s,]+(?:s\.?\s?p\.?\s?a\.?|s\.?\s?r\.?\s?l\.?\s?s?\.?|s\.?\s?a\.?\s?s\.?|s\.?\s?n\.?\s?c\.?|gmbh|ag|ltd|llc|inc\.?|corp\.?)\s*\.?\s*$",
    re.I)


def strip_legal_form(name: str) -> str:
    """'Rossi Meccanica S.p.A.' -> 'Rossi Meccanica'."""
    out = re.sub(r"\s+", " ", (name or "").strip())
    for _ in range(2):
        out = _LEGAL_FORM_TAIL.sub("", out).strip(" ,.")
    return out


def company_names(company: Dict[str, Any]) -> List[str]:
    """The names a page may use for the company: the legal name without its legal form, and the brand when given.
    Never a single short token invented from the legal name (that is how same-brand firms get mixed up)."""
    names: List[str] = []
    for raw in (company.get("legal_name"), company.get("brand")):
        clean = strip_legal_form(str(raw or ""))
        if len(clean) >= 4 and clean.lower() not in {n.lower() for n in names}:
            names.append(clean)
    return names


def name_in_text(names: Sequence[str], text: str) -> bool:
    low = re.sub(r"\s+", " ", text or "").lower()
    return any(re.search(r"(?<!\w)" + re.escape(re.sub(r"\s+", " ", n).lower()) + r"(?!\w)", low) for n in names if n)


def employer_is_company(employer: Optional[str], names: Sequence[str]) -> bool:
    """True when the organisation named as employer is the target company itself."""
    if not employer:
        return True
    emp = strip_legal_form(employer).lower()
    for n in names:
        nl = n.lower()
        if emp == nl or nl in emp or (len(emp) >= 4 and emp in nl):
            return True
    return False


_ORG_WORDS = re.compile(
    r"\b(s\.?p\.?a\.?|s\.?r\.?l\.?s?|s\.?a\.?s\.?|s\.?n\.?c\.?|gmbh|ag|ltd|llc|inc|corp|corporation|company|group|"
    r"gruppo|holding|associati|associates|studio|revisione|audit|auditors|consulting|partners|foundation|fondazione)\b",
    re.I)


def looks_like_organization(name: str) -> bool:
    """Registry 'people' rows can be audit firms; a firm is not a person to call."""
    if not name:
        return False
    return bool(_ORG_WORDS.search(name)) or "&" in name


# ---------------------------------------------------------------------------
# Person-name recognition for web text (case-sensitive: web names are written "Mario Rossi")
# ---------------------------------------------------------------------------

_UP = "A-ZÀ-ÖØ-Þ"
_LOW = "a-zà-öø-ÿ"
NAME_TOKEN = (rf"(?:[{_UP}]['’][{_UP}][{_LOW}]+|[{_UP}][{_LOW}'’]+(?:-[{_UP}][{_LOW}'’]+)?)")
NAME_PARTICLE = r"(?:de|di|da|del|della|dal|dalla|degli|dei|van|von|der|den|la|le)"
# 2 to 4 capitalised tokens on ONE line (a line break ends a name)
NAME_RE: str = rf"{NAME_TOKEN}(?:[^\S\n]+(?:{NAME_PARTICLE}[^\S\n]+)?{NAME_TOKEN}){{1,3}}"

NAME_STOPWORDS: Set[str] = {w for w in """
january february march april may june july august september october november december gennaio febbraio marzo
aprile maggio giugno luglio agosto settembre ottobre novembre dicembre monday tuesday wednesday thursday friday
contact contacts contatti contatto home news about team azienda company group gruppo corporation italia italy europe
europa america usa united states sales marketing operations production quality service services customer support
technical technology industrial industriale industries industria manufacturing engineering international global
business development management director manager general managing executive chief officer president presidente
chairman board consiglio amministratore delegato direttore responsabile head vice senior junior assistant assistente
ufficio office department dipartimento reparto chi siamo our your the and for with from cookie privacy policy terms
read more leggi scopri vai view all tutti download catalog catalogo brochure video press stampa mission vision valori
storia history prodotti products soluzioni solutions applicazioni applications settori markets mercati sede
headquarters legal notice note legali credits sitemap login search cerca menu scrivici richiedi richiesta quote
preventivo newsletter social follow seguici copyright rights reserved holding spa srl gmbh ltd inc llc
""".split()}


def plausible_person_name(name: str, extra_stop: Iterable[str] = ()) -> bool:
    """Cheap sanity gate for a name taken from web text: 2 to 5 tokens, none a heading or role word, not a company."""
    tokens = [t for t in re.split(r"\s+", (name or "").strip()) if t]
    if not 2 <= len(tokens) <= 5:
        return False
    stop = NAME_STOPWORDS | {s.lower() for s in extra_stop}
    for t in tokens:
        if t.lower().strip(".,'’") in stop:
            return False
    return not looks_like_organization(name)


def classify_role(
    raw_role: str,
    nationality: Optional[str] = None,
    parent_company: Optional[str] = None,
    context_text: Optional[str] = None
) -> Tuple[str, str, bool, bool, int, str]:
    """
    Classifies a raw job title or registry role.
    Returns:
      (role_group, normalized_role, is_operating, is_parent_director, rank, tier_label)
    """
    role_str = (raw_role or "").strip()
    norm_role = role_str
    group = "other"
    is_operating = False
    rank = 6

    for pat, grp, norm, op, rnk in ROLE_PATTERNS:
        if pat.search(role_str):
            group = grp
            norm_role = norm
            is_operating = op
            rank = rnk
            break

    # Determine if person is parent/foreign group executive:
    is_parent = False
    nat = (nationality or "").strip().lower()
    if nat and nat not in ("italy", "italia", "it", ""):
        is_parent = True
    if parent_company and parent_company.lower() in (context_text or "").lower():
        is_parent = True
    if re.search(r"\b(holding|parent company|corporate officer|group\s+(?:ceo|cfo|coo|president|chairman|managing director))\b",
                 role_str, re.I):
        is_parent = True

    # Foreign or parent company board members are non-operating parent directors
    if is_parent and not is_operating:
        group = "board_parent"
        rank = 5

    tier_label = PILOT_SPONSOR_TIERS.get(rank, f"Tier {rank}")
    return group, norm_role, is_operating, is_parent, rank, tier_label


def is_classified_title(raw_role: str) -> bool:
    """True when the role text contains a title from the vocabulary (web candidates without one are dropped)."""
    return any(pat.search(raw_role or "") for pat, *_ in ROLE_PATTERNS)


def rank_pilot_sponsors(candidates: Sequence[PersonCandidate]) -> List[PersonCandidate]:
    """Sort candidates by pilot sponsor priority (rank 1 first, then operating management, then alphabetical)."""
    return sorted(candidates, key=lambda c: (c.pilot_sponsor_rank, not c.is_operating, c.full_name))


# ---------------------------------------------------------------------------
# Contact Extraction Helpers (Phones, Emails, PEC, Languages)
# ---------------------------------------------------------------------------

PHONE_PATTERN = re.compile(
    r"""(?:\+39\s*)?(?:0\d{1,4}[/\s.-]?\d{4,8}|\b0\d{1,4}\s*\d{5,8}\b)"""
)

EMAIL_PATTERN = re.compile(
    r"""\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"""
)

PEC_DOMAINS = (
    "pec.it", "legalmail.it", "sicurezzapostale.it", "pec.aruba.it",
    "cert.ip-veneto.net", "pec.net", "mypec.eu", "postecert.it",
)


# A number printed next to one of these is a tax or registry identifier, never a telephone number (an 11-digit Italian
# VAT number looks exactly like a long landline).
_NOT_A_PHONE_CONTEXT = re.compile(
    r"(p\.?\s?iva|partita\s+iva|\bvat\b|\biva\b|c\.?\s?f\.?\b|codice\s+fiscale|\brea\b|r\.?e\.?a\.?\b|registro\s+imprese|"
    r"reg\.?\s*imp|cap\.?\s*soc|capitale\s+sociale|\bsdi\b|\bcod\.?\s*dest)", re.I)


def _is_phone_context(text: str, start: int, raw: str) -> bool:
    """False when the digits sit in a VAT / tax-code / registry line or start with the international prefix without a +."""
    # only the label RIGHT BEFORE the digits counts: "P.IVA 0067... - Tel. 0471 ..." must still accept the telephone
    if _NOT_A_PHONE_CONTEXT.search(text[max(0, start - 14): start]):
        return False
    digits = re.sub(r"\D", "", raw)
    if not raw.strip().startswith("+") and digits.startswith("00"):
        return False
    return True


def extract_phones(text: str) -> List[Tuple[str, str]]:
    """Extract candidate telephone numbers with exact quotes from the text."""
    if not text:
        return []
    results = []
    seen = set()

    # Look for labelled phone patterns first
    label_patterns = [
        re.compile(r"""(?:tel|phone|ph|chiamaci|call us|telefono)\s*[:.]?\s*(\+?39[/\s.-]?\d{2,4}[/\s.-]?\d{4,8}|\b0\d{1,4}[/\s.-]?\d{5,8}\b)""", re.I),
        re.compile(r"""(\+39\s*0\d{1,3}\s*\d{5,8})""", re.I),
        re.compile(r"""(\+39\s*\d{9,11})""", re.I),
    ]

    for pat in label_patterns:
        for m in pat.finditer(text):
            raw = m.group(1).strip()
            digits = re.sub(r"\D", "", raw)
            if len(digits) < 8 or len(digits) > 13:
                continue
            if not _is_phone_context(text, m.start(1), raw):
                continue
            norm = _normalize_italian_phone(raw)
            quote = m.group(0).strip()
            if norm not in seen and not is_suppressed(norm):
                seen.add(norm)
                results.append((norm, quote))

    # Fallback to general phone regex if none found
    if not results:
        for m in PHONE_PATTERN.finditer(text):
            raw = m.group(0).strip()
            digits = re.sub(r"\D", "", raw)
            if 8 <= len(digits) <= 12 and _is_phone_context(text, m.start(), raw):
                norm = _normalize_italian_phone(raw)
                if norm not in seen and not is_suppressed(norm):
                    seen.add(norm)
                    results.append((norm, raw))

    return results


def _normalize_italian_phone(raw: str) -> str:
    cleaned = re.sub(r"[^\d+]", " ", raw).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned.startswith("+"):
        if cleaned.startswith("39 "):
            cleaned = "+" + cleaned
        elif cleaned.startswith("0"):
            cleaned = "+39 " + cleaned
    return cleaned


def extract_emails(text: str) -> List[Tuple[str, str]]:
    """Extract valid published business email addresses with exact quotes."""
    if not text:
        return []
    results = []
    seen = set()
    for m in EMAIL_PATTERN.finditer(text):
        email = m.group(0).lower().strip()
        # Filter image filenames or invalid extensions
        if any(email.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf")):
            continue
        if "example.com" in email or "sito.it" in email or "dominio.it" in email:
            continue
        if is_suppressed(email):
            continue
        if email not in seen:
            seen.add(email)
            results.append((email, m.group(0)))
    return results


def extract_pec(text: str) -> Optional[Tuple[str, str]]:
    """Extract Certified Email (PEC) with exact quote."""
    if not text:
        return None
    # 1. Labelled PEC: "pec: name@pec.it" or "posta elettronica certificata: ..."
    labelled = re.search(r"""(?:pec|posta\s*elettronica\s*certificata)\s*[:.\-]?\s*([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})""", text, re.I)
    if labelled:
        pec = labelled.group(1).lower().strip()
        quote = labelled.group(0).strip()
        return pec, quote

    # 2. Known PEC domains
    for email, quote in extract_emails(text):
        domain = email.split("@")[-1].lower()
        if any(domain == d or domain.endswith("." + d) for d in PEC_DOMAINS) or "pec" in domain:
            return email, quote

    return None


def detect_languages(html_or_text: str, links: Sequence[str]) -> List[str]:
    """Detect published languages from HTML lang attributes, text and navigation links."""
    langs = set()
    # Check HTML lang attribute
    lang_m = re.search(r"""<html[^>]+lang=["']([a-zA-Z]{2})(?:-[a-zA-Z]{2})?["']""", html_or_text, re.I)
    if lang_m:
        langs.add(lang_m.group(1).lower())

    # Check links for language codes
    for l in links:
        path = urlparse(l).path.lower()
        for code in ("it", "en", "de", "fr", "es"):
            if f"/{code}/" in path or path.endswith(f"/{code}") or f"lang={code}" in l.lower():
                langs.add(code)

    # Text cues
    lowered = html_or_text.lower()
    if "contattaci" in lowered or "chi siamo" in lowered or "azienda" in lowered:
        langs.add("it")
    if "contact us" in lowered or "about us" in lowered or "our company" in lowered:
        langs.add("en")
    if "kontakt" in lowered or "über uns" in lowered:
        langs.add("de")
    if "contactez-nous" in lowered or "à propos" in lowered:
        langs.add("fr")

    order = ["it", "en", "de", "fr", "es"]
    return [c for c in order if c in langs] or (["it"] if "p.iva" in lowered or "c.f." in lowered else ["en"])


# Cue phrases per language. A language is asserted only when at least two different cues occur on the page, and the
# quote is a real line of the page containing a cue, so the evidence shows the page IS written in that language.
LANGUAGE_CUES: Dict[str, Tuple[str, ...]] = {
    "it": ("chi siamo", "contattaci", "i nostri prodotti", "sede legale", "partita iva", "p.iva", "telefono", "azienda"),
    "en": ("contact us", "about us", "our company", "our products", "registered office", "vat number", "phone"),
    "de": ("kontakt", "über uns", "unser unternehmen", "unsere produkte", "firmensitz", "umsatzsteuer", "telefon"),
    "fr": ("contactez-nous", "à propos", "nos produits", "siège social", "numéro de tva", "téléphone"),
}


def language_evidence(text: str) -> List[Tuple[str, str]]:
    """[(language_code, verbatim_line)] for every language with at least two distinct cues on the page."""
    out: List[Tuple[str, str]] = []
    if not text:
        return out
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lowered_lines = [ln.lower() for ln in lines]
    for lang, cues in LANGUAGE_CUES.items():
        hit_cues = [c for c in cues if any(c in ln for ln in lowered_lines)]
        if len(hit_cues) < 2:
            continue
        for i, ln in enumerate(lowered_lines):
            if len(lines[i]) >= 12 and any(c in ln for c in hit_cues):
                out.append((lang, lines[i][:160]))
                break
    return out
