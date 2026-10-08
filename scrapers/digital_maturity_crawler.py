"""
Wraps Scraper/crawlers/digital-maturity-crawler (Phase 7). Assesses a
company's website via the Wayback Machine (snapshot history, a structural-
diff redesign-year estimate), optionally BuiltWith (tech stack), and
optionally a vision-LLM assessment of one homepage screenshot (design
modernity, chatbot/personalization presence) — falls back to lightweight
heuristics without a BuiltWith key, and skips the visual pass entirely
without a CRAWLER_VISION_LLM_API_KEY.

Feeds: website_digital_maturity (years since last major redesign — the raw
value IS the need signal, no inversion: an old redesign directly means high
digital-neglect need), online_market_presence (0-5 composite of ecommerce +
social presence + snapshot activity, matching that indicator's own proxy
text). Distinct from scrapers/wappalyzer_local.py's tech_stack_intensity,
which is a live regex signature match against the site's own HTML/headers,
not a Wayback-based history read — the two measure different things and both
stay wired.

website_digital_maturity's Wayback source produces an estimate for a
minority of companies (archive.org simply never captured many of these sites
often enough — see that crawler's own README). The vision pass is a second,
qualitative source for the SAME indicator (_VISION_SCORE_TO_AGE_YEARS below),
used only to fill in a value the Wayback method left blank — it never
overrides a real Wayback-measured age, and the full visual read (chatbot/
personalization/ecommerce-UX signals, not themselves scored indicators yet)
is always attached as evidence on whichever source actually wrote the value,
for a human to read in the Company Intelligence audit dialog.
"""

from datetime import datetime
from sqlalchemy.orm import Session

from adapters.base import run_adapter
from config import (
    CRAWLER_VISION_LLM_API_KEY, CRAWLER_VISION_LLM_BASE_URL, CRAWLER_VISION_LLM_MODEL,
    CRAWLER_VISION_LLM_FALLBACK_API_KEY, CRAWLER_VISION_LLM_FALLBACK_BASE_URL, CRAWLER_VISION_LLM_FALLBACK_MODEL,
    CRAWLER_VISION_LLM_EXTRA_FALLBACKS,
)
from scrapers.node_crawler_base import (
    CrawlerRunError, run_ts_crawler, rows_for_company, save_crawler_blob,
)

SOURCE_NAME = "Digital Maturity Crawler"
CRAWLER_DIR = "digital-maturity-crawler"
DATASET_NAME = "crawler_digital_maturity"
PHASE = 7

# A vision assessment is a qualitative "does this look modern" read, not a measured redesign
# year — deliberately coarse (whole-year buckets) so it isn't mistaken for the Wayback method's
# precision. Capped at 8, matching the existing Wayback-derived age's own cap just below.
_VISION_SCORE_TO_AGE_YEARS = {5: 0.5, 4: 2.0, 3: 4.0, 2: 6.0, 1: 8.0}

# Default run_timeout for one call to this crawler — Wayback's deliberately rate-limited CDX
# calls plus (when configured) a browser launch + screenshot + vision call. Exported so
# scrapers/competitor_benchmark.py's repeated calls (once per named competitor) budget the same.
RUN_TIMEOUT_SECONDS = 250


def build_crawler_env() -> dict:
    """
    The env_overrides every call to this crawler needs: Wayback's courtesy delay, plus
    VISION_LLM_* (mirrors company_website_crawler.py's own LLM_* env-passthrough exactly — same
    fallback-chain shape, separate namespace, see config.py) when a vision key is configured.
    Pulled out so scrapers/competitor_benchmark.py's calls (run against named competitors'
    homepages, not a tracked Company) stay configured identically to this crawler's own.
    """
    env = {"WAYBACK_DELAY_MS": "1500"}
    if CRAWLER_VISION_LLM_API_KEY:
        env.update({"VISION_LLM_API_KEY": CRAWLER_VISION_LLM_API_KEY,
                    "VISION_LLM_BASE_URL": CRAWLER_VISION_LLM_BASE_URL,
                    "VISION_LLM_MODEL": CRAWLER_VISION_LLM_MODEL})
        if CRAWLER_VISION_LLM_FALLBACK_API_KEY:
            env["VISION_LLM_FALLBACK_API_KEY"] = CRAWLER_VISION_LLM_FALLBACK_API_KEY
            if CRAWLER_VISION_LLM_FALLBACK_BASE_URL:
                env["VISION_LLM_FALLBACK_BASE_URL"] = CRAWLER_VISION_LLM_FALLBACK_BASE_URL
            if CRAWLER_VISION_LLM_FALLBACK_MODEL:
                env["VISION_LLM_FALLBACK_MODEL"] = CRAWLER_VISION_LLM_FALLBACK_MODEL
            for extra in CRAWLER_VISION_LLM_EXTRA_FALLBACKS:
                env[f"VISION_LLM_FALLBACK{extra['suffix']}_API_KEY"] = extra["api_key"]
                if extra["base_url"]:
                    env[f"VISION_LLM_FALLBACK{extra['suffix']}_BASE_URL"] = extra["base_url"]
                if extra["model"]:
                    env[f"VISION_LLM_FALLBACK{extra['suffix']}_MODEL"] = extra["model"]
    return env


def _derive_signals(row: dict) -> dict:
    """
    Same field_status-first rule as the other Phase 7 wrappers: Wayback coverage
    is patchy for smaller company sites, and this indicator's own catalog comment
    says to treat "no snapshot found" as inconclusive, NOT as evidence of an old
    site — so a not_found redesign estimate writes nothing at all.
    """
    signals = {}
    field_status = row.get("field_status") or {}
    homepage = row.get("homepage_url")

    # Read once up front; attached to whichever source ends up writing website_digital_maturity
    # below (Wayback or this), and otherwise simply unused — never a reason to write a signal
    # on its own, since none of its fields map to a scored indicator today.
    visual = row.get("visual_assessment") if field_status.get("visual_assessment") == "value" else None
    visual_evidence_extra = {"visual_assessment": visual} if visual else {}

    redesign = row.get("last_major_redesign_estimate") or {}
    year = redesign.get("estimated_year")
    comparisons = redesign.get("comparisons") or []
    wayback_urls = [f"https://web.archive.org/web/*/{homepage}"] if homepage else []
    if field_status.get("last_major_redesign_estimate") == "value" and year:
        age = float(min(datetime.utcnow().year - int(year), 8))
        signals["website_digital_maturity"] = {
            "value": age, "status": "present",
            "summary": f"last major redesign estimated {int(year)} ({int(age)} years ago)",
            "evidence": {
                "method": "structural diff between Wayback Machine snapshots year over year",
                "estimated_redesign_year": int(year),
                "snapshot_comparisons": comparisons,
                "earliest_snapshot_date": row.get("earliest_snapshot_date"),
                "source_urls": wayback_urls,
                **visual_evidence_extra,
            },
        }
    elif field_status.get("last_major_redesign_estimate") == "value" and comparisons:
        # Every sampled year-over-year pair was structurally stable: that is positive
        # evidence the site has NOT been redesigned since the oldest compared capture —
        # the very thing this NEED indicator measures — and it used to be thrown away.
        # In the 2026-09-18 run 4 of 16 companies had it (fimer.it: three consecutive
        # years within 0.2%), while the only company that did get a value was the one
        # with a spurious "redesign". Written as a floor: at least this many years.
        stable_since = min(int(c["from_year"]) for c in comparisons if c.get("from_year") is not None)
        latest = max(int(c["to_year"]) for c in comparisons if c.get("to_year") is not None)
        age = float(min(max(datetime.utcnow().year - stable_since, 0), 8))
        signals["website_digital_maturity"] = {
            "value": age, "status": "present",
            "summary": f"no substantial redesign detected between {stable_since} and {latest}: "
                       f"at least {int(age)} years since the last one",
            "evidence": {
                "method": "structural diff between Wayback Machine snapshots year over year — all sampled pairs "
                          "stable, so the value is a floor (the last redesign predates the oldest compared capture)",
                "stable_since_year": stable_since,
                "snapshot_comparisons": comparisons,
                "earliest_snapshot_date": row.get("earliest_snapshot_date"),
                "source_urls": wayback_urls,
                **visual_evidence_extra,
            },
        }

    # Neither Wayback branch above produced a value (archive.org never captured enough of this
    # site) — fall back to the vision read, a direct "does this look dated" judgment that doesn't
    # depend on archive coverage at all. Only used when Wayback left the indicator genuinely
    # blank; a vision read never overrides a real measured redesign year.
    if "website_digital_maturity" not in signals and visual:
        score = visual.get("design_modernity_score")
        age = _VISION_SCORE_TO_AGE_YEARS.get(int(score)) if score is not None else None
        if age is not None:
            reasoning = visual.get("design_modernity_reasoning") or ""
            signals["website_digital_maturity"] = {
                "value": age, "status": "present",
                "summary": f"vision assessment of the current homepage (no Wayback history available): "
                           f"design_modernity_score {int(score)}/5 — {reasoning}",
                "evidence": {
                    "method": "vision LLM assessment of one homepage screenshot — a qualitative estimate, "
                              "used only because the Wayback structural-diff method found no usable snapshot "
                              "history for this site",
                    "design_modernity_score": score,
                    "design_modernity_reasoning": reasoning,
                    "source_urls": [homepage] if homepage else [],
                    **visual_evidence_extra,
                },
            }

    # The 0-5 composite is only meaningful if the homepage was actually fetched —
    # has_ecommerce defaults to false on a failed fetch, which would otherwise
    # read as a confirmed "no online presence" and score maximum need.
    has_ecommerce = row.get("has_ecommerce")
    social_links = row.get("social_presence_links") or []
    snapshot_count = row.get("snapshot_count_last_5_years")
    if field_status.get("has_ecommerce") == "value" or field_status.get("social_presence_links") == "value":
        ecom_pts = 2.0 if has_ecommerce else 0.0
        social_pts = float(min(len(social_links), 2))
        # The archive-activity point is only awarded or withheld when the CDX count
        # actually came back. Wayback's CDX API fails often (503s, timeouts) and every
        # company in the first live batch had snapshot_count=None — treating "unknown"
        # as "fewer than 10" silently docked each of them a point, i.e. manufactured
        # extra need from a data gap. When unknown, the composite is scored out of the
        # 4 points that were actually measured and rescaled to the 0-5 range.
        activity_known = snapshot_count is not None
        activity_pts = 1.0 if (activity_known and snapshot_count >= 10) else 0.0
        max_pts = 5.0 if activity_known else 4.0
        score = min((ecom_pts + social_pts + activity_pts) / max_pts * 5.0, 5.0)
        parts = [f"e-commerce {'detected' if has_ecommerce else 'not detected'} (+{ecom_pts:.0f})",
                 f"{len(social_links)} social profile(s) (+{social_pts:.0f})",
                 (f"{snapshot_count} archive snapshots (+{activity_pts:.0f})" if activity_known
                  else "archive activity unknown (Wayback lookup failed — excluded, scored out of 4 and rescaled)")]
        signals["online_market_presence"] = {
            "value": round(score, 2), "status": "present" if score > 0 else "absent",
            "summary": f"{score:.1f}/5 — " + "; ".join(parts),
            "evidence": {
                "method": "composite: e-commerce +2, each social profile +1 (max 2), 10+ archive snapshots +1"
                          + ("" if activity_known else "; archive point excluded (unknown), rescaled from /4 to /5"),
                "score_breakdown": {"ecommerce": ecom_pts, "social_profiles": social_pts,
                                     "archive_activity": activity_pts if activity_known else None,
                                     "points_measured": max_pts},
                "has_ecommerce": has_ecommerce,
                "social_profiles": [{"label": s.get("platform"), "url": s.get("url")} for s in social_links],
                "snapshot_count_last_5_years": snapshot_count,
                "data_source": row.get("data_source"),
                "source_urls": [homepage] if homepage else [],
            },
        }

    return signals


def sync_digital_maturity(company, db_session: Session) -> dict:
    captured = {}

    def _fetch_live(c):
        # Wayback's CDX API is deliberately rate-limited (WAYBACK_DELAY_MS=800 between
        # requests, several snapshots fetched for the redesign-year comparison) —
        # measured live against example.com as consistently exceeding the 90s default
        # other Phase 7 wrappers use, so this one gets a larger budget end to end.
        # 1.5s between Wayback calls (crawler default 0.8s): several companies now run in
        # parallel, and archive.org rate-limits per IP — in the 2026-09-18 batch 71% of
        # CDX calls came back 503/timeout. The crawler also retries each call with
        # back-off, so the subprocess budget grows to match. 80s of RUN_TIMEOUT_SECONDS is
        # headroom for the browser launch + screenshot + vision call build_crawler_env() adds
        # when configured; unset VISION_LLM_API_KEY means that phase is skipped inside the TS
        # crawler itself (no browser launched at all), so it's headroom, not a cost paid on every run.
        rows = run_ts_crawler(CRAWLER_DIR, [{"company_id": c.id, "homepage_url": c.website_url}],
                               env_overrides=build_crawler_env(), run_timeout=RUN_TIMEOUT_SECONDS)
        matches = rows_for_company(rows, c.id)
        if not matches:
            raise CrawlerRunError("digital-maturity-crawler returned no row for this company")
        row = matches[0]
        captured["row"] = row
        redesign = row.get("last_major_redesign_estimate") or {}
        wayback_covered = bool(redesign.get("estimated_year")) or bool(redesign.get("comparisons"))
        used_vision_for_maturity = not wayback_covered and (row.get("field_status") or {}).get("visual_assessment") == "value"
        confidence = 0.75 if row.get("data_source") == "builtwith_api" else 0.55
        if used_vision_for_maturity:
            # A qualitative vision read standing in for an entirely missing Wayback history —
            # never let it read as confidently as a real measured redesign year would.
            confidence = min(confidence, 0.6)
        return {
            "signals": _derive_signals(row),
            "raw_payload": {"data_source": row.get("data_source"),
                             "earliest_snapshot_date": row.get("earliest_snapshot_date")},
            "confidence": confidence,
        }

    def _simulate(c):
        return {
            "signals": {},
            "raw_payload": {"note": "digital-maturity-crawler unavailable or no website_url on record"},
            "confidence": 0.5,
        }

    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=bool(company.website_url),
        fetch_live=_fetch_live, simulate=_simulate, timeout=270,
    )

    if captured.get("row"):
        save_crawler_blob(db_session, company, DATASET_NAME, captured["row"])

    return result
