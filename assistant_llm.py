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
from typing import Any, Dict, List, Tuple

import requests
from sqlalchemy.orm import Session

import company_service
import config
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
    "asked to, say this assistant is read-only."
)

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
        if not results:
            return {"results": [], "note": f"No company has a present value for signal_key {signal_key!r} — "
                                            "check list_available_signals for the exact key."}
        return {"results": results}
    if name == "list_available_signals":
        defs = fetch_indicator_defs(db)
        return {"signals": [{"key": k, "label": d.get("label"), "category": d.get("category")}
                            for k, d in sorted(defs.items())]}
    if name in ("get_company_profile", "get_company_pain_points"):
        company, error = _resolve_company(db, args.get("company_name", ""))
        if error:
            return error
        if name == "get_company_profile":
            return company_service.get_company_profile(db, company)
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
            return {"content": message.get("content") or "", "tool_trace": tool_trace, "used_fallback": provider_idx > 0}

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
