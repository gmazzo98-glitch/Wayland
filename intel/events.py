"""
Shared event taxonomy, data models, timeline merging, and extraction helpers for corporate events.

Ticket I13: Corporate events timeline (deals, plants, subsidiaries, leadership changes, awards).
Grounded in diagnosis.contracts.TRIGGER_KINDS.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from diagnosis.contracts import TRIGGER_KINDS
from intel.ledger import MIN_QUOTE_CHARS, quote_in_text


@dataclass
class CorporateEvent:
    kind: str                               # from TRIGGER_KINDS
    title: str                              # Short summary
    description: str                        # Full context
    date: Optional[str] = None             # ISO date string e.g. '2022-01-13' or '2019-07'
    year: Optional[int] = None
    parties: List[str] = field(default_factory=list)      # Companies or persons involved
    amount: Optional[str] = None           # Deal value / investment amount e.g. 'EUR 19.9M'
    source_url: Optional[str] = None
    quote: Optional[str] = None
    sources: List[str] = field(default_factory=list)      # All sources confirming this event
    quotes: List[str] = field(default_factory=list)       # Quotes from all sources
    conflicts: List[Dict[str, Any]] = field(default_factory=list)  # Conflicting statements (e.g. date disagreement)
    verified: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def normalize_event_kind(kind_str: str) -> str:
    """Normalize event kind to one of diagnosis.contracts.TRIGGER_KINDS."""
    lowered = (kind_str or "").strip().lower()
    if lowered in TRIGGER_KINDS:
        return lowered
    if any(k in lowered for k in ("m&a", "deal", "acquisizione", "buyout", "takeover", "merger", "cedut")):
        return "ownership_change"
    if any(k in lowered for k in ("stabilimento", "plant", "facility", "sede", "branch", "filiale", "subsidiary")):
        return "expansion"
    if any(k in lowered for k in ("investimento", "invested", "investing", "funding", "capex")):
        return "investment_announced"
    if any(k in lowered for k in ("nomina", "appointed", "joined", "resigned", "left", "direttore", "ceo")):
        return "leadership_change"
    if any(k in lowered for k in ("partnership", "accordo", "partner", "distribut", "representative")):
        return "partnership"
    if any(k in lowered for k in ("premio", "award", "riconoscimento", "certific")):
        return "certification"
    return "other"


def parse_event_year(text: str) -> Optional[int]:
    """Extract 4-digit event year (between 1950 and current_year + 5)."""
    m = re.search(r"\b(19[5-9]\d|20[0-3]\d)\b", text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def merge_events(events: List[CorporateEvent], company_name: str = "") -> List[CorporateEvent]:
    """
    Merge duplicate reports of the same event across outlets into one event with multiple sources.
    Detects and records conflicting dates as conflicts rather than discarding or overwriting.
    `company_name` (when the caller knows it) excludes the subject's own tokens from the
    same-event test, so two different milestones that both mention the company stay separate.
    """
    merged: List[CorporateEvent] = []
    own = {t for t in re.findall(r"\b[a-z0-9\-]{4,}\b", (company_name or "").lower())}

    for ev in events:
        matched = False
        for existing in merged:
            if _is_same_event(existing, ev, own_tokens=own):
                matched = True
                # Merge sources & quotes
                if ev.source_url and ev.source_url not in existing.sources:
                    existing.sources.append(ev.source_url)
                if ev.quote and ev.quote not in existing.quotes:
                    existing.quotes.append(ev.quote)

                # Merge parties
                for p in ev.parties:
                    if p and p not in existing.parties:
                        existing.parties.append(p)

                # Merge amount
                if not existing.amount and ev.amount:
                    existing.amount = ev.amount

                # Detect date/year conflicts
                if existing.year and ev.year and existing.year != ev.year:
                    conflict_record = {
                        "field": "date/year",
                        "value_1": existing.date or str(existing.year),
                        "source_1": existing.source_url or (existing.sources[0] if existing.sources else ""),
                        "value_2": ev.date or str(ev.year),
                        "source_2": ev.source_url or "",
                        "note": f"Sources conflict on date: {existing.year} vs {ev.year}",
                    }
                    if conflict_record not in existing.conflicts:
                        existing.conflicts.append(conflict_record)
                elif not existing.date and ev.date:
                    existing.date = ev.date
                    existing.year = ev.year or existing.year
                break

        if not matched:
            if ev.source_url and ev.source_url not in ev.sources:
                ev.sources.append(ev.source_url)
            if ev.quote and ev.quote not in ev.quotes:
                ev.quotes.append(ev.quote)
            merged.append(ev)

    # Sort chronologically (earliest first, None at end)
    return sorted(merged, key=lambda x: (x.year if x.year else 9999, x.date or ""))


def _is_same_event(a: CorporateEvent, b: CorporateEvent, own_tokens: Optional[Set[str]] = None) -> bool:
    """True if two events represent the same real-world corporate milestone. Generic: same kind plus
    enough overlap between the entity names the two sentences mention (no company-specific tokens)."""
    if a.kind != b.kind:
        return False

    a_tokens = set(re.findall(r"\b[A-Za-z0-9\-]{4,}\b", (a.title + " " + " ".join(a.parties)).lower()))
    b_tokens = set(re.findall(r"\b[A-Za-z0-9\-]{4,}\b", (b.title + " " + " ".join(b.parties)).lower()))
    shared_parties = a_tokens.intersection(b_tokens) - (own_tokens or set())

    # A shared party token that is not generic vocabulary says these are the same deal/milestone
    generic = {"company", "italy", "europe", "group", "holding", "corporation", "srl", "spa",
               "acquisition", "acquires", "acquired", "investment", "merger", "deal", "founded",
               "year", "new", "plant", "facility", "subsidiary"}
    distinctive = shared_parties - generic
    if distinctive:
        return True

    # Same year and strong token overlap
    if a.year and b.year and a.year == b.year:
        overlap = len(shared_parties)
        if overlap >= 2:
            return True

    return False
