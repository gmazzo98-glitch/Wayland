"""Business-line figures printed in the notes of a payment institution's statement.

A Bank of Italy format statement of a payment institution carries, in Part D table E.8 ("Operating volumes, number and
revenues of payment transactions"), one row per service (money transfer in/out, acquiring ...) with the amount, the
number of operations, the commissions received and the expense recoveries, for the current and the previous year.
The same pages also print the agent receivables and payables and the protection fund per service.

Nothing here is stored: the saved statement already keeps its page text, so the figures are read back from it on
demand and every statement uploaded before this module existed works too. Each figure is only taken where it is
printed; a year or a line a statement does not print stays missing.
"""
import re

from balance_sheet_pdf import _is_number_token, parse_number

FIELDS = ("amount", "operations", "commission", "expense_recoveries")
FIELD_LABEL = dict(amount="Amount transferred (EUR)", operations="Operations", commission="Commissions received (EUR)",
                   expense_recoveries="Expense recoveries (EUR)")
_HEADER = re.compile(r"operating volumes|volumi operativi", re.I)
_END = re.compile(r"^\s*(section|sezione)\s+3\b|information on risks|informazioni sui rischi", re.I)
_TRANSFER = re.compile(r"money\s*transfer|remittance|rimess", re.I)
_ACQUIRING = re.compile(r"acquiring", re.I)
_INCOMING = re.compile(r"incoming|in entrata|\bentrata\b", re.I)
_OUTGOING = re.compile(r"outgoing|in uscita|\buscita\b", re.I)
_NUM = r"\(?-?[\d.,]*\d\)?"
_PAIR = r"\s+(" + _NUM + r")\s+(" + _NUM + r")"
_INCOME = re.compile(r"(?:fee and commission income|commission income|commissioni attive)" + _PAIR, re.I)
_AGENTS = {
    "agent_receivables": re.compile(r"(?:receivables from agents|crediti (?:verso|v/)\s*agenti[^\d\n]*)" + _PAIR, re.I),
    "agent_payables": re.compile(r"(?:payables (?:to/)?(?:to )?agents|debiti (?:verso|v/)\s*agenti[^\d\n]*)" + _PAIR, re.I),
}
_FUND = re.compile(r"protection fund[^.]*?(?:€|EUR)\s?([\d.,]+)\s+for the money transfer service and (?:€|EUR)\s?([\d.,]+)\s+for the acquiring", re.I)


def _year(period_end):
    return int(str(period_end)[:4]) if period_end and str(period_end)[:4].isdigit() else None


def _classify(label, group):
    """(segment key, direction) of a table row; `group` is the last heading above it."""
    text = f"{group} {label}"
    if _ACQUIRING.search(text):
        return "acquiring", None
    direction = "incoming" if _INCOMING.search(label) else "outgoing" if _OUTGOING.search(label) else None
    if _TRANSFER.search(text):
        return "money_transfer", direction
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "other"
    return slug, direction


def _row(line):
    """(label, eight figures) when the line ends in the eight numbers of an E.8 row."""
    tokens = line.split()
    n = 0
    while n < len(tokens) and _is_number_token(tokens[-1 - n]):
        n += 1
    if n < 8:
        return None
    figures = [parse_number(t) for t in tokens[len(tokens) - 8:]]
    return " ".join(tokens[:len(tokens) - 8]).strip(), figures


def _pages(statement):
    text = statement.get("page_text") or {}
    return "\n".join(text[k] for k in sorted(text, key=lambda k: int(k)))


def extract(statement):
    """Business-line rows of one statement dict (as saved in the record's `_statement`).

    {year: int|None, rows: [{segment, direction, label, years: {year: {amount, operations, commission, expense_recoveries}}}],
     agents: {key: {year: value}}, fund: {...}|None, checks: [...]}"""
    year = _year(statement.get("period_end"))
    text = _pages(statement)
    lines = text.splitlines()
    out = dict(year=year, rows=[], agents={}, fund=None, checks=[], source=statement.get("filename"))
    start = next((i for i, line in enumerate(lines) if _HEADER.search(line)), None)
    if start is not None and year:
        group = ""
        for line in lines[start + 1:]:
            if _END.search(line):
                break
            parsed = _row(line.strip())
            if parsed is None:
                if line.strip() and not re.search(r"\d", line) and (_TRANSFER.search(line) or _ACQUIRING.search(line)):
                    group = line.strip()
                continue
            label, figures = parsed
            segment, direction = _classify(label, group if label.startswith("-") or not (_TRANSFER.search(label) or _ACQUIRING.search(label)) else "")
            current, previous = figures[:4], figures[4:]
            out["rows"].append(dict(segment=segment, direction=direction, label=(label.lstrip("- ").strip() or segment),
                                    years={year: dict(zip(FIELDS, current)), year - 1: dict(zip(FIELDS, previous))}))
    for key, pattern in _AGENTS.items():
        match = pattern.search(text)
        if match and year:
            out["agents"][key] = {year: parse_number(match[1]), year - 1: parse_number(match[2])}
    fund = _FUND.search(text.replace("\n", " "))
    if fund and year:
        out["fund"] = {year: dict(money_transfer=parse_number(fund[1]), acquiring=parse_number(fund[2]))}
    income = _INCOME.search(text)
    if income and year and out["rows"]:
        for offset, printed in ((0, parse_number(income[1])), (1, parse_number(income[2]))):
            total = sum(r["years"][year - offset]["commission"] for r in out["rows"])
            ok = abs(total - printed) <= max(2.0, 0.0005 * abs(printed))
            out["checks"].append(dict(year=year - offset, printed=printed, summed=total, ok=ok,
                                      text=f"{year - offset}: lines add to {total:,.0f}, the income statement prints {printed:,.0f}"
                                           + ("" if ok else " (they differ)")))
    return out


def merge(statements):
    """One history from several statements. A year is taken from the statement that closes that year; if it only
    appears as a comparative it is taken from the newest statement carrying it, and a differing restatement is listed."""
    extracted = sorted((e for e in (extract(s) for s in statements) if e["year"]), key=lambda e: e["year"], reverse=True)
    rows, restated, checks, agents, fund = {}, [], [], {}, {}
    for e in extracted:
        for r in e["rows"]:
            slot = rows.setdefault((r["segment"], r["direction"]), dict(segment=r["segment"], direction=r["direction"], label=r["label"],
                                                                         years={}, source_year={}))
            for y, values in r["years"].items():
                if y not in slot["years"]:
                    slot["years"][y], slot["source_year"][y] = values, e["year"]
                elif slot["years"][y] == values:
                    if e["year"] == y:
                        slot["source_year"][y] = y  # the closing statement is the source, whatever later statements repeat
                else:
                    # Years arrive newest statement first, so the first one kept is the newest restatement of a comparative
                    # unless the later statement closes that very year, which wins as the original.
                    if e["year"] == y:
                        restated.append(dict(segment=r["segment"], direction=r["direction"], year=y, used=values, replaced=slot["years"][y],
                                             note=f"FY{y} is printed differently in the FY{slot['source_year'][y]} statement; the FY{y} statement's own figures are used"))
                        slot["years"][y], slot["source_year"][y] = values, e["year"]
                    else:
                        restated.append(dict(segment=r["segment"], direction=r["direction"], year=y, used=slot["years"][y], replaced=values,
                                             note=f"FY{y} differs between the FY{slot['source_year'][y]} and FY{e['year']} statements; the newer one is used"))
        for key, by_year in e["agents"].items():
            for y, v in by_year.items():
                agents.setdefault(key, {}).setdefault(y, v)
        for y, v in (e["fund"] or {}).items():
            fund.setdefault(y, v)
        checks += [dict(c, source=e["source"]) for c in e["checks"] if c["year"] == e["year"]]
    live = [r for r in rows.values() if any(any(v for v in y.values()) for y in r["years"].values())]
    return dict(rows=sorted(live, key=lambda r: (r["segment"] != "money_transfer", r["segment"], r["direction"] or "")),
                years=sorted({y for r in live for y in r["years"]}), restated=restated, checks=checks, agents=agents, fund=fund)


def metrics(row, year):
    """Derived figures of one row and year; a ratio whose input is missing or zero is None."""
    v, p = row["years"].get(year), row["years"].get(year - 1)
    if not v:
        return {}

    def ratio(a, b):
        return None if not b else a / b

    def growth(key):
        return None if not p or not p.get(key) else v[key] / p[key] - 1

    return dict(average_ticket=ratio(v["amount"], v["operations"]), take_rate=ratio(v["commission"], v["amount"]),
                commission_per_operation=ratio(v["commission"], v["operations"]),
                amount_growth=growth("amount"), operations_growth=growth("operations"), commission_growth=growth("commission"))


def shares(merged, year):
    """Each segment's share of the commissions of the year (directions of one segment are added)."""
    totals = {}
    for r in merged["rows"]:
        values = r["years"].get(year)
        if values:
            totals[r["segment"]] = totals.get(r["segment"], 0.0) + values["commission"]
    whole = sum(totals.values())
    return {} if not whole else {k: v / whole for k, v in totals.items()}


def company_segments(db, company_id):
    """Merged business-line history from every saved PDF statement of the company; None when no statement prints a table."""
    from balance_sheet_pdf import DATASET_PREFIX
    from models import RawImportRecord
    records = db.query(RawImportRecord).filter(RawImportRecord.company_id == company_id,
                                               RawImportRecord.dataset_name.like(DATASET_PREFIX + "%")).all()
    statements = []
    for record in records:
        statement = (record.raw_row or {}).get("_statement")
        if statement:
            statements.append(dict(statement, filename=statement.get("filename") or record.source_filename))
    merged = merge(statements)
    return merged if merged["rows"] else None
