"""Budgeted, resumable deep-profile orchestration (roadmap ticket I30).

Collectors remain pure: this module is the only layer that plans them, runs
them in parallel, verifies their facts, merges them into a ledger, and saves a
checkpoint.  It is deliberately usable without the application database so
that recorded-page tests and ``scripts/intel_run.py`` can exercise the same
logic.
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from intel.budget import Budget, RunBudgetExceeded
from intel.collector import Budget as CollectorBudget
from intel.collector import Collector, CollectorContext, CollectorResult, discover
from intel.ledger import Evidence, Fact, Ledger, load_ledger, save_ledger


# The checklist is intentionally category-based, not a list of one-company facts.  It
# remains useful for companies whose profile has a different shape.
CHECKLIST_CATEGORIES = (
    "identity", "ownership", "events", "financials", "people", "contact", "products",
    "certifications", "locations", "networks", "digital", "hiring", "customers", "peers",
    "documents", "ip", "funding", "risk",
)


@dataclass
class CollectorRun:
    name: str
    status: str
    facts: int = 0
    absent: List[str] = field(default_factory=list)
    refused: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    pages: int = 0
    queries: List[str] = field(default_factory=list)
    seconds: float = 0.0
    verification: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class OrchestrationResult:
    run_id: str
    ledger: Ledger
    collectors: List[CollectorRun]
    coverage: Dict[str, Any]
    stopped_reason: str
    budget: Dict[str, Any]
    follow_up_queries: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ledger": self.ledger.to_dict(),
            "collectors": [c.to_dict() for c in self.collectors],
            "coverage": self.coverage,
            "stopped_reason": self.stopped_reason,
            "budget": self.budget,
            "follow_up_queries": self.follow_up_queries,
        }


class DeepProfileOrchestrator:
    """Run applicable collectors with a bounded, resumable research loop.

    ``worker_runner`` is an adapter for the existing worker queue.  The
    orchestrator does not import or reimplement that queue: a hosted app can
    inject its queue-backed callable, while tests use ordinary collectors.
    """

    def __init__(
        self,
        *,
        collectors: Optional[Mapping[str, Collector]] = None,
        capabilities: Optional[Iterable[str]] = None,
        fetch: Optional[Callable[[str], Any]] = None,
        search: Optional[Any] = None,
        llm: Optional[Any] = None,
        worker_runner: Optional[Callable[[Collector, CollectorContext], CollectorResult]] = None,
        verifier: Optional[Callable[..., Any]] = None,
        max_workers: int = 4,
    ) -> None:
        self.collectors = dict(collectors or discover())
        self.capabilities = set(capabilities or ("web", "search"))
        if llm is not None:
            self.capabilities.add("llm")
        if fetch is not None:
            self.capabilities.add("web")
        self.fetch, self.search, self.llm = fetch, search, llm
        self.worker_runner = worker_runner
        self.verifier = verifier
        self.max_workers = max(1, int(max_workers))
        self._save_lock = Lock()

    def plan(self, *, completed: Set[str] = None, names: Optional[Sequence[str]] = None) -> List[Collector]:
        """Return collectors whose requirements are currently available."""
        completed = completed or set()
        wanted = set(names) if names else None
        planned = []
        for name, coll in sorted(self.collectors.items()):
            if wanted is not None and name not in wanted:
                continue
            if name in completed or not set(coll.requires).issubset(self.capabilities):
                continue
            if "worker" in coll.requires and self.worker_runner is None:
                continue
            planned.append(coll)
        return planned

    def run(
        self,
        company: Dict[str, Any],
        *,
        ledger: Optional[Ledger] = None,
        run_id: Optional[str] = None,
        checkpoint_path: Optional[str] = None,
        budget: Optional[Budget] = None,
        collector_names: Optional[Sequence[str]] = None,
        db: Any = None,
        session_factory: Optional[Callable[[], Any]] = None,
    ) -> OrchestrationResult:
        run_id = run_id or uuid.uuid4().hex[:12]
        budget = budget or Budget()
        completed: Set[str] = set()
        if ledger is None and checkpoint_path:
            loaded = self._load_checkpoint(checkpoint_path)
            if loaded:
                ledger = Ledger.from_dict(loaded.get("ledger"))
                completed = set(loaded.get("completed", []))
        ledger = ledger or Ledger(company.get("id"))

        # The ledger is the resumable source of truth.  A collector that has a
        # completed run entry is not repeated unless the caller supplies a new
        # run id or explicitly uses a fresh ledger.
        completed.update(r.get("collector") for r in ledger.runs if r.get("run_id") == run_id and r.get("status") == "finished")
        collector_runs: List[CollectorRun] = []
        follow_ups: List[str] = []
        stopped_reason = "no eligible collectors"

        for round_no in range(max(1, int(budget.max_rounds))):
            if budget.expired:
                stopped_reason = "deadline"
                break
            planned = self.plan(completed=completed, names=collector_names)
            if not planned:
                stopped_reason = "coverage target reached" if self.coverage(ledger)["weighted"] >= budget.coverage_target else stopped_reason
                break
            remaining = max(0, int(budget.max_collectors) - len(completed))
            planned = planned[:remaining]
            if not planned:
                stopped_reason = "collector limit"
                break

            company_for_round = copy.deepcopy(company)
            if follow_ups:
                company_for_round["research_queries"] = list(follow_ups)
            futures = {}
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(planned))) as pool:
                for coll in planned:
                    futures[pool.submit(self._run_collector, coll, company_for_round, ledger, budget, run_id)] = coll
                for future in as_completed(futures):
                    coll = futures[future]
                    try:
                        run, result = future.result()
                    except Exception as exc:  # expected failures become visible, never kill the run
                        run, result = CollectorRun(coll.name, "error", errors=[f"{type(exc).__name__}: {exc}" ]), None
                    self._merge_result(coll, result, run, company_for_round, ledger, budget)
                    collector_runs.append(run)
                    completed.add(coll.name)
                    self._record_run(ledger, run_id, run, round_no)
                    self._persist(company_for_round, ledger, run=run, db=db, session_factory=session_factory)
                    self._write_checkpoint(checkpoint_path, run_id, ledger, completed, collector_runs, budget)

            cov = self.coverage(ledger)
            if cov["weighted"] >= float(budget.coverage_target):
                stopped_reason = "coverage target"
                break
            if round_no + 1 >= int(budget.max_rounds):
                stopped_reason = "round limit"
                break
            follow_ups = self._follow_up_queries(company_for_round, ledger, budget, run_id, cov)
            if not follow_ups:
                stopped_reason = "no new follow-up queries"
                break
        else:
            stopped_reason = "round limit"

        coverage = self.coverage(ledger)
        result = OrchestrationResult(run_id, ledger, collector_runs, coverage, stopped_reason,
                                     budget.snapshot(), follow_ups)
        if checkpoint_path:
            self._write_checkpoint(checkpoint_path, run_id, ledger, completed, collector_runs, budget,
                                   result=result)
        return result

    def _run_collector(self, coll: Collector, company: Dict[str, Any], ledger: Ledger,
                       run_budget: Budget, run_id: str):
        started = time.monotonic()
        local = Ledger.from_dict(ledger.to_dict())
        context_budget = CollectorBudget(
            max_pages=60,
            max_llm_tokens=max(1, min(60_000, run_budget.max_llm_tokens - run_budget.llm_tokens_used)),
            max_search_queries=max(1, min(12, 100 - run_budget.search_queries_used)),
            deadline_seconds=run_budget.collector_deadline(),
        )
        ctx = CollectorContext(company=company, ledger=local, budget=context_budget, llm=self.llm,
                               search=self.search, fetch=self.fetch,
                               log=lambda _msg: None)
        # Python collectors do not need a scarce browser slot.  Worker-backed
        # steps do: the injected adapter owns the actual queue/target choice,
        # while this slot prevents an orchestrator from flooding one process
        # pool when an adapter is local (as it is in tests).
        try:
            import resource_governor
            resources = ("process",) if "worker" in coll.requires else ()
            slot = resource_governor.get_governor().slot("intel-collector", resources=resources)
        except Exception:
            slot = nullcontext()
        with slot:
            if "worker" in coll.requires:
                result = self.worker_runner(coll, ctx) if self.worker_runner else CollectorResult(errors=["worker unavailable"])
            else:
                result = coll.run(ctx)
        run = CollectorRun(coll.name, "finished", pages=result.pages_fetched, queries=list(result.queries),
                           errors=list(result.errors), seconds=round(time.monotonic() - started, 3))
        return run, result

    def _merge_result(self, coll: Collector, result: Optional[CollectorResult], run: CollectorRun,
                      company: Dict[str, Any], ledger: Ledger, budget: Budget) -> None:
        if result is None:
            run.status = "error"
            return
        run.absent = list(result.checked_absent)
        run.pages = result.pages_fetched
        budget.pages_fetched += result.pages_fetched
        budget.search_queries_used += len(result.queries)
        refused = []
        for fact in result.facts:
            if not coll.may_emit(fact.key):
                refused.append(f"{fact.key}: outside declared prefixes")
                continue
            verdict = self._verify_fact(fact, company, ledger)
            run.verification[verdict] = run.verification.get(verdict, 0) + 1
            refused.extend(ledger.add_many([fact]))
        run.refused = refused
        run.facts = len(result.facts) - len(refused)
        try:
            budget.check()
        except RunBudgetExceeded:
            run.errors.append("run budget exhausted after collector")

    def _verify_fact(self, fact: Fact, company: Dict[str, Any], ledger: Ledger) -> str:
        if self.verifier is None:
            from intel.verify import verify
            verifier = verify
        else:
            verifier = self.verifier
        page_texts = []
        for ev in fact.evidence:
            if ev.url and self.fetch:
                try:
                    page = self.fetch(ev.url)
                    page_texts.append(getattr(page, "text", "") or "")
                except Exception:
                    pass
        page_text = "\n".join(page_texts) or "\n".join(e.quote or "" for e in fact.evidence)
        try:
            verdict = verifier(fact, page_text, company, ledger, self.llm)
            return getattr(verdict, "decision", "unverified")
        except TypeError:
            verdict = verifier(fact, page_text, company, ledger)
            return getattr(verdict, "decision", "unverified")
        except Exception as exc:
            fact.verified = False
            fact.notes = f"verification error: {type(exc).__name__}: {exc}"
            return "unverified"

    def coverage(self, ledger: Ledger) -> Dict[str, Any]:
        known = set(f.category for f in ledger.facts.values())
        absent = set()
        for run in ledger.runs:
            for key in run.get("checked_absent", []):
                if "." in key:
                    absent.add(key.split(".", 1)[0])
        covered = known | absent
        weights = {category: 1.0 for category in CHECKLIST_CATEGORIES}
        weighted = sum(weights[c] for c in covered if c in weights) / sum(weights.values())
        return {
            "known": sorted(known),
            "absent": sorted(absent - known),
            "unknown": sorted(set(CHECKLIST_CATEGORIES) - covered),
            "covered": sorted(covered),
            "weighted": round(weighted, 4),
        }

    def _follow_up_queries(self, company: Dict[str, Any], ledger: Ledger, budget: Budget,
                           run_id: str, coverage: Dict[str, Any]) -> List[str]:
        if self.llm is None or not coverage["unknown"]:
            return []
        try:
            from intel.llm_pool import TASK_TRIAGE
            response = self.llm.complete(
                task=TASK_TRIAGE,
                system="Return only JSON with a queries array. Create short public-web research queries; never request private data.",
                user=json.dumps({"company": company, "unknown_categories": coverage["unknown"],
                                 "known_categories": coverage["known"]}, ensure_ascii=False),
                schema={"queries": ["string"]}, budget=budget,
                company_id=company.get("id"), run_id=run_id,
            )
            data = getattr(response, "data", None) or {}
            queries = data.get("queries", []) if isinstance(data, dict) else []
            budget.llm_tokens_used = self._llm_tokens_used(response, budget.llm_tokens_used)
            return [str(q).strip() for q in queries if str(q).strip()][:8]
        except Exception:
            return []

    @staticmethod
    def _llm_tokens_used(response: Any, current: int) -> int:
        usage = getattr(response, "usage", {}) or {}
        return current + int(usage.get("total_tokens", 0) or 0)

    def _record_run(self, ledger: Ledger, run_id: str, run: CollectorRun, round_no: int) -> None:
        ledger.runs.append({"run_id": run_id, "collector": run.name, "status": run.status,
                            "round": round_no, "finished_at": time.time(), "pages": run.pages,
                            "queries": run.queries, "errors": run.errors, "checked_absent": run.absent,
                            "verification": run.verification, "seconds": run.seconds})

    def _persist(self, company: Dict[str, Any], ledger: Ledger, *, run: CollectorRun,
                 db: Any, session_factory: Any) -> None:
        if db is None and session_factory is None:
            return
        session = db or session_factory()
        try:
            save_ledger(session, company.get("id"), ledger)
            self._save_source_run(session, company.get("id"), run)
            if hasattr(session, "commit"):
                session.commit()
        finally:
            if db is None and hasattr(session, "close"):
                session.close()

    @staticmethod
    def _save_source_run(session: Any, company_id: Any, run: CollectorRun) -> None:
        """Mirror collector status into the existing Pipeline Health table."""
        if not company_id:
            return
        try:
            from models import CompanySourceRun
            row = session.query(CompanySourceRun).filter_by(
                company_id=company_id, source_name=f"intel:{run.name}").first()
            if row is None:
                row = CompanySourceRun(company_id=company_id, source_name=f"intel:{run.name}", phase=7)
                session.add(row)
            row.status = "live" if run.status == "finished" and not run.errors else ("error" if run.errors else "skipped")
            row.error_message = "; ".join(run.errors)[:2000] if run.errors else None
        except (ImportError, AttributeError, TypeError):
            # The orchestration layer is also used in fixture-only runs and
            # should not require SQLAlchemy just to execute a collector.
            return

    def _write_checkpoint(self, path: Optional[str], run_id: str, ledger: Ledger, completed: Set[str],
                          runs: List[CollectorRun], budget: Budget, result: Optional[OrchestrationResult] = None) -> None:
        if not path:
            return
        payload = result.to_dict() if result else {"run_id": run_id, "ledger": ledger.to_dict(),
                                                    "completed": sorted(completed),
                                                    "collectors": [r.to_dict() for r in runs],
                                                    "budget": budget.snapshot()}
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(target)

    @staticmethod
    def _load_checkpoint(path: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None


def run_deep_profile(company: Dict[str, Any], **kwargs: Any) -> OrchestrationResult:
    """Convenience entry point used by callers that do not need a custom object."""
    return DeepProfileOrchestrator(**{k: kwargs.pop(k) for k in list(kwargs)
                                      if k in {"collectors", "capabilities", "fetch", "search", "llm",
                                               "worker_runner", "verifier", "max_workers"}}).run(company, **kwargs)
