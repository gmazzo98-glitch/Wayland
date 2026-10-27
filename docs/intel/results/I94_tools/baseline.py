"""Re-run the DB baseline (read-only SELECTs via intel_gap) for each golden file and print captured facts with the chunk that satisfied them."""
import glob
import json
import os
import sys
from pathlib import Path

WT = Path(sys.argv[1])
os.chdir(WT)
sys.path.insert(0, str(WT / "scripts"))
sys.path.insert(0, str(WT))
import intel_gap as g  # noqa: E402
from database import get_db_session  # noqa: E402

out_dir = Path(sys.argv[2])
out_dir.mkdir(parents=True, exist_ok=True)
verbose = "-v" in sys.argv
db = get_db_session()
summary = []
for p in sorted(glob.glob(str(WT / "docs/intel/golden/*.json"))):
    name = Path(p).name
    gold = g.load_golden(Path(p))
    cid = gold["subject"]["company_id"]
    chunks = g.chunks_from_session(db, cid)
    full = g.score(gold, chunks, web_only=False)
    web = g.score(gold, chunks, web_only=True)
    summary.append((name, full, web, len(chunks)))
    (out_dir / f"{name}.db.json").write_text(json.dumps({"full": full, "web": web}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"{name}: facts {full['facts']} | DB held {full['captured']} ({full['weighted_coverage']*100:.0f}% wt) | web-only {web['captured']} ({web['weighted_coverage']*100:.0f}% wt) | chunks {len(chunks)}")
    if verbose:
        for r in full["results"]:
            if r["captured"]:
                print("    HELD", r["id"], f"w{r['weight']}", r["claim"][:70], "<-", r["where"])
