"""
The deep-intelligence collection layer (docs/INTEL_ROADMAP.md).

Collectors (crawlers, adapters, research agents) write what they find into a per-company evidence ledger
(intel/ledger.py); nothing in this package writes a score, a signal or a Need/Readiness value. The diagnosis
package (diagnosis/) and the dossier read the ledger.
"""

SCHEMA_VERSION = 1

# One RawImportRecord per company holds the ledger, like every other crawler blob (no schema change).
DATASET_NAME = "crawler_deep_profile"
