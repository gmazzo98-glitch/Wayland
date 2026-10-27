"""
Verification gate for deep-intelligence facts (docs/INTEL_ROADMAP.md, ticket I06).

Every fact collected from the open web or third-party sources must pass this gate before
it is treated as verified truth in the evidence ledger or displayed in a dossier.

Checks performed:
  1. Quote grounding: the verbatim quote in Evidence must actually occur in page_text.
  2. Entity match: legal name or brand MUST occur, along with at least ONE discriminator
     (VAT, city/province, registered domain, or known person).
  3. Name-collision detection (generic, I93): the brand embedded in another entity's compound
     name, a same-brand different legal entity, or a person/news homonym — no per-company table.
  4. Date sanity: checks that event years are not in the future and not impossibly ancient.
  5. Numeric sanity: checks employee count (factor of 3) and revenue (factor of 2) against AIDA.
  6. Contradiction handling: detects conflicts against existing verified single-valued ledger facts
     (flags the contradiction without silent overwriting).
  7. Optional LLM judge: resolves borderline ambiguity via the LLM pool, with cache.

Rule: This gate sets fact.verified, adjusts fact.confidence, records reasons, and NEVER raises.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from pathlib import Path
from urllib.parse import urlparse

from intel.ledger import Fact, Ledger, SINGLE_VALUED_KEYS, quote_in_text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Documented Constants and Thresholds
# ---------------------------------------------------------------------------

MIN_QUOTE_LENGTH: int = 12
MAX_EMPLOYEE_FACTOR: float = 3.0
MIN_EMPLOYEE_FACTOR: float = 1.0 / 3.0
MAX_REVENUE_FACTOR: float = 2.0
MIN_REVENUE_FACTOR: float = 0.5

# Earliest sensible year for modern industrial entities (unless explicitly ancient craft)
MIN_PLAUSIBLE_YEAR: int = 1850

# Baseline confidence adjustments
MIN_CONFIDENCE_FOR_VERIFIED: float = 0.65
CONFIDENCE_GROUNDING_FAILED: float = 0.05
CONFIDENCE_COLLISION_PENALTY: float = 0.50
CONFIDENCE_NO_DISCRIMINATOR_PENALTY: float = 0.35
CONFIDENCE_NUMERIC_SANITY_PENALTY: float = 0.30
CONFIDENCE_DATE_SANITY_PENALTY: float = 0.30

# In-memory cache for LLM judge results: key = sha1(prompt) -> bool
_LLM_JUDGE_CACHE: Dict[str, Tuple[bool, str]] = {}


@dataclass
class Verdict:
    verified: bool
    confidence: float
    decision: str  # "verified", "rejected", "unverified", "contradiction"
    reasons: List[str] = field(default_factory=list)
    grounded: bool = False
    entity_matched: bool = False
    discriminators_found: List[str] = field(default_factory=list)
    collision_detected: bool = False
    collision_details: Optional[str] = None
    date_sane: bool = True
    numeric_sane: bool = True
    contradiction_detected: bool = False
    contradiction_details: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verified": self.verified,
            "confidence": round(self.confidence, 4),
            "decision": self.decision,
            "reasons": self.reasons,
            "grounded": self.grounded,
            "entity_matched": self.entity_matched,
            "discriminators_found": self.discriminators_found,
            "collision_detected": self.collision_detected,
            "collision_details": self.collision_details,
            "date_sane": self.date_sane,
            "numeric_sane": self.numeric_sane,
            "contradiction_detected": self.contradiction_detected,
            "contradiction_details": self.contradiction_details,
        }


# ---------------------------------------------------------------------------
# Entity and Discriminator Extraction Helpers
# ---------------------------------------------------------------------------

_LEGAL_SUFFIXES = re.compile(
    r"\b(s\.?p\.?a\.?|s\.?r\.?l\.?|s\.?a\.?s\.?|s\.?n\.?c\.?|"
    r"societ[aà]\s+(per\s+azioni|a\s+responsabilit[aà]\s+limitata)|"
    r"in\s+forma\s+abbreviata|in\s+breve|o\s+in\s+forma\s+abbreviata|gmbh)\b",
    re.IGNORECASE,
)


def extract_brand_tokens(legal_name: str) -> List[str]:
    """Extract recognizable brand and short-name tokens from a formal Italian legal name."""
    if not legal_name:
        return []
    clean = _LEGAL_SUFFIXES.sub("", legal_name).strip()
    clean = re.sub(r"[,\.\(\)\-\–\—\"\'\*\#]", " ", clean)
    tokens = [t.strip() for t in clean.split() if len(t.strip()) > 1]
    candidates: List[str] = []
    if len(tokens) >= 2:
        candidates.append(" ".join(tokens[:2]))
    if tokens:
        candidates.append(tokens[0])
    full_clean = re.sub(r"\s+", " ", clean).strip()
    if full_clean and full_clean not in candidates:
        candidates.insert(0, full_clean)
    return [c.lower() for c in candidates if len(c) >= 3]


def normalize_vat(vat: Optional[str]) -> str:
    """Normalize VAT/tax code by stripping country prefixes and leading symbols."""
    if not vat:
        return ""
    digits = re.sub(r"[^0-9A-Za-z]", "", str(vat)).upper()
    if digits.startswith("IT"):
        digits = digits[2:]
    return digits


def extract_domain(url: Optional[str]) -> str:
    """Extract registrable root domain or host from URL."""
    if not url:
        return ""
    try:
        host = urlparse(url if "://" in url else f"http://{url}").netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host.split(":")[0]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Generic Name-Collision Detection (the T20 problem), ticket I93
#
# The I06 per-brand table (KNOWN_COLLISION_RULES) was removed: it hard-coded the golden companies and their
# owners, and did not generalise to a namesake page of any brand it had not seen. Detection is now generic,
# from the page itself and the company profile only:
#   C1 embedded-brand: the brand token is part of a LARGER compound entity name on the page ("Emerson {brand}",
#      "{brand} Numatics", "{brand} Group"), when the company's own full name/legal form is not also present;
#   C2 other-entity: the page names a DIFFERENT legal entity (different legal-form suffix) around the brand
#      token while the target's own full name or legal form is absent;
#   C3 person/news homonym: the brand token is used as a PERSON name in a person-context (sports, arts,
#      politics, family), with no corporate context for the target.
# Each signal is overridden by STRONG evidence the page is really about the target: the full brand name,
# the company's own legal form adjacent to the brand, VAT, registered domain host, or a known person.
# Lexical hints (legal-form suffixes, person-context markers, stopwords) live in intel/verify_lexicon.json
# as data and contain NO company names.
# ---------------------------------------------------------------------------

_LEXICON_PATH = Path(__file__).resolve().parent / "verify_lexicon.json"


def _load_lexicon() -> Dict[str, List[str]]:
    try:
        data = json.loads(Path(_LEXICON_PATH).read_text(encoding="utf-8"))
        return {k: list(v) for k, v in data.items() if isinstance(v, list)}
    except (OSError, ValueError):
        return {"legal_form_suffixes": [], "person_context_markers": [], "cap_stopwords": []}


_LEXICON = _load_lexicon()
_LEGAL_FORMS = _LEXICON.get("legal_form_suffixes", [])
_PERSON_MARKERS = _LEXICON.get("person_context_markers", [])
_CAP_STOPWORDS = set(w.strip().lower() for w in _LEXICON.get("cap_stopwords", []) if w.strip())


def _full_brand_variants(legal_name: str) -> Set[str]:
    """The company's full name without the legal suffix, lowercase (e.g. 'brand word product word')."""
    clean = _LEGAL_SUFFIXES.sub(" ", legal_name or "").strip()
    clean = re.sub(r"[,.()\-\u2013\u2014\"'\*\#]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip().lower()
    return {clean} if len(clean) >= 3 else set()


def _norm_form(token: str) -> str:
    return re.sub(r"[^a-z]", "", (token or "").lower())


def _is_legal_form(token: str) -> bool:
    t = _norm_form(token)
    return bool(t) and any(_norm_form(f) == t for f in _LEGAL_FORMS)


def _own_legal_forms(legal_name: str) -> Set[str]:
    """The legal-form suffixes the target itself uses ('s.r.l.' for an S.R.L.)."""
    return {m.group(0).strip().lower() for m in _LEGAL_SUFFIXES.finditer(legal_name or "")}


def _legal_form_after(page_lower: str, name: str) -> Optional[str]:
    """The legal-form token immediately following `name` on the page, if any ('{name} s.r.l.'),
    longest legal-form match first so dotted suffixes are not cut short."""
    if not name:
        return None
    for form in sorted(_LEGAL_FORMS, key=len, reverse=True):
        if re.search(re.escape(name) + r"[\s,]{0,3}" + re.escape(form) + r"\b", page_lower):
            return form
    return None


def _own_legal_form_on_page(page_lower: str, brand: str, full_brand: str, own_forms: Set[str] = None) -> bool:
    """True when the target's FULL distinct name carries the target's OWN legal-form suffix
    ('{full name} s.r.l.' for an S.R.L.) — the page names the target as a company.
    A different legal form after the full name ('{full name} spa' for an S.R.L.) means a
    DIFFERENT entity with the same name, not the target (the same-brand-different-form signal)."""
    if not full_brand:
        return False
    after = _legal_form_after(page_lower, full_brand)
    if after is None:
        return False
    if not own_forms:
        return True    # target's own form unknown: any legal form still means 'named as a company'
    own_norm = {_norm_form(f) for f in own_forms}
    return _norm_form(after) in own_norm


def _compound_entity_hits(page_text: str, brand: str, full_brand: str = "",
                          known_people: Optional[List[str]] = None) -> List[str]:
    """Occurrences of the brand inside a LARGER compound proper name (another entity embedding it).
    A compound is '{Capitalized} {brand}' or '{brand} {Capitalized}' where the neighbouring token is
    capitalised in the original text (a proper-name token, not an ordinary sentence word:
    'BRAND manufactures' is not an entity, 'Othercorp BRAND' is). The two tokens must sit on the SAME
    line with no sentence boundary between them (ticket I100): extracted text puts the headline on
    its own line, so 'Orion\nMeccanica' is headline-then-body, not the entity 'Orion Meccanica'."""
    hits: List[str] = []
    word = r"([A-Za-z][a-zA-Z0-9&\-]+)"
    gap = r"[^\S\n]+"    # horizontal whitespace only: a compound never crosses a line break or a sentence end
    for m in re.finditer(r"(?<![A-Za-z0-9])(" + word + gap + re.escape(brand) + r")(?![a-z0-9])", page_text, re.I):
        prefix = m.group(2)
        if prefix.lower() in _CAP_STOPWORDS or len(prefix) <= 1:
            continue
        if not (prefix[:1].isupper() and not prefix.isupper()):
            continue    # a compound entity word is Capitalized, not ALLCAPS fragments or lowercase
        if full_brand and m.group(1).lower().endswith(full_brand):
            continue    # the target's own full name is not a foreign compound
        if any(m.group(1).lower().endswith(p) for p in (known_people or [])):
            continue    # a compound ending with a known person's name is the person, not an entity
        hits.append(m.group(1))
    for m in re.finditer(r"(?<![A-Za-z0-9])(" + re.escape(brand) + gap + word + r")(?![a-z0-9])", page_text, re.I):
        suffix = m.group(2)
        if suffix.lower() in _CAP_STOPWORDS or len(suffix) <= 1:
            continue
        if not (suffix[:1].isupper() and not suffix.isupper()):
            continue
        if full_brand and m.group(1).lower().startswith(full_brand):
            continue    # the target's own full name is not a foreign compound
        if any(m.group(1).lower().startswith(p) for p in (known_people or [])):
            continue    # a compound starting with a known person's name is the person, not an entity
        hits.append(m.group(1))
    return hits


def _person_context_hits(page_lower: str, brand: str) -> List[str]:
    """The brand is used as a person's name inside a person/news context."""
    if not _PERSON_MARKERS:
        return []
    markers_rx = "|".join(re.escape(m) for m in _PERSON_MARKERS)
    hits: List[str] = []
    for m in re.finditer(markers_rx, page_lower):
        start = max(0, m.start() - 100)
        window = page_lower[start:m.end() + 100]
        if re.search(r"(?<![a-z0-9])" + re.escape(brand) + r"(?![a-z0-9])", window):
            hits.append(m.group(0))
    return hits


def check_known_collisions(
    brand: str,
    page_text: str,
    company: Optional[Dict[str, Any]] = None,
    strong_discriminators: Optional[Set[str]] = None,
    known_people: Optional[List[str]] = None,
) -> Optional[str]:
    """Generic namesake detection. Returns a human-readable collision reason or None.

    `brand` is the primary brand token; `company` (optional) provides the full legal name for the
    strong-evidence override; `strong_discriminators` (optional) are already-found discriminators
    (vat / domain / person) that override every collision signal; `known_people` (optional) are
    the company's known people — a compound formed with a known person's name is the person,
    not a foreign entity.
    Signature kept backward-compatible with the I06 call shape (ticket I93 acceptance).
    """
    if known_people is None:
        known_people = list((company or {}).get("people") or [])
    known_people = [str(p).lower() for p in known_people if len(str(p)) >= 3]
    if not brand:
        return None
    brand = brand.strip().lower()
    page_clean = (page_text or "").strip()
    page_lower = page_clean.lower()
    if brand not in page_lower:
        return None
    strong = set(strong_discriminators or ())
    if {"vat", "domain"} & strong:
        return None    # the target's own VAT or registered domain: the page is about the target
    person_strong = any(d.startswith("person:") for d in strong)
    full_brand_set = _full_brand_variants((company or {}).get("legal_name") or brand)
    full_brand = next(iter(full_brand_set), brand)
    own_forms = _own_legal_forms((company or {}).get("legal_name") or "")
    full_name_present = full_brand != brand and full_brand in page_lower
    if company and full_name_present:
        # The target's full distinct name IS on the page: named as a company with its own legal form?
        if _own_legal_form_on_page(page_lower, brand, full_brand, own_forms):
            return None
        # same full name with a DIFFERENT legal form = another entity with the same name
        other_form = _legal_form_after(page_lower, full_brand)
        own_norm = {_norm_form(f) for f in own_forms}
        if other_form and _norm_form(other_form) not in own_norm:
            return ("Name-collision: a different legal entity with the same name ("
                    f"{full_brand} {other_form}) while the target's own legal form "
                    f"({', '.join(sorted(own_forms)) or 'unknown'}) never appears")
        # full name standalone: only an INCORPORATED namesake (a compound that carries or is
        # followed by a legal form on the page) still rejects; an explicit disambiguation
        # ("non confondere con", "not to be confused with") names the namesake on purpose and
        # never rejects the page; a bare passing mention does not reject either
        if not re.search(r"non confund|non confondere|not to be confused|da non confondere", page_lower):
            compounds = _compound_entity_hits(page_clean, brand, full_brand, known_people)
            incorporated = [c for c in compounds if _incorporated_on_page(page_lower, c.lower())]
            if incorporated:
                return _compound_reason(incorporated)
        return None
    if company and _own_legal_form_on_page(page_lower, brand, full_brand, own_forms):
        return None
    compounds = _compound_entity_hits(page_clean, brand, full_brand, known_people)
    if compounds:
        # a known person named on the page does not excuse the brand being embedded in ANOTHER
        # entity's name: the person may simply appear in a story about the namesake (the hard T20 shape)
        return _compound_reason(compounds)
    if person_strong:
        return None
    person_hits = _person_context_hits(page_lower, brand)
    if person_hits and not _LEGAL_SUFFIXES.search(page_lower):
        return _person_reason(brand, person_hits[:3])
    return None


def _incorporated_on_page(page_lower: str, compound_lower: str) -> bool:
    """True when the compound names an incorporated entity: a legal form attached
    ('{compound} SpA') or within two tokens inside ('{brand} Spa {word}', the Italian mid-name
    form), or a group/parent marker adjacent to it ('group {compound}', 'gruppo {compound}')."""
    if _legal_form_after(page_lower, compound_lower) is not None:
        return True
    first = compound_lower.split()[0]
    for m in re.finditer(r"(?<![a-z0-9])" + re.escape(first) + r"\b(.{0,24}?)(?:\b(spa|s\.p\.a|srl|s\.r\.l)\b|\b(s\.p\.a\.|s\.r\.l\.|s\.n\.c\.|s\.a\.s\.|gmbh|inc|ltd|ag)\b)",
                         page_lower):
        if _is_legal_form(m.group(2) or m.group(3)):
            return True
    if re.search(r"\b(group|gruppo)\s+(?:[a-z]+\s+){0,2}" + re.escape(compound_lower.split()[0]), page_lower):
        return True
    if re.search(re.escape(compound_lower.split()[0]) + r"\s+(?:[a-z]+\s+){0,2}(group|gruppo)\b", page_lower):
        return True
    return False


def _compound_reason(compounds: List[str]) -> str:
    return ("Name-collision: the brand appears embedded in a different entity's name (" +
            ", ".join(sorted(set(c.strip() for c in compounds))[:4]) +
            ") while the target company's own full name or legal form is absent")


def _person_reason(brand: str, markers: List[str]) -> str:
    return (f"Name-collision: '{brand}' is used as a person's name in a non-corporate context "
            f"({', '.join(markers)}) with no legal-form context")


# ---------------------------------------------------------------------------
# Verification Core
# ---------------------------------------------------------------------------

def verify(
    fact: Fact,
    page_text: str,
    company: Dict[str, Any],
    ledger: Optional[Ledger] = None,
    llm: Optional[Any] = None,
) -> Verdict:
    """
    Run verification gate on a single fact.

    Sets:
      - fact.verified (bool)
      - fact.confidence (adjusted float 0.0-1.0)
      - fact.notes (records verification decision details)
    Returns:
      - Verdict dataclass

    Guaranteed NEVER to raise an exception.
    """
    try:
        return _verify_internal(fact, page_text, company, ledger, llm)
    except Exception as exc:
        logger.exception("Unexpected error in verify gate for key %s: %s", fact.key, exc)
        verdict = Verdict(
            verified=False,
            confidence=max(0.1, min(fact.confidence, 0.3)),
            decision="unverified",
            reasons=[f"Verification gate execution error: {str(exc)}"],
        )
        fact.verified = False
        fact.confidence = verdict.confidence
        fact.notes = "; ".join(verdict.reasons)
        return verdict


def _verify_internal(
    fact: Fact,
    page_text: str,
    company: Dict[str, Any],
    ledger: Optional[Ledger],
    llm: Optional[Any],
) -> Verdict:
    reasons: List[str] = []
    page_text_clean = page_text or ""
    page_lower = page_text_clean.lower()
    adjusted_confidence = float(fact.confidence)

    # -----------------------------------------------------------------------
    # Step 1: Quote Grounding
    # -----------------------------------------------------------------------
    grounded = False
    if fact.grade == "A":
        # Official registry/filing: quote may be empty if ref or official url provided
        grounded = True
        reasons.append("Grade A official record accepted by reference")
    else:
        # Web facts (B, C, D) must have evidence with a quote occurring in page_text
        quotes = [ev.quote for ev in fact.evidence if ev.quote]
        if not quotes:
            reasons.append("No quote provided in evidence for web fact")
        else:
            all_grounded = True
            for q in quotes:
                if not quote_in_text(q, page_text_clean):
                    all_grounded = False
                    reasons.append(f"Quote not found verbatim in page text: {q[:60]!r}")
            grounded = all_grounded

    if not grounded and fact.grade != "A":
        # Grounding failure is fatal for verification
        adjusted_confidence = min(adjusted_confidence, CONFIDENCE_GROUNDING_FAILED)
        fact.verified = False
        fact.confidence = adjusted_confidence
        fact.notes = "; ".join(reasons)
        return Verdict(
            verified=False,
            confidence=adjusted_confidence,
            decision="rejected",
            reasons=reasons,
            grounded=False,
        )

    # -----------------------------------------------------------------------
    # Step 2: Entity Match & Discriminator Search
    # -----------------------------------------------------------------------
    discriminators_found: List[str] = []

    if fact.grade == "A":
        entity_matched = True
        discriminators_found.append("registry_record")
        name_matched = True
        brand_tokens = []
    else:
        legal_name = str(company.get("legal_name") or "").lower()
        vat = normalize_vat(company.get("vat") or company.get("registration_number"))
        city = str(company.get("city") or "").lower()
        province = str(company.get("province") or "").lower()
        domain = extract_domain(company.get("website_url"))
        known_people = [str(p).lower() for p in company.get("people", []) if len(str(p)) >= 3]

        brand_tokens = extract_brand_tokens(legal_name)
        name_matched = False
        if legal_name and legal_name in page_lower:
            name_matched = True
        elif any(bt in page_lower for bt in brand_tokens):
            name_matched = True

        # Check evidence URLs: if URL is on company's registered domain, domain discriminator holds!
        for ev in fact.evidence:
            ev_domain = extract_domain(ev.url)
            if domain and ev_domain and (domain in ev_domain or ev_domain in domain):
                if "domain" not in discriminators_found:
                    discriminators_found.append("domain")

        # Check VAT in page text
        if vat and len(vat) >= 7 and vat in page_text_clean:
            discriminators_found.append("vat")

        # Check city / province in page text (word-boundary safe)
        clean_city = re.sub(r"\(.*?\)", "", city).strip()
        if clean_city and len(clean_city) >= 3 and re.search(r"\b" + re.escape(clean_city) + r"\b", page_lower):
            discriminators_found.append("city")
        if province and len(province) >= 3 and re.search(r"\b" + re.escape(province) + r"\b", page_lower):
            discriminators_found.append("province")

        # Check known people
        for p in known_people:
            if re.search(r"\b" + re.escape(p) + r"\b", page_lower):
                discriminators_found.append(f"person:{p}")
                break

        # Check registered address keywords (street name or CAP)
        address = str(company.get("address") or "").lower()
        cap_match = re.search(r"\b\d{5}\b", address)
        if cap_match and cap_match.group(0) in page_text_clean:
            discriminators_found.append(f"cap:{cap_match.group(0)}")

        entity_matched = name_matched and len(discriminators_found) > 0

        if not name_matched:
            reasons.append(f"Entity name/brand not found in source text for {legal_name}")
        elif not discriminators_found:
            reasons.append(
                f"Entity name matched ({brand_tokens[:1]}), but NO discriminator found "
                f"(missing VAT, city, domain, or known person)"
            )

    # -----------------------------------------------------------------------
    # Step 3: Name-Collision Detection (generic, I93)
    # -----------------------------------------------------------------------
    primary_brand = brand_tokens[0] if brand_tokens else (company.get("legal_name") or "")
    # the SHORTEST brand token is the one that can be embedded in a namesake compound
    # (the full name cannot be; the bare brand word can be 'Othercorp BRANDWORD')
    short_brand = min(brand_tokens, key=len) if brand_tokens else primary_brand
    strong = set(d if not d.startswith("person:") else "person:x" for d in discriminators_found)
    known_people = [str(p).lower() for p in (company.get("people") or []) if len(str(p)) >= 3]
    collision_reason = check_known_collisions(
        short_brand, page_text_clean, company=company, strong_discriminators=strong,
        known_people=known_people)
    collision_detected = collision_reason is not None
    if collision_detected:
        reasons.append(f"Collision detected: {collision_reason}")
        adjusted_confidence = min(adjusted_confidence, CONFIDENCE_COLLISION_PENALTY)

    # -----------------------------------------------------------------------
    # Step 4: Date Sanity Check
    # -----------------------------------------------------------------------
    date_sane = True
    current_year = datetime.now(timezone.utc).year
    # Look for year in as_of or in fact value if date/event
    years_to_check: List[int] = []
    if fact.as_of:
        m = re.search(r"\b(1\d{3}|20\d\d)\b", str(fact.as_of))
        if m:
            years_to_check.append(int(m.group(0)))
    if isinstance(fact.value, (int, str)):
        m = re.search(r"\b(1\d{3}|20\d\d)\b", str(fact.value))
        if m:
            val_int = int(m.group(0))
            if 1000 <= val_int <= current_year + 50:
                years_to_check.append(val_int)

    is_historical_category = (
        fact.category in ("identity", "events", "financials")
        or fact.key.startswith(("events.", "identity.", "financials."))
    )

    for yr in years_to_check:
        if yr > current_year + 1:
            date_sane = False
            reasons.append(f"Future date sanity violation: year {yr} > {current_year + 1}")
        elif yr < MIN_PLAUSIBLE_YEAR and is_historical_category:
            date_sane = False
            reasons.append(f"Ancient date sanity violation: year {yr} < {MIN_PLAUSIBLE_YEAR}")

    if not date_sane:
        adjusted_confidence = min(adjusted_confidence, CONFIDENCE_DATE_SANITY_PENALTY)

    # -----------------------------------------------------------------------
    # Step 5: Numeric Sanity Check against AIDA
    # -----------------------------------------------------------------------
    numeric_sane = True
    # Employee check
    if any(k in fact.key for k in ("employee", "headcount", "staff")):
        baseline_emp = company.get("headcount") or company.get("employees")
        if baseline_emp and isinstance(fact.value, (int, float)):
            val_num = float(fact.value)
            b_num = float(baseline_emp)
            if b_num > 5:  # only apply factor bounds if baseline is non-trivial
                if val_num > b_num * MAX_EMPLOYEE_FACTOR or val_num < b_num * MIN_EMPLOYEE_FACTOR:
                    numeric_sane = False
                    reasons.append(
                        f"Numeric sanity violation for employees: stated {val_num} vs "
                        f"baseline {b_num} (outside factor {MAX_EMPLOYEE_FACTOR}x)"
                    )
    # Revenue check
    if any(k in fact.key for k in ("revenue", "turnover", "sales")):
        baseline_rev = company.get("turnover") or company.get("revenue_latest") or company.get("revenue")
        if baseline_rev and isinstance(fact.value, (int, float)):
            val_rev = float(fact.value)
            b_rev = float(baseline_rev)
            if b_rev > 100_000:  # e.g. at least 100k
                if val_rev > b_rev * MAX_REVENUE_FACTOR or val_rev < b_rev * MIN_REVENUE_FACTOR:
                    numeric_sane = False
                    reasons.append(
                        f"Numeric sanity violation for revenue: stated {val_rev:,.0f} vs "
                        f"baseline {b_rev:,.0f} (outside factor {MAX_REVENUE_FACTOR}x)"
                    )

    if not numeric_sane:
        adjusted_confidence = min(adjusted_confidence, CONFIDENCE_NUMERIC_SANITY_PENALTY)

    # -----------------------------------------------------------------------
    # Step 6: Contradiction Check against Existing Ledger
    # -----------------------------------------------------------------------
    contradiction_detected = False
    contradiction_details = None
    if ledger and fact.key in SINGLE_VALUED_KEYS and fact.status == "present":
        for existing in ledger.present(fact.key):
            if existing.id != fact.id and existing.verified:
                contradiction_detected = True
                contradiction_details = (
                    f"Conflict on single-valued key {fact.key}: new value {fact.value!r} "
                    f"contradicts verified value {existing.value!r}"
                )
                reasons.append(contradiction_details)
                break

    # -----------------------------------------------------------------------
    # Step 7: Optional LLM Judge for Ambiguity
    # -----------------------------------------------------------------------
    if llm and name_matched and not discriminators_found and not collision_detected:
        llm_verified, llm_reason = _call_llm_judge(llm, fact, page_text_clean, company)
        if llm_verified:
            entity_matched = True
            discriminators_found.append("llm_judge")
            reasons.append(f"LLM judge confirmed entity match: {llm_reason}")
        else:
            reasons.append(f"LLM judge rejected entity match: {llm_reason}")

    # -----------------------------------------------------------------------
    # Final Verdict Synthesis
    # -----------------------------------------------------------------------
    # Confidence penalties
    if not discriminators_found and name_matched:
        adjusted_confidence = min(adjusted_confidence, CONFIDENCE_NO_DISCRIMINATOR_PENALTY)

    # Can it be verified?
    can_verify = (
        grounded
        and entity_matched
        and not collision_detected
        and date_sane
        and numeric_sane
        and not contradiction_detected
        and adjusted_confidence >= MIN_CONFIDENCE_FOR_VERIFIED
    )

    if contradiction_detected:
        decision = "contradiction"
    elif collision_detected:
        decision = "rejected"
    elif can_verify:
        decision = "verified"
    elif not grounded:
        decision = "rejected"
    else:
        decision = "unverified"

    fact.verified = can_verify
    fact.confidence = round(adjusted_confidence, 4)
    if reasons:
        fact.notes = "; ".join(reasons)

    return Verdict(
        verified=can_verify,
        confidence=fact.confidence,
        decision=decision,
        reasons=reasons,
        grounded=grounded,
        entity_matched=entity_matched,
        discriminators_found=discriminators_found,
        collision_detected=collision_detected,
        collision_details=collision_reason,
        date_sane=date_sane,
        numeric_sane=numeric_sane,
        contradiction_detected=contradiction_detected,
        contradiction_details=contradiction_details,
    )


# ---------------------------------------------------------------------------
# LLM Judge Implementation with In-Memory Cache
# ---------------------------------------------------------------------------

def _call_llm_judge(
    llm: Any,
    fact: Fact,
    page_text: str,
    company: Dict[str, Any],
) -> Tuple[bool, str]:
    """Call LLM triage to resolve ambiguous entity matching."""
    cache_key = hashlib.sha1(f"{company.get('id')}|{fact.id}|{page_text[:1000]}".encode("utf-8")).hexdigest()
    if cache_key in _LLM_JUDGE_CACHE:
        return _LLM_JUDGE_CACHE[cache_key]

    system = (
        "You are an entity-resolution judge. Determine whether the provided text excerpt "
        "is genuinely about the specified Italian company, or about an unrelated company/person "
        "with a similar name. Reply in JSON: {\"same_company\": true/false, \"reason\": \"short explanation\"}."
    )
    user_prompt = (
        f"Target company: {company.get('legal_name')} (VAT: {company.get('vat')}, City: {company.get('city')})\n"
        f"Fact to verify: {fact.key} = {fact.value}\n"
        f"Text excerpt:\n{page_text[:2500]}"
    )

    try:
        if hasattr(llm, "complete"):
            res = llm.complete(task="triage", system=system, user=user_prompt)
            content = res.content if hasattr(res, "content") else str(res)
        elif callable(llm):
            content = llm(system, user_prompt)
        else:
            return False, "Unsupported LLM client"

        parsed = json.loads(content) if isinstance(content, str) and content.startswith("{") else {}
        match = re.search(r"\{.*\}", str(content), re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))

        same = bool(parsed.get("same_company", False))
        reason = str(parsed.get("reason", "Evaluated by model"))
        _LLM_JUDGE_CACHE[cache_key] = (same, reason)
        return same, reason
    except Exception as exc:
        logger.warning("LLM judge failed: %s", exc)
        return False, f"LLM judge error: {exc}"
