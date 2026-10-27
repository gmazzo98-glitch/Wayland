"""
Wraps Scraper/crawlers/deep-profile-crawler (Ticket I10).
Performs breadth-first whole-site crawling, page classification, document (PDF) inventory,
and profile fact extraction (identity, history, locations, products, certifications, contacts).

Feeds dataset: crawler_deep_profile (RawImportRecord).
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from scrapers.node_crawler_base import (
    CrawlerRunError,
    run_ts_crawler,
    save_crawler_blob,
)

SOURCE_NAME = "Deep Profile Crawler"
CRAWLER_DIR = "deep-profile-crawler"
DATASET_NAME = "crawler_deep_profile"
PHASE = 7
DEFAULT_RUN_TIMEOUT = 240


def run_crawler_for_company(
    company_id: str,
    homepage_url: str,
    env_overrides: Optional[Dict[str, str]] = None,
    run_timeout: int = DEFAULT_RUN_TIMEOUT,
) -> Dict[str, Any]:
    """Runs deep-profile-crawler for a single company and returns the parsed result dict."""
    if not homepage_url:
        raise CrawlerRunError(f"Company {company_id} has no homepage_url")

    input_rows = [{"company_id": str(company_id), "homepage_url": homepage_url}]
    rows = run_ts_crawler(
        CRAWLER_DIR,
        input_rows,
        env_overrides=env_overrides,
        run_timeout=run_timeout,
    )

    if not rows:
        raise CrawlerRunError(f"No output dataset rows returned for {company_id}")

    return rows[0]


def save_result(db, company, result_dict: Dict[str, Any]) -> None:
    """Saves the deep profile result as a RawImportRecord blob."""
    save_crawler_blob(
        db,
        company,
        dataset_name=DATASET_NAME,
        raw_row=result_dict,
        source_name=SOURCE_NAME,
        phase=PHASE,
    )
