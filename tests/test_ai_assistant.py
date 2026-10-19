"""
Tests for the AI Assistant's read-only query helpers (company_service.py) and its
tool-calling loop (assistant_llm.py). No network calls: requests.post is monkeypatched.
"""

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import assistant_llm
import company_service
import config
from indicators import seed_indicator_definitions
from models import Base, Company, SignalRecord
from painpoints import seed_pain_point_definitions

NOW = datetime.utcnow()


def _patch_settings(monkeypatch, api_key=None, base_url=None, model=None,
                     fallback_api_key=None, fallback_base_url=None, fallback_model=None):
    """assistant_llm.py reads provider settings via config.assistant_llm_settings(), called
    fresh on every turn (not frozen module constants — see that function's docstring), so
    controlling what it sees means monkeypatching this function, not attributes that no
    longer exist on assistant_llm at all."""
    monkeypatch.setattr(config, "assistant_llm_settings", lambda: {
        "api_key": api_key, "base_url": base_url, "model": model,
        "fallback_api_key": fallback_api_key, "fallback_base_url": fallback_base_url, "fallback_model": fallback_model,
    })


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    seed_indicator_definitions(session)
    seed_pain_point_definitions(session)
    yield session
    session.close()


def make_company(db, legal_name, registration_number, nace_code="282999"):
    c = Company(legal_name=legal_name, registration_number=registration_number, nace_code=nace_code, country="Italy")
    db.add(c)
    db.commit()
    return c


def set_signal(db, company, signal_key, value, status="present", source="Test", simulated=False):
    sig = SignalRecord(company_id=company.id, signal_key=signal_key, numeric_value=value,
                       status=status, source=source, is_simulated=simulated, confidence=1.0, fetched_at=NOW)
    db.add(sig)
    db.commit()
    return sig


# ---- company_service.py query helpers -----------------------------------------------------------

def test_search_companies_matches_name_and_vat_case_insensitively(db):
    make_company(db, "Acme Macchine S.R.L.", "IT00000000001")
    make_company(db, "Other Company", "IT00000000002")

    assert [c.legal_name for c in company_service.search_companies(db, "acme")] == ["Acme Macchine S.R.L."]
    assert [c.legal_name for c in company_service.search_companies(db, "IT00000000002")] == ["Other Company"]
    assert company_service.search_companies(db, "nonexistent") == []
    assert company_service.search_companies(db, "") == []


def test_top_companies_by_signal_sorts_and_excludes_non_present(db):
    a = make_company(db, "High Cost", "IT00000000010")
    b = make_company(db, "Low Cost", "IT00000000011")
    c = make_company(db, "No Data", "IT00000000012")
    set_signal(db, a, "materials_cost", 80.0)
    set_signal(db, b, "materials_cost", 20.0)
    set_signal(db, c, "materials_cost", None, status="not_yet_checked")

    top = company_service.top_companies_by_signal(db, "materials_cost", n=10, ascending=False)
    assert [r["legal_name"] for r in top] == ["High Cost", "Low Cost"]
    assert top[0]["value"] == 80.0
    assert top[0]["source"] == "Test" and top[0]["is_simulated"] is False

    bottom = company_service.top_companies_by_signal(db, "materials_cost", n=10, ascending=True)
    assert [r["legal_name"] for r in bottom] == ["Low Cost", "High Cost"]

    assert company_service.top_companies_by_signal(db, "materials_cost", n=1) == top[:1]
    assert company_service.top_companies_by_signal(db, "no_such_signal_key") == []


def test_get_company_profile_includes_scores_and_signal_detail(db):
    company = make_company(db, "Profile Co", "IT00000000020")
    set_signal(db, company, "materials_cost", 55.0)

    profile = company_service.get_company_profile(db, company)
    assert profile["legal_name"] == "Profile Co"
    assert profile["registration_number"] == "IT00000000020"
    assert isinstance(profile["need_score"], float)
    assert isinstance(profile["readiness_score"], float)
    assert any(d["key"] == "materials_cost" for d in profile["need_signals"])


def test_get_company_pain_points_detects_a_fully_evidenced_severe_pain_point(db):
    company = make_company(db, "Stressed Co", "IT00000000030")
    # Every driver of input_cost_pressure pushed well past its "severe" threshold —
    # full coverage, unambiguous score, no risk of a thin-coverage cap hiding it.
    for key, severe_value in (
        ("energy_cost", 30.0), ("materials_cost", 90.0), ("raw_material_cost", 90.0),
        ("cogs_ratio", 95.0), ("service_costs", 40.0),
        ("materials_cost_ratio_trend", 20.0), ("materials_cost_ratio_peer_gap", 30.0),
    ):
        set_signal(db, company, key, severe_value)

    detected = company_service.get_company_pain_points(db, company)
    keys = [d["key"] for d in detected]
    assert "input_cost_pressure" in keys
    hit = next(d for d in detected if d["key"] == "input_cost_pressure")
    assert hit["status"] == "severe"
    assert hit["coverage"] == pytest.approx(1.0)


def test_get_company_pain_points_is_empty_with_no_signals(db):
    company = make_company(db, "Blank Co", "IT00000000040")
    assert company_service.get_company_pain_points(db, company) == []


# ---- assistant_llm.py tool dispatch ---------------------------------------------------------------

def test_resolve_company_reports_ambiguous_and_missing_rather_than_guessing(db):
    make_company(db, "Dual Match One", "IT00000000050")
    make_company(db, "Dual Match Two", "IT00000000051")

    company, error = assistant_llm._resolve_company(db, "Dual Match")
    assert company is None and "2 companies match" in error["error"]

    company, error = assistant_llm._resolve_company(db, "Nothing Like This")
    assert company is None and "No company found" in error["error"]


def test_execute_tool_dispatches_each_known_tool(db):
    company = make_company(db, "Dispatch Co", "IT00000000060")
    set_signal(db, company, "materials_cost", 70.0)

    assert assistant_llm._execute_tool(db, "search_companies", {"query": "Dispatch"})["matches"][0]["legal_name"] == "Dispatch Co"
    top = assistant_llm._execute_tool(db, "top_companies_by_signal", {"signal_key": "materials_cost"})
    assert top["results"][0]["legal_name"] == "Dispatch Co"
    assert top["unit"] == "% of revenue"
    signals = assistant_llm._execute_tool(db, "list_available_signals", {})
    assert any(s["key"] == "materials_cost" for s in signals["signals"])
    profile = assistant_llm._execute_tool(db, "get_company_profile", {"company_name": "Dispatch Co"})
    assert profile["legal_name"] == "Dispatch Co"
    pain = assistant_llm._execute_tool(db, "get_company_pain_points", {"company_name": "Dispatch Co"})
    assert "pain_points" in pain
    assert assistant_llm._execute_tool(db, "not_a_real_tool", {}) == {"error": "unknown tool 'not_a_real_tool'"}


# ---- assistant_llm.run_assistant_turn's multi-round loop (requests.post mocked) -------------------

class _FakeResponse:
    def __init__(self, message=None, status_code=200):
        self._message = message
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as _requests
            raise _requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return {"choices": [{"message": self._message}]}


def _assistant_message(content=None, tool_calls=None):
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


def test_is_configured_reflects_the_three_settings(db, monkeypatch):
    _patch_settings(monkeypatch)
    assert assistant_llm.is_configured() is False
    _patch_settings(monkeypatch, api_key="key", base_url="https://example.test/v1", model="some-model")
    assert assistant_llm.is_configured() is True


def test_run_assistant_turn_executes_a_tool_call_then_returns_the_final_answer(db, monkeypatch):
    _patch_settings(monkeypatch, api_key="key", base_url="https://example.test/v1", model="some-model")

    company = make_company(db, "Loop Co", "IT00000000070")
    set_signal(db, company, "materials_cost", 61.0)

    responses = [
        _FakeResponse(_assistant_message(tool_calls=[
            {"id": "call_1", "function": {"name": "top_companies_by_signal",
                                          "arguments": json.dumps({"signal_key": "materials_cost", "n": 5})}},
        ])),
        _FakeResponse(_assistant_message(content="Pirelli has the highest materials cost at €2,028,300 thousand.")),
    ]
    calls = []

    def fake_post(url, headers, json, timeout):  # noqa: A002 — matches requests.post's own kwarg name
        calls.append(json)
        return responses[len(calls) - 1]

    monkeypatch.setattr(assistant_llm.requests, "post", fake_post)

    result = assistant_llm.run_assistant_turn([{"role": "user", "content": "highest materials cost?"}], db)

    assert "Loop Co — 61.00%" in result["content"]
    assert "percentages of revenue" in result["content"]
    assert "Pirelli" not in result["content"]
    assert result["used_fallback"] is False
    assert len(result["tool_trace"]) == 1
    assert result["tool_trace"][0]["tool"] == "top_companies_by_signal"
    assert result["tool_trace"][0]["result"]["results"][0]["legal_name"] == "Loop Co"
    assert len(calls) == 2
    # the second request must carry the tool's result back as a role="tool" message
    second_request_roles = [m["role"] for m in calls[1]["messages"]]
    assert second_request_roles.count("tool") == 1


def test_ranking_answer_without_company_values_does_not_repeat_model_invention(db, monkeypatch):
    _patch_settings(monkeypatch, api_key="key", base_url="https://example.test/v1", model="some-model")
    monkeypatch.setattr(assistant_llm.requests, "post", lambda *args, **kwargs: _FakeResponse(
        _assistant_message(content="Pirelli is first with €2,028,300 thousand of materials costs.")))

    result = assistant_llm.run_assistant_turn(
        [{"role": "user", "content": "Which companies have the highest materials costs?"}], db)

    assert "couldn't verify a ranking" in result["content"]
    assert "Pirelli" not in result["content"]
    assert result["tool_trace"] == []


def test_service_cost_ranking_labels_ratio_and_flags_extreme_value(db):
    company = make_company(db, "E2PACK S.R.L.", "IT00000000071")
    set_signal(db, company, "service_costs", 358.17, source="AIDA Cost Breakdown Supplement")
    result = assistant_llm._execute_tool(db, "top_companies_by_signal", {"signal_key": "service_costs"})

    answer = assistant_llm._verified_ranking_answer([{
        "tool": "top_companies_by_signal", "arguments": {"signal_key": "service_costs"}, "result": result,
    }])

    assert "E2PACK S.R.L. — 358.17%" in answer
    assert "percentages of revenue, not amounts in thousands of euros" in answer
    assert "above 100%" in answer


def test_run_assistant_turn_stops_at_max_rounds_instead_of_looping_forever(db, monkeypatch):
    _patch_settings(monkeypatch, api_key="key", base_url="https://example.test/v1", model="some-model")

    call_count = {"n": 0}

    def fake_post(url, headers, json, timeout):
        call_count["n"] += 1
        return _FakeResponse(_assistant_message(tool_calls=[
            {"id": f"call_{call_count['n']}", "function": {"name": "list_available_signals", "arguments": "{}"}},
        ]))

    monkeypatch.setattr(assistant_llm.requests, "post", fake_post)

    result = assistant_llm.run_assistant_turn([{"role": "user", "content": "..."}], db, max_rounds=3)

    assert call_count["n"] == 3
    assert "couldn't finish" in result["content"]
    assert len(result["tool_trace"]) == 3


def test_run_assistant_turn_raises_when_not_configured(db, monkeypatch):
    _patch_settings(monkeypatch)
    with pytest.raises(RuntimeError):
        assistant_llm.run_assistant_turn([{"role": "user", "content": "hi"}], db)


# ---- primary -> fallback provider switching --------------------------------------------------

def _configure_primary_and_fallback(monkeypatch):
    _patch_settings(
        monkeypatch, api_key="primary-key", base_url="https://primary.test/v1", model="primary-model",
        fallback_api_key="fallback-key", fallback_base_url="https://fallback.test/v1", fallback_model="fallback-model",
    )


def test_run_assistant_turn_falls_back_when_primary_returns_429(db, monkeypatch):
    _configure_primary_and_fallback(monkeypatch)
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(url)
        if url.startswith("https://primary.test"):
            return _FakeResponse(status_code=429)
        return _FakeResponse(_assistant_message(content="Answered by the fallback."))

    monkeypatch.setattr(assistant_llm.requests, "post", fake_post)

    result = assistant_llm.run_assistant_turn([{"role": "user", "content": "hi"}], db)

    assert result["content"] == "Answered by the fallback."
    assert result["used_fallback"] is True
    assert calls == ["https://primary.test/v1/chat/completions", "https://fallback.test/v1/chat/completions"]


def test_run_assistant_turn_falls_back_on_a_connection_error(db, monkeypatch):
    import requests as requests_module
    _configure_primary_and_fallback(monkeypatch)

    def fake_post(url, headers, json, timeout):
        if url.startswith("https://primary.test"):
            raise requests_module.ConnectionError("primary is down")
        return _FakeResponse(_assistant_message(content="Answered by the fallback."))

    monkeypatch.setattr(assistant_llm.requests, "post", fake_post)

    result = assistant_llm.run_assistant_turn([{"role": "user", "content": "hi"}], db)
    assert result["content"] == "Answered by the fallback." and result["used_fallback"] is True


def test_run_assistant_turn_stays_on_the_fallback_for_the_rest_of_the_turn(db, monkeypatch):
    """Once switched, round 2 goes straight to the fallback — the known-bad primary is not
    re-tried every round."""
    _configure_primary_and_fallback(monkeypatch)
    primary_calls, fallback_calls = [], []

    def fake_post(url, headers, json, timeout):
        if url.startswith("https://primary.test"):
            primary_calls.append(1)
            return _FakeResponse(status_code=429)
        fallback_calls.append(1)
        if len(fallback_calls) == 1:
            return _FakeResponse(_assistant_message(tool_calls=[
                {"id": "call_1", "function": {"name": "list_available_signals", "arguments": "{}"}},
            ]))
        return _FakeResponse(_assistant_message(content="Done."))

    monkeypatch.setattr(assistant_llm.requests, "post", fake_post)

    result = assistant_llm.run_assistant_turn([{"role": "user", "content": "hi"}], db)

    assert result["content"] == "Done." and result["used_fallback"] is True
    assert len(primary_calls) == 1   # tried once, then never again this turn
    assert len(fallback_calls) == 2  # the tool-call round, then the final-answer round


def test_run_assistant_turn_does_not_fall_back_on_a_non_retryable_error(db, monkeypatch):
    _configure_primary_and_fallback(monkeypatch)
    fallback_called = []

    def fake_post(url, headers, json, timeout):
        if url.startswith("https://primary.test"):
            return _FakeResponse(status_code=400)
        fallback_called.append(1)
        return _FakeResponse(_assistant_message(content="should not be reached"))

    monkeypatch.setattr(assistant_llm.requests, "post", fake_post)

    with pytest.raises(Exception):
        assistant_llm.run_assistant_turn([{"role": "user", "content": "hi"}], db)
    assert not fallback_called
