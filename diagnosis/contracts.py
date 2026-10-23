"""
The shapes every diagnosis component speaks. Frozen on purpose: agents working in parallel code against this
file, so a change here is a contract-change ticket (docs/DIAGNOSIS_TICKETS.md, "Changing a contract"), never
a side effect of a feature ticket. Bump SCHEMA_VERSION on any change that is not purely additive.

Design rules baked into the types:

  * A Fact is one observed or derived statement about ONE company, with its provenance. A fact that is not in
    CompanyFacts is UNKNOWN ("never checked"); a fact with status "absent" is a confirmed absence ("checked,
    nothing there"). The two must never be confused, and a simulated placeholder is not a fact at all: readers
    must not emit one.
  * Every Fact carries an evidence grade (A official/filed, B the company's own publication read directly,
    C a third party about the company, D inference). A claim resting on grade D alone is never "supported".
  * A HypothesisResult reports how strongly the evidence points at a problem (strength, 0-100) and how much
    to trust that reading (confidence). It is NOT a Need score and is never blended into NEED or READINESS.
  * A CallPack is a set of hypotheses plus what is unknown; it is never a verdict.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from diagnosis import SCHEMA_VERSION

# --------------------------------------------------------------------------------------------- evidence grades

GRADE_A = "A"   # filed or official: annual accounts, company registry, statistical office, state-aid register
GRADE_B = "B"   # the company's own publication, read directly: own website, own job ads, own product catalog
GRADE_C = "C"   # a third party writing about the company: press, directory listing, review site
GRADE_D = "D"   # inference: heuristic match, model reading, estimate. Never sufficient alone.
GRADES = (GRADE_A, GRADE_B, GRADE_C, GRADE_D)
GRADE_RANK = {g: i for i, g in enumerate(GRADES)}   # lower is stronger

FACT_PRESENT = "present"
FACT_ABSENT = "absent"      # actively checked, confirmed not there
FACT_STATUSES = (FACT_PRESENT, FACT_ABSENT)

# --------------------------------------------------------------------------------------------- issue tree

BRANCHES = {
    "COM": "Commercial & growth",
    "PRF": "Profitability & cost",
    "OPS": "Operations & engineering",
    "PRD": "Product & service model",
    "FIN": "Finance & resilience",
    "PPL": "People & organisation",
    "REG": "Regulation, risk & sustainability",
}

HYP_SUPPORTED = "supported"          # direct evidence of grade A/B (or two independent C), nothing contradicting
HYP_PLAUSIBLE = "plausible"          # evidence points that way but is partial, indirect or single-source
HYP_WEAK = "weak"                    # a hint only; worth a question, not a claim
HYP_CONTRADICTED = "contradicted"    # evidence points the other way
HYP_NOT_ASSESSABLE = "not_assessable"   # the facts the rule needs are unknown
HYP_STATUSES = (HYP_SUPPORTED, HYP_PLAUSIBLE, HYP_WEAK, HYP_CONTRADICTED, HYP_NOT_ASSESSABLE)

CONF_HIGH, CONF_MEDIUM, CONF_LOW = "high", "medium", "low"
CONFIDENCES = (CONF_HIGH, CONF_MEDIUM, CONF_LOW)

ROLE_SUPPORTS, ROLE_CONTRADICTS, ROLE_CONTEXT = "supports", "contradicts", "context"
EVIDENCE_ROLES = (ROLE_SUPPORTS, ROLE_CONTRADICTS, ROLE_CONTEXT)

TRIGGER_KINDS = (
    "leadership_change", "ownership_change", "investment_announced", "expansion", "new_product", "grant_awarded",
    "tender_won", "accounts_filed", "hiring_surge", "certification", "trade_fair", "regulatory_deadline",
    "incentive_window", "market_shock", "distress", "partnership", "other",
)

CALL_TIERS = ("A", "B", "C", "HOLD", "EXCLUDE")   # A call now, B warm, C nurture, HOLD enrich first, EXCLUDE


# --------------------------------------------------------------------------------------------- helpers

def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


def _jsonable(v: Any) -> Any:
    """Plain-JSON form of a value (NaN becomes None; dates become ISO strings; tuples become lists)."""
    if isinstance(v, float) and v != v:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    return v


# --------------------------------------------------------------------------------------------- facts

@dataclass(frozen=True)
class Fact:
    """One statement about one company. `key` must exist in diagnosis.factkeys.FACT_KEYS (the registry rejects
    unknown keys, so two agents can never invent two names for the same thing)."""
    key: str
    value: Any = None
    unit: str = ""
    grade: str = GRADE_B
    status: str = FACT_PRESENT
    source: str = ""          # human label, e.g. "AIDA Raw Exports"
    source_ref: str = ""      # dataset_name / signal_key / URL: where to look it up again
    as_of: Optional[str] = None       # ISO date the underlying observation refers to (fiscal year end, page date...)
    observed_at: Optional[str] = None   # ISO date-time the tool read it
    note: str = ""

    def __post_init__(self):
        if self.grade not in GRADES:
            raise ValueError(f"Fact {self.key!r}: grade must be one of {GRADES}, got {self.grade!r}")
        if self.status not in FACT_STATUSES:
            raise ValueError(f"Fact {self.key!r}: status must be one of {FACT_STATUSES}, got {self.status!r}")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["value"] = _jsonable(d["value"])
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Fact":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class CompanyFacts:
    """Everything the tool knows about one company, as Facts. Built by registry.build_facts()."""
    company_id: str
    facts: Dict[str, Fact] = field(default_factory=dict)
    built_at: Optional[str] = None
    reader_errors: Dict[str, str] = field(default_factory=dict)   # reader name -> error text; a reader failing never aborts the build
    conflicts: List[Dict[str, Any]] = field(default_factory=list)   # same key from two readers with different values
    schema_version: int = SCHEMA_VERSION

    # -- reading
    def get(self, key: str) -> Optional[Fact]:
        return self.facts.get(key)

    def known(self, key: str) -> bool:
        """Checked at all (present OR confirmed absent)."""
        return key in self.facts

    def is_absent(self, key: str) -> bool:
        f = self.facts.get(key)
        return f is not None and f.status == FACT_ABSENT

    def value(self, key: str, default: Any = None) -> Any:
        """The value of a PRESENT fact, else `default`. An absent or unknown fact has no value."""
        f = self.facts.get(key)
        if f is None or f.status != FACT_PRESENT or f.value is None:
            return default
        return f.value

    def with_prefix(self, prefix: str) -> Dict[str, Fact]:
        return {k: f for k, f in self.facts.items() if k.startswith(prefix)}

    def coverage(self, keys: Iterable[str]) -> float:
        """Share of `keys` that are known (present or confirmed absent). 1.0 for an empty list."""
        keys = list(keys)
        if not keys:
            return 1.0
        return sum(1 for k in keys if k in self.facts) / len(keys)

    def best_grade(self, keys: Iterable[str]) -> Optional[str]:
        grades = [self.facts[k].grade for k in keys if k in self.facts and self.facts[k].status == FACT_PRESENT]
        return min(grades, key=GRADE_RANK.get) if grades else None

    # -- writing
    def add(self, fact: Fact) -> None:
        """Add a fact. Two readers producing the same key: the stronger grade wins; at equal grade the more
        recent as_of wins; a differing value is recorded in `conflicts` so it is never silently dropped."""
        old = self.facts.get(fact.key)
        if old is None:
            self.facts[fact.key] = fact
            return
        if old.value != fact.value or old.status != fact.status:
            self.conflicts.append({"key": fact.key, "kept_source": None, "old": old.to_dict(), "new": fact.to_dict()})
        keep_new = _prefer(fact, old)
        if keep_new:
            self.facts[fact.key] = fact
        if old.value != fact.value or old.status != fact.status:
            self.conflicts[-1]["kept_source"] = (fact if keep_new else old).source

    # -- identity & serialisation
    def fingerprint(self) -> str:
        """Stable hash of the facts themselves (not of when they were built): the cache key for everything
        derived from them. Same facts in, same fingerprint out."""
        payload = [(k, _jsonable(f.value), f.status, f.grade, f.as_of) for k, f in sorted(self.facts.items())]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": self.schema_version, "company_id": self.company_id, "built_at": self.built_at,
                "reader_errors": dict(self.reader_errors), "conflicts": list(self.conflicts),
                "facts": {k: f.to_dict() for k, f in sorted(self.facts.items())}}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CompanyFacts":
        return cls(company_id=d["company_id"], built_at=d.get("built_at"),
                   reader_errors=dict(d.get("reader_errors") or {}), conflicts=list(d.get("conflicts") or []),
                   facts={k: Fact.from_dict(v) for k, v in (d.get("facts") or {}).items()},
                   schema_version=d.get("schema_version", SCHEMA_VERSION))


def _prefer(new: Fact, old: Fact) -> bool:
    """True when `new` should replace `old`: stronger grade first, then the later as_of (ISO strings sort by date;
    a missing date loses)."""
    if new.grade != old.grade:
        return GRADE_RANK[new.grade] < GRADE_RANK[old.grade]
    return (new.as_of or "") > (old.as_of or "")


# --------------------------------------------------------------------------------------------- hypotheses

@dataclass(frozen=True)
class EvidenceItem:
    fact_key: str
    role: str          # supports | contradicts | context
    text: str          # one sentence a human can read aloud, quoting the value and the comparison

    def __post_init__(self):
        if self.role not in EVIDENCE_ROLES:
            raise ValueError(f"EvidenceItem role must be one of {EVIDENCE_ROLES}, got {self.role!r}")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HypothesisSpec:
    """The static definition of a hypothesis: what is suspected, what the rule needs, how to test it live.
    id is BRANCH-NN (e.g. 'PRF-01'); the seed list is in docs/DIAGNOSIS_MASTERPLAN.md, section 6."""
    id: str
    branch: str                       # key of BRANCHES
    title: str                        # short, noun phrase: "Margin squeezed by input costs"
    statement: str                    # the claim as it would be said on a call; may use {company}
    requires: Tuple[str, ...] = ()    # fact keys the rule cannot work without (drive `coverage`)
    helpful: Tuple[str, ...] = ()     # fact keys that sharpen it
    disconfirm: str = ""              # what, if true, would make this wrong: said out loud to the analyst
    pilot_tags: Tuple[str, ...] = ()  # pilot-library tags this hypothesis points to
    questions: Tuple[str, ...] = ()   # discovery questions to confirm or kill it on the call
    positive: bool = False            # True for an opportunity/strength hypothesis (e.g. cash-rich and under-investing)

    def __post_init__(self):
        if self.branch not in BRANCHES:
            raise ValueError(f"{self.id}: branch must be one of {sorted(BRANCHES)}, got {self.branch!r}")
        if not self.id.startswith(self.branch + "-"):
            raise ValueError(f"hypothesis id {self.id!r} must start with its branch ({self.branch}-)")

    def to_dict(self) -> Dict[str, Any]:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(self).items()}


@dataclass
class HypothesisResult:
    spec_id: str
    status: str = HYP_NOT_ASSESSABLE
    strength: float = 0.0             # 0-100: how strongly the evidence points at the problem. Not a Need score.
    confidence: str = CONF_LOW
    evidence: List[EvidenceItem] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)    # fact keys that would sharpen or settle it
    coverage: float = 0.0             # share of `requires` known
    notes: str = ""

    def __post_init__(self):
        if self.status not in HYP_STATUSES:
            raise ValueError(f"{self.spec_id}: status must be one of {HYP_STATUSES}, got {self.status!r}")
        if self.confidence not in CONFIDENCES:
            raise ValueError(f"{self.spec_id}: confidence must be one of {CONFIDENCES}, got {self.confidence!r}")
        self.strength = max(0.0, min(100.0, float(self.strength)))

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["evidence"] = [e.to_dict() for e in self.evidence]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HypothesisResult":
        d = dict(d)
        d["evidence"] = [EvidenceItem(**e) for e in d.get("evidence") or []]
        return cls(**d)


# --------------------------------------------------------------------------------------------- triggers

@dataclass(frozen=True)
class Trigger:
    """A dated event that makes now a reasonable moment to call (or a bad one). Verified, not guessed."""
    id: str
    kind: str
    text: str
    happened_on: Optional[str] = None      # ISO date; None for an undated but real condition (e.g. a standing deadline)
    due_on: Optional[str] = None           # ISO date a future deadline falls on
    source: str = ""
    url: Optional[str] = None
    grade: str = GRADE_C
    weight: float = 0.5                    # 0-1 importance before recency decay
    polarity: str = "opening"              # opening (good moment to call) | closing (bad moment / red flag)

    def __post_init__(self):
        if self.kind not in TRIGGER_KINDS:
            raise ValueError(f"Trigger kind must be one of {TRIGGER_KINDS}, got {self.kind!r}")
        if self.grade not in GRADES:
            raise ValueError(f"Trigger grade must be one of {GRADES}, got {self.grade!r}")
        if self.polarity not in ("opening", "closing"):
            raise ValueError("Trigger polarity must be 'opening' or 'closing'")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------------------------- pilots

@dataclass(frozen=True)
class PilotConcept:
    """A pilot archetype GG can pitch (the pilot library entry). Not a startup: a type of solution and the
    time-boxed experiment around it. Startups are matched to concepts, never the other way round."""
    id: str                           # PIL-NN
    title: str
    problem: str                      # the pain in the customer's words
    tags: Tuple[str, ...]             # matches HypothesisSpec.pilot_tags
    solution_types: Tuple[str, ...]   # kinds of startup solution that fit
    duration_weeks: Tuple[int, int]   # (min, max)
    budget_eur: Tuple[int, int]       # typical customer-side pilot budget band (low, high)
    kpis: Tuple[str, ...]             # what a successful pilot measures
    prerequisites: Tuple[str, ...] = ()   # what the company needs to have for it to work (data, a champion, an asset)
    not_when: Tuple[str, ...] = ()        # when NOT to pitch it
    sources: Tuple[str, ...] = ()         # real analogous cases / references this was built from (URLs)

    def to_dict(self) -> Dict[str, Any]:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(self).items()}


# --------------------------------------------------------------------------------------------- call pack

@dataclass
class CallPack:
    """The deliverable: what to know and say before the first call. Rendered by diagnosis.callpack (D17)."""
    company_id: str
    generated_at: str
    facts_fingerprint: str
    headline: str = ""                                         # one sentence: who they are and why we might care
    snapshot: Dict[str, Any] = field(default_factory=dict)     # identity, size, money, ownership, people: the 30-second read
    why_now: List[Dict[str, Any]] = field(default_factory=list)       # Trigger dicts, most relevant first
    hypotheses: List[Dict[str, Any]] = field(default_factory=list)    # {spec, result, pilots} for the top N
    questions: List[str] = field(default_factory=list)
    unknowns: List[Dict[str, Any]] = field(default_factory=list)      # {fact_key, why_it_matters, how_to_find_out}
    red_flags: List[str] = field(default_factory=list)
    stakeholders: List[Dict[str, Any]] = field(default_factory=list)
    tier: str = "HOLD"
    tier_reasons: List[str] = field(default_factory=list)
    archetype: str = ""
    narrative: Dict[str, Any] = field(default_factory=dict)    # synthesis output: text + the evidence ids each claim cites
    llm_model: Optional[str] = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        if self.tier not in CALL_TIERS:
            raise ValueError(f"CallPack tier must be one of {CALL_TIERS}, got {self.tier!r}")

    def to_dict(self) -> Dict[str, Any]:
        return _jsonable(asdict(self))

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CallPack":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})
