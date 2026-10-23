"""
Auto-discovered extension points, so several agents can add to the diagnosis layer without editing a shared file.

    @reader("financial")                      # D01/D02: one data layer -> Facts
    def read_financial(db, company, ctx): ...  # returns an iterable of Fact

    @deriver("peer_percentiles", after=("financial",))   # needs other facts first
    def derive_peers(facts, ctx): ...          # returns an iterable of Fact

    @hypothesis(HypothesisSpec(id="PRF-01", ...))
    def prf_01(facts, ctx): ...                # returns a HypothesisResult

    @trigger_rule("leadership")
    def leadership(facts, ctx): ...            # returns an iterable of Trigger

Drop a module into diagnosis/readers, diagnosis/hypotheses or diagnosis/triggers and decorate; discover() imports
every module in those packages (and any diagnosis/factkeys_ext_*.py and diagnosis/models_*.py), so nothing else needs
to change. A reader that raises is recorded in CompanyFacts.reader_errors and never aborts the build: one broken
crawler layer must not take the whole diagnosis down.

Honesty guard: build_facts() drops (and records) any Fact whose key is not declared in factkeys.FACT_KEYS, so
two agents cannot invent two names for one thing.
"""

from __future__ import annotations

import importlib
import pkgutil
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from diagnosis.contracts import CompanyFacts, Fact, HypothesisResult, HypothesisSpec, Trigger
from diagnosis.factkeys import FACT_KEYS

_readers: Dict[str, Callable] = {}
_derivers: Dict[str, Tuple[Callable, Tuple[str, ...]]] = {}
_hypotheses: Dict[str, Tuple[HypothesisSpec, Callable]] = {}
_trigger_rules: Dict[str, Callable] = {}

PACKAGES = ("diagnosis.readers", "diagnosis.hypotheses", "diagnosis.triggers")
TOP_LEVEL_PREFIXES = ("factkeys_ext_", "models_")


@dataclass
class BuildContext:
    """What a reader, deriver, hypothesis or trigger rule may need besides the facts. Everything optional."""
    today: date = field(default_factory=date.today)
    peer_index: Any = None            # D03: peer statistics for the cohort
    extras: Dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------------------------- decorators

def reader(name: str):
    def deco(fn):
        if name in _readers and _readers[name].__module__ != fn.__module__:
            raise ValueError(f"reader {name!r} already registered by {_readers[name].__module__}")
        _readers[name] = fn
        return fn
    return deco


def deriver(name: str, after: Iterable[str] = ()):
    def deco(fn):
        if name in _derivers and _derivers[name][0].__module__ != fn.__module__:
            raise ValueError(f"deriver {name!r} already registered by {_derivers[name][0].__module__}")
        _derivers[name] = (fn, tuple(after))
        return fn
    return deco


def hypothesis(spec: HypothesisSpec):
    for k in tuple(spec.requires) + tuple(spec.helpful):
        if k not in FACT_KEYS:
            raise ValueError(f"{spec.id} names fact key {k!r}, which is not declared in diagnosis.factkeys")

    def deco(fn):
        if spec.id in _hypotheses and _hypotheses[spec.id][1].__module__ != fn.__module__:
            raise ValueError(f"hypothesis {spec.id} already registered by {_hypotheses[spec.id][1].__module__}")
        _hypotheses[spec.id] = (spec, fn)
        return fn
    return deco


def trigger_rule(name: str):
    def deco(fn):
        _trigger_rules[name] = fn
        return fn
    return deco


# ----------------------------------------------------------------------------------------------- discovery

_discovered = False


def discover(force: bool = False) -> None:
    """Import every module that can register something. Idempotent."""
    global _discovered
    if _discovered and not force:
        return
    for pkg_name in PACKAGES:
        pkg = importlib.import_module(pkg_name)
        for m in pkgutil.iter_modules(pkg.__path__):
            importlib.import_module(f"{pkg_name}.{m.name}")
    top = importlib.import_module("diagnosis")
    for m in pkgutil.iter_modules(top.__path__):
        if m.name.startswith(TOP_LEVEL_PREFIXES):
            importlib.import_module(f"diagnosis.{m.name}")
    _discovered = True


# ----------------------------------------------------------------------------------------------- accessors

def readers() -> Dict[str, Callable]:
    discover()
    return dict(_readers)


def derivers() -> Dict[str, Tuple[Callable, Tuple[str, ...]]]:
    discover()
    return dict(_derivers)


def hypotheses() -> Dict[str, Tuple[HypothesisSpec, Callable]]:
    discover()
    return dict(_hypotheses)


def trigger_rules() -> Dict[str, Callable]:
    discover()
    return dict(_trigger_rules)


def _ordered_derivers() -> List[str]:
    """Derivers in dependency order (`after`), ties broken by name so a build is reproducible."""
    names = sorted(_derivers)
    done: List[str] = []
    seen: set = set()

    def visit(n: str, stack: Tuple[str, ...]) -> None:
        if n in seen:
            return
        if n in stack:
            raise ValueError(f"deriver cycle: {' -> '.join(stack + (n,))}")
        for dep in _derivers[n][1]:
            if dep in _derivers:
                visit(dep, stack + (n,))
        seen.add(n)
        done.append(n)

    for n in names:
        visit(n, ())
    return done


# ----------------------------------------------------------------------------------------------- building

def _accept(facts: CompanyFacts, produced: Any, who: str) -> None:
    for f in produced or []:
        if not isinstance(f, Fact):
            facts.reader_errors[who] = f"emitted {type(f).__name__}, not Fact"
            continue
        if f.key not in FACT_KEYS:
            facts.reader_errors[f"{who}:{f.key}"] = "fact key not declared in diagnosis.factkeys (dropped)"
            continue
        facts.add(f)


def build_facts(db, company, ctx: Optional[BuildContext] = None, only: Optional[Iterable[str]] = None) -> CompanyFacts:
    """Run every registered reader, then every deriver, for one company. `only` restricts to named readers/derivers
    (tests, partial refresh). Never raises because of a reader."""
    discover()
    ctx = ctx or BuildContext()
    only_set = set(only) if only is not None else None
    facts = CompanyFacts(company_id=getattr(company, "id", str(company)), built_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    for name in sorted(_readers):
        if only_set is not None and name not in only_set:
            continue
        try:
            _accept(facts, _readers[name](db, company, ctx), name)
        except Exception:   # a broken layer is data about the layer, not a reason to stop
            facts.reader_errors[name] = traceback.format_exc(limit=3)
    for name in _ordered_derivers():
        if only_set is not None and name not in only_set:
            continue
        try:
            _accept(facts, _derivers[name][0](facts, ctx), name)
        except Exception:
            facts.reader_errors[name] = traceback.format_exc(limit=3)
    return facts


def run_hypotheses(facts: CompanyFacts, ctx: Optional[BuildContext] = None,
                   only: Optional[Iterable[str]] = None) -> Dict[str, HypothesisResult]:
    """Evaluate every registered hypothesis against the facts. A rule that raises yields not_assessable with the
    error in `notes`; it never hides the other hypotheses."""
    discover()
    ctx = ctx or BuildContext()
    only_set = set(only) if only is not None else None
    out: Dict[str, HypothesisResult] = {}
    for hid in sorted(_hypotheses):
        if only_set is not None and hid not in only_set:
            continue
        spec, fn = _hypotheses[hid]
        try:
            res = fn(facts, ctx)
            if not isinstance(res, HypothesisResult) or res.spec_id != spec.id:
                raise TypeError(f"{spec.id} returned {type(res).__name__} / spec_id mismatch")
        except Exception:
            res = HypothesisResult(spec_id=spec.id, notes="rule error: " + traceback.format_exc(limit=2))
        out[hid] = res
    return out


def run_trigger_rules(facts: CompanyFacts, ctx: Optional[BuildContext] = None) -> Tuple[List[Trigger], Dict[str, str]]:
    discover()
    ctx = ctx or BuildContext()
    triggers: List[Trigger] = []
    errors: Dict[str, str] = {}
    for name in sorted(_trigger_rules):
        try:
            triggers.extend(t for t in (_trigger_rules[name](facts, ctx) or []) if isinstance(t, Trigger))
        except Exception:
            errors[name] = traceback.format_exc(limit=3)
    return triggers, errors


def clear_for_tests() -> None:
    """Empty every registry. Tests only."""
    global _discovered
    _readers.clear(); _derivers.clear(); _hypotheses.clear(); _trigger_rules.clear()
    _discovered = False
