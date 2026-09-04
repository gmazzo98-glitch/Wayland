"""
The indicator catalog: every variable the tool looks for, imported from
Indicators.xlsx, reconciled against the later gg_indicators.json ground truth
(GG_Claude_Code_Indicator_Prompt.docx, Section 7.1 — 78 variables), plus a
handful of pre-existing Vienna signals that don't map cleanly onto either
source and are kept as their own entries rather than force-fitted onto a
mismatched one (see comments below on each).

This module is a SEED, not a live source of truth — seed_indicator_definitions()
inserts each row into the IndicatorDefinition table only if that key doesn't
already exist. It never overwrites a row that's there, so edits made from the
Indicator Weights page survive every app restart and every re-seed.

Field meanings are documented on IndicatorDefinition itself (models.py).
Every weight/bounds value below is a starting judgment call, not a researched
constant — the whole point of this table is that a human tunes it from the UI.
Where the source material's own "Comment" column already expressed a relative
importance ("the strongest signal", "weak signal alone", "weight lower than"),
that language is what drove the starting weight.

Reconciliation with gg_indicators.json (2026-08): 69 of its 78 rows already
had a conceptual match here (by proxy/logic, not always by exact key name);
those rows gained automation_tier/redundancy_group/axis_modifier fields from
the JSON without changing their existing axis/weight/invert/is_gate judgment
calls, several of which already handle a case the JSON's own axis_modifier
vocabulary names explicitly (NEGATIVE, GATING, GG-FIT GAP — see models.py's
IndicatorDefinition docstring for why invert/is_gate already implement these
without needing the tag to change scoring behavior). Two rows (Number of
Employees, Type of Product) were already correctly kept at axis=context/
weight=0 here despite the JSON row itself carrying a nonzero weight, because
each row's own indicator_logic explicitly argues it's a segment filter, not a
scored signal — the same judgment call is applied to two of the nine newly
added rows below (Vendor Contract Renewal Timing, Sector Pilot Precedent) for
the same reason. axis_modifier is carried through as free text rather than
forced into Section 2.2's six-value list, since the JSON's own data uses a
seventh value ("READINESS CAVEAT", on Debt/Leverage) not in that list.
"""

from sqlalchemy.orm import Session
from models import IndicatorDefinition

CAT_LEADERSHIP = "Leadership & Succession"
CAT_GOVERNANCE = "Governance & Decision Structure"
CAT_OPENNESS = "External Openness Track Record"
CAT_FINANCIAL = "Financial Health & Capital Structure"
CAT_COST = "Cost Structure & Margin Pressure"
CAT_INNOVATION_GAP = "Innovation Capacity Gap"
CAT_MARKET = "Market & Product Position"
CAT_DIGITAL = "Digital, Channel & Trade Presence"
CAT_WORKFORCE = "Workforce & Culture"
CAT_TRANSFORMATION = "Transformation & Sustainability Signals"
CAT_RISK = "Risk & Compliance Exposure"
CAT_CONTEXT = "Context & Segment Tags (not scored)"

# phase 6 = manual / first-contact interview data — not pipeline-automatable,
# entered by hand from the Company Intelligence page. Extends the existing
# 1-5 phase convention from the Technical Brief.
PHASE_MANUAL = 6

# Indicators whose raw value is a computed multi-year change, not a single
# direct observation — company_service.py's flexible data feeder only offers
# these as mapping targets for a detected time-series column group (e.g.
# revenue_latest/revenue_y-1/revenue_y-2), never for a single column, since a
# trend needs two timepoints to compute. See detect_column_groups/
# compute_group_value in company_service.py.
TREND_INDICATOR_KEYS = {"revenue_trend", "ebit_trend", "margin_compression"}

INDICATOR_SEED = [
    # ---------------------------------------------------------------- Leadership & Succession
    dict(key="management_age", automation_tier="T2", redundancy_group="MGMT_PROFILE", label="Management Age", category=CAT_LEADERSHIP, axis="readiness",
         invert=True, raw_min=35, raw_max=65, weight=2.0, phase=5, source_system="LinkedIn/Bios",
         freshness_days=365, proxy="Average age of the executive team (CEO, CFO, COO, board)",
         rationale="A younger executive team tends to be less anchored to legacy habits and faster to adopt new practices.",
         comment="Correlation, not causation. Use jointly with New Generation of Management rather than alone.",
         source_description="LinkedIn executive profiles, company leadership page, Handelsregister officer records, press bios",
         example_status="Low"),
    dict(key="mgmt_national_diversity", automation_tier="T2", redundancy_group="MGMT_DIVERSITY", label="Management National Diversity", category=CAT_LEADERSHIP, axis="readiness",
         raw_min=0, raw_max=60, weight=1.0, phase=5, source_system="LinkedIn/Bios", freshness_days=365,
         proxy="Nationality mix of the executive team and board (%)",
         rationale="Executives with international backgrounds are more likely to have been exposed to open-innovation practices abroad.",
         comment="Weak signal alone; most useful combined with International Sales Volume and Years of International Activity.",
         source_description="LinkedIn, company leadership page, press bios", example_status="High"),
    dict(key="mgmt_cultural_diversity", automation_tier="T2", redundancy_group="MGMT_DIVERSITY", label="Management Cultural Diversity", category=CAT_LEADERSHIP, axis="readiness",
         raw_min=0, raw_max=60, weight=1.0, phase=5, source_system="LinkedIn/Bios", freshness_days=365,
         proxy="Cultural/educational background mix of the executive team",
         rationale="A management team drawing on varied cultural reference points tends to score higher on openness to unfamiliar ideas.",
         comment="Overlaps heavily with Management National Diversity — treat as a secondary confirmation signal to avoid double-counting.",
         source_description="LinkedIn, bios, education history", example_status="High"),
    dict(key="mgmt_gender_diversity", automation_tier="T2", redundancy_group="MGMT_DIVERSITY", label="Management Gender Diversity", category=CAT_LEADERSHIP, axis="readiness",
         raw_min=0, raw_max=50, weight=1.0, phase=5, source_system="LinkedIn/Bios", freshness_days=365,
         proxy="% of women in the executive team and on the supervisory board",
         rationale="More gender-balanced leadership teams are associated with broader information search and less groupthink.",
         comment="Evidence in the literature is mixed with small effect sizes; weight lower than succession or education signals.",
         source_description="LinkedIn, company leadership page, Handelsregister officer listing", example_status="High"),
    dict(key="mgmt_education_level", automation_tier="T2", redundancy_group="MGMT_EDUCATION", label="Management Level of Education", category=CAT_LEADERSHIP, axis="readiness",
         raw_min=0, raw_max=100, weight=2.0, phase=5, source_system="LinkedIn/Bios", freshness_days=365,
         proxy="Share of the executive team holding a degree or higher (Master's / MBA / PhD)",
         rationale="Higher formal education correlates with comfort engaging with a structured, metrics-driven pilot framework.",
         comment="Confidence: medium. Easy to source for executives but rarely disclosed for the wider workforce.",
         source_description="LinkedIn education history, university alumni announcements, press bios", example_status="High"),
    dict(key="mgmt_education_diversity", automation_tier="T2", redundancy_group="MGMT_EDUCATION", label="Management Diversity of Education", category=CAT_LEADERSHIP, axis="readiness",
         raw_min=1, raw_max=5, weight=2.0, phase=5, source_system="LinkedIn/Bios", freshness_days=365,
         proxy="Range of academic disciplines represented across the executive team",
         rationale="Cross-disciplinary leadership is more likely to recognize an unfamiliar external idea as relevant.",
         comment="Confidence: medium, best read together with Management Level of Education.",
         source_description="LinkedIn education history", example_status="High"),
    dict(key="new_generation_management", automation_tier="T1", redundancy_group="SUCCESSION", label="New Generation of Management", category=CAT_LEADERSHIP, axis="both",
         invert=True, raw_min=0, raw_max=6, weight=5.0, phase=2, source_system="Handelsregister", freshness_days=180,
         proxy="Years since a family-surname generational handover in management",
         rationale="Both a NEED signal (incoming leadership wants to establish its own track record) and a READINESS signal (a newly installed generation is usually most open to revising the business model).",
         comment="The strongest single trigger-event variable in this table. Pair with Management Age; treat a handover under 2 years as a priority outreach signal.",
         source_description="Handelsregister officer change filings, press releases announcing new CEO/MD, company 'our story' page",
         example_status="Recent"),
    dict(key="management_turnover", automation_tier="T1", redundancy_group="MGMT_PROFILE", label="Turnover of Management", category=CAT_LEADERSHIP, axis="need",
         raw_min=0, raw_max=4, weight=2.0, phase=2, source_system="Handelsregister", freshness_days=180,
         proxy="Number of C-suite/MD changes in the past 3 years",
         rationale="Frequent leadership turnover often coincides with a company actively searching for a new strategic direction.",
         comment="Could be a liability: company needs to find its way, but might not have the time to run a pilot.",
         source_description="Handelsregister officer change filings, press releases", example_status="High"),
    dict(key="senior_mgmt_tenure", automation_tier="T2", redundancy_group="MGMT_PROFILE", axis_modifier="NEGATIVE", label="Average Tenure of Senior Management", category=CAT_LEADERSHIP, axis="readiness",
         invert=True, raw_min=2, raw_max=20, weight=2.0, phase=5, source_system="LinkedIn/Bios", freshness_days=365,
         proxy="Years in current role for CEO/MD and direct reports",
         rationale="Very long tenure in the same senior roles correlates with entrenched routines and a higher bar for an unfamiliar external idea.",
         comment="Opposite reading from Management Age — keep as a separate variable rather than assuming they move together.",
         source_description="LinkedIn tenure history, company leadership page", example_status="Long (15+ years in role)"),

    # ---------------------------------------------------------------- Governance & Decision Structure
    dict(key="interdepartmental_collaboration", automation_tier="T3", redundancy_group="ORG_STRUCTURE", label="Interdepartmental Collaboration", category=CAT_GOVERNANCE, axis="readiness",
         raw_min=1, raw_max=5, weight=2.0, phase=PHASE_MANUAL, source_system="First-Contact Interview", freshness_days=730,
         proxy="Org-chart flatness or job postings mentioning cross-functional teams (1-5 rating)",
         rationale="Low silo-ing correlates with faster internal alignment to launch and staff a time-boxed pilot.",
         comment="Genuinely hard to source externally; best captured through a short first-contact questionnaire.",
         source_description="First-contact questionnaire (primary data), job postings, org chart if published", example_status="High"),
    dict(key="org_verticality", automation_tier="T3", redundancy_group="ORG_STRUCTURE", label="Verticality in the Structure", category=CAT_GOVERNANCE, axis="readiness",
         invert=True, raw_min=2, raw_max=7, weight=2.0, phase=PHASE_MANUAL, source_system="First-Contact Interview", freshness_days=730,
         proxy="Number of management layers between CEO and frontline staff",
         rationale="Flatter hierarchies shorten the pilot approval chain, reducing the 'long, complex sale' risk.",
         comment="Same sourcing limitation as Interdepartmental Collaboration — best confirmed at first contact.",
         source_description="First-contact questionnaire, org chart if published, headcount as a rough proxy", example_status="Low"),
    dict(key="subsidiary_participations", automation_tier="T1", redundancy_group="ORG_STRUCTURE", label="Participations in Subsidiaries", category=CAT_GOVERNANCE, axis="readiness",
         invert=True, raw_min=0, raw_max=8, weight=1.0, phase=2, source_system="Handelsregister", freshness_days=730,
         proxy="Number of registered subsidiary/shareholding interests",
         rationale="A simple corporate structure means less internal complexity and bureaucracy to navigate before a pilot decision.",
         comment="A holding structure is often for tax/succession reasons unrelated to complexity — verify at first contact before reading negatively.",
         source_description="Handelsregister, Bundesanzeiger consolidated financial statement notes", example_status="Low or Zero"),
    dict(key="independent_board_members", automation_tier="T1", redundancy_group="GOVERNANCE", label="Independent (Non-Family) Board Members", category=CAT_GOVERNANCE, axis="readiness",
         raw_min=0, raw_max=4, weight=2.0, phase=2, source_system="Handelsregister", freshness_days=730,
         proxy="Number of non-family supervisory or advisory board members",
         rationale="External governance input tends to introduce outside perspectives and lower resistance to an unfamiliar external proposal.",
         comment="Confirm the board member is genuinely independent rather than counting on title alone.",
         source_description="Handelsregister, company website governance page", example_status="Present / increasing"),
    dict(key="digital_lead_role_present", automation_tier="T2", redundancy_group="GOVERNANCE", axis_modifier="GATING", label="Named Digital/Innovation Lead Role", category=CAT_GOVERNANCE, axis="readiness",
         raw_min=0, raw_max=1, weight=4.0, is_gate=True, gate_penalty_multiplier=0.7,
         phase=4, source_system="Company Website", freshness_days=365,
         proxy="Job title search for 'Head of Digital', 'Innovation Manager', 'Digitalisierungsbeauftragter' or similar",
         rationale="Maps directly to the governance question 'does an internal responsible party for the pilot exist?'. Absence strongly predicts stalling at the approval stage for lack of an internal champion.",
         comment="Treated as close to a gating variable: when confirmed absent, it multiplies the readiness score down rather than just contributing its own share.",
         source_description="Company website, LinkedIn role search", example_status="None"),
    dict(key="approval_chain_depth", automation_tier="T3", redundancy_group="GOVERNANCE", axis_modifier="NEGATIVE", label="Depth of Internal Approval Chain", category=CAT_GOVERNANCE, axis="readiness",
         invert=True, raw_min=1, raw_max=6, weight=3.0, phase=PHASE_MANUAL, source_system="First-Contact Interview", freshness_days=730,
         proxy="Estimated number of approval layers for new vendors/pilots",
         rationale="A long, multi-layer approval chain is the concrete mechanism behind the 'commitment gap: long, complex sale' pattern.",
         comment="Best confirmed directly at first contact rather than estimated from external data.",
         source_description="First-contact interview, org size/verticality as a rough proxy", example_status="High (multiple approval layers)"),

    # ---------------------------------------------------------------- External Openness Track Record
    dict(key="external_collaboration", automation_tier="T2", redundancy_group="PRIOR_COLLAB", label="External Collaboration", category=CAT_OPENNESS, axis="readiness",
         raw_min=0, raw_max=1, weight=5.0, phase=4, source_system="News/Press", freshness_days=365,
         proxy="Documented past partnerships with startups, universities, accelerators, or corporate venturing programs",
         rationale="The strongest behavioral proof of readiness available — revealed preference, not stated intent, and it de-risks GG's pitch since the company already cleared its internal approval hurdle once before.",
         comment="Weight more heavily than any self-reported openness variable, since it reflects observed behavior.",
         source_description="Press releases, university/accelerator case studies, company 'partners' page, patent co-assignments",
         example_status="Happened before"),
    dict(key="trade_fair_participation", automation_tier="T2", redundancy_group="PRIOR_COLLAB", label="Trade Fair / Association Participation", category=CAT_OPENNESS, axis="readiness",
         raw_min=0, raw_max=6, weight=2.0, phase=4, source_system="Trade Fairs/Associations", freshness_days=365,
         proxy="Exhibitor records at relevant trade fairs, membership in sector associations",
         rationale="Regular external engagement through trade fairs/associations indicates a company that invests time scanning the outside world for new ideas.",
         comment="Low-cost to check and a good early-stage filter alongside Online Market Presence.",
         source_description="Trade fair exhibitor directories, industry association member lists", example_status="Active / frequent"),
    dict(key="university_partnership", automation_tier="T2", redundancy_group="PRIOR_COLLAB", label="University / Research Institute Partnership", category=CAT_OPENNESS, axis="readiness",
         raw_min=0, raw_max=1, weight=5.0, phase=4, source_system="News/Press", freshness_days=730,
         proxy="Named partnerships or funded research collaborations with universities or Fraunhofer-type institutes",
         rationale="The strongest possible precedent variable together with External Collaboration — direct proof the company has already navigated a structured external collaboration once.",
         comment="Effectively a specific sub-case of External Collaboration; consider merging if scoring redundancy becomes an issue.",
         source_description="University press releases, Fraunhofer project databases, company website", example_status="Existing / recent"),
    dict(key="prior_open_innovation_usage", automation_tier="T2", redundancy_group="PRIOR_COLLAB", axis_modifier="BASELINE", label="Prior Open-Innovation Channel Usage", category=CAT_OPENNESS, axis="readiness",
         raw_min=0, raw_max=1, weight=4.0, phase=4, source_system="News/Press", freshness_days=730,
         proxy="Any documented sponsorship or participation in idea competitions, hackathons, or accelerator cohorts",
         rationale="Zero prior usage places a company at the DREAMER level at best (interested in principle, no proven execution track record); existing usage moves it toward OPERATOR/ORCHESTRATOR.",
         comment="A direct proxy for a company's overall maturity level — earmarked as a future primary input to that classifier rather than just one table row among many.",
         source_description="Accelerator cohort announcements, hackathon sponsor lists, press releases", example_status="None"),
    dict(key="public_grant_count", automation_tier="T1", redundancy_group="CAPITAL_APPETITE", label="Government Contributions", category=CAT_OPENNESS, axis="readiness",
         raw_min=0, raw_max=5, weight=3.0, phase=1, source_system="EU Funding Portal", freshness_days=365,
         proxy="Public grant/subsidy hits from the EU Funding Portal search API", cost_per_pull=0.0,
         rationale="Recently receiving public innovation/transformation funding shows the company has already cleared a formal application and screening process — evidence of both intent and bureaucratic capacity.",
         comment="Reuses the existing, already-live EU Funding Portal adapter (adapters/eu_funding.py) rather than a new key — that adapter counts hits, so this stays a direct (non-inverted) count rather than the recency framing the spreadsheet describes; recency-based scoring is a real future refinement of the same adapter, not a new indicator.",
         source_description="Fördermitteldatenbank, EU State Aid Transparency register, press releases", example_status="Recent"),

    # ---------------------------------------------------------------- Financial Health & Capital Structure
    dict(key="ebit_trend", automation_tier="T1", redundancy_group="FIN_TREND", label="EBIT Trend", category=CAT_FINANCIAL, axis="need",
         invert=True, raw_min=-40, raw_max=20, weight=4.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="EBIT % change over the past 3 fiscal years", cost_per_pull=5.0,
         rationale="Declining operating profit is the clearest top-level financial pressure signal.",
         comment="A company with consistently falling EBIT may also lack budget to fund even a low-cost pilot — treat a severe multi-year decline as a distress flag, not a pure opportunity signal.",
         source_description="Bundesanzeiger annual filings", example_status="Decreasing/Stagnating"),
    dict(key="margin_compression", automation_tier="T1", redundancy_group="FIN_TREND", label="Margin Compression", category=CAT_FINANCIAL, axis="need",
         raw_min=0, raw_max=25, weight=3.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="Margin compression (%), from the existing Bundesanzeiger paid-pull scraper", cost_per_pull=5.0,
         rationale="Margin compression signals unresolved cost or pricing pressure that a structured pilot can target — the spreadsheet's Gross Margin row, in the original Vienna direct-compression framing that scrapers/bundesanzeiger_paid.py already produces.",
         comment="Compare against sector benchmarks rather than an absolute threshold — margin norms vary widely by industry. Kept as a direct 0-25% compression level (not a signed trend) to match what the existing scraper actually returns.",
         source_description="Bundesanzeiger annual filings", example_status="Decreasing/Stagnating"),
    dict(key="revenue_trend", automation_tier="T1", redundancy_group="FIN_TREND", label="Revenue Trend", category=CAT_FINANCIAL, axis="need",
         invert=True, raw_min=-20, raw_max=15, weight=3.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="Revenue % change over the past 3 fiscal years", cost_per_pull=5.0,
         rationale="Flat or declining top line is a direct growth-pressure signal.",
         comment="Check whether stagnation is company-specific or an entire-sector pattern before treating it as an internal NEED signal.",
         source_description="Bundesanzeiger annual filings", example_status="Decreasing/Stagnating"),
    dict(key="interest_coverage_ratio", automation_tier="T1", redundancy_group="FIN_STRAIN", label="Interest Coverage Ratio (Financial Pressure)", category=CAT_FINANCIAL, axis="need",
         invert=True, raw_min=0, raw_max=15, weight=3.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="EBIT ÷ interest expense", cost_per_pull=5.0,
         rationale="A weakening ability to cover debt service signals financial strain.",
         comment="A very low ratio indicates distress severe enough to crowd out discretionary pilot budget — don't read the trend alone without checking the absolute level.",
         source_description="Bundesanzeiger annual filings (EBIT and interest expense lines)", example_status="Decreasing/Stagnating"),
    dict(key="capex_ratio", automation_tier="T1", redundancy_group="CAPEX_APPETITE", label="CapEx Ratio", category=CAT_FINANCIAL, axis="need",
         invert=True, raw_min=1, raw_max=15, weight=2.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="CapEx as % of revenue, trend over 3 years", cost_per_pull=5.0,
         rationale="Persistently low capital investment signals underinvestment in physical or technological capacity.",
         comment="Can also reflect a deliberately conservative, cash-preserving management style that may extend to reluctance in funding a pilot — verify appetite at first contact.",
         source_description="Bundesanzeiger annual filings", example_status="Low"),
    dict(key="total_assets", automation_tier="T1", redundancy_group="SEGMENT_FILTER", label="Total Assets", category=CAT_FINANCIAL, axis="readiness", curve_type="band",
         raw_min=5000000, raw_max=50000000, weight=2.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="Total assets from balance sheet", cost_per_pull=5.0,
         rationale="A medium asset base signals enough absorptive infrastructure to actually host and integrate a pilot's output — too small and there's nothing to attach the innovation to, too large and bureaucracy slows everything down.",
         comment="Scored as a band (sweet spot), not a straight line: too low or too high both taper the score down.",
         source_description="Bundesanzeiger annual filings", example_status="Medium"),
    dict(key="cash_position", automation_tier="T1", redundancy_group="FIN_STRAIN", axis_modifier="NEGATIVE", label="Cash Position", category=CAT_FINANCIAL, axis="readiness",
         raw_min=0, raw_max=3000000, weight=3.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="Cash and cash equivalents from balance sheet, trend over 3 years", cost_per_pull=5.0,
         rationale="Low or falling cash is a warning that the company may lack the discretionary budget to fund even a low-cost pilot, regardless of how much it needs to innovate.",
         comment="Functions as a downgrade flag that can pull down an otherwise promising NEED-heavy profile, not an independent opportunity signal.",
         source_description="Bundesanzeiger annual filings", example_status="Low/Decreasing"),
    dict(key="debt_level", automation_tier="T1", redundancy_group="FIN_STRAIN", axis_modifier="READINESS CAVEAT", label="Debt Level", category=CAT_FINANCIAL, axis="need",
         raw_min=0, raw_max=20000000, weight=2.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="Total debt from balance sheet, trend over 3 years", cost_per_pull=5.0,
         rationale="Rising debt signals financial strain that can motivate efficiency-seeking innovation, but high leverage often comes with covenant restrictions that lower practical readiness to commit to a pilot.",
         comment="Cross-check against Leverage and Interest Coverage Ratio before treating high debt as a pure opportunity signal.",
         source_description="Bundesanzeiger annual filings", example_status="High/Increasing"),
    dict(key="leverage_ratio", automation_tier="T1", redundancy_group="FIN_STRAIN", axis_modifier="READINESS CAVEAT", label="Leverage Ratio", category=CAT_FINANCIAL, axis="need",
         raw_min=0, raw_max=5, weight=2.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="Debt/EBITDA or Debt/Equity ratio", cost_per_pull=5.0,
         rationale="High leverage indicates financial pressure but also potential capital constraints on funding a pilot.",
         comment="Always interpret alongside sector-typical leverage norms — Mittelstand companies commonly run higher leverage than public peers as a matter of course.",
         source_description="Bundesanzeiger annual filings", example_status="High"),
    dict(key="private_funding", automation_tier="T1", redundancy_group="CAPITAL_APPETITE", label="Private Funding Trend", category=CAT_FINANCIAL, axis="readiness",
         raw_min=0, raw_max=1, weight=2.0, phase=2, source_system="Handelsregister", freshness_days=365,
         proxy="Non-bank financing raised in the past 2 years (equity injections, shareholder loans, private credit)",
         rationale="An actively increasing private-funding trend signals a company already comfortable raising and deploying capital outside traditional bank channels — a positive proxy for tolerance of the kind of hard-to-quantify risk a pilot represents.",
         comment="Banks often do not give loans for innovation because it is not easily quantifiable.",
         source_description="Bundesanzeiger capital increase filings, press releases on funding rounds, Handelsregister capital change filings",
         example_status="Increasing"),

    # ---------------------------------------------------------------- Cost Structure & Margin Pressure
    dict(key="materials_cost", automation_tier="T1", redundancy_group="COST_PRESSURE", label="Materials Cost", category=CAT_COST, axis="need",
         raw_min=10, raw_max=60, weight=2.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="Materials cost as % of revenue, trend over 3 years", cost_per_pull=5.0,
         rationale="Rising input costs squeeze margin and motivate sourcing or process innovation.",
         comment="Sensitive to commodity price cycles outside the company's control — corroborate with Cost of Raw Materials.",
         source_description="Bundesanzeiger annual filings", example_status="High/Increasing"),
    dict(key="labour_cost", automation_tier="T1", redundancy_group="COST_PRESSURE", label="Labour Cost", category=CAT_COST, axis="need",
         raw_min=15, raw_max=55, weight=2.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="Personnel cost as % of revenue, trend over 3 years", cost_per_pull=5.0,
         rationale="Rising labor cost share increases the appeal of automation or productivity-focused pilots.",
         comment="Can simply reflect a tight regional labor market rather than a company-specific issue — check against Bundesagentur für Arbeit regional wage data.",
         source_description="Bundesanzeiger annual filings", example_status="High/Increasing"),
    dict(key="logistics_cost", automation_tier="T1", redundancy_group="COST_PRESSURE", label="Logistics Costs", category=CAT_COST, axis="need",
         raw_min=2, raw_max=20, weight=1.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="Freight/distribution cost line as % of revenue if disclosed, else industry benchmark", cost_per_pull=5.0,
         rationale="Rising logistics costs motivate route, warehousing, or fulfillment innovation.",
         comment="Rarely broken out separately in small-company filings; industry benchmark may be the only available proxy.",
         source_description="Bundesanzeiger notes where disclosed, industry logistics cost benchmarks", example_status="High/Increasing"),
    dict(key="energy_cost", automation_tier="T1", redundancy_group="COST_PRESSURE", label="Energy Costs", category=CAT_COST, axis="need",
         raw_min=1, raw_max=15, weight=3.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="Energy cost line as % of revenue if disclosed, else sector energy-intensity benchmark", cost_per_pull=5.0,
         rationale="Rising energy costs are an especially strong, topical pressure point in the German Mittelstand post-2022 energy price shock.",
         comment="Especially relevant for energy-intensive manufacturing; less diagnostic for services businesses.",
         source_description="Bundesanzeiger notes, sector energy intensity benchmarks, press coverage", example_status="High/Increasing"),
    dict(key="cogs_ratio", automation_tier="T1", redundancy_group="COST_PRESSURE", label="COGS Ratio", category=CAT_COST, axis="need",
         raw_min=40, raw_max=90, weight=2.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="COGS as % of revenue, trend over 3 years", cost_per_pull=5.0,
         rationale="A composite of the cost-pressure rows above; rising COGS share is the aggregate signal.",
         comment="Largely a rollup of Materials/Labour/Logistics/Energy — decide whether a scoring model needs both the components and this aggregate, or just one.",
         source_description="Bundesanzeiger annual filings", example_status="High/Increasing"),
    dict(key="service_costs", automation_tier="T1", redundancy_group="COST_PRESSURE", label="Service Costs", category=CAT_COST, axis="need",
         raw_min=2, raw_max=25, weight=1.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="Purchased services cost line as % of revenue", cost_per_pull=5.0,
         rationale="Rising third-party service spend can indicate the company is already outsourcing functions it lacks in-house capability for.",
         comment="Distinguish from Labour Cost: rising service costs point to external outsourcing dependency, not internal headcount cost.",
         source_description="Bundesanzeiger annual filings", example_status="High/Increasing"),

    # ---------------------------------------------------------------- Innovation Capacity Gap
    # These three reuse the existing signal_key so the already-working EPO/EUIPO/Bundesanzeiger
    # adapters keep populating them unmodified.
    dict(key="rd_expense_ratio", automation_tier="T1", redundancy_group="INNOVATION_INPUT", axis_modifier="GG-FIT GAP", label="R&D Expenses (GG-Fit Gap)", category=CAT_INNOVATION_GAP, axis="readiness",
         invert=True, raw_min=0, raw_max=8, weight=4.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="R&D/development cost line if disclosed, else patent filing activity as proxy", cost_per_pull=5.0,
         rationale="Low or zero internal R&D spend doesn't mean the company perceives no need — it usually means there's no internal alternative to an external pilot. Less a need signal, more a fit signal for why GG rather than in-house R&D.",
         comment="Don't read as low ambition, read as low internal capability, which strengthens rather than weakens GG's pitch.",
         source_description="Bundesanzeiger filings, DPMA patent filing history as proxy", example_status="Low or Zero"),
    dict(key="patent_count", automation_tier="T1", redundancy_group="INNOVATION_INPUT", axis_modifier="GG-FIT GAP", label="Patent Count (GG-Fit Gap)", category=CAT_INNOVATION_GAP, axis="readiness",
         invert=True, raw_min=0, raw_max=10, weight=3.0, phase=1, source_system="EPO OPS", freshness_days=90,
         proxy="Patent filing count and recency, from DPMA or EPO Espacenet", cost_per_pull=0.0,
         rationale="Same logic as R&D Expenses: limited patent activity signals the company hasn't pursued the traditional in-house innovation route, making an external, pilot-based model more relevant.",
         comment="Absence of patents is normal and uninformative in many services/trade sectors — weight down outside manufacturing/technical sectors.",
         source_description="DPMA register, EPO Espacenet", example_status="Low or Zero"),
    dict(key="patent_ipc_diversity", automation_tier="T1", label="Patent IPC Class Diversity", category=CAT_INNOVATION_GAP, axis="context",
         raw_min=0, raw_max=5, weight=0.0, phase=1, source_system="EPO OPS", freshness_days=90,
         proxy="Distinct IPC classification prefixes across filed patents",
         rationale="Supplementary enrichment from the existing EPO adapter, not part of the imported spreadsheet — kept informational rather than scored to avoid double-counting alongside Patent Count.",
         comment="Displayed on the company profile but excluded from the weighted score.",
         source_description="EPO OPS classification data", example_status="—"),
    dict(key="trademark_count", automation_tier="T1", redundancy_group="INNOVATION_INPUT", label="Trademarks", category=CAT_INNOVATION_GAP, axis="readiness",
         raw_min=0, raw_max=8, weight=3.0, phase=1, source_system="EUIPO", freshness_days=90,
         proxy="Number of new trademark filings in the past 2-3 years", cost_per_pull=0.0,
         rationale="Active trademark filing suggests the company is bringing new products, sub-brands, or services to market right now — present-tense openness rather than stated intent.",
         comment="A useful complement to the low-patent signal: together they suggest a company innovating at the brand/go-to-market level but not the technical level, exactly where GG-brokered pilots add the most value.",
         source_description="DPMA trademark register", example_status="Increasing"),

    # ---------------------------------------------------------------- Market & Product Position
    dict(key="product_demand_elasticity", automation_tier="T2", label="Product Demand Elasticity", category=CAT_MARKET, axis="readiness",
         raw_min=1, raw_max=5, weight=1.0, phase=PHASE_MANUAL, source_system="First-Contact Interview", freshness_days=730,
         proxy="1-5 elasticity rating inferred from category (staple vs. discretionary good)",
         rationale="Companies with elastic demand have a stronger built-in incentive to keep customers happy, translating into higher motivation to adopt a pilot that improves customer experience.",
         comment="A motivation proxy, not a capability one — still requires the other readiness variables to be present.",
         source_description="Industry classification/category norms, review-aggregator sentiment, press coverage of pricing behavior",
         example_status="Elastic"),
    dict(key="product_differentiation", automation_tier="T2", redundancy_group="PRODUCT_HEALTH", label="Product Differentiation", category=CAT_MARKET, axis="need",
         raw_min=0, raw_max=15, weight=2.0, phase=4, source_system="Company Website", freshness_days=365,
         proxy="Number of comparable competing products at similar price points",
         rationale="A commoditized, undifferentiated product is under constant margin pressure and has the clearest need for an innovation-driven differentiation strategy.",
         comment="Best assessed together with Innovativeness of Product and Quality of Product rather than in isolation.",
         source_description="Marketplace/competitor listings, industry trade reports, product catalogs", example_status="Low / commoditized"),
    dict(key="product_portfolio_diversity", automation_tier="T2", redundancy_group="PRODUCT_HEALTH", label="Product Portfolio Diversity", category=CAT_MARKET, axis="need",
         invert=True, raw_min=1, raw_max=20, weight=1.0, phase=4, source_system="Company Website", freshness_days=730,
         proxy="Number of distinct product lines or SKUs disclosed",
         rationale="A narrow product portfolio concentrates revenue risk in a small number of lines, strengthening the case for innovation-driven diversification.",
         comment="A very narrow portfolio in a stable niche B2B category is not automatically a red flag — check whether the niche itself is shrinking first.",
         source_description="Company catalog/website, industry association member directories", example_status="Low / narrow"),
    dict(key="raw_material_rarity", automation_tier="T2", redundancy_group="SUPPLY_RISK", label="Rarity of Raw Materials", category=CAT_MARKET, axis="need",
         invert=True, raw_min=1, raw_max=10, weight=1.0, phase=PHASE_MANUAL, source_system="First-Contact Interview", freshness_days=730,
         proxy="Number of alternative suppliers available for the primary input",
         rationale="Dependence on a scarce input is a direct supply-risk pressure point that process or material-substitution innovation can address.",
         comment="Applies mainly to manufacturing and materials-intensive sectors; not applicable for services businesses.",
         source_description="Industry/commodity reports, annual report risk disclosures, procurement trade press", example_status="High"),
    dict(key="raw_material_cost", automation_tier="T1", redundancy_group="COST_PRESSURE", label="Cost of Raw Materials", category=CAT_MARKET, axis="need",
         raw_min=10, raw_max=70, weight=2.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="Input cost as % of COGS", cost_per_pull=5.0,
         rationale="Elevated material costs squeeze margin directly and create urgency for efficiency or substitution innovation.",
         comment="Same sector-benchmark caveat as Materials Cost.",
         source_description="Bundesanzeiger annual filings, industry cost benchmark reports", example_status="High"),
    dict(key="product_innovativeness", automation_tier="T2", redundancy_group="PRODUCT_HEALTH", label="Innovativeness of Product", category=CAT_MARKET, axis="need",
         raw_min=0, raw_max=10, weight=2.0, phase=4, source_system="News/Press", freshness_days=365,
         proxy="Years since last meaningful product update",
         rationale="A product line that hasn't meaningfully changed in years is the textbook target for an innovation pilot.",
         comment="Best corroborated with Product Age and Patents — a single missing update is not conclusive on its own.",
         source_description="Product catalogs (historical versions via Wayback Machine), press releases, trade fair exhibitor archives",
         example_status="Low / mature, largely unchanged"),
    dict(key="product_quality_trend", automation_tier="T2", redundancy_group="PRODUCT_HEALTH", label="Quality of Product (Review Trend)", category=CAT_MARKET, axis="need",
         invert=True, raw_min=-1.5, raw_max=0.5, weight=2.0, phase=4, source_system="Review Platforms", freshness_days=180,
         proxy="Review sentiment trend (rating points change) over the trailing 12 months",
         rationale="A declining quality trend visible in customer reviews is a direct, externally verifiable pain point a pilot could target.",
         comment="If review volume is too low to be reliable (typical for B2B manufacturers), fall back on warranty claim rates or complaint data gathered at first contact.",
         source_description="Google Reviews, Trustpilot, industry review platforms, first-contact interview for B2B", example_status="Declining / rising complaints"),
    dict(key="product_age", automation_tier="T2", redundancy_group="PRODUCT_HEALTH", label="Product Age", category=CAT_MARKET, axis="need",
         raw_min=0, raw_max=40, weight=2.0, phase=4, source_system="News/Press", freshness_days=730,
         proxy="Years since the original core product line launched",
         rationale="An old core product line still generating the bulk of revenue is an obsolescence risk and a clear need signal.",
         comment="Overlaps with Innovativeness of Product; consider merging the two if scoring redundancy becomes an issue.",
         source_description="Company 'our history' page, trade press archives", example_status="Old / long-standing core line"),

    # ---------------------------------------------------------------- Digital, Channel & Trade Presence
    dict(key="online_sales_volume", automation_tier="T2", redundancy_group="DIGITAL_COMMERCIAL", label="Volume of Online Sales", category=CAT_DIGITAL, axis="need",
         invert=True, raw_min=0, raw_max=40, weight=2.0, phase=4, source_system="Company Website", freshness_days=180,
         proxy="Estimated e-commerce revenue share (%)",
         rationale="Low online sales volume relative to industry peers is a direct, quantifiable innovation gap.",
         comment="Hard to estimate precisely without direct disclosure; treat any traffic-tool-derived figure as directional.",
         source_description="Similarweb/website traffic estimate, annual report management commentary, industry benchmarks",
         example_status="Low"),
    dict(key="online_market_presence", automation_tier="T2", redundancy_group="DIGITAL_COMMERCIAL", label="Online Market Presence", category=CAT_DIGITAL, axis="need",
         invert=True, raw_min=0, raw_max=5, weight=3.0, phase=4, source_system="Company Website", freshness_days=180,
         proxy="0-5 composite rating of e-commerce presence, social activity, and SEO visibility",
         rationale="An underdeveloped online presence is one of the most visible, easily verified NEED signals available before any direct contact.",
         comment="Fastest variable to check pre-contact — a good early-stage filter before investing research time in harder financial variables.",
         source_description="Company website audit, BuiltWith/Wayback Machine, social media presence check", example_status="No/not developed"),
    dict(key="physical_stores_trend", automation_tier="T2", redundancy_group="CHANNEL_FOOTPRINT", label="Physical Store Count Trend", category=CAT_DIGITAL, axis="need",
         invert=True, raw_min=-30, raw_max=10, weight=2.0, phase=4, source_system="Company Website", freshness_days=365,
         proxy="% change in store count over the past 3 years",
         rationale="A shrinking physical footprint without a compensating digital channel signals an unaddressed transition problem.",
         comment="If the company is pure B2B with no retail footprint, treat as null rather than 'zero = need'.",
         source_description="Company website store locator, press releases, historical Google Maps listings", example_status="Declining"),
    dict(key="store_geo_distribution", automation_tier="T2", redundancy_group="CHANNEL_FOOTPRINT", label="Geographic Distribution of Stores", category=CAT_DIGITAL, axis="need",
         invert=True, raw_min=1, raw_max=8, weight=1.0, phase=4, source_system="Company Website", freshness_days=365,
         proxy="Number of distinct regions/postal areas served",
         rationale="A geographically concentrated footprint with no digital expansion represents unrealized growth GG could help unlock.",
         comment="Same B2B applicability caveat as Physical Store Count Trend.",
         source_description="Company website, Handelsregister branch (Zweigniederlassung) filings", example_status="Concentrated in a single region"),
    dict(key="international_sales_volume", automation_tier="T1", redundancy_group="INTERNATIONAL", label="International Sales Volume", category=CAT_DIGITAL, axis="need",
         invert=True, raw_min=0, raw_max=50, weight=3.0, phase=4, source_system="News/Press", freshness_days=365,
         proxy="Export revenue share (%), from annual report segment notes or Chamber of Commerce data",
         rationale="Stagnant international sales despite an otherwise viable product is a classic unrealized-opportunity signal, and a strong one specifically for GG's German-Italian bridge extension.",
         comment="Where segment notes aren't disclosed, a rough proxy is whether the company website exists in more than one language.",
         source_description="Annual report segment notes, IHK export statistics, Germany Trade & Invest data", example_status="Low/stagnating"),
    dict(key="years_international_activity", automation_tier="T1", redundancy_group="INTERNATIONAL", label="Years of International Activity", category=CAT_DIGITAL, axis="need",
         invert=True, raw_min=0, raw_max=15, weight=2.0, phase=2, source_system="Handelsregister", freshness_days=730,
         proxy="Years since first recorded export activity or foreign branch registration",
         rationale="Limited international experience means the company likely lacks internal playbooks for cross-border expansion — a NEED signal, but also a readiness caveat on execution capacity.",
         comment="Treat as a NEED signal on the product/market axis; don't double-count as purely positive.",
         source_description="Handelsregister foreign branch filings, company 'our history' page, trade press", example_status="Few"),
    dict(key="website_digital_maturity", automation_tier="T2", redundancy_group="DIGITAL_COMMERCIAL", label="Digital Maturity of Core Website", category=CAT_DIGITAL, axis="need",
         raw_min=0, raw_max=8, weight=3.0, phase=4, source_system="Company Website", freshness_days=180,
         proxy="Years since last major website redesign; CMS platform and mobile responsiveness",
         rationale="An outdated, non-responsive website is one of the cheapest, most externally verifiable proxies for broader digital neglect.",
         comment="Wayback Machine coverage can be patchy for smaller company sites — treat 'no snapshot found' as inconclusive, not evidence of an old site.",
         source_description="BuiltWith, Wayback Machine snapshot comparison, direct site visit", example_status="Low / outdated"),
    dict(key="linkedin_activity", automation_tier="T2", redundancy_group="DIGITAL_COMMERCIAL", label="LinkedIn Company Page Activity", category=CAT_DIGITAL, axis="need",
         invert=True, raw_min=0, raw_max=8, weight=1.0, phase=5, source_system="LinkedIn/Bios", freshness_days=180,
         proxy="Posting frequency (posts/month) over the past 12 months",
         rationale="Minimal external digital communication activity signals a broader gap in market-facing digital practice.",
         comment="Weight lower than the balance-sheet-based NEED signals, since it reflects marketing habits more than core operations.",
         source_description="LinkedIn company page", example_status="Low / inactive"),
    dict(key="digital_job_postings", automation_tier="T2", redundancy_group="HIRING_SIGNAL", label="Digital/Technical Job Postings", category=CAT_DIGITAL, axis="readiness",
         raw_min=0, raw_max=10, weight=3.0, phase=4, source_system="Job Postings", freshness_days=90,
         proxy="Count of open roles mentioning digital, data, automation, or innovation in the title/description, past 12 months",
         rationale="Actively hiring for digital or innovation-adjacent roles is a strong forward-looking signal of building internal capacity to absorb pilot outcomes.",
         comment="Distinguish genuine digital/innovation hires from generic IT-support postings — only the former counts as a strong readiness signal.",
         source_description="Stepstone, Indeed, company careers page", example_status="Increasing"),
    dict(key="erp_systems_age", automation_tier="T3", redundancy_group="DATA_READINESS", label="ERP / Core Systems Age", category=CAT_DIGITAL, axis="need",
         raw_min=0, raw_max=15, weight=2.0, phase=4, source_system="Job Postings", freshness_days=365,
         proxy="Years since last major ERP upgrade, inferred from job postings mentioning ERP version",
         rationale="Old, unintegrated core systems constrain data availability for scoring future pilots too — doubles as an opportunity signal and a data-quality warning for the pipeline itself.",
         comment="Directly relevant to confirming Midcap data-collection capability before a pilot starts — a legacy ERP flags the need to scope data readiness explicitly at the Diagnosi stage.",
         source_description="Job postings mentioning ERP platform/version, first-contact interview", example_status="Legacy (10+ years, not integrated)"),
    dict(key="supply_chain_digitization", automation_tier="T3", redundancy_group="DATA_READINESS", label="Supply Chain Digitization", category=CAT_DIGITAL, axis="need",
         invert=True, raw_min=0, raw_max=5, weight=2.0, phase=PHASE_MANUAL, source_system="First-Contact Interview", freshness_days=730,
         proxy="0-5 rating of EDI / real-time tracking / supplier portal technology presence",
         rationale="Manual, paper-based supply chain processes are a concrete operational pain point directly addressable by a logistics or process-innovation pilot.",
         comment="Often only confirmable at first contact for smaller companies with no public supply-chain disclosure.",
         source_description="Job postings, first-contact interview, sector benchmark reports", example_status="Low / manual"),
    dict(key="press_launch_mentions", automation_tier="T2", redundancy_group="PRODUCT_HEALTH", label="Press Mentions of New Launches", category=CAT_DIGITAL, axis="need",
         invert=True, raw_min=0, raw_max=6, weight=2.0, phase=4, source_system="News/Press", freshness_days=365,
         proxy="Count of launch-related news mentions in the trailing 24 months",
         rationale="Absence of any recent launch coverage is a simple, externally verifiable proxy for innovation output stagnation.",
         comment="Cross-check against Trademarks and R&D Expenses before concluding stagnation — a company can innovate quietly without press coverage.",
         source_description="News search (Google News), trade press archives", example_status="None in past 2 years"),
    # Supplementary — pre-existing Vienna signals kept as their own entries because their
    # measured quantity doesn't cleanly match a spreadsheet row (see indicators.py docstring).
    dict(key="sector_export_exposure", automation_tier="T1", label="Sector Export Pressure (Macro)", category=CAT_DIGITAL, axis="need",
         raw_min=0.3, raw_max=0.9, weight=2.0, phase=1, source_system="Destatis", freshness_days=180, cost_per_pull=0.0,
         proxy="Sector-level (NACE code) export exposure ratio from Destatis GENESIS-Online",
         rationale="Macro, sector-wide export pressure — distinct from the company-level International Sales Volume row above.",
         comment="From the original sourcing plan, not the new spreadsheet; kept as its own indicator since it measures something company-level data doesn't.",
         source_description="Destatis GENESIS-Online", example_status="High"),
    dict(key="tech_stack_intensity", automation_tier="T2", label="Digital Intensity Index (Tech Signatures)", category=CAT_DIGITAL, axis="need",
         raw_min=0, raw_max=10, weight=2.0, phase=4, source_system="Wappalyzer", freshness_days=60, cost_per_pull=0.0,
         proxy="Count of modern web technology signatures detected on the company's own site",
         rationale="Related to, but distinct from, Digital Maturity of Core Website — this measures which technologies are present, not redesign recency.",
         comment="From the original sourcing plan; kept separate from website_digital_maturity to avoid conflating what each adapter actually measures.",
         source_description="Local Wappalyzer-style signature scan", example_status="—"),

    # ---------------------------------------------------------------- Workforce & Culture
    dict(key="employee_turnover", automation_tier="T2", redundancy_group="WORKFORCE_STABILITY", label="Employee Turnover", category=CAT_WORKFORCE, axis="readiness",
         invert=True, raw_min=5, raw_max=25, weight=2.0, phase=5, source_system="LinkedIn/Bios", freshness_days=365,
         proxy="Staff attrition rate (%), estimated via LinkedIn tenure distribution or review mentions",
         rationale="Low turnover signals a stable operating base able to sustain a structured pilot through to completion.",
         comment="Very low turnover can also mean entrenched routines — cross-reference against Kununu/Glassdoor sentiment before scoring, since low turnover with poor reviews likely means people are staying out of necessity.",
         source_description="LinkedIn tenure distribution, Kununu/Glassdoor reviews, Bundesagentur für Arbeit regional turnover statistics",
         example_status="Low"),
    dict(key="average_salary", automation_tier="T1", redundancy_group="WORKFORCE_CAPABILITY", label="Average Salary", category=CAT_WORKFORCE, axis="readiness",
         raw_min=35000, raw_max=90000, weight=2.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="Average personnel cost per employee (Personalaufwand ÷ headcount)", cost_per_pull=5.0,
         rationale="Above-average pay suggests the company can attract and retain more skilled staff — a proxy for the internal capacity needed to run and absorb a pilot's findings.",
         comment="Benchmark against sector-average personnel cost per employee rather than an absolute threshold.",
         source_description="Bundesanzeiger annual filings (Personalaufwand line), Kununu/Glassdoor salary estimates", example_status="Medium-High"),
    dict(key="skilled_labour_share", automation_tier="T2", redundancy_group="WORKFORCE_CAPABILITY", label="Skilled Labour Share", category=CAT_WORKFORCE, axis="readiness",
         raw_min=0, raw_max=100, weight=2.0, phase=4, source_system="Job Postings", freshness_days=365,
         proxy="Share of roles requiring a technical/university qualification",
         rationale="A more technically skilled workforce lowers the onboarding cost of a new tool or process, directly reducing pilot execution risk.",
         comment="Overlaps with Average Salary; the two together are a stronger signal than either alone.",
         source_description="Job postings (Stepstone, Indeed), LinkedIn employee skill/role data", example_status="Medium-High"),
    dict(key="job_posting_velocity", automation_tier="T1", label="Job Posting Velocity (General)", category=CAT_WORKFORCE, axis="need",
         raw_min=0, raw_max=20, weight=2.0, phase=1, source_system="Arbeitsagentur", freshness_days=14, cost_per_pull=0.0,
         proxy="Number of active job listings across all roles",
         rationale="General hiring velocity is a growth/capacity-pressure signal, distinct from Digital/Technical Job Postings above which is specifically about absorptive readiness.",
         comment="From the original sourcing plan; kept separate from digital_job_postings since one is a NEED/growth signal and the other a READINESS signal.",
         source_description="Arbeitsagentur Jobsuche API", example_status="—"),
    dict(key="kununu_rating", automation_tier="T2", redundancy_group="WORKFORCE_STABILITY", axis_modifier="CAVEAT", label="Employer Review Sentiment (Kununu/Glassdoor)", category=CAT_WORKFORCE, axis="readiness",
         raw_min=1, raw_max=5, weight=2.0, phase=5, source_system="Kununu Reseller", freshness_days=180, cost_per_pull=2.5,
         proxy="Average rating and recent review themes on Kununu or Glassdoor",
         rationale="Persistently poor internal culture reviews can undermine execution even when leadership is genuinely open, since a pilot ultimately depends on staff engagement to succeed.",
         comment="Use as a downgrade flag rather than a standalone positive/negative score.",
         source_description="Kununu, Glassdoor", example_status="Low"),

    # ---------------------------------------------------------------- Transformation & Sustainability Signals
    dict(key="esg_reporting_recency", automation_tier="T2", redundancy_group="GOVERNANCE", label="ESG / Sustainability Reporting", category=CAT_TRANSFORMATION, axis="readiness",
         invert=True, raw_min=0, raw_max=6, weight=2.0, phase=4, source_system="Company Website", freshness_days=365,
         proxy="Years since the first sustainability/ESG report was published",
         rationale="Voluntarily adopting a new disclosure framework before it's mandatory signals broader institutional openness to adopting new frameworks in general.",
         comment="CSRD is making this mandatory for a growing share of Mittelstand companies, so this will become a weaker differentiator over the next few years — revisit periodically.",
         source_description="Company website, Bundesanzeiger (where CSRD-mandated), press releases", example_status="Recently started"),
    dict(key="energy_transition_capex", automation_tier="T1", redundancy_group="CAPEX_APPETITE", label="Energy Transition / Sustainability CapEx", category=CAT_TRANSFORMATION, axis="readiness",
         raw_min=0, raw_max=3, weight=3.0, phase=3, source_system="Bundesanzeiger", freshness_days=365,
         proxy="Disclosed spend on renewable energy, efficiency retrofits, or transition projects (% revenue)", cost_per_pull=5.0,
         rationale="Already committing capital to a transformation project is concrete evidence of present-tense change appetite, complementing the Energy Costs NEED signal.",
         comment="Pair with Energy Costs: high cost pressure plus active transition investment is a strong combined signal.",
         source_description="Bundesanzeiger annual filings, press releases, Fördermitteldatenbank energy transition grants", example_status="Recent / increasing"),

    # ---------------------------------------------------------------- Context & Segment Tags (not scored)
    dict(key="number_of_employees", automation_tier="T1", redundancy_group="SEGMENT_FILTER", label="Number of Employees", category=CAT_CONTEXT, axis="context",
         weight=0.0, phase=2, source_system="Handelsregister", freshness_days=365,
         proxy="Headcount, from Handelsregister/company register filings or LinkedIn company page",
         rationale="A segment-fit filter rather than a need/openness signal — operationalizes the Midcap-vs-SME distinction. Not scored on the need/readiness scale.",
         comment="Anchors the Midcap/SME segment cutoff used elsewhere in the tool; set explicit bounds once Stage 1 pilot data shows what actually worked.",
         source_description="Handelsregister, company website, LinkedIn company page, Bundesanzeiger filings", example_status="Moderate"),
    dict(key="product_type_tag", automation_tier="T2", redundancy_group="CLASSIFICATION", label="Type of Product", category=CAT_CONTEXT, axis="context",
         weight=0.0, phase=4, source_system="Company Website", freshness_days=730,
         proxy="Product category classification (physical good, service, hybrid, B2B component, B2C finished good)",
         rationale="A segmentation filter for matching pilot type to product type, not a need or readiness score.",
         comment="Scoring this high/low the way the rest of the table does would not be meaningful — kept as a classification tag.",
         source_description="Company website, product catalog", example_status="Physical / manufactured good (example)"),
    dict(key="family_ownership_share", automation_tier="T1", redundancy_group="SUCCESSION", axis_modifier="MODERATOR", label="Family Ownership Share", category=CAT_CONTEXT, axis="context",
         weight=0.0, phase=2, source_system="Handelsregister", freshness_days=730,
         proxy="% of equity held by the founding family",
         rationale="Doesn't predict openness either way on its own — its effect depends entirely on where the company sits on New Generation of Management (mid-succession is a strong readiness signal, an entrenched second generation is often the opposite).",
         comment="Always read jointly with the succession row rather than scoring independently.",
         source_description="Handelsregister shareholder register, Bundesanzeiger", example_status="High (fully family-owned)"),
    dict(key="recent_ma_activity", automation_tier="T1", redundancy_group="CONTEXT_EVENT", axis_modifier="MODERATOR", label="Recent M&A Activity", category=CAT_CONTEXT, axis="context",
         weight=0.0, phase=4, source_system="News/Press", freshness_days=365,
         proxy="M&A announcements (acquirer or acquired) in the past 2 years",
         rationale="Cuts both ways: recent M&A can mean active, well-capitalized growth-seeking, or a company mid-integration and too distracted for a new pilot.",
         comment="Flag rather than score until clarified at first contact — don't read directionally without checking which case applies.",
         source_description="Press releases, Handelsregister ownership change filings", example_status="Recent (past 2 years)"),
    dict(key="management_diversity", automation_tier="T2", label="Leadership Page Transparency (proxy)", category=CAT_CONTEXT, axis="context",
         weight=0.0, phase=4, source_system="Own-Site Scrape", freshness_days=180, cost_per_pull=0.0,
         proxy="Leadership-title keyword mentions on the company's own About/Team page",
         rationale="A crude transparency proxy, not a measurement of demographic diversity — kept as context rather than folded into the Leadership & Succession scores above, which are the real diversity signals once automated.",
         comment="Real automation of Management National/Cultural/Gender Diversity above (rows 3-6) is future work; this existing adapter measures something different and shouldn't be conflated with them.",
         source_description="Company own-site About/Team page scrape", example_status="—"),
    dict(key="partnership_news_count", automation_tier="T2", label="Partnership / Collaboration News Signals", category=CAT_OPENNESS, axis="readiness",
         raw_min=0, raw_max=10, weight=2.0, phase=4, source_system="Google News", freshness_days=30, cost_per_pull=0.0,
         proxy="Count of recent news results for partnership/collaboration/cooperation/pilot mentions",
         rationale="External engagement signal distinct from Press Mentions of New Launches — this searches specifically for partnership/collaboration language, closer to External Openness than to product-launch stagnation.",
         comment="From the original sourcing plan; kept in External Openness Track Record rather than remapped onto the new spreadsheet's launch-mentions row, since the two adapters search for different things.",
         source_description="Google Programmable Search API", example_status="—"),

    # ---------------------------------------------------------------- Risk & Compliance Exposure
    # New in the 2026-08 gg_indicators.json refresh (78-variable ground truth) —
    # nine rows below with no prior match in this catalog. See each row's own
    # comment for how automation_tier/phase reflects what's actually built vs.
    # just cataloged (the two diverge for several of these on day one).
    dict(key="logistics_difficulty", automation_tier="T3", label="Difficulty of Logistics", category=CAT_RISK, axis="need",
         raw_min=1, raw_max=6, weight=2.0, phase=PHASE_MANUAL, source_system="First-Contact Interview", freshness_days=730,
         proxy="Number of distribution intermediaries or handling steps between production and end customer",
         rationale="Complex, multi-step logistics is a concrete operational pain point that logistics-tech or route-optimization pilots can directly address.",
         comment="Mostly relevant to companies with physical distribution; treat as not applicable for pure services or software.",
         source_description="Annual report supply chain notes, first-contact interview, trade press on sector logistics norms",
         example_status="High / complex, multi-step"),
    dict(key="customer_concentration", automation_tier="T3", redundancy_group="SUPPLY_RISK", label="Customer Concentration", category=CAT_RISK, axis="need",
         raw_min=10, raw_max=80, weight=3.0, phase=PHASE_MANUAL, source_system="First-Contact Interview", freshness_days=730,
         proxy="Estimated % of revenue from the top 3 clients",
         rationale="High dependency on a small number of customers is a structural revenue-risk pressure point — losing one client is existential, creating urgency for diversification or resilience-focused innovation.",
         comment="Rarely disclosed precisely by private Mittelstand companies; often only confirmable at first contact. Cross-reference with sector norms — some B2B/OEM-supplier models normally run concentrated.",
         source_description="Annual report risk disclosures, first-contact interview, trade press on major client relationships",
         example_status="High (top 3 clients >50% revenue)"),
    dict(key="supplier_concentration", automation_tier="T3", redundancy_group="SUPPLY_RISK", label="Supplier Concentration", category=CAT_RISK, axis="need",
         invert=True, raw_min=1, raw_max=8, weight=2.0, phase=PHASE_MANUAL, source_system="First-Contact Interview", freshness_days=730,
         proxy="Number of alternative suppliers available for critical inputs",
         rationale="Single-source or low-diversity supplier dependency is a concrete operational vulnerability that sourcing-diversification or supply-chain-tech pilots can directly target.",
         comment="Overlaps with Rarity of Raw Materials, but distinct: that row is about scarcity of the input itself, this is about the company's own sourcing strategy and could apply even to a non-scarce input.",
         source_description="Annual report risk disclosures, first-contact interview, procurement trade press",
         example_status="High (single-source dependency)"),
    dict(key="regulatory_compliance_exposure", automation_tier="T1", redundancy_group="CONTEXT_EVENT", label="Regulatory / Compliance Exposure", category=CAT_RISK, axis="need",
         raw_min=0, raw_max=3, weight=5.0, phase=1, source_system="Regulatory Threshold Reference", freshness_days=180, cost_per_pull=0.0,
         proxy="Count of upcoming mandatory regimes the company falls under by size/sector threshold (CSRD, CBAM, EU AI Act, supply-chain due-diligence laws)",
         rationale="An imminent mandatory compliance deadline is an externally forced NEED signal with a hard timeline — often the single most reliable trigger for budget approval since it removes the 'is this worth funding' debate.",
         comment="Very high-confidence signal in principle (thresholds are public, dates are fixed) — but EU scope thresholds move (e.g. the 2025 Omnibus CSRD simplification), so no threshold table is hardcoded here yet. No adapter built this pass: cataloged as T1/Phase 1 for when a maintained threshold table is wired in, not scored from a guessed rule today. Record manually via Company Intelligence in the meantime.",
         source_description="EU/national regulatory threshold tables (CSRD scope criteria, CBAM sector list, EU AI Act), company size/sector cross-reference",
         example_status="High (new mandatory reporting regime applies soon)"),
    dict(key="insurance_claims_trend", automation_tier="T3", redundancy_group="COST_PRESSURE", label="Insurance / Claims Cost Trend", category=CAT_RISK, axis="need",
         raw_min=-10, raw_max=30, weight=1.0, phase=PHASE_MANUAL, source_system="First-Contact Interview", freshness_days=365,
         proxy="Trend in insurance or self-reported claims cost (% change)",
         rationale="Rising insurance or claims costs, especially in logistics, manufacturing, or fleet-heavy sectors, often signal an unaddressed operational risk that risk-analytics or safety-tech pilots could reduce.",
         comment="The source spreadsheet's own note flags this as 'likely first-contact only in practice' despite listing a T1-style source — downgraded to T3/manual here rather than cataloged as an automatable tier that doesn't actually hold at Mittelstand filing detail.",
         source_description="Bundesanzeiger notes where disclosed, first-contact interview, sector claims benchmark reports",
         example_status="Increasing"),
    dict(key="competitor_digital_gap", automation_tier="T2", redundancy_group="DIGITAL_COMMERCIAL", label="Competitor Digital Adoption Gap", category=CAT_DIGITAL, axis="need",
         raw_min=0, raw_max=60, weight=3.0, phase=4, source_system="Company Website", freshness_days=180,
         proxy="Point gap between this company's Digital Maturity/Online Presence scores and 2-3 named direct competitors' same scores",
         rationale="A company falling visibly behind named competitors on digital adoption faces direct competitive pressure — a sharper, more specific NEED signal than an absolute digital-maturity score alone.",
         comment="Explicitly a derived variable, not a new independent data source: re-runs Digital Maturity of Core Website and Online Market Presence against named competitors. No adapter built yet — needs a competitor-tracking data model extension first (which companies are whose competitors isn't captured anywhere today); record manually via Company Intelligence until then.",
         source_description="Derived from Digital Maturity of Core Website and Online Market Presence rows, applied to named competitors",
         example_status="Peers ahead"),
    dict(key="board_innovation_statements", automation_tier="T2", redundancy_group="STATED_INTENT", label="Board/Management Public Statements on Innovation", category=CAT_OPENNESS, axis="readiness",
         raw_min=0, raw_max=6, weight=2.0, phase=4, source_system="Google News", freshness_days=180, cost_per_pull=0.0,
         proxy="Count of specific (non-boilerplate) innovation/digital-transformation statements in press or annual-report management commentary, trailing 12 months",
         rationale="Explicit, specific public statements of intent are a stated-intent signal — weaker than revealed-preference signals like External Collaboration, but easy to source and a useful early filter.",
         comment="Distinguish genuine specificity ('we are piloting X technology in Y plant') from generic boilerplate ('we value innovation'); the adapter's keyword search is a first pass; only the former should really count, so treat this as directional, not exact.",
         source_description="Annual report management commentary (Lagebericht), press interviews, LinkedIn posts by executives",
         example_status="Frequent, specific"),

    # ---------------------------------------------------------------- Context & Segment Tags (continued)
    # Both rows below score axis=READINESS/CONTEXT with a nonzero weight in the
    # source spreadsheet, but each row's own indicator_logic explicitly frames
    # it as a sequencing/prioritization input rather than a per-company score
    # ("use it to prioritize outreach sequencing", "use it to sequence sector
    # prioritization... rather than to score individual companies") — the same
    # judgment call already applied to Number of Employees and Type of Product
    # above, kept consistent here rather than scored at face value.
    dict(key="vendor_contract_renewal_timing", automation_tier="T3", redundancy_group="DATA_READINESS", axis_modifier="MODERATOR",
         label="Vendor/Software Contract Renewal Timing", category=CAT_CONTEXT, axis="context",
         weight=0.0, phase=PHASE_MANUAL, source_system="First-Contact Interview", freshness_days=365,
         proxy="Estimated renewal window for core ERP/PM/CRM vendor contracts",
         rationale="A pilot proposal timed near an existing vendor contract's renewal window has a materially higher chance of becoming a real budget conversation than one proposed mid-contract.",
         comment="Timing variable, not a need or readiness signal on its own — use it to prioritize outreach sequencing among an otherwise similarly-scored shortlist, not to score companies independently. Kept at weight 0 / context for that reason, despite the source spreadsheet listing weight 3.",
         source_description="Job postings mentioning current systems, procurement/tender notices, first-contact interview",
         example_status="Renewal within 12 months"),
    dict(key="sector_pilot_precedent", automation_tier="T2", redundancy_group="PRIOR_COLLAB", axis_modifier="MODERATOR",
         label="Sector-Wide Innovation Pilot Precedent", category=CAT_CONTEXT, axis="context",
         weight=0.0, phase=4, source_system="News/Press", freshness_days=365,
         proxy="Number of publicly documented startup pilots or open-innovation programs already run by other companies in the same sector and size band",
         rationale="A sector with visible precedent lowers the perceived risk of being 'the first' for any individual target company — a market-level moderator, not a company-specific one.",
         comment="Sector-level, not company-level — use it to sequence which of the priority sectors to approach first, not to score individual companies. Kept at weight 0 / context for that reason, despite the source spreadsheet listing weight 2.",
         source_description="Press releases, accelerator/corporate-venturing case study pages, trade press",
         example_status="Multiple known cases in sector"),
]


def fetch_indicator_defs(db: Session) -> dict:
    """Active indicator definitions as plain dicts, keyed by signal key — the
    shape scoring.py and the views consume. Re-fetched on every call (cheap,
    ~75 rows) so a weight edit is reflected immediately on the next rerun."""
    rows = db.query(IndicatorDefinition).filter_by(is_active=True).all()
    return {r.key: r.to_dict() for r in rows}


def get_indicator_def(db: Session, key: str) -> IndicatorDefinition:
    return db.query(IndicatorDefinition).filter_by(key=key).first()


def seed_indicator_definitions(db: Session) -> int:
    """Insert any catalog rows not already present. Never overwrites an existing
    row — a weight/bounds edit made from the UI must survive every restart."""
    existing_keys = {k for (k,) in db.query(IndicatorDefinition.key).all()}
    inserted = 0
    for row in INDICATOR_SEED:
        if row["key"] not in existing_keys:
            db.add(IndicatorDefinition(**row))
            inserted += 1
    if inserted:
        db.commit()
    return inserted
