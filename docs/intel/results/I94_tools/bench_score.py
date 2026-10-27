"""bench_score.py WORKTREE REPORT.json : web-only score of a fresh crawler_bench report per golden company."""
import glob
import json
import sys
from pathlib import Path

WT = Path(sys.argv[1])
sys.path.insert(0, str(WT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import intel_gap as g  # noqa: E402

rep = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
names = sorted({e.get("company") for e in rep})
print("companies in report:", names)
lanes = {}
for e in rep:
    lanes.setdefault(e.get("company"), []).append((e.get("lane"), len(e.get("signals", [])), e.get("elapsed_s"), (e.get("source_health") or {}).get("last_error")))
for p in sorted(glob.glob(str(WT / "docs/intel/golden/*.json"))):
    gold = g.load_golden(Path(p))
    legal = gold["subject"]["legal_name"]
    sub = [e for e in rep if (e.get("company") or "").strip().lower() == legal.strip().lower()]
    if not sub:
        # fall back to the DB name (golden legal_name may differ in the long form)
        first = legal.split()[0].lower()
        sub = [e for e in rep if (e.get("company") or "").lower().startswith(first)]
    tmp = Path(sys.argv[2]).with_name("tmp_one.json")
    tmp.write_text(json.dumps(sub), encoding="utf-8")
    chunks = g.chunks_from_bench_report(tmp)
    sc = g.score(gold, chunks, web_only=True)
    held = [r["id"] for r in sc["results"] if r["captured"]]
    print(f"{Path(p).name}: bench web-only {sc['captured']} of {sc['facts']} ({sc['weighted_coverage']*100:.0f}% wt) held={held} lanes={lanes.get(sub[0]['company']) if sub else None}")
