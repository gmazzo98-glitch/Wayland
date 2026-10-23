"""The diagnosis contracts, fact vocabulary and registry: the parts every parallel ticket depends on."""

import pytest

from diagnosis import registry
from diagnosis.contracts import (
    CallPack, CompanyFacts, EvidenceItem, Fact, HypothesisResult, HypothesisSpec, PilotConcept, Trigger,
    GRADE_A, GRADE_B, GRADE_C, FACT_ABSENT, HYP_SUPPORTED,
)
from diagnosis.factkeys import FACT_KEYS, FactKey, KEY_PATTERN, declare_keys, keys_owned_by


# ------------------------------------------------------------------------------------------------ facts

def test_unknown_is_not_absent():
    facts = CompanyFacts(company_id="c1")
    facts.add(Fact("prof.has_aftermarket", False, status=FACT_ABSENT, grade=GRADE_B))
    assert facts.known("prof.has_aftermarket") and facts.is_absent("prof.has_aftermarket")
    assert facts.value("prof.has_aftermarket", "dflt") == "dflt"      # an absent fact has no value
    assert not facts.known("prof.remote_service")                      # never checked is a different thing
    assert not facts.is_absent("prof.remote_service")


def test_stronger_grade_wins_and_conflict_is_recorded():
    facts = CompanyFacts(company_id="c1")
    facts.add(Fact("id.headcount", 80, grade=GRADE_C, source="press", as_of="2026-01-01"))
    facts.add(Fact("id.headcount", 72, grade=GRADE_A, source="AIDA", as_of="2025-12-31"))
    assert facts.value("id.headcount") == 72
    assert facts.conflicts and facts.conflicts[0]["kept_source"] == "AIDA"
    # equal grade: the later as_of wins
    facts.add(Fact("id.headcount", 75, grade=GRADE_A, source="AIDA", as_of="2026-06-30"))
    assert facts.value("id.headcount") == 75


def test_identical_duplicate_is_not_a_conflict():
    facts = CompanyFacts(company_id="c1")
    facts.add(Fact("id.headcount", 72, grade=GRADE_A))
    facts.add(Fact("id.headcount", 72, grade=GRADE_A))
    assert facts.conflicts == []


def test_fingerprint_ignores_build_time_but_not_content():
    a = CompanyFacts(company_id="c1", built_at="2026-01-01")
    b = CompanyFacts(company_id="c1", built_at="2026-12-31")
    for f in (a, b):
        f.add(Fact("fin.revenue_k", 1000.0, grade=GRADE_A, as_of="2025-12-31"))
    assert a.fingerprint() == b.fingerprint()
    b.add(Fact("fin.ebit_k", 5.0, grade=GRADE_A))
    assert a.fingerprint() != b.fingerprint()


def test_round_trip_through_json():
    facts = CompanyFacts(company_id="c1", built_at="x")
    facts.add(Fact("fin.revenue_series", {"latest": 10.0, "y-1": float("nan")}, grade=GRADE_A))
    again = CompanyFacts.from_dict(facts.to_dict())
    assert again.value("fin.revenue_series") == {"latest": 10.0, "y-1": None}      # NaN never leaks into JSON
    assert again.fingerprint() == facts.fingerprint()


def test_bad_grade_and_status_are_rejected():
    with pytest.raises(ValueError):
        Fact("id.headcount", 1, grade="Z")
    with pytest.raises(ValueError):
        Fact("id.headcount", 1, status="maybe")


def test_coverage_and_best_grade():
    facts = CompanyFacts(company_id="c1")
    facts.add(Fact("fin.revenue_k", 1.0, grade=GRADE_A))
    facts.add(Fact("dig.cms_platform", "wp", grade=GRADE_B))
    assert facts.coverage(["fin.revenue_k", "fin.ebit_k"]) == 0.5
    assert facts.coverage([]) == 1.0
    assert facts.best_grade(["fin.revenue_k", "dig.cms_platform"]) == GRADE_A
    assert facts.best_grade(["fin.ebit_k"]) is None


# ------------------------------------------------------------------------------------------------ other shapes

def test_hypothesis_spec_validates_id_and_branch():
    HypothesisSpec(id="PRF-01", branch="PRF", title="t", statement="s")
    with pytest.raises(ValueError):
        HypothesisSpec(id="COM-01", branch="PRF", title="t", statement="s")
    with pytest.raises(ValueError):
        HypothesisSpec(id="XXX-01", branch="XXX", title="t", statement="s")


def test_result_strength_is_clamped_and_serialises():
    r = HypothesisResult(spec_id="PRF-01", status=HYP_SUPPORTED, strength=140, evidence=[EvidenceItem("fin.ebit_margin_pct", "supports", "1.2%")])
    assert r.strength == 100.0
    assert HypothesisResult.from_dict(r.to_dict()).evidence[0].fact_key == "fin.ebit_margin_pct"
    with pytest.raises(ValueError):
        HypothesisResult(spec_id="PRF-01", status="great")


def test_trigger_and_pilot_and_callpack_construct():
    Trigger(id="t1", kind="leadership_change", text="New CEO")
    with pytest.raises(ValueError):
        Trigger(id="t2", kind="not_a_kind", text="x")
    PilotConcept(id="PIL-01", title="t", problem="p", tags=("a",), solution_types=("s",), duration_weeks=(6, 12),
                 budget_eur=(10000, 50000), kpis=("k",))
    pack = CallPack(company_id="c1", generated_at="now", facts_fingerprint="f", tier="A")
    assert CallPack.from_dict(pack.to_dict()).tier == "A"
    with pytest.raises(ValueError):
        CallPack(company_id="c1", generated_at="now", facts_fingerprint="f", tier="Z")


# ------------------------------------------------------------------------------------------------ fact vocabulary

def test_every_fact_key_is_well_formed_and_owned():
    assert len(FACT_KEYS) > 150
    for k in FACT_KEYS.values():
        assert KEY_PATTERN.match(k.key), k.key
        assert k.owner and k.owner[0] in "DR" and k.owner[1:].isdigit(), (k.key, k.owner)
        assert k.label, k.key
    assert keys_owned_by("D01") and keys_owned_by("D11") and keys_owned_by("D05")


def test_duplicate_key_declaration_is_refused():
    with pytest.raises(ValueError):
        declare_keys([FactKey("id.vat", "dup", "", "text", "D99", "exists")])
    with pytest.raises(ValueError):
        FactKey("Bad Key", "x", "", "text", "D99", "exists")


# ------------------------------------------------------------------------------------------------ registry

@pytest.fixture
def clean_registry():
    # Snapshot and restore: other tests (and real modules) may have registered things.
    saved = (dict(registry._readers), dict(registry._derivers), dict(registry._hypotheses),
             dict(registry._trigger_rules), registry._discovered)
    registry.clear_for_tests()
    registry._discovered = True            # do not auto-import real modules during these unit tests
    yield
    registry._readers.clear(); registry._derivers.clear(); registry._hypotheses.clear(); registry._trigger_rules.clear()
    registry._readers.update(saved[0]); registry._derivers.update(saved[1]); registry._hypotheses.update(saved[2])
    registry._trigger_rules.update(saved[3]); registry._discovered = saved[4]


class _Co:
    id = "c1"


def test_build_isolates_a_failing_reader_and_drops_undeclared_keys(clean_registry):
    @registry.reader("good")
    def good(db, company, ctx):
        return [Fact("fin.revenue_k", 1000.0, grade=GRADE_A), Fact("made.up_key", 1, grade=GRADE_A)]

    @registry.reader("boom")
    def boom(db, company, ctx):
        raise RuntimeError("layer down")

    facts = registry.build_facts(None, _Co())
    assert facts.value("fin.revenue_k") == 1000.0                  # the good reader still delivered
    assert "boom" in facts.reader_errors and "layer down" in facts.reader_errors["boom"]
    assert "good:made.up_key" in facts.reader_errors               # undeclared key dropped, and said so
    assert not facts.known("made.up_key")


def test_derivers_run_in_dependency_order(clean_registry):
    order = []

    @registry.deriver("b_second", after=("a_first",))
    def second(facts, ctx):
        order.append("b"); return [Fact("fin.ebit_margin_pct", facts.value("fin.ebitda_margin_pct") / 2, grade=GRADE_A)]

    @registry.deriver("a_first")
    def first(facts, ctx):
        order.append("a"); return [Fact("fin.ebitda_margin_pct", 10.0, grade=GRADE_A)]

    facts = registry.build_facts(None, _Co())
    assert order == ["a", "b"] and facts.value("fin.ebit_margin_pct") == 5.0


def test_deriver_cycle_is_detected(clean_registry):
    @registry.deriver("x", after=("y",))
    def x(facts, ctx): return []

    @registry.deriver("y", after=("x",))
    def y(facts, ctx): return []

    facts = registry.CompanyFacts(company_id="c1")
    with pytest.raises(ValueError):
        registry._ordered_derivers()


def test_hypothesis_must_use_declared_keys_and_a_rule_error_is_contained(clean_registry):
    with pytest.raises(ValueError):
        registry.hypothesis(HypothesisSpec(id="PRF-90", branch="PRF", title="t", statement="s", requires=("nope.key",)))

    spec_ok = HypothesisSpec(id="PRF-91", branch="PRF", title="ok", statement="s", requires=("fin.ebit_margin_pct",))
    spec_bad = HypothesisSpec(id="PRF-92", branch="PRF", title="bad", statement="s")

    @registry.hypothesis(spec_ok)
    def ok(facts, ctx):
        return HypothesisResult(spec_id="PRF-91", status=HYP_SUPPORTED, strength=80)

    @registry.hypothesis(spec_bad)
    def bad(facts, ctx):
        raise KeyError("oops")

    out = registry.run_hypotheses(CompanyFacts(company_id="c1"))
    assert out["PRF-91"].status == HYP_SUPPORTED
    assert out["PRF-92"].status == "not_assessable" and "rule error" in out["PRF-92"].notes


def test_real_modules_import_cleanly():
    """discover() must import every shipped module without error: this is the check that a ticket's new module
    did not break auto-discovery for everyone else."""
    registry.discover(force=True)
    for hid, (spec, fn) in registry.hypotheses().items():
        assert spec.id == hid
