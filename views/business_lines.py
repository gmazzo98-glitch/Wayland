"""Business lines of a payment institution, read from the notes of its saved statements (Company Intelligence, Financials)."""
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import statement_segments as ss

SEGMENT_NAME = {"money_transfer": "Money transfer", "acquiring": "Acquiring (POS)"}
COLORS = {"money_transfer": "#4C78A8", "acquiring": "#F58518"}
DIRECTION_NAME = {"incoming": "incoming", "outgoing": "outgoing"}


def _name(row):
    base = SEGMENT_NAME.get(row["segment"], row["label"])
    return f"{base} ({DIRECTION_NAME[row['direction']]})" if row["direction"] and row["segment"] == "money_transfer" else base


def _eur(v, d=0):
    return "-" if v is None else f"{v:,.{d}f}"


def _pct(v, d=1, sign=False):
    return "-" if v is None else f"{v * 100:{'+' if sign else ''}.{d}f}%"


def render_business_lines(db, company, scope="bl"):
    merged = ss.company_segments(db, company.id)
    if not merged:
        return
    years = merged["years"]
    latest = years[-1]
    st.subheader("🧩 Business lines (from the statement notes)")
    st.caption("Read from the operating-volumes table of the uploaded statements, as printed (euro). Years come from the statement that "
               "closes them; a year printed only as a comparative is marked. Nothing is estimated.")
    share = ss.shares(merged, latest)
    rows = merged["rows"]
    cols = st.columns(max(1, len(rows)))
    for col, row in zip(cols, rows):
        m = ss.metrics(row, latest)
        values = row["years"].get(latest)
        col.metric(f"{_name(row)} FY{latest}", f"{_eur(values['commission'] / 1000 if values else None, 1)} k EUR",
                   None if not m or m.get("commission_growth") is None else f"{m['commission_growth'] * 100:+.1f}% vs FY{latest - 1}")
        if values:
            col.caption(f"{values['operations']:,.0f} operations, average ticket {_eur(m.get('average_ticket'))}, "
                        f"take rate {_pct(m.get('take_rate'), 2)}, {_pct(share.get(row['segment']))} of commissions"
                        if row["segment"] in share else f"{values['operations']:,.0f} operations")

    table = []
    for row in rows:
        for y in reversed(years):
            values = row["years"].get(y)
            if not values:
                continue
            m = ss.metrics(row, y)
            table.append({"Line": _name(row), "Year": f"FY{y}" + ("" if row["source_year"].get(y) == y else " (comparative)"),
                          "Amount (k EUR)": _eur(values["amount"] / 1000), "Operations": _eur(values["operations"]),
                          "Commissions (k EUR)": _eur(values["commission"] / 1000), "Average ticket (EUR)": _eur(m.get("average_ticket"), 1),
                          "Take rate": _pct(m.get("take_rate"), 2), "Commission / operation (EUR)": _eur(m.get("commission_per_operation"), 2),
                          "Amount growth": _pct(m.get("amount_growth"), sign=True), "Operations growth": _pct(m.get("operations_growth"), sign=True)})
    st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)

    left, right = st.columns(2)
    fig = go.Figure()
    for row in rows:
        fig.add_bar(name=_name(row), x=[f"FY{y}" for y in years], y=[(row["years"].get(y) or {}).get("commission") for y in years],
                    marker_color=COLORS.get(row["segment"]))
    fig.update_layout(template="plotly_dark", barmode="stack", height=300, margin=dict(t=30, b=10), title="Commissions received (EUR)",
                      legend=dict(orientation="h", y=-0.2))
    left.plotly_chart(fig, use_container_width=True, key=f"{scope}_commission")
    fig = go.Figure()
    for row in rows:
        fig.add_scatter(name=_name(row), x=[f"FY{y}" for y in years], mode="lines+markers", marker_color=COLORS.get(row["segment"]),
                        y=[None if not ss.metrics(row, y).get("take_rate") else ss.metrics(row, y)["take_rate"] * 100 for y in years])
    fig.update_layout(template="plotly_dark", height=300, margin=dict(t=30, b=10), title="Take rate: commissions / amount (%)",
                      legend=dict(orientation="h", y=-0.2))
    right.plotly_chart(fig, use_container_width=True, key=f"{scope}_take")

    exposure = []
    for label, key in (("Receivables from agents", "agent_receivables"), ("Payables to agents", "agent_payables")):
        if merged["agents"].get(key):
            exposure.append({"": label, **{f"FY{y}": _eur(merged["agents"][key].get(y)) for y in reversed(years)}})
    for line, name in (("money_transfer", "Protection fund, money transfer"), ("acquiring", "Protection fund, acquiring")):
        if merged["fund"]:
            exposure.append({"": name, **{f"FY{y}": _eur((merged["fund"].get(y) or {}).get(line)) for y in reversed(years)}})
    if exposure:
        st.markdown("**Agent exposure and customer-money protection (EUR)**")
        st.dataframe(pd.DataFrame(exposure).fillna("-"), hide_index=True, use_container_width=True)
    for check in merged["checks"]:
        st.caption(("✅ " if check["ok"] else "⚠️ ") + f"Commission cross-check, {check['text']}.")
    for item in merged["restated"]:
        st.caption(f"⚠️ {item['note']}.")
