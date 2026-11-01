"""Engagement checklist: the questions to settle with a company, each next to what its saved statements already answer.

A topic carries the question, the baseline the statements give (computed live from the business-line figures, with its
source) or an honest "not in the statements", the data to request and who usually holds it. The team's progress per
topic (status, holder, note) is one JSON record per company; two people saving the same topic at once keep the last
save. Nothing here touches scoring.
"""
from datetime import datetime

import statement_segments as ss

DATASET = "Engagement checklist"
STATUSES = ("To ask", "Asked", "Received", "Not available")

# id, area, question, baseline key (or None), data to request, usual holder
TOPICS = [
    ("mt_volumes", "1. Money transfer: volumes, market, pricing", "How many operations and what amounts, by channel and corridor (Peru looks strong)?",
     "mt_volumes", "Operations and amounts by month, channel and destination corridor, 3 years", "Operations / IT"),
    ("mt_pricing", "1. Money transfer: volumes, market, pricing", "How is the price built: partner rate, our margin, and what does the 16% mean?",
     "mt_pricing", "Partner rate sheets, the margin rule per corridor, a sample of 50 priced orders with partner rate and customer rate", "Treasury / Commercial"),
    ("mt_loyalty", "1. Money transfer: volumes, market, pricing", "How loyal are customers, and how much does the offered rate drive the choice?",
     None, "Orders per customer per year, repeat share, share of orders won or lost against competitors' rates", "Commercial"),
    ("mt_forecast", "1. Money transfer: volumes, market, pricing", "Can volumes, partner terms, prices and margins be linked into a forecast?",
     "mt_volumes", "Monthly history joined to partner terms and the Bank of Italy corridor series", "Controlling"),
    ("kyc_time", "2. KYC, controls and back office", "How long does an order take, how many people touch it, how many are stopped for extra checks?",
     "ops_per_employee", "Order timestamps by step and person; count of orders held for review; watch-list hit handling log", "Compliance / Back office"),
    ("kyc_rework", "2. KYC, controls and back office", "What do exceptions, e-mails and rework cost, separating required checks from avoidable work?",
     None, "Ticket or mailbox export of exceptions with handler, minutes and outcome; list of manual steps", "Compliance"),
    ("process_mining", "3. Productivity and process mining", "How many hours go to each activity and what output per hour at what complexity?",
     "ops_per_employee", "Event logs of the order system; timesheets or staffing per team; definition of a complex order", "Operations / HR"),
    ("benchmarks", "3. Productivity and process mining", "Which benchmarks against Western Union, Ria and similar operators are usable?",
     None, "Peers' published annual reports (take rate, operations per employee) to compare with the baseline", "Wayland"),
    ("agent_model", "4. Agents and risk", "What is the agent model: prefunding, credit line or other, limits, how reliability is judged?",
     "agents", "Agent contracts, limit and exposure per agent, list of agents with payment delays", "Legal / Treasury"),
    ("agent_monitoring", "4. Agents and risk", "Which tools monitor exposure, delays and anomalies, and what happens when a limit is breached?",
     "agents", "Daily exposure report per agent, breach log, escalation procedure, write-offs", "Legal / Treasury"),
    ("treasury_cycle", "5. Treasury and settlement", "When does the customer pay, the agent deposit and the company settle with the partner? How much liquidity is advanced?",
     None, "Dated flows for a sample of weeks: customer cash-in, agent deposit, partner settlement, bank balances", "Treasury"),
    ("reporting", "6. Financial controlling and reporting", "What budget, actuals and variance reporting reaches management, who prepares it and with what effort?",
     None, "The monthly management pack, budget file, hours spent preparing it", "Finance"),
    ("market_data", "6. Financial controlling and reporting", "Which Bank of Italy market data exist, at what frequency and detail, and can they be set against our operations?",
     "mt_volumes", "Bank of Italy remittance statistics by corridor; the internal comparison already attempted", "Finance / Wayland"),
    ("cost_per_tx", "7. Cost per transaction and infrastructure", "What is the cost per transaction, variable against fixed: staff, controls, back office, partners, platforms?",
     "mt_unit_revenue", "Cost ledger by cost centre; partner and platform fees per transaction; allocation keys", "Finance"),
    ("tech_alternatives", "7. Cost per transaction and infrastructure", "Which systems and payment rails are used today and could blockchain or stablecoins help?",
     None, "System list with annual cost; settlement rails used (SWIFT or other) and fees", "IT / Treasury"),
    ("pos_role", "8. Business POS", "What is the company's actual role in the POS chain, and how does a lead become an active merchant?",
     "pos", "Contract with the payment facilitator; sales funnel from enquiry to active merchant; support and fraud handling process", "Commercial / Operations"),
    ("pos_economics", "8. Business POS", "What does it cost to acquire, activate and serve a merchant, and what margin does each leave?",
     "pos", "Merchant list with activation date, volume, fees paid to the facilitator and scheme, support hours", "Finance / Commercial"),
]
TOPIC_BY_ID = {t[0]: t for t in TOPICS}


def _k(v):
    return f"{v / 1000:,.1f} k EUR"


def _m(v):
    return f"{v / 1_000_000:,.1f}M EUR"


def baselines(merged, employees=None):
    """{baseline key: (text, source)} computed only from what the statements print; a missing input gives no entry."""
    if not merged:
        return {}
    out = {}
    year = merged["years"][-1]
    src = f"saved statements, table E.8 and notes, FY{year}"
    by_segment = {r["segment"]: r for r in merged["rows"] if r["direction"] != "incoming"}
    mt, acq = by_segment.get("money_transfer"), by_segment.get("acquiring")
    if mt and mt["years"].get(year):
        v, m = mt["years"][year], ss.metrics(mt, year)
        growth = "" if m.get("operations_growth") is None or m.get("amount_growth") is None else \
            f", operations {m['operations_growth'] * 100:+.1f}% and amount {m['amount_growth'] * 100:+.1f}% on FY{year - 1}"
        ticket = "" if m.get("average_ticket") is None else f", average ticket {m['average_ticket']:,.0f} EUR"
        out["mt_volumes"] = (f"FY{year}: {v['operations']:,.0f} outgoing operations, {_m(v['amount'])}{ticket}{growth}. "
                             "No split by channel or corridor is printed.", src)
        rates = [(y, ss.metrics(mt, y).get("take_rate")) for y in merged["years"]]
        rates = [(y, t) for y, t in rates if t]
        if rates:
            out["mt_pricing"] = ("Commissions received / amount: " + ", ".join(f"FY{y} {t * 100:.2f}%" for y, t in rates)
                                 + ". A 16% margin is not visible here; the exchange-rate result is a separate income line.", src)
        if m.get("commission_per_operation") is not None:
            pos_op = acq["years"].get(year) if acq else None
            out["mt_unit_revenue"] = (f"Revenue side only: {m['commission_per_operation']:,.2f} EUR commission per money transfer operation in FY{year}"
                                      + (f", {pos_op['commission'] / pos_op['operations']:,.2f} EUR per POS operation" if pos_op and pos_op["operations"] else "")
                                      + ". The cost side is not in the statements.", src)
    if acq and acq["years"].get(year):
        v, m, share = acq["years"][year], ss.metrics(acq, year), ss.shares(merged, year).get("acquiring")
        rate = "" if m.get("take_rate") is None else f", take rate {m['take_rate'] * 100:.2f}%"
        out["pos"] = (f"FY{year}: {v['operations']:,.0f} operations, {_m(v['amount'])}, commissions {_k(v['commission'])}"
                      + ("" if share is None else f" ({share * 100:.1f}% of commissions)")
                      + ("" if m.get("operations_growth") is None else f", operations {m['operations_growth'] * 100:+.1f}%")
                      + rate + ".", src)
    receivables, payables = merged["agents"].get("agent_receivables", {}), merged["agents"].get("agent_payables", {})
    if receivables.get(year) is not None:
        fund = (merged["fund"] or {}).get(year)
        out["agents"] = (f"Receivables from agents {_k(receivables[year])} at FY{year}"
                         + (f" ({_k(receivables[year - 1])} at FY{year - 1})" if receivables.get(year - 1) is not None else "")
                         + ("" if payables.get(year) is None else f"; payables to agents {_k(payables[year])}")
                         + ("" if not fund else f"; protection fund {_k(fund['money_transfer'])} money transfer, {_k(fund['acquiring'])} acquiring")
                         + ". The statement names agent misappropriation as the main risk.", src)
    if employees and employees > 0:
        ops = sum(r["years"][year]["operations"] for r in by_segment.values() if r["years"].get(year))
        if ops:
            out["ops_per_employee"] = (f"{ops:,.0f} operations in FY{year} over about {employees:,.0f} employees is {ops / employees:,.0f} operations per employee "
                                       "(an average: it says nothing about time per order or about complexity).", src + " and the employee count on file")
    return out


def load(db, company_id):
    from models import RawImportRecord
    row = db.query(RawImportRecord).filter_by(company_id=company_id, dataset_name=DATASET).first()
    return dict((row.raw_row or {}).get("items") or {}) if row else {}


def save(db, company_id, topic_id, status, holder, note, author):
    """One topic's progress; the other topics in the record are re-read inside the transaction and kept."""
    from models import RawImportRecord
    if topic_id not in TOPIC_BY_ID:
        raise ValueError("Unknown topic.")
    if status not in STATUSES:
        raise ValueError("Unknown status.")
    row = db.query(RawImportRecord).filter_by(company_id=company_id, dataset_name=DATASET).first()
    if row is None:
        row = RawImportRecord(company_id=company_id, dataset_name=DATASET, source_filename="team checklist", source_row_index=1, raw_row={"items": {}})
        db.add(row)
    items = dict((row.raw_row or {}).get("items") or {})
    items[topic_id] = dict(status=status, holder=(holder or "").strip(), note=(note or "").strip(),
                           updated_by=(author or "").strip() or "unknown", updated_at=datetime.utcnow().isoformat(timespec="seconds"))
    row.raw_row = {"items": items}  # a new dict: the JSON column must see a change
    row.updated_at = datetime.utcnow()
    db.commit()
    return items[topic_id]


def progress(items):
    done = sum(1 for t in TOPICS if (items.get(t[0]) or {}).get("status") == "Received")
    return done, len(TOPICS)
