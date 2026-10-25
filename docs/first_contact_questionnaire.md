# First-contact questionnaire (generated from the Proxy column)

A blank or declined answer is recorded as unknown, never as a low value. 'Does not apply' is recorded as not_applicable.

## Interdepartmental collaboration: Not directly observable pre-contact in most cases; approximated via org-chart flatness or job postings mentioning 'cross-functional teams'.

- Variable: `interdepartmental_collaboration` (Sheet1 row 11, Scored, READINESS)
- Sheet1 status being tested: High
- Answers:
  - [ ] High -> observed
  - [ ] Not 'High' -> observed
  - [ ] Not known / not willing to share -> unknown

## Verticality in the structure: Number of management layers between CEO and frontline staff, or self-reported at first contact.

- Variable: `org_verticality` (Sheet1 row 12, Scored, READINESS)
- Sheet1 status being tested: Low
- Answers:
  - [ ] Low -> observed
  - [ ] Not 'Low' -> observed
  - [ ] Not known / not willing to share -> unknown

## Difficulty of logistics: Number of distribution intermediaries or handling steps between production and end customer, from supply chain disclosures or first-contact interview.

- Variable: `logistics_difficulty` (Sheet1 row 32, Scored, NEED)
- Sheet1 status being tested: High / complex, multi-step
- Answers:
  - [ ] High / complex, multi-step -> observed
  - [ ] Not 'High / complex, multi-step' -> observed
  - [ ] Not known / not willing to share -> unknown

## Supply chain digitization: Presence of EDI, real-time tracking, or supplier portal technology, inferred from job postings or first-contact interview.

- Variable: `supply_chain_digitization` (Sheet1 row 65, Scored, NEED)
- Sheet1 status being tested: Low / manual
- Answers:
  - [ ] Low / manual -> observed
  - [ ] Not 'Low / manual' -> observed
  - [ ] Not known / not willing to share -> unknown

## Depth of internal approval chain for new vendors/pilots: Estimated from company size, org verticality, and first-contact disclosure of procurement process.

- Variable: `approval_chain_depth` (Sheet1 row 70, Gate, READINESS)
- Sheet1 status being tested: High (multiple approval layers)
- Answers:
  - [ ] High (multiple approval layers) -> observed
  - [ ] Not 'High (multiple approval layers)' -> observed
  - [ ] Not known / not willing to share -> unknown

## System integration across core functions (ERP, MES, CMMS, PLM, quality, finance): Degree to which planning, production, maintenance, quality and finance data sit in connected systems rather than in spreadsheets and re-keyed exports. Inferred from the number and mix of systems named in job postings, integration or interface roles, and first-contact disclosure.

- Variable: `system_integration` (Sheet1 row 72, Scored, NEED)
- Sheet1 status being tested: Siloed (manual reconciliation between systems)
- Answers:
  - [ ] siloed -> observed
  - [ ] integrated -> observed
  - [ ] Not known / not willing to share -> unknown

## Maintenance management digitization: Whether maintenance is planned and tracked in a CMMS or asset management system versus run reactively. Proxied by job postings for maintenance planners or CMMS skills and by first-contact disclosure. Applies only to asset-heavy firms; mark N/A for pure engineering and EPC services.

- Variable: `maintenance_digitization` (Sheet1 row 73, Scored, NEED)
- Sheet1 status being tested: Reactive and undocumented (paper or spreadsheet logs, no CMMS)
- Applicability: ASSUMPTION (flag in report): defaults taken from the integration brief. Applicable: manufacturing divisions in scope for the landing bay (28, 29, 30), industrial services and repair (33), energy (35). Not applicable: engineering and EPC services (41, 42, 43, 71). Every other division is undetermined, not silently applicable or not.
- Answers:
  - [ ] reactive undocumented -> observed
  - [ ] planned in cmms -> observed
  - [ ] Not known / not willing to share -> unknown
  - [ ] Does not apply to this company -> not_applicable

## Prior digital maturity self-assessment: Whether the company has taken a structured digital maturity assessment (Test Your Industry 4.0 / DREAMY via a Digital Innovation Hub, or a comparable audit) and the level obtained.

- Variable: `digital_maturity_self_assessment` (Sheet1 row 75, Scored, READINESS)
- Sheet1 status being tested: Completed, low to medium maturity (DREAMY levels 1 to 3)
- Answers:
  - [ ] completed low to medium -> observed
  - [ ] completed high maturity -> observed
  - [ ] Not known / not willing to share -> unknown

