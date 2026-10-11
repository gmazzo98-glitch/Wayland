"""Benchmark a company's priced catalog against its hand-named competitors.

The indicator's raw 0..15 value measures commoditization (higher means more
comparable competing products and therefore greater NEED), despite the label
"product_differentiation". For each usable peer, count distinct peer products
with the same normalized category and currency as an own product and a *fixed*
price within 20% of that own price. The score is
``min(15, 15 * mean(peer comparable counts) / own distinct fixed-price count)``.
Thus adding distinct priced own products with peer overlap held constant lowers
commoditization. Category breadth enters via the denominator and category
matching; price-band overlap is explicit. No price, no matching category, a
failed crawl, or an unconfirmed absence never becomes a zero-valued signal.
Only observed products count; partial crawls are identified in evidence and
receive lower confidence. Named peers are recorded by a human, not discovered.
"""

from __future__ import annotations

from math import isfinite
from sqlalchemy.orm import Session

from adapters.base import run_adapter
from models import RawImportRecord
from scrapers import product_catalog_crawler as catalog
from scrapers.node_crawler_base import CrawlerRunError, rows_for_company, run_ts_crawler, save_crawler_blob

SOURCE_NAME = "Competitor Catalog Benchmark"
DATASET_NAME = "competitor_catalog_benchmark"
PHASE = 7
MAX_COMPETITORS = 3
PRICE_BAND = 0.20
RUN_TIMEOUT_SECONDS = 280


def _crawler_env() -> dict[str, str]:
    """Pass the same optional LLM chain as the own-catalog wrapper."""
    if not catalog.CRAWLER_LLM_API_KEY:
        return {}
    env = {
        "LLM_API_KEY": catalog.CRAWLER_LLM_API_KEY,
        "LLM_BASE_URL": catalog.CRAWLER_LLM_BASE_URL,
        "LLM_MODEL": catalog.CRAWLER_LLM_MODEL,
    }
    if catalog.CRAWLER_LLM_FALLBACK_API_KEY:
        env["LLM_FALLBACK_API_KEY"] = catalog.CRAWLER_LLM_FALLBACK_API_KEY
        if catalog.CRAWLER_LLM_FALLBACK_BASE_URL:
            env["LLM_FALLBACK_BASE_URL"] = catalog.CRAWLER_LLM_FALLBACK_BASE_URL
        if catalog.CRAWLER_LLM_FALLBACK_MODEL:
            env["LLM_FALLBACK_MODEL"] = catalog.CRAWLER_LLM_FALLBACK_MODEL
        for extra in catalog.CRAWLER_LLM_EXTRA_FALLBACKS:
            env[f"LLM_FALLBACK{extra['suffix']}_API_KEY"] = extra["api_key"]
            if extra["base_url"]:
                env[f"LLM_FALLBACK{extra['suffix']}_BASE_URL"] = extra["base_url"]
            if extra["model"]:
                env[f"LLM_FALLBACK{extra['suffix']}_MODEL"] = extra["model"]
    return {key: value for key, value in env.items() if value}


def _crawl_competitor(competitor) -> dict:
    rows = run_ts_crawler(
        catalog.CRAWLER_DIR,
        [{"company_id": competitor.id, "homepage_url": competitor.homepage_url}],
        env_overrides=_crawler_env(), run_timeout=RUN_TIMEOUT_SECONDS,
    )
    matches = rows_for_company(rows, competitor.id)
    if not matches:
        raise CrawlerRunError(f"product-catalog-crawler returned no row for competitor '{competitor.name}'")
    row = matches[0]
    if row.get("error"):
        raise CrawlerRunError(str(row["error"]))
    return row


def _priced_products(row: dict) -> list[dict]:
    """Distinct, category-labelled products with an actual fixed numeric price."""
    if not isinstance(row, dict) or row.get("error") or (row.get("field_status") or {}).get("products") in ("not_found", "error"):
        return []
    found = {}
    for p in row.get("products") or []:
        if not isinstance(p, dict):
            continue
        category = " ".join(str(p.get("category") or "").lower().split())
        currency = str(p.get("price_currency") or "").upper().strip()
        price = p.get("price_value")
        if not category or not currency or p.get("price_type") != "fixed":
            continue
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not isfinite(price) or price <= 0:
            continue
        identity = " ".join(str(p.get("sku") or p.get("product_name") or "").lower().split())
        if not identity:
            continue
        found[(identity, category)] = {
            "name": p.get("product_name"), "sku": p.get("sku"), "category": category,
            "currency": currency, "price": float(price), "source_url": p.get("source_url"),
        }
    return list(found.values())


def _derive_signals(own_row: dict, competitor_results: list[dict]) -> dict:
    """Compute the score and auditable evidence from catalog rows; no I/O."""
    own = _priced_products(own_row)
    if not own:
        return {}
    own_categories = {p["category"] for p in own}
    usable, skipped = [], []
    for entry in competitor_results:
        peer = entry["competitor"]
        row = entry.get("row") or {}
        reason = entry.get("error") or row.get("error")
        products = _priced_products(row)
        if not reason and not products:
            reason = "no category-labelled products with confirmed fixed prices"
        if not reason and not ({p["category"] for p in products} & own_categories):
            reason = "no common catalog category; product comparability cannot be established"
        if reason:
            skipped.append({"name": peer.name, "homepage_url": peer.homepage_url, "reason": str(reason)})
            continue
        comparable = []
        for p in products:
            near = next((o for o in own if o["category"] == p["category"] and o["currency"] == p["currency"]
                         and abs(o["price"] - p["price"]) / max(o["price"], p["price"]) <= PRICE_BAND), None)
            if near:
                comparable.append({"peer_product": p, "own_product": near})
        usable.append({
            "name": peer.name, "homepage_url": peer.homepage_url,
            "sku_count": len(row.get("products") or []),
            "category_breadth": len({p.get("category") for p in row.get("products") or [] if p.get("category")}),
            "priced_sku_count": len(products), "comparable_sku_count": len(comparable),
            "comparable_products": comparable, "is_partial": bool(row.get("is_partial")),
        })
    if not usable:
        return {}
    # A partial crawl cannot establish a true zero overlap; only score zero
    # when all observed peer catalogs were complete.
    comparable_total = sum(p["comparable_sku_count"] for p in usable)
    if comparable_total == 0 and (own_row.get("is_partial") or any(p["is_partial"] for p in usable)):
        return {}
    mean_comparable = comparable_total / len(usable)
    score = round(min(15.0, 15.0 * mean_comparable / len(own)), 2)
    summary = (f"{len(own)} own distinct fixed-price SKUs in {len(own_categories)} categories vs "
               f"{mean_comparable:.1f} comparable peer SKUs on average across {len(usable)} named competitor(s); "
               f"commoditization score {score}/15")
    return {"product_differentiation": {
        "value": score, "status": "present", "summary": summary,
        "evidence": {
            "method": "own and named-peer product-catalog-crawler rows; exact normalized category and currency, fixed prices within 20%",
            "formula": "min(15, 15 * mean(peer comparable SKU counts) / own distinct fixed-price SKU count)",
            "own_sku_count": len(own_row.get("products") or []),
            "own_category_breadth": len({p.get("category") for p in own_row.get("products") or [] if p.get("category")}),
            "own_priced_sku_count": len(own), "own_is_partial": bool(own_row.get("is_partial")),
            "competitors_benchmarked": usable, "competitors_skipped": skipped,
        },
    }}


def sync_competitor_catalog_benchmark(company, db_session: Session) -> dict:
    competitors = [c for c in (company.competitors or []) if c.homepage_url][:MAX_COMPETITORS]
    if not competitors:
        return {"status": "skipped", "reason": "No named competitor with a homepage recorded."}
    rec = db_session.query(RawImportRecord).filter_by(company_id=company.id, dataset_name=catalog.DATASET_NAME).first()
    own_row = rec.raw_row if rec else None
    if not own_row or not _priced_products(own_row):
        return {"status": "skipped", "reason": "This company's own product catalog has no confirmed category-labelled fixed-price products yet."}
    captured = {}

    def _fetch_live(c):
        results = []
        for competitor in competitors:
            try:
                row = _crawl_competitor(competitor)
                results.append({"competitor": competitor, "row": row, "error": None})
            except Exception as exc:  # one failed peer does not erase successful peers
                results.append({"competitor": competitor, "row": None, "error": str(exc)})
        captured["results"] = results
        signals = _derive_signals(own_row, results)
        if not signals:
            raise CrawlerRunError("No comparable, confirmed-price competitor catalog evidence")
        evidence = signals["product_differentiation"]["evidence"]
        count = len(evidence["competitors_benchmarked"])
        partial = evidence["own_is_partial"] or any(p["is_partial"] for p in evidence["competitors_benchmarked"])
        return {"signals": signals,
                "raw_payload": {"competitors_recorded": len(competitors), "competitors_usable": count},
                "confidence": 0.35 if partial else (0.6 if count >= 2 else 0.45)}

    def _simulate(c):
        return {"signals": {}, "raw_payload": {"note": "competitor catalog benchmark unavailable"}, "confidence": 0.5}

    result = run_adapter(db_session, company, SOURCE_NAME, PHASE, credentials_ok=True,
                         fetch_live=_fetch_live, simulate=_simulate,
                         timeout=len(competitors) * RUN_TIMEOUT_SECONDS + 60)
    if captured.get("results"):
        save_crawler_blob(db_session, company, DATASET_NAME, {
            "own_catalog_updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
            "competitors": [{"name": e["competitor"].name, "homepage_url": e["competitor"].homepage_url,
                             "row": e["row"], "error": e["error"]} for e in captured["results"]],
        }, source_filename="Competitor Catalog Benchmark — per-competitor product-catalog-crawler rows")
    return result
