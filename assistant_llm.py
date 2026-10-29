"""
The AI Assistant page's (views/ai_assistant.py) tool-calling loop — read-only Q&A over the
company database via any OpenAI-compatible provider (ASSISTANT_LLM_* in config.py; Mistral
and Gemini are both genuinely OpenAI-compatible, so this reuses the exact request shape
company_brief.py already posts to Groq, just with a `tools` array added).

Nothing here writes to the database. TOOL_DISPATCH names the only five functions the model
can ever call, each a thin wrapper around an already-trusted company_service.py query helper
— no eval, no raw SQL, nothing reachable beyond that list. This is the first multi-turn
tool-calling loop in this codebase's Python side (the TypeScript crawlers' llm.ts files use
a single tool_choice-pinned call for extraction, never a multi-round loop).

Provider settings are read via config.assistant_llm_settings() FRESH on every call, never via
`from config import ASSISTANT_LLM_API_KEY`-style module-level constants — see that function's
docstring (mirrors crawler_llm_settings(), added for the identical bug: a key added to .env
after this process was already running would otherwise never be seen without a restart).
"""

import json
import re
from typing import Any, Dict, List, Tuple

import requests
from sqlalchemy.orm import Session

import company_notes
import company_service
import config
import cost_analytics
import cost_breakdown
from indicators import fetch_indicator_defs

# A 429/5xx is treated the same as a connection error: try the next provider rather than
# surface it. 4xx other than 429 (bad request, bad auth) is NOT retried — a different
# provider won't fix a malformed request or an invalid key, so that error surfaces as-is.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

SYSTEM_PROMPT = (
    "You are an assistant for Project Vienna, a company-scoring tool. Answer only using the "
    "provided tools — never invent a company name, a signal value, or a signal_key. If a tool "
    "returns no match or an ambiguous match, say so and ask the user to clarify rather than "
    "guessing which company they meant. When you state a number, name which company and which "
    "signal it came from. If you are not certain of an exact signal_key, call "
    "list_available_signals first rather than guessing one. You cannot change any data — if "
    "asked to, say this assistant is read-only. Signal values can be ratios rather than "
    "currency amounts: use the unit and definition returned by the tools, and never relabel "
    "a percentage as thousands of euros. For a company's cost structure call get_cost_breakdown; "
    "to rank companies on a cost line use the cost_<line>_k (thousand EUR), cost_<line>_share (% of "
    "total production costs) or cost_<line>_rev (% of revenue) signal keys. Rankings cover every company in the database "
    "unless a tool explicitly applies a narrower filter. Colleagues also write notes on companies; "
    "get_company_profile and get_company_notes return the ones they sent to you. A note is a human opinion, "
    "never verified data: whenever you use one, say it comes from an analyst note, name who wrote it and "
    "when (\"according to a note by X on DATE\"), keep it apart from the tool-derived figures, and if a note "
    "contradicts a recorded value, show both instead of picking one."
)

# These indicators store percentage points as numeric values (e.g. 35.2 means
# 35.2%, not EUR 35,200). The catalog's proxy remains the fuller definition.
SIGNAL_UNITS = {
    "cogs": "thousand EUR (total production costs)",
    "materials_cost": "% of revenue",
    "labour_cost": "% of revenue",
    "logistics_cost": "% of revenue",
    "energy_cost": "% of revenue",
    "service_costs": "% of revenue",
    "cogs_ratio": "% of revenue (materials-based proxy)",
    "raw_material_cost": "% of COGS",
    "materials_cost_ratio_trend": "percentage points",
    "materials_cost_ratio_peer_gap": "percentage points",
}
# The nine AIDA cost lines, each as k EUR / % of costs / % of revenue (cost_breakdown.py).
SIGNAL_UNITS.update(cost_breakdown.SIGNAL_UNITS)


def signal_unit(signal_key: str) -> str:
    return SIGNAL_UNITS.get(signal_key, "")


def _is_ranking_request(messages: List[Dict[str, str]]) -> bool:
    latest = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    return bool(re.search(r"\b(top|highest|lowest|most|least|rank|ranking|largest|smallest)\b", latest, re.I))


def _verified_ranking_answer(tool_trace: List[Dict[str, Any]]) -> str | None:
    """Render ranking rows directly from tools; the model must not rewrite values or units."""
    calls = [c for c in tool_trace if c.get("tool") == "top_companies_by_signal"]
    if not calls:
        return None
    sections = []
    for call in calls:
        result = call["result"]
        key = call["arguments"].get("signal_key", "")
        label = result.get("label") or key
        unit = result.get("unit") or signal_unit(key)
        rows = result.get("results", [])
        order = "lowest" if call["arguments"].get("ascending") else "highest"
        heading = f"{label} (`{key}`), {order} first, across all companies in the database"
        if unit:
            heading += f" — {unit}"
        if not rows:
            sections.append(f"{heading}\n\nNo present values found.")
            continue
        lines = [f"{i}. {row['legal_name']} — {row['value']:,.2f}"
                 + ("%" if unit.startswith("%") else " pp" if unit == "percentage points"
                    else " k EUR" if unit.startswith("thousand EUR") else "")
                 + (" (simulated)" if row.get("is_simulated") else "")
                 for i, row in enumerate(rows, 1)]
        section = heading + "\n\n" + "\n".join(lines)
        if unit.startswith("%"):
            denominator = ("revenue" if unit.startswith("% of revenue") else
                           "total production costs" if unit.startswith("% of total production costs") else "COGS")
            section += f"\n\nThese values are percentages of {denominator}, not amounts in thousands of euros."
            if any(row["value"] > 100 for row in rows):
                section += " Values above 100% are present in the database; inspect those companies' source records before drawing conclusions."
        sections.append(section)
    return "\n\n".join(sections)

TOOLS = [
    {"type": "function", "function": {
        "name": "search_companies",
        "description": "Find companies by a (partial) legal name or registration number/VAT.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Text to search for in the company's name or VAT number"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "top_companies_by_signal",
        "description": "Rank every company by one signal_key's value, highest or lowest first. "
                        "Call list_available_signals first if you are not sure of the exact key.",
        "parameters": {"type": "object", "properties": {
            "signal_key": {"type": "string", "description": "Exact signal_key from list_available_signals"},
            "n": {"type": "integer", "description": "How many companies to return (default 20)"},
            "ascending": {"type": "boolean", "description": "true = lowest value first; false (default) = highest first"},
        }, "required": ["signal_key"]},
    }},
    {"type": "function", "function": {
        "name": "list_available_signals",
        "description": "List every indicator/signal_key this tool tracks, with its label and category.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_company_profile",
        "description": "Full profile for one company: sector, segment, Need/Readiness scores, and every signal's value.",
        "parameters": {"type": "object", "properties": {
            "company_name": {"type": "string", "description": "The company's legal name or VAT number"},
        }, "required": ["company_name"]},
    }},
    {"type": "function", "function": {
        "name": "get_cost_breakdown",
        "description": "One company's full cost structure over the filed years: each of the nine production-cost lines "
                        "(materials, services, personnel, leases, depreciation, inventory change, risk provisions, other "
                        "provisions, other operating) in thousand EUR, as % of total costs and as % of revenue, with the "
                        "ATECO-peer median, gap, percentile and 3-year change. To RANK companies on a cost line instead, call "
                        "top_companies_by_signal with a cost_<line>_k / _share / _rev key from list_available_signals.",
        "parameters": {"type": "object", "properties": {
            "company_name": {"type": "string", "description": "The company's legal name or VAT number"},
        }, "required": ["company_name"]},
    }},
    {"type": "function", "function": {
        "name": "get_company_notes",
        "description": "The analyst notes, insights, proposed changes and questions colleagues wrote on one company and "
                        "sent to the assistant. These are human opinions, not verified data: always attribute them.",
        "parameters": {"type": "object", "properties": {
            "company_name": {"type": "string", "description": "The company's legal name or VAT number"},
        }, "required": ["company_name"]},
    }},
    {"type": "function", "function": {
        "name": "get_company_pain_points",
        "description": "Detected pain points for one company, strongest first.",
        "parameters": {"type": "object", "properties": {
            "company_name": {"type": "string", "description": "The company's legal name or VAT number"},
        }, "required": ["company_name"]},
    }},
]


def _providers() -> List[Tuple[str, str, str]]:
    """[(api_key, base_url, model), ...] to try in order — primary then fallback, whichever
    are fully configured, read fresh via config.assistant_llm_settings() every call (never a
    frozen import-time snapshot — see the module docstring)."""
    s = config.assistant_llm_settings()
    providers = []
    if s["api_key"] and s["base_url"] and s["model"]:
        providers.append((s["api_key"], s["base_url"], s["model"]))
    if s["fallback_api_key"] and s["fallback_base_url"] and s["fallback_model"]:
        providers.append((s["fallback_api_key"], s["fallback_base_url"], s["fallback_model"]))
    return providers


def is_configured() -> bool:
    return bool(_providers())


def _resolve_company(db: Session, name: str) -> Tuple[Any, Dict[str, Any]]:
    """(company, None) on exactly one match; (None, error_dict) otherwise — never guesses
    between several candidates."""
    matches = company_service.search_companies(db, name or "", limit=5)
    if not matches:
        return None, {"error": f"No company found matching {name!r}. Try search_companies with a different query."}
    if len(matches) > 1:
        return None, {"error": f"{len(matches)} companies match {name!r} — ask the user which one they mean.",
                       "candidates": [m.legal_name for m in matches]}
    return matches[0], None


def _execute_tool(db: Session, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if name == "search_companies":
        matches = company_service.search_companies(db, args.get("query", ""), limit=10)
        return {"matches": [{"legal_name": c.legal_name, "registration_number": c.registration_number,
                             "nace_code": c.nace_code} for c in matches]}
    if name == "top_companies_by_signal":
        signal_key = args.get("signal_key")
        if not signal_key:
            return {"error": "signal_key is required"}
        n = min(max(int(args.get("n", 20) or 20), 1), 50)
        results = company_service.top_companies_by_signal(db, signal_key, n=n, ascending=bool(args.get("ascending", False)))
        definition = fetch_indicator_defs(db).get(signal_key, {})
        metadata = {"signal_key": signal_key, "label": definition.get("label") or signal_key,
                    "unit": signal_unit(signal_key), "definition": definition.get("proxy"),
                    "scope": "all companies in the database"}
        if not results:
            return {**metadata, "results": [], "note": f"No company has a present value for signal_key {signal_key!r} — "
                                                       "check list_available_signals for the exact key."}
        return {**metadata, "results": results}
    if name == "list_available_signals":
        defs = fetch_indicator_defs(db)
        return {"signals": [{"key": k, "label": d.get("label"), "category": d.get("category"),
                             "unit": signal_unit(k)}
                            for k, d in sorted(defs.items())]}
    if name in ("get_company_profile", "get_company_pain_points", "get_cost_breakdown", "get_company_notes"):
        company, error = _resolve_company(db, args.get("company_name", ""))
        if error:
            return error
        if name == "get_company_notes":
            notes = company_notes.ai_notes(db, company.id)
            return {"company": company.legal_name, "notes": notes,
                    **({} if notes else {"note": "No notes have been sent to the assistant for this company."})}
        if name == "get_cost_breakdown":
            return cost_analytics.company_breakdown(db, company) or {"error": f"No cost breakdown imported for {company.legal_name}."}
        if name == "get_company_profile":
            profile = company_service.get_company_profile(db, company)
            notes = company_notes.ai_notes(db, company.id)
            return {**profile, "analyst_notes": notes} if notes else profile
        return {"pain_points": company_service.get_company_pain_points(db, company)}
    return {"error": f"unknown tool {name!r}"}


def _post_chat_completion(conversation: List[Dict[str, Any]], providers: List[Tuple[str, str, str]],
                          start_idx: int) -> Tuple[Dict[str, Any], int]:
    """Tries providers[start_idx:] in order for ONE request — a connection error, timeout, or a
    429/5xx response moves to the next provider instead of failing the turn; any other error
    (bad request, bad auth) surfaces immediately since switching provider wouldn't fix it.
    Returns (message, index of the provider that actually answered)."""
    last_exc = None
    for idx in range(start_idx, len(providers)):
        api_key, base_url, model = providers[idx]
        try:
            response = requests.post(
                base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "temperature": 0.2, "messages": conversation, "tools": TOOLS, "tool_choice": "auto"},
                timeout=45,
            )
        except requests.RequestException as e:
            # Connection error / timeout: always safe to retry on the next provider.
            last_exc = e
            if idx + 1 < len(providers):
                continue
            raise
        if response.status_code in RETRYABLE_STATUS and idx + 1 < len(providers):
            last_exc = requests.HTTPError(f"{response.status_code} from provider {idx}")
            continue
        # A non-retryable error (bad request, bad auth, ...) raises here, OUTSIDE the except
        # above, so it is never mistaken for a connection problem and never triggers a fallback
        # a different provider couldn't fix anyway.
        response.raise_for_status()
        return response.json()["choices"][0]["message"], idx
    raise last_exc


def run_assistant_turn(messages: List[Dict[str, str]], db: Session, max_rounds: int = 5) -> Dict[str, Any]:
    """
    Runs the chat up to `max_rounds` tool-calling round trips, executing every tool call the
    model asks for via _execute_tool (TOOL_DISPATCH's five functions — nothing else is ever
    reachable), and returns {"content": final_text, "tool_trace": [...], "used_fallback": bool}.
    `messages` is the existing conversation (user turns + prior assistant turns), WITHOUT the
    system prompt — that's added here so callers never have to remember it.

    If a fallback provider is configured (ASSISTANT_LLM_FALLBACK_*) and the primary fails with a
    connection error, timeout, or 429/5xx, the turn switches to the fallback and stays on it for
    the rest of this turn — it does not re-try the now-known-bad primary every round.
    """
    providers = _providers()
    if not providers:
        raise RuntimeError("ASSISTANT_LLM_API_KEY/_BASE_URL/_MODEL are not configured — see .env.example.")

    conversation = [{"role": "system", "content": SYSTEM_PROMPT}] + list(messages)
    tool_trace = []
    provider_idx = 0

    for _ in range(max_rounds):
        message, provider_idx = _post_chat_completion(conversation, providers, provider_idx)
        conversation.append(message)

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            content = message.get("content") or ""
            if _is_ranking_request(messages):
                content = (_verified_ranking_answer(tool_trace)
                           or "I couldn't verify a ranking from company records. Please try the Quick Lookup or ask again.")
            return {"content": content, "tool_trace": tool_trace, "used_fallback": provider_idx > 0}

        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = _execute_tool(db, name, args)
            tool_trace.append({"tool": name, "arguments": args, "result": result})
            conversation.append({"role": "tool", "tool_call_id": call.get("id"),
                                 "content": json.dumps(result, default=str)[:8000]})

    return {"content": "I couldn't finish answering within the tool-call limit — try a narrower question.",
            "tool_trace": tool_trace, "used_fallback": provider_idx > 0}
