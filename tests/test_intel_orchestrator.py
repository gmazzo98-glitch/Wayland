import json
import time

from intel.budget import Budget, RunBudgetExceeded
from intel.collector import Collector, CollectorContext, CollectorResult
from intel.ledger import Evidence, Fact, Ledger, quote_in_text
from intel.orchestrator import CHECKLIST_CATEGORIES, DeepProfileOrchestrator


def _collector(name, key, text="Company Alpha evidence", requires=("web",)):
    def run(ctx: CollectorContext):
        return CollectorResult(facts=[Fact(
            key=key, value="found", grade="B", confidence=.8, verified=True,
            evidence=[Evidence(source_type="company_site", url="https://example.test", quote=text)],
            collector="test:%s" % name,
        )], pages_fetched=1, queries=[name])
    return Collector(name=name, ticket="TEST", produces=(key.rsplit(".", 1)[0] + ".",),
                     requires=requires, run=run)


class Page:
    text = "Company Alpha evidence"


def test_budget_hard_deadline_and_tokens():
    b = Budget(max_llm_tokens=10, deadline_seconds=0)
    try:
        b.check()
    except RunBudgetExceeded:
        pass
    else:
        raise AssertionError("expired budget did not stop")


def test_plan_skips_missing_capabilities_and_worker_without_adapter():
    web = _collector("web", "identity.name")
    llm = _collector("llm", "people.name", requires=("web", "llm"))
    worker = _collector("worker", "documents.item", requires=("web", "worker"))
    o = DeepProfileOrchestrator(collectors={c.name: c for c in (web, llm, worker)}, fetch=lambda _: Page())
    assert [c.name for c in o.plan()] == ["web"]


def test_worker_adapter_unlocks_worker_collector_and_runs_it():
    worker = _collector("worker", "documents.item", requires=("web", "worker"))

    def run_worker(coll, ctx):
        return coll.run(ctx)

    o = DeepProfileOrchestrator(collectors={worker.name: worker}, fetch=lambda _: Page(),
                                worker_runner=run_worker)
    assert [c.name for c in o.plan()] == ["worker"]
    out = o.run({"id": "a", "legal_name": "Company Alpha"},
                budget=Budget(max_rounds=1, coverage_target=0.01))
    assert len(out.ledger.facts) == 1


def test_checkpoint_resume_skips_finished_collectors(tmp_path):
    calls = []

    def first(ctx):
        calls.append("first")
        return CollectorResult(facts=[Fact(
            key="identity.name", value="found", grade="B", confidence=.8, verified=True,
            evidence=[Evidence(source_type="company_site", url="https://example.test", quote="Company Alpha evidence")],
        )])

    def second(ctx):
        calls.append("second")
        return CollectorResult(facts=[Fact(
            key="digital.stack", value="found", grade="B", confidence=.8, verified=True,
            evidence=[Evidence(source_type="company_site", url="https://example.test", quote="Company Alpha evidence")],
        )])

    c1 = Collector("first", "TEST", ("identity.",), first)
    c2 = Collector("second", "TEST", ("digital.",), second)
    path = str(tmp_path / "resume.json")
    o = DeepProfileOrchestrator(collectors={c.name: c for c in (c1, c2)}, fetch=lambda _: Page())
    out = o.run({"id": "a", "legal_name": "Company Alpha"}, budget=Budget(max_rounds=1, coverage_target=1),
                checkpoint_path=path)
    assert calls == ["first", "second"]
    resumed = o.run({"id": "a", "legal_name": "Company Alpha"}, budget=Budget(max_rounds=1, coverage_target=1),
                    checkpoint_path=path)
    assert calls == ["first", "second"]
    assert len(resumed.ledger.facts) == len(out.ledger.facts)


def test_orchestrator_merges_per_collector_and_records_absence(tmp_path):
    c1 = _collector("identity", "identity.name")
    c2 = _collector("digital", "digital.stack")
    o = DeepProfileOrchestrator(collectors={c.name: c for c in (c1, c2)}, fetch=lambda _: Page(), max_workers=2)
    out = o.run({"id": "a", "legal_name": "Company Alpha", "website_url": "https://example.test"},
                budget=Budget(max_rounds=1, coverage_target=1.0), checkpoint_path=str(tmp_path / "run.json"))
    assert len(out.ledger.facts) == 2
    assert len(out.ledger.runs) == 2
    assert out.coverage["known"] == ["digital", "identity"]
    assert json.loads((tmp_path / "run.json").read_text())["run_id"] == out.run_id


def test_verification_gate_is_called_before_merge():
    called = []
    c = _collector("identity", "identity.name")

    def verifier(fact, page_text, company, ledger, llm=None):
        called.append((fact.key, page_text))
        fact.verified = True
        return type("Verdict", (), {"decision": "verified"})()

    o = DeepProfileOrchestrator(collectors={c.name: c}, fetch=lambda _: Page(), verifier=verifier)
    out = o.run({"id": "a", "legal_name": "Company Alpha"}, budget=Budget(max_rounds=1, coverage_target=1.0))
    assert called == [("identity.name", "Company Alpha evidence\nhttps://example.test")]
    assert out.collectors[0].verification == {"verified": 1}


def test_follow_up_planner_is_optional_and_never_required_for_a_run():
    c = _collector("identity", "identity.name")

    class LLM:
        def complete(self, **kwargs):
            raise RuntimeError("no provider")

    o = DeepProfileOrchestrator(collectors={c.name: c}, fetch=lambda _: Page(), llm=LLM())
    out = o.run({"id": "a", "legal_name": "Company Alpha"}, budget=Budget(max_rounds=2, coverage_target=0.01))
    assert out.stopped_reason == "coverage target"
    assert out.follow_up_queries == []


def test_quote_rule_used_by_test_fixture():
    assert quote_in_text("Company Alpha evidence", "Company Alpha evidence")
    assert len(CHECKLIST_CATEGORIES) >= 10
