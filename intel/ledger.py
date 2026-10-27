"""
The evidence ledger: the one shape every deep-intelligence collector writes to.

A Fact is one statement about ONE company with the proof behind it. The rules are the contract between agents
who never talk to each other, so they are enforced here, not left to convention:

  * A fact that came from the web (grade B, C or D) needs at least one Evidence with a URL and a QUOTE of the
    source text. The collector must have checked that the quote really occurs in the page it fetched
    (`quote_in_text`); a fact whose quote cannot be found is dropped, never stored. This is what stops a model
    from inventing a plausible-sounding certification or an acquisition that never happened.
  * Grade A (filed/official: a registry, an annual-accounts export, an SEC filing) may carry a registry
    reference instead of a quote.
  * "absent" means a collector actively looked and found nothing there; a fact that is not in the ledger is
    UNKNOWN. The two are never confused, and a simulated placeholder is not a fact at all.
  * The ledger holds no scores. Same grades as diagnosis/contracts.py: A filed/official, B the company's own
    publication read directly, C a third party about the company, D inference (never sufficient alone).

Storage reuses the existing blob mechanism: one RawImportRecord per company, dataset `crawler_deep_profile`,
re-running overwrites like every other crawler blob. Facts are keyed by a hash of (key, normalised value), so
two collectors finding the same thing merge their evidence instead of duplicating the fact.

Fact keys are lowercase `group.name` strings (see CATEGORIES for the groups). Where diagnosis/factkeys.py
already declares a key (own.*, prof.*, pub.*, reach.*, trg.*, ...), reuse that exact name; do not coin a second
name for the same thing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from intel import DATASET_NAME, SCHEMA_VERSION

GRADES = ("A", "B", "C", "D")
STATUSES = ("present", "absent")
SOURCE_TYPES = (
    "registry",          # company registry, annual-accounts export, chamber of commerce
    "public_register",   # Accredia, EPO, EUIPO, TED, state-aid register, SEC filing, GLEIF
    "company_site",      # the company's own web page
    "company_document",  # a PDF/brochure/certificate the company publishes
    "press",             # news, trade press, press release, deal announcement
    "directory",         # business directory, trade-fair or association member list
    "social",            # LinkedIn, YouTube, other public social pages
    "search_result",     # a search engine snippet (weak: follow the link before relying on it)
    "model_inference",   # a model's reading, no direct source text
    "manual",            # entered by a person
)
CATEGORIES = (
    "identity", "ownership", "events", "financials", "people", "contact", "products", "certifications",
    "locations", "networks", "digital", "hiring", "customers", "peers", "documents", "ip", "funding", "risk",
)
KEY_PATTERN = re.compile(r"^[a-z]+(\.[a-z0-9_]+)+$")

MIN_QUOTE_CHARS = 12
MAX_QUOTE_CHARS = 400
MAX_FACTS = 2500   # keeps the one row per company at a sane size

# Keys where two different values for the same company are a CONFLICT to surface (the other keys can hold many).
SINGLE_VALUED_KEYS = frozenset({
    "identity.founded_year", "identity.legal_form", "identity.registered_office", "identity.employees",
    "ownership.ultimate_owner", "ownership.direct_owner", "contact.switchboard", "contact.pec",
})


class LedgerError(ValueError):
    """A rule of the contract was broken; the collector has a bug (or must drop this fact)."""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def quote_in_text(quote: str, page_text: str) -> bool:
    """True when the quote really occurs in the page text (whitespace and case folded). Collectors call this
    on the text they fetched BEFORE building an Evidence; a model's paraphrase will not pass, which is the point."""
    q = _ws(quote)
    return len(q) >= MIN_QUOTE_CHARS and q in _ws(page_text)


def _norm_value(value: Any) -> str:
    if isinstance(value, str):
        return _ws(value)
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).lower()
    except (TypeError, ValueError):
        return _ws(str(value))


def fact_id(key: str, value: Any) -> str:
    return hashlib.sha1(f"{key}|{_norm_value(value)}".encode("utf-8")).hexdigest()[:16]


@dataclass
class Evidence:
    source_type: str
    url: Optional[str] = None
    quote: Optional[str] = None         # verbatim text of the source (<= MAX_QUOTE_CHARS)
    ref: Optional[str] = None           # for grade A: where in the registry/export ("AIDA Raw Exports: controlling_shareholders")
    title: Optional[str] = None
    published_at: Optional[str] = None  # date the SOURCE says it was published, when known
    retrieved_at: str = field(default_factory=utcnow_iso)
    content_hash: Optional[str] = None  # sha1 of the fetched text, for change detection

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "")}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Evidence":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Fact:
    key: str
    value: Any
    grade: str
    evidence: List[Evidence]
    status: str = "present"
    confidence: float = 0.7             # the collector's own 0-1 reading; not a probability, not a score
    as_of: Optional[str] = None         # when the fact was TRUE or happened (event date, filing date), ISO
    collector: str = ""                 # e.g. "I10:deep-site-reader@1"
    verified: bool = False              # True only after the entity-match + grounding gate (ticket I06) passed
    notes: Optional[str] = None
    first_seen: str = field(default_factory=utcnow_iso)
    last_seen: str = field(default_factory=utcnow_iso)

    @property
    def id(self) -> str:
        return fact_id(self.key, self.value)

    @property
    def category(self) -> str:
        return self.key.split(".", 1)[0]

    def validate(self) -> None:
        if not KEY_PATTERN.match(self.key or ""):
            raise LedgerError(f"fact key {self.key!r} must look like 'group.name' (lowercase, digits, underscores)")
        if self.grade not in GRADES:
            raise LedgerError(f"{self.key}: grade must be one of {GRADES}")
        if self.status not in STATUSES:
            raise LedgerError(f"{self.key}: status must be one of {STATUSES}")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise LedgerError(f"{self.key}: confidence must be between 0 and 1")
        if self.value is None and self.status == "present":
            raise LedgerError(f"{self.key}: a present fact needs a value")
        if not self.evidence:
            raise LedgerError(f"{self.key}: a fact without evidence is a guess, not a fact")
        for ev in self.evidence:
            if ev.source_type not in SOURCE_TYPES:
                raise LedgerError(f"{self.key}: unknown source_type {ev.source_type!r}")
            if ev.quote and len(ev.quote) > MAX_QUOTE_CHARS:
                raise LedgerError(f"{self.key}: quote longer than {MAX_QUOTE_CHARS} characters; cut it to the sentence")
        if self.grade != "A":
            good = [e for e in self.evidence if e.url and e.quote and len(_ws(e.quote)) >= MIN_QUOTE_CHARS]
            if not good:
                raise LedgerError(
                    f"{self.key}: a grade {self.grade} fact needs an Evidence with a url and a verbatim quote "
                    f"(>= {MIN_QUOTE_CHARS} characters) that the collector checked with quote_in_text()")
            if self.grade == "D" and not self.verified and self.confidence > 0.5:
                raise LedgerError(f"{self.key}: an unverified grade D inference cannot claim confidence above 0.5")
        else:
            if not any(e.url or e.ref for e in self.evidence):
                raise LedgerError(f"{self.key}: a grade A fact needs a url or a registry ref")

    def to_dict(self) -> Dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "evidence" and v is not None}
        d["id"] = self.id
        d["evidence"] = [e.to_dict() for e in self.evidence]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Fact":
        keys = {k for k in cls.__dataclass_fields__ if k != "evidence"}
        kw = {k: d[k] for k in keys if k in d}
        return cls(evidence=[Evidence.from_dict(e) for e in d.get("evidence", [])], **kw)


class Ledger:
    """All the facts collected about one company, merged by (key, value)."""

    def __init__(self, company_id: Optional[str] = None) -> None:
        self.company_id = company_id
        self.facts: Dict[str, Fact] = {}
        self.runs: List[Dict[str, Any]] = []   # [{run_id, collector, started_at, finished_at, queries, pages, errors}]

    # ---- writing ----------------------------------------------------------------------------------
    def add(self, fact: Fact) -> Fact:
        """Validate and merge. The same (key, value) found again adds its evidence (never a duplicate fact) and
        keeps the stronger grade and the higher confidence; `verified` is true only if every contributor verified."""
        fact.validate()
        if len(self.facts) >= MAX_FACTS and fact.id not in self.facts:
            raise LedgerError(f"ledger is full ({MAX_FACTS} facts); a collector is emitting noise")
        existing = self.facts.get(fact.id)
        if existing is None:
            self.facts[fact.id] = fact
            return fact
        seen = {(e.url, _ws(e.quote or "")) for e in existing.evidence}
        for ev in fact.evidence:
            if (ev.url, _ws(ev.quote or "")) not in seen:
                existing.evidence.append(ev)
        if GRADES.index(fact.grade) < GRADES.index(existing.grade):
            existing.grade = fact.grade
        existing.confidence = max(existing.confidence, fact.confidence)
        existing.verified = existing.verified and fact.verified
        existing.as_of = existing.as_of or fact.as_of
        existing.last_seen = utcnow_iso()
        return existing

    def add_many(self, facts: Iterable[Fact]) -> List[str]:
        """Add what is valid; return the error text of what was refused (so a collector can log, not crash)."""
        errors: List[str] = []
        for f in facts:
            try:
                self.add(f)
            except LedgerError as exc:
                errors.append(str(exc))
        return errors

    # ---- reading ----------------------------------------------------------------------------------
    def present(self, key_prefix: str = "") -> List[Fact]:
        return [f for f in self.facts.values() if f.status == "present" and f.key.startswith(key_prefix)]

    def by_category(self) -> Dict[str, List[Fact]]:
        out: Dict[str, List[Fact]] = {}
        for f in self.facts.values():
            out.setdefault(f.category, []).append(f)
        return out

    def conflicts(self) -> List[Dict[str, Any]]:
        """Single-valued keys that hold more than one present value: show both with their sources, never pick silently."""
        groups: Dict[str, List[Fact]] = {}
        for f in self.facts.values():
            if f.status == "present" and f.key in SINGLE_VALUED_KEYS:
                groups.setdefault(f.key, []).append(f)
        return [{"key": k, "values": [{"value": f.value, "grade": f.grade, "confidence": f.confidence,
                                       "sources": [e.url or e.ref for e in f.evidence]} for f in fs]}
                for k, fs in groups.items() if len(fs) > 1]

    def is_stale(self, key_prefix: str, max_age_days: int, now: Optional[datetime] = None) -> bool:
        """True when nothing under the prefix was seen within max_age_days (or nothing exists): the refresh trigger."""
        now = now or datetime.now(timezone.utc)
        seen = [datetime.strptime(f.last_seen, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                for f in self.facts.values() if f.key.startswith(key_prefix)]
        return not seen or (now - max(seen)).days > max_age_days

    # ---- (de)serialisation ------------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {"schema": SCHEMA_VERSION, "company_id": self.company_id, "updated_at": utcnow_iso(),
                "facts": [f.to_dict() for f in sorted(self.facts.values(), key=lambda f: (f.key, f.id))],
                "runs": self.runs[-50:]}

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "Ledger":
        led = cls((d or {}).get("company_id"))
        for raw in (d or {}).get("facts", []):
            fact = Fact.from_dict(raw)
            led.facts[fact.id] = fact   # already validated when written; a corrupt row must not break a page
        led.runs = list((d or {}).get("runs", []))
        return led


# ---- storage (the existing RawImportRecord blob) ----------------------------------------------------

def load_ledger(db, company_id: str) -> Ledger:
    from models import RawImportRecord
    rec = db.query(RawImportRecord).filter_by(company_id=company_id, dataset_name=DATASET_NAME).first()
    led = Ledger.from_dict(rec.raw_row if rec else None)
    led.company_id = company_id
    return led


def save_ledger(db, company_id: str, ledger: Ledger) -> None:
    """Upsert the blob. Merge-then-save is the caller's job: load, add, save. Two collectors writing the same
    company at once should each load immediately before saving (the facts merge by id), not hold a stale copy."""
    from models import RawImportRecord
    rec = db.query(RawImportRecord).filter_by(company_id=company_id, dataset_name=DATASET_NAME).first()
    if rec is None:
        rec = RawImportRecord(company_id=company_id, dataset_name=DATASET_NAME)
        db.add(rec)
    rec.source_filename = f"crawler:{DATASET_NAME}"
    rec.raw_row = ledger.to_dict()
    rec.updated_at = datetime.utcnow()
    db.commit()
