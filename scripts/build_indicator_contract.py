"""
Writes the extended variable schema / data contract and the first-contact questionnaire section
from gg_indicators/source/Indicators.xlsx + gg_indicators/settings/*.json:

    docs/indicator_contract.json            every sheet row: key, axis, polarity, role, tier, groups, status
                                            mapping, applicability, source_it, and where it differs from the
                                            live catalog (indicators.INDICATOR_SEED)
    docs/first_contact_questionnaire.md     questionnaire section for the "First contact only" rows
    docs/first_contact_questionnaire.json   same, machine-readable

Never writes to the workbook. Like generate_indicator_catalog.py it never needs the database.

Usage:
    python scripts/build_indicator_contract.py                 # write the three files
    python scripts/build_indicator_contract.py --check         # exit 1 if any committed file is stale
    python scripts/build_indicator_contract.py --workbook PATH # read a different copy of Indicators.xlsx
    python scripts/build_indicator_contract.py --persist-new-keys
        # append keys derived for brand-new sheet rows to gg_indicators/settings/key_overrides.json
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gg_indicators.contract import build_contract  # noqa: E402
from gg_indicators.questionnaire import build_first_contact_section, render_markdown  # noqa: E402
from gg_indicators.registry import IndicatorRegistry  # noqa: E402
from gg_indicators.schema import DEFAULT_WORKBOOK, SETTINGS_DIR  # noqa: E402
from indicators import INDICATOR_SEED  # noqa: E402

DOCS = ROOT / "docs"


def render_outputs(reg):
    contract = json.dumps(build_contract(reg, INDICATOR_SEED), indent=2, ensure_ascii=False) + "\n"
    section = build_first_contact_section(reg)
    return {
        DOCS / "indicator_contract.json": contract,
        DOCS / "first_contact_questionnaire.md": render_markdown(section) + "\n",
        DOCS / "first_contact_questionnaire.json": json.dumps(section, indent=2, ensure_ascii=False) + "\n",
    }


def persist_new_keys(reg) -> int:
    path = SETTINGS_DIR / "key_overrides.json"
    table = json.loads(path.read_text(encoding="utf-8"))
    added = 0
    for s in reg.specs:
        if s.key_source == "derived_unpersisted":
            table["keys"].append({"row": s.sheet_row, "label": s.label, "key": s.key, "in_catalog_seed": False})
            added += 1
    if added:
        path.write_text(json.dumps(table, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return added


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--persist-new-keys", action="store_true")
    args = ap.parse_args()

    reg = IndicatorRegistry.load(args.workbook)
    if args.persist_new_keys:
        print(f"persisted {persist_new_keys(reg)} new key(s)")
        reg = IndicatorRegistry.load(args.workbook)

    outputs = render_outputs(reg)
    if args.check:
        stale = [p for p, text in outputs.items() if not p.exists() or p.read_text(encoding="utf-8") != text]
        if stale:
            print("stale: " + ", ".join(str(p.relative_to(ROOT)) for p in stale) + " - run: python scripts/build_indicator_contract.py")
            sys.exit(1)
        print("indicator contract files are up to date")
        return
    DOCS.mkdir(exist_ok=True)
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    unpersisted = [s.key for s in reg.specs if s.key_source == "derived_unpersisted"]
    if unpersisted:
        print(f"NOTE: {len(unpersisted)} key(s) derived but not persisted: {unpersisted} (use --persist-new-keys)")


if __name__ == "__main__":
    main()
