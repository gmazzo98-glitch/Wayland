"""
Raw press-mention comparison against manually named competitors (models.Competitor —
see its own docstring for why this list is hand-curated, never auto-discovered).

Is this company appearing in the news less often than its named peers? No indicator
exists for that question yet (same open decision as scrapers/competitor_hiring_benchmark.py
for hiring velocity) — GG has not chosen a signal key or weight for peer-relative press
share, so this writes a raw comparison blob only, never a SignalRecord (Ground rule 1).

Reuses adapters.google_news_rss's exact query/attribution mechanics (one quoted-name
query per entity, headlines bucketed into concepts locally, the same generic-name and
legal-form-suffix guards that module's own docstring documents) rather than re-deriving
them — see that module for why the query is built the way it is. The company's OWN
press-mention count is read from its already-saved Phase 4 blob
(RawImportRecord.dataset_name == "crawler_news_rss", written by
adapters.google_news_rss.sync_news_rss) instead of re-fetched here — this module only
spends a live call on each named competitor, the one thing that doesn't already exist.
"""

from sqlalchemy.orm import Session
from types import SimpleNamespace

from adapters import google_news_rss
from adapters.base import run_adapter
from models import RawImportRecord
from scrapers.node_crawler_base import CrawlerRunError, save_crawler_blob

SOURCE_NAME = "Competitor News Benchmark"
DATASET_NAME = "crawler_competitor_news"
# Literal, not a shared constant — adapters/google_news_rss.py's sync_news_rss writes this
# same string directly and doesn't export it.
OWN_NEWS_DATASET = "crawler_news_rss"
PHASE = 7
MAX_COMPETITORS = 3


def _peer_proxy(name: str, country: str | None) -> SimpleNamespace:
    """Stands in for the `company` object adapters.google_news_rss._filter_headlines
    expects — a named competitor only has a name and a homepage, no province/region/
    sector, so those read as None, which is the honest outcome: a short, ambiguous
    peer name without a discriminator falls back to 'skip' rather than guessing."""
    return SimpleNamespace(legal_name=name, country=country, province=None, region=None, sector_name=None)


def press_mentions(name: str, country: str | None = None) -> dict:
    """One live query for `name`'s press coverage, reusing google_news_rss's fetch +
    attribution + concept-bucketing exactly. Always returns a dict with `mention_count`
    (None when the name was too generic to attribute safely — never a guessed zero) so a
    caller can tell 'measured zero headlines' apart from 'could not measure'."""
    if not google_news_rss._name_is_distinctive(name):
        return {
            "name": name, "skipped": "name is too generic to attribute news coverage safely",
            "search_phrase": google_news_rss._search_name(name), "mention_count": None,
            "headlines_by_concept": {}, "headlines": [],
        }
    lang, country_code = google_news_rss.LOCALES.get(country or "", google_news_rss.DEFAULT_LOCALE)
    search_phrase = google_news_rss._search_name(name)
    items = google_news_rss._fetch(f'"{search_phrase}" when:{google_news_rss.RECENCY_DAYS}d', lang, country_code)
    headlines, stats = google_news_rss._filter_headlines(items, _peer_proxy(name, country))
    return {
        "name": name, "skipped": None, "search_phrase": search_phrase,
        "locale": f"{lang}-{country_code}", "mention_count": len(headlines),
        "attribution_mode": stats["attribution_mode"],
        "headlines_by_concept": {c: len(google_news_rss._bucket(headlines, c)) for c in google_news_rss.CONCEPT_PATTERNS},
        "headlines": [{"headline": h["headline"], "outlet": h["outlet"], "url": h["url"], "published": h["published"]}
                      for h in headlines],
    }


def sync_competitor_news_benchmark(company, db_session: Session) -> dict:
    competitors = [c for c in (company.competitors or []) if c.name][:MAX_COMPETITORS]
    if not competitors:
        return {"status": "skipped",
                "reason": "No named competitor recorded — add one or more in Company Intelligence."}

    own_record = db_session.query(RawImportRecord).filter_by(
        company_id=company.id, dataset_name=OWN_NEWS_DATASET
    ).first()
    if own_record is None or not isinstance(own_record.raw_row, dict):
        return {"status": "skipped",
                "reason": "Own Google News RSS raw data has not been measured yet — run Phase 4 first, then retry."}
    own_mention_count = own_record.raw_row.get("headlines_about_company")
    if own_mention_count is None:
        return {"status": "skipped",
                "reason": "This company's own press-mention count is unavailable (name too generic to "
                          "attribute safely, or Google News RSS hasn't run yet) — re-run Phase 4 first."}

    captured = {}

    def _fetch_live(_company):
        peers = []
        for competitor in competitors:
            entry = {"competitor_name": competitor.name, "homepage_url": competitor.homepage_url}
            try:
                entry.update(press_mentions(competitor.name, company.country))
                entry["error"] = None
            except Exception as exc:  # one peer's failure (network, parse) must not sink the rest
                entry.update({"mention_count": None, "headlines_by_concept": {}, "headlines": [], "error": str(exc)})
            peers.append(entry)
        captured["blob"] = {
            "note": "Raw peer press-mention counts only; no indicator or weight has been approved.",
            "company": {"name": company.legal_name, "mention_count": own_mention_count,
                        "headlines_by_concept": own_record.raw_row.get("headlines_by_concept", {})},
            "competitors": peers,
        }
        if not any(peer["mention_count"] is not None for peer in peers):
            raise CrawlerRunError("No named competitor's press coverage could be measured "
                                  "(every name was too generic to attribute safely, or every fetch failed)")
        return {
            "signals": {},
            "raw_payload": {"competitors_recorded": len(peers),
                            "competitors_measured": sum(p["mention_count"] is not None for p in peers)},
            "confidence": 0.55,  # same headline-keyword-match caveat as google_news_rss._fetch_live
        }

    def _simulate(_company):
        return {"signals": {}, "raw_payload": {"note": "competitor news benchmark unavailable"}, "confidence": 0.5}

    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=True, fetch_live=_fetch_live, simulate=_simulate,
        timeout=len(competitors) * 20 + 30,
    )
    if "blob" in captured:
        save_crawler_blob(
            db_session, company, DATASET_NAME, captured["blob"],
            source_filename="Competitor News Benchmark — raw, unscored press-mention counts",
        )
    return result
