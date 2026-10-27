"""
Reference collector: what the company's own web server says about itself (HTTP response headers).

Written first as the worked example every other collector copies (ticket I02), and it fixes a real false negative:
scrapers/wappalyzer_local.py only knows 18 HTML signatures (WordPress, Shopify, ...), so ASCO Filtri's site, which
answers `Server: Microsoft-IIS/10.0` and `X-Powered-By: ASP.NET`, was recorded as "no technology detected".

What to copy from here:
  * all network access goes through ctx.fetch (polite, robots-aware), never `requests` directly;
  * a fact's quote is text that really occurs in what was fetched, checked with quote_in_text() before the Evidence
    is built; a fact that cannot be quoted is not emitted;
  * the collector states what it CHECKED even when it found nothing (`checked_absent`);
  * it emits only keys under its declared `produces` prefixes and writes nothing to the database.

Ticket I18 extends this to full technology, ERP/MES and security fingerprints.
"""

from __future__ import annotations

from typing import List

from intel.collector import Collector, CollectorContext, CollectorResult
from intel.fetch import normalise_url
from intel.ledger import Evidence, Fact, quote_in_text

# header name (lower-case) -> (fact key, how to word the value)
HEADER_FACTS = {
    "server": "digital.web_server",
    "x-powered-by": "digital.web_framework",
    "x-aspnet-version": "digital.web_framework",
    "x-aspnetmvc-version": "digital.web_framework",
    "x-generator": "digital.cms",
    "x-powered-by-plesk": "digital.hosting_panel",
    "x-drupal-cache": "digital.cms",
    "x-shopify-stage": "digital.ecommerce_platform",
}


def run(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult()
    url = normalise_url(ctx.company.get("website_url") or "")
    if not url:
        result.errors.append("company has no website_url")
        return result
    if ctx.fetch is None:
        result.errors.append("no fetcher supplied")
        return result
    page = ctx.fetch(url)
    result.pages_fetched += 1
    if not page.headers:
        result.errors.append(f"no response headers from {url}: {page.note or 'unreachable'}")
        return result
    header_text = "\n".join(f"{k}: {v}" for k, v in page.headers.items())
    lowered = {k.lower(): v for k, v in page.headers.items()}
    seen_keys = set()
    for name, key in HEADER_FACTS.items():
        value = lowered.get(name)
        if not value:
            continue
        quote = f"{_original_case(page.headers, name)}: {value}"
        if not quote_in_text(quote, header_text):   # cannot happen unless the header was altered; keep the gate anyway
            continue
        seen_keys.add(key)
        result.facts.append(Fact(
            key=key, value=value.strip(), grade="B", confidence=0.95, verified=True, collector="I02:site-headers@1",
            evidence=[Evidence(source_type="company_site", url=page.url, quote=quote, content_hash=page.content_hash)]))
    for key in ("digital.web_server", "digital.web_framework", "digital.cms"):
        if key not in seen_keys:
            result.checked_absent.append(key)
    return result


def _original_case(headers, lowered_name: str) -> str:
    for k in headers:
        if k.lower() == lowered_name:
            return k
    return lowered_name


COLLECTOR = Collector(
    name="site-headers", ticket="I02", produces=("digital.",), requires=("web",), refresh_days=180, cost_hint="cheap",
    description="Web server, framework, CMS and hosting panel announced in the HTTP response headers of the homepage.",
    run=run,
)
