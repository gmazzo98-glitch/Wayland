"""
Tests for intel/llm_pool.py (ticket I04).
Covers:
- Parity test: scrapers/*.py and config.py forwarded vars vs worker/worker.mjs ENV_ALLOWLIST
- Task routing (triage, extract, synthesize, vision)
- 429 failover and retry-after cool-down
- All providers cooling down
- Hard token budget stop (Budget.max_llm_tokens)
- JSON normalisation, regex repair, and one-retry LLM repair
- Usage ledger tracking per company and run
"""

import ast
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from intel.collector import Budget
from intel.llm_pool import (
    ALL_TASKS,
    TASK_EXTRACT,
    TASK_SYNTHESIZE,
    TASK_TRIAGE,
    TASK_VISION,
    AllProvidersCoolingDownError,
    AllProvidersFailedError,
    BudgetExceededError,
    JSONParseError,
    LLMPool,
    LLMPoolError,
    LLMUsageLedger,
    NoProvidersConfiguredError,
    Provider,
    ValidationError,
    complete,
    normalize_and_repair_json,
    parse_retry_after,
)


def _load_worker_env_allowlist() -> set:
    worker_mjs = Path("worker/worker.mjs").read_text(encoding="utf-8")
    # Match the ENV_ALLOWLIST set content
    m = re.search(r"const ENV_ALLOWLIST = new Set\(\[\s*([\s\S]*?)\s*\]\);", worker_mjs)
    assert m, "Could not find ENV_ALLOWLIST in worker/worker.mjs"
    body = m.group(1)

    allowlist = set()
    # Find all string literals 'FOO' or "FOO"
    for s in re.finditer(r"['\"]([A-Za-z0-9_]+)['\"]", body):
        allowlist.add(s.group(1))

    # Also check if template strings or dynamic generators like INTEL_LLM_<1..10> are evaluated
    if "INTEL_LLM_${n}" in body or "INTEL_LLM_" in body:
        for i in range(1, 11):
            for suffix in ("NAME", "API_KEY", "BASE_URL", "MODEL", "TIER", "TASKS", "TPM", "RPM"):
                allowlist.add(f"INTEL_LLM_{i}_{suffix}")

    return allowlist


def test_env_allowlist_parity():
    """Requirement (3): A parity test: every environment variable name that
    scrapers/*.py or config.py forwards to a crawler must appear in the allowlist."""
    allowlist = _load_worker_env_allowlist()

    # Collect all env keys forwarded by scrapers/*.py
    scrapers_dir = Path("scrapers")
    forwarded_keys = set()

    class ScraperEnvVisitor(ast.NodeVisitor):
        def visit_Assign(self, node):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "env"
                ):
                    if isinstance(target.slice, ast.Constant) and isinstance(target.slice.value, str):
                        forwarded_keys.add(target.slice.value)
                    elif isinstance(target.slice, ast.JoinedStr):
                        # Handle f-strings like f"LLM_FALLBACK{extra['suffix']}_API_KEY"
                        for s in ("2", "3", "4", "5"):
                            expanded = []
                            for part in target.slice.values:
                                if isinstance(part, ast.Constant):
                                    expanded.append(part.value)
                                elif isinstance(part, ast.FormattedValue):
                                    expanded.append(s)
                            forwarded_keys.add("".join(expanded))
            self.generic_visit(node)

        def visit_Dict(self, node):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    if k.value.isupper() and any(
                        sub in k.value
                        for sub in ("LLM", "API", "MAX", "KEY", "URL", "DELAY", "PROVIDER", "DEADLINE", "STORAGE")
                    ):
                        forwarded_keys.add(k.value)
            self.generic_visit(node)

        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "update":
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "env":
                    for arg in node.args:
                        if isinstance(arg, ast.Dict):
                            for k in arg.keys:
                                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                                    forwarded_keys.add(k.value)
            self.generic_visit(node)

    for py_file in scrapers_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        ScraperEnvVisitor().visit(tree)

    # CRAWLEE_STORAGE_DIR is set internally by worker.mjs in the temp folder, not via task env
    forwarded_keys.discard("CRAWLEE_STORAGE_DIR")

    # Assert every forwarded key is allowlisted
    missing = forwarded_keys - allowlist
    assert not missing, f"Environment variables forwarded to crawlers missing from ENV_ALLOWLIST: {sorted(missing)}"

    # Check numbered fallbacks from config.py
    for s in ("2", "3", "4", "5"):
        assert f"LLM_FALLBACK{s}_API_KEY" in allowlist
        assert f"LLM_FALLBACK{s}_BASE_URL" in allowlist
        assert f"LLM_FALLBACK{s}_MODEL" in allowlist
        assert f"VISION_LLM_FALLBACK{s}_API_KEY" in allowlist
        assert f"VISION_LLM_FALLBACK{s}_BASE_URL" in allowlist
        assert f"VISION_LLM_FALLBACK{s}_MODEL" in allowlist

    # Check INTEL_LLM_1..10 in allowlist
    for i in range(1, 11):
        for suffix in ("NAME", "API_KEY", "BASE_URL", "MODEL", "TIER", "TASKS", "TPM", "RPM"):
            assert f"INTEL_LLM_{i}_{suffix}" in allowlist


def test_task_routing():
    p_triage = Provider(name="cheap_triage", api_key="k1", tier="free", tasks=[TASK_TRIAGE])
    p_extract = Provider(name="mid_extract", api_key="k2", tier="standard", tasks=[TASK_EXTRACT])
    p_synth = Provider(name="high_synth", api_key="k3", tier="premium", tasks=[TASK_SYNTHESIZE])
    p_vision = Provider(name="vis_model", api_key="k4", tier="free", tasks=[TASK_VISION])

    pool = LLMPool(providers=[p_triage, p_extract, p_synth, p_vision])

    assert pool.route(TASK_TRIAGE) == [p_triage]
    assert pool.route(TASK_EXTRACT) == [p_extract]
    assert pool.route(TASK_SYNTHESIZE) == [p_synth]
    assert pool.route(TASK_VISION) == [p_vision]

    with pytest.raises(NoProvidersConfiguredError):
        pool.route("unknown_task")


def test_429_failover_and_cooldown():
    call_log = []

    def fake_http(provider, system, user, schema, images):
        call_log.append(provider.name)
        if provider.name == "p1":
            raise LLMPoolError("HTTP 429 Too Many Requests (Retry-After: 45)")
        return "OK from p2", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    p1 = Provider(name="p1", api_key="k1", tier="free", tasks=[TASK_TRIAGE])
    p2 = Provider(name="p2", api_key="k2", tier="free", tasks=[TASK_TRIAGE])

    pool = LLMPool(providers=[p1, p2], http_client=fake_http)

    res = pool.complete(task=TASK_TRIAGE, system="sys", user="usr")
    assert res.text == "OK from p2"
    assert res.provider == "p2"
    assert call_log == ["p1", "p2"]
    assert p1.is_cooling_down()
    assert not p2.is_cooling_down()

    # Second call should skip p1 directly because p1 is cooling down
    call_log.clear()
    res2 = pool.complete(task=TASK_TRIAGE, system="sys", user="usr")
    assert res2.provider == "p2"
    assert call_log == ["p2"]


def test_all_providers_cooling_down():
    now = time.time()
    p1 = Provider(name="p1", api_key="k1", tasks=[TASK_TRIAGE], cool_down_until=now + 30.0)
    p2 = Provider(name="p2", api_key="k2", tasks=[TASK_TRIAGE], cool_down_until=now + 60.0)

    pool = LLMPool(providers=[p1, p2])
    with pytest.raises(AllProvidersCoolingDownError) as exc_info:
        pool.complete(task=TASK_TRIAGE, system="sys", user="usr")
    assert "shortest cooldown" in str(exc_info.value)


def test_hard_token_budget_stop():
    def fake_http(provider, system, user, schema, images):
        return "response", {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300}

    p1 = Provider(name="p1", api_key="k1", tasks=[TASK_TRIAGE])
    pool = LLMPool(providers=[p1], http_client=fake_http)
    budget = Budget(max_llm_tokens=500)

    # First call: uses 300 tokens (<= 500)
    res1 = pool.complete(task=TASK_TRIAGE, system="s", user="u", budget=budget, run_id="run_1")
    assert res1.usage["total_tokens"] == 300

    # Second call: already at 300 tokens, call adds 300 -> total 600
    res2 = pool.complete(task=TASK_TRIAGE, system="s", user="u", budget=budget, run_id="run_1")
    assert res2.usage["total_tokens"] == 300
    assert pool.ledger.get_tokens(run_id="run_1") == 600

    # Third call: 600 >= 500 -> stopped BEFORE making HTTP call
    with pytest.raises(BudgetExceededError) as exc_info:
        pool.complete(task=TASK_TRIAGE, system="s", user="u", budget=budget, run_id="run_1")
    assert "Hard token budget exceeded" in str(exc_info.value)


def test_json_normalisation_and_repair():
    # 1. Clean JSON
    assert normalize_and_repair_json('{"name": "Asco"}') == {"name": "Asco"}

    # 2. Markdown fence
    raw_fence = '```json\n{"name": "Asco", "founded": 2002}\n```'
    assert normalize_and_repair_json(raw_fence) == {"name": "Asco", "founded": 2002}

    # 3. Leading and trailing chatter
    raw_chatter = 'Here is the data you requested:\n{"vat": "02688370135"}\nHope this helps!'
    assert normalize_and_repair_json(raw_chatter) == {"vat": "02688370135"}

    # 4. Trailing commas
    raw_trailing = '{"items": [1, 2, 3,], "active": true,}'
    assert normalize_and_repair_json(raw_trailing) == {"items": [1, 2, 3], "active": True}

    # 5. Python booleans / None
    raw_py = '{"active": True, "notes": None, "inactive": False}'
    assert normalize_and_repair_json(raw_py) == {"active": True, "notes": None, "inactive": False}


def test_json_repair_one_retry():
    calls = []

    def fake_http(provider, system, user, schema, images):
        calls.append(user)
        if len(calls) == 1:
            # First call returns broken unparseable text
            return "This is not json at all {{{ broken", {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
        # Second call (retry) returns valid json
        return '{"recovered": true}', {"prompt_tokens": 15, "completion_tokens": 5, "total_tokens": 20}

    p = Provider(name="p1", api_key="k1", tasks=[TASK_EXTRACT])
    pool = LLMPool(providers=[p], http_client=fake_http)

    res = pool.complete(task=TASK_EXTRACT, system="sys", user="extract facts", schema={"type": "object"})
    assert res.data == {"recovered": True}
    assert len(calls) == 2
    assert "previous JSON output failed" in calls[1]


def test_json_repair_fails_after_retry():
    def fake_http(provider, system, user, schema, images):
        return "Still completely garbage {{{", {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}

    p = Provider(name="p1", api_key="k1", tasks=[TASK_EXTRACT])
    pool = LLMPool(providers=[p], http_client=fake_http)

    with pytest.raises(JSONParseError) as exc_info:
        pool.complete(task=TASK_EXTRACT, system="sys", user="extract facts", schema={"type": "object"})
    assert "JSON repair retry failed" in str(exc_info.value)


def test_usage_ledger():
    ledger = LLMUsageLedger()
    ledger.record(
        company_id="c1",
        run_id="r1",
        provider="groq",
        model="gpt-oss-120b",
        task=TASK_EXTRACT,
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        latency_s=0.5,
        success=True,
    )
    ledger.record(
        company_id="c1",
        run_id="r1",
        provider="groq",
        model="gpt-oss-120b",
        task=TASK_TRIAGE,
        prompt_tokens=40,
        completion_tokens=10,
        total_tokens=50,
        latency_s=0.2,
        success=True,
    )
    ledger.record(
        company_id="c2",
        run_id="r2",
        provider="gemini",
        model="gemini-2.0-flash",
        task=TASK_SYNTHESIZE,
        prompt_tokens=200,
        completion_tokens=100,
        total_tokens=300,
        latency_s=0.8,
        success=True,
    )
    ledger.record(
        company_id="c1",
        run_id="r1",
        provider="groq",
        model="gpt-oss-120b",
        task=TASK_EXTRACT,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        latency_s=0.1,
        success=False,
        error="429",
    )

    assert ledger.get_tokens(company_id="c1") == 200
    assert ledger.get_tokens(run_id="r2") == 300
    assert ledger.get_tokens() == 500

    summary = ledger.get_summary()
    assert summary["total_calls"] == 4
    assert summary["failed_calls"] == 1
    assert summary["total_tokens"] == 500
    assert summary["by_provider"]["groq"]["tokens"] == 200
    assert summary["by_provider"]["gemini"]["tokens"] == 300
    assert summary["by_task"][TASK_EXTRACT]["tokens"] == 150


def test_parse_retry_after():
    assert parse_retry_after("30") == 30.0
    assert parse_retry_after("12.5s") == 12.5
    assert parse_retry_after("250ms") == 0.25
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
