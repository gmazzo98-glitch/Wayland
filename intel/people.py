"""
People and Contact Intelligence for Project Vienna (Ticket I14).

Ground rules:
  1. No writes to `company_people` table (management signals are computed from it).
  2. Only published business contact points; never guess an email pattern for a person (G3).
  3. LinkedIn: public pages and search snippets only; keep authenticated crawler disabled (G2).
  4. Classify local operating management vs parent-company directors.
  5. Rank who can sponsor a startup pilot (MD/GM > Tech/Ops > Commercial/Export > Purchasing).
  6. Support suppression list for compliance.
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


def is_generic_business_email(email: str) -> bool:
    """True if email is a standard business department or general mailbox (never guessed personal)."""
    if not email or "@" not in email:
        return False
    local_part = email.split("@")[0].lower()
    generic_prefixes = (
        "info", "contact", "contacts", "contatti", "commerciale", "sales", "vendite",
        "export", "office", "ufficio", "amministrazione", "segreteria", "headquarters",
        "hq", "support", "servizio", "customercare", "marketing", "acquisti", "purchasing",
        "ascoinfo", "inquiry", "mail", "pec",
    )
    return any(local_part == p or local_part.startswith(p + ".") or local_part.startswith(p + "_") or local_part.startswith(p + "-")
               for p in generic_prefixes)


# ---------------------------------------------------------------------------
# Role Classification and Pilot Sponsorship Ranking
# ---------------------------------------------------------------------------

# Role keywords mapped to (role_group, normalized_role, is_operating, default_rank)
ROLE_PATTERNS: List[Tuple[re.Pattern, str, str, bool, int]] = [
    # Tier 1: General Management / C-Level
    (re.compile(r"\b(general manager|direttore generale)\b", re.I),
     "general_management", "General Manager", True, 1),
    (re.compile(r"\b(managing director|amministratore delegato|ceo|chief executive officer)\b", re.I),
     "general_management", "Managing Director / CEO", True, 1),
    (re.compile(r"\b(presidente operativo|executive chairman|co-ceo|direttore esecutivo)\b", re.I),
     "general_management", "Executive Director", True, 1),

    # Tier 2: Operations & Technology
    (re.compile(r"\b(chief operating officer|coo|direttore operativo|direttore di stabilimento|plant manager)\b", re.I),
     "operations_technology", "Operations / Plant Manager", True, 2),
    (re.compile(r"\b(chief technology officer|cto|direttore tecnico|technical director|r&d director|responsabile r&d|engineering director|direttore ingegneria)\b", re.I),
     "operations_technology", "Technical / R&D Director", True, 2),
    (re.compile(r"\b(direttore produzione|production manager|operations manager)\b", re.I),
     "operations_technology", "Production / Operations Manager", True, 2),

    # Tier 3: Commercial & Export
    (re.compile(r"\b(manager[- ]?export|export manager|direttore export|responsabile export|international sales)\b", re.I),
     "commercial_export", "Export Manager", True, 3),
    (re.compile(r"\b(direttore commerciale|commercial director|chief commercial officer|cco|sales director|direttore vendite)\b", re.I),
     "commercial_export", "Commercial Director", True, 3),
    (re.compile(r"\b(business development|responsabile sviluppo|international development|sviluppo internazionale)\b", re.I),
     "commercial_export", "Business Development Director", True, 3),

    # Tier 4: Purchasing & Procurement
    (re.compile(r"\b(direttore acquisti|responsabile acquisti|purchasing manager|procurement director|head of supply chain|supply chain manager)\b", re.I),
     "purchasing_procurement", "Purchasing / Supply Chain Manager", True, 4),

    # Tier 5: Governance, Board & Parent Group Officers
    (re.compile(r"\b(presidente consiglio amministrazione|president|chairman|presidente)\b", re.I),
     "board_parent", "President / Chairman", False, 5),
    (re.compile(r"\b(vice presidente consiglio amministrazione|vice presidente|vice president)\b", re.I),
     "board_parent", "Vice President", False, 5),
    (re.compile(r"\b(consigliere con delega|consigliere delegato)\b", re.I),
     "general_management", "Managing Director", True, 1),
    (re.compile(r"\b(consigliere|board member|director|amministratore)\b", re.I),
     "board_parent", "Board Director", False, 5),
    (re.compile(r"\b(procuratore speciale|procuratore)\b", re.I),
     "signatory", "Procuratore / Signatory", False, 5),
    (re.compile(r"\b(sindaco effettivo|sindaco supplente|presidente del collegio sindacale|collegio sindacale)\b", re.I),
     "board_parent", "Statutory Auditor", False, 5),
]


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
    if re.search(r"\b(mott|idex|holding|group|parent|corporate officer)\b", role_str, re.I):
        is_parent = True

    # Foreign or parent company board members are non-operating parent directors
    if is_parent and not is_operating:
        group = "board_parent"
        rank = 5

    tier_label = PILOT_SPONSOR_TIERS.get(rank, f"Tier {rank}")
    return group, norm_role, is_operating, is_parent, rank, tier_label


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
            if 8 <= len(digits) <= 12:
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
    # 1. Labelled PEC: "pec: asco@pec.it" or "posta certificata: ..."
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
