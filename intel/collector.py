"""
The contract every deep-intelligence collector implements, and the registry that finds them.

Why this exists: today a new source means editing company_service.py (PHASE_7_SOURCES, plan_source_steps,
eligible_phase7_sources) and views/pipeline_health.py, so ten agents adding ten collectors collide on the same
few files. Here a collector is ONE new file, `intel/collectors/<name>.py`, that exposes a module-level
`COLLECTOR = Collector(...)`. `discover()` imports every module in that package; the orchestrator (ticket I30)
runs whatever it finds. Nothing else needs editing to add a collector.

A collector never writes to the database. It receives a CollectorContext and returns a CollectorResult of
Facts; the orchestrator validates them (intel/ledger.py), runs the verification gate, merges them into the
ledger and saves. This keeps collectors pure enough to unit-test with recorded pages, and means a collector
that runs on a helper's PC through the existing Crawler Worker (a Node crawler wrapped by a Python collector)
needs no database access of its own, exactly like the crawlers today.

A collector that finds nothing must say so: return `checked_absent` keys (an "absent" fact is written for
them) rather than an empty result, otherwise "looked and found nothing" and "never looked" are indistinguishable.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from intel.ledger import CATEGORIES, Fact, Ledger

# What a collector may require from its environment. The orchestrator skips (and records why) a collector whose
# requirements are not met, instead of letting it fail halfway. Names match the `needs` vocabulary of the boards.
CAPABILITIES = ("web", "llm", "search", "browser", "worker", "gg_approval")


@dataclass
class Budget:
    """Hard limits for one collector run on one company. A collector must stop at the first one it reaches."""
    max_pages: int = 60
    max_llm_tokens: int = 60_000
    max_search_queries: int = 12
    deadline_seconds: int = 240


@dataclass
class CollectorContext:
    company: Dict[str, Any]                 # id, legal_name, vat, website_url, country, province, sector_name, headcount ...
    ledger: Ledger                          # what earlier collectors already established (read it, do not mutate it)
    budget: Budget = field(default_factory=Budget)
    llm: Optional[Any] = None               # intel LLM pool (ticket I04); None in unit tests
    search: Optional[Any] = None            # intel search client (ticket I05); None in unit tests
    fetch: Optional[Callable[[str], Any]] = None   # polite fetcher: robots-aware, rate-limited, returns text + headers
    log: Callable[[str], None] = lambda msg: None


@dataclass
class CollectorResult:
    facts: List[Fact] = field(default_factory=list)
    checked_absent: List[str] = field(default_factory=list)   # fact keys actively checked and confirmed not there
    pages_fetched: int = 0
    queries: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)           # human-readable; never raise for an expected failure


@dataclass(frozen=True)
class Collector:
    name: str                               # unique, kebab-case, e.g. "deep-site-reader"
    ticket: str                             # the roadmap ticket that owns it, e.g. "I10"
    produces: Sequence[str]                 # fact key prefixes it may emit, e.g. ("products.", "certifications.")
    run: Callable[[CollectorContext], CollectorResult]
    requires: Sequence[str] = ("web",)
    refresh_days: int = 90                  # after this long its facts count as stale and it may run again
    cost_hint: str = "cheap"                # cheap | moderate | expensive (LLM tokens / pages), for batch planning
    description: str = ""

    def validate(self) -> None:
        if not self.name or self.name != self.name.lower() or " " in self.name:
            raise ValueError(f"collector name {self.name!r} must be lowercase kebab-case")
        if not self.ticket:
            raise ValueError(f"{self.name}: the owning ticket is required (traceability)")
        if not self.produces:
            raise ValueError(f"{self.name}: declare the fact key prefixes it produces")
        for p in self.produces:
            if p.split(".", 1)[0] not in CATEGORIES:
                raise ValueError(f"{self.name}: produces {p!r}; the group must be one of {CATEGORIES}")
        for r in self.requires:
            if r not in CAPABILITIES:
                raise ValueError(f"{self.name}: unknown requirement {r!r}; use {CAPABILITIES}")

    def may_emit(self, key: str) -> bool:
        return any(key.startswith(p) for p in self.produces)


def check_result(collector: Collector, result: CollectorResult) -> List[str]:
    """Refusals for facts the collector had no right to emit (outside its declared prefixes). One producer per key."""
    return [f"{collector.name} emitted {f.key!r} outside its declared prefixes {list(collector.produces)}"
            for f in result.facts if not collector.may_emit(f.key)]


def discover(package: str = "intel.collectors") -> Dict[str, Collector]:
    """Import every module of the package and collect its COLLECTOR. A module that fails to import is reported by
    name rather than hiding the others (one agent's broken file must not stop the portfolio run)."""
    found: Dict[str, Collector] = {}
    try:
        pkg = importlib.import_module(package)
    except ModuleNotFoundError:
        return found
    for info in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f"{package}.{info.name}")
        collector = getattr(mod, "COLLECTOR", None)
        if collector is None:
            continue
        collector.validate()
        if collector.name in found:
            raise ValueError(f"two collectors are named {collector.name!r}")
        found[collector.name] = collector
    return found
