"""
Run ONE collector (or all) against ONE company, offline from the app, and score the result against a golden file.

This is how a ticket proves a collector works on real pages without waiting for the orchestrator (I30), the
search layer (I05) or the LLM pool (I04): it builds the same CollectorContext the orchestrator will build, with a
default polite fetcher and (if CRAWLER_LLM_* is configured) a minimal OpenAI-compatible LLM client, runs the
collector, validates every fact against the ledger rules, and prints the facts and the golden-benchmark delta.

It NEVER writes to the configured database. Output goes to a JSON file (default: ./intel_run_<company>.json); the
ledger is built in memory. Reading the company row is done from a JSON fixture (like scripts/crawler_bench.py),
so no database is needed at all:

  python scripts/intel_run.py --company docs/intel/golden/asco_filtri.json --collector deep-site-reader
  python scripts/intel_run.py --company docs/intel/golden/asco_filtri.json --all --out run.json

`--company` takes a golden file (its `subject` block is used) or a bench-style companies JSON list.
`--list` prints the collectors that are registered. Exit code 1 when any collector raised or any fact was refused.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intel.collector import Budget, CollectorContext, CollectorResult, check_result, discover  # noqa: E402
from intel.fetch import PoliteFetcher  # noqa: E402
from intel.ledger import Ledger  # noqa: E402


class MiniLLM:
    """Smallest useful OpenAI-compatible client over the CRAWLER_LLM_* settings. Ticket I04 replaces it with the
    real pool (many keys, per-task routing, budgets). `complete(system, user, json_mode)` returns text."""

    def __init__(self, settings: Dict[str, Any]) -> None:
        import requests
        self._requests = requests
        self.providers = [{"key": settings["api_key"], "url": settings["base_url"], "model": settings["model"]}]
        if settings.get("fallback_api_key"):
            self.providers.append({"key": settings["fallback_api_key"],
                                   "url": settings.get("fallback_base_url") or settings["base_url"],
                                   "model": settings.get("fallback_model") or settings["model"]})
        for extra in settings.get("extra_fallbacks") or []:
            self.providers.append({"key": extra.get("api_key"), "url": extra.get("base_url") or settings["base_url"],
                                   "model": extra.get("model") or settings["model"]})
        self.tokens_used = 0

    def complete(self, system: str, user: str, json_mode: bool = False, max_tokens: int = 2000) -> str:
        last = "no provider"
        for p in self.providers:
            if not p["key"]:
                continue
            body = {"model": p["model"], "max_tokens": max_tokens,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            try:
                r = self._requests.post(p["url"].rstrip("/") + "/chat/completions", json=body, timeout=60,
                                        headers={"Authorization": f"Bearer {p['key']}"})
            except self._requests.RequestException as exc:
                last = f"{type(exc).__name__}"
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                continue
            r.raise_for_status()
            data = r.json()
            self.tokens_used += (data.get("usage") or {}).get("total_tokens", 0)
            return data["choices"][0]["message"]["content"] or ""
        raise RuntimeError(f"every LLM provider failed ({last})")


def load_company(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "subject" in data:
        s = data["subject"]
        return {"id": s.get("company_id"), "legal_name": s["legal_name"], "vat": s.get("vat"),
                "website_url": s.get("website"), "country": "Italy", "city": s.get("city")}
    row = data[0] if isinstance(data, list) else data
    return {"id": row.get("id"), "legal_name": row["legal_name"], "vat": row.get("registration_number"),
            "website_url": row.get("website_url"), "country": row.get("country", "Italy"),
            "province": row.get("province"), "sector_name": row.get("sector_name"), "headcount": row.get("headcount")}


def build_llm() -> Optional[MiniLLM]:
    try:
        import config
        s = config.crawler_llm_settings()
    except Exception:
        return None
    return MiniLLM(s) if s.get("api_key") else None


def build_search(fetcher: Any, enabled: bool = True, delay: float = 1.0) -> Optional[Any]:
    """The same polite SearchClient (ticket I05) the orchestrator will give collectors, so a collector that discovers
    pages by search can be proven live here. None when disabled (--no-search) or when it cannot be built."""
    if not enabled:
        return None
    try:
        from intel.search import SearchClient
        return SearchClient(fetcher=fetcher, delay=delay)
    except Exception:
        return None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--company", help="golden file or companies JSON")
    ap.add_argument("--collector", action="append", default=[], help="collector name (repeatable)")
    ap.add_argument("--all", action="store_true", help="run every registered collector")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--out", help="write facts + run report here")
    ap.add_argument("--no-llm", action="store_true", help="do not build an LLM client even if a key is configured")
    ap.add_argument("--no-search", action="store_true", help="do not give collectors a web search client")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests to one host")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    registry = discover()
    if a.list:
        for c in registry.values():
            print(f"{c.name:28s} {c.ticket:5s} produces {', '.join(c.produces)}  requires {', '.join(c.requires)}")
        print(f"{len(registry)} collector(s) registered")
        return 0
    if not a.company:
        print("error: --company is required", file=sys.stderr)
        return 2
    chosen = list(registry.values()) if a.all else [registry[n] for n in a.collector if n in registry]
    unknown = [n for n in a.collector if n not in registry]
    if unknown or not chosen:
        print(f"error: no such collector(s) {unknown or '(none given)'}; registered: {sorted(registry)}", file=sys.stderr)
        return 2

    company = load_company(Path(a.company))
    ledger = Ledger(company.get("id"))
    fetcher = PoliteFetcher(delay=a.delay)
    llm = None if a.no_llm else build_llm()
    search = build_search(fetcher, enabled=not a.no_search, delay=a.delay)
    exit_code, report = 0, []
    for coll in chosen:
        missing = [r for r in coll.requires if r == "llm" and llm is None]
        if missing:
            print(f"SKIP {coll.name}: needs {missing} (no CRAWLER_LLM_API_KEY configured, or --no-llm)")
            report.append({"collector": coll.name, "skipped": f"needs {missing}"})
            continue
        ctx = CollectorContext(company=company, ledger=ledger, budget=Budget(), llm=llm, search=search, fetch=fetcher,
                               log=lambda m, n=coll.name: print(f"  [{n}] {m}"))
        t0 = time.time()
        try:
            result: CollectorResult = coll.run(ctx)
        except Exception as exc:   # a crashing collector is reported, never silently ignored
            print(f"ERROR {coll.name}: {type(exc).__name__}: {exc}")
            report.append({"collector": coll.name, "error": f"{type(exc).__name__}: {exc}"})
            exit_code = 1
            continue
        refused = check_result(coll, result) + ledger.add_many(
            f for f in result.facts if coll.may_emit(f.key))
        if refused:
            exit_code = 1
        print(f"{coll.name}: {len(result.facts)} fact(s), {len(refused)} refused, {result.pages_fetched} page(s), "
              f"{len(result.errors)} error(s), {time.time() - t0:.0f}s")
        for line in refused[:10]:
            print(f"   REFUSED: {line}")
        for line in result.errors[:10]:
            print(f"   note: {line}")
        report.append({"collector": coll.name, "facts": len(result.facts), "refused": refused, "errors": result.errors,
                       "queries": result.queries, "pages": result.pages_fetched, "seconds": round(time.time() - t0, 1)})
    print(f"\nledger: {len(ledger.facts)} fact(s) in {len(ledger.by_category())} categorie(s); "
          f"{len(ledger.conflicts())} conflict(s)")
    for fact in sorted(ledger.facts.values(), key=lambda f: (f.key, f.id))[:80]:
        print(f"  {fact.grade} {fact.key:34s} {str(fact.value)[:90]}")
    out = Path(a.out or f"intel_run_{(company.get('vat') or 'company')}.json")
    out.write_text(json.dumps({"company": company, "report": report, "ledger": ledger.to_dict()},
                              indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nwrote {out}  (score it: python scripts/intel_gap.py --golden <golden> --corpus-file {out})")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
