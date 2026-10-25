# Project Vienna: the Diagnosis Masterplan

*From a list of 953 companies to a prepared first call.* Written 2026-10-23 as a senior analyst's review of what the tool
knows, what it does not, and how to close the gap with several agents working at once.

Companion files: [DIAGNOSIS_TICKETS.md](DIAGNOSIS_TICKETS.md) (the protocol, the ground rules and the 38 tickets),
[DIAGNOSIS_AGENT_PROMPT.md](DIAGNOSIS_AGENT_PROMPT.md) (the one prompt to paste into every agent),
`docs/diagnosis/board.json` (who holds what), `scripts/board.py` (check in / check out), `diagnosis/` (the code skeleton).

---

## 0. In one page

**The ask.** A tool that takes everything gathered by the crawlers, the uploaded data and the pain-point engine and produces a
*diagnosis*: what this company probably needs, why now, what to pitch, what to ask, and what we still do not know. Thorough
enough to open a first call well prepared. Not a verdict; the call completes it.

**What exists.** A strong *financial* picture (three years of accounts for all 953 companies, peer cost benchmarks, ownership,
7,967 managers and directors, 17 indicator-backed pain points, an indicative valuation) and a *partial* crawled picture (a third
of the cohort has had its website, jobs, catalogue or directories read; the news matching is about half wrong).

**What is missing, in one sentence.** The tool can say "this company's margin is in the bottom quartile of its peers" but not
"it builds filling lines for the dairy industry, that market is soft, the CEO changed last spring, the Machinery Regulation lands in
January, here is the pilot to bring and the three questions that will tell you if it is right."

**The seven gaps that matter most**, in order: (1) there is no synthesis layer from facts to hypotheses to a call plan;
(2) there is no *business profile* (what they make, for whom, how they sell); (3) the market is not crossed with the product;
(4) the regulation and incentive clock is not modelled; (5) the news and event layer ("why now") is unreliable; (6) there is no
pilot library to pitch from and no startup matching; (7) nothing learns from the calls. Section 9 has all twenty.

**The plan.** A new package `diagnosis/` with a frozen contract (facts, hypotheses, triggers, pilots, call pack), 38 tickets in three
waves, built by agents that check tickets in and out through `scripts/board.py`. Wave 1 (13 tickets) can all start at the same time.
The contract skeleton, the vocabulary of 230+ fact keys and the check-in system are already in the repository and tested.

**What GG gets at the end.** For each company a *Call Pack*: a one-page read, a call plan, and an appendix where every claim points to a
source. For the portfolio, an Origination view: tiers A/B/C/hold/exclude by explicit rules (never a blended score), archetypes, and an
Excel call list. After each call a two-minute debrief that makes the next pack better.

**What GG has to do** (section 13): label 12 golden companies (about 6 hours of an analyst's time), approve live database writes and the
long local runs, decide a few product questions, and pull one extra AIDA export (working capital, R&D, bank debt). Without the labels the
quality is unmeasured; without the AIDA pull the finance diagnosis stays thin.

---

## 1. The job

**Origination.** GG brokers structured, time-boxed innovation pilots between startups and Mittelstand-type companies. The live cohort is
953 Italian machinery makers (NACE C28, 50-99 employees); 48 are shortlisted. Before any call, GG needs to answer five questions:

| # | Question | Today the tool answers it... |
|---|---|---|
| 1 | Is this company worth a call? | Partly: NEED and READINESS scatter, shortlist, flags |
| 2 | What does it probably need? | Partly: 17 pain points, only where indicators exist |
| 3 | Why now? | Weakly: scattered news, no event model |
| 4 | What would we bring, and would they plausibly fund it? | No: one free-text `pilot_angle` per pain point; capacity to pay not modelled |
| 5 | Who do we call, and what do we say and ask? | No |

**Who uses it and how.** An analyst on Monday morning opens the Origination view, filters Tier A, opens the top company's Call Pack, reads
the first page in about two minutes and the plan in ten, calls, and records a two-minute debrief. The pack must be good enough that the
person can *talk*, honest enough that they never say something the data does not support, and short enough that they will read it.

**What "good" looks like.** Preparation under ten minutes; at least two of the top three hypotheses judged right by GG on the golden set;
every claim traceable to a source; no simulated data anywhere; and after twenty real calls, a measured confirmation rate per hypothesis.

---

## 2. What the tool is, and what it is not

### It is
- **A hypothesis engine.** For each company it proposes needs, each with the evidence for it, the evidence against, how sure we are, and
  the one question that would settle it.
- **An evidence ledger.** Every statement is a *Fact* with a source, a date and a grade (A filed/official, B the company's own publication,
  C a third party, D inference). Unknown, confirmed-absent and simulated are three different things and never mixed.
- **A cross-referencer.** It crosses the company's financials with its peers, its products with market news and association statistics, its
  size and ownership with the rules and incentives that apply, and its history with dated events.
- **A call preparation system.** It ends in a pack: the headline, why now, three needs, the pilot to bring, the questions to ask, what we do
  not know, who to call, what not to say.
- **A learning loop.** Debriefs turn calls into calibrated thresholds.
- **Honest by construction.** It reports `not assessable` when data is missing and says which crawl or question would fix it.

### It is not
| It is not | Because |
|---|---|
| A verdict or a score of how good a company is | Every output is a hypothesis to test on the call. NEED and READINESS stay separate and are never blended with anything |
| A replacement for the call | It prepares the call. Order backlog, utilisation, customer concentration, pricing, IT landscape, owner intentions and budget cannot be seen from outside (the "T3" indicators) |
| An audit, a valuation opinion or legal/tax advice | The valuation is an indicative range; the regulatory clock is screening and carries `legal_review: none` |
| A predictor of pilot success | There are zero pilot outcomes. Nothing is fitted until roughly 10-15 scored pilots exist |
| A contact-harvesting tool | Business contact data the company publishes, nothing personal, no LinkedIn scraping (off by policy) |
| A CRM | It records debriefs, not a pipeline. Export the call list to the CRM GG uses |
| Real-time | Facts carry dates and go stale; the portfolio is refreshed on a schedule |
| Sector- or country-neutral out of the box | It is built for Italian machinery first; other sectors need a playbook (D04) and a regulatory table (D05). The design allows it; the data does not exist yet |
| A startup marketplace | It ends in pilot *concepts*. Matching to GG's startups needs GG's roster (D31) |

### Ten principles (the ground rules, short form)
1. Unknown is not absent. 2. Simulated is not evidence. 3. Every fact has a grade, a source and a date. 4. A model may classify and phrase;
it may not invent a number, a name or a date. 5. Peer-relative before absolute. 6. One producer per key; the diagnosis writes Facts and its own
tables, never a SignalRecord or a weight. 7. NEED and READINESS are never blended; neither is anything else. 8. Say what we do not know, and how
to find out. 9. Never say on a call what the data cannot support (and never an individual's age). 10. Measure against a human before trusting.

---

## 3. What we know today

Snapshot of the live database, read-only, 2026-10-23.

### The cohort
953 companies, all Italian, all `SME`, headcount 50-99 (mean 70), 937 with a website, 48 shortlisted and 905 candidates. Regions: Lombardia 336,
Emilia-Romagna 187, Veneto 169, Piemonte 96, Toscana 42, Friuli-Venezia Giulia 28. Largest ATECO codes: 289300 (97), 282999 (74), 282500 (63),
282930 (63), 281400 (62), 284100 (37), 282209 (37), 289600 (36), 283090 (35), 289999 (31). Zero parent links recorded.

### Layers, what they hold, how far to trust them

| Layer | Coverage (of 953) | Grade | Trust today | Used for |
|---|---|---|---|---|
| AIDA financial statements (3 years: revenue, EBITDA, EBIT, net income, assets, equity, cash, personnel, materials, capex...) | 953 | A | High; units and shifted columns audited in September | Financial need, peers, valuation |
| AIDA cost breakdown (nine lines, year-matched) | 922 | A | High | Cost structure |
| Ownership, group size, independence class, shareholders | 953 | A | High; `own.type` not derived yet | Decision-maker, succession |
| Managers and directors (roles, ages, tenure, appointments, resignations) | 7,967 people | A | High (age 91%, tenure 86%) | People hypotheses, reach |
| 17 pain points over real indicators | all, with evidence caps | A | Good; thresholds set against the real portfolio | Seed of the hypothesis layer |
| NEED / READINESS scores, completeness | 953 | derived | Context only | Tiering context |
| Valuation (multiples + DCF, withheld on weak inputs) | most | derived | Indicative | Context |
| Sector growth and export exposure (Eurostat) | 710 / 701 | A | Good; sector-level, not company | Market context |
| Own-site crawls: website 263, jobs 301, digital maturity 290, catalogue 173 | 18-32% | B | Mixed; the T03/T15/T17 reliability fixes help; unmeasured precision | What they say, how they hire, how digital they are |
| Directory listings: MECSPE 299, ANIMA 167, Kompass 164 (confirmed only) | 17-31% | C | Reasonable | Product signal, warm routes |
| TED public awards | 158 | A | Good where it hits; misses are not proof | Public customers |
| EU funding / state aid / EPO patents | 693 / 250 real checks | A | EPO had 653 errors in 900 calls; EUIPO is simulated | Innovation activity |
| Wappalyzer technology fingerprint | 848 real | B | Good | Digital footprint |
| Google News RSS headlines | 952 blobs, 50 real signals | C -> D | **Poor**: T25's hand audit found 8 of 15 derived rows about someone else | Needs D17 before use |
| Reviews (Google, Trustpilot, Glassdoor, Kununu) | 8 / 8 / 3 / 3 | C | Negligible coverage; modes off | None yet |
| Named competitors | 3 | n/a | Hand-curated by design | Competitor benchmarks (2 companies) |
| Own-site evidence reader (local model) | 10 (pilot) | B | High precision by construction; full run not started | Partners, universities, export claims |
| AI evidence brief | 2 companies | D | Citation-bound, on demand | Reading aid |
| Pilot outcomes | **0** | | | Calibration (impossible yet) |
| Simulated placeholders | about 50,000 signal rows | none | Must never be read as evidence | Nothing |

### The honest summary
The financial and governance side is strong and trustworthy. The *commercial, operational, market and timing* side is thin, partly
because most crawlers have touched only a third of the cohort, and partly because no crawler asks the questions a diagnosis needs. What the
company makes, who buys it, how it sells and whether its machines are connected are not collected at all.

---

## 4. How a senior analyst would diagnose a Mittelstand machinery company

A first-screen diagnosis in consulting practice asks twelve questions, in the order that gets to a conversation fastest. For each, what
the tool has and what it lacks:

| Factor | The question | What decides it | Have | Lacking |
|---|---|---|---|---|
| **Sector and sub-sector** | Which business is this really? Food-processing lines, valves, packaging, refrigeration, metal forming? | ATECO 6-digit, playbook | ATECO code | Playbooks (what each code makes, for whom, cycles, pains) |
| **Product and end-markets** | What do they make and who buys it? Engineer-to-order or catalogue? Aftermarket? | Own site, catalogue, directories | Product lines for 263; catalogue for 173 | A structured profile for all (D11) |
| **Market, crossed with product** | Is *their* market growing, and what is moving it: orders, regulation, tariffs, input costs, customer capex? | Association stats, news, series | Eurostat sector growth, raw news | Radar by sub-sector and end-market; the cross (D14, D15) |
| **Size and capacity to pay** | Can they fund a EUR 30-100k trial without strain? | Revenue, margin, cash, debt, capex | All of it | A model of capacity to pay (D20) |
| **Performance against peers** | Is the margin, growth, productivity, cost mix, stock and receivables ordinary or an outlier? | Peer percentiles | Cost-line benchmarks only | Peer engine for every ratio; working-capital fields (D03, D09) |
| **Ownership and decision** | Who decides, how fast, what do they want? Founder, second generation, fund, group? Is a succession coming? | Shareholders, roster, tenure | Raw ownership, ages | Ownership typing, owner-manager and successor logic (D01, D20) |
| **Geography and cluster** | District effects: Packaging Valley, Food Valley, the ceramic and wood districts | Province + code | Region, province | District mapping (D04) |
| **Competition** | Who else sells this, and are they ahead digitally or on product? | Named peers, catalogues | 3 manual competitors | Suggested peers, human-confirmed (D32) |
| **Operations and engineering** | Lead time, WIP, skills, systems, quality | Inventory change, hiring, ERP mentions | Cost breakdown, jobs for 301 | Days inventory/receivables; profile of production (D09, D11) |
| **Product and digital model** | Connected machines? Service and spares? Configurator? | Site, profile, tech | Tech stack 848, maturity 290 | IIoT/remote service evidence (D11) |
| **Regulation, risk, sustainability** | What lands in the next 12-18 months that they must answer? | Machinery Reg., CRA, NIS2, AI Act, CBAM, incentives | One blocked indicator | A dated clock with applicability (D05) |
| **Timing** | Why this quarter? New CEO, new accounts, a grant, an investment, a deadline, a market shock | Dated events | Roster dates, grant dates, TED dates | The event engine and a news verifier (D16, D17) |

Two things sit across all twelve: *evidence quality* (how sure) and *reachability* (who, how, which door). Neither exists as a first-class
concept today.

---

## 5. The diagnosis model

### 5.1 Pipeline

```
 existing data  --readers-->  FACTS (graded, dated, sourced)  --derivers--> peers, capacity to pay, reach, market exposure
                                         |
        regulatory clock, market radar,  |        events (verified news, roster, grants, tenders, deadlines)
        sector playbooks, pilot library  v                       |
                                  HYPOTHESES (issue tree) <------+
                                         |
                       status, strength, confidence, unknowns
                                         |
              pilot matching  +  archetype  +  call tier (explicit rules)
                                         |
                    synthesis (cited, red-teamed, fallback)  -->  CALL PACK  -->  call  -->  DEBRIEF --> calibration
```

### 5.2 The issue tree
Seven branches, mutually exclusive, together covering what an Italian machinery SME can be short of:

| Branch | Code | The question |
|---|---|---|
| Commercial & growth | COM | Are they winning orders, in the right markets, with the right reach, keeping the installed base? |
| Profitability & cost | PRF | Is margin lost to inputs, overhead, outsourcing, energy, quality? |
| Operations & engineering | OPS | Do orders flow from quote to delivery without waste, with the skills and systems needed? |
| Product & service model | PRD | Are the machines differentiated, connected, serviceable and ready for what customers will ask? |
| Finance & resilience | FIN | Can they fund change, and is working capital or debt in the way? |
| People & organisation | PPL | Who decides, is leadership changing, is the team stable? |
| Regulation, risk & sustainability | REG | What must they answer in the next 12-18 months, and what is subsidised? |

### 5.3 What a hypothesis is
A claim stated the way it would be said on a call, with: the facts it needs (`requires`), the facts that sharpen it (`helpful`), what would
disprove it (`disconfirm`), the pilots it points to (`pilot_tags`), the questions that test it. Evaluating it for a company yields a
`HypothesisResult`:

- **status**: `supported` (direct evidence of grade A/B or two independent C, nothing contradicting, at least 60% of required facts known),
  `plausible`, `weak`, `contradicted`, `not_assessable`;
- **strength** 0-100: how strongly the evidence points at the problem. It is *not* a NEED score;
- **confidence** high / medium / low: from the best evidence grade, the number of independent sources, freshness, the share of required facts
  known, contradictions, and (later) the measured reliability of each crawled layer;
- the **evidence** list (each item quotes the value and the comparison), the **missing** facts, and the **coverage**.

A claim that rests on grade-D inference alone is never above `weak`. A hypothesis whose facts are unknown says so: that is a *collection
list*, not a failure. Thresholds are relative to peers (`peer.*.pctile`) wherever possible and are calibrated against the live distribution:
a hypothesis supported for more than 40% of the cohort does not discriminate and must be tightened.

### 5.4 Dimensions, never blended
A company is described on separate dimensions. They are shown side by side; none is summed into another.

| Dimension | What it says | Source |
|---|---|---|
| NEED | structural gap a pilot could close | existing score, context only |
| READINESS | budget, champion, short approval chain, openness | existing score, context only |
| Evidence of need | supported/plausible hypotheses and their grades | the engine |
| Timing | verified events and deadlines, with recency | the event engine, regulatory clock |
| Reachability | decision-maker found, a published channel, a warm route | reach |
| Pilot fit | a concept that matches, with its prerequisites met or askable, within capacity to pay | pilot matcher, `pay.*` |
| Evidence quality | share of the wanted facts known, freshness, layer reliability | the engine |

### 5.5 The call tier (rules, not arithmetic)
| Tier | Meaning | Default rule (thresholds are editable in `diagnosis/data/tiering.json`) |
|---|---|---|
| **A** call now | worth a senior person's hour | at least two supported/plausible hypotheses with grade A/B evidence, one matched pilot with prerequisites askable, one opening event or deadline within 12 months, a decision-maker identified, capacity to pay not "low", evidence quality above the floor, no exclusion |
| **B** warm | good, one dimension short | as A but missing timing, or reach, or one hypothesis |
| **C** nurture | plausible, not urgent | evidence of need but no fit or no timing; keep monitoring |
| **HOLD** enrich first | cannot tell | evidence quality below the floor: the pack names the crawl or fact that would unlock a decision |
| **EXCLUDE** | do not call | open distress procedure, dissolved, subsidiary whose parent decides, or a recorded "no" |

Every tier comes with `tier_reasons` in plain words. A company moves tier when its facts change and says why.

---

## 6. Hypothesis library v0

Thirty-nine seed hypotheses (IDs reserved, implemented by D18-D21). This is the part of the plan GG should read most closely: it is the
tool's idea of what a machinery SME can be short of. Edit it before the build, not after. `fact keys` are in `diagnosis/factkeys.py`;
pilot tags are in `DIAGNOSIS_TICKETS.md` Part 6. Wording is how it would be said on a call; thresholds are starting points to calibrate.

### Commercial & growth (COM)
| ID | Hypothesis | Signature evidence | Would contradict | Pilot tags | Key question |
|---|---|---|---|---|---|
| COM-01 | Growing slower than comparable builders: company-specific, not market-wide | `peer.revenue_growth_pct.pctile` low; `fin.revenue_trend_pct` negative; sector not falling | Peers fall too (then COM-05) | quote_automation, export_leadgen | "How do orders compare with a year ago, and where are you losing them?" |
| COM-02 | Quoting and order intake slow or manual for configured machines | `prof.business_model` ETO/CTO; WIP build (`fin.inventory_change_k`); low revenue per employee; sales-engineer vacancies | Series production, catalogue model | quote_automation, design_automation, configurator | "From a customer request to a priced offer: how long, and how many never become offers?" |
| COM-03 | Export reach narrower than the sector's | `mkt.export_exposure` high; `prof.export_markets` <= 2; single-language site | Already broad | export_leadgen | "Which three countries would you most like to sell in, and what stops you?" |
| COM-04 | The installed base is an under-used revenue line (spares, service, retrofit) | `prof.has_aftermarket` unclear/absent; no parts portal; no remote service; old company | Strong service business | spare_parts_portal, remote_service, predictive_maintenance, servitization | "What share of revenue is spares and service, and how does a customer order a part today?" |
| COM-05 | Dependent on one or two end-markets that are currently weak | `prof.end_markets` <= 2 and `mkt.headwinds` on them; negative `mkt.orders_index` | Diversified, or the market is turning | export_leadgen, open_innovation | "If your biggest customer industry paused for a year, what would you do?" |
| COM-06 | Prices not keeping up with costs: margin lost while sales hold | `fin.margin_compression_pp` high; revenue flat; materials share high; `mkt.input_cost_trend` up | Costs fell | quote_automation, procurement_analytics | "How often do you update prices, and what share of a cost rise reaches the customer?" |

### Profitability & cost (PRF)
| ID | Hypothesis | Signature evidence | Would contradict | Pilot tags | Key question |
|---|---|---|---|---|---|
| PRF-01 | Materials take a larger and growing share of revenue than peers | `peer.materials_pct_revenue.pctile` >= 75; `pp.input_cost_pressure` | A mix explained by the playbook | procurement_analytics, design_automation | "Which items drive your bill of materials, and who negotiates them?" |
| PRF-02 | Personnel cost is heavy for what each employee produces | personnel share pctile high and revenue per employee pctile low | High-value engineering firm with strong margin | scheduling_mes, back_office_ai, robotics_cell | "Where do skilled people spend time on work that is neither engineering nor assembly?" |
| PRF-03 | Heavy outsourcing: purchased services unusually large | `peer.service_costs_pct_revenue.pctile` >= 80 | Deliberate asset-light model | supply_chain_visibility, procurement_analytics | "What do you outsource, and would you bring any of it in or re-bid it?" |
| PRF-04 | Energy is a material cost with an incentive window to act | energy-intensive process (playbook), `reg.incentives` open | Energy negligible | energy_monitoring, green_redesign | "Do you meter energy by line, and what did your bills do over 2022-25?" |
| PRF-05 | Quality or warranty cost is leaking margin | low `rev.rating`, recalls; mostly call-validated | Clean record | warranty_analytics, vision_inspection | "What did warranty and rework cost last year, and where do defects start?" |
| PRF-06 | Operating margin low versus peers with no sign of a temporary dip | `peer.ebitda_margin_pct.pctile` <= 25 three years running; `pp.profit_erosion` | One-off year | scheduling_mes, procurement_analytics, quote_automation | "Which product lines earn money? Do you know margin by order?" |

### Operations & engineering (OPS)
| ID | Hypothesis | Signature evidence | Would contradict | Pilot tags | Key question |
|---|---|---|---|---|---|
| OPS-01 | Engineering-to-delivery cycle long: WIP and lead time tie up cash | inventory build large vs revenue; `fin.dio_days` high (planned); ETO; engineer vacancies | Short, stable cycle | digital_twin, design_automation, scheduling_mes | "From order to shipment, where do orders wait longest?" |
| OPS-02 | Skilled-labour bottleneck: roles stay open or know-how sits with few people | persistent technical vacancies; ageing management; flat headcount | No open roles | talent_platform, knowledge_capture, robotics_cell | "Which roles are hardest to fill, and what happens to delivery when they stay open?" |
| OPS-03 | Legacy or fragmented systems | legacy ERP mentions; low tech-stack score; `pp.legacy_systems` | Modern stack evident | scheduling_mes, back_office_ai, machine_data_platform | "How do the office and the shop floor share data today?" |
| OPS-04 | Supply chain is a fragility | high materials share; trade/supply headwinds; mostly call-validated | Diversified, buffered | supply_chain_visibility, procurement_analytics | "Which parts are single-source or over twelve weeks?" |
| OPS-05 | Own assets sweated: low capex, ageing equipment | `peer.capex_pct_revenue.pctile` <= 25; `pp.underinvestment` | Recent big investment | predictive_maintenance, robotics_cell, vision_inspection | "How old is your oldest critical machine, and what does an hour of downtime cost?" |

### Product & service model (PRD)
| ID | Hypothesis | Signature evidence | Would contradict | Pilot tags | Key question |
|---|---|---|---|---|---|
| PRD-01 | Machines are not connected; no data-based service | `dig.iiot_signals` absent; no in-house software; connected peers | Already offers monitoring | predictive_maintenance, machine_data_platform, servitization, remote_service | "Can a customer see how their machine is running, remotely, today?" |
| PRD-02 | Products look commoditised | `prod.differentiation_score` high (with confirmed peers); large priced catalogue | Distinct niche | configurator, servitization, open_innovation | "What do customers say you do that nobody else does?" |
| PRD-03 | Thin innovation capacity: little R&D, few patents, no research partners | zero patent families on a real check; low intangibles; `prof.rd_claimed` false | R&D visible | open_innovation, design_automation | "Who works on the next product generation, and with whom?" |
| PRD-04 | Customers will ask for efficient, lower-carbon machines; no visible answer | no sustainability claims; `reg.esg_pull`; efficiency tailwinds | Strong claims | green_redesign, esg_carbon | "Do customers ask you for energy or carbon data about your machines?" |
| PRD-05 | The digital shop window lags | old site, low maturity score, no online catalogue or configurator | Strong web presence | configurator, export_leadgen | "How do new customers find you, and what do they see?" |

### Finance & resilience (FIN)
| ID | Hypothesis | Signature evidence | Would contradict | Pilot tags | Key question |
|---|---|---|---|---|---|
| FIN-01 | Working capital is a drag | `fin.dso_days`/`dio_days` high (planned); proxy: inventory build, low cash | Efficient cycle | working_capital, inventory_optimisation | "How long do customers take to pay, and how much stock do you hold?" |
| FIN-02 | Debt service leaves little room (a resilience risk; lowers readiness) | `fin.interest_cover_x` < 3, `fin.leverage_x` > 4; `pp.debt_service_strain` | Cover comfortable | working_capital, incentives_copilot | "How is the financing arranged, and what are the covenants?" |
| FIN-03 | Cash is thin: any pilot must be near cost-neutral or incentive-funded | `fin.cash_pct_revenue` <= 2; `pay.affordability` low | Credit lines undisclosed | incentives_copilot | "Is there a budget line for trials, or does it need a business case?" |
| FIN-04 | **(opportunity)** The means exist and the company has not been investing | equity ratio >= 40%, cash >= 8% of revenue, capex pctile low, profitable, incentive open | Plans already funded | incentives_copilot, predictive_maintenance | "What is the investment plan for the next two years, and what is holding it?" |
| FIN-05 | Distress: open procedure, or negative equity with losses | `fin.distress_procedure`; equity < 0; two losses | none: this excludes | (none) | not asked: tier EXCLUDE |
| FIN-06 | Profitable but shrinking: cost-cutting has not stopped the erosion | margin above median, revenue trend <= -10%, headcount falling | Planned downsizing | quote_automation, export_leadgen | "What is the plan to return to growth?" |

### People & organisation (PPL)
| ID | Hypothesis | Signature evidence | Would contradict | Pilot tags | Key question |
|---|---|---|---|---|---|
| PPL-01 | Succession horizon: the person who decides is near retirement, no visible successor | `mgmt.ceo_age` high, `own.type` family, no younger generation | Successor in place | knowledge_capture, back_office_ai | "How do you see the company's leadership in five years?" (never cite the age) |
| PPL-02 | A leadership change in the last 12 months opened a window | `trg.leadership_change_12m`; tenure under a year | Change was internal and cosmetic | any (timing) | "What did you want to change when you took over?" |
| PPL-03 | Decisions sit with one person: fast yes/no, single point of failure | board of one; owner-manager | Collegial board | (approach, not pilot) | "Who decides on a trial like this, and how fast?" |
| PPL-04 | The team is eroding | falling headcount; high management turnover; poor employer ratings | Stable | talent_platform, knowledge_capture | "What changed in the team over the last two years?" |
| PPL-05 | Governance is informal in a company that is changing | no independent director or adviser, ownership change or growth | Formal board | open_innovation | "Who do you take advice from on strategy?" |

### Regulation, risk & sustainability (REG)
| ID | Hypothesis | Signature evidence | Would contradict | Pilot tags | Key question |
|---|---|---|---|---|---|
| REG-01 | Machinery Regulation (EU) 2023/1230, from 20 Jan 2027, forces the technical file, risk assessment and CE process to be redone; no visible preparation | `reg.machinery_regulation`; no compliance role or certification mention | Preparation visible | compliance_docs | "Have you mapped your machines against the new Regulation, and who owns it?" |
| REG-02 | Connected products fall under the Cyber Resilience Act; no product-security process visible | `reg.cra_exposure`; `prof.software_in_product` | No digital elements | product_cybersecurity | "Do your machines ship with software or connectivity, and who maintains it?" |
| REG-03 | NIS2 may apply as a medium manufacturer; OT security looks unaddressed | `reg.nis2_scope` in-scope-if; thin IT footprint | Out of scope or addressed | ot_cybersecurity | "Has your sector's cyber obligation reached you, and who is responsible?" |
| REG-04 | AI Act obligations may touch AI in the product or in HR/operations | `reg.ai_act_exposure` | No AI | compliance_docs | "Do your products or processes use AI today?" |
| REG-05 | Customers push ESG and carbon data requests down the chain; nothing visible | `reg.esg_pull`, `reg.cbam_exposure`, no sustainability report | Reporting exists | esg_carbon | "Which customers ask for sustainability data, and how do you answer?" |
| REG-06 | **(opportunity)** An incentive window is open (iperammortamento 2026-2028) and needs lead time | `reg.incentives`; capacity to pay not low | Not eligible | incentives_copilot | "Have you reserved the incentive for planned investments?" |

---

## 7. Company archetypes v0

Archetypes are the pitch templates: a company's hypothesis profile decides which story fits it. Assigned by explicit rules (D27); a
company may have a primary and a secondary.

| Archetype | Profile (hypotheses) | What to bring | Care |
|---|---|---|---|
| **A1 Cash-rich family OEM, under-investing** | FIN-04, PPL-03, OPS-05, REG-06 | Incentive-assisted modernisation pilots with a clear payback | Owner decides quickly; respect the family relationship |
| **A2 Squeezed series maker** | PRF-01, PRF-06, COM-06, PRD-02 | Cost-out and pricing pilots with fast payback | Check capacity to pay before selling |
| **A3 Engineer-to-order, bottlenecked** | COM-02, OPS-01, OPS-02 | Quoting, design automation, scheduling | Pilot must not disturb live orders |
| **A4 Aftermarket sleeper** | COM-04, PRD-01 | Spares portal, remote service, connected retrofit | Needs a champion in service |
| **A5 Succession in motion** | PPL-01, PPL-02, OPS-02 | Knowledge capture, digitisation of tacit know-how | Sensitive: lead with the business, never the person's age |
| **A6 Compliance-clock exposed** | REG-01, REG-02, REG-03 | Technical-file, product-security and OT pilots, co-financed | Screening only; no legal claims |
| **A7 Stretched balance sheet** | FIN-02, FIN-03 | Almost nothing now; incentive-funded or zero-cost trials | Usually HOLD or EXCLUDE; lowers readiness |
| **A8 Digital explorer** | high readiness, few needs, PRD-03 | Co-innovation and open-innovation pilots | Low need: risk of an interesting conversation with no pilot |
| **A9 Market-shock victim** | COM-05 with headwinds | Diversification and export pilots | Be honest that the market, not the company, is the cause |

---

## 8. The Call Pack

The deliverable. Page 1 is the 60-second read, page 2 the plan, then an appendix where every claim points to a source.

**Page 1: the read.**
- *Header:* name, ATECO and what they make (from the profile), location and district, staff and revenue band, ownership type, tier and
  archetype, language to open in, who to call (role, not age).
- *In one sentence:* who they are and why we might matter.
- *Five numbers that matter*, each with its peer percentile and the peer group's size.
- *Why now:* up to three dated events or deadlines, each with its source.
- *The three needs:* for each, the statement; two or three evidence bullets with a grade and a footnote; confidence; **what would prove it
  wrong**; the one pilot to bring; the one question to ask.
- *What we do not know:* the five unknowns that would change the picture most, why each matters, and how to find out (a crawl, or a question).

**Page 2: the plan.**
- *Opening* (30 seconds, in the right language and persona).
- *Discovery questions* in order, with what a strong and a weak answer reveal.
- *The pilot to propose* if a need is confirmed: scope, duration, budget band, what it measures, what the company needs to provide.
- *Qualification checklist:* budget, authority, need, timing, data access, champion, IT constraints, earlier trials.
- *Likely objections* with replies. *Next step to ask for.* *Do not say* box (no unsourced figure, no age, no implied knowledge we do not have).

**Appendix.** Three-year financial snapshot against peers; market context (three items with dates); people and ownership (roles only);
the regulatory clock that applies; the evidence list (`E1...`: grade, source, date, URL); data coverage and layer reliability; the versions of
the engine, rubric, playbook and regulatory table that produced it.

**Illustration of two evidence-bound lines (fictional company, for layout only):**

> **Need 1. Margin squeezed by input costs** *(supported, confidence medium)*
> Materials are 61% of revenue, 88th percentile of 62 peers in ATECO 28.93 [E3, filed accounts FY2025, grade A]; gross margin fell 4.2 points
> over the last three years [E4, A]; steel prices are up on the year [E9, World Bank series, C]. **Could be wrong if:** the mix is simply more
> bought-in components than peers (ask). **Bring:** procurement analytics / should-cost pilot (8-12 weeks, EUR 20-50k, KPI: cost per
> standard part). **Ask:** "Which items drive your bill of materials, and who negotiates them?"

> **Why now.** Machinery Regulation applies from 20 January 2027 [E12, EUR-Lex]; accounts for FY2025 filed in September [E1]; new managing
> director since March [E6, registry, A].

---

## 9. Gap analysis: what is missing, and how to fill it

Ranked by what each gap costs the diagnosis. "GG" marks gaps that need a human decision, data or approval.

| # | Gap | Why it matters | Today | How we fill it | Ticket | Effort | GG? |
|---|---|---|---|---|---|---|---|
| 1 | **No synthesis layer**: facts exist, hypotheses, pilots and a call plan do not | Without it the tool lists data instead of preparing a call | Pain points + flags + a generic AI brief | Fact base, hypothesis engine, 39 rules, pilot matcher, synthesis, call pack | D01, D02, D12, D18-D22, D26, D28 | L | review §6 |
| 2 | **No business profile** (what they make, for whom, ETO or series, aftermarket, export markets, connected?) | The same margin means different things for a valve maker and a filling-line builder; no pitch without it | Product lines for 263, catalogue for 173 | Local-model reader with verbatim-quote checks (the T25 method), pilot of 20, then full run | D11, D33 | L | approve full run |
| 3 | **Market not crossed with product** | "Soft market" is a reason for a call, and the right pilot depends on it | Eurostat sector growth; unverified headlines | Sector playbooks, market radar (association stats, news, prices), exposure mapper | D04, D14, D15 | L | |
| 4 | **Regulation and incentives not modelled** | The strongest "why now" for the whole cohort: Machinery Regulation 20 Jan 2027, CRA, NIS2, iperammortamento to Sep 2028 | One blocked indicator | Dated, source-linked clock with applicability rules; legal review before any score | D05, D21 | M | legal review |
| 5 | **Peer-relative view only for cost lines** | Every threshold needs a peer; absolute rules mislead | `ateco_cost_benchmarks` | Peer engine for every ratio, widening groups | D03 | L | |
| 6 | **Working capital, R&D, bank debt, export share not imported** | Finance and operations hypotheses cannot be tested; the biggest data hole | Not in the six AIDA exports | Shopping list, one extra AIDA pull, importer | D09, D35 | M | **do the pull** |
| 7 | **"Why now" is unreliable** | Timing separates tier A from tier C | Headline regex, about half wrong | Event engine; news verifier with entity checks and a closed event taxonomy | D16, D17 | L | |
| 8 | **No pilot library, no startup matching** | The pack must end in something GG can propose | `pilot_angle` text | 30 sourced pilot concepts, matcher with prerequisites and budget; roster import later | D06, D22, D31 | L | provide roster |
| 9 | **No reachability** | A great diagnosis with no way in is wasted | Roster | Decision-maker logic, published business channels, warm routes | D23 | M | |
| 10 | **Crawler coverage about 30%** | Everything crawled is missing for two thirds of the cohort | Sweeps exist, not drained | Run the existing sweep and the site-evidence reader to completion | D13 | M | approve runs |
| 11 | **Crawled-layer reliability unmeasured** | Unknown precision means unknown confidence | Spot audits only | Truth audit: precision per layer, feeds confidence | D25 | L | review sheet |
| 12 | **No call scripts or question bank** | Preparation ends at the facts | None | Questions per hypothesis, openers per persona, objections, do-not-say | D07 | M | review tone |
| 13 | **No feedback loop** (zero pilot outcomes, no debriefs) | The tool cannot improve | `PilotOutcome` table, empty | Debrief form, per-hypothesis precision, threshold suggestions | D24 | L | use it |
| 14 | **No comparison with a human analyst** | Quality is a guess | None | Golden set of 12, harness, thresholds | D08 | M | **label 12** |
| 15 | **Free sources unused** (members of associations, fair exhibitors, certifications, EU projects, innovative-SME lists) | Cheap evidence for product, innovation and warm routes | None | Feasibility study, then build the best three | D10, D34 | M-L | |
| 16 | **Competitor context** (3 manual rows) | PRD and COM hypotheses want named peers | 3 | Suggestions from the cohort, confirmed by a human | D32 | M | confirm |
| 17 | **Group and parent links** (0 recorded) | A subsidiary's parent decides | AIDA shareholders unused for this | `own.type`, `own.parent_name` from AIDA; discovery crawler for the rest | D01 | S | |
| 18 | **Unknowable before the call** (backlog, utilisation, concentration, pricing, IT, intent, budget) | The call is where these are learned | Eleven "T3" indicators | The pack turns each into a question and records the answer as a fact | D07, D24, D28 | n/a | |
| 19 | **No freshness policy** | Stale facts mislead | Per-indicator freshness exists | Every fact has `as_of`; confidence decays; portfolio re-run by fingerprint | D12, D29 | S | |
| 20 | **Other sectors and countries** | Growth beyond C28 Italy | Country map exists | Playbooks and regulatory tables are data, not code | D04, D05 | later | decide |

---

## 10. Architecture

### 10.1 The package
```
diagnosis/
  contracts.py        Fact, CompanyFacts, HypothesisSpec/Result, Trigger, PilotConcept, CallPack     (frozen, versioned)
  factkeys.py         the vocabulary: every key, unit, owner, state (exists | planned)                (frozen, additive)
  registry.py         @reader @deriver @hypothesis @trigger_rule + auto-discovery + build_facts
  models.py           imports diagnosis/models_*.py so database.init_db creates the dx_* tables
  readers/            core_* (D01)  crawled_* (D02)  profile_* (D11)  derive_* (peers, pay, reach, market, regulatory)  extra_* (D35)
  hypotheses/         com_* prf_* ops_* prd_* fin_* ppl_* reg_*   (one module per branch, one function per hypothesis)
  triggers/           events_* (D16)  regulatory (D05)
  engine.py grading.py        runner, rubric, ranking, unknowns                                         (D12)
  peers.py            peer groups and percentiles                                                       (D03)
  sectors.py + data/  playbooks                       regulatory.py + data/   the clock                 pilots.py + data/   the library
  market/ market_exposure.py  radar and the cross
  news_verify.py reach.py pilot_match.py tiering.py archetypes.py synthesis.py callpack.py render/ portfolio.py debrief.py roster.py
  eval/               golden-set harness
```
Adding a reader, hypothesis or trigger means **adding one file** and decorating one function. Nothing central is edited, which is why many
agents can add to the package at the same moment without colliding.

### 10.2 Storage
Existing tables are not changed. New tables, all prefixed `dx_`, created by `init_db` (additive, safe on Neon): peer stats, market items and
series, trigger events, verified news events, diagnoses and call packs, debriefs, hypothesis reviews and fact corrections, startup roster.
Facts themselves are recomputed from the sources (cheap) and cached by *fingerprint*, a hash of the facts: same facts, same output, no
LLM call.

### 10.3 Cost and speed
*Screen* (facts, hypotheses, tier) is deterministic and free: all 953 in minutes. *Diagnose* adds triggers, pilots, reach. *Pack* is the
only stage that calls a language model and runs for tier A/B up to a cap (default 60), with a deterministic fallback. Provider limits on the
free tiers are token-bound, so batches are small and cached. The local model (site profile, 6-12 hours for the cohort) runs on one PC in the
background.

### 10.4 The language-model policy
A model may *classify* (event type, end-market, direction) and *phrase* (the narrative). It may not supply a number, a name, a date or a
legal conclusion. Extracted text must be found verbatim on the page by code. Narrative sentences must cite numbered evidence and every number
must appear in the cited evidence. A second pass tries to break the claims. Without a key the pipeline still produces a complete, plainer pack.

### 10.5 Seams with the existing app
Three, all owned by one ticket (D30): the nav entry, the Call Pack section in the dossier, and the debrief form. The commit `32a7fc9` cut
the sidebar from 12 pages to 9 ("one home per feature"); the Origination view must fold into that, and GG decides where.

---

## 11. The build: waves, agents and the check-in / check-out system

### 11.1 Waves
| Wave | Tickets | What it delivers | Can start |
|---|---|---|---|
| **0** (done) | contracts, 230+ fact keys, registry, board and `board.py`, this plan | the rules of the game | now |
| **1** foundations | D01-D13, R01 | facts for every existing layer, peers, playbooks, regulatory clock, pilot library, question bank, golden set, data shopping list, source study, business profile pilot, engine core, coverage drain | all 13 at once |
| **2** the diagnosis | D14-D26, R02 | market radar and exposure, triggers, verified news, the 39 hypotheses, pilot matcher, reach, debrief, truth audit, synthesis | as wave-1 tickets finish (dependencies enforced) |
| **3** the product | D27-D35, R03 | tiering and archetypes, call pack, portfolio run, Origination UI, roster, competitor suggestions, JS fallback, enrichers, extra-column importer, live pilot on 12 companies | as wave-2 finishes |

Critical path: **D11 profile -> D15 exposure -> D27 tiering -> D28 pack -> D30 UI**. Start D11 first: its full run takes hours of PC time.

### 11.2 How many agents, and when
Launch 8 agents on wave 1 (13 tickets: they will drain the pool as they finish; each agent takes up to three tickets and stops when none is
claimable). Wave 2 takes 8-10 agents, wave 3 five or six. More agents than claimable tickets simply stop. The same prompt is pasted every
time; the board decides who does what.

### 11.3 Check-in / check-out in one paragraph
Each agent runs `python scripts/board.py claim`. The script reads the board from `origin/master`, refuses a ticket that is taken, whose
dependencies are not DONE, or whose declared write scope overlaps one that is held, and writes the claim as an atomic compare-and-swap push:
if two agents claim at once, git lets exactly one win and the other re-reads and takes the next. The claim is the lock on files; `@scraper-repo`,
`@bundle` and `@live-db-write` are exclusive resources. Work happens in the agent's own git worktree on a branch `diag/<ID>-<slug>`; landing
means merging `origin/master` *into* the branch (never rebase, never stash), testing and pushing `HEAD:master`. A heartbeat every hour keeps
the claim alive; a claim silent for eight hours can be taken over. `done` checks that the work and the results note are on `origin/master`
before it releases the dependents. The mechanism is tested, including two agents racing for one ticket (`tests/test_diagnosis_board.py`).

### 11.4 Hot files and who may touch them
`models.py`, `config.py`, `company_service.py`, `indicators.py`, `painpoints.py`, `scoring.py`, the worker and its bundle, and
`docs/CRAWLER_ROADMAP.md` are in no ticket's scope. `app.py`, `views/company_dossier.py` and `dossier_nav.py` belong to D30 alone. The
worker bundle is a binary that cannot be merged: only R02 (or a ticket holding `@bundle`) rebuilds it. The Scraper repo is separate: only
D11 and D33 touch it, one at a time.

### 11.5 What happens between waves (GG)
The release captains (R01, R02, R03) run the full suite, check the contracts, collect seam requests and write an acceptance note. GG reads
it, updates the main checkout (`git pull` in the folder the app runs from), answers the decisions in section 13, and supplies inputs. Agents
never write to the live database without a dry run shown to GG and an explicit yes.

### 11.6 Honest timeline
Rough, assuming 8 agents and GG's inputs arriving on time: wave 1 about a day of wall-clock time plus the D11 profile pilot; wave 2 two to
three days; wave 3 two to three days, then the 12-company live pilot. The long poles are machine time (profile run, coverage drain, market
radar backfill) and human time (labels, AIDA pull, approvals), not coding.

---

## 12. Quality, honesty and learning

**Evidence grades** travel with every fact, and the weakest input sets a derived fact's grade. **The truth audit** (D25) measures each
crawled layer's precision on samples of 40 and feeds it into confidence. **The golden set** (D08) compares the engine with a human analyst on
12 companies; "good enough to show GG" means top-3 overlap of at least 60%, citation validity 100%, zero simulated facts. **The calibration
loop** (D24) records, after every call, whether each hypothesis was confirmed, partly confirmed, refuted or unknown; it reports precision
per hypothesis and per archetype and proposes threshold changes. No model is fitted below ten scored calls.

**Targets.** Preparation time under 10 minutes. Citation validity 100%. Simulated data used: 0. No hypothesis supported for more than 40% of the
cohort without a written reason. After 20 real calls: a confirmation rate per hypothesis and a call-to-meeting rate compared with untargeted outreach.

**Known failure modes the design guards against:** a homonym's news attributed to the company (entity checks, D17); a crawler timeout read as
"no careers page" (unknown vs absent); a simulated placeholder read as a measurement (readers skip them); a model inventing a figure
(citation and number checks, red-team pass); a hypothesis that fires for everyone (base-rate rule); a stale fact shown as current (`as_of`,
decay); over-trust in the pack (it states what it does not know, on page one).

---

## 13. Decisions for GG

| # | Decision | My default if you do not decide |
|---|---|---|
| 1 | Pack language | Italian first, English second |
| 2 | Is there a startup roster, and when can it be shared? | Pilot concepts first (D06), roster later (D31 waits) |
| 3 | Pull the extra AIDA columns (working capital, R&D, bank debt, activity description, 5 years) | Yes: highest-value data decision; D09 prepares the list |
| 4 | Approve live-database writes for D29/R03 and long local runs (D11 full run, D13 drain); which PC | Ask at the moment, with a dry run |
| 5 | Where does the Origination view live in the nine-page navigation? | Replace the Targets entry's landing view; one new dossier section |
| 6 | Tier A target size | A call list of tens, not hundreds (thresholds editable) |
| 7 | Language-model budget and provider order | Existing chain (Groq/Mistral/Gemini), cap 60 packs per refresh |
| 8 | Using directors' names for outreach prep (GDPR, legitimate interest) | Internal preparation only; business channels only; confirm with counsel |
| 9 | Expand beyond C28 Italy now? | No: C28 Italy first; playbooks and regulatory tables are data when you do |
| 10 | Promote any diagnosis output to a scored indicator (for example regulatory exposure, weight 5)? | No; never without your weight decision (ground rule 1) |
| 11 | Who labels the golden set (12 companies, about 30 minutes each) and reviews the truth-audit sheet (about 3 hours)? | Without it, quality is unmeasured and wave 3 is not accepted |
| 12 | Review the hypothesis library (section 6) and archetypes (section 7) before wave 2 starts | Agents implement them as written |

---

## 14. Risks

| Risk | Likelihood / impact | Mitigation |
|---|---|---|
| Data too thin: most hypotheses `not assessable` for most companies | High / medium | That is the correct output and the collection list; D11, D13, D09/D35 are the cure; HOLD tier names what to enrich |
| False positives: confident-sounding wrong claims | Medium / high | Grades, base-rate rule, golden set, truth audit, citation and number checks, "could be wrong if" on every need |
| Over-trust by the analyst | Medium / high | Page one states unknowns; strength and confidence shown separately; no single score |
| Legal: regulation read as advice; directors' data | Medium / medium | `legal_review: none` labels; screening wording; no ages in speakable text; counsel check (decision 8) |
| Agents collide or leave work half-done | Medium / medium | Atomic claims, file locks, worktrees, heartbeat and take-over, results notes, release captains |
| An agent session is sandboxed and cannot write, commit or push (seen on the first launch: a Codex app-managed worktree outside its writable roots) | High / medium | `board.py preflight` before any claim; `claim` refuses a session that cannot write; launch guide lists the access each session needs; the agent hands the ticket back instead of half-working |
| Hot-file conflicts, binary bundle conflicts | Medium / high | Hot-file ownership, `@bundle` resource, auto-discovery instead of central edits |
| Live-database damage | Low / high | Read-only default, `@live-db-write` plus GG approval, dry runs on a copy, additive `dx_` tables only |
| Language-model cost and rate limits | Medium / low | Deterministic stages first, cap, cache by fingerprint, fallback chain |
| Scope creep into a CRM or a marketplace | Medium / medium | Section 2 "is not"; new work enters through `board.py add` |
| OneDrive sync lag and shared-tree surprises | Medium / low | Worktrees, explicit paths, fetch before push, re-check after large writes |
| Market and rule facts go stale | High / medium | Every item dated and sourced; radar and clock have refresh commands and `verified_on` |

---

## Appendix A. Facts used in this plan and how well they are verified

Read from secondary sources on 2026-10-23; the owning tickets must confirm each against the primary text before it is relied on.

- Machinery Regulation (EU) 2023/1230 applies from **20 January 2027** (42-month transition for some provisions per an industry document).
  Sources: [Ceramic World Web](https://ceramicworldweb.com/en/news/new-machinery-regulation-countdown-begins),
  [CECE regulatory update](https://www.cece.eu/stream/regulatory-update-cece-congress-2024.pdf). One vendor blog gave a different date and is not relied on.
- Cyber Resilience Act: vulnerability and incident **reporting from 11 September 2026**, main obligations **from 11 December 2027**.
  Source: [Rockwell Automation trust centre](https://www.rockwellautomation.com/en-au/trust-center/eu-cyber-resilience-act-update.html).
- Iperammortamento 2026-2028: Legge di Bilancio 2026 (L. 199/2025, art. 1 cc. 427-436) replaced the Industry 4.0 / Transizione 5.0 credits;
  investments from **1 Jan 2026 to 30 Sep 2028**; markups 180% up to EUR 2.5m, 100% to 10m, 50% to 20m; DL 38/2026 removed the EU-made requirement;
  MIMIT implementing decree 7 May 2026; reservation platform open from 12 June 2026; Transizione 5.0 funds closed 7 Nov 2025.
  Sources: [MySolution](https://www.mysolution.it/fisco/approfondimenti/prima-lettura/2026/05/iper-ammortamento-2026-decreto-attuativo-firmato-in-attesa-della-g.u),
  [ANCE](https://ance.it/2026/03/decreto-fiscale-novita-per-liperammortamento-e-la-transizione-5-0/?print=pdf),
  [Leyton](https://leyton.com/it/insights/articles/iperammortamento-2026-transizione-5-0/). One source gives the DL 38/2026 conversion as law 88 of 22 May 2026; confirm.
- Market readings: UCIMU machine-tool orders Q2 2026 **-25.8%** (domestic -38.7%) ([The Machine Maker](https://themachinemaker.com/news/ucimu-reports-25-8-decline-in-machine-tool-orders-in-q2-2026/));
  ACIMIT textile-machinery orders Q2 2026 index 46.6 (-3% y/y), H1 -4% ([Textile Excellence](https://textileexcellence.com/single-news/7090/italian-textile-machinery-orders-rebound-in-q2-2026));
  UCIMA packaging machinery H1 2026 revenue +2.3%, orders -2.5% (Italy +3.3%, abroad -3.0%) ([Packaging Journal](https://packaging-journal.de/en/wp-json/wp/v2/posts/118803)).
  No ANIMA or 2026 Federmacchine figure was found: D14 should look.
- NIS2 in Italy: D.Lgs. 138/2024 (Gazzetta Ufficiale 1 Oct 2024), ACN is the authority ([Noze](https://www.noze.it/en/insights/nis2-pmi-cyberscan/),
  [Kiwa](https://careers.kiwa.com/it/en/areas-of-expertise/cyber-security/nis-2-directive/)). **Not confirmed**: whether NACE C28 sits in Annex I or II,
  the medium-size thresholds, the 2026 ACN calendar. D05 must settle this from the decree.

## Appendix B. Glossary
**Fact** one graded, dated, sourced statement about one company. **Grade** A filed/official, B own publication, C third party, D inference.
**Hypothesis** a testable claim about a need. **Strength / confidence** how strongly the evidence points at it / how far to trust that reading.
**Trigger** a dated event that makes now a moment to call. **Pilot concept** a type of time-boxed experiment GG can propose. **Archetype** a
pitch template. **Tier** the call decision (A/B/C/HOLD/EXCLUDE) from explicit rules. **Ticket** a unit of agent work with a write scope.
**Check out / check in** claim / finish a ticket through `scripts/board.py`. **ETO / CTO** engineer-to-order / configure-to-order.
**T3** an indicator only a conversation can reveal. **Peer percentile** where the company sits among comparable companies, higher = higher value.
