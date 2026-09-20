"""
Builds worker_dist/vienna-crawler-bundle.zip — the ONLY thing a helper's computer ever
receives: the 8 Node crawlers (compiled, no sources, no node_modules), the worker script,
and the installer pieces. Nothing from the Vienna app itself goes in.

The Streamlit app cannot run this (it has no Scraper folder and no npm), so the zip is
built here, on the developer's machine, and committed; the Crawler Setup page then adds the
per-computer config to a copy of it and hands that out. Re-run this — and commit the zip —
whenever a crawler or worker/worker.mjs changes:

    python scripts/build_worker_bundle.py            # rebuild TypeScript crawlers, then bundle
    python scripts/build_worker_bundle.py --no-build # bundle the dist/ folders as they are

What it produces
  crawlers/package.json + package-lock.json   ONE merged dependency set, installed once on the
                                              helper's machine (each crawler is 165 MB of
                                              node_modules on its own; the eight share almost
                                              everything, and Node resolves upward, so a single
                                              crawlers/node_modules serves them all)
  crawlers/<name>/dist|src, package.json      the crawler itself (+ config/ where it reads one)
  worker/…                                    worker.mjs and its launchers
  VERSION.json                                version, a content hash ("build"), the crawler
                                              list, and the pinned Node download with its SHA-256

`build` is a hash of the bundle's contents, and it is what the app compares against what each
computer reports — so "is the installed scraper set the current one" needs no manual version bump.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORKER_SRC = ROOT / "worker"
OUT_DIR = ROOT / "worker_dist"
OUT_ZIP = OUT_DIR / "vienna-crawler-bundle.zip"
PROTOCOL = 1
NODE_MAJOR = "v22"

# Copied into the bundle from worker/ (supabase_rpc.sql and install.ps1 are deliberately not:
# the first is server-side, the second travels inside the Setup file itself).
WORKER_FILES = ["worker.mjs", "start-worker.vbs"]
ROOT_FILES = {
    "stop-worker.bat": "Stop Vienna Crawler Worker.bat",
    "start-worker.bat": "Start Vienna Crawler Worker.bat",
    "uninstall.bat": "Uninstall Vienna Crawlers.bat",
    "uninstall.ps1": "uninstall.ps1",
}


def npm_cmd() -> str:
    found = shutil.which("npm.cmd") or shutil.which("npm")
    if not found:
        sys.exit("npm was not found on PATH — needed to build the crawlers and the merged lockfile.")
    return found


def crawler_dirs(crawlers_dir: Path):
    return sorted(d for d in crawlers_dir.iterdir() if (d / "package.json").is_file())


def build_typescript(crawler: Path) -> None:
    pkg = json.loads((crawler / "package.json").read_text(encoding="utf-8"))
    if "build" in (pkg.get("scripts") or {}) and (crawler / "tsconfig.json").is_file():
        print(f"  building {crawler.name} ...")
        subprocess.run([npm_cmd(), "run", "build"], cwd=crawler, check=True, capture_output=True)


def merged_dependencies(crawlers) -> dict:
    merged = {}
    for c in crawlers:
        for dep, spec in (json.loads((c / "package.json").read_text(encoding="utf-8")).get("dependencies") or {}).items():
            if merged.setdefault(dep, spec) != spec:
                sys.exit(f"Dependency conflict: {dep} is '{merged[dep]}' in one crawler and '{spec}' in {c.name}. "
                         f"Align them before bundling.")
    return dict(sorted(merged.items()))


def make_lockfile(dependencies: dict, workdir: Path) -> None:
    (workdir / "package.json").write_text(json.dumps(
        {"name": "vienna-crawlers-runtime", "private": True, "version": "1.0.0", "dependencies": dependencies}, indent=2),
        encoding="utf-8")
    subprocess.run([npm_cmd(), "install", "--package-lock-only", "--omit=dev", "--ignore-scripts",
                    "--no-audit", "--no-fund"], cwd=workdir, check=True, capture_output=True)


def pin_node() -> dict:
    """The newest Node 22 LTS, with its SHA-256 for both Windows CPU types, from nodejs.org's own
    SHASUMS256.txt — fetched now so the installer can verify the download instead of trusting it."""
    with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=30) as r:
        index = json.load(r)
    version = next(x["version"] for x in index if x["version"].startswith(NODE_MAJOR + ".") and x["lts"])
    with urllib.request.urlopen(f"https://nodejs.org/dist/{version}/SHASUMS256.txt", timeout=30) as r:
        sums = {line.split()[1]: line.split()[0] for line in r.read().decode().splitlines() if line.strip()}
    files = {}
    for arch in ("x64", "arm64"):
        name = f"node-{version}-win-{arch}.zip"
        files[arch] = {"url": f"https://nodejs.org/dist/{version}/{name}", "sha256": sums[name]}
    return {"version": version, "files": files}


def collect(crawlers_dir: Path):
    """(archive path, source path) for everything that ships, in a stable order."""
    entries = []
    for c in crawler_dirs(crawlers_dir):
        has_dist = (c / "dist" / "main.js").is_file()
        payload_dirs = ["dist", "config"] if has_dist else ["src", "config"]
        entries.append((f"crawlers/{c.name}/package.json", c / "package.json"))
        for sub in payload_dirs:
            base = c / sub
            if base.is_dir():
                for f in sorted(p for p in base.rglob("*") if p.is_file() and not p.name.endswith(".map")):
                    entries.append((f"crawlers/{c.name}/{f.relative_to(c).as_posix()}", f))
    for name in WORKER_FILES:
        entries.append((f"worker/{name}", WORKER_SRC / name))
    for src, dest in ROOT_FILES.items():
        entries.append((dest, WORKER_SRC / src))
    return entries


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--crawlers-dir", default=None, help="defaults to config.SCRAPER_CRAWLERS_DIR")
    ap.add_argument("--no-build", action="store_true", help="skip 'npm run build' in the TypeScript crawlers")
    args = ap.parse_args()

    from config import SCRAPER_CRAWLERS_DIR
    crawlers_dir = Path(args.crawlers_dir or SCRAPER_CRAWLERS_DIR)
    if not crawlers_dir.is_dir():
        sys.exit(f"Crawlers folder not found: {crawlers_dir}")
    crawlers = crawler_dirs(crawlers_dir)
    print(f"Bundling {len(crawlers)} crawlers from {crawlers_dir}")

    if not args.no_build:
        for c in crawlers:
            build_typescript(c)

    for c in crawlers:
        if not (c / "dist" / "main.js").is_file() and not (c / "src" / "main.mjs").is_file():
            sys.exit(f"{c.name} has neither dist/main.js nor src/main.mjs — build it first.")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        deps = merged_dependencies(crawlers)
        print(f"  merged dependencies: {', '.join(f'{k}@{v}' for k, v in deps.items())}")
        make_lockfile(deps, tmp)
        entries = collect(crawlers_dir)
        entries.append(("crawlers/package.json", tmp / "package.json"))
        entries.append(("crawlers/package-lock.json", tmp / "package-lock.json"))

        digest = hashlib.sha256()
        for arcname, src in entries:
            digest.update(arcname.encode())
            digest.update(src.read_bytes().replace(b"\r\n", b"\n"))  # line endings must not change the build id
        build = digest.hexdigest()[:12]

        version_text = (WORKER_SRC / "VERSION").read_text(encoding="utf-8").strip()
        info = {
            "version": version_text, "build": build, "protocol": PROTOCOL,
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "crawlers": [c.name for c in crawlers], "node": pin_node(),
        }

        OUT_DIR.mkdir(exist_ok=True)
        with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            z.writestr("VERSION.json", json.dumps(info, indent=2))
            for arcname, src in entries:
                data = src.read_bytes()
                if arcname.lower().endswith((".bat", ".vbs", ".ps1")):
                    # Windows scripts want CRLF, whatever the checkout's line endings were.
                    data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                z.writestr(arcname, data)

    size_mb = OUT_ZIP.stat().st_size / 1_048_576
    print(f"\nWrote {OUT_ZIP.relative_to(ROOT)}  ({size_mb:.2f} MB)  version {info['version']}  build {build}")
    print(f"Node pinned to {info['node']['version']}. Commit the zip so the hosted app serves it.")


if __name__ == "__main__":
    main()
