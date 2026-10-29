"""
AI Assistant (Page 9) — two ways to ask "which companies have the highest/lowest X":

1. Quick Lookup: a direct, no-AI ranking of every company by one signal_key (the gap
   confirmed missing from every other page — Target Matrix only ranks by the 3 computed
   scores, Pain Points ranks by a composite pain-point score, never one raw signal alone).
2. Chat: free-form Q&A, answered by calling the exact same company_service.py query
   helpers as tools (assistant_llm.py) — read-only, provider-agnostic (Mistral or Gemini,
   see ASSISTANT_LLM_* in config.py / .env.example). Off with a clear message if neither
   is configured; Quick Lookup still works either way.
"""

import streamlit as st
from sqlalchemy.orm import Session

import assistant_llm
import company_service
from indicators import fetch_indicator_defs

MESSAGES_KEY = "ai_assistant_messages"


def _render_quick_lookup(db: Session):
    st.subheader("📊 Quick Lookup")
    st.caption("Rank every company by one signal, highest or lowest first — no AI needed.")

    defs = fetch_indicator_defs(db)
    options = sorted(defs.keys(), key=lambda k: (defs[k].get("category") or "", defs[k].get("label") or k))
    labels = {k: f"{defs[k].get('label') or k}  ({k})"
              + (f" — {assistant_llm.signal_unit(k)}" if assistant_llm.signal_unit(k) else "")
              for k in options}

    col1, col2, col3 = st.columns([3, 1, 1])
    with col1:
        signal_key = st.selectbox("Signal", options, format_func=lambda k: labels[k], key="quick_lookup_signal")
    with col2:
        n = st.number_input("Show top", min_value=1, max_value=50, value=20, key="quick_lookup_n")
    with col3:
        ascending = st.selectbox("Order", ["Highest first", "Lowest first"], key="quick_lookup_order") == "Lowest first"

    results = company_service.top_companies_by_signal(db, signal_key, n=int(n), ascending=ascending)
    if not results:
        st.info(f"No company has a present value for {labels[signal_key]} yet.")
        return
    st.dataframe(
        [{"Company": r["legal_name"], "VAT": r["registration_number"], "NACE/ATECO": r["nace_code"],
         labels[signal_key]: r["value"], "Data": "Simulated" if r.get("is_simulated") else "Recorded"}
         for r in results],
        hide_index=True, use_container_width=True,
    )


def _render_tool_trace(tool_trace: list):
    if not tool_trace:
        return
    with st.expander(f"🔍 {len(tool_trace)} tool call(s) used for this answer"):
        for call in tool_trace:
            st.markdown(f"**{call['tool']}**`({call['arguments']})`")
            st.json(call["result"])


def _render_chat(db: Session, state_key: str = MESSAGES_KEY, context_note: str = "", input_key: str = "ai_assistant_input",
                 suggestions: tuple = (), heading: bool = True, placeholder: str = ""):
    """The chat. `context_note` (e.g. "the user is looking at company X") is sent to the model as a leading turn but
    never shown; `suggestions` become one-click starter questions. The Company Intelligence dossier reuses this with
    its own state_key so each company keeps its own conversation."""
    if heading:
        st.subheader("💬 Ask the assistant")

    if not assistant_llm.is_configured():
        st.info(
            "No AI provider is configured. Set `ASSISTANT_LLM_API_KEY`, `ASSISTANT_LLM_BASE_URL` and "
            "`ASSISTANT_LLM_MODEL` (Mistral or Gemini — see `.env.example` for both) to enable chat. "
            "Quick Lookup above works without this."
        )
        return

    if state_key not in st.session_state:
        st.session_state[state_key] = []

    for msg in st.session_state[state_key]:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])
            if msg["role"] == "assistant":
                if msg.get("used_fallback"):
                    st.caption("↪️ answered via the fallback AI provider — the primary one failed or was rate-limited")
                _render_tool_trace(msg.get("tool_trace", []))

    question = st.chat_input(placeholder or "Ask about a company, e.g. \"which companies have the highest service costs?\"",
                             key=input_key)
    if not question and suggestions and not st.session_state[state_key]:
        picked = st.pills("Try asking", list(suggestions), key=f"{input_key}_suggest", label_visibility="collapsed")
        question = picked
    if not question:
        return

    st.session_state[state_key].append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.write(question)

    history = [{"role": m["role"], "content": m["content"]} for m in st.session_state[state_key]]
    if context_note:
        history = [{"role": "user", "content": context_note}, {"role": "assistant", "content": "Understood."}] + history
    with st.chat_message("assistant"):
        with st.spinner("Looking it up…"):
            try:
                result = assistant_llm.run_assistant_turn(history, db)
                content, tool_trace, used_fallback = result["content"], result["tool_trace"], result["used_fallback"]
            except Exception as e:  # noqa: BLE001 — surfaced to the user, not a crash
                content, tool_trace, used_fallback = f"Something went wrong asking the assistant: {e}", [], False
        st.write(content)
        if used_fallback:
            st.caption("↪️ answered via the fallback AI provider — the primary one failed or was rate-limited")
        _render_tool_trace(tool_trace)

    st.session_state[state_key].append(
        {"role": "assistant", "content": content, "tool_trace": tool_trace, "used_fallback": used_fallback})


def render_company_chat(db: Session, company) -> None:
    """Chat scoped to one company (Company Intelligence > Actions). Same read-only tools, plus a hidden note naming
    the company so 'its costs' / 'this company' resolve without the user typing the name."""
    note = (f"Context: the user is reading the dossier of {company.legal_name} (registration number "
            f"{company.registration_number}). Unless they name another company, 'this company', 'it' and 'its' mean "
            f"{company.legal_name}; use get_company_profile / get_cost_breakdown / get_company_pain_points / get_company_notes with that name. "
            f"Colleagues' notes that were sent to you are analyst opinions: always attribute them, never state them as recorded fact.")
    _render_chat(db, state_key=f"ai_company_chat_{company.id}", context_note=note, input_key=f"ai_company_input_{company.id}",
                 heading=False, placeholder=f"Ask about {company.legal_name}…",
                 suggestions=("Summarise this company in five bullets",
                              "Why is its Need score what it is?",
                              "How do its costs compare with its peers?",
                              "What are the biggest risks I should know before calling them?"))


def render_ai_assistant_page(db: Session):
    st.title("🤖 AI Assistant")
    st.caption("Read-only: this page can look companies up, never change anything in the database.")
    _render_quick_lookup(db)
    st.divider()
    _render_chat(db)
