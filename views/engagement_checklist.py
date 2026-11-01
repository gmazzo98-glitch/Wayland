"""Dossier section: engagement questions next to what the statements already answer, with the team's progress."""
import streamlit as st

import engagement_checklist as ec
import statement_segments as ss


def render_engagement(db, company, sig_dict=None, scope="eng"):
    merged = ss.company_segments(db, company.id)
    employees = None
    if sig_dict and sig_dict.get("number_of_employees") is not None:
        employees = getattr(sig_dict["number_of_employees"], "numeric_value", None)
    base = ec.baselines(merged, employees)
    items = ec.load(db, company.id)
    done, total = ec.progress(items)
    st.subheader("🗺️ Engagement questions")
    st.caption("Each question from the briefing, the baseline the saved statements already give (with its source) or a plain "
               "'nothing usable', the data to request and who usually holds it. Progress is shared by the team; "
               "if two people save the same question together, the last save stays.")
    st.progress(done / total, text=f"{done} of {total} questions have their data")
    if not merged:
        st.info("No saved statement prints a business-line table for this company, so no baseline can be shown; the questions and data requests still apply.")
    author = st.text_input("Your name (shown on what you save)", key=f"{scope}_author")
    area = None
    for topic_id, topic_area, question, key, request, holder in ec.TOPICS:
        if topic_area != area:
            st.markdown(f"#### {topic_area}")
            area = topic_area
        saved = items.get(topic_id) or {}
        with st.expander(f"{'✅' if saved.get('status') == 'Received' else '🔹'} {question}", expanded=False):
            if key and key in base:
                text, source = base[key]
                st.markdown(f"**Already in the statements:** {text}")
                st.caption(f"Source: {source}")
            else:
                st.markdown("**Already in the statements:** nothing usable for this question.")
            st.markdown(f"**Data to request:** {request}")
            c1, c2 = st.columns(2)
            status = c1.selectbox("Status", ec.STATUSES, index=ec.STATUSES.index(saved.get("status", "To ask")), key=f"{scope}_s_{topic_id}")
            who = c2.text_input("Who holds it", value=saved.get("holder") or holder, key=f"{scope}_h_{topic_id}")
            note = st.text_area("Note", value=saved.get("note", ""), key=f"{scope}_n_{topic_id}", height=80)
            if st.button("Save", key=f"{scope}_b_{topic_id}"):
                ec.save(db, company.id, topic_id, status, who, note, author)
                st.rerun()
            if saved:
                st.caption(f"Last saved by {saved.get('updated_by')} on {saved.get('updated_at', '')[:16].replace('T', ' ')}")
