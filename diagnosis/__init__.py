"""
Diagnosis layer: from everything the tool knows about a company to a first-call preparation pack.

Read docs/DIAGNOSIS_MASTERPLAN.md first (what this is, what it is not, the gaps), then docs/DIAGNOSIS_TICKETS.md
(the work, split into tickets that can run in parallel). The shape of the data flowing through this package is
fixed in contracts.py; the vocabulary of facts is fixed in factkeys.py. Both are versioned: change them only
through a contract-change ticket, never inside a feature ticket.

Pipeline:  readers -> CompanyFacts -> hypotheses (+ triggers) -> pilot matching -> synthesis -> CallPack
Nothing in this package writes a SignalRecord or changes a score: a diagnosis is a layer on top of NEED and
READINESS, never blended into either (see project-vienna-overview).

Extension points are auto-discovered (registry.discover): drop a module into readers/, hypotheses/ or
triggers/ and decorate its function; no central file needs editing, which is what lets several agents add
to this package at the same time without touching each other's files.
"""

SCHEMA_VERSION = 1
