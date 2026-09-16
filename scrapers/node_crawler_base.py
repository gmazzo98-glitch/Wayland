"""
Shared subprocess harness for the 8 Node.js/TypeScript Crawlee crawlers that
live in the sibling Scraper/crawlers/ folder (config.SCRAPER_CRAWLERS_DIR) —
not part of this git repo. Each crawler wrapper module (scrapers/*_crawler.py)
uses this to shell out, write an input CSV, and read the resulting Crawlee
dataset back, then feeds the parsed rows into adapters.base.run_adapter the
same way every native Python scraper/adapter already does.

Every TS crawler here follows the same on-disk contract (see
Scraper/CRAWLER_AUDIT.md): `npm run build` -> dist/main.js, `node dist/main.js
<input-path>` reads a CSV/JSON file and Crawlee's Dataset.pushData() writes
one JSON file per row under storage/datasets/default/. CRAWLEE_STORAGE_DIR is
pointed at a fresh temp dir per call specifically so concurrent/rerun calls
never read a stale file left over from a previous invocation.
"""

import csv
import io
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from config import SCRAPER_CRAWLERS_DIR
from models import RawImportRecord

DEFAULT_BUILD_TIMEOUT = 180
DEFAULT_RUN_TIMEOUT = 90


class CrawlerRunError(RuntimeError):
    """Raised for any failure to invoke or parse a Node crawler — always caught
    by the calling wrapper's fetch_live() and turned into a simulate() fallback
    by adapters.base.run_adapter, never surfaced as a crash."""


def crawler_dir(name: str) -> Path:
    d = Path(SCRAPER_CRAWLERS_DIR) / name
    if not d.is_dir():
        raise CrawlerRunError(
            f"Crawler folder not found: {d} — check SCRAPER_CRAWLERS_DIR / that "
            f"the Scraper folder is checked out next to this project."
        )
    return d


def _run(args: List[str], cwd: Path, env: Dict[str, str], timeout: int) -> subprocess.CompletedProcess:
    # npm on Windows resolves to npm.cmd, which CreateProcess can't launch directly
    # without going through a shell — list2cmdline is the same argv-quoting Python's
    # own subprocess module uses, so this is safe on both platforms.
    if os.name == "nt":
        cmd = subprocess.list2cmdline(args)
        return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                               timeout=timeout, env=env, shell=True)
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                           timeout=timeout, env=env)


def ensure_built(name: str, timeout: int = DEFAULT_BUILD_TIMEOUT) -> None:
    """No-op if dist/main.js already exists (all 7 TS crawlers ship pre-built);
    otherwise runs `npm run build` once. linkedin-profile-crawler has no build
    step (plain .mjs) and never reaches here."""
    d = crawler_dir(name)
    if (d / "dist" / "main.js").exists():
        return
    npm = shutil.which("npm") or "npm"
    result = _run([npm, "run", "build"], cwd=d, env=dict(os.environ), timeout=timeout)
    if result.returncode != 0:
        raise CrawlerRunError(f"npm run build failed for {name}: {(result.stderr or result.stdout)[-1500:]}")


def _write_input_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        raise CrawlerRunError("No input rows to write — nothing to crawl")
    fieldnames = list(rows[0].keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    path.write_text(buf.getvalue(), encoding="utf-8")


def _read_dataset(dataset_dir: Path) -> List[Dict[str, Any]]:
    if not dataset_dir.is_dir():
        return []
    rows = []
    for f in sorted(dataset_dir.glob("*.json")):
        try:
            rows.append(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return rows


def run_ts_crawler(
    name: str,
    input_rows: List[Dict[str, Any]],
    extra_args: Optional[Iterable[str]] = None,
    env_overrides: Optional[Dict[str, str]] = None,
    run_timeout: int = DEFAULT_RUN_TIMEOUT,
    build_timeout: int = DEFAULT_BUILD_TIMEOUT,
) -> List[Dict[str, Any]]:
    """
    Writes input_rows to a temp CSV, runs `node dist/main.js <csv> [extra_args]`
    in the given crawler's folder with an isolated CRAWLEE_STORAGE_DIR, and
    returns every row Dataset.pushData() wrote, in file order.

    Raises CrawlerRunError on a missing folder, failed build, non-zero exit, or
    timeout — callers (each scrapers/*_crawler.py's _fetch_live) let this
    propagate so run_adapter's existing fetch-then-fallback-to-simulate contract
    handles it, exactly like a requests.RequestException from any other adapter.
    """
    ensure_built(name, timeout=build_timeout)
    d = crawler_dir(name)
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"vienna_{name.replace('-', '_')}_"))
    try:
        input_path = tmp_dir / "input.csv"
        _write_input_csv(input_path, input_rows)
        storage_dir = tmp_dir / "storage"

        node = shutil.which("node") or "node"
        args = [node, "dist/main.js", str(input_path), *list(extra_args or [])]

        env = dict(os.environ)
        env["CRAWLEE_STORAGE_DIR"] = str(storage_dir)
        env.update(env_overrides or {})

        try:
            result = _run(args, cwd=d, env=env, timeout=run_timeout)
        except subprocess.TimeoutExpired as e:
            raise CrawlerRunError(f"{name} timed out after {run_timeout}s") from e

        if result.returncode != 0:
            raise CrawlerRunError(
                f"{name} exited {result.returncode}: {(result.stderr or result.stdout)[-1500:]}"
            )

        return _read_dataset(storage_dir / "datasets" / "default")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run_node_entrypoint(
    name: str,
    entry_relpath: str,
    cli_args: List[str],
    env_overrides: Optional[Dict[str, str]] = None,
    run_timeout: int = DEFAULT_RUN_TIMEOUT,
) -> List[Dict[str, Any]]:
    """
    Lower-level sibling of run_ts_crawler for the one crawler that doesn't fit
    the CSV-input/dist-build pattern (linkedin-profile-crawler: plain .mjs, no
    build step, takes profile URLs directly as CLI args instead of an input
    file). Runs `node <entry_relpath> <cli_args...>` with an isolated
    CRAWLEE_STORAGE_DIR and returns whatever Dataset.pushData() wrote.
    """
    d = crawler_dir(name)
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"vienna_{name.replace('-', '_')}_"))
    try:
        storage_dir = tmp_dir / "storage"
        node = shutil.which("node") or "node"
        args = [node, entry_relpath, *cli_args]

        env = dict(os.environ)
        env["CRAWLEE_STORAGE_DIR"] = str(storage_dir)
        env.update(env_overrides or {})

        try:
            result = _run(args, cwd=d, env=env, timeout=run_timeout)
        except subprocess.TimeoutExpired as e:
            raise CrawlerRunError(f"{name} timed out after {run_timeout}s") from e

        if result.returncode != 0:
            raise CrawlerRunError(
                f"{name} exited {result.returncode}: {(result.stderr or result.stdout)[-1500:]}"
            )

        return _read_dataset(storage_dir / "datasets" / "default")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def rows_for_company(rows: List[Dict[str, Any]], company_id: str, key: str = "company_id") -> List[Dict[str, Any]]:
    """Filters a crawler's full output batch down to the rows belonging to one
    company — most crawlers return exactly one, review-crawler/directory-listing-crawler
    can return several (one per source_type / directory_url)."""
    return [r for r in rows if str(r.get(key)) == str(company_id)]


def save_crawler_blob(db: Session, company, dataset_name: str, raw_row: Dict[str, Any],
                       source_filename: Optional[str] = None) -> None:
    """
    The blob side of the same twofold structured+blob pattern
    company_service.apply_data_import() already uses for spreadsheet imports
    (see RawImportRecord's docstring in models.py) — reused as-is here rather
    than a new table, per the same "one place for all of a company's raw
    ingestion payloads" rule. One row per (company, dataset_name); re-running
    the same crawler for the same company overwrites its row, same conflict
    rule as the spreadsheet-import side.

    Lives here (not company_service.py) to avoid a circular import — company_service
    imports each scrapers/*_crawler.py module, so those modules can't import back
    from company_service.
    """
    rec = db.query(RawImportRecord).filter_by(company_id=company.id, dataset_name=dataset_name).first()
    if not rec:
        rec = RawImportRecord(company_id=company.id, dataset_name=dataset_name)
        db.add(rec)
    rec.source_filename = source_filename or f"crawler:{dataset_name}"
    rec.raw_row = raw_row
    rec.updated_at = datetime.utcnow()
    db.commit()
