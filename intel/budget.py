"""Run-level budgets for the deep-intelligence orchestrator.

The collector ``Budget`` in :mod:`intel.collector` limits one collector.  This
module limits the complete research run, so a follow-up loop cannot quietly
consume an unbounded amount of time or LLM usage.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class RunBudgetExceeded(RuntimeError):
    """The run reached one of its hard limits."""


@dataclass
class Budget:
    """Hard limits for one company's complete deep-profile run."""

    max_llm_tokens: int = 250_000
    deadline_seconds: float = 15 * 60
    max_rounds: int = 3
    coverage_target: float = 0.70
    max_collectors: int = 40
    started_monotonic: float = field(default_factory=time.monotonic)
    llm_tokens_used: int = 0
    search_queries_used: int = 0
    pages_fetched: int = 0

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_monotonic)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, float(self.deadline_seconds) - self.elapsed_seconds)

    @property
    def expired(self) -> bool:
        return self.elapsed_seconds >= float(self.deadline_seconds)

    def check(self) -> None:
        if self.expired:
            raise RunBudgetExceeded("run wall-clock budget exhausted")
        if self.llm_tokens_used >= int(self.max_llm_tokens):
            raise RunBudgetExceeded("run LLM token budget exhausted")

    def charge(self, *, llm_tokens: int = 0, queries: int = 0, pages: int = 0) -> None:
        self.llm_tokens_used += max(0, int(llm_tokens or 0))
        self.search_queries_used += max(0, int(queries or 0))
        self.pages_fetched += max(0, int(pages or 0))
        self.check()

    def collector_deadline(self, default: int = 240) -> int:
        """Give a collector a bounded slice of the remaining run time."""
        return max(1, min(int(default), int(self.remaining_seconds or 1)))

    def snapshot(self) -> Dict[str, Any]:
        return {
            "max_llm_tokens": self.max_llm_tokens,
            "deadline_seconds": self.deadline_seconds,
            "max_rounds": self.max_rounds,
            "coverage_target": self.coverage_target,
            "max_collectors": self.max_collectors,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "remaining_seconds": round(self.remaining_seconds, 3),
            "llm_tokens_used": self.llm_tokens_used,
            "search_queries_used": self.search_queries_used,
            "pages_fetched": self.pages_fetched,
        }


def budget_from_dict(data: Optional[Dict[str, Any]]) -> Budget:
    """Restore a budget configuration without restoring its elapsed clock."""
    data = data or {}
    allowed = {"max_llm_tokens", "deadline_seconds", "max_rounds", "coverage_target", "max_collectors"}
    return Budget(**{k: data[k] for k in allowed if k in data})
