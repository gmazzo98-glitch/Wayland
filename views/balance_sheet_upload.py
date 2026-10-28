"""Balance sheet (PDF) upload: read, check, map, resolve conflicts, import."""
import hashlib

import pandas as pd
import streamlit as st

import balance_sheet_pdf as bs
import financial_ledger as fl

NEW_LABEL = "+ Create New Indicator..."
STATEMENTS = {"assets": "Balance sheet - assets", "liabilities": "Balance sheet - liabilities and equity",
              "income": "Income statement", "comprehensive": "Comprehensive income"}


def _money(value):
    return "" if value is None else f"{value:,.0f}"


def _render_statement(statement):
    a, b, c, d = st.columns(4)
    a.metric("Statement date", statement["period_end"] or "?")
    b.metric("Lines read", len(statement["lines"]))
    c.metric("Pages", len(statement["pages"]))
    d.metric("Read by OCR", sum(p["method"] == "ocr" for p in statement["pages"]))
    company = statement["company"]
    st.markdown(f"**{company.get('legal_name', 'Company not recognised')}** · {company.get('registration_number', 'no fiscal code')}")
    for warning in statement["warnings"]:
        st.warning(warning)
    if statement["checks"]:
        with st.expander("Arithmetic checks on the figures read", expanded=not all(c["ok"] for c in statement["checks"])):
            st.dataframe(pd.DataFrame([{"Check": c["name"], "Result": "OK" if c["ok"] else "FAILED", "Values": c["detail"]}
                                       for c in statement["checks"]]), hide_index=True, width="stretch")
    for key_, title in STATEMENTS.items():
        rows = [l for l in statement["lines"] if l["statement"] == key_]
        if rows:
            with st.expander(f"{title} ({len(rows)} lines)", expanded=False):
                st.dataframe(pd.DataFrame([{"Page": l["page"], "Line": l["label"], **{d: _money(v) for d, v in l["values"].items()},
                                            "Notes": "; ".join(l["flags"])} for l in rows]), hide_index=True, width="stretch")
    with st.expander("Text read from each page", expanded=False):
        for page in statement["pages"]:
            st.caption(f"Page {page['page']} · {page['method']}")
            st.text(statement["page_text"][str(page["page"])][:3000])


def _mapping_editor(db, statement, signature):
    from company_service import list_mapping_profiles
    st.markdown("##### Connect statement lines")
    st.caption("A connected line feeds the financial fact for every year it prints, converted to thousand EUR, and the "
               "calculated indicators follow. A direct indicator takes the statement-date value in the printed unit. "
               "Lines you leave unconnected stay in the saved statement.")
    profile_name = st.text_input("Mapping profile name", value="Standard balance sheet", key="bs_profile")
    names = [p.dataset_name[len(fl.PROFILE_PREFIX):] for p in list_mapping_profiles(db) if p.dataset_name.startswith(fl.PROFILE_PREFIX)]
    if names:
        st.caption("Saved statement profiles: " + ", ".join(names))
    if not profile_name.strip():
        st.warning("Enter a mapping profile name before importing.")
        return None, None, None
    import json
    profile = json.loads(next((p.mapping_json for p in list_mapping_profiles(db) if p.dataset_name == fl.profile_key(profile_name)), "{}"))
    context = f"{signature}|{profile_name}"
    if st.session_state.get("bs_context") != context:
        st.session_state.update(bs_context=context, bs_suggestions=bs.suggest_mapping(statement, profile), bs_version=0)
    suggestions = st.session_state["bs_suggestions"]
    labels = fl.targets(db)
    options = list(labels.values()) + [NEW_LABEL]
    by_label = {v: k for k, v in labels.items()}
    left, right = st.columns(2)
    if left.button("Ask the AI to suggest the remaining lines", disabled=not bs.llm_available("text"),
                   help="Needs a language model key (see the Crawler LLM settings). Suggestions are only proposals.", width="stretch"):
        try:
            with st.spinner("Asking the language model..."):
                extra = bs.ai_suggestions(statement, suggestions)
            suggestions.update(extra)
            st.session_state["bs_version"] += 1
            st.toast(f"{len(extra)} suggestions added; review them in the table.")
        except Exception as exc:
            st.error(f"The AI could not suggest mappings: {exc}")
    if right.button("Accept every AI suggestion above 80% confidence", width="stretch"):
        applied = 0
        for s in suggestions.values():
            if s["origin"] == "ai" and s["confidence"] >= 0.8:
                s["origin"] = "ai-accepted"
                applied += 1
        st.session_state["bs_version"] += 1
        st.toast(f"{applied} AI suggestions applied.")
    frame = pd.DataFrame([{
        "Line": l["label"], "Page": l["page"], **{d: _money(v) for d, v in l["values"].items()},
        "Suggested by": f"{suggestions[l['id']]['origin']} {suggestions[l['id']]['confidence']:.0%}" if suggestions[l["id"]]["origin"] != "none" else "",
        "Note": suggestions[l["id"]]["note"],
        "Connect to": labels.get(suggestions[l["id"]]["target"] if suggestions[l["id"]]["origin"] != "ai" else "", labels[""]),
        "New indicator label": "", "_id": l["id"]} for l in statement["lines"]])
    edited = st.data_editor(frame.drop(columns=["_id"]), hide_index=True, width="stretch", height=420,
                            disabled=[c for c in frame.columns if c not in {"Connect to", "New indicator label", "_id"}],
                            column_config={"Connect to": st.column_config.SelectboxColumn("Connect to", options=options, required=True,
                                                                                            width="large")},
                            key=f"bs_editor_{context}_{st.session_state['bs_version']}")
    ai_open = [(l["label"], suggestions[l["id"]]) for l in statement["lines"] if suggestions[l["id"]]["origin"] == "ai"]
    if ai_open:
        st.caption("AI proposals not yet applied: " + "; ".join(
            f"{label} → {s['target'].split(':')[1]} ({s['confidence']:.0%}: {s['note']})" for label, s in ai_open[:8])
            + ". Pick them in the table to use them.")
    mapping, new_labels = {}, {}
    for line, (_, row) in zip(statement["lines"], edited.iterrows()):
        picked = row["Connect to"]
        target = fl.NEW_INDICATOR if picked == NEW_LABEL else by_label.get(picked, "")
        mapping[line["id"]] = target
        if target == fl.NEW_INDICATOR:
            new_labels[line["id"]] = row["New indicator label"] or ""
    return mapping, new_labels, profile_name.strip()


def render_upload(db):
    st.subheader("Balance sheet upload (PDF)")
    st.caption("Upload a company's annual financial statements. The text layer is read first; scanned pages are read by OCR "
               "when a vision model is configured. The system recognises the statements, checks their arithmetic and "
               "proposes the connections. Figures merge with the company's other sources by fiscal year; where two "
               "sources disagree, the balance sheet is used unless you decide otherwise.")
    uploaded = st.file_uploader("Upload balance sheet (.pdf)", type=["pdf"], key="bs_upload")
    if uploaded is None:
        return
    content = uploaded.getvalue()
    signature = hashlib.sha256(content).hexdigest()
    try:
        if st.session_state.get("bs_signature") != signature:
            with st.spinner("Reading the statements..."):
                ocr = bs.vision_ocr if bs.llm_available("vision") else None
                statement = bs.parse_statement(content, uploaded.name, ocr=ocr)
            st.session_state.update(bs_statement=statement, bs_signature=signature, bs_context=None)
        statement = st.session_state["bs_statement"]
    except Exception as exc:
        st.error(f"Could not read this PDF: {exc}")
        return
    _render_statement(statement)

    detected = {"EUR": 0, "thousand EUR": 1}.get(statement["unit"])
    unit_label = st.radio("Amounts in the statement are in", ["Euro", "Thousands of euro"], index=detected, horizontal=True,
                          key=f"bs_unit_{signature}", help="Read from the statement when printed; the radio is empty if it was not found.")
    unit = {"Euro": "EUR", "Thousands of euro": "thousand EUR"}.get(unit_label)
    if not statement["period_end"]:
        statement["period_end"] = str(st.date_input("Statement date", key=f"bs_date_{signature}"))
    if not unit:
        st.info("Choose the unit to continue.")
        return
    mapping, new_labels, profile_name = _mapping_editor(db, statement, signature)
    if mapping is None:
        return
    decisions = {}
    try:
        plan = fl.plan_statement(db, statement, mapping, new_labels, unit)
    except ValueError as exc:
        st.error(str(exc))
        return
    for warning in plan["warnings"]:
        st.warning(warning)
    merged = plan["merged"]
    st.info(f"{plan['action']}: {statement['company'].get('legal_name')} ({statement['company'].get('registration_number')}).")
    if merged["anchor"]:
        before = merged["previous_anchor"]
        st.markdown(f"**Year labels:** Latest = FY{merged['anchor']}"
                    + (f" (was FY{before}; every relative label moves with it)" if before and before != merged["anchor"] else "")
                    + ". " + ", ".join(f"{p} = {y}" for p, y in list(merged["labels"].items())[1:4]) + " ...")
    if merged["missing"]:
        st.caption("No value for FY" + str(merged["anchor"]) + ": " + ", ".join(merged["missing"])
                   + ". These stay missing; earlier figures remain visible as dated history only.")

    if merged["conflicts"]:
        st.markdown("##### Data conflicts")
        st.caption("The same fact and year differs between sources. The balance sheet is selected by default; change a row to keep the other value.")
        for conflict in merged["conflicts"]:
            names = {o["dataset"]: f"{o['dataset'].replace(fl.DATASET_PREFIX, 'Balance sheet ')}: {o['value']:,.3f}" for o in conflict["options"]}
            picked = st.selectbox(f"{conflict['base'].replace('_', ' ').title()} {conflict['year']} (thousand EUR)", list(names),
                                  index=list(names).index(conflict["chosen"]), format_func=names.get,
                                  key=f"bs_conf_{signature}_{conflict['key']}")
            if picked != conflict["default"]:
                decisions[conflict["key"]] = picked
        plan = fl.plan_statement(db, statement, mapping, new_labels, unit, decisions)
        merged = plan["merged"]

    replace = {}
    decide = [c for c in plan["changes"] if c["decide"]]
    if decide:
        st.markdown("##### Indicators that would change")
        st.caption("These indicators already hold a value from another source or period. Untick a row to keep the existing value.")
        table = st.data_editor(pd.DataFrame([{"Use new value": True, "Indicator": c["signal"], "Existing": c["existing"],
                                              "Existing source": c["existing_source"],
                                              "New": c["new"] if c["new"] is not None else "no longer calculable"} for c in decide]),
                               hide_index=True, width="stretch", disabled=["Indicator", "Existing", "Existing source", "New"],
                               key=f"bs_sig_{signature}_{len(decide)}")
        replace = {c["signal"]: bool(use) for c, use in zip(decide, table["Use new value"])}
    other = [c for c in plan["changes"] if not c["decide"]]
    if other:
        with st.expander(f"{len(other)} indicators calculated without conflict", expanded=False):
            st.dataframe(pd.DataFrame([{"Indicator": c["signal"], "New": c["new"]} for c in other]), hide_index=True, width="stretch")

    replace_statement = False
    if plan["replaced_statement"]:
        replace_statement = st.checkbox("Replace the statement already saved for this period end", key=f"bs_replace_{signature}")
    if st.button("Import balance sheet", key="bs_confirm", width="stretch", disabled=plan["replaced_statement"] and not replace_statement):
        try:
            result = fl.import_statement(db, statement, mapping, new_labels, unit, decisions, replace, profile_name, replace_statement)
        except Exception as exc:
            st.error(f"Import failed; nothing was saved: {exc}")
            return
        st.success(f"Imported. Latest year is now FY{result['anchor']}; {result['signals_written']} indicators written, "
                   f"{result['signals_cleared']} withdrawn. The merged history is in Company Intelligence.")
