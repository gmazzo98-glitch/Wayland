"""
One-page printable dossier for a company: a single self-contained HTML file (no external assets) that prints
cleanly to PDF from any browser (File > Print > Save as PDF).

`build_html(snapshot)` is pure: it renders exactly the dict views/company_dossier.py assembles, so the export can
never show a number the dossier itself doesn't. Every dynamic string is HTML-escaped.
"""

from datetime import datetime
from html import escape
from typing import Any, Dict, List, Optional

LEVEL_ICON = {"risk": "🔴", "watch": "🟠", "strength": "🟢"}
PERIODS = ["latest", "y-1", "y-2"]


def _n(v: Optional[float], d: int = 0, suffix: str = "") -> str:
    return "—" if v is None else f"{v:,.{d}f}{suffix}"


def _table(headers: List[str], rows: List[List[Any]], num_cols_from: int = 1) -> str:
    th = "".join(f"<th>{escape(str(h))}</th>" for h in headers)
    body = ""
    for r in rows:
        tds = "".join(
            f'<td class="{"n" if i >= num_cols_from else ""}">{escape(str(c))}</td>' for i, c in enumerate(r))
        body += f"<tr>{tds}</tr>"
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def _statement_table(st: Optional[dict]) -> str:
    if not st or not st.get("has_data"):
        return "<p class='muted'>No multi-year financial statement on file.</p>"
    labels = [st["labels"][p] for p in PERIODS]
    L, R = st["lines"], st["ratios"]
    rows = []
    for key, label in (("revenue", "Revenue (k EUR)"), ("ebitda", "EBITDA (k EUR)"), ("ebit", "EBIT (k EUR)"),
                       ("net_income", "Net income (k EUR)"), ("total_assets", "Total assets (k EUR)"),
                       ("equity", "Equity (k EUR)"), ("cash", "Cash (k EUR)"), ("employees", "Employees")):
        rows.append([label] + [_n(L[key][p]) for p in PERIODS])
    for key, label in (("revenue_growth", "Revenue growth %"), ("ebitda_margin", "EBITDA margin %"),
                       ("net_margin", "Net margin %"), ("equity_ratio", "Equity / assets %")):
        rows.append([label] + [_n(R[key][p], 1) for p in PERIODS])
    return _table(["", *labels], rows)


def _cost_table(cost: Optional[dict]) -> str:
    if not cost:
        return "<p class='muted'>No cost breakdown imported for this company.</p>"
    rows = [[c["label"], _n(c["k_eur"]), _n(c["share_of_costs_pct"], 1), _n(c["pct_of_revenue"], 1),
             _n(c["peer_median_share_pct"], 1), _n(c["gap_vs_peers_pp"], 1)] for c in cost["categories"]]
    head = f"<p class='muted'>Total production costs {_n(cost['total_costs_k_eur'])}k EUR · peers: {escape(cost['peer_basis'])}</p>"
    return head + _table(["Cost line", "k EUR", "% of costs", "% of revenue", "Peer median %", "Gap (pp)"], rows)


def build_html(s: Dict[str, Any]) -> str:
    c, sc = s["company"], s["scores"]
    kpis = "".join(f'<div class="kpi"><div class="v">{escape(str(v))}</div><div class="l">{escape(str(k))}</div></div>'
                   for k, v in s.get("kpis", []))
    flags = "".join(f"<li>{LEVEL_ICON.get(f['level'], '•')} {escape(f['text'])}</li>" for f in s.get("flags", [])) \
        or "<li class='muted'>No flags raised.</li>"
    pains = "".join(
        f"<li><b>{escape(p['label'])}</b> — {escape(p['status'])} ({p['score']:.0f}/100)"
        + (f"<br><span class='muted'>{escape(p['headline'])}</span>" if p.get("headline") else "") + "</li>"
        for p in s.get("pain_points", [])) or "<li class='muted'>No pain point detected in the available data.</li>"
    findings = "".join(f"<li>{escape(t)}</li>" for t in s.get("brief_findings", []))
    people = _table(["Name", "Role", "Age"], [[p["name"], p["role"], p["age"]] for p in s.get("people", [])[:12]], 99) \
        if s.get("people") else "<p class='muted'>No roster imported.</p>"
    own = s.get("ownership") or {}
    owners = _table(["Shareholder", "Type", "Country", "Direct %"],
                    [[o["Name"], o["Type"], o["Country"], _n(o["Direct %"], 1)] for o in own.get("shareholders", [])[:8]], 99) \
        if own.get("shareholders") else "<p class='muted'>No shareholder list on file.</p>"
    facts = " · ".join(escape(str(x)) for x in (c.get("ateco"), c.get("sector"), c.get("region"), c.get("legal_form"),
                                                 f"{c['headcount']} staff" if c.get("headcount") else None) if x)
    css = ("body{font:13px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;color:#1b1f24;margin:24px auto;max-width:900px;padding:0 16px}"
           "h1{font-size:22px;margin:0}h2{font-size:14px;margin:18px 0 6px;border-bottom:2px solid #2d6cdf;padding-bottom:2px;color:#2d6cdf}"
           ".muted{color:#6a737d}.kpis{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}"
           ".kpi{border:1px solid #d0d7de;border-radius:6px;padding:6px 12px;min-width:110px}.kpi .v{font-size:17px;font-weight:600}"
           ".kpi .l{font-size:11px;color:#6a737d}table{border-collapse:collapse;width:100%;font-size:12px}"
           "th,td{border-bottom:1px solid #e1e4e8;padding:3px 6px;text-align:left}td.n{text-align:right}th{background:#f6f8fa}"
           "ul{margin:4px 0 4px 18px;padding:0}.cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}"
           "@media print{body{margin:0;max-width:none}h2{break-after:avoid}table,li{break-inside:avoid}}")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{escape(c['name'])} — dossier</title>
<style>{css}</style></head><body>
<h1>{escape(c['name'])}</h1>
<div class="muted">{escape(str(c.get('vat') or ''))} · {facts}</div>
<div class="muted">{escape(str(c.get('website') or ''))} · Status: {escape(str(c.get('status') or ''))}{' · Parent: ' + escape(c['parent']) if c.get('parent') else ''}</div>
<div class="kpis"><div class="kpi"><div class="v">{_n(sc.get('need'), 0)}</div><div class="l">Need /100</div></div>
<div class="kpi"><div class="v">{_n(sc.get('readiness'), 0)}</div><div class="l">Readiness /100</div></div>
<div class="kpi"><div class="v">{_n(sc.get('completeness'), 0, '%')}</div><div class="l">Data completeness</div></div>{kpis}</div>
<h2>Flags</h2><ul>{flags}</ul>
<div class="cols"><div><h2>Pain points</h2><ul>{pains}</ul></div>
<div><h2>AI report — key findings</h2><ul>{findings or "<li class='muted'>No AI report generated.</li>"}</ul></div></div>
<h2>Financials</h2>{_statement_table(s.get('statement'))}
<h2>Cost structure</h2>{_cost_table(s.get('cost'))}
<div class="cols"><div><h2>People</h2>{people}</div><div><h2>Ownership</h2>{owners}</div></div>
<p class="muted" style="margin-top:18px">Generated {datetime.now():%Y-%m-%d %H:%M} from Project Vienna. Scores and flags are
screening aids built from filed accounts and crawled public sources; verify before outreach.</p>
</body></html>"""
